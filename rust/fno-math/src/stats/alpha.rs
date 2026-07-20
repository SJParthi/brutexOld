//! ALPHA-vs-BUY&HOLD benchmark kernel (DEC-ALPHA-BUYHOLD-METRIC-001).
//!
//! The operator-ordered institutional precision layer: per persisted
//! strategy candidate, `alpha_pct` = the strategy's percent return on its
//! sweep notional MINUS the instrument's buy-&-hold percent return over the
//! SAME window (the R27 baseline of DEC-SWEEP-RESULTS-DEPTH-001 — anchor =
//! the first session's first bar OPEN, close = the last session's last bar
//! CLOSE, both from the sweep's OWN bars, both exact 2-dp-paisa integers).
//!
//! ## The one formula (fixed IEEE-754 op order — the bit-identity law)
//!
//! ```text
//! strat_pct = (net_pnl_paisa as f64) / (start_equity_paisa as f64) * 100.0
//! bh_pct    = ((last_close_paisa - anchor_open_paisa) as f64)
//!             / (anchor_open_paisa as f64) * 100.0
//! alpha_pct = strat_pct - bh_pct
//! ```
//!
//! CONVERT-FIRST is load-bearing: each i64 is converted to f64 (IEEE
//! round-to-nearest, ties-to-even — identical to CPython's `float(int)`)
//! BEFORE any division, so both arms perform the SAME seven IEEE ops in the
//! SAME order (2×convert, div, mul for each leg; one subtract). The Python
//! oracle (`brutex.engine.alpha_metric.alpha_vs_buyhold_pct_python`) writes
//! `float(a) / float(b)` explicitly for the same reason — a bare `a / b`
//! over Python ints would be ONE correctly-rounded exact-rational division,
//! which DIFFERS from convert-then-divide for |values| > 2⁵³. No FMA (Rust
//! never contracts f64 `*`/`-` into fused ops), no reassociation — the
//! Rust↔Python differential property proves bit-identity over the hostile
//! ±2⁶⁰-paisa domain.
//!
//! ## Structural properties (pinned by tests both sides)
//!
//! * `alpha_pct == strat_pct - bh_pct` EXACTLY (it IS that one IEEE
//!   subtraction — the legs are recomputable bit-identically by any caller).
//! * Sign law: `alpha_pct > 0 ⇔ strat_pct > bh_pct` over the COMPUTED legs
//!   (IEEE-754 with gradual underflow: `x - y == 0 ⇔ x == y` for finite
//!   doubles, so the subtraction can never collapse a real gap to zero nor
//!   invent one).
//! * `alpha == 0` when the strategy return equals the buy-&-hold return
//!   (e.g. `net_pnl = last_close - anchor` with `start_equity = anchor`
//!   makes the two legs the same expression — alpha is exactly ±0.0).
//!
//! HONEST LIMIT (Golden Rule #10): the sign law binds the COMPUTED f64
//! legs. Each leg is itself ≤ 3 correctly-rounded ops from exact integers,
//! so at the hostile 2⁶⁰-paisa extremes the EXACT-RATIONAL sign can differ
//! from the computed sign inside a ≲2-ulp band around alpha == 0 — stated,
//! never papered over. Production magnitudes (≤ 10¹² paisa ≈ ₹100 crore)
//! sit far inside the exact-f64 integer range where every conversion is
//! exact.
//!
//! ## Failure mode — `Result`, NEVER a panic
//!
//! Under the crate's `overflow-checks = true` + `panic = "abort"` release
//! profile an unchecked i64 overflow would SIGABRT the host interpreter.
//! Validation runs FIRST — `start_equity_paisa` and `anchor_open_paisa`
//! must be at least 1 and `last_close_paisa` at least 0 (a stored price is
//! never negative) — which makes the one subtraction provably overflow-free
//! (`0 - i64::MAX <= diff <= i64::MAX - 1`). The Python facade validates
//! the SAME domain first, so these error arms are structurally unreachable
//! through it — they exist for the fallback law (any kernel error routes to
//! warn-once + the Python oracle, never a new crash class).
//!
//! ## Complexity (honest envelope)
//!
//! O(1) per candidate — four scalar i64s in, one f64 out, seven IEEE ops.
//! The FFI hop itself costs more than the arithmetic; the kernel exists for
//! the operator's BORN-RUST uniformity mandate, not for speed (payoff ≈ 0 —
//! the ADR-0020 banner applies).

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

/// Pure core of [`alpha_vs_buyhold_pct`] — `Result<_, String>` (NO `PyErr`)
/// so the no-libpython `extension-module` `cargo test` link exercises every
/// branch (the `stats::cpcv::cpcv_pass_count_checked` split).
pub fn alpha_vs_buyhold_checked(
    net_pnl_paisa: i64,
    start_equity_paisa: i64,
    anchor_open_paisa: i64,
    last_close_paisa: i64,
) -> Result<f64, String> {
    if start_equity_paisa < 1 {
        return Err(format!(
            "alpha_vs_buyhold_pct: start_equity_paisa must be >= 1, got {start_equity_paisa}"
        ));
    }
    if anchor_open_paisa < 1 {
        return Err(format!(
            "alpha_vs_buyhold_pct: anchor_open_paisa must be >= 1, got {anchor_open_paisa}"
        ));
    }
    if last_close_paisa < 0 {
        return Err(format!(
            "alpha_vs_buyhold_pct: last_close_paisa must be >= 0, got {last_close_paisa}"
        ));
    }
    // Post-validation the subtraction is provably in-range (module docstring),
    // so the crate's overflow-checks profile can never trip here.
    let strat_pct = (net_pnl_paisa as f64) / (start_equity_paisa as f64) * 100.0;
    let diff = last_close_paisa - anchor_open_paisa;
    let bh_pct = (diff as f64) / (anchor_open_paisa as f64) * 100.0;
    Ok(strat_pct - bh_pct)
}

/// ALPHA vs buy-&-hold, percent — the DEC-ALPHA-BUYHOLD-METRIC-001 kernel.
///
/// `net_pnl_paisa` = the candidate's signal-only gross P&L (int paisa);
/// `start_equity_paisa` = the sweep grid's start equity (the strategy
/// notional); `anchor_open_paisa` / `last_close_paisa` = the R27 baseline's
/// anchor OPEN and final session CLOSE as exact 2-dp paisa integers.
/// Returns the f64 `alpha_pct` per the module-docstring fixed-op-order law.
/// Raises `ValueError` on a domain violation; the Python facade validates
/// the same domain first and routes any kernel error to the retained
/// pure-Python oracle (warn-once), never a new crash class.
///
/// FFI cost (disclosed): four scalar i64 conversions per call — O(1).
#[pyfunction]
pub fn alpha_vs_buyhold_pct(
    net_pnl_paisa: i64,
    start_equity_paisa: i64,
    anchor_open_paisa: i64,
    last_close_paisa: i64,
) -> PyResult<f64> {
    alpha_vs_buyhold_checked(
        net_pnl_paisa,
        start_equity_paisa,
        anchor_open_paisa,
        last_close_paisa,
    )
    .map_err(PyValueError::new_err)
}

#[cfg(test)]
mod tests {
    use super::alpha_vs_buyhold_checked;

    /// Hand-derived exact vector: strategy +10% on ₹10,00,000 equity
    /// (net +₹1,00,000 = 10_000_000 paisa on 100_000_000 paisa) vs B&H
    /// +4% (anchor ₹25,000.00 = 2_500_000 paisa → close ₹26,000.00 =
    /// 2_600_000 paisa). alpha = 10.0 − 4.0 = 6.0 exactly (all values
    /// exact in f64).
    #[test]
    fn hand_derived_plus_ten_minus_four() {
        let alpha = alpha_vs_buyhold_checked(10_000_000, 100_000_000, 2_500_000, 2_600_000)
            .expect("valid domain");
        assert_eq!(alpha, 6.0);
    }

    /// Hand-derived negative alpha: strategy −5% vs B&H +2.5% → −7.5.
    /// net −₹50,000 on ₹10,00,000; anchor 2_000_000 → close 2_050_000.
    #[test]
    fn hand_derived_negative_alpha() {
        let alpha = alpha_vs_buyhold_checked(-5_000_000, 100_000_000, 2_000_000, 2_050_000)
            .expect("valid domain");
        assert_eq!(alpha, -7.5);
    }

    /// The zero law: identical strategy and B&H returns → exactly 0.0
    /// (net_pnl == close − anchor over equity == anchor makes the two legs
    /// the SAME expression).
    #[test]
    fn zero_alpha_when_strategy_equals_buyhold() {
        let alpha =
            alpha_vs_buyhold_checked(100_000, 2_500_000, 2_500_000, 2_600_000).expect("valid");
        assert_eq!(alpha, 0.0);
    }

    /// Sign law at the extremes: alpha > 0 ⇔ computed strat leg > bh leg,
    /// exercised across sign boundaries incl. the ±2⁶⁰ hostile corners.
    #[test]
    fn sign_matches_computed_legs() {
        let cases: [(i64, i64, i64, i64); 6] = [
            (1, 1 << 60, 1, 1),                         // tiny positive strat, flat B&H
            (-1, 1 << 60, 1, 1),                        // tiny negative strat, flat B&H
            (1 << 60, 1, 1, 0),                         // huge strat, total B&H loss
            (-(1 << 60), 1, 1, 1 << 60),                // huge loss vs huge B&H gain
            (0, 1, 1 << 60, (1 << 60) - 1),             // flat strat vs tiny B&H loss
            ((1 << 60) - 1, (1 << 60) - 3, 7, 1 << 59), // odd magnitudes
        ];
        for (pnl, eq, anchor, close) in cases {
            let strat = (pnl as f64) / (eq as f64) * 100.0;
            let bh = ((close - anchor) as f64) / (anchor as f64) * 100.0;
            let alpha = alpha_vs_buyhold_checked(pnl, eq, anchor, close).expect("valid");
            assert_eq!(alpha, strat - bh, "exact-subtraction law");
            assert_eq!(
                alpha > 0.0,
                strat > bh,
                "sign law for {pnl}/{eq}/{anchor}/{close}"
            );
            assert_eq!(
                alpha == 0.0,
                strat == bh,
                "zero law for {pnl}/{eq}/{anchor}/{close}"
            );
        }
    }

    /// Domain refusals — each names the offending field + value.
    #[test]
    fn domain_refusals() {
        let err = alpha_vs_buyhold_checked(0, 0, 1, 1).unwrap_err();
        assert!(err.contains("start_equity_paisa must be >= 1"), "{err}");
        let err = alpha_vs_buyhold_checked(0, -5, 1, 1).unwrap_err();
        assert!(err.contains("start_equity_paisa must be >= 1"), "{err}");
        let err = alpha_vs_buyhold_checked(0, 1, 0, 1).unwrap_err();
        assert!(err.contains("anchor_open_paisa must be >= 1"), "{err}");
        let err = alpha_vs_buyhold_checked(0, 1, 1, -1).unwrap_err();
        assert!(err.contains("last_close_paisa must be >= 0"), "{err}");
    }

    /// The i64::MAX corners stay panic-free under overflow-checks (the
    /// validated domain makes the one subtraction provably in-range).
    #[test]
    fn extreme_corners_never_panic() {
        let alpha = alpha_vs_buyhold_checked(i64::MAX, i64::MAX, i64::MAX, 0).expect("valid");
        assert!(alpha.is_finite());
        let alpha = alpha_vs_buyhold_checked(i64::MIN, 1, 1, i64::MAX).expect("valid");
        assert!(alpha.is_finite());
    }
}
