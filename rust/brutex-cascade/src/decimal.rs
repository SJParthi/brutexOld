//! The `round_money` → int64-paisa law over decimal rupee strings.
//!
//! The Rust twin of `brutex.money.rupees_to_paisa` (DEC-SPOT-PRICE-2DP-001 /
//! DEC-MONEY-2DP-001): the rupee amount is snapped to the 2-decimal tick grid
//! with `ROUND_HALF_UP` (half away from zero — CPython `decimal`'s
//! `ROUND_HALF_UP`, never banker's rounding, never truncation), then `× 100`
//! exactly. The Python shims marshal `str(Decimal)` across the FFI, so this
//! parser accepts the FULL `Decimal.__str__` grammar: optional sign, digits
//! with an optional point, optional `E±exp` scientific notation (CPython
//! prints e.g. `Decimal("1E+2")` in that form).
//!
//! Non-finite tokens (`NaN` / `Infinity`) are refused with an error — the
//! Python law raises `decimal.InvalidOperation` from `quantize` there; the
//! shims surface this as the same loud failure class (a corrupt price never
//! silently converts).

/// The parse outcome of a `Decimal.__str__` token, in half-up paisa
/// magnitude form (slice 2d factored the finite-digit walk out of
/// [`rupees_str_to_paisa`] so the baseline `_price_str` quantize law can
/// share ONE parser — CLAUDE.md §11 COMMON).
pub enum ParsedPaisa {
    /// A finite value: sign + `round_half_up(|value| * 100)` exactly.
    Finite { neg: bool, magnitude: i128 },
    /// A QUIET NaN token (`NaN` / `-NaN` / `NaN<payload>`): CPython
    /// `quantize` propagates it silently (the baseline `_price_str` law),
    /// while the paisa conversion refuses it.
    QuietNan,
    /// `sNaN` / `Infinity` (either sign): CPython `quantize` raises
    /// `InvalidOperation`; the paisa conversion refuses too.
    NonFinite,
}

/// Parse a `Decimal.__str__` token into [`ParsedPaisa`] (magnitude in
/// exact half-up paisa), or an error message for a malformed token.
pub fn parse_paisa(s: &str) -> Result<ParsedPaisa, String> {
    let t = s.trim();
    let (neg, t) = match t.as_bytes().first() {
        Some(b'-') => (true, &t[1..]),
        Some(b'+') => (false, &t[1..]),
        _ => (false, t),
    };
    let lower = t.to_ascii_lowercase();
    if lower.starts_with("nan") {
        return Ok(ParsedPaisa::QuietNan);
    }
    if lower.starts_with("snan") || lower.starts_with("inf") {
        return Ok(ParsedPaisa::NonFinite);
    }
    // Split mantissa / exponent.
    let (mantissa, exp_part) = match t.find(['e', 'E']) {
        Some(i) => (&t[..i], Some(&t[i + 1..])),
        None => (t, None),
    };
    let exp10: i64 = match exp_part {
        None => 0,
        Some(e) => e
            .parse::<i64>()
            .map_err(|_| format!("malformed decimal exponent in {s:?}"))?,
    };
    let (int_part, frac_part) = match mantissa.find('.') {
        Some(i) => (&mantissa[..i], &mantissa[i + 1..]),
        None => (mantissa, ""),
    };
    if int_part.is_empty() && frac_part.is_empty() {
        return Err(format!("malformed decimal {s:?}"));
    }
    if !int_part.bytes().all(|b| b.is_ascii_digit())
        || !frac_part.bytes().all(|b| b.is_ascii_digit())
    {
        return Err(format!("malformed decimal {s:?}"));
    }
    // digits = int_part ++ frac_part, value = digits * 10^(exp10 - len(frac)).
    let mut digits: Vec<u8> = Vec::with_capacity(int_part.len() + frac_part.len());
    digits.extend(int_part.bytes().map(|b| b - b'0'));
    digits.extend(frac_part.bytes().map(|b| b - b'0'));
    let mut scale = exp10 - frac_part.len() as i64; // value = D * 10^scale
    // Target: paisa = value * 100 = D * 10^(scale + 2), ROUND_HALF_UP at
    // integer precision.
    scale += 2;
    // Strip leading zeros (keeps arithmetic bounded).
    let first_nonzero = digits.iter().position(|&d| d != 0).unwrap_or(digits.len());
    let digits = &digits[first_nonzero..];
    if digits.is_empty() {
        return Ok(ParsedPaisa::Finite { neg, magnitude: 0 });
    }
    let value: i128 = if scale >= 0 {
        let mut acc: i128 = 0;
        for &d in digits {
            acc = acc
                .checked_mul(10)
                .and_then(|a| a.checked_add(i128::from(d)))
                .ok_or_else(|| format!("decimal {s:?} overflows the paisa domain"))?;
        }
        let mut k = scale;
        while k > 0 {
            acc = acc
                .checked_mul(10)
                .ok_or_else(|| format!("decimal {s:?} overflows the paisa domain"))?;
            k -= 1;
        }
        acc
    } else {
        // Keep the first len+scale digits; ROUND_HALF_UP on the first dropped
        // digit (half away from zero — the sign is applied at the end, so
        // "away from zero" is "up" on the magnitude).
        let keep = digits.len() as i64 + scale;
        if keep <= 0 {
            // The entire mantissa is fractional dust below 1 paisa; the
            // half-up decision keys on the FIRST fractional digit — which is
            // a real digit only when keep == 0 exactly.
            let round_up = keep == 0 && digits[0] >= 5;
            i128::from(round_up)
        } else {
            let keep = keep as usize;
            let mut acc: i128 = 0;
            for &d in &digits[..keep] {
                acc = acc
                    .checked_mul(10)
                    .and_then(|a| a.checked_add(i128::from(d)))
                    .ok_or_else(|| format!("decimal {s:?} overflows the paisa domain"))?;
            }
            if digits[keep] >= 5 {
                acc += 1;
            }
            acc
        }
    };
    Ok(ParsedPaisa::Finite {
        neg,
        magnitude: value,
    })
}

/// Parse a `Decimal.__str__` rupee string and return exact int paisa
/// (`round_money(x) * 100`), or an error message for a malformed /
/// non-finite token.
pub fn rupees_str_to_paisa(s: &str) -> Result<i64, String> {
    match parse_paisa(s)? {
        ParsedPaisa::QuietNan | ParsedPaisa::NonFinite => {
            Err(format!("non-finite decimal {s:?} cannot convert to paisa"))
        }
        ParsedPaisa::Finite { neg, magnitude } => {
            let signed = if neg { -magnitude } else { magnitude };
            i64::try_from(signed)
                .map_err(|_| format!("decimal {s:?} overflows the int64 paisa domain"))
        }
    }
}

/// The baseline `_price_str` core — `str(round_money(value))` over the
/// value's own `Decimal.__str__` token (DEC-RUST-CASCADE-RESULTS-001
/// slice 2d): the 2-dp `ROUND_HALF_UP` quantize rendered exactly as
/// CPython prints the quantized `Decimal` — always two fraction digits,
/// no exponent form, the sign preserved on negative zero
/// (`round_money(Decimal("-0.004"))` → `"-0.00"`, probe-verified).
///
/// A QUIET NaN propagates through CPython `quantize` silently, so the
/// token passes through VERBATIM (`"NaN"` / `"-NaN"`, probe-verified);
/// `sNaN` / `Infinity` and a quantized coefficient beyond the ambient
/// 28-digit context precision return `Err` — the caller raises the exact
/// CPython `decimal.InvalidOperation`.
pub fn quantize_2dp_string(s: &str) -> Result<String, String> {
    match parse_paisa(s)? {
        ParsedPaisa::QuietNan => Ok(s.to_string()),
        ParsedPaisa::NonFinite => Err(format!(
            "non-finite decimal {s:?} cannot quantize to the 2-dp grid"
        )),
        ParsedPaisa::Finite { neg, magnitude } => {
            // CPython `quantize` refuses a result coefficient longer than
            // the context precision (28 significant digits): the quantized
            // coefficient IS the paisa magnitude.
            if magnitude >= 10_i128.pow(28) {
                return Err(format!(
                    "decimal {s:?} quantized to more than 28 digits (InvalidOperation)"
                ));
            }
            let units = magnitude / 100;
            let cents = magnitude % 100;
            let sign = if neg { "-" } else { "" };
            Ok(format!("{sign}{units}.{cents:02}"))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn exact_two_dp() {
        assert_eq!(rupees_str_to_paisa("100.00"), Ok(10_000));
        assert_eq!(rupees_str_to_paisa("100.50"), Ok(10_050));
        assert_eq!(rupees_str_to_paisa("0.01"), Ok(1));
        assert_eq!(rupees_str_to_paisa("-2.50"), Ok(-250));
    }

    #[test]
    fn half_up_at_the_third_place() {
        // round_money(ROUND_HALF_UP): 1.005 → 1.01 → 101 paisa (never 100).
        assert_eq!(rupees_str_to_paisa("1.005"), Ok(101));
        assert_eq!(rupees_str_to_paisa("1.004"), Ok(100));
        assert_eq!(rupees_str_to_paisa("1.0049999"), Ok(100));
        // Half away from ZERO on negatives: -1.005 → -1.01.
        assert_eq!(rupees_str_to_paisa("-1.005"), Ok(-101));
    }

    #[test]
    fn scientific_notation_decimal_str_forms() {
        assert_eq!(rupees_str_to_paisa("1E+2"), Ok(10_000));
        assert_eq!(rupees_str_to_paisa("1.5E+1"), Ok(1_500));
        assert_eq!(rupees_str_to_paisa("5E-3"), Ok(1)); // 0.005 → 0.01
        assert_eq!(rupees_str_to_paisa("4E-3"), Ok(0)); // 0.004 → 0.00
        assert_eq!(rupees_str_to_paisa("5E-4"), Ok(0)); // 0.0005 → 0.00
    }

    #[test]
    fn integers_and_zero_forms() {
        assert_eq!(rupees_str_to_paisa("0"), Ok(0));
        assert_eq!(rupees_str_to_paisa("0.000"), Ok(0));
        assert_eq!(rupees_str_to_paisa("-0.00"), Ok(0));
        assert_eq!(rupees_str_to_paisa("12345"), Ok(1_234_500));
    }

    #[test]
    fn refuses_non_finite_and_junk() {
        assert!(rupees_str_to_paisa("NaN").is_err());
        assert!(rupees_str_to_paisa("-Infinity").is_err());
        assert!(rupees_str_to_paisa("sNaN").is_err());
        assert!(rupees_str_to_paisa("abc").is_err());
        assert!(rupees_str_to_paisa("").is_err());
        assert!(rupees_str_to_paisa("1.2.3").is_err());
    }

    #[test]
    fn quantize_2dp_matches_round_money_corpus() {
        // The frozen baseline `price_str` corpus family (slice 2d).
        assert_eq!(quantize_2dp_string("1.005").unwrap(), "1.01");
        assert_eq!(quantize_2dp_string("-1.005").unwrap(), "-1.01");
        assert_eq!(quantize_2dp_string("1.0049").unwrap(), "1.00");
        assert_eq!(quantize_2dp_string("0").unwrap(), "0.00");
        assert_eq!(quantize_2dp_string("1E+2").unwrap(), "100.00");
        assert_eq!(quantize_2dp_string("123.4").unwrap(), "123.40");
        assert_eq!(quantize_2dp_string("0.005").unwrap(), "0.01");
        assert_eq!(quantize_2dp_string("2.675").unwrap(), "2.68");
        assert_eq!(quantize_2dp_string("99999999.99").unwrap(), "99999999.99");
    }

    #[test]
    fn quantize_2dp_zero_sign_and_non_finite() {
        // Negative zero keeps its sign — round_money(Decimal("-0.004")) →
        // "-0.00" (probe-verified against CPython decimal).
        assert_eq!(quantize_2dp_string("-0.004").unwrap(), "-0.00");
        assert_eq!(quantize_2dp_string("-0").unwrap(), "-0.00");
        assert_eq!(quantize_2dp_string("0E+2").unwrap(), "0.00");
        // Quiet NaN passes through verbatim; sNaN / Infinity refuse.
        assert_eq!(quantize_2dp_string("NaN").unwrap(), "NaN");
        assert_eq!(quantize_2dp_string("-NaN").unwrap(), "-NaN");
        assert!(quantize_2dp_string("sNaN").is_err());
        assert!(quantize_2dp_string("Infinity").is_err());
        assert!(quantize_2dp_string("-Infinity").is_err());
        // The 28-digit context-precision wall (1E+26 rupees = 29 paisa
        // digits → CPython quantize raises InvalidOperation).
        assert!(quantize_2dp_string("1E+26").is_err());
        assert_eq!(
            quantize_2dp_string("9999999999999999999999999.99").unwrap(),
            "9999999999999999999999999.99"
        );
    }

    #[test]
    fn overflow_is_loud() {
        assert!(rupees_str_to_paisa("1E+40").is_err());
        // i64::MAX paisa ≈ 9.22e16 rupees — just beyond must refuse.
        assert!(rupees_str_to_paisa("92233720368547758.08").is_err());
        assert_eq!(
            rupees_str_to_paisa("92233720368547758.07"),
            Ok(9_223_372_036_854_775_807)
        );
    }
}
