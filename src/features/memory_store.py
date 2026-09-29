"""DB helpers for the memory module (SPEC.md 4.10)."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import psycopg2.extras


def register_proposed_rule(conn, params: dict[str, Any]) -> None:
    """rules registry (SPEC.md 4.16, decision D20): memory_pattern_bias
    starts status='proposed'. Idempotent -- DO NOTHING on conflict, same
    convention as matrix_store.register_proposed_rule /
    rsi_store.register_proposed_rules.

    D20 already names memory_pattern_bias as one of the six voters
    connected via the rules_registry.allow_proposed_in_voting flag -- this
    function was simply never called anywhere, so it never actually voted.

    NOTE (separate from this task): module_voting._vote_memory's up_ratio
    thresholds (0.6/0.4) and minimum match count (3) are hardcoded in that
    function, not read from config/params.yaml -- recorded here as
    `params` for traceability, not moved to config by this change."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO rules (id, statement, origin, status, params, created_at)
            VALUES (%s, %s, %s, 'proposed', %s, NOW())
            ON CONFLICT (id) DO NOTHING
            """,
            (
                "memory_pattern_bias",
                "STUMPY K=window_k analogue match: agrees with BUY when up_ratio "
                "of the top_n historical analogues' H=horizon_h forward return > 0.6, "
                "agrees with SELL when < 0.4 (inverse for SELL); votes 0 in the middle "
                "band or when n_matches < 3.",
                "SPEC.md 4.10, decision D20",
                psycopg2.extras.Json({
                    "window_k": params["window_k"],
                    "horizon_h": params["horizon_h"],
                    "top_n": params["top_n"],
                    "up_ratio_bullish_thresh": 0.6,
                    "up_ratio_bearish_thresh": 0.4,
                    "min_matches": 3,
                }),
            ),
        )


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
