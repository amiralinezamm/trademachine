"""Tests for the D20 (2026-09-25) proposed-rule voters in module_voting.py:
matrix, rsi_overbought_oversold, rsi_price_divergence, macd_price_divergence.
"""
from __future__ import annotations

from src.engine.module_voting import compute_votes, PROPOSED_VOTE_FUNCTIONS

PARAMS = {
    "min_net_votes": 2,
    "weights": {
        "structure": 1.0, "round_numbers": 1.0, "fibonacci": 1.0,
        "gaps": 1.0, "patterns": 1.0,
        "matrix_score_mtf_agreement": 1.0, "rsi_overbought_oversold": 1.0,
        "rsi_price_divergence": 1.0, "macd_price_divergence": 1.0,
    },
}

ALL_PROPOSED = {rid: "proposed" for rid in PROPOSED_VOTE_FUNCTIONS}
ALL_VERIFIED = {rid: "verified" for rid in PROPOSED_VOTE_FUNCTIONS}


# ---------------------------------------------------------------------------
# Gating: proposed status + allow_proposed flag
# ---------------------------------------------------------------------------

def test_proposed_rule_votes_zero_when_flag_off():
    c = {"matrix": {"score": 6, "direction": "buy"}}
    r = compute_votes(c, "BUY", PARAMS, rule_status=ALL_PROPOSED, allow_proposed=False)
    assert r["votes"]["matrix_score_mtf_agreement"] == 0
    assert r["proposed_observations"] == []


def test_proposed_rule_votes_when_flag_on():
    c = {"matrix": {"score": 6, "direction": "buy"}}
    r = compute_votes(c, "BUY", PARAMS, rule_status=ALL_PROPOSED, allow_proposed=True)
    assert r["votes"]["matrix_score_mtf_agreement"] == 1
    assert "matrix_score_mtf_agreement" in r["proposed_observations"]


def test_missing_rule_status_fails_safe_to_zero():
    """A proposed voter absent from rule_status (e.g. DB row deleted) must
    not vote, even with the flag on -- fail-safe default."""
    c = {"matrix": {"score": 6, "direction": "buy"}}
    r = compute_votes(c, "BUY", PARAMS, rule_status={}, allow_proposed=True)
    assert r["votes"]["matrix_score_mtf_agreement"] == 0
    assert r["proposed_observations"] == []


def test_verified_rule_always_votes_regardless_of_flag():
    c = {"matrix": {"score": 6, "direction": "buy"}}
    r = compute_votes(c, "BUY", PARAMS, rule_status=ALL_VERIFIED, allow_proposed=False)
    assert r["votes"]["matrix_score_mtf_agreement"] == 1
    # No longer 'proposed' -- must not appear in proposed_observations.
    assert "matrix_score_mtf_agreement" not in r["proposed_observations"]


def test_no_rule_status_argument_defaults_to_no_proposed_votes():
    """Backward-compat default: omitting rule_status/allow_proposed entirely
    (old call signature) must behave exactly like CLAUDE.md's baseline --
    no proposed rule reaches the vote."""
    c = {"matrix": {"score": 6, "direction": "buy"}}
    r = compute_votes(c, "BUY", PARAMS)
    assert r["votes"]["matrix_score_mtf_agreement"] == 0
    assert r["proposed_observations"] == []


def test_only_participating_rules_listed_in_proposed_observations():
    c = {
        "matrix": {"score": 6, "direction": "buy"},
        "rsi": {"snapshot": {"rsi_state": "oversold"}, "recent_price_rsi_divergence": None,
                "recent_price_macd_divergence": None},
    }
    status = {"matrix_score_mtf_agreement": "proposed", "rsi_overbought_oversold": "proposed",
              "rsi_price_divergence": "rejected", "macd_price_divergence": "testing"}
    r = compute_votes(c, "BUY", PARAMS, rule_status=status, allow_proposed=True)
    assert set(r["proposed_observations"]) == {"matrix_score_mtf_agreement", "rsi_overbought_oversold"}
    assert r["votes"]["rsi_price_divergence"] == 0  # rejected -- never votes
    assert r["votes"]["macd_price_divergence"] == 0  # testing but no macd divergence data -> 0 anyway


# ---------------------------------------------------------------------------
# _vote_matrix
# ---------------------------------------------------------------------------

def test_vote_matrix_agrees():
    c = {"matrix": {"score": 4, "direction": "buy"}}
    r = compute_votes(c, "BUY", PARAMS, rule_status=ALL_PROPOSED, allow_proposed=True)
    assert r["votes"]["matrix_score_mtf_agreement"] == 1


def test_vote_matrix_disagrees():
    c = {"matrix": {"score": 4, "direction": "sell"}}
    r = compute_votes(c, "BUY", PARAMS, rule_status=ALL_PROPOSED, allow_proposed=True)
    assert r["votes"]["matrix_score_mtf_agreement"] == -1


def test_vote_matrix_neutral_direction_is_zero():
    c = {"matrix": {"score": 0, "direction": "neutral"}}
    r = compute_votes(c, "BUY", PARAMS, rule_status=ALL_PROPOSED, allow_proposed=True)
    assert r["votes"]["matrix_score_mtf_agreement"] == 0


def test_vote_matrix_missing_data_is_zero():
    r = compute_votes({}, "BUY", PARAMS, rule_status=ALL_PROPOSED, allow_proposed=True)
    assert r["votes"]["matrix_score_mtf_agreement"] == 0


# ---------------------------------------------------------------------------
# _vote_rsi_overbought_oversold
# ---------------------------------------------------------------------------

def test_vote_rsi_oversold_agrees_with_buy():
    c = {"rsi": {"snapshot": {"rsi_state": "oversold"}}}
    r = compute_votes(c, "BUY", PARAMS, rule_status=ALL_PROPOSED, allow_proposed=True)
    assert r["votes"]["rsi_overbought_oversold"] == 1


def test_vote_rsi_overbought_agrees_with_sell():
    c = {"rsi": {"snapshot": {"rsi_state": "overbought"}}}
    r = compute_votes(c, "SELL", PARAMS, rule_status=ALL_PROPOSED, allow_proposed=True)
    assert r["votes"]["rsi_overbought_oversold"] == 1


def test_vote_rsi_overbought_disagrees_with_buy():
    c = {"rsi": {"snapshot": {"rsi_state": "overbought"}}}
    r = compute_votes(c, "BUY", PARAMS, rule_status=ALL_PROPOSED, allow_proposed=True)
    assert r["votes"]["rsi_overbought_oversold"] == -1


def test_vote_rsi_neutral_state_is_zero():
    c = {"rsi": {"snapshot": {"rsi_state": "neutral"}}}
    r = compute_votes(c, "BUY", PARAMS, rule_status=ALL_PROPOSED, allow_proposed=True)
    assert r["votes"]["rsi_overbought_oversold"] == 0


# ---------------------------------------------------------------------------
# _vote_rsi_price_divergence / _vote_macd_price_divergence
# ---------------------------------------------------------------------------

def test_vote_rsi_divergence_bullish_agrees_with_buy():
    c = {"rsi": {"recent_price_rsi_divergence": {"direction": "bullish"}}}
    r = compute_votes(c, "BUY", PARAMS, rule_status=ALL_PROPOSED, allow_proposed=True)
    assert r["votes"]["rsi_price_divergence"] == 1


def test_vote_rsi_divergence_bearish_disagrees_with_buy():
    c = {"rsi": {"recent_price_rsi_divergence": {"direction": "bearish"}}}
    r = compute_votes(c, "BUY", PARAMS, rule_status=ALL_PROPOSED, allow_proposed=True)
    assert r["votes"]["rsi_price_divergence"] == -1


def test_vote_rsi_divergence_absent_is_zero():
    c = {"rsi": {"recent_price_rsi_divergence": None}}
    r = compute_votes(c, "BUY", PARAMS, rule_status=ALL_PROPOSED, allow_proposed=True)
    assert r["votes"]["rsi_price_divergence"] == 0


def test_vote_macd_divergence_bullish_agrees_with_buy():
    c = {"rsi": {"recent_price_macd_divergence": {"direction": "bullish"}}}
    r = compute_votes(c, "BUY", PARAMS, rule_status=ALL_PROPOSED, allow_proposed=True)
    assert r["votes"]["macd_price_divergence"] == 1


def test_vote_macd_divergence_bearish_disagrees_with_buy():
    c = {"rsi": {"recent_price_macd_divergence": {"direction": "bearish"}}}
    r = compute_votes(c, "BUY", PARAMS, rule_status=ALL_PROPOSED, allow_proposed=True)
    assert r["votes"]["macd_price_divergence"] == -1


def test_rsi_and_macd_divergence_votes_are_independent():
    """A bullish RSI divergence and a bearish MACD divergence at the same
    time must vote independently, not cancel or overwrite each other."""
    c = {"rsi": {
        "recent_price_rsi_divergence": {"direction": "bullish"},
        "recent_price_macd_divergence": {"direction": "bearish"},
    }}
    r = compute_votes(c, "BUY", PARAMS, rule_status=ALL_PROPOSED, allow_proposed=True)
    assert r["votes"]["rsi_price_divergence"] == 1
    assert r["votes"]["macd_price_divergence"] == -1


# ---------------------------------------------------------------------------
# net_votes with proposed voters included
# ---------------------------------------------------------------------------

def test_net_votes_includes_proposed_when_flag_on():
    c = {
        "matrix": {"score": 5, "direction": "buy"},
        "rsi": {"snapshot": {"rsi_state": "oversold"},
                "recent_price_rsi_divergence": {"direction": "bullish"},
                "recent_price_macd_divergence": None},
    }
    r = compute_votes(c, "BUY", PARAMS, rule_status=ALL_PROPOSED, allow_proposed=True)
    # matrix +1, rsi_overbought_oversold +1, rsi_price_divergence +1, macd 0
    assert r["net_votes"] == 3.0


def test_net_votes_excludes_proposed_when_flag_off():
    c = {
        "matrix": {"score": 5, "direction": "buy"},
        "rsi": {"snapshot": {"rsi_state": "oversold"},
                "recent_price_rsi_divergence": {"direction": "bullish"},
                "recent_price_macd_divergence": None},
    }
    r = compute_votes(c, "BUY", PARAMS, rule_status=ALL_PROPOSED, allow_proposed=False)
    assert r["net_votes"] == 0.0
