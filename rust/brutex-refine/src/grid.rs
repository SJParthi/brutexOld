//! The Stage-3 [0,1] ↔ int-paisa Mode-A grid bijection (the math half).
//!
//! `DEC-RUST-STAGE3-REFINE-001`: the quantization math of the retired Python
//! `brutex/engine/stage3_grid.py::GridAxis` — clamp + round-half-to-even
//! nearest-index snap (`denormalize`) and the exact inverse coordinate
//! (`normalize`) — with the SAME error-message text, so the Python thin
//! wrapper re-raises byte-identical `Stage3GridError` messages.
//!
//! `round_ties_even` matches CPython's `round()` on floats exactly (both
//! round the IEEE-754 double to the nearest integer, ties to even), so the
//! snap parity with the retired Python implementation is by construction —
//! pinned exhaustively by the crate tests and the Python golden suite.

/// One fixed grid axis — the int-paisa points `lo, lo+tick, …, hi`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GridAxis {
    pub name: String,
    pub lo_paisa: i64,
    pub hi_paisa: i64,
    pub tick_paisa: i64,
}

impl GridAxis {
    /// Validate and build an axis; the error strings are the load-bearing
    /// contract (the Python wrapper surfaces them verbatim).
    pub fn new(name: &str, lo_paisa: i64, hi_paisa: i64, tick_paisa: i64) -> Result<Self, String> {
        if lo_paisa <= 0 {
            return Err(format!("{name}: lo_paisa must be > 0, got {lo_paisa}"));
        }
        if tick_paisa <= 0 {
            return Err(format!("{name}: tick_paisa must be > 0, got {tick_paisa}"));
        }
        if hi_paisa <= lo_paisa {
            return Err(format!(
                "{name}: hi_paisa must be > lo_paisa, got [{lo_paisa}, {hi_paisa}]"
            ));
        }
        if (hi_paisa - lo_paisa) % tick_paisa != 0 {
            return Err(format!(
                "{name}: span {} is not a whole number of {tick_paisa}-paisa ticks",
                hi_paisa - lo_paisa
            ));
        }
        Ok(Self {
            name: name.to_owned(),
            lo_paisa,
            hi_paisa,
            tick_paisa,
        })
    }

    /// Number of on-grid points, always ≥ 2 (guaranteed by `new`).
    #[inline]
    pub fn steps(&self) -> i64 {
        (self.hi_paisa - self.lo_paisa) / self.tick_paisa + 1
    }

    /// Map *u* to the nearest on-grid int paisa — clamp into [0,1], then
    /// snap via round-half-to-even on `u · (steps − 1)`. NaN is rejected
    /// (no nearest grid point exists); ±inf clamp to the edges.
    pub fn denormalize(&self, u: f64) -> Result<i64, String> {
        if u.is_nan() {
            return Err(format!("{}: cannot denormalize NaN", self.name));
        }
        let clamped = u.clamp(0.0, 1.0);
        let index = (clamped * (self.steps() - 1) as f64).round_ties_even() as i64;
        Ok(self.lo_paisa + index * self.tick_paisa)
    }

    /// Map an ON-GRID int paisa back to its exact [0,1] coordinate
    /// `index / (steps − 1)`; off-grid / out-of-range input is refused —
    /// silent snapping would let a never-evaluated pair masquerade as a
    /// measured grid point.
    pub fn normalize(&self, paisa: i64) -> Result<f64, String> {
        if paisa < self.lo_paisa || paisa > self.hi_paisa {
            return Err(format!(
                "{}: {paisa} outside [{}, {}]",
                self.name, self.lo_paisa, self.hi_paisa
            ));
        }
        let offset = paisa - self.lo_paisa;
        if offset % self.tick_paisa != 0 {
            return Err(format!(
                "{}: {paisa} is off-grid (not on the {}-paisa tick from {})",
                self.name, self.tick_paisa, self.lo_paisa
            ));
        }
        Ok((offset / self.tick_paisa) as f64 / (self.steps() - 1) as f64)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sl_axis() -> GridAxis {
        GridAxis::new("sl_paisa", 5, 200, 5).unwrap()
    }

    fn tp_axis() -> GridAxis {
        GridAxis::new("tp_paisa", 10, 500, 10).unwrap()
    }

    #[test]
    fn mode_a_steps() {
        assert_eq!(sl_axis().steps(), 40);
        assert_eq!(tp_axis().steps(), 50);
    }

    #[test]
    fn validation_messages() {
        assert_eq!(
            GridAxis::new("t", 0, 100, 5).unwrap_err(),
            "t: lo_paisa must be > 0, got 0"
        );
        assert_eq!(
            GridAxis::new("t", 5, 100, 0).unwrap_err(),
            "t: tick_paisa must be > 0, got 0"
        );
        assert_eq!(
            GridAxis::new("t", 100, 100, 5).unwrap_err(),
            "t: hi_paisa must be > lo_paisa, got [100, 100]"
        );
        assert_eq!(
            GridAxis::new("t", 5, 201, 5).unwrap_err(),
            "t: span 196 is not a whole number of 5-paisa ticks"
        );
    }

    #[test]
    fn denormalize_edges_and_clamp() {
        let axis = sl_axis();
        assert_eq!(axis.denormalize(0.0).unwrap(), 5);
        assert_eq!(axis.denormalize(1.0).unwrap(), 200);
        assert_eq!(axis.denormalize(-1e9).unwrap(), 5);
        assert_eq!(axis.denormalize(f64::NEG_INFINITY).unwrap(), 5);
        assert_eq!(axis.denormalize(1e9).unwrap(), 200);
        assert_eq!(axis.denormalize(f64::INFINITY).unwrap(), 200);
    }

    #[test]
    fn denormalize_nan_rejected() {
        assert_eq!(
            sl_axis().denormalize(f64::NAN).unwrap_err(),
            "sl_paisa: cannot denormalize NaN"
        );
    }

    #[test]
    fn snap_nearest_and_ties_even() {
        // 0.5 · 39 = 19.5 → even index 20 → 5 + 20·5 = 105 (the CPython pin).
        assert_eq!(sl_axis().denormalize(0.5).unwrap(), 105);
        assert_eq!(sl_axis().denormalize(19.4 / 39.0).unwrap(), 100);
        assert_eq!(sl_axis().denormalize(19.6 / 39.0).unwrap(), 105);
        // Power-of-two axis: 0.125·4 = 0.5 → even 0; 0.375·4 = 1.5 → even 2.
        let axis = GridAxis::new("t", 5, 45, 10).unwrap();
        assert_eq!(axis.denormalize(0.125).unwrap(), 5);
        assert_eq!(axis.denormalize(0.375).unwrap(), 25);
    }

    #[test]
    fn normalize_rejections() {
        assert_eq!(
            sl_axis().normalize(4).unwrap_err(),
            "sl_paisa: 4 outside [5, 200]"
        );
        assert_eq!(
            sl_axis().normalize(7).unwrap_err(),
            "sl_paisa: 7 is off-grid (not on the 5-paisa tick from 5)"
        );
    }

    #[test]
    fn round_trip_exhaustive() {
        for axis in [sl_axis(), tp_axis()] {
            let mut paisa = axis.lo_paisa;
            while paisa <= axis.hi_paisa {
                let u = axis.normalize(paisa).unwrap();
                assert!((0.0..=1.0).contains(&u));
                assert_eq!(axis.denormalize(u).unwrap(), paisa);
                paisa += axis.tick_paisa;
            }
        }
    }

    #[test]
    fn normalize_exact_interior_coordinates() {
        // The exact-IEEE contract the Python tests pin: 1/39 and 1/49.
        assert_eq!(sl_axis().normalize(10).unwrap(), 1.0 / 39.0);
        assert_eq!(tp_axis().normalize(20).unwrap(), 1.0 / 49.0);
    }
}
