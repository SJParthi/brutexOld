---
name: ship-pr
description: >-
  Ship the current branch as a fully-gated BruteX PR and drive it to merge. Use after a change is
  implemented + locally green: it runs the parallel hostile-review + workspace-health subagents,
  writes a PR body carrying the mandatory Z+/Definition-of-Ready markers, opens a draft PR, marks
  it ready, and monitors CI to auto-merge — folding review findings in before merge.
---

# Ship a BruteX PR (the repeatable ship→merge loop)

The codified version of the BruteX delivery discipline (CLAUDE.md Rules #9, #12, #13). Follow in
order; do not skip a station.

## 1. Pre-flight (local, CI-exact) — all must be green before opening the PR
```
source .venv/bin/activate
ruff check brutex tests && ruff format --check brutex
pyright <touched modules>
# per-module 100% gate for each touched module, tested by ITS OWN test file alone:
python -m pytest tests/unit/test_<module>.py -q \
  --cov=brutex.<package>.<module> --cov-branch --cov-report=term-missing --cov-fail-under=100
```

## 2. Parallel review (run BOTH subagents at once — one message, two Agent calls)
- `hostile-reviewer` — adversarial defect + coverage-honesty audit of the diff.
- `workspace-health` — independent evidence-based "the whole repo still works" verdict.
Fold every real finding into the branch BEFORE merge (a SHIP verdict with MINORs still gets the
cheap MINORs fixed — that is the BruteX bar).

## 3. Commit + push
Conventional Commit subject; body explains WHAT + WHY. Footer:
```
Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```
`git push -u origin <branch>` (retry with backoff on a network error; never `--force` a shared branch).

## 4. Open the PR with the MANDATORY markers (CI refuses a body missing any)
- **Z+ guard (6):** `Z+ 15-row matrix`, `Z+ 7-row resilience matrix`, `Z+ 7-layer audit`,
  `4-agent hostile review`, `Honest envelope`, `Decision ID`.
- **Definition of Ready (5):** `Scenarios covered`, `Failure modes`, `Real-run proof`,
  `Rollback plan`, `Out of scope`.
- Cite a `DEC-*` id that EXISTS in `docs/MIGRATION.md` (the DEC-ID validator checks it).
- Keep every factual claim cited (the no-hallucination scan checks Groww/market claims).
Create as **draft**, then mark **ready**.

## 5. Drive to merge (monitor, do not churn)
- The repo's **Auto-merge on green CI** workflow merges when all 60+ gates pass; or merge via
  squash once `mergeable_state` is `clean` + the pre-merge gate posts ✅ MERGE-READY.
- A failure on a SUPERSEDED commit, or a GitHub-infra transient (artifact-upload ETIMEDOUT /
  crates.io download EOF at the Rust-wheel setup), is NOT a code failure — re-trigger (empty
  commit) at most a couple of times; if it keeps failing on infra, WAIT for recovery, don't spam.
- Act only on: a genuine code failure on the HEAD commit (fix it) or the merge event.

## 6. Post-merge (Definition of Done)
`git fetch origin main && git checkout -B <branch> origin/main`; re-run the touched module's tests
on the new main to confirm the squash is healthy; then start the next item.
