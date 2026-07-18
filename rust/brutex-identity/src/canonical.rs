//! The param canonicaliser — `brutex/strategies/combo.py::_canonicalise_value`
//! + `canonicalise_params` + `combo_hash`, walking live Python objects.
//!
//! DEC-RUST-RUNID-STORES-001: emits the canonical JSON text DIRECTLY (one
//! pass — the canonicalise-then-`json.dumps` composition of the retired
//! Python body, byte-identical: sorted keys at every nesting level, compact
//! `(",", ":")` separators, `ensure_ascii` escaping via [`crate::pyjson`],
//! floats via the CPython-repr twin [`crate::pyfloat`]). Byte-parity is
//! proven by the pre-conversion oracle fixture + the live differential suite,
//! so every existing run identity (Golden Rule #4) is unchanged.
//!
//! CPython runtime primitives deliberately USED (not re-implemented,
//! disclosed in the DEC): `str(Decimal)` (the exact scale-preserving text
//! law lives in CPython's `decimal`), `str(int)` for beyond-i128 bignums,
//! and `repr(value)` in the unsupported-type error message. Behavioural
//! narrowing (disclosed): a `str` carrying a LONE SURROGATE (illegal
//! Unicode, producible only via `surrogateescape`) is now REJECTED with a
//! typed error — the retired body hashed it; no engine caller can produce
//! one.

use crate::{pyfloat, pyjson};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyString, PyTuple, PyType};

/// Maximum nesting depth (`_MAX_CANONICALISE_DEPTH` twin).
const MAX_DEPTH: usize = 64;

static DECIMAL_TYPE: PyOnceLock<Py<PyType>> = PyOnceLock::new();
static ABC_MAPPING: PyOnceLock<Py<PyType>> = PyOnceLock::new();
static ABC_SEQUENCE: PyOnceLock<Py<PyType>> = PyOnceLock::new();

fn cached_type<'py>(
    py: Python<'py>,
    cell: &'static PyOnceLock<Py<PyType>>,
    module: &str,
    name: &str,
) -> PyResult<&'py Bound<'py, PyType>> {
    cell.get_or_try_init(py, || {
        let ty = py.import(module)?.getattr(name)?.cast_into::<PyType>()?;
        Ok::<_, PyErr>(ty.unbind())
    })
    .map(|ty| ty.bind(py))
}

fn value_error(message: String) -> PyErr {
    PyValueError::new_err(message)
}

/// Extract a Python `str` into Rust, mapping a lone-surrogate failure to the
/// canonicaliser's typed rejection (the disclosed narrowing).
fn extract_text(value: &Bound<'_, PyAny>, role: &str) -> PyResult<String> {
    value.extract::<String>().map_err(|_| {
        value_error(format!(
            "canonicalise_params: {role} carrying a lone surrogate is not \
             hashable (invalid Unicode)"
        ))
    })
}

/// Emit one canonical value into `out` — the `_canonicalise_value` +
/// `json.dumps` composition, byte-identical.
fn canonicalise_into(
    py: Python<'_>,
    value: &Bound<'_, PyAny>,
    depth: usize,
    out: &mut String,
) -> PyResult<()> {
    if depth > MAX_DEPTH {
        return Err(value_error(format!(
            "canonicalise_params: nesting deeper than {MAX_DEPTH} levels is not supported"
        )));
    }
    if value.is_none() {
        out.push_str("null");
        return Ok(());
    }
    if value.is_instance_of::<PyBool>() {
        out.push_str(if value.extract::<bool>()? {
            "true"
        } else {
            "false"
        });
        return Ok(());
    }
    if value.is_instance_of::<PyInt>() {
        // `json.dumps` renders arbitrary-precision ints as plain decimal
        // digits; i64 covers every engine param, i128 the realistic rest,
        // and the bignum tail rides CPython's exact `str(int(value))`.
        if let Ok(v) = value.extract::<i64>() {
            out.push_str(&v.to_string());
        } else if let Ok(v) = value.extract::<i128>() {
            out.push_str(&v.to_string());
        } else {
            let plain = py.get_type::<PyInt>().call1((value,))?;
            out.push_str(plain.str()?.to_str()?);
        }
        return Ok(());
    }
    if value.is_instance_of::<PyFloat>() {
        if !value.is_exact_instance_of::<PyFloat>() {
            return Err(value_error(format!(
                "canonicalise_params: float subclasses (e.g. numpy.float64) are \
                 not accepted — pass a built-in float; got {}",
                value.get_type().name()?
            )));
        }
        let v = value.extract::<f64>()?;
        if !v.is_finite() {
            return Err(value_error(
                "canonicalise_params: non-finite floats (NaN/inf) are not hashable".to_string(),
            ));
        }
        // -0.0 → +0.0 BEFORE repr (the `value + 0.0` signed-zero
        // canonicalisation — Golden Rule #4 reproducibility).
        pyjson::write_json_escaped(out, &pyfloat::py_float_repr(v + 0.0));
        return Ok(());
    }
    if value.is_instance(cached_type(py, &DECIMAL_TYPE, "decimal", "Decimal")?.as_any())? {
        if !value.call_method0("is_finite")?.extract::<bool>()? {
            return Err(value_error(
                "canonicalise_params: non-finite Decimals (NaN/Infinity) are not hashable"
                    .to_string(),
            ));
        }
        // `str(Decimal)` — the exact scale-preserving CPython text law.
        pyjson::write_json_escaped(out, value.str()?.to_str()?);
        return Ok(());
    }
    if value.is_instance_of::<PyString>() {
        pyjson::write_json_escaped(out, &extract_text(value, "a string value")?);
        return Ok(());
    }
    if value.is_instance(cached_type(py, &ABC_MAPPING, "collections.abc", "Mapping")?.as_any())? {
        return emit_mapping(py, value, depth, out);
    }
    if value.is_instance_of::<pyo3::types::PyList>() || value.is_instance_of::<PyTuple>() {
        return emit_array(py, value, depth, out);
    }
    let is_sequence = value
        .is_instance(cached_type(py, &ABC_SEQUENCE, "collections.abc", "Sequence")?.as_any())?;
    if is_sequence && !value.is_instance_of::<pyo3::types::PyBytes>() {
        // `str` was handled above, so this mirrors the Python
        // `Sequence and not (str | bytes)` arm exactly (bytearray included).
        return emit_array(py, value, depth, out);
    }
    Err(value_error(format!(
        "canonicalise_params: unsupported value type {}: {}",
        value.get_type().name()?,
        value.repr()?.to_str().unwrap_or("<unrepresentable>")
    )))
}

/// Emit a JSON object: keys validated as `str`, values recursed, duplicate
/// keys collapse keep-LAST (dict-comprehension semantics for a generic
/// Mapping), then sorted by key (`sort_keys=True` — code-point order, which
/// Rust's UTF-8 byte order reproduces exactly).
fn emit_mapping(
    py: Python<'_>,
    value: &Bound<'_, PyAny>,
    depth: usize,
    out: &mut String,
) -> PyResult<()> {
    let mut pairs: Vec<(String, String)> = Vec::new();
    let mut index: std::collections::HashMap<String, usize> = std::collections::HashMap::new();
    let mut visit = |key: Bound<'_, PyAny>, item: Bound<'_, PyAny>| -> PyResult<()> {
        if !key.is_instance_of::<PyString>() {
            return Err(value_error(format!(
                "canonicalise_params: dict keys must be str, got {}",
                key.get_type().name()?
            )));
        }
        let key_text = extract_text(&key, "a dict key")?;
        let mut rendered = String::new();
        canonicalise_into(py, &item, depth + 1, &mut rendered)?;
        match index.get(&key_text) {
            Some(&at) => pairs[at] = (key_text, rendered),
            None => {
                index.insert(key_text.clone(), pairs.len());
                pairs.push((key_text, rendered));
            }
        }
        Ok(())
    };
    if let Ok(dict) = value.cast::<PyDict>() {
        for (key, item) in dict.iter() {
            visit(key, item)?;
        }
    } else {
        for entry in value.call_method0("items")?.try_iter()? {
            let entry = entry?;
            let pair = entry.cast::<PyTuple>().map_err(|_| {
                value_error(
                    "canonicalise_params: Mapping.items() must yield (key, value) pairs"
                        .to_string(),
                )
            })?;
            if pair.len() != 2 {
                return Err(value_error(
                    "canonicalise_params: Mapping.items() must yield (key, value) pairs"
                        .to_string(),
                ));
            }
            visit(pair.get_item(0)?, pair.get_item(1)?)?;
        }
    }
    pairs.sort_by(|a, b| a.0.cmp(&b.0));
    out.push('{');
    for (i, (key, rendered)) in pairs.iter().enumerate() {
        if i > 0 {
            out.push(',');
        }
        pyjson::write_json_escaped(out, key);
        out.push(':');
        out.push_str(rendered);
    }
    out.push('}');
    Ok(())
}

/// Emit a JSON array over the value's iteration order.
fn emit_array(
    py: Python<'_>,
    value: &Bound<'_, PyAny>,
    depth: usize,
    out: &mut String,
) -> PyResult<()> {
    out.push('[');
    for (i, item) in value.try_iter()?.enumerate() {
        if i > 0 {
            out.push(',');
        }
        canonicalise_into(py, &item?, depth + 1, out)?;
    }
    out.push(']');
    Ok(())
}

/// `canonicalise_params` twin: the deterministic JSON-string encoding.
pub fn canonicalise_params_text(py: Python<'_>, params: &Bound<'_, PyAny>) -> PyResult<String> {
    if !params.is_instance(cached_type(py, &ABC_MAPPING, "collections.abc", "Mapping")?.as_any())? {
        return Err(value_error(format!(
            "canonicalise_params: top-level params must be a Mapping, got {}",
            params.get_type().name()?
        )));
    }
    let mut out = String::new();
    emit_mapping(py, params, 0, &mut out)?;
    Ok(out)
}

/// The identity-payload field-join delimiter (`UNIT_SEPARATOR` twin).
pub const UNIT_SEPARATOR: char = '\u{1f}';

/// `combo_hash` twin: the 9-field BLAKE3 hex run-identity.
#[allow(clippy::too_many_arguments)]
pub fn combo_hash_hex(
    py: Python<'_>,
    strategy_id: &str,
    direction: &str,
    instrument: &str,
    tf: &str,
    mode: &str,
    params: &Bound<'_, PyAny>,
    data_snapshot_hash: &str,
    indicator_version: &str,
    code_commit_sha: &str,
) -> PyResult<String> {
    let required: [(&str, &str); 8] = [
        ("strategy_id", strategy_id),
        ("direction", direction),
        ("instrument", instrument),
        ("tf", tf),
        ("mode", mode),
        ("data_snapshot_hash", data_snapshot_hash),
        ("indicator_version", indicator_version),
        ("code_commit_sha", code_commit_sha),
    ];
    for (name, field) in required {
        if field.is_empty() {
            return Err(value_error(format!("combo_hash: {name} must be non-empty")));
        }
    }
    for (name, field) in required {
        if field.contains(UNIT_SEPARATOR) {
            return Err(value_error(format!(
                "combo_hash: {name} must not contain the \\x1f unit-separator \
                 byte (it is the field join delimiter)"
            )));
        }
    }
    let canonical = canonicalise_params_text(py, params)?;
    let payload = [
        strategy_id,
        direction,
        instrument,
        tf,
        mode,
        &canonical,
        data_snapshot_hash,
        indicator_version,
        code_commit_sha,
    ]
    .join("\u{1f}");
    Ok(blake3::hash(payload.as_bytes()).to_hex().to_string())
}
