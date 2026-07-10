//! Apriori / anti-monotone level-wise PRUNED sweep-candidate frontier kernel.
//!
//! DEC-SWEEP-APRIORI-RUST-001 (Slice-1b of the `rust/fno-math` sweep-core
//! conversion). A native-Rust port of the FROZEN Python Apriori generator
//! `brutex.engine.sweep_apriori.generate_apriori_candidates`
//! (DEC-SWEEP-APRIORI-TUPLE-001, BruteX PR #1393 head 26093a5b, MIGRATION
//! #1680). Its job is to be **bit-for-bit identical** to that oracle — same
//! survivor set, same emission order, same per-level breaker trip decision +
//! trip count — for every input inside the u64 envelope.
//!
//! A SEPARATE source file from Slice-1's `enumerate_prefilter` kernel
//! (`sweep_core.rs`) so the two slices never collide in one file while they
//! are authored concurrently; both register under the `_fno_math` PyO3 module
//! via `lib.rs` (the design's registered sibling-module deviation).
//!
//! ## What the Python seam does vs what this kernel does
//!
//! The Python seam (`brutex/engine/sweep_apriori_rust.py`) owns everything
//! whose EXACT message / type is frozen-pinned: input validation
//! (negative-bit / `>= 64`-bit-unsupported / `k`-range / `root_mod` / `n_bars`
//! / `max_mask_bars` guards), the `SweepAprioriBudgetError` / `ValueError`
//! message construction (reusing the frozen module's helpers), and the
//! per-level `sweep.apriori.census` structlog emission. This kernel receives
//! ALREADY-VALIDATED inputs (sorted-unique bits, all `< 64`; `k` in range;
//! `big_w >= 1`, `0 <= w < big_w`; `n_bars >= 0`; `limit > 0`) and returns a
//! discriminated outcome — the pruned shard-owned masks, the per-level census
//! records for the seam to emit, and an OPTIONAL budget-trip count that the
//! seam turns into the frozen `SweepAprioriBudgetError`.
//!
//! ## The five bit-identity requirements (each port-pinned in `#[cfg(test)]`)
//!
//! 1. **Breaker** — trip iff `|C_j| * n_bars > max_mask_bars` (STRICT `>`,
//!    pure integer) on the WHOLE RAW per-level candidate count: at the `k == 1`
//!    TARGET level, every intermediate level `2 <= j <= k-1`, and the FINAL
//!    level `k`; for `k >= 2` the level-1 singleton frontier `|C_1|` is
//!    DELIBERATELY unchecked (`|C_1| = n_unique <= 60`, trivially bounded). The
//!    incremental strided check (`budget_check_stride`) is a decision-preserving
//!    memory optimisation that fires the SAME check on the running count so a
//!    giant list is never fully materialised — replicated here so the reported
//!    trip COUNT matches the Python oracle exactly (a strided fire reports the
//!    running multiple-of-stride count, a post-join fire reports `|C_j|`).
//! 2. **Emission order** — candidates are an in-order ascending-lex subsequence
//!    of `iter_masks_k_of_bits` (position-lex, NOT colex): the join output is
//!    sorted lexicographically on the ascending bit-position tuple.
//! 3. **Survival** — `F_1` by `hits >= min_hits` (`>=`, boundary inclusive);
//!    join on the shared `j-2` prefix; subset-prune all `(j-1)`-subsets;
//!    conflict-prune at `j == 2` only (`CONFLICT_PAIRS` injected as data);
//!    hit-filter per level; `root_mod = (w, big_w)` shard = lowest-set-bit
//!    `% big_w == w`.
//! 4. **Parity invariant** — the shard union reproduces the whole set and the
//!    shards are pairwise disjoint; hits are native superset counts
//!    (`(bar & mask) == mask`), bit-identical to the Python `count_supermasks`
//!    the frozen default counter uses.
//! 5. **Honest envelope** — the trip product is computed in `u128` (exact for
//!    any two `u64` operands — it can never overflow or saturate), so the
//!    breaker comparison is bit-identical to Python's arbitrary-precision
//!    `count * n_bars > limit` across the WHOLE u64 domain INCLUDING
//!    `limit == u64::MAX` (the former `saturating_mul` clamped an overflowing
//!    product to `u64::MAX`, and `u64::MAX > u64::MAX` is false — a fail-OPEN
//!    parity break at that one legal limit value, fixed round 1); the seam
//!    falls back to the Python oracle when `max_mask_bars`/`n_bars` exceed
//!    `u64::MAX` (or any other FFI operand overflows its converted type), so
//!    parity is exact inside the u64 envelope and honestly delegated outside.

use std::collections::HashMap;

/// A single Apriori-level census record for the seam to emit as one
/// `sweep.apriori.census` structlog event: `(level, candidates, frequent)`
/// where `frequent == -1` encodes the omitted-frequent case (the `k == 1`
/// target level and the final level `k`, whose Stage-1B is the caller's).
pub type CensusRecord = (u8, u64, i64);

/// The discriminated result of one Apriori frontier build.
///
/// * `masks` — the pruned, shard-owned level-`k` masks in lexicographic-by-
///   position order (empty on a budget trip).
/// * `census` — the per-level census records the seam emits (only those levels
///   whose census was reached BEFORE any trip — the Python oracle raises before
///   emitting the tripping level's census, and never emits a level whose budget
///   check is placed before its census).
/// * `budget_trip` — `Some(count)` iff the breaker tripped, carrying the exact
///   candidate count the frozen `SweepAprioriBudgetError` message reports (the
///   running strided count or the settled `|C_j|`); `None` on success.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AprioriOutcome {
    /// Pruned shard-owned level-`k` masks, lex-by-position order.
    pub masks: Vec<u64>,
    /// Per-level census records for the seam to emit (pre-trip levels only).
    pub census: Vec<CensusRecord>,
    /// `Some(count)` iff the breaker tripped; `None` on success.
    pub budget_trip: Option<u64>,
}

/// Sentinel in a [`CensusRecord`]'s frequent field meaning "omitted" (the
/// target level's frequent is the caller's Stage-1B authority, never emitted
/// by the generator).
const CENSUS_FREQUENT_OMITTED: i64 = -1;

/// The single `uint64` limb width — the maximum (exclusive) condition-bit
/// index the sorted-array frontier can represent. Mirrors the frozen module's
/// `_U64_BITS`.
const U64_BITS: u32 = 64;

/// The pair level. Conflict-pruning is applied ONLY when building level-2
/// candidates; every higher level inherits conflict-freeness by subset-prune
/// over the conflict-free lower frontier. Mirrors `_PAIR_LEVEL`.
const PAIR_LEVEL: u32 = 2;

/// Pack a single ascending bit-position into its `u64` mask, panic-free.
///
/// The frozen path guarantees every position is `< 64` (the seam raises the
/// `>= 64` u128 refusal), so the `else 0` arm is a dead DEFENSIVE guard that
/// keeps the kernel panic-free (a `1u64 << 64` would be an arithmetic-overflow
/// panic ⇒ SIGABRT under `panic = "abort"`).
#[inline]
fn bit_mask(pos: u32) -> u64 {
    if pos < U64_BITS { 1u64 << pos } else { 0 }
}

/// Pack an ascending bit-position slice into its integer mask (`_mask_of`).
#[inline]
fn mask_of(positions: &[u32]) -> u64 {
    positions.iter().fold(0u64, |acc, &p| acc | bit_mask(p))
}

/// Count how many bars are a SUPERSET of `mask` (`(bar & mask) == mask`).
///
/// The single-mask superset kernel — bit-identical to the shipped
/// `count_supermask_u64s` / `count_one` and therefore to the Python
/// `count_supermasks` the frozen default counter uses. Branchless linear scan.
#[inline]
fn count_superset(bars: &[u64], mask: u64) -> u64 {
    bars.iter()
        .map(|&bar| u64::from((bar & mask) == mask))
        .sum()
}

/// Whether `mask` contains any injected conflict pair (`has_conflict`).
///
/// Each entry of `conflict_pair_masks` is a pre-packed `(1 << a) | (1 << b)`
/// two-bit mask; `mask` conflicts iff it is a superset of any of them.
#[inline]
// clippy::manual_contains misfires here: the "needle" `(mask & pair)` depends on
// the element `pair`, so `.contains(&(mask & pair))` would be a DIFFERENT
// predicate. This is the superset test (all of `pair`'s bits set in `mask`).
#[allow(clippy::manual_contains)]
fn has_conflict(mask: u64, conflict_pair_masks: &[u64]) -> bool {
    conflict_pair_masks
        .iter()
        .any(|&pair| (mask & pair) == pair)
}

/// Whether `value` is present in the ascending `frontier_masks` array
/// (`_in_sorted`) — `O(log n)` binary search, no set.
#[inline]
fn in_sorted(frontier_masks: &[u64], value: u64) -> bool {
    frontier_masks.binary_search(&value).is_ok()
}

/// Whether every `(len-1)`-subset mask of `candidate` is frequent
/// (`_all_subsets_frequent`). Drops each position in turn, packs the remaining
/// ascending positions, and checks membership in the sorted frequent frontier.
fn all_subsets_frequent(candidate: &[u32], frontier_masks: &[u64]) -> bool {
    // Reused per-candidate scratch would need &mut plumbing; the candidates
    // are tiny (k <= 60, in practice <= a handful), so a fresh subset Vec per
    // drop is negligible and keeps the function pure + panic-free.
    let len = candidate.len();
    (0..len).all(|drop| {
        let mut subset: Vec<u32> = Vec::with_capacity(len.saturating_sub(1));
        for (idx, &pos) in candidate.iter().enumerate() {
            if idx != drop {
                subset.push(pos);
            }
        }
        in_sorted(frontier_masks, mask_of(&subset))
    })
}

/// The pure-integer breaker predicate — STRICT `>` on `count * n_bars`.
///
/// The product is computed in `u128`, which is EXACT for any two `u64`
/// operands (a u64×u64 product needs at most 128 bits — it can never
/// overflow or saturate), so this is panic-free under
/// `overflow-checks = true` + `panic = "abort"` AND bit-identical to
/// Python's arbitrary-precision `count * n_bars > limit` for every `u64`
/// limit — including `limit == u64::MAX`, where the former `saturating_mul`
/// clamped an overflowing product to `u64::MAX` and FAILED to trip
/// (`u64::MAX > u64::MAX` is false) while the frozen oracle trips.
#[inline]
fn over_budget(count: u64, n_bars: u64, limit: u64) -> bool {
    (count as u128) * (n_bars as u128) > limit as u128
}

/// Classic Apriori join of the `(j-1)`-frontier into pruned `j`-candidates
/// (`_join_and_prune`), REPLICATING the incremental strided budget check.
///
/// Buckets the lex-sorted frequent frontier by its first `j-2` positions, joins
/// every ordered pair within a bucket into `prefix + (A_last, B_last)`, keeps a
/// candidate iff ALL its `(j-1)`-subsets are frequent, and (when
/// `drop_conflicts`, the `j == 2` level only) drops a candidate carrying a
/// conflict pair. During accumulation, whenever the running candidate count is
/// a positive multiple of `stride`, the SAME budget check the post-join
/// authoritative check applies is fired on the running count — so a giant list
/// trips (and reports its running count) before it is fully materialised,
/// bit-identically to the frozen oracle.
///
/// Returns `Ok(sorted_candidates)` or `Err(running_trip_count)` on a strided
/// trip. The bucket/pair accumulation order is irrelevant to the trip count
/// (which depends only on the running LENGTH: the reported count is the
/// smallest multiple of `stride` whose product exceeds the budget — a pure
/// function of the settled total, order-invariant), so it need not match
/// Python's dict-insertion order; the final list is sorted lexicographically
/// before return, matching Python's `candidates.sort()`.
fn join_and_prune(
    frontier: &[Vec<u32>],
    frontier_masks: &[u64],
    drop_conflicts: bool,
    conflict_pair_masks: &[u64],
    n_bars: u64,
    limit: u64,
    stride: u64,
) -> Result<Vec<Vec<u32>>, u64> {
    // Bucket by the shared (j-2)-prefix via a HASH map — the exact shape of
    // the frozen Python `buckets.setdefault(tpl[:-1], []).append(tpl[-1])`
    // dict build, O(|frontier|) amortized. (Round-1 hostile-review fix: the
    // original `prefixes.iter().position(...)` linear scan was
    // O(|frontier| × #distinct-prefixes) — measured SLOWER than the frozen
    // Python oracle at a dense k=6 frontier. Bucket iteration order is
    // irrelevant to every observable output — see the docstring — so the
    // map's nondeterministic order is safe.)
    let mut buckets: HashMap<Vec<u32>, Vec<u32>> = HashMap::new();
    for tpl in frontier {
        let split = tpl.len().saturating_sub(1);
        let (prefix, last) = tpl.split_at(split);
        // `last` is the single trailing position (frontier tuples are length
        // `j-1 >= 1` here); guard the empty case defensively (unreachable at
        // j >= 2).
        let Some(&last_pos) = last.first() else {
            continue;
        };
        buckets.entry(prefix.to_vec()).or_default().push(last_pos);
    }

    let mut candidates: Vec<Vec<u32>> = Vec::new();
    for (prefix, lasts) in &buckets {
        let m = lasts.len();
        for i in 0..m {
            for jj in (i + 1)..m {
                let (Some(&a), Some(&b)) = (lasts.get(i), lasts.get(jj)) else {
                    continue;
                };
                let mut candidate: Vec<u32> = Vec::with_capacity(prefix.len() + 2);
                candidate.extend_from_slice(prefix);
                candidate.push(a);
                candidate.push(b);
                if !all_subsets_frequent(&candidate, frontier_masks) {
                    continue;
                }
                if drop_conflicts && has_conflict(mask_of(&candidate), conflict_pair_masks) {
                    continue;
                }
                candidates.push(candidate);
                // Incremental strided budget check on the RUNNING count — a
                // strided fire is a strictly-earlier raise of the identical
                // authoritative decision (stride-invariant trip DECISION;
                // the reported COUNT matches the Python running count).
                // stride == 0 is a DELIBERATE asymmetry vs the frozen oracle:
                // the kernel skips the strided check (the authoritative
                // post-join check still fires) while Python's `% 0` raises
                // ZeroDivisionError. `_BUDGET_CHECK_STRIDE` is a Final with
                // no env knob — only a test monkeypatch can produce 0, so the
                // divergence is production-unreachable and pinned
                // (`breaker_zero_stride_falls_back_to_post_join`), not a
                // parity bug for a future fuzzer to rediscover.
                let running = candidates.len() as u64;
                if stride != 0
                    && running.is_multiple_of(stride)
                    && over_budget(running, n_bars, limit)
                {
                    return Err(running);
                }
            }
        }
    }
    candidates.sort();
    Ok(candidates)
}

/// Build the Apriori-pruned level-`k` frontier for one sweep cell.
///
/// The pure core (no PyO3, no I/O) so `cargo test` exercises it without linking
/// libpython. Inputs are the ALREADY-VALIDATED contract (see the module
/// docstring): `unique_bits` sorted-unique `< 64`, `0 <= k <= n`, `big_w >= 1`,
/// `0 <= w < big_w`, `n_bars` any `u64`, `max_mask_bars > 0`. Defensive
/// idempotent re-sort/dedup of `unique_bits` keeps the core self-contained for
/// direct Rust testing; the seam already sorts+dedups, so it is a no-op there.
#[allow(clippy::too_many_arguments)]
pub fn apriori_frontier_core(
    bar_bits: &[u64],
    unique_bits: &[u32],
    k: u32,
    min_hits: i64,
    n_bars: u64,
    w: u64,
    big_w: u64,
    max_mask_bars: u64,
    conflict_pair_masks: &[u64],
    stride: u64,
) -> AprioriOutcome {
    // Defensive: a zero modulus would panic (SIGABRT). The seam guarantees
    // big_w >= 1; this dead guard keeps the core panic-free for direct calls.
    if big_w == 0 {
        return AprioriOutcome {
            masks: Vec::new(),
            census: Vec::new(),
            budget_trip: None,
        };
    }

    // Idempotent under the seam's pre-sorted-unique contract; robust for
    // direct cargo-test callers.
    let mut bits: Vec<u32> = unique_bits.to_vec();
    bits.sort_unstable();
    bits.dedup();

    let mut census: Vec<CensusRecord> = Vec::new();

    // ── k == 0: the single empty mask (owned by shard 0 only) ──
    if k == 0 {
        let masks = if w == 0 { vec![0u64] } else { Vec::new() };
        return AprioriOutcome {
            masks,
            census,
            budget_trip: None,
        };
    }

    // ── k == 1: every conflict-free singleton (singletons never conflict),
    //    NOT hit-filtered — the caller's Stage-1B drops the sparse ones. The
    //    WHOLE raw |C_1| is budget-checked (stride- + shard-invariant). ──
    if k == 1 {
        let c1 = bits.len() as u64;
        if over_budget(c1, n_bars, max_mask_bars) {
            // Trip BEFORE the census emit (the frozen order); no census.
            return AprioriOutcome {
                masks: Vec::new(),
                census,
                budget_trip: Some(c1),
            };
        }
        // k == 1 IS the target level: |C_1| emitted, frequent OMITTED.
        census.push((1u8, c1, CENSUS_FREQUENT_OMITTED));
        let masks: Vec<u64> = bits
            .iter()
            .filter(|&&bit| (bit as u64) % big_w == w)
            .map(|&bit| bit_mask(bit))
            .collect();
        return AprioriOutcome {
            masks,
            census,
            budget_trip: None,
        };
    }

    // ── k >= 2: build the frequent singleton frontier F_1 (NO budget check on
    //    |C_1| — deliberately unchecked for k >= 2). ──
    let mut frontier: Vec<Vec<u32>> = bits
        .iter()
        .filter(|&&bit| {
            let cnt = count_superset(bar_bits, bit_mask(bit));
            min_hits <= 0 || cnt >= min_hits as u64
        })
        .map(|&bit| vec![bit])
        .collect();
    let mut frontier_masks: Vec<u64> = frontier.iter().map(|t| mask_of(t)).collect();
    frontier_masks.sort_unstable();
    // Level-1 census: raw |C_1| singletons + the post-min_hits frequent |F_1|.
    census.push((1u8, bits.len() as u64, frontier.len() as i64));

    // Build the intermediate frequent frontiers F_2 … F_{k-1} (hit-filtered).
    let mut level = PAIR_LEVEL;
    while level < k {
        let drop_conflicts = level == PAIR_LEVEL;
        let candidates = match join_and_prune(
            &frontier,
            &frontier_masks,
            drop_conflicts,
            conflict_pair_masks,
            n_bars,
            max_mask_bars,
            stride,
        ) {
            Ok(c) => c,
            Err(running) => {
                // Strided trip during the join — census for THIS level not
                // emitted (raise precedes census), matching the oracle.
                return AprioriOutcome {
                    masks: Vec::new(),
                    census,
                    budget_trip: Some(running),
                };
            }
        };
        // AUTHORITATIVE breaker check on the SETTLED RAW |C_j| BEFORE the
        // hit-filter (guards the ENUMERATION cost, not the post-filter |F_j|).
        let cj = candidates.len() as u64;
        if over_budget(cj, n_bars, max_mask_bars) {
            return AprioriOutcome {
                masks: Vec::new(),
                census,
                budget_trip: Some(cj),
            };
        }
        // Hit-filter → the next frequent frontier.
        let mut next_frontier: Vec<Vec<u32>> = Vec::new();
        let mut next_masks: Vec<u64> = Vec::new();
        for candidate in &candidates {
            let mask = mask_of(candidate);
            let cnt = count_superset(bar_bits, mask);
            if min_hits <= 0 || cnt >= min_hits as u64 {
                next_frontier.push(candidate.clone());
                next_masks.push(mask);
            }
        }
        next_masks.sort_unstable();
        // Intermediate-level census: raw |C_j| + frequent frontier |F_j|.
        census.push((level as u8, cj, next_frontier.len() as i64));
        frontier = next_frontier;
        frontier_masks = next_masks;
        level += 1;
    }

    // Final level k: build C_k (NOT hit-filtered — the caller's Stage-1B does),
    // check the breaker on the WHOLE settled raw |C_k| BEFORE shard filtering
    // (shard-INVARIANT + whole-cell), then emit the shard-owned masks in
    // lex-by-position order.
    let final_candidates = match join_and_prune(
        &frontier,
        &frontier_masks,
        k == PAIR_LEVEL,
        conflict_pair_masks,
        n_bars,
        max_mask_bars,
        stride,
    ) {
        Ok(c) => c,
        Err(running) => {
            return AprioriOutcome {
                masks: Vec::new(),
                census,
                budget_trip: Some(running),
            };
        }
    };
    let ck = final_candidates.len() as u64;
    if over_budget(ck, n_bars, max_mask_bars) {
        return AprioriOutcome {
            masks: Vec::new(),
            census,
            budget_trip: Some(ck),
        };
    }
    // Target-level census: raw |C_k| (pre-shard); frequent OMITTED.
    census.push((k as u8, ck, CENSUS_FREQUENT_OMITTED));
    let masks: Vec<u64> = final_candidates
        .iter()
        .filter(|tpl| {
            // Shard ownership: the lowest set bit (tpl[0]) % big_w == w.
            tpl.first()
                .is_some_and(|&first| (first as u64) % big_w == w)
        })
        .map(|tpl| mask_of(tpl))
        .collect();
    AprioriOutcome {
        masks,
        census,
        budget_trip: None,
    }
}

// ────────────────────────────────────────────────────────────────────────
// PyO3 bridge
// ────────────────────────────────────────────────────────────────────────

use pyo3::prelude::*;

/// PyO3 entry: build the Apriori-pruned level-`k` frontier for one sweep cell.
///
/// Returns a 3-tuple `(masks, census, budget_trip)`:
/// * `masks: list[int]` — the pruned shard-owned level-`k` masks (lex-by-
///   position order; empty on a budget trip).
/// * `census: list[tuple[int, int, int]]` — `(level, candidates, frequent)`
///   records the seam emits as `sweep.apriori.census` events; `frequent == -1`
///   marks the omitted case (the seam maps `-1` → no `frequent` field).
/// * `budget_trip: int | None` — `None` on success, else the candidate count
///   the frozen `SweepAprioriBudgetError` reports (the seam raises it).
///
/// Inputs are the seam's validated contract: `unique_bits` sorted-unique `< 64`,
/// `0 <= k <= len(unique_bits)`, `big_w >= 1`, `0 <= w < big_w`, `n_bars` any
/// non-negative int fitting `u64`, `max_mask_bars > 0` fitting `u64`,
/// `conflict_pair_masks` the pre-packed `(1<<a)|(1<<b)` set,
/// `budget_check_stride` the frozen `_BUDGET_CHECK_STRIDE`. No `PyResult` /
/// exception is raised here — every frozen-pinned error (validation, unsupported
/// bit, budget trip) is raised by the Python seam so its EXACT message + type
/// are single-sourced from the frozen module.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn apriori_frontier_u64(
    bar_bits: Vec<u64>,
    unique_bits: Vec<u32>,
    k: u32,
    min_hits: i64,
    n_bars: u64,
    w: u64,
    big_w: u64,
    max_mask_bars: u64,
    conflict_pair_masks: Vec<u64>,
    budget_check_stride: u64,
) -> (Vec<u64>, Vec<CensusRecord>, Option<u64>) {
    let outcome = apriori_frontier_core(
        &bar_bits,
        &unique_bits,
        k,
        min_hits,
        n_bars,
        w,
        big_w,
        max_mask_bars,
        &conflict_pair_masks,
        budget_check_stride,
    );
    (outcome.masks, outcome.census, outcome.budget_trip)
}

#[cfg(test)]
mod tests {
    use super::{
        AprioriOutcome, CENSUS_FREQUENT_OMITTED, apriori_frontier_core, bit_mask, has_conflict,
        in_sorted, mask_of, over_budget,
    };

    /// Deterministic per-bar packed bitsets — the SplitMix64 mix used so the
    /// Rust unit fixtures are reproducible without an RNG crate.
    fn synthetic_bar_bits(seed: u64, n: usize, width: u32) -> Vec<u64> {
        let mask = if width >= 64 {
            u64::MAX
        } else {
            (1u64 << width) - 1
        };
        let mut state = seed.wrapping_add(0x9E37_79B9_7F4A_7C15);
        (0..n)
            .map(|_| {
                state = state.wrapping_add(0x9E37_79B9_7F4A_7C15);
                let mut z = state;
                z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
                z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
                (z ^ (z >> 31)) & mask
            })
            .collect()
    }

    // The three-bit-index conflict pairs (0,1)/(2,3)/(4,5) as packed masks —
    // mirrors the subset of `CONFLICT_PAIRS` the oracle suites exercise.
    fn conflict_pairs_012345() -> Vec<u64> {
        vec![
            (1u64 << 0) | (1u64 << 1),
            (1u64 << 2) | (1u64 << 3),
            (1u64 << 4) | (1u64 << 5),
        ]
    }

    const PROD_STRIDE: u64 = 65_536;
    const NO_LIMIT: u64 = u64::MAX;

    /// A reference brute-force frequent set: conflict-free k-combos with
    /// `hits >= min_hits`, in ascending-lex-by-position emission order.
    fn brute_frequent_ordered(
        bars: &[u64],
        bits: &[u32],
        k: u32,
        min_hits: i64,
        conflict_pairs: &[u64],
    ) -> Vec<u64> {
        // Enumerate position-combinations in lex order (mirrors
        // itertools.combinations over sorted unique bits).
        let mut sorted: Vec<u32> = bits.to_vec();
        sorted.sort_unstable();
        sorted.dedup();
        let n = sorted.len();
        let mut out: Vec<u64> = Vec::new();
        // index-combination generator (lex).
        let mut idx: Vec<usize> = (0..k as usize).collect();
        if k as usize > n {
            return out;
        }
        loop {
            let positions: Vec<u32> = idx.iter().map(|&i| sorted[i]).collect();
            let mask = mask_of(&positions);
            let conflict = has_conflict(mask, conflict_pairs);
            let hits = bars.iter().filter(|&&b| (b & mask) == mask).count() as i64;
            if !conflict && hits >= min_hits {
                out.push(mask);
            }
            // advance the combination
            if k == 0 {
                break;
            }
            let mut i = k as usize;
            loop {
                if i == 0 {
                    return out;
                }
                i -= 1;
                if idx[i] != i + n - k as usize {
                    break;
                }
            }
            idx[i] += 1;
            for j in (i + 1)..k as usize {
                idx[j] = idx[j - 1] + 1;
            }
        }
        out
    }

    /// The kept set = Apriori masks re-filtered by `hits >= min_hits`.
    fn apriori_kept_ordered(outcome: &AprioriOutcome, bars: &[u64], min_hits: i64) -> Vec<u64> {
        outcome
            .masks
            .iter()
            .copied()
            .filter(|&m| {
                let hits = bars.iter().filter(|&&b| (b & m) == m).count() as i64;
                hits >= min_hits
            })
            .collect()
    }

    fn run(
        bars: &[u64],
        bits: &[u32],
        k: u32,
        min_hits: i64,
        conflict_pairs: &[u64],
    ) -> AprioriOutcome {
        apriori_frontier_core(
            bars,
            bits,
            k,
            min_hits,
            bars.len() as u64,
            0,
            1,
            NO_LIMIT,
            conflict_pairs,
            PROD_STRIDE,
        )
    }

    // ── helper unit pins ──

    #[test]
    fn bit_mask_is_panic_free_and_exact() {
        assert_eq!(bit_mask(0), 1);
        assert_eq!(bit_mask(5), 32);
        assert_eq!(bit_mask(63), 1u64 << 63);
        // Dead defensive arm: >= 64 → 0 (never a shift-overflow panic).
        assert_eq!(bit_mask(64), 0);
        assert_eq!(bit_mask(1000), 0);
    }

    #[test]
    fn mask_of_ors_positions() {
        assert_eq!(mask_of(&[0, 3, 5]), 0b101001);
        assert_eq!(mask_of(&[]), 0);
    }

    #[test]
    fn has_conflict_detects_superset_pairs() {
        let pairs = conflict_pairs_012345();
        assert!(has_conflict(0b11, &pairs)); // bits 0,1
        assert!(has_conflict(0b1111, &pairs)); // contains (0,1) and (2,3)
        assert!(!has_conflict(0b101, &pairs)); // bits 0,2 — no pair
        assert!(!has_conflict(0, &pairs));
    }

    #[test]
    fn in_sorted_membership() {
        let arr = vec![1u64, 4, 32, 40];
        assert!(in_sorted(&arr, 1));
        assert!(in_sorted(&arr, 32));
        assert!(!in_sorted(&arr, 2));
        assert!(!in_sorted(&arr, 100)); // past end
        assert!(!in_sorted(&[], 1));
    }

    #[test]
    fn over_budget_strict_exact_u128_product() {
        // STRICT >: exactly at limit does NOT trip; one over trips.
        assert!(!over_budget(2, 10, 20)); // 20 !> 20
        assert!(over_budget(2, 10, 19)); // 20 > 19
        assert!(!over_budget(0, 10, 0)); // 0 !> 0
        // EXACT u128 product (round-1 fix): an over-u64 product trips even at
        // limit == u64::MAX — Python's big-int (2^64-1)·2 > 2^64-1 trips; the
        // former saturating_mul clamped to u64::MAX and FAILED to trip there.
        assert!(over_budget(u64::MAX, 2, 10));
        assert!(over_budget(u64::MAX, 2, u64::MAX)); // (2^64-1)·2 > 2^64-1
        assert!(over_budget(1 << 63, 2, u64::MAX)); // 2^64 > 2^64-1
        assert!(!over_budget(u64::MAX, 1, u64::MAX)); // exact == limit, !>
    }

    #[test]
    fn breaker_trips_at_limit_u64_max_with_overflowing_product() {
        // The round-1 MAJOR repro: k==1, |C_1| = 2 singletons, n_bars = 2^63
        // → product 2^64 > limit == u64::MAX ⇒ MUST trip (Python big-int
        // parity). Under the former saturating_mul the product clamped to
        // u64::MAX, the strict > read false, and the cell RAN (fail-open) —
        // and the level-1 census was emitted where the oracle raises first.
        let bars = vec![(1u64 << 6) | (1u64 << 7); 4];
        let out =
            apriori_frontier_core(&bars, &[6, 7], 1, 0, 1 << 63, 0, 1, u64::MAX, &[], PROD_STRIDE);
        assert_eq!(out.budget_trip, Some(2)); // |C_1| = 2
        assert!(out.masks.is_empty());
        assert!(out.census.is_empty()); // trip precedes census
    }

    // ── oracle parity (survivor set + emission order) ──

    #[test]
    fn oracle_kept_set_and_order_match_brute_force() {
        let bits: Vec<u32> = (0..8).collect();
        let pairs = conflict_pairs_012345();
        for seed in [1u64, 7, 99] {
            let bars = synthetic_bar_bits(seed, 500, 8);
            for min_hits in [0i64, 100, 250, 400] {
                for k in 0..=bits.len() as u32 {
                    let outcome = run(&bars, &bits, k, min_hits, &pairs);
                    assert_eq!(outcome.budget_trip, None, "no trip on the generous cap");
                    let kept = apriori_kept_ordered(&outcome, &bars, min_hits);
                    let brute = brute_frequent_ordered(&bars, &bits, k, min_hits, &pairs);
                    assert_eq!(
                        kept, brute,
                        "kept-set/order mismatch seed={seed} min_hits={min_hits} k={k}"
                    );
                }
            }
        }
    }

    #[test]
    fn emission_is_iter_order_subsequence() {
        let bits: Vec<u32> = (0..8).collect();
        let pairs = conflict_pairs_012345();
        for seed in [1u64, 7] {
            let bars = synthetic_bar_bits(seed, 500, 8);
            for min_hits in [0i64, 200] {
                for k in 0..=bits.len() as u32 {
                    let outcome = run(&bars, &bits, k, min_hits, &pairs);
                    let got: std::collections::BTreeSet<u64> =
                        outcome.masks.iter().copied().collect();
                    // The output must be the in-order subsequence of the FULL
                    // enumeration (conflict-free) filtered to the emitted set.
                    let full = brute_frequent_ordered(&bars, &bits, k, i64::MIN, &pairs);
                    let expected: Vec<u64> = full.into_iter().filter(|m| got.contains(m)).collect();
                    assert_eq!(
                        outcome.masks, expected,
                        "seed={seed} min_hits={min_hits} k={k}"
                    );
                }
            }
        }
    }

    #[test]
    fn lex_order_differs_from_int_order() {
        // Interleaved bits: (0,3)->9 sorts before (1,2)->6 lexicographically
        // but after numerically. bits 0-3 carry conflict pairs (0,1)/(2,3).
        let bars = vec![0b1111u64; 40];
        let pairs = conflict_pairs_012345();
        let outcome = run(&bars, &[0, 1, 2, 3], 2, 0, &pairs);
        assert_eq!(outcome.masks, vec![5, 9, 6, 10]); // lex-by-position
        let mut sorted = outcome.masks.clone();
        sorted.sort_unstable();
        assert_ne!(outcome.masks, sorted); // NOT ascending-by-int (9 precedes 6)
    }

    #[test]
    fn sparse_interleaved_and_bit63() {
        // Sparse interleaved bit palette incl. the top bit 63 — the refuter's
        // hostile pool. Every mask fires on all bars → whole conflict-free set.
        let bits: Vec<u32> = vec![2, 5, 11, 40, 63];
        let hi = bits.iter().fold(0u64, |a, &b| a | (1u64 << b));
        let bars = vec![hi; 20];
        let pairs = conflict_pairs_012345();
        for k in 0..=bits.len() as u32 {
            let outcome = run(&bars, &bits, k, 0, &pairs);
            let brute = brute_frequent_ordered(&bars, &bits, k, 0, &pairs);
            assert_eq!(outcome.masks, brute, "bit-63 interleaved k={k}");
        }
    }

    // ── survival-law fixtures ──

    #[test]
    fn k0_is_empty_mask_owned_by_shard_zero() {
        let out = apriori_frontier_core(
            &[1, 2, 3],
            &[0, 1, 2],
            0,
            0,
            3,
            0,
            1,
            NO_LIMIT,
            &[],
            PROD_STRIDE,
        );
        assert_eq!(out.masks, vec![0]);
        assert!(out.census.is_empty()); // no census for k==0
        let out_w1 =
            apriori_frontier_core(&[1], &[0, 1, 2], 0, 0, 1, 1, 3, NO_LIMIT, &[], PROD_STRIDE);
        assert_eq!(out_w1.masks, Vec::<u64>::new());
    }

    #[test]
    fn k1_all_singletons_not_hit_filtered() {
        // min_hits huge → no singleton frequent, but k==1 is NOT hit-filtered.
        let bars = synthetic_bar_bits(1, 500, 8);
        let bits: Vec<u32> = (0..8).collect();
        let out = run(&bars, &bits, 1, 10_000, &[]);
        let expected: Vec<u64> = bits.iter().map(|&b| 1u64 << b).collect();
        assert_eq!(out.masks, expected);
        // census: (1, |C_1|=8, omitted).
        assert_eq!(out.census, vec![(1u8, 8u64, CENSUS_FREQUENT_OMITTED)]);
    }

    #[test]
    fn minhits_boundary_exactly_30_survives_29_drops() {
        // F_1 singleton filter `count >= min_hits`: bits 6,7 fire on EXACTLY 30
        // bars → both frequent → pair (6,7) emitted. At 31 they drop.
        let pair = (1u64 << 6) | (1u64 << 7);
        let bars = vec![pair; 30];
        let out30 = run(&bars, &[6, 7], 2, 30, &[]);
        assert_eq!(out30.masks, vec![pair]); // 30 >= 30 survives
        let out31 = run(&bars, &[6, 7], 2, 31, &[]);
        assert_eq!(out31.masks, Vec::<u64>::new()); // 30 < 31 drops
    }

    #[test]
    fn high_min_hits_empty_frontier() {
        let bars = synthetic_bar_bits(1, 500, 8);
        let bits: Vec<u32> = (0..8).collect();
        for k in [2u32, 3, 4] {
            assert_eq!(run(&bars, &bits, k, 10_000, &[]).masks, Vec::<u64>::new());
        }
    }

    #[test]
    fn conflict_pair_dropped_at_level_two() {
        // (0,1) is a conflict pair; both singletons frequent → the only level-2
        // candidate (0,1) is conflict-dropped → empty.
        let bars = vec![0b11u64; 50];
        let out = run(&bars, &[0, 1], 2, 0, &conflict_pairs_012345());
        assert_eq!(out.masks, Vec::<u64>::new());
    }

    #[test]
    fn subset_prune_drops_triple_with_infrequent_pair() {
        // 6,7 fire on every bar; 2 rarely → pairs (2,6)/(2,7) infrequent → the
        // conflict-free triple (2,6,7) is subset-pruned.
        let mut bars = vec![0b1100_0000u64; 100];
        bars.extend(vec![0b1100_0100u64; 3]);
        let out = run(&bars, &[2, 6, 7], 3, 10, &conflict_pairs_012345());
        let triple = (1u64 << 2) | (1u64 << 6) | (1u64 << 7);
        assert!(!out.masks.contains(&triple));
    }

    #[test]
    fn candidates_are_conflict_free() {
        let bits: Vec<u32> = (0..8).collect();
        let pairs = conflict_pairs_012345();
        for seed in [1u64, 7, 99] {
            let bars = synthetic_bar_bits(seed, 500, 8);
            for k in [2u32, 3, 4, 5] {
                for &m in &run(&bars, &bits, k, 0, &pairs).masks {
                    assert!(!has_conflict(m, &pairs), "conflict mask leaked k={k}");
                }
            }
        }
    }

    // ── root_mod shard union + disjointness + lowest-set-bit ownership ──

    #[test]
    fn shards_union_to_whole_and_are_disjoint() {
        let bars = synthetic_bar_bits(11, 500, 8);
        let bits: Vec<u32> = (0..8).collect();
        let pairs = conflict_pairs_012345();
        for big_w in [1u64, 2, 3, 7] {
            for k in [0u32, 1, 2, 3] {
                let whole: std::collections::BTreeSet<u64> = apriori_frontier_core(
                    &bars,
                    &bits,
                    k,
                    0,
                    bars.len() as u64,
                    0,
                    1,
                    NO_LIMIT,
                    &pairs,
                    PROD_STRIDE,
                )
                .masks
                .into_iter()
                .collect();
                let mut union: std::collections::BTreeSet<u64> = std::collections::BTreeSet::new();
                let mut shards: Vec<std::collections::BTreeSet<u64>> = Vec::new();
                for w in 0..big_w {
                    let shard: std::collections::BTreeSet<u64> = apriori_frontier_core(
                        &bars,
                        &bits,
                        k,
                        0,
                        bars.len() as u64,
                        w,
                        big_w,
                        NO_LIMIT,
                        &pairs,
                        PROD_STRIDE,
                    )
                    .masks
                    .into_iter()
                    .collect();
                    for &m in &shard {
                        assert!(union.insert(m), "shard overlap big_w={big_w} k={k}");
                    }
                    shards.push(shard);
                }
                assert_eq!(union, whole, "union != whole big_w={big_w} k={k}");
            }
        }
    }

    #[test]
    fn shard_ownership_is_lowest_set_bit() {
        // (w=0, W=2) owns masks whose LOWEST set bit index is even. mask 192 =
        // bits {6,7}: lowest bit 6 (even) → owned; second bit 7 (odd) would drop
        // under a tpl[1] key.
        let bars = synthetic_bar_bits(11, 500, 8);
        let bits: Vec<u32> = vec![6, 7, 8, 9];
        let whole = apriori_frontier_core(
            &bars,
            &bits,
            2,
            0,
            bars.len() as u64,
            0,
            1,
            NO_LIMIT,
            &[],
            PROD_STRIDE,
        )
        .masks;
        let shard0 = apriori_frontier_core(
            &bars,
            &bits,
            2,
            0,
            bars.len() as u64,
            0,
            2,
            NO_LIMIT,
            &[],
            PROD_STRIDE,
        )
        .masks;
        let expected: Vec<u64> = whole
            .iter()
            .copied()
            .filter(|&m| ((m & m.wrapping_neg()).trailing_zeros()) % 2 == 0)
            .collect();
        assert_eq!(shard0, expected);
        assert!(expected.contains(&192)); // bits {6,7}: lowest bit 6 even → owned
    }

    // ── the breaker: exact boundary, strict >, k==1 vs k>=2 levels ──

    #[test]
    fn breaker_exact_boundary_does_not_trip() {
        // 2 candidates × 10 bars == 20 == limit → ALLOWED (strict >).
        let bars = vec![(1u64 << 6) | (1u64 << 7); 10];
        let out = apriori_frontier_core(&bars, &[6, 7], 1, 0, 10, 0, 1, 20, &[], PROD_STRIDE);
        assert_eq!(out.budget_trip, None);
        assert_eq!(out.masks, vec![1u64 << 6, 1u64 << 7]);
    }

    #[test]
    fn breaker_one_over_trips() {
        let bars = vec![(1u64 << 6) | (1u64 << 7); 10];
        let out = apriori_frontier_core(&bars, &[6, 7], 1, 0, 10, 0, 1, 19, &[], PROD_STRIDE);
        assert_eq!(out.budget_trip, Some(2)); // |C_1| = 2 singletons
        assert!(out.masks.is_empty());
        assert!(out.census.is_empty()); // trip precedes census
    }

    #[test]
    fn breaker_k1_target_level_trips() {
        // k==1 is the target level → its |C_1| IS budget-checked (tiny budget).
        let bars = vec![0u64; 8];
        let out = apriori_frontier_core(
            &bars,
            &[0, 1, 2, 3, 4, 5, 6, 7],
            1,
            0,
            8,
            0,
            1,
            5,
            &[],
            PROD_STRIDE,
        );
        assert_eq!(out.budget_trip, Some(8)); // 8 × 8 = 64 > 5
    }

    #[test]
    fn breaker_k2_trips_at_level_2_not_level_1() {
        // k >= 2 with a tiny budget: level-1 |C_1| is DELIBERATELY unchecked, so
        // the trip fires at the final level 2 (its raw |C_2|), NOT at level 1.
        // 5 conflict-free bits → |C_1|=5 (would trip 5×20=100>1 if checked), but
        // level-1 is skipped; |C_2| = C(5,2) = 10 → 10×20 = 200 > 1 trips at k=2.
        let bars = vec![0b1111_1111u64; 20];
        let out =
            apriori_frontier_core(&bars, &[0, 2, 4, 6, 7], 2, 0, 20, 0, 1, 1, &[], PROD_STRIDE);
        assert_eq!(out.budget_trip, Some(10)); // |C_2| = 10 (level 2), not 5
        // Level-1 census WAS emitted (F_1 has no budget check); level-2 census
        // was NOT (trip precedes it).
        assert_eq!(out.census, vec![(1u8, 5u64, 5i64)]);
    }

    #[test]
    fn breaker_trip_is_shard_invariant() {
        // Whole |C_1| = 1 singleton, 1 × 8 = 8 > 5 → trips for EVERY shard
        // regardless of which owns bit 10.
        for big_w in [1u64, 2, 3, 4] {
            for w in 0..big_w {
                let out = apriori_frontier_core(
                    &[1u64 << 10],
                    &[10],
                    1,
                    5,
                    8,
                    w,
                    big_w,
                    5,
                    &[],
                    PROD_STRIDE,
                );
                assert_eq!(out.budget_trip, Some(1), "shard ({w},{big_w}) must trip");
            }
        }
    }

    #[test]
    fn breaker_stride_invariance_of_the_decision() {
        // THE frozen contract (`test_budget_check_stride_invariance`): the trip
        // DECISION (raise vs return) is a pure function of the settled raw
        // per-level |C_j| and is INDEPENDENT of the stride — NOT the reported
        // count. The minimal repro (12 single-bit bars, k=3, min_hits=1,
        // n_bars=12, max=600): |C_2| = C(12,2) - 3 conflict pairs = 63,
        // 63×12 = 756 > 600, but NO pair co-occurs on a single-bit bar so
        // |F_2| = 0 (a |F_j|-based check would NOT trip — the pre-fix bug).
        let bars: Vec<u64> = (0..12u32).map(|i| 1u64 << i).collect();
        let bits: Vec<u32> = (0..12).collect();
        let pairs = conflict_pairs_012345();
        for stride in [1u64, 2, 100, 65_536, 1_000_000_000] {
            let out = apriori_frontier_core(&bars, &bits, 3, 1, 12, 0, 1, 600, &pairs, stride);
            assert!(out.budget_trip.is_some(), "stride={stride} must raise");
        }
    }

    #[test]
    fn breaker_strided_running_count_matches_oracle() {
        // The reported COUNT is the running strided count (a multiple of stride)
        // when the strided check fires first, else the settled |C_2| when only
        // the post-join check fires — bit-identical to the frozen Python oracle:
        // |C_2| = 63, first running > 50 (50×12=600 not>600) so 51×12=612>600.
        //   stride=1   → strided fires at 51
        //   stride=2   → strided fires at 52 (50 not>budget; 52 is next even)
        //   stride=100 → no multiple <= 63 → post-join at |C_2|=63
        let bars: Vec<u64> = (0..12u32).map(|i| 1u64 << i).collect();
        let bits: Vec<u32> = (0..12).collect();
        let pairs = conflict_pairs_012345();
        let count_for = |stride: u64| {
            apriori_frontier_core(&bars, &bits, 3, 1, 12, 0, 1, 600, &pairs, stride).budget_trip
        };
        assert_eq!(count_for(1), Some(51));
        assert_eq!(count_for(2), Some(52));
        assert_eq!(count_for(100), Some(63));
        assert_eq!(count_for(65_536), Some(63));
    }

    #[test]
    fn breaker_strided_early_trip_reports_running_count() {
        // A dense k=2 cell producing many candidates with a small stride: the
        // strided check fires at the first multiple-of-stride running count that
        // exceeds the budget, reporting that running count (not the final |C_2|).
        // 10 bits all-firing → |C_2| = C(10,2) - conflicts. With stride=4 and a
        // budget of 1 (n_bars=20), the 4th candidate (4×20=80>1) trips → 4.
        let bars = vec![0b11_1111_1111u64; 20];
        let bits: Vec<u32> = (0..10).collect();
        let out =
            apriori_frontier_core(&bars, &bits, 2, 0, 20, 0, 1, 1, &conflict_pairs_012345(), 4);
        assert_eq!(out.budget_trip, Some(4)); // strided fire at running count 4
    }

    #[test]
    fn breaker_zero_stride_falls_back_to_post_join() {
        // stride == 0 (defensive) → no strided check, only the authoritative
        // post-join |C_2| check. Must still trip on |C_2|, never panic on % 0.
        let bars = vec![0b11_1111_1111u64; 20];
        let bits: Vec<u32> = (0..10).collect();
        let out =
            apriori_frontier_core(&bars, &bits, 2, 0, 20, 0, 1, 1, &conflict_pairs_012345(), 0);
        // |C_2| for 10 conflict-free-ish bits (minus (0,1)/(2,3)/(4,5)) =
        // C(10,2) - 3 = 42 → post-join trips reporting 42.
        assert_eq!(out.budget_trip, Some(42));
    }

    // ── census records ──

    #[test]
    fn census_records_full_levels() {
        // A clean k=3 build over dense bars: level-1 (|C_1|,|F_1|), level-2
        // (|C_2|,|F_2|), level-3 target (|C_3|, omitted).
        let bits: Vec<u32> = vec![6, 7, 8];
        let bars = vec![(1u64 << 6) | (1u64 << 7) | (1u64 << 8); 20];
        let out = run(&bars, &bits, 3, 0, &[]);
        assert_eq!(out.budget_trip, None);
        // 3 singletons all frequent; C(3,2)=3 pairs all frequent; 1 triple.
        assert_eq!(
            out.census,
            vec![
                (1u8, 3u64, 3i64),
                (2u8, 3u64, 3i64),
                (3u8, 1u64, CENSUS_FREQUENT_OMITTED),
            ]
        );
        assert_eq!(out.masks, vec![(1u64 << 6) | (1u64 << 7) | (1u64 << 8)]);
    }

    // ── determinism / permutation invariance ──

    #[test]
    fn determinism_and_permutation_invariance() {
        let bars = synthetic_bar_bits(3, 500, 8);
        let bits: Vec<u32> = (0..8).collect();
        let pairs = conflict_pairs_012345();
        let a = run(&bars, &bits, 3, 150, &pairs);
        let b = run(&bars, &bits, 3, 150, &pairs);
        assert_eq!(a, b);
        let mut rev = bits.clone();
        rev.reverse();
        assert_eq!(run(&bars, &rev, 3, 150, &pairs), a); // set semantics
    }

    #[test]
    fn n_bars_zero_allowed_never_trips() {
        // n_bars == 0 → product 0, never > any positive limit → returns.
        let out = apriori_frontier_core(&[], &[6, 7], 1, 0, 0, 0, 1, 1, &[], PROD_STRIDE);
        assert_eq!(out.budget_trip, None);
        assert_eq!(out.masks, vec![1u64 << 6, 1u64 << 7]);
    }

    #[test]
    fn big_w_zero_defensive_empty() {
        // Unreachable via the seam (big_w >= 1 validated) — the dead guard keeps
        // the core panic-free (no % 0).
        let out = apriori_frontier_core(&[1], &[0, 1], 1, 0, 1, 0, 0, 100, &[], PROD_STRIDE);
        assert!(out.masks.is_empty());
        assert!(out.census.is_empty());
    }
}
