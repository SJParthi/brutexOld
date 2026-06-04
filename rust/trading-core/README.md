# trading-core

The shared **indicator + 52-condition vocabulary kernel** — the single source of
truth for the market-reading rules used by **BruteX** (offline backtest, Python
via PyO3) and **TickVault** (live tick engine, native Rust). One compiled core
makes the backtested signal equal the live signal **by construction**.

Phase 1 of `DEC-SHARED-CORE-001` (`docs/MIGRATION.md` #441).

## What's here

- `src/bits.rs` — the 52 bit-position constants.
- `src/indicators/{pivots,fib}.rs` — Standard Pivot Points + Static Daily Fib
  (the two indicators the evaluator recomputes at evaluate-time; EMA is consumed
  as a precomputed input, so it is not ported here).
- `src/evaluator.rs` — `evaluate_atoms(snapshot, proximity, prior) -> u64`, a
  bit-for-bit port of `brutex/vocabulary/evaluator.py`.
- `tests/golden_vectors.rs` — the **cross-language bit-identity proof**: reads the
  same language-neutral oracle the Python test reads
  (`tests/golden/fixtures/vocab_golden_vectors.json`, 400 cases) and asserts this
  crate reproduces every frozen `u64`.

## Bit-identity notes (the float traps)

- Python `round()` / `_round_half_even` is **round-half-to-even** (banker's). Rust
  `f64::round()` rounds half *away* from zero — wrong. We use
  `f64::round_ties_even()` (`round_half_even`).
- The Fib levels multiply the integer range by IEEE-754 double literals
  (`0.236`, `0.272`, …); the Rust expressions mirror the Python **operation
  order** token-for-token so the rounding is identical.
- Python `//` floor division (`_is_near` tolerance, the gap threshold) → Rust
  `i64::div_euclid`.
- Group-8 time-of-day is the **IST wall-clock** HH:MM:SS (the UTC offset is
  ignored), stored as `tod_seconds`.

## Build & test

```sh
cargo test            # pure-Rust core + the 400-case golden bit-identity proof
cargo test --release  # same, with overflow-checks on (finance: no silent overflow)
cargo check --features python   # the PyO3 (Phase-2) binding compiles
```

The pure-Rust core links **no** Python (the PyO3 surface is behind the optional
`python` feature), so TickVault can depend on it natively with a zero-Python tree.

## Status (honest envelope)

- ✅ **DONE (Phase 1 / 2a / 2b, merged):** the verified-identical Rust crate + its
  golden test; the PyO3 wheel; **BruteX's vocabulary delegates to this crate at
  runtime** (`brutex/vocabulary/evaluator.py` is a thin shim over
  `evaluate_with_levels`), CI-gated across every job that imports
  `brutex.vocabulary`; the `CLAUDE.md` self-containment reversal landed.
- ✅ **DONE (Phase 2c):** `compute_pivot_levels` / `compute_fib_levels` are exposed
  to Python and a parity test pins Python==Rust bit-for-bit on every build — the
  pivot/fib math can no longer drift between the two copies.
- 🚫 **NOT done:** **EMA / day-extremes / prev-day-OHLC are NOT in this crate** —
  they stay in Python and feed the evaluator as precomputed inputs (EMA is a
  stateful recurrence). **TickVault adoption (Phase 3) is 0%** — no TickVault code
  references this crate yet (it needs the TickVault repo). The **strategy / brute-
  force sweep / SL-TP / fills** are BruteX-only and NOT shared. So "common" today =
  the 52-condition evaluator + pivots + Fib, nothing more.
