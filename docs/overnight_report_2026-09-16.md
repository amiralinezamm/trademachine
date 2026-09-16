# گزارش کار شبانه — ۱۶ شهریور ۱۴۰۵ / 2026-09-16

**شروع:** بعد از تأیید candle_lag_minutes < 10 (فید زنده سالم)
**پایان:** commit `3e9e3ec`

---

## خلاصه: چه چیزی تمام شد

### ✅ کار ۱ — تکمیل چرخه عمر `levels`

بررسی کد موجود نشان داد که هر سه مورد (merge، role flip، expiry) قبلاً در `src/features/levels.py` پیاده شده بودند. آنچه واقعاً کم بود دو **تست** بود:

| تست اضافه‌شده | هدف |
|---|---|
| `test_broken_resistance_flips_to_support` | تأیید که resistance شکسته‌شده به support با `kind=support`, `status∈{flipped,expired}`, `break_count=1` تبدیل می‌شود |
| `test_flipped_level_strength_is_halved` | تأیید که strength بعد از flip کمتر از قبل است (طبق SPEC.md: نصف می‌شود) |

**نکته فنی (برای اطلاع):** fixture اول با ATR مصنوعی کوچک شکست — سطح بعد از flip بلافاصله `expired` می‌شد چون `distance > 8*ATR` بود. علت: ATR بسیار کوچک‌تر از فاصله قیمت پس از شکست بود. راه‌حل: fixture واقع‌گرایانه با ATR ≈ 15 دلار (شبیه XAUUSD واقعی) و zone نزدیک به قیمت جاری.

### ✅ کار ۲ — چارت پذیرش چشمی

فایل: `docs/acceptance_chart_2026-09-16.png`

- ۳۰ روز داده M5 (resampled به H1 برای خوانایی) = ۴۹۶ بار H1
- پنجره تشخیص سطح: ۱۸۰ روز آخر (≈ ۳۴,۷۵۳ کندل M5)
- نتیجه: **۱۷ سطح active/flipped** (۸ support + ۹ resistance) در بازه قیمتی چارت
- **⚠️ لطفاً صبح چشمی بررسی کنید** — معیار پذیرش SPEC.md 4.2 نیاز به تأیید انسانی دارد

### ✅ کار ۳ — ماژول `patterns` (SPEC.md 4.3)

| فایل | محتوا |
|---|---|
| `src/features/patterns.py` | تابع خالص `compute_patterns()` — همه ۶۱ CDL* از TA-Lib، `as_of_ts`، محاسبه `body_atr`، linkage با `at_level_id` |
| `src/features/patterns_store.py` | لایه DB: fetch candles، fetch active levels، `upsert_pattern_hits` با ON CONFLICT DO NOTHING |
| `config/params.yaml` | بخش `[patterns]` اضافه شد: `atr_period=14`, `at_level_distance_atr_mult=0.5` |
| `tests/features/test_patterns.py` | ۸ تست از جمله subset anti-lookahead |

---

## تست‌ها

```
44 passed in 1.87s
```

| ماژول | تست قبل | تست بعد |
|---|---|---|
| `test_levels.py` | 7 | 9 (+2 role flip) |
| `test_patterns.py` | 0 | 8 (جدید) |
| بقیه | 27 | 27 (بدون تغییر) |

---

## فرض‌های شبانه (جایگزین پرسیدن از کاربر)

> **فرض شبانه ۱:** پنجره ۱۸۰ روزه برای محاسبه سطح در چارت پذیرش.  
> دلیل: محاسبه روی ۲۱۲k کندل (۳ سال) بیش از ۲ دقیقه طول کشید. ۱۸۰ روز هم همان سطوح مرتبط را می‌دهد و ۵ ثانیه طول می‌کشد.

> **فرض شبانه ۲:** پارامتر `at_level_distance_atr_mult = 0.5` برای linkage patterns به levels.  
> دلیل: SPEC.md این عدد را نمی‌دهد. ۰.۵ × ATR معادل نصف عرض zone است که معقول‌ترین حد برای "روی سطح بودن" یک pattern است. باید با backtest تأیید یا اصلاح شود.

> **فرض شبانه ۳:** فیلد `regime` در pattern_hits برابر NULL ذخیره می‌شود.  
> دلیل: ماژول `regime` (SPEC.md 4.9) هنوز ساخته نشده. NULL placeholder است.

---

## مسیر چارت پذیرش

```
/opt/xauusd-bot/docs/acceptance_chart_2026-09-16.png
```

(فایل همچنین در همین جلسه برای شما ارسال شد)

---

## کامیت انجام‌شده

```
3e9e3ec feat(levels+patterns): complete levels lifecycle tests + add patterns module
```

---

## پیشنهاد قدم بعدی

۱. **چشمی چارت را تأیید کنید** (`docs/acceptance_chart_2026-09-16.png`) — معیار پذیرش SPEC.md 4.2 همین است.

۲. **اجرای backfill patterns**: پر کردن جدول `pattern_hits` از تاریخچه موجود:
   ```bash
   source /opt/xauusd-bot/src/api/venv/bin/activate
   python -c "
   from datetime import datetime, timezone
   from src.features.levels_store import get_connection, fetch_candles
   from src.features.levels import compute_levels, load_levels_params
   from src.features.patterns import compute_patterns
   from src.features.patterns_store import fetch_candles_for_patterns, fetch_active_levels, upsert_pattern_hits
   # ... (باید اسکریپت کامل نوشته شود)
   "
   ```
   این را می‌توانم در جلسه بعدی با یک اسکریپت کامل در `scripts/backfill_patterns.py` انجام دهم.

۳. **ماژول بعدی طبق CLAUDE.md**: `round_numbers` یا `gaps` (هر دو بعد از patterns هستند). اگر می‌خواهید ادامه دهیم به همین ترتیب CLAUDE.md بروید.

---

## وضعیت فید زنده

آخرین بررسی قبل از شروع کار شبانه:
```json
{"candle_lag_minutes": 9.8, "last_candle_at": "2026-09-15T23:05:00+00:00"}
```
فید سالم بود. کار شبانه به n8n، فایروال، SSH یا هیچ تنظیم سروری دست نزد.
