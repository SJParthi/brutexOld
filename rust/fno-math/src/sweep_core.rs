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

use crate::trade_sim::{SweepColumns, build_columns};

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
    #[allow(dead_code)] // read by fold_block (R3, the next commit of this slice)
    pub(crate) columns: SweepColumns,
    /// The width-typed per-bar condition bitsets (the walk's entry stream).
    #[allow(dead_code)] // read by fold_block (R3, the next commit of this slice)
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
}

#[cfg(test)]
mod tests {
    use super::*;

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
}
