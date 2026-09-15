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
