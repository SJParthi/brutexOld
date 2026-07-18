//! `brutex-pull-state` — the dashboard pull-state DECISION kernel.
//!
//! `DEC-RUST-PULL-STATE-001` (lane Phase 1): the decision plane of the
//! pull-state / rate-view modules, starting with the vendor-account SAFETY
//! guard `brutex/calendars/market_hours.py::pull_allowed_now`
//! (DEC-GROWW-MARKET-HOURS-GUARD-001, MIGRATION.md #1692 — no Groww vendor
//! pulls 08:45-15:35 IST on a trading day; the shared-TOTP protection),
//! transcribed to a pure clockless predicate ([`pull_allowed_us`]) over
//! INTEGERS ONLY — the caller's microsecond-of-day + the caller-resolved
//! trading-day flag. Python (`brutex/pull_state_rust.py` +
//! `brutex/calendars/market_hours.py`) resolves the clock and the NSE
//! calendar itself (`brutex/calendars/nse.py` stays the single source of
//! truth — the holiday sets are NEVER re-minted in Rust), so guard verdicts
//! are byte-preserved by construction against the frozen golden
//! (`tests/golden/fixtures/market_hours_guard_verdicts.json`).
//!
//! Shipped default-OFF behind `BRUTEX_PULL_STATE_RUST` (Pattern A opt-in);
//! the retained Python predicate is the parity oracle. The default flip +
//! oracle deletion are LATER lane phases (burn-in + autorevert per the
//! DEC-SWEEP-RUST-METRICS-DEFAULT-ON-001 precedent), never this one.
//!
//! ## Honest envelope (Golden Rule #10)
//!
//! The predicate is O(1) and already nanosecond-scale in Python — this port
//! is for the operator-ordered Rust uniformity program, NOT speed (the
//! ADR-0020 honesty-banner posture: expected payoff ≈ 0); the FFI hop is a
//! small SLOWDOWN, acceptable because the seam runs a handful of times per
//! pull start / per mid-run window check, never on a hot path.

pub mod market_hours;
// Wave 1A (DEC-RUST-PULL-STATE-001): the last-pull OUTCOME decision core
// (DEC-OPERATOR-HEALTH-TRUTH-001 re-hosted) — codes only; every human string
// + the banner colour authority stay Python.
pub mod pull_outcome;

pub use market_hours::{BLOCK_END_US, BLOCK_START_US, DAY_US, pull_allowed_us};
pub use pull_outcome::{classify_pull_outcome, verdict_correlates};

/// BLAKE3 hex digest of this crate's source (`Cargo.toml` + `src/**/*.rs`),
/// baked at build time by `build.rs` under the shared FRAMING v1 of
/// `rust/source_digest_core.rs` (DEC-RUST-SOURCE-DIGEST-TWIN-001,
/// `docs/MIGRATION.md` #1258). The PyO3 module exports it as
/// `SOURCE_DIGEST` / `__source_digest__`.
pub const SOURCE_DIGEST: &str = env!("BRUTEX_SOURCE_DIGEST");

#[cfg(feature = "python")]
mod python;
