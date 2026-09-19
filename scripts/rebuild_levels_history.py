"""One-time script: rebuild levels_history from full candle history.

Runs compute_levels once over the full 2023-09-15→now candle range with
record_history=True, then resolves each in-memory level to its DB level_id
by joining on (symbol, tf_origin, created_ts, initial_kind_at_creation) and
bulk-inserts into levels_history.

Expected runtime: 2–5 minutes (single walk-forward pass over ~213k candles).
Expected rows: ~6,752 levels × avg 20 events ≈ 130k rows.

Usage:
    cd /opt/xauusd-bot
    nohup /opt/xauusd-bot/src/api/venv/bin/python -m scripts.rebuild_levels_history \
        > /tmp/rebuild_levels_history.log 2>&1 &
    echo $!
"""
from __future__ import annotations

import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Make sure project root is on path when run as -m
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.features.levels import compute_levels
from src.features.levels_store import fetch_candles, get_connection

log = logging.getLogger(__name__)

SYMBOL = "XAUUSD@"
TF = "M5"


def _fetch_all_candles(conn, symbol: str, tf: str) -> list[dict]:
    """Fetch the full candle history (no date limit)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ts_utc, open, high, low, close
            FROM candles
            WHERE symbol = %s AND tf = %s
            ORDER BY ts_utc
            """,
            (symbol, tf),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _resolve_level_ids(conn, symbol: str, tf: str, events: list[dict]) -> dict[tuple, int]:
    """Map (created_ts, initial_kind) → level_id from the DB.

    The initial_kind stored in history events is the kind at creation time.
    The DB `levels.kind` may have changed due to flips, so we match ONLY on
    created_ts (which is stable). When two levels share the same created_ts
    (rare: simultaneous support + resistance swing), initial_kind disambiguates.
    """
    # Collect distinct keys
    keys = {(e["created_ts"], e["initial_kind"]) for e in events}
    if not keys:
        return {}

    # Build a lookup from the DB
    with conn.cursor() as cur:
        # Fetch all levels for this symbol/tf
        cur.execute(
            """
            SELECT id, created_ts, kind
            FROM levels
            WHERE symbol = %s AND tf_origin = %s
            """,
            (symbol, tf),
        )
        rows = cur.fetchall()

    # Primary key: (created_ts, kind_at_creation) — we use the ORIGINAL kind
    # stored in the DB (which may be post-flip). For levels that never flipped,
    # DB kind == initial_kind. For flipped/expired: DB kind may differ.
    # Strategy: first try exact (created_ts, initial_kind) match; if not found,
    # fall back to created_ts alone (handles flipped levels where kind changed).
    by_created_and_kind: dict[tuple, int] = {}
    by_created: dict[datetime, list[int]] = {}
    for row_id, row_created_ts, row_kind in rows:
        by_created_and_kind[(row_created_ts, row_kind)] = row_id
        by_created.setdefault(row_created_ts, []).append(row_id)

    mapping: dict[tuple, int] = {}
    for (created_ts, initial_kind) in keys:
        exact = by_created_and_kind.get((created_ts, initial_kind))
        if exact is not None:
            mapping[(created_ts, initial_kind)] = exact
        else:
            candidates = by_created.get(created_ts, [])
            if len(candidates) == 1:
                mapping[(created_ts, initial_kind)] = candidates[0]
            elif len(candidates) > 1:
                log.warning(
                    "Ambiguous created_ts %s initial_kind=%s — %d candidates, skipping",
                    created_ts, initial_kind, len(candidates),
                )
            else:
                log.warning(
                    "No DB level found for created_ts=%s initial_kind=%s — skipping",
                    created_ts, initial_kind,
                )
    return mapping


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    t0 = time.time()

    log.info("Connecting to DB…")
    conn = get_connection()
    try:
        log.info("Fetching full candle history for %s %s…", SYMBOL, TF)
        candles = _fetch_all_candles(conn, SYMBOL, TF)
        log.info("  %d candles loaded", len(candles))
        if not candles:
            log.error("No candles found — abort")
            return

        as_of_ts = candles[-1]["ts_utc"]
        log.info("Running compute_levels with record_history=True (as_of=%s)…", as_of_ts)

        result, history_events = compute_levels(
            candles, as_of_ts=as_of_ts,
            symbol=SYMBOL, tf=TF,
            record_history=True,
        )
        elapsed_wf = time.time() - t0
        log.info(
            "Walk-forward done in %.1fs — %d levels, %d history events",
            elapsed_wf, len(result), len(history_events),
        )

        if not history_events:
            log.warning("No history events generated — nothing to insert")
            return

        log.info("Resolving level_id for each event…")
        id_map = _resolve_level_ids(conn, SYMBOL, TF, history_events)
        log.info("  %d/%d keys resolved", len(id_map), len({(e["created_ts"], e["initial_kind"]) for e in history_events}))

        # Build rows to insert, dropping unresolved events
        rows_to_insert = []
        skipped = 0
        for e in history_events:
            key = (e["created_ts"], e["initial_kind"])
            level_id = id_map.get(key)
            if level_id is None:
                skipped += 1
                continue
            rows_to_insert.append((
                level_id,
                e["ts_utc"],
                e["strength"],
                e["status"],
                e["touch_count"],
                e["break_count"],
            ))

        log.info("  %d rows to insert, %d skipped (no DB match)", len(rows_to_insert), skipped)

        if not rows_to_insert:
            log.error("Nothing to insert after resolution — check the skipped count above")
            return

        # Truncate existing data and bulk insert
        log.info("Clearing existing levels_history…")
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE levels_history")
        conn.commit()

        log.info("Bulk-inserting %d rows…", len(rows_to_insert))
        BATCH = 5000
        inserted = 0
        with conn.cursor() as cur:
            for i in range(0, len(rows_to_insert), BATCH):
                batch = rows_to_insert[i:i + BATCH]
                cur.executemany(
                    """
                    INSERT INTO levels_history
                        (level_id, ts_utc, strength, status, touch_count, break_count)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    batch,
                )
                inserted += len(batch)
                if inserted % 20000 == 0 or inserted == len(rows_to_insert):
                    conn.commit()
                    log.info("  %d/%d inserted", inserted, len(rows_to_insert))

        conn.commit()
        elapsed_total = time.time() - t0
        log.info(
            "Done. %d rows in levels_history. Total time: %.1fs",
            len(rows_to_insert), elapsed_total,
        )

        # Quick sanity check
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM levels_history")
            (count,) = cur.fetchone()
        log.info("DB count: %d rows in levels_history", count)

    finally:
        conn.close()


if __name__ == "__main__":
    main()
