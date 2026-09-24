"""Tests for the proposed RSI + MACD divergence module (src/features/rsi.py).
See the 2026-09-24 task report for why this module has no SPEC.md section yet."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from src.features.rsi import (
    compute_rsi_macd_snapshot,
    compute_price_rsi_divergence,
    compute_price_macd_divergence,
    _find_swings,
)

BAR = timedelta(minutes=5)
T0 = datetime(2026, 1, 2, 10, 0, tzinfo=timezone.utc)

RSI_PARAMS = {"period": 14, "overbought": 70, "oversold": 30}
MACD_PARAMS = {"fast_period": 12, "slow_period": 26, "signal_period": 9, "divergence_source": "macd_line"}
DIVERGENCE_PARAMS = {"swing_n": 3, "lookback_bars": 100}


def _c(ts, o, h, l, c):
    return {"ts_utc": ts, "open": o, "high": h, "low": l, "close": c}


def _flat_candles(n, price=4200.0, ts0=T0):
    return [_c(ts0 + i * BAR, price, price + 1, price - 1, price) for i in range(n)]


def _candles_from_closes(closes, ts0=T0):
    out = []
    for i, close in enumerate(closes):
        out.append(_c(ts0 + i * BAR, close, close + 0.5, close - 0.5, close))
    return out


# ---------------------------------------------------------------------------
# Part A -- RSI + MACD snapshot
# ---------------------------------------------------------------------------

def test_rsi_neutral_on_flat_price():
    candles = _flat_candles(60)
    rows = compute_rsi_macd_snapshot(candles, candles[-1]["ts_utc"], "TEST", "M5", RSI_PARAMS, MACD_PARAMS)
    assert len(rows) > 0
    last = rows[-1]
    # Constant closes -> RSI is undefined direction, TA-Lib returns 100 or NaN
    # depending on the exact flat run; what matters here is it's a finite
    # number and the state classification is internally consistent.
    assert math.isfinite(last["rsi"])
    if last["rsi"] >= 70:
        assert last["rsi_state"] == "overbought"
    elif last["rsi"] <= 30:
        assert last["rsi_state"] == "oversold"
    else:
        assert last["rsi_state"] == "neutral"


def test_rsi_overbought_on_strong_uptrend():
    closes = [4200.0 + i * 2.0 for i in range(40)]  # relentless uptrend
    candles = _candles_from_closes(closes)
    rows = compute_rsi_macd_snapshot(candles, candles[-1]["ts_utc"], "TEST", "M5", RSI_PARAMS, MACD_PARAMS)
    assert rows[-1]["rsi"] >= 70
    assert rows[-1]["rsi_state"] == "overbought"


def test_rsi_oversold_on_strong_downtrend():
    closes = [4200.0 - i * 2.0 for i in range(40)]
    candles = _candles_from_closes(closes)
    rows = compute_rsi_macd_snapshot(candles, candles[-1]["ts_utc"], "TEST", "M5", RSI_PARAMS, MACD_PARAMS)
    assert rows[-1]["rsi"] <= 30
    assert rows[-1]["rsi_state"] == "oversold"


def test_snapshot_returns_macd_fields():
    closes = [4200.0 + math.sin(i / 5) * 10 for i in range(80)]
    candles = _candles_from_closes(closes)
    rows = compute_rsi_macd_snapshot(candles, candles[-1]["ts_utc"], "TEST", "M5", RSI_PARAMS, MACD_PARAMS)
    assert len(rows) > 0
    for r in rows:
        assert set(r.keys()) >= {"symbol", "tf", "ts_utc", "rsi", "rsi_state", "macd", "macd_signal", "macd_hist"}
        assert math.isfinite(r["macd"])
        assert math.isfinite(r["macd_signal"])
        assert math.isfinite(r["macd_hist"])


def test_too_few_bars_returns_empty():
    candles = _flat_candles(5)
    rows = compute_rsi_macd_snapshot(candles, candles[-1]["ts_utc"], "TEST", "M5", RSI_PARAMS, MACD_PARAMS)
    assert rows == []


# ---------------------------------------------------------------------------
# Anti-lookahead (CLAUDE.md rule 1)
# ---------------------------------------------------------------------------

def test_snapshot_no_lookahead():
    """A snapshot computed as_of an earlier bar must not change when later
    candles are appended to the input list."""
    closes = [4200.0 + math.sin(i / 4) * 15 for i in range(100)]
    candles = _candles_from_closes(closes)
    cutoff_ts = candles[59]["ts_utc"]

    rows_truncated = compute_rsi_macd_snapshot(candles[:60], cutoff_ts, "TEST", "M5", RSI_PARAMS, MACD_PARAMS)
    rows_full_input = compute_rsi_macd_snapshot(candles, cutoff_ts, "TEST", "M5", RSI_PARAMS, MACD_PARAMS)

    assert rows_truncated[-1]["ts_utc"] == rows_full_input[-1]["ts_utc"]
    assert rows_truncated[-1]["rsi"] == pytest.approx(rows_full_input[-1]["rsi"])
    assert rows_truncated[-1]["macd"] == pytest.approx(rows_full_input[-1]["macd"])


def test_divergence_no_lookahead():
    """A divergence confirmed strictly after as_of_ts must not appear."""
    # Build price that forms two swing highs with RSI diverging, but make
    # the second swing's confirmation bars land AFTER an early as_of_ts.
    closes = (
        [4200.0 + i for i in range(10)]        # ramp up to first swing high
        + [4210.0 - i for i in range(10)]       # down
        + [4180.0 + i * 0.5 for i in range(10)]  # weaker ramp up to 2nd swing high (lower momentum)
        + [4185.0 - i for i in range(10)]
    )
    candles = _candles_from_closes(closes)
    full_as_of = candles[-1]["ts_utc"]
    early_as_of = candles[25]["ts_utc"]  # before the 2nd swing's confirmation bars exist

    events_early = compute_price_rsi_divergence(candles, early_as_of, "TEST", "M5", RSI_PARAMS, DIVERGENCE_PARAMS)
    events_full = compute_price_rsi_divergence(candles, full_as_of, "TEST", "M5", RSI_PARAMS, DIVERGENCE_PARAMS)

    for e in events_early:
        assert e["confirmed_ts"] <= early_as_of, "divergence event confirmed after as_of_ts leaked in"
    # Truncating the input must never produce an event absent from the full run.
    early_keys = {(e["swing1_ts"], e["swing2_ts"], e["kind"]) for e in events_early}
    full_keys = {(e["swing1_ts"], e["swing2_ts"], e["kind"]) for e in events_full}
    assert early_keys <= full_keys


# ---------------------------------------------------------------------------
# Part B -- price/RSI divergence
# ---------------------------------------------------------------------------

def _lead_in(n=16, price=4200.0):
    """Mild noise so RSI(14)'s warm-up window is past before the pattern
    under test begins -- otherwise the first swing's RSI value is NaN and
    the divergence comparison is silently skipped."""
    return [price + (1 if i % 2 == 0 else -1) * 0.5 for i in range(n)]


def test_bearish_price_rsi_divergence_detected():
    """Two swing highs: price makes a higher high, momentum (RSI) makes a
    lower high because the second rally is weaker/slower."""
    closes = (
        _lead_in()
        + [4200.0]
        + [4200.0 + i * 3 for i in range(1, 11)]    # sharp rally to swing high #1 (4230)
        + [4230.0 - i * 2 for i in range(1, 11)]    # pull back to 4210
        + [4210.0 + i * 1.5 for i in range(1, 21)]  # slower/longer rally to a HIGHER price (4240)
        + [4240.0 - i * 2 for i in range(1, 11)]
    )
    candles = _candles_from_closes(closes)
    as_of = candles[-1]["ts_utc"]

    events = compute_price_rsi_divergence(candles, as_of, "TEST", "M5", RSI_PARAMS, DIVERGENCE_PARAMS)
    bearish = [e for e in events if e["direction"] == "bearish"]
    assert len(bearish) >= 1
    e = bearish[0]
    assert e["swing2_price"] > e["swing1_price"], "price must make a higher high"
    assert e["swing2_indicator"] < e["swing1_indicator"], "RSI must make a lower high"
    assert e["kind"] == "price_rsi"


def test_bullish_price_rsi_divergence_detected():
    """Two swing lows: price makes a lower low, RSI makes a higher low
    (selling momentum weakening)."""
    closes = (
        _lead_in()
        + [4200.0]
        + [4200.0 - i * 3 for i in range(1, 11)]    # sharp drop to swing low #1 (4170)
        + [4170.0 + i * 2 for i in range(1, 11)]    # bounce to 4190
        + [4190.0 - i * 1.5 for i in range(1, 21)]  # slower/longer drop to a LOWER price (4160)
        + [4160.0 + i * 2 for i in range(1, 11)]
    )
    candles = _candles_from_closes(closes)
    as_of = candles[-1]["ts_utc"]

    events = compute_price_rsi_divergence(candles, as_of, "TEST", "M5", RSI_PARAMS, DIVERGENCE_PARAMS)
    bullish = [e for e in events if e["direction"] == "bullish"]
    assert len(bullish) >= 1
    e = bullish[0]
    assert e["swing2_price"] < e["swing1_price"], "price must make a lower low"
    assert e["swing2_indicator"] > e["swing1_indicator"], "RSI must make a higher low"


def test_no_divergence_on_confirming_trend():
    """Price higher high + RSI higher high (trend confirmed, not diverging)
    must NOT fire a bearish divergence event."""
    closes = [4200.0 + i * 2 for i in range(60)]  # steady monotonic uptrend
    candles = _candles_from_closes(closes)
    as_of = candles[-1]["ts_utc"]
    events = compute_price_rsi_divergence(candles, as_of, "TEST", "M5", RSI_PARAMS, DIVERGENCE_PARAMS)
    assert all(e["direction"] != "bearish" for e in events)


# ---------------------------------------------------------------------------
# Part C -- price/MACD divergence
# ---------------------------------------------------------------------------

def test_bearish_price_macd_divergence_detected():
    # MACD(12,26,9) needs ~35 bars warm-up (slow_period + signal_period),
    # longer than RSI(14) -- a longer lead-in than the RSI tests use.
    closes = (
        _lead_in(n=40)
        + [4200.0]
        + [4200.0 + i * 3 for i in range(1, 11)]
        + [4230.0 - i * 2 for i in range(1, 11)]
        + [4210.0 + i * 1.5 for i in range(1, 21)]
        + [4240.0 - i * 2 for i in range(1, 11)]
    )
    candles = _candles_from_closes(closes)
    as_of = candles[-1]["ts_utc"]

    events = compute_price_macd_divergence(candles, as_of, "TEST", "M5", MACD_PARAMS, DIVERGENCE_PARAMS)
    bearish = [e for e in events if e["direction"] == "bearish"]
    assert len(bearish) >= 1
    assert bearish[0]["kind"] == "price_macd"


def test_macd_divergence_source_is_configurable():
    closes = [4200.0 + math.sin(i / 6) * 12 for i in range(100)]
    candles = _candles_from_closes(closes)
    as_of = candles[-1]["ts_utc"]
    line_params = {**MACD_PARAMS, "divergence_source": "macd_line"}
    hist_params = {**MACD_PARAMS, "divergence_source": "histogram"}
    # Should not raise for either source, and both must only use kind='price_macd'.
    events_line = compute_price_macd_divergence(candles, as_of, "TEST", "M5", line_params, DIVERGENCE_PARAMS)
    events_hist = compute_price_macd_divergence(candles, as_of, "TEST", "M5", hist_params, DIVERGENCE_PARAMS)
    for e in events_line + events_hist:
        assert e["kind"] == "price_macd"


# ---------------------------------------------------------------------------
# Swing detector itself
# ---------------------------------------------------------------------------

def test_find_swings_confirms_with_lag():
    """A swing at index i must have confirmed_idx == i + swing_n (CLAUDE.md
    rule 2: confirmation delay, no repainting)."""
    import numpy as np
    highs = np.array([1, 2, 3, 10, 3, 2, 1, 1, 1, 2, 3], dtype=float)
    lows = highs - 0.5
    ts = [T0 + i * BAR for i in range(len(highs))]
    swings = _find_swings(highs, lows, ts, swing_n=3)
    swing_highs = [s for s in swings if s["type"] == "high"]
    assert any(s["idx"] == 3 and s["confirmed_idx"] == 6 for s in swing_highs)
