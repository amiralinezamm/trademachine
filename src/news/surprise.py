"""SPEC.md 4.7-b(2) — surprise / expected-direction calculation. Pure logic
only; no live `actual` data source exists yet (the feed doesn't provide one —
deferred to the delivery phase), so this operates purely on numbers passed in.
"""
from __future__ import annotations

import statistics
from pathlib import Path
from typing import Any

import yaml

PARAMS_PATH = Path(__file__).resolve().parents[2] / "config" / "params.yaml"
EVENT_MAP_PATH = Path(__file__).resolve().parents[2] / "config" / "event_map.yaml"


def load_min_samples_for_z(params_path: Path = PARAMS_PATH) -> int:
    with open(params_path) as f:
        params = yaml.safe_load(f)
    return params["news"]["surprise"]["min_samples_for_z"]


def load_event_map(event_map_path: Path = EVENT_MAP_PATH) -> dict[str, int]:
    """Returns {alias_lowercased: gold_sign}."""
    with open(event_map_path) as f:
        data = yaml.safe_load(f)
    lookup = {}
    for entry in data["events"]:
        for alias in entry["aliases"]:
            lookup[alias.strip().lower()] = entry["gold_sign"]
    return lookup


def lookup_gold_sign(event_title: str, event_map: dict[str, int]) -> int | None:
    return event_map.get(event_title.strip().lower())


def _sign(x: float) -> int:
    if x > 0:
        return 1
    if x < 0:
        return -1
    return 0


def compute_surprise(
    event_title: str,
    actual: float,
    forecast: float,
    historical_surprises: list[float],
    event_map: dict[str, int] | None = None,
    min_samples_for_z: int | None = None,
) -> dict[str, Any]:
    """
    surprise      = actual - forecast
    z_surprise    = surprise / stdev(historical_surprises)   [only if >= min_samples_for_z]
    expected_dir  = sign(z_surprise) * gold_sign             [only if z_surprise available]
    raw_direction = sign(surprise) * gold_sign               [always, if event is mapped]

    historical_surprises must NOT include the current event's own surprise
    (CLAUDE.md rule 1 — no looking into data that includes the point itself).
    """
    if event_map is None:
        event_map = load_event_map()
    if min_samples_for_z is None:
        min_samples_for_z = load_min_samples_for_z()

    gold_sign = lookup_gold_sign(event_title, event_map)
    surprise = actual - forecast

    result: dict[str, Any] = {
        "event_title": event_title,
        "surprise": surprise,
        "gold_sign": gold_sign,
        "raw_direction": None,
        "z_surprise": None,
        "expected_dir": None,
        "mapped": gold_sign is not None,
        "sample_count": len(historical_surprises),
    }

    if gold_sign is not None:
        result["raw_direction"] = _sign(surprise) * gold_sign

    if len(historical_surprises) >= min_samples_for_z:
        sigma = statistics.stdev(historical_surprises)
        if sigma > 0:
            z = surprise / sigma
            result["z_surprise"] = z
            if gold_sign is not None:
                result["expected_dir"] = _sign(z) * gold_sign

    return result
