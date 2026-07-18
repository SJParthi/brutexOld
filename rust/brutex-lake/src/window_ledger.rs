//! # window_ledger — the window-ledger PURE laws (ADR-0020 Wave C-3, G8 FIRST CUT)
//!
//! DEC-LAKE-RUST-WINDOW-LEDGER-001 (MIGRATION.md #1915). Mirrors, token for
//! token, the TWO pure laws of the durable window ledger:
//!
//! 1. **Status classification** —
//!    `brutex/data/window_status.py::classify_window_status`:
//!    an error is `FAILED`; a clean bars-returning fetch is `SUCCESS` unless
//!    `degraded` demotes it to `FAILED` (DEC-INTERIOR-GAP-DETECT-001 /
//!    DEC-PARTIAL-WINDOW-REPULL-001 — `degraded` is IGNORED for a genuine
//!    error or an EMPTY); a clean zero-bar fetch is `EMPTY`.
//! 2. **Merge-key date rendering** — the `date.isoformat()` leg of the
//!    5-tuple window identity `WindowLedger._key` / `WindowStatusLog._key`
//!    (`(market, security_id, interval, start.isoformat(), end.isoformat())`).
//!    ONLY the two computed date strings are rendered here; the three
//!    identity strings never cross the FFI — the Python facade assembles the
//!    tuple from the ORIGINAL Python objects (byte-identity for those
//!    elements by construction, the DEC-FEED-RUST-CORE-001 ints-only
//!    discipline: integers in, two ASCII date strings out).
//!
//! NO I/O, NO polars — the crate's polars REV pin is irrelevant to this
//! module, so the facade's engagement guard is the SYMBOL handshake
//! (both functions present + callable), NOT the polars version guard.
//!
//! HONESTY (ADR-0020 banner): expected payoff ≈ 0 — conversion is for
//! uniformity, not speed. Both laws are nanosecond-scale in Python; the FFI
//! hop is a small SLOWDOWN, accepted because the seam is per window RECORD
//! (an I/O-dominated vendor-fetch path) and ships DEFAULT-OFF. The subtle
//! stateful machinery around these laws (`record()`'s streak/tombstone
//! rules, the merge-on-save conflict law, optional-column back-compat)
//! stays PYTHON — the named full-flip follow-up
//! (`DEC-LAKE-RUST-WINDOW-LEDGER-002`, the #1548 pyclass single-source).

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

/// `window_status.SUCCESS` — clean fetch with bars.
pub const STATUS_SUCCESS: &str = "SUCCESS";
/// `window_status.EMPTY` — clean fetch with zero bars.
pub const STATUS_EMPTY: &str = "EMPTY";
/// `window_status.FAILED` — an error, or a `degraded` clean fetch.
pub const STATUS_FAILED: &str = "FAILED";

/// The pure status law — the verbatim mirror of
/// `window_status.classify_window_status`:
///
/// ```text
/// if error_type is not None: return FAILED
/// if raw_bars > 0:           return FAILED if degraded else SUCCESS
/// return EMPTY
/// ```
///
/// `error_type` participates ONLY via presence (`is_some()`), exactly like
/// the Python `is not None`; its content is never inspected. A negative
/// `raw_bars` (contract-violating in Python too) classifies `EMPTY`,
/// identical to the Python `> 0` comparison.
pub fn classify_window_status_impl(
    raw_bars: i64,
    error_type: Option<&str>,
    degraded: bool,
) -> &'static str {
    if error_type.is_some() {
        return STATUS_FAILED;
    }
    if raw_bars > 0 {
        return if degraded {
            STATUS_FAILED
        } else {
            STATUS_SUCCESS
        };
    }
    STATUS_EMPTY
}

/// The `datetime.date.isoformat()` mirror — zero-padded `YYYY-MM-DD`.
///
/// Python's `date` guarantees `MINYEAR..=MAXYEAR` (1..=9999), `1..=12`,
/// `1..=31` by construction, so through the real facade the `Err` arm is
/// unreachable; it is the honesty rail against a junk DIRECT call (a
/// hand-passed out-of-range int must refuse loudly, never render a
/// non-`isoformat` string). Day-in-month is deliberately NOT validated —
/// Python already proved it (a real `date` cannot hold Feb-30), and a
/// stricter Rust check could never fire through the facade.
pub fn date_isoformat_impl(year: i32, month: i32, day: i32) -> Result<String, String> {
    if !(1..=9999).contains(&year) {
        return Err(format!("year out of range 1..=9999: {year}"));
    }
    if !(1..=12).contains(&month) {
        return Err(format!("month out of range 1..=12: {month}"));
    }
    if !(1..=31).contains(&day) {
        return Err(format!("day out of range 1..=31: {day}"));
    }
    Ok(format!("{year:04}-{month:02}-{day:02}"))
}

/// FFI face of [`classify_window_status_impl`] — returns one of the three
/// `&'static str` status tokens. Pure, allocation-free, infallible over the
/// full `i64` × `Option<&str>` × `bool` domain (PyO3 argument conversion is
/// the only raise surface — an out-of-`i64` Python int raises
/// `OverflowError` INTO the facade's degrade set, disclosed there).
#[pyfunction]
#[pyo3(signature = (raw_bars, error_type, degraded))]
pub fn classify_window_status(
    raw_bars: i64,
    error_type: Option<&str>,
    degraded: bool,
) -> &'static str {
    classify_window_status_impl(raw_bars, error_type, degraded)
}

/// FFI face of [`date_isoformat_impl`] — `ValueError` on an out-of-range
/// component (production-unreachable through the facade; the honesty rail).
#[pyfunction]
#[pyo3(signature = (year, month, day))]
pub fn date_isoformat(year: i32, month: i32, day: i32) -> PyResult<String> {
    date_isoformat_impl(year, month, day).map_err(PyValueError::new_err)
}

#[cfg(test)]
mod tests {
    use super::*;

    // ------------------------------------------------------------------
    // classify_window_status_impl — the full truth table.
    // ------------------------------------------------------------------

    #[test]
    fn error_always_failed_regardless_of_bars_or_degraded() {
        for raw_bars in [i64::MIN, -1, 0, 1, i64::MAX] {
            for degraded in [false, true] {
                assert_eq!(
                    classify_window_status_impl(raw_bars, Some("GrowwTimeoutError"), degraded),
                    STATUS_FAILED,
                    "raw_bars={raw_bars} degraded={degraded}"
                );
            }
        }
    }

    #[test]
    fn empty_error_string_is_still_present_hence_failed() {
        // Python: `error_type is not None` — the EMPTY STRING is not None.
        assert_eq!(
            classify_window_status_impl(5, Some(""), false),
            STATUS_FAILED
        );
    }

    #[test]
    fn clean_bars_success_unless_degraded() {
        for raw_bars in [1, 2, 375, i64::MAX] {
            assert_eq!(
                classify_window_status_impl(raw_bars, None, false),
                STATUS_SUCCESS
            );
            assert_eq!(
                classify_window_status_impl(raw_bars, None, true),
                STATUS_FAILED
            );
        }
    }

    #[test]
    fn clean_zero_or_negative_bars_empty_and_degraded_ignored() {
        // Python `raw_bars > 0`: zero AND a (contract-violating) negative both
        // fall to EMPTY; `degraded` is ignored on the EMPTY arm.
        for raw_bars in [0, -1, i64::MIN] {
            for degraded in [false, true] {
                assert_eq!(
                    classify_window_status_impl(raw_bars, None, degraded),
                    STATUS_EMPTY,
                    "raw_bars={raw_bars} degraded={degraded}"
                );
            }
        }
    }

    #[test]
    fn status_tokens_match_python_constants() {
        assert_eq!(STATUS_SUCCESS, "SUCCESS");
        assert_eq!(STATUS_EMPTY, "EMPTY");
        assert_eq!(STATUS_FAILED, "FAILED");
    }

    // ------------------------------------------------------------------
    // date_isoformat_impl — padding matrix + validation rails.
    // ------------------------------------------------------------------

    #[test]
    fn isoformat_padding_matrix() {
        // Python oracle values: `date(y, m, d).isoformat()`.
        assert_eq!(date_isoformat_impl(1, 1, 1).as_deref(), Ok("0001-01-01"));
        assert_eq!(
            date_isoformat_impl(9999, 12, 31).as_deref(),
            Ok("9999-12-31")
        );
        assert_eq!(
            date_isoformat_impl(2026, 7, 16).as_deref(),
            Ok("2026-07-16")
        );
        assert_eq!(date_isoformat_impl(2020, 1, 1).as_deref(), Ok("2020-01-01"));
        assert_eq!(date_isoformat_impl(999, 10, 5).as_deref(), Ok("0999-10-05"));
        assert_eq!(date_isoformat_impl(45, 11, 30).as_deref(), Ok("0045-11-30"));
    }

    #[test]
    fn isoformat_rejects_out_of_range_components() {
        for (y, m, d) in [
            (0, 1, 1),
            (10_000, 1, 1),
            (-1, 1, 1),
            (2026, 0, 1),
            (2026, 13, 1),
            (2026, 7, 0),
            (2026, 7, 32),
            (i32::MIN, 1, 1),
            (i32::MAX, 1, 1),
        ] {
            let got = date_isoformat_impl(y, m, d);
            assert!(
                got.is_err(),
                "({y}, {m}, {d}) unexpectedly rendered {got:?}"
            );
        }
    }

    #[test]
    fn isoformat_day_31_of_any_month_renders_structurally() {
        // Structure-only validation (doc'd): a real Python `date` can never
        // hold Feb-31, so the Rust side deliberately renders it rather than
        // duplicating the calendar Python already enforced.
        assert_eq!(
            date_isoformat_impl(2026, 2, 31).as_deref(),
            Ok("2026-02-31")
        );
    }
}
