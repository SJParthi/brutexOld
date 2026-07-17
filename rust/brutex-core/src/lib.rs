//! brutex-core — the BruteX root-leaf Rust conversion crate (slice T35).
//!
//! DEC-CONFIG-RUST-LEAVES-001 (MIGRATION.md #1943). Milestone 1 hosts the
//! MONEY laws of `brutex/money.py` (DEC-MONEY-2DP-001) as exact
//! decimal-string arithmetic — see [`money`] for the algorithm, the probed
//! CPython parity hazards, the HALF_UP-not-half-even negative pin and the
//! twin-impl coordination note (the same half-away-from-zero law also
//! lives in `brutex/data/parquet_writer.py::_snap_price_expr`, whose
//! output the `rust/brutex-lake` seams consume — the two move together).
//!
//! Honesty banner (ADR-0020, mandatory verbatim): expected payoff ≈ 0 —
//! conversion is for uniformity, not speed. Each of these functions is a
//! sub-microsecond `Decimal` op in Python; the FFI hop is a small
//! SLOWDOWN, acceptable because the seam ships DEFAULT-OFF behind
//! `BRUTEX_CORE_RUST_MONEY` (a default flip needs its own DEC) and the
//! conversion buys uniformity across the Rust-first program.
//!
//! ## The strings-only FFI law
//!
//! ONLY strings cross this boundary, in BOTH directions. The Python
//! facade (`brutex/money_rust.py`) pre-normalizes each crossing `Decimal`
//! to its fixed-point `format(d, "f")` render and each paisa `int` to
//! `str(int)`, gates ELIGIBILITY Python-side (non-finite values,
//! out-of-bounds exponents, `|paisa| >= 2**63`, context-precision /
//! trap hazards all route to the `Decimal` oracle WITHOUT touching this
//! crate), and rebuilds `Decimal(...)` / `int(...)` from the returned
//! strings. Rust never sees a float, never builds a Python numeric.
//!
//! ## Error mapping at the boundary
//!
//! * A parse/shape failure (`money::*` returns `Err`) → `ValueError` — a
//!   facade DEGRADE member (serve the oracle, warn once).
//! * A panic → caught by `catch_unwind` → `RuntimeError` — likewise a
//!   degrade member; never a process abort mid-write (the brutex-lake
//!   boundary convention).

use std::panic::{AssertUnwindSafe, catch_unwind};

use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;

pub mod money;

/// Crate version — surfaced as `brutex_core.__version__`.
pub const VERSION: &str = env!("CARGO_PKG_VERSION");

/// The ONE FFI boundary shell shared by every money pyfunction: run the
/// pure core under `catch_unwind`, map `Err` → `ValueError` and a panic →
/// `RuntimeError` (both facade degrade members — module docs).
fn money_ffi_shell(
    name: &str,
    input: &str,
    f: fn(&str) -> Result<String, String>,
) -> PyResult<String> {
    match catch_unwind(AssertUnwindSafe(|| f(input))) {
        Ok(Ok(out)) => Ok(out),
        Ok(Err(msg)) => Err(PyValueError::new_err(msg)),
        Err(_) => Err(PyRuntimeError::new_err(format!(
            "brutex-core panic in {name} (caught at the FFI boundary)"
        ))),
    }
}

/// `round_money` twin — 2dp HALF_UP (away from zero) on a fixed-point string.
#[pyfunction]
fn round_money_str(rupees: &str) -> PyResult<String> {
    money_ffi_shell("round_money_str", rupees, money::round_money_2dp)
}

/// `rupees_to_paisa` twin — round to 2dp then the exact ×100 digit shift.
#[pyfunction]
fn rupees_to_paisa_str(rupees: &str) -> PyResult<String> {
    money_ffi_shell("rupees_to_paisa_str", rupees, money::rupees_to_paisa)
}

/// `paisa_to_rupees` twin — the exact ÷100 digit shift to a 2dp string.
#[pyfunction]
fn paisa_to_rupees_str(paisa: &str) -> PyResult<String> {
    money_ffi_shell("paisa_to_rupees_str", paisa, money::paisa_to_rupees)
}

/// `format_inr` twin — `₹` + comma-grouped-by-3 + always two decimals.
#[pyfunction]
fn format_inr_str(paisa: &str) -> PyResult<String> {
    money_ffi_shell("format_inr_str", paisa, money::format_inr)
}

// `#[pymodule]` registers the `_brutex_core` Python submodule. Maturin
// produces a cdylib whose extern "C" `PyInit__brutex_core` symbol matches
// this function name; under the mixed layout it installs at
// `site-packages/brutex_core/_brutex_core.<abi>.so` with
// `brutex_core/__init__.py` re-exporting the symbols (the fno-math
// DEC-P0-055 convention, via rust/brutex-lake).
#[pymodule]
fn _brutex_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", VERSION)?;
    // Milestone 1 — the money seam (DEC-CONFIG-RUST-LEAVES-001). Later
    // T35 milestones register ADDITIVELY beside these, never in place of
    // (the brutex-lake §4.1 concurrent-sibling merge law).
    m.add_function(wrap_pyfunction!(round_money_str, m)?)?;
    m.add_function(wrap_pyfunction!(rupees_to_paisa_str, m)?)?;
    m.add_function(wrap_pyfunction!(paisa_to_rupees_str, m)?)?;
    m.add_function(wrap_pyfunction!(format_inr_str, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn version_matches_manifest() {
        assert_eq!(VERSION, "0.1.0");
    }

    #[test]
    fn ffi_shell_maps_err_to_valueerror_shape() {
        // The shell itself is exercised end-to-end only through Python;
        // here we pin the pure-core routing it wraps (Ok and Err arms).
        assert_eq!(money::round_money_2dp("1.005").unwrap(), "1.01");
        assert!(money::round_money_2dp("junk").is_err());
    }
}
