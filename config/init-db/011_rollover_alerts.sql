-- docs/instrument_rollover.md -- daily automated rollover-timing check.
-- One row per detected front-month change, for human review. Never
-- auto-deleted; `acknowledged` lets a human mark it reviewed.
CREATE TABLE rollover_alerts (
  id                    bigserial PRIMARY KEY,
  logical_symbol        text NOT NULL,
  old_physical_symbol   text,
  new_physical_symbol   text NOT NULL,
  adjustment_offset     numeric(12,5),
  offset_computed       boolean NOT NULL DEFAULT true,  -- false = old contract missing from the candidate snapshot, offset could not be computed reliably
  detected_at           timestamptz NOT NULL DEFAULT now(),
  acknowledged          boolean NOT NULL DEFAULT false
);
