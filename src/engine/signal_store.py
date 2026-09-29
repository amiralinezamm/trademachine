"""DB read/write half for the signal engine — check_level_reversion() itself
stays pure (CLAUDE.md rule 6); this fetches the latest candle + active
levels and writes any resulting signal row."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from src.engine.level_reversion import RULE_ID
from src.features.levels_store import get_connection

# Live reads filter on the LIVE rule_version. Backtest writes to the same
# `signals` table (replay._upsert_signal); with rule_version_suffix="" those
# rows used to be indistinguishable, and the unfiltered reads below then
# treated a replayed signal as "the last live signal" -- skewing the live
# same-direction spacing filter, /signal/last, /health and the
# reversal-close advisory (found on the server 2026-09-29).
LIVE_RULE_VERSION = RULE_ID


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


def fetch_latest_signal(
    conn, symbol: str, tf: str, rule_version: str = LIVE_RULE_VERSION
) -> dict[str, Any] | None:
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
              AND rule_version = %s
            ORDER BY ts_utc DESC
            LIMIT 1
            """,
            (symbol, tf, rule_version),
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
    conn, direction: str, as_of_ts, rule_version: str = LIVE_RULE_VERSION
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
            WHERE direction = %s AND ts_utc <= %s AND rule_version = %s
            ORDER BY ts_utc DESC
            LIMIT 1
            """,
            (direction, as_of_ts, rule_version),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {"entry": float(row[0]), "outcome": row[1]}


def fetch_open_signals(
    conn, symbol: str, tf: str, rule_version: str = LIVE_RULE_VERSION
) -> list[dict[str, Any]]:
    """'توقف اجباری' rule (2026-09-29): the most recent OPEN (outcome IS
    NULL) signal per direction for this symbol/tf -- what the reversal-close
    check (src/engine/exit_rules.detect_reversal_close) evaluates against.
    Needs stop_loss/take_profit populated (src/engine/exit_rules
    .compute_level_based_sl_tp), so this only returns something meaningful
    for signals fired after that wiring landed."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (direction)
                   id, ts_utc, direction, entry, stop_loss, take_profit,
                   components, reversal_alert_sent
            FROM signals
            WHERE components->>'symbol' = %s AND components->>'tf' = %s
              AND outcome IS NULL AND rule_version = %s
            ORDER BY direction, ts_utc DESC
            """,
            (symbol, tf, rule_version),
        )
        rows = cur.fetchall()
    return [
        {
            "id": r[0], "ts_utc": r[1], "direction": r[2],
            "entry": float(r[3]) if r[3] is not None else None,
            "stop_loss": float(r[4]) if r[4] is not None else None,
            "take_profit": float(r[5]) if r[5] is not None else None,
            "components": r[6],
            "reversal_alert_sent": r[7],
        }
        for r in rows
    ]


def fetch_candles_range(conn, symbol: str, tf: str, from_ts: datetime, to_ts: datetime) -> list[dict[str, Any]]:
    """Closed candles in [from_ts, to_ts], ascending -- used to scan a still-
    open signal's price history since its own entry (live outcome inference,
    see src/api/main.py's reversal-close check)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ts_utc, open, high, low, close
            FROM candles
            WHERE symbol = %s AND tf = %s AND ts_utc >= %s AND ts_utc <= %s
            ORDER BY ts_utc ASC
            """,
            (symbol, tf, from_ts, to_ts),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def mark_signal_outcome(conn, signal_id: int, outcome: str) -> None:
    """Sets `outcome` on a LIVE signal once price history shows it actually
    hit SL/TP/level-invalidated (src/backtest/replay._determine_outcome,
    reused here rather than reimplemented -- CLAUDE.md rule 6). Never
    touches pnl_usd (live cost model -- spread/slippage at fill time -- is
    a separate concern, not computed here)."""
    with conn.cursor() as cur:
        cur.execute("UPDATE signals SET outcome = %s WHERE id = %s AND outcome IS NULL", (outcome, signal_id))


def mark_reversal_alert_sent(conn, signal_id: int) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE signals SET reversal_alert_sent = true WHERE id = %s", (signal_id,))
