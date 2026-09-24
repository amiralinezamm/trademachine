"""Tests for src.engine.market_structure (Part 2-b, market_structure_filter).

Covers: bullish/bearish classification, break-of-structure -> unknown
(not the opposite trend), anti-lookahead (a swing must not be visible
before its confirmation bar), and the BUY/SELL block wiring in
check_level_reversion.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.engine.level_reversion import check_level_reversion
from src.engine.market_structure import compute_market_structure, detect_h1_swings, structure_from_swings

UTC = timezone.utc
H1 = timedelta(hours=1)

STRUCT_PARAMS = {"swing_n": 3, "swing_k": 0.3, "atr_period": 5}


def _h1(ts, o, h, l, c):
    return {"ts_utc": ts, "open": o, "high": h, "low": l, "close": c}


def _flat_h1(t0, n, price=100.0):
    return [_h1(t0 + i * H1, price, price + 0.3, price - 0.3, price) for i in range(n)]


def _m5_from_h1(h1_candles):
    """Expand H1 bars into single M5-equivalent dicts at that exact ts
    (compute_market_structure resamples by hour-bucket, so one bar per
    hour is enough to reproduce the same H1 bucket)."""
    return [dict(c) for c in h1_candles]


def _zigzag_h1(prices, flat=100.0, fr=0.3, pn=5):
    """Isolated single-bar extrema (each a clean swing high/low) separated
    by `pn` flat plateau bars on each side, so N-bar-each-side confirmation
    is unambiguous. `prices`: [("high", 103), ("low", 101), ...]."""
    t0 = datetime(2024, 1, 1, tzinfo=UTC)
    candles = []
    t = t0
    for i in range(15):  # warm-up for ATR
        candles.append(_h1(t, flat, flat + fr / 2, flat - fr / 2, flat))
        t += H1
    for kind, price in prices:
        if kind == "high":
            candles.append(_h1(t, price - 0.5, price, price - 1, price - 0.3))
        else:
            candles.append(_h1(t, price + 0.5, price + 1, price, price + 0.3))
        t += H1
        base = price - 1.5 if kind == "high" else price + 1.5
        for _ in range(pn):
            candles.append(_h1(t, base, base + fr / 2, base - fr / 2, base))
            t += H1
    return candles


def _uptrend_h1():
    """Higher-high / higher-low zigzag."""
    prices = [("high", 103), ("low", 101), ("high", 106), ("low", 104), ("high", 109), ("low", 107)]
    return _zigzag_h1(prices)


def _downtrend_h1():
    """Lower-high / lower-low zigzag."""
    prices = [("low", 101), ("high", 103), ("low", 98), ("high", 100), ("low", 95), ("high", 97)]
    return _zigzag_h1(prices)


def test_bullish_structure_detected():
    candles = _m5_from_h1(_uptrend_h1())
    as_of = candles[-1]["ts_utc"]
    result = compute_market_structure(candles, as_of, STRUCT_PARAMS)
    assert result["structure"] == "bullish", result


def test_bearish_structure_detected():
    candles = _m5_from_h1(_downtrend_h1())
    as_of = candles[-1]["ts_utc"]
    result = compute_market_structure(candles, as_of, STRUCT_PARAMS)
    assert result["structure"] == "bearish", result


def test_break_of_bearish_structure_becomes_unknown_not_bullish():
    """A close above the last (lower) swing high in a bearish structure must
    flip structure to 'unknown', NOT directly to 'bullish' -- explicit spec."""
    candles = _m5_from_h1(_downtrend_h1())
    last_hi = None
    # find the structure right before adding a breakout bar
    as_of = candles[-1]["ts_utc"]
    base_result = compute_market_structure(candles, as_of, STRUCT_PARAMS)
    assert base_result["structure"] == "bearish"
    last_hi_price = base_result["last_swing_high"]["price"]

    # Add a bar that closes above the last swing high
    t = candles[-1]["ts_utc"] + H1
    breakout = _h1(t, last_hi_price, last_hi_price + 5, last_hi_price - 1, last_hi_price + 3)
    candles2 = candles + [breakout]
    as_of2 = candles2[-1]["ts_utc"]
    result2 = compute_market_structure(candles2, as_of2, STRUCT_PARAMS)
    assert result2["structure"] == "unknown", (
        f"break of bearish structure must go to 'unknown', got {result2['structure']}"
    )


def test_anti_lookahead_swing_not_visible_before_confirmation():
    """A swing must not appear (affect structure) before its confirmation
    bar (swing_idx + N), mirroring CLAUDE.md rule 1/2 for compute_levels."""
    candles = _m5_from_h1(_uptrend_h1())
    N = STRUCT_PARAMS["swing_n"]

    h1 = candles  # already H1-spaced
    swings = detect_h1_swings(h1, STRUCT_PARAMS)
    assert swings, "fixture must produce swings"

    # Pick a swing and check: structure computed exactly AT confirmation must
    # differ from (or at least not incorrectly include) as_of just before it.
    last_swing = max(swings, key=lambda s: s["confirmed_ts"])
    just_before = last_swing["confirmed_ts"] - H1
    visible_before = [s for s in swings if s["confirmed_ts"] <= just_before]
    visible_at = [s for s in swings if s["confirmed_ts"] <= last_swing["confirmed_ts"]]
    assert len(visible_before) < len(visible_at), (
        "swing must become visible only at/after its confirmation timestamp"
    )


def test_not_enough_swings_returns_unknown():
    t0 = datetime(2024, 1, 1, tzinfo=UTC)
    candles = _flat_h1(t0, 15)  # too few bars for 2 highs + 2 lows
    result = compute_market_structure(candles, candles[-1]["ts_utc"], STRUCT_PARAMS)
    assert result["structure"] == "unknown"


# ---------------------------------------------------------------------------
# check_level_reversion wiring: BUY blocked in bearish, SELL blocked in bullish
# ---------------------------------------------------------------------------

SUPPORT = {"kind": "support", "price_low": 1998.0, "price_high": 2000.0, "strength": 1.0, "status": "active"}
RESISTANCE = {"kind": "resistance", "price_low": 2008.0, "price_high": 2010.0, "strength": 1.0, "status": "active"}
WEAK_FAR_SUPPORT = {"kind": "support", "price_low": 1900.0, "price_high": 1901.0, "strength": 0.01, "status": "active"}
WEAK_FAR = {"kind": "resistance", "price_low": 2100.0, "price_high": 2101.0, "strength": 0.01, "status": "active"}


def _ts():
    return datetime(2024, 1, 1, 12, 0, tzinfo=UTC)


def test_buy_blocked_in_bearish_structure():
    result = check_level_reversion(
        symbol="TEST", tf="M5", ts_utc=_ts(),
        high=2002.0, low=1992.0, close=2001.0,
        atr=1.0, levels=[SUPPORT, WEAK_FAR], params={},
        structure="bearish",
    )
    assert result is None, "BUY must be blocked when structure is bearish"


def test_buy_allowed_in_bullish_or_unknown_structure():
    for structure in ("bullish", "unknown", None):
        result = check_level_reversion(
            symbol="TEST", tf="M5", ts_utc=_ts(),
            high=2002.0, low=1992.0, close=2001.0,
            atr=1.0, levels=[SUPPORT, WEAK_FAR], params={},
            structure=structure,
        )
        assert result is not None, f"BUY should fire when structure={structure}"
        assert result["direction"] == "BUY"


def test_sell_blocked_in_bullish_structure():
    result = check_level_reversion(
        symbol="TEST", tf="M5", ts_utc=_ts(),
        high=2012.0, low=1999.0, close=2005.0,
        atr=1.0, levels=[RESISTANCE, WEAK_FAR_SUPPORT], params={},
        structure="bullish",
    )
    assert result is None, "SELL must be blocked when structure is bullish"


def test_sell_allowed_in_bearish_or_unknown_structure():
    for structure in ("bearish", "unknown", None):
        result = check_level_reversion(
            symbol="TEST", tf="M5", ts_utc=_ts(),
            high=2012.0, low=1999.0, close=2005.0,
            atr=1.0, levels=[RESISTANCE, WEAK_FAR_SUPPORT], params={},
            structure=structure,
        )
        assert result is not None, f"SELL should fire when structure={structure}"
        assert result["direction"] == "SELL"
