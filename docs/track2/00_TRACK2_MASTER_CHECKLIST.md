# BruteX Track 2 — Master Progress Checklist

> Single-glance dashboard for Track 2 (Options Track). Track 1 master
> dashboard lives at `../phases/00_MASTER_CHECKLIST.md`.

_Last updated: 2026-05-15._

## Track 2 status at a glance

| Phase | Status | Effort | Owner | Notes |
|---|---|---|---|---|
| **Phase 0** Documentation | 🔄 in progress | 2-3 days | Claude | This PR (`claude/track2-options-design-VXT2A`) |
| **Phase 1** Test scaffolding | ☐ not started | 1 week | Claude | Mirrors Track 1 Phase 1 — depends on Phase 0 merge |
| **Phase 2** Dhan options adapter | ⏸️ blocked | 2 weeks | Claude | Blocked on Track 1 Phase 2 complete (data lake ingest live) |
| **Phase 3** Backtest engine | ☐ not started | 2-3 weeks | Claude | Depends on Phase 2 + Track 1 Phase 3 engine |
| **Phase 4** First sweep | ☐ not started | 1 week | Claude + User | Run on a Track 1 winning signal; user signs off top-3 |

Total: 6-8 weeks of build work, starting **after Track 1 Phase 2 ships**.

## Hard prerequisites (must be GREEN before unblocking)

| Prereq | Source-of-truth | Why |
|---|---|---|
| Track 1 Phase 0 complete | `docs/phases/00_MASTER_CHECKLIST.md` row "Phase 0" | We mirror their patterns |
| Track 1 Phase 2 complete (Dhan adapter shipped) | `docs/phases/phase_2_dhan_adapter_vocabulary.md` | Reuses auth + rate-limit + audit infra |
| Track 1 rolling window working (5y ingested + daily incremental) | `docs/TRACK1_DATA_INGEST_DESIGN.md` §1-§7 | Options ingest reads spot underlying as a covariate |
| Dhan access token issued + renew loop green | `docs/TRACK1_DATA_INGEST_DESIGN.md` §5A | Same token works for `/charts/rollingoption` |
| 4-agent hostile review on Track 1 Phase 2 PASS | `docs/runbooks/4-agent-review.md` | Pattern must be proven before Track 2 reuses it |

If ANY of the above is RED, Track 2 implementation stops at the
phase-2 entry gate. Phase 0 (this design) is the only thing that
proceeds in parallel.

## What's locked vs in-progress

| Decision | Status | Where |
|---|---|---|
| 3 underlyings (NIFTY 50, BANKNIFTY, SENSEX) — NO India VIX options | 🔒 locked | This dir's `TRACK2_OPTIONS_SPEC.md` §2 |
| `/v2/charts/rollingoption` endpoint (ATM-relative, not per-contract) | 🔒 locked | `TRACK2_OPTIONS_SPEC.md` §3 |
| 1-minute timeframe ONLY (no resampling) | 🔒 locked | `TRACK2_OPTIONS_SPEC.md` §4 |
| ATM ± 10 strike radius (Dhan-enforced) | 🔒 locked | `TRACK2_OPTIONS_SPEC.md` §5 |
| Current expiry only at sweep time (week + month per underlying) | 🔒 locked | `TRACK2_OPTIONS_SPEC.md` §6 |
| **6 backfill streams** with per-regime date gating (NIFTY-WEEK/MONTH, BANKNIFTY-WEEK [2021→Nov 2024]/BANKNIFTY-MONTH, SENSEX-WEEK/MONTH [2023-05-15 onwards]) | 🔒 locked | `TRACK2_OPTIONS_SPEC.md` §2 |
| IV from Dhan; delta/gamma/theta/vega computed by BruteX (Black-Scholes) | 🔒 locked | `TRACK2_OPTIONS_SPEC.md` §8 |
| Storage layout: `options/<exch>/<segment>/<underlying>/<cycle>/ATM<offset>/<type>/<year>/<month>.parquet` | 🔒 locked | `TRACK2_DATA_INGEST_DESIGN.md` §3 |
| Money: int64 paisa everywhere (same as Track 1) | 🔒 locked | `TRACK2_OPTIONS_SPEC.md` §11 |
| Brokerage/STT/STO model | ☐ TBD | `phases/phase_3_backtest_engine.md` Week 1 task |
| Slippage model | ☐ TBD | `phases/phase_3_backtest_engine.md` Week 2 task |
| Execution clock / event scheduler | ☐ TBD | `phases/phase_3_backtest_engine.md` Week 1 task |
| Greeks freshness policy (1m bars vs 15s tick) | ☐ TBD | `phases/phase_3_backtest_engine.md` Week 2 task |

## Phase entry / exit summary (what each phase delivers)

### Phase 0 — Documentation (current)
- **Goal:** Lock the API surface + storage + state machine + cost model in writing.
- **Exit:** All `TRACK2_*` docs merged to main. 4-agent hostile review passed. MIGRATION.md decisions #200-#220.
- **Does NOT include:** Any production code. Only docs + checklists + skeleton phase plans.

### Phase 1 — Verification scaffolding
- **Goal:** Add the test pyramid for Track 2 — extends Track 1's 9-layer pyramid.
- **Exit:** 11 new property invariants (options-specific) + 2 new oracle tests pass against a fake adapter.

### Phase 2 — Dhan options adapter (BLOCKED on Track 1 Phase 2)
- **Goal:** Real `/v2/charts/rollingoption` calls, 4-stream backfill, daily incremental.
- **Exit:** 5y backfill green; daily incremental + bar_hash audit chain unbroken; `brutex doctor` C12 (options-fresh) added.

### Phase 3 — Options backtest engine
- **Goal:** Translate a Track 1 winning signal into option entries + simulate fills.
- **Exit:** Brokerage / STT / STO / slippage model locked + measured; replay oracle byte-identical.

### Phase 4 — First sweep + ship
- **Goal:** Run sweep on a Track 1 top-3 signal across the options grid; user picks top-3 strategies.
- **Exit:** `results.parquet` written; DSR / CPCV / WFO / Lockbox gates applied; lockbox PDF signed.

## Source-of-truth docs (read these first)

| Doc | Purpose |
|---|---|
| `../../CLAUDE.md` | Project rules + Dhan facts |
| `../TRACK1_BACKTEST_SPEC.md` | Track 1 master spec — Track 2 derives from it |
| `../TRACK1_DATA_INGEST_DESIGN.md` | Pipeline + state-machine pattern we mirror |
| `../TRACK1_VERIFICATION_PYRAMID.md` | 9-layer pyramid extends to options |
| `../TRACK1_PHASE_PLAN.md` | Same methodology |
| `../TRACK1_RUNTIME_ARCHITECTURE.md` | Day/night envelope; `brutex doctor` C12 will land here |
| `../MIGRATION.md` | Decision ledger; Track 2 adds rows #200+ |
| `TRACK2_OPTIONS_SPEC.md` | Master spec (API, universe, scope) — THIS DIR |
| `TRACK2_DATA_INGEST_DESIGN.md` | Storage + state machine + pseudocode — THIS DIR |

## Convention

Same as Track 1:
- `- [ ]` not started
- `- [x] (commit: abc1234)` done with commit SHA
- ✅ all sub-items done
- 🔄 currently in progress
- ⏸️ blocked on external dependency
