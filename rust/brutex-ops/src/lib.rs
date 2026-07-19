//! `brutex-ops` — the ops-lane Rust kernels (cross-verify first cut).
//!
//! DEC-CROSSVERIFY-RUST-CORE (first cut): pure string-law twins of the two
//! `brutex/crossverify/` pure-law modules — the pinned CSV byte assembly of
//! `csv_render.render_day_csv` and the canonical-identity mapping + S3-key
//! law of `canonical.py` — shipped default-OFF behind
//! `BRUTEX_CROSSVERIFY_RUST` (`brutex/crossverify/rust_seam.py`), with the
//! verbatim Python bodies retained as the LIVE parity oracles (burn-in
//! bit-compare + ship-the-oracle autorevert, the #1531/#1532 pattern).
//!
//! ## The ints+pre-formatted-strings-only FFI law
//!
//! Python pre-formats every price via the existing `round_money` Decimal
//! ROUND_HALF_UP path and every timestamp via its own `strftime` /
//! `isoformat` BEFORE the FFI; only ints, strings and bools cross. Nothing
//! that reaches hashed / logged / raised output is re-implemented here —
//! refusals cross as typed integer codes the Python seam maps onto the
//! EXISTING `ExportContractError` semantics.
//!
//! ## Lane structure (APPEND-ONLY)
//!
//! One module per ops lane. `crossverify` is the first; future lanes
//! (backup, nightly, …) add their OWN `src/<lane>.rs` + a delimited
//! registration block in `src/python.rs` — never editing an existing
//! lane's block.
//!
//! ## Honest envelope (Golden Rule #10)
//!
//! Uniformity conversion (the ADR-0020 banner): expected payoff ≈ 0 — the
//! Python side still runs the full Decimal/datetime validation + cell
//! pre-formatting, and the FFI hop is a small constant cost per file. No
//! speedup is claimed anywhere.

pub mod crossverify;

pub use crossverify::{
    CSV_CELLS_PER_ROW, CSV_REFUSAL_CONTROL_CHAR, CSV_REFUSAL_EMPTY_ROWS, CSV_REFUSAL_METACHAR,
    CSV_REFUSAL_NON_INCREASING_TS, CSV_REFUSAL_WRONG_ARITY, CSV_TS_CELL_INDEX, CsvRefusal,
    IdentityFields, canonical_identity_fields, render_csv_bytes, s3_key_assemble, symbol_key_safe,
};

/// BLAKE3 hex digest of this crate's source (`Cargo.toml` + `src/**/*.rs`),
/// baked at build time by `build.rs` under the shared FRAMING v1 of
/// `rust/source_digest_core.rs` (DEC-RUST-SOURCE-DIGEST-TWIN-001,
/// `docs/MIGRATION.md` #1258). The PyO3 module exports it as
/// `SOURCE_DIGEST` / `__source_digest__` (a doctor wheel-freshness stage is
/// future work, not claimed here).
pub const SOURCE_DIGEST: &str = env!("BRUTEX_SOURCE_DIGEST");

#[cfg(feature = "python")]
mod python;
