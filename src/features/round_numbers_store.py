"""SPEC.md 4.4 (`round_numbers`) — DB read/write layer.

compute_round_numbers() in round_numbers.py is pure (no DB); this module
handles the DB layer, shared by the API endpoint and CLI tools.
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


def fetch_candles_with_volume(
    conn, symbol: str, tf: str, as_of_ts: datetime
) -> list[dict[str, Any]]:
    """Candles including tick_volume, up to as_of_ts."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ts_utc, open, high, low, close, tick_volume
            FROM candles
            WHERE symbol = %s AND tf = %s AND ts_utc <= %s
            ORDER BY ts_utc
            """,
            (symbol, tf, as_of_ts),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def upsert_round_number_hits(
    conn, hits: list[dict[str, Any]]
) -> dict[str, int]:
    """Upsert round_number_hits. Natural key: (symbol, tf, ts_utc, level, state).
    ON CONFLICT DO NOTHING — idempotent, same as pattern_hits."""
    if not hits:
        return {"inserted": 0, "skipped": 0}
    rows = [
        (
            h["symbol"],
            h["tf"],
            h["ts_utc"],
            float(h["level"]),
            int(h["multiplier"]),
            float(h["weight"]),
            h["state"],
            h["direction"],
            h["approach_ts"],
        )
        for h in hits
    ]
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO round_number_hits
                (symbol, tf, ts_utc, level, multiplier, weight,
                 state, direction, approach_ts)
            VALUES %s
            ON CONFLICT (symbol, tf, ts_utc, level, state) DO NOTHING
            """,
            rows,
        )
        inserted = cur.rowcount if cur.rowcount >= 0 else 0
    return {"inserted": inserted, "skipped": len(rows) - inserted}


def fetch_round_number_hits(
    conn,
    symbol: str,
    tf: str,
    as_of_ts: datetime,
    states: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Fetch stored hits up to as_of_ts, optionally filtered by state."""
    with conn.cursor() as cur:
        if states:
            cur.execute(
                """
                SELECT symbol, tf, ts_utc, level, multiplier, weight,
                       state, direction, approach_ts
                FROM round_number_hits
                WHERE symbol = %s AND tf = %s AND ts_utc <= %s
                  AND state = ANY(%s)
                ORDER BY ts_utc
                """,
                (symbol, tf, as_of_ts, states),
            )
        else:
            cur.execute(
                """
                SELECT symbol, tf, ts_utc, level, multiplier, weight,
                       state, direction, approach_ts
                FROM round_number_hits
                WHERE symbol = %s AND tf = %s AND ts_utc <= %s
                ORDER BY ts_utc
                """,
                (symbol, tf, as_of_ts),
            )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def acceptance_stats(conn, symbol: str, tf: str) -> dict[str, Any]:
    """SPEC 4.4 acceptance criterion: compare reversal rate near round numbers
    vs baseline (all bars).

    Returns:
      approaching_count: how many APPROACHING events
      reversal_count:    how many resolved as REVERSAL
      acceleration_count: how many resolved as ACCELERATION
      reversal_rate:     reversal_count / (reversal_count + acceleration_count)
      baseline_rate:     fraction of ALL M5 closes that are lower than the
                         previous close (a rough directionless baseline)
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT state, COUNT(*) FROM round_number_hits
            WHERE symbol=%s AND tf=%s
            GROUP BY state
            """,
            (symbol, tf),
        )
        by_state = {row[0]: row[1] for row in cur.fetchall()}

        approaching = by_state.get("APPROACHING", 0)
        reversal    = by_state.get("REVERSAL", 0)
        accel       = by_state.get("ACCELERATION", 0)
        resolved    = reversal + accel

        # Baseline: simple fraction of candles with negative returns
        cur.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT close, LAG(close) OVER (PARTITION BY symbol, tf ORDER BY ts_utc) AS prev_close
                FROM candles WHERE symbol=%s AND tf=%s
            ) sub WHERE close < prev_close
            """,
            (symbol, tf),
        )
        down_candles = cur.fetchone()[0]
        cur.execute(
            "SELECT COUNT(*) FROM candles WHERE symbol=%s AND tf=%s",
            (symbol, tf),
        )
        total_candles = cur.fetchone()[0]

    baseline = down_candles / total_candles if total_candles > 0 else 0.0
    rev_rate = reversal / resolved if resolved > 0 else None

    return {
        "approaching_count":    approaching,
        "reversal_count":       reversal,
        "acceleration_count":   accel,
        "reversal_rate":        rev_rate,
        "baseline_reversal_rate": baseline,
        "note": (
            "reversal_rate vs baseline_reversal_rate: if reversal_rate is "
            "significantly higher, the zone_atr_mult parameter is correctly "
            "capturing the effect. If not, widen/narrow zone_min_width."
        ),
    }
