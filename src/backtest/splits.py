"""SPEC.md 4.15 — Walk-forward split generation and HOLDOUT boundary.

HOLDOUT_START_TS is computed ONCE from the 80th percentile candle timestamp
and stored in backtest_meta.  All subsequent calls read from that row —
never recompute from live candle data — so new candles never move the boundary
(CLAUDE.md rule 7).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterator

_HOLDOUT_KEY = "holdout_start_ts"
_TF_MINUTES: dict[str, int] = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240}


class HoldoutViolation(Exception):
    """Raised when a backtest range extends into the sealed HOLDOUT region."""


@dataclass(frozen=True)
class WFSplit:
    fold: int
    train_start: datetime
    train_end: datetime    # effective end after purging
    test_start: datetime
    test_end: datetime


def _ensure_backtest_meta_table(cur) -> None:
    cur.execute("""
        CREATE TABLE IF NOT EXISTS backtest_meta (
            key         text PRIMARY KEY,
            value       jsonb NOT NULL,
            computed_at timestamptz NOT NULL DEFAULT now()
        )
    """)


def get_holdout_start(
    symbol: str = "XAUUSD@",
    tf: str = "M5",
    *,
    _conn=None,
) -> datetime:
    """Return the frozen HOLDOUT boundary.  Computes on first call, reads cache after."""
    from src.features.levels_store import get_connection

    own_conn = _conn is None
    conn = get_connection() if own_conn else _conn
    try:
        with conn.cursor() as cur:
            _ensure_backtest_meta_table(cur)
            cur.execute(
                "SELECT value FROM backtest_meta WHERE key = %s",
                (_HOLDOUT_KEY,),
            )
            row = cur.fetchone()
            if row is not None:
                raw = row[0]
                val = json.loads(raw) if isinstance(raw, str) else raw
                return datetime.fromisoformat(str(val))

            # First call: compute 80th-percentile candle timestamp and persist.
            cur.execute(
                "SELECT percentile_disc(0.8) WITHIN GROUP (ORDER BY ts_utc) "
                "FROM candles WHERE symbol = %s AND tf = %s",
                (symbol, tf),
            )
            boundary = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO backtest_meta(key, value, computed_at) "
                "VALUES(%s, to_json(%s::text)::jsonb, now())",
                (_HOLDOUT_KEY, boundary.isoformat()),
            )
            conn.commit()
            return boundary
    finally:
        if own_conn:
            conn.close()


def walk_forward_splits(
    from_ts: datetime,
    to_ts: datetime,
    train_candles: int,
    test_candles: int,
    embargo_candles: int,
    purge_candles: int,
    tf: str = "M5",
) -> Iterator[WFSplit]:
    """Yield non-overlapping walk-forward folds with purge+embargo gap.

    Timeline per fold:
        [train_start .. raw_boundary - purge_dur] [purge] [embargo] [test_start .. test_end]

    Neither train_end nor test_start touches the forbidden zone.
    """
    tf_min = _TF_MINUTES.get(tf, 5)
    train_dur   = timedelta(minutes=tf_min * train_candles)
    test_dur    = timedelta(minutes=tf_min * test_candles)
    embargo_dur = timedelta(minutes=tf_min * embargo_candles)
    purge_dur   = timedelta(minutes=tf_min * purge_candles)

    raw_boundary = from_ts + train_dur
    fold = 0
    while raw_boundary + embargo_dur + test_dur <= to_ts:
        yield WFSplit(
            fold=fold,
            train_start=raw_boundary - train_dur,
            train_end=raw_boundary - purge_dur,
            test_start=raw_boundary + embargo_dur,
            test_end=raw_boundary + embargo_dur + test_dur,
        )
        raw_boundary += test_dur
        fold += 1
