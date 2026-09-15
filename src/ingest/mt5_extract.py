"""SPEC.md 4.1 (`mt5_bridge`) — Windows-side extraction half.

Runs ONLY on the Windows MT5 server (the MetaTrader5 package requires a
live local terminal via IPC; there is no way to run it from the Ubuntu
box). This script is copied there manually — no git on that host.

Deliberately does NO timezone conversion. It dumps raw MT5 epoch values
exactly as the API returns them (broker wall-clock time, mislabeled as
UTC by Python's utcfromtimestamp — see src/ingest/timezones.py for why).
Conversion happens in exactly one place, load_candles.py on the Ubuntu
side, so the DST rule has a single source of truth instead of two copies
that could quietly drift apart.

Output: one CSV per timeframe, to be scp'd to the Ubuntu server and
loaded with load_candles.py.
"""
from __future__ import annotations

import argparse
import csv
import math
from pathlib import Path

import MetaTrader5 as mt5

TF_MAP = {
    "M1": mt5.TIMEFRAME_M1,
    "M5": mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15,
    "H1": mt5.TIMEFRAME_H1,
}
TF_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "H1": 60}

FIELDS = ["symbol", "tf", "epoch", "open", "high", "low", "close", "tick_volume", "spread", "real_volume"]


def extract(symbol: str, tf_name: str, years: float, terminal_path: str) -> list[dict]:
    if not mt5.initialize(path=terminal_path):
        raise RuntimeError(f"initialize() failed: {mt5.last_error()}")
    mt5.symbol_select(symbol, True)

    # Generous over-estimate (buffer for weekend/holiday closures already
    # reduces real bar count well below calendar-time count) — the exact
    # 3-year cutoff is enforced later, on the Ubuntu side, from config.
    count = math.ceil(years * 366 * 24 * 60 / TF_MINUTES[tf_name] * 1.15)
    rates = mt5.copy_rates_from_pos(symbol, TF_MAP[tf_name], 0, count)
    mt5.shutdown()
    if rates is None:
        raise RuntimeError(f"copy_rates_from_pos failed for {tf_name}: {mt5.last_error()}")

    rows = []
    for r in rates:
        rows.append(
            {
                "symbol": symbol,
                "tf": tf_name,
                "epoch": int(r["time"]),
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "tick_volume": int(r["tick_volume"]),
                "spread": int(r["spread"]),
                "real_volume": int(r["real_volume"]),
            }
        )
    return rows


def write_csv(rows: list[dict], out_path: Path) -> None:
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--timeframes", nargs="+", required=True, choices=list(TF_MAP))
    parser.add_argument("--years", type=float, required=True)
    parser.add_argument("--terminal-path", required=True)
    parser.add_argument("--outdir", required=True)
    args = parser.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    for tf_name in args.timeframes:
        rows = extract(args.symbol, tf_name, args.years, args.terminal_path)
        out_path = outdir / f"candles_{args.symbol.replace('@', '')}_{tf_name}.csv"
        write_csv(rows, out_path)
        print(f"{tf_name}: wrote {len(rows)} rows -> {out_path}")
