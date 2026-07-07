# `cargo-fuzz` targets for `trading-core`

The shared evaluate-time indicator + 60-condition vocabulary kernel
(`DEC-SHARED-CORE-001`, MIGRATION.md #441) is the bit-identical core BruteX
(PyO3) and TickVault (native) both consume, so its panic/overflow safety on
adversarial input is load-bearing for both repos. These libFuzzer targets were
activated by **DEC-FUZZ-HOTKERNEL-001** (MIGRATION.md #585) and extended by the
security-audit fuzz workstream (2026-07-06), which also **repaired the drifted
`fuzz_evaluator`**: it had rotted un-compilable when the vocabulary widened
u64→u128 (DEC-VOCAB-U128-CODEGEN-001) and `IndicatorSnapshot` gained
`vwap_paisa` + `market_structure` — invisible because no CI built the fuzz
crate (fixed by `.github/workflows/rust-fuzz-smoke.yml`).

| Target | Kernel | Asserts |
|---|---|---|
| `fuzz_pivot_fib` | `compute_pivot_levels` + `compute_fib_levels` (`src/indicators/`) | no panic; monotone ladder `R4≥R3≥PDH`, `S4≤S3≤PDL`, `PDL≤P≤PDH`; `range≥0`; 50% retracement in band; up/down extensions on the correct side |
| `fuzz_evaluator` | `evaluate_atoms` (`src/evaluator.rs`) | no panic; only bits 0..=59 set (60-127 reserved, always clear); every mutually-exclusive pair (EMA/VWAP/PDH/PDL/bull-bear/gap/inside-outside/thirds) holds; ≤1 time-of-day bucket; Group-12 bits mirror the resolved BOS/CHoCH flags exactly; VWAP warm-up fires no bit |
| `fuzz_market_structure` | `MarketStructureState` (`src/indicators/market_structure.rs`) | no panic for `k∈[1,4]` over arbitrary bar streams; warm-up silence before `2k+1` bars; per-side BOS/CHoCH exclusivity; trend coherence; swing provenance (a confirmed swing is always a fed bar's high/low — never fabricated/future); mid-stream clone determinism |
| `fuzz_spec` | `StrategySpec` serde + `mask_hex` + `Direction::from_u8` + `StrategySpec::new` (`src/spec.rs`) | no panic on ARBITRARY JSON bytes / hex strings (the crate's only untrusted-bytes boundary); lossless round-trip; `canonical_bytes` fixed 57-byte layout + determinism; `from_u8` total (Ok iff 0/1); admission iff no reserved bit |
| `fuzz_metadata` | `derive_bar_metadata` / `weekday_from_ordinal` / `minute_of_session` / `resolve_near_month` (`src/indicators/metadata.rs`) | no panic over the documented domain; exact civil arithmetic; weekday `≤6` + advances (mod 7); minute-of-session anchored/floored/monotone (pre-open honestly negative); `is_expiry_day` iff ordinal equality; near-month roll decision exact |
| `fuzz_greeks` | `greeks` / Black-76 + IV solve (`src/greeks/mod.rs`) | phase 1: NO PANIC on raw IEEE-754 bit patterns (NaN/±∞/subnormals) + only the two documented error classes; phase 2 (in-domain): iv finite + in-clamp, delta within the TRUE envelope `±e^{−rT}`, `gamma/vega ≥ 0`, theta/rho finite, price finite + non-negative up to cancellation, solve path reproduces the traded price ≤ 1e-6 relative |

## Findings from the first `fuzz_greeks` runs (2026-07-06)

Seeds preserved under `seeds/fuzz_greeks/` (copied into the live corpus by the
CI smoke job — regression inputs, not crashes today):

* `seed-negative-rate-delta-gt-1` — `GreeksOut::delta`'s doc comment claims
  `CE ∈ [0, 1]`, but Black-76 delta is `e^{−rT}·N(d1)`: with the permitted
  negative rate (`|rate| ≤ 1.0`) the fuzzer produced CE delta
  `1.0000095…` — a DOC overclaim (the math is right), reconcile upstream.
* `seed-deep-otm-negative-price` — a deep-OTM known-sigma tuple rounded the
  model price to a TINY negative (`−1.2e-321`); the kernel does not clamp
  float-cancellation negatives to 0. Cosmetic, reported upstream.

## Honest domain (Golden Rule #10 — matches `src/lib.rs`)

`trading-core` **intentionally panics** on (a) corrupt prev-day OHLC
(`pdh < pdl`, or `pdc ∉ [pdl, pdh]`) via an always-on `assert!` mirroring
Python's `ValueError`, and (b) absurd `i64`-scale magnitudes (`> ~10¹⁷` paisa)
via the release `overflow-checks` — both are documented CONTRACT behaviour, not
bugs (the crate's own unit tests cover them). So the paisa fuzzers map
arbitrary bytes into the **realistic envelope** the crate is specified for:
prices in `[1, ₹100,000,000]` (paisa), OHLC ordered, `PDH≥PDL`,
`PDC∈[PDL,PDH]`, proximity bps `∈[0,10000]`. At that cap every internal
product stays far below `i64::MAX`, so **panic/overflow-freedom is the real
invariant under test**. `fuzz_spec` (arbitrary JSON/hex bytes) and
`fuzz_greeks` phase 1 (raw f64 bit patterns) are the two genuinely
UNTRUSTED-input surfaces and take fully arbitrary input.

**Python⇆Rust bit-identity** is NOT re-asserted inside libFuzzer (it would need
an embedded interpreter); it is covered by the `tests/golden_vectors.rs`
400-case answer-key. These targets add panic-freedom + structural invariants
over the full in-domain input space.

This fuzz crate builds the parent with `default-features = false` plus the
pure-Rust `serde` feature for `fuzz_spec` (still **no libpython**, unlike
`fno-math/fuzz`), and is a separate, non-published, NON-workspace package, so
it never perturbs the parent crate's `cargo build` / `cargo test` / coverage
gates.

## Reproduce (nightly Rust + cargo-fuzz)

```bash
rustup toolchain install nightly
cargo install cargo-fuzz
cd rust/trading-core
cargo +nightly fuzz build
for t in $(cargo +nightly fuzz list); do
  cargo +nightly fuzz run "$t" -- -max_total_time=60
done
```

CI: `.github/workflows/rust-fuzz-smoke.yml` builds every target in BOTH fuzz
crates on every PR touching `rust/**` and runs each for a bounded smoke
(crash/panic = red; it is a smoke tier, not a coverage campaign).

Last local run (2026-07-06, nightly 1.99 + cargo-fuzz 0.13.2, ASan): all six
targets, 15 s each — `fuzz_evaluator` 2,398,442 · `fuzz_greeks` 2,355,099 ·
`fuzz_market_structure` 913,766 · `fuzz_metadata` 7,361,241 · `fuzz_pivot_fib`
10,090,210 · `fuzz_spec` 567,436 execs — **0 crashes** (after the two
`fuzz_greeks` findings above were triaged to doc/cosmetic and re-asserted
against the true mathematical envelope).
