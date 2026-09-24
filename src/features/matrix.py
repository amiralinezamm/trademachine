"""SPEC.md 4.10 (`matrix`) -- multi-timeframe indicator voting engine (D18).

Reference: Matrix Arrow Indicator MTF (MQL5 product/75011). Ten standard
indicators vote buy/sell/neutral independently per timeframe; a timeframe
"votes" only when >= matrix_min_votes of the 10 agree. M5 (D2) is the pivotal
timeframe -- matrix_score counts how many of the other 6 timeframes agree
with M5's direction (0-6). matrix_score is fusion input only (like `regime`)
-- it never issues a signal by itself.

Pure module (CLAUDE.md rule 6, no DB): compute_matrix_score() takes
candles_by_tf (already-fetched candle lists, one per timeframe) + as_of_ts
and returns the components.matrix dict verbatim (SPEC.md 4.10 JSON shape).
The IO split (fetch each of the 7 timeframes, call this, store) lives in
matrix_store.py / the API endpoint.

Anti-repaint (CLAUDE.md rules 1-3): every timeframe's candle list is cut to
ts_utc <= as_of_ts here, independently of what the caller passed in (same
defensive pattern as levels.py). Each indicator is evaluated on the LAST
bar of that filtered, sorted list -- i.e. the most recent CLOSED bar ("t-1"
in the SPEC's MT4/5 terms; "t", the still-forming bar, is never in the
candles table to begin with, so the cutoff alone is sufficient). Calling
with two different as_of_ts values that both fall strictly inside the same
still-forming bar's window must therefore return identical results, since
no new closed bar exists in between -- this is asserted directly in
tests/features/test_matrix.py.

--- Per-indicator vote conventions (DESIGN DECISIONS -- SPEC.md 4.10 lists
the 10 indicators and their role but not the exact vote rule; these are
standard, conventional readings for each indicator, documented here rather
than left as unexplained magic behaviour, per CLAUDE.md's "no unexplained
choice" spirit) ---

This matrix measures DIRECTIONAL AGREEMENT across timeframes (D18: does
H1 point the same way M5 does), not overbought/oversold REVERSAL timing
-- that is what the separate rsi_macd_divergence module (SPEC.md 4.19)
already does with 70/30 thresholds. Using the same 70/30 reversal
convention here would answer a different question ("is price
overextended") than what matrix_score is defined to measure ("is the
trend confirmed across timeframes"), so every oscillator below votes on
its TREND/MIDLINE convention, not its overbought/oversold convention:

  RSI(14):            > 50 buy, < 50 sell (midline momentum direction)
  Stochastic(14,3,3):  %K > %D buy, %K < %D sell (classic crossover)
  CCI(14):             > 0 buy, < 0 sell (zero-line, not +-100 extremes)
  MACD(12,26,9):        macd line > signal buy, < signal sell (crossover)
  Moving Average:       close > SMA(period) buy, < sell (classic trend filter)
  ADX(14):              ADX itself is non-directional (strength only);
                        direction comes from its companion +DI/-DI lines
                        (standard practice) -- +DI > -DI buy, else sell
  Williams %R(14):      > -50 buy, < -50 sell (midline, mirrors RSI/Stoch)
  Momentum(10):         > 0 buy, < 0 sell (price above/below N bars ago)
  Bulls & Bears Power:  Elder's bull=high-EMA(13), bear=low-EMA(13);
                        reduced to one vote via net = bull + bear (both
                        measured from the same EMA baseline) > 0 buy, < 0 sell
  Volume:               confirms the bar's own direction only when volume
                        is above its SMA(period) average; close>open AND
                        volume>avg -> buy, close<open AND volume>avg ->
                        sell, otherwise neutral (no confirmation)

Every threshold/period is a params.yaml value (matrix.indicator_periods),
never hardcoded.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import talib
import yaml

PARAMS_PATH = Path(__file__).resolve().parents[2] / "config" / "params.yaml"

Vote = str  # "buy" | "sell" | "neutral"


def load_matrix_params(path: Path = PARAMS_PATH) -> dict[str, Any]:
    with open(path) as f:
        return yaml.safe_load(f)["matrix"]


def _closed(candles: list[dict[str, Any]], as_of_ts: datetime) -> list[dict[str, Any]]:
    return sorted((c for c in candles if c["ts_utc"] <= as_of_ts), key=lambda c: c["ts_utc"])


def _last_valid(arr: np.ndarray) -> float | None:
    if len(arr) == 0 or np.isnan(arr[-1]):
        return None
    return float(arr[-1])


def _side(value: float | None, low: float = 0.0) -> Vote:
    if value is None:
        return "neutral"
    if value > low:
        return "buy"
    if value < low:
        return "sell"
    return "neutral"


def compute_indicator_votes(
    candles: list[dict[str, Any]],
    as_of_ts: datetime,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Ten indicator votes for ONE timeframe's candle history, evaluated on
    the last closed bar <= as_of_ts. Returns {"votes": {...10 keys...},
    "buy_count", "sell_count", "neutral_count", "tf_vote"} where tf_vote is
    "buy"/"sell"/"neutral" depending on whether >= min_votes agree (SPEC.md
    4.10: "وقتی حداقل matrix_min_votes از ۱۰ ... هم‌جهت باشند")."""
    if params is None:
        params = load_matrix_params()
    p = params["indicator_periods"]
    min_votes = params["min_votes"]

    hist = _closed(candles, as_of_ts)
    votes: dict[str, Vote] = {}

    min_bars = max(p["ma_period"], p["macd_slow"] + p["macd_signal"], p["adx_period"] * 2) + 2
    if len(hist) < min_bars:
        empty = {k: "neutral" for k in (
            "RSI", "Stochastic", "CCI", "MACD", "MovingAverage", "ADX",
            "WilliamsR", "Momentum", "BullsBearsPower", "Volume",
        )}
        return {"votes": empty, "buy_count": 0, "sell_count": 0, "neutral_count": 10, "tf_vote": "neutral"}

    highs = np.array([float(c["high"]) for c in hist])
    lows = np.array([float(c["low"]) for c in hist])
    closes = np.array([float(c["close"]) for c in hist])
    opens = np.array([float(c["open"]) for c in hist])
    volumes = np.array([float(c.get("tick_volume") or 0) for c in hist])

    # RSI -- midline convention (see module docstring; NOT overbought/oversold).
    rsi = talib.RSI(closes, timeperiod=p["rsi_period"])
    votes["RSI"] = _side(_last_valid(rsi), low=50.0)

    # Stochastic -- %K vs %D crossover.
    slowk, slowd = talib.STOCH(
        highs, lows, closes,
        fastk_period=p["stoch_fastk"], slowk_period=p["stoch_slowk"], slowk_matype=0,
        slowd_period=p["stoch_slowd"], slowd_matype=0,
    )
    k_val, d_val = _last_valid(slowk), _last_valid(slowd)
    if k_val is None or d_val is None:
        votes["Stochastic"] = "neutral"
    else:
        votes["Stochastic"] = "buy" if k_val > d_val else ("sell" if k_val < d_val else "neutral")

    # CCI -- zero-line convention.
    cci = talib.CCI(highs, lows, closes, timeperiod=p["cci_period"])
    votes["CCI"] = _side(_last_valid(cci), low=0.0)

    # MACD -- line vs signal crossover.
    macd_line, macd_signal, _ = talib.MACD(
        closes, fastperiod=p["macd_fast"], slowperiod=p["macd_slow"], signalperiod=p["macd_signal"],
    )
    m_val, s_val = _last_valid(macd_line), _last_valid(macd_signal)
    if m_val is None or s_val is None:
        votes["MACD"] = "neutral"
    else:
        votes["MACD"] = "buy" if m_val > s_val else ("sell" if m_val < s_val else "neutral")

    # Moving Average -- price vs SMA (classic trend filter).
    ma = talib.SMA(closes, timeperiod=p["ma_period"])
    ma_val = _last_valid(ma)
    close_val = closes[-1]
    if ma_val is None:
        votes["MovingAverage"] = "neutral"
    else:
        votes["MovingAverage"] = "buy" if close_val > ma_val else ("sell" if close_val < ma_val else "neutral")

    # ADX -- direction from +DI/-DI (ADX itself is non-directional).
    plus_di = talib.PLUS_DI(highs, lows, closes, timeperiod=p["adx_period"])
    minus_di = talib.MINUS_DI(highs, lows, closes, timeperiod=p["adx_period"])
    pdi_val, mdi_val = _last_valid(plus_di), _last_valid(minus_di)
    if pdi_val is None or mdi_val is None:
        votes["ADX"] = "neutral"
    else:
        votes["ADX"] = "buy" if pdi_val > mdi_val else ("sell" if pdi_val < mdi_val else "neutral")

    # Williams %R -- midline convention (-50), mirrors RSI/Stochastic.
    willr = talib.WILLR(highs, lows, closes, timeperiod=p["willr_period"])
    votes["WilliamsR"] = _side(_last_valid(willr), low=-50.0)

    # Momentum -- sign of MOM.
    mom = talib.MOM(closes, timeperiod=p["momentum_period"])
    votes["Momentum"] = _side(_last_valid(mom), low=0.0)

    # Bulls & Bears Power (Elder) -- net of bull/bear power from one EMA baseline.
    ema = talib.EMA(closes, timeperiod=p["bbp_ema_period"])
    ema_val = _last_valid(ema)
    if ema_val is None:
        votes["BullsBearsPower"] = "neutral"
    else:
        bull = highs[-1] - ema_val
        bear = lows[-1] - ema_val
        votes["BullsBearsPower"] = _side(bull + bear, low=0.0)

    # Volume -- confirms the bar's own direction only above its own average.
    vol_sma = talib.SMA(volumes, timeperiod=p["volume_period"])
    vol_avg = _last_valid(vol_sma)
    if vol_avg is None or volumes[-1] <= vol_avg:
        votes["Volume"] = "neutral"
    else:
        votes["Volume"] = "buy" if closes[-1] > opens[-1] else ("sell" if closes[-1] < opens[-1] else "neutral")

    buy_count = sum(1 for v in votes.values() if v == "buy")
    sell_count = sum(1 for v in votes.values() if v == "sell")
    neutral_count = 10 - buy_count - sell_count

    if buy_count >= min_votes:
        tf_vote = "buy"
    elif sell_count >= min_votes:
        tf_vote = "sell"
    else:
        tf_vote = "neutral"

    return {
        "votes": votes,
        "buy_count": buy_count,
        "sell_count": sell_count,
        "neutral_count": neutral_count,
        "tf_vote": tf_vote,
    }


def compute_matrix_score(
    candles_by_tf: dict[str, list[dict[str, Any]]],
    as_of_ts: datetime,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """SPEC.md 4.10. candles_by_tf: {"M1": [...], "M5": [...], ...} for
    (a subset of) matrix.tf_list -- missing timeframes are treated as
    'neutral' votes (matrix_score can only ever be a lower bound if a
    timeframe's data is unavailable, never fabricated).

    Returns the exact components.matrix JSON shape from SPEC.md 4.10:
    {"score": int 0-6, "direction": "buy"|"sell"|"neutral",
     "timeframes": {tf: "buy"|"sell"|"neutral", ...}}.

    M5 is pivotal (D2): if M5 doesn't vote (tf_vote == "neutral"),
    score=0 and direction="neutral" regardless of the other timeframes.
    """
    if params is None:
        params = load_matrix_params()
    tf_list: list[str] = params["tf_list"]
    pivot_tf = "M5"

    tf_votes: dict[str, str] = {}
    for tf in tf_list:
        candles = candles_by_tf.get(tf, [])
        result = compute_indicator_votes(candles, as_of_ts, params)
        tf_votes[tf] = result["tf_vote"]

    pivot_direction = tf_votes.get(pivot_tf, "neutral")
    if pivot_direction == "neutral":
        return {"score": 0, "direction": "neutral", "timeframes": tf_votes}

    other_tfs = [tf for tf in tf_list if tf != pivot_tf]
    score = sum(1 for tf in other_tfs if tf_votes[tf] == pivot_direction)

    return {"score": score, "direction": pivot_direction, "timeframes": tf_votes}
