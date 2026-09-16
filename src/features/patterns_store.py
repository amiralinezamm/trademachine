"""SPEC.md 4.3 (`patterns`) — DB read/write half.

compute_patterns() in patterns.py is pure (no DB); this module handles
the DB layer, shared by the API endpoint and CLI tools.
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


def fetch_candles_for_patterns(
    conn, symbol: str, tf: str, as_of_ts: datetime
) -> list[dict[str, Any]]:
    """Candles up to as_of_ts — the cutoff is enforced again inside
    compute_patterns() itself; this is just an efficiency pre-filter."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ts_utc, open, high, low, close
            FROM candles
            WHERE symbol = %s AND tf = %s AND ts_utc <= %s
            ORDER BY ts_utc
            """,
            (symbol, tf, as_of_ts),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def fetch_active_levels(
    conn, symbol: str, tf: str
) -> list[dict[str, Any]]:
    """Active and flipped levels including their DB id (needed for at_level_id FK)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, kind, price_low, price_high, strength, status
            FROM levels
            WHERE symbol = %s AND tf_origin = %s AND status IN ('active', 'flipped')
            """,
            (symbol, tf),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def upsert_pattern_hits(conn, hits: list[dict[str, Any]]) -> dict[str, int]:
    """Primary key is (ts_utc, tf, pattern) — ON CONFLICT DO NOTHING (idempotent).
    The 'symbol' convenience field in the hit dict is stripped before insert
    (not a column in the pattern_hits table schema)."""
    if not hits:
        return {"inserted": 0, "skipped": 0}
    rows = [
        (
            h["ts_utc"],
            h["tf"],
            h["pattern"],
            int(h["direction"]),
            float(h["body_atr"]) if h["body_atr"] is not None else None,
            h.get("at_level_id"),
            float(h["level_strength"]) if h.get("level_strength") is not None else None,
            h.get("regime"),
        )
        for h in hits
    ]
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO pattern_hits
                (ts_utc, tf, pattern, direction, body_atr,
                 at_level_id, level_strength, regime)
            VALUES %s
            ON CONFLICT (ts_utc, tf, pattern) DO NOTHING
            """,
            rows,
        )
        inserted = cur.rowcount if cur.rowcount >= 0 else 0
    return {"inserted": inserted, "skipped": len(rows) - inserted}
