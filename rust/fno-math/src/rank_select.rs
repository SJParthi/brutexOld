//! Multi-key selection kernels — ε-Pareto skyline + per-axis bounded top-M +
//! DD-band quantize (DEC-RANK-PARETO-PERSIST-001 / DEC-RANK-QUOTA-PERSIST-1000-001 /
//! DEC-RANK-DD-BAND-ORDER-001; LANE W1 of the winning-completeness P0 ruling,
//! 2026-07-18 — BORN-RUST law: new selection mechanics land here first, the
//! Python twin in `brutex.ranking.multi_select` is the parity ORACLE).
//!
//! ## Kernels
//!
//! * [`rank_pareto_frontier`] — the additive-ε Pareto dominance filter over the
//!   FOUR W1 axes `{net_pnl_paisa MAX, win_rate MAX, max_drawdown_pct MIN,
//!   losing_trades MIN}` (the pinned 5th axis `sl_hit_rate` is LANE W2's
//!   column — the axis table is one-row-extensible, see the dominance helper).
//!   Returns the ASCENDING indices of every row NOT ε-dominated by any other
//!   row. HONEST complexity (Golden Rule #10): **O(n²) worst case** (every
//!   pair compared when nothing dominates early), O(n·D) typical where D is
//!   the scan depth until a dominator is found — NEVER O(1), never claimed
//!   O(n log n) (a 4-axis ε-skyline has no simple sort-based decomposition;
//!   ε-dominance is NOT transitive, so incremental frontier maintenance
//!   would be UNSOUND — every candidate must be tested against the whole
//!   pool, per the definition "not dominated by ANY other gate-passer").
//! * [`rank_axis_topm`] — the per-axis bounded top-M select over the FIVE
//!   quota axes `{composite DESC, max_drawdown_pct ASC, losing_trades ASC,
//!   win_rate DESC, expectancy_paisa DESC}` under the non-composite
//!   tie-break law `(axis value, −composite, mask)` (composite keeps the
//!   existing `(−composite, mask)` law). Bounded binary heaps —
//!   **O(A·n·log M)** time, O(A·M) space.
//! * [`rank_dd_band_indices`] — the batch DD ε-band quantizer:
//!   `floor(dd / band_pct)` per element (IEEE f64 division + floor — the
//!   bit-identical twin of Python's `math.floor(dd / band)` on finite
//!   inputs). **O(n)**.
//!
//! ## Bit-identity to the Python oracle (Golden Rule #4)
//!
//! Every returned value is an INDEX (usize→u64) or an i64 band — integers
//! compare exactly across the FFI. The float comparisons inside use the SAME
//! IEEE-754 expressions, in the SAME form, as the Python oracle
//! (`brutex/ranking/multi_select.py`): the within-ε clauses are written
//! `x + eps >= y` (MAX axes) / `x - eps <= y` (MIN axes) and the strict
//! clauses `x > y + eps` (MAX) / `x < y - eps` (MIN) — the EXPRESSION FORM is
//! load-bearing (`x + eps >= y` and `x >= y - eps` round differently), so
//! both sides pin the same spelling. All axis floats are FINITE by the
//! `ResultRow` construction law (validated at the FFI edge anyway), so every
//! comparison is total.
//!
//! ## Failure mode — `Result`, NEVER a panic
//!
//! Length mismatches, non-finite floats, negative/non-finite ε, and
//! out-of-i64 band quotients all return `Err(String)` mapped to a Python
//! `ValueError`; the Python seam (`brutex.engine.rust_rank_select`) treats
//! any kernel error as "serve this pool from the Python oracle".

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use std::cmp::Ordering;
use std::collections::BinaryHeap;

/// One candidate's Pareto-axis payload (the four W1 axes).
///
/// Extending the axis set (W2's `sl_hit_rate` MIN axis) is ONE new field
/// here, one clause in each of [`dominates_within`] / [`dominates_strict`],
/// and the FFI column — the "axis table is one row" extensibility promise.
#[derive(Clone, Copy)]
struct ParetoPoint {
    net_pnl_paisa: i64,
    win_rate: f64,
    max_dd_pct: f64,
    losing_trades: i64,
}

/// The per-axis ε payload (same field order as [`ParetoPoint`]).
#[derive(Clone, Copy)]
struct ParetoEps {
    net_pnl_paisa: i64,
    win_rate: f64,
    max_dd_pct: f64,
    losing_trades: i64,
}

/// The within-ε clause of ε-dominance: on EVERY axis, `x` is
/// better-or-equal-within-ε than `y`.
///
/// MAX axes: `x + ε >= y`; MIN axes: `x - ε <= y`. Int axes use i128 so the
/// `+ ε` shift can never overflow i64.
fn dominates_within(x: &ParetoPoint, y: &ParetoPoint, eps: &ParetoEps) -> bool {
    (x.net_pnl_paisa as i128) + (eps.net_pnl_paisa as i128) >= (y.net_pnl_paisa as i128)
        && x.win_rate + eps.win_rate >= y.win_rate
        && x.max_dd_pct - eps.max_dd_pct <= y.max_dd_pct
        && (x.losing_trades as i128) - (eps.losing_trades as i128) <= (y.losing_trades as i128)
}

/// The strict clause of ε-dominance: on AT LEAST ONE axis, `x` beats `y` by
/// MORE than that axis's ε.
///
/// MAX axes: `x > y + ε`; MIN axes: `x < y - ε` (the pinned expression forms).
fn dominates_strict(x: &ParetoPoint, y: &ParetoPoint, eps: &ParetoEps) -> bool {
    (x.net_pnl_paisa as i128) > (y.net_pnl_paisa as i128) + (eps.net_pnl_paisa as i128)
        || x.win_rate > y.win_rate + eps.win_rate
        || x.max_dd_pct < y.max_dd_pct - eps.max_dd_pct
        || (x.losing_trades as i128) < (y.losing_trades as i128) - (eps.losing_trades as i128)
}

/// `x` ε-dominates `y` iff BOTH clauses hold.
///
/// At ε = 0 this reduces to CLASSICAL Pareto dominance (better-or-equal on
/// every axis, strictly better on one). The relation is ANTISYMMETRIC (a
/// strictly-beyond-ε win on any axis falsifies the reverse within clause on
/// that axis) but NOT transitive and NOT proven acyclic for ε > 0 — the
/// frontier is computed by full pairwise exclusion, and an empty frontier on
/// a non-empty pool is tolerated by the consumer (the persist union always
/// includes the composite top-K, so persistence can never go empty).
fn epsilon_dominates(x: &ParetoPoint, y: &ParetoPoint, eps: &ParetoEps) -> bool {
    dominates_within(x, y, eps) && dominates_strict(x, y, eps)
}

fn checked_finite(name: &str, values: &[f64]) -> Result<(), String> {
    for (i, v) in values.iter().enumerate() {
        if !v.is_finite() {
            return Err(format!(
                "rank_select: {name}[{i}] = {v} is not finite — the ResultRow \
                 construction law guarantees finite metrics; refusing"
            ));
        }
    }
    Ok(())
}

fn checked_eps_f64(name: &str, eps: f64) -> Result<(), String> {
    if !eps.is_finite() || eps < 0.0 {
        return Err(format!(
            "rank_select: epsilon {name} = {eps} must be finite and >= 0"
        ));
    }
    Ok(())
}

fn checked_eps_i64(name: &str, eps: i64) -> Result<(), String> {
    if eps < 0 {
        return Err(format!("rank_select: epsilon {name} = {eps} must be >= 0"));
    }
    Ok(())
}

fn checked_same_len(lens: &[(&str, usize)]) -> Result<usize, String> {
    let n = lens[0].1;
    for &(name, len) in lens {
        if len != n {
            return Err(format!(
                "rank_select: column length mismatch — {name} has {len} rows, \
                 {} has {n}",
                lens[0].0
            ));
        }
    }
    Ok(n)
}

/// Pure core of [`rank_pareto_frontier`] (unit/proptest target).
pub(crate) fn pareto_frontier_checked(
    net_pnl_paisa: &[i64],
    win_rate: &[f64],
    max_dd_pct: &[f64],
    losing_trades: &[i64],
    eps_net_pnl_paisa: i64,
    eps_win_rate: f64,
    eps_max_dd_pct: f64,
    eps_losing_trades: i64,
) -> Result<Vec<u64>, String> {
    let n = checked_same_len(&[
        ("net_pnl_paisa", net_pnl_paisa.len()),
        ("win_rate", win_rate.len()),
        ("max_dd_pct", max_dd_pct.len()),
        ("losing_trades", losing_trades.len()),
    ])?;
    checked_finite("win_rate", win_rate)?;
    checked_finite("max_dd_pct", max_dd_pct)?;
    checked_eps_i64("eps_net_pnl_paisa", eps_net_pnl_paisa)?;
    checked_eps_f64("eps_win_rate", eps_win_rate)?;
    checked_eps_f64("eps_max_dd_pct", eps_max_dd_pct)?;
    checked_eps_i64("eps_losing_trades", eps_losing_trades)?;
    let eps = ParetoEps {
        net_pnl_paisa: eps_net_pnl_paisa,
        win_rate: eps_win_rate,
        max_dd_pct: eps_max_dd_pct,
        losing_trades: eps_losing_trades,
    };
    let points: Vec<ParetoPoint> = (0..n)
        .map(|i| ParetoPoint {
            net_pnl_paisa: net_pnl_paisa[i],
            win_rate: win_rate[i],
            max_dd_pct: max_dd_pct[i],
            losing_trades: losing_trades[i],
        })
        .collect();
    // Full pairwise exclusion with early exit — O(n²) worst case, disclosed.
    // (ε-dominance is not transitive, so no frontier-only pruning is sound.)
    let mut kept = Vec::new();
    for (y_idx, y) in points.iter().enumerate() {
        let dominated = points
            .iter()
            .enumerate()
            .any(|(x_idx, x)| x_idx != y_idx && epsilon_dominates(x, y, &eps));
        if !dominated {
            kept.push(y_idx as u64);
        }
    }
    Ok(kept)
}

/// One quota-heap entry: the axis primary key + the shared
/// `(−composite, mask)` tie tail. `BinaryHeap` is a MAX-heap, so `Ordering`
/// here means "ranks WORSE than" — the heap root is the worst kept entry.
struct QuotaEntry {
    /// Axis primary, already oriented so SMALLER-IS-BETTER (ASC axes raw,
    /// DESC axes negated — exact IEEE negation for finite floats).
    primary: f64,
    /// `−composite` (smaller is better — higher composite wins ties).
    neg_composite: f64,
    /// The u128 mask (smaller wins deep ties — the `(−composite, mask)` law).
    mask: u128,
    index: usize,
}

impl QuotaEntry {
    fn key(&self) -> (f64, f64, u128) {
        (self.primary, self.neg_composite, self.mask)
    }
}

impl PartialEq for QuotaEntry {
    fn eq(&self, other: &Self) -> bool {
        self.key() == other.key()
    }
}
impl Eq for QuotaEntry {}
impl PartialOrd for QuotaEntry {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}
impl Ord for QuotaEntry {
    fn cmp(&self, other: &Self) -> Ordering {
        // All floats are validated finite, so partial_cmp is total here; a
        // defensive Equal keeps the comparator total even off-domain.
        self.key()
            .partial_cmp(&other.key())
            .unwrap_or(Ordering::Equal)
    }
}

/// Keep the best-M entries by the strict total order `(primary,
/// −composite, mask)` ascending; return their indices best-first.
fn bounded_topm(entries: impl Iterator<Item = QuotaEntry>, m: usize) -> Vec<u64> {
    if m == 0 {
        return Vec::new();
    }
    let mut heap: BinaryHeap<QuotaEntry> = BinaryHeap::with_capacity(m + 1);
    for entry in entries {
        if heap.len() < m {
            heap.push(entry);
        } else if let Some(worst) = heap.peek() {
            if entry.cmp(worst) == Ordering::Less {
                heap.pop();
                heap.push(entry);
            }
        }
    }
    let mut kept: Vec<QuotaEntry> = heap.into_vec();
    kept.sort();
    kept.into_iter().map(|e| e.index as u64).collect()
}

/// Pure core of [`rank_axis_topm`] (unit/proptest target).
///
/// Returns the five per-axis best-M index lists in the FIXED quota-axis
/// order `(composite, max_dd_pct, losing_trades, win_rate,
/// expectancy_paisa)`, each ordered best-first under its axis law.
#[allow(clippy::too_many_arguments)]
pub(crate) fn axis_topm_checked(
    m: usize,
    composite: &[f64],
    mask_lo: &[u64],
    mask_hi: &[u64],
    max_dd_pct: &[f64],
    losing_trades: &[i64],
    win_rate: &[f64],
    expectancy_paisa: &[i64],
) -> Result<(Vec<u64>, Vec<u64>, Vec<u64>, Vec<u64>, Vec<u64>), String> {
    let n = checked_same_len(&[
        ("composite", composite.len()),
        ("mask_lo", mask_lo.len()),
        ("mask_hi", mask_hi.len()),
        ("max_dd_pct", max_dd_pct.len()),
        ("losing_trades", losing_trades.len()),
        ("win_rate", win_rate.len()),
        ("expectancy_paisa", expectancy_paisa.len()),
    ])?;
    checked_finite("composite", composite)?;
    checked_finite("max_dd_pct", max_dd_pct)?;
    checked_finite("win_rate", win_rate)?;
    let mask = |i: usize| ((mask_hi[i] as u128) << 64) | (mask_lo[i] as u128);
    let entry = |i: usize, primary: f64| QuotaEntry {
        primary,
        neg_composite: -composite[i],
        mask: mask(i),
        index: i,
    };
    // composite axis: the existing law (−composite, mask) — primary IS the
    // tie tail's first member, kept explicit so all five axes share one shape.
    let by_composite = bounded_topm((0..n).map(|i| entry(i, -composite[i])), m);
    let by_dd = bounded_topm((0..n).map(|i| entry(i, max_dd_pct[i])), m);
    // int axes ride the f64 primary exactly (|losing_trades| and paisa fit
    // f64's 2^53 integer envelope in practice; exactness is not load-bearing
    // for ties — the tie tail decides — but the ORDER of distinct ints must
    // be preserved, so refuse values outside the exact envelope).
    for (name, values) in [
        ("losing_trades", losing_trades),
        ("expectancy_paisa", expectancy_paisa),
    ] {
        for (i, v) in values.iter().enumerate() {
            if v.abs() > (1_i64 << 53) {
                return Err(format!(
                    "rank_select: {name}[{i}] = {v} outside the f64-exact \
                     integer envelope (|v| <= 2^53) — refusing"
                ));
            }
        }
    }
    let by_losing = bounded_topm((0..n).map(|i| entry(i, losing_trades[i] as f64)), m);
    let by_win_rate = bounded_topm((0..n).map(|i| entry(i, -win_rate[i])), m);
    let by_expectancy = bounded_topm((0..n).map(|i| entry(i, -(expectancy_paisa[i] as f64))), m);
    Ok((by_composite, by_dd, by_losing, by_win_rate, by_expectancy))
}

/// Pure core of [`rank_dd_band_indices`] (unit/proptest target).
pub(crate) fn dd_band_indices_checked(dd: &[f64], band_pct: f64) -> Result<Vec<i64>, String> {
    if !band_pct.is_finite() || band_pct <= 0.0 {
        return Err(format!(
            "rank_select: band_pct = {band_pct} must be finite and > 0"
        ));
    }
    checked_finite("max_dd_pct", dd)?;
    let mut bands = Vec::with_capacity(dd.len());
    for (i, &value) in dd.iter().enumerate() {
        let band = (value / band_pct).floor();
        // i64-exactness guard: a quotient outside ±2^53 cannot round-trip the
        // Python `math.floor` int exactly; refuse rather than diverge.
        if band.abs() > (1_i64 << 53) as f64 {
            return Err(format!(
                "rank_select: dd[{i}] / band_pct = {band} outside the \
                 f64-exact integer envelope — refusing"
            ));
        }
        bands.push(band as i64);
    }
    Ok(bands)
}

/// The additive-ε Pareto frontier over `{net_pnl MAX, win_rate MAX,
/// max_dd_pct MIN, losing_trades MIN}` — ascending kept indices.
///
/// See the module docs for the dominance definition + the honest O(n²)
/// worst-case complexity. Raises `ValueError` on a length mismatch, a
/// non-finite float, or a negative/non-finite ε.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn rank_pareto_frontier(
    net_pnl_paisa: Vec<i64>,
    win_rate: Vec<f64>,
    max_dd_pct: Vec<f64>,
    losing_trades: Vec<i64>,
    eps_net_pnl_paisa: i64,
    eps_win_rate: f64,
    eps_max_dd_pct: f64,
    eps_losing_trades: i64,
) -> PyResult<Vec<u64>> {
    pareto_frontier_checked(
        &net_pnl_paisa,
        &win_rate,
        &max_dd_pct,
        &losing_trades,
        eps_net_pnl_paisa,
        eps_win_rate,
        eps_max_dd_pct,
        eps_losing_trades,
    )
    .map_err(PyValueError::new_err)
}

/// The per-axis bounded top-M select over the five quota axes — five
/// best-first index lists in `(composite, max_dd_pct, losing_trades,
/// win_rate, expectancy_paisa)` order.
///
/// `O(A·n·log M)` time, `O(A·M)` space. Raises `ValueError` on a length
/// mismatch, a non-finite float, or an int axis value outside the f64-exact
/// integer envelope (|v| ≤ 2^53).
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn rank_axis_topm(
    m: usize,
    composite: Vec<f64>,
    mask_lo: Vec<u64>,
    mask_hi: Vec<u64>,
    max_dd_pct: Vec<f64>,
    losing_trades: Vec<i64>,
    win_rate: Vec<f64>,
    expectancy_paisa: Vec<i64>,
) -> PyResult<(Vec<u64>, Vec<u64>, Vec<u64>, Vec<u64>, Vec<u64>)> {
    axis_topm_checked(
        m,
        &composite,
        &mask_lo,
        &mask_hi,
        &max_dd_pct,
        &losing_trades,
        &win_rate,
        &expectancy_paisa,
    )
    .map_err(PyValueError::new_err)
}

/// The batch DD ε-band quantizer: `floor(dd / band_pct)` per element —
/// bit-identical to Python's `math.floor(dd / band)` on finite inputs
/// (same IEEE division, same floor; a DD exactly on a band edge joins the
/// UPPER band index via floor — the documented boundary rule).
#[pyfunction]
pub fn rank_dd_band_indices(max_dd_pct: Vec<f64>, band_pct: f64) -> PyResult<Vec<i64>> {
    dd_band_indices_checked(&max_dd_pct, band_pct).map_err(PyValueError::new_err)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Naive quadratic oracle for the frontier (independent re-derivation).
    fn naive_frontier(points: &[(i64, f64, f64, i64)], eps: (i64, f64, f64, i64)) -> Vec<u64> {
        let dominated = |x: &(i64, f64, f64, i64), y: &(i64, f64, f64, i64)| -> bool {
            let within = (x.0 as i128) + (eps.0 as i128) >= (y.0 as i128)
                && x.1 + eps.1 >= y.1
                && x.2 - eps.2 <= y.2
                && (x.3 as i128) - (eps.3 as i128) <= (y.3 as i128);
            let strict = (x.0 as i128) > (y.0 as i128) + (eps.0 as i128)
                || x.1 > y.1 + eps.1
                || x.2 < y.2 - eps.2
                || (x.3 as i128) < (y.3 as i128) - (eps.3 as i128);
            within && strict
        };
        (0..points.len())
            .filter(|&yi| {
                !(0..points.len()).any(|xi| xi != yi && dominated(&points[xi], &points[yi]))
            })
            .map(|i| i as u64)
            .collect()
    }

    fn run_frontier(points: &[(i64, f64, f64, i64)], eps: (i64, f64, f64, i64)) -> Vec<u64> {
        pareto_frontier_checked(
            &points.iter().map(|p| p.0).collect::<Vec<_>>(),
            &points.iter().map(|p| p.1).collect::<Vec<_>>(),
            &points.iter().map(|p| p.2).collect::<Vec<_>>(),
            &points.iter().map(|p| p.3).collect::<Vec<_>>(),
            eps.0,
            eps.1,
            eps.2,
            eps.3,
        )
        .unwrap()
    }

    #[test]
    fn frontier_empty_input_is_empty() {
        assert_eq!(run_frontier(&[], (0, 0.0, 0.0, 0)), Vec::<u64>::new());
    }

    #[test]
    fn frontier_single_point_kept() {
        assert_eq!(
            run_frontier(&[(100, 0.5, 5.0, 3)], (0, 0.0, 0.0, 0)),
            vec![0]
        );
    }

    #[test]
    fn frontier_classical_dominance_at_eps_zero() {
        // Point 1 is strictly better on every axis than point 0.
        let pts = [(100, 0.5, 10.0, 5), (200, 0.6, 5.0, 2)];
        assert_eq!(run_frontier(&pts, (0, 0.0, 0.0, 0)), vec![1]);
    }

    #[test]
    fn frontier_incomparable_points_both_kept() {
        // 0 wins on pnl, 1 wins on dd — classical frontier keeps both.
        let pts = [(200, 0.5, 10.0, 5), (100, 0.5, 5.0, 5)];
        assert_eq!(run_frontier(&pts, (0, 0.0, 0.0, 0)), vec![0, 1]);
    }

    #[test]
    fn frontier_equal_points_all_kept_at_eps_zero() {
        // Identical points: within holds both ways, strict never — no
        // domination, all kept (classical reflexive-free law).
        let pts = [(100, 0.5, 5.0, 3), (100, 0.5, 5.0, 3), (100, 0.5, 5.0, 3)];
        assert_eq!(run_frontier(&pts, (0, 0.0, 0.0, 0)), vec![0, 1, 2]);
    }

    #[test]
    fn frontier_epsilon_absorbs_small_axis_win() {
        // 0 beats 1 hugely on pnl; 1 beats 0 by a tiny dd edge. At ε_dd=0.25
        // the tiny dd edge is inside the tolerance, so 0 ε-dominates 1.
        let pts = [(1_000_000, 0.5, 5.10, 3), (100, 0.5, 5.00, 3)];
        assert_eq!(run_frontier(&pts, (0, 0.0, 0.0, 0)), vec![0, 1]);
        assert_eq!(run_frontier(&pts, (1_000, 0.005, 0.25, 0)), vec![0]);
    }

    #[test]
    fn frontier_epsilon_can_disarm_strict_clause() {
        // 1D-style: 0 beats 1 by less than ε on the only differing axis —
        // no strict-beyond-ε win, both kept (the mission's 1D example).
        let pts = [(1_500, 0.5, 5.0, 3), (1_000, 0.5, 5.0, 3)];
        assert_eq!(run_frontier(&pts, (0, 0.0, 0.0, 0)), vec![0]);
        assert_eq!(run_frontier(&pts, (1_000, 0.0, 0.0, 0)), vec![0, 1]);
    }

    #[test]
    fn frontier_extreme_i64_values_no_overflow() {
        let pts = [(i64::MAX, 1.0, 0.0, 0), (i64::MIN, 0.0, 100.0, i64::MAX)];
        // i128 widening: MAX + eps must not overflow (point 0 dominates
        // point 1 on every axis regardless of the huge ε — hand-derived:
        // within holds trivially; strict holds on pnl since
        // i64::MIN + i64::MAX = -1 < i64::MAX).
        assert_eq!(run_frontier(&pts, (i64::MAX, 0.0, 0.0, i64::MAX)), vec![0]);
        assert_eq!(run_frontier(&pts, (0, 0.0, 0.0, 0)), vec![0]);
    }

    #[test]
    fn frontier_matches_naive_oracle_on_grid() {
        // Deterministic pseudo-random battery vs the independent oracle.
        let mut points = Vec::new();
        let mut state: u64 = 0x9E3779B97F4A7C15;
        for _ in 0..60 {
            state = state
                .wrapping_mul(6364136223846793005)
                .wrapping_add(1442695040888963407);
            let pnl = ((state >> 33) as i64 % 1000) * 100;
            state = state
                .wrapping_mul(6364136223846793005)
                .wrapping_add(1442695040888963407);
            let wr = ((state >> 40) % 1000) as f64 / 1000.0;
            state = state
                .wrapping_mul(6364136223846793005)
                .wrapping_add(1442695040888963407);
            let dd = ((state >> 40) % 3000) as f64 / 100.0;
            state = state
                .wrapping_mul(6364136223846793005)
                .wrapping_add(1442695040888963407);
            let lt = ((state >> 45) % 50) as i64;
            points.push((pnl, wr, dd, lt));
        }
        for eps in [
            (0, 0.0, 0.0, 0),
            (1_000, 0.005, 0.25, 0),
            (50_000, 0.05, 2.0, 3),
        ] {
            assert_eq!(run_frontier(&points, eps), naive_frontier(&points, eps));
        }
    }

    #[test]
    fn frontier_rejects_bad_inputs() {
        assert!(pareto_frontier_checked(&[1], &[0.5, 0.6], &[1.0], &[1], 0, 0.0, 0.0, 0).is_err());
        assert!(pareto_frontier_checked(&[1], &[f64::NAN], &[1.0], &[1], 0, 0.0, 0.0, 0).is_err());
        assert!(
            pareto_frontier_checked(&[1], &[0.5], &[f64::INFINITY], &[1], 0, 0.0, 0.0, 0).is_err()
        );
        assert!(pareto_frontier_checked(&[1], &[0.5], &[1.0], &[1], -1, 0.0, 0.0, 0).is_err());
        assert!(pareto_frontier_checked(&[1], &[0.5], &[1.0], &[1], 0, f64::NAN, 0.0, 0).is_err());
        assert!(pareto_frontier_checked(&[1], &[0.5], &[1.0], &[1], 0, 0.0, -0.5, 0).is_err());
        assert!(pareto_frontier_checked(&[1], &[0.5], &[1.0], &[1], 0, 0.0, 0.0, -1).is_err());
    }

    fn run_topm(
        m: usize,
        rows: &[(f64, u128, f64, i64, f64, i64)],
    ) -> (Vec<u64>, Vec<u64>, Vec<u64>, Vec<u64>, Vec<u64>) {
        axis_topm_checked(
            m,
            &rows.iter().map(|r| r.0).collect::<Vec<_>>(),
            &rows
                .iter()
                .map(|r| (r.1 & u64::MAX as u128) as u64)
                .collect::<Vec<_>>(),
            &rows.iter().map(|r| (r.1 >> 64) as u64).collect::<Vec<_>>(),
            &rows.iter().map(|r| r.2).collect::<Vec<_>>(),
            &rows.iter().map(|r| r.3).collect::<Vec<_>>(),
            &rows.iter().map(|r| r.4).collect::<Vec<_>>(),
            &rows.iter().map(|r| r.5).collect::<Vec<_>>(),
        )
        .unwrap()
    }

    #[test]
    fn topm_orders_each_axis_by_its_law() {
        // rows: (composite, mask, dd, losing, win_rate, expectancy)
        let rows = [
            (1.0, 1u128, 10.0, 5, 0.50, 100),
            (2.0, 2u128, 20.0, 3, 0.60, 300),
            (3.0, 3u128, 5.0, 8, 0.40, 200),
        ];
        let (comp, dd, losing, wr, exp) = run_topm(2, &rows);
        assert_eq!(comp, vec![2, 1]); // composite DESC
        assert_eq!(dd, vec![2, 0]); // dd ASC
        assert_eq!(losing, vec![1, 0]); // losing ASC
        assert_eq!(wr, vec![1, 0]); // win_rate DESC
        assert_eq!(exp, vec![1, 2]); // expectancy DESC
    }

    #[test]
    fn topm_tie_break_composite_then_mask() {
        // Equal dd: higher composite wins; equal composite too: smaller mask.
        let rows = [
            (1.0, 7u128, 5.0, 1, 0.5, 10),
            (2.0, 9u128, 5.0, 1, 0.5, 10),
            (2.0, 8u128, 5.0, 1, 0.5, 10),
        ];
        let (_, dd, ..) = run_topm(3, &rows);
        assert_eq!(dd, vec![2, 1, 0]);
    }

    #[test]
    fn topm_u128_mask_reassembly_orders_high_limbs() {
        // Two masks differing only in the HIGH limb: the smaller u128 wins.
        let low = 5u128;
        let high = (1u128 << 90) | 5u128;
        let rows = [(1.0, high, 5.0, 1, 0.5, 10), (1.0, low, 5.0, 1, 0.5, 10)];
        let (_, dd, ..) = run_topm(2, &rows);
        assert_eq!(dd, vec![1, 0]);
    }

    #[test]
    fn topm_m_zero_and_m_over_n() {
        let rows = [(1.0, 1u128, 5.0, 1, 0.5, 10)];
        let (comp, ..) = run_topm(0, &rows);
        assert_eq!(comp, Vec::<u64>::new());
        let (comp, ..) = run_topm(10, &rows);
        assert_eq!(comp, vec![0]);
    }

    #[test]
    fn topm_rejects_out_of_envelope_ints() {
        let big = (1_i64 << 53) + 1;
        assert!(axis_topm_checked(1, &[1.0], &[1], &[0], &[5.0], &[big], &[0.5], &[0]).is_err());
        assert!(axis_topm_checked(1, &[1.0], &[1], &[0], &[5.0], &[0], &[0.5], &[-big]).is_err());
        assert!(axis_topm_checked(1, &[f64::NAN], &[1], &[0], &[5.0], &[0], &[0.5], &[0]).is_err());
        assert!(axis_topm_checked(1, &[1.0], &[1, 2], &[0], &[5.0], &[0], &[0.5], &[0]).is_err());
    }

    #[test]
    fn dd_band_floor_boundaries() {
        let bands =
            dd_band_indices_checked(&[0.0, 0.49, 0.5, 0.99, 1.0, 4.99, 5.0, 5.01], 0.5).unwrap();
        assert_eq!(bands, vec![0, 0, 1, 1, 2, 9, 10, 10]);
    }

    #[test]
    fn dd_band_negative_dd_floors_down() {
        // Defensive: dd is validated >= 0 upstream, but floor is well-defined.
        assert_eq!(dd_band_indices_checked(&[-0.1], 0.5).unwrap(), vec![-1]);
    }

    #[test]
    fn dd_band_rejects_bad_inputs() {
        assert!(dd_band_indices_checked(&[1.0], 0.0).is_err());
        assert!(dd_band_indices_checked(&[1.0], -0.5).is_err());
        assert!(dd_band_indices_checked(&[1.0], f64::NAN).is_err());
        assert!(dd_band_indices_checked(&[f64::NAN], 0.5).is_err());
        assert!(dd_band_indices_checked(&[1e300], 0.5).is_err());
    }
}
