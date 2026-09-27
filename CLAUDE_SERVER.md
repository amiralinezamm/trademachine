# XAUUSD Bot — Server Context for Claude Code Sessions

## اتصال به سرور
```bash
ssh xauusd-bot                          # alias در ~/.ssh/config روی Mac
# پروژه در: /opt/xauusd-bot
# Python venv: /opt/xauusd-bot/src/api/venv/bin/python
# .env با credentials در: /opt/xauusd-bot/.env
```

## اتصال به دیتابیس (از داخل کد Python روی سرور)
```python
import sys
sys.path.insert(0, "/opt/xauusd-bot")
from src.features.levels_store import get_connection
conn = get_connection()   # credentials را از .env می‌خواند
```

## اجرای script روی سرور
```bash
ssh xauusd-bot "cd /opt/xauusd-bot && /opt/xauusd-bot/src/api/venv/bin/python /tmp/myscript.py"
```

## ساختار پروژه
```
/opt/xauusd-bot/
├── src/
│   ├── api/main.py              # FastAPI — endpoints: /signal/latest /levels/near-price /gaps/open /news/upcoming /health
│   ├── engine/
│   │   ├── level_reversion.py  # check_level_reversion() — منطق اصلی سیگنال
│   │   └── signal_store.py     # fetch_latest_closed_candle, fetch_active_levels
│   ├── backtest/replay.py      # full backtest replay
│   ├── features/
│   │   ├── levels.py           # compute_levels()
│   │   ├── levels_store.py     # get_connection(), fetch_active_levels()
│   │   └── levels_history.py   # point-in-time lookup
│   ├── telegram_bot/
│   │   ├── bot.py              # Telegram assistant bot (polling)
│   │   └── formatters.py       # message formatters (pure functions)
│   └── ingest/                 # candle ingestion
├── tests/
│   ├── engine/test_edge_trigger.py   # 18 تست — edge-trigger + off_hours filter
│   └── telegram_bot/test_formatters.py  # 15 تست — Telegram formatters
├── config/params.yaml           # پارامترها — confirm_atr_mult=0.5
├── SPEC.md                      # مشخصات سیستم
└── .env                         # credentials — هرگز در git
```

## جدول‌های DB اصلی
- `candles` — OHLCV با symbol, tf, ts_utc
- `levels` — سطوح حمایت/مقاومت با kind, price_low, price_high, strength, status
- `levels_history` — point-in-time history (161,840 row) برای جلوگیری از lookahead
- `signals` — سیگنال‌های صادرشده با outcome (tp/sl/level_invalidated/timeout), components JSONB
- `rules` — رجیستری قوانین با status (proposed/testing/rejected)
- `gaps` — گپ‌های قیمتی باز

## وضعیت rules registry (آخرین بروزرسانی 2026-09-20)
| id | status |
|---|---|
| level_reversion_v1 | testing — EV=+$1.03/trade، WR=48.3%، TP:SL=1.5:1 |
| off_hours_resistance_filter | testing — resistance ساعت 17-23 UTC حذف می‌شود |
| gap_near_filter | testing — WR=63.3% وقتی gap باز < 5 ATR هست (فقط rules، بدون کد) |
| double_touch_confidence_multiplier | proposed — هنوز کد اجرایی ندارد |
| pressure_reversal | rejected |

## سرویس‌های systemd
```bash
systemctl status xauusd-api        # FastAPI روی port 8000
systemctl status xauusd-tgbot      # Telegram assistant bot
```

## اجرای تست‌ها
```bash
ssh xauusd-bot "cd /opt/xauusd-bot && /opt/xauusd-bot/src/api/venv/bin/python -m pytest tests/ -v"
```

## محدودیت‌های مهم (HARD RULES)
1. تا وقتی کار ۱ و ۲ کامل نشده، سیگنال به کاربران واقعی ارسال نشود
2. N8N_ENCRYPTION_KEY و DB password فقط در .env سرور — هرگز در git
3. هر جا فیکس درخواست‌شده از قبل در کد هست، متوقف شو و بگو
4. Telegram token در .env سرور — هرگز در chat نوشته شود

## git log آخرین commitها
```bash
ssh xauusd-bot "cd /opt/xauusd-bot && git log --oneline -8"
```
```
a23856a feat(signal): add close to components; suppress off-hours resistance
be04443 feat(bot): add Telegram assistant bot with /status /levels /gaps /news
616688d fix(signal): add confirm_atr_mult margin to directional close condition
b28854e docs: register double_touch_confidence_multiplier as proposed rule
3536c00 test(signal): add 13 edge-trigger tests for check_level_reversion
57e007d feat(signal): replace proximity check with zone-touch edge-trigger
```

## نکات فنی کلیدی
- `entry = close` در سیگنال — قیمت ورود همان close کندل تاییدیه است
- `level_price_low`, `level_price_high`, `close`, `atr` همه در components JSONB هستند
- اتصال DB از طریق get_connection() که .env را از root پروژه می‌خواند
- replay کامل روی 212,972 کندل ≈ 6 ساعت طول می‌کشد
- levels_history rebuild ≈ 15 دقیقه (scripts/rebuild_levels_history.py)
