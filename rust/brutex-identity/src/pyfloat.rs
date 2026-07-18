//! CPython-compatible `repr(float)` — the shortest-round-trip float text law.
//!
//! `brutex/strategies/combo.py::_canonicalise_value` canonicalises every
//! finite float via CPython's `repr()` (shortest string that round-trips,
//! CPython `float_repr_style == "short"`). The BLAKE3 run-identity folds that
//! text, so this module must reproduce it byte-for-byte — proven by the
//! hypothesis differential in `tests/unit/test_brutex_identity_kernels.py`
//! (Rust output vs live CPython `repr()`) plus the boundary corpus below.
//!
//! Law (CPython `Python/pystrtod.c` `format_float_short`, format code `'r'`):
//!
//! * shortest correctly-rounded digit string (unique — David Gay / Grisu /
//!   Ryū all agree; Rust's `{:e}` formatting produces the same digits);
//! * FIXED notation iff `-4 < decpt <= 16` (`decpt` = the decimal-point
//!   position: `value = 0.d₁d₂… × 10^decpt`), with `".0"` appended when the
//!   value is integral;
//! * otherwise SCIENTIFIC `d[.ddd]e±NN` with a signed, ≥2-digit exponent and
//!   no trailing `".0"` in the mantissa (`repr(1e16) == "1e+16"`).
//!
//! `-0.0` is NOT handled here: the canonicaliser normalises `-0.0 → +0.0`
//! BEFORE the repr (the results_store / trial_stats signed-zero precedent),
//! and this function mirrors that by rendering any zero input as `"0.0"`.

/// Render a FINITE f64 exactly as CPython `repr()` would.
///
/// Callers guarantee finiteness (the canonicaliser rejects NaN/±inf first);
/// a non-finite input would panic in debug builds and render garbage in
/// release — the PyO3 edge never forwards one.
pub fn py_float_repr(value: f64) -> String {
    debug_assert!(value.is_finite(), "py_float_repr requires a finite f64");
    if value == 0.0 {
        // Both zeros render "0.0" — the canonicaliser has already normalised
        // the sign of zero (`value + 0.0`), so the sign bit is never observable.
        return "0.0".to_string();
    }
    let negative = value < 0.0;
    let magnitude = value.abs();
    // Rust's `LowerExp` renders the SHORTEST digit string that round-trips —
    // the same unique digits CPython's shortest-repr machinery emits — in the
    // fixed shape `d[.ddd]e±?exp` (e.g. "1.5e-5", "1e16").
    let sci = format!("{magnitude:e}");
    let (mantissa, exp_text) = sci
        .split_once('e')
        .expect("LowerExp output always contains 'e'");
    let exponent: i32 = exp_text
        .parse()
        .expect("LowerExp exponent is a plain integer");
    let digits: String = mantissa.chars().filter(|c| *c != '.').collect();
    // decpt: position of the decimal point relative to the digit string —
    // magnitude = 0.digits × 10^decpt. LowerExp mantissa is d.ddd (one digit
    // before the point), so decpt = exponent + 1.
    let decpt = exponent + 1;
    let body = if decpt > -4 && decpt <= 16 {
        render_fixed(&digits, decpt)
    } else {
        render_scientific(&digits, decpt)
    };
    if negative { format!("-{body}") } else { body }
}

/// Fixed-notation body: place the decimal point at `decpt`, zero-padding as
/// needed; an integral value gains the CPython `".0"` suffix.
fn render_fixed(digits: &str, decpt: i32) -> String {
    let n = digits.len() as i32;
    if decpt <= 0 {
        // 0.00…digits
        let zeros = "0".repeat((-decpt) as usize);
        format!("0.{zeros}{digits}")
    } else if decpt >= n {
        // digits00….0 — integral: append ".0" (Py_DTSF_ADD_DOT_0).
        let zeros = "0".repeat((decpt - n) as usize);
        format!("{digits}{zeros}.0")
    } else {
        let (head, tail) = digits.split_at(decpt as usize);
        format!("{head}.{tail}")
    }
}

/// Scientific-notation body: `d[.ddd]e±NN` with a ≥2-digit signed exponent.
fn render_scientific(digits: &str, decpt: i32) -> String {
    let exponent = decpt - 1;
    let (head, tail) = digits.split_at(1);
    let mantissa = if tail.is_empty() {
        head.to_string()
    } else {
        format!("{head}.{tail}")
    };
    let sign = if exponent < 0 { '-' } else { '+' };
    format!("{mantissa}e{sign}{:02}", exponent.abs())
}

#[cfg(test)]
mod tests {
    use super::py_float_repr;

    /// The CPython boundary corpus — every expected string captured from
    /// CPython 3.12 `repr()` (the differential pytest re-proves against the
    /// live interpreter; this in-crate copy keeps `cargo test` self-sufficient).
    #[test]
    fn cpython_boundary_corpus() {
        let cases: &[(f64, &str)] = &[
            (0.0, "0.0"),
            (-0.0, "0.0"), // canonicaliser-normalised sign of zero
            (1.0, "1.0"),
            (-1.0, "-1.0"),
            (1.5, "1.5"),
            (0.1, "0.1"),
            (0.5, "0.5"),
            (1e15, "1000000000000000.0"),
            (1e16, "1e+16"),
            (-1e16, "-1e+16"),
            (1e-4, "0.0001"),
            (1e-5, "1e-05"),
            (1.5e-5, "1.5e-05"),
            (5e-324, "5e-324"),
            (2.2250738585072014e-308, "2.2250738585072014e-308"),
            (1.7976931348623157e308, "1.7976931348623157e+308"),
            (123456789012345.6, "123456789012345.6"),
            (2.5e-10, "2.5e-10"),
            (1e100, "1e+100"),
            (9007199254740993.0, "9007199254740992.0"),
            (1.2345678901234567, "1.2345678901234567"),
            (100.0, "100.0"),
            (1234.5678, "1234.5678"),
            (9999999999999998.0, "9999999999999998.0"),
            (0.000123, "0.000123"),
            (3.0000000000000004, "3.0000000000000004"),
        ];
        for (value, expected) in cases {
            assert_eq!(&py_float_repr(*value), expected, "value {value:?}");
        }
    }

    /// Round-trip law: the rendered text parses back to the exact input bits
    /// (the shortest-repr guarantee this whole module rides on).
    #[test]
    fn round_trips_exactly() {
        for &value in &[
            1.5f64,
            0.1,
            1e16,
            5e-324,
            1.7976931348623157e308,
            123456789012345.6,
            -2.5e-10,
        ] {
            let text = py_float_repr(value);
            let back: f64 = text.parse().expect("repr text parses");
            assert_eq!(back.to_bits(), value.to_bits(), "round trip of {value:?}");
        }
    }
}
