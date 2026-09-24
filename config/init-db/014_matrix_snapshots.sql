-- SPEC.md 4.10 (`matrix`, decision D18) -- multi-timeframe voting engine.
-- Isolated: no FK to levels/signals. One row per M5 pivot bar (the
-- timeframe D2 anchors matrix_score to), timeframes stored as jsonb
-- matching the exact components.matrix shape (SPEC.md 4.10), same
-- convention as signals.components.

CREATE TABLE matrix_snapshots (
  id          bigserial PRIMARY KEY,
  symbol      text NOT NULL,
  ts_utc      timestamptz NOT NULL,   -- M5 pivot bar's ts_utc
  score       smallint NOT NULL,       -- 0-6, count of non-M5 TFs agreeing with M5
  direction   text NOT NULL,           -- 'buy' | 'sell' | 'neutral'
  timeframes  jsonb NOT NULL,          -- {"M1":"buy","M5":"buy",...}
  created_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (symbol, ts_utc),
  CONSTRAINT matrix_snapshots_direction_check CHECK (direction IN ('buy', 'sell', 'neutral')),
  CONSTRAINT matrix_snapshots_score_check CHECK (score BETWEEN 0 AND 6)
);

CREATE INDEX ix_matrix_snapshots_symbol_ts ON matrix_snapshots (symbol, ts_utc);
