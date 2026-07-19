//! `brutex-calendar` — the calendar-leaf DECISION laws.
//!
//! `DEC-RUST-CALENDAR-LEAVES-001` (lane T18): a byte-parity port of the pure
//! decision plane of two calendar leaves —
//!
//! * `brutex/calendars/observed_sessions.py`: the pure
//!   [`drift::classify_month_drift`] admission/veto/cap core and the
//!   overlay-sidecar whole-accept/whole-reject validation
//!   ([`drift::parse_observed_sidecar`] — including the Python `==`
//!   int/bool/float VERSION PUNNING and the canonical-ISO-date round-trip
//!   entry law);
//! * `brutex/calendars/settle_evidence.py`: the full
//!   [`settle::validate_evidence_payload`] truth table — every refusal
//!   message BYTE-IDENTICAL to the Python `ValueError` text, riding exact
//!   twins of CPython's `repr()` ([`pyrepr`]), `str(timedelta)`, and the
//!   C-accelerated `datetime.fromisoformat` ([`pyiso`]).
//!
//! Everything impure stays Python: file/dir I/O, `os.stat` memo tokens,
//! structlog events, env resolution, the sidecar merge/diff + serializer,
//! and the explicit-vs-default failure tiers. Only ints/strings/bools/bytes
//! cross the FFI ([`python`], feature-gated), and where the Rust JSON engine
//! could disagree with Python's `json.loads` (NaN/Infinity literals, lone
//! surrogates, deep nesting — measured: the abstain fires from PAYLOAD
//! depth 127, i.e. total JSON depth 128 = serde_json's recursion limit;
//! depths ≤126 validate normally) the binding ABSTAINS so the caller
//! degrades to the retained Python oracle — never a silent divergence.
//!
//! ## Honest envelope (Golden Rule #10)
//!
//! A uniformity conversion (the ADR-0020 banner): the Python laws are
//! microsecond-scale, no speedup is claimed, and the port ships consumed by
//! nothing until the Python-side flip lands behind its own default-OFF seam.

pub mod drift;
mod py_printable_15_0;
pub mod pyiso;
pub mod pyrepr;
pub mod settle;

pub use drift::{ClassifyOut, classify_month_drift, parse_observed_sidecar};
pub use settle::{SettleValid, validate_evidence_payload};

/// BLAKE3 hex digest of this crate's source (`Cargo.toml` + `src/**/*.rs`),
/// baked at build time by `build.rs` under the shared FRAMING v1 of
/// `rust/source_digest_core.rs` (DEC-RUST-SOURCE-DIGEST-TWIN-001,
/// `docs/MIGRATION.md` #1258). The PyO3 module exports it as
/// `SOURCE_DIGEST` / `__source_digest__`.
pub const SOURCE_DIGEST: &str = env!("BRUTEX_SOURCE_DIGEST");

#[cfg(feature = "python")]
mod python;
