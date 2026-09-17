"""SPEC.md 4.8 (`correlation`) -- DB read/write layer.

compute_*()/backtest_*() in correlation.py are pure (no DB); this module
handles the DB layer, shared by the API endpoints and any future backtest
tooling (CLAUDE.md rule 6).
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


def fetch_candles(conn, symbol: str, tf: str, as_of_ts: datetime) -> list[dict[str, Any]]:
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


# --- 4.8-a: dollar_correlation ---------------------------------------------

def upsert_dollar_correlation(conn, rows: list[dict[str, Any]]) -> dict[str, int]:
    if not rows:
        return {"upserted": 0}
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO dollar_correlation (symbol, tf, ts_utc, correlation)
            VALUES %s
            ON CONFLICT (symbol, tf, ts_utc) DO UPDATE SET correlation = EXCLUDED.correlation
            """,
            [(r["symbol"], r["tf"], r["ts_utc"], r["correlation"]) for r in rows],
        )
    return {"upserted": len(rows)}


def fetch_latest_dollar_correlation(conn, symbol: str, tf: str, as_of_ts: datetime) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT symbol, tf, ts_utc, correlation FROM dollar_correlation
            WHERE symbol = %s AND tf = %s AND ts_utc <= %s
            ORDER BY ts_utc DESC LIMIT 1
            """,
            (symbol, tf, as_of_ts),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))


# --- 4.8-b: oil_shock_events -----------------------------------------------

def upsert_oil_shock_events(conn, events: list[dict[str, Any]]) -> dict[str, int]:
    if not events:
        return {"upserted": 0}
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO oil_shock_events
                (symbol, tf, ts_utc, oil_return_3bar, oil_atr, gold_return_3bar,
                 gold_return_6bar_fwd, reversal, oil_gold_concurrent_agree)
            VALUES %s
            ON CONFLICT (symbol, tf, ts_utc) DO UPDATE SET
                gold_return_6bar_fwd = EXCLUDED.gold_return_6bar_fwd,
                reversal = EXCLUDED.reversal
            """,
            [
                (
                    e["symbol"], e["tf"], e["ts_utc"], e["oil_return_3bar"], e["oil_atr"],
                    e["gold_return_3bar"], e["gold_return_6bar_fwd"], e["reversal"],
                    e["oil_gold_concurrent_agree"],
                )
                for e in events
            ],
        )
    return {"upserted": len(events)}


def store_oil_shock_backtest(conn, bt: dict[str, Any]) -> None:
    """Registers oil_shock_divergence in `rules` per SPEC.md 4.8-b's
    explicit instruction (verified/rejected, based on the binomial test)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO rules
                (id, statement, origin, status, evidence, params,
                 bt_trades, bt_winrate, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
            ON CONFLICT (id) DO UPDATE SET
                status = EXCLUDED.status, evidence = EXCLUDED.evidence,
                bt_trades = EXCLUDED.bt_trades, bt_winrate = EXCLUDED.bt_winrate,
                updated_at = NOW()
            """,
            (
                "oil_shock_divergence",
                "Gold reverses its own 3-bar move more often within 6 bars after an oil shock than at baseline",
                "SPEC.md 4.8-b",
                bt["rule_status"],
                (
                    f"shock_reversal_rate={bt['shock_reversal_rate']} "
                    f"baseline_reversal_rate={bt['baseline_reversal_rate']} "
                    f"p_value={bt['p_value']} best_lag={bt['best_lag']}"
                ),
                psycopg2.extras.Json({
                    "alpha": bt["alpha"], "min_samples": bt["min_samples"],
                    "lag_cross_correlation": bt["lag_cross_correlation"],
                }),
                bt["shock_count"],
                bt["shock_reversal_rate"],
            ),
        )


# --- 4.8-c: pressure_snapshots + pressure_episodes -------------------------

def upsert_pressure_snapshots(conn, rows: list[dict[str, Any]]) -> dict[str, int]:
    if not rows:
        return {"upserted": 0}
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO pressure_snapshots (symbol, tf, ts_utc, residual, pressure_raw, pressure_z, flagged)
            VALUES %s
            ON CONFLICT (symbol, tf, ts_utc) DO UPDATE SET
                residual = EXCLUDED.residual, pressure_raw = EXCLUDED.pressure_raw,
                pressure_z = EXCLUDED.pressure_z, flagged = EXCLUDED.flagged
            """,
            [(r["symbol"], r["tf"], r["ts_utc"], r["residual"], r["pressure_raw"], r["pressure_z"], r["flagged"]) for r in rows],
        )
    return {"upserted": len(rows)}


def upsert_pressure_episodes(conn, symbol: str, tf: str, episodes: list[dict[str, Any]]) -> dict[str, int]:
    if not episodes:
        return {"upserted": 0}
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO pressure_episodes
                (symbol, tf, flag_ts, pressure_sign, discharge_ts, bars_to_discharge, return_12bar, return_24bar)
            VALUES %s
            ON CONFLICT (symbol, tf, flag_ts) DO UPDATE SET
                discharge_ts = EXCLUDED.discharge_ts,
                bars_to_discharge = EXCLUDED.bars_to_discharge,
                return_12bar = EXCLUDED.return_12bar,
                return_24bar = EXCLUDED.return_24bar
            """,
            [
                (
                    symbol, tf, e["flag_ts"], e["pressure_sign"], e["discharge_ts"],
                    e["bars_to_discharge"], e["return_12bar"], e["return_24bar"],
                )
                for e in episodes
            ],
        )
    return {"upserted": len(episodes)}


def store_pressure_reversal_backtest(conn, bt: dict[str, Any]) -> None:
    """Registers pressure_reversal in `rules` as 'testing' (SPEC gives no
    numeric accept/reject gate for this one -- see correlation.py docstring)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO rules
                (id, statement, origin, status, evidence, params,
                 bt_trades, bt_winrate, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
            ON CONFLICT (id) DO UPDATE SET
                status = EXCLUDED.status, evidence = EXCLUDED.evidence,
                bt_trades = EXCLUDED.bt_trades, updated_at = NOW()
            """,
            (
                "pressure_reversal",
                "After a sustained accumulated-pressure flag, gold discharges with a sharp reversal move",
                "SPEC.md 4.8-c",
                bt["rule_status"],
                str(bt["answers"]),
                psycopg2.extras.Json({}),
                bt["episode_count"],
                None,
            ),
        )
