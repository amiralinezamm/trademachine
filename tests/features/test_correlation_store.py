import pytest

from src.features.levels_store import get_connection
from src.features.correlation_store import register_dollar_correlation_direction_rule

PARAMS = {"dollar_corr_window": 200, "dxy_direction_bars": 3, "dollar_corr_min_abs": 0.3}


@pytest.fixture
def db_conn():
    conn = get_connection()
    yield conn
    conn.rollback()
    conn.close()


def test_register_dollar_correlation_direction_rule_inserts_proposed_status(db_conn):
    register_dollar_correlation_direction_rule(db_conn, PARAMS)
    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM rules WHERE id = 'dollar_correlation_direction'")
        (status,) = cur.fetchone()
    assert status == "proposed"


def test_register_dollar_correlation_direction_rule_does_not_clobber_advanced_status(db_conn):
    register_dollar_correlation_direction_rule(db_conn, PARAMS)
    with db_conn.cursor() as cur:
        cur.execute("UPDATE rules SET status = 'testing' WHERE id = 'dollar_correlation_direction'")

    register_dollar_correlation_direction_rule(db_conn, PARAMS)  # re-run -- no-op on conflict

    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM rules WHERE id = 'dollar_correlation_direction'")
        (status,) = cur.fetchone()
    assert status == "testing"
