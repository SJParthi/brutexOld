//! Rate-limiter token math — the pure IEEE f64 twin of the adaptive bucket.
//!
//! DEC-RATE-MATH-RUST-001 (slice 9, ADR-0019 / MIGRATION.md #1727a): the Rust
//! port of the FOUR pure token-math expressions inside
//! `brutex/orchestrator/rate_limiter.py`'s `AdaptiveTokenBucket` — refill,
//! consume, the acquire-retry wait step, and the `set_rate` post-state
//! (drain-to-empty law, DEC-DHAN-RATE-SINGLE-BUCKET-001). Everything ELSE
//! stays Python: locks, clock/sleep injection, the 429 deque window, the
//! quota-gate seam, events, and the `_ACQUIRE_LOOP_CAP` escape hatch (a
//! state write). §2 Rule 6 scope guard: NO cap / floor / ladder / governor
//! VALUE lives here — these are transcriptions of expressions, with every
//! constant still owned by the Python module.
//!
//! ## Bit-exactness law
//!
//! Unlike the audit-chain float REPR (statistical proof), these are plain
//! IEEE-754 `+ - * /` plus ORDERED comparisons — bit-specified, so parity is
//! EXACT, not statistical. Two transcription subtleties are load-bearing:
//!
//! * **Operation order is transcribed verbatim** (`tokens + elapsed * rps`,
//!   `(1.0 - tokens) / rps`) — no re-association; scalar Rust f64 never
//!   auto-fuses into FMA, so each op rounds exactly like CPython's.
//! * **CPython `min`/`max` argument-order semantics** — Python's builtins
//!   return `b if b < a else a` (min) / `b if b > a else a` (max), which is
//!   NOT `f64::min`/`f64::max` (IEEE minNum/maxNum returns the non-NaN
//!   operand; CPython propagates positionally). [`py_min`] / [`py_max`]
//!   transcribe the comparison form so NaN routing and the ±0.0 tie
//!   (first-argument wins on ties) are bit-identical to the oracle even for
//!   inputs outside the bucket's validated domain.
//!
//! If ANY byte diverges from the Python oracle here, the TRANSCRIPTION is
//! wrong (there is no legitimate divergence class for these ops) — fix the
//! transcription; the audit-chain escalation clause never applies to this
//! module (slice-9 spec §3).

use pyo3::prelude::*;

/// CPython `min(a, b)` transcription: `b if b < a else a`.
///
/// Positional semantics matter: `min(5.0, NAN)` is `5.0` but
/// `min(NAN, 5.0)` is `NAN` in CPython; `min(0.0, -0.0)` is `0.0` (first
/// argument wins the tie). `f64::min` would return the non-NaN operand and
/// `-0.0` on the signed-zero tie — both divergences.
#[inline]
fn py_min(a: f64, b: f64) -> f64 {
    if b < a { b } else { a }
}

/// CPython `max(a, b)` transcription: `b if b > a else a`.
#[inline]
fn py_max(a: f64, b: f64) -> f64 {
    if b > a { b } else { a }
}

/// `_refill_locked`'s token arithmetic (rate_limiter.py):
/// `tokens` unchanged when `elapsed <= 0`, else
/// `min(burst, tokens + elapsed * rps)` — argument order preserved.
///
/// The `elapsed <= 0` arm makes the function TOTAL; the Python call site
/// keeps its own early return (which also skips the `_last_refill` stamp —
/// a state decision that stays Python).
pub fn bucket_refill(tokens: f64, burst: f64, rps: f64, elapsed: f64) -> f64 {
    if elapsed <= 0.0 {
        return tokens;
    }
    py_min(burst, tokens + elapsed * rps)
}

/// `acquire`'s first consume step: `tokens >= 1.0` consumes one token with
/// zero wait, else leaves the tokens and quotes `(1.0 - tokens) / rps`.
///
/// Domain note (slice-9 review, ratemath N2): `rps > 0` is a CALLER
/// invariant (`AdaptiveTokenBucket.__post_init__` validates it, and the
/// parity differential feeds only positive rps). For `rps == 0.0` this fn
/// (and [`bucket_more_wait`]) returns `inf` where the Python expression
/// raises `ZeroDivisionError` — an unreachable-domain divergence,
/// documented rather than guarded (a guard would add a branch no caller
/// can reach).
pub fn bucket_consume(tokens: f64, rps: f64) -> (f64, f64) {
    if tokens >= 1.0 {
        (tokens - 1.0, 0.0)
    } else {
        (tokens, (1.0 - tokens) / rps)
    }
}

/// The acquire retry-loop wait step:
/// `max((1.0 - tokens) / rps, eps)` — argument order preserved
/// (`eps = 1e-9` at today's call site; the value stays Python-owned).
pub fn bucket_more_wait(tokens: f64, rps: f64, eps: f64) -> f64 {
    py_max((1.0 - tokens) / rps, eps)
}

/// `set_rate`'s post-state pair: tokens drained to `0.0` + the moved
/// `downshift_rps = new_rps / 2.0` (DEC-DHAN-RATE-SINGLE-BUCKET-001 — the
/// validation `new_rps > 0` and every state write stay Python).
pub fn bucket_set_rate_state(new_rps: f64) -> (f64, f64) {
    (0.0, new_rps / 2.0)
}

// ────────────────────────────────────────────────────────────────────────
// PyO3 surface
// ────────────────────────────────────────────────────────────────────────

/// `AdaptiveTokenBucket._refill_locked`'s token expression.
#[pyfunction(name = "bucket_refill")]
pub fn bucket_refill_py(tokens: f64, burst: f64, rps: f64, elapsed: f64) -> f64 {
    bucket_refill(tokens, burst, rps, elapsed)
}

/// `AdaptiveTokenBucket.acquire`'s consume step → `(new_tokens, wait_s)`.
#[pyfunction(name = "bucket_consume")]
pub fn bucket_consume_py(tokens: f64, rps: f64) -> (f64, f64) {
    bucket_consume(tokens, rps)
}

/// `AdaptiveTokenBucket.acquire`'s retry-loop wait step.
#[pyfunction(name = "bucket_more_wait")]
pub fn bucket_more_wait_py(tokens: f64, rps: f64, eps: f64) -> f64 {
    bucket_more_wait(tokens, rps, eps)
}

/// `AdaptiveTokenBucket.set_rate`'s post-state → `(tokens, downshift_rps)`.
#[pyfunction(name = "bucket_set_rate_state")]
pub fn bucket_set_rate_state_py(new_rps: f64) -> (f64, f64) {
    bucket_set_rate_state(new_rps)
}

// ────────────────────────────────────────────────────────────────────────
// In-crate tests (pure cores — the byte-exactness proof vs CPython lives in
// tests/property/test_rate_math_rust_parity.py's bit-pattern differential)
// ────────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn py_min_positional_nan_and_signed_zero_ties() {
        // CPython: min(5.0, nan) == 5.0 ; min(nan, 5.0) is nan.
        assert_eq!(py_min(5.0, f64::NAN), 5.0);
        assert!(py_min(f64::NAN, 5.0).is_nan());
        // Tie: the FIRST argument wins — min(0.0, -0.0) == 0.0 (+ sign).
        assert_eq!(py_min(0.0, -0.0).to_bits(), 0.0f64.to_bits());
        assert_eq!(py_min(-0.0, 0.0).to_bits(), (-0.0f64).to_bits());
        assert_eq!(py_min(1.0, 2.0), 1.0);
        assert_eq!(py_min(2.0, 1.0), 1.0);
    }

    #[test]
    fn py_max_positional_nan_and_signed_zero_ties() {
        assert_eq!(py_max(5.0, f64::NAN), 5.0);
        assert!(py_max(f64::NAN, 5.0).is_nan());
        assert_eq!(py_max(0.0, -0.0).to_bits(), 0.0f64.to_bits());
        assert_eq!(py_max(-0.0, 0.0).to_bits(), (-0.0f64).to_bits());
        assert_eq!(py_max(1.0, 2.0), 2.0);
        assert_eq!(py_max(2.0, 1.0), 2.0);
    }

    #[test]
    fn refill_elapsed_boundary_is_inclusive_zero() {
        // elapsed == 0.0 → tokens unchanged (kills `<=` → `<`).
        assert_eq!(bucket_refill(2.5, 5.0, 3.0, 0.0), 2.5);
        // elapsed < 0 (backwards injected clock) → tokens unchanged.
        assert_eq!(bucket_refill(2.5, 5.0, 3.0, -1.0), 2.5);
        // The smallest positive elapsed refills (kills `<=` → `>=`/`==`).
        let refilled = bucket_refill(2.5, 5.0, 3.0, f64::MIN_POSITIVE);
        assert_eq!(refilled, 2.5 + f64::MIN_POSITIVE * 3.0);
    }

    #[test]
    fn refill_clamps_at_burst_with_exact_arithmetic() {
        // Under the clamp: 1.0 + 0.5 * 3.0 = 2.5 < 5.0.
        assert_eq!(bucket_refill(1.0, 5.0, 3.0, 0.5), 2.5);
        // Over the clamp: 4.0 + 1.0 * 3.0 = 7.0 → burst 5.0.
        assert_eq!(bucket_refill(4.0, 5.0, 3.0, 1.0), 5.0);
        // Exactly AT the clamp: 2.0 + 1.0 * 3.0 = 5.0 → the tie serves the
        // FIRST argument (burst) — bit-equal to the sum here either way.
        assert_eq!(bucket_refill(2.0, 5.0, 3.0, 1.0), 5.0);
        // Overflow-adjacent: a huge product clamps to burst, never inf out.
        assert_eq!(bucket_refill(0.0, 8.0, f64::MAX, f64::MAX), 8.0);
    }

    #[test]
    fn consume_boundary_is_inclusive_one() {
        // tokens == 1.0 consumes (kills `>=` → `>`).
        assert_eq!(bucket_consume(1.0, 3.0), (0.0, 0.0));
        // Just below 1.0 waits: (1.0 - tokens) / rps, tokens unchanged.
        let just_below = 1.0 - f64::EPSILON;
        let (tokens, wait) = bucket_consume(just_below, 3.0);
        assert_eq!(tokens, just_below);
        assert_eq!(wait, (1.0 - just_below) / 3.0);
        // Plenty of tokens: plain subtract.
        assert_eq!(bucket_consume(5.0, 3.0), (4.0, 0.0));
    }

    #[test]
    fn more_wait_floors_at_eps() {
        let eps = 1e-9;
        // Wait dominates.
        assert_eq!(bucket_more_wait(0.25, 3.0, eps), (1.0 - 0.25) / 3.0);
        // The float-drift shape the loop exists for: tokens ≈ 1.0 → the
        // computed wait underflows below eps → the eps floor serves.
        let drifted = 1.0 - 1e-12;
        assert_eq!(bucket_more_wait(drifted, 3.0, eps), eps);
        // Exactly AT eps (0.5 / 2.0 == 0.25, IEEE-exact): the tie serves the
        // computed wait (py_max first argument) — pinned at the boundary.
        assert_eq!(bucket_more_wait(0.5, 2.0, 0.25), 0.25);
    }

    #[test]
    fn set_rate_state_drains_and_halves() {
        assert_eq!(bucket_set_rate_state(8.0), (0.0, 4.0));
        assert_eq!(bucket_set_rate_state(3.0), (0.0, 1.5));
        // The drained-token cell is POSITIVE zero, bit-exactly.
        assert_eq!(bucket_set_rate_state(1.0).0.to_bits(), 0.0f64.to_bits());
        // Subnormal halving stays IEEE-exact (power-of-two divide).
        let tiny = f64::MIN_POSITIVE;
        assert_eq!(bucket_set_rate_state(tiny).1, tiny / 2.0);
    }
}
