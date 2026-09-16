"""DB helpers for the regime module (SPEC.md 4.9)."""
from __future__ import annotations

from datetime import datetime
from typing import Any


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
