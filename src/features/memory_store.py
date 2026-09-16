"""DB helpers for the memory module (SPEC.md 4.10)."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any


def upsert_memory_result(conn, result: dict[str, Any]) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO memory_results
                (symbol, tf_origin, computed_at, n_matches, up_ratio,
                 median_return, ci_low, ci_high, raw_returns)
            VALUES
                (%(symbol)s, %(tf_origin)s, %(computed_at)s, %(n_matches)s,
                 %(up_ratio)s, %(median_return)s, %(ci_low)s, %(ci_high)s,
                 %(raw_returns)s::jsonb)
            ON CONFLICT (symbol, tf_origin, computed_at)
            DO UPDATE SET
                n_matches     = EXCLUDED.n_matches,
                up_ratio      = EXCLUDED.up_ratio,
                median_return = EXCLUDED.median_return,
                ci_low        = EXCLUDED.ci_low,
                ci_high       = EXCLUDED.ci_high,
                raw_returns   = EXCLUDED.raw_returns
            """,
            {**result, "raw_returns": json.dumps(result["raw_returns"])},
        )
    return {"upserted": 1}


def fetch_latest_memory_result(
    conn,
    symbol: str,
    tf: str,
    as_of_ts: datetime,
) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT symbol, tf_origin, computed_at, n_matches, up_ratio,
                   median_return, ci_low, ci_high, raw_returns
            FROM memory_results
            WHERE symbol = %s AND tf_origin = %s AND computed_at <= %s
            ORDER BY computed_at DESC
            LIMIT 1
            """,
            (symbol, tf, as_of_ts),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        d = dict(zip(cols, row))
        if isinstance(d.get("raw_returns"), str):
            d["raw_returns"] = json.loads(d["raw_returns"])
        return d
