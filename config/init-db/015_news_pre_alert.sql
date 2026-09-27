-- Pre-release Telegram alert flag ("30 minutes before" push, task from
-- 2026-09-27 news revamp). Deliberately a separate column from `notified`
-- (002_news_events.sql) — that column is reserved for the post-release
-- actual_watcher flow (a different semantic: "actual arrived, release
-- message sent") and this project's rule is columns are never repurposed
-- silently once another flow might rely on their meaning.

ALTER TABLE news_events
  ADD COLUMN IF NOT EXISTS pre_alert_sent boolean NOT NULL DEFAULT false;

CREATE INDEX IF NOT EXISTS idx_news_events_pre_alert_pending
  ON news_events (ts_utc)
  WHERE pre_alert_sent = false;
