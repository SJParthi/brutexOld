//! Orchestrator SHA-256 audit-chain serializer — the CPython-exact byte twin.
//!
//! DEC-AUDIT-CHAIN-RUST-PORT-001 (slice 9, ADR-0019 / MIGRATION.md #1727): the
//! Rust port of `brutex/orchestrator/audit.py`'s SERIALIZATION + HASH layer —
//! `_canonical_json` (json.dumps `sort_keys=True, separators=(",", ":"),
//! ensure_ascii=False, allow_nan=False`), the `str(float)` shortest-round-trip
//! repr, the `|`-joined SHA-256 preimage, and the 8-key on-disk JSONL line.
//! ALL file I/O, fsync, locking, torn-tail repair, and resume stay Python.
//!
//! ## The bounded value model (envelope)
//!
//! The port is byte-exact for the bounded model that covers EVERY production
//! payload (the call-site census in the slice-9 spec §1.4): `None` / `bool` /
//! `int` within `i64` / finite `f64` / UTF-8 `str` / `list` / `tuple` / `dict`
//! with exact-`str` keys. Anything OUTSIDE the model (big ints, non-finite
//! floats, non-str or subclassed dict keys, subclassed containers, sets,
//! arbitrary objects, lone-surrogate strings) returns
//! [`EnvelopeError`] → the Python seam falls back LOUDLY to the retained
//! Python oracle, which reproduces today's behaviour bit-for-bit (including
//! its raises). "Rust-served" is therefore narrower than "correct" — never
//! the other way round.
//!
//! ## CPython float repr (the hard part)
//!
//! Step 1 uses the `ryu` crate for the shortest round-trip digit string (the
//! same closest-with-ties-to-even rule as CPython's `_Py_dg_dtoa` mode 0);
//! ryu's own NOTATION differs from CPython's, so its output is parsed into
//! `(sign, digits, decpt)` and re-rendered by [`format_repr`] under the
//! CPython `pystrtod.c` format-code-`'r'` law, stated ONCE here in `decpt`
//! terms (decpt = position of the decimal point relative to the digit
//! string: `0.0001` → digits `"1"`, decpt `-3`; `1e16` → digits `"1"`,
//! decpt `17`):
//!
//! * scientific iff `decpt <= -4 || decpt > 16`; else fixed
//!   (boundary vectors pinned inline: decpt 16 fixed `9999999999999998.0`;
//!   decpt 17 sci `1e+16`; decpt −3 fixed `0.0001`; decpt −4 sci `1e-05`);
//! * fixed: place the point, left-pad `0.000…`, append `".0"` when no
//!   fractional digit remains (`1000000000000000.0`);
//! * scientific: mantissa = first digit, then `.` + rest ONLY if more than
//!   one digit (`1e+16`, never `1.0e+16`); exponent = `decpt − 1` with an
//!   explicit sign and a MINIMUM of 2 digits (`1e-05`, `5e-324`,
//!   `1.7976931348623157e+308`).
//!
//! ESCALATION CLAUSE (pre-committed, ADR-0019): if any finite f64 is found
//! where ryu's digit string ≠ CPython's shortest digits, STOP — do not
//! hand-patch digit generation — and escalate with the failing bit pattern.
//! The statistical proof is the hypothesis differential
//! (`tests/property/test_audit_rust_parity.py`) + the 16-sample burn-in +
//! the frozen goldens (`tests/golden/fixtures/audit_chain_parity_*`).

use std::fmt::Write as _;

use pyo3::create_exception;
use pyo3::exceptions::PyException;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple};
use sha2::{Digest, Sha256};

create_exception!(
    fno_math,
    AuditEnvelopeError,
    PyException,
    "A value is outside the Rust audit-chain kernel's bounded model — the \
     Python seam must fall back to the retained Python oracle (loudly)."
);

/// Maximum payload nesting depth the walker accepts. Production payloads
/// nest ≤ ~4 levels; deeper trees refuse to the Python oracle (which owns
/// CPython's own recursion-limit behaviour) instead of risking a stack
/// overflow under the crate's `panic = "abort"` release profile.
pub const MAX_PAYLOAD_DEPTH: usize = 100;

/// Out-of-model refusals — every variant routes the Python seam to the
/// retained Python oracle (never a silently-different byte).
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum EnvelopeError {
    /// NaN / ±Infinity — the oracle's `allow_nan=False` raise (or, for a
    /// non-finite `ts_utc` hash preimage, its `str()` rendering) governs.
    #[error("non-finite float is outside the audit-chain kernel envelope")]
    NonFinite,
    /// A Python int outside `i64` — the oracle renders arbitrary precision.
    #[error("int outside i64 is outside the audit-chain kernel envelope")]
    IntOutOfRange,
    /// The `|` hash-preimage delimiter inside a free-form field (defensive:
    /// `AuditEntry.__post_init__` already rejects it Python-side).
    #[error("audit field {0:?} contains the '|' hash-preimage delimiter")]
    DelimiterInField(&'static str),
    /// A dict key that is not an exact `str` (non-str keys are coerced or
    /// refused by CPython's encoder; subclassed keys can override sorting).
    #[error("non-(exact-)str dict key is outside the audit-chain kernel envelope")]
    NonStrKey,
    /// A string PyO3 cannot extract as UTF-8 (lone surrogates) — today's
    /// Python path also crashes on these; the oracle reproduces the raise.
    #[error("string is not extractable as UTF-8 (lone surrogate?)")]
    NotUtf8,
    /// Any other type (subclassed containers, sets, bytes, objects, …) or a
    /// payload nested past [`MAX_PAYLOAD_DEPTH`].
    #[error("unsupported payload value ({0}) is outside the audit-chain kernel envelope")]
    Unsupported(String),
    /// Internal parse invariant broken (ryu output shape) — never expected;
    /// refusing keeps the seam on the oracle instead of shipping junk.
    #[error("internal float-parse invariant broken: {0}")]
    Internal(String),
}

/// The bounded value model crossing the FFI (module docs). `Map` entries are
/// kept in insertion order at construction and sorted by Unicode code point
/// at render time (Rust `str` byte comparison over UTF-8 IS code-point
/// order, exactly CPython's `sort_keys=True` for every string that can
/// cross the FFI).
#[derive(Debug, Clone, PartialEq)]
pub enum Value {
    Null,
    Bool(bool),
    Int(i64),
    /// Finite by construction at the FFI edge; [`py_float_repr`] re-checks.
    Float(f64),
    Str(String),
    List(Vec<Value>),
    Map(Vec<(String, Value)>),
}

// ────────────────────────────────────────────────────────────────────────
// CPython float repr
// ────────────────────────────────────────────────────────────────────────

/// `repr(float)` / `str(float)` / json.dumps float rendering — CPython-exact.
pub fn py_float_repr(x: f64) -> Result<String, EnvelopeError> {
    if !x.is_finite() {
        return Err(EnvelopeError::NonFinite);
    }
    if x == 0.0 {
        // ±0.0 short-circuit: ryu also renders these, but the sign law is
        // clearest stated directly (repr(-0.0) == "-0.0").
        return Ok(if x.is_sign_negative() {
            "-0.0".to_string()
        } else {
            "0.0".to_string()
        });
    }
    let (neg, digits, decpt) = shortest_digits(x)?;
    Ok(format_repr(neg, &digits, decpt))
}

/// Shortest round-trip digits via ryu, parsed into `(neg, digits, decpt)`.
/// Pure string surgery over ryu's output — no float math.
fn shortest_digits(x: f64) -> Result<(bool, String, i32), EnvelopeError> {
    let mut buf = ryu::Buffer::new();
    let s = buf.format_finite(x);
    parse_decimal(s)
}

/// Parse a ryu-rendered finite non-zero decimal (`"123.45"`, `"1e16"`,
/// `"-1.5e-7"`, `"5e-324"`) into `(neg, shortest_digits, decpt)` with
/// leading + trailing zeros stripped (neither changes the value; CPython's
/// shortest digit string carries neither).
fn parse_decimal(s: &str) -> Result<(bool, String, i32), EnvelopeError> {
    let (neg, rest) = match s.strip_prefix('-') {
        Some(r) => (true, r),
        None => (false, s),
    };
    let (mantissa, exp) = match rest.split_once(['e', 'E']) {
        Some((m, e)) => (
            m,
            e.parse::<i32>()
                .map_err(|_| EnvelopeError::Internal(format!("unparseable exponent in {s:?}")))?,
        ),
        None => (rest, 0i32),
    };
    let (int_part, frac_part) = match mantissa.split_once('.') {
        Some((i, f)) => (i, f),
        None => (mantissa, ""),
    };
    if int_part.is_empty() || !int_part.bytes().all(|b| b.is_ascii_digit()) {
        return Err(EnvelopeError::Internal(format!(
            "unexpected mantissa in {s:?}"
        )));
    }
    let mut digits = String::with_capacity(int_part.len() + frac_part.len());
    digits.push_str(int_part);
    digits.push_str(frac_part);
    let mut decpt = int_part.len() as i32 + exp;
    // Strip leading zeros (each strips one position off decpt).
    let lead = digits.len() - digits.trim_start_matches('0').len();
    if lead == digits.len() {
        return Err(EnvelopeError::Internal(format!(
            "all-zero digits for non-zero {s:?}"
        )));
    }
    digits.drain(..lead);
    decpt -= lead as i32;
    // Strip trailing zeros (value + decpt unchanged; keeps ≥ 1 digit).
    let kept = digits.trim_end_matches('0').len().max(1);
    digits.truncate(kept);
    Ok((neg, digits, decpt))
}

/// The CPython `pystrtod.c` format-code-`'r'` notation layer (module docs).
fn format_repr(neg: bool, digits: &str, decpt: i32) -> String {
    let mut out = String::with_capacity(digits.len() + 8);
    if neg {
        out.push('-');
    }
    if decpt <= -4 || decpt > 16 {
        // Scientific: first digit, "." + rest only when more than one digit,
        // then e{sign}{exponent, minimum 2 digits}.
        out.push_str(&digits[..1]);
        if digits.len() > 1 {
            out.push('.');
            out.push_str(&digits[1..]);
        }
        out.push('e');
        let e = decpt - 1;
        out.push(if e < 0 { '-' } else { '+' });
        let abs = e.unsigned_abs();
        if abs < 10 {
            out.push('0');
        }
        let _ = write!(out, "{abs}");
    } else if decpt <= 0 {
        out.push_str("0.");
        for _ in 0..(-decpt) {
            out.push('0');
        }
        out.push_str(digits);
    } else if decpt as usize >= digits.len() {
        out.push_str(digits);
        for _ in 0..(decpt as usize - digits.len()) {
            out.push('0');
        }
        out.push_str(".0");
    } else {
        out.push_str(&digits[..decpt as usize]);
        out.push('.');
        out.push_str(&digits[decpt as usize..]);
    }
    out
}

// ────────────────────────────────────────────────────────────────────────
// Canonical JSON (json.dumps sort_keys/compact/ensure_ascii=False twin)
// ────────────────────────────────────────────────────────────────────────

/// Render *value* exactly as `audit._canonical_json` renders the same tree.
pub fn canonical_json(value: &Value) -> Result<String, EnvelopeError> {
    let mut out = String::new();
    render_value(value, &mut out)?;
    Ok(out)
}

fn render_value(value: &Value, out: &mut String) -> Result<(), EnvelopeError> {
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(true) => out.push_str("true"),
        Value::Bool(false) => out.push_str("false"),
        Value::Int(i) => {
            let _ = write!(out, "{i}");
        }
        Value::Float(x) => out.push_str(&py_float_repr(*x)?),
        Value::Str(s) => render_str(s, out),
        Value::List(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                render_value(item, out)?;
            }
            out.push(']');
        }
        Value::Map(entries) => {
            let mut sorted: Vec<&(String, Value)> = entries.iter().collect();
            // Unicode code-point key sort == UTF-8 byte order (order-preserving
            // encoding) == CPython sort_keys=True over FFI-crossable strings.
            sorted.sort_by(|a, b| a.0.cmp(&b.0));
            out.push('{');
            for (i, (key, val)) in sorted.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                render_str(key, out);
                out.push(':');
                render_value(val, out)?;
            }
            out.push('}');
        }
    }
    Ok(())
}

/// CPython `json` C-encoder string escaping under `ensure_ascii=False`:
/// `"` and `\` escaped; C0 controls via the 5 shorthands then `\u00xx`
/// (lowercase hex, 4 digits); EVERYTHING else — 0x7F DEL and all
/// non-ASCII included — passes through raw as UTF-8.
fn render_str(s: &str, out: &mut String) {
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
            c if (c as u32) < 0x20 => {
                let _ = write!(out, "\\u{:04x}", c as u32);
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

// ────────────────────────────────────────────────────────────────────────
// Preimage + hash + line
// ────────────────────────────────────────────────────────────────────────

/// SHA-256 hexdigest of the `|`-joined preimage — `AuditEntry.compute_hash`'s
/// exact byte twin: `sha256(str(seq) | prev_hash | str(ts_utc) | actor |
/// action | subject | canonical_json(payload))`, literal `b"|"` joins, UTF-8.
pub fn entry_hash(
    seq: i64,
    prev_hash: &str,
    ts_utc: f64,
    actor: &str,
    action: &str,
    subject: &str,
    payload: &Value,
) -> Result<String, EnvelopeError> {
    for (name, field) in [("actor", actor), ("action", action), ("subject", subject)] {
        // Defensive: __post_init__ already rejects these Python-side; a "|"
        // here must never produce a silently ambiguous preimage.
        if field.contains('|') {
            return Err(EnvelopeError::DelimiterInField(name));
        }
    }
    let ts_repr = py_float_repr(ts_utc)?;
    let payload_json = canonical_json(payload)?;
    let mut h = Sha256::new();
    h.update(seq.to_string().as_bytes());
    h.update(b"|");
    h.update(prev_hash.as_bytes());
    h.update(b"|");
    h.update(ts_repr.as_bytes());
    h.update(b"|");
    h.update(actor.as_bytes());
    h.update(b"|");
    h.update(action.as_bytes());
    h.update(b"|");
    h.update(subject.as_bytes());
    h.update(b"|");
    h.update(payload_json.as_bytes());
    let digest = h.finalize();
    let mut hex = String::with_capacity(64);
    for byte in digest {
        let _ = write!(hex, "{byte:02x}");
    }
    Ok(hex)
}

/// The canonical 8-key JSONL line (no trailing newline) —
/// `AuditEntry.to_json_line`'s exact byte twin. Physical key order after the
/// canonical sort: `action, actor, entry_hash, payload, prev_hash, seq,
/// subject, ts_utc`.
pub fn entry_line(
    seq: i64,
    prev_hash: &str,
    ts_utc: f64,
    actor: &str,
    action: &str,
    subject: &str,
    payload: &Value,
    entry_hash: &str,
) -> Result<String, EnvelopeError> {
    let row = Value::Map(vec![
        ("seq".to_string(), Value::Int(seq)),
        ("prev_hash".to_string(), Value::Str(prev_hash.to_string())),
        ("ts_utc".to_string(), Value::Float(ts_utc)),
        ("actor".to_string(), Value::Str(actor.to_string())),
        ("action".to_string(), Value::Str(action.to_string())),
        ("subject".to_string(), Value::Str(subject.to_string())),
        ("payload".to_string(), payload.clone()),
        ("entry_hash".to_string(), Value::Str(entry_hash.to_string())),
    ]);
    canonical_json(&row)
}

// ────────────────────────────────────────────────────────────────────────
// PyO3 edge — the exact-type walker + the wheel surface
// ────────────────────────────────────────────────────────────────────────

/// Walk a Python object into the bounded [`Value`] model with EXACT type
/// discipline: `bool` BEFORE `int` (Python `True` is an `int` subclass and
/// json renders `true`); `int`/`float`/`str` accept subclasses (CPython's C
/// encoder renders subclasses via the BASE type's repr — the IntEnum case);
/// containers accept EXACT `list`/`tuple`/`dict` only (subclassed containers
/// or non-exact-str keys can change encoder behaviour → refuse to the
/// oracle).
fn extract_value(obj: &Bound<'_, PyAny>, depth: usize) -> Result<Value, EnvelopeError> {
    if depth > MAX_PAYLOAD_DEPTH {
        return Err(EnvelopeError::Unsupported(
            "payload nested too deep".to_string(),
        ));
    }
    if obj.is_none() {
        return Ok(Value::Null);
    }
    if let Ok(b) = obj.cast::<PyBool>() {
        return Ok(Value::Bool(b.is_true()));
    }
    if obj.is_instance_of::<PyInt>() {
        return obj
            .extract::<i64>()
            .map(Value::Int)
            .map_err(|_| EnvelopeError::IntOutOfRange);
    }
    if obj.is_instance_of::<PyFloat>() {
        let x = obj
            .extract::<f64>()
            .map_err(|_| EnvelopeError::Unsupported("float extraction failed".to_string()))?;
        if !x.is_finite() {
            return Err(EnvelopeError::NonFinite);
        }
        return Ok(Value::Float(x));
    }
    if obj.is_instance_of::<PyString>() {
        return obj
            .extract::<String>()
            .map(Value::Str)
            .map_err(|_| EnvelopeError::NotUtf8);
    }
    if let Ok(list) = obj.cast_exact::<PyList>() {
        let mut items = Vec::with_capacity(list.len());
        for item in list.iter() {
            items.push(extract_value(&item, depth + 1)?);
        }
        return Ok(Value::List(items));
    }
    if let Ok(tuple) = obj.cast_exact::<PyTuple>() {
        let mut items = Vec::with_capacity(tuple.len());
        for item in tuple.iter() {
            items.push(extract_value(&item, depth + 1)?);
        }
        return Ok(Value::List(items));
    }
    if let Ok(dict) = obj.cast_exact::<PyDict>() {
        let mut entries = Vec::with_capacity(dict.len());
        for (key, val) in dict.iter() {
            let Ok(key_str) = key.cast_exact::<PyString>() else {
                return Err(EnvelopeError::NonStrKey);
            };
            let key_owned = key_str
                .extract::<String>()
                .map_err(|_| EnvelopeError::NotUtf8)?;
            entries.push((key_owned, extract_value(&val, depth + 1)?));
        }
        return Ok(Value::Map(entries));
    }
    let type_name = obj
        .get_type()
        .name()
        .map(|n| n.to_string())
        .unwrap_or_else(|_| "<unknown type>".to_string());
    Err(EnvelopeError::Unsupported(type_name))
}

fn envelope_to_pyerr(err: EnvelopeError) -> PyErr {
    AuditEnvelopeError::new_err(err.to_string())
}

/// `AuditEntry.compute_hash`'s Rust twin over the bounded value model.
/// Raises `fno_math.AuditEnvelopeError` on ANY out-of-model input — the
/// Python seam then serves the entry from the retained Python oracle.
#[pyfunction(name = "audit_entry_hash")]
pub fn audit_entry_hash_py(
    seq: i64,
    prev_hash: &str,
    ts_utc: f64,
    actor: &str,
    action: &str,
    subject: &str,
    payload: &Bound<'_, PyAny>,
) -> PyResult<String> {
    let value = extract_value(payload, 0).map_err(envelope_to_pyerr)?;
    entry_hash(seq, prev_hash, ts_utc, actor, action, subject, &value).map_err(envelope_to_pyerr)
}

/// `AuditEntry.to_json_line`'s Rust twin (no trailing newline).
#[pyfunction(name = "audit_entry_line")]
#[allow(clippy::too_many_arguments)]
pub fn audit_entry_line_py(
    seq: i64,
    prev_hash: &str,
    ts_utc: f64,
    actor: &str,
    action: &str,
    subject: &str,
    payload: &Bound<'_, PyAny>,
    entry_hash: &str,
) -> PyResult<String> {
    let value = extract_value(payload, 0).map_err(envelope_to_pyerr)?;
    entry_line(
        seq, prev_hash, ts_utc, actor, action, subject, &value, entry_hash,
    )
    .map_err(envelope_to_pyerr)
}

/// `repr(float)` scalar surface — for the golden scalar-vector replays.
#[pyfunction(name = "py_float_repr")]
pub fn py_float_repr_py(x: f64) -> PyResult<String> {
    py_float_repr(x).map_err(envelope_to_pyerr)
}

/// `audit._canonical_json` scalar surface — for the golden vector replays.
#[pyfunction(name = "canonical_json")]
pub fn canonical_json_py(payload: &Bound<'_, PyAny>) -> PyResult<String> {
    let value = extract_value(payload, 0).map_err(envelope_to_pyerr)?;
    canonical_json(&value).map_err(envelope_to_pyerr)
}

// ────────────────────────────────────────────────────────────────────────
// In-crate tests (pure cores only — the PyO3 edge is covered by the Python
// parity/property suites and the golden integration tests)
// ────────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    fn repr_of(x: f64) -> String {
        py_float_repr(x).unwrap()
    }

    #[test]
    fn float_zoo_matches_cpython_repr() {
        // The empirically-pinned CPython 3.12.3 vectors from the slice-9 spec
        // §1.2 + the frozen scalars fixture.
        let vectors: &[(f64, &str)] = &[
            (0.0, "0.0"),
            (-0.0, "-0.0"),
            (0.1, "0.1"),
            (1.5, "1.5"),
            (-1.5, "-1.5"),
            (1e15, "1000000000000000.0"),
            (9999999999999998.0, "9999999999999998.0"),
            (1e16, "1e+16"),
            (1e17, "1e+17"),
            (0.0001, "0.0001"),
            (1e-05, "1e-05"),
            (3e-05, "3e-05"),
            (5e-324, "5e-324"),
            (1.7976931348623157e308, "1.7976931348623157e+308"),
            (1.152921504606847e18, "1.152921504606847e+18"),
            (0.3333333333333333, "0.3333333333333333"),
            (1700000000.0, "1700000000.0"),
            (1718000000.123456, "1718000000.123456"),
            (1700000006.125, "1700000006.125"),
            (0.6666666666666666, "0.6666666666666666"),
        ];
        for (value, expected) in vectors {
            assert_eq!(&repr_of(*value), expected, "repr({value:?})");
        }
    }

    #[test]
    fn notation_switch_boundaries_decpt_law() {
        // decpt <= -4 || decpt > 16 → scientific; the four boundary vectors.
        assert_eq!(repr_of(9999999999999998.0), "9999999999999998.0"); // decpt 16 fixed
        assert_eq!(repr_of(1e16), "1e+16"); // decpt 17 sci
        assert_eq!(repr_of(0.0001), "0.0001"); // decpt -3 fixed
        assert_eq!(repr_of(1e-05), "1e-05"); // decpt -4 sci
    }

    #[test]
    fn exponent_has_minimum_two_digits_and_explicit_sign() {
        assert_eq!(repr_of(1e-5), "1e-05");
        assert_eq!(repr_of(1e22), "1e+22");
        assert_eq!(repr_of(5e-324), "5e-324");
        assert_eq!(repr_of(1.7976931348623157e308), "1.7976931348623157e+308");
    }

    #[test]
    fn float_repr_round_trips() {
        for bits in [
            0x0000_0000_0000_0001_u64, // smallest subnormal
            0x000f_ffff_ffff_ffff,     // largest subnormal
            0x0010_0000_0000_0000,     // smallest normal
            0x3ff0_0000_0000_0001,     // 1.0 + ulp
            0x4340_0000_0000_0001,     // 2^53 + ulp region
            0x7fef_ffff_ffff_ffff,     // f64::MAX
        ] {
            let x = f64::from_bits(bits);
            let rendered = repr_of(x);
            let parsed: f64 = rendered.parse().unwrap();
            assert_eq!(parsed.to_bits(), x.to_bits(), "round trip of {rendered}");
        }
    }

    #[test]
    fn non_finite_refuses() {
        assert_eq!(py_float_repr(f64::NAN), Err(EnvelopeError::NonFinite));
        assert_eq!(py_float_repr(f64::INFINITY), Err(EnvelopeError::NonFinite));
        assert_eq!(
            py_float_repr(f64::NEG_INFINITY),
            Err(EnvelopeError::NonFinite)
        );
    }

    fn map(entries: Vec<(&str, Value)>) -> Value {
        Value::Map(
            entries
                .into_iter()
                .map(|(k, v)| (k.to_string(), v))
                .collect(),
        )
    }

    #[test]
    fn canonical_json_vectors_match_the_frozen_fixture() {
        // Mirrors canonical_json_vectors in audit_chain_parity_scalars.json.
        assert_eq!(canonical_json(&map(vec![])).unwrap(), "{}");
        assert_eq!(
            canonical_json(&map(vec![
                ("b", Value::Int(1)),
                ("a", Value::Int(2)),
                ("A", Value::Int(3)),
            ]))
            .unwrap(),
            "{\"A\":3,\"a\":2,\"b\":1}"
        );
        assert_eq!(
            canonical_json(&map(vec![
                (
                    "outer",
                    map(vec![
                        ("z", Value::Int(1)),
                        (
                            "a",
                            Value::List(vec![Value::Int(3), Value::Int(1), Value::Int(2)])
                        ),
                    ])
                ),
                (
                    "flags",
                    Value::List(vec![Value::Bool(true), Value::Bool(false), Value::Null])
                ),
            ]))
            .unwrap(),
            "{\"flags\":[true,false,null],\"outer\":{\"a\":[3,1,2],\"z\":1}}"
        );
        assert_eq!(
            canonical_json(&map(vec![
                ("currency", Value::Str("₹".to_string())),
                ("name", Value::Str("M&M".to_string())),
                ("combining", Value::Str("é".to_string())),
            ]))
            .unwrap(),
            "{\"combining\":\"é\",\"currency\":\"₹\",\"name\":\"M&M\"}"
        );
        assert_eq!(
            canonical_json(&map(vec![
                ("neg_zero", Value::Float(-0.0)),
                ("sci", Value::Float(1e16)),
                ("sub", Value::Float(5e-324)),
                ("big", Value::Int(1152921504606846976)),
            ]))
            .unwrap(),
            "{\"big\":1152921504606846976,\"neg_zero\":-0.0,\"sci\":1e+16,\"sub\":5e-324}"
        );
        assert_eq!(
            canonical_json(&map(vec![(
                "esc",
                Value::Str("line\nbreak\ttab\"quote".to_string())
            )]))
            .unwrap(),
            "{\"esc\":\"line\\nbreak\\ttab\\\"quote\"}"
        );
    }

    #[test]
    fn c0_controls_escape_lowercase_4digit_and_del_passes_raw() {
        // {"a\x01b": "\x7f₹\n\t\"\\"} → {"ab":"\x7f₹\n\t\"\\"} with
        // \x7f RAW (the empirically-pinned ensure_ascii=False law).
        let value = map(vec![(
            "a\u{01}b",
            Value::Str("\u{7f}₹\n\t\"\\".to_string()),
        )]);
        assert_eq!(
            canonical_json(&value).unwrap(),
            "{\"a\\u0001b\":\"\u{7f}₹\\n\\t\\\"\\\\\"}"
        );
        // Shorthands: \b \t \n \f \r; other C0 → \u00xx lowercase.
        let all = Value::Str("\u{08}\t\n\u{0c}\r\u{00}\u{1f}\u{1e}".to_string());
        assert_eq!(
            canonical_json(&all).unwrap(),
            "\"\\b\\t\\n\\f\\r\\u0000\\u001f\\u001e\""
        );
    }

    #[test]
    fn key_sort_is_code_point_order() {
        // "A" < "a" < "b" < "é" < "₹" (the empirically-verified ordering).
        let value = map(vec![
            ("₹", Value::Int(5)),
            ("é", Value::Int(4)),
            ("b", Value::Int(3)),
            ("a", Value::Int(2)),
            ("A", Value::Int(1)),
        ]);
        assert_eq!(
            canonical_json(&value).unwrap(),
            "{\"A\":1,\"a\":2,\"b\":3,\"é\":4,\"₹\":5}"
        );
    }

    #[test]
    fn entry_hash_matches_the_frozen_genesis_vector() {
        // Entry 0 of audit_chain_parity_golden.jsonl (the frozen fixture).
        let hash = entry_hash(
            0,
            "0000000000000000000000000000000000000000000000000000000000000000",
            1700000000.0,
            "brutex",
            "RUN_START",
            "run:parity-golden",
            &map(vec![]),
        )
        .unwrap();
        assert_eq!(
            hash,
            "c3a174797bd48b5eebf27de1d65b58c9f4432c839f4af083e56812010b7e5f79"
        );
    }

    #[test]
    fn entry_line_matches_the_frozen_genesis_vector() {
        let line = entry_line(
            0,
            "0000000000000000000000000000000000000000000000000000000000000000",
            1700000000.0,
            "brutex",
            "RUN_START",
            "run:parity-golden",
            &map(vec![]),
            "c3a174797bd48b5eebf27de1d65b58c9f4432c839f4af083e56812010b7e5f79",
        )
        .unwrap();
        assert_eq!(
            line,
            "{\"action\":\"RUN_START\",\"actor\":\"brutex\",\"entry_hash\":\"c3a174797bd48b5eebf27de1d65b58c9f4432c839f4af083e56812010b7e5f79\",\"payload\":{},\"prev_hash\":\"0000000000000000000000000000000000000000000000000000000000000000\",\"seq\":0,\"subject\":\"run:parity-golden\",\"ts_utc\":1700000000.0}"
        );
    }

    #[test]
    fn delimiter_in_free_form_field_refuses() {
        let payload = map(vec![]);
        for (idx, fields) in [
            ("a|b", "ACTION", "subj"),
            ("actor", "A|B", "subj"),
            ("actor", "ACT", "s|j"),
        ]
        .iter()
        .enumerate()
        {
            let (actor, action, subject) = *fields;
            let err = entry_hash(0, "0", 1.0, actor, action, subject, &payload).unwrap_err();
            assert!(
                matches!(err, EnvelopeError::DelimiterInField(_)),
                "case {idx}: {err:?}"
            );
        }
    }

    #[test]
    fn non_finite_ts_refuses_before_hashing() {
        let err = entry_hash(0, "0", f64::NAN, "a", "b", "c", &map(vec![])).unwrap_err();
        assert_eq!(err, EnvelopeError::NonFinite);
    }

    #[test]
    fn negative_seq_renders_like_python_str() {
        // A tampered on-disk line can carry a negative seq; str(-1) == "-1".
        let h1 = entry_hash(-1, "p", 1.0, "a", "b", "c", &map(vec![])).unwrap();
        let h2 = entry_hash(1, "p", 1.0, "a", "b", "c", &map(vec![])).unwrap();
        assert_ne!(h1, h2);
        let line = entry_line(-1, "p", 1.0, "a", "b", "c", &map(vec![]), "h").unwrap();
        assert!(line.contains("\"seq\":-1"));
    }

    #[test]
    fn i64_bounds_render_exactly() {
        let line = canonical_json(&Value::List(vec![
            Value::Int(i64::MAX),
            Value::Int(i64::MIN),
            Value::Int(9007199254740993),
        ]))
        .unwrap();
        assert_eq!(
            line,
            "[9223372036854775807,-9223372036854775808,9007199254740993]"
        );
    }
}
