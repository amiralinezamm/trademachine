"""Pure (fake-cursor) tests: every live read of `signals` filters on the live
rule_version, so rows replayed by the backtest (rule_version + "_bt") can
never be mistaken for live signals (server finding, 2026-09-29)."""
from datetime import datetime, timezone

from src.engine.level_reversion import RULE_ID
from src.engine.signal_store import (
    LIVE_RULE_VERSION,
    fetch_last_signal_for_direction,
    fetch_latest_signal,
    fetch_open_signals,
)


class _Cur:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params):
        self.log.append((sql, params))

    def fetchone(self):
        return None

    def fetchall(self):
        return []


class _Conn:
    def __init__(self):
        self.log = []

    def cursor(self):
        return _Cur(self.log)


def _assert_filtered(conn, expected):
    sql, params = conn.log[-1]
    assert "rule_version = %s" in sql
    assert params[-1] == expected


def test_live_rule_version_is_the_rule_id():
    assert LIVE_RULE_VERSION == RULE_ID


def test_default_reads_filter_on_live_rule_version():
    conn = _Conn()
    fetch_latest_signal(conn, "XAUUSD@", "M5")
    _assert_filtered(conn, RULE_ID)
    fetch_last_signal_for_direction(conn, "BUY", datetime(2026, 9, 29, tzinfo=timezone.utc))
    _assert_filtered(conn, RULE_ID)
    fetch_open_signals(conn, "XAUUSD@", "M5")
    _assert_filtered(conn, RULE_ID)


def test_backtest_rule_version_is_never_the_live_one():
    from src.backtest.replay import load_costs
    suffix = load_costs()["backtest"]["rule_version_suffix"]
    assert suffix, "backtest writes must carry a suffix so live reads skip them"
    assert RULE_ID + suffix != LIVE_RULE_VERSION
