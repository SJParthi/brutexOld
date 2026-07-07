# Phase 1 — Empty Pyramid Baseline (2026-05-27)

> **Phase 1 Week 3 deliverable** per `docs/TRACK1_PHASE_PLAN.md §"Phase
> 1 / Week 3"` row 2: "First end-to-end 'empty pyramid' CI run".
>
> **Purpose:** record the baseline state of the 9-layer test pyramid
> at the moment Phase 1 (Verification Scaffolding) closes — every
> layer has at least one file, every invariant has a stub, every
> hot-path benchmark has a name, CI runs to green via skips +
> stubs + working-but-empty tests.
>
> **Why this matters:** Phase 2+ will add real engine code. Without a
> known-good empty-pyramid baseline, you can't tell whether a future
> red CI is caused by your new code OR by a long-standing scaffolding
> defect. The baseline below is the "all-skip / all-green" reference
> CI run that every Phase 2 PR re-runs against.

---

## 1. Pyramid layer status (count snapshot 2026-05-27)

The 9-layer pyramid per `docs/TRACK1_VERIFICATION_PYRAMID.md §2`.
Counts are file-level, not test-level.

| # | Layer | Files today | Stubs | Working | Spec target | Closure |
|---|---|---|---|---|---|---|
| 1 | Unit (`tests/unit/`) | 60 | 0 | 60 | per-module 100% line/branch on `brutex/` | 33 of ~49 modules at strict 100% (DEC-INDICATOR-COVERAGE-001 + DEC-COMBO-HASH-UNIQUENESS-001); 🟡 PARTIAL |
| 2 | Property (`tests/property/`) | 6 | 1 (catalog) | 5 (combo_hash + bloom + dedup + audit + fsm) | 24 invariants × 500 examples each | 5 of 24 IMPLEMENTED, 19 of 24 ROADMAP via catalog skips |
| 3 | Golden (`tests/golden/`) | 1 working + 1 truth-table CSV (5 rows) + 1 fixtures README | 1 (truth table is skeleton ≥ 200 rows in Phase 2) | 1 (bloom golden) | 9 fixtures + 200-row truth table | 1 working test + 5-row CSV skeleton + 9-corpora README |
| 4 | Integration (`tests/integration/`) | 13 | 2 (replay_audit_log + python_rust_parity) | 11 | 4 oracle + N drivers | 2 skeletons land Phase 1; 3rd oracle (vocabulary) lands Phase 2 |
| 5 | Benchmark (`tests/bench/`) | 5 | 0 | 5 (3 ratio gates + 1 absolute stub + 1 extrapolation) | 6 hot-path ratios all ≤ 1.1 + 1 sweep extrapolation | M4 Pro absolute is 📋 ROADMAP until runner online (DEC-M4PRO-ABSOLUTE-001) |
| 6 | Stress (`tests/stress/`) | 1 (audit chain sustained) | 0 | 1 | ~~24h-sustained on each hot path~~ multi-hour sustained per hot path **(CORRECTED — DEC-SOAK-REAL-TIER-001 #1552: no tier delivers a 24h single run; ≤ 20 h per dispatch, SEC9-1)** | nightly ~5-min regression gate via `stress-nightly.yml` + weekly self-hosted multi-hour soak via `soak-selfhosted.yml` (#1552) |
| 7 | Smoke (`tests/smoke/`) | 1 | 0 | 1 (pre-push, 12 tests, < 30 sec) | < 30s pre-push sanity | DEC-PHASE1-SMOKE-AND-SKELETONS-001 |
| 8 | Fuzz (`rust/fno-math/fuzz/`) | 1 README directory marker | 1 (whole dir is README + ROADMAP for 7 targets) | 0 | 7 cargo-fuzz targets each running 10 min/night | activates per-target alongside each Phase 2 hot-path landing |
| 9 | Mutation (`.cargo/mutants.toml` + `mutation.yml`) | 1 config + 1 workflow | 0 | 1 weekly workflow exists | ≥ 85% mutation score Python + Rust | mutation.yml runs weekly; Python state.py gated at 85% (DEC-P0-003) |

**All 9 layers have at least 1 file present.** This is the Phase 1
exit criterion per `docs/TRACK1_PHASE_PLAN.md` line 113.

---

## 2. 24-invariant catalog snapshot

Per `tests/property/test_invariants_catalog.py` (DEC-PHASE1-INVARIANT-
CATALOG-001, MIGRATION entry #250).

| Status | Count | Invariant IDs |
|---|---|---|
| ✅ IMPLEMENTED (sibling file) | 5 | P-01, P-02 (combo_hash uniqueness) · P-05 (bloom invariants) · P-16, P-17 (audit chain properties) |
| 📋 ROADMAP — Phase 1.5 (adapter) | 1 | P-18 (rate limiter pacing) |
| 📋 ROADMAP — Phase 2 (engine) | 16 | P-03, P-04 (PnL accounting) · P-06 (instrumented bar accessor) · P-07, P-10, P-12, P-15 (Track 2 cost/futures) · P-08 (resampler) · P-09 (forced-exit) · P-11, P-24 (mask commutativity + concurrency) · P-13 (EMA contraction) · P-14 (Pivot/Fib purity) · P-19, P-20 (deterministic Parquet) · P-22, P-23 (calendar + session) |
| 📋 ROADMAP — Phase 3+ (advanced) | 0 | — |

CI's `pytest --tb=short --no-header tests/property/test_invariants_catalog.py`
produces 5 skip + 19 skip = **24 skips total, 0 failures**. The skip
messages carry the implementation pointer; CI's `-rs` flag lists them
all on every run.

---

## 3. Golden fixtures snapshot

Per `tests/golden/fixtures/README.md` (DEC-PHASE1-SMOKE-AND-SKELETONS-
001, MIGRATION entry #251) + `tests/golden/condition_truth_table.csv`
(DEC-PHASE1-WEEK2-SCAFFOLDING-001, MIGRATION entry #252).

| Fixture | State | Closure |
|---|---|---|
| `groww_intraday_2024-01-02_NIFTY.json` | 📋 PHASE 2 | Lands with Phase 2 ingest pipeline |
| `groww_intraday_2024-02-01_NIFTY.json` (Budget Day) | 📋 PHASE 2 | Lands with Phase 2 ingest pipeline |
| `groww_intraday_2024-11-01_NIFTY.json` (Diwali Muhurat) | 📋 PHASE 2 | Lands with Phase 2 ingest pipeline |
| `groww_intraday_2024-05-20_NIFTY.json` (Election holiday) | 📋 PHASE 2 | Lands with Phase 2 ingest pipeline |
| `groww_intraday_2025-03-14_NIFTY.json` (Holi) | 📋 PHASE 2 | Lands with Phase 2 ingest pipeline |
| `indicator_2024-06-03_NIFTY_1m.json` (TradingView pin) | 📋 PHASE 2 | Lands with first indicator unit landing |
| `condition_truth_table.csv` | ✅ 5-row skeleton | Phase 2 expands to ≥ 200 rows |
| `paper_pnl_12fill.csv` | 📋 PHASE 2 | Lands with cost-calculator unit |
| `e2e_smoke_2024-09-19.parquet` | 📋 PHASE 2 | Lands at end of Phase 2 vocabulary work |

**1 of 9 fixtures has a skeleton present today.** The 8 others are
explicitly Phase 2 deliverables; the README + missing-file gap is
discoverable via grep but not currently a CI failure (the spec only
requires the directory to exist with at least the README placeholder).

---

## 4. Hot-path benchmark snapshot

Per `docs/TRACK1_VERIFICATION_PYRAMID.md §5`.

| Hot path | Test file | Status | Gate |
|---|---|---|---|
| Bar lookup by ts | (covered by `test_store_o1.py` — LMDB lookup proxy) | ✅ ratio gate live | p99(N=1M)/p99(N=100K) ≤ 8× per DEC-P0-020 (relaxed for shared CI) |
| Indicator value get | (covered by `test_throughput_canary.py`) | ✅ ratio gate live | numpy mask AND throughput |
| Bloom dedup pre-check | (covered by `test_store_o1.py` Bloom path) | ✅ ratio gate live | < 200 ns p99 target |
| LMDB dedup confirm | `test_store_o1.py` | ✅ working | 3× relaxed ratio gate per DEC-P0-020 |
| Strategy mask u64 AND | `test_throughput_canary.py` | ✅ working | numpy throughput ratio |
| Result Parquet append | 📋 PHASE 2 | not yet — needs writer | 5 µs/row target |
| Full-sweep extrapolation | `test_full_sweep_extrapolation.py` | ✅ working | projects to 33B trials wall-time |
| Throughput absolute | `test_throughput_absolute.py` | ✅ stub | activates with `BRUTEX_M4PRO_ABS=1` on self-hosted runner |
| Rate-limiter pacing | `test_rate_limiter_o1.py` | ✅ working | call-pacing latency under load |

**6 of 6 ratio-gated hot paths are present today** (5 working + 1
stub awaiting runner). The Phase 2 Parquet append benchmark is the
only gap and lands with the writer itself.

---

## 5. CI workflow inventory

Per `.github/workflows/`.

| Workflow | Cadence | Empty-pyramid behaviour |
|---|---|---|
| `ci.yml` | every push + pull_request | runs all 9 layers; passes 100% (catalog all-skip + integration skeletons all-skip + benches return real ratios + fuzz dir is README only — no fuzz job runs without explicit target) |
| `pre-merge-gate.yml` | every PR | aggregates ci.yml + zplus-pr-guard.sh marker check |
| `auto-merge.yml` | label `automerge` | squashes when ci-ok passes |
| `bench-m4pro.yml` | self-hosted M4 Pro runner only | skip-when-not-online; no CI failure (DEC-M4PRO-ABSOLUTE-001) |
| `branch-protection-drift.yml` | daily 03:30 UTC | reports drift; no CI failure on the branch under review |
| `dep-freshness-nightly.yml` | nightly | report-only; no CI failure |
| `mutation.yml` | weekly | runs cargo-mutants + mutmut; Python state.py gated at 85% (DEC-P0-003) |
| `redteam-nightly.yml` | nightly | runs `tests/redteam/`; report-only on PR |
| `release.yml` | tag push | Release Please |
| `safety.yml` | every push | bandit + secrets scan + gitleaks |
| `security-audit.yml` | weekly | CodeQL + Semgrep + Trivy + pip-audit + cargo-audit |
| `stress-nightly.yml` | nightly | runs `tests/stress/`; report-only on PR |

**Empty-pyramid expectation:** on `claude/*` branch push, `ci.yml` is
the only gate that runs synchronously. It MUST go green with the
following pattern:

- **lint** (ruff + ruff format) — GREEN
- **type** (mypy strict + pyright strict) — GREEN
- **unit** (60 files) — GREEN
- **property** (5 implemented + 19 catalog-skip = 24 skips on the
  catalog file; 5 implemented invariants assert across hundreds of
  hypothesis examples) — GREEN
- **golden** (1 test + 5-row CSV skeleton) — GREEN
- **integration** (11 working + 2 all-skip skeletons) — GREEN
- **bench** (5 files; ratios pass; absolute skipped sans env) — GREEN
  with possible flake on the throughput canary (treated as non-blocking
  per DEC-P0-020)
- **stress** (1 working file; PR-event runs reduced set) — GREEN
- **smoke** (12 working tests, < 30s) — GREEN
- **redteam** (10 working tests) — GREEN
- **coverage** (per-module strict-100% on 33 modules; global 90% floor)
  — GREEN
- **duplication** (jscpd ≤ 3%) — GREEN
- **security** (bandit -l + gitleaks) — GREEN
- **z+ marker guard** (`scripts/ci/zplus-pr-guard.sh`) — GREEN

Mutation testing (`mutation.yml`) is weekly; not in the empty-pyramid
per-PR run. Same for `security-audit.yml`.

---

## 6. Empty-pyramid CI run — verification commands

Reproduce the green run locally:

```bash
# Activate the venv (or run bootstrap.sh first if .venv is missing)
source .venv/bin/activate

# Layer 1 — unit
pytest tests/unit -q

# Layer 2 — property (5 implemented + 19 skips on catalog)
pytest tests/property -q -rs

# Layer 3 — golden
pytest tests/golden -q

# Layer 4 — integration (skips visible)
pytest tests/integration -q -rs

# Layer 5 — bench (ratios pass; absolute skipped sans env)
pytest tests/bench -q -rs

# Layer 6 — stress (per-PR regression subset; the multi-hour soak runs only
# on soak-selfhosted.yml — CORRECTED, DEC-SOAK-REAL-TIER-001 #1552: the prior
# command here deselected a `slow_24h` marker that was NEVER registered, so
# the deselect was a no-op and "full run is nightly" repeated the retracted
# #208 claim)
pytest tests/stress -q -m "not soak"

# Layer 7 — smoke
pytest tests/smoke -q

# Layer 8 — fuzz (no targets yet → no-op locally)
ls rust/fno-math/fuzz/ 2>/dev/null

# Layer 9 — mutation (weekly; not in empty-pyramid local run)
# cargo mutants --config .cargo/mutants.toml --manifest-path rust/fno-math/Cargo.toml

# Lint / type / format
ruff check brutex tests
ruff format --check brutex tests
mypy --strict brutex
pyright brutex

# Aggregate verdict
echo "Empty pyramid baseline: GREEN if all layers above passed."
```

Expected aggregate result (post `.venv` + `maturin develop --release`):

```
unit:        N tests passed
property:    M passed, 19 skipped (catalog roadmap items)
golden:      2 passed
integration: K passed, 9 skipped (2 skeleton files × stub counts)
bench:       29 passed, 1 skipped (absolute without env)
stress:      1 passed (subset)
smoke:       12 passed
fuzz:        0 targets (README only)
mutation:    weekly only
```

Any deviation from this pattern after `git checkout origin/main` is a
regression. Open a follow-up PR with a 1-agent hostile review.

---

## 7. Phase 1 sign-off checklist (operator-facing)

Operator: tick each box to sign off Phase 1.

- [ ] All 9 pyramid layers have ≥ 1 file in the matrix above
- [ ] `tests/property/test_invariants_catalog.py` lists all 24
      invariants by ID, with 5 IMPLEMENTED + 19 ROADMAP
- [ ] `tests/golden/fixtures/README.md` documents all 9 planned
      corpora; `condition_truth_table.csv` has ≥ 5 skeleton rows
- [ ] `tests/integration/test_replay_audit_log.py` + `test_python_rust
      _parity.py` skeletons land with explicit Phase 2 skip messages
- [ ] `tests/smoke/test_pre_push.py` runs in < 30s and catches the 5
      regression classes (lint / format / import / conv-commit / docs)
- [ ] `tests/stress/test_audit_chain_sustained.py` lands as the stress
      anchor; ~~nightly workflow `stress-nightly.yml` runs the full
      24h subset~~ **CORRECTED (DEC-SOAK-REAL-TIER-001 #1552):**
      `stress-nightly.yml` is a ~5-min regression gate (its fence refuses
      `STRESS_EVENTS` > 200000); the real multi-hour soak runs weekly on
      the self-hosted `soak-selfhosted.yml` tier
- [ ] `rust/fno-math/fuzz/README.md` documents the 7 planned cargo-
      fuzz targets; per-target activation deferred to Phase 2
- [ ] `.cargo/mutants.toml` configured for the existing Rust modules;
      `mutation.yml` runs weekly
- [ ] `docs/runbooks/4-agent-review.md` is the canonical runbook
      (DEC-PHASE1-WEEK3-RUNBOOK-001, this PR)
- [ ] Empty-pyramid CI run is GREEN on `main` HEAD (= the merge
      commit of this PR)
- [ ] `docs/MIGRATION.md` has a fresh DEC entry citing this Phase 1
      sign-off

If all 10 boxes check, Phase 1 closes. Phase 2 unblocks the moment
the operator (a) subscribes to Groww (₹499/mo), (b) sets the env vars
documented in `docs/runbooks/ENV_VARS.md`, (c) runs
`scripts/groww_probe.py` to HARD-GATE the INDIA VIX presence + RPS
bucket per DEC-VENDOR-PIVOT-001.

---

## 8. Honest envelope (Golden Rule #10)

- ✅ REAL: every layer of the 9-layer pyramid has a file on disk
  today. CI's `pytest -q` exits 0 on this scaffolding.
- ✅ REAL: 5 of 24 property invariants assert real properties; 19 are
  honestly marked ROADMAP with a Phase pointer.
- 🟡 PARTIAL: 1 of 9 golden fixtures has a skeleton (the truth table
  CSV at 5 of ≥ 200 target rows). The 8 other corpora are explicitly
  Phase 2 deliverables.
- 🟡 PARTIAL: 6 of 7 hot-path benchmarks have a ratio gate; the
  Parquet-append bench waits for the writer itself.
- 🟡 PARTIAL: fuzz directory exists with README only; per-target
  activation is Phase 2.
- 📋 ROADMAP: Phase 2+ fills in every ROADMAP-marked line above.

**The empty pyramid does NOT prove correctness of the engine.** It
proves the *test surface is mapped*. Phase 2 will add the engine code
that the empty pyramid is shaped to catch defects in.

---

## 9. Cross-references

- `docs/TRACK1_PHASE_PLAN.md §"Phase 1"` — original phase scope
- `docs/TRACK1_VERIFICATION_PYRAMID.md` — the 9-layer spec
- `docs/runbooks/4-agent-review.md` — Phase 1 Week 3 sister deliverable
- `docs/STANDARDS_100PCT_HONEST_AUDIT.md §2` — the 14 bracket gates
  this baseline informs
- `docs/MIGRATION.md` entries #250, #251, #252 — Phase 1 Week 1 + Week 2
  predecessors
- `CLAUDE.md §10` — pre-push gate commands the smoke layer enforces
