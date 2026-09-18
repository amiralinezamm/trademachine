"""Signal component enrichment — pure selection layer.

fetch_db_context() does the DB queries (one call per module store).
build_context() is pure: receives pre-fetched lists, applies as_of_ts
cutoff on every list, and returns a dict ready to be merged into the
signal's components jsonb (SPEC.md 4.14).

Splitting the two keeps CLAUDE.md rule 6 in effect: backtest can call
build_context() with exactly the same inputs the live path uses, since
both pull from the same pre-computed module tables.

Anti-lookahead guarantee: every list is filtered to ts_utc <= as_of_ts
inside build_context() before any value is read. The test in
tests/api/test_signal_components.py verifies this explicitly.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any


# ---------------------------------------------------------------------------
# Pure selection layer (testable without DB)
# ---------------------------------------------------------------------------

def build_context(
    *,
    regime_snaps: list[dict[str, Any]],
    round_hits: list[dict[str, Any]],
    fib_zones: list[dict[str, Any]],
    pattern_rows: list[dict[str, Any]],
    open_gaps: list[dict[str, Any]],
    corr_rows: list[dict[str, Any]],
    close_price: float,
    atr_value: float,
    as_of_ts: datetime,
) -> dict[str, Any]:
    """Pure: derive component context from pre-fetched module outputs.

    Each list is filtered to rows with ts_utc <= as_of_ts first (anti-
    lookahead). Absent data for a module returns None for that key so
    components is always schema-stable regardless of which modules have
    run so far.
    """
    ctx: dict[str, Any] = {}

    # --- regime ---
    valid_regime = [r for r in regime_snaps if r["ts_utc"] <= as_of_ts]
    if valid_regime:
        latest = max(valid_regime, key=lambda r: r["ts_utc"])
        ctx["regime"] = latest["regime"]
    else:
        ctx["regime"] = None

    # --- dollar correlation ---
    valid_corr = [r for r in corr_rows if r["ts_utc"] <= as_of_ts]
    if valid_corr:
        latest = max(valid_corr, key=lambda r: r["ts_utc"])
        ctx["dollar_corr"] = float(latest["correlation"])
    else:
        ctx["dollar_corr"] = None

    # --- nearest APPROACHING round number (the relevant state for signal engine) ---
    valid_rounds = [r for r in round_hits
                    if r["ts_utc"] <= as_of_ts and r["state"] in ("APPROACHING", "REVERSAL")]
    if valid_rounds:
        nearest = min(valid_rounds, key=lambda r: abs(float(r["level"]) - close_price))
        ctx["round"] = {
            "level": float(nearest["level"]),
            "multiplier": int(nearest.get("multiplier", 0)),
            "weight": float(nearest.get("weight", 0)),
            "state": nearest["state"],
            "distance_atr": abs(float(nearest["level"]) - close_price) / atr_value if atr_value else None,
        }
    else:
        ctx["round"] = None

    # --- fibonacci confluence: zone whose price is within 1×ATR of close ---
    valid_fib = [z for z in fib_zones if z.get("computed_at", as_of_ts) <= as_of_ts]
    fib_near = [z for z in valid_fib
                if atr_value and abs(float(z["price"]) - close_price) <= atr_value]
    if fib_near:
        # Prefer overlapping zones (SPEC 4.6: overlap = confluence with a level)
        overlapping = [z for z in fib_near if z.get("overlapping")]
        best = (overlapping or fib_near)[0]
        ctx["fib"] = {
            "level_pct": float(best["level_pct"]),
            "price": float(best["price"]),
            "role": best["role"],
            "overlapping": bool(best.get("overlapping")),
            "distance_atr": abs(float(best["price"]) - close_price) / atr_value if atr_value else None,
        }
    else:
        ctx["fib"] = None

    # --- most recent candlestick pattern at as_of_ts (the signal bar itself) ---
    # Prefer patterns at exactly the signal candle; fall back to the most recent bar.
    valid_pats = [p for p in pattern_rows if p["ts_utc"] <= as_of_ts]
    at_bar = [p for p in valid_pats if p["ts_utc"] == as_of_ts]
    recent_pats = at_bar or (sorted(valid_pats, key=lambda p: p["ts_utc"])[-1:])
    if recent_pats:
        # Summarise: count by direction and list names
        by_dir: dict[int, list[str]] = {}
        for p in recent_pats:
            d = int(p["direction"])
            by_dir.setdefault(d, []).append(p["pattern"])
        ctx["pattern"] = {
            "bullish": by_dir.get(100, []),
            "bearish": by_dir.get(-100, []),
            "ts_utc": recent_pats[0]["ts_utc"].isoformat()
                      if hasattr(recent_pats[0]["ts_utc"], "isoformat")
                      else str(recent_pats[0]["ts_utc"]),
        }
    else:
        ctx["pattern"] = None

    # --- nearest open/half-filled gap to close (already as_of_ts filtered upstream) ---
    if open_gaps:
        def gap_distance(g: dict[str, Any]) -> float:
            mid = (float(g["gap_high"]) + float(g["gap_low"])) / 2
            return abs(mid - close_price)
        nearest_gap = min(open_gaps, key=gap_distance)
        ctx["gap"] = {
            "direction": nearest_gap["direction"],
            "high": float(nearest_gap["gap_high"]),
            "low": float(nearest_gap["gap_low"]),
            "status": nearest_gap["status"],
            "weight": float(nearest_gap.get("weight", 0)),
            "distance_atr": gap_distance(nearest_gap) / atr_value if atr_value else None,
        }
    else:
        ctx["gap"] = None

    return ctx


# ---------------------------------------------------------------------------
# DB query layer — one call fetches everything, then build_context() selects
# ---------------------------------------------------------------------------

def fetch_db_context(
    conn,
    symbol: str,
    tf: str,
    ts_utc: datetime,
    close_price: float,
    atr_value: float,
) -> dict[str, Any]:
    """Fetch all module outputs from DB and call build_context()."""
    from src.features.regime_store import fetch_latest_regime, fetch_regime_snapshots
    from src.features.round_numbers_store import fetch_round_number_hits
    from src.features.fibonacci_store import fetch_fibonacci_zones
    from src.features.gaps_store import fetch_open_gaps
    from src.features.correlation_store import fetch_latest_dollar_correlation

    # regime snapshots — fetch_latest_regime returns one dict; for the pure
    # function we wrap it in a list so build_context works uniformly.
    regime_snap = fetch_latest_regime(conn, symbol, tf, ts_utc)
    regime_snaps = [regime_snap] if regime_snap else []

    # round number hits at or before ts_utc — limited to active states
    round_hits = fetch_round_number_hits(
        conn, symbol, tf, ts_utc, states=["APPROACHING", "REVERSAL"]
    )

    # fibonacci zones from the latest computed snapshot
    fib_zones = fetch_fibonacci_zones(conn, symbol, tf, ts_utc)

    # pattern hits: last 3 bars before and including ts_utc to catch the
    # signal bar and the bar immediately preceding it
    pattern_rows = _fetch_recent_pattern_rows(conn, symbol, tf, ts_utc, n_bars=3)

    # open gaps
    open_gaps = fetch_open_gaps(conn, symbol, tf, ts_utc)

    # dollar correlation
    corr = fetch_latest_dollar_correlation(conn, symbol, tf, ts_utc)
    corr_rows = [corr] if corr else []

    return build_context(
        regime_snaps=regime_snaps,
        round_hits=round_hits,
        fib_zones=fib_zones,
        pattern_rows=pattern_rows,
        open_gaps=open_gaps,
        corr_rows=corr_rows,
        close_price=close_price,
        atr_value=atr_value,
        as_of_ts=ts_utc,
    )


def _fetch_recent_pattern_rows(
    conn, symbol: str, tf: str, as_of_ts: datetime, n_bars: int
) -> list[dict[str, Any]]:
    """Pattern hits for the last n_bars M5 candles at or before as_of_ts."""
    from datetime import timedelta
    window_start = as_of_ts - timedelta(minutes=5 * n_bars)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ts_utc, tf, pattern, direction, body_atr,
                   at_level_id, level_strength, regime
            FROM pattern_hits
            WHERE tf = %s AND ts_utc >= %s AND ts_utc <= %s
            ORDER BY ts_utc, pattern
            """,
            (tf, window_start, as_of_ts),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
