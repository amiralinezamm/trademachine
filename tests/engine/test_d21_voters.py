"""D21 tests: dollar_correlation_direction voter + round_numbers ACCELERATION."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

UTC = timezone.utc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ts(n: int = 0) -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=5 * n)


def _make_components(
    dollar_corr: float | None = None,
    dxy_direction: str | None = None,
    round_state: str | None = None,
    round_level: float | None = None,
    round_direction: str | None = None,
    close: float = 2000.0,
) -> dict:
    comp: dict = {"close": close}
    if dollar_corr is not None:
        comp["dollar_corr"] = dollar_corr
    if dxy_direction is not None:
        comp["dxy_direction"] = dxy_direction
    if round_state is not None:
        comp["round"] = {
            "level": round_level or 2000.0,
            "state": round_state,
            "direction": round_direction,
            "multiplier": 1,
            "weight": 1.0,
            "distance_atr": 0.5,
        }
    return comp


# ---------------------------------------------------------------------------
# _vote_dollar_correlation_direction tests
# ---------------------------------------------------------------------------

from src.engine.module_voting import _vote_dollar_correlation_direction


class TestDollarCorrelationDirection:
    def test_negative_corr_dxy_up_agrees_with_sell(self):
        # DXY up + negative corr -> gold expected down -> SELL gets +1
        comp = _make_components(dollar_corr=-0.7, dxy_direction="up")
        assert _vote_dollar_correlation_direction(comp, "SELL") == 1

    def test_negative_corr_dxy_up_disagrees_with_buy(self):
        comp = _make_components(dollar_corr=-0.7, dxy_direction="up")
        assert _vote_dollar_correlation_direction(comp, "BUY") == -1

    def test_negative_corr_dxy_down_agrees_with_buy(self):
        comp = _make_components(dollar_corr=-0.7, dxy_direction="down")
        assert _vote_dollar_correlation_direction(comp, "BUY") == 1

    def test_negative_corr_dxy_down_disagrees_with_sell(self):
        comp = _make_components(dollar_corr=-0.7, dxy_direction="down")
        assert _vote_dollar_correlation_direction(comp, "SELL") == -1

    def test_positive_corr_dxy_up_agrees_with_buy(self):
        # Unusual positive corr: DXY up -> gold up -> BUY gets +1
        comp = _make_components(dollar_corr=0.6, dxy_direction="up")
        assert _vote_dollar_correlation_direction(comp, "BUY") == 1

    def test_positive_corr_dxy_up_disagrees_with_sell(self):
        comp = _make_components(dollar_corr=0.6, dxy_direction="up")
        assert _vote_dollar_correlation_direction(comp, "SELL") == -1

    def test_weak_corr_below_threshold_returns_zero(self):
        # |dollar_corr| = 0.1 < 0.3 threshold
        comp = _make_components(dollar_corr=-0.1, dxy_direction="up")
        assert _vote_dollar_correlation_direction(comp, "SELL") == 0

    def test_flat_dxy_returns_zero(self):
        comp = _make_components(dollar_corr=-0.8, dxy_direction="flat")
        assert _vote_dollar_correlation_direction(comp, "SELL") == 0

    def test_missing_dollar_corr_returns_zero(self):
        comp = _make_components(dollar_corr=None, dxy_direction="up")
        assert _vote_dollar_correlation_direction(comp, "SELL") == 0

    def test_missing_dxy_direction_returns_zero(self):
        comp = _make_components(dollar_corr=-0.8, dxy_direction=None)
        assert _vote_dollar_correlation_direction(comp, "SELL") == 0

    def test_at_threshold_exactly_votes(self):
        # |dollar_corr| = 0.3 exactly — should vote (>= not >)
        comp = _make_components(dollar_corr=-0.3, dxy_direction="up")
        assert _vote_dollar_correlation_direction(comp, "SELL") == 1


# ---------------------------------------------------------------------------
# _vote_round_numbers ACCELERATION tests
# ---------------------------------------------------------------------------

from src.engine.module_voting import _vote_round_numbers


class TestRoundNumbersAcceleration:
    # REVERSAL regression — existing behavior must not change
    def test_reversal_buy_from_below_agrees(self):
        # close=1999 (below 2000 round), REVERSAL -> expected bounce up -> BUY agrees
        comp = _make_components(round_state="REVERSAL", round_level=2000.0, close=1999.0)
        assert _vote_round_numbers(comp, "BUY") == 1

    def test_reversal_sell_from_above_agrees(self):
        comp = _make_components(round_state="REVERSAL", round_level=2000.0, close=2001.0)
        assert _vote_round_numbers(comp, "SELL") == 1

    def test_reversal_buy_from_above_disagrees(self):
        comp = _make_components(round_state="REVERSAL", round_level=2000.0, close=2001.0)
        assert _vote_round_numbers(comp, "BUY") == -1

    # ACCELERATION new behavior
    def test_acceleration_up_agrees_with_buy(self):
        comp = _make_components(round_state="ACCELERATION", round_level=2000.0,
                                round_direction="UP", close=2005.0)
        assert _vote_round_numbers(comp, "BUY") == 1

    def test_acceleration_up_disagrees_with_sell(self):
        comp = _make_components(round_state="ACCELERATION", round_level=2000.0,
                                round_direction="UP", close=2005.0)
        assert _vote_round_numbers(comp, "SELL") == -1

    def test_acceleration_down_agrees_with_sell(self):
        comp = _make_components(round_state="ACCELERATION", round_level=2000.0,
                                round_direction="DOWN", close=1995.0)
        assert _vote_round_numbers(comp, "SELL") == 1

    def test_acceleration_down_disagrees_with_buy(self):
        comp = _make_components(round_state="ACCELERATION", round_level=2000.0,
                                round_direction="DOWN", close=1995.0)
        assert _vote_round_numbers(comp, "BUY") == -1

    def test_acceleration_no_direction_returns_zero(self):
        comp = _make_components(round_state="ACCELERATION", round_level=2000.0,
                                round_direction=None, close=2005.0)
        assert _vote_round_numbers(comp, "BUY") == 0

    def test_approaching_state_returns_zero(self):
        # APPROACHING is not acted on (no confirmed direction yet)
        comp = _make_components(round_state="APPROACHING", round_level=2000.0, close=2001.0)
        assert _vote_round_numbers(comp, "BUY") == 0

    def test_no_round_context_returns_zero(self):
        assert _vote_round_numbers({"close": 2000.0}, "BUY") == 0


# ---------------------------------------------------------------------------
# build_context: ACCELERATION propagated through + dxy_direction computed
# ---------------------------------------------------------------------------

from src.engine.signal_context import build_context


def _make_round_hit(state: str, direction: str, level: float, ts: datetime) -> dict:
    return {
        "ts_utc": ts,
        "level": level,
        "state": state,
        "direction": direction,
        "multiplier": 1,
        "weight": 1.0,
    }


def _make_dxy_candle(ts: datetime, close: float) -> dict:
    return {"ts_utc": ts, "close": close}


class TestBuildContextExtensions:
    def test_acceleration_hit_reaches_ctx_round(self):
        ts = _ts(10)
        hit = _make_round_hit("ACCELERATION", "UP", 2000.0, _ts(5))
        ctx = build_context(
            regime_snaps=[], round_hits=[hit], fib_zones=[], pattern_rows=[],
            open_gaps=[], corr_rows=[], close_price=2005.0, atr_value=3.0,
            as_of_ts=ts,
        )
        assert ctx["round"] is not None
        assert ctx["round"]["state"] == "ACCELERATION"
        assert ctx["round"]["direction"] == "UP"

    def test_reversal_still_has_direction_field(self):
        ts = _ts(10)
        hit = _make_round_hit("REVERSAL", "DOWN", 2000.0, _ts(5))
        ctx = build_context(
            regime_snaps=[], round_hits=[hit], fib_zones=[], pattern_rows=[],
            open_gaps=[], corr_rows=[], close_price=1999.0, atr_value=3.0,
            as_of_ts=ts,
        )
        assert ctx["round"] is not None
        assert ctx["round"]["state"] == "REVERSAL"
        assert "direction" in ctx["round"]

    def test_dxy_direction_up_when_current_gt_prev(self):
        ts = _ts(10)
        # 4 DXY candles at ts 7,8,9,10 — all before as_of_ts
        dxy = [
            _make_dxy_candle(_ts(7), 104.0),
            _make_dxy_candle(_ts(8), 104.2),
            _make_dxy_candle(_ts(9), 104.5),
            _make_dxy_candle(_ts(10), 105.0),  # now > 3 bars ago (104.0)
        ]
        ctx = build_context(
            regime_snaps=[], round_hits=[], fib_zones=[], pattern_rows=[],
            open_gaps=[], corr_rows=[], close_price=2000.0, atr_value=3.0,
            as_of_ts=ts, dxy_candles=dxy, dxy_direction_bars=3,
        )
        assert ctx["dxy_direction"] == "up"

    def test_dxy_direction_down_when_current_lt_prev(self):
        ts = _ts(10)
        dxy = [
            _make_dxy_candle(_ts(7), 105.0),
            _make_dxy_candle(_ts(8), 104.8),
            _make_dxy_candle(_ts(9), 104.5),
            _make_dxy_candle(_ts(10), 104.0),
        ]
        ctx = build_context(
            regime_snaps=[], round_hits=[], fib_zones=[], pattern_rows=[],
            open_gaps=[], corr_rows=[], close_price=2000.0, atr_value=3.0,
            as_of_ts=ts, dxy_candles=dxy, dxy_direction_bars=3,
        )
        assert ctx["dxy_direction"] == "down"

    def test_dxy_direction_none_when_insufficient_bars(self):
        ts = _ts(10)
        dxy = [_make_dxy_candle(_ts(8), 104.0), _make_dxy_candle(_ts(9), 104.5)]
        ctx = build_context(
            regime_snaps=[], round_hits=[], fib_zones=[], pattern_rows=[],
            open_gaps=[], corr_rows=[], close_price=2000.0, atr_value=3.0,
            as_of_ts=ts, dxy_candles=dxy, dxy_direction_bars=3,
        )
        assert ctx["dxy_direction"] is None

    def test_dxy_direction_none_when_no_candles(self):
        ts = _ts(10)
        ctx = build_context(
            regime_snaps=[], round_hits=[], fib_zones=[], pattern_rows=[],
            open_gaps=[], corr_rows=[], close_price=2000.0, atr_value=3.0,
            as_of_ts=ts,
        )
        assert ctx["dxy_direction"] is None

    def test_dxy_future_candles_excluded(self):
        ts = _ts(5)  # as_of_ts is bar 5
        # candles at bars 3,4,5,6,7 — bars 6 and 7 are future
        dxy = [
            _make_dxy_candle(_ts(3), 104.0),
            _make_dxy_candle(_ts(4), 104.5),
            _make_dxy_candle(_ts(5), 105.0),  # valid (at as_of_ts)
            _make_dxy_candle(_ts(6), 103.0),  # future — must be excluded
            _make_dxy_candle(_ts(7), 102.0),  # future — must be excluded
        ]
        ctx = build_context(
            regime_snaps=[], round_hits=[], fib_zones=[], pattern_rows=[],
            open_gaps=[], corr_rows=[], close_price=2000.0, atr_value=3.0,
            as_of_ts=ts, dxy_candles=dxy, dxy_direction_bars=2,
        )
        # Only 3 valid candles (ts 3,4,5); dxy_direction_bars=2 -> 3 needed
        # dxy_now=105.0, dxy_prev (2 bars back) = 104.0 -> up
        assert ctx["dxy_direction"] == "up"
