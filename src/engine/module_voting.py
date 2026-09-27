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


def load_rules_registry_params(path: Path = PARAMS_PATH) -> dict[str, Any]:
    """D20 (2026-09-25). rules_registry.allow_proposed_in_voting."""
    with open(path) as f:
        return yaml.safe_load(f).get("rules_registry", {})


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
    """REVERSAL: agrees when a reversal from the round number matches signal direction.
    ACCELERATION: agrees when price is breaking through in the signal direction
    (momentum in the break direction, SPEC.md 4.4)."""
    r = components.get("round")
    close = components.get("close")
    if not r or close is None or r.get("level") is None:
        return 0
    state = r.get("state")
    if state == "REVERSAL":
        # Geometric proxy for approach side: which side of the round number close sits on.
        approached_from_above = close < r["level"]
        if direction == "BUY" and approached_from_above:
            return 1
        if direction == "SELL" and not approached_from_above:
            return 1
        return -1
    if state == "ACCELERATION":
        # Vote in the direction of the break (momentum confirmation).
        break_dir = r.get("direction")  # "UP" or "DOWN" from round_number_hits.direction
        if break_dir == "UP":
            return 1 if direction == "BUY" else -1
        if break_dir == "DOWN":
            return 1 if direction == "SELL" else -1
        return 0
    return 0


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
    """Legacy stub -- kept for backward compat but always returns 0.
    The real implementation is _vote_dollar_correlation_direction below."""
    return 0


# VOTE_FUNCTIONS: unconditional voters. Only add functions that compute a
# real, directional vote from real data. dollar_correlation is intentionally
# absent: its implementation is a stub (always returns 0 -- see its docstring
# for the missing DXY direction piece). A zero-always voter in this dict
# pollutes the votes dict without contributing; it stays in the codebase as
# a named placeholder until the real implementation is ready, at which point
# it belongs here (unconditional) or in PROPOSED_VOTE_FUNCTIONS (gated).
VOTE_FUNCTIONS: dict[str, Callable[[dict, str], int]] = {
    "structure": _vote_structure,
    "round_numbers": _vote_round_numbers,
    "fibonacci": _vote_fibonacci,
    "gaps": _vote_gaps,
    "patterns": _vote_patterns,
}


# ---------------------------------------------------------------------------
# D20 (2026-09-25) -- proposed-rule voters. Kept in a SEPARATE dict from
# VOTE_FUNCTIONS (not merged in) because these are gated by rules-registry
# status + the rules_registry.allow_proposed_in_voting flag, unlike the six
# above which have always voted unconditionally. Each key here is the
# EXACT id that rule has in the `rules` table (see rsi_store.py's
# register_proposed_rules() / matrix_store.py's register_proposed_rule()) --
# compute_votes() below looks status up by this same key.
# ---------------------------------------------------------------------------

def _vote_matrix(components: dict, direction: str) -> int:
    """SPEC.md 4.10. Agrees when the matrix's own pivot direction (M5,
    D2) matches the signal direction. Deliberately NOT scaled by score
    (0-6) -- like every other voter here, the vote is a simple -1/0/+1 and
    confidence is expressed only through params.yaml's external weight,
    not the module's own internal magnitude (same convention as gaps'
    weight/round's multiplier, which also aren't read into the vote)."""
    m = components.get("matrix")
    if not m or m.get("direction") == "neutral":
        return 0
    matrix_dir = "BUY" if m["direction"] == "buy" else "SELL"
    return 1 if matrix_dir == direction else -1


def _vote_rsi_overbought_oversold(components: dict, direction: str) -> int:
    """SPEC.md 4.19-a. Reversal semantics: oversold -> expect a bounce up
    (agrees with BUY); overbought -> expect a drop (agrees with SELL)."""
    snap = (components.get("rsi") or {}).get("snapshot")
    if not snap:
        return 0
    state = snap.get("rsi_state")
    if state == "oversold":
        return 1 if direction == "BUY" else -1
    if state == "overbought":
        return 1 if direction == "SELL" else -1
    return 0


def _vote_rsi_price_divergence(components: dict, direction: str) -> int:
    """SPEC.md 4.19-b. Bullish divergence -> expect a reversal up (agrees
    with BUY); bearish -> agrees with SELL. Only counts within
    divergence_recency_bars of the signal bar (build_context already
    applied that window when populating this key)."""
    d = (components.get("rsi") or {}).get("recent_price_rsi_divergence")
    if not d:
        return 0
    if d["direction"] == "bullish":
        return 1 if direction == "BUY" else -1
    if d["direction"] == "bearish":
        return 1 if direction == "SELL" else -1
    return 0


def _vote_macd_price_divergence(components: dict, direction: str) -> int:
    """SPEC.md 4.19-c. Same reversal semantics as the RSI divergence vote,
    against the MACD-line divergence instead."""
    d = (components.get("rsi") or {}).get("recent_price_macd_divergence")
    if not d:
        return 0
    if d["direction"] == "bullish":
        return 1 if direction == "BUY" else -1
    if d["direction"] == "bearish":
        return 1 if direction == "SELL" else -1
    return 0



def _vote_regime(components: dict, direction: str) -> int:
    """Regime quality filter for level_reversion (reversal) signals.
    Range regime = mean-reversion market -> agrees with all reversal signals.
    Trend regime = continuation market -> disagrees with reversals.
    Gray = uncertain -> 0. Direction-agnostic: regime labels the market TYPE,
    not the direction; _vote_structure already handles the directional half."""
    regime = components.get("regime")
    if regime == "range":
        return 1
    if regime == "trend":
        return -1
    return 0


def _vote_memory(components: dict, direction: str) -> int:
    """SPEC.md 4.10 (memory module). up_ratio = fraction of historical
    pattern-match analogues whose H-bar forward return was positive.
    > 0.6 -> analogue history is mostly bullish -> agrees with BUY.
    < 0.4 -> analogue history is mostly bearish -> agrees with SELL.
    Middle band (0.4–0.6) or n_matches < 3 -> 0 (insufficient signal)."""
    mem = components.get("memory") or {}
    up_ratio = mem.get("up_ratio")
    n_matches = mem.get("n_matches", 0)
    if up_ratio is None or n_matches < 3:
        return 0
    if direction == "BUY":
        return 1 if up_ratio > 0.6 else (-1 if up_ratio < 0.4 else 0)
    # SELL
    return 1 if up_ratio < 0.4 else (-1 if up_ratio > 0.6 else 0)


def _vote_dollar_correlation_direction(components: dict, direction: str) -> int:
    """SPEC.md 4.8-a: inverse dollar-gold correlation.
    Requires |dollar_corr| >= dollar_corr_min_abs AND dxy_direction present.
    DXY up -> gold expected to fall -> agrees with SELL (and vice versa) when
    correlation is negative (the typical case). Flips when correlation is
    positive (unusual but possible in risk-off flight-to-safety regimes).
    Reads thresholds from params.yaml correlation section at call time.
    """
    dollar_corr = components.get("dollar_corr")
    dxy_direction = components.get("dxy_direction")
    if dollar_corr is None or dxy_direction is None or dxy_direction == "flat":
        return 0
    try:
        from pathlib import Path as _P
        import yaml as _y
        _cfg = _y.safe_load(open(_P(__file__).resolve().parents[2] / "config" / "params.yaml"))["correlation"]
        min_abs = float(_cfg.get("dollar_corr_min_abs", 0.3))
    except Exception:
        min_abs = 0.3
    if abs(dollar_corr) < min_abs:
        return 0
    # Implied gold direction: sign(dollar_corr) flips the DXY move
    # negative corr (normal): DXY up -> gold down; DXY down -> gold up
    # positive corr (unusual): DXY up -> gold up; DXY down -> gold down
    corr_sign = 1 if dollar_corr >= 0 else -1
    dxy_sign  = 1 if dxy_direction == "up" else -1
    implied_gold_sign = corr_sign * dxy_sign  # +1 = gold up expected, -1 = gold down
    implied_dir = "BUY" if implied_gold_sign > 0 else "SELL"
    return 1 if implied_dir == direction else -1


PROPOSED_VOTE_FUNCTIONS: dict[str, Callable[[dict, str], int]] = {
    "matrix_score_mtf_agreement": _vote_matrix,
    "rsi_overbought_oversold": _vote_rsi_overbought_oversold,
    "rsi_price_divergence": _vote_rsi_price_divergence,
    "macd_price_divergence": _vote_macd_price_divergence,
    "regime_quality": _vote_regime,
    "memory_pattern_bias": _vote_memory,
    "dollar_correlation_direction": _vote_dollar_correlation_direction,
}

# Programmatic guard (D20-ext, 2026-09-25): each proposed voter must
# explicitly declare its implementation is complete before it can reach
# compute_votes(). A missing entry or False blocks the voter at runtime --
# no manual audit needed to catch accidentally-wired stubs. When adding
# a new voter, set False until the function body computes real data.
# dollar_correlation is deliberately absent here (and from VOTE_FUNCTIONS)
# because it is not yet a real voter.
VOTER_IMPLEMENTATION_COMPLETE: dict[str, bool] = {
    "matrix_score_mtf_agreement": True,   # real 10-indicator x 7-TF score
    "rsi_overbought_oversold":    True,   # RSI(14) state from rsi_snapshots
    "rsi_price_divergence":       True,   # fractal-pivot divergence from divergence_events
    "macd_price_divergence":      True,   # MACD-line divergence from divergence_events
    "regime_quality":             True,   # ADX/BB regime label from regime_snapshots
    "memory_pattern_bias":        True,   # stumpy pattern-match up_ratio from memory table
    "dollar_correlation_direction": True,  # DXY M5 candles + dollar_corr coefficient
}


def compute_votes(
    components: dict[str, Any],
    direction: str,
    params: dict[str, Any] | None = None,
    rule_status: dict[str, str] | None = None,
    allow_proposed: bool = False,
) -> dict[str, Any]:
    """Pure. Returns {"votes": {module: -1|0|1, ...}, "net_votes": float,
    "proposed_observations": [rule_id, ...]}. Weights default to 1.0 each;
    override per-module via params.yaml module_voting.weights.

    rule_status/allow_proposed (D20, 2026-09-25): PROPOSED_VOTE_FUNCTIONS
    only cast a real vote when their rules-table status is
    'testing'/'verified', OR status is 'proposed' AND allow_proposed is
    True (params.yaml rules_registry.allow_proposed_in_voting). A
    proposed-status module missing from rule_status, or with
    allow_proposed False, votes 0 (fails safe -- same as CLAUDE.md's
    default "no proposed rule reaches the signal engine"). Every
    proposed-status id that DID cast a real vote this call is listed in
    proposed_observations so the caller can mark components accordingly --
    this function never touches the rules table itself (status changes
    are a human/backtest decision, not this function's).
    """
    if params is None:
        params = load_voting_params()
    weights = params.get("weights", {})
    rule_status = rule_status or {}

    votes: dict[str, int] = {}
    net = 0.0
    proposed_observations: list[str] = []

    for name, fn in VOTE_FUNCTIONS.items():
        v = fn(components, direction)
        votes[name] = v
        net += v * float(weights.get(name, 1.0))

    for rule_id, fn in PROPOSED_VOTE_FUNCTIONS.items():
        # Guard: reject stubs before any status/flag check.
        if not VOTER_IMPLEMENTATION_COMPLETE.get(rule_id, False):
            votes[rule_id] = 0
            continue

        status = rule_status.get(rule_id)
        if status in ("testing", "verified"):
            participates = True
        elif status == "proposed" and allow_proposed:
            participates = True
            proposed_observations.append(rule_id)
        else:
            participates = False  # unknown status, rejected, or proposed-but-flag-off

        if not participates:
            votes[rule_id] = 0
            continue
        v = fn(components, direction)
        votes[rule_id] = v
        net += v * float(weights.get(rule_id, 1.0))

    return {"votes": votes, "net_votes": net, "proposed_observations": proposed_observations}
