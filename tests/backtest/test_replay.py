"""Tests for src/backtest/replay.py — pure logic only (no DB needed)."""
from __future__ import annotations

import math
import numpy as np
import pytest

from datetime import datetime, timezone

from src.backtest.replay import _determine_outcome, _make_context


# ---------------------------------------------------------------------------
# Synthetic candle factory
# ---------------------------------------------------------------------------

def candle(open_=2600.0, high=2605.0, low=2595.0, close=2600.0, spread=6):
    return {"open": open_, "high": high, "low": low, "close": close, "spread": spread}


def flat_atr(n: int, val: float = 3.0):
    """Constant ATR array."""
    arr = np.full(n, val)
    return arr


# ---------------------------------------------------------------------------
# Helper: build a candle list with fixed values for most bars,
# then replace specific bars with given high/low/close.
# ---------------------------------------------------------------------------

def _make_candles(n, default_close=2600.0, overrides: dict | None = None):
    cs = [candle(close=default_close) for _ in range(n)]
    for idx, vals in (overrides or {}).items():
        cs[idx] = candle(**vals)
    return cs


# Shared constants
ENTRY_IDX = 5
ENTRY_PRICE = 2600.0
SL_BUY = 2594.0    # entry - 2*ATR (ATR=3)
TP_BUY  = 2609.0   # entry + 3*ATR
SL_SELL = 2606.0
TP_SELL = 2591.0
LEVEL_LO = 2596.0  # support zone low
LEVEL_HI = 2604.0  # support zone high (for BUY signal from support)
BREAK_MULT = 0.5
MAX_SAFETY = 10


def _run(candles, direction="BUY", atr_val=3.0,
         level_lo=LEVEL_LO, level_hi=LEVEL_HI):
    atr = flat_atr(len(candles), atr_val)
    if direction == "BUY":
        sl, tp = SL_BUY, TP_BUY
    else:
        sl, tp = SL_SELL, TP_SELL
    return _determine_outcome(
        candles, atr, ENTRY_IDX, ENTRY_PRICE, direction,
        sl, tp, MAX_SAFETY, level_lo, level_hi, BREAK_MULT,
    )


# ---------------------------------------------------------------------------
# TP tests
# ---------------------------------------------------------------------------

def test_tp_hit_buy():
    cs = _make_candles(20, overrides={ENTRY_IDX + 2: {"high": 2610.0, "low": 2598.0, "close": 2609.0}})
    outcome, exit_price = _run(cs, "BUY")
    assert outcome == "tp"
    assert exit_price == TP_BUY


def test_tp_hit_sell():
    cs = _make_candles(20, overrides={ENTRY_IDX + 2: {"high": 2600.0, "low": 2590.0, "close": 2591.0}})
    outcome, exit_price = _run(cs, "SELL")
    assert outcome == "tp"
    assert exit_price == TP_SELL


# ---------------------------------------------------------------------------
# SL tests
# ---------------------------------------------------------------------------

def test_sl_hit_buy():
    cs = _make_candles(20, overrides={ENTRY_IDX + 1: {"high": 2600.0, "low": 2590.0, "close": 2592.0}})
    outcome, exit_price = _run(cs, "BUY")
    assert outcome == "sl"
    assert exit_price == SL_BUY


def test_sl_hit_sell():
    cs = _make_candles(20, overrides={ENTRY_IDX + 1: {"high": 2608.0, "low": 2600.0, "close": 2607.0}})
    outcome, exit_price = _run(cs, "SELL")
    assert outcome == "sl"
    assert exit_price == SL_SELL


# ---------------------------------------------------------------------------
# Level invalidation — the key new test
# ---------------------------------------------------------------------------

def test_level_invalidated_buy_support_broken():
    """BUY from support. Price never hits SL/TP but closes BELOW level_lo - 0.5*ATR.
    level_lo=2596, ATR=3 → break threshold = 2596 - 0.5*3 = 2594.5.
    Set close=2594.0 (below threshold), high stays above SL (so SL not triggered by wick).
    Expected: level_invalidated, NOT sl or timeout.
    """
    # Bar at ENTRY_IDX+3: low=2595 (above SL=2594), close=2594.0 (below break threshold 2594.5)
    cs = _make_candles(20, overrides={
        ENTRY_IDX + 3: {"high": 2599.0, "low": 2595.0, "close": 2594.0},
    })
    # With level_lo=2596, ATR=3, break_mult=0.5: threshold = 2596 - 1.5 = 2594.5
    # close=2594.0 < 2594.5 → broken, but low=2595 > SL=2594 → SL not hit by low
    outcome, exit_price = _run(cs, "BUY", atr_val=3.0, level_lo=2596.0)
    assert outcome == "level_invalidated", f"Expected level_invalidated, got {outcome}"
    assert exit_price == 2594.0


def test_level_invalidated_sell_resistance_broken():
    """SELL from resistance. Price closes ABOVE level_hi + 0.5*ATR.
    level_hi=2604, ATR=3 → break threshold = 2604 + 1.5 = 2605.5.
    Set close=2606.0 (above threshold), low stays below SL (so SL not triggered).
    Expected: level_invalidated.
    """
    cs = _make_candles(20, overrides={
        ENTRY_IDX + 3: {"high": 2606.0, "low": 2601.0, "close": 2606.0},
    })
    # SL_SELL=2606 — high=2606 would trigger SL. Make SL slightly higher to isolate level test.
    atr = flat_atr(20, 3.0)
    outcome, exit_price = _determine_outcome(
        cs, atr, ENTRY_IDX, ENTRY_PRICE, "SELL",
        sl=2607.0, tp=TP_SELL, max_safety_bars=MAX_SAFETY,
        level_lo=LEVEL_LO, level_hi=2604.0, break_mult=0.5,
    )
    assert outcome == "level_invalidated", f"Expected level_invalidated, got {outcome}"
    assert exit_price == 2606.0


def test_sl_takes_priority_over_level_invalidation_buy():
    """When low <= SL (SL check) and close also below break threshold on the same bar,
    SL must win since SL is checked first (conservative)."""
    # low=2593 < SL=2594, close=2593 < break threshold 2594.5
    cs = _make_candles(20, overrides={
        ENTRY_IDX + 2: {"high": 2600.0, "low": 2593.0, "close": 2593.0},
    })
    outcome, _ = _run(cs, "BUY")
    assert outcome == "sl"


def test_level_not_invalidated_when_atr_is_nan():
    """NaN ATR bars must not trigger level_invalidated (guard against NaN math)."""
    cs = _make_candles(20, overrides={
        ENTRY_IDX + 2: {"high": 2598.0, "low": 2590.0, "close": 2585.0},
    })
    atr = flat_atr(20, 3.0)
    atr[ENTRY_IDX + 2] = float("nan")
    outcome, _ = _determine_outcome(
        cs, atr, ENTRY_IDX, ENTRY_PRICE, "BUY",
        SL_BUY, TP_BUY, MAX_SAFETY, LEVEL_LO, LEVEL_HI, BREAK_MULT,
    )
    # NaN ATR → level invalidation skipped; SL was not hit by low (low=2590 > SL=2594)...
    # actually low=2590 < SL=2594, so SL fires. Let's fix the candle.
    # Use a bar where low stays above SL but close is very low — with NaN ATR, should NOT invalidate.
    cs2 = _make_candles(20, overrides={
        ENTRY_IDX + 2: {"high": 2600.0, "low": 2595.0, "close": 2580.0},
    })
    atr2 = flat_atr(20, 3.0)
    atr2[ENTRY_IDX + 2] = float("nan")
    outcome2, _ = _determine_outcome(
        cs2, atr2, ENTRY_IDX, ENTRY_PRICE, "BUY",
        SL_BUY, TP_BUY, MAX_SAFETY, LEVEL_LO, LEVEL_HI, BREAK_MULT,
    )
    assert outcome2 != "level_invalidated"


# ---------------------------------------------------------------------------
# Timeout / open tests
# ---------------------------------------------------------------------------

def test_timeout_when_nothing_triggered():
    """Price stays flat for MAX_SAFETY bars → timeout."""
    cs = _make_candles(20, default_close=2600.0)
    outcome, exit_price = _run(cs, "BUY")
    assert outcome == "timeout"


def test_open_when_dataset_ends_before_safety_cap():
    """Only ENTRY_IDX+2 bars exist after entry — dataset ends before safety cap."""
    cs = _make_candles(ENTRY_IDX + 3, default_close=2600.0)
    outcome, _ = _run(cs, "BUY")
    assert outcome == "open"


# ---------------------------------------------------------------------------
# _make_context — memory_pattern_bias wiring (2026-09-29 regression guard)
#
# Bug: _make_context() never accepted or forwarded a memory_result to
# build_context(), so components["memory"] was unconditionally None on every
# backtest bar and module_voting._vote_memory always voted 0 — regardless of
# whether memory_results actually had rows for that period. Same bug class
# as the dollar_correlation_direction all_dxy NameError fixed just before
# this in the same file.
# ---------------------------------------------------------------------------

UTC = timezone.utc


def _empty_context_args(as_of_ts):
    return dict(
        all_regime=[], all_rounds=[], all_fib=[], all_pats=[], all_gaps=[], all_corr=[],
        close=2600.0, atr=3.0, as_of_ts=as_of_ts,
    )


def test_make_context_populates_memory_when_present():
    as_of = datetime(2026, 1, 2, tzinfo=UTC)
    all_memory = [
        {"ts_utc": datetime(2026, 1, 1, tzinfo=UTC), "n_matches": 12,
         "up_ratio": 0.72, "median_return": 1.5, "ci_low": 0.5, "ci_high": 2.0},
    ]
    ctx = _make_context(**_empty_context_args(as_of), all_memory=all_memory)
    assert ctx["memory"] is not None
    assert ctx["memory"]["up_ratio"] == 0.72
    assert ctx["memory"]["n_matches"] == 12


def test_make_context_memory_is_none_when_no_rows_yet():
    as_of = datetime(2026, 1, 2, tzinfo=UTC)
    ctx = _make_context(**_empty_context_args(as_of), all_memory=[])
    assert ctx["memory"] is None


def test_make_context_memory_respects_as_of_ts_anti_lookahead():
    """A memory_results row computed AFTER as_of_ts must not be used —
    _last_row_up_to() bisects on ts_utc <= as_of_ts."""
    as_of = datetime(2026, 1, 1, tzinfo=UTC)
    future_row = [{"ts_utc": datetime(2026, 1, 5, tzinfo=UTC), "n_matches": 9,
                    "up_ratio": 0.9, "median_return": 2.0, "ci_low": 1.0, "ci_high": 3.0}]
    ctx = _make_context(**_empty_context_args(as_of), all_memory=future_row)
    assert ctx["memory"] is None


def test_make_context_memory_picks_latest_not_first():
    as_of = datetime(2026, 1, 3, tzinfo=UTC)
    rows = [
        {"ts_utc": datetime(2026, 1, 1, tzinfo=UTC), "n_matches": 5,
         "up_ratio": 0.3, "median_return": -1.0, "ci_low": -2.0, "ci_high": 0.0},
        {"ts_utc": datetime(2026, 1, 2, tzinfo=UTC), "n_matches": 8,
         "up_ratio": 0.8, "median_return": 1.0, "ci_low": 0.2, "ci_high": 1.8},
    ]
    ctx = _make_context(**_empty_context_args(as_of), all_memory=rows)
    assert ctx["memory"]["up_ratio"] == 0.8  # the 2026-01-02 row, not 2026-01-01
