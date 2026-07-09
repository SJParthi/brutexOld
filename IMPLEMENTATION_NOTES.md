# IMPLEMENTATION NOTES — claude/data-edge-guard-fixes

> Working notes for the DEC-BADTICK-HEAD-SEED-001 +
> DEC-MUHURAT-WEEKLY-ANCHOR-ABSTAIN-001 fix branch (MIGRATION.md #1625/#1626).
> Rounds 1-2 notes lived out-of-tree in the earlier session worktrees (the
> round-2 gate outputs the round-3 reviewer quoted — e.g. "mypy --strict
> brutex -> Success: no issues found in 403 source files" — were recorded
> there); this file is committed from round 3 onward so later rounds keep a
> durable in-branch record.

## Standing rules (repo — binding)

- Touched modules at 100% line + 100% branch (`bad_tick.py`, `replay.py` if
  touched, `stage1a.py`).
- Regression tests that FAIL with the fix reverted (the fix-reversion ci-ok
  gate checks this for `fix:`-titled PRs).
- `ruff check` + `ruff format --check` + `mypy --strict` + pyright clean.
- structlog only, no `print`; no bare `except`.
- Conventional-commit style with NO `+` or other illegal chars in the scope
  (the #1580 lesson).
- DEC ids: DEC-BADTICK-HEAD-SEED-001 + DEC-MUHURAT-WEEKLY-ANCHOR-ABSTAIN-001;
  ONE MIGRATION.md ledger entry covering both guards (#1625, with the #1626
  validator stub header for the second id).

## Round 3 fixes (2026-07-09)

Three confirmed findings (each survived 3 refuters), all resolved:

### Finding 1 (MINOR) — escape-band position-3 head spike seated the baseline

- **Defect:** `_resolve_head` judged all 3 head bars against the head MEDIAN
  but seeded the post-head baseline from the LAST kept head bar. A
  threshold-adjacent spike inside the disclosed escape band
  `[4·min(other head closes), 4·median)` at head POSITION 3 was therefore
  kept AND became the baseline; over a flat/declining tail every healthy
  successor with close `<= spike/4` cascade-dropped (a skipped bar never
  advances the baseline). Reviewer probe:
  `[100.0, 101.0, 400.5] + [100.0]*20` → kept only rows 0-2, dropped all 20
  healthy successors, fired skip + majority-dropped WARNs — falsifying the
  "is KEPT, silently" and "re-armed at a 2-corrupt-head-bars threshold"
  claims (ONE bar sufficed).
- **Fix (the reviewer's first recommended remedy, in the branch's idiom):**
  `_resolve_head` now sets `baseline = median` after the vote — the post-head
  baseline SEEDS from the evidence-confirmed center, never blindly the LAST
  kept head bar. The escaped spike is still kept (the disclosed residual:
  its ~4x close reaches the day's OHLC via Stage-1A absorb) but can never
  seat the baseline; the probe now keeps all 23 rows with zero skips and
  zero WARNs. The 2-corrupt-head-bars collapse threshold is thereby made
  EXACT (a corrupt MEDIAN is the only remaining head-seeded collapse).
- **Sanctioned micro-change:** the FIRST post-head bar's reference (incl.
  the skip WARN's `first_ref_close`) is now the head median, not the last
  kept head close — visible only inside the ~1%-wide head dispersion band.
  Two existing tests asserting `first_ref_close == last-kept` were updated
  to the median (27942→27941, 102→101).
- **New pins (both FAIL with the seed reverted — fix-reversion gate teeth,
  verified by a live revert probe: 4 failed / 51 passed reverted, 55 passed
  fixed):**
  - `test_head_escape_band_position_three_spike_cannot_seat_baseline`
    (the reviewer's exact probe + its noisy-tail control);
  - `test_post_head_baseline_seeds_from_median_not_last_kept_head_bar`
    (a 405 close between 4·median = 404 and 4·last-kept = 408 — dropped
    ONLY under the median seed; kills a mutant restoring the last-kept
    baseline).
- **Claim sites corrected (all three):** `bad_tick.py` module docstring
  (the "is KEPT, silently" clause + the corrupt-majority "re-armed"
  paragraph + the post-head-judging bullet), MIGRATION.md #1625 (in-place
  ROUND-3 FIX clause inside the escape-band sentence + the round-3 fold),
  and BOTH CLAUDE.md #1625 chain copies (top `_Last updated` + §1 Active
  branch).

### Finding 2 (MINOR) — third CLAUDE.md #1616 site left uncorrected

- The top `_Last updated` Earlier-chain compressed #1616 blurb still read
  "first-bar-of-session exempt, one `feed.bad_tick_skipped` WARN per
  session" with no strikethrough/[CORRECTED] marker (the §1 chain copy of
  the same blurb WAS corrected at round 2 — inconsistent 2-of-3 treatment).
- Fixed: the phrase now carries `~~first-bar-of-session exempt~~` +
  a [CORRECTED 2026-07-08, DEC-BADTICK-HEAD-SEED-001 #1625] note pointing
  at the §1 chain copy's full correction.

### Finding 3 (NIT) — #1625 GATES block authoring-time counts re-stamped

- goldens "ALL golden suites pass (368)" → 383, re-measured at round 3 on
  the post-f028fe65-merge tree (`pytest tests/golden -q` → `383 passed`;
  `tests/golden/fixtures/` byte-untouched vs origin/main — stale TALLY
  only, the claim held).
- "`mypy --strict` clean (400 files)" → 403 files, re-measured
  (`Success: no issues found in 403 source files`).
- Both re-stamps carry the strikethrough + "authoring time" hedge the
  pyright figure already had; recorded in the round-3 fold as the third
  recurrence of the stale-tally class ("(9 tests)" round 1, "(69/20)"
  round 2).

### Round-3 gate outputs (measured 2026-07-09, this worktree, venv 3.12)

- `pytest tests/unit/test_data_bad_tick.py tests/integration/test_feed_replay_bad_tick_screen.py -q`
  → 64 passed (55 unit + 9 integration).
- Coverage (branch): `brutex/data/bad_tick.py` 100.00% (77/22 — the
  `baseline = median` seed adds 1 statement over round 2's 76/22);
  `brutex/feed/replay.py` 100.00% (95/28, untouched this round);
  `brutex/engine/stage1a.py` untouched this round (round-2 tally 212/40
  stands).
- Fix-reversion teeth: with `baseline = median` reverted, 4 tests fail
  (the 2 new pins + the 2 updated `first_ref_close` asserts); restored,
  55/55 unit tests pass.
- `pytest tests/golden -q` → 383 passed; `git diff origin/main...HEAD --
  tests/golden/ tests/property/` empty.
- `ruff check brutex tests` clean; `ruff format --check` clean (all files).
- `mypy --strict brutex` → Success, 403 files (fresh, non-incremental run;
  an earlier stale-cache run in this fresh worktree venv false-flagged
  cma/starlette import-not-found — environment, not code).
- pyright: 0 errors on `brutex/data/bad_tick.py` +
  `tests/unit/test_data_bad_tick.py` (the only code files touched this
  round; the repo baseline of 40 pre-existing errors is unaffected).
- `validate_ledger_numbering.py` + `validate_dec_ids.py` + citation
  scanner: run post-edit (see commit).
