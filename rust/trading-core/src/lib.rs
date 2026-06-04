//! `trading-core` — the shared indicator + 52-condition vocabulary kernel.
//!
//! `DEC-SHARED-CORE-001` (`docs/MIGRATION.md` #441): a bit-for-bit Rust port of
//! BruteX's Python vocabulary, the single source of truth that **BruteX consumes
//! today** (via PyO3, live since Phase 2b — `brutex/vocabulary/evaluator.py`
//! delegates to it) and that **TickVault will share natively (Phase 3, not yet
//! done)**, so the backtested signal equals the live signal *by construction*.
//!
//! The pure-Rust core ([`evaluate_atoms`] + the [`indicators`]) is dependency-free
//! and is proven identical to the Python source against the language-neutral
//! golden answer-key (repo-root `tests/golden/fixtures/vocab_golden_vectors.json`,
//! 400 cases — the Rust test reaches it via `../../tests/...`) by the
//! `tests/golden_vectors.rs` integration test. The PyO3 surface (behind the
//! optional `python` feature) is the live BruteX binding.
//!
//! ## Domain (honest envelope)
//!
//! Money is `i64` paisa over the realistic Indian index/stock range (≈10⁶–10⁹).
//! Two intentional, documented boundary behaviours differ from Python's
//! unbounded ints — both unreachable for real prices:
//! * **Corrupt prev-day OHLC** (`pdh < pdl`, or `pdc ∉ [pdl, pdh]`) **panics**
//!   (an always-on `assert!`), mirroring Python's `ValueError` — never a silent
//!   wrong result.
//! * **Absurd i64-scale magnitudes** (`> ~10¹⁷` paisa) **panic** via release
//!   `overflow-checks` rather than silently wrapping — a finance-safety choice;
//!   Python would compute the huge value. The 400 golden cases (and every real
//!   market price) sit far inside this bound.

pub mod bits;
pub mod evaluator;
pub mod indicators;

pub use evaluator::{IndicatorSnapshot, PriorBarHistory, ProximityParams, evaluate_atoms};
pub use indicators::fib::{FibLevels, compute_fib_levels};
pub use indicators::pivots::{PivotLevels, compute_pivot_levels};

#[cfg(feature = "python")]
mod python;
