# `cargo-fuzz` target list for `fno-math`

This directory holds the cargo-fuzz harness for the Rust hot kernel.
The 7 planned fuzz targets per the verification pyramid:

| # | Target | Purpose | Status |
|---|---|---|---|
| 1 | `fuzz_combo_hash` | combo-hash never panics or hangs on any input | 📋 ROADMAP |
| 2 | `fuzz_mask_and` | u64 mask AND popcount is total + commutative + bounded | ✅ LIVE (DEC-FUZZ-HOTKERNEL-001) |
| — | `fuzz_supermask` | SUPERSET count `(bar&mask)==mask` is total + bounded + batch==single | ✅ LIVE (DEC-FUZZ-HOTKERNEL-001) |
| 3 | `fuzz_ema_update` | EMA never produces NaN / Inf / overflow | 📋 ROADMAP (kernel not yet built) |
| 4 | `fuzz_pivot_resolve` | pivot resolver tolerates any in-domain (PDH, PDL, PDC) triple | ✅ LIVE — `trading-core/fuzz/fuzz_pivot_fib` (kernel lives in the shared crate, DEC-SHARED-CORE-001) |
| 5 | `fuzz_fib_resolve` | fib resolver tolerates any in-domain (PDH, PDL) pair | ✅ LIVE — `trading-core/fuzz/fuzz_pivot_fib` |
| — | (evaluator) | the 52-condition u64 evaluator never panics + only sets bits 0..=51 | ✅ LIVE — `trading-core/fuzz/fuzz_evaluator` |
| 6 | `fuzz_audit_chain` | hash-chained JSONL reader never panics on adversarial input | 📋 ROADMAP (kernel not yet built) |
| 7 | `fuzz_parquet_reader` | Parquet reader never panics on corrupt files | 📋 ROADMAP (kernel not yet built) |

## Status

🟢 PARTIAL — DEC-FUZZ-HOTKERNEL-001 (MIGRATION.md #585) activated the two
`fno-math` mask kernels here (`fuzz_mask_and`, `fuzz_supermask`) and the
pivot/fib + 52-vocab targets in `rust/trading-core/fuzz/` (those kernels live
in the shared crate per DEC-SHARED-CORE-001). The remaining four targets stay
ROADMAP until their hot-path kernels exist.

### Honest build note

`fno-math` is a PyO3 `extension-module`: its kernels are only reachable through
the `#[pyfunction]` surface, which references Python C-API symbols. A normal
`cargo test` dodges this by testing the *private* pure cores, but a fuzz
EXECUTABLE that calls the public `#[pyfunction]`s must link libpython — so
`fuzz/build.rs` resolves it via `python3.12-config`. The fuzzed code path is
pure integer work and never initialises the interpreter, so resolving the
symbols is sufficient. (`trading-core/fuzz` has no such need — it builds the
crate's dependency-free pure-Rust core with `default-features = false`.)

## Activation / reproduce (nightly Rust + cargo-fuzz)

```bash
rustup toolchain install nightly
cargo install cargo-fuzz
cd rust/fno-math
cargo +nightly fuzz run fuzz_mask_and  -- -max_total_time=60
cargo +nightly fuzz run fuzz_supermask -- -max_total_time=60
```

Last local run (DEC-FUZZ-HOTKERNEL-001, nightly 1.98 + cargo-fuzz 0.13.1):
`fuzz_mask_and` 7,646,580 execs / `fuzz_supermask` 3,627,225 execs — **0
crashes**. CI integration via a sibling `.github/workflows` fuzz job remains a
tracked follow-up (not wired yet).

## Decision

DEC-PHASE1-WEEK2-SCAFFOLDING-001 (MIGRATION entry #252) — scaffolding;
DEC-FUZZ-HOTKERNEL-001 (MIGRATION entry #585) — activation of the two mask
targets above.
