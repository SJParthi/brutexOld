# Phase 4 — Filter Cascade + First Production Sweep + SHIP 🚢

## Status: ☐ Not started

## Effort: 1-2 weeks
## Owner: Claude (implementation) + User (Phase 4 SHIP sign-off — business judgment)

## Goal

Build Stage 4 — the statistical filter cascade (Deflated Sharpe Ratio
with effective-N + CPCV + WFO + Lockbox) that takes Phase 3's top 100
candidates down to the FINAL **top 3 production strategies**.

Then run the **first production sweep** (baseline 2-4 days on the M4 Pro
envelope, no AWS; the former "overnight" figure assumed the abandoned
Stage-2.5 — DEC-SPRT-SLA-RETIRE-001).

This is the LAST PHASE. After Phase 4 ship: top 3 strategies are
**ready** for forward-paper-trading or the Options Track translation.

## Prerequisites

- [x] Phase 0 complete
- [ ] Phase 1 complete
- [ ] Phase 2 complete
- [ ] Phase 2.5 complete
- [ ] Phase 3 complete (canary measured)
- [ ] Phase 3.5 complete (GPU offload working OR documented why not)
- [ ] Read `TRACK1_BACKTEST_SPEC.md §12 Stage 4`
- [ ] User confirms "start Phase 4"

## Checklist

### Week 11 — 4-filter cascade

#### Stage 4.1 — Deflated Sharpe Ratio (DSR) with effective-N

- [ ] `brutex/strategies/stage4_dsr.py`:
  - [ ] Implement DSR per Bailey & López de Prado 2014 (DOI: 10.21314/JOR.2014.282)
  - [ ] **Effective-N via correlation clustering** (Bailey-LdP):
    - [ ] Patterns sharing ≥ 3 of K conditions cluster together
    - [ ] Cluster counts as 1 trial (not K trials)
    - [ ] Effective N drops 33B → 10K-100K (makes DSR achievable)
  - [ ] DSR threshold for top-100 candidates
- [ ] Unit tests with synthetic Sharpe distributions

#### Stage 4.2 — CPCV (Combinatorial Purged Cross-Validation)

- [ ] `brutex/strategies/stage4_cpcv.py`:
  - [ ] Generate combinatorial path splits over 5-year window
  - [ ] Purge overlapping bars between train/test
  - [ ] Per-candidate CPCV out-of-sample Sharpe distribution
- [ ] Unit tests with known-Sharpe synthetic strategies

#### Stage 4.3 — Walk-Forward Optimization (WFO)

- [ ] `brutex/strategies/stage4_wfo.py`:
  - [ ] Rolling window: train on N years, test on M months, slide
  - [ ] Walk-forward Sharpe stability metric
- [ ] Unit tests

#### Stage 4.4 — Lockbox replay

- [ ] `brutex/strategies/stage4_lockbox.py`:
  - [ ] Held-out window: Dec 2025 → May 2026 (configurable)
  - [ ] Replay top-3 candidates on lockbox without any tuning
  - [ ] Out-of-sample Sharpe must be within 30% of in-sample
- [ ] Unit tests

#### Property tests Phase 4

- [ ] Each filter has at least 1 property test
- [ ] All filter property tests green

### Week 12 — First production sweep + SHIP

#### Pre-sweep checklist

- [ ] `brutex doctor` all GREEN
- [ ] `brutex auth status` — token valid
- [ ] Lake mounted + sufficient disk (8 TB free at start, ~100 GB needed)
- [ ] No other heavy workload running
- [ ] Caffeinate enabled

#### Run the first overnight production sweep

- [ ] `brutex pull` — refresh latest data (incremental)
- [ ] `brutex sweep --stage 1A --inst NIFTY --tf 15m` — pattern discovery
- [ ] `brutex sweep --stage 1B` — expansion to 63 inst-TF
- [ ] `brutex sweep --stage 2 --night-mode` — full brute force (baseline 2-4 days; GPU projection pending measurement — Stage 2.5 abandoned, DEC-SPRT-SLA-RETIRE-001)
- [ ] `brutex sweep --stage 3` — CMA-ES refine top 100
- [ ] `brutex sweep --stage 4 --filter-cascade` — DSR → CPCV → WFO → Lockbox
- [ ] Wake up to top 3 production candidates ready

#### Post-sweep verification

- [ ] Audit chain verifies clean
- [ ] Results Parquet writes byte-identically on replay
- [ ] All 4 oracle tests green on the production sweep output
- [ ] Top 3 candidates have Lockbox OOS Sharpe within 30% of IS

#### Phase 4 SHIP sign-off

- [ ] Generate `TRACK1_SHIP_REPORT.md` with:
  - [ ] Top 3 candidate details (pattern, params, inst, TF, mode)
  - [ ] In-sample Sharpe + drawdown + win rate per candidate
  - [ ] Lockbox out-of-sample Sharpe + drawdown
  - [ ] DSR p-value (probability the strategy is real, not lucky)
  - [ ] CPCV stability metric
  - [ ] WFO drift over time
  - [ ] `vix_at_entry` / `vix_at_exit` distribution (regime analysis)
- [ ] **Spawn 4-agent hostile review** on the SHIP candidates:
  - [ ] Audit agent: are the results reproducible?
  - [ ] Architecture agent: did the pipeline behave as designed?
  - [ ] Hostile agent: is anything overfit / curve-fit?
  - [ ] Reality-check agent: are the Sharpes consistent with public benchmarks?
- [ ] **USER SIGN-OFF**: business judgment on whether to ship these 3 or rerun
- [ ] MIGRATION decision row with SHA of the SHIP commit
- [ ] Tag the SHA as `track1-v1.0` 🏁

## Verification gates

- [ ] DSR with effective-N produces sensible thresholds
- [ ] CPCV doesn't violate purge boundaries
- [ ] WFO covers full 5-year window
- [ ] Lockbox window strictly out-of-sample
- [ ] All property tests green
- [ ] All oracle tests green
- [ ] CI 22 gates green
- [ ] 4-agent hostile review report

## Exit criteria — TRACK 1 SHIPPED

- [ ] First production sweep completes successfully
- [ ] Top 3 candidates produced with full audit trail
- [ ] Lockbox out-of-sample Sharpe within 30% of in-sample
- [ ] SHIP report generated and reviewed
- [ ] User signs off on top 3 (or rejects → iterate)
- [ ] `track1-v1.0` git tag created
- [ ] 🚢 **TRACK 1 = SHIPPED**

## Files to create

```
brutex/strategies/stage4_dsr.py                    (DSR + effective-N)
brutex/strategies/stage4_cpcv.py                   (Combinatorial Purged CV)
brutex/strategies/stage4_wfo.py                    (Walk-Forward Optimization)
brutex/strategies/stage4_lockbox.py                (Held-out replay)
brutex/strategies/filter_cascade.py                (orchestrator for 4 filters)
tests/property/test_invariants.py                  (extend with filter invariants)
tests/integration/test_phase4_full_pipeline.py     (end-to-end smoke)
docs/TRACK1_SHIP_REPORT.md                         (post-sweep deliverable)
```

## Risk register

| Risk | Mitigation |
|---|---|
| All 100 Stage-3 candidates fail DSR | Effective-N clustering reduces N by orders of magnitude; should accept reasonable strategies |
| Lockbox Sharpe drops > 30% from in-sample | Track 1 SHIP rule: publish 1-2 candidates instead of 3; honest disclosure |
| CPCV violates time ordering | Purge gap explicit in implementation; integration test catches |
| Sweep overruns 24 hours | C11 canary + auto-pause + checkpoint; resume next night |
| Audit chain breaks mid-sweep | brutex doctor C4 catches; halt + replay diagnostic |
| Top 3 candidates are all variations of same idea | Conflict-graph pruning + cluster analysis catches; manual review at user sign-off |

## Decisions to lock (#151-#160 estimated)

- #151: Stage 4.1 DSR with effective-N (Bailey-LdP clustering) shipped
- #152: Stage 4.2 CPCV shipped
- #153: Stage 4.3 WFO shipped
- #154: Stage 4.4 Lockbox shipped
- #155: Filter cascade orchestrator shipped
- #156: First production sweep run + completion timestamp
- #157: Wall time measured (specific value)
- #158: Top 3 candidates: pattern + params + metrics
- #159: SHIP report generated
- #160: Track 1 v1.0 git tag created

## Phase 4 entry / exit summary

| | Before Phase 4 | After Phase 4 SHIP |
|---|---|---|
| 4-filter cascade | designed only | shipped + tested |
| First production sweep | n/a | DONE — top 3 strategies ready |
| Lockbox out-of-sample validation | n/a | PASSED (or 1-2 candidates honest-shipped) |
| MIGRATION decisions | ~150 | ~160 |
| Track 1 status | Phase 3.5 complete | 🚢 **SHIPPED v1.0** |

## What's next after Phase 4 ships

**Options Track (post-Track-1):** translate Track 1 winning signals
into ATM/ITM option fill simulations with IV/theta/spread modeling.
This is when execution realism (brokerage, STT, slippage) re-enters.
NOT part of Track 1; see `docs/TRACK1_BACKTEST_SPEC.md §1` for context.

OR:

**Forward paper trading**: run the top 3 strategies on live Groww data
(via WebSocket) for N months, compare paper PnL to backtest projection.
Out of Track 1 scope; would be Track 2 / Live Track.
