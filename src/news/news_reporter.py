"""SPEC.md 4.7-b — compose the post-release Telegram message.

Connects actual_watcher (detected actual), surprise.py (compute_surprise),
intensity_estimator.py (estimate_intensity), blackout.py (window_end),
and live DB price data into the exact SPEC-format message.

Pure function: build_release_message() takes all pre-computed inputs and
returns a formatted string. DB reads happen in get_price_move_from_db(),
kept separate so the core logic stays testable.
"""
from __future__ import annotations

import html
from datetime import datetime, timezone
from typing import Any

from src.news.persian_calendar import jalali_day_header_fa, tehran_calendar_day_key

DIRECTION_LABEL = {1: "صعودی ↑", -1: "نزولی ↓", 0: "بدون جهت", None: "نامشخص"}


def build_release_message(
    event: dict[str, Any],
    actual: str,
    surprise_result: dict[str, Any],
    intensity_result: dict[str, Any],
    price_move: dict[str, Any] | None,
    blackout_end: datetime | None,
) -> str:
    """Pure. Formats the SPEC 4.7-b Telegram message.

    Parameters
    ----------
    event          : {"title", "country", "impact", "ts_utc", "forecast", "previous"}
    actual         : string value from feed (e.g. "280K")
    surprise_result: output of compute_surprise()
    intensity_result: output of estimate_intensity()
    price_move     : {"delta": float, "seconds": int} | None  (live price move since release)
    blackout_end   : aware datetime of blackout window end, or None if no blackout
    """
    impact_emoji = "🔴" if event.get("impact") == "High" else "🟠"
    title = event.get("title", "رویداد")

    lines = [f"{impact_emoji} {title} منتشر شد"]

    forecast = event.get("forecast") or "—"
    previous = event.get("previous") or "—"
    lines.append(f"واقعی {actual}  |  انتظار {forecast}  |  قبلی {previous}")

    # Surprise line
    z = surprise_result.get("z_surprise")
    raw_dir = surprise_result.get("raw_direction")
    if z is not None:
        sign_str = "+" if z >= 0 else ""
        strength = "قوی‌تر از انتظار" if z > 0 else ("ضعیف‌تر از انتظار" if z < 0 else "مطابق انتظار")
        lines.append(f"سورپرایز: {sign_str}{z:.1f} انحراف معیار ({strength})")
    else:
        surp = surprise_result.get("surprise")
        if surp is not None:
            sign_str = "+" if surp >= 0 else ""
            lines.append(f"سورپرایز خام: {sign_str}{surp} (داده تاریخی کافی برای z نیست)")

    # Expected direction
    expected = surprise_result.get("expected_dir") or raw_dir
    lines.append(f"جهت مورد انتظار برای طلا: {DIRECTION_LABEL.get(expected, 'نامشخص')}")

    # Live price move
    if price_move is not None and price_move.get("delta") is not None:
        delta = price_move["delta"]
        secs = price_move.get("seconds", 0)
        sign_str = "+" if delta >= 0 else ""
        lines.append(f"حرکت واقعی تا این لحظه: {sign_str}{delta:.2f} دلار در {secs} ثانیه")

    # Historical intensity
    if intensity_result.get("sufficient_data"):
        lines.append(f"سابقه: {intensity_result['message']}")
    else:
        lines.append("سابقه: داده کافی برای برآورد شدت وجود ندارد")

    # Blackout
    if blackout_end is not None:
        tehran_str = _to_tehran_hhmm(blackout_end)
        lines.append(f"⛔ سیگنال تا ساعت {tehran_str} مسدود است")

    lines.append("⚠️ سیگنال‌های آزمایشی — مسئولیت با شماست")
    return "\n".join(lines)


def build_upcoming_message(event: dict[str, Any], surprise_result: dict[str, Any]) -> str:
    """Pure. Preview for an upcoming event that has no actual yet (کار ۵ /news).
    Shows schedule + expected direction only.
    """
    impact_emoji = "🔴" if event.get("impact") == "High" else "🟠"
    title = event.get("title", "رویداد")
    ts_tehran = _to_tehran_hhmm(event["ts_utc"])

    lines = [f"{impact_emoji} {title} ({event.get('country', '')} — {event.get('impact', '')})"]
    lines.append(f"زمان: {ts_tehran} (تهران)")
    forecast = event.get("forecast") or "—"
    previous = event.get("previous") or "—"
    lines.append(f"انتظار: {forecast}  |  قبلی: {previous}")

    gold_sign = surprise_result.get("gold_sign")
    if gold_sign is not None:
        lines.append(f"جهت انتظاری در صورت بالا بودن: {DIRECTION_LABEL.get(gold_sign, '—')}")
    else:
        lines.append("جهت: نامشخص (رویداد در نقشه یافت نشد)")

    lines.append("⚠️ سیگنال‌های آزمایشی — مسئولیت با شماست")
    return "\n".join(lines)


def _upcoming_event_block(event: dict[str, Any], surprise_result: dict[str, Any]) -> str:
    """One event, formatted as a Telegram HTML blockquote — no per-event
    disclaimer (that's added once by build_upcoming_digest)."""
    impact_emoji = "🔴" if event.get("impact") == "High" else "🟠"
    title = html.escape(str(event.get("title", "رویداد")))
    ts_tehran = _to_tehran_hhmm(event["ts_utc"])
    forecast = html.escape(str(event.get("forecast") or "—"))
    previous = html.escape(str(event.get("previous") or "—"))

    lines = [f"{impact_emoji} <b>{title}</b> ({event.get('country', '')})"]
    lines.append(f"ساعت {ts_tehran}")
    lines.append(f"انتظار: {forecast}  |  قبلی: {previous}")

    gold_sign = surprise_result.get("gold_sign")
    if gold_sign is not None:
        lines.append(f"در صورت بالاتر از انتظار: {DIRECTION_LABEL.get(gold_sign, '—')}")
    else:
        lines.append("جهت: هنوز در نقشه ثبت نشده")

    return "<blockquote>" + "\n".join(lines) + "</blockquote>"


def build_upcoming_digest(
    events: list[dict[str, Any]],
    header: str = "🗞 <b>رویدادهای اقتصادی پیش‌رو</b>",
) -> str:
    """Groups upcoming events by Tehran-local calendar day (Jalali) into one
    Telegram HTML message — one disclaimer at the end, not one per event.

    Each item in `events` must be {"event": {...same shape as
    build_upcoming_message's `event`...}, "surprise_result": {...}}.
    """
    disclaimer = "⚠️ سیگنال‌های آزمایشی — مسئولیت با شماست"
    if not events:
        return f"{header}\n\nرویداد اقتصادی پیش‌رویی ثبت نشده.\n\n{disclaimer}"

    blocks: list[str] = [header]
    current_day: Any = None
    for item in events:
        event = item["event"]
        day_key = tehran_calendar_day_key(event["ts_utc"])
        if day_key != current_day:
            current_day = day_key
            day_header = jalali_day_header_fa(event["ts_utc"])
            blocks.append(f"\n<b>{day_header}</b>\n――――――――――――")
        blocks.append(_upcoming_event_block(event, item["surprise_result"]))

    blocks.append(f"\n{disclaimer}")
    return "\n".join(blocks)


def _to_tehran_hhmm(dt: datetime) -> str:
    """UTC datetime → Tehran local time string (HH:MM).
    Iran permanently abolished DST on 22 Sep 2022 — UTC+3:30 all year.
    Uses zoneinfo (stdlib) so any future policy change is handled by tzdata, not hardcoded math.
    For display only — do NOT use for time arithmetic.
    """
    from zoneinfo import ZoneInfo
    return dt.astimezone(ZoneInfo("Asia/Tehran")).strftime("%H:%M")


def get_price_move_from_db(conn, symbol: str, tf: str, event_ts: datetime) -> dict[str, Any]:
    """IO. Reads the price change since event_ts from the candles table.

    Returns the close delta between the candle at/just after event_ts and the
    most recent closed candle. Delta is signed (positive = price rose).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT close FROM candles
            WHERE symbol = %s AND tf = %s AND ts_utc >= %s
            ORDER BY ts_utc ASC LIMIT 1
            """,
            (symbol, tf, event_ts),
        )
        row_at = cur.fetchone()
        if row_at is None:
            return {"delta": None, "seconds": None}
        price_at = float(row_at[0])

        cur.execute(
            """
            SELECT close, ts_utc FROM candles
            WHERE symbol = %s AND tf = %s AND ts_utc > %s
            ORDER BY ts_utc DESC LIMIT 1
            """,
            (symbol, tf, event_ts),
        )
        row_now = cur.fetchone()
        if row_now is None:
            return {"delta": None, "seconds": None}
        price_now = float(row_now[0])
        ts_now = row_now[1]

        delta_t = (ts_now.replace(tzinfo=timezone.utc) - event_ts.replace(tzinfo=timezone.utc))
        return {
            "delta": round(price_now - price_at, 3),
            "seconds": int(delta_t.total_seconds()),
        }
