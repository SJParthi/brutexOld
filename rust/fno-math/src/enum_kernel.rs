//! Sweep-enumeration kernel — the fused k-of-bits enumeration + conflict prune.
//!
//! DEC-SWEEP-ENUM-RUST-001 (MIGRATION.md #1738; umbrella
//! DEC-RUST-FIRST-MANDATE-001). The Rust port of the sweep's mask-source
//! segment: `brutex.engine.combo_mask.iter_masks_k_of_bits` (the production
//! enumerator) FUSED with the per-mask `brutex.vocabulary.has_conflict` prune,
//! returning the ordered conflict-free survivor list plus the two honest
//! tallies (`enumerated`, `conflict_pruned`) the sweep's counters, trial-stats
//! sidecar and audit payloads embed verbatim.
//!
//! ## The order law (load-bearing — Golden Rule #4)
//!
//! Survivors are emitted in **`itertools.combinations` lexicographic order on
//! ascending bit-position tuples over `sorted(set(bits))`** — NOT
//! numeric-ascending mask order. The two orders differ from n = 4: position-lex
//! for n=4, k=2 yields masks 3, 5, **9, 6**, 10, 12 while a Gosper's-hack
//! enumeration yields 3, 5, **6, 9**, 10, 12. Enumeration order feeds the
//! sweep's chunk boundaries, `SweepComboError` attribution, stable-sort
//! tie-breaks and the legacy `mark_many` order — a set-equal but order-drifted
//! list produces DIFFERENT durable bytes. The odometer below is a literal
//! transcription of the CPython `itertools.combinations` advance.
//!
//! ## Conflict pairs are DATA, never re-minted here
//!
//! The pair table crosses the FFI per call (`sorted(CONFLICT_PAIRS)` from
//! `brutex.vocabulary.conflicts` — the single source of truth). The prune test
//! `mask & pair_mask == pair_mask` is arithmetic-identical to Python's
//! `(mask >> a) & 1 and (mask >> b) & 1` (a pair fires iff BOTH bits are set).
//! A pairs-to-`bits` prefilter was DELIBERATELY NOT implemented: dropping a
//! pair that touches a bit absent from `bits` is output-UNOBSERVABLE by
//! construction (an enumerated mask only carries present bits, so such a pair
//! can never fire) — every mutant of such a filter is therefore provably
//! equivalent, an unkillable-survivor factory under the zero-missed
//! cargo-mutants gate — while its win is nil at the production 26-pair table.
//! Same reshape-away-the-equivalent-class discipline as the
//! `below_parallel_threshold` extraction (DEC-MUTATION-RUNTIME-003).
//!
//! ## Failure mode — `Result`, NEVER a panic
//!
//! Under the crate's `overflow-checks = true` + `panic = "abort"` release
//! profile an unchecked overflow / OOB index / failed alloc would SIGABRT the
//! whole host interpreter. Every shift is preceded by the `< 128` validation;
//! the binomial count is checked u64 arithmetic; the survivor buffer is
//! `try_reserve_exact` (failure → a clean Python `MemoryError`, the
//! global-stop class); no `unwrap` on fallible ops; no unestablished indexing.
//!
//! ## Complexity (honest envelope — Golden Rule #10)
//!
//! One call is `O(C(n_unique, k) · (k + n_pairs))` time and `O(survivors)`
//! space — the same work class as the Python loop it replaces (a
//! constant-factor win only; claiming an O-class change is FORBIDDEN). The
//! Python wrapper caps `C(n_unique, k)` at `ENUM_KERNEL_MAX_MASKS` (2²⁴)
//! before calling; the `max_masks` parameter here is the belt for direct
//! library callers. GIL held, serial (the recorded crate stance — no
//! `allow_threads`; rayon order-preserving par-collect is a named follow-up).

use pyo3::exceptions::{PyMemoryError, PyValueError};
use pyo3::prelude::*;

/// Failed-allocation marker prefix — the ONE token [`sweep_enum_conflict_free`]
/// keys the `PyMemoryError` mapping on (every other core `Err` is a
/// `PyValueError`). Kept `pub(crate)` so the shim and the tests share the
/// single literal (a drifted copy could silently downgrade an OOM to a
/// catchable `ValueError`, defeating the global-stop law).
pub(crate) const ALLOC_ERR_PREFIX: &str = "sweep_enum_conflict_free: allocation";

/// Reserve the survivor buffer up front — `try_reserve_exact`, never a bare
/// `Vec::with_capacity` (whose alloc failure would abort the interpreter
/// under `panic = "abort"`). Split out so the failure arm is directly
/// unit-testable (`u64::MAX` masks × 16 bytes overflows `isize` — a
/// deterministic `Err` with no allocator round-trip).
pub(crate) fn reserve_survivors(expected: u64) -> Result<Vec<u128>, String> {
    let mut out: Vec<u128> = Vec::new();
    out.try_reserve_exact(expected as usize)
        .map_err(|e| format!("{ALLOC_ERR_PREFIX} of {expected} survivor masks failed: {e}"))?;
    Ok(out)
}

/// Checked binomial coefficient `C(n, k)` in u64 (`0 <= k <= n` is the
/// caller's contract). The incremental product `acc(i) = C(n, i + 1)` is
/// exact at every step (`C(n, i) · (n − i)` is divisible by `i + 1`);
/// symmetry (`k → min(k, n−k)`) bounds the intermediates at
/// `C(n, k) · k`, so a count whose intermediates overflow u64 returns a
/// clean `Err` — unreachable through the Python wrapper (which pre-caps the
/// count at 2²⁴ via exact bigint `math.comb`), a belt for direct callers.
pub(crate) fn checked_binomial(n: usize, k: usize) -> Result<u64, String> {
    debug_assert!(k <= n);
    let k_sym = k.min(n - k);
    let mut acc: u64 = 1;
    for i in 0..k_sym {
        acc = acc.checked_mul((n - i) as u64).ok_or_else(|| {
            format!(
                "sweep_enum_conflict_free: C({n}, {k}) overflows the u64 \
                 count (intermediate at term {i})"
            )
        })?;
        acc /= (i as u64) + 1;
    }
    Ok(acc)
}

/// Pure core of [`sweep_enum_conflict_free`] — `Result<_, String>` (NO
/// `PyErr`) so the no-libpython `cargo test` link exercises every branch
/// (the [`crate::python::add_u64`] split). Returns
/// `(survivors, enumerated, conflict_pruned)` with survivors in
/// `itertools.combinations` lexicographic order (the module-docstring order
/// law).
pub(crate) fn enum_conflict_free_checked(
    bits: &[u8],
    k: usize,
    conflict_pairs: &[(u8, u8)],
    max_masks: u64,
) -> Result<(Vec<u128>, u64, u64), String> {
    // Normalise exactly as Python does: `sorted(set(bits))`. Validation runs
    // on the sorted-deduped list so the FIRST-named offending position
    // matches `iter_masks_k_of_bits`'s ascending-scan message ordering.
    let mut unique: Vec<u8> = bits.to_vec();
    unique.sort_unstable();
    unique.dedup();
    for &pos in &unique {
        if pos >= 128 {
            return Err(format!(
                "sweep_enum_conflict_free: position {pos} out of range [0, 128)"
            ));
        }
    }
    let n = unique.len();
    if k > n {
        return Err(format!(
            "sweep_enum_conflict_free: invalid k={k} for {n} unique bits \
             (require 0 <= k <= n)"
        ));
    }
    for &(a, b) in conflict_pairs {
        if a >= b || b >= 128 {
            return Err(format!(
                "sweep_enum_conflict_free: malformed conflict pair ({a}, {b}) \
                 (require a < b < 128)"
            ));
        }
    }

    let expected = checked_binomial(n, k)?;
    if expected > max_masks {
        return Err(format!(
            "sweep_enum_conflict_free: enumerated {expected} exceeds \
             max_masks {max_masks}"
        ));
    }

    // Every shift below is proven `< 128` by the position validation above
    // (FM-2) — no reachable shift overflow under overflow-checks = true.
    let pos_mask: Vec<u128> = unique.iter().map(|&p| 1u128 << p).collect();
    // Pair masks: `a < b` is validated above, so the two set bits are
    // DISTINCT powers of two and `+` is exactly `|` — chosen over `|` because
    // an `|`→`^` mutant on disjoint operands is provably equivalent
    // (unkillable), while `+`'s `-`/`*` mutants underflow/mis-set a bit and
    // are killed by the pair tests (the module-docstring reshape discipline).
    // Duplicate pairs are tolerated (frozenset semantics: `any()` over
    // duplicates is unchanged); no pairs-to-bits prefilter (module docstring).
    let cm: Vec<u128> = conflict_pairs
        .iter()
        .map(|&(a, b)| (1u128 << a) + (1u128 << b))
        .collect();

    let mut out = reserve_survivors(expected)?;
    let mut enumerated: u64 = 0;
    let mut conflict_pruned: u64 = 0;

    if k == 0 {
        // The single empty combination — mask 0, never conflicting (both
        // pair bits unset). Matches Python's `[0]`, enumerated=1, pruned=0.
        out.push(0);
        debug_assert_eq!(expected, 1);
        return Ok((out, 1, 0));
    }

    // The k-position odometer — a literal transcription of CPython's
    // `itertools.combinations` advance, yielding position tuples in
    // lexicographic order over the sorted unique bits (the order law).
    let mut idx: Vec<usize> = (0..k).collect();
    loop {
        // O(k) mask rebuild (k <= 6 in production; incremental-mask
        // micro-optimisation is a named follow-up).
        let mut mask: u128 = 0;
        for &i in &idx {
            mask |= pos_mask[i];
        }
        enumerated += 1;
        // The literal `has_conflict` transcription: a pair fires iff BOTH its
        // bits are set, i.e. `mask & pair_mask == pair_mask` (a SUBSET test
        // per element — clippy's `manual_contains` suggestion `contains(&(mask
        // & c))` is not equivalent: it would search for one VALUE, not apply
        // the per-element subset predicate).
        #[allow(clippy::manual_contains)]
        if cm.iter().any(|&c| mask & c == c) {
            conflict_pruned += 1;
        } else {
            out.push(mask);
        }
        // Advance: rightmost slot i with idx[i] != i + n - k; exhausted when
        // no such slot exists (== itertools.combinations termination).
        let mut slot = k;
        for i in (0..k).rev() {
            if idx[i] != i + n - k {
                slot = i;
                break;
            }
        }
        if slot == k {
            break;
        }
        idx[slot] += 1;
        for j in (slot + 1)..k {
            idx[j] = idx[j - 1] + 1;
        }
    }
    debug_assert_eq!(enumerated, expected);
    Ok((out, enumerated, conflict_pruned))
}

/// Fused k-of-bits enumeration + conflict-pair prune — the sweep-enumeration
/// kernel (see the module docstring).
///
/// Returns `(survivors, enumerated, conflict_pruned)`: the conflict-free
/// masks in the EXACT `iter_masks_k_of_bits` lexicographic order, the total
/// combination count, and the pruned tally
/// (`enumerated == len(survivors) + conflict_pruned`). Raises `ValueError`
/// on any input outside the validated envelope and `MemoryError` when the
/// survivor buffer cannot be reserved; the Python wrapper routes a
/// `ValueError` to the bit-exact Python oracle and re-raises `MemoryError`
/// bare (the global-stop law).
#[pyfunction]
pub fn sweep_enum_conflict_free(
    bits: Vec<u8>,
    k: usize,
    conflict_pairs: Vec<(u8, u8)>,
    max_masks: u64,
) -> PyResult<(Vec<u128>, u64, u64)> {
    enum_conflict_free_checked(&bits, k, &conflict_pairs, max_masks).map_err(|msg| {
        if msg.starts_with(ALLOC_ERR_PREFIX) {
            PyMemoryError::new_err(msg)
        } else {
            PyValueError::new_err(msg)
        }
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Naive in-crate reference — DELIBERATELY different mechanics from the
    /// kernel: recursive combination generation (not the odometer) + the
    /// literal shift-based `has_conflict` transcription (not the pair-mask
    /// subset test). The differential oracle for order, set and counters.
    fn reference(bits: &[u8], k: usize, pairs: &[(u8, u8)]) -> (Vec<u128>, u64, u64) {
        let mut unique: Vec<u8> = bits.to_vec();
        unique.sort_unstable();
        unique.dedup();
        let mut combos: Vec<Vec<u8>> = Vec::new();
        fn recurse(
            unique: &[u8],
            k: usize,
            start: usize,
            cur: &mut Vec<u8>,
            out: &mut Vec<Vec<u8>>,
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
        let mut survivors = Vec::new();
        let mut enumerated = 0u64;
        let mut pruned = 0u64;
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
            } else {
                survivors.push(mask);
            }
        }
        (survivors, enumerated, pruned)
    }

    // ── literal pins ─────────────────────────────────────────────────────

    #[test]
    fn doctest_pin_bits_0_3_5_k2() {
        // iter_masks_k_of_bits([0, 3, 5], 2) == [9, 33, 40] (the Python doctest).
        let (survivors, enumerated, pruned) =
            enum_conflict_free_checked(&[0, 3, 5], 2, &[], u64::MAX).expect("valid");
        assert_eq!(survivors, vec![9, 33, 40]);
        assert_eq!((enumerated, pruned), (3, 0));
    }

    #[test]
    fn lex_order_witness_is_not_numeric_order() {
        // n=4, k=2 position-lex masks: 3, 5, 9, 6, 10, 12 — 9 BEFORE 6.
        // A Gosper/numeric-ascending kernel yields 3, 5, 6, 9, 10, 12 and
        // MUST fail here (FM-1).
        let (survivors, ..) =
            enum_conflict_free_checked(&[0, 1, 2, 3], 2, &[], u64::MAX).expect("valid");
        assert_eq!(survivors, vec![3, 5, 9, 6, 10, 12]);
    }

    #[test]
    fn input_order_and_duplicates_are_normalised() {
        // [5, 0, 3] and [0, 3, 5, 5, 0] both normalise to sorted-unique.
        let a = enum_conflict_free_checked(&[5, 0, 3], 2, &[], u64::MAX).expect("valid");
        let b = enum_conflict_free_checked(&[0, 3, 5, 5, 0], 2, &[], u64::MAX).expect("valid");
        assert_eq!(a, b);
        assert_eq!(a.0, vec![9, 33, 40]);
        // Duplicates collapse: [5, 5, 5] has ONE unique bit.
        let (survivors, enumerated, pruned) =
            enum_conflict_free_checked(&[5, 5, 5], 1, &[], u64::MAX).expect("valid");
        assert_eq!(survivors, vec![32]);
        assert_eq!((enumerated, pruned), (1, 0));
    }

    #[test]
    fn k_zero_yields_the_single_empty_mask() {
        // Matches Python: [0], enumerated=1, pruned=0 — even with pairs
        // present (mask 0 never conflicts) and even for EMPTY bits.
        let (survivors, enumerated, pruned) =
            enum_conflict_free_checked(&[7, 2], 0, &[(2, 7)], u64::MAX).expect("valid");
        assert_eq!(survivors, vec![0]);
        assert_eq!((enumerated, pruned), (1, 0));
        let (survivors, enumerated, pruned) =
            enum_conflict_free_checked(&[], 0, &[], u64::MAX).expect("valid");
        assert_eq!(survivors, vec![0]);
        assert_eq!((enumerated, pruned), (1, 0));
    }

    #[test]
    fn k_equals_n_unique_single_mask_and_full_prune() {
        // k == n_unique yields the single all-bits mask …
        let (survivors, enumerated, pruned) =
            enum_conflict_free_checked(&[1, 4], 2, &[], u64::MAX).expect("valid");
        assert_eq!(survivors, vec![0b10010]);
        assert_eq!((enumerated, pruned), (1, 0));
        // … and a conflicting pair prunes it to an EMPTY survivor list.
        let (survivors, enumerated, pruned) =
            enum_conflict_free_checked(&[1, 4], 2, &[(1, 4)], u64::MAX).expect("valid");
        assert!(survivors.is_empty());
        assert_eq!((enumerated, pruned), (1, 1));
    }

    #[test]
    fn u128_reach_bits_63_64_127() {
        let (survivors, enumerated, pruned) =
            enum_conflict_free_checked(&[63, 64, 127], 2, &[], u64::MAX).expect("valid");
        assert_eq!(
            survivors,
            vec![
                (1u128 << 63) | (1u128 << 64),
                (1u128 << 63) | (1u128 << 127),
                (1u128 << 64) | (1u128 << 127),
            ]
        );
        assert_eq!((enumerated, pruned), (3, 0));
        // A high-limb pair prunes across the 64-bit boundary.
        let (survivors, _, pruned) =
            enum_conflict_free_checked(&[63, 64, 127], 2, &[(64, 127)], u64::MAX).expect("valid");
        assert_eq!(
            survivors,
            vec![
                (1u128 << 63) | (1u128 << 64),
                (1u128 << 63) | (1u128 << 127)
            ]
        );
        assert_eq!(pruned, 1);
    }

    #[test]
    fn bit_127_alone_round_trips() {
        let (survivors, enumerated, pruned) =
            enum_conflict_free_checked(&[127], 1, &[], u64::MAX).expect("valid");
        assert_eq!(survivors, vec![1u128 << 127]);
        assert_eq!((enumerated, pruned), (1, 0));
    }

    // ── boundary validation (Result, never a panic) ──────────────────────

    #[test]
    fn out_of_range_position_is_rejected_naming_the_smallest_offender() {
        let err = enum_conflict_free_checked(&[200, 5, 128], 1, &[], u64::MAX).unwrap_err();
        // Sorted-deduped scan names 128 (the smallest >= 128), matching the
        // Python message ordering.
        assert!(err.contains("position 128 out of range [0, 128)"), "{err}");
    }

    #[test]
    fn k_beyond_n_unique_is_rejected() {
        let err = enum_conflict_free_checked(&[0, 1], 3, &[], u64::MAX).unwrap_err();
        assert!(err.contains("invalid k=3 for 2 unique bits"), "{err}");
        // Duplicates collapse BEFORE the k check (set semantics).
        let err = enum_conflict_free_checked(&[5, 5], 2, &[], u64::MAX).unwrap_err();
        assert!(err.contains("invalid k=2 for 1 unique bits"), "{err}");
    }

    #[test]
    fn malformed_pairs_are_rejected() {
        for pair in [(3u8, 3u8), (5, 2), (0, 128), (0, 255)] {
            let err = enum_conflict_free_checked(&[0, 1], 1, &[pair], u64::MAX).unwrap_err();
            assert!(err.contains("malformed conflict pair"), "{pair:?}: {err}");
        }
        // Duplicate WELL-FORMED pairs are tolerated (frozenset semantics).
        let (survivors, _, pruned) =
            enum_conflict_free_checked(&[0, 1], 2, &[(0, 1), (0, 1)], u64::MAX).expect("valid");
        assert!(survivors.is_empty());
        assert_eq!(pruned, 1);
    }

    #[test]
    fn count_overflow_is_a_clean_err() {
        // C(128, 64) overflows u64 — checked_binomial must Err, never abort.
        let bits: Vec<u8> = (0..128).collect();
        let err = enum_conflict_free_checked(&bits, 64, &[], u64::MAX).unwrap_err();
        assert!(err.contains("overflows the u64 count"), "{err}");
    }

    #[test]
    fn ceiling_refusal_names_both_numbers() {
        // C(6, 3) = 20 > max_masks 10 → Err naming both.
        let err = enum_conflict_free_checked(&[0, 1, 2, 3, 4, 5], 3, &[], 10).unwrap_err();
        assert!(err.contains("enumerated 20 exceeds max_masks 10"), "{err}");
    }

    #[test]
    fn ceiling_boundary_is_admissible() {
        // expected == max_masks is ADMITTED (strict `>` refusal law): kills
        // the `>`→`>=` boundary mutant the first cargo-mutants run surfaced.
        let (survivors, enumerated, _) =
            enum_conflict_free_checked(&[0, 1, 2, 3, 4, 5], 3, &[], 20).expect("at the ceiling");
        assert_eq!(enumerated, 20);
        assert_eq!(survivors.len(), 20);
        // One below the count refuses.
        assert!(enum_conflict_free_checked(&[0, 1, 2, 3, 4, 5], 3, &[], 19).is_err());
    }

    #[test]
    fn reserve_survivors_alloc_failure_is_a_clean_err() {
        // u64::MAX masks × 16 bytes overflows isize — a deterministic
        // capacity-overflow Err with NO allocator round-trip (the arm the
        // shim maps to PyMemoryError; unreachable through the Python
        // wrapper's 2^24 ceiling — the direct-caller belt).
        let err = reserve_survivors(u64::MAX).unwrap_err();
        assert!(err.starts_with(ALLOC_ERR_PREFIX), "{err}");
        // The happy arm reserves exactly.
        let out = reserve_survivors(16).expect("small reserve");
        assert!(out.capacity() >= 16);
        assert!(out.is_empty());
    }

    // ── checked_binomial pins ────────────────────────────────────────────

    #[test]
    fn binomial_known_values() {
        assert_eq!(checked_binomial(60, 0), Ok(1));
        assert_eq!(checked_binomial(60, 1), Ok(60));
        assert_eq!(checked_binomial(60, 2), Ok(1_770));
        assert_eq!(checked_binomial(60, 3), Ok(34_220));
        assert_eq!(checked_binomial(60, 5), Ok(5_461_512));
        assert_eq!(checked_binomial(60, 60), Ok(1));
        assert_eq!(checked_binomial(0, 0), Ok(1));
        // Symmetry keeps large-k counts exact.
        assert_eq!(checked_binomial(60, 55), checked_binomial(60, 5));
    }

    #[test]
    fn binomial_symmetry_is_load_bearing_for_large_k() {
        // C(80, 70) = C(80, 10) = 1,646,492,110,120 (independently computed
        // via Python `math.comb`). WITHOUT the `k.min(n - k)` symmetry the
        // unsymmetrised iteration (k_sym = 70) reaches intermediates around
        // C(80, 40)·41 ≈ 4.4e24 ≫ u64::MAX and must overflow-Err — so this
        // pin kills BOTH the `n - k`→`n + k` and the `min`→`max` mutants the
        // first cargo-mutants run surfaced (each degrades symmetry away).
        assert_eq!(checked_binomial(80, 70), Ok(1_646_492_110_120));
    }

    // ── differential vs the naive reference ──────────────────────────────

    #[test]
    fn matches_reference_on_exhaustive_small_matrix() {
        // Every n <= 7, 0 <= k <= n, with a pair table spanning the palette.
        let pairs = [(0u8, 1u8), (2, 3), (1, 5)];
        for n in 0..=7u8 {
            let bits: Vec<u8> = (0..n).collect();
            for k in 0..=(n as usize) {
                let kernel = enum_conflict_free_checked(&bits, k, &pairs, u64::MAX).expect("valid");
                let oracle = reference(&bits, k, &pairs);
                assert_eq!(kernel, oracle, "n={n} k={k}");
            }
        }
    }

    // ── proptest: kernel == naive reference over random inputs ───────────

    use proptest::prelude::*;

    proptest! {
        #[test]
        fn prop_kernel_matches_reference(
            bits in proptest::collection::vec(0u8..128, 0..12),
            k_raw in 0usize..8,
            pairs in proptest::collection::vec(
                (0u8..127, 1u8..128), 0..8
            ),
        ) {
            // Clamp k into the valid range for the deduped bit set; keep
            // pairs well-formed (a < b) by swapping and skipping equals.
            let mut unique = bits.clone();
            unique.sort_unstable();
            unique.dedup();
            let k = k_raw % (unique.len() + 1);
            let pairs: Vec<(u8, u8)> = pairs
                .into_iter()
                .filter(|(a, b)| a != b)
                .map(|(a, b)| (a.min(b), a.max(b)))
                .collect();
            let kernel = enum_conflict_free_checked(&bits, k, &pairs, u64::MAX)
                .expect("valid inputs by construction");
            let oracle = reference(&bits, k, &pairs);
            // List equality asserts SET ∧ ORDER in one assertion (the
            // reference derives its prune from raw shifts, the kernel from
            // pair masks — agreement proves the two formulations identical).
            prop_assert_eq!(kernel.0, oracle.0);
            prop_assert_eq!(kernel.1, oracle.1);
            prop_assert_eq!(kernel.2, oracle.2);
            // Σ law: enumerated == C(n_unique, k).
            prop_assert_eq!(
                kernel.1,
                checked_binomial(unique.len(), k).expect("small count")
            );
        }
    }
}
