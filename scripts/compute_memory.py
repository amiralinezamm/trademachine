#!/usr/bin/env python3
"""Nohup compute script for the memory module (SPEC.md 4.10).

Usage (from /opt/xauusd-bot):
  nohup venv/bin/python3 scripts/compute_memory.py XAUUSD@ M5 2026-09-16T12:00:00Z \
      > /tmp/memory_compute.log 2>&1 &
  echo $! > /tmp/memory_compute.pid

The script prints DONE or ERROR as its last line so the API can poll
/tmp/memory_compute.log and return the result.
"""
import sys, os
sys.path.insert(0, '/opt/xauusd-bot')
sys.path.insert(0, '/opt/xauusd-bot/src')
os.chdir('/opt/xauusd-bot')

from dotenv import load_dotenv
load_dotenv('/opt/xauusd-bot/.env')

from datetime import datetime, timezone

symbol = sys.argv[1] if len(sys.argv) > 1 else "XAUUSD@"
tf     = sys.argv[2] if len(sys.argv) > 2 else "M5"
as_of  = sys.argv[3] if len(sys.argv) > 3 else datetime.now(timezone.utc).isoformat()

ts = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
if ts.tzinfo is None:
    ts = ts.replace(tzinfo=timezone.utc)

print(f"[memory] START symbol={symbol} tf={tf} as_of={ts.isoformat()}", flush=True)

from src.features.levels_store import get_connection, fetch_candles
from src.features.memory import compute_memory
from src.features.memory_store import upsert_memory_result

conn = get_connection()
try:
    print("[memory] fetching candles...", flush=True)
    candles = fetch_candles(conn, symbol, tf, ts)
    print(f"[memory] {len(candles)} candles fetched, running stumpy.match()...", flush=True)
    result = compute_memory(candles, ts, symbol, tf)
    if result is None:
        print("ERROR: compute_memory returned None — not enough history")
        sys.exit(1)
    print(f"[memory] n_matches={result['n_matches']} up_ratio={result['up_ratio']:.3f} "
          f"median_return={result['median_return']:.5f} "
          f"ci=[{result['ci_low']:.5f},{result['ci_high']:.5f}]", flush=True)
    upsert_memory_result(conn, result)
    conn.commit()
    print(f"DONE n_matches={result['n_matches']} up_ratio={result['up_ratio']} "
          f"median_return={result['median_return']} "
          f"ci_low={result['ci_low']} ci_high={result['ci_high']}")
finally:
    conn.close()
