//! Sweep-enumeration kernel — ranged fused enumeration + conflict prune + Stage-1B min-hits.
//!
//! DEC-SWEEP-ENUM-RUST-001 (MIGRATION.md #1738; umbrella
//! DEC-RUST-FIRST-MANDATE-001), reworked to the design-of-record
//! `docs/design/sweep_core/DESIGN_FINAL_RUST_SWEEP_CORE.md` §1.3 `enumerate_prefilter`
//! surface (branch `claude/rust-sweep-core` @ a3c61e9b). The Rust port of the
//! sweep's mask-source segment: `brutex.engine.combo_mask.iter_masks_k_of_bits`
//! (the production enumerator) FUSED with the per-mask
//! `brutex.vocabulary.has_conflict` prune AND the Stage-1B min-hits supermask
//! count (`count_supermasks` semantics) over a RESIDENT condition-major
//! transposed-bitplane substrate, served in RANGES via combinatorial-number-
//! system (CNS) LEX-RANK unranking — stateless, no cursor, no files, no
//! statics (the design §3 statelessness law).
//!
//! ## The order law (load-bearing — Golden Rule #4)
//!
//! Kept masks are emitted in **`itertools.combinations` lexicographic order on
//! ascending bit-position tuples over `sorted(set(bits))`** — POSITION-LEX,
//! NOT numeric-ascending mask order (== colex == the STANDARD CNS order). The
//! two differ from n = 4: position-lex for n=4, k=2 yields masks
//! 3, 5, **9, 6**, 10, 12 while ascending-numeric yields 3, 5, **6, 9**, 10, 12.
//! The unranking below is therefore the **LEX-RANK variant** (rank within
//! `combinations(sorted_bits, k)` lex order — the design's Slice-1 substrate
//! item 1 MANDATE), and the advance is a literal transcription of the CPython
//! `itertools.combinations` odometer. Enumeration order feeds the sweep's
//! chunk boundaries, `SweepComboError` attribution, stable-sort tie-breaks and
//! the store append order — a set-equal but order-drifted list produces
//! DIFFERENT durable bytes.
//!
//! ## The substrate (design Slice-1 item 2 — condition-major transposed bitplanes)
//!
//! The resident pyclasses hold ONE per-condition bitmap over bars (plane `b`,
//! packed u64 words; bit `i%64` of word `i/64` = bar `i` fires condition `b`),
//! built once per tuple from `bar_bits`. The fused Stage-1B count for a
//! combination is then popcount(AND of its k planes) — arithmetic-identical to
//! counting bars with `(bar & mask) == mask` (the SAME supermask predicate as
//! `count_one` / `count_supermasks`, pinned by the strict->= bit-parity gate).
//! The substrate is invisible outside the kernel; it exists so the later
//! prefix-AND DFS pruning lever retrofits with no substrate migration.
//!
//! ## The MIN-HITS SHORT-CIRCUIT LAW (design §1.3, round-6 — MANDATED)
//!
//! When `min_hits == 0` the per-mask O(n_bars) supermask-hits count MUST NOT
//! run — kept = conflict-free directly, exactly today's spine (`if
//! cfg.min_hits > 0` guards the WHOLE Stage-1B count). Enforcement is
//! BYTE-OBSERVABLE, in-band: `hits_counted_in_range` returns the number of
//! masks whose hits count actually EXECUTED — a correct call has
//! `hits_counted == 0` when `min_hits == 0` and `hits_counted ==
//! enumerated_in_range − conflict_pruned_in_range` when `min_hits > 0`. A
//! mutant deleting the guard is data-identical but flips `hits_counted` and
//! dies deterministically in the in-crate units + the Python seam pin.
//!
//! ## Work caps + guaranteed progress (design §1.3/§3)
//!
//! Dual bounds per call: `max_enumerated` (range bound, floored at 1 — the
//! repo's `max(1, …)` idiom) and `max_work_cells` (enumerated × n_bars,
//! saturating — the GIL/liveness knob; the crate holds the GIL for the full
//! call, so the cap IS the liveness mechanism). The CHARGING RULE is
//! deliberately min-hits-BLIND (over-charging only SHORTENS GIL holds; results
//! are cap-invariant). Caps are checked AFTER each mask's work, so every
//! non-exhausted call advances `next_rank > start_rank` — a zero-progress
//! driver loop is structurally impossible. `exhausted` is computed IN-CORE as
//! `next_rank == total_count`; the driver loops on it, never on heuristics.
//!
//! ## Conflict pairs are DATA, never re-minted here
//!
//! The pair table crosses the FFI per call (`sorted(CONFLICT_PAIRS)` from
//! `brutex.vocabulary.conflicts` — the single source of truth). The prune test
//! `mask & pair_mask == pair_mask` is arithmetic-identical to Python's
//! `(mask >> a) & 1 and (mask >> b) & 1`. A pairs-to-`bits` prefilter was
//! DELIBERATELY NOT implemented: dropping a pair that touches a bit absent
//! from `bits` is output-UNOBSERVABLE by construction — every mutant of such
//! a filter is provably equivalent, an unkillable-survivor factory under the
//! zero-missed cargo-mutants gate.
//!
//! ## Failure mode — `Result`, NEVER a panic
//!
//! Under the crate's `overflow-checks = true` + `panic = "abort"` release
//! profile an unchecked overflow / OOB index / failed alloc would SIGABRT the
//! whole host interpreter. Domain validation runs UP FRONT (design §1.3):
//! (a) `k <= n_distinct` with `k == 0` LEGAL; (b) EVERY condition-bit index
//! AND conflict-pair index `< W::BITS` — before any shift; (c) `start_rank <=
//! total_count` (`== total` legal, returns exhausted-empty; `>` is
//! `rank_out_of_domain`). ALL binomial arithmetic is u128 checked (Pascal
//! table; C(128, 64) ≈ 2.4e37 fits u128 exactly), and a total count exceeding
//! `u64::MAX` returns a clean Err — never an overflow SIGABRT. Buffers are
//! `try_reserve` (failure → a clean Python `MemoryError`, the global-stop
//! class). This module additionally carries the design R4 module-scoped
//! indexing deny — all new code uses checked accessors.
//!
//! ## Complexity (honest envelope — Golden Rule #10)
//!
//! One call is `O(range · (k + n_pairs))` mask work plus, iff `min_hits > 0`,
//! `O(range_conflict_free · k · n_bars/64)` plane-AND words; substrate
//! construction is a one-off `O(n_bars · set_bits)` transpose per resident
//! object (per tuple). Same work class as the Python loop it replaces — a
//! constant-factor win only; claiming an O-class change is FORBIDDEN. GIL
//! held, serial (the recorded crate stance — no `allow_threads`; the work
//! caps bound the hold).

#![cfg_attr(not(test), deny(clippy::indexing_slicing))]

use pyo3::exceptions::{PyMemoryError, PyValueError};
use pyo3::prelude::*;

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
fn advance_odometer(idx: &mut [usize], n: usize, k: usize) -> bool {
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

/// Pure core of the ranged `enumerate_prefilter` FFI (design §1.3) —
/// `Result<_, String>` (NO `PyErr`) so the no-libpython `cargo test` link
/// exercises every branch. Returns the design sextuple
/// `(enumerated_in_range, conflict_pruned_in_range, next_rank, exhausted,
/// hits_counted_in_range, kept_masks)` with
/// `kept = conflict-free AND (min_hits == 0 OR supermask_hits >= min_hits)`
/// (STRICT `>=` boundary) in `itertools.combinations` lexicographic order.
#[allow(clippy::too_many_arguments)]
pub(crate) fn enumerate_prefilter_impl<W: EnumWord>(
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
fn map_enum_err(msg: String) -> PyErr {
    if msg.starts_with(ALLOC_ERR_PREFIX) {
        PyMemoryError::new_err(msg)
    } else {
        PyValueError::new_err(msg)
    }
}

/// Resident u64-width sweep-enumeration substrate (design §1.3 Slice-1 scope:
/// `bar_bits` only — the fold-arm paisa/forced/session columns land with the
/// Slice-2 `ResidentSweepCore`). Frozen: zero cross-call state.
#[pyclass(frozen, module = "fno_math")]
pub struct ResidentSweepEnum {
    planes: EnumPlanes,
}

#[pymethods]
impl ResidentSweepEnum {
    /// Build the resident transposed-bitplane substrate from the tuple's
    /// `bar_bits` window (u64 width — every bar value < 2^64; PyO3 raises
    /// `OverflowError` on a wider int, which the seam maps to its
    /// `kernel_error` fallback).
    #[new]
    fn new(bar_bits: Vec<u64>) -> PyResult<Self> {
        let planes = EnumPlanes::build::<u64>(&bar_bits).map_err(map_enum_err)?;
        Ok(Self { planes })
    }

    /// Number of resident bars — the seam's `bars_length_mismatch` probe
    /// (design §1.3 F27: asserted against the Python-side window length
    /// immediately after construction, before ANY call).
    fn n_bars(&self) -> usize {
        self.planes.n_bars()
    }

    /// Ranged fused enumeration + conflict prune + Stage-1B min-hits count —
    /// the design §1.3 sextuple; see the module docstring for the laws
    /// (order, min-hits short-circuit, charging rule, exhaustion contract,
    /// guaranteed progress, strict `>=`).
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
        enumerate_prefilter_impl::<u64>(
            &self.planes,
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

/// Resident u128-width sweep-enumeration substrate — the wide twin of
/// [`ResidentSweepEnum`] for condition bits in `[64, 128)` (legal per
/// `MAX_VOCAB_BITS = 128`; production-unreachable today —
/// `IMPLEMENTED_MASK < 2^60`).
#[pyclass(frozen, module = "fno_math")]
pub struct ResidentSweepEnumU128 {
    planes: EnumPlanes,
}

#[pymethods]
impl ResidentSweepEnumU128 {
    /// Build the resident transposed-bitplane substrate (u128 width).
    #[new]
    fn new(bar_bits: Vec<u128>) -> PyResult<Self> {
        let planes = EnumPlanes::build::<u128>(&bar_bits).map_err(map_enum_err)?;
        Ok(Self { planes })
    }

    /// Number of resident bars — the seam's `bars_length_mismatch` probe.
    fn n_bars(&self) -> usize {
        self.planes.n_bars()
    }

    /// Ranged fused enumeration + prune + min-hits — the u128 monomorphisation
    /// of the SAME width-generic core as [`ResidentSweepEnum`].
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
        enumerate_prefilter_impl::<u128>(
            &self.planes,
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

    /// Naive in-crate reference — DELIBERATELY different mechanics from the
    /// kernel: recursive combination generation (not the odometer/unranking),
    /// the literal shift-based `has_conflict` transcription (not pair masks),
    /// and a bar-major `(bar & mask) == mask` supermask count over the RAW
    /// bar_bits (not the transposed planes). The differential oracle for
    /// order, set, counters, and the fused min-hits verdicts.
    fn reference(
        bar_bits: &[u128],
        bits: &[u32],
        k: usize,
        pairs: &[(u32, u32)],
        min_hits: u64,
    ) -> (Vec<u128>, u64, u64, u64) {
        let mut unique: Vec<u32> = bits.to_vec();
        unique.sort_unstable();
        unique.dedup();
        let mut combos: Vec<Vec<u32>> = Vec::new();
        fn recurse(
            unique: &[u32],
            k: usize,
            start: usize,
            cur: &mut Vec<u32>,
            out: &mut Vec<Vec<u32>>,
        ) {
            if cur.len() == k {
                out.push(cur.clone());
                return;
            }
            for i in start..unique.len() {
                cur.push(unique[i]);
                recurse(unique, k, i + 1, cur, out);
                cur.pop();
            }
        }
        recurse(&unique, k, 0, &mut Vec::new(), &mut combos);
        let mut kept = Vec::new();
        let mut enumerated = 0u64;
        let mut pruned = 0u64;
        let mut hits_counted = 0u64;
        for combo in combos {
            let mut mask: u128 = 0;
            for &p in &combo {
                mask |= 1u128 << p;
            }
            enumerated += 1;
            // Literal `has_conflict`: any((mask >> a) & 1 and (mask >> b) & 1).
            let conflict = pairs
                .iter()
                .any(|&(a, b)| (mask >> a) & 1 == 1 && (mask >> b) & 1 == 1);
            if conflict {
                pruned += 1;
            } else if min_hits == 0 {
                kept.push(mask);
            } else {
                // Bar-major supermask count — count_supermasks semantics.
                let hits = bar_bits.iter().filter(|&&bar| (bar & mask) == mask).count() as u64;
                hits_counted += 1;
                if hits >= min_hits {
                    kept.push(mask);
                }
            }
        }
        (kept, enumerated, pruned, hits_counted)
    }

    /// Monolithic (whole-range) call on a u128 core built from `bar_bits`.
    fn monolithic(
        bar_bits: &[u128],
        bits: &[u32],
        k: usize,
        pairs: &[(u32, u32)],
        min_hits: u64,
    ) -> (u64, u64, u64, bool, u64, Vec<u128>) {
        let planes = EnumPlanes::build::<u128>(bar_bits).expect("planes");
        enumerate_prefilter_impl::<u128>(&planes, bits, k, pairs, min_hits, 0, u64::MAX, u64::MAX)
            .expect("valid")
    }

    /// Empty-window u64 planes (n_bars = 0) for integer-only tests.
    fn no_bars() -> EnumPlanes {
        EnumPlanes::build::<u64>(&[]).expect("planes")
    }

    // ── literal pins ─────────────────────────────────────────────────────

    #[test]
    fn doctest_pin_bits_0_3_5_k2() {
        // iter_masks_k_of_bits([0, 3, 5], 2) == [9, 33, 40] (the Python doctest).
        let (enumerated, pruned, next_rank, exhausted, hits, kept) =
            enumerate_prefilter_impl::<u64>(
                &no_bars(),
                &[0, 3, 5],
                2,
                &[],
                0,
                0,
                u64::MAX,
                u64::MAX,
            )
            .expect("valid");
        assert_eq!(kept, vec![9, 33, 40]);
        assert_eq!(
            (enumerated, pruned, next_rank, exhausted, hits),
            (3, 0, 3, true, 0)
        );
    }

    #[test]
    fn lex_order_witness_is_not_numeric_order() {
        // n=4, k=2 position-lex masks: 3, 5, 9, 6, 10, 12 — 9 BEFORE 6.
        // A Gosper/numeric-ascending/colex-CNS kernel yields 3, 5, 6, 9, 10, 12
        // and MUST fail here (the design's item-1 counterexample).
        let (.., kept) = enumerate_prefilter_impl::<u64>(
            &no_bars(),
            &[0, 1, 2, 3],
            2,
            &[],
            0,
            0,
            u64::MAX,
            u64::MAX,
        )
        .expect("valid");
        assert_eq!(kept, vec![3, 5, 9, 6, 10, 12]);
    }

    #[test]
    fn input_order_and_duplicates_are_normalised() {
        // [5, 0, 3] and [0, 3, 5, 5, 0] both normalise to sorted-unique.
        let a = enumerate_prefilter_impl::<u64>(
            &no_bars(),
            &[5, 0, 3],
            2,
            &[],
            0,
            0,
            u64::MAX,
            u64::MAX,
        )
        .expect("valid");
        let b = enumerate_prefilter_impl::<u64>(
            &no_bars(),
            &[0, 3, 5, 5, 0],
            2,
            &[],
            0,
            0,
            u64::MAX,
            u64::MAX,
        )
        .expect("valid");
        assert_eq!(a, b);
        assert_eq!(a.5, vec![9, 33, 40]);
        // Duplicates collapse: [5, 5, 5] has ONE unique bit.
        let (enumerated, pruned, .., kept) = enumerate_prefilter_impl::<u64>(
            &no_bars(),
            &[5, 5, 5],
            1,
            &[],
            0,
            0,
            u64::MAX,
            u64::MAX,
        )
        .expect("valid");
        assert_eq!(kept, vec![32]);
        assert_eq!((enumerated, pruned), (1, 0));
    }

    #[test]
    fn k_zero_yields_the_single_empty_mask() {
        // Matches Python: [0], enumerated=1, pruned=0 — even with pairs
        // present (mask 0 never conflicts) and even for EMPTY bits.
        let (enumerated, pruned, next_rank, exhausted, hits, kept) =
            enumerate_prefilter_impl::<u64>(
                &no_bars(),
                &[7, 2],
                0,
                &[(2, 7)],
                0,
                0,
                u64::MAX,
                u64::MAX,
            )
            .expect("valid");
        assert_eq!(kept, vec![0]);
        assert_eq!(
            (enumerated, pruned, next_rank, exhausted, hits),
            (1, 0, 1, true, 0)
        );
        let (.., kept) =
            enumerate_prefilter_impl::<u64>(&no_bars(), &[], 0, &[], 0, 0, u64::MAX, u64::MAX)
                .expect("valid");
        assert_eq!(kept, vec![0]);
    }

    #[test]
    fn k_zero_min_hits_counts_every_bar() {
        // (bar & 0) == 0 for every bar ⇒ hits == n_bars; the strict >=
        // boundary keeps at min_hits == n_bars, drops at n_bars + 1; and the
        // count EXECUTED (hits_counted == 1) — the k=0 min-hits arm.
        let planes = EnumPlanes::build::<u64>(&[0b1, 0b10, 0b11, 0, 0b100]).expect("planes");
        let (.., hits, kept) =
            enumerate_prefilter_impl::<u64>(&planes, &[], 0, &[], 5, 0, u64::MAX, u64::MAX)
                .expect("valid");
        assert_eq!((hits, kept), (1, vec![0]));
        let (.., hits, kept) =
            enumerate_prefilter_impl::<u64>(&planes, &[], 0, &[], 6, 0, u64::MAX, u64::MAX)
                .expect("valid");
        assert_eq!((hits, kept), (1, Vec::<u64>::new()));
    }

    #[test]
    fn k_equals_n_unique_single_mask_and_full_prune() {
        let (enumerated, pruned, .., kept) =
            enumerate_prefilter_impl::<u64>(&no_bars(), &[1, 4], 2, &[], 0, 0, u64::MAX, u64::MAX)
                .expect("valid");
        assert_eq!(kept, vec![0b10010]);
        assert_eq!((enumerated, pruned), (1, 0));
        // … and a conflicting pair prunes it to an EMPTY kept list.
        let (enumerated, pruned, .., kept) = enumerate_prefilter_impl::<u64>(
            &no_bars(),
            &[1, 4],
            2,
            &[(1, 4)],
            0,
            0,
            u64::MAX,
            u64::MAX,
        )
        .expect("valid");
        assert!(kept.is_empty());
        assert_eq!((enumerated, pruned), (1, 1));
    }

    #[test]
    fn u128_reach_bits_63_64_127() {
        let planes = EnumPlanes::build::<u128>(&[]).expect("planes");
        let (enumerated, pruned, .., kept) = enumerate_prefilter_impl::<u128>(
            &planes,
            &[63, 64, 127],
            2,
            &[],
            0,
            0,
            u64::MAX,
            u64::MAX,
        )
        .expect("valid");
        assert_eq!(
            kept,
            vec![
                (1u128 << 63) | (1u128 << 64),
                (1u128 << 63) | (1u128 << 127),
                (1u128 << 64) | (1u128 << 127),
            ]
        );
        assert_eq!((enumerated, pruned), (3, 0));
        // A high-limb pair prunes across the 64-bit boundary.
        let (_, pruned, .., kept) = enumerate_prefilter_impl::<u128>(
            &planes,
            &[63, 64, 127],
            2,
            &[(64, 127)],
            0,
            0,
            u64::MAX,
            u64::MAX,
        )
        .expect("valid");
        assert_eq!(
            kept,
            vec![
                (1u128 << 63) | (1u128 << 64),
                (1u128 << 63) | (1u128 << 127)
            ]
        );
        assert_eq!(pruned, 1);
    }

    #[test]
    fn u128_min_hits_counts_on_high_limb_planes() {
        // Bars firing bit 127 twice, bit 64 once — the wide substrate's
        // planes count across the 64-bit boundary.
        let bars: Vec<u128> = vec![1u128 << 127, (1u128 << 127) | (1u128 << 64), 1u128 << 3];
        let planes = EnumPlanes::build::<u128>(&bars).expect("planes");
        let (.., hits, kept) =
            enumerate_prefilter_impl::<u128>(&planes, &[127], 1, &[], 2, 0, u64::MAX, u64::MAX)
                .expect("valid");
        assert_eq!((hits, kept), (1, vec![1u128 << 127]));
        let (.., kept) =
            enumerate_prefilter_impl::<u128>(&planes, &[127], 1, &[], 3, 0, u64::MAX, u64::MAX)
                .expect("valid");
        assert_eq!(kept, Vec::<u128>::new());
    }

    // ── the MIN-HITS SHORT-CIRCUIT LAW (in-band deterministic net) ────────

    #[test]
    fn min_hits_zero_runs_zero_counts() {
        // min_hits == 0 ⇒ hits_counted == 0: the per-mask O(n_bars) count
        // MUST NOT run (the design §1.3 round-6 law; a mutant deleting the
        // guard is data-identical but flips hits_counted and dies HERE).
        let planes = EnumPlanes::build::<u64>(&[0b111; 50]).expect("planes");
        let (enumerated, pruned, _, _, hits, kept) = enumerate_prefilter_impl::<u64>(
            &planes,
            &[0, 1, 2],
            2,
            &[(0, 1)],
            0,
            0,
            u64::MAX,
            u64::MAX,
        )
        .expect("valid");
        assert_eq!(hits, 0);
        assert_eq!((enumerated, pruned), (3, 1));
        assert_eq!(kept.len(), 2);
    }

    #[test]
    fn min_hits_positive_counts_every_conflict_free_mask() {
        // min_hits > 0 ⇒ hits_counted == enumerated − conflict_pruned (the
        // count runs on EVERY conflict-free mask, kept or dropped).
        let planes = EnumPlanes::build::<u64>(&[0b111; 50]).expect("planes");
        let (enumerated, pruned, _, _, hits, _) = enumerate_prefilter_impl::<u64>(
            &planes,
            &[0, 1, 2],
            2,
            &[(0, 1)],
            10,
            0,
            u64::MAX,
            u64::MAX,
        )
        .expect("valid");
        assert_eq!(hits, enumerated - pruned);
        assert_eq!(hits, 2);
    }

    // ── strict >= boundary (Verifier B F15/F18) ──────────────────────────

    #[test]
    fn strict_gte_boundary_keeps_at_min_hits_drops_below() {
        // Mask {0,1} fires on exactly 3 of 5 bars. hits == min_hits ⇒ KEPT;
        // min_hits == hits + 1 ⇒ dropped. Kills the >=→> boundary mutant.
        let bars: Vec<u64> = vec![0b11, 0b11, 0b11, 0b01, 0b00];
        let planes = EnumPlanes::build::<u64>(&bars).expect("planes");
        let (.., kept) =
            enumerate_prefilter_impl::<u64>(&planes, &[0, 1], 2, &[], 3, 0, u64::MAX, u64::MAX)
                .expect("valid");
        assert_eq!(kept, vec![0b11]);
        let (.., kept) =
            enumerate_prefilter_impl::<u64>(&planes, &[0, 1], 2, &[], 4, 0, u64::MAX, u64::MAX)
                .expect("valid");
        assert_eq!(kept, Vec::<u64>::new());
    }

    #[test]
    fn planes_count_matches_bar_major_reference() {
        // Plane-AND popcount ≡ bar-major (bar & mask) == mask counting,
        // including a tail word (n_bars deliberately > 64, not a multiple).
        let bars: Vec<u64> = (0..77u64).map(|i| (i % 8) | ((i % 3) << 3)).collect();
        let planes = EnumPlanes::build::<u64>(&bars).expect("planes");
        for positions in [vec![0u32], vec![0, 1], vec![0, 1, 2], vec![3, 4], vec![5]] {
            let mask: u64 = positions.iter().fold(0, |m, &p| m | (1u64 << p));
            let expected = bars.iter().filter(|&&b| (b & mask) == mask).count() as u64;
            assert_eq!(planes.count_hits(&positions), expected, "{positions:?}");
        }
    }

    // ── ranges: caps, progress, exhaustion, rank domain ──────────────────

    #[test]
    fn ranged_calls_concatenate_to_the_monolithic_result() {
        let bars: Vec<u128> = (0..40u128).map(|i| i % 16).collect();
        let bits = [0u32, 1, 2, 3, 4, 5];
        let pairs = [(0u32, 1u32), (2, 3)];
        for min_hits in [0u64, 1, 3, 100] {
            let mono = monolithic(&bars, &bits, 3, &pairs, min_hits);
            let planes = EnumPlanes::build::<u128>(&bars).expect("planes");
            for max_enumerated in [1u64, 2, 5, 7, u64::MAX] {
                let mut rank = 0u64;
                let mut kept: Vec<u128> = Vec::new();
                let mut enumerated = 0u64;
                let mut pruned = 0u64;
                let mut hits = 0u64;
                loop {
                    let (e, p, next, exhausted, h, kp) = enumerate_prefilter_impl::<u128>(
                        &planes,
                        &bits,
                        3,
                        &pairs,
                        min_hits,
                        rank,
                        max_enumerated,
                        u64::MAX,
                    )
                    .expect("valid");
                    // Guaranteed progress: every non-exhausted call advances.
                    assert!(next > rank || exhausted);
                    // Cap respected (+ the design's return-field assertion).
                    assert!(next - rank <= max_enumerated.max(1));
                    enumerated += e;
                    pruned += p;
                    hits += h;
                    kept.extend(kp);
                    rank = next;
                    if exhausted {
                        break;
                    }
                }
                assert_eq!(
                    (enumerated, pruned, rank, true, hits, kept.clone()),
                    mono.clone(),
                    "min_hits={min_hits} max_enumerated={max_enumerated}"
                );
            }
        }
    }

    #[test]
    fn work_cells_cap_bounds_the_range_with_one_mask_slop() {
        // 10 bars/mask; a 25-cell budget admits 3 masks (cells checked AFTER
        // work: 10, 20, 30 ≥ 25 → stop) — (next − start) × n_bars ≤
        // max_work_cells + n_bars, the design's one-mask-slop inequality.
        let bars: Vec<u64> = vec![0; 10];
        let planes = EnumPlanes::build::<u64>(&bars).expect("planes");
        let (enumerated, _, next, exhausted, ..) = enumerate_prefilter_impl::<u64>(
            &planes,
            &[0, 1, 2, 3, 4, 5],
            2,
            &[],
            0,
            0,
            u64::MAX,
            25,
        )
        .expect("valid");
        assert_eq!(enumerated, 3);
        assert_eq!(next, 3);
        assert!(!exhausted);
        assert!(next * 10 <= 25 + 10);
        // Degenerate cap = 0: exactly ONE mask advances (progress law).
        let (enumerated, _, next, ..) = enumerate_prefilter_impl::<u64>(
            &planes,
            &[0, 1, 2, 3, 4, 5],
            2,
            &[],
            0,
            0,
            u64::MAX,
            0,
        )
        .expect("valid");
        assert_eq!((enumerated, next), (1, 1));
    }

    #[test]
    fn max_enumerated_zero_still_advances_one_mask() {
        // The max(1, …) floor idiom: a degenerate 0 cap enumerates ONE mask.
        let (enumerated, _, next, exhausted, ..) =
            enumerate_prefilter_impl::<u64>(&no_bars(), &[0, 1, 2], 2, &[], 0, 0, 0, u64::MAX)
                .expect("valid");
        assert_eq!((enumerated, next), (1, 1));
        assert!(!exhausted);
    }

    #[test]
    fn mid_range_start_rank_resumes_exactly() {
        // start_rank == 2 over C(4, 2): the lex stream's tail [9, 6, 10, 12].
        let (enumerated, _, next, exhausted, _, kept) = enumerate_prefilter_impl::<u64>(
            &no_bars(),
            &[0, 1, 2, 3],
            2,
            &[],
            0,
            2,
            u64::MAX,
            u64::MAX,
        )
        .expect("valid");
        assert_eq!(kept, vec![9, 6, 10, 12]);
        assert_eq!((enumerated, next), (4, 6));
        assert!(exhausted);
    }

    #[test]
    fn start_rank_at_total_is_exhausted_empty() {
        let out = enumerate_prefilter_impl::<u64>(
            &no_bars(),
            &[0, 1, 2],
            2,
            &[],
            0,
            3,
            u64::MAX,
            u64::MAX,
        )
        .expect("valid");
        assert_eq!(out, (0, 0, 3, true, 0, Vec::<u64>::new()));
    }

    #[test]
    fn start_rank_past_total_is_rank_out_of_domain() {
        let err = enumerate_prefilter_impl::<u64>(
            &no_bars(),
            &[0, 1, 2],
            2,
            &[],
            0,
            4,
            u64::MAX,
            u64::MAX,
        )
        .unwrap_err();
        assert!(err.contains("start_rank 4 out of domain"), "{err}");
    }

    // ── boundary validation (Result, never a panic) ──────────────────────

    #[test]
    fn out_of_width_condition_bit_is_rejected() {
        // bits=[100] on the u64 core: n_distinct = 1 <= 64 passes the COUNT
        // bound; the VALUE bound must refuse BEFORE the 1 << 100 shift
        // (a shift-overflow SIGABRT under overflow-checks; design round-2).
        let err =
            enumerate_prefilter_impl::<u64>(&no_bars(), &[100], 1, &[], 0, 0, u64::MAX, u64::MAX)
                .unwrap_err();
        assert!(
            err.contains("condition bit 100 out of range [0, 64)"),
            "{err}"
        );
        // The u128 core accepts 100 but refuses 128.
        let planes = EnumPlanes::build::<u128>(&[]).expect("planes");
        assert!(
            enumerate_prefilter_impl::<u128>(&planes, &[100], 1, &[], 0, 0, u64::MAX, u64::MAX)
                .is_ok()
        );
        let err =
            enumerate_prefilter_impl::<u128>(&planes, &[128], 1, &[], 0, 0, u64::MAX, u64::MAX)
                .unwrap_err();
        assert!(
            err.contains("condition bit 128 out of range [0, 128)"),
            "{err}"
        );
    }

    #[test]
    fn k_beyond_n_unique_is_rejected() {
        let err =
            enumerate_prefilter_impl::<u64>(&no_bars(), &[0, 1], 3, &[], 0, 0, u64::MAX, u64::MAX)
                .unwrap_err();
        assert!(err.contains("invalid k=3 for 2 unique bits"), "{err}");
        // Duplicates collapse BEFORE the k check (set semantics).
        let err =
            enumerate_prefilter_impl::<u64>(&no_bars(), &[5, 5], 2, &[], 0, 0, u64::MAX, u64::MAX)
                .unwrap_err();
        assert!(err.contains("invalid k=2 for 1 unique bits"), "{err}");
    }

    #[test]
    fn malformed_and_out_of_width_pairs_are_rejected() {
        for pair in [(3u32, 3u32), (5, 2), (0, 64), (0, 255)] {
            let err = enumerate_prefilter_impl::<u64>(
                &no_bars(),
                &[0, 1],
                1,
                &[pair],
                0,
                0,
                u64::MAX,
                u64::MAX,
            )
            .unwrap_err();
            assert!(err.contains("malformed conflict pair"), "{pair:?}: {err}");
        }
        // (0, 64) is LEGAL on the u128 core (width-scoped VALUE bound) …
        let planes = EnumPlanes::build::<u128>(&[]).expect("planes");
        assert!(
            enumerate_prefilter_impl::<u128>(
                &planes,
                &[0, 1],
                1,
                &[(0, 64)],
                0,
                0,
                u64::MAX,
                u64::MAX
            )
            .is_ok()
        );
        // … and (0, 128) is not.
        assert!(
            enumerate_prefilter_impl::<u128>(
                &planes,
                &[0, 1],
                1,
                &[(0, 128)],
                0,
                0,
                u64::MAX,
                u64::MAX
            )
            .is_err()
        );
        // Duplicate WELL-FORMED pairs are tolerated (frozenset semantics).
        let (_, pruned, .., kept) = enumerate_prefilter_impl::<u64>(
            &no_bars(),
            &[0, 1],
            2,
            &[(0, 1), (0, 1)],
            0,
            0,
            u64::MAX,
            u64::MAX,
        )
        .expect("valid");
        assert!(kept.is_empty());
        assert_eq!(pruned, 1);
    }

    #[test]
    fn total_count_past_u64_is_a_clean_err() {
        // C(128, 64) ≈ 2.4e37 > u64::MAX — the u128 table computes it
        // exactly and the rank-domain check refuses, never an abort.
        let planes = EnumPlanes::build::<u128>(&[]).expect("planes");
        let bits: Vec<u32> = (0..128).collect();
        let err =
            enumerate_prefilter_impl::<u128>(&planes, &bits, 64, &[], 0, 0, u64::MAX, u64::MAX)
                .unwrap_err();
        assert!(err.contains("exceeds the u64 rank domain"), "{err}");
    }

    #[test]
    fn reserve_kept_alloc_failure_is_a_clean_err() {
        // u64::MAX masks × 16 bytes overflows isize — a deterministic
        // capacity Err with NO allocator round-trip (the arm the PyErr
        // mapper turns into MemoryError).
        let err = reserve_kept::<u128>(u64::MAX).unwrap_err();
        assert!(err.starts_with(ALLOC_ERR_PREFIX), "{err}");
        let out = reserve_kept::<u64>(16).expect("small reserve");
        assert!(out.capacity() >= 16);
        assert!(out.is_empty());
    }

    // ── binomial table + CNS unranking pins ──────────────────────────────

    #[test]
    fn binomial_table_known_values() {
        let t = binomial_table(60, 6).expect("table");
        assert_eq!(binom(&t, 60, 0), 1);
        assert_eq!(binom(&t, 60, 1), 60);
        assert_eq!(binom(&t, 60, 2), 1_770);
        assert_eq!(binom(&t, 60, 3), 34_220);
        assert_eq!(binom(&t, 60, 5), 5_461_512);
        assert_eq!(binom(&t, 60, 6), 50_063_860);
        assert_eq!(binom(&t, 0, 0), 1);
        // Out-of-range j > i reads 0 (the unranking identity).
        assert_eq!(binom(&t, 3, 6), 0);
    }

    #[test]
    fn binomial_table_serves_c_128_64_exactly() {
        // The largest reachable cell fits u128 exactly (independently
        // computed via Python math.comb).
        let t = binomial_table(128, 64).expect("table");
        assert_eq!(
            binom(&t, 128, 64),
            23951146041928082866135587776380551750_u128
        );
    }

    #[test]
    fn unrank_lex_matches_the_odometer_stream() {
        // Unranking every rank of C(7, 3) reproduces the odometer walk —
        // the LEX-RANK (not colex) law, exhaustively.
        let n = 7usize;
        let k = 3usize;
        let t = binomial_table(n, k).expect("table");
        let mut idx: Vec<usize> = (0..k).collect();
        let mut rank = 0u128;
        loop {
            assert_eq!(
                unrank_lex(&t, n, k, rank).expect("unrank"),
                idx,
                "rank {rank}"
            );
            assert_eq!(rank_lex(&t, n, k, &idx), rank);
            rank += 1;
            if !advance_odometer(&mut idx, n, k) {
                break;
            }
        }
        assert_eq!(rank, binom(&t, n, k));
    }

    // ── differential vs the naive reference ──────────────────────────────

    #[test]
    fn matches_reference_on_exhaustive_small_matrix() {
        // Every n <= 7, 0 <= k <= n, min_hits ∈ {0, 1, 2, 9}, with a pair
        // table spanning the palette and a mixed bar window.
        let bars: Vec<u128> = (0..21u128).map(|i| i % 128).collect();
        let pairs = [(0u32, 1u32), (2, 3), (1, 5)];
        for n in 0..=7u32 {
            let bits: Vec<u32> = (0..n).collect();
            for k in 0..=(n as usize) {
                for min_hits in [0u64, 1, 2, 9] {
                    let kernel = monolithic(&bars, &bits, k, &pairs, min_hits);
                    let oracle = reference(&bars, &bits, k, &pairs, min_hits);
                    assert_eq!(
                        (kernel.5, kernel.0, kernel.1, kernel.4),
                        oracle,
                        "n={n} k={k} min_hits={min_hits}"
                    );
                }
            }
        }
    }

    // ── proptest: kernel == reference; CNS round-trip; ranged == mono ─────

    use proptest::prelude::*;

    proptest! {
        #[test]
        fn prop_kernel_matches_reference(
            bar_bits in proptest::collection::vec(0u128..(1u128 << 100), 0..24),
            bits in proptest::collection::vec(0u32..128, 0..10),
            k_raw in 0usize..6,
            pairs in proptest::collection::vec((0u32..127, 1u32..128), 0..8),
            min_hits in 0u64..8,
        ) {
            let mut unique = bits.clone();
            unique.sort_unstable();
            unique.dedup();
            let k = k_raw % (unique.len() + 1);
            let pairs: Vec<(u32, u32)> = pairs
                .into_iter()
                .filter(|(a, b)| a != b)
                .map(|(a, b)| (a.min(b), a.max(b)))
                .collect();
            let kernel = monolithic(&bar_bits, &bits, k, &pairs, min_hits);
            let oracle = reference(&bar_bits, &bits, k, &pairs, min_hits);
            // List equality asserts SET ∧ ORDER in one assertion.
            prop_assert_eq!(kernel.5, oracle.0);
            prop_assert_eq!((kernel.0, kernel.1, kernel.4), (oracle.1, oracle.2, oracle.3));
            // Σ law: enumerated == C(n_unique, k); exhausted in-band.
            let t = binomial_table(unique.len(), k).expect("table");
            prop_assert_eq!(u128::from(kernel.0), binom(&t, unique.len(), k));
            prop_assert!(kernel.3);
        }

        #[test]
        fn prop_cns_round_trip(
            n in 1usize..40,
            k_raw in 0usize..8,
            seed in 0u64..u64::MAX,
        ) {
            let k = k_raw.min(n);
            let t = binomial_table(n, k).expect("table");
            let total = binom(&t, n, k);
            let rank = u128::from(seed) % total;
            let idx = unrank_lex(&t, n, k, rank).expect("unrank");
            // Ascending, in range, and rank(unrank(r)) == r.
            prop_assert!(idx.windows(2).all(|w| w[0] < w[1]));
            prop_assert!(idx.iter().all(|&c| c < n));
            prop_assert_eq!(rank_lex(&t, n, k, &idx), rank);
        }

        #[test]
        fn prop_ranged_equals_monolithic(
            bar_bits in proptest::collection::vec(0u128..(1u128 << 20), 0..16),
            n in 1u32..9,
            k_raw in 1usize..4,
            min_hits in 0u64..4,
            max_enumerated in 1u64..10,
            max_work_cells in prop_oneof![Just(u64::MAX), 1u64..200],
        ) {
            let bits: Vec<u32> = (0..n).collect();
            let k = k_raw.min(n as usize);
            let pairs = [(0u32, 2u32)];
            let mono = monolithic(&bar_bits, &bits, k, &pairs, min_hits);
            let planes = EnumPlanes::build::<u128>(&bar_bits).expect("planes");
            let n_bars = bar_bits.len() as u64;
            let mut rank = 0u64;
            let mut kept: Vec<u128> = Vec::new();
            let (mut enumerated, mut pruned, mut hits) = (0u64, 0u64, 0u64);
            loop {
                let (e, p, next, exhausted, h, kp) = enumerate_prefilter_impl::<u128>(
                    &planes, &bits, k, &pairs, min_hits, rank, max_enumerated, max_work_cells,
                )
                .expect("valid");
                // Cap-respected assertions from the return tuple's own fields
                // (the design's dual-knob matrix law).
                prop_assert!(next - rank <= max_enumerated.max(1));
                prop_assert!((next - rank).saturating_mul(n_bars) <= max_work_cells.saturating_add(n_bars));
                prop_assert!(next > rank || exhausted);
                enumerated += e;
                pruned += p;
                hits += h;
                kept.extend(kp);
                rank = next;
                if exhausted {
                    break;
                }
            }
            prop_assert_eq!((enumerated, pruned, rank, true, hits, kept), mono);
        }
    }
}
