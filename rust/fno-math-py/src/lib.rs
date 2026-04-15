//! # fno-math-py — minimal P3 kickoff bindings
//!
//! PyO3 wrapper around the scalar public API of the `fno-math` Rust
//! crate. This first-pass wheel exposes only the Black-Scholes
//! scalar pricing + solver + per-option Greeks, which is enough to
//! unblock BruteX's concrete options strategies (Iron Condor,
//! Iron Fly, Straddle, etc.) that depend on Greeks for sizing and
//! hedging decisions.
//!
//! **What this wheel exposes (v0.1.0):**
//!
//! ```text
//! fno_math_py
//! ├── bs_call_price(spot, strike, time, rate, div, sigma) -> float
//! ├── bs_put_price(spot, strike, time, rate, div, sigma) -> float
//! ├── bs_price(side, spot, strike, time, rate, div, sigma) -> float
//! ├── iv_solve(side, spot, strike, time, rate, div, market_price) -> Optional[float]
//! ├── compute_greeks(side, spot, strike, time, rate, div, market_price) -> Optional[dict]
//! └── compute_greeks_from_iv(side, spot, strike, time, rate, div, iv,
//!                             market_price, day_count) -> dict
//! ```
//!
//! **Side** is a plain `str` — `"CE"` / `"C"` / `"CALL"` maps to
//! `OptionSide::Call`, `"PE"` / `"P"` / `"PUT"` maps to
//! `OptionSide::Put`. Anything else raises `ValueError` at the FFI
//! boundary before hitting the Rust solver.
//!
//! **Indicator wrapping** (SMA/EMA/RSI/…) is still TODO — the
//! `fno_math::indicators` API is the stateful `IndicatorEngine`
//! which needs a richer wrapper (likely a PyClass holding the
//! Rust engine). See `docs/runbooks/fno-math-extraction.md` for
//! the full population plan.
//!
//! Every wrapper is a pure forwarder: zero logic, zero
//! reimplementation. The bytes running under BruteX's Greek
//! computations are byte-identical to tickvault's live pipeline.

use fno_math::greeks::black_scholes::{
    OptionGreeks, OptionSide, bs_call_price, bs_price, bs_put_price, compute_greeks,
    compute_greeks_from_iv, iv_solve,
};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyDict;

// ---------------------------------------------------------------------------
// Side parsing
// ---------------------------------------------------------------------------

/// Parse a Python string into a Rust `OptionSide`.
///
/// Accepts case-insensitive `"CE"`, `"C"`, `"CALL"` → Call and
/// `"PE"`, `"P"`, `"PUT"` → Put. Anything else raises `ValueError`.
fn parse_side(side: &str) -> PyResult<OptionSide> {
    match side.to_ascii_uppercase().as_str() {
        "CE" | "C" | "CALL" => Ok(OptionSide::Call),
        "PE" | "P" | "PUT" => Ok(OptionSide::Put),
        other => Err(PyValueError::new_err(format!(
            "option side must be one of CE/C/CALL or PE/P/PUT, got {other:?}"
        ))),
    }
}

// ---------------------------------------------------------------------------
// OptionGreeks -> dict projection
// ---------------------------------------------------------------------------

/// Project a Rust `OptionGreeks` struct into a Python dict.
///
/// Using a plain dict (instead of a `PyClass` wrapper) keeps the
/// Python consumer side dependency-free: the dict is serialisable
/// with `json.dumps`, survives pickling, and plays nicely with the
/// `brutex.strategies.combo.canonicalise_params` hash pipeline.
fn greeks_to_dict<'py>(py: Python<'py>, g: OptionGreeks) -> PyResult<Bound<'py, PyDict>> {
    let d = PyDict::new(py);
    d.set_item("iv", g.iv)?;
    d.set_item("delta", g.delta)?;
    d.set_item("gamma", g.gamma)?;
    d.set_item("theta", g.theta)?;
    d.set_item("vega", g.vega)?;
    d.set_item("rho", g.rho)?;
    d.set_item("charm", g.charm)?;
    d.set_item("vanna", g.vanna)?;
    d.set_item("volga", g.volga)?;
    d.set_item("veta", g.veta)?;
    d.set_item("speed", g.speed)?;
    d.set_item("color", g.color)?;
    d.set_item("zomma", g.zomma)?;
    d.set_item("ultima", g.ultima)?;
    d.set_item("bs_price", g.bs_price)?;
    d.set_item("intrinsic", g.intrinsic)?;
    d.set_item("extrinsic", g.extrinsic)?;
    Ok(d)
}

// ---------------------------------------------------------------------------
// Scalar pricing functions
// ---------------------------------------------------------------------------

/// Black-Scholes price for a European call option.
#[pyfunction]
#[pyo3(name = "bs_call_price")]
#[pyo3(signature = (spot, strike, time, rate, div, sigma))]
fn py_bs_call_price(spot: f64, strike: f64, time: f64, rate: f64, div: f64, sigma: f64) -> f64 {
    bs_call_price(spot, strike, time, rate, div, sigma)
}

/// Black-Scholes price for a European put option.
#[pyfunction]
#[pyo3(name = "bs_put_price")]
#[pyo3(signature = (spot, strike, time, rate, div, sigma))]
fn py_bs_put_price(spot: f64, strike: f64, time: f64, rate: f64, div: f64, sigma: f64) -> f64 {
    bs_put_price(spot, strike, time, rate, div, sigma)
}

/// Black-Scholes price for either side — dispatches on `side`.
#[pyfunction]
#[pyo3(name = "bs_price")]
#[pyo3(signature = (side, spot, strike, time, rate, div, sigma))]
fn py_bs_price(
    side: &str,
    spot: f64,
    strike: f64,
    time: f64,
    rate: f64,
    div: f64,
    sigma: f64,
) -> PyResult<f64> {
    let side = parse_side(side)?;
    Ok(bs_price(side, spot, strike, time, rate, div, sigma))
}

// ---------------------------------------------------------------------------
// IV solver
// ---------------------------------------------------------------------------

/// Solve for implied volatility. Returns `None` if the solver fails
/// (out-of-range price, non-finite inputs, no convergence).
#[pyfunction]
#[pyo3(name = "iv_solve")]
#[pyo3(signature = (side, spot, strike, time, rate, div, market_price))]
fn py_iv_solve(
    side: &str,
    spot: f64,
    strike: f64,
    time: f64,
    rate: f64,
    div: f64,
    market_price: f64,
) -> PyResult<Option<f64>> {
    let side = parse_side(side)?;
    Ok(iv_solve(side, spot, strike, time, rate, div, market_price))
}

// ---------------------------------------------------------------------------
// Full Greeks (solver path + passthrough path)
// ---------------------------------------------------------------------------

/// Solve IV from `market_price`, then compute all 13 Greeks. Returns
/// `None` if the IV solver fails.
#[pyfunction]
#[pyo3(name = "compute_greeks")]
#[pyo3(signature = (side, spot, strike, time, rate, div, market_price))]
fn py_compute_greeks<'py>(
    py: Python<'py>,
    side: &str,
    spot: f64,
    strike: f64,
    time: f64,
    rate: f64,
    div: f64,
    market_price: f64,
) -> PyResult<Option<Bound<'py, PyDict>>> {
    let side = parse_side(side)?;
    match compute_greeks(side, spot, strike, time, rate, div, market_price) {
        Some(g) => Ok(Some(greeks_to_dict(py, g)?)),
        None => Ok(None),
    }
}

/// Compute all 13 Greeks from a **given** IV — no solver. Useful
/// for validating formulas against an external provider's IV.
#[pyfunction]
#[pyo3(name = "compute_greeks_from_iv")]
#[pyo3(signature = (side, spot, strike, time, rate, div, iv, market_price, day_count))]
#[allow(clippy::too_many_arguments)]
fn py_compute_greeks_from_iv<'py>(
    py: Python<'py>,
    side: &str,
    spot: f64,
    strike: f64,
    time: f64,
    rate: f64,
    div: f64,
    iv: f64,
    market_price: f64,
    day_count: f64,
) -> PyResult<Bound<'py, PyDict>> {
    let side = parse_side(side)?;
    let greeks =
        compute_greeks_from_iv(side, spot, strike, time, rate, div, iv, market_price, day_count);
    greeks_to_dict(py, greeks)
}

// ---------------------------------------------------------------------------
// Per-Greek scalar accessors (match `brutex.math` facade symbol names)
// ---------------------------------------------------------------------------
//
// Each of these is a zero-allocation forwarder to
// `compute_greeks_from_iv`, which in one pass computes every Greek
// from shared d1/d2. We pass the BS theoretical price as
// `market_price` so the intrinsic/extrinsic split has a coherent
// zero-extrinsic anchor (the Greek values themselves do not depend
// on market_price, only the intrinsic/extrinsic projection does).
//
// Signature rationale: all six take the full
// `(side, spot, strike, time, rate, div, sigma)` tuple so the
// caller always has dividend yield under explicit control. No
// Python-side default hides an implicit `div=0` assumption.

const FACADE_DAY_COUNT: f64 = 365.0;

/// Black-Scholes delta for the given option.
#[pyfunction]
#[pyo3(name = "delta")]
#[pyo3(signature = (side, spot, strike, time, rate, div, sigma))]
fn py_delta(
    side: &str,
    spot: f64,
    strike: f64,
    time: f64,
    rate: f64,
    div: f64,
    sigma: f64,
) -> PyResult<f64> {
    let side = parse_side(side)?;
    let bs = bs_price(side, spot, strike, time, rate, div, sigma);
    Ok(
        compute_greeks_from_iv(side, spot, strike, time, rate, div, sigma, bs, FACADE_DAY_COUNT)
            .delta,
    )
}

/// Black-Scholes gamma for the given option.
#[pyfunction]
#[pyo3(name = "gamma")]
#[pyo3(signature = (side, spot, strike, time, rate, div, sigma))]
fn py_gamma(
    side: &str,
    spot: f64,
    strike: f64,
    time: f64,
    rate: f64,
    div: f64,
    sigma: f64,
) -> PyResult<f64> {
    let side = parse_side(side)?;
    let bs = bs_price(side, spot, strike, time, rate, div, sigma);
    Ok(
        compute_greeks_from_iv(side, spot, strike, time, rate, div, sigma, bs, FACADE_DAY_COUNT)
            .gamma,
    )
}

/// Black-Scholes theta for the given option (per calendar day).
#[pyfunction]
#[pyo3(name = "theta")]
#[pyo3(signature = (side, spot, strike, time, rate, div, sigma))]
fn py_theta(
    side: &str,
    spot: f64,
    strike: f64,
    time: f64,
    rate: f64,
    div: f64,
    sigma: f64,
) -> PyResult<f64> {
    let side = parse_side(side)?;
    let bs = bs_price(side, spot, strike, time, rate, div, sigma);
    Ok(
        compute_greeks_from_iv(side, spot, strike, time, rate, div, sigma, bs, FACADE_DAY_COUNT)
            .theta,
    )
}

/// Black-Scholes vega (sensitivity to 1% change in IV).
#[pyfunction]
#[pyo3(name = "vega")]
#[pyo3(signature = (side, spot, strike, time, rate, div, sigma))]
fn py_vega(
    side: &str,
    spot: f64,
    strike: f64,
    time: f64,
    rate: f64,
    div: f64,
    sigma: f64,
) -> PyResult<f64> {
    let side = parse_side(side)?;
    let bs = bs_price(side, spot, strike, time, rate, div, sigma);
    Ok(
        compute_greeks_from_iv(side, spot, strike, time, rate, div, sigma, bs, FACADE_DAY_COUNT)
            .vega,
    )
}

/// Black-Scholes rho (sensitivity to 1% change in risk-free rate).
#[pyfunction]
#[pyo3(name = "rho")]
#[pyo3(signature = (side, spot, strike, time, rate, div, sigma))]
fn py_rho(
    side: &str,
    spot: f64,
    strike: f64,
    time: f64,
    rate: f64,
    div: f64,
    sigma: f64,
) -> PyResult<f64> {
    let side = parse_side(side)?;
    let bs = bs_price(side, spot, strike, time, rate, div, sigma);
    Ok(
        compute_greeks_from_iv(side, spot, strike, time, rate, div, sigma, bs, FACADE_DAY_COUNT)
            .rho,
    )
}

/// Alias of :func:`iv_solve` — facade-compatible name.
#[pyfunction]
#[pyo3(name = "implied_volatility")]
#[pyo3(signature = (side, spot, strike, time, rate, div, market_price))]
fn py_implied_volatility(
    side: &str,
    spot: f64,
    strike: f64,
    time: f64,
    rate: f64,
    div: f64,
    market_price: f64,
) -> PyResult<Option<f64>> {
    let side = parse_side(side)?;
    Ok(iv_solve(side, spot, strike, time, rate, div, market_price))
}

// ---------------------------------------------------------------------------
// Module registration
// ---------------------------------------------------------------------------

/// Top-level `fno_math_py` Python module.
#[pymodule]
fn fno_math_py(m: &Bound<'_, PyModule>) -> PyResult<()> {
    // Scalar pricing + dispatch
    m.add_function(wrap_pyfunction!(py_bs_call_price, m)?)?;
    m.add_function(wrap_pyfunction!(py_bs_put_price, m)?)?;
    m.add_function(wrap_pyfunction!(py_bs_price, m)?)?;
    // IV solver (two names — compact + facade-friendly)
    m.add_function(wrap_pyfunction!(py_iv_solve, m)?)?;
    m.add_function(wrap_pyfunction!(py_implied_volatility, m)?)?;
    // Full-Greeks dict computations
    m.add_function(wrap_pyfunction!(py_compute_greeks, m)?)?;
    m.add_function(wrap_pyfunction!(py_compute_greeks_from_iv, m)?)?;
    // Per-Greek scalar accessors (match brutex.math facade names)
    m.add_function(wrap_pyfunction!(py_delta, m)?)?;
    m.add_function(wrap_pyfunction!(py_gamma, m)?)?;
    m.add_function(wrap_pyfunction!(py_theta, m)?)?;
    m.add_function(wrap_pyfunction!(py_vega, m)?)?;
    m.add_function(wrap_pyfunction!(py_rho, m)?)?;
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    Ok(())
}
