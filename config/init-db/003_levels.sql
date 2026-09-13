-- SPEC.md section 4.2 (`levels`) — copied verbatim.
CREATE TABLE levels (
  id           bigserial PRIMARY KEY,
  symbol       text NOT NULL,
  tf_origin    text NOT NULL,
  kind         text NOT NULL,               -- 'support' | 'resistance'
  price_low    numeric(12,3) NOT NULL,      -- ناحیه، نه خط
  price_high   numeric(12,3) NOT NULL,
  created_ts   timestamptz NOT NULL,
  last_touch   timestamptz,
  touch_count  integer DEFAULT 0,
  break_count  integer DEFAULT 0,
  strength     numeric(8,4) DEFAULT 0,
  status       text DEFAULT 'active',       -- active|broken|flipped|expired
  atr_at_birth numeric(10,4) NOT NULL
);
