# BruteX — 100% Standards Honest Audit (2026-05-26)

> **Authority:** This doc is the operator-facing ground truth for the
> "100% guarantee" question. It is built from five parallel deep-research
> agent passes on 2026-05-25 against the live `claude/admiring-knuth-KJiMY`
> branch. Every cell is backed by a file path + decision ID. Per Golden
> Rule #1 (no hallucination) and Golden Rule #10 (honest envelope), nothing
> here is rounded up.
>
> **Last refresh:** 2026-05-26 evening — second-pass reconciliation
> (DEC-AUDIT-TRUTH-RECONCILE-001, MIGRATION entry #248). Four §9 gap
> rows that were still labeled 🔴 HIGH / 🟡 MID as `unimplemented` /
> `missing module` are reclassified 🟢 CLOSED: Watchdog (entry #240),
> SLA detector (entry #239), L3 replay oracle (entry #241), SessionStart
> hook (entry #237). All four landed via DEC-INFRA-OPS-001 (entry #244)
> + companion decisions but the audit text wasn't refreshed at the
> time. §0 "per-scope" metric ratchets 3.5/6 → 4.5/6 and §0 5-pillar
> AUTOMATED row ratchets ❌ → 🟡 to reflect this. **No new code or
> tests landed in this refresh — it is pure audit truth-up.** The
> earlier 2026-05-26 afternoon refresh recorded the DEC-VENDOR-PIVOT-001
> cascade (PRs #135–#141); the 2026-05-25 evening refresh recorded
> `DEC-COMBO-HASH-UNIQUENESS-001` (commit `2d2cf2a`).

---

## 0. The bottom line (read this first)

**Can the operator have a literal 100% guarantee today? NO.** Saying yes
would violate Golden Rule #1. The codebase itself documents the gap.

| Headline metric | Today | Target | Honest gap |
|---|---|---|---|
| **STANDARDS rows fully CI-blocking** | 15 / 21 (71%) | 21 / 21 | 4 partial + 3 manual rows |
| **Z+ defensive layers mechanically enforced** | 5 / 7 | 7 / 7 | RECOVER + COOLDOWN remain manual |
| **Z+ 6-marker PR-body guard** | ✅ FAIL-CLOSED | — | Marker PRESENCE checked; CONTENT quality is reviewer |
| **5-pillar decree fully evidenced in code** | 2 / 5 | 5 / 5 | COMMON 🟡, SCALABLE 🟡, AUTOMATED 🟡 (was ❌; ratcheted via DEC-INFRA-OPS-001) |
| **14 bracket items fully enforced** | 8 / 14 | 14 / 14 | 4 partial + 2 manual |
| **Real-time capabilities truly continuous** | 6 / 12 | — | 3 nightly + 1 batched + 2 not-wired |
| **Per-scope auto enforcement** (PR/sub-PR/task/sub-task/cur-session/new-session) | 4.5 / 6 | 6 / 6 | Pre-commit install discipline + branch-protection ROADMAP (was 3.5/6 pre-DEC-SESSION-START-HOOK-001) |

**One-sentence verdict:** ~70% of the decreed defense surface is mechanically
enforced on every PR today. The remaining 30% is partial, manual, or stub.
Every gap is tracked in `docs/MIGRATION.md` with a decision ID.

---

## 1. Per-scope guarantee table — does the gate apply to EVERY PR / sub-PR / task / sub-task / current session / new session?

| Scope | Trigger | Automatic gates | Discipline-dependent | Citation |
|---|---|---|---|---|
| **Per PR (full)** | `pull_request` to main | 20 CI-required gates (lint/type/test/coverage/security/dedup/etc), Z+ 6-marker guard FAIL-CLOSED, pre-merge-gate.yml posts verdict | Branch protection server-side is 📋 ROADMAP per `docs/runbooks/github-branch-protection-setup.md` | `.github/workflows/ci.yml:41-51`, `scripts/ci/zplus-pr-guard.sh` |
| **Per sub-PR (push to open PR)** | `push` to `claude/**`, `feat/**`, `fix/**` | Full 20-gate suite re-runs on every push (concurrency cancels stale) | None | `.github/workflows/ci.yml:42-47` |
| **Per task (git commit)** | Pre-commit hooks | 13 fast gates: ruff fix/format, mypy strict, cargo fmt/clippy, gitleaks, banned patterns, conventional commits | One-time `pre-commit install` required; can be bypassed with `--no-verify` (forbidden by CLAUDE.md §3 without operator instruction) | `.pre-commit-config.yaml:1-99` |
| **Per sub-task (mid-edit save)** | PostToolUse hook | `ruff check --fix` auto-runs on every Python file Write/Edit | YAML/JSON/Markdown saves have NO post-edit check; ruff is fix-only, not test/coverage | `.claude/settings.json:60-70` |
| **Per current session** | Harness auto-load | `CLAUDE.md` auto-loaded (12 Golden Rules + 18-requirement decree), `.claude/settings.json` env+permissions+hooks active, PreToolUse blocks direct edits on `main` | Reading `TRACK1_BACKTEST_SPEC.md` + `MIGRATION.md` recent decisions = Claude discipline, not a hook | `CLAUDE.md:1-149`, `.claude/settings.json:48-82` |
| **Per new session** | Fresh harness init | Same as current session **PLUS** `.claude/hooks/session-start.sh` prints branch + last 5 MIGRATION decisions + audit-doc freshness + venv/fno_math presence + Z+ 6-marker reminder to stderr on every session start (DEC-SESSION-START-HOOK-001, MIGRATION entry #237) | Pre-commit install state does NOT persist across containers (operator must `pre-commit install` once per fresh checkout). | `.claude/hooks/session-start.sh`, `.claude/settings.json::hooks.SessionStart` |

**Verdict:** 4.5 of 6 scopes have full automatic enforcement; 1.5 rely
significantly on discipline (was 3.5/6 pre-DEC-SESSION-START-HOOK-001). The
remaining critical gap is **server-side branch protection** (still 📋 ROADMAP
per the setup runbook); without it, a single discipline lapse could let a
red PR merge.

---

## 2. The 14 bracket items — operator-demanded checklist

Every bracket the operator listed: code coverage, code scanning, code
duplication, functionality missing, scenario missing, bug fixing, issues
finding, testing types, testing coverage, real-time testing, code
performance, uniqueness, deduplication, O(1) latency.

| # | Bracket item | Enforcement | Gate citation | Threshold | Gap to 100% |
|---|---|---|---|---|---|
| 1 | **Code coverage** | 🟡 PARTIAL | `ci.yml::test`, DEC-COV-001 | 90% global + 29 modules @ strict 100% | ~16 modules still below strict-100% (ratchet in progress) |
| 2 | **Code scanning** | 🟢 FULL | `ci.yml::security` + `security-audit.yml` | bandit `-l` + CodeQL + Semgrep + Trivy + gitleaks + pip-audit + cargo-audit | — |
| 3 | **Code duplication** | 🟢 FULL | `ci.yml::duplication` | jscpd ≤3% blocks (DEC-P0-001 made it blocking) | Target 0% (currently 3%) |
| 4 | **Functionality missing** | 🟡 PARTIAL | `scripts/ci/spec_test_crosswalk.py` FAIL-CLOSED | Every spec section maps to ≥1 test | Spec-coverage measurement is binary (matched/not), not depth |
| 5 | **Scenario missing** | 👁️ MANUAL + ✅ partial | property + golden + redteam tests | All 7 test types present | No "exhaustive scenario generator" |
| 6 | **Bug fixing (regression req)** | 👁️ + ✅ | `validate_bugfix_regression.py` | `fix:` PR must touch `tests/` | Test QUALITY judged by reviewer |
| 7 | **Issues finding** | 🟢 FULL | `ci.yml::lint`, `ci.yml::type` | ruff strict + mypy strict + pyright strict + vulture + pylint subset | — |
| 8 | **Testing types (7)** | 🟢 FULL | `ci.yml` + `stress-nightly.yml` | unit/integration/property/golden/bench/stress/mutation | Stress runs nightly only on GHA |
| 9 | **Testing coverage** | 🟡 PARTIAL | `ci.yml::test` | 90% floor + 29 strict-100% modules | Full per-module 100% not yet |
| 10 | **Real-time testing** | 🟢 FULL | `ci.yml` on `push` + `pull_request` | Every push runs full suite | — |
| 11 | **Code performance** | 🟡 PARTIAL | `ci.yml::bench`, DEC-P0-020 | p99 ratio ≤3-10× on CI; 1.1× target on M4 Pro | M4 Pro absolute gate needs runner |
| 12 | **Uniqueness** | 🟢 FULL | `tests/property/test_combo_hash_uniqueness.py` + `test_dedup_hash_properties.py` | 9-field BLAKE3 + distinct-combo→distinct-hash | Just landed: DEC-COMBO-HASH-UNIQUENESS-001 (commit 2d2cf2a) |
| 13 | **Deduplication** | 🟢 FULL (L1+L2) / 🟡 PARTIAL (L3+L4) | `brutex/orchestrator/bloom.py`, `store.py` | Bloom + LMDB tested | L3 replay oracle UNIMPLEMENTED; L4 partial |
| 14 | **O(1) latency** | 🟢 FULL (ratio) / 🟡 PARTIAL (absolute) | `ci.yml::bench` | 5/5 hot paths gated; ratio 3-10× ceilings | Absolute 1.1× ratio needs M4 Pro runner |

**Tally:** 8 🟢 FULL · 4 🟡 PARTIAL · 2 👁️ MANUAL+✅ partial · 0 ❌ NOT-ENFORCED.

---

## 3. The 5-pillar COMMON·DYNAMIC·SCALABLE·AUTOMATED decree

The operator-locked decree from 2026-05-16 (CLAUDE.md §2 rule 11).
Re-audited 2026-05-25.

| Pillar | Status | Evidence | Citation |
|---|---|---|---|
| **COMPREHENSIVE** | 🟢 ENFORCED | EMAState has both `update()` + `compute_ema()`; bit-exact oracle via `test_bloom_golden.py`; P-06 look-ahead invariant in 3 indicator tests; serialize/restore proven in `test_streaming_parity.py` | `brutex/indicators/ema.py`, `tests/unit/test_streaming_parity.py`, `tests/golden/test_bloom_golden.py` |
| **COMMON** | 🟡 PARTIAL | jscpd 3% gate live + Rust facade in `rust/fno-math/` + indicators imported cleanly | `pyproject.toml`, `STANDARDS_100PCT.md` §5 row 7 |
| **DYNAMIC** | 🟢 ENFORCED | Indicator registry exports 5 classes; plugin factories; 52-bit u64 mask vocab fully implemented; conflict-graph pruner present | `brutex/indicators/__init__.py`, `brutex/vocabulary/evaluator.py`, `brutex/vocabulary/bits.py` |
| **SCALABLE** | 🟡 PARTIAL | O(1) ratio gates ENFORCED in CI (3-10× ceilings honest per GHA flake tolerance); absolute 50 M/s is ROADMAP | `tests/bench/test_throughput_canary.py`, DEC-P0-020 |
| **AUTOMATED** | ❌ ASPIRATIONAL | Release Please ✅ + auto-merge ✅; **Watchdog ❌ no code, Telegram 🟠 code-without-wiring, Auto-resume ❌ explicitly rejected by DEC-P0-029** | `.github/workflows/release.yml`, `auto-merge.yml`, `brutex/orchestrator/quota_tracker.py` (no-resume rationale), `brutex/notifications/telegram.py` (stub) |

**Verdict:** 2 of 5 pillars fully evidenced (COMPREHENSIVE, DYNAMIC).
2 partial (COMMON, SCALABLE). **1 aspirational (AUTOMATED).**

---

## 4. Real-time capability audit (12 items)

The operator demanded "real-time" everything. Here's what's truly continuous.

| # | Capability | Cadence | Citation |
|---|---|---|---|
| 1 | Test execution | 🔵 EVERY-PUSH | `ci.yml` `on: [push, pull_request]` |
| 2 | Linting | 🔵 EVERY-PUSH + 🟢 PER-COMMIT | `.pre-commit-config.yaml:38-47` + `ci.yml:80-98` |
| 3 | Type checking | 🔵 EVERY-PUSH | `ci.yml:103-132` mypy + pyright |
| 4 | Coverage | 🔵 EVERY-PUSH | `ci.yml:166-496` |
| 5 | Mutation | 🔵 EVERY-PUSH (state.py) + 🟡 WEEKLY (full) | `ci.yml:1042-1166` + `mutation.yml` |
| 6 | Security scan | 🔵 EVERY-PUSH + 🟡 WEEKLY | `ci.yml:840-898` + `security-audit.yml` |
| 7 | Dep freshness | 🟡 NIGHTLY | `dep-freshness-nightly.yml` (reports only, no enforcement) |
| 8 | Telegram alerts | 🟠 EVENT-DRIVEN (CODE-ONLY) | `brutex/notifications/telegram.py` — no wiring to publish sites |
| 9 | Dashboard updates | 🟢 CONTINUOUS | `brutex/dashboard/app.py:659` — 500ms WebSocket + 1s HTTP fallback |
| 10 | Audit log (hash-chain) | 🟢 CONTINUOUS | `brutex/orchestrator/audit.py` — per-event JSONL append |
| 11 | SLA monitoring | ❌ NOT WIRED | docstring mention only; no enforcement |
| 12 | Worker health watchdog | ❌ NOT WIRED | dashboard tracks last_heartbeat but no watchdog detects staleness |

**Verdict:** 6 truly real-time (push/event/stream); 3 scheduled (nightly);
1 batched (Telegram dedupe); **2 not wired (SLA + worker watchdog)**.

---

## 5. The Z+ defensive layer — what's enforced on EVERY PR

Per `docs/zplus/ZPLUS_PR_DISCIPLINE.md`, every PR must carry **6 marker
phrases** in the body. The `zplus-pr-guard.sh` script FAIL-CLOSED enforces
this. Verified live on commit `2d2cf2a`.

| # | Marker | What it proves |
|---|---|---|
| 1 | `Z+ 15-row matrix` | Design/review coverage matrix completed |
| 2 | `Z+ 7-row resilience matrix` | Resilience assessed across DETECT/VERIFY/RECONCILE/PREVENT/AUDIT/RECOVER/COOLDOWN |
| 3 | `Z+ 7-layer audit` | Audit discipline layers reviewed |
| 4 | `4-agent hostile review` | Plan + Explore + 2× general-purpose agent passes documented |
| 5 | `Honest envelope` | Golden Rule #10 admission included |
| 6 | `Decision ID` | DEC-FOO-NNN or equivalent cited |

**Layer-by-layer status:**

| Z+ Layer | Status | Notes |
|---|---|---|
| DETECT | 🟢 13 redteam tests | `tests/redteam/test_redteam_zplus_pr_guard.py` |
| VERIFY | 🟢 pytest 100% gate | per-PR test evidence |
| RECONCILE | 👁️ MANUAL | reviewer judgement |
| **PREVENT** | 🟢 zplus-pr-guard.sh | `scripts/ci/zplus-pr-guard.sh` FAIL-CLOSED |
| **AUDIT** | 🟢 JSONL override log | `scripts/ci/pre-merge-overrides.log` |
| RECOVER | 🟡 MANUAL | per-PR git-revert path; no revert-test yet |
| COOLDOWN | 🟡 MANUAL | manual soak decision |

---

## 6. The 6 solo-closable gaps post-audit — ratchet plan + status

After the 2026-05-25 audit, 6 gaps were classified as solo-closable (no
engine/no hardware needed). Current ratchet status:

| # | Gap | LoC est | Status |
|---|---|---|---|
| 1 | Wire Telegram `.publish()` to 5 audit events | ~250 | 📋 NEXT |
| 2 | Build `brutex/observability/watchdog.py` | ~400 | 📋 NEXT |
| 3 | Build `brutex/observability/sla.py` (needs #1) | ~250 | 📋 NEXT |
| 4 | L3 audit log replay oracle | ~300 | 📋 NEXT |
| 5 | SessionStart hook | ~50 | 📋 NEXT |
| 6 | Explicit "no duplicate combo emitted" property test | ~270 | ✅ **DONE (commit 2d2cf2a, MIGRATION #235)** |

**Today's progress:** 1/6 done. After all 6 land: STANDARDS enforcement
moves from 71% → ~90%; AUTOMATED pillar moves from ❌ → 🟢; uniqueness
verification is fully proven.

---

## 7. ASCII diagrams for the lazy-auto-driver framing

### Defense-in-depth (every PR)

```
                ┌─────────────────────────────────────────────┐
                │  EVERY PR — 20 BLOCKING CI GATES (today)   │
                │  + Z+ 6-marker guard FAIL-CLOSED            │
                └────────────────┬────────────────────────────┘
                                 │
  ┌──────────┬──────────┬───────┴────┬──────────┬──────────┐
  ▼          ▼          ▼            ▼          ▼          ▼
┌────────┐┌────────┐┌────────┐  ┌────────┐┌────────┐┌────────┐
│ LINT   ││ TYPES  ││ TESTS  │  │ COVER  ││ SECURE ││ Z+     │
│ ruff + ││ mypy + ││ 6 of 7 │  │ 90% +  ││ bandit ││ marker │
│ vulture││pyright ││ types  │  │ 29 @   ││ + CodeQL│ │guard  │
│ jscpd  ││ strict ││ run    │  │ 100%   ││ +Semgrep│ │FAIL-  │
│        ││        ││        │  │        ││ +Trivy ││CLOSED │
└────────┘└────────┘└────────┘  └────────┘└────────┘└────────┘
                                 │
                                 ▼
                ┌─────────────────────────────────────────────┐
                │   ci-ok aggregate ─ all green or BLOCK     │
                └────────────────┬────────────────────────────┘
                                 │
                                 ▼
                ┌─────────────────────────────────────────────┐
                │       auto-merge.yml SQUASHES               │
                └─────────────────────────────────────────────┘
```

### What's NOT guaranteed (gaps)

```
                ┌────────────────────────────────────────────┐
                │   ❌ NOT GUARANTEED TODAY                  │
                └────────────────┬───────────────────────────┘
                                 │
        ┌────────────────────────┼─────────────────────────┐
        ▼                        ▼                         ▼
  ┌────────────┐         ┌──────────────┐         ┌──────────────┐
  │ TELEGRAM   │         │ WATCHDOG     │         │ AUTO-RESUME  │
  │ code 100%  │         │ no module    │         │ DEC-P0-029   │
  │ NOT WIRED  │         │ at all       │         │ rejected by  │
  │ to events  │         │              │         │ design       │
  └────────────┘         └──────────────┘         └──────────────┘
        │                        │                         │
        ▼                        ▼                         ▼
  ┌────────────┐         ┌──────────────┐         ┌──────────────┐
  │ SLA ALERT  │         │ 12/13 DASH   │         │ L3 REPLAY    │
  │ no detector│         │ fields mock  │         │ ORACLE       │
  │            │         │ (Phase 3)    │         │ unimplemented│
  └────────────┘         └──────────────┘         └──────────────┘
```

### Coverage map (simple visual)

```
STANDARDS rows enforced:  [████████████████░░░░░] 15/21 = 71%
Z+ layers automated:      [██████████████░░░░░░░] 5/7   = 71%
Bracket items full:       [██████████████░░░░░░░] 8/14  = 57%
5 pillars in code:        [████████░░░░░░░░░░░░░] 2/5   = 40%
Real-time capabilities:   [████████████░░░░░░░░░] 6/12  = 50%
Scope auto enforcement:   [████████████░░░░░░░░░] 3.5/6 = 58%
```

---

## 8. The "explain like to an auto-driver" version

| Concept | Analogy |
|---|---|
| **20 CI gates per PR** | Airport security: 20 checkpoints, one beeps → you don't fly |
| **Z+ 6-marker guard** | Hotel housekeeping checklist on the door — 6 boxes, one missing → manager pages |
| **Hash-chained audit log** | Bank passbook where every page is signed by the previous page's hash — tear one page → tampering visible |
| **O(1) ratio gate** | Speedometer alarm: 60 km/h with 1 passenger should be 60 km/h with 5 passengers; slows → engine sick |
| **Bloom + LMDB dedup** | Bouncer at door (fast "probably new") → check ID at desk (slow but certain) |
| **Telegram alerts (PARTIAL)** | Fire alarm installed, speakers tested, **smoke sensors (SLA + Watchdog + Replay) wired and verified end-to-end (DEC-TELEGRAM-WIRING-VERIFICATION-001); pipeline-event sensor still on the bench waiting for Phase 2** |
| **Watchdog (WIRED)** | Dead-man's switch installed on the train: `brutex.observability.watchdog.Watchdog` (DEC-WATCHDOG-001, MIGRATION entry #240) wakes on `make_heartbeat_subscriber` beats, fires Telegram on `on_hung` callback when no beat for `BRUTEX_WATCHDOG_TIMEOUT_SECONDS` (default 300s) |
| **Mutation testing** | Pranking the codebase by mutating one operator at a time; if no test catches the prank, you have a coverage hole |
| **Property tests (Hypothesis)** | A million random inputs probe corner cases your unit tests forgot |

---

## 9. What I CANNOT guarantee — explicit honest gaps

Listed by severity. Every one has a tracked PR path.

| Severity | Gap | Why I can't 100%-guarantee | Closure path |
|---|---|---|---|
| 🟡 MID | Telegram alerts: SLA/Watchdog/Replay wired, pipeline path dormant | `compose_standard_monitor` (PR #131) wires SLA + Watchdog + Replay callbacks → `_publish_telegram` → `notifier.publish` (PR #133 added `cmd_monitor` to drive it). End-to-end chain verified by `tests/integration/test_compose_monitor_sla_to_telegram.py` (DEC-TELEGRAM-WIRING-VERIFICATION-001). `make_pipeline_event_subscriber` exists but is dormant — `PhaseRunner` has zero production callers until Phase 2 drivers land. | Pipeline subscriber activates automatically when Phase 2 drivers instantiate `PhaseRunner` with the subscriber list. |
| 🟢 CLOSED | Watchdog implemented + wired | `brutex/observability/watchdog.py::Watchdog` (DEC-WATCHDOG-001, MIGRATION entry #240) + `make_heartbeat_subscriber` for `PhaseRunner` integration + `on_hung` Telegram routing in `compose_standard_monitor`. Module is 25-test 100%-coverage and runs under `brutex monitor`. | Activates on first Phase 2 worker that subscribes via `make_heartbeat_subscriber`. |
| 🟢 CLOSED | SLA breach alerting implemented + wired | `brutex/observability/sla.py::SLADetector` (DEC-SLA-DETECTOR-001, MIGRATION entry #239) with state-machine NORMAL ↔ BREACHED transitions + `on_breach` / `on_recovery` callbacks. Telegram path verified end-to-end by `tests/integration/test_compose_monitor_sla_to_telegram.py` (DEC-TELEGRAM-WIRING-VERIFICATION-001). | Operator declares rules via `MonitorConfig(sla_rules=...)`; loop runs under `brutex monitor`. |
| 🔴 HIGH | Server-side branch protection 📋 ROADMAP | Setup runbook §1-6 not completed by operator | Operator action (5 mins in GitHub UI) |
| 🟡 MID | 12/13 dashboard fields still mock | Sweep producer not wired (needs Phase 3) | Phase 3 engine work |
| 🟢 CLOSED | L3 audit replay oracle implemented | `brutex/orchestrator/replay.py::assert_dedup_consistent` (DEC-L3-REPLAY-ORACLE-001, MIGRATION entry #241) re-projects the audit log + verifies the LMDB dedup set is consistent with the replayed subjects. Wired into `MonitorLoop` via `_make_replay_check` in `compose_standard_monitor`. Optional opt-in via `BRUTEX_LIVE_SUBJECTS_FILE` env var. | Operator sets the live-subjects file path to enable continuous L3 verification. |
| 🟡 MID | 6 of 21 STANDARDS rows partial/manual | Coverage strict-100% × 2, throughput absolute, content-quality marker, regression-test quality, citation correctness | Multi-PR ratchet over coming sessions |
| 🟢 CLOSED | SessionStart hook implemented | `.claude/hooks/session-start.sh` (DEC-SESSION-START-HOOK-001, MIGRATION entry #237) wired via `.claude/settings.json::hooks.SessionStart`. Prints branch / last 5 MIGRATION DEC-IDs / docs freshness / venv + fno_math presence / Z+ 6-marker reminder to stderr on every session start. | Operator sees the same context on every session — zero discipline burden. |
| 🟢 LOW | Per-PR "review quality" not measurable | Marker presence ≠ matrix depth | Manual reviewer judgement always |

---

## 10. Refresh cadence + ownership

| Aspect | Refresh cadence | Owner |
|---|---|---|
| This doc | Each session that touches enforcement, plus weekly drift check | Claude (per session) |
| `STANDARDS_100PCT.md` row matrix | Per ratchet PR | Claude |
| `MIGRATION.md` decision ledger | Per decision lock | Claude |
| 5-agent deep audit | Each operator "100% guarantee" ask | Claude |
| Branch protection drift check | Daily 03:30 UTC | `branch-protection-drift.yml` |
| Z+ 6-marker guard | Every PR open | `scripts/ci/zplus-pr-guard.sh` |

**This doc is canonical:** when an answer differs between this doc and any
ad-hoc claim, this doc wins (until superseded by a fresher audit).
