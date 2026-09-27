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

from datetime import datetime, timedelta
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
    matrix_snaps: list[dict[str, Any]] | None = None,
    rsi_snaps: list[dict[str, Any]] | None = None,
    divergence_rows: list[dict[str, Any]] | None = None,
    divergence_recency_bars: int = 12,
    memory_result: dict | None = None,
    dxy_candles: list[dict[str, Any]] | None = None,
    dxy_direction_bars: int = 3,
) -> dict[str, Any]:
    """Pure: derive component context from pre-fetched module outputs.

    Each list is filtered to rows with ts_utc <= as_of_ts first (anti-
    lookahead). Absent data for a module returns None for that key so
    components is always schema-stable regardless of which modules have
    run so far.

    memory_result (D20-ext, 2026-09-25): pre-fetched memory module output dict
    (from fetch_latest_memory_result). Passed directly -- no ts_utc filter needed
    since it is itself fetched with as_of_ts by the store layer.

    dxy_candles (D21, 2026-09-26): DXY@ M5 candles up to as_of_ts (pre-fetched
    in replay, queried in fetch_db_context). Used to derive short-window DXY
    direction for the dollar_correlation_direction voter (SPEC.md 4.8-a).

    matrix_snaps/rsi_snaps/divergence_rows (D20, 2026-09-25): the proposed
    matrix (SPEC.md 4.10) and rsi_macd_divergence (SPEC.md 4.19) module
    outputs, added so module_voting_v1 can vote on them (params.yaml
    rules_registry.allow_proposed_in_voting). Optional/default-None so
    this stays backward compatible with any other caller.
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
                    if r["ts_utc"] <= as_of_ts
                    and r["state"] in ("APPROACHING", "REVERSAL", "ACCELERATION")]
    if valid_rounds:
        nearest = min(valid_rounds, key=lambda r: abs(float(r["level"]) - close_price))
        ctx["round"] = {
            "level": float(nearest["level"]),
            "multiplier": int(nearest.get("multiplier", 0)),
            "weight": float(nearest.get("weight", 0)),
            "state": nearest["state"],
            "direction": nearest.get("direction"),
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

    # --- matrix (SPEC.md 4.10, D18/D20 -- proposed) ---
    valid_matrix = [r for r in (matrix_snaps or []) if r["ts_utc"] <= as_of_ts]
    if valid_matrix:
        latest = max(valid_matrix, key=lambda r: r["ts_utc"])
        ctx["matrix"] = {"score": int(latest["score"]), "direction": latest["direction"]}
    else:
        ctx["matrix"] = None

    # --- rsi_macd_divergence (SPEC.md 4.19, D19/D20 -- proposed) ---
    valid_rsi = [r for r in (rsi_snaps or []) if r["ts_utc"] <= as_of_ts]
    rsi_snapshot = None
    if valid_rsi:
        latest = max(valid_rsi, key=lambda r: r["ts_utc"])
        rsi_snapshot = {
            "rsi": float(latest["rsi"]), "rsi_state": latest["rsi_state"],
            "macd": float(latest["macd"]), "macd_signal": float(latest["macd_signal"]),
        }

    # A divergence event is only "active" for voting within a recency
    # window (DESIGN DECISION, params.yaml module_voting.divergence_recency_bars
    # -- SPEC.md does not say how long a confirmed divergence stays
    # actionable; see that param's comment).
    recency_cutoff = as_of_ts - timedelta(minutes=5 * divergence_recency_bars)
    valid_div = [
        d for d in (divergence_rows or [])
        if d["confirmed_ts"] <= as_of_ts and d["confirmed_ts"] >= recency_cutoff
    ]

    def _most_recent(kind: str) -> dict[str, Any] | None:
        matches = sorted((d for d in valid_div if d["kind"] == kind), key=lambda d: d["confirmed_ts"])
        if not matches:
            return None
        m = matches[-1]
        return {"direction": m["direction"], "confirmed_ts": m["confirmed_ts"].isoformat()
                if hasattr(m["confirmed_ts"], "isoformat") else str(m["confirmed_ts"])}

    ctx["rsi"] = {
        "snapshot": rsi_snapshot,
        "recent_price_rsi_divergence": _most_recent("price_rsi"),
        "recent_price_macd_divergence": _most_recent("price_macd"),
    }

    # --- DXY direction (SPEC.md 4.8-a, D21 -- proposed) ---
    # Short-window direction: compare the most-recent DXY close against the
    # close dxy_direction_bars bars prior.  Only candles at or before as_of_ts
    # are used (anti-lookahead).
    valid_dxy = [c for c in (dxy_candles or []) if c["ts_utc"] <= as_of_ts]
    if len(valid_dxy) >= dxy_direction_bars + 1:
        valid_dxy_sorted = sorted(valid_dxy, key=lambda c: c["ts_utc"])
        dxy_now  = float(valid_dxy_sorted[-1]["close"])
        dxy_prev = float(valid_dxy_sorted[-(dxy_direction_bars + 1)]["close"])
        ctx["dxy_direction"] = "up" if dxy_now > dxy_prev else ("down" if dxy_now < dxy_prev else "flat")
    else:
        ctx["dxy_direction"] = None

    # --- memory (SPEC.md 4.10, D20-ext -- proposed) ---
    if memory_result:
        ctx["memory"] = {
            "up_ratio":      memory_result.get("up_ratio"),
            "median_return": memory_result.get("median_return"),
            "n_matches":     memory_result.get("n_matches", 0),
            "ci_low":        memory_result.get("ci_low"),
            "ci_high":       memory_result.get("ci_high"),
        }
    else:
        ctx["memory"] = None

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
    divergence_recency_bars: int = 12,
    memory_result: dict | None = None,
) -> dict[str, Any]:
    """Fetch all module outputs from DB and call build_context()."""
    from src.features.regime_store import fetch_latest_regime, fetch_regime_snapshots
    from src.features.round_numbers_store import fetch_round_number_hits
    from src.features.fibonacci_store import fetch_fibonacci_zones
    from src.features.gaps_store import fetch_open_gaps
    from src.features.correlation_store import fetch_latest_dollar_correlation
    from src.features.matrix_store import fetch_latest_matrix_snapshot
    from src.features.rsi_store import fetch_latest_snapshot, fetch_recent_divergences
    from src.features.memory_store import fetch_latest_memory_result

    # regime snapshots — fetch_latest_regime returns one dict; for the pure
    # function we wrap it in a list so build_context works uniformly.
    regime_snap = fetch_latest_regime(conn, symbol, tf, ts_utc)
    regime_snaps = [regime_snap] if regime_snap else []

    # round number hits at or before ts_utc — limited to active states
    round_hits = fetch_round_number_hits(
        conn, symbol, tf, ts_utc, states=["APPROACHING", "REVERSAL", "ACCELERATION"]
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

    # matrix (SPEC.md 4.10, D20)
    matrix_snap = fetch_latest_matrix_snapshot(conn, symbol, ts_utc)
    matrix_snaps = [matrix_snap] if matrix_snap else []

    # rsi_macd_divergence (SPEC.md 4.19, D20)
    rsi_snap = fetch_latest_snapshot(conn, symbol, tf, ts_utc)
    rsi_snaps = [rsi_snap] if rsi_snap else []
    divergence_rows = (
        fetch_recent_divergences(conn, symbol, tf, ts_utc, kind="price_rsi", limit=3)
        + fetch_recent_divergences(conn, symbol, tf, ts_utc, kind="price_macd", limit=3)
    )

    # memory (D20-ext)
    memory_result = fetch_latest_memory_result(conn, symbol, tf, ts_utc)

    # DXY direction (D21, SPEC.md 4.8-a)
    import yaml as _yaml
    from pathlib import Path as _Path
    _cfg = _yaml.safe_load(open(_Path(__file__).resolve().parents[2] / "config" / "params.yaml"))["correlation"]
    _dxy_bars = int(_cfg.get("dxy_direction_bars", 3))
    with conn.cursor() as _cur:
        _cur.execute(
            """
            SELECT ts_utc, close
            FROM candles
            WHERE symbol = 'DXY@' AND tf = 'M5' AND ts_utc <= %s
            ORDER BY ts_utc DESC
            LIMIT %s
            """,
            (ts_utc, _dxy_bars + 1),
        )
        _cols = [d[0] for d in _cur.description]
        dxy_candles = [dict(zip(_cols, row)) for row in _cur.fetchall()]

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
        matrix_snaps=matrix_snaps,
        rsi_snaps=rsi_snaps,
        divergence_rows=divergence_rows,
        divergence_recency_bars=divergence_recency_bars,
        memory_result=memory_result,
        dxy_candles=dxy_candles,
        dxy_direction_bars=_dxy_bars,
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
