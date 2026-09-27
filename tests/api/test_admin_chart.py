"""PHASE3-PANEL-PLAN.md §7 قدم ۱ — tests for /admin/api/chart/* endpoints.

Acceptance criteria:
  ✓ Range over TF limit → 400
  ✓ tf=XYZ → 400
  ✓ signals returns raw components with bullish/bearish structure
  ✓ Each endpoint responds on a real data range without 500
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, "/opt/xauusd-bot")

from src.api.main import app

client = TestClient(app)

# Real data range from step-0 inventory: signals exist 2023-09-15 → 2026-09-21
# Use a 1-day window inside that range for smoke tests
_DAY = 86_400
_FROM = int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp())
_TO   = _FROM + _DAY          # 1 day — well within all TF limits
_FROM_M5 = _FROM
_TO_M5   = _FROM + 30 * _DAY  # 30 days for M5 (limit = 90 days)


# ---------------------------------------------------------------------------
# Allowlist / range validation
# ---------------------------------------------------------------------------

class TestValidation:

    def test_candles_invalid_tf_rejected(self):
        r = client.get("/admin/api/chart/candles", params={"tf": "XYZ", "from": _FROM, "to": _TO})
        assert r.status_code == 400, r.text
        assert "tf" in r.json()["detail"].lower()

    def test_candles_m1_over_limit_rejected(self):
        # M1 limit = 7 days; send 8 days
        over = _FROM + 8 * _DAY
        r = client.get("/admin/api/chart/candles",
                       params={"tf": "M1", "from": _FROM, "to": over})
        assert r.status_code == 400, r.text
        assert "limit" in r.json()["detail"].lower() or "exceed" in r.json()["detail"].lower()

    def test_candles_m5_over_limit_rejected(self):
        # M5 limit = 90 days; send 91 days
        over = _FROM + 91 * _DAY
        r = client.get("/admin/api/chart/candles",
                       params={"tf": "M5", "from": _FROM, "to": over})
        assert r.status_code == 400, r.text

    def test_candles_m15_over_limit_rejected(self):
        over = _FROM + 181 * _DAY
        r = client.get("/admin/api/chart/candles",
                       params={"tf": "M15", "from": _FROM, "to": over})
        assert r.status_code == 400, r.text

    def test_candles_h1_over_limit_rejected(self):
        over = _FROM + 731 * _DAY
        r = client.get("/admin/api/chart/candles",
                       params={"tf": "H1", "from": _FROM, "to": over})
        assert r.status_code == 400, r.text

    def test_levels_invalid_status_rejected(self):
        r = client.get("/admin/api/chart/levels",
                       params={"from": _FROM, "to": _TO, "status": "unknown"})
        assert r.status_code == 400, r.text

    def test_patterns_invalid_tf_rejected(self):
        r = client.get("/admin/api/chart/patterns",
                       params={"tf": "D1", "from": _FROM, "to": _TO})
        assert r.status_code == 400, r.text

    def test_rounds_inverted_range_rejected(self):
        r = client.get("/admin/api/chart/rounds",
                       params={"price_min": 5000, "price_max": 4000})
        assert r.status_code == 400, r.text


# ---------------------------------------------------------------------------
# Smoke tests — real data, no 500, correct shape
# ---------------------------------------------------------------------------

class TestSmoke:

    def test_candles_returns_list(self):
        r = client.get("/admin/api/chart/candles",
                       params={"tf": "M5", "from": _FROM_M5, "to": _TO_M5})
        assert r.status_code == 200, r.text
        data = r.json()
        assert "candles" in data
        assert "count" in data
        assert data["count"] == len(data["candles"])
        if data["candles"]:
            c = data["candles"][0]
            assert all(k in c for k in ("t", "o", "h", "l", "c"))
            assert isinstance(c["t"], int)  # epoch seconds

    def test_levels_returns_list(self):
        r = client.get("/admin/api/chart/levels",
                       params={"from": _FROM_M5, "to": _TO_M5,
                                "status": "active,broken,flipped,expired"})
        assert r.status_code == 200, r.text
        data = r.json()
        assert "levels" in data
        if data["levels"]:
            lv = data["levels"][0]
            assert all(k in lv for k in ("id", "kind", "lo", "hi", "status", "touches"))

    def test_signals_returns_raw_components(self):
        """Components must be the raw JSONB with actual bullish/bearish structure."""
        # Use a wider window to ensure we capture signals
        wide_from = int(datetime(2026, 9, 15, tzinfo=timezone.utc).timestamp())
        wide_to   = int(datetime(2026, 9, 21, tzinfo=timezone.utc).timestamp())
        r = client.get("/admin/api/chart/signals",
                       params={"from": wide_from, "to": wide_to})
        assert r.status_code == 200, r.text
        data = r.json()
        assert "signals" in data
        # Should have signals in this window
        assert len(data["signals"]) > 0, "Expected signals in 2026-09-15 → 2026-09-21"
        sig = data["signals"][0]
        assert "components" in sig
        comp = sig["components"]
        # components must NOT be None
        assert comp is not None, "components should be present"
        # Must have core keys that exist in real data (from step-0 inventory)
        assert "level_id" in comp, f"Missing level_id in components: {comp.keys()}"
        assert "atr" in comp, f"Missing atr in components: {comp.keys()}"
        # pattern in real data is either None or a dict with bullish/bearish keys
        if comp.get("pattern") is not None:
            pat = comp["pattern"]
            assert isinstance(pat, dict)
            assert "bullish" in pat or "bearish" in pat, (
                f"Expected bullish/bearish keys in pattern, got: {pat}"
            )

    def test_patterns_returns_list(self):
        r = client.get("/admin/api/chart/patterns",
                       params={"tf": "M5", "from": _FROM_M5, "to": _TO_M5,
                                "min_body_atr": 0.0})
        assert r.status_code == 200, r.text
        data = r.json()
        assert "hits" in data

    def test_gaps_returns_list(self):
        r = client.get("/admin/api/chart/gaps",
                       params={"from": _FROM_M5, "to": _TO_M5})
        assert r.status_code == 200, r.text
        data = r.json()
        assert "gaps" in data

    def test_news_windows_returns_list(self):
        r = client.get("/admin/api/chart/news-windows",
                       params={"from": _FROM_M5, "to": _TO_M5})
        assert r.status_code == 200, r.text
        data = r.json()
        assert "windows" in data

    def test_regime_returns_without_500(self):
        """regime_snapshots now has data; endpoint must return 200 with segments list."""
        r = client.get("/admin/api/chart/regime",
                       params={"from": _FROM_M5, "to": _TO_M5})
        assert r.status_code == 200, r.text
        data = r.json()
        assert "segments" in data
        assert isinstance(data["segments"], list)
        if data["segments"]:
            seg = data["segments"][0]
            assert all(k in seg for k in ("from", "to", "regime"))

    def test_rounds_returns_list(self):
        r = client.get("/admin/api/chart/rounds",
                       params={"price_min": 4200, "price_max": 4400})
        assert r.status_code == 200, r.text
        data = r.json()
        assert "levels" in data
        assert len(data["levels"]) > 0, "Expected round numbers between 4200–4400"
        lv = data["levels"][0]
        assert "price" in lv and "multiple" in lv

    def test_fib_returns_without_500(self):
        """fibonacci_zones now has data; endpoint must return 200 with levels list."""
        r = client.get("/admin/api/chart/fib",
                       params={"from": _FROM_M5, "to": _TO_M5})
        assert r.status_code == 200, r.text
        data = r.json()
        assert "levels" in data
        assert isinstance(data["levels"], list)
        if data["levels"]:
            lv = data["levels"][0]
            assert all(k in lv for k in ("id", "price", "role"))

    def test_rules_returns_all_rows(self):
        r = client.get("/admin/api/chart/rules")
        assert r.status_code == 200, r.text
        data = r.json()
        assert "rules" in data
        # step-0: 8 rows in rules table
        assert len(data["rules"]) == 8, f"Expected 8 rules, got {len(data['rules'])}"
        rule = data["rules"][0]
        assert all(k in rule for k in ("id", "status", "statement", "weight"))

    def test_status_returns_required_keys(self):
        r = client.get("/admin/api/status")
        assert r.status_code == 200, r.text
        data = r.json()
        assert all(k in data for k in ("data", "quality", "last_signal", "news_block", "emergency_stop"))
        assert "counts" in data["data"]
        assert "lag_minutes" in data["data"]
        counts = data["data"]["counts"]
        assert all(tf in counts for tf in ("M1", "M5", "M15", "H1"))
        # M5 count from step-0 inventory: ~212k rows
        assert counts["M5"] > 200_000, f"Unexpected M5 count: {counts['M5']}"
