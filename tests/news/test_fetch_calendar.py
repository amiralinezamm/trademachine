from datetime import datetime, timezone

import pytest

from src.news.fetch_calendar import filter_events, get_connection, parse_events, upsert_events

SAMPLE_XML = """<?xml version="1.0" encoding="windows-1252"?>
<weeklyevents>
	<event>
		<title>Core CPI m/m</title>
		<country>USD</country>
		<date><![CDATA[09-11-2026]]></date>
		<time><![CDATA[12:30pm]]></time>
		<impact><![CDATA[High]]></impact>
		<forecast><![CDATA[0.3%]]></forecast>
		<previous><![CDATA[0.3%]]></previous>
		<url><![CDATA[https://www.forexfactory.com/calendar/example-core-cpi]]></url>
	</event>
	<event>
		<title>ANZ Job Advertisements m/m</title>
		<country>AUD</country>
		<date><![CDATA[09-07-2026]]></date>
		<time><![CDATA[1:30am]]></time>
		<impact><![CDATA[Low]]></impact>
		<forecast />
		<previous><![CDATA[0.8%]]></previous>
		<url><![CDATA[https://www.forexfactory.com/calendar/example-anz]]></url>
	</event>
	<event>
		<title>Bank Holiday</title>
		<country>GBP</country>
		<date><![CDATA[09-08-2026]]></date>
		<time><![CDATA[12:00am]]></time>
		<impact><![CDATA[Holiday]]></impact>
		<forecast />
		<previous />
		<url><![CDATA[https://www.forexfactory.com/calendar/example-holiday]]></url>
	</event>
	<event>
		<title>Unemployment Claims</title>
		<country>USD</country>
		<date><![CDATA[09-10-2026]]></date>
		<time><![CDATA[12:30pm]]></time>
		<impact><![CDATA[Medium]]></impact>
		<forecast><![CDATA[235K]]></forecast>
		<previous><![CDATA[236K]]></previous>
		<url><![CDATA[https://www.forexfactory.com/calendar/example-claims]]></url>
	</event>
	<event>
		<title>Tankan Manufacturing Index</title>
		<country>JPY</country>
		<date><![CDATA[09-08-2026]]></date>
		<time><![CDATA[7:50pm]]></time>
		<impact><![CDATA[High]]></impact>
		<forecast><![CDATA[15]]></forecast>
		<previous><![CDATA[13]]></previous>
		<url><![CDATA[https://www.forexfactory.com/calendar/example-tankan]]></url>
	</event>
</weeklyevents>
"""


def test_parse_events_stores_utc_with_no_shift():
    """The feed's date/time is already UTC (confirmed against known fixed BLS
    release times) — parsing must NOT apply any ET->UTC or other shift."""
    events = parse_events(SAMPLE_XML)
    cpi = next(e for e in events if e["title"] == "Core CPI m/m")
    assert cpi["ts_utc"] == datetime(2026, 9, 11, 12, 30, tzinfo=timezone.utc)
    assert cpi["ts_utc"].tzinfo is timezone.utc


def test_filter_keeps_only_high_medium_and_usd_eur_gbp():
    events = parse_events(SAMPLE_XML)
    kept = filter_events(events, currency_weights={"USD": 1.0, "EUR": 0.5, "GBP": 0.5})

    titles = {e["title"] for e in kept}
    assert titles == {"Core CPI m/m", "Unemployment Claims"}  # AUD/Low, GBP/Holiday, JPY dropped

    cpi = next(e for e in kept if e["title"] == "Core CPI m/m")
    assert cpi["currency_weight"] == 1.0


def test_filter_drops_currencies_outside_usd_eur_gbp():
    events = parse_events(SAMPLE_XML)
    kept = filter_events(events, currency_weights={"USD": 1.0, "EUR": 0.5, "GBP": 0.5})
    assert all(e["country"] in ("USD", "EUR", "GBP") for e in kept)
    # the High-impact JPY (Tankan) event must be dropped despite being High impact
    assert "Tankan Manufacturing Index" not in {e["title"] for e in kept}


@pytest.fixture
def db_conn():
    conn = get_connection()
    yield conn
    conn.rollback()  # never persist test data
    conn.close()


def test_upsert_is_idempotent(db_conn):
    """Running the upsert twice with the same event must not create a
    duplicate row and must not error."""
    event = {
        "title": "__TEST_IDEMPOTENCY_MARKER__",
        "country": "USD",
        "ts_utc": datetime(2099, 1, 1, 0, 0, tzinfo=timezone.utc),
        "impact": "High",
        "currency_weight": 1.0,
        "forecast": "1.0%",
        "previous": "0.9%",
        "url": "https://example.invalid/marker",
    }

    upsert_events(db_conn, [event])
    upsert_events(db_conn, [{**event, "forecast": "1.1%"}])  # simulate a re-fetch with updated forecast

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*), max(forecast) FROM news_events WHERE title = %s",
            (event["title"],),
        )
        count, forecast = cur.fetchone()

    assert count == 1
    assert forecast == "1.1%"  # updated in place, not duplicated
