//! CPython-`json.dumps`-byte-identical renders for the combo-aggregate lens
//! payloads.
//!
//! The ONE deterministic JSON rendering law of the deleted
//! `combo_aggregates._dumps` — `json.dumps(payload, sort_keys=True,
//! separators=(",", ":"), ensure_ascii=True, allow_nan=False)` — restricted
//! to the shapes the fold actually renders: `dict[str, int]` maps and the
//! `[[iso_date, int], …]` worst-days list. Every payload value is an exact
//! integer, so no float-repr arm exists here (stated, not hidden).
//!
//! String escaping is the `py_encode_basestring_ascii` twin — the proven
//! `rust/brutex-lake/src/stats_shard.rs` law (short escapes, lowercase
//! `\uXXXX`, astral-plane surrogate pairs). Key ordering: Python `sorted`
//! compares `str` by code point; UTF-8 byte order equals code-point order,
//! so a `BTreeMap<String, _>` walk is byte-identical.

use std::collections::BTreeMap;
use std::fmt::Write as _;

use pyo3::exceptions::{PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple};

/// Append *s* to *out* as a JSON string literal, byte-identical to
/// CPython's `py_encode_basestring_ascii` (`ensure_ascii=True`).
pub fn push_json_string(out: &mut String, s: &str) {
    out.push('"');
    for ch in s.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\u{08}' => out.push_str("\\b"),
            '\t' => out.push_str("\\t"),
            '\n' => out.push_str("\\n"),
            '\u{0c}' => out.push_str("\\f"),
            '\r' => out.push_str("\\r"),
            ' '..='~' => out.push(ch),
            _ => {
                let code = ch as u32;
                if code < 0x10000 {
                    let _ = write!(out, "\\u{code:04x}");
                } else {
                    let c = code - 0x10000;
                    let hi = 0xd800 + (c >> 10);
                    let lo = 0xdc00 + (c & 0x3ff);
                    let _ = write!(out, "\\u{hi:04x}\\u{lo:04x}");
                }
            }
        }
    }
    out.push('"');
}

/// Render a `{str: int}` map exactly as `_dumps(dict)` did (sorted keys,
/// compact separators).
pub fn dumps_str_int_map(map: &BTreeMap<String, i128>) -> String {
    let mut out = String::from("{");
    for (i, (key, value)) in map.iter().enumerate() {
        if i > 0 {
            out.push(',');
        }
        push_json_string(&mut out, key);
        let _ = write!(out, ":{value}");
    }
    out.push('}');
    out
}

/// Render the worst-days `[[iso_date, pnl], …]` list exactly as
/// `_dumps(list)` did.
pub fn dumps_pairs(pairs: &[(String, i128)]) -> String {
    let mut out = String::from("[");
    for (i, (day, pnl)) in pairs.iter().enumerate() {
        if i > 0 {
            out.push(',');
        }
        out.push('[');
        push_json_string(&mut out, day);
        let _ = write!(out, ",{pnl}");
        out.push(']');
    }
    out.push(']');
    out
}

/// The GENERAL `json.dumps(payload, sort_keys=True, separators=(",", ":"),
/// ensure_ascii=True, allow_nan=False)` twin over an arbitrary Python object
/// tree (slice 2a — the cascade_wire serialisation law,
/// DEC-RUST-CASCADE-RESULTS-001).
///
/// Rendering laws, each CPython's own:
///
/// * `str` — [`push_json_string`] (the `py_encode_basestring_ascii` twin);
/// * `int` — the object's own decimal text (`str(int)`, unbounded-exact);
/// * `float` — `float.__repr__(o)` semantics exactly as CPython `json.dumps`
///   (the shortest-round-trip repr of the VALUE, rendered via a fresh
///   `PyFloat` from the extracted double — NEVER the object's own `repr`,
///   so a float subclass like np.float64 serialises as its value), with the
///   `allow_nan=False` gate raising the VERBATIM
///   `ValueError("Out of range float values are not JSON compliant: {o!r}")`
///   (the refusal message keeps the OBJECT's repr — CPython's `%R`);
/// * `bool` / `None` — literals (`PyBool` checked BEFORE `PyInt`);
/// * `list` / `tuple` — element walk;
/// * `dict` — **str keys only**, sorted by Rust `String` `Ord` (byte order ==
///   code-point order == Python `sorted`); a NON-str key refuses loudly
///   (disclosed divergence — `json.dumps` would coerce e.g. int keys;
///   unreachable from the cascade_wire projection builders, which only ever
///   mint str keys);
/// * anything else — the verbatim CPython
///   `TypeError("Object of type {name} is not JSON serializable")`.
pub fn dumps_sorted(obj: &Bound<'_, PyAny>) -> PyResult<String> {
    let mut out = String::new();
    write_json_value(obj, &mut out)?;
    Ok(out)
}

fn write_json_value(v: &Bound<'_, PyAny>, out: &mut String) -> PyResult<()> {
    if v.is_none() {
        out.push_str("null");
    } else if v.is_instance_of::<PyString>() {
        // A lone-surrogate str fails UTF-8 extraction — a loud refusal (the
        // slice-1 disclosed divergence envelope; CPython would escape it).
        let s = v.extract::<String>()?;
        push_json_string(out, &s);
    } else if v.is_instance_of::<PyBool>() {
        out.push_str(if v.is_truthy()? { "true" } else { "false" });
    } else if v.is_instance_of::<PyInt>() {
        // Exact unbounded decimal text — `str(int)` == `int.__repr__`.
        out.push_str(v.str()?.to_cow()?.as_ref());
    } else if v.is_instance_of::<PyFloat>() {
        let f = v.extract::<f64>()?;
        if !f.is_finite() {
            return Err(PyValueError::new_err(format!(
                "Out of range float values are not JSON compliant: {}",
                crate::capture_repr(v)
            )));
        }
        // `float.__repr__(o)` semantics, NEVER the object's own `repr()`
        // (fix round, MINOR-2): CPython `json.dumps` renders every float
        // through `float.__repr__` unconditionally, so a float SUBCLASS with
        // an overridden repr (np.float64's exact shape) must serialise as
        // its VALUE — repr of a FRESH `PyFloat` carrying the extracted
        // double IS `float.__repr__`'s output (it formats the stored C
        // double, nothing else). The object's own repr would emit invalid
        // JSON (`np.float64(1.5)`).
        out.push_str(PyFloat::new(v.py(), f).repr()?.to_cow()?.as_ref());
    } else if v.is_instance_of::<PyList>() || v.is_instance_of::<PyTuple>() {
        out.push('[');
        for (i, item) in v.try_iter()?.enumerate() {
            if i > 0 {
                out.push(',');
            }
            write_json_value(&item?, out)?;
        }
        out.push(']');
    } else if v.is_instance_of::<PyDict>() {
        let d = v.cast::<PyDict>()?;
        let mut pairs: Vec<(String, Bound<'_, PyAny>)> = Vec::with_capacity(d.len());
        for (key, value) in d.iter() {
            if !key.is_instance_of::<PyString>() {
                return Err(PyTypeError::new_err(format!(
                    "dumps_sorted: non-str dict key {} (disclosed divergence — \
                     unreachable from the cascade_wire projection builders)",
                    crate::capture_repr(&key)
                )));
            }
            pairs.push((key.extract::<String>()?, value));
        }
        pairs.sort_by(|a, b| a.0.cmp(&b.0));
        out.push('{');
        for (i, (key, value)) in pairs.iter().enumerate() {
            if i > 0 {
                out.push(',');
            }
            push_json_string(out, key);
            out.push(':');
            write_json_value(value, out)?;
        }
        out.push('}');
    } else {
        return Err(PyTypeError::new_err(format!(
            "Object of type {} is not JSON serializable",
            v.get_type().name()?.to_cow()?
        )));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn empty_shapes() {
        assert_eq!(dumps_str_int_map(&BTreeMap::new()), "{}");
        assert_eq!(dumps_pairs(&[]), "[]");
    }

    #[test]
    fn sorted_compact_map() {
        let mut map = BTreeMap::new();
        map.insert("9".to_string(), 2_i128);
        map.insert("10".to_string(), 1_i128);
        map.insert("13".to_string(), -3_i128);
        // Python: json.dumps({"9":2,"10":1,"13":-3}, sort_keys=True,
        // separators=(",",":")) == '{"10":1,"13":-3,"9":2}' — "10" < "13" < "9".
        assert_eq!(dumps_str_int_map(&map), "{\"10\":1,\"13\":-3,\"9\":2}");
    }

    #[test]
    fn pairs_render() {
        let pairs = vec![
            ("2024-01-04".to_string(), -2_500_i128),
            ("2024-01-02".to_string(), 3_000_i128),
        ];
        assert_eq!(
            dumps_pairs(&pairs),
            "[[\"2024-01-04\",-2500],[\"2024-01-02\",3000]]"
        );
    }

    #[test]
    fn escaper_matches_cpython_ensure_ascii() {
        let mut out = String::new();
        push_json_string(&mut out, "a\"b\\c\u{08}\t\n\u{0c}\r\u{7f}é₹\u{1f600}");
        // CPython: json.dumps(...) ==
        // "a\"b\\c\b\t\n\f\ré₹😀"
        assert_eq!(
            out,
            "\"a\\\"b\\\\c\\b\\t\\n\\f\\r\\u007f\\u00e9\\u20b9\\ud83d\\ude00\""
        );
    }
}
