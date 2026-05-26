# Track 2 Phase 2 — Groww Options Adapter

## Status: ⏸️ BLOCKED on Track 1 Phase 2 ship

## Effort: 2 weeks
## Owner: Claude

## Goal

Implement the production Groww `/v2/charts/rollingoption` adapter:
authentication (shared with Track 1), rate limiting (shared 3 r/s
token bucket), the **6-stream** backfill driver, daily incremental,
state-machine recovery, Greeks computation, and the `brutex doctor`
C12 OPTIONS_FRESH check.

## Prerequisites

- [ ] Phase 0 ✅ DONE
- [ ] Phase 1 ✅ DONE (11 invariants + 2 oracles green vs fake)
- [ ] **Track 1 Phase 2 ✅ DONE** (Groww adapter + spot lake live)
- [ ] User has Groww access token issued; auto-renew loop green
- [ ] User confirms "start Track 2 Phase 2"

## Checklist (skeleton — fills in when Phase 2 starts)

### Week 1 — Adapter + backfill

- [ ] `brutex/adapters/groww/options_constants.py` — hardcoded underlying + exchange + segment + strike step + lot size for 3 underlyings
- [ ] `brutex/adapters/groww/options_client.py` — typed wrapper around `/v2/charts/rollingoption`
- [ ] `brutex/adapters/groww/options_backfill.py` — **6-stream** driver + state machine (per-stream date gating)
- [ ] `brutex/options/expiry_calendar.py` — per-underlying expiry day registry (SEBI-aware)
- [ ] `brutex/options/strike_grid.py` — per-underlying strike step
- [ ] `brutex/options/risk_free.py` — 60-row monthly 5y T-bill table
- [ ] `brutex/data/options_partitioner.py` — write to `data/options/<exch>/<segment>/<underlying>/<cycle>/ATM<offset>/<type>/<YYYY>/<MM>.parquet`
- [ ] `brutex/orchestrator/options_state.py` — watermark + state-machine transitions
- [ ] Audit kinds `OPT_INGEST_CHUNK_OK/ERR` wired
- [ ] 5y backfill green on real Groww against all **6 streams** (~88 min wall time, ~15,876 calls; ~5.5 GB storage)

### Week 2 — Greeks + incremental + doctor

- [ ] `rust/fno-math/src/bsm.rs` — Rust Black-Scholes-Merton kernel
- [ ] `rust/fno-math-py/src/bsm.rs` — PyO3 bindings exposed as `fno_math.bsm.greeks(...)`
- [ ] `brutex/options/greeks.py` — wires Rust kernel into the per-chunk Greeks pipeline
- [ ] Audit kind `OPT_GREEK_COMPUTED` wired (sampled 1/1000)
- [ ] Daily incremental driver
- [ ] `brutex/cli/cmd_doctor.py` — add C12 OPTIONS_FRESH check
- [ ] Re-measure: P-O10 put-call parity holds across all 252 series on real Groww data
- [ ] Re-measure: backfill wall time on M4 Pro

### Phase 2 sign-off

- [ ] All Phase 1 invariants + oracles still green on real data
- [ ] `brutex doctor` C12 GREEN
- [ ] Spawn 4-agent hostile review on Phase 2 deliverables
- [ ] User signs off → ready for Phase 3

## Verification gates

- [ ] All 37 property invariants green (26 Track 1 + 11 Track 2)
- [ ] All 6 oracle tests green (4 Track 1 + 2 Track 2)
- [ ] 22 CI gates green
- [ ] 4-agent hostile review pass
- [ ] Backfill replay byte-identical (oracle O-O1)

## Exit criteria

- [ ] 5y options lake live (~5.5 GB)
- [ ] Daily incremental runs in < 60 sec
- [ ] All 252 series tracked by watermark
- [ ] Doctor C12 GREEN
- [ ] User signs off

## Files to create / modify

```
brutex/adapters/groww/options_constants.py        (new)
brutex/adapters/groww/options_client.py           (new)
brutex/adapters/groww/options_backfill.py         (new)
brutex/options/                                  (new package)
brutex/options/expiry_calendar.py
brutex/options/strike_grid.py
brutex/options/risk_free.py
brutex/options/greeks.py
brutex/data/options_partitioner.py               (new)
brutex/orchestrator/options_state.py             (new)
brutex/cli/cmd_doctor.py                         (modify; add C12)
rust/fno-math/src/bsm.rs                         (new)
rust/fno-math-py/src/bsm.rs                      (new)
```

## Decisions to lock (#231-#240 estimated)

- #231 — Groww options adapter wired (CALL/PUT vocabulary; integer interval)
- #232 — **6-stream** backfill driver (with per-stream date gating + dormant-after-2024-11-13 BN-WEEK)
- #233 — Expiry calendar in `brutex/options/expiry_calendar.py` (SEBI-aware; encodes 5y regime changes from SPEC §2 with withdrawn-Monday-window excluded)
- #234 — Strike grid (date-keyed historical verification) + **lot-size history table** (NOT hardcoded; SEBI revisions handled per SPEC §17 #211c)
- #235 — Risk-free rate table v1 (step-function lookup, no interpolation per §13)
- #236 — Greeks computed inline (Rust BSM); BSM_KERNEL_VERSION tracked in audit; expiry-day T-clamp at 1 min
- #237 — Daily incremental driver
- #238 — `brutex doctor` C12 wired (5 active + 1 dormant)
- #239 — Measured backfill wall time recorded
- #240 — Phase 2 4-agent hostile review pass
