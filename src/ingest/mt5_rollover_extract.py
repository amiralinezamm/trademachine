"""docs/instrument_rollover.md (SPEC.md 4.8 prerequisite) -- Windows-side
extraction for logical instruments (DXY@/T10Y@/BRENT@) that map to a
broker contract-family pattern (e.g. USINDX.* -> USINDX.U26, USINDX.Z26...).

Runs ONLY on the Windows MT5 server, same reasoning as mt5_extract.py.

Front-month selection mirrors src/ingest/rollover.select_front_month() --
duplicated inline rather than imported, because this script has no
dependency on the rest of the Ubuntu-side codebase and stays a single
copy-pasteable file, matching the existing mt5_extract.py convention.
Mechanism (decided 2026-09-17, docs/instrument_rollover.md Sec.3):
  1. list all symbols starting with --pattern-prefix
  2. disqualify any with trade_mode == 0 (SYMBOL_TRADE_MODE_DISABLED)
  3. among survivors, pick the highest summed tick_volume over the last
     --volume-window-days D1 bars
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

FIELDS = [
    "logical_symbol", "physical_symbol", "tf", "epoch",
    "open", "high", "low", "close", "tick_volume", "spread", "real_volume",
]


def select_front_month(pattern_prefix: str, volume_window_days: int) -> dict:
    all_syms = mt5.symbols_get()
    candidates = [s for s in all_syms if s.name.upper().startswith(pattern_prefix.upper())]
    if not candidates:
        raise RuntimeError(f"No symbols found matching prefix {pattern_prefix!r}")

    survivors = []
    for s in candidates:
        mt5.symbol_select(s.name, True)
        info = mt5.symbol_info(s.name)
        rates = mt5.copy_rates_from_pos(s.name, mt5.TIMEFRAME_D1, 0, volume_window_days)
        vol = 0 if rates is None else int(sum(r["tick_volume"] for r in rates))
        trade_mode = info.trade_mode if info else None
        print(f"  candidate {s.name}: trade_mode={trade_mode} {volume_window_days}day_volume={vol}")
        if trade_mode != 0:
            survivors.append({"symbol": s.name, "trade_mode": trade_mode, "volume_window": vol})

    if not survivors:
        raise RuntimeError(
            f"No non-DISABLED candidate for prefix {pattern_prefix!r} -- "
            f"manual review needed, see docs/instrument_rollover.md Sec.3"
        )
    winner = max(survivors, key=lambda c: c["volume_window"])
    return winner


def extract(physical_symbol: str, tf_name: str, years: float) -> list:
    mt5.symbol_select(physical_symbol, True)
    count = math.ceil(years * 366 * 24 * 60 / TF_MINUTES[tf_name] * 1.15)
    rates = mt5.copy_rates_from_pos(physical_symbol, TF_MAP[tf_name], 0, count)
    if rates is None:
        raise RuntimeError(f"copy_rates_from_pos failed for {physical_symbol}/{tf_name}: {mt5.last_error()}")
    return rates


def write_csv(rows: list[dict], out_path: Path) -> None:
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--logical-symbol", required=True, help="e.g. DXY@")
    parser.add_argument("--pattern-prefix", required=True, help="e.g. USINDX")
    parser.add_argument("--timeframes", nargs="+", required=True, choices=list(TF_MAP))
    parser.add_argument("--years", type=float, required=True)
    parser.add_argument("--volume-window-days", type=int, default=3)
    parser.add_argument("--terminal-path", required=True)
    parser.add_argument("--outdir", required=True)
    args = parser.parse_args()

    if not mt5.initialize(path=args.terminal_path):
        raise RuntimeError(f"initialize() failed: {mt5.last_error()}")

    print(f"=== front-month selection for {args.logical_symbol} (pattern {args.pattern_prefix}.*) ===")
    winner = select_front_month(args.pattern_prefix, args.volume_window_days)
    print(f"SELECTED: {winner['symbol']} (trade_mode={winner['trade_mode']}, "
          f"{args.volume_window_days}day_volume={winner['volume_window']})")

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    for tf_name in args.timeframes:
        rates = extract(winner["symbol"], tf_name, args.years)
        rows = [
            {
                "logical_symbol": args.logical_symbol,
                "physical_symbol": winner["symbol"],
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
            for r in rates
        ]
        out_path = outdir / f"candles_{args.logical_symbol.replace('@', '')}_{tf_name}.csv"
        write_csv(rows, out_path)
        print(f"{tf_name}: wrote {len(rows)} rows -> {out_path} (physical={winner['symbol']})")

    mt5.shutdown()
