//! PyO3 binding (feature `python`) — the thin tuple-mapping edge.
//!
//! ONE `#[pyfunction]` ([`scan_bars`]) delegates to the pure core
//! (`crate::scan::scan_bars`) and maps its outcome to plain Python tuples of
//! INTEGERS. No float, no string, no timestamp crosses Rust → Python — the
//! warn-record offenders are returned as ROW INDICES and Python logs its OWN
//! float objects (the structural bit-identity guarantee of
//! DEC-FEED-RUST-CORE-001).

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::wrap_pyfunction;

use crate::scan;

/// Crate version baked at compile time (`CARGO_PKG_VERSION`).
#[pyfunction]
fn version() -> &'static str {
    env!("CARGO_PKG_VERSION")
}

/// Scan raw bar columns; return `(kept_rows, warn_events, positivity_stop)`
/// or `None` (ABSTAIN — caller degrades to the Python oracle).
///
/// See `crate::scan::scan_bars` for the full contract — `warn_events` are
/// the 9-int decision-plane records shared with
/// `bad_tick.BadTickScreen.screen_mask` (`ScreenWarnEvent`). A column-length
/// mismatch raises `ValueError` (the Python seam's defensive belt catches it
/// and degrades to the oracle).
///
/// ## FFI probe transcript (pyo3 0.29, `abi3-py312` — house discipline, the
/// `rust/fno-math/src/trade_sim.rs` `ResidentSweepSimU128` precedent)
///
/// Probe-verified 2026-07-20 on the maturin-built wheel (CPython 3.12,
/// x86_64-linux) that `Vec<f64>` crossing preserves the IEEE-754 payloads the
/// comparisons need (re-probed for the head-seed + hi/lo law,
/// DEC-BADTICK-HEAD-SEED-001 / DEC-BADTICK-HILO-SCREEN-001):
///
/// * `-0.0` crosses with its sign bit intact: `scan_bars([], [-0.0], [1.0],
///   [1.0], [1.0], False, 4.0)` → `([], [], (0, 0))` — the positivity stop
///   fires on the OPEN field (`-0.0 <= 0.0` true), matching CPython.
/// * `float("nan")` crosses as NaN: a NaN close is KEPT UNJUDGED and
///   excluded from the head vote (`([0, 1, 2],
///   [(1, 3, 0, 0, 2, 0, 0, 0, 0)], None)` for `[100.0, nan, 12345.6]`
///   closes in one session — the two finite closes form a 2-bar pending
///   head, mutually out-of-band, flushed as ONE `HEAD_UNCONFIRMED` event).
/// * `float("inf")` crosses as +inf: an inf close is KEPT UNJUDGED
///   (`([0, 1], [], None)` for `[100.0, inf]` closes — a lone finite head
///   bar has no witness, silent keep), while an inf HIGH on a sane close
///   rides the ordinary predicate (out-of-band — dropped).
/// * `2**63` in `ts_us` raises `OverflowError` at the pyo3 edge (i64 bound),
///   never a silent wrap — the Python seam's `except Exception` belt
///   degrades such a call to the oracle.
#[pyfunction]
#[pyo3(signature = (ts_us, opens, highs, lows, closes, screen_enabled, max_ratio))]
#[allow(clippy::type_complexity)]
fn scan_bars(
    ts_us: Vec<i64>,
    opens: Vec<f64>,
    highs: Vec<f64>,
    lows: Vec<f64>,
    closes: Vec<f64>,
    screen_enabled: bool,
    max_ratio: f64,
) -> PyResult<
    Option<(
        Vec<usize>,
        Vec<crate::scan::WarnEvent>,
        Option<(usize, usize)>,
    )>,
> {
    let outcome = scan::scan_bars(
        &ts_us,
        &opens,
        &highs,
        &lows,
        &closes,
        screen_enabled,
        max_ratio,
    )
    .map_err(PyValueError::new_err)?;
    Ok(outcome.map(|o| (o.kept_rows, o.warn_events, o.positivity_stop)))
}

#[pymodule]
fn _brutex_feed(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", version())?;
    // Dunder-named build stamp (sibling convention): invisible to
    // non-underscore symbol-surface checks.
    m.add("__source_digest__", crate::SOURCE_DIGEST)?;
    // Plain-named twin: part of the shim's `_EXPECTED_COMPILED_SYMBOLS`
    // handshake surface (DEC-FEED-RUST-CORE-001 design §2).
    m.add("SOURCE_DIGEST", crate::SOURCE_DIGEST)?;
    m.add_function(wrap_pyfunction!(version, m)?)?;
    m.add_function(wrap_pyfunction!(scan_bars, m)?)?;
    Ok(())
}
