"""SPEC.md 4.9 — Market regime classifier + range analytics.

Classification rule (per-bar, no lookahead):
  trend : ADX >= trend_adx_thresh
  range : ADX < range_adx_thresh AND bb_width_pct <= range_bb_pct_thresh
  gray  : everything else (transitioning / ambiguous)

where bb_width_pct = percentile rank of current BB-width among the
last bb_width_pct_window bars (0–100).

Session definitions (UTC, overlapping intentionally):
  Asia   : 00:00–09:00
  London : 07:00–16:00
  NY     : 12:00–21:00

DESIGN DECISIONS (SPEC unspecified):
  - BB percentile window: rolling 1000 candles (config/params.yaml)
  - Session boundaries: standard UTC (above)
  - Breakout probability: fraction of range bars within [start, t] where
    the *next* bar close exits the BB band (BB_upper or BB_lower) —
    computed only on confirmed (non-last) bars to avoid lookahead.
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import talib
import yaml

PARAMS_PATH = Path(__file__).resolve().parents[2] / "config" / "params.yaml"

SESSIONS = {
    "Asia":   (0, 9),
    "London": (7, 16),
    "NY":     (12, 21),
}


def load_regime_params(path: Path = PARAMS_PATH) -> dict[str, Any]:
    with open(path) as f:
        return yaml.safe_load(f)["regime"]


def _bb_width_pct(bb_widths: np.ndarray, window: int) -> np.ndarray:
    """Rolling percentile rank of current BB width among last `window` values.
    Returns NaN for bars where fewer than 2 historical values are available."""
    n = len(bb_widths)
    result = np.full(n, float("nan"))
    for i in range(1, n):
        start = max(0, i - window)
        hist = bb_widths[start:i]          # exclude current bar (anti-lookahead)
        if len(hist) < 2:
            continue
        pct = float(np.sum(hist < bb_widths[i]) / len(hist) * 100)
        result[i] = pct
    return result


def _classify(adx: float, bb_pct: float, params: dict[str, Any]) -> str:
    if math.isnan(adx) or math.isnan(bb_pct):
        return "gray"
    if adx >= params["trend_adx_thresh"]:
        return "trend"
    if adx < params["range_adx_thresh"] and bb_pct <= params["range_bb_pct_thresh"]:
        return "range"
    return "gray"


def _session_for(hour: int) -> list[str]:
    return [name for name, (s, e) in SESSIONS.items() if s <= hour < e]


def compute_regime(
    candles: list[dict[str, Any]],
    as_of_ts: datetime,
    symbol: str,
    tf: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Pure function. Returns a dict with:
      - snapshots: list of per-bar regime dicts (for DB upsert)
      - range_duration: dict {hour: [duration_bars, ...], session: [...]}
      - breakout_prob: {hour: float, session: float}  (fraction of range
          bars immediately followed by a breakout close)
    """
    if params is None:
        params = load_regime_params()

    hist = sorted(
        (c for c in candles if c["ts_utc"] <= as_of_ts),
        key=lambda c: c["ts_utc"],
    )
    n = len(hist)
    min_bars = max(params["adx_period"], params["bb_period"]) + params["bb_width_pct_window"] + 5
    if n < min_bars:
        return {"snapshots": [], "range_duration": {}, "breakout_prob": {}}

    highs  = np.array([float(c["high"])  for c in hist])
    lows   = np.array([float(c["low"])   for c in hist])
    closes = np.array([float(c["close"]) for c in hist])

    adx_arr   = talib.ADX(highs, lows, closes, timeperiod=params["adx_period"])
    bb_upper, bb_mid, bb_lower = talib.BBANDS(
        closes,
        timeperiod=params["bb_period"],
        nbdevup=params["bb_std"],
        nbdevdn=params["bb_std"],
    )
    bb_width  = bb_upper - bb_lower
    bb_pct_arr = _bb_width_pct(bb_width, params["bb_width_pct_window"])

    regimes: list[str] = []
    snapshots: list[dict[str, Any]] = []
    for i in range(n):
        regime = _classify(float(adx_arr[i]), float(bb_pct_arr[i]), params)
        regimes.append(regime)
        snapshots.append({
            "symbol":      symbol,
            "tf_origin":   tf,
            "ts_utc":      hist[i]["ts_utc"],
            "regime":      regime,
            "adx":         None if math.isnan(float(adx_arr[i])) else float(adx_arr[i]),
            "bb_width":    None if math.isnan(float(bb_width[i])) else float(bb_width[i]),
            "bb_width_pct": None if math.isnan(float(bb_pct_arr[i])) else float(bb_pct_arr[i]),
        })

    # --- Range run analytics (SPEC.md 4.9) ---
    # Collect: range run durations and breakout events, segmented by hour and session.
    # Only use bars up to n-2 (we check i+1 for breakout) to avoid lookahead on the last bar.
    range_durations_by_hour: dict[int, list[int]] = defaultdict(list)
    range_durations_by_session: dict[str, list[int]] = defaultdict(list)
    breakout_count_by_hour: dict[int, int] = defaultdict(int)
    breakout_count_by_session: dict[str, int] = defaultdict(int)
    total_range_by_hour: dict[int, int] = defaultdict(int)
    total_range_by_session: dict[str, int] = defaultdict(int)

    i = 0
    while i < n - 1:
        if regimes[i] != "range":
            i += 1
            continue
        # Start of a range run
        run_start = i
        hour = hist[i]["ts_utc"].hour
        sessions = _session_for(hour)
        while i < n and regimes[i] == "range":
            i += 1
        run_end = i  # exclusive
        run_len = run_end - run_start

        range_durations_by_hour[hour].append(run_len)
        for sess in sessions:
            range_durations_by_session[sess].append(run_len)

        # Breakout: close of first bar AFTER the run exits the BB band
        # (only meaningful if the run doesn't reach the last bar)
        if run_end < n:
            j = run_end
            exit_close = closes[j]
            bb_u = float(bb_upper[j]) if not math.isnan(float(bb_upper[j])) else None
            bb_l = float(bb_lower[j]) if not math.isnan(float(bb_lower[j])) else None
            is_breakout = (bb_u is not None and bb_l is not None
                           and (exit_close > bb_u or exit_close < bb_l))
            for bar_i in range(run_start, run_end):
                h = hist[bar_i]["ts_utc"].hour
                sesses = _session_for(h)
                total_range_by_hour[h] += 1
                for s in sesses:
                    total_range_by_session[s] += 1
                if is_breakout:
                    breakout_count_by_hour[h] += 1
                    for s in sesses:
                        breakout_count_by_session[s] += 1

    breakout_prob_by_hour = {
        h: round(breakout_count_by_hour[h] / total_range_by_hour[h], 4)
        for h in total_range_by_hour if total_range_by_hour[h] > 0
    }
    breakout_prob_by_session = {
        s: round(breakout_count_by_session[s] / total_range_by_session[s], 4)
        for s in total_range_by_session if total_range_by_session[s] > 0
    }

    return {
        "snapshots": snapshots,
        "range_duration": {
            "by_hour": dict(range_durations_by_hour),
            "by_session": dict(range_durations_by_session),
        },
        "breakout_prob": {
            "by_hour": breakout_prob_by_hour,
            "by_session": breakout_prob_by_session,
        },
    }
