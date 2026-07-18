//! `brutex-observability` — the observability DECISION-law kernel.
//!
//! `DEC-OBS-RUST-DETECTOR-CORE-001`: the four PURE decision laws of
//! `brutex/observability/` — the loop-watchdog tiered stall state machine
//! (law A), the per-worker HEALTHY↔HUNG decision (law B), the SLA
//! predicate + hysteresis edge (law C) and the stage-stamp shape-order /
//! verdict-matrix laws (laws D1/D2) — transcribed bit-for-bit from the
//! Python oracles. Python (`brutex/observability/observability_rust.py` +
//! the four consumer modules) keeps ALL materialization: dataclass
//! construction, enum reconstruction, error f-strings, structlog events,
//! callbacks, registries, clocks — so nothing that reaches logged/raised
//! output is ever re-implemented in Rust (the DEC-FEED-RUST-CORE-001
//! decide-in-Rust / materialize-in-Python shape).
//!
//! Shipped default-OFF behind `BRUTEX_OBSERVABILITY_RUST`; the retained
//! Python laws are the LIVE parity oracles (per-law 16-sample burn-in +
//! ship-the-oracle autorevert, the #1531/#1532 pattern, facade-side).
//!
//! ## Honest envelope (Golden Rule #10; the ADR-0020 banner, verbatim)
//!
//! Expected payoff ≈ 0 — the conversion is for uniformity, not speed. Every
//! routed law is nanosecond-scale in Python; the FFI hop is a small
//! SLOWDOWN, acceptable because the seams run per monitor tick / per
//! validate call (never a hot mask path) and ship DEFAULT-OFF.

pub mod detector;
pub mod stamp;

pub use detector::{
    LoopVerdict, SLA_EPS, TIER_CRITICAL, TIER_OK, TIER_WARNED, loop_watchdog_check_once,
    predicate_holds, sla_decide, watchdog_decide,
};
pub use stamp::{VERDICT_HASH_MISMATCH, VERDICT_OK, stamp_shape_index, stamp_verdict};

/// BLAKE3 hex digest of this crate's source (`Cargo.toml` + `src/**/*.rs`),
/// baked at build time by `build.rs` under the shared FRAMING v1 of
/// `rust/source_digest_core.rs` (DEC-RUST-SOURCE-DIGEST-TWIN-001,
/// `docs/MIGRATION.md` #1258). The PyO3 module exports it as
/// `SOURCE_DIGEST` / `__source_digest__`.
pub const SOURCE_DIGEST: &str = env!("BRUTEX_SOURCE_DIGEST");

#[cfg(feature = "python")]
mod python;
