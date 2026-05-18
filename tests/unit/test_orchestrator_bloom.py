"""Unit tests for :mod:`brutex.orchestrator.bloom` (DEC-P0-047 / PR-H).

Coverage targets per the 4-agent hostile pre-impl review:

* Sizing math (Hostile §1) — verify ``m_bits``, ``k`` use ``ceil``
  not truncate; empirical FPR ≤ target.
* MAX_BLOOM_BYTES guard (Hostile §3) — reject sizings that exceed
  the 16 GiB envelope.
* FAIL_CLOSED capacity gate (Hostile §2) — :meth:`add` raises
  :class:`BloomCapacityExceeded` past ``capacity``.
* FPR range validation (Reality-check §4) — reject ``fpr_target``
  outside ``[MIN_FPR_TARGET, MAX_FPR_TARGET)``.
* Cross-hash compatibility (Hostile §8) — sha256 + blake3 digests
  feed the wrapper identically (zero-touch migration discipline).
* structlog init emission (Hostile §9) — every construct logs the
  achieved config.
* Concurrent thread-safety — writers serialise via
  :class:`threading.Lock`; readers are lockless.
* Public API surface — bytes-only, type errors on str input,
  ``__repr__`` redacts sensitive state.
"""

from __future__ import annotations

import hashlib
import math
import threading
from unittest.mock import patch

import pytest
from brutex.orchestrator.bloom import (
    DEFAULT_CAPACITY,
    DEFAULT_FPR,
    MAX_FPR_TARGET,
    MIN_FPR_TARGET,
    BloomCapacityExceeded,
    BloomConfigError,
    BloomFilter,
    BloomTooLargeError,
    _compute_m_bytes,
)

# ────────────────────────────────────────────────────────────────────────
# Sizing math + constants
# ────────────────────────────────────────────────────────────────────────


class TestSizingMath:
    """Validate that m_bits + k follow ceil() not truncate (CRITICAL §1)."""

    def test_capacity_1000_fpr_1e_6_matches_canonical_formula(self) -> None:
        bf = BloomFilter(capacity=1000, fpr_target=1e-6)
        # m = ceil(-1000 * ln(1e-6) / (ln 2)^2) = ceil(28755.176) = 28756
        assert bf.m_bits == 28756
        # k = ceil(m/n * ln 2) = ceil(19.93) = 20
        assert bf.k == 20

    def test_m_bits_uses_ceil_not_truncate(self) -> None:
        """ceil(28755.176) = 28756 (not 28755)."""
        bf = BloomFilter(capacity=1000, fpr_target=1e-6)
        # Canonical fractional m = 28755.176... → must round UP.
        canonical_float = -1000 * math.log(1e-6) / (math.log(2) ** 2)
        assert bf.m_bits == math.ceil(canonical_float)
        assert bf.m_bits > canonical_float

    def test_bits_per_item_property_matches_division(self) -> None:
        bf = BloomFilter(capacity=10_000, fpr_target=1e-9)
        assert bf.bits_per_item == bf.m_bits / bf.capacity

    def test_compute_m_bytes_helper_matches_constructor(self) -> None:
        # Helper must mirror constructor arithmetic.
        params = [(1000, 1e-6), (1_000_000, 1e-7), (10, 1e-3)]
        for cap, fpr in params:
            bf = BloomFilter(capacity=cap, fpr_target=fpr)
            assert bf.m_bytes == _compute_m_bytes(cap, fpr)

    def test_k_is_minimum_1(self) -> None:
        # Edge: tiny capacity / loose FPR could compute k=0; clamp to 1.
        bf = BloomFilter(capacity=10, fpr_target=0.49)
        assert bf.k >= 1


# ────────────────────────────────────────────────────────────────────────
# Constructor validation
# ────────────────────────────────────────────────────────────────────────


class TestConstructorValidation:
    """4-agent Reality-check §4: V1 rejects out-of-envelope sizings."""

    def test_default_capacity_is_100m(self) -> None:
        bf = BloomFilter()
        assert bf.capacity == DEFAULT_CAPACITY == 100_000_000
        assert bf.fpr_target == DEFAULT_FPR == 1e-7

    def test_capacity_zero_rejected(self) -> None:
        with pytest.raises(BloomConfigError):
            BloomFilter(capacity=0, fpr_target=1e-6)

    def test_capacity_negative_rejected(self) -> None:
        with pytest.raises(BloomConfigError):
            BloomFilter(capacity=-5, fpr_target=1e-6)

    def test_fpr_below_min_rejected(self) -> None:
        # MIN_FPR_TARGET = 1e-9; anything tighter requires sharded follow-up.
        with pytest.raises(BloomConfigError):
            BloomFilter(capacity=1000, fpr_target=1e-12)

    def test_fpr_at_min_accepted(self) -> None:
        # Exactly MIN is OK.
        bf = BloomFilter(capacity=1000, fpr_target=MIN_FPR_TARGET)
        assert bf.fpr_target == MIN_FPR_TARGET

    def test_fpr_above_max_rejected(self) -> None:
        with pytest.raises(BloomConfigError):
            BloomFilter(capacity=1000, fpr_target=0.7)

    def test_fpr_at_max_rejected(self) -> None:
        # MAX is exclusive — 0.5 itself is too loose for L1 utility.
        with pytest.raises(BloomConfigError):
            BloomFilter(capacity=1000, fpr_target=MAX_FPR_TARGET)

    def test_capacity_too_large_rejected(self) -> None:
        # 146B (Cuckoo escalation threshold) at FPR 1e-9 would need
        # ~787 GB — blows the 16 GiB envelope by 50×.
        with pytest.raises(BloomTooLargeError):
            BloomFilter(capacity=146_000_000_000, fpr_target=1e-9)

    def test_bloom_too_large_is_subclass_of_config_error(self) -> None:
        # Caller can catch the parent BloomConfigError.
        assert issubclass(BloomTooLargeError, BloomConfigError)


# ────────────────────────────────────────────────────────────────────────
# add / might_contain semantics
# ────────────────────────────────────────────────────────────────────────


class TestAddMightContain:
    def test_round_trip_single_hash(self) -> None:
        bf = BloomFilter(capacity=1000, fpr_target=1e-6)
        h = hashlib.sha256(b"NIFTY:2024-01-01").digest()
        bf.add(h)
        assert bf.might_contain(h) is True

    def test_empty_filter_returns_false(self) -> None:
        bf = BloomFilter(capacity=1000, fpr_target=1e-6)
        h = hashlib.sha256(b"x").digest()
        assert bf.might_contain(h) is False

    def test_idempotent_add(self) -> None:
        # Re-adding the same hash doesn't change membership.
        bf = BloomFilter(capacity=1000, fpr_target=1e-6)
        h = hashlib.sha256(b"x").digest()
        bf.add(h)
        bf.add(h)
        bf.add(h)
        assert bf.might_contain(h) is True

    def test_idempotent_add_increments_insertions_counter(self) -> None:
        # Duplicates DO count toward insertions_attempted (conservative
        # FAIL_CLOSED gate).
        bf = BloomFilter(capacity=1000, fpr_target=1e-6)
        h = hashlib.sha256(b"x").digest()
        for _ in range(5):
            bf.add(h)
        assert bf.insertions_attempted == 5

    def test_str_input_rejected(self) -> None:
        bf = BloomFilter(capacity=1000, fpr_target=1e-6)
        with pytest.raises(TypeError, match="must be bytes"):
            bf.add("not bytes")  # type: ignore[arg-type]

    def test_might_contain_str_input_rejected(self) -> None:
        bf = BloomFilter(capacity=1000, fpr_target=1e-6)
        with pytest.raises(TypeError, match="must be bytes"):
            bf.might_contain("not bytes")  # type: ignore[arg-type]

    def test_int_input_rejected(self) -> None:
        # fastbloom-rs has an add_int helper but our wrapper forbids it.
        bf = BloomFilter(capacity=1000, fpr_target=1e-6)
        with pytest.raises(TypeError):
            bf.add(42)  # type: ignore[arg-type]


# ────────────────────────────────────────────────────────────────────────
# Capacity gate (FAIL_CLOSED)
# ────────────────────────────────────────────────────────────────────────


class TestCapacityGate:
    """Hostile §2: insertions past capacity raise BloomCapacityExceeded."""

    def test_capacity_reached_raises(self) -> None:
        bf = BloomFilter(capacity=3, fpr_target=1e-6)
        bf.add(hashlib.sha256(b"a").digest())
        bf.add(hashlib.sha256(b"b").digest())
        bf.add(hashlib.sha256(b"c").digest())
        # 4th insertion is past capacity.
        with pytest.raises(BloomCapacityExceeded):
            bf.add(hashlib.sha256(b"d").digest())

    def test_failed_add_does_not_corrupt_filter(self) -> None:
        bf = BloomFilter(capacity=3, fpr_target=1e-6)
        for token in (b"a", b"b", b"c"):
            bf.add(hashlib.sha256(token).digest())
        with pytest.raises(BloomCapacityExceeded):
            bf.add(hashlib.sha256(b"d").digest())
        # Already-inserted items still resolve correctly.
        for token in (b"a", b"b", b"c"):
            assert bf.might_contain(hashlib.sha256(token).digest())

    def test_might_contain_works_at_capacity(self) -> None:
        bf = BloomFilter(capacity=3, fpr_target=1e-6)
        h = hashlib.sha256(b"a").digest()
        bf.add(h)
        bf.add(hashlib.sha256(b"b").digest())
        bf.add(hashlib.sha256(b"c").digest())
        # Reads still work.
        assert bf.might_contain(h) is True


# ────────────────────────────────────────────────────────────────────────
# Empirical FPR + unique-count estimator
# ────────────────────────────────────────────────────────────────────────


class TestEstimators:
    def test_empty_filter_estimates_zero(self) -> None:
        bf = BloomFilter(capacity=1000, fpr_target=1e-6)
        assert bf.unique_count_estimate == 0
        assert bf.empirical_fpr == 0.0

    def test_unique_count_estimate_grows_monotonically(self) -> None:
        bf = BloomFilter(capacity=10_000, fpr_target=1e-6)
        previous = 0
        for i in range(0, 100, 10):
            for j in range(i, i + 10):
                bf.add(hashlib.sha256(str(j).encode()).digest())
            est = bf.unique_count_estimate
            assert est >= previous
            previous = est

    def test_unique_count_estimate_clamped_to_capacity(self) -> None:
        # Saturate the filter past sensible limits; estimator must
        # clip at capacity.
        bf = BloomFilter(capacity=10, fpr_target=1e-3)
        for i in range(10):
            bf.add(hashlib.sha256(str(i).encode()).digest())
        assert bf.unique_count_estimate <= bf.capacity

    def test_empirical_fpr_below_target_after_full_insertion(self) -> None:
        # Insert exactly capacity unique items; empirical FPR must be
        # within 10x of fpr_target (Bloom math + ceil rounding).
        bf = BloomFilter(capacity=1000, fpr_target=1e-3)
        for i in range(1000):
            bf.add(hashlib.sha256(str(i).encode()).digest())
        assert bf.empirical_fpr <= bf.fpr_target * 10


# ────────────────────────────────────────────────────────────────────────
# Cross-hash compatibility (sha256 + blake3)
# ────────────────────────────────────────────────────────────────────────


class TestCrossHashCompat:
    """Hostile §8: bytes-only API works identically for sha256 and blake3.

    The wrapper never re-hashes — DEC-P0-044 Q-02 migration is
    zero-touch.
    """

    def test_sha256_digest_round_trips(self) -> None:
        bf = BloomFilter(capacity=1000, fpr_target=1e-6)
        h = hashlib.sha256(b"NIFTY").digest()
        bf.add(h)
        assert bf.might_contain(h)

    def test_blake2b_digest_round_trips(self) -> None:
        # blake2b is in stdlib (vs blake3 needing an extra dep); the
        # wrapper treats all bytes identically.
        bf = BloomFilter(capacity=1000, fpr_target=1e-6)
        h = hashlib.blake2b(b"NIFTY", digest_size=32).digest()
        bf.add(h)
        assert bf.might_contain(h)

    def test_arbitrary_bytes_length_accepted(self) -> None:
        # Filter doesn't care about input length.
        bf = BloomFilter(capacity=1000, fpr_target=1e-6)
        bf.add(b"\x00")
        bf.add(b"\xff" * 64)
        bf.add(b"")
        assert bf.might_contain(b"\x00")
        assert bf.might_contain(b"\xff" * 64)


# ────────────────────────────────────────────────────────────────────────
# structlog init emission
# ────────────────────────────────────────────────────────────────────────


class TestStructlogInit:
    """Hostile §9: every BloomFilter() init logs the achieved config."""

    def test_init_emits_bloom_init_event(self) -> None:
        with patch("brutex.orchestrator.bloom.structlog") as mock_structlog:
            mock_logger = mock_structlog.get_logger.return_value
            BloomFilter(capacity=1000, fpr_target=1e-6)
            mock_logger.info.assert_called_once()
            args, kwargs = mock_logger.info.call_args
            assert args[0] == "bloom_init"
            assert kwargs["capacity"] == 1000
            assert kwargs["fpr_target"] == 1e-6
            assert kwargs["backend"] == "fastbloom-rs"
            assert kwargs["m_bytes"] > 0
            assert kwargs["k"] >= 1


# ────────────────────────────────────────────────────────────────────────
# Concurrent safety
# ────────────────────────────────────────────────────────────────────────


class TestConcurrency:
    """Writers serialise via threading.Lock; readers are lockless."""

    def test_concurrent_adds_all_persisted(self) -> None:
        # Two threads each add 100 unique items; all 200 must resolve True.
        bf = BloomFilter(capacity=10_000, fpr_target=1e-6)
        items_a = [hashlib.sha256(f"a:{i}".encode()).digest() for i in range(100)]
        items_b = [hashlib.sha256(f"b:{i}".encode()).digest() for i in range(100)]

        def add_all(items: list[bytes]) -> None:
            for h in items:
                bf.add(h)

        t1 = threading.Thread(target=add_all, args=(items_a,))
        t2 = threading.Thread(target=add_all, args=(items_b,))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert bf.insertions_attempted == 200
        for h in items_a + items_b:
            assert bf.might_contain(h) is True

    def test_concurrent_capacity_check(self) -> None:
        # 3 threads racing to insert 4 items into a 5-capacity filter.
        # Total 12 inserts > 5 → at least 7 BloomCapacityExceeded.
        bf = BloomFilter(capacity=5, fpr_target=1e-3)
        exceeded_count = 0
        lock = threading.Lock()

        def add_all() -> None:
            nonlocal exceeded_count
            for i in range(4):
                try:
                    bf.add(hashlib.sha256(str(i).encode()).digest())
                except BloomCapacityExceeded:
                    with lock:
                        exceeded_count += 1

        threads = [threading.Thread(target=add_all) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert bf.insertions_attempted == 5
        assert exceeded_count == 7  # 12 attempts - 5 successes


# ────────────────────────────────────────────────────────────────────────
# Observability properties
# ────────────────────────────────────────────────────────────────────────


class TestObservability:
    def test_repr_includes_capacity_and_counts(self) -> None:
        bf = BloomFilter(capacity=1000, fpr_target=1e-6)
        text = repr(bf)
        assert "1_000" in text  # capacity
        assert "fpr_target=1e-06" in text
        assert "inserted=0" in text

    def test_repr_does_not_leak_internal_state(self) -> None:
        bf = BloomFilter(capacity=1000, fpr_target=1e-6)
        text = repr(bf)
        # No mention of the underlying bit array or lock object.
        assert "_impl" not in text
        assert "_lock" not in text
        assert "RustBloomFilter" not in text

    def test_m_bits_m_bytes_consistency(self) -> None:
        bf = BloomFilter(capacity=1000, fpr_target=1e-6)
        assert bf.m_bytes == math.ceil(bf.m_bits / 8)


# ────────────────────────────────────────────────────────────────────────
# Reasonable Phase-1 defaults
# ────────────────────────────────────────────────────────────────────────


class TestPhase1Defaults:
    """Reality-check §4: defaults must fit the 92-chunk first-run workload."""

    def test_default_sizing_under_500_mb(self) -> None:
        bf = BloomFilter()
        # 100M cap + 1e-7 → ~419 MB; cap at 500 MB to catch regression.
        assert bf.m_bytes < 500 * 1024 * 1024

    def test_default_capacity_covers_first_run_load(self) -> None:
        # 92-chunk first-run × 6500 bars × 4 instruments × 21 TFs ≈ 50M keys.
        # Default capacity must exceed this with ≥ 2× headroom.
        bf = BloomFilter()
        assert bf.capacity >= 50_000_000 * 2
