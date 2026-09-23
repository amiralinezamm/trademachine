"""SPEC.md 4.5 (`gaps`) — DB read/write layer."""
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
    """lookback_bars: when set, fetches only the most recent N bars. None
    (default) fetches the full history — required by backtest callers."""
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


def upsert_gaps(conn, gaps: list[dict[str, Any]]) -> dict[str, int]:
    """Upsert gaps. Natural key: (symbol, tf, ts_utc, direction).
    ON CONFLICT UPDATE fill status and weight (status can change as gaps fill)."""
    if not gaps:
        return {"upserted": 0}
    rows = [
        (
            g["symbol"],
            g["tf"],
            g["ts_utc"],
            g["direction"],
            float(g["gap_high"]),
            float(g["gap_low"]),
            float(g["size"]),
            float(g["initial_weight"]),
            float(g["weight"]),
            g["half_fill_ts"],
            g["fill_ts"],
            g["status"],
            g["fill_mode"],
        )
        for g in gaps
    ]
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO gaps
                (symbol, tf, ts_utc, direction,
                 gap_high, gap_low, size, initial_weight, weight,
                 half_fill_ts, fill_ts, status, fill_mode)
            VALUES %s
            ON CONFLICT (symbol, tf, ts_utc, direction) DO UPDATE SET
                weight       = EXCLUDED.weight,
                half_fill_ts = EXCLUDED.half_fill_ts,
                fill_ts      = EXCLUDED.fill_ts,
                status       = EXCLUDED.status
            """,
            rows,
        )
        upserted = cur.rowcount if cur.rowcount >= 0 else 0
    return {"upserted": upserted}


def store_backtest_result(conn, bt_results: dict[str, Any]) -> None:
    """Write backtest outcome for the gap two-phase claim into the rules table.
    One row per fill_mode; id is 'gap_two_phase_body' or 'gap_two_phase_wick'."""
    for mode, res in bt_results.items():
        rule_id = f"gap_two_phase_{mode}"
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO rules
                    (id, statement, origin, status, evidence, params,
                     bt_trades, bt_winrate, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
                ON CONFLICT (id) DO UPDATE SET
                    status     = EXCLUDED.status,
                    evidence   = EXCLUDED.evidence,
                    bt_trades  = EXCLUDED.bt_trades,
                    bt_winrate = EXCLUDED.bt_winrate,
                    updated_at = NOW()
                """,
                (
                    rule_id,
                    "Price reaches 50% of gap then reverses before full fill",
                    "SPEC.md 4.5 two-phase backtest",
                    res["rule_status"],
                    (
                        f"total={res['total_gaps']} full_filled={res['full_filled']} "
                        f"half_only={res['half_only']} neither={res['neither']} "
                        f"two_phase_freq={res['two_phase_freq']:.4f}"
                    ),
                    psycopg2.extras.Json({"fill_mode": mode}),
                    res["total_gaps"],
                    res["two_phase_freq"],
                ),
            )


def fetch_open_gaps(
    conn, symbol: str, tf: str, as_of_ts: datetime
) -> list[dict[str, Any]]:
    """Open and half-filled gaps as of as_of_ts (for signal engine use later)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT symbol, tf, ts_utc, direction, gap_high, gap_low,
                   size, initial_weight, weight, half_fill_ts, fill_ts,
                   status, fill_mode
            FROM gaps
            WHERE symbol = %s AND tf = %s AND ts_utc <= %s
              AND status = ANY(%s)
            ORDER BY ts_utc
            """,
            (symbol, tf, as_of_ts, ["OPEN", "HALF_FILLED"]),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
