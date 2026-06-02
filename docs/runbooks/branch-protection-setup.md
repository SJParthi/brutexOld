# Runbook — enable branch protection on `main` (operator, one-time)

> **Why:** the Merge-Readiness Charter (`docs/MERGE_READINESS_GUARANTEE.md` §3/§8)
> requires **`CI OK (aggregate gate)`** to be a **server-side required status
> check** so GitHub itself blocks a merge over a red/missing gate. Today the guard
> is honored by *convention* — `auto-merge.yml` keys on `CI OK == success` (which
> works: every PR this session merged correctly) — but a human with write access
> could still **manually** merge over a red `CI OK`, because there is no
> branch-protection rule. This runbook closes that gap. **(DEC-BRANCH-PROTECTION-RUNBOOK-001.)**
>
> **Who:** the repo **admin** (operator) — Claude Code cannot set branch
> protection (no admin scope, and the GitHub MCP exposes no protection tool).
> **Time:** ~2 minutes.

---

## The canonical expected config

The single source of truth for the expected protection is the Python dict + checks
in **`.github/workflows/branch-protection-drift.yml`** (it audits the *live* rule
against this daily and opens an issue on drift). Apply a rule that matches it:

- **Required status checks** (`strict: true` — branch must be up to date): the
  full `ci-ok` needs list — 21 contexts, ending with **`CI OK (aggregate gate)`**.
- **`enforce_admins: true`** — admins cannot bypass (the whole point).
- **`required_linear_history: true`** — matches the squash-merge policy.
- **`required_pull_request_reviews`** — present (PRs required). Use
  `required_approving_review_count: 0` so the existing `auto-merge.yml` bot can
  still merge on green `CI OK` (a human-approval requirement would block the bot;
  raise it later if you add reviewers).
- **`required_conversation_resolution: true`**.
- `allow_force_pushes: false`, `allow_deletions: false`.

---

## Apply it (option A — `gh` CLI, recommended)

Run from a machine authenticated as a repo admin (`gh auth status` shows admin):

```bash
gh api -X PUT repos/SJParthi/bruteX/branches/main/protection \
  -H "Accept: application/vnd.github+json" --input - <<'JSON'
{
  "required_status_checks": {
    "strict": true,
    "contexts": [
      "Lint (ruff)",
      "Type check (mypy + pyright)",
      "Unit tests + coverage (100% gate)",
      "Property tests (hypothesis)",
      "Benchmarks (O(1) NFR gates)",
      "Golden-master tests",
      "Security scan (bandit + pip-audit)",
      "Docstring coverage (interrogate)",
      "License compliance",
      "Rust (fno-math build + clippy + test)",
      "Banned pattern scan",
      "Secret scan (gitleaks)",
      "CodeQL (Python SAST)",
      "Semgrep (python + owasp + security-audit)",
      "Trivy (filesystem + config scan)",
      "CycloneDX SBOM generation",
      "Vulture (dead code detection)",
      "jscpd (code duplication)",
      "Python mutation (mutmut on critical modules)",
      "Proof bundle (aggregate CI artifacts)",
      "CI OK (aggregate gate)"
    ]
  },
  "enforce_admins": true,
  "required_pull_request_reviews": { "required_approving_review_count": 0 },
  "required_linear_history": true,
  "required_conversation_resolution": true,
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false
}
JSON
```

> Keep the context strings **byte-identical** to the job `name:`s in `ci.yml`
> (and to the drift-check list) — GitHub matches required checks by display name.

## Apply it (option B — Settings UI)

GitHub → **Settings → Branches → Add branch ruleset / protection rule** for `main`:
require status checks (tick all 21, set "Require branches to be up to date"),
require a PR before merging, require linear history, require conversation
resolution, and **"Do not allow bypassing the above settings"** (= `enforce_admins`).

---

## Verify

```bash
gh api repos/SJParthi/bruteX/branches/main/protection | \
  jq '{strict:.required_status_checks.strict,
       checks:(.required_status_checks.contexts|length),
       enforce_admins:.enforce_admins.enabled,
       linear:.required_linear_history.enabled,
       reviews:(.required_pull_request_reviews!=null),
       conversation:.required_conversation_resolution.enabled}'
```

Expect `strict:true, checks:21, enforce_admins:true, linear:true, reviews:true,
conversation:true`. The daily **`branch-protection-drift.yml`** run will then pass
(no drift issue); if anyone later weakens the rule it re-opens a drift alert.

> **Honest envelope (Golden Rule #10):** until this is applied, `CI OK` is enforced
> by the auto-merge script + the pre-merge bot (convention), not by GitHub. Applying
> this rule is the only step that makes it a *mechanical* server-side guarantee.
