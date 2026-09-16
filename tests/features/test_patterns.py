"""Tests for src/features/patterns.py — SPEC.md 4.3.

Focus: anti-lookahead guarantee and basic correctness. The subset property
(earlier as_of_ts gives a subset of later) is the canonical anti-lookahead
test mandated by CLAUDE.md and the overnight-work brief.
"""
from datetime import datetime, timedelta, timezone

import pytest

from src.features.patterns import compute_patterns

UTC = timezone.utc
BAR = timedelta(minutes=5)

# Minimal params that match the structure of config/params.yaml[patterns]
PARAMS = {
    "atr_period": 14,
    "at_level_distance_atr_mult": 0.5,
}


def _candle(ts, o, h, l, c):
    return {"ts_utc": ts, "open": o, "high": h, "low": l, "close": c}


def _realistic_candles(n: int = 200, start_price: float = 2000.0):
    """200 candles with realistic XAUUSD-like OHLC values so TA-Lib
    can produce meaningful ATR and pattern signals."""
    import math
    t0 = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)
    candles = []
    price = start_price
    for i in range(n):
        ts = t0 + i * BAR
        # Mild trending + noise
        price += 0.3 * math.sin(i * 0.07) + (0.5 if i % 7 == 0 else -0.3)
        o = round(price, 3)
        h = round(price + abs(2.0 * math.sin(i * 0.13)) + 1.0, 3)
        l = round(price - abs(2.0 * math.sin(i * 0.17)) - 1.0, 3)
        c = round(price + 0.2 * math.sin(i * 0.11), 3)
        candles.append(_candle(ts, o, h, l, c))
    return candles, t0


def test_no_patterns_returned_when_too_few_candles():
    """With fewer candles than atr_period + 2, compute_patterns must
    return an empty list rather than crash or return junk."""
    candles = [
        _candle(datetime(2026, 1, 1, tzinfo=UTC) + i * BAR, 2000, 2005, 1995, 2001)
        for i in range(5)
    ]
    result = compute_patterns(candles, candles[-1]["ts_utc"], "XAUUSD@", "M5", params=PARAMS)
    assert result == []


def test_future_candles_are_ignored():
    """compute_patterns() must drop candles with ts_utc > as_of_ts even
    when they are present in the input list."""
    candles, t0 = _realistic_candles(n=100)
    as_of_ts = t0 + 50 * BAR  # halfway

    result_with_future = compute_patterns(candles, as_of_ts, "XAUUSD@", "M5", params=PARAMS)
    # Every pattern's ts must be <= as_of_ts
    for hit in result_with_future:
        assert hit["ts_utc"] <= as_of_ts, (
            f"hit at {hit['ts_utc']} > as_of_ts {as_of_ts} — lookahead leak"
        )


def test_earlier_output_is_subset_of_later():
    """The canonical anti-lookahead test: call with two different as_of_ts
    values and assert the earlier set is a strict subset of the later set,
    identified by (ts_utc, pattern)."""
    candles, t0 = _realistic_candles(n=200)
    t_mid = t0 + 100 * BAR
    t_late = t0 + 190 * BAR

    hits_mid = compute_patterns(candles, t_mid, "XAUUSD@", "M5", params=PARAMS)
    hits_late = compute_patterns(candles, t_late, "XAUUSD@", "M5", params=PARAMS)

    keys_mid = {(h["ts_utc"], h["pattern"]) for h in hits_mid}
    keys_late = {(h["ts_utc"], h["pattern"]) for h in hits_late}

    assert keys_mid.issubset(keys_late), (
        f"Earlier output is NOT a subset of later output. "
        f"Patterns in mid but not in late: {keys_mid - keys_late}"
    )


def test_all_directions_are_plus_or_minus_100():
    """TA-Lib returns exactly +100 or -100 for confirmed patterns (0 = no
    pattern, which we already filter out). Any other value is a bug."""
    candles, t0 = _realistic_candles(n=200)
    hits = compute_patterns(candles, candles[-1]["ts_utc"], "XAUUSD@", "M5", params=PARAMS)
    bad = [h for h in hits if h["direction"] not in (100, -100)]
    assert not bad, f"unexpected direction values: {set(h['direction'] for h in bad)}"


def test_body_atr_is_non_negative():
    """body_atr = |close-open| / ATR — must always be >= 0."""
    candles, _ = _realistic_candles(n=200)
    hits = compute_patterns(candles, candles[-1]["ts_utc"], "XAUUSD@", "M5", params=PARAMS)
    bad = [h for h in hits if h["body_atr"] < 0]
    assert not bad, "body_atr must be non-negative"


def test_pattern_names_are_all_cdl_prefixed():
    """All returned pattern names must start with 'CDL' (TA-Lib convention)."""
    candles, _ = _realistic_candles(n=200)
    hits = compute_patterns(candles, candles[-1]["ts_utc"], "XAUUSD@", "M5", params=PARAMS)
    bad = [h["pattern"] for h in hits if not h["pattern"].startswith("CDL")]
    assert not bad, f"non-CDL pattern names: {bad}"


def test_at_level_linkage_when_level_present():
    """When a level zone is passed that encompasses a pattern candle's price,
    that hit should get a non-None at_level_id and level_strength."""
    candles, t0 = _realistic_candles(n=200)
    # Synthesise a level whose zone covers the mid-range of the candles
    price_mid = 2000.0
    fake_level = {
        "id": 9999,
        "kind": "support",
        "price_low": price_mid - 20,
        "price_high": price_mid + 20,
        "strength": 2.5,
        "status": "active",
    }
    hits = compute_patterns(
        candles, candles[-1]["ts_utc"], "XAUUSD@", "M5",
        levels=[fake_level], params=PARAMS
    )
    # At least some hits should link to the level (candle mids will often
    # fall in the 1980-2020 range where the fake level sits)
    linked = [h for h in hits if h["at_level_id"] == 9999]
    assert linked, "expected at least one pattern hit to link to the fake level"
    for h in linked:
        assert h["level_strength"] == 2.5


def test_count_per_pattern_reported():
    """Acceptance criterion from SPEC.md 4.3: patterns with < 50 hits over
    the full history should be identifiable. We verify the machinery by
    counting occurrences per pattern name — this is a structure test, not
    a threshold assertion (we only have 200 synthetic candles here)."""
    candles, _ = _realistic_candles(n=200)
    hits = compute_patterns(candles, candles[-1]["ts_utc"], "XAUUSD@", "M5", params=PARAMS)
    from collections import Counter
    counts = Counter(h["pattern"] for h in hits)
    # At least some patterns should have been detected in 200 candles
    assert len(counts) > 0, "no patterns detected at all in 200 candles"
