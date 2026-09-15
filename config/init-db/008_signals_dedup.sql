-- Prevent duplicate signals for the same candle + rule combination.
-- ON CONFLICT DO NOTHING in insert_signal relies on this.
ALTER TABLE signals ADD CONSTRAINT signals_candle_rule_unique
    UNIQUE (ts_utc, rule_version);
