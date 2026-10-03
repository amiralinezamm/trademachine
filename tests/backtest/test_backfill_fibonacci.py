"""Anti-lookahead guards for scripts/backfill_fibonacci.py.

The backfill itself calls the live compute-and-store function per bar
(already anti-lookahead by construction -- CLAUDE.md rule 1/6). What this
script adds on top, and must prove here, is that it refuses outright
rather than silently backfilling into HOLDOUT or over a live-written row.
"""
from datetime import datetime, timezone

import pytest

from scripts.backfill_fibonacci import check_holdout_guard, check_live_overlap_guard

UTC = timezone.utc


def test_refuses_when_to_ts_exceeds_holdout():
    H = datetime(2026, 2, 18, 7, 5, tzinfo=UTC)
    to_ts = datetime(2026, 2, 18, 7, 5, 1, tzinfo=UTC)
    with pytest.raises(SystemExit):
        check_holdout_guard(to_ts, H)


def test_allows_when_to_ts_at_or_before_holdout():
    H = datetime(2026, 2, 18, 7, 5, tzinfo=UTC)
    check_holdout_guard(H, H)
    check_holdout_guard(H.replace(year=2025), H)


def test_refuses_when_to_ts_exceeds_first_live_row():
    first_live = datetime(2026, 9, 21, 9, 0, tzinfo=UTC)
    to_ts = datetime(2026, 9, 21, 9, 0, 1, tzinfo=UTC)
    with pytest.raises(SystemExit):
        check_live_overlap_guard(to_ts, first_live)


def test_allows_when_no_live_rows_yet():
    check_live_overlap_guard(datetime(2026, 1, 1, tzinfo=UTC), None)


def test_allows_when_to_ts_at_or_before_first_live_row():
    first_live = datetime(2026, 9, 21, 9, 0, tzinfo=UTC)
    check_live_overlap_guard(first_live, first_live)
    check_live_overlap_guard(first_live.replace(year=2025), first_live)
