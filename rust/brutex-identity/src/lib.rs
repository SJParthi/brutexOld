//! `brutex-identity` — run-identity + ledger-store kernels.
//!
//! `DEC-RUST-RUNID-STORES-001` (the L14 conversion lane): the computation
//! plane of BruteX's four run-identity/store surfaces, hosted in ONE light
//! crate (blake3 + parquet — no polars, no arrow, no chrono):
//!
//! * [`pyfloat`] — CPython-compatible `repr(float)` (shortest round-trip);
//! * [`pyjson`] — the CPython `json.dumps` ensure-ascii byte-law escaper;
//! * [`snapshot`] — the streamed `bxsnap2` data-snapshot digest framing +
//!   ISO-8601 timestamp rendering;
//! * [`store_law`] — the results-store laws (run-id validation, the u128
//!   mask↔limb codec, the append/VIX-merge decision, top-k ordering);
//! * [`ledger`] — the window-ledger merge-on-save law + legacy-column
//!   derivations + row coercion;
//! * [`parquet_codec`] — the canonical window-ledger Parquet encoder +
//!   decoder (the #1917 G10 resolution by golden re-baseline, option (iii)).
//!
//! The param canonicaliser (which must walk live Python objects) and the
//! PyO3 surface live behind the optional `python` feature; `cargo test`
//! (default features) links zero Python symbols — the brutex-feed shape.
//!
//! ## Honest envelope (Golden Rule #10)
//!
//! ADR-0020's banner applies: expected payoff ≈ 0 — this conversion is for
//! Rust uniformity, not speed. Every law here is either O(payload) once per
//! launch/append/save (hashing, codecs) or O(rows) once per store operation
//! (merge, top-k); nothing sits on the ~33B-trial mask hot path. Run-identity
//! VALUES are byte-preserved where parity is provable (combo/bxsnap2 — the
//! frozen goldens pass unmodified); the window-ledger Parquet BYTES are
//! re-baselined onto this crate's encoder (greenfield-sanctioned, the row
//! fixed point is the correctness law).

pub mod ledger;
pub mod parquet_codec;
pub mod pyfloat;
pub mod pyjson;
pub mod snapshot;
pub mod store_law;

/// BLAKE3 hex digest of this crate's source (`Cargo.toml` + `src/**/*.rs`),
/// baked at build time by `build.rs` under the shared FRAMING v1 of
/// `rust/source_digest_core.rs` (DEC-RUST-SOURCE-DIGEST-TWIN-001,
/// `docs/MIGRATION.md` #1258). The PyO3 module exports it as
/// `SOURCE_DIGEST` / `__source_digest__`.
pub const SOURCE_DIGEST: &str = env!("BRUTEX_SOURCE_DIGEST");

#[cfg(feature = "python")]
mod canonical;
#[cfg(feature = "python")]
mod python;
