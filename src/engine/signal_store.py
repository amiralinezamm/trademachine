"""DB read/write half for the signal engine — check_level_reversion() itself
stays pure (CLAUDE.md rule 6); this fetches the latest candle + active
levels and writes any resulting signal row."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from src.features.levels_store import get_connection


def fetch_latest_closed_candle(conn, symbol: str, tf: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ts_utc, open, high, low, close
            FROM candles
            WHERE symbol = %s AND tf = %s
            ORDER BY ts_utc DESC
            LIMIT 1
            """,
            (symbol, tf),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))


def fetch_atr_at(conn, symbol: str, tf: str, ts_utc: datetime, period: int) -> float | None:
    """Recomputes ATR(period) from the last `period`+1 closed candles up to
    ts_utc — same source of truth as compute_levels(), just enough history
    for one ATR value instead of pulling years of candles per signal check."""
    import numpy as np
    import talib

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT high, low, close FROM candles
            WHERE symbol = %s AND tf = %s AND ts_utc <= %s
            ORDER BY ts_utc DESC
            LIMIT %s
            """,
            (symbol, tf, ts_utc, period * 3),
        )
        rows = cur.fetchall()
    if len(rows) < period + 1:
        return None
    rows.reverse()
    highs = np.array([float(r[0]) for r in rows])
    lows = np.array([float(r[1]) for r in rows])
    closes = np.array([float(r[2]) for r in rows])
    atr = talib.ATR(highs, lows, closes, timeperiod=period)
    val = atr[-1]
    return None if (val != val) else float(val)  # NaN check without importing math


def fetch_active_levels(conn, symbol: str, tf: str, as_of_ts: datetime) -> list[dict[str, Any]]:
    """Anti-repainting: only levels created at or before as_of_ts. Both
    'active' and 'flipped' are live/tradeable states per SPEC.md 4.2 and
    compute_levels() itself (only 'expired' is dead) — a level that has
    been broken and re-tested keeps trading off its (new) kind, so
    excluding 'flipped' here would silently drop the most contested zones
    (the ones with the most touches) from the rule.
    (`levels` rows are never deleted, so as_of_ts here just bounds which
    ones existed yet — see CLAUDE.md rules 1/2.)"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, kind, price_low, price_high, strength, status
            FROM levels
            WHERE symbol = %s AND tf_origin = %s AND status IN ('active', 'flipped') AND created_ts <= %s
            """,
            (symbol, tf, as_of_ts),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def insert_signal(conn, signal: dict[str, Any]) -> int | None:
    """Insert signal, ignoring duplicates (same candle + rule_version).
    Returns the new id, or None if a signal for this candle already exists.
    Catches UniqueViolation explicitly to guard against concurrent inserts."""
    import psycopg2
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO signals (ts_utc, direction, entry, stop_loss, take_profit,
                                      confidence, components, rule_version)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (ts_utc, rule_version) DO NOTHING
                RETURNING id
                """,
                (
                    signal["ts_utc"], signal["direction"], signal["entry"],
                    signal["stop_loss"], signal["take_profit"], signal["confidence"],
                    json.dumps(signal["components"]), signal["rule_version"],
                ),
            )
            row = cur.fetchone()
            return row[0] if row else None
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        return None
