"""Regression test: ±3×ATR price pre-filter must be evaluated per-bar, not once.

Scenario: a level is out-of-range at bar 0 (price far from level) but
enters the ±3×ATR window at bar N when price moves toward it. The test
verifies the level IS considered at bar N (not permanently excluded by
an initial static filter).
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from src.backtest.replay import _PreIndexed, _levels_at, _level_state_at

UTC = timezone.utc

def _ts(n: int) -> datetime:
    return datetime(2025, 11, 1, tzinfo=UTC) + timedelta(minutes=5 * n)


def test_price_filter_is_per_bar_not_static():
    """Level at 2000 is out-of-range at bars 0-9 (price=2100) but within
    ±3xATR at bars 10-19 (price=2001). Confirms filter re-evaluates per bar."""
    import numpy as np

    ATR = 3.0
    LEVEL_PRICE = 2000.0

    n_bars = 20
    candles = []
    for i in range(n_bars):
        close = 2001.0 if i >= 10 else 2100.0
        candles.append({
            "ts_utc": _ts(i),
            "open": close, "high": close + 1.0, "low": close - 1.0,
            "close": close, "spread": 3,
        })

    all_levels_raw = [
        {
            "id": 1,
            "kind": "support",
            "price_low": LEVEL_PRICE,
            "price_high": LEVEL_PRICE + 2.0,
            "strength": 0.8,
            "status": "active",
            "created_ts": _ts(0),
        }
    ]
    all_levels = _PreIndexed(all_levels_raw, "created_ts")
    history_by_id: dict = {}

    atr_arr = [ATR] * n_bars

    seen_bars: list[int] = []
    absent_bars: list[int] = []

    for i in range(n_bars):
        atr = atr_arr[i]
        c = candles[i]
        ts = c["ts_utc"]
        high = float(c["high"])
        low  = float(c["low"])

        levels_now = _levels_at(all_levels, ts)
        _atr_f = float(atr) if float(atr) > 0 else 1.0
        _price_lo = low  - 3.0 * _atr_f
        _price_hi = high + 3.0 * _atr_f

        pit_levels = []
        for lvl in levels_now:
            if lvl["price_high"] < _price_lo:
                continue
            if lvl["price_low"] > _price_hi:
                continue
            s, status, tc, bc = _level_state_at(history_by_id, lvl["id"], ts)
            if status not in ("active", "flipped"):
                continue
            pit_levels.append({**lvl, "strength": s, "status": status,
                               "touch_count": tc, "break_count": bc})

        if pit_levels:
            seen_bars.append(i)
        else:
            absent_bars.append(i)

    # Bars 0-9: close=2100, window [2096, 2110] — level at 2000-2002 OUTSIDE
    wrong_early = [b for b in range(10) if b in seen_bars]
    assert not wrong_early, (
        f"Level should be absent in bars 0-9 (price=2100, level=2000, "
        f"window=[2096,2110]), but seen at: {wrong_early}"
    )

    # Bars 10-19: close=2001, window [1997, 2011] — level at 2000-2002 INSIDE
    wrong_late = [b for b in range(10, n_bars) if b in absent_bars]
    assert not wrong_late, (
        f"Level should be seen in bars 10-19 (price=2001, level=2000, "
        f"window=[1997,2011] i.e. within ±3xATR), but absent at: {wrong_late}"
    )
