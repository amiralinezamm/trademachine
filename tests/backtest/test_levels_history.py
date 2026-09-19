"""Tests for levels_history point-in-time mechanism.

Verifies that:
1. compute_levels(record_history=True) emits creation/touch/break events.
2. _level_state_at correctly returns historical strength/status at any bar.
3. A level with high strength at T_old returns that strength for a bar at T_old,
   NOT the decayed/zero strength stored in the current DB.
"""
from __future__ import annotations

import bisect
from datetime import datetime, timedelta, timezone

import pytest

from src.features.levels import compute_levels

# ── Shared helpers ────────────────────────────────────────────────────────────

UTC = timezone.utc
T0 = datetime(2024, 1, 1, tzinfo=UTC)


def _ts(offset_bars: int) -> datetime:
    """Bar timestamp offset_bars × 5 min from T0."""
    return T0 + timedelta(minutes=5 * offset_bars)


def _candle(ts: datetime, o=2000.0, h=2005.0, lo=1995.0, c=2000.0) -> dict:
    return {"ts_utc": ts, "open": o, "high": h, "low": lo, "close": c}


# ── Minimal synthetic candle series that produces at least one level ──────────

def _build_candles(n: int = 200) -> list[dict]:
    """200 bars: baseline neutral, with one clear swing high around bar 80
    and one clear swing low around bar 140, each retracing enough to confirm."""
    candles = []
    for i in range(n):
        ts = _ts(i)
        base = 2000.0
        # Swing high at bar 80
        if 75 <= i <= 85:
            h = 2030.0 if i == 80 else 2010.0
            lo = 1995.0
            c = 2010.0 if i == 80 else 2000.0
        # Retrace after swing high (goes back to ~2000 to confirm)
        elif 86 <= i <= 100:
            h = 2005.0
            lo = 1985.0
            c = 1990.0
        # Swing low at bar 140
        elif 135 <= i <= 145:
            lo = 1960.0 if i == 140 else 1980.0
            h = 2005.0
            c = 1975.0 if i == 140 else 1990.0
        # Touch the resistance zone (around 2030) at bar 160
        elif 155 <= i <= 165:
            h = 2032.0  # enters zone [2027.5, 2032.5] and closes outside
            lo = 2005.0
            c = 2000.0  # closes below zone
        else:
            h = base + 5
            lo = base - 5
            c = base
        candles.append(_candle(_ts(i), h=h, lo=lo, c=c))
    return candles


# ── Test 1: record_history=True returns two values ────────────────────────────

def test_record_history_returns_tuple():
    candles = _build_candles()
    result = compute_levels(candles, as_of_ts=_ts(199), symbol="TEST", tf="M5",
                             record_history=True)
    assert isinstance(result, tuple) and len(result) == 2
    levels, events = result
    assert isinstance(levels, list)
    assert isinstance(events, list)


# ── Test 2: record_history=False is unchanged (backward compat) ───────────────

def test_record_history_false_returns_list():
    candles = _build_candles()
    result = compute_levels(candles, as_of_ts=_ts(199), symbol="TEST", tf="M5",
                             record_history=False)
    assert isinstance(result, list)


# ── Test 3: creation events are emitted ──────────────────────────────────────

def test_creation_events_emitted():
    candles = _build_candles()
    _, events = compute_levels(candles, as_of_ts=_ts(199), symbol="TEST", tf="M5",
                                record_history=True)
    creation_events = [e for e in events if e["touch_count"] == 0 and e["strength"] == 0.0]
    assert len(creation_events) > 0, "Expected at least one creation event"


# ── Test 4: touch events increase touch_count and emit positive strength ──────

def test_touch_events_have_positive_strength():
    candles = _build_candles()
    _, events = compute_levels(candles, as_of_ts=_ts(199), symbol="TEST", tf="M5",
                                record_history=True)
    touch_events = [e for e in events if e["touch_count"] > 0]
    # After a touch, strength must be > 0 (the level was visited)
    for ev in touch_events:
        assert ev["strength"] > 0, f"Touch event has strength=0: {ev}"


# ── Test 5: point-in-time lookup via _level_state_at ─────────────────────────

def _make_history_by_id(events: list[dict], level_id: int = 1) -> dict:
    """Map all events to a single synthetic level_id for testing."""
    rows = []
    for e in events:
        rows.append({
            "ts_utc": e["ts_utc"],
            "strength": e["strength"],
            "status": e["status"],
            "touch_count": e["touch_count"],
            "break_count": e["break_count"],
        })
    rows.sort(key=lambda r: r["ts_utc"])
    return {level_id: rows}


def test_level_state_at_before_any_event():
    """Before the first history event, should return initial state."""
    from src.backtest.replay import _level_state_at
    history = {42: [{"ts_utc": _ts(50), "strength": 1.5, "status": "active",
                     "touch_count": 1, "break_count": 0}]}
    s, status, tc, bc = _level_state_at(history, 42, _ts(30))
    assert s == 0.0 and status == "active" and tc == 0 and bc == 0


def test_level_state_at_returns_most_recent():
    """Should return the most recent event at or before the query timestamp."""
    from src.backtest.replay import _level_state_at
    events = [
        {"ts_utc": _ts(10), "strength": 0.0, "status": "active",   "touch_count": 0, "break_count": 0},
        {"ts_utc": _ts(50), "strength": 2.5, "status": "active",   "touch_count": 1, "break_count": 0},
        {"ts_utc": _ts(80), "strength": 0.5, "status": "flipped",  "touch_count": 1, "break_count": 1},
    ]
    history = {7: events}

    # At bar 60 → should see the bar-50 event (touch, strength 2.5)
    s, status, tc, bc = _level_state_at(history, 7, _ts(60))
    assert s == 2.5
    assert status == "active"
    assert tc == 1

    # At bar 90 → should see the bar-80 event (flipped)
    s, status, tc, bc = _level_state_at(history, 7, _ts(90))
    assert status == "flipped"
    assert s == 0.5


def test_level_state_at_unknown_level_returns_defaults():
    """Unknown level_id → initial defaults (strength=0, active)."""
    from src.backtest.replay import _level_state_at
    s, status, tc, bc = _level_state_at({}, 999, _ts(100))
    assert s == 0.0 and status == "active" and tc == 0 and bc == 0


# ── Test 6: _level_state_at returns stored event strength, not decayed ───────

def test_level_state_at_returns_event_strength_not_decayed():
    """Key regression: _level_state_at must return the strength stored at the
    most recent event BEFORE as_of_ts — it must NOT compute decay on-the-fly.

    Scenario: level touched at T=50 (strength=2.5), then flipped at T=80
    (strength=0.5). For a query at T=70, we should get 2.5 (the T=50 event),
    NOT a decayed version of 2.5 nor the T=80 value.
    """
    from src.backtest.replay import _level_state_at

    events = [
        {"ts_utc": _ts(10), "strength": 0.0,  "status": "active",  "touch_count": 0, "break_count": 0},
        {"ts_utc": _ts(50), "strength": 2.5,  "status": "active",  "touch_count": 1, "break_count": 0},
        {"ts_utc": _ts(80), "strength": 0.5,  "status": "flipped", "touch_count": 1, "break_count": 1},
        {"ts_utc": _ts(120),"strength": 0.05, "status": "expired", "touch_count": 1, "break_count": 1},
    ]
    history = {1: events}

    # At T=70 → should see the T=50 event, strength 2.5
    s, status, tc, bc = _level_state_at(history, 1, _ts(70))
    assert s == 2.5 and status == "active"

    # At T=90 → should see the T=80 event, strength 0.5 (flipped)
    s, status, tc, bc = _level_state_at(history, 1, _ts(90))
    assert s == 0.5 and status == "flipped"

    # At T=130 → expired
    s, status, tc, bc = _level_state_at(history, 1, _ts(130))
    assert status == "expired"


def test_touch_event_strength_is_exp_zero():
    """At the moment a touch is recorded (bar j), the touch contributes
    exp(-lam * (j - j)) = exp(0) = 1.0 to strength.
    Subsequent touches at the same bar also contribute 1.0 each.
    """
    candles = _build_candles()
    _, events = compute_levels(candles, as_of_ts=_ts(199), symbol="TEST", tf="M5",
                                record_history=True)
    touch_events_tc1 = [e for e in events if e["touch_count"] == 1]
    for ev in touch_events_tc1:
        # First touch contributes exp(0) = 1.0 exactly
        assert abs(ev["strength"] - 1.0) < 1e-9, (
            f"First touch should give strength=1.0, got {ev['strength']}"
        )
