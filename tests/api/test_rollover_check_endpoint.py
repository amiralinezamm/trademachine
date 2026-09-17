"""Tests for POST /instruments/rollover-check (docs/instrument_rollover.md).

The daily-check decision logic (src/api/main.py:_process_rollover_check_sync)
is tested here at the API layer since it orchestrates DB calls; the pure
front-month/offset math it delegates to (src/ingest/rollover.py) already
has its own unit tests in tests/ingest/test_rollover.py.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.api.main import app

CHECKED_AT = "2026-09-18T12:00:00Z"


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _payload(instruments: dict) -> dict:
    return {"checked_at": CHECKED_AT, "instruments": instruments}


def test_unknown_logical_symbol_is_rejected(client):
    with patch("src.api.main.get_connection", return_value=MagicMock()):
        resp = client.post(
            "/instruments/rollover-check",
            json=_payload({"NOTREAL@": {"candidates": [{"symbol": "X.U26", "trade_mode": 4, "volume_window": 100}]}}),
        )
    assert resp.status_code == 200
    assert resp.json()["results"]["NOTREAL@"]["status"] == "rejected"


def test_no_change_when_winner_matches_active_contract(client):
    with patch("src.api.main.get_connection", return_value=MagicMock()), \
         patch("src.api.main.fetch_active_contract", return_value={"physical_symbol": "USINDX.Z26"}), \
         patch("src.api.main.close_contract") as mock_close, \
         patch("src.api.main.upsert_instrument_contract") as mock_upsert, \
         patch("src.api.main.insert_rollover_alert") as mock_alert:
        resp = client.post(
            "/instruments/rollover-check",
            json=_payload({
                "DXY@": {"candidates": [
                    {"symbol": "USINDX.U26", "trade_mode": 0, "volume_window": 100},
                    {"symbol": "USINDX.Z26", "trade_mode": 4, "volume_window": 5000},
                ]}
            }),
        )
    assert resp.status_code == 200
    assert resp.json()["results"]["DXY@"] == {"status": "no_change", "physical_symbol": "USINDX.Z26"}
    mock_close.assert_not_called()
    mock_upsert.assert_not_called()
    mock_alert.assert_not_called()


def test_rollover_detected_computes_offset_when_old_candidate_present(client):
    """Old contract (USINDX.Z26) still appears in this snapshot with a
    latest_close -- offset must be new_close - old_close = 105.0 - 100.0 = 5.0."""
    with patch("src.api.main.get_connection", return_value=MagicMock()), \
         patch("src.api.main.fetch_active_contract", return_value={"physical_symbol": "USINDX.Z26"}), \
         patch("src.api.main.close_contract") as mock_close, \
         patch("src.api.main.upsert_instrument_contract") as mock_upsert, \
         patch("src.api.main.insert_rollover_alert") as mock_alert:
        resp = client.post(
            "/instruments/rollover-check",
            json=_payload({
                "DXY@": {"candidates": [
                    {"symbol": "USINDX.Z26", "trade_mode": 0, "volume_window": 100, "latest_close": 100.0},
                    {"symbol": "USINDX.H27", "trade_mode": 4, "volume_window": 9000, "latest_close": 105.0},
                ]}
            }),
        )
    assert resp.status_code == 200
    result = resp.json()["results"]["DXY@"]
    assert result["status"] == "ROLLOVER_DETECTED"
    assert result["old_physical_symbol"] == "USINDX.Z26"
    assert result["new_physical_symbol"] == "USINDX.H27"
    assert result["offset_computed"] is True
    assert result["adjustment_offset"] == 5.0
    mock_close.assert_called_once()
    mock_upsert.assert_called_once()
    mock_alert.assert_called_once()


def test_rollover_detected_without_offset_when_old_candidate_missing(client):
    """Old contract has vanished from the candidate snapshot entirely (e.g.
    delisted) -- offset cannot be computed reliably; must NOT guess a
    number, must flag offset_computed=False instead."""
    with patch("src.api.main.get_connection", return_value=MagicMock()), \
         patch("src.api.main.fetch_active_contract", return_value={"physical_symbol": "USINDX.Z26"}), \
         patch("src.api.main.close_contract"), \
         patch("src.api.main.upsert_instrument_contract"), \
         patch("src.api.main.insert_rollover_alert") as mock_alert:
        resp = client.post(
            "/instruments/rollover-check",
            json=_payload({
                "DXY@": {"candidates": [
                    {"symbol": "USINDX.H27", "trade_mode": 4, "volume_window": 9000, "latest_close": 105.0},
                ]}
            }),
        )
    assert resp.status_code == 200
    result = resp.json()["results"]["DXY@"]
    assert result["status"] == "ROLLOVER_DETECTED"
    assert result["offset_computed"] is False
    assert result["adjustment_offset"] == 0.0
    mock_alert.assert_called_once()


def test_no_qualifying_candidate_is_reported_not_guessed(client):
    with patch("src.api.main.get_connection", return_value=MagicMock()):
        resp = client.post(
            "/instruments/rollover-check",
            json=_payload({
                "DXY@": {"candidates": [
                    {"symbol": "USINDX.U26", "trade_mode": 0, "volume_window": 100},
                    {"symbol": "USINDX.Z26", "trade_mode": 0, "volume_window": 200},
                ]}
            }),
        )
    assert resp.status_code == 200
    assert resp.json()["results"]["DXY@"]["status"] == "no_qualifying_candidate"


def test_first_mapping_when_no_active_contract_exists(client):
    with patch("src.api.main.get_connection", return_value=MagicMock()), \
         patch("src.api.main.fetch_active_contract", return_value=None), \
         patch("src.api.main.upsert_instrument_contract") as mock_upsert:
        resp = client.post(
            "/instruments/rollover-check",
            json=_payload({
                "BRENT@": {"candidates": [
                    {"symbol": "UKBRENT.X26", "trade_mode": 4, "volume_window": 5000},
                ]}
            }),
        )
    assert resp.status_code == 200
    assert resp.json()["results"]["BRENT@"]["status"] == "first_mapping"
    mock_upsert.assert_called_once()


def test_rollover_alerts_endpoint_returns_list(client):
    with patch("src.api.main.get_connection", return_value=MagicMock()), \
         patch("src.api.main.fetch_rollover_alerts", return_value=[
             {"id": 1, "logical_symbol": "DXY@", "old_physical_symbol": "A", "new_physical_symbol": "B",
              "adjustment_offset": 1.0, "offset_computed": True, "detected_at": datetime.now(timezone.utc), "acknowledged": False}
         ]):
        resp = client.get("/instruments/rollover-alerts")
    assert resp.status_code == 200
    assert len(resp.json()["alerts"]) == 1
