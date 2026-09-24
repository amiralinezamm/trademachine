"""RSI + MACD momentum module (proposed — not yet in SPEC.md, see final report).

Three independent pieces, all pure (no DB access — CLAUDE.md rule 6, backtest
and live call the exact same functions):

  compute_rsi_macd_snapshot() -- part A: RSI(14) + MACD(12,26,9) per closed
      bar, plus the RSI overbought/oversold state.
  compute_price_rsi_divergence()  -- part B: classic price/RSI divergence.
  compute_price_macd_divergence() -- part C: classic price/MACD divergence.

Anti-lookahead (CLAUDE.md rules 1-3): every function takes as_of_ts and
drops any candle with ts_utc > as_of_ts before anything else runs. TA-Lib's
RSI/MACD are already causal (bar i only uses closes[0..i]), so the cutoff
on the input candle list is sufficient to make the indicator series itself
anti-lookahead. Swing points used by the divergence detectors additionally
need swing_n bars *after* them to be confirmed (CLAUDE.md rule 2 -- no
repainting), exactly like levels.py's swing confirmation.

Swing detection: levels.py's `_confirm_swing()` was evaluated for reuse and
NOT used here. It IS a pure function (highs/lows/atr arrays + index), so
importing it was technically possible, but:
  1. It's underscore-prefixed -- not a published/reusable API, and importing
     a private helper across modules creates coupling its author didn't
     intend.
  2. Its definition requires a retracement of at least swing_k * ATR after
     the extreme, because levels.py needs "swing worth building a S/R zone
     around". Classic price/oscillator divergence (Wilder's RSI, Appel's
     MACD) is defined on plain local price pivots -- no dollar-retracement
     filter. Reusing it would silently drop real pivots and would tie this
     module's behaviour to swing_n/swing_k values tuned for zone sizing,
     not for divergence.
A simple standalone fractal-pivot detector (_find_swings) is implemented
below instead, with its own dedicated params.yaml block (divergence.swing_n),
so the two concepts don't share a knob that means different things.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import numpy as np
import talib
import yaml

PARAMS_PATH = Path(__file__).resolve().parents[2] / "config" / "params.yaml"


def load_rsi_params(path: Path = PARAMS_PATH) -> dict[str, Any]:
    with open(path) as f:
        return yaml.safe_load(f)["rsi"]


def load_macd_params(path: Path = PARAMS_PATH) -> dict[str, Any]:
    with open(path) as f:
        return yaml.safe_load(f)["macd"]


def load_divergence_params(path: Path = PARAMS_PATH) -> dict[str, Any]:
    with open(path) as f:
        return yaml.safe_load(f)["divergence"]


# ---------------------------------------------------------------------------
# Part A -- RSI + MACD snapshot per bar
# ---------------------------------------------------------------------------

def compute_rsi_macd_snapshot(
    candles: list[dict[str, Any]],
    as_of_ts: datetime,
    symbol: str,
    tf: str,
    rsi_params: dict[str, Any] | None = None,
    macd_params: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """RSI(14) + MACD(12,26,9) on closed candles up to as_of_ts.

    Returns one dict per bar (matching the `rsi_snapshots` table columns)
    for every bar where RSI is defined (i.e. past TA-Lib's warm-up window).
    rsi_state is 'overbought' | 'oversold' | 'neutral' against the
    configurable overbought/oversold thresholds (SPEC-proposed default
    70/30).
    """
    if rsi_params is None:
        rsi_params = load_rsi_params()
    if macd_params is None:
        macd_params = load_macd_params()

    hist = sorted((c for c in candles if c["ts_utc"] <= as_of_ts), key=lambda c: c["ts_utc"])
    n = len(hist)

    rsi_period = rsi_params["period"]
    slow = macd_params["slow_period"]
    signal_p = macd_params["signal_period"]
    # TA-Lib MACD warm-up is roughly slow+signal bars before it stabilizes;
    # require a bit more so both series are meaningfully defined together.
    min_bars = max(rsi_period, slow + signal_p) + 2
    if n < min_bars:
        return []

    closes = np.array([float(c["close"]) for c in hist])
    ts = [c["ts_utc"] for c in hist]

    rsi = talib.RSI(closes, timeperiod=rsi_period)
    macd_line, macd_signal, macd_hist = talib.MACD(
        closes,
        fastperiod=macd_params["fast_period"],
        slowperiod=macd_params["slow_period"],
        signalperiod=macd_params["signal_period"],
    )

    overbought = rsi_params["overbought"]
    oversold = rsi_params["oversold"]

    result = []
    for i in range(n):
        if np.isnan(rsi[i]) or np.isnan(macd_line[i]) or np.isnan(macd_signal[i]):
            continue
        rsi_val = float(rsi[i])
        if rsi_val >= overbought:
            state = "overbought"
        elif rsi_val <= oversold:
            state = "oversold"
        else:
            state = "neutral"
        result.append({
            "symbol": symbol,
            "tf": tf,
            "ts_utc": ts[i],
            "rsi": rsi_val,
            "rsi_state": state,
            "macd": float(macd_line[i]),
            "macd_signal": float(macd_signal[i]),
            "macd_hist": float(macd_hist[i]),
        })
    return result


# ---------------------------------------------------------------------------
# Shared swing-pivot detector (used by both divergence detectors)
# ---------------------------------------------------------------------------

def _find_swings(
    highs: np.ndarray, lows: np.ndarray, ts: list[datetime], swing_n: int
) -> list[dict[str, Any]]:
    """Plain fractal pivots: bar i is a swing high if it is the strict max
    of the window [i-swing_n, i+swing_n], swing low if the strict min.
    Confirmed at idx = i + swing_n (CLAUDE.md rule 2 -- no repainting: a
    swing at bar i is not usable until swing_n bars after it exist)."""
    n = len(highs)
    swings: list[dict[str, Any]] = []
    for i in range(swing_n, n - swing_n):
        window_hi = highs[i - swing_n:i + swing_n + 1]
        window_lo = lows[i - swing_n:i + swing_n + 1]
        if highs[i] == window_hi.max() and (highs[i] > np.delete(window_hi, swing_n)).all():
            swings.append({"idx": i, "confirmed_idx": i + swing_n, "ts": ts[i], "price": float(highs[i]), "type": "high"})
        if lows[i] == window_lo.min() and (lows[i] < np.delete(window_lo, swing_n)).all():
            swings.append({"idx": i, "confirmed_idx": i + swing_n, "ts": ts[i], "price": float(lows[i]), "type": "low"})
    return swings


def _detect_divergence(
    swings: list[dict[str, Any]],
    indicator: np.ndarray,
    last_confirmed_idx: int,
    lookback_bars: int,
    kind: str,
) -> list[dict[str, Any]]:
    """Compare each pair of CONSECUTIVE same-type swings (the classic
    definition: two consecutive swing highs, or two consecutive swing lows).
    Only swings already confirmed as of last_confirmed_idx are considered.
    Only pairs within lookback_bars of each other (by bar distance) count --
    a "divergence" between swings months apart is not a signal."""
    events: list[dict[str, Any]] = []
    for typ, is_bearish_kind in (("high", True), ("low", False)):
        same_type = [s for s in swings if s["type"] == typ and s["confirmed_idx"] <= last_confirmed_idx]
        same_type.sort(key=lambda s: s["idx"])
        for prev, last in zip(same_type, same_type[1:]):
            if last["idx"] - prev["idx"] > lookback_bars:
                continue
            ind_prev = indicator[prev["idx"]]
            ind_last = indicator[last["idx"]]
            if np.isnan(ind_prev) or np.isnan(ind_last):
                continue
            if is_bearish_kind:
                # Bearish: price makes a HIGHER high, indicator makes a LOWER high.
                fired = last["price"] > prev["price"] and ind_last < ind_prev
                direction = "bearish"
            else:
                # Bullish: price makes a LOWER low, indicator makes a HIGHER low.
                fired = last["price"] < prev["price"] and ind_last > ind_prev
                direction = "bullish"
            if fired:
                events.append({
                    "kind": kind,
                    "direction": direction,
                    "swing1_ts": prev["ts"],
                    "swing2_ts": last["ts"],
                    "swing1_price": prev["price"],
                    "swing2_price": last["price"],
                    "swing1_indicator": float(ind_prev),
                    "swing2_indicator": float(ind_last),
                    "confirmed_ts": None,  # filled by caller (needs ts[last['confirmed_idx']])
                    "_confirmed_idx": last["confirmed_idx"],
                })
    return events


def _finalize_events(events: list[dict[str, Any]], ts: list[datetime], symbol: str, tf: str) -> list[dict[str, Any]]:
    out = []
    for e in events:
        e = dict(e)
        e["confirmed_ts"] = ts[e.pop("_confirmed_idx")]
        e["symbol"] = symbol
        e["tf"] = tf
        out.append(e)
    return out


# ---------------------------------------------------------------------------
# Part B -- classic price / RSI divergence
# ---------------------------------------------------------------------------

def compute_price_rsi_divergence(
    candles: list[dict[str, Any]],
    as_of_ts: datetime,
    symbol: str,
    tf: str,
    rsi_params: dict[str, Any] | None = None,
    divergence_params: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Bearish: price higher high, RSI lower high. Bullish: price lower low,
    RSI higher low. Returns divergence_events rows with kind='price_rsi'."""
    if rsi_params is None:
        rsi_params = load_rsi_params()
    if divergence_params is None:
        divergence_params = load_divergence_params()

    hist = sorted((c for c in candles if c["ts_utc"] <= as_of_ts), key=lambda c: c["ts_utc"])
    n = len(hist)
    swing_n = divergence_params["swing_n"]
    rsi_period = rsi_params["period"]
    min_bars = rsi_period + 2 * swing_n + 2
    if n < min_bars:
        return []

    highs = np.array([float(c["high"]) for c in hist])
    lows = np.array([float(c["low"]) for c in hist])
    closes = np.array([float(c["close"]) for c in hist])
    ts = [c["ts_utc"] for c in hist]

    rsi = talib.RSI(closes, timeperiod=rsi_period)

    last_confirmed_idx = n - 1
    swings = _find_swings(highs, lows, ts, swing_n)
    events = _detect_divergence(swings, rsi, last_confirmed_idx, divergence_params["lookback_bars"], kind="price_rsi")
    return _finalize_events(events, ts, symbol, tf)


# ---------------------------------------------------------------------------
# Part C -- classic price / MACD divergence
# ---------------------------------------------------------------------------

def compute_price_macd_divergence(
    candles: list[dict[str, Any]],
    as_of_ts: datetime,
    symbol: str,
    tf: str,
    macd_params: dict[str, Any] | None = None,
    divergence_params: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Same swing/divergence logic as compute_price_rsi_divergence(), against
    the MACD LINE (not the histogram) -- see macd.divergence_source in
    params.yaml and the final report for why the line was chosen. Returns
    divergence_events rows with kind='price_macd'."""
    if macd_params is None:
        macd_params = load_macd_params()
    if divergence_params is None:
        divergence_params = load_divergence_params()

    hist = sorted((c for c in candles if c["ts_utc"] <= as_of_ts), key=lambda c: c["ts_utc"])
    n = len(hist)
    swing_n = divergence_params["swing_n"]
    slow = macd_params["slow_period"]
    signal_p = macd_params["signal_period"]
    min_bars = (slow + signal_p) + 2 * swing_n + 2
    if n < min_bars:
        return []

    highs = np.array([float(c["high"]) for c in hist])
    lows = np.array([float(c["low"]) for c in hist])
    closes = np.array([float(c["close"]) for c in hist])
    ts = [c["ts_utc"] for c in hist]

    macd_line, macd_signal, macd_hist = talib.MACD(
        closes,
        fastperiod=macd_params["fast_period"],
        slowperiod=macd_params["slow_period"],
        signalperiod=macd_params["signal_period"],
    )
    source: Literal["macd_line", "histogram"] = macd_params.get("divergence_source", "macd_line")
    indicator = macd_line if source == "macd_line" else macd_hist

    last_confirmed_idx = n - 1
    swings = _find_swings(highs, lows, ts, swing_n)
    events = _detect_divergence(swings, indicator, last_confirmed_idx, divergence_params["lookback_bars"], kind="price_macd")
    return _finalize_events(events, ts, symbol, tf)
