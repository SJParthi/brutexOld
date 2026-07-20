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
//! (the same manifest emits the Python twin). Bits 54..=55 (R5/S5) became
//! `implemented=true` in PR-2; bits 56..=59 (BOS/CHoCH) became `implemented=true`
//! in DEC-NIFTY-BOS-CHOCH-001 (the stateful look-ahead-clean
//! [`MarketStructureState`] resolves the four booleans the evaluator reads);
//! bits 60..=63 (Central Pivot Range) are implemented off the same prev-day
//! pivot anchor; bits 64..=65 (Supertrend) are implemented off the stateful
//! ×2-scaled-integer [`SupertrendState`] line (ATR-10 Wilder, factor 3, hl2).
//! Bits 52..=53 (VWAP) are TOMBSTONED (`implemented=false`,
//! positions held forever — the VWAP/volume wipe).
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
pub mod greeks;
pub mod indicators;
pub mod spec;
pub mod stage1a_batch;

pub use evaluator::{IndicatorSnapshot, PriorBarHistory, ProximityParams, evaluate_atoms};
pub use greeks::{GreeksOut, black76_price, greeks};
pub use indicators::ema::{EmaState, compute_ema, ema_alpha, ema_seed, ema_step};
pub use indicators::fib::{FibLevels, compute_fib_levels};
pub use indicators::gap_mid::{GapDirection, compute_gap_mid};
pub use indicators::market_structure::{MarketStructureSignal, MarketStructureState, Trend};
pub use indicators::metadata::{
    BarMetadata, derive_bar_metadata, minute_of_session, resolve_near_month, seconds_of_day,
    weekday_from_ordinal,
};
pub use indicators::pivots::{
    CprLevels, NARROW_CPR_WIDTH_BPS, PivotLevels, compute_cpr_levels, compute_pivot_levels,
};
pub use indicators::supertrend::{
    SUPERTREND_ATR_PERIOD, SUPERTREND_DIR_DOWN, SUPERTREND_DIR_UP, SUPERTREND_FACTOR,
    SupertrendState, compute_supertrend,
};
pub use spec::{Direction, SPEC_SCHEMA_VERSION, Signal, StrategySpec, evaluate};

/// BLAKE3 hex digest of this crate's source (`Cargo.toml` + `src/**/*.rs`),
/// baked at build time by `build.rs` under the shared FRAMING v1 of
/// `rust/source_digest_core.rs` (DEC-RUST-SOURCE-DIGEST-TWIN-001,
/// `docs/MIGRATION.md` #1258). The PyO3 module exports it as
/// `__source_digest__`; `brutex doctor`'s `wheel_source_digest` stage compares
/// it against the Python twin's (`brutex/wheel_digest.py`) fresh recompute
/// over the repo checkout, catching a LOCAL wheel built from older source at
/// the same crate version (same-version behavioural drift).
pub const SOURCE_DIGEST: &str = env!("BRUTEX_SOURCE_DIGEST");

#[cfg(feature = "python")]
mod python;
