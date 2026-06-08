# `cargo-fuzz` targets for `trading-core`

Test-type #4 (fuzzing) for the SHARED 52-vocab + pivots/fib core — the
bit-identical contract with TickVault (`DEC-SHARED-CORE-001`). Added under
`DEC-TRADING-CORE-FUZZ-PROPTEST-001` to close the gap where this crate had ZERO
fuzz coverage.

## Targets

| Target | What it proves |
|--------|----------------|
| `fuzz_mask_and` | `evaluate_atoms` never panics / overflows and never sets a reserved bit (52..=63) on arbitrary domain-valid input; the `u64` mask AND is total + idempotent + commutative + associative with `FULL52` as identity. |
| `fuzz_pivot_fib_resolve` | `compute_pivot_levels` + `compute_fib_levels` never panic / overflow on any valid `(pdh, pdl, pdc)`; the pivot ladder is monotone (R4≥…≥S4), every retracement sits in `[pdl, pdh]`, extensions bracket the range, and the fib midpoint equals the closed-form round-half-even. |

`fuzz_targets/common.rs` folds arbitrary bytes into trading-core's documented
honest envelope (realistic paisa range, well-ordered anchors) so the fuzzer
probes LOGIC instead of re-triggering the *intentional* corrupt-OHLC `assert!`
and i64-overflow panics (see `src/lib.rs` "Domain (honest envelope)").

## Run

```bash
cd rust/trading-core
cargo install cargo-fuzz          # one-time
cargo +nightly fuzz run fuzz_mask_and -- -max_total_time=60
cargo +nightly fuzz run fuzz_pivot_fib_resolve -- -max_total_time=60
```

Both targets have been run for 60 s with zero crashes (9.7 M and 32.4 M
executions respectively). cargo-fuzz needs the nightly toolchain (libFuzzer +
sanitizer support). This is a separate package (detached via its own
`[workspace]` table) so plain `cargo test` in trading-core never pulls
`libfuzzer-sys`.
