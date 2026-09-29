"""DB helpers for the regime module (SPEC.md 4.9)."""
from __future__ import annotations

from datetime import datetime
from typing import Any

import psycopg2.extras


def register_proposed_rule(conn, params: dict[str, Any]) -> None:
    """rules registry (SPEC.md 4.16, decision D20): regime_quality starts
    status='proposed'. Idempotent -- DO NOTHING on conflict so a status a
    human/backtest has since advanced isn't reset (same convention as
    matrix_store.register_proposed_rule / rsi_store.register_proposed_rules).

    D20 already names regime_quality as one of the six voters connected
    via the rules_registry.allow_proposed_in_voting flag -- this function
    was simply never called anywhere, so module_voting.compute_votes()'s
    `SELECT id, status FROM rules WHERE id = ANY(...)` found no row and the
    voter always participated=False despite its code being complete."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO rules (id, statement, origin, status, params, created_at)
            VALUES (%s, %s, %s, 'proposed', %s, NOW())
            ON CONFLICT (id) DO NOTHING
            """,
            (
                "regime_quality",
                "Range regime (ADX below range_adx_thresh and BB width below its "
                "rolling percentile threshold) agrees with every level_reversion_v1 "
                "reversal signal; trend regime (ADX above trend_adx_thresh) disagrees. "
                "Direction-agnostic -- labels market TYPE, not direction "
                "(module_voting._vote_structure carries the directional half).",
                "SPEC.md 4.9, decision D20",
                psycopg2.extras.Json({
                    "trend_adx_thresh": params["trend_adx_thresh"],
                    "range_adx_thresh": params["range_adx_thresh"],
                    "range_bb_pct_thresh": params["range_bb_pct_thresh"],
                }),
            ),
        )


def upsert_regime_snapshots(conn, snapshots: list[dict[str, Any]]) -> dict[str, int]:
    if not snapshots:
        return {"upserted": 0}
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO regime_snapshots
                (symbol, tf_origin, ts_utc, regime, adx, bb_width, bb_width_pct)
            VALUES
                (%(symbol)s, %(tf_origin)s, %(ts_utc)s, %(regime)s,
                 %(adx)s, %(bb_width)s, %(bb_width_pct)s)
            ON CONFLICT (symbol, tf_origin, ts_utc)
            DO UPDATE SET
                regime      = EXCLUDED.regime,
                adx         = EXCLUDED.adx,
                bb_width    = EXCLUDED.bb_width,
                bb_width_pct = EXCLUDED.bb_width_pct
            """,
            snapshots,
        )
    return {"upserted": len(snapshots)}


def fetch_latest_regime(
    conn,
    symbol: str,
    tf: str,
    as_of_ts: datetime,
) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT symbol, tf_origin, ts_utc, regime, adx, bb_width, bb_width_pct
            FROM regime_snapshots
            WHERE symbol = %s AND tf_origin = %s AND ts_utc <= %s
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


def fetch_regime_snapshots(
    conn,
    symbol: str,
    tf: str,
    from_ts: datetime,
    to_ts: datetime,
) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT symbol, tf_origin, ts_utc, regime, adx, bb_width, bb_width_pct
            FROM regime_snapshots
            WHERE symbol = %s AND tf_origin = %s
              AND ts_utc >= %s AND ts_utc <= %s
            ORDER BY ts_utc
            """,
            (symbol, tf, from_ts, to_ts),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
