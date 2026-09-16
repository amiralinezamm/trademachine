"""Tests for src/features/regime.py (SPEC.md 4.9)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.features.regime import compute_regime, SESSIONS

BAR = timedelta(minutes=5)
T0 = datetime(2024, 1, 2, 10, 0, tzinfo=timezone.utc)  # 10:00 UTC → London session

# Small params for deterministic tests
PARAMS = {
    "atr_period": 3,
    "adx_period": 3,
    "bb_period": 5,
    "bb_std": 2.0,
    "trend_adx_thresh": 25,
    "range_adx_thresh": 20,
    "bb_width_pct_window": 20,
    "range_bb_pct_thresh": 30,
}


def _flat_candles(n: int, price: float = 2000.0, base_ts: datetime = T0) -> list[dict]:
    """Flat market — low volatility, ADX should be low."""
    out = []
    for i in range(n):
        out.append({
            "ts_utc": base_ts + i * BAR,
            "open": price, "high": price + 0.5, "low": price - 0.5, "close": price,
        })
    return out


def _trending_candles(n: int, step: float = 10.0, base_ts: datetime = T0) -> list[dict]:
    """Strong uptrend — ADX should be high."""
    out = []
    for i in range(n):
        p = 2000.0 + i * step
        out.append({
            "ts_utc": base_ts + i * BAR,
            "open": p, "high": p + 5.0, "low": p - 2.0, "close": p + 4.0,
        })
    return out


# --- Test 1: not enough candles returns empty result -----------------------

def test_insufficient_candles_returns_empty():
    candles = _flat_candles(10)
    result = compute_regime(candles, candles[-1]["ts_utc"], "TEST", "M5", params=PARAMS)
    assert result["snapshots"] == []
    assert result["range_duration"] == {}
    assert result["breakout_prob"] == {}


# --- Test 2: anti-lookahead — candles after as_of_ts ignored ---------------

def test_anti_lookahead():
    candles = _flat_candles(60)
    future = _flat_candles(10, base_ts=candles[-1]["ts_utc"] + BAR)
    all_candles = candles + future
    as_of = candles[-1]["ts_utc"]

    result_clipped = compute_regime(all_candles, as_of, "TEST", "M5", params=PARAMS)
    result_baseline = compute_regime(candles, as_of, "TEST", "M5", params=PARAMS)

    ts_clipped  = [s["ts_utc"] for s in result_clipped["snapshots"]]
    ts_baseline = [s["ts_utc"] for s in result_baseline["snapshots"]]
    assert ts_clipped == ts_baseline, (
        "Future candles beyond as_of_ts must not appear in snapshots (anti-lookahead)"
    )
    # ADX values at the cutoff must be identical
    adx_clipped  = result_clipped["snapshots"][-1]["adx"]
    adx_baseline = result_baseline["snapshots"][-1]["adx"]
    assert adx_clipped == adx_baseline, (
        "Future candles beyond as_of_ts must not change adx values (anti-lookahead)"
    )


# --- Test 3: snapshot schema correct ---------------------------------------

def test_snapshot_schema():
    candles = _flat_candles(60)
    result = compute_regime(candles, candles[-1]["ts_utc"], "XAUUSD@", "M5", params=PARAMS)
    assert result["snapshots"], "Should produce some snapshots"
    snap = result["snapshots"][-1]
    for key in ("symbol", "tf_origin", "ts_utc", "regime", "adx", "bb_width", "bb_width_pct"):
        assert key in snap, f"Missing key: {key}"
    assert snap["regime"] in ("trend", "range", "gray")
    assert snap["symbol"] == "XAUUSD@"
    assert snap["tf_origin"] == "M5"


# --- Test 4: flat market produces range regime in late bars ----------------

def test_flat_market_produces_range():
    candles = _flat_candles(150)
    result = compute_regime(candles, candles[-1]["ts_utc"], "TEST", "M5", params=PARAMS)
    late_snaps = result["snapshots"][-30:]
    regimes = [s["regime"] for s in late_snaps]
    # In a flat market ADX should be very low → range or gray
    assert "trend" not in regimes, (
        "Flat market should not produce trend regime in late bars"
    )


# --- Test 5: session assignment is correct ---------------------------------

def test_session_assignments():
    # 10:00 UTC is in London only (not Asia 0-9, not NY 12-21)
    assert "London" in [s for s, (a, b) in SESSIONS.items() if a <= 10 < b]
    assert "Asia" not in [s for s, (a, b) in SESSIONS.items() if a <= 10 < b]
    # 13:00 UTC is in both London and NY
    both = [s for s, (a, b) in SESSIONS.items() if a <= 13 < b]
    assert "London" in both and "NY" in both


# --- Test 6: breakout_prob keys are fractions in [0,1] --------------------

def test_breakout_prob_range():
    candles = _flat_candles(200)
    result = compute_regime(candles, candles[-1]["ts_utc"], "TEST", "M5", params=PARAMS)
    for hour, prob in result["breakout_prob"]["by_hour"].items():
        assert 0.0 <= prob <= 1.0, f"Breakout prob for hour {hour} out of range: {prob}"
    for sess, prob in result["breakout_prob"]["by_session"].items():
        assert 0.0 <= prob <= 1.0, f"Breakout prob for session {sess} out of range: {prob}"
