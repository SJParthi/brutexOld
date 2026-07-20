//! # fno-math
//!
//! Minimal Rust scaffold reserved for BruteX Phase 2/3 hot kernels
//! (per `CLAUDE.md` §4 + §5 + `docs/MIGRATION.md` Decision #68).
//!
//! BruteX is a **Track 1 signal-only backtest** over Groww historical
//! data for 4 Indian spot indices (NIFTY 50, BANKNIFTY, SENSEX, INDIA
//! VIX). The crate currently contains only the `compat` types module
//! (Exchange / ExchangeSegment / NSE trading calendar) used by Python
//! integration. The Greeks / indicators / strategies / live-tick
//! modules previously inlined here were tickvault-extracted residue
//! and were removed per DEC-P0-025 (PR #47) — see `docs/MIGRATION.md`.
//!
//! ## Currently exported
//!
//! - [`compat`] — Exchange enum, ExchangeSegment, NSE trading calendar,
//!   instrument-type codecs, error types.
//! - [`math`] — empty placeholder for future Phase 2 utilities.
//!
//! ## Phase 2 (MG-043) plan
//!
//! When Track 1 needs Rust-accelerated indicators (Phase 2 / Phase 3
//! brute-force engine hot kernel), they will be added here as
//! BruteX-native modules — not by re-extracting from any other repo.
//!
//! This crate intentionally contains no I/O, no async, no logging, and
//! no allocations on hot paths. Every public function must be a
//! deterministic pure function from inputs to outputs.

#![cfg_attr(not(test), deny(clippy::unwrap_used))]
#![cfg_attr(not(test), deny(clippy::expect_used))]
#![cfg_attr(not(test), deny(clippy::print_stdout))]
#![cfg_attr(not(test), deny(clippy::print_stderr))]
#![cfg_attr(not(test), deny(clippy::dbg_macro))]
#![deny(unsafe_code)]
#![allow(missing_docs)]
#![allow(clippy::excessive_precision)]
#![allow(clippy::inconsistent_digit_grouping)]
#![allow(clippy::type_complexity)]
#![allow(clippy::bind_instead_of_map)]
#![allow(clippy::unnecessary_map_or)]
#![allow(clippy::assertions_on_constants)]
#![allow(clippy::needless_borrow)]
#![allow(clippy::collapsible_if)]
#![allow(clippy::too_many_arguments)]
#![allow(clippy::manual_range_contains)]
#![allow(clippy::field_reassign_with_default)]
#![allow(clippy::unreadable_literal)]

/// Domain types shared across the Rust hot kernel: Exchange enum,
/// ExchangeSegment, NSE trading calendar, instrument-type codecs,
/// error types. Future Phase 2 / Phase 3 modules consume these.
pub mod compat;

/// Reserved for Phase 2 math utilities (rolling stats, distributions,
/// solvers). Currently empty — populated when MG-043 needs it.
pub mod math;

/// DEC-ENUM-KERNEL-U128-001 (hoist prerequisite): the width-generic
/// `MaskWord` trait — HOISTED verbatim from `trade_sim` (where
/// DEC-TRADE-SIM-U128-KERNEL-001 introduced it module-internal) so the
/// Stage-2 enum/count kernels and the trade-sim walk genericize over the
/// SAME shared core (CLAUDE.md §11 COMMON). Crate-internal (`pub(crate)`).
pub(crate) mod mask_word;

/// DEC-P0-050 (PR-K): PyO3 bridge to Python. Future Stage 1B
/// AND-mask kernel, combo_hash blake3 migration, and other
/// performance-critical Rust functions get registered here.
pub mod python;

/// DEC-SWEEP-RUST-TRADESIM-001 (#560): resident Stage-2 trade-simulation kernel
/// — the per-bar SL/TP/forced-exit state machine (Python `TradeSimWalker`) ported
/// to Rust + fused with the per-mask entry projection over a resident bar window.
pub mod trade_sim;

/// DEC-SWEEP-RUST-METRICS-001 (#1186): Phase-B sweep-metrics kernel — the fused
/// per-mask equity fold + drawdown scan (`build_equity_curve` +
/// `_drawdown_stats`), bit-identical to the Python oracle inside the 2^52
/// f64-exactness envelope the Python wrapper pre-guards.
pub mod metrics;

/// DEC-METRICS-RUST-STATS-001 (slice 6): Stage-3/4 statistics kernels — the
/// sequential/integer heavy loops of the Stage-3/4 statistical filters
/// (CPCV fold pass-count in PR-1; later PRs append inside the same module).
/// Python keeps every numpy pairwise reduction, libm call and raise.
pub mod stats;
/// DEC-AUDIT-CHAIN-RUST-PORT-001 (slice 9, ADR-0019 / #1727): the orchestrator
/// SHA-256 audit-chain serializer — CPython-exact float repr + canonical JSON
/// + `|`-joined preimage + on-disk line bytes, parity-gated on the frozen
/// `tests/golden/fixtures/audit_chain_parity_*` fixtures. File I/O stays Python.
pub mod audit_chain;

/// DEC-CALENDAR-VERDICT-RUST-001 (slice 9, ADR-0019 / #1727): the NSE/BSE
/// calendar VERDICT engine — decision logic only; the calendar DATA stays in
/// `brutex.calendars` (ADR-0012 D8) and crosses the FFI as runtime config,
/// parity-gated on the frozen 2,557-day calendar verdict-table golden.
pub mod calendar_verdict;

/// DEC-RATE-MATH-RUST-001 (slice 9, ADR-0019 / #1727a): the adaptive
/// token-bucket's FOUR pure IEEE f64 expressions (refill / consume /
/// retry-wait / set-rate post-state) — bit-exact CPython min/max order
/// semantics; locks, clock, deque window, and every cap/ladder VALUE stay
/// Python (§2 Rule 6).
pub mod rate_math;

/// Crate version (semver). Must be bumped on any breaking change.
pub const VERSION: &str = env!("CARGO_PKG_VERSION");

/// BLAKE3 hex digest of this crate's source (`Cargo.toml` + `src/**/*.rs`),
/// baked at build time by `build.rs` under the shared FRAMING v1 of
/// `rust/source_digest_core.rs` (DEC-RUST-SOURCE-DIGEST-TWIN-001,
/// `docs/MIGRATION.md` #1258). The PyO3 module exports it as
/// `__source_digest__`; `brutex doctor`'s `wheel_source_digest_fno_math`
/// stage compares it against the Python twin's (`brutex/wheel_digest.py`)
/// fresh recompute over the repo checkout, catching a LOCAL wheel built from
/// older source at the same crate version (same-version behavioural drift).
pub const SOURCE_DIGEST: &str = env!("BRUTEX_SOURCE_DIGEST");

/// Serial-vs-parallel dispatch predicate (DEC-RUST-RAYON-SUPERMASK-001):
/// batches strictly below `threshold` mask·bar ops stay SERIAL (small-N
/// O(1)-canary protection); at/above they fan out on rayon. Extracted so the
/// boundary is directly unit-testable — the serial and parallel arms are
/// output-identical by design, so no output test can kill a comparison flip
/// at the call sites (DEC-MUTATION-RUNTIME-003).
#[inline]
pub(crate) fn below_parallel_threshold(work: u64, threshold: u64) -> bool {
    work < threshold
}

#[cfg(test)]
mod dispatch_tests {
    use super::below_parallel_threshold;

    #[test]
    fn parallel_dispatch_boundary_is_exclusive() {
        assert!(below_parallel_threshold(0, 1 << 20));
        assert!(below_parallel_threshold((1 << 20) - 1, 1 << 20)); // kills < -> == / >
        assert!(!below_parallel_threshold(1 << 20, 1 << 20)); // kills < -> <=
        assert!(!below_parallel_threshold(u64::MAX, 1 << 20)); // saturating arm
    }
}

// ────────────────────────────────────────────────────────────────────────
// PyO3 module entry point (DEC-P0-050 / PR-K; DEC-P0-055 / PR-N+2)
// ────────────────────────────────────────────────────────────────────────
//
// `#[pymodule]` registers the `_fno_math` Python submodule. Maturin
// produces a cdylib whose extern "C" `PyInit__fno_math` symbol matches
// this function name. Under the DEC-P0-055 mixed Python+Rust layout,
// the cdylib is installed at `site-packages/fno_math/_fno_math.<abi>.so`
// and `site-packages/fno_math/__init__.py` re-exports its symbols, so
// downstream callers continue to write `import fno_math; fno_math.X`.

use pyo3::prelude::*;

#[pymodule]
fn _fno_math(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", VERSION)?;
    // Dunder-named deliberately: the doctor's wheel-freshness symbol-set
    // check (and EXPECTED_BUILD.json) covers only non-underscore names, so
    // this build stamp can never trip the symbol-surface comparison
    // (DEC-RUST-SOURCE-DIGEST-TWIN-001).
    m.add("__source_digest__", SOURCE_DIGEST)?;
    m.add_function(wrap_pyfunction!(python::add_u64, m)?)?;
    m.add_function(wrap_pyfunction!(python::bitand_count_u64s, m)?)?;
    m.add_function(wrap_pyfunction!(python::bitcount_u64s, m)?)?;
    m.add_function(wrap_pyfunction!(python::blake3_hash, m)?)?;
    m.add_function(wrap_pyfunction!(python::count_supermask_u64s, m)?)?;
    m.add_function(wrap_pyfunction!(python::count_supermasks_u64s, m)?)?;
    m.add_function(wrap_pyfunction!(python::count_supermasks_u64s_nthreads, m)?)?;
    m.add_class::<python::ResidentBars>()?;
    // ── DEC-ENUM-KERNEL-U128-001: the u128 enum/count kernel twins ──
    // (fenced additive registration block — keep-both union seam with
    // sibling fno-math lanes; the twins of count_supermask_u64s /
    // count_supermasks_u64s / ResidentBars over native Vec<u128>.)
    m.add_function(wrap_pyfunction!(python::count_supermask_u128s, m)?)?;
    m.add_function(wrap_pyfunction!(python::count_supermasks_u128s, m)?)?;
    m.add_class::<python::ResidentBarsU128>()?;
    // ── end DEC-ENUM-KERNEL-U128-001 registration block ──
    m.add_class::<trade_sim::ResidentSweepSim>()?;
    m.add_class::<trade_sim::ResidentSweepSimU128>()?;
    m.add_function(wrap_pyfunction!(metrics::sweep_drawdown_stats, m)?)?;
    m.add_function(wrap_pyfunction!(python::popcount_bytes, m)?)?;
    // ---- stats module (slice 6, DEC-METRICS-RUST-STATS-001): Stage-3/4
    // statistics kernels. Additive registration only; existing registrations
    // above are untouched. Later slice-6 PRs append inside this fence.
    m.add_function(wrap_pyfunction!(stats::cpcv::stats_cpcv_pass_count, m)?)?;
    // ---- end stats module ----
    // ── slice-9 audit-chain kernels (DEC-AUDIT-CHAIN-RUST-PORT-001, ADR-0019) ──
    // The orchestrator SHA-256 chain's serializer + hash + line bytes; the
    // envelope-error type routes out-of-model payloads back to the Python oracle.
    m.add(
        "AuditEnvelopeError",
        m.py().get_type::<audit_chain::AuditEnvelopeError>(),
    )?;
    m.add_function(wrap_pyfunction!(audit_chain::audit_entry_hash_py, m)?)?;
    m.add_function(wrap_pyfunction!(audit_chain::audit_entry_line_py, m)?)?;
    m.add_function(wrap_pyfunction!(audit_chain::py_float_repr_py, m)?)?;
    m.add_function(wrap_pyfunction!(audit_chain::canonical_json_py, m)?)?;
    // ── slice-9 calendar-verdict kernels (DEC-CALENDAR-VERDICT-RUST-001) ──
    // The NSE/BSE verdict engine (config supplied by brutex.calendars at
    // runtime — ADR-0012 D8) + the four module-level session-law scalars
    // (`_s` suffix = seconds-of-day, avoiding nse.py name shadowing).
    m.add_class::<calendar_verdict::CalendarVerdictEngine>()?;
    m.add_function(wrap_pyfunction!(
        calendar_verdict::expected_bars_per_day_for_tf_py,
        m
    )?)?;
    m.add_function(wrap_pyfunction!(
        calendar_verdict::is_forced_exit_bar_s_py,
        m
    )?)?;
    m.add_function(wrap_pyfunction!(
        calendar_verdict::forced_exit_seconds_s_py,
        m
    )?)?;
    m.add_function(wrap_pyfunction!(
        calendar_verdict::session_last_bar_complete_s_py,
        m
    )?)?;
    // ── slice-9 rate-limiter token math (DEC-RATE-MATH-RUST-001) ──
    // The AdaptiveTokenBucket's four pure f64 expressions; every cap /
    // floor / governor VALUE and the eps/isclose gates stay owned by
    // brutex/orchestrator/rate_limiter.py. The ONE ladder-adjacent literal
    // Rust-side is bucket_set_rate_state's `/ 2.0` downshift halving —
    // inherent to the ported set_rate expression itself (byte-identical to
    // the Python line it transcribes), not an independently tunable value.
    m.add_function(wrap_pyfunction!(rate_math::bucket_refill_py, m)?)?;
    m.add_function(wrap_pyfunction!(rate_math::bucket_consume_py, m)?)?;
    m.add_function(wrap_pyfunction!(rate_math::bucket_more_wait_py, m)?)?;
    m.add_function(wrap_pyfunction!(rate_math::bucket_set_rate_state_py, m)?)?;
    Ok(())
}
