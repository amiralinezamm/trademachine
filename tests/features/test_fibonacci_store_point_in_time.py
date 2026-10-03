"""Anti-lookahead proof for fetch_active_levels_for_fib (2026-09-30 fix).

Integration test against the real schema (this project has no DB-fixture
convention -- see conftest.py -- so this opens a real connection like the
live code does, inserts throwaway rows under a symbol no real data ever
uses, and rolls back instead of committing so nothing is left behind).

Scenario: a level created long ago was ACTIVE back in 2024, and has since
EXPIRED as of today (levels_history has a later 'expired' event). Reading
it with as_of_ts=2024 must return 'active' (what was true then), never
the level's current, future-relative 'expired' status.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.features.levels_store import get_connection
from src.features.fibonacci_store import fetch_active_levels_for_fib

UTC = timezone.utc
TEST_SYMBOL = "TESTFIBPIT@"  # never used by real ingestion -- safe sandbox
TF = "M5"


@pytest.fixture
def conn():
    c = get_connection()
    try:
        yield c
    finally:
        c.rollback()  # never persist anything this test inserts
        c.close()


def _insert_level(conn, level_id: int, created_ts: datetime,
                   status: str = "active") -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO levels
                (id, symbol, tf_origin, kind, price_low, price_high,
                 created_ts, status, atr_at_birth)
            VALUES (%s, %s, %s, 'support', 2000.0, 2001.0, %s, %s, 1.0)
            """,
            (level_id, TEST_SYMBOL, TF, created_ts, status),
        )


def _insert_history(conn, level_id: int, ts_utc: datetime, status: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO levels_history
                (level_id, ts_utc, strength, status, touch_count, break_count)
            VALUES (%s, %s, 1.0, %s, 0, 0)
            """,
            (level_id, ts_utc, status),
        )


def test_expired_today_was_active_back_then(conn):
    level_id = 9_990_001
    created = datetime(2024, 2, 20, tzinfo=UTC)
    _insert_level(conn, level_id, created, status="expired")  # CURRENT status
    _insert_history(conn, level_id, datetime(2024, 2, 20, 0, 5, tzinfo=UTC), "active")
    _insert_history(conn, level_id, datetime(2026, 6, 1, tzinfo=UTC), "expired")

    # Point-in-time query for a date shortly after creation, long before expiry.
    as_of = datetime(2024, 3, 1, tzinfo=UTC)
    rows = fetch_active_levels_for_fib(conn, TEST_SYMBOL, TF, as_of)

    assert len(rows) == 1, "an active-at-the-time level must not be excluded"
    assert rows[0]["status"] == "active"


def test_currently_active_is_excluded_before_its_creation(conn):
    level_id = 9_990_002
    created = datetime(2026, 1, 1, tzinfo=UTC)
    _insert_level(conn, level_id, created, status="active")

    as_of = datetime(2024, 6, 1, tzinfo=UTC)  # before the level even existed
    rows = fetch_active_levels_for_fib(conn, TEST_SYMBOL, TF, as_of)

    assert rows == [], "created_ts <= as_of_ts must still be enforced"


def test_no_history_entry_yet_defaults_to_active(conn):
    level_id = 9_990_003
    created = datetime(2024, 5, 1, tzinfo=UTC)
    # levels.status already flipped to something else since, but no
    # levels_history row exists before the as_of_ts we query at all.
    _insert_level(conn, level_id, created, status="flipped")

    as_of = datetime(2024, 5, 2, tzinfo=UTC)  # right after creation, untouched
    rows = fetch_active_levels_for_fib(conn, TEST_SYMBOL, TF, as_of)

    assert len(rows) == 1
    assert rows[0]["status"] == "active"
