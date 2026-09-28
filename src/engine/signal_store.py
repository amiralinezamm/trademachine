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


def fetch_latest_signal(conn, symbol: str, tf: str) -> dict[str, Any] | None:
    """Most recent row in `signals` for this symbol/tf, regardless of
    outcome or whether it was 'just' fired. `signals` has no symbol/tf
    columns of its own (SPEC.md 4.15 schema) -- level_reversion.py always
    writes them into components, so filter through there.

    This is a plain read -- no rule evaluation, no insert. It exists
    because /signal/latest (_check_signal_sync) re-runs check_level_reversion
    against the CURRENT candle and returns signal=None once that signal is
    already stored (reason='already_fired') or once price has moved past
    the triggering level -- so polling it after the fact (e.g. a Telegram
    /status a few minutes after n8n already fired and stored the signal)
    shows nothing even though a real signal exists. This function is for
    display: "what was the last signal", not "does one fire right now"."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, ts_utc, direction, entry, stop_loss, take_profit,
                   confidence, components, rule_version, outcome, pnl_usd
            FROM signals
            WHERE components->>'symbol' = %s AND components->>'tf' = %s
            ORDER BY ts_utc DESC
            LIMIT 1
            """,
            (symbol, tf),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "ts_utc": row[1],
        "direction": row[2],
        "entry": float(row[3]) if row[3] is not None else None,
        "stop_loss": float(row[4]) if row[4] is not None else None,
        "take_profit": float(row[5]) if row[5] is not None else None,
        "confidence": float(row[6]) if row[6] is not None else None,
        "components": row[7],
        "rule_version": row[8],
        "outcome": row[9],
        "pnl_usd": float(row[10]) if row[10] is not None else None,
    }


def fetch_last_signal_for_direction(
    conn, direction: str, as_of_ts
) -> "dict[str, Any] | None":
    """Return the most recent signal with `direction` whose ts_utc <= as_of_ts.

    Used by the same-direction spacing filter (Rule: same_direction_spacing_filter).
    Returns a dict with keys 'entry' (float) and 'outcome' (str | None), or None
    if no prior signal of that direction exists at as_of_ts.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT entry, outcome
            FROM signals
            WHERE direction = %s AND ts_utc <= %s
            ORDER BY ts_utc DESC
            LIMIT 1
            """,
            (direction, as_of_ts),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {"entry": float(row[0]), "outcome": row[1]}
