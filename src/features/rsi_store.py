"""RSI + MACD module -- DB read/write layer (proposed module, see final report).

compute_*() in rsi.py is pure (no DB); this module is the DB half, same
split as gaps_store.py / patterns_store.py.
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
    """lookback_bars: when set, fetches only the most recent N bars. None
    (default) fetches the full history -- required by backtest callers."""
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


def upsert_rsi_snapshots(conn, rows: list[dict[str, Any]]) -> dict[str, int]:
    """Natural key: (symbol, tf, ts_utc). ON CONFLICT UPDATE -- a snapshot
    can legitimately be recomputed (e.g. wider lookback_bars re-run)."""
    if not rows:
        return {"upserted": 0}
    values = [
        (
            r["symbol"], r["tf"], r["ts_utc"],
            r["rsi"], r["rsi_state"],
            r["macd"], r["macd_signal"], r["macd_hist"],
        )
        for r in rows
    ]
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO rsi_snapshots
                (symbol, tf, ts_utc, rsi, rsi_state, macd, macd_signal, macd_hist)
            VALUES %s
            ON CONFLICT (symbol, tf, ts_utc) DO UPDATE SET
                rsi         = EXCLUDED.rsi,
                rsi_state   = EXCLUDED.rsi_state,
                macd        = EXCLUDED.macd,
                macd_signal = EXCLUDED.macd_signal,
                macd_hist   = EXCLUDED.macd_hist
            """,
            values,
        )
        upserted = cur.rowcount if cur.rowcount >= 0 else 0
    return {"upserted": upserted}


def upsert_divergence_events(conn, events: list[dict[str, Any]]) -> dict[str, int]:
    """Natural key: (symbol, tf, kind, swing1_ts, swing2_ts). ON CONFLICT DO
    NOTHING -- an event table, like pattern_hits (values are frozen once the
    two swings that define it are both confirmed)."""
    if not events:
        return {"inserted": 0, "skipped": 0}
    rows = [
        (
            e["symbol"], e["tf"], e["kind"], e["direction"],
            e["swing1_ts"], e["swing2_ts"],
            float(e["swing1_price"]), float(e["swing2_price"]),
            float(e["swing1_indicator"]), float(e["swing2_indicator"]),
            e["confirmed_ts"],
        )
        for e in events
    ]
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO divergence_events
                (symbol, tf, kind, direction, swing1_ts, swing2_ts,
                 swing1_price, swing2_price, swing1_indicator, swing2_indicator,
                 confirmed_ts)
            VALUES %s
            ON CONFLICT (symbol, tf, kind, swing1_ts, swing2_ts) DO NOTHING
            """,
            rows,
        )
        inserted = cur.rowcount if cur.rowcount >= 0 else 0
    return {"inserted": inserted, "skipped": len(rows) - inserted}


def fetch_latest_snapshot(conn, symbol: str, tf: str, as_of_ts: datetime) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT symbol, tf, ts_utc, rsi, rsi_state, macd, macd_signal, macd_hist
            FROM rsi_snapshots
            WHERE symbol = %s AND tf = %s AND ts_utc <= %s
            ORDER BY ts_utc DESC
            LIMIT 1
            """,
            (symbol, tf, as_of_ts),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))


def fetch_recent_divergences(
    conn, symbol: str, tf: str, as_of_ts: datetime, kind: str | None = None, limit: int = 20
) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        if kind is None:
            cur.execute(
                """
                SELECT symbol, tf, kind, direction, swing1_ts, swing2_ts,
                       swing1_price, swing2_price, swing1_indicator, swing2_indicator,
                       confirmed_ts
                FROM divergence_events
                WHERE symbol = %s AND tf = %s AND confirmed_ts <= %s
                ORDER BY confirmed_ts DESC
                LIMIT %s
                """,
                (symbol, tf, as_of_ts, limit),
            )
        else:
            cur.execute(
                """
                SELECT symbol, tf, kind, direction, swing1_ts, swing2_ts,
                       swing1_price, swing2_price, swing1_indicator, swing2_indicator,
                       confirmed_ts
                FROM divergence_events
                WHERE symbol = %s AND tf = %s AND kind = %s AND confirmed_ts <= %s
                ORDER BY confirmed_ts DESC
                LIMIT %s
                """,
                (symbol, tf, kind, as_of_ts, limit),
            )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def register_proposed_rules(
    conn,
    rsi_params: dict[str, Any],
    macd_params: dict[str, Any],
    divergence_params: dict[str, Any],
) -> None:
    """Registers the three rules this module implements into the `rules`
    registry with status='proposed' (SPEC.md 4.15 / CLAUDE.md: every new
    rule starts proposed). No backtest has been run -- see final report.
    Idempotent: DO NOTHING on conflict so re-running this doesn't clobber a
    status a human/backtest has since advanced (e.g. to 'testing')."""
    rules = [
        (
            "rsi_overbought_oversold",
            "RSI(14) crossing the configured overbought (>=70) / oversold (<=30) "
            "threshold flags a potential reversal zone.",
            "user-specified 2026-09-24 (RSI+MACD divergence task)",
            {
                "period": rsi_params["period"],
                "overbought": rsi_params["overbought"],
                "oversold": rsi_params["oversold"],
            },
        ),
        (
            "rsi_price_divergence",
            "Classic bearish divergence: price makes a higher high while RSI makes "
            "a lower high (consecutive confirmed swings). Classic bullish divergence: "
            "price makes a lower low while RSI makes a higher low.",
            "user-specified 2026-09-24 (RSI+MACD divergence task)",
            {
                "swing_n": divergence_params["swing_n"],
                "lookback_bars": divergence_params["lookback_bars"],
                "rsi_period": rsi_params["period"],
            },
        ),
        (
            "macd_price_divergence",
            "Same classic divergence logic as rsi_price_divergence, compared against "
            "the MACD line (12,26,9) instead of RSI.",
            "user-specified 2026-09-24 (RSI+MACD divergence task)",
            {
                "swing_n": divergence_params["swing_n"],
                "lookback_bars": divergence_params["lookback_bars"],
                "macd_fast": macd_params["fast_period"],
                "macd_slow": macd_params["slow_period"],
                "macd_signal": macd_params["signal_period"],
                "divergence_source": macd_params.get("divergence_source", "macd_line"),
            },
        ),
    ]
    with conn.cursor() as cur:
        for rule_id, statement, origin, params in rules:
            cur.execute(
                """
                INSERT INTO rules (id, statement, origin, status, params, created_at)
                VALUES (%s, %s, %s, 'proposed', %s, NOW())
                ON CONFLICT (id) DO NOTHING
                """,
                (rule_id, statement, origin, psycopg2.extras.Json(params)),
            )
