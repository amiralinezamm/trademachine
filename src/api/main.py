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


# ---------------------------------------------------------------------------
# Round numbers (SPEC.md 4.4)
# ---------------------------------------------------------------------------
from src.features.round_numbers import compute_round_numbers, load_round_numbers_params
from src.features.round_numbers_store import (
    fetch_candles_with_volume,
    upsert_round_number_hits,
    acceptance_stats,
)


def _compute_and_store_round_numbers_sync(symbol: str, tf: str, ts: datetime) -> dict:
    conn = get_connection()
    try:
        candles = fetch_candles_with_volume(conn, symbol, tf, ts)
        hits = compute_round_numbers(candles, ts, symbol, tf)
        result = upsert_round_number_hits(conn, hits)
        conn.commit()
        by_state = {}
        for h in hits:
            by_state[h["state"]] = by_state.get(h["state"], 0) + 1
        return {
            "symbol": symbol, "tf": tf, "as_of": ts.isoformat(),
            "total_hits": len(hits),
            "by_state": by_state,
            **result,
        }
    finally:
        conn.close()


@app.post("/round_numbers/compute")
async def compute_round_numbers_endpoint(
    symbol: str = Query(...),
    tf: str = Query("M5"),
    as_of: str = Query(..., description="ISO-8601 timestamp"),
):
    """SPEC.md 4.4. Compute round-number hit events up to as_of and upsert
    into round_number_hits. Returns counts by state."""
    try:
        ts = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=422, detail="as_of must be a valid ISO-8601 timestamp")
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return await run_in_threadpool(_compute_and_store_round_numbers_sync, symbol, tf, ts)


@app.get("/round_numbers/acceptance")
async def round_numbers_acceptance(
    symbol: str = Query("XAUUSD@"),
    tf: str = Query("M5"),
):
    """SPEC.md 4.4 acceptance criterion: compare reversal rate near round
    numbers vs the baseline candle-close-down rate."""
    conn = get_connection()
    try:
        return acceptance_stats(conn, symbol, tf)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Gaps (SPEC.md 4.5)
# ---------------------------------------------------------------------------
from src.features.gaps import compute_gaps, backtest_two_phase_claim, load_gaps_params
from src.features.gaps_store import (
    fetch_candles as fetch_candles_gaps,
    upsert_gaps,
    store_backtest_result,
    fetch_open_gaps,
)


def _compute_and_store_gaps_sync(symbol: str, tf: str, ts: datetime) -> dict:
    conn = get_connection()
    try:
        candles = fetch_candles_gaps(conn, symbol, tf, ts)
        params  = load_gaps_params()
        gaps    = compute_gaps(candles, ts, symbol, tf, params=params)
        result  = upsert_gaps(conn, gaps)
        conn.commit()
        by_status: dict = {}
        for g in gaps:
            by_status[g["status"]] = by_status.get(g["status"], 0) + 1
        return {"upserted": result["upserted"], "total": len(gaps), "by_status": by_status}
    finally:
        conn.close()


def _run_backtest_sync(symbol: str, tf: str, ts: datetime) -> dict:
    conn = get_connection()
    try:
        candles = fetch_candles_gaps(conn, symbol, tf, ts)
        params  = load_gaps_params()
        bt      = backtest_two_phase_claim(candles, ts, params=params)
        store_backtest_result(conn, bt)
        conn.commit()
        return bt
    finally:
        conn.close()


@app.post("/gaps/compute")
async def compute_gaps_endpoint(
    symbol: str = Query(...),
    tf: str = Query("M5"),
    as_of: str = Query(..., description="ISO-8601 timestamp"),
):
    """SPEC.md 4.5. Compute gaps up to as_of and upsert into gaps table."""
    try:
        ts = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=422, detail="as_of must be a valid ISO-8601 timestamp")
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return await run_in_threadpool(_compute_and_store_gaps_sync, symbol, tf, ts)


@app.post("/gaps/backtest")
async def run_gaps_backtest(
    symbol: str = Query("XAUUSD@"),
    tf: str = Query("M5"),
    as_of: str = Query(..., description="ISO-8601 timestamp"),
):
    """SPEC.md 4.5 two-phase backtest. Runs both body and wick modes; writes to rules table."""
    try:
        ts = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=422, detail="as_of must be a valid ISO-8601 timestamp")
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return await run_in_threadpool(_run_backtest_sync, symbol, tf, ts)


@app.get("/gaps/open")
async def get_open_gaps(
    symbol: str = Query("XAUUSD@"),
    tf: str = Query("M5"),
    as_of: str = Query(..., description="ISO-8601 timestamp"),
):
    """Return OPEN and HALF_FILLED gaps as of as_of."""
    try:
        ts = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=422, detail="as_of must be a valid ISO-8601 timestamp")
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    conn = get_connection()
    try:
        return fetch_open_gaps(conn, symbol, tf, ts)
    finally:
        conn.close()

# ---------------------------------------------------------------------------
# Fibonacci (SPEC.md 4.6)
# ---------------------------------------------------------------------------
from src.features.fibonacci import compute_fibonacci, load_fibonacci_params
from src.features.fibonacci_store import (
    fetch_active_levels_for_fib,
    upsert_fibonacci_zones,
    fetch_fibonacci_zones,
)


def _compute_and_store_fibonacci_sync(symbol: str, tf: str, ts: datetime) -> dict:
    conn = get_connection()
    try:
        candles = fetch_candles(conn, symbol, tf, ts)
        active_levels = fetch_active_levels_for_fib(conn, symbol, tf, ts)
        zones = compute_fibonacci(candles, ts, symbol, tf, active_levels)
        result = upsert_fibonacci_zones(conn, zones)
        conn.commit()
        by_role: dict = {}
        for z in zones:
            by_role[z["role"]] = by_role.get(z["role"], 0) + 1
        return {
            "symbol": symbol, "tf": tf, "as_of": ts.isoformat(),
            "active_level_count": len(active_levels),
            "zone_count": len(zones),
            "by_role": by_role,
            **result,
        }
    finally:
        conn.close()


@app.post("/fibonacci/compute")
async def compute_fibonacci_endpoint(
    symbol: str = Query(...),
    tf: str = Query("M5"),
    as_of: str = Query(..., description="ISO-8601 timestamp"),
):
    """SPEC.md 4.6. Compute Fibonacci zones from active levels as of as_of
    and upsert into fibonacci_zones. Swings are read from the levels table
    (NEVER computed independently). Extension levels are profit-target only
    (role='extension'); retracement levels are entry-zone candidates."""
    try:
        ts = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=422, detail="as_of must be a valid ISO-8601 timestamp")
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return await run_in_threadpool(_compute_and_store_fibonacci_sync, symbol, tf, ts)


@app.get("/fibonacci/zones")
async def get_fibonacci_zones(
    symbol: str = Query("XAUUSD@"),
    tf: str = Query("M5"),
    as_of: str = Query(..., description="ISO-8601 timestamp"),
    role: str | None = Query(None, description="Filter by role: retracement or extension"),
):
    """Return the latest Fibonacci zones snapshot stored at or before as_of."""
    try:
        ts = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=422, detail="as_of must be a valid ISO-8601 timestamp")
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    if role and role not in ("retracement", "extension"):
        raise HTTPException(status_code=422, detail="role must be 'retracement' or 'extension'")
    conn = get_connection()
    try:
        zones = fetch_fibonacci_zones(conn, symbol, tf, ts, role=role)
        return {"symbol": symbol, "tf": tf, "as_of": ts.isoformat(), "zones": zones}
    finally:
        conn.close()

# ---------------------------------------------------------------------------
# Regime (SPEC.md 4.9)
# ---------------------------------------------------------------------------
from src.features.regime import compute_regime, load_regime_params
from src.features.regime_store import (
    upsert_regime_snapshots,
    fetch_latest_regime,
    fetch_regime_snapshots,
)


def _compute_and_store_regime_sync(symbol: str, tf: str, ts: datetime) -> dict:
    conn = get_connection()
    try:
        candles = fetch_candles(conn, symbol, tf, ts)
        result = compute_regime(candles, ts, symbol, tf)
        write = upsert_regime_snapshots(conn, result["snapshots"])
        conn.commit()
        regime_counts: dict = {}
        for s in result["snapshots"]:
            regime_counts[s["regime"]] = regime_counts.get(s["regime"], 0) + 1
        return {
            "symbol": symbol, "tf": tf, "as_of": ts.isoformat(),
            "snapshot_count": len(result["snapshots"]),
            "regime_counts": regime_counts,
            "breakout_prob": result["breakout_prob"],
            **write,
        }
    finally:
        conn.close()


@app.post("/regime/compute")
async def compute_regime_endpoint(
    symbol: str = Query(...),
    tf: str = Query("M5"),
    as_of: str = Query(..., description="ISO-8601 timestamp"),
):
    """SPEC.md 4.9. Classify each bar as trend/range/gray using ADX+BB
    percentile rule, compute range duration distribution by hour/session
    and breakout probability curve, upsert all snapshots into regime_snapshots."""
    try:
        ts = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=422, detail="as_of must be a valid ISO-8601 timestamp")
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return await run_in_threadpool(_compute_and_store_regime_sync, symbol, tf, ts)


@app.get("/regime/latest")
async def get_latest_regime(
    symbol: str = Query("XAUUSD@"),
    tf: str = Query("M5"),
    as_of: str = Query(..., description="ISO-8601 timestamp"),
):
    """Return the most-recent regime snapshot stored at or before as_of."""
    try:
        ts = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=422, detail="as_of must be a valid ISO-8601 timestamp")
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    conn = get_connection()
    try:
        snap = fetch_latest_regime(conn, symbol, tf, ts)
        if snap is None:
            raise HTTPException(status_code=404, detail="No regime snapshot found at or before as_of")
        snap["ts_utc"] = snap["ts_utc"].isoformat()
        return snap
    finally:
        conn.close()

# ---------------------------------------------------------------------------
# Memory (SPEC.md 4.10) — STUMPY Matrix Profile
# ---------------------------------------------------------------------------
import subprocess
from src.features.memory_store import (
    upsert_memory_result,
    fetch_latest_memory_result,
)
from src.ingest.timezones import load_mt5_params

MEMORY_LOG = "/tmp/memory_compute.log"
MEMORY_PID = "/tmp/memory_compute.pid"
MEMORY_VENV_PYTHON = "/opt/xauusd-bot/src/api/venv/bin/python3"
MEMORY_SCRIPT = "/opt/xauusd-bot/scripts/compute_memory.py"


def _memory_compute_running() -> bool:
    """True if a previous compute_memory nohup job is still alive."""
    import os
    if not os.path.exists(MEMORY_PID):
        return False
    try:
        with open(MEMORY_PID) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)  # no-op if process exists, OSError if not
        return True
    except (ValueError, OSError):
        return False


@app.post("/memory/compute")
async def compute_memory_endpoint(
    symbol: str = Query("XAUUSD@"),
    tf: str = Query("M5"),
    as_of: str = Query(..., description="ISO-8601 timestamp"),
):
    """SPEC.md 4.10. Launches stumpy.match() in a background process
    (can take several minutes on 212K candles) and returns immediately.
    Poll GET /memory/result to check for completion.
    POST again while running returns status='already_running'.

    SECURITY (2026-09-17): symbol/tf are whitelisted against the same
    project-wide values used by /levels/compute — CLAUDE.md D1 fixes
    symbol to a single value ("XAUUSD@"), so that's the whitelist for
    symbol; tf reuses load_levels_params()["timeframes"], same source
    /levels/compute already validates against. The subprocess is launched
    with an argument LIST and shell=False (no shell string, so shell
    metacharacters in symbol/tf have no special meaning even if they
    somehow got past the whitelist) and start_new_session=True (detaches
    the child from this request's process group, replacing the old
    `nohup ... &` shell trick without needing a shell at all)."""
    try:
        ts = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=422, detail="as_of must be a valid ISO-8601 timestamp")
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)

    allowed_symbol = load_mt5_params()["symbol"]
    if symbol != allowed_symbol:
        raise HTTPException(status_code=422, detail=f"symbol must be {allowed_symbol!r}")

    allowed_tfs = load_levels_params()["timeframes"]
    if tf not in allowed_tfs:
        raise HTTPException(status_code=422, detail=f"tf must be one of {allowed_tfs}")

    if _memory_compute_running():
        return {"status": "already_running", "log": MEMORY_LOG}

    with open(MEMORY_LOG, "w") as log_f:
        proc = subprocess.Popen(
            [MEMORY_VENV_PYTHON, MEMORY_SCRIPT, symbol, tf, ts.isoformat()],
            stdout=log_f,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # detach — survives this request finishing
        )
    with open(MEMORY_PID, "w") as pid_f:
        pid_f.write(str(proc.pid))
    return {"status": "started", "log": MEMORY_LOG, "pid_file": MEMORY_PID}


@app.get("/memory/result")
async def get_memory_result(
    symbol: str = Query("XAUUSD@"),
    tf: str = Query("M5"),
    as_of: str = Query(..., description="ISO-8601 timestamp"),
):
    """Return the latest memory result stored at or before as_of.
    Returns 404 if no result has been computed yet."""
    try:
        ts = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=422, detail="as_of must be a valid ISO-8601 timestamp")
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    conn = get_connection()
    try:
        row = fetch_latest_memory_result(conn, symbol, tf, ts)
        if row is None:
            raise HTTPException(status_code=404, detail="No memory result found at or before as_of")
        if row.get("computed_at"):
            row["computed_at"] = row["computed_at"].isoformat()
        return row
    finally:
        conn.close()

# ---------------------------------------------------------------------------
# Correlation (SPEC.md 4.8) -- fixed pairs only, no free-form `symbol` param.
# 4.8-a/c: XAUUSD@ vs DXY@.  4.8-b: XAUUSD@ vs BRENT@.  T10Y@ not used yet
# (open decision). No whitelist needed here (unlike /memory/compute) because
# these endpoints never accept an instrument name as input at all -- the
# pair is fixed in code, matching SPEC.md's own fixed pairing.
# ---------------------------------------------------------------------------
from src.features.correlation import (
    compute_dollar_correlation,
    compute_oil_shock_events,
    backtest_oil_shock_divergence,
    compute_pressure_series,
    backtest_pressure_reversal,
    load_correlation_params,
)
from src.features.correlation_store import (
    fetch_candles as fetch_candles_corr,
    upsert_dollar_correlation,
    fetch_latest_dollar_correlation,
    upsert_oil_shock_events,
    store_oil_shock_backtest,
    upsert_pressure_snapshots,
    upsert_pressure_episodes,
    store_pressure_reversal_backtest,
)

GOLD_SYMBOL = "XAUUSD@"
DXY_SYMBOL = "DXY@"
BRENT_SYMBOL = "BRENT@"


def _compute_and_store_dollar_correlation_sync(tf: str, ts: datetime) -> dict:
    conn = get_connection()
    try:
        gold = fetch_candles_corr(conn, GOLD_SYMBOL, tf, ts)
        dxy = fetch_candles_corr(conn, DXY_SYMBOL, tf, ts)
        rows = compute_dollar_correlation(gold, dxy, ts, GOLD_SYMBOL, tf)
        result = upsert_dollar_correlation(conn, rows)
        conn.commit()
        return {
            "pair": f"{GOLD_SYMBOL}/{DXY_SYMBOL}", "tf": tf, "as_of": ts.isoformat(),
            "gold_candle_count": len(gold), "dxy_candle_count": len(dxy),
            "row_count": len(rows),
            "latest_correlation": rows[-1]["correlation"] if rows else None,
            **result,
        }
    finally:
        conn.close()


@app.post("/correlation/dollar/compute")
async def compute_dollar_correlation_endpoint(
    tf: str = Query("M5"),
    as_of: str = Query(..., description="ISO-8601 timestamp"),
):
    """SPEC.md 4.8-a. Rolling dollar_corr_window-bar correlation between
    XAUUSD@ and DXY@ returns; the correlation value itself is the feature."""
    try:
        ts = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=422, detail="as_of must be a valid ISO-8601 timestamp")
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return await run_in_threadpool(_compute_and_store_dollar_correlation_sync, tf, ts)


@app.get("/correlation/dollar/latest")
async def get_latest_dollar_correlation(
    tf: str = Query("M5"),
    as_of: str = Query(..., description="ISO-8601 timestamp"),
):
    try:
        ts = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=422, detail="as_of must be a valid ISO-8601 timestamp")
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    conn = get_connection()
    try:
        row = fetch_latest_dollar_correlation(conn, GOLD_SYMBOL, tf, ts)
        if row is None:
            raise HTTPException(status_code=404, detail="No correlation value found at or before as_of")
        row["ts_utc"] = row["ts_utc"].isoformat()
        return row
    finally:
        conn.close()


def _compute_and_store_oil_shock_sync(tf: str, ts: datetime) -> dict:
    conn = get_connection()
    try:
        gold = fetch_candles_corr(conn, GOLD_SYMBOL, tf, ts)
        oil = fetch_candles_corr(conn, BRENT_SYMBOL, tf, ts)
        events = compute_oil_shock_events(gold, oil, ts, GOLD_SYMBOL, tf)
        write_result = upsert_oil_shock_events(conn, events)
        bt = backtest_oil_shock_divergence(gold, oil, ts)
        store_oil_shock_backtest(conn, bt)
        conn.commit()
        return {
            "pair": f"{GOLD_SYMBOL}/{BRENT_SYMBOL}", "tf": tf, "as_of": ts.isoformat(),
            "gold_candle_count": len(gold), "oil_candle_count": len(oil),
            "event_count": len(events),
            "backtest": bt,
            **write_result,
        }
    finally:
        conn.close()


@app.post("/correlation/oil-shock/compute")
async def compute_oil_shock_endpoint(
    tf: str = Query("M5"),
    as_of: str = Query(..., description="ISO-8601 timestamp"),
):
    """SPEC.md 4.8-b. Detects oil shocks (XAUUSD@ vs BRENT@), stores events,
    runs the reversal-rate binomial significance test + lag cross-correlation,
    and registers oil_shock_divergence in `rules` as verified/rejected per
    SPEC.md's explicit instruction."""
    try:
        ts = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=422, detail="as_of must be a valid ISO-8601 timestamp")
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return await run_in_threadpool(_compute_and_store_oil_shock_sync, tf, ts)


def _compute_and_store_pressure_sync(tf: str, ts: datetime) -> dict:
    conn = get_connection()
    try:
        gold = fetch_candles_corr(conn, GOLD_SYMBOL, tf, ts)
        dxy = fetch_candles_corr(conn, DXY_SYMBOL, tf, ts)
        series = compute_pressure_series(gold, dxy, ts, GOLD_SYMBOL, tf)
        write_result = upsert_pressure_snapshots(conn, series)
        bt = backtest_pressure_reversal(gold, dxy, ts)
        episodes_written = upsert_pressure_episodes(conn, GOLD_SYMBOL, tf, bt.get("episodes", []))
        store_pressure_reversal_backtest(conn, bt)
        conn.commit()
        flagged_count = sum(1 for row in series if row["flagged"])
        return {
            "pair": f"{GOLD_SYMBOL}/{DXY_SYMBOL}", "tf": tf, "as_of": ts.isoformat(),
            "snapshot_count": len(series), "flagged_count": flagged_count,
            "episode_count": bt["episode_count"],
            "answers": bt["answers"],
            **write_result,
            "episodes_upserted": episodes_written["upserted"],
        }
    finally:
        conn.close()


@app.post("/correlation/pressure/compute")
async def compute_pressure_endpoint(
    tf: str = Query("M5"),
    as_of: str = Query(..., description="ISO-8601 timestamp"),
):
    """SPEC.md 4.8-c. Rolling regression residual -> accumulated pressure ->
    flag/discharge episodes (XAUUSD@ vs DXY@), stores the time series +
    episodes, and registers pressure_reversal in `rules` as 'testing'
    (SPEC gives no numeric accept/reject gate here, only three questions
    to answer -- see src/features/correlation.py docstring)."""
    try:
        ts = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=422, detail="as_of must be a valid ISO-8601 timestamp")
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return await run_in_threadpool(_compute_and_store_pressure_sync, tf, ts)
