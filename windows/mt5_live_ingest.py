"""Live poller: read candle data exported by CandleExporter.mq5 (MT5 EA)
and push to the Ubuntu API via SSH.

No MetaTrader5 Python API needed — the EA writes a JSON file to MT5's
Files directory. This script reads it and sends via SSH using the
restricted candlepush key (server-side command= runs candlepush.sh).

Run every 5 minutes via Windows Task Scheduler (can run as SYSTEM).
Idempotent: re-sends the last few candles; /ingest/candles uses ON CONFLICT DO NOTHING.
"""
import base64
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

# --- Configuration ---
UBUNTU_HOST = "82.115.20.124"
UBUNTU_USER = "root"
SSH_KEY = r"C:\xauusd-bot\candlepush_ed25519"

# Path where CandleExporter.mq5 writes candle data (MT5 Files directory)
MT5_CANDLES_FILE = (
    r"C:\Users\Administrator\AppData\Roaming\MetaQuotes\Terminal"
    r"\6F2BC4B36A0BAC4B951FC9ADDEF025F7\MQL5\Files\xauusd_candles.json"
)

SYMBOL = "XAUUSD@"
TF = "M5"


def broker_offset_hours(utc_now: datetime) -> int:
    """DST offset for broker UTC+3 (summer) / UTC+2 (winter).
    Same rule as config/params.yaml mt5_bridge.server_timezone."""
    year = utc_now.year
    march_sundays = [d for d in range(1, 32) if datetime(year, 3, d).weekday() == 6]
    dst_start = datetime(year, 3, march_sundays[1], 0, 0, tzinfo=timezone.utc)
    oct_sundays = [d for d in range(1, 32) if datetime(year, 10, d).weekday() == 6]
    dst_end = datetime(year, 10, oct_sundays[-1], 0, 0, tzinfo=timezone.utc)
    return 3 if dst_start <= utc_now < dst_end else 2


def main():
    if not os.path.exists(MT5_CANDLES_FILE):
        print(f"candles file not found: {MT5_CANDLES_FILE}", file=sys.stderr)
        sys.exit(1)

    with open(MT5_CANDLES_FILE, "r", encoding="utf-8") as f:
        ea_data = json.load(f)

    utc_now = datetime.now(timezone.utc)
    offset = broker_offset_hours(utc_now)

    candles = []
    for c in ea_data.get("candles", []):
        broker_dt = datetime.fromtimestamp(int(c["broker_ts"]), tz=timezone.utc)
        ts_utc = broker_dt - timedelta(hours=offset)
        candles.append({
            "ts_utc": ts_utc.isoformat(),
            "open": float(c["open"]),
            "high": float(c["high"]),
            "low": float(c["low"]),
            "close": float(c["close"]),
            "tick_volume": int(c.get("tick_volume", 0)),
            "spread": int(c.get("spread", 0)),
            "real_volume": int(c.get("real_volume", 0)),
        })

    if not candles:
        print("no candles in file", file=sys.stderr)
        sys.exit(1)

    payload_json = json.dumps({"symbol": SYMBOL, "tf": TF, "candles": candles}, separators=(",", ":"))
    encoded = base64.b64encode(payload_json.encode("utf-8")).decode("ascii")

    # SSH runs candlepush.sh on Ubuntu (via command= restriction).
    # Do NOT use capture_output=True — causes hang on Windows OpenSSH subprocess.
    result = subprocess.run(
        [
            "ssh",
            "-i", SSH_KEY,
            "-o", "StrictHostKeyChecking=no",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=15",
            f"{UBUNTU_USER}@{UBUNTU_HOST}",
            f"send_candles {encoded}",
        ],
        timeout=30,
    )

    if result.returncode != 0:
        print(f"SSH failed (rc={result.returncode})", file=sys.stderr)
        sys.exit(1)

    print(f"pushed {len(candles)} candles OK")


if __name__ == "__main__":
    main()
