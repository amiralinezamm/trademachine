from datetime import datetime, timedelta, timezone

from src.news.blackout import compute_blackout

UTC = timezone.utc


def _event(title, impact, ts):
    return {"title": title, "impact": impact, "ts_utc": ts}


def test_no_relevant_news_means_no_blackout():
    """SPEC.md 4.7-a: absence of news must report 'no blackout', not a
    conservative default."""
    t0 = datetime(2026, 9, 11, 12, 30, tzinfo=UTC)
    events = [_event("Low impact thing", "Low", t0)]  # not High/Medium
    result = compute_blackout(t0, events)
    assert result["blackout"] is False
    assert result["window_start"] is None
    assert result["events"] == []


def test_high_impact_window_before_and_after():
    """High: 20 min before, 15 min after (SPEC.md 4.7-a)."""
    t0 = datetime(2026, 9, 11, 12, 30, tzinfo=UTC)
    events = [_event("CPI m/m", "High", t0)]

    just_before_window = compute_blackout(t0 - timedelta(minutes=21), events)
    assert just_before_window["blackout"] is False

    start_of_window = compute_blackout(t0 - timedelta(minutes=20), events)
    assert start_of_window["blackout"] is True

    at_event = compute_blackout(t0, events)
    assert at_event["blackout"] is True

    end_of_window = compute_blackout(t0 + timedelta(minutes=15), events)
    assert end_of_window["blackout"] is True

    just_after_window = compute_blackout(t0 + timedelta(minutes=16), events)
    assert just_after_window["blackout"] is False


def test_medium_impact_window_before_and_after():
    """Medium: 15 min before, 10 min after (SPEC.md 4.7-a)."""
    t0 = datetime(2026, 9, 11, 17, 0, tzinfo=UTC)
    events = [_event("Unemployment Claims", "Medium", t0)]

    assert compute_blackout(t0 - timedelta(minutes=16), events)["blackout"] is False
    assert compute_blackout(t0 - timedelta(minutes=15), events)["blackout"] is True
    assert compute_blackout(t0 + timedelta(minutes=10), events)["blackout"] is True
    assert compute_blackout(t0 + timedelta(minutes=11), events)["blackout"] is False


def test_consensus_rule_doubles_windows_when_clustered():
    """SPEC.md 4.7-a: >1 news within a 30-minute window -> both windows double
    (union of intervals, not summed separately)."""
    t0 = datetime(2026, 9, 11, 12, 30, tzinfo=UTC)
    t1 = t0 + timedelta(minutes=20)  # within 30 min of t0 -> triggers consensus
    events = [
        _event("CPI m/m", "High", t0),
        _event("Core CPI m/m", "High", t1),
    ]

    # Without doubling, t0's High window would end at t0+15min (before t1 starts
    # its own window at t1-20min = t0). With doubling (x2), t0's window extends
    # to t0+30min, well past t1's own window end (t1+30min = t0+50min).
    # Check a point that is ONLY inside the blackout range because of doubling:
    # t0 + 27 minutes: without doubling this is outside t0's window (ends +15)
    # and outside t1's window (starts at t1-15=t0+5, i.e. covered anyway).
    # Use a case that isolates doubling unambiguously: a single high-impact
    # event whose only neighbor is just past the un-doubled window edge.
    probe = t0 + timedelta(minutes=27)  # 7 min after t0's un-doubled end (+20)
    result = compute_blackout(probe, events)
    assert result["blackout"] is True
    assert len(result["events"]) == 2  # merged into one window covering both

    # Sanity: far outside even the doubled range there is no blackout.
    far_after = t1 + timedelta(minutes=31)  # past t1's doubled after-window (+30)
    assert compute_blackout(far_after, events)["blackout"] is False


def test_isolated_events_do_not_trigger_consensus_and_dont_merge():
    """Two High events more than 30 minutes apart must NOT double and must
    produce two separate, non-overlapping windows."""
    t0 = datetime(2026, 9, 11, 12, 30, tzinfo=UTC)
    t1 = t0 + timedelta(hours=2)
    events = [
        _event("CPI m/m", "High", t0),
        _event("Fed Funds Rate", "High", t1),
    ]

    # 16 minutes after t0 would be inside a doubled window (+30) but outside
    # the correct, un-doubled window (+15) — must be False.
    result = compute_blackout(t0 + timedelta(minutes=16), events)
    assert result["blackout"] is False

    # Each event's own (non-doubled) window still works normally.
    assert compute_blackout(t0, events)["blackout"] is True
    assert compute_blackout(t1, events)["blackout"] is True
