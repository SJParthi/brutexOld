# Rust sweep-core conversion — CHECKPOINT-1 audit artifact pack

**Status: LOCKED FOR CHECKPOINT 1.**

This directory is the CHECKPOINT-1 audit artifact pack for the Rust
sweep-core conversion (DEC ids pending the implementation PRs — the
implementation slices land on this branch next with their own
MIGRATION.md ledger entries). This commit is **documentation-only** —
no `brutex/` or `rust/` code is included.

## Contents

| File | What it is |
|---|---|
| `SPEC_RUST_SWEEP_CORE.md` | The conversion spec (scope, FFI surface, identity contract) |
| `DESIGN_FINAL_RUST_SWEEP_CORE.md` | The synthesized final design (4 hostile-review rounds folded; §9/§9.1 hold the binding perf law) |
| `baseline.md` | Measured sandbox baseline (per-mask / per-kept / per-enumerated cost decomposition) |
| `combo_counts.md` | Grid combinatorics (its §3 wall-clock arithmetic is WITHDRAWN pending the on-branch rebuild — see DESIGN §9) |
| `external_findings_verifier_b.md` | Verifier B's external findings (counterparty-relayed anchors labelled as such) |
| `perf_RECONCILIATION.md` | The walk-rate discrepancy reconciliation (two-term fit; from `perf_reconcile/RECONCILIATION.md`) |
| `fit_results_pinned.json` | Pinned-config fit results backing the reconciliation |
| `reads/` | Codebase read notes (dedup, identity, parallel, rust, spine, tests) |
| `candidates/` | The three design candidates the final design was synthesized from (correctness / perfmax / riskmin) |

## Headline honesty (binding — see DESIGN §9.1)

- **Bit-identity ship conditions:** float-layer ownership by CONSTRUCTION
  (the entire #1186 libm / None / boundary / floor-div class stays in
  today's Python `combine_metrics_from_folds`); trades NEVER cross the
  FFI; the zero-trade §1.3 short-circuit law is byte-observable via the
  in-band `scans_run` count; per-slice golden proof + the 64-mask
  dual-oracle burn-in + the S2 kernel-ratio gate (≤ ~1.15× of the
  same-fixture kernel sum, kernel+seam scope only).
- **Honest perf band:** the BINDING two-term cost law is
  `cost(mask) = c0 + ≈6.2 ns/mask·bar (walk) + ≈4.1 µs/trade` (pinned
  production config, `RAYON_NUM_THREADS=1`). Campaign band from the FFI
  port ALONE: **≈1.3–3.1× shape-dependent** (≈2.3–2.5× at the measured
  FIX-L median density). Day-level k≤6 wall-clock is **NOT** reachable
  from the port alone — the previously-quoted absolute day figures are
  WITHDRAWN (unit conflation) and day-level k≤6 requires the NAMED
  algorithmic levers (DESIGN §10): hit-list-intersection prefilter,
  parallelism topology (in-kernel rayon, measured ≈3.0–3.77× on heavy
  tuples), entry-sparsity exploitation, kept-vs-enumerated work shaping.

## Audience

Independent cross-container audit: Verifier A / Verifier B + the
coordinator. Do not merge this branch on the design pack alone.
