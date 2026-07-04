# ADR-0016 — Multicore sweep worker AUTO-default (`BRUTEX_SWEEP_WORKERS` unset → `max(1, min(cpu_count − 2, 8))`)

**Decision ID:** `DEC-MULTICORE-SWEEP-AUTODEFAULT-001` (MIGRATION.md entry #1264)
**Date:** 2026-07-04
**Status:** **ACCEPTED** — the flip this ADR owns ships in the same PR
(resolution-only; the engine per-tuple path is untouched).
**Author:** SJParthi (operator-directed engine-scale pack)
**Executes the deferral of:** `DEC-MULTICORE-SWEEP-PHASE1-001` (MIGRATION.md
entry #1080), whose "DEFERRED to follow-up PRs" list states verbatim: *"and
RAISING the default worker count above 1 (an operator decision behind an ADR —
this slice ships default 1, byte-identical)"*. This is that ADR + that flip.
**Builds on:** ADR-0011 (the Stage-2 sweep-parallelism design lock),
`DEC-MULTICORE-SWEEP-PHASE1-001` (#1080 — the `ParallelGridDriver` +
writer-actor + reorder heap + spawn-context pool this default now engages),
`DEC-SWEEP-RUST-TRADESIM-WIRE-001` (#1182 — the resident Rust trade-sim each
worker runs under the `RAYON_NUM_THREADS=1` determinism pin).

---

## 1. Context

`brutex/engine/sweep_parallel.py` (#1080) shipped the multi-core grid-sweep
driver DEFAULT-OFF: `resolve_sweep_workers` read `BRUTEX_SWEEP_WORKERS` and
fell back to `1` (the sequential `run_supervised_grid` path, byte-for-byte)
when the variable was unset, blank, non-integer, or `< 1`. The correctness of
the parallel path is already pinned by the sequential==parallel byte-identity
goldens (`tests/unit/test_engine_sweep_parallel.py`
`TestSequentialEqualsParallelGolden`: identical per-tuple Parquet tree +
sidecars + sentinels, audit chains content-identical modulo timestamps AND
verifying, identical checkpoint). What remained was the OPERATOR DECISION to
make multicore the default — deferred behind this ADR.

## 2. Decision

`resolve_sweep_workers` resolution semantics change to (all O(1), one loud
`sweep.workers.resolved` structlog event per call carrying `requested_env`
(repr) / `cpu_count` / `auto` / `tuple_count` / `resolved` / `reason`):

1. **Unset / blank env → AUTO** = `max(1, min(os.cpu_count() − 2, 8))`
   (`auto_sweep_workers`). Reserve 2 CPUs (the CLAUDE.md §8 "2 reserved"
   cores), cap at 8 total workers (1 writer-actor + 7 EVAL processes).
   `os.cpu_count()` returning `None` → treated as 1 → auto = 1 (sequential;
   reason `cpu_count_none` — never guess parallelism on an undetectable box).
2. **Explicit `BRUTEX_SWEEP_WORKERS=1` → 1.** The sequential opt-out stays,
   verbatim, never demoted or promoted (reason `env_explicit`).
3. **Explicit valid env `> 1` → that value.** An env override beats auto —
   even above the auto cap (reason `env_explicit`).
4. **INVALID env (non-integer / `< 1`) → AUTO** (reason `env_invalid_auto`,
   logged at WARNING — see §4, the invariant reversal this ADR owns).
5. **Small-grid fallback (AUTO path only):** when the caller supplies the
   sweep's `tuple_count` and `tuple_count < 2 × auto` (with `auto > 1`),
   resolve to 1 (reason `small_grid_sequential`) — pool spawn + result
   marshalling is not worth fewer than ~2 tuples per prospective worker. The
   boundary is strict: `tuple_count == 2·auto − 1` → 1, `== 2·auto` → auto.
   The CLI launcher (`cmd_sweep_all`) threads `count_sweep_tuples(grid)` in —
   the one call site that knows the grid. An explicit env override is never
   demoted by this rule.

The CPU probe is injectable (`cpu_count_fn`, default `os.cpu_count` resolved
at call time) so every branch is deterministic under test. This introduces the
FIRST `os.cpu_count()` usage in `brutex/` (verified by grep before the change).

## 3. Scope — what this ADR does NOT change

- **Resolution-only.** The engine per-tuple path, the `ParallelGridDriver`,
  the writer-actor apply order, the spawn context, the determinism pins, the
  digest-path scope and its legacy-path hard-refuse are ALL byte-untouched.
- The **correctness net is the EXISTING sequential==parallel byte-identity
  goldens** (#1080) — they pass UNMODIFIED under this PR; whichever count the
  resolver picks, the result tree is the same bytes.
- ENGINE SCOPE UNCHANGED (NIFTY/BANKNIFTY/SENSEX + the ADR-0012 NIFTY-futures
  track); no vocab / rate / lake / schema change.

## 4. The invariant REVERSAL this ADR owns (invalid env → auto)

`resolve_sweep_workers`'s prior docstring documented the invariant: *"a bad
value can never silently switch on parallelism"* — an invalid
`BRUTEX_SWEEP_WORKERS` fell back to the sequential 1. **This ADR consciously
REVERSES that invariant: an invalid value now resolves to the AUTO count,
i.e. a bad value CAN enable parallelism.** This is a deliberate,
operator-authorized flip, not an accident:

- **Rationale.** Once AUTO is the default, "invalid → 1" would mean a typo
  (`BRUTEX_SWEEP_WORKERS=fourr`) silently degrades a 14-core sweep to ONE core
  — the opposite illusion: days of silently-lost throughput on a multi-day
  production sweep, which is the harder failure to notice than extra cores
  being used. Invalid-input behavior should converge to the DEFAULT behavior,
  and the default is now auto.
- **Not silent.** The invalid value is logged LOUDLY: the ONE
  `sweep.workers.resolved` event is emitted at WARNING level whenever the env
  value was invalid, naming the offending value via `repr` — pinned by a
  dedicated regression test.
- **Safety floor.** Parallelism engaged this way is the SAME goldens-pinned
  byte-identical path; the blast radius of the reversal is resource usage,
  never correctness.
- The code comment at the `env_invalid_auto` branch names this ADR as the
  owner of the flip.

## 5. Honest envelope (Golden Rule #10)

- **48 GB memory safety is ASSERTED, NOT MEASURED.** The #1080 deferred
  "pytest-benchmark core-scaling + RSS-vs-48GB memory bench" remains DEFERRED
  — this ADR asserts the 8-worker default fits the CLAUDE.md §8 48 GB envelope
  on the reasoning that each spawn-context worker holds one grid + one bars
  slice + one resident kernel (bounded by the driver's O(pool_size) heap cap),
  but NO RSS bench ran here. If the deferred bench falsifies the assertion,
  the cap constant (`AUTO_SWEEP_WORKERS_CAP`) is the one-line revert lever.
- **8-worker THROUGHPUT is NOT benched.** No speedup number is claimed for
  the 8-worker default — the #1080 "~6–8×" remains a PROJECTION pending the
  same deferred core-scaling bench. The cap is a conservative ceiling inside
  the "10 cores used by sweep, 2 reserved" envelope, not a measured optimum.
- The small-grid `2×` factor and the `−2` CPU reservation are HEURISTICS
  (operator-tunable via this one module), not measured optima.
- A CI runner or laptop with `cpu_count ≤ 3` auto-resolves to 1 — sequential
  behavior there is byte-identical to pre-ADR.

## 6. Consequences

- A production `brutex sweep-all` on the M4 Pro reference machine now uses 8
  workers by default (was 1) — the #1080 driver finally engages without an
  env flag; tiny research grids stay sequential automatically.
- `DEFAULT_SWEEP_WORKERS` (=1, no longer the default) is renamed
  `SEQUENTIAL_SWEEP_WORKERS` (the opt-out + small-grid fallback constant);
  `auto_sweep_workers` / `AUTO_SWEEP_WORKERS_CAP` /
  `AUTO_SWEEP_WORKERS_RESERVED_CPUS` / `SMALL_GRID_FACTOR` are new public
  names.
- CI suites that require the sequential path already set
  `BRUTEX_SWEEP_WORKERS=1` explicitly (the opt-out) and are unaffected.

## 7. Verification

`tests/unit/test_engine_sweep_parallel.py` — cpu fixtures {1, 2, 4, 10, 14,
None} through the injectable seam; env-override-beats-auto; explicit-1
opt-out; invalid-env→auto with the WARNING log pinned; the strict small-grid
boundary (`2·auto − 1` → 1, `2·auto` → auto); small-grid never demotes an
explicit override; the `sweep.workers.resolved` event fields. END-TO-END:
`tests/integration/test_cli_sweep_all_dedup.py`
`test_cmd_sweep_all_small_grid_auto_resolves_sequential` — a real tiny
`cmd_sweep_all` sweep with env unset on a simulated 14-core box dispatches the
SEQUENTIAL path (the parallel driver poisoned to fail the test if entered).
The #1080 byte-identity goldens pass UNMODIFIED. `sweep_parallel.py` at 100%
line + 100% branch.
