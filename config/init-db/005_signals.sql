-- SPEC.md section 4.14 (`backtest` — three-view analysis) — copied verbatim.
CREATE TABLE signals (
  id          bigserial PRIMARY KEY,
  ts_utc      timestamptz NOT NULL,
  direction   text,                    -- 'buy' | 'sell'
  entry       numeric(12,3),
  stop_loss   numeric(12,3),
  take_profit numeric(12,3),
  confidence  numeric(5,4),
  components  jsonb NOT NULL,          -- ← کلید کل ماجرا
  rule_version text NOT NULL,
  outcome     text,                    -- 'tp'|'sl'|'timeout'|'open'
  pnl_usd     numeric(12,2)
);
