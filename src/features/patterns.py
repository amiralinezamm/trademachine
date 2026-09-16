"""SPEC.md 4.3 (`patterns`) — all 61 TA-Lib candlestick patterns.

compute_patterns() is pure (no DB access); patterns_store.py handles DB
reads and writes (CLAUDE.md rule 6: same function in backtest and live).

Anti-lookahead guarantee: every candle with ts_utc > as_of_ts is dropped
at the top of compute_patterns(), regardless of what the caller passed.
TA-Lib pattern detection is inherently backward-looking (no future bar
needed to detect a pattern on bar i), so the only look-ahead risk is
accidentally including future candles — the as_of_ts cutoff prevents that.

SPEC 4.3 storage: all 61 patterns stored (body_atr, context). The 0.7
body_atr threshold is an ENGINE filter (applied in signal evaluation),
NOT a storage filter — we store everything so we can analyse which patterns
have which statistics.
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

# All 61 TA-Lib CDL functions, sorted for deterministic ordering.
_ALL_CDL = sorted(name for name in dir(talib) if name.startswith("CDL"))


def load_patterns_params(path: Path = PARAMS_PATH) -> dict[str, Any]:
    with open(path) as f:
        return yaml.safe_load(f)["patterns"]


def compute_patterns(
    candles: list[dict[str, Any]],
    as_of_ts: datetime,
    symbol: str,
    tf: str,
    levels: list[dict[str, Any]] | None = None,
    params: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Detect all 61 TA-Lib patterns up to as_of_ts.

    candles: list of {"ts_utc","open","high","low","close"} dicts (any order).
    levels: list of level dicts from compute_levels() or the DB (may include
            "id" field for at_level_id linkage; omit or pass [] if unavailable).
    Returns list of pattern_hits dicts matching the pattern_hits table schema.
    """
    if params is None:
        params = load_patterns_params()

    # --- Anti-lookahead: enforce the as_of_ts cutoff ---
    hist = sorted((c for c in candles if c["ts_utc"] <= as_of_ts), key=lambda c: c["ts_utc"])
    n = len(hist)
    atr_period = params["atr_period"]
    if n < atr_period + 2:
        return []

    opens  = np.array([float(c["open"])  for c in hist])
    highs  = np.array([float(c["high"])  for c in hist])
    lows   = np.array([float(c["low"])   for c in hist])
    closes = np.array([float(c["close"]) for c in hist])
    ts_arr = [c["ts_utc"] for c in hist]

    atr = talib.ATR(highs, lows, closes, timeperiod=atr_period)

    # Pre-process levels for fast zone lookup
    # Only active/flipped levels are considered for at_level linkage
    active_levels: list[dict[str, Any]] = []
    if levels:
        active_levels = [
            lvl for lvl in levels
            if lvl.get("status") in ("active", "flipped")
        ]

    at_level_dist_mult = params["at_level_distance_atr_mult"]

    hits: list[dict[str, Any]] = []

    for cdl_name in _ALL_CDL:
        fn = getattr(talib, cdl_name)
        try:
            result = fn(opens, highs, lows, closes)
        except Exception:
            continue  # defensive: skip if TA-Lib raises (shouldn't normally happen)

        for i in range(n):
            val = int(result[i])
            if val == 0:
                continue
            if math.isnan(atr[i]) or atr[i] == 0:
                continue

            # body_atr = candle body size normalised by ATR
            body_size = abs(closes[i] - opens[i])
            body_atr = float(body_size / atr[i])

            # Find the nearest active level whose zone contains or is close
            # to this candle's price range.
            at_level_id: int | None = None
            level_strength: float | None = None

            candle_mid = (highs[i] + lows[i]) / 2.0
            best_dist = float("inf")
            for lvl in active_levels:
                zone_mid = (float(lvl["price_low"]) + float(lvl["price_high"])) / 2.0
                dist = abs(candle_mid - zone_mid)
                thresh = at_level_dist_mult * atr[i]
                if dist < thresh and dist < best_dist:
                    best_dist = dist
                    at_level_id = lvl.get("id")  # None if not yet in DB
                    level_strength = float(lvl["strength"])

            hits.append(
                {
                    "ts_utc": ts_arr[i],
                    "tf": tf,
                    "pattern": cdl_name,
                    "direction": val,  # +100 or -100 (TA-Lib convention)
                    "body_atr": body_atr,
                    "at_level_id": at_level_id,
                    "level_strength": level_strength,
                    "regime": None,  # populated by regime module once built
                    "symbol": symbol,  # convenience field, not in table schema
                }
            )

    return hits
