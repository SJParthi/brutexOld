//! The Stage-3 per-mask refinement driver — restarted CMA-ES over the
//! quantized (SL, TP) Mode-A grid.
//!
//! `DEC-RUST-STAGE3-REFINE-001` — the optimizer half of the retired Python
//! `brutex/engine/stage3_refine.py::refine_mask` loop. Laws (the frozen
//! behavior spec, pinned by the crate tests + the Python golden):
//!
//! * **Restart schedule** — restart *k* (0-based) runs a FRESH CMA-ES seeded
//!   `seed.wrapping_add(k)` (disjoint, fully-derived streams — the retired
//!   Python law, widened from its numpy-forced 32-bit seed domain to the
//!   full u64) with population `λ_k = min(λ₀ · 2ᵏ, max_fevals)` where
//!   `λ₀ = 4 + ⌊3 ln n⌋` — the IPOP population-doubling half of BIPOP
//!   (large-population restarts diversify; the small-population interleave
//!   is deliberately NOT implemented — a documented simplification, every
//!   restart is budgeted identically). All `max_restarts` restarts ALWAYS
//!   run (an early inner stop just moves on to the next restart — the
//!   retired Python law verbatim).
//! * **HARD budget** — at most `max_fevals` asks per restart, enforced
//!   STRICTLY: the final generation is truncated to the remaining budget
//!   (the retired pycma path could overshoot by a partial population —
//!   measured 121 asks on a 60-ask budget; this port closes that to the
//!   documented `≤ max_fevals × max_restarts` bound exactly).
//! * **Quantize before exposure** — every proposal is snapped to the axis
//!   grids (`GridAxis::denormalize`) INSIDE the driver, so the caller only
//!   ever sees on-grid int paisa (ADR-0007 condition 3).
//! * **Minimization protocol** — `tell` takes the values to MINIMIZE (the
//!   Python wrapper negates Sharpe, exactly as before).

use crate::cmaes::{Cmaes, default_lambda};
use crate::grid::GridAxis;

/// Driver protocol errors (the FFI edge maps these to `ValueError`).
pub type DriverError = String;

/// The restarted-CMA-ES driver for one mask's (SL, TP) refinement.
#[derive(Debug, Clone)]
pub struct RefineDriver {
    seed: u64,
    sigma0: f64,
    max_fevals: u64,
    max_restarts: u32,
    sl_axis: GridAxis,
    tp_axis: GridAxis,
    x0: [f64; 2],
    restart_idx: u32,
    evals_this_restart: u64,
    cma: Cmaes,
    pending: Option<usize>, // len of the outstanding ask
    done: bool,
}

impl RefineDriver {
    /// Population for restart *k*: `min(λ₀ · 2ᵏ, max_fevals)`, floor 2.
    fn lambda_for_restart(max_fevals: u64, k: u32) -> usize {
        let base = default_lambda(2) as u64;
        let doubled = base.saturating_mul(1u64 << k.min(32));
        doubled.min(max_fevals).max(2) as usize
    }

    /// Build a driver at the normalized start `x0` (each coordinate already
    /// in `[0, 1]` — the caller normalizes the survivor's incumbent cell).
    pub fn new(
        x0: [f64; 2],
        sigma0: f64,
        seed: u64,
        max_fevals: u64,
        max_restarts: u32,
        sl_axis: GridAxis,
        tp_axis: GridAxis,
    ) -> Result<Self, DriverError> {
        if max_fevals < 1 {
            return Err(format!("max_fevals must be >= 1, got {max_fevals}"));
        }
        if max_restarts < 1 {
            return Err(format!("max_restarts must be >= 1, got {max_restarts}"));
        }
        if !(sigma0 > 0.0 && sigma0 <= 1.0) {
            return Err(format!("sigma0 must be in (0, 1], got {sigma0}"));
        }
        for (label, u) in [("u_sl", x0[0]), ("u_tp", x0[1])] {
            if !(0.0..=1.0).contains(&u) {
                return Err(format!("{label} must be in [0, 1], got {u}"));
            }
        }
        let cma = Cmaes::new(&x0, sigma0, seed, Self::lambda_for_restart(max_fevals, 0));
        Ok(Self {
            seed,
            sigma0,
            max_fevals,
            max_restarts,
            sl_axis,
            tp_axis,
            x0,
            restart_idx: 0,
            evals_this_restart: 0,
            cma,
            pending: None,
            done: false,
        })
    }

    /// Whether every restart has exhausted its budget/stop verdict.
    pub fn done(&self) -> bool {
        self.done
    }

    /// Restarts run so far (== `max_restarts` once `done()`).
    pub fn restarts_run(&self) -> u32 {
        if self.done {
            self.max_restarts
        } else {
            self.restart_idx + 1
        }
    }

    /// Propose the next generation as QUANTIZED on-grid `(sl, tp)` paisa.
    pub fn ask(&mut self) -> Result<Vec<(i64, i64)>, DriverError> {
        if self.done {
            return Err("ask() on a finished refinement".to_owned());
        }
        if self.pending.is_some() {
            return Err("ask() called with a pending tell()".to_owned());
        }
        let remaining = (self.max_fevals - self.evals_this_restart) as usize;
        let xs = self.cma.ask(remaining);
        self.pending = Some(xs.len());
        let mut pairs = Vec::with_capacity(xs.len());
        for x in &xs {
            // Non-NaN by construction (finite mean/σ arithmetic over
            // clamped samples), so denormalize cannot fail here; propagate
            // defensively rather than unwrap.
            let sl = self.sl_axis.denormalize(x[0])?;
            let tp = self.tp_axis.denormalize(x[1])?;
            pairs.push((sl, tp));
        }
        Ok(pairs)
    }

    /// Report the (minimization) objective values for the last `ask`.
    ///
    /// A REJECTED tell (no pending ask / length mismatch / non-finite value)
    /// leaves the outstanding ask pending, so a corrected retry succeeds —
    /// the validation runs BEFORE the pending marker is consumed.
    pub fn tell(&mut self, values: &[f64]) -> Result<(), DriverError> {
        let Some(expected) = self.pending else {
            return Err("tell() without a pending ask()".to_owned());
        };
        if values.len() != expected {
            return Err(format!(
                "tell() expected {expected} values, got {}",
                values.len()
            ));
        }
        for &v in values {
            if !v.is_finite() {
                return Err(format!("tell() values must be finite, got {v}"));
            }
        }
        self.pending = None;
        let truncated = expected < self.cma.lambda();
        self.cma.tell(values);
        self.evals_this_restart += expected as u64;
        if truncated || self.evals_this_restart >= self.max_fevals || self.cma.stopped().is_some() {
            self.advance_restart();
        }
        Ok(())
    }

    fn advance_restart(&mut self) {
        self.restart_idx += 1;
        if self.restart_idx >= self.max_restarts {
            self.done = true;
            return;
        }
        self.evals_this_restart = 0;
        self.cma = Cmaes::new(
            &self.x0,
            self.sigma0,
            self.seed.wrapping_add(u64::from(self.restart_idx)),
            Self::lambda_for_restart(self.max_fevals, self.restart_idx),
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn mode_a_axes() -> (GridAxis, GridAxis) {
        (
            GridAxis::new("sl_paisa", 5, 200, 5).unwrap(),
            GridAxis::new("tp_paisa", 10, 500, 10).unwrap(),
        )
    }

    /// A drive outcome: (all asked pairs, best pair, total asks, restarts).
    type DriveOutcome = (Vec<(i64, i64)>, (i64, i64), usize, u32);

    /// Drive a full refinement on the bowl objective.
    fn drive(seed: u64, max_fevals: u64, max_restarts: u32) -> DriveOutcome {
        let (sl, tp) = mode_a_axes();
        let x0 = [sl.normalize(50).unwrap(), tp.normalize(100).unwrap()];
        let mut driver =
            RefineDriver::new(x0, 0.3, seed, max_fevals, max_restarts, sl, tp).unwrap();
        let mut asked = Vec::new();
        let mut best = ((0i64, 0i64), f64::INFINITY);
        while !driver.done() {
            let pairs = driver.ask().unwrap();
            let values: Vec<f64> = pairs
                .iter()
                .map(|&(s, t)| ((s - 100) * (s - 100) + (t - 250) * (t - 250)) as f64 / 1e4)
                .collect();
            for (&pair, &v) in pairs.iter().zip(&values) {
                if v < best.1 {
                    best = (pair, v);
                }
            }
            asked.extend_from_slice(&pairs);
            driver.tell(&values).unwrap();
        }
        let restarts = driver.restarts_run();
        let total = asked.len();
        (asked, best.0, total, restarts)
    }

    #[test]
    fn hard_budget_never_exceeded() {
        for (fevals, restarts) in [(40u64, 2u32), (7, 3), (1, 1), (200, 3)] {
            let (asked, _, total, ran) = drive(9, fevals, restarts);
            assert!(total as u64 <= fevals * u64::from(restarts), "{total} asks");
            assert_eq!(ran, restarts);
            assert_eq!(asked.len(), total);
        }
    }

    #[test]
    fn finds_the_bowl_optimum_region() {
        let (_, best, _, _) = drive(0xABAB, 200, 3);
        assert!((best.0 - 100).abs() <= 15, "best sl {}", best.0);
        assert!((best.1 - 250).abs() <= 30, "best tp {}", best.1);
    }

    #[test]
    fn deterministic_ask_sequence() {
        let a = drive(1234, 60, 2);
        let b = drive(1234, 60, 2);
        assert_eq!(a.0, b.0);
        assert_eq!(a.1, b.1);
    }

    #[test]
    fn seed_changes_the_sequence() {
        let a = drive(1, 60, 2);
        let b = drive(2, 60, 2);
        assert_ne!(a.0, b.0);
    }

    #[test]
    fn all_proposals_on_grid() {
        let (asked, _, _, _) = drive(77, 120, 3);
        for (sl, tp) in asked {
            assert!((5..=200).contains(&sl) && sl % 5 == 0, "sl={sl}");
            assert!((10..=500).contains(&tp) && tp % 10 == 0, "tp={tp}");
        }
    }

    #[test]
    fn restart_population_schedule() {
        // λ₀ = 6 for n=2; doubling per restart, capped at max_fevals.
        assert_eq!(RefineDriver::lambda_for_restart(200, 0), 6);
        assert_eq!(RefineDriver::lambda_for_restart(200, 1), 12);
        assert_eq!(RefineDriver::lambda_for_restart(200, 2), 24);
        assert_eq!(RefineDriver::lambda_for_restart(10, 2), 10); // capped
        assert_eq!(RefineDriver::lambda_for_restart(1, 0), 2); // floor
        assert_eq!(RefineDriver::lambda_for_restart(200, 60), 200); // shift cap
    }

    #[test]
    fn config_validation() {
        let (sl, tp) = mode_a_axes();
        let x0 = [0.5, 0.5];
        assert!(RefineDriver::new(x0, 0.3, 1, 0, 3, sl.clone(), tp.clone()).is_err());
        assert!(RefineDriver::new(x0, 0.3, 1, 10, 0, sl.clone(), tp.clone()).is_err());
        assert!(RefineDriver::new(x0, 0.0, 1, 10, 1, sl.clone(), tp.clone()).is_err());
        assert!(RefineDriver::new(x0, 1.5, 1, 10, 1, sl.clone(), tp.clone()).is_err());
        assert!(RefineDriver::new([1.5, 0.5], 0.3, 1, 10, 1, sl.clone(), tp.clone()).is_err());
        assert!(RefineDriver::new([0.5, -0.1], 0.3, 1, 10, 1, sl, tp).is_err());
    }

    #[test]
    fn protocol_misuse_is_an_error_not_a_panic() {
        let (sl, tp) = mode_a_axes();
        let mut driver = RefineDriver::new([0.5, 0.5], 0.3, 1, 10, 1, sl, tp).unwrap();
        assert!(driver.tell(&[1.0]).is_err()); // tell without ask
        let pairs = driver.ask().unwrap();
        assert!(driver.ask().is_err()); // double ask
        assert!(driver.tell(&vec![1.0; pairs.len() + 1]).is_err()); // len mismatch
        assert!(driver.tell(&vec![f64::NAN; pairs.len()]).is_err()); // non-finite
        // A rejected tell leaves the ask PENDING — the corrected retry lands.
        driver.tell(&vec![0.0; pairs.len()]).unwrap();
    }

    #[test]
    fn done_after_last_restart_and_ask_refused() {
        let mut d = {
            let (sl, tp) = mode_a_axes();
            RefineDriver::new([0.5, 0.5], 0.3, 5, 6, 1, sl, tp).unwrap()
        };
        while !d.done() {
            let pairs = d.ask().unwrap();
            d.tell(&vec![1.0; pairs.len()]).unwrap();
        }
        assert_eq!(d.restarts_run(), 1);
        assert!(d.ask().is_err());
    }
}
