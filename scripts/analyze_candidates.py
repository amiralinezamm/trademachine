"""Report on a bt_candidates run (SPEC.md D24). Read-only: never writes
config or the signals table -- suggested weights are for human review.

    # one period: funnel, per-voter edge, threshold sweep, suggested weights
    python3 scripts/analyze_candidates.py --tag train_2024h2

    # out-of-sample check: fit weights on an EARLIER run, score a LATER one
    python3 scripts/analyze_candidates.py --tag train_2025h1 --fit-tag train_2024h2
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.backtest.candidate_analysis import (  # noqa: E402
    funnel, months_spanned, suggest_weights, summarize, threshold_sweep, voter_table,
)
from src.features.levels_store import get_connection  # noqa: E402

COLS = ("ts_utc", "gate", "exit_mode", "votes", "net_votes", "outcome", "pnl_usd",
        "risk_usd", "u_outcome", "u_pnl_usd", "rr")


def fetch(conn, tag: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(f"SELECT {', '.join(COLS)} FROM bt_candidates WHERE run_tag = %s ORDER BY ts_utc", (tag,))
        return [dict(zip(COLS, r)) for r in cur.fetchall()]


def _pct(x):
    return "—" if x is None else f"{x * 100:.1f}%"


def _num(x, fmt="{:.2f}"):
    return "—" if x is None else fmt.format(x)


def _ci(ci, pct=False):
    if not ci:
        return "—"
    lo, hi = ci
    return f"[{_pct(lo)} , {_pct(hi)}]" if pct else f"[{lo:+.2f} , {hi:+.2f}]"


def _row(label, s):
    return (f"| {label} | {s['n']} | {_pct(s['winrate'])} {_ci(s['winrate_ci'], True)} "
            f"| {_num(s['mean_r'], '{:+.2f}')} {_ci(s['mean_r_ci'])} | {_num(s['mean_usd'], '{:+.2f}')} |")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True, help="run_tag to report on")
    ap.add_argument("--fit-tag", default=None,
                    help="fit weights on THIS (earlier) run and apply them to --tag (out-of-sample)")
    ap.add_argument("--min-n", type=int, default=200, help="min trades per vote side before it earns a weight")
    args = ap.parse_args()

    conn = get_connection()
    try:
        rows = fetch(conn, args.tag)
        fit_rows = fetch(conn, args.fit_tag) if args.fit_tag else rows
    finally:
        conn.close()
    if not rows:
        sys.exit(f"no bt_candidates rows for run_tag={args.tag!r}")

    months = months_spanned(rows)
    print(f"# bt_candidates report — run_tag `{args.tag}`\n")
    print(f"{len(rows)} candidates over ~{months:.1f} months "
          f"({len(rows) / max(months, 1e-9):.0f}/month)\n")

    print("## 1. Funnel — where candidates stop, and how good those entries were")
    print("Uniform exit (same level-A stop, TP at min_rr × risk) for every row, so gates are comparable.\n")
    print("| gate | n | win rate [95% CI] | mean R [95% CI] | mean $ |")
    print("|---|---|---|---|---|")
    for gate, s in funnel(rows).items():
        print(_row(gate, s))
    print("\nIf a reject gate's mean R is not clearly below `fired`, that gate removes "
          "signals without improving them.\n")

    print("## 2. Real strategy P&L (level exits only — what live could actually send)")
    print("| subset | n | win rate [95% CI] | mean R [95% CI] | mean $ |")
    print("|---|---|---|---|---|")
    print(_row("fired", summarize([r for r in rows if r["gate"] == "fired"], uniform=False)))
    print(_row("all with a level exit", summarize(
        [r for r in rows if r["exit_mode"] == "level" and r["gate"] != "blackout"], uniform=False)))
    print()

    print("## 3. Per-voter edge (uniform exit, blackout excluded)")
    print("| voter | vote | n | win rate [95% CI] | mean R [95% CI] |")
    print("|---|---|---|---|---|")
    table = voter_table(rows)
    for name, by_v in table.items():
        for v in (1, 0, -1):
            s = by_v[v]
            if s["n"] == 0:
                continue
            print(f"| {name} | {v:+d} | {s['n']} | {_pct(s['winrate'])} {_ci(s['winrate_ci'], True)} "
                  f"| {_num(s['mean_r'], '{:+.2f}')} {_ci(s['mean_r_ci'])} |")
    print("\nVoters that only ever show `0` never took part in this period — check data "
          "coverage before judging them.\n")

    print("## 4. Threshold sweep on current net_votes")
    _print_sweep(threshold_sweep(rows, [-2, -1, 0, 1, 2, 3, 4, 5]))

    fit_table = voter_table(fit_rows) if args.fit_tag else table
    base = summarize([r for r in fit_rows if r["gate"] != "blackout"], uniform=True)
    if not base["n"] or base["winrate"] is None:
        return
    weights = suggest_weights(fit_table, base["winrate"], min_n=args.min_n)
    src = f"`{args.fit_tag}`" if args.fit_tag else "this same run (IN-SAMPLE — do not trust the sweep below)"
    print(f"## 5. Suggested evidence weights (fitted on {src}, min_n={args.min_n})")
    print("Log-odds units; 0 = no evidence yet. For human review — nothing is written to config.\n")
    print("| voter | weight | basis |")
    print("|---|---|---|")
    for name, w in sorted(weights.items(), key=lambda kv: -abs(kv[1]["weight"])):
        print(f"| {name} | {w['weight']:+.3f} | {w['reason']} |")
    print()
    wmap = {k: v["weight"] for k, v in weights.items()}
    if any(abs(v) > 0 for v in wmap.values()):
        print("## 6. Threshold sweep re-scored with the suggested weights")
        _print_sweep(threshold_sweep(rows, [-0.4, -0.2, 0.0, 0.1, 0.2, 0.3, 0.5], weights=wmap))


def _print_sweep(sweep):
    print("| threshold | live n (/month) | live win rate | live mean R | live total $ | entry n (/month) | entry mean R |")
    print("|---|---|---|---|---|---|---|")
    for s in sweep:
        lv, en = s["live"], s["entry"]
        print(f"| ≥ {s['threshold']} | {lv['n']} ({s['live_per_month']:.1f}) | {_pct(lv['winrate'])} "
              f"| {_num(lv['mean_r'], '{:+.2f}')} | {_num(lv['total_usd'], '{:+.0f}')} "
              f"| {en['n']} ({s['entry_per_month']:.1f}) | {_num(en['mean_r'], '{:+.2f}')} |")
    print()


if __name__ == "__main__":
    main()
