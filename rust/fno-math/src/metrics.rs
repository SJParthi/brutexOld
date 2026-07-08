//! Sweep-metrics kernel — the fused per-mask equity fold + drawdown scan.
//!
//! DEC-SWEEP-RUST-METRICS-001 (Phase B of the Rust-migration blueprint;
//! MIGRATION.md #1186). The Rust port of the measured-hot part of the sweep's
//! per-mask trades→metrics tail: `brutex.engine.sweep_driver.build_equity_curve`'s
//! running-sum equity fold FUSED with `brutex.metrics.compute._drawdown_stats`'s
//! per-bar drawdown scan (max-drawdown paisa / %, drawdown duration, Ulcer
//! Index). Every OTHER metric (moments, Sharpe/Sortino, CAGR, percentiles …)
//! deliberately STAYS in Python — see the bit-exactness section for why.
//!
//! ## Bit-exactness (Golden Rule #4) — the f64-exactness envelope
//!
//! The Python oracle folds equity in ARBITRARY-PRECISION ints and computes
//! `dd_pct = drop / peak` with CPython's `long_true_divide` — a CORRECTLY
//! ROUNDED division of the exact rational. A naive `(drop as f64)/(peak as
//! f64)` double-rounds whenever either operand exceeds 2⁵³. This kernel is
//! therefore only ever handed inputs inside the **exactness envelope**
//! [`MAX_EXACT_EQUITY_PAISA`] = 2⁵²:
//!
//! * The Python wrapper PRE-GUARDS `start_equity_paisa + Σ|pnl| ≤ 2⁵²`
//!   (exact int arithmetic) and routes anything larger to the Python oracle.
//! * Under that bound every equity value `e` satisfies `|e| ≤ 2⁵²`, hence
//!   `peak ≤ 2⁵²` and `drop = peak − e ≤ 2⁵³` — BOTH convert to f64 EXACTLY,
//!   so IEEE-754 division of the exact operands is correctly rounded and
//!   equals CPython's exact-rational division bit-for-bit.
//! * `sqrt` is IEEE-754 correctly rounded on both sides (CPython libm /
//!   Rust `sqrtsd`); `× 100.0` is a single rounding on both sides; the
//!   `sum_sq_pct` accumulation is a strictly SEQUENTIAL left-to-right f64
//!   fold on both sides (NO Kahan/Neumaier, NO rayon reduction, NO
//!   `mul_add` — an explicit FMA would fuse where CPython rounds twice).
//! * All interior i64 arithmetic is bounded by `2⁵³ ≪ i64::MAX`, so the
//!   fold is provably overflow-free once the boundary validation passes
//!   (the [`crate::trade_sim::MAX_PRICE_PAISA`] idiom: validate at the FFI
//!   edge, keep the hot loop unguarded with the proof carried here).
//!
//! What STAYS in Python (MUST-STAY-PYTHON — not correctly-rounded-portable):
//! `math.pow(m2_n, 1.5)` (skewness) and `growth ** (1.0/span)` (CAGR, plus
//! its OverflowError→inf clamp) are libm `pow`, which carries NO
//! correct-rounding guarantee, so porting them could diverge in the last
//! ulp; the Welford/Pébay moment recurrence and the O(trades) aggregates are
//! not the measured bottleneck. `math.fsum` is absent from this entire path.
//!
//! ## Failure mode — `Result`, NEVER a panic
//!
//! Under the crate's `overflow-checks = true` + `panic = "abort"` release
//! profile an unchecked overflow (or an out-of-bounds index) would SIGABRT
//! the whole host interpreter. Every boundary violation here returns a pure
//! `Result<_, String>` mapped to a clean Python `ValueError` — and the
//! Python wrapper treats ANY kernel error as "route this mask to the Python
//! oracle", never a raise the pure-Python arm would not have produced.
//! Honest envelope (Golden Rule #10): the per-call `vec![0i64; n_bars]`
//! scatter buffer allocation cannot be guarded — a genuine allocation
//! failure aborts (the same envelope as `ResidentSweepSim` construction);
//! at 8 bytes/bar a multi-year 1-minute window (~500k bars) is ~4 MB.
//!
//! ## Complexity (honest envelope — NOT O(1) total)
//!
//! One call is `O(N_bars + N_exits)` compute + one `O(N_bars)` scratch
//! allocation. The per-tuple sweep tail stays `O(kept · N_bars)` — this
//! kernel is a CONSTANT-FACTOR win only; claiming otherwise is FORBIDDEN.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

/// f64-exactness ceiling (2⁵²) for `start_equity_paisa + Σ|pnl|`.
///
/// See the module docstring: this bound makes every equity value, the running
/// peak AND the peak-to-trough drop exactly representable as f64, so the
/// `drop/peak` division is correctly rounded on exact operands — bit-identical
/// to CPython's exact-rational `int / int`. The Python wrapper mirrors this
/// constant (`brutex.engine.rust_metrics.MAX_EXACT_EQUITY_PAISA`).
pub const MAX_EXACT_EQUITY_PAISA: i64 = 1 << 52;

/// Pure core of [`sweep_drawdown_stats`] — `Result<_, String>` (NO `PyErr`) so
/// the no-libpython `cargo test` link exercises every branch (the
/// [`crate::python::add_u64`] split). Validates the FFI boundary with
/// `checked_*` arithmetic, then runs the provably-overflow-free fused fold.
///
/// Mirrors `brutex.metrics.compute._drawdown_stats` over the equity curve
/// `equity[i] = start_equity_paisa + Σ_{(idx,pnl): idx ≤ i} pnl` — the exact
/// curve `brutex.engine.sweep_driver.build_equity_curve` materialises —
/// WITHOUT materialising it. Returns
/// `(max_dd_paisa, max_dd_pct·100, max_dd_duration_bars, ulcer_index·100)`.
pub fn drawdown_stats_checked(
    n_bars: usize,
    start_equity_paisa: i64,
    exits: &[(usize, i64)],
) -> Result<(i64, f64, i64, f64), String> {
    if n_bars == 0 {
        return Err("sweep_drawdown_stats: n_bars must be >= 1".to_string());
    }
    if start_equity_paisa <= 0 {
        return Err(format!(
            "sweep_drawdown_stats: start_equity_paisa must be > 0, got {start_equity_paisa}"
        ));
    }
    // Exactness-envelope validation (checked_* — the inputs are arbitrary i64
    // from Python, so the VALIDATION itself must be overflow-safe).
    let mut magnitude: i64 = start_equity_paisa;
    for &(idx, pnl) in exits {
        if idx >= n_bars {
            return Err(format!(
                "sweep_drawdown_stats: exit index {idx} out of range for {n_bars} bars"
            ));
        }
        let abs_pnl = pnl.checked_abs().ok_or_else(|| {
            format!("sweep_drawdown_stats: |pnl {pnl}| overflows the exactness envelope")
        })?;
        magnitude = magnitude.checked_add(abs_pnl).ok_or_else(|| {
            "sweep_drawdown_stats: Σ|pnl| overflows the exactness envelope".to_string()
        })?;
        if magnitude > MAX_EXACT_EQUITY_PAISA {
            return Err(format!(
                "sweep_drawdown_stats: start + Σ|pnl| ({magnitude}) exceeds the \
                 f64-exactness envelope [1, {MAX_EXACT_EQUITY_PAISA}] paisa"
            ));
        }
    }

    // Scatter the per-exit P&L onto its bar (integer addition — order-free;
    // partial sums are bounded by Σ|pnl| ≤ 2⁵², so the += cannot overflow).
    let mut realized = vec![0i64; n_bars];
    for &(idx, pnl) in exits {
        realized[idx] += pnl;
    }

    // Fused equity fold + drawdown scan — mirrors `_drawdown_stats` line by
    // line. All i64 arithmetic below is provably in ±2⁵³ (module docstring);
    // `peak` starts at i64::MIN and is overwritten on the FIRST bar
    // (`value > i64::MIN` always under the envelope), which is identical to
    // Python's `peak = equity[0]` initialisation.
    let mut running: i64 = start_equity_paisa;
    let mut peak: i64 = i64::MIN;
    let mut max_dd_paisa: i64 = 0;
    let mut max_dd_pct: f64 = 0.0;
    let mut sum_sq_pct: f64 = 0.0;
    let mut current_dd_bars: i64 = 0;
    let mut max_dd_bars: i64 = 0;
    // The four running-max folds below use `.max()` instead of `if new > acc`
    // — behavior-identical BY CONSTRUCTION for the i64 folds (a tie assigns
    // the same value) and for the f64 pct fold (`dd_pct` is guarded finite,
    // never NaN, by the `peak > 0` gate + bounded operands; `drop ≥ 0` casts
    // to +0.0 so signed-zero ties are bit-identical too). This mirrors the
    // Python oracle's own `max()` shape (compute.py) and removes the
    // tie-equivalent `>`→`>=` cargo-mutants class structurally
    // (DEC-MUTATION-RUNTIME-003; proven bitwise by the two-pass-oracle
    // proptest below + the Python↔Rust byte-parity suites).
    for &delta in &realized {
        running += delta;
        let value = running;
        peak = peak.max(value);
        let drop = peak - value;
        if drop > 0 {
            current_dd_bars += 1;
        } else {
            current_dd_bars = 0;
        }
        max_dd_bars = max_dd_bars.max(current_dd_bars);
        max_dd_paisa = max_dd_paisa.max(drop);
        if peak > 0 {
            // Both operands ≤ 2⁵³ ⇒ exact f64s ⇒ the IEEE division is the
            // correctly rounded quotient — CPython's `drop / peak` result.
            let dd_pct = (drop as f64) / (peak as f64);
            max_dd_pct = max_dd_pct.max(dd_pct);
            // Sequential left-to-right fold; plain mul then add (NO mul_add —
            // a fused multiply-add would round once where CPython rounds twice).
            sum_sq_pct += dd_pct * dd_pct;
        }
    }
    // n_bars < 2⁵³ in any real window ⇒ the usize→f64 conversion is exact.
    let ulcer = (sum_sq_pct / (n_bars as f64)).sqrt();
    Ok((max_dd_paisa, max_dd_pct * 100.0, max_dd_bars, ulcer * 100.0))
}

/// Fused per-mask equity fold + drawdown scan over `(exit_bar_idx, pnl_paisa)`
/// pairs — the Phase-B sweep-metrics kernel (see the module docstring).
///
/// Returns `(max_dd_paisa, max_dd_pct, max_dd_duration_bars, ulcer_index)`
/// exactly as `brutex.metrics.compute._drawdown_stats` returns them (the two
/// percentages already ×100). Raises `ValueError` on any input outside the
/// validated envelope; the Python wrapper routes that mask to the bit-exact
/// Python oracle instead of surfacing the error.
#[pyfunction]
pub fn sweep_drawdown_stats(
    n_bars: usize,
    start_equity_paisa: i64,
    exits: Vec<(usize, i64)>,
) -> PyResult<(i64, f64, i64, f64)> {
    drawdown_stats_checked(n_bars, start_equity_paisa, &exits).map_err(PyValueError::new_err)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Naive two-pass oracle: materialise the equity curve, then scan it —
    /// the shape of the Python `build_equity_curve` + `_drawdown_stats`
    /// pair. The fused kernel must match it bit-for-bit.
    fn two_pass_oracle(n_bars: usize, start: i64, exits: &[(usize, i64)]) -> (i64, f64, i64, f64) {
        let mut realized = vec![0i64; n_bars];
        for &(idx, pnl) in exits {
            realized[idx] += pnl;
        }
        let mut equity = Vec::with_capacity(n_bars);
        let mut running = start;
        for delta in realized {
            running += delta;
            equity.push(running);
        }
        let mut peak = equity[0];
        let mut max_dd_paisa: i64 = 0;
        let mut max_dd_pct: f64 = 0.0;
        let mut sum_sq_pct: f64 = 0.0;
        let mut current_dd_bars: i64 = 0;
        let mut max_dd_bars: i64 = 0;
        for &value in &equity {
            if value > peak {
                peak = value;
            }
            let drop = peak - value;
            if drop > 0 {
                current_dd_bars += 1;
            } else {
                current_dd_bars = 0;
            }
            if current_dd_bars > max_dd_bars {
                max_dd_bars = current_dd_bars;
            }
            if drop > max_dd_paisa {
                max_dd_paisa = drop;
            }
            if peak > 0 {
                let dd_pct = (drop as f64) / (peak as f64);
                if dd_pct > max_dd_pct {
                    max_dd_pct = dd_pct;
                }
                sum_sq_pct += dd_pct * dd_pct;
            }
        }
        let ulcer = (sum_sq_pct / (n_bars as f64)).sqrt();
        (max_dd_paisa, max_dd_pct * 100.0, max_dd_bars, ulcer * 100.0)
    }

    fn bits(x: f64) -> u64 {
        x.to_bits()
    }

    // ── known values ────────────────────────────────────────────────────

    #[test]
    fn flat_curve_is_all_zero_with_positive_zero_floats() {
        // Zero exits ⇒ equity ≡ start ⇒ (0, +0.0, 0, +0.0). The signed-zero
        // identity the Python zero-trade shortcut relies on.
        let (dd, pct, bars, ulcer) =
            drawdown_stats_checked(5, 10_000, &[]).expect("flat curve in envelope");
        assert_eq!(dd, 0);
        assert_eq!(bars, 0);
        assert_eq!(bits(pct), bits(0.0)); // +0.0, not -0.0
        assert_eq!(bits(ulcer), bits(0.0));
    }

    #[test]
    fn monotone_up_has_zero_drawdown_nonzero_only_at_no_bar() {
        let exits = vec![(1usize, 50i64), (3usize, 25i64)];
        let (dd, pct, bars, ulcer) = drawdown_stats_checked(4, 1_000, &exits).expect("in envelope");
        assert_eq!((dd, bars), (0, 0));
        assert_eq!(bits(pct), bits(0.0));
        assert_eq!(bits(ulcer), bits(0.0));
    }

    #[test]
    fn single_drop_known_values() {
        // equity: [1000, 900, 900, 1100] — one −100 drop at bar 1, recovered
        // at bar 3. max_dd = 100; dd_pct = 100/1000 = 10%; duration = 2 bars
        // (bars 1 and 2 below the 1000 peak); ulcer = sqrt((0 + .01 + .01 + 0)/4)·100.
        let exits = vec![(1usize, -100i64), (3usize, 200i64)];
        let (dd, pct, bars, ulcer) = drawdown_stats_checked(4, 1_000, &exits).expect("ok");
        assert_eq!(dd, 100);
        assert_eq!(bars, 2);
        assert_eq!(bits(pct), bits((100.0f64 / 1000.0) * 100.0));
        // The expected ulcer accumulates dd_pct² SEQUENTIALLY, exactly as the
        // fold does (0.1² is NOT exactly 0.01 in f64 — a 0.02 literal is 1 ulp
        // off the true left-to-right sum).
        let dd_pct = 100.0f64 / 1000.0;
        let sum_sq = (dd_pct * dd_pct) + (dd_pct * dd_pct);
        assert_eq!(bits(ulcer), bits((sum_sq / 4.0).sqrt() * 100.0));
    }

    #[test]
    fn ruin_drawdown_exceeds_100_pct_and_is_not_clamped() {
        // equity: [1000, -500] — the drop (1500) exceeds the peak (1000):
        // 150% drawdown, deliberately NOT clamped (compute.py docstring).
        let exits = vec![(1usize, -1_500i64)];
        let (dd, pct, bars, _ulcer) = drawdown_stats_checked(2, 1_000, &exits).expect("ok");
        assert_eq!(dd, 1_500);
        assert_eq!(bars, 1);
        assert_eq!(bits(pct), bits(150.0));
    }

    #[test]
    fn nonpositive_peak_skips_the_pct_accumulation() {
        // Bar 0 loss drives equity (and thus the running peak) ≤ 0: the
        // pct block must be SKIPPED for those bars (Python `if peak > 0`),
        // while the paisa/duration stats still accumulate. equity:
        // [-100, -100, 300]: peak ≤ 0 on bars 0-1, positive from bar 2.
        let exits = vec![(0usize, -1_100i64), (2usize, 400i64)];
        let (dd, pct, bars, ulcer) = drawdown_stats_checked(3, 1_000, &exits).expect("ok");
        assert_eq!(dd, 0); // peak tracks value down: never a positive drop here
        assert_eq!(bars, 0);
        assert_eq!(bits(pct), bits(0.0));
        assert_eq!(bits(ulcer), bits(0.0));
    }

    #[test]
    fn zero_peak_never_enters_the_pct_scan() {
        // DEC-MUTATION-RUNTIME-003: kills the `peak > 0` → `peak >= 0` mutant.
        // start=1, bar0 pnl −1 → running 0 (peak exactly 0), bar1 pnl −1 →
        // running −1, drop = 1 with peak == 0. The pct scan must SKIP
        // (peak > 0 strictly): the `>=` mutant divides drop/0 → inf pct +
        // inf/NaN ulcer.
        let out = drawdown_stats_checked(2, 1, &[(0, -1), (1, -1)]).expect("in envelope");
        assert_eq!(out.0, 1); // max_dd_paisa
        assert_eq!(bits(out.1), bits(0.0)); // max_dd_pct — NOT inf (NaN/inf-path pin)
        assert_eq!(out.2, 1); // one bar below the 0 peak
        assert_eq!(bits(out.3), bits(0.0)); // ulcer — NOT inf/NaN
    }

    #[test]
    fn negative_trough_after_positive_peak_counts_full_drop() {
        // equity: [1000, -200, -200] — peak 1000 stays positive, value goes
        // negative: drop = 1200 > peak, pct = 120%, TWO bars below peak.
        let exits = vec![(1usize, -1_200i64)];
        let (dd, pct, bars, _ulcer) = drawdown_stats_checked(3, 1_000, &exits).expect("ok");
        assert_eq!(dd, 1_200);
        assert_eq!(bars, 2);
        assert_eq!(bits(pct), bits(120.0));
    }

    #[test]
    fn duration_resets_on_new_peak_and_keeps_longest_run() {
        // equity: [100, 90, 90, 120, 110] — run of 2 below the 100 peak,
        // reset at the new 120 peak, then a fresh 1-bar run.
        let exits = vec![(1usize, -10i64), (3usize, 30i64), (4usize, -10i64)];
        let (dd, _pct, bars, _ulcer) = drawdown_stats_checked(5, 100, &exits).expect("ok");
        assert_eq!(bars, 2);
        assert_eq!(dd, 10);
    }

    #[test]
    fn multiple_exits_on_one_bar_accumulate() {
        // Two exits scattered onto bar 1 sum before the fold: −30 −20 = −50.
        let exits = vec![(1usize, -30i64), (1usize, -20i64)];
        let (dd, _pct, bars, _ulcer) = drawdown_stats_checked(2, 1_000, &exits).expect("ok");
        assert_eq!(dd, 50);
        assert_eq!(bars, 1);
    }

    #[test]
    fn exit_on_first_and_last_bar_are_in_range() {
        let exits = vec![(0usize, -10i64), (2usize, 10i64)];
        assert!(drawdown_stats_checked(3, 1_000, &exits).is_ok());
    }

    // ── boundary validation (Result, never a panic) ─────────────────────

    #[test]
    fn zero_bars_is_rejected() {
        let err = drawdown_stats_checked(0, 1_000, &[]).unwrap_err();
        assert!(err.contains("n_bars must be >= 1"), "{err}");
    }

    #[test]
    fn nonpositive_start_is_rejected() {
        for start in [0i64, -1, i64::MIN] {
            let err = drawdown_stats_checked(1, start, &[]).unwrap_err();
            assert!(err.contains("start_equity_paisa must be > 0"), "{err}");
        }
    }

    #[test]
    fn out_of_range_exit_index_is_rejected() {
        let err = drawdown_stats_checked(3, 1_000, &[(3usize, 1i64)]).unwrap_err();
        assert!(err.contains("out of range for 3 bars"), "{err}");
    }

    #[test]
    fn envelope_boundary_is_inclusive() {
        // start + Σ|pnl| == 2^52 exactly: the LAST admissible input.
        let start = 1i64;
        let pnl = MAX_EXACT_EQUITY_PAISA - 1;
        assert!(drawdown_stats_checked(1, start, &[(0, pnl)]).is_ok());
        // One paisa more crosses the envelope.
        let err = drawdown_stats_checked(1, start + 1, &[(0, pnl)]).unwrap_err();
        assert!(err.contains("exceeds the"), "{err}");
    }

    #[test]
    fn start_alone_above_envelope_is_rejected_via_the_running_magnitude() {
        // A start above 2^52 with ONE exit trips the magnitude check on the
        // first exit (a zero-exit call never enters the pct-hazard division
        // with drop > 0, but the wrapper pre-guards it anyway — belt).
        let err = drawdown_stats_checked(1, MAX_EXACT_EQUITY_PAISA + 1, &[(0, 0)]).unwrap_err();
        assert!(err.contains("exceeds the"), "{err}");
    }

    #[test]
    fn i64_min_pnl_is_rejected_not_aborted() {
        // |i64::MIN| overflows checked_abs — must be a clean Err under the
        // overflow-checks=true + panic=abort profile, never a SIGABRT.
        let err = drawdown_stats_checked(1, 1_000, &[(0usize, i64::MIN)]).unwrap_err();
        assert!(err.contains("overflows the exactness envelope"), "{err}");
    }

    #[test]
    fn sum_of_pnls_overflowing_i64_is_rejected() {
        let big = i64::MAX / 2 + 1;
        let err = drawdown_stats_checked(2, 1_000, &[(0usize, big), (1usize, big)]).unwrap_err();
        // The FIRST big pnl already exceeds the 2^52 envelope, so the
        // envelope message fires before the checked_add could; both are Err.
        assert!(err.contains("envelope"), "{err}");
    }

    // ── fused fold == two-pass oracle (bitwise) ─────────────────────────

    #[test]
    fn fused_fold_matches_two_pass_oracle_on_known_shapes() {
        let cases: Vec<(usize, i64, Vec<(usize, i64)>)> = vec![
            (1, 1, vec![]),
            (1, 1_000, vec![(0, -999)]),
            (7, 10_000_000, vec![(2, -5_000), (3, 5_000), (6, -1)]),
            (5, 100, vec![(0, -101), (4, 300)]),
            (4, 1_000, vec![(1, -100), (1, -100), (2, 150)]),
        ];
        for (n_bars, start, exits) in cases {
            let fused = drawdown_stats_checked(n_bars, start, &exits).expect("in envelope");
            let oracle = two_pass_oracle(n_bars, start, &exits);
            assert_eq!(fused.0, oracle.0);
            assert_eq!(bits(fused.1), bits(oracle.1));
            assert_eq!(fused.2, oracle.2);
            assert_eq!(bits(fused.3), bits(oracle.3));
        }
    }

    // ── proptest: fused == two-pass over random windows ─────────────────

    use proptest::prelude::*;

    proptest! {
        #[test]
        fn prop_fused_fold_is_bitwise_equal_to_two_pass(
            n_bars in 1usize..64,
            start in 1i64..=1_000_000,
            raw in proptest::collection::vec((0usize..64, -1_000_000i64..=1_000_000), 0..32),
        ) {
            let exits: Vec<(usize, i64)> =
                raw.into_iter().map(|(i, p)| (i % n_bars, p)).collect();
            let fused = drawdown_stats_checked(n_bars, start, &exits)
                .expect("bounded generator stays inside the envelope");
            let oracle = two_pass_oracle(n_bars, start, &exits);
            prop_assert_eq!(fused.0, oracle.0);
            prop_assert_eq!(bits(fused.1), bits(oracle.1));
            prop_assert_eq!(fused.2, oracle.2);
            prop_assert_eq!(bits(fused.3), bits(oracle.3));
        }
    }
}
