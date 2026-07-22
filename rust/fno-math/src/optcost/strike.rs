//! Strike-grid kernels (DEC-OPTCOST-TAILS-RUST-001).
//!
//! Rust twins of the pure strike math in `brutex/options/strike_grid.py`
//! ([`optcost_round_to_atm`], [`optcost_strike_offset_from_atm`],
//! [`optcost_grid_around_atm`]) and
//! `brutex/options/strike_rules.py::resolve_offset_strike`
//! ([`optcost_offset_strike`]).
//!
//! The per-underlying `STRIKE_STEP` table (`strike_grid.py:26-30`) stays
//! Python `Final` data — a 3-entry dict lookup has NO computation to
//! kernel; Python resolves the step and passes it. The `pub const` mirror
//! below is a cargo-test fixture. The non-OFFSET-rule ValueError
//! (`strike_rules.py:180-181`) is decided Python-side BEFORE the FFI (the
//! check reads `rule.kind`); `int(rule.param)` truncation is Python-side
//! too — the kernel takes the resulting int.
//!
//! ## `round_to_atm` — the CPython float-semantics port (proof sketch)
//!
//! `strike_grid.py:60` computes `int((spot_price + step / 2) // step) * step`
//! over Python floats. Every operation is IEEE-deterministic, so an
//! operation-for-operation transcription is bit-exact:
//!
//! * `step / 2` — Python `int.__truediv__` returns the correctly-rounded
//!   f64 of the RATIONAL step/2; for `float(step)` exact (|step| <= 2^53,
//!   guarded) this equals Rust `(step as f64) / 2.0` (division by a power
//!   of two is exact scaling).
//! * `spot_price + half` — one IEEE add, identical in both languages.
//! * `//` — CPython `float_floor_div` → `float_divmod`
//!   (`Objects/floatobject.c`): `fmod` (exact by IEEE definition), one
//!   subtract, one divide, the sign fix-up, `floor`, and the
//!   `div - floordiv > 0.5` snap — transcribed line-for-line in
//!   [`cpython_float_floor_div`]. Rust `%` on f64 IS C `fmod`; `floor` /
//!   `copysign` are exact.
//! * `int(...)` — truncation of an integral-valued f64; exact for
//!   |value| < 2^63 (guarded; past the guard Python computes an arbitrary-
//!   precision int the kernel cannot represent → `Err` → the Python oracle
//!   serves the call).
//! * `* step` — integer multiply, `checked_mul` (overflow → oracle).
//!
//! A non-finite `spot_price` (or a sum that overflows to inf) makes the
//! Python chain raise (`int(nan)` / `int(inf)`) — the kernel `Err`s and the
//! oracle raises exactly as today. The Python-stage seam re-proves the port
//! with a hypothesis full-float differential; the cargo pins below hardcode
//! oracle-derived literals (bit-exact spots via `f64::from_bits`).

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

// ── Cargo-test fixture mirror of the Python STRIKE_STEP table ────────────
//
// AUDIT: derived from `brutex/options/strike_grid.py:26-30` (NIFTY 50 ·
// BANKNIFTY 100 · SENSEX 100; verified via derive_oracle.py 2026-07-18).
// Test fixture only — Python data stays the ONE runtime source of truth.

/// `STRIKE_STEP` — (underlying, step rupees): NIFTY 50 · BANKNIFTY 100 ·
/// SENSEX 100.
pub const STRIKE_STEP: &[(&str, i64)] = &[("NIFTY", 50), ("BANKNIFTY", 100), ("SENSEX", 100)];

/// Pure core of [`optcost_offset_strike`] — the
/// `strike_rules.py:182-187` direction law: BULL adds
/// `step_count * strike_step`, BEAR inverts the sign ("plus" always means
/// further OTM in the trade direction).
pub fn offset_strike_checked(
    atm_strike: i64,
    step_count: i64,
    strike_step: i64,
    is_bull: bool,
) -> Result<i64, String> {
    let delta = step_count.checked_mul(strike_step).ok_or_else(|| {
        "optcost_offset_strike: step_count * strike_step overflows i64".to_string()
    })?;
    let signed = if is_bull {
        delta
    } else {
        // BEAR inverts the sign (strike_rules.py:187); i64::MIN has no i64
        // negation → Err (→ the Python oracle, whose ints never overflow).
        delta
            .checked_neg()
            .ok_or_else(|| "optcost_offset_strike: BEAR sign inversion overflows i64".to_string())?
    };
    atm_strike
        .checked_add(signed)
        .ok_or_else(|| "optcost_offset_strike: resolved strike overflows i64".to_string())
}

/// Resolve a strike from an OFFSET-kind rule — the
/// `brutex/options/strike_rules.py::resolve_offset_strike` law (the
/// non-OFFSET-rule ValueError is decided Python-side pre-FFI).
///
/// Raises `ValueError` on i64 overflow (Python's arbitrary-precision int
/// handles those inputs — the seam degrades to the oracle).
#[pyfunction]
pub fn optcost_offset_strike(
    atm_strike: i64,
    step_count: i64,
    strike_step: i64,
    is_bull: bool,
) -> PyResult<i64> {
    offset_strike_checked(atm_strike, step_count, strike_step, is_bull)
        .map_err(PyValueError::new_err)
}

/// Compute the strike at `offset_steps` grid steps from ATM — the
/// `brutex/options/strike_grid.py::strike_offset_from_atm` law
/// (`strike_grid.py:83`: `atm_strike + offset_steps * step`; no
/// bounds-check by contract).
///
/// Raises `ValueError` on i64 overflow (→ the Python oracle).
#[pyfunction]
pub fn optcost_strike_offset_from_atm(
    atm_strike: i64,
    offset_steps: i64,
    step: i64,
) -> PyResult<i64> {
    let delta = offset_steps.checked_mul(step).ok_or_else(|| {
        PyValueError::new_err("optcost_strike_offset_from_atm: offset_steps * step overflows i64")
    })?;
    atm_strike.checked_add(delta).ok_or_else(|| {
        PyValueError::new_err("optcost_strike_offset_from_atm: resolved strike overflows i64")
    })
}

/// Guard against a hostile `half_width` allocating an absurd Vec — the
/// Python oracle handles such calls (slowly) with identical output, so the
/// kernel refuses anything past a generous production ceiling (default
/// half_width is 10; `strike_grid.py:89`).
const MAX_HALF_WIDTH: i64 = 1_000_000;

/// Pure core of [`optcost_grid_around_atm`] — the
/// `strike_grid.py:101-102` chain: `[atm + i*step for i in
/// range(-half_width, half_width + 1)]` (a negative half_width yields an
/// empty range, mirrored).
pub fn grid_around_atm_checked(
    atm_strike: i64,
    step: i64,
    half_width: i64,
) -> Result<Vec<i64>, String> {
    if half_width < 0 {
        // Python range(-h, h+1) with h < 0 is empty.
        return Ok(Vec::new());
    }
    if half_width > MAX_HALF_WIDTH {
        return Err(format!(
            "optcost_grid_around_atm: half_width {half_width} past the \
             {MAX_HALF_WIDTH} kernel ceiling — the Python oracle serves this call"
        ));
    }
    let mut grid = Vec::with_capacity((2 * half_width + 1) as usize);
    for i in -half_width..=half_width {
        let delta = i
            .checked_mul(step)
            .ok_or_else(|| "optcost_grid_around_atm: i * step overflows i64".to_string())?;
        let strike = atm_strike
            .checked_add(delta)
            .ok_or_else(|| "optcost_grid_around_atm: strike overflows i64".to_string())?;
        grid.push(strike);
    }
    Ok(grid)
}

/// The `half_width`-strike chain centered on ATM — the
/// `brutex/options/strike_grid.py::grid_around_atm` law.
///
/// Raises `ValueError` on i64 overflow or an absurd width (→ the Python
/// oracle). FFI cost (disclosed): one `O(2·half_width + 1)` Vec→list copy
/// per call — 21 entries at the production default.
#[pyfunction]
pub fn optcost_grid_around_atm(atm_strike: i64, step: i64, half_width: i64) -> PyResult<Vec<i64>> {
    grid_around_atm_checked(atm_strike, step, half_width).map_err(PyValueError::new_err)
}

/// CPython `float_floor_div` (via `float_divmod`,
/// `Objects/floatobject.c`) transcribed operation-for-operation for FINITE
/// `vx` and positive finite `wx`. Every step is an IEEE-deterministic
/// operation (fmod / sub / div / floor / copysign), so the result is
/// bit-identical to Python's `vx // wx`.
fn cpython_float_floor_div(vx: f64, wx: f64) -> f64 {
    debug_assert!(vx.is_finite() && wx.is_finite() && wx > 0.0);
    // mod = fmod(vx, wx) — exact by IEEE definition (Rust f64 `%` is fmod).
    let md = vx % wx;
    // div = (vx - mod) / wx
    let mut div = (vx - md) / wx;
    // Ensure the remainder has the same sign as the denominator (CPython
    // also does `mod += wx` here; `mod` does not feed the floor-div result,
    // so the assignment — and the zero-case `copysign` snap of `mod` — are
    // elided; the `div -= 1.0` companion is what matters).
    if md != 0.0 && ((wx < 0.0) != (md < 0.0)) {
        // wx > 0 here, so this fires iff md < 0.
        div -= 1.0;
    }
    if div != 0.0 {
        // Snap quotient to nearest integral value.
        let mut floordiv = div.floor();
        if div - floordiv > 0.5 {
            floordiv += 1.0;
        }
        floordiv
    } else {
        // div is zero — return a zero with the sign of the true quotient.
        0.0f64.copysign(vx / wx)
    }
}

/// The exact-f64-conversion ceiling for `step` (float(step) exact iff
/// |step| <= 2^53; the shipped steps are 50/100).
const MAX_EXACT_STEP: i64 = 1i64 << 53;

/// Pure core of [`optcost_round_to_atm`] — the `strike_grid.py:56-60`
/// chain (see the module docs for the operation-for-operation proof).
pub fn round_to_atm_checked(spot_price: f64, step: i64) -> Result<i64, String> {
    if step <= 0 {
        return Err(format!(
            "optcost_round_to_atm: step must be positive (got {step}) — the \
             STRIKE_STEP table is resolved Python-side"
        ));
    }
    if step > MAX_EXACT_STEP {
        return Err(format!(
            "optcost_round_to_atm: step {step} past the 2^53 float-exactness \
             envelope — the Python oracle serves this call"
        ));
    }
    if !spot_price.is_finite() {
        // Python: int(nan) raises ValueError, int(inf) raises OverflowError
        // — the oracle serves (and raises for) this call.
        return Err(format!(
            "optcost_round_to_atm: non-finite spot_price {spot_price} — the \
             Python oracle serves this call"
        ));
    }
    let wx = step as f64; // exact (guarded)
    let half = wx / 2.0; // exact power-of-two scaling == Python step / 2
    let vx = spot_price + half; // one IEEE add, identical
    if !vx.is_finite() {
        // spot + half overflowed to inf → Python's chain raises at int().
        return Err(
            "optcost_round_to_atm: spot_price + step/2 overflows f64 — the \
             Python oracle serves this call"
                .to_string(),
        );
    }
    let floordiv = cpython_float_floor_div(vx, wx);
    // int(floordiv): truncation of an integral-valued float — exact iff it
    // fits i64. i64::MAX as f64 == 2^63 exactly; any integral f64 strictly
    // below it fits (Python's arbitrary-precision int serves the rest via
    // the oracle).
    if !(floordiv >= i64::MIN as f64 && floordiv < i64::MAX as f64) {
        return Err(format!(
            "optcost_round_to_atm: quotient {floordiv} outside i64 — the \
             Python oracle serves this call"
        ));
    }
    let q = floordiv as i64; // exact: integral-valued and in range
    q.checked_mul(step).ok_or_else(|| {
        "optcost_round_to_atm: strike overflows i64 — the Python oracle serves this call"
            .to_string()
    })
}

/// Round a float rupee spot to the nearest strike on the grid (= ATM) —
/// the `brutex/options/strike_grid.py::round_to_atm` half-up grid rounding,
/// CPython float semantics replicated operation-for-operation.
///
/// FFI note (the ONE sanctioned f64 crossing — see the `optcost` module
/// docs): `spot_price` is the Python function's OWN float-rupee input
/// domain; PyO3 passes the C double through bit-unchanged and no money
/// AMOUNT is computed in floats. Raises `ValueError` when the input is
/// outside the kernel's provable envelope (non-finite, out-of-i64,
/// step past 2^53) — the seam degrades to the Python oracle, which
/// answers (or raises) exactly as today.
#[pyfunction]
pub fn optcost_round_to_atm(spot_price: f64, step: i64) -> PyResult<i64> {
    round_to_atm_checked(spot_price, step).map_err(PyValueError::new_err)
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;

    // ── Python-oracle pins (derive_oracle.py 2026-07-18) ─────────────────

    /// Every `round_to_atm` pin below hardcodes the Python oracle's answer,
    /// with the spot given as EXACT bits (`f64::from_bits`) where the
    /// decimal literal could mislead.
    #[test]
    fn round_to_atm_pins_match_python_oracle() {
        // (spot_bits, step, expected)
        let pins: &[(u64, i64, i64)] = &[
            (0x40d7730000000000, 50, 24_000),                // 24012.0 → 24000
            (0x40d7764000000000, 50, 24_050),                // 24025.0 half-tie UP
            (0x40e8736000000000, 100, 50_100),               // 50075.0 → 50100
            (0x40d7763fffffffff, 50, 24_000),                // 24024.999999999996 → 24000
            (0x40d863c000000000, 50, 25_000),                // 24975.0 → 25000
            (0x0000000000000000, 50, 0),                     // 0.0 → 0
            (0xc0d7764000000000, 50, -24_000),               // -24025.0 → -24000
            (0xc0d7730000000000, 50, -24_000),               // -24012.0 → -24000
            (0x430c6bf5263400c8, 50, 1_000_000_000_000_050), // 1e15+25 → ...050
            (0x0000000000000001, 50, 0),                     // 5e-324 subnormal → 0
            (0x40f3d61fffffffff, 100, 81_200),               // 81249.99999999999 → 81200
            (0x40f3d62000000000, 100, 81_300),               // 81250.0 half-tie UP
        ];
        for &(bits, step, want) in pins {
            let spot = f64::from_bits(bits);
            assert_eq!(
                round_to_atm_checked(spot, step),
                Ok(want),
                "round_to_atm({spot:?} [0x{bits:016x}], {step})"
            );
        }
    }

    #[test]
    fn round_to_atm_err_arms_degrade_to_oracle() {
        assert!(round_to_atm_checked(f64::NAN, 50).is_err());
        assert!(round_to_atm_checked(f64::INFINITY, 50).is_err());
        assert!(round_to_atm_checked(f64::NEG_INFINITY, 50).is_err());
        assert!(round_to_atm_checked(1.0, 0).is_err());
        assert!(round_to_atm_checked(1.0, -50).is_err());
        assert!(round_to_atm_checked(1.0, MAX_EXACT_STEP + 1).is_err());
        // Quotient past i64 → oracle (Python returns a big int).
        assert!(round_to_atm_checked(1.5e300, 50).is_err());
        // f64::MAX + half stays finite; the quotient is what explodes.
        assert!(round_to_atm_checked(f64::MAX, 50).is_err());
    }

    #[test]
    fn strike_step_table_mirror_pins_python_source() {
        // strike_grid.py:26-30 — NIFTY 50 · BANKNIFTY 100 · SENSEX 100.
        assert_eq!(
            STRIKE_STEP,
            &[("NIFTY", 50), ("BANKNIFTY", 100), ("SENSEX", 100)]
        );
    }

    #[test]
    fn offset_strike_pins_match_python_oracle() {
        // resolve_offset_strike pins (derive_oracle.py 2026-07-18):
        // atm/BULL/24000/50 → 24000 (param 0)
        assert_eq!(offset_strike_checked(24_000, 0, 50, true), Ok(24_000));
        // atm_plus_2/BULL → 24100 · atm_plus_2/BEAR → 23900
        assert_eq!(offset_strike_checked(24_000, 2, 50, true), Ok(24_100));
        assert_eq!(offset_strike_checked(24_000, 2, 50, false), Ok(23_900));
        // atm_minus_5/BULL/50000/100 → 49500 · BEAR → 50500
        assert_eq!(offset_strike_checked(50_000, -5, 100, true), Ok(49_500));
        assert_eq!(offset_strike_checked(50_000, -5, 100, false), Ok(50_500));
        // Overflow → Err (→ oracle).
        assert!(offset_strike_checked(i64::MAX, 1, 50, true).is_err());
        assert!(offset_strike_checked(0, i64::MAX, 2, true).is_err());
        // BEAR negation of delta: i64::MIN delta would overflow on negate —
        // step_count*strike_step = i64::MIN only from MIN*1.
        assert!(offset_strike_checked(0, i64::MIN, 1, false).is_err());
    }

    #[test]
    fn grid_around_atm_pins_match_python_oracle() {
        // grid_around_atm(24000, NIFTY, 2) == [23900, 23950, 24000, 24050, 24100]
        assert_eq!(
            grid_around_atm_checked(24_000, 50, 2),
            Ok(vec![23_900, 23_950, 24_000, 24_050, 24_100])
        );
        // Default half_width=10 → 21 strikes, sorted, ATM centered.
        let grid = grid_around_atm_checked(24_000, 50, 10)
            .unwrap_or_else(|e| panic!("unexpected Err: {e}"));
        assert_eq!(grid.len(), 21);
        assert_eq!(grid[0], 23_500);
        assert_eq!(grid[10], 24_000);
        assert_eq!(grid[20], 24_500);
        // Negative half_width → empty (Python range(-h, h+1) empty).
        assert_eq!(grid_around_atm_checked(24_000, 50, -1), Ok(vec![]));
        // Absurd width / overflow → Err (→ oracle).
        assert!(grid_around_atm_checked(24_000, 50, MAX_HALF_WIDTH + 1).is_err());
        assert!(grid_around_atm_checked(i64::MAX, i64::MAX, 1).is_err());
    }

    #[test]
    fn strike_offset_overflow_errs() {
        assert!(
            offset_strike_checked(i64::MAX - 1, 1, 2, true).is_err(),
            "checked_add arm"
        );
    }

    // ── properties ────────────────────────────────────────────────────

    /// Exact integer half-up reference for integral spots: Python's
    /// `(spot + step/2) // step * step` over EXACT values equals
    /// `floor((2·spot + step) / (2·step)) · step` — valid when every float
    /// op is exact (integral spot, |spot| <= 2^52, step even <= 2^52).
    fn exact_int_half_up(spot: i64, step: i64) -> i64 {
        let n = 2 * i128::from(spot) + i128::from(step);
        let d = 2 * i128::from(step);
        (n.div_euclid(d) * i128::from(step)) as i64
    }

    proptest! {
        /// Differential vs the exact-integer half-up law over the exact
        /// float domain (integral spots): the float chain must land on the
        /// same grid strike, half-ties UP.
        #[test]
        fn round_to_atm_matches_exact_integer_reference(
            spot in -(1i64 << 50)..=(1i64 << 50),
            step in prop_oneof![Just(50i64), Just(100i64), 2i64..=10_000],
        ) {
            // Keep every float op exact: even steps only for the tie law.
            let step = if step % 2 == 1 { step + 1 } else { step };
            let got = round_to_atm_checked(spot as f64, step)
                .unwrap_or_else(|e| panic!("unexpected Err: {e}"));
            prop_assert_eq!(got, exact_int_half_up(spot, step));
        }

        /// Structural laws over arbitrary finite spots: divisibility by the
        /// step, determinism, and monotonicity in the spot.
        #[test]
        fn round_to_atm_structural_laws(
            a in proptest::num::f64::NORMAL | proptest::num::f64::SUBNORMAL
                | proptest::num::f64::ZERO,
            b in proptest::num::f64::NORMAL | proptest::num::f64::SUBNORMAL
                | proptest::num::f64::ZERO,
            step in prop_oneof![Just(50i64), Just(100i64), 1i64..=100_000],
        ) {
            let lo = a.min(b);
            let hi = a.max(b);
            let r_lo = round_to_atm_checked(lo, step);
            let r_hi = round_to_atm_checked(hi, step);
            if let (Ok(x), Ok(y)) = (r_lo, r_hi) {
                prop_assert_eq!(x % step, 0);
                prop_assert_eq!(y % step, 0);
                prop_assert!(x <= y, "monotone: {} -> {}, {} -> {}", lo, x, hi, y);
                // Determinism.
                prop_assert_eq!(round_to_atm_checked(lo, step), Ok(x));
            }
        }
    }
}
