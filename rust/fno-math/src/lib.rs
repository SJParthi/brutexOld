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

/// DEC-METRICS-RUST-COMPUTE-FOLD-001: the metrics-FOLD kernel — the Rust
/// mirror of `brutex.metrics.compute._fold_trades` (the Slice-0 fold layer;
/// the libm-carrying combine tail deliberately stays in Python).
pub mod metrics_fold;
/// Slice-4 PR-1 (`feat(ranking): Rust ranking conversion`): the Rust twins
/// of `brutex.ranking`'s rank-time math — gates + gate reasons, z-scores,
/// composite term inputs/fold, the DSR z-term, the rank-order index
/// permutation, batched overfit verdicts, and the correctly-rounded
/// i64/i64 division fast path. Pure rlib-style cores + thin `#[pyfunction]`
/// wrappers; default-ON behind `BRUTEX_RANKING_RUST` on the Python side
/// (DEC-RUST-DEFAULT-ON-WAVE-001, MIGRATION.md #2215 — `=0` is the kill
/// switch).
pub mod ranking;

/// DEC-SWEEP-ENUM-RUST-001 (#1738): sweep-enumeration kernel — the RANGED
/// `enumerate_prefilter` surface (design-of-record
/// `docs/design/sweep_core/DESIGN_FINAL_RUST_SWEEP_CORE.md` §1.3): fused
/// k-of-bits enumeration (`iter_masks_k_of_bits`) + conflict-pair prune
/// (`has_conflict`) + Stage-1B min-hits count over a resident condition-major
/// transposed-bitplane substrate, unranked via the LEX-RANK combinatorial
/// number system, emitting kept masks in the exact `itertools.combinations`
/// lexicographic order the sweep's durable bytes depend on. Default-ON
/// behind `BRUTEX_SWEEP_RUST_ENUM` (DEC-RUST-DEFAULT-ON-WAVE-001,
/// MIGRATION.md #2215 — `=0` is the kill switch; Python wrapper
/// `brutex/engine/rust_enum.py`).
pub mod enum_kernel;

// --- exec (slice 7 / P8, DEC-EXEC-RUST-CORE-001, MIGRATION.md #2212) ---
/// Options/futures execution integer cores: money-law primitives, the
/// worst-case fill law (DEC-FILL-WORST-CASE-ONLY-001), both charge stacks,
/// and the post-I/O materialize verdict tails — exact-integer twins of the
/// Python oracles in `brutex.costs` / the two executors; rates cross the
/// FFI as ARGUMENTS (Python stays the sole rate/refusal authority).
pub mod exec;
// --- end exec (slice 7 / P8) ---

/// DEC-METRICS-RUST-STATS-001 (slice 6): Stage-3/4 statistics kernels — the
/// sequential/integer heavy loops of the Stage-3/4 statistical filters
/// (CPCV fold pass-count in PR-1; later PRs append inside the same module).
/// Python keeps every numpy pairwise reduction, libm call and raise.
pub mod stats;

/// DEC-OPTCOST-TAILS-RUST-001: options-cost TAIL kernels — the pure decision
/// math of the regime-rate lookup (`brutex/costs/constants.py`), moneyness
/// classification (`brutex/data/moneyness.py`) and strike/lot/expiry rules
/// (`brutex/options/`). Ints-only FFI; every refusal stays a code Python
/// materializes with today's exact exception bytes (VERIFIED-RATES-ONLY,
/// DEC-ETC-REGIME-HISTORY-001).
pub mod optcost;

/// LANE W1 (DEC-RANK-PARETO-PERSIST-001 / DEC-RANK-QUOTA-PERSIST-1000-001 /
/// DEC-RANK-DD-BAND-ORDER-001): born-Rust multi-key selection kernels —
/// ε-Pareto skyline, per-axis bounded top-M heaps, DD-band quantize. The
/// Python twin (`brutex.ranking.multi_select`) is the parity ORACLE.
pub mod rank_select;

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
    m.add_function(wrap_pyfunction!(metrics_fold::metrics_fold_recency, m)?)?;
    m.add_function(wrap_pyfunction!(metrics_fold::metrics_fold_trades, m)?)?;
    // ── DEC-SWEEP-ENUM-RUST-001 (#1738) — sweep-enum kernel surface ──
    // (fenced additive block: unmerged sibling slices share this surface;
    // whoever lands second reconciles trivially. The interim free function
    // `sweep_enum_conflict_free` was REPLACED pre-merge by the ranged
    // resident classes — the design-of-record §1.3 surface.)
    m.add_class::<enum_kernel::ResidentSweepEnum>()?;
    m.add_class::<enum_kernel::ResidentSweepEnumU128>()?;
    // ── end DEC-SWEEP-ENUM-RUST-001 block ──
    m.add_function(wrap_pyfunction!(python::popcount_bytes, m)?)?;
    // ── slice-4 ranking.rs registrations (feat(ranking): Rust ranking
    //    conversion) — a comment-fenced per-slice block per the slice-4 /
    //    slice-6 separate-registration agreement: slice 6 adds its own
    //    stats block AFTER this one; never interleave slices inside a
    //    block (lib.rs is a sequenced shared file — first merger wins,
    //    the other rebases; the fences make the rebase mechanical). ──
    m.add_function(wrap_pyfunction!(ranking::ranking_composite_scores, m)?)?;
    m.add_function(wrap_pyfunction!(ranking::ranking_composite_term_inputs, m)?)?;
    m.add_function(wrap_pyfunction!(ranking::ranking_cr_div_i64, m)?)?;
    m.add_function(wrap_pyfunction!(ranking::ranking_dsr_z_contributions, m)?)?;
    m.add_function(wrap_pyfunction!(ranking::ranking_gate_reasons, m)?)?;
    m.add_function(wrap_pyfunction!(ranking::ranking_overfit_verdicts, m)?)?;
    m.add_function(wrap_pyfunction!(ranking::ranking_passes_gates, m)?)?;
    m.add_function(wrap_pyfunction!(ranking::ranking_rank_order, m)?)?;
    m.add_function(wrap_pyfunction!(ranking::ranking_zscores, m)?)?;
    // ── end slice-4 ranking.rs registrations ──
    // --- exec (slice 7 / P8, DEC-EXEC-RUST-CORE-001, MIGRATION.md #2212) ---
    m.add_function(wrap_pyfunction!(exec::ffi::exec_round_to_paisa, m)?)?;
    m.add_function(wrap_pyfunction!(exec::ffi::exec_round_to_rupee, m)?)?;
    m.add_function(wrap_pyfunction!(exec::ffi::exec_apply_slippage, m)?)?;
    m.add_function(wrap_pyfunction!(exec::ffi::exec_apply_futures_fill, m)?)?;
    m.add_function(wrap_pyfunction!(exec::ffi::exec_option_charge_stack, m)?)?;
    m.add_function(wrap_pyfunction!(exec::ffi::exec_futures_charge_stack, m)?)?;
    m.add_function(wrap_pyfunction!(exec::ffi::exec_options_materialize, m)?)?;
    m.add_function(wrap_pyfunction!(exec::ffi::exec_futures_materialize, m)?)?;
    // --- end exec (slice 7 / P8) ---
    // ---- stats module (slice 6, DEC-METRICS-RUST-STATS-001): Stage-3/4
    // statistics kernels. Additive registration only; existing registrations
    // above are untouched. Later slice-6 PRs append inside this fence.
    m.add_function(wrap_pyfunction!(stats::cpcv::stats_cpcv_pass_count, m)?)?;
    // ── DEC-METRICS-RUST-STATS-002 (slice 6, PR-2 — the stats tails):
    // DSR (PSR + SR_0), PBO/CSCV and WFO kernels. Additive appends only,
    // inside the same slice-6 fence; existing registrations untouched.
    m.add_function(wrap_pyfunction!(stats::deflated_sharpe::stats_psr, m)?)?;
    m.add_function(wrap_pyfunction!(
        stats::deflated_sharpe::stats_expected_max_sharpe,
        m
    )?)?;
    m.add_function(wrap_pyfunction!(stats::pbo::stats_pbo_cscv, m)?)?;
    m.add_function(wrap_pyfunction!(stats::wfo::stats_wfo_pass_count, m)?)?;
    // DEC-RANK-CVAR-TAIL-GATE-001: the integer-exact CVaR worst-k tail fold
    // behind the `BRUTEX_RANK_CVAR_RUST` seam (`brutex.ranking.cvar_gate`).
    // Default-ON since DEC-RUST-DEFAULT-ON-WAVE-001 (MIGRATION.md #2215);
    // `=0` is the kill switch (runtime-verified: `cvar_rust_enabled()` is
    // True with the flag unset).
    m.add_function(wrap_pyfunction!(stats::cvar::stats_cvar_tail_fold, m)?)?;
    // DEC-ALPHA-BUYHOLD-METRIC-001 — the ALPHA-vs-BUY&HOLD benchmark scalar
    // (fenced additive registration; NO crate version bump — the #1942
    // precedent, run identity / `_indicator_version` untouched).
    m.add_function(wrap_pyfunction!(stats::alpha::alpha_vs_buyhold_pct, m)?)?;
    // ---- end stats module ----
    // ---- optcost module (DEC-OPTCOST-TAILS-RUST-001): options-cost tail
    // kernels (regime rates / moneyness / strike-lot-expiry rules).
    // Additive registration only; existing registrations above are
    // untouched. Consumed by the BRUTEX_OPTCOST_TAILS_RUST seam — default-ON
    // since DEC-RUST-DEFAULT-ON-WAVE-001 (MIGRATION.md #2215), `=0` is the
    // kill switch (runtime-verified: `optcost_rust_enabled()` is True with
    // the flag unset).
    m.add_function(wrap_pyfunction!(optcost::regime::optcost_regime_rate, m)?)?;
    m.add_function(wrap_pyfunction!(optcost::lots::optcost_history_lookup, m)?)?;
    m.add_function(wrap_pyfunction!(
        optcost::expiry::optcost_next_weekly_expiry,
        m
    )?)?;
    m.add_function(wrap_pyfunction!(
        optcost::expiry::optcost_next_monthly_expiry,
        m
    )?)?;
    m.add_function(wrap_pyfunction!(
        optcost::moneyness::optcost_moneyness_classify,
        m
    )?)?;
    m.add_function(wrap_pyfunction!(optcost::moneyness::optcost_atm_offset, m)?)?;
    m.add_function(wrap_pyfunction!(
        optcost::moneyness::optcost_strike_step,
        m
    )?)?;
    m.add_function(wrap_pyfunction!(optcost::strike::optcost_offset_strike, m)?)?;
    m.add_function(wrap_pyfunction!(
        optcost::strike::optcost_strike_offset_from_atm,
        m
    )?)?;
    m.add_function(wrap_pyfunction!(
        optcost::strike::optcost_grid_around_atm,
        m
    )?)?;
    m.add_function(wrap_pyfunction!(optcost::strike::optcost_round_to_atm, m)?)?;
    // ---- end optcost module ----
    // ── LANE W1 (DEC-RANK-PARETO-PERSIST-001 / DEC-RANK-QUOTA-PERSIST-1000-001 /
    // DEC-RANK-DD-BAND-ORDER-001): the multi-key selection kernels (fenced
    // additive registration block — keep-both union seam with sibling
    // fno-math lanes). ──
    m.add_function(wrap_pyfunction!(rank_select::rank_pareto_frontier, m)?)?;
    m.add_function(wrap_pyfunction!(rank_select::rank_axis_topm, m)?)?;
    m.add_function(wrap_pyfunction!(rank_select::rank_dd_band_indices, m)?)?;
    // ── end LANE W1 registration block ──
    Ok(())
}
