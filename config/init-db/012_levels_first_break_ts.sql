-- Migration 012: add first_break_ts column to levels table (Task 1 — expiry overhaul)
-- first_break_ts: timestamp of the first time a level was broken.
-- NULL for unbroken levels (break_count=0) — those keep extreme_max_days=365 expiry.
-- Used for strength-based decay: min(30, 30 * strength / 2.0) days from first break.

ALTER TABLE levels ADD COLUMN IF NOT EXISTS first_break_ts TIMESTAMPTZ NULL;

-- Backfill: for existing broken levels, best approximation = last_touch.
-- (We don't know the exact first break from historical data, so we use last_touch,
--  which is an upper bound — the real first_break_ts was at or before last_touch.)
UPDATE levels
SET first_break_ts = last_touch
WHERE break_count >= 1
  AND last_touch IS NOT NULL
  AND first_break_ts IS NULL;
