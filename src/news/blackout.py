"""SPEC.md 4.7-a — pre/post news blackout window computation.

compute_blackout() is pure (no DB, no network) so backtest and live code call
the exact same function (CLAUDE.md rule 6) and it can be asked about any past
instant via as_of_ts (CLAUDE.md rule 1). get_blackout_status() is the thin DB
wrapper used by the API.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

PARAMS_PATH = Path(__file__).resolve().parents[2] / "config" / "params.yaml"

_DEGREE_KEY = {"High": "degree_1", "Medium": "degree_2"}


def load_params(path: Path = PARAMS_PATH) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def compute_blackout(
    as_of_ts: datetime,
    events: list[dict[str, Any]],
    params: dict | None = None,
) -> dict[str, Any]:
    """
    events: list of {"title": str, "impact": "High"|"Medium", "ts_utc": aware datetime}
    Returns {"blackout": bool, "window_start", "window_end", "events": [...]}.
    No blackout => blackout False and the rest None/empty — never a conservative
    default (SPEC.md 4.7-a: "اگر خبر مهمی در پنجره نیست، هیچ احتیاطی لازم نیست").
    """
    if params is None:
        params = load_params()
    cfg = params["news"]["blackout"]
    cluster_window = timedelta(minutes=cfg["consensus"]["cluster_window_min"])
    multiplier = cfg["consensus"]["multiplier"]

    relevant = [e for e in events if e.get("impact") in _DEGREE_KEY]

    intervals: list[tuple[datetime, datetime, dict]] = []
    for event in relevant:
        degree_cfg = cfg[_DEGREE_KEY[event["impact"]]]
        before = timedelta(minutes=degree_cfg["before_min"])
        after = timedelta(minutes=degree_cfg["after_min"])

        has_cluster = any(
            other is not event and abs(other["ts_utc"] - event["ts_utc"]) <= cluster_window
            for other in relevant
        )
        if has_cluster:
            before *= multiplier
            after *= multiplier

        intervals.append((event["ts_utc"] - before, event["ts_utc"] + after, event))

    # Union of intervals — never sum windows separately (SPEC.md explicit instruction).
    intervals.sort(key=lambda iv: iv[0])
    merged: list[tuple[datetime, datetime, list[dict]]] = []
    for start, end, event in intervals:
        if merged and start <= merged[-1][1]:
            prev_start, prev_end, prev_events = merged[-1]
            merged[-1] = (prev_start, max(prev_end, end), prev_events + [event])
        else:
            merged.append((start, end, [event]))

    for start, end, events_in_window in merged:
        if start <= as_of_ts <= end:
            return {
                "blackout": True,
                "window_start": start,
                "window_end": end,
                "events": [
                    {"title": e["title"], "impact": e["impact"], "ts_utc": e["ts_utc"]}
                    for e in events_in_window
                ],
            }

    return {"blackout": False, "window_start": None, "window_end": None, "events": []}


async def fetch_relevant_events(
    pool,
    as_of_ts: datetime,
    lookback: timedelta = timedelta(hours=2),
    lookahead: timedelta = timedelta(hours=2),
) -> list[dict[str, Any]]:
    rows = await pool.fetch(
        """
        SELECT title, impact, ts_utc FROM news_events
        WHERE impact IN ('High', 'Medium')
          AND ts_utc BETWEEN $1 AND $2
        """,
        as_of_ts - lookback,
        as_of_ts + lookahead,
    )
    return [dict(r) for r in rows]


async def get_blackout_status(pool, as_of_ts: datetime, params: dict | None = None) -> dict[str, Any]:
    events = await fetch_relevant_events(pool, as_of_ts)
    return compute_blackout(as_of_ts, events, params=params)
