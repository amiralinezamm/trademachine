"""Tests for SPEC.md 4.4 round_numbers module."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.features.round_numbers import compute_round_numbers, _candidate_levels

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

BAR = timedelta(minutes=5)
T0  = datetime(2026, 1, 2, 10, 0, tzinfo=timezone.utc)

# Small PARAMS for deterministic, ATR-independent tests.
# ATR period 3 lets us get valid ATR quickly.
# zone_min_width=5.0 means the zone is always [R-5, R+5] regardless of ATR
# (since 0.3 * small_ATR < 5 in quiet fixtures).
PARAMS = {
    "atr_period":        3,
    "volume_median_bars": 4,
    "zone_atr_mult":     0.3,
    "zone_min_width":    5.0,
    "break_atr_mult":    0.3,
    "multiples": {5: 0.3, 10: 0.4, 25: 0.6, 50: 0.8, 100: 1.0},
}

# Round number used in tests: 4300 (multiple of 100 → weight 1.0)
R = 4300.0
# Zone: [R-5, R+5] = [4295, 4305] (zone_min_width=5 > 0.3*small_ATR)


def _c(ts, open_, high, low, close, vol=100):
    return {
        "ts_utc": ts, "open": open_, "high": high,
        "low": low, "close": close, "tick_volume": vol,
    }


def _base_candles():
    """20 quiet candles far below R=4300 (close ~ 4250) to seed ATR and vol median."""
    candles = []
    for i in range(20):
        t = T0 + i * BAR
        candles.append(_c(t, 4250.0, 4255.0, 4245.0, 4250.0, vol=100))
    return candles


# ---------------------------------------------------------------------------
# 1. Anti-lookahead test (SPEC must-have, mirrors levels test)
# ---------------------------------------------------------------------------

def test_no_lookahead():
    """Candles AFTER as_of_ts must not affect the result."""
    candles = _base_candles()
    last_base = candles[-1]["ts_utc"]

    # Approach from below: close enters [4295, 4305]
    t_approach = last_base + BAR
    candles.append(_c(t_approach, 4290.0, 4302.0, 4288.0, 4300.0, vol=100))

    # Reversal bar (would create a REVERSAL event)
    t_reversal = t_approach + BAR
    candles.append(_c(t_reversal, 4300.0, 4302.0, 4288.0, 4290.0, vol=100))

    # as_of_ts is between approach and reversal
    as_of = t_approach + timedelta(seconds=1)

    hits_limited = compute_round_numbers(candles, as_of, "TEST", "M5", params=PARAMS)
    hits_full    = compute_round_numbers(candles, t_reversal + timedelta(seconds=1),
                                         "TEST", "M5", params=PARAMS)

    limited_states = {h["state"] for h in hits_limited if abs(h["level"] - R) < 0.1}
    full_states    = {h["state"] for h in hits_full    if abs(h["level"] - R) < 0.1}

    assert "REVERSAL" not in limited_states, "future REVERSAL leaked into truncated compute"
    assert "REVERSAL" in full_states, "REVERSAL not detected in full compute"


# ---------------------------------------------------------------------------
# 2. State machine: APPROACHING → REVERSAL
# ---------------------------------------------------------------------------

def test_approaching_then_reversal():
    """Price enters zone, then closes back below the lower zone edge → REVERSAL."""
    candles = _base_candles()
    last_ts = candles[-1]["ts_utc"]

    # Enter zone from below
    t1 = last_ts + BAR
    candles.append(_c(t1, 4290.0, 4305.0, 4289.0, 4300.0, vol=100))  # close = R = in zone

    # Back off below zone lower edge (< 4295)
    t2 = t1 + BAR
    candles.append(_c(t2, 4300.0, 4302.0, 4285.0, 4290.0, vol=100))  # close < 4295 → REVERSAL

    as_of = t2 + timedelta(seconds=1)
    hits = compute_round_numbers(candles, as_of, "TEST", "M5", params=PARAMS)

    near_R = [h for h in hits if abs(h["level"] - R) < 0.1]
    states = [h["state"] for h in near_R]

    assert "APPROACHING" in states
    assert "REVERSAL" in states
    assert "ACCELERATION" not in states


# ---------------------------------------------------------------------------
# 3. State machine: APPROACHING → ACCELERATION
# ---------------------------------------------------------------------------

def test_approaching_then_acceleration():
    """Price enters zone from below, then breaks through R+0.3*ATR with high volume."""
    candles = _base_candles()
    last_ts = candles[-1]["ts_utc"]

    # Enter zone
    t1 = last_ts + BAR
    candles.append(_c(t1, 4290.0, 4305.0, 4289.0, 4300.0, vol=100))

    # High-volume break: close well above R (4300 + 5+ = >4305, and >0.3*ATR beyond R)
    # ATR after 3 quiet bars + 1 approach bar ~ 8-10; break_dist = 0.3 * 8 ≈ 2.4
    # So close > 4302.4 and vol > median(100,100,100,100) = 100 → need vol > 100
    t2 = t1 + BAR
    candles.append(_c(t2, 4300.0, 4315.0, 4299.0, 4312.0, vol=200))  # high vol break

    as_of = t2 + timedelta(seconds=1)
    hits = compute_round_numbers(candles, as_of, "TEST", "M5", params=PARAMS)

    near_R = [h for h in hits if abs(h["level"] - R) < 0.1]
    states = [h["state"] for h in near_R]

    assert "APPROACHING" in states
    assert "ACCELERATION" in states
    assert "REVERSAL" not in states


# ---------------------------------------------------------------------------
# 4. Dynamic zone width (not a fixed dollar amount)
# ---------------------------------------------------------------------------

def test_dynamic_zone_width():
    """Zone width = max(zone_min_width, 0.3*ATR). In a high-volatility environment
    (large ATR > zone_min_width/zone_atr_mult), the zone must be wider than
    zone_min_width so that a close at distance < 0.3*ATR (but > zone_min_width)
    is still detected as APPROACHING.

    With 12 base candles (range=60) ATR converges to ~60.
    zone_width = max(5.0, 0.3*60) = max(5.0, 18) = 18   (dynamic, not fixed)
    Zone = [4282, 4318].
    close=4295 → distance from R=4300 is 5; 5 < 18 → in zone  (would FAIL with fixed 5)
    """
    candles = []
    t = T0
    # 12 high-volatility bars to bring ATR >> zone_min_width/zone_atr_mult (=5/0.3≈16.7)
    for i in range(12):
        candles.append(_c(t, 4200.0, 4230.0, 4170.0, 4200.0, vol=100))  # range=60
        t += BAR

    # Approach from below: close=4295, distance=5 from R=4300.
    # This is inside the dynamic zone (≤18) but NOT inside a fixed zone of 5.
    candles.append(_c(t, 4280.0, 4298.0, 4279.0, 4295.0, vol=100))
    t += BAR

    # Reversal: close=4278 < R-18=4282 → exits the dynamic zone on entry side
    candles.append(_c(t, 4295.0, 4296.0, 4271.0, 4278.0, vol=100))
    t += BAR

    as_of = t
    hits = compute_round_numbers(candles, as_of, "TEST", "M5", params=PARAMS)
    near_R = [h for h in hits if abs(h["level"] - R) < 0.1]
    states = {h["state"] for h in near_R}
    assert "APPROACHING" in states, (
        "close=4295 (distance 5 from R=4300) must be inside dynamic zone (~18) "
        "even though it exceeds fixed zone_min_width=5.0"
    )
    assert "REVERSAL" in states, "close=4278 must trigger REVERSAL (below R-zone_width)"


# ---------------------------------------------------------------------------
# 5. Weight hierarchy correctness
# ---------------------------------------------------------------------------

def test_weight_hierarchy():
    """4300 → multiple of 100 → weight 1.0.  4250 → multiple of 50 → weight 0.8.
    4275 → multiple of 25 → weight 0.6.  4310 → multiple of 10 → weight 0.4.
    4305 → multiple of 5 → weight 0.3."""
    multiples = {5: 0.3, 10: 0.4, 25: 0.6, 50: 0.8, 100: 1.0}
    levels = _candidate_levels(4295.0, 4315.0, multiples, margin=0)
    by_level = {int(l["level"]): l for l in levels}

    assert by_level[4300]["weight"] == 1.0
    assert by_level[4300]["multiplier"] == 100
    assert by_level[4310]["weight"] == 0.4
    assert by_level[4310]["multiplier"] == 10
    assert by_level[4305]["weight"] == 0.3
    assert by_level[4305]["multiplier"] == 5


# ---------------------------------------------------------------------------
# 6. Low-volume break does NOT produce ACCELERATION
# ---------------------------------------------------------------------------

def test_low_volume_break_not_acceleration():
    """A break through the zone without sufficient volume ends the approach
    silently — no ACCELERATION event."""
    candles = _base_candles()
    last_ts = candles[-1]["ts_utc"]

    t1 = last_ts + BAR
    candles.append(_c(t1, 4290.0, 4305.0, 4289.0, 4300.0, vol=100))

    # Break above zone but with LOW volume (= median, not > median)
    t2 = t1 + BAR
    candles.append(_c(t2, 4300.0, 4315.0, 4299.0, 4312.0, vol=50))  # vol < 100 = median

    as_of = t2 + timedelta(seconds=1)
    hits = compute_round_numbers(candles, as_of, "TEST", "M5", params=PARAMS)
    near_R = [h for h in hits if abs(h["level"] - R) < 0.1]
    states = {h["state"] for h in near_R}

    assert "ACCELERATION" not in states, "Low-volume break must NOT be ACCELERATION"
