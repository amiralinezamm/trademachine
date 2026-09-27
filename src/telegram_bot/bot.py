"""XAUUSD Assistant Telegram Bot.

Read-only window into the trading pipeline: /status /levels /gaps /news.
Calls internal FastAPI endpoints; no analytical logic here.

Run:
    python -m src.telegram_bot.bot

Requires TELEGRAM_BOT_TOKEN in .env (or environment).
Requires TELEGRAM_ASSISTANT_API_BASE (default: http://172.18.0.1:8000).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from src.telegram_bot.formatters import fmt_gaps, fmt_levels, fmt_news, fmt_status

load_dotenv()

log = logging.getLogger(__name__)

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
API_BASE = os.environ.get("TELEGRAM_ASSISTANT_API_BASE", "http://172.18.0.1:8000")
TIMEOUT = 20


# ── helpers ───────────────────────────────────────────────────────────────────

def _get(path: str, **params) -> dict:
    url = f"{API_BASE}{path}"
    r = httpx.get(url, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


async def _reply(update: Update, text: str, parse_mode: str | None = None) -> None:
    await update.message.reply_text(text, parse_mode=parse_mode)


# ── command handlers ──────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "سلام! دستورهای موجود:\n"
        "/status — آخرین سیگنال\n"
        "/levels — سطوح حمایت/مقاومت نزدیک\n"
        "/gaps   — گپ‌های باز\n"
        "/news   — رویدادهای اقتصادی پیش‌رو\n\n"
        "⚠️ این ربات آزمایشی است — سیگنال‌ها تایید نهایی ندارند."
    )
    await _reply(update, text)


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        data = _get("/signal/latest")
        await _reply(update, fmt_status(data))
    except Exception as exc:
        log.exception("cmd_status error")
        await _reply(update, f"❌ خطا در دریافت سیگنال: {exc}")


async def cmd_levels(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        data = _get("/levels/near-price")
        await _reply(update, fmt_levels(data))
    except Exception as exc:
        log.exception("cmd_levels error")
        await _reply(update, f"❌ خطا در دریافت سطوح: {exc}")


async def cmd_gaps(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        as_of = datetime.now(timezone.utc).isoformat()
        data = _get("/gaps/open", as_of=as_of)
        await _reply(update, fmt_gaps(data))
    except Exception as exc:
        log.exception("cmd_gaps error")
        await _reply(update, f"❌ خطا در دریافت گپ‌ها: {exc}")


async def cmd_news(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        data = _get("/news/upcoming")
        # digest is rendered with Telegram HTML (<b>, <blockquote>) server-side
        await _reply(update, fmt_news(data), parse_mode="HTML")
    except Exception as exc:
        log.exception("cmd_news error")
        await _reply(update, f"❌ خطا در دریافت اخبار: {exc}")


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("levels", cmd_levels))
    app.add_handler(CommandHandler("gaps", cmd_gaps))
    app.add_handler(CommandHandler("news", cmd_news))
    log.info("Bot polling started — API base: %s", API_BASE)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
