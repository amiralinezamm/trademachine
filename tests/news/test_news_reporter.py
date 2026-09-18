"""Tests for src/news/news_reporter.py and src/news/intensity_estimator.py.

All pure function tests — no DB, no network.
"""
from datetime import datetime, timedelta, timezone

import pytest

from src.news.intensity_estimator import estimate_intensity
from src.news.news_reporter import (
    DIRECTION_LABEL,
    _to_tehran_hhmm,
    build_release_message,
    build_upcoming_message,
)

UTC = timezone.utc
T0 = datetime(2026, 9, 26, 12, 30, tzinfo=UTC)  # Friday 12:30 UTC = 17:00 IRDT


# ---------------------------------------------------------------------------
# intensity_estimator
# ---------------------------------------------------------------------------

def test_intensity_returns_no_data_when_empty():
    result = estimate_intensity(z_abs=1.5, historical_moves_15m=[])
    assert result["sufficient_data"] is False
    assert result["min_move"] is None
    assert "داده کافی" in result["message"]


def test_intensity_returns_no_data_below_min_episodes():
    # min_episodes default is 5; supply 4
    result = estimate_intensity(
        z_abs=1.5,
        historical_moves_15m=[-3.0, -5.0, -4.0, -2.0],
        params={"min_episodes": 5, "band_width": 0.5},
    )
    assert result["sufficient_data"] is False
    assert result["episode_count"] == 4


def test_intensity_returns_range_with_sufficient_data():
    moves = [-3.0, -5.0, -4.0, -2.0, -6.0]  # exactly 5
    result = estimate_intensity(
        z_abs=1.5,
        historical_moves_15m=moves,
        params={"min_episodes": 5, "band_width": 0.5},
    )
    assert result["sufficient_data"] is True
    assert result["min_move"] == -6.0
    assert result["max_move"] == -2.0
    assert result["episode_count"] == 5
    assert "۵ مورد" in result["message"]


def test_intensity_message_includes_range_values():
    result = estimate_intensity(
        z_abs=None,
        historical_moves_15m=[-3.0, -5.0, 1.0, -4.0, -2.0],
        params={"min_episodes": 5, "band_width": 0.5},
    )
    assert result["sufficient_data"] is True
    assert "-5.00" in result["message"] or "5.00" in result["message"]


# ---------------------------------------------------------------------------
# _to_tehran_hhmm
# ---------------------------------------------------------------------------

def test_tehran_offset_summer_irdt():
    """UTC+4:30 in summer (April–September). 12:30 UTC → 17:00 IRDT."""
    dt = datetime(2026, 9, 26, 12, 30, tzinfo=UTC)
    assert _to_tehran_hhmm(dt) == "17:00"


def test_tehran_offset_winter_irst():
    """UTC+3:30 in winter (October–March). 12:30 UTC → 16:00 IRST."""
    dt = datetime(2026, 12, 5, 12, 30, tzinfo=UTC)
    assert _to_tehran_hhmm(dt) == "16:00"


# ---------------------------------------------------------------------------
# build_release_message
# ---------------------------------------------------------------------------

def _surprise_result(z=1.8, raw_dir=-1, expected_dir=-1, surprise=0.09, gold_sign=-1):
    return {
        "surprise": surprise,
        "z_surprise": z,
        "raw_direction": raw_dir,
        "expected_dir": expected_dir,
        "gold_sign": gold_sign,
        "mapped": True,
        "sample_count": 12,
    }


def _intensity_ok():
    return {
        "sufficient_data": True,
        "min_move": -9.0,
        "max_move": -3.0,
        "episode_count": 7,
        "band_applied": False,
        "message": "در ۷ مورد مشابه، حرکت ۱۵دقیقه‌ای بین -9.00 و -3.00 دلار بوده",
    }


def _event():
    return {
        "title": "Non-Farm Employment Change",
        "country": "USD",
        "impact": "High",
        "ts_utc": T0,
        "forecast": "190K",
        "previous": "205K",
    }


def test_release_message_contains_title():
    msg = build_release_message(
        event=_event(),
        actual="280K",
        surprise_result=_surprise_result(),
        intensity_result=_intensity_ok(),
        price_move={"delta": -4.20, "seconds": 40},
        blackout_end=T0 + timedelta(minutes=15),
    )
    assert "Non-Farm Employment Change" in msg


def test_release_message_contains_actual_forecast_previous():
    msg = build_release_message(
        event=_event(),
        actual="280K",
        surprise_result=_surprise_result(),
        intensity_result=_intensity_ok(),
        price_move=None,
        blackout_end=None,
    )
    assert "280K" in msg
    assert "190K" in msg
    assert "205K" in msg


def test_release_message_z_surprise_shown_when_available():
    msg = build_release_message(
        event=_event(), actual="280K",
        surprise_result=_surprise_result(z=1.8),
        intensity_result=_intensity_ok(),
        price_move=None, blackout_end=None,
    )
    assert "1.8" in msg
    assert "انحراف معیار" in msg


def test_release_message_no_z_shows_raw_surprise():
    result = _surprise_result(z=None, surprise=0.09)
    msg = build_release_message(
        event=_event(), actual="0.4%",
        surprise_result=result,
        intensity_result=_intensity_ok(),
        price_move=None, blackout_end=None,
    )
    assert "z" not in msg.lower() or "انحراف" not in msg
    assert "سورپرایز خام" in msg


def test_release_message_expected_direction_bearish():
    msg = build_release_message(
        event=_event(), actual="280K",
        surprise_result=_surprise_result(expected_dir=-1),
        intensity_result=_intensity_ok(),
        price_move=None, blackout_end=None,
    )
    assert "نزولی" in msg


def test_release_message_price_move_shown():
    msg = build_release_message(
        event=_event(), actual="280K",
        surprise_result=_surprise_result(),
        intensity_result=_intensity_ok(),
        price_move={"delta": -4.20, "seconds": 40},
        blackout_end=None,
    )
    assert "-4.20" in msg or "4.20" in msg
    assert "40" in msg


def test_release_message_blackout_shown_in_tehran_time():
    blackout_end = datetime(2026, 9, 26, 12, 45, tzinfo=UTC)  # 17:15 IRDT
    msg = build_release_message(
        event=_event(), actual="280K",
        surprise_result=_surprise_result(),
        intensity_result=_intensity_ok(),
        price_move=None,
        blackout_end=blackout_end,
    )
    assert "17:15" in msg
    assert "مسدود" in msg


def test_release_message_no_blackout_no_stop_line():
    msg = build_release_message(
        event=_event(), actual="280K",
        surprise_result=_surprise_result(),
        intensity_result=_intensity_ok(),
        price_move=None, blackout_end=None,
    )
    assert "مسدود" not in msg


def test_release_message_always_has_warning():
    msg = build_release_message(
        event=_event(), actual="280K",
        surprise_result=_surprise_result(),
        intensity_result=_intensity_ok(),
        price_move=None, blackout_end=None,
    )
    assert "آزمایشی" in msg
    assert "مسئولیت" in msg


def test_release_message_insufficient_intensity_data():
    no_data = {
        "sufficient_data": False,
        "min_move": None, "max_move": None,
        "episode_count": 2, "band_applied": False,
        "message": "داده کافی برای برآورد شدت وجود ندارد",
    }
    msg = build_release_message(
        event=_event(), actual="280K",
        surprise_result=_surprise_result(),
        intensity_result=no_data,
        price_move=None, blackout_end=None,
    )
    assert "داده کافی" in msg


# ---------------------------------------------------------------------------
# build_upcoming_message
# ---------------------------------------------------------------------------

def test_upcoming_message_contains_title_and_time():
    surprise = {"gold_sign": -1, "mapped": True}
    msg = build_upcoming_message(event=_event(), surprise_result=surprise)
    assert "Non-Farm Employment Change" in msg
    assert "17:00" in msg  # T0 = 12:30 UTC = 17:00 IRDT


def test_upcoming_message_shows_expected_direction():
    surprise = {"gold_sign": -1, "mapped": True}
    msg = build_upcoming_message(event=_event(), surprise_result=surprise)
    assert "نزولی" in msg


def test_upcoming_message_always_has_warning():
    surprise = {"gold_sign": None, "mapped": False}
    msg = build_upcoming_message(event=_event(), surprise_result=surprise)
    assert "آزمایشی" in msg
    assert "مسئولیت" in msg


def test_upcoming_message_unmapped_event():
    surprise = {"gold_sign": None, "mapped": False}
    msg = build_upcoming_message(event=_event(), surprise_result=surprise)
    assert "نامشخص" in msg or "یافت نشد" in msg
