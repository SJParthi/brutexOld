//! Black-76 options-Greeks kernel + bounded Jäckel IV inversion.
//!
//! Phase 2 of the shared Greeks engine (`DEC-GREEKS-ENGINE-PR2-001`; plan
//! `docs/GREEKS_ENGINE_PLAN.md` §2 / §11 Phase 2; ADR-0015 D2-D4) — the ONE
//! compiled kernel BruteX (PyO3 wheel) and TickVault (native link) share, the
//! Greeks twin of the 60-condition vocabulary core (`DEC-SHARED-CORE-001`).
//!
//! # Model (plan §2.2, Hull 11e ch. 17/19)
//!
//! Black-76 on the **forward** `F` with discount factor `e^(−rT)`:
//!
//! ```text
//! d1 = [ln(F/K) + σ²T/2] / (σ√T)      d2 = d1 − σ√T
//! call = e^(−rT)·(F·N(d1) − K·N(d2))  put = e^(−rT)·(K·N(−d2) − F·N(−d1))
//! ```
//!
//! Output conventions (frozen into `tests/golden/fixtures/greeks_golden.json`;
//! they match the pre-DEC-P0-025 kernel recovered from
//! `a87bce43^:rust/fno-math/src/greeks/black_scholes.rs` and the common broker
//! quoting convention):
//!
//! * `delta` — signed: CE `e^(−rT)·N(d1)` ∈ \[0, 1\], PE `−e^(−rT)·N(−d1)` ∈ \[−1, 0\].
//! * `gamma` — `e^(−rT)·φ(d1) / (F·σ·√T)` ≥ 0, identical CE/PE. Per unit of `F`.
//! * `theta` — per **calendar day** (Act/365): `[r·price − e^(−rT)·F·φ(d1)·σ/(2√T)] / 365`.
//!   NOTE (honest): under Black-76 a deep-ITM option's theta can be POSITIVE
//!   (the `e^(−rT)` discount unwinds toward expiry); the "theta ≤ 0 for long
//!   premium" folklore holds unconditionally only at `r = 0` (property-tested).
//! * `vega` — per 1 vol-POINT (per 1% IV): `F·e^(−rT)·φ(d1)·√T / 100` ≥ 0 (plan §2.2).
//! * `rho` — per 1% rate: CE `K·T·e^(−rT)·N(d2)/100`, PE `−K·T·e^(−rT)·N(−d2)/100` —
//!   the plan §2.2 formula verbatim (the BSM-form rho where `F = S·e^{rT}`
//!   co-moves with `r`), NOT the pure-Black-76 `∂/∂r|F = −T·price`.
//!
//! # IV inversion (plan §7 Proposed Decision #5 + §13 Open Decision #7)
//!
//! Primary path: Peter Jäckel's *"Let's Be Rational"* (2015) via the
//! EXACT-pinned pure-Rust [`jaeckel`] crate (`= 0.2.0`, MIT, zero runtime
//! deps — the same crate the deleted kernel used). LBR is a
//! rational-approximation + ≤ 2-Householder-step inversion — bounded by
//! construction, machine-precision in the liquid band.
//!
//! **VERIFIED, never trusted (Golden Rule #1).** An empirical 590k-point
//! domain scan of `jaeckel v0.2.0` (2026-07-02, this session; grid
//! `F = 23_500`, `K ∈ [15k, 33k]`, `T ∈ [2d, 1y]`, `σ ∈ [0.005, 5.0]`, both
//! sides) found the crate **hard-fails (returns a negative sentinel-like
//! value) for total volatility `σ√T ≳ 0.93`** and can return a
//! positive-but-WRONG root for near-underflow prices — so every LBR result is
//! round-trip VERIFIED by repricing (`|reprice − b| ≤ 1e-9·b`); a failed
//! verification falls back to a vendored **deterministic bracketed
//! Newton-bisection** over the [0.001, 5.0] clamp (the plan §2.3 "Rational +
//! bisection fallback" sourcing), iteration-capped at
//! [`FALLBACK_MAX_ITERS`] — monotone + bracketed, so it converges or returns
//! a clean `Err`. Non-convergence / out-of-clamp / no-arb inputs return a
//! clean `Err` — an honest NA upstream, NEVER a fabricated value and NEVER a
//! panic (`panic = "abort"` would SIGABRT the host interpreter).
//!
//! # Error taxonomy (the FFI + wrapper contract)
//!
//! Every `Err(String)` starts with exactly one of two machine-readable prefixes:
//!
//! * [`ERR_INVALID`] (`"invalid_input: …"`) — the CALLER broke the contract
//!   (non-finite / non-positive / subnormal inputs, out-of-band rate or sigma,
//!   neither price nor sigma). Loud: the Python wrapper re-raises.
//! * [`ERR_NO_SOLUTION`] (`"no_solution: …"`) — the inputs are well-formed but
//!   NO trustworthy IV exists (below the 600 s T-floor, price below intrinsic /
//!   above the upper no-arb bound, solve outside the [0.001, 5.0] clamp, LBR
//!   non-convergence). Honest NA: the Python wrapper maps to `None`.
//!
//! # Complexity (honest envelope — Golden Rule #5 / #10)
//!
//! O(1) per evaluation: a fixed count of `exp/ln/sqrt/erfc` calls plus the LBR
//! inversion, itself bounded at ≤ 2 Householder corrections by construction.
//! O(1) space, zero allocation on the success path. This is O(1) PER CALL —
//! not an O(1)-total claim for any batch caller.

use jaeckel::{implied_black_volatility, norm_cdf, norm_pdf};

// DEC-GREEKS-RUST-BATCH-001: the columnar batch face of [`greeks`] — calls
// the SAME frozen kernel per element (zero math duplication); compiled under
// DEFAULT features so the per-PR `cargo test` exercises it.
pub mod batch;

/// Machine-readable prefix for caller-contract violations (wrapper re-raises).
pub const ERR_INVALID: &str = "invalid_input";
/// Machine-readable prefix for honest no-solution outcomes (wrapper maps to NA).
pub const ERR_NO_SOLUTION: &str = "no_solution";

/// Seconds per year under the kernel's Act/365 day-count (matches the theta
/// per-calendar-day convention and PR-1's `DAY_COUNT_BASE = 365`,
/// `DEC-GREEKS-ENGINE-PR1-001`).
pub const SECONDS_PER_YEAR: f64 = 365.0 * 86_400.0;

/// Hard time-to-expiry floor: 600 seconds, expressed in Act/365 years
/// (plan §11 Phase 2 "hard `T`-floor 600 s clean `Err`"; §9 row 4 — below it
/// gamma → ∞ / theta → −∞, so the kernel refuses instead of blowing up).
pub const T_FLOOR_YEARS: f64 = 600.0 / SECONDS_PER_YEAR;

/// Lower IV clamp (plan §11 Phase 2 "IV clamp [0.001, 5.0]"). A solve that
/// lands below it is `Err` — the boundary is never reported as "the IV"
/// (plan §9 row 10).
pub const IV_CLAMP_MIN: f64 = 0.001;
/// Upper IV clamp (500% annualized). Same never-report-the-boundary contract.
pub const IV_CLAMP_MAX: f64 = 5.0;

/// Admission floor on |delta| (plan §11 Phase 2): below it the tuple is
/// flagged NOT [`GreeksOut::admissible`] — a filter for delta-target strike
/// selection, not an error.
pub const DELTA_ADMISSION_FLOOR: f64 = 0.02;
/// Admission floor on the RAW (per unit vol, un-scaled) vega — plan §9 rows
/// 6/8: below it the IV is numerically untrustworthy (vega → 0), so the tuple
/// is flagged NOT admissible.
pub const VEGA_ADMISSION_FLOOR: f64 = 1e-6;

/// Calendar days per year for the theta quote (Act/365 — the recovered
/// kernel's `DEFAULT_DAY_COUNT`).
pub const THETA_DAY_COUNT: f64 = 365.0;

/// Divisor turning per-unit vega / rho into the per-1% broker quote.
const PER_CENT: f64 = 100.0;

/// Relative round-trip tolerance every accepted IV solve must satisfy:
/// `|black(σ_solved) − b| ≤ VERIFY_REL_TOL · b` (undiscounted space). The
/// measured worst residual of a CORRECT `jaeckel v0.2.0` solve over the 590k
/// domain scan sits at ~1e-15 relative in the liquid band; 1e-9 is the loose
/// acceptance that still rejects every observed wrong root by ≥ 20 orders of
/// magnitude.
pub const VERIFY_REL_TOL: f64 = 1e-9;

/// Iteration cap on the deterministic bracketed Newton-bisection fallback.
/// Pure bisection alone needs ≤ 56 halvings to shrink the [0.001, 5.0]
/// bracket below `1e-13·σ` at the smallest clamp value; Newton acceleration
/// only shortens that, so 80 is a proven upper bound with margin — pinned by
/// `fallback_iteration_cap_holds_over_adversarial_grid`.
pub const FALLBACK_MAX_ITERS: u32 = 80;

/// One priced Greeks tuple — the kernel's only output shape.
///
/// `Copy` for zero-allocation FFI; every field is a plain `f64` (full
/// precision, never rounded — `DEC-MONEY-2DP-001` forbids rounding
/// statistical values).
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct GreeksOut {
    /// Implied (or supplied) volatility, annualized decimal (0.18 = 18%).
    pub iv: f64,
    /// Signed delta — CE ∈ \[0, 1\], PE ∈ \[−1, 0\].
    pub delta: f64,
    /// Gamma ≥ 0, identical for CE / PE.
    pub gamma: f64,
    /// Theta per calendar day (Act/365). See the module note on its sign.
    pub theta: f64,
    /// Vega per 1 vol-point (per 1% IV) ≥ 0.
    pub vega: f64,
    /// Rho per 1% rate (plan §2.2 BSM-form; signed CE + / PE −).
    pub rho: f64,
    /// Black-76 model price at [`GreeksOut::iv`] (discounted, same unit as the
    /// inputs — rupees for BruteX). On the solve path it reproduces the input
    /// price to LBR machine precision.
    pub price: f64,
    /// `|delta| ≥ 0.02 AND raw vega ≥ 1e-6` — the plan's admission floors.
    /// `false` = the tuple is numerically well-defined but too far OTM/ITM to
    /// trust for delta-target selection (plan §9 rows 6/8). A FILTER, not an
    /// error.
    pub admissible: bool,
}

/// Reject a non-finite / non-positive / subnormal price-like input.
///
/// Subnormals (< ~2.2·10⁻³⁰⁸) are rejected explicitly: they are positive yet
/// arithmetically degenerate (ratios overflow, logs saturate), and no real
/// forward / strike / premium is remotely near them — a subnormal here is a
/// caller bug, refused loudly (the extreme-case matrix's "subnormal floats"
/// row).
fn check_positive_normal(value: f64, name: &str) -> Result<(), String> {
    if !value.is_finite() || value <= 0.0 || !value.is_normal() {
        return Err(format!(
            "{ERR_INVALID}: {name} must be a positive normal finite float, got {value:e}"
        ));
    }
    Ok(())
}

/// Undiscounted Black price `b(σ)` — the monotone-in-σ function the IV solve
/// inverts (Jäckel's working space).
fn black_undiscounted(sigma: f64, forward: f64, strike: f64, t_years: f64, is_call: bool) -> f64 {
    let sqrt_t = t_years.sqrt();
    let sig_sqrt_t = sigma * sqrt_t;
    // ln(F/K) via ln_1p((F−K)/K) — preserves precision when F ≈ K (ATM), the
    // same trick the recovered kernel used.
    let log_moneyness = ((forward - strike) / strike).ln_1p();
    let d1 = (log_moneyness + 0.5 * sigma * sigma * t_years) / sig_sqrt_t;
    let d2 = d1 - sig_sqrt_t;
    if is_call {
        forward * norm_cdf(d1) - strike * norm_cdf(d2)
    } else {
        strike * norm_cdf(-d2) - forward * norm_cdf(-d1)
    }
}

/// Raw (per unit vol, un-discounted) vega `∂b/∂σ = F·φ(d1)·√T` — the Newton
/// slope of the fallback solver.
fn vega_undiscounted(sigma: f64, forward: f64, strike: f64, t_years: f64) -> f64 {
    let sqrt_t = t_years.sqrt();
    let sig_sqrt_t = sigma * sqrt_t;
    let log_moneyness = ((forward - strike) / strike).ln_1p();
    let d1 = (log_moneyness + 0.5 * sigma * sigma * t_years) / sig_sqrt_t;
    forward * norm_pdf(d1) * sqrt_t
}

/// Black-76 discounted price for a validated `(σ, F, K, T, r, side)` tuple.
///
/// Pure pricing helper shared by both kernel paths and exercised directly by
/// the property tests (put-call parity, strike monotonicity). Inputs are NOT
/// re-validated here — [`greeks`] is the validating entry point.
#[must_use]
pub fn black76_price(
    sigma: f64,
    forward: f64,
    strike: f64,
    t_years: f64,
    rate: f64,
    is_call: bool,
) -> f64 {
    (-rate * t_years).exp() * black_undiscounted(sigma, forward, strike, t_years, is_call)
}

/// Price + all five Greeks from a KNOWN sigma (the shared closed form both
/// paths converge on — ADR-0015 D3).
fn greeks_from_sigma(
    sigma: f64,
    forward: f64,
    strike: f64,
    t_years: f64,
    rate: f64,
    is_call: bool,
) -> GreeksOut {
    let sqrt_t = t_years.sqrt();
    let sig_sqrt_t = sigma * sqrt_t;
    let log_moneyness = ((forward - strike) / strike).ln_1p();
    let d1 = (log_moneyness + 0.5 * sigma * sigma * t_years) / sig_sqrt_t;
    let d2 = d1 - sig_sqrt_t;
    let df = (-rate * t_years).exp();
    let pdf1 = norm_pdf(d1);

    let (price, delta, rho) = if is_call {
        let nd1 = norm_cdf(d1);
        let nd2 = norm_cdf(d2);
        (
            df * (forward * nd1 - strike * nd2),
            df * nd1,
            strike * t_years * df * nd2 / PER_CENT,
        )
    } else {
        let nmd1 = norm_cdf(-d1);
        let nmd2 = norm_cdf(-d2);
        (
            df * (strike * nmd2 - forward * nmd1),
            -df * nmd1,
            -strike * t_years * df * nmd2 / PER_CENT,
        )
    };

    // φ(d1) underflows to exactly 0.0 for |d1| ≳ 38.6 — gamma/vega then read an
    // honest 0.0 (no division hazard: the denominator is bounded away from 0 by
    // the validated σ ≥ IV_CLAMP_MIN and T ≥ T_FLOOR_YEARS).
    let gamma = df * pdf1 / (forward * sig_sqrt_t);
    let vega_raw = forward * df * pdf1 * sqrt_t;
    let theta = (rate * price - df * forward * pdf1 * sigma / (2.0 * sqrt_t)) / THETA_DAY_COUNT;

    GreeksOut {
        iv: sigma,
        delta,
        gamma,
        theta,
        vega: vega_raw / PER_CENT,
        rho,
        price,
        admissible: delta.abs() >= DELTA_ADMISSION_FLOOR && vega_raw >= VEGA_ADMISSION_FLOOR,
    }
}

/// The ONE shared Greeks seam (plan §2.3 / ADR-0015 D3).
///
/// * `sigma = Some(σ)` — live / vendor-IV path: skip the solve, price the
///   Greeks directly off `σ` (any `price` argument is IGNORED — sigma takes
///   precedence, documented).
/// * `sigma = None` — historical path: solve IV from the traded (discounted)
///   option `price` via Jäckel's "Let's Be Rational", then price the Greeks
///   off the SAME closed form.
///
/// # Arguments
///
/// * `price` — traded option premium (discounted, e.g. the stored 1-minute
///   close, rupees). Required when `sigma` is `None`.
/// * `sigma` — known annualized volatility ∈ \[[`IV_CLAMP_MIN`], [`IV_CLAMP_MAX`]\].
/// * `forward` — the underlying forward `F = S·e^((r−q)T)` (the CALLER builds
///   it; `q` is the caller's stamped assumption — plan §7 decision #3).
/// * `strike` — the option strike `K`.
/// * `t_years` — time to expiry in years under the caller's stamped
///   convention; must be ≥ [`T_FLOOR_YEARS`] (600 s) or the result is a clean
///   no-solution `Err`.
/// * `rate` — continuously-compounded annual risk-free rate. BruteX callers
///   obtain it ONLY through the PR-1 `RiskFreeSource` seam
///   (`brutex/options/risk_free.py`, `DEC-GREEKS-ENGINE-PR1-001`).
/// * `is_call` — `true` CE, `false` PE.
///
/// # Errors
///
/// `Err("invalid_input: …")` on a caller-contract violation;
/// `Err("no_solution: …")` when no trustworthy IV exists (honest NA — plan §9).
/// Never panics.
pub fn greeks(
    price: Option<f64>,
    sigma: Option<f64>,
    forward: f64,
    strike: f64,
    t_years: f64,
    rate: f64,
    is_call: bool,
) -> Result<GreeksOut, String> {
    check_positive_normal(forward, "forward")?;
    check_positive_normal(strike, "strike")?;
    if !t_years.is_finite() {
        return Err(format!(
            "{ERR_INVALID}: t_years must be finite, got {t_years:e}"
        ));
    }
    if !rate.is_finite() || rate.abs() > 1.0 {
        return Err(format!(
            "{ERR_INVALID}: rate must be finite with |rate| <= 1.0, got {rate:e}"
        ));
    }
    if t_years < T_FLOOR_YEARS {
        // Plan §9 rows 3/4: a bar at/after expiry or inside the 600 s floor is
        // an HONEST NA, not a caller bug (gamma/theta would blow up).
        return Err(format!(
            "{ERR_NO_SOLUTION}: t_below_floor — t_years {t_years:e} < floor {T_FLOOR_YEARS:e} (600s)"
        ));
    }

    let solved_sigma = match sigma {
        Some(s) => {
            if !s.is_finite() || !(IV_CLAMP_MIN..=IV_CLAMP_MAX).contains(&s) {
                return Err(format!(
                    "{ERR_INVALID}: sigma must lie in [{IV_CLAMP_MIN}, {IV_CLAMP_MAX}], got {s:e}"
                ));
            }
            s
        }
        None => {
            let Some(p) = price else {
                return Err(format!(
                    "{ERR_INVALID}: need price or sigma — both were None"
                ));
            };
            check_positive_normal(p, "price")?;
            solve_iv(p, forward, strike, t_years, rate, is_call)?
        }
    };

    Ok(greeks_from_sigma(
        solved_sigma,
        forward,
        strike,
        t_years,
        rate,
        is_call,
    ))
}

/// Invert the traded price to a VERIFIED implied volatility inside the clamp.
///
/// Works in Jäckel's Black (undiscounted, forward) space: `b = price·e^{rT}`.
/// Fast path = LBR (`jaeckel v0.2.0`); EVERY result — fast or fallback — must
/// pass the round-trip verification `|b(σ) − b| ≤ VERIFY_REL_TOL·b` before it
/// is returned (the measured-scan wrong-root guard; module docs). A rejected
/// fast path falls back to the deterministic bracketed Newton-bisection.
fn solve_iv(
    price: f64,
    forward: f64,
    strike: f64,
    t_years: f64,
    rate: f64,
    is_call: bool,
) -> Result<f64, String> {
    let theta_sign = if is_call { 1.0 } else { -1.0 };
    let undiscounted = price * (rate * t_years).exp();
    let intrinsic = (theta_sign * (forward - strike)).max(0.0);
    if undiscounted < intrinsic {
        // Plan §9 row 5: a no-arbitrage violation (negative time value) has NO
        // implied volatility — honest NA, never a spurious root.
        return Err(format!(
            "{ERR_NO_SOLUTION}: below_intrinsic — undiscounted price {undiscounted:e} < intrinsic {intrinsic:e}"
        ));
    }
    let upper_bound = if is_call { forward } else { strike };
    if undiscounted >= upper_bound {
        // The σ → ∞ limit of the undiscounted Black price is F (call) / K
        // (put); at or above it no finite IV reproduces the price.
        return Err(format!(
            "{ERR_NO_SOLUTION}: above_upper_bound — undiscounted price {undiscounted:e} >= bound {upper_bound:e}"
        ));
    }

    // The clamp maps to a price bracket via monotonicity of b(σ): outside it
    // no in-clamp IV exists and the boundary is NEVER reported as "the IV"
    // (plan §9 row 10).
    let b_lo = black_undiscounted(IV_CLAMP_MIN, forward, strike, t_years, is_call);
    let b_hi = black_undiscounted(IV_CLAMP_MAX, forward, strike, t_years, is_call);
    if undiscounted < b_lo {
        return Err(format!(
            "{ERR_NO_SOLUTION}: iv_below_clamp — price implies sigma < {IV_CLAMP_MIN} (boundary never reported as the IV)"
        ));
    }
    if undiscounted > b_hi {
        return Err(format!(
            "{ERR_NO_SOLUTION}: iv_above_clamp — price implies sigma > {IV_CLAMP_MAX} (boundary never reported as the IV)"
        ));
    }

    let tolerance = VERIFY_REL_TOL * undiscounted;

    // Fast path: LBR — accepted ONLY when in-clamp AND round-trip verified.
    let lbr = implied_black_volatility(undiscounted, forward, strike, t_years, theta_sign);
    if lbr.is_finite()
        && (IV_CLAMP_MIN..=IV_CLAMP_MAX).contains(&lbr)
        && (black_undiscounted(lbr, forward, strike, t_years, is_call) - undiscounted).abs()
            <= tolerance
    {
        return Ok(lbr);
    }

    // Deterministic fallback (plan §2.3 "Rational + bisection fallback").
    let (sigma, _iters) =
        bracketed_solve(undiscounted, forward, strike, t_years, is_call, tolerance)?;
    let residual =
        (black_undiscounted(sigma, forward, strike, t_years, is_call) - undiscounted).abs();
    if residual <= tolerance {
        Ok(sigma)
    } else {
        Err(format!(
            "{ERR_NO_SOLUTION}: iv_solve_failed — best fallback residual {residual:e} > tolerance {tolerance:e}"
        ))
    }
}

/// Deterministic bracketed Newton-bisection over the IV clamp.
///
/// Invariants: `b(σ)` is strictly increasing in σ (raw vega > 0 on the
/// validated domain) and the caller has proven `b(IV_CLAMP_MIN) ≤ b ≤
/// b(IV_CLAMP_MAX)`, so the bracket always contains the root. A Newton
/// proposal is taken only when it lands STRICTLY inside the current bracket;
/// otherwise the step bisects, so the bracket shrinks every iteration and the
/// iteration count is bounded (see [`FALLBACK_MAX_ITERS`]). Returns the
/// solved σ plus the iteration count (pinned by the cap test).
fn bracketed_solve(
    b: f64,
    forward: f64,
    strike: f64,
    t_years: f64,
    is_call: bool,
    tolerance: f64,
) -> Result<(f64, u32), String> {
    let mut lo = IV_CLAMP_MIN;
    let mut hi = IV_CLAMP_MAX;
    let mut sigma = 0.5 * (lo + hi);
    for iteration in 1..=FALLBACK_MAX_ITERS {
        let priced = black_undiscounted(sigma, forward, strike, t_years, is_call);
        let diff = priced - b;
        if diff.abs() <= 0.5 * tolerance {
            return Ok((sigma, iteration));
        }
        if diff > 0.0 {
            hi = sigma;
        } else {
            lo = sigma;
        }
        if hi - lo <= 1e-13 * sigma {
            return Ok((0.5 * (lo + hi), iteration));
        }
        // Newton in LOG-price space: b(σ) is hyper-exponential in σ on the
        // deep-OTM tail (it spans hundreds of orders of magnitude across a
        // tiny σ range), where plain Newton creeps with near-constant steps.
        // g(σ) = ln b(σ) − ln b has slope vega/b(σ), so the step is
        // σ − (ln b(σ) − ln b)·b(σ)/vega — near-exact on the exponential
        // family, plain-Newton-equivalent near ATM. A zero/underflowed b(σ)
        // yields a non-finite proposal → the step bisects (the bracket is the
        // safety net either way).
        let newton = if priced > 0.0 {
            sigma
                - (priced.ln() - b.ln()) * priced
                    / vega_undiscounted(sigma, forward, strike, t_years)
        } else {
            f64::NAN
        };
        sigma = if newton.is_finite() && newton > lo && newton < hi {
            newton
        } else {
            0.5 * (lo + hi)
        };
    }
    Err(format!(
        "{ERR_NO_SOLUTION}: iv_solve_failed — fallback exceeded {FALLBACK_MAX_ITERS} iterations"
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    const F: f64 = 23_500.0;
    const K: f64 = 23_500.0;
    const T: f64 = 30.0 / 365.0;
    const R: f64 = 0.065;

    fn assert_invalid(result: Result<GreeksOut, String>, needle: &str) {
        let err = result.expect_err("expected invalid_input");
        assert!(err.starts_with(ERR_INVALID), "wrong prefix: {err}");
        assert!(err.contains(needle), "missing {needle:?} in {err}");
    }

    fn assert_no_solution(result: Result<GreeksOut, String>, needle: &str) {
        let err = result.expect_err("expected no_solution");
        assert!(err.starts_with(ERR_NO_SOLUTION), "wrong prefix: {err}");
        assert!(err.contains(needle), "missing {needle:?} in {err}");
    }

    // ── seam basics ─────────────────────────────────────────────────────────

    #[test]
    fn sigma_path_prices_atm_call() {
        let out = greeks(None, Some(0.18), F, K, T, R, true).unwrap();
        assert!(out.iv == 0.18);
        assert!(
            out.delta > 0.5 && out.delta < 0.6,
            "ATM call delta ≈ 0.5+, got {}",
            out.delta
        );
        assert!(out.gamma > 0.0 && out.vega > 0.0 && out.price > 0.0);
        assert!(out.theta < 0.0, "ATM long call bleeds, got {}", out.theta);
        assert!(out.rho > 0.0, "call rho positive, got {}", out.rho);
        assert!(out.admissible);
    }

    #[test]
    fn solve_path_round_trips_the_sigma_path_price() {
        let priced = greeks(None, Some(0.22), F, 23_600.0, T, R, false).unwrap();
        let solved = greeks(Some(priced.price), None, F, 23_600.0, T, R, false).unwrap();
        assert!(
            (solved.iv - 0.22).abs() < 1e-12,
            "LBR round-trip drift: {}",
            solved.iv
        );
        assert!((solved.price - priced.price).abs() <= 1e-9 * priced.price);
    }

    #[test]
    fn sigma_takes_precedence_over_price() {
        let with_both = greeks(Some(1.0), Some(0.18), F, K, T, R, true).unwrap();
        let sigma_only = greeks(None, Some(0.18), F, K, T, R, true).unwrap();
        assert_eq!(with_both, sigma_only, "sigma=Some must ignore price");
    }

    #[test]
    fn both_none_is_invalid() {
        assert_invalid(greeks(None, None, F, K, T, R, true), "need price or sigma");
    }

    // ── input validation (loud) ─────────────────────────────────────────────

    #[test]
    fn rejects_zero_negative_nonfinite_forward_and_strike() {
        for bad in [0.0, -1.0, f64::NAN, f64::INFINITY, f64::NEG_INFINITY] {
            assert_invalid(greeks(None, Some(0.2), bad, K, T, R, true), "forward");
            assert_invalid(greeks(None, Some(0.2), F, bad, T, R, true), "strike");
        }
    }

    #[test]
    fn rejects_subnormal_inputs() {
        let sub = f64::MIN_POSITIVE / 2.0;
        assert!(sub > 0.0 && !sub.is_normal(), "test needs a true subnormal");
        assert_invalid(greeks(None, Some(0.2), sub, K, T, R, true), "forward");
        assert_invalid(greeks(None, Some(0.2), F, sub, T, R, true), "strike");
        assert_invalid(greeks(Some(sub), None, F, K, T, R, true), "price");
    }

    #[test]
    fn rejects_bad_price_on_solve_path() {
        for bad in [0.0, -5.0, f64::NAN, f64::INFINITY] {
            assert_invalid(greeks(Some(bad), None, F, K, T, R, true), "price");
        }
    }

    #[test]
    fn rejects_bad_rate_and_nonfinite_t() {
        assert_invalid(greeks(None, Some(0.2), F, K, T, 1.5, true), "rate");
        assert_invalid(greeks(None, Some(0.2), F, K, T, f64::NAN, true), "rate");
        assert_invalid(greeks(None, Some(0.2), F, K, f64::NAN, R, true), "t_years");
        assert_invalid(
            greeks(None, Some(0.2), F, K, f64::INFINITY, R, true),
            "t_years",
        );
    }

    #[test]
    fn rejects_sigma_outside_clamp() {
        for bad in [0.0009, 5.1, 0.0, -0.2, f64::NAN, f64::INFINITY] {
            assert_invalid(greeks(None, Some(bad), F, K, T, R, true), "sigma");
        }
        // The clamp boundaries themselves are VALID sigma inputs.
        assert!(greeks(None, Some(IV_CLAMP_MIN), F, K, T, R, true).is_ok());
        assert!(greeks(None, Some(IV_CLAMP_MAX), F, K, T, R, true).is_ok());
    }

    // ── honest-NA family (plan §9) ──────────────────────────────────────────

    #[test]
    fn t_below_600s_floor_is_no_solution() {
        let just_below = 599.0 / SECONDS_PER_YEAR;
        assert_no_solution(
            greeks(None, Some(0.2), F, K, just_below, R, true),
            "t_below_floor",
        );
        assert_no_solution(greeks(None, Some(0.2), F, K, 0.0, R, true), "t_below_floor");
        assert_no_solution(greeks(None, Some(0.2), F, K, -T, R, true), "t_below_floor");
        // Exactly at the floor is allowed (the expiry-minute boundary).
        assert!(greeks(None, Some(0.2), F, K, T_FLOOR_YEARS, R, true).is_ok());
    }

    #[test]
    fn price_below_intrinsic_is_no_solution() {
        // Deep ITM call: intrinsic (undiscounted) = 1500; discounted price of
        // 1000 sits far below any arbitrage-free value.
        assert_no_solution(
            greeks(Some(1_000.0), None, 25_000.0, K, T, R, true),
            "below_intrinsic",
        );
        // Deep ITM put twin.
        assert_no_solution(
            greeks(Some(1_000.0), None, 22_000.0, K, T, R, false),
            "below_intrinsic",
        );
    }

    #[test]
    fn price_at_or_above_upper_bound_is_no_solution() {
        assert_no_solution(
            greeks(Some(F), None, F, K, T, 0.0, true),
            "above_upper_bound",
        );
        assert_no_solution(
            greeks(Some(K + 1.0), None, F, K, T, 0.0, false),
            "above_upper_bound",
        );
    }

    #[test]
    fn price_outside_sigma_clamp_bracket_is_no_solution() {
        // ATM price generated just BELOW the σ = 0.001 clamp and just ABOVE
        // the σ = 5.0 clamp; the solve must refuse the boundary, never report
        // it (plan §9 row 10).
        let low = black76_price(0.0005, F, K, T, R, true);
        assert!(
            low > 0.0 && low.is_normal(),
            "test premise: representable low price"
        );
        assert_no_solution(greeks(Some(low), None, F, K, T, R, true), "iv_below_clamp");
        let high = black76_price(5.5, F, 24_000.0, T, R, true);
        assert_no_solution(
            greeks(Some(high), None, F, 24_000.0, T, R, true),
            "iv_above_clamp",
        );
    }

    #[test]
    fn itm_price_a_hair_above_intrinsic_solves_to_a_verified_in_clamp_iv() {
        // Deep ITM, time value ~1e-9 on a 1500-rupee intrinsic: the time value
        // sits at the f64 quantization floor of the PRICE itself, so a whole
        // BAND of σ reprices to the identical f64 premium (ill-conditioning of
        // the INPUT, not the solver). The honest contract: the returned IV is
        // in-clamp and REPRICES the observed premium to verification
        // tolerance — never a fabricated out-of-band value.
        let df = (-R * T).exp();
        let price = df * (25_000.0 - K) * (1.0 + 1e-12);
        let out = greeks(Some(price), None, 25_000.0, K, T, R, true).unwrap();
        assert!((IV_CLAMP_MIN..=IV_CLAMP_MAX).contains(&out.iv));
        assert!(
            (out.price - price).abs() <= VERIFY_REL_TOL * price,
            "reprice drift"
        );
    }

    #[test]
    fn lbr_hard_failure_band_recovers_via_fallback() {
        // Measured jaeckel v0.2.0 hard-failure point (domain scan, module
        // docs): F=23500, K=18600, T=30/365, σ=4.97 call → the crate returns
        // a NEGATIVE value. The verified fallback must recover the true root.
        let sigma_true = 4.97;
        let strike = 18_600.0;
        let b = black_undiscounted(sigma_true, F, strike, T, true);
        assert!(
            implied_black_volatility(b, F, strike, T, 1.0) <= 0.0,
            "test premise: this point must hard-fail in jaeckel v0.2.0"
        );
        let price = b * (-R * T).exp();
        let out = greeks(Some(price), None, F, strike, T, R, true).unwrap();
        assert!(
            (out.iv - sigma_true).abs() <= 1e-6 * sigma_true,
            "fallback root drift: {}",
            out.iv
        );
        assert!((out.price - price).abs() <= VERIFY_REL_TOL * price);
    }

    #[test]
    fn lbr_wrong_root_is_rejected_and_fallback_recovers() {
        // Measured jaeckel v0.2.0 silent-wrong-root point (domain scan): a
        // near-underflow deep-OTM price where LBR returns σ ≈ 0.0496 for a
        // true σ = 0.015. The round-trip guard must reject it and the
        // fallback must find the true root.
        let sigma_true = 0.015;
        let strike = 27_600.0;
        let b = black_undiscounted(sigma_true, F, strike, T, true);
        assert!(
            b > 0.0 && b.is_normal(),
            "test premise: representable price"
        );
        let lbr = implied_black_volatility(b, F, strike, T, 1.0);
        assert!(
            (lbr - sigma_true).abs() > 1e-2,
            "test premise: this point must be a wrong root in jaeckel v0.2.0, got {lbr}"
        );
        let out = greeks(Some(b), None, F, strike, T, 0.0, true).unwrap();
        assert!(
            (out.iv - sigma_true).abs() <= 1e-6 * sigma_true,
            "wrong root leaked / fallback drift: {}",
            out.iv
        );
    }

    #[test]
    fn fallback_iteration_cap_holds_over_adversarial_grid() {
        // The pinned iteration-cap gate (plan §12 "a PINNED IV-solve
        // iteration cap"): the deterministic fallback must converge UNDER
        // FALLBACK_MAX_ITERS for every point of an adversarial grid spanning
        // deep-ITM/OTM strikes, the full clamp, and 2d..1y expiries.
        let mut max_iters = 0;
        for &t in &[2.0 / 365.0, T, 1.0] {
            for &strike in &[15_000.0, 21_000.0, F, 26_000.0, 33_000.0] {
                for &sigma in &[0.001, 0.01, 0.2, 1.0, 3.0, 4.99] {
                    for &is_call in &[true, false] {
                        let b = black_undiscounted(sigma, F, strike, t, is_call);
                        let theta_sign = if is_call { 1.0 } else { -1.0 };
                        let intrinsic = (theta_sign * (F - strike)).max(0.0);
                        let bound = if is_call { F } else { strike };
                        if b <= intrinsic || b >= bound || !b.is_normal() {
                            continue; // not a solvable premium at f64 precision
                        }
                        let (solved, iters) =
                            bracketed_solve(b, F, strike, t, is_call, VERIFY_REL_TOL * b)
                                .expect("bracketed fallback must converge on the grid");
                        max_iters = max_iters.max(iters);
                        let residual =
                            (black_undiscounted(solved, F, strike, t, is_call) - b).abs();
                        assert!(residual <= VERIFY_REL_TOL * b, "residual {residual:e}");
                    }
                }
            }
        }
        assert!(max_iters <= FALLBACK_MAX_ITERS, "cap breached: {max_iters}");
        assert!(max_iters > 0, "grid must exercise the solver");
    }

    // ── extreme-case matrix ─────────────────────────────────────────────────

    #[test]
    fn expiry_minute_just_above_floor_is_priced_cleanly() {
        let t = 601.0 / SECONDS_PER_YEAR;
        let out = greeks(None, Some(0.25), F, K, t, R, true).unwrap();
        assert!(out.price > 0.0 && out.price.is_finite());
        assert!(out.gamma.is_finite() && out.theta.is_finite());
    }

    #[test]
    fn deep_itm_call_delta_saturates_toward_discount_factor() {
        let out = greeks(None, Some(0.15), 30_000.0, K, T, R, true).unwrap();
        let df = (-R * T).exp();
        assert!(
            (out.delta - df).abs() < 1e-6,
            "deep ITM Δ → e^(−rT), got {}",
            out.delta
        );
        assert!(!out.admissible || out.vega * 100.0 >= VEGA_ADMISSION_FLOOR);
    }

    #[test]
    fn deep_otm_put_is_flagged_inadmissible() {
        // K far below F → put nearly worthless: |Δ| < 0.02 → filtered.
        let out = greeks(None, Some(0.12), F, 15_000.0, T, R, false).unwrap();
        assert!(out.delta > -DELTA_ADMISSION_FLOOR, "got {}", out.delta);
        assert!(!out.admissible);
        assert!(out.price >= 0.0);
    }

    #[test]
    fn penny_option_solves_to_a_clamped_or_valid_iv() {
        // ₹0.05 (one tick) deep-OTM call, 30d out: either a clean solved IV
        // inside the clamp or an honest no_solution — NEVER a fabricated value
        // or a panic.
        match greeks(Some(0.05), None, F, 26_500.0, T, R, true) {
            Ok(out) => {
                assert!((IV_CLAMP_MIN..=IV_CLAMP_MAX).contains(&out.iv));
                assert!(!out.admissible, "a 1-tick deep-OTM option must be filtered");
            }
            Err(err) => assert!(err.starts_with(ERR_NO_SOLUTION), "{err}"),
        }
    }

    #[test]
    fn exact_atm_call_matches_undiscounted_normalized_form() {
        // ATM (F == K): call = df·F·(2N(σ√T/2) − 1) exactly.
        let sigma = 0.2;
        let out = greeks(None, Some(sigma), F, K, T, R, true).unwrap();
        let df = (-R * T).exp();
        let expected = df * F * (2.0 * norm_cdf(0.5 * sigma * T.sqrt()) - 1.0);
        assert!(
            (out.price - expected).abs() < 1e-9 * expected,
            "{} vs {expected}",
            out.price
        );
    }

    #[test]
    fn put_call_parity_holds() {
        let call = greeks(None, Some(0.3), F, 23_000.0, T, R, true).unwrap();
        let put = greeks(None, Some(0.3), F, 23_000.0, T, R, false).unwrap();
        let df = (-R * T).exp();
        let parity = call.price - put.price - df * (F - 23_000.0);
        assert!(parity.abs() < 1e-9, "PCP residual {parity}");
        // Gamma and vega are side-independent.
        assert!((call.gamma - put.gamma).abs() < 1e-15);
        assert!((call.vega - put.vega).abs() < 1e-12);
    }

    #[test]
    fn zero_rate_theta_is_never_positive() {
        for (strike, is_call) in [(23_000.0, true), (23_500.0, true), (24_000.0, false)] {
            let out = greeks(None, Some(0.2), F, strike, T, 0.0, is_call).unwrap();
            assert!(out.theta <= 0.0, "r=0 theta must be ≤ 0, got {}", out.theta);
        }
    }

    #[test]
    fn black76_price_matches_the_kernel_price_bit_for_bit() {
        // Mutation teeth: `black76_price` is a separate expression path from
        // `greeks_from_sigma`'s internal price — any mutation of either
        // (e.g. `df * b` -> `df + b`, or a dropped discount sign) breaks this
        // bit-for-bit cross-check at r > 0.
        for is_call in [true, false] {
            for &(strike, sigma) in &[(23_000.0, 0.14), (K, 0.2), (24_000.0, 0.35)] {
                let out = greeks(None, Some(sigma), F, strike, T, R, is_call).unwrap();
                let direct = black76_price(sigma, F, strike, T, R, is_call);
                assert_eq!(
                    direct.to_bits(),
                    out.price.to_bits(),
                    "black76_price diverged from the kernel price: {direct} vs {}",
                    out.price
                );
            }
        }
    }

    #[test]
    fn vega_undiscounted_matches_central_finite_difference() {
        // Mutation teeth: pins the Newton slope itself (a zeroed / negated /
        // mis-composed vega would otherwise be masked by the bisection
        // safety net and only show up as slower convergence).
        let h = 1e-6;
        for &(strike, sigma) in &[(23_000.0, 0.14), (K, 0.2), (26_000.0, 0.8)] {
            let up = black_undiscounted(sigma + h, F, strike, T, true);
            let down = black_undiscounted(sigma - h, F, strike, T, true);
            let finite_difference = (up - down) / (2.0 * h);
            let analytic = vega_undiscounted(sigma, F, strike, T);
            assert!(
                (analytic - finite_difference).abs() <= 1e-4 * analytic.abs().max(1.0),
                "vega slope drift: analytic {analytic} vs FD {finite_difference}"
            );
        }
    }

    #[test]
    fn fallback_newton_acceleration_stays_fast_on_the_deep_otm_tail() {
        // Mutation teeth + performance pin: log-Newton converges in ~14
        // iterations on the measured deep-OTM point; a broken slope degrades
        // the loop to bisection-only (~50+ iterations), so a tight iteration
        // budget catches slope mutations the bracket would otherwise mask.
        let strike = 27_600.0;
        let sigma_true = 0.015;
        let b = black_undiscounted(sigma_true, F, strike, T, true);
        let (sigma, iters) =
            bracketed_solve(b, F, strike, T, true, VERIFY_REL_TOL * b).expect("must converge");
        assert!(
            (sigma - sigma_true).abs() <= 1e-6 * sigma_true,
            "root drift: {sigma}"
        );
        assert!(iters <= 25, "Newton acceleration lost: {iters} iterations");
    }

    #[test]
    fn error_strings_carry_exactly_one_known_prefix() {
        let invalid = greeks(None, None, F, K, T, R, true).unwrap_err();
        let na = greeks(None, Some(0.2), F, K, 0.0, R, true).unwrap_err();
        assert!(invalid.starts_with("invalid_input: "));
        assert!(na.starts_with("no_solution: "));
    }
}
