//! `trading-core` — the shared indicator + condition vocabulary kernel.
//!
//! `DEC-SHARED-CORE-001` (`docs/MIGRATION.md` #441): a bit-for-bit Rust port of
//! BruteX's Python vocabulary, the single source of truth that **BruteX consumes
//! today** (via PyO3, live since Phase 2b — `brutex/vocabulary/evaluator.py`
//! delegates to it) and that **TickVault will share natively (Phase 3, not yet
//! done)**, so the backtested signal equals the live signal *by construction*.
//!
//! The vocabulary mask is a `u128` (DEC-VOCAB-U128-CODEGEN-001 / ADR-0012 D4 —
//! the DEC-PLUGIN-001-gated u64→u128 widening for headroom). The bit LAYOUT
//! ([`bits`]) is GENERATED from `conditions.toml` by `scripts/codegen_vocab.py`
//! (the same manifest emits the Python twin). Bits 52..=55 (VWAP/R5-S5) became
//! `implemented=true` in PR-2; bits 56..=59 (BOS/CHoCH) became `implemented=true`
//! in DEC-NIFTY-BOS-CHOCH-001 (the stateful look-ahead-clean
//! [`MarketStructureState`] resolves the four booleans the evaluator reads).
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
pub use indicators::market_structure::{MarketStructureSignal, MarketStructureState, Trend};
pub use indicators::metadata::{
    BarMetadata, derive_bar_metadata, minute_of_session, resolve_near_month, weekday_from_ordinal,
};
pub use indicators::pivots::{PivotLevels, compute_pivot_levels};
pub use indicators::vwap::VwapState;

#[cfg(feature = "python")]
mod python;
