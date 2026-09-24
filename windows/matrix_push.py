r"""Stage 2/2 of the matrix module's live ingest (SPEC.md 4.10, D18).

Runs under SYSTEM (same account/scheduled-task shape as the existing
run_ingest.bat -> mt5_live_ingest.py -- the only account with read access
to candlepush_ed25519's ACL, which is deliberately not loosened).

Reads the JSON files matrix_pull.py (runs as `botconnection`, separately)
writes to C:\xauusd-bot\pull\ and pushes each timeframe over the existing
outbound-only SSH channel (candlepush_ed25519 -> candlepush.sh ->
/ingest/candles) -- same idempotent ON CONFLICT DO NOTHING endpoint the M5
pipeline already uses.
"""
import base64
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

UBUNTU_HOST = "82.115.20.124"
UBUNTU_USER = "root"
SSH_KEY = r"C:\xauusd-bot\candlepush_ed25519"
PULL_DIR = r"C:\xauusd-bot\pull"
SYMBOL = "XAUUSD@"
TIMEFRAMES = ["M1", "M15", "M30", "H1", "H4", "D1"]


def broker_offset_hours(utc_now: datetime) -> int:
    """Same DST rule as mt5_live_ingest.py -- broker UTC+3 summer / UTC+2 winter."""
    year = utc_now.year
    march_sundays = [d for d in range(1, 32) if datetime(year, 3, d).weekday() == 6]
    dst_start = datetime(year, 3, march_sundays[1], 0, 0, tzinfo=timezone.utc)
    oct_sundays = [d for d in range(1, 32) if datetime(year, 10, d).weekday() == 6]
    dst_end = datetime(year, 10, oct_sundays[-1], 0, 0, tzinfo=timezone.utc)
    return 3 if dst_start <= utc_now < dst_end else 2


def push(symbol: str, tf: str, candles: list) -> bool:
    # capture_output=True hangs on Windows OpenSSH subprocess (documented
    # gotcha, mt5_live_ingest.py) -- redirect to a temp file instead.
    payload_json = json.dumps({"symbol": symbol, "tf": tf, "candles": candles}, separators=(",", ":"))
    encoded = base64.b64encode(payload_json.encode("utf-8")).decode("ascii")
    with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".log") as tmp:
        out_path = tmp.name
    with open(out_path, "w") as out_f:
        result = subprocess.run(
            [
                "ssh", "-i", SSH_KEY,
                "-o", "StrictHostKeyChecking=no",
                "-o", "BatchMode=yes",
                "-o", "ConnectTimeout=15",
                f"{UBUNTU_USER}@{UBUNTU_HOST}",
                f"send_candles {encoded}",
            ],
            stdout=out_f, stderr=subprocess.STDOUT, timeout=60,
        )
    with open(out_path) as f:
        out = f.read()
    os.unlink(out_path)
    if result.returncode != 0:
        print(f"[{tf}] push failed: {out.strip()}", file=sys.stderr)
    return result.returncode == 0


def main():
    utc_now = datetime.now(timezone.utc)
    offset = broker_offset_hours(utc_now)
    exit_code = 0

    for tf in TIMEFRAMES:
        path = os.path.join(PULL_DIR, f"{tf}.json")
        if not os.path.exists(path):
            print(f"[{tf}] no pull file yet: {path}", file=sys.stderr)
            exit_code = 1
            continue
        with open(path) as f:
            raw = json.load(f)

        candles = []
        for r in raw:
            broker_dt = datetime.fromtimestamp(r["time"], tz=timezone.utc)
            ts_utc = broker_dt - timedelta(hours=offset)
            candles.append({
                "ts_utc": ts_utc.isoformat(),
                "open": r["open"], "high": r["high"], "low": r["low"], "close": r["close"],
                "tick_volume": r["tick_volume"], "spread": r["spread"], "real_volume": r["real_volume"],
            })

        if not candles:
            continue
        if push(SYMBOL, tf, candles):
            print(f"[{tf}] pushed {len(candles)}, latest={candles[-1]['ts_utc']}")
        else:
            exit_code = 1

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
