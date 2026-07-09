"""Flat-memory bench for the Apriori candidate generator.

DEC-SWEEP-APRIORI-TUPLE-001. The generator never holds the bar window — it calls
the injected ``count_hits`` — so its allocation peak is driven by the FRONTIER
size (bounded by the deterministic mask·bar breaker), NOT by the bar count. This
bench pins that: for a FIXED cell (fixed ``condition_bits`` / ``k`` / ``min_hits``,
hence a fixed frontier), the tracemalloc PEAK of
:func:`brutex.engine.sweep_apriori.generate_apriori_candidates` must be FLAT as
the bar count grows 100× (``1_000`` → ``100_000``).

HONEST envelope (Golden Rule #10): a tracemalloc (Python-allocator) PEAK bound,
NOT an RSS/absolute-memory claim; the modeled ``max_mask_bars`` limit is an
UNVERIFIED-on-M4 placeholder. The bar list itself is allocated by the CALLER
OUTSIDE the traced region (its ``O(bars)`` cost is not the generator's), and the
injected counter returns a constant so the frontier is identical across arms.

TEETH (per the repo precedent, DEC-SWEEP-STREAMING-RANK-002): a variant whose
counter MATERIALISES an ``O(bars)`` structure per call runs through the SAME
harness and BLOWS the ceiling — so the flat-peak bound can never go vacuous.
"""

from __future__ import annotations

import gc
import tracemalloc
from collections.abc import Callable, Sequence

from brutex.engine.sweep_apriori import generate_apriori_candidates

#: A cell with a non-trivial frontier: 200 conflict-free triples over bits 6..17
#: (C(12, 3) = 220 minus the (13,14)/(15,16) conflict pairs), all frequent under
#: the constant counter.
_CONDITION_BITS = list(range(6, 18))
_K = 3
_SMALL_BARS = 1_000
_LARGE_BARS = 100_000

#: Flat-peak ceiling: a 100× bar-count growth may not even double the traced
#: peak (measured ratio ≈ 1.0 — the frontier is identical either way). The teeth
#: variant (an ``O(bars)`` counter) measures ~100× here, PINNED executably below.
_PEAK_RATIO_CEILING = 2.0

_Counter = Callable[[Sequence[int]], list[int]]


def _constant_counter(masks: Sequence[int]) -> list[int]:
    """Every mask is maximally frequent — the frontier never prunes. O(masks)."""
    return [_LARGE_BARS] * len(masks)


def _peak_bytes(n_bars: int, counter: _Counter) -> int:
    """Traced allocation peak of one generation over an ``n_bars`` bar window."""
    bar_bits = [0] * n_bars  # allocated by the caller, OUTSIDE the traced region
    gc.collect()
    tracemalloc.start()
    try:
        out = generate_apriori_candidates(
            bar_bits,
            condition_bits=_CONDITION_BITS,
            k=_K,
            min_hits=0,
            count_hits=counter,
            n_bars=n_bars,
            max_mask_bars=10**18,  # generous → the breaker never trips
        )
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert out  # the generation genuinely produced candidates
    return peak


def _o_bars_counter(bar_bits: Sequence[int]) -> _Counter:
    """A counter that leaks an ``O(bars)`` structure per call (the teeth)."""

    def counter(masks: Sequence[int]) -> list[int]:
        # Retain an O(bars) copy alive for the mask count so the peak scales
        # with the bar window — the anti-pattern the flat bound forbids.
        leaked = [list(bar_bits) for _ in masks]
        return [len(block) for block in leaked]

    return counter


class TestAprioriPeakRamIsFlatInBarCount:
    def test_peak_is_flat_from_1k_to_100k_bars(self) -> None:
        _peak_bytes(_SMALL_BARS, _constant_counter)  # warm-up (interning off the books)
        small = _peak_bytes(_SMALL_BARS, _constant_counter)
        large = _peak_bytes(_LARGE_BARS, _constant_counter)
        ratio = large / small
        assert ratio <= _PEAK_RATIO_CEILING, (
            f"generate_apriori_candidates peak RAM grew with bar count: "
            f"{small} B @ {_SMALL_BARS} bars -> {large} B @ {_LARGE_BARS} bars "
            f"(ratio {ratio:.2f} > {_PEAK_RATIO_CEILING}) — the generator is holding "
            f"the bar window (DEC-SWEEP-APRIORI-TUPLE-001 flat-memory bound regressed)"
        )

    def test_gate_has_teeth_o_bars_counter_exceeds_ceiling(self) -> None:
        # An O(bars) counter (materialising the bar window per call) BLOWS the
        # flat ceiling — proving the bound above is not vacuous.
        def peak(n: int) -> int:
            bar_bits = [0] * n
            gc.collect()
            tracemalloc.start()
            try:
                generate_apriori_candidates(
                    bar_bits,
                    condition_bits=_CONDITION_BITS,
                    k=_K,
                    min_hits=0,
                    count_hits=_o_bars_counter(bar_bits),
                    n_bars=n,
                    max_mask_bars=10**18,
                )
                return tracemalloc.get_traced_memory()[1]
            finally:
                tracemalloc.stop()

        peak(_SMALL_BARS)  # warm-up
        small = peak(_SMALL_BARS)
        large = peak(_LARGE_BARS)
        assert large / small > _PEAK_RATIO_CEILING
