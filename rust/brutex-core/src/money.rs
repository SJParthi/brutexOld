//! The DEC-MONEY-2DP-001 money laws as exact decimal-STRING arithmetic.
//!
//! Byte-locked to the CPython `Decimal` oracle in `brutex/money.py`:
//!
//! * [`round_money_2dp`] — `Decimal.quantize(Decimal("0.01"),
//!   rounding=ROUND_HALF_UP)`: round at two fractional digits, ties (and
//!   everything at-or-past the half) AWAY FROM ZERO, proper carry
//!   propagation (`"9.995"` → `"10.00"`), the input's sign PRESERVED even
//!   on a zero result (`"-0.001"` → `"-0.00"` — probed CPython behavior,
//!   2026-07-17: `Decimal("-0.001").quantize(...)` → `Decimal("-0.00")`).
//! * [`rupees_to_paisa`] — round first, then an EXACT ×100 digit shift to
//!   an integer string. A negative-zero round result collapses to `"0"`
//!   (CPython: `int(Decimal("-0.00") * 100)` == `0` — the ONE place the
//!   sign drops, probed + pinned below).
//! * [`paisa_to_rupees`] — an EXACT ÷100 digit shift of an integer paisa
//!   string to the fixed-2dp rupee string (`"5"` → `"0.05"`).
//! * [`format_inr`] — `f"₹{paisa_to_rupees(p):,.2f}"`: the `₹` prefix +
//!   Python's WESTERN comma-grouping-by-3 (probed: `Decimal("-1234.56")`
//!   formats as `"-1,234.56"` — NOT Indian lakh/crore grouping; byte-parity
//!   with today's behavior wins over any "correct INR" notion, per the
//!   milestone brief).
//!
//! ## The rounding law — HALF_UP, never half-even
//!
//! CPython's `ROUND_HALF_UP` is "round to nearest, ties away from zero".
//! On digit strings that is exactly: look at the FIRST discarded fractional
//! digit (position 3); `>= 5` → increment the kept magnitude, `<= 4` → keep
//! it (the discarded tail beyond position 3 can never flip the verdict:
//! first digit >= 5 means the discarded value is >= half an ulp, first
//! digit <= 4 means it is < half). This is deliberately NOT
//! `trading-core`'s `round_half_even` (a DIFFERENT law — banker's
//! rounding); the divergence is negative-pinned below (`"1.005"` → `"1.01"`
//! here where half-even yields `"1.00"`).
//!
//! ## TWIN-IMPL COORDINATION (audit-session addendum)
//!
//! `round_money` is applied to EVERY stored OHLC value at the lake-write
//! boundary, so this law is run-identity / stored-bytes critical. The SAME
//! half-away-from-zero law has a second non-Python impl: the
//! polars-columnar `_snap_price_expr` contract in
//! `brutex/data/parquet_writer.py` (`floor(d*100 + 0.5)/100` for
//! `d >= 0`, `ceil(d*100 - 0.5)/100` for `d < 0`), whose pre-snapped
//! output the `rust/brutex-lake` render/merge seams consume. Any change
//! must move BOTH impls plus the Python oracle. The cross-crate
//! consistency pin ([`tests::cross_crate_snap_corpus_consistency_pin`])
//! locks this module to the SAME literal corpus
//! `tests/golden/test_writer_vectorized_snap_parity.py` pins (`_EDGE_CASES`
//! rendered through `str(float)` — the seed the legacy
//! `Decimal(str(op))` parsed).
//!
//! ## Input grammar (what the Python facade sends)
//!
//! Rupee inputs arrive as `format(decimal_value, "f")` — CPython's
//! fixed-point render: optional leading `-`, >= 1 ASCII int digit, an
//! optional `.` followed by >= 1 ASCII fraction digit. No `+`, no
//! exponent, no grouping, no whitespace, no Unicode digits (probed:
//! `format(Decimal("1E+2"), "f")` == `"100"`, `format(Decimal("1E-7"),
//! "f")` == `"0.0000001"`). Paisa inputs arrive as `str(int)`: optional
//! `-` then ASCII digits. ANYTHING else is a parse error (`Err`), which
//! the FFI shell maps to `ValueError` — a facade DEGRADE member, so a
//! malformed crossing serves the Python oracle, never a wrong value.
//! Inputs longer than [`MAX_INPUT_LEN`] are refused the same way (the
//! facade's eligibility gate bounds exponents far below this — the cap is
//! defense-in-depth, not a reachable production path).

/// Defensive input-length cap (the facade gates ~90 chars; see module docs).
pub const MAX_INPUT_LEN: usize = 512;

/// A parsed fixed-point decimal: sign + ASCII int digits + ASCII fraction
/// digits (possibly empty when the input carried no `.`).
struct Fixed<'a> {
    neg: bool,
    int_digits: &'a [u8],
    frac_digits: &'a [u8],
}

/// Parse the `format(d, "f")` fixed-point grammar (module docs) or `Err`.
fn parse_fixed(s: &str) -> Result<Fixed<'_>, String> {
    if s.len() > MAX_INPUT_LEN {
        return Err(format!(
            "money input too long ({} > {MAX_INPUT_LEN} bytes)",
            s.len()
        ));
    }
    let bytes = s.as_bytes();
    let (neg, rest) = match bytes.first() {
        Some(b'-') => (true, &bytes[1..]),
        _ => (false, bytes),
    };
    let int_len = rest.iter().take_while(|b| b.is_ascii_digit()).count();
    if int_len == 0 {
        return Err(format!("not a fixed-point decimal string: {s:?}"));
    }
    let int_digits = &rest[..int_len];
    let tail = &rest[int_len..];
    let frac_digits: &[u8] = match tail.first() {
        None => &[],
        Some(b'.') => {
            let frac = &tail[1..];
            if frac.is_empty() || !frac.iter().all(u8::is_ascii_digit) {
                return Err(format!("not a fixed-point decimal string: {s:?}"));
            }
            frac
        }
        Some(_) => return Err(format!("not a fixed-point decimal string: {s:?}")),
    };
    Ok(Fixed {
        neg,
        int_digits,
        frac_digits,
    })
}

/// Parse a `str(int)` paisa string: optional `-`, >= 1 ASCII digit, or `Err`.
fn parse_int(s: &str) -> Result<(bool, &[u8]), String> {
    if s.len() > MAX_INPUT_LEN {
        return Err(format!(
            "paisa input too long ({} > {MAX_INPUT_LEN} bytes)",
            s.len()
        ));
    }
    let bytes = s.as_bytes();
    let (neg, rest) = match bytes.first() {
        Some(b'-') => (true, &bytes[1..]),
        _ => (false, bytes),
    };
    if rest.is_empty() || !rest.iter().all(u8::is_ascii_digit) {
        return Err(format!("not an integer string: {s:?}"));
    }
    Ok((neg, rest))
}

/// Increment an ASCII-digit magnitude in place, with carry (`"999"` → `"1000"`).
fn increment(digits: &mut Vec<u8>) {
    for d in digits.iter_mut().rev() {
        if *d == b'9' {
            *d = b'0';
        } else {
            *d += 1;
            return;
        }
    }
    digits.insert(0, b'1');
}

/// Strip leading zeros from an ASCII-digit magnitude, keeping at least one.
fn strip_leading_zeros(digits: &[u8]) -> &[u8] {
    let first_nonzero = digits.iter().take_while(|&&b| b == b'0').count();
    if first_nonzero == digits.len() {
        &digits[digits.len() - 1..]
    } else {
        &digits[first_nonzero..]
    }
}

/// The HALF_UP core: round `int_digits.frac_digits` at 2 fractional digits,
/// away from zero, returning `(int_magnitude, [frac0, frac1])` — int part
/// leading-zero-stripped (>= 1 digit).
fn round2(int_digits: &[u8], frac_digits: &[u8]) -> (Vec<u8>, [u8; 2]) {
    let f0 = frac_digits.first().copied().unwrap_or(b'0');
    let f1 = frac_digits.get(1).copied().unwrap_or(b'0');
    // ROUND_HALF_UP verdict: first discarded digit >= 5 → away from zero
    // (module docs — the tail beyond it can never flip the verdict).
    let bump = frac_digits.get(2).is_some_and(|&b| b >= b'5');
    let mut magnitude: Vec<u8> = Vec::with_capacity(int_digits.len() + 3);
    magnitude.extend_from_slice(int_digits);
    magnitude.push(f0);
    magnitude.push(f1);
    if bump {
        increment(&mut magnitude);
    }
    let split = magnitude.len() - 2;
    let frac = [magnitude[split], magnitude[split + 1]];
    let int_part = strip_leading_zeros(&magnitude[..split]).to_vec();
    (int_part, frac)
}

/// Render `sign int.frac2` — the `str(Decimal)` shape of a quantized value
/// (exponent −2 never renders scientific; sign preserved even on zero).
fn render_fixed2(neg: bool, int_part: &[u8], frac: [u8; 2]) -> String {
    let mut out = String::with_capacity(int_part.len() + 4);
    if neg {
        out.push('-');
    }
    // SAFETY-free: every byte is an ASCII digit by construction.
    out.push_str(std::str::from_utf8(int_part).expect("ascii digits"));
    out.push('.');
    out.push(frac[0] as char);
    out.push(frac[1] as char);
    out
}

/// `round_money` twin: 2dp HALF_UP (away from zero), sign preserved.
pub fn round_money_2dp(s: &str) -> Result<String, String> {
    let fixed = parse_fixed(s)?;
    let (int_part, frac) = round2(fixed.int_digits, fixed.frac_digits);
    Ok(render_fixed2(fixed.neg, &int_part, frac))
}

/// `rupees_to_paisa` twin: round to 2dp, then the exact ×100 digit shift.
///
/// A zero result drops the sign (`int(Decimal("-0.00") * 100)` == `0`).
pub fn rupees_to_paisa(s: &str) -> Result<String, String> {
    let fixed = parse_fixed(s)?;
    let (int_part, frac) = round2(fixed.int_digits, fixed.frac_digits);
    let mut magnitude = int_part;
    magnitude.push(frac[0]);
    magnitude.push(frac[1]);
    let stripped = strip_leading_zeros(&magnitude);
    let is_zero = stripped == b"0";
    let mut out = String::with_capacity(stripped.len() + 1);
    if fixed.neg && !is_zero {
        out.push('-');
    }
    out.push_str(std::str::from_utf8(stripped).expect("ascii digits"));
    Ok(out)
}

/// Split an integer paisa magnitude into `(int_digits, [frac0, frac1])` —
/// the exact ÷100 digit shift (pad to >= 3 digits with leading zeros).
fn paisa_split(digits: &[u8]) -> (Vec<u8>, [u8; 2]) {
    let mut padded: Vec<u8> = Vec::with_capacity(digits.len().max(3));
    padded.extend(std::iter::repeat_n(
        b'0',
        3usize.saturating_sub(digits.len()),
    ));
    padded.extend_from_slice(digits);
    let split = padded.len() - 2;
    let frac = [padded[split], padded[split + 1]];
    let int_part = strip_leading_zeros(&padded[..split]).to_vec();
    (int_part, frac)
}

/// `paisa_to_rupees` twin: exact ÷100 to the fixed-2dp rupee string.
pub fn paisa_to_rupees(s: &str) -> Result<String, String> {
    let (neg, digits) = parse_int(s)?;
    let (int_part, frac) = paisa_split(digits);
    Ok(render_fixed2(neg, &int_part, frac))
}

/// `format_inr` twin: `₹` + sign + comma-grouped-by-3 int part + 2dp.
pub fn format_inr(s: &str) -> Result<String, String> {
    let (neg, digits) = parse_int(s)?;
    let (int_part, frac) = paisa_split(digits);
    let mut out = String::with_capacity(int_part.len() + int_part.len() / 3 + 8);
    out.push('₹');
    if neg {
        out.push('-');
    }
    // Western 3-grouping from the right (probed `:,` behavior — module docs).
    let lead = int_part.len() % 3;
    for (i, &d) in int_part.iter().enumerate() {
        if i != 0 && (i % 3) == (lead % 3) {
            out.push(',');
        }
        out.push(d as char);
    }
    out.push('.');
    out.push(frac[0] as char);
    out.push(frac[1] as char);
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round_money_rounding_boundaries() {
        // Literal golden corpus — expected strings are the PROBED CPython
        // `Decimal.quantize(Decimal("0.01"), ROUND_HALF_UP)` outputs
        // (probe transcript 2026-07-17, recorded in the module docs).
        let cases: &[(&str, &str)] = &[
            ("1.005", "1.01"),   // half away from zero
            ("-1.005", "-1.01"), // negative half away from zero
            ("1.004", "1.00"),   // below half
            ("0.005", "0.01"),
            ("-0.005", "-0.01"),
            ("0.995", "1.00"),
            ("9.995", "10.00"), // carry across the point
            ("-9.995", "-10.00"),
            ("99.995", "100.00"),
            ("999.999", "1000.00"),
            ("123456789.995", "123456790.00"),
            ("-0.001", "-0.00"), // sign PRESERVED on a zero result
            ("-0", "-0.00"),
            ("0", "0.00"),
            ("0.00001", "0.00"),
            ("1.0100", "1.01"), // trailing zeros beyond 2dp
            ("100", "100.00"),  // no fraction at all
            ("14020.7999", "14020.80"),
            ("2.675", "2.68"),
            ("1.0049999999", "1.00"), // long tail below half — NOT bumped
            ("22000.00", "22000.00"),
            ("12187.3", "12187.30"),
        ];
        for (input, expected) in cases {
            assert_eq!(
                round_money_2dp(input).unwrap(),
                *expected,
                "round_money_2dp({input:?})"
            );
        }
    }

    /// Cross-crate consistency pin (audit-session addendum): the SAME
    /// literal corpus `tests/golden/test_writer_vectorized_snap_parity.py`
    /// pins for the polars-columnar `_snap_price_expr` twin. TWO entry
    /// kinds (review-r1 precision): most entries are its `_EDGE_CASES`
    /// floats rendered through Python `str(float)` — the seed the legacy
    /// `Decimal(str(op))` parsed; the `"99999.99499999999"` entry is the
    /// RAW pre-collapse decimal string (`str(float("99999.99499999999"))`
    /// COLLAPSES to `"99999.995"`, pinned separately above and rounding
    /// UP, while the raw string's third fractional digit 4 rounds DOWN —
    /// the collapse divergence itself is pinned in
    /// `tests/golden/test_money_rounding_golden.py`). Any change to the
    /// ONE half-up law must move this crate, that expression AND the
    /// Python oracle together (module docs).
    #[test]
    fn cross_crate_snap_corpus_consistency_pin() {
        let cases: &[(&str, &str)] = &[
            ("14020.7999", "14020.80"),
            ("100.005", "100.01"),
            ("100.015", "100.02"),
            ("1.005", "1.01"),
            ("1.004", "1.00"),
            ("2.675", "2.68"),
            ("99999.995", "100000.00"),
            ("100000.125", "100000.13"),
            ("0.0", "0.00"),
            ("-0.0", "-0.00"),
            ("-1.005", "-1.01"),
            ("-100000.125", "-100000.13"),
            ("12187.3", "12187.30"),
            ("22000.0", "22000.00"), // str(22000.00) == "22000.0"
            ("0.05", "0.05"),
            ("0.04", "0.04"),
            ("0.00499999999", "0.00"),
            ("0.12499999999", "0.12"),
            ("1.00499999999", "1.00"),
            ("-0.00499999999", "-0.00"),
            ("2.67499999999", "2.67"),
            ("99999.99499999999", "99999.99"),
            ("0.00500000001", "0.01"),
            ("1.00500000001", "1.01"),
            ("0.004999999999999", "0.00"),
            ("0.005000000000001", "0.01"),
        ];
        for (input, expected) in cases {
            assert_eq!(
                round_money_2dp(input).unwrap(),
                *expected,
                "snap-corpus round_money_2dp({input:?})"
            );
        }
    }

    /// Negative pin (audit-session addendum 1c): this is HALF_UP (away
    /// from zero), NOT `trading-core`'s `round_half_even`. On these ties
    /// the two laws DIVERGE — locked to the HALF_UP answer.
    #[test]
    fn half_even_divergence_negative_pin() {
        // half-even("1.005") == "1.00" (0 even); ours is "1.01".
        assert_eq!(round_money_2dp("1.005").unwrap(), "1.01");
        // half-even("0.125") == "0.12" (2 even); ours is "0.13".
        assert_eq!(round_money_2dp("0.125").unwrap(), "0.13");
        // half-even("-0.125") == "-0.12"; ours is "-0.13".
        assert_eq!(round_money_2dp("-0.125").unwrap(), "-0.13");
        // half-even("2.345") == "2.34" (4 even); ours is "2.35".
        assert_eq!(round_money_2dp("2.345").unwrap(), "2.35");
    }

    #[test]
    fn rupees_to_paisa_exact_shift() {
        let cases: &[(&str, &str)] = &[
            ("0.01", "1"),
            ("0.05", "5"),
            ("1.00", "100"),
            ("1234.56", "123456"),
            ("-0.50", "-50"),
            ("14020.7999", "1402080"),
            ("1.005", "101"),
            ("-1.005", "-101"),
            ("0", "0"),
            ("-0.001", "0"), // negative zero COLLAPSES on the int path
            ("-0", "0"),
            ("9.995", "1000"),
            ("123456789.995", "12345679000"),
        ];
        for (input, expected) in cases {
            assert_eq!(
                rupees_to_paisa(input).unwrap(),
                *expected,
                "rupees_to_paisa({input:?})"
            );
        }
    }

    #[test]
    fn paisa_to_rupees_exact_shift() {
        let cases: &[(&str, &str)] = &[
            ("1", "0.01"),
            ("5", "0.05"),
            ("99", "0.99"),
            ("100", "1.00"),
            ("101", "1.01"),
            ("123456", "1234.56"),
            ("-50", "-0.50"),
            ("-5", "-0.05"),
            ("0", "0.00"),
            ("100000000", "1000000.00"),
            ("007", "0.07"), // tolerant of leading zeros (unreachable via str(int))
        ];
        for (input, expected) in cases {
            assert_eq!(
                paisa_to_rupees(input).unwrap(),
                *expected,
                "paisa_to_rupees({input:?})"
            );
        }
    }

    #[test]
    fn format_inr_grouping() {
        let cases: &[(&str, &str)] = &[
            ("5", "₹0.05"),
            ("0", "₹0.00"),
            ("123456", "₹1,234.56"),
            ("-123456", "₹-1,234.56"),
            ("100000000", "₹1,000,000.00"),
            ("-50", "₹-0.50"),
            ("99999", "₹999.99"),
            ("9999999", "₹99,999.99"),
            ("123456789012", "₹1,234,567,890.12"),
            ("100000", "₹1,000.00"),
            ("99900", "₹999.00"),
        ];
        for (input, expected) in cases {
            assert_eq!(
                format_inr(input).unwrap(),
                *expected,
                "format_inr({input:?})"
            );
        }
    }

    #[test]
    fn carry_propagation() {
        // The increment helper across every carry shape.
        let mut d = b"999".to_vec();
        increment(&mut d);
        assert_eq!(d, b"1000");
        let mut d = b"1099".to_vec();
        increment(&mut d);
        assert_eq!(d, b"1100");
        let mut d = b"0".to_vec();
        increment(&mut d);
        assert_eq!(d, b"1");
    }

    #[test]
    fn parse_errors_are_errs_never_panics() {
        for bad in [
            "", "-", ".", "1.", ".5", "1..2", "1.2.3", "abc", "1e5", "1E+2", "+1", " 1", "1 ",
            "--1", "-.5", "1,000", "NaN", "Infinity", "١٢", "1.٢", "¹2", "1.5abc",
        ] {
            assert!(
                round_money_2dp(bad).is_err(),
                "round_money_2dp({bad:?}) must Err"
            );
            assert!(
                rupees_to_paisa(bad).is_err(),
                "rupees_to_paisa({bad:?}) must Err"
            );
        }
        for bad in [
            "", "-", "1.5", "abc", "+1", " 1", "1 ", "--1", "0x10", "١٢", "1_0",
        ] {
            assert!(
                paisa_to_rupees(bad).is_err(),
                "paisa_to_rupees({bad:?}) must Err"
            );
            assert!(format_inr(bad).is_err(), "format_inr({bad:?}) must Err");
        }
    }

    #[test]
    fn oversized_input_refused() {
        let long = "1".repeat(MAX_INPUT_LEN + 1);
        assert!(round_money_2dp(&long).is_err());
        assert!(rupees_to_paisa(&long).is_err());
        assert!(paisa_to_rupees(&long).is_err());
        assert!(format_inr(&long).is_err());
        // Exactly AT the cap parses fine (boundary law).
        let at_cap = "1".repeat(MAX_INPUT_LEN);
        assert!(round_money_2dp(&at_cap).is_ok());
    }
}
