"""Tests for src/engine/signal_context.py — build_context() pure function.

Verifies:
1. Anti-lookahead: rows with ts_utc > as_of_ts are ignored for every module.
2. Structure: returned dict always has the expected keys regardless of
   which modules have data.
3. Selection logic: correct row is chosen when multiple are present.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.engine.signal_context import build_context

UTC = timezone.utc
T0 = datetime(2026, 9, 18, 10, 0, tzinfo=UTC)
T_FUTURE = T0 + timedelta(minutes=5)  # one bar ahead — must be excluded
ATR = 4.0
CLOSE = 2570.0


def _regime_snap(ts, regime="trend"):
    return {"ts_utc": ts, "regime": regime}


def _round_hit(ts, level, state="APPROACHING"):
    return {"ts_utc": ts, "level": level, "multiplier": 50, "weight": 0.8, "state": state}


def _fib_zone(computed_at, price, role="retracement", overlapping=False):
    return {"computed_at": computed_at, "price": price, "level_pct": 61.8,
            "role": role, "overlapping": overlapping}


def _pattern(ts, pattern="CDLENGULFING", direction=100):
    return {"ts_utc": ts, "pattern": pattern, "direction": direction,
            "body_atr": 1.2, "at_level_id": None, "level_strength": None, "regime": None}


def _gap(ts, high, low, direction="UP", status="OPEN"):
    return {"ts_utc": ts, "gap_high": high, "gap_low": low,
            "direction": direction, "status": status, "weight": 0.78}


def _corr(ts, corr=-0.45):
    return {"ts_utc": ts, "correlation": corr}


# ---------------------------------------------------------------------------
# Anti-lookahead: future rows must be ignored
# ---------------------------------------------------------------------------

def test_regime_future_row_excluded():
    ctx = build_context(
        regime_snaps=[_regime_snap(T0, "trend"), _regime_snap(T_FUTURE, "range")],
        round_hits=[], fib_zones=[], pattern_rows=[], open_gaps=[], corr_rows=[],
        close_price=CLOSE, atr_value=ATR, as_of_ts=T0,
    )
    # T_FUTURE row must be ignored; only T0 row valid
    assert ctx["regime"] == "trend", "future regime row leaked in"


def test_correlation_future_row_excluded():
    ctx = build_context(
        regime_snaps=[], round_hits=[], fib_zones=[], pattern_rows=[], open_gaps=[],
        corr_rows=[_corr(T0, -0.4), _corr(T_FUTURE, 0.9)],
        close_price=CLOSE, atr_value=ATR, as_of_ts=T0,
    )
    assert ctx["dollar_corr"] == pytest.approx(-0.4), "future corr row leaked in"


def test_round_future_row_excluded():
    ctx = build_context(
        regime_snaps=[], round_hits=[_round_hit(T_FUTURE, CLOSE - 1.0)],
        fib_zones=[], pattern_rows=[], open_gaps=[], corr_rows=[],
        close_price=CLOSE, atr_value=ATR, as_of_ts=T0,
    )
    assert ctx["round"] is None, "future round_hit leaked in"


def test_fib_future_row_excluded():
    ctx = build_context(
        regime_snaps=[], round_hits=[],
        fib_zones=[_fib_zone(T_FUTURE, CLOSE + 0.5)],
        pattern_rows=[], open_gaps=[], corr_rows=[],
        close_price=CLOSE, atr_value=ATR, as_of_ts=T0,
    )
    assert ctx["fib"] is None, "future fib zone leaked in"


def test_pattern_future_row_excluded():
    ctx = build_context(
        regime_snaps=[], round_hits=[], fib_zones=[],
        pattern_rows=[_pattern(T_FUTURE)],
        open_gaps=[], corr_rows=[],
        close_price=CLOSE, atr_value=ATR, as_of_ts=T0,
    )
    assert ctx["pattern"] is None, "future pattern leaked in"


# ---------------------------------------------------------------------------
# Schema stability: all keys present even with empty inputs
# ---------------------------------------------------------------------------

def test_all_keys_present_when_no_data():
    ctx = build_context(
        regime_snaps=[], round_hits=[], fib_zones=[],
        pattern_rows=[], open_gaps=[], corr_rows=[],
        close_price=CLOSE, atr_value=ATR, as_of_ts=T0,
    )
    for key in ("regime", "dollar_corr", "round", "fib", "pattern", "gap"):
        assert key in ctx, f"key '{key}' missing from components"
        assert ctx[key] is None, f"key '{key}' should be None when no data"


# ---------------------------------------------------------------------------
# Selection logic
# ---------------------------------------------------------------------------

def test_regime_most_recent_row_wins():
    t_older = T0 - timedelta(minutes=10)
    ctx = build_context(
        regime_snaps=[_regime_snap(t_older, "range"), _regime_snap(T0, "trend")],
        round_hits=[], fib_zones=[], pattern_rows=[], open_gaps=[], corr_rows=[],
        close_price=CLOSE, atr_value=ATR, as_of_ts=T0,
    )
    assert ctx["regime"] == "trend"


def test_round_nearest_level_wins():
    ctx = build_context(
        regime_snaps=[], corr_rows=[], fib_zones=[], pattern_rows=[], open_gaps=[],
        round_hits=[
            _round_hit(T0, CLOSE - 10.0),   # far
            _round_hit(T0, CLOSE + 1.5),    # near
        ],
        close_price=CLOSE, atr_value=ATR, as_of_ts=T0,
    )
    assert ctx["round"] is not None
    assert ctx["round"]["level"] == pytest.approx(CLOSE + 1.5)


def test_fib_overlapping_preferred_over_plain():
    # Both within ATR, one overlapping — overlapping should win
    ctx = build_context(
        regime_snaps=[], round_hits=[], pattern_rows=[], open_gaps=[], corr_rows=[],
        fib_zones=[
            _fib_zone(T0, CLOSE + 0.5, overlapping=False),
            _fib_zone(T0, CLOSE + 0.3, overlapping=True),
        ],
        close_price=CLOSE, atr_value=ATR, as_of_ts=T0,
    )
    assert ctx["fib"] is not None
    assert ctx["fib"]["overlapping"] is True


def test_pattern_prefers_at_bar_patterns():
    t_prev = T0 - timedelta(minutes=5)
    ctx = build_context(
        regime_snaps=[], round_hits=[], fib_zones=[], open_gaps=[], corr_rows=[],
        pattern_rows=[
            _pattern(t_prev, "CDLDOJI"),
            _pattern(T0, "CDLENGULFING"),
        ],
        close_price=CLOSE, atr_value=ATR, as_of_ts=T0,
    )
    assert ctx["pattern"] is not None
    assert "CDLENGULFING" in ctx["pattern"]["bullish"]


def test_gap_nearest_to_close_wins():
    ctx = build_context(
        regime_snaps=[], round_hits=[], fib_zones=[], pattern_rows=[], corr_rows=[],
        open_gaps=[
            _gap(T0, CLOSE + 20, CLOSE + 15),   # far up gap
            _gap(T0, CLOSE + 3,  CLOSE + 1),    # near gap
        ],
        close_price=CLOSE, atr_value=ATR, as_of_ts=T0,
    )
    assert ctx["gap"] is not None
    assert ctx["gap"]["high"] == pytest.approx(CLOSE + 3)
