# Claude Code automation playbook (DEC-CLAUDE-AUTOMATION-001)

> How BruteX automates the build → review → CI → merge loop so a Claude Code session (CLI,
> desktop, or web) ships fully-gated PRs with minimal manual intervention. Honest envelope: the
> rows below are tagged **Verified** (proven in this repo), **Documented** (standard Claude Code
> mechanism, format-correct here, but end-to-end consumption by a given runtime not verified from
> inside a session), or **Manual** (a one-time step only a human with repo-admin can do).

## 1. What is ALREADY automated (Verified — these run today)

| Surface | Mechanism | Evidence |
|---|---|---|
| 60+ CI gates on every push | `.github/workflows/ci.yml` (ruff, mypy, pyright, pytest 100% gate, property, mutation, benchmarks, bandit/pip-audit/Semgrep/CodeQL/gitleaks/Trivy, jscpd, vulture, radon, docstring, Z+/DoR/DEC-ID/no-hallucination guards, Rust) | every merged PR (#783–#786) passed it |
| Zero-click merge | **Auto-merge on green CI** workflow | merged #783, #784, #785, #786 hands-free |
| Pre-merge verdict bot | `.github/workflows/pre-merge-gate.yml` posts ✅ MERGE-READY / ❌ BLOCKED | seen on every PR |
| Per-session project briefing | `.claude/hooks/session-start.sh` (SessionStart hook) | prints branch + last 5 DEC ids + env + gates |
| Auto-lint on edit | `.claude/settings.json` PostToolUse (`ruff check --fix` on every `.py` write) | in `settings.json` |
| Edit-on-main guard | `.claude/settings.json` PreToolUse (blocks edits on `main`) | in `settings.json` |
| Permission allow/deny | `.claude/settings.json` (force-push / `reset --hard` / `--amend` denied) | in `settings.json` |

## 2. What this PR ADDS

| Artifact | Status | What it does |
|---|---|---|
| `.claude/agents/hostile-reviewer.md` | Documented | Codifies the adversarial diff review (correctness, security, O-complexity, **coverage honesty**) run on every PR this session. |
| `.claude/agents/workspace-health.md` | Documented | Codifies the independent evidence-based "whole repo still works" verifier (the 7,747-test sweep). |
| `.claude/skills/ship-pr/SKILL.md` | Documented | The repeatable ship→merge loop (pre-flight gates → parallel review → markers → draft→ready → monitor→merge → post-merge). |

**Documented vs Verified:** project subagents (`.claude/agents/*.md`) and skills (`.claude/skills/`)
are the standard Claude Code mechanism with format-correct frontmatter; they are honored by the
CLI/desktop. Whether a given **remote/web** session auto-discovers repo-defined subagent *types*
is runtime-dependent and was NOT verified from inside this session — so they are tagged
**Documented**, not Verified, per CLAUDE.md Rule #1. They are additive + harmless if a runtime
ignores them; the parallel-review pattern is ALSO invoked explicitly each PR regardless.

## 3. Cloud auto-fix GitHub Action — RETIRED

> ⚠️ **Retired 2026-07-06 (DEC-CLOUD-AUTOFIX-RETIRE-001).** The opt-in cloud auto-fix design
> that lived in this section — a `.github/workflows/claude-autofix.yml` snippet using
> `anthropics/claude-code-action@v1`, shipped dormant by DEC-CLAUDE-AUTOMATION-001
> (MIGRATION.md #831: "job-gated off by a `CLAUDE_AUTOFIX_ENABLED` repo variable so it stays
> dormant until enabled") — is **withdrawn**. It was never wired: no workflow file, no secret,
> no repo variable, no test ever existed (grep re-verified 2026-07-07 against the committed
> tree — the 18 files in `.github/workflows/` carry zero references to
> `claude-autofix` / `CLAUDE_AUTOFIX` / `claude-code-action` / `ANTHROPIC_API_KEY`; the only
> repo-wide hits are the historical ledger entry #831 plus this retirement's OWN artifacts —
> this banner, ledger entry #1539, and the deliberate `_RETIRED_AUTOFIX_SNIPPET` negative
> teeth fixture in `tests/redteam/test_redteam_fork_pr_harden.py`; before this retirement
> the hits were confined to the original §3 snippet + ledger entry #831 — no secret, no
> variable, no wiring). Do not resurrect the snippet from
> git history without meeting the revival preconditions below.
>
> **Why retired — the four disqualifiers:**
>
> 1. **Never wired + redundant.** Dormant since 2026-06-19 (#831) with zero uptake, while §1's
>    zero-click merge loop (auto-merge on green CI) already works without it and CI fixes are
>    performed in operator-driven Claude sessions — marginal value is low.
> 2. **Security-regression vector.** As written, the snippet combined an
>    `issue_comment: created` trigger (base-repo context, attacker-authored input on this
>    PUBLIC repo) with `secrets.ANTHROPIC_API_KEY` +
>    `permissions: { contents: write, pull-requests: write, issues: write }` and NO
>    author-trust gate — re-opening exactly the privileged-trigger class
>    DEC-CI-FORK-PR-HARDEN-001 (MIGRATION.md #1180) closed. And author-trust gating alone does
>    NOT bound the prompt-injection envelope of an agentic write-capable job whose prompt
>    surface is comment/PR text.
> 3. **Sweep blind spot.** The #1180 redteam directory sweep
>    (`tests/redteam/test_redteam_fork_pr_harden.py`) scanned only
>    `workflow_run`/`pull_request_target` triggers, so shipping the snippet verbatim would have
>    bypassed the guard suite that hardened the repo. That gap is now closed independently of
>    this design: `test_no_issue_comment_privileged_workflows` fail-closed-pins that no
>    comment-family-triggered workflow may hold write permissions, reference a secret other
>    than the implicit `secrets.GITHUB_TOKEN`, check out a PR-controlled ref, or omit its
>    `permissions:` block entirely (the repo-DEFAULT `GITHUB_TOKEN` it would inherit is
>    read/write on an un-hardened repo — statically unboundable) without a #1180-pattern
>    trust gate first.
> 4. **Limbo arithmetic.** Enablement is owner-manual (§4: a session cannot add repo secrets or
>    flip branch-protection/variables), so shipping the workflow would merely convert
>    doc-limbo into disabled-workflow-limbo — and the snippet's action inputs were themselves
>    self-declared UNVERIFIED, which Golden Rule #1 forbids encoding.
>
> **Revival preconditions** (a fresh DEC + a Golden Rule #9 4-agent hostile review are
> REQUIRED; all five must be satisfied before any auto-fix workflow lands):
>
> 1. Live verification of `anthropics/claude-code-action`'s CURRENT release inputs
>    (Golden Rule #1 — never encode unverified inputs).
> 2. The #1180 author-trust check (the `auto-merge.yml` `trust-check` step — explicit
>    allowlist `SJParthi`/`dependabot[bot]`/`renovate[bot]` OR live `author_association` ∈
>    OWNER/MEMBER/COLLABORATOR) EXTRACTED into ONE COMMON `scripts/ci/` script consumed by
>    BOTH `auto-merge.yml` and the new workflow (Golden Rule #11 — the extraction is
>    deliberately deferred to revival time: after this retirement there is no second
>    consumer, so extracting now would be a speculative refactor of a live security-critical
>    workflow).
> 3. The trust gate runs BEFORE any checkout of a PR-controlled ref (the #1180 H2
>    base-pinned-checkout law).
> 4. The extended comment-trigger redteam sweep
>    (`test_no_issue_comment_privileged_workflows`) passes against the new workflow.
> 5. The #1180 owner-side server settings (branch ruleset for `main` + Actions → "Require
>    approval for all outside collaborators" — the exact click-path is recorded in
>    MIGRATION.md #1180's honest-limits follow-up) confirmed DONE by the operator.
>
> The retired snippet remains recoverable from git history if ever needed; its exact shape
> survives as the negative teeth fixture in
> `tests/redteam/test_redteam_fork_pr_harden.py` so the sweep can never regress silently.

## 4. Honest limits (Golden Rule #10)
- A session **cannot** add repo secrets or flip branch-protection — those are Manual.
- Re-triggering CI into a GitHub-infra outage (crates.io / artifact endpoints) is futile churn;
  wait for recovery rather than spam empty commits (the `ship-pr` skill §5 encodes this).
- Nothing here weakens a gate; automation only ADDS verification + removes manual clicks.
