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
