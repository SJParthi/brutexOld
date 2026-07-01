"""Hypothesis property tests — Rust ``count_supermasks_u64s`` parity.

Closes the determinism guarantee for the Stage-2 hot kernel shipped under
DEC-RUST-SUPERMASK-KERNEL-001 (ADR-0004 "Rust CPU kernel first"). The Rust
kernel ``fno_math.count_supermasks_u64s`` replaces the inner numpy pass of
``brutex.engine.combo_mask.count_supermasks``; this module proves it is
**bit-for-bit identical** to the numpy reference oracle
``count_supermasks_reference`` for arbitrary bars/masks.

Per CLAUDE.md Golden Rule #4 (reproducibility-first) the Stage-2 hit count
feeds the blake3 run-identity hash; a single off-by-one between the Rust
and numpy paths would silently invalidate every downstream result. The
kernel is pure integer arithmetic — no floats, no reduction-order
ambiguity — so the two paths must agree exactly on every input.

Invariants covered
------------------

1. **Bit-exact parity** — for arbitrary ``(bar_bits, masks)`` the Rust
   kernel equals the numpy reference equals the per-bar ``evaluate_mask``
   ground truth.
2. **Edge cases** — empty masks → ``[]``; empty bars → ``0`` per mask;
   ``mask == 0`` → all bars; full-width (bit 63) masks.
3. **Determinism** — repeated calls on the same input return the same
   result (no hidden state / nondeterminism).
4. **u128 two-limb path** (DEC-DEDUP-CAPACITY-UNIQUE-001) — when a bar/mask
   sets a bit ``>= 64`` the ``count_supermasks`` facade + ``ResidentBarBuffer``
   route to the bit-exact two-limb numpy path; this must equal an independent
   pure-Python scalar ``(bar & mask) == mask`` oracle over the FULL u128 range,
   so a future vocabulary growth past 64 bits cannot silently truncate a high
   bit and corrupt the Stage-2 hit count / blake3 run-identity hash.
"""

from __future__ import annotations

import fno_math
from brutex.engine.combo_mask import (
    ResidentBarBuffer,
    count_supermasks,
    count_supermasks_reference,
    evaluate_mask,
)
from hypothesis import given, settings
from hypothesis import strategies as st

_U64 = st.integers(min_value=0, max_value=(1 << 64) - 1)
_BARS = st.lists(_U64, max_size=200)
_MASKS = st.lists(_U64, max_size=64)

# ── u128 range (DEC-DEDUP-CAPACITY-UNIQUE-001, FIX 3) ─────────────────────
# The Rust hot kernel is u64-native; when ANY bar/mask sets a bit >= 64 the
# ``count_supermasks`` facade + ``ResidentBarBuffer`` route to the bit-exact
# two-limb numpy path (``combo_mask.py:413`` / the fits_u64=False branch) so no
# high bit is truncated. The old strategies only sampled up to ``(1<<64)-1``, so
# that two-limb path had thin property coverage. These strategies span the FULL
# u128 range (bits 0..127) — the ``_HI128`` variants FORCE at least one bit >= 64
# so the two-limb branch is definitely exercised, hardening against a future
# vocabulary growth past 64 bits (the vocab is at 60 bits today, headroom to 128).
_U128 = st.integers(min_value=0, max_value=(1 << 128) - 1)
# A value guaranteed to have a bit >= 64 set (so ``v > U64_MASK`` — the two-limb
# trigger): OR in a random high limb bit.
_HI128 = st.integers(min_value=0, max_value=(1 << 64) - 1).flatmap(
    lambda lo: st.integers(min_value=1, max_value=(1 << 64) - 1).map(lambda hi: lo | (hi << 64))
)
_BARS128 = st.lists(_U128, max_size=200)
_MASKS128 = st.lists(_U128, max_size=64)
_BARS_HI128 = st.lists(_HI128, min_size=1, max_size=200)
_MASKS_HI128 = st.lists(_HI128, min_size=1, max_size=64)


@given(bars=_BARS, masks=_MASKS)
def test_rust_kernel_matches_numpy_reference(bars: list[int], masks: list[int]) -> None:
    """Rust kernel == numpy reference oracle, bit-for-bit."""
    rust = fno_math.count_supermasks_u64s(bars, masks)
    reference = count_supermasks_reference(bars, masks)
    assert list(rust) == reference


@given(bars=_BARS, masks=_MASKS)
def test_facade_matches_reference(bars: list[int], masks: list[int]) -> None:
    """The combo_mask facade (Rust-backed) == numpy reference."""
    assert count_supermasks(bars, masks) == count_supermasks_reference(bars, masks)


@given(bars=_BARS, masks=_MASKS)
def test_kernel_matches_per_bar_ground_truth(bars: list[int], masks: list[int]) -> None:
    """Both paths agree with the per-bar evaluate_mask ground truth."""
    truth = [sum(evaluate_mask(b, m) for b in bars) for m in masks]
    assert count_supermasks(bars, masks) == truth
    assert count_supermasks_reference(bars, masks) == truth


@given(bars=_BARS, masks=_MASKS)
def test_kernel_is_deterministic(bars: list[int], masks: list[int]) -> None:
    """Repeated calls on the same input yield identical output."""
    first = fno_math.count_supermasks_u64s(bars, masks)
    second = fno_math.count_supermasks_u64s(bars, masks)
    assert list(first) == list(second)


# ─────────────────────────────────────────────────────────────────
# Rayon multi-core parity (DEC-RUST-RAYON-SUPERMASK-001, MIGRATION.md #417)
# ─────────────────────────────────────────────────────────────────


# deadline=None: each example builds a fresh scoped rayon ThreadPool per `n`
# (spawns OS threads) — incidental cost that, under coverage instrumentation,
# can exceed hypothesis's 200 ms default deadline. These tests verify
# correctness/determinism, NOT latency, so the deadline is disabled to keep the
# gate deterministic (no timing flake — the O(1)/throughput SLAs live in bench).
@settings(deadline=None)
@given(bars=_BARS, masks=_MASKS)
def test_parallel_equals_serial_all_thread_counts(bars: list[int], masks: list[int]) -> None:
    """The scoped-pool rayon kernel == numpy reference for n in {1, 2, 4}.

    `count_supermasks_u64s_nthreads` runs the masks-outer scan on a SCOPED
    rayon pool of exactly `n` workers via an order-preserving
    `par_iter().map().collect()`. Because the collect is order-preserving and
    every element is a pure integer count (no reduction-order ambiguity), the
    output is bit-identical to the serial numpy reference oracle for ANY worker
    count — the determinism guarantee (Golden Rule #4). One off-by-thread would
    silently invalidate the blake3 run-identity hash.
    """
    reference = count_supermasks_reference(bars, masks)
    for n in (1, 2, 4):
        out = list(fno_math.count_supermasks_u64s_nthreads(bars, masks, n))
        assert out == reference, f"n_threads={n} diverged from serial reference"


@settings(deadline=None)  # builds 2 scoped pools per n — correctness test, not latency
@given(bars=_BARS, masks=_MASKS)
def test_parallel_is_deterministic_repeated_runs(bars: list[int], masks: list[int]) -> None:
    """Repeated parallel runs (and the default-pool kernel) all agree.

    Re-running the scoped-pool kernel — and the production default-global-pool
    `count_supermasks_u64s` — on the same input must yield byte-identical output
    every time (no thread-scheduling nondeterminism leaks into the result).
    """
    reference = count_supermasks_reference(bars, masks)
    for n in (1, 2, 4):
        first = list(fno_math.count_supermasks_u64s_nthreads(bars, masks, n))
        second = list(fno_math.count_supermasks_u64s_nthreads(bars, masks, n))
        assert first == second == reference
    # The production default-pool path must also match, repeatedly.
    assert (
        list(fno_math.count_supermasks_u64s(bars, masks))
        == list(fno_math.count_supermasks_u64s(bars, masks))
        == reference
    )


@given(bars=_BARS)
def test_mask_zero_matches_all_bars(bars: list[int]) -> None:
    """mask == 0 matches every bar (count == len(bars))."""
    assert fno_math.count_supermasks_u64s(bars, [0]) == [len(bars)]
    assert fno_math.count_supermask_u64s(bars, 0) == len(bars)


@given(masks=_MASKS)
def test_empty_bars_zero_per_mask(masks: list[int]) -> None:
    """Empty bars → 0 per mask."""
    assert fno_math.count_supermasks_u64s([], masks) == [0] * len(masks)


@given(bars=_BARS, mask=_U64)
def test_single_mask_matches_batched(bars: list[int], mask: int) -> None:
    """The single-mask convenience equals the batched kernel for that mask."""
    single = fno_math.count_supermask_u64s(bars, mask)
    batched = fno_math.count_supermasks_u64s(bars, [mask])
    assert [single] == list(batched)


# ─────────────────────────────────────────────────────────────────
# ResidentBarBuffer parity (DEC-RUST-RESIDENT-BARS-001)
# ─────────────────────────────────────────────────────────────────


@given(bars=_BARS, masks=_MASKS)
def test_resident_matches_reference(bars: list[int], masks: list[int]) -> None:
    """ResidentBarBuffer == numpy reference == per-bar evaluate_mask truth."""
    truth = [sum(evaluate_mask(b, m) for b in bars) for m in masks]
    assert ResidentBarBuffer(bars).count_supermasks(masks) == count_supermasks_reference(
        bars, masks
    )
    assert count_supermasks_reference(bars, masks) == truth


@given(bars=_BARS, masks=_MASKS)
def test_resident_is_deterministic(bars: list[int], masks: list[int]) -> None:
    """Build once, call twice on the same buffer → identical output."""
    buf = ResidentBarBuffer(bars)
    first = buf.count_supermasks(masks)
    second = buf.count_supermasks(masks)
    assert first == second


@given(bars=_BARS, masks=_MASKS)
def test_resident_reuse_equals_fresh(bars: list[int], masks: list[int]) -> None:
    """A reused buffer agrees with a freshly-built one (no hidden state)."""
    reused = ResidentBarBuffer(bars)
    _ = reused.count_supermasks(masks)  # warm / mutate-if-any
    again = reused.count_supermasks(masks)
    fresh = ResidentBarBuffer(bars).count_supermasks(masks)
    assert again == fresh == count_supermasks(bars, masks)


# ─────────────────────────────────────────────────────────────────
# u128 two-limb path parity (DEC-DEDUP-CAPACITY-UNIQUE-001, FIX 3)
# ─────────────────────────────────────────────────────────────────
#
# The above suites only sample bars/masks up to ``(1<<64)-1``, so the two-limb
# numpy path in ``combo_mask.py`` (taken when any bar/mask has a bit >= 64) had
# thin property coverage. These tests sample inputs spanning the FULL u128 range
# — and, in the ``_hi128`` variants, FORCE a bit >= 64 — and compare against an
# INDEPENDENT pure-Python scalar oracle. Python ints are arbitrary-precision so
# ``(bar & mask) == mask`` is exact over u128; the two-limb numpy path must agree
# bit-for-bit. NB: the u64-native Rust kernel (``fno_math.count_supermasks_u64s``)
# is NOT called with u128 inputs — it would truncate the high limb; the facade
# and buffer route around it by construction, which is the property under test.


def _scalar_superset_count(bars: list[int], masks: list[int]) -> list[int]:
    """Independent pure-Python oracle: per-mask count of superset bars.

    Uses arbitrary-precision Python-int ``(bar & mask) == mask`` — no numpy, no
    Rust, no limb split — so it is a trustworthy ground truth for the FULL u128
    range (and beyond). Mirrors the ``count_supermasks_reference`` contract:
    empty masks → ``[]``; empty bars → ``0`` per mask; ``mask == 0`` → all bars.
    """
    return [sum(1 for b in bars if (b & m) == m) for m in masks]


@given(bars=_BARS128, masks=_MASKS128)
def test_facade_matches_scalar_oracle_u128(bars: list[int], masks: list[int]) -> None:
    """The ``count_supermasks`` facade == pure-Python scalar oracle over u128."""
    assert count_supermasks(bars, masks) == _scalar_superset_count(bars, masks)


@given(bars=_BARS128, masks=_MASKS128)
def test_reference_matches_scalar_oracle_u128(bars: list[int], masks: list[int]) -> None:
    """The numpy two-limb reference == pure-Python scalar oracle over u128."""
    assert count_supermasks_reference(bars, masks) == _scalar_superset_count(bars, masks)


@given(bars=_BARS_HI128, masks=_MASKS_HI128)
def test_facade_routes_to_two_limb_when_bit_ge_64(bars: list[int], masks: list[int]) -> None:
    """A bar/mask with a bit >= 64 forces the two-limb path; it stays exact.

    Every ``bars``/``masks`` element here has a bit >= 64 set (``_HI128``), so
    ``combo_mask.count_supermasks`` MUST route to ``count_supermasks_reference``
    (its ``any(... > U64_MASK ...)`` guard) rather than the u64 Rust kernel. The
    result must equal the independent scalar oracle — proving no high bit is
    dropped.
    """
    assert any(b > (1 << 64) - 1 for b in bars)
    assert any(m > (1 << 64) - 1 for m in masks)
    assert count_supermasks(bars, masks) == _scalar_superset_count(bars, masks)


@given(bars=_BARS128, masks=_MASKS128)
def test_resident_matches_scalar_oracle_u128(bars: list[int], masks: list[int]) -> None:
    """``ResidentBarBuffer`` (two-limb numpy backing) == scalar oracle over u128.

    A bar with a bit >= 64 forces ``fits_u64=False`` so the buffer holds the
    two-limb numpy arrays instead of the Rust ``ResidentBars``; the count must
    still be bit-exact for the full u128 range.
    """
    assert ResidentBarBuffer(bars).count_supermasks(masks) == _scalar_superset_count(bars, masks)


@given(bars=_BARS_HI128, masks=_MASKS_HI128)
def test_resident_two_limb_backing_matches_oracle(bars: list[int], masks: list[int]) -> None:
    """Forced-high-bit bars keep the buffer on its two-limb numpy backing.

    With every bar bit >= 64, ``ResidentBarBuffer`` never builds the u64 Rust
    ``ResidentBars`` — it must serve counts from the two-limb numpy arrays and
    still match the scalar oracle exactly.
    """
    buf = ResidentBarBuffer(bars)
    assert buf.count_supermasks(masks) == _scalar_superset_count(bars, masks)
    # Determinism on the two-limb backing: a second call agrees.
    assert buf.count_supermasks(masks) == buf.count_supermasks(masks)
