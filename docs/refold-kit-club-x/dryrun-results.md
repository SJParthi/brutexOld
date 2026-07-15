# Dry-run results — club X refold rehearsal (2026-07-15, isolated worktree, nothing pushed)

## Setup / refs

| ref | value |
|---|---|
| origin/main tip | `d6ac9f85` — fix(dashboard): honest last-pull outcome … (#1457) |
| origin/integration/club-p-pull tip | `2bd0f89d` (== PR #1547 head; PR base = main d6ac9f85; **main IS ancestor of club-p**, so the #1547 squash's tree == club-p's tree) |
| origin/integration/club-x-dhan tip | `b6341fc` (**main IS ancestor**) — `test(club-x): two cross-member weave reconciles` |
| simulated post-P main | temp branch `tmp-postp-main` = `git merge --squash origin/integration/club-p-pull` onto origin/main, committed as `b1e4ff2` with the exact expected squash subject `feat(pull): club P — pull/ingest train #1369 #1456 #1442 #1444 #1472 #1420 #1471 (#1547)`; verified **tree byte-identical to club-p's tree** (empty `git diff`) |
| PR #1547 title (live GitHub API) | `feat(pull): club P — pull/ingest train #1369 #1456 #1442 #1444 #1472 #1420 #1471` (state: open/draft at probe time) |
| dry-run merge commit | `0bd2c52`, parents `b6341fc b1e4ff2` (2-parent shape verified); 141 files, +23,635/−668 vs club-x parent |

## #1800 / DEC-DHAN-GUARD-RETIRE-002 status

**NOT landed** on `origin/integration/club-x-dhan` at fetch time (entries on the
branch: #1795 FEED-INTERFACE · #1796 FEED-LAKE-ROOT · #1797 DHAN-SPOT-FEED ·
#1798 ROLLING-FLOOR · #1799 ROLLING-OPTIONS). Mid-task coordinator ruling:
it will NOT land before the refold (permission-blocked; deferred to a
post-merge follow-up, non-CI-critical) → the renumber target became
NEXT-FREE-AT-EXECUTION (detected, not hardcoded #1801). Detected in this dry
run: **#1800** (max header #1799 + 1). #1802–#1804 are being claimed by the
parallel branch `claude/dhan-options-native-floor` (logged per ruling; not
collided with here).

## Merge — conflict set observed

`git checkout -B refold-dryrun origin/integration/club-x-dhan` +
`git merge --no-commit tmp-postp-main`:

- Conflicts: **CLAUDE.md only** (exactly 1 hunk, markers at line 8398 — the §6
  "Adapter effective cap" row), matching the prior probe.
- Auto-merged cleanly: docs/MIGRATION.md, brutex/cli.py, dashboard/app.py,
  dashboard/pull_jobs.py, templates/universe.html, 2 test files, + the rest.
- Merged MIGRATION.md double-claims #1795 as briefed:
  `1795. **DEC-AUDIT-LAKE-REPLAY-ORACLE-001` (club P, line 6969) and
  `1795. **DEC-FEED-INTERFACE-001` (Dhan, line 7163);
  `validate_ledger_numbering.py` pre-fix → **exit 1** naming exactly that pair.

## CLAUDE.md hunk resolution (applied + verified)

Resolved = post-P side + Dhan's "(injected at the CLI live build site)" +
**cli.py cite repointed** (see SURPRISE #1). Diff vs both parents:

```
vs club-x parent (citations column only):
-  …`dashboard/pull_controller.py:984`; `groww/constants.py:177` (injected at the CLI live build site) + this file §2 rule 6 |
+  …`dashboard/pull_controller.py:1026-1027`; `cli.py:5484-5485` (injected at the CLI live build site) + this file §2 rule 6 |

vs post-P parent (citations column only):
-  …`dashboard/pull_controller.py:1026-1027`; `cli.py:5271-5272` + this file §2 rule 6 |
+  …`dashboard/pull_controller.py:1026-1027`; `cli.py:5484-5485` (injected at the CLI live build site) + this file §2 rule 6 |
```

Full resolved row: `_resolved_row.txt` (637 chars + newline).

## SURPRISES

1. **Post-P's `cli.py:5271-5272` citation is WRONG in the merged tree** —
   club X's Dhan CLI code above that point shifts the CLI live build site to
   `cli.py:5484-5485` (`ceiling_rps=`/`ceiling_per_min=` injection, verified
   by inspection). The citation-accuracy scanner FAILED on the first-pass
   resolution ("no anchor within ±5 of :5271-5272 … found … ceiling_rps at
   :5484; ceiling_per_min at :5485") and passes after the repoint. The
   repoint is folded into `claude-hunk-resolution.md` as resolution-law
   part 3. `dashboard/pull_controller.py:1026-1027` needed NO repoint
   (scanner-verified accurate in the merged tree).
2. **#1800 not landed + coordinator ruling** flipped the renumber target from
   the briefed #1801 to detected-next-free (**#1800** here); `renumber.sh`
   detects max+1 at execution and sweeps every citation with the same number.
3. The briefed "grep -c '^1801.' == 1" check is superseded by the detected
   number: dry-run pins are `grep -c '^1795\. '` == 1 (club P's only, verified
   `DEC-AUDIT-LAKE-REPLAY-ORACLE-001`) and
   `grep -cE '^1800\. \*\*DEC-FEED-INTERFACE-001'` == 1. Both green.
4. No other conflicts, no unexpected citation sites. The `merge --squash`
   composition confirmed post-P main's tree is exactly club-p's tree (main
   hasn't moved), so the rehearsal is byte-faithful unless main moves again.

## Renumber manifest (renumber.sh, detected target #1800) — 19 sites in 14 files

docs/MIGRATION.md (4):
1. header `1795. **DEC-FEED-INTERFACE-001` → `1800. **DEC-FEED-INTERFACE-001` (now line 7163)
2. in-entry chain note extended: `…DEC-LEDGER-RENUMBER-001): #1792 → #1795 → #1800 (the #1795 → #1800 hop at the 2026-07-15 post-club-P refold — club P / PR #1547 took #1795 for DEC-AUDIT-LAKE-REPLAY-ORACLE-001) — main's 2026-07-14/15 wave…`
3. #1797 umbrella cite: `` `DEC-FEED-INTERFACE-001` #1795 `` → `#1800` (line 7161)
4. Builds-on cite: `ADR-0021 (#1795/#1796)` → `ADR-0021 (#1800/#1796)`

CLAUDE.md (4 sites via 2 replacements):
5. header blurb line 25: `(DEC-FEED-INTERFACE-001 #1795 +` → `#1800`
6. header sentence (lines 28–31) rewritten: "…renumbered to the free consecutive #1795–#1799 block, then DEC-FEED-INTERFACE-001 moved #1795 → #1800 at the 2026-07-15 post-club-P refold (club P / PR #1547 took #1795 for DEC-AUDIT-LAKE-REPLAY-ORACLE-001; DEC-IDs are the stable handles per DEC-LEDGER-RENUMBER-001)."
7. §1 Active-branch blurb (line 2780 post-merge): `DEC-FEED-INTERFACE-001 #1795` → `#1800`
8. §3 blurb (line 7582 post-merge): `DEC-FEED-INTERFACE-001 #1795` → `#1800`

.github/workflows/ci.yml (4): the per-module coverage-gate comments
`# DEC-FEED-INTERFACE-001 (MIGRATION.md #1795):` → `#1800` at (post-merge)
lines 576 / 583 / 590 / 597 (feed_audit · feeds.binding · feeds.registry ·
feeds.groww_binding gates).

brutex docstrings (5): `DEC-FEED-INTERFACE-001 (MIGRATION.md #1795)` → `#1800`
in brutex/data/feed_audit.py:3 · brutex/feeds/__init__.py:3 ·
brutex/feeds/binding.py:3 · brutex/feeds/groww_binding.py:3 ·
brutex/feeds/registry.py:3.

docs/adr/0021-historical-feed-and-feed-lake-root.md (1): Decision-IDs line 4
`` `DEC-FEED-INTERFACE-001` (MIGRATION.md entry #1795) `` → `entry #1800`.

tests (5): tests/integration/test_feed_lake_byte_parity.py:3
`DEC-FEED-INTERFACE-001 (#1795)` → `(#1800)`; docstring line 1 of
tests/unit/test_data_feed_audit.py · test_feeds_binding.py ·
test_feeds_groww_binding.py · test_feeds_registry.py
`DEC-FEED-INTERFACE-001, #1795` → `, #1800`.

**Deliberately untouched** (classified, verified): club P's
`1795. **DEC-AUDIT-LAKE-REPLAY-ORACLE-001` header + its `#1723 → #1795` chain
note; the 5 dated push-time-weave notes "…next-free consecutive #1795–#1799…"
inside MIGRATION.md entries (historical record of the 2026-07-15 weave event);
DEC-DHAN-ROLLING-FLOOR-001's `#1795 → #1798` chain history (its OWN old
number); coincidental numeric matches — docs/dashboard/mockups/v28.html +
v29.html (`17956` cost cell), docs/research/ARCHITECTURE_TRILLION_SCALE_EVAL.md
(`sweep_run.py:1795-1817` source-line cite), docs/security/
sbom-rust-fno-math.cdx.json + rust/fno-math/Cargo.lock(+fuzz) (checksums),
rust/trading-core/tests/fixtures/stage1a_batch_golden.json +
tests/golden/fixtures/twin_state_golden_vectors.json (frozen goldens — never
edited).

Post-sweep census: ZERO remaining `DEC-FEED-INTERFACE-001`-adjacent `#1795`
anywhere in the tree (`_census.txt`).

## Validator / gate results (all on the fully-applied merged tree)

| gate | invocation | result |
|---|---|---|
| ledger numbering (pre-fix control) | `python3 scripts/ci/validate_ledger_numbering.py` | **exit 1** — "entry number 1795 is claimed 2x: DEC-AUDIT-LAKE-REPLAY-ORACLE-001 (6969); DEC-FEED-INTERFACE-001 (7163)" |
| ledger numbering (post-renumber) | same (ci.yml:3713) | **exit 0** — "ledger numbering OK: 1368 entry headers, every (number,suffix) pair unique" |
| citation accuracy (first pass) | `python3 scripts/ci/validate_citation_accuracy.py` (ci.yml:4530) | **exit 1** — the cli.py:5271-5272 surprise (see above) |
| citation accuracy (after repoint) | same | **exit 0** — "✅ every extracted citation is accurate" (123 citations, 409 ledger refs) |
| renumber.sh idempotency re-run | `bash renumber.sh` | **exit 0 no-op** — "ALREADY APPLIED: … header sits at #1800" |
| header grep pins | `grep -c '^1795\. '` / `'^1800\. '` docs/MIGRATION.md | 1 and 1 (`1795.` = DEC-AUDIT-LAKE-REPLAY-ORACLE-001; `1800.` = DEC-FEED-INTERFACE-001) |
| import smoke | `python3 -c "import brutex"` | OK |
| ruff | `ruff check` + `ruff format --check` on all touched .py | clean ("All checks passed!" / "6 files already formatted") |
| validator test suites | `.venv/bin/python -m pytest tests/unit/test_validate_ledger_numbering.py tests/unit/test_ci_citation_accuracy.py` | **43 passed** (2.77s) |
| touched feeds suites | pytest test_feeds_{binding,registry,groww_binding}.py + test_data_feed_audit.py | **48 passed** (1.60s) |
| merge commit shape | `git log -1 --format='%p'` | 2 parents: `b6341fc b1e4ff2` |

## Cleanup

Temp branches `tmp-postp-main` + `refold-dryrun` and the dry-run commits
deleted from the worktree after recording these results; artifacts retained in
this directory. Nothing pushed.
