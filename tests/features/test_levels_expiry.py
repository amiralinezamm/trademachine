"""Tests for levels expiry overhaul (Task 1) and compute_blackout (Task 2).

Uses spike-over-flat-baseline pattern for reliable swing detection.
expiry_distance_atr_mult=9999 isolates time/strength-based expiry.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.features.levels import compute_levels

UTC = timezone.utc
BAR = timedelta(minutes=5)


def _candle(ts, o, h, l, c):
    return {"ts_utc": ts, "open": o, "high": h, "low": l, "close": c}


def _flat(t0, n, price=100.0):
    return [_candle(t0 + i * BAR, price, price + 0.05, price - 0.05, price) for i in range(n)]


def _spike(ts, sh=150.0, base=100.0):
    return _candle(ts, base, sh, base - 0.5, base)


def _make_one_resistance(n_before=30, n_after=30, spike_high=150.0):
    """Return (candles, spike_ts, last_ts) with one clear swing high at spike_high."""
    t0 = datetime(2024, 1, 1, tzinfo=UTC)
    before = _flat(t0, n_before)
    spike_ts = t0 + n_before * BAR
    after_start = spike_ts + BAR
    after = _flat(after_start, n_after)
    candles = before + [_spike(spike_ts, spike_high)] + after
    return candles, spike_ts, candles[-1]["ts_utc"]


BASE_PARAMS = {
    "swing_n": 5,
    "swing_k": 1.0,
    "atr_period": 14,
    "zone_width_atr_mult": 0.25,
    "break_atr_mult": 0.5,
    "merge_atr_mult": 1.0,
    "strength_lambda": 0.01,
    "strength_expiry_threshold": 0.0,
    "expiry_distance_atr_mult": 9999,
    "max_break_count": 5,
    "extreme_max_days": 365,
    "normal_max_days": 30,
    "strength_ref_for_expiry": 2.0,
}

N = BASE_PARAMS["swing_n"]


# ---------------------------------------------------------------------------
# Test 1a: break_count=0 regression — 364 days quiet → still active
# ---------------------------------------------------------------------------

def test_break_count0_still_active_at_364_days():
    """Regression: a level never broken must not expire within extreme_max_days=365."""
    candles, spike_ts, _ = _make_one_resistance(n_before=30, n_after=N)
    last_ts = candles[-1]["ts_utc"]

    for i in range(364 * 24 * 12):
        last_ts += BAR
        candles.append(_candle(last_ts, 100.0, 100.05, 99.95, 100.0))

    result = compute_levels(candles, last_ts, "TEST", "M5", BASE_PARAMS)
    active = [l for l in result if l["status"] in ("active", "flipped")]
    assert len(active) >= 1, "Level should still be active at 364 days (break_count=0)"
    for lvl in active:
        assert lvl["break_count"] == 0


# ---------------------------------------------------------------------------
# Test 1b: break_count=0 with extreme_max_days=10 → expires at 11 days
# ---------------------------------------------------------------------------

def test_break_count0_expires_after_extreme_max_days():
    """Level with break_count=0 expires after extreme_max_days (10)."""
    params = {**BASE_PARAMS, "extreme_max_days": 10}
    candles, _, _ = _make_one_resistance(n_before=30, n_after=N)
    last_ts = candles[-1]["ts_utc"]

    for i in range(11 * 24 * 12):
        last_ts += BAR
        candles.append(_candle(last_ts, 100.0, 100.05, 99.95, 100.0))

    result = compute_levels(candles, last_ts, "TEST", "M5", params)
    active = [l for l in result if l["status"] in ("active", "flipped")]
    assert len(active) == 0, "Level should expire after extreme_max_days=10"


# ---------------------------------------------------------------------------
# Test 2a: first_break_ts is set after a break
# ---------------------------------------------------------------------------

def test_broken_level_has_first_break_ts():
    """After a break, first_break_ts must be populated.

    ATR is elevated from the spike (~3.7). Zone hi ≈ 150.46.
    Break threshold = hi + 0.5 * atr ≈ 152.3.
    We use close=160 to guarantee a break.
    """
    candles, _, _ = _make_one_resistance(n_before=30, n_after=N)
    last_ts = candles[-1]["ts_utc"]

    last_ts += BAR
    # close=160: well above any ATR-inflated break threshold (~152)
    candles.append(_candle(last_ts, 150.0, 162.0, 148.0, 160.0))

    result = compute_levels(candles, last_ts, "TEST", "M5", BASE_PARAMS)
    broken = [l for l in result if l["break_count"] >= 1]
    assert len(broken) >= 1, "Expected at least one broken level"
    for lvl in broken:
        assert lvl.get("first_break_ts") is not None, "first_break_ts must be set after break"
        assert lvl["first_break_ts"] <= last_ts


# ---------------------------------------------------------------------------
# Test 2b: broken level expires by strength decay
# ---------------------------------------------------------------------------

def test_broken_level_expires_by_strength_decay():
    """A broken level's strength decays; within effective_max_days it expires.

    With lam=0.01 and 2 days (576 bars) after the break:
      strength ≈ exp(-0.01 * 576) ≈ 0.003
      effective_max_days = min(5, 5 * 0.003 / 2.0) ≈ 0.008 days
      age_days = 2 >> 0.008 → expired
    """
    params = {**BASE_PARAMS, "normal_max_days": 5, "strength_ref_for_expiry": 2.0}
    candles, _, _ = _make_one_resistance(n_before=30, n_after=N)
    last_ts = candles[-1]["ts_utc"]

    # Break bar (close=160 >> threshold)
    last_ts += BAR
    candles.append(_candle(last_ts, 150.0, 162.0, 148.0, 160.0))
    break_ts = last_ts

    # 2 days of quiet above the (now flipped) zone — no further touches
    for _ in range(2 * 24 * 12):
        last_ts += BAR
        candles.append(_candle(last_ts, 165.0, 165.05, 164.95, 165.0))

    result = compute_levels(candles, last_ts, "TEST", "M5", params)
    broken_active = [
        l for l in result
        if l["break_count"] >= 1 and l["status"] in ("active", "flipped")
    ]
    assert len(broken_active) == 0, (
        f"Broken level with decayed strength should expire; still active: {broken_active}"
    )


# ---------------------------------------------------------------------------
# Test 3a: merge_atr_mult=1.0 merges zones within 1 ATR
# ---------------------------------------------------------------------------

def test_merge_1atr_combines_nearby_spikes():
    """Two spikes at 150.0 and 150.05 (< 1 ATR ≈ 3.7 apart) merge into one zone.

    After spike1, ATR ≈ 3.7. Zone1 = [149.54, 150.46].
    Spike2 at 150.05 inflates ATR further; zone2 overlaps zone1 → merge.
    """
    t0 = datetime(2024, 1, 1, tzinfo=UTC)
    before = _flat(t0, 30)
    spike1_ts = t0 + 30 * BAR
    mid = _flat(spike1_ts + BAR, 10)
    spike2_ts = spike1_ts + BAR + 10 * BAR
    after = _flat(spike2_ts + BAR, N + 1)
    candles = before + [_spike(spike1_ts, 150.0)] + mid + [_spike(spike2_ts, 150.05)] + after

    result = compute_levels(candles, candles[-1]["ts_utc"], "TEST", "M5", BASE_PARAMS)
    resistance = [l for l in result if l["kind"] == "resistance" and l["status"] in ("active", "flipped")]
    assert len(resistance) <= 1, (
        f"Nearby spikes should merge; got {len(resistance)} zones: "
        f"{[(l['price_low'], l['price_high']) for l in resistance]}"
    )


# ---------------------------------------------------------------------------
# Test 3b: merge_atr_mult=1.0 keeps far zones separate
# ---------------------------------------------------------------------------

def test_merge_1atr_keeps_faraway_spikes_separate():
    """Two spikes at 150.0 and 200.0 (50 apart >> any ATR) stay separate.

    Even with ATR ≈ 5.3 (elevated from spike2), gap = 48 >> 5.3 → no merge.
    """
    t0 = datetime(2024, 1, 1, tzinfo=UTC)
    before = _flat(t0, 30)
    spike1_ts = t0 + 30 * BAR
    mid = _flat(spike1_ts + BAR, 10)
    spike2_ts = spike1_ts + BAR + 10 * BAR
    after = _flat(spike2_ts + BAR, N + 1)
    candles = before + [_spike(spike1_ts, 150.0)] + mid + [_spike(spike2_ts, 200.0)] + after

    result = compute_levels(candles, candles[-1]["ts_utc"], "TEST", "M5", BASE_PARAMS)
    resistance = [l for l in result if l["kind"] == "resistance" and l["status"] in ("active", "flipped")]
    assert len(resistance) >= 2, (
        f"Expected ≥2 separate resistance zones (50 apart), got {len(resistance)}: "
        f"{[(l['price_low'], l['price_high']) for l in resistance]}"
    )


# ---------------------------------------------------------------------------
# Test 4: compute_blackout (pure function — no DB)
# ---------------------------------------------------------------------------

def test_blackout_suppresses_inside_window():
    """compute_blackout returns blackout=True inside a High-impact event window."""
    from src.news.blackout import compute_blackout

    event_ts = datetime(2024, 6, 5, 14, 0, tzinfo=UTC)
    as_of = event_ts - timedelta(minutes=5)   # 5 min before — inside 20-min window

    params = {
        "news": {
            "blackout": {
                "degree_1": {"before_min": 20, "after_min": 15},
                "degree_2": {"before_min": 15, "after_min": 10},
                "consensus": {"cluster_window_min": 30, "multiplier": 2},
            }
        }
    }
    result = compute_blackout(as_of, [{"title": "NFP", "impact": "High", "ts_utc": event_ts}], params)
    assert result["blackout"] is True


def test_blackout_clear_outside_window():
    """compute_blackout returns blackout=False when no event is near."""
    from src.news.blackout import compute_blackout

    event_ts = datetime(2024, 6, 5, 14, 0, tzinfo=UTC)
    as_of = event_ts - timedelta(hours=4)    # 4 hours away — outside any window

    result = compute_blackout(as_of, [{"title": "NFP", "impact": "High", "ts_utc": event_ts}])
    assert result["blackout"] is False
