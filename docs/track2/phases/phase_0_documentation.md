# Track 2 Phase 0 — Documentation

## Status: 🔄 in progress

## Effort: 2-3 days
## Owner: Claude (this PR)

## Goal

Lock the entire Track 2 design in writing — API surface, universe,
storage layout, state machine, cost model placeholders, and the 4-week
build schedule — before any code is written. Mirrors Track 1 Phase 0.

## Prerequisites

- [x] Track 1 Phase 0 ✅ DONE (commit `225e1f7` merged)
- [x] User provided Dhan `/charts/rollingoption` API docs
- [x] 3 research agents verified API, storage, state machine
- [x] User signed off scope: 3 underlyings, NO India VIX options, 1m only,
      ATM ± 10, current expiry, IV from Dhan + Greeks computed

## Checklist

### Documentation files

- [x] `docs/track2/README.md` — directory index (commit: this PR)
- [x] `docs/track2/00_TRACK2_MASTER_CHECKLIST.md` — progress dashboard (commit: this PR)
- [x] `docs/track2/RESUME_PROMPT_TRACK2.md` — for new Claude sessions (commit: this PR)
- [x] `docs/track2/TRACK2_OPTIONS_SPEC.md` — master spec (commit: this PR)
- [x] `docs/track2/TRACK2_DATA_INGEST_DESIGN.md` — storage + state machine (commit: this PR)
- [x] `docs/track2/phases/phase_0_documentation.md` — this file (commit: this PR)
- [x] `docs/track2/phases/phase_1_verification.md` — Phase 1 skeleton (commit: this PR)
- [x] `docs/track2/phases/phase_2_dhan_options_adapter.md` — Phase 2 skeleton (commit: this PR)
- [x] `docs/track2/phases/phase_3_backtest_engine.md` — Phase 3 skeleton (commit: this PR)
- [x] `docs/track2/phases/phase_4_first_sweep.md` — Phase 4 skeleton (commit: this PR)

### Decisions to append to MIGRATION.md (Phase 0 entries)

- [ ] #200 — Track 2 = options realism leg
- [ ] #201 — `/v2/charts/rollingoption` (ATM-relative)
- [ ] #202 — 3 underlyings; NO India VIX options
- [ ] #203 — ATM ± 10 strike radius
- [ ] #204 — 1m timeframe ONLY
- [ ] #205 — Expiry cycle history reconstructed for 5y window from primary sources (BSE Notice 20230327-65; NSE FAOP/57540, 60011, 64506, 65336, 68747; SEBI master circular 2024-10-01); withdrawn Monday-window 2025-Apr explicitly excluded
- [ ] #206 — **6 backfill streams** (NIFTY-WEEK, NIFTY-MONTH, BANKNIFTY-WEEK [until 2024-11-13], BANKNIFTY-MONTH, SENSEX-WEEK, SENSEX-MONTH; SENSEX extends back to 2021-01-01 per BSE Notice 20230327-65 with thin-liquidity annotation)
- [ ] #207 — IV from Dhan; Greeks computed (BSM)
- [ ] #208 — Risk-free rate from 3-month T-bill table
- [ ] #209 — Dividend yield q = 0
- [ ] #210 — Storage ~5.5 GB / 5.37y (revised: 6 streams × 42 series × ~25 MB; SENSEX extended back to 2021-01-01; trading-day count corrected 1,250 → 1,342)
- [ ] #211 — Backfill ~15,876 calls / ~88 min wall time / 15.9% daily quota (one-time; chunk count uses ceil per reality-check fix)
- [ ] #211a-#211g — Two-hash bar integrity, CALL/PUT vocabulary lock, date-keyed lot-size table, audit serialization, step-function risk-free rate, expiry-day theta clamp, interval int
- [ ] #212 — Cost model location + versioning
- [ ] #213 — Slippage profile = 80 combos per (signal, strike, expiry)
- [ ] #214 — Money int64 paisa
- [ ] #215 — Audit kinds OPT_*
- [ ] #216 — 11 Track 2 property invariants
- [ ] #217 — 2 Track 2 oracle tests
- [ ] #218 — `brutex doctor` C12 OPTIONS_FRESH
- [ ] #219 — Track 2 effort = 6-8 weeks (after Track 1 Phase 2 ships)
- [ ] #220 — Track 2 Phase 0 4-agent hostile review pass

(MIGRATION.md update will be a separate commit in this PR.)

### 4-agent hostile review

- [ ] **Agent 1 — Architect Reviewer:** Walks the spec; finds gaps and inconsistencies between SPEC and DATA_INGEST_DESIGN.
- [ ] **Agent 2 — Hostile Reviewer:** Tries to break the design (e.g., "what if Dhan removes /rollingoption mid-year?", "what about ATM strike rollover discontinuities?").
- [ ] **Agent 3 — Reality-check Reviewer:** Cross-references claimed numbers (~15,876 calls, ~88 min wall, ~5.5 GB) against the Dhan rate limit + 30-day chunking math (ceil), with per-regime date gating + 1,342 trading-day count audited.
- [ ] **Agent 4 — Synthesis:** Merges the 3 reviews; produces a findings list.
- [ ] Operator reviews findings list and either:
  - (a) Accepts as-is + adds notes to spec, OR
  - (b) Refactors spec before merge

### Pre-merge gates

- [ ] CI green (no code changes in this PR — docs only)
- [ ] All Phase 0 checklist boxes ticked
- [ ] User signs off "ready to merge Track 2 Phase 0"
- [ ] PR flipped from draft → ready-for-review
- [ ] PR merged to main

## Verification gates

- [ ] Spec consistency: SPEC.md cross-refs DATA_INGEST_DESIGN.md correctly
- [ ] All locked decisions documented in MIGRATION.md
- [ ] No code added (Phase 0 is doc-only)
- [ ] 4-agent hostile review pass

## Exit criteria

- [ ] All Phase 0 docs merged to main
- [ ] 4-agent hostile review passed
- [ ] MIGRATION.md rows #200-#220 appended
- [ ] User signs off "Phase 0 complete; ready for Phase 1"
- [ ] `00_TRACK2_MASTER_CHECKLIST.md` Phase 0 row flipped to ✅ DONE

## Files modified by this PR

```
docs/track2/                                       (new directory)
docs/track2/README.md                              (new)
docs/track2/00_TRACK2_MASTER_CHECKLIST.md          (new)
docs/track2/RESUME_PROMPT_TRACK2.md                (new)
docs/track2/TRACK2_OPTIONS_SPEC.md                 (new, master spec)
docs/track2/TRACK2_DATA_INGEST_DESIGN.md           (new)
docs/track2/phases/                                (new directory)
docs/track2/phases/phase_0_documentation.md        (new, this file)
docs/track2/phases/phase_1_verification.md         (new)
docs/track2/phases/phase_2_dhan_options_adapter.md (new)
docs/track2/phases/phase_3_backtest_engine.md      (new)
docs/track2/phases/phase_4_first_sweep.md          (new)
docs/MIGRATION.md                                  (modified — append rows #200-#220)
```

## Risk register (Phase 0 only)

| Risk | Mitigation |
|---|---|
| Dhan `/rollingoption` schema differs from SDK source | Phase 1 contract test before Phase 2 starts |
| SEBI changes expiry cycles again mid-Phase 2 | Expiry calendar centralized in `brutex/options/expiry_calendar.py` |
| BSM model insufficient (smile not captured) | Phase 1 oracle test bounds; Phase 4 first sweep uses ATM ± 5 first |
| Cost model hallucinated | Phase 3 requires citation per tariff row; CI check enforces |

## Phase 0 deliverables summary

| Deliverable | File | Status |
|---|---|---|
| Track 2 directory + README | `docs/track2/README.md` | ✅ |
| Master checklist | `docs/track2/00_TRACK2_MASTER_CHECKLIST.md` | ✅ |
| Resume prompt | `docs/track2/RESUME_PROMPT_TRACK2.md` | ✅ |
| Master spec | `docs/track2/TRACK2_OPTIONS_SPEC.md` | ✅ |
| Data ingest design | `docs/track2/TRACK2_DATA_INGEST_DESIGN.md` | ✅ |
| Phase 0 checklist (this file) | `docs/track2/phases/phase_0_documentation.md` | ✅ |
| Phase 1-4 skeletons | `docs/track2/phases/phase_{1,2,3,4}_*.md` | ✅ |
| MIGRATION rows #200-#220 | `docs/MIGRATION.md` | ☐ pending |
| 4-agent hostile review | (spawned at PR review time) | ☐ pending |
