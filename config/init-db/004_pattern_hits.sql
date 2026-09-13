-- SPEC.md section 4.3 (`patterns`) — copied verbatim.
-- Depends on levels (at_level_id FK) — must run after 003_levels.sql.
CREATE TABLE pattern_hits (
  ts_utc        timestamptz NOT NULL,
  tf            text NOT NULL,
  pattern       text NOT NULL,          -- 'CDLENGULFING'
  direction     smallint NOT NULL,      -- +100 / -100
  body_atr      numeric(8,4),           -- نسبت بدنه به ATR
  at_level_id   bigint REFERENCES levels(id),
  level_strength numeric(8,4),
  regime        text,                   -- 'trend' | 'range'
  PRIMARY KEY (ts_utc, tf, pattern)
);
