"""Migration: backtest_meta table + rules.holdout_tested_at column.

Run once:  cd /opt/xauusd-bot && python3 src/backtest/migrate_holdout.py
Idempotent: safe to run again (CREATE IF NOT EXISTS, ADD COLUMN IF NOT EXISTS).
"""
from __future__ import annotations

import json
import sys
sys.path.insert(0, "/opt/xauusd-bot")

from src.features.levels_store import get_connection

def run() -> None:
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS backtest_meta (
                        key         text PRIMARY KEY,
                        value       jsonb NOT NULL,
                        computed_at timestamptz NOT NULL DEFAULT now()
                    )
                """)
                cur.execute("""
                    ALTER TABLE rules
                        ADD COLUMN IF NOT EXISTS holdout_tested_at timestamptz DEFAULT NULL
                """)
        print("Migration complete.")
        print("  + backtest_meta table: created (or already existed)")
        print("  + rules.holdout_tested_at: added (or already existed, NULL for all rows)")

        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM backtest_meta WHERE key = 'holdout_start_ts'")
                row = cur.fetchone()
                if row is None:
                    cur.execute("""
                        SELECT percentile_disc(0.8) WITHIN GROUP (ORDER BY ts_utc)
                        FROM candles WHERE symbol = 'XAUUSD@' AND tf = 'M5'
                    """)
                    boundary = cur.fetchone()[0]
                    cur.execute(
                        "INSERT INTO backtest_meta(key, value, computed_at) "
                        "VALUES('holdout_start_ts', to_json(%s::text)::jsonb, now())",
                        (boundary.isoformat(),),
                    )
                    print(f"  + HOLDOUT_START_TS computed and persisted: {boundary.isoformat()} UTC")
                else:
                    val = json.loads(row[0]) if isinstance(row[0], str) else row[0]
                    print(f"  + HOLDOUT_START_TS already persisted: {val} UTC (not recomputed)")
    finally:
        conn.close()

if __name__ == "__main__":
    run()
