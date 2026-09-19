"""Tests for src/backtest/replay.py — pure logic only (no DB needed)."""
from __future__ import annotations

import math
import numpy as np
import pytest

from src.backtest.replay import _determine_outcome


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
