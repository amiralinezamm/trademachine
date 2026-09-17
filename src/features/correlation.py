"""SPEC.md 4.8 (`correlation`) -- dollar correlation, oil shock, pressure/discharge.

Three independent pure functions (CLAUDE.md rule 6: no DB access here).
Fixed instrument pairs only (no free-form symbol param anywhere in this
module, by design -- see docs/instrument_rollover.md and the API layer):
  4.8-a / 4.8-c: XAUUSD@ vs DXY@
  4.8-b:         XAUUSD@ vs BRENT@ (T10Y@ intentionally NOT used yet --
                 pending a separate user decision)

Anti-lookahead: every function takes as_of_ts and drops any candle with
ts_utc > as_of_ts before computing anything, same pattern as every other
feature module in this project.

SPEC AMBIGUITY (flagged per PM-RULES.md, not silently resolved):
  4.8-b's "علامت‌ها موافق‌اند یا مخالف" ("do the signs agree or disagree?")
  does not say explicitly WHICH two quantities are compared. Two readings
  are plausible: (a) gold's concurrent 3-bar return vs oil's concurrent
  3-bar return (does gold move with or against oil during the shock?), or
  (b) gold's concurrent 3-bar return vs gold's own forward 6-bar return
  (does gold reverse its own shock-window move afterward?). The very next
  line -- "نرخ حرکت معکوس" ("rate of REVERSAL movement") -- only makes
  literal sense under reading (b): a "reversal" is a thing reversing
  itself, and the whole point of the test (compare shock-conditional
  reversal rate against the baseline reversal rate via a binomial test)
  requires a per-event boolean "did it reverse". Implemented as (b).
  Reading (a) is also computed and stored (oil_gold_concurrent_agree) since
  it's cheap and directly requested by the bullet list, but it does NOT
  feed the binomial significance test -- only (b) does. Flagging this
  explicitly rather than guessing silently.
"""
from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import talib
import yaml
from scipy.stats import binomtest

PARAMS_PATH = Path(__file__).resolve().parents[2] / "config" / "params.yaml"


def load_correlation_params(path: Path = PARAMS_PATH) -> dict[str, Any]:
    with open(path) as f:
        return yaml.safe_load(f)["correlation"]


def _align_by_ts(a: list[dict], b: list[dict]) -> tuple[list[dict], list[dict]]:
    """Inner-join two candle lists on ts_utc (both assumed same-cadence M5
    here -- SPEC.md 4.8-a's lower-frequency alignment rule doesn't apply
    since XAUUSD@/DXY@/BRENT@ all come from the same M5 feed)."""
    a_by_ts = {c["ts_utc"]: c for c in a}
    b_by_ts = {c["ts_utc"]: c for c in b}
    common = sorted(set(a_by_ts) & set(b_by_ts))
    return [a_by_ts[t] for t in common], [b_by_ts[t] for t in common]


def _returns(closes: np.ndarray) -> np.ndarray:
    """Simple percent return, closes[i] vs closes[i-1]. First element is NaN."""
    out = np.full(len(closes), np.nan)
    out[1:] = (closes[1:] - closes[:-1]) / closes[:-1]
    return out


def _dollar_diff(closes: np.ndarray, lag: int) -> np.ndarray:
    """closes[i] - closes[i-lag], NaN for the first `lag` elements."""
    out = np.full(len(closes), np.nan)
    out[lag:] = closes[lag:] - closes[:-lag]
    return out


def _rolling_sum_valid(arr: np.ndarray, window: int) -> np.ndarray:
    """Vectorized rolling sum over `window` bars (cumsum trick, O(n) not
    O(n*window)) -- NaN wherever the window isn't fully non-NaN. Needed
    because the naive per-bar-slice-and-numpy-reduce loop this module used
    at first took ~55s for a 190K-bar real backfill; this is milliseconds.
    """
    n = len(arr)
    out = np.full(n, np.nan)
    if n < window:
        return out
    valid = ~np.isnan(arr)
    filled = np.where(valid, arr, 0.0)
    csum = np.concatenate(([0.0], np.cumsum(filled)))
    ccount = np.concatenate(([0], np.cumsum(valid.astype(np.int64))))
    hi = np.arange(window - 1, n) + 1
    lo = hi - window
    counts = ccount[hi] - ccount[lo]
    sums = csum[hi] - csum[lo]
    full = counts == window
    out_tail = np.full(n - window + 1, np.nan)
    out_tail[full] = sums[full]
    out[window - 1 :] = out_tail
    return out


# ---------------------------------------------------------------------------
# 4.8-a: rolling dollar correlation
# ---------------------------------------------------------------------------

def compute_dollar_correlation(
    gold_candles: list[dict[str, Any]],
    dxy_candles: list[dict[str, Any]],
    as_of_ts: datetime,
    symbol: str,
    tf: str,
    params: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """SPEC.md 4.8-a: rolling `dollar_corr_window`-bar Pearson correlation
    between XAUUSD@ and DXY@ returns. The correlation value itself is the
    feature (not just a diagnostic)."""
    if params is None:
        params = load_correlation_params()
    window = params["dollar_corr_window"]

    gold_hist = sorted((c for c in gold_candles if c["ts_utc"] <= as_of_ts), key=lambda c: c["ts_utc"])
    dxy_hist = sorted((c for c in dxy_candles if c["ts_utc"] <= as_of_ts), key=lambda c: c["ts_utc"])
    gold_aligned, dxy_aligned = _align_by_ts(gold_hist, dxy_hist)
    n = len(gold_aligned)
    if n < window + 1:
        return []

    gold_closes = np.array([float(c["close"]) for c in gold_aligned])
    dxy_closes = np.array([float(c["close"]) for c in dxy_aligned])
    ts = [c["ts_utc"] for c in gold_aligned]

    gold_ret = _returns(gold_closes)
    dxy_ret = _returns(dxy_closes)

    # Vectorized rolling Pearson correlation via the cumsum trick (see
    # _rolling_sum_valid) instead of a per-bar slice+np.corrcoef loop --
    # the naive loop took ~55s on a 190K-bar real backfill, this is ~0.1s.
    sx = _rolling_sum_valid(gold_ret, window)
    sy = _rolling_sum_valid(dxy_ret, window)
    sxx = _rolling_sum_valid(gold_ret * gold_ret, window)
    syy = _rolling_sum_valid(dxy_ret * dxy_ret, window)
    sxy = _rolling_sum_valid(gold_ret * dxy_ret, window)

    mean_x, mean_y = sx / window, sy / window
    var_x = sxx / window - mean_x * mean_x
    var_y = syy / window - mean_y * mean_y
    cov_xy = sxy / window - mean_x * mean_y

    with np.errstate(invalid="ignore", divide="ignore"):
        corr_arr = cov_xy / np.sqrt(var_x * var_y)

    result = []
    for i in range(window, n):
        c = corr_arr[i]
        if math.isnan(c) or var_x[i] <= 0 or var_y[i] <= 0:
            continue
        result.append({
            "symbol": symbol,
            "tf": tf,
            "ts_utc": ts[i],
            "correlation": round(float(c), 6),
        })
    return result


# ---------------------------------------------------------------------------
# 4.8-b: oil shock events + significance test + lag cross-correlation
# ---------------------------------------------------------------------------

def compute_oil_shock_events(
    gold_candles: list[dict[str, Any]],
    oil_candles: list[dict[str, Any]],
    as_of_ts: datetime,
    symbol: str,
    tf: str,
    params: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """SPEC.md 4.8-b: detect oil shocks (|3-candle oil DOLLAR move| >
    2.5xATR(oil) -- dollar units, matching every other ATR-multiple
    comparison in this codebase, e.g. levels.py/round_numbers.py; the
    "return" in an ATR-multiple comparison is a price distance, not a
    percent). For each shock, record gold's own concurrent (3-bar) and
    forward (6-bar) dollar moves, and whether gold's forward move reverses
    its own concurrent move (see module docstring for the ambiguity this
    resolves)."""
    if params is None:
        params = load_correlation_params()
    ret_bars = params["oil_shock_return_bars"]
    atr_mult = params["oil_shock_atr_mult"]
    fwd_bars = params["oil_shock_forward_bars"]
    atr_period = params["atr_period"]

    gold_hist = sorted((c for c in gold_candles if c["ts_utc"] <= as_of_ts), key=lambda c: c["ts_utc"])
    oil_hist = sorted((c for c in oil_candles if c["ts_utc"] <= as_of_ts), key=lambda c: c["ts_utc"])
    gold_aligned, oil_aligned = _align_by_ts(gold_hist, oil_hist)
    n = len(gold_aligned)
    if n < atr_period + ret_bars + fwd_bars + 1:
        return []

    gold_closes = np.array([float(c["close"]) for c in gold_aligned])
    oil_highs = np.array([float(c["high"]) for c in oil_aligned])
    oil_lows = np.array([float(c["low"]) for c in oil_aligned])
    oil_closes = np.array([float(c["close"]) for c in oil_aligned])
    ts = [c["ts_utc"] for c in gold_aligned]

    oil_atr = talib.ATR(oil_highs, oil_lows, oil_closes, timeperiod=atr_period)
    oil_ret3 = _dollar_diff(oil_closes, ret_bars)
    gold_ret3 = _dollar_diff(gold_closes, ret_bars)
    gold_fwd6 = np.full(n, np.nan)
    gold_fwd6[: n - fwd_bars] = gold_closes[fwd_bars:] - gold_closes[: n - fwd_bars]

    events = []
    for i in range(ret_bars, n):
        if math.isnan(oil_atr[i]) or math.isnan(oil_ret3[i]):
            continue
        if abs(oil_ret3[i]) <= atr_mult * oil_atr[i]:
            continue

        g3 = gold_ret3[i]
        gfwd = gold_fwd6[i] if i < n - fwd_bars else None

        reversal = None
        if gfwd is not None and not math.isnan(gfwd) and g3 != 0:
            reversal = bool(np.sign(gfwd) != np.sign(g3))

        oil_gold_agree = None
        if not math.isnan(g3) and oil_ret3[i] != 0:
            oil_gold_agree = bool(np.sign(oil_ret3[i]) == np.sign(g3))

        events.append({
            "symbol": symbol,
            "tf": tf,
            "ts_utc": ts[i],
            "oil_return_3bar": round(float(oil_ret3[i]), 5),
            "oil_atr": round(float(oil_atr[i]), 5),
            "gold_return_3bar": round(float(g3), 5) if not math.isnan(g3) else None,
            "gold_return_6bar_fwd": round(float(gfwd), 5) if (gfwd is not None and not math.isnan(gfwd)) else None,
            "reversal": reversal,
            "oil_gold_concurrent_agree": oil_gold_agree,
        })
    return events


def _baseline_reversal_rate(
    gold_candles: list[dict[str, Any]],
    as_of_ts: datetime,
    ret_bars: int,
    fwd_bars: int,
) -> tuple[int, int]:
    """Reversal rate over ALL bars (not just shocks): does gold's forward
    `fwd_bars`-return reverse its own concurrent `ret_bars`-return? Returns
    (reversal_count, total_count)."""
    hist = sorted((c for c in gold_candles if c["ts_utc"] <= as_of_ts), key=lambda c: c["ts_utc"])
    n = len(hist)
    if n < ret_bars + fwd_bars + 1:
        return 0, 0
    closes = np.array([float(c["close"]) for c in hist])
    ret_c = _dollar_diff(closes, ret_bars)
    fwd = np.full(n, np.nan)
    fwd[: n - fwd_bars] = closes[fwd_bars:] - closes[: n - fwd_bars]

    reversal_count = 0
    total = 0
    for i in range(ret_bars, n - fwd_bars):
        g3, gfwd = ret_c[i], fwd[i]
        if math.isnan(g3) or math.isnan(gfwd) or g3 == 0:
            continue
        total += 1
        if np.sign(gfwd) != np.sign(g3):
            reversal_count += 1
    return reversal_count, total


def backtest_oil_shock_divergence(
    gold_candles: list[dict[str, Any]],
    oil_candles: list[dict[str, Any]],
    as_of_ts: datetime,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """SPEC.md 4.8-b validity test: shock-conditional reversal rate vs
    baseline reversal rate, binomial test for significance, plus lag
    cross-correlation (-lag_range..+lag_range) between 1-bar oil and gold
    returns to see whether oil leads.

    DESIGN DECISIONS (SPEC gives the METHOD -- binomial test -- but not a
    significance threshold or a minimum sample size):
      - alpha = params['oil_shock_significance_alpha'] (default 0.05, standard)
      - min sample = params['oil_shock_min_samples'] (default 30, reusing
        SPEC.md 4.5's own ">30" convention for "is this real" questions)
    """
    if params is None:
        params = load_correlation_params()
    ret_bars = params["oil_shock_return_bars"]
    fwd_bars = params["oil_shock_forward_bars"]
    lag_range = params["oil_shock_lag_range"]
    alpha = params["oil_shock_significance_alpha"]
    min_samples = params["oil_shock_min_samples"]

    events = compute_oil_shock_events(gold_candles, oil_candles, as_of_ts, "XAUUSD@", "M5", params)
    resolved = [e for e in events if e["reversal"] is not None]
    shock_reversals = sum(1 for e in resolved if e["reversal"])
    shock_total = len(resolved)

    baseline_reversals, baseline_total = _baseline_reversal_rate(gold_candles, as_of_ts, ret_bars, fwd_bars)

    shock_rate = shock_reversals / shock_total if shock_total > 0 else None
    baseline_rate = baseline_reversals / baseline_total if baseline_total > 0 else None

    p_value = None
    significant = False
    if shock_total >= min_samples and baseline_rate is not None and 0 < baseline_rate < 1:
        test = binomtest(shock_reversals, shock_total, baseline_rate, alternative="greater")
        p_value = float(test.pvalue)
        significant = p_value < alpha

    accepted = significant and shock_total >= min_samples
    status = "verified" if accepted else "rejected"

    # Lag cross-correlation: corr(oil_1bar_return[t], gold_1bar_return[t+lag])
    # for lag in -lag_range..+lag_range. Positive lag = oil at t correlates
    # with gold `lag` bars LATER -> oil leads gold by `lag` bars.
    gold_hist = sorted((c for c in gold_candles if c["ts_utc"] <= as_of_ts), key=lambda c: c["ts_utc"])
    oil_hist = sorted((c for c in oil_candles if c["ts_utc"] <= as_of_ts), key=lambda c: c["ts_utc"])
    gold_aligned, oil_aligned = _align_by_ts(gold_hist, oil_hist)
    lag_corr: dict[int, float | None] = {}
    if len(gold_aligned) > 2 * lag_range + 10:
        gold_r = _returns(np.array([float(c["close"]) for c in gold_aligned]))
        oil_r = _returns(np.array([float(c["close"]) for c in oil_aligned]))
        n = len(gold_r)
        for lag in range(-lag_range, lag_range + 1):
            if lag >= 0:
                a, b = oil_r[1 : n - lag], gold_r[1 + lag : n]
            else:
                a, b = oil_r[1 - lag : n], gold_r[1 : n + lag]
            mask = ~(np.isnan(a) | np.isnan(b))
            if mask.sum() < 10 or a[mask].std() == 0 or b[mask].std() == 0:
                lag_corr[lag] = None
                continue
            lag_corr[lag] = round(float(np.corrcoef(a[mask], b[mask])[0, 1]), 4)
    else:
        lag_corr = {lag: None for lag in range(-lag_range, lag_range + 1)}

    best_lag = None
    valid_lags = {k: v for k, v in lag_corr.items() if v is not None}
    if valid_lags:
        best_lag = max(valid_lags, key=lambda k: abs(valid_lags[k]))

    return {
        "shock_count": shock_total,
        "shock_reversal_count": shock_reversals,
        "shock_reversal_rate": shock_rate,
        "baseline_reversal_count": baseline_reversals,
        "baseline_total": baseline_total,
        "baseline_reversal_rate": baseline_rate,
        "p_value": p_value,
        "alpha": alpha,
        "min_samples": min_samples,
        "significant": significant,
        "rule_status": status,
        "lag_cross_correlation": lag_corr,
        "best_lag": best_lag,
        "best_lag_corr": valid_lags.get(best_lag) if best_lag is not None else None,
    }


# ---------------------------------------------------------------------------
# 4.8-c: accumulated pressure + sharp reversal (discharge)
# ---------------------------------------------------------------------------

def _rolling_ols_beta_alpha(y: np.ndarray, x: np.ndarray, window: int) -> tuple[np.ndarray, np.ndarray]:
    """Rolling single-predictor OLS (y = alpha + beta*x) over `window` bars,
    vectorized via _rolling_sum_valid (see compute_dollar_correlation for
    why: the equivalent per-bar-slice loop was the dominant cost on a
    190K-bar real backfill). Returns (beta, alpha) arrays, NaN until the
    window is fully valid (in practice this only excludes the very first
    `window` bars, since these input return series only have a single
    leading NaN)."""
    sx = _rolling_sum_valid(x, window)
    sy = _rolling_sum_valid(y, window)
    sxx = _rolling_sum_valid(x * x, window)
    sxy = _rolling_sum_valid(x * y, window)

    mean_x, mean_y = sx / window, sy / window
    var_x = sxx / window - mean_x * mean_x
    cov_xy = sxy / window - mean_x * mean_y

    with np.errstate(invalid="ignore", divide="ignore"):
        beta = cov_xy / var_x
    alpha = mean_y - beta * mean_x
    invalid = ~(var_x > 0)
    beta[invalid] = np.nan
    alpha[invalid] = np.nan
    return beta, alpha


def compute_pressure_series(
    gold_candles: list[dict[str, Any]],
    dxy_candles: list[dict[str, Any]],
    as_of_ts: datetime,
    symbol: str,
    tf: str,
    params: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """SPEC.md 4.8-c steps 1-4: rolling regression residual -> 48-bar summed
    pressure -> z-normalized -> flag when |z| > threshold sustained for
    >= min_bars consecutive bars.

    DESIGN DECISIONS (SPEC unspecified):
      - regression window: reuses 4.8-a's own 200-candle window for the
        same gold/dollar relationship (params['pressure_regression_window'])
      - z-normalization lookback: params['pressure_z_window'], default 500,
        matching regime.py's own precedent of a generous rolling baseline
        for a percentile/z-type normalization
    """
    if params is None:
        params = load_correlation_params()
    reg_window = params["pressure_regression_window"]
    sum_window = params["pressure_sum_window"]
    z_window = params["pressure_z_window"]
    flag_threshold = params["pressure_flag_threshold"]
    flag_min_bars = params["pressure_flag_min_bars"]

    gold_hist = sorted((c for c in gold_candles if c["ts_utc"] <= as_of_ts), key=lambda c: c["ts_utc"])
    dxy_hist = sorted((c for c in dxy_candles if c["ts_utc"] <= as_of_ts), key=lambda c: c["ts_utc"])
    gold_aligned, dxy_aligned = _align_by_ts(gold_hist, dxy_hist)
    n = len(gold_aligned)
    if n < reg_window + sum_window + z_window:
        return []

    gold_closes = np.array([float(c["close"]) for c in gold_aligned])
    dxy_closes = np.array([float(c["close"]) for c in dxy_aligned])
    ts = [c["ts_utc"] for c in gold_aligned]

    gold_ret = _returns(gold_closes)
    dxy_ret = _returns(dxy_closes)

    beta, alpha = _rolling_ols_beta_alpha(gold_ret, dxy_ret, reg_window)
    expected = alpha + beta * dxy_ret
    residual = gold_ret - expected

    # SPEC.md step 3: pressure = 48-bar (sum_window) rolling sum of residuals.
    pressure_raw = _rolling_sum_valid(residual, sum_window)

    # z-normalize the 48-bar pressure sum against its own rolling
    # z_window-bar history (mean/std), vectorized the same way.
    s1 = _rolling_sum_valid(pressure_raw, z_window)
    s2 = _rolling_sum_valid(pressure_raw * pressure_raw, z_window)
    mean_p = s1 / z_window
    var_p = s2 / z_window - mean_p * mean_p
    with np.errstate(invalid="ignore", divide="ignore"):
        pressure_z = (pressure_raw - mean_p) / np.sqrt(var_p)
    pressure_z[~(var_p > 0)] = np.nan

    result = []
    consecutive = 0
    for i in range(n):
        z = pressure_z[i]
        over_threshold = (not math.isnan(z)) and abs(z) > flag_threshold
        consecutive = consecutive + 1 if over_threshold else 0
        flagged = consecutive >= flag_min_bars

        result.append({
            "symbol": symbol,
            "tf": tf,
            "ts_utc": ts[i],
            "residual": round(float(residual[i]), 6) if not math.isnan(residual[i]) else None,
            "pressure_raw": round(float(pressure_raw[i]), 6) if not math.isnan(pressure_raw[i]) else None,
            "pressure_z": round(float(z), 4) if not math.isnan(z) else None,
            "flagged": bool(flagged),
        })
    return result


def _find_discharge(
    closes: np.ndarray, atr: np.ndarray, start_idx: int, pressure_sign: int,
    discharge_atr_mult: float, max_lookout_bars: int,
) -> int | None:
    """First index >= start_idx (within max_lookout_bars) where price moves
    more than discharge_atr_mult*ATR in the direction OPPOSITE pressure_sign
    (pressure positive = gold overextended up relative to dollar-implied
    level -> discharge is a DOWN move, and vice versa). Returns None if no
    such move is found within the lookout window."""
    base = closes[start_idx]
    for j in range(start_idx + 1, min(start_idx + max_lookout_bars, len(closes))):
        if math.isnan(atr[j]):
            continue
        move = closes[j] - base
        if pressure_sign > 0 and move < -discharge_atr_mult * atr[j]:
            return j
        if pressure_sign < 0 and move > discharge_atr_mult * atr[j]:
            return j
    return None


def backtest_pressure_reversal(
    gold_candles: list[dict[str, Any]],
    dxy_candles: list[dict[str, Any]],
    as_of_ts: datetime,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """SPEC.md 4.8-c validation: answers exactly the three questions SPEC
    asks, THEN applies an explicit accept/reject rule (corrected 2026-09-18
    -- an earlier version of this function always registered 'testing'
    regardless of the answers, which a user review correctly called out as
    avoiding a verdict the data already gave clearly):

      1. Is the discharge move really bigger than normal? (mean of the
         horizon return AFTER a flag, direction-adjusted so a "good"
         discharge is positive, vs the same horizon's unconditional
         baseline |return| distribution)
      2. On average how many bars after the flag does it happen?
      3. What's the success rate (fraction of flags with any qualifying
         discharge inside the lookout window)?

    DESIGN DECISION (SPEC gives the three QUESTIONS but, unlike 4.8-b's
    explicit binomial test, no numeric accept/reject formula): 'verified'
    only if the direction-adjusted mean reversal is bigger than the
    baseline's mean |return| on EVERY validation horizon (mirrors question
    1 requiring a real, not marginal, effect) AND success_rate >=
    params['pressure_min_success_rate'] (default 0.5 -- a flag that
    resolves via genuine discharge less often than a coin flip isn't a
    usable signal) AND episode_count >= params['oil_shock_min_samples']
    (reusing 4.8-b's own minimum-sample convention for the same "is this
    real" question). Otherwise 'rejected' -- same honesty standard already
    applied to oil_shock_divergence, not a softer bar for this rule.

    DESIGN DECISION (SPEC unspecified): max_lookout_bars for "was there a
    discharge at all" -- params['pressure_discharge_lookout_bars'].
    """
    if params is None:
        params = load_correlation_params()
    atr_period = params["atr_period"]
    discharge_atr_mult = params["pressure_discharge_atr_mult"]
    lookout = params["pressure_discharge_lookout_bars"]
    horizons = params["pressure_validation_horizons"]

    gold_hist = sorted((c for c in gold_candles if c["ts_utc"] <= as_of_ts), key=lambda c: c["ts_utc"])
    dxy_hist = sorted((c for c in dxy_candles if c["ts_utc"] <= as_of_ts), key=lambda c: c["ts_utc"])
    gold_aligned, dxy_aligned = _align_by_ts(gold_hist, dxy_hist)
    n = len(gold_aligned)

    series = compute_pressure_series(gold_candles, dxy_candles, as_of_ts, "XAUUSD@", "M5", params)
    if not series or n < atr_period + 1:
        return {"episode_count": 0, "answers": {}, "rule_status": "rejected"}

    gold_closes = np.array([float(c["close"]) for c in gold_aligned])
    gold_highs = np.array([float(c["high"]) for c in gold_aligned])
    gold_lows = np.array([float(c["low"]) for c in gold_aligned])
    gold_atr = talib.ATR(gold_highs, gold_lows, gold_closes, timeperiod=atr_period)
    ts_to_idx = {c["ts_utc"]: i for i, c in enumerate(gold_aligned)}

    # Flag episodes: a NEW episode starts the first bar `flagged` becomes
    # True after having been False (rising edge) -- avoids counting every
    # bar of a sustained flag as a separate episode.
    episodes = []
    was_flagged = False
    for row in series:
        if row["flagged"] and not was_flagged:
            idx = ts_to_idx.get(row["ts_utc"])
            if idx is not None:
                episodes.append({"flag_ts": row["ts_utc"], "flag_idx": idx, "pressure_sign": 1 if row["pressure_z"] > 0 else -1})
        was_flagged = row["flagged"]

    # Baseline: unconditional horizon returns across all bars.
    baseline_returns: dict[int, list[float]] = {h: [] for h in horizons}
    for h in horizons:
        for i in range(n - h):
            baseline_returns[h].append(float(gold_closes[i + h] - gold_closes[i]))

    episode_results = []
    for ep in episodes:
        idx = ep["flag_idx"]
        discharge_idx = _find_discharge(gold_closes, gold_atr, idx, ep["pressure_sign"], discharge_atr_mult, lookout)
        bars_to_discharge = (discharge_idx - idx) if discharge_idx is not None else None

        horizon_returns = {}
        for h in horizons:
            if idx + h < n:
                raw = float(gold_closes[idx + h] - gold_closes[idx])
                # direction-adjust: a "good" discharge move is negative when
                # pressure was positive (overextended up) and vice versa, so
                # flip sign for positive-pressure episodes to make "reversal
                # size" comparable/positive-is-good across both directions.
                horizon_returns[h] = -raw if ep["pressure_sign"] > 0 else raw
            else:
                horizon_returns[h] = None

        episode_results.append({
            "flag_ts": ep["flag_ts"],
            "pressure_sign": ep["pressure_sign"],
            "discharge_ts": gold_aligned[discharge_idx]["ts_utc"] if discharge_idx is not None else None,
            "bars_to_discharge": bars_to_discharge,
            "horizon_returns": horizon_returns,  # {h: value_or_None for h in params['pressure_validation_horizons']}
            # SPEC.md's own two horizons (12/24) kept as named fields too,
            # for callers/DB columns that assume exactly those two.
            "return_12bar": horizon_returns.get(12),
            "return_24bar": horizon_returns.get(24),
        })

    # Direction-adjust baseline the same way is meaningless (no pressure
    # sign to adjust against) -- baseline stays raw |return| for a fair
    # "is the episode move bigger than a typical move" comparison.
    answers: dict[int, dict[str, Any]] = {}
    for h in horizons:
        ep_vals = [e["horizon_returns"][h] for e in episode_results if e["horizon_returns"][h] is not None]
        baseline_abs = [abs(v) for v in baseline_returns[h]]
        answers[h] = {
            "episode_mean_return": round(float(np.mean(ep_vals)), 5) if ep_vals else None,
            "episode_median_return": round(float(np.median(ep_vals)), 5) if ep_vals else None,
            "baseline_mean_abs_return": round(float(np.mean(baseline_abs)), 5) if baseline_abs else None,
            "bigger_than_baseline": (
                bool(np.mean(ep_vals) > np.mean(baseline_abs)) if ep_vals and baseline_abs else None
            ),
            "sample_count": len(ep_vals),
        }

    discharged = [e for e in episode_results if e["bars_to_discharge"] is not None]
    success_rate = len(discharged) / len(episode_results) if episode_results else None
    avg_bars_to_discharge = float(np.mean([e["bars_to_discharge"] for e in discharged])) if discharged else None

    min_success_rate = params["pressure_min_success_rate"]
    min_samples = params["oil_shock_min_samples"]  # reused convention, see docstring
    bigger_on_every_horizon = bool(answers) and all(
        answers[h]["bigger_than_baseline"] is True for h in horizons
    )
    accepted = (
        bigger_on_every_horizon
        and success_rate is not None and success_rate >= min_success_rate
        and len(episode_results) >= min_samples
    )
    rule_status = "verified" if accepted else "rejected"

    return {
        "episode_count": len(episode_results),
        "episodes": episode_results,
        "answers": {
            "is_reversal_bigger_than_normal": answers,
            "avg_bars_to_discharge": round(avg_bars_to_discharge, 2) if avg_bars_to_discharge is not None else None,
            "success_rate": round(success_rate, 4) if success_rate is not None else None,
        },
        "rule_status": rule_status,
    }
