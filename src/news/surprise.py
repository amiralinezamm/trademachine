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


def load_event_map(event_map_path: Path = EVENT_MAP_PATH) -> dict[str, dict[str, int]]:
    """Returns {alias_lowercased: {country_upper: gold_sign}}.

    Keyed by (alias, country) rather than alias alone: the SAME title (e.g.
    "CPI y/y", "GDP m/m", "Retail Sales m/m") carries the OPPOSITE gold_sign
    depending on currency (config/event_map.yaml header explains why) — a
    flat alias->gold_sign map would silently let whichever country's entry
    loaded last win for every other country's event of the same name.
    """
    with open(event_map_path) as f:
        data = yaml.safe_load(f)
    lookup: dict[str, dict[str, int]] = {}
    for entry in data["events"]:
        country = entry["country"].strip().upper()
        for alias in entry["aliases"]:
            lookup.setdefault(alias.strip().lower(), {})[country] = entry["gold_sign"]
    return lookup


def load_event_meta_map(event_map_path: Path = EVENT_MAP_PATH) -> dict[str, dict[str, dict]]:
    """Returns {alias_lowercased: {country_upper: {tier, usd_range, pct_range}}}.

    Companion to load_event_map() (2026-09-29): same keying by (alias,
    country) for the same reason (a title's magnitude differs by currency
    too, not just its gold_sign), but carries the expected-move metadata
    from config/event_map.yaml instead of gold_sign, so the digest can show
    "شدت حرکت" without guessing a number that isn't in the researched table."""
    with open(event_map_path) as f:
        data = yaml.safe_load(f)
    lookup: dict[str, dict[str, dict]] = {}
    for entry in data["events"]:
        country = entry["country"].strip().upper()
        meta = {
            "tier": entry.get("tier"),
            "usd_range": entry.get("usd_range"),
            "pct_range": entry.get("pct_range"),
        }
        for alias in entry["aliases"]:
            lookup.setdefault(alias.strip().lower(), {})[country] = meta
    return lookup


def lookup_event_meta(
    event_title: str, meta_map: dict[str, dict[str, dict]], country: str
) -> dict | None:
    """country is required here (unlike lookup_gold_sign) -- every call site
    already has it from the news_events row, and magnitude varies by
    currency just as much as direction does, so there's no legitimate
    country-less caller to support."""
    candidates = meta_map.get(event_title.strip().lower())
    if not candidates:
        return None
    return candidates.get(country.strip().upper())


def lookup_gold_sign(
    event_title: str, event_map: dict[str, dict[str, int]], country: str | None = None
) -> int | None:
    """country=None matches only if every country sharing this title agrees
    on the same gold_sign (e.g. a USD-only title) — otherwise, per the
    project's "don't guess" rule, this returns None rather than picking one
    currency's mapping for an event whose currency we don't actually know."""
    candidates = event_map.get(event_title.strip().lower())
    if not candidates:
        return None
    if country is not None:
        return candidates.get(country.strip().upper())
    signs = set(candidates.values())
    return signs.pop() if len(signs) == 1 else None


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
    event_map: dict[str, dict[str, int]] | None = None,
    min_samples_for_z: int | None = None,
    country: str | None = None,
) -> dict[str, Any]:
    """
    surprise      = actual - forecast
    z_surprise    = surprise / stdev(historical_surprises)   [only if >= min_samples_for_z]
    expected_dir  = sign(z_surprise) * gold_sign             [only if z_surprise available]
    raw_direction = sign(surprise) * gold_sign               [always, if event is mapped]

    historical_surprises must NOT include the current event's own surprise
    (CLAUDE.md rule 1 — no looking into data that includes the point itself).

    `country` disambiguates titles shared across currencies (see
    lookup_gold_sign) — pass it whenever the caller knows the event's
    currency (it always does; it's a column on news_events).
    """
    if event_map is None:
        event_map = load_event_map()
    if min_samples_for_z is None:
        min_samples_for_z = load_min_samples_for_z()

    gold_sign = lookup_gold_sign(event_title, event_map, country=country)
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
