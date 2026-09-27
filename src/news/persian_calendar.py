"""Pure Persian (Jalali) calendar helpers for Telegram-facing news messages.

Display-only, per CLAUDE.md rule 4 (storage stays UTC, display converts to
Tehran). Weekday/month names are a fixed table here rather than relying on
jdatetime's global locale switch, so output never depends on locale state
set elsewhere in the process.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import jdatetime

TEHRAN = ZoneInfo("Asia/Tehran")

# Python's datetime.weekday(): Monday=0 ... Sunday=6. Same ordering here.
_WEEKDAY_FA = ["دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه", "شنبه", "یکشنبه"]

_MONTH_FA = [
    "فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
    "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند",
]

_PERSIAN_DIGITS = "۰۱۲۳۴۵۶۷۸۹"


def to_persian_digits(s: str) -> str:
    """ASCII digits -> Persian digits. Non-digit characters pass through."""
    return "".join(_PERSIAN_DIGITS[int(ch)] if ch.isdigit() else ch for ch in s)


def jalali_day_header_fa(dt_utc: datetime) -> str:
    """UTC-aware datetime -> Tehran-local Jalali day header, e.g. 'دوشنبه ۱۲ مهر'."""
    tehran_dt = dt_utc.astimezone(TEHRAN)
    weekday_fa = _WEEKDAY_FA[tehran_dt.weekday()]
    jalali = jdatetime.date.fromgregorian(date=tehran_dt.date())
    day_fa = to_persian_digits(str(jalali.day))
    month_fa = _MONTH_FA[jalali.month - 1]
    return f"{weekday_fa} {day_fa} {month_fa}"


def tehran_calendar_day_key(dt_utc: datetime) -> "datetime.date":
    """Tehran-local calendar date (for grouping events by day). Not a display value."""
    return dt_utc.astimezone(TEHRAN).date()
