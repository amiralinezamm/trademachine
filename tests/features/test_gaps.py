"""Tests for SPEC.md 4.5 gaps module."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.features.gaps import compute_gaps, backtest_two_phase_claim

BAR = timedelta(minutes=5)
T0  = datetime(2026, 1, 2, 10, 0, tzinfo=timezone.utc)

PARAMS = {
    "min_size_dollars":    3.0,
    "initial_weight":      0.78,
    "decay_rate_per_hour": 0.05,  # half-life ~14h; design param — SPEC unspecified
    "half_fill_fraction":  0.5,
    "fill_mode":           "body",
}


def _c(ts, open_, high, low, close):
    return {"ts_utc": ts, "open": open_, "high": high, "low": low, "close": close}


def _base_before_gap():
    """5 quiet candles at 4200."""
    candles = []
    for i in range(5):
        t = T0 + i * BAR
        candles.append(_c(t, 4200.0, 4205.0, 4195.0, 4200.0))
    return candles


# ---------------------------------------------------------------------------
# 1. Anti-lookahead
# ---------------------------------------------------------------------------

def test_no_lookahead():
    """Candles after as_of_ts must not affect gap detection."""
    candles = _base_before_gap()
    last_base_ts = candles[-1]["ts_utc"]

    # Gap UP of $5 at t6
    t6 = last_base_ts + BAR
    candles.append(_c(t6, 4210.0, 4215.0, 4209.0, 4212.0))  # open 4210, prev_close 4200 → gap=10

    # Fill bar AFTER the gap (would mark FILLED if seen)
    t7 = t6 + BAR
    candles.append(_c(t7, 4212.0, 4212.0, 4198.0, 4199.0))  # body fills gap (close ≤ 4200)

    # as_of just after gap forms, before fill bar
    as_of_before_fill = t6 + timedelta(seconds=1)
    gaps_limited = compute_gaps(candles, as_of_before_fill, "TEST", "M5", params=PARAMS)

    as_of_full = t7 + timedelta(seconds=1)
    gaps_full   = compute_gaps(candles, as_of_full,         "TEST", "M5", params=PARAMS)

    assert len(gaps_limited) == 1
    assert gaps_limited[0]["status"] == "OPEN",   "fill bar leaked into truncated compute"
    assert gaps_full[0]["status"] in ("FILLED", "HALF_FILLED")


# ---------------------------------------------------------------------------
# 2. Gap UP detected and tracked — body fill
# ---------------------------------------------------------------------------

def test_gap_up_body_fill():
    """Gap UP: prev_close=4200, open=4210 → gap=[4200,4210].
    A bar that closes at 4198 fills the gap (body mode)."""
    candles = _base_before_gap()
    t = candles[-1]["ts_utc"]

    t1 = t + BAR
    candles.append(_c(t1, 4210.0, 4215.0, 4208.0, 4212.0))  # gap UP of $10

    t2 = t1 + BAR
    candles.append(_c(t2, 4212.0, 4213.0, 4197.0, 4198.0))  # close 4198 < 4200 → FILLED

    gaps = compute_gaps(candles, t2 + timedelta(seconds=1), "TEST", "M5", params=PARAMS)
    assert len(gaps) == 1
    g = gaps[0]
    assert g["direction"] == "UP"
    assert g["gap_high"]  == pytest.approx(4210.0)
    assert g["gap_low"]   == pytest.approx(4200.0)
    assert g["size"]      == pytest.approx(10.0)
    assert g["status"]    == "FILLED"
    assert g["fill_ts"]   is not None
    assert g["half_fill_ts"] is not None  # must have passed through 4205 (mid)


# ---------------------------------------------------------------------------
# 3. Gap DOWN detected — wick fill
# ---------------------------------------------------------------------------

def test_gap_down_wick_fill():
    """Gap DOWN: prev_close=4200, open=4190 → gap=[4190,4200].
    In wick mode, a bar with high=4201 fills the gap."""
    candles = _base_before_gap()
    t = candles[-1]["ts_utc"]
    params = {**PARAMS, "fill_mode": "wick"}

    t1 = t + BAR
    candles.append(_c(t1, 4190.0, 4191.0, 4185.0, 4188.0))  # gap DOWN of $10

    t2 = t1 + BAR
    candles.append(_c(t2, 4188.0, 4201.0, 4186.0, 4187.0))  # wick=4201 > 4200 → FILLED

    gaps = compute_gaps(candles, t2 + timedelta(seconds=1), "TEST", "M5", params=params)
    g = [x for x in gaps if x["direction"] == "DOWN"][0]
    assert g["status"] == "FILLED"


# ---------------------------------------------------------------------------
# 4. Half-fill reached, gap not fully filled
# ---------------------------------------------------------------------------

def test_half_fill_not_completed():
    """Gap UP [4200,4210]; price reaches 4204 (< 4205 mid) but never reaches 4200."""
    candles = _base_before_gap()
    t = candles[-1]["ts_utc"]

    t1 = t + BAR
    candles.append(_c(t1, 4210.0, 4215.0, 4208.0, 4212.0))  # gap UP $10

    t2 = t1 + BAR
    candles.append(_c(t2, 4212.0, 4212.0, 4203.0, 4204.0))  # close 4204 ≤ 4205 mid → HALF

    t3 = t2 + BAR
    candles.append(_c(t3, 4204.0, 4212.0, 4203.0, 4210.0))  # bounces back, never reaches 4200

    as_of = t3 + timedelta(seconds=1)
    gaps = compute_gaps(candles, as_of, "TEST", "M5", params=PARAMS)
    g = gaps[0]
    assert g["status"]       == "HALF_FILLED"
    assert g["half_fill_ts"] is not None
    assert g["fill_ts"]      is None


# ---------------------------------------------------------------------------
# 5. Small gap below min_size is ignored
# ---------------------------------------------------------------------------

def test_gap_below_min_size_ignored():
    """A gap of $2 (< min_size_dollars=3) must not be detected."""
    candles = _base_before_gap()
    t = candles[-1]["ts_utc"]

    t1 = t + BAR
    candles.append(_c(t1, 4202.0, 4205.0, 4201.0, 4202.0))  # open=4202, prev_close=4200 → gap=$2

    gaps = compute_gaps(candles, t1 + timedelta(seconds=1), "TEST", "M5", params=PARAMS)
    assert len(gaps) == 0, "gap of $2 < min_size=3 should be ignored"


# ---------------------------------------------------------------------------
# 6. Backtest two-phase claim structure
# ---------------------------------------------------------------------------

def test_backtest_returns_both_modes():
    """backtest_two_phase_claim must return results for both 'body' and 'wick' modes."""
    candles = _base_before_gap()
    t = candles[-1]["ts_utc"]
    # Add a gap and some follow-through
    t1 = t + BAR
    candles.append(_c(t1, 4210.0, 4215.0, 4208.0, 4212.0))

    result = backtest_two_phase_claim(candles, t1 + timedelta(seconds=1), params=PARAMS)
    assert "body" in result
    assert "wick" in result
    for mode in ("body", "wick"):
        assert "total_gaps"     in result[mode]
        assert "two_phase_freq" in result[mode]
        assert "rule_status"    in result[mode]
        assert result[mode]["rule_status"] in ("testing", "rejected")


# ---------------------------------------------------------------------------
# 7. Weight decays over time
# ---------------------------------------------------------------------------

def test_weight_decays():
    """Weight at as_of_ts near gap creation > weight much later."""
    candles = _base_before_gap()
    t = candles[-1]["ts_utc"]

    t1 = t + BAR
    candles.append(_c(t1, 4210.0, 4215.0, 4208.0, 4212.0))

    # Add 240 quiet bars (= 20h of bars)
    for i in range(240):
        t1 += BAR
        candles.append(_c(t1, 4212.0, 4213.0, 4211.0, 4212.0))

    gaps_early = compute_gaps(candles, candles[6]["ts_utc"] + timedelta(seconds=1),
                              "TEST", "M5", params=PARAMS)
    gaps_late  = compute_gaps(candles, t1 + timedelta(seconds=1),
                              "TEST", "M5", params=PARAMS)

    w_early = gaps_early[0]["weight"]
    w_late  = gaps_late[0]["weight"]
    assert w_early > w_late, "weight must decay over time"
    assert w_early <= PARAMS["initial_weight"]
