-- Research table (2026-09-29, SPEC.md D24): EVERY level_reversion candidate a
-- backtest run sees -- not just the ones that fired -- with its full vote
-- vector and an evaluated outcome. Written only by
-- src/backtest/replay.run_backtest(record_candidates=<tag>); never read by
-- the live signal path.
--
-- Why: the `signals` table only holds candidates that survived every gate,
-- so any "which voter helps" analysis done on it is selection-biased, and
-- candidates rejected for having no valid level-based TP/SL were never
-- evaluated at all. Here each candidate carries:
--   gate      -- first gate that rejected it: blackout | votes_rejected |
--                no_valid_sl_tp | fired
--   exit_mode -- 'level'          : the real level-to-level SL/TP (exit_rules)
--                'shadow_min_rr'  : no level B qualified; same level-A stop,
--                                   synthetic TP at exactly min_rr x risk
--                                   (research only, never sent live)
--   u_*       -- EVERY candidate re-evaluated with one uniform exit (same
--                level-A stop, TP at exactly min_rr x risk). Level exits have
--                different reward:risk per trade, so comparing voters by
--                win rate across mixed exits would be misleading -- voter
--                analysis uses u_* only; strategy P&L uses the real exit.
--   risk_usd  -- |entry - stop|, so P&L can be read in R multiples.

CREATE TABLE IF NOT EXISTS bt_candidates (
  run_tag          text        NOT NULL,
  ts_utc           timestamptz NOT NULL,
  symbol           text        NOT NULL,
  tf               text        NOT NULL,
  direction        text        NOT NULL,
  entry            numeric(12,3) NOT NULL,
  atr              numeric(10,4),
  level_id         bigint,
  level_strength   numeric(8,4),
  strength_median  numeric(8,4),
  structure        text,
  regime           text,
  votes            jsonb       NOT NULL,
  net_votes        numeric(8,3) NOT NULL,
  gate             text        NOT NULL,
  exit_mode        text,
  stop_loss        numeric(12,3),
  take_profit      numeric(12,3),
  rr               numeric(8,3),
  outcome          text,
  exit_price       numeric(12,3),
  pnl_usd          numeric(12,4),
  risk_usd         numeric(12,4),
  u_outcome        text,
  u_pnl_usd        numeric(12,4),
  created_at       timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (run_tag, ts_utc)
);

CREATE INDEX IF NOT EXISTS idx_bt_candidates_run_gate ON bt_candidates (run_tag, gate);
