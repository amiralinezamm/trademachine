-- SPEC.md section 4.15 (`rules` — registry) — copied verbatim.
CREATE TABLE rules (
  id            text PRIMARY KEY,        -- 'R-012'
  statement     text NOT NULL,           -- بیان قانون به زبان ساده
  origin        text,                    -- 'جزوه' | 'تحقیق' | 'discovery'
  status        text NOT NULL,           -- proposed|testing|verified|rejected|retired
  evidence      text,                    -- منبع بیرونی یا شناسه بک‌تست
  params        jsonb,
  bt_trades     integer,
  bt_winrate    numeric(5,4),
  bt_expectancy numeric(10,4),
  weight        numeric(5,4),            -- وزن فعلی در سیستم؛ صفر یعنی غیرفعال
  created_at    timestamptz DEFAULT now(),
  updated_at    timestamptz
);
