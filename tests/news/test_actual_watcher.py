"""Tests for src/news/actual_watcher.py — all pure or mock-injected, no network."""
from datetime import datetime, timedelta, timezone

import pytest

from src.news.actual_watcher import (
    WATCH_AFTER_MIN,
    WATCH_BEFORE_MIN,
    find_actual_in_feed,
    get_watch_events,
    poll_for_actual,
    should_poll,
)

UTC = timezone.utc

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

T0 = datetime(2026, 9, 19, 12, 30, tzinfo=UTC)  # a High event at 12:30 UTC


def _event(title="NFP", impact="High", ts=T0, country="USD"):
    return {"title": title, "impact": impact, "ts_utc": ts, "country": country}


def _xml(title="NFP", country="USD", date="09-19-2026", time="12:30pm", actual=""):
    actual_tag = f"<actual><![CDATA[{actual}]]></actual>" if actual else "<actual/>"
    return f"""<?xml version="1.0" encoding="windows-1252"?>
<weeklyevents>
  <event>
    <title>{title}</title>
    <country>{country}</country>
    <date><![CDATA[{date}]]></date>
    <time><![CDATA[{time}]]></time>
    <impact><![CDATA[High]]></impact>
    <forecast><![CDATA[190K]]></forecast>
    <previous><![CDATA[205K]]></previous>
    {actual_tag}
    <url><![CDATA[https://www.forexfactory.com/calendar/1-nfp]]></url>
  </event>
</weeklyevents>"""


# ---------------------------------------------------------------------------
# get_watch_events — pure
# ---------------------------------------------------------------------------

def test_exactly_at_event_time_is_in_window():
    result = get_watch_events(T0, [_event()])
    assert len(result) == 1


def test_just_before_window_start_is_excluded():
    """WATCH_BEFORE_MIN before the event — just before window opens."""
    just_before = T0 - timedelta(minutes=WATCH_BEFORE_MIN + 1)
    result = get_watch_events(just_before, [_event()])
    assert result == []


def test_at_window_start_is_included():
    window_start = T0 - timedelta(minutes=WATCH_BEFORE_MIN)
    result = get_watch_events(window_start, [_event()])
    assert len(result) == 1


def test_at_window_end_is_included():
    window_end = T0 + timedelta(minutes=WATCH_AFTER_MIN)
    result = get_watch_events(window_end, [_event()])
    assert len(result) == 1


def test_just_after_window_end_is_excluded():
    just_after = T0 + timedelta(minutes=WATCH_AFTER_MIN + 1)
    result = get_watch_events(just_after, [_event()])
    assert result == []


def test_low_impact_event_not_watched():
    result = get_watch_events(T0, [_event(impact="Low")])
    assert result == []


def test_medium_impact_is_watched():
    result = get_watch_events(T0, [_event(impact="Medium")])
    assert len(result) == 1


def test_window_boundaries_added_to_result():
    result = get_watch_events(T0, [_event()])
    e = result[0]
    assert e["watch_window_start"] == T0 - timedelta(minutes=WATCH_BEFORE_MIN)
    assert e["watch_window_end"] == T0 + timedelta(minutes=WATCH_AFTER_MIN)


# ---------------------------------------------------------------------------
# find_actual_in_feed — pure
# ---------------------------------------------------------------------------

def test_returns_none_when_actual_absent():
    xml = _xml(actual="")
    assert find_actual_in_feed(xml, "NFP", "USD", T0) is None


def test_returns_actual_when_present():
    xml = _xml(actual="280K")
    result = find_actual_in_feed(xml, "NFP", "USD", T0)
    assert result == "280K"


def test_returns_none_for_wrong_title():
    xml = _xml(title="CPI m/m", actual="0.3%")
    assert find_actual_in_feed(xml, "NFP", "USD", T0) is None


def test_returns_none_for_wrong_country():
    xml = _xml(country="EUR", actual="280K")
    assert find_actual_in_feed(xml, "NFP", "USD", T0) is None


def test_returns_none_for_wrong_timestamp():
    xml = _xml(date="09-20-2026", actual="280K")  # different date
    assert find_actual_in_feed(xml, "NFP", "USD", T0) is None


def test_malformed_xml_returns_none():
    assert find_actual_in_feed("not xml at all", "NFP", "USD", T0) is None


def test_actual_whitespace_only_returns_none():
    xml = _xml(actual="   ")
    # The CDATA will have whitespace — should be treated as absent
    result = find_actual_in_feed(xml, "NFP", "USD", T0)
    assert result is None


# ---------------------------------------------------------------------------
# should_poll — pure
# ---------------------------------------------------------------------------

def test_should_poll_true_while_inside_window():
    window_end = T0 + timedelta(minutes=3)
    assert should_poll(T0, window_end) is True


def test_should_poll_false_after_window_end():
    window_end = T0
    assert should_poll(T0 + timedelta(seconds=1), window_end) is False


def test_should_poll_true_at_window_end():
    assert should_poll(T0, T0) is True


# ---------------------------------------------------------------------------
# poll_for_actual — mock-injected IO
# ---------------------------------------------------------------------------

def test_poll_returns_actual_on_first_successful_poll():
    """Feed immediately returns actual — one poll, done."""
    calls = []

    def fetch():
        calls.append(1)
        return _xml(actual="280K")

    slept = []
    window_end = T0 + timedelta(minutes=3)
    result = poll_for_actual(
        fetch_fn=fetch,
        event=_event(),
        window_end=window_end,
        interval_s=5,
        _now_fn=lambda: T0,
        _sleep_fn=lambda s: slept.append(s),
    )

    assert result == "280K"
    assert len(calls) == 1
    assert len(slept) == 0  # found on first try, no sleep needed


def test_poll_returns_actual_after_multiple_empty_polls():
    """First two polls return no actual; third returns it."""
    poll_count = [0]

    def fetch():
        poll_count[0] += 1
        if poll_count[0] < 3:
            return _xml(actual="")
        return _xml(actual="265K")

    # Clock advances slowly — stays inside window
    now_sequence = [T0, T0 + timedelta(seconds=5), T0 + timedelta(seconds=10)]
    now_idx = [0]

    def now():
        val = now_sequence[min(now_idx[0], len(now_sequence) - 1)]
        now_idx[0] += 1
        return val

    slept = []
    window_end = T0 + timedelta(minutes=3)
    result = poll_for_actual(
        fetch_fn=fetch,
        event=_event(),
        window_end=window_end,
        interval_s=5,
        _now_fn=now,
        _sleep_fn=lambda s: slept.append(s),
    )

    assert result == "265K"
    assert poll_count[0] == 3
    assert slept == [5, 5]  # slept twice between polls


def test_poll_returns_none_when_window_expires_without_actual():
    """Window ends before actual ever appears."""
    window_end = T0  # window already ending right now

    # Clock starts just at window_end, so should_poll is True once, then exits
    times = iter([T0, T0 + timedelta(seconds=1)])

    def fetch():
        return _xml(actual="")  # never has actual

    result = poll_for_actual(
        fetch_fn=fetch,
        event=_event(),
        window_end=window_end,
        interval_s=5,
        _now_fn=lambda: next(times),
        _sleep_fn=lambda s: None,
    )

    assert result is None


def test_poll_sleeps_correct_interval():
    """Verify the injected interval_s is passed to sleep correctly."""
    poll_count = [0]

    def fetch():
        poll_count[0] += 1
        return _xml(actual="" if poll_count[0] < 2 else "1.0%")

    slept = []
    times = iter([T0, T0 + timedelta(seconds=2), T0 + timedelta(seconds=4)])
    window_end = T0 + timedelta(minutes=3)

    poll_for_actual(
        fetch_fn=fetch,
        event=_event(),
        window_end=window_end,
        interval_s=7,  # custom interval
        _now_fn=lambda: next(times),
        _sleep_fn=lambda s: slept.append(s),
    )

    assert slept == [7]
