//! Batch Stage-1A bar-bits kernel — one call per `(instrument, tf)` tuple.
//!
//! `DEC-STAGE1A-RUST-BATCH-001`: BruteX's Stage-1A matrix builder
//! (`brutex/engine/stage1a.py::Stage1AMatrixBuilder`) walks a bar feed in
//! Python calling `evaluate_with_levels` + `VwapState.update` +
//! `MarketStructureState.update` once PER BAR over the FFI. This module is the
//! batch twin: the Python prepass (`Stage1AMatrixBuilder.collect_batch_inputs`)
//! resolves every DECISION column with the builder's own roll/day/anchor
//! machinery — the Muhurat guard, the ADR-0012 D11 roll hard-reset, the
//! daily-history bound and the anchor resolution are NEVER re-implemented here
//! — and this core re-expresses only the integer state bookkeeping (running
//! day extremes, the prior-bar flag deque, per-contract state recreation)
//! around the SAME frozen cores: [`VwapState`], [`MarketStructureState`] and
//! [`evaluate_atoms`]. One `Vec<u128>` out, one mask per bar, input order.
//!
//! ## Bit-identity contract
//!
//! For every column set the prepass emits, the output is bit-identical to the
//! Python builder's `build()` — pinned by the cross-language golden fixture
//! (`tests/stage1a_batch_golden.rs`, Python-oracle-generated once, replayed
//! Rust-only), the dual-arm hypothesis parity suite
//! (`tests/property/test_stage1a_rust_batch_parity.py`) and the per-process
//! burn-in self-check in `brutex/engine/rust_stage1a.py`.
//!
//! ## Totality (feature-free core, `panic = "abort"` discipline)
//!
//! Every STRUCTURAL violation (length/day-table/`day_index` desync, an
//! out-of-domain `prior_n`/`fractal_k`) is an `Err(String)` — never a panic
//! (G-2: a desynced day table must never silently serve shifted anchors). The
//! VALUE envelope (price/level magnitudes, bps products, VWAP operands) is
//! enforced by the feature-gated PyO3 wrapper in `python.rs`, which reuses the
//! EXISTING boundary guards (`check_evaluate_inputs` / `bar_in_range` /
//! `vwap_update_guard`) in place, verbatim — this core deliberately duplicates
//! none of their constants. Inside that envelope the arithmetic here is the
//! evaluator's own (proven overflow-free under the guards — see
//! `MAX_BAR_PAISA`'s derivation) plus `max`/`min` folds that cannot overflow.
//!
//! ## Clear masks (G-1)
//!
//! The Python side passes the POSITIVE `ANCHOR_DEPENDENT_MASK` /
//! `FIB_DEPENDENT_MASK` / `APPLIES_TO_1DAY_MASK` constants and this core
//! complements in-width (`mask & !dep`) — Python's `mask & ~DEP` on an
//! arbitrary-precision int is identical for any mask `< 2^128` (unit-pinned
//! below), and the masks the evaluator emits occupy bits 0..=59 only.

use crate::evaluator::{IndicatorSnapshot, PriorBarHistory, ProximityParams, evaluate_atoms};
use crate::indicators::fib::FibLevels;
use crate::indicators::market_structure::MarketStructureState;
use crate::indicators::pivots::PivotLevels;
use crate::indicators::vwap::VwapState;

/// Entries per day-table pivot row — mirrors the FROZEN `evaluate_with_levels`
/// pivot ABI (`python.rs::PIVOT_LEVELS_LEN`; the feature-gated tests pin the
/// two constants equal): `[p, r1, r2, r3, s1, s2, s3, pdh, pdl, pdc, r5, s5]`.
pub const PIVOT_ROW_LEN: usize = 12;

/// Entries per day-table fib row — mirrors the FROZEN `evaluate_with_levels`
/// fib ABI (`python.rs::FIB_LEVELS_LEN`): `[retrace@pdh, r23, r38, r50, r62,
/// r78, retrace@pdl, up127, up162, up200, up262, dn127, dn162, dn200, dn262]`.
pub const FIB_ROW_LEN: usize = 15;

/// The valid `prior_n` domain (Group-6 look-back ∈ {1, 2, 3, 4} — the same
/// domain `Stage1AMatrixBuilder` sweeps; `docs/TRACK1_VOCABULARY.md` Group 6).
pub const MIN_PRIOR_N: usize = 1;
/// See [`MIN_PRIOR_N`].
pub const MAX_PRIOR_N: usize = 4;

/// The flat column/table view of one Stage-1A batch build.
///
/// Per-bar columns are all length `N` (bar order); the per-day tables carry
/// `D` rows keyed by `day_index`. Every field is prepass-RESOLVED on the
/// Python side (see the module docs) — this core never derives a calendar,
/// roll or anchor decision itself.
#[derive(Debug, Clone, Copy)]
pub struct Stage1aBatchInputs<'a> {
    /// Bar open (int64 paisa).
    pub open_paisa: &'a [i64],
    /// Bar high (int64 paisa).
    pub high_paisa: &'a [i64],
    /// Bar low (int64 paisa).
    pub low_paisa: &'a [i64],
    /// Bar close (int64 paisa).
    pub close_paisa: &'a [i64],
    /// HLC3 typical price, snapped Python-side via the canonical
    /// `_price_to_paisa` seam (the raw-Decimal law — never re-derived here).
    pub typical_paisa: &'a [i64],
    /// Bar volume.
    pub volume: &'a [i64],
    /// IST wall-clock seconds-of-day, prepass-resolved via the shared
    /// `trading_core.seconds_of_day` seam (DEC-TOD-SECONDS-RUST-SEAM-001).
    pub tod_seconds: &'a [u32],
    /// True for a 09:00-09:15 pre-open auction bar (VWAP-excluded).
    pub pre_open: &'a [bool],
    /// Engine-injected VWAP session key (spot: the constant 0; futures: the
    /// owning contract's expiry ordinal).
    pub session_key: &'a [u64],
    /// IST calendar-date ordinal of the bar.
    pub session_day: &'a [i64],
    /// True = the ADR-0012 D11 TOTAL per-contract hard reset fires BEFORE this
    /// bar (emitted by the SAME prepass cursor that drives
    /// `_roll_to_new_contract`).
    pub contract_reset: &'a [bool],
    /// Bar → row index into the day tables. Structurally validated: starts at
    /// 0, steps by 0 or 1, and consumes every day row (G-2).
    pub day_index: &'a [u32],
    /// Prepass-computed EMA(20) column (paisa; `None` during warm-up) —
    /// per-roll-segment `compute_ema` + `_ema_to_paisa`, Python-side.
    pub ema20_paisa: &'a [Option<i64>],
    /// Prepass-computed EMA(200) column — see [`Stage1aBatchInputs::ema20_paisa`].
    pub ema200_paisa: &'a [Option<i64>],
    /// `PIVOT_ROW_LEN * D` flattened pivot rows, evaluator ABI order.
    pub day_pivot_levels: &'a [i64],
    /// `FIB_ROW_LEN * D` flattened fib rows, evaluator ABI order.
    pub day_fib_levels: &'a [i64],
    /// Per-day pivot-anchor warm flag (`False` ⇒ clear every
    /// anchor-dependent bit — the first-day / post-roll cold start).
    pub day_has_pivot: &'a [bool],
    /// Per-day fib-anchor warm flag (`False` with a warm pivot ⇒ clear ONLY
    /// the Group-4 fib bits — DEC-NIFTY-FIB-AXIS-001 R-2).
    pub day_has_fib: &'a [bool],
    /// Group-2 pivot proximity tolerance (bps).
    pub pivot_bps: i64,
    /// Group-3 PDH/PDL proximity tolerance (bps).
    pub pdh_pdl_bps: i64,
    /// Group-4 fib proximity tolerance (bps).
    pub fib_bps: i64,
    /// Group-6 prior-bar look-back (∈ `MIN_PRIOR_N..=MAX_PRIOR_N`).
    pub prior_n: usize,
    /// Fractal half-window for [`MarketStructureState`] (`>= 1`; the pyclass
    /// default — and the only production value — is 2).
    pub fractal_k: usize,
    /// ADR-0012 D12: AND the final mask with
    /// [`Stage1aBatchInputs::applies_to_1day_mask`].
    pub apply_1day_mask: bool,
    /// POSITIVE `stage1a.ANCHOR_DEPENDENT_MASK` (G-1: complemented in-width
    /// here — a negative Python int cannot cross the `u128` FFI edge).
    pub anchor_dependent_mask: u128,
    /// POSITIVE `stage1a.FIB_DEPENDENT_MASK`.
    pub fib_dependent_mask: u128,
    /// POSITIVE `bits.APPLIES_TO_1DAY_MASK` (an AND mask — applied directly).
    pub applies_to_1day_mask: u128,
}

/// Validate every STRUCTURAL invariant of *inp*; return the bar count `N`.
///
/// `Err(String)` on any violation (never a panic — G-2: a marshal-desynced
/// day table must never silently serve shifted anchors):
///
/// * all 14 per-bar columns share one length `N`;
/// * `day_pivot_levels` / `day_fib_levels` / `day_has_fib` sizes agree with
///   `D = day_has_pivot.len()` (checked multiplication — total for any `D`);
/// * `N == 0` requires empty day tables (and yields `Ok(0)`);
/// * `day_index` starts at 0, steps by 0 or 1, and its final value satisfies
///   `last + 1 == D` (every day row consumed — the G-2 terminal check; the
///   step law subsumes "non-decreasing and `< D`");
/// * `prior_n ∈ MIN_PRIOR_N..=MAX_PRIOR_N`; `fractal_k >= 1` (the native
///   [`MarketStructureState::new`] would `assert!` on 0 — rejected here so the
///   panic is unreachable).
pub fn validate_structure(inp: &Stage1aBatchInputs<'_>) -> Result<usize, String> {
    let n = inp.open_paisa.len();
    for (name, len) in [
        ("high_paisa", inp.high_paisa.len()),
        ("low_paisa", inp.low_paisa.len()),
        ("close_paisa", inp.close_paisa.len()),
        ("typical_paisa", inp.typical_paisa.len()),
        ("volume", inp.volume.len()),
        ("tod_seconds", inp.tod_seconds.len()),
        ("pre_open", inp.pre_open.len()),
        ("session_key", inp.session_key.len()),
        ("session_day", inp.session_day.len()),
        ("contract_reset", inp.contract_reset.len()),
        ("day_index", inp.day_index.len()),
        ("ema20_paisa", inp.ema20_paisa.len()),
        ("ema200_paisa", inp.ema200_paisa.len()),
    ] {
        if len != n {
            return Err(format!(
                "stage1a_batch: {name} length {len} != open_paisa length {n}"
            ));
        }
    }
    let n_days = inp.day_has_pivot.len();
    if inp.day_has_fib.len() != n_days {
        return Err(format!(
            "stage1a_batch: day_has_fib length {} != day_has_pivot length {n_days}",
            inp.day_has_fib.len()
        ));
    }
    let want_pivot = PIVOT_ROW_LEN
        .checked_mul(n_days)
        .ok_or_else(|| format!("stage1a_batch: day count {n_days} overflows the pivot table"))?;
    if inp.day_pivot_levels.len() != want_pivot {
        return Err(format!(
            "stage1a_batch: day_pivot_levels length {} != {PIVOT_ROW_LEN} * {n_days} days",
            inp.day_pivot_levels.len()
        ));
    }
    let want_fib = FIB_ROW_LEN
        .checked_mul(n_days)
        .ok_or_else(|| format!("stage1a_batch: day count {n_days} overflows the fib table"))?;
    if inp.day_fib_levels.len() != want_fib {
        return Err(format!(
            "stage1a_batch: day_fib_levels length {} != {FIB_ROW_LEN} * {n_days} days",
            inp.day_fib_levels.len()
        ));
    }
    if !(MIN_PRIOR_N..=MAX_PRIOR_N).contains(&inp.prior_n) {
        return Err(format!(
            "stage1a_batch: prior_n ({}) out of the [{MIN_PRIOR_N}, {MAX_PRIOR_N}] domain",
            inp.prior_n
        ));
    }
    if inp.fractal_k < 1 {
        return Err("stage1a_batch: fractal_k must be >= 1, got 0".to_string());
    }
    if n == 0 {
        if n_days != 0 {
            return Err(format!(
                "stage1a_batch: {n_days} day row(s) supplied for an empty bar stream"
            ));
        }
        return Ok(0);
    }
    // day_index law (G-2): first row 0, per-bar step ∈ {0, +1}, every day row
    // consumed. The step law implies non-decreasing AND (with the terminal
    // check) `< D` everywhere, so a desynced table can never serve a shifted
    // — or worse, out-of-bounds — anchor row.
    if inp.day_index[0] != 0 {
        return Err(format!(
            "stage1a_batch: day_index must start at 0, got {}",
            inp.day_index[0]
        ));
    }
    for i in 1..n {
        let prev = inp.day_index[i - 1];
        let cur = inp.day_index[i];
        if cur != prev && cur != prev.wrapping_add(1) {
            return Err(format!(
                "stage1a_batch: day_index steps {prev} -> {cur} at bar {i} \
                 (must step by 0 or 1)"
            ));
        }
    }
    let last = inp.day_index[n - 1] as usize;
    if last
        .checked_add(1)
        .is_none_or(|consumed| consumed != n_days)
    {
        return Err(format!(
            "stage1a_batch: final day_index {last} + 1 != {n_days} day rows \
             (every day row must be consumed — marshal desync)"
        ));
    }
    Ok(n)
}

/// Assemble one day row's [`PivotLevels`] exactly like
/// `evaluate_with_levels_impl` does (r4/s4 zero-filled — the conditions never
/// read them; pinned by the feature-gated differential test in `python.rs`).
fn pivots_from_row(row: &[i64]) -> PivotLevels {
    PivotLevels {
        p_paisa: row[0],
        r1_paisa: row[1],
        r2_paisa: row[2],
        r3_paisa: row[3],
        s1_paisa: row[4],
        s2_paisa: row[5],
        s3_paisa: row[6],
        pdh_paisa: row[7],
        pdl_paisa: row[8],
        pdc_paisa: row[9],
        r5_paisa: row[10],
        s5_paisa: row[11],
        r4_paisa: 0,
        s4_paisa: 0,
    }
}

/// Assemble one day row's [`FibLevels`] exactly like
/// `evaluate_with_levels_impl` does (pdh/pdl/range/424s/bull tail zero-filled
/// — the conditions never read them; pinned by the differential test).
fn fib_from_row(row: &[i64]) -> FibLevels {
    FibLevels {
        retrace_level_at_pdh_paisa: row[0],
        retrace_23_paisa: row[1],
        retrace_38_paisa: row[2],
        retrace_50_paisa: row[3],
        retrace_62_paisa: row[4],
        retrace_78_paisa: row[5],
        retrace_level_at_pdl_paisa: row[6],
        ext_up_127_paisa: row[7],
        ext_up_162_paisa: row[8],
        ext_up_200_paisa: row[9],
        ext_up_262_paisa: row[10],
        ext_down_127_paisa: row[11],
        ext_down_162_paisa: row[12],
        ext_down_200_paisa: row[13],
        ext_down_262_paisa: row[14],
        pdh_paisa: 0,
        pdl_paisa: 0,
        range_paisa: 0,
        ext_up_424_paisa: 0,
        ext_down_424_paisa: 0,
        retrace_bull_23_paisa: 0,
        retrace_bull_38_paisa: 0,
        retrace_bull_62_paisa: 0,
        retrace_bull_78_paisa: 0,
    }
}

/// One `u128` condition mask per bar, in input order (`Err(String)` on any
/// structural violation — see [`validate_structure`]).
///
/// The loop body mirrors `Stage1AMatrixBuilder.push` step-for-step:
///
/// 1. `contract_reset[i]` ⇒ recreate [`VwapState`] / [`MarketStructureState`],
///    clear the prior-flag deque, void the current day (D11 — the prepass
///    emits the flag from the SAME cursor that fires `_roll_to_new_contract`;
///    the EMA hard reset is a prepass concern — EMA arrives as a column).
/// 2. A `day_index` change ⇒ reset the running day extremes / open-of-day and
///    load the day row's pivot/fib levels (the `DayExtremesState` /
///    `_roll_to_new_day` twin — the prepass emits `day_index` from the same
///    day-branch, so the two boundaries coincide by construction).
/// 3. Absorb high/low into the running day extremes (with the defensive
///    `high >= low` `Err` arm mirroring `DayExtremesState.update`'s
///    `ValueError` — unreachable through a validated `CandleBar`).
/// 4. Feed the SAME core [`VwapState`] the pyclass wraps.
/// 5. Feed the SAME core [`MarketStructureState`].
/// 6. [`evaluate_atoms`] — the ONE frozen evaluator, in-crate.
/// 7. The clears, in `push()`'s exact order: cold pivot anchor ⇒
///    `mask &= !anchor_dependent_mask`; else cold fib ⇒
///    `mask &= !fib_dependent_mask`; then the optional D12
///    `mask &= applies_to_1day_mask`.
/// 8. AFTER evaluation: push `close > open` into the prior deque, trimmed to
///    `prior_n` (a doji records bearish — the evaluator.py:119-123 law).
pub fn stage1a_bar_bits_batch_impl(inp: &Stage1aBatchInputs<'_>) -> Result<Vec<u128>, String> {
    let n = validate_structure(inp)?;
    if n == 0 {
        return Ok(Vec::new());
    }
    let proximity = ProximityParams {
        pivot_proximity_bps: inp.pivot_bps,
        pdh_pdl_proximity_bps: inp.pdh_pdl_bps,
        fib_proximity_bps: inp.fib_bps,
    };
    let mut vwap = VwapState::new();
    let mut structure = MarketStructureState::new(inp.fractal_k);
    let mut prior = PriorBarHistory {
        prior_n: inp.prior_n,
        flags: Vec::with_capacity(inp.prior_n + 1),
    };
    // Day-row cursor state. `current_day = None` forces the day branch on the
    // first bar (and after every contract reset — the `_current_day = None`
    // twin), so `pivots`/`fib`/extremes are always day-seeded before use; the
    // initial literals below are never read (row 0 loads at bar 0).
    let mut current_day: Option<u32> = None;
    let mut day_high: i64 = 0;
    let mut day_low: i64 = 0;
    let mut open_of_day: i64 = 0;
    let mut pivots = pivots_from_row(&inp.day_pivot_levels[..PIVOT_ROW_LEN]);
    let mut fib = fib_from_row(&inp.day_fib_levels[..FIB_ROW_LEN]);
    let mut out = Vec::with_capacity(n);
    for i in 0..n {
        if inp.contract_reset[i] {
            // ADR-0012 D11 TOTAL hard reset — post-roll state == fresh state.
            vwap = VwapState::new();
            structure = MarketStructureState::new(inp.fractal_k);
            prior.flags.clear();
            current_day = None;
        }
        let open = inp.open_paisa[i];
        let high = inp.high_paisa[i];
        let low = inp.low_paisa[i];
        let close = inp.close_paisa[i];
        if high < low {
            // Mirrors `DayExtremesState.update`'s ValueError (data corruption
            // indicator). Unreachable through a validated CandleBar (the
            // constructor enforces low <= high and round_money is monotone) —
            // a defensive Err arm, never a panic.
            return Err(format!(
                "day-extremes bar high < low: high={high} low={low}"
            ));
        }
        let d = inp.day_index[i];
        if current_day != Some(d) {
            current_day = Some(d);
            day_high = high;
            day_low = low;
            open_of_day = open;
            let du = d as usize;
            pivots = pivots_from_row(
                &inp.day_pivot_levels[du * PIVOT_ROW_LEN..(du + 1) * PIVOT_ROW_LEN],
            );
            fib = fib_from_row(&inp.day_fib_levels[du * FIB_ROW_LEN..(du + 1) * FIB_ROW_LEN]);
        } else {
            day_high = day_high.max(high);
            day_low = day_low.min(low);
        }
        let vwap_paisa = vwap.update(
            inp.typical_paisa[i],
            inp.volume[i],
            inp.pre_open[i],
            inp.session_key[i],
            inp.session_day[i],
            inp.tod_seconds[i],
        );
        let market_structure = structure.update(high, low, close);
        let snapshot = IndicatorSnapshot {
            tod_seconds: inp.tod_seconds[i],
            open_paisa: open,
            high_paisa: high,
            low_paisa: low,
            close_paisa: close,
            ema20_paisa: inp.ema20_paisa[i],
            ema200_paisa: inp.ema200_paisa[i],
            vwap_paisa,
            pivots,
            fib,
            day_high_paisa: day_high,
            day_low_paisa: day_low,
            open_of_day_paisa: open_of_day,
            market_structure,
        };
        let mut mask = evaluate_atoms(&snapshot, &proximity, &prior);
        let du = d as usize;
        if !inp.day_has_pivot[du] {
            mask &= !inp.anchor_dependent_mask;
        } else if !inp.day_has_fib[du] {
            mask &= !inp.fib_dependent_mask;
        }
        if inp.apply_1day_mask {
            mask &= inp.applies_to_1day_mask;
        }
        // AFTER evaluation (P-06: the bar is not in its own look-back);
        // close == open (doji) records BEARISH — the one-definition tie-break.
        prior.flags.push(close > open);
        if prior.flags.len() > inp.prior_n {
            prior.flags.remove(0);
        }
        out.push(mask);
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Minimal valid single-day inputs over `n` bars (flat 100.00-rupee dojis,
    /// warm placeholder-free day rows). Columns are returned as owned Vecs the
    /// caller borrows into a [`Stage1aBatchInputs`] via [`view`].
    struct Cols {
        open: Vec<i64>,
        high: Vec<i64>,
        low: Vec<i64>,
        close: Vec<i64>,
        typical: Vec<i64>,
        volume: Vec<i64>,
        tod: Vec<u32>,
        pre_open: Vec<bool>,
        session_key: Vec<u64>,
        session_day: Vec<i64>,
        contract_reset: Vec<bool>,
        day_index: Vec<u32>,
        ema20: Vec<Option<i64>>,
        ema200: Vec<Option<i64>>,
        day_pivots: Vec<i64>,
        day_fibs: Vec<i64>,
        day_has_pivot: Vec<bool>,
        day_has_fib: Vec<bool>,
    }

    fn cols(n: usize) -> Cols {
        // One day row, cold anchors (placeholder zeros — cleared anyway).
        let n_days = usize::from(n > 0);
        Cols {
            open: vec![10_000; n],
            high: vec![10_100; n],
            low: vec![9_900; n],
            close: vec![10_050; n],
            typical: vec![10_017; n],
            volume: vec![100; n],
            tod: (0..n).map(|i| 9 * 3600 + 16 * 60 + i as u32).collect(),
            pre_open: vec![false; n],
            session_key: vec![0; n],
            session_day: vec![738_000; n],
            contract_reset: vec![false; n],
            day_index: vec![0; n],
            ema20: vec![None; n],
            ema200: vec![None; n],
            day_pivots: vec![0; PIVOT_ROW_LEN * n_days],
            day_fibs: vec![0; FIB_ROW_LEN * n_days],
            day_has_pivot: vec![false; n_days],
            day_has_fib: vec![false; n_days],
        }
    }

    fn view(c: &Cols) -> Stage1aBatchInputs<'_> {
        Stage1aBatchInputs {
            open_paisa: &c.open,
            high_paisa: &c.high,
            low_paisa: &c.low,
            close_paisa: &c.close,
            typical_paisa: &c.typical,
            volume: &c.volume,
            tod_seconds: &c.tod,
            pre_open: &c.pre_open,
            session_key: &c.session_key,
            session_day: &c.session_day,
            contract_reset: &c.contract_reset,
            day_index: &c.day_index,
            ema20_paisa: &c.ema20,
            ema200_paisa: &c.ema200,
            day_pivot_levels: &c.day_pivots,
            day_fib_levels: &c.day_fibs,
            day_has_pivot: &c.day_has_pivot,
            day_has_fib: &c.day_has_fib,
            pivot_bps: 10,
            pdh_pdl_bps: 10,
            fib_bps: 10,
            prior_n: 3,
            fractal_k: 2,
            apply_1day_mask: false,
            anchor_dependent_mask: crate::bits::IMPLEMENTED_MASK, // any positive mask
            fib_dependent_mask: 0,
            applies_to_1day_mask: crate::bits::IMPLEMENTED_MASK,
        }
    }

    // ── validation matrix ──────────────────────────────────────────────

    #[test]
    fn empty_input_yields_empty_vec() {
        let c = cols(0);
        assert_eq!(
            stage1a_bar_bits_batch_impl(&view(&c)).unwrap(),
            Vec::<u128>::new()
        );
    }

    #[test]
    fn empty_bars_with_day_rows_rejected() {
        let mut c = cols(0);
        c.day_has_pivot = vec![false];
        c.day_has_fib = vec![false];
        c.day_pivots = vec![0; PIVOT_ROW_LEN];
        c.day_fibs = vec![0; FIB_ROW_LEN];
        let err = stage1a_bar_bits_batch_impl(&view(&c)).unwrap_err();
        assert!(err.contains("empty bar stream"), "got: {err}");
    }

    #[test]
    fn every_per_bar_column_length_is_validated() {
        // Truncate each of the 13 sibling columns in turn; each must be
        // rejected with a length Err (the 14-slice length law).
        for which in 0..13 {
            let mut c = cols(3);
            match which {
                0 => {
                    c.high.pop();
                }
                1 => {
                    c.low.pop();
                }
                2 => {
                    c.close.pop();
                }
                3 => {
                    c.typical.pop();
                }
                4 => {
                    c.volume.pop();
                }
                5 => {
                    c.tod.pop();
                }
                6 => {
                    c.pre_open.pop();
                }
                7 => {
                    c.session_key.pop();
                }
                8 => {
                    c.session_day.pop();
                }
                9 => {
                    c.contract_reset.pop();
                }
                10 => {
                    c.day_index.pop();
                }
                11 => {
                    c.ema20.pop();
                }
                _ => {
                    c.ema200.pop();
                }
            }
            let err = stage1a_bar_bits_batch_impl(&view(&c)).unwrap_err();
            assert!(err.contains("length"), "column {which}: {err}");
        }
    }

    #[test]
    fn day_table_sizes_are_validated() {
        let mut c = cols(2);
        c.day_has_fib.push(true);
        let err = stage1a_bar_bits_batch_impl(&view(&c)).unwrap_err();
        assert!(err.contains("day_has_fib"), "got: {err}");

        let mut c = cols(2);
        c.day_pivots.push(0);
        let err = stage1a_bar_bits_batch_impl(&view(&c)).unwrap_err();
        assert!(err.contains("day_pivot_levels"), "got: {err}");

        let mut c = cols(2);
        c.day_fibs.pop();
        let err = stage1a_bar_bits_batch_impl(&view(&c)).unwrap_err();
        assert!(err.contains("day_fib_levels"), "got: {err}");
    }

    #[test]
    fn day_index_law_is_validated() {
        // First row must be 0.
        let mut c = cols(2);
        c.day_index = vec![1, 1];
        c.day_has_pivot = vec![false, false];
        c.day_has_fib = vec![false, false];
        c.day_pivots = vec![0; PIVOT_ROW_LEN * 2];
        c.day_fibs = vec![0; FIB_ROW_LEN * 2];
        let err = stage1a_bar_bits_batch_impl(&view(&c)).unwrap_err();
        assert!(err.contains("start at 0"), "got: {err}");

        // Steps of 2 (a skipped day row) are a desync.
        let mut c = cols(2);
        c.day_index = vec![0, 2];
        c.day_has_pivot = vec![false; 3];
        c.day_has_fib = vec![false; 3];
        c.day_pivots = vec![0; PIVOT_ROW_LEN * 3];
        c.day_fibs = vec![0; FIB_ROW_LEN * 3];
        let err = stage1a_bar_bits_batch_impl(&view(&c)).unwrap_err();
        assert!(err.contains("must step by 0 or 1"), "got: {err}");

        // A DECREASING step is a desync.
        let mut c = cols(3);
        c.day_index = vec![0, 1, 0];
        c.day_has_pivot = vec![false; 2];
        c.day_has_fib = vec![false; 2];
        c.day_pivots = vec![0; PIVOT_ROW_LEN * 2];
        c.day_fibs = vec![0; FIB_ROW_LEN * 2];
        let err = stage1a_bar_bits_batch_impl(&view(&c)).unwrap_err();
        assert!(err.contains("must step by 0 or 1"), "got: {err}");

        // The G-2 terminal check: an unconsumed trailing day row.
        let mut c = cols(2);
        c.day_has_pivot = vec![false, false];
        c.day_has_fib = vec![false, false];
        c.day_pivots = vec![0; PIVOT_ROW_LEN * 2];
        c.day_fibs = vec![0; FIB_ROW_LEN * 2];
        let err = stage1a_bar_bits_batch_impl(&view(&c)).unwrap_err();
        assert!(err.contains("every day row must be consumed"), "got: {err}");
    }

    #[test]
    fn prior_n_and_fractal_k_domains_are_validated() {
        let c = cols(1);
        let mut v = view(&c);
        v.prior_n = 0;
        assert!(
            stage1a_bar_bits_batch_impl(&v)
                .unwrap_err()
                .contains("prior_n")
        );
        v.prior_n = 5;
        assert!(
            stage1a_bar_bits_batch_impl(&v)
                .unwrap_err()
                .contains("prior_n")
        );
        v.prior_n = 3;
        v.fractal_k = 0;
        assert!(
            stage1a_bar_bits_batch_impl(&v)
                .unwrap_err()
                .contains("fractal_k")
        );
    }

    #[test]
    fn high_below_low_is_a_clean_err() {
        let mut c = cols(1);
        c.high[0] = 9_000;
        let err = stage1a_bar_bits_batch_impl(&view(&c)).unwrap_err();
        assert!(err.contains("high < low"), "got: {err}");
    }

    // ── semantics ──────────────────────────────────────────────────────

    /// A two-day warm-anchor stream: day 0 cold, day 1 warm with real levels.
    fn two_day_cols() -> Cols {
        let mut c = cols(4);
        c.day_index = vec![0, 0, 1, 1];
        c.session_day = vec![738_000, 738_000, 738_001, 738_001];
        c.day_has_pivot = vec![false, true];
        c.day_has_fib = vec![false, true];
        // Day-1 row: pivots/fib anchored at ~100 rupees so proximity can fire.
        let pivot_row = [
            10_000, 10_100, 10_200, 10_300, 9_900, 9_800, 9_700, 10_100, 9_900, 10_000, 10_500,
            9_500,
        ];
        let fib_row = [
            10_100, 10_053, 10_024, 10_000, 9_976, 9_943, 9_900, 10_154, 10_224, 10_300, 10_424,
            9_846, 9_776, 9_700, 9_576,
        ];
        c.day_pivots = [[0i64; PIVOT_ROW_LEN].as_slice(), pivot_row.as_slice()].concat();
        c.day_fibs = [[0i64; FIB_ROW_LEN].as_slice(), fib_row.as_slice()].concat();
        c
    }

    #[test]
    fn cold_pivot_day_clears_anchor_dependent_bits() {
        let c = two_day_cols();
        let mut v = view(&c);
        let above_pdh = 1u128 << crate::bits::BIT_CLOSE_ABOVE_PDH;
        let above_pdl = 1u128 << crate::bits::BIT_CLOSE_ABOVE_PDL;
        v.anchor_dependent_mask = above_pdh | above_pdl;
        v.fib_dependent_mask = 0;
        let masks = stage1a_bar_bits_batch_impl(&v).unwrap();
        // Day 0 (cold): the anchor-dependent bits are cleared.
        assert_eq!(masks[0] & v.anchor_dependent_mask, 0);
        assert_eq!(masks[1] & v.anchor_dependent_mask, 0);
        // Day 1 (warm): close 10_050 < pdh 10_100 (no above-PDH bit) but
        // close 10_050 > pdl 9_900 → above-PDL fires and SURVIVES warm.
        assert_ne!(masks[2] & above_pdl, 0);
    }

    #[test]
    fn fib_cold_day_clears_only_fib_bits() {
        // Make near_fib_0 GENUINELY fire when the fib anchor is warm: put the
        // day-1 fib row's first level exactly at the close.
        let mut warm = two_day_cols();
        warm.day_fibs[FIB_ROW_LEN] = 10_050;
        let mut v = view(&warm);
        let fib_bit = 1u128 << crate::bits::BIT_NEAR_FIB_0;
        let pdl_bit = 1u128 << crate::bits::BIT_CLOSE_ABOVE_PDL;
        v.anchor_dependent_mask = pdl_bit | fib_bit;
        v.fib_dependent_mask = fib_bit;
        let warm_masks = stage1a_bar_bits_batch_impl(&v).unwrap();
        assert_ne!(warm_masks[2] & fib_bit, 0, "warm fib: bit fires");

        // Same levels, fib anchor COLD: ONLY the fib bit is cleared (R-2);
        // the warm pivot bit survives.
        let mut cold = two_day_cols();
        cold.day_fibs[FIB_ROW_LEN] = 10_050;
        cold.day_has_fib = vec![false, false];
        let mut v = view(&cold);
        v.anchor_dependent_mask = pdl_bit | fib_bit;
        v.fib_dependent_mask = fib_bit;
        let cold_masks = stage1a_bar_bits_batch_impl(&v).unwrap();
        assert_eq!(cold_masks[2] & fib_bit, 0, "cold fib: bit cleared");
        assert_ne!(cold_masks[2] & pdl_bit, 0, "warm pivots survive");
    }

    #[test]
    fn apply_1day_mask_ands_the_final_mask() {
        let c = cols(1);
        let mut v = view(&c);
        v.apply_1day_mask = true;
        v.applies_to_1day_mask = 0; // degenerate: clears EVERYTHING
        let masks = stage1a_bar_bits_batch_impl(&v).unwrap();
        assert_eq!(masks, vec![0]);
    }

    #[test]
    fn day_boundary_resets_extremes_on_inequality() {
        // Day 0 carries a huge high; day 1's extremes must NOT inherit it:
        // day 1's first bar has close == its own day high → bit 40 fires.
        let mut c = two_day_cols();
        c.high = vec![20_000, 20_000, 10_050, 10_100];
        c.close = vec![10_050, 10_050, 10_050, 10_050];
        let masks = stage1a_bar_bits_batch_impl(&view(&c)).unwrap();
        assert_ne!(
            masks[2] & (1u128 << crate::bits::BIT_CLOSE_AT_DAY_HIGH),
            0,
            "day-1 first bar close==day high"
        );
        // …and the same bar on day 0 (bar 0's close 10_050 < high 20_000)
        // does NOT read at-day-high.
        assert_eq!(masks[0] & (1u128 << crate::bits::BIT_CLOSE_AT_DAY_HIGH), 0);
    }

    #[test]
    fn doji_prior_flag_records_bearish() {
        // Three flat dojis (close == open) then a fourth bar: with prior_n=3
        // the fourth bar's Group-6 all-BEARISH bit (38) fires. The cold-day
        // clear is disarmed (mask 0) so the Group-6 bit survives to output.
        let mut c = cols(4);
        c.open = vec![10_050; 4];
        let mut v = view(&c);
        v.anchor_dependent_mask = 0;
        let masks = stage1a_bar_bits_batch_impl(&v).unwrap();
        assert_ne!(
            masks[3] & (1u128 << crate::bits::BIT_PRIOR_N_BEARISH),
            0,
            "3 dojis == 3 bearish flags"
        );
        assert_eq!(masks[3] & (1u128 << crate::bits::BIT_PRIOR_N_BULLISH), 0);
    }

    #[test]
    fn contract_reset_recreates_state_bit_for_bit() {
        // Segment B replayed standalone == segment B after a mid-stream reset
        // (the D11 "post-roll state == fresh-builder state" law, natively).
        let mut c = two_day_cols();
        c.contract_reset = vec![false, false, true, false];
        c.session_key = vec![7, 7, 8, 8];
        // Vary the prices so states (VWAP / structure / prior) carry signal.
        c.open = vec![10_000, 10_040, 10_010, 10_020];
        c.close = vec![10_040, 10_010, 10_030, 10_010];
        c.high = vec![10_060, 10_060, 10_060, 10_060];
        c.low = vec![9_990, 9_990, 9_990, 9_990];
        let full = stage1a_bar_bits_batch_impl(&view(&c)).unwrap();

        // Standalone segment B: bars 2..4 with rebased day tables/indices.
        let b = Cols {
            open: c.open[2..].to_vec(),
            high: c.high[2..].to_vec(),
            low: c.low[2..].to_vec(),
            close: c.close[2..].to_vec(),
            typical: c.typical[2..].to_vec(),
            volume: c.volume[2..].to_vec(),
            tod: c.tod[2..].to_vec(),
            pre_open: c.pre_open[2..].to_vec(),
            session_key: c.session_key[2..].to_vec(),
            session_day: c.session_day[2..].to_vec(),
            contract_reset: vec![false, false],
            day_index: vec![0, 0],
            ema20: c.ema20[2..].to_vec(),
            ema200: c.ema200[2..].to_vec(),
            day_pivots: c.day_pivots[PIVOT_ROW_LEN..].to_vec(),
            day_fibs: c.day_fibs[FIB_ROW_LEN..].to_vec(),
            day_has_pivot: vec![c.day_has_pivot[1]],
            day_has_fib: vec![c.day_has_fib[1]],
        };
        let standalone = stage1a_bar_bits_batch_impl(&view(&b)).unwrap();
        assert_eq!(&full[2..], standalone.as_slice());
    }

    #[test]
    fn u128_complement_identity_matches_python_arbitrary_precision_and() {
        // G-1: for ANY mask < 2^128, `mask & !dep` here equals Python's
        // `mask & ~DEP` (arbitrary-precision) — the dep bits are removed,
        // everything else survives.
        for (mask, dep) in [
            (u128::MAX, 0b1010u128),
            ((1 << 60) - 1, (1 << 60) - 1),
            (0x0123_4567_89ab_cdef, 1 << 127),
            (0, u128::MAX),
        ] {
            let cleared = mask & !dep;
            assert_eq!(cleared & dep, 0);
            assert_eq!(cleared | (mask & dep), mask);
        }
    }

    #[test]
    fn prefix_of_masks_is_masks_of_prefix() {
        // Suffix independence natively: truncating the inputs to a prefix
        // yields exactly the prefix of the full output.
        let c = two_day_cols();
        let full = stage1a_bar_bits_batch_impl(&view(&c)).unwrap();
        for k in 1..=2usize {
            // Prefixes ending inside day 0 (bars 1..=2) need only day row 0.
            let p = Cols {
                open: c.open[..k].to_vec(),
                high: c.high[..k].to_vec(),
                low: c.low[..k].to_vec(),
                close: c.close[..k].to_vec(),
                typical: c.typical[..k].to_vec(),
                volume: c.volume[..k].to_vec(),
                tod: c.tod[..k].to_vec(),
                pre_open: c.pre_open[..k].to_vec(),
                session_key: c.session_key[..k].to_vec(),
                session_day: c.session_day[..k].to_vec(),
                contract_reset: c.contract_reset[..k].to_vec(),
                day_index: c.day_index[..k].to_vec(),
                ema20: c.ema20[..k].to_vec(),
                ema200: c.ema200[..k].to_vec(),
                day_pivots: c.day_pivots[..PIVOT_ROW_LEN].to_vec(),
                day_fibs: c.day_fibs[..FIB_ROW_LEN].to_vec(),
                day_has_pivot: c.day_has_pivot[..1].to_vec(),
                day_has_fib: c.day_has_fib[..1].to_vec(),
            };
            let prefix = stage1a_bar_bits_batch_impl(&view(&p)).unwrap();
            assert_eq!(prefix.as_slice(), &full[..k], "prefix k={k}");
        }
    }
}
