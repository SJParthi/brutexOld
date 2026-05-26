# Track 2 Phase 3 — Options Backtest Engine

## Status: ☐ Not started

## Effort: 2-3 weeks
## Owner: Claude

## Goal

Translate a Track 1 winning spot signal into a realistic options trade:
- Pick `(option_strike, option_type, expiry_cycle)` per entry
- Simulate fills with brokerage / STT / STO / SEBI / GST / slippage
- Return option-realistic P&L vector for downstream DSR/CPCV/WFO
  filters (same as Track 1 Phase 4)

## Prerequisites

- [ ] Phase 0 ✅ DONE
- [ ] Phase 1 ✅ DONE
- [ ] Phase 2 ✅ DONE (options lake + Greeks live)
- [ ] Track 1 Phase 3 ✅ DONE (engine + 5-stage funnel)
- [ ] User confirms "start Phase 3"

## Checklist (skeleton)

### Week 1 — Cost model + execution clock

- [ ] `brutex/options/costs.py` — versioned cost model
  - [ ] Groww brokerage row (cite URL + verified date)
  - [ ] STT rates (NSE + BSE circulars cited)
  - [ ] STO (Securities Transaction on Options) rates
  - [ ] SEBI turnover charge rate
  - [ ] NSE / BSE transaction fee rates
  - [ ] GST 18% application rule
  - [ ] Stamp duty 0.003% buy-side rule
- [ ] `docs/track2/COSTS_VERIFIED.md` — every row cited (URL + verified date)
- [ ] CI check: every cost row has a citation
- [ ] `brutex/options/execution_clock.py` — bar-step engine (entry at signal-bar+1 OPEN)
- [ ] Decision: brokerage / STT version stamped on every backtest row

### Week 2 — Strike selection + slippage

- [ ] `brutex/options/strike_picker.py` — given (signal, spot_at_entry), pick `(strike_position, option_type, expiry_cycle)`
  - [ ] Default: ATM CE for long bull; ATM PE for long bear
  - [ ] Sweep grid: `strike_position ∈ {-2, -1, 0, +1, +2}` × `expiry ∈ {WEEK, MONTH}` (NIFTY/SENSEX) or `{MONTH}` (BANKNIFTY)
- [ ] `brutex/options/slippage.py` — 3-knob slippage model (80 combos)
- [ ] Audit kinds `OPT_BACKTEST_ENTRY/EXIT` wired
- [ ] Replay oracle test extended to cover options trades

### Week 3 — Engine integration + DSR/CPCV pass-through

- [ ] `brutex/engine/options_runner.py` — given (track1_signal_top3, sweep_grid), runs option trades + writes results.parquet
- [ ] Result schema extends Track 1's with: `option_strike`, `option_type`, `expiry_cycle`, `entry_premium_paisa`, `exit_premium_paisa`, `cost_paisa`, `slippage_profile_hash`, `greeks_at_entry` (delta/gamma/theta/vega)
- [ ] DSR / CPCV / WFO / Lockbox gates from Track 1 Phase 4 apply unchanged to the options result table

### Phase 3 sign-off

- [ ] All Phase 1 + 2 tests still green
- [ ] Spawn 4-agent hostile review on cost model + execution clock
- [ ] User signs off → ready for Phase 4

## Verification gates

- [ ] All 37 property invariants green
- [ ] All 6 oracle tests green
- [ ] 22 CI gates green
- [ ] 4-agent hostile review pass
- [ ] Every cost row has a verified citation (CI check)

## Exit criteria

- [ ] Cost model locked + cited
- [ ] Execution clock + slippage model shipped
- [ ] Options runner produces results.parquet for a Track 1 top-3 signal
- [ ] User signs off

## Files to create / modify

```
brutex/options/costs.py                     (new, versioned)
brutex/options/execution_clock.py           (new)
brutex/options/strike_picker.py             (new)
brutex/options/slippage.py                  (new)
brutex/engine/options_runner.py             (new)
docs/track2/COSTS_VERIFIED.md               (new, citations)
tests/integration/test_options_replay.py    (extend with option trades)
```

## Decisions to lock (#251-#270 estimated)

- #251 — Cost model schema + versioning
- #252 — Brokerage row cited
- #253 — STT row cited
- #254 — STO row cited
- #255 — SEBI charge row cited
- #256 — Exchange transaction fee row cited
- #257 — GST application rule
- #258 — Stamp duty rule
- #259 — Execution clock at bar-N+1 OPEN (consistent with Track 1)
- #260 — Strike picker default + sweep grid
- #261 — Slippage 80-combo grid
- #262 — Options runner result schema
- #263-#270 — TBD during implementation
