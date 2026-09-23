# Running a Full Replay / Backtest

## Prerequisites

1. **SSH to the server** — the DB is not accessible locally:
   ```bash
   ssh xauusd-bot
   cd /opt/xauusd-bot
   ```

2. **Activated venv** (or use the full path below — no manual activation needed).

---

## Commands

### Full range (2023-09-15 → today)
```bash
nohup /opt/xauusd-bot/src/api/venv/bin/python -m src.backtest.replay \
    > /tmp/replay.log 2>&1 &
echo "PID: $!"
```

### Custom date range
```bash
nohup /opt/xauusd-bot/src/api/venv/bin/python -m src.backtest.replay \
    2025-01-01 2025-12-31 \
    > /tmp/replay.log 2>&1 &
```

### Dry run (no DB writes — stats only)
```bash
/opt/xauusd-bot/src/api/venv/bin/python -m src.backtest.replay \
    2025-01-01 2025-12-31 --dry-run
```

---

## What it does

1. Loads all M5 candles for the date range from the `candles` table.
2. Pre-fetches all module outputs once (levels_history, regime_snapshots,
   fibonacci_zones, pattern_hits, gaps, round_number_hits, correlations).
3. Iterates bar-by-bar, calling the **exact same functions** as the live engine
   (`check_level_reversion`, `build_context`).  
   Anti-lookahead: each bar only sees data with `ts_utc ≤ bar_ts`.
4. Entry = open of the **next bar** after the signal bar.
5. Outcome (TP / SL / level_invalidated / timeout) determined from subsequent
   candles.
6. Writes results to the `signals` table (tagged with `dry_run=False`).

---

## Duration

| Range | Candles | Approx. time |
|-------|---------|-------------|
| Full (2023-09-15 → today) | ~213,000 | **~6 hours** |
| 1 year | ~105,000 | ~3 hours |
| 3 months | ~26,000 | ~45 min |

Run with `nohup` and tail the log to monitor:
```bash
tail -f /tmp/replay.log
```

---

## Output

**Stdout** (printed at end, or captured by nohup):
```json
{
  "total": 212972,
  "signals": 19006,
  "tp": 9178,
  "sl": 7942,
  "level_invalidated": ...,
  "timeout": ...,
  "open": ...
}
```

**DB** (unless `--dry-run`): results written to the `signals` table. Each row has
`rule_version`, `outcome`, `pnl_usd`, `components` JSONB.

**Log progress** (stderr / nohup file): progress line every 1,000 bars with signal
count so far.

---

## Config files

| File | Purpose |
|------|---------|
| `config/costs.yaml` | Spread, slippage, swap, commission, SL/TP ATR multipliers |
| `config/params.yaml` | Signal rules: `swing_n`, `confirm_atr_mult`, `min_same_direction_spacing_usd`, etc. |

> **Exit criterion**: four outcomes — TP, SL, level_invalidated, safety cap.
> The safety cap is controlled by `max_safety_bars` in `costs.yaml` (default: 288 bars = 24 h).
> `timeout_bars` is **not** read by the code and can be ignored.

---

## Caution

- A full replay writes ~19,000 rows to `signals` — **clears any prior replay data**
  in the date range via ON CONFLICT (natural key: symbol, tf, ts_utc, direction, rule_version).
- Do **not** run replay while the live n8n pipeline is writing signals — use
  `--dry-run` first to validate, then run the real replay in a maintenance window.
- The process is single-threaded and CPU-bound; it will use 100% of one core for
  several hours. Monitor with `htop`; the API and n8n continue running normally.
