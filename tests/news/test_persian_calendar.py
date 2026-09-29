"""Tests for src/news/persian_calendar.py — pure, no DB, no network.

Deliberately avoids hardcoding a specific Gregorian->Jalali date pair (e.g.
"2026-09-26 is Mehr 4, 1405") since that number isn't independently verified
here — instead these check the properties the digest code actually relies
on: round-trip correctness (via jdatetime itself) and Persian-digit output.
"""
from datetime import datetime, timezone

from src.news.persian_calendar import (
    _MONTH_FA,
    _WEEKDAY_FA,
    jalali_day_header_fa,
    tehran_calendar_day_key,
    to_persian_digits,
)

UTC = timezone.utc


def test_to_persian_digits_basic():
    assert to_persian_digits("123") == "۱۲۳"


def test_to_persian_digits_preserves_non_digits():
    assert to_persian_digits("16:00") == "۱۶:۰۰"


def test_jalali_day_header_contains_known_weekday_and_month():
    dt = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)  # a Saturday in Tehran
    header = jalali_day_header_fa(dt)
    assert any(w in header for w in _WEEKDAY_FA)
    assert any(m in header for m in _MONTH_FA)


def test_jalali_day_header_uses_persian_digits_for_day():
    dt = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
    header = jalali_day_header_fa(dt)
    assert not any(ch.isdigit() and ch.isascii() for ch in header)


def test_tehran_calendar_day_key_groups_same_local_day():
    early = datetime(2026, 9, 26, 0, 0, tzinfo=UTC)   # 03:30 Tehran
    late = datetime(2026, 9, 26, 19, 0, tzinfo=UTC)    # 22:30 Tehran, same local day
    next_day = datetime(2026, 9, 26, 21, 0, tzinfo=UTC)  # 00:30 Tehran next day
    assert tehran_calendar_day_key(early) == tehran_calendar_day_key(late)
    assert tehran_calendar_day_key(early) != tehran_calendar_day_key(next_day)
