"""SPEC.md 4.15 — Walk-forward backtesting with purging/embargo.

run_walk_forward(): wraps run_backtest() across multiple OOS folds.
run_holdout_validation(): one-shot sealed-set evaluation, atomic guard.

CLAUDE.md rule 6: run_backtest() is called unchanged — no signal logic is
duplicated here.  Walk-forward is purely a date-range driver.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from statistics import mean, stdev
from typing import Any

from src.backtest.splits import HoldoutViolation, WFSplit, get_holdout_start, walk_forward_splits

log = logging.getLogger(__name__)


class HoldoutAlreadyTested(Exception):
    """Raised when run_holdout_validation() is called twice for the same rule_id."""


@dataclass
class WFConfig:
    train_candles: int
    test_candles: int
    embargo_candles: int
    purge_candles: int
    tf: str = "M5"


@dataclass
class FoldResult:
    split: WFSplit
    oos: dict[str, Any]


@dataclass
class WFResult:
    folds: list[FoldResult] = field(default_factory=list)
    n_signals: int = 0
    mean_expectancy: float | None = None
    std_expectancy: float | None = None
    mean_winrate: float | None = None


def run_walk_forward(
    from_ts: datetime,
    to_ts: datetime,
    config: WFConfig,
    symbol: str = "XAUUSD@",
    dry_run: bool = True,
    allow_proposed: bool | None = None,
    voter_filter: list[str] | None = None,
) -> WFResult:
    """Run walk-forward backtest across all folds within [from_ts, to_ts].

    to_ts must not extend into HOLDOUT.  dry_run=True by default.
    """
    from src.backtest.replay import run_backtest

    holdout = get_holdout_start(symbol=symbol, tf=config.tf)
    if to_ts > holdout:
        raise HoldoutViolation(
            f"to_ts {to_ts} exceeds HOLDOUT boundary {holdout}. "
            "Walk-forward must stay in the TRAIN/DEV region."
        )

    splits = list(walk_forward_splits(
        from_ts=from_ts,
        to_ts=to_ts,
        train_candles=config.train_candles,
        test_candles=config.test_candles,
        embargo_candles=config.embargo_candles,
        purge_candles=config.purge_candles,
        tf=config.tf,
    ))
    log.info("walk-forward: %d folds over %s → %s", len(splits), from_ts.date(), to_ts.date())

    fold_results: list[FoldResult] = []
    for s in splits:
        oos = run_backtest(
            from_ts=s.test_start,
            to_ts=s.test_end,
            symbol=symbol,
            tf=config.tf,
            dry_run=dry_run,
            allow_proposed=allow_proposed,
            voter_filter=voter_filter,
            holdout_mode=False,
        )
        fold_results.append(FoldResult(split=s, oos=oos))
        log.info(
            "  fold %d oos: n=%s winrate=%s expectancy=%s",
            s.fold, oos.get("signals", 0),
            oos.get("winrate"), oos.get("expectancy"),
        )

    result = WFResult(folds=fold_results)
    result.n_signals = sum(f.oos.get("signals", 0) for f in fold_results)
    expectancies = [
        f.oos["expectancy"]
        for f in fold_results
        if f.oos.get("signals", 0) > 0 and f.oos.get("expectancy") is not None
    ]
    winrates = [
        f.oos["winrate"]
        for f in fold_results
        if f.oos.get("signals", 0) > 0 and f.oos.get("winrate") is not None
    ]
    if expectancies:
        result.mean_expectancy = mean(expectancies)
        result.std_expectancy = stdev(expectancies) if len(expectancies) > 1 else 0.0
    if winrates:
        result.mean_winrate = mean(winrates)
    return result


def run_holdout_validation(
    rule_id: str,
    symbol: str = "XAUUSD@",
    tf: str = "M5",
    voter_filter: list[str] | None = None,
) -> dict[str, Any]:
    """Run the one-shot HOLDOUT evaluation for a candidate rule.

    Atomic guard: uses a conditional UPDATE (WHERE holdout_tested_at IS NULL)
    so two concurrent calls for the same rule_id cannot both proceed.
    Only the first writer gets RETURNING rows; the second raises HoldoutAlreadyTested.
    """
    from src.backtest.replay import run_backtest
    from src.features.levels_store import get_connection

    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE rules SET holdout_tested_at = now() "
                    "WHERE id = %s AND holdout_tested_at IS NULL "
                    "RETURNING id",
                    (rule_id,),
                )
                updated = cur.fetchone()
                if updated is None:
                    raise HoldoutAlreadyTested(
                        f"rule_id '{rule_id}' has already been tested on HOLDOUT "
                        "(holdout_tested_at IS NOT NULL). "
                        "Each rule may be evaluated on HOLDOUT exactly once."
                    )

        holdout_start = get_holdout_start(symbol=symbol, tf=tf, _conn=conn)
        now_ts = datetime.now(tz=timezone.utc)

        result = run_backtest(
            from_ts=holdout_start,
            to_ts=now_ts,
            symbol=symbol,
            tf=tf,
            dry_run=True,
            voter_filter=voter_filter,
            holdout_mode=True,
        )
        log.info(
            "HOLDOUT validation for '%s': n=%s winrate=%s expectancy=%s",
            rule_id, result.get("signals"), result.get("winrate"), result.get("expectancy"),
        )
        return result
    finally:
        conn.close()
