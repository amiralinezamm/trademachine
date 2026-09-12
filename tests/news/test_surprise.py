from src.news.surprise import compute_surprise, load_event_map

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
    """An event not present in event_map.yaml (e.g. PPI, not covered by
    SPEC.md's table) must not guess a direction — surprise is still computed,
    but raw_direction/expected_dir stay None and `mapped` is False."""
    result = compute_surprise(
        event_title="PPI m/m",
        actual=0.5,
        forecast=0.3,
        historical_surprises=[],
        event_map=EVENT_MAP,
    )
    assert result["mapped"] is False
    assert result["gold_sign"] is None
    assert result["raw_direction"] is None
    assert result["surprise"] == 0.5 - 0.3
