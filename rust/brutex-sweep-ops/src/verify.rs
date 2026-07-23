//! slice 5 — verify/apriori pure cores (DEC-RUST-SWEEP-OPS-001).
//!
//! The PURE decision laws of the verify/apriori family — each a
//! token-for-token mirror of its retained Python oracle. The RUN-IDENTITY
//! boundary seams (`sweep_run.canonical_trade_params` / `mask_run_id`) are
//! NEVER converted or re-rendered — `sweep_verify` consumes them as opaque
//! Python values; every finding/refusal MESSAGE byte, the audit reads, the
//! Parquet loads, the sha256 hashing and the `dataclasses`-introspection
//! VIX row-attribution ladder stay Python. `sweep_diffcheck` is assessed
//! N/A WHOLE (the R45 independence artifact — its reference simulator must
//! never be re-hosted in the shared conversion crate; its `sample_masks`
//! is CPython-MT19937 `random.Random.sample` semantics-land; its
//! `drawdown_bit_key` is byte RENDERING).
//!
//! * [`recount_verdict`] — `brutex/engine/sweep_verify.py`'s
//!   `compare_recount` post-byte-diff CLASSIFICATION tail (the B1/B2/B5
//!   condition ladder + the VIX-residual family split). The VIX
//!   attribution itself (row loads + `replace()`/`getattr` introspection)
//!   is computed Python-side and crosses as ONE boolean; the
//!   `grid_corrupt_leaderboards` side effect and every finding
//!   code/severity/detail byte stay Python, keyed off the token.
//! * [`apriori_join_prune`] — `brutex/engine/sweep_apriori.py`'s classic
//!   `F_{j-1} ⋈ F_{j-1}` join + subset-prune level fold (the module's own
//!   docstring law: "deterministic, pure integer, bit-identical
//!   Python↔Rust"), including the strided mask·bar budget trip as a
//!   RETURNED value (never an Err — a genuine trip is a semantic outcome
//!   the facade must not degrade away; Python raises the
//!   `SweepAprioriBudgetError` with its own message bytes). The frequent
//!   frontier membership set is REBUILT here from the frontier tuples
//!   (identical to the caller's sorted-mask array by construction), and
//!   conflict pairs cross as pre-packed pair-masks (`(1<<a) | (1<<b)`) so
//!   `has_conflict` mirrors as `mask & pm == pm`.
//!
//! Every function is TOTAL (the crate profile is `panic = "abort"` +
//! `overflow-checks = true` — a panic would abort the host process, so
//! fallible shapes return `Err`, the budget product uses u128, and no
//! unchecked arithmetic is used).

use std::collections::BTreeSet;

/// The recount post-byte-diff verdict vocabulary (the facade's
/// `_RECOUNT_VERDICT_TOKENS` twin).
pub const RECOUNT_VERDICT_TOKENS: [&str; 6] = [
    "vix_divergence",
    "grid_corrupt",
    "grid_inconclusive",
    "cell_dependent",
    "cell_corrupt",
    "cell_inconclusive",
];

/// `sweep_verify.compare_recount`'s post-byte-diff classification tail.
///
/// Consulted ONLY after both artifacts exist AND their bytes differ, with
/// the VIX attribution (`_artifact_vix_shape` — Python-side row/pairing
/// introspection) folded to one boolean. Mirrors the ladder
/// token-for-token:
///
/// 1. VIX-attributable → `"vix_divergence"` (Python picks the grid/cell
///    finding code and the moved/backfill remedy text from its own shape);
/// 2. a GRID tuple → `"grid_corrupt"` under a fully matched context
///    (verdict-grade — the only heal-grade state), else
///    `"grid_inconclusive"`;
/// 3. a CELL whose leaderboard's grid recount already diverged →
///    `"cell_dependent"` (collateral, condition B5);
/// 4. a CELL under full context AND full cell evidence → `"cell_corrupt"`
///    (condition B2 — never aliases the grid code);
/// 5. anything else → `"cell_inconclusive"` (condition B1).
pub fn recount_verdict(
    vix_attributable: bool,
    is_grid_tuple: bool,
    context_match: bool,
    cell_evidence: bool,
    grid_corrupt_leaderboard: bool,
) -> &'static str {
    if vix_attributable {
        return "vix_divergence";
    }
    if is_grid_tuple {
        return if context_match {
            "grid_corrupt"
        } else {
            "grid_inconclusive"
        };
    }
    if grid_corrupt_leaderboard {
        return "cell_dependent";
    }
    if context_match && cell_evidence {
        return "cell_corrupt";
    }
    "cell_inconclusive"
}

/// Pack an ascending bit-position tuple into its u64 mask (`_mask_of`).
fn mask_of(positions: &[u8]) -> u64 {
    let mut mask = 0u64;
    for &p in positions {
        mask |= 1u64 << p;
    }
    mask
}

/// `sweep_apriori._join_and_prune`'s level fold with the strided budget trip.
///
/// *frontier* is the frequent `(j-1)`-frontier as LEX-SORTED ascending
/// bit-position tuples (every bit `< 64` — `Err` otherwise, defensive: the
/// facade gates, and `generate_apriori_candidates` refuses `>= 64` bits
/// before any join). Buckets the frontier by its shared `j-2` prefix
/// (lex-sorted input ⇒ equal prefixes are CONTIGUOUS, so contiguous
/// grouping reproduces Python's insertion-ordered dict buckets), joins
/// every ordered pair within a bucket, keeps a candidate iff ALL its
/// `(j-1)`-subsets are frequent, drops conflict-pair carriers when
/// *drop_conflicts* (the `j == 2` level), and applies the strided
/// mask·bar budget check after every append exactly like the Python
/// `on_count` closure: at each `stride` multiple, trip iff
/// `count * n_bars > limit` (STRICT — pure u128 product, no overflow).
///
/// Returns `(candidates, None)` — lex-sorted, exactly Python's
/// `candidates.sort()` order — or `(empty, Some(tripped_count))` on a
/// budget trip (the count at the tripping stride multiple; Python raises
/// its own `SweepAprioriBudgetError` bytes from it). The trip decision +
/// count are append-order-independent (the count only grows), so the
/// contiguous bucketing cannot move the trip.
pub fn apriori_join_prune(
    frontier: &[Vec<u8>],
    drop_conflicts: bool,
    conflict_masks: &[u64],
    n_bars: u64,
    limit: u64,
    stride: u64,
) -> Result<(Vec<Vec<u8>>, Option<u64>), String> {
    if stride == 0 {
        return Err("stride must be >= 1".to_string());
    }
    for tuple in frontier {
        if tuple.is_empty() {
            return Err("frontier tuples must be non-empty".to_string());
        }
        for &bit in tuple {
            if bit >= 64 {
                return Err(format!("frontier bit {bit} out of range [0, 64)"));
            }
        }
    }
    let frontier_masks: BTreeSet<u64> = frontier.iter().map(|t| mask_of(t)).collect();

    // Contiguous prefix grouping over the lex-sorted frontier — Python's
    // insertion-ordered `buckets.setdefault(tpl[:-1], []).append(tpl[-1])`.
    let mut candidates: Vec<Vec<u8>> = Vec::new();
    let mut count: u64 = 0;
    let mut start = 0usize;
    while start < frontier.len() {
        let prefix = &frontier[start][..frontier[start].len().saturating_sub(1)];
        let mut end = start;
        while end < frontier.len() && frontier[end].len() == frontier[start].len() {
            let candidate_prefix = &frontier[end][..frontier[end].len().saturating_sub(1)];
            if candidate_prefix != prefix {
                break;
            }
            end += 1;
        }
        let lasts: Vec<u8> = frontier[start..end]
            .iter()
            .map(|t| *t.last().unwrap_or(&0))
            .collect();
        for i in 0..lasts.len() {
            for jj in (i + 1)..lasts.len() {
                let mut candidate: Vec<u8> = Vec::with_capacity(prefix.len() + 2);
                candidate.extend_from_slice(prefix);
                candidate.push(lasts[i]);
                candidate.push(lasts[jj]);
                if !all_subsets_frequent(&candidate, &frontier_masks) {
                    continue;
                }
                if drop_conflicts && has_conflict(mask_of(&candidate), conflict_masks) {
                    continue;
                }
                candidates.push(candidate);
                count += 1;
                if count.is_multiple_of(stride)
                    && u128::from(count) * u128::from(n_bars) > u128::from(limit)
                {
                    return Ok((Vec::new(), Some(count)));
                }
            }
        }
        start = end;
    }
    candidates.sort();
    Ok((candidates, None))
}

/// `has_conflict` over pre-packed conflict pair-masks (`mask & pm == pm`).
fn has_conflict(mask: u64, conflict_masks: &[u64]) -> bool {
    for &pair_mask in conflict_masks {
        if mask & pair_mask == pair_mask {
            return true;
        }
    }
    false
}

/// `_all_subsets_frequent` — every `(len-1)`-subset mask is frequent.
fn all_subsets_frequent(candidate: &[u8], frontier_masks: &BTreeSet<u64>) -> bool {
    let full = mask_of(candidate);
    for &drop in candidate {
        let subset = full & !(1u64 << drop);
        if !frontier_masks.contains(&subset) {
            return false;
        }
    }
    true
}

#[cfg(test)]
mod tests {
    use super::*;

    // ── recount_verdict ─────────────────────────────────────────────────

    #[test]
    fn recount_vix_wins_over_everything() {
        for grid in [false, true] {
            for ctx in [false, true] {
                assert_eq!(
                    recount_verdict(true, grid, ctx, true, true),
                    "vix_divergence"
                );
            }
        }
    }

    #[test]
    fn recount_grid_splits_on_context_match() {
        assert_eq!(
            recount_verdict(false, true, true, false, false),
            "grid_corrupt"
        );
        assert_eq!(
            recount_verdict(false, true, false, true, true),
            "grid_inconclusive"
        );
    }

    #[test]
    fn recount_cell_dependent_wins_over_cell_evidence() {
        // B5: collateral divergence outranks independent cell evidence.
        assert_eq!(
            recount_verdict(false, false, true, true, true),
            "cell_dependent"
        );
    }

    #[test]
    fn recount_cell_corrupt_needs_context_and_evidence() {
        assert_eq!(
            recount_verdict(false, false, true, true, false),
            "cell_corrupt"
        );
        assert_eq!(
            recount_verdict(false, false, false, true, false),
            "cell_inconclusive"
        );
        assert_eq!(
            recount_verdict(false, false, true, false, false),
            "cell_inconclusive"
        );
    }

    // ── apriori_join_prune ──────────────────────────────────────────────

    fn tuples(raw: &[&[u8]]) -> Vec<Vec<u8>> {
        raw.iter().map(|t| t.to_vec()).collect()
    }

    #[test]
    fn join_singleton_frontier_emits_all_pairs() {
        // F_1 = {0, 1, 2}: one shared () prefix → the 3 ordered pairs.
        let frontier = tuples(&[&[0], &[1], &[2]]);
        let (cands, tripped) =
            apriori_join_prune(&frontier, false, &[], 10, 1_000_000, 65_536).unwrap();
        assert_eq!(tripped, None);
        assert_eq!(cands, tuples(&[&[0, 1], &[0, 2], &[1, 2]]));
    }

    #[test]
    fn join_subset_prune_kills_a_candidate_with_an_infrequent_subset() {
        // F_2 = {(0,1), (0,2)} — candidate (0,1,2) needs subset (1,2), absent.
        let frontier = tuples(&[&[0, 1], &[0, 2]]);
        let (cands, tripped) =
            apriori_join_prune(&frontier, false, &[], 10, 1_000_000, 65_536).unwrap();
        assert_eq!(tripped, None);
        assert!(cands.is_empty());
    }

    #[test]
    fn join_triple_survives_when_all_subsets_frequent() {
        let frontier = tuples(&[&[0, 1], &[0, 2], &[1, 2]]);
        let (cands, tripped) =
            apriori_join_prune(&frontier, false, &[], 10, 1_000_000, 65_536).unwrap();
        assert_eq!(tripped, None);
        assert_eq!(cands, tuples(&[&[0, 1, 2]]));
    }

    #[test]
    fn join_drop_conflicts_removes_pair_mask_carriers() {
        // Conflict pair (0, 2) → pair-mask 0b101; (0,1)/(1,2) survive.
        let frontier = tuples(&[&[0], &[1], &[2]]);
        let conflict = [(1u64 << 0) | (1u64 << 2)];
        let (cands, tripped) =
            apriori_join_prune(&frontier, true, &conflict, 10, 1_000_000, 65_536).unwrap();
        assert_eq!(tripped, None);
        assert_eq!(cands, tuples(&[&[0, 1], &[1, 2]]));
        // drop_conflicts OFF keeps the carrier (the j >= 3 inductive law).
        let (all, _) =
            apriori_join_prune(&frontier, false, &conflict, 10, 1_000_000, 65_536).unwrap();
        assert_eq!(all.len(), 3);
    }

    #[test]
    fn join_budget_trips_at_the_stride_multiple() {
        // stride 2, n_bars 10, limit 25: the count-2 check reads 20 <= 25
        // (no trip); the count-4 check reads 40 > 25 → trip AT 4.
        let frontier = tuples(&[&[0], &[1], &[2], &[3]]);
        let (cands, tripped) = apriori_join_prune(&frontier, false, &[], 10, 25, 2).unwrap();
        assert_eq!(tripped, Some(4));
        assert!(cands.is_empty());
    }

    #[test]
    fn join_budget_strict_gt_allows_exactly_at_limit() {
        // 6 pairs × 10 bars = 60 == limit → NO trip (STRICT >), even with
        // stride 1 checking every append.
        let frontier = tuples(&[&[0], &[1], &[2], &[3]]);
        let (cands, tripped) = apriori_join_prune(&frontier, false, &[], 10, 60, 1).unwrap();
        assert_eq!(tripped, None);
        assert_eq!(cands.len(), 6);
    }

    #[test]
    fn join_off_stride_count_never_trips_mid_join() {
        // The settled |C_j| check is the CALLER's (Python) — a count that
        // exceeds the budget only between stride multiples returns whole.
        let frontier = tuples(&[&[0], &[1], &[2]]);
        let (cands, tripped) = apriori_join_prune(&frontier, false, &[], 10, 5, 65_536).unwrap();
        assert_eq!(tripped, None);
        assert_eq!(cands.len(), 3);
    }

    #[test]
    fn join_budget_product_uses_u128() {
        // count * n_bars would overflow u64; the u128 product still trips.
        let frontier = tuples(&[&[0], &[1], &[2]]);
        // count 1: 1 * MAX == MAX is NOT > MAX (STRICT); count 2 trips.
        let (_, tripped) =
            apriori_join_prune(&frontier, false, &[], u64::MAX, u64::MAX, 1).unwrap();
        assert_eq!(tripped, Some(2));
    }

    #[test]
    fn join_multi_prefix_buckets_group_contiguously() {
        // F_2 buckets: prefix (0,) → lasts [1, 2]; prefix (1,) → lasts [2].
        // Join emits (0,1,2) from the first bucket; subset (1,2) is frequent.
        let frontier = tuples(&[&[0, 1], &[0, 2], &[1, 2]]);
        let (cands, _) = apriori_join_prune(&frontier, false, &[], 1, 1_000, 65_536).unwrap();
        assert_eq!(cands, tuples(&[&[0, 1, 2]]));
    }

    #[test]
    fn join_empty_frontier_is_empty() {
        let (cands, tripped) = apriori_join_prune(&[], false, &[], 10, 1_000, 65_536).unwrap();
        assert_eq!(tripped, None);
        assert!(cands.is_empty());
    }

    #[test]
    fn join_defensive_errs() {
        assert!(apriori_join_prune(&tuples(&[&[64]]), false, &[], 1, 1, 1).is_err());
        assert!(apriori_join_prune(&[], false, &[], 1, 1, 0).is_err());
        assert!(apriori_join_prune(&tuples(&[&[]]), false, &[], 1, 1, 1).is_err());
    }
}
