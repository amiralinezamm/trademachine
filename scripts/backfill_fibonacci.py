"""Backfill historical fibonacci_zones for a TRAIN period.

CLAUDE.md rule 6: calls the EXACT live compute-and-store function
(src.api.main._compute_and_store_fibonacci_sync) -- not a parallel
reimplementation. Params (lookback_bars=500) match the live n8n
"fibonacci/compute" node exactly (config/params.yaml is not the source of
lookback_bars for this call -- the live n8n node hardcodes it; documented
here rather than silently guessing a different value).

Guards (hard-stop, not warnings):
  1. to_ts must never exceed the frozen HOLDOUT boundary H.
  2. to_ts must never exceed the first ts_utc already written by the live
     path, so a live-computed row can never be touched.

Idempotent: fibonacci_zones' ON CONFLICT (natural key) means re-running a
range is always safe. Resumable via a progress checkpoint file so a killed
run can continue instead of redoing already-processed bars.

Usage:
    python -m scripts.backfill_fibonacci 2024-02-18 2025-02-18 --dry-run
    nohup python -m scripts.backfill_fibonacci 2024-02-18 2026-02-18 \
        > ~/backfill_fibonacci.log 2>&1 &
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

log = logging.getLogger(__name__)

SYMBOL = "XAUUSD@"
TF = "M5"
LOOKBACK_BARS = 500  # matches n8n "fibonacci/compute" node's queryParameters
COSTS_PATH = Path(__file__).resolve().parents[1] / "config" / "costs.yaml"
CHECKPOINT_PATH = Path("/tmp/backfill_fibonacci_checkpoint.txt")


def check_holdout_guard(to_ts: datetime, holdout_start: datetime) -> None:
    """Raise if to_ts reaches into the sealed HOLDOUT region."""
    if to_ts > holdout_start:
        raise SystemExit(
            f"REFUSING: to_ts {to_ts.isoformat()} exceeds HOLDOUT boundary "
            f"H={holdout_start.isoformat()}. Backfill must stay strictly "
            f"before H."
        )


def check_live_overlap_guard(to_ts: datetime, first_live_ts: datetime | None) -> None:
    """Raise if to_ts would reach a timestamp the live path already wrote."""
    if first_live_ts is not None and to_ts > first_live_ts:
        raise SystemExit(
            f"REFUSING: to_ts {to_ts.isoformat()} would overlap the first "
            f"live-written row at {first_live_ts.isoformat()}. A backfill "
            f"must never touch a live-computed timestamp."
        )


def load_checkpoint() -> datetime | None:
    if not CHECKPOINT_PATH.exists():
        return None
    raw = CHECKPOINT_PATH.read_text().strip()
    return datetime.fromisoformat(raw) if raw else None


def save_checkpoint(ts: datetime) -> None:
    CHECKPOINT_PATH.write_text(ts.isoformat())


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    ap = argparse.ArgumentParser(description="Backfill fibonacci_zones (D24/D-backfill)")
    ap.add_argument("from_date", help="ISO date, inclusive")
    ap.add_argument("to_date", help="ISO date, exclusive")
    ap.add_argument("--dry-run", action="store_true", help="log what would run, write nothing")
    ap.add_argument("--fresh", action="store_true", help="ignore any existing checkpoint")
    args = ap.parse_args()

    UTC = timezone.utc
    from_ts = datetime.fromisoformat(args.from_date).replace(tzinfo=UTC)
    to_ts = datetime.fromisoformat(args.to_date).replace(tzinfo=UTC)

    from src.backtest.splits import get_holdout_start
    from src.backtest.replay import _fetch_all_candles
    from src.features.levels_store import get_connection

    holdout_start = get_holdout_start(symbol=SYMBOL, tf=TF)
    check_holdout_guard(to_ts, holdout_start)

    conn = get_connection()
    with conn.cursor() as cur:
        # WHERE computed_at > holdout_start: a backfill run can only ever
        # write rows <= H (check_holdout_guard enforces this on every run),
        # so this is always the true first LIVE row, never a row this
        # script itself wrote on an earlier run -- a plain MIN() would pick
        # up its own backfilled rows once any exist, defeating the guard.
        cur.execute(
            "SELECT min(computed_at) FROM fibonacci_zones WHERE symbol = %s AND computed_at > %s",
            (SYMBOL, holdout_start),
        )
        first_live_ts = cur.fetchone()[0]
    check_live_overlap_guard(to_ts, first_live_ts)

    log.info("HOLDOUT H=%s  first_live_ts=%s", holdout_start, first_live_ts)
    log.info("Fetching candle list %s -> %s ...", from_ts, to_ts)
    candles = _fetch_all_candles(conn, SYMBOL, TF, from_ts, to_ts)
    log.info("  %d candles to backfill", len(candles))

    resume_from = None if args.fresh else load_checkpoint()
    if resume_from is not None:
        candles = [c for c in candles if c["ts_utc"] > resume_from]
        log.info("Resuming after checkpoint %s -- %d candles remaining",
                  resume_from, len(candles))

    if args.dry_run:
        conn.close()
        log.info("DRY RUN -- no writes. Would process %d bars.", len(candles))
        if candles:
            log.info("  first=%s last=%s", candles[0]["ts_utc"], candles[-1]["ts_utc"])
        return

    import yaml
    from src.api.main import _compute_and_store_fibonacci_sync

    costs = yaml.safe_load(open(COSTS_PATH))
    batch_size = int(costs["backfill"]["commit_batch_size"])
    log.info("commit_batch_size=%d (config/costs.yaml backfill.commit_batch_size)", batch_size)

    # 2026-09-30: one shared connection + batched commit (I/O only -- the
    # per-bar call is still the exact live function, CLAUDE.md rule 6).
    # conn=None (the live endpoint's own default) opens+commits+closes per
    # call; passing conn here reuses it and this loop controls the commit.
    t0 = time.time()
    zone_total = 0
    try:
        for i, c in enumerate(candles):
            ts = c["ts_utc"]
            result = _compute_and_store_fibonacci_sync(
                SYMBOL, TF, ts, lookback_bars=LOOKBACK_BARS, conn=conn,
            )
            zone_total += result.get("zone_count", 0)
            if (i + 1) % batch_size == 0:
                conn.commit()
                save_checkpoint(ts)
            if (i + 1) % 1000 == 0:
                elapsed = time.time() - t0
                log.info("  %d/%d  ts=%s  zones_so_far=%d  elapsed=%.1fs",
                          i + 1, len(candles), ts, zone_total, elapsed)
        conn.commit()
        if candles:
            save_checkpoint(candles[-1]["ts_utc"])
    finally:
        conn.close()

    elapsed = time.time() - t0
    log.info("Done. %d bars, %d zones written, %.1fs (%.3fs/bar)",
              len(candles), zone_total, elapsed,
              elapsed / len(candles) if candles else 0)


if __name__ == "__main__":
    main()
