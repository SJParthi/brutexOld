# Refold runbook — `integration/club-x-dhan` onto post-club-P main

**Goal:** build + push the real 2-parent merge commit in <10 minutes once
PR #1547 (club P, head `integration/club-p-pull` @ 2bd0f89d) is squash-merged
to main. Full end-to-end dry run executed 2026-07-15 in an isolated worktree —
see `dryrun-results.md` beside this file.

**Artifacts used by this runbook (same directory):**
- `claude-hunk-resolution.md` (+ raw row in `_resolved_row.txt`) — the ONE
  CLAUDE.md conflict.
- `renumber.sh` — the MIGRATION.md #1795 double-claim fix + citation sweep
  (target number DETECTED at execution = max entry + 1; dry run detected
  **#1800**).

**Coordinator rulings baked in (2026-07-15):**
- DEC-DHAN-GUARD-RETIRE-002 (#1800) will NOT land on the club branch before
  the refold (permission-blocked; deferred to a post-merge follow-up,
  verified non-CI-critical). Do NOT include any guard-retire entry in the
  refold commit.
- The renumber target is NEXT-FREE-AT-EXECUTION, never hardcoded. As of the
  dry run that is #1800. Entries **#1802–#1804 are being claimed by the
  parallel follow-up branch `claude/dhan-options-native-floor`** — if the
  detected next-free lands ON one of those it is FINE (that PR re-verifies
  at its own push time), but LOG what you detected in your report.

---

## 0. Preconditions — verify main's tip contains the #1547 squash

```bash
git fetch origin main integration/club-x-dhan
git log origin/main --oneline -3
```

GATE: the tip subject must be the PR #1547 squash. PR title (recorded live
from the GitHub API 2026-07-15):

```
feat(pull): club P — pull/ingest train #1369 #1456 #1442 #1444 #1472 #1420 #1471
```

GitHub's default squash subject appends the PR number, so expect:

```
feat(pull): club P — pull/ingest train #1369 #1456 #1442 #1444 #1472 #1420 #1471 (#1547)
```

Belt (content-level, survives a reworded subject):

```bash
git show origin/main:docs/MIGRATION.md | grep -c '^1795\. \*\*DEC-AUDIT-LAKE-REPLAY-ORACLE-001'
# must print 1 — club P's #1795 entry is on main
```

Also record the club-x tip:

```bash
git log origin/integration/club-x-dhan --oneline -2
# dry run was derived at tip b6341fc. A different tip is OK (renumber.sh
# fails loudly on anchor drift and auto-detects the target number), but
# NOTE IT in your report. Per the coordinator ruling, NO guard-retire/#1800
# commit is expected to have landed.
```

STOP-CONDITION: if main's tip is NOT the #1547 squash (or the belt grep
prints 0), stop and report — do not merge against a different main.

## 1. Build the merge

```bash
git checkout -B refold origin/integration/club-x-dhan
git merge --no-commit origin/main
git diff --name-only --diff-filter=U
```

GATE: the conflict list must be EXACTLY `CLAUDE.md` (one hunk, the §6
"Adapter effective cap" row — dry-run-verified against main d6ac9f85 +
the #1547 squash).

FALLBACK (main moved past P): if ANY other file conflicts, or CLAUDE.md has
more than one `<<<<<<<` marker —

```bash
git merge --abort
```

— STOP and report the conflict list. Do NOT improvise resolutions.

## 2. Resolve the CLAUDE.md hunk

Run the mechanical snippet from `claude-hunk-resolution.md` §"Mechanical
application" (it takes the post-P side and inserts Dhan's "(injected at the
CLI live build site)" wording + repoints the cli.py cite to the merged tree's
`cli.py:5484-5485`). It ends with `git add CLAUDE.md`. If its assertions
fail: `git merge --abort`, stop, report.

## 3. Apply the renumber + citation sweep

```bash
bash /tmp/claude-0/-home-user-bruteX/12ba35bd-e8ab-5d13-9bfe-c7e884403a56/scratchpad/refold/renumber.sh
```

- It prints `DETECTED next-free ledger number: #NNNN` — expected **#1800**;
  RECORD the printed number (call it `#N` below). If it detects #1802–#1804,
  that is the parallel `claude/dhan-options-native-floor` claim being
  overtaken — fine, but log it.
- It renames `1795. **DEC-FEED-INTERFACE-001` → `N. **DEC-FEED-INTERFACE-001`,
  extends the entry's in-entry renumber chain, and sweeps all 18 cross-citation
  sites (manifest printed; full site list in `dryrun-results.md`). Club P's
  #1795 (DEC-AUDIT-LAKE-REPLAY-ORACLE-001) is never touched.
- ANCHOR MISMATCH → it exits 2 with NOTHING written: `git merge --abort`,
  stop, report.

```bash
git add -u
git status --short | grep -E '^(U|AA|DD)' && { echo UNRESOLVED; exit 1; } || true
```

## 4. Validate (exact CI invocations)

```bash
# ci.yml:3713
python3 scripts/ci/validate_ledger_numbering.py
# → exit 0, "ledger numbering OK: … every (number,suffix) pair unique"

# ci.yml:4530 (citation-accuracy-scan job)
python3 scripts/ci/validate_citation_accuracy.py
# → exit 0, "✅ every extracted citation is accurate"
# If it flags CLAUDE.md's "Adapter effective cap" cli.py cite: the merged
# tree drifted — repoint to the 'found at :NNN' lines it prints, re-run.

test "$(grep -c '^1795\. ' docs/MIGRATION.md)" = 1       # club P's only
grep -E '^1795\. \*\*DEC-AUDIT-LAKE-REPLAY-ORACLE-001' docs/MIGRATION.md >/dev/null
test "$(grep -cE "^1800\. \*\*DEC-FEED-INTERFACE-001" docs/MIGRATION.md)" = 1
# ^ substitute 1800 with the number renumber.sh printed, if different

python3 -c "import brutex; print('import OK')"
# optional fast belts (dry-run: 43 + 48 passed):
#   .venv/bin/python -m pytest -q tests/unit/test_validate_ledger_numbering.py \
#       tests/unit/test_ci_citation_accuracy.py
#   .venv/bin/python -m pytest -q tests/unit/test_feeds_binding.py \
#       tests/unit/test_feeds_registry.py tests/unit/test_feeds_groww_binding.py \
#       tests/unit/test_data_feed_audit.py
```

Any red → `git merge --abort` (or `git reset --merge` if already committed
nothing), stop, report.

## 5. Commit (2-parent merge commit)

```bash
git commit -m "merge(main): refold club X onto post-club-P main — CLAUDE.md citation-row weave + DEC-FEED-INTERFACE-001 #1795→#1800 renumber (DEC-LEDGER-RENUMBER-001)

Post-#1547 refold: post-P citation row kept (+ Dhan's CLI-live-build-site
wording, cli.py cite repointed to the merged tree), MIGRATION.md #1795
double-claim resolved to the detected next-free number; validators green
(ledger numbering + citation accuracy). #1800 guard-retire entry deliberately
NOT included (deferred post-merge per coordinator ruling 2026-07-15).

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01TwTbSZy5hn3zG9AT4AbhGK"
```

(Substitute `#1800` in the subject with the detected number if different.)

Verify shape:

```bash
git log -1 --format='%p'   # must print TWO parents:
#   <club-x tip (b6341fc unless it moved)> <origin/main tip (the #1547 squash)>
git diff --stat origin/main HEAD | tail -1   # sanity: the Dhan pack lands
```

## 6. Push (no force)

```bash
git push origin HEAD:integration/club-x-dhan
```

If rejected non-fast-forward (the branch moved during the refold): fetch and
RESTART from step 0 against the new tip — never `--force`.

## 7. Report

Record: main tip SHA + subject, club-x tip SHA, the detected renumber target,
validator outputs, the merge commit SHA + its two parents, and any deviation
from this runbook.

---

## FINAL CORRECTIONS (2026-07-15 — these OVERRIDE the body above where they conflict)

1. **CodeQL patch step DELETED.** The CodeQL batch is resolved UPSTREAM on
   the club head `4d54955b` via `__all__` unexport (verified). Do NOT apply
   any TypeAlias patch — if the body above (or any older kit copy) mentions
   one, skip it entirely.

2. **Renumber TARGET PINNED to `#1800` explicitly.** Verified free on BOTH
   trees. Do NOT use detect-max+1 / "NEXT-FREE-AT-EXECUTION" — the fix
   commits on `claude/dhan-x-fixes` add tail entries **#1865/#1866**, which
   would skew any max+1 detection. Invoke `renumber.sh` with the target
   pinned to 1800 (if the script auto-detects, override/verify it prints
   exactly `#1800`; anything else = STOP and report).

3. **Fix-commit apply order.** Cherry-pick / `git am` the two fix commits
   BEFORE building the 2-parent merge with post-P main, in this order:
   1. `9569609` — `fix(dhan): halt the rolling pull on a terminal stop` (F2)
   2. `7dd58ed` — `fix(dhan): key the account flock on the Dhan client id` (F1)
   Then build the merge, and push everything as ONE event.

4. **PR body is FINAL.** Never rewrite the PR body from any snapshot — leave
   it exactly as it stands on the PR.

5. **Validators to run BEFORE the push:**
   ```bash
   python3 scripts/ci/validate_ledger_numbering.py
   python3 scripts/ci/validate_citation_accuracy.py   # the citation-accuracy scanner
   python -c "import brutex"
   test "$(grep -c '^1800\.' docs/MIGRATION.md)" = 1
   test "$(grep -c '^1795\. ' docs/MIGRATION.md)" = 1   # club P's only
   test "$(grep -c '^1865\.' docs/MIGRATION.md)" = 1
   test "$(grep -c '^1866\.' docs/MIGRATION.md)" = 1
   ```
   Any red → stop and report; do not push.
