"""SPEC.md 4.2 (`levels`) — support/resistance zones with a decaying
strength score.

CLAUDE.md rules 1/2 (anti-repainting — SPEC.md calls this the single most
important point in this module): compute_levels() takes as_of_ts and
enforces it itself — every candle with ts_utc > as_of_ts is dropped before
anything else runs, regardless of what the caller passed in. A swing at
bar i needs `swing_n` bars *after* it to confirm (both the structural and
retracement conditions look forward), so a level's created_ts can predate
as_of_ts by a lot, but the level itself never appears in the output until
as_of_ts has advanced past created_ts + swing_n bars. This is what the
anti-lookahead test in tests/features/test_levels.py checks directly.

compute_levels() is pure — no DB access — so backtest and live call the
exact same function (CLAUDE.md rule 6). It recomputes the full state from
raw candles on every call rather than mutating stored state incrementally;
simpler to prove correct, and cheap enough at this data volume (see
docs/ for the real-data timing).
"""
from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import talib
import yaml

PARAMS_PATH = Path(__file__).resolve().parents[2] / "config" / "params.yaml"


def load_levels_params(path: Path = PARAMS_PATH) -> dict[str, Any]:
    with open(path) as f:
        return yaml.safe_load(f)["levels"]


def _zone_gap(a_lo: float, a_hi: float, b_lo: float, b_hi: float) -> float:
    """Distance between two zones; <= 0 means they already overlap."""
    return max(a_lo, b_lo) - min(a_hi, b_hi)


def _add_or_merge(
    levels: list[dict[str, Any]],
    kind: str,
    new_lo: float,
    new_hi: float,
    created_ts: datetime,
    created_idx: int,
    atr_val: float,
    merge_gap_thresh: float,
) -> None:
    """SPEC.md 4.2: zones of the same kind closer than merge_atr_mult * ATR
    ARE one zone — merge into the existing (earlier) level rather than
    creating a duplicate row. Only active/flipped levels of the same
    (current) kind are eligible."""
    for lvl in levels:
        if lvl["kind"] != kind or lvl["status"] not in ("active", "flipped"):
            continue
        if _zone_gap(new_lo, new_hi, lvl["price_low"], lvl["price_high"]) < merge_gap_thresh:
            lvl["price_low"] = min(lvl["price_low"], new_lo)
            lvl["price_high"] = max(lvl["price_high"], new_hi)
            return
    levels.append(
        {
            "kind": kind,
            "initial_kind": kind,   # stable even after flips — used for history keying
            "price_low": new_lo,
            "price_high": new_hi,
            "created_ts": created_ts,
            "created_idx": created_idx,
            "last_touch": None,
            "touch_count": 0,
            "break_count": 0,
            "status": "active",
            "atr_at_birth": atr_val,
            "touches": [],
        }
    )


def compute_levels(
    candles: list[dict[str, Any]],
    as_of_ts: datetime,
    symbol: str,
    tf: str,
    params: dict[str, Any] | None = None,
    record_history: bool = False,
) -> list[dict[str, Any]] | tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """candles: {"ts_utc","open","high","low","close"} dicts, any order/range —
    the as_of_ts cutoff is enforced here, not trusted from the caller.
    Returns level dicts matching the `levels` table columns.

    record_history=True: also returns a list of state-change events
    (touch / break / creation / expiry) suitable for bulk-insert into
    levels_history. Used only by scripts/rebuild_levels_history.py — live
    callers leave the default False to avoid any overhead.
    Return type when record_history=True: (levels_list, history_events_list).
    Each history event: {created_ts, initial_kind, ts_utc, strength, status,
                         touch_count, break_count}.
    """
    if params is None:
        params = load_levels_params()

    hist = sorted((c for c in candles if c["ts_utc"] <= as_of_ts), key=lambda c: c["ts_utc"])
    n = len(hist)
    N = params["swing_n"]
    atr_period = params["atr_period"]
    if n < 2 * N + atr_period + 1:
        if record_history:
            return [], []
        return []

    highs = np.array([float(c["high"]) for c in hist])
    lows = np.array([float(c["low"]) for c in hist])
    closes = np.array([float(c["close"]) for c in hist])
    ts = [c["ts_utc"] for c in hist]

    atr = talib.ATR(highs, lows, closes, timeperiod=atr_period)

    k = params["swing_k"]
    zone_mult = params["zone_width_atr_mult"]
    break_mult = params["break_atr_mult"]
    merge_mult = params["merge_atr_mult"]
    lam = params["strength_lambda"]
    expiry_strength = params["strength_expiry_threshold"]
    expiry_dist_mult = params["expiry_distance_atr_mult"]
    max_break_count = params.get("max_break_count", 5)
    extreme_max_days = params.get("extreme_max_days", 365)
    normal_max_days = params.get("normal_max_days", 30)

    levels: list[dict[str, Any]] = []
    history_events: list[dict[str, Any]] = []

    # Per-level prev state for change detection (only populated when record_history=True).
    # Key: index into `levels` list; value: (touch_count, break_count, status)
    prev_state: dict[int, tuple[int, int, str]] = {}

    def _emit(lvl: dict, bar_j: int) -> None:
        """Append a history event for this level at bar j."""
        cur_strength = sum(math.exp(-lam * (bar_j - t_idx)) for t_idx in lvl["touches"])
        if lvl["status"] == "flipped":
            cur_strength /= 2
        history_events.append({
            "created_ts": lvl["created_ts"],
            "initial_kind": lvl["initial_kind"],
            "ts_utc": ts[bar_j],
            "strength": float(cur_strength),
            "status": lvl["status"],
            "touch_count": int(lvl["touch_count"]),
            "break_count": int(lvl["break_count"]),
        })

    # --- Interleaved walk-forward + swing detection ---
    #
    # Root-cause fix for "zero active levels" (2026-09-16): the original
    # two-pass approach (swing detection first, walk-forward second) meant
    # that when _add_or_merge() ran for a new swing at bar i, ALL previously
    # detected levels still had status="active" — the walk-forward hadn't
    # run yet, so levels that would eventually be expired (break_count >= max)
    # were incorrectly used as merge targets for the new swing.
    #
    # Fix: for each bar j, run the walk-forward BEFORE detecting the swing
    # that is confirmed at bar j (i.e., the swing at bar j-N). By the time
    # _add_or_merge is called, every level broken by bars 0..j already has
    # status="expired" and is correctly skipped as a merge candidate.
    for j in range(n):
        cur_high  = highs[j]
        cur_low   = lows[j]
        cur_close = closes[j]
        cur_atr   = atr[j]

        # Walk-forward for bar j -- only when ATR is valid (early NaN bars
        # have no reliable break threshold, same behaviour as before).
        if not math.isnan(cur_atr):
            for idx, lvl in enumerate(levels):
                if lvl["created_idx"] + N > j or lvl["status"] not in ("active", "flipped"):
                    continue
                lo, hi = lvl["price_low"], lvl["price_high"]

                if record_history:
                    snap = (lvl["touch_count"], lvl["break_count"], lvl["status"])

                entered = cur_low <= hi and cur_high >= lo
                closed_outside = cur_close < lo or cur_close > hi
                if entered and closed_outside:
                    lvl["touch_count"] += 1
                    lvl["last_touch"] = ts[j]
                    lvl["touches"].append(j)

                # SPEC.md: break = CLOSE beyond the zone, never wick-only.
                if lvl["kind"] == "resistance" and cur_close > hi + break_mult * cur_atr:
                    lvl["break_count"] += 1
                    lvl["kind"] = "support"
                    lvl["status"] = "expired" if lvl["break_count"] >= max_break_count else "flipped"
                elif lvl["kind"] == "support" and cur_close < lo - break_mult * cur_atr:
                    lvl["break_count"] += 1
                    lvl["kind"] = "resistance"
                    lvl["status"] = "expired" if lvl["break_count"] >= max_break_count else "flipped"

                if record_history:
                    new_snap = (lvl["touch_count"], lvl["break_count"], lvl["status"])
                    if new_snap != snap:
                        _emit(lvl, j)
                        prev_state[idx] = new_snap

        # Swing detection: bar j confirms the swing at bar j-N.
        # A swing at swing_idx needs N bars before it and N bars after it,
        # so it is confirmed (and added to levels) exactly when j = swing_idx + N.
        # At this point the walk-forward above has processed bars 0..j, so any
        # level expired by those bars is already marked "expired" -- _add_or_merge
        # will correctly skip it and create an independent new level instead.
        swing_idx = j - N
        if swing_idx < N or math.isnan(atr[swing_idx]):
            continue

        before_hi = highs[swing_idx - N:swing_idx]
        after_hi  = highs[swing_idx + 1:swing_idx + N + 1]
        before_lo = lows[swing_idx - N:swing_idx]
        after_lo  = lows[swing_idx + 1:swing_idx + N + 1]

        old_count = len(levels)

        if highs[swing_idx] > before_hi.max() and highs[swing_idx] > after_hi.max():
            retrace = highs[swing_idx] - after_lo.min()
            if retrace >= k * atr[swing_idx]:
                half = zone_mult * atr[swing_idx] / 2
                _add_or_merge(
                    levels, "resistance",
                    highs[swing_idx] - half, highs[swing_idx] + half,
                    ts[swing_idx], swing_idx, atr[swing_idx],
                    merge_mult * atr[swing_idx],
                )

        if lows[swing_idx] < before_lo.min() and lows[swing_idx] < after_lo.min():
            retrace = after_hi.max() - lows[swing_idx]
            if retrace >= k * atr[swing_idx]:
                half = zone_mult * atr[swing_idx] / 2
                _add_or_merge(
                    levels, "support",
                    lows[swing_idx] - half, lows[swing_idx] + half,
                    ts[swing_idx], swing_idx, atr[swing_idx],
                    merge_mult * atr[swing_idx],
                )

        # Emit creation events for newly added levels
        if record_history and len(levels) > old_count:
            for new_idx in range(old_count, len(levels)):
                new_lvl = levels[new_idx]
                prev_state[new_idx] = (0, 0, "active")
                history_events.append({
                    "created_ts": new_lvl["created_ts"],
                    "initial_kind": new_lvl["initial_kind"],
                    "ts_utc": ts[j],
                    "strength": 0.0,
                    "status": "active",
                    "touch_count": 0,
                    "break_count": 0,
                })

    # --- Strength + expiry, evaluated as of the last confirmed bar ---
    last_idx = n - 1
    cur_close, cur_atr = closes[last_idx], atr[last_idx]

    result = []
    for idx, lvl in enumerate(levels):
        if lvl["created_idx"] + N > last_idx:
            continue  # not confirmed yet as of as_of_ts -> must not appear

        strength = sum(math.exp(-lam * (last_idx - t_idx)) for t_idx in lvl["touches"])
        if lvl["status"] == "flipped":
            strength /= 2  # SPEC.md: halved on role flip

        status = lvl["status"]
        if status in ("active", "flipped") and not math.isnan(cur_atr):
            mid = (lvl["price_low"] + lvl["price_high"]) / 2
            # Strength expiry only applies to TESTED levels (touch_count > 0).
            # A fresh level (never visited) has strength=0 by construction --
            # applying the threshold there would expire it instantly, before
            # price ever gets a chance to react to it. The time-based and
            # distance rules handle untested-level cleanup instead.
            strength_expired = lvl["touch_count"] > 0 and strength < expiry_strength
            if strength_expired or abs(cur_close - mid) > expiry_dist_mult * cur_atr:
                status = "expired"

        # Time-based retention: age is measured from the LAST TOUCH (or from
        # creation for untouched levels). This way a level tested yesterday is
        # "fresh" regardless of how old its creation date is, while a level
        # that price has abandoned for months expires cleanly.
        # extreme = never touched (touch_count==0) -> extreme_max_days
        # normal  = tested at least once               -> normal_max_days
        if status in ("active", "flipped"):
            anchor = lvl["last_touch"] if lvl["last_touch"] is not None else lvl["created_ts"]
            age_days = (ts[last_idx] - anchor).total_seconds() / 86400
            max_days = extreme_max_days if lvl["touch_count"] == 0 else normal_max_days
            if age_days > max_days:
                status = "expired"

        # Emit final-expiry event when status changed vs walk-forward state
        if record_history and status != lvl["status"]:
            history_events.append({
                "created_ts": lvl["created_ts"],
                "initial_kind": lvl["initial_kind"],
                "ts_utc": ts[last_idx],
                "strength": float(strength),
                "status": status,
                "touch_count": int(lvl["touch_count"]),
                "break_count": int(lvl["break_count"]),
            })

        result.append(
            {
                "symbol": symbol,
                "tf_origin": tf,
                "kind": lvl["kind"],
                # numpy scalars (from array arithmetic above) confuse psycopg2's
                # adapter -- it silently stringifies them as "np.float64(...)"
                # instead of a plain number. Cast to native Python types here,
                # the one place results leave numpy-land.
                "price_low": float(lvl["price_low"]),
                "price_high": float(lvl["price_high"]),
                "created_ts": lvl["created_ts"],
                "last_touch": lvl["last_touch"],
                "touch_count": int(lvl["touch_count"]),
                "break_count": int(lvl["break_count"]),
                "strength": float(strength),
                "status": status,
                "atr_at_birth": float(lvl["atr_at_birth"]),
            }
        )

    if record_history:
        return result, history_events
    return result
