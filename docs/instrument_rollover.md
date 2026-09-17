# Instrument rollover — design doc (correlation module 4.8 prerequisite)

**Status: IMPLEMENTED AND BACKFILLED (2026-09-17) — see §6.** Written from
live `MetaTrader5` queries against the WM Markets terminal
(`xauusd-bot-windows`, `C:\Program Files\WM Markets MT5 Terminal\terminal64.exe`).

---

## 1. Confirmed symbol inventory

| logical instrument | broker symbol(s) found | contract_size | expiration_time | trade_mode | live now? |
|---|---|---|---|---|---|
| US Dollar Index | `USINDX.U26` (Sep26) | 1000 USD/pt (CFD) | **0** | 0 = DISABLED | no ticks ever (epoch 1970), 5 D1 bars exist |
| | `USINDX.Z26` (Dec26) | 1000 USD/pt (CFD) | **0** | 4 = FULL | no ticks ever (epoch 1970), 5 D1 bars exist |
| US 10Y T-Note | `10TBILL.U26` (Sep26) | 1000 USD/pt (CFD) | **0** | 0 = DISABLED | live ticks flowing, full volume history |
| | `10TBILL.Z26` (Dec26) | 1000 USD/pt (CFD) | **0** | 3 = CLOSEONLY | live ticks flowing |
| UK Brent Crude | `UKBRENT.X26` (Nov26) | 1000 USD/pt (CFD) | **0** | 4 = FULL | live ticks flowing — clear front month |
| | `UKBRENT.Z26` (Dec26) | 1000 USD/pt (CFD) | **0** | 0 = DISABLED | live ticks + 5 D1 bars (listed, not yet active) |
| | `UKBRENT.F27` (Jan27) | 1000 USD/pt (CFD) | **0** | 0 = DISABLED | zero data (not really live yet) |
| Brent spot (alt.) | `BRENTCASH` | 10 USD/lot | 1893456000 = **2030-01-01** (placeholder) | 4 = FULL | live ticks flowing |
| WTI spot (alt.) | `WTICASH` | 10 USD/lot | 1893456000 = **2030-01-01** (placeholder) | 4 = FULL | no ticks right now |

Month codes confirmed standard CME/ICE (U=Sep, Z=Dec, X=Nov, F=Jan).

**All of `contract_size`/`tick_value`/etc. requested in Task 1 are in the table
above and in the raw dumps kept in this session's scratch output** — full
`symbol_info()._asdict()` was pulled for `10TBILL.U26` and `UKBRENT.X26`
(~85 fields each); available on request if you want the complete raw dump
rather than the summary table.

---

## 2. Two corrections to the assumptions in the last message

**2.1 — `USINDX` is NOT a spot index at this broker.** It exists only as
`USINDX.U26` / `USINDX.Z26` — same futures-CFD family as the other two, with
the same CME month-code suffix convention. The plan in the previous message
("USINDX ندارد چون spot index است نه فیوچرز") does not hold — it needs the
same rollover treatment as `10TBILL.*` and `UKBRENT.*`, not an exemption.

**2.2 — Oil: `BRENTCASH`/`WTICASH` evaluated and rejected (2026-09-17).**
Beyond the 3-month `UKBRENT.*` futures family, the broker also lists
`BRENTCASH` and `WTICASH` — spot/cash CFDs needing no rollover at all (the
`2030-01-01` in their `expiration_time` is a platform placeholder, not a
contract date). These were considered as a way to skip the rollover problem
entirely for oil, but **rejected**: the user's manual test on the broker
platform showed `UKBRENT` tracks the real Brent price with higher accuracy
than `BRENTCASH`, which is presumed to be a synthetic price with more
tracking error — undesirable for the correlation module (4.8-b), which is
sensitive to the precision of price-shock timing/size. **Decision: oil uses
`UKBRENT.*` through the same rollover mechanism as the other two
instruments — not exempted, not spot.** `BRENTCASH`/`WTICASH` are recorded
here for the historical record, not deleted from consideration silently.

---

## 3. Rollover-timing mechanism — RESOLVED (user decision, 2026-09-17)

`expiration_time` is unavailable (reads `0`) for all 7 futures-family
symbols on this broker (§2 finding, unchanged) — the originally-requested
"buffer before real expiration_time" mechanism has no data source here. User
decision: use a two-stage mechanism, generic across all three instruments
(same pipeline, no per-instrument special-casing):

1. **Disqualification signal:** drop any candidate contract whose
   `symbol_info().trade_mode == 0` (`SYMBOL_TRADE_MODE_DISABLED`).
2. **Primary signal (tie-break among survivors):** among the remaining
   candidates, pick the one with the higher summed `tick_volume` over the
   last 3 D1 bars (`mt5.copy_rates_from_pos(sym, TIMEFRAME_D1, 0, 3)`).

Logical-name mapping (§4): `DXY@` → `USINDX.*`, `T10Y@` → `10TBILL.*`,
`BRENT@` → `UKBRENT.*` — the active contract for each is whatever this
two-stage rule currently selects.

### Live verification run (2026-09-17, all three instruments)

Ran the mechanism above as a one-off script against the live MT5 terminal —
no candles written, no DB touched, detection logic only:

```
=== USINDX (DXY@) ===
  USINDX.U26: trade_mode=DISABLED(0)  3day_volume=5572    -> DISQUALIFIED
  USINDX.Z26: trade_mode=FULL(4)      3day_volume=208712  -> candidate
  RESULT: front-month = USINDX.Z26  (only one non-disabled candidate)

=== 10TBILL (T10Y@) ===
  10TBILL.U26: trade_mode=DISABLED(0)   3day_volume=8715  -> DISQUALIFIED
  10TBILL.Z26: trade_mode=CLOSEONLY(3)  3day_volume=9545  -> candidate
  RESULT: front-month = 10TBILL.Z26  (only one non-disabled candidate)

=== UKBRENT (BRENT@) ===
  UKBRENT.X26: trade_mode=FULL(4)     3day_volume=184813  -> candidate
  UKBRENT.Z26: trade_mode=DISABLED(0) 3day_volume=173993  -> DISQUALIFIED
  UKBRENT.F27: trade_mode=DISABLED(0) 3day_volume=0       -> DISQUALIFIED
  RESULT: front-month = UKBRENT.X26  (only one non-disabled candidate)
```

| logical symbol | selected active contract | how it won |
|---|---|---|
| `DXY@` | `USINDX.Z26` | sole survivor after disqualification |
| `T10Y@` | `10TBILL.Z26` | sole survivor after disqualification |
| `BRENT@` | `UKBRENT.X26` | sole survivor after disqualification |

**Honest caveat, not hidden:** the 3-day-volume tie-break never actually
engaged today — in all three cases exactly one candidate survived the
`trade_mode` filter, so the mechanism reduces to "pick the only non-DISABLED
contract" right now. The tie-break logic is still needed for when two
candidates are simultaneously non-DISABLED (expected during an actual
rollover window, when the old and new contract briefly overlap) — today's
run just didn't exercise that branch.

**Second caveat:** `10TBILL.Z26` — today's selected T-Note contract — itself
has `trade_mode=CLOSEONLY`, not `FULL`. It survived only because it isn't
`DISABLED`; it is still not open for new orders at the broker right now. If
that matters for how `correlation.py` or anything else treats this
instrument (e.g. should a CLOSEONLY-but-selected contract's price still be
trusted for feature computation even though no new position could be opened
on it), that's worth a decision before implementation — flagging it now
rather than assuming it's fine.

§4–5 (symbol abstraction, back-adjustment) are unchanged and already
consistent with this mechanism.

---

## 4. Symbol abstraction (safe to finalize independent of §3)

The rest of the system (correlation module, and anything else consuming
these instruments) should never see `USINDX.U26` vs `.Z26` or `UKBRENT.X26`
vs `.Z26`/`.F27` directly. Proposed logical names, mirroring the `XAUUSD@`
pattern already used everywhere in `config/params.yaml` / `candles`:

```yaml
instrument_map:
  DXY@:   { pattern: "USINDX.*",  active: null }   # active = currently-mapped broker symbol, set by rollover job
  T10Y@:  { pattern: "10TBILL.*", active: null }
  BRENT@: { pattern: "UKBRENT.*", active: null }
```

`candles` rows would be written under the logical symbol (`DXY@`, `T10Y@`,
`BRENT@`), never the dated contract name — this is what lets `correlation.py`
and everything downstream stay ignorant of which specific contract is live,
exactly like it's already ignorant of MT5 internals for `XAUUSD@`.

`mt5.symbols_get()` **can** enumerate a name pattern live (confirmed working
in this session's script — `s.name.upper().startswith("10TBILL")` etc.), so
"list all contracts for this instrument and pick one" is mechanically
straightforward once §3 says *which one* to pick.

---

## 5. Back-adjustment methodology (general futures-data practice, not a guess)

When the active contract switches (old → new), splicing raw prices directly
creates an artificial jump with no market meaning — exactly the false-signal
risk you described for `levels`/`gaps`. Standard practice, two variants:

- **Difference (additive) adjustment — "Panama method":** at the roll instant,
  compute `offset = new_contract_price − old_contract_price`. Add `offset` to
  *every* historical bar of the old contract (and all earlier contracts
  already spliced behind it), so the continuous series stays anchored to
  today's real (new-contract) price level while shapes/differences in the
  old segment are preserved exactly.
- **Ratio (multiplicative) adjustment:** same idea but multiply by
  `new_price / old_price` instead of adding a difference. Preferred when
  computing % returns over a series spanning large price-level changes
  (common for equity indices/commodities over decades).

**Recommendation, not a decision:** this project's existing modules
(`levels`, `gaps`, `round_numbers`) are built entirely on dollar/ATR
distances, not percentage returns (CLAUDE.md's own convention) — additive
adjustment is the natural fit for consistency with that. Final call is
yours.

**Critical invariant regardless of which variant:** the back-adjusted series
is a *derived, separate* table/column from the raw per-contract prints —
never overwrite raw contract data, matching the project's existing rule of
never physically deleting historical rows (CLAUDE.md 4.2's `expired` status
rule, same principle). A real backtest that needs "what price would an order
actually have filled at on that contract" must still be able to read the
unadjusted series; only continuous-feature computation (correlation, levels
run over the synthetic instrument) uses the adjusted one.

---

## 6. Implementation status — DONE (2026-09-17)

Backfilled and live in `candles` under the logical symbols. Summary (3-year
window, matching `mt5_bridge.backfill_years`, same as `XAUUSD@`):

| logical symbol | physical contract | M1 | M5 | M15 | H1 | range |
|---|---|---|---|---|---|---|
| `DXY@` | USINDX.Z26 | 954,979 | 191,101 | 64,164 | 16,049 | 2023-09-18 .. 2026-09-17 |
| `T10Y@` | 10TBILL.Z26 | 543,036 | 174,743 | 66,040 | 17,205 | 2023-09-18 .. 2026-09-17 |
| `BRENT@` | UKBRENT.X26 | 983,142 | 200,787 | 67,140 | 16,810 | 2023-09-18 .. 2026-09-17 |

`instrument_contracts` (one row per logical symbol, not per timeframe —
"which physical contract is mapped" is one fact, independent of which
timeframe you query): all three `valid_to_ts IS NULL` (still the active
mapped contract), `adjustment_offset = 0` (nothing to splice against yet —
this is each instrument's first backfill).

Code: `src/ingest/rollover.py` (pure, 10 tests), `src/ingest/
instrument_contracts_store.py`, `src/ingest/mt5_rollover_extract.py`
(Windows-side), `src/ingest/load_rollover_candles.py` (Ubuntu-side loader).
Full test suite: 99 passed.

**Symbol-whitelist audit (per user instruction before implementation):**
grepped `src/api/main.py` for the whitelist pattern used in the
`/memory/compute` security fix (commit `dbbca43`) — it is the **only**
endpoint in the entire file with an actual symbol whitelist. Every other
endpoint (`/levels/compute`, `/round_numbers/compute`, `/gaps/*`,
`/fibonacci/*`, `/regime/*`, `/signal/latest`, `/memory/result`) accepts
`symbol` as an unwhitelisted free string and is semantically XAUUSD@-only
(support/resistance, patterns-on-levels, gaps, fibonacci-on-levels, round
numbers, regime, memory — none of them are about DXY/T10Y/BRENT). No
correlation-module endpoint exists yet to update. **Conclusion: zero
endpoints changed.** `/memory/compute`'s whitelist correctly stays
XAUUSD@-only (SPEC.md 4.10 scopes Matrix Profile memory search to gold).
This will need revisiting once SPEC.md 4.8 (`correlation`) actually gets an
API endpoint — flagging for that future work, not implemented now.

Still not implemented: the correlation module itself (SPEC.md 4.8) that
would actually *use* these three instruments, and live/scheduled rollover
re-detection (today's mapping was a one-time manual run; nothing re-checks
`trade_mode`/volume on a schedule yet).

---

## 7. Daily automated rollover-timing check -- IMPLEMENTED (2026-09-18)

The one-time manual detection in §3/§6 is now a daily job. Architecture:

```
Windows (Task Scheduler, daily 06:00 local = ~13:00 UTC currently)
  -> C:\xauusd-bot\run_rollover_check.bat
     -> mt5_rollover_check.py (MetaTrader5 API: collect raw candidate
        stats ONLY -- symbol, trade_mode, 3-day tick_volume, latest_close
        for every symbol matching each instrument's pattern; makes NO
        decision itself)
     -> pushed via the SAME restricted SSH channel already used for
        live XAUUSD@ candle ingest every 5 min (candlepush_ed25519 key,
        forced command= in Ubuntu's authorized_keys) -- extended
        scripts/candlepush.sh to dispatch on the command verb
        (send_candles vs send_rollover_check) instead of assuming
        send_candles always. NO new SSH key, NO authorized_keys change,
        NO new port.
  -> Ubuntu: POST /instruments/rollover-check
     -> whitelists logical_symbol against config's
        instrument_rollover.instruments (same discipline as the
        /memory/compute security fix)
     -> decision made by the ONE tested code path,
        src/ingest/rollover.select_front_month() -- same function the
        original backfill used, not a second copy
     -> if winner == currently mapped contract: no-op, logged as
        "no_change"
     -> if different (a real rollover): computes the additive
        back-adjustment offset FROM THIS SAME SNAPSHOT (old contract's
        latest_close vs new contract's latest_close, if the old contract
        is still present in the candidate list -- if not, offset is left
        0 and explicitly flagged offset_computed=False rather than
        guessed), closes the old instrument_contracts row, opens a new
        one, and inserts a rollover_alerts row for human review
```

**Scope boundary (deliberate, not an oversight):** a detected rollover
updates the `instrument_contracts` mapping and records the offset, but
does **not** automatically re-extract the new contract's full history or
retroactively rewrite already-stored `candles` rows with the offset. Two
reasons: (1) the framing in this task explicitly called a rollover "a
structural change to historical data for every dependent module" --
that's exactly the kind of thing this project's own principle (never
physically delete/modify historical rows, CLAUDE.md 4.2) argues against
doing silently and automatically; (2) `apply_back_adjustment()` already
exists as a pure, tested function (`src/ingest/rollover.py`) that any
consumer needing a continuous adjusted series can call at read-time using
the stored `adjustment_offset` -- raw per-contract prices stay untouched
and always available for a real backtest. A human reviewing a
`rollover_alerts` row can run the existing `mt5_rollover_extract.py` +
`load_rollover_candles.py` pair manually to bring `candles` current for
the new contract, same as the original backfill.

**DB:** new `rollover_alerts` table (never auto-deleted; `acknowledged`
flag for human review tracking).

**API:** `POST /instruments/rollover-check` (called only from the
Windows box via the restricted channel above -- not meant for direct use),
`GET /instruments/rollover-alerts` (human-readable, for checking pending
alerts).

**Live-verified end-to-end 2026-09-18:** ran the Windows script both
directly and via `Start-ScheduledTask` (`LastTaskResult=0`) -- all three
instruments correctly reported `no_change` (nothing has actually rolled
over since yesterday's backfill, as expected).

**Tests:** `tests/api/test_rollover_check_endpoint.py` (7 tests) covers
the decision logic -- no-change, rollover-with-offset,
rollover-without-offset (old contract missing, must not guess),
unknown-symbol rejection, no-qualifying-candidate, first-mapping. Full
suite: 118 passed.
