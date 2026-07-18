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

/// Parse a `Decimal.__str__` rupee string and return exact int paisa
/// (`round_money(x) * 100`), or an error message for a malformed /
/// non-finite token.
pub fn rupees_str_to_paisa(s: &str) -> Result<i64, String> {
    let t = s.trim();
    let (neg, t) = match t.as_bytes().first() {
        Some(b'-') => (true, &t[1..]),
        Some(b'+') => (false, &t[1..]),
        _ => (false, t),
    };
    let lower = t.to_ascii_lowercase();
    if lower.starts_with("nan") || lower.starts_with("snan") || lower.starts_with("inf") {
        return Err(format!("non-finite decimal {s:?} cannot convert to paisa"));
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
        return Ok(0);
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
    let signed = if neg { -value } else { value };
    i64::try_from(signed).map_err(|_| format!("decimal {s:?} overflows the int64 paisa domain"))
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
