//! The `brutex/config/backup_env.py` verify-subset SHAPE law (milestone 3).
//!
//! DEC-CONFIG-RUST-LEAVES-001 (MIGRATION.md #1943). ONE law —
//! [`verify_subset_valid`], the `_valid_verify_subset` twin: the restic
//! `--read-data-subset` shape validation (DEC-BACKUP-OFFBOX-001 round-7 /
//! round-9 — restic refuses a malformed flag with the SAME rc=1 as
//! genuine corruption, so an unvalidated typo would page repo-corrupt
//! every night). The REST of `backup_env.py` deliberately stays Python —
//! the resolver's fail-safe malformed-value WARNs are observability side
//! effects woven into the law (the honest seam split recorded in the
//! facade docstring).
//!
//! ## The three restic forms (mirroring the Python regexes byte-for-byte)
//!
//! * `n/t` — `^([0-9]+)/([0-9]+)$` with `1 <= n <= t <= 256`
//!   (arbitrary-length digit runs: CPython `int()` is a bigint, so the
//!   twins strip leading zeros and saturate past `u128` — any such value
//!   fails the 256 bound identically);
//! * `x%` — `^([0-9]+(?:\.[0-9]*)?|\.[0-9]+)%$` with `0 < float(x) <=
//!   100` (plain decimals only; CPython `float()` and Rust
//!   `str::parse::<f64>` are both correctly-rounded, and a huge digit
//!   run overflows to `+inf` on BOTH sides — probed 2026-07-17 — which
//!   the `<= 100` bound then rejects identically);
//! * `<int><b|k|m|g|t>` — `^([0-9]+)([bBkKmMgGtT])$` with `0 <
//!   value × multiplier <= 2^63 − 1` (the Go `int64` overflow bound,
//!   round-9; binary multipliers per restic `ui.ParseBytes`).
//!
//! ## The CPython `re` `$` quirk (probed, mirrored)
//!
//! `$` in a CPython pattern also matches just before ONE trailing
//! newline, so `_valid_verify_subset("1/2\n")` is True while
//! `"1/2\n\n"` is False. The twin strips at most one trailing `\n`
//! before matching — the exact equivalence (probed 2026-07-17).
//!
//! ONLY a string crosses in and a `bool` crosses out; the facade gates
//! non-ASCII to the oracle and every `Err` maps to `ValueError` at the
//! FFI shell (a facade DEGRADE member).

/// Defensive input-length cap (a real subset token is < 32 chars).
pub const MAX_VALUE_LEN: usize = 4096;

/// Go `int64::MAX` — the restic `ui.ParseBytes` overflow bound (round-9).
const MAX_SIZE_BYTES: u128 = (1u128 << 63) - 1;

/// restic `checkFlags`: `--read-data-subset=n/t` requires `t <= 256`.
const MAX_BUCKETS: u128 = 256;

/// restic `checkFlags`: `--read-data-subset=x%` requires `0 < x <= 100`.
const MAX_PERCENT: f64 = 100.0;

/// Parse an all-ASCII-digit run as CPython `int()` would, saturating at
/// `u128::MAX` for values past 38 digits (every law bound is tiny, so a
/// saturated value fails each bound exactly like the real bigint).
fn parse_digits_saturating(digits: &str) -> Option<u128> {
    if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    let stripped = digits.trim_start_matches('0');
    if stripped.is_empty() {
        return Some(0);
    }
    if stripped.len() > 38 {
        return Some(u128::MAX);
    }
    Some(stripped.parse::<u128>().unwrap_or(u128::MAX))
}

/// The `x%` mantissa grammar: `[0-9]+(\.[0-9]*)?` or `\.[0-9]+`.
fn is_percent_mantissa(s: &str) -> bool {
    let bytes = s.as_bytes();
    match bytes.iter().position(|&b| b == b'.') {
        None => !s.is_empty() && bytes.iter().all(u8::is_ascii_digit),
        Some(dot) => {
            let (int_part, frac_part) = (&bytes[..dot], &bytes[dot + 1..]);
            if !int_part.iter().all(u8::is_ascii_digit) || !frac_part.iter().all(u8::is_ascii_digit)
            {
                return false;
            }
            // `[0-9]+\.[0-9]*` (frac may be empty) or `\.[0-9]+`.
            !int_part.is_empty() || !frac_part.is_empty()
        }
    }
}

/// The `_valid_verify_subset` twin (module docs).
pub fn verify_subset_valid(value: &str) -> Result<bool, String> {
    if value.len() > MAX_VALUE_LEN {
        return Err(format!(
            "verify_subset_valid: input exceeds {MAX_VALUE_LEN} bytes ({} bytes)",
            value.len()
        ));
    }
    if !value.is_ascii() {
        return Err(
            "verify_subset_valid: non-ASCII input (the facade eligibility gate routes these to the oracle)"
                .to_string(),
        );
    }
    // The CPython `$`-before-one-trailing-newline quirk (module docs).
    let v = value.strip_suffix('\n').unwrap_or(value);

    // n/t form.
    if let Some(slash) = v.find('/') {
        let (n_raw, t_raw) = (&v[..slash], &v[slash + 1..]);
        if let (Some(n), Some(t)) = (
            parse_digits_saturating(n_raw),
            parse_digits_saturating(t_raw),
        ) {
            return Ok(1 <= n && n <= t && t <= MAX_BUCKETS);
        }
        return Ok(false);
    }

    // x% form.
    if let Some(mantissa) = v.strip_suffix('%') {
        if is_percent_mantissa(mantissa) {
            // Both CPython float() and Rust f64 parse are correctly
            // rounded (huge runs → +inf on both) — module docs.
            let x: f64 = mantissa.parse().map_err(|_| {
                "verify_subset_valid: mantissa passed the grammar but failed the f64 parse"
                    .to_string()
            })?;
            return Ok(0.0 < x && x <= MAX_PERCENT);
        }
        return Ok(false);
    }

    // size form: [0-9]+ then exactly one suffix char.
    if v.len() >= 2 {
        let (digits, suffix) = v.split_at(v.len() - 1);
        let multiplier: u128 = match suffix.as_bytes()[0] {
            b'b' | b'B' => 1,
            b'k' | b'K' => 1 << 10,
            b'm' | b'M' => 1 << 20,
            b'g' | b'G' => 1 << 30,
            b't' | b'T' => 1 << 40,
            _ => return Ok(false),
        };
        if let Some(value) = parse_digits_saturating(digits) {
            let total = value.saturating_mul(multiplier);
            return Ok(0 < total && total <= MAX_SIZE_BYTES);
        }
        return Ok(false);
    }

    Ok(false)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn valid(v: &str) -> bool {
        verify_subset_valid(v).unwrap()
    }

    #[test]
    fn nt_form_bounds_match_python() {
        assert!(valid("1/1"));
        assert!(valid("1/256"));
        assert!(valid("256/256"));
        assert!(valid("0001/0004")); // leading zeros — bigint law
        assert!(!valid("0/5"));
        assert!(!valid("5/4"));
        assert!(!valid("1/257"));
        assert!(!valid("1/999999999999999999999999999999999999999999")); // saturated
        assert!(!valid("999999999999999999999999999999999999999999/256"));
        assert!(!valid("1/2/3"));
        assert!(!valid("1/"));
        assert!(!valid("/2"));
        assert!(!valid("-1/2"));
        assert!(!valid("1/2 "));
    }

    #[test]
    fn percent_form_bounds_match_python() {
        assert!(valid("2%"));
        assert!(valid("0.5%"));
        assert!(valid(".5%"));
        assert!(valid("1.%"));
        assert!(valid("007.5%"));
        assert!(valid("100%"));
        assert!(valid("100.0%"));
        assert!(!valid("0%"));
        assert!(!valid("0.0%"));
        assert!(!valid("100.0001%"));
        assert!(!valid("2e1%")); // exotic spellings deliberately refused
        assert!(!valid("%"));
        assert!(!valid(".%"));
        assert!(!valid("-2%"));
        // A huge digit run overflows to +inf on BOTH sides → refused.
        let huge = format!("{}%", "9".repeat(400));
        assert!(!valid(&huge));
    }

    #[test]
    fn size_form_bounds_match_python_including_the_go_int64_overflow() {
        assert!(valid("1b"));
        assert!(valid("500m"));
        assert!(valid("2G"));
        // The round-9 probed boundary: 8388607t fits, 8388608t is 2^63.
        assert!(valid("8388607t"));
        assert!(!valid("8388608t"));
        assert!(!valid("9999999999t"));
        assert!(!valid(&format!("{}b", "9".repeat(40)))); // saturated
        assert!(valid("00000000000000000001b")); // leading zeros, value 1
        assert!(!valid("0b"));
        assert!(!valid("1x"));
        assert!(!valid("b"));
        assert!(!valid("1 b"));
        assert!(!valid("12")); // bare integer is restic-INVALID (probed)
    }

    #[test]
    fn dollar_trailing_newline_quirk_is_mirrored() {
        // Probed: re.match(r"...$", "1/2\n") matches; "1/2\n\n" does not.
        assert!(valid("1/2\n"));
        assert!(!valid("1/2\n\n"));
        assert!(valid("2%\n"));
        assert!(valid("1b\n"));
        assert!(!valid("\n"));
    }

    #[test]
    fn auto_and_none_are_not_this_laws_business() {
        // The resolver short-circuits "auto"/"none" BEFORE the shape law;
        // as bare shapes they are invalid (parity with the Python fn).
        assert!(!valid("auto"));
        assert!(!valid("none"));
        assert!(!valid(""));
    }

    #[test]
    fn refuses_non_ascii_and_oversize() {
        assert!(verify_subset_valid("٢%").is_err());
        assert!(verify_subset_valid(&"1".repeat(MAX_VALUE_LEN + 1)).is_err());
    }
}
