//! `brutex-refine` — the Stage-3 refinement optimizer core.
//!
//! `DEC-RUST-STAGE3-REFINE-001`: the WHOLE Stage-3 optimizer math of the
//! retired Python path — the deterministic restarted CMA-ES over the
//! normalized (SL, TP) plane ([`refine::RefineDriver`]), the Mode-A grid
//! bijection ([`grid::GridAxis`]) and the bounded per-leaderboard top-K
//! survivor selection ([`select::SelectCore`]) — moved to pure Rust and
//! shipped DEFAULT-ON (the compressed-cycle lane: the pycma oracle is
//! DELETED, `BRUTEX_REFINE_RUST` falsy refuses loudly — there is no Python
//! fallback). Python (`brutex/engine/refine_rust.py` + the stage3_* thin
//! wrappers) keeps ALL bookkeeping: the value cache, the eval records, the
//! run identity, the audit lines, the `ResultRow` materialization.
//!
//! GREENFIELD ruling (no deployment, no stored backtests): NO pycma
//! bit-match is claimed. Correctness = this crate's convergence suite over
//! standard objectives + the determinism/bounds/budget/restart laws + the
//! frozen goldens (Rust-side literals + the Python-side JSON fixture).
//!
//! ## Honest envelope (Golden Rule #10)
//!
//! Each refinement evaluation is a FULL Python-side backtest — O(bars) per
//! eval; the optimizer bookkeeping here is O(λ·n²) per generation and the
//! selection is O(rows · log top_k). NOTHING here is one of the Rule-#5
//! O(1) hot paths and no throughput figure is claimed. Determinism is
//! same-platform: libm transcendentals (`ln`/`exp`/`sin`/`cos`) are
//! platform-rounded, so cross-platform bit-identity is NOT claimed — the
//! same envelope the retired pycma path carried (ADR-0007 condition 5).

pub mod cmaes;
pub mod grid;
pub mod linalg;
pub mod refine;
pub mod rng;
pub mod select;

pub use cmaes::{Cmaes, StopReason, default_lambda};
pub use grid::GridAxis;
pub use refine::RefineDriver;
pub use select::{PushOutcome, SelectCore};

/// BLAKE3 hex digest of this crate's source (`Cargo.toml` + `src/**/*.rs`),
/// baked at build time by `build.rs` under the shared FRAMING v1 of
/// `rust/source_digest_core.rs` (DEC-RUST-SOURCE-DIGEST-TWIN-001,
/// `docs/MIGRATION.md` #1258). The PyO3 module exports it as
/// `SOURCE_DIGEST` / `__source_digest__`; it is ALSO the second token of the
/// refinement-run identity (`optimizer_build`), so ANY crate-source change
/// re-keys every refinement run (Golden Rule #4).
pub const SOURCE_DIGEST: &str = env!("BRUTEX_SOURCE_DIGEST");

#[cfg(feature = "python")]
mod python;
