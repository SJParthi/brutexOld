//! PyO3 binding (feature `python`) — the thin plain-data edge.
//!
//! `DEC-RUST-STAGE3-REFINE-001` FFI discipline (the brutex-feed shape): only
//! plain ints / floats / strings / tuples cross the boundary. The grid
//! functions map their `Err(String)` verdicts to `ValueError` (the Python
//! wrappers re-raise them as `Stage3GridError` with the SAME message bytes);
//! the two session pyclasses map every protocol misuse to `ValueError`
//! BEFORE any core code could panic. No `unwrap` on a caller-reachable
//! path — a poisoned call degrades to a typed Python exception, never a
//! `PanicException` (the #1919 BaseException-escape lesson).
//!
//! MAINTAINER NOTE (hostile-review round 1, NIT-2): this crate builds with
//! the house `panic = "abort"` profile, so an UNGUARDED panic does not raise
//! a catchable `PanicException` — it SIGABRTs the whole Python process. The
//! FFI input validation in `RefineDriver::new`/`tell` and `SelectCore::push`
//! is therefore PROCESS-SURVIVAL load-bearing, not mere exception hygiene: a
//! future regression that lets unvalidated input reach a core `assert!` is
//! fatal to the host process, never a degradable error. Keep every
//! caller-reachable path validated BEFORE it enters `cmaes`/`linalg`.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::wrap_pyfunction;

use crate::grid::GridAxis;
use crate::refine::RefineDriver;
use crate::select::SelectCore;

/// Crate version baked at compile time (`CARGO_PKG_VERSION`).
#[pyfunction]
fn version() -> &'static str {
    env!("CARGO_PKG_VERSION")
}

fn axis(name: &str, lo: i64, hi: i64, tick: i64) -> PyResult<GridAxis> {
    GridAxis::new(name, lo, hi, tick).map_err(PyValueError::new_err)
}

/// Validate one grid axis; return its step count.
///
/// Raises `ValueError` with the exact `Stage3GridError` message text on an
/// invalid bound/tick (the Python `GridAxis.__post_init__` twin keeps its
/// own pure-Python copy of these checks so module import never needs the
/// wheel — this function is the CALLABLE validation surface + drift pin).
#[pyfunction]
fn grid_validate(name: &str, lo_paisa: i64, hi_paisa: i64, tick_paisa: i64) -> PyResult<i64> {
    Ok(axis(name, lo_paisa, hi_paisa, tick_paisa)?.steps())
}

/// Map *u* to the nearest on-grid int paisa (clamp + round-half-even snap).
#[pyfunction]
fn grid_denormalize(
    name: &str,
    lo_paisa: i64,
    hi_paisa: i64,
    tick_paisa: i64,
    u: f64,
) -> PyResult<i64> {
    axis(name, lo_paisa, hi_paisa, tick_paisa)?
        .denormalize(u)
        .map_err(PyValueError::new_err)
}

/// Map an ON-GRID int paisa back to its exact [0, 1] coordinate.
#[pyfunction]
fn grid_normalize(
    name: &str,
    lo_paisa: i64,
    hi_paisa: i64,
    tick_paisa: i64,
    paisa: i64,
) -> PyResult<f64> {
    axis(name, lo_paisa, hi_paisa, tick_paisa)?
        .normalize(paisa)
        .map_err(PyValueError::new_err)
}

/// One mask's restarted-CMA-ES refinement session (ask/tell protocol).
///
/// Construction takes the NORMALIZED x0 plus both axes; `ask()` returns the
/// next generation as QUANTIZED on-grid `(sl_paisa, tp_paisa)` int pairs;
/// `tell(values)` reports the MINIMIZATION objective (the Python wrapper
/// negates Sharpe); `done()` flips after the last restart's budget/stop.
#[pyclass]
pub struct RefineSession {
    driver: RefineDriver,
}

#[pymethods]
impl RefineSession {
    #[new]
    #[pyo3(signature = (x0, sigma0, seed, max_fevals, max_restarts, sl_axis, tp_axis))]
    fn new(
        x0: (f64, f64),
        sigma0: f64,
        seed: u64,
        max_fevals: u64,
        max_restarts: u32,
        sl_axis: (i64, i64, i64),
        tp_axis: (i64, i64, i64),
    ) -> PyResult<Self> {
        let sl = axis("sl_paisa", sl_axis.0, sl_axis.1, sl_axis.2)?;
        let tp = axis("tp_paisa", tp_axis.0, tp_axis.1, tp_axis.2)?;
        let driver =
            RefineDriver::new([x0.0, x0.1], sigma0, seed, max_fevals, max_restarts, sl, tp)
                .map_err(PyValueError::new_err)?;
        Ok(Self { driver })
    }

    /// Propose the next generation as on-grid `(sl_paisa, tp_paisa)` pairs.
    fn ask(&mut self) -> PyResult<Vec<(i64, i64)>> {
        self.driver.ask().map_err(PyValueError::new_err)
    }

    /// Report the minimization objective values for the last `ask`.
    fn tell(&mut self, values: Vec<f64>) -> PyResult<()> {
        self.driver.tell(&values).map_err(PyValueError::new_err)
    }

    /// Whether every restart has finished.
    fn done(&self) -> bool {
        self.driver.done()
    }

    /// Restarts run so far (== `max_restarts` once done).
    fn restarts_run(&self) -> u32 {
        self.driver.restarts_run()
    }
}

/// Streaming bounded top-K survivor selection (push/finish protocol).
///
/// `push` returns `(kept, evicted_seq)` so the Python caller retains ONLY
/// the kept rows (the bounded-memory law); `finish` emits every board in
/// sorted key order with its kept `seq`s in final leaderboard order.
#[pyclass]
pub struct SelectSession {
    core: SelectCore,
}

#[pymethods]
impl SelectSession {
    #[new]
    fn new(top_k: usize) -> PyResult<Self> {
        Ok(Self {
            core: SelectCore::new(top_k).map_err(PyValueError::new_err)?,
        })
    }

    /// Stream one row; `seq` is assigned in push order (0, 1, 2, …).
    fn push(
        &mut self,
        instrument: &str,
        tf: &str,
        mode: &str,
        sharpe: f64,
        run_id: &str,
    ) -> PyResult<(bool, Option<u64>)> {
        let out = self
            .core
            .push(instrument, tf, mode, sharpe, run_id)
            .map_err(PyValueError::new_err)?;
        Ok((out.kept, out.evicted_seq))
    }

    /// Emit `[((instrument, tf, mode), [seq, …]), …]` in sorted board order.
    #[allow(clippy::type_complexity)]
    fn finish(&mut self) -> PyResult<Vec<((String, String, String), Vec<u64>)>> {
        self.core.finish().map_err(PyValueError::new_err)
    }
}

#[pymodule]
fn _brutex_refine(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", version())?;
    // DEC-RUST-SOURCE-DIGEST-TWIN-001: the baked crate-source digest — ALSO
    // the refinement-run identity's `optimizer_build` token.
    m.add("SOURCE_DIGEST", crate::SOURCE_DIGEST)?;
    m.add("__source_digest__", crate::SOURCE_DIGEST)?;
    m.add_function(wrap_pyfunction!(grid_validate, m)?)?;
    m.add_function(wrap_pyfunction!(grid_denormalize, m)?)?;
    m.add_function(wrap_pyfunction!(grid_normalize, m)?)?;
    m.add_class::<RefineSession>()?;
    m.add_class::<SelectSession>()?;
    Ok(())
}
