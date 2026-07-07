# Track 1 — 8-12 Week Phase Plan

> **Status:** Locked 2026-05-15. Companion to `TRACK1_BACKTEST_SPEC.md §19`.

> **✅ VENDOR RE-RECONCILIATION (DEC-DHAN-GROWW-REMIGRATE-001 / ADR-0009,
> 2026-06-12) — SUPERSEDES the 2026-06-06 banner below.** The live vendor is
> **Groww** again (the `growwapi` SDK + TOTP auth). The short-lived Dhan revert
> (DEC-GROWW-DHAN-REVERT-001 / ADR-0002) was itself REVERTED: the Track-1
> backtesting data source was fully re-migrated back to the Groww Trade API and
> the `brutex/adapters/dhan/` package was DELETED (a `no-dhan-guard.sh` CI gate
> now enforces zero-`dhan` on the live surface). **So read every forward-looking
> "Groww" in this plan as Groww (NOT as "Dhan").** The 2026-06-06 banner
> immediately below is retained as immutable history of the abandoned revert —
> do NOT action its "read Groww as Dhan" instruction. Authoritative, repo-sourced
> vendor facts live in CLAUDE.md §6 + `docs/adr/0009-*`.

> **⚠️ VENDOR RECONCILIATION (DEC-DHAN-DOC-RECONCILE-001, 2026-06-06):** this
> plan was written 2026-05-15 and revised during the short-lived Dhan→Groww
> pivot, so its forward-looking phases say "Groww". **The live vendor is Dhan
> Data API v2** — the Groww pivot was REVERTED per DEC-GROWW-DHAN-REVERT-001
> (`docs/adr/0002-groww-to-dhan-revert.md`, 2026-05-31; CLAUDE.md §1/§6). Read
> every forward-looking "Groww" below as **Dhan** (adapter already shipped at
> `brutex/adapters/dhan/`). HISTORICAL rows that record what was done at the
> time (Phase 0 deliverables, the 2026-05-15 review findings) are left intact as
> an accurate record. This installment reconciles Phase 0b + Phase 2 + the Risk
> Register on this file; the prior installment (ledger #378) covered the
> operator-facing docs (README / bootstrap / ENV_VARS / runday).

This document specifies the 5-phase build plan from this week (Phase 0
documentation) through to the first production sweep.

---

## Phase 0 — Documentation & Resolution (this week)

**Goal:** Lock all design decisions in markdown before any code lands.

| Deliverable | Owner | Status |
|---|---|---|
| `CLAUDE.md` rewritten for Groww + Track 1 | this PR | ✅ |
| `docs/REQUIREMENTS.md` retired to stub | this PR | ✅ |
| `docs/TRACK1_BACKTEST_SPEC.md` rewritten | this PR | ✅ |
| `docs/TRACK1_VERIFICATION_PYRAMID.md` written | this PR | ✅ |
| `docs/TRACK1_VOCABULARY.md` written | this PR | ✅ |
| `docs/TRACK1_PHASE_PLAN.md` written | this PR | ✅ (this file) |
| `docs/TRACK1_PERFORMANCE_BUDGET.md` written | this PR | (next) |
| `docs/TRACK1_MICROSTRUCTURE.md` written | this PR | (next) |
| `docs/MIGRATION.md` written | this PR | (next) |
| broker adapter (deleted) deleted | this PR | ✅ |
| Groww test files deleted | this PR | ✅ |
| `docs/broker-sdk-docs/` deleted | this PR | ✅ |
| `pyproject.toml` the prior broker dep removed | this PR | ✅ |
| `brutex/cli.py` stubbed to minimal Track 1 | this PR | ✅ |
| `brutex/universe.py` rewritten for spot indexes | this PR | ✅ |
| PR #12 description updated | TBD | ⏳ |

**Exit criteria:** PR #12 merged. CI green. User approves Phase 1 start.

---

## Phase 0b — Empirical Probe (HARD GATE before Phase 1 adapter code)

> **🛑 SUPERSEDED — MOOT post-revert (DEC-DHAN-DOC-RECONCILE-001, 2026-06-06).**
> This entire Phase 0b was a **Groww-vendor** empirical-probe HARD GATE. It was
> created during the Dhan→Groww pivot (DEC-VENDOR-PIVOT-001) and is **no longer
> in force**: the Groww pivot was REVERTED to **Dhan Data API v2** per
> DEC-GROWW-DHAN-REVERT-001 (`docs/adr/0002-groww-to-dhan-revert.md`), so the
> Groww subscription, the `BRUTEX_GROWW_*` env vars, and the four
> `docs/groww/probe/` transcripts below are **NOT prerequisites for anything**.
> The live data path is the shipped `brutex/adapters/dhan/` adapter (REST, no
> SDK; CLAUDE.md §6). The text is retained verbatim below only as a historical
> record of the abandoned Groww gate — do NOT action it.

> **Added 2026-05-26** post-merge of `DEC-VENDOR-PIVOT-001`
> (`MIGRATION.md` entry #246). Inserted between the original Phase 0
> doc-lock and Phase 1 scaffolding because the 4-agent hostile review
> on the Dhan → Groww pivot accepted 4 P0 risks **on condition** that
> empirical evidence land before any Groww adapter code does. The
> probe script merged on PR #140 (commit `1058b57`) is the evidence
> collector; this section is its checklist.

**Goal:** Collect first-hand empirical evidence on Groww's data
surface before any `brutex/adapters/groww/` code lands. Eliminates
the 4 accepted-risk items in `docs/adr/0001-groww-vendor-pivot.md §3.1`.

| Deliverable | Owner | Status |
|---|---|---|
| `scripts/groww_probe.py` (4 sub-probes, ZERO `brutex/` imports) | PR #140 | ✅ merged 2026-05-26 |
| `scripts/groww_universe_discovery.py` (Phase 0c companion) | PR #141 | ✅ merged 2026-05-26 |
| Groww Trade API subscription (₹499/mo) | operator | ⏳ pending |
| `BRUTEX_GROWW_API_KEY` + `BRUTEX_GROWW_TOTP_SECRET` env vars (PROBE-LOCAL to the standalone `scripts/groww_probe.py` ONLY — production credentials are AWS-SSM-only, never env vars; DEC-AWS-ONLY-CREDS-001, MIGRATION.md #1065) | operator | ⏳ pending |
| Probe 1 transcript — `docs/groww/probe/01_vix_csv_grep.json` | operator-run | ⏳ pending (**HARD GATE**) |
| Probe 2 transcript — `docs/groww/probe/02_vix_1m_candle.json` | operator-run | ⏳ pending |
| Probe 3 transcript — `docs/groww/probe/03_rate_limit_probe.json` | operator-run | ⏳ pending |
| Probe 4 transcript — `docs/groww/probe/04_nifty_1m_dayrun.json` | operator-run | ⏳ pending |
| Universe inventory transcripts — `docs/groww/universe/0[1-5]_*.json` | operator-run | ⏳ pending |

**HARD GATE on Probe 1 (INDIA VIX presence):** if VIX absent from
the Groww instruments CSV (exit code 5), the operator MUST write
`docs/adr/0002-groww-vendor-pivot-RESCIND.md` and the rest of Phase 0b
+ all of Phase 1 are blocked until alternate broker decision lands.
See `docs/TRACK1_GROWW_ADAPTER.md §3` for the no-VIX contingency tree.

**Exit criteria:** all 4 probe transcripts committed to
`docs/groww/probe/`, all 5 universe inventories committed to
`docs/groww/universe/`, Probe 1 confirms VIX present, Probe 3
confirms rate-limit bucket allows ≥ 4 r/s sustained. Then Phase 1
adapter code may land.

---

## Phase 1 — Verification Scaffolding (2-3 weeks)

**Goal:** Build the 9-layer test pyramid as empty shells so every
Phase 2+ code change is verified from day one.

### Week 1

| Deliverable | Notes |
|---|---|
| `tests/property/test_invariants_catalog.py` — canonical catalog of all 24 invariants (DEC-PHASE1-INVARIANT-CATALOG-001, MIGRATION entry #250) | ✅ landed 2026-05-26 — 24 `test_p_NN_*` functions, each `pytest.skip(...)` with either a pointer to the implementing file or a "Phase N pending" tag. Today: ✅ P-01, P-02, P-05, P-16, P-17 (5 of 24 implemented in sibling files); 📋 P-03..P-04, P-06..P-15, P-18..P-24 awaiting Phase 1.5/2/3 work. |
| `tests/golden/fixtures/` directory created with placeholder fixtures | ✅ landed 2026-05-26 (DEC-PHASE1-SMOKE-AND-SKELETONS-001, MIGRATION entry #251) — placeholder README documents the 9 planned corpora; real fixtures land in Phase 2 ingest pipeline |
| `tests/integration/test_replay_audit_log.py` skeleton | ✅ landed 2026-05-26 (DEC-PHASE1-SMOKE-AND-SKELETONS-001, MIGRATION entry #251) — 4 `pytest.skip` stubs for the audit-log → Parquet roundtrip (P-20); fills in alongside the Parquet writer in Phase 2 |
| `tests/integration/test_python_rust_parity.py` skeleton | ✅ landed 2026-05-26 (DEC-PHASE1-SMOKE-AND-SKELETONS-001, MIGRATION entry #251) — 5 `pytest.skip` stubs for the 5 hot-path operations; fills in alongside the Rust ↔ Python parity in Phase 2/3 |
| `tests/bench/test_hotpath_o1.py` skeleton with 6 hot-path stubs | already landed pre-2026-05-26 — see `tests/bench/test_store_o1.py` + `test_rate_limiter_o1.py` + `test_throughput_canary.py` + `test_throughput_absolute.py` + `test_full_sweep_extrapolation.py` |
| `tests/stress/test_24h_sustained.py` skeleton | already landed as `tests/stress/test_audit_chain_sustained.py` |
| `tests/smoke/test_pre_push.py` skeleton | ✅ landed 2026-05-26 (DEC-PHASE1-SMOKE-AND-SKELETONS-001, MIGRATION entry #251) — working smoke test (not a skeleton); 5 test classes, ~30s runtime; catches lint/format/import/conventional-commit/docs regressions |

### Week 2

| Deliverable | Notes |
|---|---|
| `cargo-fuzz` target list under `rust/fno-math/fuzz/` for 7 targets | ✅ landed 2026-05-26 (DEC-PHASE1-WEEK2-SCAFFOLDING-001, MIGRATION entry #252) — README directory marker documents the 7 planned targets; Phase 2 runs `cargo fuzz init` + per-target `cargo fuzz add` as each hot-path implementation lands |
| `cargo-mutants` config (`.cargo/mutants.toml`) | ✅ landed 2026-05-26 (DEC-PHASE1-WEEK2-SCAFFOLDING-001, MIGRATION entry #252) — walks `rust/fno-math/src/math/` + `src/compat/`; excludes generated PyO3 glue + tests + benches; used by existing `.github/workflows/mutation.yml` |
| `tests/golden/condition_truth_table.csv` skeleton with 5 example rows | ✅ landed 2026-05-26 (DEC-PHASE1-WEEK2-SCAFFOLDING-001, MIGRATION entry #252) — 5-row skeleton covers `bar_bullish` / `bar_bearish` / `close_above_ema20` / `close_below_ema20` / `early_morning`; Phase 2 expands to ≥ 200 rows |
| `.github/workflows/nightly.yml` new — stress + fuzz | ✅ already covered by `stress-nightly.yml` (DEC-STRESS-001 entry #208) + `redteam-nightly.yml` + `dep-freshness-nightly.yml` — three split workflows beats one mega-workflow |
| `.github/workflows/weekly.yml` new — mutation testing | ✅ already covered by `mutation.yml` (canonical weekly job per CLAUDE.md §4) |
| Coverage gate raised in `pyproject.toml`: `fail_under = 100` | 📋 ROADMAP — coverage is gated per-module via the **51 strict-100% module gates** today (DEC-COV-001 + DEC-COV-RATCHET-001 + DEC-COV-BACKFILL-001/002/003/004 entries #210-#232 + #256-#260; closed per **DEC-COV-BACKFILL-LIMIT-001 entry #261** with operator-accepted limit on the 8 remaining `brutex/orchestrator/` modules blocked by `fastbloom_rs` PyO3 ABI3 single-init — all 8 covered at 100% via global `--cov=brutex` run); global `fail_under = 100` requires the remaining ~20 modules to ratchet up first (3 ROADMAP closure paths in entry #261: vendor swap, per-test-subprocess CI harness, or lazy-import refactor) |

### Week 3

| Deliverable | Notes |
|---|---|
| `4-agent hostile review pattern` documented as a runbook | ✅ landed 2026-05-27 (DEC-PHASE1-WEEK3-RUNBOOK-001, MIGRATION entry #253) — full runbook at `docs/runbooks/4-agent-review.md` with copy-paste prompts for all 4 agents (Audit/Architect/Hostile/Reality), trigger matrix (4-agent for major / 2-agent for compliance / 1-agent for refactor), reconciliation steps, the 2026-05-15 case study, and the operator sign-off gate |
| First end-to-end "empty pyramid" CI run | ✅ landed 2026-05-27 (DEC-PHASE1-WEEK3-RUNBOOK-001, MIGRATION entry #253) — baseline documented in `docs/PHASE1_EMPTY_PYRAMID_BASELINE.md` with per-layer counts (all 9 layers ≥ 1 file), 24-invariant catalog snapshot (5 IMPLEMENTED + 19 ROADMAP), CI workflow inventory, reproducible verification commands, honest envelope, and 10-item Phase 1 sign-off checklist |
| Phase 1 sign-off | ✅ closed by supersession 2026-07-06 (DEC-PHASE1-SIGNOFF-RECONCILE-001, MIGRATION.md #1542) — 11-item checklist (the original "10-item" count was defective) reconciled in `docs/PHASE1_EMPTY_PYRAMID_BASELINE.md §7` |

**Exit criteria:** All 9 test layers have at least 1 file each. CI is
green with all stubs. The verification surface is mapped.

---

## Phase 2 — Dhan Adapter + Spot-Index Ingest + Vocabulary (3-4 weeks)

> **VENDOR NOTE (DEC-GROWW-DHAN-REVERT-001, ADR-0002, 2026-05-31):** this phase
> plan predates the Groww→Dhan revert; read every "Groww" below as **Dhan Data
> API v2**. The live adapter is `brutex/adapters/dhan/` (CLAUDE.md §1/§6). A full
> Groww→Dhan text pass over this doc is a tracked follow-up (DEC-DHAN-DOC-RECONCILE-001).

> **AMENDED 2026-05-22 per DEC-P0-063 (MIGRATION.md entry #200):** the "Spot-Index Ingest" scope below describes the **core 4-instrument surface** (NIFTY 50 / BANKNIFTY / SENSEX / INDIA VIX). DEC-P0-063 expanded the LAKE to additionally cover **200 NSE F&O eligible single stocks (spot + options across all 4 Groww `expiryCodes`)**. The 200 stocks join INDIAVIX in REFERENCE_ONLY (BRUTE-FORCE ENGINE excluded — engine stays at the 3 brute-force indexes). Phase 2's Week 4-7 deliverables use the same Groww adapter primitives for both surfaces (per COMMON · DYNAMIC · SCALABLE rules in CLAUDE.md §11); no Phase 2 work needs to be re-scoped — the deliverable table below is the 4-instrument core, and the 200-stock universe joins on the same code path. See CLAUDE.md §3 + `TRACK1_BACKTEST_SPEC.md §"Scope Lock"` (per DEC-SPEC-CONSISTENCY-002, entry #264) + `TRACK1_DATA_INGEST_DESIGN.md §1` (per DEC-SPEC-CONSISTENCY-003, entry #265) + `TRACK1_RUNTIME_ARCHITECTURE.md §2` (per DEC-SPEC-CONSISTENCY-004, entry #266) for the full locked wording.

**Goal:** Stand up the Dhan data path, the 52-condition vocabulary,
and the indicator precompute pipeline.

### Week 4

> **Reconciled to Dhan (DEC-DHAN-DOC-RECONCILE-001):** the adapter package below
> has **already shipped** as `brutex/adapters/dhan/` (REST, no SDK; CLAUDE.md §6).
> The original "Groww" module names are corrected to their real Dhan equivalents.

| Deliverable | Notes |
|---|---|
| `brutex/adapters/dhan/__init__.py` | ✅ shipped — package |
| `brutex/adapters/dhan/adapter.py` + `auth.py` | ✅ shipped — HTTP client + token auth |
| `brutex/adapters/dhan/rate_control.py` | ✅ shipped — adaptive [3..5] r/s token bucket (CLAUDE.md §6) |
| `brutex/adapters/dhan/instruments.py` | ✅ shipped — master CSV fetcher + `SECURITY_ID` resolver |
| `brutex/adapters/dhan/adapter.py` — `POST /v2/charts/intraday` (1m) | ✅ shipped wrapper |
| `brutex/adapters/dhan/adapter.py` — `POST /v2/charts/historical` (1d) | ✅ shipped wrapper |
| Property tests P-18 (rate limiter) green | |
| First live call to Dhan verifies rate limit and `SECURITY_ID` map | |

### Week 5

| Deliverable | Notes |
|---|---|
| `brutex/adapters/dhan/parser.py` — Dhan columnar candle response (✅ shipped) | Parses parallel arrays `{open[],high[],low[],close[],volume[],timestamp[],open_interest[]}` (CLAUDE.md §6) |
| 1m → 14 TFs resampler verified deterministic (P-08) | |
| Indicators 1-9 implemented in Rust (`rust/fno-math/src/indicators/`) | Extends existing crate |
| Indicator cache key (`sha256(data_version ‖ indicator_version)`) | |
| Golden fixtures #1-5 (Diwali, Holi, election day, Ram Navami, normal day) populated | |

### Week 6

| Deliverable | Notes |
|---|---|
| `brutex/strategies/vocabulary/atoms.rs` (Rust) — 52 condition evaluators | |
| `tests/golden/condition_truth_table.csv` fully populated (≥200 rows) | |
| Property tests P-11 (mask commutativity), P-14 (pivots pure) green | |
| Conflict graph (`brutex/strategies/vocabulary/conflict_graph.py`) implemented | |
| Pattern generator (`brutex/strategies/pattern_generator.py`) produces ~1.2-1.5M survivors | |

### Week 7

| Deliverable | Notes |
|---|---|
| Phase 2 hostile review (4-agent pass) | Catches anything missed |
| Phase 2 sign-off | User reviews vocabulary + adapter |

**Exit criteria:** Dhan ingest of 5 years × 4 instruments (NIFTY 50, BANKNIFTY, SENSEX, INDIA VIX) × 21 TFs
working. 9 indicators precomputed. 52 atomic conditions evaluable. The
pattern generator emits the pruned set.

---

## Phase 3 — Brute-Force Engine (2-3 weeks)

**Goal:** Build the 4-stage search pipeline (Stage 1A → 1B → 2 → 3)
hitting the throughput SLA.

### Week 8

| Deliverable | Notes |
|---|---|
| `rust/fno-math/src/engine/runner.rs` — single-trial walker | Per-bar, per-mode-A, per-mode-B trial logic |
| `rust/fno-math/src/engine/parallel.rs` — Rayon work-stealing | |
| Memory-tiled bar buffer | Hold bar mask in registers; sweep strategies |
| First micro-benchmark — confirm 80-150K trials/sec achievable | Per Agent 4 reality check |

### Week 9

| Deliverable | Notes |
|---|---|
| Stage 1A implementation (pattern discovery, NIFTY 15m) | ~90 sec wall |
| Stage 1B implementation (expansion to 63 inst-TF = 3 brute-force inst × 21 TFs) | ~56 sec wall |
| Stage 2 implementation (full Mode A + Mode B sweeps) | The bulk |
| `brutex/strategies/result_writer.py` — Parquet streaming + threshold filter | |
| Dedup unified across stages (Bloom + LMDB) | |
| Property tests P-01, P-02, P-05, P-06, P-19, P-20 green | |
| `tests/integration/test_replay_audit_log.py` green — first reproducibility win | |
| `tests/integration/test_python_rust_parity.py` green — correctness oracle | |
| All 6 O(1) hot-path benchmarks ratio ≤ 1.1 | |

### Week 10

| Deliverable | Notes |
|---|---|
| Stage 3 implementation (CMA-ES via Optuna) | Top-100 fine refinement |
| First micro-sweep (1 day × 3 inst × 100 patterns × full params) | Smoke test the pipeline end-to-end |
| Throughput sustained at 80-150K trials/sec confirmed on M4 Pro | |
| Phase 3 hostile review | |
| Phase 3 sign-off | |

**Exit criteria:** Stages 1A-3 produce a results.parquet for a small
test sweep. SHA256 reproducibility confirmed. Python-Rust parity holds.
O(1) bench passes.

---

## Phase 4 — Filter Cascade + First Production Sweep (1-2 weeks)

**Goal:** Stage 4 filter cascade + first 2-4 day production sweep.

### Week 11

| Deliverable | Notes |
|---|---|
| Stage 4.1 — DSR with effective-N (cluster-adjusted) | Bailey-LdP correlation clustering |
| Stage 4.2 — CPCV (Combinatorial Purged Cross-Validation) | |
| Stage 4.3 — WFO (Walk-Forward Optimization) | |
| Stage 4.4 — Lockbox replay (Dec 2025 - May 2026 held-out window) | |
| All filter property tests green | |

### Week 12

| Deliverable | Notes |
|---|---|
| First production sweep (full 5-year × 3 inst × 21 TF × ~33B trials) | 2-4 day wall on M4 Pro |
| Top 3 candidates produced + reviewed | |
| Final Phase 4 hostile review | |
| Track 1 SHIP signoff | |

**Exit criteria:** First production sweep produces 3 ranked candidate
strategies with full audit trail. Lockbox replay confirms out-of-sample
Sharpe within 30% of in-sample. Then Phase 2 (options translation)
begins.

---

## The 4-Agent Hostile Review Pattern

Every phase ends with a 4-agent hostile-review gate. Pattern:

```
1. Audit agent (Explore)       → repo ground-truth survey
2. Architecture agent (Plan)   → forward design check
3. Hostile agent (general)     → factual hallucination hunt + logic flaw
4. Reality-check agent (general) → public-benchmark / hardware-budget sanity
```

This pattern caught the major issues on 2026-05-15 (the broker-vs-Groww
adapter mismatch, 10x throughput claim, spot-index tradability,
DSR-with-N=33B, basis-point vs absolute-point tolerances, native Groww
timeframe count, securityId hardcoding). It is a non-negotiable gate.

See `docs/runbooks/4-agent-review.md` (Phase 1 deliverable) for the
runbook with exact prompts.

---

## Risk Register

| Risk | Mitigation |
|---|---|
| Dhan Data API rate-limit lower than expected | Documented 5 r/s + 100,000/day; adapter runs an adaptive **[3..5] r/s** token bucket that steps down on 429 (`brutex/adapters/dhan/rate_control.py`; CLAUDE.md §6). Never raised without explicit user approval (Golden Rule #6) |
| Throughput below 80K trials/sec sustained | Phase 3 micro-benchmark gates this; if fails, escalate to larger hardware before Phase 4 |
| DSR effective-N still rejects everything | Phase 4 dry-run on Phase 3 outputs; if rejects everything, revisit cluster-pruning aggressiveness |
| Indian market microstructure surprise | Phase 2 hostile review re-checks; calendar tests P-22, P-23 guard the canary |
| Lockbox out-of-sample Sharpe < 30% of in-sample | Track 1 ships ONLY 1-2 candidates instead of 3 if quality bar isn't met; honest disclosure |

---

## Honest SLA Restatement (per CLAUDE.md §10)

- **8-12 weeks total build** on M4 Pro reference hardware
- **2-4 days per production sweep** on M4 Pro at 80-150K trials/sec
  sustained
- **Faster iteration possible** on EPYC/Threadripper 64-128 core
  (12-24 hours per sweep, 6-8 weeks build) at user's discretion
- No "millions in minutes" claim without naming hardware AND citing
  public benchmarks. The 4-agent review on 2026-05-15 caught the prior
  10x optimism; this plan is built around that lesson.
