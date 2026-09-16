"""SPEC.md 4.10 — Pattern-memory module using STUMPY Matrix Profile.

Finds the K=48-bar window ending at as_of_ts in the historical corpus,
locates the top_n most similar past windows via stumpy.match(), reads
the subsequent H=12-bar forward return for each match, and computes a
bootstrap confidence interval on the return distribution.

Anti-lookahead / embargo:
  The corpus excludes the last (K + H) candles relative to as_of_ts.
  This means no match window can start where its H-bar horizon would
  extend into the future or overlap the query window itself.

DESIGN DECISIONS (SPEC unspecified):
  - CI level: 95% (config/params.yaml, key ci_level)
  - n_bootstrap: 1000 (config/params.yaml)
  - Return metric: (close[match_start + K + H] - close[match_start + K]) /
                   close[match_start + K]  (simple percent over horizon H)
"""
from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import yaml

PARAMS_PATH = Path(__file__).resolve().parents[2] / "config" / "params.yaml"


def load_memory_params(path: Path = PARAMS_PATH) -> dict[str, Any]:
    with open(path) as f:
        return yaml.safe_load(f)["memory"]


def _z_normalize(arr: np.ndarray) -> np.ndarray:
    std = arr.std()
    if std < 1e-10:
        return np.zeros_like(arr)
    return (arr - arr.mean()) / std


def compute_memory(
    candles: list[dict[str, Any]],
    as_of_ts: datetime,
    symbol: str,
    tf: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Pure function. Returns None if not enough history.

    Result dict keys:
      symbol, tf_origin, computed_at, n_matches,
      up_ratio, median_return, ci_low, ci_high, raw_returns (list[float])
    """
    import stumpy  # lazy import — heavy, not needed at module load time

    if params is None:
        params = load_memory_params()

    K   = params["window_k"]
    H   = params["horizon_h"]
    N   = params["top_n"]
    ci  = params["ci_level"]
    n_b = params["n_bootstrap"]

    hist = sorted(
        (c for c in candles if c["ts_utc"] <= as_of_ts),
        key=lambda c: c["ts_utc"],
    )
    total = len(hist)
    # Need at least: embargo window (K+H) + K for query + K for at least 1 match
    if total < 3 * K + H:
        return None

    closes = np.array([float(c["close"]) for c in hist], dtype=np.float64)

    # Query: last K bars ending at as_of_ts
    query = _z_normalize(closes[-K:])

    # Corpus: everything EXCEPT the last K+H bars (embargo zone)
    embargo_start = total - (K + H)
    corpus = closes[:embargo_start]

    if len(corpus) < K:
        return None

    # stumpy.match returns (distance, index) for each match, sorted by distance.
    # The exclusion zone is handled by the embargo — corpus already excludes the
    # dangerous tail, so we don't need stumpy's built-in exclusion zone here.
    matches = stumpy.match(
        query,
        corpus,
        max_matches=N,
        normalize=True,
    )

    # Each match: matches[i] = (distance, start_idx_in_corpus)
    # Forward return: from corpus[start_idx + K] to corpus[start_idx + K + H]
    # Both indices must be valid within the FULL closes array (corpus is a prefix).
    raw_returns: list[float] = []
    for _dist, start_idx in matches:
        idx_entry = int(start_idx) + K      # close at end of matched window
        idx_exit  = idx_entry + H           # close H bars later
        if idx_exit >= total:
            continue  # safety: horizon would reach beyond available data
        entry_close = closes[idx_entry]
        exit_close  = closes[idx_exit]
        if entry_close <= 0:
            continue
        ret = float((exit_close - entry_close) / entry_close)
        raw_returns.append(ret)

    if not raw_returns:
        return None

    arr = np.array(raw_returns)
    up_ratio = float(np.mean(arr > 0))
    median_return = float(np.median(arr))

    # Bootstrap CI on the mean return
    rng = np.random.default_rng(seed=42)
    boot_means = np.array([
        rng.choice(arr, size=len(arr), replace=True).mean()
        for _ in range(n_b)
    ])
    alpha = 1 - ci
    ci_low  = float(np.percentile(boot_means, 100 * alpha / 2))
    ci_high = float(np.percentile(boot_means, 100 * (1 - alpha / 2)))

    return {
        "symbol":        symbol,
        "tf_origin":     tf,
        "computed_at":   as_of_ts,
        "n_matches":     len(raw_returns),
        "up_ratio":      round(up_ratio, 4),
        "median_return": round(median_return, 6),
        "ci_low":        round(ci_low, 6),
        "ci_high":       round(ci_high, 6),
        "raw_returns":   [round(r, 6) for r in raw_returns],
    }
