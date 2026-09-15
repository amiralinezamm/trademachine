"""SPEC.md 4.1 (`mt5_bridge`) — Ubuntu-side load half.

Takes CSVs produced by mt5_extract.py (scp'd over from the Windows MT5
server), applies the broker->UTC conversion (single source of truth:
src/ingest/timezones.py), trims to the configured backfill window,
drops any candle that hasn't fully closed (CLAUDE.md rule 3), and
upserts via the same function the API endpoint uses (CLAUDE.md rule 6).
"""
from __future__ import annotations

import argparse
import csv
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

from src.ingest.candles_store import filter_closed_candles, upsert_candles
from src.ingest.timezones import broker_epoch_to_utc, load_mt5_params

logger = logging.getLogger(__name__)


def load_csv_rows(csv_path: Path, tz_params: dict) -> list[dict]:
    rows = []
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            ts_utc = broker_epoch_to_utc(int(row["epoch"]), params=tz_params)
            rows.append(
                {
                    "ts_utc": ts_utc,
                    "open": row["open"],
                    "high": row["high"],
                    "low": row["low"],
                    "close": row["close"],
                    "tick_volume": row["tick_volume"],
                    "spread": row["spread"],
                    "real_volume": row["real_volume"],
                }
            )
    return rows


def trim_to_window(candles: list[dict], years: float, now_utc: datetime) -> list[dict]:
    cutoff = now_utc - timedelta(days=years * 365.25)
    return [c for c in candles if c["ts_utc"] >= cutoff]


def get_connection():
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    return psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST_LOCAL", "127.0.0.1"),
        port=os.environ.get("POSTGRES_PORT", "5432"),
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        dbname=os.environ["POSTGRES_DB"],
    )


def load_one_timeframe(csv_path: Path, symbol: str, tf: str, params: dict, conn, now_utc: datetime | None = None) -> dict:
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    raw = load_csv_rows(csv_path, tz_params=params)
    windowed = trim_to_window(raw, params["backfill_years"], now_utc)
    closed = filter_closed_candles(windowed, tf, now_utc)

    inserted = upsert_candles(conn, symbol, tf, closed)
    conn.commit()

    return {
        "tf": tf,
        "raw_count": len(raw),
        "windowed_count": len(windowed),
        "closed_count": len(closed),
        "inserted_count": inserted,
        "oldest": min((c["ts_utc"] for c in closed), default=None),
        "newest": max((c["ts_utc"] for c in closed), default=None),
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument("--indir", required=True, help="directory containing candles_<SYMBOL>_<TF>.csv files")
    args = parser.parse_args()

    params = load_mt5_params()
    symbol = params["symbol"]
    indir = Path(args.indir)
    now_utc = datetime.now(timezone.utc)

    conn = get_connection()
    try:
        for tf in params["timeframes"]:
            csv_path = indir / f"candles_{symbol.replace('@', '')}_{tf}.csv"
            if not csv_path.exists():
                logger.warning("missing CSV for %s: %s", tf, csv_path)
                continue
            result = load_one_timeframe(csv_path, symbol, tf, params, conn, now_utc)
            logger.info(
                "%s: raw=%d windowed=%d closed=%d inserted=%d range=%s..%s",
                result["tf"], result["raw_count"], result["windowed_count"],
                result["closed_count"], result["inserted_count"],
                result["oldest"], result["newest"],
            )
    finally:
        conn.close()
