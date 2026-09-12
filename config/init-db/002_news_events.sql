-- SPEC.md section 4.7 (news). ts_utc is taken directly from the ForexFactory
-- feed's <date>/<time> fields with NO timezone conversion — that feed is
-- already UTC (confirmed empirically against known fixed US release times,
-- e.g. BLS CPI/PPI always at 8:30am ET / PPI 8:30am ET; the feed showed
-- these at 12:30pm which only lines up if the feed is already UTC).
-- This is specific to this feed; MT5 server-time-to-UTC conversion (CLAUDE.md
-- rule 4) is unaffected and still required in src/ingest.

CREATE TABLE IF NOT EXISTS news_events (
  id               bigserial   PRIMARY KEY,
  title            text        NOT NULL,       -- as given by the feed, e.g. 'Core CPI m/m'
  country          text        NOT NULL,       -- feed's "country" field is actually a currency code (USD, EUR, ...)
  ts_utc           timestamptz NOT NULL,        -- feed date+time, stored as-is (already UTC)
  impact           text        NOT NULL CHECK (impact IN ('High','Medium')),
  currency_weight  numeric(3,2) NOT NULL,       -- 1.00 USD, 0.50 EUR/GBP
  forecast         text,
  previous         text,
  actual           text,                        -- not provided by this feed; reserved for the delivery phase
  url              text,
  notified         boolean     NOT NULL DEFAULT false,
  fetched_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (title, country, ts_utc)
);

CREATE INDEX IF NOT EXISTS idx_news_events_ts_utc ON news_events (ts_utc);
