# ADR-0019 — Unkillable sweep run core (Phase 2 of the one-click sweep build)

**Decision ID:** `DEC-SWEEP-UNKILLABLE-CORE-001` (MIGRATION.md entry #1557)
**Date:** 2026-07-08
**Status:** **ACCEPTED** — shipped as one PR on `claude/magical-pasteur-y6gmr8`
(WP-1..7, parallel fan-out per the operator's 2026-07-07 parallel-build
override; the sibling in-flight ADR claim on 0018 is honored — this ADR takes
0019 by design, design-final §1).
**Author:** Phase-2 parallel build (design synthesized from 3 competing
designs + 3 judges; 20 decided resolutions D-1..D-20 bind every work package).
**Builds on:** ADR-0011 (stage-2 sweep parallelism — the writer-actor spine
the G3 progress tick attaches to), ADR-0017 / `DEC-SWEEP-RUST-METRICS-DEFAULT-ON-001`
(#1532 — the burn-in + autorevert stance the R45 never-adopt law mirrors),
`DEC-LAKE-HEAL-001` (#1175 — the quarantine-copy-never-delete forensics idiom),
`DEC-FETCH-HUNG-WRITE-DEADLINE-001` (#999 — the "bounded, not killable" honesty
stance the watchdog keeps), `DEC-LOOKAHEAD-BARRIER-001` +
`DEC-SWEEP-LOOKAHEAD-GUARD-001` (#1181 — the guard-complementarity framing).
**Named follow-ups minted here:** `DEC-SWEEP-LOCK-WORDING-001` (D-13 — the G10
"peer pull" refusal wording while a sweep holds LOCK_SH), the P3-owned
5-hard-gate per-door kill counters (D-8), the dashboard detach adapter
(1-line route swap at merge-integration), the mutation-floor ratchet raise to
≥85% on the four D-19 modules, and the two WP-6-discovered gaps (cache-tier
skip without sentinel cross-check; old-schema store raw abort on the
sweep→cascade spec-loader path).

---

## 1. Context

The operator's one-click sweep program (requirements register
R1–R51) demands that a multi-hour brute-force sweep be **unkillable**: never
silent, never corrupt, never lost compute, always named, always resumable
(R16's assurance contract). The 2026-07-06 75-agent gap hunt confirmed 15
concrete in-run gaps (G1–G15): a corrupt tuple store aborted the whole grid,
the 300 s stall guard false-aborted big grids, parallel mode never wrote live
progress, the heal tooling compared `time.time()` against a
`time.monotonic()`-based progress stamp (and could quarantine a LIVE run's
stores), the dashboard was blind to exit codes, two concurrent sweeps forked
the audit chain, cascade replay ignored changed bars, `/results` co-ranked
ungated rows, no keep-awake, preload raced pulls, a failed progress write
killed the run, a corrupt checkpoint crash-looped, the daemon was phone-silent
before its alerter existed, an old-schema Parquet 500'd `/results`, and
dashboard sweeps >10 min fired false STALLED alerts.

Phase 2 builds the run core that closes all fifteen plus the observability
requirements R28 (run manifest + funnel), R38 (full-chain CI rehearsal +
fault injection), R39 (live tripwires), R40 (auto diagnostic bundle), R41
(flight recorder + chain map + Telegram milestones), R42 (mid-chain break
law), R43 (one run_id), R45 (differential spot-check) — under the absolute
constraint **ENGINE UNCHANGED** (invariant I-1: `_GOLDEN_CONTENT_HASH` +
`_GOLDEN_RUN_ID` reproduce byte-for-byte with every Phase-2 default ON).

## 2. Decision (the load-bearing resolutions)

- **D-1 · run_id = `grid_identity(grid)`** (BLAKE3 over axes + repro hashes,
  NO timestamp). Resume/re-run of the same selection+data lands in the SAME
  `runs/<run_id>/`; `attempt` is an observability-only monotone int. Wall
  clock appears ONLY in observability fields, never in any identity (I-2).
- **D-2 · ONE chain core.** The flight recorder reuses
  `brutex.orchestrator.audit.AuditWriter` (SHA-256, 8-key line, resume-aware,
  torn-tail tolerant) at `runs/<run_id>/flight-recorder.jsonl` with
  `actor="flight"`, `action=<KIND>`, `subject=<run_id>`. Verifier =
  `brutex.audit_verify.auto_verify`; the rehearsal chain-break gate invokes
  THIS verifier. The supervisor BLAKE3 chain is untouched.
- **D-3 · R28 letter-fidelity.** ONE additive `RUN_MANIFEST` action (payload =
  final manifest + its content hash) rides the EXISTING CLI-level SHA-256
  sweep chain post-run; `run_persisted_sweep`'s 3-line golden contract is
  untouched.
- **D-4 · stage-3 CELLS are stamped** and validated (run_id + snapshot hash)
  before the cascade consumes; MISMATCH/MISSING ⇒ discard-and-flag + ONE
  re-run of the idempotent stage-3 pass, then hard error.
- **D-5/G13 · daemon kernel** (`CycleFailureTracker` + `escalate_after` +
  alerter-first `daemon_init_guard`) lives 100%-tested in
  `sweep_scheduler.py`; the cli daemon region is a thin adapter.
- **D-6 · traceability is a TEST**: `tests/unit/test_r16_traceability_matrix.py`
  mechanically asserts every token in {R7,R16,R28,R38–R43,R45,G1–G15} maps to
  ≥1 collected test BY NAME; the runbook table is the human view
  (`docs/runbooks/sweep-unkillable.md`).
- **D-7 · `dedup_skips` = scalar int** + additive `dedup_skips_detail
  {cache, resume, sentinel}`; the contracts memory file is updated at ship
  time (done in this PR) per its own no-drift law.
- **D-8 · per_door_kills = ABSENT-key convention** (never null, never a
  fabricated 0). Sourceable doors populated today: `min_hits`, `no_bars`,
  `cache_skip`, `resume_skip` + the six cascade doors. The 5 hard-gate splits
  stay ABSENT — P3-owned named follow-up.
- **D-9 · TUPLE_DONE decimation is lossless**: stride
  `ceil(total/10_000)`, every recorded payload carries CUMULATIVE
  `{done_cum, skipped_cum, failed_cum}`; the R39 count reconciliation reads
  `SweepProgress` counters EXACTLY, never recorder record counts;
  stage/milestone/failure records are never decimated. (The honesty note on
  R41's "one record per event" wording is ledgered in #1557.)
- **D-10 · rehearsal CI** = dedicated `sweep-rehearsal` job in ci.yml wired
  into `ci-ok.needs` (not a new branch-protection context); the fault matrix
  rides the REQUIRED `chaos-race` job; diffcheck exercises at
  `CI_K_SAMPLES = 8` every push.
- **D-11/R45 · independent naive reference simulator** in
  `sweep_diffcheck.py`: AST-pinned import ban on {trade_sim, rust_trade_sim,
  sweep_driver, rust_metrics, metrics.compute, exit_index}; int/datetime-only
  compare surface; deterministic per-tuple seeding from
  `blake3("diffcheck|{sweep_id}|{tuple_digest}|{data_snapshot_hash}")`;
  K=200 default (`BRUTEX_SWEEP_DIFFCHECK` tristate, default ON).
- **D-12 · SUSPECT latch** is a set-once plain bool; first set fans out ONCE
  (record + structlog ERROR + `alerter.run_suspect` + manifest + exit-status +
  progress). Mismatch NEVER replaces the production value (I-4 never-adopt).
- **D-15 · module homes**: manifest/stamp/recorder/tripwires/bundle/chain-map
  under `brutex/observability/`; run-lifecycle mechanics under
  `brutex/engine/`; all run artifacts under `<results_root>/runs/<run_id>/`.
- **D-16 · bundle redacts secrets** (env names matching TOKEN/SECRET/KEY);
  `ru_maxrss` is platform-normalized (macOS bytes vs Linux KiB); a probe
  failure disables the memory tripwire with ONE WARN, never fabricates.
- **D-20 · recorder degraded latch**: first append failure ⇒ ONE WARN +
  latch + drop counter; no retry spam; the run continues (fail-soft I-3).

Sixteen new 100%-line+branch modules ship (flight_recorder, run_manifest,
stage_stamp, diag_bundle, chain_map, run_tripwires under observability;
sweep_run_core, sweep_singleflight, sweep_stall_policy, sweep_liveness,
sweep_quarantine, sweep_diffcheck, sweep_exit_status, sweep_detach,
run_verdicts, sweep_artifact_guard under engine), wired at the cli
`cmd_sweep_all` seam through ONE `with sweep_run_core(...)` block plus the
G-gap seams (supervisor progress keys, parallel `on_progress`, orchestrator
quarantine + diffcheck hook, cascade atomic+stamped sidecars, heal liveness
verdict swap, daemon thin adapter, `--detach`).

## 3. Consequences

**Positive.** A break at link N never corrupts links 1..N−1 and recovery
starts at N (R42 one-line law — proven by the chaos kill/ENOSPC/corruption
matrix with before/after hashes); a corrupt tuple store or checkpoint is
quarantined forensically instead of aborting; two sweeps can never fork the
chain; the dashboard poll serves exit status + suspect with zero route edits;
every run leaves a manifest, a hash-chained timeline, a chain map, verdicts,
and a one-file diagnostic bundle; a bad run marks itself SUSPECT in minutes,
not at hour 40; the whole chain rehearses in CI on every push (measured
350.5 s for the 21-test rehearsal suite in the build sandbox, inside the
15-min job timeout).

**Negative / accepted costs.** ~seconds of per-run observability overhead
(fsync-bounded by decimation); `runs/<run_id>/` adds an artifact namespace
(structurally invisible to sweep_heal + /results discovery — pinned);
`cli.py` remains pragma-no-cover so its wiring is proven by the rehearsal
integration test rather than unit coverage.

## 4. Honest envelope (Golden Rule #10 — what is NOT claimed)

Verbatim from the binding design §10:

- The five hard-gate per-door kill counts are ABSENT keys (P3-owned ranking
  internals); follow-up DEC to thread real counters. Sourceable doors ARE
  populated (D-8).
- G8/G14 FINAL joins + dashboard detach wiring + P4 chain-map/timeline RENDER
  are other-zone 1-3-line adapters landing at merge-integration; P2 ships
  artifacts, readers, fixtures, docs. Until the detach adapter lands, the
  dashboard sweep remains in-process (the CLI/nightly path IS
  detached-capable immediately).
- Diffcheck verifies projection + trade-sim + drawdown from CERTIFIED
  bar_bits — NOT the 60 indicator evaluators (own golden/property/
  shared-crate surface); full ResultMetrics-vector compare beyond the
  drawdown 4-tuple + trade stream is follow-up. K=200 is a sampling
  assurance, not a proof (~0.004% of a 5M-mask grid).
- `estimate` in the manifest is honest null until P1's estimator contract
  adapter is wired; `preflight.refusals/clamps` fold ships fixture-tested but
  live-wires at merge-integration.
- Memory tripwire reads ru_maxrss (monotone high-water) — cannot see
  leak-then-free; thermal/SSD-unplug remain observation-closed (R16).
- Bundle build + verdicts build + stamp validation + timeline search are
  O(artifacts)/O(bytes)/O(records) — honestly O(n) on every docstring, never
  claimed O(1); only append/tick/poll surfaces claim O(1), each with a bench
  pin.
- Watchdog still DETECTS, never kills a wedged worker; a truly wedged C-level
  call parks a zombie (the #999 stance).
- G2 removes the false-abort class; real per-tuple durations vs the constant
  still close by OBSERVATION in the first supervised run (R16 honest limit).
- Telegram delivery is best-effort by house contract; "LOUD" is guaranteed on
  page artifacts + logs; phone delivery depends on notifier configuration.
- Tripwires mark SUSPECT + alert; they deliberately do NOT auto-kill the run
  (law: never silent, never lost compute; the operator decides on the alert).
- G10 refusal wording ("peer pull" while a sweep holds SH) is pinned
  misleading-but-safe debt — follow-up DEC-SWEEP-LOCK-WORDING-001.
- Rehearsal is the chain in miniature — scale-only classes (memory creep over
  days, LMDB growth, multi-day throughput decay) stay with the operator's
  smoke + supervised run per R38's own placement law.
- flock semantics are weaker on network volumes (NFS) — same envelope as
  `partition_lock` (POSIX local/external SSD; Windows out of scope).

Ship-time additions (measured/discovered during WP-6, ledgered in #1557):

- **D-19 mutation floors are RATCHETS below the 85% target** (measured,
  mutmut 2.5.1): sweep_liveness 78.9% · stage_stamp 74.4% · run_manifest
  77.9% · run_tripwires 75.6%. ci.yml gates at measured-minus-headroom floors
  so the kill rate can only rise; raising to ≥85% is a named follow-up.
- **Cache-tier skip without sentinel cross-check**: with the LMDB tuple cache
  intact, a tuple whose store was corrupted on disk AND whose sentinel is
  missing is still `skipped_cache` (never re-run) — G16-class follow-up.
- **Old-schema results store on the SWEEP→cascade spec-loader path aborts
  RAW** (`KeyError`, not `ResultsStoreError`) — the typed degrade ships on
  the /results contract path via `sweep_artifact_guard.guarded_load`; the
  sweep-path mapping is a named follow-up.

## 5. Rollback

Every Phase-2 surface is additive around an untouched engine: the byte-
identity A/B test (`test_sweep_runcore_byte_identity.py`) proves run-core ON
vs OFF produces identical per-tuple store bytes. Rollback = revert the cli
seam commit (the `with sweep_run_core(...)` wrap + G-seam wiring); the new
modules become dormant leaves. Per-feature kill switches:
`BRUTEX_SWEEP_DIFFCHECK=0` (diffcheck), `BRUTEX_SWEEP_STALL_SECONDS`
(stall policy pin), `BRUTEX_SWEEP_TRIPWIRE_*` (tripwire thresholds; RSS
fraction 0 disables the memory arm).

## 6. References

- `docs/MIGRATION.md` entry #1557 (`DEC-SWEEP-UNKILLABLE-CORE-001`).
- `docs/runbooks/sweep-unkillable.md` — operator runbook (resume, quarantine
  forensics, SUSPECT triage, failure matrix, decimation policy, traceability
  human table).
- `tests/unit/test_r16_traceability_matrix.py` — the mechanized R/G gate.
- Team memory: `sweep-oneclick-requirements-living.md`,
  `sweep-parallel-integration-contracts.md` (D-7 ship-time update in this PR).
