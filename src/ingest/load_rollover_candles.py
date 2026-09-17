"""docs/instrument_rollover.md (SPEC.md 4.8 prerequisite) -- Ubuntu-side
load half for the rollover-managed instruments (DXY@/T10Y@/BRENT@).

Takes CSVs produced by mt5_rollover_extract.py (scp'd over from the Windows
MT5 server), applies the SAME broker->UTC conversion as the main XAUUSD@
pipeline (single source of truth: src/ingest/timezones.py -- these
instruments quote from the same MT5 terminal/account, so the same DST rule
applies), trims to the same backfill_years window as XAUUSD@
(mt5_bridge.backfill_years, for consistency), drops any candle that hasn't
fully closed (CLAUDE.md rule 3), and upserts under the LOGICAL symbol name
(never the physical contract name) via the same upsert_candles() the API
endpoint uses (CLAUDE.md rule 6).

Also records exactly ONE instrument_contracts row per logical_symbol for
the whole run (not per timeframe -- "which physical contract is currently
mapped" is one fact about the instrument, independent of which timeframe
you're asking about). valid_from_ts is the earliest ts_utc across every
timeframe loaded (finest granularity, normally M1). This is the FIRST
backfill for each of these three instruments, so valid_to_ts is left NULL
(still the active mapped contract) and adjustment_offset is 0 (nothing to
splice against yet; see docs/instrument_rollover.md Sec.3 note on the
broker apparently already serving a long continuous history under each
contract label).
"""
from __future__ import annotations

import argparse
import csv
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.ingest.candles_store import filter_closed_candles, upsert_candles
from src.ingest.instrument_contracts_store import get_connection, upsert_instrument_contract
from src.ingest.timezones import broker_epoch_to_utc, load_mt5_params

logger = logging.getLogger(__name__)


def load_csv_rows(csv_path: Path, tz_params: dict) -> tuple[list[dict], str]:
    """Returns (candle_dicts, physical_symbol) -- physical_symbol is read
    from the CSV's own column (all rows in one file share it; consistency
    checked)."""
    rows = []
    physical_symbols: set[str] = set()
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            physical_symbols.add(row["physical_symbol"])
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
    if len(physical_symbols) > 1:
        raise ValueError(f"{csv_path} mixes multiple physical symbols: {physical_symbols} -- expected exactly one")
    physical_symbol = physical_symbols.pop() if physical_symbols else None
    return rows, physical_symbol


def trim_to_window(candles: list[dict], years: float, now_utc: datetime) -> list[dict]:
    cutoff = now_utc - timedelta(days=years * 365.25)
    return [c for c in candles if c["ts_utc"] >= cutoff]


def load_one_timeframe(
    csv_path: Path, logical_symbol: str, tf: str, params: dict, conn, now_utc: datetime | None = None
) -> dict:
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    raw, physical_symbol = load_csv_rows(csv_path, tz_params=params)
    windowed = trim_to_window(raw, params["backfill_years"], now_utc)
    closed = filter_closed_candles(windowed, tf, now_utc)

    inserted = upsert_candles(conn, logical_symbol, tf, closed)
    conn.commit()

    oldest = min((c["ts_utc"] for c in closed), default=None)
    newest = max((c["ts_utc"] for c in closed), default=None)

    return {
        "tf": tf,
        "physical_symbol": physical_symbol,
        "raw_count": len(raw),
        "windowed_count": len(windowed),
        "closed_count": len(closed),
        "inserted_count": inserted,
        "oldest": oldest,
        "newest": newest,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument("--logical-symbol", required=True, help="e.g. DXY@")
    parser.add_argument("--indir", required=True, help="directory containing candles_<LOGICAL>_<TF>.csv files")
    parser.add_argument("--timeframes", nargs="+", required=True)
    args = parser.parse_args()

    params = load_mt5_params()  # shares mt5_bridge.server_timezone + backfill_years with XAUUSD@
    indir = Path(args.indir)
    now_utc = datetime.now(timezone.utc)
    logical_name = args.logical_symbol.replace("@", "")

    conn = get_connection()
    try:
        results = []
        for tf in args.timeframes:
            csv_path = indir / f"candles_{logical_name}_{tf}.csv"
            if not csv_path.exists():
                logger.warning("missing CSV for %s: %s", tf, csv_path)
                continue
            result = load_one_timeframe(csv_path, args.logical_symbol, tf, params, conn, now_utc)
            results.append(result)
            logger.info(
                "%s %s (physical=%s): raw=%d windowed=%d closed=%d inserted=%d range=%s..%s",
                args.logical_symbol, result["tf"], result["physical_symbol"],
                result["raw_count"], result["windowed_count"],
                result["closed_count"], result["inserted_count"],
                result["oldest"], result["newest"],
            )

        # One instrument_contracts row per logical_symbol for the whole run
        # (not per timeframe) -- "which physical contract is mapped right
        # now" is one fact, independent of timeframe. valid_from_ts is the
        # earliest ts_utc across all timeframes actually loaded.
        physical_symbols = {r["physical_symbol"] for r in results if r["physical_symbol"]}
        if len(physical_symbols) > 1:
            raise ValueError(f"timeframes for {args.logical_symbol} disagree on physical symbol: {physical_symbols}")
        oldest_overall = min((r["oldest"] for r in results if r["oldest"] is not None), default=None)
        if physical_symbols and oldest_overall is not None:
            upsert_instrument_contract(
                conn,
                logical_symbol=args.logical_symbol,
                physical_symbol=physical_symbols.pop(),
                valid_from_ts=oldest_overall,
                valid_to_ts=None,
                adjustment_offset=0,
            )
            conn.commit()
    finally:
        conn.close()
