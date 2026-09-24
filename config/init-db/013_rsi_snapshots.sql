-- RSI + MACD divergence module (proposed, not yet in SPEC.md -- see the
-- 2026-09-24 task report). Isolated: no FK from/to any table the live
-- signal engine reads (levels, signals). Two tables, mirroring the existing
-- snapshot/episode split used by correlation.py (pressure_snapshots +
-- pressure_episodes):
--   rsi_snapshots     -- per-bar RSI + MACD values (part A)
--   divergence_events -- price/RSI and price/MACD divergence occurrences (parts B, C)

CREATE TABLE rsi_snapshots (
  id           bigserial PRIMARY KEY,
  symbol       text NOT NULL,
  tf           text NOT NULL,
  ts_utc       timestamptz NOT NULL,
  rsi          numeric(6,3) NOT NULL,
  rsi_state    text NOT NULL,          -- 'overbought' | 'oversold' | 'neutral'
  macd         numeric(12,6) NOT NULL,
  macd_signal  numeric(12,6) NOT NULL,
  macd_hist    numeric(12,6) NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (symbol, tf, ts_utc),
  CONSTRAINT rsi_snapshots_state_check CHECK (rsi_state IN ('overbought', 'oversold', 'neutral'))
);

CREATE TABLE divergence_events (
  id                 bigserial PRIMARY KEY,
  symbol             text NOT NULL,
  tf                 text NOT NULL,
  kind               text NOT NULL,      -- 'price_rsi' | 'price_macd'
  direction          text NOT NULL,      -- 'bullish' | 'bearish'
  swing1_ts          timestamptz NOT NULL,
  swing2_ts           timestamptz NOT NULL,
  swing1_price       numeric NOT NULL,
  swing2_price       numeric NOT NULL,
  swing1_indicator   numeric NOT NULL,
  swing2_indicator   numeric NOT NULL,
  confirmed_ts       timestamptz NOT NULL,  -- bar at which swing2 became confirmed (swing_n bars after it)
  created_at         timestamptz NOT NULL DEFAULT now(),
  UNIQUE (symbol, tf, kind, swing1_ts, swing2_ts),
  CONSTRAINT divergence_events_kind_check CHECK (kind IN ('price_rsi', 'price_macd')),
  CONSTRAINT divergence_events_direction_check CHECK (direction IN ('bullish', 'bearish'))
);

CREATE INDEX ix_divergence_events_symbol_tf_kind ON divergence_events (symbol, tf, kind);
