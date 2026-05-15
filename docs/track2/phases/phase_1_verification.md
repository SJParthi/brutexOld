# Track 2 Phase 1 — Verification Scaffolding

## Status: ☐ Not started

## Effort: 1 week
## Owner: Claude

## Goal

Extend Track 1's 9-layer test pyramid with **11 options-specific property
invariants** + **2 oracle tests** (options replay + Black-Scholes-Merton
parity vs `py_vollib`). All against a `FakeRollingOptionAdapter` so we
can run Phase 1 in parallel with Track 1 Phase 1/2.

## Prerequisites

- [ ] Phase 0 ✅ DONE (this PR merged)
- [ ] Track 1 Phase 1 ✅ DONE (9-layer pyramid land, 26 invariants, 4 oracles green)
- [ ] User confirms "start Track 2 Phase 1"

## Checklist (skeleton — to be filled in when Phase 1 starts)

### Test scaffolding

- [ ] `tests/fakes/fake_rolling_option_adapter.py` — synthetic bars for 252 series
- [ ] `tests/fakes/synthetic_options_data.py` — generates BSM-priced bars given (S, K, T, r, σ, q)

### 11 property invariants (P-O1 .. P-O11)

- [ ] P-O1 — Monotonic timestamps within contract span
- [ ] P-O2 — Premium / volume / OI ≥ 0
- [ ] P-O3 — OHLC consistency
- [ ] P-O4 — Strike step matches per-underlying spec
- [ ] P-O5 — `strikePosition ∈ [-10, +10]`
- [ ] P-O6 — Time-to-expiry T ≤ 1 / (365.25 × 86400) at 15:30 IST on expiry day
- [ ] P-O7 — Delta(CE) ∈ [0, 1]; Delta(PE) ∈ [-1, 0]
- [ ] P-O8 — Gamma ≥ 0
- [ ] P-O9 — Theta ≤ 0
- [ ] P-O10 — Put-call parity |C − P − (S − K·e^{-rT})| ≤ 10 paisa
- [ ] P-O11 — Audit chain unbroken across mixed Track1 + Track2 ingest

### 2 oracle tests

- [ ] **O-O1** Options-replay byte-identical (replay audit log; result Parquet matches original)
- [ ] **O-O2** BSM parity vs `py_vollib.black_scholes_merton` on 10K-sample synthetic grid (1e-10 abs tolerance)

### Smoke + contract + golden

- [ ] `tests/smoke/test_options_minimal.py` (≤30 sec; verify pipeline against 1-day fake)
- [ ] `tests/contract/test_rollingoption_schema.py` (recorded responses; catches Dhan schema drift)
- [ ] `tests/golden/options_replay_fixture.json` (pinned synthetic 1-day output)

### CI

- [ ] All 11 invariants in property-test gate
- [ ] Both oracle tests in oracle-test gate
- [ ] Smoke + contract + golden run on every PR

### 4-agent hostile review

- [ ] Spawn 4-agent hostile review on Phase 1 test design
- [ ] User signs off → ready for Phase 2 (when Track 1 Phase 2 ships)

## Verification gates

- [ ] All 11 Track 2 property invariants green
- [ ] Both Track 2 oracle tests green
- [ ] 22 CI gates green on PR
- [ ] 4-agent hostile review pass

## Exit criteria

- [ ] 11 Track 2 invariants live in CI
- [ ] 2 oracle tests live in CI
- [ ] Fake adapter shipped + smoke green
- [ ] User signs off → Phase 2 unblocked (if Track 1 Phase 2 done) or staged

## Files to create

```
brutex/options/                                  (new package; mostly empty in Phase 1)
brutex/options/__init__.py
tests/fakes/fake_rolling_option_adapter.py
tests/fakes/synthetic_options_data.py
tests/property/test_options_invariants.py        (11 invariants)
tests/integration/test_options_replay.py         (oracle O-O1)
tests/integration/test_bsm_vs_pyvollib.py        (oracle O-O2)
tests/smoke/test_options_minimal.py
tests/contract/test_rollingoption_schema.py
tests/golden/options_replay_fixture.json
```

## Decisions to lock (#241-#250 estimated)

> Numbering note: SPEC §17 (#200-#220) + OPERATIONAL_GUARANTEES §11
> (#221-#230) + Phase 2 (#231-#240) precede this range — architect
> review caught the prior #221-#230 collision. Phase 1 takes #241+.

- #241 — 12 Track 2 invariants land in CI (P-O1 .. P-O12)
- #242 — 2 Track 2 oracles land in CI (O-O1 options replay, O-O2 BSM-vs-py_vollib)
- #243 — FakeRollingOptionAdapter shipped + smoke gate
- #244 — Rust BSM kernel in `fno-math::bsm` with versioned API (Phase 2 wires PyO3)
- #245 — Contract test C-2: expiryFlag enforcement against live API (validator vs server)
- #246 — Contract test C-3: expiryCode=0 semantics on historical fromDate
- #247 — Contract test C-4: withdrawn-Monday-window response shape (Mar-Apr 2025)
- #248-#250 — TBD during implementation
