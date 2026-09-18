"""SPEC.md 4.7 — watcher for 'actual' values on High/Medium news events.

SPEC says: from 2 minutes before to 3 minutes after each degree-1/2 event,
poll the feed every 5 seconds until `actual` appears.

Pure functions (testable without network):
  get_watch_events(now_ts, events)         → events currently in watch window
  find_actual_in_feed(xml_text, ...)       → str | None
  should_poll(now_ts, window_end)          → bool

IO layer (injectable for tests):
  poll_for_actual(fetch_fn, ...)           → actual value string | None
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from xml.etree import ElementTree as ET

WATCH_BEFORE_MIN = 2
WATCH_AFTER_MIN = 3
POLL_INTERVAL_S = 5


def get_watch_events(
    now_ts: datetime,
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Pure. Returns events whose watch window contains now_ts.

    Watch window: [event_ts - WATCH_BEFORE_MIN, event_ts + WATCH_AFTER_MIN].
    Only High and Medium impact events are watched.
    """
    watched = []
    for e in events:
        if e.get("impact") not in ("High", "Medium"):
            continue
        ts = e["ts_utc"]
        window_start = ts - timedelta(minutes=WATCH_BEFORE_MIN)
        window_end = ts + timedelta(minutes=WATCH_AFTER_MIN)
        if window_start <= now_ts <= window_end:
            watched.append({
                **e,
                "watch_window_start": window_start,
                "watch_window_end": window_end,
            })
    return watched


def should_poll(now_ts: datetime, window_end: datetime) -> bool:
    """Pure. True while we are still inside the watch window."""
    return now_ts <= window_end


def find_actual_in_feed(
    xml_text: str,
    title: str,
    country: str,
    ts_utc: datetime,
) -> str | None:
    """Pure. Parses the feed XML and returns the actual value for the matching
    event, or None if the event is not found or actual is absent/empty.

    Matching: title + country + parsed date/time must equal ts_utc (UTC).
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None

    for el in root.findall("event"):
        el_title = el.findtext("title", "")
        el_country = el.findtext("country", "")
        if el_title != title or el_country != country:
            continue
        # Parse date/time — same logic as fetch_calendar._parse_ts_utc
        date_str = el.findtext("date", "")
        time_str = el.findtext("time", "")
        el_ts = _parse_ts_utc(date_str, time_str)
        if el_ts != ts_utc:
            continue
        actual_raw = el.findtext("actual", "") or ""
        return actual_raw.strip() if actual_raw.strip() else None

    return None


def _parse_ts_utc(date_str: str, time_str: str) -> datetime | None:
    from datetime import datetime, timezone
    try:
        naive = datetime.strptime(f"{date_str} {time_str}", "%m-%d-%Y %I:%M%p")
    except ValueError:
        return None
    return naive.replace(tzinfo=timezone.utc)


def poll_for_actual(
    fetch_fn: Callable[[], str],
    event: dict[str, Any],
    window_end: datetime,
    interval_s: float = POLL_INTERVAL_S,
    _now_fn: Callable[[], datetime] | None = None,
    _sleep_fn: Callable[[float], None] | None = None,
) -> str | None:
    """IO layer. Polls fetch_fn every interval_s seconds until actual appears
    or window_end is reached.

    Parameters
    ----------
    fetch_fn     : callable returning the raw XML string from the feed
    event        : dict with keys title, country, ts_utc, impact
    window_end   : aware datetime — stop polling at this moment
    interval_s   : seconds between polls (default 5, injectable for tests)
    _now_fn      : injectable clock (default: datetime.now(UTC))
    _sleep_fn    : injectable sleep (default: time.sleep)

    Returns the actual value string when found, or None if window expired.
    """
    now_fn = _now_fn or (lambda: datetime.now(timezone.utc))
    sleep_fn = _sleep_fn or time.sleep

    title = event["title"]
    country = event["country"]
    ts_utc = event["ts_utc"]

    while should_poll(now_fn(), window_end):
        xml_text = fetch_fn()
        actual = find_actual_in_feed(xml_text, title, country, ts_utc)
        if actual is not None:
            return actual
        sleep_fn(interval_s)

    return None
