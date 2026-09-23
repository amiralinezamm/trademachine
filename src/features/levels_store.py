"""SPEC.md 4.2 (`levels`) — DB read/write half. compute_levels() itself
stays pure (no DB); this module fetches candles and upserts results,
shared by the CLI/report tooling and the API endpoint (CLAUDE.md rule 6).
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv


def get_connection():
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    return psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST_LOCAL", "127.0.0.1"),
        port=os.environ.get("POSTGRES_PORT", "5432"),
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        dbname=os.environ["POSTGRES_DB"],
    )


def fetch_candles(
    conn, symbol: str, tf: str, as_of_ts: datetime,
    lookback_bars: int | None = None,
) -> list[dict[str, Any]]:
    """All closed candles up to as_of_ts.

    lookback_bars: when set, fetches only the most recent N bars (DESC LIMIT
    then reversed to ASC). None (default) fetches the full history — required
    by replay/backtest callers; never pass a number there.
    """
    with conn.cursor() as cur:
        if lookback_bars is None:
            cur.execute(
                """
                SELECT ts_utc, open, high, low, close
                FROM candles
                WHERE symbol = %s AND tf = %s AND ts_utc <= %s
                ORDER BY ts_utc
                """,
                (symbol, tf, as_of_ts),
            )
        else:
            cur.execute(
                """
                SELECT ts_utc, open, high, low, close FROM (
                    SELECT ts_utc, open, high, low, close
                    FROM candles
                    WHERE symbol = %s AND tf = %s AND ts_utc <= %s
                    ORDER BY ts_utc DESC
                    LIMIT %s
                ) sub ORDER BY ts_utc ASC
                """,
                (symbol, tf, as_of_ts, lookback_bars),
            )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def upsert_levels(conn, levels: list[dict[str, Any]]) -> dict[str, int]:
    """Natural key (symbol, tf_origin, created_ts) — see migration
    007_levels_natural_key.sql. A role flip changes `kind` on the SAME row,
    it never creates a new one."""
    if not levels:
        return {"inserted": 0, "updated": 0}
    # A single bar can qualify as both swing-high and swing-low, producing two
    # levels with the same natural key. Deduplicate — keep the last entry per
    # key (most up-to-date state after the walk-forward loop).
    seen: dict[tuple, dict] = {}
    for lvl in levels:
        seen[(lvl["symbol"], lvl["tf_origin"], lvl["created_ts"])] = lvl
    levels = list(seen.values())
    rows = [
        (
            lvl["symbol"], lvl["tf_origin"], lvl["kind"], lvl["price_low"], lvl["price_high"],
            lvl["created_ts"], lvl["last_touch"], lvl["touch_count"], lvl["break_count"],
            lvl["strength"], lvl["status"], lvl["atr_at_birth"],
        )
        for lvl in levels
    ]
    with conn.cursor() as cur:
        results = psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO levels
                (symbol, tf_origin, kind, price_low, price_high, created_ts,
                 last_touch, touch_count, break_count, strength, status, atr_at_birth)
            VALUES %s
            ON CONFLICT (symbol, tf_origin, created_ts) DO UPDATE SET
                kind = EXCLUDED.kind,
                price_low = EXCLUDED.price_low,
                price_high = EXCLUDED.price_high,
                last_touch = EXCLUDED.last_touch,
                touch_count = EXCLUDED.touch_count,
                break_count = EXCLUDED.break_count,
                strength = EXCLUDED.strength,
                status = EXCLUDED.status
            RETURNING (xmax = 0) AS was_insert
            """,
            rows,
            fetch=True,
        )
        inserted = sum(1 for (was_insert,) in results if was_insert)
        return {"inserted": inserted, "updated": len(results) - inserted}
