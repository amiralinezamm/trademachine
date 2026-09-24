"""Tests for src.engine.module_voting (Part 3, module_voting_v1)."""
from __future__ import annotations

from src.engine.module_voting import compute_votes

PARAMS = {"min_net_votes": 2, "weights": {
    "structure": 1.0, "round_numbers": 1.0, "fibonacci": 1.0,
    "gaps": 1.0, "patterns": 1.0, "dollar_correlation": 1.0,
}}


def test_structure_agrees_with_bullish_buy():
    c = {"market_structure": "bullish"}
    r = compute_votes(c, "BUY", PARAMS)
    assert r["votes"]["structure"] == 1


def test_structure_disagrees_with_bearish_buy():
    c = {"market_structure": "bearish"}
    r = compute_votes(c, "BUY", PARAMS)
    assert r["votes"]["structure"] == -1


def test_structure_neutral_when_unknown():
    c = {"market_structure": "unknown"}
    r = compute_votes(c, "BUY", PARAMS)
    assert r["votes"]["structure"] == 0


def test_round_numbers_reversal_agrees_when_approached_from_above_buy():
    # close below round.level -> approached from above -> favors BUY (bounce up)
    c = {"round": {"level": 2000.0, "state": "REVERSAL"}, "close": 1998.0}
    r = compute_votes(c, "BUY", PARAMS)
    assert r["votes"]["round_numbers"] == 1


def test_round_numbers_reversal_disagrees_when_wrong_side():
    c = {"round": {"level": 2000.0, "state": "REVERSAL"}, "close": 1998.0}
    r = compute_votes(c, "SELL", PARAMS)
    assert r["votes"]["round_numbers"] == -1


def test_round_numbers_approaching_no_vote():
    c = {"round": {"level": 2000.0, "state": "APPROACHING"}, "close": 1999.0}
    r = compute_votes(c, "BUY", PARAMS)
    assert r["votes"]["round_numbers"] == 0


def test_fib_overlapping_agrees():
    c = {"fib": {"overlapping": True}}
    r = compute_votes(c, "BUY", PARAMS)
    assert r["votes"]["fibonacci"] == 1


def test_fib_not_overlapping_neutral():
    c = {"fib": {"overlapping": False}}
    r = compute_votes(c, "BUY", PARAMS)
    assert r["votes"]["fibonacci"] == 0


def test_gap_within_5atr_same_direction_agrees():
    c = {"gap": {"direction": "UP", "distance_atr": 3.0}}
    r = compute_votes(c, "BUY", PARAMS)
    assert r["votes"]["gaps"] == 1


def test_gap_within_5atr_opposite_direction_disagrees():
    c = {"gap": {"direction": "DOWN", "distance_atr": 3.0}}
    r = compute_votes(c, "BUY", PARAMS)
    assert r["votes"]["gaps"] == -1


def test_gap_beyond_5atr_neutral():
    c = {"gap": {"direction": "UP", "distance_atr": 7.0}}
    r = compute_votes(c, "BUY", PARAMS)
    assert r["votes"]["gaps"] == 0


def test_pattern_bullish_only_agrees_with_buy():
    c = {"pattern": {"bullish": ["CDLHAMMER"], "bearish": []}}
    r = compute_votes(c, "BUY", PARAMS)
    assert r["votes"]["patterns"] == 1


def test_pattern_bearish_only_disagrees_with_buy():
    c = {"pattern": {"bullish": [], "bearish": ["CDLHARAMI"]}}
    r = compute_votes(c, "BUY", PARAMS)
    assert r["votes"]["patterns"] == -1


def test_pattern_mixed_neutral():
    c = {"pattern": {"bullish": ["CDLHAMMER"], "bearish": ["CDLHARAMI"]}}
    r = compute_votes(c, "BUY", PARAMS)
    assert r["votes"]["patterns"] == 0


def test_dollar_correlation_always_zero():
    c = {"dollar_corr": -0.8}
    r = compute_votes(c, "BUY", PARAMS)
    assert r["votes"]["dollar_correlation"] == 0


def test_net_votes_sums_weighted():
    c = {
        "market_structure": "bullish",       # +1
        "fib": {"overlapping": True},        # +1
        "gap": {"direction": "UP", "distance_atr": 2.0},  # +1
        "pattern": {"bullish": [], "bearish": []},  # 0
    }
    r = compute_votes(c, "BUY", PARAMS)
    assert r["net_votes"] == 3.0


def test_custom_weights_applied():
    params = {**PARAMS, "weights": {**PARAMS["weights"], "structure": 2.5}}
    c = {"market_structure": "bullish"}
    r = compute_votes(c, "BUY", params)
    assert r["net_votes"] == 2.5


def test_missing_module_data_all_neutral():
    r = compute_votes({}, "BUY", PARAMS)
    assert r["net_votes"] == 0
    assert all(v == 0 for v in r["votes"].values())
