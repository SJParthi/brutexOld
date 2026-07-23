//! The abstention-first gate stream's per-element kernel
//! (DEC-ABSTAIN-GATE-STREAM-001 — ANNOTATION-ONLY today).
//!
//! One pure function, [`abstain_masks`]: for each trade-entry snapshot
//! (regime code, VIX level, event flag, model confidence) judge every gate
//! door INDEPENDENTLY and return a reason BITMASK per element. Bit order is
//! the stable `ABSTAIN_REASON_ORDER` wire vocabulary of
//! `brutex/meta/abstain.py` (mirror-pinned by the Python facade's tests):
//!
//! * bit 0 — `regime_mismatch`
//! * bit 1 — `vix_out_of_band`
//! * bit 2 — `vix_missing`
//! * bit 3 — `event_day`
//! * bit 4 — `low_confidence`
//!
//! `0` = every configured door passes. ALL doors are judged independently
//! (no short-circuit — the `brutex/ranking/__init__.py::gate_reasons`
//! template), so a snapshot failing three doors reports all three bits.
//!
//! ## Encoding conventions (the documented FFI code map)
//!
//! * `regimes` — i64 codes: `0 = up`, `1 = down`, `2 = sideways`,
//!   `3 = unknown` (`brutex/meta/abstain.py::REGIME_CODES`, the one map
//!   both sides import/pin). `allowed_mask` bit `r` set ⇔ code `r` allowed;
//!   a code outside `[0, 64)` can never be allowed → `regime_mismatch`.
//! * `vix` — f64 levels; **NaN = missing stamp**.
//! * `vix_lo` / `vix_hi` — inclusive band `[lo, hi]`; **NaN = that side
//!   unbounded**. Both NaN = no VIX gate configured.
//! * `event_flags` — u8; nonzero = a verified event day. Judged only when
//!   `block_event_days`.
//! * `confidence` — f64 P(win); **NaN = missing** (no out-of-fold
//!   coverage). `tau` — NaN = no confidence gate.
//!
//! ## Door semantics (each pinned by tests)
//!
//! * `regime_mismatch` — the code's `allowed_mask` bit is unset.
//! * `vix_out_of_band` — VIX present AND a band side is configured AND
//!   (`vix < vix_lo` or `vix > vix_hi`) — the band is INCLUSIVE both ends.
//! * `vix_missing` — a VIX gate is configured (either side finite) AND the
//!   stamp is missing. ANNOTATION-ONLY honesty: today this is a COUNTED
//!   observation in a sidecar, never an executed block; a future in-sim
//!   gating flip must decide the missing-stamp policy (fail-closed vs
//!   pass-through) under its own DEC. No gate configured → no observation
//!   emitted (the door does not exist).
//! * `event_day` — `block_event_days` AND the flag is nonzero.
//! * `low_confidence` — `tau` is configured AND NOT (`confidence >= tau`).
//!   The boundary is EXACT `>=`: `confidence == tau` PASSES (pinned). A
//!   missing (NaN) confidence under a configured `tau` reads
//!   `low_confidence` — the model cannot certify `>= tau` (fail-closed,
//!   the abstention-first posture; documented, annotation-only).
//!
//! ## Complexity (Golden Rule #5)
//!
//! O(1) per element, branch-simple (a handful of comparisons + one byte
//! write); the batch is O(n_elements) by construction.

/// `ABSTAIN_REASON_ORDER` bit positions (the stable wire order).
pub const BIT_REGIME_MISMATCH: u8 = 1 << 0;
/// VIX present but outside the configured inclusive band.
pub const BIT_VIX_OUT_OF_BAND: u8 = 1 << 1;
/// VIX gate configured but the entry stamp is missing.
pub const BIT_VIX_MISSING: u8 = 1 << 2;
/// A verified event day under `block_event_days`.
pub const BIT_EVENT_DAY: u8 = 1 << 3;
/// Confidence gate configured and `confidence >= tau` not certified.
pub const BIT_LOW_CONFIDENCE: u8 = 1 << 4;

/// Judge one entry snapshot against the gate config — the O(1) core.
///
/// Nine scalar arguments by DESIGN (the FFI's flat-scalar shape — no config
/// struct crosses the boundary, so the core mirrors the boundary's arity).
#[inline]
#[must_use]
#[allow(clippy::too_many_arguments)]
fn abstain_mask_one(
    regime: i64,
    allowed_mask: u64,
    vix: f64,
    vix_lo: f64,
    vix_hi: f64,
    event_flag: u8,
    block_event_days: bool,
    confidence: f64,
    tau: f64,
) -> u8 {
    let mut mask = 0u8;
    let allowed = u64::try_from(regime).is_ok_and(|r| r < 64 && (allowed_mask >> r) & 1 == 1);
    if !allowed {
        mask |= BIT_REGIME_MISMATCH;
    }
    let vix_gate_configured = !vix_lo.is_nan() || !vix_hi.is_nan();
    if vix_gate_configured {
        if vix.is_nan() {
            mask |= BIT_VIX_MISSING;
        } else if (!vix_lo.is_nan() && vix < vix_lo) || (!vix_hi.is_nan() && vix > vix_hi) {
            mask |= BIT_VIX_OUT_OF_BAND;
        }
    }
    if block_event_days && event_flag != 0 {
        mask |= BIT_EVENT_DAY;
    }
    // EXACT `>=` boundary (pinned): confidence == tau PASSES. The negated
    // comparison is DELIBERATE (not `confidence < tau`): a NaN confidence
    // makes `confidence >= tau` false → the door fires (fail-closed on a
    // missing P(win) under a configured tau) — `<` would read a NaN as PASS.
    #[allow(clippy::neg_cmp_op_on_partial_ord)]
    if !tau.is_nan() && !(confidence >= tau) {
        mask |= BIT_LOW_CONFIDENCE;
    }
    mask
}

/// Batch abstention-gate judgment: one reason bitmask per element.
///
/// # Errors
///
/// A `String` naming the violation when the four input slices' lengths
/// disagree — the caller degrades to the Python oracle on the mapped
/// `ValueError`. Empty inputs are first-class (empty output).
#[allow(clippy::too_many_arguments)]
pub fn abstain_masks(
    regimes: &[i64],
    allowed_mask: u64,
    vix: &[f64],
    vix_lo: f64,
    vix_hi: f64,
    event_flags: &[u8],
    block_event_days: bool,
    confidence: &[f64],
    tau: f64,
) -> Result<Vec<u8>, String> {
    let n = regimes.len();
    if vix.len() != n || event_flags.len() != n || confidence.len() != n {
        return Err(format!(
            "abstain_masks: column lengths disagree (regimes={}, vix={}, event_flags={}, confidence={})",
            n,
            vix.len(),
            event_flags.len(),
            confidence.len()
        ));
    }
    let mut out = Vec::with_capacity(n);
    for i in 0..n {
        out.push(abstain_mask_one(
            regimes[i],
            allowed_mask,
            vix[i],
            vix_lo,
            vix_hi,
            event_flags[i],
            block_event_days,
            confidence[i],
            tau,
        ));
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    const NAN: f64 = f64::NAN;
    /// All four regime codes allowed (up/down/sideways/unknown).
    const ALL_REGIMES: u64 = 0b1111;

    #[allow(clippy::too_many_arguments)] // mirrors the FFI's flat-scalar arity
    fn one(
        regime: i64,
        allowed: u64,
        vix: f64,
        lo: f64,
        hi: f64,
        flag: u8,
        block: bool,
        conf: f64,
        tau: f64,
    ) -> u8 {
        abstain_masks(
            &[regime],
            allowed,
            &[vix],
            lo,
            hi,
            &[flag],
            block,
            &[conf],
            tau,
        )
        .expect("valid")[0]
    }

    #[test]
    fn empty_inputs_are_first_class() {
        let out = abstain_masks(&[], ALL_REGIMES, &[], NAN, NAN, &[], true, &[], NAN);
        assert_eq!(out.expect("valid"), Vec::<u8>::new());
    }

    #[test]
    fn mismatched_lengths_are_err() {
        assert!(abstain_masks(&[0], ALL_REGIMES, &[], NAN, NAN, &[0], true, &[NAN], NAN).is_err());
        assert!(
            abstain_masks(&[0], ALL_REGIMES, &[1.0], NAN, NAN, &[], true, &[NAN], NAN).is_err()
        );
        assert!(abstain_masks(&[0], ALL_REGIMES, &[1.0], NAN, NAN, &[0], true, &[], NAN).is_err());
    }

    #[test]
    fn unrestricted_config_passes_everything() {
        assert_eq!(one(0, ALL_REGIMES, NAN, NAN, NAN, 1, false, NAN, NAN), 0);
        assert_eq!(one(3, ALL_REGIMES, 55.0, NAN, NAN, 0, false, 0.0, NAN), 0);
    }

    #[test]
    fn regime_mismatch_fires_on_unset_bit() {
        // Only "up" (code 0) allowed — code 1 (down) mismatches.
        assert_eq!(
            one(1, 0b0001, NAN, NAN, NAN, 0, false, NAN, NAN),
            BIT_REGIME_MISMATCH
        );
        assert_eq!(one(0, 0b0001, NAN, NAN, NAN, 0, false, NAN, NAN), 0);
    }

    #[test]
    fn out_of_range_regime_codes_always_mismatch() {
        assert_eq!(
            one(-1, u64::MAX, NAN, NAN, NAN, 0, false, NAN, NAN),
            BIT_REGIME_MISMATCH
        );
        assert_eq!(
            one(64, u64::MAX, NAN, NAN, NAN, 0, false, NAN, NAN),
            BIT_REGIME_MISMATCH
        );
        assert_eq!(
            one(i64::MIN, u64::MAX, NAN, NAN, NAN, 0, false, NAN, NAN),
            BIT_REGIME_MISMATCH
        );
        assert_eq!(
            one(i64::MAX, u64::MAX, NAN, NAN, NAN, 0, false, NAN, NAN),
            BIT_REGIME_MISMATCH
        );
        // Code 63 with the top bit set IS allowed (bit arithmetic, not luck).
        assert_eq!(one(63, 1u64 << 63, NAN, NAN, NAN, 0, false, NAN, NAN), 0);
    }

    #[test]
    fn vix_band_is_inclusive_both_ends() {
        // Band [12, 20]: the exact edges PASS.
        assert_eq!(one(0, ALL_REGIMES, 12.0, 12.0, 20.0, 0, false, NAN, NAN), 0);
        assert_eq!(one(0, ALL_REGIMES, 20.0, 12.0, 20.0, 0, false, NAN, NAN), 0);
        assert_eq!(
            one(0, ALL_REGIMES, 11.999, 12.0, 20.0, 0, false, NAN, NAN),
            BIT_VIX_OUT_OF_BAND
        );
        assert_eq!(
            one(0, ALL_REGIMES, 20.001, 12.0, 20.0, 0, false, NAN, NAN),
            BIT_VIX_OUT_OF_BAND
        );
    }

    #[test]
    fn vix_half_bounded_bands() {
        // Only a floor configured: +inf VIX passes, below-floor fails.
        assert_eq!(
            one(0, ALL_REGIMES, f64::INFINITY, 12.0, NAN, 0, false, NAN, NAN),
            0
        );
        assert_eq!(
            one(0, ALL_REGIMES, 1.0, 12.0, NAN, 0, false, NAN, NAN),
            BIT_VIX_OUT_OF_BAND
        );
        // Only a ceiling configured: -inf passes the ceiling.
        assert_eq!(
            one(
                0,
                ALL_REGIMES,
                f64::NEG_INFINITY,
                NAN,
                20.0,
                0,
                false,
                NAN,
                NAN
            ),
            0
        );
        assert_eq!(
            one(0, ALL_REGIMES, 25.0, NAN, 20.0, 0, false, NAN, NAN),
            BIT_VIX_OUT_OF_BAND
        );
    }

    #[test]
    fn vix_missing_fires_only_under_a_configured_gate() {
        assert_eq!(
            one(0, ALL_REGIMES, NAN, 12.0, 20.0, 0, false, NAN, NAN),
            BIT_VIX_MISSING
        );
        assert_eq!(
            one(0, ALL_REGIMES, NAN, 12.0, NAN, 0, false, NAN, NAN),
            BIT_VIX_MISSING
        );
        assert_eq!(
            one(0, ALL_REGIMES, NAN, NAN, 20.0, 0, false, NAN, NAN),
            BIT_VIX_MISSING
        );
        // No gate configured → no observation, never a fabricated door.
        assert_eq!(one(0, ALL_REGIMES, NAN, NAN, NAN, 0, false, NAN, NAN), 0);
    }

    #[test]
    fn event_day_judged_only_when_blocking() {
        assert_eq!(
            one(0, ALL_REGIMES, NAN, NAN, NAN, 1, true, NAN, NAN),
            BIT_EVENT_DAY
        );
        assert_eq!(
            one(0, ALL_REGIMES, NAN, NAN, NAN, 7, true, NAN, NAN),
            BIT_EVENT_DAY
        );
        assert_eq!(one(0, ALL_REGIMES, NAN, NAN, NAN, 1, false, NAN, NAN), 0);
        assert_eq!(one(0, ALL_REGIMES, NAN, NAN, NAN, 0, true, NAN, NAN), 0);
    }

    #[test]
    fn tau_boundary_is_exact_gte() {
        // confidence == tau PASSES (the pinned `>=` law).
        assert_eq!(one(0, ALL_REGIMES, NAN, NAN, NAN, 0, false, 0.7, 0.7), 0);
        // One ULP below tau fails.
        let below = f64::from_bits(0.7f64.to_bits() - 1);
        assert_eq!(
            one(0, ALL_REGIMES, NAN, NAN, NAN, 0, false, below, 0.7),
            BIT_LOW_CONFIDENCE
        );
        assert_eq!(one(0, ALL_REGIMES, NAN, NAN, NAN, 0, false, 0.9, 0.7), 0);
    }

    #[test]
    fn missing_confidence_under_tau_is_low_confidence() {
        assert_eq!(
            one(0, ALL_REGIMES, NAN, NAN, NAN, 0, false, NAN, 0.7),
            BIT_LOW_CONFIDENCE
        );
        // No tau configured → the door does not exist.
        assert_eq!(one(0, ALL_REGIMES, NAN, NAN, NAN, 0, false, NAN, NAN), 0);
    }

    #[test]
    fn all_doors_judged_independently() {
        // Every configured door fails at once → all five bits.
        let mask = one(2, 0b0001, NAN, 12.0, 20.0, 1, true, 0.1, 0.7);
        assert_eq!(
            mask,
            BIT_REGIME_MISMATCH | BIT_VIX_MISSING | BIT_EVENT_DAY | BIT_LOW_CONFIDENCE
        );
        let mask = one(2, 0b0001, 50.0, 12.0, 20.0, 1, true, 0.1, 0.7);
        assert_eq!(
            mask,
            BIT_REGIME_MISMATCH | BIT_VIX_OUT_OF_BAND | BIT_EVENT_DAY | BIT_LOW_CONFIDENCE
        );
    }

    #[test]
    fn batch_matches_per_element_calls() {
        let regimes = [0i64, 1, 2, 3, -5];
        let vix = [10.0, NAN, 15.0, 30.0, 18.0];
        let flags = [0u8, 1, 0, 1, 0];
        let conf = [0.9, 0.5, NAN, 0.7, 0.71];
        let batch = abstain_masks(&regimes, 0b0111, &vix, 12.0, 20.0, &flags, true, &conf, 0.7)
            .expect("valid");
        for i in 0..regimes.len() {
            let single = one(
                regimes[i], 0b0111, vix[i], 12.0, 20.0, flags[i], true, conf[i], 0.7,
            );
            assert_eq!(batch[i], single, "element {i}");
        }
    }
}
