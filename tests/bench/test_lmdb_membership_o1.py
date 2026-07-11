"""O(1) benchmarks for the LMDB-backed dedup membership tier.

Enforces the operator's O(1)-dedup decree (MIGRATION.md #342) for the
*persistent* tier: ``__contains__`` latency must be independent of how many
keys are stored. LMDB is a B+tree (O(log n)) but a dense working set keeps
the tree a few pages deep, so the cost is dominated by the O(1) key compare.

Mirrors the historical `tests/bench/test_store_o1.py` (deleted with
`orchestrator/store.py` — MIGRATION.md #1727b): empirical ratio with CI-jitter
headroom (`< 5×`) — a scan regression would blow the ratio and fail the gate.
The 10k-vs-1k ratio routes its M4-Pro strict ceiling through
:func:`tests.bench._o1_tiers.cache_tier_ceiling` (the DEC-O1-CACHE-TIER-001
measured 3× cache allowance) per DEC-O1-CACHE-TIER-MARGINAL-001 — a 10k-key
B+tree working set crosses a CPU cache tier a 1k-key one does not (see the
in-test comment for the measured flake). Shared CI keeps the robust 5×.
"""

from __future__ import annotations

import time
from pathlib import Path

from brutex.data.lmdb_membership import LmdbMembership

from tests.bench._o1_tiers import cache_tier_ceiling

_SMALL = 1_000
_LARGE = 10_000


def _key(i: int) -> bytes:
    return i.to_bytes(32, "big")


def _filled(path: Path, n: int) -> LmdbMembership:
    m = LmdbMembership.open(path, map_size_mb=64)
    m.add_many(_key(i) for i in range(n))
    return m


class TestContainsIsO1:
    def test_contains_at_small_scale(self, benchmark, tmp_path: Path) -> None:
        m = _filled(tmp_path / "s.lmdb", _SMALL)
        probe = _key(_SMALL // 2)
        assert benchmark(lambda: probe in m) is True

    def test_contains_at_large_scale(self, benchmark, tmp_path: Path) -> None:
        m = _filled(tmp_path / "l.lmdb", _LARGE)
        probe = _key(_LARGE // 2)
        assert benchmark(lambda: probe in m) is True

    def test_contains_ratio_below_5x(self, tmp_path: Path) -> None:
        """`__contains__` latency at 10k / 1k keys must stay O(1) (< 5×)."""

        def _time(path: Path, n: int, *, iterations: int = 5_000) -> float:
            m = _filled(path, n)
            probe = _key(n // 2)
            for _ in range(50):  # warm the page cache
                _ = probe in m
            t0 = time.perf_counter_ns()
            for _ in range(iterations):
                _ = probe in m
            t1 = time.perf_counter_ns()
            m.close()
            return (t1 - t0) / iterations

        t_small = _time(tmp_path / "rs.lmdb", _SMALL)
        t_large = _time(tmp_path / "rl.lmdb", _LARGE)
        ratio = t_large / max(t_small, 1.0)
        # DEC-O1-CACHE-TIER-MARGINAL-001: routed through cache_tier_ceiling
        # (3.0× on M4-Pro strict, 5× shared CI) after a measured singleton
        # overshoot of the raw strict 1.1×: 1.1048× (t_1k=778ns → t_10k=860ns)
        # on the quiet M4-Pro runner, 2026-07-04, PR #1085 head 291605e (rerun
        # job 85094981848) — a PR touching neither this module nor
        # LmdbMembership — and PASSED on the original run (job 85094686407),
        # proving B+tree-probe memory-hierarchy noise across the 1k→10k
        # cache-tier boundary, not a scan (same physics as
        # DEC-O1-CACHE-TIER-001). A genuine O(n) scan reads 10×+, far above
        # the 3.0× tier, so the O(1) oracle keeps real teeth.
        assert ratio < cache_tier_ceiling(5.0), (
            f"LmdbMembership.__contains__ ratio {ratio:.2f}× (10k vs 1k) exceeds "
            f"5× — membership may have regressed to a scan. "
            f"t_1k={t_small:.0f}ns, t_10k={t_large:.0f}ns."
        )
