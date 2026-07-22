//! Expiry-date kernels — next weekly / next monthly (DEC-OPTCOST-TAILS-RUST-001).
//!
//! Rust twins of the pure date arithmetic in
//! `brutex/options/expiry_calendar.py`: [`optcost_next_weekly_expiry`]
//! (`next_weekly_expiry`, lines 222-236) and
//! [`optcost_next_monthly_expiry`] (`next_monthly_expiry` +
//! `_last_dow_of_month`, lines 239-281). The dow-regime lookup itself
//! (`get_weekly_dow` / `get_monthly_dow` / `has_weekly`, lines 192-219) is
//! served by [`super::lots::optcost_history_lookup`] — the identical bisect
//! law; Python materializes `None` from the `NO_WEEKLY` value.
//!
//! The six per-underlying dow tables stay Python `Final` data
//! (`expiry_calendar.py:95-174`) and cross the FFI as
//! `(start_ordinal, dow | NO_WEEKLY)` vectors; the `pub const` mirrors
//! below are cargo-test fixtures pinned against Python-derived literals.
//!
//! ## Date arithmetic laws (all pure integers)
//!
//! * `weekday(ordinal) == (ordinal - 1) mod 7` (Python ordinal 1 =
//!   0001-01-01 = Monday; pinned in the `optcost` mod-level tests).
//! * `days_until = (target_dow - weekday) mod 7` under FLOOR modulo —
//!   Python's `%` (`expiry_calendar.py:235`); Rust `rem_euclid`.
//! * "last `<dow>` of month" walks back from the month's last day by
//!   `(weekday(last) - dow) mod 7` (`expiry_calendar.py:274-281`).
//! * civil→ordinal via [`super::civil_to_ordinal`] (Hinnant days_from_civil
//!   shifted to the Python epoch, day-walk-differential-pinned).
//!
//! A result outside Python's `date` domain (e.g. the December-9999
//! `_last_dow_of_month` rollover that makes Python's `date(10000, 1, 1)`
//! constructor raise) returns `Err` → the seam degrades to the Python
//! oracle, which raises exactly what it raises today.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

use crate::optcost::lots::history_lookup_checked;
use crate::optcost::{
    CODE_NO_WEEKLY, CODE_OK, CODE_PRE_HISTORY, MAX_ORDINAL, NO_WEEKLY, civil_to_ordinal,
    weekday_of_ordinal,
};

// ── Cargo-test fixture mirrors of the six Python dow tables ──────────────
//
// AUDIT: each `(effective_ordinal, dow | -1)` row derived by RUNNING the
// repo Python oracle over `brutex/options/expiry_calendar.py`'s
// `_WEEKLY_*` / `_MONTHLY_*` tables (python3.12, derive_oracle.py
// transcript 2026-07-18). Test fixtures only — Python data stays the ONE
// runtime source of truth.

/// `_WEEKLY_NIFTY` — 2020-01-01→Thu(3) · 2025-09-02→Tue(1)
/// (`expiry_calendar.py:95-104`).
pub const WEEKLY_NIFTY: &[(i64, i64)] = &[(737_425, 3), (739_496, 1)];

/// `_WEEKLY_BANKNIFTY` — 2020-01-01→Thu(3) · 2023-09-04→Wed(2) ·
/// 2024-11-14→NO_WEEKLY (`expiry_calendar.py:106-114`).
pub const WEEKLY_BANKNIFTY: &[(i64, i64)] = &[(737_425, 3), (738_767, 2), (739_204, NO_WEEKLY)];

/// `_WEEKLY_SENSEX` — 2020-01-01→Thu(3) · 2023-05-15→Fri(4) ·
/// 2025-01-01→Tue(1) · 2025-09-04→Thu(3) (`expiry_calendar.py:116-128`).
pub const WEEKLY_SENSEX: &[(i64, i64)] = &[(737_425, 3), (738_655, 4), (739_252, 1), (739_498, 3)];

/// `_MONTHLY_NIFTY` — 2020-01-01→Thu(3) · 2025-09-02→Tue(1)
/// (`expiry_calendar.py:134-141`).
pub const MONTHLY_NIFTY: &[(i64, i64)] = &[(737_425, 3), (739_496, 1)];

/// `_MONTHLY_BANKNIFTY` — 2020-01-01→Thu(3) · 2024-03-01→Wed(2) ·
/// 2025-01-01→Thu(3) · 2025-09-02→Tue(1) (`expiry_calendar.py:143-151`).
pub const MONTHLY_BANKNIFTY: &[(i64, i64)] =
    &[(737_425, 3), (738_946, 2), (739_252, 3), (739_496, 1)];

/// `_MONTHLY_SENSEX` — 2020-01-01→Thu(3) · 2023-05-15→Fri(4) ·
/// 2025-01-01→Tue(1) · 2025-09-04→Thu(3) (`expiry_calendar.py:153-161`).
pub const MONTHLY_SENSEX: &[(i64, i64)] = &[(737_425, 3), (738_655, 4), (739_252, 1), (739_498, 3)];

/// `_last_dow_of_month(year, month, target_dow)` — the exact
/// `expiry_calendar.py:274-281` law over ordinals: the last day of the
/// month, walked back `(weekday - target_dow) mod 7` days.
fn last_dow_of_month(year: i64, month: i64, target_dow: i64) -> Result<i64, String> {
    // expiry_calendar.py:277 — first day of the NEXT month (December rolls
    // the year; year 9999 December → date(10000, 1, 1) raises Python-side,
    // mirrored here by civil_to_ordinal's domain check).
    let next_month_first = if month == 12 {
        civil_to_ordinal(year + 1, 1, 1)?
    } else {
        civil_to_ordinal(year, month + 1, 1)?
    };
    // expiry_calendar.py:278 — last day of (year, month).
    let last_day = next_month_first - 1;
    // expiry_calendar.py:280 — floor-mod walk-back.
    let days_back = (weekday_of_ordinal(last_day) - target_dow).rem_euclid(7);
    Ok(last_day - days_back)
}

/// Pure core of [`optcost_next_weekly_expiry`].
pub fn next_weekly_expiry_checked(
    starts: &[i64],
    dows: &[i64],
    on_ordinal: i64,
) -> Result<(i64, i64), String> {
    // expiry_calendar.py:232 — get_weekly_dow (the shared bisect law).
    let (code, dow) = history_lookup_checked(starts, dows, on_ordinal)?;
    if code == CODE_PRE_HISTORY {
        return Ok((CODE_PRE_HISTORY, 0));
    }
    // expiry_calendar.py:233-234 — NO_WEEKLY → None Python-side.
    if dow == NO_WEEKLY {
        return Ok((CODE_NO_WEEKLY, 0));
    }
    if !(0..=6).contains(&dow) {
        return Err(format!(
            "optcost_next_weekly_expiry: dow {dow} outside 0..=6 (and not the \
             NO_WEEKLY sentinel) — poisoned table"
        ));
    }
    // expiry_calendar.py:235-236 — (dow - weekday) floor-mod-7 walk forward.
    let days_until = (dow - weekday_of_ordinal(on_ordinal)).rem_euclid(7);
    let result = on_ordinal + days_until;
    if result > MAX_ORDINAL {
        // Python's `on_or_after + timedelta(days_until)` would overflow past
        // date.max and raise — the seam degrades to the oracle.
        return Err(format!(
            "optcost_next_weekly_expiry: result ordinal {result} past date.max"
        ));
    }
    Ok((CODE_OK, result))
}

/// Next weekly expiry AT OR AFTER `on_ordinal` — the
/// `brutex/options/expiry_calendar.py::next_weekly_expiry` law over a
/// `(start_ordinal, dow | NO_WEEKLY)` regime table.
///
/// Returns `(CODE_OK, expiry_ordinal)`, `(CODE_NO_WEEKLY, 0)` (Python
/// materializes `None`) or `(CODE_PRE_HISTORY, 0)` (Python raises the exact
/// `expiry_calendar.py:186` ValueError). Raises `ValueError` on poisoned
/// input — the seam degrades to the Python oracle.
#[pyfunction]
pub fn optcost_next_weekly_expiry(
    starts: Vec<i64>,
    dows: Vec<i64>,
    on_ordinal: i64,
) -> PyResult<(i64, i64)> {
    next_weekly_expiry_checked(&starts, &dows, on_ordinal).map_err(PyValueError::new_err)
}

/// Pure core of [`optcost_next_monthly_expiry`].
///
/// `year`/`month` are `on_or_after.year` / `on_or_after.month` — passed
/// alongside the ordinal so the kernel never re-derives ordinal→civil; a
/// consistency check refuses a mismatched triple (refuse-poison).
pub fn next_monthly_expiry_checked(
    starts: &[i64],
    dows: &[i64],
    year: i64,
    month: i64,
    on_ordinal: i64,
) -> Result<(i64, i64), String> {
    // Refuse-poison: (year, month) must be on_ordinal's own month.
    let month_first = civil_to_ordinal(year, month, 1)?;
    let month_last = if month == 12 {
        civil_to_ordinal(year, 12, 31)?
    } else {
        civil_to_ordinal(year, month + 1, 1)? - 1
    };
    if !(month_first..=month_last).contains(&on_ordinal) {
        return Err(format!(
            "optcost_next_monthly_expiry: on_ordinal {on_ordinal} is not inside \
             (year {year}, month {month})"
        ));
    }
    let dow_of = |ordinal: i64| -> Result<(i64, i64), String> {
        let (code, dow) = history_lookup_checked(starts, dows, ordinal)?;
        if code == CODE_OK && !(0..=6).contains(&dow) {
            return Err(format!(
                "optcost_next_monthly_expiry: dow {dow} outside 0..=6 — \
                 poisoned table (monthly tables carry no NO_WEEKLY sentinel)"
            ));
        }
        Ok((code, dow))
    };
    // expiry_calendar.py:257-261 — this month's regime first.
    let (code, this_month_dow) = dow_of(on_ordinal)?;
    if code == CODE_PRE_HISTORY {
        return Ok((CODE_PRE_HISTORY, 0));
    }
    let candidate = last_dow_of_month(year, month, this_month_dow)?;
    if candidate >= on_ordinal {
        return Ok((CODE_OK, candidate));
    }
    // expiry_calendar.py:262-266 — roll to the next month.
    let (next_year, next_month) = if month == 12 {
        (year + 1, 1)
    } else {
        (year, month + 1)
    };
    // expiry_calendar.py:267-270 — mid-month (day 15) dow-lookup reference,
    // RE-EVALUATING the regime for the rolled month (the Dec-2024 BANKNIFTY
    // last-Wed → Jan-2025 last-Thu rollover).
    let next_month_ref = civil_to_ordinal(next_year, next_month, 15)?;
    let (code, next_month_dow) = dow_of(next_month_ref)?;
    if code == CODE_PRE_HISTORY {
        // Unreachable when the first lookup passed (ref > on_ordinal), kept
        // for totality.
        return Ok((CODE_PRE_HISTORY, 0));
    }
    // expiry_calendar.py:271.
    Ok((
        CODE_OK,
        last_dow_of_month(next_year, next_month, next_month_dow)?,
    ))
}

/// Next monthly expiry AT OR AFTER `on_ordinal` — the
/// `brutex/options/expiry_calendar.py::next_monthly_expiry`
/// "last `<dow>` of the month" law with the cross-month regime
/// re-evaluation.
///
/// `year`/`month` are the ordinal's own civil year/month (consistency-
/// checked). Returns `(CODE_OK, expiry_ordinal)` or `(CODE_PRE_HISTORY, 0)`
/// (Python raises the exact `expiry_calendar.py:186` ValueError). Raises
/// `ValueError` on poisoned/out-of-domain input (incl. the December-9999
/// rollover Python's own `date` constructor refuses) — the seam degrades to
/// the Python oracle.
#[pyfunction]
pub fn optcost_next_monthly_expiry(
    starts: Vec<i64>,
    dows: Vec<i64>,
    year: i64,
    month: i64,
    on_ordinal: i64,
) -> PyResult<(i64, i64)> {
    next_monthly_expiry_checked(&starts, &dows, year, month, on_ordinal)
        .map_err(PyValueError::new_err)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn table(rows: &[(i64, i64)]) -> (Vec<i64>, Vec<i64>) {
        (
            rows.iter().map(|r| r.0).collect(),
            rows.iter().map(|r| r.1).collect(),
        )
    }

    /// Every row of every mirrored dow table pinned against the
    /// Python-derived literals (derive_oracle.py 2026-07-18).
    #[test]
    fn dow_table_mirrors_pin_python_derived_literals() {
        assert_eq!(WEEKLY_NIFTY, &[(737_425, 3), (739_496, 1)]);
        assert_eq!(
            WEEKLY_BANKNIFTY,
            &[(737_425, 3), (738_767, 2), (739_204, -1)]
        );
        assert_eq!(
            WEEKLY_SENSEX,
            &[(737_425, 3), (738_655, 4), (739_252, 1), (739_498, 3)]
        );
        assert_eq!(MONTHLY_NIFTY, &[(737_425, 3), (739_496, 1)]);
        assert_eq!(
            MONTHLY_BANKNIFTY,
            &[(737_425, 3), (738_946, 2), (739_252, 3), (739_496, 1)]
        );
        assert_eq!(
            MONTHLY_SENSEX,
            &[(737_425, 3), (738_655, 4), (739_252, 1), (739_498, 3)]
        );
    }

    /// next_weekly_expiry regime-boundary pins ±1 day, every value from the
    /// Python oracle (derive_oracle.py 2026-07-18).
    #[test]
    fn next_weekly_pins_match_python_oracle() {
        // (table, on_ordinal, expected)
        let pins: &[(&[(i64, i64)], i64, (i64, i64))] = &[
            // NIFTY 2025-09-01 → 2025-09-04 (last Thu week before the Tue flip)
            (WEEKLY_NIFTY, 739_495, (CODE_OK, 739_498)),
            // NIFTY 2025-09-02 → 2025-09-02 (first Tue-regime day IS a Tue)
            (WEEKLY_NIFTY, 739_496, (CODE_OK, 739_496)),
            // BANKNIFTY 2024-11-13 → 2024-11-13 (last weekly day)
            (WEEKLY_BANKNIFTY, 739_203, (CODE_OK, 739_203)),
            // BANKNIFTY 2024-11-14 → None (SEBI killed BN weekly)
            (WEEKLY_BANKNIFTY, 739_204, (CODE_NO_WEEKLY, 0)),
            // BANKNIFTY 2023-09-03 → 2023-09-07 (Thu regime's last week)
            (WEEKLY_BANKNIFTY, 738_766, (CODE_OK, 738_770)),
            // BANKNIFTY 2023-09-04 → 2023-09-06 (first Wed-regime week)
            (WEEKLY_BANKNIFTY, 738_767, (CODE_OK, 738_769)),
            // SENSEX 2023-05-14 → 2023-05-18 · 2023-05-15 → 2023-05-19
            (WEEKLY_SENSEX, 738_654, (CODE_OK, 738_658)),
            (WEEKLY_SENSEX, 738_655, (CODE_OK, 738_659)),
            // SENSEX 2024-12-31 → 2025-01-03 · 2025-01-01 → 2025-01-07
            (WEEKLY_SENSEX, 739_251, (CODE_OK, 739_254)),
            (WEEKLY_SENSEX, 739_252, (CODE_OK, 739_258)),
            // SENSEX 2025-09-03 → 2025-09-09 · 2025-09-04 → 2025-09-04
            (WEEKLY_SENSEX, 739_497, (CODE_OK, 739_503)),
            (WEEKLY_SENSEX, 739_498, (CODE_OK, 739_498)),
        ];
        for &(rows, on, want) in pins {
            let (s, d) = table(rows);
            assert_eq!(
                next_weekly_expiry_checked(&s, &d, on),
                Ok(want),
                "next_weekly on ordinal {on}"
            );
        }
        // Pre-history (2019-12-31 = ordinal 737424).
        let (s, d) = table(WEEKLY_NIFTY);
        assert_eq!(
            next_weekly_expiry_checked(&s, &d, 737_424),
            Ok((CODE_PRE_HISTORY, 0))
        );
    }

    /// next_monthly_expiry pins incl. the Dec→Jan regime rollover
    /// (BANKNIFTY Dec-2024 last-Wed / Jan-2025 last-Thu), every value from
    /// the Python oracle (derive_oracle.py 2026-07-18).
    #[test]
    fn next_monthly_pins_match_python_oracle() {
        // (table, year, month, on_ordinal, expected_ordinal)
        let pins: &[(&[(i64, i64)], i64, i64, i64, i64)] = &[
            // NIFTY 2025-08-29 → 2025-09-30 (Aug last-Thu 28th already past
            // → rolls into September's TUESDAY regime)
            (MONTHLY_NIFTY, 2025, 8, 739_492, 739_524),
            // NIFTY 2025-09-02 → 2025-09-30
            (MONTHLY_NIFTY, 2025, 9, 739_496, 739_524),
            // BANKNIFTY 2024-12-25 → 2024-12-25 (last Wed of Dec, ON the day)
            (MONTHLY_BANKNIFTY, 2024, 12, 739_245, 739_245),
            // BANKNIFTY 2024-12-26 → 2025-01-30 (Dec last-Wed past → Jan
            // re-evaluates to last THURSDAY — the year-boundary rollover)
            (MONTHLY_BANKNIFTY, 2024, 12, 739_246, 739_281),
            // BANKNIFTY 2024-12-31 → 2025-01-30
            (MONTHLY_BANKNIFTY, 2024, 12, 739_251, 739_281),
            // BANKNIFTY 2024-02-29 → 2024-02-29 (leap-day last Thu, ON it)
            (MONTHLY_BANKNIFTY, 2024, 2, 738_945, 738_945),
            // BANKNIFTY 2024-03-01 → 2024-03-27 (first Wed-monthly regime day)
            (MONTHLY_BANKNIFTY, 2024, 3, 738_946, 738_972),
            // SENSEX 2024-12-28 → 2025-01-28 (Dec last-Tue past → Jan Tue)
            (MONTHLY_SENSEX, 2024, 12, 739_248, 739_279),
            // SENSEX 2023-05-15 → 2023-05-26 (first Friday-regime day)
            (MONTHLY_SENSEX, 2023, 5, 738_655, 738_666),
            // NIFTY 2020-01-01 → 2020-01-30 (history start)
            (MONTHLY_NIFTY, 2020, 1, 737_425, 737_454),
            // NIFTY 2020-01-31 → 2020-02-27 (Jan last-Thu 30th past → Feb)
            (MONTHLY_NIFTY, 2020, 1, 737_455, 737_482),
        ];
        for &(rows, y, m, on, want) in pins {
            let (s, d) = table(rows);
            assert_eq!(
                next_monthly_expiry_checked(&s, &d, y, m, on),
                Ok((CODE_OK, want)),
                "next_monthly {y}-{m} on ordinal {on}"
            );
        }
        // Pre-history.
        let (s, d) = table(MONTHLY_NIFTY);
        assert_eq!(
            next_monthly_expiry_checked(&s, &d, 2019, 12, 737_424),
            Ok((CODE_PRE_HISTORY, 0))
        );
    }

    #[test]
    fn last_dow_of_month_spot_checks() {
        // Last Thursday of Jan-2020 = 2020-01-30 = ordinal 737454
        // (Python oracle pin via next_monthly_expiry(NIFTY, 2020-01-01)).
        assert_eq!(last_dow_of_month(2020, 1, 3), Ok(737_454));
        // Last Wednesday of Dec-2024 = 2024-12-25 = 739245.
        assert_eq!(last_dow_of_month(2024, 12, 2), Ok(739_245));
        // Last Thursday of Jan-2025 = 2025-01-30 = 739281.
        assert_eq!(last_dow_of_month(2025, 1, 3), Ok(739_281));
        // Leap February: last Thursday of Feb-2024 = 2024-02-29 = 738945.
        assert_eq!(last_dow_of_month(2024, 2, 3), Ok(738_945));
        // December 9999 rolls into date(10000, 1, 1) — Python raises,
        // the kernel Errs (→ oracle).
        assert!(last_dow_of_month(9999, 12, 3).is_err());
    }

    #[test]
    fn december_9999_degrades_to_oracle() {
        let (s, d) = table(MONTHLY_NIFTY);
        // 9999-12-31 = ordinal 3652059: this month's last-Tue could be in
        // range, but if the candidate is past on_ordinal the rollover needs
        // date(10000, 1, 15) → Err either way when it rolls. Use a day past
        // December 9999's last Tuesday to force the rollover arm.
        let e = next_monthly_expiry_checked(&s, &d, 9999, 12, 3_652_059);
        // last Tuesday of Dec-9999 is 9999-12-28 (ordinal 3652056) < on →
        // rollover → Err.
        assert!(e.is_err());
    }

    #[test]
    fn err_on_inconsistent_year_month_ordinal() {
        let (s, d) = table(MONTHLY_NIFTY);
        // ordinal 739245 is 2024-12-25 — claiming 2024-11 is poison.
        assert!(next_monthly_expiry_checked(&s, &d, 2024, 11, 739_245).is_err());
        assert!(next_monthly_expiry_checked(&s, &d, 2025, 12, 739_245).is_err());
        assert!(next_monthly_expiry_checked(&s, &d, 2024, 13, 739_245).is_err());
    }

    #[test]
    fn err_on_poisoned_dow_values() {
        // A weekly dow outside 0..=6 (and not NO_WEEKLY) is poison.
        assert!(next_weekly_expiry_checked(&[1], &[7], 5).is_err());
        assert!(next_weekly_expiry_checked(&[1], &[-2], 5).is_err());
        // Monthly tables carry no NO_WEEKLY sentinel — -1 is poison there.
        assert!(next_monthly_expiry_checked(&[1], &[-1], 2024, 1, 738_886).is_err());
    }

    /// Result-domain law: the weekly result is always the FIRST on-or-after
    /// day with the regime's weekday — checked by brute force against a
    /// day-scan over the whole 2020-2026 NIFTY/SENSEX regime span.
    #[test]
    fn weekly_result_is_first_on_or_after_target_dow_day_scan() {
        for rows in [WEEKLY_NIFTY, WEEKLY_SENSEX] {
            let (s, d) = table(rows);
            for on in 737_425..=739_800 {
                let (code, got) = next_weekly_expiry_checked(&s, &d, on)
                    .unwrap_or_else(|e| panic!("unexpected Err: {e}"));
                assert_eq!(code, CODE_OK);
                // Brute-force: the regime dow AT `on` (the Python law reads
                // the regime once, at on_or_after), first day >= on with it.
                let (c2, dow) = history_lookup_checked(&s, &d, on)
                    .unwrap_or_else(|e| panic!("unexpected Err: {e}"));
                assert_eq!(c2, CODE_OK);
                let mut want = on;
                while weekday_of_ordinal(want) != dow {
                    want += 1;
                }
                assert_eq!(got, want, "on ordinal {on}");
                assert!(got >= on && got - on < 7);
            }
        }
    }
}
