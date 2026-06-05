# GitHub Branch Protection — Z+ Defensive Setup Runbook

> **Audience:** Repo owner (operator).
> **Frequency:** One-time setup; revisit when CI gate list changes.
> **Why this exists:** The `scripts/ci/pre-merge-check.sh` script is
> CLIENT-SIDE — Claude must remember to call it. GitHub Branch
> Protection is SERVER-SIDE — GitHub itself refuses merges that
> violate the rules. This is the L4 PREVENT layer of the Z+ Defense
> Doctrine; without it, the lock is only as strong as Claude's
> discipline (which failed once on PR #25).
>
> **Outcome:** AFTER this setup completes (one-time, ~5 min of
> operator clicks in GitHub Settings UI), even if Claude calls
> `mcp__github__merge_pull_request` while CI is red, GitHub will
> REJECT the merge at the API boundary. **Honest envelope: this
> file describes the intended state; the mechanical guarantee is
> 📋 ROADMAP until §1-6 below are completed by the operator.**
> Until then, the only enforcement is the client-side
> `scripts/ci/pre-merge-check.sh` script (Claude-discipline-dependent).

---

## Prerequisites

- Owner-level access on `SJParthi/bruteX`
- A browser session at https://github.com/SJParthi/bruteX

---

## Step-by-step setup (5 minutes)

### 1. Open the Branch protection rules page

Navigate: https://github.com/SJParthi/bruteX/settings/branches

Click **"Add branch protection rule"** (or **"Edit"** on an existing
`main` rule).

### 2. Set the branch name pattern

```
Branch name pattern: main
```

### 3. Required status checks (the heart of Z+ PREVENT)

Tick: **"Require status checks to pass before merging"**

Tick: **"Require branches to be up to date before merging"**

In the search box, add **EACH** of the following status checks
(they appear after the first CI run on a PR — push any commit
to a PR branch first if the list is empty):

**Hard gates (MUST be on the list):**

- [ ] `CI OK (aggregate gate)` — the aggregate sentinel that bundles
      every other gate's result. **If you only have time to add ONE
      check, add this one.**

**Individual gates (recommended; redundant with the aggregate but
explicit is better than implicit):**

- [ ] Lint (ruff)
- [ ] Type check (mypy + pyright)
- [ ] Unit tests + coverage (100% gate)
- [ ] Property tests (hypothesis)
- [ ] Benchmarks (O(1) NFR gates)
- [ ] Golden-master tests
- [ ] Security scan (bandit + pip-audit)
- [ ] Docstring coverage (interrogate)
- [ ] License compliance
- [ ] Rust (fno-math build + clippy + test)
- [ ] Banned pattern scan
- [ ] Secret scan (gitleaks)
- [ ] CodeQL (Python SAST)
- [ ] Semgrep (python + owasp + security-audit)
- [ ] Trivy (filesystem + config scan)
- [ ] CycloneDX SBOM generation
- [ ] **Vulture (dead code detection)** — added in DEC-P0-001
- [ ] **jscpd (code duplication)** — added in DEC-P0-001
- [ ] **Python mutation (mutmut on critical modules)** — added in DEC-P0-001
- [ ] **Proof bundle (aggregate CI artifacts)** — final artifact step
- [ ] **make run smoke (cold-start boot)** — added in DEC-RUN-SMOKE-GATE-001; the ONLY gate that boots the app from a clean clone (`.[data,ui]` + kernels, no `[dev]`), so "100% green but `make run` is broken" can never recur

**Do NOT add (these run on nightly cron only — would block ALL PR merges if listed):**
- ~~Red-team harness — verify gates actually block~~ — defined in `.github/workflows/redteam-nightly.yml`, runs on `schedule: cron "0 2 * * *"` + `paths` filter on `tests/redteam/**`. Per Agent 4 of the DEC-P0-004 hostile-review pass: if you add this as a required check, GitHub will block every PR forever because the job doesn't run on most PRs. Wire it to all PRs first (separate PR), then add here.

**Advisory-only (do NOT add — these are informational):**
- ~~Typos + codespell~~
- ~~Radon complexity + maintainability~~
- ~~fno-math-drift~~
- ~~Conventional commit format~~ (skip if optional)

### 4. PR & merge requirements

Tick: **"Require a pull request before merging"**

Sub-options:
- [ ] **"Require approvals"** — set to `0` (you're solo; this is
      gate redundancy, not human review)
- [ ] **"Dismiss stale pull request approvals when new commits are pushed"**
- [ ] **"Require conversation resolution before merging"** — forces
      ALL review threads resolved before merge

Tick: **"Require linear history"** — forces squash or rebase
(matches BruteX policy of squash merges).

Tick: **"Require signed commits"** — optional but strongly
recommended for supply-chain hardening.

### 5. The teeth (closes admin bypass loophole)

Tick: **"Do not allow bypassing the above settings"** — this is
the SINGLE MOST IMPORTANT checkbox. Without it, repo admins
(including you) can merge red PRs from the web UI. With it,
even you have to fix the red checks first.

**This is the Z+ PREVENT layer's killshot.** It means:
- No one can merge a red PR (including Claude via API)
- No one can push directly to main (must go through PR)
- No one can bypass via "force push"
- No one can delete the branch protection rule without leaving an audit trail

Tick: **"Restrict who can push to matching branches"** — leave
the allowlist empty (means: nobody pushes directly to main;
PRs only).

### 6. Save

Click **"Create"** (or **"Save changes"**).

---

## Verification (5 minutes)

After the rule is live, verify with a deliberate red-CI test:

1. Create a throwaway branch:
   ```bash
   git checkout -b test/branch-protection-verify
   echo "syntax error to break CI" >> brutex/strategies/__init__.py
   git add -A && git commit -m "test: deliberately break CI to verify branch protection"
   git push -u origin test/branch-protection-verify
   ```

2. Open a PR via GitHub UI. Wait for CI to fail (~5 min).

3. Try to merge via the GitHub UI — **the merge button should be
   GRAYED OUT** with message "Required statuses must pass before
   merging".

4. Try via Claude / `mcp__github__merge_pull_request` — should
   return error `405 Method Not Allowed` or similar refusal.

5. Verify the override IS NOT possible without disabling the rule:
   - Even as admin, the "Merge anyway" button should NOT exist
     (because you ticked "Do not allow bypassing").

6. Clean up:
   ```bash
   git checkout main
   git branch -D test/branch-protection-verify
   gh pr close <pr-number> --delete-branch  # or via UI
   ```

If any step fails, re-check your settings against this runbook.

---

## What this gives you (Z+ DETECT → PREVENT → AUDIT)

| Layer | Mechanism | After this setup |
|---|---|---|
| **L1 DETECT** | CI runs on every push | ✅ unchanged (was already working) |
| **L2 VERIFY** | Red-team harness in `tests/redteam/` | ✅ unchanged |
| **L3 RECONCILE** | Daily/weekly cron CI re-runs | ✅ unchanged |
| **L4 PREVENT — client** | `scripts/ci/pre-merge-check.sh` | ✅ already in place |
| **L4 PREVENT — server** | **GitHub Branch Protection (this runbook)** | ✅ **NEW after setup** |
| **L5 AUDIT** | Override log + GitHub audit log | ✅ unchanged + new GitHub-side audit entries on rule changes |
| **L6 RECOVER** | Revert PR pattern (PR #26 precedent) | ✅ unchanged |
| **L7 COOLDOWN** | "Cannot bypass" rule = no rate-unlimited override | ✅ enforced server-side |

**Without this runbook applied:** Claude has DISCIPLINE (call
`pre-merge-check.sh` first). Discipline failed once (PR #25).

**With this runbook applied:** Claude is mechanically prevented from
merging red PRs even if discipline lapses. The GitHub API itself
refuses the request.

---

## What to do if you genuinely need an override

The override path is INTENTIONALLY painful (Z+ COOLDOWN):

1. Go to https://github.com/SJParthi/bruteX/settings/branches
2. Edit the `main` rule
3. Temporarily un-tick **"Do not allow bypassing the above settings"**
4. Click **"Save changes"** — leaves an entry in the GitHub audit log
5. Perform the merge
6. **Immediately re-enable** the bypass-disable checkbox + Save
7. Document the override in `docs/MIGRATION.md` with a DEC-ID

If you're doing this more than once a month, the gates need
rethinking — talk to Claude about which gate is wrong, not how to
bypass it.

---

## Maintenance

- **When a new CI job lands:** add it to the required-status-checks list
  in §3 above. The aggregate `CI OK` gate auto-includes it via
  `needs:`, but explicit listing is belt+suspenders.
- **When a CI job is renamed:** update the required-check name (GitHub
  matches by name — a rename will break enforcement until updated).
- **When a CI job is permanently removed:** remove it from the required
  list (or the rule will block ALL merges waiting for a check that
  never runs).

---

## See also

- `scripts/ci/pre-merge-check.sh` — the client-side discipline layer
- `docs/MIGRATION.md` entry #142 (DEC-P0-004) — the violation that
  prompted this Z+ hardening
- `CLAUDE.md` §2 rule 12 — 100% standards decree
- `docs/zplus/ZPLUS_PR_DISCIPLINE.md` — Z+ defensive layer + 6 marker
  phrases enforced by `scripts/ci/zplus-pr-guard.sh`
