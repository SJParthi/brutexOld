//! FFI magnitude-ceiling teeth for `expected_bar_instant`
//! (`DEC-FFI-MAGNITUDE-CEILING-001`).
//!
//! ## The hazard, stated precisely
//!
//! `series::expected_bar_instant`'s intraday label expression
//!
//! ```text
//! open_us + -((open_us - seg_start_us).div_euclid(step_us)) * step_us
//! ```
//!
//! carries FIVE unchecked `i64` ops (subtract, `div_euclid`, negate,
//! multiply, add). This crate's `[profile.release]` sets BOTH
//! `overflow-checks = true` and `panic = "abort"` (Cargo.toml), so in the
//! SHIPPED `cdylib` an overflow there does not unwind into pyo3's
//! catch-and-convert wrapper — it SIGABRTs the CPython interpreter that
//! called `brutex_feed.expected_bar_instant`. No traceback, no `except`,
//! process dead.
//!
//! The fix is an envelope guard at the `#[pyfunction]` face
//! (`python.rs::expected_bar_instant`) over the crate's OWN shared magnitude
//! law `series::ts_in_envelope` — the same `(0..MAX_SAFE_TS_US)` predicate
//! `series::ownership_cut` already abstains on.
//!
//! ## What these tests DO and DO NOT prove (Golden Rule #10)
//!
//! Cargo ignores `panic = "abort"` for TEST profiles (libtest needs
//! unwinding), so here the unguarded overflow *panics* and is catchable —
//! which is exactly what makes [`unguarded_kernel_really_does_overflow`] a
//! usable executable exhibit. It proves the OVERFLOW half. The ABORT half is
//! a property of the release-profile artifact and is evidenced in the DEC
//! entry from that profile; it is deliberately NOT claimed to be
//! demonstrated by this file.
//!
//! These tests also cannot reach the `#[pyfunction]` guard itself (it is
//! `python`-feature-gated and needs libpython). They pin the LAW the guard
//! applies — the envelope predicate's exact boundaries, and the fact that
//! every envelope-bounded input is overflow-free — so a future edit that
//! widens the predicate or drops a `require_ts_envelope` call fails a named
//! test here or in the Python seam suite.

use proptest::prelude::*;
// The crate-root re-exports (`lib.rs:48-51`) — the same public surface
// `python.rs` and TickVault consume, so this file pins the law through the
// shipped API rather than a module-internal path.
use brutex_feed::{MAX_SAFE_TS_US, expected_bar_instant, ts_in_envelope};

/// The envelope predicate's exact boundaries — the values a widening edit
/// would move.
#[test]
fn envelope_boundaries_are_exact() {
    assert!(
        !ts_in_envelope(-1),
        "a negative epoch-µs is out of envelope"
    );
    assert!(!ts_in_envelope(i64::MIN), "i64::MIN is out of envelope");
    assert!(ts_in_envelope(0), "the epoch instant itself is in envelope");
    assert!(ts_in_envelope(1), "1 µs past epoch is in envelope");
    assert!(
        ts_in_envelope(MAX_SAFE_TS_US - 1),
        "the last in-envelope µs must be accepted"
    );
    assert!(
        !ts_in_envelope(MAX_SAFE_TS_US),
        "the envelope is half-open: MAX_SAFE_TS_US itself is OUT"
    );
    assert!(!ts_in_envelope(i64::MAX), "i64::MAX is out of envelope");
}

/// The load-bearing exhibit: the pure kernel DOES overflow on an
/// out-of-envelope tuple, so the guard is not decoration.
///
/// The tuple is hand-derived to reach the label expression: `seg_end >=
/// seg_start` (not degenerate), `step_us != 0` (the intraday arm),
/// `seg_end >= open` and `seg_start < close` (the session overlaps), and
/// `seg_start > open` (the ELSE branch that computes the label). With
/// `open_us = i64::MIN` the very first op — `open_us - seg_start_us` —
/// overflows.
///
/// Under the TEST profile this is a catchable panic; under the shipped
/// release profile the same overflow is an abort. See the module docstring.
#[test]
fn unguarded_kernel_really_does_overflow() {
    // open_us = i64::MIN is out of envelope, so the #[pyfunction] guard
    // refuses this call in production. Reached directly, it overflows.
    let sessions = vec![(0_i64, i64::MIN, 2_i64)];
    let outcome = std::panic::catch_unwind(|| expected_bar_instant(1, 1, 1, 0, &sessions));
    let err = outcome.expect_err(
        "the UNGUARDED kernel must overflow on an out-of-envelope open_us — \
         if this ever returns a value, the arithmetic changed and the DEC's \
         hazard claim must be re-derived (DEC-FFI-MAGNITUDE-CEILING-001)",
    );
    let message = err
        .downcast_ref::<String>()
        .map(String::as_str)
        .or_else(|| err.downcast_ref::<&str>().copied())
        .unwrap_or("<non-string panic payload>");
    assert!(
        message.contains("overflow"),
        "the panic must be an arithmetic overflow, not some other failure; got {message:?}"
    );
    // Sanity: the tuple really is out of envelope, i.e. the guard covers it.
    assert!(
        !ts_in_envelope(i64::MIN),
        "the exhibit is only meaningful if the guard rejects this open_us"
    );
}

// The guarded domain is overflow-free — the claim the DEC makes, asserted
// rather than narrated.
//
// The generators draw every integer from the envelope the `#[pyfunction]`
// face enforces, so a panic in either property would mean the bound argument
// in `ts_in_envelope`'s docstring is wrong. The test profile enables
// `overflow-checks`, so an overflow shows up as a test failure.
//
// (Plain `//` comments, not `///`: rustdoc does not document items produced
// by a macro invocation, so a doc comment here is an `unused_doc_comments`
// warning — and CI runs clippy with `-D warnings`.)
proptest! {
    #![proptest_config(ProptestConfig::with_cases(512))]

    #[test]
    fn envelope_bounded_inputs_never_overflow(
        seg_start_us in 0i64..MAX_SAFE_TS_US,
        seg_end_us in 0i64..MAX_SAFE_TS_US,
        step_us in 0i64..MAX_SAFE_TS_US,
        now_us in 0i64..MAX_SAFE_TS_US,
        stamp_us in 0i64..MAX_SAFE_TS_US,
        open_us in 0i64..MAX_SAFE_TS_US,
        close_us in 0i64..MAX_SAFE_TS_US,
    ) {
        // The verdict itself is not asserted here (that is
        // `series_tests.rs`' job) — only that computing it cannot overflow.
        let _verdict = expected_bar_instant(
            seg_start_us,
            seg_end_us,
            step_us,
            now_us,
            &[(stamp_us, open_us, close_us)],
        );
    }

    // The same, biased HARD at the label branch: `seg_start > open` (the ELSE
    // arm), a tiny `step_us` (the largest quotient), and both timestamps near
    // the envelope ceiling (the largest operands) — the worst case for the
    // multiply and the final add.
    #[test]
    fn worst_case_label_arithmetic_never_overflows(
        open_us in 0i64..1_000_000i64,
        step_us in 1i64..4i64,
        gap_us in (MAX_SAFE_TS_US - 1_000_000)..MAX_SAFE_TS_US,
    ) {
        let seg_start_us = gap_us; // > open_us ⇒ the ELSE branch
        let seg_end_us = MAX_SAFE_TS_US - 1; // ⇒ seg_end >= open, not degenerate
        let close_us = MAX_SAFE_TS_US - 1; // ⇒ seg_start < close, session overlaps
        let _verdict = expected_bar_instant(
            seg_start_us,
            seg_end_us,
            step_us,
            0,
            &[(0, open_us, close_us)],
        );
    }
}
