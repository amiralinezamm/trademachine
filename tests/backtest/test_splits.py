"""Tests for src/backtest/splits.py and holdout guard in replay.py."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.backtest.splits import HoldoutViolation, WFSplit, walk_forward_splits


def _utc(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def _make_cursor_ctx(mock_cur: MagicMock) -> MagicMock:
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=mock_cur)
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx


# ---------------------------------------------------------------------------
# walk_forward_splits — pure logic
# ---------------------------------------------------------------------------

def test_splits_correct_count():
    tf_min = 5
    from_ts = _utc("2024-01-01T00:00:00")
    to_ts = from_ts + timedelta(minutes=tf_min * (100 + 10 + 20 + 20 + 10 + 20))
    splits = list(walk_forward_splits(
        from_ts=from_ts, to_ts=to_ts,
        train_candles=100, test_candles=20,
        embargo_candles=10, purge_candles=5,
    ))
    assert len(splits) >= 2


def test_splits_no_overlap_between_train_and_test():
    from_ts = _utc("2024-01-01T00:00:00")
    to_ts = from_ts + timedelta(days=30)
    for s in walk_forward_splits(
        from_ts=from_ts, to_ts=to_ts,
        train_candles=500, test_candles=100,
        embargo_candles=60, purge_candles=5,
    ):
        assert s.train_end < s.test_start


def test_splits_embargo_gap_size():
    from_ts = _utc("2024-01-01T00:00:00")
    to_ts = from_ts + timedelta(days=60)
    embargo_candles, purge_candles, tf_min = 60, 5, 5
    splits = list(walk_forward_splits(
        from_ts=from_ts, to_ts=to_ts,
        train_candles=1000, test_candles=100,
        embargo_candles=embargo_candles, purge_candles=purge_candles,
    ))
    assert splits
    s = splits[0]
    raw_boundary = s.train_end + timedelta(minutes=tf_min * purge_candles)
    embargo_actual = int((s.test_start - raw_boundary).total_seconds() / 60 / tf_min)
    assert embargo_actual == embargo_candles


def test_splits_folds_sequential():
    from_ts = _utc("2024-01-01T00:00:00")
    to_ts = from_ts + timedelta(days=90)
    splits = list(walk_forward_splits(
        from_ts=from_ts, to_ts=to_ts,
        train_candles=1000, test_candles=200,
        embargo_candles=60, purge_candles=5,
    ))
    for i in range(1, len(splits)):
        assert splits[i].test_start > splits[i - 1].test_start
        assert splits[i].fold == i


# ---------------------------------------------------------------------------
# Test A: get_holdout_start() never calls percentile_disc on cache hit
# ---------------------------------------------------------------------------

def test_holdout_start_stable_after_first_compute():
    fixed_ts = _utc("2026-02-18T07:05:00")

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value = _make_cursor_ctx(mock_cur)
    # Cache hit: first fetchone returns a stored value
    mock_cur.fetchone.return_value = (fixed_ts.isoformat(),)

    from src.backtest.splits import get_holdout_start
    result = get_holdout_start(_conn=mock_conn)

    assert result == fixed_ts
    for c in mock_cur.execute.call_args_list:
        assert "percentile_disc" not in str(c), "percentile_disc called on cache hit"


# ---------------------------------------------------------------------------
# Test B: run_holdout_validation raises HoldoutAlreadyTested on second call
# ---------------------------------------------------------------------------

def test_holdout_validation_raises_on_second_call():
    from src.backtest.walk_forward import HoldoutAlreadyTested, run_holdout_validation

    holdout_ts = _utc("2026-02-18T07:05:00")
    fake_result = {"signals": 5, "winrate": 0.6, "expectancy": 12.0}

    call_n = {"v": 0}

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.__enter__ = lambda s: s
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value = _make_cursor_ctx(mock_cur)

    def _execute(sql, params=None):
        if "UPDATE rules SET holdout_tested_at" in sql:
            call_n["v"] += 1
            mock_cur.fetchone.return_value = (
                ("rsi_overbought_oversold",) if call_n["v"] == 1 else None
            )

    mock_cur.execute.side_effect = _execute

    with (
        patch("src.backtest.replay.run_backtest", return_value=fake_result),
        patch("src.backtest.walk_forward.get_holdout_start", return_value=holdout_ts),
        patch("src.features.levels_store.get_connection", return_value=mock_conn),
    ):
        result = run_holdout_validation("rsi_overbought_oversold")
        assert result["signals"] == 5

        with pytest.raises(HoldoutAlreadyTested):
            run_holdout_validation("rsi_overbought_oversold")


# ---------------------------------------------------------------------------
# Test C: run_backtest raises HoldoutViolation when to_ts > holdout_start
# ---------------------------------------------------------------------------

def test_run_backtest_raises_holdout_violation():
    holdout_ts = _utc("2026-02-18T07:05:00")
    future_ts  = holdout_ts + timedelta(days=10)

    # get_holdout_start is imported lazily inside run_backtest from src.backtest.splits
    with patch("src.backtest.splits.get_holdout_start", return_value=holdout_ts):
        from src.backtest.replay import run_backtest

        with pytest.raises(HoldoutViolation):
            run_backtest(
                from_ts=_utc("2025-01-01T00:00:00"),
                to_ts=future_ts,
                holdout_mode=False,
            )
