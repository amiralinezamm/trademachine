import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import asyncpg
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

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
def health():
    return {"status": "ok"}


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
