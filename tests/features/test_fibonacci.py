"""Tests for src/features/fibonacci.py (SPEC.md 4.6)."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from src.features.fibonacci import compute_fibonacci

BAR = timedelta(minutes=5)
T0 = datetime(2024, 1, 1, tzinfo=timezone.utc)

PARAMS = {
    "atr_period": 3,          # small for tests
    "max_pair_distance_atr": 200,
    "max_pairs": 10,
    "overlap_atr_mult": 1.0,  # generous for tests
    "retracement_levels": [38.2, 50.0, 61.8],
    "extension_levels": [61.8, 100.0, 161.8],
}


def _candles(prices: list[float], base_ts: datetime = T0) -> list[dict]:
    """Generate candles from a list of close prices (open=close=high=low)."""
    out = []
    for i, p in enumerate(prices):
        out.append({
            "ts_utc": base_ts + i * BAR,
            "open": p, "high": p + 1.0, "low": p - 1.0, "close": p,
        })
    return out


def _level(kind: str, price_low: float, price_high: float) -> dict:
    return {
        "kind": kind,
        "price_low": price_low,
        "price_high": price_high,
        "status": "active",
        "break_count": 0,
        "atr_at_birth": 2.0,
    }


# --- Test 1: returns empty list when no active levels provided ---------------

def test_no_active_levels_returns_empty():
    candles = _candles([2000.0] * 20)
    result = compute_fibonacci(candles, candles[-1]["ts_utc"], "TEST", "M5", [], params=PARAMS)
    assert result == []


# --- Test 2: basic retracements + extensions are produced -------------------

def test_produces_retracements_and_extensions():
    candles = _candles([2000.0] * 20)
    levels = [
        _level("support", 1990.0, 1992.0),      # mid = 1991
        _level("resistance", 2010.0, 2012.0),   # mid = 2011
    ]
    result = compute_fibonacci(candles, candles[-1]["ts_utc"], "XAUUSD@", "M5", levels, params=PARAMS)

    roles = {z["role"] for z in result}
    assert "retracement" in roles
    assert "extension" in roles

    ret_pcts = sorted({z["level_pct"] for z in result if z["role"] == "retracement"})
    ext_pcts = sorted({z["level_pct"] for z in result if z["role"] == "extension"})
    assert ret_pcts == [38.2, 50.0, 61.8]
    assert ext_pcts == [61.8, 100.0, 161.8]


# --- Test 3: anti-lookahead — candles after as_of_ts are ignored ------------

def test_anti_lookahead_as_of_ts():
    candles = _candles([2000.0] * 20)
    levels = [
        _level("support", 1990.0, 1992.0),
        _level("resistance", 2010.0, 2012.0),
    ]
    # Append future candles that would change ATR
    future = _candles([9999.0] * 5, base_ts=candles[-1]["ts_utc"] + BAR)
    all_candles = candles + future

    as_of = candles[-1]["ts_utc"]  # cutoff at last "current" candle
    result_at_cutoff = compute_fibonacci(all_candles, as_of, "TEST", "M5", levels, params=PARAMS)
    result_without_future = compute_fibonacci(candles, as_of, "TEST", "M5", levels, params=PARAMS)

    # Prices must be identical — future candles must not affect output
    prices_cutoff  = sorted(z["price"] for z in result_at_cutoff)
    prices_nowfut  = sorted(z["price"] for z in result_without_future)
    assert prices_cutoff == prices_nowfut, (
        "Future candles beyond as_of_ts must not affect fibonacci output (anti-lookahead)"
    )


# --- Test 4: extension role is extension, retracement role is retracement ---

def test_role_field_correct():
    candles = _candles([2000.0] * 20)
    levels = [
        _level("support", 1900.0, 1902.0),
        _level("resistance", 2100.0, 2102.0),
    ]
    result = compute_fibonacci(candles, candles[-1]["ts_utc"], "TEST", "M5", levels, params=PARAMS)

    for z in result:
        assert z["role"] in ("retracement", "extension"), f"Unexpected role: {z['role']}"
        if z["role"] == "extension":
            # Extension prices are ABOVE the swing high (for a bullish move)
            # swing_high mid ~2101, price > swing_high for extension
            assert z["price"] > z["swing_high"] or z["price"] < z["swing_low"], (
                "Extension price should be outside the swing range"
            )


# --- Test 5: overlapping flag set when fib price is near an active level ----

def test_overlapping_flag():
    candles = _candles([2000.0] * 20)
    # Support near 1950, resistance near 2050 → 50% retracement ≈ 2000
    # Place another active resistance level exactly at 2000
    levels = [
        _level("support", 1948.0, 1952.0),          # mid = 1950
        _level("resistance", 2048.0, 2052.0),        # mid = 2050
        _level("resistance", 1998.0, 2002.0),        # mid = 2000 — triggers overlap
    ]
    # ATR is very small in our mock candles (high-low spread = 2.0),
    # so use a tighter overlap_atr_mult
    p = {**PARAMS, "overlap_atr_mult": 5.0}  # 5× ATR window
    result = compute_fibonacci(candles, candles[-1]["ts_utc"], "TEST", "M5", levels, params=p)

    # The 50% level should land near 2000 and overlap with the active level there
    fifty_pct = [z for z in result if z["level_pct"] == 50.0 and z["role"] == "retracement"]
    assert fifty_pct, "50% retracement zone must exist"
    assert any(z["overlapping"] for z in fifty_pct), (
        "50% retracement near an active level must be flagged overlapping=True"
    )


# --- Test 6: pair distance filter — pairs too far apart are skipped ---------

def test_max_pair_distance_filter():
    candles = _candles([2000.0] * 20)
    # ATR ≈ 2 in test candles, max_pair_distance_atr=5 → max_dist ≈ 10
    # This pair spans 500 points — should be filtered out
    levels = [
        _level("support", 1500.0, 1502.0),
        _level("resistance", 2000.0, 2002.0),
    ]
    p = {**PARAMS, "max_pair_distance_atr": 5}
    result = compute_fibonacci(candles, candles[-1]["ts_utc"], "TEST", "M5", levels, params=p)
    assert result == [], "Pair too far apart must be filtered out"
