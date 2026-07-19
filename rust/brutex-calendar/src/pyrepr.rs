//! CPython value-rendering twins — `repr()`, `str(int)`, float repr,
//! `str(timedelta)` — for JSON-decoded values (DEC-RUST-CALENDAR-LEAVES-001).
//!
//! Every settle-evidence refusal message embeds the offending VALUE exactly
//! as CPython renders it (`{value!r}` / `{value}` in the Python f-strings),
//! so this module hand-mirrors the CPython algorithms for the value space a
//! JSON document can produce: `None`/`True`/`False`, arbitrary-precision
//! ints, shortest-repr floats (incl. `inf` from an overflowing token),
//! `str` repr with CPython's quote-choice + escape rules (printability per
//! `str.isprintable()` — Unicode general categories), and recursive
//! list/dict reprs in document order.
//!
//! Honest envelope (Golden Rule #10): printability rides the
//! `unicode-properties` crate's Unicode tables, which may trail or lead
//! CPython 3.12's Unicode 15.0 by a version — a codepoint newly (un)assigned
//! between the two tables would repr differently. No such codepoint appears
//! in any pinned message; the goldens gate the realistic space.

use serde_json::{Number, Value};
use unicode_properties::{GeneralCategory, UnicodeGeneralCategory};

/// `type(x).__name__` for a JSON-decoded Python value.
pub fn json_type_name(v: &Value) -> &'static str {
    match v {
        Value::Null => "NoneType",
        Value::Bool(_) => "bool",
        Value::Number(n) => {
            if number_token_is_int(&n.to_string()) {
                "int"
            } else {
                "float"
            }
        }
        Value::String(_) => "str",
        Value::Array(_) => "list",
        Value::Object(_) => "dict",
    }
}

/// True iff the raw JSON number token decodes to a Python `int`
/// (`json.loads` yields `int` iff the token has no `.` and no exponent).
pub fn number_token_is_int(token: &str) -> bool {
    !token.contains(['.', 'e', 'E'])
}

/// Python `str(int)` / `repr(int)` of a canonical JSON int token.
///
/// JSON forbids leading zeros, so the only non-canonical spelling is `-0`
/// (which `json.loads` folds to `0`).
pub fn py_int_str(token: &str) -> String {
    if token == "-0" {
        "0".to_owned()
    } else {
        token.to_owned()
    }
}

/// Python `repr(float)` — shortest round-trip digits under CPython's
/// fixed-vs-scientific formatting law (`format_float_short`, type `'r'`).
pub fn py_float_repr(f: f64) -> String {
    if f.is_nan() {
        return "nan".to_owned();
    }
    if f.is_infinite() {
        return if f > 0.0 { "inf" } else { "-inf" }.to_owned();
    }
    if f == 0.0 {
        return if f.is_sign_negative() { "-0.0" } else { "0.0" }.to_owned();
    }
    let neg = f < 0.0;
    let a = f.abs();
    // Rust's `{:e}` renders the shortest round-trip mantissa (the same
    // shortest-digits algorithm CPython's repr uses) as `d[.ddd]e<exp>`.
    let sci = format!("{a:e}");
    let (mant, exp_txt) = sci
        .split_once('e')
        .expect("`{:e}` always renders an exponent");
    let exp: i32 = exp_txt.parse().expect("`{:e}` exponent is an integer");
    let digits: String = mant.chars().filter(|c| *c != '.').collect();
    // value == 0.<digits> * 10^decpt  (CPython's decpt convention).
    let decpt = exp + 1;
    let nd = i32::try_from(digits.len()).expect("shortest f64 digits fit i32");
    let mut out = String::new();
    if neg {
        out.push('-');
    }
    if !(-3..=16).contains(&decpt) {
        // Scientific: d[.ddd]e±XX (exponent signed, at least two digits).
        out.push_str(&digits[0..1]);
        if nd > 1 {
            out.push('.');
            out.push_str(&digits[1..]);
        }
        let e10 = decpt - 1;
        out.push('e');
        out.push(if e10 >= 0 { '+' } else { '-' });
        let ae = e10.abs();
        if ae < 10 {
            out.push('0');
        }
        out.push_str(&ae.to_string());
    } else if decpt <= 0 {
        out.push_str("0.");
        for _ in 0..(-decpt) {
            out.push('0');
        }
        out.push_str(&digits);
    } else if decpt >= nd {
        out.push_str(&digits);
        for _ in 0..(decpt - nd) {
            out.push('0');
        }
        out.push_str(".0");
    } else {
        let cut = usize::try_from(decpt).expect("0 < decpt < nd");
        out.push_str(&digits[..cut]);
        out.push('.');
        out.push_str(&digits[cut..]);
    }
    out
}

/// CPython `str.isprintable()` for one char: printable unless the general
/// category is Cc/Cf/Cs/Co/Cn/Zl/Zp/Zs — with the ASCII space exempted.
fn is_py_printable(c: char) -> bool {
    if c == ' ' {
        return true;
    }
    !matches!(
        c.general_category(),
        GeneralCategory::Control
            | GeneralCategory::Format
            | GeneralCategory::Surrogate
            | GeneralCategory::PrivateUse
            | GeneralCategory::Unassigned
            | GeneralCategory::LineSeparator
            | GeneralCategory::ParagraphSeparator
            | GeneralCategory::SpaceSeparator
    )
}

/// Python `repr(str)` — CPython `unicode_repr`: single quotes unless the
/// string holds `'` and no `"`; `\\`, the quote, `\t`/`\n`/`\r` escaped;
/// other non-printables as `\xXX` / `\uXXXX` / `\UXXXXXXXX` (lowercase hex).
pub fn py_str_repr(s: &str) -> String {
    let has_sq = s.contains('\'');
    let has_dq = s.contains('"');
    let quote = if has_sq && !has_dq { '"' } else { '\'' };
    let mut out = String::with_capacity(s.len() + 2);
    out.push(quote);
    for c in s.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\t' => out.push_str("\\t"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if is_py_printable(c) => out.push(c),
            c => {
                let cp = c as u32;
                if cp < 0x100 {
                    out.push_str(&format!("\\x{cp:02x}"));
                } else if cp < 0x1_0000 {
                    out.push_str(&format!("\\u{cp:04x}"));
                } else {
                    out.push_str(&format!("\\U{cp:08x}"));
                }
            }
        }
    }
    out.push(quote);
    out
}

/// Python `repr()` of a JSON-decoded value (`None` for an absent field —
/// Python's `payload.get(...)` yields `None`).
pub fn py_json_repr(v: Option<&Value>) -> String {
    match v {
        None | Some(Value::Null) => "None".to_owned(),
        Some(Value::Bool(true)) => "True".to_owned(),
        Some(Value::Bool(false)) => "False".to_owned(),
        Some(Value::Number(n)) => py_number_repr(n),
        Some(Value::String(s)) => py_str_repr(s),
        Some(Value::Array(items)) => {
            let inner: Vec<String> = items.iter().map(|x| py_json_repr(Some(x))).collect();
            format!("[{}]", inner.join(", "))
        }
        Some(Value::Object(map)) => {
            if map.is_empty() {
                return "{}".to_owned();
            }
            let inner: Vec<String> = map
                .iter()
                .map(|(k, x)| format!("{}: {}", py_str_repr(k), py_json_repr(Some(x))))
                .collect();
            format!("{{{}}}", inner.join(", "))
        }
    }
}

/// Python `repr()` of one JSON number (int → canonical digits; float →
/// shortest repr of the `float(token)` value — an overflowing token reads
/// `inf`, exactly like CPython's json float path).
pub fn py_number_repr(n: &Number) -> String {
    let token = n.to_string();
    if number_token_is_int(&token) {
        py_int_str(&token)
    } else {
        py_float_repr(token.parse::<f64>().unwrap_or(f64::NAN))
    }
}

/// Python `str(timedelta)` over a total-microseconds value — the exact
/// `timedelta.__str__` law: normalize to `(days, 0..86400 s, 0..1e6 µs)`,
/// render `[D day(s), ]H:MM:SS[.ffffff]` (hours unpadded; the `day`/`days`
/// plural on `abs(days) != 1`).
pub fn py_timedelta_str(total_us: i128) -> String {
    const DAY_US: i128 = 86_400_000_000;
    let days = total_us.div_euclid(DAY_US);
    let rem = total_us.rem_euclid(DAY_US);
    let secs = rem / 1_000_000;
    let us = rem % 1_000_000;
    let hh = secs / 3600;
    let mm = (secs % 3600) / 60;
    let ss = secs % 60;
    let mut out = format!("{hh}:{mm:02}:{ss:02}");
    if days != 0 {
        let plural = if days.abs() != 1 { "s" } else { "" };
        out = format!("{days} day{plural}, {out}");
    }
    if us != 0 {
        out.push_str(&format!(".{us:06}"));
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn float_repr_matches_cpython_battery() {
        // Every expected string below is CPython 3.12 `repr()` output
        // (probe-verified 2026-07-18).
        let cases: &[(f64, &str)] = &[
            (1.0, "1.0"),
            (-1.0, "-1.0"),
            (0.0, "0.0"),
            (-0.0, "-0.0"),
            (0.1, "0.1"),
            (0.5, "0.5"),
            (1.5, "1.5"),
            (100.0, "100.0"),
            (1234.5, "1234.5"),
            (1e15, "1000000000000000.0"),
            (1e16, "1e+16"),
            (1.5e16, "1.5e+16"),
            (1e-4, "0.0001"),
            (1e-5, "1e-05"),
            (1.5e300, "1.5e+300"),
            (5e-324, "5e-324"),
            (f64::INFINITY, "inf"),
            (f64::NEG_INFINITY, "-inf"),
            (2.5e-10, "2.5e-10"),
            (9999999999999998.0, "9999999999999998.0"),
        ];
        for (f, want) in cases {
            assert_eq!(py_float_repr(*f), *want, "repr({f})");
        }
        assert_eq!(py_float_repr(f64::NAN), "nan");
    }

    #[test]
    fn str_repr_matches_cpython_battery() {
        let cases: &[(&str, &str)] = &[
            ("abc", "'abc'"),
            ("it's", "\"it's\""),
            ("say \"hi\"", "'say \"hi\"'"),
            ("both ' and \"", "'both \\' and \"'"),
            ("a\x01b\tc\nd\\e", "'a\\x01b\\tc\\nd\\\\e'"),
            ("a\u{7f}b", "'a\\x7fb'"),
            ("a\u{a0}b", "'a\\xa0b'"),
            ("a\u{200b}b", "'a\\u200bb'"),
            ("café ☃", "'café ☃'"),
            ("", "''"),
            ("\r", "'\\r'"),
        ];
        for (s, want) in cases {
            assert_eq!(py_str_repr(s), *want, "repr({s:?})");
        }
    }

    #[test]
    fn int_str_folds_negative_zero() {
        assert_eq!(py_int_str("-0"), "0");
        assert_eq!(py_int_str("0"), "0");
        assert_eq!(py_int_str("-12"), "-12");
        assert_eq!(
            py_int_str("1000000000000000000000000000000"),
            "1000000000000000000000000000000"
        );
    }

    #[test]
    fn timedelta_str_matches_cpython_battery() {
        // str(timedelta(...)) probe-verified values.
        assert_eq!(py_timedelta_str(0), "0:00:00");
        assert_eq!(py_timedelta_str(19_800_000_000), "5:30:00"); // +05:30
        assert_eq!(py_timedelta_str(-19_800_000_000), "-1 day, 18:30:00"); // -05:30
        assert_eq!(py_timedelta_str(1_000_000), "0:00:01");
        assert_eq!(py_timedelta_str(19_815_123_456), "5:30:15.123456");
        assert_eq!(py_timedelta_str(-1), "-1 day, 23:59:59.999999");
        assert_eq!(py_timedelta_str(22_500_000_000), "6:15:00"); // +05:75 folded
        assert_eq!(
            py_timedelta_str(-2 * 86_400_000_000 - 1_000_000),
            "-3 days, 23:59:59"
        );
        assert_eq!(py_timedelta_str(86_399_999_999 + 1), "1 day, 0:00:00");
    }

    #[test]
    fn json_repr_nested_matches_cpython() {
        let v: Value =
            serde_json::from_str(r#"{"k": [true, null, 1.0, "x"], "m": {}}"#).expect("valid json");
        assert_eq!(
            py_json_repr(Some(&v)),
            "{'k': [True, None, 1.0, 'x'], 'm': {}}"
        );
        assert_eq!(py_json_repr(None), "None");
        let neg_zero: Value = serde_json::from_str("-0.0").expect("valid json");
        assert_eq!(py_json_repr(Some(&neg_zero)), "-0.0");
        let neg_zero_int: Value = serde_json::from_str("-0").expect("valid json");
        assert_eq!(py_json_repr(Some(&neg_zero_int)), "0");
        let huge: Value = serde_json::from_str("1e400").expect("valid json");
        assert_eq!(py_json_repr(Some(&huge)), "inf");
    }
}
