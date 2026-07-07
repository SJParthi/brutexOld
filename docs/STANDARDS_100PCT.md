# BruteX — 100% Always Standards (Project Law)

**Status:** REWRITTEN HONESTLY 2026-05-16 night, post 6-agent
assurance round. **Earlier draft was 62% overstated per forensic
audit (Agents A + C + D).** This version distinguishes ENFORCED
gates from ROADMAP items, per Golden Rule #10 (honest limits).

**Applies to:** every branch, every PR, every sub-PR, every commit,
every task, every sub-task, every plan, every doc, every spec,
every agent run — for the lifetime of the BruteX project.

**Single source of truth for project standards.** Supersedes ad-hoc
quality rules scattered across other docs.

---

## 0. The decree

> *"100 percentage dude" — extensive, comprehensive, intensive, deep
> thorough. Code coverage, code scanning, code duplication,
> functionality missing, scenario missing, bug fixing, issues finding,
> testing types, testing coverage, extensive comprehensive real-time
> testing, code performance, uniqueness, deduplication, O(1) latency
> always. Guarantee, assurance, proof — no hallucination, no illusion,
> no missing.*

This document operationalizes that decree HONESTLY: it distinguishes
what is CURRENTLY ENFORCED (CI gates blocking merge today) from
what is ROADMAP (planned but not yet wired).

**The decree itself is aspirational; the doc enforcing it is realistic.**

---

## 1. The 18 standing requirements — REAL STATUS TABLE

Status legend:
- ✅ **ENFORCED** — CI gate blocks merge today
- 🟡 **PARTIAL** — runs but advisory-only (`continue-on-error: true` or `|| true`) OR threshold below decree
- 📋 **ROADMAP** — planned, not yet wired into CI
- 👁️ **MANUAL** — human-reviewer enforced via PR template, no automation possible

| # | Requirement | Decree target | Current state | Status | Closing path |
|---|---|---|---|---|---|
| 1 | Coverage line | 100% on `brutex/` | `fail_under=90` global floor; **strict per-module 100% gate at 103 modules** (verified 2026-06-02; was 51 on 2026-05-27 before the Dhan/data/options/dashboard gate expansion — DEC-WORKSPACE-VERIFY-001 entry #398 D3 / DEC-DOC-DRIFT-RECONCILE-001) after the Option B 5-PR ratchet arc (DEC-COV-001 steps 1–17, 2026-05-24; DEC-INDICATOR-COVERAGE-001 entry #245 +3; DEC-VENDOR-PIVOT-001 entry #246 −5 Groww; DEC-COV-RATCHET-001 entry #256 +14; DEC-COV-BACKFILL-001/002/003/004 entries #257-#260 +9 backfilled; **DEC-COV-BACKFILL-LIMIT-001 entry #261 closes Option B with operator-accepted limit** on remaining 8 orchestrator modules blocked by fastbloom_rs PyO3 ABI3). **103 modules gated** (verified 2026-06-02 by `grep -vP '^\s*#' .github/workflows/ci.yml | grep -oP '(?<=--cov=)brutex\.[a-zA-Z0-9_.]+' | grep -v -- '--cov-fail-under' | sort -u | wc -l`; was 51 on 2026-05-27). Coverage families fully covered: indicators, observability, costs, strategies, vocabulary, data, options, config, execution (`execution.{errors,types,costs,simulator}`); plus `doctor`, `models`, `universe`, `engine.combo_mask`, `feed.resampler`. The fastbloom_rs PyO3 ABI3 single-init blocker on the standalone orchestrator strict gates is **RESOLVED by DEC-ORCH-LAZY-BLOOM-001 (entry #589, 2026-06-08)** — `bloom.py` now imports `fastbloom_rs` lazily (on first `BloomFilter` construction, mirroring the existing lazy `fno_math` import), so importing `brutex.orchestrator.*` no longer initialises the abi3≤3.8 extension at coverage-import time. As a result `orchestrator.{replay, retry, audit, adaptive_rate_limiter}` are now strict-100% gated (the "lazy-import refactor" closure path entry #261 named). The remaining six — `orchestrator.{state (98.6%), store (91.3%), pipeline (97.3%), rate_limiter (86.6%), quota_tracker (92.6%), bloom (96.8%)}` standalone gaps — were CLOSED by **DEC-ORCH-STRICT-GATES-001 (entry #613, 2026-06-10)**: surgical gap-closing tests in each dedicated unit file took all six to **100.00% line+branch STANDALONE** (verified locally, Python 3.12.3) and the six gate lines were added to ci.yml — every `brutex.orchestrator.*` submodule with a dedicated test file is now strict-gated (10 of 10), fully retiring the entry #261 operator-accepted ABI3 limit. The new flush() test also exposed + fixed a latent `lmdb sync()` keyword-arg TypeError in `store.py`. **140 unique modules on strict `--cov-fail-under=100` gate lines** (verified 2026-06-10 by `grep -vP '^\s*#' .github/workflows/ci.yml \| grep -P '^\s*pytest .*--cov-fail-under=100' \| grep -oP '(?<=--cov=)brutex[a-zA-Z0-9_.]*' \| sort -u \| wc -l`; was 134 pre-#613 — earlier "103/107" snapshots used a looser count). | 🟢 (floor at **100** with the omit list REMOVED — ratchet arc 90 → 99 → 100: DEC-COV-SMALL-GAPS-001 closed the 3 small gaps, DEC-COV-APP-BACKFILL-001 backfilled dashboard/app.py, and DEC-COV-CLI-HARNESS-001 2026-06-12 backfilled cli.py 50.17%→100.00% (496/496 stmts, 98/98 branches) + __main__.py and DELETED the omit key; the WHOLE package measures exactly 100.00%, 6738 tests, gate exit 0) | The DEC-COV-001 arc is CLOSED — coverage line is decree-true with no asterisk; regressions fail the zero-cushion aggregate gate + the 140 per-module strict gates. |
| 2 | Coverage branch | 100% | `--cov-branch` flag ON; same strict-per-module gate as row 1 (DEC-COV-001) also enforces branch=100% on ratcheted modules; the aggregate floor at 100 covers every branch arm (4506/4506 measured at DEC-COV-CLI-HARNESS-001 close). | 🟢 | Closed with row 1 (DEC-COV-CLI-HARNESS-001). |
| 3 | Mutation testing | ≥ 85% | DEC-P0-001 (2026-05-17): `\|\| true` REMOVED; mutmut junitxml → Python parser enforces gate on `brutex/orchestrator/state.py`. DEC-P0-002 (2026-05-17 same day, REVERTED by DEC-P0-003): briefly lowered to 60% interim; operator decreed "no downgrade — 100% standards". **DEC-P0-003 (2026-05-17): threshold RESTORED to 85%.** **DEC-MUTATION-GATE-RANKING-001 (entry #590, 2026-06-08): the weekly `mutation.yml` gate extended to the ranking/metrics MATH — `brutex/metrics/deflated_sharpe.py` (DSR engine) added at ZERO surviving mutants; the broken job install fixed (mutmut pinned `<3.0`, Rust wheels + `[data,ui]` added so `pytest tests/unit` can collect). Sharpe (`compute.py`, 172/177 mutants killed) + composite (`ranking`) heavily hardened; folding them into the zero-survivor set is the next ratchet (residual equivalent mutants + property-test-killed mutants).** **DEC-MUTATION-GATE-RANKING-002 (entry #615, 2026-06-10): that ratchet EXECUTED for the composite ranking — `brutex/ranking/__init__.py` + `brutex/ranking/overfit.py` added to `--paths-to-mutate` at ZERO surviving mutants (measured 42 → 0: 28 new tests kill 35; 7 provably-equivalent excluded via documented `# pragma: no mutate`); the job restructured into two sequential passes so the fast math pass always reports. DISCOVERED + disclosed (not fixed): the broad orchestrator pass has never finished inside the 120-min job budget since #590 un-broke the install (cancelled at 123/804 mutants with 26 early survivors) — proposed DEC-MUTATION-RUNTIME-001.** **DEC-MUTATION-RUNTIME-001 (entry #624, 2026-06-10): that follow-up EXECUTED — all 223 measured orchestrator/data survivors killed (~179 new tests + 13 proven pragmas), every pass moved onto per-module focused runners, every runner wrapped `sh -c '… || exit 1'` (closes the mutmut exit-2 import-crash blind spot), the 2 rate_limiter TIMEOUT mutants converted to <1 s kills; final consolidated verification **951/951 mutants KILLED, 0 survived/timed-out**, whole job measured ~35 min local (fits the 120-min budget with >2× headroom).** **DEC-MUTATION-GATE-COMPUTE-001 (2026-06-12): the `compute.py` fold-in (#590's "next ratchet") EXECUTED — `brutex/metrics/compute.py` (Sharpe/drawdown/moments) joined the per-module loop at ZERO surviving mutants (measured 16 survivors + 3 untested → 0: 7 new targeted tests; provably-equivalent mutants excluded via documented in-module pragmas; the ruin boundary re-phrased `<= 0` → `< 1` on ints and the dead CAGR lower clamp / dead `n_trades` guard restructured so every remaining mutant is observable).** | 🟡 (the PYTHON mutation job is zero-survivor + completing; the fno-math `cargo-mutants` residual is now SHIPPED-pending-verification: **DEC-MUTATION-RUNTIME-002** (2026-06-12) measured BOTH defects from the run history — 24/30 recent runs CANCELLED by the ref-keyed `cancel-in-progress` group (every push-to-main superseded the in-flight run, incl. the weekly scheduled pass, run 27118349051 killed 25 min in) AND a genuine serial overrun (run 27125614548's cargo-mutants step ran exactly the full 120 min and was timeout-killed) — fixed by event-keyed concurrency groups (the weekly pass can no longer be cancelled by merges) + `--jobs 2`; completion proof = the next SCHEDULED run green inside budget) | Python side DONE at 951/951 + the `compute.py` fold-in (DEC-MUTATION-GATE-COMPUTE-001, #590 ratchet closed); remaining: the DEC-MUTATION-RUNTIME-002 verification run |
| 4 | mypy --strict | 0 errors | Configured strict + 6 extra strict flags; pre-existing failures cleared in commit d57efaf + DEC-P0-001 ruff sweep | ✅ | none |
| 4 | pyright | 0 errors | DEC-P0-001 (2026-05-17): bumped `typeCheckingMode = "strict"`; 3 new narrow silences for strict-only checks (`reportMissingParameterType`, `reportMissingTypeArgument`, `reportUnnecessaryComparison`) on legitimate patterns in test fixtures | ✅ | none |
| 5 | ruff lint | E+W+F+I+N | Active; DEC-P0-001 cleared 29 legacy errors (PLR2004/S101/ERA001/SIM110/RUF046/RUF022/N812/SIM108/E741) + applied `ruff format` to 19 files | ✅ | none |
| 5 | pylint (via ruff PL) | pylint-equivalent rules | **ruff `PL` subset ENFORCED** (DEC-PYLINT-DROP-001 correction 2026-05-24): `pyproject.toml [tool.ruff.lint].select` includes `"PL"` — runs on every push via the `Lint (ruff)` CI job, blocking. 7 specific PL rules are explicitly ignored with documented rationale (PLR0913/0911/0912/0915 for dispatchers/CLIs/adapter boundaries; PLC0415 for lazy imports; PLE0604 for math facade's dynamic `__all__`; PLR2004 in tests/). Standalone pylint dropped — ruff's Rust-based PL is faster + same rule coverage. | 🟢 | none (ruff PL covers pylint-equivalent rules; standalone pylint claim removed) |
| 6 | bandit security | 0 high/critical | DEC-P0-001 (2026-05-17): kept `-l` (low+ — stricter than `-ll`). S104/S105/S108/S110/S311 removed from project-wide ruff `ignore` (S110 narrowed to `tests/` per-file-ignores); per-line noqa at 4 brutex/ sites with rationale | ✅ | none |
| 7 | Code duplication | zero | DEC-P0-001 (2026-05-17): `\|\| true` REMOVED; jscpd narrowed to `brutex/` only at 3% threshold (tests/ legitimately repeat fixture patterns) | ✅ | Tighten threshold from 3% → 1% in a follow-up PR |
| 7 | Dead code | zero | DEC-P0-001 (2026-05-17): `\|\| true` REMOVED; whitelist at `scripts/ci/vulture_whitelist.py` covers context-manager protocol, `__post_init__`, and `audit.verify_chain` | ✅ | Extend whitelist when CI surfaces real false positives |
| 8 | Spec ↔ test crosswalk | spec sections → tests | **FAIL-CLOSED** (DEC-CROSSWALK-001 correction 2026-05-24): `scripts/ci/spec_test_crosswalk.py` now exits 1 if a spec-section regex fails to match. Env-var escape `CROSSWALK_ALLOW_STALE=1` for local script-iteration only (CI never sets it; verified `grep -rn CROSSWALK_ALLOW_STALE .github/` = 0). Failure message cites this row + MIGRATION.md entry #206 for actionable next steps. | 🟢 | Expansion to additionally cover `TRACK1_BACKTEST_SPEC.md` sections tracked separately |
| 9 | Property tests | hypothesis | `tests/property/` runs with `-x` (stops on first fail) | ✅ runs, advisory mode | Add `--hypothesis-seed=0 --derandomize` for CI determinism |
| 10 | Bug-fix regression test | required | PR template + **partial automation added** (DEC-BUGFIX-REGRESSION-001 2026-05-24): `scripts/ci/validate_bugfix_regression.py` matches PR title against conventional-commit `fix:`/`bugfix:`/`hotfix:` prefixes + body keywords (`bug`, `regression`, `fixes #NNN`), then fails CI if no files under `tests/` were changed. Reviewer still judges "is this the RIGHT regression test for the bug" — script catches the most common failure mode (bug-fix PR ships no test changes at all). | 👁️ + ✅ partial | "Must have RIGHT test for the bug" stays reviewer; "bug-fix PR must touch tests/" now CI-enforced |
| 11 | 7 test types | all 7 | unit ✅, integration ✅, property ✅, golden ✅, bench ✅, mutation ✅, **stress ✅** (DEC-STRESS-001 2026-05-24; corrected DEC-SOAK-REAL-TIER-001 entry #1535 2026-07-06): `tests/stress/test_audit_chain_sustained.py` runs via `.github/workflows/stress-nightly.yml` as a **~5-min ubuntu-24.04 regression gate** (daily 03:00 UTC + workflow_dispatch) — its guard step **REFUSES `STRESS_EVENTS > 200000`** (a soak-scale dispatch is rejected, not run: the 60-min job timeout structurally forbids a 10M soak from completing, ~1/17th done). The **real multi-hour soak** is a SEPARATE tier — `.github/workflows/soak-selfhosted.yml` on the self-hosted M4-Pro (CLAUDE.md §8), with a duration-DERIVED `timeout-minutes` (`soak_minutes + 30 + soak_minutes/6` — fixed install slack + duration-linear teardown, SOAK-R7-4; `soak_minutes` fenced 1..1200 (20 h) because the job's `GITHUB_TOKEN` expires at 24 h on self-hosted runners — a longer run's failure watchdog could never authenticate to file its issue, SEC9-1; the 5-day platform kill of SOAK-R8-1 stays the outer, no-longer-binding bound) that can never outrun its own clock, driving `tests/stress/test_soak_sustained.py::test_soak_sustained` (the `soak` marker) for the full `STRESS_DURATION_S` under the pure `brutex.observability.soak_drift.SoakDriftMonitor`; default weekly cron soaks 6 h. Markers `stress` + `soak` registered in `pyproject.toml`. | 🟢 | Real multi-hour soak lives on soak-selfhosted.yml (duration-derived timeout); the ubuntu tier is a ~5-min regression gate that refuses `STRESS_EVENTS > 200000` (honest envelope per Golden Rule #10 — the old `STRESS_EVENTS=10_000_000` "24h soak via workflow_dispatch" route was FALSE and is retracted, DEC-SOAK-REAL-TIER-001) |
| 12 | Real-time CI | every push + nightly | ✅ ci.yml on push + dep-freshness-nightly.yml + drift-check.yml | ✅ | none |
| 13 | O(1) per hot path | p99(1M)/p99(1K) ≤ 1.1 | **Ratio gates ENFORCED on CI** (DEC-STANDARDS-AUDIT-001 correction 2026-05-24): `tests/bench/test_store_o1.py::test_get_ratio_below_3x` gates store lookup at 3×; `tests/bench/test_throughput_canary.py::test_vectorized_mask_and_ratio` gates numpy mask AND at 8×; `test_dict_lookup_ratio` gates dict lookup at 10×. Per-CI ceiling is looser (3-10×) than the M4 Pro production target (1.1×) per documented shared-GHA flake-mitigation in DEC-P0-020; the strict 1.1× is now a WIRED **second tier** (DEC-O1-STRICT-TIER-001). | 🟢 | CI robust 3-10× tier live; the strict **1.1× tier is now WIRED** (`BRUTEX_M4PRO_STRICT` → `tests/bench/_o1_tiers.py::ratio_ceiling`, run via `bench-m4pro.yml`) — the M4-Pro runner is REGISTERED + CERTIFIED and the first real `bench-m4pro` run passed all 11 absolute floors + 4/4 33B-SLA gates (DEC-O1-CACHE-TIER-001, entry #655); the strict tier is split same-footprint 1.1× vs cross-cache-tier 3.0× (`tests/bench/_o1_tiers.py`) |
| 14 | Throughput canary ≥ 50 M/s | yes | `tests/bench/test_throughput_canary.py` LIVE (DEC-P0-020, PR #42) — 3 benchmarks gate the O(1) ratio (`p99(1M)/p99(100K) < 5×`) on the brute-force hot path (numpy mask AND + dict bar lookup). **CI gate is RATIO** (matches Rule #5 + existing `test_store_o1.py` pattern); absolute ops/s recorded in `bench-results.json` informationally. Local measurement: numpy ratio 1.27× (absolute 512M ops/s), dict ratio 3.59× (absolute 2.3M ops/s). **50 M/s absolute is the M4 Pro production envelope** (per `docs/TRACK1_PERFORMANCE_BUDGET.md`); CI shared GHA runner is the sub-target. Absolute gates would chronic-flake on shared GHA — verified empirically when PR #42 commit `8ea175a` failed both push + PR runs with 30M/2.5M absolute gates; hotfix `fd3f2dd` switched to ratio gates. | 🟡 | CI gate live (ratio); the M4-Pro runner is now REGISTERED and the first real run passed all 11 ABSOLUTE floors (mask-AND ≈ 2.0 B ops/s = 40× the 50 M/s floor — DEC-O1-CACHE-TIER-001, entry #655); stays 🟡 only until the operator's post-merge fully-green `bench-m4pro` confirmation re-run (tracked in entry #655); Phase 3 PyO3 kernel can lift further. The single-op 50 M/s floor is now COMPLEMENTED by the committed per-hardware ABSOLUTE baseline compare gate over 6 hot-path entries (DEC-BENCH-ABS-BASELINE-001 entry #1534 — dormant until the first measured bless merges) |
| 15 | Bloom + LMDB dedup | 4-tier | **Property tests ENFORCED** (DEC-STANDARDS-AUDIT-001 correction 2026-05-24): `tests/property/test_bloom_invariants.py` covers Bloom filter invariants (no-false-negatives, FPR bounds, idempotent inserts); `tests/property/test_dedup_hash_properties.py` covers hash-based dedup properties. Implementation at `brutex/orchestrator/bloom.py` + `store.py`. | 🟢 | Property tests run on every push via the `Property tests (hypothesis)` CI job |
| 16 | Audit JSONL hash-chain + `brutex doctor` | always | **`brutex/doctor.py` LIVE** (DEC-DOCTOR-001 correction 2026-05-24): `brutex doctor [--audit-file PATH] [--audit-dir PATH] [--runtime-file PATH]` subcommand runs 2 health checks (audit chain integrity via `verify_chain`; runtime file presence + parseability). Exit 0 on all PASS/SKIP; 1 on any FAIL. Wired into `brutex/cli.py` as `cmd_doctor`. Unit tests in `tests/unit/test_doctor.py` cover all check states + the CLI exit-code contract. | 🟢 | Future stress / lake / quota checks tracked as P2 — broader coverage is a ratchet on this scaffold |
| 17 | No hallucination | every claim cited | Golden Rule #1 in CLAUDE.md + PR template + **partial automation added** (DEC-HALLUCINATION-SCAN-001 2026-05-24): `scripts/ci/validate_no_hallucination.py` scans PR body for external-system keywords (Groww / Dhan [added by DEC-HONESTY-AUDIT-RECONCILE-001 — the 2026-06-11 audit found the scanner still Groww-only post-revert] / NSE / BSE / SEBI / RBI / brokerage / securityId / rate_limit / exchange_segment / Renaissance / Two Sigma / AQR / etc.) and fails CI if any paragraph mentions them without a nearby citation marker (URL, `verified YYYY-MM-DD`, `CLAUDE.md §N`, `MIGRATION.md entry #NNN`, `TRACK1_*.md`, `STANDARDS_100PCT.md`, `groww.in/trade-api/docs/docs`, `nseindia.com`, or a `DEC-FOO-NNN` identifier). Code spans (` ``` ` / `` ` ``) stripped before scanning. Reviewer still judges "is the citation CORRECT" — script catches the failure mode where a claim has NO citation at all. | 👁️ + ✅ partial | "Is the citation correct" stays reviewer; "external claims must have a citation nearby" now CI-enforced |
| 18 | Decision IDs | every locked decision | PR template requires DEC-ID + MIGRATION.md row; **partial automation added** (DEC-DEC-ID-VALIDATOR-001 2026-05-24): `scripts/ci/validate_dec_ids.py` parses PR body for `DEC-FOO-NNN` tokens and fails CI if any cited ID has no matching ledger entry. Reviewer still judges "should this PR have cited a DEC-ID at all?" | 👁️ + ✅ partial | "Must cite DEC-ID for arch change" stays reviewer; "cited DEC-IDs must exist" now CI-enforced |

### Summary count (post DEC-MUTATION-GATE-RANKING-002, 2026-06-10)

| Status | Count of 21 sub-rows | % | Delta vs pre-audit |
|---|---:|---:|---:|
| 🟢 Fully enforced | **14** (rows 4×2, 5×2 [PYLINT-DROP closed 5b], 6, 7×2, 8, 9, **11** ← DEC-STRESS-001 flipped from 🟡, 12, 13, 15, **16** ← DEC-DOCTOR-001 flipped from 📋) | **67%** | +5 (audit + CROSSWALK + PYLINT-DROP + STRESS + DOCTOR; row 3 re-classified honest 🟡 by DEC-MUTATION-GATE-RANKING-002) |
| 🟡 Partial (runs but threshold/advisory issues) | 3 (rows 1, 2, **3** ← DEC-MUTATION-GATE-RANKING-002 honesty re-classification: the math pass is zero-survivor + completing, but the broad orchestrator pass has NEVER finished inside the weekly job's 120-min budget since #590 — pre-existing, measured from the GitHub job logs, tracked as DEC-MUTATION-RUNTIME-001) | 14% | −1 (rows 8 + 11 closed; row 3 re-opened honestly) |
| 📋 Roadmap (not yet wired) | **1** (row 14) | **5%** | **−4** (rows 13, 15 were stale; row 5b closed by PYLINT-DROP; row 16 closed by DOCTOR) |
| 👁️ Manual (PR template + reviewer) | 3 (rows 10, 17, 18) | 14% | unchanged |

**Pre-audit numbers (kept for history):** 9 ✅ / 4 🟡 / 5 📋 / 3 👁️ → 43% / 19% / 24% / 14%.

**Post-audit reality (DEC-STANDARDS-AUDIT-001):** 11 🟢 / 4 🟡 / 3 📋 / 3 👁️ → 52% / 19% / 14% / 14%.

**Post-DEC-CROSSWALK-001 reality (2026-05-24):** 12 🟢 / 3 🟡 / 3 📋 / 3 👁️ → 57% / 14% / 14% / 14%.

**Post-DEC-PYLINT-DROP-001 reality (2026-05-24):** 13 🟢 / 3 🟡 / 2 📋 / 3 👁️ → 62% / 14% / 10% / 14%.

**Post-DEC-DOCTOR-001 reality (2026-05-24):** 14 🟢 / 3 🟡 / 1 📋 / 3 👁️ → 67% / 14% / 5% / 14%.

**Post-DEC-STRESS-001 reality (2026-05-24):** 15 🟢 / 2 🟡 / 1 📋 / 3 👁️ → **71% / 10% / 5% / 14%**.

**Post-DEC-MUTATION-GATE-RANKING-002 reality (2026-06-10):** 14 🟢 / 3 🟡 / 1 📋 / 3 👁️ → **67% / 14% / 5% / 14%** (row 3 re-classified honest 🟡: math pass zero-survivor + completing; the broad orchestrator pass's pre-existing weekly-job timeout + ≥26 early survivors DISCOVERED via the GitHub job logs and tracked as DEC-MUTATION-RUNTIME-001 — Golden Rule #10: a gate that never finishes is not "fully enforced").

**Post-DEC-MUTATION-RUNTIME-001 reality (2026-06-10):** counts unchanged at 14 🟢 / 3 🟡 / 1 📋 / 3 👁️, but row 3's 🟡 SHRANK: the Python mutation job is now measured at **951/951 mutants killed in ~35 min** (all 223 broad-pass survivors eliminated; per-module runners; the mutmut exit-2 import-crash blind spot closed at the runner level). The `compute.py` fold-in (#590 ratchet) EXECUTED by DEC-MUTATION-GATE-COMPUTE-001 (2026-06-12: measured 16 survivors + 3 untested → 0, the module joined the per-module loop). The row stays 🟡 only for the fno-math `cargo-mutants` residual — fix SHIPPED by DEC-MUTATION-RUNTIME-002 (2026-06-12: event-keyed concurrency + `--jobs 2`; verification = the next scheduled run).

**Post-DEC-COV-CLI-HARNESS-001 reality (2026-06-12):** **16 🟢 / 1 🟡 / 1 📋 / 3 👁️** → **76% / 5% / 5% / 14%** — rows 1+2 flipped 🟢: the coverage ratchet arc (90 → 99 → 100, DEC-COV-SMALL-GAPS-001 → DEC-COV-APP-BACKFILL-001 → DEC-COV-CLI-HARNESS-001) ended with the `[tool.coverage.run] omit` key DELETED and the whole `brutex/` package measured at exactly 100.00% line+branch (6738 tests, fail_under=100, exit 0). The single remaining 🟡 is row 3, pending only the DEC-MUTATION-RUNTIME-002 verification run (next scheduled Monday 04:00 UTC).

**The 1 genuinely remaining ROADMAP gap that DOES need code work:**
1. **Row 14 — Throughput canary 50 M/s absolute on CI** — ✅ the M4-Pro self-hosted runner (`Parthibans-MacBook-Pro`) is REGISTERED + CERTIFIED; `bench-m4pro.yml` runs the absolute 50 M/s floor daily/push/dispatch and it PASSES (MIGRATION #655; run #109, 2026-06-14 — mask-AND ≈ 2.0 B ops/s = 40× the floor). Remaining lift: Phase 3 PyO3 kernel. NOT a per-PR blocker by design (DEC-M4PRO-PR-TRIGGER-001).

**~~Row 5b — pylint standalone~~** — ✅ CLOSED by DEC-PYLINT-DROP-001 (2026-05-24). ruff `PL` subset in `pyproject.toml [tool.ruff.lint].select` covers pylint-equivalent rules; standalone pylint dropped. See MIGRATION.md entry #207.

**~~Row 16 — `brutex doctor` CLI~~** — ✅ CLOSED by DEC-DOCTOR-001 (2026-05-24). `brutex/doctor.py` + `brutex doctor` subcommand + 18 unit tests in `tests/unit/test_doctor.py`. See MIGRATION.md entry #209.

**The 1 PARTIAL item remaining** (rows 1/2 closed by DEC-COV-CLI-HARNESS-001 2026-06-12; the prior 3-count's coverage items are done):
1. Row 3 — Mutation: fno-math `cargo-mutants` fix SHIPPED-pending-verification (DEC-MUTATION-RUNTIME-002: cancellation starvation + measured 120-min serial overrun, both fixed); the Python side is fully zero-survivor — 951/951 killed (DEC-MUTATION-RUNTIME-001) + the `compute.py` fold-in executed (DEC-MUTATION-GATE-COMPUTE-001, #590 ratchet closed)

**~~Row 8 — Spec↔test crosswalk warn-and-continue → fail-closed~~** — ✅ CLOSED by DEC-CROSSWALK-001 (2026-05-24). Row 8 is now 🟢. See MIGRATION.md entry #206.

**~~Row 11 — Stress test job missing~~** — ✅ CLOSED by DEC-STRESS-001 (2026-05-24). `tests/stress/test_audit_chain_sustained.py` runs nightly via `.github/workflows/stress-nightly.yml`; pytest `stress` marker registered. Row 11 is now 🟢. **CORRECTED (DEC-SOAK-REAL-TIER-001, MIGRATION.md entry #1535, 2026-07-06):** the old "24h soak reachable via workflow_dispatch with `STRESS_EVENTS=10_000_000`" claim was FALSE (the 60-min ubuntu job timeout hard-kills a 10M run at ~1/17th done — its guard now REFUSES `STRESS_EVENTS > 200000`); the real multi-hour soak runs on `.github/workflows/soak-selfhosted.yml` (self-hosted M4-Pro, duration-derived timeout). See MIGRATION.md entries #208 + #1535.

**The 3 MANUAL-REVIEWER items** cannot be automated (Golden Rule #1 hallucination check, bug-fix regression discipline, decision-ID enforcement); PR template + reviewer is the correct control.

**Arithmetic check (per Agent 4 of DEC-P0-004 hostile-review pass):**
9 + 4 + 5 + 3 = **21 sub-rows** ✅ (the table has 21 sub-rows total;
row 4 has 2 sub-items mypy+pyright; row 5 has ruff+pylint; row 7 has
jscpd+vulture; row 16 is roadmap only, not also Manual — previous double-
counting fixed).

**Note on counts (post operator's no-downgrade decree DEC-P0-003):**
the table has 21 sub-rows (some requirement-numbers have 2 sub-items, e.g.
row 4 covers mypy + pyright; row 5 covers ruff + pylint; row 7 covers jscpd
+ vulture). The summary above counts SUB-ROWS. By unique row-numbers: 5 of
18 requirements move from 🟡/🔴 to ✅ in this PR sequence (rows 3 [mutation],
4-pyright, 6, 7-jscpd, 7-vulture). **The mutation 85% gate is now a HARD
gate; if CI fails, the answer is more tests, not a lower threshold.**

**Per Golden Rule #10 (honest limits):** of the 18 standing requirements,
**4 are real, 8 partial, 4 missing, 3 inherently manual.** The closing
path is documented per row. Phase-by-phase patches will move items
from 🟡/📋 to ✅.

---

## 2. Observability — non-negotiable on every PR

Per `docs/track2/TRACK2_OBSERVABILITY.md` (DEC-OBS-001), every PR
must preserve the 4 pillars:

| Pillar | Mechanism | SLA | Current enforcement |
|---|---|---|---|
| **Tracking** | Audit JSONL hash-chained | every material event recorded within 100 ms | ✅ implementation in `brutex/orchestrator/audit.py`; ✅ `brutex doctor` CLI LIVE (DEC-DOCTOR-001, 2026-05-24); ✅ `brutex monitor` CLI LIVE (DEC-MONITOR-LOOP-001 entry #242 + DEC-STANDARD-MONITOR-001 entry #243, 2026-05-25) — composes Telegram + SLA + Watchdog + L3 replay subscribers via `compose_standard_monitor` against env-driven `MonitorConfig` |
| **Logging** | `structlog` only (no `print`) | uniform structured fields | ✅ banned-patterns scanner enforces (narrow scope; see #5 below) |
| **Dashboard** | FastAPI on `127.0.0.1:9080` | accurate within 5 sec | ✅ 10+ dedicated `--cov=brutex.dashboard.*` 100% line+branch gates run in `.github/workflows/ci.yml` (the prior "excluded from coverage" claim was stale — corrected by DEC-SPEC-TOPK-RECONCILE-001's honesty sweep) |
| **Alerting** | Telegram + dashboard + email digest + macOS fallback | CRITICAL alert within 60 sec | 🟡 PARTIAL — Telegram path WIRED end-to-end (DEC-TELEGRAM-WIRING-001 entry #238 + DEC-TELEGRAM-WIRING-VERIFICATION-001 entry #247): `compose_standard_monitor` routes SLA breach / Watchdog heartbeat-stale / L3 replay-inconsistency events into `_publish_telegram` → `TelegramNotifier.publish`; end-to-end proven by `tests/integration/test_compose_monitor_sla_to_telegram.py`. CI runs the test on every push (verifies the WIRING; not the bot reachability — that requires `BRUTEX_TELEGRAM_BOT_TOKEN` env var operator-set). Pipeline-event subscriber (`make_pipeline_event_subscriber`) exists but legitimately dormant until Phase 2 `PhaseRunner` drivers land. |

Plus `brutex doctor` daily 07:30 IST — **CLI BUILT** (DEC-DOCTOR-001, 2026-05-24) and **CRON LIVE** (DEC-DOCTOR-DAILY-CRON-001, 2026-07-06): `.github/workflows/doctor-daily.yml`, 02:00 UTC = 07:30 IST daily + dispatch; alert-on-red via one deduped auto-closing tracking issue + best-effort Telegram.

---

## 3. Deep-research protocol — major decisions

Per Golden Rule #9:

| Decision class | Minimum agents | Pattern |
|---|---:|---|
| Major architecture change | 4 | Plan + general + general + Explore (parallel hostile + consistency) |
| Methodology choice | 4 | 4-agent literature audit incl. 2024-2026 papers |
| Cost-model change | 4 | rates / contract notes / rounding / edge cases |
| Risk / compliance change | 2 | SEBI/RBI/IT-Act primary sources |
| Bug-fix or refactor | 1 | hostile review by Plan agent |
| Doc-only edit | 0 | direct edit OK; must log to MIGRATION.md |

Decision IDs in MIGRATION.md are MANDATORY for any locked decision.

---

## 4. Architecture mandate (COMMON · DYNAMIC · SCALABLE · AUTOMATED)

Per Golden Rule #11. Every module obeys all 5:

| Principle | Definition | Current enforcement |
|---|---|---|
| **COMPREHENSIVE** | streaming + batch interfaces; bit-exact oracle test; look-ahead invariant test; serialize/restore | ✅ pattern in `brutex/indicators/ema.py` etc.; 📋 not yet a property-test family |
| **COMMON** | one source of truth (this repo); every utility lives in exactly one place; downstream modules import; no copy-paste between modules | 👁️ enforced by review |
| **DYNAMIC** | add/remove indicator/TF/condition without re-architecting | ✅ documented in DEC-PLUGIN-001 + DEC-PLUGIN-002 |
| **SCALABLE** | same code path Day 1 → Phase 10; O(1) everywhere | 🟡 bench gates partial (no ratio assertion yet) |
| **AUTOMATED** | 100% automated after one-time bootstrap | 🟡 Release Please ✅, auto-merge ✅, Watchdog ✅ LIVE (DEC-WATCHDOG-001 entry #240 + DEC-STANDARD-MONITOR-001 entry #243), SLA detector ✅ LIVE (DEC-SLA-DETECTOR-001 entry #239 + entry #243), L3 replay oracle ✅ LIVE (DEC-L3-REPLAY-ORACLE-001 entry #241 + entry #243), Telegram wiring ✅ LIVE (DEC-TELEGRAM-WIRING-001 entry #238 + DEC-TELEGRAM-WIRING-VERIFICATION-001 entry #247), Auto-resume ❌ explicitly rejected by DEC-P0-029 — remaining gaps: pipeline-event subscriber dormant until Phase 2; Telegram bot reachability proof tracked as P2 (doctor-daily cron ✅ DEC-DOCTOR-DAILY-CRON-001) |

---

## 5. PR gate matrix (REAL current state)

| Gate | Tool | Decree threshold | Current threshold | Blocking? |
|---|---|---|---|---|
| Unit tests | pytest | 100% pass | 100% pass enforced via `ci.yml::test`; **103 modules at strict per-module 100%** (verified 2026-06-02 per DEC-WORKSPACE-VERIFY-001 #398 D3 / DEC-DOC-DRIFT-RECONCILE-001; was "28" — stale) | ✅ blocks |
| Coverage line | pytest-cov | 100% | 90% global floor + **103 strict-100% modules**; per-module gate enforced in CI (DEC-COV-001) | 🟡 90% blocks globally; 100% strict on 103 modules |
| Coverage branch | pytest-cov --cov-branch | 100% | `--cov-branch` flag ON; 90% global floor; same 103 strict-100% per-module gate enforces branch=100% on those modules (DEC-COV-001) | 🟡 90% blocks globally; 100% strict on 103 modules |
| Mutation | mutmut | ≥ 85% | **`\|\| true` REMOVED** (DEC-P0-001 entry #139 + DEC-P0-003 entry #141 restored from interim DEC-P0-002 downgrade); 85% gate enforced on `brutex/orchestrator/state.py` via `mutmut junitxml` parser; `mutation.yml` runs weekly main-only for broader mutation coverage | ✅ blocks |
| Type Python | mypy --strict | 0 errors | ✅ enforced (DEC-P0-001 cleared pre-existing failures; commit d57efaf fixed 4 indicator generics errors) | ✅ blocks |
| Type supplementary | pyright | 0 errors | `typeCheckingMode = "strict"` (DEC-P0-001 entry #139 bump from "standard"; 3 narrow strict-only silences + 6 per-test fixtures silences per DEC-P0-004 entry #142) | ✅ blocks |
| Lint | ruff check + format | 10/10 | enforced; DEC-P0-001 entry #139 cleared 29 legacy errors + applied `ruff format` to 19 files; `pyproject.toml [tool.ruff.lint].select` includes `"PL"` subset replacing standalone pylint (DEC-PYLINT-DROP-001 entry #207) | ✅ blocks |
| Pylint | ruff `PL` subset | pylint-equivalent rules | **ruff `PL` subset ENFORCED** (DEC-PYLINT-DROP-001 entry #207, 2026-05-24); standalone pylint dropped — ruff's Rust-based PL is faster + same rule coverage; 7 PL rules ignored with documented rationale | 🟢 blocks via ruff |
| Security | bandit | 0 high/critical | **bandit `-l`** (low+ — stricter than `-ll`); DEC-P0-001 entry #139 un-ignored S104/S105/S108/S311 from project-wide ruff `ignore` (S110 narrowed to `tests/`); per-line noqa rationale at 4 brutex/ sites | ✅ blocks |
| Duplication | jscpd | < 1% | **`\|\| true` REMOVED** (DEC-P0-001 entry #139); jscpd narrowed to `brutex/` only at 3% threshold; tests/ legitimately repeats fixture patterns | ✅ blocks (3% threshold; tighten to 1% tracked as follow-up) |
| Dead code | vulture | 0 hits | **`\|\| true` REMOVED** (DEC-P0-001 entry #139); whitelist at `scripts/ci/vulture_whitelist.py` covers context-manager protocol, `__post_init__`, `audit.verify_chain` | ✅ blocks |
| Benchmarks | pytest-benchmark | p99 ratio ≤ 1.1 | **Ratio gates LIVE** (DEC-STANDARDS-AUDIT-001 + DEC-P0-020 entry #158); `test_store_o1.py::test_get_ratio_below_3x` gates store lookup at 3×; `test_throughput_canary.py::test_vectorized_mask_and_ratio` gates numpy mask AND at 8×; `test_dict_lookup_ratio` gates dict lookup at 10×; per-CI ratio looser than 1.1× target per shared-GHA flake-mitigation in DEC-P0-020 | ✅ blocks (ratio gates; 1.1× requires M4 Pro runner per DEC-M4PRO-ABSOLUTE-001 entry #236) |
| Property tests | hypothesis | 0 falsifications | runs via `tests/property/` with `-x`; **catalog test stub** of all 24 invariants per DEC-PHASE1-INVARIANT-CATALOG-001 entry #250 (5 IMPLEMENTED + 19 ROADMAP via `pytest.skip`); CI runs `Property tests (hypothesis)` job on every push | ✅ blocks |
| Golden fixtures | pytest -m golden | 100% byte-equal | `tests/golden/` runs on every push; `test_bloom_golden.py` working + `condition_truth_table.csv` 5-row skeleton (DEC-PHASE1-WEEK2-SCAFFOLDING-001 entry #252; expands to ≥200 rows in Phase 2) | ✅ blocks |
| Audit chain | `brutex doctor` | green | **`brutex doctor` CLI LIVE** (DEC-DOCTOR-001 entry #209, 2026-05-24); `--audit-file` + `--audit-dir` + `--runtime-file` + `--live-subjects-file` flags; runs `verify_chain` + runtime parseability + L3 dedup-replay consistency check (DEC-L3-REPLAY-ORACLE-001 entry #241) | 🟢 CLI + 07:30 IST cron LIVE (doctor-daily.yml, DEC-DOCTOR-DAILY-CRON-001 #1525) |
| Conventional commit | scripts/ci/check-conventional-commits.sh | passes | enforced via `Conventional commit format` CI job (Lint group); rejects non-`feat:` / `fix:` / `docs:` / `chore:` / `test:` / etc. prefixes | ✅ blocks |
| MIGRATION.md entry | reviewer + validator | every locked decision | PR template line + **`scripts/ci/validate_dec_ids.py`** (DEC-DEC-ID-VALIDATOR-001 entry #214) rejects PRs citing non-existent DEC-IDs; reviewer judges "should this PR have cited a DEC-ID" | 👁️ + ✅ partial |
| Spec ↔ test crosswalk | scripts/ci/spec_test_crosswalk.py | all sections matched | **fail-closed** (DEC-CROSSWALK-001 entry #206, 2026-05-24); env-var `CROSSWALK_ALLOW_STALE=1` for local script-iteration only — CI never sets it | ✅ blocks |
| Banned patterns | scripts/ci/scan-banned-patterns.sh | clean | runs on every push; narrow scope (e.g. pandas only checked in `brutex/math/`) — broadening tracked as follow-up | ✅ blocks (narrow scope) |
| Bug-fix regression | scripts/ci/validate_bugfix_regression.py | `fix:` PR must touch tests/ | **partial automation** (DEC-BUGFIX-REGRESSION-001 entry #213, 2026-05-24); matches PR title against `fix:`/`bugfix:`/`hotfix:` prefixes + body keywords; reviewer judges "is this the RIGHT regression test" | 👁️ + ✅ partial |
| No-hallucination citation scan | scripts/ci/validate_no_hallucination.py | external claims must cite source | **partial automation** (DEC-HALLUCINATION-SCAN-001 entry #211, 2026-05-24); scans PR body for external-system keywords + nearby citation markers (URLs, DEC-FOO-NNN, MIGRATION.md entry #NNN, CLAUDE.md §N, verified YYYY-MM-DD) | 👁️ + ✅ partial |
| Z+ PR-body marker check | scripts/ci/zplus-pr-guard.sh | 6 marker phrases present | **FAIL-CLOSED** per DEC-P0-014 entry #152; markers: `Z+ 15-row matrix`, `Z+ 7-row resilience matrix`, `Z+ 7-layer audit`, `4-agent hostile review`, `Honest envelope`, `Decision ID` | ✅ blocks |
| Stress (~5-min regression gate) | pytest -m stress | invariants hold under load | **`stress-nightly.yml`** runs `tests/stress/test_audit_chain_sustained.py` nightly 03:00 UTC + workflow_dispatch (ubuntu-24.04); guard REFUSES `STRESS_EVENTS > 200000` (DEC-STRESS-001 #208, corrected DEC-SOAK-REAL-TIER-001 #1535) | 🟡 nightly only (not per-PR) |
| Real multi-hour soak (drift-gated) | pytest -m 'stress or soak' | RSS-ceiling / fd-growth / chain drift flat | **`soak-selfhosted.yml`** runs `tests/stress/test_soak_sustained.py` on the self-hosted M4-Pro (weekly cron + `workflow_dispatch(soak_minutes)`); duration-DERIVED `timeout-minutes`; `SoakDriftMonitor` → real-time `SweepAlerter.soak_drift` (DEC-SOAK-REAL-TIER-001 #1535) | 🟡 weekly self-hosted (not per-PR) |

---

## 6. Per-task / per-sub-task discipline

Every task follows this sequence:

| Phase | Action | Output |
|---|---|---|
| 1. Plan | TodoWrite breakdown | tracked todo list |
| 2. Research | If decision needed → 4-agent or 2-agent | findings + primary sources |
| 3. Spec | Write or update doc with decision ID | MIGRATION.md entry |
| 4. Code | Implement against spec | tests pass on first commit |
| 5. Test | Unit + property + golden + bench | all enforced gates green |
| 6. Audit | Append audit row | hash-chain extended |
| 7. Commit | Conventional commit msg + signed | passes commit-checker |
| 8. Push | `git push -u origin <branch>` | remote updated |
| 9. PR | Update PR description with REAL gate status | reviewers notified |

---

## 7. Documentation discipline

| Doc type | Required sections |
|---|---|
| Spec (`*_SPEC.md`) | Status, Scope, Out-of-scope, Algorithm, API surface, Test fixtures, UNVERIFIED items, Sources, Amendment log |
| Plan (`*_PLAN.md`) | Status, Phases, Per-phase deliverables, Dependencies, Risks |
| Verified (`*_VERIFIED.md`) | Verification provenance, Locked values, Citations per value, UNVERIFIED items |
| Operational (`*_GUARANTEES.md`, `*_OBSERVABILITY.md`) | Guarantees, SLAs (REAL vs ROADMAP), Property invariants, Failure modes |
| Migration (`MIGRATION.md`) | Decision ID, Date, Rationale, Source citation, Files touched |
| Standards (this doc) | **HONESTLY distinguish ENFORCED from ROADMAP per Golden Rule #10** |

---

## 8. Real-time guarantees — REAL vs ROADMAP

| Guarantee | SLA target | Current state | Status |
|---|---|---|---|
| Every material action audited | within 100 ms | implementation in `brutex/orchestrator/audit.py` (DEC-P0-006); no CI test of latency itself, but audit-chain property test asserts append correctness on every push | 🟡 |
| Hash chain intact | always | property test in `tests/property/test_audit_chain_properties.py` (P-16, P-17) + **`brutex doctor` CLI LIVE** (DEC-DOCTOR-001 entry #209) runs `verify_chain` on demand | 🟢 |
| CRITICAL alert reaches operator | within 60 sec | **Telegram wiring LIVE** (DEC-TELEGRAM-WIRING-001 entry #238 + DEC-TELEGRAM-WIRING-VERIFICATION-001 entry #247); `compose_standard_monitor::_publish_telegram` fires on SLA breach / Watchdog stale / L3 replay-inconsistency events; end-to-end proven by `tests/integration/test_compose_monitor_sla_to_telegram.py`. Operator-side: needs `BRUTEX_TELEGRAM_BOT_TOKEN` env var for bot reachability | 🟡 (wired; bot reachability operator-side) |
| Dashboard accurate | within 5 sec | `brutex/dashboard/app.py:659` — 500ms WebSocket + 1s HTTP fallback; dashboard module currently excluded from coverage — needs Phase 3 real test | 🟡 |
| `brutex doctor` runs every workday | 07:30 IST cron | CLI LIVE (DEC-DOCTOR-001 entry #209); `doctor-daily.yml` cron LIVE at 07:30 IST on a hosted runner (DEC-DOCTOR-DAILY-CRON-001 entry #1525 — envelope checks SKIP-with-reason, contract-asserted; wheel handshakes are the substantive verdicts) + production-envelope doctor daily on the M4 Pro (`verify-m4pro.yml`, 17:30 IST). "Alert if missed" NOT yet detected — named follow-up. | 🟢 |
| Backtest reproducible | bit-for-bit | **L3 replay oracle LIVE** (DEC-L3-REPLAY-ORACLE-001 entry #241); `brutex/orchestrator/replay.py::assert_dedup_consistent` re-projects audit log → subjects → diffs vs LMDB; wired into `brutex doctor --live-subjects-file` and into `MonitorLoop` via `compose_standard_monitor` | 🟢 (L3 oracle wired; full Parquet bit-equality test is Phase 2/3) |
| No data loss on crash | always | audit chain property tests assert append correctness; **adaptive rate limiter** (DEC-ADAPTIVE-RATE-LIMITER-001 entry #249) provides backpressure under load. Crash-recovery property test is Phase 2 | 🟡 |
| O(1) per hot path | p99(1M)/p99(1K) ≤ 1.1 | **Ratio gates LIVE** in `ci.yml::bench` (DEC-STANDARDS-AUDIT-001 + DEC-P0-020 entry #158); 5 of 6 hot paths gated at 3-10× ratio per shared-GHA flake-tolerance; absolute 1.1× target requires M4 Pro runner (DEC-M4PRO-ABSOLUTE-001 entry #236) | 🟢 (ratio gates live; strict M4-Pro tier ACTIVE on the registered runner — MIGRATION #655) |
| Combo dedup unique | always | implementation in `brutex/orchestrator/store.py` + `brutex/strategies/combo.py`; **uniqueness property tests** (DEC-COMBO-HASH-UNIQUENESS-001) live in `tests/property/test_combo_hash_uniqueness.py` + `test_dedup_hash_properties.py` — 9-field BLAKE3 + distinct-combo→distinct-hash | 🟢 |
| Hung-worker watchdog | within 5 min | **Watchdog LIVE** (DEC-WATCHDOG-001 entry #240); `brutex/observability/watchdog.py::Watchdog` + `make_heartbeat_subscriber` for `PhaseRunner` + `on_hung` Telegram routing via `compose_standard_monitor` (entry #243). Phase 2 workers activate via `make_heartbeat_subscriber` | 🟢 |
| SLA breach detection | rule-driven NORMAL↔BREACHED | **SLA detector LIVE** (DEC-SLA-DETECTOR-001 entry #239); `brutex/observability/sla.py::SLADetector` state-machine + `on_breach` / `on_recovery` callbacks; runs under `brutex monitor` per env-driven `MonitorConfig(sla_rules=...)` | 🟢 |
| No hallucination | every claim cited | reviewer-enforced via PR template + **`scripts/ci/validate_no_hallucination.py`** (DEC-HALLUCINATION-SCAN-001 entry #211) partial automation | 👁️ + ✅ partial |

**Post-2026-05-27 reconciliation (DEC-AUDIT-RECONCILE-003):** **7 of 12 guarantees** are now 🟢 fully wired (was 4 of 10); **4 are 🟡 partial** (wired but with operator-side / Phase 2 / cron dependencies); **0 are 📋 unimplemented** (was 6); 1 is 👁️ + ✅ partial. Honest accounting per Golden Rule #10 — every ratchet from 📋 to 🟡/🟢 cites the closing DEC-ID.

---

## 9. Plain-English mandate (the auto-driver test)

Every spec / decision / output passes the auto-driver test:

> "Auto-bhaiya, kya samjha?" — if the operator's auto-driver can repeat
> back the core idea after one paragraph, the doc is clear enough.

Use tables over prose. Cross-references over duplication.

---

## 10. Operator promise — what's REAL today vs ROADMAP

By honoring this standards document on every PR, the operator gets:

**REAL today (15 enforced gates blocking merge, post DEC-AUDIT-RECONCILE-003 entry #255):**
1. `mypy --strict` (0 errors required; currently clean)
2. **pyright strict** (0 errors on `brutex/`; `typeCheckingMode = "strict"` per DEC-P0-001 entry #139)
3. `ruff check + format` (29 legacy errors cleared in DEC-P0-001; ruff `PL` subset replaces standalone pylint per DEC-PYLINT-DROP-001 entry #207)
4. Unit tests (100% pass + ≥ 90% global coverage + **103 modules at strict per-module 100%** — verified 2026-06-02 per DEC-WORKSPACE-VERIFY-001 #398 D3 / DEC-DOC-DRIFT-RECONCILE-001; was "28", stale)
5. **Mutation 85% gate** on `brutex/orchestrator/state.py` — DEC-P0-001 + DEC-P0-003 entry #141 (operator no-downgrade restored from DEC-P0-002 60% interim)
6. **bandit `-l`** (low+ — stricter than `-ll`); DEC-P0-001 un-ignored S104/S105/S108/S110/S311
7. **jscpd code-duplication 3% gate** on `brutex/` — DEC-P0-001 (`|| true` removed)
8. **Vulture dead-code 80%-confidence gate** with minimal whitelist — DEC-P0-001 (`|| true` removed)
9. **Golden fixtures** + interrogate docstring 100% + 5-row condition truth table skeleton (DEC-PHASE1-WEEK2-SCAFFOLDING-001 entry #252)
10. **Z+ PR-body marker check FAIL-CLOSED** (`scripts/ci/zplus-pr-guard.sh` per DEC-P0-014 entry #152) — 6 marker phrases required
11. **Spec ↔ test crosswalk FAIL-CLOSED** (DEC-CROSSWALK-001 entry #206)
12. **O(1) ratio gates** on 5 hot paths (DEC-STANDARDS-AUDIT-001 + DEC-P0-020 entry #158) — store lookup, mask AND, dict lookup, throughput canary, rate limiter
13. **Combo-hash uniqueness property tests** (DEC-COMBO-HASH-UNIQUENESS-001) — 9-field BLAKE3 + distinct-combo→distinct-hash
14. **Bug-fix regression scan** (DEC-BUGFIX-REGRESSION-001 entry #213) — `fix:` PRs must touch `tests/`
15. **No-hallucination citation scan** (DEC-HALLUCINATION-SCAN-001 entry #211) + **DEC-ID validator** (DEC-DEC-ID-VALIDATOR-001 entry #214) — external claims must cite source; cited DEC-IDs must exist

**Plus (LIVE but not strictly merge-blocking):**
- **`brutex doctor` CLI LIVE** (DEC-DOCTOR-001 entry #209) — `--audit-file` + `--audit-dir` + `--runtime-file` + `--live-subjects-file` flags
- **`brutex monitor` CLI LIVE** (DEC-MONITOR-LOOP-001 entry #242 + DEC-STANDARD-MONITOR-001 entry #243) — composes Telegram + SLA + Watchdog + L3 replay
- **L3 audit-replay oracle LIVE** (DEC-L3-REPLAY-ORACLE-001 entry #241)
- **Watchdog LIVE** (DEC-WATCHDOG-001 entry #240) — hung-worker detector
- **SLA detector LIVE** (DEC-SLA-DETECTOR-001 entry #239) — rule-driven NORMAL↔BREACHED
- **Telegram wiring LIVE end-to-end** (DEC-TELEGRAM-WIRING-001 entry #238 + DEC-TELEGRAM-WIRING-VERIFICATION-001 entry #247)
- **Adaptive rate limiter LIVE** (DEC-ADAPTIVE-RATE-LIMITER-001 entry #249) — generic token-bucket primitive, **re-pinned to Dhan's verified `[3..5]` r/s** in `brutex/adapters/dhan/rate_control.py` (the binding runtime band; the legacy `[4..8]` here was Groww-era prose, corrected per DEC-WORKSPACE-VERIFY-001 entry #398 / DEC-DOC-DRIFT-RECONCILE-001)
- **24-invariant catalog** (DEC-PHASE1-INVARIANT-CATALOG-001 entry #250) — 5 IMPLEMENTED + 19 ROADMAP via skip
- **4-agent hostile review runbook** (DEC-PHASE1-WEEK3-RUNBOOK-001 entry #253) — `docs/runbooks/4-agent-review.md`
- **Empty-pyramid baseline doc** (DEC-PHASE1-WEEK3-RUNBOOK-001 entry #253) — `docs/PHASE1_EMPTY_PYRAMID_BASELINE.md`
- **Nightly workflows** (DEC-STRESS-001 entry #208): `stress-nightly.yml`, `redteam-nightly.yml`, `dep-freshness-nightly.yml`
- **Weekly mutation** `mutation.yml` runs cargo-mutants per `.cargo/mutants.toml` (DEC-PHASE1-WEEK2-SCAFFOLDING-001 entry #252)

**ROADMAP (closing path remaining):**
- Full 100% coverage line + branch globally (currently 90% floor + **140** strict-100% modules; the remaining below-strict set is the CLI layer, covered via global `--cov=brutex` — the formerly ABI3-blocked orchestrator modules are ALL strict-gated as of DEC-ORCH-STRICT-GATES-001 #613; "103 + 8 blocked" was the pre-#589/#613 state)
- M4 Pro absolute throughput gate ≥ 50 M/s (DEC-M4PRO-ABSOLUTE-001 entry #236) — ✅ ACTIVE: runner registered + certified (MIGRATION #655); `bench-m4pro.yml` runs + passes it daily/push/dispatch (run #109, 2026-06-14)
- `.github/workflows/doctor-daily.yml` cron — CLI exists, cron wrapper is P2 follow-up — ✅ SHIPPED (DEC-DOCTOR-DAILY-CRON-001 entry #1525)
- Server-side GitHub branch protection per `docs/runbooks/github-branch-protection-setup.md` (operator-applied)
- Re-promote 6 demoted pyright report-codes from per-test "warning" → "error" by fixing each pattern in source (P1, tracked in DEC-P0-004)
- Rewrite `TestReadAPI` + `TestInstrumentSnapshot` with IDX-only fixtures (currently xfail per DEC-P0-004)
- Tighten jscpd threshold from 3% → 1% (follow-up)
- Widen banned-patterns scanner scope beyond `brutex/math/` (follow-up)

**MANUAL (reviewer-enforced, cannot be automated):**
- No-hallucination correctness (Golden Rule #1) — citation PRESENCE auto-checked, CORRECTNESS reviewer
- Decision-ID appropriateness (Golden Rule #18) — existence auto-checked, "should this PR have cited a DEC-ID" reviewer
- Bug-fix regression-test QUALITY — `fix:` PR touching tests/ auto-checked, "is this the RIGHT test" reviewer
- 4-agent hostile review CONTENT quality — marker presence auto-checked (Z+ 6-marker guard), review depth reviewer

Per Golden Rule #10 (honest limits): this doc honestly reflects
reality at 2026-05-27. Earlier draft was 62% overstated per 6-agent
forensic audit 2026-05-16. Closing path is per-row above; no claim
made without primary DEC-ID evidence.

---

## 11. Closing path priority (post 6-agent audit)

| Priority | Action | Effort | Status |
|---|---|---|---|
| P0 | Remove `\|\| true` from mutation-py, jscpd, vulture in ci.yml | 30 min | ✅ DONE (DEC-P0-001, 2026-05-17) |
| P0 | Bump pyright `typeCheckingMode = "strict"`; close 11-category silencing | 1 hr | ✅ DONE (DEC-P0-001) — strict mode with 3 additional narrow silences for strict-only checks |
| P0 | Fix the 4 pre-existing mypy generics errors in `indicators/ema.py` + `day_extremes.py` | 30 min | ✅ DONE (commit d57efaf, pre-DEC-P0-001) |
| P0 | Fix the 29 ruff errors + 19 format violations on current tree | 1 hr | ✅ DONE (DEC-P0-001) — 9 PLR2004 + 7 S101 + 3 ERA001 + 2 SIM110 + 2 RUF046 + 2 RUF022 + 2 N812 + 1 SIM108 + 1 E741 all fixed; ruff format applied to 19 files |
| P0 | Tighten bandit + un-ignore ruff S-rules with per-line justifications | 1 hr | ✅ DONE (DEC-P0-001) — **bandit kept at `-l` (low+ — stricter than `-ll`)** per Agent 2 hostile-review finding; S104/S105/S108/S311 removed from project ignore (S110 narrowed to `tests/` per-file-ignores); per-line noqa at 4 `brutex/` sites + `tests/**` ignores expanded for S104/S108/S311/S603/S607 (35 legitimate test patterns) |
| P0 | Install [data] extras in CI test job (fixes 6 collection errors) | 15 min | ✅ DONE (DEC-P0-001) — `pip install -e ".[dev,data]"` in test job |
| P0 | Add mutation score ≥ 85% gate (parse `mutmut junitxml`) | 30 min | ✅ DONE (DEC-P0-001) — XML-parsing block enforces 85% floor; fails CI on regression |
| P0 | Add `scripts/ci/vulture_whitelist.py` for legitimate false positives | 15 min | ✅ DONE (DEC-P0-001) — covers `__exit__` args, FastAPI handlers, plugin protocol stubs |
| P1 | Write benchmark baseline JSON + `--benchmark-compare --fail` gate | 2 hr | 🟢 (dormant-until-runner) — repo-owned absolute baseline JSON + compare gate SHIPPED (DEC-BENCH-ABS-BASELINE-001 entry #1534; deliberate ledgered deviation from the literal `--benchmark-compare` flag — the repo-owned comparator abstains/governs, native compare cannot; precedent entry #158 DEC-P0-020): committed per-hardware baselines at `tests/bench/baselines/<hardware_key>.json`, measure→compare→capture→bless on `bench-m4pro.yml`, schema `validate` + no-silent-re-baseline `govern` enforced on every PR TODAY via the required `bench` job. The absolute compare is ACTIVE only once the m4pro runner is online AND the first measured bless PR merges (the bootstrap ships ZERO numbers — fabrication-free); until then it ABSTAINS green-but-loud. NOT claimed ENFORCED-as-merge-blocker: bench-m4pro stays non-required/non-PR (DEC-M4PRO-PR-TRIGGER-001) |
| P1 | Build minimal `brutex doctor` CLI with C2 (audit-chain) check | 4 hr | ✅ DONE (DEC-DOCTOR-001 entry #209, 2026-05-24) — `--audit-file` + `--audit-dir` + `--runtime-file` + `--live-subjects-file` flags |
| P1 | Write P-DEDUP-1, P-AUDIT-1 property tests | 4 hr | ✅ DONE — P-DEDUP-1 covered by `tests/property/test_combo_hash_uniqueness.py` + `test_dedup_hash_properties.py` (DEC-COMBO-HASH-UNIQUENESS-001); P-AUDIT-1 covered by `tests/property/test_audit_chain_properties.py` (P-16, P-17 in DEC-PHASE1-INVARIANT-CATALOG-001 entry #250) |
| P1 | Widen banned-patterns scanner scope (not just `brutex/math/`) | 1 hr | 📋 still tracked (out of scope for DEC-AUDIT-RECONCILE-003) |
| P2 | Per-module 100% coverage gate matrix (iterate every `brutex/` module) | 8 hr | 🟢 PARTIAL — **103** modules at strict-100% (verified 2026-06-02 / DEC-DOC-DRIFT-RECONCILE-001; "28" was stale); remaining below-strict = CLI layer + 8 ABI3-blocked orchestrator modules — multi-PR ratchet ongoing |
| P2 | Install + wire pylint OR remove the claim | 1 hr | ✅ DONE (DEC-PYLINT-DROP-001 entry #207, 2026-05-24) — replaced by ruff `PL` subset; standalone pylint dropped |
| P2 | Add stress test job to ci.yml | 4 hr | ✅ DONE (DEC-STRESS-001 entry #208, 2026-05-24) — `tests/stress/test_audit_chain_sustained.py` runs nightly via `stress-nightly.yml` (~5-min ubuntu regression gate, guard refuses `STRESS_EVENTS > 200000`); the real multi-hour soak runs on `soak-selfhosted.yml` (self-hosted M4-Pro, duration-derived timeout — DEC-SOAK-REAL-TIER-001 entry #1535, corrects the retracted `STRESS_EVENTS=10_000_000` route) |
| P2 | Wire `.github/workflows/doctor-daily.yml` 07:30 IST cron | 1 hr | ✅ DONE (DEC-DOCTOR-DAILY-CRON-001 entry #1525, 2026-07-06) — hosted 02:00 UTC = 07:30 IST cron + dispatch + push-smoke; envelope-honest (5 checks asserted SKIP-with-reason, 4 wheel handshakes asserted PASS — the 2 freshness + the 2 DEC-RUST-SOURCE-DIGEST-TWIN-001 source-digest checks, classified by DEC-DOCTOR-DAILY-CHECKSET-9-001 entry #1526 after the live-proof dispatch run red-flagged the mid-flight check-set growth exactly as designed); red → ONE deduped tracking issue (auto-closed on green) + best-effort Telegram; production-envelope doctor remains verify-m4pro.yml (DEC-M4PRO-VERIFY-001) |
| P2 | Observability stack: Watchdog + SLA + L3 replay + Telegram wiring | 16 hr | ✅ DONE — 5 decisions cumulatively: DEC-TELEGRAM-WIRING-001 entry #238 + DEC-SLA-DETECTOR-001 entry #239 + DEC-WATCHDOG-001 entry #240 + DEC-L3-REPLAY-ORACLE-001 entry #241 + DEC-STANDARD-MONITOR-001 entry #243 — all 4 detectors wired into `brutex monitor` via `compose_standard_monitor`; end-to-end verified by DEC-TELEGRAM-WIRING-VERIFICATION-001 entry #247 |
| P2 | Adaptive rate limiter for Dhan | 4 hr | ✅ DONE (DEC-ADAPTIVE-RATE-LIMITER-001 entry #249) — generic token-bucket pure logic, **re-pinned to Dhan's verified `[3..5]` r/s** in `dhan/rate_control.py` (the `[4..8]` here was Groww-era; corrected per DEC-DOC-DRIFT-RECONCILE-001) |
| P2 | Phase 1 verification scaffolding (9-layer pyramid) | 24 hr | ✅ DONE — 4 PRs cumulatively: DEC-PHASE1-INVARIANT-CATALOG-001 entry #250 + DEC-PHASE1-SMOKE-AND-SKELETONS-001 entry #251 + DEC-PHASE1-WEEK2-SCAFFOLDING-001 entry #252 + DEC-PHASE1-WEEK3-RUNBOOK-001 entry #253 (4-agent runbook + empty-pyramid baseline) |
| P3 | Merkle audit batching upgrade (per Agent B industry research) | 16 hr | 📋 still tracked |
| P3 | SLSA L3 provenance workflow | 8 hr | 📋 still tracked |
| P3 | Chaos injection harness | 8 hr | 📋 still tracked |
| P3 | M4 Pro absolute-throughput ≥ 50 M/s gate activation | ✅ DONE | runner registered + certified (MIGRATION #655); `bench-m4pro.yml` passes daily/push/dispatch (run #109, 2026-06-14) — DEC-M4PRO-DOC-RECONCILE-001 |
| OPERATOR | GitHub server-side branch protection (only operator can configure) | 5 min | 📋 still tracked per `docs/runbooks/github-branch-protection-setup.md` |
| OPERATOR | Subscribe to Groww (₹499/mo) + set env vars + run `scripts/groww_probe.py` | 30 min | 📋 HARD GATE blocking Phase 2 per DEC-VENDOR-PIVOT-001 entry #246 |
| OPERATOR | Phase 1 sign-off (10-item checklist) | 15 min | 📋 per `docs/PHASE1_EMPTY_PYRAMID_BASELINE.md §7` |

---

## 12. Amendment log

| Date | Change | Reason |
|---|---|---|
| 2026-05-16 evening | Initial draft (62% overstated) | Operator decree on 100% standards |
| 2026-05-16 night | REWRITTEN HONESTLY post 6-agent forensic audit | Agents A + B + C + D + E + F findings; operator: "I don't have the confidence dude"; Golden Rule #10 honest-limits required |
| 2026-05-17 | DEC-P0-001 — closed 8 of 8 P0 enforcement gaps in §11 priority matrix | All 4 P0 ruff/format items + bandit tightening + pyright strict + `\|\| true` removal + [data] extras + mutation score gate + vulture whitelist all done in one PR. Section §1 row counts: ✅ ENFORCED moves from 4 → 9, 🟡 PARTIAL moves from 8 → 3, 📋 ROADMAP moves from 4 → 4, 👁️ MANUAL unchanged at 3. See `docs/MIGRATION.md` entry #139 for full detail. |
| 2026-05-24 | DEC-STANDARDS-AUDIT-001 + DEC-CROSSWALK-001 + DEC-PYLINT-DROP-001 + DEC-DOCTOR-001 + DEC-STRESS-001 | Multi-PR ratchet; §1 row counts moved post-audit truth-up: 15 🟢 / 2 🟡 / 1 📋 / 3 👁️ (71% / 10% / 5% / 14%). See MIGRATION.md entries #205-#209. |
| 2026-05-25 | DEC-TELEGRAM-WIRING-001 + DEC-SLA-DETECTOR-001 + DEC-WATCHDOG-001 + DEC-L3-REPLAY-ORACLE-001 + DEC-MONITOR-LOOP-001 + DEC-STANDARD-MONITOR-001 + DEC-INFRA-OPS-001 | Observability stack landed: 4 detectors + production composer + env-driven CLI. §2 + §4 + §8 capability rows ratcheted. See MIGRATION.md entries #238-#244. |
| 2026-05-26 | DEC-VENDOR-PIVOT-001 + DEC-INDICATOR-COVERAGE-001 + DEC-TELEGRAM-WIRING-VERIFICATION-001 + DEC-AUDIT-TRUTH-RECONCILE-001 + DEC-ADAPTIVE-RATE-LIMITER-001 + DEC-PHASE1-INVARIANT-CATALOG-001 + DEC-PHASE1-SMOKE-AND-SKELETONS-001 + DEC-PHASE1-WEEK2-SCAFFOLDING-001 | Vendor pivot to Groww; Phase 1 scaffolding Weeks 1+2; honest-audit second-pass truth-up. See MIGRATION.md entries #245-#252. |
| 2026-05-27 | DEC-PHASE1-WEEK3-RUNBOOK-001 (entry #253) + DEC-AUDIT-RECONCILE-002 (entry #254) + **DEC-AUDIT-RECONCILE-003** (entry #255) | Phase 1 Week 3 closure + STANDARDS audit reconciliation (this entry). §5 PR gate matrix updated (20 rows refreshed; 4 new rows added for Z+ marker / no-hallucination / DEC-ID / bug-fix scanners); §8 Real-time guarantees refreshed (4 rows ratcheted from 📋 to 🟢, 2 new rows added for SLA + Watchdog); §10 Operator promise expanded from "9 enforced gates" → 15 + 12 LIVE plus list; §11 Closing path priority — multiple P1/P2 items flipped from 📋 to ✅ DONE with DEC-ID citations. |
| 2026-07-06 | DEC-DOCTOR-DAILY-CRON-001 | Wired the doctor-daily 07:30 IST cron (§11 P2 row → ✅, §8 SLA row → 🟢, §2/§4/§5/§10 reconciled). Envelope-honest: hosted runner asserts SKIP-with-reason on envelope checks + PASS on wheel handshakes; production-envelope doctor remains verify-m4pro.yml; "alert if missed" named follow-up. |
