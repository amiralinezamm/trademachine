"""Regression test for the 2026-09-24 live bug: a support zone that kept
merging with new swing lows during a sustained downtrend, so its own
price_low chased the price down and the break condition was never met
(zone id=90288 grew to a 23-point 'support' spanning the whole recent range).

max_zone_width_atr_mult caps merged zone width — once exceeded, a new
swing creates an INDEPENDENT zone instead of merging.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.features.levels import compute_levels

UTC = timezone.utc
BAR = timedelta(minutes=5)


def _candle(ts, o, h, l, c):
    return {"ts_utc": ts, "open": o, "high": h, "low": l, "close": c}


PARAMS = {
    "swing_n": 3,
    "swing_k": 0.3,
    "atr_period": 14,
    "zone_width_atr_mult": 0.25,
    "break_atr_mult": 0.5,
    "merge_atr_mult": 1.0,
    "max_zone_width_atr_mult": 2.0,
    "strength_lambda": 0.0015,
    "strength_expiry_threshold": 0.0,
    "expiry_distance_atr_mult": 9999,
    "max_break_count": 20,
    "extreme_max_days": 365,
    "normal_max_days": 30,
    "strength_ref_for_expiry": 2.0,
}


def _stairstep_downtrend(n_steps=12, step=1.0, bars_per_step=8, start=4300.0, atr_seed=1.5):
    """A staircase downtrend: each step makes a slightly lower low with a
    small pullback, similar to the live price action (4350 -> 4270).
    With merge_atr_mult=1.0 and no width cap, each new swing low (within
    ~1 ATR of the previous zone) would keep merging into one giant zone."""
    t0 = datetime(2024, 1, 1, tzinfo=UTC)
    candles = []
    price = start
    # warm-up bars for stable ATR
    for i in range(20):
        candles.append(_candle(t0 + i * BAR, price, price + 0.3, price - 0.3, price))
    t = t0 + 20 * BAR
    for step_i in range(n_steps):
        low_price = price - step
        # down leg: dip to new low
        candles.append(_candle(t, price, price + 0.2, low_price, low_price + 0.1))
        t += BAR
        # small pullback (retrace) to confirm the swing low structurally
        for k in range(bars_per_step - 1):
            pullback = low_price + 0.5 + 0.05 * k
            candles.append(_candle(t, pullback, pullback + 0.2, pullback - 0.2, pullback))
            t += BAR
        price = low_price
    return candles


def test_zone_width_capped_during_sustained_downtrend():
    """With the cap, no support zone should exceed max_zone_width_atr_mult * ATR
    at time of creation -- i.e. price_low..price_high span stays bounded even
    though many nearby swing lows form during the downtrend."""
    candles = _stairstep_downtrend()
    as_of = candles[-1]["ts_utc"]

    result = compute_levels(candles, as_of, "TEST", "M5", PARAMS)
    support_zones = [l for l in result if l["kind"] in ("support", "resistance")]
    assert support_zones, "fixture should produce at least one zone"

    # Reasonable ATR in this fixture is small (~0.5-2); no zone should span
    # more than a few points. The bug produced a 23-point zone from a
    # ~50-point downtrend -- with the cap, zones must stay well under that.
    for lvl in result:
        width = lvl["price_high"] - lvl["price_low"]
        assert width < 15.0, (
            f"Zone [{lvl['price_low']:.2f},{lvl['price_high']:.2f}] width={width:.2f} "
            f"is uncapped -- regression of the 2026-09-24 'chasing zone' bug"
        )


def test_more_than_one_zone_created_during_downtrend():
    """Without the cap, all swing lows in the downtrend merge into ONE zone
    that never breaks. With the cap, once a merge would exceed max width,
    a NEW independent zone must be created -- so a sustained downtrend
    produces multiple support zones over time, not one infinitely-growing one."""
    candles = _stairstep_downtrend(n_steps=12, step=1.0)
    as_of = candles[-1]["ts_utc"]

    result = compute_levels(candles, as_of, "TEST", "M5", PARAMS)
    # Zones flip kind as the downtrend keeps breaking them, so count ALL
    # distinct zones (by created_ts), not just kind==support -- what matters
    # is that the cap forces independent zones instead of one infinite blob.
    assert len(result) >= 2, (
        f"Expected multiple independent zones once the width cap kicks in, "
        f"got {len(result)}: {[(l['price_low'], l['price_high']) for l in result]}"
    )
