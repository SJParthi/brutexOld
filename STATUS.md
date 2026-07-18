# Observability lane status — WAVE FIX PUSHED (2026-07-18, head c22e4e06513d48fb060ae005ba06cf5f86718745)

Lane: `claude/rust-observability-lane` (DEC-RUST-OBSERVABILITY-001,
MIGRATION.md **#1956** provisional — renumbered, see (d)).

**New head:** `c22e4e06513d48fb060ae005ba06cf5f86718745`
("fix(observability): register brutex-observability as an optional
kernel"), on top of the merge commit `dcfab2ee` (origin/main tip
`3ee742b3` folded). Pushed `fec2aba..c22e4e0` (plain push, first
attempt); PR #1647 CI auto-refired.

## (a) OPTIONAL_KERNELS registration

`brutex_observability` registered in the guard test's module-level
registry (`tests/unit/test_run_builds_rust_kernels.py::OPTIONAL_KERNELS`)
as `("brutex/observability/observability_rust.py",
".github/workflows/observability-rust-parity.yml")` — the entry mirrors
the `brutex_strategies` row shape, with the mandated documented-reason
comment ("deliberately default-OFF import-guarded facade
(BRUTEX_OBSERVABILITY_RUST), built + tested by its own
observability-rust-parity CI lane, not wired into the always-on kernel
build") + the DEC-RUST-OBSERVABILITY-001 / MIGRATION.md #1956 cite + the
inverse-pin clause. The 3 pre-fix failing arms (import / Makefile
run-raw / bootstrap × brutex_observability) confirmed failing pre-fix;
guard file **30/30 green** post-fix.

## (b) CodeQL cycle — already fixed in fec2aba

The facade→consumer module-level import cycle was closed in `fec2aba`
via structural Protocols — the imports were removed ENTIRELY, because a
TYPE_CHECKING-only import still counts as a CodeQL cycle edge. No
further CodeQL work in this wave.

## (c) Merge-forward onto main 3ee742b3

Merge SHA `dcfab2ee52dfa156df6ce93e6a646b4f7da49050`. Conflicts:
`.gitignore` (union — both Cargo.lock negations kept:
brutex-observability + main's brutex-strategies) and `CLAUDE.md`
(ours-lead in header/§1/§3; main's five lead blurbs demoted VERBATIM to
Earlier: in main's own recency order). `docs/MIGRATION.md` +
mutation.yml + conftest auto-merged clean. NO conflict touched the four
law modules or the crate/facade. (Wheel staleness: main's Vehicle #1
changed rust/fno-math ⇒ fno_math wheel rebuilt via
`maturin develop --release`; import smoke green.)

## (d) LEDGER RENUMBERED #1954 → #1956

Main took **#1954** (DEC-TEST-AWS-HERMETICITY-001) + **#1955**
(DEC-STRATEGIES-RUST-COMBO-001), so our entry renumbered
**#1954 → #1956** (next free on the merged ledger; tail scan confirmed
nothing ≥1956 existed) with the in-entry renumber-chain note per
DEC-LEDGER-RENUMBER-001; CLAUDE.md header/§1/§3 references swept
#1954 → #1956 (residual #1954 mentions are inside main's demoted
hermeticity blurbs + our chain notes — correct; no code file referenced
#1954, grep-verified).

**RELAY ACTION REQUESTED:** the PR #1647 body's "#1954 (PROVISIONAL)"
mentions are now STALE — please update them to "#1956 (provisional)"
(the PR body is relay-owned).

## (e) Post-fix gates (final tree @ c22e4e0)

- Guard file `test_run_builds_rust_kernels.py`: **30 passed**.
- The four ci.yml module stanzas, UNMODIFIED test lists, all 100.00%:
  sla 92/16 (32) · watchdog 71/8 (25) · loop_watchdog 190/46 (52) ·
  stage_stamp 152/42 (22).
- Lane suites wheel-present: **176 passed** (unit 166 + integration 10).
- Flag-ON parity (BRUTEX_OBSERVABILITY_RUST=1): **307 passed**.
- Protected consumer/law batch, ONE run, ZERO edits (`git diff` empty
  over the set): **749 passed, 1 pre-existing env-only skip**.
- `validate_ledger_numbering.py`: 1398 entry headers, every
  (number,suffix) pair unique.
- `test_claude_md_branch_consistency.py`: 10 passed.
- `validate_citation_accuracy.py`: green (every citation accurate).
- ruff check + ruff format --check: clean.
- `mypy --strict brutex`: 0 new (the one pre-existing env-only `cma`
  import-not-found).
- check-conventional-commits.sh origin/main..HEAD: **11/11 green**.
- Push: `fec2aba..c22e4e0` plain, CI auto-refired.

---

# Observability lane status — CODEQL FIX PUSHED (2026-07-18)

Lane: `claude/rust-observability-lane` (DEC-RUST-OBSERVABILITY-001,
MIGRATION.md #1954 provisional).

**New head:** `fec2aba8985444374adb7e02643b3bcbc2747961` — commits
`14fdc57` (fix(observability): break module-level import cycle between
facade and consumers) + `fec2aba` (test(observability): pin the
structural stand-ins' property descriptors), on top of the main merge
`3c56600a`. Pushed to `origin/claude/rust-observability-lane`.

## Fix shape

Facade-local structural Protocols (`WatchdogStateLike` /
`WatchdogVerdictLike` — @runtime_checkable, read-only `@property`
members) + variance-correct generic TypeVars (`_PredicateT` bound=str
for `SLAPredicate`, etc.) replace ALL facade→consumer imports — CodeQL
counts TYPE_CHECKING imports as module-level cycle edges, so guarding
was insufficient; the imports had to go. Zero consumer edits (consumer
call sites still solve to the concrete types via the generic routed
laws). Runtime-inert: the routed-body diff is annotations only;
flag-OFF byte-identity, refuse-poison gates, burn-in and the reset seam
are untouched. The AST no-brutex-import pin
(`test_facade_source_carries_no_brutex_import`) is the regression net.

## Gates re-proven (final tree @ fec2aba)

- mutmut: **303/303 killed, 0 survivors, 0 suspicious** — a novel
  @property-decorator-strip mutant class was found (13 run-1 survivors,
  runtime-invisible to isinstance presence probes) and killed via
  `inspect.getattr_static` descriptor-kind pins; flagged for any future
  property-stub-protocol pair.
- Facade coverage: 320/92, 0 missed, 100.00% BOTH arms
  (wheel-present + wheel-absent).
- Suites: wheel-present 176 passed; wheel-absent 153 passed + 14
  skipped.
- The four ci.yml module stanzas: 100.00% over UNMODIFIED test lists
  (sla 92/16 · watchdog 71/8 · loop_watchdog 190/46 · stage_stamp
  152/42).
- Protected consumer/law battery (29-file superset): 680 passed,
  1 pre-existing env-only skip; zero edits to any.
- Flag-ON parity (BRUTEX_OBSERVABILITY_RUST=1): 307 passed.
- mypy --strict: 0 new (the one pre-existing env-only `cma` error);
  pyright: 0 errors.

## Transport note

Reply channel down in the authoring session — this branch is the
pushed-confirmation the coordinator asked for; PR #1647 head is now
fec2aba, CI auto-refired.
