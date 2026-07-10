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
use pyo3::types::PyBytes;

use crate::metrics::{MAX_EXACT_EQUITY_PAISA, drawdown_stats_into};
use crate::trade_sim::{
    MaskWord, SweepColumns, build_columns, check_level_distance_for, policy_from_flags,
    simulate_one_core,
};

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

// ────────────────────────────────────────────────────────────────────────
// Slice 2 — the fold_block metrics core (§1.3 columnar ABI).
// ────────────────────────────────────────────────────────────────────────

/// §1.3 FOLD_INT_FIELDS — the i64 row layout, ONE layout law mirrored by the
/// Python seam's `FOLD_INT_FIELDS` tuple (pinned equal by the layout golden).
pub const SWEEP_CORE_FOLD_INT_FIELDS: [&str; 15] = [
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

/// §1.3 FOLD_FLOAT_FIELDS — the f64 row layout (same ONE layout law).
pub const SWEEP_CORE_FOLD_FLOAT_FIELDS: [&str; 9] = [
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

/// §1.3 flags byte: bit0 = zero_trade (float row zeros, unread).
pub const FOLD_FLAG_ZERO_TRADE: u8 = 0b0000_0001;
/// §1.3 flags byte: bit1 = python_required (2^52 equity envelope / i128→i64
/// narrowing failure / non-finite fold value — the Python spine serves).
pub const FOLD_FLAG_PYTHON_REQUIRED: u8 = 0b0000_0010;

/// §3 round-6 in-core fold work ceiling: `fold_block` refuses the WHOLE
/// chunk when `masks.len() × n_bars` exceeds this (2× the fold override's
/// own ≤2^27 upper clamp, so no clamp-legal configuration can reach it —
/// the loud bounded refusal for a wrong-chunk-sizing driver regression).
pub const FOLD_WORK_CEILING_CELLS: u128 = 1 << 28;

/// One decoded fold row before byte-encoding (crate-internal).
struct FoldRow {
    ints: [i64; 15],
    floats: [f64; 9],
    flags: u8,
}

impl FoldRow {
    fn zeroed(flags: u8) -> Self {
        Self {
            ints: [0i64; 15],
            floats: [0.0f64; 9],
            flags,
        }
    }
}

/// §1.3 bit1 non-finite belt, FACTORED into a tiny pure helper with direct
/// in-crate tests (defence-in-depth, stated as such: through the validated
/// entry points the envelope gate plausibly makes a non-finite fold value
/// unreachable — bounded-integer inputs ⇒ finite folds — so the helper's own
/// tests carry the mutant-kill reachability, never dead-code survivors).
pub(crate) fn fold_guard_finite(values: &[f64]) -> bool {
    values.iter().all(|v| v.is_finite())
}

/// One CPython-gh-100425 Neumaier compensation step (the `builtins.sum()`
/// float fast path's loop body, token-for-token from CPython 3.12 SOURCE):
/// running `f_result`, compensation `c`, the `|f_result| >= |x|` branch.
/// Factored per §7.6 so its threshold/branch mutants die in direct tests.
pub(crate) fn neumaier_add(f_result: f64, c: f64, x: f64) -> (f64, f64) {
    let t = f_result + x;
    let c_next = if f_result.abs() >= x.abs() {
        c + ((f_result - t) + x)
    } else {
        c + ((x - t) + f_result)
    };
    (t, c_next)
}

/// The downside Σr² fold — CPython 3.12 `builtins.sum()` over
/// `(r*r for r in returns if r < 0.0)`, ported token-for-token (§4 row 6):
///
/// * empty downside → `0.0` (Python keeps the int `0`; `0 / n` reproduces
///   `0.0` bit-for-bit combine-side, so the f64 zero is value-identical);
/// * the FIRST float item is consumed via the int→float transition
///   (`f_result = 0 + x₀`, `c = 0.0` — `float(0) + x₀ == x₀` bit-exactly for
///   every finite f64, the enumerated-token value-invisibility proof);
/// * every later item runs the Neumaier compensation step;
/// * the GUARDED final add `if (c && isfinite(c)) f_result += c` — never the
///   unconditional form (CPython source, not a paraphrase).
///
/// Strict `r < 0.0` with the identical operator on the identical bit value
/// (−0.0 excluded in both languages — `-0.0 < 0.0` is false in each).
pub(crate) fn downside_accumulate(returns: &[f64]) -> f64 {
    let mut squares = returns.iter().filter(|&&r| r < 0.0).map(|&r| r * r);
    let Some(first) = squares.next() else {
        return 0.0;
    };
    let mut f_result = first;
    let mut c = 0.0f64;
    for x in squares {
        (f_result, c) = neumaier_add(f_result, c, x);
    }
    if c != 0.0 && c.is_finite() {
        f_result += c;
    }
    f_result
}

/// Welford/Pébay first-four-moments fold — the token-for-token port of
/// `brutex.metrics.compute._accumulate_moments` (§4 row 4): exact update
/// expressions, exact operand order, strictly sequential left-to-right;
/// `(n*n − 3*n + 3)` in INTEGER arithmetic then converted (exact for every
/// reachable n — trades ≤ bars ≪ 2^26, so n² ≪ 2^53). Returns
/// `(mean, m2, m3, m4)`; the count is the caller's `n_trades`.
pub(crate) fn accumulate_moments(values: &[f64]) -> (f64, f64, f64, f64) {
    let mut n: i64 = 0;
    let mut mean = 0.0f64;
    let mut m2 = 0.0f64;
    let mut m3 = 0.0f64;
    let mut m4 = 0.0f64;
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

/// Linear-interpolation percentile over a PRE-SORTED series — token-for-token
/// from `brutex.metrics.compute.percentile` SOURCE (§4 row 12), BOTH branches
/// the lerp paraphrase omits enumerated: (a) the single-element early return;
/// (b) the `lower == upper` exact-index return. `sorted_values` is non-empty
/// and NaN-free (the caller's fold precondition); `pct ∈ [0, 100]`.
pub(crate) fn percentile_sorted(sorted_values: &[f64], pct: f64) -> f64 {
    if sorted_values.len() == 1 {
        return sorted_values.first().copied().unwrap_or(0.0);
    }
    let rank = (pct / 100.0) * ((sorted_values.len() - 1) as f64);
    let lower = rank.floor() as usize;
    let upper = rank.ceil() as usize;
    let lo = sorted_values.get(lower).copied().unwrap_or(0.0);
    if lower == upper {
        return lo;
    }
    let hi = sorted_values.get(upper).copied().unwrap_or(0.0);
    let frac = rank - (lower as f64);
    lo * (1.0 - frac) + hi * frac
}

/// Streak fold — the port of `brutex.metrics.compute._consecutive_runs`:
/// a win is `pnl > 0`, a loss is `pnl < 0`, a scratch (`pnl == 0`) breaks
/// both streaks. Returns `(max_consecutive_losses, max_consecutive_wins)`.
pub(crate) fn consecutive_runs(pnls: &[i64]) -> (i64, i64) {
    let mut max_losses: i64 = 0;
    let mut max_wins: i64 = 0;
    let mut cur_losses: i64 = 0;
    let mut cur_wins: i64 = 0;
    for &pnl in pnls {
        if pnl > 0 {
            cur_wins += 1;
            cur_losses = 0;
        } else if pnl < 0 {
            cur_losses += 1;
            cur_wins = 0;
        } else {
            cur_wins = 0;
            cur_losses = 0;
        }
        max_wins = max_wins.max(cur_wins);
        max_losses = max_losses.max(cur_losses);
    }
    (max_losses, max_wins)
}

/// Narrow one i128 fold sum to i64, or signal the §1.3 bit1 demotion.
fn narrow_i64(value: i128) -> Result<i64, ()> {
    i64::try_from(value).map_err(|_| ())
}

/// Fold ONE mask: walk → integer folds → envelope gate → float folds →
/// (SCAN-LAST, §1.3 round-4) the drawdown/ulcer scan. Returns the row plus
/// whether the scan actually EXECUTED (the in-band `scans_run` oracle).
///
/// A bit1 trip (envelope / narrowing / non-finite) short-circuits BEFORE the
/// scan, so a flag-served row NEVER counts a scan and
/// `scans_run == count(plain rows)` holds EXACTLY.
#[allow(clippy::too_many_arguments)]
fn fold_one_mask<W: MaskWord>(
    cols: &SweepColumns,
    bar_bits: &[W],
    mask: W,
    is_buy: bool,
    sl: Option<i64>,
    tp: Option<i64>,
    policy: crate::trade_sim::FillPolicy,
    start_equity_paisa: i64,
    scratch: &mut Vec<i64>,
) -> Result<(FoldRow, bool), String> {
    let trades = simulate_one_core(cols, bar_bits, mask, is_buy, sl, tp, policy);
    if trades.is_empty() {
        // §1.3 MANDATED zero-trade short-circuit: flag + zeroed row, NO
        // drawdown/ulcer scan, NO float folds (the in-crate twin of the
        // pure-Python FLAT shortcut).
        return Ok((FoldRow::zeroed(FOLD_FLAG_ZERO_TRADE), false));
    }

    // ── Integer trade aggregates (exact i128 accumulate; §4 row 1) ──
    let n_trades = trades.len() as i64;
    let mut net_pnl: i128 = 0;
    let mut gross_profit: i128 = 0;
    let mut gross_loss_abs: i128 = 0;
    let mut winners: i64 = 0;
    let mut losers: i64 = 0;
    let mut sum_mfe: i128 = 0;
    let mut sum_mae: i128 = 0;
    let mut sum_holding: i128 = 0;
    let mut max_holding: i64 = 0;
    let mut largest_loss: i64 = 0;
    let mut abs_pnl_sum: i128 = 0;
    let mut pnls: Vec<i64> = Vec::with_capacity(trades.len());
    for &(pnl, mfe, mae, holding, _entry_idx, _exit_idx) in &trades {
        net_pnl += i128::from(pnl);
        sum_mfe += i128::from(mfe);
        sum_mae += i128::from(mae);
        sum_holding += i128::from(holding);
        max_holding = max_holding.max(i64::try_from(holding).unwrap_or(i64::MAX));
        if pnl > 0 {
            winners += 1;
            gross_profit += i128::from(pnl);
        } else if pnl < 0 {
            losers += 1;
            gross_loss_abs += -i128::from(pnl);
            largest_loss = largest_loss.min(pnl);
        }
        abs_pnl_sum += i128::from(pnl).abs();
        pnls.push(pnl);
    }
    let (max_losses, max_wins) = consecutive_runs(&pnls);

    // ── §1.3 bit1 gate #1: the 2^52 equity envelope, CHECKED IN THE FOLD
    // (i128 arithmetic — `start + Σ|pnl|` cannot overflow i128). Outside ⇒
    // python_required: the Python bigint/exact-rational spine serves. ──
    if i128::from(start_equity_paisa) + abs_pnl_sum > i128::from(MAX_EXACT_EQUITY_PAISA) {
        return Ok((FoldRow::zeroed(FOLD_FLAG_PYTHON_REQUIRED), false));
    }
    // ── §1.3 bit1 gate #2: i128→i64 narrowing (structurally satisfied
    // under the envelope; the belt keeps Python bigint semantics by
    // FALLBACK, never emulation). ──
    let narrowed = (|| -> Result<[i64; 15], ()> {
        Ok([
            n_trades,
            narrow_i64(net_pnl)?,
            narrow_i64(gross_profit)?,
            narrow_i64(gross_loss_abs)?,
            winners,
            losers,
            narrow_i64(sum_mfe)?,
            narrow_i64(sum_mae)?,
            narrow_i64(sum_holding)?,
            max_holding,
            largest_loss,
            max_losses,
            max_wins,
            0, // max_dd_paisa — the scan fills it below
            0, // max_dd_bars
        ])
    })();
    let Ok(mut ints) = narrowed else {
        return Ok((FoldRow::zeroed(FOLD_FLAG_PYTHON_REQUIRED), false));
    };

    // ── Float folds (§4 rows 4-6, 12): returns → moments → downside Σr² →
    // stable sort → p95/p5. Inside the envelope both int→f64 conversions
    // are exact, so `pnl / start` is the correctly rounded quotient —
    // CPython's int/int true division bit-for-bit. ──
    let start_f = start_equity_paisa as f64;
    let returns: Vec<f64> = pnls.iter().map(|&pnl| (pnl as f64) / start_f).collect();
    let (mean, m2, m3, m4) = accumulate_moments(&returns);
    let sum_sq_downside = downside_accumulate(&returns);
    let mut sorted_returns = returns;
    // STABLE sort (§4 row 11 — never sort_unstable_by): `partial_cmp` on
    // finite floats ranks −0.0 == +0.0 Equal in both languages ⇒ tie order
    // = input order in both. NaN is structurally excluded (finite integer
    // quotients); the `Equal` fallback is the validated-precondition arm,
    // never an unwrap that could panic under panic="abort".
    sorted_returns.sort_by(|a, b| a.partial_cmp(b).unwrap_or(core::cmp::Ordering::Equal));
    let p95 = percentile_sorted(&sorted_returns, 95.0);
    let p5 = percentile_sorted(&sorted_returns, 5.0);

    // ── §1.3 bit1 gate #3 (defence-in-depth, factored helper): every float
    // fold must be finite BEFORE the scan runs (the SCAN-LAST ordering law —
    // a bit1 trip here still counts NO scan). ──
    if !fold_guard_finite(&[mean, m2, m3, m4, sum_sq_downside, p95, p5]) {
        return Ok((FoldRow::zeroed(FOLD_FLAG_PYTHON_REQUIRED), false));
    }

    // ── SCAN-LAST: the drawdown/ulcer fold — `drawdown_stats_checked` via
    // the scratch seam, consuming the walker's NATIVE exit indices (bar
    // indices by construction; the seam engages only under the per-tuple
    // `bars_strictly_increasing` certificate, §4 row 7). Under the envelope
    // (pre-checked above) the scan cannot refuse; a refusal here is a
    // kernel-defect signal and propagates as a loud Err. ──
    let exits: Vec<(usize, i64)> = trades
        .iter()
        .map(|&(pnl, _mfe, _mae, _holding, _entry_idx, exit_idx)| (exit_idx as usize, pnl))
        .collect();
    let (max_dd_paisa, max_dd_pct, max_dd_bars, ulcer) =
        drawdown_stats_into(bar_bits.len(), start_equity_paisa, &exits, scratch)?;
    if let Some(slot) = ints.get_mut(13) {
        *slot = max_dd_paisa;
    }
    if let Some(slot) = ints.get_mut(14) {
        *slot = max_dd_bars;
    }
    Ok((
        FoldRow {
            ints,
            floats: [
                mean,
                m2,
                m3,
                m4,
                sum_sq_downside,
                p95,
                p5,
                max_dd_pct,
                ulcer,
            ],
            flags: 0,
        },
        true,
    ))
}

/// The pure `fold_block` core (§1.3): one chunk of kept masks → the
/// row-major little-endian columnar buffers + per-mask flags + the in-band
/// `scans_run` count. `Result<_, String>` per the crate convention.
#[allow(clippy::too_many_arguments)]
fn fold_block_impl<W: MaskWord>(
    cols: &SweepColumns,
    bar_bits: &[W],
    masks: &[W],
    is_buy: bool,
    sl_paisa: Option<i64>,
    tp_paisa: Option<i64>,
    sl_next_open: bool,
    tp_next_open: bool,
    tp_first: bool,
    start_equity_paisa: i64,
) -> Result<(u64, Vec<u8>, Vec<u8>, Vec<u8>), String> {
    // Boundary validation FIRST (the R2-RC-F1 idiom): the SAME SL/TP
    // distance law the batch trade-sim entries run, plus the fold's own
    // start-equity positivity (the drawdown core would refuse it later —
    // refusing up front keeps the refusal whole-chunk + pre-walk).
    check_level_distance_for("fold_block", "sl_paisa", sl_paisa)?;
    check_level_distance_for("fold_block", "tp_paisa", tp_paisa)?;
    if start_equity_paisa <= 0 {
        return Err(format!(
            "fold_block: start_equity_paisa must be > 0, got {start_equity_paisa}"
        ));
    }
    // §3 round-6 in-core fold work ceiling — a whole-chunk loud refusal
    // (`fold_work_ceiling`), never a partial fold and never an unbounded
    // GIL hold. Production-unreachable by construction (the driver sizes
    // chunks ≤ 2^24 cells); one multiplication in u128, overflow-free.
    let work_cells = (masks.len() as u128) * (bar_bits.len() as u128);
    if work_cells > FOLD_WORK_CEILING_CELLS {
        return Err(format!(
            "fold_block: fold_work_ceiling — {} masks × {} bars = {work_cells} cells \
             exceeds the in-core ceiling {FOLD_WORK_CEILING_CELLS}",
            masks.len(),
            bar_bits.len(),
        ));
    }
    let policy = policy_from_flags(sl_next_open, tp_next_open, tp_first);
    let mut ints_le: Vec<u8> = Vec::with_capacity(masks.len() * 15 * 8);
    let mut floats_le: Vec<u8> = Vec::with_capacity(masks.len() * 9 * 8);
    let mut flags: Vec<u8> = Vec::with_capacity(masks.len());
    let mut scans_run: u64 = 0;
    // ONE scratch buffer per block, fully re-zeroed per mask inside the
    // seam — allocation-only reuse, values identical to fresh buffers.
    let mut scratch: Vec<i64> = Vec::new();
    for &mask in masks {
        let (row, scanned) = fold_one_mask(
            cols,
            bar_bits,
            mask,
            is_buy,
            sl_paisa,
            tp_paisa,
            policy,
            start_equity_paisa,
            &mut scratch,
        )?;
        if scanned {
            scans_run = scans_run.saturating_add(1);
        }
        for value in row.ints {
            ints_le.extend_from_slice(&value.to_le_bytes());
        }
        for value in row.floats {
            floats_le.extend_from_slice(&value.to_le_bytes());
        }
        flags.push(row.flags);
    }
    Ok((scans_run, ints_le, floats_le, flags))
}

/// Project one pure fold-block result to the §1.3 FFI 5-tuple.
fn fold_block_ffi(
    py: Python<'_>,
    result: (u64, Vec<u8>, Vec<u8>, Vec<u8>),
) -> (u32, u64, Py<PyBytes>, Py<PyBytes>, Py<PyBytes>) {
    let (scans_run, ints_le, floats_le, flags) = result;
    (
        SWEEP_CORE_ABI_VERSION,
        scans_run,
        PyBytes::new(py, &ints_le).into(),
        PyBytes::new(py, &floats_le).into(),
        PyBytes::new(py, &flags).into(),
    )
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
    /// Consumed by the Slice-2 `fold_block` walk (the frozen §1.3
    /// constructor contract took the full 7 columns from Slice 1).
    columns: SweepColumns,
    bar_bits: Vec<u64>,
}

/// Resident sweep core over a u128 `bar_bits` window — the [64, 128)-bit
/// headroom twin (§1.3; separately monomorphised, same pure core).
#[pyclass(frozen, module = "fno_math")]
pub struct ResidentSweepCoreU128 {
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

    /// Fold one chunk of kept masks -> the §1.3 columnar 5-tuple
    /// `(SWEEP_CORE_ABI_VERSION, scans_run, ints_le, floats_le, flags)`.
    ///
    /// `ints_le` is row-major i64-LE (stride 15 = the exported
    /// `SWEEP_CORE_FOLD_INT_FIELDS`), `floats_le` row-major f64-LE (stride
    /// 9 = `SWEEP_CORE_FOLD_FLOAT_FIELDS`), `flags` one byte per mask
    /// (bit0 zero_trade, bit1 python_required, bits 2-7 reserved zero).
    /// Trades NEVER cross this boundary (§9.1 acceptance criterion 2).
    #[pyo3(signature = (masks, is_buy, sl_paisa, tp_paisa, sl_next_open, tp_next_open, tp_first, start_equity_paisa))]
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
        fold_block_impl(
            &self.columns,
            &self.bar_bits,
            &masks,
            is_buy,
            sl_paisa,
            tp_paisa,
            sl_next_open,
            tp_next_open,
            tp_first,
            start_equity_paisa,
        )
        .map(|result| fold_block_ffi(py, result))
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

    /// Fold one chunk of kept u128 masks (same contract as the u64 twin).
    #[pyo3(signature = (masks, is_buy, sl_paisa, tp_paisa, sl_next_open, tp_next_open, tp_first, start_equity_paisa))]
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
        fold_block_impl(
            &self.columns,
            &self.bar_bits,
            &masks,
            is_buy,
            sl_paisa,
            tp_paisa,
            sl_next_open,
            tp_next_open,
            tp_first,
            start_equity_paisa,
        )
        .map(|result| fold_block_ffi(py, result))
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

    // ── Slice 2: fold_block in-crate tests ──────────────────────────────

    /// Tiny 6-bar fixture window: bit 0 fires on bars 0+3 (two round-trip
    /// trades under next-open entry), bit 1 never fires (zero-trade mask).
    fn fold_fixture() -> (SweepColumns, Vec<u64>) {
        let open = vec![100_00, 101_00, 102_00, 103_00, 99_00, 98_00];
        let high = vec![101_00, 103_00, 102_50, 103_50, 100_00, 99_00];
        let low = vec![99_00, 100_00, 101_00, 98_00, 97_00, 97_50];
        let close = vec![100_50, 102_00, 101_50, 99_50, 98_50, 98_00];
        let is_forced = vec![false; 6];
        let session_idx = vec![738_000i64; 6];
        let cols = build_columns(
            "ResidentSweepCore",
            open,
            high,
            low,
            close,
            is_forced,
            session_idx,
            6,
        )
        .expect("fixture builds");
        // bar_bits: bit0 on bars 0 and 3; bit1 nowhere; bit2 everywhere.
        let bar_bits = vec![0b101u64, 0b100, 0b100, 0b101, 0b100, 0b100];
        (cols, bar_bits)
    }

    fn fold_row(ints_le: &[u8], floats_le: &[u8], row: usize) -> ([i64; 15], [f64; 9]) {
        let mut ints = [0i64; 15];
        let mut floats = [0.0f64; 9];
        for (i, slot) in ints.iter_mut().enumerate() {
            let at = (row * 15 + i) * 8;
            let mut b = [0u8; 8];
            b.copy_from_slice(&ints_le[at..at + 8]);
            *slot = i64::from_le_bytes(b);
        }
        for (i, slot) in floats.iter_mut().enumerate() {
            let at = (row * 9 + i) * 8;
            let mut b = [0u8; 8];
            b.copy_from_slice(&floats_le[at..at + 8]);
            *slot = f64::from_le_bytes(b);
        }
        (ints, floats)
    }

    #[test]
    fn fold_layout_constants_are_frozen() {
        assert_eq!(SWEEP_CORE_FOLD_INT_FIELDS.len(), 15);
        assert_eq!(SWEEP_CORE_FOLD_FLOAT_FIELDS.len(), 9);
        assert_eq!(SWEEP_CORE_FOLD_INT_FIELDS[0], "n_trades");
        assert_eq!(SWEEP_CORE_FOLD_INT_FIELDS[13], "max_dd_paisa");
        assert_eq!(SWEEP_CORE_FOLD_INT_FIELDS[14], "max_dd_bars");
        assert_eq!(SWEEP_CORE_FOLD_FLOAT_FIELDS[0], "mean");
        assert_eq!(SWEEP_CORE_FOLD_FLOAT_FIELDS[4], "sum_sq_downside");
        assert_eq!(SWEEP_CORE_FOLD_FLOAT_FIELDS[7], "max_dd_pct");
        assert_eq!(SWEEP_CORE_FOLD_FLOAT_FIELDS[8], "ulcer");
        assert_eq!(FOLD_FLAG_ZERO_TRADE, 1);
        assert_eq!(FOLD_FLAG_PYTHON_REQUIRED, 2);
    }

    #[test]
    fn scans_run_zero_for_all_zero_trade_chunk() {
        // §1.3 zero-trade short-circuit law, the BYTE-OBSERVABLE oracle:
        // an all-zero-trade chunk executes ZERO drawdown/ulcer scans.
        let (cols, bar_bits) = fold_fixture();
        let (scans, ints, floats, flags) = fold_block_impl::<u64>(
            &cols,
            &bar_bits,
            &[0b10, 0b10, 0b10],
            true,
            None,
            None,
            false,
            false,
            false,
            1_000_000,
        )
        .expect("in ceiling");
        assert_eq!(scans, 0);
        assert_eq!(flags, vec![FOLD_FLAG_ZERO_TRADE; 3]);
        let (i0, f0) = fold_row(&ints, &floats, 0);
        assert_eq!(i0, [0i64; 15]);
        assert!(f0.iter().all(|v| v.to_bits() == 0.0f64.to_bits()));
    }

    #[test]
    fn scans_run_counts_exactly_the_plain_rows() {
        // Mixed chunk: mask 0b1 trades (plain row, ONE scan), mask 0b10 is
        // zero-trade (no scan) — scans_run == n_plain_rows exactly.
        let (cols, bar_bits) = fold_fixture();
        let (scans, ints, _floats, flags) = fold_block_impl::<u64>(
            &cols,
            &bar_bits,
            &[0b1, 0b10, 0b1],
            true,
            None,
            None,
            false,
            false,
            false,
            1_000_000,
        )
        .expect("in ceiling");
        assert_eq!(scans, 2);
        assert_eq!(flags, vec![0, FOLD_FLAG_ZERO_TRADE, 0]);
        let (i0, _) = fold_row(&ints, &_floats, 0);
        assert!(i0[0] >= 1, "the trading mask must fold >= 1 trade");
    }

    #[test]
    fn envelope_tripped_row_counts_no_scan_and_flags_python_required() {
        // §1.3 SCAN-LAST ordering: a bit1 (envelope) trip short-circuits
        // BEFORE the scan — the flag-served row counts NO scan.
        let (cols, bar_bits) = fold_fixture();
        // start_equity so large that start + Σ|pnl| > 2^52.
        let start = crate::metrics::MAX_EXACT_EQUITY_PAISA;
        let (scans, ints, floats, flags) = fold_block_impl::<u64>(
            &cols,
            &bar_bits,
            &[0b1],
            true,
            None,
            None,
            false,
            false,
            false,
            start,
        )
        .expect("in ceiling");
        assert_eq!(scans, 0);
        assert_eq!(flags, vec![FOLD_FLAG_PYTHON_REQUIRED]);
        let (i0, f0) = fold_row(&ints, &floats, 0);
        assert_eq!(i0, [0i64; 15]);
        assert!(f0.iter().all(|v| v.to_bits() == 0.0f64.to_bits()));
    }

    #[test]
    fn envelope_boundary_exactly_at_2_52_is_plain_one_more_demotes() {
        // start + Σ|pnl| == 2^52 exactly is INSIDE the envelope (the strict
        // `>` law mirroring `exceeds_exact_equity_envelope`); +1 demotes.
        let (cols, bar_bits) = fold_fixture();
        let trades = simulate_one_core::<u64>(
            &cols,
            &bar_bits,
            0b1,
            true,
            None,
            None,
            policy_from_flags(false, false, false),
        );
        let abs_sum: i64 = trades.iter().map(|t| t.0.abs()).sum();
        let at_boundary = crate::metrics::MAX_EXACT_EQUITY_PAISA - abs_sum;
        let (scans, _ints, _floats, flags) = fold_block_impl::<u64>(
            &cols,
            &bar_bits,
            &[0b1],
            true,
            None,
            None,
            false,
            false,
            false,
            at_boundary,
        )
        .expect("ok");
        assert_eq!((scans, flags), (1, vec![0]));
        let (scans, _ints, _floats, flags) = fold_block_impl::<u64>(
            &cols,
            &bar_bits,
            &[0b1],
            true,
            None,
            None,
            false,
            false,
            false,
            at_boundary + 1,
        )
        .expect("ok");
        assert_eq!((scans, flags), (0, vec![FOLD_FLAG_PYTHON_REQUIRED]));
    }

    #[test]
    fn fold_work_ceiling_refuses_whole_chunk() {
        let (cols, bar_bits) = fold_fixture();
        // 6 bars ⇒ a chunk of ceiling/6 + 1 masks crosses the ceiling.
        let n = (FOLD_WORK_CEILING_CELLS / 6 + 1) as usize;
        let masks = vec![0b10u64; n];
        let err = fold_block_impl::<u64>(
            &cols, &bar_bits, &masks, true, None, None, false, false, false, 1_000_000,
        )
        .unwrap_err();
        assert!(err.contains("fold_work_ceiling"), "{err}");
    }

    #[test]
    fn fold_rejects_nonpositive_start_equity_and_bad_levels() {
        let (cols, bar_bits) = fold_fixture();
        let err = fold_block_impl::<u64>(
            &cols,
            &bar_bits,
            &[0b1],
            true,
            None,
            None,
            false,
            false,
            false,
            0,
        )
        .unwrap_err();
        assert!(err.contains("start_equity_paisa must be > 0"), "{err}");
        let err = fold_block_impl::<u64>(
            &cols,
            &bar_bits,
            &[0b1],
            true,
            Some(-1),
            None,
            false,
            false,
            false,
            1_000,
        )
        .unwrap_err();
        assert!(err.contains("sl_paisa"), "{err}");
    }

    #[test]
    fn fold_matches_reference_fold_shape() {
        // Fold vs a deliberately DIFFERENT-shaped in-crate reference fold
        // (two-pass, non-streaming): integer aggregates + streaks + the
        // drawdown ints must agree exactly.
        let (cols, bar_bits) = fold_fixture();
        let trades = simulate_one_core::<u64>(
            &cols,
            &bar_bits,
            0b1,
            true,
            None,
            None,
            policy_from_flags(false, false, false),
        );
        assert!(!trades.is_empty());
        let (_, ints, floats, flags) = fold_block_impl::<u64>(
            &cols,
            &bar_bits,
            &[0b1],
            true,
            None,
            None,
            false,
            false,
            false,
            1_000_000,
        )
        .expect("ok");
        assert_eq!(flags, vec![0]);
        let (i, f) = fold_row(&ints, &floats, 0);
        // Reference: naive two-pass.
        let n = trades.len() as i64;
        let net: i64 = trades.iter().map(|t| t.0).sum();
        let gp: i64 = trades.iter().map(|t| t.0.max(0)).sum();
        let gl: i64 = trades.iter().map(|t| (-t.0).max(0)).sum();
        let winners = trades.iter().filter(|t| t.0 > 0).count() as i64;
        let losers = trades.iter().filter(|t| t.0 < 0).count() as i64;
        let sum_mfe: i64 = trades.iter().map(|t| t.1).sum();
        let sum_mae: i64 = trades.iter().map(|t| t.2).sum();
        let sum_holding: i64 = trades.iter().map(|t| t.3 as i64).sum();
        let max_holding: i64 = trades.iter().map(|t| t.3 as i64).max().unwrap_or(0);
        let largest_loss: i64 = trades.iter().map(|t| t.0).min().unwrap_or(0).min(0);
        assert_eq!(
            &i[..11],
            &[
                n,
                net,
                gp,
                gl,
                winners,
                losers,
                sum_mfe,
                sum_mae,
                sum_holding,
                max_holding,
                largest_loss
            ][..]
        );
        // Drawdown ints against the allocating metrics core directly.
        let exits: Vec<(usize, i64)> = trades.iter().map(|t| (t.5 as usize, t.0)).collect();
        let dd = crate::metrics::drawdown_stats_checked(6, 1_000_000, &exits).expect("ok");
        assert_eq!(i[13], dd.0);
        assert_eq!(i[14], dd.2);
        assert_eq!(f[7].to_bits(), dd.1.to_bits());
        assert_eq!(f[8].to_bits(), dd.3.to_bits());
    }

    // ── fold_guard_finite (direct reachability — §1.3 defence-in-depth) ──

    #[test]
    fn fold_guard_finite_accepts_finite_and_rejects_each_nonfinite() {
        assert!(fold_guard_finite(&[0.0, -0.0, 1.5, -1e300]));
        assert!(!fold_guard_finite(&[0.0, f64::NAN]));
        assert!(!fold_guard_finite(&[f64::INFINITY]));
        assert!(!fold_guard_finite(&[f64::NEG_INFINITY, 1.0]));
        assert!(fold_guard_finite(&[]));
    }

    // ── Neumaier / downside Σr² (CPython-baked vectors, §4 row 6) ──────

    #[test]
    fn neumaier_add_branches_are_exact() {
        // |f| >= |x| branch: compensation captures the lost low bits.
        let (f, c) = neumaier_add(1e16, 0.0, 1.0);
        assert_eq!(f.to_bits(), (1e16f64 + 1.0).to_bits());
        assert_eq!(c.to_bits(), ((1e16 - (1e16f64 + 1.0)) + 1.0).to_bits());
        // |f| < |x| branch.
        let (f2, c2) = neumaier_add(1.0, 0.0, 1e16);
        assert_eq!(f2.to_bits(), (1.0f64 + 1e16).to_bits());
        assert_eq!(c2.to_bits(), ((1e16 - (1.0f64 + 1e16)) + 1.0).to_bits());
    }

    #[test]
    fn downside_accumulate_matches_cpython_sum_vectors() {
        // CPython 3.12 `sum(r*r for r in downside)` baked bit patterns
        // (generated 2026-07-10 on the pinned interpreter — see the Python
        // twin test for the generation harness).
        let va = [
            0.01, -0.02, 0.03, -0.0, 0.0, -0.5, 0.25, 1e-300, -1e-300, 0.125,
        ];
        assert_eq!(downside_accumulate(&va).to_bits(), 0x3fd0068db8bac711);
        let vb = [1e16, 1.0, -1e16, 3.0, 1e-8, -2.0];
        assert_eq!(downside_accumulate(&vb).to_bits(), 0x4693b8b5b5056e17);
        let vc = [-1.0, -1e-8, -3.0, -1e16, -1.0];
        assert_eq!(downside_accumulate(&vc).to_bits(), 0x4693b8b5b5056e17);
        // Empty downside → 0.0 (the int-0 arm's float twin, +0.0).
        assert_eq!(
            downside_accumulate(&[0.5, 0.0, -0.0]).to_bits(),
            0.0f64.to_bits()
        );
    }

    // ── Welford/Pébay moments (CPython-baked vectors, §4 row 4) ────────

    #[test]
    fn accumulate_moments_matches_cpython_vectors() {
        let va = [
            0.01, -0.02, 0.03, -0.0, 0.0, -0.5, 0.25, 1e-300, -1e-300, 0.125,
        ];
        let (mean, m2, m3, m4) = accumulate_moments(&va);
        assert_eq!(mean.to_bits(), 0xbf85810624dd2f1a);
        assert_eq!(m2.to_bits(), 0x3fd504dfce3150db);
        assert_eq!(m3.to_bits(), 0xbfb8d7f0c24a67da);
        assert_eq!(m4.to_bits(), 0x3fafed66e517967e);
        let vb = [1e16, 1.0, -1e16, 3.0, 1e-8, -2.0];
        let (mean, m2, m3, m4) = accumulate_moments(&vb);
        assert_eq!(mean.to_bits(), 0x3fc5555558e997d2);
        assert_eq!(m2.to_bits(), 0x46a3b8b5b5056e17);
        assert_eq!(m3.to_bits(), 0xc693b8b5b8547688);
        assert_eq!(m4.to_bits(), 0x4d484f03e93ff9f5);
        // Empty series → all zeros (the Python n == 0 arm).
        let (mean, m2, m3, m4) = accumulate_moments(&[]);
        assert_eq!(
            [mean.to_bits(), m2.to_bits(), m3.to_bits(), m4.to_bits()],
            [0u64; 4]
        );
    }

    // ── percentile (CPython-baked vectors, §4 row 12 — both branches) ──

    #[test]
    fn percentile_matches_cpython_vectors() {
        let mut sv = [
            0.01f64, -0.02, 0.03, -0.0, 0.0, -0.5, 0.25, 1e-300, -1e-300, 0.125,
        ];
        sv.sort_by(|a, b| a.partial_cmp(b).unwrap_or(core::cmp::Ordering::Equal));
        assert_eq!(percentile_sorted(&sv, 95.0).to_bits(), 0x3fc8ccccccccccc8);
        assert_eq!(percentile_sorted(&sv, 5.0).to_bits(), 0xbfd22d0e56041894);
        let sv4 = [-2.0, -1.0, 1.0, 4.0];
        assert_eq!(percentile_sorted(&sv4, 95.0).to_bits(), 0x400c666666666664);
        assert_eq!(percentile_sorted(&sv4, 5.0).to_bits(), 0xbffd99999999999a);
        // lower == upper exact-index branch (rank 1.5 → no; 50% of 4 →
        // rank 1.5 lerp; use 100%/0% for the exact-index arm instead).
        assert_eq!(percentile_sorted(&sv4, 50.0).to_bits(), 0x0);
        assert_eq!(percentile_sorted(&sv4, 100.0).to_bits(), 4.0f64.to_bits());
        assert_eq!(percentile_sorted(&sv4, 0.0).to_bits(), (-2.0f64).to_bits());
        // single-element early return preserves a −0.0 endpoint bit-exactly.
        assert_eq!(
            percentile_sorted(&[-0.0], 95.0).to_bits(),
            0x8000000000000000
        );
    }

    // ── streaks ────────────────────────────────────────────────────────

    #[test]
    fn consecutive_runs_matches_python_semantics() {
        // scratch (0) breaks BOTH streaks.
        assert_eq!(consecutive_runs(&[1, 1, -1, -1, -1, 0, 1]), (3, 2));
        assert_eq!(consecutive_runs(&[]), (0, 0));
        assert_eq!(consecutive_runs(&[0, 0]), (0, 0));
        assert_eq!(consecutive_runs(&[-5]), (1, 0));
    }

    // ── u128 twin serves the same fold bytes on u64-fit inputs ─────────

    #[test]
    fn fold_u128_twin_matches_u64_on_u64_fit_inputs() {
        let (cols, bar_bits) = fold_fixture();
        let bar_bits_wide: Vec<u128> = bar_bits.iter().map(|&b| u128::from(b)).collect();
        let out64 = fold_block_impl::<u64>(
            &cols,
            &bar_bits,
            &[0b1, 0b10],
            true,
            Some(500),
            Some(500),
            false,
            false,
            false,
            1_000_000,
        )
        .expect("ok");
        let cols2 = fold_fixture().0;
        let out128 = fold_block_impl::<u128>(
            &cols2,
            &bar_bits_wide,
            &[0b1, 0b10],
            true,
            Some(500),
            Some(500),
            false,
            false,
            false,
            1_000_000,
        )
        .expect("ok");
        assert_eq!(out64, out128);
    }
}
