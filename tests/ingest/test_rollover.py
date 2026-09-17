"""Tests for src/ingest/rollover.py (docs/instrument_rollover.md)."""
from __future__ import annotations

from src.ingest.rollover import (
    select_front_month,
    compute_back_adjustment,
    apply_back_adjustment,
)


# --- select_front_month -------------------------------------------------

def test_disqualifies_disabled_and_returns_sole_survivor():
    """Matches the live 2026-09-17 result for all three instruments: exactly
    one non-DISABLED candidate, volume tie-break never engages."""
    candidates = [
        {"symbol": "USINDX.U26", "trade_mode": 0, "volume_window": 5572},
        {"symbol": "USINDX.Z26", "trade_mode": 4, "volume_window": 208712},
    ]
    winner = select_front_month(candidates)
    assert winner["symbol"] == "USINDX.Z26"


def test_picks_highest_volume_among_multiple_survivors():
    """When two candidates are simultaneously non-DISABLED (expected during
    an actual rollover window), volume is the tie-break."""
    candidates = [
        {"symbol": "UKBRENT.X26", "trade_mode": 4, "volume_window": 100},
        {"symbol": "UKBRENT.Z26", "trade_mode": 3, "volume_window": 500},
    ]
    winner = select_front_month(candidates)
    assert winner["symbol"] == "UKBRENT.Z26"


def test_returns_none_when_all_disabled():
    candidates = [
        {"symbol": "UKBRENT.Z26", "trade_mode": 0, "volume_window": 173993},
        {"symbol": "UKBRENT.F27", "trade_mode": 0, "volume_window": 0},
    ]
    assert select_front_month(candidates) is None


def test_empty_candidate_list_returns_none():
    assert select_front_month([]) is None


def test_closeonly_is_not_disqualified():
    """CLOSEONLY (trade_mode=3) is a live 2026-09-17 case (10TBILL.Z26) --
    must NOT be treated the same as DISABLED (trade_mode=0)."""
    candidates = [
        {"symbol": "10TBILL.U26", "trade_mode": 0, "volume_window": 8715},
        {"symbol": "10TBILL.Z26", "trade_mode": 3, "volume_window": 9545},
    ]
    winner = select_front_month(candidates)
    assert winner["symbol"] == "10TBILL.Z26"


# --- compute_back_adjustment ---------------------------------------------

def test_back_adjustment_is_additive_difference():
    offset = compute_back_adjustment(old_contract_last_close=100.0, new_contract_first_close=103.5)
    assert offset == 3.5


def test_back_adjustment_negative_when_new_is_lower():
    offset = compute_back_adjustment(old_contract_last_close=100.0, new_contract_first_close=97.0)
    assert offset == -3.0


# --- apply_back_adjustment -------------------------------------------------

def test_apply_back_adjustment_shifts_ohlc_only():
    candles = [
        {"ts_utc": "t1", "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "tick_volume": 10},
    ]
    adjusted = apply_back_adjustment(candles, offset=2.0)
    assert adjusted[0]["open"] == 102.0
    assert adjusted[0]["high"] == 103.0
    assert adjusted[0]["low"] == 101.0
    assert adjusted[0]["close"] == 102.5
    # non-price fields pass through untouched
    assert adjusted[0]["ts_utc"] == "t1"
    assert adjusted[0]["tick_volume"] == 10


def test_apply_back_adjustment_does_not_mutate_input():
    candles = [{"ts_utc": "t1", "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5}]
    original_open = candles[0]["open"]
    apply_back_adjustment(candles, offset=5.0)
    assert candles[0]["open"] == original_open, "input list must not be mutated"


def test_apply_back_adjustment_zero_offset_is_a_noop_copy():
    candles = [{"ts_utc": "t1", "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5}]
    adjusted = apply_back_adjustment(candles, offset=0)
    assert adjusted == candles
    assert adjusted is not candles
