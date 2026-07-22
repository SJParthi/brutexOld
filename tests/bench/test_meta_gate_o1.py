"""O(1)-per-element bench teeth for the meta gate kernels
(DEC-META-LABEL-ANNOTATE-001 + DEC-ABSTAIN-GATE-STREAM-001).

HONEST envelope (Golden Rule #10): a batch is O(n) TOTAL — the O(1) claim
is strictly PER TRADE (scorer) / PER ELEMENT (abstain predicate). The
gates assert per-element cost FLATNESS across a 10× batch-size decade
(``cache_tier_ceiling`` — the big arm's working set crosses a cache tier,
the combo_mask precedent), on the Python ORACLE always and on the compiled
``brutex_meta`` kernel when the wheel is present. Wall numbers are
measured best-of-N mins; nothing is claimed as an SLA.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import pytest
from brutex.meta.abstain import abstain_masks_python
from brutex.meta.features import N_FEATURES
from brutex.meta.logistic import score_logistic

from tests.bench._o1_tiers import cache_tier_ceiling

N_SMALL = 1_000
N_BIG = 10_000

#: Best-of-N repetitions per timed arm (min absorbs scheduler noise).
_REPS = 5

WEIGHTS = [((i * 37) % 13 - 6) / 10.0 for i in range(N_FEATURES)]


def _features(n: int) -> list[float]:
    return [((i * 31) % 97 - 48) / 16.0 for i in range(n * N_FEATURES)]


def _abstain_args(
    n: int,
) -> tuple[list[int], int, list[float], float, float, list[int], bool, list[float], float]:
    regimes = [i % 4 for i in range(n)]
    vix = [10.0 + (i % 25) for i in range(n)]
    flags = [1 if i % 11 == 0 else 0 for i in range(n)]
    conf = [((i * 13) % 100) / 100.0 for i in range(n)]
    return regimes, 0b0111, vix, 11.0, 30.0, flags, True, conf, 0.7


def _best_of(fn: Callable[[], object]) -> float:
    best = float("inf")
    for _ in range(_REPS):
        t0 = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t0)
    return best


class TestScorerPerTradeFlat:
    def _per_trade_ratio(
        self, scorer: Callable[[list[float], int, list[float], float], object]
    ) -> float:
        small = _features(N_SMALL)
        big = _features(N_BIG)
        scorer(small, N_FEATURES, WEIGHTS, 0.1)  # warm-up
        t_small = _best_of(lambda: scorer(small, N_FEATURES, WEIGHTS, 0.1)) / N_SMALL
        t_big = _best_of(lambda: scorer(big, N_FEATURES, WEIGHTS, 0.1)) / N_BIG
        return t_big / t_small

    def test_python_oracle_per_trade_cost_is_flat(self) -> None:
        ratio = self._per_trade_ratio(score_logistic)
        assert ratio < cache_tier_ceiling(5.0), (
            f"oracle per-trade cost ratio {ratio:.2f} at {N_BIG}x{N_FEATURES} — "
            "the scorer is no longer O(1)/trade"
        )

    def test_rust_kernel_per_trade_cost_is_flat(self) -> None:
        brutex_meta = pytest.importorskip("brutex_meta", reason="wheel not built")
        ratio = self._per_trade_ratio(brutex_meta.score_logistic)
        assert ratio < cache_tier_ceiling(5.0), (
            f"kernel per-trade cost ratio {ratio:.2f} at {N_BIG}x{N_FEATURES} — "
            "the scorer is no longer O(1)/trade"
        )


class TestAbstainPerElementFlat:
    def _per_element_ratio(self, masks: Callable[..., object]) -> float:
        small = _abstain_args(N_SMALL)
        big = _abstain_args(N_BIG)
        masks(*small)  # warm-up
        t_small = _best_of(lambda: masks(*small)) / N_SMALL
        t_big = _best_of(lambda: masks(*big)) / N_BIG
        return t_big / t_small

    def test_python_oracle_per_element_cost_is_flat(self) -> None:
        ratio = self._per_element_ratio(abstain_masks_python)
        assert ratio < cache_tier_ceiling(5.0), (
            f"oracle per-element cost ratio {ratio:.2f} at {N_BIG} elements — "
            "the abstain predicate is no longer O(1)/element"
        )

    def test_rust_kernel_per_element_cost_is_flat(self) -> None:
        brutex_meta = pytest.importorskip("brutex_meta", reason="wheel not built")
        ratio = self._per_element_ratio(brutex_meta.abstain_masks)
        assert ratio < cache_tier_ceiling(5.0), (
            f"kernel per-element cost ratio {ratio:.2f} at {N_BIG} elements — "
            "the abstain predicate is no longer O(1)/element"
        )
