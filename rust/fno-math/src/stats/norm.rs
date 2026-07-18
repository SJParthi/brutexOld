//! Shared standard-normal numerics for the slice-6 statistics kernels.
//!
//! DEC-METRICS-RUST-STATS-002 (slice 6, PR-2 — the stats-tails continuation).
//! Two primitives the DSR kernels ([`super::deflated_sharpe`]) share:
//!
//! * [`std_norm_cdf`] — the operation-for-operation transcription of
//!   CPython's `statistics.NormalDist(0.0, 1.0).cdf` (`statistics.py`:
//!   `0.5 * (1.0 + erf((x - mu) / (sigma * _SQRT2)))`), with `erf` bound to
//!   the **system libm `erf` symbol** — see the module-level law below.
//! * [`std_norm_inv_cdf_checked`] — the operation-for-operation
//!   transcription of CPython's `_normal_dist_inv_cdf` (Wichura AS241
//!   rational approximation; `statistics.py` lines 1155-1224 on CPython
//!   3.12, the same coefficients + Horner order the `_statistics` C
//!   accelerator compiles). Coefficients are copied VERBATIM from the
//!   CPython source (PSF license permits transcription with attribution;
//!   Wichura 1988, "Algorithm AS241"). `Err` mirrors the Python raise
//!   domain (`p <= 0.0 or p >= 1.0` → `StatisticsError`); the seam maps an
//!   `Err` to a refusal so the ORACLE raises the identical exception.
//!
//! ## The `erf` law (the ONE sanctioned libm FFI call)
//!
//! The slice-6 scope law said "Python keeps every libm call". This PR
//! extends it with ONE precisely-scoped exception, PROVEN necessary by
//! measurement (DEC-METRICS-RUST-STATS-002): CPython's `math.erf` is the
//! **platform libm's `erf`** (`HAVE_ERF` — verified bit-for-bit against
//! `ctypes` libm over a 3.5M-point corpus on the CI platform), and glibc
//! ≥ 2.28's `erf` is NOT the classic fdlibm algorithm (an fdlibm
//! transcription was measured 1-ULP off on ~1.1% of a 3.5M-point hostile
//! corpus — transcription provably CANNOT be bit-exact on this platform).
//! The only bit-exact-by-construction route is calling the SAME dynamic
//! symbol CPython calls: `extern "C" fn erf` resolved from the process's
//! libm at load time — both engines then execute literally the same
//! machine code, on every platform where CPython's `math.erf` is the
//! system erf (Linux/glibc, macOS/libSystem, Windows/ucrt). The Python
//! seam's differential/property suites re-prove the equality per platform
//! and the burn-in autorevert latch is the runtime fence for any exotic
//! platform where it does not hold. `log`/`exp`/`sqrt` already share this
//! property implicitly: Rust's `f64::ln`/`exp` lower to the same libm
//! symbols CPython's `math.log`/`exp` call, and `sqrt` is a correctly
//! rounded IEEE-754 instruction on both sides.

/// The ONE sanctioned `unsafe` item in this crate's stats tree: the C99
/// `erf` declaration (§7.12.8.1), resolved from the process libm — the SAME
/// function CPython's `math.erf` calls (see the module docstring law).
/// Scoped-allow with a SAFETY argument, the `rust/brutex-lake/src/locks.rs`
/// precedent (DEC-LAKE-RUST-LOCKS-ATOMIC-001) for a documented, narrow FFI
/// exception under the crate-level `#![deny(unsafe_code)]`.
#[allow(unsafe_code)]
mod cmath {
    // SAFETY: `erf` is a total, pure C function over one f64 (C99
    // §7.12.8.1): no pointers, no preconditions, no memory access, defined
    // for every input bit pattern (NaN/±inf included). Declaring it `safe`
    // cannot violate memory safety for any argument. (It may set errno /
    // FP status flags — process-benign, observed by nothing here.)
    unsafe extern "C" {
        pub(super) safe fn erf(x: f64) -> f64;
    }
}

/// The system libm `erf` — bit-identical to CPython's `math.erf` by
/// construction (same dynamic symbol; module docstring law).
#[inline]
pub(crate) fn erf(x: f64) -> f64 {
    cmath::erf(x)
}

/// `statistics.NormalDist(0.0, 1.0).cdf(x)` transcription:
/// `0.5 * (1.0 + erf((x - mu) / (sigma * _SQRT2)))` with `mu = 0.0`,
/// `sigma = 1.0`, `_SQRT2 = sqrt(2.0)` (Rust's `SQRT_2` is the same
/// correctly-rounded double — bit-pinned by the in-file test). The
/// `(x - 0.0)` / `(1.0 * SQRT_2)` shapes are kept verbatim so every IEEE
/// operation matches the Python source one-for-one.
#[inline]
pub(crate) fn std_norm_cdf(x: f64) -> f64 {
    0.5 * (1.0 + erf((x - 0.0) / (1.0 * std::f64::consts::SQRT_2)))
}

/// `statistics._normal_dist_inv_cdf(p, 0.0, 1.0)` transcription (Wichura
/// AS241) with the Python raise domain as the `Err` arm.
///
/// Mirrors `NormalDist.inv_cdf`'s guard EXACTLY: `p <= 0.0 or p >= 1.0` is
/// the error (`StatisticsError` on the Python side — the seam refuses on
/// `Err` so the oracle raises it verbatim). A NaN `p` passes the guard on
/// BOTH sides (`NaN <= 0.0` and `NaN >= 1.0` are both false) and
/// propagates to a NaN result through identical IEEE arithmetic — reached
/// by no production caller (the DSR quantile arguments are always real),
/// covered for totality parity.
pub(crate) fn std_norm_inv_cdf_checked(p: f64) -> Result<f64, String> {
    if p <= 0.0 || p >= 1.0 {
        // The byte-identical *message* lives Python-side (the oracle's
        // StatisticsError) — this string is seam-internal only.
        return Err(format!(
            "std_norm_inv_cdf: p must be in the range 0.0 < p < 1.0, got {p}"
        ));
    }
    let q = p - 0.5;
    if q.abs() <= 0.425 {
        let r = 0.180625 - q * q;
        // Hash sum: 55.88319_28806_14901_4439 (Wichura AS241; coefficients
        // verbatim from CPython statistics.py — underscores preserved).
        let num = (((((((2.50908_09287_30122_6727e+3 * r + 3.34305_75583_58812_8105e+4) * r
            + 6.72657_70927_00870_0853e+4)
            * r
            + 4.59219_53931_54987_1457e+4)
            * r
            + 1.37316_93765_50946_1125e+4)
            * r
            + 1.97159_09503_06551_4427e+3)
            * r
            + 1.33141_66789_17843_7745e+2)
            * r
            + 3.38713_28727_96366_6080e+0)
            * q;
        let den = ((((((5.22649_52788_52854_5610e+3 * r + 2.87290_85735_72194_2674e+4) * r
            + 3.93078_95800_09271_0610e+4)
            * r
            + 2.12137_94301_58659_5867e+4)
            * r
            + 5.39419_60214_24751_1077e+3)
            * r
            + 6.87187_00749_20579_0830e+2)
            * r
            + 4.23133_30701_60091_1252e+1)
            * r
            + 1.0;
        let x = num / den;
        return Ok(0.0 + (x * 1.0));
    }
    let mut r = if q <= 0.0 { p } else { 1.0 - p };
    r = (-r.ln()).sqrt();
    let num: f64;
    let den: f64;
    if r <= 5.0 {
        r -= 1.6;
        // Hash sum: 49.33206_50330_16102_89036
        num = ((((((7.74545_01427_83414_07640e-4 * r + 2.27238_44989_26918_45833e-2) * r
            + 2.41780_72517_74506_11770e-1)
            * r
            + 1.27045_82524_52368_38258e+0)
            * r
            + 3.64784_83247_63204_60504e+0)
            * r
            + 5.76949_72214_60691_40550e+0)
            * r
            + 4.63033_78461_56545_29590e+0)
            * r
            + 1.42343_71107_49683_57734e+0;
        den = ((((((1.05075_00716_44416_84324e-9 * r + 5.47593_80849_95344_94600e-4) * r
            + 1.51986_66563_61645_71966e-2)
            * r
            + 1.48103_97642_74800_74590e-1)
            * r
            + 6.89767_33498_51000_04550e-1)
            * r
            + 1.67638_48301_83803_84940e+0)
            * r
            + 2.05319_16266_37758_82187e+0)
            * r
            + 1.0;
    } else {
        r -= 5.0;
        // Hash sum: 47.52583_31754_92896_71629
        num = ((((((2.01033_43992_92288_13265e-7 * r + 2.71155_55687_43487_57815e-5) * r
            + 1.24266_09473_88078_43860e-3)
            * r
            + 2.65321_89526_57612_30930e-2)
            * r
            + 2.96560_57182_85048_91230e-1)
            * r
            + 1.78482_65399_17291_33580e+0)
            * r
            + 5.46378_49111_64114_36990e+0)
            * r
            + 6.65790_46435_01103_77720e+0;
        den = ((((((2.04426_31033_89939_78564e-15 * r + 1.42151_17583_16445_88870e-7) * r
            + 1.84631_83175_10054_68180e-5)
            * r
            + 7.86869_13114_56132_59100e-4)
            * r
            + 1.48753_61290_85061_48525e-2)
            * r
            + 1.36929_88092_27358_05310e-1)
            * r
            + 5.99832_20655_58879_37690e-1)
            * r
            + 1.0;
    }
    let mut x = num / den;
    if q < 0.0 {
        x = -x;
    }
    Ok(0.0 + (x * 1.0))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn bits(x: f64) -> u64 {
        x.to_bits()
    }

    #[test]
    fn sqrt2_const_is_the_correctly_rounded_sqrt() {
        // Python's statistics._SQRT2 is sqrt(2.0) — the same correctly
        // rounded double as Rust's SQRT_2 (bit pin: a drift here would
        // silently shift every cdf argument).
        assert_eq!(bits(std::f64::consts::SQRT_2), bits(2.0_f64.sqrt()));
    }

    #[test]
    fn erf_special_values() {
        // erf(0) = 0 (sign preserved), erf(±inf) = ±1, erf(nan) is nan —
        // the C99 contract of the bound symbol.
        assert_eq!(bits(erf(0.0)), bits(0.0));
        assert_eq!(erf(f64::INFINITY), 1.0);
        assert_eq!(erf(f64::NEG_INFINITY), -1.0);
        assert!(erf(f64::NAN).is_nan());
        // Odd symmetry at a representative point.
        assert_eq!(bits(erf(-0.5)), bits(-erf(0.5)));
    }

    #[test]
    fn inv_cdf_known_value_bit_pins() {
        // Bit-pins captured from CPython 3.12 `NormalDist().inv_cdf` (the C
        // _statistics accelerator) on the CI platform — one per AS241
        // branch. A 1-ULP transcription drift fails these exactly.
        // inv_cdf(0.5) = 0.0 (central branch, q = 0).
        assert_eq!(
            std_norm_inv_cdf_checked(0.5).map(bits),
            Ok(bits(0.0)),
            "central"
        );
        // inv_cdf(0.975) — central branch (|q| <= 0.425).
        assert_eq!(
            std_norm_inv_cdf_checked(0.975).map(bits),
            Ok(bits(1.9599639845400536_f64)),
            "central 0.975"
        );
        // inv_cdf(0.999) — middle tail branch (r <= 5.0).
        assert_eq!(
            std_norm_inv_cdf_checked(0.999).map(bits),
            Ok(bits(3.090232306167813_f64)),
            "tail r<=5"
        );
        // inv_cdf(1e-20) — far tail branch (r > 5.0), negative side.
        assert_eq!(
            std_norm_inv_cdf_checked(1e-20).map(bits),
            Ok(bits(-9.262340089798405_f64)),
            "far tail"
        );
    }

    #[test]
    fn inv_cdf_err_on_python_raise_domain() {
        // The exact Python guard: p <= 0.0 or p >= 1.0 raises.
        assert!(std_norm_inv_cdf_checked(0.0).is_err());
        assert!(std_norm_inv_cdf_checked(-0.5).is_err());
        assert!(std_norm_inv_cdf_checked(1.0).is_err());
        assert!(std_norm_inv_cdf_checked(1.5).is_err());
        // The smallest positive subnormal is INSIDE the domain (far tail).
        assert!(std_norm_inv_cdf_checked(f64::from_bits(1)).is_ok());
        // NaN passes the guard on both sides and propagates (totality
        // parity with the Python source — no production caller reaches it).
        let nan = std_norm_inv_cdf_checked(f64::NAN);
        assert!(nan.is_ok_and(f64::is_nan));
    }

    #[test]
    fn cdf_shape_pins() {
        // cdf(0) = 0.5 exactly (erf(0) = 0); symmetry cdf(-x) = 1 - cdf(x)
        // holds to the bit at x where erf is exact.
        assert_eq!(bits(std_norm_cdf(0.0)), bits(0.5));
        assert_eq!(std_norm_cdf(f64::INFINITY), 1.0);
        assert_eq!(std_norm_cdf(f64::NEG_INFINITY), 0.0);
    }
}
