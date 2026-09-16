"""SPEC.md 4.4 (`round_numbers`) — psychological price levels and their dual effect.

Osler research: limit orders cluster at round numbers → two opposing outcomes:
  APPROACHING  → price tends to REVERSE (profit-taking walls before the number)
  ACCELERATION → price blasts through with order-book cascade (stops on the far side)

State machine per level instance:
    APPROACHING ──(close outside zone, same side)──────────────► REVERSAL
         │
         └──(close > 0.3*ATR beyond the number AND vol>median)──► ACCELERATION

compute_round_numbers() is pure — no DB access (CLAUDE.md rule 6).
Anti-lookahead: the as_of_ts cutoff is enforced at the top of the function.
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


def load_round_numbers_params(path: Path = PARAMS_PATH) -> dict[str, Any]:
    with open(path) as f:
        return yaml.safe_load(f)["round_numbers"]


def _level_weight(level_value: float, multiples: dict[int, float]) -> tuple[int, float]:
    """Return (highest_multiplier, weight) for a given price level.
    Uses the highest applicable multiplier (e.g. 4200 → 100 → weight 1.0)."""
    int_val = round(level_value)
    for mult in sorted(multiples.keys(), reverse=True):
        if int_val % mult == 0:
            return mult, multiples[mult]
    return 0, 0.0


def _candidate_levels(
    price_lo: float,
    price_hi: float,
    multiples: dict[int, float],
    margin: float = 50.0,
) -> list[dict[str, Any]]:
    """All round-number levels inside [price_lo-margin, price_hi+margin]
    with their multiplier and weight.  Only the HIGHEST applicable multiplier
    is assigned per level (4200 gets 100, not 50/25/10/5)."""
    step = min(multiples.keys())
    lo = int(math.floor((price_lo - margin) / step)) * step
    hi = int(math.ceil((price_hi + margin) / step)) * step
    out = []
    seen: set[int] = set()
    for v in range(lo, hi + step, step):
        if v in seen:
            continue
        mult, weight = _level_weight(float(v), multiples)
        if weight > 0:
            out.append({"level": float(v), "multiplier": mult, "weight": weight})
            seen.add(v)
    return out


def compute_round_numbers(
    candles: list[dict[str, Any]],
    as_of_ts: datetime,
    symbol: str,
    tf: str,
    params: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """candles: {"ts_utc","open","high","low","close","tick_volume"} dicts.
    Returns list of event dicts (one per state transition):
      APPROACHING — price entered the zone around a round number
      REVERSAL    — price backed off (closed outside zone on entry side)
      ACCELERATION— price broke through (close > 0.3*ATR beyond the number,
                    AND tick_volume above 20-bar median)
    """
    if params is None:
        params = load_round_numbers_params()

    # --- Anti-lookahead ---
    hist = sorted(
        (c for c in candles if c["ts_utc"] <= as_of_ts),
        key=lambda c: c["ts_utc"],
    )
    n = len(hist)
    atr_period = params["atr_period"]
    vol_window = params["volume_median_bars"]  # 20
    if n < atr_period + vol_window + 1:
        return []

    highs   = np.array([float(c["high"])  for c in hist])
    lows    = np.array([float(c["low"])   for c in hist])
    closes  = np.array([float(c["close"]) for c in hist])
    volumes = np.array([
        float(c["tick_volume"]) if c.get("tick_volume") is not None else 0.0
        for c in hist
    ])
    ts = [c["ts_utc"] for c in hist]

    atr = talib.ATR(highs, lows, closes, timeperiod=atr_period)

    multiples: dict[int, float] = {int(k): v for k, v in params["multiples"].items()}
    zone_atr_mult  = params["zone_atr_mult"]    # 0.3
    zone_min_width = params["zone_min_width"]   # 1.0  (dollars)
    break_atr_mult = params["break_atr_mult"]   # 0.3

    price_lo = float(lows.min())
    price_hi = float(highs.max())
    round_levels = _candidate_levels(price_lo, price_hi, multiples)

    # State per level: None | {direction, approach_ts, approach_idx}
    active: dict[float, dict[str, Any]] = {}

    hits: list[dict[str, Any]] = []

    for j in range(n):
        if math.isnan(atr[j]):
            continue

        cur_atr     = float(atr[j])
        zone_width  = max(zone_min_width, zone_atr_mult * cur_atr)
        break_dist  = break_atr_mult * cur_atr
        close       = float(closes[j])
        tv          = float(volumes[j])

        # Volume median: last `vol_window` bars up to j (inclusive)
        vol_slice   = volumes[max(0, j - vol_window + 1): j + 1]
        vol_median  = float(np.median(vol_slice)) if len(vol_slice) > 0 else 0.0

        for rn in round_levels:
            R = rn["level"]

            if R in active:
                st = active[R]
                direction   = st["direction"]
                approach_ts = st["approach_ts"]

                # ACCELERATION: close far beyond R with volume confirmation.
                # Checked BEFORE REVERSAL — a high-volume break wins even if
                # the close technically left the zone.
                if direction == "UP" and close > R + break_dist and tv > vol_median:
                    hits.append(_make_hit(symbol, tf, ts[j], rn, "ACCELERATION",
                                          direction, approach_ts))
                    del active[R]
                    continue
                if direction == "DOWN" and close < R - break_dist and tv > vol_median:
                    hits.append(_make_hit(symbol, tf, ts[j], rn, "ACCELERATION",
                                          direction, approach_ts))
                    del active[R]
                    continue

                # REVERSAL: close exits zone on the entry side (price backed off).
                if direction == "UP" and close < R - zone_width:
                    hits.append(_make_hit(symbol, tf, ts[j], rn, "REVERSAL",
                                          direction, approach_ts))
                    del active[R]
                    continue
                if direction == "DOWN" and close > R + zone_width:
                    hits.append(_make_hit(symbol, tf, ts[j], rn, "REVERSAL",
                                          direction, approach_ts))
                    del active[R]
                    continue

                # Low-volume break through zone (no ACCELERATION confirmation):
                # silently end the approach — not a clean signal.
                if direction == "UP" and close > R + zone_width:
                    del active[R]
                    continue
                if direction == "DOWN" and close < R - zone_width:
                    del active[R]
                    continue

            else:
                # Not currently in an approach — check if price is entering zone.
                if abs(close - R) <= zone_width:
                    # Need to know which side price is coming from.
                    # Look back to the first bar before this approach started.
                    prev_j = j - 1
                    while prev_j >= 0 and abs(float(closes[prev_j]) - R) <= zone_width:
                        prev_j -= 1
                    if prev_j < 0:
                        continue  # no clear pre-approach bar
                    prev_close = float(closes[prev_j])
                    if prev_close < R - zone_width:
                        direction = "UP"    # coming from below
                    elif prev_close > R + zone_width:
                        direction = "DOWN"  # coming from above
                    else:
                        continue  # ambiguous — skip

                    active[R] = {"direction": direction, "approach_ts": ts[j]}
                    hits.append(_make_hit(symbol, tf, ts[j], rn, "APPROACHING",
                                          direction, ts[j]))

    return hits


def _make_hit(
    symbol: str,
    tf: str,
    ts_utc: Any,
    rn: dict[str, Any],
    state: str,
    direction: str,
    approach_ts: Any,
) -> dict[str, Any]:
    return {
        "symbol":      symbol,
        "tf":          tf,
        "ts_utc":      ts_utc,
        "level":       rn["level"],
        "multiplier":  rn["multiplier"],
        "weight":      rn["weight"],
        "state":       state,
        "direction":   direction,
        "approach_ts": approach_ts,
    }
