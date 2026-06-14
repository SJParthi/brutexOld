"""Hypothesis property tests for :mod:`brutex.orchestrator.bloom` (DEC-P0-049).

Exhaustively explores random ``(capacity, fpr_target)`` parameterisations
and random input sequences to prove the BloomFilter's core invariants
from ``docs/MIGRATION.md`` entries #184 (DEC-P0-047) and #185 (DEC-P0-048):

1. **No false negatives.** For any sequence of unique ``content_hash``
   values added to the filter, every subsequent :meth:`might_contain`
   on those exact hashes returns ``True``. The Bloom contract is
   "false positives possible, false negatives never" (modulo the
   documented concurrent-write race — these tests are single-threaded).

2. **Empirical FPR within target.** For a sweep of ``(n, p)`` pairs,
   the achieved empirical false-positive rate on 1000 disjoint probe
   hashes is bounded above by ``fpr_target × 5`` (loose envelope so
   the property doesn't flake on small-sample variance; the
   :class:`BloomFilter` sizing math guarantees ≤ ``fpr_target`` at
   :attr:`capacity` saturation).

3. **Sizing monotonicity.** ``m_bits`` and ``k`` are monotonic
   non-decreasing as ``capacity`` rises (with ``fpr_target`` fixed)
   and as ``fpr_target`` falls (with ``capacity`` fixed). Catches the
   Hostile-lens §1 truncate-vs-ceil regression on either dimension.

4. **Determinism across instances.** Two :class:`BloomFilter` objects
   constructed with identical ``(capacity, fpr_target)`` and fed the
   same insertion sequence produce equal :attr:`m_bits`,
   :attr:`m_bytes`, :attr:`k`, :attr:`insertions_attempted`, and equal
   :meth:`might_contain` answers on every probe.

5. **Insertions-counter discipline.** ``insertions_attempted`` equals
   the exact number of :meth:`add` calls (including duplicates) for
   any input sequence within capacity.

6. **Capacity gate triggers exactly at threshold.** Adding exactly
   ``capacity`` items succeeds; the ``(capacity + 1)``-th add raises
   :class:`BloomCapacityExceeded`.

7. **`MIN_FPR_TARGET` floor + `MAX_FPR_TARGET` ceiling rejected.**
   Any ``fpr_target < MIN_FPR_TARGET`` OR ``fpr_target >= MAX_FPR_TARGET``
   raises :class:`BloomConfigError` at construction.

Uses Hypothesis bounded strategies to keep per-property runtime under
a second. The empirical-FPR property runs with reduced
``max_examples`` so the slow inner loop doesn't blow the CI budget.
"""

from __future__ import annotations

import hashlib
import math

import fno_math
import pytest
from brutex.orchestrator.bloom import (
    MAX_FPR_TARGET,
    MIN_FPR_TARGET,
    BloomCapacityExceeded,
    BloomConfigError,
    BloomFilter,
)
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ────────────────────────────────────────────────────────────────────────
# Hypothesis strategies — bounded to keep tests fast
# ────────────────────────────────────────────────────────────────────────


# Capacity in [10, 5_000] — keeps bit-arrays small (under 30 KB), tests fast.
_capacities = st.integers(min_value=10, max_value=5_000)

# FPR in [1e-9, 0.4] — covers the V1 envelope without saturating the
# 0.5 exclusive ceiling. Use log-scaled sampling so we explore the
# tighter (more interesting) end of the range.
_fprs = st.floats(
    min_value=MIN_FPR_TARGET,
    max_value=0.4,
    exclude_min=False,
    allow_nan=False,
    allow_infinity=False,
)

# 32-byte content_hash values (sha256-shaped).
_hashes = st.binary(min_size=32, max_size=32)


# ────────────────────────────────────────────────────────────────────────
# 1. No false negatives
# ────────────────────────────────────────────────────────────────────────


class TestNoFalseNegatives:
    """Invariant: add(x) ⇒ might_contain(x) == True. Always."""

    @given(
        capacity=_capacities,
        fpr_target=_fprs,
        items=st.lists(_hashes, min_size=1, max_size=100, unique=True),
    )
    @settings(max_examples=50, deadline=None)
    def test_added_hashes_always_contained(
        self,
        capacity: int,
        fpr_target: float,
        items: list[bytes],
    ) -> None:
        # Skip cases where item count exceeds capacity (gate-tested elsewhere).
        if len(items) > capacity:
            return
        bf = BloomFilter(capacity=capacity, fpr_target=fpr_target)
        for h in items:
            bf.add(h)
        for h in items:
            assert bf.might_contain(h) is True, (
                f"False negative for {h.hex()[:16]}... at capacity={capacity}, fpr={fpr_target}"
            )


# ────────────────────────────────────────────────────────────────────────
# 2. Empirical FPR within target
# ────────────────────────────────────────────────────────────────────────


class TestEmpiricalFprBounded:
    """Empirical FPR ≤ fpr_target × 5 at full capacity."""

    # Minimum EXPECTED false-positive count the probe sample must yield, so the
    # 5× multiplicative envelope below is statistically meaningful. A pure
    # multiplicative bound is noise at small expected counts: at fpr_target=1e-3
    # a fixed 1000-probe sample expects only ~1 false positive, so a single
    # extra FP from sampling variance (the deterministic SHA-256 inputs make the
    # breach reproducible, not flaky) blows a 5× bound. Scaling the probe count
    # to expect ≥ this many FPs keeps the measurement adequately powered across
    # the whole fpr_target range (DEC-BLOOM-FPR-SAMPLE-001).
    _MIN_EXPECTED_FP = 10
    # Absolute discrete-tail cushion on top of the 5× envelope. With expected
    # count E ≥ 10 the 5× bound (50) is already astronomically safe under
    # Poisson noise (P(X≥55 | λ=10) ≈ 1e-20); the +5 only absorbs the modest,
    # legitimate design-FPR overshoot from integer-k rounding at small E.
    _FPR_TAIL_CUSHION = 5

    @given(
        capacity=st.integers(min_value=200, max_value=1000),
        fpr_target=st.floats(
            min_value=1e-3,
            max_value=0.1,
            allow_nan=False,
            allow_infinity=False,
        ),
    )
    @settings(
        max_examples=10,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
    )
    def test_fpr_within_target_at_capacity(
        self,
        capacity: int,
        fpr_target: float,
    ) -> None:
        bf = BloomFilter(capacity=capacity, fpr_target=fpr_target)
        # Insert exactly `capacity` deterministic hashes.
        inserted = {hashlib.sha256(f"in:{i}".encode()).digest() for i in range(capacity)}
        for h in inserted:
            bf.add(h)
        # Probe with enough disjoint, never-inserted hashes that the EXPECTED
        # false-positive count is ≥ _MIN_EXPECTED_FP (floor 1000), so the 5×
        # envelope is statistically sound at every fpr_target — not just loose.
        n_probes = max(1000, math.ceil(self._MIN_EXPECTED_FP / fpr_target))
        probes = [hashlib.sha256(f"probe:{i}".encode()).digest() for i in range(n_probes)]
        # Filter out any probe that accidentally collides with the
        # inserted set (statistically zero but defensive).
        probes = [p for p in probes if p not in inserted]
        false_positives = sum(1 for p in probes if bf.might_contain(p))
        empirical_fpr = false_positives / len(probes)
        # 5× design envelope (absorbs integer-k rounding overshoot) plus an
        # absolute discrete-tail cushion for the low-count regime. Asserting on
        # the COUNT keeps the cushion meaningful when expected counts are small.
        expected_fp = len(probes) * fpr_target
        max_false_positives = expected_fp * 5 + self._FPR_TAIL_CUSHION
        assert false_positives <= max_false_positives, (
            f"FPR breach at capacity={capacity}, fpr_target={fpr_target}: "
            f"observed {false_positives} FPs (empirical_fpr={empirical_fpr}) on "
            f"{len(probes)} probes; allowed ≤ {max_false_positives:.1f}"
        )


# ────────────────────────────────────────────────────────────────────────
# 3. Sizing monotonicity
# ────────────────────────────────────────────────────────────────────────


class TestSizingMonotonicity:
    """``m_bits`` and ``k`` are monotonic in (capacity, fpr_target)."""

    @given(
        capacity=_capacities,
        fpr_target=_fprs,
        delta=st.integers(min_value=1, max_value=100),
    )
    @settings(max_examples=30, deadline=None)
    def test_m_bits_grows_with_capacity(
        self,
        capacity: int,
        fpr_target: float,
        delta: int,
    ) -> None:
        a = BloomFilter(capacity=capacity, fpr_target=fpr_target)
        b = BloomFilter(capacity=capacity + delta, fpr_target=fpr_target)
        assert b.m_bits >= a.m_bits

    @given(
        capacity=_capacities,
        fpr_a=_fprs,
        fpr_b=_fprs,
    )
    @settings(max_examples=30, deadline=None)
    def test_m_bits_grows_as_fpr_tightens(
        self,
        capacity: int,
        fpr_a: float,
        fpr_b: float,
    ) -> None:
        a = BloomFilter(capacity=capacity, fpr_target=fpr_a)
        b = BloomFilter(capacity=capacity, fpr_target=fpr_b)
        # Tighter FPR (smaller p) ⇒ larger bit array.
        if fpr_a < fpr_b:
            assert a.m_bits >= b.m_bits
        elif fpr_b < fpr_a:
            assert b.m_bits >= a.m_bits


# ────────────────────────────────────────────────────────────────────────
# 4. Determinism across instances
# ────────────────────────────────────────────────────────────────────────


class TestDeterminism:
    """Two filters with identical params + inputs produce identical state."""

    @given(
        capacity=_capacities,
        fpr_target=_fprs,
        items=st.lists(_hashes, min_size=0, max_size=50, unique=True),
    )
    @settings(max_examples=30, deadline=None)
    def test_two_filters_identical_state(
        self,
        capacity: int,
        fpr_target: float,
        items: list[bytes],
    ) -> None:
        if len(items) > capacity:
            return
        a = BloomFilter(capacity=capacity, fpr_target=fpr_target)
        b = BloomFilter(capacity=capacity, fpr_target=fpr_target)
        # Same sizing.
        assert a.m_bits == b.m_bits
        assert a.m_bytes == b.m_bytes
        assert a.k == b.k
        # Same inputs.
        for h in items:
            a.add(h)
            b.add(h)
        # Same observable state.
        assert a.insertions_attempted == b.insertions_attempted
        # Probe with 20 deterministic hashes — both filters agree.
        for i in range(20):
            probe = hashlib.sha256(f"probe:{i}".encode()).digest()
            assert a.might_contain(probe) == b.might_contain(probe)


# ────────────────────────────────────────────────────────────────────────
# 5. Insertions-counter discipline
# ────────────────────────────────────────────────────────────────────────


class TestInsertionsCounter:
    """``insertions_attempted`` equals the exact ``add()`` call count."""

    @given(
        n_adds=st.integers(min_value=0, max_value=200),
    )
    @settings(max_examples=20, deadline=None)
    def test_counter_increments_per_add(self, n_adds: int) -> None:
        bf = BloomFilter(capacity=max(n_adds, 1), fpr_target=1e-3)
        # Same hash N times (duplicates count, per Hostile §2 docs).
        h = hashlib.sha256(b"x").digest()
        for _ in range(n_adds):
            bf.add(h)
        assert bf.insertions_attempted == n_adds


# ────────────────────────────────────────────────────────────────────────
# 6. Capacity gate triggers exactly at threshold
# ────────────────────────────────────────────────────────────────────────


class TestCapacityGateThreshold:
    """The ``(capacity+1)``-th add() raises; the ``capacity``-th does not."""

    @given(capacity=st.integers(min_value=1, max_value=200))
    @settings(max_examples=20, deadline=None)
    def test_capacity_th_add_succeeds(self, capacity: int) -> None:
        bf = BloomFilter(capacity=capacity, fpr_target=1e-3)
        # Insert exactly capacity items.
        for i in range(capacity):
            bf.add(hashlib.sha256(f"k:{i}".encode()).digest())
        assert bf.insertions_attempted == capacity

    @given(capacity=st.integers(min_value=1, max_value=200))
    @settings(max_examples=20, deadline=None)
    def test_capacity_plus_one_add_raises(self, capacity: int) -> None:
        bf = BloomFilter(capacity=capacity, fpr_target=1e-3)
        for i in range(capacity):
            bf.add(hashlib.sha256(f"k:{i}".encode()).digest())
        with pytest.raises(BloomCapacityExceeded):
            bf.add(hashlib.sha256(b"overflow").digest())


# ────────────────────────────────────────────────────────────────────────
# 7. FPR floor + ceiling rejection
# ────────────────────────────────────────────────────────────────────────


class TestFprBoundaryRejection:
    """``fpr_target`` outside ``[MIN_FPR_TARGET, MAX_FPR_TARGET)`` rejected."""

    @given(
        fpr_target=st.floats(
            min_value=0.0,
            max_value=MIN_FPR_TARGET,
            exclude_max=True,  # below MIN_FPR_TARGET only
            allow_nan=False,
            allow_infinity=False,
        ),
    )
    @settings(max_examples=10, deadline=None)
    def test_fpr_below_min_rejected(self, fpr_target: float) -> None:
        with pytest.raises(BloomConfigError):
            BloomFilter(capacity=1000, fpr_target=fpr_target)

    @given(
        fpr_target=st.floats(
            min_value=MAX_FPR_TARGET,
            max_value=1.5,
            allow_nan=False,
            allow_infinity=False,
        ),
    )
    @settings(max_examples=10, deadline=None)
    def test_fpr_at_or_above_max_rejected(self, fpr_target: float) -> None:
        with pytest.raises(BloomConfigError):
            BloomFilter(capacity=1000, fpr_target=fpr_target)


# ────────────────────────────────────────────────────────────────────────
# 8. Sizing math vs canonical formula
# ────────────────────────────────────────────────────────────────────────


class TestSizingMathProperty:
    """``m_bits`` is ``ceil()``-rounded from the Bloom 1970 formula."""

    @given(
        capacity=_capacities,
        fpr_target=_fprs,
    )
    @settings(max_examples=50, deadline=None)
    def test_m_bits_at_or_above_canonical_float(
        self,
        capacity: int,
        fpr_target: float,
    ) -> None:
        bf = BloomFilter(capacity=capacity, fpr_target=fpr_target)
        canonical = -capacity * math.log(fpr_target) / (math.log(2) ** 2)
        # Wrapper uses ceil() — must equal or exceed canonical float.
        assert bf.m_bits >= canonical
        # And must be exactly the ceiling.
        assert bf.m_bits == math.ceil(canonical)


# ────────────────────────────────────────────────────────────────────────
# 9. Rust popcount equivalence (DEC-P0-057 / PR-N+4)
# ────────────────────────────────────────────────────────────────────────


class TestRustPopcountEquivalence:
    """``fno_math.popcount_bytes`` must agree bit-for-bit with the Python reference.

    The :attr:`BloomFilter.unique_count_estimate` property's bit-density scan
    is delegated to the Rust ``popcount_bytes`` kernel (DEC-P0-057). This
    property test cross-checks the Rust kernel against the obviously-correct
    Python reference ``sum(byte.bit_count() for byte in data)`` on arbitrary
    byte sequences plus the actual bit array of a populated filter.

    Catches any future divergence — Rust kernel regression, wheel ABI drift,
    endianness bug in the u64 chunk loop — at every CI run.
    """

    @given(data=st.binary(min_size=0, max_size=4096))
    @settings(max_examples=100, deadline=None)
    def test_popcount_matches_python_reference_on_arbitrary_bytes(
        self,
        data: bytes,
    ) -> None:
        """Rust popcount of any byte sequence equals the Python reference."""
        python_ref = sum(byte.bit_count() for byte in data)
        rust_kernel = fno_math.popcount_bytes(data)
        assert rust_kernel == python_ref, (
            f"popcount divergence on {len(data)}-byte input: "
            f"Rust={rust_kernel} vs Python={python_ref}"
        )

    @given(
        capacity=_capacities,
        fpr_target=_fprs,
        insertions=st.lists(st.binary(min_size=32, max_size=32), max_size=200),
    )
    @settings(
        max_examples=20,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    def test_popcount_matches_on_live_bloom_bit_array(
        self,
        capacity: int,
        fpr_target: float,
        insertions: list[bytes],
    ) -> None:
        """Rust popcount of a real BloomFilter's bit array equals the Python reference.

        Exercises the exact byte buffer that
        :attr:`BloomFilter.unique_count_estimate` feeds to the kernel.
        """
        bf = BloomFilter(capacity=capacity, fpr_target=fpr_target)
        for content_hash in insertions[: bf.capacity]:
            bf.add(content_hash)
        raw = bf._impl.get_bytes()
        python_ref = sum(byte.bit_count() for byte in raw)
        rust_kernel = fno_math.popcount_bytes(bytes(raw))
        assert rust_kernel == python_ref, (
            f"popcount divergence on Bloom({capacity},{fpr_target}) "
            f"with {len(insertions)} insertions: "
            f"Rust={rust_kernel} vs Python={python_ref}"
        )
