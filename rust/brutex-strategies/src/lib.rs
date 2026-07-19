//! `brutex-strategies` — combo.py's two pure byte laws (default-OFF kernels).
//!
//! `DEC-STRATEGIES-RUST-COMBO-001` (T16 PR-1): re-hosts EXACTLY two pure laws
//! of `brutex/strategies/combo.py` —
//!
//! * [`scan_sep::first_unit_separator`] — the `\x1f` first-offender scan of
//!   `reject_unit_separator`. The FFI returns `Option<usize>` (the index of
//!   the first offending VALUE); Python owns the field-name lookup, the
//!   error-message bytes (the literal-`\x1f` f-string) and the injected
//!   exception class, so the exception taxonomy is unchanged by construction.
//! * [`canon`] — the canonical-JSON RENDER of `canonicalise_params`
//!   (`json.dumps(lowered, sort_keys=True, separators=(",", ":"),
//!   ensure_ascii=True)`) over the ALREADY-lowered tree.
//!
//! Canonicalisation (`_canonicalise_value`) stays Python: the crossing tree
//! is dict / list / str / int / bool / None — float-free and Decimal-free BY
//! LAW (floats and Decimals arrive as already-`repr()`/`str()`-rendered
//! strings), so this crate NEVER reproduces `float.__repr__` /
//! `Decimal.__str__` — the LOAD-BEARING byte-safety property of the design
//! (#1914 "Python pre-formats; ints+strings-only FFI" applied to a tree). Any
//! sibling-site port that reintroduces BARE floats into a crossing tree owns
//! those repr-twin laws in ITS OWN PR — never folded into this crate
//! silently (a standing crate invariant).
//!
//! Consumed by `brutex/strategies/combo_rust.py` behind the default-OFF
//! `BRUTEX_STRATEGIES_RUST_COMBO` flag; any degrade condition (wheel
//! missing/stale, extraction failure, out-of-i128 int, lone surrogate,
//! non-exact-str dict key, kernel error/panic) falls back to the
//! byte-identical retained Python oracle.
//!
//! ## Honest envelope (Golden Rule #10 / ADR-0020 §0 banner)
//!
//! Expected payoff ≈ 0 — conversion is for uniformity, not speed. The
//! engaged path ADDS an FFI hop (plus a full tree extraction) per canonical
//! render and per `\x1f` scan — a small SLOWDOWN, not a win. Both laws run
//! per combo MATERIALISATION (launch / persist / verify / re-derivation),
//! never inside the ~33B-trial mask/bar hot loop.

pub mod canon;
pub mod scan_sep;

pub use canon::{Node, render};
pub use scan_sep::first_unit_separator;

/// BLAKE3 hex digest of this crate's source (`Cargo.toml` + `src/**/*.rs`),
/// baked at build time by `build.rs` under the shared FRAMING v1 of
/// `rust/source_digest_core.rs` (DEC-RUST-SOURCE-DIGEST-TWIN-001,
/// `docs/MIGRATION.md` #1258). The PyO3 module exports it as
/// `SOURCE_DIGEST` / `__source_digest__` (the brutex-feed convention).
pub const SOURCE_DIGEST: &str = env!("BRUTEX_SOURCE_DIGEST");

#[cfg(feature = "python")]
mod python;
