"""Tests for is_forex_market_open — pure logic, no DB needed."""
from datetime import datetime, timezone

import pytest

from src.api.main import is_forex_market_open

UTC = timezone.utc


def _dt(weekday_name: str, hour: int, minute: int = 0) -> datetime:
    """Build a UTC datetime for the given weekday in the current ISO week."""
    days = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}
    # Use a known reference Monday (2026-09-14)
    ref_monday = datetime(2026, 9, 14, tzinfo=UTC)
    from datetime import timedelta
    return ref_monday + timedelta(days=days[weekday_name], hours=hour, minutes=minute)


# ---------------------------------------------------------------------------
# Saturday — always closed
# ---------------------------------------------------------------------------

def test_saturday_midnight_closed():
    assert is_forex_market_open(_dt("Sat", 0)) is False


def test_saturday_noon_closed():
    assert is_forex_market_open(_dt("Sat", 12)) is False


def test_saturday_23_closed():
    assert is_forex_market_open(_dt("Sat", 23)) is False


# ---------------------------------------------------------------------------
# Sunday — closed before 21:00 UTC, open from 21:00
# ---------------------------------------------------------------------------

def test_sunday_before_21_closed():
    assert is_forex_market_open(_dt("Sun", 20, 59)) is False


def test_sunday_exactly_21_open():
    assert is_forex_market_open(_dt("Sun", 21)) is True


def test_sunday_after_21_open():
    assert is_forex_market_open(_dt("Sun", 22)) is True


# ---------------------------------------------------------------------------
# Friday — open until 21:00, closed from 21:00
# ---------------------------------------------------------------------------

def test_friday_before_21_open():
    assert is_forex_market_open(_dt("Fri", 20, 59)) is True


def test_friday_exactly_21_closed():
    assert is_forex_market_open(_dt("Fri", 21)) is False


def test_friday_after_21_closed():
    assert is_forex_market_open(_dt("Fri", 23)) is False


# ---------------------------------------------------------------------------
# Weekdays — always open (candle-lag alert still fires)
# ---------------------------------------------------------------------------

def test_monday_open():
    assert is_forex_market_open(_dt("Mon", 3)) is True


def test_tuesday_open():
    assert is_forex_market_open(_dt("Tue", 14)) is True


def test_wednesday_open():
    assert is_forex_market_open(_dt("Wed", 0)) is True


def test_thursday_open():
    assert is_forex_market_open(_dt("Thu", 21)) is True
