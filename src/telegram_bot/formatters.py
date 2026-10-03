"""Message formatters for the XAUUSD assistant Telegram bot.

Pure functions: input is an API response dict, output is a Telegram-ready
string. No DB access, no HTTP calls — easy to unit-test with mock data.
"""
from __future__ import annotations

from src.engine.signal_formatter import build_signal_alert_text

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
        lines = ["📭 سیگنال فعال: ندارد"]
        if as_of:
            lines.append(f"🕐 آخرین بررسی: {as_of}")
        lines.append(f"دلیل: {reason}")
        return "\n".join(lines) + DISCLAIMER

    if "telegram_html" in sig:
        return sig["telegram_html"]
    return build_signal_alert_text(sig)


def fmt_levels(data: dict) -> str:
    """Format /levels/near-price response."""
    price = data.get("price")
    sup = data.get("support")
    res = data.get("resistance")

    lines = [f"📍 قیمت فعلی: {price}"]

    if res:
        lines.append(
            f"🔴 مقاومت نزدیک: [{res['price_low']:.2f}–{res['price_high']:.2f}]"
            f"  (فاصله: {res['distance']:.2f})  قدرت: {res['strength']:.2f}"
        )
    else:
        lines.append("🔴 مقاومت: پیدا نشد")

    if sup:
        lines.append(
            f"🟢 حمایت نزدیک: [{sup['price_low']:.2f}–{sup['price_high']:.2f}]"
            f"  (فاصله: {sup['distance']:.2f})  قدرت: {sup['strength']:.2f}"
        )
    else:
        lines.append("🟢 حمایت: پیدا نشد")

    return "\n".join(lines) + DISCLAIMER


def fmt_gaps(data: dict) -> str:
    """Format /gaps/open response."""
    gaps = data if isinstance(data, list) else data.get("gaps", [])
    if not gaps:
        return "📭 گپ باز: ندارد" + DISCLAIMER

    lines = ["📐 گپ‌های باز:"]
    for g in gaps[:5]:
        status = g.get("status", "?")
        hi = g.get("gap_high", "?")
        lo = g.get("gap_low", "?")
        weight = g.get("weight")
        w_s = f"{weight:.2f}" if weight is not None else "?"
        ts = g.get("ts_utc", "")
        lines.append(f"  • {status}: [{lo}–{hi}]  وزن: {w_s}  ({ts[:10]})")

    return "\n".join(lines) + DISCLAIMER


def fmt_news(data: dict) -> str:
    """Format /news/upcoming response. The backend (build_upcoming_digest,
    src/news/news_reporter.py) already renders the full Telegram-HTML digest
    — grouped by Jalali day, one disclaimer — so this is a thin pass-through.
    Send with parse_mode="HTML" (see src/telegram_bot/bot.py cmd_news)."""
    digest = data.get("digest")
    if digest:
        return digest
    if not data.get("events"):
        return "📭 رویداد اقتصادی پیش‌رو: ندارد" + DISCLAIMER
    return f"📰 رویدادهای پیش‌رو ({data.get('count', 0)} رویداد)" + DISCLAIMER
