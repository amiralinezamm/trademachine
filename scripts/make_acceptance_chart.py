"""Generate acceptance chart for SPEC.md 4.2 — visual check of levels.

Creates a 30-day M5 candlestick chart (resampled to H1) with active
support/resistance zones overlaid. Levels are computed on the last
LEVELS_LOOKBACK_DAYS of history — enough to capture the relevant zones
while keeping runtime manageable.

Saved to docs/acceptance_chart_<date>.png for human review.
Usage: python scripts/make_acceptance_chart.py
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import matplotlib
matplotlib.use("Agg")
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import mplfinance as mpf
import pandas as pd
from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from src.features.levels import compute_levels, load_levels_params
from src.features.levels_store import get_connection, fetch_candles

SYMBOL = "XAUUSD@"
TF = "M5"
CHART_DAYS = 30        # candles shown on the chart
LEVELS_LOOKBACK_DAYS = 180  # history window for level detection (6 months)


def main():
    conn = get_connection()
    utc_now = datetime.now(timezone.utc)

    levels_since = utc_now - timedelta(days=LEVELS_LOOKBACK_DAYS)
    chart_since  = utc_now - timedelta(days=CHART_DAYS)

    print(f"Fetching M5 candles for level computation (last {LEVELS_LOOKBACK_DAYS} days)…", flush=True)
    level_candles = fetch_candles(conn, SYMBOL, TF, utc_now)
    level_candles = [c for c in level_candles if c["ts_utc"] >= levels_since]
    conn.close()
    print(f"  {len(level_candles)} candles for levels.", flush=True)

    params = load_levels_params()
    print("Computing levels…", flush=True)
    levels = compute_levels(level_candles, utc_now, SYMBOL, TF, params=params)
    active = [l for l in levels if l["status"] in ("active", "flipped")]
    broken = [l for l in levels if l["status"] == "broken"]
    expired = [l for l in levels if l["status"] == "expired"]
    print(f"  Levels: {len(active)} active/flipped, {len(broken)} broken, {len(expired)} expired.", flush=True)

    # Chart candles (last 30 days)
    chart_candles = [c for c in level_candles if c["ts_utc"] >= chart_since]
    print(f"  Chart window: {len(chart_candles)} M5 candles.", flush=True)

    # Build DataFrame, resample to H1
    df = pd.DataFrame(chart_candles)
    df["ts_utc"] = pd.to_datetime(df["ts_utc"], utc=True)
    df = df.set_index("ts_utc").sort_index()
    df = df[["open", "high", "low", "close"]]
    df["volume"] = 0
    df = df.astype(float)

    df_h1 = df.resample("1h").agg({
        "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"
    }).dropna()
    print(f"  H1 bars for chart: {len(df_h1)}", flush=True)

    price_lo = df_h1["low"].min() * 0.995
    price_hi = df_h1["high"].max() * 1.005

    support_levels    = []
    resistance_levels = []
    for lvl in active:
        mid = (float(lvl["price_low"]) + float(lvl["price_high"])) / 2.0
        if not (price_lo <= mid <= price_hi):
            continue
        if lvl["kind"] == "support":
            support_levels.append((mid, float(lvl["strength"])))
        else:
            resistance_levels.append((mid, float(lvl["strength"])))

    support_prices    = [x[0] for x in support_levels]
    resistance_prices = [x[0] for x in resistance_levels]
    print(f"  In-range: {len(support_prices)} support, {len(resistance_prices)} resistance.", flush=True)

    # --- hlines for mplfinance ---
    all_prices  = support_prices + resistance_prices
    all_colors  = ["#00cc55"] * len(support_prices) + ["#ff4444"] * len(resistance_prices)
    all_widths  = [1.0] * len(all_prices)
    all_styles  = ["-"] * len(all_prices)

    out_path = ROOT / "docs" / f"acceptance_chart_{utc_now.strftime('%Y-%m-%d')}.png"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    mc    = mpf.make_marketcolors(up="#26a69a", down="#ef5350", inherit=True)
    style = mpf.make_mpf_style(
        marketcolors=mc, gridstyle=":", facecolor="#0e1117", edgecolor="#444",
        figcolor="#0e1117", gridcolor="#333",
        rc={"axes.labelcolor": "#ccc", "xtick.color": "#aaa", "ytick.color": "#aaa",
            "axes.titlecolor": "#eee"},
    )

    plot_kwargs = dict(
        type="candle", style=style,
        title=(f"XAUUSD M5→H1 | last {CHART_DAYS} days "
               f"| levels from {LEVELS_LOOKBACK_DAYS}-day window | "
               f"{utc_now.strftime('%Y-%m-%d %H:%M UTC')}"),
        ylabel="Price (USD)", figsize=(22, 10),
        returnfig=True, warn_too_much_data=99999,
    )
    if all_prices:
        plot_kwargs["hlines"] = dict(
            hlines=all_prices, colors=all_colors,
            linestyle=all_styles, linewidths=all_widths,
        )

    fig, axes = mpf.plot(df_h1, **plot_kwargs)

    legend_handles = [
        mpatches.Patch(color="#00cc55", label=f"Support ({len(support_prices)})"),
        mpatches.Patch(color="#ff4444", label=f"Resistance ({len(resistance_prices)})"),
    ]
    axes[0].legend(handles=legend_handles, loc="upper left", fontsize=10,
                   facecolor="#1a1a2e", edgecolor="#555", labelcolor="white")

    fig.savefig(out_path, dpi=120, bbox_inches="tight", facecolor="#0e1117")
    plt.close(fig)
    print(f"✅ Chart saved → {out_path}", flush=True)
    return str(out_path)


if __name__ == "__main__":
    main()
