# Track 1 — Master Checklist Dashboard

> **Single-glance progress dashboard.** Updated as each phase ticks
> boxes. The first phase with unchecked boxes is the current work front.

Last updated: 2026-05-15 evening

## Overall progress

```
   Phase 0  ✅✅✅✅✅✅✅✅✅✅  100% (3 PRs merged: #12 #13 #14)
   Phase 1  ☐☐☐☐☐☐☐☐☐☐    0% (Not started)
   Phase 2  ☐☐☐☐☐☐☐☐☐☐    0% (Blocked: needs Dhan token)
   Phase 2.5☐☐☐☐☐☐☐☐☐☐    0% (Not started)
   Phase 3  ☐☐☐☐☐☐☐☐☐☐    0% (Not started)
   Phase 3.5☐☐☐☐☐☐☐☐☐☐    0% (Not started)
   Phase 4  ☐☐☐☐☐☐☐☐☐☐    0% (Not started)
```

## Phase summary

| Phase | Status | Effort | Owner | What it produces |
|---|---|---|---|---|
| 0 — Documentation | ✅ DONE | done | Claude | 11 TRACK1 docs + 102 decisions + Groww wipe |
| 1 — Verification Scaffolding | ☐ Not started | 2-3 weeks | Claude | 9-layer empty test pyramid + 26 invariant stubs + brutex doctor + bootstrap hook |
| 2 — Dhan Adapter + Vocabulary | ⏸️ Blocked | 3-4 weeks | Claude (needs token) | Working Dhan ingest + 52-condition atoms + pattern generator |
| 2.5 — Bayesian Early-Stop | ☐ Not started | 1 week | Claude | Stage 2 trial reduction 61% |
| 3 — Brute-Force Engine | ☐ Not started | 2-3 weeks | Claude | Stage 1A/1B/2/3 working + first canary throughput measurement |
| 3.5 — Metal GPU Offload | ☐ Not started | 3-4 weeks | Claude | 2-5× speedup, 33B sweep in 1 overnight |
| 4 — Filter Cascade + Ship | ☐ Not started | 1-2 weeks | Claude | DSR + CPCV + WFO + Lockbox + FIRST PRODUCTION SWEEP + top 3 candidates |

**Total: 12-17 weeks** of Claude work + ~3-5 hours of user sign-offs.

## What user needs to provide

| Item | When | Effort | Status |
|---|---|---|---|
| Dhan access token (for `~/.brutex/dhan/access_token.json`) | Before Phase 2 start | 1 min | ☐ TBD |
| `/Volumes/QuantData` SSD mounted | Before Phase 2 start | 1 min | ☐ TBD |
| Sign-off per phase end | At each phase boundary | 5-30 min | recurring |
| Business judgment on top-3 strategies | At Phase 4 ship | 30-60 min | ☐ TBD |

## Decisions locked (102 total)

See `../MIGRATION.md` for the full ledger. Phase-grouped:

| Phase | Decision range | Count |
|---|---|---|
| 0 (Groww→Dhan pivot, Phase 0-4 plan) | #1-#70 | 70 |
| 0 (post-merge corrections, design refinements) | #71-#80 | 10 |
| Post-#14 (runtime + Phase 2.5/3.5 designs) | #81-#102 | 22 |
| **TOTAL** | **#1-#102** | **102** |

## Verification stack (must remain green throughout)

| Layer | Status target | Owner |
|---|---|---|
| ruff check + ruff format --check | clean | every PR |
| mypy --strict | clean (55+ files) | every PR |
| 22 CI gates | all green | every PR |
| 26 property invariants (P-01 to P-26) | all pass with 500 examples | per phase end |
| 4 oracle tests | all green | per phase end |
| 4-agent hostile review | required at each phase end | per phase end |
| brutex doctor C1-C11 | all GREEN (or SKIP) | every workday |

## Risk register (cross-phase)

| Risk | Phase impact | Mitigation |
|---|---|---|
| Throughput < 80K t/s on real M4 Pro | 3+ | Contingency SLA matrix in spec §2.1 self-amends |
| GPU kernel produces wrong result | 3.5 | Bit-exact parity test + audit-replay oracle blocks merge |
| Stage 2.5 kills a true winner | 2.5 | A/B oracle test; rolling 30d miss-rate budget ≤ 5% |
| Dhan API changes / breaks | 2+ | Hardcoded 4 securityIds + quarterly smoke test |
| User's M4 Pro unavailable | 2+ | No fallback (NO AWS by user decision #99) |
| Lake mount goes missing | 2+ | brutex doctor C1-C8 catches; halt + alert |

## Current work front

**☐ Phase 1 — Verification Scaffolding.** First Claude session to pick this up reads `phase_1_verification_scaffolding.md` and starts ticking boxes.

## After Phase 0 (already done) recap

| PR | Merged | Highlights |
|---|---|---|
| #12 | ✅ | Groww wipe + 7 TRACK1 docs + decisions #1-#70 |
| #13 | ✅ | VIX reference-only + Dhan rate limit + 4 securityIds + auth design + decisions #71-#87 |
| #14 | ✅ | Runtime atlas + day/night envelope + Phase 2.5/3.5 designs + decisions #88-#102 |

Main currently at: latest commit per `git log main --oneline -1`.
