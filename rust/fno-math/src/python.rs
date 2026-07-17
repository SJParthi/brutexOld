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

use crate::mask_word::MaskWord;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use rayon::prelude::*;

/// Add two unsigned 64-bit integers. Sanity-check the PyO3 wheel
/// build round-trips correctly.
///
/// Equivalent to Python's `a + b` for non-negative ints whose SUM fits
/// `2 ** 64 - 1`. Overflow-guarded at the FFI edge (R2-RC-F1): a sum
/// exceeding `u64::MAX` raises a clean Python `ValueError` instead of
/// tripping the crate's `overflow-checks = true` + `panic = "abort"`
/// release profile — which would SIGABRT the WHOLE host interpreter
/// (exit 134, uncatchable from Python). Mirrors the sibling
/// trading-core boundary-validation idiom (`MAX_ANCHOR_PAISA` /
/// `check_prev_day_hl` in `rust/trading-core/src/python.rs`).
#[pyfunction]
pub fn add_u64(a: u64, b: u64) -> PyResult<u64> {
    add_u64_checked(a, b).map_err(PyValueError::new_err)
}

/// Pure-Rust core of [`add_u64`] — `checked_add` returning a plain
/// `Result<_, String>` (NO `PyErr`), so the `#[cfg(test)]` suite can exercise
/// the overflow branch WITHOUT referencing Python C-API symbols under the
/// no-libpython `extension-module` `cargo test` link (the same split as
/// [`count_supermasks_scoped`]). The `#[pyfunction]` wrapper maps the error
/// to a `ValueError`.
fn add_u64_checked(a: u64, b: u64) -> Result<u64, String> {
    a.checked_add(b)
        .ok_or_else(|| format!("add_u64: {a} + {b} overflows u64 (max {})", u64::MAX))
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
    bitand_count_checked(&a, &b).map_err(PyValueError::new_err)
}

/// Pure-Rust core of [`bitand_count_u64s`] — `Result<_, String>` (NO `PyErr`)
/// so the `#[cfg(test)]` suite can exercise the AND-popcount loop AND the
/// length-mismatch reject WITHOUT referencing Python C-API symbols under the
/// no-libpython `extension-module` `cargo test` link (the same split as
/// [`add_u64_checked`] / [`count_supermasks_scoped`]; DEC-MUTATION-RUNTIME-003
/// — extracted so the `&`-vs-`|`/`^` and `!=`-vs-`==` cargo-mutants classes
/// are killable by in-file tests). The `#[pyfunction]` wrapper maps the error
/// to a `ValueError`, byte-identical message.
fn bitand_count_checked(a: &[u64], b: &[u64]) -> Result<u64, String> {
    if a.len() != b.len() {
        return Err(format!(
            "bitand_count_u64s: length mismatch — a={}, b={}",
            a.len(),
            b.len(),
        ));
    }
    Ok(a.iter()
        .zip(b.iter())
        .map(|(x, y)| u64::from((x & y).count_ones()))
        .sum())
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
    count_one(&bar_bits, mask)
}

/// SINGLE SOURCE OF TRUTH for the SUPERSET-count inner loop.
///
/// Counts how many bars in `bars` are a superset of `mask`
/// (`(bar & mask) == mask`). Branchless — `bool as u64` is `0`/`1`,
/// summed with no branch so the loop is a straight linear scan the
/// compiler auto-vectorises at `-C opt-level=3`. Reused by BOTH the free
/// `#[pyfunction]` kernels (`count_supermask_u64s` /
/// `count_supermasks_u64s`) AND the resident [`ResidentBars`] methods, so
/// the resident path can NEVER diverge from the shipped kernel
/// (DEC-RUST-RESIDENT-BARS-001 P0-3).
///
/// `mask == 0` requires nothing, so every bar matches and the result is
/// `bars.len()` (`(bar & 0) == 0` always). Empty `bars` → `0`.
///
/// Since DEC-ENUM-KERNEL-U128-001 this is the `W = u64` thin delegator onto
/// the width-generic [`count_one_core`] (the trade-sim `simulate_one_core`
/// delegator idiom) — kept under its original name + signature so every
/// pre-existing u64 call site (and test) compiles UNCHANGED; the compiler
/// monomorphizes the core back to the exact pre-generic code.
fn count_one(bars: &[u64], mask: u64) -> u64 {
    count_one_core(bars, mask)
}

/// Width-generic SUPERSET-count inner loop — the ONE core both mask widths
/// share (DEC-ENUM-KERNEL-U128-001, over the hoisted [`MaskWord`] trait).
/// The superset test is the trait's single method, so the u64 production
/// kernel and the u128 kernel run this exact loop: for `W = u64` the
/// compiler monomorphizes to the pre-generic [`count_one`] code (the
/// existing u64 suite passing UNMODIFIED is the frozen-behaviour gate); for
/// `W = u128` the compiler's two-limb lowering of the 128-bit `&`/`==` is
/// the "two-limb" evaluation — no hand-rolled limb pairs.
fn count_one_core<W: MaskWord>(bars: &[W], mask: W) -> u64 {
    bars.iter()
        .map(|&bar| u64::from(W::is_superset(bar, mask)))
        .sum()
}

/// Below this many `mask · bar` ops, run the batch scan SERIALLY.
///
/// DEC-RUST-RAYON-SUPERMASK-001 (MIGRATION.md #417). The masks-outer scan
/// is embarrassingly parallel, but for tiny batches the rayon fork/join +
/// work-stealing overhead would dominate AND would perturb the O(1) per-op
/// latency canary at small N (Golden Rule #5). The serial fast-path below
/// `2^20` (= 1 048 576) `mask·bar` ops keeps the small-N hot path identical
/// to the pre-rayon kernel; only genuinely large Stage-2 batches cross into
/// the parallel arm. `1 << 20` is one resident-bar window (~94 K) against
/// ~11 masks, or a few hundred masks against a small bar slice — well below
/// any real Stage-2 batch, so production always takes the parallel arm.
const PARALLEL_WORK_THRESHOLD: u64 = 1 << 20;

/// Dispatch the batched SUPERSET count to the serial or parallel arm.
///
/// DEC-RUST-RAYON-SUPERMASK-001 (MIGRATION.md #417). Both arms call ONLY the
/// shared [`count_one`] kernel (single source of truth), so they are
/// byte-identical by construction. The parallel arm uses
/// `par_iter().map().collect()` — an `IndexedParallelIterator`, which is
/// **order-preserving**: `out[j]` always aligns with `masks[j]` regardless of
/// thread scheduling, and the per-element values are pure integer counts (no
/// reduction-order ambiguity). The result is therefore **bit-identical to the
/// serial scan on every device** (Golden Rule #4 determinism), so it feeds the
/// blake3 run-identity hash unchanged. `par_bridge` is deliberately NOT used —
/// it is not order-preserving.
///
/// `saturating_mul` keeps the work estimate panic-free: with the release
/// profile's `overflow-checks = true` + `panic = "abort"`, an unchecked
/// `masks.len() * bars.len()` that overflowed `u64` would SIGABRT the host
/// process. Saturation simply caps at `u64::MAX` (which is `>= THRESHOLD`, so
/// such an enormous workload correctly takes the parallel arm).
///
/// Since DEC-ENUM-KERNEL-U128-001 this is the `W = u64` thin delegator onto
/// the width-generic [`count_supermasks_dispatch_core`] — original name +
/// signature kept so every pre-existing u64 call site (and test) compiles
/// UNCHANGED (the trade-sim delegator idiom).
#[inline]
fn count_supermasks_dispatch(bars: &[u64], masks: &[u64]) -> Vec<u64> {
    count_supermasks_dispatch_core(bars, masks)
}

/// Width-generic serial/parallel batch dispatch — the ONE dispatch both mask
/// widths share (DEC-ENUM-KERNEL-U128-001, same monomorphization argument as
/// [`count_one_core`]): the u64 AND u128 batch entries — free functions and
/// resident classes alike — route through here, so the serial/parallel
/// boundary and the order-preserving collect can never drift between widths.
#[inline]
fn count_supermasks_dispatch_core<W: MaskWord>(bars: &[W], masks: &[W]) -> Vec<u64> {
    let work = (masks.len() as u64).saturating_mul(bars.len() as u64);
    // Dispatch via the COMMON crate predicate (DEC-MUTATION-RUNTIME-003):
    // both arms are output-identical by design, so the boundary itself is
    // gated by `dispatch_tests::parallel_dispatch_boundary_is_exclusive`.
    if crate::below_parallel_threshold(work, PARALLEL_WORK_THRESHOLD) {
        masks.iter().map(|&m| count_one_core(bars, m)).collect()
    } else {
        masks.par_iter().map(|&m| count_one_core(bars, m)).collect()
    }
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
///
/// ## Multi-core (DEC-RUST-RAYON-SUPERMASK-001, MIGRATION.md #417)
///
/// Routes through [`count_supermasks_dispatch`]: batches below
/// [`PARALLEL_WORK_THRESHOLD`] `mask·bar` ops run serially (small-N O(1)
/// canary protection); larger batches fan out across rayon's default global
/// thread pool via an order-preserving `par_iter().map().collect()` — so the
/// output is bit-identical to the serial scan (Golden Rule #4). rayon spawns
/// its own worker threads regardless of the GIL, so the multi-core speedup
/// does NOT depend on releasing it.
///
/// NOTE: this function deliberately does NOT wrap the scan in `Python::detach`
/// (`allow_threads`). `detach` would be a pure host-cooperation nicety (let
/// other Python threads run during the GIL-free integer scan), but it pulls
/// `PyEval_SaveThread`/`PyEval_RestoreThread` + the panic-unwind print path
/// into the crate, which do NOT link under the `extension-module` `cargo test`
/// build (CI's Rust job links no libpython) — the spec made `detach` "ONLY IF
/// it compiles cleanly", and it does not. Parallelism + bit-exactness are
/// unchanged; only the GIL-release nicety is forgone, matching
/// `ResidentBars::count_supermasks`.
#[pyfunction]
pub fn count_supermasks_u64s(bar_bits: Vec<u64>, masks: Vec<u64>) -> Vec<u64> {
    // bars inner — `bar_bits` streams linearly, `mask` is register-resident.
    // Delegates to the shared `count_one` kernel (single source of truth) via
    // the serial/parallel dispatch.
    count_supermasks_dispatch(&bar_bits, &masks)
}

/// BENCH-ONLY: batched SUPERSET count on a SCOPED rayon pool of `n_threads`.
///
/// DEC-RUST-RAYON-SUPERMASK-001 (MIGRATION.md #417). Production uses rayon's
/// DEFAULT global pool (this crate never calls `build_global` — a double-call
/// would panic → SIGABRT under `panic = "abort"` — and never sets
/// `RAYON_NUM_THREADS`). This shim exists ONLY so the scaling bench +
/// thread-count-independence parity tests can pin a specific worker count
/// without touching the global pool: it builds a SCOPED
/// `rayon::ThreadPoolBuilder` and runs the same order-preserving
/// `par_iter().map().collect()` inside `pool.install`.
///
/// Determinism: the result is bit-identical to [`count_supermasks_u64s`] for
/// any `n_threads >= 1` (order-preserving collect + pure integer counts), which
/// the parity tests assert across `n in {1, 2, 4}`.
///
/// `n_threads == 0` lets rayon choose its default worker count (documented
/// rayon behaviour for a 0 in `num_threads`); any value `>= 1` pins exactly
/// that many. Pool-build failure is surfaced as a `ValueError` via `map_err`
/// (never `unwrap`/`expect` — clippy denies them and `panic = "abort"` would
/// SIGABRT the host).
#[pyfunction]
pub fn count_supermasks_u64s_nthreads(
    bar_bits: Vec<u64>,
    masks: Vec<u64>,
    n_threads: usize,
) -> PyResult<Vec<u64>> {
    // The pure-Rust core lives in `count_supermasks_scoped` so the
    // `#[cfg(test)]` suite can exercise it WITHOUT referencing any Python
    // C-API symbol — under the `extension-module` `cargo test` build (CI's
    // Rust job links no libpython) a test that pulled `PyValueError`/PyErr
    // machinery would fail to link (the prior detach/PyErr link breakage).
    count_supermasks_scoped(&bar_bits, &masks, n_threads).map_err(PyValueError::new_err)
}

/// Pure-Rust core of [`count_supermasks_u64s_nthreads`] — a SCOPED-pool scan
/// returning a plain `Result<_, String>` (NO `PyErr`), so Rust unit tests can
/// call it without dragging Python C-API symbols into the no-libpython
/// `cargo test` link. The `#[pyfunction]` wrapper maps the `String` error to a
/// `ValueError`.
fn count_supermasks_scoped(
    bars: &[u64],
    masks: &[u64],
    n_threads: usize,
) -> Result<Vec<u64>, String> {
    let pool = rayon::ThreadPoolBuilder::new()
        .num_threads(n_threads)
        .build()
        .map_err(|e| {
            format!(
                "count_supermasks_scoped: failed to build rayon pool (n_threads={n_threads}): {e}"
            )
        })?;
    Ok(pool.install(|| masks.par_iter().map(|&m| count_one(bars, m)).collect()))
}

/// Resident bar buffer — holds the per-bar condition bitset `Vec<u64>`
/// Rust-side so the Stage-2 batch path pays the `O(N_bars)` Python→Rust
/// copy ONCE (at construction) instead of on every batch call.
///
/// DEC-RUST-RESIDENT-BARS-001 (#413; builds on DEC-RUST-SUPERMASK-KERNEL-001
/// #411 / ADR-0004 §5.2). The Stage-2 sweep evaluates the SAME `bar_bits`
/// against many successive mask batches. The free
/// [`count_supermasks_u64s`] re-marshals the whole `bar_bits` (~94 K u64)
/// across the FFI boundary on every call; this class moves it across once
/// and keeps it resident, so only `masks` crosses per call.
///
/// ## Complexity (honest envelope — NOT O(1))
///
/// * Construction: `O(N_bars)` — the one-time PyO3 extract copy, paid here.
/// * [`Self::count_supermasks`]: `O(N_masks · N_bars)` compute +
///   `O(N_masks)` marshalling. The `N_bars` marshalling is paid once at
///   construction, NOT per call — that is the entire win, which grows with
///   the number of batches and `N_bars`. The inner kernel ([`count_one`])
///   is byte-identical to the shipped free functions.
///
/// ## Frozen + Sync (P3-1 scope lock)
///
/// `#[pyclass(frozen)]` — no interior mutability, no append / `__getitem__`
/// / `bars` getter (a `bars` getter would copy `O(N)` per access, defeating
/// the purpose). Only [`Self::n_bars`] is exposed. The buffer is immutable
/// once built; this is a pre-sliced Stage-2 AGGREGATE-COUNT window, never a
/// per-bar feed (no look-ahead semantics).
///
/// ## P0-1 panic safety
///
/// `panic = "abort"` is set in the release profile, so a panic inside a
/// `#[pymethod]` would SIGABRT the host pytest process. NO `unwrap` /
/// `expect` / indexing / `as`-cast appears in any method body; the fallible
/// Python→Rust conversion is done entirely by the `Vec<u64>` signature
/// extract (exactly as [`count_supermasks_u64s`] does), which raises a
/// clean `TypeError` / `OverflowError` on the Python side instead.
#[pyclass(frozen, module = "fno_math")]
pub struct ResidentBars {
    bars: Vec<u64>,
}

#[pymethods]
impl ResidentBars {
    /// Build a resident buffer from `bar_bits`. The ONE-TIME `O(N_bars)`
    /// Python→Rust copy is paid here via the `Vec<u64>` extract.
    #[new]
    fn new(bar_bits: Vec<u64>) -> Self {
        Self { bars: bar_bits }
    }

    /// Number of resident bars (the only exposed view of the buffer).
    #[getter]
    fn n_bars(&self) -> usize {
        self.bars.len()
    }

    /// Batched per-mask SUPERSET count over the resident bars.
    ///
    /// For each `mask` in `masks`, return how many resident bars satisfy
    /// `(bar & mask) == mask`. `out[j]` aligns with `masks[j]`. Empty
    /// `masks` → empty `Vec`; empty resident bars → `0` per mask;
    /// `mask == 0` → all bars. Bars come from `&self.bars` — NOT
    /// re-marshalled — so only `masks` crosses the FFI boundary.
    ///
    /// Multi-core via [`count_supermasks_dispatch`]
    /// (DEC-RUST-RAYON-SUPERMASK-001, MIGRATION.md #417): below
    /// [`PARALLEL_WORK_THRESHOLD`] runs serially; larger batches fan out across
    /// rayon's default global pool with an order-preserving collect, so output
    /// is bit-identical to the serial scan.
    ///
    /// Like the free [`count_supermasks_u64s`], this does NOT wrap the scan in
    /// `Python::detach`/`allow_threads`: that would only be a host-cooperation
    /// nicety, and it drags `PyEval_SaveThread`/`RestoreThread` + the panic
    /// print path into the crate, which do not link under the `extension-module`
    /// `cargo test` build (CI's Rust job links no libpython). rayon spawns its
    /// own worker threads regardless of the GIL, so parallelism + bit-exactness
    /// are identical on the direct path; only the GIL-release nicety is forgone.
    fn count_supermasks(&self, masks: Vec<u64>) -> Vec<u64> {
        count_supermasks_dispatch(&self.bars, &masks)
    }

    /// Single-mask SUPERSET count over the resident bars.
    fn count_supermask(&self, mask: u64) -> u64 {
        count_one(&self.bars, mask)
    }
}

// ─────────────────────────────────────────────────────────────────────────
// DEC-ENUM-KERNEL-U128-001 — the u128 enum/count kernel twins (fenced,
// self-contained additive block; the merge-union seam with sibling lanes).
// ─────────────────────────────────────────────────────────────────────────

/// Batched per-mask SUPERSET count over a **u128** domain — the wide twin of
/// [`count_supermasks_u64s`] (DEC-ENUM-KERNEL-U128-001).
///
/// Identical contract with `bar_bits` / `masks` widened to native
/// `Vec<u128>` (the DEC-TRADE-SIM-U128-KERNEL-001 FFI crossing, probe-verified
/// under pyo3 0.29 `abi3-py312`: round-trip exact; a value `>= 2**128` or a
/// negative int raises `OverflowError` at the extraction edge — no new probe
/// needed, the recorded transcript covers `Vec<u128>`). Routes through the
/// SAME width-generic [`count_supermasks_dispatch`] (one dispatch, one
/// serial/parallel boundary, one order-preserving collect), so the u128 count
/// is the compiler's two-limb lowering of the exact u64 loop — bit-identical
/// semantics, Golden Rule #4 determinism unchanged (pure integer counts).
///
/// Unlike the trade-sim u128 twin (future headroom), THIS kernel is the
/// ACTIVE production path for the Stage-2 enum/count the moment the
/// vocabulary crosses 64 bits — `brutex.engine.combo_mask` routes every
/// >64-bit batch here and REFUSES to degrade silently when it is missing
/// (the LOUD-FAIL inversion recorded in DEC-ENUM-KERNEL-U128-001).
#[pyfunction]
pub fn count_supermasks_u128s(bar_bits: Vec<u128>, masks: Vec<u128>) -> Vec<u64> {
    count_supermasks_dispatch_core(&bar_bits, &masks)
}

/// Single-mask SUPERSET count over a u128 domain — the wide twin of
/// [`count_supermask_u64s`]. Counts bars where `(bar & mask) == mask`;
/// `mask == 0` matches every bar.
#[pyfunction]
pub fn count_supermask_u128s(bar_bits: Vec<u128>, mask: u128) -> u64 {
    count_one_core(&bar_bits, mask)
}

/// Resident bar buffer over a **u128** domain — the wide twin of
/// [`ResidentBars`] (DEC-ENUM-KERNEL-U128-001).
///
/// Same frozen/Sync/no-panic contract and the same honest complexity
/// envelope: construction is the one-time `O(N_bars)` `Vec<u128>` extract;
/// each [`Self::count_supermasks`] is `O(N_masks · N_bars)` compute +
/// `O(N_masks)` marshalling, delegating to the SAME width-generic
/// [`count_supermasks_dispatch`] / [`count_one`] the u64 class uses, so the
/// two widths can never drift. A `bar_bits` / mask value outside
/// `[0, 2**128)` raises `OverflowError` at the PyO3 extraction edge
/// (probe-pinned; never a panic under `panic = "abort"`).
#[pyclass(frozen, module = "fno_math")]
pub struct ResidentBarsU128 {
    bars: Vec<u128>,
}

#[pymethods]
impl ResidentBarsU128 {
    /// Build a resident buffer from u128 `bar_bits`. The ONE-TIME `O(N_bars)`
    /// Python→Rust copy is paid here via the `Vec<u128>` extract.
    #[new]
    fn new(bar_bits: Vec<u128>) -> Self {
        Self { bars: bar_bits }
    }

    /// Number of resident bars (the only exposed view of the buffer).
    #[getter]
    fn n_bars(&self) -> usize {
        self.bars.len()
    }

    /// Batched per-mask SUPERSET count over the resident u128 bars.
    ///
    /// Same contract as [`ResidentBars::count_supermasks`] with masks widened
    /// to `Vec<u128>`: `out[j]` aligns with `masks[j]`; empty `masks` →
    /// empty `Vec`; empty resident bars → `0` per mask; `mask == 0` → all
    /// bars. Only `masks` crosses the FFI boundary per call.
    fn count_supermasks(&self, masks: Vec<u128>) -> Vec<u64> {
        count_supermasks_dispatch_core(&self.bars, &masks)
    }

    /// Single-mask SUPERSET count over the resident u128 bars.
    fn count_supermask(&self, mask: u128) -> u64 {
        count_one_core(&self.bars, mask)
    }
}

// ── end DEC-ENUM-KERNEL-U128-001 block ───────────────────────────────────

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
    use super::{
        PARALLEL_WORK_THRESHOLD, ResidentBars, add_u64_checked, bitand_count_checked,
        bitcount_u64s, count_one, count_supermask_u64s, count_supermasks_dispatch,
        count_supermasks_scoped, count_supermasks_u64s, popcount_bytes,
    };

    // ── add_u64_checked (R2-RC-F1 overflow guard at the FFI edge) ──

    #[test]
    fn add_u64_overflowing_operands_return_err_not_abort() {
        // The EXACT old-abort reproduction: two ~2^63 operands. Before the
        // guard, `a + b` under `overflow-checks = true` + `panic = "abort"`
        // SIGABRT'd the host process (exit 134); now it is a clean Err the
        // `#[pyfunction]` wrapper maps to ValueError.
        let err = add_u64_checked(1u64 << 63, 1u64 << 63).unwrap_err();
        assert!(
            err.contains("overflows u64"),
            "message names the overflow: {err}"
        );
        // Minimal off-by-one overflow.
        assert!(add_u64_checked(u64::MAX, 1).is_err());
        assert!(add_u64_checked(1, u64::MAX).is_err());
        assert!(add_u64_checked(u64::MAX, u64::MAX).is_err());
    }

    #[test]
    fn add_u64_valid_inputs_compute_identically() {
        // Golden/regression: the values the Python bridge test pins, plus the
        // exact-fit boundary — all byte-identical to the pre-guard `a + b`.
        assert_eq!(add_u64_checked(2, 3), Ok(5));
        assert_eq!(add_u64_checked(0, 42), Ok(42));
        assert_eq!(add_u64_checked(42, 0), Ok(42));
        assert_eq!(add_u64_checked(1u64 << 32, 1u64 << 32), Ok(1u64 << 33));
        // Largest representable sum — fits exactly, must NOT be rejected.
        assert_eq!(add_u64_checked(u64::MAX, 0), Ok(u64::MAX));
        assert_eq!(add_u64_checked(u64::MAX - 1, 1), Ok(u64::MAX));
    }

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

    // ── count_supermasks_dispatch (batched; serial/parallel arms) ──
    //
    // The free `count_supermasks_u64s` pyfunction and `ResidentBars::
    // count_supermasks` both delegate directly to `count_supermasks_dispatch`
    // (no `py`/`allow_threads` wrapper — see their docs), so the pure-Rust unit
    // tests exercise `count_supermasks_dispatch` directly (byte-identical work,
    // no GIL needed). The serial/parallel arm selection is covered explicitly
    // by the threshold tests below.

    #[test]
    fn batch_empty_masks_is_empty() {
        assert_eq!(
            count_supermasks_dispatch(&[1, 2, 3], &[]),
            Vec::<u64>::new()
        );
    }

    #[test]
    fn batch_empty_bars_is_zero_per_mask() {
        assert_eq!(
            count_supermasks_dispatch(&[], &[0b1, 0b11, 0]),
            vec![0, 0, 0]
        );
    }

    #[test]
    fn batch_mask_zero_matches_all() {
        let bars = vec![0b0001, 0b1010, 0b1111];
        assert_eq!(count_supermasks_dispatch(&bars, &[0]), vec![3]);
    }

    #[test]
    fn batch_multi_mask_mixed() {
        let bars = vec![0b0011, 0b0111, 0b1000, 0b1011, 0b0000];
        // mask 0b0011 → bars {0b0011,0b0111,0b1011} = 3
        // mask 0b1000 → bars {0b1000,0b1011}        = 2
        // mask 0b1111 → none                        = 0
        // mask 0      → all                         = 5
        let out = count_supermasks_dispatch(&bars, &[0b0011, 0b1000, 0b1111, 0]);
        assert_eq!(out, vec![3, 2, 0, 5]);
    }

    #[test]
    fn batch_positional_alignment() {
        // out[j] must align with masks[j]; reordering masks reorders output.
        let bars = vec![0b0001, 0b0010, 0b0011];
        assert_eq!(
            count_supermasks_dispatch(&bars, &[0b0001, 0b0010]),
            vec![2, 2]
        );
        assert_eq!(
            count_supermasks_dispatch(&bars, &[0b0010, 0b0001]),
            vec![2, 2]
        );
    }

    #[test]
    fn batch_matches_single_kernel() {
        // The batched kernel must agree with the single-mask kernel element-wise.
        let bars = vec![0u64, 1, 0b1011, 0b1111, 1u64 << 40, 0xDEAD_BEEF];
        let masks = vec![0u64, 1, 0b1010, 1u64 << 40, 0xFFFF_FFFF_FFFF_FFFF];
        let batched = count_supermasks_dispatch(&bars, &masks);
        let one_by_one: Vec<u64> = masks
            .iter()
            .map(|&m| count_supermask_u64s(bars.clone(), m))
            .collect();
        assert_eq!(batched, one_by_one);
    }

    // ── dispatch serial vs parallel arm + threshold boundary ──

    /// Serial reference: the exact pre-rayon masks-outer loop.
    fn serial_reference(bars: &[u64], masks: &[u64]) -> Vec<u64> {
        masks.iter().map(|&m| count_one(bars, m)).collect()
    }

    #[test]
    fn dispatch_serial_arm_below_threshold() {
        // Small work (< THRESHOLD) → serial arm; correct vs reference.
        let bars = vec![0b0011, 0b0111, 0b1000, 0b1011, 0b0000];
        let masks = vec![0b0011u64, 0b1000, 0b1111, 0];
        assert!((masks.len() as u64) * (bars.len() as u64) < PARALLEL_WORK_THRESHOLD);
        assert_eq!(
            count_supermasks_dispatch(&bars, &masks),
            serial_reference(&bars, &masks)
        );
    }

    #[test]
    fn dispatch_parallel_arm_above_threshold() {
        // bars.len()=1024 * masks.len()=1100 = 1_126_400 > 1<<20 (=1_048_576)
        // → parallel arm; bit-exact vs serial reference. Kept cheap.
        let bars: Vec<u64> = (0..1024u64).map(|i| i | 0b11).collect();
        let masks: Vec<u64> = (0..1100u64).map(|i| (i & 0b1) | (1 << (i % 50))).collect();
        let work = (masks.len() as u64) * (bars.len() as u64);
        assert!(
            work >= PARALLEL_WORK_THRESHOLD,
            "work={work} must cross threshold"
        );
        assert_eq!(
            count_supermasks_dispatch(&bars, &masks),
            serial_reference(&bars, &masks)
        );
    }

    #[test]
    fn dispatch_threshold_boundary_identical_to_serial() {
        // At work == THRESHOLD-1 (serial arm) and work == THRESHOLD (parallel
        // arm) the dispatch output must equal the plain serial count_one loop.
        // THRESHOLD = 1<<20 = 1_048_576 = 1024 * 1024.
        let bars: Vec<u64> = (0..1024u64).map(|i| i | 0b101).collect();

        // work == THRESHOLD (1024 masks) → parallel arm.
        let masks_at: Vec<u64> = (0..1024u64).map(|i| (1 << (i % 40)) | (i & 0b11)).collect();
        let work_at = (masks_at.len() as u64) * (bars.len() as u64);
        assert_eq!(work_at, PARALLEL_WORK_THRESHOLD);
        assert_eq!(
            count_supermasks_dispatch(&bars, &masks_at),
            serial_reference(&bars, &masks_at)
        );

        // work == THRESHOLD - 1024 (1023 masks) → serial arm, just below.
        let masks_below = &masks_at[..1023];
        let work_below = (masks_below.len() as u64) * (bars.len() as u64);
        assert!(work_below < PARALLEL_WORK_THRESHOLD);
        assert_eq!(
            count_supermasks_dispatch(&bars, masks_below),
            serial_reference(&bars, masks_below)
        );
    }

    #[test]
    fn dispatch_parallel_equals_serial_parity() {
        // Direct parallel==serial parity on a parallel-arm input with varied bits.
        let bars: Vec<u64> = (0..2048u64)
            .map(|i| (i.wrapping_mul(0x9E37_79B9)) & 0xFFFF_FFFF_FFFF)
            .collect();
        let masks: Vec<u64> = (0..600u64)
            .map(|i| (1u64 << (i % 48)) | (i & 0b111))
            .collect();
        assert!((masks.len() as u64) * (bars.len() as u64) >= PARALLEL_WORK_THRESHOLD);
        assert_eq!(
            count_supermasks_dispatch(&bars, &masks),
            serial_reference(&bars, &masks)
        );
    }

    #[test]
    fn dispatch_empty_masks() {
        assert_eq!(
            count_supermasks_dispatch(&[1, 2, 3], &[]),
            Vec::<u64>::new()
        );
    }

    #[test]
    fn dispatch_empty_bars() {
        assert_eq!(
            count_supermasks_dispatch(&[], &[0b1, 0b11, 0]),
            vec![0, 0, 0]
        );
    }

    #[test]
    fn dispatch_single_mask() {
        let bars = vec![0b0011, 0b0001, 0b1011];
        assert_eq!(count_supermasks_dispatch(&bars, &[0b0011]), vec![2]);
    }

    #[test]
    fn dispatch_mask_zero_matches_all() {
        let bars = vec![0u64, 1, 0xDEAD_BEEF, 0xFFFF_FFFF_FFFF_FFFF];
        assert_eq!(
            count_supermasks_dispatch(&bars, &[0]),
            vec![bars.len() as u64]
        );
    }

    // ── count_supermasks_scoped (pure core of the bench-only nthreads shim) ──
    // Tests call the PURE helper (Result<_, String>, no PyErr) so the test
    // binary references zero Python C-API symbols under the no-libpython
    // `cargo test` link; the `#[pyfunction]` wrapper only adds `map_err`.

    #[test]
    fn nthreads_returns_ok_and_matches_reference() {
        let bars = vec![0b0011, 0b0111, 0b1000, 0b1011, 0b0000];
        let masks = vec![0b0011u64, 0b1000, 0b1111, 0];
        let out = count_supermasks_scoped(&bars, &masks, 2)
            .expect("pool build with n_threads=2 must succeed");
        assert_eq!(out, serial_reference(&bars, &masks));
    }

    #[test]
    fn nthreads_independence_across_thread_counts() {
        // Same input, n in {1,2,4} → all equal to each other AND to the serial
        // reference (which is what count_supermasks_dispatch / the free
        // count_supermasks_u64s produce on the serial arm).
        let bars: Vec<u64> = (0..300u64).map(|i| (i | 0b101) & 0xFFFF).collect();
        let masks: Vec<u64> = (0..200u64)
            .map(|i| (1u64 << (i % 16)) | (i & 0b11))
            .collect();
        let reference = serial_reference(&bars, &masks);
        let dispatch_eq = count_supermasks_dispatch(&bars, &masks);
        assert_eq!(dispatch_eq, reference);
        for n in [1usize, 2, 4] {
            let out = count_supermasks_scoped(&bars, &masks, n).expect("pool build must succeed");
            assert_eq!(
                out, reference,
                "n_threads={n} diverged from serial reference"
            );
        }
    }

    #[test]
    fn nthreads_empty_and_edge_cases() {
        // empty masks → []; empty bars → 0 per mask; mask=0 → all.
        assert_eq!(
            count_supermasks_scoped(&[1, 2, 3], &[], 4).expect("ok"),
            Vec::<u64>::new()
        );
        assert_eq!(
            count_supermasks_scoped(&[], &[0b1, 0b11, 0], 4).expect("ok"),
            vec![0, 0, 0]
        );
        assert_eq!(
            count_supermasks_scoped(&[0b1, 0b1010, 0b1111], &[0], 2).expect("ok"),
            vec![3]
        );
    }

    // ── count_one (shared kernel — single source of truth) ──

    #[test]
    fn count_one_empty_bars_is_zero() {
        assert_eq!(count_one(&[], 0b1011), 0);
    }

    #[test]
    fn count_one_mask_zero_matches_all() {
        let bars = [0u64, 1, 0xFFFF_FFFF_FFFF_FFFF, 0xDEAD_BEEF];
        assert_eq!(count_one(&bars, 0), bars.len() as u64);
    }

    #[test]
    fn count_one_partial_match() {
        let bars = [0b0011, 0b0001, 0b0010, 0b1011, 0b0100];
        assert_eq!(count_one(&bars, 0b0011), 2);
    }

    #[test]
    fn count_one_used_by_free_functions() {
        // The free functions and the shared kernel must agree by construction.
        let bars = vec![0u64, 1, 0b1011, 0b1111, 1u64 << 63, 0xDEAD_BEEF];
        for &m in &[0u64, 1, 0b1010, 1u64 << 63, 0xFFFF_FFFF_FFFF_FFFF] {
            assert_eq!(count_supermask_u64s(bars.clone(), m), count_one(&bars, m));
        }
    }

    // ── ResidentBars ──

    #[test]
    fn resident_empty_bars_is_zero_per_mask() {
        let rb = ResidentBars::new(vec![]);
        assert_eq!(rb.count_supermasks(vec![0b1, 0b11, 0]), vec![0, 0, 0]);
        assert_eq!(rb.count_supermask(0b11), 0);
    }

    #[test]
    fn resident_empty_masks_is_empty() {
        let rb = ResidentBars::new(vec![1, 2, 3]);
        assert_eq!(rb.count_supermasks(vec![]), Vec::<u64>::new());
    }

    #[test]
    fn resident_mask_zero_matches_all() {
        let rb = ResidentBars::new(vec![0b0001, 0b1010, 0b1111]);
        assert_eq!(rb.count_supermasks(vec![0]), vec![3]);
        assert_eq!(rb.count_supermask(0), 3);
    }

    #[test]
    fn resident_multi_mask_mixed() {
        let rb = ResidentBars::new(vec![0b0011, 0b0111, 0b1000, 0b1011, 0b0000]);
        let out = rb.count_supermasks(vec![0b0011, 0b1000, 0b1111, 0]);
        assert_eq!(out, vec![3, 2, 0, 5]);
    }

    #[test]
    fn resident_positional_alignment() {
        let rb = ResidentBars::new(vec![0b0001, 0b0010, 0b0011]);
        assert_eq!(rb.count_supermasks(vec![0b0001, 0b0010]), vec![2, 2]);
        assert_eq!(rb.count_supermasks(vec![0b0010, 0b0001]), vec![2, 2]);
    }

    #[test]
    fn resident_high_bit_mask() {
        let top = 1u64 << 63;
        let rb = ResidentBars::new(vec![top, top | 1, 0, 1]);
        assert_eq!(rb.count_supermask(top), 2);
        assert_eq!(rb.count_supermasks(vec![top]), vec![2]);
    }

    #[test]
    fn resident_full_width_mask() {
        let full = 0xFFFF_FFFF_FFFF_FFFFu64;
        let rb = ResidentBars::new(vec![full, full, 0, 1]);
        assert_eq!(rb.count_supermask(full), 2);
    }

    #[test]
    fn resident_n_bars_getter() {
        assert_eq!(ResidentBars::new(vec![]).n_bars(), 0);
        assert_eq!(ResidentBars::new(vec![1, 2, 3, 4]).n_bars(), 4);
    }

    #[test]
    fn resident_reuse_is_stable() {
        // Calling count_supermasks twice on ONE instance yields identical
        // results — the resident buffer is immutable + reusable.
        let rb = ResidentBars::new(vec![0b0011, 0b0111, 0b1111, 0b1011]);
        let masks = vec![0b0011, 0b0100, 0u64];
        let first = rb.count_supermasks(masks.clone());
        let second = rb.count_supermasks(masks);
        assert_eq!(first, second);
        assert_eq!(first, vec![4, 2, 4]);
    }

    #[test]
    fn resident_parity_with_free_function() {
        // resident.count_supermasks(m) elementwise == the shared dispatch (the
        // exact work the free `count_supermasks_u64s` pyfunction performs — both
        // delegate directly to `count_supermasks_dispatch`).
        let bars = vec![0u64, 1, 0b1011, 0b1111, 1u64 << 40, 1u64 << 63, 0xDEAD_BEEF];
        let masks = vec![0u64, 1, 0b1010, 1u64 << 40, 0xFFFF_FFFF_FFFF_FFFF];
        let rb = ResidentBars::new(bars.clone());
        assert_eq!(
            rb.count_supermasks(masks.clone()),
            count_supermasks_dispatch(&bars, &masks)
        );
    }

    // ── DEC-MUTATION-RUNTIME-003 cargo-mutants survivor kills ──
    // Exact-value pins on the GIL-free `#[pyfunction]` bodies (plain
    // Vec<u64>/&[u8] args — callable without an interpreter). Each test names
    // the mutant class it kills.

    #[test]
    fn bitcount_u64s_counts_across_words() {
        // ≥2 result distinguishes the 0|1 fn-replacement mutants (3 + 1 = 4).
        assert_eq!(bitcount_u64s(vec![0b1011, 0b1]), 4);
    }

    #[test]
    fn bitand_count_matches_hand_popcount_and_rejects_mismatch() {
        // & vs | vs ^ separate on (0b1100, 0b1010): 1 vs 3 vs 2;
        // equal-length must SUCCEED and mismatch must ERR (kills != -> ==).
        // Calls the pure `bitand_count_checked` core (the `#[pyfunction]`
        // wrapper only adds `map_err` — no-libpython link constraint).
        assert_eq!(
            bitand_count_checked(&[0b1100], &[0b1010]).expect("equal lengths must pass"),
            1
        );
        let err = bitand_count_checked(&[1], &[1, 2]).unwrap_err();
        assert!(err.contains("length mismatch"), "{err}");
    }

    #[test]
    fn count_supermasks_u64s_positional_counts() {
        // len-2 output with values {2, 1} kills the vec![]/vec![0]/vec![1]
        // whole-fn replacement mutants on the pyfunction wrapper.
        assert_eq!(
            count_supermasks_u64s(vec![0b11, 0b01, 0b10], vec![0b01, 0b11]),
            vec![2, 1]
        );
    }

    #[test]
    fn popcount_bytes_sums_body_and_tail() {
        // ≥2 result kills the 0|1 replacements.
        assert_eq!(popcount_bytes(&[0xFF, 0x01, 0x00]), 9);
        // body=8 (one exact chunk of 0x01×8), tail=2 (0x03):
        // sum 10 vs `-`→6 vs `*`→16 — kills the body/tail `+` flips.
        assert_eq!(popcount_bytes(&[1, 1, 1, 1, 1, 1, 1, 1, 3]), 10);
    }
}

// ─────────────────────────────────────────────────────────────────────────
// DEC-ENUM-KERNEL-U128-001 — u128 enum/count kernel tests (fenced additive
// test module; the pre-existing `mod tests` above is deliberately untouched).
// ─────────────────────────────────────────────────────────────────────────
#[cfg(test)]
mod u128_kernel_tests {
    use super::{
        ResidentBarsU128, count_one_core, count_supermask_u128s, count_supermasks_dispatch_core,
        count_supermasks_u128s,
    };

    const U64_MAX: u128 = u64::MAX as u128;

    /// Naive reference: per-mask filter+count with the raw scalar superset
    /// expression — deliberately a DIFFERENT loop shape from the branchless
    /// `count_one` sum, so the differential has independent teeth.
    fn naive_reference(bars: &[u128], masks: &[u128]) -> Vec<u64> {
        masks
            .iter()
            .map(|&m| bars.iter().filter(|&&b| (b & m) == m).count() as u64)
            .collect()
    }

    #[test]
    fn u128_empty_masks_is_empty() {
        assert_eq!(
            count_supermasks_u128s(vec![1, 2, 3], vec![]),
            Vec::<u64>::new()
        );
    }

    #[test]
    fn u128_empty_bars_is_zero_per_mask() {
        assert_eq!(
            count_supermasks_u128s(vec![], vec![1, 1 << 127, 0]),
            vec![0, 0, 0]
        );
    }

    #[test]
    fn u128_single_bar_window() {
        let bar = (1u128 << 100) | 0b11;
        assert_eq!(
            count_supermasks_u128s(vec![bar], vec![bar, 1 << 100, 0b11, 1 << 99, 0]),
            vec![1, 1, 1, 0, 1]
        );
    }

    #[test]
    fn u128_boundary_bits_63_and_64() {
        // The limb boundary: bit 63 is the low limb's top bit, bit 64 the
        // high limb's bottom bit — a truncating kernel (or a limb swap)
        // conflates them.
        let b63 = 1u128 << 63;
        let b64 = 1u128 << 64;
        let bars = vec![b63, b64, b63 | b64, 0];
        assert_eq!(
            count_supermasks_u128s(bars.clone(), vec![b63, b64, b63 | b64]),
            vec![2, 2, 1]
        );
        assert_eq!(count_supermask_u128s(bars.clone(), b63), 2);
        assert_eq!(count_supermask_u128s(bars, b64), 2);
    }

    #[test]
    fn u128_bit_127_sign_bit_patterns() {
        // Bit 127 (the u128 "sign bit" were it signed) + bit 63 together —
        // a signed-lowering or arithmetic-shift defect shows here.
        let top = 1u128 << 127;
        let sign_pair = top | (1 << 63);
        let bars = vec![top, sign_pair, 1 << 63, u128::MAX];
        assert_eq!(
            count_supermasks_u128s(bars, vec![top, sign_pair, 1 << 63]),
            vec![3, 2, 3]
        );
    }

    #[test]
    fn u128_hi_limb_only_masks() {
        // Masks with ZERO low-limb bits: a kernel that truncated to u64 would
        // see mask 0 and count EVERY bar.
        let bars = vec![(1u128 << 70) | 1, 1 << 80, 0b111, 0];
        assert_eq!(
            count_supermasks_u128s(bars, vec![1 << 70, 1 << 80, (1 << 70) | (1 << 80)]),
            vec![1, 1, 0]
        );
    }

    #[test]
    fn u128_all_ones() {
        let bars = vec![u128::MAX, u128::MAX - 1, 0];
        assert_eq!(
            count_supermasks_u128s(bars.clone(), vec![u128::MAX, 0]),
            vec![1, 3]
        );
        assert_eq!(count_supermask_u128s(bars, u128::MAX), 1);
    }

    #[test]
    fn u128_mask_zero_matches_all_bars() {
        let bars = vec![0u128, 1 << 127, 42];
        assert_eq!(count_supermasks_u128s(bars.clone(), vec![0]), vec![3]);
        assert_eq!(count_supermask_u128s(bars, 0), 3);
    }

    #[test]
    fn u128_resident_class_matches_free_kernels() {
        let bars = vec![(1u128 << 90) | 0b101, 1 << 90, 0b101, u128::MAX];
        let masks = vec![(1u128 << 90) | 0b101, 1 << 90, 0b100, 0];
        let resident = ResidentBarsU128::new(bars.clone());
        assert_eq!(resident.n_bars(), 4);
        assert_eq!(
            resident.count_supermasks(masks.clone()),
            count_supermasks_u128s(bars.clone(), masks.clone())
        );
        for &m in &masks {
            assert_eq!(
                resident.count_supermask(m),
                count_supermask_u128s(bars.clone(), m)
            );
        }
        // Empty-mask + repeat-call determinism on the resident buffer.
        assert_eq!(resident.count_supermasks(vec![]), Vec::<u64>::new());
        assert_eq!(
            resident.count_supermasks(masks.clone()),
            resident.count_supermasks(masks)
        );
    }

    #[test]
    fn u128_kernel_equals_u64_kernel_on_u64_fit_inputs() {
        // Anti-drift pin (the #1549 idiom): on u64-fitting inputs the u128
        // monomorphization must agree with the u64 monomorphization exactly —
        // the shared width-generic core makes any future de-sharing loud.
        let bars64: Vec<u64> = vec![0b0011, 0b0111, 0b1000, 0b1011, 0, u64::MAX, 1 << 63];
        let masks64: Vec<u64> = vec![0b0011, 0b1000, 1 << 63, 0, u64::MAX];
        let bars128: Vec<u128> = bars64.iter().map(|&b| u128::from(b)).collect();
        let masks128: Vec<u128> = masks64.iter().map(|&m| u128::from(m)).collect();
        assert_eq!(
            count_supermasks_dispatch_core(&bars128, &masks128),
            count_supermasks_dispatch_core(&bars64, &masks64)
        );
    }

    #[test]
    fn u128_parallel_arm_bit_exact_vs_naive() {
        // Cross the 1<<20 work threshold with wide operands so the rayon arm
        // runs on genuinely two-limb values; must equal the naive reference.
        let bars: Vec<u128> = (0..1024u128).map(|i| (i << 64) | i | 0b11).collect();
        let masks: Vec<u128> = (0..1100u128)
            .map(|i| (1 << (64 + (i % 60))) | (i & 0b1))
            .collect();
        let work = (masks.len() as u64) * (bars.len() as u64);
        assert!(work >= super::PARALLEL_WORK_THRESHOLD);
        assert_eq!(
            count_supermasks_dispatch_core(&bars, &masks),
            naive_reference(&bars, &masks)
        );
    }

    #[test]
    fn u128_count_one_matches_naive_single() {
        let bars = vec![(1u128 << 127) | 1, 1 << 64, (1 << 64) | (1 << 63)];
        for &m in &[0u128, 1, 1 << 63, 1 << 64, (1 << 127) | 1, u128::MAX] {
            assert_eq!(count_one_core(&bars, m), naive_reference(&bars, &[m])[0]);
        }
    }

    // ── proptest differential: generic kernel vs naive reference ─────────

    use proptest::prelude::*;

    /// Hostile u128 strategy: mixes pure-low, pure-high, boundary-straddling
    /// (bits 63+64), sign-bit (bits 63/127) and fully random words.
    fn u128_word() -> impl Strategy<Value = u128> {
        prop_oneof![
            (0u64..=u64::MAX).prop_map(u128::from),
            (0u64..=u64::MAX).prop_map(|hi| u128::from(hi) << 64),
            Just((1u128 << 63) | (1u128 << 64)),
            Just((1u128 << 127) | (1u128 << 63)),
            Just(u128::MAX),
            proptest::num::u128::ANY,
        ]
    }

    proptest! {
        #[test]
        fn u128_dispatch_matches_naive_reference(
            bars in proptest::collection::vec(u128_word(), 0..64),
            masks in proptest::collection::vec(u128_word(), 0..16),
        ) {
            prop_assert_eq!(
                count_supermasks_dispatch_core(&bars, &masks),
                naive_reference(&bars, &masks)
            );
        }

        #[test]
        fn u128_kernel_agrees_with_u64_kernel_on_u64_domain(
            bars in proptest::collection::vec(0u64..=u64::MAX, 0..64),
            masks in proptest::collection::vec(0u64..=u64::MAX, 0..16),
        ) {
            let bars128: Vec<u128> = bars.iter().map(|&b| u128::from(b)).collect();
            let masks128: Vec<u128> = masks.iter().map(|&m| u128::from(m)).collect();
            prop_assert_eq!(
                count_supermasks_dispatch_core(&bars128, &masks128),
                count_supermasks_dispatch_core(&bars, &masks)
            );
        }

        #[test]
        fn u128_masking_off_high_limb_equals_u64_count(
            bars in proptest::collection::vec(u128_word(), 0..64),
            masks in proptest::collection::vec(u128_word(), 0..16),
        ) {
            // Truncation-relationship pin: mask every hostile wide word down
            // to its LOW limb; the wide kernel over the truncations must equal
            // the u64 kernel over the same truncations (feeding the u64
            // strategy's hostile low-limb bit patterns into the cross-width
            // agreement) — while the FULL wide count over the original words
            // legitimately diverges whenever a high limb is nonzero, which is
            // exactly the truncation bug class the u128 kernel exists to
            // prevent (so NO assertion equates wide-count with truncated
            // count on the originals).
            let bars_low: Vec<u128> = bars.iter().map(|&b| b & U64_MAX).collect();
            let masks_low: Vec<u128> = masks.iter().map(|&m| m & U64_MAX).collect();
            let bars64: Vec<u64> = bars_low.iter().map(|&b| b as u64).collect();
            let masks64: Vec<u64> = masks_low.iter().map(|&m| m as u64).collect();
            prop_assert_eq!(
                count_supermasks_dispatch_core(&bars_low, &masks_low),
                count_supermasks_dispatch_core(&bars64, &masks64)
            );
        }
    }
}
