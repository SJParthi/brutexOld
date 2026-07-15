//! Sweep-core kernel — the unified 7-column resident substrate (Slice 2).
//!
//! DEC-SWEEP-CORE-RUST-001 (MIGRATION.md #1739 reserved; umbrella
//! DEC-RUST-FIRST-MANDATE-001), the design-of-record
//! `docs/design/sweep_core/DESIGN_FINAL_RUST_SWEEP_CORE.md` §1.3/§1.4 single
//! `ResidentSweepCore` object: ONE construction per tuple carrying BOTH the
//! ranged fused enumeration (`enumerate_prefilter` — Slice 1's kernel, whose
//! width-generic core [`enumerate_prefilter_core`] and its whole substrate
//! (`EnumPlanes` transposed bitplanes, CNS lex-rank unranking, the
//! `itertools.combinations` odometer, the checked binomial table) MOVED here
//! VERBATIM from `enum_kernel.rs` — the Q1 unification; `ResidentSweepEnum`
//! / `ResidentSweepEnumU128` remain as byte-identical delegating surfaces)
//! AND, from Slice-2 R3, the fused per-mask sim→metrics `fold_block` over
//! the same seven resident columns `ResidentSweepSim` validates
//! (`open/high/low/close_paisa, is_forced, session_idx` via the SAME
//! `trade_sim::build_columns`, plus the width-typed `bar_bits`).
//!
//! Every law documented on `enum_kernel.rs` (the ORDER law, the MIN-HITS
//! SHORT-CIRCUIT law, the charging rule, the exhaustion contract, guaranteed
//! progress, `Result`-never-panic, the honest complexity envelope) governs
//! the moved core UNCHANGED — the frozen-behaviour gate for the move is the
//! untouched crate suite + the Slice-1 Python suites/goldens passing
//! byte-unmodified.
//!
//! Statelessness law (design §3): the resident objects are frozen; the core
//! owns ZERO durable state and ZERO cross-call state — no files, no statics,
//! no caches, no cursors.

#![cfg_attr(not(test), deny(clippy::indexing_slicing))]

use pyo3::exceptions::{PyMemoryError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyBytes;

use crate::metrics::{DrawdownScratch, drawdown_stats_into};
use crate::trade_sim::{
    FillPolicy, MaskWord, SweepColumns, TradeRecord, build_columns, check_level_distance_for,
    policy_from_flags, simulate_one_core,
};

/// Failed-allocation marker prefix — the ONE token the PyErr mapper keys the
/// `PyMemoryError` mapping on (every other core `Err` is a `PyValueError`).
/// Kept `pub(crate)` so the shim and the tests share the single literal (a
/// drifted copy could silently downgrade an OOM to a catchable `ValueError`,
/// defeating the global-stop law).
pub(crate) const ALLOC_ERR_PREFIX: &str = "enumerate_prefilter: allocation";

/// Mask-width abstraction for the two kernel widths (u64 / u128) — the enum
/// twin of `trade_sim::MaskWord` (which is trade-walk-shaped and stays
/// untouched in Slice 1; the fold core consumes it in Slice 2). Both impls
/// monomorphise to the exact per-width code.
pub(crate) trait EnumWord:
    Copy + PartialEq + core::ops::BitAnd<Output = Self> + core::ops::BitOr<Output = Self>
{
    /// Kernel width in bits (64 / 128) — the VALUE bound for every index.
    const BITS: u32;
    /// The empty mask.
    const ZERO: Self;
    /// `1 << pos` — caller must have validated `pos < Self::BITS`.
    fn from_pos(pos: u32) -> Self;
    /// Lowest set bit position, or `None` when zero (transpose iteration).
    fn lowest_set(self) -> Option<u32>;
    /// Clear the lowest set bit (`self & (self - 1)`).
    fn clear_lowest(self) -> Self;
}

impl EnumWord for u64 {
    const BITS: u32 = 64;
    const ZERO: Self = 0;
    #[inline(always)]
    fn from_pos(pos: u32) -> Self {
        1u64 << pos
    }
    #[inline(always)]
    fn lowest_set(self) -> Option<u32> {
        if self == 0 {
            None
        } else {
            Some(self.trailing_zeros())
        }
    }
    #[inline(always)]
    fn clear_lowest(self) -> Self {
        self & self.wrapping_sub(1)
    }
}

impl EnumWord for u128 {
    const BITS: u32 = 128;
    const ZERO: Self = 0;
    #[inline(always)]
    fn from_pos(pos: u32) -> Self {
        1u128 << pos
    }
    #[inline(always)]
    fn lowest_set(self) -> Option<u32> {
        if self == 0 {
            None
        } else {
            Some(self.trailing_zeros())
        }
    }
    #[inline(always)]
    fn clear_lowest(self) -> Self {
        self & self.wrapping_sub(1)
    }
}

/// Condition-major transposed bitplanes over the resident bar window (the
/// design's Slice-1 substrate item 2). Plane `b` occupies words
/// `[b · n_words, (b+1) · n_words)`; bit `i % 64` of word `i / 64` within a
/// plane = bar `i` fires condition `b`. Padding lanes past `n_bars` in the
/// tail word are ZERO in every plane, so plane-AND popcounts never count
/// phantom bars.
pub(crate) struct EnumPlanes {
    words: Vec<u64>,
    n_words: usize,
    n_bars: usize,
}

impl EnumPlanes {
    /// Build the transpose from a bar_bits window — one-off `O(n_bars ·
    /// set_bits_per_bar)` scatter, paid once per resident object (per tuple).
    /// Every set bit of a `W`-typed bar is `< W::BITS` by the input TYPE, so
    /// the scatter slot is always in range.
    pub(crate) fn build<W: EnumWord>(bar_bits: &[W]) -> Result<Self, String> {
        let n_bars = bar_bits.len();
        let n_words = n_bars.div_ceil(64);
        let total = (W::BITS as usize)
            .checked_mul(n_words)
            .ok_or_else(|| format!("{ALLOC_ERR_PREFIX} plane size overflows usize"))?;
        let mut words: Vec<u64> = Vec::new();
        words
            .try_reserve_exact(total)
            .map_err(|e| format!("{ALLOC_ERR_PREFIX} of {total} plane words failed: {e}"))?;
        words.resize(total, 0);
        for (i, &bar) in bar_bits.iter().enumerate() {
            let word_idx = i / 64;
            let lane = 1u64 << (i % 64);
            let mut rem = bar;
            while let Some(b) = rem.lowest_set() {
                let slot = (b as usize) * n_words + word_idx;
                if let Some(w) = words.get_mut(slot) {
                    *w |= lane;
                }
                rem = rem.clear_lowest();
            }
        }
        Ok(Self {
            words,
            n_words,
            n_bars,
        })
    }

    /// Number of resident bars (the seam's `bars_length_mismatch` probe).
    pub(crate) fn n_bars(&self) -> usize {
        self.n_bars
    }

    /// Plane `pos` as a word slice (empty when out of range — structurally
    /// unreachable through the validated entry points; a zero plane is the
    /// safe direction).
    fn plane(&self, pos: u32) -> &[u64] {
        let start = (pos as usize) * self.n_words;
        self.words
            .get(start..start.saturating_add(self.n_words))
            .unwrap_or(&[])
    }

    /// Supermask-hit count for the combination of condition-bit `positions`:
    /// popcount of the AND of the k bitplanes. Arithmetic-identical to
    /// counting bars with `(bar & mask) == mask` — the SAME predicate as
    /// `count_one` / `count_supermasks` (the strict->= parity gate pins it).
    /// `positions` empty (k = 0) counts EVERY bar: `(bar & 0) == 0` always.
    pub(crate) fn count_hits(&self, positions: &[u32]) -> u64 {
        let Some((&first, rest)) = positions.split_first() else {
            return self.n_bars as u64;
        };
        let mut total = 0u64;
        for (wi, &w0) in self.plane(first).iter().enumerate() {
            let mut acc = w0;
            for &p in rest {
                acc &= self.plane(p).get(wi).copied().unwrap_or(0);
            }
            total += u64::from(acc.count_ones());
        }
        total
    }
}

/// Exact Pascal binomial table `C(i, j)` for `0 <= i <= n`, `0 <= j <= k`, in
/// u128 (max relevant value C(128, 64) ≈ 2.4e37 < u128::MAX ≈ 3.4e38 — exact
/// for every reachable `n <= 128`). Checked additions belt the arithmetic
/// anyway: an overflow is a clean Err, never an overflow-checks SIGABRT.
pub(crate) fn binomial_table(n: usize, k: usize) -> Result<Vec<Vec<u128>>, String> {
    let mut rows: Vec<Vec<u128>> = Vec::with_capacity(n + 1);
    for i in 0..=n {
        let mut row: Vec<u128> = Vec::with_capacity(k + 1);
        for j in 0..=k {
            let v = if j == 0 {
                1u128
            } else if j > i {
                0u128
            } else {
                let prev = rows
                    .last()
                    .ok_or_else(|| "enumerate_prefilter: binomial table internal".to_string())?;
                let a = prev.get(j - 1).copied().unwrap_or(0);
                let b = prev.get(j).copied().unwrap_or(0);
                a.checked_add(b).ok_or_else(|| {
                    format!("enumerate_prefilter: C({i}, {j}) overflows the u128 table")
                })?
            };
            row.push(v);
        }
        rows.push(row);
    }
    Ok(rows)
}

/// Table lookup `C(i, j)` — 0 outside the built range (the unranking only
/// consults in-range cells; 0 is the safe identity for out-of-range `j > i`).
pub(crate) fn binom(table: &[Vec<u128>], i: usize, j: usize) -> u128 {
    table
        .get(i)
        .and_then(|row| row.get(j))
        .copied()
        .unwrap_or(0)
}

/// LEX-RANK CNS unranking: `rank` → the k ascending combination indices into
/// the sorted-unique position list, in `itertools.combinations` lexicographic
/// order (the design's Slice-1 item-1 MANDATE — NOT the standard/colex CNS,
/// which would silently reorder emission; witness n=4, k=2: lex masks
/// `[3, 5, 9, 6, 10, 12]` vs numeric `[3, 5, 6, 9, 10, 12]`). Caller
/// establishes `rank < C(n, k)`.
pub(crate) fn unrank_lex(
    table: &[Vec<u128>],
    n: usize,
    k: usize,
    rank: u128,
) -> Result<Vec<usize>, String> {
    let mut idx: Vec<usize> = Vec::with_capacity(k);
    let mut rem = rank;
    let mut cand = 0usize;
    for j in 0..k {
        let mut chosen: Option<usize> = None;
        for c in cand..=(n - (k - j)) {
            // Combinations with slot j fixed at c: C(n-1-c, k-1-j).
            let cnt = binom(table, n - 1 - c, k - 1 - j);
            if rem < cnt {
                chosen = Some(c);
                break;
            }
            rem -= cnt;
        }
        let c = chosen.ok_or_else(|| {
            "enumerate_prefilter: unrank rank out of domain (internal)".to_string()
        })?;
        idx.push(c);
        cand = c + 1;
    }
    Ok(idx)
}

/// LEX rank of an ascending combination — the inverse of [`unrank_lex`]
/// (kept test-only: it is the round-trip proptest's oracle, mirroring the
/// crate's `simulate_one` test-delegator convention).
#[cfg(test)]
pub(crate) fn rank_lex(table: &[Vec<u128>], n: usize, k: usize, indices: &[usize]) -> u128 {
    let mut rank = 0u128;
    let mut prev = 0usize;
    for (j, &cj) in indices.iter().enumerate() {
        for c in prev..cj {
            rank += binom(table, n - 1 - c, k - 1 - j);
        }
        prev = cj + 1;
    }
    rank
}

/// Advance the k-position odometer one step in `itertools.combinations`
/// lexicographic order — a literal transcription of the CPython advance.
/// Returns `false` when the LAST combination was already held (exhausted).
pub(crate) fn advance_odometer(idx: &mut [usize], n: usize, k: usize) -> bool {
    let mut slot = k;
    for i in (0..k).rev() {
        let at_limit = idx.get(i).copied() == Some(i + n - k);
        if !at_limit {
            slot = i;
            break;
        }
    }
    if slot == k {
        return false;
    }
    if let Some(v) = idx.get_mut(slot) {
        *v += 1;
    }
    for j in (slot + 1)..k {
        let prev = idx.get(j - 1).copied().unwrap_or(0);
        if let Some(v) = idx.get_mut(j) {
            *v = prev + 1;
        }
    }
    true
}

/// Reserve the kept-mask buffer — `try_reserve`, never a bare
/// `Vec::with_capacity` (whose alloc failure would abort the interpreter
/// under `panic = "abort"`). Split out so the failure arm is directly
/// unit-testable.
pub(crate) fn reserve_kept<W: EnumWord>(expected: u64) -> Result<Vec<W>, String> {
    let mut out: Vec<W> = Vec::new();
    let want = usize::try_from(expected).unwrap_or(usize::MAX);
    out.try_reserve(want)
        .map_err(|e| format!("{ALLOC_ERR_PREFIX} of {expected} kept masks failed: {e}"))?;
    Ok(out)
}

/// Pure SHARED core of the ranged `enumerate_prefilter` FFI (design §1.3) —
/// `Result<_, String>` (NO `PyErr`) so the no-libpython `cargo test` link
/// exercises every branch. Returns the design sextuple
/// `(enumerated_in_range, conflict_pruned_in_range, next_rank, exhausted,
/// hits_counted_in_range, kept_masks)` with
/// `kept = conflict-free AND (min_hits == 0 OR supermask_hits >= min_hits)`
/// (STRICT `>=` boundary) in `itertools.combinations` lexicographic order.
#[allow(clippy::too_many_arguments)]
pub(crate) fn enumerate_prefilter_core<W: EnumWord>(
    planes: &EnumPlanes,
    condition_bits: &[u32],
    k: usize,
    conflict_pairs: &[(u32, u32)],
    min_hits: u64,
    start_rank: u64,
    max_enumerated: u64,
    max_work_cells: u64,
) -> Result<(u64, u64, u64, bool, u64, Vec<W>), String> {
    // Normalise exactly as Python does: `sorted(set(bits))`.
    let mut unique: Vec<u32> = condition_bits.to_vec();
    unique.sort_unstable();
    unique.dedup();
    // (b) VALUE bound — every index < W::BITS, validated BEFORE any shift
    // (bits=[100] on the u64 core must be an Err, never a shift-overflow
    // SIGABRT — design §1.3 round-2 fix).
    for &pos in &unique {
        if pos >= W::BITS {
            return Err(format!(
                "enumerate_prefilter: condition bit {pos} out of range [0, {})",
                W::BITS
            ));
        }
    }
    let n = unique.len();
    // (a) COUNT bound — k <= n_distinct; k == 0 is LEGAL (C(n, 0) = 1, the
    // single empty mask — the validation must NEVER tighten to 1 <= k).
    if k > n {
        return Err(format!(
            "enumerate_prefilter: invalid k={k} for {n} unique bits (require 0 <= k <= n)"
        ));
    }
    for &(a, b) in conflict_pairs {
        if a >= b || b >= W::BITS {
            return Err(format!(
                "enumerate_prefilter: malformed conflict pair ({a}, {b}) \
                 (require a < b < {})",
                W::BITS
            ));
        }
    }

    let table = binomial_table(n, k)?;
    let total_u128 = binom(&table, n, k);
    if total_u128 > u128::from(u64::MAX) {
        return Err(format!(
            "enumerate_prefilter: C({n}, {k}) = {total_u128} exceeds the u64 rank domain"
        ));
    }
    let total = total_u128 as u64;
    // (c) RANK bound — `== total` is legal (exhausted-empty); `>` has NO
    // defined combination and a "saturate" implementation would return a
    // silently WRONG kept set, hence the refusal.
    if start_rank > total {
        return Err(format!(
            "enumerate_prefilter: start_rank {start_rank} out of domain (total {total})"
        ));
    }
    if start_rank == total {
        return Ok((0, 0, total, true, 0, Vec::new()));
    }

    // Every shift below is proven `< W::BITS` by the (b) validation above.
    let pos_mask: Vec<W> = unique.iter().map(|&p| W::from_pos(p)).collect();
    // Pair bits are DISTINCT (a < b validated), so `|` sets exactly two bits.
    // Duplicate pairs are tolerated (frozenset semantics: `any()` over
    // duplicates is unchanged); no pairs-to-bits prefilter (module docstring).
    let pair_masks: Vec<W> = conflict_pairs
        .iter()
        .map(|&(a, b)| W::from_pos(a) | W::from_pos(b))
        .collect();

    // The `max(1, …)` floor idiom (sweep_run.py:203): every non-exhausted
    // call enumerates AT LEAST ONE mask — the guaranteed-progress law.
    let cap_enumerated = max_enumerated.max(1);
    let n_bars_u64 = planes.n_bars as u64;
    let range_len = total - start_rank;
    // Kept reservation bounded by range length, the enumerated cap, and the
    // cells-implied mask bound (so a huge-cap direct call over a huge range
    // fails in try_reserve cleanly, never aborts).
    let cells_bound = if n_bars_u64 == 0 {
        u64::MAX
    } else {
        (max_work_cells / n_bars_u64).saturating_add(1)
    };
    let reserve = range_len.min(cap_enumerated).min(cells_bound);
    let mut kept = reserve_kept::<W>(reserve)?;

    let mut idx = unrank_lex(&table, n, k, u128::from(start_rank))?;
    let mut pos_scratch: Vec<u32> = Vec::with_capacity(k);
    let mut enumerated = 0u64;
    let mut conflict_pruned = 0u64;
    let mut hits_counted = 0u64;
    let mut cells = 0u64;
    let mut rank = start_rank;

    loop {
        // O(k) mask rebuild (k <= 6 in production).
        let mut mask = W::ZERO;
        for &i in &idx {
            mask = mask | pos_mask.get(i).copied().unwrap_or(W::ZERO);
        }
        enumerated += 1;
        rank += 1;
        // CHARGING RULE (design §1.3, explicit): cells are charged per
        // enumerated mask × n_bars REGARDLESS of the min-hits short-circuit —
        // deliberately min-hits-BLIND (over-charging only SHORTENS GIL holds;
        // results are cap-invariant). Do not "fix" this to actual O(k) work
        // at min_hits == 0 — the cap-respected pins encode it.
        cells = cells.saturating_add(n_bars_u64);
        // The literal `has_conflict` transcription: a pair fires iff BOTH its
        // bits are set, i.e. `mask & pair_mask == pair_mask` (a SUBSET test
        // per element — clippy's `manual_contains` suggestion is not
        // equivalent: it would search for one VALUE, not apply the
        // per-element subset predicate).
        #[allow(clippy::manual_contains)]
        let conflicted = pair_masks.iter().any(|&c| (mask & c) == c);
        if conflicted {
            conflict_pruned += 1;
        } else if min_hits == 0 {
            // MIN-HITS SHORT-CIRCUIT LAW (design §1.3, round-6): at
            // min_hits == 0 the per-mask O(n_bars) count MUST NOT run —
            // kept = conflict-free directly, exactly today's spine.
            // `hits_counted` stays 0: the in-band deterministic net.
            kept.push(mask);
        } else {
            // Fused Stage-1B count on the resident bitplanes — the SAME
            // supermask predicate as count_one / count_supermasks.
            pos_scratch.clear();
            for &i in &idx {
                pos_scratch.push(unique.get(i).copied().unwrap_or(0));
            }
            let hits = planes.count_hits(&pos_scratch);
            hits_counted += 1;
            // STRICT `>=` boundary (Verifier B F15/F18): hits == min_hits is
            // KEPT; hits == min_hits − 1 is dropped.
            if hits >= min_hits {
                kept.push(mask);
            }
        }
        // Caps are checked AFTER each mask's work (the guaranteed-progress
        // law): `next_rank > start_rank` on every non-exhausted return.
        if enumerated >= cap_enumerated || cells >= max_work_cells {
            break;
        }
        if !advance_odometer(&mut idx, n, k) {
            break;
        }
    }
    // EXHAUSTION CONTRACT (design §1.3, round-3): in-band, computed as
    // `next_rank == total_count` — the driver loops on this flag, never on
    // "returned empty" / "no progress" heuristics.
    let exhausted = rank == total;
    Ok((
        enumerated,
        conflict_pruned,
        rank,
        exhausted,
        hits_counted,
        kept,
    ))
}

/// Map a core `Err` onto the Python exception taxonomy: allocation failures
/// (the [`ALLOC_ERR_PREFIX`] token) raise `MemoryError` (the global-stop
/// class — the wrapper re-raises BARE); every other refusal is `ValueError`
/// (the wrapper's narrow fallback catch).
pub(crate) fn map_enum_err(msg: String) -> PyErr {
    if msg.starts_with(ALLOC_ERR_PREFIX) {
        PyMemoryError::new_err(msg)
    } else {
        PyValueError::new_err(msg)
    }
}

/// The width-generic interior of the unified core pyclasses: the seven
/// validated resident columns + the width-typed `bar_bits` + the transposed
/// enumeration bitplanes. Built once per tuple (design §1.4).
pub(crate) struct SweepCoreInner<W> {
    /// The six width-independent per-bar columns (`ResidentSweepSim`-identical
    /// validation via `trade_sim::build_columns`). Consumed by `fold_block`
    /// (Slice-2 R3); held from construction so the SAME object serves both
    /// arms with one validation pass.
    pub(crate) columns: SweepColumns,
    /// The width-typed per-bar condition bitsets (the walk's entry stream).
    pub(crate) bar_bits: Vec<W>,
    /// The condition-major transposed bitplanes (the enumeration substrate).
    pub(crate) planes: EnumPlanes,
}

impl<W: EnumWord> SweepCoreInner<W> {
    /// Build + validate the unified resident substrate — `Result<_, String>`
    /// (NO `PyErr`) so the no-libpython `cargo test` link exercises every
    /// branch. Column validation is IDENTICAL to `ResidentSweepSim::__new__`
    /// (the same `build_columns` — length equality across all seven columns +
    /// the R2-RC-F1 price-boundary scan), then the bitplane transpose.
    pub(crate) fn build(
        label: &str,
        open: Vec<i64>,
        high: Vec<i64>,
        low: Vec<i64>,
        close: Vec<i64>,
        is_forced: Vec<bool>,
        session_idx: Vec<i64>,
        bar_bits: Vec<W>,
    ) -> Result<Self, String> {
        let columns = build_columns(
            label,
            open,
            high,
            low,
            close,
            is_forced,
            session_idx,
            bar_bits.len(),
        )?;
        let planes = EnumPlanes::build::<W>(&bar_bits)?;
        Ok(Self {
            columns,
            bar_bits,
            planes,
        })
    }

    /// Number of resident bars — the seam's `bars_length_mismatch` probe
    /// (design §1.3 F27: asserted against the Python-side window length
    /// immediately after construction, before ANY call).
    pub(crate) fn n_bars(&self) -> usize {
        self.planes.n_bars()
    }
}

// ────────────────────────────────────────────────────────────────────────
// Slice-2 R3 — the fused per-mask sim→metrics fold (design §1.3/§2.3)
// ────────────────────────────────────────────────────────────────────────

/// The fold ABI version — the FIRST element of every `fold_block` return;
/// the Python decode refuses any other value (`abi_version_mismatch`).
pub(crate) const SWEEP_CORE_ABI_VERSION: u32 = 1;

/// i64 columns per mask row (design §1.3 column law).
pub(crate) const FOLD_INT_STRIDE: usize = 15;

/// f64 columns per mask row (design §1.3 column law).
pub(crate) const FOLD_FLOAT_STRIDE: usize = 9;

/// The in-core whole-chunk work ceiling: `masks.len() × n_bars` past this
/// refuses the WHOLE chunk with `Err("fold_work_ceiling")` (design §3 —
/// 2× the Python fold knob's own ≤2^27 upper clamp, production-unreachable
/// by construction; the seam maps the string to the NAMED fallback reason).
pub(crate) const FOLD_WORK_CEILING_CELLS: u64 = 1 << 28;

/// The exact Err token [`fold_block_core`] returns at the ceiling — the ONE
/// string the Python seam maps to its `fold_work_ceiling` fallback reason.
pub(crate) const FOLD_WORK_CEILING_ERR: &str = "fold_work_ceiling";

/// Flag bit 0: a zero-trade mask — zeroed row, NO drawdown scan, NO float
/// folds (the §1.3 short-circuit law; Python serves `_empty_metrics`).
pub(crate) const FLAG_ZERO_TRADE: u8 = 1;

/// Flag bit 1: `python_required` — the 2^52 envelope was exceeded, an i64
/// narrowing failed, or a non-finite float fold appeared; the row is FULLY
/// ZEROED (Python re-derives the mask on today's spine and never reads it).
pub(crate) const FLAG_PYTHON_REQUIRED: u8 = 2;

/// The i64 column names, in ROW ORDER — exported to Python as
/// `SWEEP_CORE_FOLD_INT_FIELDS` (the layout-golden mechanism: the Python
/// wrapper pins its own tuple literally equal to this one).
pub(crate) const FOLD_INT_FIELDS: [&str; FOLD_INT_STRIDE] = [
    "n_trades",
    "net_pnl",
    "gross_profit",
    "gross_loss_abs",
    "winners",
    "losers",
    "sum_mfe",
    "sum_mae",
    "sum_holding",
    "max_holding",
    "largest_loss",
    "max_consecutive_losses",
    "max_consecutive_wins",
    "max_dd_paisa",
    "max_dd_bars",
];

/// The f64 column names, in ROW ORDER — exported as
/// `SWEEP_CORE_FOLD_FLOAT_FIELDS`.
pub(crate) const FOLD_FLOAT_FIELDS: [&str; FOLD_FLOAT_STRIDE] = [
    "mean",
    "m2",
    "m3",
    "m4",
    "sum_sq_downside",
    "p95",
    "p5",
    "max_dd_pct",
    "ulcer",
];

/// The f64-exactness envelope (2^52) as i128 — `start + Σ|pnl|` STRICTLY
/// above it flags `python_required` (the comparison operator token-mirrored
/// from `exceeds_exact_equity_envelope`: strict `>`).
const ENVELOPE_I128: i128 = 1_i128 << 52;

/// One Neumaier compensation step — CPython 3.12's `builtins.sum()` float
/// fast path (Python/bltinmodule.c, gh-100425), token-for-token: running
/// `f_result`, compensation `c`, the `|f_result| >= |x|` branch. The tie
/// (`|f_result| == |x|`) is branch-equivalent by algebra (both arms yield
/// the identical compensation term when the magnitudes tie) — the recorded
/// provably-equivalent cargo-mutants exclusion (`>=` → `>`).
#[inline]
pub(crate) fn neumaier_add(f_result: &mut f64, c: &mut f64, x: f64) {
    let t = *f_result + x;
    if f_result.abs() >= x.abs() {
        *c += (*f_result - t) + x;
    } else {
        *c += (x - t) + *f_result;
    }
    *f_result = t;
}

/// Neumaier-compensated sum with the INT-START seed + the GUARDED final add.
///
/// Seed `f_result = x₀, c = 0.0` (bit-identical to CPython's int→float
/// transition `0 + x₀` for every finite `x₀` except `-0.0` — outside the
/// fold domain, whose summands are non-negative squares); per item
/// [`neumaier_add`]; final add GUARDED `if c != 0.0 && c.is_finite()`
/// (CPython does NOT unconditionally add — the guard preserves an
/// inf/overflowed sum from becoming NaN). Empty input returns `0.0`
/// (`builtins.sum([])` is int 0, which packs identically through `/ n`).
pub(crate) fn neumaier_sum(values: impl Iterator<Item = f64>) -> f64 {
    let mut seeded = false;
    let mut f_result = 0.0_f64;
    let mut c = 0.0_f64;
    for x in values {
        if !seeded {
            f_result = x;
            seeded = true;
            continue;
        }
        neumaier_add(&mut f_result, &mut c, x);
    }
    if c != 0.0 && c.is_finite() {
        f_result += c;
    }
    f_result
}

/// The downside Σr² fold — squares of the STRICTLY negative returns, in
/// trade order (the genexpr order of `sum(r * r for r in downside)` at
/// compute.py:703), Neumaier-summed. The strict `r < 0.0` filter is
/// token-identical to the Python filter (`-0.0` excluded in both); the
/// `<` → `<=` mutant is provably equivalent (an admitted `±0.0` return
/// contributes a `+0.0` square, which perturbs neither the seed nor any
/// compensation term) — the second recorded cargo-mutants exclusion.
pub(crate) fn downside_accumulate(returns: &[f64]) -> f64 {
    neumaier_sum(returns.iter().filter(|r| **r < 0.0).map(|r| r * r))
}

/// Streaming Welford/Pébay first-four central moments — token-for-token
/// from `compute.py:136-152` (SAND2008-6212): exact update expressions,
/// exact operand order, `(n*n − 3*n + 3)` in INTEGER (i128 — exact for
/// every reachable n; converted once, as Python converts its int once),
/// m4 before m3 before m2, strictly sequential left-to-right. Returns the
/// raw `(mean, m2, m3, m4)` — Python constructs `Moments(...)`.
pub(crate) fn accumulate_moments_fold(values: &[f64]) -> (f64, f64, f64, f64) {
    let mut n: i128 = 0;
    let mut mean = 0.0_f64;
    let mut m2 = 0.0_f64;
    let mut m3 = 0.0_f64;
    let mut m4 = 0.0_f64;
    for &x in values {
        let n1 = n;
        n += 1;
        let delta = x - mean;
        let delta_n = delta / (n as f64);
        let delta_n2 = delta_n * delta_n;
        let term1 = delta * delta_n * (n1 as f64);
        mean += delta_n;
        m4 += term1 * delta_n2 * ((n * n - 3 * n + 3) as f64) + 6.0 * delta_n2 * m2
            - 4.0 * delta_n * m3;
        m3 += term1 * delta_n * ((n - 2) as f64) - 3.0 * delta_n * m2;
        m2 += term1;
    }
    (mean, m2, m3, m4)
}

/// The two-branch linear-interpolation percentile — token-for-token from
/// `compute.py:272-280` over a PRE-SORTED series: (a) the single-element
/// early return; (b) the `lower == upper` exact-index return (integral
/// rank); then the lerp `lo*(1−f) + hi*f`. `f64::floor`/`ceil` are exact;
/// the `checked` accessors satisfy the module's indexing deny (an
/// out-of-range read is structurally unreachable — `rank ≤ len−1` — and
/// reads `0.0`, the safe direction). Empty input returns `0.0`
/// (unreachable through the fold: a plain row has ≥1 trade).
pub(crate) fn percentile_lerp(sorted_values: &[f64], pct: f64) -> f64 {
    if sorted_values.len() == 1 {
        return sorted_values.first().copied().unwrap_or(0.0);
    }
    let rank = (pct / 100.0) * ((sorted_values.len().saturating_sub(1)) as f64);
    let lower = rank.floor();
    let upper = rank.ceil();
    let lo = sorted_values.get(lower as usize).copied().unwrap_or(0.0);
    if lower == upper {
        return lo;
    }
    let hi = sorted_values.get(upper as usize).copied().unwrap_or(0.0);
    let frac = rank - lower;
    lo * (1.0 - frac) + hi * frac
}

/// True iff every value is finite — the pre-scan/final float guard (any
/// non-finite anywhere ⇒ `python_required`, the row unread by Python).
pub(crate) fn fold_guard_finite(values: &[f64]) -> bool {
    values.iter().all(|v| v.is_finite())
}

/// One fold row: the 15 i64 columns + 9 f64 columns + flags byte + whether
/// the drawdown scan EXECUTED (the `scans_run` law's input — counted by the
/// caller ONLY for rows that end plain, so `scans_run == count(plain rows)`
/// holds by construction).
pub(crate) type FoldRow = ([i64; FOLD_INT_STRIDE], [f64; FOLD_FLOAT_STRIDE], u8, bool);

/// A fully-zeroed row with `flags` (the bit0/bit1 arms).
fn zeroed_row(flags: u8) -> FoldRow {
    ([0; FOLD_INT_STRIDE], [0.0; FOLD_FLOAT_STRIDE], flags, false)
}

/// The §2.3 per-mask fold pipeline over one simulated trade list — ORDER IS
/// LAW: zero-trade short-circuit → integer folds (i128 accumulate, checked
/// i64 narrow ⇒ bit1) → the 2^52 envelope gate (strict `>` ⇒ bit1) →
/// returns `(pnl as f64)/(start as f64)` → Pébay moments → Neumaier
/// downside Σr² → finite pre-pass over returns (BEFORE the sort — a NaN
/// never reaches the comparator) → STABLE `sort_by(partial_cmp)` (never
/// `total_cmp`: it orders `-0.0 < +0.0` where Python's stable `sorted`
/// keeps input order on ties) → percentile lerp → finite guard → SCAN LAST
/// (`drawdown_stats_into`, scratch reused across the chunk; NOT invoked for
/// zero-trade masks; a scan `Err` — structurally unreachable, the envelope
/// is identical and walker exit indices are bar indices by construction —
/// demotes to bit1 with the scan NOT counted) → final finite guard over all
/// nine floats. A bit1 row is FULLY ZEROED.
///
/// `Err` is returned ONLY for a failed buffer reservation (the
/// `MemoryError` class the wrapper re-raises bare).
pub(crate) fn fold_trades_core(
    trades: &[TradeRecord],
    n_bars: usize,
    start_equity_paisa: i64,
    scratch: &mut DrawdownScratch,
) -> Result<FoldRow, String> {
    if trades.is_empty() {
        return Ok(zeroed_row(FLAG_ZERO_TRADE));
    }
    // ── integer folds (i128 accumulate) — §2.3 step 3 ───────────────────
    let mut net_pnl: i128 = 0;
    let mut gross_profit: i128 = 0;
    let mut gross_loss_abs: i128 = 0;
    let mut winners: i128 = 0;
    let mut losers: i128 = 0;
    let mut sum_mfe: i128 = 0;
    let mut sum_mae: i128 = 0;
    let mut sum_holding: i128 = 0;
    let mut max_holding: i128 = 0;
    let mut largest_loss: i128 = 0;
    let mut max_losses: i128 = 0;
    let mut max_wins: i128 = 0;
    let mut cur_losses: i128 = 0;
    let mut cur_wins: i128 = 0;
    for &(pnl, mfe, mae, holding, _entry_idx, _exit_idx) in trades {
        net_pnl += i128::from(pnl);
        sum_mfe += i128::from(mfe);
        sum_mae += i128::from(mae);
        sum_holding += i128::from(holding);
        max_holding = max_holding.max(i128::from(holding));
        if pnl > 0 {
            winners += 1;
            gross_profit += i128::from(pnl);
            cur_wins += 1;
            cur_losses = 0;
        } else if pnl < 0 {
            losers += 1;
            gross_loss_abs += -i128::from(pnl);
            largest_loss = largest_loss.min(i128::from(pnl));
            cur_losses += 1;
            cur_wins = 0;
        } else {
            cur_wins = 0;
            cur_losses = 0;
        }
        max_wins = max_wins.max(cur_wins);
        max_losses = max_losses.max(cur_losses);
    }
    let ints_head: [i128; 13] = [
        trades.len() as i128,
        net_pnl,
        gross_profit,
        gross_loss_abs,
        winners,
        losers,
        sum_mfe,
        sum_mae,
        sum_holding,
        max_holding,
        largest_loss,
        max_losses,
        max_wins,
    ];
    let mut ints = [0_i64; FOLD_INT_STRIDE];
    for (slot, &wide) in ints.iter_mut().zip(ints_head.iter()) {
        match i64::try_from(wide) {
            Ok(narrow) => *slot = narrow,
            // Checked narrow failure ⇒ Python bigint semantics preserved by
            // FALLBACK, never emulated (§2 ownership row).
            Err(_) => return Ok(zeroed_row(FLAG_PYTHON_REQUIRED)),
        }
    }
    // ── the 2^52 envelope gate — §2.3 step 4 (strict `>`) ───────────────
    let mut magnitude: i128 = i128::from(start_equity_paisa);
    for &(pnl, ..) in trades {
        magnitude += i128::from(pnl).abs();
    }
    if magnitude > ENVELOPE_I128 {
        return Ok(zeroed_row(FLAG_PYTHON_REQUIRED));
    }
    // ── returns — §2.3 step 5 (exact int→f64 within the envelope) ───────
    let mut returns: Vec<f64> = Vec::new();
    returns
        .try_reserve_exact(trades.len())
        .map_err(|e| format!("{ALLOC_ERR_PREFIX} of {} returns failed: {e}", trades.len()))?;
    let start_f = start_equity_paisa as f64;
    for &(pnl, ..) in trades {
        returns.push((pnl as f64) / start_f);
    }
    // ── float folds — §2.3 steps 6-9 ────────────────────────────────────
    let (mean, m2, m3, m4) = accumulate_moments_fold(&returns);
    let sum_sq_downside = downside_accumulate(&returns);
    // Finite pre-pass BEFORE the sort: a NaN must never reach the
    // comparator (under panic="abort" a sort panic would kill the worker).
    // Structurally unreachable inside the envelope — a belt.
    if !fold_guard_finite(&returns) {
        return Ok(zeroed_row(FLAG_PYTHON_REQUIRED));
    }
    let mut sorted_returns: Vec<f64> = Vec::new();
    sorted_returns
        .try_reserve_exact(returns.len())
        .map_err(|e| {
            format!(
                "{ALLOC_ERR_PREFIX} of {} sorted returns failed: {e}",
                returns.len()
            )
        })?;
    sorted_returns.extend_from_slice(&returns);
    // STABLE sort — Python's stable `sorted` twin; the `Equal` fallback is
    // unreachable post-guard (comment-pinned; comparator returns Ordering
    // under the validated all-finite precondition — no unwrap).
    sorted_returns.sort_by(|a, b| a.partial_cmp(b).unwrap_or(core::cmp::Ordering::Equal));
    let p95 = percentile_lerp(&sorted_returns, 95.0);
    let p5 = percentile_lerp(&sorted_returns, 5.0);
    let pre_scan = [mean, m2, m3, m4, sum_sq_downside, p95, p5];
    if !fold_guard_finite(&pre_scan) {
        return Ok(zeroed_row(FLAG_PYTHON_REQUIRED));
    }
    // ── SCAN LAST — §2.3 step 11 ────────────────────────────────────────
    let mut exits: Vec<(usize, i64)> = Vec::new();
    exits
        .try_reserve_exact(trades.len())
        .map_err(|e| format!("{ALLOC_ERR_PREFIX} of {} exits failed: {e}", trades.len()))?;
    for &(pnl, _mfe, _mae, _holding, _entry_idx, exit_idx) in trades {
        match usize::try_from(exit_idx) {
            Ok(idx) => exits.push((idx, pnl)),
            Err(_) => return Ok(zeroed_row(FLAG_PYTHON_REQUIRED)),
        }
    }
    let Ok((max_dd_paisa, max_dd_pct, max_dd_bars, ulcer)) =
        drawdown_stats_into(n_bars, start_equity_paisa, &exits, scratch)
    else {
        // Structurally unreachable (identical envelope; walker indices are
        // bar indices) — demote WITHOUT counting the scan, so the
        // `scans_run == count(plain rows)` law stays airtight.
        return Ok(zeroed_row(FLAG_PYTHON_REQUIRED));
    };
    let floats = [
        mean,
        m2,
        m3,
        m4,
        sum_sq_downside,
        p95,
        p5,
        max_dd_pct,
        ulcer,
    ];
    if !fold_guard_finite(&floats) {
        return Ok(zeroed_row(FLAG_PYTHON_REQUIRED));
    }
    let (Ok(dd_paisa_i64), Ok(dd_bars_i64)) =
        (i64::try_from(max_dd_paisa), i64::try_from(max_dd_bars))
    else {
        return Ok(zeroed_row(FLAG_PYTHON_REQUIRED));
    };
    let mut ints_full = [0_i64; FOLD_INT_STRIDE];
    for (slot, &v) in ints_full.iter_mut().zip(ints.iter()) {
        *slot = v;
    }
    if let Some(slot) = ints_full.get_mut(13) {
        *slot = dd_paisa_i64;
    }
    if let Some(slot) = ints_full.get_mut(14) {
        *slot = dd_bars_i64;
    }
    Ok((ints_full, floats, 0, true))
}

/// Pure core of the pyclass `fold_block` methods — `Result<_, String>` (NO
/// `PyErr`) so the no-libpython `cargo test` link exercises every branch.
/// Walks each mask via [`simulate_one_core`] (trades NEVER cross the FFI —
/// the G4-b acceptance criterion) and folds it via [`fold_trades_core`]
/// with ONE [`DrawdownScratch`] reused across the whole chunk. Returns
/// `(scans_run, ints ×15/row, floats ×9/row, flags ×1/row)` in INPUT mask
/// order. `Err(`[`FOLD_WORK_CEILING_ERR`]`)` when
/// `masks.len() × n_bars > 2^28` (whole-chunk refusal, checked FIRST);
/// SL/TP distances validated exactly as `simulate_masks_core` validates
/// them; allocation failures carry the [`ALLOC_ERR_PREFIX`] token (mapped
/// to `MemoryError`).
#[allow(clippy::too_many_arguments)]
pub(crate) fn fold_block_core<W: MaskWord>(
    columns: &SweepColumns,
    bar_bits: &[W],
    label: &str,
    masks: &[W],
    is_buy: bool,
    sl_paisa: Option<i64>,
    tp_paisa: Option<i64>,
    policy: FillPolicy,
    start_equity_paisa: i64,
    n_bars: usize,
) -> Result<(u64, Vec<i64>, Vec<f64>, Vec<u8>), String> {
    let work = (masks.len() as u64).saturating_mul(n_bars as u64);
    if work > FOLD_WORK_CEILING_CELLS {
        return Err(FOLD_WORK_CEILING_ERR.to_string());
    }
    check_level_distance_for(label, "sl_paisa", sl_paisa)?;
    check_level_distance_for(label, "tp_paisa", tp_paisa)?;
    let mut ints: Vec<i64> = Vec::new();
    ints.try_reserve_exact(masks.len() * FOLD_INT_STRIDE)
        .map_err(|e| format!("{ALLOC_ERR_PREFIX} of the int column failed: {e}"))?;
    let mut floats: Vec<f64> = Vec::new();
    floats
        .try_reserve_exact(masks.len() * FOLD_FLOAT_STRIDE)
        .map_err(|e| format!("{ALLOC_ERR_PREFIX} of the float column failed: {e}"))?;
    let mut flags: Vec<u8> = Vec::new();
    flags
        .try_reserve_exact(masks.len())
        .map_err(|e| format!("{ALLOC_ERR_PREFIX} of the flags column failed: {e}"))?;
    let mut scratch = DrawdownScratch::default();
    let mut scans_run: u64 = 0;
    for &mask in masks {
        let trades = simulate_one_core(columns, bar_bits, mask, is_buy, sl_paisa, tp_paisa, policy);
        let (row_ints, row_floats, row_flags, scan_ran) =
            fold_trades_core(&trades, n_bars, start_equity_paisa, &mut scratch)?;
        ints.extend_from_slice(&row_ints);
        floats.extend_from_slice(&row_floats);
        flags.push(row_flags);
        if scan_ran {
            scans_run += 1;
        }
    }
    Ok((scans_run, ints, floats, flags))
}

/// Encode the i64 column as LE bytes (`'<q'` — the decode contract).
fn ints_to_le(values: &[i64]) -> Result<Vec<u8>, String> {
    let mut out: Vec<u8> = Vec::new();
    out.try_reserve_exact(values.len() * 8)
        .map_err(|e| format!("{ALLOC_ERR_PREFIX} of the int byte column failed: {e}"))?;
    for v in values {
        out.extend_from_slice(&v.to_le_bytes());
    }
    Ok(out)
}

/// Encode the f64 column as LE bytes (`'<d'` — bit-preserving).
fn floats_to_le(values: &[f64]) -> Result<Vec<u8>, String> {
    let mut out: Vec<u8> = Vec::new();
    out.try_reserve_exact(values.len() * 8)
        .map_err(|e| format!("{ALLOC_ERR_PREFIX} of the float byte column failed: {e}"))?;
    for v in values {
        out.extend_from_slice(&v.to_le_bytes());
    }
    Ok(out)
}

/// Resident u64-width unified sweep-core substrate (design §1.3): the seven
/// per-bar columns + bitplanes, carrying the ranged fused enumeration (and,
/// from Slice-2 R3, `fold_block`). Frozen: zero cross-call state.
#[pyclass(frozen, module = "fno_math")]
pub struct ResidentSweepCore {
    inner: SweepCoreInner<u64>,
}

#[pymethods]
impl ResidentSweepCore {
    /// Build the resident 7-column substrate (u64 width — every bar value
    /// < 2^64; PyO3 raises `OverflowError` on a wider int, which the seam
    /// maps to its `kernel_error` fallback). Validation is identical to
    /// `ResidentSweepSim::__new__` + the enum bitplane transpose.
    #[new]
    fn new(
        open_paisa: Vec<i64>,
        high_paisa: Vec<i64>,
        low_paisa: Vec<i64>,
        close_paisa: Vec<i64>,
        is_forced: Vec<bool>,
        session_idx: Vec<i64>,
        bar_bits: Vec<u64>,
    ) -> PyResult<Self> {
        let inner = SweepCoreInner::build(
            "ResidentSweepCore",
            open_paisa,
            high_paisa,
            low_paisa,
            close_paisa,
            is_forced,
            session_idx,
            bar_bits,
        )
        .map_err(map_enum_err)?;
        Ok(Self { inner })
    }

    /// Number of resident bars — the seam's `bars_length_mismatch` probe.
    fn n_bars(&self) -> usize {
        self.inner.n_bars()
    }

    /// Ranged fused enumeration + conflict prune + Stage-1B min-hits count —
    /// the design §1.3 sextuple off the SAME width-generic core as
    /// `ResidentSweepEnum` (see `enum_kernel.rs` for the laws).
    #[allow(clippy::too_many_arguments)]
    fn enumerate_prefilter(
        &self,
        condition_bits: Vec<u32>,
        k: usize,
        conflict_pairs: Vec<(u32, u32)>,
        min_hits: u64,
        start_rank: u64,
        max_enumerated: u64,
        max_work_cells: u64,
    ) -> PyResult<(u64, u64, u64, bool, u64, Vec<u64>)> {
        enumerate_prefilter_core::<u64>(
            &self.inner.planes,
            &condition_bits,
            k,
            &conflict_pairs,
            min_hits,
            start_rank,
            max_enumerated,
            max_work_cells,
        )
        .map_err(map_enum_err)
    }
    /// Fused per-mask sim→metrics fold over a chunk of kept masks (design
    /// §1.3): walks each mask in-crate via the SAME width-generic
    /// `simulate_one_core` the trade-sim pyclasses use (trades NEVER cross
    /// the FFI), folds the §2.3 pipeline, and returns
    /// `(SWEEP_CORE_ABI_VERSION, scans_run, ints_le ×15-stride,
    /// floats_le ×9-stride, flags)` — LE byte columns in INPUT mask order.
    /// Raises `ValueError` at the 2^28 whole-chunk work ceiling (message
    /// exactly `fold_work_ceiling`) or on an invalid SL/TP distance, and
    /// `MemoryError` on a failed buffer reservation.
    #[allow(clippy::too_many_arguments)]
    fn fold_block(
        &self,
        py: Python<'_>,
        masks: Vec<u64>,
        is_buy: bool,
        sl_paisa: Option<i64>,
        tp_paisa: Option<i64>,
        sl_next_open: bool,
        tp_next_open: bool,
        tp_first: bool,
        start_equity_paisa: i64,
    ) -> PyResult<(u32, u64, Py<PyBytes>, Py<PyBytes>, Py<PyBytes>)> {
        let policy = policy_from_flags(sl_next_open, tp_next_open, tp_first);
        let (scans_run, ints, floats, flags) = fold_block_core(
            &self.inner.columns,
            &self.inner.bar_bits,
            "ResidentSweepCore",
            &masks,
            is_buy,
            sl_paisa,
            tp_paisa,
            policy,
            start_equity_paisa,
            self.inner.n_bars(),
        )
        .map_err(map_enum_err)?;
        let ints_le = ints_to_le(&ints).map_err(map_enum_err)?;
        let floats_le = floats_to_le(&floats).map_err(map_enum_err)?;
        Ok((
            SWEEP_CORE_ABI_VERSION,
            scans_run,
            PyBytes::new(py, &ints_le).into(),
            PyBytes::new(py, &floats_le).into(),
            PyBytes::new(py, &flags).into(),
        ))
    }
}

/// Resident u128-width unified sweep-core substrate — the wide twin of
/// [`ResidentSweepCore`] for condition bits / bar values in `[64, 128)`.
#[pyclass(frozen, module = "fno_math")]
pub struct ResidentSweepCoreU128 {
    inner: SweepCoreInner<u128>,
}

#[pymethods]
impl ResidentSweepCoreU128 {
    /// Build the resident 7-column substrate (u128 width).
    #[new]
    fn new(
        open_paisa: Vec<i64>,
        high_paisa: Vec<i64>,
        low_paisa: Vec<i64>,
        close_paisa: Vec<i64>,
        is_forced: Vec<bool>,
        session_idx: Vec<i64>,
        bar_bits: Vec<u128>,
    ) -> PyResult<Self> {
        let inner = SweepCoreInner::build(
            "ResidentSweepCoreU128",
            open_paisa,
            high_paisa,
            low_paisa,
            close_paisa,
            is_forced,
            session_idx,
            bar_bits,
        )
        .map_err(map_enum_err)?;
        Ok(Self { inner })
    }

    /// Number of resident bars — the seam's `bars_length_mismatch` probe.
    fn n_bars(&self) -> usize {
        self.inner.n_bars()
    }

    /// Ranged fused enumeration + prune + min-hits — the u128
    /// monomorphisation of the SAME width-generic core.
    #[allow(clippy::too_many_arguments)]
    fn enumerate_prefilter(
        &self,
        condition_bits: Vec<u32>,
        k: usize,
        conflict_pairs: Vec<(u32, u32)>,
        min_hits: u64,
        start_rank: u64,
        max_enumerated: u64,
        max_work_cells: u64,
    ) -> PyResult<(u64, u64, u64, bool, u64, Vec<u128>)> {
        enumerate_prefilter_core::<u128>(
            &self.inner.planes,
            &condition_bits,
            k,
            &conflict_pairs,
            min_hits,
            start_rank,
            max_enumerated,
            max_work_cells,
        )
        .map_err(map_enum_err)
    }
    /// Fused per-mask sim→metrics fold over a chunk of kept masks (design
    /// §1.3): walks each mask in-crate via the SAME width-generic
    /// `simulate_one_core` the trade-sim pyclasses use (trades NEVER cross
    /// the FFI), folds the §2.3 pipeline, and returns
    /// `(SWEEP_CORE_ABI_VERSION, scans_run, ints_le ×15-stride,
    /// floats_le ×9-stride, flags)` — LE byte columns in INPUT mask order.
    /// Raises `ValueError` at the 2^28 whole-chunk work ceiling (message
    /// exactly `fold_work_ceiling`) or on an invalid SL/TP distance, and
    /// `MemoryError` on a failed buffer reservation.
    #[allow(clippy::too_many_arguments)]
    fn fold_block(
        &self,
        py: Python<'_>,
        masks: Vec<u128>,
        is_buy: bool,
        sl_paisa: Option<i64>,
        tp_paisa: Option<i64>,
        sl_next_open: bool,
        tp_next_open: bool,
        tp_first: bool,
        start_equity_paisa: i64,
    ) -> PyResult<(u32, u64, Py<PyBytes>, Py<PyBytes>, Py<PyBytes>)> {
        let policy = policy_from_flags(sl_next_open, tp_next_open, tp_first);
        let (scans_run, ints, floats, flags) = fold_block_core(
            &self.inner.columns,
            &self.inner.bar_bits,
            "ResidentSweepCoreU128",
            &masks,
            is_buy,
            sl_paisa,
            tp_paisa,
            policy,
            start_equity_paisa,
            self.inner.n_bars(),
        )
        .map_err(map_enum_err)?;
        let ints_le = ints_to_le(&ints).map_err(map_enum_err)?;
        let floats_le = floats_to_le(&floats).map_err(map_enum_err)?;
        Ok((
            SWEEP_CORE_ABI_VERSION,
            scans_run,
            PyBytes::new(py, &ints_le).into(),
            PyBytes::new(py, &floats_le).into(),
            PyBytes::new(py, &flags).into(),
        ))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;

    fn cols(n: usize) -> (Vec<i64>, Vec<i64>, Vec<i64>, Vec<i64>, Vec<bool>, Vec<i64>) {
        (
            vec![100; n],
            vec![110; n],
            vec![90; n],
            vec![105; n],
            vec![false; n],
            vec![1; n],
        )
    }

    #[test]
    fn inner_build_validates_column_lengths_like_resident_sweep_sim() {
        let (open, high, low, close, forced, session) = cols(3);
        // A short bar_bits vector fails the SAME build_columns law with the
        // core's own label.
        let err = SweepCoreInner::<u64>::build(
            "ResidentSweepCore",
            open,
            high,
            low,
            close,
            forced,
            session,
            vec![0b1; 2],
        )
        .err()
        .expect("short bar_bits must fail");
        assert!(
            err.contains("ResidentSweepCore: column length mismatch"),
            "{err}"
        );
        assert!(err.contains("bar_bits=2"), "{err}");
    }

    #[test]
    fn inner_build_reports_n_bars_and_counts_hits() {
        let (open, high, low, close, forced, session) = cols(4);
        let inner = SweepCoreInner::<u64>::build(
            "ResidentSweepCore",
            open,
            high,
            low,
            close,
            forced,
            session,
            vec![0b11, 0b01, 0b11, 0b00],
        )
        .expect("valid columns");
        assert_eq!(inner.n_bars(), 4);
        // The moved core serves the same fused enumeration off the planes.
        let (enumerated, pruned, next, exhausted, hits, kept) = enumerate_prefilter_core::<u64>(
            &inner.planes,
            &[0, 1],
            2,
            &[],
            2,
            0,
            u64::MAX,
            u64::MAX,
        )
        .expect("valid");
        assert_eq!(
            (enumerated, pruned, next, exhausted, hits),
            (1, 0, 1, true, 1)
        );
        assert_eq!(kept, vec![0b11]);
    }

    #[test]
    fn inner_build_rejects_out_of_envelope_prices() {
        // The R2-RC-F1 price-boundary scan rides build_columns verbatim.
        let (mut open, high, low, close, forced, session) = cols(2);
        open[0] = i64::MAX;
        let err = SweepCoreInner::<u64>::build(
            "ResidentSweepCore",
            open,
            high,
            low,
            close,
            forced,
            session,
            vec![0, 0],
        )
        .err()
        .expect("out-of-envelope price must fail");
        assert!(err.contains("open"), "{err}");
    }

    // ── R3: the fold — frozen corpus vectors (the independent-oracle arm) ──

    /// Corpus trades `(pnl, mfe, mae, holding, exit_idx)` → walker-shaped
    /// `TradeRecord`s (entry_idx synthesized 0 — the fold never reads it).
    fn corpus_trades(raw: &[(i64, i64, i64, u64, u64)]) -> Vec<TradeRecord> {
        raw.iter()
            .map(|&(pnl, mfe, mae, holding, exit_idx)| (pnl, mfe, mae, holding, 0_u64, exit_idx))
            .collect()
    }

    #[test]
    fn fold_matches_every_frozen_corpus_vector_bit_for_bit() {
        // The design §1(a) hard gate: expected bytes are FROZEN literals
        // minted by CPython 3.12 — the Pébay-m3 / Neumaier-guard mutant
        // classes die here, never against a shared helper's output.
        let mut scratch = DrawdownScratch::default();
        for vec in crate::sweep_core_vectors::FOLD_VECTORS {
            let trades = corpus_trades(vec.trades);
            let (ints, floats, flags, scan_ran) =
                fold_trades_core(&trades, vec.n_bars, vec.start_equity_paisa, &mut scratch)
                    .expect("corpus vectors never exhaust memory");
            assert_eq!(ints, vec.expected_ints, "{} ints", vec.name);
            let got_bits: Vec<u64> = floats.iter().map(|f| f.to_bits()).collect();
            assert_eq!(got_bits, vec.expected_float_bits, "{} floats", vec.name);
            assert_eq!(flags, vec.expected_flags, "{} flags", vec.name);
            // The scan runs iff the row ends plain (the scans_run law).
            assert_eq!(scan_ran, vec.expected_flags == 0, "{} scan", vec.name);
        }
    }

    #[test]
    fn helper_vectors_match_cpython_bits() {
        // Neumaier (seed + guarded final add) == the frozen builtins.sum
        // bits; Pébay moments + the two-branch lerp likewise — on the
        // streams trades cannot reach (−0.0 ties, denormals, the
        // [1e16, 1, −1e16] compensation classic).
        for vec in crate::sweep_core_vectors::HELPER_VECTORS {
            let values: Vec<f64> = vec.value_bits.iter().map(|&b| f64::from_bits(b)).collect();
            let total = neumaier_sum(values.iter().copied());
            assert_eq!(total.to_bits(), vec.neumaier_bits, "{} neumaier", vec.name);
            let (mean, m2, m3, m4) = accumulate_moments_fold(&values);
            assert_eq!(
                [mean.to_bits(), m2.to_bits(), m3.to_bits(), m4.to_bits()],
                vec.moments_bits,
                "{} moments",
                vec.name
            );
            if !values.is_empty() {
                let mut ordered = values.clone();
                ordered.sort_by(|a, b| a.partial_cmp(b).unwrap_or(core::cmp::Ordering::Equal));
                assert_eq!(
                    percentile_lerp(&ordered, 95.0).to_bits(),
                    vec.p95_bits,
                    "{} p95",
                    vec.name
                );
                assert_eq!(
                    percentile_lerp(&ordered, 5.0).to_bits(),
                    vec.p5_bits,
                    "{} p5",
                    vec.name
                );
            }
        }
    }

    #[test]
    fn neumaier_classic_compensation_is_not_a_naive_fold() {
        // [1e16, 1.0, −1e16] reads 1.0 compensated; a naive left fold reads
        // 0.0 — the anti-vacuity tooth for the whole Neumaier lane.
        let total = neumaier_sum([1e16, 1.0, -1e16].into_iter());
        assert_eq!(total, 1.0);
    }

    #[test]
    fn neumaier_guarded_final_add_keeps_overflowed_sum_inf_not_nan() {
        // f_result overflows to +inf and the compensation term goes −inf
        // (non-finite): the GUARDED final add must SKIP it (CPython
        // gh-100425 — `if (c && isfinite(c))`), keeping inf; the
        // unconditional-add mutant produces inf + −inf = NaN and dies here.
        let total = neumaier_sum([f64::MAX, f64::MAX].into_iter());
        assert!(total.is_infinite() && total > 0.0, "got {total}");
    }

    #[test]
    fn neumaier_int_start_seeds_denormal_first_exactly() {
        // Seed f_result = x₀ with a denormal first element — bit-exact.
        let d = f64::from_bits(1); // 5e-324
        assert_eq!(neumaier_sum([d].into_iter()).to_bits(), d.to_bits());
        assert_eq!(
            neumaier_sum(core::iter::empty()).to_bits(),
            0.0_f64.to_bits()
        );
    }

    #[test]
    fn downside_filter_is_strictly_negative() {
        // r < 0.0: +0.0 and −0.0 returns are EXCLUDED (token-identical to
        // the Python filter); only genuine losses contribute squares.
        assert_eq!(downside_accumulate(&[0.5, 0.0, -0.0, 1.0]), 0.0);
        let got = downside_accumulate(&[-0.5, 0.25, -0.25]);
        // squares in trade order: 0.25, 0.0625 — Neumaier-summed.
        assert_eq!(
            got.to_bits(),
            neumaier_sum([0.25, 0.0625].into_iter()).to_bits()
        );
    }

    #[test]
    fn percentile_lerp_branches() {
        // (a) single-element early return; (b) integral-rank exact index;
        // (c) the lerp.
        assert_eq!(percentile_lerp(&[7.5], 95.0), 7.5);
        let vals = [1.0, 2.0, 3.0]; // rank(50) = 1.0 exactly → branch (b)
        assert_eq!(percentile_lerp(&vals, 50.0), 2.0);
        let pair = [3.0, 5.0]; // rank(95) = 0.95 → 3*(0.05) + 5*(0.95)
        let rank = 0.95_f64;
        let frac = rank - rank.floor();
        assert_eq!(
            percentile_lerp(&pair, 95.0).to_bits(),
            (3.0 * (1.0 - frac) + 5.0 * frac).to_bits()
        );
        // −0.0/+0.0 tie endpoints survive the lerp bit-exactly.
        assert_eq!(
            percentile_lerp(&[-0.0], 5.0).to_bits(),
            (-0.0_f64).to_bits()
        );
    }

    #[test]
    fn fold_guard_finite_flags_every_non_finite() {
        assert!(fold_guard_finite(&[0.0, -1.5, 1e300]));
        assert!(!fold_guard_finite(&[0.0, f64::NAN]));
        assert!(!fold_guard_finite(&[f64::INFINITY]));
        assert!(!fold_guard_finite(&[f64::NEG_INFINITY, 1.0]));
    }

    // ── fold_block_core: ceiling, scans law, flags, order ────────────────

    /// A tiny 4-bar window where mask 0b1 trades (bar bits fire on bars
    /// 0 and 2) and mask 0b100 never fires (zero-trade).
    fn fold_inner() -> SweepCoreInner<u64> {
        let (open, high, low, close, forced, session) = cols(4);
        SweepCoreInner::<u64>::build(
            "ResidentSweepCore",
            open,
            high,
            low,
            close,
            forced,
            session,
            vec![0b1, 0b0, 0b1, 0b0],
        )
        .expect("valid columns")
    }

    fn run_fold(inner: &SweepCoreInner<u64>, masks: &[u64]) -> (u64, Vec<i64>, Vec<f64>, Vec<u8>) {
        fold_block_core(
            &inner.columns,
            &inner.bar_bits,
            "ResidentSweepCore",
            masks,
            true,
            None,
            None,
            policy_from_flags(false, false, false),
            100_000,
            inner.n_bars(),
        )
        .expect("in ceiling")
    }

    #[test]
    fn fold_block_scans_run_equals_plain_rows_and_zero_trade_rows_are_zeroed() {
        let inner = fold_inner();
        // 0b1 fires (trades); 0b100 never fires (zero-trade).
        let (scans_run, ints, floats, flags) = run_fold(&inner, &[0b1, 0b100]);
        assert_eq!(flags, vec![0, FLAG_ZERO_TRADE]);
        assert_eq!(scans_run, 1); // ONE plain row — the short-circuit law
        assert_eq!(ints.len(), 2 * FOLD_INT_STRIDE);
        assert_eq!(floats.len(), 2 * FOLD_FLOAT_STRIDE);
        // The zero-trade row is fully zeroed.
        assert!(ints[FOLD_INT_STRIDE..].iter().all(|&v| v == 0));
        assert!(
            floats[FOLD_FLOAT_STRIDE..]
                .iter()
                .all(|&v| v.to_bits() == 0)
        );
        // The plain row's n_trades is positive.
        assert!(ints[0] >= 1);
    }

    #[test]
    fn fold_block_all_zero_trade_chunk_has_scans_run_zero() {
        let inner = fold_inner();
        let (scans_run, _ints, _floats, flags) = run_fold(&inner, &[0b100, 0b1000, 0b10000]);
        assert_eq!(scans_run, 0);
        assert_eq!(flags, vec![FLAG_ZERO_TRADE; 3]);
    }

    #[test]
    fn fold_block_rows_are_in_input_mask_order() {
        let inner = fold_inner();
        let (_s, a_ints, ..) = run_fold(&inner, &[0b1, 0b100]);
        let (_s, b_ints, ..) = run_fold(&inner, &[0b100, 0b1]);
        // Row 0 of a == row 1 of b (the same mask's row moved with its slot).
        assert_eq!(a_ints[..FOLD_INT_STRIDE], b_ints[FOLD_INT_STRIDE..]);
    }

    #[test]
    fn fold_block_work_ceiling_refuses_whole_chunk() {
        let inner = fold_inner(); // 4 bars ⇒ ceiling at > 2^28 cells
        let masks = vec![0b1_u64; ((FOLD_WORK_CEILING_CELLS / 4) + 1) as usize];
        let err = fold_block_core(
            &inner.columns,
            &inner.bar_bits,
            "ResidentSweepCore",
            &masks,
            true,
            None,
            None,
            policy_from_flags(false, false, false),
            100_000,
            inner.n_bars(),
        )
        .err()
        .expect("over the ceiling");
        assert_eq!(err, FOLD_WORK_CEILING_ERR);
        // Exactly AT the ceiling is legal (strict `>`).
        let at = vec![0b10000_u64; (FOLD_WORK_CEILING_CELLS / 4) as usize];
        assert!(
            fold_block_core(
                &inner.columns,
                &inner.bar_bits,
                "ResidentSweepCore",
                &at,
                true,
                None,
                None,
                policy_from_flags(false, false, false),
                100_000,
                inner.n_bars(),
            )
            .is_ok()
        );
    }

    #[test]
    fn fold_block_validates_sl_tp_like_simulate_masks() {
        let inner = fold_inner();
        let err = fold_block_core(
            &inner.columns,
            &inner.bar_bits,
            "ResidentSweepCore",
            &[0b1],
            true,
            Some(0),
            None,
            policy_from_flags(false, false, false),
            100_000,
            inner.n_bars(),
        )
        .err()
        .expect("zero SL distance must fail");
        assert!(err.contains("sl_paisa"), "{err}");
    }

    #[test]
    fn fold_trades_envelope_and_narrow_duals() {
        let mut scratch = DrawdownScratch::default();
        let e = 1_i64 << 52;
        // start + Σ|pnl| == 2^52 exactly: PLAIN (strict `>`).
        let (_, _, flags, scan) =
            fold_trades_core(&[(e - 1, 0, 0, 1, 0, 0)], 1, 1, &mut scratch).expect("ok");
        assert_eq!((flags, scan), (0, true));
        // One paisa more: python_required, fully zeroed, NO scan.
        let (ints, floats, flags, scan) =
            fold_trades_core(&[(e - 1, 0, 0, 1, 0, 0)], 1, 2, &mut scratch).expect("ok");
        assert_eq!((flags, scan), (FLAG_PYTHON_REQUIRED, false));
        assert!(ints.iter().all(|&v| v == 0));
        assert!(floats.iter().all(|&v| v.to_bits() == 0));
        // i64 narrow failure (sum_mfe past i64::MAX) ⇒ python_required.
        let big = 1_i64 << 62;
        let trades = [
            (1, big, 0, 1, 0, 0),
            (1, big, 0, 1, 0, 0),
            (1, big, 0, 1, 0, 0),
        ];
        let (_, _, flags, scan) = fold_trades_core(&trades, 1, 1_000, &mut scratch).expect("ok");
        assert_eq!((flags, scan), (FLAG_PYTHON_REQUIRED, false));
    }

    #[test]
    fn fold_trades_largest_loss_stays_signed() {
        let mut scratch = DrawdownScratch::default();
        let trades = [(-750, 0, 750, 1, 0, 0), (300, 300, 0, 1, 0, 1)];
        let (ints, _, flags, _) = fold_trades_core(&trades, 2, 100_000, &mut scratch).expect("ok");
        assert_eq!(flags, 0);
        assert_eq!(ints[10], -750); // largest_loss — SIGNED, most negative
        assert_eq!(ints[1], -450); // net_pnl
        assert_eq!(ints[3], 750); // gross_loss_abs — magnitude
    }

    // ── proptests: chunk invariance + flag routing ───────────────────────

    proptest! {
        #[test]
        fn prop_fold_block_is_chunk_invariant(
            bar_bits in proptest::collection::vec(0u64..8, 1..24),
            masks in proptest::collection::vec(0u64..8, 1..12),
            split in 0usize..12,
            is_buy in proptest::bool::ANY,
        ) {
            let n = bar_bits.len();
            let inner = SweepCoreInner::<u64>::build(
                "ResidentSweepCore",
                vec![10_000; n],
                vec![10_500; n],
                vec![9_500; n],
                vec![10_200; n],
                vec![false; n],
                vec![1; n],
                bar_bits,
            )
            .expect("valid");
            let policy = policy_from_flags(false, false, false);
            let mono = fold_block_core(
                &inner.columns, &inner.bar_bits, "ResidentSweepCore", &masks,
                is_buy, None, None, policy, 1_000_000, n,
            )
            .expect("in ceiling");
            let cut = split % masks.len().max(1);
            let (head, tail) = masks.split_at(cut.min(masks.len()));
            let mut scans = 0_u64;
            let mut ints: Vec<i64> = Vec::new();
            let mut floats: Vec<f64> = Vec::new();
            let mut flags: Vec<u8> = Vec::new();
            for chunk in [head, tail] {
                let (s, i, f, fl) = fold_block_core(
                    &inner.columns, &inner.bar_bits, "ResidentSweepCore", chunk,
                    is_buy, None, None, policy, 1_000_000, n,
                )
                .expect("in ceiling");
                scans += s;
                ints.extend(i);
                floats.extend(f);
                flags.extend(fl);
            }
            // Chunking NEVER changes results — rows, bytes and the scans
            // tally concatenate exactly (the dual-knob invariance law).
            prop_assert_eq!(scans, mono.0);
            prop_assert_eq!(ints, mono.1);
            let mono_bits: Vec<u64> = mono.2.iter().map(|f| f.to_bits()).collect();
            let got_bits: Vec<u64> = floats.iter().map(|f| f.to_bits()).collect();
            prop_assert_eq!(got_bits, mono_bits);
            prop_assert_eq!(flags, mono.3);
        }

        #[test]
        fn prop_scans_run_equals_plain_rows(
            bar_bits in proptest::collection::vec(0u64..4, 1..16),
            masks in proptest::collection::vec(0u64..8, 0..10),
        ) {
            let n = bar_bits.len();
            let inner = SweepCoreInner::<u64>::build(
                "ResidentSweepCore",
                vec![10_000; n],
                vec![10_500; n],
                vec![9_500; n],
                vec![10_200; n],
                vec![false; n],
                vec![1; n],
                bar_bits,
            )
            .expect("valid");
            let (scans_run, _ints, _floats, flags) = fold_block_core(
                &inner.columns, &inner.bar_bits, "ResidentSweepCore", &masks,
                true, None, None, policy_from_flags(false, false, false), 1_000_000, n,
            )
            .expect("in ceiling");
            let plain = flags.iter().filter(|&&f| f == 0).count() as u64;
            prop_assert_eq!(scans_run, plain);
        }
    }
}
