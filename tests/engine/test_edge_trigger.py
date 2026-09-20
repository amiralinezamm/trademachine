"""Tests for check_level_reversion edge-trigger behavior.

Verifies that the signal fires EXACTLY ONCE on the confirmation bar
(wick-in + close-outside), not on every bar where price hovers near
the zone.

Note: check_level_reversion requires strength > median(all_active_levels).
Tests that expect a signal always include a second weak level so the
target level qualifies (strength 2.0 > median(2.0, 0.5) = 1.25).
"""
from __future__ import annotations
from datetime import datetime, timezone

import pytest

from src.engine.level_reversion import check_level_reversion

UTC = timezone.utc
TS = datetime(2024, 1, 1, tzinfo=UTC)
PARAMS = {}  # proximity_atr_mult deprecated; not used

# Target levels — strength=2.0
SUPPORT = {
    "id": 1, "kind": "support", "status": "active",
    "price_low": 1990.0, "price_high": 1995.0,
    "strength": 2.0,
}
RESISTANCE = {
    "id": 2, "kind": "resistance", "status": "active",
    "price_low": 2005.0, "price_high": 2010.0,
    "strength": 2.0,
}
# Weak level far from action — ensures target qualifies as above-median
WEAK_FAR = {
    "id": 99, "kind": "resistance", "status": "active",
    "price_low": 2200.0, "price_high": 2205.0,
    "strength": 0.5,
}


def _sig(high, low, close, levels, atr=10.0):
    return check_level_reversion(
        symbol="TEST", tf="M5", ts_utc=TS,
        high=high, low=low, close=close,
        atr=atr, levels=levels, params=PARAMS,
    )


# ── Support (BUY) tests ───────────────────────────────────────────────────────

class TestSupportBuy:
    def test_no_signal_when_price_above_zone(self):
        """Price never enters zone — no signal."""
        assert _sig(high=2002.0, low=1997.0, close=1998.0, levels=[SUPPORT, WEAK_FAR]) is None

    def test_no_signal_when_wick_enters_but_close_inside(self):
        """Wick dips into zone but close stays inside — not confirmed yet."""
        assert _sig(high=1998.0, low=1992.0, close=1993.0, levels=[SUPPORT, WEAK_FAR]) is None

    def test_no_signal_when_wick_enters_and_close_breaks_through(self):
        """Wick entered zone, close below zone_low — break-through, not reversal."""
        assert _sig(high=1998.0, low=1988.0, close=1986.0, levels=[SUPPORT, WEAK_FAR]) is None

    def test_signal_when_wick_enters_and_close_above_zone(self):
        """Wick dipped into support zone, close rebounded above zone_high → BUY."""
        result = _sig(high=2002.0, low=1992.0, close=2001.0, levels=[SUPPORT, WEAK_FAR])
        assert result is not None
        assert result["direction"] == "BUY"
        assert result["components"]["level_id"] == 1

    def test_signal_fires_only_once_not_on_proximity_bar(self):
        """
        Scenario: 3 bars with price near support zone (old proximity logic would
        fire 3 times). New logic: only the bar that wicks in AND closes above zone.
        """
        levels = [SUPPORT, WEAK_FAR]

        # Bar 1: close near zone but no wick entry (high < price_high)
        r1 = _sig(high=1994.0, low=1997.0, close=1997.5, levels=levels)
        assert r1 is None, "Should not fire — no wick entry"

        # Bar 2: wick enters zone, close stays inside — no confirmation yet
        r2 = _sig(high=1997.0, low=1991.0, close=1993.0, levels=levels)
        assert r2 is None, "Should not fire — close still inside zone"

        # Bar 3: wick enters zone, close above zone_high — THIS is the signal bar
        r3 = _sig(high=2005.0, low=1992.0, close=2001.0, levels=levels)
        assert r3 is not None, "Should fire on confirmation bar"
        assert r3["direction"] == "BUY"

    def test_above_median_strength_required(self):
        """Level with below-median strength is filtered out."""
        weak_support = {**SUPPORT, "id": 5, "strength": 0.3}
        strong_far = {**WEAK_FAR, "id": 6, "strength": 2.0}
        # weak_support wick enters zone but strength(0.3) < median(1.15) → filtered
        result = _sig(high=1998.0, low=1991.0, close=1996.5,
                      levels=[weak_support, strong_far])
        assert result is None

    def test_nearest_qualifying_level_wins(self):
        """When two qualifying levels are in range, nearest close wins."""
        support_far  = {**SUPPORT, "id": 10, "price_low": 1970.0, "price_high": 1975.0, "strength": 3.0}
        support_near = {**SUPPORT, "id": 11, "price_low": 1990.0, "price_high": 1995.0, "strength": 4.0}
        # Wick enters both zones; close=1997 → above both zones → both qualify
        r = _sig(high=2005.0, low=1970.0, close=2001.0, levels=[support_far, support_near])
        assert r is not None
        assert r["components"]["level_id"] == 11  # nearer to close wins


# ── Resistance (SELL) tests ───────────────────────────────────────────────────

WEAK_FAR_SUPPORT = {
    "id": 98, "kind": "support", "status": "active",
    "price_low": 1700.0, "price_high": 1705.0,
    "strength": 0.5,
}


class TestResistanceSell:
    def test_no_signal_when_price_below_zone(self):
        assert _sig(high=2003.0, low=1998.0, close=2002.0,
                    levels=[RESISTANCE, WEAK_FAR_SUPPORT]) is None

    def test_no_signal_when_wick_enters_but_close_inside(self):
        """Wick pokes into resistance, close stays inside — no confirmation."""
        assert _sig(high=2008.0, low=2000.0, close=2006.0,
                    levels=[RESISTANCE, WEAK_FAR_SUPPORT]) is None

    def test_no_signal_when_wick_enters_and_close_breaks_through(self):
        """Wick entered resistance, close above zone_high — break-through, not reversal."""
        assert _sig(high=2015.0, low=2000.0, close=2013.0,
                    levels=[RESISTANCE, WEAK_FAR_SUPPORT]) is None

    def test_signal_when_wick_enters_and_close_below_zone(self):
        """Wick poked into resistance, close fell back below zone_low → SELL."""
        result = _sig(high=2008.0, low=2000.0, close=1999.0,
                      levels=[RESISTANCE, WEAK_FAR_SUPPORT])
        assert result is not None
        assert result["direction"] == "SELL"
        assert result["components"]["level_id"] == 2

    def test_edge_trigger_not_repeat_fire(self):
        """Three bars near resistance. Only the reversal bar fires."""
        levels = [RESISTANCE, WEAK_FAR_SUPPORT]

        # Bar 1: hovering below resistance — no wick entry (high < price_low)
        r1 = _sig(high=2004.0, low=2001.0, close=2003.0, levels=levels)
        assert r1 is None

        # Bar 2: wick enters resistance, close inside zone — no confirmation
        r2 = _sig(high=2007.0, low=2001.0, close=2006.0, levels=levels)
        assert r2 is None

        # Bar 3: wick enters resistance, close back below zone_low — SIGNAL
        r3 = _sig(high=2007.0, low=1998.0, close=1999.0, levels=levels)
        assert r3 is not None
        assert r3["direction"] == "SELL"

    def test_no_active_levels_returns_none(self):
        expired = {**RESISTANCE, "status": "expired"}
        assert _sig(high=2008.0, low=2000.0, close=2003.0, levels=[expired]) is None


# ── Off hours resistance filter tests ────────────────────────────────────────

def _sig_at_hour(hour: int, kind: str = "resistance", close: float = 1999.0):
    """Helper: emit signal at given UTC hour using RESISTANCE or SUPPORT zone."""
    from datetime import datetime, timezone
    ts = datetime(2024, 1, 1, hour, 0, tzinfo=timezone.utc)
    if kind == "resistance":
        return check_level_reversion(
            symbol="TEST", tf="M5", ts_utc=ts,
            high=2008.0, low=1998.0, close=close,
            atr=10.0, levels=[RESISTANCE, WEAK_FAR_SUPPORT], params={},
        )
    else:  # support
        return check_level_reversion(
            symbol="TEST", tf="M5", ts_utc=ts,
            high=2002.0, low=1992.0, close=2001.0,
            atr=10.0, levels=[SUPPORT, WEAK_FAR], params={},
        )


def test_off_hours_resistance_suppressed():
    """Resistance signal suppressed during off hours (17-23 UTC)."""
    for hour in (17, 20, 23):
        result = _sig_at_hour(hour, kind="resistance")
        assert result is None, f"expected None at hour {hour} (resistance)"


def test_off_hours_support_still_fires():
    """Support signal NOT suppressed during off hours — filter is resistance-only."""
    for hour in (17, 20, 23):
        result = _sig_at_hour(hour, kind="support")
        assert result is not None, f"expected signal at hour {hour} (support)"
        assert result["direction"] == "BUY"


def test_intraday_resistance_still_fires():
    """Resistance signal fires normally during non-off-hours (hours 0-16)."""
    for hour in (0, 7, 12, 16):
        result = _sig_at_hour(hour, kind="resistance")
        assert result is not None, f"expected signal at hour {hour} (resistance)"
        assert result["direction"] == "SELL"


def test_off_hours_boundary_hour16_fires():
    """Hour 16 is NOT off hours — resistance should still emit."""
    result = _sig_at_hour(16, kind="resistance")
    assert result is not None
    assert result["direction"] == "SELL"


def test_off_hours_boundary_hour17_suppressed():
    """Hour 17 IS off hours — resistance suppressed."""
    result = _sig_at_hour(17, kind="resistance")
    assert result is None
