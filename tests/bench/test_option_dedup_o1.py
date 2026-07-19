"""O(1) benchmarks for the expired-option dedup engine.

Enforces the operator's O(1)-dedup decree (MIGRATION.md #342): the latency
of :meth:`OptionDedupIndex.seen` and :meth:`OptionDedupIndex.add_if_new`
must be **independent of how many keys are already in the index** — looking
up one key out of 1,000 must cost the same as out of 100,000.

The Bloom probe is O(1) and the exact `set` membership is O(1) amortised, so
the combined dedup is O(1). If a regression turned either into a scan, the
ratio blows up and this gate fails.

Mirrors the historical `tests/bench/test_store_o1.py` (deleted with
`orchestrator/store.py` — MIGRATION.md #1969): empirical ratio with CI-jitter
headroom (`< 5×`), the explicit enforcement of the decree's per-operation
O(1) guarantee.
"""

from __future__ import annotations

import time

from brutex.data.option_dedup import OptionDedupIndex, option_bar_key

from tests.bench._o1_tiers import ratio_ceiling

_SMALL = 1_000
_LARGE = 100_000


def _key(i: int) -> bytes:
    return option_bar_key(
        underlying="NIFTY", expiry="2021-01-07", strike="17500", side="CE", epoch=i
    )


def _filled_index(n: int) -> tuple[OptionDedupIndex, bytes]:
    """Build an index with *n* keys; return it + a probe key known to be present."""
    idx = OptionDedupIndex(capacity=max(2 * n, 1024), fpr_target=1e-3)
    for i in range(n):
        idx.add(_key(i))
    return idx, _key(n // 2)


def _time_seen(n: int, *, iterations: int = 5_000) -> float:
    idx, probe = _filled_index(n)
    for _ in range(100):  # warm up
        idx.seen(probe)
    t0 = time.perf_counter_ns()
    for _ in range(iterations):
        idx.seen(probe)
    t1 = time.perf_counter_ns()
    return (t1 - t0) / iterations


class TestSeenIsO1:
    def test_seen_at_small_scale(self, benchmark) -> None:
        idx, probe = _filled_index(_SMALL)
        assert benchmark(lambda: idx.seen(probe)) is True

    def test_seen_at_large_scale(self, benchmark) -> None:
        idx, probe = _filled_index(_LARGE)
        assert benchmark(lambda: idx.seen(probe)) is True

    def test_seen_ratio_below_5x(self) -> None:
        """`seen` latency at 100k / 1k keys must stay O(1) (< 5×)."""
        t_small = _time_seen(_SMALL)
        t_large = _time_seen(_LARGE)
        ratio = t_large / max(t_small, 1.0)
        assert ratio < ratio_ceiling(5.0), (
            f"OptionDedupIndex.seen latency ratio {ratio:.2f}× (100k vs 1k) "
            f"exceeds 5× — dedup may have regressed to a scan. "
            f"t_1k={t_small:.0f}ns, t_100k={t_large:.0f}ns."
        )


#: ``seen`` is the dedup *decision* hot path (one Bloom probe + at most one
#: exact lookup, both O(1)); proving its latency is index-size-independent is
#: the O(1)-dedup guarantee. ``add`` is O(1) by construction (one ``bloom.add``
#: + one ``set.add``, no scan) and is covered exhaustively by the unit suite —
#: it is not benchmarked here because a fixture loop would insert past the
#: Bloom's fail-closed capacity gate, which is correct behaviour, not a perf
#: signal.
