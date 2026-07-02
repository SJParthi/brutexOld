//! Property-based tests for the shared Black-76 Greeks kernel
//! (`DEC-GREEKS-ENGINE-PR2-001`; plan `docs/GREEKS_ENGINE_PLAN.md` §12).
//!
//! Families (mirroring `tests/proptests.rs`):
//!
//! 1. **Universal invariants** over the finance-safe domain — signed delta
//!    bounds, gamma/vega ≥ 0, put-call parity, strike monotonicity, the
//!    `r = 0` theta sign, determinism.
//! 2. **Round-trip** — `sigma → price → solve` recovers sigma wherever the
//!    tuple is admissible (well-conditioned), and EVERY accepted solve
//!    reprices its input within the kernel's own verification tolerance.
//! 3. **Never-panic / clean-error taxonomy** — arbitrary (finite) inputs
//!    produce `Ok` or an `Err` carrying exactly one of the two documented
//!    prefixes; garbage never aborts the process (`panic = "abort"` would
//!    SIGABRT a host interpreter).
//!
//! Domain: rupee-scale forwards `[100, 100_000]` (covers NIFTY / BANKNIFTY /
//! SENSEX and every F&O stock), strikes within ±50% moneyness, `T` from the
//! 600 s floor to 1 year, `r ∈ [0, 0.15]`, `σ ∈ [0.01, 2.0]` (the liquid
//! band; the clamp edges are pinned by unit tests).

use proptest::prelude::*;
use trading_core::greeks::{
    DELTA_ADMISSION_FLOOR, ERR_INVALID, ERR_NO_SOLUTION, T_FLOOR_YEARS, VERIFY_REL_TOL,
};
use trading_core::{black76_price, greeks};

/// Liquid-band strategy: `(forward, strike, t_years, rate, sigma, is_call)`.
fn liquid_domain() -> impl Strategy<Value = (f64, f64, f64, f64, f64, bool)> {
    (
        100.0f64..100_000.0,
        0.5f64..1.5,
        T_FLOOR_YEARS..1.0,
        0.0f64..0.15,
        0.01f64..2.0,
        any::<bool>(),
    )
        .prop_map(|(forward, moneyness, t, rate, sigma, is_call)| {
            (forward, forward * moneyness, t, rate, sigma, is_call)
        })
}

proptest! {
    /// Delta is signed and bounded: CE ∈ [0, 1], PE ∈ [−1, 0] (plan §12).
    #[test]
    fn delta_is_signed_and_bounded(args in liquid_domain()) {
        let (f, k, t, r, sigma, is_call) = args;
        let out = greeks(None, Some(sigma), f, k, t, r, is_call).unwrap();
        if is_call {
            prop_assert!((0.0..=1.0).contains(&out.delta), "call delta {}", out.delta);
        } else {
            prop_assert!((-1.0..=0.0).contains(&out.delta), "put delta {}", out.delta);
        }
    }

    /// Gamma and vega are non-negative pre-expiry, for calls AND puts, and the
    /// pair is side-independent (plan §2.2).
    #[test]
    fn gamma_and_vega_nonnegative_and_side_independent(args in liquid_domain()) {
        let (f, k, t, r, sigma, _) = args;
        let call = greeks(None, Some(sigma), f, k, t, r, true).unwrap();
        let put = greeks(None, Some(sigma), f, k, t, r, false).unwrap();
        prop_assert!(call.gamma >= 0.0 && call.vega >= 0.0);
        prop_assert!((call.gamma - put.gamma).abs() <= 1e-12 * call.gamma.max(1e-300));
        prop_assert!((call.vega - put.vega).abs() <= 1e-9 * call.vega.max(1e-300));
    }

    /// Put-call parity: `c − p = e^(−rT)·(F − K)` to float noise (plan §12).
    #[test]
    fn put_call_parity_residual_is_noise(args in liquid_domain()) {
        let (f, k, t, r, sigma, _) = args;
        let call = greeks(None, Some(sigma), f, k, t, r, true).unwrap();
        let put = greeks(None, Some(sigma), f, k, t, r, false).unwrap();
        let df = (-r * t).exp();
        let residual = call.price - put.price - df * (f - k);
        prop_assert!(residual.abs() <= 1e-9 * f, "PCP residual {residual}");
    }

    /// Monotonicity in strike: the call price is non-increasing and the put
    /// price non-decreasing in K; the call delta is non-increasing (plan §12).
    #[test]
    fn prices_and_call_delta_are_monotone_in_strike(
        args in liquid_domain(),
        bump in 1.001f64..1.25,
    ) {
        let (f, k, t, r, sigma, _) = args;
        let k_hi = k * bump;
        let call_lo = greeks(None, Some(sigma), f, k, t, r, true).unwrap();
        let call_hi = greeks(None, Some(sigma), f, k_hi, t, r, true).unwrap();
        let put_lo = greeks(None, Some(sigma), f, k, t, r, false).unwrap();
        let put_hi = greeks(None, Some(sigma), f, k_hi, t, r, false).unwrap();
        let slack = 1e-12 * f;
        prop_assert!(call_hi.price <= call_lo.price + slack);
        prop_assert!(put_hi.price + slack >= put_lo.price);
        prop_assert!(call_hi.delta <= call_lo.delta + 1e-12);
    }

    /// At `r = 0` a long option's theta is never positive (the unconditional
    /// form of the plan's "theta ≤ 0 long premium"; with `r > 0` Black-76
    /// deep-ITM theta can legitimately turn positive — module docs).
    #[test]
    fn zero_rate_theta_never_positive(args in liquid_domain()) {
        let (f, k, t, _, sigma, is_call) = args;
        let out = greeks(None, Some(sigma), f, k, t, 0.0, is_call).unwrap();
        prop_assert!(out.theta <= 0.0, "r=0 theta {}", out.theta);
    }

    /// Round-trip: price the sigma path, solve it back. Wherever the tuple is
    /// ADMISSIBLE (the plan's own well-conditioned floor — vega above 1e-6 so
    /// the price actually pins sigma at f64 resolution), the solved IV matches
    /// the generating sigma tightly; and EVERY accepted solve reprices the
    /// input premium within the kernel's verification tolerance.
    #[test]
    fn iv_round_trip_recovers_sigma_where_admissible(args in liquid_domain()) {
        let (f, k, t, r, sigma, is_call) = args;
        let priced = greeks(None, Some(sigma), f, k, t, r, is_call).unwrap();
        if !priced.price.is_normal() {
            return Ok(()); // premium below f64-normal — unrepresentable, skip
        }
        match greeks(Some(priced.price), None, f, k, t, r, is_call) {
            Ok(solved) => {
                prop_assert!(
                    (solved.price - priced.price).abs() <= VERIFY_REL_TOL * priced.price,
                    "reprice residual {}", (solved.price - priced.price).abs()
                );
                if priced.admissible {
                    // What the kernel GUARANTEES is the price round-trip
                    // (`|reprice − price| ≤ VERIFY_REL_TOL·price`); mapped
                    // through the local slope that is a sigma envelope of
                    // (VERIFY_REL_TOL·price + a few ULPs of price) / vega_raw
                    // (vega_raw = vega·100 — the price-quantization
                    // conditioning), plus float noise.
                    let vega_raw = priced.vega * 100.0;
                    let allowed = (VERIFY_REL_TOL * priced.price
                        + 8.0 * priced.price * f64::EPSILON)
                        / vega_raw
                        + 1e-9 * sigma;
                    prop_assert!(
                        (solved.iv - sigma).abs() <= allowed,
                        "iv drift {} vs {} (allowed {allowed:e})", solved.iv, sigma
                    );
                }
            }
            Err(err) => {
                // An honest NA is allowed ONLY for the ill-conditioned family
                // (a premium indistinguishable from intrinsic / the clamp).
                prop_assert!(err.starts_with(ERR_NO_SOLUTION), "{err}");
                prop_assert!(!priced.admissible, "admissible tuple must solve: {err}");
            }
        }
    }

    /// Below the 600 s T-floor the kernel returns the documented honest-NA
    /// error, never a number (plan §9 rows 3/4).
    #[test]
    fn below_t_floor_is_always_no_solution(
        args in liquid_domain(),
        shrink in 0.0f64..0.999,
    ) {
        let (f, k, _, r, sigma, is_call) = args;
        let t = T_FLOOR_YEARS * shrink;
        let err = greeks(None, Some(sigma), f, k, t, r, is_call).unwrap_err();
        prop_assert!(err.contains("t_below_floor"), "{err}");
    }

    /// Determinism: the same inputs produce bit-identical outputs (Golden
    /// Rule #7 — same inputs → same outputs, bit-for-bit).
    #[test]
    fn kernel_is_deterministic(args in liquid_domain()) {
        let (f, k, t, r, sigma, is_call) = args;
        let a = greeks(None, Some(sigma), f, k, t, r, is_call).unwrap();
        let b = greeks(None, Some(sigma), f, k, t, r, is_call).unwrap();
        prop_assert!(a == b, "non-deterministic kernel");
        let price = black76_price(sigma, f, k, t, r, is_call);
        let s1 = greeks(Some(price), None, f, k, t, r, is_call);
        let s2 = greeks(Some(price), None, f, k, t, r, is_call);
        prop_assert_eq!(s1, s2);
    }

    /// Never-panic taxonomy: ARBITRARY finite inputs (including garbage —
    /// negative forwards, absurd rates, tiny T) always yield `Ok` or an `Err`
    /// with exactly one documented prefix. A panic here would abort the whole
    /// host process under `panic = "abort"`.
    #[test]
    fn arbitrary_finite_inputs_never_panic(
        price in proptest::option::of(-1e9f64..1e9),
        sigma in proptest::option::of(-10.0f64..10.0),
        forward in -1e9f64..1e9,
        strike in -1e9f64..1e9,
        t_years in -1.0f64..2.0,
        rate in -2.0f64..2.0,
        is_call in any::<bool>(),
    ) {
        match greeks(price, sigma, forward, strike, t_years, rate, is_call) {
            Ok(out) => {
                prop_assert!(out.iv.is_finite() && out.price.is_finite());
                prop_assert!(out.delta.is_finite() && out.gamma.is_finite());
                prop_assert!(out.theta.is_finite() && out.vega.is_finite() && out.rho.is_finite());
            }
            Err(err) => prop_assert!(
                err.starts_with(ERR_INVALID) || err.starts_with(ERR_NO_SOLUTION),
                "unknown error prefix: {err}"
            ),
        }
    }

    /// The admission floor is exactly the documented predicate: an admissible
    /// tuple always carries |delta| ≥ 0.02 (plan §11 Phase 2 floors).
    #[test]
    fn admissible_implies_delta_floor(args in liquid_domain()) {
        let (f, k, t, r, sigma, is_call) = args;
        let out = greeks(None, Some(sigma), f, k, t, r, is_call).unwrap();
        if out.admissible {
            prop_assert!(out.delta.abs() >= DELTA_ADMISSION_FLOOR);
        }
    }
}
