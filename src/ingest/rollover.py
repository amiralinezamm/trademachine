"""docs/instrument_rollover.md — front-month detection + back-adjustment.

Pure functions (CLAUDE.md rule 6: same code path everywhere) — no DB, no
MT5 access. select_front_month() operates on candidate stats already
gathered by the Windows-side extraction script
(src/ingest/mt5_rollover_extract.py); the back-adjustment functions operate
on plain candle dicts and are reused wherever a contract switch needs
splicing (initial backfill today, live rollover detection later).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

PARAMS_PATH = Path(__file__).resolve().parents[2] / "config" / "params.yaml"

SYMBOL_TRADE_MODE_DISABLED = 0


def load_rollover_params(path: Path = PARAMS_PATH) -> dict[str, Any]:
    with open(path) as f:
        return yaml.safe_load(f)["instrument_rollover"]


def select_front_month(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    """candidates: [{"symbol": str, "trade_mode": int, "volume_window": int}, ...]

    Mechanism decided 2026-09-17 (docs/instrument_rollover.md §3):
      1. Disqualify any candidate with trade_mode == SYMBOL_TRADE_MODE_DISABLED.
      2. Among survivors, pick the highest volume_window (summed tick_volume
         over the configured window — instrument_rollover.volume_window_days
         in config/params.yaml).

    Returns None if every candidate is disqualified — this is the "no
    healthy contract" case docs/instrument_rollover.md §3 says needs manual
    review, not a guessed fallback.
    """
    survivors = [c for c in candidates if c.get("trade_mode") != SYMBOL_TRADE_MODE_DISABLED]
    if not survivors:
        return None
    return max(survivors, key=lambda c: c["volume_window"])


def compute_back_adjustment(old_contract_last_close: float, new_contract_first_close: float) -> float:
    """Additive ('Panama method') offset (docs/instrument_rollover.md §5):
    added to every historical bar of the OLD contract so the spliced series
    stays anchored to the new contract's current price level, while the
    shape/differences of the old segment are preserved exactly."""
    return new_contract_first_close - old_contract_last_close


def apply_back_adjustment(candles: list[dict[str, Any]], offset: float) -> list[dict[str, Any]]:
    """Returns a NEW list with open/high/low/close shifted by `offset`.
    Never mutates the input — raw per-contract prices must stay retrievable
    for a real backtest (same never-physically-delete principle as
    levels.status='expired', CLAUDE.md 4.2)."""
    if offset == 0:
        return list(candles)
    return [
        {
            **c,
            "open": c["open"] + offset,
            "high": c["high"] + offset,
            "low": c["low"] + offset,
            "close": c["close"] + offset,
        }
        for c in candles
    ]
