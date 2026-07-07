# Runbook — absolute benchmark baseline gate (bench-m4pro)

**DEC-BENCH-ABS-BASELINE-001 (MIGRATION.md #1531).** Operator procedures for
the committed per-hardware absolute-benchmark compare gate.

## Reading a RED absolute gate

1. Open the failed `bench-m4pro` run → the compare step prints a table
   (entry · blessed ns/op · measured ns/op · ops/s both sides · ratio ·
   verdict) plus `::error::` lines naming each failing entry.
2. **Soft-drift WARNs first**: `soft context drift on 'numpy'/'os_version'`
   lines mean the STACK moved, not your code — the verdict still stands on
   the numbers (a stack perf regression IS a regression of the shipped
   stack), but the triage answer is one line away.
3. **The bounded auto-retry already ran**: a numeric red (exit 1) triggers
   ONE fresh-process re-measure inside the same job before failing — a
   still-red scheduled run means the regression reproduced across two
   independent process-level measurements. Treat it as real.
4. Both measurement reports (fragments + retry) live in the run's
   `abs-bench` artifact (90-day retention) — every raw round sample is
   there for offline triage.
5. If you suspect a hot/loaded machine anyway: quiet the runner and
   re-dispatch the workflow ONCE more before escalating. A third red is a
   code/stack regression — bisect the offending hot path.

Verdict cheat-sheet:

| Verdict | Meaning | Exit |
|---|---|---|
| `REGRESSION` | measured > blessed x (1 + tolerance/100) | 1 (retried once) |
| `SUSPICIOUS_SPEEDUP` | > 4x faster than blessed — hollowed bench or a genuine win; re-bless required (a STANDING one blocks the dispatch re-bless — see the cell-removal recovery below) | 1 |
| `STRUCTURAL` | fragment set ≠ registry / workload-hash drift / wheel missing / python-minor drift / schema | 2 (never retried) |
| `ABSTAIN_*` | no (or partial) baseline for this hardware key | 0, loud |

## The bless flow (first capture / new machine / new hot path)

Fully automatic: an abstaining scheduled run captures 3 process-fresh
measurement runs (capture REFUSES a noisy triple — cross-run spread >
tolerance/2), uploads the candidate, and the GitHub-hosted `bless-baseline`
job opens a bot PR committing the candidate VERBATIM. The operator:

1. **Nudge CI**: GITHUB_TOKEN-created PRs do not trigger CI — close/reopen
   the PR (or push an empty commit) so `CI OK (aggregate gate)` reports.
2. Review the measured table in the PR body, then merge. The merge click
   IS the bless sign-off (the bot is deliberately NOT auto-merge-trusted).

## The re-bless flow (changing a blessed number)

1. Dispatch `bench-m4pro` with `capture_candidate=true`, a NEW
   `decision_id` (e.g. `DEC-BENCH-REBLESS-BARLOOKUP-002`), and
   `replace_entries=bar_lookup` (comma-separated).
2. The capture re-measures the FULL registry, folds, and the bless PR opens
   with ONLY the baseline change: every blessed cell NOT in
   `replace_entries` is CARRIED byte-identical (its fresh measurement is
   ignored — never a refusal, never a quiet re-mint), so a single-entry
   re-bless and an additive bless both just work. For a PROACTIVE context
   re-bless while the compare is still GREEN (e.g. ahead of a planned
   runner interpreter upgrade), list EVERY blessed entry in
   `replace_entries`: a FULL replace refreshes the candidate's top-level
   context/fingerprint from the fresh measurement; a partial replace keeps
   the old context (the carried cells were measured under it). NOTE: this
   dispatch flow requires the run's own compare step to pass — a STANDING
   red of EITHER exit class, STRUCTURAL (e.g. `context.python` drift
   already in effect) or NUMERIC (e.g. a reproducing `SUSPICIOUS_SPEEDUP`
   after a genuine > 4x hot-path win merged), dies at compare before
   capture can run; use the matching removal-first recovery below.
3. **Write the MIGRATION.md entry yourself** (ledger entries are always
   human-written) and push it onto the bless branch: the entry's added
   lines MUST contain the new decision id, the literal entry key
   (`bar_lookup`), and the literal new value token exactly as serialized in
   the JSON (e.g. `55.0`). The governance gate names the exact required
   tokens in its error message if you miss one.
4. Nudge CI, merge. The gate live-verifies the cited run (exists on THIS
   repository — the `$GITHUB_REPOSITORY` repo pin, so a run on a fork or
   any foreign repo is a breach and is never even queried; is
   bench-m4pro.yml; completed with conclusion `success`; head_sha == the
   embedded commit) — a fabricated provenance is machine-refuted. The gate
   fails CLOSED (exit 2) when `$GITHUB_REPOSITORY` is unset.

## Recovering a STANDING `context.python` STRUCTURAL red (removal-first)

Once the runner's python minor has ALREADY moved, every run — scheduled OR
dispatched — fails at the compare step (STRUCTURAL, exit 2) BEFORE capture:
the capture step's `if:` carries GitHub's implicit `success()` (deliberate —
a red run never mints a candidate; wiring-test-pinned), so a
`capture_candidate=true` dispatch can NOT reach capture and can NOT heal
this red. The working recovery is REMOVAL-FIRST — the same recipe as
retiring a machine:

1. Delete `tests/bench/baselines/<key>.json` in a PR whose MIGRATION.md
   entry names the filename (the governance gate enforces it).
2. Merge. The next scheduled run ABSTAINS (green-but-loud), capture arms on
   the abstain, and the FIRST-capture path refreshes the candidate's
   top-level context/fingerprint from the fresh measurement under the
   bootstrap decision id.
3. Nudge CI on the bless PR, review the measured table, merge.

The gate is honestly dormant between steps 1 and 3 (the abstain window) —
one scheduled cycle on a healthy runner.

## Recovering a STANDING `SUSPICIOUS_SPEEDUP` NUMERIC red (cell removal)

KNOWN LIMITATION (same dispatch dead-end as the structural arm above): a
`SUSPICIOUS_SPEEDUP` is exit 1, and once a genuine > 4x hot-path win has
MERGED it reproduces on every run — scheduled or dispatched — so the
compare step fails BEFORE capture on every attempt and the "re-bless flow"
dispatch can NOT mint the ratchet candidate. There is deliberately no
red-tolerant capture arm (a red run never mints a candidate); the honest
operator recovery is cell-level removal-first:

1. REMOVE the affected entry cell(s) from `tests/bench/baselines/<key>.json`
   in a PR whose MIGRATION.md added lines name each removed entry key with
   a DEC id (the governed REMOVED-cell law — no value token / fresh run is
   required for a removal; the untouched cells keep gating).
2. Merge. The next scheduled run ABSTAINS on the removed entries
   (green-but-loud) and auto-blesses them additively at the new, faster
   measured numbers under the bootstrap decision id.
3. Nudge CI on the bless PR, review the measured table, merge.

If the speedup was instead a HOLLOWED benchmark (the workload silently
stopped doing the work), do NOT remove the cell — fix the benchmark;
the red is doing its job.

## Workload-parameter change (S11 recipe)

Changing any `ENTRY_WORKLOADS` value moves that entry's `workload_hash` and
STRUCTURAL-fails its blessed cell. The SAME PR must:

1. update the workload-hash golden in
   `tests/bench/test_absolute_baseline.py` (it pins the literals), and
2. REMOVE the affected baseline entries from every
   `tests/bench/baselines/*.json`, with a MIGRATION.md added line naming
   each removed entry key and a DEC id.

The next scheduled run abstains on the removed entries and auto-blesses
them additively under the bootstrap DEC — no multi-day red window. If the
PR forgets step 2, the next m4pro compare STRUCTURAL-fails loudly; recovery
= a follow-up PR doing step 2.

## Escalation

- Missing-baseline tracking issues are titled
  `bench-m4pro: absolute baseline missing/incomplete for <key>` (label
  `bench`, never `critical`) — bootstrap housekeeping, not an incident.
- `bench-m4pro: bless-baseline job failed …` (labels `bug,bench,critical`)
  means the bootstrap loop itself broke — fix before the gate can arm.
- Retiring a machine: delete its `tests/bench/baselines/<key>.json` in a PR
  whose MIGRATION.md entry names the filename (the governance gate enforces
  it).
