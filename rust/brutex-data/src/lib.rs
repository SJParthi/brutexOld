//! brutex-data — pure-law kernels for brutex/data (ADR-0020 slice T10).
//!
//! DEC-DATA-RUST-PURELAW-001 (MIGRATION.md #1940). PR-1: the fno_symbol
//! structural grammar ([`fno_symbol::parse_fno_symbol_coords`] — integers
//! only cross the FFI; Python materializes every object). Room reserved:
//! lake_keys (annex A, HELD pending a fleet ruling).
//!
//! The crate is stdlib-pure (no polars/chrono/regex) and OPTIONAL-class:
//! consumed only through the import-guarded default-OFF facade
//! `brutex/data/fno_symbol_rust.py`; built only by its own CI lane
//! (`.github/workflows/data-rust-parity.yml`).

pub mod fno_symbol; // pure core — cargo-testable, no Python

/// Crate version baked at compile time (`CARGO_PKG_VERSION`) — surfaced as
/// `__version__` (the facade's engaged INFO reads it; no source digest
/// exists for an OPTIONAL-class kernel, the brutex-lake school).
pub const VERSION: &str = env!("CARGO_PKG_VERSION");

/// Sanity window for the RESOLVED expiry calendar year — the cargo-pinned
/// mirror of `brutex/data/fno_symbol.py::EXPIRY_YEAR_MIN`
/// (DEC-FNO-EXPIRY-YEAR-PIVOT-001; both endpoints inclusive).
pub const EXPIRY_YEAR_MIN: i32 = 2019;
/// Mirror of `brutex/data/fno_symbol.py::EXPIRY_YEAR_MAX` (cargo-pinned).
pub const EXPIRY_YEAR_MAX: i32 = 2059;

#[cfg(feature = "python")]
mod python {
    //! PyO3 binding (feature `python`) — the thin integers-only edge.
    //!
    //! ONE `#[pyfunction]` delegates to the pure core under a
    //! `std::panic::catch_unwind` boundary (the brutex-lake FFI-shell law:
    //! a panic never crosses raw — it maps to `RuntimeError`, which the
    //! facade's degrade set catches; the unwinding panic profile in
    //! Cargo.toml is what keeps this boundary live). No float, no string,
    //! no date object crosses Rust → Python — spans + y/m/d + a type code
    //! only (DEC-FEED-RUST-CORE-001 discipline).

    use pyo3::exceptions::PyRuntimeError;
    use pyo3::prelude::*;
    use pyo3::wrap_pyfunction;

    /// Decode an ASCII F&O contract symbol into the 9-slot integer tuple,
    /// or `None` (structural rejection — mirrors the oracle's never-raises
    /// contract). See `crate::fno_symbol::parse_fno_symbol_coords`.
    #[pyfunction]
    #[allow(clippy::type_complexity)]
    fn parse_fno_symbol_coords(
        symbol: &str,
    ) -> PyResult<Option<(u8, usize, usize, usize, i32, u8, u8, usize, usize)>> {
        std::panic::catch_unwind(|| crate::fno_symbol::parse_fno_symbol_coords(symbol)).map_err(
            |_| {
                PyRuntimeError::new_err(
                    "brutex-data parse_fno_symbol_coords panicked (caught at the FFI boundary)",
                )
            },
        )
    }

    // `#[pymodule]` registers the `_brutex_data` Python submodule. Maturin
    // produces a cdylib whose extern "C" `PyInit__brutex_data` symbol
    // matches this function name; under the mixed layout it installs at
    // `site-packages/brutex_data/_brutex_data.<abi>.so` with
    // `brutex_data/__init__.py` re-exporting the symbols (the fno-math
    // DEC-P0-055 convention).
    #[pymodule]
    fn _brutex_data(m: &Bound<'_, PyModule>) -> PyResult<()> {
        m.add("__version__", crate::VERSION)?;
        m.add_function(wrap_pyfunction!(parse_fno_symbol_coords, m)?)?;
        Ok(())
    }
}
