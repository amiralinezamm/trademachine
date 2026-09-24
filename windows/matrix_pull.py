r"""Stage 1/2 of the matrix module's live ingest (SPEC.md 4.10, D18).

Runs under the `botconnection` Windows user (S4U scheduled task, no stored
password) -- the account the MT5 terminal itself runs under. MUST run as
this user, not SYSTEM: mt5.initialize() only attaches via IPC to a
terminal running in the SAME user's session; tested 2026-09-24 and SYSTEM
gets "IPC initialize failed, MetaTrader 5 x64 not found".

Pulls M1, M15, M30, H1, H4, D1 independently via copy_rates_from_pos() and
writes one JSON file per timeframe to C:\xauusd-bot\pull\ -- mirrors
CandleExporter.mq5's own EA-writes-JSON-file pattern, just in Python
instead of MQL5 (no need for a second EA/chart attachment).

Stage 2 (matrix_push.py) runs separately, under SYSTEM (the only account
with read access to the candlepush_ed25519 key's ACL -- deliberately not
loosened), and pushes these files over the existing SSH channel. Two
separate scheduled tasks, same split-by-account-capability shape as the
existing M5 EA -> mt5_live_ingest.py pipeline.
"""
import json
import os
import sys
from datetime import datetime, timezone

import MetaTrader5 as mt5

SYMBOL = "XAUUSD@"
TIMEFRAMES = {
    "M1": mt5.TIMEFRAME_M1, "M15": mt5.TIMEFRAME_M15, "M30": mt5.TIMEFRAME_M30,
    "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4, "D1": mt5.TIMEFRAME_D1,
}
OUT_DIR = r"C:\xauusd-bot\pull"
BARS = 20  # small rolling window for the recurring 5-min run


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    if not mt5.initialize():
        print(f"mt5.initialize() failed: {mt5.last_error()}", file=sys.stderr)
        sys.exit(1)
    try:
        if not mt5.symbol_select(SYMBOL, True):
            print(f"symbol_select failed: {mt5.last_error()}", file=sys.stderr)
            sys.exit(1)

        for tf_name, period in TIMEFRAMES.items():
            rates = mt5.copy_rates_from_pos(SYMBOL, period, 1, BARS)  # index 1 skips the forming bar
            if rates is None or len(rates) == 0:
                print(f"[{tf_name}] no data: {mt5.last_error()}", file=sys.stderr)
                continue
            data = [
                {
                    "time": int(r["time"]), "open": float(r["open"]), "high": float(r["high"]),
                    "low": float(r["low"]), "close": float(r["close"]),
                    "tick_volume": int(r["tick_volume"]), "spread": int(r["spread"]),
                    "real_volume": int(r["real_volume"]),
                }
                for r in rates
            ]
            out_path = os.path.join(OUT_DIR, f"{tf_name}.json")
            tmp_path = out_path + ".tmp"
            with open(tmp_path, "w") as f:
                json.dump(data, f)
            os.replace(tmp_path, out_path)  # atomic -- push stage never sees a half-written file
            print(f"[{tf_name}] pulled {len(data)} bars -> {out_path}")
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()
