# Instrument rollover — design doc (correlation module 4.8 prerequisite)

**Status: DRAFT — rollover-timing detection section is BLOCKED, see §3.** Written
2026-09-17 from live `MetaTrader5` queries against the WM Markets terminal
(`xauusd-bot-windows`, `C:\Program Files\WM Markets MT5 Terminal\terminal64.exe`).
No code written yet — architecture only, per PM-RULES.md golden rule (stop and
ask when the SPEC/assumption doesn't hold, don't guess).

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

**2.2 — Oil: confirmed `UKBRENT.*` is not the only option.** Beyond the
3-month futures family you already knew about (X26/Z26/F27), the broker also
lists **`BRENTCASH`** and **`WTICASH`** — spot/cash CFDs with no real
expiration (the `2030-01-01` in `expiration_time` is a platform placeholder,
not a contract date; confirmed by checking `start_time` too, which is
2016-03-2016-04 for these — clearly an "always open" instrument, not a
dated contract). **These need no rollover logic at all.** Worth a decision:
oil correlation could use `BRENTCASH` directly and skip the entire rollover
problem for that one leg, at the cost of it being a broker CFD price rather
than the literal ICE Brent front-month print. Your call — not assuming this.

---

## 3. BLOCKED — rollover-timing detection cannot use `expiration_time` as designed

The message asked for detection "بر مبنای expiration_time واقعی از MT5" with
a 5–8 business day buffer. **Checked `expiration_time` on all 7 futures-family
symbols above (`USINDX.*`, `10TBILL.*`, `UKBRENT.*`): every single one reads
`0`.** This broker does not populate that MT5 field for these CFD-wrapped
futures products (unlike `BRENTCASH`/`WTICASH`, which at least carry a — non-
real — placeholder). There is no `symbol_info` field carrying a real
settlement/expiry date for any of these three instruments on this broker.

This is exactly the "SPEC/plan doesn't hold" case PM-RULES.md says to stop
on rather than substitute a guess. Two things point toward *a* front-month
signal without expiration_time, but neither is the clean mechanism you asked
for, and I'm not picking one without your input:

- **`trade_mode`** does distinguish cleanly for Brent (X26=FULL / Z26,F27=
  DISABLED — unambiguous), but **not** for the T-Note (U26=DISABLED yet still
  quoting live with full volume; Z26=CLOSEONLY, neither is FULL). If DISABLED
  can mean either "expiring, wind-down only" (U26's case) or "not yet
  activated" (UKBRENT.Z26's case), `trade_mode` alone can't tell those apart.
- **Live-tick presence + volume trend** (checked: U26 volume ≈ Z26 volume,
  both actively quoting right now) doesn't cleanly signal an approaching
  T-Note rollover either.

**Question for you:** how do you want front-month/rollover-timing determined,
given MT5's `expiration_time` is unavailable for these three instruments?
Options I see, no preference implied:
1. Use `trade_mode` transitions as the live trigger (works for Brent, needs a
   tie-break rule for T-Note — e.g. "if both are non-FULL, prefer the one
   with fresher/higher tick_volume").
2. Fall back to parsing the month code from the symbol name against a fixed
   CME/ICE calendar rule for each instrument's real last-trading-day
   convention (this is the "پارس‌کردن حرف از نام" approach you explicitly
   said not to use — flagging it only because it's the remaining option, not
   proposing it).
3. Something else — e.g. ask the broker/account manager whether they expose
   a rollover-date list elsewhere (contract specification page, not the API).

Nothing below this line is implemented; §4–5 describe the parts of the design
that don't depend on this open question.

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

## 6. Next step

Waiting on your decision for §3 before writing any code — no rollover
implementation, no backfill started, per your instruction.
