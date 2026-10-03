"""Unit tests for Telegram bot message formatters.

No Telegram API, no DB, no HTTP — pure function tests with mock data.
Every formatter now shares one visual language (DIVIDER + <blockquote>
per fact-block, HTML parse_mode) -- 2026-10-03 follow-up to the signal
alert revamp (src/engine/signal_formatter.py).
"""
import pytest
from src.engine.signal_formatter import DIVIDER
from src.telegram_bot.formatters import (
    DISCLAIMER,
    fmt_error,
    fmt_gaps,
    fmt_levels,
    fmt_news,
    fmt_start,
    fmt_status,
)

# ── /start ────────────────────────────────────────────────────────────────────


def test_start_lists_all_commands_in_a_quote_block():
    msg = fmt_start()
    quote_body = msg[msg.index("<blockquote>") : msg.index("</blockquote>")]
    for cmd in ("/status", "/levels", "/gaps", "/news"):
        assert cmd in quote_body
    assert DIVIDER in msg
    assert "آزمایشی" in msg


# ── error replies ─────────────────────────────────────────────────────────────


def test_error_escapes_html_special_chars_in_exception_text():
    """Every bot reply is now sent with parse_mode='HTML' -- an exception
    message containing '<'/'&' must not break (or silently truncate) the
    rendered message."""
    exc = Exception("timeout calling <http://bad> & giving up")
    msg = fmt_error("دریافت سیگنال", exc)
    assert "<http://bad>" not in msg
    assert "&lt;http://bad&gt;" in msg
    assert "دریافت سیگنال" in msg


# ── /status ───────────────────────────────────────────────────────────────────


def test_status_no_signal():
    data = {"signal": None, "reason": "no candles", "as_of": "2026-09-19T10:00:00"}
    msg = fmt_status(data)
    assert "سیگنال فعال" in msg and "ندارد" in msg
    assert "no candles" in msg
    assert DISCLAIMER in msg


def test_status_no_signal_without_as_of():
    """/signal/last (unlike the old /signal/latest) never returns an
    'as_of' field for the no-signal-stored case -- must not print a bare
    'آخرین بررسی: ' line."""
    data = {"signal": None, "reason": "no_signal_stored"}
    msg = fmt_status(data)
    assert "سیگنال فعال" in msg and "ندارد" in msg
    assert "no_signal_stored" in msg
    assert "آخرین بررسی" not in msg
    assert DISCLAIMER in msg


def _signal(**overrides):
    base = {
        "direction": "BUY",
        "entry": 2350.5,
        "stop_loss": 2340.0,
        "take_profit": 2365.0,
        "ts_utc": "2026-09-19T10:05:00+00:00",
        "rule_version": "level_reversion_v1",
        "components": {
            "symbol": "XAUUSD@",
            "level_kind": "support",
            "level_price_low": 2340.0,
            "level_price_high": 2345.0,
            "level_strength": 1.75,
            "atr": 8.5,
            "net_votes": 3,
            "sl_tp": {"rr": 2.1, "relaxed": False},
            "market_structure": "bullish",
        },
    }
    base.update(overrides)
    return base


def test_status_signal_shows_outcome_when_present():
    data = {"signal": _signal(outcome="tp")}
    msg = fmt_status(data)
    assert "TP" in msg


def test_status_signal_omits_outcome_line_when_absent():
    data = {"signal": _signal()}
    msg = fmt_status(data)
    assert "نتیجه" not in msg


def test_status_buy_signal():
    data = {"signal": _signal()}
    msg = fmt_status(data)
    assert "BUY" in msg
    assert "2350.500" in msg
    assert "حمایت" in msg
    assert "2340" in msg
    assert "1.75" in msg


def test_status_sell_signal():
    data = {
        "signal": _signal(
            direction="SELL", entry=2410.0,
            ts_utc="2026-09-19T11:00:00+00:00",
            components={
                "symbol": "XAUUSD@",
                "level_kind": "resistance",
                "level_price_low": 2415.0,
                "level_price_high": 2420.0,
                "level_strength": 2.3,
                "atr": 9.0,
            },
        )
    }
    msg = fmt_status(data)
    assert "SELL" in msg
    assert "🔴" in msg


def test_status_missing_components():
    data = {"signal": _signal(components={})}
    msg = fmt_status(data)
    assert "BUY" in msg
    assert "?" in msg  # placeholders for missing fields


def test_status_uses_telegram_html_field_verbatim_when_present():
    """/signal/last attaches telegram_html itself (built from the SAME
    builder the n8n alert uses) -- fmt_status must pass it through
    untouched rather than re-deriving it, so the two surfaces can never
    drift even if build_signal_alert_text's signature changes."""
    sig = _signal()
    sig["telegram_html"] = "<b>exact server-built text</b>"
    msg = fmt_status({"signal": sig})
    assert msg == "<b>exact server-built text</b>"


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
    assert "2350.00" in msg
    assert "2330" in msg
    assert "2370" in msg
    assert "مقاومت" in msg
    assert "حمایت" in msg
    assert DIVIDER in msg
    assert DISCLAIMER in msg


def test_levels_support_and_resistance_each_in_their_own_quote_block():
    data = {
        "price": 2350.0,
        "support": {"price_low": 2330.0, "price_high": 2335.0, "distance": 15.0, "strength": 1.2},
        "resistance": {"price_low": 2370.0, "price_high": 2375.0, "distance": 20.0, "strength": 0.9},
    }
    msg = fmt_levels(data)
    assert msg.count("<blockquote>") == 2
    assert msg.count("</blockquote>") == 2


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
    assert msg.count("<blockquote>") == 2
    assert DIVIDER in msg
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
    assert msg.count("<blockquote>") == 5
    # the 6th gap ([5.0 – 6.0]) must not appear at all
    assert "[5.0 – 6.0]" not in msg


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
