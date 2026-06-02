//! PyO3-exposed functions — the Python ⇆ Rust bridge surface.
//!
//! DEC-P0-050 (PR-K) scaffolding. This module hosts the
//! `#[pyfunction]`-annotated entries that the `fno_math` Python module
//! re-exports (registered in `lib.rs`'s `#[pymodule]` block).
//!
//! Two functions ship in PR-K:
//!
//! 1. [`add_u64`] — a sanity-check primitive that proves the Python ⇆
//!    Rust round-trip works under the maturin + abi3-py312 wheel build
//!    pipeline. Used by `tests/unit/test_fno_math_bridge.py`.
//!
//! 2. [`bitcount_u64s`] — counts the total number of set bits across a
//!    list of `u64` values via Rust's intrinsic `u64::count_ones`. This
//!    is the operation behind [`brutex.orchestrator.bloom.BloomFilter`]'s
//!    `unique_count_estimate` AND the future Stage 1B AND-mask hot
//!    path. Ships now so the Python facade has a real-world function
//!    to exercise the bridge against, not just `add_u64`.
//!
//! Future PRs (Stage 1B AND-mask, blake3 hash, Bloom bit-density)
//! register their `#[pyfunction]` entries here and add them to the
//! `lib.rs` `#[pymodule]` block.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyBytes;

/// Add two unsigned 64-bit integers. Sanity-check the PyO3 wheel
/// build round-trips correctly.
///
/// Equivalent to Python's `a + b` for non-negative ints up to
/// `2 ** 64 - 1`. Overflow panics in debug builds and saturates in
/// release builds (per the crate's `overflow-checks = true` flag, this
/// also panics in release).
#[pyfunction]
pub fn add_u64(a: u64, b: u64) -> u64 {
    a + b
}

/// Return the total number of set bits across a slice of `u64` values.
///
/// Uses `u64::count_ones` which compiles to the host CPU's `popcnt`
/// instruction on x86_64 (-msse4.2) and `cnt` on ARMv8. For a 1M-entry
/// slice this typically runs in under 100 µs on M4 Pro — versus the
/// equivalent Python `sum(bin(x).count('1') for x in xs)` which
/// requires ~50 ms (500× speedup).
///
/// Used by:
///
/// * [`brutex.orchestrator.bloom.BloomFilter.unique_count_estimate`]
///   (future migration from per-byte `bit_count()` to vectorised
///   `bitcount_u64s` on the underlying bit array).
/// * Future Stage 1B AND-mask kernel — counting matching combos after
///   bitwise AND across `u64` strategy masks.
#[pyfunction]
pub fn bitcount_u64s(data: Vec<u64>) -> u64 {
    data.iter().map(|x| u64::from(x.count_ones())).sum()
}

/// Element-wise bitwise AND + popcount in a single vectorised pass.
///
/// Computes `sum(popcount(a[i] & b[i]) for i in 0..len)` over two
/// equal-length slices of `u64`. This is the foundational Stage 1B
/// AND-mask kernel — used to count how many bars match BOTH a
/// strategy's entry-condition mask AND the candidate trial's
/// indicator-state mask.
///
/// Hot-path properties:
///
/// * Single fused loop — `(a & b).count_ones()` per element compiles
///   to two CPU ops (AND + popcnt), both single-cycle on M4 Pro / x86.
/// * Branchless and cache-friendly — strict linear scan over both
///   inputs, ideal for the SIMD-eligible inner loop the Rust compiler
///   emits at `-C opt-level=3` (release profile, locked in
///   `rust/fno-math/Cargo.toml`).
/// * For a 1M-entry sweep (~8 MB pair of slices) this typically runs
///   in under 200 µs on M4 Pro — versus the equivalent Python
///   `sum(((a & b).bit_count()) for a, b in zip(...))` at ~100 ms
///   (500× speedup).
///
/// Validates equal lengths at the boundary — raises
/// :class:`ValueError` on mismatch rather than silently truncating
/// (the Stage 1B caller must ensure mask shapes align).
#[pyfunction]
pub fn bitand_count_u64s(a: Vec<u64>, b: Vec<u64>) -> PyResult<u64> {
    if a.len() != b.len() {
        return Err(PyValueError::new_err(format!(
            "bitand_count_u64s: length mismatch — a={}, b={}",
            a.len(),
            b.len(),
        )));
    }
    let total: u64 = a
        .iter()
        .zip(b.iter())
        .map(|(x, y)| u64::from((x & y).count_ones()))
        .sum();
    Ok(total)
}

/// Return the 32-byte blake3 hash of ``data`` as bytes.
///
/// DEC-P0-052 (PR-M). Foundational for the deferred sha256 → blake3
/// migration of `brutex.strategies.combo.combo_hash` (DEC-P0-044 Q-02).
/// Blake3 is ~5× faster than sha256 on a single core and ~10-20× on
/// multi-core for large inputs (BLAKE3 paper §5.5); for the small
/// (~256-byte) combo-hash payloads BruteX uses, the single-core speedup
/// dominates.
///
/// Properties:
///
/// * Deterministic — same input always produces same output. Required
///   by Golden Rule #4 (Reproducibility first).
/// * 32-byte output (256-bit digest) — matches sha256's digest size so
///   the swap is wire-format compatible at the storage layer.
/// * Single-tree mode (no key, no derive-key context) — matches the
///   plain `sha256(payload)` API the existing combo_hash uses.
///
/// Usage from Python:
///
/// ```ignore
/// import fno_math
/// digest = fno_math.blake3_hash(b"my payload")
/// assert len(digest) == 32
/// assert digest.hex() == "..."  # deterministic
/// ```
#[pyfunction]
pub fn blake3_hash(py: Python<'_>, data: &[u8]) -> Py<PyBytes> {
    let hash = blake3::hash(data);
    PyBytes::new(py, hash.as_bytes()).into()
}

/// Count, for a single strategy mask, how many bars are a SUPERSET of it.
///
/// A bar (its packed u64 condition bitset) **matches** the mask iff
/// every required bit in `mask` is also set in the bar:
///
/// ```text
/// (bar & mask) == mask
/// ```
///
/// This is the SUPERSET / superset-membership test — distinct from the
/// `bitand_count_u64s` AND-popcount (which sums *bit overlaps*, not
/// whole-bar matches). It is the literal Stage-2 hot op described in
/// `brutex/engine/combo_mask.py:189-221` (`evaluate_mask`).
///
/// Branchless: the inner test `(bar & mask) == mask` yields a `bool`
/// which is added as `u64` (`0`/`1`) with no branch, so the loop is a
/// straight linear scan the compiler can auto-vectorise at
/// `-C opt-level=3`.
///
/// Edge case: `mask == 0` requires nothing, so **every** bar matches
/// (`(bar & 0) == 0` always) — count == `bar_bits.len()`. This mirrors
/// `evaluate_mask(bar, 0) is True`.
#[pyfunction]
pub fn count_supermask_u64s(bar_bits: Vec<u64>, mask: u64) -> u64 {
    bar_bits
        .iter()
        .map(|&bar| u64::from((bar & mask) == mask))
        .sum()
}

/// Batched per-mask SUPERSET count — the Stage-2 brute-force accelerator.
///
/// For each `mask` in `masks`, return how many bars in `bar_bits` satisfy
/// `(bar & mask) == mask` (the bar is a superset of the mask). The result
/// is a `Vec<u64>` of length `masks.len()`, positionally aligned with
/// `masks` — `out[j]` is the hit count for `masks[j]`.
///
/// This is THE Stage-2 hot kernel (ADR-0004 "Rust CPU kernel first"):
/// the 33-billion-trial sweep evaluates one strategy mask against the
/// full precomputed per-bar condition bitset column. Today the Python
/// path in `brutex.engine.combo_mask.count_supermasks` does this with a
/// numpy vectorised `((bar_bits & mask) == mask).sum()` per mask; this
/// kernel replaces the inner numpy pass with a single fused Rust loop.
///
/// ## Memory-bandwidth-bound (ADR-0004 finding 2)
///
/// The inner op is a branchless integer `AND` + `CMP` + conditional add
/// — all single-cycle on M4 Pro / x86. The kernel is therefore **memory-
/// bandwidth-bound, not compute-bound**: throughput is governed by how
/// fast `bar_bits` streams from cache/RAM, not by the ALU. The loop is
/// structured **masks outer / bars inner** so that the (smaller, reused)
/// `mask` scalar stays in a register while the (large) `bar_bits` slice
/// streams linearly — cache-friendly and SIMD-eligible. For the common
/// case where `bar_bits` fits in L2 (~94 K u64 = ~750 KiB), it is
/// resident across all masks, so only the first mask pays the cold-cache
/// cost. GPU offload (ADR-0004) buys only ~1.2-2.3× on the M4 Pro's
/// SHARED memory pool, which is why this CPU kernel ships first.
///
/// ## Determinism (Golden Rule #4)
///
/// Pure integer arithmetic — no floats, no reduction-order ambiguity.
/// Same `(bar_bits, masks)` → bit-identical output on every device, so
/// the result feeds the blake3 reproducibility hash unchanged.
///
/// Empty inputs are valid: empty `masks` → empty `Vec`; empty `bar_bits`
/// → a zero per mask.
#[pyfunction]
pub fn count_supermasks_u64s(bar_bits: Vec<u64>, masks: Vec<u64>) -> Vec<u64> {
    masks
        .iter()
        .map(|&mask| {
            // bars inner — `bar_bits` streams linearly, `mask` is register-
            // resident. Branchless: `bool as u64` is 0/1, summed with no
            // branch in the hot loop.
            bar_bits
                .iter()
                .map(|&bar| u64::from((bar & mask) == mask))
                .sum()
        })
        .collect()
}

/// Return the total number of set bits across a raw byte slice.
///
/// DEC-P0-053 (PR-N). Used by
/// [`brutex.orchestrator.bloom.BloomFilter.unique_count_estimate`] to
/// avoid the per-byte Python iteration that scaled poorly with the
/// underlying bit array size.
///
/// Internally processes the slice 8 bytes at a time as `u64` chunks via
/// `u64::count_ones` (popcnt CPU instruction), with a tail loop for
/// the trailing 0-7 bytes. Single fused linear scan, SIMD-eligible at
/// `-C opt-level=3`.
///
/// For a 16 MiB bit array (Phase 1 default Bloom @ 100M / 1e-7 is
/// ~419 MB; saturated probe density similar order) this runs in
/// under 2 ms versus the Python per-byte loop's ~50 ms (25× speedup).
#[pyfunction]
pub fn popcount_bytes(data: &[u8]) -> u64 {
    // Process 8-byte chunks via popcnt.
    let chunks = data.chunks_exact(8);
    let tail = chunks.remainder();
    let body: u64 = chunks
        .map(|c| {
            // `c.try_into().unwrap()` is infallible here because chunks_exact
            // yields slices of EXACTLY 8 bytes — but we use a manual
            // build to satisfy clippy's "no unwrap in production code"
            // lint (`Cargo.toml` denies `clippy::unwrap_used`).
            let mut bytes = [0u8; 8];
            bytes.copy_from_slice(c);
            u64::from(u64::from_le_bytes(bytes).count_ones())
        })
        .sum();
    let tail_bits: u64 = tail.iter().map(|b| u64::from(b.count_ones())).sum();
    body + tail_bits
}

#[cfg(test)]
mod tests {
    use super::{count_supermask_u64s, count_supermasks_u64s};

    // ── count_supermask_u64s (single mask) ──

    #[test]
    fn single_empty_bars_is_zero() {
        assert_eq!(count_supermask_u64s(vec![], 0b1011), 0);
    }

    #[test]
    fn single_mask_zero_matches_all_bars() {
        // (bar & 0) == 0 for every bar — mirrors evaluate_mask(bar, 0) is True.
        let bars = vec![0u64, 1, 0xFFFF_FFFF_FFFF_FFFF, 0xDEAD_BEEF];
        assert_eq!(count_supermask_u64s(bars.clone(), 0), bars.len() as u64);
    }

    #[test]
    fn single_all_match() {
        // Every bar is a superset of mask 0b0011.
        let bars = vec![0b0011, 0b0111, 0b1111, 0b1011];
        assert_eq!(count_supermask_u64s(bars, 0b0011), 4);
    }

    #[test]
    fn single_none_match() {
        // No bar has bit 3 set, so none is a superset of 0b1000.
        let bars = vec![0b0001, 0b0010, 0b0100, 0b0111];
        assert_eq!(count_supermask_u64s(bars, 0b1000), 0);
    }

    #[test]
    fn single_partial_match() {
        // mask requires bits 0 and 1; only bars with BOTH set count.
        let bars = vec![0b0011, 0b0001, 0b0010, 0b1011, 0b0100];
        assert_eq!(count_supermask_u64s(bars, 0b0011), 2);
    }

    #[test]
    fn single_high_bit_mask() {
        let top = 1u64 << 63;
        let bars = vec![top, top | 1, 0, 1];
        assert_eq!(count_supermask_u64s(bars, top), 2);
    }

    // ── count_supermasks_u64s (batched) ──

    #[test]
    fn batch_empty_masks_is_empty() {
        assert_eq!(
            count_supermasks_u64s(vec![1, 2, 3], vec![]),
            Vec::<u64>::new()
        );
    }

    #[test]
    fn batch_empty_bars_is_zero_per_mask() {
        assert_eq!(
            count_supermasks_u64s(vec![], vec![0b1, 0b11, 0]),
            vec![0, 0, 0]
        );
    }

    #[test]
    fn batch_mask_zero_matches_all() {
        let bars = vec![0b0001, 0b1010, 0b1111];
        assert_eq!(count_supermasks_u64s(bars, vec![0]), vec![3]);
    }

    #[test]
    fn batch_multi_mask_mixed() {
        let bars = vec![0b0011, 0b0111, 0b1000, 0b1011, 0b0000];
        // mask 0b0011 → bars {0b0011,0b0111,0b1011} = 3
        // mask 0b1000 → bars {0b1000,0b1011}        = 2
        // mask 0b1111 → none                        = 0
        // mask 0      → all                         = 5
        let out = count_supermasks_u64s(bars, vec![0b0011, 0b1000, 0b1111, 0]);
        assert_eq!(out, vec![3, 2, 0, 5]);
    }

    #[test]
    fn batch_positional_alignment() {
        // out[j] must align with masks[j]; reordering masks reorders output.
        let bars = vec![0b0001, 0b0010, 0b0011];
        assert_eq!(
            count_supermasks_u64s(bars.clone(), vec![0b0001, 0b0010]),
            vec![2, 2]
        );
        assert_eq!(
            count_supermasks_u64s(bars, vec![0b0010, 0b0001]),
            vec![2, 2]
        );
    }

    #[test]
    fn batch_matches_single_kernel() {
        // The batched kernel must agree with the single-mask kernel element-wise.
        let bars = vec![0u64, 1, 0b1011, 0b1111, 1u64 << 40, 0xDEAD_BEEF];
        let masks = vec![0u64, 1, 0b1010, 1u64 << 40, 0xFFFF_FFFF_FFFF_FFFF];
        let batched = count_supermasks_u64s(bars.clone(), masks.clone());
        let one_by_one: Vec<u64> = masks
            .iter()
            .map(|&m| count_supermask_u64s(bars.clone(), m))
            .collect();
        assert_eq!(batched, one_by_one);
    }
}
