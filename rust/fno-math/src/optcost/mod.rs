//! Options-cost TAIL kernels (DEC-OPTCOST-TAILS-RUST-001).
//!
//! Rust ports of the UNCLAIMED pure decision math in the options/costs tail
//! modules — the surfaces NO in-flight exec/costs Rust lane touches:
//!
//! * `brutex/costs/constants.py::rate_for_date` — the ONE date-regime lookup
//!   chokepoint behind the four STT/ETC tables ([`regime`]).
//! * `brutex/data/moneyness.py` — classify / atm_offset / derive_strike_step
//!   ([`moneyness`]).
//! * `brutex/options/strike_grid.py` + `strike_rules.py::resolve_offset_strike`
//!   ([`strike`]).
//! * `brutex/options/lot_size_history.py::get_lot_size` +
//!   `brutex/options/expiry_calendar.py` ([`lots`], [`expiry`]).
//!
//! ## Scope law (VERIFIED-RATES-ONLY refusals stay Python)
//!
//! Rust returns REFUSAL CODES only — every exception TYPE + MESSAGE BYTE
//! (`UnverifiedCostRegimeError` per DEC-ETC-REGIME-HISTORY-001, the
//! pre-history `ValueError`s) stays composed by the EXISTING Python lines
//! from the same Python-side row objects. The regime/lot/expiry TABLES stay
//! Python `Final` data (the citation record); they cross the FFI as parallel
//! integer vectors per call. The `pub const` mirror tables in the submodules
//! are CARGO-TEST FIXTURES pinned row-for-row against literals derived by
//! RUNNING the repo Python oracle (2026-07-18, python3.12, the scratchpad
//! `derive_oracle.py` transcript) — never a second runtime source of truth.
//!
//! ## FFI discipline (ints only)
//!
//! i64 paisa in/out for money, i64 `date.toordinal()` ordinals for dates,
//! small int codes for refusal classes, compound outputs as tuples of ints.
//! The ONE deliberate exception is [`strike::optcost_round_to_atm`]'s
//! `spot_price: f64` — the Python function's OWN input domain is a float
//! rupee spot (`strike_grid.py:38`), PyO3 passes the C double through
//! BIT-UNCHANGED, and the kernel replicates CPython's float semantics
//! operation-for-operation (see the module docs there); no money AMOUNT is
//! ever computed in floats.
//!
//! ## Failure mode — `Result`, NEVER a panic
//!
//! Structurally invalid input (length mismatch, non-ascending starts, an
//! out-of-envelope value) returns `Err(String)` → `PyValueError`; the Python
//! seam maps ANY kernel error to warn-once + the Python oracle (refuse-poison
//! discipline — a broken caller can only degrade, never corrupt). Under the
//! crate's `overflow-checks = true` + `panic = "abort"` release profile an
//! unchecked overflow would SIGABRT the host interpreter, so every
//! reachable-overflow site uses checked arithmetic.
//!
//! ## Honesty (ADR-0020 banner)
//!
//! Expected payoff ≈ 0 — these are cold/warm per-trade decision paths, not
//! the ~33B-trial mask path; this module is uniformity, not speed. No
//! speedup SLA is claimed. Each kernel keeps the SAME asymptotics as its
//! Python original.

pub mod expiry;
pub mod lots;
pub mod moneyness;
pub mod regime;
pub mod strike;

// ── Shared refusal/verdict codes (the `(code, value)` tuple convention) ──
//
// Python materializes the exceptions/None from these; the numeric values are
// part of the FFI contract with the `brutex` seam leaf (next stage) and must
// never be renumbered (append-only).

/// The lookup succeeded; the tuple's `value` is the answer.
pub const CODE_OK: i64 = 0;
/// Regime refusal row selected (`rate_bps_x100 is None` Python-side; `-1` on
/// the wire). The tuple's `value` is the SELECTED ROW INDEX so Python can
/// compose `UnverifiedCostRegimeError` from its own `RegimeRow` objects
/// (`brutex/costs/constants.py:372-380` → `brutex/costs/types.py:186-191`,
/// byte-untouched).
pub const CODE_REFUSAL: i64 = 1;
/// Empty table — Python raises the exact `rate_for_date` ValueError
/// (`brutex/costs/constants.py:361-363` bytes).
pub const CODE_EMPTY_TABLE: i64 = 2;
/// `on_date` precedes the first recorded row — Python raises the exact
/// pre-history ValueError of the calling surface
/// (`constants.py:364-369` / `lot_size_history.py:114-118` /
/// `expiry_calendar.py:186` bytes).
pub const CODE_PRE_HISTORY: i64 = 3;
/// Weekly expiry not available on this date (the `NO_WEEKLY = -1` regime
/// row, `expiry_calendar.py:85`) — Python returns `None`.
pub const CODE_NO_WEEKLY: i64 = 4;
/// No strike-grid step derivable (fewer than two distinct positive strikes,
/// `moneyness.py:137-138`) — Python returns `None`.
pub const CODE_NO_STEP: i64 = 5;

/// The `NO_WEEKLY` sentinel as stored IN the weekly dow tables
/// (`expiry_calendar.py:85` — already an int sentinel Python-side, so it
/// crosses the FFI verbatim as a table VALUE, distinct from the regime
/// tables where `-1` encodes a refusal row).
pub const NO_WEEKLY: i64 = -1;

/// `date.min.toordinal()` — Python's proleptic-Gregorian day 1 (0001-01-01).
pub const MIN_ORDINAL: i64 = 1;
/// `date.max.toordinal()` — 9999-12-31. Derived literal: Python
/// `date(9999, 12, 31).toordinal() == 3652059` (derive_oracle.py 2026-07-18).
pub const MAX_ORDINAL: i64 = 3_652_059;

/// Python `date.weekday()` from a `date.toordinal()` ordinal: 0=Mon..6=Sun.
///
/// Python ordinal 1 == 0001-01-01 == Monday (`date(1,1,1).weekday() == 0`,
/// pinned by the derive_oracle.py transcript), so
/// `weekday == (ordinal - 1) mod 7` under FLOOR modulo (`rem_euclid` — Rust
/// `%` is truncating remainder and would differ for the negative ordinals a
/// hostile caller could pass; every valid ordinal is >= 1 but the helper is
/// total anyway).
#[inline]
pub(crate) fn weekday_of_ordinal(ordinal: i64) -> i64 {
    (ordinal - 1).rem_euclid(7)
}

/// `date(year, month, day).toordinal()` — the proleptic-Gregorian civil →
/// ordinal conversion, EXACTLY Python's (pinned against Python-derived
/// literals + a day-walk differential in the tests below).
///
/// Algorithm: Howard Hinnant's `days_from_civil` (public-domain integer
/// algorithm, era = 400-year Gregorian cycles) shifted by the Python-epoch
/// offset `719163 == date(1970, 1, 1).toordinal()` (derived literal,
/// derive_oracle.py 2026-07-18). Pure integer arithmetic — no floats, no
/// rounding.
///
/// Returns `Err` for a (year, month, day) outside Python's `date` domain
/// (year 1..=9999, month 1..=12, day 1..=days-in-month) — the caller
/// degrades to the Python oracle, which raises its own ValueError.
pub(crate) fn civil_to_ordinal(year: i64, month: i64, day: i64) -> Result<i64, String> {
    if !(1..=9999).contains(&year) {
        return Err(format!("civil_to_ordinal: year {year} outside 1..=9999"));
    }
    if !(1..=12).contains(&month) {
        return Err(format!("civil_to_ordinal: month {month} outside 1..=12"));
    }
    if !(1..=31).contains(&day) || day > days_in_month(year, month) {
        return Err(format!(
            "civil_to_ordinal: day {day} invalid for year {year} month {month}"
        ));
    }
    // Hinnant days_from_civil (epoch 1970-03-01-based era arithmetic).
    let y = if month <= 2 { year - 1 } else { year };
    let era = y.div_euclid(400);
    let yoe = y - era * 400; // [0, 399]
    let mp = (month + 9).rem_euclid(12); // Mar=0..Feb=11
    let doy = (153 * mp + 2) / 5 + day - 1; // [0, 365]
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy; // [0, 146096]
    let days_from_1970 = era * 146_097 + doe - 719_468;
    Ok(days_from_1970 + 719_163)
}

/// Days in a proleptic-Gregorian month (helper for the domain check above).
#[inline]
pub(crate) fn days_in_month(year: i64, month: i64) -> i64 {
    match month {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 => {
            if is_leap(year) {
                29
            } else {
                28
            }
        }
        _ => 0,
    }
}

/// Proleptic-Gregorian leap-year predicate (Python `calendar.isleap`).
#[inline]
pub(crate) fn is_leap(year: i64) -> bool {
    (year % 4 == 0 && year % 100 != 0) || year % 400 == 0
}

/// The shared strictly-ascending-starts + parallel-length validation every
/// table-taking kernel runs (mirrors the guarantee the Python tables carry
/// from `validate_regime_table` / their `Final` literal shape — re-checked
/// here because a kernel must never silently mis-bisect a poisoned input;
/// O(n) with n <= 6 rows, negligible).
pub(crate) fn validate_table(starts: &[i64], values: &[i64], label: &str) -> Result<(), String> {
    if starts.len() != values.len() {
        return Err(format!(
            "{label}: starts/values length mismatch ({} / {})",
            starts.len(),
            values.len()
        ));
    }
    for pair in starts.windows(2) {
        if pair[1] <= pair[0] {
            return Err(format!(
                "{label}: starts must be strictly ascending — got {} then {}",
                pair[0], pair[1]
            ));
        }
    }
    Ok(())
}

/// `bisect_right(starts, on_ordinal) - 1` — the index of the LATEST row with
/// `start <= on_ordinal`, the exact `bisect.bisect_right` law of
/// `constants.py:370` / `lot_size_history.py:122` /
/// `expiry_calendar.py:188`. Precondition (checked by the callers): `starts`
/// non-empty, strictly ascending, `on_ordinal >= starts[0]`.
#[inline]
pub(crate) fn bisect_row(starts: &[i64], on_ordinal: i64) -> usize {
    // partition_point == the count of elements <= on_ordinal for a sorted
    // slice — identical to Python's bisect_right on a strictly-ascending key.
    starts.partition_point(|&s| s <= on_ordinal) - 1
}

#[cfg(test)]
mod tests {
    use super::*;

    // ── civil_to_ordinal: Python-derived literal pins ─────────────────
    // Every literal derived by RUNNING the repo Python oracle
    // (`date(y, m, d).toordinal()`, python3.12, derive_oracle.py transcript
    // 2026-07-18).
    #[test]
    fn civil_to_ordinal_python_literal_pins() {
        let pins: &[(i64, i64, i64, i64)] = &[
            (1, 1, 1, 1),
            (1970, 1, 1, 719_163),
            (2000, 2, 29, 730_179),
            (2020, 1, 1, 737_425),
            (2024, 2, 29, 738_945),
            (2024, 12, 31, 739_251),
            (2025, 1, 15, 739_266),
            (2026, 7, 18, 739_815),
            (9999, 12, 31, 3_652_059),
            (2100, 2, 28, 766_703),
            (2400, 2, 29, 876_276),
        ];
        for &(y, m, d, want) in pins {
            let got = civil_to_ordinal(y, m, d)
                .unwrap_or_else(|e| panic!("civil({y},{m},{d}) unexpected Err: {e}"));
            assert_eq!(got, want, "civil({y},{m},{d})");
        }
        assert_eq!(
            civil_to_ordinal(9999, 12, 31).ok(),
            Some(MAX_ORDINAL),
            "MAX_ORDINAL const must equal civil(9999,12,31)"
        );
    }

    #[test]
    fn civil_to_ordinal_rejects_out_of_domain() {
        assert!(civil_to_ordinal(0, 1, 1).is_err());
        assert!(civil_to_ordinal(10_000, 1, 1).is_err());
        assert!(civil_to_ordinal(2024, 0, 1).is_err());
        assert!(civil_to_ordinal(2024, 13, 1).is_err());
        assert!(civil_to_ordinal(2024, 2, 30).is_err()); // leap Feb has 29
        assert!(civil_to_ordinal(2023, 2, 29).is_err()); // non-leap Feb has 28
        assert!(civil_to_ordinal(2024, 4, 31).is_err());
        assert!(civil_to_ordinal(2024, 1, 0).is_err());
    }

    /// Day-walk differential: iterate every day 1970-01-01 → 2200-12-31 with
    /// an INDEPENDENT naive tomorrow() successor and check civil_to_ordinal
    /// is exactly the running counter — an off-by-one anywhere in the era
    /// arithmetic (leap rules, month lengths, epoch shift) diverges.
    #[test]
    fn civil_to_ordinal_day_walk_differential() {
        let (mut y, mut m, mut d) = (1970i64, 1i64, 1i64);
        let mut ordinal = 719_163i64; // Python date(1970,1,1).toordinal()
        while y <= 2200 {
            assert_eq!(
                civil_to_ordinal(y, m, d).ok(),
                Some(ordinal),
                "civil({y},{m},{d})"
            );
            // Independent naive successor.
            d += 1;
            if d > days_in_month(y, m) {
                d = 1;
                m += 1;
                if m > 12 {
                    m = 1;
                    y += 1;
                }
            }
            ordinal += 1;
        }
    }

    #[test]
    fn weekday_of_ordinal_matches_python_convention() {
        // date(1,1,1).weekday() == 0 (Monday) — derive_oracle.py pin.
        assert_eq!(weekday_of_ordinal(1), 0);
        // date(2026,7,18) is a Saturday (weekday 5); ordinal 739815.
        assert_eq!(weekday_of_ordinal(739_815), 5);
        // date(2024,10,1) is a Tuesday (weekday 1); ordinal 739160.
        assert_eq!(weekday_of_ordinal(739_160), 1);
        // Total for non-positive ordinals (floor modulo, never negative).
        assert_eq!(weekday_of_ordinal(0), 6);
        assert_eq!(weekday_of_ordinal(-1), 5);
    }

    #[test]
    fn bisect_row_matches_bisect_right_law() {
        let starts = [1i64, 100, 200];
        assert_eq!(bisect_row(&starts, 1), 0);
        assert_eq!(bisect_row(&starts, 99), 0);
        assert_eq!(bisect_row(&starts, 100), 1); // boundary lands ON the row
        assert_eq!(bisect_row(&starts, 101), 1);
        assert_eq!(bisect_row(&starts, 200), 2);
        assert_eq!(bisect_row(&starts, i64::MAX), 2);
    }

    #[test]
    fn validate_table_rejects_poison() {
        assert!(validate_table(&[1, 2], &[10], "t").is_err()); // length mismatch
        assert!(validate_table(&[2, 1], &[10, 20], "t").is_err()); // descending
        assert!(validate_table(&[1, 1], &[10, 20], "t").is_err()); // duplicate
        assert!(validate_table(&[1, 2], &[10, 20], "t").is_ok());
        assert!(validate_table(&[], &[], "t").is_ok()); // emptiness judged by callers
    }
}
