"""SPEC.md 4.2 (`levels`) — support/resistance zones with a decaying
strength score.

CLAUDE.md rules 1/2 (anti-repainting — SPEC.md calls this the single most
important point in this module): compute_levels() takes as_of_ts and
enforces it itself — every candle with ts_utc > as_of_ts is dropped before
anything else runs, regardless of what the caller passed in. A swing at
bar i needs `swing_n` bars *after* it to confirm (both the structural and
retracement conditions look forward), so a level's created_ts can predate
as_of_ts by a lot, but the level itself never appears in the output until
as_of_ts has advanced past created_ts + swing_n bars. This is what the
anti-lookahead test in tests/features/test_levels.py checks directly.

compute_levels() is pure — no DB access — so backtest and live call the
exact same function (CLAUDE.md rule 6). It recomputes the full state from
raw candles on every call rather than mutating stored state incrementally;
simpler to prove correct, and cheap enough at this data volume (see
docs/ for the real-data timing).
"""
from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import talib
import yaml

PARAMS_PATH = Path(__file__).resolve().parents[2] / "config" / "params.yaml"


def load_levels_params(path: Path = PARAMS_PATH) -> dict[str, Any]:
    with open(path) as f:
        return yaml.safe_load(f)["levels"]


def _zone_gap(a_lo: float, a_hi: float, b_lo: float, b_hi: float) -> float:
    """Distance between two zones; <= 0 means they already overlap."""
    return max(a_lo, b_lo) - min(a_hi, b_hi)


def _add_or_merge(
    levels: list[dict[str, Any]],
    kind: str,
    new_lo: float,
    new_hi: float,
    created_ts: datetime,
    created_idx: int,
    atr_val: float,
    merge_gap_thresh: float,
) -> None:
    """SPEC.md 4.2: zones of the same kind closer than merge_atr_mult * ATR
    ARE one zone — merge into the existing (earlier) level rather than
    creating a duplicate row. Only active/flipped levels of the same
    (current) kind are eligible."""
    for lvl in levels:
        if lvl["kind"] != kind or lvl["status"] not in ("active", "flipped"):
            continue
        if _zone_gap(new_lo, new_hi, lvl["price_low"], lvl["price_high"]) < merge_gap_thresh:
            lvl["price_low"] = min(lvl["price_low"], new_lo)
            lvl["price_high"] = max(lvl["price_high"], new_hi)
            return
    levels.append(
        {
            "kind": kind,
            "price_low": new_lo,
            "price_high": new_hi,
            "created_ts": created_ts,
            "created_idx": created_idx,
            "last_touch": None,
            "touch_count": 0,
            "break_count": 0,
            "status": "active",
            "atr_at_birth": atr_val,
            "touches": [],
        }
    )


def compute_levels(
    candles: list[dict[str, Any]],
    as_of_ts: datetime,
    symbol: str,
    tf: str,
    params: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """candles: {"ts_utc","open","high","low","close"} dicts, any order/range —
    the as_of_ts cutoff is enforced here, not trusted from the caller.
    Returns level dicts matching the `levels` table columns."""
    if params is None:
        params = load_levels_params()

    hist = sorted((c for c in candles if c["ts_utc"] <= as_of_ts), key=lambda c: c["ts_utc"])
    n = len(hist)
    N = params["swing_n"]
    atr_period = params["atr_period"]
    if n < 2 * N + atr_period + 1:
        return []

    highs = np.array([float(c["high"]) for c in hist])
    lows = np.array([float(c["low"]) for c in hist])
    closes = np.array([float(c["close"]) for c in hist])
    ts = [c["ts_utc"] for c in hist]

    atr = talib.ATR(highs, lows, closes, timeperiod=atr_period)

    k = params["swing_k"]
    zone_mult = params["zone_width_atr_mult"]
    break_mult = params["break_atr_mult"]
    merge_mult = params["merge_atr_mult"]
    lam = params["strength_lambda"]
    expiry_strength = params["strength_expiry_threshold"]
    expiry_dist_mult = params["expiry_distance_atr_mult"]

    levels: list[dict[str, Any]] = []

    # --- Swing detection (SPEC.md 4.2: two conditions, both required) ---
    for i in range(N, n - N):
        if math.isnan(atr[i]):
            continue
        before_hi, after_hi = highs[i - N:i], highs[i + 1:i + N + 1]
        before_lo, after_lo = lows[i - N:i], lows[i + 1:i + N + 1]

        if highs[i] > before_hi.max() and highs[i] > after_hi.max():
            retrace = highs[i] - after_lo.min()
            if retrace >= k * atr[i]:
                half = zone_mult * atr[i] / 2
                _add_or_merge(levels, "resistance", highs[i] - half, highs[i] + half, ts[i], i, atr[i], merge_mult * atr[i])

        if lows[i] < before_lo.min() and lows[i] < after_lo.min():
            retrace = after_hi.max() - lows[i]
            if retrace >= k * atr[i]:
                half = zone_mult * atr[i] / 2
                _add_or_merge(levels, "support", lows[i] - half, lows[i] + half, ts[i], i, atr[i], merge_mult * atr[i])

    # --- Touch / break walk-forward ---
    # A level only interacts with bar j once it is itself confirmed as of j
    # (created_idx + N <= j) — this is what stops a level from affecting
    # (or being affected by) candles that predate its own confirmation.
    for j in range(n):
        if math.isnan(atr[j]):
            continue
        cur_high, cur_low, cur_close, cur_atr = highs[j], lows[j], closes[j], atr[j]
        for lvl in levels:
            if lvl["created_idx"] + N > j or lvl["status"] not in ("active", "flipped"):
                continue
            lo, hi = lvl["price_low"], lvl["price_high"]

            entered = cur_low <= hi and cur_high >= lo
            closed_outside = cur_close < lo or cur_close > hi
            if entered and closed_outside:
                lvl["touch_count"] += 1
                lvl["last_touch"] = ts[j]
                lvl["touches"].append(j)

            # SPEC.md: break = CLOSE beyond the zone, never wick-only.
            if lvl["kind"] == "resistance" and cur_close > hi + break_mult * cur_atr:
                lvl["break_count"] += 1
                lvl["kind"] = "support"
                lvl["status"] = "flipped"
            elif lvl["kind"] == "support" and cur_close < lo - break_mult * cur_atr:
                lvl["break_count"] += 1
                lvl["kind"] = "resistance"
                lvl["status"] = "flipped"

    # --- Strength + expiry, evaluated as of the last confirmed bar ---
    last_idx = n - 1
    cur_close, cur_atr = closes[last_idx], atr[last_idx]

    result = []
    for lvl in levels:
        if lvl["created_idx"] + N > last_idx:
            continue  # not confirmed yet as of as_of_ts -> must not appear

        strength = sum(math.exp(-lam * (last_idx - t_idx)) for t_idx in lvl["touches"])
        if lvl["status"] == "flipped":
            strength /= 2  # SPEC.md: halved on role flip

        status = lvl["status"]
        if status in ("active", "flipped") and not math.isnan(cur_atr):
            mid = (lvl["price_low"] + lvl["price_high"]) / 2
            if strength < expiry_strength or abs(cur_close - mid) > expiry_dist_mult * cur_atr:
                status = "expired"

        result.append(
            {
                "symbol": symbol,
                "tf_origin": tf,
                "kind": lvl["kind"],
                # numpy scalars (from array arithmetic above) confuse psycopg2's
                # adapter — it silently stringifies them as "np.float64(...)"
                # instead of a plain number. Cast to native Python types here,
                # the one place results leave numpy-land.
                "price_low": float(lvl["price_low"]),
                "price_high": float(lvl["price_high"]),
                "created_ts": lvl["created_ts"],
                "last_touch": lvl["last_touch"],
                "touch_count": int(lvl["touch_count"]),
                "break_count": int(lvl["break_count"]),
                "strength": float(strength),
                "status": status,
                "atr_at_birth": float(lvl["atr_at_birth"]),
            }
        )
    return result
