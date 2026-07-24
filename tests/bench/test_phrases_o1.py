"""O(k) bench pin for :func:`brutex.ranking.phrases.reason_for_mask` (WP3, R33).

The assembly cost must scale with the mask's FIRED-bit count k, never with
the 128-bit mask width or the 66-entry table size: the per-fired-bit latency
of a DENSE mask must stay flat vs a SPARSE 2-bit mask (ratio under the
shared-CI cache-tier ceiling). Manual ``perf_counter_ns`` loops with warm-up —
the house ratio-gate idiom (not pytest-benchmark).
"""

from __future__ import annotations

import time

from brutex.ranking.phrases import reason_for_mask
from brutex.vocabulary.bits import TOTAL_CONDITIONS

from tests.bench._o1_tiers import cache_tier_ceiling

_SPARSE_MASK = (1 << 13) | (1 << 52)  # k = 2
_DENSE_MASK = (1 << TOTAL_CONDITIONS) - 1  # k = 60
_ROUNDS = 2_000


def _per_bit_ns(mask: int, fired: int) -> float:
    for _ in range(200):  # warm-up
        reason_for_mask(mask)
    start = time.perf_counter_ns()
    for _ in range(_ROUNDS):
        reason_for_mask(mask)
    elapsed = time.perf_counter_ns() - start
    return elapsed / (_ROUNDS * fired)


def test_reason_assembly_is_o_k_in_fired_bits() -> None:
    per_bit_sparse = _per_bit_ns(_SPARSE_MASK, 2)
    per_bit_dense = _per_bit_ns(_DENSE_MASK, TOTAL_CONDITIONS)
    ratio = per_bit_dense / max(per_bit_sparse, 1.0)
    assert ratio < cache_tier_ceiling(5.0), (
        f"reason_for_mask per-fired-bit latency ratio {ratio:.2f}× (dense 60-bit vs sparse "
        f"2-bit mask) — assembly must be O(k) in fired bits; per_bit_sparse="
        f"{per_bit_sparse:.1f}ns, per_bit_dense={per_bit_dense:.1f}ns"
    )
