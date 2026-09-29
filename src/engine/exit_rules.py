"""Level-to-level SL/TP + forced-stop reversal detection (2026-09-29 task).

CLAUDE.md rule 6: compute_level_based_sl_tp is the ONE function both the
live signal path (src/api/main.py _check_signal_sync) and the backtest
replay engine (src/backtest/replay.py) call to turn a fired
level_reversion signal into concrete SL/TP prices -- no separate ATR-
multiple reimplementation in either path (that's what this replaces).

Design (project decision, 2026-09-29 conversation):
  - SL sits beyond the level the signal fired FROM (level A) by
    sl_tp_margin_atr_mult * ATR -- symmetric with how check_level_reversion
    itself requires the confirming close to clear the zone by a margin
    before the signal fires at all.
  - TP sits just short of the next level in the direction of travel
    (level B), by the same margin -- a small deliberate haircut on profit
    for safety, not the full run to the level.
  - B must be "similar" to A: it has to pass the SAME strength
    qualification A itself passed to be tradeable (strength above the
    median of currently active/flipped levels) -- never target a weak,
    throwaway level as the profit objective.
  - Candidates are searched nearest-first; the first one that reaches
    min_rr (config, default 2.0) wins. If none do, fall back to the
    nearest qualifying B ONLY when net_votes is at least
    high_confidence_net_votes (config) AND the resulting reward:risk is
    still >= ABSOLUTE_MIN_RR (SL must never be farther from entry than
    TP -- a hard invariant, not a sweep parameter).
  - No qualifying B at all in the signal's direction -> no valid TP -> the
    caller must reject the signal (this function returns None).
"""
from __future__ import annotations

import statistics
from pathlib import Path
from typing import Any

import yaml

PARAMS_PATH = Path(__file__).resolve().parents[2] / "config" / "params.yaml"

# Invariant, not configurable: SL distance must never exceed TP distance,
# even on the high-confidence relaxed path below.
ABSOLUTE_MIN_RR = 1.0


def load_exit_rules_params(path: Path = PARAMS_PATH) -> dict[str, Any]:
    with open(path) as f:
        return yaml.safe_load(f)["exit_rules"]


def compute_level_based_sl_tp(
    direction: str,
    level_a: dict[str, Any],
    entry: float,
    atr: float,
    levels: list[dict[str, Any]],
    net_votes: float,
    params: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Returns {"stop_loss", "take_profit", "rr", "level_b_id", "relaxed"}
    or None if no acceptable level-based exit exists (caller must reject
    the signal -- there is no fallback to an ATR-multiple exit).

    `levels`: the same active/flipped levels list the caller already
    fetched for check_level_reversion -- as_of_ts filtering is the
    CALLER's responsibility (CLAUDE.md rules 1/2), same convention as
    check_level_reversion itself.
    """
    if direction not in ("BUY", "SELL"):
        raise ValueError(f"direction must be BUY or SELL, got {direction!r}")

    if params is None:
        params = load_exit_rules_params()

    margin = float(params["sl_tp_margin_atr_mult"]) * atr
    min_rr = float(params["min_rr"])
    high_conf_votes = float(params["high_confidence_net_votes"])

    active = [lvl for lvl in levels if lvl.get("status") in ("active", "flipped")]
    strengths = [float(lvl["strength"]) for lvl in active]
    if not strengths:
        return None
    strength_median = statistics.median(strengths)

    level_a_id = level_a.get("id")

    if direction == "BUY":
        sl = float(level_a["price_low"]) - margin
        candidates = [
            lvl for lvl in active
            if lvl["kind"] == "resistance" and lvl.get("id") != level_a_id
            and float(lvl["price_low"]) > entry
            and float(lvl["strength"]) > strength_median
        ]
        candidates.sort(key=lambda lvl: float(lvl["price_low"]))  # nearest first
    else:  # SELL
        sl = float(level_a["price_high"]) + margin
        candidates = [
            lvl for lvl in active
            if lvl["kind"] == "support" and lvl.get("id") != level_a_id
            and float(lvl["price_high"]) < entry
            and float(lvl["strength"]) > strength_median
        ]
        candidates.sort(key=lambda lvl: -float(lvl["price_high"]))  # nearest first

    sl_dist = abs(entry - sl)
    if sl_dist <= 0:
        return None

    best: tuple[dict, float, float] | None = None  # (level_b, tp, rr) -- nearest qualifying
    for cand in candidates:
        tp = float(cand["price_low"]) - margin if direction == "BUY" else float(cand["price_high"]) + margin
        tp_dist = abs(tp - entry)
        if tp_dist <= 0:
            continue
        rr = tp_dist / sl_dist
        if best is None:
            best = (cand, tp, rr)
        if rr >= min_rr:
            return {
                "stop_loss": sl, "take_profit": tp, "rr": rr,
                "level_b_id": cand.get("id"), "relaxed": False,
            }

    if best is not None:
        cand, tp, rr = best
        if rr >= ABSOLUTE_MIN_RR and net_votes >= high_conf_votes:
            return {
                "stop_loss": sl, "take_profit": tp, "rr": rr,
                "level_b_id": cand.get("id"), "relaxed": True,
            }

    return None


def detect_reversal_close(open_direction: str, structure: str | None) -> bool:
    """'توقف اجباری' rule (2026-09-29): the confirmed H1 structure has
    flipped against an OPEN position's own direction. Deliberately the
    SAME structure signal check_level_reversion itself already uses to
    block a NEW entry (market_structure filter) -- 'no BUY into bearish
    structure' mirrored as 'BUY already open, structure turned bearish ->
    tell the user to close it manually'. structure=None ('unknown', H1
    ingestion stale) never triggers -- same fail-safe convention as the
    entry-side filter."""
    if structure == "bearish" and open_direction == "BUY":
        return True
    if structure == "bullish" and open_direction == "SELL":
        return True
    return False
