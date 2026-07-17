//! Batch Stage-1A bar-bits kernel — one call per `(instrument, tf)` tuple.
//!
//! `DEC-STAGE1A-RUST-BATCH-001`: BruteX's Stage-1A matrix builder
//! (`brutex/engine/stage1a.py::Stage1AMatrixBuilder`) walks a bar feed in
//! Python calling `evaluate_with_levels` +
//! `MarketStructureState.update` once PER BAR over the FFI. This module is the
//! batch twin: the Python prepass (`Stage1AMatrixBuilder.collect_batch_inputs`)
//! resolves every DECISION column with the builder's own roll/day/anchor
//! machinery — the Muhurat guard, the ADR-0012 D11 roll hard-reset, the
//! daily-history bound and the anchor resolution are NEVER re-implemented here
//! — and this core re-expresses only the integer state bookkeeping (running
//! day extremes, the prior-bar flag deque, per-contract state recreation)
//! around the SAME frozen cores: [`MarketStructureState`] and
//! [`evaluate_atoms`]. One `Vec<u128>` out, one mask per bar, input order.
//! (The former VWAP columns — typical/volume/pre_open/session_key/session_day —
//! were DELETED with the bits 52-53 tombstone: the VWAP/volume wipe.)
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
//! VALUE envelope (price/level magnitudes, bps products) is
//! enforced by the feature-gated PyO3 wrapper in `python.rs`, which reuses the
//! EXISTING boundary guards (`check_evaluate_inputs` / `bar_in_range`)
//! in place, verbatim — this core deliberately duplicates
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
//! below), and the masks the evaluator emits occupy bits 0..=63 only.

use crate::evaluator::{IndicatorSnapshot, PriorBarHistory, ProximityParams, evaluate_atoms};
use crate::indicators::fib::FibLevels;
use crate::indicators::gap_mid::compute_gap_mid;
use crate::indicators::market_structure::MarketStructureState;
use crate::indicators::pivots::PivotLevels;

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

/// Upper bound for `fractal_k` (the BOS/CHoCH swing look-back; production
/// passes the literal 2 — `Stage1AMatrixBuilder` hardcodes
/// `MarketStructureState()`, there is no config knob).
/// [`MarketStructureState::new`] computes a `2*k + 1` deque capacity under
/// `overflow-checks = true` + `panic = "abort"`, so an unbounded hostile `k`
/// (e.g. `2^64 - 1`, or `2^62` via capacity overflow) would SIGABRT the
/// whole interpreter through the FFI (refuter RF-B F2); the bound exists to
/// keep that panic unreachable through this pyfunction, and 64 is
/// comfortably above every conceivable production value. HONEST (RF-I
/// round-3 F3): a `k > 64` IS satisfiable on real streams — `k = 65` needs
/// only 131 bars (one NSE session is 375) and the structure state persists
/// across days within a contract — the bound is a hostile-input safety cap,
/// NOT a market-physics limit. NOTE: the pre-existing frozen
/// `MarketStructureState` PYCLASS has the same abort behaviour for a direct
/// hostile construction — deliberately NOT touched (frozen TickVault-shared
/// surface, out of this slice's scope).
pub const MAX_FRACTAL_K: usize = 64;

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
    /// IST wall-clock seconds-of-day, prepass-resolved via the shared
    /// `trading_core.seconds_of_day` seam (DEC-TOD-SECONDS-RUST-SEAM-001).
    pub tod_seconds: &'a [u32],
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
    /// Per-day prior-session-edge X1 HIGH (int64 paisa) — the HIGH of the
    /// prior session's LAST completed TF bar, prepass-RESOLVED per day exactly
    /// like `day_pivot_levels` (the Muhurat skip + first-day/post-roll cold
    /// start are Python-side decisions; DEC-GAP-MIDPOINT-CONDITIONS-001).
    /// Read only where [`Stage1aBatchInputs::day_has_x1`] is true.
    pub day_x1_hi_paisa: &'a [i64],
    /// Per-day prior-session-edge X1 LOW — see
    /// [`Stage1aBatchInputs::day_x1_hi_paisa`].
    pub day_x1_lo_paisa: &'a [i64],
    /// Per-day X1 warm flag (`False` ⇒ no gap-midpoint level exists that day —
    /// the Group-15 bits 66-68 stay 0; the `day_has_pivot` twin for the
    /// session-edge anchor).
    pub day_has_x1: &'a [bool],
    /// Group-15 gap-midpoint proximity tolerance (bps).
    pub gap_mid_bps: i64,
}

/// Validate every STRUCTURAL invariant of *inp*; return the bar count `N`.
///
/// `Err(String)` on any violation (never a panic — G-2: a marshal-desynced
/// day table must never silently serve shifted anchors):
///
/// * all 9 per-bar columns share one length `N`;
/// * `day_pivot_levels` / `day_fib_levels` / `day_has_fib` sizes agree with
///   `D = day_has_pivot.len()` (checked multiplication — total for any `D`);
/// * `N == 0` requires empty day tables (and yields `Ok(0)`);
/// * `day_index` starts at 0, steps by 0 or 1, and its final value satisfies
///   `last + 1 == D` (every day row consumed — the G-2 terminal check; the
///   step law subsumes "non-decreasing and `< D`");
/// * `prior_n ∈ MIN_PRIOR_N..=MAX_PRIOR_N`; `fractal_k ∈ 1..=MAX_FRACTAL_K`
///   (the native [`MarketStructureState::new`] would `assert!` on 0 and
///   overflow-abort on a huge `k` — both rejected here so the panic is
///   unreachable; refuter RF-B F2).
pub fn validate_structure(inp: &Stage1aBatchInputs<'_>) -> Result<usize, String> {
    let n = inp.open_paisa.len();
    for (name, len) in [
        ("high_paisa", inp.high_paisa.len()),
        ("low_paisa", inp.low_paisa.len()),
        ("close_paisa", inp.close_paisa.len()),
        ("tod_seconds", inp.tod_seconds.len()),
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
    for (name, len) in [
        ("day_x1_hi_paisa", inp.day_x1_hi_paisa.len()),
        ("day_x1_lo_paisa", inp.day_x1_lo_paisa.len()),
        ("day_has_x1", inp.day_has_x1.len()),
    ] {
        if len != n_days {
            return Err(format!(
                "stage1a_batch: {name} length {len} != day_has_pivot length {n_days}"
            ));
        }
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
    if !(1..=MAX_FRACTAL_K).contains(&inp.fractal_k) {
        return Err(format!(
            "stage1a_batch: fractal_k ({}) out of the [1, {MAX_FRACTAL_K}] domain",
            inp.fractal_k
        ));
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
/// 1. `contract_reset[i]` ⇒ recreate [`MarketStructureState`],
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
/// 4. Feed the SAME core [`MarketStructureState`].
/// 5. [`evaluate_atoms`] — the ONE frozen evaluator, in-crate.
/// 6. The clears, in `push()`'s exact order: cold pivot anchor ⇒
///    `mask &= !anchor_dependent_mask`; else cold fib ⇒
///    `mask &= !fib_dependent_mask`; then the optional D12
///    `mask &= applies_to_1day_mask`.
/// 7. AFTER evaluation: push `close > open` into the prior deque, trimmed to
///    `prior_n` (a doji records bearish — the evaluator.py:119-123 law).
/// 9. Gap-midpoint (Group 15, DEC-GAP-MIDPOINT-CONDITIONS-001): on the day
///    branch the day's level is minted from the prepass-resolved X1 day row +
///    THIS first bar's own high/low (X2) + its open and the day row's `pdc`
///    (direction = the exact sign of `open − pdc`; zero gap ⇒ no level) via
///    the shared [`compute_gap_mid`]; the first bar of the day evaluates with
///    NO level (the arm-from-bar-2 law — on 1day EVERY bar is its session's
///    first TF bar, so the bits stay 0 there by construction), later bars of
///    the day carry it. A mid-day `contract_reset` (structurally legal,
///    never emitted by the sanctioned prepass — the RF-I F2(c) defensive
///    class) SUPPRESSES the level for the remainder of that day, mirroring
///    the Python D11 hard reset's cleared `prev_session_edge`.
pub fn stage1a_bar_bits_batch_impl(inp: &Stage1aBatchInputs<'_>) -> Result<Vec<u128>, String> {
    let n = validate_structure(inp)?;
    if n == 0 {
        return Ok(Vec::new());
    }
    let proximity = ProximityParams {
        pivot_proximity_bps: inp.pivot_bps,
        pdh_pdl_proximity_bps: inp.pdh_pdl_bps,
        fib_proximity_bps: inp.fib_bps,
        gap_mid_proximity_bps: inp.gap_mid_bps,
    };
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
    // Gap-midpoint state (Group 15). `prev_day_loaded` is deliberately NOT
    // cleared by a contract reset — it distinguishes a GENUINE new day row
    // (clears the suppression) from a mid-day reset re-seeding the SAME day
    // (keeps it), so the defensive same-day-reset shape mirrors Python's
    // cleared `prev_session_edge` (no level for the rest of that day).
    let mut prev_day_loaded: Option<u32> = None;
    let mut gap_suppressed = false;
    let mut day_gap_mid: Option<i64> = None;
    let mut first_bar_of_day = false;
    let mut day_high: i64 = 0;
    let mut day_low: i64 = 0;
    let mut open_of_day: i64 = 0;
    let mut pivots = pivots_from_row(&inp.day_pivot_levels[..PIVOT_ROW_LEN]);
    let mut fib = fib_from_row(&inp.day_fib_levels[..FIB_ROW_LEN]);
    let mut out = Vec::with_capacity(n);
    for i in 0..n {
        if inp.contract_reset[i] {
            // ADR-0012 D11 TOTAL hard reset — post-roll state == fresh state.
            structure = MarketStructureState::new(inp.fractal_k);
            prior.flags.clear();
            current_day = None;
            // The Python twin's `_roll_to_new_contract` clears the prior
            // session's edge, so no gap-mid level can exist until the next
            // GENUINE day row (whose X1 the prepass resolves post-roll).
            gap_suppressed = true;
            day_gap_mid = None;
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
            // Gap-midpoint mint (step 9). A GENUINE new day row clears the
            // reset suppression (the day table is the authority there — the
            // prepass emits `day_has_x1 = false` for the post-roll day); a
            // same-day reset re-seed keeps it. X2 = THIS first bar's own
            // high/low; direction = sign(open − pdc) off the just-loaded day
            // row. The `day_has_pivot` conjunct mirrors the Python law that
            // the level needs a real `pdc` (a cold-pivot day has no prev-day
            // anchor, so no direction exists — never a placeholder-pdc level).
            if prev_day_loaded != Some(d) {
                prev_day_loaded = Some(d);
                gap_suppressed = false;
            }
            first_bar_of_day = true;
            day_gap_mid = if inp.day_has_x1[du] && inp.day_has_pivot[du] && !gap_suppressed {
                let x1_hi = inp.day_x1_hi_paisa[du];
                let x1_lo = inp.day_x1_lo_paisa[du];
                if x1_hi < x1_lo {
                    // Defensive Err arm (never a panic — the totality law):
                    // `compute_gap_mid` would assert; unreachable through the
                    // sanctioned prepass, which emits real candle extremes.
                    return Err(format!(
                        "stage1a_batch: day {du} X1 high < low: high={x1_hi} low={x1_lo}"
                    ));
                }
                compute_gap_mid(x1_hi, x1_lo, high, low, open, pivots.pdc_paisa)
                    .map(|(level, _direction)| level)
            } else {
                None
            };
        } else {
            day_high = day_high.max(high);
            day_low = day_low.min(low);
        }
        let market_structure = structure.update(high, low, close);
        let snapshot = IndicatorSnapshot {
            tod_seconds: inp.tod_seconds[i],
            open_paisa: open,
            high_paisa: high,
            low_paisa: low,
            close_paisa: close,
            ema20_paisa: inp.ema20_paisa[i],
            ema200_paisa: inp.ema200_paisa[i],
            pivots,
            fib,
            day_high_paisa: day_high,
            day_low_paisa: day_low,
            open_of_day_paisa: open_of_day,
            market_structure,
            // The arm-from-bar-2 law: the session's first TF bar (which
            // MINTS the level from its own extremes) evaluates with NO level.
            gap_mid_paisa: if first_bar_of_day { None } else { day_gap_mid },
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
        // The gap-mid level arms from the SECOND bar of the day onward.
        first_bar_of_day = false;
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
        tod: Vec<u32>,
        contract_reset: Vec<bool>,
        day_index: Vec<u32>,
        ema20: Vec<Option<i64>>,
        ema200: Vec<Option<i64>>,
        day_pivots: Vec<i64>,
        day_fibs: Vec<i64>,
        day_has_pivot: Vec<bool>,
        day_has_fib: Vec<bool>,
        day_x1_hi: Vec<i64>,
        day_x1_lo: Vec<i64>,
        day_has_x1: Vec<bool>,
    }

    fn cols(n: usize) -> Cols {
        // One day row, cold anchors (placeholder zeros — cleared anyway).
        let n_days = usize::from(n > 0);
        Cols {
            open: vec![10_000; n],
            high: vec![10_100; n],
            low: vec![9_900; n],
            close: vec![10_050; n],
            tod: (0..n).map(|i| 9 * 3600 + 16 * 60 + i as u32).collect(),
            contract_reset: vec![false; n],
            day_index: vec![0; n],
            ema20: vec![None; n],
            ema200: vec![None; n],
            day_pivots: vec![0; PIVOT_ROW_LEN * n_days],
            day_fibs: vec![0; FIB_ROW_LEN * n_days],
            day_has_pivot: vec![false; n_days],
            day_has_fib: vec![false; n_days],
            day_x1_hi: vec![0; n_days],
            day_x1_lo: vec![0; n_days],
            day_has_x1: vec![false; n_days],
        }
    }

    fn view(c: &Cols) -> Stage1aBatchInputs<'_> {
        Stage1aBatchInputs {
            open_paisa: &c.open,
            high_paisa: &c.high,
            low_paisa: &c.low,
            close_paisa: &c.close,
            tod_seconds: &c.tod,
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
            day_x1_hi_paisa: &c.day_x1_hi,
            day_x1_lo_paisa: &c.day_x1_lo,
            day_has_x1: &c.day_has_x1,
            gap_mid_bps: 10,
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

    /// Resize every per-day table (incl. the Group-15 X1 tables) to `d` rows.
    fn resize_day_tables(c: &mut Cols, d: usize) {
        c.day_has_pivot = vec![false; d];
        c.day_has_fib = vec![false; d];
        c.day_pivots = vec![0; PIVOT_ROW_LEN * d];
        c.day_fibs = vec![0; FIB_ROW_LEN * d];
        c.day_x1_hi = vec![0; d];
        c.day_x1_lo = vec![0; d];
        c.day_has_x1 = vec![false; d];
    }

    #[test]
    fn empty_bars_with_day_rows_rejected() {
        let mut c = cols(0);
        resize_day_tables(&mut c, 1);
        let err = stage1a_bar_bits_batch_impl(&view(&c)).unwrap_err();
        assert!(err.contains("empty bar stream"), "got: {err}");
    }

    #[test]
    fn every_per_bar_column_length_is_validated() {
        // Truncate each of the 8 sibling columns in turn; each must be
        // rejected with a length Err (the 9-slice length law).
        for which in 0..8 {
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
                    c.tod.pop();
                }
                4 => {
                    c.contract_reset.pop();
                }
                5 => {
                    c.day_index.pop();
                }
                6 => {
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
        resize_day_tables(&mut c, 2);
        let err = stage1a_bar_bits_batch_impl(&view(&c)).unwrap_err();
        assert!(err.contains("start at 0"), "got: {err}");

        // Steps of 2 (a skipped day row) are a desync.
        let mut c = cols(2);
        c.day_index = vec![0, 2];
        resize_day_tables(&mut c, 3);
        let err = stage1a_bar_bits_batch_impl(&view(&c)).unwrap_err();
        assert!(err.contains("must step by 0 or 1"), "got: {err}");

        // A DECREASING step is a desync.
        let mut c = cols(3);
        c.day_index = vec![0, 1, 0];
        resize_day_tables(&mut c, 2);
        let err = stage1a_bar_bits_batch_impl(&view(&c)).unwrap_err();
        assert!(err.contains("must step by 0 or 1"), "got: {err}");

        // The G-2 terminal check: an unconsumed trailing day row.
        let mut c = cols(2);
        resize_day_tables(&mut c, 2);
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
        // RF-B F2: the UPPER bound — an unbounded hostile k would
        // overflow-abort inside MarketStructureState::new (2*k + 1 capacity
        // under panic="abort"); the domain check must fire FIRST.
        for huge in [MAX_FRACTAL_K + 1, 1 << 62, usize::MAX] {
            v.fractal_k = huge;
            assert!(
                stage1a_bar_bits_batch_impl(&v)
                    .unwrap_err()
                    .contains("fractal_k")
            );
        }
        // The bound itself is legal (boundary law).
        v.fractal_k = MAX_FRACTAL_K;
        assert!(stage1a_bar_bits_batch_impl(&v).is_ok());
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
        c.day_x1_hi = vec![0, 0];
        c.day_x1_lo = vec![0, 0];
        c.day_has_x1 = vec![false, false];
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
    fn cpr_bits_ride_the_anchor_dependent_clear() {
        // The CPR comparison bits (60-62) fire against a COLD day's zero
        // placeholder row (close 10_050 > tc 0 — the close_above_pdh class),
        // so they MUST be in the anchor-dependent clear; on the WARM day they
        // evaluate off the real row. Day-1 row: pdh=10_100, pdl=9_900,
        // p=10_000 ⇒ bc = round((10_100+9_900)/2) = 10_000, tc = 2·10_000 −
        // 10_000 = 10_000 (a width-0 CPR: narrow fires too); close 10_050 >
        // tc ⇒ bit 60.
        let c = two_day_cols();
        let mut v = view(&c);
        const CPR_FAMILY: u128 = 0xF << 60;
        v.anchor_dependent_mask = REAL_ANCHOR_DEP;
        v.fib_dependent_mask = REAL_FIB_DEP;
        let masks = stage1a_bar_bits_batch_impl(&v).unwrap();
        // Day 0 (cold): every CPR bit cleared — never a fabricated verdict
        // off the placeholder zeros.
        assert_eq!(masks[0] & CPR_FAMILY, 0);
        assert_eq!(masks[1] & CPR_FAMILY, 0);
        // Day 1 (warm): above-TC + narrow fire off the real anchor row.
        assert_ne!(masks[2] & (1u128 << crate::bits::BIT_ABOVE_CPR_TC), 0);
        assert_ne!(masks[2] & (1u128 << crate::bits::BIT_NARROW_CPR_DAY), 0);
        assert_eq!(masks[2] & (1u128 << crate::bits::BIT_INSIDE_CPR), 0);
        assert_eq!(masks[2] & (1u128 << crate::bits::BIT_BELOW_CPR_BC), 0);
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

    /// The three POSITIVE dependency masks exactly as the Python dispatch
    /// sends them (`stage1a.ANCHOR_DEPENDENT_MASK` / `FIB_DEPENDENT_MASK` /
    /// `bits.APPLIES_TO_1DAY_MASK`). Updated for the 66-bit reshape: the
    /// anchor mask GAINS the CPR bits 60-63 (they compare against the SAME
    /// prev-day anchor and would otherwise fire against a cold-day zero row —
    /// the stage1a.py `_pivot_dependent_bits` update ships with the Python
    /// half of this change) and the applies-1day mask gains bits 60-65
    /// (all six new conditions are 1day-applicable in the manifest).
    const REAL_ANCHOR_DEP: u128 = 0xf00f_0000_3fff_ffc0;
    const REAL_FIB_DEP: u128 = 0x3ff80000;
    const REAL_APPLIES_1DAY: u128 = 0x3ffcf0fffffffffff;

    /// The pin-1 columns (directions B,B,B,R,R,R,B,B on 2024-01-02) — shared
    /// by the rolling-window pin and the RF-I F1 `apply_1day_mask` pin.
    fn pin1_cols() -> Cols {
        let mut c = cols(8);
        c.open = vec![
            10_000, 10_500, 11_000, 11_500, 11_000, 10_500, 10_000, 10_500,
        ];
        c.high = vec![
            10_600, 11_100, 11_600, 11_600, 11_100, 10_600, 10_600, 11_100,
        ];
        c.low = vec![9_900, 10_400, 10_900, 10_900, 10_400, 9_900, 9_900, 10_400];
        c.close = vec![
            10_500, 11_000, 11_500, 11_000, 10_500, 10_000, 10_500, 11_000,
        ];
        c.tod = vec![
            33_300, 33_360, 33_420, 33_480, 33_540, 33_600, 33_660, 33_720,
        ];
        c
    }

    /// The Python-oracle masks for [`pin1_cols`] under the REAL dependency
    /// masks (`Stage1AMatrixBuilder(prior_n=3).build`, derived once for the
    /// 60-bit layout; mechanically re-derived for the 66-bit reshape as
    /// `old & !(0b11 << 52)` — sound because the deleted VWAP state only ever
    /// fed bits 52/53, and the new CPR bits 60-63 are cleared on this COLD
    /// (day_has_pivot = false) stream by the updated REAL_ANCHOR_DEP;
    /// CONFIRMED by Phase B against the regenerated Python oracle —
    /// `Stage1AMatrixBuilder(prior_n=3).build` reproduces these literals
    /// bit-for-bit).
    const PIN1_MASKS: [u128; 8] = [
        0x40240000000,
        0x140240000000,
        0x140240000000,
        0x102280000000,
        0x100280000000,
        0x180280000000,
        0x104240000000,
        0x100240000000,
    ];

    /// Refuter RF-B F1(c): the mixed-direction ROLLING-WINDOW value pin.
    ///
    /// Directions B,B,B,R,R,R,B,B with `prior_n = 3` — expected masks derived
    /// ONCE from the Python oracle (`Stage1AMatrixBuilder(prior_n=3).build`)
    /// over the identical `collect_batch_inputs` columns. Kills BOTH
    /// demonstrated survivors: `flags.remove(0)` → `pop()` freezes the FIRST
    /// window ([B,B,B] forever — bar 6 would read all-BULLISH instead of the
    /// oracle's all-BEARISH) and `push(close > open)` → `push(close < open)`
    /// inverts every non-doji flag.
    #[test]
    fn mixed_direction_rolling_window_matches_the_python_oracle() {
        let c = pin1_cols();
        let mut v = view(&c); // prior_n = 3, fractal_k = 2, bps 10/10/10
        v.anchor_dependent_mask = REAL_ANCHOR_DEP;
        v.fib_dependent_mask = REAL_FIB_DEP;
        v.applies_to_1day_mask = REAL_APPLIES_1DAY;
        let masks = stage1a_bar_bits_batch_impl(&v).unwrap();
        assert_eq!(masks, PIN1_MASKS.to_vec());
        // Anti-vacuous: the Group-6 window genuinely ROLLS across the stream
        // (all-bullish at bar 3, all-bearish at bar 6 — a frozen-first-window
        // mutant cannot reproduce both).
        assert_ne!(masks[3] & (1u128 << crate::bits::BIT_PRIOR_N_BULLISH), 0);
        assert_ne!(masks[6] & (1u128 << crate::bits::BIT_PRIOR_N_BEARISH), 0);
    }

    /// Refuter RF-I round-3 F1: the D12 `apply_1day_mask` AND is value-pinned
    /// with a NONZERO incoming mask — the gate's one missed survivor
    /// (`mask &= applies` → `mask |=`) FABRICATES every applies-mask bit ON,
    /// while the oracle (`Stage1AMatrixBuilder(prior_n=3,
    /// apply_1day_mask=True).build`, re-derived + verified) equals
    /// `pin ∧ APPLIES` exactly. The older `apply_1day_mask_ands_the_final_mask`
    /// unit was vacuous against `|=` (applies-mask 0 into an already-0 mask).
    #[test]
    fn apply_1day_mask_matches_the_python_oracle_on_the_pin_stream() {
        let c = pin1_cols();
        let mut v = view(&c);
        v.anchor_dependent_mask = REAL_ANCHOR_DEP;
        v.fib_dependent_mask = REAL_FIB_DEP;
        v.applies_to_1day_mask = REAL_APPLIES_1DAY;
        v.apply_1day_mask = true;
        let masks = stage1a_bar_bits_batch_impl(&v).unwrap();
        let expected: Vec<u128> = PIN1_MASKS.iter().map(|m| m & REAL_APPLIES_1DAY).collect();
        assert_eq!(
            expected,
            vec![
                0x40240000000,
                0x40240000000,
                0x40240000000,
                0x2280000000,
                0x280000000,
                0x80280000000,
                0x4240000000,
                0x240000000,
            ],
            "the ∧-law self-check drifted from the oracle-derived literals"
        );
        assert_eq!(masks, expected);
        // Anti-vacuous: the AND genuinely REMOVES bits on 7 of the 8 bars (an
        // `|=` mutant, which can only ADD, must diverge on those). Bar 0's tod
        // (09:15) precedes every ToD bucket, and its only formerly-cleared bit
        // was the TOMBSTONED VWAP bit 52 — post-wipe it legitimately loses
        // nothing to the 1day AND.
        let removed = PIN1_MASKS
            .iter()
            .zip(&masks)
            .filter(|(m, e)| m != e)
            .count();
        assert!(
            removed >= 7,
            "the 1day clear removed bits on only {removed}/8 bars — vacuous pin"
        );
    }

    /// Refuter RF-B F1(c): the all-BULL prior-flag pin (`prior_n = 2`) —
    /// oracle-derived masks; a direction-inverted `push(close < open)` mutant
    /// flips bit 37 → 38 from bar 2 on.
    #[test]
    fn bull_stream_prior_flags_match_the_python_oracle() {
        let mut c = cols(6);
        c.open = vec![10_000, 10_200, 10_400, 10_600, 10_800, 11_000];
        c.high = vec![10_300, 10_500, 10_700, 10_900, 11_100, 11_300];
        c.low = vec![9_900, 10_100, 10_300, 10_500, 10_700, 10_900];
        c.close = vec![10_200, 10_400, 10_600, 10_800, 11_000, 11_200];
        c.tod = vec![33_300, 33_360, 33_420, 33_480, 33_540, 33_600];
        let mut v = view(&c);
        v.prior_n = 2;
        v.anchor_dependent_mask = REAL_ANCHOR_DEP;
        v.fib_dependent_mask = REAL_FIB_DEP;
        v.applies_to_1day_mask = REAL_APPLIES_1DAY;
        let masks = stage1a_bar_bits_batch_impl(&v).unwrap();
        // Oracle-derived for the 60-bit layout, mechanically re-derived for
        // the 66-bit reshape as `old & !(0b11 << 52)` (see PIN1_MASKS;
        // CONFIRMED by Phase B against the regenerated Python oracle).
        assert_eq!(
            masks,
            vec![
                0x40040000000,
                0x140040000000,
                0x142040000000,
                0x142040000000,
                0x142040000000,
                0x142040000000,
            ]
        );
        assert_ne!(masks[2] & (1u128 << crate::bits::BIT_PRIOR_N_BULLISH), 0);
        assert_eq!(masks[2] & (1u128 << crate::bits::BIT_PRIOR_N_BEARISH), 0);
    }

    /// Refuter RF-I round-3 F2(a): fib ROW-MAPPING teeth — for every fib row
    /// slot, a warm day whose slot value is the ONLY level near the close
    /// must fire EXACTLY that slot's near-fib bit. Kills any
    /// `fib_from_row` `row[i] → row[j]` remap (the demonstrated M5 survivor:
    /// `retrace_38: row[2] → row[4]`). Slots 7..=10 (ext up) and 11..=14
    /// (ext down) share bits 26..=29 — the evaluator ORs the up/down pair,
    /// so a same-rung up↔down swap is definitionally equivalent and cannot
    /// (and need not) be distinguished.
    #[test]
    fn each_fib_row_slot_feeds_exactly_its_own_near_bit() {
        const EXPECTED_BIT: [u32; FIB_ROW_LEN] =
            [19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 26, 27, 28, 29];
        const FIB_BIT_FAMILY: u128 = 0x3ff8_0000; // bits 19..=29
        for (slot, expected_bit) in EXPECTED_BIT.iter().enumerate() {
            let mut c = cols(1);
            c.day_has_pivot = vec![true];
            c.day_has_fib = vec![true];
            // Every level FAR from close (10_050) and mutually distinct, so a
            // remapped slot reads a far value and the near bit goes dark.
            c.day_pivots = (0..PIVOT_ROW_LEN as i64)
                .map(|i| 20_000 + 1_000 * i)
                .collect();
            c.day_fibs = (0..FIB_ROW_LEN as i64)
                .map(|i| 40_000 + 1_000 * i)
                .collect();
            c.day_fibs[slot] = 10_050; // == close → near under fib_bps = 10
            let masks = stage1a_bar_bits_batch_impl(&view(&c)).unwrap();
            assert_eq!(
                masks[0] & FIB_BIT_FAMILY,
                1u128 << expected_bit,
                "fib slot {slot} fed the wrong near bit"
            );
        }
    }

    /// Refuter RF-I round-3 F2(a), pivot half: every pivot row slot pinned to
    /// its own bit. Slots 0..=6 → near-pivot bits 6..=12; slot 7 (pdh) → 17;
    /// slot 8 (pdl) → 18; slots 10/11 (r5/s5) → 54/55; slot 9 (pdc) has no
    /// near bit — it is pinned below via the 0.5%-threshold GAP verdict.
    #[test]
    fn each_pivot_row_slot_feeds_exactly_its_own_bit() {
        const NEAR_SLOTS: [(usize, u32); 11] = [
            (0, 6),
            (1, 7),
            (2, 8),
            (3, 9),
            (4, 10),
            (5, 11),
            (6, 12),
            (7, 17),
            (8, 18),
            (10, 54),
            (11, 55),
        ];
        const PIVOT_NEAR_FAMILY: u128 = (0x7f << 6) | (0x3 << 17) | (0x3 << 54);
        for (slot, bit_pos) in NEAR_SLOTS {
            let mut c = cols(1);
            c.day_has_pivot = vec![true];
            c.day_pivots = (0..PIVOT_ROW_LEN as i64)
                .map(|i| 20_000 + 1_000 * i)
                .collect();
            c.day_pivots[slot] = 10_050; // == close → near under pivot/pp bps = 10
            let masks = stage1a_bar_bits_batch_impl(&view(&c)).unwrap();
            assert_eq!(
                masks[0] & PIVOT_NEAR_FAMILY,
                1u128 << bit_pos,
                "pivot slot {slot} fed the wrong bit"
            );
        }
        // pdc (slot 9): far BELOW the 10_000 open → GAP-UP fires; every other
        // slot value sits far ABOVE the open, so ANY remap of slot 9 flips
        // the verdict to GAP-DOWN.
        let mut c = cols(1);
        c.day_has_pivot = vec![true];
        c.day_pivots = (0..PIVOT_ROW_LEN as i64)
            .map(|i| 20_000 + 1_000 * i)
            .collect();
        c.day_pivots[9] = 5_000;
        let masks = stage1a_bar_bits_batch_impl(&view(&c)).unwrap();
        assert_ne!(masks[0] & (1u128 << crate::bits::BIT_GAP_UP_DAY), 0);
        assert_eq!(masks[0] & (1u128 << crate::bits::BIT_GAP_DOWN_DAY), 0);
    }

    /// Refuter RF-I round-3 F2(c): the reset arm's `current_day = None`
    /// day-cursor drop is DEFENSIVE-semantics teeth. A reset WITHOUT a
    /// day_index bump is structurally LEGAL (`validate_structure` allows a
    /// step of 0 and does not couple `contract_reset` to the day cursor —
    /// stated: the wrapper does NOT reject this shape), even though the
    /// sanctioned prepass always pairs a roll with a new day row. Pinned:
    /// the post-reset bar RE-SEEDS the day extremes (close == the reseeded
    /// day high → bit 40); the demonstrated M7 mutant (delete
    /// `current_day = None`) carries the stale 20_000 day high across the
    /// reset and the bit goes dark.
    #[test]
    fn reset_arm_reseeds_the_day_extremes_via_the_day_cursor() {
        let mut c = cols(2);
        c.contract_reset = vec![false, true];
        c.high = vec![20_000, 10_050];
        c.low = vec![9_900, 9_900];
        c.open = vec![10_000, 10_000];
        c.close = vec![10_050, 10_050];
        let mut v = view(&c);
        v.anchor_dependent_mask = 0; // cold-day clear disarmed — bit 40 observable
        let masks = stage1a_bar_bits_batch_impl(&v).unwrap();
        assert_ne!(
            masks[1] & (1u128 << crate::bits::BIT_CLOSE_AT_DAY_HIGH),
            0,
            "the reset did not re-seed the day extremes"
        );
    }

    #[test]
    fn contract_reset_recreates_state_bit_for_bit() {
        // Segment B replayed standalone == segment B after a mid-stream reset
        // (the D11 "post-roll state == fresh-builder state" law, natively).
        let mut c = two_day_cols();
        c.contract_reset = vec![false, false, true, false];
        // Vary the prices so states (structure / prior) carry signal.
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
            tod: c.tod[2..].to_vec(),
            contract_reset: vec![false, false],
            day_index: vec![0, 0],
            ema20: c.ema20[2..].to_vec(),
            ema200: c.ema200[2..].to_vec(),
            day_pivots: c.day_pivots[PIVOT_ROW_LEN..].to_vec(),
            day_fibs: c.day_fibs[FIB_ROW_LEN..].to_vec(),
            day_has_pivot: vec![c.day_has_pivot[1]],
            day_has_fib: vec![c.day_has_fib[1]],
            day_x1_hi: vec![c.day_x1_hi[1]],
            day_x1_lo: vec![c.day_x1_lo[1]],
            day_has_x1: vec![c.day_has_x1[1]],
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
                tod: c.tod[..k].to_vec(),
                contract_reset: c.contract_reset[..k].to_vec(),
                day_index: c.day_index[..k].to_vec(),
                ema20: c.ema20[..k].to_vec(),
                ema200: c.ema200[..k].to_vec(),
                day_pivots: c.day_pivots[..PIVOT_ROW_LEN].to_vec(),
                day_fibs: c.day_fibs[..FIB_ROW_LEN].to_vec(),
                day_has_pivot: c.day_has_pivot[..1].to_vec(),
                day_has_fib: c.day_has_fib[..1].to_vec(),
                day_x1_hi: c.day_x1_hi[..1].to_vec(),
                day_x1_lo: c.day_x1_lo[..1].to_vec(),
                day_has_x1: c.day_has_x1[..1].to_vec(),
            };
            let prefix = stage1a_bar_bits_batch_impl(&view(&p)).unwrap();
            assert_eq!(prefix.as_slice(), &full[..k], "prefix k={k}");
        }
    }

    // ── Group-15 gap-midpoint semantics (DEC-GAP-MIDPOINT-CONDITIONS-001) ──

    const GAP_ABOVE: u128 = 1u128 << crate::bits::BIT_CLOSE_ABOVE_GAP_MID;
    const GAP_BELOW: u128 = 1u128 << crate::bits::BIT_CLOSE_BELOW_GAP_MID;
    const GAP_NEAR: u128 = 1u128 << crate::bits::BIT_NEAR_GAP_MID;
    const GAP_FAMILY: u128 = GAP_ABOVE | GAP_BELOW | GAP_NEAR;

    /// A single warm day whose X1/pivot rows mint a gap-UP level.
    ///
    /// Day pivot row pdc = 10_000 < opens 10_020 ⇒ gap-UP; X1 high = 10_060,
    /// X2 high = bar-0 high; the per-test bar columns pick the verdicts.
    fn gap_day_cols(n: usize) -> Cols {
        let mut c = cols(n);
        c.day_has_pivot = vec![true];
        c.day_has_fib = vec![true];
        // pdc (slot 9) = 10_000; every other level far away so only the gap
        // family is in play for the assertions below.
        c.day_pivots = (0..PIVOT_ROW_LEN as i64)
            .map(|i| 50_000 + 1_000 * i)
            .collect();
        c.day_pivots[9] = 10_000;
        c.day_fibs = (0..FIB_ROW_LEN as i64)
            .map(|i| 80_000 + 1_000 * i)
            .collect();
        c.day_has_x1 = vec![true];
        c.day_x1_hi = vec![10_060];
        c.day_x1_lo = vec![9_950];
        c.open = vec![10_020; n];
        c.high = vec![10_100; n]; // X2 high = 10_100 ⇒ level = (10_060+10_100)/2 = 10_080
        c.low = vec![9_990; n];
        c.close = vec![10_050; n];
        c
    }

    #[test]
    fn gap_mid_arms_from_the_second_bar_of_the_day() {
        // Bar 0 mints the level (10_080) from its OWN high but must evaluate
        // with NO level (arm-from-bar-2); bar 1 close 10_050 < 10_080 ⇒ BELOW.
        let masks = stage1a_bar_bits_batch_impl(&view(&gap_day_cols(3))).unwrap();
        assert_eq!(
            masks[0] & GAP_FAMILY,
            0,
            "first bar of the day must be unarmed"
        );
        assert_eq!(masks[1] & GAP_FAMILY, GAP_BELOW);
        assert_eq!(masks[2] & GAP_FAMILY, GAP_BELOW);
    }

    #[test]
    fn gap_mid_above_and_near_fire_per_the_evaluator_laws() {
        // Level = 10_080. A later bar closing 10_085 is ABOVE and (10 bps of
        // 10_085 = 10 paisa ≥ |10_085 − 10_080|) NEAR — the strict bit and the
        // near bit legally co-fire (the PDH/PDL family convention).
        let mut c = gap_day_cols(2);
        c.close[1] = 10_085;
        let masks = stage1a_bar_bits_batch_impl(&view(&c)).unwrap();
        assert_eq!(masks[1] & GAP_FAMILY, GAP_ABOVE | GAP_NEAR);
        // An exactly-at-level close fires ONLY near (strict above/below are
        // mutually exclusive and both false at equality).
        let mut c = gap_day_cols(2);
        c.close[1] = 10_080;
        let masks = stage1a_bar_bits_batch_impl(&view(&c)).unwrap();
        assert_eq!(masks[1] & GAP_FAMILY, GAP_NEAR);
    }

    #[test]
    fn gap_down_day_uses_the_low_pair() {
        // Opens 9_980 < pdc 10_000 ⇒ gap-DOWN ⇒ level = midpoint of the LOWs
        // = (9_950 + 9_910) / 2 = 9_930; bar-1 close 9_950 > 9_930 ⇒ ABOVE.
        let mut c = gap_day_cols(2);
        c.open = vec![9_980; 2];
        c.high = vec![10_000; 2];
        c.low = vec![9_910; 2];
        c.close = vec![9_950; 2];
        let masks = stage1a_bar_bits_batch_impl(&view(&c)).unwrap();
        assert_eq!(masks[1] & GAP_FAMILY, GAP_ABOVE);
    }

    #[test]
    fn zero_gap_day_never_arms() {
        // open_of_day == pdc exactly ⇒ compute_gap_mid yields None ⇒ the
        // whole day stays dark on the gap family.
        let mut c = gap_day_cols(3);
        c.open = vec![10_000; 3];
        let masks = stage1a_bar_bits_batch_impl(&view(&c)).unwrap();
        for (i, m) in masks.iter().enumerate() {
            assert_eq!(m & GAP_FAMILY, 0, "bar {i} armed on a zero-gap day");
        }
    }

    #[test]
    fn cold_x1_day_never_arms() {
        let mut c = gap_day_cols(2);
        c.day_has_x1 = vec![false];
        let masks = stage1a_bar_bits_batch_impl(&view(&c)).unwrap();
        assert_eq!(masks[1] & GAP_FAMILY, 0, "no X1 ⇒ no level");
    }

    #[test]
    fn warm_x1_with_cold_pivot_never_arms() {
        // X1 warm but the pivot anchor cold ⇒ no real pdc exists ⇒ no
        // direction ⇒ no level (the placeholder-pdc guard), independent of
        // the anchor-dependent clear (disarmed here so the guard is the ONLY
        // thing keeping the family dark).
        let mut c = gap_day_cols(2);
        c.day_has_pivot = vec![false];
        let mut v = view(&c);
        v.anchor_dependent_mask = 0;
        let masks = stage1a_bar_bits_batch_impl(&v).unwrap();
        assert_eq!(masks[1] & GAP_FAMILY, 0, "cold pivot ⇒ no pdc ⇒ no level");
    }

    #[test]
    fn mid_day_contract_reset_suppresses_the_level() {
        // A reset WITHOUT a day_index bump (structurally legal, never emitted
        // by the sanctioned prepass — the RF-I F2(c) defensive class) mirrors
        // the Python D11 hard reset's cleared prev_session_edge: no level for
        // the remainder of that day, even though the day row's X1 is warm.
        let mut c = gap_day_cols(4);
        c.contract_reset = vec![false, false, true, false];
        let masks = stage1a_bar_bits_batch_impl(&view(&c)).unwrap();
        assert_eq!(masks[1] & GAP_FAMILY, GAP_BELOW, "pre-reset bar armed");
        assert_eq!(masks[2] & GAP_FAMILY, 0, "reset bar re-seeds unarmed");
        assert_eq!(
            masks[3] & GAP_FAMILY,
            0,
            "suppressed for the rest of the day"
        );
    }

    #[test]
    fn reset_paired_with_a_genuine_new_day_clears_the_suppression() {
        // The SANCTIONED roll shape: reset + a new day row. The new day's
        // level minting is governed by ITS day table (X1 warm here), so the
        // suppression must not leak past the genuine day boundary.
        let mut c = gap_day_cols(4);
        c.day_index = vec![0, 0, 1, 1];
        c.contract_reset = vec![false, false, true, false];
        resize_day_tables(&mut c, 2);
        c.day_has_pivot = vec![true, true];
        c.day_has_fib = vec![true, true];
        let mut pivots: Vec<i64> = (0..PIVOT_ROW_LEN as i64)
            .map(|i| 50_000 + 1_000 * i)
            .collect();
        pivots[9] = 10_000;
        c.day_pivots = [pivots.as_slice(), pivots.as_slice()].concat();
        let fibs: Vec<i64> = (0..FIB_ROW_LEN as i64)
            .map(|i| 80_000 + 1_000 * i)
            .collect();
        c.day_fibs = [fibs.as_slice(), fibs.as_slice()].concat();
        c.day_x1_hi = vec![10_060, 10_060];
        c.day_x1_lo = vec![9_950, 9_950];
        c.day_has_x1 = vec![true, true];
        let masks = stage1a_bar_bits_batch_impl(&view(&c)).unwrap();
        assert_eq!(
            masks[2] & GAP_FAMILY,
            0,
            "post-roll day's first bar unarmed"
        );
        assert_eq!(
            masks[3] & GAP_FAMILY,
            GAP_BELOW,
            "post-roll day arms from bar 2"
        );
    }

    #[test]
    fn corrupt_x1_row_is_a_clean_err() {
        // X1 high < low would assert inside compute_gap_mid — the batch core
        // must reject it as an Err (totality law), never a panic.
        let mut c = gap_day_cols(2);
        c.day_x1_hi = vec![9_000];
        c.day_x1_lo = vec![9_950];
        let err = stage1a_bar_bits_batch_impl(&view(&c)).unwrap_err();
        assert!(err.contains("X1 high < low"), "got: {err}");
    }

    #[test]
    fn x1_table_lengths_are_validated() {
        for which in 0..3 {
            let mut c = gap_day_cols(2);
            match which {
                0 => c.day_x1_hi.push(0),
                1 => c.day_x1_lo.push(0),
                _ => c.day_has_x1.push(false),
            }
            let err = stage1a_bar_bits_batch_impl(&view(&c)).unwrap_err();
            assert!(err.contains("length"), "table {which}: {err}");
        }
    }

    #[test]
    fn one_day_bars_stay_dark_via_the_arming_ordinal() {
        // On TF=1day every bar is its session's FIRST TF bar (one day row per
        // bar), so the arm-from-bar-2 ordinal keeps the family dark WITHOUT
        // relying on the applies_to_1day mask (the design's load-bearing
        // ordinal law) — apply_1day_mask stays false here on purpose.
        let mut c = gap_day_cols(3);
        c.day_index = vec![0, 1, 2];
        resize_day_tables(&mut c, 3);
        c.day_has_pivot = vec![true; 3];
        c.day_has_fib = vec![true; 3];
        let mut pivots: Vec<i64> = (0..PIVOT_ROW_LEN as i64)
            .map(|i| 50_000 + 1_000 * i)
            .collect();
        pivots[9] = 10_000;
        c.day_pivots = [pivots.as_slice(), pivots.as_slice(), pivots.as_slice()].concat();
        let fibs: Vec<i64> = (0..FIB_ROW_LEN as i64)
            .map(|i| 80_000 + 1_000 * i)
            .collect();
        c.day_fibs = [fibs.as_slice(), fibs.as_slice(), fibs.as_slice()].concat();
        c.day_x1_hi = vec![10_060; 3];
        c.day_x1_lo = vec![9_950; 3];
        c.day_has_x1 = vec![true; 3];
        let masks = stage1a_bar_bits_batch_impl(&view(&c)).unwrap();
        for (i, m) in masks.iter().enumerate() {
            assert_eq!(m & GAP_FAMILY, 0, "1day-shaped bar {i} must stay dark");
        }
    }
}
