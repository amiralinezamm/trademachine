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
from datetime import datetime, timedelta, timezone
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


def load_break_atr_mult() -> float:
    with open(PARAMS_PATH) as f:
        return float(yaml.safe_load(f)["levels"]["break_atr_mult"])


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


def _fetch_all_levels(conn, symbol: str, tf: str,
                      max_ts=None) -> list[dict]:
    """All levels sorted by created_ts for bisect filtering (no status filter — historical levels were active when created)."""
    with conn.cursor() as cur:
        if max_ts is not None:
            cur.execute(
                """
                SELECT id, kind, price_low, price_high, strength, status, created_ts
                FROM levels
                WHERE symbol = %s AND tf_origin = %s AND created_ts <= %s
                ORDER BY created_ts
                """,
                (symbol, tf, max_ts),
            )
        else:
            cur.execute(
                """
                SELECT id, kind, price_low, price_high, strength, status, created_ts
                FROM levels
                WHERE symbol = %s AND tf_origin = %s
                ORDER BY created_ts
                """,
                (symbol, tf),
            )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]



def _fetch_all_levels_history(conn, symbol: str, tf: str,
                               min_ts=None, max_ts=None) -> dict[int, list[dict]]:
    """Fetch all levels_history rows and group by level_id.

    Returns {level_id: [sorted events by ts_utc]} for point-in-time lookup.
    Each event dict has: ts_utc, strength, status, touch_count, break_count.
    """
    with conn.cursor() as cur:
        if min_ts is not None and max_ts is not None:
            cur.execute(
                """
                SELECT lh.level_id, lh.ts_utc, lh.strength, lh.status,
                       lh.touch_count, lh.break_count
                FROM levels_history lh
                JOIN levels l ON l.id = lh.level_id
                WHERE l.symbol = %s AND l.tf_origin = %s
                  AND lh.ts_utc >= %s AND lh.ts_utc <= %s
                ORDER BY lh.level_id, lh.ts_utc
                """,
                (symbol, tf, min_ts, max_ts),
            )
        else:
            cur.execute(
                """
                SELECT lh.level_id, lh.ts_utc, lh.strength, lh.status,
                       lh.touch_count, lh.break_count
                FROM levels_history lh
                JOIN levels l ON l.id = lh.level_id
                WHERE l.symbol = %s AND l.tf_origin = %s
                ORDER BY lh.level_id, lh.ts_utc
                """,
                (symbol, tf),
            )
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    grouped: dict[int, list[dict]] = {}
    for r in rows:
        grouped.setdefault(r["level_id"], []).append(r)
    return grouped


def _fetch_all_regime(conn, symbol: str, tf: str,
                      min_ts=None, max_ts=None) -> list[dict]:
    with conn.cursor() as cur:
        if min_ts is not None and max_ts is not None:
            cur.execute(
                """
                SELECT ts_utc, regime, adx, bb_width, bb_width_pct
                FROM regime_snapshots
                WHERE symbol=%s AND tf_origin=%s AND ts_utc >= %s AND ts_utc <= %s
                ORDER BY ts_utc
                """,
                (symbol, tf, min_ts, max_ts),
            )
        else:
            cur.execute(
                """
                SELECT ts_utc, regime, adx, bb_width, bb_width_pct
                FROM regime_snapshots WHERE symbol=%s AND tf_origin=%s ORDER BY ts_utc
                """,
                (symbol, tf),
            )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _fetch_all_round_hits(conn, symbol: str, tf: str,
                          min_ts=None, max_ts=None) -> list[dict]:
    with conn.cursor() as cur:
        if min_ts is not None and max_ts is not None:
            cur.execute(
                """
                SELECT ts_utc, level, multiplier, weight, state, direction
                FROM round_number_hits
                WHERE symbol=%s AND tf=%s AND ts_utc >= %s AND ts_utc <= %s
                ORDER BY ts_utc
                """,
                (symbol, tf, min_ts, max_ts),
            )
        else:
            cur.execute(
                """
                SELECT ts_utc, level, multiplier, weight, state, direction
                FROM round_number_hits WHERE symbol=%s AND tf=%s ORDER BY ts_utc
                """,
                (symbol, tf),
            )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _fetch_all_fib_zones(conn, symbol: str, tf: str,
                         max_ts=None) -> list[dict]:
    with conn.cursor() as cur:
        if max_ts is not None:
            cur.execute(
                """
                SELECT computed_at AS ts_utc, price, level_pct, role, overlapping
                FROM fibonacci_zones
                WHERE symbol=%s AND tf_origin=%s AND computed_at <= %s
                ORDER BY computed_at
                """,
                (symbol, tf, max_ts),
            )
        else:
            cur.execute(
                """
                SELECT computed_at AS ts_utc, price, level_pct, role, overlapping
                FROM fibonacci_zones WHERE symbol=%s AND tf_origin=%s ORDER BY computed_at
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


def _fetch_all_gaps(conn, symbol: str, tf: str,
                    min_ts=None, max_ts=None) -> list[dict]:
    with conn.cursor() as cur:
        if min_ts is not None and max_ts is not None:
            cur.execute(
                """
                SELECT ts_utc, direction, gap_high, gap_low, weight,
                       status, fill_ts, half_fill_ts, fill_mode
                FROM gaps
                WHERE symbol=%s AND tf=%s AND ts_utc >= %s AND ts_utc <= %s
                ORDER BY ts_utc
                """,
                (symbol, tf, min_ts, max_ts),
            )
        else:
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


def _fetch_all_corr(conn, symbol: str, tf: str,
                    min_ts=None, max_ts=None) -> list[dict]:
    with conn.cursor() as cur:
        if min_ts is not None and max_ts is not None:
            cur.execute(
                """
                SELECT ts_utc, correlation FROM dollar_correlation
                WHERE symbol=%s AND tf=%s AND ts_utc >= %s AND ts_utc <= %s
                ORDER BY ts_utc
                """,
                (symbol, tf, min_ts, max_ts),
            )
        else:
            cur.execute(
                """
                SELECT ts_utc, correlation FROM dollar_correlation
                WHERE symbol=%s AND tf=%s ORDER BY ts_utc
                """,
                (symbol, tf),
            )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _fetch_all_matrix(conn, symbol: str,
                      min_ts=None, max_ts=None) -> list[dict]:
    """D20 -- matrix_snapshots (SPEC.md 4.10). Not tf-scoped (one row per
    symbol per M5 pivot bar)."""
    with conn.cursor() as cur:
        if min_ts is not None and max_ts is not None:
            cur.execute(
                """
                SELECT ts_utc, score, direction FROM matrix_snapshots
                WHERE symbol=%s AND ts_utc >= %s AND ts_utc <= %s
                ORDER BY ts_utc
                """,
                (symbol, min_ts, max_ts),
            )
        else:
            cur.execute(
                "SELECT ts_utc, score, direction FROM matrix_snapshots WHERE symbol=%s ORDER BY ts_utc",
                (symbol,),
            )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _fetch_all_rsi_snapshots(conn, symbol: str, tf: str,
                             min_ts=None, max_ts=None) -> list[dict]:
    """D20 -- rsi_snapshots (SPEC.md 4.19)."""
    with conn.cursor() as cur:
        if min_ts is not None and max_ts is not None:
            cur.execute(
                """
                SELECT ts_utc, rsi, rsi_state, macd, macd_signal
                FROM rsi_snapshots
                WHERE symbol=%s AND tf=%s AND ts_utc >= %s AND ts_utc <= %s
                ORDER BY ts_utc
                """,
                (symbol, tf, min_ts, max_ts),
            )
        else:
            cur.execute(
                """
                SELECT ts_utc, rsi, rsi_state, macd, macd_signal
                FROM rsi_snapshots WHERE symbol=%s AND tf=%s ORDER BY ts_utc
                """,
                (symbol, tf),
            )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _fetch_all_divergence_events(conn, symbol: str, tf: str,
                                 min_ts=None, max_ts=None) -> list[dict]:
    """D20 -- divergence_events (SPEC.md 4.19), both kinds together --
    build_context()'s _most_recent() separates by 'kind'."""
    with conn.cursor() as cur:
        if min_ts is not None and max_ts is not None:
            cur.execute(
                """
                SELECT confirmed_ts, kind, direction
                FROM divergence_events
                WHERE symbol=%s AND tf=%s
                  AND confirmed_ts >= %s AND confirmed_ts <= %s
                ORDER BY confirmed_ts
                """,
                (symbol, tf, min_ts, max_ts),
            )
        else:
            cur.execute(
                """
                SELECT confirmed_ts, kind, direction
                FROM divergence_events WHERE symbol=%s AND tf=%s ORDER BY confirmed_ts
                """,
                (symbol, tf),
            )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# In-memory filtering helpers (all O(log N) per bar via bisect)
# ---------------------------------------------------------------------------

class _PreIndexed(list):
    """list subclass that pre-builds timestamp keys once for O(1) per-bar bisect.

    Eliminates O(N) key-list construction on every _rows_up_to() call.
    Compatible with all list operations; downstream code sees a plain list.
    """
    def __init__(self, rows: list[dict], key_field: str = "ts_utc") -> None:
        super().__init__(rows)
        self._ts_keys = [r[key_field] for r in rows]


def _ts_key(row: dict) -> datetime:
    return row["ts_utc"]


def _rows_up_to(sorted_rows, as_of_ts: datetime) -> list[dict]:
    """All rows with ts_utc <= as_of_ts (sorted_rows must be sorted by ts_utc)."""
    keys = getattr(sorted_rows, "_ts_keys", None)
    if keys is None:
        keys = [r["ts_utc"] for r in sorted_rows]
    idx = bisect.bisect_right(keys, as_of_ts)
    return sorted_rows[:idx]


def _last_row_up_to(sorted_rows, as_of_ts: datetime) -> dict | None:
    keys = getattr(sorted_rows, "_ts_keys", None)
    if keys is None:
        keys = [r[ts_utc] for r in sorted_rows]
    idx = bisect.bisect_right(keys, as_of_ts)
    return sorted_rows[idx - 1] if idx > 0 else None


def _levels_at(all_levels, as_of_ts: datetime) -> list[dict]:
    """Levels created at or before as_of_ts (anti-lookahead on created_ts)."""
    keys = getattr(all_levels, "_ts_keys", None)
    if keys is None:
        keys = [r["created_ts"] for r in all_levels]
    idx = bisect.bisect_right(keys, as_of_ts)
    return all_levels[:idx]



def _level_state_at(
    history_by_id: dict[int, list[dict]],
    level_id: int,
    as_of_ts,
) -> tuple[float, str, int, int]:
    """Point-in-time lookup: (strength, status, touch_count, break_count) for
    a level at as_of_ts, using the most recent levels_history entry.

    If no history entry exists yet (level was just created, no events before
    as_of_ts), returns (0.0, "active", 0, 0) — the level's initial state.

    Note: strength is stored at the moment of each touch/break event.
    Between events, the true strength is lower (exponential decay since last
    touch), but the stored value is used as an upper-bound approximation.
    This is a conscious trade-off: better than strength=0 for all historical
    levels, and the median-relative comparison in check_level_reversion
    remains approximately correct since all levels experience the same decay.
    """
    entries = history_by_id.get(level_id)
    if not entries:
        return 0.0, "active", 0, 0
    ts_list = getattr(entries, "_ts_keys", None)
    if ts_list is None:
        ts_list = [e["ts_utc"] for e in entries]
    idx = bisect.bisect_right(ts_list, as_of_ts) - 1
    if idx < 0:
        return 0.0, "active", 0, 0
    e = entries[idx]
    return float(e["strength"]), str(e["status"]), int(e["touch_count"]), int(e["break_count"])


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
    all_matrix: list[dict] | None = None,
    all_rsi: list[dict] | None = None,
    all_divergence: list[dict] | None = None,
    divergence_recency_bars: int = 12,
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

    # D20 -- matrix/rsi/divergence rows are already the full pre-fetched
    # history (like all the lists above); build_context() re-applies the
    # as_of_ts / recency-window cutoff itself (single source of truth,
    # shared with the live path -- CLAUDE.md rule 6).
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
        matrix_snaps=all_matrix or [],
        rsi_snaps=all_rsi or [],
        divergence_rows=all_divergence or [],
        divergence_recency_bars=divergence_recency_bars,
    )


# ---------------------------------------------------------------------------
# Outcome determination
# ---------------------------------------------------------------------------

def _determine_outcome(
    candles: list[dict],
    atr_arr,
    entry_idx: int,
    entry_price: float,
    direction: str,
    sl: float,
    tp: float,
    max_safety_bars: int,
    level_lo: float,
    level_hi: float,
    break_mult: float,
) -> tuple[str, float]:
    """Scan forward from entry_idx+1 for the first exit condition.

    Exit priority (checked in this order each bar):
      1. SL hit   → 'sl'
      2. TP hit   → 'tp'
      3. Level invalidated (same break condition as levels.py walk-forward) → 'level_invalidated'
      4. max_safety_bars reached (safety cap, not the primary criterion)    → 'timeout'
      5. End of dataset before safety cap                                   → 'open'

    Break condition (from levels.py lines 157-164):
      BUY  (from support):    close < level_lo - break_mult * atr  → support broken downward
      SELL (from resistance): close > level_hi + break_mult * atr  → resistance broken upward
    """
    limit = min(entry_idx + max_safety_bars + 1, len(candles))
    for j in range(entry_idx + 1, limit):
        c = candles[j]
        h = float(c["high"])
        lo = float(c["low"])
        close_j = float(c["close"])
        atr_j_raw = atr_arr[j]
        atr_j = float(atr_j_raw) if atr_j_raw == atr_j_raw else None  # NaN guard

        if direction == "BUY":
            if lo <= sl:
                return "sl", sl
            if h >= tp:
                return "tp", tp
            if atr_j and close_j < level_lo - break_mult * atr_j:
                return "level_invalidated", close_j
        else:  # SELL
            if h >= sl:
                return "sl", sl
            if lo <= tp:
                return "tp", tp
            if atr_j and close_j > level_hi + break_mult * atr_j:
                return "level_invalidated", close_j

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
              SET direction   = EXCLUDED.direction,
                  entry       = EXCLUDED.entry,
                  stop_loss   = EXCLUDED.stop_loss,
                  take_profit = EXCLUDED.take_profit,
                  confidence  = EXCLUDED.confidence,
                  components  = EXCLUDED.components,
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
    voter_filter: list[str] | None = None,
    allow_proposed: bool | None = None,
    min_net_votes_override: float | None = None,
    holdout_mode: bool = False,
) -> dict[str, Any]:
    """Run the replay backtest and write results to the signals table.

    dry_run=True prints stats without writing to DB.

    voter_filter (D20, 2026-09-25 -- per-module isolation for the quick
    proposed-rule look, step 4/5): when set, ONLY the named module_voting
    voters (VOTE_FUNCTIONS or PROPOSED_VOTE_FUNCTIONS keys) contribute --
    every other voter's weight is forced to 0 for this run. None (default)
    runs the normal full aggregate, unchanged from before.

    allow_proposed: None (default) reads params.yaml's own
    rules_registry.allow_proposed_in_voting (mirrors whatever the live
    service is currently configured with); explicit True/False overrides
    it for this run only -- a backtest is analysis, not the live signal
    path this flag was written to gate, so overriding it here carries none
    of the live-risk consideration that flag exists for.

    min_net_votes_override: None (default) uses the configured
    min_net_votes UNLESS voter_filter narrows to exactly one voter, in
    which case it's auto-set to 1 -- with only one -1/0/+1 vote
    contributing, the default threshold of 2 could never pass and the
    "isolated" run would trivially produce zero signals. Pass an explicit
    value to override this convenience.

    Returns summary dict.

    holdout_mode: when True, skips the HOLDOUT boundary check --
    only run_holdout_validation() should pass True here.
    """
    from src.engine.level_reversion import apply_spacing_filter, check_level_reversion, load_rule_params
    from src.engine.market_structure import (
        _resample_to_h1, detect_h1_swings, load_structure_params, structure_from_swings,
    )
    from src.engine.module_voting import (
        compute_votes, load_voting_params, load_rules_registry_params,
        VOTE_FUNCTIONS, PROPOSED_VOTE_FUNCTIONS,
    )
    from src.features.levels_store import get_connection

    if not holdout_mode:
        from src.backtest.splits import HoldoutViolation, get_holdout_start
        holdout_start = get_holdout_start(symbol=symbol, tf=tf)
        if to_ts > holdout_start:
            raise HoldoutViolation(
                f"to_ts {to_ts} > HOLDOUT boundary {holdout_start}. "
                "Pass holdout_mode=True only via run_holdout_validation()."
            )

    costs = load_costs()
    bt_cfg = costs.get("backtest", {})
    sl_mult = float(bt_cfg.get("sl_atr_mult", 2.0))
    tp_mult = float(bt_cfg.get("tp_atr_mult", 3.0))
    max_safety_bars = int(bt_cfg.get("max_safety_bars", 288))

    atr_period = load_atr_period()
    break_mult = load_break_atr_mult()
    rule_params = load_rule_params()
    _min_spacing_usd = float(rule_params.get("min_same_direction_spacing_usd", 10.0))
    _last_by_dir: dict[str, dict] = {}  # {direction: {entry, outcome}} anti-lookahead

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

        log.info("Pre-computing H1 market structure (market_structure_filter)…")
        _structure_params = load_structure_params()
        _h1_candles = _resample_to_h1(candles)
        _h1_swings = detect_h1_swings(_h1_candles, _structure_params)
        log.info("  %d H1 bars, %d confirmed swings", len(_h1_candles), len(_h1_swings))

        log.info("Pre-fetching module outputs…")
        _fetch_min = from_ts - timedelta(days=30)
        # Pre-convert Decimal columns to float once to avoid 19M float() calls per fold
        _raw_levels = _fetch_all_levels(conn, symbol, tf, max_ts=to_ts)
        for _lv in _raw_levels:
            _lv["price_low"]  = float(_lv["price_low"])
            _lv["price_high"] = float(_lv["price_high"])
            _lv["strength"]   = float(_lv["strength"])
        all_levels = _PreIndexed(_raw_levels, "created_ts")
        _raw_hist  = _fetch_all_levels_history(conn, symbol, tf, min_ts=_fetch_min, max_ts=to_ts)
        history_by_id = {lid: _PreIndexed(rows) for lid, rows in _raw_hist.items()}
        all_regime  = _PreIndexed(_fetch_all_regime(conn, symbol, tf, min_ts=_fetch_min, max_ts=to_ts))
        all_rounds  = _PreIndexed(_fetch_all_round_hits(conn, symbol, tf, min_ts=_fetch_min, max_ts=to_ts))
        all_fib     = _PreIndexed(_fetch_all_fib_zones(conn, symbol, tf, max_ts=to_ts))
        all_pats    = _PreIndexed(_fetch_all_patterns(conn, tf, from_ts, to_ts))
        all_gaps    = _PreIndexed(_fetch_all_gaps(conn, symbol, tf, min_ts=_fetch_min, max_ts=to_ts))
        all_corr    = _PreIndexed(_fetch_all_corr(conn, symbol, tf, min_ts=_fetch_min, max_ts=to_ts))
        all_matrix  = _PreIndexed(_fetch_all_matrix(conn, symbol, min_ts=_fetch_min, max_ts=to_ts))
        all_rsi     = _PreIndexed(_fetch_all_rsi_snapshots(conn, symbol, tf, min_ts=_fetch_min, max_ts=to_ts))
        all_div     = _PreIndexed(_fetch_all_divergence_events(conn, symbol, tf, min_ts=_fetch_min, max_ts=to_ts), "confirmed_ts")
        log.info("  levels=%d history_keys=%d regime=%d rounds=%d fib=%d pats=%d gaps=%d corr=%d matrix=%d rsi=%d div=%d",
                 len(all_levels), len(history_by_id), len(all_regime), len(all_rounds),
                 len(all_fib), len(all_pats), len(all_gaps), len(all_corr),
                 len(all_matrix), len(all_rsi), len(all_div))

        # Sort gap list (already ordered by ts_utc from query, but ensure)
        all_gaps.sort(key=lambda r: r["ts_utc"])

        _voting_params = load_voting_params()

        # D20: voter_filter zeroes every OTHER voter's weight for this run;
        # rule_status + allow_proposed gate the four proposed voters exactly
        # like the live path (main.py) does.
        if voter_filter is not None:
            all_voter_names = set(VOTE_FUNCTIONS) | set(PROPOSED_VOTE_FUNCTIONS)
            unknown = set(voter_filter) - all_voter_names
            if unknown:
                raise ValueError(f"unknown voter_filter name(s): {unknown}")
            _weights = dict(_voting_params.get("weights", {}))
            for name in all_voter_names:
                if name not in voter_filter:
                    _weights[name] = 0.0
            _voting_params = {**_voting_params, "weights": _weights}
            if min_net_votes_override is None and len(voter_filter) == 1:
                min_net_votes_override = 1

        if min_net_votes_override is not None:
            _voting_params = {**_voting_params, "min_net_votes": min_net_votes_override}

        _allow_proposed = (
            allow_proposed if allow_proposed is not None
            else bool(load_rules_registry_params().get("allow_proposed_in_voting", False))
        )
        with conn.cursor() as _cur:
            _cur.execute(
                "SELECT id, status FROM rules WHERE id = ANY(%s)",
                (list(PROPOSED_VOTE_FUNCTIONS.keys()),),
            )
            _rule_status = {row[0]: row[1] for row in _cur.fetchall()}

        stats = {"total": 0, "signals": 0, "votes_rejected": 0,
                 "tp": 0, "sl": 0, "level_invalidated": 0, "timeout": 0, "open": 0,
                 "total_pnl": 0.0}
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
            high  = float(c["high"])
            low   = float(c["low"])
            close = float(c["close"])
            stats["total"] += 1

            levels_now = _levels_at(all_levels, ts)
            # Apply point-in-time strength/status from levels_history.
            # Levels with no history entry yet (freshly created, no touches)
            # default to (0.0, "active") — filter to active/flipped only.
            # Price pre-filter: skip levels >3×ATR from current bar's range
            # (check_level_reversion requires wick to touch zone; levels this
            # far away are guaranteed misses — avoids 9.7M _level_state_at
            # calls for irrelevant levels in walk-forward folds).
            _atr_f = float(atr) if float(atr) > 0 else 1.0
            _price_lo = low  - 3.0 * _atr_f
            _price_hi = high + 3.0 * _atr_f
            pit_levels = []
            for lvl in levels_now:
                if lvl["price_high"] < _price_lo:
                    continue
                if lvl["price_low"] > _price_hi:
                    continue
                s, status, tc, bc = _level_state_at(history_by_id, lvl["id"], ts)
                if status not in ("active", "flipped"):
                    continue
                pit_levels.append({**lvl, "strength": s, "status": status,
                                   "touch_count": tc, "break_count": bc})
            if not pit_levels:
                continue
            _structure = structure_from_swings(_h1_swings, ts, close)["structure"]
            signal = check_level_reversion(
                symbol=symbol, tf=tf, ts_utc=ts,
                high=high, low=low,
                close=close, atr=float(atr), levels=pit_levels,
                params=rule_params, structure=_structure,
            )
            if signal is None:
                continue

            # same-direction spacing filter — use NEXT bar's open (actual replay entry)
            next_c = candles[i + 1]
            entry = float(next_c["open"])
            _prev = _last_by_dir.get(signal["direction"])
            signal = apply_spacing_filter(signal, entry, _prev, _min_spacing_usd)
            if signal is None:
                continue

            ctx = _make_context(
                all_regime, all_rounds, all_fib, all_pats, all_gaps, all_corr,
                close, float(atr), ts,
                all_matrix=all_matrix, all_rsi=all_rsi, all_divergence=all_div,
                divergence_recency_bars=_voting_params.get("divergence_recency_bars", 12),
            )
            signal["components"].update(ctx)
            signal["components"]["market_structure"] = _structure

            # module_voting_v1: aggregate per-module votes; suppress if below threshold.
            _vote_result = compute_votes(
                signal["components"], signal["direction"], _voting_params,
                rule_status=_rule_status, allow_proposed=_allow_proposed,
            )
            signal["components"]["votes"] = _vote_result["votes"]
            signal["components"]["net_votes"] = _vote_result["net_votes"]
            signal["components"]["proposed_observations"] = _vote_result["proposed_observations"]
            if _vote_result["net_votes"] < _voting_params.get("min_net_votes", 2):
                stats["votes_rejected"] += 1
                continue

            # Entry already set above (dام #4: open of the NEXT bar)
            cost = total_cost(c.get("spread"), ts, in_news_window=False, costs=costs)

            direction = signal["direction"]
            if direction == "BUY":
                sl = entry - sl_mult * float(atr)
                tp = entry + tp_mult * float(atr)
            else:
                sl = entry + sl_mult * float(atr)
                tp = entry - tp_mult * float(atr)

            level_lo = float(signal["components"].get("level_price_low", 0))
            level_hi = float(signal["components"].get("level_price_high", 0))
            outcome, exit_price = _determine_outcome(
                candles, atr_arr, i + 1, entry, direction, sl, tp,
                max_safety_bars, level_lo, level_hi, break_mult,
            )
            pnl = _pnl(direction, entry, exit_price, cost)
            stats["total_pnl"] += pnl

            signal["entry"] = entry
            _last_by_dir[signal["direction"]] = {"entry": entry, "outcome": outcome}
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

        log.info(
            "Done. signals=%d tp=%d sl=%d level_invalidated=%d timeout=%d open=%d",
            stats["signals"], stats["tp"], stats["sl"],
            stats["level_invalidated"], stats["timeout"], stats["open"],
        )
        if stats["signals"] > 0:
            stats["winrate"] = stats["tp"] / stats["signals"]
            stats["expectancy"] = stats["total_pnl"] / stats["signals"]
            log.info("Raw winrate (tp only): %.1f%%  expectancy=$%.2f/trade",
                      stats["winrate"] * 100, stats["expectancy"])
        else:
            stats["winrate"] = None
            stats["expectancy"] = None

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
