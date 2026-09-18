"""SPEC.md 4.14 — Backtest replay engine.

CRITICAL (dام #10): calls the EXACT same functions as the live signal path:
  check_level_reversion()  from src.engine.level_reversion
  build_context()           from src.engine.signal_context

This is NOT a reimplementation. It is the same code, called with historical
inputs that mirror what the live path receives for each M5 bar.

Anti-lookahead (dام #1):
  - Each candle bar uses only data that existed at that ts (created_ts filter
    on levels; ts_utc <= bar_ts filter on all module outputs).
  - Entry is at the OPEN of the NEXT bar, not the signal bar's close (dام #4).
  - Gaps use fill_ts to determine historical open/half-filled status instead
    of the current status column (which would look into the future).

Performance: pre-fetches all module data once, filters in Python per bar.
ATR is computed vectorially over the whole candle array (one TA-Lib call).

Usage:
    python -m src.backtest.replay                        # full date range
    python -m src.backtest.replay 2025-01-01 2025-12-31  # custom range

Configuration: config/costs.yaml — set timeout_bars before first run.
"""
from __future__ import annotations

import bisect
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import talib
import yaml

log = logging.getLogger(__name__)

COSTS_PATH = Path(__file__).resolve().parents[2] / "config" / "costs.yaml"
PARAMS_PATH = Path(__file__).resolve().parents[2] / "config" / "params.yaml"


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def load_costs() -> dict[str, Any]:
    with open(COSTS_PATH) as f:
        return yaml.safe_load(f)


def load_atr_period() -> int:
    with open(PARAMS_PATH) as f:
        return yaml.safe_load(f)["signal_rules"]["level_reversion"].get("atr_period", 14)


# ---------------------------------------------------------------------------
# Session detection (UTC hour → session name for fallback spread lookup)
# ---------------------------------------------------------------------------

def _session(ts_utc: datetime) -> str:
    h = ts_utc.hour
    if 21 <= h < 22:
        return "rollover"
    if 0 <= h < 8:
        return "tokyo"
    if 8 <= h < 13:
        return "london"
    return "newyork"


# ---------------------------------------------------------------------------
# Cost model
# ---------------------------------------------------------------------------

def spread_cost(candle_spread_int: int | None, ts_utc: datetime, costs: dict) -> float:
    """Return spread in USD/oz for this bar.

    Uses candle.spread (integer broker points) when point_size is set in
    config/costs.yaml; falls back to session table otherwise.
    """
    point_size = costs["spread"].get("point_size")
    if point_size is not None and candle_spread_int is not None:
        return candle_spread_int * point_size
    sess = _session(ts_utc)
    return costs["spread"]["fallback_by_session"].get(sess,
           costs["spread"]["fallback_by_session"]["newyork"])


def total_cost(candle_spread_int: int | None, ts_utc: datetime,
               in_news_window: bool, costs: dict) -> float:
    sp = spread_cost(candle_spread_int, ts_utc, costs)
    slip = (costs["slippage"]["news_window"] if in_news_window
            else costs["slippage"]["base"])
    return sp + slip


# ---------------------------------------------------------------------------
# DB — bulk pre-fetchers
# ---------------------------------------------------------------------------

def _fetch_all_candles(conn, symbol: str, tf: str,
                       from_ts: datetime, to_ts: datetime) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ts_utc, open, high, low, close, spread
            FROM candles
            WHERE symbol = %s AND tf = %s AND ts_utc BETWEEN %s AND %s
            ORDER BY ts_utc
            """,
            (symbol, tf, from_ts, to_ts),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _fetch_all_levels(conn, symbol: str, tf: str) -> list[dict]:
    """All levels (active+flipped) sorted by created_ts for bisect filtering."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, kind, price_low, price_high, strength, status, created_ts
            FROM levels
            WHERE symbol = %s AND tf_origin = %s AND status IN ('active', 'flipped')
            ORDER BY created_ts
            """,
            (symbol, tf),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _fetch_all_regime(conn, symbol: str, tf: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ts_utc, regime, adx, bb_width, bb_width_pct
            FROM regime_snapshots WHERE symbol=%s AND tf_origin=%s ORDER BY ts_utc
            """,
            (symbol, tf),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _fetch_all_round_hits(conn, symbol: str, tf: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ts_utc, level, multiplier, weight, state, direction
            FROM round_number_hits WHERE symbol=%s AND tf=%s ORDER BY ts_utc
            """,
            (symbol, tf),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _fetch_all_fib_zones(conn, symbol: str, tf: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT computed_at AS ts_utc, price, level_pct, role, overlapping
            FROM fibonacci_zones WHERE symbol=%s AND tf=%s ORDER BY computed_at
            """,
            (symbol, tf),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _fetch_all_patterns(conn, tf: str,
                        from_ts: datetime, to_ts: datetime) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ts_utc, pattern, direction, body_atr, at_level_id,
                   level_strength, regime
            FROM pattern_hits
            WHERE tf=%s AND ts_utc BETWEEN %s AND %s
            ORDER BY ts_utc
            """,
            (tf, from_ts, to_ts),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _fetch_all_gaps(conn, symbol: str, tf: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ts_utc, direction, gap_high, gap_low, weight,
                   status, fill_ts, half_fill_ts, fill_mode
            FROM gaps WHERE symbol=%s AND tf=%s ORDER BY ts_utc
            """,
            (symbol, tf),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _fetch_all_corr(conn, symbol: str, tf: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ts_utc, correlation FROM dollar_correlation
            WHERE symbol=%s AND tf=%s ORDER BY ts_utc
            """,
            (symbol, tf),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# In-memory filtering helpers (all O(log N) per bar via bisect)
# ---------------------------------------------------------------------------

def _ts_key(row: dict) -> datetime:
    return row["ts_utc"]


def _rows_up_to(sorted_rows: list[dict], as_of_ts: datetime) -> list[dict]:
    """All rows with ts_utc <= as_of_ts (sorted_rows must be sorted by ts_utc)."""
    keys = [r["ts_utc"] for r in sorted_rows]
    idx = bisect.bisect_right(keys, as_of_ts)
    return sorted_rows[:idx]


def _last_row_up_to(sorted_rows: list[dict], as_of_ts: datetime) -> dict | None:
    rows = _rows_up_to(sorted_rows, as_of_ts)
    return rows[-1] if rows else None


def _levels_at(all_levels: list[dict], as_of_ts: datetime) -> list[dict]:
    """Levels created at or before as_of_ts (anti-lookahead on created_ts)."""
    keys = [r["created_ts"] for r in all_levels]
    idx = bisect.bisect_right(keys, as_of_ts)
    return all_levels[:idx]


def _open_gaps_at(all_gaps: list[dict], as_of_ts: datetime) -> list[dict]:
    """Gaps that were OPEN or HALF_FILLED at as_of_ts (historical status)."""
    result = []
    for g in all_gaps:
        if g["ts_utc"] > as_of_ts:
            break
        fill_ts = g.get("fill_ts")
        if fill_ts is not None and fill_ts <= as_of_ts:
            continue  # was already fully filled
        result.append(g)
    return result


def _recent_patterns(all_pats: list[dict], as_of_ts: datetime,
                     n_bars: int, tf_minutes: int = 5) -> list[dict]:
    from datetime import timedelta
    window_start = as_of_ts - timedelta(minutes=tf_minutes * n_bars)
    lo = bisect.bisect_left([r["ts_utc"] for r in all_pats], window_start)
    hi = bisect.bisect_right([r["ts_utc"] for r in all_pats], as_of_ts)
    return all_pats[lo:hi]


# ---------------------------------------------------------------------------
# Build context from pre-fetched lists (calls the pure build_context)
# ---------------------------------------------------------------------------

def _make_context(
    all_regime: list[dict],
    all_rounds: list[dict],
    all_fib: list[dict],
    all_pats: list[dict],
    all_gaps: list[dict],
    all_corr: list[dict],
    close: float,
    atr: float,
    as_of_ts: datetime,
) -> dict:
    from src.engine.signal_context import build_context

    regime_snap = _last_row_up_to(all_regime, as_of_ts)
    regime_snaps = [regime_snap] if regime_snap else []

    round_hits = [r for r in _rows_up_to(all_rounds, as_of_ts)
                  if r["state"] in ("APPROACHING", "REVERSAL")]

    fib_zones = _rows_up_to(all_fib, as_of_ts)

    pattern_rows = _recent_patterns(all_pats, as_of_ts, n_bars=3)

    open_gaps = _open_gaps_at(all_gaps, as_of_ts)

    corr = _last_row_up_to(all_corr, as_of_ts)
    corr_rows = [corr] if corr else []

    return build_context(
        regime_snaps=regime_snaps,
        round_hits=round_hits,
        fib_zones=fib_zones,
        pattern_rows=pattern_rows,
        open_gaps=open_gaps,
        corr_rows=corr_rows,
        close_price=close,
        atr_value=atr,
        as_of_ts=as_of_ts,
    )


# ---------------------------------------------------------------------------
# Outcome determination
# ---------------------------------------------------------------------------

def _determine_outcome(
    candles: list[dict],
    entry_idx: int,
    entry_price: float,
    direction: str,
    sl: float,
    tp: float,
    timeout_bars: int,
) -> tuple[str, float | None]:
    """Scan forward from entry_idx+1 for SL/TP hit or timeout.

    Returns (outcome, exit_price) where outcome in ('tp','sl','timeout','open').
    'open' means we hit the end of the dataset before timeout.
    """
    sign = 1 if direction == "BUY" else -1
    limit = min(entry_idx + timeout_bars + 1, len(candles))
    for j in range(entry_idx + 1, limit):
        c = candles[j]
        h = float(c["high"])
        lo = float(c["low"])
        if direction == "BUY":
            if lo <= sl:
                return "sl", sl
            if h >= tp:
                return "tp", tp
        else:  # SELL
            if h >= sl:
                return "sl", sl
            if lo <= tp:
                return "tp", tp
    if limit == len(candles):
        return "open", float(candles[-1]["close"])
    return "timeout", float(candles[limit - 1]["close"])


def _pnl(direction: str, entry: float, exit_price: float, cost: float) -> float:
    sign = 1.0 if direction == "BUY" else -1.0
    return round((exit_price - entry) * sign - cost, 4)


# ---------------------------------------------------------------------------
# DB writes
# ---------------------------------------------------------------------------

def _upsert_signal(conn, signal: dict, sl: float, tp: float,
                   outcome: str, pnl: float) -> None:
    """Insert new or update existing signal row with SL/TP/outcome/pnl."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO signals
              (ts_utc, direction, entry, stop_loss, take_profit,
               confidence, components, rule_version, outcome, pnl_usd)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (ts_utc, rule_version) DO UPDATE
              SET stop_loss  = EXCLUDED.stop_loss,
                  take_profit = EXCLUDED.take_profit,
                  outcome     = EXCLUDED.outcome,
                  pnl_usd     = EXCLUDED.pnl_usd
            """,
            (
                signal["ts_utc"], signal["direction"], signal["entry"],
                sl, tp, signal.get("confidence"),
                json.dumps(signal["components"]),
                signal["rule_version"], outcome, pnl,
            ),
        )


# ---------------------------------------------------------------------------
# Main engine
# ---------------------------------------------------------------------------

def run_backtest(
    from_ts: datetime,
    to_ts: datetime,
    symbol: str = "XAUUSD@",
    tf: str = "M5",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run the replay backtest and write results to the signals table.

    dry_run=True prints stats without writing to DB.
    Returns summary dict.
    """
    from src.engine.level_reversion import check_level_reversion, load_rule_params
    from src.features.levels_store import get_connection

    costs = load_costs()
    bt_cfg = costs.get("backtest", {})
    sl_mult = float(bt_cfg.get("sl_atr_mult", 2.0))
    tp_mult = float(bt_cfg.get("tp_atr_mult", 3.0))
    timeout_bars = bt_cfg.get("timeout_bars")

    if timeout_bars is None:
        raise ValueError(
            "costs.yaml backtest.timeout_bars is null — set it before running. "
            "Ask the user: 12 bars (1 h), 24 bars (2 h), or 48 bars (4 h)?"
        )
    timeout_bars = int(timeout_bars)

    atr_period = load_atr_period()
    rule_params = load_rule_params()

    log.info("Connecting to DB…")
    conn = get_connection()
    try:
        log.info("Pre-fetching candles %s → %s…", from_ts.date(), to_ts.date())
        candles = _fetch_all_candles(conn, symbol, tf, from_ts, to_ts)
        if len(candles) < atr_period + 2:
            raise ValueError(f"Too few candles ({len(candles)}) for ATR({atr_period})")
        log.info("  %d candles loaded", len(candles))

        # Compute ATR array once (vectorised, same formula as fetch_atr_at)
        highs  = np.array([float(c["high"])  for c in candles])
        lows   = np.array([float(c["low"])   for c in candles])
        closes = np.array([float(c["close"]) for c in candles])
        atr_arr = talib.ATR(highs, lows, closes, timeperiod=atr_period)

        log.info("Pre-fetching module outputs…")
        all_levels = _fetch_all_levels(conn, symbol, tf)
        all_regime  = _fetch_all_regime(conn, symbol, tf)
        all_rounds  = _fetch_all_round_hits(conn, symbol, tf)
        all_fib     = _fetch_all_fib_zones(conn, symbol, tf)
        all_pats    = _fetch_all_patterns(conn, tf, from_ts, to_ts)
        all_gaps    = _fetch_all_gaps(conn, symbol, tf)
        all_corr    = _fetch_all_corr(conn, symbol, tf)
        log.info("  levels=%d regime=%d rounds=%d fib=%d pats=%d gaps=%d corr=%d",
                 len(all_levels), len(all_regime), len(all_rounds),
                 len(all_fib), len(all_pats), len(all_gaps), len(all_corr))

        # Sort gap list (already ordered by ts_utc from query, but ensure)
        all_gaps.sort(key=lambda r: r["ts_utc"])

        stats = {"total": 0, "signals": 0, "tp": 0, "sl": 0, "timeout": 0, "open": 0}
        BATCH = 200
        batch_signals = []

        for i in range(atr_period, len(candles) - 1):
            atr = atr_arr[i]
            if atr != atr or atr <= 0:  # NaN or zero
                continue
            if i % 10_000 == 0:
                log.info("  bar %d/%d  signals_so_far=%d",
                         i, len(candles), stats["signals"])

            c = candles[i]
            ts = c["ts_utc"]
            close = float(c["close"])
            stats["total"] += 1

            levels_now = _levels_at(all_levels, ts)
            signal = check_level_reversion(
                symbol=symbol, tf=tf, ts_utc=ts,
                close=close, atr=float(atr), levels=levels_now,
                params=rule_params,
            )
            if signal is None:
                continue

            ctx = _make_context(
                all_regime, all_rounds, all_fib, all_pats, all_gaps, all_corr,
                close, float(atr), ts,
            )
            signal["components"].update(ctx)

            # Entry at OPEN of the NEXT bar (dام #4)
            next_c = candles[i + 1]
            entry = float(next_c["open"])
            cost = total_cost(c.get("spread"), ts, in_news_window=False, costs=costs)

            direction = signal["direction"]
            if direction == "BUY":
                sl = entry - sl_mult * float(atr)
                tp = entry + tp_mult * float(atr)
            else:
                sl = entry + sl_mult * float(atr)
                tp = entry - tp_mult * float(atr)

            outcome, exit_price = _determine_outcome(
                candles, i + 1, entry, direction, sl, tp, timeout_bars
            )
            pnl = _pnl(direction, entry, exit_price, cost)

            signal["entry"] = entry
            stats["signals"] += 1
            stats[outcome] += 1

            batch_signals.append((signal, sl, tp, outcome, pnl))
            if not dry_run and len(batch_signals) >= BATCH:
                for args in batch_signals:
                    _upsert_signal(conn, *args)
                conn.commit()
                batch_signals = []

        # Flush remainder
        if not dry_run and batch_signals:
            for args in batch_signals:
                _upsert_signal(conn, *args)
            conn.commit()

        log.info("Done. signals=%d tp=%d sl=%d timeout=%d open=%d",
                 stats["signals"], stats["tp"], stats["sl"],
                 stats["timeout"], stats["open"])
        if stats["signals"] > 0:
            winrate = (stats["tp"]) / stats["signals"]
            log.info("Raw winrate (tp only): %.1f%%", winrate * 100)

        return stats
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    UTC = timezone.utc
    if len(sys.argv) == 3:
        from_ts = datetime.fromisoformat(sys.argv[1]).replace(tzinfo=UTC)
        to_ts   = datetime.fromisoformat(sys.argv[2]).replace(tzinfo=UTC)
    else:
        # Full range: 2023-09-15 to today (quality-verified range from phase 0)
        from_ts = datetime(2023, 9, 15, tzinfo=UTC)
        to_ts   = datetime.now(UTC)

    dry = "--dry-run" in sys.argv
    if dry:
        log.info("DRY RUN — no DB writes")
    result = run_backtest(from_ts, to_ts, dry_run=dry)
    print(json.dumps(result, indent=2))
