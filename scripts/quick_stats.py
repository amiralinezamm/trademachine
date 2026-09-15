"""Quick statistics for the last 30 days of M5 XAUUSD signals.

READ ONLY — makes no DB writes, inserts no new signals.

For each signal in the last 30 days:
  - Direction (BUY / SELL)
  - Checks the candle 5 bars after the signal candle
  - A BUY is a "hit" if close 5 bars later > entry price
  - A SELL is a "hit" if close 5 bars later < entry price

Usage (on server — must use API venv for psycopg2):
    source /opt/xauusd-bot/src/api/venv/bin/activate
    python scripts/quick_stats.py

Requires DATABASE_URL in environment or .env file.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import psycopg2
from psycopg2.extras import RealDictCursor

try:
    from dotenv import load_dotenv
    load_dotenv("/opt/xauusd-bot/.env")
except ImportError:
    pass

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    # Fallback: build from individual vars (matches docker-compose env)
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    user = os.environ.get("POSTGRES_USER", "xauusd")
    pw   = os.environ.get("POSTGRES_PASSWORD", "")
    db   = os.environ.get("POSTGRES_DB", "xauusd_bot")
    DATABASE_URL = f"postgresql://{user}:{pw}@{host}:{port}/{db}"

SYMBOL = "XAUUSD@"
TF = "M5"
LOOKBACK_DAYS = 30
FORWARD_BARS = 5


def main():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    conn.set_session(readonly=True)

    since = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, ts_utc, direction, entry, rule_version
            FROM signals
            WHERE ts_utc >= %s
            ORDER BY ts_utc ASC
            """,
            (since,),
        )
        signals = cur.fetchall()

    if not signals:
        print(f"No signals found in last {LOOKBACK_DAYS} days.")
        conn.close()
        return

    print(f"\nSignals in last {LOOKBACK_DAYS} days: {len(signals)}")
    print(f"{'Direction':<10}{'Count':>7}")
    for direction in ("BUY", "SELL"):
        count = sum(1 for s in signals if s["direction"] == direction)
        print(f"  {direction:<8}{count:>7}")

    # 5-candles-later direction check
    hits = 0
    misses = 0
    no_data = 0

    with conn.cursor() as cur:
        for sig in signals:
            sig_ts = sig["ts_utc"]
            # Find the candle 5 M5 bars after the signal candle
            cur.execute(
                """
                SELECT close
                FROM candles
                WHERE symbol = %s AND tf = %s AND ts_utc > %s
                ORDER BY ts_utc ASC
                LIMIT 1
                OFFSET %s
                """,
                (SYMBOL, TF, sig_ts, FORWARD_BARS - 1),
            )
            row = cur.fetchone()
            if row is None:
                no_data += 1
                continue

            future_close = float(row["close"])
            entry = float(sig["entry"])

            if sig["direction"] == "BUY":
                hit = future_close > entry
            else:
                hit = future_close < entry

            if hit:
                hits += 1
            else:
                misses += 1

    evaluated = hits + misses
    print(f"\n5-candles-later direction check (entry price as threshold):")
    print(f"  Evaluated:  {evaluated}")
    print(f"  Correct:    {hits}  ({hits/evaluated*100:.1f}%)" if evaluated else "  Correct:    0")
    print(f"  Wrong:      {misses}  ({misses/evaluated*100:.1f}%)" if evaluated else "  Wrong:      0")
    print(f"  No data:    {no_data}")

    conn.close()


if __name__ == "__main__":
    main()
