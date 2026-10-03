"""Anti-lookahead guards for scripts/backfill_matrix.py (see
test_backfill_fibonacci.py for the rationale -- identical guard shape)."""
from datetime import datetime, timezone

import pytest

from scripts.backfill_matrix import check_holdout_guard, check_live_overlap_guard

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
    first_live = datetime(2026, 3, 25, 0, 0, tzinfo=UTC)
    to_ts = datetime(2026, 3, 25, 0, 0, 1, tzinfo=UTC)
    with pytest.raises(SystemExit):
        check_live_overlap_guard(to_ts, first_live)


def test_allows_when_no_live_rows_yet():
    check_live_overlap_guard(datetime(2026, 1, 1, tzinfo=UTC), None)


def test_allows_when_to_ts_at_or_before_first_live_row():
    first_live = datetime(2026, 3, 25, 0, 0, tzinfo=UTC)
    check_live_overlap_guard(first_live, first_live)
    check_live_overlap_guard(first_live.replace(year=2025), first_live)
