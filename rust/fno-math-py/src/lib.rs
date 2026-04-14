//! # fno-math-py
//!
//! PyO3 wrapper around [`fno-math`](../fno-math). Exposes every public
//! function in `fno-math` as a Python callable, grouped into submodules
//! that mirror the Rust module layout (`greeks`, `indicators`,
//! `strategies`, `math`).
//!
//! **Status:** scaffold only. Populated alongside `fno-math` by the
//! extraction procedure in `docs/runbooks/fno-math-extraction.md`.
//!
//! ## Layout
//!
//! ```text
//! fno_math_py (Python module)
//! ├── greeks
//! │   ├── delta(S, K, T, r, sigma, is_call) -> f64
//! │   ├── gamma(S, K, T, r, sigma) -> f64
//! │   ├── theta(...) -> f64
//! │   ├── vega(...) -> f64
//! │   ├── rho(...) -> f64
//! │   └── implied_volatility(price, S, K, T, r, is_call) -> f64
//! ├── indicators
//! │   ├── sma(series, period) -> ndarray
//! │   ├── ema(series, period) -> ndarray
//! │   ├── rsi(series, period) -> ndarray
//! │   └── ... (full catalog)
//! ├── strategies
//! │   └── ... (strategy types + runners)
//! └── math
//!     └── rolling_mean(series, period) -> ndarray
//! ```
//!
//! Each Python function is a thin wrapper that:
//! 1. Accepts numpy arrays and scalar Python values
//! 2. Acquires the `GILPool` or releases the GIL via `py.allow_threads`
//!    for long-running computations
//! 3. Calls the underlying `fno_math::<path>` Rust function
//! 4. Returns the result as a numpy array or Python scalar
//!
//! Zero logic lives here. Every wrapper is a forwarder.

// This file compiles as a stub once PyO3 is uncommented. The actual
// #[pymodule] body is populated during the extraction refactor.

// use pyo3::prelude::*;
//
// /// The top-level `fno_math_py` Python module.
// #[pymodule]
// fn fno_math_py(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
//     // Register submodules
//     register_greeks(py, m)?;
//     register_indicators(py, m)?;
//     register_strategies(py, m)?;
//     register_math(py, m)?;
//     Ok(())
// }
//
// fn register_greeks(py: Python<'_>, parent: &Bound<'_, PyModule>) -> PyResult<()> {
//     let m = PyModule::new_bound(py, "greeks")?;
//     // m.add_function(wrap_pyfunction!(py_delta, &m)?)?;
//     // m.add_function(wrap_pyfunction!(py_gamma, &m)?)?;
//     // ...
//     parent.add_submodule(&m)?;
//     Ok(())
// }
//
// // Example wrapper shape (uncomment during extraction):
// //
// // #[pyfunction]
// // #[pyo3(name = "delta")]
// // fn py_delta(
// //     spot: f64,
// //     strike: f64,
// //     time_to_expiry: f64,
// //     risk_free: f64,
// //     volatility: f64,
// //     is_call: bool,
// // ) -> f64 {
// //     fno_math::greeks::black_scholes::delta(
// //         spot, strike, time_to_expiry, risk_free, volatility, is_call,
// //     )
// // }
