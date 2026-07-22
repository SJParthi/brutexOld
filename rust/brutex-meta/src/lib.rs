//! `brutex-meta` — the decision-precision layer's O(1)/trade inference
//! kernels (BORN-RUST, operator law 2026-07-18).
//!
//! * [`score::score_logistic`] — the meta-label P(win) batch scorer
//!   (`DEC-META-LABEL-ANNOTATE-001`): fixed-vector logistic inference,
//!   O(1)/trade for the documented feature vector. Training stays offline
//!   Python (the sanctioned offline-statistics allowance).
//! * [`abstain::abstain_masks`] — the abstention-first gate stream's
//!   per-element reason bitmasks (`DEC-ABSTAIN-GATE-STREAM-001`),
//!   ANNOTATION-ONLY today: the masks land in `.meta.json` sidecars, never
//!   in any simulation path (the in-sim arm is designed + dormant behind a
//!   refused env knob — `docs/design/meta/ABSTAIN_INSIM_DESIGN.md`).
//!
//! Shipped default-OFF behind `BRUTEX_META_GATE` (Pattern A opt-in) with the
//! retained Python oracles (`brutex/meta/logistic.py::score_logistic`,
//! `brutex/meta/abstain.py::abstain_masks_python`) as the burn-in parity
//! arms (`brutex/meta/meta_rust.py`).
//!
//! ## Honest envelope (Golden Rule #10)
//!
//! Both kernels are O(1) per ELEMENT (fixed feature width / a handful of
//! comparisons); a batch call is O(n_elements) total by construction —
//! never claimed O(1) total. Nothing here enters run identity: every output
//! is annotation-sidecar material.

pub mod abstain;
pub mod score;

pub use abstain::{
    BIT_EVENT_DAY, BIT_LOW_CONFIDENCE, BIT_REGIME_MISMATCH, BIT_VIX_MISSING, BIT_VIX_OUT_OF_BAND,
    abstain_masks,
};
pub use score::{score_logistic, sigmoid};

/// BLAKE3 hex digest of this crate's source (`Cargo.toml` + `src/**/*.rs`),
/// baked at build time by `build.rs` under the shared FRAMING v1 of
/// `rust/source_digest_core.rs` (DEC-RUST-SOURCE-DIGEST-TWIN-001,
/// `docs/MIGRATION.md` #1258). The PyO3 module exports it as
/// `SOURCE_DIGEST` / `__source_digest__`.
pub const SOURCE_DIGEST: &str = env!("BRUTEX_SOURCE_DIGEST");

#[cfg(feature = "python")]
mod python;
