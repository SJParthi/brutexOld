# `cargo-fuzz` target list for `fno-math`

Phase 1 Week 2 scaffolding placeholder per `docs/TRACK1_PHASE_PLAN.md`.

This directory holds the cargo-fuzz harness for the Rust hot kernel.
The 7 planned fuzz targets per the verification pyramid:

| # | Target | Purpose | Phase |
|---|---|---|---|
| 1 | `fuzz_combo_hash` | combo-hash never panics or hangs on any input | Phase 2 |
| 2 | `fuzz_mask_and` | u64 mask AND is total + commutative + associative | Phase 2 |
| 3 | `fuzz_ema_update` | EMA never produces NaN / Inf / overflow | Phase 2 |
| 4 | `fuzz_pivot_resolve` | pivot resolver tolerates any (PDH, PDL, PDC) triple | Phase 2 |
| 5 | `fuzz_fib_resolve` | fib resolver tolerates any (PDH, PDL) pair | Phase 2 |
| 6 | `fuzz_audit_chain` | hash-chained JSONL reader never panics on adversarial input | Phase 2 |
| 7 | `fuzz_parquet_reader` | Parquet reader never panics on corrupt files | Phase 2 |

## Status

📋 ROADMAP — each fuzz target lands alongside the corresponding hot-path
implementation in Phase 2. Until then this README is a placeholder so
the directory exists in git and `cargo-fuzz init` runs against the
existing structure cleanly.

## Activation

When Phase 2 wires the first fuzz target:

```bash
cd rust/fno-math
cargo install cargo-fuzz
cargo fuzz init                    # one-time
cargo fuzz add fuzz_combo_hash     # per target
cargo fuzz run fuzz_combo_hash     # local 60s probe
```

CI integration via `.github/workflows/nightly.yml` (already wires
`stress-nightly` + `redteam-nightly`); the fuzz workflow lands as a
sibling workflow when the first target activates.

## Decision

DEC-PHASE1-WEEK2-SCAFFOLDING-001 (MIGRATION entry #252).
