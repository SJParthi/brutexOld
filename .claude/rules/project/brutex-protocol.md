# BruteX Protocol Bundle — auto-loaded charter for every session

> **Status:** PARTIAL bundle (2 of 9 tickvault rule files inlined verbatim).
> Auto-loads on every Claude Code session in BruteX.
> **Origin:** Synthesized verbatim from `tickvault/.claude/rules/project/`
> on 2026-05-17 per operator decree (DEC-PROTOCOL-001). The MECHANICAL
> PROTOCOL is identical across both repos; the SUBSTANTIVE EXAMPLES
> (WebSocket / QuestDB / DHAT / Loom / tick rescue ring / Dhan order WS)
> are tickvault-specific and do NOT translate to BruteX 1:1.
> **Currently inlined verbatim (2 of 9 files):**
> - `pr-completion-protocol.md` — 10-step PR loop with serial-completion lock
> - `operator-charter-forever.md` — 15-row + 7-row matrices + honest 100% wording
>
> **Referenced by name only (7 of 9 — NOT inlined in this file):**
> - `z-plus-defense-doctrine.md` — Z+ 7-layer pattern
> - `plan-enforcement.md` — DRAFT→APPROVED→IN_PROGRESS→VERIFIED FSM
> - `stream-resilience.md` — 12 B-rules
> - `per-wave-guarantee-matrix.md` — equivalent to BruteX `docs/STANDARDS_100PCT.md`
> - `wave-4-shared-preamble.md` — Sections 1-8 mostly tickvault-specific
> - `testing.md` — 22 test categories (BruteX applies 7 per Golden Rule #12)
> - `testing-scope.md` — block-scoped default
>
> The 7 referenced-only files apply via the adaptation doc translation
> table; their canonical text remains in the tickvault repo. Inlining
> the verbatim text is on the BruteX roadmap (open issue: split into
> per-file `.claude/rules/project/<name>.md` files).
>
> **Adaptation guide:** `docs/PROTOCOL_BRUTEX_ADAPTATION.md` — READ THIS FIRST
> before applying any rule that references tickvault-specific infrastructure.
> The adaptation doc OUTRANKS this bundle body.
>
> **Authority order (locked DEC-PROTOCOL-001):**
> 1. `CLAUDE.md` — 12 Golden Rules
> 2. `docs/PROTOCOL_BRUTEX_ADAPTATION.md` — translation table
> 3. This file (bundle body)
> 4. Defaults
>
> Every rule below is binding only after filtering through the adaptation doc.

---

## How to read this bundle inside BruteX

| Bundle term | BruteX equivalent | If equivalent missing |
|---|---|---|
| `crates/<X>/src/...` (Rust crate path) | `brutex/<module>/...` (Python package) OR `rust/fno-math/src/...` (Rust hot kernel) | N/A |
| `cargo test -p <crate>` | `pytest tests/unit/test_<module>*.py` (Python) + `cargo test` (Rust hot kernel only) | N/A |
| `cargo clippy` | `ruff check brutex` + `mypy --strict brutex` + `pyright brutex` | N/A |
| `cargo fmt` | `ruff format brutex` | N/A |
| QuestDB (live TSDB) | DuckDB (catalog) + LMDB (dedup) + Parquet (bars) | Stored data only; BruteX has no live TSDB |
| WebSocket main-feed | N/A — BruteX is a BACKTEST engine; no live feeds (Track 1) | Apply to Dhan adapter at Phase 2 ingest only |
| WebSocket order-update | N/A — Track 1 is signal-only; Track 2 simulates execution offline | Skip entirely |
| DHAT zero-alloc tests | Rust hot kernel only (`rust/fno-math/` benches); Python hot path uses `pytest-benchmark` p99 ratio | Skip for Python-only changes |
| Loom concurrency | N/A — BruteX is single-process orchestrator + parallel pytest | Skip entirely |
| `cargo-mutants` | `mutmut` (Python) + `cargo-mutants` (Rust crate) | N/A |
| `cargo fuzz` | `hypothesis` (Python property tests) + `cargo fuzz` (Rust crate) | N/A |
| Telegram NotificationEvent | BruteX Telegram bot (📋 spec'd, not built — see `brutex/notifications/telegram.py`) | Defer until bot lands |
| `tv_*_total` Prometheus counter | structlog event with `event=...` field + audit JSONL line | No Prometheus in BruteX MVP |
| Grafana panel | FastAPI dashboard `brutex/dashboard/` (📋 not yet built) | Defer until dashboard lands |
| `mcp__tickvault-logs__*` MCP tools | N/A — BruteX has no equivalent MCP server | Use `Read`/`Bash` directly |
| SessionStart hooks (`session-auto-health.sh`) | None in BruteX yet — opportunity to add | If high value, propose as separate PR |
| `.claude/plans/active-plan*.md` (DRAFT→APPROVED→VERIFIED) | `TodoWrite` in-session + per-PR commits | If multi-session plans become real (Track 2 Phase 3+), adopt this format |
| Wave-4 sub-PR sequencing (Wave-4-A through Wave-4-E3) | Track 1 / Track 2 phase numbering in `docs/track2/phases/` | Use BruteX phase numbers, not "Wave N" |
| Dhan WS connection lock (2 conns only) | Dhan HTTP REST adapter (no WS in Track 1) | N/A for Track 1; Track 2 may add WS for live |

**Hard rule:** if any section below cites tickvault-specific infrastructure
that doesn't exist in BruteX, apply the SHAPE of the rule (file-first,
chunked output, decision IDs, audit trail, 100% honest-envelope claim) and
substitute the BruteX equivalent from the table above. NEVER invent a
tickvault-like component just to satisfy a rule.

---

## Per-section applicability (honest — see adaptation doc §2 for full map)

The full BruteX-side applicability matrix lives at
`docs/PROTOCOL_BRUTEX_ADAPTATION.md §2`. Summary for the 2 inlined files
in this bundle:

| Section | BruteX applicability | Why not ✅ 1:1 |
|---|---|---|
| `pr-completion-protocol.md` — 10-step loop | 🟡 5 of 10 steps need substitutes | Steps 1/4 reference `.claude/hooks/*.sh` and `.claude/plans/active-plan*.md` that DO NOT exist in BruteX; substitutes are `scripts/ci/*.sh` + TodoWrite in-session. Steps 3/5/6/7/9/10 work 1:1 via `mcp__github__*` tools. |
| `operator-charter-forever.md` §C 15-row + 7-row matrices | ✅ Apply 1:1 via `docs/STANDARDS_100PCT.md` | Mirrored honestly there (4 enforced, 8 partial, 4 roadmap, 3 manual). |
| `operator-charter-forever.md` §D Telegram 10 commandments | 🟡 Apply to message-text NOW (`brutex/notifications/telegram.py` ✅ BUILT, 539 lines); defer event-source wiring | Module exists; alert producers not yet wired into subsystems. |
| `operator-charter-forever.md` §E 3-agent review | ✅ BruteX uses 4-agent variant per `docs/TRACK1_PHASE_PLAN.md §0` | Superset of bundle's 3-agent pattern. |
| `operator-charter-forever.md` §F honest 100% claim wording | ✅ Apply with BruteX envelope per adaptation doc §0 | |
| `operator-charter-forever.md` §H PR completion 10-step loop | 🟡 Same caveats as `pr-completion-protocol.md` row above | Section H is a near-duplicate. |
| `operator-charter-forever.md` §I WebSocket scope lock (2 WS only) | ❌ **N/A** — BruteX Track 1 has ZERO WebSocket connections | Track 1 = REST `/v2/charts/intraday`; Track 2 = offline execution sim. The verbatim §I text below references tickvault Rust file paths (`connection.rs:1666`, `phase2_recovery.rs:197`, etc.) that DO NOT exist in BruteX — IGNORE them. Apply only if Track 2 ever adds a live WS. |

For the 7 referenced-but-not-inlined files (z-plus-defense-doctrine,
plan-enforcement, stream-resilience, per-wave-guarantee-matrix,
wave-4-shared-preamble, testing, testing-scope), see
`docs/PROTOCOL_BRUTEX_ADAPTATION.md §2` for full applicability. Their
canonical text remains in tickvault; inlining here is on the BruteX
roadmap (open issue).

---

## Honest 100% claim — BruteX-flavored

See `docs/PROTOCOL_BRUTEX_ADAPTATION.md §0` for the full BruteX-specific
honest envelope wording (each line marked REAL ✅, PARTIAL 🟡, or
ROADMAP 📋 based on what actually exists in the codebase today).

Short form: when a BruteX PR body / commit message says "100% guarantee",
the body MUST be qualified using only currently-shipping capability,
mark each clause as REAL/PARTIAL/ROADMAP, and cite the specific file
that backs it. Promising literal "never miscalculates" / "always correct"
without envelope qualifier = REJECT IN REVIEW.

**Anti-pattern (rejected in this very PR's first draft):** citing
`tests/golden/test_cost_calculator_*.json` and `P-COST-1..P-COST-5`
property tests as proof of envelope when NEITHER exists in the codebase.
That is a Golden Rule #1 violation. Use existing artifacts verified to
exist on 2026-05-17:
- `tests/unit/test_costs_calculator.py` ✅ REAL (cost calc unit tests)
- `tests/unit/test_execution_costs.py` ✅ REAL (execution cost tests)
- `tests/unit/test_orchestrator_audit.py` ✅ REAL (audit unit tests)
- `tests/property/test_audit_chain_properties.py` ✅ REAL (audit hash-chain property tests)
- `tests/unit/test_engine_combo_mask.py` ✅ REAL (engine foundation)
- `tests/redteam/` ✅ REAL (planted-bug harness)
- Or mark proposed artifacts explicitly as 📋 ROADMAP.

**The 7-agent review pass on this PR demonstrated the pattern:** the first
draft cited 4 nonexistent test paths AND 1 nonexistent module path
(`brutex/adapters/dhan/constants.py` — actual is `options_constants.py`).
All caught before push by the exhaustive cite/path verifier (Agent 7).
This is the discipline every PR must clear.

---

## Cross-references to BruteX-native docs

| BruteX-native doc | Substantive overlap with bundle |
|---|---|
| `CLAUDE.md` | 12 Golden Rules — supersedes any contradicting bundle clause |
| `docs/STANDARDS_100PCT.md` | 18 standing requirements + honest 4-enforced/8-partial/4-roadmap/3-manual split — equivalent to bundle's 15-row matrix |
| `docs/TRACK1_BACKTEST_SPEC.md` | Per-stage algorithm + invariants — equivalent to bundle's per-wave spec |
| `docs/TRACK1_PHASE_PLAN.md §0` | 4-agent hostile review pattern — superset of bundle's 3-agent pattern |
| `docs/MIGRATION.md` | Decision ID ledger — equivalent to bundle's per-decision audit trail |
| `tests/redteam/` | Planted-bug harness — equivalent to bundle's mutation-test discipline |
| `.github/pull_request_template.md` | PR body matrix checkboxes — partial implementation of bundle's 9-box checklist |
| `.github/workflows/ci.yml` | Gate enforcement — partial implementation of bundle's mechanical-ratchet doctrine |

---

# VERBATIM BUNDLE BEGINS BELOW

> Everything below this line is the bundle content as supplied by the
> operator on 2026-05-17. Tickvault-specific references (WS / QuestDB /
> DHAT / Loom / cargo-mutants / Dhan order WS / Telegram NotificationEvent)
> are preserved verbatim — apply via the translation table above.

---

================================================================================
# FILE: pr-completion-protocol.md
================================================================================

# PR Completion Protocol — Serial Completion (operator lock 2026-05-15)

> **Authority:** CLAUDE.md > `operator-charter-forever.md` §H > this file > defaults.
> **Scope:** PERMANENT. Every PR Claude opens in this repo. FOREVER.
> **Operator-locked:** 2026-05-15 (verbatim quote in §H of charter).
> **Auto-load trigger:** Always loaded (path is in `.claude/rules/project/`).

---

## The verbatim operator demand (preserve exactly)

> "always ensure to make it ready for merge once branch pr is created and finished and even always monitor the current PR until it gets merged successfully to main after doing this alone only go ahead with the remaining process always dude okay? always save this bro okay?"

---

## The rule (one line)

**Finish + merge the current PR to `main` BEFORE starting the next item. One PR at a time. Monitor every PR until it lands.**

---

## The 10-step loop (MANDATORY, every PR)

| # | Step | Tool / Command |
|---|---|---|
| 1 | Code complete + local verify | `cargo test -p <crate>` + `bash .claude/hooks/banned-pattern-scanner.sh` + `bash .claude/hooks/pub-fn-test-guard.sh` + `bash .claude/hooks/plan-verify.sh` |
| 2 | Push branch with retry | `git push -u origin <branch>` (4 retries with exponential backoff on network err) |
| 3 | Open PR as **DRAFT** | `mcp__github__create_pull_request` — body MUST include: 9-box checklist, 15-row + 7-row matrices, honest 100% claim with envelope qualifier |
| 4 | Tick plan checkboxes INSIDE this PR's diff | `Edit` on `.claude/plans/active-plan*.md` — `[ ]` → `[x]` for every item this PR completes; commit as part of the PR |
| 5 | Mark PR **ready for review** once CI starts green | `mcp__github__update_pull_request` with `draft=false` |
| 6 | Enable **auto-merge** (squash strategy) | `mcp__github__enable_pr_auto_merge` |
| 7 | **Subscribe** to PR activity | `mcp__github__subscribe_pr_activity` |
| 8 | Event-driven loop: CI red → fix + push; reviewer comment → respond inline or fix | `<github-webhook-activity>` events arrive automatically; do NOT `sleep` poll |
| 9 | Verify merge: `state=closed, merged=true` | `mcp__github__pull_request_read` |
| 10 | Unsubscribe; THEN start next item | `mcp__github__unsubscribe_pr_activity` |

---

## What this rule FORBIDS

| Banned pattern | Why it's banned |
|---|---|
| Opening PR #2 before PR #1 merges | Violates serial completion |
| Marking plan checkbox `[x]` before PR merges to main | The merge IS the completion signal; premature ticks hide failed merges |
| Closing the session while a PR is mid-flight without subscribing | Operator loses visibility |
| `sleep` polling for PR status | Use `subscribe_pr_activity` — webhook events wake the session |
| `--no-verify` / `--no-gpg-sign` on push | Operator never authorized this |
| Manual merge skipping CI | Branch protection blocks; auto-merge respects gates |
| Working on `claude/main` or any branch protected by branch protection | Develop on feature branches only |

---

## Mandatory PR body sections

```markdown
## Summary
<one-paragraph what + why>

## Items completed
- [x] Item N — <one-line>
- [x] Item M — <one-line>

## Z+ 15-row "100% everything" matrix
<copy from operator-charter-forever.md §C, fill in this PR's specifics>

## Z+ 7-row "Resilience" matrix
<copy from operator-charter-forever.md §C, fill in this PR's specifics>

## Adversarial 3-agent review
- [x] hot-path-reviewer pass BEFORE impl (link to comment)
- [x] security-reviewer pass BEFORE impl
- [x] general-purpose hostile pass BEFORE impl
- [x] all 3 agents repeat AFTER impl on the diff

## Honest 100% claim
100% inside the tested envelope, with ratcheted regression coverage: <list specific envelope bounds with constants + ratchet test files>.

## Test plan
- [x] `cargo test -p <crate>` green
- [x] banned-pattern scanner clean
- [x] pub-fn-test-guard clean
- [x] plan-verify clean
- [x] CI all green
```

---

## When CI fails mid-loop

The session stays subscribed. On each `<github-webhook-activity>` CI-fail event:

1. Read the failing job log (`mcp__github__get_commit` or `pull_request_read`)
2. Diagnose root cause (do NOT blanket-retry — that's `sleep` polling in disguise)
3. Push the fix to the same branch
4. Auto-merge will fire on green; subscription stays alive

If the same fix has been pushed 3 times and still fails, reply on the PR with the diagnosis + ask the operator before pushing again.

---

## When a reviewer comments

1. Read the comment via `mcp__github__pull_request_read`
2. Decide: fix inline (push commit) OR reply with grep evidence (false positive) OR `AskUserQuestion` if ambiguous
3. After fix → resolve the review thread via `mcp__github__resolve_review_thread`
4. Auto-merge re-fires once all "requires action" review threads are resolved + CI green

---

## Mechanical ratchets (to be wired)

| Ratchet | Status |
|---|---|
| `.claude/hooks/serial-pr-guard.sh` — refuses to start new code if open PR exists on the operator's repo | PLANNED |
| `crates/storage/tests/serial_pr_protocol_guard.rs` — greps for this file's canonical phrases | PLANNED |
| Banned-pattern scanner category for `--no-verify` / `--no-gpg-sign` in shell history | PLANNED |

---

## Auto-driver / Insta-reel explanation

> "Sir, imagine your juice shop. Old way: you start making orange juice, then in the middle you start mango, then in the middle of mango you start sweet-lime. Three half-juices on the counter, nothing finished. Customer angry. New way: ONE juice at a time. Make orange → pour → hand to customer → take payment → only THEN start mango. Every juice gets the checkmark on the list ONLY when the customer pays. Three juices = three customers paid = three checkmarks. Simple. No half-juices on the counter."

---

## Trigger (auto-loaded paths)

Always loaded. Activates on any session that:
- Opens any PR
- Edits `.claude/plans/active-plan*.md`
- Pushes any branch
- Uses any `mcp__github__*` tool

================================================================================
# FILE: operator-charter-forever.md
================================================================================

# Operator Charter — FOREVER (auto-loaded every session)

> **Authority:** CLAUDE.md > this file > defaults.
> **Scope:** Every Claude Code session, every Claude Cowork task, every git branch, every PR, every plan item, every task file, every commit — FOREVER.
> **Status:** PERMANENT. Cannot be superseded. Operator's verbatim demand 2026-05-11.
> **Auto-load trigger:** Always loaded. (This file's path puts it in the auto-load set for `.claude/rules/project/`.)

---

## The verbatim operator charter (preserved exactly, do not paraphrase)

> "I need extreme complete comprehensive extensive automation 100 percentage especially where my every new or existing Claude code session or every new Claude Cowork task or existing should automatically do everything as an automated process always. Especially it should access logs queries dbs project product everything should be entirely accessible as an automated process either in local or AWS as a common runtime dynamic scalable automated extreme automation approach.
>
> See always achieve guaranteed 100 percentage code coverage 100 percentage audit coverage 100 percentage testing coverage 100 percentage code checks 100 percentage code performance 100 percentage monitoring 100 percentage logging 100 percentage alerting 100 percentage security 100 percentage security hardening 100 percentage bugs fixing 100 percentage scenarios covering 100 percentage functionalities covering 100 percentage code review 100 percentage extreme check as well.
>
> I need 100 percentage guarantee (extensive, comprehensive, intensive, deep thorough code coverage, code scanning, code duplication, functionality missing, scenario missing, bug fixing, issues finding, testing types, testing coverage, extensive comprehensive real time testing, code performance, uniqueness, deduplication, O(1) latency always) — inside these brackets whatever is mentioned I need 100 percentage.
>
> Zero ticks loss and nowhere WebSocket should get disconnected or reconnect, not even a single tick should be missed. Meanwhile our entire WebSockets and connections should never ever become slow or locked or hanged or stuck. QuestDB should never ever fail or always disconnect. I need guarantee for both of these always.
>
> Always extreme comprehensive automation monitoring alerting notifying on Telegram capturing tracking auditing debugging detailing visualizing checking everything.
>
> Always activate and use all the agents and all subagents as well to do a deep thorough research.
>
> Always it should be common runtime dynamic scalable incremental approach.
>
> Always ensure to maintain O(1) latency and uniqueness guarantee and deduplication. Provide all these with real-time checks and guarantee and assurance and proof.
>
> Always provide the explanation in a table comparison easy viewable readable understandable format where anyone can easily understand.
>
> Explain everything like explaining to a lazy dumb non-technical kid where the kid or auto-driver or some technical or non-technical person can't understand anything even if he is lazy to read — so explain using diagrams + animations + easily-understood format, looking like an Insta reel where if 1 million people see it they all understand."

---

## What this means MECHANICALLY (the binding contract)

### A. Every new Claude Code session at START

Auto-fired by `.claude/settings.json` SessionStart hooks (already wired):

| Step | Tool | What it does |
|---|---|---|
| 1 | `mcp__tickvault-logs__run_doctor` | 7-section health snapshot |
| 2 | `mcp__tickvault-logs__summary_snapshot` | Last-hour error signatures |
| 3 | `mcp__tickvault-logs__list_active_alerts` | Currently-firing Prom alerts |
| 4 | `bash .claude/hooks/session-auto-health.sh` | doctor + validate-automation in bg |
| 5 | `bash .claude/hooks/session-context-brief.sh` | Active plans + open PR + errors-last-hour |
| 6 | `bash .claude/hooks/session-sanity.sh` | Branch + auto-save check |

**If ANY step fails:** fix the underlying issue. Do NOT bypass with skip-flags.

### B. Every new Claude Cowork task

The `.mcp.json` `tickvault-logs` entry is loaded automatically — same MCP tools surface as `mcp__tickvault-logs__*`. Therefore:

- Logs ✅ accessible via `mcp__tickvault-logs__app_log_tail` / `tail_errors`
- Queries ✅ accessible via `mcp__tickvault-logs__questdb_sql` / `prometheus_query`
- DBs ✅ accessible (QuestDB + Prometheus via above)
- Project / product ✅ accessible (entire repo + grep + read)
- Local / AWS ✅ both supported (same MCP server)
- Common runtime ✅ same docker-compose Mac dev = AWS prod
- Dynamic / scalable ✅ same tool surface regardless of env
- Automated ✅ no manual setup needed

### C. Every plan item / task file MUST carry the 15-row + 7-row guarantee matrix

(Mechanically enforced by `.claude/hooks/per-item-guarantee-check.sh` + `make wave-guarantee-check`.)

#### 15-row "100% everything" matrix

| Demand | Mechanical proof artefact | Real-time check tool |
|---|---|---|
| 100% code coverage | `quality/crate-coverage-thresholds.toml` per crate | `scripts/coverage-gate.sh` in CI |
| 100% audit coverage | `<event>_audit` table with DEDUP UPSERT KEYS | `mcp__tickvault-logs__questdb_sql` |
| 100% testing coverage | 22 test categories per `testing.md` (unit/integration/property/loom/dhat/fuzz/mutation/sanitizer/coverage/etc.) | `cargo test --workspace` green |
| 100% code checks | banned-pattern + pub-fn-test + pub-fn-wiring + plan-verify + secret-scan + 8 pre-commit gates | pre-push mandatory |
| 100% code performance | DHAT zero-alloc + Criterion p99 budgets + bench-gate ≤5% regression | `cargo bench` + `scripts/bench-gate.sh` |
| 100% monitoring | 7-layer telemetry (Prom counter + gauge + tracing span + Loki log + Telegram event + Grafana panel + audit table) | `mcp__tickvault-logs__run_doctor` |
| 100% logging | tracing macros mandatory; ERROR → Telegram via Loki/Alertmanager | hourly `errors.jsonl` rotation |
| 100% alerting | `alerts.yml` Prom rule + `resilience_sla_alert_guard.rs` ratchet | `mcp__tickvault-logs__list_active_alerts` |
| 100% security | banned-pattern + secret-scan + `Secret<T>` enforcement + security-reviewer agent | `cargo audit` post-deploy |
| 100% security hardening | static IP enforcement + secret scan + `unused_must_use` lint | post-deploy IP verify |
| 100% bug fixing | adversarial 3-agent review (proven 4-bug catch rate per 30-commit PR) | pre-PR + post-impl agent pass |
| 100% scenarios covering | 9-box + chaos test for every new failure mode | `crates/storage/tests/chaos_*.rs` (16 today) |
| 100% functionalities covering | every pub fn has call site + matching test | pre-push gates 6+11 |
| 100% code review | adversarial 3-agent on diff BEFORE and AFTER impl | per-PR |
| 100% extreme check | all of above + ratchet test fails build on regression | every commit |

#### 7-row "Resilience demand" matrix

| Demand | Honest envelope (the truth) | Per-item proof |
|---|---|---|
| Zero ticks lost | Bounded zero loss inside chaos envelope: 5M-tick rescue ring → NDJSON spill → DLQ NDJSON catches every payload | item must not introduce new tick-drop path |
| WS never disconnects | SEBI 24h JWT forces ≥1 reconnect/day BY LAW. DETECT ≤5s, reconnect with `SubscribeRxGuard`, sleep-until-open post-close. | item must not break `SubscribeRxGuard` or pool watchdog |
| Never slow/locked/hanged | DHAT ≤4 alloc blocks/8KB across 10K calls; Criterion p99 ≤100ns enqueue; tick-gap >30s coalesced Telegram; core_affinity Core 0 | item must not add hot-path allocation |
| QuestDB never fails | ABSORB via 3-tier rescue→spill→DLQ + schema self-heal via `ALTER ADD COLUMN IF NOT EXISTS` | item must not break self-heal |
| O(1) latency | `from_le_bytes` + `papaya` + `Arc<HashMap>` + SPSC bounded; bench-gate ≤5% regression on hot path | item adds Criterion bench if hot path |
| Uniqueness + dedup | Composite `(security_id, exchange_segment)` per I-P1-11 + DEDUP UPSERT KEYS on every storage table | item DEDUP key includes segment |
| Real-time proof | 7-layer telemetry + SLO score every 10s + market-open self-test at 09:16:30 IST | item ratchet pins all 7 layers |

---

## D. Every Telegram message MUST follow the 10 commandments

(See `.claude/plans/friday-may-15-mega/topic-telegram-message-style-rules.md` for full detail.)

| # | Rule |
|---|---|
| 1 | Plain English ONLY — auto-driver level |
| 2 | NO library names (rkyv / papaya / DEDUP / arc-swap / mpsc / GCRA banned) |
| 3 | NO file paths |
| 4 | NO version numbers in body |
| 5 | YES emoji for status (✅ ⚠️ 🆘 🚀 🔔 🟢) |
| 6 | YES specific numbers ("11,034 instruments", "5/5 connections") |
| 7 | YES action verbs in degraded ("What you need to do RIGHT NOW: 1...2...3...") |
| 8 | One Telegram = one decision |
| 9 | Time stamps IST 12-hour ("9:13 AM", not "0913" or "09:13:00.000Z") |
| 10 | Severity emoji at START of subject |

**Litmus test for every NEW NotificationEvent:** Could a 60-year-old auto-driver read it on a phone in 5 seconds and know "OK or not OK + what to do"? If no → REWRITE.

---

## E. Every NEW design / PR / task MUST run adversarial 3-agent review

For any change touching > 3 crates or > 1,000 LoC: spawn THREE specialist agents IN PARALLEL:

| Agent | Mandate | Output |
|---|---|---|
| **hot-path-reviewer** | No `.clone()` / `Vec::new()` / `.collect()` / `format!()` / `Box` / `dyn` on hot path | Report ≤ 400 words |
| **security-reviewer** | Secret exposure, ILP injection, path traversal, unsafe blocks, missing sanitization | Report ≤ 400 words |
| **general-purpose (hostile)** | Race conditions, daily-reset collisions, missing market-hours gate, missing edge-trigger, false-OK class bugs | Report ≤ 600 words |

Wait for ALL THREE. Synthesize into CRITICAL/HIGH/MEDIUM/LOW table. Fix every CRITICAL/HIGH inline BEFORE opening the PR. After implementation lands green → run same 3 agents on the diff (proven pattern: 4-bug catch rate per 30-commit PR).

---

## F. The HONEST 100% claim (mandated PR-body wording)

When ANY PR body / commit message / Telegram message / docs writes "100% guarantee", it MUST be qualified exactly:

> "100% inside the tested envelope, with ratcheted regression coverage:
> ≤60s QuestDB outage absorbed by rescue→spill→DLQ;
> ≤5,000,000-tick ring buffer capacity (constant `TICK_BUFFER_CAPACITY`, ratcheted by `zero_tick_loss_alert_guard.rs`);
> bench-gated O(1) hot path;
> composite-key uniqueness;
> chaos-tested 65h Fri 16:00 IST → Mon 09:00 IST weekend sleep/wake.
> Beyond the envelope, DLQ NDJSON catches every payload as recoverable text."

**Anything stronger ("WebSocket never disconnects" / "QuestDB never fails" without envelope) = REJECT IN REVIEW.**

---

## G. Every artefact MUST include kid-friendly + table explanation

For every plan item / new feature / Telegram message / docs:

- Table-comparison format ✅ (operator-mandated)
- Auto-driver / dumb-kid analogy ✅ ("Sir, imagine your juice shop register breaks...")
- Diagrams / ASCII visualizations ✅
- Tight bullet lists, not paragraphs
- If a 1-million-view Insta reel must explain it → simplify until it can

---

## The 11 ALWAYS-ON rules (cheat sheet for any session)

| # | Rule | Source |
|---|---|---|
| 1 | Common-runtime: same docker-compose Mac dev = AWS prod | `aws-budget.md` rule 10 |
| 2 | RAM-first hot path — no DB queries from indicator/strategy/risk paths | `aws-budget.md` rule 12 |
| 3 | Composite-key uniqueness `(security_id, exchange_segment)` everywhere | `security-id-uniqueness.md` |
| 4 | DEDUP UPSERT KEYS on every QuestDB table + `ALTER ADD COLUMN IF NOT EXISTS` | `stream-resilience.md` B10 |
| 5 | Every `error!` carries `code = ErrorCode::X.code_str()` field | `error_code_tag_guard.rs` |
| 6 | flush/persist/drain failures use `error!`, never `warn!` | `error_level_meta_guard.rs` |
| 7 | Every counter panel wraps in `increase()` or `rate()` (per audit-findings Rule 12) | `audit-findings-2026-04-17.md` |
| 8 | Every new pub fn has a call site | `pub-fn-wiring-guard.sh` |
| 9 | Market-hours-aware tokio tasks (audit-findings Rule 3) | `audit-findings-2026-04-17.md` |
| 10 | Edge-triggered alerts only (audit-findings Rule 4) | `audit-findings-2026-04-17.md` |
| 11 | NO false-OK signals — gate success notifications on positive progress (audit-findings Rule 11) | `audit-findings-2026-04-17.md` |
| 12 | One PR open at a time — finish + merge current before starting next (operator lock 2026-05-15) | `.claude/rules/project/pr-completion-protocol.md` |
| 13 | Only 2 WebSocket connections EVER — 1 main-feed + 1 order-update; never depth-20, depth-200, or any 2nd main-feed conn (operator lock 2026-05-15) | `.claude/rules/project/websocket-connection-scope-lock.md` |

---

## H. PR Completion Protocol (MANDATORY, every PR — operator lock 2026-05-15)

> Operator verbatim 2026-05-15:
> "always ensure to make it ready for merge once branch pr is created and finished and even always monitor the current PR until it gets merged successfully to main after doing this alone only go ahead with the remaining process always dude okay? always save this bro okay?"

**The serial-completion rule:** Claude MUST finish + merge the current PR to `main` BEFORE starting the next item. No parallel branches. No "stack 3 PRs in flight". One PR at a time, monitored to merge.

### The 10-step loop (every PR)

| Step | Mandatory action | Tool |
|---|---|---|
| 1 | Code complete + local verify (`cargo test`, banned-pattern, pub-fn guards, plan-verify) | Bash |
| 2 | Push branch (`git push -u origin <branch>` with retry on network err) | Bash |
| 3 | Open PR as **draft**; body contains 9-box + 15-row + 7-row matrices + honest 100% claim | `mcp__github__create_pull_request` |
| 4 | Tick the plan checkbox `[ ]` → `[x]` for every item this PR completes — INSIDE this PR's diff, same commit | Edit |
| 5 | Mark PR **ready for review** once CI starts green | `mcp__github__update_pull_request` |
| 6 | Enable **auto-merge** (squash) so merge fires the moment branch protection signals pass | `mcp__github__enable_pr_auto_merge` |
| 7 | **Subscribe** to PR activity (CI status + review comments) | `mcp__github__subscribe_pr_activity` |
| 8 | On CI red → diagnose → push fix → re-monitor. On reviewer comment → fix or reply with grep evidence. | event-driven |
| 9 | Verify merge to `main`: `pull_request_read` returns `state=closed, merged=true` | `mcp__github__pull_request_read` |
| 10 | Unsubscribe; ONLY THEN start the next item (next PR) | `mcp__github__unsubscribe_pr_activity` |

### What this rule FORBIDS

| Banned pattern | Why |
|---|---|
| Opening PR #2 before PR #1 is merged to main | Violates serial completion |
| Marking plan checkbox `[x]` BEFORE PR merges | The merge IS the completion signal |
| Closing the session while a PR is mid-flight without subscribing | Operator loses visibility |
| Manual merge skipping CI | Branch protection blocks; auto-merge respects gates |
| Bypassing `--no-verify` on push or `--no-gpg-sign` | Operator never authorized this |

### Auto-driver one-liner

> "Sir, one juice at a time. Make the orange juice → squeeze it → pour it → hand it to customer → wait until they pay → only then start the next mango juice. No making three juices at once."

### Mechanical ratchet (to be wired)

- New hook `.claude/hooks/serial-pr-guard.sh` runs at session start; refuses code commits if an open PR authored by this branch exists on the operator's repo without merge.
- Tag-guard meta-test `crates/storage/tests/serial_pr_protocol_guard.rs` greps for the canonical phrases in this section so the rule cannot be silently deleted.

---

## I. WebSocket connection scope lock (MANDATORY, FOREVER — operator lock 2026-05-15)

> 🚨 **N/A IN BRUTEX (DEC-PROTOCOL-001):** BruteX Track 1 has ZERO WebSocket
> connections (REST API only: `POST /v2/charts/intraday`,
> `POST /v2/charts/historical`). Track 2 = offline execution simulator
> over stored option data. The entire §I below references tickvault
> infrastructure — `wss://api-feed.dhan.co`, `wss://api-order-update.dhan.co`,
> `connection.rs:1666`, `phase2_recovery.rs:197`, `config.rs:1192/2634`,
> `SubscribeRxGuard`, `compute_reconnect_base_delay_ms`,
> `compute_reconnect_backoff_ms`, `MsgCode 42`, `Source=P` filter, "222 SIDs"
> — all of which DO NOT EXIST in BruteX. **IGNORE this section unless
> Track 2 ever adds a live WebSocket.** See `docs/PROTOCOL_BRUTEX_ADAPTATION.md §3`.

> Operator verbatim 2026-05-15:
> "except [for] this 1 connection main feed websocket and order update websocket we will never ever use anything else"

**The only WebSocket connections this product will EVER open:**

| WS type | Count | Allowed instruments | Mode |
|---|---|---|---|
| **Main feed** (`wss://api-feed.dhan.co`) | **1 conn** | 4 IDX_I (NIFTY=13, BANKNIFTY=25, SENSEX=51, INDIA VIX=21) + 218 NSE_EQ F&O underlying stocks = **222 SIDs** | Ticker for IDX_I + Quote for NSE_EQ |
| **Order update** (`wss://api-order-update.dhan.co`) | **1 conn** | n/a (receives order events for orders we place) | JSON, MsgCode 42, filter `Source=P` |

**Forbidden FOREVER (until operator explicitly re-approves):**

| ❌ Banned WS type | Why |
|---|---|
| Depth-20 (`depth-api-feed.dhan.co/twentydepth`) | Operator lock — not in scope for any phase |
| Depth-200 (`full-depth-api.dhan.co/?token=...`) | Same |
| Any 2nd/3rd/4th/5th main-feed conn | 222 SIDs fit on 1 conn (Dhan cap = 5000/conn). More conns = wasted token + IP budget |
| Any new WS endpoint Dhan ships in future | Not in scope without operator re-approval |

**Reconnect parity (both allowed WS types):**

- First reconnect attempt: **0 ms (instant)** on both. Main feed via `compute_reconnect_base_delay_ms(0, _, _) → 0` (`connection.rs:1666`); order-update via `compute_reconnect_backoff_ms(1) → 0` (`order_update_connection.rs:639`, Phase 0 Item 4 fix 2026-05-15).
- Subsequent attempts: exponential backoff capped at `*_MAX_MS`.

**Mechanical guards:**

- `should_spawn_depth_dynamic_pipeline(IndicesUnderlyingsOnly, _) → false` in `crates/app/src/phase2_recovery.rs:197` (ratcheted at `phase2_recovery.rs:457`).
- `effective_main_feed_pool_size(IndicesUnderlyingsOnly, _) → 1` in `crates/common/src/config.rs:1192` (ratcheted at `config.rs:2634`).
- Greeks/movers pipelines deleted or gated to `false`.

**Auto-driver one-liner:**

> "Sir, only 2 phone lines to Dhan EVER. Line 1: live price feed for 4 indices + 218 stocks = 222 wires. Line 2: order update notifications. NOTHING else. No depth phone line. No second feed phone line. EVER. If anyone tries to add a 3rd phone, this rule file rejects the PR."

---

## Mechanical enforcement chain (the gates that fail-the-build)

| Gate | Where | What it catches |
|---|---|---|
| `pre-commit-fast-gate.sh` | every `git commit` | fmt + banned-pattern + secret-scan + version pin + 9 invariants |
| `pre-push-gate.sh` | every `git push` | 12 fast gates (~35s total) |
| `per-item-guarantee-check.sh` | pre-PR + `make wave-guarantee-check` | 15-row + 7-row matrix presence |
| `banned-pattern-scanner.sh` | pre-commit | `HashSet<u32>` / `.clone()` on hot path / etc. |
| `pub-fn-test-guard.sh` | pre-push | every pub fn has test or TEST-EXEMPT comment |
| `pub-fn-wiring-guard.sh` | pre-push | every pub fn has a call site |
| `plan-verify.sh` | pre-push | active plan items checked |
| `error_code_tag_guard.rs` | `cargo test` | every error code mentioned in a log has `code =` field |
| `error_code_rule_file_crossref.rs` | `cargo test` | every ErrorCode variant has rule-file mention |
| `dedup_segment_meta_guard.rs` | `cargo test` | every DEDUP key includes segment |
| `operator_health_dashboard_guard.rs` | `cargo test` | every counter has a Grafana panel |
| `resilience_sla_alert_guard.rs` | `cargo test` | every counter has an alert rule |
| `error_level_meta_guard.rs` | `cargo test` | flush/persist failures use `error!` not `warn!` |
| `scripts/bench-gate.sh` | post-merge | 5% regression budget on hot-path benches |
| `scripts/coverage-gate.sh` | post-merge | 100% line coverage per crate |
| GitHub Actions CI | every PR | full 22-test battery + mutation + fuzz |

**Anything that needs to be true must be machine-checked. Hand-wave = REJECT.**

---

## Read-this-first protocol for ANY new session

Any new Claude Code session (CLI or web Cowork) opening this repo MUST:

```bash
# 1. Sync
git fetch origin
git pull <current-branch>

# 2. Mandatory reads (in order)
cat CLAUDE.md                                                            # project charter
cat .claude/rules/project/operator-charter-forever.md                    # this file (the forever rules)
cat .claude/rules/project/wave-4-shared-preamble.md                      # wave-4 specifics (still applies)
cat .claude/plans/friday-may-15-mega/INDEX.md                            # current state
cat .claude/plans/friday-may-15-mega/step-1-honest-envelope.md           # canonical reference
tail -50 .claude/plans/friday-may-15-mega/00-decisions-log.md            # recent locks

# 3. Auto-fire (handled by SessionStart hook — already wired)
# - run_doctor / summary_snapshot / list_active_alerts

# 4. Ask operator: "I've read the charter. Current state is <X>. Continuing with <Y>?"
```

---

## Templates that MUST exist (so every artefact carries the charter)

| Template | Path | Purpose |
|---|---|---|
| PR template | `.github/pull_request_template.md` | every PR auto-asks for 15-row + 7-row matrix check |
| Task template | `.claude/plans/friday-may-15-mega/tasks/T00-example-template.md` | every Friday task carries the matrix |
| Plan-item template | (inline in plan files) | every plan item carries 9-box minimum |

---

## Summary (auto-driver one-liner)

> "Sir, this is the FOREVER contract. Every new chat I open, every PR I write, every task I claim — this file is the FIRST thing I read. Same rules: plain Telegram messages, 100% in 15 dimensions, honest envelope (no 'never disconnects' lies), kid-friendly explanations, 3-agent adversarial review on every PR. Mac dev = AWS prod identical. NEVER paraphrase the 100% claim without the envelope qualifier. NEVER use library jargon in operator-facing text. Mechanical gates catch every shortcut. This is the standard for tickvault forever."
