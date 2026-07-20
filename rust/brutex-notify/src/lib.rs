//! `brutex-notify` — the notification PURE-law kernel.
//!
//! `DEC-NOTIFY-RUST-CORE-001`: the four pure notification laws of
//! `brutex/notifications/` — the MarkdownV2 escape law, the
//! `_count_or_question` honest-unknown law, the dedupe-window drop
//! predicate (over IEEE-754 bit patterns as `u64` — no float object ever
//! crosses the FFI) and the 12-code-point id truncation — transcribed
//! token-for-token from their Python oracles ([`laws`]). Python keeps
//! EVERYTHING else: the ~30 f-string message builders (fixed-spec float
//! format sites — the named follow-up `DEC-NOTIFY-RUST-BUILDERS-002`), the
//! `TelegramNotifier` HTTP transport (network I/O, honestly disclosed as
//! such), threads/queues/atexit lifecycle, and the wiring singleton.
//!
//! Shipped default-OFF behind `BRUTEX_NOTIFY_RUST` (Pattern A opt-in); the
//! retained Python law bodies are the LIVE parity oracle with a per-process
//! 16-call burn-in bit-compare + ship-the-oracle autorevert in the facade
//! (`brutex/notifications/notify_rust.py`).
//!
//! ## Honest envelope (Golden Rule #10)
//!
//! Expected payoff ≈ 0 — conversion is for uniformity, not speed (the
//! ADR-0020 banner): the escape is O(len(text)) either way, the FFI hop is
//! a small SLOWDOWN, and the Telegram send the laws feed is network-bound
//! I/O that stays Python. No secret ever crosses the FFI — the Rust surface
//! is message TEXT + integer bit patterns only, never config/token/chat-id.

pub mod laws;

pub use laws::{
    ID_TRUNCATE_CODE_POINTS, MDV2_SPECIALS, count_or_question, dedupe_should_drop,
    escape_markdown_v2, truncate_id_12,
};

/// BLAKE3 hex digest of this crate's source (`Cargo.toml` + `src/**/*.rs`),
/// baked at build time by `build.rs` under the shared FRAMING v1 of
/// `rust/source_digest_core.rs` (DEC-RUST-SOURCE-DIGEST-TWIN-001,
/// `docs/MIGRATION.md` #1258). The PyO3 module exports it as
/// `SOURCE_DIGEST` / `__source_digest__` (the brutex-feed shape).
pub const SOURCE_DIGEST: &str = env!("BRUTEX_SOURCE_DIGEST");

#[cfg(feature = "python")]
mod python;
