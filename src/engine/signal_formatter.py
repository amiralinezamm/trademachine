"""Telegram-HTML message builders for live signal alerts (2026-10-03 task).

Pure functions: input is the same dict the API already returns from
/signal/latest, /signal/last and the reversal-close advisory, output is a
ready-to-send Telegram HTML string. No DB access, no HTTP calls.

Why these live in src/engine (not src/telegram_bot): the SAME text must be
usable from both the Telegram bot (/status, fmt_status) and the n8n alert
workflow (via a telegram_html field on the API response) — one function, one
place CLAUDE.md rule 6's "backtest and live, one code" spirit extends to
"every surface shows the signal the same way", so the n8n node template and
fmt_status never drift apart from each other again.

Layout (per project request, 2026-10-03): quote-block isolates SL/TP so
they're never confused with each other at a glance, fixed section order,
one summary line, and the price AT THE MOMENT the signal fired reported
explicitly (not just buried in "entry").
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from src.news.persian_calendar import jalali_day_header_fa, to_persian_digits

STRUCTURE_LABEL_FA = {
    "bullish": "صعودی ↑",
    "bearish": "نزولی ↓",
    "unknown": "نامشخص",
    None: "نامشخص",
}

OUTCOME_LABEL_FA = {
    "tp": "🏁 بسته شد — حد سود (TP)",
    "sl": "🏁 بسته شد — حد ضرر (SL)",
    "level_invalidated": "🏁 بسته شد — سطح نقض شد",
    "timeout": "🏁 بسته شد — پایان بازه",
    "open": "⏳ باز",
}

DIRECTION_LABEL_FA = {"BUY": "خرید (BUY)", "SELL": "فروش (SELL)"}
LEVEL_KIND_FA = {"support": "حمایت", "resistance": "مقاومت"}

ALERT_DISCLAIMER = (
    "⚠️ این سیگنال آزمایشی است و هنوز تایید نهایی نشده؛ "
    "مسئولیت هر تصمیم معاملاتی با شماست."
)

# Shared visual language across EVERY Telegram message this project sends
# (signal alerts, reversal advisories, and -- per the 2026-10-03 follow-up --
# /status, /levels, /gaps, /start too): one divider under the header, one
# <blockquote> per "block" of related facts, HTML parse_mode throughout.
# Exported (not module-private) so src/telegram_bot/formatters.py reuses the
# exact same divider/time rendering instead of a second, driftable copy.
DIVIDER = "━━━━━━━━━━━━━━━"


def tehran_time_fa(ts_iso_or_dt: "str | datetime") -> str:
    """UTC ISO string or datetime -> 'جمعه ۱۲ مهر، ساعت ۱۴:۳۵' (CLAUDE.md
    rule 4: storage/computation stays UTC, this is a DISPLAY conversion)."""
    dt = ts_iso_or_dt if isinstance(ts_iso_or_dt, datetime) else datetime.fromisoformat(ts_iso_or_dt)
    if dt.tzinfo is None:
        from datetime import timezone
        dt = dt.replace(tzinfo=timezone.utc)
    day = jalali_day_header_fa(dt)
    hm = to_persian_digits(dt.astimezone(_TEHRAN()).strftime("%H:%M"))
    return f"{day}، ساعت {hm}"


def escape_html(text: str) -> str:
    """Escape user/exception-derived text before it goes inside a Telegram
    HTML-parse-mode message -- an exception string containing a stray '<'
    or '&' would otherwise break rendering (or silently drop the rest of
    the message) once every bot reply moved to parse_mode='HTML'."""
    return (
        str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def _TEHRAN():
    from zoneinfo import ZoneInfo
    return ZoneInfo("Asia/Tehran")


def _f(x, nd=2):
    return f"{float(x):.{nd}f}" if x is not None else "?"


def build_signal_alert_text(signal: dict[str, Any]) -> str:
    """The new-signal alert: what n8n sends the moment a signal fires, and
    what /status (fmt_status) replays for the most recently STORED signal.

    Required keys: direction, entry, ts_utc, stop_loss, take_profit,
    components (level_kind, level_price_low, level_price_high,
    level_strength, atr, net_votes, sl_tp, market_structure), rule_version.
    Any missing field degrades to '?' rather than raising -- same
    fail-soft convention as the existing formatters.
    """
    comp = signal.get("components") or {}
    direction = signal.get("direction", "?")
    arrow = "🟢" if direction == "BUY" else "🔴" if direction == "SELL" else "⚪"
    dir_label = DIRECTION_LABEL_FA.get(direction, direction)
    symbol = comp.get("symbol", "XAUUSD@")

    entry = signal.get("entry")
    sl = signal.get("stop_loss")
    tp = signal.get("take_profit")

    sl_tp = comp.get("sl_tp") or {}
    rr = sl_tp.get("rr")
    rr_note = ""
    if sl_tp.get("relaxed"):
        rr_note = " (زیر نسبت استاندارد ۲:۱ — فقط چون اطمینان رأی‌گیری بالا بود)"

    level_kind = LEVEL_KIND_FA.get(comp.get("level_kind"), comp.get("level_kind", "?"))
    level_lo, level_hi = comp.get("level_price_low"), comp.get("level_price_high")
    strength, atr = comp.get("level_strength"), comp.get("atr")
    structure = STRUCTURE_LABEL_FA.get(comp.get("market_structure"), comp.get("market_structure", "نامشخص"))
    net_votes = comp.get("net_votes")

    price_line = f"💰 قیمت در لحظه‌ی صدور: <b>{_f(entry, 3)}</b>"
    current_price = signal.get("current_price")
    if current_price is not None and entry is not None and signal.get("outcome") is None:
        sign = 1 if direction == "BUY" else -1
        delta = (float(current_price) - float(entry)) * sign
        price_line += (
            f"\n💰 قیمت فعلی: <b>{_f(current_price, 3)}</b> "
            f"({'+' if delta >= 0 else ''}{_f(delta, 2)} دلار)"
        )

    lines = [
        f"{arrow} <b>سیگنال {dir_label}</b> — {symbol}",
        DIVIDER,
        price_line,
        "",
        "<blockquote>"
        f"🎯 حد سود (TP): <b>{_f(tp, 3)}</b>\n"
        f"🛑 حد ضرر (SL): <b>{_f(sl, 3)}</b>\n"
        f"⚖️ نسبت سود:ضرر: {_f(rr, 2) if rr is not None else '?'}:1{rr_note}"
        "</blockquote>",
        "",
        f"📊 سطح برخوردی: {level_kind} [{_f(level_lo, 2)} – {_f(level_hi, 2)}]",
        f"💪 قدرت سطح: {_f(strength, 2)}  |  ATR: {_f(atr, 2)}",
        f"🧭 ساختار H1: {structure}",
        f"🗳️ رأی ماژول‌ها: {net_votes if net_votes is not None else '?'}",
    ]

    outcome = signal.get("outcome")
    if outcome:
        lines.append(OUTCOME_LABEL_FA.get(outcome, f"🏁 نتیجه: {outcome}"))

    ts = signal.get("ts_utc")
    lines += [
        "",
        f"🕐 زمان (تهران): {tehran_time_fa(ts)}" if ts else "🕐 زمان: ?",
        f"📘 قانون: {signal.get('rule_version', '?')}",
        "",
        ALERT_DISCLAIMER,
    ]
    return "\n".join(lines)


def build_reversal_advisory_text(advisory: dict[str, Any], current_price: float | None = None) -> str:
    """'توقف اجباری': H1 structure flipped against an open position.

    current_price: the latest closed candle's close at the moment this
    advisory fires (reported alongside entry so the user sees the floating
    move, not just the open direction) -- caller passes it since advisory
    dicts built by src/api/main.py._check_reversal_close_sync don't already
    carry it (see that function's own latest_candle fetch).
    """
    direction = advisory.get("direction", "?")
    arrow = "🟢" if direction == "BUY" else "🔴" if direction == "SELL" else "⚪"
    dir_label = DIRECTION_LABEL_FA.get(direction, direction)
    entry = advisory.get("entry")
    structure = STRUCTURE_LABEL_FA.get(advisory.get("structure"), advisory.get("structure", "نامشخص"))

    move_line = ""
    if current_price is not None and entry is not None:
        sign = 1 if direction == "BUY" else -1
        delta = (float(current_price) - float(entry)) * sign
        move_line = f"\n💰 قیمت فعلی: <b>{_f(current_price, 3)}</b> ({'+' if delta >= 0 else ''}{_f(delta, 2)} دلار)"

    lines = [
        "⚠️ <b>توقف اجباری</b>",
        DIVIDER,
        f"{arrow} پوزیشن باز: {dir_label} از {tehran_time_fa(advisory['ts_utc'])}" if advisory.get("ts_utc") else f"{arrow} پوزیشن باز: {dir_label}",
        f"💰 قیمت ورود: <b>{_f(entry, 3)}</b>{move_line}",
        "",
        f"<blockquote>🧭 ساختار H1 جدید: {structure} (برخلاف این پوزیشن)</blockquote>",
        "",
        "📣 این سیگنال باز را دستی ببندید.",
        "",
        ALERT_DISCLAIMER,
    ]
    return "\n".join(lines)
