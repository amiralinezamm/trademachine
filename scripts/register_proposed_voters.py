"""One-off, idempotent: register every proposed module_voting_v1 voter's row
in `rules` so compute_votes() actually sees them (SPEC.md 4.16 / D20).

Why this exists (2026-09-29 task): module_voting.py's code for all seven
PROPOSED_VOTE_FUNCTIONS voters was complete and VOTER_IMPLEMENTATION_COMPLETE
already marks all seven True, but compute_votes() only lets a proposed-status
voter participate when `rules.status` for its id is 'testing'/'verified', or
'proposed' AND rules_registry.allow_proposed_in_voting is true (params.yaml).
That status comes from `SELECT id, status FROM rules WHERE id = ANY(...)` --
a row that was never inserted means the id is simply absent from that query's
result, so rule_status.get(rule_id) is None and the voter always
participated=False, silently voting 0 forever regardless of the flag.

Auditing the codebase found register_proposed_rule() (matrix_store.py) and
register_proposed_rules() (rsi_store.py) already existed for four of the
seven ids, but neither is called from ANY application code path (only from
tests/features/test_rsi_store.py against a rolled-back test connection) --
so it is very likely NONE of the seven have ever actually voted on a live
signal, not just the three (regime_quality, memory_pattern_bias,
dollar_correlation_direction) this task named. This script registers all
seven in one idempotent run so nothing is silently left out.

Every INSERT is `ON CONFLICT (id) DO NOTHING` (same convention throughout
src/features/*_store.py) -- a status a human/backtest has since advanced
past 'proposed' is never touched, and re-running this script is always safe.

Run once, on the server:
    cd /opt/xauusd-bot && python3 scripts/register_proposed_voters.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.features.levels_store import get_connection
from src.features.matrix_store import register_proposed_rule as register_matrix_rule
from src.features.rsi_store import register_proposed_rules as register_rsi_macd_rules
from src.features.regime_store import register_proposed_rule as register_regime_rule
from src.features.memory_store import register_proposed_rule as register_memory_rule
from src.features.correlation_store import register_dollar_correlation_direction_rule

PARAMS_PATH = Path(__file__).resolve().parents[1] / "config" / "params.yaml"

VOTER_IDS = [
    "matrix_score_mtf_agreement",
    "rsi_overbought_oversold",
    "rsi_price_divergence",
    "macd_price_divergence",
    "regime_quality",
    "memory_pattern_bias",
    "dollar_correlation_direction",
]


def _print_status(conn, label: str) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT id, status FROM rules WHERE id = ANY(%s)", (VOTER_IDS,))
        rows = dict(cur.fetchall())
    print(f"\n-- {label} --")
    for rid in VOTER_IDS:
        print(f"  {rid:32s} {rows.get(rid, '(no row)')}")


def main() -> None:
    with open(PARAMS_PATH) as f:
        params = yaml.safe_load(f)

    conn = get_connection()
    try:
        _print_status(conn, "BEFORE")

        register_matrix_rule(conn, params["matrix"])
        register_rsi_macd_rules(conn, params["rsi"], params["macd"], params["divergence"])
        register_regime_rule(conn, params["regime"])
        register_memory_rule(conn, params["memory"])
        register_dollar_correlation_direction_rule(conn, params["correlation"])
        conn.commit()

        _print_status(conn, "AFTER")

        allow_proposed = params.get("rules_registry", {}).get("allow_proposed_in_voting")
        print(f"\nrules_registry.allow_proposed_in_voting = {allow_proposed}")
        if allow_proposed is not True:
            print("WARNING: flag is not literally `true` -- proposed-status voters "
                  "above will still NOT vote until it is (params.yaml).")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
