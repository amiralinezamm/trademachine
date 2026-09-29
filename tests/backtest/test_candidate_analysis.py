"""Pure tests for src/backtest/candidate_analysis.py — no DB."""
from datetime import datetime, timedelta, timezone

import pytest

from src.backtest.candidate_analysis import (
    funnel, months_spanned, rescore, suggest_weights, summarize,
    threshold_sweep, voter_table, wilson_interval,
)

UTC = timezone.utc
T0 = datetime(2025, 1, 1, tzinfo=UTC)


def _row(i, gate="fired", votes=None, net=2.0, win=True, exit_mode="level", risk=4.0):
    pnl = 8.0 if win else -4.0
    return {
        "ts_utc": T0 + timedelta(days=i), "gate": gate, "exit_mode": exit_mode,
        "votes": votes or {}, "net_votes": net,
        "outcome": "tp" if win else "sl", "pnl_usd": pnl, "risk_usd": risk,
        "u_outcome": "tp" if win else "sl", "u_pnl_usd": pnl,
    }


def test_wilson_interval_known_value():
    lo, hi = wilson_interval(12, 29)          # the 2026-09 HOLDOUT: 12 TP / 29
    assert lo == pytest.approx(0.2548, abs=1e-3)
    assert hi == pytest.approx(0.5925, abs=1e-3)
    assert wilson_interval(0, 0) is None


def test_summarize_r_multiples_and_winrate():
    rows = [_row(0, win=True), _row(1, win=False), _row(2, win=False)]
    s = summarize(rows, uniform=True)
    assert s["n"] == 3 and s["wins"] == 1
    assert s["winrate"] == pytest.approx(1 / 3)
    assert s["mean_r"] == pytest.approx((2 - 1 - 1) / 3)   # +2R, -1R, -1R
    assert s["total_usd"] == pytest.approx(0.0)


def test_summarize_skips_unevaluated_rows():
    r = _row(0)
    r["u_outcome"] = None
    assert summarize([r], uniform=True)["n"] == 0


def test_funnel_groups_by_gate():
    rows = [_row(0, "fired"), _row(1, "votes_rejected", win=False), _row(2, "no_valid_sl_tp")]
    f = funnel(rows)
    assert f["fired"]["n"] == 1
    assert f["votes_rejected"]["n"] == 1 and f["votes_rejected"]["wins"] == 0
    assert f["blackout"]["n"] == 0


def test_voter_table_excludes_blackout():
    rows = [_row(0, votes={"patterns": 1}), _row(1, "blackout", votes={"patterns": 1})]
    t = voter_table(rows)
    assert t["patterns"][1]["n"] == 1


def test_suggest_weights_requires_min_n():
    rows = [_row(i, votes={"x": 1}, win=(i % 2 == 0)) for i in range(10)]
    w = suggest_weights(voter_table(rows), baseline_winrate=0.5, min_n=200)
    assert w["x"]["weight"] == 0.0
    assert "no evidence" in w["x"]["reason"]


def test_suggest_weights_sign_follows_evidence():
    good = [_row(i, votes={"g": 1}, win=(i % 10 < 7)) for i in range(300)]    # 70% when +1
    bad = [_row(300 + i, votes={"g": -1}, win=(i % 10 < 3)) for i in range(300)]  # 30% when -1
    w = suggest_weights(voter_table(good + bad), baseline_winrate=0.5, min_n=200)
    assert w["g"]["weight"] > 0
    assert w["g"]["reason"] == "both sides"


def test_threshold_sweep_counts_and_live_view():
    rows = [
        _row(0, net=0.0, exit_mode="shadow_min_rr"),
        _row(1, net=1.0),
        _row(2, net=3.0),
        _row(3, "blackout", net=5.0),
    ]
    sweep = {s["threshold"]: s for s in threshold_sweep(rows, [0, 1, 3])}
    assert sweep[0]["entry"]["n"] == 3          # blackout never counted
    assert sweep[0]["live"]["n"] == 2           # shadow exit isn't live-sendable
    assert sweep[3]["entry"]["n"] == 1


def test_rescore_and_weighted_sweep():
    assert rescore({"a": 1, "b": -1}, {"a": 0.5, "b": 0.2}) == pytest.approx(0.3)
    rows = [_row(0, votes={"a": 1}), _row(1, votes={"a": -1})]
    sweep = threshold_sweep(rows, [0.1], weights={"a": 0.5})
    assert sweep[0]["entry"]["n"] == 1


def test_months_spanned():
    assert months_spanned([_row(0), _row(61)]) == pytest.approx(61 / 30.44)
