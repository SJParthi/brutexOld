//! Resident sweep core — Slice 1: integer-only enumeration + prefilter.
//!
//! DESIGN_FINAL_RUST_SWEEP_CORE.md §1.3/§6 (Slice 1) — the `enumerate_prefilter`
//! kernel: LEX-RANK combinatorial-number-system unranking over the SORTED
//! condition bits (the POSITION-LEX / `itertools.combinations` order the
//! engine's `iter_masks_k_of_bits` emits — NOT ascending-numeric/colex; the
//! memorialized counterexample: n=4, k=2 → itertools emits masks
//! `[3, 5, 9, 6, 10, 12]` vs ascending-numeric `[3, 5, 6, 9, 10, 12]`),
//! conflict-prune against CONFLICT_PAIRS injected from Python as DATA (Rust
//! never re-derives them — single source), and the fused Stage-1B min-hits
//! count against a CONDITION-MAJOR TRANSPOSED-BITPLANE substrate built once
//! per call from the resident `bar_bits` (the lever-discovery fold item 2 —
//! chosen so the later prefix-AND DFS pruning lever retrofits freely; the
//! substrate is invisible outside the kernel).
//!
//! Contract highlights (all from the locked design):
//!
//! * **Stateless across calls** — no files, no statics, no caches, no
//!   cursors. Ranges are driven by `(start_rank, max_enumerated,
//!   max_work_cells)`; the CNS unranking is exact + order-preserving.
//! * **MIN-HITS SHORT-CIRCUIT LAW (§1.3, round-6):** at `min_hits == 0` the
//!   per-mask O(n_bars) supermask-hits count MUST NOT run — kept =
//!   conflict-free directly. `hits_counted_in_range` is the BYTE-OBSERVABLE
//!   in-band oracle: 0 when `min_hits == 0`, `enumerated − conflict_pruned`
//!   when `min_hits > 0`.
//! * **Work-cap charging rule (§1.3, fix-forward 5+9):** cells are charged
//!   per enumerated mask × n_bars REGARDLESS of the min-hits short-circuit
//!   (deliberately min-hits-BLIND — liveness-conservative). Caps are checked
//!   AFTER each mask's work, so every non-exhausted call advances at least
//!   ONE mask (`next_rank > start_rank` — the guaranteed-progress law) and
//!   `(next_rank − start_rank) × n_bars ≤ max_work_cells + n_bars` (one-mask
//!   slop).
//! * **Exhaustion contract (§1.3, round-3):** `exhausted` is computed IN-CORE
//!   as `next_rank == total_count` with `total_count = C(n_distinct, k)` in
//!   checked u128 arithmetic; a total exceeding `u64::MAX` is a clean `Err`
//!   refusal (never an overflow-check SIGABRT — C(128, 64) is
//!   Python-reachable through this FFI).
//! * **Domain validation up front (§1.3 rounds 1-3):** (a) `k ≤ n_distinct ≤
//!   width` with `k == 0` LEGAL (C(n, 0) = 1, the single empty mask); (b)
//!   EVERY condition-bit index AND every conflict-pair index `< width` (the
//!   bits=[100]-on-u64 shift-overflow abort class); (c) `start_rank ≤
//!   total_count` (`rank_out_of_domain` — a rank past the domain has NO
//!   defined combination and must never silently saturate);
//!   `start_rank == total_count` is legal and returns
//!   `(0, 0, total_count, exhausted=true, 0, [])`.
//! * **Supermask predicate:** the SAME `(bar & mask) == mask` containment
//!   test as `count_one` / `MaskWord::is_superset` — the bitplane AND +
//!   popcount evaluation is exactly that predicate transposed (the strict
//!   `hits >= min_hits` boundary is pinned by the in-crate parity property).
//!
//! Placement per §1.1: this module lives in the existing fno-math crate so it
//! can consume `trade_sim.rs`'s `MaskWord`/`SweepColumns`/`build_columns`
//! (pub(crate)-promoted, visibility-only) as internal Rust items; the
//! `SweepColumns` price/forced/session validation at construction is the SAME
//! already-mutation-gated code path `ResidentSweepSim::__new__` runs. The
//! core owns ZERO lake I/O — bars and bar_bits cross the FFI as DATA (§12
//! data-plane law). Pure `Result<_, String>` cores + one-line PyErr wrappers
//! (the crate convention — cargo test links no libpython).

// Module-scoped indexing discipline (design §8 R4 / round-4): a slip of the
// index arithmetic must be a compile-time refusal here, never a runtime
// panic→SIGABRT under the crate's panic="abort" profile.
#![deny(clippy::indexing_slicing)]

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

use crate::trade_sim::{MaskWord, SweepColumns, build_columns};

/// In-band ABI version for the sweep-core surface (§1.3). Bumped on ANY
/// change to the `enumerate_prefilter` return shape or (Slice 2) the
/// `fold_block` buffer layout; the Python seam refuses engagement on a
/// mismatch (`abi_version_mismatch`) so a stale wheel can never be decoded
/// with the wrong layout.
pub const SWEEP_CORE_ABI_VERSION: u32 = 1;

/// Width-generic word operations the enumeration core needs BEYOND the
/// promoted [`MaskWord`] superset test. Kept crate-internal to this module's
/// zone (§11): extending `MaskWord` itself would violate the "visibility-only
/// promotion, zero body/signature change" grant.
pub(crate) trait EnumWord: MaskWord + PartialEq {
    /// Kernel width in bits (64 / 128) — the §1.3 VALUE bound.
    const WIDTH: u32;
    /// The all-zero word (the k == 0 empty mask).
    fn zero() -> Self;
    /// Single-bit word `1 << pos`. Callers MUST have validated
    /// `pos < Self::WIDTH` (the §1.3 up-front VALUE bound); the checked shift
    /// degrades to zero on the structurally-unreachable violation instead of
    /// panicking (panic="abort" would SIGABRT the host process).
    fn bit(pos: u32) -> Self;
    /// Bitwise OR.
    fn union(a: Self, b: Self) -> Self;
}

impl EnumWord for u64 {
    const WIDTH: u32 = 64;
    #[inline(always)]
    fn zero() -> Self {
        0
    }
    #[inline(always)]
    fn bit(pos: u32) -> Self {
        1u64.checked_shl(pos).unwrap_or(0)
    }
    #[inline(always)]
    fn union(a: Self, b: Self) -> Self {
        a | b
    }
}

impl EnumWord for u128 {
    const WIDTH: u32 = 128;
    #[inline(always)]
    fn zero() -> Self {
        0
    }
    #[inline(always)]
    fn bit(pos: u32) -> Self {
        1u128.checked_shl(pos).unwrap_or(0)
    }
    #[inline(always)]
    fn union(a: Self, b: Self) -> Self {
        a | b
    }
}

/// The `enumerate_prefilter` return record (pure core side; the FFI wrappers
/// project it to the §1.3 6-tuple `(enumerated_in_range,
/// conflict_pruned_in_range, next_rank, exhausted, hits_counted_in_range,
/// kept_masks)`).
#[cfg_attr(test, derive(Debug))]
pub(crate) struct EnumRange<W> {
    enumerated: u64,
    conflict_pruned: u64,
    next_rank: u64,
    exhausted: bool,
    hits_counted: u64,
    kept: Vec<W>,
}

impl<W> EnumRange<W> {
    fn into_tuple(self) -> (u64, u64, u64, bool, u64, Vec<W>) {
        (
            self.enumerated,
            self.conflict_pruned,
            self.next_rank,
            self.exhausted,
            self.hits_counted,
            self.kept,
        )
    }
}

/// Exact Pascal-triangle binomial table `C(0..=n, 0..=k)` in CHECKED u128.
///
/// For `n ≤ 128` every entry fits u128 (`C(128, 64) ≈ 2.4e37 <
/// u128::MAX ≈ 3.4e38`), but the additions stay `checked_add` anyway — an
/// overflow is a clean `Err`, never an overflow-check SIGABRT (§1.3).
/// Out-of-table / `k > n` lookups read 0, the mathematically consistent
/// extension the unranking scan relies on at the right edge.
struct BinomTable {
    /// rows[m] holds C(m, 0..=k_cap).
    rows: Vec<Vec<u128>>,
}

impl BinomTable {
    fn build(n: usize, k_cap: usize) -> Result<Self, String> {
        let mut rows: Vec<Vec<u128>> = Vec::with_capacity(n.saturating_add(1));
        for m in 0..=n {
            let mut row: Vec<u128> = Vec::with_capacity(k_cap.saturating_add(1));
            for j in 0..=k_cap {
                let value: u128 = if j == 0 {
                    1
                } else if j > m {
                    0
                } else {
                    let prev = rows.last().ok_or_else(|| {
                        "enumerate_prefilter: binomial table build invariant violated".to_string()
                    })?;
                    let a = prev.get(j).copied().unwrap_or(0);
                    let b = prev.get(j.saturating_sub(1)).copied().unwrap_or(0);
                    a.checked_add(b).ok_or_else(|| {
                        format!("enumerate_prefilter: binomial C({m},{j}) overflows u128")
                    })?
                };
                row.push(value);
            }
            rows.push(row);
        }
        Ok(Self { rows })
    }

    /// `C(m, j)` — 0 for `j > m` or out-of-table (consistent extension).
    fn get(&self, m: usize, j: usize) -> u128 {
        self.rows
            .get(m)
            .and_then(|row| row.get(j))
            .copied()
            .unwrap_or(0)
    }
}

/// LEX-RANK CNS unranking: rank `r` within `combinations(0..n, k)` POSITION-LEX
/// order → the ascending index tuple (indices into the sorted-unique bit
/// list). The lever-discovery fold item 1 MANDATE — this is the rank order of
/// `itertools.combinations`, NOT the colex order of the standard textbook CNS.
///
/// Standard scan: at each slot, a candidate index `c` prefixes
/// `C(n − 1 − c, k − 1 − slot)` combinations; walk candidates ascending,
/// subtracting, until the rank falls inside a candidate's block. O(n·k) with
/// the exact table. The caller guarantees `r < C(n, k)`; the internal bound
/// checks turn any violation into a clean `Err` (never a panic).
fn unrank_lex(r: u128, n: usize, k: usize, binom: &BinomTable) -> Result<Vec<usize>, String> {
    let mut remaining = r;
    let mut positions: Vec<usize> = Vec::with_capacity(k);
    let mut cand = 0usize;
    for slot in 0..k {
        let mut chosen: Option<usize> = None;
        while cand < n {
            let m = n
                .checked_sub(1)
                .and_then(|v| v.checked_sub(cand))
                .ok_or_else(|| "enumerate_prefilter: unrank candidate out of range".to_string())?;
            let j = k
                .checked_sub(1)
                .and_then(|v| v.checked_sub(slot))
                .ok_or_else(|| "enumerate_prefilter: unrank slot out of range".to_string())?;
            let cnt = binom.get(m, j);
            if remaining < cnt {
                chosen = Some(cand);
                cand = cand.saturating_add(1);
                break;
            }
            remaining = remaining
                .checked_sub(cnt)
                .ok_or_else(|| "enumerate_prefilter: unrank rank underflow".to_string())?;
            cand = cand.saturating_add(1);
        }
        let picked =
            chosen.ok_or_else(|| "enumerate_prefilter: unrank invariant violated".to_string())?;
        positions.push(picked);
    }
    Ok(positions)
}

/// Advance an ascending index combination to its POSITION-LEX successor.
///
/// The classic `itertools.combinations` step: find the rightmost slot that
/// can increment (`positions[i] < n − k + i`), bump it, and reset every later
/// slot consecutive. The caller only invokes this while a successor EXISTS
/// (`rank + 1 < total`); on the structurally-unreachable last combination it
/// leaves the input unchanged (the caller's rank bookkeeping is the
/// authority, so no silent extra emission is possible).
fn advance_lex(positions: &mut [usize], n: usize) {
    let k = positions.len();
    let mut i = k;
    while i > 0 {
        i = i.saturating_sub(1);
        let limit = n.saturating_sub(k).saturating_add(i);
        let cur = positions.get(i).copied().unwrap_or(0);
        if cur < limit {
            let base = cur.saturating_add(1);
            for (j, slot) in positions.iter_mut().enumerate().skip(i) {
                *slot = base.saturating_add(j.saturating_sub(i));
            }
            return;
        }
    }
}

/// Build the CONDITION-MAJOR TRANSPOSED BITPLANES (lever-discovery fold item
/// 2): one `Vec<u64>` bitmap over bars per sorted-unique condition bit — word
/// `w` bit `o` set iff bar `w*64 + o` carries the condition. Built once per
/// call from the resident `bar_bits` (only when `min_hits > 0` and `k > 0` —
/// the short-circuit law means a `min_hits == 0` call never pays this).
/// Tail bits beyond `n_bars` are structurally zero (only real bars set bits),
/// so plane-AND popcounts never over-count.
fn build_bitplanes<W: EnumWord>(bar_bits: &[W], unique_bits: &[u32]) -> Vec<Vec<u64>> {
    unique_bits
        .iter()
        .map(|&pos| {
            let probe = W::bit(pos);
            bar_bits
                .chunks(64)
                .map(|chunk| {
                    let mut word = 0u64;
                    for (off, &bar) in chunk.iter().enumerate() {
                        if W::is_superset(bar, probe) {
                            word |= 1u64.checked_shl(off as u32).unwrap_or(0);
                        }
                    }
                    word
                })
                .collect()
        })
        .collect()
}

/// Supermask-hits count for one combination via the bitplane substrate:
/// AND the k selected planes word-wise and popcount — exactly the transposed
/// `(bar & mask) == mask` containment count (`count_one`'s predicate).
/// `positions` is non-empty here (the k == 0 arm counts `n_bars` directly).
fn count_plane_hits(planes: &[Vec<u64>], positions: &[usize]) -> Result<u64, String> {
    let (first_pos, rest) = positions
        .split_first()
        .ok_or_else(|| "enumerate_prefilter: hits count over empty combination".to_string())?;
    let first_plane = planes
        .get(*first_pos)
        .ok_or_else(|| "enumerate_prefilter: bitplane index out of range".to_string())?;
    let mut selected: Vec<&[u64]> = Vec::with_capacity(rest.len());
    for &p in rest {
        let plane = planes
            .get(p)
            .ok_or_else(|| "enumerate_prefilter: bitplane index out of range".to_string())?;
        selected.push(plane.as_slice());
    }
    let mut total: u64 = 0;
    for (wi, &w0) in first_plane.iter().enumerate() {
        let mut word = w0;
        for plane in &selected {
            word &= plane.get(wi).copied().unwrap_or(0);
        }
        total = total.saturating_add(u64::from(word.count_ones()));
    }
    Ok(total)
}

/// The pure `enumerate_prefilter` core (no PyErr — the crate convention).
///
/// See the module docstring + §1.3 for the full contract. Returns the range
/// record; every refusal is a clean `Err(String)`.
#[allow(clippy::too_many_arguments)]
pub(crate) fn enumerate_prefilter_impl<W: EnumWord>(
    bar_bits: &[W],
    condition_bits: &[u32],
    k: usize,
    conflict_pairs: &[(u32, u32)],
    min_hits: u64,
    start_rank: u64,
    max_enumerated: u64,
    max_work_cells: u64,
) -> Result<EnumRange<W>, String> {
    // Re-sort + dedup in-core (§1.3: the seam passes Python-sorted-unique
    // bits; the core normalizes anyway so the two can never drift).
    let mut unique: Vec<u32> = condition_bits.to_vec();
    unique.sort_unstable();
    unique.dedup();

    // ── Domain validation UP FRONT (§1.3 (a)/(b)/(c)) — before any shift,
    // mask construction, or unranking. ──
    for &pos in &unique {
        if pos >= W::WIDTH {
            return Err(format!(
                "enumerate_prefilter: condition bit {pos} >= kernel width {} — refused",
                W::WIDTH
            ));
        }
    }
    for &(a, b) in conflict_pairs {
        if a >= W::WIDTH || b >= W::WIDTH {
            return Err(format!(
                "enumerate_prefilter: conflict pair ({a}, {b}) has an index >= kernel width {} — refused",
                W::WIDTH
            ));
        }
    }
    let n = unique.len();
    if n > W::WIDTH as usize {
        // Structurally unreachable (each bit < WIDTH and deduped), kept as a
        // stated COUNT-bound refusal per §1.3 (a).
        return Err(format!(
            "enumerate_prefilter: {n} distinct bits exceed kernel width {} — refused",
            W::WIDTH
        ));
    }
    if k > n {
        return Err(format!(
            "enumerate_prefilter: k={k} exceeds n_distinct={n} — refused"
        ));
    }

    let binom = BinomTable::build(n, k)?;
    let total_u128 = binom.get(n, k);
    if total_u128 > u128::from(u64::MAX) {
        return Err(format!(
            "enumerate_prefilter: combination count C({n},{k}) = {total_u128} exceeds u64 — refused"
        ));
    }
    let total = total_u128 as u64;
    if start_rank > total {
        return Err(format!(
            "enumerate_prefilter: rank_out_of_domain — start_rank {start_rank} > C({n},{k}) = {total}"
        ));
    }
    if start_rank == total {
        // Legal empty-range call (§1.3 (c)).
        return Ok(EnumRange {
            enumerated: 0,
            conflict_pruned: 0,
            next_rank: total,
            exhausted: true,
            hits_counted: 0,
            kept: Vec::new(),
        });
    }

    // ── Per-call setup: pair masks + (min_hits > 0 only) the bitplanes. ──
    let pair_masks: Vec<W> = conflict_pairs
        .iter()
        .map(|&(a, b)| W::union(W::bit(a), W::bit(b)))
        .collect();
    let planes: Vec<Vec<u64>> = if min_hits > 0 && k > 0 {
        build_bitplanes(bar_bits, &unique)
    } else {
        Vec::new()
    };
    let n_bars = bar_bits.len() as u64;

    let mut positions = unrank_lex(u128::from(start_rank), n, k, &binom)?;
    let enum_floor = max_enumerated.max(1); // guaranteed-progress law (§3)

    let mut enumerated: u64 = 0;
    let mut conflict_pruned: u64 = 0;
    let mut hits_counted: u64 = 0;
    let mut kept: Vec<W> = Vec::new();
    let mut rank = start_rank;
    let mut cells: u64 = 0;

    loop {
        // Build this rank's mask from the sorted-unique bit positions.
        let mut mask = W::zero();
        for &idx in &positions {
            let pos = unique
                .get(idx)
                .copied()
                .ok_or_else(|| "enumerate_prefilter: combination index out of range".to_string())?;
            mask = W::union(mask, W::bit(pos));
        }
        enumerated = enumerated.saturating_add(1);
        // CHARGING RULE (§1.3): per enumerated mask × n_bars, min-hits-BLIND.
        cells = cells.saturating_add(n_bars);

        let conflicted = pair_masks.iter().any(|&p| W::is_superset(mask, p));
        if conflicted {
            conflict_pruned = conflict_pruned.saturating_add(1);
        } else if min_hits == 0 {
            // MIN-HITS SHORT-CIRCUIT LAW: no count runs; kept directly.
            kept.push(mask);
        } else {
            hits_counted = hits_counted.saturating_add(1);
            let hits = if k == 0 {
                // (bar & 0) == 0 for every bar — the empty mask hits all.
                n_bars
            } else {
                count_plane_hits(&planes, &positions)?
            };
            if hits >= min_hits {
                kept.push(mask);
            }
        }

        rank = rank.saturating_add(1);
        if rank == total {
            return Ok(EnumRange {
                enumerated,
                conflict_pruned,
                next_rank: rank,
                exhausted: true,
                hits_counted,
                kept,
            });
        }
        // Caps AFTER each mask's work (the §3 floor idiom): every
        // non-exhausted return has advanced ≥ 1 mask.
        if enumerated >= enum_floor || cells >= max_work_cells {
            return Ok(EnumRange {
                enumerated,
                conflict_pruned,
                next_rank: rank,
                exhausted: false,
                hits_counted,
                kept,
            });
        }
        advance_lex(&mut positions, n);
    }
}

/// Shared constructor body for both width pyclasses — the SAME
/// `build_columns` validation `ResidentSweepSim::__new__` runs (§1.3:
/// identical validation; the `MAX_PRICE_PAISA` envelope + 7-column length
/// checks are the already-mutation-gated code paths).
#[allow(clippy::too_many_arguments)]
fn build_core_columns(
    label: &str,
    open: Vec<i64>,
    high: Vec<i64>,
    low: Vec<i64>,
    close: Vec<i64>,
    is_forced: Vec<bool>,
    session_idx: Vec<i64>,
    bar_bits_len: usize,
) -> Result<SweepColumns, String> {
    build_columns(
        label,
        open,
        high,
        low,
        close,
        is_forced,
        session_idx,
        bar_bits_len,
    )
}

/// Resident sweep core over a u64 `bar_bits` window (§1.3).
///
/// Slice 1 exposes `n_bars` + `enumerate_prefilter`; the Slice-2 `fold_block`
/// consumes the retained price/forced/session columns.
#[pyclass(frozen, module = "fno_math")]
pub struct ResidentSweepCore {
    /// Retained for the Slice-2 `fold_block` (the frozen §1.3 constructor
    /// contract takes the full 7 columns now so the ABI never moves).
    #[allow(dead_code)]
    columns: SweepColumns,
    bar_bits: Vec<u64>,
}

/// Resident sweep core over a u128 `bar_bits` window — the [64, 128)-bit
/// headroom twin (§1.3; separately monomorphised, same pure core).
#[pyclass(frozen, module = "fno_math")]
pub struct ResidentSweepCoreU128 {
    #[allow(dead_code)]
    columns: SweepColumns,
    bar_bits: Vec<u128>,
}

#[pymethods]
impl ResidentSweepCore {
    /// Build the resident core — identical column validation to
    /// `ResidentSweepSim.__new__` (clean `ValueError`, never a panic).
    #[new]
    fn new(
        open: Vec<i64>,
        high: Vec<i64>,
        low: Vec<i64>,
        close: Vec<i64>,
        is_forced: Vec<bool>,
        session_idx: Vec<i64>,
        bar_bits: Vec<u64>,
    ) -> PyResult<Self> {
        let columns = build_core_columns(
            "ResidentSweepCore",
            open,
            high,
            low,
            close,
            is_forced,
            session_idx,
            bar_bits.len(),
        )
        .map_err(PyValueError::new_err)?;
        Ok(Self { columns, bar_bits })
    }

    /// Number of resident bars — the §1.3 F27 seam-invariant probe
    /// (`core.n_bars() == len(python-side bars)` is asserted by the seam
    /// immediately after construction; a mismatch is `bars_length_mismatch`).
    fn n_bars(&self) -> usize {
        self.bar_bits.len()
    }

    /// Enumerate + conflict-prune + Stage-1B-prefilter one rank range.
    ///
    /// Returns `(enumerated_in_range, conflict_pruned_in_range, next_rank,
    /// exhausted, hits_counted_in_range, kept_masks)` — the §1.3 6-tuple.
    #[pyo3(signature = (condition_bits, k, conflict_pairs, min_hits, start_rank, max_enumerated, max_work_cells))]
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
        enumerate_prefilter_impl(
            &self.bar_bits,
            &condition_bits,
            k,
            &conflict_pairs,
            min_hits,
            start_rank,
            max_enumerated,
            max_work_cells,
        )
        .map(EnumRange::into_tuple)
        .map_err(PyValueError::new_err)
    }
}

#[pymethods]
impl ResidentSweepCoreU128 {
    /// Build the resident u128 core — same validation as the u64 twin.
    #[new]
    fn new(
        open: Vec<i64>,
        high: Vec<i64>,
        low: Vec<i64>,
        close: Vec<i64>,
        is_forced: Vec<bool>,
        session_idx: Vec<i64>,
        bar_bits: Vec<u128>,
    ) -> PyResult<Self> {
        let columns = build_core_columns(
            "ResidentSweepCoreU128",
            open,
            high,
            low,
            close,
            is_forced,
            session_idx,
            bar_bits.len(),
        )
        .map_err(PyValueError::new_err)?;
        Ok(Self { columns, bar_bits })
    }

    /// Number of resident bars (F27 seam-invariant probe — see the u64 twin).
    fn n_bars(&self) -> usize {
        self.bar_bits.len()
    }

    /// Enumerate + conflict-prune + Stage-1B-prefilter one rank range
    /// (u128 masks; same contract as the u64 twin).
    #[pyo3(signature = (condition_bits, k, conflict_pairs, min_hits, start_rank, max_enumerated, max_work_cells))]
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
        enumerate_prefilter_impl(
            &self.bar_bits,
            &condition_bits,
            k,
            &conflict_pairs,
            min_hits,
            start_rank,
            max_enumerated,
            max_work_cells,
        )
        .map(EnumRange::into_tuple)
        .map_err(PyValueError::new_err)
    }
}

// ────────────────────────────────────────────────────────────────────────
// In-crate tests (pure cores — no libpython link).
// ────────────────────────────────────────────────────────────────────────
#[cfg(test)]
#[allow(clippy::indexing_slicing)]
mod tests {
    use super::*;
    use proptest::prelude::*;

    /// Reference oracle: POSITION-LEX enumeration via the successor walk
    /// from rank 0 (mirrors `itertools.combinations` by construction).
    fn oracle_masks(bits: &[u32], k: usize) -> Vec<u64> {
        let mut unique: Vec<u32> = bits.to_vec();
        unique.sort_unstable();
        unique.dedup();
        let n = unique.len();
        if k > n {
            return Vec::new();
        }
        // itertools.combinations replica over indices.
        let mut out = Vec::new();
        let mut idx: Vec<usize> = (0..k).collect();
        loop {
            let mask = idx.iter().fold(0u64, |m, &i| m | (1u64 << unique[i]));
            out.push(mask);
            // successor
            let mut i = k;
            let mut advanced = false;
            while i > 0 {
                i -= 1;
                if idx[i] < n - k + i {
                    idx[i] += 1;
                    for j in (i + 1)..k {
                        idx[j] = idx[j - 1] + 1;
                    }
                    advanced = true;
                    break;
                }
            }
            if k == 0 || !advanced {
                break;
            }
        }
        out
    }

    fn run_full(
        bar_bits: &[u64],
        bits: &[u32],
        k: usize,
        pairs: &[(u32, u32)],
        min_hits: u64,
    ) -> EnumRange<u64> {
        enumerate_prefilter_impl(bar_bits, bits, k, pairs, min_hits, 0, u64::MAX, u64::MAX)
            .expect("full-range call must succeed")
    }

    #[test]
    fn emission_order_is_position_lex_not_colex() {
        // The memorialized n=4, k=2 counterexample (lever-discovery fold
        // item 1): itertools order vs ascending-numeric (colex) order.
        let range = run_full(&[], &[0, 1, 2, 3], 2, &[], 0);
        assert_eq!(range.kept, vec![3, 5, 9, 6, 10, 12]);
        assert_ne!(range.kept, vec![3, 5, 6, 9, 10, 12]); // colex — the bug
        assert_eq!(range.enumerated, 6);
        assert_eq!(range.next_rank, 6);
        assert!(range.exhausted);
        assert_eq!(range.hits_counted, 0); // min_hits == 0 short-circuit
    }

    #[test]
    fn emission_matches_oracle_scattered_bits() {
        let bits = [3u32, 0, 7, 12, 5, 21, 33, 63];
        for k in 0..=6usize {
            let range = run_full(&[], &bits, k, &[], 0);
            assert_eq!(range.kept, oracle_masks(&bits, k), "k={k}");
            assert!(range.exhausted);
        }
    }

    #[test]
    fn k0_vector_is_the_single_empty_mask() {
        // §1.3 (a): k == 0 is LEGAL — C(n, 0) = 1, the empty mask 0.
        let range = run_full(&[0b1, 0b10], &[0, 1, 5], 0, &[(0, 1)], 0);
        assert_eq!(range.kept, vec![0]);
        assert_eq!(range.enumerated, 1);
        assert_eq!(range.conflict_pruned, 0);
        assert_eq!(range.next_rank, 1);
        assert!(range.exhausted);
        assert_eq!(range.hits_counted, 0);
        // min_hits > 0: the empty mask hits EVERY bar ((bar & 0) == 0).
        let range = run_full(&[0b1, 0b10], &[0, 1, 5], 0, &[], 2);
        assert_eq!(range.kept, vec![0]);
        assert_eq!(range.hits_counted, 1);
        let range = run_full(&[0b1, 0b10], &[0, 1, 5], 0, &[], 3);
        assert!(range.kept.is_empty()); // 2 bars < min_hits 3
    }

    #[test]
    fn conflict_prune_counts_and_drops() {
        // bits {0,1,2}, k=2 → masks [3, 5, 6]; pair (0,1) conflicts mask 3.
        let range = run_full(&[], &[0, 1, 2], 2, &[(0, 1)], 0);
        assert_eq!(range.kept, vec![5, 6]);
        assert_eq!(range.enumerated, 3);
        assert_eq!(range.conflict_pruned, 1);
    }

    #[test]
    fn min_hits_strict_boundary_and_hits_counted() {
        // 4 bars; bit 0 on bars 0-2 (3 hits), bit 1 on bars 0-1 (2 hits).
        let bar_bits = [0b11u64, 0b11, 0b01, 0b00];
        // mask 0b01 (bit 0): hits = 3; mask 0b10 (bit 1): 2; mask 0b11: 2.
        let range = run_full(&bar_bits, &[0, 1], 1, &[], 3);
        assert_eq!(range.kept, vec![0b01]); // strict >=: 3 >= 3 kept, 2 < 3 dropped
        assert_eq!(range.hits_counted, 2); // count ran on every conflict-free mask
        let range = run_full(&bar_bits, &[0, 1], 1, &[], 2);
        assert_eq!(range.kept, vec![0b01, 0b10]); // 2 >= 2 kept at the boundary
        // min_hits == 0: no count runs (the short-circuit law).
        let range = run_full(&bar_bits, &[0, 1], 1, &[], 0);
        assert_eq!(range.hits_counted, 0);
        assert_eq!(range.kept, vec![0b01, 0b10]);
    }

    #[test]
    fn hits_counted_equals_conflict_free_count_when_min_hits_positive() {
        let bar_bits = [0b111u64; 10];
        let range = run_full(&bar_bits, &[0, 1, 2], 2, &[(0, 1)], 1);
        assert_eq!(range.enumerated, 3);
        assert_eq!(range.conflict_pruned, 1);
        assert_eq!(range.hits_counted, range.enumerated - range.conflict_pruned);
    }

    #[test]
    fn cap_exact_next_rank_one_mask() {
        // §3 cap-respected pins: a 1-mask cap advances EXACTLY one rank.
        let r = enumerate_prefilter_impl::<u64>(&[], &[0, 1, 2, 3], 2, &[], 0, 0, 1, u64::MAX)
            .expect("ok");
        assert_eq!(r.next_rank, 1);
        assert!(!r.exhausted);
        assert_eq!(r.kept, vec![3]);
        // max_enumerated == 0 still advances one mask (guaranteed progress).
        let r = enumerate_prefilter_impl::<u64>(&[], &[0, 1, 2, 3], 2, &[], 0, 0, 0, u64::MAX)
            .expect("ok");
        assert_eq!(r.next_rank, 1);
        assert!(!r.exhausted);
    }

    #[test]
    fn cap_exact_next_rank_k_masks() {
        let r = enumerate_prefilter_impl::<u64>(&[], &[0, 1, 2, 3], 2, &[], 0, 0, 4, u64::MAX)
            .expect("ok");
        assert_eq!(r.next_rank, 4);
        assert!(!r.exhausted);
        assert_eq!(r.kept, vec![3, 5, 9, 6]);
    }

    #[test]
    fn work_cells_cap_is_min_hits_blind_with_one_mask_slop() {
        // 100 bars; cells cap 250 → after mask 3 (300 cells >= 250) stop:
        // ⌈250/100⌉ = 3 masks — identical at min_hits 0 and 1 (the charging
        // rule is min-hits-BLIND).
        let bar_bits = vec![0b1111u64; 100];
        for min_hits in [0u64, 1] {
            let r = enumerate_prefilter_impl::<u64>(
                &bar_bits,
                &[0, 1, 2, 3],
                2,
                &[],
                min_hits,
                0,
                u64::MAX,
                250,
            )
            .expect("ok");
            assert_eq!(r.next_rank, 3, "min_hits={min_hits}");
            assert!(!r.exhausted);
            // (next_rank − start_rank) × n_bars ≤ max_work_cells + n_bars
            assert!(r.next_rank * 100 <= 250 + 100);
        }
        // Degenerate cells cap: still ≥ 1 mask of progress.
        let r =
            enumerate_prefilter_impl::<u64>(&bar_bits, &[0, 1, 2, 3], 2, &[], 0, 0, u64::MAX, 1)
                .expect("ok");
        assert_eq!(r.next_rank, 1);
        assert!(!r.exhausted);
    }

    #[test]
    fn ranged_equals_monolithic() {
        let bits = [0u32, 2, 4, 6, 8, 10, 12];
        let bar_bits: Vec<u64> = (0..50).map(|i| (i * 2654435761u64) & 0x1FFF).collect();
        let pairs = [(0u32, 2u32), (4u32, 6u32)];
        for k in 1..=4usize {
            for min_hits in [0u64, 5] {
                let mono = run_full(&bar_bits, &bits, k, &pairs, min_hits);
                for cap in [1u64, 2, 3, 7] {
                    let mut rank = 0u64;
                    let mut kept: Vec<u64> = Vec::new();
                    let mut enumerated = 0u64;
                    let mut pruned = 0u64;
                    loop {
                        let r = enumerate_prefilter_impl::<u64>(
                            &bar_bits,
                            &bits,
                            k,
                            &pairs,
                            min_hits,
                            rank,
                            cap,
                            u64::MAX,
                        )
                        .expect("ranged ok");
                        assert!(r.next_rank - rank <= cap.max(1));
                        kept.extend(r.kept);
                        enumerated += r.enumerated;
                        pruned += r.conflict_pruned;
                        rank = r.next_rank;
                        if r.exhausted {
                            break;
                        }
                    }
                    assert_eq!(kept, mono.kept, "k={k} cap={cap} min_hits={min_hits}");
                    assert_eq!(enumerated, mono.enumerated);
                    assert_eq!(pruned, mono.conflict_pruned);
                }
            }
        }
    }

    #[test]
    fn start_rank_at_total_is_legal_empty_exhausted() {
        let r = enumerate_prefilter_impl::<u64>(&[], &[0, 1, 2, 3], 2, &[], 0, 6, 100, u64::MAX)
            .expect("ok");
        assert_eq!(
            (
                r.enumerated,
                r.conflict_pruned,
                r.next_rank,
                r.exhausted,
                r.hits_counted
            ),
            (0, 0, 6, true, 0)
        );
        assert!(r.kept.is_empty());
    }

    #[test]
    fn start_rank_past_total_is_refused() {
        let err = enumerate_prefilter_impl::<u64>(&[], &[0, 1, 2, 3], 2, &[], 0, 7, 100, u64::MAX)
            .expect_err("must refuse");
        assert!(err.contains("rank_out_of_domain"), "{err}");
    }

    #[test]
    fn c128_choose_64_is_a_refusal_not_an_abort() {
        let bits: Vec<u32> = (0..128).collect();
        let err = enumerate_prefilter_impl::<u128>(&[], &bits, 64, &[], 0, 0, 100, u64::MAX)
            .expect_err("must refuse");
        assert!(err.contains("exceeds u64"), "{err}");
    }

    #[test]
    fn value_bound_refusals() {
        // bits=[100] on the u64 kernel — the shift-overflow abort class.
        let err = enumerate_prefilter_impl::<u64>(&[], &[100], 1, &[], 0, 0, 100, u64::MAX)
            .expect_err("must refuse");
        assert!(err.contains("condition bit 100"), "{err}");
        // conflict-pair index >= width.
        let err = enumerate_prefilter_impl::<u64>(&[], &[0, 1], 1, &[(0, 64)], 0, 0, 100, u64::MAX)
            .expect_err("must refuse");
        assert!(err.contains("conflict pair"), "{err}");
        // k > n_distinct.
        let err = enumerate_prefilter_impl::<u64>(&[], &[0, 1], 3, &[], 0, 0, 100, u64::MAX)
            .expect_err("must refuse");
        assert!(err.contains("k=3 exceeds n_distinct=2"), "{err}");
    }

    #[test]
    fn duplicate_bits_are_deduped_in_core() {
        let range = run_full(&[], &[5, 5, 5], 1, &[], 0);
        assert_eq!(range.kept, vec![32]);
        assert_eq!(range.enumerated, 1);
    }

    #[test]
    fn u128_high_bit_emission() {
        // Bits crossing 64 — the u128 monomorphisation's own order check.
        let bits = [60u32, 70, 100];
        let mut expected: Vec<u128> = Vec::new();
        for (i, j) in [(0usize, 1usize), (0, 2), (1, 2)] {
            expected.push((1u128 << bits[i]) | (1u128 << bits[j]));
        }
        let r = enumerate_prefilter_impl::<u128>(&[], &bits, 2, &[], 0, 0, u64::MAX, u64::MAX)
            .expect("ok");
        assert_eq!(r.kept, expected);
        assert!(r.exhausted);
    }

    proptest! {
        /// CNS unranking round-trip: rank → combination → recomputed rank.
        #[test]
        fn unrank_round_trip(n in 1usize..20, k in 0usize..7, seed in 0u64..u64::MAX) {
            let k = k.min(n);
            let binom = BinomTable::build(n, k).expect("table");
            let total = binom.get(n, k);
            prop_assume!(total > 0);
            let rank = u128::from(seed) % total;
            let positions = unrank_lex(rank, n, k, &binom).expect("unrank");
            // Recompute the lex rank of `positions`.
            let mut recomputed: u128 = 0;
            let mut prev: usize = 0;
            for (slot, &p) in positions.iter().enumerate() {
                for c in prev..p {
                    recomputed += binom.get(n - 1 - c, k - 1 - slot);
                }
                prev = p + 1;
            }
            prop_assert_eq!(recomputed, rank);
        }

        /// Bitplane hits ≡ the direct supermask count (`count_one` predicate
        /// parity at the strict >= boundary) — u64 width.
        #[test]
        fn plane_hits_parity_u64(
            bars in proptest::collection::vec(0u64..1024, 0..200),
            min_hits in 0u64..8,
        ) {
            let bits = [0u32, 1, 2, 3, 4, 5, 6, 7, 8, 9];
            let range = run_full(&bars, &bits, 3, &[], min_hits);
            // Direct oracle over the same emission order.
            let mut expected: Vec<u64> = Vec::new();
            for mask in oracle_masks(&bits, 3) {
                if min_hits == 0 {
                    expected.push(mask);
                } else {
                    let hits = bars.iter().filter(|&&b| (b & mask) == mask).count() as u64;
                    if hits >= min_hits {
                        expected.push(mask);
                    }
                }
            }
            prop_assert_eq!(range.kept, expected);
        }

        /// Same parity on the u128 kernel with bits straddling the 64 line.
        #[test]
        fn plane_hits_parity_u128(
            seeds in proptest::collection::vec(0u64..64, 0..100),
            min_hits in 0u64..5,
        ) {
            let bits = [1u32, 62, 63, 64, 65, 100];
            let bars: Vec<u128> = seeds
                .iter()
                .map(|&s| {
                    let mut v: u128 = 0;
                    for (i, &b) in bits.iter().enumerate() {
                        if (s >> i) & 1 == 1 {
                            v |= 1u128 << b;
                        }
                    }
                    v
                })
                .collect();
            let range = enumerate_prefilter_impl::<u128>(
                &bars, &bits, 2, &[], min_hits, 0, u64::MAX, u64::MAX,
            ).expect("ok");
            let mut unique: Vec<u32> = bits.to_vec();
            unique.sort_unstable();
            let mut expected: Vec<u128> = Vec::new();
            for i in 0..unique.len() {
                for j in (i + 1)..unique.len() {
                    let mask = (1u128 << unique[i]) | (1u128 << unique[j]);
                    let hits = bars.iter().filter(|&&b| (b & mask) == mask).count() as u64;
                    if min_hits == 0 || hits >= min_hits {
                        expected.push(mask);
                    }
                }
            }
            prop_assert_eq!(range.kept, expected);
        }

        /// Range partitioning at random split points reproduces the
        /// monolithic stream (chunk-invariance).
        #[test]
        fn random_range_partition(cap in 1u64..40, min_hits in 0u64..4) {
            let bits = [0u32, 3, 5, 9, 11, 13];
            let bars: Vec<u64> = (0..64u64).map(|i| i.wrapping_mul(0x9E3779B97F4A7C15) & 0x3FFF).collect();
            let pairs = [(3u32, 5u32)];
            let mono = run_full(&bars, &bits, 3, &pairs, min_hits);
            let mut rank = 0u64;
            let mut kept: Vec<u64> = Vec::new();
            loop {
                let r = enumerate_prefilter_impl::<u64>(
                    &bars, &bits, 3, &pairs, min_hits, rank, cap, u64::MAX,
                ).expect("ok");
                prop_assert!(r.next_rank > rank || r.exhausted);
                kept.extend(r.kept);
                rank = r.next_rank;
                if r.exhausted { break; }
            }
            prop_assert_eq!(kept, mono.kept);
        }
    }
}
