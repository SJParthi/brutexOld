# ADR-0011 — Stage-2 Brute-Force Sweep Parallelism (design-locked, build deferred)

- **Status:** Accepted (DESIGN ONLY — build deferred; no engine code authorized by this ADR)
- **Date:** 2026-06-23
- **Decision ID:** DEC-STAGE2-PARALLEL-DESIGN-001 (MIGRATION.md #914)
- **Supersedes / relates:** the W2-c "parallel tuples" follow-up noted in
  `brutex/engine/sweep_orchestrator.py`; complements ADR-0004 (GPU — the engine
  is intentionally CPU/Rust-first), ADR-0006/0007/0008 (Stage 2.5/3/4 design locks).

## Context

Production Stage-2 (`run_supervised_grid` → `run_sweep_grid` → per-tuple
`run_persisted_sweep`) evaluates the sweep grid **strictly sequentially**. The
CLAUDE.md §8 SLA — ~33 B trials in 2–4 days on a 10P-core M4 Pro — is only
reachable with multi-core parallelism; serial single-threaded is ~80–160 h.
Parallelizing the engine is dangerous because it guarantees **determinism /
reproducibility** (Golden Rule #7), a **hash-chained audit** (Golden Rule #8),
and **O(1) dedup** (Golden Rule #5). This ADR records the architecture that
parallelizes Stage-2 without breaking those invariants. It was produced from a
read-only reconnaissance of the live engine and an adversarial design review
(Golden Rule #9); the full working notes, hazard table, and PR sequence live in
the decision (MIGRATION.md #914).

## Key facts established (file:line evidence in the recon)

1. **Results are order-independent (the linchpin).** Each combo's output depends
   only on its `(tuple, mask)` input: `ResultsStore.flush()` sorts rows by
   `run_id` (byte-identical Parquet + content-hash regardless of append order),
   `rank_combos` is deterministic per tuple, trial-stats use `math.fsum`
   (order-independent DSR inputs), and no cross-tuple order-sensitive merge
   exists. Parallel evaluation therefore **cannot change the result files**.
2. **The hot path is GIL-bound Python** (`trade_sim.py`, no GIL release) →
   thread parallelism is ~1.0–1.5×; **process** parallelism is required.
3. **The PyO3 wheels** (`fastbloom_rs`, `fno_math`) are init-once-per-interpreter;
   the parent pre-initializes them → the pool **must use `spawn`, not `fork`**.
4. **The audit chains hash a wall-clock `ts_utc`**, so no audit chain is
   byte-stable run-to-run (not even the serial path). The honest determinism
   oracle is the **result-Parquet content-hash**, not the audit bytes.

## Decision

Adopt **process-level parallelism sharded by `(instrument, tf, proximity)`**
(preserving per-worker kernel-cache reuse), with:

- **Pool:** `ProcessPoolExecutor(mp_context=get_context("spawn"), max_workers=W)`,
  `W = min(cores-2, 10)`, `--workers` overridable (default 1 = serial).
- **Audit:** per-worker buffers → deterministic canonical-order merge by the
  parent across BOTH chains (SHA256 ingest + BLAKE3 supervisor). The merged
  chain is **structurally identical modulo `ts_utc`** (= the serial baseline),
  tamper-evident — NOT "byte-identical" (that claim was proven false and removed).
- **Resume:** replace the contiguous-prefix `SweepCheckpointStore` (breaks on
  out-of-order completion) with **sentinel-set resume** (already race-free).
- **Dedup:** none added — mask dedup is per-worker (run_ids embed tuple coords,
  no cross-shard collision); the only on-path LMDB (`SweepCache`) is already
  cross-process safe + write-rare.
- **Progress/watchdog:** re-homed to the parent aggregating worker results.

**Acceptance gate:** a golden test asserting `W=1` and `W>1` produce
byte-identical per-tuple result Parquet + content-hashes, and structurally-equal
audit chains (`ts_utc` projected out). Parallelism ships only if provably
indistinguishable from serial in result output.

## Consequences

- **Positive:** unlocks the §8 SLA; the result contract is provably unchanged;
  the hardest piece (audit buffer→merge) is de-riskable on the serial loop FIRST
  with zero concurrency.
- **Negative / honest envelope (Golden Rule #10):** no Nx speedup claim until
  benched; memory ~linear in W (per-worker bars + kernel cache); the live audit
  tail trades real-time ordering for deterministic merged order (progress stays
  live via the parent-aggregated `SweepProgress`). A Rust trade-sim with GIL
  release (thread-parallel + lower memory) is explicitly **deferred** — processes
  are used precisely to avoid that rewrite now.

## Build sequence (each a separate, fully-gated PR; PR-3 gated on operator approval)

1. Audit buffer + deterministic merge **on the serial loop** (both chains; zero concurrency).
2. Sentinel-set resume (replace the contiguous-prefix checkpoint; serial, no behavior change).
3. `spawn` process pool sharded by `(instrument, tf, proximity)`; re-home progress/watchdog; the determinism golden.
4. `SweepCache` N-writer concurrency regression test.
5. `tests/bench` throughput + memory vs W (with kernel-reuse-loss + per-worker bar-load as explicit dimensions).

## Status note

This ADR **authorizes no engine code.** It records the approved, hostile-reviewed
plan so it survives the ephemeral session and is ready to build when the operator
chooses to scale past a pilot. The brute-force ENGINE scope is unchanged
(NIFTY/BANKNIFTY/SENSEX, CLAUDE.md §3).
