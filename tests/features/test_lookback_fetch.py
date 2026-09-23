"""Regression tests for lookback_bars parameter in fetch_candles variants.

Tests run against the real DB. Skipped if connection unavailable.
"""
from __future__ import annotations

import pytest

SYMBOL = "XAUUSD@"
TF = "M5"


def _conn():
    try:
        from src.features.levels_store import get_connection
        return get_connection()
    except Exception:
        return None


@pytest.fixture(scope="module")
def conn():
    c = _conn()
    if c is None:
        pytest.skip("DB not available")
    yield c
    c.close()


def _as_of(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ts_utc FROM candles WHERE symbol=%s AND tf=%s ORDER BY ts_utc DESC LIMIT 1",
            (SYMBOL, TF),
        )
        row = cur.fetchone()
    if row is None:
        pytest.skip("no candles in DB")
    return row[0]


class TestLevelsStoreFetchCandles:
    def test_none_returns_full_history(self, conn):
        from src.features.levels_store import fetch_candles
        as_of = _as_of(conn)
        rows = fetch_candles(conn, SYMBOL, TF, as_of, lookback_bars=None)
        assert len(rows) > 1000, "full history should have >1000 M5 bars"

    def test_lookback_limits_count(self, conn):
        from src.features.levels_store import fetch_candles
        as_of = _as_of(conn)
        rows = fetch_candles(conn, SYMBOL, TF, as_of, lookback_bars=100)
        assert len(rows) <= 100

    def test_lookback_returns_most_recent(self, conn):
        from src.features.levels_store import fetch_candles
        as_of = _as_of(conn)
        full = fetch_candles(conn, SYMBOL, TF, as_of, lookback_bars=None)
        limited = fetch_candles(conn, SYMBOL, TF, as_of, lookback_bars=200)
        assert limited[-1]["ts_utc"] == full[-1]["ts_utc"], "last bar must be the same (most recent)"

    def test_lookback_result_ascending(self, conn):
        from src.features.levels_store import fetch_candles
        as_of = _as_of(conn)
        rows = fetch_candles(conn, SYMBOL, TF, as_of, lookback_bars=200)
        ts = [r["ts_utc"] for r in rows]
        assert ts == sorted(ts), "result must be in ascending ts_utc order"

    def test_none_identical_to_omitting(self, conn):
        """lookback_bars=None must return the same rows as before this change."""
        from src.features.levels_store import fetch_candles
        as_of = _as_of(conn)
        rows_none = fetch_candles(conn, SYMBOL, TF, as_of, lookback_bars=None)
        rows_default = fetch_candles(conn, SYMBOL, TF, as_of)
        assert len(rows_none) == len(rows_default)
        assert rows_none[0]["ts_utc"] == rows_default[0]["ts_utc"]
        assert rows_none[-1]["ts_utc"] == rows_default[-1]["ts_utc"]


class TestPatternsStoreFetchCandles:
    def test_lookback_limits_count(self, conn):
        from src.features.patterns_store import fetch_candles_for_patterns
        as_of = _as_of(conn)
        rows = fetch_candles_for_patterns(conn, SYMBOL, TF, as_of, lookback_bars=50)
        assert len(rows) <= 50

    def test_lookback_most_recent(self, conn):
        from src.features.patterns_store import fetch_candles_for_patterns
        from src.features.levels_store import fetch_candles as fetch_full
        as_of = _as_of(conn)
        full = fetch_full(conn, SYMBOL, TF, as_of)
        limited = fetch_candles_for_patterns(conn, SYMBOL, TF, as_of, lookback_bars=100)
        assert limited[-1]["ts_utc"] == full[-1]["ts_utc"]

    def test_none_returns_full_history(self, conn):
        from src.features.patterns_store import fetch_candles_for_patterns
        as_of = _as_of(conn)
        rows = fetch_candles_for_patterns(conn, SYMBOL, TF, as_of, lookback_bars=None)
        assert len(rows) > 1000


class TestGapsStoreFetchCandles:
    def test_lookback_limits_count(self, conn):
        from src.features.gaps_store import fetch_candles
        as_of = _as_of(conn)
        rows = fetch_candles(conn, SYMBOL, TF, as_of, lookback_bars=50)
        assert len(rows) <= 50

    def test_none_returns_full_history(self, conn):
        from src.features.gaps_store import fetch_candles
        as_of = _as_of(conn)
        rows = fetch_candles(conn, SYMBOL, TF, as_of, lookback_bars=None)
        assert len(rows) > 1000


class TestRoundNumbersStoreFetchCandles:
    def test_lookback_limits_count(self, conn):
        from src.features.round_numbers_store import fetch_candles_with_volume
        as_of = _as_of(conn)
        rows = fetch_candles_with_volume(conn, SYMBOL, TF, as_of, lookback_bars=50)
        assert len(rows) <= 50

    def test_none_returns_full_history(self, conn):
        from src.features.round_numbers_store import fetch_candles_with_volume
        as_of = _as_of(conn)
        rows = fetch_candles_with_volume(conn, SYMBOL, TF, as_of, lookback_bars=None)
        assert len(rows) > 1000
