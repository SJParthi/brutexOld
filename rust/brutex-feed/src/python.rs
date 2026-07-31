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

use crate::{lookahead, materialize, scan, series};

/// Crate version baked at compile time (`CARGO_PKG_VERSION`).
#[pyfunction]
fn version() -> &'static str {
    env!("CARGO_PKG_VERSION")
}

/// Scan raw bar columns; return `(kept_rows, warn_events, positivity_stop)`
/// or `None` (ABSTAIN — caller degrades to the Python oracle).
///
/// See `crate::scan::scan_bars` for the full contract. A column-length
/// mismatch raises `ValueError` (the Python seam's defensive belt catches it
/// and degrades to the oracle).
///
/// ## FFI probe transcript (pyo3 0.29, `abi3-py312` — house discipline, the
/// `rust/fno-math/src/trade_sim.rs` `ResidentSweepSimU128` precedent)
///
/// Probe-verified 2026-07-11 on the maturin-built wheel (CPython 3.12.3,
/// x86_64-linux) that `Vec<f64>` crossing preserves the IEEE-754 payloads the
/// comparisons need:
///
/// * `-0.0` crosses with its sign bit intact: `scan_bars([], [-0.0], [1.0],
///   [1.0], [1.0], False, 4.0)` → `([], [], (0, 0))` — the positivity stop
///   fires on the OPEN field (`-0.0 <= 0.0` true), matching CPython.
/// * `float("nan")` crosses as NaN: a NaN close is KEPT and poisons the
///   baseline (`([0, 1, 2], [], None)` for `[100.0, nan, 12345.6]` closes in
///   one session — every subsequent comparison false, all kept).
/// * `float("inf")` crosses as +inf: an inf close SKIPS as an up-spike
///   (`([0], [(2, 0, 1, 0, 1)], None)` for `[100.0, inf]` closes).
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
        Vec<(usize, usize, usize, usize, usize)>,
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

// ── DEC-FEED-RUST-MATERIALIZE-002 — the wheel-REQUIRED extension surface ──
// APPEND-ONLY (the concurrent-sibling merge law): new pyfunctions + the
// BarrierCore pyclass register BELOW the frozen DEC-FEED-RUST-CORE-001
// surface; nothing above this line moved.

/// Strict scan (finiteness-first): returns
/// `(kept_rows, warn_events, (row, field, kind) | None)` or `None`
/// (ABSTAIN — a consulted timestamp left the fixed-IST envelope; the
/// wheel-required Python seam raises a LOUD refusal, it no longer
/// degrades). `kind` is 0 = non-positive / 1 = non-finite. A column-length
/// mismatch raises `ValueError`.
#[pyfunction]
#[pyo3(signature = (ts_us, opens, highs, lows, closes, screen_enabled, max_ratio))]
#[allow(clippy::type_complexity)]
fn scan_bars_strict(
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
        Vec<(usize, usize, usize, usize, usize)>,
        Option<(usize, usize, usize)>,
    )>,
> {
    let outcome = materialize::scan_bars_strict(
        &ts_us,
        &opens,
        &highs,
        &lows,
        &closes,
        screen_enabled,
        max_ratio,
    )
    .map_err(PyValueError::new_err)?;
    Ok(outcome.map(|o| (o.kept_rows, o.warn_events, o.stop)))
}

/// Refuse an epoch-µs / step magnitude outside the crate's OWN fixed-IST
/// envelope `[0, MAX_SAFE_TS_US)` — `DEC-FFI-MAGNITUDE-CEILING-001`.
///
/// The predicate itself is the crate's ONE shared magnitude law
/// (`scan::ts_in_envelope`, re-exported as `series::ts_in_envelope`): the
/// single expression every decision site calls — the two scans' ABSTAIN
/// gates, `series::ownership_cut`'s per-timestamp arm, and this guard — so
/// they cannot drift apart. Those sites can express the refusal in their
/// return type (`Ok(None)` / `Option`); `expected_bar_instant` returns a
/// plain `bool`, so the refusal has to be raised here.
///
/// Why a refusal and not a `checked_*` fallback: this crate's
/// `[profile.release]` carries BOTH `overflow-checks = true` and
/// `panic = "abort"` (Cargo.toml), so an overflow inside
/// `series::expected_bar_instant`'s label arithmetic does not unwind into
/// pyo3's catch-and-convert wrapper — it SIGABRTs the CPython interpreter.
/// A `ValueError` naming the offending argument is diagnosable; a dead
/// process is not.
#[inline]
fn require_ts_envelope(name: &str, value: i64) -> PyResult<()> {
    if series::ts_in_envelope(value) {
        return Ok(());
    }
    Err(PyValueError::new_err(format!(
        "{name} must be in [0, {}) epoch-µs (fixed-IST envelope); got {value}",
        scan::MAX_SAFE_TS_US
    )))
}

/// [`require_ts_envelope`] for one `sessions[i].<field>` element, with the
/// argument NAME built lazily.
///
/// The name is a `format!` — an allocation — and the loop below runs it for
/// three fields of every session on EVERY call. Passing it as an argument
/// (`require_ts_envelope(&format!(…), v)`) would allocate on the happy path,
/// where the value is in envelope and the string is dropped unread; the
/// predicate is checked FIRST here so the allocation happens only on the
/// refusal branch. The refusal itself still goes through the single
/// error-text expression in [`require_ts_envelope`] — the message bytes are
/// unchanged.
#[inline]
fn require_ts_envelope_at(field: &str, index: usize, value: i64) -> PyResult<()> {
    if series::ts_in_envelope(value) {
        return Ok(());
    }
    require_ts_envelope(&format!("sessions[{index}].{field}"), value)
}

/// `_expected_bar_time_in_window`'s geometric core: `step_us == 0` marks
/// the derived-`1day` arm; `sessions` are `(stamp_us, open_us, close_us)`
/// exact epoch-microsecond triples in chronological order. Pure verdict —
/// the Python wrapper owns the calendar walk + both abstain arms.
///
/// Every integer argument is envelope-checked FIRST
/// (`DEC-FFI-MAGNITUDE-CEILING-001`): the intraday label expression
/// `open + -((open - seg_start) / step) * step` (`series.rs`) carries five
/// unchecked `i64` ops, and under this crate's `panic = "abort"` release
/// profile an overflow there kills the caller's interpreter instead of
/// raising. Bounding all inputs into `[0, MAX_SAFE_TS_US)` (≈ year 9999)
/// makes every one of those ops provably overflow-free:
/// `|open - seg_start| < MAX_SAFE_TS_US`, so the quotient, its negation and
/// the re-multiplication stay inside that same magnitude, and the final add
/// is `< 2 · MAX_SAFE_TS_US ≈ 5.1e17 ≪ i64::MAX`.
///
/// Production can never trip it: `series_load.py` derives every argument
/// from a real `datetime` through `_ts_epoch_us`, so the values are
/// calendar-bounded far inside the envelope (a `step_us` likewise comes from
/// a positive `timedelta`). Only a direct FFI call can reach the refusal.
#[pyfunction]
#[pyo3(signature = (seg_start_us, seg_end_us, step_us, now_us, sessions))]
fn expected_bar_instant(
    seg_start_us: i64,
    seg_end_us: i64,
    step_us: i64,
    now_us: i64,
    sessions: Vec<(i64, i64, i64)>,
) -> PyResult<bool> {
    require_ts_envelope("seg_start_us", seg_start_us)?;
    require_ts_envelope("seg_end_us", seg_end_us)?;
    // `step_us == 0` is the derived-`1day` arm; a positive step is the
    // intraday grid. A NEGATIVE step is not a production shape (Python
    // computes it from a positive `timedelta`) and is exactly the argument
    // that turns the label multiply into an abort, so it is refused here.
    require_ts_envelope("step_us", step_us)?;
    require_ts_envelope("now_us", now_us)?;
    for (i, &(stamp_us, open_us, close_us)) in sessions.iter().enumerate() {
        require_ts_envelope_at("stamp_us", i, stamp_us)?;
        require_ts_envelope_at("open_us", i, open_us)?;
        require_ts_envelope_at("close_us", i, close_us)?;
    }
    Ok(series::expected_bar_instant(
        seg_start_us,
        seg_end_us,
        step_us,
        now_us,
        &sessions,
    ))
}

/// The exact IST-date ownership cut: kept indices ascending, or `None`
/// (ABSTAIN — a timestamp left the fixed-IST envelope; the caller raises
/// a loud refusal, never a silently wrong civil date).
#[pyfunction]
#[pyo3(signature = (ts_us, own_start_days, own_end_days))]
fn ownership_cut(ts_us: Vec<i64>, own_start_days: i64, own_end_days: i64) -> Option<Vec<usize>> {
    series::ownership_cut(&ts_us, own_start_days, own_end_days)
}

/// `True` iff stitching breaks the disjoint-ownership monotonicity law
/// (`first <= prev_last` — the caller raises `FuturesSeriesError`).
#[pyfunction]
#[pyo3(signature = (prev_last_us, first_us))]
fn boundary_monotonic_break(prev_last_us: i64, first_us: i64) -> bool {
    series::boundary_monotonic_break(prev_last_us, first_us)
}

/// The P-06 barrier state machine — verdict codes out, exceptions built by
/// the thin Python wrapper (`brutex/feed/lookahead.py::LookAheadBarrier`),
/// so every pinned message byte + exception TYPE stays Python-owned.
#[pyclass(name = "BarrierCore")]
pub struct BarrierCore {
    state: lookahead::BarrierState,
}

#[pymethods]
impl BarrierCore {
    #[new]
    #[pyo3(signature = (record))]
    fn new(record: bool) -> Self {
        Self {
            state: lookahead::BarrierState::new(record),
        }
    }

    /// Current bar index `N` (`-1` before the first successful advance).
    #[getter]
    fn current_index(&self) -> i64 {
        self.state.current_index()
    }

    /// Whether this barrier retains the touched-index trail.
    #[getter]
    fn records(&self) -> bool {
        self.state.records()
    }

    /// Every permitted access index, in call order (empty in O(1) mode).
    fn touched(&self) -> Vec<i64> {
        self.state.touched().to_vec()
    }

    /// Largest permitted index touched, or `None` when nothing recorded.
    fn max_touched(&self) -> Option<i64> {
        self.state.max_touched()
    }

    /// Advance verdict: 0 = ok · 1 = negative · 2 = backwards.
    fn advance_to(&mut self, index: i64) -> u8 {
        self.state.advance_to(index)
    }

    /// Access verdict: 0 = ok · 1 = no-cursor · 2 = negative ·
    /// 3 = look-ahead.
    fn access(&mut self, index: i64) -> u8 {
        self.state.access(index)
    }

    /// Restore the just-constructed form (cursor `-1`, empty trail).
    fn reset(&mut self) {
        self.state.reset();
    }
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
    // DEC-FEED-RUST-MATERIALIZE-002 (append-only registrations).
    m.add_function(wrap_pyfunction!(scan_bars_strict, m)?)?;
    m.add_function(wrap_pyfunction!(expected_bar_instant, m)?)?;
    m.add_function(wrap_pyfunction!(ownership_cut, m)?)?;
    m.add_function(wrap_pyfunction!(boundary_monotonic_break, m)?)?;
    m.add_class::<BarrierCore>()?;
    Ok(())
}
