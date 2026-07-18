//! The pull-progress DERIVED-METRICS core — pure, clockless (Wave 2F).
//!
//! Laws (`brutex/dashboard/pull_progress.py` — DEC-PULL-PROGRESS-HONESTY-001,
//! the `ProgressSnapshot` derived properties), transcribed token-for-token:
//!
//! * [`percent`] — `min(100.0, round(completed / total * 100.0, 1))`, `0.0`
//!   when `total == 0`. The `round(x, 1)` is CPython's dtoa/strtod path —
//!   HALF-EVEN on the EXACT decimal value of the binary float — reproduced
//!   bit-exactly by [`round1`] (exact integer arithmetic; the Wave-2F spike
//!   proved bit-parity over 224k hostile cases + 200k percent-chain pairs).
//! * [`elapsed`] — `updated_at - started_at` clamped through `if e > 0.0`
//!   (a NaN elapsed compares `false` and clamps to `0.0`, exactly as the
//!   Python `elapsed if elapsed > 0.0 else 0.0` does).
//! * [`units_per_sec`] — the PHASE-baseline rate law: `0.0` unless
//!   `progressed > 0` over a strictly positive span; a NaN span falls
//!   through the `span <= 0.0` refusal (IEEE `false`) and yields a NaN rate,
//!   exactly as the oracle does (pinned by the frozen golden's NaN rows).
//! * [`eta_seconds`] — the honest-`None` law: `None` when not running /
//!   open-ended / no known total / no measured positive rate; `0.0` when
//!   nothing remains; else the linear extrapolation `remaining / rate`.
//!
//! ## Count domain (the `2**53` ceiling)
//!
//! CPython's `int / int` true division is CORRECTLY ROUNDED on the exact
//! rational; IEEE f64 division equals it ONLY when both operands convert to
//! f64 exactly — guaranteed for counts `<= 2**53`. The kernel therefore
//! REFUSES counts beyond [`COUNT_DOMAIN_MAX`] (the facade's eligibility
//! gate never sends them; the refusal is defense-in-depth), and every
//! int→f64 conversion inside the domain is EXACT, so all four metrics are
//! bit-identical to the oracle by construction.
//!
//! Clockless: `updated_at` / `started_at` / `phase_started_at` are the
//! CALLER's stamped monotonic instants (the injected-clock discipline —
//! a kernel that read any clock would break every frozen-clock test).

/// The inclusive count-domain ceiling: `2**53` (both division operands
/// exactly representable in f64 — see the module doc).
pub const COUNT_DOMAIN_MAX: u64 = 1 << 53;

/// The count-domain refusal (defense-in-depth behind the facade gate).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct CountDomainError;

impl core::fmt::Display for CountDomainError {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        write!(
            f,
            "count outside the pull-progress kernel domain [0, 2**53] \
             (the facade degrades such snapshots to the Python oracle)"
        )
    }
}

/// CPython `round(x, 1)` for finite non-negative `x < 100.0` — bit-exact.
///
/// CPython's `round(float, ndigits)` is `_Py_dg_dtoa(x, mode 3, 1)` +
/// `_Py_dg_strtod`: HALF-EVEN rounding of the EXACT decimal value of the
/// binary float at one decimal place, then a correctly-rounded parse back.
/// Equivalently: `n = round_half_even_exact(10 * x)` over the exact rational
/// `10·x`, then the correctly-rounded f64 of `n / 10`. For `x < 100.0` the
/// integer `n <= 1000`, so `n as f64 / 10.0` has exact operands and IEEE
/// division IS the correctly-rounded parse — no big-number path needed.
///
/// Callers guard the domain (the percent path shortcuts `x >= 100.0` to the
/// clamp before calling); debug-asserted here.
fn round1_sub100(x: f64) -> f64 {
    debug_assert!(x.is_finite() && (0.0..100.0).contains(&x));
    let bits = x.to_bits();
    let exp_field = ((bits >> 52) & 0x7ff) as i64;
    let frac = bits & ((1u64 << 52) - 1);
    let (m, e): (u64, i64) = if exp_field == 0 {
        (frac, -1074) // subnormal (or zero)
    } else {
        (frac | (1u64 << 52), exp_field - 1075)
    };
    // x = m * 2^e exactly.
    if e >= 0 {
        // x is an exact integer: 10*x is an integer, n/10 == x.
        return x;
    }
    let v: u64 = 10 * m; // <= 10 * 2^53 < 2^57
    let s = -e; // 1..=1074
    let n: u64 = if s >= 58 {
        // v < 2^57 <= 2^(s-1): the fractional value is under one half — n = 0.
        // (Ties are impossible: v < 2^(s-1) strictly.)
        0
    } else {
        let q = v >> s;
        let r = v - (q << s);
        let half = 1u64 << (s - 1);
        if r > half || (r == half && (q & 1) == 1) {
            q + 1
        } else {
            q
        }
    };
    // n = round_half_even_exact(10*x) <= 1000: exact operands, correctly
    // rounded IEEE division == CPython's strtod of "n/10".
    debug_assert!(n <= 1000);
    n as f64 / 10.0
}

/// `ProgressSnapshot.percent` — `min(100.0, round(c/t*100, 1))`, 0 when `t == 0`.
pub fn percent(total: u64, completed: u64) -> Result<f64, CountDomainError> {
    if total > COUNT_DOMAIN_MAX || completed > COUNT_DOMAIN_MAX {
        return Err(CountDomainError);
    }
    if total == 0 {
        // The oracle's `total <= 0` arm (u64: only 0 reaches it here; the
        // facade routes negative Python ints to the oracle at the gate).
        return Ok(0.0);
    }
    // Both operands exact in f64 → IEEE division == CPython int/int true
    // division; the ×100.0 is the same single IEEE product on both sides.
    let x = (completed as f64 / total as f64) * 100.0;
    if x >= 100.0 {
        // round(x, 1) can never bring a value >= 100.0 below 100.0 (its
        // floor-to-tenth is >= 100.0), so the min-clamp absorbs it — and the
        // exact-integer path stays in small-integer territory (spike-proven).
        return Ok(100.0);
    }
    let r = round1_sub100(x);
    // round can round UP to exactly 100.0 (x = 99.96); Python's min keeps 100.0.
    Ok(if r > 100.0 { 100.0 } else { r })
}

/// `ProgressSnapshot.elapsed_seconds` — the run-anchor span clamped `> 0.0`.
pub fn elapsed(updated_at: f64, started_at: f64) -> f64 {
    let e = updated_at - started_at;
    if e > 0.0 { e } else { 0.0 }
}

/// `ProgressSnapshot.units_per_sec` — the PHASE-scoped throughput law.
pub fn units_per_sec(
    total_domain_probe: u64,
    completed: u64,
    phase_base_completed: u64,
    updated_at: f64,
    phase_started_at: f64,
) -> Result<f64, CountDomainError> {
    if total_domain_probe > COUNT_DOMAIN_MAX
        || completed > COUNT_DOMAIN_MAX
        || phase_base_completed > COUNT_DOMAIN_MAX
    {
        return Err(CountDomainError);
    }
    // Python: progressed = completed - phase_base_completed (exact int sub).
    let progressed: i64 = completed as i64 - phase_base_completed as i64;
    let span = updated_at - phase_started_at;
    // `span <= 0.0` is IEEE-false for a NaN span — the oracle falls through
    // and serves progressed/NaN = NaN; mirrored exactly.
    if span <= 0.0 || progressed <= 0 {
        return Ok(0.0);
    }
    // progressed <= 2^53 → exact i64→f64 conversion (CPython converts the
    // int the same way before the C double division).
    Ok(progressed as f64 / span)
}

/// `ProgressSnapshot.eta_seconds` — `(present, value)`; `(false, 0.0)` is the
/// honest `None` (Python folds it back to `None` at the facade).
#[allow(clippy::too_many_arguments)] // 1:1 FFI mirror of the snapshot's derived inputs
pub fn eta_seconds(
    state_running: bool,
    open_ended: bool,
    total: u64,
    completed: u64,
    phase_base_completed: u64,
    updated_at: f64,
    phase_started_at: f64,
) -> Result<(bool, f64), CountDomainError> {
    if total > COUNT_DOMAIN_MAX || completed > COUNT_DOMAIN_MAX {
        return Err(CountDomainError);
    }
    if !state_running || open_ended || total == 0 {
        return Ok((false, 0.0));
    }
    let rate = units_per_sec(
        total,
        completed,
        phase_base_completed,
        updated_at,
        phase_started_at,
    )?;
    // `rate <= 0.0` is IEEE-false for a NaN rate — the oracle falls through
    // to remaining/NaN (NaN) exactly like this arm does.
    if rate <= 0.0 {
        return Ok((false, 0.0));
    }
    let remaining: i64 = total as i64 - completed as i64;
    if remaining <= 0 {
        return Ok((true, 0.0));
    }
    Ok((true, remaining as f64 / rate))
}

/// The one-call derived-metrics bundle the facade consumes:
/// `(percent, elapsed_seconds, units_per_sec, eta_present, eta_seconds)`.
///
/// `started_at` feeds ONLY `elapsed_seconds`; the rate/eta pair anchors on
/// the PHASE baseline (`phase_started_at` / `phase_base_completed`) — the
/// DEC-PULL-PROGRESS-HONESTY-001 split, preserved verbatim.
#[allow(clippy::too_many_arguments)] // 1:1 FFI mirror of the snapshot's derived inputs
pub fn progress_derived(
    state_running: bool,
    open_ended: bool,
    total: u64,
    completed: u64,
    phase_base_completed: u64,
    updated_at: f64,
    started_at: f64,
    phase_started_at: f64,
) -> Result<(f64, f64, f64, bool, f64), CountDomainError> {
    let pct = percent(total, completed)?;
    let ups = units_per_sec(
        total,
        completed,
        phase_base_completed,
        updated_at,
        phase_started_at,
    )?;
    let (eta_present, eta) = eta_seconds(
        state_running,
        open_ended,
        total,
        completed,
        phase_base_completed,
        updated_at,
        phase_started_at,
    )?;
    Ok((pct, elapsed(updated_at, started_at), ups, eta_present, eta))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pct(total: u64, completed: u64) -> f64 {
        percent(total, completed).expect("in-domain")
    }

    // --- round1 hostile suite (the Wave-2F spike, frozen as cargo teeth) ---

    #[test]
    fn round1_exact_ties_are_half_even() {
        // The ONLY exact ties at the tenths position are odd multiples of
        // 1/4 (10·x half-integer ⇔ x = odd/4 — spike-derived).
        assert_eq!(round1_sub100(0.25).to_bits(), 0.2f64.to_bits()); // down to even
        assert_eq!(round1_sub100(0.75).to_bits(), 0.8f64.to_bits()); // up to even
        assert_eq!(round1_sub100(6.25).to_bits(), 6.2f64.to_bits());
        assert_eq!(round1_sub100(18.75).to_bits(), 18.8f64.to_bits());
        assert_eq!(round1_sub100(99.75).to_bits(), 99.8f64.to_bits());
    }

    #[test]
    fn round1_repr_traps_round_on_the_exact_binary_value() {
        // 0.35 is stored as 0.34999999999999997… → 0.3 (NOT a tie).
        assert_eq!(round1_sub100(0.35).to_bits(), 0.3f64.to_bits());
        // 0.05 is stored slightly ABOVE 1/20 → rounds up to 0.1.
        assert_eq!(round1_sub100(0.05).to_bits(), 0.1f64.to_bits());
        // 2.675 is stored below → 2.7? No: at ndigits=1 it is 2.675 → 2.7
        // by ordinary nearest (6.75 tie is at ndigits=2, not here).
        assert_eq!(round1_sub100(2.675).to_bits(), 2.7f64.to_bits());
        // Neighbours of a tie resolve by ordinary nearest.
        assert_eq!(
            round1_sub100(f64::from_bits(0.25f64.to_bits() - 1)).to_bits(),
            0.2f64.to_bits()
        );
        assert_eq!(
            round1_sub100(f64::from_bits(0.25f64.to_bits() + 1)).to_bits(),
            0.3f64.to_bits()
        );
    }

    #[test]
    fn round1_subnormals_and_tiny_round_to_zero() {
        assert_eq!(round1_sub100(f64::from_bits(1)).to_bits(), 0.0f64.to_bits()); // 5e-324
        assert_eq!(round1_sub100(1e-310).to_bits(), 0.0f64.to_bits());
        assert_eq!(round1_sub100(0.0).to_bits(), 0.0f64.to_bits());
        assert_eq!(round1_sub100(0.04999).to_bits(), 0.0f64.to_bits());
    }

    #[test]
    fn round1_integers_pass_through() {
        for i in 0..100u64 {
            let x = i as f64;
            assert_eq!(round1_sub100(x).to_bits(), x.to_bits());
        }
    }

    #[test]
    fn round1_exact_one_decimal_values_pass_through() {
        // 12.5 = exact binary; one decimal digit suffices → unchanged.
        assert_eq!(round1_sub100(12.5).to_bits(), 12.5f64.to_bits());
        assert_eq!(round1_sub100(99.9).to_bits(), 99.9f64.to_bits());
    }

    // --- percent chain ---

    #[test]
    fn percent_zero_total_is_zero() {
        assert_eq!(pct(0, 0).to_bits(), 0.0f64.to_bits());
        assert_eq!(pct(0, 5).to_bits(), 0.0f64.to_bits());
    }

    #[test]
    fn percent_ties_on_the_sixteenth_grid() {
        assert_eq!(pct(16, 1).to_bits(), 6.2f64.to_bits()); // 6.25 → even DOWN
        assert_eq!(pct(16, 3).to_bits(), 18.8f64.to_bits()); // 18.75 → even UP
    }

    #[test]
    fn percent_clamps_and_boundary() {
        assert_eq!(pct(4, 9).to_bits(), 100.0f64.to_bits()); // completed > total
        assert_eq!(pct(2500, 2499).to_bits(), 100.0f64.to_bits()); // 99.96 → 100.0
        assert_eq!(pct(1000, 999).to_bits(), 99.9f64.to_bits());
        assert_eq!(pct(1, 1).to_bits(), 100.0f64.to_bits());
    }

    #[test]
    fn percent_huge_counts_at_the_ceiling() {
        assert_eq!(
            pct(COUNT_DOMAIN_MAX, COUNT_DOMAIN_MAX).to_bits(),
            100.0f64.to_bits()
        );
        // (2^53 - 1) / 2^53 * 100 = 99.99999999999999 → round → 100.0.
        assert_eq!(
            pct(COUNT_DOMAIN_MAX, COUNT_DOMAIN_MAX - 1).to_bits(),
            100.0f64.to_bits()
        );
    }

    #[test]
    fn percent_refuses_out_of_domain() {
        assert_eq!(percent(COUNT_DOMAIN_MAX + 1, 0), Err(CountDomainError));
        assert_eq!(percent(1, COUNT_DOMAIN_MAX + 1), Err(CountDomainError));
    }

    // --- elapsed / rate / eta ---

    #[test]
    fn elapsed_clamps_negative_and_nan() {
        assert_eq!(elapsed(1056.0, 1024.0).to_bits(), 32.0f64.to_bits());
        assert_eq!(elapsed(1000.0, 1024.0).to_bits(), 0.0f64.to_bits());
        assert_eq!(elapsed(f64::NAN, 1024.0).to_bits(), 0.0f64.to_bits());
        assert!(elapsed(f64::INFINITY, 1024.0).is_infinite());
    }

    #[test]
    fn rate_baseline_law() {
        // progressed 2 over 32 s = 0.0625.
        let r = units_per_sec(16, 6, 4, 1056.0, 1024.0).unwrap();
        assert_eq!(r.to_bits(), 0.0625f64.to_bits());
        // Baseline ahead of completed → 0.0; zero/negative span → 0.0.
        assert_eq!(units_per_sec(10, 2, 5, 1056.0, 1024.0).unwrap(), 0.0);
        assert_eq!(units_per_sec(16, 6, 4, 1024.0, 1024.0).unwrap(), 0.0);
        assert_eq!(units_per_sec(16, 6, 4, 1000.0, 1024.0).unwrap(), 0.0);
        // NaN span falls through the IEEE-false refusal → NaN rate.
        assert!(units_per_sec(16, 6, 4, f64::NAN, 1024.0).unwrap().is_nan());
    }

    #[test]
    fn eta_none_arms_and_values() {
        // Not running / open-ended / zero total → absent.
        assert_eq!(
            eta_seconds(false, false, 16, 6, 4, 1056.0, 1024.0).unwrap(),
            (false, 0.0)
        );
        assert_eq!(
            eta_seconds(true, true, 16, 6, 4, 1056.0, 1024.0).unwrap(),
            (false, 0.0)
        );
        assert_eq!(
            eta_seconds(true, false, 0, 6, 4, 1056.0, 1024.0).unwrap(),
            (false, 0.0)
        );
        // No measured rate → absent.
        assert_eq!(
            eta_seconds(true, false, 16, 6, 6, 1056.0, 1024.0).unwrap(),
            (false, 0.0)
        );
        // remaining 10 at 0.0625 u/s = 160 s.
        let (present, eta) = eta_seconds(true, false, 16, 6, 4, 1056.0, 1024.0).unwrap();
        assert!(present);
        assert_eq!(eta.to_bits(), 160.0f64.to_bits());
        // Nothing remaining (completed > total pun) → present 0.0.
        assert_eq!(
            eta_seconds(true, false, 4, 9, 0, 1056.0, 1024.0).unwrap(),
            (true, 0.0)
        );
    }

    #[test]
    fn bundle_composes_the_four_metrics() {
        let (p, el, ups, present, eta) =
            progress_derived(true, false, 16, 6, 4, 1056.0, 1000.0, 1024.0).unwrap();
        assert_eq!(p.to_bits(), 37.5f64.to_bits()); // 6/16 = 37.5 exact
        assert_eq!(el.to_bits(), 56.0f64.to_bits());
        assert_eq!(ups.to_bits(), 0.0625f64.to_bits());
        assert!(present);
        assert_eq!(eta.to_bits(), 160.0f64.to_bits());
    }
}
