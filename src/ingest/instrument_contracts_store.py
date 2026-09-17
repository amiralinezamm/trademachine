"""DB helpers for instrument_contracts (docs/instrument_rollover.md).

Tracks which physical MT5 contract each logical instrument symbol (DXY@,
T10Y@, BRENT@) is currently mapped to, and the back-adjustment offset
applied to each historical segment.
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg2
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


def upsert_instrument_contract(
    conn,
    logical_symbol: str,
    physical_symbol: str,
    valid_from_ts: datetime,
    valid_to_ts: datetime | None,
    adjustment_offset: float,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO instrument_contracts
                (logical_symbol, physical_symbol, valid_from_ts, valid_to_ts, adjustment_offset)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (logical_symbol, physical_symbol, valid_from_ts)
            DO UPDATE SET
                valid_to_ts = EXCLUDED.valid_to_ts,
                adjustment_offset = EXCLUDED.adjustment_offset
            """,
            (logical_symbol, physical_symbol, valid_from_ts, valid_to_ts, adjustment_offset),
        )


def fetch_active_contract(conn, logical_symbol: str) -> dict[str, Any] | None:
    """The contract currently mapped (valid_to_ts IS NULL) for this logical symbol."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT logical_symbol, physical_symbol, valid_from_ts, valid_to_ts, adjustment_offset
            FROM instrument_contracts
            WHERE logical_symbol = %s AND valid_to_ts IS NULL
            ORDER BY valid_from_ts DESC
            LIMIT 1
            """,
            (logical_symbol,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))


def close_contract(conn, logical_symbol: str, physical_symbol: str, valid_to_ts: datetime) -> None:
    """Marks a contract segment as ended (used when a real rollover happens)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE instrument_contracts
            SET valid_to_ts = %s
            WHERE logical_symbol = %s AND physical_symbol = %s AND valid_to_ts IS NULL
            """,
            (valid_to_ts, logical_symbol, physical_symbol),
        )


def insert_rollover_alert(
    conn,
    logical_symbol: str,
    old_physical_symbol: str | None,
    new_physical_symbol: str,
    adjustment_offset: float | None,
    offset_computed: bool,
) -> None:
    """One row per detected front-month change, for human review (docs/
    instrument_rollover.md's daily automated check). Never auto-deleted."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO rollover_alerts
                (logical_symbol, old_physical_symbol, new_physical_symbol, adjustment_offset, offset_computed)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (logical_symbol, old_physical_symbol, new_physical_symbol, adjustment_offset, offset_computed),
        )


def fetch_rollover_alerts(conn, acknowledged: bool | None = None) -> list[dict[str, Any]]:
    query = "SELECT id, logical_symbol, old_physical_symbol, new_physical_symbol, adjustment_offset, offset_computed, detected_at, acknowledged FROM rollover_alerts"
    params: tuple = ()
    if acknowledged is not None:
        query += " WHERE acknowledged = %s"
        params = (acknowledged,)
    query += " ORDER BY detected_at DESC"
    with conn.cursor() as cur:
        cur.execute(query, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
