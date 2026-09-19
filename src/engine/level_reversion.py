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
) -> dict[str, Any] | None:
    """levels: rows from the `levels` table (dicts with at least kind,
    price_low, price_high, strength, status). Returns a signal dict ready
    for the `signals` table, or None if no rule condition is met.

    Signal fires ONLY when two conditions hold on the SAME bar (edge-trigger):
      1. Wick-entered zone: low <= price_high AND high >= price_low
      2. Directional close confirms reversal (not a break-through):
           support  (BUY):  close > price_high  (rebounded above zone)
           resistance (SELL): close < price_low  (fell back below zone)
    This prevents repeat-fire across consecutive bars where price hovers
    near a zone without actually touching and rebounding.
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

        # Condition 1: wick entered the zone (touch event)
        if not (low <= hi and high >= lo):
            continue

        # Condition 2: close confirms directional reversal
        if lvl["kind"] == "support":
            if not (close > hi):   # support: close must be back above zone
                continue
        else:                       # resistance: close must be back below zone
            if not (close < lo):
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
        },
    }
