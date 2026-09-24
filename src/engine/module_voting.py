"""Module voting framework v1 (roadmap-rev2, interim ahead of the fusion
layer in phase 4). Each module casts +1 (agrees with the candidate
direction), -1 (disagrees), or 0 (neutral / no opinion) -- purely off the
SAME `components` dict already built for signals.components (CLAUDE.md
rule 6: no new data source, no extra lookahead risk since components is
itself as_of_ts-filtered upstream).

To add a new module's vote: write one function (components, direction) ->
int and add it to VOTE_FUNCTIONS. Nothing else here needs to change.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import yaml

PARAMS_PATH = Path(__file__).resolve().parents[2] / "config" / "params.yaml"


def load_voting_params(path: Path = PARAMS_PATH) -> dict[str, Any]:
    with open(path) as f:
        return yaml.safe_load(f)["module_voting"]


def _vote_structure(components: dict, direction: str) -> int:
    """regime+structure vote: agrees if direction matches H1 market
    structure (see market_structure.py). 'regime' itself (gray/trend/range
    volatility label) carries no directional sign, so this vote is driven
    by market_structure -- the directional half of the pair."""
    structure = components.get("market_structure")
    if structure == "bullish":
        return 1 if direction == "BUY" else -1
    if structure == "bearish":
        return -1 if direction == "BUY" else 1
    return 0


def _vote_round_numbers(components: dict, direction: str) -> int:
    """Agrees on a REVERSAL that matches the signal direction.
    NOTE: build_context() (signal_context.py) currently filters round hits
    to state in (APPROACHING, REVERSAL) only -- ACCELERATION events never
    reach components, so the 'disagree on opposing ACCELERATION' half of
    the spec can't be evaluated yet. Flagged in the rule doc; needs
    build_context extended to also pass ACCELERATION through."""
    r = components.get("round")
    close = components.get("close")
    if not r or close is None or r.get("level") is None:
        return 0
    if r.get("state") != "REVERSAL":
        return 0
    # Geometric proxy for approach side (direction field itself isn't
    # surfaced in components): which side of the round number close sits on.
    approached_from_above = close < r["level"]
    if direction == "BUY" and approached_from_above:
        return 1
    if direction == "SELL" and not approached_from_above:
        return 1
    return -1


def _vote_fibonacci(components: dict, direction: str) -> int:
    """Agrees when the nearest fib zone overlaps a level (SPEC 4.6
    confluence) -- no opposition case specified."""
    f = components.get("fib")
    if not f:
        return 0
    return 1 if f.get("overlapping") else 0


def _vote_gaps(components: dict, direction: str) -> int:
    """Agrees when an open/half-filled gap in the signal's direction is
    within 5 ATR (the backtest-confirmed gap_near_filter finding)."""
    g = components.get("gap")
    if not g or g.get("distance_atr") is None or g["distance_atr"] >= 5:
        return 0
    gap_dir = g.get("direction")
    if direction == "BUY":
        return 1 if gap_dir == "UP" else -1 if gap_dir == "DOWN" else 0
    return 1 if gap_dir == "DOWN" else -1 if gap_dir == "UP" else 0


def _vote_patterns(components: dict, direction: str) -> int:
    """Agrees if a same-bar candlestick pattern points the same way,
    disagrees if it points the opposite way. Mixed bullish+bearish on the
    same bar -> neutral (no clear signal)."""
    p = components.get("pattern")
    if not p:
        return 0
    bullish, bearish = bool(p.get("bullish")), bool(p.get("bearish"))
    if bullish == bearish:
        return 0
    pattern_dir = "BUY" if bullish else "SELL"
    return 1 if pattern_dir == direction else -1


def _vote_dollar_correlation(components: dict, direction: str) -> int:
    """Not yet actionable -- always 0.

    components only carries the correlation COEFFICIENT (dollar_corr), not
    the dollar index's own recent price direction. A directional vote needs
    both: sign(dollar_corr) x dollar_direction -> implied gold direction,
    compared to the signal's direction. Proposal: extend build_context to
    also store a short-window dollar-index direction (e.g. up/down over the
    last N bars, mirroring the already-explored oil_shock_divergence idea
    in the rules table) before this can cast a real vote."""
    return 0


VOTE_FUNCTIONS: dict[str, Callable[[dict, str], int]] = {
    "structure": _vote_structure,
    "round_numbers": _vote_round_numbers,
    "fibonacci": _vote_fibonacci,
    "gaps": _vote_gaps,
    "patterns": _vote_patterns,
    "dollar_correlation": _vote_dollar_correlation,
}


def compute_votes(
    components: dict[str, Any],
    direction: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Pure. Returns {"votes": {module: -1|0|1, ...}, "net_votes": float}.
    Weights default to 1.0 each; override per-module via
    params.yaml module_voting.weights."""
    if params is None:
        params = load_voting_params()
    weights = params.get("weights", {})

    votes: dict[str, int] = {}
    net = 0.0
    for name, fn in VOTE_FUNCTIONS.items():
        v = fn(components, direction)
        votes[name] = v
        net += v * float(weights.get(name, 1.0))

    return {"votes": votes, "net_votes": net}
