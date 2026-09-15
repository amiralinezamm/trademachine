-- levels (SPEC.md 4.2) has only a surrogate bigserial PK — no natural key
-- to upsert on. compute_levels() is pure and recomputes the full current
-- state from raw candles on every call (CLAUDE.md rule 6), so a level's
-- identity across re-runs must be (symbol, tf_origin, created_ts): stable
-- even across a role flip, where `kind` itself changes and can't be part
-- of the identity.
ALTER TABLE levels ADD CONSTRAINT levels_natural_key UNIQUE (symbol, tf_origin, created_ts);
