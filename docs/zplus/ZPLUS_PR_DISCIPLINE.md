# Z+ PR Discipline — going-forward template (DEC-P0-014, PR #36)

> **Authority:** CLAUDE.md > operator-charter-forever.md §C/§E/§H > this file.
> **Scope:** EVERY PR Claude opens in this repo from PR #37 onwards.
> **Operator-locked:** 2026-05-17 ("i need the gauarnteed z plus defensive layer
> motherfucker"). Verbatim demand preserved in MIGRATION.md DEC-P0-014.

This is the canonical PR-body template + 4-agent review checklist for the
Z+ defensive layer. The mechanical ratchet (`scripts/ci/zplus-pr-guard.sh`)
refuses any PR body missing the 6 required marker phrases. If you find
yourself wanting to bypass — STOP and run the 4-agent review instead.

---

## The 6 mandatory marker phrases (parsed verbatim by the guard)

```
Z+ 15-row matrix
Z+ 7-row resilience matrix
Z+ 7-layer audit
4-agent hostile review
Honest envelope
Decision ID
```

Case-insensitive; substring match. Absent = guard blocks merge.

---

## PR body template (copy-paste, fill in the [REPLACE] markers)

```markdown
## Summary

[REPLACE: 1-paragraph what + why]

## Items completed

- [x] [REPLACE: item N — one line]

## Decision ID

DEC-P0-NNN (added to docs/MIGRATION.md entry #NNN). Tracks back to
[REPLACE: parent decision].

---

## Z+ 15-row matrix (100% everything — charter §C)

| Demand | This PR's proof | Real-time check |
|---|---|---|
| 100% code coverage | [REPLACE: pytest --cov shows N% on touched modules] | scripts/coverage-gate.sh |
| 100% audit coverage | [REPLACE: every material action emits audit JSONL line OR N/A docs-only] | mcp__github__... |
| 100% testing coverage | [REPLACE: unit + integration + property + golden + bench + stress + mutation OR N/A] | pytest --strict-markers |
| 100% code checks | [REPLACE: ruff + mypy strict + pyright strict + bandit + pip-audit pass] | pre-push hooks |
| 100% code performance | [REPLACE: O(1) ratio bench OR N/A] | pytest-benchmark |
| 100% monitoring | [REPLACE: structlog event added OR N/A] | grep brutex/observability/ |
| 100% logging | [REPLACE: structlog event with `event=...` field OR N/A] | grep -r structlog |
| 100% alerting | [REPLACE: alert rule wired OR N/A] | brutex/observability/ |
| 100% security | [REPLACE: bandit + secret-scan + Secret<T> enforcement] | pre-push gate |
| 100% security hardening | [REPLACE: per-line noqa with rationale OR N/A] | grep noqa |
| 100% bug fixing | [REPLACE: 4-agent review evidence below] | this section |
| 100% scenarios covering | [REPLACE: failure-mode test added OR N/A] | grep -r failure |
| 100% functionalities covering | [REPLACE: every new pub fn has test] | pre-push gate |
| 100% code review | [REPLACE: 4-agent review summary below] | this section |
| 100% extreme check | [REPLACE: all of above + ratchet test prevents regression] | every commit |

## Z+ 7-row resilience matrix (charter §C)

| Demand | Honest envelope | This PR's proof |
|---|---|---|
| Zero ticks lost | N/A — BruteX is backtest-only (Track 1) | [REPLACE: confirmed or specific impact] |
| WS never disconnects | N/A — no WebSocket in Track 1 | [REPLACE: confirmed] |
| Never slow/locked/hanged | [REPLACE: no hot-path allocation OR N/A docs/test-only] | grep -r ".clone()/Vec::new()" |
| QuestDB never fails | N/A — BruteX uses DuckDB + LMDB | [REPLACE: confirmed] |
| O(1) latency | [REPLACE: bench passing OR N/A] | scripts/bench-gate.sh |
| Uniqueness + dedup | [REPLACE: composite-key uniqueness preserved OR N/A] | grep DEDUP |
| Real-time proof | [REPLACE: this PR's proof artifact link OR N/A] | this PR body |

## Z+ 7-layer audit

| Layer | This PR's status |
|---|---|
| DETECT | [REPLACE: what detects regression of this PR's change?] |
| VERIFY | [REPLACE: what test verifies the new behavior end-to-end?] |
| RECONCILE | [REPLACE: how does this PR reconcile with existing state? Or "additive only"] |
| PREVENT | [REPLACE: what mechanical gate prevents future re-introduction of the fixed bug?] |
| AUDIT | [REPLACE: what audit-log entry records this change?] |
| RECOVER | [REPLACE: rollback path — git revert sha + revert-test] |
| COOLDOWN | [REPLACE: post-merge soak window OR "test-only / docs-only — no soak needed"] |

## 4-agent hostile review

| Agent | Lens | Findings | Severity |
|---|---|---|---|
| Hot-path | perf + O(1) + allocation | [REPLACE: agent's findings summary] | [REPLACE: count] |
| Security | secrets + injection + supply-chain | [REPLACE] | [REPLACE] |
| Correctness | race + edge-trigger + false-OK + reproducibility | [REPLACE] | [REPLACE] |
| Cite/path | every cited path + symbol + line + decision ID verified | [REPLACE] | [REPLACE] |

Review evidence (links to agent transcripts or inline summaries): [REPLACE].

---

## Honest envelope (Golden Rule #10)

- ✅ REAL: [REPLACE: things actually verified against the codebase NOW]
- 🟡 PARTIAL: [REPLACE: things partially shipped, with explicit gap statement]
- 📋 ROADMAP: [REPLACE: things explicitly deferred to future PRs]

Anti-pattern check: every claim above cites a real file path/line/test.
NO claim is based on a hypothetical artifact. Verified via 4-agent
cite/path verifier.

## Test plan

- [x] [REPLACE: ruff check brutex tests — clean]
- [x] [REPLACE: mypy --strict brutex — clean]
- [x] [REPLACE: pyright brutex tests — clean]
- [x] [REPLACE: pytest -q — N passed, M skipped]
- [x] [REPLACE: scripts/ci/zplus-pr-guard.sh — pass]

https://claude.ai/code/session_0138FNLirm91ykCAqqWYS9UP
```

---

## The 4-agent hostile review (operator-charter-forever.md §E)

Spawn 4 agents IN PARALLEL with these specific lenses. Each agent runs on
the diff (`git show <sha>` for merged PRs, or staged diff for in-progress).
The 4 lenses are mutually exclusive — each catches what the others miss.

### Agent 1: Hot-path / Performance / O(1)

**Prompt skeleton:**
> You are doing a hostile Z+ hot-path review on PR [N] (sha [SHA]) at
> /home/user/bruteX. Lens: HOT-PATH PERFORMANCE + O(1) discipline + memory.
> Check: hot-path allocation, list.copy/dict.copy/.clone() in production
> paths, perf regression risk, O(n) accidentally introduced where O(1)
> required (Rule #5). Report CRITICAL/HIGH/MEDIUM/LOW. 400 words max.

### Agent 2: Security

**Prompt skeleton:**
> You are doing a hostile Z+ security review on PR [N] (sha [SHA]).
> Lens: SECRET EXPOSURE + injection + path traversal + supply-chain
> + bandit S-rule patterns. Check: pip-audit-style CVE heuristics on
> dep changes, weakened security ignores, widened API surface. Report
> CRITICAL/HIGH/MEDIUM/LOW. 400 words max.

### Agent 3: Correctness / Race / Audit

**Prompt skeleton:**
> You are doing a hostile Z+ correctness review on PR [N] (sha [SHA]).
> Lens: race conditions + missing market-hours gate + edge-trigger
> + false-OK + look-ahead violations + audit-trail integrity +
> reproducibility (Rule #4 sha256). Report CRITICAL/HIGH/MEDIUM/LOW.
> 500 words max.

### Agent 4: Cite/Path Verification

**Prompt skeleton:**
> You are doing a hostile Z+ cite/path verification on PR [N] (sha [SHA]).
> Verify EVERY cited file path, test name, function name, line number,
> decision ID, PR reference in the PR body + commit message + MIGRATION.md
> entry ACTUALLY EXISTS in the codebase. Report each as VERIFIED or FAILED
> with evidence. 600 words max.

---

## When to run the review

- **BEFORE** the implementation lands (Plan phase): spawn 4 agents on the
  intended-design doc to catch architectural CRITICAL/HIGH early.
- **AFTER** the implementation (pre-merge): re-run on the diff to catch
  what slipped through implementation.

For sub-1000-LoC test/docs-only changes: ONE pass (post-impl) is acceptable.
For anything touching `brutex/orchestrator/`, `brutex/engine/`,
`brutex/data/`, `brutex/feed/`, `brutex/strategies/`, `brutex/vocabulary/`,
`brutex/indicators/`: BOTH passes mandatory.

---

## Honest envelope on this discipline doc itself

- ✅ REAL: `scripts/ci/zplus-pr-guard.sh` enforces the 6 marker phrases
  via fail-closed exit 1 (+ OPERATOR_OVERRIDE escape with audit log).
- ✅ REAL: PR #36 retroactively audits PRs #33, #34, #35 with 4 agents.
- 🟡 PARTIAL: the guard checks marker-phrase PRESENCE, not the QUALITY of
  the matrix content. A PR with "Z+ 15-row matrix: TODO" would still pass
  the guard. Human review remains required for content quality.
- 📋 ROADMAP: integrate the guard into `.github/workflows/pre-merge-gate.yml`
  so it runs as a required CI check (currently invokable only locally).
  Tracked as P2 follow-up — will land in PR #37+.
- 📋 ROADMAP: add a corresponding `pre-merge-zplus-check.yml` workflow that
  reads the PR body via `pull_request_target` event + invokes the guard.

---

## Decision ID

DEC-P0-014 (added to `docs/MIGRATION.md` entry #152). Source: operator
verbatim decree 2026-05-17.
