# trading-core

The shared **indicator + 52-condition vocabulary kernel** — the single source of
truth for the market-reading rules used by **BruteX** (offline backtest, Python
via PyO3) and **TickVault** (live tick engine, native Rust). One compiled core
makes the backtested signal equal the live signal **by construction**.

Phase 1 of `DEC-SHARED-CORE-001` (`docs/MIGRATION.md` #441).

## What's here

- `src/bits.rs` — the 52 bit-position constants.
- `src/indicators/{pivots,fib,ema}.rs` — Standard Pivot Points + Static Daily Fib
  + the EMA 20/200 recurrence (`compute_ema` / `EmaState`; ported in
  `DEC-EMA-RUST-PORT-001`). EMA still feeds the evaluator as a precomputed
  `ema20`/`ema200` paisa input (the caller snaps the `f64` rupees to paisa), but
  the RECURRENCE now lives here — the all-Rust shared indicator core.
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
- The **EMA** recurrence runs entirely in `f64` (no rounding) and the result
  stays `f64`; the three arithmetic sites (`2.0/(N+1)`, `seed_sum/N`,
  `α·close+(1−α)·prior`) are mirrored token-for-token so the IEEE-754 doubles are
  byte-identical. The caller (`_ema_to_paisa`) snaps to paisa separately.
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
- ✅ **DONE (Phase 2c):** `compute_pivot_levels` / `compute_fib_levels` exposed to
  Python + a parity test pinning them bit-for-bit on every build.
- ✅ **DONE (Phase 2d):** BruteX's `brutex/indicators/compute_pivot_levels` /
  `compute_fib_levels` now **delegate** to this crate — the Python copy of the
  pivot/Fib arithmetic was REMOVED, so the math is **single-source (Rust)**, not
  "two copies kept identical". The wrappers keep only the input validation + the
  dataclass packing (verified bit-exact against the TradingView oracle tests).
- ✅ **DONE (DEC-EMA-RUST-PORT-001):** the **EMA 20/200 recurrence** is now in this
  crate (`compute_ema` / `EmaState`; `ema_alpha` / `ema_seed` / `ema_step` exposed
  to Python). BruteX's `brutex/indicators/ema.py` **delegates** every arithmetic
  site to Rust (the streaming `EMAState` keeps only its branch/state shell +
  checkpoint serialization), proven bit-for-bit by the `tests/ema_golden.rs`
  cross-language proof (the SAME `indicators_golden.json` the Python golden reads)
  + the Python parity test. EMA still feeds the evaluator as a precomputed paisa
  input — the RECURRENCE is what moved.
- 🚫 **NOT done:** **day-extremes / prev-day-OHLC are NOT in this crate** — they
  stay in Python and feed the evaluator as precomputed inputs. **TickVault
  adoption (Phase 3) is 0%** — no TickVault code references this crate yet (it
  needs the TickVault repo). The **strategy / brute-force sweep / SL-TP / fills**
  are BruteX-only and NOT shared. So "common" today = the 52→60-condition
  evaluator + pivots + Fib + market structure + EMA, nothing more.
