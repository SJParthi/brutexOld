//! PyO3 wrappers for the exec cores (DEC-EXEC-RUST-CORE-001, MIGRATION.md
//! #1963) — every export prefixed `exec_` (the `brutex.math` facade
//! collision law). Wrappers ONLY `.map_err(PyValueError::new_err)`; the pure
//! cores return `Result<_, String>` so `cargo test` links zero libpython
//! (the house `metrics.rs` split — wrappers live beside the module, NOT in
//! `src/python.rs`, keeping the collided registration surface minimal).
//!
//! Wire shapes (NO pyclasses — the Python seam immediately re-wraps into the
//! existing frozen dataclasses, whose `__post_init__` re-validates every
//! value): a charge stack crosses as the PINNED 17-slot i64 list
//! (`ChargeStack::to_wire` order = the `brutex.costs.types.ChargeStack`
//! NamedTuple order); materialize verdicts cross as
//! `(kind, values | None)` with kind ∈ {"stale_bar", "tick_floor", "trade"}
//! and the futures trade list carrying `[…17 wire…, mae_paisa, mfe_paisa]`
//! (19 slots). Fills take the fill bars' FOUR extremes (entry high/low +
//! exit high/low — the DEC-FILL-WORST-CASE-ONLY-001 adverse anchors); the
//! stale gate is UNCONDITIONAL (the former `reject_stale` knob is DELETED).
//! Dates, Decimals, datetimes and callbacks never cross.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

use crate::exec::costs_futures::futures_charge_stack;
use crate::exec::costs_options::option_charge_stack;
use crate::exec::exec_futures::{BarVerdictFutures, futures_materialize_core};
use crate::exec::exec_options::{BarVerdict, options_materialize_core};
use crate::exec::fills::{apply_futures_fill, apply_slippage};
use crate::exec::money::{round_to_paisa, round_to_rupee};

/// Verdict discriminants — the seam maps them onto the executor enums.
const KIND_STALE_BAR: &str = "stale_bar";
const KIND_TICK_FLOOR: &str = "tick_floor";
const KIND_TRADE: &str = "trade";

/// `exec::money::round_to_paisa` (ADVERSE CEILING at scale 10^7 —
/// DEC-FILL-WORST-CASE-ONLY-001 permanent levy rounding).
#[pyfunction]
pub fn exec_round_to_paisa(amount_x_bps_x100: i128) -> PyResult<i64> {
    round_to_paisa(amount_x_bps_x100).map_err(PyValueError::new_err)
}

/// `exec::money::round_to_rupee` (ADVERSE CEILING to a multiple of 100).
#[pyfunction]
pub fn exec_round_to_rupee(amount_paisa: i64) -> PyResult<i64> {
    round_to_rupee(amount_paisa).map_err(PyValueError::new_err)
}

/// `exec::fills::apply_slippage` (worst-case bar-extreme anchors) →
/// `(buy_fill, sell_fill, realized_per_share)`.
#[pyfunction]
pub fn exec_apply_slippage(
    entry_high_paisa: i64,
    entry_low_paisa: i64,
    exit_high_paisa: i64,
    exit_low_paisa: i64,
    is_long: bool,
) -> PyResult<(i64, i64, i64)> {
    apply_slippage(
        entry_high_paisa,
        entry_low_paisa,
        exit_high_paisa,
        exit_low_paisa,
        is_long,
    )
    .map_err(PyValueError::new_err)
}

/// `exec::fills::apply_futures_fill` (worst-case bar-extreme anchors) →
/// `(buy_fill, sell_fill, realized_per_unit)`.
#[pyfunction]
pub fn exec_apply_futures_fill(
    entry_high_paisa: i64,
    entry_low_paisa: i64,
    exit_high_paisa: i64,
    exit_low_paisa: i64,
    is_long: bool,
) -> PyResult<(i64, i64, i64)> {
    apply_futures_fill(
        entry_high_paisa,
        entry_low_paisa,
        exit_high_paisa,
        exit_low_paisa,
        is_long,
    )
    .map_err(PyValueError::new_err)
}

/// `exec::costs_options::option_charge_stack` → the pinned 17-slot wire list.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn exec_option_charge_stack(
    entry_high_paisa: i64,
    entry_low_paisa: i64,
    exit_high_paisa: i64,
    exit_low_paisa: i64,
    is_long: bool,
    lot_size: i64,
    n_lots: i64,
    brokerage_round_trip_paisa: i64,
    stt_rate: i64,
    etc_rate: i64,
    sebi_rate: i64,
    ipft_rate: i64,
    stamp_rate: i64,
    gst_rate: i64,
) -> PyResult<Vec<i64>> {
    option_charge_stack(
        entry_high_paisa,
        entry_low_paisa,
        exit_high_paisa,
        exit_low_paisa,
        is_long,
        lot_size,
        n_lots,
        brokerage_round_trip_paisa,
        stt_rate,
        etc_rate,
        sebi_rate,
        ipft_rate,
        stamp_rate,
        gst_rate,
    )
    .map(|stack| stack.to_wire().to_vec())
    .map_err(PyValueError::new_err)
}

/// `exec::costs_futures::futures_charge_stack` → the pinned 17-slot wire list.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn exec_futures_charge_stack(
    entry_high_paisa: i64,
    entry_low_paisa: i64,
    exit_high_paisa: i64,
    exit_low_paisa: i64,
    is_long: bool,
    lot_size: i64,
    n_lots: i64,
    cost_free: bool,
    brokerage_flat_paisa: i64,
    brokerage_turnover_bps_x100: Option<i64>,
    stt_rate: i64,
    etc_rate: i64,
    sebi_rate: i64,
    ipft_rate: i64,
    stamp_rate: i64,
    gst_rate: i64,
) -> PyResult<Vec<i64>> {
    futures_charge_stack(
        entry_high_paisa,
        entry_low_paisa,
        exit_high_paisa,
        exit_low_paisa,
        is_long,
        lot_size,
        n_lots,
        cost_free,
        brokerage_flat_paisa,
        brokerage_turnover_bps_x100,
        stt_rate,
        etc_rate,
        sebi_rate,
        ipft_rate,
        stamp_rate,
        gst_rate,
    )
    .map(|stack| stack.to_wire().to_vec())
    .map_err(PyValueError::new_err)
}

/// `exec::exec_options::options_materialize_core` →
/// `("stale_bar" | "tick_floor", None)` or `("trade", 17-slot wire list)`.
///
/// The two OPENS feed only the TICK_FLOOR gate; the four extremes feed the
/// worst-case fills.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn exec_options_materialize(
    entry_open_paisa: i64,
    exit_open_paisa: i64,
    entry_high_paisa: i64,
    entry_low_paisa: i64,
    exit_high_paisa: i64,
    exit_low_paisa: i64,
    entry_volume: i64,
    exit_volume: i64,
    is_long: bool,
    lot_size: i64,
    n_lots: i64,
    brokerage_round_trip_paisa: i64,
    stt_rate: i64,
    etc_rate: i64,
    sebi_rate: i64,
    ipft_rate: i64,
    stamp_rate: i64,
    gst_rate: i64,
) -> PyResult<(&'static str, Option<Vec<i64>>)> {
    let verdict = options_materialize_core(
        entry_open_paisa,
        exit_open_paisa,
        entry_high_paisa,
        entry_low_paisa,
        exit_high_paisa,
        exit_low_paisa,
        entry_volume,
        exit_volume,
        is_long,
        lot_size,
        n_lots,
        brokerage_round_trip_paisa,
        stt_rate,
        etc_rate,
        sebi_rate,
        ipft_rate,
        stamp_rate,
        gst_rate,
    )
    .map_err(PyValueError::new_err)?;
    Ok(match verdict {
        BarVerdict::StaleBar => (KIND_STALE_BAR, None),
        BarVerdict::TickFloor => (KIND_TICK_FLOOR, None),
        BarVerdict::Trade(stack) => (KIND_TRADE, Some(stack.to_wire().to_vec())),
    })
}

/// `exec::exec_futures::futures_materialize_core` →
/// `("stale_bar", None)` or `("trade", [17 wire slots, mae, mfe])` (19).
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn exec_futures_materialize(
    entry_high_paisa: i64,
    entry_low_paisa: i64,
    exit_high_paisa: i64,
    exit_low_paisa: i64,
    entry_volume: i64,
    exit_volume: i64,
    path_highs_paisa: Vec<i64>,
    path_lows_paisa: Vec<i64>,
    is_long: bool,
    lot_size: i64,
    n_lots: i64,
    cost_free: bool,
    brokerage_flat_paisa: i64,
    brokerage_turnover_bps_x100: Option<i64>,
    stt_rate: i64,
    etc_rate: i64,
    sebi_rate: i64,
    ipft_rate: i64,
    stamp_rate: i64,
    gst_rate: i64,
) -> PyResult<(&'static str, Option<Vec<i64>>)> {
    let verdict = futures_materialize_core(
        entry_high_paisa,
        entry_low_paisa,
        exit_high_paisa,
        exit_low_paisa,
        entry_volume,
        exit_volume,
        &path_highs_paisa,
        &path_lows_paisa,
        is_long,
        lot_size,
        n_lots,
        cost_free,
        brokerage_flat_paisa,
        brokerage_turnover_bps_x100,
        stt_rate,
        etc_rate,
        sebi_rate,
        ipft_rate,
        stamp_rate,
        gst_rate,
    )
    .map_err(PyValueError::new_err)?;
    Ok(match verdict {
        BarVerdictFutures::StaleBar => (KIND_STALE_BAR, None),
        BarVerdictFutures::Trade(out) => {
            let mut wire = out.stack.to_wire().to_vec();
            wire.push(out.mae_paisa);
            wire.push(out.mfe_paisa);
            (KIND_TRADE, Some(wire))
        }
    })
}
