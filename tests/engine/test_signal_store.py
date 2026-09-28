from datetime import datetime, timezone

import pytest

from src.features.levels_store import get_connection
from src.engine.signal_store import fetch_latest_signal, insert_signal

UTC = timezone.utc


def _signal(ts_utc, **overrides):
    base = {
        "ts_utc": ts_utc,
        "direction": "BUY",
        "entry": 2350.0,
        "stop_loss": 2340.0,
        "take_profit": 2365.0,
        "confidence": None,
        "rule_version": "__TEST_RULE__",
        "components": {"symbol": "__TEST_SIGNALS__", "tf": "M5"},
    }
    base.update(overrides)
    return base


@pytest.fixture
def db_conn():
    conn = get_connection()
    yield conn
    conn.rollback()
    conn.close()


def test_fetch_latest_signal_returns_none_when_nothing_stored(db_conn):
    assert fetch_latest_signal(db_conn, "__TEST_SIGNALS_EMPTY__", "M5") is None


def test_fetch_latest_signal_returns_most_recent_by_ts(db_conn):
    older = _signal(datetime(2099, 1, 1, tzinfo=UTC), direction="BUY")
    newer = _signal(datetime(2099, 1, 2, tzinfo=UTC), direction="SELL")
    insert_signal(db_conn, older)
    insert_signal(db_conn, newer)

    result = fetch_latest_signal(db_conn, "__TEST_SIGNALS__", "M5")

    assert result is not None
    assert result["direction"] == "SELL"
    assert result["ts_utc"] == datetime(2099, 1, 2, tzinfo=UTC)


def test_fetch_latest_signal_ignores_other_symbol_tf(db_conn):
    sig = _signal(
        datetime(2099, 1, 3, tzinfo=UTC),
        components={"symbol": "__TEST_SIGNALS__", "tf": "H1"},
    )
    insert_signal(db_conn, sig)

    # stored under tf=H1 -- must not show up for tf=M5
    assert fetch_latest_signal(db_conn, "__TEST_SIGNALS__", "M5") is None
    result = fetch_latest_signal(db_conn, "__TEST_SIGNALS__", "H1")
    assert result is not None
    assert result["ts_utc"] == datetime(2099, 1, 3, tzinfo=UTC)


def test_fetch_latest_signal_survives_already_fired_duplicate(db_conn):
    """Regression: /status used to call /signal/latest, which returns
    signal=None (reason='already_fired') once a signal for the current
    candle is already stored -- so a second lookup right after n8n fired
    the signal showed nothing. fetch_latest_signal is a plain read and
    must keep returning the stored row regardless of a duplicate insert
    attempt on the same ts_utc/rule_version."""
    ts = datetime(2099, 1, 4, tzinfo=UTC)
    sig = _signal(ts)
    first_id = insert_signal(db_conn, sig)
    second_id = insert_signal(db_conn, sig)  # duplicate -- ON CONFLICT DO NOTHING

    assert first_id is not None
    assert second_id is None  # confirms the duplicate-insert behavior being tested

    result = fetch_latest_signal(db_conn, "__TEST_SIGNALS__", "M5")
    assert result is not None
    assert result["id"] == first_id
