import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import asyncpg
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from src.engine.level_reversion import check_level_reversion, load_rule_params
from src.engine.signal_store import (
    fetch_active_levels,
    fetch_atr_at,
    fetch_latest_closed_candle,
    insert_signal,
)
from src.features.levels import compute_levels, load_levels_params
from src.features.levels_store import fetch_candles, get_connection, upsert_levels
from src.ingest.candles_store import TF_MINUTES, filter_closed_candles
from src.news.blackout import get_blackout_status

load_dotenv()


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = await asyncpg.create_pool(
        host=os.environ.get("POSTGRES_HOST_LOCAL", "127.0.0.1"),
        port=int(os.environ.get("POSTGRES_PORT", "5432")),
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        database=os.environ["POSTGRES_DB"],
        min_size=1,
        max_size=5,
    )
    yield
    await app.state.pool.close()


app = FastAPI(title="XAUUSD Trader Bot API", lifespan=lifespan)


@app.get("/health")
async def health():
    """Pipeline health: last candle received, last signal fired, candle lag."""
    async with app.state.pool.acquire() as conn:
        last_candle_row = await conn.fetchrow(
            "SELECT ts_utc FROM candles WHERE symbol='XAUUSD@' AND tf='M5' ORDER BY ts_utc DESC LIMIT 1"
        )
        last_signal_row = await conn.fetchrow(
            "SELECT ts_utc, direction, rule_version FROM signals ORDER BY id DESC LIMIT 1"
        )
    now = datetime.now(timezone.utc)
    if last_candle_row:
        lag_s = (now - last_candle_row["ts_utc"].replace(tzinfo=timezone.utc)).total_seconds()
        last_candle_iso = last_candle_row["ts_utc"].isoformat()
    else:
        lag_s = None
        last_candle_iso = None
    return {
        "status": "ok",
        "last_candle_at": last_candle_iso,
        "candle_lag_minutes": round(lag_s / 60, 1) if lag_s is not None else None,
        "last_signal": {
            "ts_utc": last_signal_row["ts_utc"].isoformat(),
            "direction": last_signal_row["direction"],
            "rule": last_signal_row["rule_version"],
        } if last_signal_row else None,
    }


@app.get("/news/blackout-status")
async def blackout_status(as_of: str = Query(..., description="ISO-8601 timestamp, e.g. 2026-09-11T12:30:00Z")):
    try:
        ts = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=422, detail="as_of must be a valid ISO-8601 timestamp")
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)

    result = await get_blackout_status(app.state.pool, ts)
    return {
        "as_of": ts.isoformat(),
        "blackout": result["blackout"],
        "window_start": result["window_start"].isoformat() if result["window_start"] else None,
        "window_end": result["window_end"].isoformat() if result["window_end"] else None,
        "events": [
            {"title": e["title"], "impact": e["impact"], "ts_utc": e["ts_utc"].isoformat()}
            for e in result["events"]
        ],
    }


class CandleIn(BaseModel):
    ts_utc: datetime
    open: float
    high: float
    low: float
    close: float
    tick_volume: int | None = None
    spread: int | None = None
    real_volume: int | None = None


class CandlesIngestRequest(BaseModel):
    symbol: str
    tf: str
    candles: list[CandleIn]


@app.post("/ingest/candles")
async def ingest_candles(payload: CandlesIngestRequest):
    """SPEC.md 4.1. Caller must already have converted timestamps to UTC
    (see src/ingest/timezones.py) — this endpoint only enforces the
    closed-candle rule (CLAUDE.md rule 3) and upserts idempotently."""
    if payload.tf not in TF_MINUTES:
        raise HTTPException(status_code=422, detail=f"unknown timeframe: {payload.tf}")

    now_utc = datetime.now(timezone.utc)
    candle_dicts = [c.model_dump() for c in payload.candles]
    closed = filter_closed_candles(candle_dicts, payload.tf, now_utc)

    if closed:
        rows = [
            (
                payload.symbol, payload.tf, c["ts_utc"], c["open"], c["high"], c["low"], c["close"],
                c.get("tick_volume"), c.get("spread"), c.get("real_volume"),
            )
            for c in closed
        ]
        async with app.state.pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO candles
                    (symbol, tf, ts_utc, open, high, low, close, tick_volume, spread, real_volume)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                ON CONFLICT (symbol, tf, ts_utc) DO NOTHING
                """,
                rows,
            )

    return {"received": len(payload.candles), "closed": len(closed), "skipped_unclosed": len(payload.candles) - len(closed)}


def _compute_and_store_levels_sync(symbol: str, tf: str, as_of_ts: datetime) -> dict:
    """Sync (psycopg2) on purpose — compute_levels() is CPU-bound over
    potentially years of candles; run via threadpool below so it doesn't
    block the event loop."""
    conn = get_connection()
    try:
        candles = fetch_candles(conn, symbol, tf, as_of_ts)
        levels = compute_levels(candles, as_of_ts, symbol, tf)
        write_result = upsert_levels(conn, levels)
        conn.commit()
    finally:
        conn.close()
    return {
        "as_of": as_of_ts.isoformat(),
        "candle_count": len(candles),
        "level_count": len(levels),
        **write_result,
    }


@app.post("/levels/compute")
async def compute_levels_endpoint(
    symbol: str = Query(...),
    tf: str = Query("M5"),
    as_of: str = Query(..., description="ISO-8601 timestamp, e.g. 2026-09-11T12:30:00Z"),
):
    """SPEC.md 4.2. Recomputes the full levels state as of `as_of` from raw
    candles (CLAUDE.md rule 6: same function backtest and live) and upserts
    into `levels` by natural key (symbol, tf_origin, created_ts)."""
    try:
        ts = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=422, detail="as_of must be a valid ISO-8601 timestamp")
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)

    allowed_tfs = load_levels_params()["timeframes"]
    if tf not in allowed_tfs:
        raise HTTPException(status_code=422, detail=f"tf must be one of {allowed_tfs}")

    return await run_in_threadpool(_compute_and_store_levels_sync, symbol, tf, ts)


def _check_signal_sync(symbol: str, tf: str) -> dict:
    """Sync (psycopg2) on purpose, same reasoning as levels/compute above.
    Reads the latest closed candle, its ATR, and levels known as of that
    candle's own ts (anti-repainting — nothing newer is used), calls the
    same check_level_reversion() the backtest will call (CLAUDE.md rule 6),
    and persists any resulting signal."""
    conn = get_connection()
    try:
        candle = fetch_latest_closed_candle(conn, symbol, tf)
        if candle is None:
            return {"signal": None, "reason": "no candles for this symbol/tf"}

        atr_period = load_levels_params()["atr_period"]
        atr = fetch_atr_at(conn, symbol, tf, candle["ts_utc"], atr_period)
        if atr is None:
            return {"signal": None, "reason": "not enough history for ATR yet"}

        levels = fetch_active_levels(conn, symbol, tf, candle["ts_utc"])
        signal = check_level_reversion(
            symbol=symbol, tf=tf, ts_utc=candle["ts_utc"],
            close=float(candle["close"]), atr=atr, levels=levels,
        )
        if signal is None:
            return {"signal": None, "as_of": candle["ts_utc"].isoformat()}

        signal_id = insert_signal(conn, signal)
        conn.commit()
        if signal_id is None:
            # Already stored for this candle+rule — don't re-fire n8n/Telegram
            return {"signal": None, "as_of": candle["ts_utc"].isoformat(), "reason": "already_fired"}
        out = dict(signal)
        out["id"] = signal_id
        out["ts_utc"] = out["ts_utc"].isoformat()
        return {"signal": out}
    finally:
        conn.close()


@app.get("/signal/latest")
async def signal_latest(
    symbol: str = Query("XAUUSD@"),
    tf: str = Query("M5"),
):
    """Runs the level_reversion rule (src/engine/level_reversion.py) against
    the most recent closed candle. If it fires, the signal is stored in
    `signals` and returned; n8n polls this endpoint on a schedule."""
    allowed_tfs = load_levels_params()["timeframes"]
    if tf not in allowed_tfs:
        raise HTTPException(status_code=422, detail=f"tf must be one of {allowed_tfs}")
    return await run_in_threadpool(_check_signal_sync, symbol, tf)
