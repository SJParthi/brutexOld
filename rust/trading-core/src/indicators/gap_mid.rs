//! Session-edge gap-midpoint level (DEC-GAP-MIDPOINT-CONDITIONS-001).
//!
//! The ES Trading Academy spreadsheet's "exact reversal" level, generalized
//! per-timeframe: on a gap day the level is the midpoint `(X1 + X2) / 2` of two
//! session-edge candle extremes —
//!
//! * **X1** — the prior session's LAST completed TF bar (HIGH on a gap-up day,
//!   LOW on a gap-down day);
//! * **X2** — today's FIRST completed TF bar (same extreme).
//!
//! The gap DIRECTION is `sign(open_of_day − pdc)` — the exact sign, no
//! threshold (the existing `gap_up_day`/`gap_down_day` bits 48/49 carry the
//! 0.5% threshold; masks compose by AND, so the thresholded variant is swept
//! for free). A ZERO gap (`open_of_day == pdc`) means no level exists.
//!
//! Pure function of its inputs (int64 paisa), no state — the stateful X1/X2
//! capture + the arm-from-bar-2 law live upstream (Python Stage-1A / the
//! `stage1a_batch` twin); this module owns only the level ARITHMETIC, shared
//! bit-identically by BruteX (PyO3) and TickVault (native) per
//! DEC-SHARED-CORE-001.

use crate::indicators::round_half_even;

/// The gap direction resolved from `sign(open_of_day − pdc)`.
///
/// Selects WHICH extreme pair mints the level: `Up` → the two HIGHs,
/// `Down` → the two LOWs. There is no `Flat` variant — a zero gap yields
/// `None` from [`compute_gap_mid`] (no level exists at all).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum GapDirection {
    /// `open_of_day > pdc` — the level is the midpoint of the two HIGHs.
    Up,
    /// `open_of_day < pdc` — the level is the midpoint of the two LOWs.
    Down,
}

/// Compute the session-edge gap-midpoint level, or `None` on a zero gap.
///
/// * `x1_hi_paisa` / `x1_lo_paisa` — the prior session's LAST completed TF
///   bar's high/low (int64 paisa).
/// * `x2_hi_paisa` / `x2_lo_paisa` — today's FIRST completed TF bar's high/low.
/// * `open_of_day_paisa` / `pdc_paisa` — today's open and the previous
///   session's close; their exact sign difference is the gap direction.
///
/// Returns `Some((gap_mid_paisa, direction))` where `gap_mid_paisa` is
/// `round_half_even((X1 + X2) / 2)` over the direction's extreme pair — the
/// house paisa-rounding law the pivot/fib kernels use — or `None` when
/// `open_of_day_paisa == pdc_paisa` (no gap ⇒ no level; the Group-15 bits
/// stay 0).
///
/// Like `compute_pivot_levels`, corrupt candle extremes (`hi < lo` on either
/// candle) fail LOUD via an always-on `assert!` — never a silent wrong level;
/// the PyO3 boundary pre-validates and raises a clean `ValueError` instead
/// (`python.rs::py_compute_gap_mid`). The midpoint float is exact: the FFI
/// boundary bounds every operand to `MAX_BAR_PAISA ≈ 1.4·10¹⁴`, so the sum
/// (≤ 2.8·10¹⁴) sits far inside f64's 2⁵³ exact-integer range and `/ 2.0`
/// is exact (a power of two).
#[must_use]
pub fn compute_gap_mid(
    x1_hi_paisa: i64,
    x1_lo_paisa: i64,
    x2_hi_paisa: i64,
    x2_lo_paisa: i64,
    open_of_day_paisa: i64,
    pdc_paisa: i64,
) -> Option<(i64, GapDirection)> {
    // Always-on asserts (the compute_pivot_levels law): corrupt candle
    // extremes fail loud, never a silent wrong level.
    assert!(x1_hi_paisa >= x1_lo_paisa, "X1 high must be >= X1 low");
    assert!(x2_hi_paisa >= x2_lo_paisa, "X2 high must be >= X2 low");

    if open_of_day_paisa == pdc_paisa {
        // Zero gap: no level exists (the design's no-threshold law — the
        // EXACT sign decides; a zero difference has no sign).
        return None;
    }
    let (a, b, direction) = if open_of_day_paisa > pdc_paisa {
        (x1_hi_paisa, x2_hi_paisa, GapDirection::Up)
    } else {
        (x1_lo_paisa, x2_lo_paisa, GapDirection::Down)
    };
    // Float only here: the pair midpoint, rounded half-even to whole paisa
    // (exact for FFI-bounded operands — see the doc comment).
    let gap_mid = round_half_even((a + b) as f64 / 2.0);
    Some((gap_mid, direction))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn gap_up_uses_the_two_highs() {
        // X1 = prev session's last candle (hi 2_401_000), X2 = today's first
        // candle (hi 2_407_000); open 2_405_000 > pdc 2_400_000 ⇒ gap-UP ⇒
        // midpoint of the HIGHs = (2_401_000 + 2_407_000) / 2 = 2_404_000.
        let got = compute_gap_mid(
            2_401_000, 2_399_000, 2_407_000, 2_404_500, 2_405_000, 2_400_000,
        );
        assert_eq!(got, Some((2_404_000, GapDirection::Up)));
    }

    #[test]
    fn gap_down_uses_the_two_lows() {
        // open 2_395_000 < pdc 2_400_000 ⇒ gap-DOWN ⇒ midpoint of the LOWs =
        // (2_399_000 + 2_394_000) / 2 = 2_396_500.
        let got = compute_gap_mid(
            2_401_000, 2_399_000, 2_396_000, 2_394_000, 2_395_000, 2_400_000,
        );
        assert_eq!(got, Some((2_396_500, GapDirection::Down)));
    }

    #[test]
    fn zero_gap_yields_no_level() {
        // open_of_day == pdc exactly ⇒ None (all three Group-15 bits stay 0).
        let got = compute_gap_mid(
            2_401_000, 2_399_000, 2_407_000, 2_404_500, 2_400_000, 2_400_000,
        );
        assert_eq!(got, None);
    }

    #[test]
    fn one_paisa_gap_arms_the_level() {
        // The no-threshold law: a 1-paisa gap already mints a level (bits
        // 48/49's 0.5% threshold deliberately does NOT apply here).
        let up = compute_gap_mid(100, 90, 104, 95, 2_400_001, 2_400_000);
        assert_eq!(up, Some((102, GapDirection::Up)));
        let down = compute_gap_mid(100, 90, 104, 96, 2_399_999, 2_400_000);
        assert_eq!(down, Some((93, GapDirection::Down)));
    }

    #[test]
    fn odd_sum_rounds_half_even() {
        // (101 + 104) / 2 = 102.5 → half-even → 102 (Python round() law).
        let got = compute_gap_mid(101, 90, 104, 95, 200, 100);
        assert_eq!(got, Some((102, GapDirection::Up)));
        // (103 + 104) / 2 = 103.5 → half-even → 104.
        let got = compute_gap_mid(103, 90, 104, 95, 200, 100);
        assert_eq!(got, Some((104, GapDirection::Up)));
    }

    #[test]
    fn equal_extremes_yield_the_shared_value() {
        // X1 == X2 ⇒ the midpoint IS that value (the property suite's
        // equality-only-when-equal case, pinned deterministically).
        let got = compute_gap_mid(2_400_000, 2_390_000, 2_400_000, 2_395_000, 300, 200);
        assert_eq!(got, Some((2_400_000, GapDirection::Up)));
    }

    #[test]
    fn spreadsheet_parity_nifty_3minute_shape() {
        // The TF=3minute spreadsheet arithmetic on a plain-decimal shape:
        // X1 = 24,010.00 (prev 15:27 bucket high), X2 = 24,062.50 (today's
        // 09:15–09:17 bucket high) ⇒ (X1+X2)/2 = 24,036.25 = 2_403_625 paisa.
        let got = compute_gap_mid(
            2_401_000, 2_399_500, 2_406_250, 2_403_000, 2_405_000, 2_398_000,
        );
        assert_eq!(got, Some((2_403_625, GapDirection::Up)));
    }

    #[test]
    #[should_panic(expected = "X1 high must be >= X1 low")]
    fn corrupt_x1_candle_fails_loud() {
        let _ = compute_gap_mid(90, 100, 104, 95, 200, 100);
    }

    #[test]
    #[should_panic(expected = "X2 high must be >= X2 low")]
    fn corrupt_x2_candle_fails_loud() {
        let _ = compute_gap_mid(100, 90, 95, 104, 200, 100);
    }
}
