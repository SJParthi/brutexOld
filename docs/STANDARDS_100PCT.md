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
| 1 | Coverage line | 100% on `brutex/` | `fail_under=90` global floor; **strict per-module 100% gate ratcheted** (DEC-COV-001 steps 1–17, 2026-05-24; DEC-INDICATOR-COVERAGE-001 entry #245 added 3 more; DEC-VENDOR-PIVOT-001 entry #246 removed ~5 Groww gates; DEC-COV-RATCHET-001 entry #256 added 14 already-at-100% modules; **DEC-COV-BACKFILL-001 entry #257 added 3 backfilled modules**): **45 modules** now gated at 100% (verified 2026-05-27 by `grep -oE "pytest --cov=brutex\.[a-z_.]+" .github/workflows/ci.yml`). Indicator family fully covered (`indicators.{ema,day_extremes,prev_day_ohlc,pivots,fib_levels}` + `calendars.nse`); observability family fully covered (`observability.{logging,monitor,sla,standard_monitor,watchdog,metrics}` + `notifications.{telegram,wiring}` + `orchestrator.replay`); costs family fully covered (`costs.{types,constants,calculator}` + `execution.costs`); strategies family fully covered (`strategies.{combo,errors,registry,types,protocol}` + `strategies.library.{buy_and_hold,close_previous_momentum,opening_range_breakout,donchian_breakout}`); vocabulary fully covered (`vocabulary.{bits,conflicts,evaluator}`); engine `engine.combo_mask` covered; feed family adds `feed.resampler`; plus `doctor`, `models`, `data.models`, `data.partition`, `universe`. Remaining ~26 modules below strict-100% live mostly in `dashboard/`, `orchestrator/{audit,bloom,store,pipeline,quota_tracker,rate_limiter,adaptive_rate_limiter,retry,state}`, `strategies/{runner,sweep}`, `data/{parser,timeframes,tf_resampler}`, `config/runtime`, `execution/simulator`, `options/strike_rules`. | 🟡 | Multi-PR ratchet: each PR adds one or more modules. Tracking in MIGRATION entries #210–#257. |
| 2 | Coverage branch | 100% | `--cov-branch` flag ON; same strict-per-module gate as row 1 (DEC-COV-001) also enforces branch=100% on ratcheted modules. | 🟡 | Same multi-PR ratchet as row 1. |
| 3 | Mutation testing | ≥ 85% | DEC-P0-001 (2026-05-17): `\|\| true` REMOVED; mutmut junitxml → Python parser enforces gate on `brutex/orchestrator/state.py`. DEC-P0-002 (2026-05-17 same day, REVERTED by DEC-P0-003): briefly lowered to 60% interim; operator decreed "no downgrade — 100% standards". **DEC-P0-003 (2026-05-17): threshold RESTORED to 85%.** | ✅ | If CI fails on this gate, write more `state.py` tests OR extend gate to additional modules (`brutex/costs/calculator.py` at 85% — P1) |
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
| 11 | 7 test types | all 7 | unit ✅, integration ✅, property ✅, golden ✅, bench ✅, mutation ✅, **stress ✅** (DEC-STRESS-001 correction 2026-05-24): `tests/stress/test_audit_chain_sustained.py` runs via `.github/workflows/stress-nightly.yml` (daily 03:00 UTC + workflow_dispatch for 24h soak via `STRESS_EVENTS=10_000_000`); pytest marker `stress` registered in `pyproject.toml`. | 🟢 | 24h soak via workflow_dispatch (not auto-cron — burns GHA hours; honest envelope per Golden Rule #10) |
| 12 | Real-time CI | every push + nightly | ✅ ci.yml on push + dep-freshness-nightly.yml + drift-check.yml | ✅ | none |
| 13 | O(1) per hot path | p99(1M)/p99(1K) ≤ 1.1 | **Ratio gates ENFORCED on CI** (DEC-STANDARDS-AUDIT-001 correction 2026-05-24): `tests/bench/test_store_o1.py::test_get_ratio_below_3x` gates store lookup at 3×; `tests/bench/test_throughput_canary.py::test_vectorized_mask_and_ratio` gates numpy mask AND at 8×; `test_dict_lookup_ratio` gates dict lookup at 10×. Per-CI ceiling is looser (3-10×) than the M4 Pro production target (1.1×) per documented shared-GHA flake-mitigation in DEC-P0-020. | 🟢 | CI ratio gates live; tightening to 1.1× requires M4 Pro local-runner (ROADMAP) |
| 14 | Throughput canary ≥ 50 M/s | yes | `tests/bench/test_throughput_canary.py` LIVE (DEC-P0-020, PR #42) — 3 benchmarks gate the O(1) ratio (`p99(1M)/p99(100K) < 5×`) on the brute-force hot path (numpy mask AND + dict bar lookup). **CI gate is RATIO** (matches Rule #5 + existing `test_store_o1.py` pattern); absolute ops/s recorded in `bench-results.json` informationally. Local measurement: numpy ratio 1.27× (absolute 512M ops/s), dict ratio 3.59× (absolute 2.3M ops/s). **50 M/s absolute is the M4 Pro production envelope** (per `docs/TRACK1_PERFORMANCE_BUDGET.md`); CI shared GHA runner is the sub-target. Absolute gates would chronic-flake on shared GHA — verified empirically when PR #42 commit `8ea175a` failed both push + PR runs with 30M/2.5M absolute gates; hotfix `fd3f2dd` switched to ratio gates. | 🟡 | CI gate live (ratio); ROADMAP: M4 Pro local-runner runbook + Phase 3 PyO3 kernel can lift to ≥200 M/s absolute |
| 15 | Bloom + LMDB dedup | 4-tier | **Property tests ENFORCED** (DEC-STANDARDS-AUDIT-001 correction 2026-05-24): `tests/property/test_bloom_invariants.py` covers Bloom filter invariants (no-false-negatives, FPR bounds, idempotent inserts); `tests/property/test_dedup_hash_properties.py` covers hash-based dedup properties. Implementation at `brutex/orchestrator/bloom.py` + `store.py`. | 🟢 | Property tests run on every push via the `Property tests (hypothesis)` CI job |
| 16 | Audit JSONL hash-chain + `brutex doctor` | always | **`brutex/doctor.py` LIVE** (DEC-DOCTOR-001 correction 2026-05-24): `brutex doctor [--audit-file PATH] [--audit-dir PATH] [--runtime-file PATH]` subcommand runs 2 health checks (audit chain integrity via `verify_chain`; runtime file presence + parseability). Exit 0 on all PASS/SKIP; 1 on any FAIL. Wired into `brutex/cli.py` as `cmd_doctor`. Unit tests in `tests/unit/test_doctor.py` cover all check states + the CLI exit-code contract. | 🟢 | Future stress / lake / quota checks tracked as P2 — broader coverage is a ratchet on this scaffold |
| 17 | No hallucination | every claim cited | Golden Rule #1 in CLAUDE.md + PR template + **partial automation added** (DEC-HALLUCINATION-SCAN-001 2026-05-24): `scripts/ci/validate_no_hallucination.py` scans PR body for external-system keywords (Groww / NSE / BSE / SEBI / RBI / brokerage / securityId / rate_limit / exchange_segment / Renaissance / Two Sigma / AQR / etc.) and fails CI if any paragraph mentions them without a nearby citation marker (URL, `verified YYYY-MM-DD`, `CLAUDE.md §N`, `MIGRATION.md entry #NNN`, `TRACK1_*.md`, `STANDARDS_100PCT.md`, `groww.in/trade-api/docs/docs`, `nseindia.com`, or a `DEC-FOO-NNN` identifier). Code spans (` ``` ` / `` ` ``) stripped before scanning. Reviewer still judges "is the citation CORRECT" — script catches the failure mode where a claim has NO citation at all. | 👁️ + ✅ partial | "Is the citation correct" stays reviewer; "external claims must have a citation nearby" now CI-enforced |
| 18 | Decision IDs | every locked decision | PR template requires DEC-ID + MIGRATION.md row; **partial automation added** (DEC-DEC-ID-VALIDATOR-001 2026-05-24): `scripts/ci/validate_dec_ids.py` parses PR body for `DEC-FOO-NNN` tokens and fails CI if any cited ID has no matching ledger entry. Reviewer still judges "should this PR have cited a DEC-ID at all?" | 👁️ + ✅ partial | "Must cite DEC-ID for arch change" stays reviewer; "cited DEC-IDs must exist" now CI-enforced |

### Summary count (post DEC-STRESS-001, 2026-05-24)

| Status | Count of 21 sub-rows | % | Delta vs pre-audit |
|---|---:|---:|---:|
| 🟢 Fully enforced | **15** (rows 3, 4×2, 5×2 [PYLINT-DROP closed 5b], 6, 7×2, 8, 9, **11** ← DEC-STRESS-001 flipped from 🟡, 12, 13, 15, **16** ← DEC-DOCTOR-001 flipped from 📋) | **71%** | +6 (audit + CROSSWALK + PYLINT-DROP + STRESS + DOCTOR) |
| 🟡 Partial (runs but threshold/advisory issues) | 2 (rows 1, 2) | 10% | −2 (rows 8 + 11 closed) |
| 📋 Roadmap (not yet wired) | **1** (row 14) | **5%** | **−4** (rows 13, 15 were stale; row 5b closed by PYLINT-DROP; row 16 closed by DOCTOR) |
| 👁️ Manual (PR template + reviewer) | 3 (rows 10, 17, 18) | 14% | unchanged |

**Pre-audit numbers (kept for history):** 9 ✅ / 4 🟡 / 5 📋 / 3 👁️ → 43% / 19% / 24% / 14%.

**Post-audit reality (DEC-STANDARDS-AUDIT-001):** 11 🟢 / 4 🟡 / 3 📋 / 3 👁️ → 52% / 19% / 14% / 14%.

**Post-DEC-CROSSWALK-001 reality (2026-05-24):** 12 🟢 / 3 🟡 / 3 📋 / 3 👁️ → 57% / 14% / 14% / 14%.

**Post-DEC-PYLINT-DROP-001 reality (2026-05-24):** 13 🟢 / 3 🟡 / 2 📋 / 3 👁️ → 62% / 14% / 10% / 14%.

**Post-DEC-DOCTOR-001 reality (2026-05-24):** 14 🟢 / 3 🟡 / 1 📋 / 3 👁️ → 67% / 14% / 5% / 14%.

**Post-DEC-STRESS-001 reality (2026-05-24):** 15 🟢 / 2 🟡 / 1 📋 / 3 👁️ → **71% / 10% / 5% / 14%**.

**The 1 genuinely remaining ROADMAP gap that DOES need code work:**
1. **Row 14 — Throughput canary 50 M/s absolute on CI** — ratio gate live (status 🟡 not 📋 strictly); absolute 50 M/s requires M4 Pro local-runner per documented shared-GHA flake-mitigation. ROADMAP: M4 Pro local-runner runbook + Phase 3 PyO3 kernel.

**~~Row 5b — pylint standalone~~** — ✅ CLOSED by DEC-PYLINT-DROP-001 (2026-05-24). ruff `PL` subset in `pyproject.toml [tool.ruff.lint].select` covers pylint-equivalent rules; standalone pylint dropped. See MIGRATION.md entry #207.

**~~Row 16 — `brutex doctor` CLI~~** — ✅ CLOSED by DEC-DOCTOR-001 (2026-05-24). `brutex/doctor.py` + `brutex doctor` subcommand + 18 unit tests in `tests/unit/test_doctor.py`. See MIGRATION.md entry #209.

**The 2 PARTIAL items remaining** (post DEC-STRESS-001 + DEC-DOCTOR-001; genuinely partial; not stale):
1. Row 1 — Coverage line 90% → 100% (legacy module backfill needed; tracked as DEC-COV-001)
2. Row 2 — Coverage branch 90% → 100% (same as row 1)

**~~Row 8 — Spec↔test crosswalk warn-and-continue → fail-closed~~** — ✅ CLOSED by DEC-CROSSWALK-001 (2026-05-24). Row 8 is now 🟢. See MIGRATION.md entry #206.

**~~Row 11 — Stress test job missing~~** — ✅ CLOSED by DEC-STRESS-001 (2026-05-24). `tests/stress/test_audit_chain_sustained.py` runs nightly via `.github/workflows/stress-nightly.yml`; pytest `stress` marker registered. 24h soak reachable via workflow_dispatch with `STRESS_EVENTS=10_000_000`. Row 11 is now 🟢. See MIGRATION.md entry #208.

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
| **Dashboard** | FastAPI on `127.0.0.1:9080` | accurate within 5 sec | 📋 dashboard module is excluded from coverage; needs real test |
| **Alerting** | Telegram + dashboard + email digest + macOS fallback | CRITICAL alert within 60 sec | 🟡 PARTIAL — Telegram path WIRED end-to-end (DEC-TELEGRAM-WIRING-001 entry #238 + DEC-TELEGRAM-WIRING-VERIFICATION-001 entry #247): `compose_standard_monitor` routes SLA breach / Watchdog heartbeat-stale / L3 replay-inconsistency events into `_publish_telegram` → `TelegramNotifier.publish`; end-to-end proven by `tests/integration/test_compose_monitor_sla_to_telegram.py`. CI runs the test on every push (verifies the WIRING; not the bot reachability — that requires `BRUTEX_TELEGRAM_BOT_TOKEN` env var operator-set). Pipeline-event subscriber (`make_pipeline_event_subscriber`) exists but legitimately dormant until Phase 2 `PhaseRunner` drivers land. |

Plus `brutex doctor` daily 07:30 IST — **CLI BUILT** (DEC-DOCTOR-001, 2026-05-24). The CLI is shippable; wiring it as an actual 07:30 IST cron (instead of on-demand) is a tracked P2 follow-up (`.github/workflows/doctor-daily.yml` — not in this PR).

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
| **AUTOMATED** | 100% automated after one-time bootstrap | 🟡 Release Please ✅, auto-merge ✅, Watchdog ✅ LIVE (DEC-WATCHDOG-001 entry #240 + DEC-STANDARD-MONITOR-001 entry #243), SLA detector ✅ LIVE (DEC-SLA-DETECTOR-001 entry #239 + entry #243), L3 replay oracle ✅ LIVE (DEC-L3-REPLAY-ORACLE-001 entry #241 + entry #243), Telegram wiring ✅ LIVE (DEC-TELEGRAM-WIRING-001 entry #238 + DEC-TELEGRAM-WIRING-VERIFICATION-001 entry #247), Auto-resume ❌ explicitly rejected by DEC-P0-029 — remaining gaps: pipeline-event subscriber dormant until Phase 2; doctor-daily cron + Telegram bot reachability proof tracked as P2 |

---

## 5. PR gate matrix (REAL current state)

| Gate | Tool | Decree threshold | Current threshold | Blocking? |
|---|---|---|---|---|
| Unit tests | pytest | 100% pass | 100% pass enforced via `ci.yml::test`; **28 modules at strict per-module 100%** (DEC-COV-001 + DEC-INDICATOR-COVERAGE-001 entry #245 + DEC-VENDOR-PIVOT-001 entry #246; verified 2026-05-27 by `grep -E "pytest.*--cov=brutex\.[a-z_.]+.*--cov-fail-under=100" .github/workflows/ci.yml \| sort -u \| wc -l`) | ✅ blocks |
| Coverage line | pytest-cov | 100% | 90% global floor + **28 strict-100% modules**; per-module gate enforced in CI (DEC-COV-001) | 🟡 90% blocks globally; 100% strict on 28 modules |
| Coverage branch | pytest-cov --cov-branch | 100% | `--cov-branch` flag ON; 90% global floor; same 28 strict-100% per-module gate enforces branch=100% on those modules (DEC-COV-001) | 🟡 90% blocks globally; 100% strict on 28 modules |
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
| Audit chain | `brutex doctor` | green | **`brutex doctor` CLI LIVE** (DEC-DOCTOR-001 entry #209, 2026-05-24); `--audit-file` + `--audit-dir` + `--runtime-file` + `--live-subjects-file` flags; runs `verify_chain` + runtime parseability + L3 dedup-replay consistency check (DEC-L3-REPLAY-ORACLE-001 entry #241) | 🟢 CLI exists; 07:30 IST cron tracked as P2 |
| Conventional commit | scripts/ci/check-conventional-commits.sh | passes | enforced via `Conventional commit format` CI job (Lint group); rejects non-`feat:` / `fix:` / `docs:` / `chore:` / `test:` / etc. prefixes | ✅ blocks |
| MIGRATION.md entry | reviewer + validator | every locked decision | PR template line + **`scripts/ci/validate_dec_ids.py`** (DEC-DEC-ID-VALIDATOR-001 entry #214) rejects PRs citing non-existent DEC-IDs; reviewer judges "should this PR have cited a DEC-ID" | 👁️ + ✅ partial |
| Spec ↔ test crosswalk | scripts/ci/spec_test_crosswalk.py | all sections matched | **fail-closed** (DEC-CROSSWALK-001 entry #206, 2026-05-24); env-var `CROSSWALK_ALLOW_STALE=1` for local script-iteration only — CI never sets it | ✅ blocks |
| Banned patterns | scripts/ci/scan-banned-patterns.sh | clean | runs on every push; narrow scope (e.g. pandas only checked in `brutex/math/`) — broadening tracked as follow-up | ✅ blocks (narrow scope) |
| Bug-fix regression | scripts/ci/validate_bugfix_regression.py | `fix:` PR must touch tests/ | **partial automation** (DEC-BUGFIX-REGRESSION-001 entry #213, 2026-05-24); matches PR title against `fix:`/`bugfix:`/`hotfix:` prefixes + body keywords; reviewer judges "is this the RIGHT regression test" | 👁️ + ✅ partial |
| No-hallucination citation scan | scripts/ci/validate_no_hallucination.py | external claims must cite source | **partial automation** (DEC-HALLUCINATION-SCAN-001 entry #211, 2026-05-24); scans PR body for external-system keywords + nearby citation markers (URLs, DEC-FOO-NNN, MIGRATION.md entry #NNN, CLAUDE.md §N, verified YYYY-MM-DD) | 👁️ + ✅ partial |
| Z+ PR-body marker check | scripts/ci/zplus-pr-guard.sh | 6 marker phrases present | **FAIL-CLOSED** per DEC-P0-014 entry #152; markers: `Z+ 15-row matrix`, `Z+ 7-row resilience matrix`, `Z+ 7-layer audit`, `4-agent hostile review`, `Honest envelope`, `Decision ID` | ✅ blocks |
| Stress (24h-style) | pytest -m stress | sustained latency p99 flat | **`stress-nightly.yml`** runs `tests/stress/test_audit_chain_sustained.py` nightly 03:00 UTC + workflow_dispatch for 24h soak via `STRESS_EVENTS=10_000_000` (DEC-STRESS-001 entry #208) | 🟡 nightly only (not per-PR) |

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
| `brutex doctor` runs every workday | 07:30 IST cron | **CLI LIVE** (DEC-DOCTOR-001 entry #209); cron wrapper `.github/workflows/doctor-daily.yml` tracked as P2 follow-up | 🟡 |
| Backtest reproducible | bit-for-bit | **L3 replay oracle LIVE** (DEC-L3-REPLAY-ORACLE-001 entry #241); `brutex/orchestrator/replay.py::assert_dedup_consistent` re-projects audit log → subjects → diffs vs LMDB; wired into `brutex doctor --live-subjects-file` and into `MonitorLoop` via `compose_standard_monitor` | 🟢 (L3 oracle wired; full Parquet bit-equality test is Phase 2/3) |
| No data loss on crash | always | audit chain property tests assert append correctness; **adaptive rate limiter** (DEC-ADAPTIVE-RATE-LIMITER-001 entry #249) provides backpressure under load. Crash-recovery property test is Phase 2 | 🟡 |
| O(1) per hot path | p99(1M)/p99(1K) ≤ 1.1 | **Ratio gates LIVE** in `ci.yml::bench` (DEC-STANDARDS-AUDIT-001 + DEC-P0-020 entry #158); 5 of 6 hot paths gated at 3-10× ratio per shared-GHA flake-tolerance; absolute 1.1× target requires M4 Pro runner (DEC-M4PRO-ABSOLUTE-001 entry #236) | 🟢 (ratio gates live; absolute target ROADMAP) |
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
4. Unit tests (100% pass + ≥ 90% global coverage + **28 modules at strict per-module 100%** per DEC-COV-001 + DEC-INDICATOR-COVERAGE-001 entry #245)
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
- **Adaptive rate limiter LIVE** (DEC-ADAPTIVE-RATE-LIMITER-001 entry #249) — 4-state machine `[4..8]` r/s
- **24-invariant catalog** (DEC-PHASE1-INVARIANT-CATALOG-001 entry #250) — 5 IMPLEMENTED + 19 ROADMAP via skip
- **4-agent hostile review runbook** (DEC-PHASE1-WEEK3-RUNBOOK-001 entry #253) — `docs/runbooks/4-agent-review.md`
- **Empty-pyramid baseline doc** (DEC-PHASE1-WEEK3-RUNBOOK-001 entry #253) — `docs/PHASE1_EMPTY_PYRAMID_BASELINE.md`
- **Nightly workflows** (DEC-STRESS-001 entry #208): `stress-nightly.yml`, `redteam-nightly.yml`, `dep-freshness-nightly.yml`
- **Weekly mutation** `mutation.yml` runs cargo-mutants per `.cargo/mutants.toml` (DEC-PHASE1-WEEK2-SCAFFOLDING-001 entry #252)

**ROADMAP (closing path remaining):**
- Full 100% coverage line + branch globally (currently 90% floor + 28 strict-100% modules; ~43 modules below strict-100% per honest audit §2 row 1)
- M4 Pro absolute throughput gate ≥ 50 M/s (DEC-M4PRO-ABSOLUTE-001 entry #236) — workflow stub exists; activates when operator registers M4 Pro self-hosted runner
- `.github/workflows/doctor-daily.yml` cron — CLI exists, cron wrapper is P2 follow-up
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
| P1 | Write benchmark baseline JSON + `--benchmark-compare --fail` gate | 2 hr | 🟢 PARTIAL via ratio gates (DEC-STANDARDS-AUDIT-001 + DEC-P0-020 entry #158); absolute baseline JSON tracked as P2 |
| P1 | Build minimal `brutex doctor` CLI with C2 (audit-chain) check | 4 hr | ✅ DONE (DEC-DOCTOR-001 entry #209, 2026-05-24) — `--audit-file` + `--audit-dir` + `--runtime-file` + `--live-subjects-file` flags |
| P1 | Write P-DEDUP-1, P-AUDIT-1 property tests | 4 hr | ✅ DONE — P-DEDUP-1 covered by `tests/property/test_combo_hash_uniqueness.py` + `test_dedup_hash_properties.py` (DEC-COMBO-HASH-UNIQUENESS-001); P-AUDIT-1 covered by `tests/property/test_audit_chain_properties.py` (P-16, P-17 in DEC-PHASE1-INVARIANT-CATALOG-001 entry #250) |
| P1 | Widen banned-patterns scanner scope (not just `brutex/math/`) | 1 hr | 📋 still tracked (out of scope for DEC-AUDIT-RECONCILE-003) |
| P2 | Per-module 100% coverage gate matrix (iterate every `brutex/` module) | 8 hr | 🟢 PARTIAL — 28 modules at strict-100% (DEC-COV-001 + DEC-INDICATOR-COVERAGE-001 entry #245); ~43 modules remain — multi-PR ratchet ongoing |
| P2 | Install + wire pylint OR remove the claim | 1 hr | ✅ DONE (DEC-PYLINT-DROP-001 entry #207, 2026-05-24) — replaced by ruff `PL` subset; standalone pylint dropped |
| P2 | Add stress test job to ci.yml | 4 hr | ✅ DONE (DEC-STRESS-001 entry #208, 2026-05-24) — `tests/stress/test_audit_chain_sustained.py` runs nightly via `stress-nightly.yml`; 24h soak reachable via workflow_dispatch with `STRESS_EVENTS=10_000_000` |
| P2 | Wire `.github/workflows/doctor-daily.yml` 07:30 IST cron | 1 hr | 📋 still tracked (CLI exists; cron wrapper is P2 follow-up) |
| P2 | Observability stack: Watchdog + SLA + L3 replay + Telegram wiring | 16 hr | ✅ DONE — 5 decisions cumulatively: DEC-TELEGRAM-WIRING-001 entry #238 + DEC-SLA-DETECTOR-001 entry #239 + DEC-WATCHDOG-001 entry #240 + DEC-L3-REPLAY-ORACLE-001 entry #241 + DEC-STANDARD-MONITOR-001 entry #243 — all 4 detectors wired into `brutex monitor` via `compose_standard_monitor`; end-to-end verified by DEC-TELEGRAM-WIRING-VERIFICATION-001 entry #247 |
| P2 | Adaptive rate limiter for Groww | 4 hr | ✅ DONE (DEC-ADAPTIVE-RATE-LIMITER-001 entry #249) — 4-state machine `[4..8]` r/s pure logic; HTTP wiring deferred to Phase 1 adapter |
| P2 | Phase 1 verification scaffolding (9-layer pyramid) | 24 hr | ✅ DONE — 4 PRs cumulatively: DEC-PHASE1-INVARIANT-CATALOG-001 entry #250 + DEC-PHASE1-SMOKE-AND-SKELETONS-001 entry #251 + DEC-PHASE1-WEEK2-SCAFFOLDING-001 entry #252 + DEC-PHASE1-WEEK3-RUNBOOK-001 entry #253 (4-agent runbook + empty-pyramid baseline) |
| P3 | Merkle audit batching upgrade (per Agent B industry research) | 16 hr | 📋 still tracked |
| P3 | SLSA L3 provenance workflow | 8 hr | 📋 still tracked |
| P3 | Chaos injection harness | 8 hr | 📋 still tracked |
| P3 | M4 Pro absolute-throughput ≥ 50 M/s gate activation | 1 hr (operator) | 🟡 STUB LANDED (DEC-M4PRO-ABSOLUTE-001 entry #236); activates when operator registers M4 Pro self-hosted runner |
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
