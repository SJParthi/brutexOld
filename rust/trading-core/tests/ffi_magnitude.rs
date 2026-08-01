//! FFI magnitude-ceiling teeth (`DEC-FFI-MAGNITUDE-CEILING-001`).
//!
//! ## Why this file exists — the failure mode is an ABORT, not an exception
//!
//! `rust/trading-core/Cargo.toml`'s `[profile.release]` carries BOTH
//! `overflow-checks = true` and `panic = "abort"`. That pair is deliberate (an
//! integer overflow must never silently wrap a paisa/ordinal), but it means an
//! overflow panic does NOT unwind: it does not reach pyo3's catch-and-convert
//! wrapper, so it never becomes a Python exception. It SIGABRTs the CPython
//! interpreter that made the call. No traceback, no `except`, no recovery — the
//! caller's whole process dies.
//!
//! So every `#[pyfunction]` that accepts an integer straight from Python and does
//! arithmetic on it needs either a range guard or a `checked_*` op. This file pins
//! the ones that were missing it, at the exact boundary, so a future revert to a
//! bare `+` fails a named test instead of shipping a process-killer.
//!
//! ## What these tests DO and DO NOT prove (Golden Rule #10)
//!
//! Cargo ignores `panic = "abort"` when building a TEST profile — libtest requires
//! unwinding to report a failing test — so a revert of the fix makes these tests
//! *panic* here (measured: `attempt to add with overflow` at the `ema_alpha` add),
//! NOT abort. These tests therefore prove the OVERFLOW half directly, and pin the
//! refusal that replaces it.
//!
//! The ABORT half is a property of the SHIPPED artifact, not of this harness: the
//! wheel Python imports is the `cdylib` built with `[profile.release]`, where
//! `panic = "abort"` DOES apply, so the same overflow there kills the interpreter
//! instead of raising. That half is evidenced in the DEC entry from the release
//! profile itself; it is deliberately not claimed to be demonstrated by this file.
//!
//! ## The pinned law
//!
//! `ema_alpha(period)` is reachable from Python by two routes, both with an
//! unconstrained `i64`:
//!
//! * `trading_core.ema_alpha(period)`   (`python.rs::py_ema_alpha`)
//! * `trading_core.compute_ema(values, period)` (`python.rs::py_compute_ema`
//!   → `compute_ema` → `EmaState::new` → `ema_alpha`)
//!
//! At `period == i64::MAX` the `period + 1` in `α = 2 / (N + 1)` overflows. The
//! fix routes it through the function's EXISTING `Err(String)` channel, which
//! `py_ema_alpha`/`py_compute_ema` already map to `ValueError`.
//!
//! ## Bit-identity (why this is run-identity-inert)
//!
//! `checked_add(1)` performs the SAME integer add and the SAME `as f64` cast for
//! every period that does not overflow, so no valid EMA value moves. The crate
//! VERSION is unchanged, so `indicator_version` — which folds it — cannot re-key
//! any recorded run.

// The crate-root re-exports (`lib.rs:54`) — the same public surface `python.rs`
// and TickVault consume, so this file pins the law through the shipped API.
use trading_core::{EmaState, compute_ema, ema_alpha, ema_seed};

/// The exact overflow boundary: `i64::MAX` refuses, `i64::MAX - 1` succeeds.
///
/// `i64::MAX - 1` is the largest period whose `+ 1` still fits, so it is the last
/// value that MUST keep working — a lazy fix that rejected "very large" periods by
/// magnitude instead of by overflow would fail this half.
#[test]
fn ema_alpha_refuses_the_overflowing_period_instead_of_aborting() {
    let err = ema_alpha(i64::MAX).expect_err(
        "ema_alpha(i64::MAX) must return Err, not panic-abort the interpreter \
         (DEC-FFI-MAGNITUDE-CEILING-001)",
    );
    assert!(
        err.contains("overflows i64"),
        "the refusal must name the overflow so a Python ValueError is diagnosable; got {err:?}"
    );

    // The last non-overflowing period still computes, and computes the value the
    // unchecked code would have: 2 / (i64::MAX - 1 + 1) == 2 / i64::MAX.
    let alpha =
        ema_alpha(i64::MAX - 1).expect("i64::MAX - 1 does not overflow and must still be accepted");
    assert_eq!(
        alpha,
        2.0 / (i64::MAX as f64),
        "the largest valid period must keep its exact pre-fix IEEE-754 alpha"
    );
}

/// The two OTHER reachable routes into the same add must refuse too, not abort.
///
/// `EmaState::new` and `compute_ema` both delegate their alpha to `ema_alpha`, so
/// the guard has to propagate rather than being re-derived at each entry point.
#[test]
fn ema_state_and_compute_ema_propagate_the_overflow_refusal() {
    let state_err = EmaState::new(i64::MAX)
        .expect_err("EmaState::new(i64::MAX) must propagate the ema_alpha refusal");
    assert!(
        state_err.contains("overflows i64"),
        "EmaState::new must surface the overflow reason verbatim; got {state_err:?}"
    );

    // The `compute_ema` route is what `trading_core.compute_ema(values, period)`
    // calls. An EMPTY value list is used on purpose: it proves the refusal fires
    // on the PERIOD alone, before any bar is consumed, so a caller cannot dodge it
    // by passing no data.
    let batch_err = compute_ema(&[], i64::MAX)
        .expect_err("compute_ema(&[], i64::MAX) must refuse before touching any bar");
    assert!(
        batch_err.contains("overflows i64"),
        "compute_ema must surface the overflow reason verbatim; got {batch_err:?}"
    );
}

/// The pre-existing `period < 1` guard is UNCHANGED and still wins at the low end.
///
/// This is the anti-regression half: the fix added a second refusal reason, and a
/// careless reorder could shadow the original message that `ema.py`'s documented
/// `ValueError` text mirrors.
#[test]
fn ema_period_floor_guard_is_unchanged_and_still_first() {
    for period in [0_i64, -1, i64::MIN] {
        let err = ema_alpha(period).expect_err("period < 1 must still refuse");
        assert_eq!(
            err,
            format!("EMA period must be >= 1, got {period}"),
            "the period-floor message is mirrored by ema.py and must not drift"
        );
        // Notably i64::MIN must take the FLOOR path, not an overflow path: the
        // `< 1` test precedes the add, so `i64::MIN + 1` is never evaluated.
        assert!(
            !err.contains("overflows"),
            "i64::MIN must be refused by the floor guard, not by the add"
        );
    }

    // `ema_seed` shares the same floor guard and has no add to overflow — pinned
    // here so the sibling cannot drift apart from `ema_alpha`'s wording.
    let seed_err = ema_seed(1.0, 0).expect_err("ema_seed(_, 0) must refuse");
    assert_eq!(seed_err, "EMA period must be >= 1, got 0");
}

/// A representative spread of ordinary periods is bit-identical to the pre-fix
/// arithmetic — the run-identity-inertness claim, asserted rather than narrated.
#[test]
fn ordinary_periods_keep_their_exact_pre_fix_alpha() {
    // 1 and 2 are the degenerate/boundary ends; 20 and 200 are the two periods the
    // engine actually evaluates; the rest bracket the f64 mantissa.
    for period in [1_i64, 2, 3, 20, 200, 1_000, 1 << 30, (1_i64 << 53) - 1] {
        let got = ema_alpha(period).expect("an ordinary period must be accepted");
        // The pre-fix expression, spelled out. `period + 1` cannot overflow for any
        // value in this list, so this reference is itself safe to evaluate.
        let want = 2.0 / (period + 1) as f64;
        assert_eq!(
            got, want,
            "alpha for period {period} moved: {got:?} != {want:?} (bit-identity broken)"
        );
    }
}
