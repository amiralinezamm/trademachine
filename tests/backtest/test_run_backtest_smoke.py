"""End-to-end smoke test of run_backtest() on synthetic data with a fake DB.

Guards the 2026-09-29 loop rewrite: the TP level B sits ~6 ATR from price,
which the old ±3xATR speed filter hid from the level-B search (the cause of
most "no_valid_sl_tp" rejections). With live-parity levels it must be found.
Also checks candidate logging records rejected candidates, with outcomes.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import src.backtest.replay as replay

UTC = timezone.utc
T0 = datetime(2025, 6, 2, 8, 0, tzinfo=UTC)   # Monday 08:00 UTC -- not off-hours, no weekend
K = 60                                        # signal bar


class _Cur:
    description = ()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        pass

    def fetchall(self):
        return []


class _Conn:
    def cursor(self):
        return _Cur()

    def commit(self):
        pass

    def close(self):
        pass


def _candles():
    out = []
    for i in range(120):
        c = {"ts_utc": T0 + timedelta(minutes=5 * i), "open": 2000.0, "high": 2000.5,
             "low": 1999.5, "close": 2000.0, "spread": 30}
        if i == K:                       # wick into support A, close back above it
            c.update(high=2000.2, low=1998.2, close=1999.2)
        if i >= K + 3:                   # rally into level B -> TP
            c.update(open=2006.0, high=2007.0, low=2005.0, close=2006.0)
        out.append(c)
    return out


def _level(id_, kind, lo, hi):
    return {"id": id_, "kind": kind, "price_low": lo, "price_high": hi,
            "strength": 0.0, "status": "active", "created_ts": T0}


LEVELS = [
    _level(1, "support", 1998.0, 1998.5),      # A: touched at bar K
    _level(2, "resistance", 2006.0, 2006.5),   # B: ~6 ATR above entry
    _level(3, "support", 1994.0, 1994.4),      # weak fillers -> realistic median
    _level(4, "support", 1993.0, 1993.4),
]
STRENGTH = {1: 2.0, 2: 1.5, 3: 0.1, 4: 0.1}


@pytest.fixture
def fake_db(monkeypatch):
    import src.backtest.splits as splits
    import src.features.levels_store as levels_store

    candles = _candles()
    recorded: list[tuple] = []
    monkeypatch.setattr(levels_store, "get_connection", lambda: _Conn())
    monkeypatch.setattr(splits, "get_holdout_start", lambda **k: datetime(2100, 1, 1, tzinfo=UTC))
    monkeypatch.setattr(replay, "_fetch_all_candles",
                        lambda conn, sym, tf, a, b: candles if sym != "DXY@" else [])
    monkeypatch.setattr(replay, "_fetch_all_levels", lambda *a, **k: [dict(l) for l in LEVELS])
    monkeypatch.setattr(replay, "_fetch_all_levels_history", lambda *a, **k: {
        lid: [{"level_id": lid, "ts_utc": T0, "strength": s, "status": "active",
               "touch_count": 1, "break_count": 0}]
        for lid, s in STRENGTH.items()
    })
    for name in ("_fetch_all_regime", "_fetch_all_round_hits", "_fetch_all_fib_zones",
                 "_fetch_all_patterns", "_fetch_all_gaps", "_fetch_all_corr", "_fetch_all_matrix",
                 "_fetch_all_rsi_snapshots", "_fetch_all_divergence_events", "_fetch_all_memory",
                 "_fetch_all_news"):
        monkeypatch.setattr(replay, name, lambda *a, **k: [])
    monkeypatch.setattr(replay, "_insert_candidates", lambda conn, rows: recorded.extend(rows))
    return recorded


def test_fires_with_level_b_six_atr_away(fake_db):
    stats = replay.run_backtest(
        T0, T0 + timedelta(hours=10), dry_run=True, min_net_votes_override=0,
    )
    assert stats["candidates"] == 1
    assert stats["no_valid_sl_tp"] == 0, "level B ~6 ATR away must be visible (live parity)"
    assert stats["signals"] == 1
    assert stats["tp"] == 1


def test_vote_rejected_candidate_is_recorded_with_outcomes(fake_db):
    stats = replay.run_backtest(
        T0, T0 + timedelta(hours=10), dry_run=True, record_candidates="smoke",
    )
    # synthetic data has no module outputs -> every vote is 0 < min_net_votes=2
    assert stats["votes_rejected"] == 1 and stats["signals"] == 0
    assert stats["candidates_recorded"] == 1
    row = dict(zip(replay._CANDIDATE_COLS, fake_db[0]))
    assert row["run_tag"] == "smoke"
    assert row["gate"] == "votes_rejected"
    assert row["exit_mode"] == "level"
    assert row["outcome"] == "tp" and row["u_outcome"] == "tp"
    assert row["rr"] >= 2.0
    assert row["risk_usd"] > 0
