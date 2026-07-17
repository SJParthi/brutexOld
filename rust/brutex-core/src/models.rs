//! The `brutex/models.py::normalise_tick_size` law as exact decimal-STRING
//! arithmetic (slice T35 PR-2 — the models seam).
//!
//! Byte-locked to the CPython oracle `brutex.models._normalise_tick_size_python`:
//! the vendor tick-size normalisation law — type-tagged branch arithmetic,
//! a 2dp `Decimal.quantize` (context-default **ROUND_HALF_EVEN** — banker's
//! rounding, deliberately NOT `money.rs`'s HALF_UP; the divergence is
//! negative-pinned below), then membership in the frozen 7-value
//! `VALID_TICK_SIZES_RUPEES` set.
//!
//! Honesty banner (ADR-0020, mandatory verbatim): expected payoff ≈ 0 —
//! conversion is for uniformity, not speed. `normalise_tick_size` has ZERO
//! production consumers outside `brutex/models.py` itself (exported public
//! API + unit-tested; grep census 2026-07-17) — this twin exists purely for
//! the Rust-first uniformity program, shipped DEFAULT-OFF behind
//! `BRUTEX_CORE_RUST_MODELS`.
//!
//! ## The branch law (mirrors the Python `isinstance` tree VERBATIM)
//!
//! The Python facade performs the TYPE dispatch (a Python-type law — the
//! bool-before-int trap, exact-`Decimal` gating, `str(float)` shortest-repr
//! rendering CANNOT cross an FFI) and sends `(kind, payload)`:
//!
//! * `kind == "decimal"` — payload is `format(d, "f")` (the money.rs
//!   fixed-point grammar): candidate = the value verbatim.
//! * `kind == "str"` — payload is the raw string, pre-gated by the facade
//!   to the strict ASCII `-?\d+(\.\d+)?` grammar (anything richer — Python
//!   `Decimal()` accepts whitespace / exponents / Unicode digits — routes
//!   to the oracle without touching this crate): candidate = the value.
//! * `kind == "int"` — payload is `str(int)`; in `[1, 1000)` the paise
//!   convention applies → candidate = value ÷ 100; outside → a REFUSE
//!   outcome (`tick_refuse:paise_range` — the oracle raises
//!   `TickSizeFormatError` with today's exact message bytes).
//! * `kind == "float"` — payload is `str(float)` pre-gated fixed-point:
//!   candidate = the value, EXCEPT an integral value > 1 (paise-form
//!   floats `5.0` / `10.0` / …) → value ÷ 100. `1.0` is the crossover
//!   preserved as rupees (probed CPython parity: `normalise_tick_size(1.0)
//!   == Decimal("1.00")`, `normalise_tick_size(5.0) == Decimal("0.05")`).
//!
//! Then the common tail: quantize the candidate to 2dp HALF_EVEN and test
//! membership in `{0.01, 0.05, 0.10, 0.25, 0.50, 1.00, 5.00}` — a member
//! returns `Ok(<2dp string>)`, a non-member returns the REFUSE outcome
//! `tick_refuse:not_valid`.
//!
//! ## The REFUSE-vs-FAULT protocol (the facade contract)
//!
//! A law-level refusal (out-of-paise-range int, non-member value) is a
//! LEGITIMATE oracle outcome (`TickSizeFormatError`), not a crate fault —
//! it is signalled as `Err` whose message starts with the
//! [`REFUSE_PREFIX`] marker; the facade serves the ORACLE's raise (exact
//! message bytes stay 100% Python-owned) after burn-in verifying the
//! outcome KIND matches. Any other `Err` (grammar violation, unknown
//! kind) is a FAULT → the facade's degrade-to-oracle path (warn once).
//!
//! ## The rounding law — HALF_EVEN, never HALF_UP
//!
//! CPython's `Decimal.quantize(Decimal("0.01"))` with no explicit rounding
//! uses the context default `ROUND_HALF_EVEN`. Probed 2026-07-17 (the
//! membership-gating ties): `Decimal("0.095")` → `0.10` (ACCEPT — ties to
//! even 0), `Decimal("0.105")` → `0.10` (ACCEPT), `Decimal("0.045")` →
//! `0.04` (refuse — ties to even 4), `Decimal("0.055")` → `0.06` (refuse),
//! `Decimal("0.049999")` → `0.05` (ACCEPT — above-half carries up). The
//! `money.rs` HALF_UP core would send `0.045` → `0.05` (accept) — a
//! DIFFERENT law; the negative pin below locks the divergence.

/// The refuse-outcome marker (the facade checks this exact prefix).
pub const REFUSE_PREFIX: &str = "tick_refuse:";

/// Hard input-length bound — mirrors `money.rs`'s posture; the facade's
/// eligibility gates keep real inputs tiny, so past this length is a fault.
const MAX_INPUT_LEN: usize = 128;

/// The frozen valid set — `brutex.models.VALID_TICK_SIZES_RUPEES` rendered
/// in the canonical 2dp form this module emits.
const VALID_TICK_SIZES_2DP: [&str; 7] = ["0.01", "0.05", "0.10", "0.25", "0.50", "1.00", "5.00"];

/// A parsed fixed-point magnitude: sign + integer digits + fraction digits.
struct Fixed {
    neg: bool,
    int_digits: Vec<u8>,
    frac_digits: Vec<u8>,
}

/// Parse the strict fixed-point grammar `-?\d+(\.\d+)?` (ASCII only) or `Err`
/// (a FAULT — the facade pre-gates, so a violation is a broken contract).
fn parse_fixed(s: &str) -> Result<Fixed, String> {
    if s.len() > MAX_INPUT_LEN {
        return Err(format!(
            "tick input too long ({} > {MAX_INPUT_LEN} bytes)",
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
    let int_digits = rest[..int_len].to_vec();
    let tail = &rest[int_len..];
    let frac_digits: Vec<u8> = match tail.first() {
        None => Vec::new(),
        Some(b'.') => {
            let frac = &tail[1..];
            if frac.is_empty() || !frac.iter().all(u8::is_ascii_digit) {
                return Err(format!("not a fixed-point decimal string: {s:?}"));
            }
            frac.to_vec()
        }
        Some(_) => return Err(format!("not a fixed-point decimal string: {s:?}")),
    };
    Ok(Fixed {
        neg,
        int_digits,
        frac_digits,
    })
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

/// The HALF_EVEN core: round `int_digits.frac_digits` at 2 fractional
/// digits (banker's rounding — the CPython context-default `quantize` law),
/// returning `(int_magnitude, [frac0, frac1])`, int part
/// leading-zero-stripped (>= 1 digit).
///
/// Verdict on the discarded tail (`frac_digits[2..]`):
/// * first discarded digit `> 5` → increment the kept magnitude;
/// * first discarded digit `< 5` → keep;
/// * first discarded digit `== 5`: any non-zero digit AFTER it → increment
///   (strictly above half); an all-zero tail is the exact tie → increment
///   iff the last KEPT digit is ODD (ties to even).
fn round2_half_even(int_digits: &[u8], frac_digits: &[u8]) -> (Vec<u8>, [u8; 2]) {
    let f0 = frac_digits.first().copied().unwrap_or(b'0');
    let f1 = frac_digits.get(1).copied().unwrap_or(b'0');
    let bump = match frac_digits.get(2).copied() {
        None => false,
        Some(d) if d > b'5' => true,
        Some(d) if d < b'5' => false,
        _ => {
            // First discarded digit is exactly '5'.
            if frac_digits[3..].iter().any(|&b| b != b'0') {
                true // strictly above half
            } else {
                (f1 - b'0') % 2 == 1 // exact tie → to even
            }
        }
    };
    let mut magnitude: Vec<u8> = Vec::with_capacity(int_digits.len() + 3);
    magnitude.extend_from_slice(int_digits);
    magnitude.push(f0);
    magnitude.push(f1);
    if bump {
        increment(&mut magnitude);
    }
    let frac = [
        magnitude[magnitude.len() - 2],
        magnitude[magnitude.len() - 1],
    ];
    let int_part = strip_leading_zeros(&magnitude[..magnitude.len() - 2]).to_vec();
    (int_part, frac)
}

/// Render a signed 2dp value (`neg`, int magnitude, two fraction digits) —
/// the sign is preserved even on a zero magnitude (CPython parity:
/// `Decimal("-0.001").quantize(...)` renders `-0.00`; such a value can
/// never be a member, so it only shapes the refuse decision).
fn render_fixed2(neg: bool, int_part: &[u8], frac: [u8; 2]) -> String {
    let mut out = String::with_capacity(int_part.len() + 4);
    if neg {
        out.push('-');
    }
    for &b in int_part {
        out.push(b as char);
    }
    out.push('.');
    out.push(frac[0] as char);
    out.push(frac[1] as char);
    out
}

/// Shift an integer paise magnitude right by two decimal places into a
/// (int digits, frac digits) pair — `"5"` → `("0", "05")`, `"100"` →
/// `("1", "00")`, `"12345"` → `("123", "45")`. Exact by construction.
fn shift_div100(digits: &[u8]) -> (Vec<u8>, Vec<u8>) {
    let mut padded: Vec<u8> = Vec::with_capacity(digits.len().max(3));
    if digits.len() < 3 {
        padded.extend(std::iter::repeat_n(b'0', 3 - digits.len()));
    }
    padded.extend_from_slice(digits);
    let split = padded.len() - 2;
    let int_part = strip_leading_zeros(&padded[..split]).to_vec();
    let frac_part = padded[split..].to_vec();
    (int_part, frac_part)
}

/// `true` iff the parsed magnitude is integral (empty or all-zero fraction).
fn is_integral(fixed: &Fixed) -> bool {
    fixed.frac_digits.iter().all(|&b| b == b'0')
}

/// `true` iff the parsed value is strictly greater than 1 (signed).
fn greater_than_one(fixed: &Fixed) -> bool {
    if fixed.neg {
        return false;
    }
    let int_part = strip_leading_zeros(&fixed.int_digits);
    if int_part.len() > 1 || int_part[0] >= b'2' {
        return true;
    }
    // int part is exactly one digit, 0 or 1: value > 1 iff int == 1 and
    // any non-zero fraction digit exists.
    int_part[0] == b'1' && fixed.frac_digits.iter().any(|&b| b != b'0')
}

/// The full `normalise_tick_size` decision law. `kind` ∈
/// `{"decimal", "str", "int", "float"}` (module docs); returns the
/// canonical 2dp member string, a `tick_refuse:*` `Err` (law refusal), or
/// a plain `Err` (fault).
pub fn normalise_tick_size(kind: &str, payload: &str) -> Result<String, String> {
    let (neg, int_part, frac_part): (bool, Vec<u8>, Vec<u8>) = match kind {
        "int" => {
            if payload.len() > MAX_INPUT_LEN {
                return Err(format!(
                    "tick input too long ({} > {MAX_INPUT_LEN} bytes)",
                    payload.len()
                ));
            }
            let bytes = payload.as_bytes();
            let (neg, digits) = match bytes.first() {
                Some(b'-') => (true, &bytes[1..]),
                _ => (false, bytes),
            };
            if digits.is_empty() || !digits.iter().all(u8::is_ascii_digit) {
                return Err(format!("not an integer string: {payload:?}"));
            }
            // The paise-range law [1, 1000): magnitude 1..=999, positive
            // (a leading-zero-stripped 3-digit magnitude is at most 999).
            let stripped = strip_leading_zeros(digits);
            let in_range = !neg && stripped != b"0" && stripped.len() <= 3;
            if !in_range {
                return Err(format!("{REFUSE_PREFIX}paise_range: {payload}"));
            }
            let (i, f) = shift_div100(stripped);
            (false, i, f)
        }
        "float" => {
            let fixed = parse_fixed(payload)?;
            if is_integral(&fixed) && greater_than_one(&fixed) {
                // Integer-valued floats > 1.0 are paise → ÷ 100.
                let stripped = strip_leading_zeros(&fixed.int_digits).to_vec();
                let (i, f) = shift_div100(&stripped);
                (fixed.neg, i, f)
            } else {
                (fixed.neg, fixed.int_digits, fixed.frac_digits)
            }
        }
        "decimal" | "str" => {
            let fixed = parse_fixed(payload)?;
            (fixed.neg, fixed.int_digits, fixed.frac_digits)
        }
        other => return Err(format!("unknown tick kind: {other:?}")),
    };
    let (int2, frac2) = round2_half_even(&int_part, &frac_part);
    let rendered = render_fixed2(neg, &int2, frac2);
    if VALID_TICK_SIZES_2DP.contains(&rendered.as_str()) {
        Ok(rendered)
    } else {
        Err(format!("{REFUSE_PREFIX}not_valid: {rendered}"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::money;

    fn ok(kind: &str, payload: &str) -> String {
        normalise_tick_size(kind, payload).unwrap()
    }

    fn refuse(kind: &str, payload: &str) -> String {
        let err = normalise_tick_size(kind, payload).unwrap_err();
        assert!(
            err.starts_with(REFUSE_PREFIX),
            "expected refuse, got fault: {err}"
        );
        err
    }

    fn fault(kind: &str, payload: &str) -> String {
        let err = normalise_tick_size(kind, payload).unwrap_err();
        assert!(
            !err.starts_with(REFUSE_PREFIX),
            "expected fault, got refuse: {err}"
        );
        err
    }

    #[test]
    fn decimal_branch_members_verbatim() {
        for s in VALID_TICK_SIZES_2DP {
            assert_eq!(ok("decimal", s), s);
        }
        // format(d, "f") variants normalise to the canonical member.
        assert_eq!(ok("decimal", "0.050"), "0.05");
        assert_eq!(ok("decimal", "0.1"), "0.10");
        assert_eq!(ok("decimal", "1"), "1.00");
        assert_eq!(ok("decimal", "5"), "5.00");
        assert_eq!(ok("decimal", "00.05"), "0.05");
    }

    #[test]
    fn str_branch_mirrors_decimal_branch() {
        assert_eq!(ok("str", "0.05"), "0.05");
        assert_eq!(ok("str", "0.25"), "0.25");
        assert_eq!(ok("str", "1"), "1.00");
        refuse("str", "0.07");
    }

    #[test]
    fn int_branch_paise_convention() {
        // Probed CPython parity: normalise_tick_size(100) == Decimal("1.00").
        assert_eq!(ok("int", "1"), "0.01");
        assert_eq!(ok("int", "5"), "0.05");
        assert_eq!(ok("int", "10"), "0.10");
        assert_eq!(ok("int", "25"), "0.25");
        assert_eq!(ok("int", "50"), "0.50");
        assert_eq!(ok("int", "100"), "1.00");
        assert_eq!(ok("int", "500"), "5.00");
        // In-range but non-member paise → the not_valid refusal.
        let err = refuse("int", "999");
        assert!(err.contains("not_valid"), "{err}");
    }

    #[test]
    fn int_branch_out_of_range_refuses() {
        for payload in ["0", "1000", "5000", "-5"] {
            let err = refuse("int", payload);
            assert!(err.contains("paise_range"), "{err}");
        }
        // Leading-zero magnitudes take the stripped-value law: "0005" is 5.
        assert_eq!(ok("int", "0005"), "0.05");
    }

    #[test]
    fn float_branch_integral_over_one_is_paise() {
        // Probed CPython parity: 5.0 → 0.05, 1.0 → 1.00 (the crossover
        // preserved as rupees), 0.1 → 0.10.
        assert_eq!(ok("float", "5.0"), "0.05");
        assert_eq!(ok("float", "10.0"), "0.10");
        assert_eq!(ok("float", "25.0"), "0.25");
        assert_eq!(ok("float", "100.0"), "1.00");
        assert_eq!(ok("float", "1.0"), "1.00");
        assert_eq!(ok("float", "0.1"), "0.10");
        assert_eq!(ok("float", "0.05"), "0.05");
        // Non-integral > 1 is NOT paise: 1.5 stays 1.50 → refuse.
        let err = refuse("float", "1.5");
        assert!(err.contains("not_valid: 1.50"), "{err}");
        // Negative integral "paise-looking" floats stay rupees (signed >1 law).
        let err = refuse("float", "-5.0");
        assert!(err.contains("not_valid: -5.00"), "{err}");
    }

    #[test]
    fn half_even_tie_pins_probed_cpython() {
        // Probed 2026-07-17 against the CPython oracle (module docs):
        assert_eq!(ok("decimal", "0.095"), "0.10"); // tie → even 0 → ACCEPT
        assert_eq!(ok("decimal", "0.105"), "0.10"); // tie → even 0 → ACCEPT
        assert_eq!(ok("decimal", "0.049999"), "0.05"); // above-half carries
        let err = refuse("decimal", "0.045"); // tie → even 4 → 0.04 refuse
        assert!(err.contains("not_valid: 0.04"), "{err}");
        let err = refuse("decimal", "0.055"); // tie → even 6 → 0.06 refuse
        assert!(err.contains("not_valid: 0.06"), "{err}");
        // Above-half-past-the-5 arm: 0.0450001 → 0.05 (strictly above half).
        assert_eq!(ok("decimal", "0.0450001"), "0.05");
        // Below-half arm: 0.0449 → 0.04 refuse.
        let err = refuse("decimal", "0.0449");
        assert!(err.contains("not_valid: 0.04"), "{err}");
    }

    #[test]
    fn half_even_vs_money_half_up_negative_pin() {
        // The DIVERGENCE lock (module docs): money.rs HALF_UP sends 0.045
        // → "0.05"; this module's HALF_EVEN sends it to 0.04 (refuse).
        assert_eq!(money::round_money_2dp("0.045").unwrap(), "0.05");
        let err = refuse("decimal", "0.045");
        assert!(err.contains("0.04"), "{err}");
    }

    #[test]
    fn negative_zero_sign_preserved_in_refusal_render() {
        // CPython parity: Decimal("-0.001").quantize(...) → -0.00 — never
        // a member, sign visible in the refuse token.
        let err = refuse("decimal", "-0.001");
        assert!(err.contains("not_valid: -0.00"), "{err}");
    }

    #[test]
    fn carry_propagation_at_the_boundary() {
        // 9.999 → HALF_EVEN at 2dp: discarded '9' > 5 → 10.00 → refuse.
        let err = refuse("decimal", "9.999");
        assert!(err.contains("not_valid: 10.00"), "{err}");
    }

    #[test]
    fn grammar_faults_are_faults_not_refusals() {
        for payload in [
            "", "-", ".", "1.", ".5", "1..2", "1e-2", " 0.05", "0,05", "nan",
        ] {
            fault("decimal", payload);
            fault("float", payload);
            fault("str", payload);
        }
        for payload in ["", "-", "1.5", "0x5", " 5"] {
            fault("int", payload);
        }
        fault("bool", "1"); // unknown kind — bool never crosses (Python trap)
        fault("", "1");
        let long = "1".repeat(MAX_INPUT_LEN + 1);
        fault("decimal", &long);
        fault("int", &long);
    }
}
