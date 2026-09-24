"""Headless multi-timeframe ingest for the matrix module (SPEC.md 4.10 / D18).

Confirmed 2026-09-24 by direct test: MetaTrader5.initialize() succeeds with
NO active RDP session (both `administrator` and `botconnection` sessions were
State=Disc at test time) -- it attaches to the already-running, already
logged-in WM Markets MT5 Terminal via IPC. No GUI/RDP needed for this script
to run on a schedule.

Pulls M1, M15, M30, H1, H4, D1 INDEPENDENTLY via
MetaTrader5.copy_rates_from_pos() -- real broker-native candles per
timeframe, NOT resampled from M5 (explicit project decision: resampling
was rejected). M5 itself is untouched by this script -- CandleExporter.mq5
+ mt5_live_ingest.py continue to own the M5 live path unchanged.

Pushes over the SAME outbound-only SSH channel the M5 pipeline already uses
(candlepush_ed25519 key -> candlepush.sh -> POST /ingest/candles) -- no new
inbound port, matching the project's fixed security constraint (only
22/SSH and 3389/RDP open, all data transfer outbound Windows->Ubuntu).

Usage:
    python matrix_tf_ingest.py --bars 20          # recurring 5-min run (small window)
    python matrix_tf_ingest.py --bars 60000        # one-time backfill (full history)
    python matrix_tf_ingest.py --bars 20 --tf M1 M15 H1   # subset

NOTE: M30/H4/D1 are new tf values -- /ingest/candles on the CURRENTLY
RUNNING xauusd-api.service process will reject them with 422 until that
process is restarted to pick up the updated TF_MINUTES dict in
src/ingest/candles_store.py (already patched on disk, not yet live). The
live service was deliberately NOT restarted as part of this task (see
final report) -- for those two timeframes this script's push step will
fail with a clear error until a restart is explicitly approved. M1/M15/H1
are already supported and push successfully right now.
"""
import argparse
import base64
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import MetaTrader5 as mt5

UBUNTU_HOST = "82.115.20.124"
UBUNTU_USER = "root"
SSH_KEY = r"C:\xauusd-bot\candlepush_ed25519"

SYMBOL = "XAUUSD@"
ALL_TIMEFRAMES = {
    "M1": mt5.TIMEFRAME_M1,
    "M15": mt5.TIMEFRAME_M15,
    "M30": mt5.TIMEFRAME_M30,
    "H1": mt5.TIMEFRAME_H1,
    "H4": mt5.TIMEFRAME_H4,
    "D1": mt5.TIMEFRAME_D1,
}
BATCH_SIZE = 100  # Windows CreateProcess command-line length limit (~32KB) forces small batches


def broker_offset_hours(utc_now: datetime) -> int:
    """Same DST rule as mt5_live_ingest.py -- broker UTC+3 summer / UTC+2 winter."""
    year = utc_now.year
    march_sundays = [d for d in range(1, 32) if datetime(year, 3, d).weekday() == 6]
    dst_start = datetime(year, 3, march_sundays[1], 0, 0, tzinfo=timezone.utc)
    oct_sundays = [d for d in range(1, 32) if datetime(year, 10, d).weekday() == 6]
    dst_end = datetime(year, 10, oct_sundays[-1], 0, 0, tzinfo=timezone.utc)
    return 3 if dst_start <= utc_now < dst_end else 2


def push_batch(symbol: str, tf: str, candles: list) -> tuple[bool, str]:
    # Do NOT use capture_output=True here -- hangs on Windows OpenSSH
    # subprocess (same gotcha documented in mt5_live_ingest.py). Redirect
    # stdout/stderr to a temp file instead and read it back.
    payload_json = json.dumps({"symbol": symbol, "tf": tf, "candles": candles}, separators=(",", ":"))
    encoded = base64.b64encode(payload_json.encode("utf-8")).decode("ascii")
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w+", delete=False, suffix=".log") as tf_out:
        out_path = tf_out.name
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
    import os
    os.unlink(out_path)
    return result.returncode == 0, out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bars", type=int, default=20, help="bars to pull per timeframe (index 1..bars, skips the still-forming bar)")
    parser.add_argument("--tf", nargs="*", default=list(ALL_TIMEFRAMES.keys()), help="subset of timeframes")
    args = parser.parse_args()

    ok = mt5.initialize()
    if not ok:
        print(f"mt5.initialize() failed: {mt5.last_error()}", file=sys.stderr)
        sys.exit(1)

    try:
        if not mt5.symbol_select(SYMBOL, True):
            print(f"symbol_select({SYMBOL}) failed: {mt5.last_error()}", file=sys.stderr)
            sys.exit(1)

        utc_now = datetime.now(timezone.utc)
        offset = broker_offset_hours(utc_now)

        exit_code = 0
        for tf_name in args.tf:
            period = ALL_TIMEFRAMES[tf_name]
            # index 1 skips the still-forming current bar -- same anti-repaint
            # convention as CandleExporter.mq5's live M5 export.
            rates = mt5.copy_rates_from_pos(SYMBOL, period, 1, args.bars)
            if rates is None or len(rates) == 0:
                print(f"[{tf_name}] no data: {mt5.last_error()}", file=sys.stderr)
                exit_code = 1
                continue

            candles = []
            for r in rates:
                broker_dt = datetime.fromtimestamp(int(r["time"]), tz=timezone.utc)
                ts_utc = broker_dt - timedelta(hours=offset)
                candles.append({
                    "ts_utc": ts_utc.isoformat(),
                    "open": float(r["open"]), "high": float(r["high"]),
                    "low": float(r["low"]), "close": float(r["close"]),
                    "tick_volume": int(r["tick_volume"]),
                    "spread": int(r["spread"]),
                    "real_volume": int(r["real_volume"]),
                })

            pushed = 0
            for i in range(0, len(candles), BATCH_SIZE):
                batch = candles[i:i + BATCH_SIZE]
                success, out = push_batch(SYMBOL, tf_name, batch)
                if not success:
                    print(f"[{tf_name}] batch {i}-{i+len(batch)} FAILED: {out.strip()}", file=sys.stderr)
                    exit_code = 1
                    break
                pushed += len(batch)
            print(f"[{tf_name}] pulled {len(candles)}, pushed {pushed}, range {candles[0]['ts_utc']} .. {candles[-1]['ts_utc']}")

        sys.exit(exit_code)
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()
