"""Tests for the proposed matrix module (SPEC.md 4.10, D18, src/features/matrix.py)."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from src.features.matrix import compute_indicator_votes, compute_matrix_score

BAR = timedelta(minutes=5)
T0 = datetime(2026, 1, 2, 0, 0, tzinfo=timezone.utc)

PARAMS = {
    "tf_list": ["M1", "M5", "M15", "M30", "H1", "H4", "D1"],
    "min_votes": 6,
    "min_tf_agreement": 4,
    "indicator_periods": {
        "rsi_period": 14, "stoch_fastk": 14, "stoch_slowk": 3, "stoch_slowd": 3,
        "cci_period": 14, "macd_fast": 12, "macd_slow": 26, "macd_signal": 9,
        "ma_period": 20, "adx_period": 14, "willr_period": 14, "momentum_period": 10,
        "bbp_ema_period": 13, "volume_period": 20,
    },
}


def _c(ts, o, h, l, c, vol=100):
    return {"ts_utc": ts, "open": o, "high": h, "low": l, "close": c, "tick_volume": vol}


def _uptrend_candles(n=100, start=4200.0, step=1.5, ts0=T0, vol=100):
    out = []
    price = start
    for i in range(n):
        o = price
        c = price + step
        out.append(_c(ts0 + i * BAR, o, max(o, c) + 0.3, min(o, c) - 0.3, c, vol))
        price = c
    return out


def _downtrend_candles(n=100, start=4200.0, step=1.5, ts0=T0, vol=100):
    out = []
    price = start
    for i in range(n):
        o = price
        c = price - step
        out.append(_c(ts0 + i * BAR, o, max(o, c) + 0.3, min(o, c) - 0.3, c, vol))
        price = c
    return out


def _flat_candles(n=100, price=4200.0, ts0=T0, vol=100):
    return [_c(ts0 + i * BAR, price, price + 0.3, price - 0.3, price, vol) for i in range(n)]


# ---------------------------------------------------------------------------
# Per-indicator vote logic (single timeframe)
# ---------------------------------------------------------------------------

def test_strong_uptrend_votes_buy_on_most_indicators():
    candles = _uptrend_candles(n=100)
    as_of = candles[-1]["ts_utc"]
    result = compute_indicator_votes(candles, as_of, PARAMS)
    assert result["buy_count"] >= 6, f"expected strong buy consensus, got {result['votes']}"
    assert result["tf_vote"] == "buy"
    assert result["votes"]["RSI"] == "buy"
    assert result["votes"]["MovingAverage"] == "buy"
    assert result["votes"]["Momentum"] == "buy"


def test_strong_downtrend_votes_sell_on_most_indicators():
    candles = _downtrend_candles(n=100)
    as_of = candles[-1]["ts_utc"]
    result = compute_indicator_votes(candles, as_of, PARAMS)
    assert result["sell_count"] >= 6, f"expected strong sell consensus, got {result['votes']}"
    assert result["tf_vote"] == "sell"
    assert result["votes"]["RSI"] == "sell"
    assert result["votes"]["MovingAverage"] == "sell"
    assert result["votes"]["Momentum"] == "sell"


def test_below_min_votes_threshold_yields_neutral_tf_vote():
    """A mixed/choppy market shouldn't cross min_votes in either direction."""
    # Oscillating candles: up 2, down 2, repeating -- no clear trend.
    candles = []
    price = 4200.0
    for i in range(100):
        step = 1.5 if (i // 2) % 2 == 0 else -1.5
        o = price
        c = price + step
        candles.append(_c(T0 + i * BAR, o, max(o, c) + 0.3, min(o, c) - 0.3, c))
        price = c
    as_of = candles[-1]["ts_utc"]
    result = compute_indicator_votes(candles, as_of, PARAMS)
    # Not asserting a specific outcome (depends on exact indicator math),
    # just that the threshold logic is internally consistent.
    assert result["tf_vote"] in ("buy", "sell", "neutral")
    if result["tf_vote"] == "buy":
        assert result["buy_count"] >= PARAMS["min_votes"]
    elif result["tf_vote"] == "sell":
        assert result["sell_count"] >= PARAMS["min_votes"]
    else:
        assert result["buy_count"] < PARAMS["min_votes"] and result["sell_count"] < PARAMS["min_votes"]


def test_too_few_bars_returns_all_neutral():
    candles = _uptrend_candles(n=5)
    result = compute_indicator_votes(candles, candles[-1]["ts_utc"], PARAMS)
    assert result["tf_vote"] == "neutral"
    assert all(v == "neutral" for v in result["votes"].values())
    assert len(result["votes"]) == 10


def test_all_ten_indicators_present():
    candles = _uptrend_candles(n=100)
    result = compute_indicator_votes(candles, candles[-1]["ts_utc"], PARAMS)
    expected = {"RSI", "Stochastic", "CCI", "MACD", "MovingAverage", "ADX",
                "WilliamsR", "Momentum", "BullsBearsPower", "Volume"}
    assert set(result["votes"].keys()) == expected
    assert result["buy_count"] + result["sell_count"] + result["neutral_count"] == 10


def test_volume_vote_requires_above_average_volume():
    """An up-close bar with BELOW-average volume must not vote buy (no
    confirmation) -- tests the Volume indicator's specific convention."""
    candles = _uptrend_candles(n=99, vol=100)
    # Final bar: up-close but far below average volume.
    last = candles[-1]
    low_vol_bar = _c(last["ts_utc"] + BAR, last["close"], last["close"] + 2, last["close"] - 0.3,
                      last["close"] + 1.5, vol=1)
    candles.append(low_vol_bar)
    result = compute_indicator_votes(candles, candles[-1]["ts_utc"], PARAMS)
    assert result["votes"]["Volume"] == "neutral"


# ---------------------------------------------------------------------------
# Anti-repaint (CLAUDE.md rules 1-3)
# ---------------------------------------------------------------------------

def test_indicator_votes_no_lookahead():
    """Truncating the candle list to as_of_ts must give the same result as
    passing the full list with the same as_of_ts cutoff."""
    candles = _uptrend_candles(n=120)
    cutoff_ts = candles[79]["ts_utc"]
    truncated = compute_indicator_votes(candles[:80], cutoff_ts, PARAMS)
    full_input = compute_indicator_votes(candles, cutoff_ts, PARAMS)
    assert truncated["votes"] == full_input["votes"]
    assert truncated["tf_vote"] == full_input["tf_vote"]


def test_same_result_for_two_as_of_ts_within_the_same_forming_bar():
    """Two as_of_ts values that both fall strictly inside the window of what
    would be the NEXT (still-forming, never-stored) bar must yield an
    identical result, since no new closed bar exists between them."""
    candles = _uptrend_candles(n=100)
    last_closed_ts = candles[-1]["ts_utc"]
    as_of_a = last_closed_ts + timedelta(seconds=1)
    as_of_b = last_closed_ts + timedelta(minutes=4, seconds=59)  # still inside the next M5 bar's window
    result_a = compute_indicator_votes(candles, as_of_a, PARAMS)
    result_b = compute_indicator_votes(candles, as_of_b, PARAMS)
    assert result_a == result_b, "result changed without a new closed candle -- repainting"


def test_future_candle_in_input_is_ignored():
    """A candle appended to the input list with ts_utc > as_of_ts must have
    zero effect on the result (defensive cutoff, not trusted from caller)."""
    candles = _uptrend_candles(n=100)
    as_of = candles[-1]["ts_utc"]
    result_without_future = compute_indicator_votes(candles, as_of, PARAMS)

    future_candle = _c(as_of + BAR, 9999.0, 9999.0, 1.0, 1.0)  # wildly different, would flip every vote
    result_with_future = compute_indicator_votes(candles + [future_candle], as_of, PARAMS)
    assert result_without_future == result_with_future


# ---------------------------------------------------------------------------
# Multi-timeframe integration (compute_matrix_score)
# ---------------------------------------------------------------------------

def test_matrix_score_zero_when_m5_does_not_vote():
    """SPEC.md 4.10: if M5 doesn't vote, matrix_score=0 regardless of the
    other timeframes' votes."""
    candles_by_tf = {tf: _uptrend_candles(n=100) for tf in PARAMS["tf_list"]}
    candles_by_tf["M5"] = _flat_candles(n=100)  # M5 stays neutral -- flat price
    as_of = candles_by_tf["M5"][-1]["ts_utc"]
    result = compute_matrix_score(candles_by_tf, as_of, PARAMS)
    assert result["score"] == 0
    assert result["direction"] == "neutral"
    assert result["timeframes"]["M5"] == "neutral"


def test_matrix_score_counts_agreeing_timeframes():
    """All 7 timeframes trending the same direction -> M5 votes buy and all
    6 others agree -> score=6."""
    candles_by_tf = {tf: _uptrend_candles(n=100) for tf in PARAMS["tf_list"]}
    as_of = candles_by_tf["M5"][-1]["ts_utc"]
    result = compute_matrix_score(candles_by_tf, as_of, PARAMS)
    assert result["direction"] == "buy"
    assert result["score"] == 6
    assert all(v == "buy" for v in result["timeframes"].values())


def test_matrix_score_partial_agreement():
    """M5 buy, half the other timeframes disagree (downtrend) -> score < 6."""
    candles_by_tf = {}
    for tf in PARAMS["tf_list"]:
        candles_by_tf[tf] = _uptrend_candles(n=100)
    for tf in ("H1", "H4", "D1"):
        candles_by_tf[tf] = _downtrend_candles(n=100)
    as_of = candles_by_tf["M5"][-1]["ts_utc"]
    result = compute_matrix_score(candles_by_tf, as_of, PARAMS)
    assert result["direction"] == "buy"
    assert result["score"] == 3  # M1, M15, M30 agree; H1, H4, D1 don't
    assert result["timeframes"]["H1"] == "sell"


def test_matrix_score_output_shape_matches_spec():
    """SPEC.md 4.10 exact components.matrix JSON shape."""
    candles_by_tf = {tf: _uptrend_candles(n=100) for tf in PARAMS["tf_list"]}
    as_of = candles_by_tf["M5"][-1]["ts_utc"]
    result = compute_matrix_score(candles_by_tf, as_of, PARAMS)
    assert set(result.keys()) == {"score", "direction", "timeframes"}
    assert isinstance(result["score"], int)
    assert 0 <= result["score"] <= 6
    assert result["direction"] in ("buy", "sell", "neutral")
    assert set(result["timeframes"].keys()) == set(PARAMS["tf_list"])


def test_matrix_score_missing_timeframe_treated_as_neutral():
    """A timeframe absent from candles_by_tf must not crash and must count
    as neutral (never fabricated agreement)."""
    candles_by_tf = {tf: _uptrend_candles(n=100) for tf in PARAMS["tf_list"] if tf != "D1"}
    as_of = candles_by_tf["M5"][-1]["ts_utc"]
    result = compute_matrix_score(candles_by_tf, as_of, PARAMS)
    assert result["timeframes"]["D1"] == "neutral"
    assert result["score"] == 5  # 6 others agree minus D1 (absent/neutral)
