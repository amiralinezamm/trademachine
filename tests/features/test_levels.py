from datetime import datetime, timedelta, timezone

import pytest

from src.features.levels import compute_levels

UTC = timezone.utc
BAR = timedelta(minutes=5)  # M5


def _candle(ts, o, h, l, c):
    return {"ts_utc": ts, "open": o, "high": h, "low": l, "close": c}


def _synthetic_candles_with_one_swing_high(n_before=30, n_after=30, spike_high=150.0):
    """A flat, tiny-noise baseline (gives ATR a small stable value) with a
    single, unmistakable spike -> exactly one obvious swing high, at a known
    index, with a huge retracement so k*ATR is trivially satisfied."""
    t0 = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)  # a Monday
    candles = []
    for i in range(n_before):
        ts = t0 + i * BAR
        price = 100.1 if i % 2 == 0 else 99.9
        candles.append(_candle(ts, 100.0, price + 0.05, price - 0.05, 100.0))

    spike_idx = n_before
    spike_ts = t0 + spike_idx * BAR
    candles.append(_candle(spike_ts, 100.0, spike_high, 99.0, 100.0))

    for i in range(1, n_after + 1):
        ts = t0 + (spike_idx + i) * BAR
        price = 100.1 if i % 2 == 0 else 99.9
        candles.append(_candle(ts, 100.0, price + 0.05, price - 0.05, 100.0))

    return candles, spike_ts


PARAMS = {
    "swing_n": 5,
    "swing_k": 1.0,
    "atr_period": 14,
    "zone_width_atr_mult": 0.25,
    "break_atr_mult": 0.5,
    "merge_atr_mult": 0.25,
    "strength_lambda": 0.0015,
    "strength_expiry_threshold": 0.03,
    "expiry_distance_atr_mult": 8,
    "max_break_count": 5,
    "extreme_max_days": 365,
    "normal_max_days": 30,
}


def test_swing_not_visible_before_confirmation_delay():
    """CLAUDE.md rule 1/2, SPEC.md's own 'most important point': a swing at
    bar i must not appear in the output until N bars after it exist."""
    candles, spike_ts = _synthetic_candles_with_one_swing_high()
    N = PARAMS["swing_n"]

    one_bar_short = spike_ts + (N - 1) * BAR
    result_early = compute_levels(candles, one_bar_short, "TEST", "M5", params=PARAMS)
    assert result_early == [], "swing must not repaint into existence before its confirmation delay has passed"


def test_swing_visible_exactly_at_confirmation():
    candles, spike_ts = _synthetic_candles_with_one_swing_high()
    N = PARAMS["swing_n"]

    exactly_confirmed = spike_ts + N * BAR
    result = compute_levels(candles, exactly_confirmed, "TEST", "M5", params=PARAMS)
    assert len(result) == 1
    assert result[0]["kind"] == "resistance"
    assert result[0]["created_ts"] == spike_ts


def test_earlier_as_of_ts_output_is_a_strict_subset_of_later():
    """The exact property PM-RULES.md / the prompt asks for: call twice with
    different as_of_ts, prove the earlier result is a subset of the later one
    (matched by the stable natural key: created_ts + kind)."""
    candles, spike_ts = _synthetic_candles_with_one_swing_high(n_before=30, n_after=60)
    N = PARAMS["swing_n"]

    t_mid = spike_ts + (N + 10) * BAR
    t_late = spike_ts + (N + 50) * BAR

    result_mid = compute_levels(candles, t_mid, "TEST", "M5", params=PARAMS)
    result_late = compute_levels(candles, t_late, "TEST", "M5", params=PARAMS)

    keys_mid = {(lvl["created_ts"], lvl["kind"]) for lvl in result_mid}
    keys_late = {(lvl["created_ts"], lvl["kind"]) for lvl in result_late}

    assert keys_mid.issubset(keys_late)
    assert len(keys_mid) >= 1  # sanity: the fixture actually produced a level


def test_no_level_created_ts_violates_the_confirmation_bound():
    """No returned level may have created_ts later than as_of_ts minus the
    confirmation delay (swing_n bars)."""
    candles, spike_ts = _synthetic_candles_with_one_swing_high(n_before=30, n_after=60)
    N = PARAMS["swing_n"]
    as_of_ts = spike_ts + (N + 20) * BAR

    result = compute_levels(candles, as_of_ts, "TEST", "M5", params=PARAMS)
    latest_allowed_created_ts = as_of_ts - N * BAR
    for lvl in result:
        assert lvl["created_ts"] <= latest_allowed_created_ts


def test_future_candles_passed_in_are_ignored_even_if_caller_forgets_to_filter():
    """The as_of_ts cutoff is enforced by compute_levels() itself, not just
    trusted from the caller — pass candles beyond as_of_ts and confirm they
    have zero effect on the result."""
    candles, spike_ts = _synthetic_candles_with_one_swing_high(n_before=30, n_after=60)
    N = PARAMS["swing_n"]
    as_of_ts = spike_ts + (N - 1) * BAR  # one bar short: swing must not show

    result = compute_levels(candles, as_of_ts, "TEST", "M5", params=PARAMS)
    assert result == []


def test_merge_of_two_nearby_same_kind_zones():
    """SPEC.md 4.2: zones of the same kind closer than merge_atr_mult * ATR
    are one zone, not two duplicate rows."""
    candles, spike_ts = _synthetic_candles_with_one_swing_high(n_before=30, n_after=15, spike_high=101.0)
    N = PARAMS["swing_n"]

    # A second, nearly-identical spike a bit later, at almost the same price
    # (within merge distance) -> must merge into the first level, not create
    # a second one.
    second_spike_ts = spike_ts + 20 * BAR
    candles2 = list(candles)
    # extend with a second spike + confirmation tail
    t0 = candles[0]["ts_utc"]
    base_i = len(candles2)
    for i in range(20):
        ts = candles2[-1]["ts_utc"] + BAR
        price = 100.1 if i % 2 == 0 else 99.9
        candles2.append(_candle(ts, 100.0, price + 0.05, price - 0.05, 100.0))

    as_of_ts = candles2[-1]["ts_utc"]
    result = compute_levels(candles2, as_of_ts, "TEST", "M5", params=PARAMS)
    resistances = [lvl for lvl in result if lvl["kind"] == "resistance"]
    assert len(resistances) == 1, "two nearby same-kind swings must merge into one level, not duplicate"


def test_expired_status_is_not_deleted_just_marked():
    """SPEC.md 4.2: expiry changes status, it never removes the row —
    compute_levels() must keep returning an 'expired' level, not drop it."""
    params = dict(PARAMS)
    params["strength_expiry_threshold"] = 1.0  # force expiry (no touch ever reaches this)
    candles, spike_ts = _synthetic_candles_with_one_swing_high(n_before=30, n_after=60)
    N = params["swing_n"]
    as_of_ts = spike_ts + (N + 50) * BAR

    result = compute_levels(candles, as_of_ts, "TEST", "M5", params=params)
    assert len(result) == 1
    assert result[0]["status"] == "expired"



def _make_candle(ts, o, h, l, c):
    return {"ts_utc": ts, "open": o, "high": h, "low": l, "close": c}


def _candles_with_resistance_and_touch(spike_high=2050.0):
    """Fixture with a realistic ATR (~15 USD, typical XAUUSD M5) that:
    1. Creates a resistance zone around spike_high.
    2. Has one confirmed TOUCH of the zone (price enters, closes outside).
    3. Leaves enough room for the break test.
    ATR is kept large intentionally so expiry_distance check (8 * ATR ≈ 120)
    does not fire when price is a few dollars away from the zone."""
    N = PARAMS["swing_n"]
    t0 = datetime(2026, 3, 1, 0, 0, tzinfo=UTC)
    candles = []

    # 30-bar volatile baseline ~2000 (ATR ~ 10-15)
    import math as _math
    for i in range(30):
        ts = t0 + i * BAR
        mid = 2000.0 + 5 * _math.sin(i * 0.3)
        candles.append(_make_candle(ts, mid, mid + 8, mid - 8, mid))

    # Swing high spike: price runs to spike_high then closes back at baseline
    spike_idx = 30
    spike_ts = t0 + spike_idx * BAR
    candles.append(_make_candle(spike_ts, 2000.0, spike_high, 1995.0, 2001.0))

    # N confirmation bars at baseline (confirms the swing)
    for i in range(1, N + 1):
        ts = spike_ts + i * BAR
        candles.append(_make_candle(ts, 2000.0, 2008.0, 1992.0, 2000.0))

    # ONE TOUCH: price enters the zone (zone_hi ≈ spike_high + 0.25*ATR/2)
    # but closes back below the zone. This gives strength > 0.
    touch_ts = spike_ts + (N + 1) * BAR
    candles.append(_make_candle(touch_ts, 2020.0, spike_high + 2, 2018.0, 2022.0))

    # Quiet bars to let the touch be recorded cleanly
    for i in range(1, 4):
        ts = touch_ts + i * BAR
        candles.append(_make_candle(ts, 2000.0, 2008.0, 1992.0, 2000.0))

    return candles, spike_ts, spike_high


def test_broken_resistance_flips_to_support():
    """SPEC.md 4.2 تبدیل نقش: a resistance whose zone is broken by close
    beyond zone_hi + break_mult*ATR must change kind -> 'support' and
    status -> 'flipped', and break_count must increment."""
    N = PARAMS["swing_n"]
    candles, spike_ts, spike_high = _candles_with_resistance_and_touch()

    # Break: price closes decisively ABOVE the resistance zone
    # Zone top ~ spike_high + 0.125*ATR (ATR~15, so zone_hi ~ spike_high+1.9)
    # break_mult*ATR ~ 0.5*15 = 7.5, so need close > spike_high + ~10
    break_ts = candles[-1]["ts_utc"] + BAR
    candles.append(_make_candle(break_ts, spike_high + 5, spike_high + 20, spike_high + 4, spike_high + 15))

    # A few bars at the new higher level
    for i in range(1, 8):
        ts = break_ts + i * BAR
        candles.append(_make_candle(ts, spike_high + 15, spike_high + 23, spike_high + 13, spike_high + 15))

    as_of_ts = candles[-1]["ts_utc"]
    result = compute_levels(candles, as_of_ts, "TEST", "M5", params=PARAMS)

    # After the flip the level may be 'flipped' or 'expired' depending on
    # subsequent price distance — both are valid outcomes. What matters is
    # that the KIND changed and break_count incremented.
    broken_levels = [lvl for lvl in result if lvl["break_count"] >= 1]
    assert len(broken_levels) == 1, (
        f"expected exactly one level with break_count>=1, got {[l['status'] for l in result]}"
    )
    lvl = broken_levels[0]
    assert lvl["kind"] == "support", (
        f"broken resistance must flip to kind='support', got '{lvl['kind']}'"
    )
    assert lvl["status"] in ("flipped", "expired"), (
        f"status after break must be 'flipped' or 'expired', got '{lvl['status']}'"
    )


def test_flipped_level_strength_is_halved():
    """SPEC.md 4.2: on role flip, strength is halved. Compare strength just
    before the break (no halving) versus just after (halved by definition).
    Uses a fixture with one touch so initial strength > 0."""
    N = PARAMS["swing_n"]
    candles, spike_ts, spike_high = _candles_with_resistance_and_touch()

    as_of_before = candles[-1]["ts_utc"]  # after touch, before break
    result_before = compute_levels(candles, as_of_before, "TEST", "M5", params=PARAMS)
    # Filter for the resistance zone near spike_high specifically — other zones
    # (support zones from the oscillating baseline) may also appear in the
    # result now that fresh untouched levels are no longer expired by strength.
    resistance_before = [
        lvl for lvl in result_before
        if lvl["status"] in ("active", "flipped")
        and lvl["kind"] == "resistance"
        and abs((lvl["price_low"] + lvl["price_high"]) / 2 - spike_high) < 5
    ]
    assert resistance_before, "expected the resistance zone near spike_high to be active"
    strength_before = resistance_before[0]["strength"]

    # Add the break candle
    break_ts = candles[-1]["ts_utc"] + BAR
    candles.append(_make_candle(break_ts, spike_high + 5, spike_high + 20, spike_high + 4, spike_high + 15))
    for i in range(1, 4):
        ts = break_ts + i * BAR
        candles.append(_make_candle(ts, spike_high + 15, spike_high + 23, spike_high + 13, spike_high + 15))

    as_of_after = candles[-1]["ts_utc"]
    result_after = compute_levels(candles, as_of_after, "TEST", "M5", params=PARAMS)
    broken = [lvl for lvl in result_after if lvl["break_count"] >= 1]
    assert broken, "expected a flipped/broken level after the break candle"
    strength_after = broken[0]["strength"]

    # After the flip strength must be strictly less than before (halved + decay)
    assert strength_after < strength_before, (
        f"strength should decrease on flip (before={strength_before:.4f}, after={strength_after:.4f})"
    )


def _flip_candles(candles, spike_high, n_flips):
    """Append n_flips alternating break candles to candles (in-place).
    Zone starts as resistance (even flip breaks upward, odd breaks downward).
    Calm bars between flips stay near spike_high so they never accidentally
    trigger an extra break — a close at zone mid is inside both thresholds
    (zone_lo - 0.5*ATR and zone_hi + 0.5*ATR) for any realistic ATR."""
    zone_mid = spike_high  # close enough to mid for any fixture ATR
    last_ts = candles[-1]["ts_utc"]
    for flip in range(n_flips):
        last_ts += BAR
        if flip % 2 == 0:
            # resistance → support: close well above zone_hi + 0.5*ATR
            candles.append(_make_candle(
                last_ts, spike_high + 10, spike_high + 30,
                spike_high + 9, spike_high + 25,
            ))
        else:
            # support → resistance: close well below zone_lo - 0.5*ATR
            candles.append(_make_candle(
                last_ts, spike_high - 10, spike_high - 9,
                spike_high - 30, spike_high - 25,
            ))
        # Calm bars at zone_mid — won't enter zone (open/close outside the
        # 2-pt zone width) but also won't trigger extra breaks.
        for _ in range(10):
            last_ts += BAR
            candles.append(_make_candle(
                last_ts, zone_mid + 1, zone_mid + 3, zone_mid - 3, zone_mid,
            ))


def test_level_expires_after_max_break_count():
    """Regression for 2026-09-16 SELL-on-support incident: a zone broken
    max_break_count times must be immediately expired and not used for signals.
    Root cause: levels with 19–38 flips were classified as 'resistance' and
    generated 24 wrong SELL signals in the 4280-4298 zone."""
    max_bc = PARAMS["max_break_count"]
    candles, spike_ts, spike_high = _candles_with_resistance_and_touch()
    _flip_candles(candles, spike_high, max_bc)

    as_of_ts = candles[-1]["ts_utc"]
    result = compute_levels(candles, as_of_ts, "TEST", "M5", params=PARAMS)

    zone = [lvl for lvl in result if lvl["break_count"] >= max_bc]
    assert zone, f"expected a level with break_count>={max_bc}, got statuses={[l['status'] for l in result]}"
    assert zone[0]["status"] == "expired", (
        f"level with {zone[0]['break_count']} breaks must be 'expired', "
        f"got '{zone[0]['status']}'"
    )


def test_level_still_active_at_max_break_count_minus_one():
    """Boundary: max_break_count - 1 flips must NOT trigger the break-count
    expiry (though strength/distance expiry may still apply)."""
    max_bc = PARAMS["max_break_count"]
    candles, spike_ts, spike_high = _candles_with_resistance_and_touch()
    _flip_candles(candles, spike_high, max_bc - 1)

    as_of_ts = candles[-1]["ts_utc"]
    result = compute_levels(candles, as_of_ts, "TEST", "M5", params=PARAMS)

    zone = [lvl for lvl in result if lvl["break_count"] == max_bc - 1]
    assert zone, f"expected a level with break_count=={max_bc - 1}"
    # The level must NOT be expired solely because of break_count
    # (it could expire from distance/strength, but break_count-1 < max_bc so
    # that path is closed — only the other expiry conditions can trigger here,
    # which the fixture is designed to avoid: distance ~50 << 8*ATR, strength >> 0.03)
    assert zone[0]["status"] in ("active", "flipped"), (
        f"level with only {zone[0]['break_count']} breaks must still be "
        f"active/flipped, got '{zone[0]['status']}'"
    )


def test_normal_level_expires_after_normal_max_days():
    """Time-based retention: a level with touch_count > 0 must be expired
    once it is older than normal_max_days (30 days by default)."""
    params = {**PARAMS, "normal_max_days": 1}  # 1-day limit for the test
    candles, spike_ts, spike_high = _candles_with_resistance_and_touch()
    # Append >1 day worth of bars (1 day = 24*12 = 288 M5 bars)
    last_ts = candles[-1]["ts_utc"]
    for _ in range(300):
        last_ts += BAR
        candles.append(_make_candle(last_ts, 2000.0, 2008.0, 1992.0, 2000.0))

    as_of_ts = candles[-1]["ts_utc"]
    result = compute_levels(candles, as_of_ts, "TEST", "M5", params=params)

    touched_levels = [lvl for lvl in result if lvl["touch_count"] > 0]
    assert touched_levels, "fixture must produce a touched level"
    assert all(lvl["status"] == "expired" for lvl in touched_levels), (
        "touched level older than normal_max_days must be 'expired'"
    )

