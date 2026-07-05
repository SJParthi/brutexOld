# Runbook — sweep worker-scaling A/B bench (the ADR-0016 §5 deferred bench)

> Operator procedure for producing the ONE measurement that can move
> `AUTO_SWEEP_WORKERS_CAP` (today **8**, `brutex/engine/sweep_parallel.py`).
> ADR-0016 (`docs/adr/0016-multicore-sweep-autodefault.md`,
> DEC-MULTICORE-SWEEP-AUTODEFAULT-001, MIGRATION.md #1269) §5 states: "8-worker
> THROUGHPUT is NOT benched" and "48 GB memory safety is ASSERTED, NOT
> MEASURED" — `tests/bench/test_sweep_worker_scaling.py` is that deferred
> core-scaling + RSS bench.

## 1. Honesty banner (read first — Golden Rule #10)

- **Only a real M4 Pro (14-core) run counts.** The gated body skips unless
  `BRUTEX_M4PRO_ABS=1` AND ≥ 14 CPUs are visible (max arm 12 + the 2 reserved
  cores). A shared-GHA or sandbox run either skips or — if forced — produces
  **UNVERIFIED lab numbers**: an oversubscribed measurement time-slices EVAL
  processes and structurally CANNOT prove a real >8-worker win. Every printed
  row carries `cores=` and `oversubscribed=` so a lab number can never
  masquerade as evidence.
- **`workers` is the TOTAL budget** = 1 writer-actor + `workers − 1` EVAL
  processes. Today's auto default on the M4 Pro is `workers=8` (7 EVAL); the
  measured arms are `workers ∈ {7, 8, 10, 12}` (7 = below-cap control, derived
  as `cap − 1`).
- **Storage matters:** run with the repo (and the pytest tmp dir) on the
  **internal SSD**, not the external 8 TB drive — the writer-actor's per-tuple
  `fsync` tail is part of what is being measured, and a slow external volume
  inflates the serial fraction. Record which device the run used.
- Determinism invariants are untouched: the bench drives the public
  `run_parallel_grid(workers=…)` seam only; the `_worker_init`
  OMP/RAYON/POLARS=1 pins and the small-grid sequential demotion are never
  modified, and byte-identity across worker counts is pinned by the golden
  (`tests/unit/test_engine_sweep_parallel.py::TestSequentialEqualsParallelGolden`,
  extended to workers ∈ {3, 4, 10, 12}).

## 2. Exact command (real M4 Pro, from the repo root)

```bash
source .venv/bin/activate
BRUTEX_M4PRO_ABS=1 pytest tests/bench/test_sweep_worker_scaling.py \
    -q -s -rA --tb=short -p no:cacheprovider -m bench
```

`-s` is required — the table, the `SWEEP_WORKER_SCALING` lines and the
`VERDICT:` line are printed, not asserted. (The bench also runs automatically
wherever `.github/workflows/bench-m4pro.yml` sets `BRUTEX_M4PRO_ABS=1`, once
this file is enumerated there — until then the manual command above is the
procedure.)

Expected wall time: the gated body runs a warmup + 4 arms × 3 runs of a
24-tuple × 3,750-bar sweep — single-digit minutes on the M4 Pro
(UNVERIFIED until first run; the smoke/helper tests alone finish in seconds).

## 3. What to read

One machine-readable line per arm:

```
SWEEP_WORKER_SCALING workers=10 eval=9 median_s=… tuples=24 tuples_per_s=… \
  baseline_w=7 speedup_vs_baseline=… speedup_vs_cap=… cores=14 oversubscribed=0
```

- `median_s` — median of 3 timed runs (`time.monotonic`), each in a FRESH
  results root (a reused `SweepCache` would short-circuit tuples and fake a
  speedup). Pool spawn cost is deliberately INSIDE the clock — more workers
  really do spawn more interpreters.
- `speedup_vs_cap` — the decision axis: `median(cap=8) / median(W)`.
- `speedup_vs_baseline` — vs the `workers=7` below-cap control (sanity: 8
  should not lose to 7; if it does, the writer-actor tail already dominates).
- `rss parent_peak / child_peak` — `getrusage` peaks (bytes-normalised;
  `RUSAGE_CHILDREN` is the cumulative max over all dead children, not
  per-arm) and `rss projected @ workers=12` — `parent + 11 × child_peak`,
  read against the 48 GiB envelope. Printed, never hard-gated.
- `VERDICT: RAISE-ELIGIBLE(W)` or `VERDICT: KEEP CAP=8` — the throughput arm
  of the decision rule only.

## 4. Decision rule (the only way the cap moves)

Raise `AUTO_SWEEP_WORKERS_CAP` 8 → W **only if ALL of**:

1. Run on the M4 Pro runner (`BRUTEX_M4PRO_ABS=1`, ≥ 14 visible CPUs, core
   guard passed — i.e. `oversubscribed=0` on every arm).
2. `median_s(8) / median_s(W) ≥ 1.15` (`RAISE_WIN_THRESHOLD` — a ≥ 15% real
   win, above scheduler noise; a smaller "win" is the #906 trap: noise burned
   as churn) for W ∈ {10, 12}.
3. The RSS projection at W stays well under 48 GiB.
4. The byte-identity golden is green at W (already pinned for 10 and 12).

Oversubscribed, core-limited or non-M4-Pro measurements **cannot** satisfy
rule 1 — label any such numbers UNVERIFIED wherever they are quoted.

## 5. If the verdict is RAISE-ELIGIBLE(W)

Follow-up PR (separate from the bench, per ADR-0016 §5's "one-line revert
lever" symmetry):

1. `AUTO_SWEEP_WORKERS_CAP = W` in `brutex/engine/sweep_parallel.py` + update
   its honesty comment with the measured numbers (file:line-cited).
2. Flip the `(10, 8)` / `(14, 8)` rows in
   `TestAutoSweepWorkers.test_auto_formula`
   (`tests/unit/test_engine_sweep_parallel.py`).
3. Amendment section in `docs/adr/0016-multicore-sweep-autodefault.md` +
   a MIGRATION.md entry citing the measured table verbatim.
4. Re-run this bench after the change (the arms re-derive from the new cap
   automatically — `worker_counts()` is cap-derived, never hardcoded).

If the verdict is `KEEP CAP=8`, record the measured table in the MIGRATION
entry anyway — the ADR-0016 §5 "NOT benched" caveat is then CLOSED with
evidence, and the cap stands as a measured (no longer projected) optimum.
A sharded writer-actor (> 1 consumers) is the only lever past the fsync
Amdahl wall — explicit FUTURE work, NOT claimed; it gets its own decision ID
when it ships.

## 6. Measurement log

| Date | Runner | cores | Arms (workers → median s) | oversubscribed | Verdict | Record |
|------|--------|-------|---------------------------|----------------|---------|--------|
| 2026-07-05 | 4-core Linux sandbox (NOT the M4 Pro) — **UNVERIFIED lab, non-binding** | 4 | rep 1: 7→3.166 · 8→3.276 · 10→3.683 · 12→4.186; rep 2: 7→3.197 · 8→3.243 · 10→3.732 · 12→4.140 | every arm | KEEP CAP=8 (structural veto: cores < 14; empirical: 10/12 SLOWER than 8, `speedup_vs_cap` 0.87–0.89 / 0.78) | DEC-SWEEP-CAP-BENCH-001, MIGRATION.md #1356 |

The in-repo gated body SKIPPED at its core guard on that runner (as designed);
the numbers came from an out-of-band driver over the same helpers. Result-tree
digests were identical across all four arms in both repetitions. **The M4 Pro
row of this table is still empty — producing it (§2) is the only path to an
evidence-based cap change.**
