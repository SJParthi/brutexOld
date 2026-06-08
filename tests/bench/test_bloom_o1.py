"""O(1) benchmark for the Bloom-filter membership probe — in ISOLATION.

DEC-O1-BENCH-GAPS-001 (G1 of the 4-gap deep audit). The O(1)-dedup decree
(CLAUDE.md §2 r5 / MIGRATION.md #342) names ``combo dedup (Bloom + LMDB)`` as a
hot path. The LMDB tier already has a dedicated O(1) gate
(``test_lmdb_membership_o1.py``) and the composite index has one
(``test_option_dedup_o1.py``), but the **Bloom probe itself was only ever
exercised as part of the composite** — never benched alone. The deep audit
flagged this as a YELLOW gap ("architecturally O(1) but not directly proven").

This closes it: :meth:`BloomFilter.might_contain` wraps ``fastbloom_rs`` and
performs a FIXED ``k`` hash probes (``k`` is derived from ``(capacity, fpr)`` at
construction, NOT from the number of inserted elements — Bloom 1970). So the
probe latency must be **independent of how many elements were inserted**. A
regression that made membership scan the inserted set (or rehash per element)
would blow the ratio and fail the gate.

Mirrors ``test_lmdb_membership_o1.py``: empirical p99-style ratio with shared-CI
jitter headroom (``< 5×``), tightening to the decreed ``1.1×`` on the M4-Pro
strict tier via :func:`ratio_ceiling`.
"""

from __future__ import annotations

import time

from brutex.orchestrator.bloom import BloomFilter

from tests.bench._o1_tiers import ratio_ceiling

#: Probe latency must not grow between these two insert counts.
_SMALL = 1_000
_LARGE = 100_000
#: Capacity ≥ _LARGE so neither fill trips BloomCapacityExceeded; one fixed
#: bit-array size (≈ same ``k``) for both, so the probe cost is comparable.
_CAPACITY = 200_000


def _key(i: int) -> bytes:
    """A 32-byte content-hash stand-in (callers pre-hash to bytes)."""
    return i.to_bytes(32, "big")


def _filled(n: int) -> BloomFilter:
    bloom = BloomFilter(capacity=_CAPACITY)
    for i in range(n):
        bloom.add(_key(i))
    return bloom


class TestMightContainIsO1:
    """``might_contain`` is O(1) in the number of inserted elements."""

    def test_probe_at_small_scale(self, benchmark) -> None:
        bloom = _filled(_SMALL)
        probe = _key(_SMALL // 2)  # a present key → True
        assert benchmark(lambda: bloom.might_contain(probe)) is True

    def test_probe_at_large_scale(self, benchmark) -> None:
        bloom = _filled(_LARGE)
        probe = _key(_LARGE // 2)
        assert benchmark(lambda: bloom.might_contain(probe)) is True

    def test_probe_ratio_below_5x(self) -> None:
        """``might_contain`` latency at 100k / 1k inserts must stay O(1) (< 5×)."""

        def _time(n: int, *, iterations: int = 5_000) -> float:
            bloom = _filled(n)
            probe = _key(n // 2)
            for _ in range(50):  # warm the bit-array pages + branch predictor
                _ = bloom.might_contain(probe)
            t0 = time.perf_counter_ns()
            for _ in range(iterations):
                _ = bloom.might_contain(probe)
            t1 = time.perf_counter_ns()
            return (t1 - t0) / iterations

        t_small = _time(_SMALL)
        t_large = _time(_LARGE)
        ratio = t_large / max(t_small, 1.0)
        assert ratio < ratio_ceiling(5.0), (
            f"BloomFilter.might_contain ratio {ratio:.2f}× (100k vs 1k inserts) "
            f"exceeds 5× — the probe may have regressed from O(k) to O(n_inserted). "
            f"t_1k={t_small:.0f}ns, t_100k={t_large:.0f}ns."
        )
