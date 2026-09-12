"""SPEC.md 4.7 — ForexFactory calendar ingest.

Only ff_calendar_thisweek.xml is used. nextweek/lastweek don't exist on this
feed (confirmed 404) and are intentionally not implemented — the feed is
rolling, so near-term events surface in thisweek on their own; the only real
limitation is reduced lookahead near the end of the week, which is accepted.

The feed's <date>/<time> are UTC already (confirmed against known fixed BLS/
UoM release times) — stored as-is, with NO timezone conversion. This is
specific to this feed; MT5 server-time still needs conversion elsewhere.

Idempotent: re-running upserts by (title, country, ts_utc), never inserts
duplicates.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import psycopg2
import psycopg2.extras
import requests
import yaml
from dotenv import load_dotenv

FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"
KEPT_IMPACTS = {"High", "Medium"}

PARAMS_PATH = Path(__file__).resolve().parents[2] / "config" / "params.yaml"

logger = logging.getLogger(__name__)


def fetch_raw_xml(url: str = FEED_URL, timeout: float = 15.0) -> str:
    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def parse_events(xml_text: str) -> list[dict[str, Any]]:
    """Parse raw feed XML into event dicts. Pure — no filtering, no network."""
    root = ET.fromstring(xml_text)
    events = []
    for el in root.findall("event"):
        title = el.findtext("title")
        country = el.findtext("country")
        date_str = el.findtext("date")
        time_str = el.findtext("time")
        impact = el.findtext("impact")
        forecast = el.findtext("forecast") or None
        previous = el.findtext("previous") or None
        url = el.findtext("url") or None

        ts_utc = _parse_ts_utc(date_str, time_str)
        if ts_utc is None:
            logger.warning("Skipping event with unparseable date/time: %r %r %r", title, date_str, time_str)
            continue

        events.append(
            {
                "title": title,
                "country": country,
                "ts_utc": ts_utc,
                "impact": impact,
                "forecast": forecast,
                "previous": previous,
                "url": url,
            }
        )
    return events


def _parse_ts_utc(date_str: str | None, time_str: str | None) -> datetime | None:
    if not date_str or not time_str:
        return None
    try:
        naive = datetime.strptime(f"{date_str} {time_str}", "%m-%d-%Y %I:%M%p")
    except ValueError:
        return None
    # Feed is already UTC — attach the tzinfo, do not shift the clock.
    return naive.replace(tzinfo=timezone.utc)


def load_currency_weights(params_path: Path = PARAMS_PATH) -> dict[str, float]:
    with open(params_path) as f:
        params = yaml.safe_load(f)
    return params["news"]["currency_weights"]


def filter_events(
    events: list[dict[str, Any]], currency_weights: dict[str, float] | None = None
) -> list[dict[str, Any]]:
    """SPEC.md 4.7: keep High/Medium impact only; keep USD/EUR/GBP only, tagging weight."""
    if currency_weights is None:
        currency_weights = load_currency_weights()

    kept = []
    for e in events:
        if e["impact"] not in KEPT_IMPACTS:
            continue
        weight = currency_weights.get(e["country"])
        if weight is None:
            continue
        kept.append({**e, "currency_weight": weight})
    return kept


def upsert_events(conn, events: list[dict[str, Any]]) -> int:
    """Upsert by (title, country, ts_utc). Never touches `notified` on conflict.
    Returns number of rows affected."""
    if not events:
        return 0
    rows = [
        (
            e["title"],
            e["country"],
            e["ts_utc"],
            e["impact"],
            e["currency_weight"],
            e["forecast"],
            e["previous"],
            e.get("actual"),
            e["url"],
        )
        for e in events
    ]
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO news_events
                (title, country, ts_utc, impact, currency_weight, forecast, previous, actual, url)
            VALUES %s
            ON CONFLICT (title, country, ts_utc) DO UPDATE SET
                impact = EXCLUDED.impact,
                currency_weight = EXCLUDED.currency_weight,
                forecast = EXCLUDED.forecast,
                previous = EXCLUDED.previous,
                actual = COALESCE(EXCLUDED.actual, news_events.actual),
                url = EXCLUDED.url,
                fetched_at = now()
            """,
            rows,
        )
        return cur.rowcount


def get_connection():
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    return psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST_LOCAL", "127.0.0.1"),
        port=os.environ.get("POSTGRES_PORT", "5432"),
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        dbname=os.environ["POSTGRES_DB"],
    )


def run(url: str = FEED_URL) -> int:
    xml_text = fetch_raw_xml(url)
    events = parse_events(xml_text)
    kept = filter_events(events)
    conn = get_connection()
    try:
        n = upsert_events(conn, kept)
        conn.commit()
    finally:
        conn.close()
    logger.info("Fetched %d raw events, kept %d, upserted.", len(events), len(kept))
    return n


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="Fetch ForexFactory calendar and upsert into news_events")
    parser.parse_args()
    run()
