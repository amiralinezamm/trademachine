"""DB helpers for the fibonacci module (SPEC.md 4.6).

fetch_active_levels_for_fib() reads from the `levels` table (status IN
active/flipped) — fibonacci.py MUST NOT compute its own swings.
upsert_fibonacci_zones() writes results to `fibonacci_zones`.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any


def fetch_active_levels_for_fib(
    conn,
    symbol: str,
    tf: str,
    as_of_ts: datetime,
) -> list[dict[str, Any]]:
    """Return active/flipped S/R levels as plain dicts.
    as_of_ts is stored/compared as UTC; ensure the caller passes a tz-aware dt."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT kind, price_low, price_high, status, break_count, atr_at_birth
            FROM levels
            WHERE symbol = %s
              AND tf_origin = %s
              AND status IN ('active', 'flipped')
              AND created_ts <= %s
            """,
            (symbol, tf, as_of_ts),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def upsert_fibonacci_zones(conn, zones: list[dict[str, Any]]) -> dict[str, int]:
    if not zones:
        return {"upserted": 0}
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO fibonacci_zones
                (symbol, tf_origin, computed_at, swing_low, swing_high,
                 range_size, level_pct, price, role, overlapping)
            VALUES
                (%(symbol)s, %(tf_origin)s, %(computed_at)s, %(swing_low)s, %(swing_high)s,
                 %(range_size)s, %(level_pct)s, %(price)s, %(role)s, %(overlapping)s)
            ON CONFLICT (symbol, tf_origin, computed_at, swing_low, swing_high, level_pct)
            DO UPDATE SET
                price       = EXCLUDED.price,
                range_size  = EXCLUDED.range_size,
                role        = EXCLUDED.role,
                overlapping = EXCLUDED.overlapping
            """,
            zones,
        )
    return {"upserted": len(zones)}


def fetch_fibonacci_zones(
    conn,
    symbol: str,
    tf: str,
    as_of_ts: datetime,
    role: str | None = None,
) -> list[dict[str, Any]]:
    """Read back the most-recently-computed fib zones (at most one
    computed_at per symbol/tf combo — the latest snapshot)."""
    extra = "AND role = %s" if role else ""
    args: list[Any] = [symbol, tf, as_of_ts]
    if role:
        args.append(role)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, symbol, tf_origin, computed_at, swing_low, swing_high,
                   range_size, level_pct, price, role, overlapping
            FROM fibonacci_zones
            WHERE symbol = %s
              AND tf_origin = %s
              AND computed_at = (
                  SELECT MAX(computed_at) FROM fibonacci_zones
                  WHERE symbol = %s AND tf_origin = %s AND computed_at <= %s
              )
              {extra}
            ORDER BY role, level_pct
            """,
            [symbol, tf, symbol, tf] + args[2:],  # type: ignore[arg-type]
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
