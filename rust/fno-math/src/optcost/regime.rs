//! Date-regime rate lookup — the `rate_for_date` kernel (DEC-OPTCOST-TAILS-RUST-001).
//!
//! The Rust twin of `brutex/costs/constants.py::rate_for_date` (lines
//! 313-381) — the ONE shared regime-lookup mechanism behind the four
//! shipped tables (STT options / STT exercise / NSE ETC / BSE ETC) and
//! their thin delegators `stt_options_rate_bps_x100` /
//! `stt_exercise_rate_bps_x100` / `exchange_charge_bps_x100`.
//!
//! ## Refusal preservation (VERIFIED-RATES-ONLY, DEC-ETC-REGIME-HISTORY-001)
//!
//! This kernel NEVER composes an exception and NEVER fabricates a rate. It
//! returns `(code, value)`:
//!
//! * `(CODE_OK, rate_bps_x100)` — the selected row carries a verified rate.
//! * `(CODE_REFUSAL, row_index)` — the selected row is a refusal row
//!   (`rate_bps_x100 is None` Python-side, `-1` on the wire); Python raises
//!   `UnverifiedCostRegimeError` from its OWN `RegimeRow` objects
//!   (`constants.py:372-380` → `types.py:186-191` `__init__` — the
//!   charge/exchange/source strings, `window_start`, `verified_from`, every
//!   message byte composed by the EXISTING Python lines, untouched).
//! * `(CODE_EMPTY_TABLE, 0)` — Python raises the exact
//!   `constants.py:361-363` ValueError.
//! * `(CODE_PRE_HISTORY, 0)` — Python raises the exact
//!   `constants.py:364-369` ValueError.
//!
//! The `-1` refusal sentinel is collision-safe: `RegimeRow.__post_init__`
//! (`constants.py:250-256`) refuses every negative rate, so no legal rate
//! value can alias it (any OTHER negative rate on the wire is a poisoned
//! caller → `Err` → the seam degrades to the Python oracle).
//!
//! ## Bit-exactness claim: STRUCTURAL
//!
//! Pure i64 `bisect_right` over date ordinals — no floats anywhere. The
//! selection law transcribes `constants.py:361-371` line-for-line (empty
//! check, `on_date < regimes[0].start` pre-history check,
//! `bisect_right(...) - 1`, refusal-row check).

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

use crate::optcost::{
    CODE_EMPTY_TABLE, CODE_OK, CODE_PRE_HISTORY, CODE_REFUSAL, bisect_row, validate_table,
};

/// The wire sentinel for a refusal row's rate (`rate_bps_x100 is None`
/// Python-side). Safe: `RegimeRow.__post_init__` refuses negative rates.
pub const REFUSAL_RATE: i64 = -1;

// ── Cargo-test fixture mirrors of the four shipped Python tables ─────────
//
// AUDIT (instruction: consts mirror the Python tables value-for-value
// INCLUDING every refusal row): each `(start_ordinal, rate_bps_x100 | -1)`
// row below was derived by RUNNING the repo Python oracle over
// `brutex/costs/constants.py`'s `_STT_OPTIONS_REGIMES` /
// `_STT_EXERCISE_REGIMES` / `_NSE_ETC_OPTIONS_REGIMES` /
// `_BSE_ETC_OPTIONS_REGIMES` (python3.12, derive_oracle.py transcript
// 2026-07-18). These consts are TEST FIXTURES + reference data — the Python
// `Final` tables (with their citation source strings) remain the ONE
// runtime source of truth and cross the FFI per call.

/// `_STT_OPTIONS_REGIMES` — 0001-01-01 → 6250 · 2024-10-01 → 10000 ·
/// 2026-04-01 → 15000 (`constants.py:385-408`).
pub const STT_OPTIONS_REGIMES: &[(i64, i64)] = &[(1, 6_250), (739_160, 10_000), (739_707, 15_000)];

/// `_STT_EXERCISE_REGIMES` — 0001-01-01 → 12500 · 2026-04-01 → 15000
/// (`constants.py:411-425`).
pub const STT_EXERCISE_REGIMES: &[(i64, i64)] = &[(1, 12_500), (739_707, 15_000)];

/// `_NSE_ETC_OPTIONS_REGIMES` — 0001-01-01 → REFUSAL · 2024-10-01 → 3503
/// (`constants.py:431-453`; the refusal row IS the
/// DEC-ETC-REGIME-HISTORY-001 pre-2024-10-01 window).
pub const NSE_ETC_OPTIONS_REGIMES: &[(i64, i64)] = &[(1, REFUSAL_RATE), (739_160, 3_503)];

/// `_BSE_ETC_OPTIONS_REGIMES` — 0001-01-01 → REFUSAL · 2024-10-01 → 3250
/// (`constants.py:455-478`).
pub const BSE_ETC_OPTIONS_REGIMES: &[(i64, i64)] = &[(1, REFUSAL_RATE), (739_160, 3_250)];

/// Pure core of [`optcost_regime_rate`] — `Result<(code, value), String>`
/// so the no-libpython `cargo test` link exercises every branch.
pub fn regime_rate_checked(
    starts: &[i64],
    rates: &[i64],
    on_ordinal: i64,
) -> Result<(i64, i64), String> {
    validate_table(starts, rates, "optcost_regime_rate")?;
    if let Some(&bad) = rates.iter().find(|&&r| r < REFUSAL_RATE) {
        // A rate below -1 is unrepresentable Python-side
        // (RegimeRow.__post_init__ refuses negatives) — poisoned caller.
        return Err(format!(
            "optcost_regime_rate: rate {bad} below the -1 refusal sentinel is \
             unrepresentable (RegimeRow refuses negative rates)"
        ));
    }
    // constants.py:361-363 — empty-table check FIRST.
    if starts.is_empty() {
        return Ok((CODE_EMPTY_TABLE, 0));
    }
    // constants.py:364-369 — pre-history check SECOND.
    if on_ordinal < starts[0] {
        return Ok((CODE_PRE_HISTORY, 0));
    }
    // constants.py:370-371 — bisect_right(...) - 1 row selection.
    let idx = bisect_row(starts, on_ordinal);
    // constants.py:372-380 — refusal-row check; Python composes the
    // UnverifiedCostRegimeError from ITS row objects via the returned index.
    if rates[idx] == REFUSAL_RATE {
        return Ok((CODE_REFUSAL, idx as i64));
    }
    Ok((CODE_OK, rates[idx]))
}

/// Date-regime rate lookup over a `(start_ordinal, rate_bps_x100 | -1)`
/// table — the `brutex/costs/constants.py::rate_for_date` decision kernel
/// (see the module docs for the `(code, value)` contract; every refusal is
/// materialized Python-side with today's exact exception bytes).
///
/// `starts` are `date.toordinal()` values, strictly ascending; `rates` are
/// parallel `rate_bps_x100` values with `-1` marking a refusal row. Raises
/// `ValueError` on structurally invalid input (the seam degrades to the
/// Python oracle).
///
/// FFI cost (disclosed): two `O(n_rows)` list→Vec copies per call —
/// n_rows <= 3 on every shipped table.
#[pyfunction]
pub fn optcost_regime_rate(
    starts: Vec<i64>,
    rates: Vec<i64>,
    on_ordinal: i64,
) -> PyResult<(i64, i64)> {
    regime_rate_checked(&starts, &rates, on_ordinal).map_err(PyValueError::new_err)
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

    // ── Python-oracle boundary pins (derive_oracle.py 2026-07-18) ────────
    // Ordinals: 2024-09-30=739159 · 2024-10-01=739160 · 2026-03-31=739706 ·
    // 2026-04-01=739707 · date.min=1.

    #[test]
    fn stt_options_boundary_pins_match_python_oracle() {
        let (s, r) = table(STT_OPTIONS_REGIMES);
        // stt_options_rate_bps_x100(2024-09-30) == 6250
        assert_eq!(regime_rate_checked(&s, &r, 739_159), Ok((CODE_OK, 6_250)));
        // stt_options_rate_bps_x100(2024-10-01) == 10000 (boundary ON)
        assert_eq!(regime_rate_checked(&s, &r, 739_160), Ok((CODE_OK, 10_000)));
        // stt_options_rate_bps_x100(2026-03-31) == 10000
        assert_eq!(regime_rate_checked(&s, &r, 739_706), Ok((CODE_OK, 10_000)));
        // stt_options_rate_bps_x100(2026-04-01) == 15000 (boundary ON)
        assert_eq!(regime_rate_checked(&s, &r, 739_707), Ok((CODE_OK, 15_000)));
        // stt_options_rate_bps_x100(date.min) == 6250 (anchor row)
        assert_eq!(regime_rate_checked(&s, &r, 1), Ok((CODE_OK, 6_250)));
    }

    #[test]
    fn stt_exercise_boundary_pins_match_python_oracle() {
        let (s, r) = table(STT_EXERCISE_REGIMES);
        // stt_exercise_rate_bps_x100(2026-03-31) == 12500
        assert_eq!(regime_rate_checked(&s, &r, 739_706), Ok((CODE_OK, 12_500)));
        // stt_exercise_rate_bps_x100(2026-04-01) == 15000
        assert_eq!(regime_rate_checked(&s, &r, 739_707), Ok((CODE_OK, 15_000)));
        // stt_exercise_rate_bps_x100(date.min) == 12500
        assert_eq!(regime_rate_checked(&s, &r, 1), Ok((CODE_OK, 12_500)));
    }

    #[test]
    fn etc_refusal_windows_match_python_oracle() {
        // exchange_charge_bps_x100(NIFTY/BANKNIFTY, 2024-10-01) == 3503;
        // 2024-09-30 RAISES UnverifiedCostRegimeError (row index 0).
        let (s, r) = table(NSE_ETC_OPTIONS_REGIMES);
        assert_eq!(regime_rate_checked(&s, &r, 739_160), Ok((CODE_OK, 3_503)));
        assert_eq!(regime_rate_checked(&s, &r, 739_159), Ok((CODE_REFUSAL, 0)));
        assert_eq!(regime_rate_checked(&s, &r, 1), Ok((CODE_REFUSAL, 0)));
        // exchange_charge_bps_x100(SENSEX, 2024-10-01) == 3250; 2024-09-30
        // refuses.
        let (s, r) = table(BSE_ETC_OPTIONS_REGIMES);
        assert_eq!(regime_rate_checked(&s, &r, 739_160), Ok((CODE_OK, 3_250)));
        assert_eq!(regime_rate_checked(&s, &r, 739_159), Ok((CODE_REFUSAL, 0)));
    }

    /// Every row of every mirrored table pinned against the Python-derived
    /// literals — value-for-value INCLUDING the refusal rows (the audit
    /// pin the lane instruction requires).
    #[test]
    fn table_mirrors_pin_python_derived_literals() {
        assert_eq!(
            STT_OPTIONS_REGIMES,
            &[(1, 6_250), (739_160, 10_000), (739_707, 15_000)]
        );
        assert_eq!(STT_EXERCISE_REGIMES, &[(1, 12_500), (739_707, 15_000)]);
        assert_eq!(NSE_ETC_OPTIONS_REGIMES, &[(1, -1), (739_160, 3_503)]);
        assert_eq!(BSE_ETC_OPTIONS_REGIMES, &[(1, -1), (739_160, 3_250)]);
        // Boundary-date ordinals (Python: date(2024,10,1)/date(2026,4,1)).
        assert_eq!(STT_OPTIONS_REGIMES[1].0, 739_160);
        assert_eq!(STT_OPTIONS_REGIMES[2].0, 739_707);
    }

    #[test]
    fn empty_table_and_pre_history_codes() {
        assert_eq!(regime_rate_checked(&[], &[], 5), Ok((CODE_EMPTY_TABLE, 0)));
        // Pre-history: on < starts[0] (a caller-supplied table without a
        // date.min anchor — constants.py:364-369).
        assert_eq!(
            regime_rate_checked(&[100], &[42], 99),
            Ok((CODE_PRE_HISTORY, 0))
        );
        // Exactly ON the first start is NOT pre-history.
        assert_eq!(regime_rate_checked(&[100], &[42], 100), Ok((CODE_OK, 42)));
    }

    #[test]
    fn refusal_row_reports_selected_index() {
        // A mid-table refusal row must report ITS index (Python composes
        // verified_from from regimes[idx + 1].start — constants.py:378).
        let starts = [1i64, 100, 200];
        let rates = [5i64, REFUSAL_RATE, 7];
        assert_eq!(regime_rate_checked(&starts, &rates, 150), Ok((1, 1)));
        assert_eq!(regime_rate_checked(&starts, &rates, 100), Ok((1, 1)));
        assert_eq!(regime_rate_checked(&starts, &rates, 99), Ok((CODE_OK, 5)));
        assert_eq!(regime_rate_checked(&starts, &rates, 200), Ok((CODE_OK, 7)));
    }

    #[test]
    fn err_on_poisoned_input() {
        // Length mismatch.
        assert!(regime_rate_checked(&[1, 2], &[5], 3).is_err());
        // Non-ascending starts.
        assert!(regime_rate_checked(&[2, 1], &[5, 6], 3).is_err());
        // Duplicate starts.
        assert!(regime_rate_checked(&[1, 1], &[5, 6], 3).is_err());
        // A rate below the -1 sentinel (unrepresentable Python-side).
        assert!(regime_rate_checked(&[1], &[-2], 3).is_err());
    }

    // ── properties ────────────────────────────────────────────────────

    proptest! {
        /// Differential vs a naive linear-scan reference over arbitrary
        /// ascending tables: same code, same value, deterministic.
        #[test]
        fn matches_naive_linear_scan(
            rows in proptest::collection::btree_map(
                1i64..4_000_000, -1i64..=20_000, 1..6,
            ),
            on in 0i64..4_000_001,
        ) {
            let starts: Vec<i64> = rows.keys().copied().collect();
            let rates: Vec<i64> = rows.values().copied().collect();
            let got = regime_rate_checked(&starts, &rates, on)
                .unwrap_or_else(|e| panic!("unexpected Err: {e}"));
            // Naive reference: walk the rows, keep the last with start <= on.
            let want = if on < starts[0] {
                (CODE_PRE_HISTORY, 0)
            } else {
                let mut sel = 0usize;
                for (i, &s) in starts.iter().enumerate() {
                    if s <= on {
                        sel = i;
                    }
                }
                if rates[sel] == REFUSAL_RATE {
                    (CODE_REFUSAL, sel as i64)
                } else {
                    (CODE_OK, rates[sel])
                }
            };
            prop_assert_eq!(got, want);
            let again = regime_rate_checked(&starts, &rates, on)
                .unwrap_or_else(|e| panic!("unexpected Err: {e}"));
            prop_assert_eq!(got, again);
        }
    }
}
