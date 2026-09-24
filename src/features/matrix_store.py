"""SPEC.md 4.10 (`matrix`) -- DB read/write layer.

compute_matrix_score() in matrix.py is pure (no DB); this module fetches
each of the 7 timeframes independently (SPEC.md 4.10 IO split) and handles
storage.
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
    conn, symbol: str, tf: str, as_of_ts: datetime, lookback_bars: int | None = None,
) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        if lookback_bars is None:
            cur.execute(
                """
                SELECT ts_utc, open, high, low, close, tick_volume
                FROM candles
                WHERE symbol = %s AND tf = %s AND ts_utc <= %s
                ORDER BY ts_utc
                """,
                (symbol, tf, as_of_ts),
            )
        else:
            cur.execute(
                """
                SELECT ts_utc, open, high, low, close, tick_volume FROM (
                    SELECT ts_utc, open, high, low, close, tick_volume
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


def fetch_candles_by_tf(
    conn, symbol: str, tf_list: list[str], as_of_ts: datetime, lookback_bars: int | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Fetches each timeframe with its own separate query (SPEC.md 4.10:
    'لایه IO جدا: fetch از DB برای هر ۷ تایم‌فریم') -- not a single joined
    query, so one timeframe's absence/lag never blocks the others."""
    return {tf: fetch_candles(conn, symbol, tf, as_of_ts, lookback_bars) for tf in tf_list}


def upsert_matrix_snapshot(conn, symbol: str, ts_utc: datetime, result: dict[str, Any]) -> dict[str, int]:
    """Natural key: (symbol, ts_utc) -- ts_utc is the M5 pivot bar's own
    timestamp. ON CONFLICT UPDATE: a snapshot can legitimately be
    recomputed (e.g. wider lookback_bars re-run), same convention as
    rsi_snapshots."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO matrix_snapshots (symbol, ts_utc, score, direction, timeframes)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (symbol, ts_utc) DO UPDATE SET
                score      = EXCLUDED.score,
                direction  = EXCLUDED.direction,
                timeframes = EXCLUDED.timeframes
            """,
            (symbol, ts_utc, result["score"], result["direction"], psycopg2.extras.Json(result["timeframes"])),
        )
    return {"upserted": 1}


def fetch_latest_matrix_snapshot(conn, symbol: str, as_of_ts: datetime) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT symbol, ts_utc, score, direction, timeframes
            FROM matrix_snapshots
            WHERE symbol = %s AND ts_utc <= %s
            ORDER BY ts_utc DESC
            LIMIT 1
            """,
            (symbol, as_of_ts),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))


def register_proposed_rule(conn, params: dict[str, Any]) -> None:
    """rules registry (SPEC.md 4.16): matrix_score starts status='proposed'.
    Idempotent -- DO NOTHING on conflict so a status a human/backtest has
    since advanced isn't reset (same convention as rsi_store.py)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO rules (id, statement, origin, status, params, created_at)
            VALUES (%s, %s, %s, 'proposed', %s, NOW())
            ON CONFLICT (id) DO NOTHING
            """,
            (
                "matrix_score_mtf_agreement",
                "Multi-timeframe indicator voting: matrix_score (0-6) counts how many of "
                "M1/M15/M30/H1/H4/D1 agree with the M5-pivot direction from a 10-indicator "
                "vote per timeframe (>=min_votes agreement to vote). Fusion input only, "
                "like regime -- never issues a signal by itself.",
                "SPEC.md 4.10, decision D18, MQL5 product/75011",
                psycopg2.extras.Json({
                    "tf_list": params["tf_list"],
                    "min_votes": params["min_votes"],
                    "min_tf_agreement": params["min_tf_agreement"],
                }),
            ),
        )
