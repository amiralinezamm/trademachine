-- SPEC.md section 4.8 (`correlation`) -- dollar correlation, oil shock, pressure/discharge.
CREATE TABLE dollar_correlation (
  id           bigserial PRIMARY KEY,
  symbol       text NOT NULL,          -- always 'XAUUSD@'
  tf           text NOT NULL,
  ts_utc       timestamptz NOT NULL,
  correlation  numeric(6,4) NOT NULL,
  UNIQUE (symbol, tf, ts_utc)
);

CREATE TABLE oil_shock_events (
  id                          bigserial PRIMARY KEY,
  symbol                      text NOT NULL,       -- always 'XAUUSD@'
  tf                          text NOT NULL,
  ts_utc                      timestamptz NOT NULL,
  oil_return_3bar             numeric(12,5) NOT NULL,
  oil_atr                     numeric(12,5) NOT NULL,
  gold_return_3bar            numeric(12,5),
  gold_return_6bar_fwd        numeric(12,5),        -- NULL until enough forward data exists
  reversal                    boolean,               -- NULL until gold_return_6bar_fwd is known
  oil_gold_concurrent_agree   boolean,
  UNIQUE (symbol, tf, ts_utc)
);

CREATE TABLE pressure_snapshots (
  id           bigserial PRIMARY KEY,
  symbol       text NOT NULL,
  tf           text NOT NULL,
  ts_utc       timestamptz NOT NULL,
  residual     numeric(12,6),
  pressure_raw numeric(12,6),
  pressure_z   numeric(8,4),
  flagged      boolean NOT NULL DEFAULT false,
  UNIQUE (symbol, tf, ts_utc)
);

CREATE TABLE pressure_episodes (
  id                 bigserial PRIMARY KEY,
  symbol             text NOT NULL,
  tf                 text NOT NULL,
  flag_ts            timestamptz NOT NULL,
  pressure_sign      smallint NOT NULL,
  discharge_ts       timestamptz,
  bars_to_discharge  integer,
  return_12bar       numeric(12,5),
  return_24bar       numeric(12,5),
  UNIQUE (symbol, tf, flag_ts)
);
