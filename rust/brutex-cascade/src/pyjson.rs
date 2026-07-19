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

/// String-arm policy of the value walk. The two public entries are DISTINCT
/// byte laws (DEC-RUST-CASCADE-RESULTS-001 slice 2b): identical on every
/// UTF-8-extractable string, divergent ONLY on a lone-surrogate `str`.
#[derive(Clone, Copy, PartialEq, Eq)]
enum StrPolicy {
    /// [`dumps_sorted`] (slice 2a — cascade_wire): a lone-surrogate str fails
    /// UTF-8 extraction — the disclosed loud refusal (unreachable from the
    /// cascade_wire projection builders).
    RefuseSurrogates,
    /// [`dumps_sorted_surrogatepass`] (slice 2b — top100_artifact): the full
    /// CPython `py_encode_basestring_ascii` arm — a lone surrogate escapes as
    /// its `\uXXXX` code unit (`ensure_ascii=True` never rejects it; the
    /// frozen `lone_surrogate_instrument` corpus case).
    EscapeSurrogates,
}

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

/// Append a string given as UTF-16-LE bytes (`str.encode("utf-16-le",
/// "surrogatepass")`) as a JSON literal, byte-identical to CPython's
/// `py_encode_basestring_ascii`. CPython escapes per CODE POINT: BMP points
/// (lone surrogates included) render `\uXXXX`, astral points render their
/// surrogate PAIR — which is exactly the UTF-16 code-unit sequence, so a
/// per-unit walk reproduces both arms verbatim.
fn push_json_string_utf16le(out: &mut String, bytes: &[u8]) {
    out.push('"');
    for chunk in bytes.chunks_exact(2) {
        let unit = u16::from_le_bytes([chunk[0], chunk[1]]);
        match unit {
            0x22 => out.push_str("\\\""),
            0x5c => out.push_str("\\\\"),
            0x08 => out.push_str("\\b"),
            0x09 => out.push_str("\\t"),
            0x0a => out.push_str("\\n"),
            0x0c => out.push_str("\\f"),
            0x0d => out.push_str("\\r"),
            0x20..=0x7e => out.push(unit as u8 as char),
            _ => {
                let _ = write!(out, "\\u{unit:04x}");
            }
        }
    }
    out.push('"');
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
    write_json_value(obj, &mut out, StrPolicy::RefuseSurrogates)?;
    Ok(out)
}

/// The `top100_artifact._dumps` byte law (slice 2b) — IDENTICAL to
/// [`dumps_sorted`] (`sort_keys=True`, `separators=(",", ":")`,
/// `ensure_ascii=True`, `allow_nan=False`, no trailing newline — the shim
/// appends the `"\n"` at its two call sites) EXCEPT the string arm, which
/// carries CPython's full lone-surrogate `\uXXXX` escape (the frozen
/// `lone_surrogate_instrument` corpus case) instead of the slice-2a refusal.
/// A DISTINCT entry so neither law bends the other
/// (DEC-RUST-CASCADE-RESULTS-001 slice-2b recorded decision). Residual
/// divergence: a lone-surrogate dict KEY still refuses at extraction
/// (CPython would escape it; unreachable — every top100 payload key is
/// writer-minted ASCII or a `json.loads` product of the artifact's own
/// ASCII bytes).
pub fn dumps_sorted_surrogatepass(obj: &Bound<'_, PyAny>) -> PyResult<String> {
    let mut out = String::new();
    write_json_value(obj, &mut out, StrPolicy::EscapeSurrogates)?;
    Ok(out)
}

fn write_json_value(v: &Bound<'_, PyAny>, out: &mut String, policy: StrPolicy) -> PyResult<()> {
    if v.is_none() {
        out.push_str("null");
    } else if v.is_instance_of::<PyString>() {
        match v.extract::<String>() {
            Ok(s) => push_json_string(out, &s),
            // A lone-surrogate str fails UTF-8 extraction: under the slice-2a
            // law a loud refusal (the slice-1 disclosed divergence envelope);
            // under the top100 law CPython's own per-code-unit escape.
            Err(err) => {
                if policy == StrPolicy::RefuseSurrogates {
                    return Err(err);
                }
                let encoded = v.call_method1("encode", ("utf-16-le", "surrogatepass"))?;
                let bytes: Vec<u8> = encoded.extract()?;
                push_json_string_utf16le(out, &bytes);
            }
        }
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
            write_json_value(&item?, out, policy)?;
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
            write_json_value(value, out, policy)?;
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
    fn utf16le_escaper_matches_cpython_lone_surrogate_law() {
        // "BAD-" + lone U+D800 + "-S😀" as UTF-16-LE code units
        // (`str.encode("utf-16-le", "surrogatepass")`): CPython renders the
        // lone surrogate as its own \ud800 escape and the astral char as its
        // surrogate pair — the frozen `lone_surrogate_instrument` law.
        let mut bytes: Vec<u8> = Vec::new();
        for unit in [
            0x42u16, 0x41, 0x44, 0x2d, 0xd800, 0x2d, 0x53, 0xd83d, 0xde00,
        ] {
            bytes.extend_from_slice(&unit.to_le_bytes());
        }
        let mut out = String::new();
        push_json_string_utf16le(&mut out, &bytes);
        assert_eq!(out, "\"BAD-\\ud800-S\\ud83d\\ude00\"");
    }

    #[test]
    fn utf16le_escaper_short_escapes_and_ascii() {
        // The short-escape + printable-ASCII arms are the push_json_string
        // law verbatim (byte-parity of the two escapers on valid strings).
        let s = "a\"b\\c\u{08}\t\n\u{0c}\r\u{7f}é";
        let mut bytes: Vec<u8> = Vec::new();
        for unit in s.encode_utf16() {
            bytes.extend_from_slice(&unit.to_le_bytes());
        }
        let mut via_utf16 = String::new();
        push_json_string_utf16le(&mut via_utf16, &bytes);
        let mut via_str = String::new();
        push_json_string(&mut via_str, s);
        assert_eq!(via_utf16, via_str);
        assert_eq!(via_utf16, "\"a\\\"b\\\\c\\b\\t\\n\\f\\r\\u007f\\u00e9\"");
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
