//! Metrics-FOLD kernel — the Rust mirror of `brutex.metrics.compute._fold_trades`.
//!
//! DEC-METRICS-RUST-COMPUTE-FOLD-001 (the Rust arm of the Slice-0
//! fold/combine decomposition, DEC-SWEEP-COMPUTE-FOLD-DECOMP-001 /
//! MIGRATION.md #1685). The per-trade FOLD pass of the metric body —
//! the single-pass integer trade aggregates, the `_consecutive_runs`
//! streak folds, the Welford/Pébay moment recurrence, the downside Σr²
//! Sortino term, and the p95/p5 percentile folds — transcribed
//! TERM-FOR-TERM from `_fold_trades` (`brutex/metrics/compute.py`).
//! The scalar COMBINE tail (`combine_metrics_from_folds`) deliberately
//! STAYS in Python: it carries libm `pow` (`growth ** (1.0/span_years)`
//! for CAGR; `math.pow(m2_n, 1.5)` for skewness), which has NO
//! correct-rounding guarantee across libms — the exact MUST-STAY-PYTHON
//! class the sibling `metrics.rs` module documents. The fold below uses
//! ONLY IEEE-exact-or-correctly-rounded operations (`+ - * /`, min/max,
//! comparisons — NO sqrt, NO pow, NO `mul_add`/FMA), so bit-identity to
//! CPython holds unconditionally given identical f64 inputs.
//!
//! ## Bit-exactness (Golden Rule #4) — why the RETURNS column crosses as f64
//!
//! The Python fold computes `returns = [t.pnl_paisa / start_equity_paisa …]`
//! with CPython's `long_true_divide` — a CORRECTLY ROUNDED division of the
//! exact rational, which a naive `(pnl as f64) / (start as f64)` double-rounds
//! whenever either operand exceeds 2⁵³. Rather than carry an exactness
//! envelope, the Python seam (`brutex.metrics.compute_rust`) computes the
//! returns column with the VERBATIM Python expression and passes it across
//! the FFI as f64 — bit-identical BY CONSTRUCTION, no envelope needed. Every
//! other float in the fold is derived from those f64s by plain sequential
//! `+ - * /` in the exact operation order of the Python body:
//!
//! * the Welford/Pébay recurrence mirrors `_accumulate_moments` line by
//!   line — same update order (`mean`, then `m4`, then `m3`, then `m2`),
//!   same association (Rust's `a * b * c` / `a + b - c` group exactly like
//!   Python's), integer subterms (`n*n - 3*n + 3`, `n - 2`, `n1`) computed
//!   EXACTLY in i128 then converted once (Rust `as f64` and CPython
//!   int→float both round half-even);
//! * the downside Σr² mirrors CPython 3.12+'s builtin `sum()` — which is
//!   NEUMAIER-COMPENSATED for floats (gh-100425), not a plain fold: the
//!   int-0 start hands the first `r*r` through the generic `0 + x1` add
//!   (`≡ x1` — `r*r` is never `-0.0`), compensation runs from the second
//!   term, and the tail adds `c` only when nonzero AND finite (the exact
//!   `builtin_sum` float fast path; a plain fold is 1 ulp off on real
//!   corpora — burn-in-caught on the golden `dense_1500` case);
//! * the percentile interpolation mirrors `percentile` token-for-token
//!   (`(pct/100.0)*(n-1)`, floor/ceil, `lo*(1.0-frac) + hi*frac`).
//!
//! Sorting: Python's stable Timsort and `slice::sort_by(f64::total_cmp)`
//! produce IDENTICAL value sequences here because the returns column can
//! contain neither NaN (validated at the boundary) nor `-0.0` (`pnl/start`
//! with integer `|pnl| ≥ 1`, `1 ≤ start ≤ i64::MAX` has magnitude
//! ≥ 1/2⁶³ ≈ 1.1e-19 ≫ the underflow threshold, and `0/start` is `+0.0`) —
//! so equal-comparing elements are bit-identical and any order among them
//! yields the same sorted VALUES. `total_cmp` (never `partial_cmp` +
//! unwrap) keeps the crate's deny(unwrap) discipline and cannot panic.
//!
//! ## Integer exactness — why the sums return i128
//!
//! CPython folds the integer aggregates in arbitrary-precision ints. The
//! kernel accumulates them in i128: with `n ≤ 2⁶¹` (each i64 column element
//! occupies 8 bytes, so a real machine cannot hold more) and `|pnl| ≤ 2⁶³`,
//! every partial sum is bounded by `2⁶¹ · 2⁶³ = 2¹²⁴ < i128::MAX` — provably
//! overflow-free under the crate's `overflow-checks = true` profile (the
//! `metrics.rs` validate-at-the-edge idiom). The i128s convert losslessly to
//! Python ints at the FFI boundary. `gross_loss_abs += -pnl` is folded in
//! i128 too, so `-(i64::MIN)` cannot overflow.
//!
//! ## Failure mode — `Result`, NEVER a panic
//!
//! Every boundary violation returns a pure `Result<_, String>` mapped to a
//! clean Python `ValueError`; the Python seam treats ANY kernel error as
//! "serve this row from the Python oracle". Under the release profile's
//! `panic = "abort"` a genuine allocation failure aborts (the same envelope
//! as the sibling kernels — one `O(n)` sort scratch buffer per call).
//!
//! ## Complexity (honest envelope — NOT O(1))
//!
//! One call is `O(n·log n)` (the percentile sort dominates; the folds are
//! `O(n)`), exactly like the Python arm. A constant-factor lever only;
//! claiming otherwise is FORBIDDEN.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

/// The fold outputs, mirroring `brutex.metrics.compute.TradeFolds`
/// field-for-field (the `moments` value object flattened to its five
/// scalars). Integer SUMS are i128 (see the module docstring); counts,
/// maxima and the signed most-negative pnl stay i64 (each is bounded by a
/// single input element or the trade count).
#[derive(Debug, Clone, PartialEq)]
pub struct TradeFoldsOut {
    pub n_trades: usize,
    pub net_pnl_paisa: i128,
    pub gross_profit_paisa: i128,
    pub gross_loss_abs_paisa: i128,
    pub winners: i64,
    pub losers: i64,
    pub sum_mfe_paisa: i128,
    pub sum_mae_paisa: i128,
    pub sum_holding_bars: i128,
    pub max_holding_bars: i64,
    pub largest_loss_paisa: i64,
    pub max_consecutive_losses: i64,
    pub max_consecutive_wins: i64,
    pub moments_n: i64,
    pub moments_mean: f64,
    pub moments_m2: f64,
    pub moments_m3: f64,
    pub moments_m4: f64,
    pub downside_sum_sq: f64,
    pub p95: f64,
    pub p5: f64,
}

/// Mirror of `brutex.metrics.compute.percentile` (NumPy "linear"
/// convention over a PRE-SORTED slice) — token-for-token: the rank
/// expression, the floor/ceil integral short-circuit, and the
/// `lo*(1.0-frac) + hi*frac` interpolation grouping are the Python
/// body's own shapes, so every rounding step is shared.
///
/// Caller guarantees `values` is non-empty and pre-sorted; `pct` is a
/// compile-time literal (95.0 / 5.0) here, so `rank` is always finite
/// and in `[0, len-1]` — the indices below cannot go out of bounds.
fn percentile_sorted(values: &[f64], pct: f64) -> f64 {
    if values.len() == 1 {
        return values[0];
    }
    let rank = (pct / 100.0) * ((values.len() - 1) as f64);
    let lower = rank.floor();
    let upper = rank.ceil();
    if lower == upper {
        return values[lower as usize];
    }
    let frac = rank - lower;
    values[lower as usize] * (1.0 - frac) + values[upper as usize] * frac
}

/// Pure core of [`metrics_fold_trades`] — `Result<_, String>` (NO `PyErr`)
/// so the no-libpython `cargo test` link exercises every branch (the
/// sibling `metrics.rs` split). Mirrors `_fold_trades` +
/// `_consecutive_runs` + `_accumulate_moments` over the five parallel
/// per-trade columns; `returns` is the Python-computed
/// `pnl_paisa / start_equity_paisa` column (module docstring).
pub fn fold_trades_checked(
    pnl_paisa: &[i64],
    mfe_paisa: &[i64],
    mae_paisa: &[i64],
    holding_bars: &[i64],
    returns: &[f64],
) -> Result<TradeFoldsOut, String> {
    let n_trades = pnl_paisa.len();
    if n_trades == 0 {
        return Err(
            "metrics_fold_trades: trades must be non-empty (the zero-trade row \
             is _empty_metrics, the caller's arm)"
                .to_string(),
        );
    }
    if mfe_paisa.len() != n_trades
        || mae_paisa.len() != n_trades
        || holding_bars.len() != n_trades
        || returns.len() != n_trades
    {
        return Err(format!(
            "metrics_fold_trades: column lengths differ — pnl {}, mfe {}, mae {}, \
             holding {}, returns {}",
            n_trades,
            mfe_paisa.len(),
            mae_paisa.len(),
            holding_bars.len(),
            returns.len()
        ));
    }
    // ClosedTrade invariant belts (mfe/mae >= 0, holding >= 1) + the
    // finite-returns law (a NaN/inf return cannot arise from the seam's own
    // int/int division — CPython would have raised OverflowError first — so
    // a violation here means a poisoned caller; refuse, never fold garbage).
    for i in 0..n_trades {
        if mfe_paisa[i] < 0 || mae_paisa[i] < 0 {
            return Err(format!(
                "metrics_fold_trades: mfe/mae must be >= 0, got ({}, {}) at row {i}",
                mfe_paisa[i], mae_paisa[i]
            ));
        }
        if holding_bars[i] < 1 {
            return Err(format!(
                "metrics_fold_trades: holding_bars must be >= 1, got {} at row {i}",
                holding_bars[i]
            ));
        }
        if !returns[i].is_finite() {
            return Err(format!(
                "metrics_fold_trades: returns must be finite, got {} at row {i}",
                returns[i]
            ));
        }
    }

    // ── Per-trade aggregates (single pass) — `_fold_trades`'s first loop.
    // Integer arithmetic is EXACT (i128 sums, module-docstring bound), so
    // only the LOOP BODY shape must match Python, not float rounding.
    let mut net_pnl: i128 = 0;
    let mut gross_profit: i128 = 0;
    let mut gross_loss_abs: i128 = 0; // accumulated as a non-negative magnitude
    let mut winners: i64 = 0;
    let mut losers: i64 = 0;
    let mut sum_mfe: i128 = 0;
    let mut sum_mae: i128 = 0;
    let mut sum_holding: i128 = 0;
    let mut max_holding: i64 = 0;
    let mut largest_loss: i64 = 0; // most-negative pnl, stored signed
    for i in 0..n_trades {
        let pnl = pnl_paisa[i];
        net_pnl += pnl as i128;
        sum_mfe += mfe_paisa[i] as i128;
        sum_mae += mae_paisa[i] as i128;
        sum_holding += holding_bars[i] as i128;
        max_holding = max_holding.max(holding_bars[i]);
        if pnl > 0 {
            // ClosedTrade.is_win — strictly positive P&L.
            winners += 1;
            gross_profit += pnl as i128;
        } else if pnl < 0 {
            // ClosedTrade.is_loss — strictly negative P&L.
            losers += 1;
            gross_loss_abs += -(pnl as i128);
            largest_loss = largest_loss.min(pnl);
        }
    }

    // ── Streak folds — `_consecutive_runs` (win = pnl > 0, loss = pnl < 0,
    // scratch breaks both), exact integer state machine.
    let mut max_losses: i64 = 0;
    let mut max_wins: i64 = 0;
    let mut cur_losses: i64 = 0;
    let mut cur_wins: i64 = 0;
    for &pnl in pnl_paisa {
        if pnl > 0 {
            cur_wins += 1;
            cur_losses = 0;
        } else if pnl < 0 {
            cur_losses += 1;
            cur_wins = 0;
        } else {
            cur_wins = 0;
            cur_losses = 0;
        }
        max_wins = max_wins.max(cur_wins);
        max_losses = max_losses.max(cur_losses);
    }

    // ── Welford/Pébay moments — `_accumulate_moments` line by line: the
    // SAME sequential order over `returns`, the SAME update order
    // (`mean` first, then m4, then m3 — each reading the PRE-update m2/m3
    // — then m2 last), the SAME association (Rust `a * b * c` groups
    // `(a*b)*c` and `a + b - c` groups `(a+b)-c`, exactly like Python),
    // plain mul/add (NO `mul_add` — an FMA would fuse where CPython rounds
    // twice). Integer subterms are computed EXACTLY in i128 and converted
    // once (`as f64` rounds half-even, as does CPython's int→float).
    let mut n: i64 = 0;
    let mut mean: f64 = 0.0;
    let mut m2: f64 = 0.0;
    let mut m3: f64 = 0.0;
    let mut m4: f64 = 0.0;
    for &x in returns {
        let n1 = n;
        n += 1;
        let delta = x - mean;
        let delta_n = delta / (n as f64);
        let delta_n2 = delta_n * delta_n;
        let term1 = delta * delta_n * (n1 as f64);
        mean += delta_n;
        let ni = n as i128;
        let poly = (ni * ni - 3 * ni + 3) as f64;
        m4 += term1 * delta_n2 * poly + 6.0 * delta_n2 * m2 - 4.0 * delta_n * m3;
        m3 += term1 * delta_n * ((ni - 2) as f64) - 3.0 * delta_n * m2;
        m2 += term1;
    }

    // ── Downside Σr² — `sum(r * r for r in downside)` where CPython 3.12+'s
    // builtin `sum()` is NEUMAIER-COMPENSATED for floats (gh-100425), NOT a
    // plain left-to-right fold — a plain fold diverges by 1 ulp on real
    // corpora (caught by the seam's burn-in self-check on the golden
    // `dense_1500` case). Mirror `Python/bltinmodule.c builtin_sum` exactly:
    // the int-0 start hands the FIRST float to the generic `0 + x1` add
    // (`0.0 + x1 ≡ x1` here — `r*r` is never `-0.0`), the float fast path
    // then resumes with `c = 0.0`, compensating from the SECOND term on
    // (`t = f + x; c += (f - t) + x` when `|f| >= |x|` else `(x - t) + f`),
    // and the tail adds `c` only when it is nonzero AND finite.
    let mut downside_sum_sq: f64 = 0.0;
    let mut compensation: f64 = 0.0;
    let mut first_downside = true;
    for &r in returns {
        if r < 0.0 {
            let x = r * r;
            if first_downside {
                downside_sum_sq = x; // 0.0 + x1 ≡ x1 (x1 is never -0.0)
                first_downside = false;
                continue;
            }
            let t = downside_sum_sq + x;
            if downside_sum_sq.abs() >= x.abs() {
                compensation += (downside_sum_sq - t) + x;
            } else {
                compensation += (x - t) + downside_sum_sq;
            }
            downside_sum_sq = t;
        }
    }
    if compensation != 0.0 && compensation.is_finite() {
        downside_sum_sq += compensation;
    }

    // ── Percentile folds — sort a copy (Python's `sorted(returns)`), then
    // the NumPy-"linear" interpolation at 95.0 / 5.0. `total_cmp` ≡ the
    // Python sort's value order here (no NaN, no -0.0 — module docstring).
    let mut sorted_returns = returns.to_vec();
    sorted_returns.sort_by(f64::total_cmp);
    let p95 = percentile_sorted(&sorted_returns, 95.0);
    let p5 = percentile_sorted(&sorted_returns, 5.0);

    Ok(TradeFoldsOut {
        n_trades,
        net_pnl_paisa: net_pnl,
        gross_profit_paisa: gross_profit,
        gross_loss_abs_paisa: gross_loss_abs,
        winners,
        losers,
        sum_mfe_paisa: sum_mfe,
        sum_mae_paisa: sum_mae,
        sum_holding_bars: sum_holding,
        max_holding_bars: max_holding,
        largest_loss_paisa: largest_loss,
        max_consecutive_losses: max_losses,
        max_consecutive_wins: max_wins,
        moments_n: n,
        moments_mean: mean,
        moments_m2: m2,
        moments_m3: m3,
        moments_m4: m4,
        downside_sum_sq,
        p95,
        p5,
    })
}

/// The wire shape [`metrics_fold_trades`] returns — four nested tuples
/// (PyO3's tuple conversions cap at arity 12, so the 21 scalars ship
/// grouped; the Python seam reassembles `TradeFolds` by name):
///
/// 1. counts/maxima: `(n_trades, winners, losers, max_holding_bars,
///    largest_loss_paisa, max_consecutive_losses, max_consecutive_wins)`;
/// 2. exact integer sums (Python ints, folded in i128): `(net_pnl_paisa,
///    gross_profit_paisa, gross_loss_abs_paisa, sum_mfe_paisa,
///    sum_mae_paisa, sum_holding_bars)`;
/// 3. moments: `(n, mean, m2, m3, m4)`;
/// 4. floats: `(downside_sum_sq, p95, p5)`.
pub type FoldWire = (
    (usize, i64, i64, i64, i64, i64, i64),
    (i128, i128, i128, i128, i128, i128),
    (i64, f64, f64, f64, f64),
    (f64, f64, f64),
);

/// Group a [`TradeFoldsOut`] into the [`FoldWire`] tuple shape.
pub fn fold_wire(out: &TradeFoldsOut) -> FoldWire {
    (
        (
            out.n_trades,
            out.winners,
            out.losers,
            out.max_holding_bars,
            out.largest_loss_paisa,
            out.max_consecutive_losses,
            out.max_consecutive_wins,
        ),
        (
            out.net_pnl_paisa,
            out.gross_profit_paisa,
            out.gross_loss_abs_paisa,
            out.sum_mfe_paisa,
            out.sum_mae_paisa,
            out.sum_holding_bars,
        ),
        (
            out.moments_n,
            out.moments_mean,
            out.moments_m2,
            out.moments_m3,
            out.moments_m4,
        ),
        (out.downside_sum_sq, out.p95, out.p5),
    )
}

/// The per-trade metrics FOLD over five parallel columns — the Rust mirror
/// of `brutex.metrics.compute._fold_trades` (see the module docstring).
///
/// `returns` MUST be the Python-computed
/// `[t.pnl_paisa / start_equity_paisa for t in trades]` column (bit-identity
/// by construction — the seam owns that expression). Raises `ValueError` on
/// any input outside the validated envelope; the Python seam routes that row
/// to the bit-exact Python oracle instead of surfacing the error.
#[pyfunction]
pub fn metrics_fold_trades(
    pnl_paisa: Vec<i64>,
    mfe_paisa: Vec<i64>,
    mae_paisa: Vec<i64>,
    holding_bars: Vec<i64>,
    returns: Vec<f64>,
) -> PyResult<FoldWire> {
    fold_trades_checked(&pnl_paisa, &mfe_paisa, &mae_paisa, &holding_bars, &returns)
        .map(|out| fold_wire(&out))
        .map_err(PyValueError::new_err)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The pinned fixture from
    /// `tests/golden/test_compute_fold_combine_parity.py` (`_PIN_TRADES`):
    /// w, w, l, l, l, scratch, w over a 1_000_000-paisa base.
    fn pin_columns() -> (Vec<i64>, Vec<i64>, Vec<i64>, Vec<i64>, Vec<f64>) {
        let pnl: Vec<i64> = vec![500, 700, -300, -400, -300, 0, 900];
        let mfe: Vec<i64> = vec![600, 800, 50, 10, 20, 0, 950];
        let mae: Vec<i64> = vec![100, 50, 400, 500, 350, 0, 60];
        let hold: Vec<i64> = vec![3, 2, 2, 4, 1, 1, 5];
        // Both operands ≤ 2^53 ⇒ the division is the correctly rounded
        // quotient — exactly CPython's `pnl / 1_000_000`.
        let returns: Vec<f64> = pnl.iter().map(|&p| (p as f64) / 1_000_000.0).collect();
        (pnl, mfe, mae, hold, returns)
    }

    /// Exact-value integer pins — hand-captured from the Python golden
    /// (`TestFoldExactValuePins.test_fold_integer_aggregates_exact`).
    #[test]
    fn fold_integer_aggregates_exact() {
        let (pnl, mfe, mae, hold, returns) = pin_columns();
        let out = match fold_trades_checked(&pnl, &mfe, &mae, &hold, &returns) {
            Ok(out) => out,
            Err(err) => panic!("fold refused the pin fixture: {err}"),
        };
        assert_eq!(out.n_trades, 7);
        assert_eq!(out.net_pnl_paisa, 1100);
        assert_eq!(out.gross_profit_paisa, 2100);
        assert_eq!(out.gross_loss_abs_paisa, 1000);
        assert_eq!(out.winners, 3);
        assert_eq!(out.losers, 3);
        assert_eq!(out.sum_mfe_paisa, 2430);
        assert_eq!(out.sum_mae_paisa, 1460);
        assert_eq!(out.sum_holding_bars, 18);
        assert_eq!(out.max_holding_bars, 5);
        assert_eq!(out.largest_loss_paisa, -400);
        assert_eq!(out.max_consecutive_losses, 3);
        assert_eq!(out.max_consecutive_wins, 2);
        assert_eq!(out.moments_n, 7);
    }

    /// Bit-exact float pins — the Python golden's `float.fromhex` literals
    /// re-encoded as IEEE-754 bit patterns (derived from
    /// `TestFoldExactValuePins`, hand-captured 2026-07-09; converted via
    /// `struct.pack("<d", float.fromhex(...))`).
    #[test]
    fn fold_float_fields_bit_exact() {
        let (pnl, mfe, mae, hold, returns) = pin_columns();
        let out = match fold_trades_checked(&pnl, &mfe, &mae, &hold, &returns) {
            Ok(out) => out,
            Err(err) => panic!("fold refused the pin fixture: {err}"),
        };
        // 0x1.498d6dd4d1037p-13
        assert_eq!(out.moments_mean.to_bits(), 0x3f2498d6dd4d1037);
        // 0x1.ccf1289b920b0p-20
        assert_eq!(out.moments_m2.to_bits(), 0x3ebccf1289b920b0);
        // 0x1.0a70dac47959ap-32
        assert_eq!(out.moments_m3.to_bits(), 0x3df0a70dac47959a);
        // 0x1.4bdb1ae58a600p-41
        assert_eq!(out.moments_m4.to_bits(), 0x3d64bdb1ae58a600);
        // 0x1.6d127d05394fdp-22
        assert_eq!(out.downside_sum_sq.to_bits(), 0x3e96d127d05394fd);
        // 0x1.b866e43aa79bap-11
        assert_eq!(out.p95.to_bits(), 0x3f4b866e43aa79ba);
        // -0x1.83f91e646f156p-12
        assert_eq!(out.p5.to_bits(), 0xbf383f91e646f156);
    }

    /// The downside Σr² must be CPython 3.12+'s NEUMAIER-compensated
    /// `sum()`, not a plain fold: ten `-0.1` returns square to
    /// `0.010000000000000002` each, whose plain fold ends
    /// `0x3fb999999999999c` while CPython's `sum()` ends
    /// `0x3fb999999999999b` (hand-captured 2026-07-16 on CPython 3.12 —
    /// the exact 1-ulp class the seam's burn-in caught on `dense_1500`).
    #[test]
    fn downside_sum_is_neumaier_compensated() {
        let pnl: Vec<i64> = vec![-1000; 10];
        let z: Vec<i64> = vec![0; 10];
        let h: Vec<i64> = vec![1; 10];
        // -1000 / 10000 = -0.1 exactly as CPython computes it (both
        // operands ≤ 2^53 ⇒ correctly rounded).
        let returns: Vec<f64> = pnl.iter().map(|&p| (p as f64) / 10_000.0).collect();
        let out = match fold_trades_checked(&pnl, &z, &z, &h, &returns) {
            Ok(out) => out,
            Err(err) => panic!("{err}"),
        };
        assert_eq!(out.downside_sum_sq.to_bits(), 0x3fb999999999999b);
        // The plain fold's answer is REFUSED (anti-vacuous teeth).
        assert_ne!(out.downside_sum_sq.to_bits(), 0x3fb999999999999c);
    }

    /// The `-7 // 2` corpus class feeds from these exact sums — pin the
    /// negative-net fold + the empty-downside +0.0 arm + a huge-magnitude
    /// i128 sum the i64 accumulator would overflow on.
    #[test]
    fn fold_edge_classes() {
        // negative_expectancy_floor: net -7 over 2 losers.
        let pnl: Vec<i64> = vec![-3, -4];
        let z: Vec<i64> = vec![0, 0];
        let h: Vec<i64> = vec![1, 1];
        let returns: Vec<f64> = pnl.iter().map(|&p| (p as f64) / 999.0).collect();
        let out = match fold_trades_checked(&pnl, &z, &z, &h, &returns) {
            Ok(out) => out,
            Err(err) => panic!("{err}"),
        };
        assert_eq!(out.net_pnl_paisa, -7);
        assert_eq!(out.gross_loss_abs_paisa, 7);
        assert_eq!(out.largest_loss_paisa, -4);
        assert_eq!(out.max_consecutive_losses, 2);
        assert_eq!(out.max_consecutive_wins, 0);

        // scratch_only: empty downside stays exactly +0.0 (the Python
        // `sum([])` int-0 arm — `0/n` reproduces the same +0.0 combine-side).
        let pnl: Vec<i64> = vec![0, 0];
        let returns: Vec<f64> = vec![0.0, 0.0];
        let out = match fold_trades_checked(&pnl, &z, &z, &h, &returns) {
            Ok(out) => out,
            Err(err) => panic!("{err}"),
        };
        assert_eq!(out.downside_sum_sq.to_bits(), 0.0f64.to_bits());
        assert_eq!(out.winners, 0);
        assert_eq!(out.losers, 0);
        assert_eq!(out.largest_loss_paisa, 0);

        // i128 accumulation: 4 trades at i64::MIN would overflow an i64 sum.
        let pnl: Vec<i64> = vec![i64::MIN, i64::MIN, i64::MIN, i64::MIN];
        let z4: Vec<i64> = vec![0, 0, 0, 0];
        let h4: Vec<i64> = vec![1, 1, 1, 1];
        let returns: Vec<f64> = pnl.iter().map(|&p| (p as f64) / 1_000_000.0).collect();
        let out = match fold_trades_checked(&pnl, &z4, &z4, &h4, &returns) {
            Ok(out) => out,
            Err(err) => panic!("{err}"),
        };
        assert_eq!(out.net_pnl_paisa, 4 * (i64::MIN as i128));
        assert_eq!(out.gross_loss_abs_paisa, -4 * (i64::MIN as i128));
        assert_eq!(out.largest_loss_paisa, i64::MIN);
    }

    /// The percentile mirror at both interpolation arms: single element,
    /// integral rank (lower == upper), and the fractional blend.
    #[test]
    fn percentile_mirror() {
        assert_eq!(percentile_sorted(&[7.5], 95.0).to_bits(), 7.5f64.to_bits());
        // Three elements at pct=50 → rank 1.0 exactly → direct index.
        assert_eq!(
            percentile_sorted(&[1.0, 2.0, 5.0], 50.0).to_bits(),
            2.0f64.to_bits()
        );
        // Two elements at pct=95 → rank 0.95 → 1.0*(1-0.95) + 2.0*0.95.
        let expected = 1.0f64 * (1.0 - 0.95) + 2.0 * 0.95;
        assert_eq!(
            percentile_sorted(&[1.0, 2.0], 95.0).to_bits(),
            expected.to_bits()
        );
    }

    /// Every boundary refusal arm returns Err (never panics).
    #[test]
    fn boundary_refusals() {
        let ok: Vec<i64> = vec![1];
        let h: Vec<i64> = vec![1];
        let r: Vec<f64> = vec![0.001];
        // Empty columns.
        assert!(fold_trades_checked(&[], &[], &[], &[], &[]).is_err());
        // Length mismatch.
        assert!(fold_trades_checked(&ok, &[], &ok, &h, &r).is_err());
        assert!(fold_trades_checked(&ok, &ok, &ok, &h, &[]).is_err());
        // Negative mfe / mae.
        assert!(fold_trades_checked(&ok, &[-1], &ok, &h, &r).is_err());
        assert!(fold_trades_checked(&ok, &ok, &[-1], &h, &r).is_err());
        // holding_bars < 1.
        assert!(fold_trades_checked(&ok, &ok, &ok, &[0], &r).is_err());
        // Non-finite return.
        assert!(fold_trades_checked(&ok, &ok, &ok, &h, &[f64::NAN]).is_err());
        assert!(fold_trades_checked(&ok, &ok, &ok, &h, &[f64::INFINITY]).is_err());
    }

    /// The wire grouping is a pure reshape — every field crosses once.
    #[test]
    fn wire_grouping_is_lossless() {
        let (pnl, mfe, mae, hold, returns) = pin_columns();
        let out = match fold_trades_checked(&pnl, &mfe, &mae, &hold, &returns) {
            Ok(out) => out,
            Err(err) => panic!("{err}"),
        };
        let (counts, sums, moments, floats) = fold_wire(&out);
        assert_eq!(counts.0, out.n_trades);
        assert_eq!(counts.1, out.winners);
        assert_eq!(counts.2, out.losers);
        assert_eq!(counts.3, out.max_holding_bars);
        assert_eq!(counts.4, out.largest_loss_paisa);
        assert_eq!(counts.5, out.max_consecutive_losses);
        assert_eq!(counts.6, out.max_consecutive_wins);
        assert_eq!(sums.0, out.net_pnl_paisa);
        assert_eq!(sums.1, out.gross_profit_paisa);
        assert_eq!(sums.2, out.gross_loss_abs_paisa);
        assert_eq!(sums.3, out.sum_mfe_paisa);
        assert_eq!(sums.4, out.sum_mae_paisa);
        assert_eq!(sums.5, out.sum_holding_bars);
        assert_eq!(moments.0, out.moments_n);
        assert_eq!(moments.1.to_bits(), out.moments_mean.to_bits());
        assert_eq!(moments.2.to_bits(), out.moments_m2.to_bits());
        assert_eq!(moments.3.to_bits(), out.moments_m3.to_bits());
        assert_eq!(moments.4.to_bits(), out.moments_m4.to_bits());
        assert_eq!(floats.0.to_bits(), out.downside_sum_sq.to_bits());
        assert_eq!(floats.1.to_bits(), out.p95.to_bits());
        assert_eq!(floats.2.to_bits(), out.p5.to_bits());
    }
}
