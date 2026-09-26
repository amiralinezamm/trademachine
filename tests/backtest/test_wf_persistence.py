"""Test that run_walk_forward results are persisted to walk_forward_runs table."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest


def _utc(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def _mock_conn():
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    conn = MagicMock()
    conn.cursor.return_value = cur
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    return conn, cur


class _FakeOOS:
    def get(self, key, default=None):
        return {"signals": 2, "winrate": 0.6, "expectancy": 3.0, "pnl": 120.0}.get(key, default)


class _FakeSplit:
    fold = 1
    train_start = _utc("2025-10-01T00:00:00")
    train_end   = _utc("2025-11-05T00:00:00")
    test_start  = _utc("2025-11-06T00:00:00")
    test_end    = _utc("2025-11-13T00:00:00")


class _FakeFoldResult:
    split = _FakeSplit()
    oos   = _FakeOOS()


class _FakeWFResult:
    folds           = [_FakeFoldResult()]
    n_signals       = 2
    mean_expectancy = 3.0
    std_expectancy  = 0.5
    mean_winrate    = 0.6


def test_persistence_inserts_one_row():
    """After _run_walk_forward_sync, exactly one row is inserted into walk_forward_runs."""
    import src.backtest.walk_forward as wf_mod
    from src.api.main import WalkForwardRequest, _run_walk_forward_sync

    payload = WalkForwardRequest(
        from_ts=_utc("2025-10-01T00:00:00"),
        to_ts=_utc("2025-11-13T00:00:00"),
        train_candles=288,
        test_candles=96,
        embargo_candles=60,
        purge_candles=5,
        symbol="XAUUSD@",
        tf="M5",
    )

    fake_wf_result = _FakeWFResult()
    db_conn, db_cur = _mock_conn()

    orig_run = wf_mod.run_walk_forward
    wf_mod.run_walk_forward = lambda **kw: fake_wf_result
    inserted_rows = []

    def _fake_execute(sql, params=None):
        if "walk_forward_runs" in sql:
            inserted_rows.append(params)

    db_cur.execute.side_effect = _fake_execute

    try:
        with patch("src.features.levels_store.get_connection", return_value=db_conn):
            result = _run_walk_forward_sync(payload)
    finally:
        wf_mod.run_walk_forward = orig_run

    # The function returns the expected shape
    assert result["n_folds"] == 1
    assert result["n_signals_total"] == 2
    # One INSERT was executed
    assert len(inserted_rows) == 1, f"Expected 1 INSERT, got {len(inserted_rows)}"


def test_persistence_survives_db_failure():
    """If DB insert fails, _run_walk_forward_sync still returns the result (no exception)."""
    import src.backtest.walk_forward as wf_mod
    from src.api.main import WalkForwardRequest, _run_walk_forward_sync

    payload = WalkForwardRequest(
        from_ts=_utc("2025-10-01T00:00:00"),
        to_ts=_utc("2025-11-13T00:00:00"),
        train_candles=288,
        test_candles=96,
        symbol="XAUUSD@",
        tf="M5",
    )

    fake_wf_result = _FakeWFResult()
    orig_run = wf_mod.run_walk_forward
    wf_mod.run_walk_forward = lambda **kw: fake_wf_result
    try:
        # Simulate DB completely unavailable
        with patch("src.features.levels_store.get_connection",
                   side_effect=Exception("DB down")):
            result = _run_walk_forward_sync(payload)
    finally:
        wf_mod.run_walk_forward = orig_run

    assert result["n_folds"] == 1
    assert "folds" in result
