//! The `brutex.engine.cascade_wire` serialisation + parse LAW family
//! (DEC-RUST-CASCADE-RESULTS-001 phase B, slice 2a).
//!
//! Ported token-for-token from the DELETED Python law bodies and verified
//! against the FROZEN pre-conversion parity corpus
//! (`tests/golden/fixtures/cascade_rust_parity/cascade_wire.json`).
//!
//! ## Serialise side
//!
//! `getattr`-walks over the Python dataclasses (`CascadeWireReport` /
//! `CascadeManifest` / `CascadeTupleSpec` / `Stage3ManifestRecord` /
//! `LeaderboardTrialsRecord`); every value is re-used as a PyObject (ints
//! unbounded-exact, floats bit-exact) and rendered through
//! [`crate::pyjson::dumps_sorted`] — the CPython
//! `json.dumps(sort_keys=True, separators=(",", ":"), allow_nan=False)`
//! byte twin. The §11-COMMON inputs whose laws live elsewhere are computed
//! by the SHIM and passed in: the `FillMode.WORST_CASE.value` token
//! (`brutex.execution.fill_mode`), the per-spec/per-cell
//! `price_source_params_fragment` dicts (`brutex.strategies.types`) and the
//! `dataclasses.asdict(config)` dict (`brutex.metrics.cascade`).
//!
//! ## Parse side
//!
//! The STRUCTURAL walk lives here; every COERCION is delegated to CPython's
//! own builtins (`int()` / `str()` / `float()` / `bool()` / `list()` /
//! `dict()` called via `py.import("builtins")`), so semantics AND error
//! messages are CPython's exactly (`int("abc")`, the 40-digit int string —
//! valid + unbounded). Mapping access uses generic `PyObject_GetItem` /
//! the bound `.get` method, so the REAL `KeyError` / `TypeError` /
//! `IndexError` / `AttributeError` escape with CPython's exact messages —
//! the frozen law includes RAW escapes (a 2-element `proximity_bps` list →
//! `IndexError`; a non-dict spec entry → `AttributeError` from `.get`).
//! Tuple-unpack is EMULATED with CPython's verbatim messages
//! (`cannot unpack non-iterable {name} object` /
//! `not enough values to unpack (expected 2, got {n})` /
//! `too many values to unpack (expected 2)`); a 2-char str row unpacks like
//! Python (str is iterable), a dict row iterates its keys.
//!
//! The shim keeps the per-family `(KeyError, TypeError, ValueError)` →
//! `CascadeWireError` prefix guards VERBATIM; only the messages the Python
//! law composed COMPLETELY are composed here (the
//! DEC-FILL-WORST-CASE-ONLY-001 precise-era refusal, the
//! `invalid roll_schedules in manifest: {exc}` family, the
//! `unsupported cascade manifest version {v!r}` gate) — raised as
//! `ValueError` for the shim to re-raise as `CascadeWireError` verbatim.
//!
//! ## Math trio
//!
//! * [`finite_or_zero`] — `None`/non-finite → `0.0` (`-0.0` passes through);
//! * [`per_bar_returns`] — `(value − prev) / start` with the division
//!   delegated to CPython int true-division (`int.__truediv__`) so the
//!   beyond-2^53 numerators round EXACTLY as Python's correctly-rounded
//!   exact-int division (the frozen `big_paisa` case refutes a naive
//!   f64-cast division: `-9000000000000001/10**12` must be
//!   `-9000.000000000002`, not `-9000.0`); equity values beyond ±i64
//!   refuse loudly at extraction (the disclosed divergence envelope);
//! * [`max_drawdown_return_units`] — the peak/cum fold with Python's
//!   `max(a, b)` first-arg-on-unordered NaN semantics replicated exactly
//!   (`if b > a { b } else { a }` for BOTH folds).
//!
//! Divergence envelope (each loud, none reachable from real manifests):
//! roll-schedule ordinals beyond ±i128 refuse (`OverflowError`);
//! lone-surrogate strings fail UTF-8 extraction; non-str JSON dict keys
//! refuse in `dumps_sorted`.

use pyo3::exceptions::{PyOverflowError, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::pyclass::CompareOp;
use pyo3::types::{PyBool, PyBytes, PyDict, PyInt, PyList, PyTuple};

use crate::pyjson::dumps_sorted;
use crate::{capture_repr, mapping_get};

// ───────────────────────────── helpers ─────────────────────────────

/// The CPython builtins coercers, bound once per FFI entry.
struct Builtins<'py> {
    int_f: Bound<'py, PyAny>,
    str_f: Bound<'py, PyAny>,
    float_f: Bound<'py, PyAny>,
    bool_f: Bound<'py, PyAny>,
    list_f: Bound<'py, PyAny>,
    dict_f: Bound<'py, PyAny>,
}

impl<'py> Builtins<'py> {
    fn new(py: Python<'py>) -> PyResult<Self> {
        let b = py.import("builtins")?;
        Ok(Self {
            int_f: b.getattr("int")?,
            str_f: b.getattr("str")?,
            float_f: b.getattr("float")?,
            bool_f: b.getattr("bool")?,
            list_f: b.getattr("list")?,
            dict_f: b.getattr("dict")?,
        })
    }

    fn int(&self, v: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        self.int_f.call1((v,))
    }

    fn str_(&self, v: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        self.str_f.call1((v,))
    }

    fn float(&self, v: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        self.float_f.call1((v,))
    }

    fn bool_(&self, v: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        self.bool_f.call1((v,))
    }

    fn list(&self, v: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        self.list_f.call1((v,))
    }

    fn dict(&self, v: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        self.dict_f.call1((v,))
    }
}

/// CPython's `not enough values to unpack` message (2-target unpack).
fn unpack_short_msg(got: usize) -> String {
    format!("not enough values to unpack (expected 2, got {got})")
}

/// CPython's `cannot unpack non-iterable` message (2-target unpack).
fn unpack_non_iterable_msg(type_name: &str) -> String {
    format!("cannot unpack non-iterable {type_name} object")
}

/// Emulate CPython's 2-target unpack `a, b = row` with its VERBATIM
/// error messages. A str row iterates chars, a dict row iterates keys
/// (`try_iter` == `iter()`); a raising third `next()` propagates raw,
/// exactly as CPython's `UNPACK_SEQUENCE` protocol does.
fn unpack_pair<'py>(row: &Bound<'py, PyAny>) -> PyResult<(Bound<'py, PyAny>, Bound<'py, PyAny>)> {
    let mut iter = match row.try_iter() {
        Ok(iter) => iter,
        Err(_) => {
            let name = row.get_type().name()?.to_cow()?.into_owned();
            return Err(PyTypeError::new_err(unpack_non_iterable_msg(&name)));
        }
    };
    let first = match iter.next() {
        Some(item) => item?,
        None => return Err(PyValueError::new_err(unpack_short_msg(0))),
    };
    let second = match iter.next() {
        Some(item) => item?,
        None => return Err(PyValueError::new_err(unpack_short_msg(1))),
    };
    match iter.next() {
        None => Ok((first, second)),
        Some(Err(err)) => Err(err),
        Some(Ok(_)) => Err(PyValueError::new_err(
            "too many values to unpack (expected 2)",
        )),
    }
}

// ───────────────────────────── serialise side ─────────────────────────────

/// `_report_to_dict` — one `CascadeReport` → its JSON-native dict.
fn report_to_dict<'py>(
    py: Python<'py>,
    report: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyDict>> {
    let b = Builtins::new(py)?;
    let d = PyDict::new(py);
    d.set_item("n_input", report.getattr("n_input")?)?;
    d.set_item("n_survived", report.getattr("n_survived")?)?;
    d.set_item("dsr_n_source", report.getattr("dsr_n_source")?)?;
    d.set_item("dsr_n_trials", report.getattr("dsr_n_trials")?)?;
    d.set_item("dsr_trial_variance", report.getattr("dsr_trial_variance")?)?;
    d.set_item("pool_pbo", report.getattr("pool_pbo")?)?;
    d.set_item("survivor_ids", b.list(&report.getattr("survivor_ids")?)?)?;
    d.set_item(
        "stage_survivors",
        b.dict(&report.getattr("stage_survivors")?)?,
    )?;
    let verdicts = PyList::empty(py);
    for verdict in report.getattr("verdicts")?.try_iter()? {
        let verdict = verdict?;
        let vd = PyDict::new(py);
        vd.set_item("candidate_id", verdict.getattr("candidate_id")?)?;
        vd.set_item("passed", verdict.getattr("passed")?)?;
        let rejected_at = verdict.getattr("rejected_at")?;
        let rejected_val = if rejected_at.is_none() {
            rejected_at
        } else {
            rejected_at.getattr("value")?
        };
        vd.set_item("rejected_at", rejected_val)?;
        vd.set_item("detail", verdict.getattr("detail")?)?;
        vd.set_item("composite_score", verdict.getattr("composite_score")?)?;
        vd.set_item("provisional", verdict.getattr("provisional")?)?;
        vd.set_item("cpcv_pass_rate", verdict.getattr("cpcv_pass_rate")?)?;
        verdicts.append(vd)?;
    }
    d.set_item("verdicts", verdicts)?;
    Ok(d)
}

/// `wire_report_to_dict` — the aggregate report projection.
pub fn wire_report_to_dict<'py>(
    py: Python<'py>,
    report: &Bound<'py, PyAny>,
    report_version: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyDict>> {
    let d = PyDict::new(py);
    d.set_item("version", report_version)?;
    d.set_item("sweep_id", report.getattr("sweep_id")?)?;
    d.set_item("n_input", report.getattr("n_input")?)?;
    d.set_item("n_survived", report.getattr("n_survived")?)?;
    let groups = PyList::empty(py);
    for group in report.getattr("groups")?.try_iter()? {
        let group = group?;
        let gd = PyDict::new(py);
        gd.set_item("instrument", group.get_item(0)?)?;
        gd.set_item("tf", group.get_item(1)?)?;
        gd.set_item("report", report_to_dict(py, &group.get_item(2)?)?)?;
        groups.append(gd)?;
    }
    d.set_item("groups", groups)?;
    Ok(d)
}

/// `serialize_wire_report` — deterministic JSON bytes (sorted keys,
/// compact separators, `allow_nan=False` — the byte-identity surface).
pub fn serialize_wire_report<'py>(
    py: Python<'py>,
    report: &Bound<'py, PyAny>,
    report_version: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyBytes>> {
    let payload = wire_report_to_dict(py, report, report_version)?;
    let rendered = dumps_sorted(payload.as_any())?;
    Ok(PyBytes::new(py, rendered.as_bytes()))
}

/// `_spec_to_dict` — one `CascadeTupleSpec` → its JSON-native dict.
/// *fill_mode_token* and *fragment* are the shim's §11-COMMON inputs.
pub fn spec_to_dict<'py>(
    py: Python<'py>,
    spec: &Bound<'py, PyAny>,
    fill_mode_token: &Bound<'py, PyAny>,
    fragment: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyDict>> {
    let b = Builtins::new(py)?;
    let d = PyDict::new(py);
    d.set_item("instrument", spec.getattr("instrument")?)?;
    d.set_item("tf_label", spec.getattr("tf_label")?)?;
    d.set_item("tf_minutes", spec.getattr("tf_minutes")?)?;
    d.set_item("direction_label", spec.getattr("direction_label")?)?;
    d.set_item("sl_paisa", spec.getattr("sl_paisa")?)?;
    d.set_item("tp_paisa", spec.getattr("tp_paisa")?)?;
    d.set_item("prior_n", spec.getattr("prior_n")?)?;
    let proximity = spec.getattr("proximity_bps")?;
    let proximity_val = if proximity.is_none() {
        proximity
    } else {
        b.list(&proximity)?
    };
    d.set_item("proximity_bps", proximity_val)?;
    d.set_item("start_equity_paisa", spec.getattr("start_equity_paisa")?)?;
    let winners = PyList::empty(py);
    for pair in spec.getattr("winners")?.try_iter()? {
        let pair = pair?;
        let row = PyList::empty(py);
        row.append(pair.get_item(0)?)?;
        row.append(pair.get_item(1)?)?;
        winners.append(row)?;
    }
    d.set_item("winners", winners)?;
    d.set_item("fib_anchor", spec.getattr("fib_anchor")?.getattr("value")?)?;
    // DEC-FILL-WORST-CASE-ONLY-001: the UNCONDITIONAL "fill_mode" token —
    // the ONE literal home is `execution.fill_mode` (shim-passed).
    d.set_item("fill_mode", fill_mode_token)?;
    // DEFAULT-OMITTED price-source keys — the ONE COMMON fragment
    // (`strategies.types.price_source_params_fragment`, shim-computed).
    for (key, value) in fragment.cast::<PyDict>()?.iter() {
        d.set_item(key, value)?;
    }
    Ok(d)
}

/// One Stage-3 refined cell → its JSON-native dict (the `_stage3_to_dict`
/// per-cell body).
fn stage3_cell_to_dict<'py>(
    py: Python<'py>,
    cell: &Bound<'py, PyAny>,
    fragment: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyDict>> {
    let b = Builtins::new(py)?;
    let d = PyDict::new(py);
    d.set_item("instrument", cell.getattr("instrument")?)?;
    d.set_item("tf_label", cell.getattr("tf_label")?)?;
    d.set_item("direction_label", cell.getattr("direction_label")?)?;
    d.set_item("k", cell.getattr("k")?)?;
    d.set_item("sl_paisa", cell.getattr("sl_paisa")?)?;
    d.set_item("tp_paisa", cell.getattr("tp_paisa")?)?;
    let proximity = cell.getattr("proximity_bps")?;
    let proximity_val = if proximity.is_none() {
        proximity
    } else {
        b.list(&proximity)?
    };
    d.set_item("proximity_bps", proximity_val)?;
    d.set_item("rel_path", cell.getattr("rel_path")?)?;
    d.set_item("fib_anchor", cell.getattr("fib_anchor")?.getattr("value")?)?;
    for (key, value) in fragment.cast::<PyDict>()?.iter() {
        d.set_item(key, value)?;
    }
    Ok(d)
}

/// `_stage3_to_dict` — one `Stage3ManifestRecord` → its JSON-native dict.
/// *cell_fragments* is the shim-computed per-cell fragment list.
pub fn stage3_to_dict<'py>(
    py: Python<'py>,
    record: &Bound<'py, PyAny>,
    cell_fragments: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyDict>> {
    let cells = record.getattr("refined_cells")?;
    if cell_fragments.len()? != cells.len()? {
        // Defensive only — the shim derives the fragments from the SAME
        // record, so a mismatch is a caller bug, never a manifest shape.
        return Err(PyValueError::new_err(
            "stage3_to_dict: cell_fragments length != refined_cells length",
        ));
    }
    let d = PyDict::new(py);
    d.set_item("enabled", record.getattr("enabled")?)?;
    d.set_item(
        "train_snapshot_hash",
        record.getattr("train_snapshot_hash")?,
    )?;
    let out_cells = PyList::empty(py);
    for (i, cell) in cells.try_iter()?.enumerate() {
        let cell = cell?;
        let fragment = cell_fragments.get_item(i)?;
        out_cells.append(stage3_cell_to_dict(py, &cell, &fragment)?)?;
    }
    d.set_item("refined_cells", out_cells)?;
    Ok(d)
}

/// `_lbt_to_dict` — one `LeaderboardTrialsRecord` → its JSON-native dict.
pub fn lbt_to_dict<'py>(
    py: Python<'py>,
    record: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyDict>> {
    let d = PyDict::new(py);
    d.set_item("instrument", record.getattr("instrument")?)?;
    d.set_item("tf_label", record.getattr("tf_label")?)?;
    d.set_item("n_trials", record.getattr("n_trials")?)?;
    d.set_item("n_finite", record.getattr("n_finite")?)?;
    d.set_item("sum_sharpe", record.getattr("sum_sharpe")?)?;
    d.set_item("sum_sharpe_sq", record.getattr("sum_sharpe_sq")?)?;
    d.set_item("complete", record.getattr("complete")?)?;
    Ok(d)
}

/// `manifest_to_json` — the full manifest payload projection + render.
/// *config_dict* / *fill_mode_token* / *spec_fragments* / *cell_fragments*
/// are the shim's §11-COMMON inputs (their laws live out-of-crate).
pub fn manifest_to_json<'py>(
    py: Python<'py>,
    manifest: &Bound<'py, PyAny>,
    config_dict: &Bound<'py, PyAny>,
    fill_mode_token: &Bound<'py, PyAny>,
    spec_fragments: &Bound<'py, PyAny>,
    cell_fragments: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyBytes>> {
    let b = Builtins::new(py)?;
    let payload = PyDict::new(py);
    payload.set_item("version", manifest.getattr("version")?)?;
    payload.set_item("sweep_id", manifest.getattr("sweep_id")?)?;
    payload.set_item("cascade_config", config_dict)?;
    let bars = manifest.getattr("bars_context")?;
    let bars_dict = PyDict::new(py);
    bars_dict.set_item("exchange", bars.getattr("exchange")?)?;
    bars_dict.set_item("segment", bars.getattr("segment")?)?;
    bars_dict.set_item("start", bars.getattr("start")?)?;
    bars_dict.set_item("end", bars.getattr("end")?)?;
    payload.set_item("bars_context", bars_dict)?;
    let specs = manifest.getattr("specs")?;
    if spec_fragments.len()? != specs.len()? {
        // Defensive only — the shim derives the fragments from the SAME
        // manifest's specs (caller bug, never a manifest shape).
        return Err(PyValueError::new_err(
            "manifest_to_json: spec_fragments length != specs length",
        ));
    }
    let out_specs = PyList::empty(py);
    for (i, spec) in specs.try_iter()?.enumerate() {
        let spec = spec?;
        let fragment = spec_fragments.get_item(i)?;
        out_specs.append(spec_to_dict(py, &spec, fill_mode_token, &fragment)?)?;
    }
    payload.set_item("specs", out_specs)?;
    let stage3 = manifest.getattr("stage3")?;
    let stage3_val = if stage3.is_none() {
        stage3
    } else {
        stage3_to_dict(py, &stage3, cell_fragments)?.into_any()
    };
    payload.set_item("stage3", stage3_val)?;
    let lbt = manifest.getattr("leaderboard_trials")?;
    let lbt_val = if lbt.is_none() {
        lbt
    } else {
        let records = PyList::empty(py);
        for record in lbt.try_iter()? {
            records.append(lbt_to_dict(py, &record?)?)?;
        }
        records.into_any()
    };
    payload.set_item("leaderboard_trials", lbt_val)?;
    let rolls = manifest.getattr("roll_schedules")?;
    let rolls_val = if rolls.is_none() {
        rolls
    } else {
        let rows = PyList::empty(py);
        for pair in rolls.try_iter()? {
            let pair = pair?;
            let row = PyList::empty(py);
            row.append(pair.get_item(0)?)?;
            row.append(b.list(&pair.get_item(1)?)?)?;
            rows.append(row)?;
        }
        rows.into_any()
    };
    payload.set_item("roll_schedules", rolls_val)?;
    let rendered = dumps_sorted(payload.as_any())?;
    Ok(PyBytes::new(py, rendered.as_bytes()))
}

// ───────────────────────────── parse side ─────────────────────────────

/// The DEC-FILL-WORST-CASE-ONLY-001 precise-era refusal gate. The
/// `data.get("fill_mode")` access is the BOUND `.get` method — a non-dict
/// spec entry raises CPython's REAL `AttributeError`
/// (`'str' object has no attribute 'get'`), which propagates raw through
/// the shim (the frozen law). A refusal raises `ValueError` with the
/// COMPLETE message; the shim re-raises it as `CascadeWireError` verbatim
/// (no prefix — the original was a direct raise).
pub fn spec_fill_mode_gate(
    py: Python<'_>,
    data: &Bound<'_, PyAny>,
    worst_case_token: &str,
) -> PyResult<()> {
    let fm = mapping_get(data, "fill_mode")?;
    let refuse = if fm.is_none() {
        true
    } else {
        let b = Builtins::new(py)?;
        let coerced = b.str_(&data.get_item("fill_mode")?)?;
        coerced.extract::<String>()? != worst_case_token
    };
    if refuse {
        return Err(PyValueError::new_err(format!(
            "precise-era cascade manifest refused (DEC-FILL-WORST-CASE-ONLY-001): \
             fill_mode={} — worst_case is the only fill model; re-run the sweep to \
             produce a worst-case manifest",
            capture_repr(&fm)
        )));
    }
    Ok(())
}

/// The `(int(proximity[0]), int(proximity[1]), int(proximity[2]))` triple —
/// element getitem + `int()` in ORDER, so a 2-element list raises the RAW
/// `IndexError` at `proximity[2]` exactly as the Python law did.
fn proximity_triple<'py>(
    b: &Builtins<'py>,
    py: Python<'py>,
    proximity: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let p0 = b.int(&proximity.get_item(0)?)?;
    let p1 = b.int(&proximity.get_item(1)?)?;
    let p2 = b.int(&proximity.get_item(2)?)?;
    Ok(PyTuple::new(py, [p0, p1, p2])?.into_any())
}

/// `_spec_from_dict`'s coercion walk (the fill gate runs FIRST, separately;
/// enum/dataclass construction is the shim's). Coercions run in the
/// original's exact token order: proximity triple → winners → the
/// construction kwargs (instrument … start_equity) → fib/vwap/ema tokens
/// (str-coerced, or None-for-absent — the shim maps None → default enums).
pub fn spec_coerce<'py>(py: Python<'py>, data: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyDict>> {
    let b = Builtins::new(py)?;
    let out = PyDict::new(py);
    let proximity = data.get_item("proximity_bps")?;
    let proximity_val = if proximity.is_none() {
        proximity
    } else {
        proximity_triple(&b, py, &proximity)?
    };
    let mut winner_rows: Vec<Bound<'py, PyAny>> = Vec::new();
    for row in data.get_item("winners")?.try_iter()? {
        let row = row?;
        let (run_id, mask) = unpack_pair(&row)?;
        let pair = PyTuple::new(py, [b.str_(&run_id)?, b.int(&mask)?])?;
        winner_rows.push(pair.into_any());
    }
    let winners = PyTuple::new(py, winner_rows)?;
    out.set_item("instrument", b.str_(&data.get_item("instrument")?)?)?;
    out.set_item("tf_label", b.str_(&data.get_item("tf_label")?)?)?;
    out.set_item("tf_minutes", b.int(&data.get_item("tf_minutes")?)?)?;
    out.set_item(
        "direction_label",
        b.str_(&data.get_item("direction_label")?)?,
    )?;
    let sl = data.get_item("sl_paisa")?;
    out.set_item(
        "sl_paisa",
        if sl.is_none() {
            sl
        } else {
            b.int(&data.get_item("sl_paisa")?)?
        },
    )?;
    let tp = data.get_item("tp_paisa")?;
    out.set_item(
        "tp_paisa",
        if tp.is_none() {
            tp
        } else {
            b.int(&data.get_item("tp_paisa")?)?
        },
    )?;
    out.set_item("prior_n", b.int(&data.get_item("prior_n")?)?)?;
    out.set_item("proximity_bps", proximity_val)?;
    out.set_item(
        "start_equity_paisa",
        b.int(&data.get_item("start_equity_paisa")?)?,
    )?;
    out.set_item("winners", winners)?;
    let fib = mapping_get(data, "fib_anchor")?;
    out.set_item(
        "fib_anchor",
        if fib.is_none() {
            fib
        } else {
            b.str_(&data.get_item("fib_anchor")?)?
        },
    )?;
    let vwap = mapping_get(data, "vwap_source")?;
    out.set_item(
        "vwap_source",
        if vwap.is_none() {
            vwap
        } else {
            b.str_(&data.get_item("vwap_source")?)?
        },
    )?;
    let ema = mapping_get(data, "ema_source")?;
    out.set_item(
        "ema_source",
        if ema.is_none() {
            ema
        } else {
            b.str_(&data.get_item("ema_source")?)?
        },
    )?;
    Ok(out)
}

/// `_stage3_from_dict`'s coercion walk (dataclass construction is the
/// shim's). Per-cell coercions run in the original's exact token order
/// (the `proximity_bps` getitem FIRST, then the construction kwargs);
/// `train_snapshot_hash` / `enabled` follow the original's post-loop
/// evaluation order.
pub fn stage3_coerce<'py>(
    py: Python<'py>,
    data: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyDict>> {
    let b = Builtins::new(py)?;
    let cells = PyList::empty(py);
    for cell in data.get_item("refined_cells")?.try_iter()? {
        let cell = cell?;
        let cd = PyDict::new(py);
        let proximity = cell.get_item("proximity_bps")?;
        cd.set_item("instrument", b.str_(&cell.get_item("instrument")?)?)?;
        cd.set_item("tf_label", b.str_(&cell.get_item("tf_label")?)?)?;
        cd.set_item(
            "direction_label",
            b.str_(&cell.get_item("direction_label")?)?,
        )?;
        cd.set_item("k", b.int(&cell.get_item("k")?)?)?;
        cd.set_item("sl_paisa", b.int(&cell.get_item("sl_paisa")?)?)?;
        cd.set_item("tp_paisa", b.int(&cell.get_item("tp_paisa")?)?)?;
        let proximity_val = if proximity.is_none() {
            proximity
        } else {
            proximity_triple(&b, py, &proximity)?
        };
        cd.set_item("proximity_bps", proximity_val)?;
        let rel = mapping_get(&cell, "rel_path")?;
        cd.set_item(
            "rel_path",
            if rel.is_none() {
                rel
            } else {
                b.str_(&cell.get_item("rel_path")?)?
            },
        )?;
        let fib = mapping_get(&cell, "fib_anchor")?;
        cd.set_item(
            "fib_anchor",
            if fib.is_none() {
                fib
            } else {
                b.str_(&cell.get_item("fib_anchor")?)?
            },
        )?;
        let vwap = mapping_get(&cell, "vwap_source")?;
        cd.set_item(
            "vwap_source",
            if vwap.is_none() {
                vwap
            } else {
                b.str_(&cell.get_item("vwap_source")?)?
            },
        )?;
        let ema = mapping_get(&cell, "ema_source")?;
        cd.set_item(
            "ema_source",
            if ema.is_none() {
                ema
            } else {
                b.str_(&cell.get_item("ema_source")?)?
            },
        )?;
        cells.append(cd)?;
    }
    let hash_raw = data.get_item("train_snapshot_hash")?;
    let enabled = b.bool_(&data.get_item("enabled")?)?;
    let hash_val = if hash_raw.is_none() {
        hash_raw
    } else {
        b.str_(&hash_raw)?
    };
    let out = PyDict::new(py);
    out.set_item("cells", cells)?;
    out.set_item("enabled", enabled)?;
    out.set_item("train_snapshot_hash", hash_val)?;
    Ok(out)
}

/// `_lbt_from_dict`'s coercion walk (dataclass construction is the shim's).
pub fn lbt_coerce<'py>(py: Python<'py>, data: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyDict>> {
    let b = Builtins::new(py)?;
    let out = PyDict::new(py);
    out.set_item("instrument", b.str_(&data.get_item("instrument")?)?)?;
    out.set_item("tf_label", b.str_(&data.get_item("tf_label")?)?)?;
    out.set_item("n_trials", b.int(&data.get_item("n_trials")?)?)?;
    out.set_item("n_finite", b.int(&data.get_item("n_finite")?)?)?;
    out.set_item("sum_sharpe", b.float(&data.get_item("sum_sharpe")?)?)?;
    out.set_item("sum_sharpe_sq", b.float(&data.get_item("sum_sharpe_sq")?)?)?;
    out.set_item("complete", b.bool_(&data.get_item("complete")?)?)?;
    Ok(out)
}

/// One parsed roll row: the str-coerced instrument (object + key) + the
/// strict-int ordinals.
struct RollRow<'py> {
    instrument_obj: Bound<'py, PyAny>,
    instrument: String,
    schedule: Vec<i128>,
}

/// The parse phase of `_roll_schedules_from_payload` — the inner
/// `tuple(...)` generator whose `(TypeError, ValueError)` failures the
/// caller composes into `invalid roll_schedules in manifest: {exc}`.
fn roll_parse_phase<'py>(py: Python<'py>, raw: &Bound<'py, PyAny>) -> PyResult<Vec<RollRow<'py>>> {
    let b = Builtins::new(py)?;
    let mut rows: Vec<RollRow<'py>> = Vec::new();
    for pair in raw.try_iter()? {
        let pair = pair?;
        let (instrument, schedule) = unpack_pair(&pair)?;
        let instrument_obj = b.str_(&instrument)?;
        let instrument_key = instrument_obj.extract::<String>()?;
        let mut ordinals: Vec<i128> = Vec::new();
        for ordinal in schedule.try_iter()? {
            let ordinal = ordinal?;
            // Strict int-not-bool (the audit-reader mirror): bool is checked
            // FIRST (PyBool ⊂ PyInt), then genuine-int isinstance.
            if ordinal.is_instance_of::<PyBool>() || !ordinal.is_instance_of::<PyInt>() {
                return Err(PyValueError::new_err(format!(
                    "non-int roll_schedule ordinal {}",
                    capture_repr(&ordinal)
                )));
            }
            let value = ordinal.extract::<i128>().map_err(|_| {
                // Beyond ±i128 — the disclosed loud divergence envelope
                // (Python's unbounded int would carry it; no real day
                // ordinal approaches 1.7e38).
                PyOverflowError::new_err(format!(
                    "roll_schedule ordinal beyond ±i128 (divergence envelope, \
                     DEC-RUST-CASCADE-RESULTS-001): {}",
                    capture_repr(&ordinal)
                ))
            })?;
            ordinals.push(value);
        }
        rows.push(RollRow {
            instrument_obj,
            instrument: instrument_key,
            schedule: ordinals,
        });
    }
    Ok(rows)
}

/// `not schedule or list(schedule) != sorted(set(schedule)) or schedule[0] < 0`
/// — the shape law: non-empty, strictly increasing, non-negative.
fn roll_schedule_shape_ok(schedule: &[i128]) -> bool {
    if schedule.is_empty() {
        return false;
    }
    if schedule.windows(2).any(|w| w[1] <= w[0]) {
        return false;
    }
    schedule[0] >= 0
}

/// Python's `list(schedule)` repr of int ordinals (`", "` separator).
fn roll_schedule_list_repr(schedule: &[i128]) -> String {
    let items: Vec<String> = schedule.iter().map(|o| o.to_string()).collect();
    format!("[{}]", items.join(", "))
}

/// The FULL `_roll_schedules_from_payload` law. `None` passes through;
/// every refusal raises `ValueError` carrying the COMPLETE
/// `invalid roll_schedules in manifest: …` message (the shim re-raises as
/// `CascadeWireError` verbatim). *futures_alias* is the shim-passed
/// `brutex.universe.NIFTY_FUTURES_SERIES_SYMBOL` (import-identity — no
/// second literal).
pub fn roll_schedules_parse(
    py: Python<'_>,
    raw: &Bound<'_, PyAny>,
    futures_alias: &str,
) -> PyResult<Py<PyAny>> {
    if raw.is_none() {
        return Ok(py.None());
    }
    let rows = match roll_parse_phase(py, raw) {
        Ok(rows) => rows,
        Err(err)
            if err.is_instance_of::<PyTypeError>(py) || err.is_instance_of::<PyValueError>(py) =>
        {
            return Err(PyValueError::new_err(format!(
                "invalid roll_schedules in manifest: {}",
                err.value(py)
            )));
        }
        Err(err) => return Err(err),
    };
    let mut seen: Vec<&str> = Vec::new();
    for row in &rows {
        if seen.contains(&row.instrument.as_str()) {
            return Err(PyValueError::new_err(format!(
                "invalid roll_schedules in manifest: duplicate entry for instrument \
                 {} — an internally contradictory manifest cannot pick a schedule \
                 (dict() would silently adopt the last); the manifest is corrupt — \
                 re-run `brutex sweep-all` to mint a fresh one",
                capture_repr(&row.instrument_obj)
            )));
        }
        seen.push(row.instrument.as_str());
        if row.instrument != futures_alias {
            return Err(PyValueError::new_err(format!(
                "invalid roll_schedules in manifest: instrument {} is not the \
                 NIFTY-futures series alias — a roll schedule may exist for (and \
                 only for) the futures alias (presence ⟺ alias); the manifest is \
                 corrupt — re-run `brutex sweep-all` to mint a fresh one",
                capture_repr(&row.instrument_obj)
            )));
        }
        if !roll_schedule_shape_ok(&row.schedule) {
            return Err(PyValueError::new_err(format!(
                "invalid roll_schedules in manifest: schedule for {} must be a \
                 non-empty strictly increasing tuple of non-negative day ordinals, \
                 got {}",
                capture_repr(&row.instrument_obj),
                roll_schedule_list_repr(&row.schedule)
            )));
        }
    }
    let mut pairs: Vec<Bound<'_, PyAny>> = Vec::with_capacity(rows.len());
    for row in rows {
        let schedule = PyTuple::new(py, row.schedule)?;
        let pair = PyTuple::new(py, [row.instrument_obj, schedule.into_any()])?;
        pairs.push(pair.into_any());
    }
    Ok(PyTuple::new(py, pairs)?.into_any().unbind())
}

/// The manifest version gate — Python rich-compare `!=` semantics, so the
/// `version: true` bool==int PUN parses (the frozen law). A mismatch raises
/// `ValueError` with the verbatim
/// `unsupported cascade manifest version {v!r} (expected {N})` message
/// (the shim re-raises as `CascadeWireError`).
pub fn manifest_version_gate(
    payload: &Bound<'_, PyAny>,
    manifest_version: &Bound<'_, PyAny>,
) -> PyResult<()> {
    let version = mapping_get(payload, "version")?;
    let differs = version
        .rich_compare(manifest_version, CompareOp::Ne)?
        .is_truthy()?;
    if differs {
        return Err(PyValueError::new_err(format!(
            "unsupported cascade manifest version {} (expected {})",
            capture_repr(&version),
            manifest_version.str()?.to_cow()?
        )));
    }
    Ok(())
}

/// The `bars_context` coercion — real `KeyError`/`TypeError` propagate to
/// the shim's `cascade manifest missing/invalid field` guard.
pub fn bars_context_coerce<'py>(
    py: Python<'py>,
    payload: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyDict>> {
    let b = Builtins::new(py)?;
    let bars_raw = payload.get_item("bars_context")?;
    let out = PyDict::new(py);
    out.set_item("exchange", b.str_(&bars_raw.get_item("exchange")?)?)?;
    out.set_item("segment", b.str_(&bars_raw.get_item("segment")?)?)?;
    out.set_item("start", b.str_(&bars_raw.get_item("start")?)?)?;
    out.set_item("end", b.str_(&bars_raw.get_item("end")?)?)?;
    Ok(out)
}

// ───────────────────────────── math trio ─────────────────────────────

/// `_finite` — `None` / non-finite → `0.0`; finite values (including
/// `-0.0`) pass through unchanged.
pub fn finite_or_zero(value: Option<f64>) -> f64 {
    match value {
        Some(v) if v.is_finite() => v,
        _ => 0.0,
    }
}

/// `_per_bar_returns` — `(value − prev) / start` per bar, with the division
/// delegated to CPython int true-division for exact >2^53 rounding (module
/// docs). The i128 subtraction cannot overflow for i64 inputs.
pub fn per_bar_returns(py: Python<'_>, equity_paisa: &[i64], start: i64) -> PyResult<Vec<f64>> {
    let start_obj = start.into_pyobject(py)?;
    let mut out: Vec<f64> = Vec::with_capacity(equity_paisa.len());
    let mut prev: i64 = start;
    for &value in equity_paisa {
        let diff: i128 = i128::from(value) - i128::from(prev);
        let quotient = diff.into_pyobject(py)?.div(&start_obj)?;
        out.push(quotient.extract::<f64>()?);
        prev = value;
    }
    Ok(out)
}

/// Python `max(a, b)` — returns the FIRST arg when the comparison is
/// unordered (the NaN law the frozen `nan_in_series` case pins).
#[inline]
fn py_max(a: f64, b: f64) -> f64 {
    if b > a { b } else { a }
}

/// `_max_drawdown_return_units` — largest peak-to-trough decline of the
/// cumulative-return path (`>= 0`; `0.0` for an empty path).
pub fn max_drawdown_return_units(returns: &[f64]) -> f64 {
    let mut peak = f64::NEG_INFINITY;
    let mut cum = 0.0_f64;
    let mut max_dd = 0.0_f64;
    for &r in returns {
        cum += r;
        peak = py_max(peak, cum);
        max_dd = py_max(max_dd, peak - cum);
    }
    max_dd
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn finite_or_zero_law() {
        assert_eq!(finite_or_zero(None), 0.0);
        assert_eq!(finite_or_zero(Some(f64::NAN)), 0.0);
        assert_eq!(finite_or_zero(Some(f64::INFINITY)), 0.0);
        assert_eq!(finite_or_zero(Some(f64::NEG_INFINITY)), 0.0);
        assert_eq!(finite_or_zero(Some(1.5)), 1.5);
        // -0.0 is FINITE — passes through with its sign preserved.
        assert!(finite_or_zero(Some(-0.0)).is_sign_negative());
    }

    #[test]
    fn max_drawdown_basic() {
        assert_eq!(max_drawdown_return_units(&[]), 0.0);
        assert_eq!(max_drawdown_return_units(&[0.1, 0.2, 0.3]), 0.0);
        // [0.1, -0.3, 0.2, -0.5]: peak 0.1, trough -0.5 → 0.6 (frozen).
        assert_eq!(max_drawdown_return_units(&[0.1, -0.3, 0.2, -0.5]), 0.6);
    }

    #[test]
    fn max_drawdown_nan_never_displaces_peak() {
        // Python max(a, b) keeps the FIRST arg on an unordered compare —
        // a NaN cum can never displace the running peak (frozen 0.0).
        assert_eq!(max_drawdown_return_units(&[0.1, f64::NAN, 0.2]), 0.0);
    }

    #[test]
    fn roll_shape_law() {
        assert!(roll_schedule_shape_ok(&[1, 2, 3]));
        assert!(roll_schedule_shape_ok(&[0]));
        assert!(!roll_schedule_shape_ok(&[]));
        assert!(!roll_schedule_shape_ok(&[5, 5]));
        assert!(!roll_schedule_shape_ok(&[3, 1]));
        assert!(!roll_schedule_shape_ok(&[-1, 3]));
    }

    #[test]
    fn roll_list_repr_matches_python() {
        assert_eq!(roll_schedule_list_repr(&[]), "[]");
        assert_eq!(roll_schedule_list_repr(&[5, 5]), "[5, 5]");
        assert_eq!(roll_schedule_list_repr(&[-1, 3]), "[-1, 3]");
    }

    #[test]
    fn unpack_messages_verbatim() {
        assert_eq!(
            unpack_short_msg(1),
            "not enough values to unpack (expected 2, got 1)"
        );
        assert_eq!(
            unpack_non_iterable_msg("int"),
            "cannot unpack non-iterable int object"
        );
    }
}
