# BruteX Engineering Guarantee — Merge-Readiness Charter

> **Operator decree (2026-05-29).** Standing law for **every** PR, sub-PR,
> task, sub-task, every current & future Claude Code session, and every
> GitHub merge. Decision ID: **DEC-MERGE-GUARANTEE-001** (MIGRATION.md).
>
> **Authority:** this charter sits under `CLAUDE.md` (the 12 Golden Rules
> remain supreme). Where this charter and a Golden Rule appear to conflict,
> the Golden Rule wins — in particular **Golden Rule #10 (Honest limits)**:
> no PR may claim "100% / fully X" unless the enforcing gate is green.

---

## 0. Law of priority (tie-breaker for every decision)

**Correctness > Reliability > Recoverability > Observability > Performance > Scalability.**

When two goals conflict, the higher one wins. "Ultra-fast" never beats
"correct"; "scalable" never beats "recoverable".

## 1. Scope

Applies to **everything** — frontend, backend, DB, APIs, CI/CD, automation,
docs — **except** websocket ticks and live market-feed handling (the one
explicitly-excluded live surface). Track 1 is a local research backtest
engine: there is **no deployed production service, no AWS/cloud, no served
multi-user API** today, so "production runtime monitoring" items below are
**OUT OF SCOPE until such a surface ships** (the Options Track / live feed).

## 2. Enforcement tiers (this is what makes it a *guarantee we can keep*)

Every requirement carries a tier. A PR may only claim a requirement "done"
when its tier is ✅ and the gate is green:

| Tier | Meaning |
|------|---------|
| ✅ **ENFORCED** | A CI gate blocks merge if it fails. Machine-verified. |
| 🟡 **PARTIAL** | Gate exists, not at full coverage; gap tracked in `MIGRATION.md`. |
| 📋 **ROADMAP** | Required target, not yet wired; dedicated follow-up PR. |
| 🚫 **OUT OF SCOPE (until deployed)** | Applies only to a production/live surface that does not exist yet. |

The decree-vs-reality gap is a **living ratchet, not a bypass** (see
`docs/STANDARDS_100PCT.md` §1). Every PR that touches a 🟡/📋 row updates
that row's status.

## 3. Merge-ready pipeline — every change runs the gauntlet

| Stage | Tier | Gate (CI job) |
|-------|------|---------------|
| Code analysis / static scan | ✅ | ruff + pylint |
| Security scan | ✅ | bandit + CodeQL + Semgrep + Trivy + gitleaks + pip-audit |
| Duplicate / dedup detection | ✅ | jscpd + Bloom/LMDB dedup tests |
| Type safety | ✅ | mypy --strict + pyright |
| Coverage (line + branch) | ✅ | 100% on changed `brutex/` modules |
| Mutation | 🟡 | mutmut / cargo-mutants ≥ 85% (critical modules) |
| Performance / O(1) | ✅ | bench NFR gate (p99 ratio ≤ 1.1; absolute = self-hosted) |
| Build + dependency | ✅ | editable build + CycloneDX SBOM + audit |
| Unit / integration / property / golden | ✅ / 🟡 | pytest + hypothesis + fixtures |
| Load / stress / soak / chaos / race / failover | 📋 | nightly ~5-min stress regression gate + weekly self-hosted soak (#1541); chaos/race/failover roadmap |
| DB / API / frontend / integration validation | 🟡 | dashboard + integration tests |
| Runtime / production monitoring | 🚫→📋 | activates when a service is deployed |
| Docstrings / typos / complexity / dead code | ✅ / 🟡 | interrogate + codespell + radon + vulture |
| Conventional commits + decision ID + citations | ✅ | commit-format + DEC-ID + no-hallucination scans |
| Merge validation | ✅ | **`CI OK (aggregate gate)`** = required check |
| Z+ discipline | ✅ | `zplus-pr-guard` (6 markers) |

## 4. Definition of Done (a task / PR is NOT done until ALL hold)

1. Every ✅ gate green **locally and in CI** — verified, not assumed.
2. Every 🟡 / 📋 / 🚫 row it touched has its status updated in `MIGRATION.md`.
3. A **decision ID** is recorded and the PR body carries the 6 Z+ markers
   + an **Honest envelope** disclosing anything not green.
4. No secret ever printed, logged, or committed; audit JSONL chain intact.
5. **Never merged over a red or pending gate.** Red/conflict → diagnose →
   fix → re-push → re-verify → merge.

   **`mergeable_state` envelope (DEC-AUTOMERGE-CLEAN-STRICT-001,
   amends the original strict-`clean` wording):** strict
   `mergeable_state == clean` is the **target**, reachable only once
   server-side branch protection (G2,
   `docs/runbooks/github-branch-protection-setup.md`) exists. Until G2,
   GitHub computes the state from the head's check/status rollup, and at
   the moment the auto-merge gate evaluates, that rollup is usually
   non-passing — on the `ready_for_review` path the gate's own
   in-progress `Auto-merge on green CI` check run structurally ALWAYS
   sits `pending` on the PR head (that path can never observe `clean`);
   on the `workflow_run` path the concurrent duplicate CI run is
   typically still finishing. Measured (2026-06-03→06-10 run-log sample,
   30 verified gate executions): `unstable` 21 — including every
   `ready_for_review`-path merge; `clean` only 4 of 18 verified merges,
   all `workflow_run`-path after every check completed; `dirty`/
   `behind`/`blocked`/`has_hooks` never observed (full table in
   MIGRATION.md DEC-AUTOMERGE-CLEAN-STRICT-001). Requiring strict
   `clean` today would deterministically deadlock the
   `ready_for_review` path and strand green PRs on the rest (the #304
   failure mode DEC-AUTOMERGE-RACE-FIX-001 removed). The **enforced pre-G2 gate** is
   a **denylist**: `mergeable == true` AND `mergeable_state` NOT IN
   `{dirty, behind, blocked, unknown}` — every other value passes:
   `clean`, `unstable` (the pre-G2 normal), and structurally also
   `has_hooks` (GitHub-Enterprise-only pre-receive hooks; not expected
   on github.com) or any future value. Red checks are refused UPSTREAM
   by the all-green ci-check (DEC-AUTOMERGE-ALLGREEN-001: ANY failing
   check run refuses; EVERY `CI OK (aggregate gate)` instance must be
   success) **plus the CI-runs-complete rule (same DEC-ID): every
   push/pull_request CI run on the head must be COMPLETED and at least
   one pull_request-event CI run must be SUCCESS** — added because
   "every CI OK instance green" is existence-blind: PR #571 merged at
   19:56:26 while its pull_request CI run was still in flight (its
   `CI OK` check run did not exist yet; it completed 19:57:03 — runs
   27232004649 / 27231261106 / 27231295196). A tolerated `unstable`
   therefore carries only pending NON-CI checks (the gate's own check
   run and/or advisory third-party/nightly checks, whose completion
   fires no auto-merge trigger and whose FAILURE the failing-check
   filter refuses). **Known blind spot (disclosed, not guarded):**
   legacy commit STATUSES (the pre-check-runs API) are invisible to
   ci-check; no workflow in this repo posts them today — re-verify this
   envelope if a status-posting integration (e.g. codecov) is ever
   added. Pre-G2 no surface may claim "never merges over red" as an
   absolute; the claim is "no failing check run, no missing CI verdict,
   no conflict/stale/blocked/uncomputed state at merge time".
   **Ratchet:** when G2 lands, tighten the allow-list to `clean` only
   (tracked under the same DEC-ID). Pinned by
   `tests/redteam/test_redteam_automerge_safety.py` both ways
   (workflow ↔ charter).

## 5. Auto-handling (agent-in-the-loop — the honest version)

| Problem | Action |
|---------|--------|
| Merge conflict | resolve & re-verify |
| Build / test failure | debug & fix, re-run |
| Coverage drop | block until restored |
| Performance regression | **block** |
| Security risk | **block** |
| Dependency issue | fix & re-audit |

Fully-autonomous auto-merge of **unreviewed** fixes is **forbidden** — it
produced a broken `main` once (a red merge slipped through because the
required check was advisory). A **verify-before-merge** gate stays, and
`CI OK (aggregate gate)` must be a **required** branch-protection check so
GitHub itself blocks a red merge.

## 6. Observability (in-scope today)

Structured logs (`/logs` viewer + rotating file), metrics endpoint,
immutable hash-chained audit JSONL, run dashboard. Production
runtime/latency/CPU/anomaly monitoring is 🚫→📋 (no deployed surface yet).

## 7. O(1) mandate

Per hot path: indexed / filtered queries (never a full scan to display a
subset), the Rust kernel for the billion-combo sweep, Bloom + LMDB O(1)
dedup, u64 mask evaluation. CI enforces `p99(1M)/p99(1K) ≤ 1.1` on hot
paths. **Row count is not latency** — a 100M-row table is fine if each
query is constant-time; the sin is materialising rows you don't display.

## 8. How this charter is enforced on every surface

| Surface | Mechanism |
|---------|-----------|
| Every Claude Code session (current + new) | `CLAUDE.md` §13 points here; auto-loaded each session |
| Every PR / sub-PR on GitHub | `.github/PULL_REQUEST_TEMPLATE.md` §4 checklist + the CI gate matrix in §3 |
| Every merge | `CI OK (aggregate gate)` required status check (branch protection) |
| Every task / sub-task | the §4 Definition of Done, attested before opening a PR |

## 9. Honesty clause (Golden Rule #10, restated)

This charter is the **target**. The **actually-enforced** subset is the ✅
rows above + `docs/STANDARDS_100PCT.md` §1. No PR, comment, or report may
claim "100% / fully / guaranteed" for a 🟡 / 📋 / 🚫 row. State what is
green, what is partial, and what is out of scope — every time.
