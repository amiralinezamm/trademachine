-- docs/instrument_rollover.md — correlation module (SPEC.md 4.8) prerequisite.
-- Tracks which physical MT5 contract each logical instrument symbol
-- (DXY@ / T10Y@ / BRENT@) is mapped to over time, and the additive
-- back-adjustment offset applied to that segment's historical prices so a
-- contract switch never produces an artificial price jump in `candles`.
CREATE TABLE instrument_contracts (
  id                 bigserial PRIMARY KEY,
  logical_symbol     text NOT NULL,           -- 'DXY@' | 'T10Y@' | 'BRENT@'
  physical_symbol    text NOT NULL,           -- e.g. 'USINDX.Z26'
  valid_from_ts      timestamptz NOT NULL,    -- oldest candle ts stored under this contract
  valid_to_ts        timestamptz,             -- NULL = still the active/mapped contract
  adjustment_offset  numeric(12,5) NOT NULL DEFAULT 0,  -- additive offset applied to this segment's raw prices before writing to candles
  detected_at        timestamptz NOT NULL DEFAULT now(),
  UNIQUE (logical_symbol, physical_symbol, valid_from_ts)
);
