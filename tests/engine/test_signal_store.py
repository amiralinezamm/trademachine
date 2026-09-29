from datetime import datetime, timezone

import pytest

from src.features.levels_store import get_connection
from src.engine.signal_store import (
    fetch_candles_range,
    fetch_latest_signal,
    fetch_open_signals,
    insert_signal,
    mark_reversal_alert_sent,
    mark_signal_outcome,
)

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
    assert fetch_latest_signal(db_conn, "__TEST_SIGNALS_EMPTY__", "M5", rule_version="__TEST_RULE__") is None


def test_fetch_latest_signal_returns_most_recent_by_ts(db_conn):
    older = _signal(datetime(2099, 1, 1, tzinfo=UTC), direction="BUY")
    newer = _signal(datetime(2099, 1, 2, tzinfo=UTC), direction="SELL")
    insert_signal(db_conn, older)
    insert_signal(db_conn, newer)

    result = fetch_latest_signal(db_conn, "__TEST_SIGNALS__", "M5", rule_version="__TEST_RULE__")

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
    assert fetch_latest_signal(db_conn, "__TEST_SIGNALS__", "M5", rule_version="__TEST_RULE__") is None
    result = fetch_latest_signal(db_conn, "__TEST_SIGNALS__", "H1", rule_version="__TEST_RULE__")
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

    result = fetch_latest_signal(db_conn, "__TEST_SIGNALS__", "M5", rule_version="__TEST_RULE__")
    assert result is not None
    assert result["id"] == first_id


# ---------------------------------------------------------------------------
# fetch_open_signals / mark_signal_outcome / mark_reversal_alert_sent
# ("توقف اجباری" rule, 2026-09-29)
# ---------------------------------------------------------------------------

def test_fetch_open_signals_excludes_closed_ones(db_conn):
    closed = _signal(datetime(2099, 2, 1, tzinfo=UTC), direction="BUY")
    open_one = _signal(datetime(2099, 2, 2, tzinfo=UTC), direction="BUY")
    closed_id = insert_signal(db_conn, closed)
    insert_signal(db_conn, open_one)
    mark_signal_outcome(db_conn, closed_id, "tp")

    open_signals = fetch_open_signals(db_conn, "__TEST_SIGNALS__", "M5", rule_version="__TEST_RULE__")

    assert len(open_signals) == 1
    assert open_signals[0]["ts_utc"] == datetime(2099, 2, 2, tzinfo=UTC)
    assert open_signals[0]["reversal_alert_sent"] is False


def test_fetch_open_signals_returns_latest_open_per_direction(db_conn):
    buy1 = _signal(datetime(2099, 2, 3, tzinfo=UTC), direction="BUY")
    buy2 = _signal(datetime(2099, 2, 4, tzinfo=UTC), direction="BUY")
    sell1 = _signal(datetime(2099, 2, 3, 12, 30, tzinfo=UTC), direction="SELL")
    insert_signal(db_conn, buy1)
    insert_signal(db_conn, buy2)
    insert_signal(db_conn, sell1)

    open_signals = fetch_open_signals(db_conn, "__TEST_SIGNALS__", "M5", rule_version="__TEST_RULE__")
    by_dir = {s["direction"]: s for s in open_signals}

    assert by_dir["BUY"]["ts_utc"] == datetime(2099, 2, 4, tzinfo=UTC)  # latest BUY, not buy1
    assert by_dir["SELL"]["ts_utc"] == datetime(2099, 2, 3, 12, 30, tzinfo=UTC)


def test_mark_signal_outcome_is_idempotent_and_does_not_reopen(db_conn):
    ts = datetime(2099, 2, 5, tzinfo=UTC)
    sid = insert_signal(db_conn, _signal(ts))

    mark_signal_outcome(db_conn, sid, "sl")
    mark_signal_outcome(db_conn, sid, "tp")  # must NOT overwrite the first outcome

    with db_conn.cursor() as cur:
        cur.execute("SELECT outcome FROM signals WHERE id = %s", (sid,))
        (outcome,) = cur.fetchone()
    assert outcome == "sl"


def test_mark_reversal_alert_sent_flips_the_flag(db_conn):
    ts = datetime(2099, 2, 6, tzinfo=UTC)
    sid = insert_signal(db_conn, _signal(ts))

    mark_reversal_alert_sent(db_conn, sid)

    open_signals = fetch_open_signals(db_conn, "__TEST_SIGNALS__", "M5", rule_version="__TEST_RULE__")
    matching = [s for s in open_signals if s["id"] == sid]
    assert matching and matching[0]["reversal_alert_sent"] is True


def test_fetch_candles_range_is_ascending_and_bounded(db_conn):
    # candles table isn't seeded by this fixture set -- just verify the
    # bounded-range query shape returns [] cleanly rather than erroring.
    result = fetch_candles_range(
        db_conn, "__TEST_SIGNALS_NO_CANDLES__", "M5",
        datetime(2099, 3, 1, tzinfo=UTC), datetime(2099, 3, 2, tzinfo=UTC),
    )
    assert result == []
