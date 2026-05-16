# Track 2 Phase 4 — First Sweep + Ship

## Status: ☐ Not started

## Effort: 1 week
## Owner: Claude + User (sign-off)

## Goal

Take Track 1's top-3 winning spot signals and run the options
realism sweep:
- For each (signal, strike_position, expiry_cycle, slippage_profile):
  run the trade simulation
- Apply DSR / CPCV / WFO / Lockbox gates (same as Track 1 Phase 4)
- Produce ranked top-3 options strategies + lockbox PDF
- User signs off

## Prerequisites

- [ ] Phase 0 ✅ DONE
- [ ] Phase 1 ✅ DONE
- [ ] Phase 2 ✅ DONE
- [ ] Phase 3 ✅ DONE
- [ ] Track 1 Phase 4 ✅ DONE (top-3 spot signals already chosen + locked)
- [ ] User confirms "start Track 2 Phase 4"

## Checklist (skeleton)

### Week 1 — First sweep + filters + lockbox

- [ ] Load Track 1 top-3 spot signals from `results/track1/top3.json`
- [ ] Sweep grid (per-underlying expiry-cycle count is **regime-dependent** — hostile-review fix):
  - **Formula:** `sum_over_signals(strike_positions × expiry_cycles(underlying, signal_date) × slippage_profiles)`
  - Per-underlying expiry cycles (at sweep time, post-2024-11-13):
    - NIFTY → 2 (WEEK + MONTH)
    - BANKNIFTY → 1 (MONTH only; SEBI killed weekly)
    - SENSEX → 2 (WEEK + MONTH)
  - Sweep cardinality per signal at slippage_profiles=80, strike_positions=5:
    - NIFTY signal: 5 × 2 × 80 = 800
    - BANKNIFTY signal: 5 × 1 × 80 = 400
    - SENSEX signal: 5 × 2 × 80 = 800
  - **Total for 3 signals** depends on which underlyings: e.g., all-NIFTY = 2,400; mixed (1 each) = 2,000; all-BN = 1,200.
  - **Historical signals on BANKNIFTY pre-2024-11-13** can additionally test WEEK (2 cycles), boosting BN-historical-signal sweep to 5 × 2 × 80 = 800.
- [ ] Run sweep (single-node, M4 Pro; ~30 min wall time expected)
- [ ] Apply DSR / CPCV / WFO / Lockbox filters
- [ ] Generate top-3 options strategies (with realistic P&L distributions)
- [ ] Generate lockbox PDF
- [ ] User reviews + signs off top-3

### Track 2 SHIP

- [ ] Spawn 4-agent hostile review on Phase 4 deliverables
- [ ] Lockbox PDF signed
- [ ] Top-3 options strategies announced
- [ ] Track 2 marked ✅ SHIPPED in master checklist
- [ ] MIGRATION.md rows #271-#280 appended
- [ ] Optional: Phase 5 (live paper trading) discussion opened

## Verification gates

- [ ] All 37 invariants + 6 oracles green
- [ ] DSR > threshold; CPCV / WFO pass
- [ ] 22 CI gates green
- [ ] 4-agent hostile review pass
- [ ] Lockbox PDF generated + signed

## Exit criteria

- [ ] Top-3 options strategies locked
- [ ] Lockbox PDF signed by user
- [ ] Track 2 ✅ SHIPPED

## Decisions to lock (#271-#280 estimated)

- #271 — Track 1 top-3 spot signals locked as Phase 4 input
- #272 — Sweep grid cardinality (2,400 BNs excluded → 1,200 BN + 1,200 NIFTY/SENSEX)
- #273 — DSR / CPCV / WFO thresholds (inherit from Track 1)
- #274 — Lockbox format
- #275 — Top-3 options strategies announced
- #276 — Phase 4 4-agent hostile review pass
- #277 — Track 2 ✅ SHIPPED
- #278-#280 — TBD
