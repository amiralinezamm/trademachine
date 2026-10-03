"""Message formatters for the XAUUSD assistant Telegram bot.

Pure functions: input is an API response dict, output is a Telegram-ready
string. No DB access, no HTTP calls — easy to unit-test with mock data.
"""
from __future__ import annotations

from src.engine.signal_formatter import DIVIDER, build_signal_alert_text, escape_html

# 2026-10-03: every bot reply now shares the signal alert's visual language
# (DIVIDER under the header, one <blockquote> per fact-block, HTML parse
# mode throughout -- see src/telegram_bot/bot.py, every _reply call passes
# parse_mode="HTML"). DISCLAIMER text is unescaped literal HTML-safe Farsi
# (no '<'/'&'), so it's fine to append directly everywhere below.
DISCLAIMER = (
    "\n\n⚠️ این سیگنال‌ها آزمایشی‌اند و هنوز تایید نهایی نشده‌اند؛ "
    "مسئولیت هر تصمیم معاملاتی با شماست."
)


def fmt_status(data: dict) -> str:
    """Format /signal/last (the latest STORED signal — see
    src/engine/signal_store.py fetch_latest_signal for why this is a
    separate endpoint from /signal/latest). Renders the SAME layout the
    n8n alert sends (src/engine/signal_formatter.build_signal_alert_text)
    so /status never shows the signal differently from the original
    alert. Send with parse_mode="HTML" (see src/telegram_bot/bot.py)."""
    sig = data.get("signal")
    if sig is None:
        reason = data.get("reason", "")
        as_of = data.get("as_of")
        lines = ["📭 <b>سیگنال فعال:</b> ندارد", DIVIDER]
        if as_of:
            lines.append(f"🕐 آخرین بررسی: {as_of}")
        lines.append(f"دلیل: {escape_html(reason)}")
        return "\n".join(lines) + DISCLAIMER

    if "telegram_html" in sig:
        return sig["telegram_html"]
    return build_signal_alert_text(sig)


def fmt_levels(data: dict) -> str:
    """Format /levels/near-price response. Same visual language as the
    signal alert: price at the moment of the check up top, each level in
    its own quote block so support/resistance are never read as one
    blurred line."""
    price = data.get("price")
    sup = data.get("support")
    res = data.get("resistance")

    price_s = f"{price:.2f}" if price is not None else "?"
    lines = [f"📍 <b>قیمت در لحظه‌ی بررسی:</b> {price_s}", DIVIDER]

    if res:
        lines.append(
            "<blockquote>"
            f"🔴 مقاومت نزدیک: [{res['price_low']:.2f} – {res['price_high']:.2f}]\n"
            f"فاصله: {res['distance']:.2f}  |  قدرت: {res['strength']:.2f}"
            "</blockquote>"
        )
    else:
        lines.append("🔴 مقاومت: پیدا نشد")

    if sup:
        lines.append(
            "<blockquote>"
            f"🟢 حمایت نزدیک: [{sup['price_low']:.2f} – {sup['price_high']:.2f}]\n"
            f"فاصله: {sup['distance']:.2f}  |  قدرت: {sup['strength']:.2f}"
            "</blockquote>"
        )
    else:
        lines.append("🟢 حمایت: پیدا نشد")

    return "\n".join(lines) + DISCLAIMER


def fmt_gaps(data: dict) -> str:
    """Format /gaps/open response. One quote block per gap (max 5),
    newest first, status/range/weight/date in a fixed order."""
    gaps = data if isinstance(data, list) else data.get("gaps", [])
    if not gaps:
        return "📭 <b>گپ باز:</b> ندارد" + DISCLAIMER

    lines = ["📐 <b>گپ‌های باز</b>", DIVIDER]
    for g in gaps[:5]:
        status = g.get("status", "?")
        hi = g.get("gap_high", "?")
        lo = g.get("gap_low", "?")
        weight = g.get("weight")
        w_s = f"{weight:.2f}" if weight is not None else "?"
        ts = g.get("ts_utc", "")
        lines.append(
            "<blockquote>"
            f"{status}: [{lo} – {hi}]\n"
            f"وزن: {w_s}  |  تاریخ: {ts[:10]}"
            "</blockquote>"
        )

    return "\n".join(lines) + DISCLAIMER


def fmt_start() -> str:
    """The /start welcome text, in the same visual language as every other
    reply (DIVIDER + one quote block for the command list)."""
    lines = [
        "👋 <b>ربات دستیار XAUUSD</b>",
        DIVIDER,
        "<blockquote>"
        "/status — آخرین سیگنال\n"
        "/levels — سطوح حمایت/مقاومت نزدیک\n"
        "/gaps — گپ‌های باز\n"
        "/news — رویدادهای اقتصادی پیش‌رو"
        "</blockquote>",
        "",
        "⚠️ این ربات آزمایشی است — سیگنال‌ها تایید نهایی ندارند.",
    ]
    return "\n".join(lines)


def fmt_error(label: str, exc: object) -> str:
    """Uniform error reply for every command handler -- escapes the
    exception text since it's now sent with parse_mode='HTML' (an
    exception message containing a stray '<' used to risk breaking, or
    silently truncating, the Telegram render)."""
    return f"❌ <b>خطا در {label}:</b>\n{escape_html(exc)}"


def fmt_news(data: dict) -> str:
    """Format /news/upcoming response. The backend (build_upcoming_digest,
    src/news/news_reporter.py) already renders the full Telegram-HTML digest
    — grouped by Jalali day, one disclaimer — so this is a thin pass-through.
    Send with parse_mode="HTML" (see src/telegram_bot/bot.py cmd_news)."""
    digest = data.get("digest")
    if digest:
        return digest
    if not data.get("events"):
        return "📭 <b>رویداد اقتصادی پیش‌رو:</b> ندارد" + DISCLAIMER
    return f"📰 <b>رویدادهای پیش‌رو</b> ({data.get('count', 0)} رویداد)" + DISCLAIMER
