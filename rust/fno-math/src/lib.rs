//! # fno-math
//!
//! Minimal Rust scaffold reserved for BruteX Phase 2/3 hot kernels
//! (per `CLAUDE.md` §4 + §5 + `docs/MIGRATION.md` Decision #68).
//!
//! BruteX is a **Track 1 signal-only backtest** over Dhan historical
//! data for 4 Indian spot indices (NIFTY 50, BANKNIFTY, SENSEX, INDIA
//! VIX). The crate currently contains only the `compat` types module
//! (Exchange / ExchangeSegment / NSE trading calendar) used by Python
//! integration. The Greeks / indicators / strategies / live-tick
//! modules previously inlined here were tickvault-extracted residue
//! and were removed per DEC-P0-025 (PR #47) — see `docs/MIGRATION.md`.
//!
//! ## Currently exported
//!
//! - [`compat`] — Exchange enum, ExchangeSegment, NSE trading calendar,
//!   instrument-type codecs, error types.
//! - [`math`] — empty placeholder for future Phase 2 utilities.
//!
//! ## Phase 2 (MG-043) plan
//!
//! When Track 1 needs Rust-accelerated indicators (Phase 2 / Phase 3
//! brute-force engine hot kernel), they will be added here as
//! BruteX-native modules — not by re-extracting from any other repo.
//!
//! This crate intentionally contains no I/O, no async, no logging, and
//! no allocations on hot paths. Every public function must be a
//! deterministic pure function from inputs to outputs.

#![cfg_attr(not(test), deny(clippy::unwrap_used))]
#![cfg_attr(not(test), deny(clippy::expect_used))]
#![cfg_attr(not(test), deny(clippy::print_stdout))]
#![cfg_attr(not(test), deny(clippy::print_stderr))]
#![cfg_attr(not(test), deny(clippy::dbg_macro))]
#![deny(unsafe_code)]
#![allow(missing_docs)]
#![allow(clippy::excessive_precision)]
#![allow(clippy::inconsistent_digit_grouping)]
#![allow(clippy::type_complexity)]
#![allow(clippy::bind_instead_of_map)]
#![allow(clippy::unnecessary_map_or)]
#![allow(clippy::assertions_on_constants)]
#![allow(clippy::needless_borrow)]
#![allow(clippy::collapsible_if)]
#![allow(clippy::too_many_arguments)]
#![allow(clippy::manual_range_contains)]
#![allow(clippy::field_reassign_with_default)]
#![allow(clippy::unreadable_literal)]

/// Domain types shared across the Rust hot kernel: Exchange enum,
/// ExchangeSegment, NSE trading calendar, instrument-type codecs,
/// error types. Future Phase 2 / Phase 3 modules consume these.
pub mod compat;

/// Reserved for Phase 2 math utilities (rolling stats, distributions,
/// solvers). Currently empty — populated when MG-043 needs it.
pub mod math;

/// DEC-P0-050 (PR-K): PyO3 bridge to Python. Future Stage 1B
/// AND-mask kernel, combo_hash blake3 migration, and other
/// performance-critical Rust functions get registered here.
pub mod python;

/// Crate version (semver). Must be bumped on any breaking change.
pub const VERSION: &str = env!("CARGO_PKG_VERSION");

// ────────────────────────────────────────────────────────────────────────
// PyO3 module entry point (DEC-P0-050 / PR-K)
// ────────────────────────────────────────────────────────────────────────
//
// `#[pymodule]` registers the `fno_math` Python module. Maturin produces
// a cdylib whose extern "C" `PyInit_fno_math` symbol matches this
// function name. Python's `import fno_math` resolves to this entry.

use pyo3::prelude::*;

#[pymodule]
fn fno_math(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", VERSION)?;
    m.add_function(wrap_pyfunction!(python::bitcount_u64s, m)?)?;
    m.add_function(wrap_pyfunction!(python::add_u64, m)?)?;
    Ok(())
}
