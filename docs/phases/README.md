# BruteX Track 1 — Phase Checklists

> **Operating manual for any Claude Code session or operator working on
> BruteX Track 1.** Designed for the "pick up where the last session
> left off" pattern.

## How to use this directory

| You are... | Read this |
|---|---|
| A brand-new Claude Code session | `RESUME_PROMPT.md` — copy/paste it as your first prompt |
| The operator (SJParthi) checking status | `00_MASTER_CHECKLIST.md` — single-glance progress dashboard |
| Working on a specific phase | `phase_N_*.md` — the per-phase checklist with all sub-tasks |
| Wondering "what's done?" | `00_MASTER_CHECKLIST.md` shows ✅/🔄/☐/⏸️ per phase |
| Wondering "what's next?" | First phase doc with unchecked boxes is next |

## File index

| File | Purpose | Status |
|---|---|---|
| `README.md` | This file (index) | — |
| `00_MASTER_CHECKLIST.md` | One-glance progress dashboard | living |
| `RESUME_PROMPT.md` | Exact prompt for a new Claude session to resume work | living |
| `phase_0_documentation.md` | Phase 0 — Docs + wipe + locked decisions | ✅ DONE |
| `phase_1_verification_scaffolding.md` | Phase 1 — 9-layer test pyramid + invariants | ☐ Not started |
| `phase_2_groww_adapter_vocabulary.md` | Phase 2 — Groww adapter + 52-vocabulary | ☐ Not started |
| `phase_2_5_bayesian_early_stop.md` | Phase 2.5 — Stage 2.5 early-stop | ☐ Not started |
| `phase_3_brute_force_engine.md` | Phase 3 — 5-stage funnel engine | ☐ Not started |
| `phase_3_5_metal_gpu_offload.md` | Phase 3.5 — Apple Metal compute | ☐ Not started |
| `phase_4_filter_cascade_ship.md` | Phase 4 — DSR/CPCV/WFO/Lockbox + first sweep | ☐ Not started |

## Convention: checkbox status

| Symbol | Meaning |
|---|---|
| `- [ ]` | Not started |
| `- [x]` | Done |
| `🔄` (in headings) | Currently in progress |
| `✅` (in headings) | All sub-items checked |
| `⏸️` (in headings) | Blocked on external dependency (e.g., user must provide Groww token) |

## Convention: commit linking

Every checked box should reference the commit SHA where it was completed:
```
- [x] Build watermark table (commit: 3d47fe0)
```

This lets anyone trace from a checkbox to the exact code/doc change.

## Convention: phase entry / exit

Each phase doc has:
1. **Status** at top — Not started / In progress / Done / Blocked
2. **Effort** estimate
3. **Owner** — Claude (most work) or User (sign-offs, token, etc.)
4. **Prerequisites** — what must be done before this phase
5. **Checklist** — broken down by week or sub-task
6. **Verification gates** — tests + 4-agent hostile review
7. **Exit criteria** — clear bullet list of "done"
8. **Decisions locked** — links to MIGRATION.md decision rows
9. **Files to create/modify** — exact paths
10. **Risk register** — what can go wrong + mitigation

## Source-of-truth docs (read these first)

Every phase doc references these. Read them before starting any phase:

| Doc | Purpose |
|---|---|
| `../../CLAUDE.md` | Project rules + Groww facts + 4-agent review pattern |
| `../TRACK1_BACKTEST_SPEC.md` | Master spec (signal-only, 21 TFs, 4 inst incl. VIX ref-only) |
| `../TRACK1_DATA_INGEST_DESIGN.md` | Ingest pipeline + state machine + auth + error map |
| `../TRACK1_VOCABULARY.md` | 52 atomic conditions, K=2..5 ANDed |
| `../TRACK1_VERIFICATION_PYRAMID.md` | 9 layers + 26 property invariants + 4 oracle tests |
| `../TRACK1_PHASE_PLAN.md` | 8-12 week build plan (master) |
| `../TRACK1_PERFORMANCE_BUDGET.md` | Honest SLA + day/night envelope |
| `../TRACK1_MICROSTRUCTURE.md` | Pre-open, Muhurat, halts, rebalance |
| `../TRACK1_RUNTIME_ARCHITECTURE.md` | Operations atlas + bootstrap + institutional comparison |
| `../TRACK1_OPTIMIZATIONS.md` | Phase 2.5 + Phase 3.5 designs |
| `../MIGRATION.md` | 102-decision ledger (single source of truth for design decisions) |
