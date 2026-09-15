from datetime import datetime, timezone

import pytest

from src.features.levels_store import get_connection, upsert_levels

UTC = timezone.utc


def _level(created_ts, **overrides):
    base = {
        "symbol": "__TEST_LEVELS__",
        "tf_origin": "M5",
        "kind": "resistance",
        "price_low": 99.0,
        "price_high": 101.0,
        "created_ts": created_ts,
        "last_touch": None,
        "touch_count": 0,
        "break_count": 0,
        "strength": 0.0,
        "status": "active",
        "atr_at_birth": 1.5,
    }
    base.update(overrides)
    return base


@pytest.fixture
def db_conn():
    conn = get_connection()
    yield conn
    conn.rollback()
    conn.close()


def test_upsert_is_idempotent_by_natural_key(db_conn):
    created_ts = datetime(2099, 1, 1, tzinfo=UTC)
    lvl = _level(created_ts)

    r1 = upsert_levels(db_conn, [lvl])
    r2 = upsert_levels(db_conn, [lvl])  # identical re-run

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM levels WHERE symbol = %s AND created_ts = %s",
            ("__TEST_LEVELS__", created_ts),
        )
        (count,) = cur.fetchone()

    assert r1 == {"inserted": 1, "updated": 0}
    assert r2 == {"inserted": 0, "updated": 1}
    assert count == 1  # never duplicated


def test_upsert_updates_status_on_the_same_row_across_a_flip(db_conn):
    """A role flip changes `kind` but must stay the SAME row (same natural
    key: symbol, tf_origin, created_ts) — never a new one."""
    created_ts = datetime(2099, 1, 2, tzinfo=UTC)
    born = _level(created_ts, kind="resistance", status="active", strength=0.8)
    flipped = _level(created_ts, kind="support", status="flipped", strength=0.4, break_count=1)

    upsert_levels(db_conn, [born])
    upsert_levels(db_conn, [flipped])

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT kind, status, break_count FROM levels WHERE symbol = %s AND created_ts = %s",
            ("__TEST_LEVELS__", created_ts),
        )
        rows = cur.fetchall()

    assert len(rows) == 1  # still one row, not two
    kind, status, break_count = rows[0]
    assert kind == "support"
    assert status == "flipped"
    assert break_count == 1
