from src.news.surprise import compute_surprise, load_event_map, lookup_gold_sign

EVENT_MAP = load_event_map()


def test_raw_direction_for_negative_gold_sign_event_below_min_samples():
    """CPI is gold_sign=-1: actual above forecast -> gold expected down (-1).
    With < 10 historical samples, z_surprise must be None (SPEC.md 4.7-b)."""
    result = compute_surprise(
        event_title="Core CPI m/m",
        actual=0.4,
        forecast=0.3,
        historical_surprises=[0.01, -0.02, 0.03],  # only 3 samples
        event_map=EVENT_MAP,
    )
    assert result["mapped"] is True
    assert result["surprise"] == 0.4 - 0.3
    assert result["raw_direction"] == -1
    assert result["z_surprise"] is None
    assert result["expected_dir"] is None


def test_z_surprise_reported_once_min_samples_reached():
    historical = [0.0, 0.01, -0.01, 0.02, -0.02, 0.0, 0.01, -0.01, 0.02, -0.02]  # exactly 10
    result = compute_surprise(
        event_title="Core CPI m/m",
        actual=0.4,
        forecast=0.3,
        historical_surprises=historical,
        event_map=EVENT_MAP,
    )
    assert result["sample_count"] == 10
    assert result["z_surprise"] is not None
    assert result["expected_dir"] == -1  # positive surprise * gold_sign(-1)


def test_z_surprise_absent_one_below_min_samples():
    historical = [0.0, 0.01, -0.01, 0.02, -0.02, 0.0, 0.01, -0.01, 0.02]  # 9 samples
    result = compute_surprise(
        event_title="Core CPI m/m",
        actual=0.4,
        forecast=0.3,
        historical_surprises=historical,
        event_map=EVENT_MAP,
    )
    assert result["z_surprise"] is None
    assert result["expected_dir"] is None


def test_inverse_event_positive_gold_sign():
    """Unemployment Claims (alias for Initial Jobless Claims) is gold_sign=+1:
    actual above forecast -> gold expected UP."""
    result = compute_surprise(
        event_title="Unemployment Claims",
        actual=250_000,
        forecast=230_000,
        historical_surprises=[],
        event_map=EVENT_MAP,
    )
    assert result["mapped"] is True
    assert result["raw_direction"] == 1


def test_unmapped_event_reports_surprise_but_no_direction():
    """An event not present in event_map.yaml at all must not guess a
    direction — surprise is still computed, but raw_direction/expected_dir
    stay None and `mapped` is False."""
    result = compute_surprise(
        event_title="Some Made-Up Indicator Nobody Tracks",
        actual=0.5,
        forecast=0.3,
        historical_surprises=[],
        event_map=EVENT_MAP,
    )
    assert result["mapped"] is False
    assert result["gold_sign"] is None
    assert result["raw_direction"] is None
    assert result["surprise"] == 0.5 - 0.3


def test_ppi_now_mapped_from_2026_09_27_research():
    """PPI m/m (USD) is in the 2026-09-27 research-backed event_map: higher
    producer-price inflation -> Fed hawkish -> gold down (gold_sign -1)."""
    result = compute_surprise(
        event_title="PPI m/m", actual=0.5, forecast=0.3,
        historical_surprises=[], event_map=EVENT_MAP,
    )
    assert result["mapped"] is True
    assert result["gold_sign"] == -1
    assert result["raw_direction"] == -1


# ---------------------------------------------------------------------------
# Cross-currency title collisions (2026-09-27 event_map rebuild) — the same
# title means the OPPOSITE gold_sign for USD vs. EUR/GBP, so `country` must
# disambiguate. Without it, an ambiguous title must resolve to None rather
# than silently picking one currency's mapping.
# ---------------------------------------------------------------------------

def test_cpi_yy_usd_is_bearish_for_gold():
    assert lookup_gold_sign("CPI y/y", EVENT_MAP, country="USD") == -1


def test_cpi_yy_gbp_is_bullish_for_gold():
    assert lookup_gold_sign("CPI y/y", EVENT_MAP, country="GBP") == 1


def test_cpi_yy_without_country_is_ambiguous():
    """USD says -1, GBP says +1 for the same title — without knowing the
    currency, guessing either one would be wrong half the time."""
    assert lookup_gold_sign("CPI y/y", EVENT_MAP, country=None) is None


def test_compute_surprise_uses_country_to_disambiguate():
    usd_result = compute_surprise(
        event_title="CPI y/y", actual=3.5, forecast=3.2,
        historical_surprises=[], event_map=EVENT_MAP, country="USD",
    )
    gbp_result = compute_surprise(
        event_title="CPI y/y", actual=3.5, forecast=3.2,
        historical_surprises=[], event_map=EVENT_MAP, country="GBP",
    )
    assert usd_result["raw_direction"] == -1
    assert gbp_result["raw_direction"] == 1
