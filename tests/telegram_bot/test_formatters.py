"""Unit tests for Telegram bot message formatters.

No Telegram API, no DB, no HTTP — pure function tests with mock data.
"""
import pytest
from src.telegram_bot.formatters import (
    DISCLAIMER,
    fmt_gaps,
    fmt_levels,
    fmt_news,
    fmt_status,
)

# ── /status ───────────────────────────────────────────────────────────────────


def test_status_no_signal():
    data = {"signal": None, "reason": "no candles", "as_of": "2026-09-19T10:00:00"}
    msg = fmt_status(data)
    assert "سیگنال فعال: ندارد" in msg
    assert "no candles" in msg
    assert DISCLAIMER in msg


def test_status_buy_signal():
    data = {
        "signal": {
            "direction": "BUY",
            "entry": 2350.5,
            "ts_utc": "2026-09-19T10:05:00+00:00",
            "components": {
                "level_kind": "support",
                "level_price_low": 2340.0,
                "level_price_high": 2345.0,
                "level_strength": 1.75,
                "atr": 8.5,
            },
        }
    }
    msg = fmt_status(data)
    assert "BUY" in msg
    assert "2350.5" in msg
    assert "support" in msg
    assert "2340" in msg
    assert "1.75" in msg
    assert DISCLAIMER in msg


def test_status_sell_signal():
    data = {
        "signal": {
            "direction": "SELL",
            "entry": 2410.0,
            "ts_utc": "2026-09-19T11:00:00+00:00",
            "components": {
                "level_kind": "resistance",
                "level_price_low": 2415.0,
                "level_price_high": 2420.0,
                "level_strength": 2.3,
                "atr": 9.0,
            },
        }
    }
    msg = fmt_status(data)
    assert "SELL" in msg
    assert "🔴" in msg
    assert DISCLAIMER in msg


def test_status_missing_components():
    data = {
        "signal": {
            "direction": "BUY",
            "entry": 2300.0,
            "ts_utc": "2026-09-19T09:00:00",
            "components": {},
        }
    }
    msg = fmt_status(data)
    assert "BUY" in msg
    assert "?" in msg  # placeholders for missing fields
    assert DISCLAIMER in msg


# ── /levels ───────────────────────────────────────────────────────────────────


def test_levels_both_sides():
    data = {
        "price": 2350.0,
        "support": {
            "price_low": 2330.0,
            "price_high": 2335.0,
            "distance": 15.0,
            "strength": 1.2,
        },
        "resistance": {
            "price_low": 2370.0,
            "price_high": 2375.0,
            "distance": 20.0,
            "strength": 0.9,
        },
    }
    msg = fmt_levels(data)
    assert "2350.0" in msg
    assert "2330" in msg
    assert "2370" in msg
    assert "مقاومت" in msg
    assert "حمایت" in msg
    assert DISCLAIMER in msg


def test_levels_no_support():
    data = {
        "price": 2350.0,
        "support": None,
        "resistance": {
            "price_low": 2370.0,
            "price_high": 2375.0,
            "distance": 20.0,
            "strength": 0.9,
        },
    }
    msg = fmt_levels(data)
    assert "پیدا نشد" in msg


def test_levels_no_resistance():
    data = {
        "price": 2350.0,
        "support": {
            "price_low": 2330.0,
            "price_high": 2335.0,
            "distance": 15.0,
            "strength": 1.2,
        },
        "resistance": None,
    }
    msg = fmt_levels(data)
    assert "پیدا نشد" in msg


def test_levels_both_missing():
    data = {"price": 2350.0, "support": None, "resistance": None}
    msg = fmt_levels(data)
    assert msg.count("پیدا نشد") == 2
    assert DISCLAIMER in msg


# ── /gaps ─────────────────────────────────────────────────────────────────────


def test_gaps_empty_list():
    msg = fmt_gaps([])
    assert "ندارد" in msg
    assert DISCLAIMER in msg


def test_gaps_empty_dict():
    msg = fmt_gaps({"gaps": []})
    assert "ندارد" in msg
    assert DISCLAIMER in msg


def test_gaps_with_data():
    gaps = [
        {
            "status": "OPEN",
            "gap_high": 2360.0,
            "gap_low": 2355.0,
            "weight": 0.78,
            "ts_utc": "2026-09-17T10:00:00",
        },
        {
            "status": "HALF_FILLED",
            "gap_high": 2310.0,
            "gap_low": 2305.0,
            "weight": 0.45,
            "ts_utc": "2026-09-15T08:00:00",
        },
    ]
    msg = fmt_gaps(gaps)
    assert "OPEN" in msg
    assert "HALF_FILLED" in msg
    assert "2360" in msg
    assert "0.78" in msg
    assert DISCLAIMER in msg


def test_gaps_truncates_at_five():
    gaps = [
        {
            "status": "OPEN",
            "gap_high": float(i + 1),
            "gap_low": float(i),
            "weight": 0.5,
            "ts_utc": "2026-09-01T00:00:00",
        }
        for i in range(10)
    ]
    msg = fmt_gaps(gaps)
    # Only first 5 should appear; gap_high=6 is the 6th entry — should not be shown
    assert "6.0–7.0" not in msg


# ── /news ─────────────────────────────────────────────────────────────────────


def test_news_empty():
    msg = fmt_news({"events": [], "count": 0})
    assert "ندارد" in msg
    assert DISCLAIMER in msg


def test_news_uses_digest_field_verbatim():
    """digest is fully rendered server-side (build_upcoming_digest) — fmt_news
    is just a pass-through so the Telegram HTML tags survive untouched."""
    digest = "🗞 <b>رویدادهای اقتصادی پیش‌رو</b>\n\n<blockquote>NFP</blockquote>"
    msg = fmt_news({"events": [{"title": "NFP"}], "count": 1, "digest": digest})
    assert msg == digest


def test_news_no_digest_falls_back_to_count_summary():
    events = [{"title": "FOMC", "ts_utc": "2026-09-19T18:00:00"}]
    msg = fmt_news({"events": events, "count": 1})
    assert "1" in msg
    assert DISCLAIMER in msg
