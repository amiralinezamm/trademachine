"""SPEC.md 4.6 (`fibonacci`) -- confirmation-only Fibonacci levels.

Rules:
- Swing input from the `levels` table (status IN active/flipped), NEVER
  computed independently -- removes cherry-pick bias (SPEC.md explicit).
- Retracement levels (38.2/50/61.8): entry-zone candidates (role='retracement').
- Extension levels (61.8/100/161.8): profit-target only (role='extension').
- A level scores (overlapping=True) only when its price falls within
  overlap_atr_mult*ATR of an active S/R level zone.
- Anti-lookahead: as_of_ts enforced; candles after it are silently dropped.

DESIGN DECISION -- pairing (SPEC unspecified):
  All pairs (active support S, active resistance R) where S.mid < R.mid
  and abs(R.mid - S.mid) < max_pair_distance_atr * ATR are used.
  Limited to the closest `max_pairs` pairs by proximity to current price.
  Awaiting user confirmation on pairing logic.
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

RETRACEMENT_PCTS = [38.2, 50.0, 61.8]
EXTENSION_PCTS   = [61.8, 100.0, 161.8]


def load_fibonacci_params(path: Path = PARAMS_PATH) -> dict[str, Any]:
    with open(path) as f:
        return yaml.safe_load(f)["fibonacci"]


def compute_fibonacci(
    candles: list[dict[str, Any]],
    as_of_ts: datetime,
    symbol: str,
    tf: str,
    active_levels: list[dict[str, Any]],
    params: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Pure function. `active_levels` must already be filtered to
    status IN ('active','flipped') by the caller (from the DB).
    Returns a list of fib zone dicts -- one per (pair, level_pct)."""
    if params is None:
        params = load_fibonacci_params()

    hist = sorted(
        (c for c in candles if c["ts_utc"] <= as_of_ts),
        key=lambda c: c["ts_utc"],
    )
    if len(hist) < params["atr_period"] + 1:
        return []

    highs  = np.array([float(c["high"])  for c in hist])
    lows   = np.array([float(c["low"])   for c in hist])
    closes = np.array([float(c["close"]) for c in hist])
    atr_arr = talib.ATR(highs, lows, closes, timeperiod=params["atr_period"])
    cur_atr = float(atr_arr[-1])
    if math.isnan(cur_atr) or cur_atr <= 0:
        return []

    cur_close  = float(closes[-1])
    max_dist   = params["max_pair_distance_atr"] * cur_atr
    overlap_th = params["overlap_atr_mult"] * cur_atr

    supports    = [l for l in active_levels if l["kind"] == "support"]
    resistances = [l for l in active_levels if l["kind"] == "resistance"]

    def mid(lvl: dict) -> float:
        return (float(lvl["price_low"]) + float(lvl["price_high"])) / 2.0

    # Build pairs (S support below, R resistance above)
    pairs: list[tuple[float, float]] = []
    for s in supports:
        s_mid = mid(s)
        for r in resistances:
            r_mid = mid(r)
            if r_mid <= s_mid:
                continue
            rng = r_mid - s_mid
            if rng > max_dist:
                continue
            pairs.append((s_mid, r_mid))

    if not pairs:
        return []

    # Keep closest max_pairs pairs by how close current price is to the range
    def pair_distance(pair: tuple[float, float]) -> float:
        s, r = pair
        if s <= cur_close <= r:
            return 0.0
        return min(abs(cur_close - s), abs(cur_close - r))

    pairs.sort(key=pair_distance)
    pairs = pairs[: params["max_pairs"]]

    # Build overlap lookup: level mids from all active levels
    level_mids = [mid(l) for l in active_levels]

    def is_overlapping(price: float) -> bool:
        return any(abs(price - lm) < overlap_th for lm in level_mids)

    results: list[dict[str, Any]] = []
    for s_mid, r_mid in pairs:
        rng = r_mid - s_mid

        for pct in params["retracement_levels"]:
            price = r_mid - (pct / 100.0) * rng
            results.append({
                "symbol":      symbol,
                "tf_origin":   tf,
                "computed_at": as_of_ts,
                "swing_low":   s_mid,
                "swing_high":  r_mid,
                "range_size":  rng,
                "level_pct":   pct,
                "price":       round(price, 5),
                "role":        "retracement",
                "overlapping": is_overlapping(price),
            })

        for pct in params["extension_levels"]:
            price = r_mid + (pct / 100.0) * rng
            results.append({
                "symbol":      symbol,
                "tf_origin":   tf,
                "computed_at": as_of_ts,
                "swing_low":   s_mid,
                "swing_high":  r_mid,
                "range_size":  rng,
                "level_pct":   pct,
                "price":       round(price, 5),
                "role":        "extension",
                "overlapping": is_overlapping(price),
            })

    return results
