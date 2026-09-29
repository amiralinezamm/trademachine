"""Pure tests for src/engine/exit_rules.py — no DB, no network."""
import pytest

from src.engine.exit_rules import (
    ABSOLUTE_MIN_RR,
    compute_level_based_sl_tp,
    detect_reversal_close,
)

PARAMS = {
    "sl_tp_margin_atr_mult": 0.25,
    "min_rr": 2.0,
    "high_confidence_net_votes": 4.0,
}

ATR = 4.0  # margin = 1.0


def _level(id_, kind, price_low, price_high, strength):
    return {
        "id": id_, "kind": kind, "price_low": price_low,
        "price_high": price_high, "strength": strength, "status": "active",
    }


def _level_a_buy():
    # support the BUY signal fired from
    return _level("A", "support", 2340.0, 2342.0, 1.5)


def _fillers(kind):
    """Low-strength levels of `kind` so the strength median in a test isn't
    computed from just 2 levels (where one of A/B almost always ends up on
    the wrong side of the median by construction). kind is chosen to never
    match the query side (e.g. 'support' fillers for a BUY test, which only
    searches 'resistance' candidates), so they can't accidentally become B."""
    return [_level(f"filler{i}", kind, 2000.0, 2001.0, 0.05) for i in range(3)]


def test_nearest_qualifying_b_meets_min_rr():
    level_a = _level_a_buy()
    entry = 2343.0
    # SL = 2340 - 1.0 = 2339.0 -> sl_dist = 4.0
    # need tp_dist >= 8.0 for rr>=2 -> tp >= 2351.0 -> level_low - 1.0 >= 2351 -> level_low >= 2352
    level_b = _level("B", "resistance", 2353.0, 2355.0, 1.4)
    levels = [level_a, level_b] + _fillers("support")

    result = compute_level_based_sl_tp("BUY", level_a, entry, ATR, levels, net_votes=1.0, params=PARAMS)

    assert result is not None
    assert result["level_b_id"] == "B"
    assert result["stop_loss"] == pytest.approx(2339.0)
    assert result["take_profit"] == pytest.approx(2352.0)
    assert result["rr"] >= 2.0
    assert result["relaxed"] is False


def test_searches_past_a_too_close_level_to_a_farther_qualifying_one():
    level_a = _level_a_buy()
    entry = 2343.0
    too_close = _level("B1", "resistance", 2346.0, 2347.0, 1.4)  # rr=0.5, below min_rr
    far_enough = _level("B2", "resistance", 2353.0, 2355.0, 1.4)  # rr >= 2
    levels = [level_a, too_close, far_enough] + _fillers("support")

    result = compute_level_based_sl_tp("BUY", level_a, entry, ATR, levels, net_votes=1.0, params=PARAMS)

    assert result is not None
    assert result["level_b_id"] == "B2"
    assert result["relaxed"] is False


def test_weak_level_never_chosen_as_b_even_if_geometry_is_perfect():
    """'سطح B باید نظیر سطح A باشد' -- B must clear the same
    above-median-strength bar A itself had to clear."""
    level_a = _level_a_buy()
    entry = 2343.0
    weak_level = _level("Bweak", "resistance", 2353.0, 2355.0, 0.01)
    levels = [level_a, weak_level] + _fillers("support")

    result = compute_level_based_sl_tp("BUY", level_a, entry, ATR, levels, net_votes=10.0, params=PARAMS)

    assert result is None


def test_no_qualifying_b_at_all_rejects_signal():
    level_a = _level_a_buy()
    entry = 2343.0
    levels = [level_a]  # nothing ahead in the BUY direction

    result = compute_level_based_sl_tp("BUY", level_a, entry, ATR, levels, net_votes=10.0, params=PARAMS)

    assert result is None


def test_high_confidence_relaxes_below_min_rr_but_not_below_one():
    level_a = _level_a_buy()
    entry = 2343.0
    # sl_dist = 4.0; put B so tp_dist ~= 5.0 -> rr = 1.25 (below min_rr=2, above 1.0)
    close_b = _level("B", "resistance", 2349.0, 2350.0, 1.4)
    levels = [level_a, close_b] + _fillers("support")

    rejected_low_confidence = compute_level_based_sl_tp(
        "BUY", level_a, entry, ATR, levels, net_votes=1.0, params=PARAMS
    )
    assert rejected_low_confidence is None

    accepted_high_confidence = compute_level_based_sl_tp(
        "BUY", level_a, entry, ATR, levels, net_votes=4.0, params=PARAMS
    )
    assert accepted_high_confidence is not None
    assert accepted_high_confidence["relaxed"] is True
    assert accepted_high_confidence["rr"] >= ABSOLUTE_MIN_RR


def test_high_confidence_never_allows_sl_bigger_than_tp():
    level_a = _level_a_buy()
    entry = 2343.0
    # B so close that tp_dist < sl_dist (rr < 1.0) -- must be rejected
    # even at very high confidence.
    very_close_b = _level("B", "resistance", 2343.5, 2344.0, 1.4)
    levels = [level_a, very_close_b] + _fillers("support")

    result = compute_level_based_sl_tp(
        "BUY", level_a, entry, ATR, levels, net_votes=100.0, params=PARAMS
    )
    assert result is None


def test_sell_direction_mirrors_buy():
    level_a = _level("A", "resistance", 2360.0, 2362.0, 1.5)
    entry = 2359.0
    # SL = 2362 + 1.0 = 2363.0 -> sl_dist = 4.0
    level_b = _level("B", "support", 2347.0, 2349.0, 1.4)  # tp = price_high(2349) + margin(1.0) = 2350
    levels = [level_a, level_b] + _fillers("resistance")

    result = compute_level_based_sl_tp("SELL", level_a, entry, ATR, levels, net_votes=1.0, params=PARAMS)

    assert result is not None
    assert result["stop_loss"] == pytest.approx(2363.0)
    assert result["take_profit"] == pytest.approx(2350.0)  # price_high(2349) + margin(1.0)
    assert result["rr"] >= 2.0


def test_invalid_direction_raises():
    with pytest.raises(ValueError):
        compute_level_based_sl_tp("HOLD", _level_a_buy(), 2343.0, ATR, [], 1.0, PARAMS)


# ---------------------------------------------------------------------------
# detect_reversal_close
# ---------------------------------------------------------------------------

def test_reversal_close_buy_open_structure_flips_bearish():
    assert detect_reversal_close("BUY", "bearish") is True


def test_reversal_close_sell_open_structure_flips_bullish():
    assert detect_reversal_close("SELL", "bullish") is True


def test_reversal_close_no_conflict():
    assert detect_reversal_close("BUY", "bullish") is False
    assert detect_reversal_close("SELL", "bearish") is False


def test_reversal_close_unknown_structure_never_triggers():
    assert detect_reversal_close("BUY", None) is False
    assert detect_reversal_close("BUY", "unknown") is False
