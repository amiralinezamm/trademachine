# Live Signal Pipeline — وضعیت فعلی

## آنچه الان کار می‌کند

| لایه | وضعیت | جزئیات |
|------|--------|---------|
| داده‌های تاریخی | ✅ | ۳ سال M5 در DB تا 2026-09-15 10:40 UTC |
| سطوح قیمتی | ✅ | ۲۲۷۰ سطح محاسبه‌شده (۱۵ flipped فعال) |
| Rule Engine | ✅ | `check_level_reversion()` — `src/engine/level_reversion.py` |
| API: `/signal/latest` | ✅ | روی `172.18.0.1:8000` — dedup با UNIQUE constraint |
| API: `/health` | ✅ | آخرین کندل + lag + آخرین سیگنال |
| n8n workflow | ✅ | هر ۵ دقیقه، active و published |
| n8n → Telegram | ✅ | credential تنظیم‌شده، پیام فارسی |
| SSH push (Windows→Ubuntu) | ✅ | `candlepush_ed25519` + `command=/opt/xauusd-bot/scripts/candlepush.sh` |
| Task Scheduler Windows | ✅ | `XAUUSD-LiveIngest`، هر ۵ دقیقه، SYSTEM |
| لاگ Windows | ✅ | `C:\xauusd-bot\ingest.log` |

## آنچه باقی‌مانده (یک مرحله دستی)

**بارگذاری EA در MT5:**

1. در MT5، منوی **Navigator** → **Expert Advisors** → `CandleExporter` را پیدا کن
2. آن را روی نمودار **XAUUSD@ M5** بکش
3. در تنظیمات EA، گزینه **Allow live trading** را فعال کن → OK
4. علامت چراغ سبز (اسمایلی) در گوشه بالا-راست نمودار تأیید می‌کند EA فعال است

EA هر ۳۰ ثانیه آخرین کندل‌های بسته‌شده را در این مسیر می‌نویسد:
```
C:\Users\Administrator\AppData\Roaming\MetaQuotes\Terminal\6F2BC4B36A0BAC4B951FC9ADDEF025F7\MQL5\Files\xauusd_candles.json
```
Task Scheduler هر ۵ دقیقه این فایل را می‌خواند و به Ubuntu می‌فرستد.

## بررسی سلامت pipeline

**بلافاصله بعد از بارگذاری EA:**
```bash
# روی Ubuntu — آیا کندل تازه رسید؟
curl -s http://172.18.0.1:8000/health
# باید candle_lag_minutes < 10 باشد
```

**اگر کندل نرسید:**
```bash
# روی Windows: لاگ آخرین push را بخوان
type C:\xauusd-bot\ingest.log
```

**بررسی n8n:**
```
http://localhost:5678 → Executions → XAUUSD Signal -> Telegram
```

## معماری جریان داده

```
MT5 (Session 2, Administrator)
  ↓  EA هر 30s
C:\...\MQL5\Files\xauusd_candles.json
  ↓  Task Scheduler هر 5m (SYSTEM)
  ↓  C:\xauusd-bot\run_ingest.bat
  ↓  python mt5_live_ingest.py
  ↓  ssh candlepush@ubuntu (restricted key)
  ↓  /opt/xauusd-bot/scripts/candlepush.sh
  ↓  curl POST http://172.18.0.1:8000/ingest/candles
  ↓  PostgreSQL (candles table)
  ↓  n8n هر 5m
  ↓  GET /signal/latest
  ↓  check_level_reversion() — price within 0.3×ATR of active/flipped level
  ↓  سیگنال؟ → INSERT signals → Telegram پیام
```

## ایمنی anti-repainting (CLAUDE.md قوانین ۱ و ۲)

- همه query ها از `as_of_ts = candle["ts_utc"]` استفاده می‌کنند
- `fetch_active_levels(conn, symbol, tf, as_of_ts)` → `created_ts <= as_of_ts`
- همان `check_level_reversion()` برای live API و backtest آینده استفاده می‌شود (CLAUDE.md قانون ۶)

## فایل‌های کلیدی

| فایل | توضیح |
|------|-------|
| `src/engine/level_reversion.py` | pure signal rule |
| `src/engine/signal_store.py` | DB read/write برای signal |
| `src/api/main.py` | FastAPI — `/signal/latest`, `/health` |
| `scripts/candlepush.sh` | SSH command= wrapper روی Ubuntu |
| `windows/mt5_live_ingest.py` | Python روی Windows Task Scheduler |
| `windows/CandleExporter.mq5` | MT5 EA — کندل‌ها را به JSON می‌نویسد |
| `n8n/xauusd_signal_to_telegram.json` | n8n workflow JSON |
| `config/init-db/008_signals_dedup.sql` | UNIQUE constraint برای dedup |
