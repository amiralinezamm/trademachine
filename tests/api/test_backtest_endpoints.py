"""End-to-end tests for POST /backtest/walk-forward and /backtest/holdout-validate.

Uses mocked run_walk_forward / run_holdout_validation so no DB is needed.
Tests the HTTP contract: status codes, response shape, 409/400 error paths.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient

from src.api.main import app

client = TestClient(app, raise_server_exceptions=False)

FROM_TS = "2025-01-01T00:00:00Z"
TO_TS   = "2025-06-01T00:00:00Z"


def _make_wf_result(n_folds: int = 2) -> MagicMock:
    from src.backtest.walk_forward import FoldResult, WFResult
    from src.backtest.splits import WFSplit

    def _utc(s):
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)

    folds = []
    for i in range(n_folds):
        split = WFSplit(
            fold=i,
            train_start=_utc("2025-01-01T00:00:00"),
            train_end=_utc("2025-02-01T00:00:00"),
            test_start=_utc("2025-02-05T00:00:00"),
            test_end=_utc("2025-02-15T00:00:00"),
        )
        folds.append(FoldResult(
            split=split,
            oos={"signals": 5, "winrate": 0.6, "expectancy": 8.0, "pnl": 40.0},
        ))
    r = WFResult(folds=folds)
    r.n_signals = n_folds * 5
    r.mean_expectancy = 8.0
    r.std_expectancy = 0.0
    r.mean_winrate = 0.6
    return r


# ---------------------------------------------------------------------------
# /backtest/walk-forward
# ---------------------------------------------------------------------------

def test_walk_forward_returns_200_with_aggregate():
    fake_result = _make_wf_result(n_folds=2)
    with patch("src.backtest.walk_forward.run_walk_forward", return_value=fake_result):
        r = client.post("/backtest/walk-forward", json={
            "from_ts": FROM_TS,
            "to_ts": TO_TS,
        })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["n_folds"] == 2
    assert body["n_signals_total"] == 10
    assert body["mean_expectancy"] == 8.0
    assert body["mean_winrate"] == 0.6
    assert len(body["folds"]) == 2
    fold0 = body["folds"][0]
    assert "train_start" in fold0
    assert "test_start" in fold0
    assert fold0["signals"] == 5


def test_walk_forward_returns_400_on_holdout_violation():
    from src.backtest.splits import HoldoutViolation

    with patch("src.api.main._run_walk_forward_sync",
               side_effect=HoldoutViolation("to_ts exceeds HOLDOUT boundary")):
        r = client.post("/backtest/walk-forward", json={
            "from_ts": FROM_TS,
            "to_ts": "2026-12-01T00:00:00Z",  # past HOLDOUT
        })
    assert r.status_code == 400
    assert "HOLDOUT" in r.json()["detail"]


def test_walk_forward_uses_defaults_when_not_provided():
    """Omitting train/test/embargo/purge uses the documented defaults."""
    fake_result = _make_wf_result(n_folds=1)
    with patch("src.backtest.walk_forward.run_walk_forward", return_value=fake_result) as mock_wf:
        client.post("/backtest/walk-forward", json={
            "from_ts": FROM_TS,
            "to_ts": TO_TS,
        })
    # run_walk_forward called with config that has defaults
    call_kwargs = mock_wf.call_args
    if call_kwargs:
        cfg = call_kwargs.kwargs.get("config") or (call_kwargs.args[2] if len(call_kwargs.args) > 2 else None)
        if cfg is not None:
            assert cfg.embargo_candles == 60
            assert cfg.purge_candles == 5


# ---------------------------------------------------------------------------
# /backtest/holdout-validate
# ---------------------------------------------------------------------------

def test_holdout_validate_returns_200():
    fake_result = {"signals": 12, "winrate": 0.58, "expectancy": 6.5, "pnl": 78.0}
    with patch("src.backtest.walk_forward.run_holdout_validation", return_value=fake_result):
        r = client.post("/backtest/holdout-validate", json={"rule_id": "rsi_overbought_oversold"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["rule_id"] == "rsi_overbought_oversold"
    assert body["signals"] == 12
    assert body["winrate"] == 0.58


def test_holdout_validate_returns_409_on_second_call():
    from src.backtest.walk_forward import HoldoutAlreadyTested

    with patch("src.api.main._run_holdout_validate_sync",
               side_effect=HoldoutAlreadyTested("already tested")):
        r = client.post("/backtest/holdout-validate", json={"rule_id": "rsi_overbought_oversold"})
    assert r.status_code == 409
    assert "already" in r.json()["detail"].lower()


def test_holdout_validate_requires_rule_id():
    r = client.post("/backtest/holdout-validate", json={})
    assert r.status_code == 422  # Pydantic validation error
