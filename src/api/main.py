import os
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import asyncpg
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from src.engine.level_reversion import apply_spacing_filter, check_level_reversion, load_rule_params
from src.engine.signal_store import (
    fetch_active_levels,
    fetch_atr_at,
    fetch_latest_closed_candle,
    LIVE_RULE_VERSION,
    fetch_latest_signal,
    fetch_last_signal_for_direction,
    insert_signal,
)
from src.features.levels import compute_levels, load_levels_params
from src.features.levels_store import fetch_candles, get_connection, upsert_levels
from src.ingest.candles_store import TF_MINUTES, filter_closed_candles
from src.engine.market_structure import compute_market_structure
from src.engine.module_voting import (
    compute_votes, load_voting_params, load_rules_registry_params, PROPOSED_VOTE_FUNCTIONS,
)
from src.news.blackout import compute_blackout, get_blackout_status

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


from src.api.admin_router import router as admin_router

app = FastAPI(title="XAUUSD Trader Bot API", lifespan=lifespan)
app.include_router(admin_router)



def is_forex_market_open(utc_dt: datetime) -> bool:
    """True when XAUUSD/gold is expected to be trading.

    Gold/FX on WM Markets runs Sun 21:00 – Fri 21:00 UTC.
    This is a time-based fallback, not a live MT5 query — it will not catch
    broker-specific early closes (e.g. Christmas Eve) or delayed Sunday opens.
    """
    wd = utc_dt.weekday()  # 0=Mon … 4=Fri, 5=Sat, 6=Sun
    h = utc_dt.hour
    if wd == 5:            # Saturday: always closed
        return False
    if wd == 6 and h < 21: # Sunday before 21:00 UTC: still closed
        return False
    if wd == 4 and h >= 21: # Friday from 21:00 UTC: closed
        return False
    return True


@app.get("/health")
async def health():
    """Pipeline health: last candle received, last signal fired, candle lag."""
    async with app.state.pool.acquire() as conn:
        last_candle_row = await conn.fetchrow(
            "SELECT ts_utc FROM candles WHERE symbol='XAUUSD@' AND tf='M5' ORDER BY ts_utc DESC LIMIT 1"
        )
        last_signal_row = await conn.fetchrow(
            "SELECT ts_utc, direction, rule_version FROM signals"
            " WHERE rule_version = $1 ORDER BY id DESC LIMIT 1",
            LIVE_RULE_VERSION,
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
        "market_open": is_forex_market_open(now),
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



@app.post("/news/calendar/ingest")
async def news_calendar_ingest():
    """SPEC.md 4.7: pull ForexFactory thisweek XML, upsert into news_events.
    Idempotent. n8n calls this every 6 h."""
    from starlette.concurrency import run_in_threadpool
    from src.news.fetch_calendar import (
        fetch_raw_xml, parse_events, filter_events, upsert_events, get_connection,
    )

    def _run():
        xml_text = fetch_raw_xml()
        events = parse_events(xml_text)
        kept = filter_events(events)
        conn = get_connection()
        try:
            n = upsert_events(conn, kept)
            conn.commit()
        finally:
            conn.close()
        return {"raw": len(events), "kept": len(kept), "upserted": n}

    return await run_in_threadpool(_run)

def _fetch_events_with_surprise(where_sql: str, params: tuple, limit: int = 40) -> list[dict]:
    """Shared by /news/upcoming, /news/digest/weekly, /news/digest/daily.
    Returns [{"event": {...}, "surprise_result": {...}}] ordered by ts_utc.
    Sync (psycopg2) — matches the rest of src/news, run via run_in_threadpool.
    """
    import datetime as _dt
    from src.news.fetch_calendar import get_connection
    from src.news.surprise import load_event_map, load_event_meta_map, lookup_event_meta, lookup_gold_sign

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT title, country, impact, ts_utc, forecast, previous
                FROM news_events
                WHERE {where_sql}
                ORDER BY ts_utc ASC
                LIMIT %s
                """,
                (*params, limit),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    event_map = load_event_map()
    meta_map = load_event_meta_map()
    out = []
    for title, country, impact, ts_utc, forecast, previous in rows:
        if ts_utc.tzinfo is None:
            ts_utc = ts_utc.replace(tzinfo=_dt.timezone.utc)
        event = {
            "title": title,
            "country": country,
            "impact": impact,
            "ts_utc": ts_utc,
            "forecast": forecast,
            "previous": previous,
        }
        gold_sign = lookup_gold_sign(title, event_map, country=country)
        out.append({
            "event": event,
            "surprise_result": {"gold_sign": gold_sign, "mapped": gold_sign is not None},
            "meta": lookup_event_meta(title, meta_map, country=country),
        })
    return out


@app.get("/news/upcoming")
async def news_upcoming():
    """SPEC.md 4.7 / کار 5: upcoming High/Medium events formatted for Telegram /news command.
    No actual yet — shows schedule + expected direction only. One grouped
    Telegram HTML digest (SPEC 4.7 revamp: Jalali day headers, single
    disclaimer) rather than one message per event.
    """
    from src.news.news_reporter import build_upcoming_digest

    def _run():
        items = _fetch_events_with_surprise(
            "ts_utc > NOW() AND impact IN ('High', 'Medium') AND (actual IS NULL OR actual = '')",
            (),
            limit=20,
        )
        digest = build_upcoming_digest(items)
        return {
            "count": len(items),
            "digest": digest,
            "events": [
                {
                    "title": it["event"]["title"],
                    "ts_utc": it["event"]["ts_utc"].isoformat(),
                    "impact": it["event"]["impact"],
                }
                for it in items
            ],
        }

    return await run_in_threadpool(_run)


@app.get("/news/digest/weekly")
async def news_digest_weekly():
    """n8n: Monday 10:00 Tehran weekly digest — all High/Medium USD/EUR/GBP
    events in the next 7 days. Always has content to send (even if empty)."""
    from src.news.news_reporter import build_upcoming_digest

    def _run():
        items = _fetch_events_with_surprise(
            "ts_utc > NOW() AND ts_utc < NOW() + INTERVAL '7 days' "
            "AND impact IN ('High', 'Medium') AND (actual IS NULL OR actual = '')",
            (),
        )
        digest = build_upcoming_digest(items, header="🗞 <b>اخبار اقتصادی این هفته</b>")
        return {"count": len(items), "digest": digest}

    return await run_in_threadpool(_run)


@app.get("/news/digest/daily")
async def news_digest_daily():
    """n8n: daily digest — today's (Tehran calendar day) High/Medium events.
    n8n gates sending on count > 0 (SPEC: "هر روزی که خبر داریم")."""
    from zoneinfo import ZoneInfo
    from src.news.news_reporter import build_upcoming_digest

    def _run():
        now_tehran = datetime.now(ZoneInfo("Asia/Tehran"))
        day_start_tehran = now_tehran.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end_tehran = day_start_tehran + timedelta(days=1)
        day_start_utc = day_start_tehran.astimezone(timezone.utc)
        day_end_utc = day_end_tehran.astimezone(timezone.utc)

        items = _fetch_events_with_surprise(
            "ts_utc >= %s AND ts_utc < %s "
            "AND impact IN ('High', 'Medium') AND (actual IS NULL OR actual = '')",
            (day_start_utc, day_end_utc),
        )
        digest = build_upcoming_digest(items, header="🗞 <b>اخبار اقتصادی امروز</b>")
        return {"count": len(items), "digest": digest}

    return await run_in_threadpool(_run)


@app.post("/news/alerts/dispatch")
async def news_alerts_dispatch():
    """n8n: fires every `pre_alert.trigger_interval_minutes` (params.yaml).
    Atomically claims (UPDATE ... RETURNING) events whose release falls
    inside the pre_alert window ahead of now, so a race between overlapping
    n8n runs can't double-send. Each claimed event gets its own alert
    message (build_upcoming_message — same anti-repaint direction lookup as
    everywhere else in src/news)."""
    import yaml
    from pathlib import Path
    from src.news.fetch_calendar import get_connection
    from src.news.surprise import load_event_map, lookup_gold_sign
    from src.news.news_reporter import build_upcoming_message

    params_path = Path(__file__).resolve().parents[2] / "config" / "params.yaml"
    with open(params_path) as f:
        pre_alert = yaml.safe_load(f)["news"]["pre_alert"]
    window_minutes = pre_alert["window_minutes"]
    trigger_interval_minutes = pre_alert["trigger_interval_minutes"]

    def _run():
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE news_events
                    SET pre_alert_sent = true
                    WHERE id IN (
                        SELECT id FROM news_events
                        WHERE pre_alert_sent = false
                          AND impact IN ('High', 'Medium')
                          AND ts_utc >= NOW() + %s * INTERVAL '1 minute'
                          AND ts_utc <  NOW() + %s * INTERVAL '1 minute'
                        FOR UPDATE SKIP LOCKED
                    )
                    RETURNING title, country, impact, ts_utc, forecast, previous
                    """,
                    (window_minutes - trigger_interval_minutes, window_minutes),
                )
                rows = cur.fetchall()
            conn.commit()
        finally:
            conn.close()

        event_map = load_event_map()
        messages = []
        for title, country, impact, ts_utc, forecast, previous in rows:
            if ts_utc.tzinfo is None:
                ts_utc = ts_utc.replace(tzinfo=timezone.utc)
            event = {
                "title": title, "country": country, "impact": impact,
                "ts_utc": ts_utc, "forecast": forecast, "previous": previous,
            }
            gold_sign = lookup_gold_sign(title, event_map, country=country)
            surprise_result = {"gold_sign": gold_sign, "mapped": gold_sign is not None}
            messages.append(build_upcoming_message(event, surprise_result))

        text = "\n\n――――――――――――\n\n".join(messages)
        return {"count": len(messages), "text": text}

    return await run_in_threadpool(_run)

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


def _compute_and_store_levels_sync(symbol: str, tf: str, as_of_ts: datetime, lookback_bars: int | None = None) -> dict:
    """Sync (psycopg2) on purpose — compute_levels() is CPU-bound over
    potentially years of candles; run via threadpool below so it doesn't
    block the event loop."""
    conn = get_connection()
    try:
        candles = fetch_candles(conn, symbol, tf, as_of_ts, lookback_bars=lookback_bars)
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
    lookback_bars: int | None = Query(None, description="Limit to the most recent N candles; None = full history (default, required for backtest)"),
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

    return await run_in_threadpool(_compute_and_store_levels_sync, symbol, tf, ts, lookback_bars)



def _fetch_rule_statuses_sync(conn, rule_ids: list[str]) -> dict[str, str]:
    """D20 (2026-09-25): current `rules` table status for the given ids --
    used to gate module_voting_v1's PROPOSED_VOTE_FUNCTIONS. Reads only;
    never writes rules.status (that's a human/backtest decision)."""
    with conn.cursor() as cur:
        cur.execute("SELECT id, status FROM rules WHERE id = ANY(%s)", (rule_ids,))
        return {row[0]: row[1] for row in cur.fetchall()}


def _fetch_news_events_sync(conn, as_of_ts: datetime) -> list[dict]:
    """Sync psycopg2 mirror of blackout.fetch_relevant_events — for use in
    _check_signal_sync which cannot use async/asyncpg."""
    window_start = as_of_ts - timedelta(hours=2)
    window_end = as_of_ts + timedelta(hours=2)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT title, impact, ts_utc
            FROM news_events
            WHERE impact IN ('High', 'Medium')
              AND ts_utc BETWEEN %s AND %s
            """,
            (window_start, window_end),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


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

        # SPEC.md 4.7-a: suppress signal during pre/post news blackout window
        _news_events = _fetch_news_events_sync(conn, candle["ts_utc"])
        _bko = compute_blackout(candle["ts_utc"], _news_events)
        if _bko["blackout"]:
            return {"signal": None, "as_of": candle["ts_utc"].isoformat(), "reason": "blackout"}

        levels = fetch_active_levels(conn, symbol, tf, candle["ts_utc"])

        # market_structure_filter (roadmap-rev2 4.4): H1 structure, resampled
        # from M5 (H1 ingestion stale, tracked separately -- see market_structure.py).
        # 2000 M5 bars (~7 days) is comfortably more than the H1 swing detector needs.
        _structure_candles = fetch_candles(conn, symbol, tf, candle["ts_utc"], lookback_bars=2000)
        _structure = compute_market_structure(_structure_candles, candle["ts_utc"])

        signal = check_level_reversion(
            symbol=symbol, tf=tf, ts_utc=candle["ts_utc"],
            high=float(candle["high"]), low=float(candle["low"]),
            close=float(candle["close"]), atr=atr, levels=levels,
            structure=_structure["structure"],
        )
        if signal is not None:
            # same-direction spacing filter (Rule: same_direction_spacing_filter)
            _rule_params = load_rule_params()
            _min_spacing = float(_rule_params.get("min_same_direction_spacing_usd", 10.0))
            _prev = fetch_last_signal_for_direction(
                conn, signal["direction"], candle["ts_utc"]
            )
            signal = apply_spacing_filter(
                signal, float(candle["close"]), _prev, _min_spacing
            )
        if signal is None:
            return {"signal": None, "as_of": candle["ts_utc"].isoformat()}

        # Enrich components with all module outputs before storing
        # (SPEC.md 4.14 — components jsonb must include all signal contributors).
        # fetch_db_context() is pure-logic separated: build_context() inside it
        # re-applies as_of_ts filter on every list so no future data can leak in.
        _voting_params = load_voting_params()
        from src.engine.signal_context import fetch_db_context
        ctx = fetch_db_context(
            conn, symbol, tf, candle["ts_utc"],
            float(candle["close"]), atr,
            divergence_recency_bars=_voting_params.get("divergence_recency_bars", 12),
        )
        signal["components"].update(ctx)
        signal["components"]["market_structure"] = _structure["structure"]

        # module_voting_v1: aggregate per-module votes; suppress signal if
        # net_votes is below the configured threshold.
        # D20 (2026-09-25): proposed-status rules (matrix, rsi_overbought_oversold,
        # rsi_price_divergence, macd_price_divergence) only vote when
        # rules_registry.allow_proposed_in_voting is true; whichever of them
        # did vote this call is recorded in components.proposed_observations
        # so a proposed rule's influence on a live signal is never silent.
        _rule_status = _fetch_rule_statuses_sync(conn, list(PROPOSED_VOTE_FUNCTIONS.keys()))
        _allow_proposed = bool(load_rules_registry_params().get("allow_proposed_in_voting", False))
        _vote_result = compute_votes(
            signal["components"], signal["direction"], _voting_params,
            rule_status=_rule_status, allow_proposed=_allow_proposed,
        )
        signal["components"]["votes"] = _vote_result["votes"]
        signal["components"]["net_votes"] = _vote_result["net_votes"]
        signal["components"]["proposed_observations"] = _vote_result["proposed_observations"]
        if _vote_result["net_votes"] < _voting_params.get("min_net_votes", 2):
            return {
                "signal": None, "as_of": candle["ts_utc"].isoformat(),
                "reason": "insufficient_votes", "net_votes": _vote_result["net_votes"],
            }

        # Level-to-level SL/TP (2026-09-29 decision) -- the ONE function
        # backtest calls too (CLAUDE.md rule 6). No fallback to an ATR-
        # multiple exit: if no level gives an acceptable reward:risk, the
        # signal is rejected rather than sent with a worse-than-specified
        # ratio (src/engine/exit_rules.py docstring has the full rule).
        from src.engine.exit_rules import compute_level_based_sl_tp, load_exit_rules_params
        _level_a = {
            "id": signal["components"].get("level_id"),
            "price_low": signal["components"]["level_price_low"],
            "price_high": signal["components"]["level_price_high"],
            "strength": signal["components"]["level_strength"],
        }
        _exit = compute_level_based_sl_tp(
            signal["direction"], _level_a, float(candle["close"]), atr,
            levels, _vote_result["net_votes"], load_exit_rules_params(),
        )
        if _exit is None:
            return {
                "signal": None, "as_of": candle["ts_utc"].isoformat(),
                "reason": "no_valid_sl_tp",
            }
        signal["stop_loss"] = _exit["stop_loss"]
        signal["take_profit"] = _exit["take_profit"]
        signal["components"]["sl_tp"] = _exit

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


def _check_reversal_close_sync(symbol: str, tf: str) -> dict | None:
    """'توقف اجباری' rule (2026-09-29): for each still-open signal (per
    direction), first tries to resolve its outcome from price history since
    entry (reuses src/backtest/replay._determine_outcome -- CLAUDE.md rule
    6, not a reimplementation), then -- if genuinely still open and not
    already alerted -- checks whether H1 structure has flipped against it
    (src/engine/exit_rules.detect_reversal_close). Returns at most one
    advisory per call (n8n polls this every 5 minutes, same cadence as the
    entry signal check)."""
    import numpy as np
    import talib

    from src.backtest.replay import _determine_outcome
    from src.engine.exit_rules import detect_reversal_close
    from src.engine.signal_store import (
        fetch_candles_range, fetch_open_signals, mark_reversal_alert_sent, mark_signal_outcome,
    )

    conn = get_connection()
    try:
        open_signals = fetch_open_signals(conn, symbol, tf)
        if not open_signals:
            return None

        atr_period = load_levels_params()["atr_period"]
        # Same source as backtest's load_break_atr_mult() (CLAUDE.md rule 6) --
        # levels.break_atr_mult, NOT signal_rules.level_reversion.confirm_atr_mult
        # (a different, unrelated threshold for the entry confirmation margin).
        break_mult = float(load_levels_params()["break_atr_mult"])
        latest_candle = fetch_latest_closed_candle(conn, symbol, tf)
        if latest_candle is None:
            return None
        _structure_candles = fetch_candles(conn, symbol, tf, latest_candle["ts_utc"], lookback_bars=2000)
        structure = compute_market_structure(_structure_candles, latest_candle["ts_utc"])["structure"]

        for sig in open_signals:
            if sig["stop_loss"] is None or sig["take_profit"] is None:
                continue  # fired before this SL/TP wiring existed -- nothing to scan against

            cands = fetch_candles_range(conn, symbol, tf, sig["ts_utc"], latest_candle["ts_utc"])
            if len(cands) >= 2:
                highs = np.array([float(c["high"]) for c in cands])
                lows = np.array([float(c["low"]) for c in cands])
                closes = np.array([float(c["close"]) for c in cands])
                atr_arr = talib.ATR(highs, lows, closes, timeperiod=atr_period)
                level_lo = float(sig["components"].get("level_price_low", 0))
                level_hi = float(sig["components"].get("level_price_high", 0))
                outcome, _exit_price = _determine_outcome(
                    cands, atr_arr, 0, sig["entry"], sig["direction"],
                    sig["stop_loss"], sig["take_profit"],
                    max_safety_bars=len(cands),  # never force 'timeout' -- 'open' if nothing hit yet
                    level_lo=level_lo, level_hi=level_hi, break_mult=break_mult,
                )
                if outcome in ("tp", "sl", "level_invalidated"):
                    mark_signal_outcome(conn, sig["id"], outcome)
                    conn.commit()
                    continue  # closed -- no advisory needed

            if sig["reversal_alert_sent"]:
                continue
            if detect_reversal_close(sig["direction"], structure):
                mark_reversal_alert_sent(conn, sig["id"])
                conn.commit()
                return {
                    "signal_id": sig["id"], "direction": sig["direction"],
                    "entry": sig["entry"], "ts_utc": sig["ts_utc"].isoformat(),
                    "structure": structure,
                    "message": "توقف اجباری: ساختار بازار برخلاف این سیگنال برگشت — سیگنال باز را دستی ببندید.",
                }
        return None
    finally:
        conn.close()



@app.get("/levels/near-price")
async def levels_near_price(
    symbol: str = Query("XAUUSD@"),
    tf: str = Query("M5"),
):
    """Nearest active support below price and resistance above price."""
    def _run():
        conn = get_connection()
        try:
            candle = fetch_latest_closed_candle(conn, symbol, tf)
            if candle is None:
                return {"price": None, "support": None, "resistance": None,
                        "reason": "no candles"}
            price = float(candle["close"])
            levels = fetch_active_levels(conn, symbol, tf, candle["ts_utc"])
        finally:
            conn.close()

        support = resistance = None
        best_sup_dist = best_res_dist = float("inf")
        for lvl in levels:
            mid = (float(lvl["price_low"]) + float(lvl["price_high"])) / 2
            if lvl["kind"] == "support" and mid < price:
                d = price - mid
                if d < best_sup_dist:
                    best_sup_dist = d
                    support = {**lvl, "distance": round(d, 4)}
            elif lvl["kind"] == "resistance" and mid > price:
                d = mid - price
                if d < best_res_dist:
                    best_res_dist = d
                    resistance = {**lvl, "distance": round(d, 4)}

        def _clean(lvl):
            if lvl is None:
                return None
            return {
                k: (
                    float(v) if hasattr(v, "__float__") and not isinstance(v, (str, bool))
                    else v.isoformat() if hasattr(v, "isoformat") else v
                )
                for k, v in lvl.items()
            }

        return {
            "price": price,
            "support": _clean(support),
            "resistance": _clean(resistance),
        }

    return await run_in_threadpool(_run)

@app.get("/signal/latest")
async def signal_latest(
    symbol: str = Query("XAUUSD@"),
    tf: str = Query("M5"),
):
    """Runs the level_reversion rule (src/engine/level_reversion.py) against
    the most recent closed candle. If it fires, the signal is stored in
    `signals` and returned; n8n polls this endpoint on a schedule.

    Also runs the 'توقف اجباری' forced-stop check (_check_reversal_close_sync)
    on the SAME poll and includes it as `close_advisory` -- at most one
    manual-close advisory per call, independent of whether a new entry
    signal fired this time."""
    allowed_tfs = load_levels_params()["timeframes"]
    if tf not in allowed_tfs:
        raise HTTPException(status_code=422, detail=f"tf must be one of {allowed_tfs}")
    result = await run_in_threadpool(_check_signal_sync, symbol, tf)
    result["close_advisory"] = await run_in_threadpool(_check_reversal_close_sync, symbol, tf)
    return result


def _fetch_latest_signal_sync(symbol: str, tf: str) -> dict:
    """Read-only -- see fetch_latest_signal() docstring for why this is a
    separate endpoint from /signal/latest (which re-runs the rule and
    stops returning a signal once it's already stored)."""
    conn = get_connection()
    try:
        sig = fetch_latest_signal(conn, symbol, tf)
    finally:
        conn.close()
    if sig is None:
        return {"signal": None, "reason": "no_signal_stored"}
    sig["ts_utc"] = sig["ts_utc"].isoformat()
    return {"signal": sig}


@app.get("/signal/last")
async def signal_last(
    symbol: str = Query("XAUUSD@"),
    tf: str = Query("M5"),
):
    """The most recently STORED signal in `signals`, for display (e.g. the
    Telegram /status command) -- unlike /signal/latest, this never
    re-evaluates the rule and never returns signal=None just because the
    signal was already fired/stored earlier or price has since moved."""
    return await run_in_threadpool(_fetch_latest_signal_sync, symbol, tf)



# ---------------------------------------------------------------------------
# Patterns (SPEC.md 4.3)
# ---------------------------------------------------------------------------
from src.features.patterns import compute_patterns, load_patterns_params
from src.features.patterns_store import (
    fetch_candles_for_patterns,
    fetch_active_levels as fetch_active_levels_for_patterns,
    upsert_pattern_hits,
)


def _compute_and_store_patterns_sync(symbol: str, tf: str, ts, lookback_bars: int | None = None) -> dict:
    conn = get_connection()
    try:
        candles = fetch_candles_for_patterns(conn, symbol, tf, ts, lookback_bars=lookback_bars)
        levels = fetch_active_levels_for_patterns(conn, symbol, tf)
        hits = compute_patterns(candles, ts, symbol, tf, levels=levels)
        result = upsert_pattern_hits(conn, hits)
        conn.commit()
        by_pattern: dict = {}
        for h in hits:
            by_pattern[h["pattern"]] = by_pattern.get(h["pattern"], 0) + 1
        return {
            "symbol": symbol, "tf": tf, "as_of": ts.isoformat(),
            "pattern_hits": len(hits),
            "unique_patterns": len(by_pattern),
            **result,
        }
    finally:
        conn.close()


@app.post("/patterns/compute")
async def compute_patterns_endpoint(
    symbol: str = Query(...),
    tf: str = Query("M5"),
    as_of: str = Query(..., description="ISO-8601 timestamp"),
    lookback_bars: int | None = Query(None, description="Limit to the most recent N candles; None = full history (default)"),
):
    """SPEC.md 4.3. Detect all 61 TA-Lib CDL patterns up to as_of and
    upsert into pattern_hits. Levels context (at_level_id, level_strength)
    is linked from the active levels in DB at computation time."""
    try:
        ts = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=422, detail="as_of must be a valid ISO-8601 timestamp")
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    allowed_tfs = load_levels_params()["timeframes"]
    if tf not in allowed_tfs:
        raise HTTPException(status_code=422, detail=f"tf must be one of {allowed_tfs}")
    return await run_in_threadpool(_compute_and_store_patterns_sync, symbol, tf, ts, lookback_bars)

# ---------------------------------------------------------------------------
# Round numbers (SPEC.md 4.4)
# ---------------------------------------------------------------------------
from src.features.round_numbers import compute_round_numbers, load_round_numbers_params
from src.features.round_numbers_store import (
    fetch_candles_with_volume,
    upsert_round_number_hits,
    acceptance_stats,
)


def _compute_and_store_round_numbers_sync(symbol: str, tf: str, ts: datetime, lookback_bars: int | None = None) -> dict:
    conn = get_connection()
    try:
        candles = fetch_candles_with_volume(conn, symbol, tf, ts, lookback_bars=lookback_bars)
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
    lookback_bars: int | None = Query(None, description="Limit to the most recent N candles; None = full history (default)"),
):
    """SPEC.md 4.4. Compute round-number hit events up to as_of and upsert
    into round_number_hits. Returns counts by state."""
    try:
        ts = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=422, detail="as_of must be a valid ISO-8601 timestamp")
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return await run_in_threadpool(_compute_and_store_round_numbers_sync, symbol, tf, ts, lookback_bars)


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


def _compute_and_store_gaps_sync(symbol: str, tf: str, ts: datetime, lookback_bars: int | None = None) -> dict:
    conn = get_connection()
    try:
        candles = fetch_candles_gaps(conn, symbol, tf, ts, lookback_bars=lookback_bars)
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
    lookback_bars: int | None = Query(None, description="Limit to the most recent N candles; None = full history (default)"),
):
    """SPEC.md 4.5. Compute gaps up to as_of and upsert into gaps table."""
    try:
        ts = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=422, detail="as_of must be a valid ISO-8601 timestamp")
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return await run_in_threadpool(_compute_and_store_gaps_sync, symbol, tf, ts, lookback_bars)


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
# RSI + MACD divergence (PROPOSED module, not yet in SPEC.md -- see the
# 2026-09-24 task report). Deliberately isolated: NOT wired into
# module_voting_v1, check_level_reversion, or any other live-signal path.
# rules registry ids: rsi_overbought_oversold, rsi_price_divergence,
# macd_price_divergence (all status=proposed).
# ---------------------------------------------------------------------------
from src.features.rsi import (
    compute_rsi_macd_snapshot,
    compute_price_rsi_divergence,
    compute_price_macd_divergence,
    load_rsi_params,
    load_macd_params,
    load_divergence_params,
)
from src.features.rsi_store import (
    fetch_candles as fetch_candles_rsi,
    upsert_rsi_snapshots,
    upsert_divergence_events,
    fetch_latest_snapshot,
    fetch_recent_divergences,
)


def _compute_and_store_rsi_sync(symbol: str, tf: str, ts: datetime, lookback_bars: int | None = None) -> dict:
    conn = get_connection()
    try:
        candles = fetch_candles_rsi(conn, symbol, tf, ts, lookback_bars=lookback_bars)
        rsi_params = load_rsi_params()
        macd_params = load_macd_params()
        divergence_params = load_divergence_params()

        snapshot_rows = compute_rsi_macd_snapshot(candles, ts, symbol, tf, rsi_params, macd_params)
        snap_result = upsert_rsi_snapshots(conn, snapshot_rows)

        rsi_events = compute_price_rsi_divergence(candles, ts, symbol, tf, rsi_params, divergence_params)
        macd_events = compute_price_macd_divergence(candles, ts, symbol, tf, macd_params, divergence_params)
        div_result = upsert_divergence_events(conn, rsi_events + macd_events)

        conn.commit()
        by_state: dict = {}
        for r in snapshot_rows:
            by_state[r["rsi_state"]] = by_state.get(r["rsi_state"], 0) + 1
        return {
            "symbol": symbol, "tf": tf, "as_of": ts.isoformat(),
            "snapshots": len(snapshot_rows),
            "by_rsi_state": by_state,
            "snapshots_upserted": snap_result["upserted"],
            "divergences_found": {"price_rsi": len(rsi_events), "price_macd": len(macd_events)},
            "divergences_inserted": div_result["inserted"],
        }
    finally:
        conn.close()


@app.post("/rsi/compute")
async def compute_rsi_endpoint(
    symbol: str = Query(...),
    tf: str = Query("M5"),
    as_of: str = Query(..., description="ISO-8601 timestamp"),
    lookback_bars: int | None = Query(None, description="Limit to the most recent N candles; None = full history (default)"),
):
    """PROPOSED module (no SPEC.md section yet). Computes RSI(14) + MACD(12,26,9)
    up to as_of, upserts into rsi_snapshots, then runs both classic divergence
    detectors (price/RSI, price/MACD) and upserts hits into divergence_events.
    Isolated: does not touch signals, levels, or module_voting_v1."""
    try:
        ts = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=422, detail="as_of must be a valid ISO-8601 timestamp")
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return await run_in_threadpool(_compute_and_store_rsi_sync, symbol, tf, ts, lookback_bars)


@app.get("/rsi/latest")
async def get_latest_rsi(
    symbol: str = Query("XAUUSD@"),
    tf: str = Query("M5"),
    as_of: str = Query(..., description="ISO-8601 timestamp"),
):
    """Latest stored RSI/MACD snapshot at or before as_of."""
    try:
        ts = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=422, detail="as_of must be a valid ISO-8601 timestamp")
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    conn = get_connection()
    try:
        snap = fetch_latest_snapshot(conn, symbol, tf, ts)
        if snap is None:
            raise HTTPException(status_code=404, detail="no rsi_snapshots row at or before as_of")
        return snap
    finally:
        conn.close()


@app.get("/rsi/divergences")
async def get_recent_divergences(
    symbol: str = Query("XAUUSD@"),
    tf: str = Query("M5"),
    as_of: str = Query(..., description="ISO-8601 timestamp"),
    kind: str | None = Query(None, description="'price_rsi' or 'price_macd'; omit for both"),
    limit: int = Query(20, le=200),
):
    """Recent divergence_events at or before as_of, most recent first."""
    try:
        ts = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=422, detail="as_of must be a valid ISO-8601 timestamp")
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    if kind is not None and kind not in ("price_rsi", "price_macd"):
        raise HTTPException(status_code=422, detail="kind must be 'price_rsi' or 'price_macd'")
    conn = get_connection()
    try:
        return fetch_recent_divergences(conn, symbol, tf, ts, kind=kind, limit=limit)
    finally:
        conn.close()

# ---------------------------------------------------------------------------
# Matrix (PROPOSED module, SPEC.md 4.10, decision D18). Deliberately
# isolated: NOT wired into module_voting_v1, check_level_reversion, or any
# other live-signal path. rules registry id: matrix_score_mtf_agreement
# (status=proposed).
# ---------------------------------------------------------------------------
from src.features.matrix import compute_matrix_score, load_matrix_params
from src.features.matrix_store import (
    fetch_candles_by_tf,
    upsert_matrix_snapshot,
    fetch_latest_matrix_snapshot,
)


def _compute_and_store_matrix_sync(symbol: str, ts: datetime, lookback_bars: int | None = None) -> dict:
    conn = get_connection()
    try:
        params = load_matrix_params()
        candles_by_tf = fetch_candles_by_tf(conn, symbol, params["tf_list"], ts, lookback_bars)
        result = compute_matrix_score(candles_by_tf, ts, params)
        upsert_matrix_snapshot(conn, symbol, ts, result)
        conn.commit()
        return {
            "symbol": symbol, "as_of": ts.isoformat(),
            "candle_counts": {tf: len(c) for tf, c in candles_by_tf.items()},
            **result,
        }
    finally:
        conn.close()


@app.post("/matrix/compute")
async def compute_matrix_endpoint(
    symbol: str = Query(...),
    as_of: str = Query(..., description="ISO-8601 timestamp"),
    lookback_bars: int | None = Query(2000, description="Bars per timeframe (each TF fetched independently); None = full history"),
):
    """PROPOSED module (SPEC.md 4.10, D18). Computes the 10-indicator vote
    across all 7 timeframes as of `as_of`, anchors to M5, and upserts into
    matrix_snapshots. Isolated: does not touch signals, levels, or
    module_voting_v1."""
    try:
        ts = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=422, detail="as_of must be a valid ISO-8601 timestamp")
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return await run_in_threadpool(_compute_and_store_matrix_sync, symbol, ts, lookback_bars)


@app.get("/matrix/latest")
async def get_latest_matrix(
    symbol: str = Query("XAUUSD@"),
    as_of: str = Query(..., description="ISO-8601 timestamp"),
):
    """Latest stored matrix_snapshots row at or before as_of."""
    try:
        ts = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=422, detail="as_of must be a valid ISO-8601 timestamp")
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    conn = get_connection()
    try:
        snap = fetch_latest_matrix_snapshot(conn, symbol, ts)
        if snap is None:
            raise HTTPException(status_code=404, detail="no matrix_snapshots row at or before as_of")
        return snap
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


def _compute_and_store_fibonacci_sync(symbol: str, tf: str, ts: datetime, lookback_bars: int | None = None) -> dict:
    conn = get_connection()
    try:
        candles = fetch_candles(conn, symbol, tf, ts, lookback_bars=lookback_bars)
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
    lookback_bars: int | None = Query(None, description="Limit to the most recent N candles; None = full history (default)"),
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
    return await run_in_threadpool(_compute_and_store_fibonacci_sync, symbol, tf, ts, lookback_bars)


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

_REGIME_FULL_LOCK = threading.Lock()
_REGIME_INCR_LOCK = threading.Lock()

# M5 = 5 min/bar; keyed by tf string for future flexibility
_TF_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "H1": 60}


def _compute_and_store_regime_sync(symbol: str, tf: str, ts: datetime) -> dict:
    """Full recompute — loads ALL candles. Guarded by _REGIME_FULL_LOCK."""
    if not _REGIME_FULL_LOCK.acquire(blocking=False):
        raise RuntimeError("regime/compute (full) already running — retry later")
    try:
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
    finally:
        _REGIME_FULL_LOCK.release()


def _compute_and_store_regime_incremental_sync(symbol: str, tf: str, ts: datetime) -> dict:
    """Incremental recompute — loads only the lookback window needed.

    Strategy: find the last snapshot ts, load (min_bars + buffer) candles
    before it as context, compute regime for that window, upsert only the
    bars newer than the last snapshot.  Memory: O(1025 bars) instead of
    O(all history).
    """
    if not _REGIME_INCR_LOCK.acquire(blocking=False):
        raise RuntimeError("regime/compute/incremental already running — retry later")
    try:
        params = load_regime_params()
        min_bars = (
            max(params["adx_period"], params["bb_period"])
            + params["bb_width_pct_window"]
            + 5
        )
        tf_min = _TF_MINUTES.get(tf, 5)

        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT MAX(ts_utc) FROM regime_snapshots"
                    " WHERE symbol=%s AND tf_origin=%s",
                    (symbol, tf),
                )
                last_ts = cur.fetchone()[0]

            if last_ts is None:
                return {
                    "symbol": symbol, "tf": tf, "as_of": ts.isoformat(),
                    "snapshot_count": 0, "new_snapshots": 0,
                    "note": "no_prior_snapshots_run_full_first",
                }

            window_start = last_ts - timedelta(minutes=min_bars * tf_min * 3)
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT ts_utc, open, high, low, close
                       FROM candles
                       WHERE symbol=%s AND tf=%s
                         AND ts_utc >= %s AND ts_utc <= %s
                       ORDER BY ts_utc""",
                    (symbol, tf, window_start, ts),
                )
                cols = [d[0] for d in cur.description]
                candles = [dict(zip(cols, row)) for row in cur.fetchall()]

            result = compute_regime(candles, ts, symbol, tf, params=params)

            new_snaps = [s for s in result["snapshots"] if s["ts_utc"] > last_ts]
            if new_snaps:
                write = upsert_regime_snapshots(conn, new_snaps)
                conn.commit()
            else:
                write = {"inserted": 0, "updated": 0}

            return {
                "symbol": symbol, "tf": tf, "as_of": ts.isoformat(),
                "candle_window": len(candles),
                "snapshot_count": len(result["snapshots"]),
                "new_snapshots": len(new_snaps),
                **write,
            }
        finally:
            conn.close()
    finally:
        _REGIME_INCR_LOCK.release()


@app.post("/regime/compute")
@app.post("/regime/compute/full")
async def compute_regime_endpoint(
    symbol: str = Query(...),
    tf: str = Query("M5"),
    as_of: str = Query(..., description="ISO-8601 timestamp"),
):
    """SPEC.md 4.9. Full recompute — DISABLED via HTTP (causes OOM on 213k candles).
    Use /regime/compute/incremental for the timer.
    For a one-off full rebuild, run: python3 scripts/compute_regime_full.py"""
    raise HTTPException(
        status_code=503,
        detail=(
            "regime/compute (full) is disabled via HTTP to prevent OOM. "
            "Use /regime/compute/incremental for timer use. "
            "For one-off full rebuild run scripts/compute_regime_full.py on the server."
        ),
    )


@app.post("/regime/compute/incremental")
async def compute_regime_incremental_endpoint(
    symbol: str = Query(...),
    tf: str = Query("M5"),
    as_of: str = Query(..., description="ISO-8601 timestamp"),
):
    """Incremental regime update — loads only the context window (~1025 bars).
    Use this from the systemd timer every 5 minutes instead of the full endpoint."""
    try:
        ts = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=422, detail="as_of must be a valid ISO-8601 timestamp")
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    try:
        return await run_in_threadpool(_compute_and_store_regime_incremental_sync, symbol, tf, ts)
    except RuntimeError as exc:
        if "already running" in str(exc):
            raise HTTPException(status_code=503, detail=str(exc))
        raise


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


# ---------------------------------------------------------------------------
# Instrument rollover -- daily automated front-month check (docs/
# instrument_rollover.md). Pushed from the Windows MT5 box via the SAME
# restricted candlepush SSH channel already used for XAUUSD@ live candle
# ingest (scripts/candlepush.sh dispatches on the command verb) -- no new
# SSH key, no authorized_keys change, no new port. Not internet-facing
# (candlepush.sh's curl target is 172.18.0.1:8000, same as every other
# internal-only endpoint in this file).
# ---------------------------------------------------------------------------
from src.ingest.rollover import select_front_month, compute_back_adjustment, load_rollover_params
from src.ingest.instrument_contracts_store import (
    fetch_active_contract,
    upsert_instrument_contract,
    close_contract,
    insert_rollover_alert,
    fetch_rollover_alerts,
)


class RolloverCandidate(BaseModel):
    symbol: str
    trade_mode: int | None = None
    volume_window: int
    latest_close: float | None = None


class InstrumentDetection(BaseModel):
    candidates: list[RolloverCandidate]


class RolloverCheckRequest(BaseModel):
    checked_at: datetime
    instruments: dict[str, InstrumentDetection]


def _process_rollover_check_sync(payload: RolloverCheckRequest) -> dict:
    """For each instrument in the payload: pick the front-month using the
    SAME tested pure function as the initial backfill (rollover.
    select_front_month) -- Windows only collects raw candidate stats, the
    actual decision lives in one place. If the winner differs from the
    currently mapped contract, closes the old instrument_contracts row,
    opens a new one (computing the additive back-adjustment offset from
    the old/new contract's latest_close, when the old contract is still
    present in this same candidate snapshot), and inserts a rollover_alerts
    row for human review. Whitelists logical_symbol against config's
    instrument_rollover.instruments -- same discipline as the
    /memory/compute security fix, even though this channel is restricted."""
    allowed = load_rollover_params()["instruments"]
    conn = get_connection()
    results: dict[str, Any] = {}
    try:
        for logical_symbol, detection in payload.instruments.items():
            if logical_symbol not in allowed:
                results[logical_symbol] = {"status": "rejected", "reason": "not in instrument_rollover.instruments whitelist"}
                continue

            candidates = [c.model_dump() for c in detection.candidates]
            winner = select_front_month(candidates)
            if winner is None:
                results[logical_symbol] = {"status": "no_qualifying_candidate"}
                continue

            active = fetch_active_contract(conn, logical_symbol)
            if active is None:
                # Shouldn't happen post-backfill, but handle gracefully: first mapping, no offset to compute.
                upsert_instrument_contract(conn, logical_symbol, winner["symbol"], payload.checked_at, None, 0)
                conn.commit()
                results[logical_symbol] = {"status": "first_mapping", "physical_symbol": winner["symbol"]}
                continue

            if winner["symbol"] == active["physical_symbol"]:
                results[logical_symbol] = {"status": "no_change", "physical_symbol": winner["symbol"]}
                continue

            # ROLLOVER DETECTED.
            old_symbol = active["physical_symbol"]
            old_candidate = next((c for c in candidates if c["symbol"] == old_symbol), None)
            offset = 0.0
            offset_computed = False
            if old_candidate is not None and old_candidate.get("latest_close") is not None and winner.get("latest_close") is not None:
                offset = compute_back_adjustment(old_candidate["latest_close"], winner["latest_close"])
                offset_computed = True

            close_contract(conn, logical_symbol, old_symbol, payload.checked_at)
            upsert_instrument_contract(conn, logical_symbol, winner["symbol"], payload.checked_at, None, offset)
            insert_rollover_alert(conn, logical_symbol, old_symbol, winner["symbol"], offset, offset_computed)
            conn.commit()

            results[logical_symbol] = {
                "status": "ROLLOVER_DETECTED",
                "old_physical_symbol": old_symbol,
                "new_physical_symbol": winner["symbol"],
                "adjustment_offset": offset,
                "offset_computed": offset_computed,
            }
    finally:
        conn.close()
    return {"checked_at": payload.checked_at.isoformat(), "results": results}


@app.post("/instruments/rollover-check")
async def rollover_check_endpoint(payload: RolloverCheckRequest):
    """Called daily from the Windows MT5 box (Task Scheduler ->
    scripts/mt5_rollover_check.py -> candlepush SSH channel). See
    docs/instrument_rollover.md for the full design and
    src/ingest/rollover.py for the front-month selection mechanism."""
    result = await run_in_threadpool(_process_rollover_check_sync, payload)
    return result


@app.get("/instruments/rollover-alerts")
async def get_rollover_alerts(acknowledged: bool | None = Query(None)):
    """Pending (or all) rollover_alerts rows for human review."""
    conn = get_connection()
    try:
        rows = fetch_rollover_alerts(conn, acknowledged=acknowledged)
        for r in rows:
            r["detected_at"] = r["detected_at"].isoformat()
        return {"alerts": rows}
    finally:
        conn.close()

# ─── Walk-forward & holdout endpoints (SPEC.md 4.15) ─────────────────────────

class WalkForwardRequest(BaseModel):
    from_ts: datetime
    to_ts: datetime
    train_candles: int = 10080
    test_candles: int = 2016
    embargo_candles: int = 60
    purge_candles: int = 5
    symbol: str = "XAUUSD@"
    tf: str = "M5"
    voter_filter: list[str] | None = None


class HoldoutValidateRequest(BaseModel):
    rule_id: str
    symbol: str = "XAUUSD@"
    tf: str = "M5"
    voter_filter: list[str] | None = None


def _run_walk_forward_sync(payload: WalkForwardRequest) -> dict:
    from src.backtest.walk_forward import WFConfig, run_walk_forward

    config = WFConfig(
        train_candles=payload.train_candles,
        test_candles=payload.test_candles,
        embargo_candles=payload.embargo_candles,
        purge_candles=payload.purge_candles,
        tf=payload.tf,
    )
    result = run_walk_forward(
        from_ts=payload.from_ts,
        to_ts=payload.to_ts,
        config=config,
        symbol=payload.symbol,
        dry_run=True,
        voter_filter=payload.voter_filter,
    )
    folds_out = [
        {
            "fold": fr.split.fold,
            "train_start": fr.split.train_start.isoformat(),
            "train_end": fr.split.train_end.isoformat(),
            "test_start": fr.split.test_start.isoformat(),
            "test_end": fr.split.test_end.isoformat(),
            "signals": fr.oos.get("signals", 0),
            "winrate": fr.oos.get("winrate"),
            "expectancy": fr.oos.get("expectancy"),
            "pnl": fr.oos.get("pnl"),
        }
        for fr in result.folds
    ]
    # Persist run to walk_forward_runs table
    try:
        import json as _json
        from src.features.levels_store import get_connection as _get_conn
        _conn = _get_conn()
        try:
            with _conn, _conn.cursor() as _cur:
                _cur.execute(
                    """
                    INSERT INTO walk_forward_runs
                        (from_ts, to_ts, symbol, tf, config,
                         n_folds, n_signals, mean_expectancy,
                         std_expectancy, mean_winrate, fold_results)
                    VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s::jsonb)
                    """,
                    (
                        payload.from_ts, payload.to_ts,
                        payload.symbol, payload.tf,
                        _json.dumps({
                            "train_candles": payload.train_candles,
                            "test_candles": payload.test_candles,
                            "embargo_candles": payload.embargo_candles,
                            "purge_candles": payload.purge_candles,
                        }),
                        len(result.folds), result.n_signals,
                        result.mean_expectancy, result.std_expectancy,
                        result.mean_winrate,
                        _json.dumps(folds_out),
                    ),
                )
        finally:
            _conn.close()
    except Exception as _exc:
        import logging as _log
        _log.getLogger(__name__).warning("walk_forward_runs persist failed: %s", _exc)

    return {
        "n_folds": len(result.folds),
        "n_signals_total": result.n_signals,
        "mean_expectancy": result.mean_expectancy,
        "std_expectancy": result.std_expectancy,
        "mean_winrate": result.mean_winrate,
        "folds": folds_out,
    }


@app.post("/backtest/walk-forward")
async def backtest_walk_forward(payload: WalkForwardRequest):
    """SPEC.md 4.15 — Walk-forward OOS evaluation with purging/embargo.

    Always runs dry_run=True (no writes to signals table).
    to_ts must not exceed HOLDOUT boundary (2026-02-18T07:05:00Z);
    requests into HOLDOUT return 400.

    Example request:
        POST /backtest/walk-forward
        {"from_ts": "2025-01-01T00:00:00Z", "to_ts": "2026-01-01T00:00:00Z"}

    Example response:
        {"n_folds": 18, "n_signals_total": 47, "mean_expectancy": 4.2,
         "std_expectancy": 3.1, "mean_winrate": 0.55, "folds": [...]}
    """
    from src.backtest.splits import HoldoutViolation

    try:
        return await run_in_threadpool(_run_walk_forward_sync, payload)
    except HoldoutViolation as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _run_holdout_validate_sync(payload: HoldoutValidateRequest) -> dict:
    from src.backtest.walk_forward import HoldoutAlreadyTested, run_holdout_validation

    result = run_holdout_validation(
        rule_id=payload.rule_id,
        symbol=payload.symbol,
        tf=payload.tf,
        voter_filter=payload.voter_filter,
    )
    return {
        "rule_id": payload.rule_id,
        "signals": result.get("signals", 0),
        "winrate": result.get("winrate"),
        "expectancy": result.get("expectancy"),
        "pnl": result.get("pnl"),
    }


@app.post("/backtest/holdout-validate")
async def backtest_holdout_validate(payload: HoldoutValidateRequest):
    """SPEC.md 4.15 — One-shot HOLDOUT evaluation for a candidate rule.

    Each rule_id may be evaluated exactly once.  Calling again returns HTTP 409.
    Runs dry_run=True (no writes to signals table).

    Example request:
        POST /backtest/holdout-validate
        {"rule_id": "rsi_overbought_oversold"}

    Example responses:
        200: {"rule_id": "rsi_overbought_oversold", "signals": 12, "winrate": 0.58, ...}
        409: {"detail": "rule_id 'rsi_overbought_oversold' has already been tested on HOLDOUT..."}
    """
    from src.backtest.walk_forward import HoldoutAlreadyTested

    try:
        return await run_in_threadpool(_run_holdout_validate_sync, payload)
    except HoldoutAlreadyTested as exc:
        raise HTTPException(status_code=409, detail=str(exc))
