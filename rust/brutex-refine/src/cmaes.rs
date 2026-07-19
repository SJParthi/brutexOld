//! One CMA-ES run — the (μ/μ_w, λ) covariance-matrix-adaptation evolution
//! strategy of Hansen's tutorial (arXiv:1604.00772), minimizing over the
//! unit box `[0, 1]ⁿ` with projection repair.
//!
//! `DEC-RUST-STAGE3-REFINE-001` — the deterministic in-crate port replacing
//! pycma (greenfield ruling: no pycma bit-match is claimed; correctness =
//! the standard-objective convergence suite + the algebraic invariants +
//! this crate's own frozen golden). Laws:
//!
//! * **Determinism** — one [`NormalSource`] stream seeded from the caller's
//!   u64; sampling order, selection sort (value then index) and every update
//!   are single-threaded and branch-deterministic. Same seed + same told
//!   values ⇒ bit-identical trajectory on one platform (cross-platform
//!   bit-identity is NOT claimed — libm `ln`/`exp`/`cos` are
//!   platform-rounded; the same honest envelope the pycma path carried).
//! * **Bounds** — every sampled proposal is PROJECTED into `[0, 1]ⁿ` before
//!   it is exposed; updates use the repaired points (standard projection
//!   repair — consistent with the downstream grid clamp).
//! * **ask/tell protocol** — `ask(max_pop)` samples `min(λ, max_pop)`
//!   candidates; `tell(values)` (minimization) applies the mean/step/
//!   covariance updates. A TRUNCATED generation (`max_pop < λ` — the
//!   caller's eval budget floor) is evaluated but applies NO update: the
//!   caller ends the run right after, so a partial-population update would
//!   be dead weight with a nondeterministic-feeling half-law.

use crate::linalg::{idx, sym_eigen};
use crate::rng::NormalSource;

/// Numerical floors/ceilings (documented stop laws, not tunables).
const TOL_FUN: f64 = 1e-12;
const TOL_X_REL: f64 = 1e-12;
const TOL_CONDITION: f64 = 1e14;
const SIGMA_DIVERGE: f64 = 1e7;

/// Why a run stopped (beyond the caller's own eval budget).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StopReason {
    /// Best-fitness range over the history window fell below `TOL_FUN`.
    TolFun,
    /// The search distribution collapsed below `TOL_X_REL · σ0`.
    TolX,
    /// Covariance condition number exceeded `TOL_CONDITION`.
    TolConditionCov,
    /// Step-size × largest axis exploded past `SIGMA_DIVERGE` (divergence).
    SigmaDiverged,
}

/// Strategy constants derived from (n, λ) — Hansen's default parametrization.
#[derive(Debug, Clone)]
struct Params {
    n: usize,
    lambda: usize,
    weights: Vec<f64>, // positive recombination weights, len = mu
    mu_eff: f64,
    c_sigma: f64,
    d_sigma: f64,
    c_c: f64,
    c_1: f64,
    c_mu: f64,
    chi_n: f64, // E‖N(0, I)‖
}

impl Params {
    fn new(n: usize, lambda: usize) -> Self {
        let nf = n as f64;
        let mu = lambda / 2;
        let mut weights: Vec<f64> = (0..mu)
            .map(|i| ((lambda as f64 + 1.0) / 2.0).ln() - ((i + 1) as f64).ln())
            .collect();
        let sum: f64 = weights.iter().sum();
        for w in &mut weights {
            *w /= sum;
        }
        let mu_eff = 1.0 / weights.iter().map(|w| w * w).sum::<f64>();
        let c_sigma = (mu_eff + 2.0) / (nf + mu_eff + 5.0);
        let d_sigma = 1.0 + 2.0 * (((mu_eff - 1.0) / (nf + 1.0)).sqrt() - 1.0).max(0.0) + c_sigma;
        let c_c = (4.0 + mu_eff / nf) / (nf + 4.0 + 2.0 * mu_eff / nf);
        let c_1 = 2.0 / ((nf + 1.3) * (nf + 1.3) + mu_eff);
        let c_mu = (2.0 * (mu_eff - 2.0 + 1.0 / mu_eff) / ((nf + 2.0) * (nf + 2.0) + mu_eff))
            .min(1.0 - c_1);
        let chi_n = nf.sqrt() * (1.0 - 1.0 / (4.0 * nf) + 1.0 / (21.0 * nf * nf));
        Self {
            n,
            lambda,
            weights,
            mu_eff,
            c_sigma,
            d_sigma,
            c_c,
            c_1,
            c_mu,
            chi_n,
        }
    }
}

/// The default population size λ = 4 + ⌊3 ln n⌋.
pub fn default_lambda(n: usize) -> usize {
    4 + (3.0 * (n as f64).ln()).floor() as usize
}

/// One CMA-ES run (ask/tell driven, minimization, unit-box repair).
#[derive(Debug, Clone)]
pub struct Cmaes {
    params: Params,
    normals: NormalSource,
    mean: Vec<f64>,
    sigma: f64,
    sigma0: f64,
    cov: Vec<f64>,     // n×n row-major, symmetric
    p_sigma: Vec<f64>, // step-size evolution path
    p_c: Vec<f64>,     // covariance evolution path
    generation: u64,
    // Sampled state pending a tell: repaired xs + their y = (x − m)/σ.
    pending: Option<Vec<Vec<f64>>>,
    // Best value per completed generation (tolfun history window).
    best_history: Vec<f64>,
    stop: Option<StopReason>,
}

impl Cmaes {
    /// Start a run at *x0* (already inside the unit box) with step *sigma0*.
    pub fn new(x0: &[f64], sigma0: f64, seed: u64, lambda: usize) -> Self {
        let n = x0.len();
        assert!(n >= 1, "dimension must be >= 1");
        assert!(lambda >= 2, "population must be >= 2");
        assert!(sigma0 > 0.0, "sigma0 must be > 0");
        let mut cov = vec![0.0; n * n];
        for i in 0..n {
            cov[idx(n, i, i)] = 1.0;
        }
        Self {
            params: Params::new(n, lambda),
            normals: NormalSource::new(seed),
            mean: x0.to_vec(),
            sigma: sigma0,
            sigma0,
            cov,
            p_sigma: vec![0.0; n],
            p_c: vec![0.0; n],
            generation: 0,
            pending: None,
            best_history: Vec::new(),
            stop: None,
        }
    }

    /// The run's population size λ.
    pub fn lambda(&self) -> usize {
        self.params.lambda
    }

    /// The stop verdict, if any (never set before the first `tell`).
    pub fn stopped(&self) -> Option<StopReason> {
        self.stop
    }

    /// Sample `min(λ, max_pop)` candidates, projected into `[0, 1]ⁿ`.
    ///
    /// # Panics
    /// If a previous `ask` has not been answered by `tell` (protocol misuse —
    /// the FFI edge maps this to a `ValueError` before it can panic).
    pub fn ask(&mut self, max_pop: usize) -> Vec<Vec<f64>> {
        assert!(self.pending.is_none(), "ask() called with a pending tell()");
        assert!(max_pop >= 1, "max_pop must be >= 1");
        let n = self.params.n;
        let pop = self.params.lambda.min(max_pop);
        let (eigvals, eigvecs) = sym_eigen(&self.cov, n);
        // D floor: keep sqrt well-defined under numerical noise.
        let d: Vec<f64> = eigvals.iter().map(|&v| v.max(1e-30).sqrt()).collect();
        let mut xs = Vec::with_capacity(pop);
        for _ in 0..pop {
            // y = B · diag(d) · z
            let z: Vec<f64> = (0..n).map(|_| self.normals.next_normal()).collect();
            let mut y = vec![0.0; n];
            for (i, y_i) in y.iter_mut().enumerate() {
                let mut acc = 0.0;
                for (k, z_k) in z.iter().enumerate() {
                    acc += eigvecs[idx(n, i, k)] * d[k] * z_k;
                }
                *y_i = acc;
            }
            // x = m + σ·y, projected into the unit box (repair).
            let x: Vec<f64> = (0..n)
                .map(|i| (self.mean[i] + self.sigma * y[i]).clamp(0.0, 1.0))
                .collect();
            xs.push(x);
        }
        self.pending = Some(xs.clone());
        xs
    }

    /// Report the objective values (minimization) for the last `ask`.
    ///
    /// A truncated generation (fewer candidates than λ) records fitness
    /// history but applies NO distribution update (see the module docs).
    ///
    /// # Panics
    /// If no `ask` is pending or the value count mismatches (the FFI edge
    /// maps both to `ValueError` first).
    pub fn tell(&mut self, values: &[f64]) {
        let xs = self.pending.take().expect("tell() without a pending ask()");
        assert_eq!(xs.len(), values.len(), "value count mismatch");
        let n = self.params.n;
        let p = &self.params;

        let gen_best = values.iter().cloned().fold(f64::INFINITY, f64::min);
        self.best_history.push(gen_best);
        let hist_len = 10 + (30 * n) / p.lambda + 1;
        if self.best_history.len() > hist_len {
            self.best_history.remove(0);
        }

        if xs.len() < p.lambda {
            // Truncated final generation — evaluated, recorded, no update.
            self.generation += 1;
            return;
        }

        // Selection: indices sorted by (value, index) ascending — the index
        // tie-break makes flat-fitness generations fully deterministic.
        let mut order: Vec<usize> = (0..values.len()).collect();
        order.sort_by(|&a, &b| {
            values[a]
                .partial_cmp(&values[b])
                .unwrap_or(std::cmp::Ordering::Equal)
                .then(a.cmp(&b))
        });

        let old_mean = self.mean.clone();
        // m ← Σ w_i x_(i)
        let mut new_mean = vec![0.0; n];
        for (rank, &w) in p.weights.iter().enumerate() {
            let x = &xs[order[rank]];
            for (i, m_i) in new_mean.iter_mut().enumerate() {
                *m_i += w * x[i];
            }
        }
        // y_w = (m_new − m_old)/σ; per-candidate y_(i) = (x_(i) − m_old)/σ.
        let y_w: Vec<f64> = (0..n)
            .map(|i| (new_mean[i] - old_mean[i]) / self.sigma)
            .collect();

        // C^(−1/2) · y_w via the current eigendecomposition.
        let (eigvals, eigvecs) = sym_eigen(&self.cov, n);
        let d_inv: Vec<f64> = eigvals.iter().map(|&v| 1.0 / v.max(1e-30).sqrt()).collect();
        let mut c_inv_sqrt_yw = vec![0.0; n];
        for (i, out) in c_inv_sqrt_yw.iter_mut().enumerate() {
            let mut acc = 0.0;
            for k in 0..n {
                // B · D⁻¹ · Bᵀ · y_w
                let mut bty = 0.0;
                for (j, y_j) in y_w.iter().enumerate() {
                    bty += eigvecs[idx(n, j, k)] * y_j;
                }
                acc += eigvecs[idx(n, i, k)] * d_inv[k] * bty;
            }
            *out = acc;
        }

        // Step-size path + h_σ gate.
        let cs = p.c_sigma;
        let path_scale = (cs * (2.0 - cs) * p.mu_eff).sqrt();
        for (i, ps) in self.p_sigma.iter_mut().enumerate() {
            *ps = (1.0 - cs) * *ps + path_scale * c_inv_sqrt_yw[i];
        }
        let ps_norm = self.p_sigma.iter().map(|v| v * v).sum::<f64>().sqrt();
        let decay = 1.0 - (1.0 - cs).powi(2 * (self.generation as i32 + 1));
        let h_sigma = if ps_norm / decay.max(f64::MIN_POSITIVE).sqrt()
            < (1.4 + 2.0 / (n as f64 + 1.0)) * p.chi_n
        {
            1.0
        } else {
            0.0
        };

        // Covariance path.
        let cc = p.c_c;
        let pc_scale = (cc * (2.0 - cc) * p.mu_eff).sqrt();
        for (i, pc) in self.p_c.iter_mut().enumerate() {
            *pc = (1.0 - cc) * *pc + h_sigma * pc_scale * y_w[i];
        }

        // Covariance update: rank-1 + rank-μ (+ the h_σ variance correction).
        let c1a = p.c_1 * (1.0 - (1.0 - h_sigma * h_sigma) * cc * (2.0 - cc));
        for i in 0..n {
            for j in 0..n {
                let mut rank_mu = 0.0;
                for (rank, &w) in p.weights.iter().enumerate() {
                    let x = &xs[order[rank]];
                    let yi = (x[i] - old_mean[i]) / self.sigma;
                    let yj = (x[j] - old_mean[j]) / self.sigma;
                    rank_mu += w * yi * yj;
                }
                let c_ij = self.cov[idx(n, i, j)];
                self.cov[idx(n, i, j)] = (1.0 - c1a - p.c_mu) * c_ij
                    + p.c_1 * self.p_c[i] * self.p_c[j]
                    + p.c_mu * rank_mu;
            }
        }
        // Exact symmetrization against drift.
        for i in 0..n {
            for j in (i + 1)..n {
                let avg = 0.5 * (self.cov[idx(n, i, j)] + self.cov[idx(n, j, i)]);
                self.cov[idx(n, i, j)] = avg;
                self.cov[idx(n, j, i)] = avg;
            }
        }

        // Step-size adaptation.
        self.sigma *= ((cs / p.d_sigma) * (ps_norm / p.chi_n - 1.0)).exp();
        self.mean = new_mean;
        self.generation += 1;

        // Stop verdicts (checked once per completed update).
        let (eigvals, _) = sym_eigen(&self.cov, n);
        let max_eig = eigvals.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
        let min_eig = eigvals.iter().cloned().fold(f64::INFINITY, f64::min);
        if self.best_history.len() >= hist_len {
            let hi = self
                .best_history
                .iter()
                .cloned()
                .fold(f64::NEG_INFINITY, f64::max);
            let lo = self
                .best_history
                .iter()
                .cloned()
                .fold(f64::INFINITY, f64::min);
            if hi - lo < TOL_FUN {
                self.stop = Some(StopReason::TolFun);
                return;
            }
        }
        let max_std = self.sigma * max_eig.max(0.0).sqrt();
        if max_std < TOL_X_REL * self.sigma0 {
            self.stop = Some(StopReason::TolX);
            return;
        }
        if min_eig <= 0.0 || max_eig / min_eig > TOL_CONDITION {
            self.stop = Some(StopReason::TolConditionCov);
            return;
        }
        if max_std > SIGMA_DIVERGE {
            self.stop = Some(StopReason::SigmaDiverged);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Drive one run to its budget on *objective*; return (best_x, best_f, evals).
    fn run(
        x0: &[f64],
        sigma0: f64,
        seed: u64,
        max_evals: usize,
        objective: impl Fn(&[f64]) -> f64,
    ) -> (Vec<f64>, f64, usize) {
        let mut es = Cmaes::new(x0, sigma0, seed, default_lambda(x0.len()));
        let mut best_x = x0.to_vec();
        let mut best_f = f64::INFINITY;
        let mut evals = 0;
        while evals < max_evals && es.stopped().is_none() {
            let xs = es.ask(max_evals - evals);
            let values: Vec<f64> = xs.iter().map(|x| objective(x)).collect();
            evals += xs.len();
            for (x, &f) in xs.iter().zip(&values) {
                if f < best_f {
                    best_f = f;
                    best_x = x.clone();
                }
            }
            es.tell(&values);
        }
        (best_x, best_f, evals)
    }

    #[test]
    fn sphere_2d_converges() {
        // Minimum of Σ(x−0.7)² at (0.7, 0.7) inside the box.
        let obj = |x: &[f64]| x.iter().map(|v| (v - 0.7) * (v - 0.7)).sum::<f64>();
        let (best_x, best_f, _) = run(&[0.2, 0.2], 0.3, 42, 600, obj);
        assert!(best_f < 1e-8, "best_f={best_f}");
        for v in best_x {
            assert!((v - 0.7).abs() < 1e-3, "v={v}");
        }
    }

    #[test]
    fn sphere_5d_converges() {
        let obj = |x: &[f64]| x.iter().map(|v| (v - 0.5) * (v - 0.5)).sum::<f64>();
        let (_, best_f, _) = run(&[0.9; 5], 0.3, 7, 3000, obj);
        assert!(best_f < 1e-6, "best_f={best_f}");
    }

    #[test]
    fn rosenbrock_2d_converges() {
        // Rosenbrock over the unit box, minimum at (1, 1) — a box corner,
        // exercising the projection repair near the boundary.
        let obj = |x: &[f64]| {
            let (a, b) = (x[0], x[1]);
            100.0 * (b - a * a) * (b - a * a) + (1.0 - a) * (1.0 - a)
        };
        let (best_x, best_f, _) = run(&[0.2, 0.2], 0.3, 3, 4000, obj);
        assert!(best_f < 1e-4, "best_f={best_f}");
        assert!((best_x[0] - 1.0).abs() < 0.02 && (best_x[1] - 1.0).abs() < 0.03);
    }

    #[test]
    fn rosenbrock_5d_converges() {
        // Shifted 5-D Rosenbrock with the optimum at 0.5ⁿ (interior).
        let obj = |x: &[f64]| {
            let y: Vec<f64> = x.iter().map(|v| v + 0.5).collect();
            (0..4)
                .map(|i| {
                    100.0 * (y[i + 1] - y[i] * y[i]) * (y[i + 1] - y[i] * y[i])
                        + (1.0 - y[i]) * (1.0 - y[i])
                })
                .sum::<f64>()
        };
        let (best_x, best_f, _) = run(&[0.2; 5], 0.3, 11, 20000, obj);
        assert!(best_f < 1e-3, "best_f={best_f}");
        for v in best_x {
            assert!((v - 0.5).abs() < 0.05, "v={v}");
        }
    }

    #[test]
    fn deterministic_bit_identical() {
        let obj = |x: &[f64]| x.iter().map(|v| (v - 0.6) * (v - 0.6)).sum::<f64>();
        let a = run(&[0.1, 0.9], 0.3, 99, 300, obj);
        let b = run(&[0.1, 0.9], 0.3, 99, 300, obj);
        assert_eq!(
            a.0.iter().map(|v| v.to_bits()).collect::<Vec<_>>(),
            b.0.iter().map(|v| v.to_bits()).collect::<Vec<_>>()
        );
        assert_eq!(a.1.to_bits(), b.1.to_bits());
        assert_eq!(a.2, b.2);
    }

    #[test]
    fn seed_drives_trajectory() {
        let obj = |x: &[f64]| x.iter().map(|v| (v - 0.6) * (v - 0.6)).sum::<f64>();
        let a = run(&[0.1, 0.9], 0.3, 1, 60, obj);
        let b = run(&[0.1, 0.9], 0.3, 2, 60, obj);
        assert_ne!(a.0, b.0, "different seeds must sample differently");
    }

    #[test]
    fn proposals_stay_in_unit_box() {
        let mut es = Cmaes::new(&[0.5, 0.5], 1.0, 5, default_lambda(2));
        for _ in 0..50 {
            if es.stopped().is_some() {
                break;
            }
            let xs = es.ask(usize::MAX);
            for x in &xs {
                for &v in x {
                    assert!((0.0..=1.0).contains(&v), "v={v} escaped the box");
                }
            }
            let values: Vec<f64> = xs.iter().map(|x| x[0] + x[1]).collect();
            es.tell(&values);
        }
    }

    #[test]
    fn flat_fitness_stops_via_tolfun() {
        let mut es = Cmaes::new(&[0.5, 0.5], 0.3, 8, default_lambda(2));
        let mut gens = 0;
        while es.stopped().is_none() && gens < 200 {
            let xs = es.ask(usize::MAX);
            let values = vec![0.0; xs.len()];
            es.tell(&values);
            gens += 1;
        }
        assert_eq!(es.stopped(), Some(StopReason::TolFun), "after {gens} gens");
    }

    #[test]
    fn truncated_generation_applies_no_update() {
        let mut es = Cmaes::new(&[0.5, 0.5], 0.3, 8, default_lambda(2));
        let mean_before = es.mean.clone();
        let sigma_before = es.sigma;
        let xs = es.ask(2); // λ = 6, truncated to 2
        assert_eq!(xs.len(), 2);
        es.tell(&[1.0, 2.0]);
        assert_eq!(es.mean, mean_before);
        assert_eq!(es.sigma, sigma_before);
    }

    #[test]
    fn default_lambda_values() {
        assert_eq!(default_lambda(2), 6);
        assert_eq!(default_lambda(5), 8);
        assert_eq!(default_lambda(10), 10);
    }

    #[test]
    fn lambda_accessor_reports_the_constructed_population() {
        // Direct pin on the accessor the restart driver's truncation law
        // reads (refine.rs: `expected < self.cma.lambda()`): a mutant
        // returning a constant (0/1) would silently disable the
        // truncated-final-generation-no-update law.
        for lambda in [2usize, 6, 13] {
            let es = Cmaes::new(&[0.5, 0.5], 0.3, 7, lambda);
            assert_eq!(es.lambda(), lambda);
        }
    }
}
