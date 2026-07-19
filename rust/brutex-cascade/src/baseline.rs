//! # `baseline` — the buy-&-hold baseline compute / render / verdict laws
//!
//! DEC-RUST-CASCADE-RESULTS-001 phase B, slice 2d: the law families of the
//! deleted `brutex/engine/baseline.py` bodies, ported token-for-token and
//! verified against the FROZEN pre-conversion parity corpus
//! (`tests/golden/fixtures/cascade_rust_parity/baseline.json`). The file
//! I/O half — `atomic_write_simple`, `path.read_text`, the structlog
//! `baseline.written` / `baseline.load_invalid` observability — STAYS
//! shim-side Python (the sanctioned I/O clause), as does the load
//! CONSTRUCTION funnel (`date.fromisoformat` / `Decimal` / `float`
//! marshalling under the `(BaselineError, InvalidOperation, ValueError)`
//! catch — the verdict `bad_field_values` is order-insensitive, so the
//! shim-side funnel is byte-equivalent).
//!
//! ## Laws hosted here
//!
//! * [`compute`] — the anchor / per-session-close fold: stable sort by
//!   timestamp, the frozen refusal ORDER (empty → duplicate-timestamp →
//!   non-positive anchor, messages verbatim incl. the FULL CPython
//!   `datetime` repr), the IST session key (`ist::ist_epoch_day` — the
//!   fixed +05:30 twin of `session_lens.ist_trade_date`, exact over the
//!   whole post-1970 lake domain), last-bar-per-session-wins, and the
//!   28-significant-digit precision law: `(last_close − anchor) / anchor
//!   × 100` runs on the ORIGINAL `Decimal` objects through CPython's own
//!   operators (ambient context — never plain f64 division) and
//!   `builtins.float` converts — quirk 15 holds BY CONSTRUCTION;
//! * [`validate_series`] — the `BaselineSeries.__post_init__` matrix:
//!   truthiness gates, the strictly-ascending `>=` date law, the
//!   start/end consistency message (`[A … B]`), the `anchor_open <= 0`
//!   rich compare (a `Decimal("NaN")` anchor raises `InvalidOperation`
//!   out of the compare — quirk 24 — while `Infinity` legally passes) and
//!   `math.isfinite` on the total (the `True` bool==int pun PASSES);
//! * [`filename`] — `baseline-{instrument}.json` with NO sanitization
//!   (hostile instruments pass VERBATIM; only empty refuses);
//! * [`price_str`] — `str(round_money(v))` via the crate's
//!   [`decimal::quantize_2dp_string`] half-up twin (quiet NaN passes
//!   through; `sNaN` / `Infinity` / a beyond-28-digit quantize raise the
//!   EXACT CPython `decimal.InvalidOperation` —
//!   `args == ([InvalidOperation],)`, probe-verified);
//! * [`render`] — the B8 artifact byte law over the payload dict
//!   (`json.dumps(sort_keys=True, separators=(",", ":"),
//!   ensure_ascii=True, allow_nan=False)` via the crate's
//!   [`pyjson::dumps_sorted_surrogatepass`] twin — a lone-surrogate
//!   instrument escapes to ASCII exactly as CPython does — plus the
//!   trailing newline), with the empty-`data_snapshot_hash` refusal
//!   BEFORE any payload build; the `allow_nan=False` arm is structurally
//!   DEAD through the public API (post_init refuses non-finite totals;
//!   session closes serialize as strings) — ported faithfully in pyjson,
//!   reachability NOT invented;
//! * [`load_verdict`] — the damage-verdict ladder over ALREADY-READ
//!   content: CPython `json.loads` with the `except ValueError` catch set
//!   mirrored (a `RecursionError` nesting bomb escapes raw) → `not_object`
//!   → the EXACT key set → the `schema_version !=` rich-compare gate (the
//!   `True` / `1.0` puns LOAD) → hash / sessions-shape / field-type gates
//!   (isinstance laws verbatim — a bool price fails the str check; the
//!   `total_return_pct` gate excludes bool EXPLICITLY, the frozen
//!   constructor-vs-load asymmetry).
//!
//! ## Disclosed divergence envelope (non-production shapes)
//!
//! * a hostile `Decimal` SUBCLASS whose `__str__` yields a lone surrogate
//!   fails `to_cow` extraction loudly at [`price_str`] (real Decimals
//!   print ASCII always);
//! * a quiet-NaN price with a diagnostic PAYLOAD (`NaN123`) passes
//!   through [`price_str`] verbatim — CPython `quantize` propagates the
//!   payload too, and the payload-carry EQUALITY is TESTED (the
//!   `test_quiet_nan_payload_carry_matches` pin in
//!   `tests/property/test_baseline_rust_differential.py`); still a
//!   non-production shape (no lake price is NaN);
//! * pre-1945 bar instants would diverge from tzdb IST history (the
//!   `ist.rs` honesty envelope — the lake floor is 2020-01-01).

use std::collections::HashSet;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PyString};

use crate::{Frags, capture_repr, decimal, domain_err, domain_err_any, ist, pyjson};

/// Raise the EXACT CPython `decimal.InvalidOperation` the C `quantize`
/// raises: `InvalidOperation([InvalidOperation])` — `str(exc)` is
/// `"[<class 'decimal.InvalidOperation'>]"` (probe-verified 2026-07-19).
fn invalid_operation_err(py: Python<'_>) -> PyErr {
    let build = || -> PyResult<PyErr> {
        let inv = py.import("decimal")?.getattr("InvalidOperation")?;
        let arg = PyList::new(py, [&inv])?;
        Ok(PyErr::from_value(inv.call1((arg,))?))
    };
    match build() {
        Ok(err) => err,
        Err(err) => err,
    }
}

/// `_price_str` — `str(round_money(value))` over the value's own
/// `Decimal.__str__` token.
pub fn price_str(py: Python<'_>, value: &Bound<'_, PyAny>) -> PyResult<String> {
    let token = value.str()?.to_cow()?.into_owned();
    match decimal::quantize_2dp_string(&token) {
        Ok(out) => Ok(out),
        Err(_) => Err(invalid_operation_err(py)),
    }
}

/// `baseline_filename` — `f"baseline-{instrument}.json"` VERBATIM (no
/// sanitization — that IS the law); only an empty instrument refuses.
pub fn filename<'py>(
    py: Python<'py>,
    instrument: &Bound<'py, PyAny>,
    error_cls: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    if !instrument.is_truthy()? {
        return Err(domain_err(
            error_cls,
            "baseline_filename: instrument must be non-empty".to_string(),
        ));
    }
    let f = Frags::new(py);
    f.lit("baseline-")?;
    f.obj(instrument)?;
    f.lit(".json")?;
    f.join()
}

/// `compute_baseline`'s fold over marshalled bar columns (`ts_us[i]`,
/// `ts_objs[i]`, `opens[i]`, `closes[i]` describe the caller's bar `i`;
/// prices are the ORIGINAL `Decimal` objects — the series carries them
/// VERBATIM, the 2-dp snap happens only at write).
pub fn compute<'py>(
    py: Python<'py>,
    instrument: &Bound<'py, PyAny>,
    ts_us: Vec<i64>,
    ts_objs: &Bound<'py, PyAny>,
    opens: &Bound<'py, PyAny>,
    closes: &Bound<'py, PyAny>,
    error_cls: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyDict>> {
    let n = ts_us.len();
    if n == 0 {
        let msg = format!(
            "compute_baseline: no bars for {} — nothing to anchor",
            capture_repr(instrument)
        );
        return Err(domain_err(error_cls, msg));
    }
    // The timestamp-sorted view (stable — equal instants keep input order,
    // so the duplicate refusal names the SAME bar the deleted law named).
    let mut order: Vec<usize> = (0..n).collect();
    order.sort_by_key(|&i| ts_us[i]);
    for w in order.windows(2) {
        if ts_us[w[0]] == ts_us[w[1]] {
            let ts_obj = ts_objs.get_item(w[1])?;
            let msg = format!(
                "compute_baseline: duplicate bar timestamp {} — dedup at the lake read \
                 boundary before baselining",
                capture_repr(&ts_obj)
            );
            return Err(domain_err(error_cls, msg));
        }
    }
    let anchor = opens.get_item(order[0])?;
    if anchor.le(0)? {
        let msg = format!(
            "compute_baseline: first bar OPEN must be > 0, got {} (corrupt stored bar)",
            anchor.str()?.to_cow()?
        );
        return Err(domain_err(error_cls, msg));
    }
    // One point per IST session — the LAST bar (by sorted timestamp) wins.
    let mut close_by_day: std::collections::BTreeMap<i64, usize> =
        std::collections::BTreeMap::new();
    for &i in &order {
        close_by_day.insert(ist::ist_epoch_day(ts_us[i]), i);
    }
    let session_days: Vec<i64> = close_by_day.keys().copied().collect();
    let session_closes = PyList::empty(py);
    for &i in close_by_day.values() {
        session_closes.append(closes.get_item(i)?)?;
    }
    let last_idx = *close_by_day
        .values()
        .next_back()
        .expect("non-empty by the n == 0 gate");
    let last_close = closes.get_item(last_idx)?;
    // total = float((last_close − anchor) / anchor * 100) — CPython Decimal
    // operators at the ambient (28-significant-digit) context, THEN the
    // nearest-f64 conversion (quirk 15; never plain f64 division).
    let scaled = last_close.sub(&anchor)?.div(&anchor)?.mul(100)?;
    let total = py.import("builtins")?.getattr("float")?.call1((scaled,))?;
    // The source_note f-string (instrument interpolated via str() — a
    // lone-surrogate instrument flows through the fragment join verbatim).
    let f = Frags::new(py);
    f.lit("buy-&-hold ")?;
    f.obj(instrument)?;
    f.owned(&format!(
        ": anchor = first bar OPEN of the first session; one point per IST session = \
         that session's LAST bar CLOSE; computed from the sweep's own {} bars over {} \
         session(s) (R27 same-bars law)",
        n,
        session_days.len()
    ))?;
    let source_note = f.join()?;

    let out = PyDict::new(py);
    out.set_item("session_days", session_days)?;
    out.set_item("session_closes", session_closes)?;
    out.set_item("anchor", anchor)?;
    out.set_item("total_return_pct", total)?;
    out.set_item("source_note", source_note)?;
    Ok(out)
}

/// The `BaselineSeries.__post_init__` validation matrix.
#[allow(clippy::too_many_arguments)]
pub fn validate_series(
    py: Python<'_>,
    instrument: &Bound<'_, PyAny>,
    start: &Bound<'_, PyAny>,
    end: &Bound<'_, PyAny>,
    anchor_open: &Bound<'_, PyAny>,
    sessions: &Bound<'_, PyAny>,
    total_return_pct: &Bound<'_, PyAny>,
    error_cls: &Bound<'_, PyAny>,
) -> PyResult<()> {
    if !instrument.is_truthy()? {
        return Err(domain_err(
            error_cls,
            "BaselineSeries.instrument must be non-empty".to_string(),
        ));
    }
    if !sessions.is_truthy()? {
        return Err(domain_err(
            error_cls,
            "BaselineSeries.sessions must be non-empty".to_string(),
        ));
    }
    let n = sessions.len()?;
    for i in 1..n {
        let prev = sessions.get_item(i - 1)?.get_item(0)?;
        let cur = sessions.get_item(i)?.get_item(0)?;
        if prev.ge(&cur)? {
            return Err(domain_err(
                error_cls,
                "BaselineSeries.sessions must be strictly ascending by date".to_string(),
            ));
        }
    }
    let first = sessions.get_item(0)?.get_item(0)?;
    let last = sessions.get_item(-1)?.get_item(0)?;
    if start.ne(&first)? || end.ne(&last)? {
        let f = Frags::new(py);
        f.lit("BaselineSeries start/end must equal the first/last session date, got start=")?;
        f.obj(start)?;
        f.lit(" end=")?;
        f.obj(end)?;
        f.lit(" vs sessions [")?;
        f.obj(&first)?;
        f.lit(" … ")?;
        f.obj(&last)?;
        f.lit("]")?;
        return Err(domain_err_any(error_cls, &f.join()?));
    }
    // `anchor_open <= 0` — the rich compare on the RAW object: a
    // `Decimal("NaN")` anchor raises `InvalidOperation` out of the compare
    // (quirk 24 — trapped by the load funnel), `Infinity` legally passes.
    if anchor_open.le(0)? {
        let f = Frags::new(py);
        f.lit("BaselineSeries.anchor_open must be > 0, got ")?;
        f.obj(anchor_open)?;
        return Err(domain_err_any(error_cls, &f.join()?));
    }
    // `math.isfinite` — the bool==int pun PASSES (`isfinite(True)`); a huge
    // int raises the same OverflowError CPython raised.
    let finite = py
        .import("math")?
        .getattr("isfinite")?
        .call1((total_return_pct,))?
        .is_truthy()?;
    if !finite {
        let f = Frags::new(py);
        f.lit("BaselineSeries.total_return_pct must be finite, got ")?;
        f.frag(&total_return_pct.repr()?.into_any())?;
        return Err(domain_err_any(error_cls, &f.join()?));
    }
    Ok(())
}

/// `write_baseline`'s payload-build + render law over the series fields
/// (the empty-hash refusal fires BEFORE any payload work; the shim owns
/// the atomic write + the `baseline.written` log).
#[allow(clippy::too_many_arguments)]
pub fn render(
    py: Python<'_>,
    instrument: &Bound<'_, PyAny>,
    start: &Bound<'_, PyAny>,
    end: &Bound<'_, PyAny>,
    anchor_open: &Bound<'_, PyAny>,
    sessions: &Bound<'_, PyAny>,
    total_return_pct: &Bound<'_, PyAny>,
    source_note: &Bound<'_, PyAny>,
    data_snapshot_hash: &Bound<'_, PyAny>,
    schema_version: &Bound<'_, PyAny>,
    error_cls: &Bound<'_, PyAny>,
) -> PyResult<String> {
    if !data_snapshot_hash.is_truthy()? {
        return Err(domain_err(
            error_cls,
            "write_baseline: data_snapshot_hash must be non-empty".to_string(),
        ));
    }
    // The payload dict, built in the deleted literal's evaluation order.
    let payload = PyDict::new(py);
    payload.set_item("schema_version", schema_version)?;
    payload.set_item("instrument", instrument)?;
    payload.set_item("start", start.call_method0("isoformat")?)?;
    payload.set_item("end", end.call_method0("isoformat")?)?;
    payload.set_item("anchor_open", price_str(py, anchor_open)?)?;
    let sessions_out = PyList::empty(py);
    for pair in sessions.try_iter()? {
        let pair = pair?;
        let day = pair.get_item(0)?;
        let close = pair.get_item(1)?;
        let row = PyList::new(
            py,
            [
                day.call_method0("isoformat")?,
                PyString::new(py, &price_str(py, &close)?).into_any(),
            ],
        )?;
        sessions_out.append(row)?;
    }
    payload.set_item("sessions", sessions_out)?;
    payload.set_item("total_return_pct", total_return_pct)?;
    payload.set_item("data_snapshot_hash", data_snapshot_hash)?;
    payload.set_item("source_note", source_note)?;
    let mut rendered = pyjson::dumps_sorted_surrogatepass(payload.as_any())?;
    rendered.push('\n');
    Ok(rendered)
}

/// `load_baseline`'s damage-verdict ladder over ALREADY-READ content →
/// `(reason | None, payload | None)`; the shim maps a reason to ONE
/// `baseline.load_invalid` warning and runs the construction funnel on a
/// verdict-clean payload.
pub fn load_verdict<'py>(
    py: Python<'py>,
    raw: &Bound<'py, PyAny>,
    schema_version: &Bound<'py, PyAny>,
    payload_keys: &Bound<'py, PyAny>,
    session_fields: usize,
) -> PyResult<(Option<&'static str>, Option<Bound<'py, PyAny>>)> {
    let loads = py.import("json")?.getattr("loads")?;
    let payload = match loads.call1((raw,)) {
        Ok(p) => p,
        Err(err) => {
            // The deleted law's `except ValueError` catch set — a non-member
            // (a RecursionError nesting bomb) escapes exactly as before.
            if err.is_instance_of::<PyValueError>(py) {
                return Ok((Some("not_json"), None));
            }
            return Err(err);
        }
    };
    if !payload.is_instance_of::<PyDict>() {
        return Ok((Some("not_object"), None));
    }
    // The EXACT B8 key set (an extra OR missing key is damage). A
    // lone-surrogate key can never equal the ASCII key set — the failed
    // extraction takes the same `bad_key_set` verdict.
    let dict = payload.cast::<PyDict>().expect("gated isinstance dict");
    let mut keys: HashSet<String> = HashSet::new();
    for key in dict.keys() {
        match key.extract::<String>() {
            Ok(k) => {
                keys.insert(k);
            }
            Err(_) => return Ok((Some("bad_key_set"), None)),
        }
    }
    let mut expected: HashSet<String> = HashSet::new();
    for key in payload_keys.try_iter()? {
        expected.insert(key?.extract()?);
    }
    if keys != expected {
        return Ok((Some("bad_key_set"), None));
    }
    // `payload["schema_version"] != BASELINE_SCHEMA_VERSION` — the Python
    // rich compare: `True != 1` and `1.0 != 1` are False → BOTH LOAD.
    if payload
        .get_item("schema_version")?
        .rich_compare(schema_version, pyo3::pyclass::CompareOp::Ne)?
        .is_truthy()?
    {
        return Ok((Some("schema_version_mismatch"), None));
    }
    let hash = payload.get_item("data_snapshot_hash")?;
    if !hash.is_instance_of::<PyString>() || !hash.is_truthy()? {
        return Ok((Some("bad_data_snapshot_hash"), None));
    }
    // Sessions: a list of `[iso_date, price_str]` pairs — bools fail the
    // isinstance-str checks (a bool price is shape damage).
    let sessions_raw = payload.get_item("sessions")?;
    if !sessions_raw.is_instance_of::<PyList>() {
        return Ok((Some("bad_sessions_shape"), None));
    }
    for entry in sessions_raw.try_iter()? {
        let entry = entry?;
        if !entry.is_instance_of::<PyList>()
            || entry.len()? != session_fields
            || !entry.get_item(0)?.is_instance_of::<PyString>()
            || !entry.get_item(1)?.is_instance_of::<PyString>()
        {
            return Ok((Some("bad_sessions_shape"), None));
        }
    }
    for key in ["instrument", "start", "end", "anchor_open"] {
        if !payload.get_item(key)?.is_instance_of::<PyString>() {
            return Ok((Some("bad_field_types"), None));
        }
    }
    if !payload
        .get_item("source_note")?
        .is_instance_of::<PyString>()
    {
        return Ok((Some("bad_field_types"), None));
    }
    // `isinstance(x, int | float) and not isinstance(x, bool)` — bool True
    // is EXPLICITLY excluded here (the constructor accepts it — the frozen
    // asymmetry, quirk 23); an int LOADS and round-trips through float().
    let total = payload.get_item("total_return_pct")?;
    let is_num = total.is_instance_of::<PyInt>() || total.is_instance_of::<PyFloat>();
    if !is_num || total.is_instance_of::<PyBool>() {
        return Ok((Some("bad_total_return_pct"), None));
    }
    Ok((None, Some(payload)))
}
