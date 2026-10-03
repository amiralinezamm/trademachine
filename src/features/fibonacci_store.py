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
    """Return active/flipped S/R levels as plain dicts, status resolved
    point-in-time as of as_of_ts.

    2026-09-30 fix: this used to read `levels.status` directly, which is
    the level's CURRENT status, not its status at as_of_ts. Harmless for
    the live caller (as_of_ts is always ~now, so current == point-in-time),
    but a real look-ahead violation (CLAUDE.md rule 1) the moment this same
    function -- rule 6 requires it stay the same one -- is reused for a
    historical as_of_ts: a level created in 2024 and expired by today would
    be wrongly excluded from a 2024 snapshot. Resolved the same way
    src/backtest/replay.py's _level_state_at() does: most recent
    levels_history row at or before as_of_ts; no row yet => the level's
    initial state ('active', just created, untouched).
    as_of_ts is stored/compared as UTC; ensure the caller passes a tz-aware dt."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT l.kind, l.price_low, l.price_high,
                   COALESCE(lh.status, 'active') AS status,
                   l.break_count, l.atr_at_birth
            FROM levels l
            LEFT JOIN LATERAL (
                SELECT status
                FROM levels_history
                WHERE level_id = l.id AND ts_utc <= %s
                ORDER BY ts_utc DESC
                LIMIT 1
            ) lh ON true
            WHERE l.symbol = %s
              AND l.tf_origin = %s
              AND l.created_ts <= %s
              AND COALESCE(lh.status, 'active') IN ('active', 'flipped')
            """,
            (as_of_ts, symbol, tf, as_of_ts),
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
