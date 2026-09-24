"""DB-layer tests for the proposed RSI + MACD module (src/features/rsi_store.py)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.features.rsi_store import (
    get_connection,
    upsert_rsi_snapshots,
    upsert_divergence_events,
    fetch_latest_snapshot,
    fetch_recent_divergences,
    register_proposed_rules,
)

UTC = timezone.utc
SYMBOL = "__TEST_RSI__"


@pytest.fixture
def db_conn():
    conn = get_connection()
    yield conn
    conn.rollback()
    conn.close()


def _snapshot(ts, **overrides):
    base = {
        "symbol": SYMBOL, "tf": "M5", "ts_utc": ts,
        "rsi": 55.0, "rsi_state": "neutral",
        "macd": 1.2, "macd_signal": 1.0, "macd_hist": 0.2,
    }
    base.update(overrides)
    return base


def _event(ts1, ts2, ts_confirmed, **overrides):
    base = {
        "symbol": SYMBOL, "tf": "M5", "kind": "price_rsi", "direction": "bearish",
        "swing1_ts": ts1, "swing2_ts": ts2,
        "swing1_price": 4230.0, "swing2_price": 4240.0,
        "swing1_indicator": 88.0, "swing2_indicator": 80.0,
        "confirmed_ts": ts_confirmed,
    }
    base.update(overrides)
    return base


def test_upsert_rsi_snapshots_is_idempotent(db_conn):
    ts = datetime(2099, 1, 1, tzinfo=UTC)
    row = _snapshot(ts)
    r1 = upsert_rsi_snapshots(db_conn, [row])
    r2 = upsert_rsi_snapshots(db_conn, [row])
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM rsi_snapshots WHERE symbol=%s AND ts_utc=%s", (SYMBOL, ts))
        (count,) = cur.fetchone()
    assert r1["upserted"] == 1
    assert r2["upserted"] == 1  # ON CONFLICT DO UPDATE still reports a row touched
    assert count == 1  # never duplicated


def test_upsert_rsi_snapshots_updates_value_on_recompute(db_conn):
    ts = datetime(2099, 1, 2, tzinfo=UTC)
    upsert_rsi_snapshots(db_conn, [_snapshot(ts, rsi=55.0, rsi_state="neutral")])
    upsert_rsi_snapshots(db_conn, [_snapshot(ts, rsi=75.0, rsi_state="overbought")])
    latest = fetch_latest_snapshot(db_conn, SYMBOL, "M5", ts)
    assert float(latest["rsi"]) == pytest.approx(75.0)
    assert latest["rsi_state"] == "overbought"


def test_fetch_latest_snapshot_respects_as_of_ts(db_conn):
    older = datetime(2099, 1, 3, tzinfo=UTC)
    newer = datetime(2099, 1, 4, tzinfo=UTC)
    upsert_rsi_snapshots(db_conn, [_snapshot(older, rsi=40.0), _snapshot(newer, rsi=90.0)])
    latest_as_of_older = fetch_latest_snapshot(db_conn, SYMBOL, "M5", older)
    assert float(latest_as_of_older["rsi"]) == pytest.approx(40.0), "must not see the future row"


def test_upsert_divergence_events_is_idempotent(db_conn):
    ts1 = datetime(2099, 1, 5, tzinfo=UTC)
    ts2 = datetime(2099, 1, 5, 1, tzinfo=UTC)
    confirmed = datetime(2099, 1, 5, 1, 15, tzinfo=UTC)
    ev = _event(ts1, ts2, confirmed)
    r1 = upsert_divergence_events(db_conn, [ev])
    r2 = upsert_divergence_events(db_conn, [ev])
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM divergence_events WHERE symbol=%s AND swing1_ts=%s AND swing2_ts=%s",
            (SYMBOL, ts1, ts2),
        )
        (count,) = cur.fetchone()
    assert r1 == {"inserted": 1, "skipped": 0}
    assert r2 == {"inserted": 0, "skipped": 1}  # DO NOTHING on repeat
    assert count == 1


def test_fetch_recent_divergences_filters_by_kind(db_conn):
    ts1 = datetime(2099, 1, 6, tzinfo=UTC)
    ts2 = datetime(2099, 1, 6, 1, tzinfo=UTC)
    confirmed = datetime(2099, 1, 6, 1, 15, tzinfo=UTC)
    upsert_divergence_events(db_conn, [
        _event(ts1, ts2, confirmed, kind="price_rsi"),
        _event(ts1, ts2, confirmed, kind="price_macd"),
    ])
    only_macd = fetch_recent_divergences(db_conn, SYMBOL, "M5", confirmed, kind="price_macd")
    assert all(e["kind"] == "price_macd" for e in only_macd)
    assert len(only_macd) >= 1


def test_register_proposed_rules_is_idempotent_and_does_not_clobber(db_conn):
    rsi_params = {"period": 14, "overbought": 70, "oversold": 30}
    macd_params = {"fast_period": 12, "slow_period": 26, "signal_period": 9, "divergence_source": "macd_line"}
    divergence_params = {"swing_n": 5, "lookback_bars": 100}

    register_proposed_rules(db_conn, rsi_params, macd_params, divergence_params)
    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM rules WHERE id = 'rsi_overbought_oversold'")
        (status,) = cur.fetchone()
    assert status == "proposed"

    # Simulate the rule having been promoted by a human/backtest since; a
    # second registration call must NOT reset it back to 'proposed'.
    with db_conn.cursor() as cur:
        cur.execute("UPDATE rules SET status = 'testing' WHERE id = 'rsi_overbought_oversold'")
    register_proposed_rules(db_conn, rsi_params, macd_params, divergence_params)
    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM rules WHERE id = 'rsi_overbought_oversold'")
        (status,) = cur.fetchone()
    assert status == "testing", "re-registering must not clobber a status already advanced"
