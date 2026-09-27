"""PHASE3-PANEL-PLAN.md §7 قدم ۲ — Admin API with session auth.

Login / logout at /admin/api/login and /admin/api/logout (open).
All other /admin/api/* endpoints require a valid session cookie.
Rate limit: 5 login attempts per IP per 15 minutes (in-memory).
"""
from __future__ import annotations

import os
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import bcrypt
from fastapi import APIRouter, Cookie, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from itsdangerous import BadSignature, SignatureExpired, TimestampSigner
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from src.features.levels_store import get_connection
from src.news.blackout import compute_blackout, load_params as load_blackout_params

router = APIRouter(prefix="/admin/api")

_SYMBOL = "XAUUSD@"
ALLOWED_TF = {"M1", "M5", "M15", "H1"}
ALLOWED_STATUS = {"active", "broken", "flipped", "expired"}
_TF_MAX_SPAN: dict[str, int] = {
    "M1":  7   * 86_400,
    "M5":  90  * 86_400,
    "M15": 180 * 86_400,
    "H1":  730 * 86_400,
}
_DEFAULT_MAX_SPAN = _TF_MAX_SPAN["H1"]

_USERNAME = "amirali.nezam"


# ---------------------------------------------------------------------------
# Session auth
# ---------------------------------------------------------------------------

def _signer() -> TimestampSigner:
    secret = os.environ.get("PANEL_SESSION_SECRET", "")
    if not secret:
        raise RuntimeError("PANEL_SESSION_SECRET env var not set")
    return TimestampSigner(secret)


def require_session(session: Optional[str] = Cookie(default=None)) -> None:
    if not session:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        _signer().unsign(session, max_age=604_800)
    except (BadSignature, SignatureExpired):
        raise HTTPException(status_code=401, detail="Invalid or expired session")


_AUTH = [Depends(require_session)]


# ---------------------------------------------------------------------------
# Rate limiter (in-memory, single-process)
# ---------------------------------------------------------------------------

_login_attempts: dict[str, list[float]] = defaultdict(list)
_RATE_WINDOW = 15 * 60  # seconds
_RATE_MAX = 5


def _check_login_rate(ip: str) -> None:
    now = time.time()
    prev = [t for t in _login_attempts[ip] if now - t < _RATE_WINDOW]
    if len(prev) >= _RATE_MAX:
        raise HTTPException(
            status_code=429,
            detail="Too many login attempts. Try again in 15 minutes.",
        )
    prev.append(now)
    _login_attempts[ip] = prev


# ---------------------------------------------------------------------------
# POST /admin/api/login
# ---------------------------------------------------------------------------

class _LoginBody(BaseModel):
    username: str
    password: str


@router.post("/login")
async def admin_login(body: _LoginBody, request: Request) -> JSONResponse:
    # Use X-Real-IP set by nginx so rate-limit is per real client, not per proxy
    ip = (
        request.headers.get("X-Real-IP")
        or (request.client.host if request.client else "unknown")
    )
    _check_login_rate(ip)

    stored_hash = os.environ.get("PANEL_PASSWORD_HASH", "")
    if not stored_hash:
        raise HTTPException(status_code=500, detail="Server misconfigured")

    username_ok = body.username == _USERNAME
    password_ok = await run_in_threadpool(
        bcrypt.checkpw, body.password.encode(), stored_hash.encode()
    )

    if not (username_ok and password_ok):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token: bytes = await run_in_threadpool(_signer().sign, _USERNAME)
    response = JSONResponse({"ok": True})
    response.set_cookie(
        key="session",
        value=token.decode(),
        httponly=True,
        secure=True,
        samesite="strict",
        max_age=604_800,
    )
    return response


# ---------------------------------------------------------------------------
# POST /admin/api/logout
# ---------------------------------------------------------------------------

@router.post("/logout")
async def admin_logout() -> JSONResponse:
    response = JSONResponse({"ok": True})
    response.delete_cookie(
        key="session",
        httponly=True,
        secure=True,
        samesite="strict",
    )
    return response


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _epoch(dt: datetime | None) -> int | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _from_epoch(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def _check_tf(tf: str) -> None:
    if tf not in ALLOWED_TF:
        raise HTTPException(400, detail=f"tf must be one of {sorted(ALLOWED_TF)}")


def _check_span(from_ts: int, to_ts: int, max_span: int) -> None:
    span = to_ts - from_ts
    if span <= 0:
        raise HTTPException(400, detail="from must be less than to")
    if span > max_span:
        raise HTTPException(
            400,
            detail=f"Range {span}s exceeds limit {max_span}s ({max_span // 86_400} days)",
        )


# ---------------------------------------------------------------------------
# GET /admin/api/chart/candles
# ---------------------------------------------------------------------------

def _candles_sync(tf: str, from_ts: int, to_ts: int) -> dict[str, Any]:
    dt_from = _from_epoch(from_ts)
    dt_to = _from_epoch(to_ts)
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT ts_utc, open, high, low, close, tick_volume, spread
            FROM candles
            WHERE symbol = %s AND tf = %s
              AND ts_utc >= %s AND ts_utc <= %s
            ORDER BY ts_utc
            """,
            (_SYMBOL, tf, dt_from, dt_to),
        )
        rows = cur.fetchall()
    finally:
        conn.close()
    return {
        "tf": tf,
        "count": len(rows),
        "candles": [
            {
                "t": _epoch(r[0]),
                "o": float(r[1]), "h": float(r[2]),
                "l": float(r[3]), "c": float(r[4]),
                "v": r[5], "sp": r[6],
            }
            for r in rows
        ],
    }


@router.get("/chart/candles", dependencies=_AUTH)
async def chart_candles(
    tf: str = Query(...),
    from_: int = Query(..., alias="from"),
    to: int = Query(...),
):
    _check_tf(tf)
    _check_span(from_, to, _TF_MAX_SPAN[tf])
    return await run_in_threadpool(_candles_sync, tf, from_, to)


# ---------------------------------------------------------------------------
# GET /admin/api/chart/levels
# ---------------------------------------------------------------------------

def _levels_sync(from_ts: int, to_ts: int, statuses: list[str]) -> dict[str, Any]:
    dt_to = _from_epoch(to_ts)
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, kind, price_low, price_high, created_ts,
                   status, strength, touch_count, break_count, atr_at_birth
            FROM levels
            WHERE symbol = %s AND created_ts <= %s AND status = ANY(%s)
            ORDER BY created_ts
            """,
            (_SYMBOL, dt_to, statuses),
        )
        rows = cur.fetchall()
    finally:
        conn.close()
    return {
        "levels": [
            {
                "id": r[0],
                "kind": r[1],
                "lo": float(r[2]),
                "hi": float(r[3]),
                "created": _epoch(r[4]),
                "ended": None,
                "status": r[5],
                "strength": float(r[6]) if r[6] is not None else 0.0,
                "touches": r[7] or 0,
                "breaks": r[8] or 0,
                "double_touch": (r[7] or 0) >= 2,
                "atr_birth": float(r[9]) if r[9] is not None else None,
            }
            for r in rows
        ]
    }


@router.get("/chart/levels", dependencies=_AUTH)
async def chart_levels(
    from_: int = Query(..., alias="from"),
    to: int = Query(...),
    status: str = Query("active,broken,flipped,expired"),
):
    statuses = [s.strip() for s in status.split(",") if s.strip()]
    invalid = set(statuses) - ALLOWED_STATUS
    if invalid:
        raise HTTPException(400, detail=f"invalid status: {invalid}. Allowed: {ALLOWED_STATUS}")
    _check_span(from_, to, _DEFAULT_MAX_SPAN)
    return await run_in_threadpool(_levels_sync, from_, to, statuses)


# ---------------------------------------------------------------------------
# GET /admin/api/chart/signals
# ---------------------------------------------------------------------------

def _signals_sync(from_ts: int, to_ts: int) -> dict[str, Any]:
    dt_from = _from_epoch(from_ts)
    dt_to = _from_epoch(to_ts)
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, ts_utc, direction, entry, stop_loss, take_profit,
                   confidence, outcome, pnl_usd, rule_version, components
            FROM signals
            WHERE ts_utc >= %s AND ts_utc <= %s
            ORDER BY ts_utc
            """,
            (dt_from, dt_to),
        )
        rows = cur.fetchall()
    finally:
        conn.close()
    return {
        "signals": [
            {
                "id": r[0],
                "ts": _epoch(r[1]),
                "dir": r[2],
                "entry": float(r[3]) if r[3] is not None else None,
                "sl": float(r[4]) if r[4] is not None else None,
                "tp": float(r[5]) if r[5] is not None else None,
                "conf": float(r[6]) if r[6] is not None else None,
                "outcome": r[7],
                "pnl": float(r[8]) if r[8] is not None else None,
                "rule_version": r[9],
                "components": r[10],
            }
            for r in rows
        ]
    }


@router.get("/chart/signals", dependencies=_AUTH)
async def chart_signals(
    from_: int = Query(..., alias="from"),
    to: int = Query(...),
):
    _check_span(from_, to, _DEFAULT_MAX_SPAN)
    return await run_in_threadpool(_signals_sync, from_, to)


# ---------------------------------------------------------------------------
# GET /admin/api/chart/patterns
# ---------------------------------------------------------------------------

def _patterns_sync(tf: str, from_ts: int, to_ts: int, min_body_atr: float) -> dict[str, Any]:
    dt_from = _from_epoch(from_ts)
    dt_to = _from_epoch(to_ts)
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT ts_utc, pattern, direction, body_atr,
                   at_level_id, level_strength, regime
            FROM pattern_hits
            WHERE tf = %s AND ts_utc >= %s AND ts_utc <= %s
              AND (body_atr IS NULL OR body_atr >= %s)
            ORDER BY ts_utc
            """,
            (tf, dt_from, dt_to, min_body_atr),
        )
        rows = cur.fetchall()
    finally:
        conn.close()
    return {
        "hits": [
            {
                "ts": _epoch(r[0]),
                "pattern": r[1],
                "dir": r[2],
                "body_atr": float(r[3]) if r[3] is not None else None,
                "level_id": r[4],
                "level_strength": float(r[5]) if r[5] is not None else None,
                "regime": r[6],
            }
            for r in rows
        ]
    }


@router.get("/chart/patterns", dependencies=_AUTH)
async def chart_patterns(
    tf: str = Query(...),
    from_: int = Query(..., alias="from"),
    to: int = Query(...),
    min_body_atr: float = Query(0.0),
):
    _check_tf(tf)
    _check_span(from_, to, _TF_MAX_SPAN[tf])
    return await run_in_threadpool(_patterns_sync, tf, from_, to, min_body_atr)


# ---------------------------------------------------------------------------
# GET /admin/api/chart/gaps
# ---------------------------------------------------------------------------

def _gaps_sync(from_ts: int, to_ts: int) -> dict[str, Any]:
    dt_from = _from_epoch(from_ts)
    dt_to = _from_epoch(to_ts)
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, ts_utc, direction, gap_low, gap_high, size,
                   weight, fill_ts, status
            FROM gaps
            WHERE symbol = %s AND ts_utc <= %s
              AND (fill_ts IS NULL OR fill_ts >= %s)
            ORDER BY ts_utc
            """,
            (_SYMBOL, dt_to, dt_from),
        )
        rows = cur.fetchall()
    finally:
        conn.close()
    return {
        "gaps": [
            {
                "id": r[0],
                "open_ts": _epoch(r[1]),
                "direction": r[2],
                "low": float(r[3]) if r[3] is not None else None,
                "high": float(r[4]) if r[4] is not None else None,
                "size": float(r[5]) if r[5] is not None else None,
                "weight": float(r[6]) if r[6] is not None else None,
                "filled": r[7] is not None,
                "filled_ts": _epoch(r[7]),
            }
            for r in rows
        ]
    }


@router.get("/chart/gaps", dependencies=_AUTH)
async def chart_gaps(
    from_: int = Query(..., alias="from"),
    to: int = Query(...),
):
    _check_span(from_, to, _DEFAULT_MAX_SPAN)
    return await run_in_threadpool(_gaps_sync, from_, to)


# ---------------------------------------------------------------------------
# GET /admin/api/chart/news-windows
# ---------------------------------------------------------------------------

def _news_windows_sync(from_ts: int, to_ts: int) -> dict[str, Any]:
    dt_from = _from_epoch(from_ts)
    dt_to = _from_epoch(to_ts)
    margin = timedelta(hours=2)

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT title, impact, ts_utc FROM news_events
            WHERE impact IN ('High', 'Medium')
              AND ts_utc >= %s AND ts_utc <= %s
            ORDER BY ts_utc
            """,
            (dt_from - margin, dt_to + margin),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    events = [{"title": r[0], "impact": r[1], "ts_utc": r[2]} for r in rows]
    params = load_blackout_params()
    cfg = params["news"]["blackout"]
    cluster_window = timedelta(minutes=cfg["consensus"]["cluster_window_min"])
    multiplier = cfg["consensus"]["multiplier"]
    _DEGREE_KEY = {"High": "degree_1", "Medium": "degree_2"}

    intervals: list[tuple[datetime, datetime, dict]] = []
    for event in events:
        degree_cfg = cfg[_DEGREE_KEY[event["impact"]]]
        before = timedelta(minutes=degree_cfg["before_min"])
        after_td = timedelta(minutes=degree_cfg["after_min"])
        has_cluster = any(
            other is not event
            and abs(other["ts_utc"] - event["ts_utc"]) <= cluster_window
            for other in events
        )
        if has_cluster:
            before *= multiplier
            after_td *= multiplier
        intervals.append((event["ts_utc"] - before, event["ts_utc"] + after_td, event))

    intervals.sort(key=lambda iv: iv[0])
    merged: list[tuple[datetime, datetime, list[dict]]] = []
    for start, end, event in intervals:
        if merged and start <= merged[-1][1]:
            ps, pe, pevts = merged[-1]
            merged[-1] = (ps, max(pe, end), pevts + [event])
        else:
            merged.append((start, end, [event]))

    windows = []
    for start, end, evts in merged:
        if end < dt_from or start > dt_to:
            continue
        windows.append({
            "start": _epoch(start),
            "end": _epoch(end),
            "impact": 1 if any(e["impact"] == "High" for e in evts) else 2,
            "titles": [e["title"] for e in evts],
            "ccy": "USD",
            "doubled": len(evts) > 1,
        })
    return {"windows": windows}


@router.get("/chart/news-windows", dependencies=_AUTH)
async def chart_news_windows(
    from_: int = Query(..., alias="from"),
    to: int = Query(...),
):
    _check_span(from_, to, _DEFAULT_MAX_SPAN)
    return await run_in_threadpool(_news_windows_sync, from_, to)


# ---------------------------------------------------------------------------
# GET /admin/api/chart/regime
# ---------------------------------------------------------------------------

def _regime_sync(from_ts: int, to_ts: int) -> dict[str, Any]:
    dt_from = _from_epoch(from_ts)
    dt_to = _from_epoch(to_ts)
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT ts_utc, regime, adx, bb_width_pct
            FROM regime_snapshots
            WHERE symbol = %s AND ts_utc >= %s AND ts_utc <= %s
            ORDER BY ts_utc
            """,
            (_SYMBOL, dt_from, dt_to),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        return {"segments": []}

    segments: list[dict[str, Any]] = []
    seg_start = _epoch(rows[0][0])
    seg_regime = rows[0][1]
    seg_adx = float(rows[0][2]) if rows[0][2] is not None else None
    seg_bb = float(rows[0][3]) if rows[0][3] is not None else None

    for i in range(1, len(rows)):
        ts, regime, adx, bb = rows[i]
        if regime != seg_regime:
            segments.append({
                "from": seg_start,
                "to": _epoch(rows[i - 1][0]),
                "regime": seg_regime,
                "adx": seg_adx,
                "bb_pctile": seg_bb,
            })
            seg_start = _epoch(ts)
            seg_regime = regime
            seg_adx = float(adx) if adx is not None else None
            seg_bb = float(bb) if bb is not None else None

    segments.append({
        "from": seg_start,
        "to": _epoch(rows[-1][0]),
        "regime": seg_regime,
        "adx": seg_adx,
        "bb_pctile": seg_bb,
    })
    return {"segments": segments}


@router.get("/chart/regime", dependencies=_AUTH)
async def chart_regime(
    from_: int = Query(..., alias="from"),
    to: int = Query(...),
):
    _check_span(from_, to, _DEFAULT_MAX_SPAN)
    return await run_in_threadpool(_regime_sync, from_, to)


# ---------------------------------------------------------------------------
# GET /admin/api/chart/rounds
# ---------------------------------------------------------------------------

def _rounds_sync(price_min: float, price_max: float) -> dict[str, Any]:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT DISTINCT ON (level, multiplier) level, multiplier, weight
            FROM round_number_hits
            WHERE symbol = %s AND level >= %s AND level <= %s
            ORDER BY level, multiplier, weight DESC
            """,
            (_SYMBOL, price_min, price_max),
        )
        rows = cur.fetchall()
    finally:
        conn.close()
    return {
        "levels": [
            {
                "price": float(r[0]),
                "multiple": r[1],
                "weight": float(r[2]) if r[2] is not None else None,
            }
            for r in rows
        ]
    }


@router.get("/chart/rounds", dependencies=_AUTH)
async def chart_rounds(
    price_min: float = Query(...),
    price_max: float = Query(...),
):
    if price_min >= price_max:
        raise HTTPException(400, detail="price_min must be less than price_max")
    if price_max - price_min > 2_000:
        raise HTTPException(400, detail="price range exceeds 2000 USD limit")
    return await run_in_threadpool(_rounds_sync, price_min, price_max)


# ---------------------------------------------------------------------------
# GET /admin/api/chart/fib
# ---------------------------------------------------------------------------

def _fib_sync(from_ts: int, to_ts: int) -> dict[str, Any]:
    dt_from = _from_epoch(from_ts)
    dt_to = _from_epoch(to_ts)
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, computed_at, swing_low, swing_high,
                   level_pct, price, role, overlapping
            FROM fibonacci_zones
            WHERE symbol = %s AND computed_at >= %s AND computed_at <= %s
            ORDER BY computed_at, price
            """,
            (_SYMBOL, dt_from, dt_to),
        )
        rows = cur.fetchall()
    finally:
        conn.close()
    return {
        "levels": [
            {
                "id": r[0],
                "computed_at": _epoch(r[1]),
                "swing_lo": float(r[2]) if r[2] is not None else None,
                "swing_hi": float(r[3]) if r[3] is not None else None,
                "ratio": float(r[4]) if r[4] is not None else None,
                "price": float(r[5]) if r[5] is not None else None,
                "role": r[6],
                "confluence_level_id": r[7],
            }
            for r in rows
        ]
    }


@router.get("/chart/fib", dependencies=_AUTH)
async def chart_fib(
    from_: int = Query(..., alias="from"),
    to: int = Query(...),
):
    _check_span(from_, to, _DEFAULT_MAX_SPAN)
    return await run_in_threadpool(_fib_sync, from_, to)


# ---------------------------------------------------------------------------
# GET /admin/api/chart/rules
# ---------------------------------------------------------------------------

def _rules_sync() -> dict[str, Any]:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, statement, origin, status, evidence, params,
                   bt_trades, bt_winrate, bt_expectancy, weight,
                   created_at, updated_at
            FROM rules ORDER BY created_at
            """
        )
        rows = cur.fetchall()
    finally:
        conn.close()
    return {
        "rules": [
            {
                "id": r[0],
                "statement": r[1],
                "origin": r[2],
                "status": r[3],
                "evidence": r[4],
                "params": r[5],
                "bt_trades": r[6],
                "bt_winrate": float(r[7]) if r[7] is not None else None,
                "bt_expectancy": float(r[8]) if r[8] is not None else None,
                "weight": float(r[9]) if r[9] is not None else None,
                "created_at": _epoch(r[10]),
                "updated_at": _epoch(r[11]),
            }
            for r in rows
        ]
    }


@router.get("/chart/rules", dependencies=_AUTH)
async def chart_rules():
    return await run_in_threadpool(_rules_sync)


# ---------------------------------------------------------------------------
# GET /admin/api/status
# ---------------------------------------------------------------------------

def _status_sync() -> dict[str, Any]:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT tf, COUNT(*) AS cnt, MAX(ts_utc) AS last_ts
            FROM candles WHERE symbol = %s GROUP BY tf
            """,
            (_SYMBOL,),
        )
        tf_map = {r[0]: {"count": r[1], "last_ts": r[2]} for r in cur.fetchall()}

        cur.execute(
            """
            SELECT
              SUM(CASE WHEN high < low                    THEN 1 ELSE 0 END),
              SUM(CASE WHEN close < low OR close > high   THEN 1 ELSE 0 END),
              SUM(CASE WHEN open <= 0 OR high <= 0
                            OR low <= 0 OR close <= 0     THEN 1 ELSE 0 END),
              SUM(CASE WHEN ts_utc > NOW()                THEN 1 ELSE 0 END)
            FROM candles
            WHERE symbol = %s AND tf = 'M5' AND ts_utc >= NOW() - INTERVAL '7 days'
            """,
            (_SYMBOL,),
        )
        q = cur.fetchone()

        cur.execute(
            "SELECT id, ts_utc, direction FROM signals ORDER BY id DESC LIMIT 1"
        )
        sig = cur.fetchone()

        now = datetime.now(timezone.utc)
        cur.execute(
            """
            SELECT title, impact, ts_utc FROM news_events
            WHERE impact IN ('High', 'Medium') AND ts_utc BETWEEN %s AND %s
            ORDER BY ts_utc
            """,
            (now - timedelta(hours=2), now + timedelta(hours=2)),
        )
        news_events = [{"title": r[0], "impact": r[1], "ts_utc": r[2]} for r in cur.fetchall()]
    finally:
        conn.close()

    bl = compute_blackout(now, news_events)
    m5_last = tf_map.get("M5", {}).get("last_ts")
    if m5_last and m5_last.tzinfo is None:
        m5_last = m5_last.replace(tzinfo=timezone.utc)
    lag_minutes = (
        round((now - m5_last).total_seconds() / 60, 1) if m5_last else None
    )
    return {
        "data": {
            "last_candle_ts": _epoch(m5_last),
            "lag_minutes": lag_minutes,
            "counts": {tf: tf_map.get(tf, {}).get("count", 0) for tf in ["M1", "M5", "M15", "H1"]},
        },
        "quality": {
            "high_lt_low":        int(q[0] or 0),
            "close_out_of_range": int(q[1] or 0),
            "nonpositive":        int(q[2] or 0),
            "future_ts":          int(q[3] or 0),
        },
        "dst": None,
        "n8n": [],
        "last_signal": {"id": sig[0], "ts": _epoch(sig[1]), "dir": sig[2]} if sig else None,
        "news_block": {
            "active": bl["blackout"],
            "until": _epoch(bl["window_end"]) if bl["window_end"] else None,
        },
        "emergency_stop": False,
    }


@router.get("/status", dependencies=_AUTH)
async def admin_status():
    return await run_in_threadpool(_status_sync)

# ============================================================
# NEW ENDPOINTS — appended to admin_router.py
# ============================================================

# ── helpers ────────────────────────────────────────────────

def _audit(action: str, entity: str | None = None,
           entity_id: int | None = None, field: str | None = None,
           old_value: str | None = None, new_value: str | None = None) -> None:
    """Insert one audit_log row synchronously (call inside a threadpool task)."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO audit_log (action, entity, entity_id, field, old_value, new_value)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (action, entity, entity_id, field, old_value, new_value),
        )
        conn.commit()
    finally:
        conn.close()


def _get_flag(key: str) -> Any:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT value_float, value_bool, value_text FROM system_flags WHERE key=%s", (key,))
        row = cur.fetchone()
        if row is None:
            return None
        val_float, val_bool, val_text = row
        if val_float is not None:
            return float(val_float)
        if val_bool is not None:
            return bool(val_bool)
        return val_text
    finally:
        conn.close()


def _set_flag(key: str, value: float | bool | str, by: str = "admin") -> None:
    conn = get_connection()
    try:
        cur = conn.cursor()
        if isinstance(value, bool):
            cur.execute(
                """INSERT INTO system_flags (key, value_bool, updated_by) VALUES (%s, %s, %s)
                   ON CONFLICT (key) DO UPDATE SET value_bool=EXCLUDED.value_bool,
                   updated_at=now(), updated_by=EXCLUDED.updated_by""",
                (key, value, by),
            )
        elif isinstance(value, (int, float)):
            cur.execute(
                """INSERT INTO system_flags (key, value_float, updated_by) VALUES (%s, %s, %s)
                   ON CONFLICT (key) DO UPDATE SET value_float=EXCLUDED.value_float,
                   updated_at=now(), updated_by=EXCLUDED.updated_by""",
                (key, value, by),
            )
        else:
            cur.execute(
                """INSERT INTO system_flags (key, value_text, updated_by) VALUES (%s, %s, %s)
                   ON CONFLICT (key) DO UPDATE SET value_text=EXCLUDED.value_text,
                   updated_at=now(), updated_by=EXCLUDED.updated_by""",
                (key, value, by),
            )
        conn.commit()
    finally:
        conn.close()


# ── pydantic models ─────────────────────────────────────────

class LotSizeBody(BaseModel):
    lot_size: float

class DefaultLotBody(BaseModel):
    value: float


# ── lot-size endpoints ──────────────────────────────────────

def _get_default_lot_sync() -> dict[str, Any]:
    val = _get_flag("default_lot_size")
    return {"default_lot_size": val if val is not None else 0.01}


@router.get("/settings/default-lot-size", dependencies=_AUTH)
async def get_default_lot_size():
    return await run_in_threadpool(_get_default_lot_sync)


def _put_default_lot_sync(value: float) -> dict[str, Any]:
    if value <= 0 or value > 100:
        raise HTTPException(400, detail="lot_size must be between 0 and 100")
    old = _get_flag("default_lot_size") or 0.01
    _set_flag("default_lot_size", value)
    _audit("UPDATE", "settings", None, "default_lot_size", str(old), str(value))
    return {"default_lot_size": value}


@router.put("/settings/default-lot-size", dependencies=_AUTH)
async def put_default_lot_size(body: DefaultLotBody):
    return await run_in_threadpool(_put_default_lot_sync, body.value)


def _patch_signal_lot_sync(signal_id: int, lot_size: float) -> dict[str, Any]:
    if lot_size <= 0 or lot_size > 100:
        raise HTTPException(400, detail="lot_size must be between 0 and 100")
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM signals WHERE id=%s", (signal_id,))
        if cur.fetchone() is None:
            raise HTTPException(404, detail="Signal not found")
        cur.execute("SELECT lot_size FROM signal_overrides WHERE signal_id=%s", (signal_id,))
        existing = cur.fetchone()
        old_val = str(float(existing[0])) if existing else None
        cur.execute(
            """INSERT INTO signal_overrides (signal_id, lot_size)
               VALUES (%s, %s)
               ON CONFLICT (signal_id) DO UPDATE SET lot_size=EXCLUDED.lot_size, updated_at=now()""",
            (signal_id, lot_size),
        )
        conn.commit()
    finally:
        conn.close()
    _audit("UPDATE", "signals", signal_id, "lot_size", old_val, str(lot_size))
    return {"signal_id": signal_id, "lot_size": lot_size}


@router.patch("/signals/{signal_id}/lot-size", dependencies=_AUTH)
async def patch_signal_lot_size(signal_id: int, body: LotSizeBody):
    return await run_in_threadpool(_patch_signal_lot_sync, signal_id, body.lot_size)


# ── dashboard summary ───────────────────────────────────────

_BASE_LOT = 0.01  # lot size used when pnl_usd was calculated

def _dashboard_summary_sync() -> dict[str, Any]:
    conn = get_connection()
    try:
        cur = conn.cursor()

        # Default lot + tracking start (stats filter, does NOT affect chart endpoints)
        default_lot = _get_flag("default_lot_size") or _BASE_LOT
        scale = default_lot / _BASE_LOT

        tracking_ts = _get_flag("dashboard_tracking_start")
        tracking_dt = _from_epoch(int(tracking_ts)) if tracking_ts is not None else None
        ts_clause = "AND ts_utc >= %(ts_start)s" if tracking_dt else ""
        ts_params: dict = {"ts_start": tracking_dt} if tracking_dt else {}

        # Win-rate and signal counts (filtered by tracking_start)
        cur.execute(f"""
            SELECT
                COUNT(*) FILTER (WHERE outcome IS NOT NULL) AS closed,
                COUNT(*) FILTER (WHERE outcome = 'tp') AS wins,
                COUNT(*) FILTER (WHERE outcome = 'sl') AS losses,
                COUNT(*) FILTER (WHERE outcome IS NULL) AS open_cnt,
                COUNT(*) AS total
            FROM signals
            WHERE 1=1 {ts_clause}
        """, ts_params)
        row = cur.fetchone()
        closed, wins, losses, open_cnt, total = row
        win_rate = round(wins / (wins + losses), 4) if (wins + losses) > 0 else None

        # Total PnL (filtered by tracking_start)
        cur.execute(f"""
            SELECT COALESCE(SUM(pnl_usd), 0) FROM signals
            WHERE pnl_usd IS NOT NULL {ts_clause}
        """, ts_params)
        total_pnl = float(cur.fetchone()[0])
        scaled_pnl = round(total_pnl * scale, 2)

        # Equity curve filtered by tracking_start
        cur.execute(f"""
            SELECT ts_utc, pnl_usd FROM signals
            WHERE pnl_usd IS NOT NULL {ts_clause}
            ORDER BY ts_utc
        """, ts_params)
        equity_rows = cur.fetchall()
        equity_curve = []
        running = 1000.0
        for eq_ts, pnl in equity_rows:
            running += float(pnl) * scale
            equity_curve.append({"ts": _epoch(eq_ts), "balance": round(running, 2)})

        # Last signal (unfiltered — most recent regardless of tracking_start)
        cur.execute("""
            SELECT id, ts_utc, direction, entry, outcome, pnl_usd
            FROM signals ORDER BY ts_utc DESC LIMIT 1
        """)
        sig_row = cur.fetchone()
        last_signal = None
        if sig_row:
            last_signal = {
                "id": sig_row[0],
                "ts": _epoch(sig_row[1]),
                "dir": sig_row[2],
                "entry": float(sig_row[3]) if sig_row[3] else None,
                "outcome": sig_row[4],
                "pnl_usd": float(sig_row[5]) * scale if sig_row[5] else None,
            }

        # Recent signals list (last 50, unfiltered — user can scroll back)
        cur.execute("""
            SELECT s.id, s.ts_utc, s.direction, s.entry, s.stop_loss, s.take_profit,
                   s.confidence, s.outcome, s.pnl_usd, s.rule_version,
                   COALESCE(so.lot_size, %(dlot)s) AS lot_size,
                   s.components
            FROM signals s
            LEFT JOIN signal_overrides so ON so.signal_id = s.id
            ORDER BY s.ts_utc DESC
            LIMIT 50
        """, {"dlot": default_lot})
        sig_rows = cur.fetchall()
        signals_list = []
        for r in sig_rows:
            sig_lot = float(r[10]) if r[10] else default_lot
            sig_scale = sig_lot / _BASE_LOT
            signals_list.append({
                "id": r[0],
                "ts": _epoch(r[1]),
                "dir": r[2],
                "entry": float(r[3]) if r[3] else None,
                "sl": float(r[4]) if r[4] else None,
                "tp": float(r[5]) if r[5] else None,
                "conf": float(r[6]) if r[6] else None,
                "outcome": r[7],
                "pnl_usd": round(float(r[8]) * sig_scale, 2) if r[8] else None,
                "rule_version": r[9],
                "lot_size": sig_lot,
                "components": r[11],
            })

    finally:
        conn.close()

    return {
        "win_rate": win_rate,
        "total_signals": int(total),
        "closed_signals": int(closed),
        "open_signals": int(open_cnt),
        "total_pnl": scaled_pnl,
        "current_balance": round(1000.0 + scaled_pnl, 2),
        "default_lot_size": default_lot,
        "tracking_start": int(tracking_ts) if tracking_ts is not None else None,
        "equity_curve": equity_curve,
        "last_signal": last_signal,
        "signals": signals_list,
    }


@router.get("/dashboard/summary", dependencies=_AUTH)
async def dashboard_summary():
    return await run_in_threadpool(_dashboard_summary_sync)


# ── tracking start endpoints ────────────────────────────────

class TrackingStartBody(BaseModel):
    ts: int  # epoch seconds


def _get_tracking_start_sync() -> dict[str, Any]:
    val = _get_flag("dashboard_tracking_start")
    if val is None:
        now_epoch = float(int(time.time()))
        _set_flag("dashboard_tracking_start", now_epoch)
        _audit("INSERT", "settings", None, "dashboard_tracking_start", None, str(int(now_epoch)))
        return {"tracking_start": int(now_epoch)}
    return {"tracking_start": int(val)}


@router.get("/settings/dashboard-tracking-start", dependencies=_AUTH)
async def get_tracking_start():
    return await run_in_threadpool(_get_tracking_start_sync)


def _put_tracking_start_sync(ts: int) -> dict[str, Any]:
    now = int(time.time())
    if ts < 0 or ts > now + 86400:
        raise HTTPException(400, detail="Invalid timestamp")
    old = _get_flag("dashboard_tracking_start")
    _set_flag("dashboard_tracking_start", float(ts))
    _audit("UPDATE", "settings", None, "dashboard_tracking_start",
           str(int(old)) if old is not None else None, str(ts))
    return {"tracking_start": ts}


@router.put("/settings/dashboard-tracking-start", dependencies=_AUTH)
async def put_tracking_start(body: TrackingStartBody):
    return await run_in_threadpool(_put_tracking_start_sync, body.ts)


# ── connection log ──────────────────────────────────────────

def _connection_log_sync(from_ts: int, to_ts: int) -> dict[str, Any]:
    _check_span(from_ts, to_ts, 365 * 86400)
    dt_from = _from_epoch(from_ts)
    dt_to = _from_epoch(to_ts)
    conn = get_connection()
    try:
        cur = conn.cursor()
        # Find M5 candle gaps larger than 2× the expected interval (10 minutes)
        cur.execute("""
            SELECT ts_utc FROM candles
            WHERE symbol='XAUUSD@' AND tf='M5'
              AND ts_utc >= %s AND ts_utc <= %s
            ORDER BY ts_utc
        """, (dt_from, dt_to))
        rows = cur.fetchall()
    finally:
        conn.close()

    events = []
    threshold_minutes = 10  # 2× M5 interval
    for i in range(1, len(rows)):
        prev_ts = rows[i-1][0]
        curr_ts = rows[i][0]
        gap_minutes = (curr_ts - prev_ts).total_seconds() / 60
        if gap_minutes > threshold_minutes:
            events.append({
                "ts_start": _epoch(prev_ts),
                "ts_end": _epoch(curr_ts),
                "duration_minutes": round(gap_minutes, 1),
                "type": "gap",
            })

    return {
        "note": "Gaps include expected market closures (weekends, holidays). Not all gaps indicate EA disconnection.",
        "events": events,
        "total_gaps": len(events),
    }


@router.get("/logs/connection", dependencies=_AUTH)
async def connection_log(
    from_: int = Query(..., alias="from"),
    to: int = Query(...),
):
    return await run_in_threadpool(_connection_log_sync, from_, to)


# ── server resources ─────────────────────────────────────────

try:
    import psutil as _psutil
    _HAS_PSUTIL = True
except ImportError:
    _psutil = None
    _HAS_PSUTIL = False


def _resources_sync() -> dict[str, Any]:
    if not _HAS_PSUTIL:
        raise HTTPException(503, detail="psutil not installed on server")
    return {
        "cpu_pct": _psutil.cpu_percent(interval=0.5),
        "ram_pct": _psutil.virtual_memory().percent,
        "disk_pct": _psutil.disk_usage("/").percent,
        "load_avg": list(_psutil.getloadavg()),
    }


@router.get("/status/resources", dependencies=_AUTH)
async def status_resources():
    return await run_in_threadpool(_resources_sync)
