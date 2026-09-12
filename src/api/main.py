import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import asyncpg
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query

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
