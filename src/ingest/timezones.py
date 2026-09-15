"""Broker (WM Markets MT5) server time -> UTC.

The MT5 Python API returns candle/tick times as epoch seconds that, when
read naively, give the BROKER's wall-clock reading — not UTC (CLAUDE.md
rule 4 requires UTC storage; this is the conversion boundary).

The DST rule below is not assumed from a named timezone (no "EET", no
"NY+7") — it was derived by scanning every weekend gap in the full
2019-2026 H1 history for the broker's own 1-hour anomaly, and it matched,
with zero exceptions across 7 years:
  - "summer" (dst) offset starts the weekend of the US 2nd-Sunday-of-March
  - "summer" (dst) offset ends the weekend of the EU last-Sunday-of-October
This is an unusual hybrid (not plain EET, not plain "NY+offset"), which is
exactly why it's implemented as measured rather than guessed to fit a
known zone name. See docs/data_depth.md for the investigation.

Because the market is closed during the transition weekend, no traded
candle ever falls in the ambiguous hour itself — classifying by calendar
date alone is safe.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

PARAMS_PATH = Path(__file__).resolve().parents[2] / "config" / "params.yaml"


def load_mt5_params(path: Path = PARAMS_PATH) -> dict[str, Any]:
    with open(path) as f:
        return yaml.safe_load(f)["mt5_bridge"]


def _nth_sunday(year: int, month: int, n: int) -> date:
    d = date(year, month, 1)
    first_sunday = d + timedelta(days=(6 - d.weekday()) % 7)
    return first_sunday + timedelta(weeks=n - 1)


def _last_sunday(year: int, month: int) -> date:
    next_month = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    d = next_month - timedelta(days=1)
    while d.weekday() != 6:
        d -= timedelta(days=1)
    return d


def is_dst_active(local_date: date) -> bool:
    """DST window is [2nd Sunday of March, last Sunday of October) for that year."""
    dst_start = _nth_sunday(local_date.year, 3, 2)
    dst_end = _last_sunday(local_date.year, 10)
    return dst_start <= local_date < dst_end


def broker_naive_to_utc(naive_broker_dt: datetime, params: dict | None = None) -> datetime:
    """naive_broker_dt: a naive datetime holding the broker's wall-clock reading
    (e.g. datetime.utcfromtimestamp(mt5_epoch), which is naive and NOT actually UTC
    despite the function name — that mislabeling is exactly the bug this module exists
    to avoid). Returns a UTC-aware datetime.
    """
    if params is None:
        params = load_mt5_params()
    tz_cfg = params["server_timezone"]
    offset_hours = tz_cfg["dst_offset_hours"] if is_dst_active(naive_broker_dt.date()) else tz_cfg["standard_offset_hours"]
    return (naive_broker_dt - timedelta(hours=offset_hours)).replace(tzinfo=timezone.utc)


def broker_epoch_to_utc(epoch_seconds: int, params: dict | None = None) -> datetime:
    """MT5's rates['time'] / tick.time: epoch seconds whose UTC-labeled reading
    IS the broker's wall clock (not true UTC)."""
    naive_broker_dt = datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).replace(tzinfo=None)
    return broker_naive_to_utc(naive_broker_dt, params)
