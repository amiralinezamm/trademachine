"""SPEC.md 4.5 (`gaps`) — gaps as probable price targets with decaying weight.

Reference stat (Osler study, 211 FX gaps): 78.6% filled, median 1h, 90% within 24h.
Initial weight 0.78 per SPEC; weight decays exponentially over time (decay rate is a
design decision — SPEC does not specify a rate; see params.yaml note).

compute_gaps() is pure — no DB access (CLAUDE.md rule 6).
Anti-lookahead: the as_of_ts cutoff is enforced at the top of the function.
"""
from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

PARAMS_PATH = Path(__file__).resolve().parents[2] / "config" / "params.yaml"


def load_gaps_params(path: Path = PARAMS_PATH) -> dict[str, Any]:
    with open(path) as f:
        return yaml.safe_load(f)["gaps"]


def compute_gaps(
    candles: list[dict[str, Any]],
    as_of_ts: datetime,
    symbol: str,
    tf: str,
    params: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Detect gaps between consecutive candles and track their fill status.

    A gap forms when open[i] differs from close[i-1] by more than min_size.

    For each gap, walk forward through subsequent candles (up to as_of_ts) to
    determine whether price reached the 50% mark and/or fully filled the gap.

    fill_mode='body': fill is triggered when the candle CLOSE crosses the
                      fill level (matches the user's stated preference).
    fill_mode='wick': fill is triggered when the candle HIGH or LOW touches
                      the fill level.

    Returns list of gap dicts matching the `gaps` table schema.
    """
    if params is None:
        params = load_gaps_params()

    # --- Anti-lookahead ---
    hist = sorted(
        (c for c in candles if c["ts_utc"] <= as_of_ts),
        key=lambda c: c["ts_utc"],
    )
    n = len(hist)
    if n < 2:
        return []

    min_size          = float(params["min_size_dollars"])   # e.g. 3.0
    initial_weight    = float(params["initial_weight"])      # 0.78
    decay_per_hour    = float(params["decay_rate_per_hour"]) # design param, SPEC unspecified
    half_fraction     = float(params["half_fill_fraction"])  # 0.5
    fill_mode: str    = params.get("fill_mode", "body")      # 'body' or 'wick'

    opens  = [float(c["open"])  for c in hist]
    highs  = [float(c["high"])  for c in hist]
    lows   = [float(c["low"])   for c in hist]
    closes = [float(c["close"]) for c in hist]
    ts     = [c["ts_utc"]       for c in hist]

    # --- Pass 1: detect gaps ---
    raw_gaps: list[dict[str, Any]] = []
    for i in range(1, n):
        gap_up_size   = opens[i] - closes[i - 1]
        gap_down_size = closes[i - 1] - opens[i]

        if gap_up_size > min_size:
            raw_gaps.append({
                "start_idx": i,
                "ts_utc":    ts[i],
                "direction": "UP",
                "gap_high":  opens[i],       # top of the empty range
                "gap_low":   closes[i - 1],  # bottom of the empty range
                "size":      gap_up_size,
                "half_fill_ts": None,
                "fill_ts":      None,
                "status":    "OPEN",
            })
        elif gap_down_size > min_size:
            raw_gaps.append({
                "start_idx": i,
                "ts_utc":    ts[i],
                "direction": "DOWN",
                "gap_high":  closes[i - 1],  # top of the empty range
                "gap_low":   opens[i],       # bottom of the empty range
                "size":      gap_down_size,
                "half_fill_ts": None,
                "fill_ts":      None,
                "status":    "OPEN",
            })

    # --- Pass 2: walk forward and track fill status ---
    for gap in raw_gaps:
        mid = gap["gap_low"] + (gap["gap_high"] - gap["gap_low"]) * half_fraction
        direction = gap["direction"]

        for j in range(gap["start_idx"] + 1, n):
            if direction == "UP":
                # Filling a gap UP means price drops back toward gap_low (prev_close)
                price_for_half = lows[j]  if fill_mode == "wick" else closes[j]
                price_for_full = lows[j]  if fill_mode == "wick" else closes[j]
                reached_half = price_for_half <= mid
                reached_full = price_for_full <= gap["gap_low"]
            else:  # DOWN
                # Filling a gap DOWN means price rises back toward gap_high (prev_close)
                price_for_half = highs[j] if fill_mode == "wick" else closes[j]
                price_for_full = highs[j] if fill_mode == "wick" else closes[j]
                reached_half = price_for_half >= mid
                reached_full = price_for_full >= gap["gap_high"]

            if gap["half_fill_ts"] is None and reached_half:
                gap["half_fill_ts"] = ts[j]
                gap["status"]       = "HALF_FILLED"

            if reached_full:
                gap["fill_ts"]  = ts[j]
                gap["status"]   = "FILLED"
                break

    # --- Compute weight as of the last bar ---
    last_ts = ts[-1]

    result = []
    for gap in raw_gaps:
        hours_elapsed = (last_ts - gap["ts_utc"]).total_seconds() / 3600.0
        current_weight = initial_weight * math.exp(-decay_per_hour * hours_elapsed)
        current_weight = max(0.0, current_weight)

        result.append({
            "symbol":         symbol,
            "tf":             tf,
            "ts_utc":         gap["ts_utc"],
            "direction":      gap["direction"],
            "gap_high":       float(gap["gap_high"]),
            "gap_low":        float(gap["gap_low"]),
            "size":           float(gap["size"]),
            "initial_weight": initial_weight,
            "weight":         current_weight,
            "half_fill_ts":   gap["half_fill_ts"],
            "fill_ts":        gap["fill_ts"],
            "status":         gap["status"],
            "fill_mode":      fill_mode,
        })
    return result


def backtest_two_phase_claim(
    candles: list[dict[str, Any]],
    as_of_ts: datetime,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """SPEC.md 4.5: test whether price reaches 50% of a gap and reverses
    before full fill, for both fill_mode='body' and fill_mode='wick'.

    Returns a summary dict to decide rule status (testing vs rejected).
    Acceptance threshold: frequency > 0.60 with sample_count > 30.
    """
    if params is None:
        params = load_gaps_params()

    results: dict[str, Any] = {}
    for mode in ("body", "wick"):
        p = {**params, "fill_mode": mode}
        gaps = compute_gaps(candles, as_of_ts, "__BACKTEST__", "__ALL__", params=p)
        total = len(gaps)
        half_reversed = sum(
            1 for g in gaps
            if g["half_fill_ts"] is not None and g["status"] != "FILLED"
        )
        filled = sum(1 for g in gaps if g["status"] == "FILLED")
        half_and_filled = sum(
            1 for g in gaps
            if g["half_fill_ts"] is not None and g["status"] == "FILLED"
        )
        not_reached = total - half_reversed - filled - half_and_filled + sum(
            1 for g in gaps
            if g["half_fill_ts"] is not None and g["status"] == "FILLED"
        )

        # Simpler: just count outcomes
        full_filled_count  = sum(1 for g in gaps if g["status"] == "FILLED")
        half_only_count    = sum(
            1 for g in gaps
            if g["half_fill_ts"] is not None and g["status"] != "FILLED"
        )
        neither_count      = total - full_filled_count - half_only_count

        freq = half_only_count / total if total > 0 else 0.0
        accepted = freq > 0.60 and total > 30

        results[mode] = {
            "fill_mode":        mode,
            "total_gaps":       total,
            "full_filled":      full_filled_count,
            "half_only":        half_only_count,
            "neither":          neither_count,
            "two_phase_freq":   freq,
            "accepted":         accepted,
            "rule_status":      "testing"  if accepted else "rejected",
        }

    return results
