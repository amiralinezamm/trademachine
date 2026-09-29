-- "توقف اجباری" rule (2026-09-29): tracks whether a manual-close advisory
-- was already sent for this signal, so a still-open position whose H1
-- structure has flipped against it doesn't get re-alerted on every 5-minute
-- poll. Separate column from `outcome` (tp/sl/level_invalidated/timeout) --
-- an alerted position is not necessarily closed yet, the user closes it
-- manually.

ALTER TABLE signals
  ADD COLUMN IF NOT EXISTS reversal_alert_sent boolean NOT NULL DEFAULT false;

CREATE INDEX IF NOT EXISTS idx_signals_open_by_direction
  ON signals (direction, ts_utc DESC)
  WHERE outcome IS NULL;
