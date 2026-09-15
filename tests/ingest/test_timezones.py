from datetime import datetime, timezone

from src.ingest.timezones import broker_epoch_to_utc, broker_naive_to_utc, is_dst_active

PARAMS = {
    "server_timezone": {
        "dst_offset_hours": 3,
        "standard_offset_hours": 2,
        "dst_start_rule": "2nd_sunday_march",
        "dst_end_rule": "last_sunday_october",
    }
}


def test_summer_date_is_dst_active():
    # Mid-July, well inside the DST window every year.
    assert is_dst_active(datetime(2026, 7, 15).date()) is True


def test_winter_date_is_not_dst_active():
    # Mid-January, well outside the DST window.
    assert is_dst_active(datetime(2026, 1, 15).date()) is False


def test_dst_boundary_dates_2026():
    # 2026: 2nd Sunday of March = March 8; last Sunday of October = Oct 25.
    assert is_dst_active(datetime(2026, 3, 7).date()) is False  # Saturday before
    assert is_dst_active(datetime(2026, 3, 8).date()) is True   # the transition Sunday itself
    assert is_dst_active(datetime(2026, 10, 24).date()) is True   # Saturday before end
    assert is_dst_active(datetime(2026, 10, 25).date()) is False  # the transition Sunday itself (window is [start, end))


def test_summer_offset_matches_live_measurement():
    """2026-09-15: measured live against NTP-synced UTC as exactly +3h."""
    summer_dt = datetime(2026, 9, 15, 13, 29, 37)
    result = broker_naive_to_utc(summer_dt, params=PARAMS)
    assert result == datetime(2026, 9, 15, 10, 29, 37, tzinfo=timezone.utc)


def test_winter_offset_is_two_hours():
    winter_dt = datetime(2026, 1, 15, 12, 0, 0)
    result = broker_naive_to_utc(winter_dt, params=PARAMS)
    assert result == datetime(2026, 1, 15, 10, 0, 0, tzinfo=timezone.utc)


def test_broker_epoch_to_utc_roundtrip():
    # Same instant as the live measurement, expressed as the epoch MT5 would report.
    epoch = int(datetime(2026, 9, 15, 13, 29, 37, tzinfo=timezone.utc).timestamp())
    result = broker_epoch_to_utc(epoch, params=PARAMS)
    assert result == datetime(2026, 9, 15, 10, 29, 37, tzinfo=timezone.utc)


def test_no_exceptions_across_seven_years_of_real_transition_dates():
    """Every DST-start Friday found in the 2019-2026 weekend-gap analysis
    (docs/data_depth.md investigation) must classify as DST-inactive the day
    before and DST-active the transition Sunday itself."""
    transition_sundays = [
        (2020, 3, 8), (2021, 3, 14), (2022, 3, 13), (2023, 3, 12),
        (2024, 3, 10), (2025, 3, 9), (2026, 3, 8),
    ]
    for year, month, day in transition_sundays:
        sunday = datetime(year, month, day).date()
        saturday = datetime(year, month, day - 1).date()
        assert is_dst_active(saturday) is False, f"{saturday} should be pre-DST"
        assert is_dst_active(sunday) is True, f"{sunday} should be DST-active"
