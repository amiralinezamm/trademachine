-- SPEC.md section 4.1
CREATE TABLE IF NOT EXISTS candles (
  symbol      text        NOT NULL,
  tf          text        NOT NULL,
  ts_utc      timestamptz NOT NULL,
  open        numeric(12,3) NOT NULL,
  high        numeric(12,3) NOT NULL,
  low         numeric(12,3) NOT NULL,
  close       numeric(12,3) NOT NULL,
  tick_volume bigint,
  spread      integer,
  real_volume bigint,
  PRIMARY KEY (symbol, tf, ts_utc)
);
