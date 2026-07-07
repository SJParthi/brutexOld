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
| 6 | Stress (`tests/stress/`) | 1 (audit chain sustained) | 0 | 1 | 24h-sustained on each hot path | nightly via `stress-nightly.yml` |
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

# Layer 6 — stress (PR-event subset; full run is nightly)
pytest tests/stress -q -m "not slow_24h"

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

> **RECONCILED 2026-07-06 — closure by SUPERSESSION**
> (DEC-PHASE1-SIGNOFF-RECONCILE-001, MIGRATION.md #1540). The formal
> operator tick NEVER happened in 2026-05; the operator has since
> directed and approved the entire Phase-2/3-era programme
> (MIGRATION.md #254–#1520+), and every technical item below was
> independently re-verified against the live tree on 2026-07-06.
> This is NOT a backdated claim that boxes were ticked in May — each
> ✅ below records the 2026-07-06 re-verification verdict with its
> governing ledger citation (Golden Rule #10). One internal defect is
> also corrected: the checklist lists **11** boxes but its own trailer
> said "all 10 boxes" (every external pointer repeated "10-item").

Operator: tick each box to sign off Phase 1. *(Reconciled 2026-07-06:
every box below is closed by supersession per the banner above.)*

- [x] All 9 pyramid layers have ≥ 1 file in the matrix above
      — ✅ all 9 layers populated with real tests today
      (`tests/{unit,property,golden,integration,bench,stress,smoke}/`,
      `rust/fno-math/fuzz/fuzz_targets/` + `rust/trading-core/fuzz/`,
      `.cargo/mutants.toml` + `mutation.yml`);
      DEC-PHASE1-SMOKE-AND-SKELETONS-001 (MIGRATION.md #251) +
      DEC-PHASE1-WEEK2-SCAFFOLDING-001 (#252).
- [x] `tests/property/test_invariants_catalog.py` lists all 24
      invariants by ID, with 5 IMPLEMENTED + 19 ROADMAP
      — ✅ SUPERSEDED-AND-EXCEEDED: the catalog now holds **26**
      invariants (P-25/P-26 added), **22** with implemented-in-sibling
      pointers and **4** honestly pending (P-07/P-10/P-12/P-15 —
      Track-2 cost/futures subjects). Promotions:
      DEC-PROPERTY-INVARIANTS-LIVE-001 (MIGRATION.md #548b) ·
      DEC-LOOKAHEAD-BARRIER-001 (#573, P-06) ·
      DEC-PHASE2-INVARIANTS-PROMOTE-001 (#579b) · P-20 made REAL by
      DEC-INGEST-AUDIT-WIRING-001 (#1500).
- [x] `tests/golden/fixtures/README.md` documents all 9 planned
      corpora; `condition_truth_table.csv` has ≥ 5 skeleton rows
      — ✅ EXCEEDED: the README's own status line reads "✅ All Phase 2
      corpora (#1–5, #7, #8, #9) are landed" (DEC-PHASE2-CORPORA-001,
      MIGRATION.md #548a; DEC-ENGINE-PAISA-ACCOUNTING-GOLDEN-001 #544);
      the truth table is a real, evaluator-backed golden artifact —
      1,144 rows at DEC-PHASE2-TRUTH-TABLE-001 (#538), 1,320 rows
      (22 cases × 60 conditions) today after the 52→60 vocabulary
      growth (DEC-VOCAB-U128-CODEGEN-001 #1010).
- [x] `tests/integration/test_replay_audit_log.py` + `test_python_rust
      _parity.py` skeletons land with explicit Phase 2 skip messages
      — ✅ PROMOTED past the target: the skeletons landed (#251) and
      were later replaced by REAL tests — `test_python_rust_parity.py`
      is the live trading-core bit-identity anchor that supersedes the
      retired parity skeleton (DEC-SHARED-CORE-001 Phase 2c,
      MIGRATION.md #441, whose entry retires the
      DEC-PHASE1-INTEGRATION-SKELETONS-001 skeleton by name), and
      `test_replay_audit_log.py`'s P-20 replay round-trip is REAL over
      a fixture pull (DEC-INGEST-AUDIT-WIRING-001 #1500).
- [x] `tests/smoke/test_pre_push.py` runs in < 30s and catches the 5
      regression classes (lint / format / import / conv-commit / docs)
      — ✅ landed and live in CI (MIGRATION.md #251).
- [x] `tests/stress/test_audit_chain_sustained.py` lands as the stress
      anchor; nightly workflow `stress-nightly.yml` runs the full
      24h subset
      — ✅ both present and running (DEC-STRESS-001, MIGRATION.md #208).
- [x] `rust/fno-math/fuzz/README.md` documents the 7 planned cargo-
      fuzz targets; per-target activation deferred to Phase 2
      — ✅ EXCEEDED: **5** of the README's 7-planned rows are LIVE
      (`fuzz_mask_and` + `fuzz_supermask` here; pivot/fib + the
      52-vocab evaluator in `rust/trading-core/fuzz/`), 4 honestly
      ROADMAP ("kernel not yet built") — DEC-FUZZ-HOTKERNEL-001
      (MIGRATION.md #585c). Live-tree recount 2026-07-07: **8**
      fuzz-target FILES exist in all — 4 further targets
      (`fuzz_greeks` / `fuzz_market_structure` / `fuzz_metadata` /
      `fuzz_spec`) landed in `rust/trading-core/fuzz/` after #585c,
      outside the README's 7-planned map (the earlier "5 LIVE / 4
      ROADMAP" line was the #585c row accounting, not a file count).
- [x] `.cargo/mutants.toml` configured for the existing Rust modules;
      `mutation.yml` runs weekly
      — ✅ both present; HONEST caveat carried, not papered over: the
      two Rust cargo-mutants jobs chronically hit the 120-min timeout
      and now ALERT on `cancelled()` instead of dying silently
      (DEC-MUTATION-ALERT-CANCELLED-001, MIGRATION.md #1511).
- [x] `docs/runbooks/4-agent-review.md` is the canonical runbook
      (DEC-PHASE1-WEEK3-RUNBOOK-001, this PR)
      — ✅ landed with this baseline's own PR (MIGRATION.md #253).
- [x] Empty-pyramid CI run is GREEN on `main` HEAD (= the merge
      commit of this PR)
      — ✅ AS SUPERSEDED: the "empty pyramid" reference state is long
      gone (the pyramid is populated); CI-green-on-`main` is
      CONTINUOUSLY enforced by the `CI OK (aggregate gate)` required
      check + the auto-merge envelope (DEC-MERGE-GUARANTEE-001,
      MIGRATION.md #161b / DEC-AUTOMERGE-CLEAN-STRICT-001 #612).
- [x] `docs/MIGRATION.md` has a fresh DEC entry citing this Phase 1
      sign-off
      — ✅ satisfied by DEC-PHASE1-SIGNOFF-RECONCILE-001 (MIGRATION.md
      #1540) itself — the FIRST ledger entry ever to CLOSE this
      sign-off (the checklist's own creating entry #253 already cites
      it; an exhaustive DEC-PHASE1 grep of MIGRATION.md confirms no
      earlier CLOSURE entry existed; this was the one genuinely-open
      item).

If all 11 boxes check, Phase 1 closes. *(Count corrected 2026-07-06:
the original trailer said "all 10 boxes" while the list above has
always had 11 — the count itself was defective; see the banner.)*
Phase 2 unblocks the moment
the operator (a) subscribes to Groww (₹499/mo), (b) sets the env vars
documented in `docs/runbooks/ENV_VARS.md`, (c) runs
`scripts/groww_probe.py` to HARD-GATE the INDIA VIX presence + RPS
bucket per DEC-VENDOR-PIVOT-001.

> **Trailer SUPERSEDED 2026-07-06** (same reconciliation, kept above
> as the dated record): condition (b) is SUPERSEDED by
> DEC-AWS-ONLY-CREDS-001 (MIGRATION.md #1065) — Groww credentials +
> tokens are AWS-SSM-only, read fail-fast, and NEVER env vars (see
> `docs/runbooks/ENV_VARS.md` §5). Conditions (a) and (c) were
> OVERTAKEN BY EVENTS: the DEC-VENDOR-PIVOT-001 (#246) framing
> predates the ADR-0002 → ADR-0009 vendor round-trip
> (DEC-DHAN-GROWW-REMIGRATE-001, MIGRATION.md #667), under which Groww
> is the live production data source with production pulls log-verified
> (e.g. the #906/#909 live-log audits); the remaining ADR-0009 Phase-0
> probe gates are tracked in CLAUDE.md §6, not by this trailer.

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
