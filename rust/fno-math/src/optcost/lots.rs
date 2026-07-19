//! Dated history lookup — lot sizes + expiry-dow tables (DEC-OPTCOST-TAILS-RUST-001).
//!
//! The ONE `(effective_ordinal, value)` bisect kernel behind
//! `brutex/options/lot_size_history.py::get_lot_size` (lines 96-123) AND
//! `brutex/options/expiry_calendar.py::_lookup_in_history` (lines 180-189)
//! — both are the identical "latest row with `start <= on_date`" law with a
//! pre-history refusal.
//!
//! Unlike [`super::regime`], a NEGATIVE table value here is LEGAL data (the
//! weekly-expiry tables store `NO_WEEKLY = -1` as a real regime value,
//! `expiry_calendar.py:85`), so this kernel passes values through verbatim
//! and encodes ONLY the pre-history refusal as a code:
//!
//! * `(CODE_OK, value)` — the selected row's value (lot size, or dow /
//!   `NO_WEEKLY`).
//! * `(CODE_PRE_HISTORY, 0)` — `on_date` precedes the first row; Python
//!   raises the exact caller-side ValueError
//!   (`lot_size_history.py:114-118` bytes incl. `instrument.value` /
//!   `expiry_calendar.py:186` bytes).

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

use crate::optcost::{CODE_OK, CODE_PRE_HISTORY, bisect_row, validate_table};

// ── Cargo-test fixture mirrors of the Python lot-size tables ─────────────
//
// AUDIT: each `(effective_ordinal, lot)` row derived by RUNNING the repo
// Python oracle over `brutex/options/lot_size_history.py`'s
// `_LOT_HISTORY_*` tables (python3.12, derive_oracle.py transcript
// 2026-07-18). Test fixtures only — the Python `Final` tables stay the ONE
// runtime source of truth and cross the FFI per call.

/// `_LOT_HISTORY_NIFTY` — 2021-01-01→75 · 2021-07-29→50 · 2024-04-26→25 ·
/// 2024-11-20→75 · 2026-01-01→65 (`lot_size_history.py:58-64`).
pub const LOT_HISTORY_NIFTY: &[(i64, i64)] = &[
    (737_791, 75),
    (738_000, 50),
    (739_002, 25),
    (739_210, 75),
    (739_617, 65),
];

/// `_LOT_HISTORY_BANKNIFTY` — 2021-01-01→25 · 2023-06-30→15 ·
/// 2024-11-20→30 · 2025-07-01→35 · 2025-12-30→30
/// (`lot_size_history.py:66-72`).
pub const LOT_HISTORY_BANKNIFTY: &[(i64, i64)] = &[
    (737_791, 25),
    (738_701, 15),
    (739_210, 30),
    (739_433, 35),
    (739_615, 30),
];

/// `_LOT_HISTORY_SENSEX` — 2021-01-01→10 · 2024-11-20→20
/// (`lot_size_history.py:74-77`).
pub const LOT_HISTORY_SENSEX: &[(i64, i64)] = &[(737_791, 10), (739_210, 20)];

/// Pure core of [`optcost_history_lookup`] — `Result<(code, value), String>`
/// so the no-libpython `cargo test` link exercises every branch.
pub fn history_lookup_checked(
    starts: &[i64],
    values: &[i64],
    on_ordinal: i64,
) -> Result<(i64, i64), String> {
    validate_table(starts, values, "optcost_history_lookup")?;
    if starts.is_empty() {
        // The shipped tables are never empty; an empty table here is a
        // poisoned caller (Python would IndexError on history[0][0] — not a
        // contract to replicate; the seam degrades to the oracle).
        return Err("optcost_history_lookup: empty history table".to_string());
    }
    // lot_size_history.py:114 / expiry_calendar.py:185 — pre-history check.
    if on_ordinal < starts[0] {
        return Ok((CODE_PRE_HISTORY, 0));
    }
    // lot_size_history.py:119-123 / expiry_calendar.py:187-189 —
    // bisect_right(dates, on_date) - 1 row selection, value verbatim.
    let idx = bisect_row(starts, on_ordinal);
    Ok((CODE_OK, values[idx]))
}

/// Dated history lookup over a `(effective_ordinal, value)` table — the
/// shared `get_lot_size` / `_lookup_in_history` bisect law (see the module
/// docs for the `(code, value)` contract; the pre-history ValueError is
/// materialized Python-side with today's exact bytes).
///
/// `starts` are `date.toordinal()` values, strictly ascending; `values`
/// pass through verbatim (a `-1` is legal data — the weekly `NO_WEEKLY`
/// sentinel). Raises `ValueError` on structurally invalid input (the seam
/// degrades to the Python oracle).
///
/// FFI cost (disclosed): two `O(n_rows)` list→Vec copies per call —
/// n_rows <= 5 on every shipped table.
#[pyfunction]
pub fn optcost_history_lookup(
    starts: Vec<i64>,
    values: Vec<i64>,
    on_ordinal: i64,
) -> PyResult<(i64, i64)> {
    history_lookup_checked(&starts, &values, on_ordinal).map_err(PyValueError::new_err)
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;

    fn table(rows: &[(i64, i64)]) -> (Vec<i64>, Vec<i64>) {
        (
            rows.iter().map(|r| r.0).collect(),
            rows.iter().map(|r| r.1).collect(),
        )
    }

    /// Every row of every mirrored lot table pinned against the
    /// Python-derived literals (derive_oracle.py 2026-07-18).
    #[test]
    fn lot_table_mirrors_pin_python_derived_literals() {
        assert_eq!(
            LOT_HISTORY_NIFTY,
            &[
                (737_791, 75),
                (738_000, 50),
                (739_002, 25),
                (739_210, 75),
                (739_617, 65)
            ]
        );
        assert_eq!(
            LOT_HISTORY_BANKNIFTY,
            &[
                (737_791, 25),
                (738_701, 15),
                (739_210, 30),
                (739_433, 35),
                (739_615, 30)
            ]
        );
        assert_eq!(LOT_HISTORY_SENSEX, &[(737_791, 10), (739_210, 20)]);
    }

    /// Every lot transition date ±1 day pinned against the Python oracle
    /// (`get_lot_size` boundary pins, derive_oracle.py 2026-07-18).
    #[test]
    fn lot_transition_boundaries_match_python_oracle() {
        // (table, transition_ordinal, lot_on_date, lot_prev_day_or_none)
        let pins: &[(&[(i64, i64)], i64, i64, Option<i64>)] = &[
            (LOT_HISTORY_NIFTY, 737_791, 75, None), // 2021-01-01 (history start)
            (LOT_HISTORY_NIFTY, 738_000, 50, Some(75)), // 2021-07-29
            (LOT_HISTORY_NIFTY, 739_002, 25, Some(50)), // 2024-04-26
            (LOT_HISTORY_NIFTY, 739_210, 75, Some(25)), // 2024-11-20
            (LOT_HISTORY_NIFTY, 739_617, 65, Some(75)), // 2026-01-01
            (LOT_HISTORY_BANKNIFTY, 737_791, 25, None), // 2021-01-01
            (LOT_HISTORY_BANKNIFTY, 738_701, 15, Some(25)), // 2023-06-30
            (LOT_HISTORY_BANKNIFTY, 739_210, 30, Some(15)), // 2024-11-20
            (LOT_HISTORY_BANKNIFTY, 739_433, 35, Some(30)), // 2025-07-01
            (LOT_HISTORY_BANKNIFTY, 739_615, 30, Some(35)), // 2025-12-30
            (LOT_HISTORY_SENSEX, 737_791, 10, None), // 2021-01-01
            (LOT_HISTORY_SENSEX, 739_210, 20, Some(10)), // 2024-11-20
        ];
        for &(rows, ord, want_on, want_prev) in pins {
            let (s, v) = table(rows);
            assert_eq!(
                history_lookup_checked(&s, &v, ord),
                Ok((CODE_OK, want_on)),
                "on ordinal {ord}"
            );
            match want_prev {
                Some(prev) => assert_eq!(
                    history_lookup_checked(&s, &v, ord - 1),
                    Ok((CODE_OK, prev)),
                    "prev-day of ordinal {ord}"
                ),
                None => assert_eq!(
                    history_lookup_checked(&s, &v, ord - 1),
                    Ok((CODE_PRE_HISTORY, 0)),
                    "pre-history at ordinal {}",
                    ord - 1
                ),
            }
        }
    }

    #[test]
    fn negative_values_pass_through_verbatim() {
        // The weekly NO_WEEKLY sentinel (-1) is DATA here, never a refusal.
        let (s, v) = (vec![100i64, 200], vec![3i64, -1]);
        assert_eq!(history_lookup_checked(&s, &v, 150), Ok((CODE_OK, 3)));
        assert_eq!(history_lookup_checked(&s, &v, 200), Ok((CODE_OK, -1)));
        assert_eq!(
            history_lookup_checked(&s, &v, 99),
            Ok((CODE_PRE_HISTORY, 0))
        );
    }

    #[test]
    fn err_on_poisoned_input() {
        assert!(history_lookup_checked(&[], &[], 5).is_err());
        assert!(history_lookup_checked(&[1, 2], &[5], 3).is_err());
        assert!(history_lookup_checked(&[2, 1], &[5, 6], 3).is_err());
        assert!(history_lookup_checked(&[1, 1], &[5, 6], 3).is_err());
    }

    proptest! {
        /// Differential vs a naive linear scan over arbitrary ascending
        /// tables (values unrestricted — negatives legal), plus determinism.
        #[test]
        fn matches_naive_linear_scan(
            rows in proptest::collection::btree_map(
                1i64..4_000_000, -10i64..=10_000, 1..7,
            ),
            on in 0i64..4_000_001,
        ) {
            let starts: Vec<i64> = rows.keys().copied().collect();
            let values: Vec<i64> = rows.values().copied().collect();
            let got = history_lookup_checked(&starts, &values, on)
                .unwrap_or_else(|e| panic!("unexpected Err: {e}"));
            let want = if on < starts[0] {
                (CODE_PRE_HISTORY, 0)
            } else {
                let mut sel = 0usize;
                for (i, &s) in starts.iter().enumerate() {
                    if s <= on {
                        sel = i;
                    }
                }
                (CODE_OK, values[sel])
            };
            prop_assert_eq!(got, want);
        }
    }
}
