import pytest

from src.features.levels_store import get_connection
from src.features.regime_store import register_proposed_rule

PARAMS = {"trend_adx_thresh": 25, "range_adx_thresh": 20, "range_bb_pct_thresh": 30}


@pytest.fixture
def db_conn():
    conn = get_connection()
    yield conn
    conn.rollback()
    conn.close()


def test_register_proposed_rule_inserts_proposed_status(db_conn):
    register_proposed_rule(db_conn, PARAMS)
    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM rules WHERE id = 'regime_quality'")
        (status,) = cur.fetchone()
    assert status == "proposed"


def test_register_proposed_rule_does_not_clobber_advanced_status(db_conn):
    register_proposed_rule(db_conn, PARAMS)
    with db_conn.cursor() as cur:
        cur.execute("UPDATE rules SET status = 'testing' WHERE id = 'regime_quality'")

    register_proposed_rule(db_conn, PARAMS)  # re-run -- must be a no-op on conflict

    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM rules WHERE id = 'regime_quality'")
        (status,) = cur.fetchone()
    assert status == "testing"
