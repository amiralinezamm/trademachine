"""SPEC roadmap-rev2 4.4 — market structure filter (layer 1/2 price action).

Structure ("bullish" = higher-high/higher-low, "bearish" = lower-high/lower-low,
"unknown" otherwise) is derived from the SAME swing detector as levels.py
(_confirm_swing — CLAUDE.md rule 6: one swing definition, not reimplemented),
applied to H1 bars.

H1 ingestion (candles table, tf='H1') has been stale since 2026-09-15 (see
task flagged separately) -- rather than depend on it, H1 bars are resampled
on the fly from the caller-supplied M5 (or finer) candle history. This keeps
the filter always in sync with the same M5 feed levels.py and the signal
engine already use, with no extra ingestion dependency.

compute_market_structure() is pure -- no DB access -- and enforces its own
as_of_ts cutoff (CLAUDE.md rules 1/2), exactly like compute_levels(), so
live and replay call the identical function (rule 6).
"""
from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import talib
import yaml

from src.features.levels import _confirm_swing

PARAMS_PATH = Path(__file__).resolve().parents[2] / "config" / "params.yaml"


def load_structure_params(path: Path = PARAMS_PATH) -> dict[str, Any]:
    with open(path) as f:
        return yaml.safe_load(f)["market_structure"]


def _resample_to_h1(candles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate finer-timeframe candles into H1 bars, bucketed by the hour
    each candle's ts_utc falls in. Caller must have already filtered to
    ts_utc <= as_of_ts, so a bucket only appears here if at least one input
    candle in that hour has been seen -- an in-progress (not yet closed)
    hour still gets included using whatever partial data exists so far,
    exactly mirroring how the live M5 feed itself is always "as of now"."""
    buckets: dict[datetime, list[dict[str, Any]]] = {}
    for c in candles:
        hour_ts = c["ts_utc"].replace(minute=0, second=0, microsecond=0)
        buckets.setdefault(hour_ts, []).append(c)
    out = []
    for hour_ts in sorted(buckets):
        bars = buckets[hour_ts]
        out.append({
            "ts_utc": hour_ts,
            "open": float(bars[0]["open"]),
            "high": max(float(b["high"]) for b in bars),
            "low": min(float(b["low"]) for b in bars),
            "close": float(bars[-1]["close"]),
        })
    return out


def detect_h1_swings(
    h1_candles: list[dict[str, Any]],
    params: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Confirmed swing highs/lows over the given (already as_of_ts-filtered)
    H1 bars, using the exact same swing math as levels.py's walk-forward.
    Each entry: {swing_idx, swing_ts, confirmed_ts, kind, price}.
    confirmed_ts = swing_ts + N H1-bars -- a swing must not be visible to
    any caller before this point (anti-repaint, CLAUDE.md rule 1/2).
    Pure, as_of_ts-unaware by design -- callers filter on confirmed_ts."""
    if params is None:
        params = load_structure_params()
    N = params["swing_n"]
    k = params["swing_k"]
    atr_period = params["atr_period"]

    n = len(h1_candles)
    if n < 2 * N + atr_period + 1:
        return []

    highs = np.array([c["high"] for c in h1_candles])
    lows = np.array([c["low"] for c in h1_candles])
    closes = np.array([c["close"] for c in h1_candles])
    ts = [c["ts_utc"] for c in h1_candles]
    atr = talib.ATR(highs, lows, closes, timeperiod=atr_period)

    swings = []
    for swing_idx in range(N, n - N):
        if math.isnan(atr[swing_idx]):
            continue
        is_high, is_low = _confirm_swing(highs, lows, atr, swing_idx, N, k)
        confirmed_ts = ts[swing_idx + N]
        if is_high:
            swings.append({
                "swing_idx": swing_idx, "swing_ts": ts[swing_idx],
                "confirmed_ts": confirmed_ts, "kind": "high",
                "price": float(highs[swing_idx]),
            })
        if is_low:
            swings.append({
                "swing_idx": swing_idx, "swing_ts": ts[swing_idx],
                "confirmed_ts": confirmed_ts, "kind": "low",
                "price": float(lows[swing_idx]),
            })
    return swings


def structure_from_swings(
    swings: list[dict[str, Any]],
    as_of_ts: datetime,
    cur_close: float,
) -> dict[str, Any]:
    """Pure reduction: given a (possibly precomputed, full-range) swings
    list, the current as_of_ts, and the current close, return the market
    structure. Filters to swings whose confirmed_ts <= as_of_ts (anti-repaint)
    before looking at the last two swing highs and last two swing lows."""
    visible = [s for s in swings if s["confirmed_ts"] <= as_of_ts]
    highs = [s for s in visible if s["kind"] == "high"]
    lows = [s for s in visible if s["kind"] == "low"]

    if len(highs) < 2 or len(lows) < 2:
        return {"structure": "unknown", "reason": "not_enough_swings"}

    last_hi, prev_hi = highs[-1], highs[-2]
    last_lo, prev_lo = lows[-1], lows[-2]

    bullish = last_hi["price"] > prev_hi["price"] and last_lo["price"] > prev_lo["price"]
    bearish = last_hi["price"] < prev_hi["price"] and last_lo["price"] < prev_lo["price"]
    structure = "bullish" if bullish else "bearish" if bearish else "unknown"

    # Break-of-structure: a close beyond the most recent opposing swing
    # invalidates the current structure -> unknown, NOT the opposite trend
    # (a single break doesn't prove a new trend -- explicit spec).
    if structure == "bearish" and cur_close > last_hi["price"]:
        structure = "unknown"
    elif structure == "bullish" and cur_close < last_lo["price"]:
        structure = "unknown"

    return {
        "structure": structure,
        "last_swing_high": {"ts_utc": last_hi["swing_ts"], "price": last_hi["price"]},
        "prev_swing_high": {"ts_utc": prev_hi["swing_ts"], "price": prev_hi["price"]},
        "last_swing_low": {"ts_utc": last_lo["swing_ts"], "price": last_lo["price"]},
        "prev_swing_low": {"ts_utc": prev_lo["swing_ts"], "price": prev_lo["price"]},
    }


def compute_market_structure(
    candles: list[dict[str, Any]],
    as_of_ts: datetime,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Convenience single-call entry point for live use: resample + detect
    + reduce in one call, from scratch, every time. Cheap for live (called
    once per signal check on a small lookback window). Replay pre-computes
    detect_h1_swings() ONCE over the full history and calls
    structure_from_swings() per bar instead, to avoid O(bars^2) -- same
    underlying pure functions, just a different (and much cheaper) iteration
    wrapper, the same pattern as fetch_candles(lookback_bars=...)."""
    if params is None:
        params = load_structure_params()
    hist = [c for c in candles if c["ts_utc"] <= as_of_ts]
    if not hist:
        return {"structure": "unknown", "reason": "no_candles"}
    h1 = _resample_to_h1(hist)
    swings = detect_h1_swings(h1, params)
    cur_close = float(hist[-1]["close"])
    return structure_from_swings(swings, as_of_ts, cur_close)
