# Track 1 — 8-12 Week Phase Plan

> **Status:** Locked 2026-05-15. Companion to `TRACK1_BACKTEST_SPEC.md §19`.

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
| `BRUTEX_GROWW_API_KEY` + `BRUTEX_GROWW_TOTP_SECRET` env vars | operator | ⏳ pending |
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
| `tests/property/` skeleton with all 24 invariants stubbed (`@hypothesis.given(...)` with `pytest.skip("Phase 2")` body) | Lets us count invariants in CI even before impl |
| `tests/golden/fixtures/` directory created with placeholder fixtures | Real fixtures filled in Phase 2 |
| `tests/integration/test_replay_audit_log.py` skeleton | Implementation in Phase 2 |
| `tests/integration/test_python_rust_parity.py` skeleton | Implementation in Phase 3 |
| `tests/bench/test_hotpath_o1.py` skeleton with 6 hot-path stubs | Implementation in Phase 3 |
| `tests/stress/test_24h_sustained.py` skeleton | Implementation in Phase 4 |
| `tests/smoke/test_pre_push.py` skeleton | Implementation in Phase 2 |

### Week 2

| Deliverable | Notes |
|---|---|
| `cargo-fuzz` target list under `rust/fno-math/fuzz/` for 7 targets | Empty fuzz targets, will be seeded in Phase 2 |
| `cargo-mutants` config (`.cargo/mutants.toml`) | Wires the 85% gate |
| `tests/golden/condition_truth_table.csv` skeleton with 5 example rows | Fully populated in Phase 2 |
| `.github/workflows/nightly.yml` new — stress + fuzz | |
| `.github/workflows/weekly.yml` new — mutation testing | |
| Coverage gate raised in `pyproject.toml`: `fail_under = 100` | But code paths exempted until they land |

### Week 3

| Deliverable | Notes |
|---|---|
| `4-agent hostile review pattern` documented as a runbook | `docs/runbooks/4-agent-review.md` |
| First end-to-end "empty pyramid" CI run | Should green; all property tests skip, all benches return 0 |
| Phase 1 sign-off | User reviews scaffolding |

**Exit criteria:** All 9 test layers have at least 1 file each. CI is
green with all stubs. The verification surface is mapped.

---

## Phase 2 — Groww Adapter + Spot-Index Ingest + Vocabulary (3-4 weeks)

**Goal:** Stand up the Groww data path, the 52-condition vocabulary,
and the indicator precompute pipeline.

### Week 4

| Deliverable | Notes |
|---|---|
| `brutex/adapters/groww/__init__.py` | New package |
| `brutex/adapters/groww/client.py` | HTTP client with token, rate limit |
| `brutex/adapters/groww/instruments.py` | Master CSV fetcher + securityId resolver |
| `brutex/adapters/groww/intraday.py` | `POST /v2/charts/intraday` wrapper |
| `brutex/adapters/groww/historical.py` | `POST /v2/charts/historical` wrapper |
| Property tests P-18 (rate limiter) green | |
| First live call to Groww verifies rate limit and `securityId` map | |

### Week 5

| Deliverable | Notes |
|---|---|
| `brutex/data/parser.py` updated for Groww response format | Generic candle parser |
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

**Exit criteria:** Groww ingest of 5 years × 3 instruments × 15 TFs
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
| First production sweep (full 5-year × 3 inst × 15 TF × ~33B trials) | 2-4 day wall on M4 Pro |
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
| Groww API rate-limit lower than expected | Cap at 10 r/s; verify on first call; escalate adapter only with user approval |
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
