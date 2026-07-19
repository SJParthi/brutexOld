//! # brutex-cascade — the cascade / results-depth compute laws in Rust
//!
//! DEC-RUST-CASCADE-RESULTS-001 (the T14 lane of the pure-Rust O(1)
//! conversion program). Phase A hosts three law families, ported
//! token-for-token from the DELETED Python implementations and verified
//! against the FROZEN pre-conversion parity corpus
//! (`tests/golden/fixtures/cascade_rust_parity/` in the main repo):
//!
//! * [`cascade_join`] — the ONE cascade-report → display-join
//!   flattener/validator;
//! * [`minute_path`] — the 1-minute-path excursion / equity / drawdown math;
//! * [`combo_aggregates`] — the per-combo aggregate fold + its CPython
//!   `json.dumps`-byte-identical lens renders.
//!
//! ## FFI boundary contract (the house decision-kernel discipline)
//!
//! Scalars and strings cross the boundary; Python keeps every type face
//! (frozen dataclasses, enums, error classes) and every raise whose message
//! interpolates a Python `repr` — EXCEPT the cascade-join projection, which
//! captures real CPython `repr()` strings during its one traversal so the
//! defect-note law stays byte-identical. Errors cross as `ValueError` /
//! `KeyError` (the shims re-wrap into the domain error types); no panic
//! crosses raw — the laws are panic-free by construction (checked i128
//! arithmetic, no indexing beyond validated bounds) and pyo3's own
//! `PanicException` guard remains the outer belt.

use pyo3::PyTypeInfo;
use pyo3::exceptions::{PyKeyError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PyString};
use std::collections::BTreeMap;

pub mod cascade_join;
pub mod cascade_wire;
pub mod combo_aggregates;
pub mod decimal;
pub mod ist;
pub mod minute_path;
pub mod pyjson;

use cascade_join::{GroupNode, Leaf, LeafR, ReportNode, RootView, VerdictNode};

/// `session_equity_series`'s FFI shape: `([(close, low, closed), …], status)`.
type EquitySeriesOut = (Vec<(i128, Option<i128>, i64)>, &'static str);

/// Crate version baked at compile time (`CARGO_PKG_VERSION`).
#[pyfunction]
fn version() -> &'static str {
    env!("CARGO_PKG_VERSION")
}

// ───────────────────────────── minute_path ─────────────────────────────

/// One instrument's parsed 1-minute bars — the `MinuteIndex` core.
#[pyclass(frozen, name = "MinuteIndexRs")]
struct MinuteIndexRs {
    bars: minute_path::MinuteBars,
}

#[pymethods]
impl MinuteIndexRs {
    /// Build from parallel columns: epoch-µs timestamps (strictly
    /// increasing — validated by the shim BEFORE construction) + rupee
    /// `str(Decimal)` price columns.
    #[new]
    fn new(
        ts_us: Vec<i64>,
        opens: Vec<String>,
        highs: Vec<String>,
        lows: Vec<String>,
    ) -> PyResult<Self> {
        let bars = minute_path::MinuteBars::from_strings(ts_us, &opens, &highs, &lows)
            .map_err(PyValueError::new_err)?;
        Ok(Self { bars })
    }

    /// Bar count (the shim's emptiness probe).
    fn __len__(&self) -> usize {
        self.bars.len()
    }

    /// `MinuteIndex.span_slice` — `(lo, hi)`, both endpoints inclusive.
    /// The `start > end` refusal (repr-carrying message) is the shim's.
    fn span_slice(&self, start_us: i64, end_us: i64) -> (usize, usize) {
        minute_path::span_slice(&self.bars.ts_us, start_us, end_us)
    }

    /// `trade_excursions_1m`'s fold → `(mae, mfe, status_token)`.
    fn excursions(
        &self,
        entry_us: i64,
        exit_us: i64,
        entry_fill_paisa: i64,
        is_buy: bool,
        exit_bar_full_range: bool,
    ) -> (Option<i128>, Option<i128>, &'static str) {
        minute_path::trade_excursions(
            &self.bars,
            entry_us,
            exit_us,
            entry_fill_paisa,
            is_buy,
            exit_bar_full_range,
        )
    }
}

/// `session_equity_series`'s walk (post-validation) →
/// `([(close, low, closed), …], status_token)`. *index* `None` = no
/// 1-minute data (close-only degrade).
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn session_equity_series(
    index: Option<PyRef<'_, MinuteIndexRs>>,
    entry_us: Vec<i64>,
    exit_us: Vec<i64>,
    is_buy: Vec<bool>,
    pnl_paisa: Vec<i64>,
    entry_px: Vec<String>,
    sessions: Vec<i64>,
    start_equity_paisa: i64,
) -> PyResult<EquitySeriesOut> {
    let n = entry_us.len();
    if [exit_us.len(), is_buy.len(), pnl_paisa.len(), entry_px.len()] != [n, n, n, n] {
        return Err(PyValueError::new_err(
            "session_equity_series: trade column lengths diverge",
        ));
    }
    let mut trades: Vec<minute_path::EqTrade> = Vec::with_capacity(n);
    for i in 0..n {
        trades.push(minute_path::EqTrade {
            entry_us: entry_us[i],
            exit_us: exit_us[i],
            is_buy: is_buy[i],
            pnl_paisa: pnl_paisa[i],
            entry_paisa: decimal::rupees_str_to_paisa(&entry_px[i])
                .map_err(PyValueError::new_err)?,
        });
    }
    let bars = index.as_ref().map(|ix| &ix.bars);
    let (points, status) =
        minute_path::session_equity_series(bars, &trades, &sessions, start_equity_paisa);
    Ok((
        points
            .into_iter()
            .map(|p| (p.close_paisa, p.intraday_low_paisa, p.trades_closed))
            .collect(),
        status,
    ))
}

/// `minute_path_max_drawdown`'s fold (post-validation) →
/// `(max_dd_paisa, max_dd_pct)`.
#[pyfunction]
fn minute_path_max_drawdown(
    closes: Vec<i128>,
    lows: Vec<Option<i128>>,
    start_equity_paisa: Option<i128>,
) -> PyResult<(i128, f64)> {
    if closes.len() != lows.len() {
        return Err(PyValueError::new_err(
            "minute_path_max_drawdown: column lengths diverge",
        ));
    }
    Ok(minute_path::max_drawdown(
        &closes,
        &lows,
        start_equity_paisa,
    ))
}

/// `ist_session_dates` — sorted unique IST epoch days of bar timestamps.
#[pyfunction]
fn ist_session_dates(ts_us: Vec<i64>) -> Vec<i64> {
    minute_path::session_dates(&ts_us)
}

// ─────────────────────────── combo_aggregates ───────────────────────────

/// The nearest-rank 0-based index (`rank - 1`); validity is the shim's
/// check (it raises the exact `ComboAggregateError` messages).
#[pyfunction]
fn nearest_rank_index(n: usize, pct: i64) -> usize {
    combo_aggregates::nearest_rank_index(n, pct)
}

/// `_float_stats` — `(min, clamped_mean, max)` over non-empty floats.
#[pyfunction]
fn float_stats(values: Vec<f64>) -> PyResult<(f64, f64, f64)> {
    if values.is_empty() {
        return Err(PyValueError::new_err(
            "float_stats: values must be non-empty",
        ));
    }
    Ok(combo_aggregates::float_stats(&values))
}

/// `aggregate_combo`'s fold (post the shim's run_id refusal) → the computed
/// record fields as a dict. An exit-reason token outside the walker
/// vocabulary raises `KeyError` (the Python tally's own error class).
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn aggregate_fold<'py>(
    py: Python<'py>,
    entry_us: Vec<i64>,
    exit_us: Vec<i64>,
    pnl_paisa: Vec<i64>,
    mae_paisa: Vec<i64>,
    mfe_paisa: Vec<i64>,
    mae_1m_paisa: Vec<Option<i64>>,
    mfe_1m_paisa: Vec<Option<i64>>,
    path_1m_ok: Vec<bool>,
    exit_reasons: Vec<String>,
    vix_entry: Vec<Option<f64>>,
    trend_days: Vec<i64>,
    trend_labels: Vec<String>,
    event_days: Vec<i64>,
    event_flags: Vec<Vec<String>>,
    eq_closes: Vec<i128>,
    eq_lows: Vec<Option<i128>>,
    row_total_trades: i64,
    row_net_pnl_paisa: i64,
    row_max_drawdown_paisa: i64,
    parity_ok: bool,
    start_equity_paisa: i64,
) -> PyResult<Bound<'py, PyDict>> {
    let n = entry_us.len();
    let widths = [
        exit_us.len(),
        pnl_paisa.len(),
        mae_paisa.len(),
        mfe_paisa.len(),
        mae_1m_paisa.len(),
        mfe_1m_paisa.len(),
        path_1m_ok.len(),
        exit_reasons.len(),
        vix_entry.len(),
    ];
    if widths.iter().any(|&w| w != n) {
        return Err(PyValueError::new_err(
            "aggregate_fold: trade column lengths diverge",
        ));
    }
    if trend_days.len() != trend_labels.len()
        || event_days.len() != event_flags.len()
        || eq_closes.len() != eq_lows.len()
    {
        return Err(PyValueError::new_err(
            "aggregate_fold: lens/equity column lengths diverge",
        ));
    }
    let trades: Vec<combo_aggregates::AggTrade> = (0..n)
        .map(|i| combo_aggregates::AggTrade {
            entry_us: entry_us[i],
            exit_us: exit_us[i],
            pnl_paisa: pnl_paisa[i],
            mae_paisa: mae_paisa[i],
            mfe_paisa: mfe_paisa[i],
            mae_1m_paisa: mae_1m_paisa[i],
            mfe_1m_paisa: mfe_1m_paisa[i],
            path_1m_ok: path_1m_ok[i],
            exit_reason: exit_reasons[i].clone(),
            vix_entry: vix_entry[i],
        })
        .collect();
    let trend_by_day: BTreeMap<i64, String> = trend_days.into_iter().zip(trend_labels).collect();
    let event_flags_by_day: BTreeMap<i64, Vec<String>> =
        event_days.into_iter().zip(event_flags).collect();
    let out = combo_aggregates::aggregate_fold(
        &trades,
        &trend_by_day,
        &event_flags_by_day,
        &eq_closes,
        &eq_lows,
        row_total_trades,
        row_net_pnl_paisa,
        row_max_drawdown_paisa,
        parity_ok,
        start_equity_paisa,
    )
    .map_err(PyKeyError::new_err)?;
    let d = PyDict::new(py);
    d.set_item("n_trades", out.n_trades)?;
    d.set_item("zero_trades", out.zero_trades)?;
    let tri = |stats: Option<(i128, i128, i128)>| {
        (stats.map(|s| s.0), stats.map(|s| s.1), stats.map(|s| s.2))
    };
    let (mae_avg, mae_worst, mae_p95) = tri(out.mae_stats);
    d.set_item("mae_avg_paisa", mae_avg)?;
    d.set_item("mae_worst_paisa", mae_worst)?;
    d.set_item("mae_p95_paisa", mae_p95)?;
    let (mfe_avg, mfe_best, mfe_p95) = tri(out.mfe_stats);
    d.set_item("mfe_avg_paisa", mfe_avg)?;
    d.set_item("mfe_best_paisa", mfe_best)?;
    d.set_item("mfe_p95_paisa", mfe_p95)?;
    let (mae_1m_avg, mae_1m_worst, mae_1m_p95) = tri(out.mae_1m_stats);
    d.set_item("mae_1m_avg_paisa", mae_1m_avg)?;
    d.set_item("mae_1m_worst_paisa", mae_1m_worst)?;
    d.set_item("mae_1m_p95_paisa", mae_1m_p95)?;
    let (mfe_1m_avg, mfe_1m_best, mfe_1m_p95) = tri(out.mfe_1m_stats);
    d.set_item("mfe_1m_avg_paisa", mfe_1m_avg)?;
    d.set_item("mfe_1m_best_paisa", mfe_1m_best)?;
    d.set_item("mfe_1m_p95_paisa", mfe_1m_p95)?;
    d.set_item("trades_with_1m", out.trades_with_1m)?;
    d.set_item("max_dd_minute_path_paisa", out.max_dd_minute_path_paisa)?;
    d.set_item("max_dd_minute_path_pct", out.max_dd_minute_path_pct)?;
    d.set_item("worst5_days_json", out.worst5_days_json)?;
    d.set_item("vix_win_min", out.vix_win.map(|v| v.0))?;
    d.set_item("vix_win_avg", out.vix_win.map(|v| v.1))?;
    d.set_item("vix_win_max", out.vix_win.map(|v| v.2))?;
    d.set_item("vix_loss_min", out.vix_loss.map(|v| v.0))?;
    d.set_item("vix_loss_avg", out.vix_loss.map(|v| v.1))?;
    d.set_item("vix_loss_max", out.vix_loss.map(|v| v.2))?;
    d.set_item("vix_band_lo", out.vix_band.map(|v| v.0))?;
    d.set_item("vix_band_hi", out.vix_band.map(|v| v.1))?;
    d.set_item("vix_stamped_trades", out.vix_stamped_trades)?;
    d.set_item("pnl_by_year_json", out.pnl_by_year_json)?;
    d.set_item("pnl_by_trend_json", out.pnl_by_trend_json)?;
    d.set_item("one_regime_wonder", out.one_regime_wonder)?;
    d.set_item("regime_wonder_axis", out.regime_wonder_axis)?;
    d.set_item("entry_hour_hist_json", out.entry_hour_hist_json)?;
    d.set_item("event_day_counts_json", out.event_day_counts_json)?;
    d.set_item("event_day_pnl_json", out.event_day_pnl_json)?;
    d.set_item("exits_stop_loss", out.exit_tally[0])?;
    d.set_item("exits_take_profit", out.exit_tally[1])?;
    d.set_item("exits_forced_1520", out.exit_tally[2])?;
    d.set_item("exits_signal", out.exit_tally[3])?;
    d.set_item("exits_end_of_data", out.exit_tally[4])?;
    d.set_item("exits_session_gap", out.exit_tally[5])?;
    d.set_item("max_consecutive_losses", out.max_consecutive_losses)?;
    d.set_item("max_consecutive_wins", out.max_consecutive_wins)?;
    d.set_item("win_loss_ratio", out.win_loss_ratio)?;
    d.set_item("recovery_factor", out.recovery_factor)?;
    Ok(d)
}

// ───────────────────────────── cascade_wire ─────────────────────────────
// Slice 2a (DEC-RUST-CASCADE-RESULTS-001 phase B): the cascade-manifest /
// wire-report serialisation + parse laws. Thin wrappers — the law bodies
// live in `cascade_wire.rs` (structural walks need `py`).

/// `wire_report_to_dict` — the aggregate-report JSON-native projection.
#[pyfunction]
fn wire_report_to_dict<'py>(
    py: Python<'py>,
    report: &Bound<'py, PyAny>,
    report_version: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyDict>> {
    cascade_wire::wire_report_to_dict(py, report, report_version)
}

/// `serialize_wire_report` — the deterministic report JSON bytes.
#[pyfunction]
fn serialize_wire_report<'py>(
    py: Python<'py>,
    report: &Bound<'py, PyAny>,
    report_version: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, pyo3::types::PyBytes>> {
    cascade_wire::serialize_wire_report(py, report, report_version)
}

/// `_spec_to_dict` — one spec's JSON-native projection (fragment shim-passed).
#[pyfunction]
fn spec_to_dict<'py>(
    py: Python<'py>,
    spec: &Bound<'py, PyAny>,
    fill_mode_token: &Bound<'py, PyAny>,
    fragment: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyDict>> {
    cascade_wire::spec_to_dict(py, spec, fill_mode_token, fragment)
}

/// `_stage3_to_dict` — one Stage-3 record's JSON-native projection.
#[pyfunction]
fn stage3_to_dict<'py>(
    py: Python<'py>,
    record: &Bound<'py, PyAny>,
    cell_fragments: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyDict>> {
    cascade_wire::stage3_to_dict(py, record, cell_fragments)
}

/// `_lbt_to_dict` — one leaderboard-trials record's JSON-native projection.
#[pyfunction]
fn lbt_to_dict<'py>(py: Python<'py>, record: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyDict>> {
    cascade_wire::lbt_to_dict(py, record)
}

/// `manifest_to_json` — the full manifest projection + deterministic render.
#[pyfunction]
fn manifest_to_json<'py>(
    py: Python<'py>,
    manifest: &Bound<'py, PyAny>,
    config_dict: &Bound<'py, PyAny>,
    fill_mode_token: &Bound<'py, PyAny>,
    spec_fragments: &Bound<'py, PyAny>,
    cell_fragments: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, pyo3::types::PyBytes>> {
    cascade_wire::manifest_to_json(
        py,
        manifest,
        config_dict,
        fill_mode_token,
        spec_fragments,
        cell_fragments,
    )
}

/// The DEC-FILL-WORST-CASE-ONLY-001 precise-era refusal gate.
#[pyfunction]
fn spec_fill_mode_gate(
    py: Python<'_>,
    data: &Bound<'_, PyAny>,
    worst_case_token: &str,
) -> PyResult<()> {
    cascade_wire::spec_fill_mode_gate(py, data, worst_case_token)
}

/// `_spec_from_dict`'s coercion walk (construction is the shim's).
#[pyfunction]
fn spec_coerce<'py>(py: Python<'py>, data: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyDict>> {
    cascade_wire::spec_coerce(py, data)
}

/// `_stage3_from_dict`'s parse walk — FULL per-cell construction (coercions,
/// the passed-in `FibAnchorKind`/`PriceSource` validations AND the passed-in
/// `Stage3CellRecord` construction, all before the next cell — fix round,
/// MINOR-1); the `Stage3ManifestRecord` construction is the shim's.
#[pyfunction]
fn stage3_coerce<'py>(
    py: Python<'py>,
    data: &Bound<'py, PyAny>,
    cell_cls: &Bound<'py, PyAny>,
    fib_cls: &Bound<'py, PyAny>,
    price_cls: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyDict>> {
    cascade_wire::stage3_coerce(py, data, cell_cls, fib_cls, price_cls)
}

/// `_lbt_from_dict`'s coercion walk (construction is the shim's).
#[pyfunction]
fn lbt_coerce<'py>(py: Python<'py>, data: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyDict>> {
    cascade_wire::lbt_coerce(py, data)
}

/// The FULL `_roll_schedules_from_payload` law (complete messages).
#[pyfunction]
fn roll_schedules_parse(
    py: Python<'_>,
    raw: &Bound<'_, PyAny>,
    futures_alias: &str,
) -> PyResult<Py<PyAny>> {
    cascade_wire::roll_schedules_parse(py, raw, futures_alias)
}

/// The manifest version gate (Python rich-compare `!=` — the bool pun law).
#[pyfunction]
fn manifest_version_gate(
    payload: &Bound<'_, PyAny>,
    manifest_version: &Bound<'_, PyAny>,
) -> PyResult<()> {
    cascade_wire::manifest_version_gate(payload, manifest_version)
}

/// The `bars_context` field coercion (real KeyError/TypeError propagate).
#[pyfunction]
fn bars_context_coerce<'py>(
    py: Python<'py>,
    payload: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyDict>> {
    cascade_wire::bars_context_coerce(py, payload)
}

/// `_finite` — None / non-finite → 0.0.
#[pyfunction]
fn finite_or_zero(value: Option<f64>) -> f64 {
    cascade_wire::finite_or_zero(value)
}

/// `_per_bar_returns` — per-bar equity change ÷ the notional base
/// (CPython-exact int true-division).
#[pyfunction]
fn per_bar_returns(
    py: Python<'_>,
    equity_paisa: Vec<i64>,
    start_equity_paisa: i64,
) -> PyResult<Vec<f64>> {
    cascade_wire::per_bar_returns(py, &equity_paisa, start_equity_paisa)
}

/// `_max_drawdown_return_units` — the peak/cum fold (Python NaN max law).
#[pyfunction]
fn max_drawdown_return_units(returns: Vec<f64>) -> f64 {
    cascade_wire::max_drawdown_return_units(&returns)
}

// ───────────────────────────── cascade_join ─────────────────────────────

/// Classify one Python value into the law's [`Leaf`] model.
fn classify(py: Python<'_>, v: &Bound<'_, PyAny>) -> Leaf {
    if v.is_none() {
        Leaf::None_
    } else if v.is_instance_of::<PyBool>() {
        match v.extract::<bool>() {
            Ok(b) => Leaf::Bool(b),
            Err(_) => Leaf::Other,
        }
    } else if v.is_instance_of::<PyInt>() {
        let exact = v.get_type().is(PyInt::type_object(py));
        match v.extract::<i128>() {
            Ok(i) => Leaf::Int { v: i, exact },
            // Beyond ±i128 — unreachable from any real report; classified
            // Other (documented divergence envelope, module docs).
            Err(_) => Leaf::Other,
        }
    } else if v.is_instance_of::<PyFloat>() {
        match v.extract::<f64>() {
            Ok(f) => Leaf::Float(f),
            Err(_) => Leaf::Other,
        }
    } else if v.is_instance_of::<PyString>() {
        match v.extract::<String>() {
            Ok(s) => Leaf::Str(s),
            // A lone-surrogate str — documented divergence envelope.
            Err(_) => Leaf::Other,
        }
    } else {
        Leaf::Other
    }
}

/// Capture a value's real CPython `repr()` (lossy only if the repr itself
/// is unrepresentable — CPython escapes unprintables, so this is total in
/// practice).
pub(crate) fn capture_repr(v: &Bound<'_, PyAny>) -> String {
    match v.repr() {
        Ok(r) => r.to_string_lossy().into_owned(),
        Err(_) => "<unrepresentable>".to_string(),
    }
}

/// `mapping.get(key)` via the bound method (subclass overrides honored,
/// exactly as the Python law's `payload.get(...)` calls).
pub(crate) fn mapping_get<'py>(obj: &Bound<'py, PyAny>, key: &str) -> PyResult<Bound<'py, PyAny>> {
    obj.call_method1("get", (key,))
}

/// Project one verdict entry.
fn project_verdict(py: Python<'_>, entry: &Bound<'_, PyAny>) -> PyResult<VerdictNode> {
    if !entry.is_instance_of::<PyDict>() {
        return Ok(VerdictNode::NotDict);
    }
    let cid = mapping_get(entry, "candidate_id")?;
    Ok(VerdictNode::V {
        candidate_id: LeafR::new(classify(py, &cid), capture_repr(&cid)),
        passed: classify(py, &mapping_get(entry, "passed")?),
        rejected_at: classify(py, &mapping_get(entry, "rejected_at")?),
        detail: classify(py, &mapping_get(entry, "detail")?),
        composite_score: classify(py, &mapping_get(entry, "composite_score")?),
        provisional: classify(py, &mapping_get(entry, "provisional")?),
        cpcv_pass_rate: classify(py, &mapping_get(entry, "cpcv_pass_rate")?),
    })
}

/// Project one group entry.
fn project_group(py: Python<'_>, group: &Bound<'_, PyAny>) -> PyResult<GroupNode> {
    if !group.is_instance_of::<PyDict>() {
        return Ok(GroupNode::NotDict);
    }
    let instrument = classify(py, &mapping_get(group, "instrument")?);
    let tf = classify(py, &mapping_get(group, "tf")?);
    let report_obj = mapping_get(group, "report")?;
    if !report_obj.is_instance_of::<PyDict>() {
        return Ok(GroupNode::G {
            instrument,
            tf,
            report: ReportNode::NotDict,
        });
    }
    let verdicts_raw = mapping_get(&report_obj, "verdicts")?;
    let verdicts = if verdicts_raw.is_instance_of::<PyList>() {
        let mut out = Vec::new();
        for entry in verdicts_raw.try_iter()? {
            out.push(project_verdict(py, &entry?)?);
        }
        Some(out)
    } else {
        None
    };
    let survivors_raw = mapping_get(&report_obj, "survivor_ids")?;
    let survivor_ids = if survivors_raw.is_instance_of::<PyList>() {
        let mut out = Vec::new();
        for sid in survivors_raw.try_iter()? {
            let sid = sid?;
            out.push(LeafR::new(classify(py, &sid), capture_repr(&sid)));
        }
        Some(out)
    } else {
        None
    };
    Ok(GroupNode::G {
        instrument,
        tf,
        report: ReportNode::R {
            verdicts,
            n_input: classify(py, &mapping_get(&report_obj, "n_input")?),
            n_survived: classify(py, &mapping_get(&report_obj, "n_survived")?),
            pool_pbo: classify(py, &mapping_get(&report_obj, "pool_pbo")?),
            dsr_n_source: classify(py, &mapping_get(&report_obj, "dsr_n_source")?),
            dsr_n_trials: classify(py, &mapping_get(&report_obj, "dsr_n_trials")?),
            dsr_trial_variance: classify(py, &mapping_get(&report_obj, "dsr_trial_variance")?),
            survivor_ids,
        },
    })
}

/// `build_cascade_join`'s full law over a raw Python payload. Structural
/// refusals raise `ValueError` (the shim re-raises `CascadeJoinError`);
/// success returns the join projection + the WARN inputs as a dict.
#[pyfunction]
fn build_cascade_join<'py>(
    py: Python<'py>,
    payload: &Bound<'py, PyAny>,
    report_version: i128,
) -> PyResult<Bound<'py, PyDict>> {
    let abc = py.import("collections.abc")?;
    let mapping_cls = abc.getattr("Mapping")?;
    let is_mapping = payload.is_instance(&mapping_cls)?;
    let root = if is_mapping {
        let version_obj = mapping_get(payload, "version")?;
        let groups_raw = mapping_get(payload, "groups")?;
        let groups = if groups_raw.is_instance_of::<PyList>() {
            let mut out = Vec::new();
            for group in groups_raw.try_iter()? {
                out.push(project_group(py, &group?)?);
            }
            Some(out)
        } else {
            None
        };
        RootView {
            is_mapping: true,
            version: LeafR::new(classify(py, &version_obj), capture_repr(&version_obj)),
            sweep_id: classify(py, &mapping_get(payload, "sweep_id")?),
            groups,
            n_input: classify(py, &mapping_get(payload, "n_input")?),
            n_survived: classify(py, &mapping_get(payload, "n_survived")?),
        }
    } else {
        RootView {
            is_mapping: false,
            version: LeafR::new(Leaf::None_, "None"),
            sweep_id: Leaf::None_,
            groups: None,
            n_input: Leaf::None_,
            n_survived: Leaf::None_,
        }
    };
    let join = cascade_join::build_join(&root, report_version).map_err(PyValueError::new_err)?;
    let d = PyDict::new(py);
    d.set_item("sweep_id", join.sweep_id)?;
    d.set_item("version", join.version)?;
    d.set_item("n_input", join.n_input)?;
    d.set_item("n_survived", join.n_survived)?;
    let views = PyList::empty(py);
    for view in &join.views {
        let v = PyDict::new(py);
        v.set_item("candidate_id", &view.candidate_id)?;
        v.set_item("instrument", &view.instrument)?;
        v.set_item("tf", &view.tf)?;
        v.set_item("passed", view.passed)?;
        v.set_item("rejected_at", view.rejected_at.as_deref())?;
        v.set_item("detail", &view.detail)?;
        v.set_item("cascade_composite", view.cascade_composite)?;
        v.set_item("provisional", view.provisional)?;
        v.set_item("cpcv_pass_rate", view.cpcv_pass_rate)?;
        v.set_item("survivor_rank", view.survivor_rank)?;
        views.append(v)?;
    }
    d.set_item("views", views)?;
    d.set_item("ambiguous_ids", join.ambiguous_ids)?;
    let groups = PyList::empty(py);
    for g in &join.groups {
        let item = PyDict::new(py);
        item.set_item("instrument", &g.instrument)?;
        item.set_item("tf", &g.tf)?;
        item.set_item("n_input", g.n_input)?;
        item.set_item("n_survived", g.n_survived)?;
        item.set_item("pool_pbo", g.pool_pbo)?;
        item.set_item("dsr_n_source", &g.dsr_n_source)?;
        item.set_item("dsr_n_trials", g.dsr_n_trials)?;
        item.set_item("dsr_trial_variance", g.dsr_trial_variance)?;
        groups.append(item)?;
    }
    d.set_item("groups", groups)?;
    d.set_item("skipped_groups", join.skipped_groups)?;
    d.set_item("skipped_verdicts", join.skipped_verdicts)?;
    d.set_item("nulled_values", join.nulled_values)?;
    d.set_item("rank_inconsistencies", join.rank_inconsistencies)?;
    d.set_item("notes", join.notes)?;
    d.set_item("warn", join.warn)?;
    Ok(d)
}

/// The `_brutex_cascade` extension module.
#[pymodule]
fn _brutex_cascade(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(version, m)?)?;
    m.add_class::<MinuteIndexRs>()?;
    m.add_function(wrap_pyfunction!(session_equity_series, m)?)?;
    m.add_function(wrap_pyfunction!(minute_path_max_drawdown, m)?)?;
    m.add_function(wrap_pyfunction!(ist_session_dates, m)?)?;
    m.add_function(wrap_pyfunction!(nearest_rank_index, m)?)?;
    m.add_function(wrap_pyfunction!(float_stats, m)?)?;
    m.add_function(wrap_pyfunction!(aggregate_fold, m)?)?;
    m.add_function(wrap_pyfunction!(build_cascade_join, m)?)?;
    m.add_function(wrap_pyfunction!(wire_report_to_dict, m)?)?;
    m.add_function(wrap_pyfunction!(serialize_wire_report, m)?)?;
    m.add_function(wrap_pyfunction!(spec_to_dict, m)?)?;
    m.add_function(wrap_pyfunction!(stage3_to_dict, m)?)?;
    m.add_function(wrap_pyfunction!(lbt_to_dict, m)?)?;
    m.add_function(wrap_pyfunction!(manifest_to_json, m)?)?;
    m.add_function(wrap_pyfunction!(spec_fill_mode_gate, m)?)?;
    m.add_function(wrap_pyfunction!(spec_coerce, m)?)?;
    m.add_function(wrap_pyfunction!(stage3_coerce, m)?)?;
    m.add_function(wrap_pyfunction!(lbt_coerce, m)?)?;
    m.add_function(wrap_pyfunction!(roll_schedules_parse, m)?)?;
    m.add_function(wrap_pyfunction!(manifest_version_gate, m)?)?;
    m.add_function(wrap_pyfunction!(bars_context_coerce, m)?)?;
    m.add_function(wrap_pyfunction!(finite_or_zero, m)?)?;
    m.add_function(wrap_pyfunction!(per_bar_returns, m)?)?;
    m.add_function(wrap_pyfunction!(max_drawdown_return_units, m)?)?;
    // The crate's hardcoded walker exit-reason vocabulary, exposed so the
    // Python drift pin can assert set AND order equality against
    // `trade_sim.EXIT_REASONS` (DEC-RUST-CASCADE-RESULTS-001 round-2 MINOR:
    // a future 7th reason must fail loudly at TEST time, not flip the
    // tolerant tally into a runtime KeyError).
    m.add("EXIT_REASONS", combo_aggregates::EXIT_REASONS)?;
    Ok(())
}
