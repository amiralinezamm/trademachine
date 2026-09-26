"""Migration: walk_forward_runs table.

Run once:  cd /opt/xauusd-bot && python3 src/backtest/migrate_wf_runs.py
Idempotent: safe to run again (CREATE IF NOT EXISTS).
"""
from __future__ import annotations
import sys
sys.path.insert(0, "/opt/xauusd-bot")
from src.features.levels_store import get_connection

def run() -> None:
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS walk_forward_runs (
                        id              serial PRIMARY KEY,
                        run_at          timestamptz NOT NULL DEFAULT now(),
                        from_ts         timestamptz NOT NULL,
                        to_ts           timestamptz NOT NULL,
                        symbol          text NOT NULL DEFAULT 'XAUUSD@',
                        tf              text NOT NULL DEFAULT 'M5',
                        config          jsonb NOT NULL,
                        n_folds         int,
                        n_signals       int,
                        mean_expectancy numeric(12,6),
                        std_expectancy  numeric(12,6),
                        mean_winrate    numeric(8,4),
                        fold_results    jsonb
                    )
                """)
        print("Migration complete.")
        print("  + walk_forward_runs table: created (or already existed)")
    finally:
        conn.close()

if __name__ == "__main__":
    run()
