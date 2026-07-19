//! Deterministic in-crate PRNG — xoshiro256++ seeded via splitmix64, plus a
//! Box–Muller standard-normal source.
//!
//! `DEC-RUST-STAGE3-REFINE-001`: determinism is a Golden-Rule-#4 contract
//! (same seed ⇒ bit-identical trajectory on one platform), so the generator
//! is IN-CRATE — it can never drift under an external crate's version bump.
//! xoshiro256++ is the named, public-domain generator of Blackman & Vigna
//! (<https://prng.di.unimi.it/>); splitmix64 is its recommended seeder (it
//! expands ANY u64 — including 0 — into a full non-zero 256-bit state).

/// splitmix64 — the state expander (Vigna's reference sequence).
#[inline]
fn splitmix64(state: &mut u64) -> u64 {
    *state = state.wrapping_add(0x9E37_79B9_7F4A_7C15);
    let mut z = *state;
    z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
    z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
    z ^ (z >> 31)
}

/// xoshiro256++ — 256-bit state, 64-bit output, period 2²⁵⁶ − 1.
#[derive(Debug, Clone)]
pub struct Xoshiro256pp {
    s: [u64; 4],
}

impl Xoshiro256pp {
    /// Seed the full state from one u64 via splitmix64 (never all-zero:
    /// splitmix64 output over four consecutive states cannot be 0,0,0,0).
    pub fn new(seed: u64) -> Self {
        let mut sm = seed;
        Self {
            s: [
                splitmix64(&mut sm),
                splitmix64(&mut sm),
                splitmix64(&mut sm),
                splitmix64(&mut sm),
            ],
        }
    }

    /// Next raw 64-bit output.
    #[inline]
    pub fn next_u64(&mut self) -> u64 {
        let result = self.s[0]
            .wrapping_add(self.s[3])
            .rotate_left(23)
            .wrapping_add(self.s[0]);
        let t = self.s[1] << 17;
        self.s[2] ^= self.s[0];
        self.s[3] ^= self.s[1];
        self.s[1] ^= self.s[2];
        self.s[0] ^= self.s[3];
        self.s[2] ^= t;
        self.s[3] = self.s[3].rotate_left(45);
        result
    }

    /// Uniform f64 in the HALF-OPEN interval (0, 1] — 53 mantissa bits.
    ///
    /// `(bits >> 11) + 1` maps the 53-bit integer 0..2⁵³−1 to 1..2⁵³, so the
    /// result is never 0.0 (safe under `ln`) and at most exactly 1.0.
    #[inline]
    pub fn next_f64_open01(&mut self) -> f64 {
        (((self.next_u64() >> 11) + 1) as f64) * (1.0 / (1u64 << 53) as f64)
    }
}

/// Box–Muller standard-normal source over a [`Xoshiro256pp`].
///
/// Each transform consumes two uniforms and yields two normals; the second is
/// cached so consecutive draws advance the underlying stream deterministically
/// (2 uniforms per 2 normals, always in the same order).
#[derive(Debug, Clone)]
pub struct NormalSource {
    rng: Xoshiro256pp,
    cached: Option<f64>,
}

impl NormalSource {
    /// Build over a fresh xoshiro256++ stream seeded from *seed*.
    pub fn new(seed: u64) -> Self {
        Self {
            rng: Xoshiro256pp::new(seed),
            cached: None,
        }
    }

    /// Next standard-normal draw N(0, 1).
    #[inline]
    pub fn next_normal(&mut self) -> f64 {
        if let Some(z) = self.cached.take() {
            return z;
        }
        // u1 ∈ (0, 1] ⇒ ln(u1) finite; u2 ∈ (0, 1] drives the angle.
        let u1 = self.rng.next_f64_open01();
        let u2 = self.rng.next_f64_open01();
        let r = (-2.0 * u1.ln()).sqrt();
        let theta = 2.0 * std::f64::consts::PI * u2;
        self.cached = Some(r * theta.sin());
        r * theta.cos()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn same_seed_same_stream() {
        let mut a = Xoshiro256pp::new(42);
        let mut b = Xoshiro256pp::new(42);
        for _ in 0..1000 {
            assert_eq!(a.next_u64(), b.next_u64());
        }
    }

    #[test]
    fn different_seed_different_stream() {
        let mut a = Xoshiro256pp::new(42);
        let mut b = Xoshiro256pp::new(43);
        let same = (0..64).filter(|_| a.next_u64() == b.next_u64()).count();
        assert!(same < 4, "streams should diverge, {same}/64 collisions");
    }

    #[test]
    fn zero_seed_is_valid() {
        let mut rng = Xoshiro256pp::new(0);
        // splitmix64 expansion never leaves the all-zero fixed point.
        assert_ne!(rng.next_u64() | rng.next_u64() | rng.next_u64(), 0);
    }

    #[test]
    fn uniform_open01_bounds() {
        let mut rng = Xoshiro256pp::new(7);
        for _ in 0..100_000 {
            let u = rng.next_f64_open01();
            assert!(u > 0.0 && u <= 1.0, "u={u} outside (0, 1]");
        }
    }

    #[test]
    fn normals_are_deterministic_and_plausible() {
        let mut a = NormalSource::new(11);
        let mut b = NormalSource::new(11);
        let mut sum = 0.0;
        let mut sum_sq = 0.0;
        const N: usize = 100_000;
        for _ in 0..N {
            let z = a.next_normal();
            assert_eq!(z.to_bits(), b.next_normal().to_bits());
            sum += z;
            sum_sq += z * z;
        }
        let mean = sum / N as f64;
        let var = sum_sq / N as f64 - mean * mean;
        assert!(mean.abs() < 0.02, "mean={mean}");
        assert!((var - 1.0).abs() < 0.03, "var={var}");
    }
}
