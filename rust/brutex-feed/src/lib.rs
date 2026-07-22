//! `brutex-feed` — the engine read-boundary DECISION kernel.
//!
//! `DEC-FEED-RUST-CORE-001`: the decision plane of
//! `brutex/feed/replay.py::arrow_table_to_bars` + the
//! `brutex/data/bad_tick.py::BadTickScreen` state machine, transcribed to a
//! single pure forward scan ([`scan_bars`]) that returns INTEGERS ONLY —
//! kept row indices, owed-WARN coordinates, and the positivity-stop
//! coordinate. Python (`brutex/feed_rust.py` + `brutex/feed/replay.py`)
//! selects rows and builds every `Decimal` / `CandleBar` / error / log
//! itself with the verbatim existing expressions, so run identity
//! (`bxsnap2`, CLAUDE.md Golden Rule #4) is byte-preserved by construction.
//!
//! Shipped default-OFF behind `BRUTEX_FEED_RUST` (Pattern A opt-in); the
//! retained Python loop is the parity oracle. The one-constant default flip
//! is the LATER `DEC-FEED-RUST-FLIP-001` (burn-in + autorevert per the
//! DEC-SWEEP-RUST-METRICS-DEFAULT-ON-001 precedent).
//!
//! ## Honest envelope (Golden Rule #10)
//!
//! The scan is O(n_bars) per LAUNCH — a per-call latency win on the feed
//! read boundary, NOT an O(1) claim and NOT on the ~33B-trial mask path.
//! Memory: the PyO3 edge packs five owned `Vec<f64>`/`Vec<i64>` column
//! copies (plus the optional `ts_us` list the caller builds when the screen
//! is on) — a transient O(n_bars) cost, dominated by the pre-existing
//! `to_pylist()` materialization the read path already pays in BOTH arms.
//!
//! ## `DEC-FEED-RUST-MATERIALIZE-002` — the wheel-REQUIRED extension
//!
//! The T17 lane retires the two-arm seam: the Python oracle bodies of
//! `arrow_table_to_bars` / `LookAheadBarrier` / the `series_load` decision
//! helpers are DELETED and this crate becomes REQUIRED for feed
//! materialization. Append-only additions ([`materialize::scan_bars_strict`]
//! — the finiteness-first strict scan; [`lookahead::BarrierState`] — the
//! P-06 barrier state machine; [`series`] — the sweep-slice decision
//! arithmetic). The original [`scan_bars`] ships FROZEN (its own tests +
//! the shim handshake keep it; nothing in production consults it any more).

pub mod lookahead;
pub mod materialize;
pub mod scan;
pub mod series;

pub use lookahead::{
    ACCESS_LOOKAHEAD, ACCESS_NEGATIVE, ACCESS_NO_CURSOR, ACCESS_OK, ADVANCE_BACKWARDS,
    ADVANCE_NEGATIVE, ADVANCE_OK, BarrierState,
};
pub use materialize::{STOP_NONFINITE, STOP_NONPOSITIVE, StrictOutcome, scan_bars_strict};
pub use scan::{DAY_US, IST_OFFSET_US, MAX_SAFE_TS_US, ScanOutcome, WarnEvent, scan_bars};
pub use series::{SessionDay, boundary_monotonic_break, expected_bar_instant, ownership_cut};

/// BLAKE3 hex digest of this crate's source (`Cargo.toml` + `src/**/*.rs`),
/// baked at build time by `build.rs` under the shared FRAMING v1 of
/// `rust/source_digest_core.rs` (DEC-RUST-SOURCE-DIGEST-TWIN-001,
/// `docs/MIGRATION.md` #1258). The PyO3 module exports it as
/// `SOURCE_DIGEST` / `__source_digest__`; the `brutex doctor` freshness
/// stage for this wheel is the named follow-up `DEC-FEED-RUST-DOCTOR-STAGE-001`.
pub const SOURCE_DIGEST: &str = env!("BRUTEX_SOURCE_DIGEST");

#[cfg(feature = "python")]
mod python;
