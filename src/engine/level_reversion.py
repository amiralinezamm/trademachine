"""First entry in `rules` (status=testing, unverified): price approaching a
strong active level -> reversal signal. CLAUDE.md rule 6: this exact
function is what both backtest and live/API call — no separate copy.

Pure — no DB access. Caller passes the current bar and the set of levels
already known as of that bar (CLAUDE.md rules 1/2: caller is responsible for
only passing levels/candles that were actually knowable at that ts, e.g.
from compute_levels(..., as_of_ts=<= current bar's ts)).
"""
from __future__ import annotations

from pathlib import Path
from statistics import median
from typing import Any

import yaml

PARAMS_PATH = Path(__file__).resolve().parents[2] / "config" / "params.yaml"
RULE_ID = "level_reversion_v1"


def load_rule_params(path: Path = PARAMS_PATH) -> dict[str, Any]:
    with open(path) as f:
        return yaml.safe_load(f)["signal_rules"]["level_reversion"]


def check_level_reversion(
    symbol: str,
    tf: str,
    ts_utc,
    high: float,
    low: float,
    close: float,
    atr: float,
    levels: list[dict[str, Any]],
    params: dict[str, Any] | None = None,
    structure: str | None = None,
) -> dict[str, Any] | None:
    """levels: rows from the `levels` table (dicts with at least kind,
    price_low, price_high, strength, status). Returns a signal dict ready
    for the `signals` table, or None if no rule condition is met.

    Signal fires ONLY when two conditions hold on the SAME bar (edge-trigger):
      1. Wick-entered zone: low <= price_high AND high >= price_low
      2. Directional close with meaningful margin (confirm_atr_mult * ATR):
           support  (BUY):  close > price_high + confirm_atr_mult * ATR
           resistance (SELL): close < price_low  - confirm_atr_mult * ATR
    The margin reuses levels.break_atr_mult semantics: same numeric value (0.5),
    same idea of "close must clear the zone by a non-trivial distance".
    This prevents repeat-fire and rules out tangential closes at the zone edge.

    structure: "bullish"/"bearish"/"unknown"/None, from
    market_structure.compute_market_structure() (or structure_from_swings()
    in replay). None behaves like "unknown" -- no filtering (rule
    market_structure_filter, roadmap-rev2 4.4). BUY is blocked when
    structure=="bearish"; SELL is blocked when structure=="bullish".
    """
    if params is None:
        params = load_rule_params()

    # 'flipped' is still a live state per SPEC.md 4.2 (only 'expired' is
    # dead) — restricting to 'active' would drop the most-contested zones,
    # which are exactly the ones that have already flipped at least once.
    active = [lvl for lvl in levels if lvl["status"] in ("active", "flipped")]
    if not active:
        return None

    strengths = [float(lvl["strength"]) for lvl in active]
    strength_median = median(strengths)

    candidates = []
    for lvl in active:
        if float(lvl["strength"]) <= strength_median:
            continue
        lo, hi = float(lvl["price_low"]), float(lvl["price_high"])

        # Off hours (17-23 UTC) + resistance: EV-negative sub-segment
        # (WR=40.1%, N=1,684, EV=-$1.18/sig from replay 2026-09-20).
        _hour = getattr(ts_utc, "hour", None)
        if _hour is not None and lvl["kind"] == "resistance" and 17 <= _hour <= 23:
            continue

        # Condition 1: wick entered the zone (touch event)
        if not (low <= hi and high >= lo):
            continue

        # Condition 2: close confirms directional reversal with meaningful margin.
        # Must be at least confirm_atr_mult * ATR beyond the zone edge, not just
        # a pixel-level cross (mirrors the break_atr_mult threshold in levels.py).
        margin = params.get("confirm_atr_mult", 0.5) * atr
        if lvl["kind"] == "support":
            if not (close > hi + margin):   # rebounded meaningfully above zone
                continue
            # market_structure_filter (roadmap-rev2 4.4): don't BUY into a
            # confirmed bearish H1 structure.
            if structure == "bearish":
                continue
        else:                               # fell back meaningfully below zone
            if not (close < lo - margin):
                continue
            # market_structure_filter: don't SELL into a confirmed bullish
            # H1 structure.
            if structure == "bullish":
                continue

        mid = (lo + hi) / 2
        dist = abs(close - mid)
        candidates.append((dist, lvl))

    if not candidates:
        return None

    # Nearest qualifying level wins if more than one is in range.
    dist, lvl = min(candidates, key=lambda c: c[0])
    lo, hi = float(lvl["price_low"]), float(lvl["price_high"])
    direction = "BUY" if lvl["kind"] == "support" else "SELL"

    return {
        "ts_utc": ts_utc,
        "direction": direction,
        "entry": close,
        "stop_loss": None,
        "take_profit": None,
        "confidence": None,
        "rule_version": RULE_ID,
        "components": {
            "symbol": symbol,
            "tf": tf,
            "level_id": lvl.get("id"),
            "level_kind": lvl["kind"],
            "level_price_low": lo,
            "level_price_high": hi,
            "level_strength": float(lvl["strength"]),
            "strength_median_at_signal": strength_median,
            "distance": dist,
            "atr": atr,
            "close": close,
        },
    }


def apply_spacing_filter(
    signal: "dict[str, Any] | None",
    entry_price: float,
    prev_same_dir: "dict[str, Any] | None",
    min_spacing_usd: float,
) -> "dict[str, Any] | None":
    """Same-direction spacing filter (Rule: same_direction_spacing_filter).

    Rejects `signal` (returns None) when ALL of these hold:
      - prev_same_dir is not None (a prior same-direction signal exists)
      - prev_same_dir['outcome'] is None (the prior position is still open)
      - |entry_price - prev_same_dir['entry']| < min_spacing_usd

    Interpretation used: any non-None outcome (tp, sl, level_invalidated,
    timeout) counts as "closed" — restriction is lifted.  If the prior signal
    carries a non-None outcome for any of these reasons, the new signal is
    allowed without a spacing check.
    """
    if signal is None:
        return None
    if prev_same_dir is None:
        return signal
    if prev_same_dir.get("outcome") is not None:
        return signal          # prior position is closed — no restriction
    if abs(entry_price - float(prev_same_dir["entry"])) < min_spacing_usd:
        return None            # rejected: too close to an open same-dir signal
    return signal
