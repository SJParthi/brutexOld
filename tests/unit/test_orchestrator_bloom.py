"""Unit tests for :mod:`brutex.orchestrator.bloom` (DEC-P0-047 / PR-H).

Coverage targets per the 4-agent hostile pre-impl review:

* Sizing math (Hostile §1) — verify ``m_bits``, ``k`` use ``ceil``
  not truncate; empirical FPR ≤ target.
* MAX_BLOOM_BYTES guard (Hostile §3) — reject sizings that exceed
  the RAM-derived ceiling (DEC-RAM-BUDGETS-001; the DEC-P0-047 16 GiB
  value is now the undetected-RAM floor + exact 48 GiB-box parity).
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
import sys
import threading
from collections.abc import Iterator
from unittest.mock import patch

import pytest
from brutex.hardware_envelope import UNDETECTED_RAM, reset_hardware_envelope_cache
from brutex.orchestrator.bloom import (
    DEFAULT_CAPACITY,
    DEFAULT_FPR,
    MAX_BLOOM_BYTES,
    MAX_BLOOM_BYTES_ENV,
    MAX_BLOOM_BYTES_FLOOR,
    MAX_BLOOM_BYTES_FRACTION,
    MAX_FPR_TARGET,
    MIN_FPR_TARGET,
    BloomCapacityExceeded,
    BloomConfigError,
    BloomFilter,
    BloomTooLargeError,
    _compute_m_bytes,
    _popcount_bytes,
    resolve_max_bloom_bytes,
)

_GIB = 1024**3


def _simulate_ram(monkeypatch: pytest.MonkeyPatch, total_bytes: int) -> None:
    """Force RAM detection to *total_bytes* (0 = the undetected sentinel)."""
    monkeypatch.setattr("brutex.hardware_envelope._sysconf_total_ram_bytes", lambda: total_bytes)
    monkeypatch.setattr("brutex.hardware_envelope._sysctl_total_ram_bytes", lambda: UNDETECTED_RAM)
    reset_hardware_envelope_cache()


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
        # ~787 GB — blows the 16 GiB floor by 50× (and any realistic
        # RAM-derived ceiling: a machine would need ~2.3 TB RAM for its
        # 1/3 share to admit it).
        with pytest.raises(BloomTooLargeError):
            BloomFilter(capacity=146_000_000_000, fpr_target=1e-9)

    def test_capacity_too_large_rejected_at_the_16_gib_floor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # DEC-RAM-BUDGETS-001 back-compat pin: with the ceiling forced to the
        # undetected-RAM floor (the pre-derivation DEC-P0-047 constant), the
        # guard behaves exactly as before — refusal BEFORE any allocation.
        monkeypatch.setattr("brutex.orchestrator.bloom.MAX_BLOOM_BYTES", MAX_BLOOM_BYTES_FLOOR)
        with pytest.raises(BloomTooLargeError, match="exceeds MAX_BLOOM_BYTES"):
            BloomFilter(capacity=146_000_000_000, fpr_target=1e-9)

    def test_bloom_too_large_is_subclass_of_config_error(self) -> None:
        # Caller can catch the parent BloomConfigError.
        assert issubclass(BloomTooLargeError, BloomConfigError)


# ────────────────────────────────────────────────────────────────────────
# MAX_BLOOM_BYTES derivation (DEC-RAM-BUDGETS-001)
# ────────────────────────────────────────────────────────────────────────


class TestMaxBloomBytesDerivation:
    """The RAM-derived Bloom ceiling: floor sentinel, 48 GiB parity, env knob."""

    @pytest.fixture(autouse=True)
    def _ram_seam(self) -> Iterator[None]:
        """Isolate each test from the process-global RAM detection cache."""
        reset_hardware_envelope_cache()
        yield
        reset_hardware_envelope_cache()

    def test_floor_when_ram_undetected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The sentinel law: undetected RAM → the DEC-P0-047 16 GiB constant
        # EXACTLY (pre-derivation behavior, bit-for-bit).
        _simulate_ram(monkeypatch, UNDETECTED_RAM)
        assert resolve_max_bloom_bytes({}) == MAX_BLOOM_BYTES_FLOOR == 16 * _GIB

    def test_48_gib_box_derives_exact_old_constant(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The CLAUDE.md §8 regression pin: 48 GiB × 1/3 == 16 GiB.
        _simulate_ram(monkeypatch, 48 * _GIB)
        assert resolve_max_bloom_bytes({}) == MAX_BLOOM_BYTES_FLOOR

    def test_detected_ram_scales_by_fraction(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _simulate_ram(monkeypatch, 96 * _GIB)
        assert resolve_max_bloom_bytes({}) == int(96 * _GIB * MAX_BLOOM_BYTES_FRACTION)

    def test_small_box_derives_a_smaller_wall(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Portability: the guard refuses BEFORE an allocation a small box
        # could never hold — deliberately below the old constant.
        _simulate_ram(monkeypatch, 6 * _GIB)
        assert resolve_max_bloom_bytes({}) == 2 * _GIB

    def test_env_override_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _simulate_ram(monkeypatch, 48 * _GIB)
        assert resolve_max_bloom_bytes({MAX_BLOOM_BYTES_ENV: "123456"}) == 123456

    def test_garbage_env_fails_loud(self) -> None:
        with pytest.raises(ValueError, match=MAX_BLOOM_BYTES_ENV):
            resolve_max_bloom_bytes({MAX_BLOOM_BYTES_ENV: "16GiB"})

    @pytest.mark.parametrize("raw", ["0", "-1"])
    def test_non_positive_env_fails_loud(self, raw: str) -> None:
        with pytest.raises(ValueError, match="must be > 0 bytes"):
            resolve_max_bloom_bytes({MAX_BLOOM_BYTES_ENV: raw})

    def test_module_constant_matches_process_resolution(self) -> None:
        # MAX_BLOOM_BYTES was derived ONCE at import (once-per-process law);
        # re-resolving against the same process environment reproduces it.
        assert resolve_max_bloom_bytes() == MAX_BLOOM_BYTES

    def test_guard_refuses_just_past_the_derived_wall(self) -> None:
        # The BloomTooLargeError wall tracks the DERIVED ceiling: a capacity
        # sized to need just over MAX_BLOOM_BYTES is refused BEFORE any
        # allocation (the guard runs before the Rust bit array is built).
        bits_per_item = -math.log(DEFAULT_FPR) / math.log(2) ** 2
        just_past = int(MAX_BLOOM_BYTES * 8 / bits_per_item * 1.01)
        with pytest.raises(BloomTooLargeError, match="exceeds MAX_BLOOM_BYTES"):
            BloomFilter(capacity=just_past, fpr_target=DEFAULT_FPR)


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

    def test_capacity_exceeded_message_names_operator_knob(self) -> None:
        """R2-SL-F4-F5 knob surface: the FAIL_CLOSED message is ACTIONABLE.

        The capacity limit is the constructor's ``capacity`` — for the sweep
        dedup cache an operator knob — so the raise names the tunables
        (``--sweep-cache-bloom-capacity`` / ``BRUTEX_SWEEP_CACHE_BLOOM_CAPACITY``)
        alongside the reached limit. Doc/message surface only — the gate's
        math + FAIL_CLOSED semantics are byte-unchanged (the sibling tests
        above).
        """
        bf = BloomFilter(capacity=1, fpr_target=1e-6)
        bf.add(hashlib.sha256(b"a").digest())
        with pytest.raises(BloomCapacityExceeded) as excinfo:
            bf.add(hashlib.sha256(b"b").digest())
        msg = str(excinfo.value)
        assert "capacity 1 reached" in msg
        assert "--sweep-cache-bloom-capacity" in msg
        assert "BRUTEX_SWEEP_CACHE_BLOOM_CAPACITY" in msg


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

    def test_saturated_bit_array_estimates_capacity(self) -> None:
        """A fully-set bit array diverges the estimator → return capacity.

        ``bits_set >= m_bits`` is unreachable through the public API (the
        FAIL_CLOSED capacity gate stops insertions long before saturation),
        so the popcount is stubbed to report a saturated array
        (DEC-ORCH-STRICT-GATES-001 gap closure, bloom.py:482).
        """
        bf = BloomFilter(capacity=10, fpr_target=1e-3)
        bf.add(hashlib.sha256(b"one").digest())
        with patch("brutex.orchestrator.bloom._popcount_bytes", return_value=bf.m_bits):
            assert bf.unique_count_estimate == bf.capacity

    def test_log_domain_error_estimates_capacity(self) -> None:
        """A float-collapse inside ``math.log`` clips to capacity.

        The ``bits_set >= m_bits`` guard makes ``1 - bits_set/m_bits``
        strictly positive in exact arithmetic, but the defensive
        ``except ValueError`` (bloom.py:485-486) protects against float
        round-off collapsing the log argument to 0.0 on filters whose
        ``m_bits`` exceeds the float mantissa — forced here by stubbing
        ``math.log`` (DEC-ORCH-STRICT-GATES-001 gap closure).
        """
        bf = BloomFilter(capacity=10, fpr_target=1e-3)
        bf.add(hashlib.sha256(b"one").digest())
        with patch(
            "brutex.orchestrator.bloom.math.log",
            side_effect=ValueError("math domain error"),
        ):
            assert bf.unique_count_estimate == bf.capacity


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


# ────────────────────────────────────────────────────────────────────────
# Empirical FPR measurement (DEC-P0-049 / PR-J)
# ────────────────────────────────────────────────────────────────────────


class TestEmpiricalFprAtScale:
    """Insert N hashes; measure observed FPR on disjoint probes.

    Marked slow because it inserts + probes ~30k hashes. Validates the
    sizing math against an end-to-end workload that the unit-test +
    property suites only spot-check.
    """

    def test_empirical_fpr_within_target_at_10k_capacity(self) -> None:
        """At ``capacity=10_000, fpr_target=1e-3`` the observed FPR on
        20k disjoint probes must be ≤ ``fpr_target × 5`` (envelope for
        finite-sample variance).
        """
        capacity = 10_000
        fpr_target = 1e-3
        bf = BloomFilter(capacity=capacity, fpr_target=fpr_target)
        # Insert exactly capacity unique hashes.
        for i in range(capacity):
            bf.add(hashlib.sha256(f"in:{i}".encode()).digest())
        # Probe 20k disjoint hashes.
        probe_count = 20_000
        false_positives = 0
        for i in range(probe_count):
            probe = hashlib.sha256(f"probe:{i}".encode()).digest()
            if bf.might_contain(probe):
                false_positives += 1
        observed = false_positives / probe_count
        # 5× envelope is loose; tighter would flake on the 20k-sample
        # variance. The Bloom math guarantees ≤ fpr_target at sat'n.
        assert observed <= fpr_target * 5, (
            f"FPR breach: observed {observed} > {fpr_target * 5} on "
            f"{probe_count} probes at capacity={capacity}"
        )


class TestPopcountFallback:
    """Regression: Bloom must not hard-require the optional ``fno_math`` kernel.

    Before DEC-CLONE-RUN-001 (MIGRATION.md entry #278) ``bloom.py``
    imported ``fno_math`` at module top, so every ``brutex`` command
    crashed on a plain ``pip install`` that lacked the maturin-built Rust
    wheel. :func:`_popcount_bytes` now imports it lazily and falls back to
    a pure-Python ``int.bit_count()`` path. These tests pin the fallback's
    exactness so the regression cannot return.
    """

    @staticmethod
    def _reference_popcount(raw: bytes) -> int:
        """Naive set-bit count, independent of the implementation."""
        return sum(byte.bit_count() for byte in raw)

    @pytest.mark.parametrize(
        "raw",
        [
            b"",
            b"\x00",
            b"\xff",
            b"\x0f\xf0",
            b"\x80\x01\x7f",
            bytes(range(256)),
        ],
    )
    def test_fallback_matches_reference_without_fno_math(
        self, raw: bytes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With ``fno_math`` forced absent, the stdlib fallback is exact."""
        # A ``None`` entry in sys.modules makes ``import fno_math`` raise
        # ImportError (CPython contract), forcing the fallback branch even
        # in CI where the Rust wheel is built.
        monkeypatch.setitem(sys.modules, "fno_math", None)
        assert _popcount_bytes(raw) == self._reference_popcount(raw)

    def test_unique_count_estimate_works_without_fno_math(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end: the public estimator works on the fallback path."""
        monkeypatch.setitem(sys.modules, "fno_math", None)
        bf = BloomFilter(capacity=1000, fpr_target=1e-6)
        for i in range(100):
            bf.add(hashlib.sha256(f"k:{i}".encode()).digest())
        # The Swamidass-Baldi estimator is approximate; 100 unique inserts
        # must land in a sane band (not exact-equal).
        assert 50 <= bf.unique_count_estimate <= 200

    @pytest.mark.parametrize(
        "raw",
        [
            b"",
            b"\x00",
            b"\xff",
            b"\x0f\xf0",
            b"\x80\x01\x7f",
            bytes(range(256)),
        ],
    )
    def test_fallback_equals_rust_kernel_when_present(self, raw: bytes) -> None:
        """Pin fallback ≡ Rust: where the kernel IS built, the pure-Python
        popcount must equal ``fno_math.popcount_bytes`` byte-for-byte — the
        bit-exactness DEC-CLONE-RUN-001 relies on. Skipped where absent.
        """
        fno_math = pytest.importorskip("fno_math")
        rust = int(fno_math.popcount_bytes(raw))
        python = int.from_bytes(raw, "big").bit_count()
        assert rust == python == self._reference_popcount(raw)
