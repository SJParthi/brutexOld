# BruteX — 100% Always Standards (Project Law)

**Status:** LOCKED 2026-05-16. Operator decree.

**Applies to:** every branch, every PR, every sub-PR, every commit,
every task, every sub-task, every plan, every doc, every spec, every
agent run, every research round — for the lifetime of the BruteX
project.

**Single source of truth for project standards.** Supersedes ad-hoc
quality rules scattered across other docs. Cross-references
preserved as pointers, not duplications.

---

## 0. The decree

> *"100 percentage dude" — extensive, comprehensive, intensive, deep
> thorough. Code coverage, code scanning, code duplication,
> functionality missing, scenario missing, bug fixing, issues finding,
> testing types, testing coverage, extensive comprehensive real-time
> testing, code performance, uniqueness, deduplication, O(1) latency
> always. Guarantee, assurance, proof — no hallucination, no illusion,
> no missing.*

This document operationalizes that decree as concrete, CI-enforced
gates with named tooling.

---

## 1. The 18 standing requirements

| # | Requirement | Concrete gate | Tooling | Failure mode caught |
|---|---|---|---|---|
| 1 | **Code coverage** — line | 100% on `brutex/` package | `pytest --cov-fail-under=100 --cov-branch` | dead code, untested branches |
| 2 | **Code coverage** — branch | 100% on `brutex/` package | same as above with `--cov-branch` | unreachable conditions, missing if-else |
| 3 | **Mutation testing** | ≥ 85% mutation kill rate | `mutmut`, `cargo-mutants` | tests that don't actually exercise the code |
| 4 | **Type checking** | `mypy --strict` AND `pyright` both pass | both tools in pre-commit + CI | implicit Any, unchecked Nones |
| 5 | **Static analysis** | `ruff check --fix` + `pylint --fail-under=10` | `.ruff.toml` + `.pylintrc` | dead imports, unused vars, smells |
| 6 | **Security scanning** | `bandit` zero high/critical | bandit on every push | hardcoded secrets, SQL injection patterns, weak crypto |
| 7 | **Code duplication** | zero copy-paste | `pylint --enable=duplicate-code --min-similarity=8`, `jscpd`, `vulture` for dead code | DRY violations, parallel implementations |
| 8 | **Functionality completeness** | every spec section has ≥1 test | spec-↔-test crosswalk script in CI | "spec says X but no test for X" |
| 9 | **Scenario completeness** | edge cases enumerated + tested | `hypothesis` property tests + golden fixtures | off-by-one, boundary errors, NaN handling |
| 10 | **Bug-fix regression coverage** | every fixed issue gets a regression test | PR template requires "failing test before fix" | bug reappears in different shape |
| 11 | **Test types** | unit + integration + property + golden + bench + stress + mutation | pytest markers; GitHub workflow matrix | one type of bug class undetected |
| 12 | **Real-time CI** | runs on every push + nightly soak | `.github/workflows/*.yml`, scheduled triggers | slow regressions, time-of-day bugs |
| 13 | **Performance — O(1)** | p99(N=1M) / p99(N=1K) ≤ 1.1 per hot path | `pytest-benchmark` CI gate | algorithm complexity creep |
| 14 | **Performance — throughput** | sweep canary ≥ 50 M evals/s aggregate on M4 Pro | `tests/bench/test_full_sweep_extrapolation.py` | engine throughput regression |
| 15 | **Uniqueness — combo dedup** | Bloom + LMDB 4-tier defence | property test P-DEDUP-1 | duplicate trial work, polluted leaderboard |
| 16 | **Deduplication — at every layer** | data + results + audit + code | property tests P-DEDUP-{2,3,4,5} | duplicate ingest bars, double-counted PnL |
| 17 | **No hallucination** | every claim has primary source citation | manual review gate + Golden Rule #1 | invented facts, drifted assumptions |
| 18 | **Audit trail completeness** | every material action emits JSONL hash-chain row | `audit_chain_intact` doctor check + P-AUDIT-1 | silent state change, replay-oracle break |

---

## 2. Observability — non-negotiable on every PR

Per the existing `docs/track2/TRACK2_OBSERVABILITY.md` spec (DEC-OBS-001),
every PR must preserve the 4 pillars:

| Pillar | Mechanism | SLA |
|---|---|---|
| **Tracking** | Audit JSONL hash-chained | every material event recorded within 100 ms |
| **Logging** | `structlog` only (no `print`) | uniform structured fields |
| **Dashboard** | FastAPI on `127.0.0.1:9080` (9 pages) | accurate within 5 sec of underlying state |
| **Alerting** | Telegram + dashboard banner + email digest + macOS fallback | CRITICAL alert reaches operator within 60 sec |

Plus `brutex doctor` daily 07:30 IST (12 C-checks) + Prometheus
`/metrics` on `127.0.0.1:9081`.

---

## 3. Deep-research protocol — major decisions

Per Golden Rule #9 + this addendum:

| Decision class | Minimum agents | Pattern |
|---|---:|---|
| Major architecture change | 4 | Plan + general + general + Explore (parallel hostile + consistency) |
| Methodology choice (DSR, CPCV, etc.) | 4 | 4-agent literature audit including 2024-2026 papers |
| Cost-model change | 4 | rates / contract notes / rounding / edge cases |
| Risk / compliance change | 2 | SEBI/RBI/IT-Act primary sources |
| Bug-fix or refactor | 1 | hostile review by Plan agent |
| Doc-only edit | 0 | direct edit OK; must log to MIGRATION.md |

Decision IDs in MIGRATION.md are MANDATORY for any locked decision.

---

## 4. Architecture mandate (COMMON · DYNAMIC · SCALABLE · AUTOMATED)

Per Golden Rule #11. Every module obeys all 5:

| Principle | Definition | Enforcement |
|---|---|---|
| **COMPREHENSIVE** | streaming + batch interfaces; bit-exact oracle test; look-ahead invariant test; serialize/restore | property tests P-COMP-{1,2,3,4} |
| **COMMON** | one source of truth (this repo); shared via `pip install git+...@v0.X.Y`; NEVER copy-paste | enforced by `docs/SHARED_LIBRARY.md` |
| **DYNAMIC** | add/remove indicator/TF/condition/mask without re-architecting | DEC-PLUGIN-001 + DEC-PLUGIN-002 |
| **SCALABLE** | same code path Day 1 (12.6M trials) → Phase 10 (730B); O(1) per-bar / per-mask / per-dedup / per-write | benchmark gates 13 + 14 above |
| **AUTOMATED** | 100% automated after one-time bootstrap; Release Please + auto-merge + watchdog + Telegram + auto-resume | zero operator clicks/day target |

---

## 5. PR gate matrix (what every PR must show GREEN)

| Gate | Tool | Threshold | Owner |
|---|---|---|---|
| Unit tests | pytest | 100% pass, ≥ 1188 baseline | CI |
| Coverage line | pytest-cov | 100% | CI |
| Coverage branch | pytest-cov --cov-branch | 100% | CI |
| Mutation score | mutmut | ≥ 85% | CI nightly |
| Type check Python | mypy --strict | 0 errors | pre-commit |
| Type check supplementary | pyright | 0 errors | pre-commit |
| Lint | ruff check + pylint | 10.00/10 | pre-commit |
| Security | bandit | 0 high/critical | CI |
| Duplication | jscpd | < 1% similarity | CI |
| Dead code | vulture | 0 hits on `brutex/` | CI |
| Benchmarks | pytest-benchmark | p99 ratio ≤ 1.1 | CI nightly |
| Property tests | hypothesis | 0 falsifications | CI |
| Golden fixtures | pytest -m golden | 100% byte-equality | CI |
| Audit chain | `brutex doctor C2` | green | CI |
| Conventional commit msg | commitlint | passes | pre-commit |
| MIGRATION.md entry | manual review | every locked decision has entry | reviewer |

If ANY gate is red, the PR is BLOCKED. Operator override requires
ADR.

---

## 6. Per-task / per-sub-task discipline

Every task — no matter how small — follows this sequence:

| Phase | Action | Output |
|---|---|---|
| 1. Plan | TodoWrite breakdown into sub-tasks | tracked todo list |
| 2. Research | If decision needed → 4-agent or 2-agent | findings + primary sources |
| 3. Spec | Write or update doc with decision ID | spec change → MIGRATION.md entry |
| 4. Code | Implement against spec; never against memory | tests pass on first commit |
| 5. Test | Unit + property + golden + bench | all coverage gates green |
| 6. Audit | Append audit row for material change | hash-chain extended |
| 7. Commit | Conventional commit msg + signed | commit lands |
| 8. Push | `git push -u origin <branch>` | remote updated |
| 9. PR / Update | Update PR description + status | reviewers notified |

No phase skipped. No shortcuts.

---

## 7. Documentation discipline

| Doc type | Required sections |
|---|---|
| Spec (`*_SPEC.md`) | Status, Scope, Out-of-scope, Algorithm, API surface, Test fixtures, UNVERIFIED items, Sources, Amendment log |
| Plan (`*_PLAN.md`) | Status, Phases, Per-phase deliverables, Dependencies, Risks |
| Verified (`*_VERIFIED.md`) | Verification provenance, Locked values, Citations per value, UNVERIFIED items |
| Operational (`*_GUARANTEES.md`, `*_OBSERVABILITY.md`) | Guarantees, SLAs, Property invariants, Failure modes |
| Migration (`MIGRATION.md`) | Decision ID, Date, Rationale, Source citation, Files touched |

Every doc must be readable by both:
- Senior quant (precision + primary sources)
- Auto-driver test (the gist understandable to a non-technical reader)

Tables preferred over prose where they fit. Insta-reel-style summaries
allowed for at-a-glance comprehension, but never replace precision.

---

## 8. Real-time guarantees (the contract)

| Guarantee | SLA | Verification |
|---|---|---|
| Every material action audited | within 100 ms | unit test on audit emitter |
| Hash chain intact | always (zero tolerance) | `brutex doctor C2` daily |
| CRITICAL alert reaches operator | within 60 sec | Telegram bot heartbeat + delivery receipt |
| Dashboard accurate | within 5 sec of underlying state | WebSocket push test |
| `brutex doctor` runs every workday | 07:30 IST cron | systemd timer + alert if missed |
| Backtest reproducible | bit-for-bit identical | `audit replay` CLI |
| No data loss on crash | guaranteed via checkpointing | property test P-AUDIT-1 |
| O(1) per hot path | p99(1M)/p99(1K) ≤ 1.1 | bench gate per push |
| Combo dedup unique | guaranteed via 4-tier defence | property test P-DEDUP-1 |
| No hallucination | every claim cited | manual gate + Golden Rule #1 |

Any SLA violation → operator gets a CRITICAL Telegram alert.

---

## 9. Plain-English mandate (the auto-driver test)

Every spec / decision / output passes the auto-driver test:

> "Auto-bhaiya, kya samjha?" — if the operator's auto-driver can repeat
> back the core idea after one paragraph, the doc is clear enough.

If the doc requires the reader to load 5 prior files of context to
understand a single decision, it fails. Use:
- Tables over prose
- Cross-references over duplication
- Examples over abstractions
- "What this means in plain rules" sections

---

## 10. Operator promise

By honoring this standards document on every PR, the operator gets:

1. **Guarantee:** code on main is at the documented quality bar
2. **Assurance:** the decision ledger captures every material choice
3. **Proof:** CI gates + property tests + audit chain are the evidence
4. **Reproducibility:** any past run replays bit-for-bit
5. **No hallucination:** every claim has a primary source
6. **Auto-driver readable:** any future reader can understand the why

Per Golden Rule #1 (no hallucination): nothing in this doc is invented.
Every gate references a real tool, every SLA references a real
mechanism, every cross-ref points to a real spec section.

---

## 11. Amendment log

| Date | Change |
|---|---|
| 2026-05-16 | Initial draft — codifies operator decree on 100% always standards into project law |
