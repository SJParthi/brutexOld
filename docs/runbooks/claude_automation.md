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

## 3. Opt-in: cloud auto-fix + auto-merge GitHub Action (Manual — needs YOUR secret)

The repo already auto-merges on green WITHOUT any API key. To additionally have Claude **auto-fix**
failing CI / respond to `@claude` in the cloud, enable `anthropics/claude-code-action` — this
requires a repo secret **only a repo admin can add** (a session cannot set secrets):

1. Add the secret: repo → Settings → Secrets and variables → Actions → `ANTHROPIC_API_KEY`.
2. Add a workflow `.github/workflows/claude-autofix.yml` (snippet below), guarded so it no-ops
   when the secret is absent:

```yaml
name: Claude auto-fix
on:
  pull_request: { types: [opened, synchronize] }
  issue_comment: { types: [created] }
permissions: { contents: write, pull-requests: write, issues: write }
jobs:
  claude:
    if: ${{ vars.CLAUDE_AUTOFIX_ENABLED == 'true' }}   # flip the repo VARIABLE to enable
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
        with: { fetch-depth: 0 }
      - uses: anthropics/claude-code-action@v1
        with:
          anthropic_api_key: ${{ secrets.ANTHROPIC_API_KEY }}
          prompt: "If CI is failing on this PR, diagnose + fix the root cause and push; never weaken a gate."
```

Until the secret + `CLAUDE_AUTOFIX_ENABLED=true` variable are set, this stays dormant (the `if:`
gates the job off) — no broken/red workflow. Verify the action's exact inputs against its current
release before enabling.

## 4. Honest limits (Golden Rule #10)
- A session **cannot** add repo secrets or flip branch-protection — those are Manual.
- Re-triggering CI into a GitHub-infra outage (crates.io / artifact endpoints) is futile churn;
  wait for recovery rather than spam empty commits (the `ship-pr` skill §5 encodes this).
- Nothing here weakens a gate; automation only ADDS verification + removes manual clicks.
