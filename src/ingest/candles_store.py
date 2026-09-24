"""SPEC.md 4.1 (`candles` table) — shared write path.

Same function backs both the CLI backfill loader and the API endpoint
(CLAUDE.md rule 6: one code path, not two implementations that quietly
drift apart).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg2.extras

TF_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440}


def is_closed(ts_utc: datetime, tf: str, now_utc: datetime) -> bool:
    """CLAUDE.md rule 3: never store a candle that hasn't fully closed yet."""
    close_time = ts_utc + timedelta(minutes=TF_MINUTES[tf])
    return close_time <= now_utc


def filter_closed_candles(candles: list[dict[str, Any]], tf: str, now_utc: datetime | None = None) -> list[dict[str, Any]]:
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    return [c for c in candles if is_closed(c["ts_utc"], tf, now_utc)]


def upsert_candles(conn, symbol: str, tf: str, candles: list[dict[str, Any]]) -> int:
    """candles: list of dicts with ts_utc (aware datetime), open, high, low, close,
    tick_volume, spread, real_volume. Idempotent: PK conflict is a no-op, never a
    duplicate and never an error."""
    if not candles:
        return 0
    rows = [
        (
            symbol,
            tf,
            c["ts_utc"],
            c["open"],
            c["high"],
            c["low"],
            c["close"],
            c.get("tick_volume"),
            c.get("spread"),
            c.get("real_volume"),
        )
        for c in candles
    ]
    with conn.cursor() as cur:
        # execute_values pages internally (default page_size=100) and issues
        # one INSERT per page; cur.rowcount after the call only reflects the
        # LAST page, not the true total — use RETURNING + fetch=True instead,
        # which correctly accumulates results across all pages.
        inserted = psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO candles
                (symbol, tf, ts_utc, open, high, low, close, tick_volume, spread, real_volume)
            VALUES %s
            ON CONFLICT (symbol, tf, ts_utc) DO NOTHING
            RETURNING 1
            """,
            rows,
            fetch=True,
        )
        return len(inserted)
