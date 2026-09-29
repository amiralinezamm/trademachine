"""Pure analysis over bt_candidates rows (SPEC.md D24). No DB access here --
scripts/analyze_candidates.py does the fetching and printing.

Each row is a dict with at least: ts_utc, gate, exit_mode, votes (dict),
net_votes, outcome, pnl_usd, risk_usd, u_outcome, u_pnl_usd.

Two outcome columns, used for two different questions:
  - u_*  (uniform exit: level-A stop, TP at min_rr x risk, for EVERY
         candidate) -> "does this voter / gate pick better ENTRIES?"
         Comparable across candidates because every trade has the same R:R.
  - real (level exit when one existed) -> "what would the strategy as
         configured actually have made?" Only rows with exit_mode='level'
         are live-faithful.

Everything is reported with a sample size and a 95% interval. A number with
n < min_n is printed but never turned into a weight -- CLAUDE.md/SPEC.md's
own bar is >= 200 trades before a rule is considered verified.
"""
from __future__ import annotations

import math
from typing import Any, Iterable

Z95 = 1.96


def wilson_interval(wins: int, n: int, z: float = Z95) -> tuple[float, float] | None:
    """95% Wilson score interval for a win rate -- stays sane at small n and
    near 0/1, unlike the normal approximation."""
    if n <= 0:
        return None
    p = wins / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def _mean_ci(values: list[float], z: float = Z95) -> tuple[float, float, float] | None:
    n = len(values)
    if n == 0:
        return None
    m = sum(values) / n
    if n < 2:
        return m, m, m
    var = sum((v - m) ** 2 for v in values) / (n - 1)
    half = z * math.sqrt(var / n)
    return m, m - half, m + half


def r_multiple(pnl: float | None, risk: float | None) -> float | None:
    if pnl is None or not risk:
        return None
    return float(pnl) / float(risk)


def summarize(rows: Iterable[dict[str, Any]], uniform: bool) -> dict[str, Any]:
    """n, win rate (+ Wilson CI), mean R (+ CI), mean/total $ for rows that
    have an evaluated outcome under the chosen exit."""
    out_key, pnl_key = ("u_outcome", "u_pnl_usd") if uniform else ("outcome", "pnl_usd")
    evaluated = [r for r in rows if r.get(out_key) is not None and r.get(pnl_key) is not None]
    n = len(evaluated)
    wins = sum(1 for r in evaluated if r[out_key] == "tp")
    pnls = [float(r[pnl_key]) for r in evaluated]
    rs = [x for x in (r_multiple(r[pnl_key], r.get("risk_usd")) for r in evaluated) if x is not None]
    mean_r = _mean_ci(rs)
    return {
        "n": n,
        "wins": wins,
        "winrate": wins / n if n else None,
        "winrate_ci": wilson_interval(wins, n),
        "mean_r": mean_r[0] if mean_r else None,
        "mean_r_ci": (mean_r[1], mean_r[2]) if mean_r else None,
        "mean_usd": sum(pnls) / n if n else None,
        "total_usd": sum(pnls),
    }


def months_spanned(rows: list[dict[str, Any]]) -> float:
    ts = [r["ts_utc"] for r in rows if r.get("ts_utc") is not None]
    if len(ts) < 2:
        return 0.0
    return max((max(ts) - min(ts)).total_seconds() / 86400 / 30.44, 1e-9)


def funnel(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per gate: how many candidates stopped there and how good their
    entries were (uniform exit). If a gate's rejects score as well as the
    fired set, that gate is costing signals without adding quality."""
    out: dict[str, dict[str, Any]] = {}
    for gate in ("fired", "votes_rejected", "no_valid_sl_tp", "blackout"):
        subset = [r for r in rows if r.get("gate") == gate]
        out[gate] = summarize(subset, uniform=True)
    return out


def voter_table(rows: list[dict[str, Any]]) -> dict[str, dict[int, dict[str, Any]]]:
    """For each voter and each vote value (+1/0/-1): uniform-exit stats.
    Blackout candidates are excluded (live never trades them)."""
    tradable = [r for r in rows if r.get("gate") != "blackout"]
    names = sorted({name for r in tradable for name in (r.get("votes") or {})})
    table: dict[str, dict[int, dict[str, Any]]] = {}
    for name in names:
        table[name] = {}
        for v in (1, 0, -1):
            subset = [r for r in tradable if int((r.get("votes") or {}).get(name, 0)) == v]
            table[name][v] = summarize(subset, uniform=True)
    return table


def _logit(p: float) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


def suggest_weights(
    table: dict[str, dict[int, dict[str, Any]]],
    baseline_winrate: float,
    min_n: int = 200,
    prior_strength: float = 20.0,
) -> dict[str, dict[str, Any]]:
    """Naive-Bayes style evidence weights, in log-odds units.

    For vote value v with n trades and w wins, the shrunk win rate is
    (w + a*p0) / (n + a) with p0 = baseline and a = prior_strength -- small
    samples are pulled toward "no information". lift(v) = logit(shrunk) -
    logit(p0). A voter's weight is how much a +1 moves the odds relative to
    a -1: (lift(+1) - lift(-1)) / 2, using only the sides with n >= min_n.
    A side below min_n contributes nothing; both below -> weight 0 and
    reason says why. Never writes config -- output is for human review.
    """
    out: dict[str, dict[str, Any]] = {}
    for name, by_v in table.items():
        lifts: dict[int, float] = {}
        for v in (1, -1):
            s = by_v.get(v) or {}
            n = s.get("n", 0)
            if n >= min_n:
                shrunk = (s["wins"] + prior_strength * baseline_winrate) / (n + prior_strength)
                lifts[v] = _logit(shrunk) - _logit(baseline_winrate)
        if 1 in lifts and -1 in lifts:
            w, reason = (lifts[1] - lifts[-1]) / 2, "both sides"
        elif 1 in lifts:
            w, reason = lifts[1], "+1 side only"
        elif -1 in lifts:
            w, reason = -lifts[-1], "-1 side only"
        else:
            w, reason = 0.0, f"n < {min_n} on both sides -- no evidence yet"
        out[name] = {"weight": w, "reason": reason, "lifts": lifts}
    return out


def rescore(votes: dict[str, int], weights: dict[str, float]) -> float:
    return sum(float(v) * float(weights.get(k, 0.0)) for k, v in (votes or {}).items())


def threshold_sweep(
    rows: list[dict[str, Any]],
    thresholds: Iterable[float],
    weights: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """For each threshold t: what would firing every non-blackout candidate
    with score >= t have produced? score = stored net_votes, or a re-score
    with `weights`. Two views per t:
      live  -- only candidates that had a real level exit (what live could
               actually send), real P&L
      entry -- every candidate, uniform exit (entry quality alone)
    Spacing-filter interactions are not re-simulated (see replay docstring)."""
    tradable = [r for r in rows if r.get("gate") != "blackout"]
    months = months_spanned(tradable) or 1.0
    out = []
    for t in thresholds:
        if weights is None:
            picked = [r for r in tradable if float(r["net_votes"]) >= t]
        else:
            picked = [r for r in tradable if rescore(r.get("votes"), weights) >= t]
        live = summarize([r for r in picked if r.get("exit_mode") == "level"], uniform=False)
        entry = summarize(picked, uniform=True)
        out.append({
            "threshold": t,
            "live": live, "live_per_month": live["n"] / months,
            "entry": entry, "entry_per_month": entry["n"] / months,
        })
    return out
