# IMPLEMENTATION NOTES — claude/data-edge-guard-fixes

> WORKING NOTES — NOT a normative doc. Branch-scoped, per-round process
> record for the DEC-BADTICK-HEAD-SEED-001 +
> DEC-MUHURAT-WEEKLY-ANCHOR-ABSTAIN-001 fix branch (MIGRATION.md
> #1625/#1626). The DURABLE record of every decision + gate claim is
> MIGRATION.md #1625 (Golden Rule #2 — where this file and the ledger ever
> disagree, the ledger wins and this file is the stale copy); tallies here
> are AUTHORING-TIME measurements dated per round, expected to stale on
> main, and this file sits OUTSIDE the citation scanner's normative doc set
> (`validate_citation_accuracy.py` scans CLAUDE.md + `docs/TRACK1_*.md` +
> `docs/STANDARDS_100PCT.md` only) — deliberate: process notes, not claims.
> Rounds 1-2 notes lived out-of-tree in the earlier session worktrees (the
> round-2 gate outputs the round-3 reviewer quoted — e.g. "mypy --strict
> brutex -> Success: no issues found in 403 source files" — were recorded
> there), so the in-repo record is PARTIAL by construction; this file is
> committed from round 3 onward so later rounds keep a durable in-branch
> record. Committed at the repo ROOT by the round-3 commit (793130d1) —
> UNDISCLOSED there (a round-4 finding); RELOCATED to docs/ at round 4 and
> disclosed in the #1625 Files list.

## Standing rules (repo — binding)

The binding rules are CLAUDE.md §2 (Golden Rules) + §4 (Coding Standards) —
they are NOT copied here (§11 COMMON: one source of truth; an earlier copy
of six of them lived in this section and was replaced by this pointer at
round 4). Task-scoped parameters this branch adds on top:

- Touched-module gate scope for THIS branch: `bad_tick.py`, `replay.py` (if
  touched), `stage1a.py` — each 100% line + branch per CLAUDE.md §4.
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

## Round 4 fixes (2026-07-09)

Nine confirmed findings (each survived 3 refuters) — 4 recurrences of the
base-drift class, 3 on this file's round-3 ship, 1 #1616-correction
overclaim, 1 stale date stamp. All resolved; ZERO brutex/ source or
test-file changes this round (docs + merge reconcile only), so the round-3
fix-reversion teeth stand unmodified.

### Findings 1/3/9 (MINOR/NIT/NIT) + 4 (MINOR) — base drift to b73d3b4b

- origin/main advanced past the round-2 merge base (f028fe65 → b73d3b4b,
  DEC-LAKE-STATS-MANIFEST-001 #1614), rewriting CLAUDE.md's top blurb +
  §1/§3 Active-branch regions — a dry-run merge conflicted in CLAUDE.md
  (the round-2 finding-1 class recurring).
- Fixed by merging origin/main (merge commit cabcaef1): the three CLAUDE.md
  conflicts resolved the round-2 way (#1625 blurb stays on top at all three
  sites; #1614's blurb demoted into the Earlier chains — top `Earlier:
  2026-07-07 (DEC-LAKE-STATS-MANIFEST-001 …)`, §1 `Earlier:
  \`claude/dazzling-pasteur-jf8s3w\` (…full blurb…)`, §3 compressed pointer
  entry). MIGRATION.md auto-merged; #1625/#1626 collision-free (main took
  #1614; validator green, 1246 headers). Zero source conflicts — the three
  brutex sources + three test files untouched by b73d3b4b.
- The merged tree adopts main's NEW `test_claude_md_branch_consistency.py`
  header law (`TestHeaderRoundNarrationRetired`): the top `_Last updated`
  blurb now ends with the stale-proof pointer ("The FULL per-round record
  lives ONLY in the §1 Active-branch blurb + MIGRATION.md #1625") and the
  stale round-2 pyright parenthetical is refreshed to the round-4
  measurement in the same edit; the 10-test pin file is green.

### Findings 2/5/7 (MINOR/NIT/MINOR) — this file's root-level round-3 ship

- Round 3 committed this file at the repo ROOT, undisclosed (not in #1625's
  Files list, not in the round-3 commit message, not in any CLAUDE.md
  blurb), with a "Standing rules (repo — binding)" section duplicating
  CLAUDE.md §2/§4 (§11 COMMON violation) and outside every validator's
  scope.
- Fixed: `git mv` → `docs/IMPLEMENTATION_NOTES_DATA_EDGE_GUARD_FIXES.md`;
  header rewritten as an explicit NON-normative ledger-wins working-notes
  disclaimer (incl. the partial-record and unscanned-by-the-citation-
  validator disclosures); the duplicated rules section replaced by a
  pointer to CLAUDE.md §2/§4 + the task-scoped parameters only; DISCLOSED
  in #1625's Files list with the why-committed clause; the round-4 fold
  records the original undisclosed root ship as history.

### Finding 6 (MINOR) — #1616 correction sites overclaimed "caught"

- All three #1616 correction sites (MIGRATION.md #1616 in-place [CORRECTED]
  note, CLAUDE.md §1 chain copy, top compressed blurb) said "a corrupt head
  bar at ANY of positions 1-3 is NOW caught" (unconditional — false for the
  escape-band spike) and enumerated only two "remaining honest limits",
  omitting the escape-band kept-spike + bounded-forward-looking
  truncated-load corners rounds 1-2 disclosed in #1625.
- Fixed at all three sites: "caught" qualified as ≥4x (or ≤1/4x) the HEAD
  MEDIAN; the enumeration completed with both omitted corners (+ the
  round-3 "can no longer seat the baseline" status) and pointers to #1625.

### Finding 8 (NIT) — stale `_Last updated: 2026-07-08` stamp

- Bumped to 2026-07-09 (the file's actual last-edit date; the blurb carries
  round-2/3/4 material authored 2026-07-09).

### Round-4 gate outputs (measured 2026-07-09, this worktree, venv 3.12)

- `pytest tests/unit/test_data_bad_tick.py
  tests/integration/test_feed_replay_bad_tick_screen.py -q` → 64 passed.
- Coverage (branch): `brutex/data/bad_tick.py` 100.00% (77/22) +
  `brutex/feed/replay.py` 100.00% (95/28) over the three replay/bad-tick
  suites (86 tests); `brutex/engine/stage1a.py` 100.00% (212/40) over the
  1,699-test engine-family selection (`-k "stage1a or sweep or stage1b or
  fib_axis"`).
- `pytest tests/golden -q` → 387 passed (post-b73d3b4b tree; 383 was the
  round-3 post-f028fe65 count — tally re-stamped in #1625's GATES);
  `git diff origin/main...HEAD -- tests/golden/ tests/property/` empty.
- `pytest tests/unit/test_claude_md_branch_consistency.py -q` → 10 passed
  (was 1 failed pre-fix on the new main-side header law).
- `ruff check brutex tests` clean; `ruff format --check` clean (1458 files).
- `mypy --strict brutex` → Success, 404 files.
- pyright: `brutex` 0 errors; `brutex tests` 1 error — in
  `tests/unit/test_window_ledger.py`, byte-identical to origin/main on this
  branch (main's own baseline; 0 new).
- `validate_ledger_numbering.py` (1246 headers OK) +
  `validate_citation_accuracy.py` (every citation accurate) green post-edit.

## Additions cycle (2026-07-09) — DEC-BADTICK-HILO-SCREEN-001 + DEC-READ-BOUNDARY-FINITE-001

Merge + two approved additions (the provisional-API §3 items, finalized) + the
fix-forward register completeness items, on top of the round-4 tree.

### Merge (base drift, again)

Main advanced one commit past the round-4 merge base (b73d3b4b → e8c708bd,
DEC-ONECLICK-TOP100-OBJECT-ROOT-001 #1631). Merged with the established
resolution: this branch's blurbs stay newest in CLAUDE.md's three regions,
main's #1631 entry demoted to the next `Earlier:` slot; `docs/MIGRATION.md`
auto-merged (no numbering collision — ours #1625/#1626, main's #1631). The new
ledger entries took the interim **#1632/#1633** (next free above the then-live
main max #1631) — RENUMBERED to **#1655/#1656** at fixer round 1 (below): live
main advanced to #1654 max and PR #1354 took #1632, so the interim numbers
collided on the merged tree.

### Addition B — DEC-BADTICK-HILO-SCREEN-001 (MIGRATION.md #1655; interim #1632)

`BadTickScreen.screen_mask` gains OPTIONAL keyword-only `highs` / `lows`
(each independently optional; omitted = close-only, byte-identical — the
provisional-API §4.1 additive-only contract honored: no existing name/law
changed). Semantics finalized from the provisional §3a:

- high/low judged against the SAME close-derived session reference (head
  median / last-KEPT close) under the SAME `max_ratio` law, boundary bad, via
  the ONE `is_bad_tick` predicate (symmetric — the down-arm of the high check
  is unreachable for an ordered bar with a kept close, but a mis-ordered
  stored bar is corrupt anyway, so one law beats two);
- whole-bar drop; baseline stays close-derived; a skipped bar (any reason)
  never advances it; reason precedence `close` → `high` → `low` PINNED (a
  fat-range both-scaled bar counts once, as `high`);
- WARN: hi/lo skips fold into `skipped`; ADDITIVE `skip_reasons` (per-reason
  count map) + `first_reason` fields; `first_close` keeps its meaning (the
  offending BAR's close, sane for a hi/lo skip);
- 1-bar/2-bar sessions stay kept INCLUDING their highs/lows (no confirmed
  reference — disclosed + test-pinned; the audit-3 "1-bar session kept" limit);
- the head escape band `[4·min-neighbor, 4·median)` applies identically.
- `replay.py` passes the table's highs/lows into the screen.

Audit-3 reproducer regressions (all in `tests/unit/test_data_bad_tick.py` +
`tests/integration/test_feed_replay_bad_tick_screen.py`): HIGH-only ×10 (the
DATA-3b PDH-poison shape) · LOW-only ÷10 · both-scaled fat-range · poison at
position {0,1,last} · head permutations with hi/lo corruption (judged vs the
head close median) · hi/lo skip never advances the baseline · highs-only /
lows-only supply · exact-4× boundary · mixed-reason one-WARN breakdown ·
legit reopen gap must-NOT-trip · omitted-kwargs byte-identity · end-to-end
replay drops with `skip_reasons` on the wire WARN.

### Addition C — DEC-READ-BOUNDARY-FINITE-001 (MIGRATION.md #1656; interim #1633)

NEW `brutex/data/bad_tick.py::non_finite_keep_mask` + NEW event constant
`NON_FINITE_SKIP_EVENT = "feed.non_finite_bar_skipped"` (a DISTINCT event
name — decision documented: a non-finite price is a different, CERTAIN
corruption class with a different remediation story; not additive fields on
the bad-tick WARN). Wired in `arrow_table_to_bars` as the FIRST screen:

- a bar with ANY non-finite OHLC field (inf/-inf/nan) is skipped-and-counted
  BEFORE the positivity raise; one WARN per affected session (fields: source,
  session, skipped, first_field in pinned open→high→low→close order,
  first_value as a repr STRING — inf/nan are not JSON numbers);
- SANCTIONED CHANGE (documented in code + #1656): `-inf` moves from
  raise-class (positivity ValueError, the pre-change whole-table crash) to
  skip-class; the positivity raise stays reserved for FINITE non-positive
  prices, byte-unchanged;
- non-finite rows are FILTERED OUT of the ratio screen's input (NaN compares
  false — fed raw it would blind the median vote / baseline; test-pinned: a
  NaN head row + a later ×10 spike in the same session still catches the
  spike — this exercises the replay filtration/re-expansion branch);
- KILL SWITCH decision: SHARED with the ratio screen
  (`BRUTEX_BAD_TICK_SCREEN`, one `bad_tick_screen_enabled()` gate). Rationale
  recorded: the switch's contract is "OFF = the exact pre-#1616 raw read path
  for forensic replay of raw stored bytes"; a separate finite-only switch
  would mint a mixed mode with no operator story. OFF restores the pre-#1616
  envelope for non-finite values too (`-inf` raises at positivity again —
  pinned; NaN/+inf flow raw, the pre-existing disclosed envelope).
- NaN-compares-false trap cases pinned explicitly: `nan <= 0` is False,
  `is_bad_tick(nan, x)` / `is_bad_tick(x, nan)` are False.

### Fix-forward register items (D1–D5)

1. **Head escape band disclosure** — already landed in rounds 2/3 (module
   docstring names `[max_ratio * min(other head closes), max_ratio * median)`
   + the ledger #1625 carries it); verified present at this HEAD, no change
   needed beyond the #1655 restatement.
2. **Majority-collapse WARN doc precision** — already landed in round 2 (the
   event's doc + `_flush` comment state it cannot fire on the minimal 2-of-3 /
   2v2 shapes and over-fires on a healthy-median short session; strict gate +
   test-pins kept); verified present, unchanged.
3. **First-day-Muhurat WARN** — the round-2 behavior fix stands; the
   additions cycle WIDENS its pin: `test_first_day_muhurat_stream_emits_the_abstain_warn`
   is now parametrized over ALL SIX real Muhurat dates (2020-2025) as the
   stream START × both weekly anchors (12 cases).
4. **Stale tallies re-stamped at THIS head** (re-measured, never copied) —
   see the gates block below.
5. **Kernel-path Muhurat coverage** (the EXTERNAL_REQUIREMENTS mandatory
   item): NEW `TestKernelPathMuhuratWeeklyAnchor` in
   `tests/unit/test_engine_sweep_kernel.py` EXECUTES `ResidentSweepKernel`
   construction AND `ResidentKernelCache.get` over the real 2024 Muhurat
   boundary for ROLLING_WEEKLY + WEEKLY_STATIC: completes without raising,
   exactly one abstain WARN, `kernel.bar_bits == build_bar_bits(...)`
   bit-identical. Structural note (verified at this HEAD): the kernel path
   reaches anchors through the SAME seam — `sweep_kernel.py`
   (`ResidentSweepKernel.__init__` → `build_bar_bits` →
   `Stage1AMatrixBuilder` → `stage1a._compute_anchors`) — so NO guard mirror
   is needed; the tests prove it by execution, never by inference.

### PR-body obligations carried forward (for the ship step — no PR opened here)

- **Screen-then-hash ordering** (verifier B req 2): the screen runs at
  `feed/replay.py::arrow_table_to_bars` (mask pre-pass + row skip), and the
  run-identity `data_snapshot_hash` (bxsnap2, `engine/stage3_wire.py:171`)
  is computed over the LOADED bar list — `cli.py` `cmd_sweep_all` loads
  `slices[(symbol, interval)] = [event.bar for event in feed]` (ReplayFeed →
  `Catalog.get_bars` → `arrow_table_to_bars`, `brutex/feed/replay.py:246-255`)
  and hashes those slices (`brutex/cli.py:6496`). A kept-set change under the
  screen therefore SELF-RE-KEYS. Now ALSO test-pinned:
  `test_screen_then_hash_kept_set_change_self_rekeys` (same table, screen ON
  vs OFF → different snapshot hash).
- **Golden census** (verifier B req 1a, re-run at this HEAD): `grep -rl` over
  `tests/golden/` for all six Muhurat dates (ISO + compact forms) AND for any
  weekly-anchor token (`rolling_weekly|weekly_static|ROLLING_WEEKLY|WEEKLY_STATIC`)
  → ZERO files. No golden fixture exercises a weekly-anchor Muhurat-day tuple.
- **Vacuous re-key note** (req 1b): a hypothetical operator with persisted
  Muhurat-weekly tuples from a prior non-aborting build would need a re-key —
  vacuous today (the only prior state was a whole-grid abort; zero persisted
  rows; frozen checkpoint), stated explicitly. No indicator_version bump
  needed (pre-fix = abort; completed tuples recompute bit-identically).
- Post-merge notification obligations (verifier B re-pin/re-run, verifier A
  parity-golden PR #1319 extension, product-audit-3 + coordinator ping with
  the final DEC ids DEC-BADTICK-HILO-SCREEN-001 / DEC-READ-BOUNDARY-FINITE-001
  and the PR number) — owed by the orchestrator at PR time.

### Additions-cycle gate outputs (measured 2026-07-09 at this HEAD, venv312)

- `pytest tests/unit/test_data_bad_tick.py
  tests/integration/test_feed_replay_bad_tick_screen.py
  tests/integration/test_feed_replay.py -q` → 125 passed; coverage (branch):
  `brutex/data/bad_tick.py` 100.00% (124/46) + `brutex/feed/replay.py`
  100.00% (106/34).
- `pytest tests/unit -k engine --cov=brutex.engine.stage1a` → 2621 passed;
  `brutex/engine/stage1a.py` 100.00% (212 stmts, 40 branches — table total
  442/120 incl. bad_tick + replay, 0 missing).
- `pytest tests/unit/test_engine_stage1a.py -q` → 64 passed (incl. the 12
  first-day-Muhurat cases); `tests/unit/test_engine_sweep_kernel.py` → 27
  passed (incl. the 3 kernel-path Muhurat tests).
- `pytest tests/property/test_lookahead_properties.py
  tests/property/test_sweep_suffix_independence.py
  tests/property/test_market_structure_lookahead.py -q` → 18 passed
  UNMODIFIED.
- `pytest tests/golden -q` → 388 passed (387 was the round-4 count — the +1
  is main's e8c708bd oneclick golden, re-stamped);
  `git diff origin/main HEAD -- tests/golden/fixtures/` EMPTY (the frozen
  400-case fixture byte-untouched).
- `pytest tests/unit/test_claude_md_branch_consistency.py -q` → 10 passed.
- `ruff check brutex tests` clean; `ruff format --check` → 1441 files
  already formatted.
- `mypy --strict brutex` → Success: no issues found in 404 source files.
- pyright: touched files (`bad_tick.py` + `replay.py` + `stage1a.py`) 0
  errors; `pyright brutex` → 1 error in `brutex/dashboard/trusted_host.py`
  (reportUntypedBaseClass) — the file is byte-identical to origin/main on
  this branch (zero diff) and untouched by every commit here; an
  environment-baseline finding, 0 new from this change.
- `validate_ledger_numbering.py` → 1249 headers OK;
  `validate_citation_accuracy.py` → every citation accurate.

## Fixer round 1 (2026-07-09) — additions-cycle review findings

Ten confirmed findings from the additions-cycle review (1 BLOCKER + 3 MAJOR +
5 MINOR + 1 NIT, several duplicated across lenses), all resolved on top of a
fresh origin/main merge (95955a71 → 0729fee1 range; merge commit resolves the
CLAUDE.md three-region conflict the established way — this branch's blurbs
newest, main's #1594/#1650 demoted into the Earlier chains; MIGRATION.md
auto-merged).

### BLOCKER/MAJOR — ledger #1632/#1633 collided with live main

- Live main advanced to #1654 max and PR #1354 took #1632
  (DEC-DASHBOARD-VISUAL-OVERHAUL-001); `validate_ledger_numbering.py` exited 1
  on the merged tree ("entry number 1632 is claimed 2x") — a guaranteed CI
  red at the required gate.
- Fixed by the P-1 rule + the #1546–#1550/#1580 renumber precedent:
  DEC-BADTICK-HILO-SCREEN-001 #1632 → **#1655**, DEC-READ-BOUNDARY-FINITE-001
  #1633 → **#1656** (= live-main max #1654 + 1/+2; the validator allows
  gaps), with in-entry renumber notes, the #1656 "Builds on"/next-free
  cross-refs repointed, and every cross-reference swept: CLAUDE.md top blurb
  + §1 + §3 (with renumber parentheticals), this file's five refs. No
  brutex/ source or test file referenced the numbers (grep-verified — DEC
  ids only). Validator green post-edit on the merged tree (1262 headers).

### MAJOR — hi/lo arm's mis-ordered raise→skip narrowing undisclosed

- A mis-ordered stored bar whose corrupt field is ALSO ratio-out-of-band
  (e.g. a ×10 LOW above its own high) was pre-additions ALWAYS kept by the
  close-only screen and hit the DEC-STORED-DATA-VALIDATION-001 ordering
  ValueError (loud whole-table refusal); the hi/lo arm now skips it silently
  — a real loud→quiet contract change with no SANCTIONED CHANGE record (the
  `-inf` twin in #1656 got one), a stale `replay.py` docstring ("an ordering
  violation raises a LOUD ValueError"), and no test pin.
- Fixed as DISCLOSURE + pins (the behavior stands on the merits — the bar is
  corrupt either way and both outcomes refuse to serve it): a SANCTIONED
  CHANGE record in #1655; the `replay.py::arrow_table_to_bars` docstring
  scoped ("FINITE non-positive price"; the ordering raise's scope with the
  screens ON stated); module-docstring disclosure in `bad_tick.py`; three
  new integration pins — `test_misordered_out_of_band_low_skipped_not_raised`
  + `test_misordered_in_band_bar_still_raises_ordering_error` +
  `test_kill_switch_off_restores_misordered_ordering_raise`.

### MAJOR/MINOR (×4 lenses) — the never-emptied head-vote claim falsified

- The invariant is CLOSE-vote-only: `_judge(median, i)` also tests the
  median-close bar's OWN high/low, so the median bar can drop and a ≤3-bar
  session whose every head bar carries an out-of-band high/low is fully
  EMPTIED (kept=0; reproduced) — production `arrow_table_to_bars` always
  supplies highs/lows. Stale at `_resolve_head`'s docstring, MIGRATION.md
  #1625, and #1655's "carried verbatim" honest-limits framing.
- Fixed: `_resolve_head` docstring scoped to the close vote (+ the
  median-close-still-seeds-the-baseline note); #1625 corrected IN PLACE
  (strikethrough + [CORRECTED 2026-07-09 …#1655] note, the #208 precedent);
  #1655's honest limits gain the emptied-session shape (loud: skip WARN
  `skip_reasons` + majority-dropped WARN kept=0) and drop the "carried
  verbatim" wording; CLAUDE.md top + §1 blurbs updated; three new unit pins
  — `test_hilo_median_close_bar_dropped_for_its_own_high` +
  `test_hilo_all_head_bars_corrupt_highs_empty_the_session` (mask all-False,
  skip_reasons={'high': 3}, majority kept=0) +
  `test_close_only_vote_alone_never_empties_a_session` (the counter-pin
  scoping the corrected claim). (The out-of-tree scratchpad notes' permutation
  table carried the same stale row — superseded by this in-repo record.)

### MINOR — kill-switch-OFF envelope claim imprecise ("NaN/+inf flow raw")

- Measured per field on BOTH trees (branch OFF ≡ main, case-for-case): NaN
  in ANY OHLC field raises an UNWRAPPED `decimal.InvalidOperation` from
  CandleBar validation; `+inf` in open/low/close raises the wrapped
  corrupt-bar ValueError; ONLY `+inf`-in-HIGH flows raw; `-inf` raises at
  positivity. The old summary contradicted #1656's own GAP passage.
- Fixed (docs only, behavior unchanged): the `bad_tick.py` non-finite
  kill-switch paragraph + the #1656 kill-switch passage + the CLAUDE.md
  top/§1 blurbs now carry the per-field truth.

### MINOR — verifier-B PR-body materials not in the ledger

- The golden census (1a), the vacuous re-key note (1b), and the
  screen-then-hash ordering citation (2) lived only in this file; the
  requirement wants them in the PR body / ledger entry.
- Fixed: all three folded into #1655 as a "PR-BODY MATERIALS" passage (the
  renumber forced a ledger edit anyway), so the ship step can source ONE
  durable record. This file's copies stand as the working duplicates.

### NIT — unscoped "P-06 + suffix-independence suites pass UNMODIFIED"

- The additions-cycle CLAUDE.md blurbs + commit message re-minted the claim
  without #1625's scoping clause. Fixed: the scoping ("they do not route
  through `arrow_table_to_bars`, so they do not certify the new screens")
  appended at the CLAUDE.md top + §1 blurbs and in #1655's gates sentence.

### Incidental (environment-verified while gating)

- The additions-cycle `test_screen_then_hash_kept_set_change_self_rekeys`
  pin carried a pyright red in THIS environment (pyright 1.1.408:
  `list[object]` invariance on the local `_hash`) — annotation fixed to
  `list[CandleBar]`, the now-unneeded `# type: ignore[dict-item]` dropped;
  0 errors post-fix, test green.

### Fixer round-1 gate outputs (measured 2026-07-09, this worktree, venv312)

- `pytest tests/unit/test_data_bad_tick.py
  tests/integration/test_feed_replay_bad_tick_screen.py
  tests/integration/test_feed_replay.py -q` → 131 passed (89 + 20 + 22);
  coverage (branch): `brutex/data/bad_tick.py` 100.00% (124/46) +
  `brutex/feed/replay.py` 100.00% (106/34).
- `pytest tests/property/test_lookahead_properties.py
  tests/property/test_sweep_suffix_independence.py
  tests/property/test_market_structure_lookahead.py -q` → 18 passed
  UNMODIFIED (scoped: they do not certify the screens).
- `pytest tests/golden -q` → 388 passed;
  `git diff origin/main HEAD -- tests/golden/fixtures/` EMPTY.
- `pytest tests/unit/test_claude_md_branch_consistency.py -q` → 10 passed
  (post-CLAUDE.md edits).
- `ruff check brutex tests` clean; `ruff format --check` clean on every
  touched file.
- `mypy --strict brutex` → Success: no issues found in 407 source files
  (post-merge tree).
- pyright: touched files (`bad_tick.py` + `replay.py` + both test files)
  0 errors post the incidental fix above.
- `validate_ledger_numbering.py` → 1262 headers OK (on the MERGED tree —
  the pre-fix red reproduced then closed);
  `validate_citation_accuracy.py` → every citation accurate.
