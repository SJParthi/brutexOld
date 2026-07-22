//! Frozen golden of one full refinement run (`DEC-RUST-STAGE3-REFINE-001`).
//!
//! GREENFIELD ruling: no pycma bit-match exists to compare against — this
//! golden freezes THIS implementation's own intended trajectory (captured
//! 2026-07-19 on the first green build) so any later math/RNG/schedule edit
//! that silently moves the deterministic trajectory fails LOUDLY and forces
//! a conscious, decision-ID'd re-lock. The Python-side twin
//! (`tests/golden/test_stage3_refine_rust_golden.py` + its committed JSON
//! fixture) freezes the SAME run through the production `refine_mask`
//! wrapper — the two goldens must agree by construction (one driver).
//!
//! Coordinates: the Mode-A axes, x0 = (50, 100) paisa, σ0 = 0.3,
//! seed = 0xabababababababab (= `seed_from_mask_run_id("ab" * 32)`, the low
//! 64 identity bits), budget 60 asks × 2 restarts on the bowl objective
//! `((sl − 100)² + (tp − 250)²) / 10⁴` (minimized).

use brutex_refine::{GridAxis, RefineDriver};

const SEED: u64 = 0xabab_abab_abab_abab;
const MAX_FEVALS: u64 = 60;
const MAX_RESTARTS: u32 = 2;

/// FNV-1a over the little-endian pair bytes — pins the FULL ask sequence.
fn fnv(pairs: &[(i64, i64)]) -> u64 {
    let mut h: u64 = 0xcbf2_9ce4_8422_2325;
    for &(a, b) in pairs {
        for v in [a as u64, b as u64] {
            for byte in v.to_le_bytes() {
                h ^= u64::from(byte);
                h = h.wrapping_mul(0x0000_0100_0000_01b3);
            }
        }
    }
    h
}

/// One drive outcome: (asked pairs, restarts run, (best pair, best value)).
type DriveOutcome = (Vec<(i64, i64)>, u32, ((i64, i64), f64));

fn drive() -> DriveOutcome {
    let sl = GridAxis::new("sl_paisa", 5, 200, 5).unwrap();
    let tp = GridAxis::new("tp_paisa", 10, 500, 10).unwrap();
    let x0 = [sl.normalize(50).unwrap(), tp.normalize(100).unwrap()];
    let mut driver = RefineDriver::new(x0, 0.3, SEED, MAX_FEVALS, MAX_RESTARTS, sl, tp).unwrap();
    let mut asked = Vec::new();
    let mut best = ((0i64, 0i64), f64::INFINITY);
    while !driver.done() {
        let pairs = driver.ask().unwrap();
        let values: Vec<f64> = pairs
            .iter()
            .map(|&(s, t)| ((s - 100).pow(2) as f64 + (t - 250).pow(2) as f64) / 1e4)
            .collect();
        for (&p, &v) in pairs.iter().zip(&values) {
            if v < best.1 {
                best = (p, v);
            }
        }
        asked.extend_from_slice(&pairs);
        driver.tell(&values).unwrap();
    }
    let restarts = driver.restarts_run();
    (asked, restarts, best)
}

#[test]
fn frozen_trajectory_reproduces_exactly() {
    let (asked, restarts, best) = drive();
    // The hard budget consumed exactly: 60 asks × 2 restarts.
    assert_eq!(asked.len(), 120);
    assert_eq!(restarts, MAX_RESTARTS);
    // Head + tail literals (debuggability on a drift).
    assert_eq!(
        &asked[..12],
        &[
            (5, 150),
            (5, 10),
            (75, 90),
            (50, 130),
            (90, 10),
            (30, 100),
            (15, 70),
            (25, 190),
            (30, 50),
            (5, 10),
            (100, 60),
            (30, 130),
        ]
    );
    assert_eq!(
        &asked[asked.len() - 4..],
        &[(75, 280), (35, 270), (5, 270), (60, 170)]
    );
    // Best quantized candidate + its exact objective value.
    assert_eq!(best.0, (100, 260));
    assert_eq!(best.1, 0.01);
    // The full-sequence pin: every one of the 120 pairs, in order.
    assert_eq!(fnv(&asked), 0x00c3_0d1f_8db3_7f61);
}

#[test]
fn frozen_trajectory_is_replay_stable() {
    // The golden is not a fluke of one process: two in-process replays agree
    // bit-for-bit (the determinism law the frozen literals rest on).
    let a = drive();
    let b = drive();
    assert_eq!(a.0, b.0);
    assert_eq!(a.1, b.1);
    assert_eq!(a.2.0, b.2.0);
    assert_eq!(a.2.1.to_bits(), b.2.1.to_bits());
}
