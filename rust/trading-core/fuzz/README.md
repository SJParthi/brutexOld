# `cargo-fuzz` targets for `trading-core`

The shared evaluate-time indicator + 52-condition vocabulary kernel
(`DEC-SHARED-CORE-001`, MIGRATION.md #441) is the bit-identical core BruteX
(PyO3) and TickVault (native) both consume, so its panic/overflow safety on
adversarial input is load-bearing for both repos. These libFuzzer targets were
activated by **DEC-FUZZ-HOTKERNEL-001** (MIGRATION.md #585).

| Target | Kernel | Asserts |
|---|---|---|
| `fuzz_pivot_fib` | `compute_pivot_levels` + `compute_fib_levels` (`src/indicators/`) | no panic; monotone ladder `R4≥R3≥PDH`, `S4≤S3≤PDL`, `PDL≤P≤PDH`; `range≥0`; 50% retracement in band; up/down extensions on the correct side |
| `fuzz_evaluator` | `evaluate_atoms` (`src/evaluator.rs`) | no panic; only bits 0..=51 set (reserved 52-63 always clear); every mutually-exclusive condition pair holds; ≤1 time-of-day bucket fires |

## Honest domain (Golden Rule #10 — matches `src/lib.rs`)

`trading-core` **intentionally panics** on (a) corrupt prev-day OHLC
(`pdh < pdl`, or `pdc ∉ [pdl, pdh]`) via an always-on `assert!` mirroring
Python's `ValueError`, and (b) absurd `i64`-scale magnitudes (`> ~10¹⁷` paisa)
via the release `overflow-checks` — both are documented CONTRACT behaviour, not
bugs (the crate's own unit tests cover them). So these fuzzers map arbitrary
bytes into the **realistic envelope** the crate is specified for: prices in
`[1, ₹100,000,000]` (paisa), OHLC ordered, `PDH≥PDL`, `PDC∈[PDL,PDH]`,
proximity bps `∈[0,10000]`. At that cap every internal product stays far below
`i64::MAX`, so **panic/overflow-freedom is the real invariant under test**.

**Python⇆Rust bit-identity** is NOT re-asserted inside libFuzzer (it would need
an embedded interpreter); it is covered by the `tests/golden_vectors.rs`
400-case answer-key. These targets add panic-freedom + structural invariants
over the full in-domain input space.

This fuzz crate builds the parent with `default-features = false` (the
dependency-free pure-Rust core — **no libpython**, unlike `fno-math/fuzz`), and
is a separate, non-published, NON-workspace package, so it never perturbs the
parent crate's `cargo build` / `cargo test` / coverage gates.

## Reproduce (nightly Rust + cargo-fuzz)

```bash
rustup toolchain install nightly
cargo install cargo-fuzz
cd rust/trading-core
cargo +nightly fuzz run fuzz_pivot_fib -- -max_total_time=60
cargo +nightly fuzz run fuzz_evaluator -- -max_total_time=60
```

Last local run (DEC-FUZZ-HOTKERNEL-001, nightly 1.98 + cargo-fuzz 0.13.1):
`fuzz_pivot_fib` 31,164,014 execs / `fuzz_evaluator` 7,719,702 execs — **0
crashes**.
