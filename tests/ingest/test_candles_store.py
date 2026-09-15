from datetime import datetime, timedelta, timezone

import pytest

from src.ingest.candles_store import filter_closed_candles, is_closed, upsert_candles
from src.ingest.load_candles import get_connection

UTC = timezone.utc


def test_is_closed_true_for_fully_elapsed_candle():
    ts = datetime(2026, 9, 15, 10, 0, tzinfo=UTC)
    now = datetime(2026, 9, 15, 10, 5, tzinfo=UTC)  # a full M5 bar has elapsed
    assert is_closed(ts, "M5", now) is True


def test_is_closed_false_for_still_forming_candle():
    """CLAUDE.md rule 3: a candle still in progress must never be stored."""
    ts = datetime(2026, 9, 15, 10, 0, tzinfo=UTC)
    now = datetime(2026, 9, 15, 10, 3, tzinfo=UTC)  # only 3 of 5 minutes elapsed
    assert is_closed(ts, "M5", now) is False


def test_filter_closed_candles_drops_only_the_unclosed_tail():
    now = datetime(2026, 9, 15, 10, 3, tzinfo=UTC)
    candles = [
        {"ts_utc": datetime(2026, 9, 15, 9, 50, tzinfo=UTC)},  # closed
        {"ts_utc": datetime(2026, 9, 15, 9, 55, tzinfo=UTC)},  # closed
        {"ts_utc": datetime(2026, 9, 15, 10, 0, tzinfo=UTC)},  # still forming
    ]
    result = filter_closed_candles(candles, "M5", now)
    assert len(result) == 2
    assert all(c["ts_utc"] < datetime(2026, 9, 15, 10, 0, tzinfo=UTC) for c in result)


@pytest.fixture
def db_conn():
    conn = get_connection()
    yield conn
    conn.rollback()
    conn.close()


def test_upsert_candles_is_idempotent(db_conn):
    candle = {
        "ts_utc": datetime(2099, 1, 1, 0, 0, tzinfo=UTC),
        "open": 4000.0, "high": 4005.0, "low": 3995.0, "close": 4002.0,
        "tick_volume": 100, "spread": 30, "real_volume": 0,
    }
    n1 = upsert_candles(db_conn, "__TEST_MARKER__", "M5", [candle])
    n2 = upsert_candles(db_conn, "__TEST_MARKER__", "M5", [candle])  # re-run, same data

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM candles WHERE symbol = %s AND tf = %s",
            ("__TEST_MARKER__", "M5"),
        )
        (count,) = cur.fetchone()

    assert n1 == 1
    assert n2 == 0  # ON CONFLICT DO NOTHING -> zero rows affected the second time
    assert count == 1  # never duplicated
