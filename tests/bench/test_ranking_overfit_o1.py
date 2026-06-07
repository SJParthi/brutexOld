"""O(1)-per-op bench for the Stage-3 overfit deflation (DEC-RANKING-OVERFIT-001).

The per-combo Deflated Sharpe (:func:`brutex.ranking.overfit.deflated_sharpe_for`)
is a CLOSED-FORM evaluation — two Normal-CDF/quantile calls — whose cost does NOT
depend on the trial count ``N`` it deflates against. So a single deflation at
``N = 1,000,000`` must cost the same as one at ``N = 1,000``: O(1) per operation.

Honest envelope (Golden Rule #10): this gates the PER-OPERATION cost only. Ranking
a whole leaderboard is inherently O(survivors) deflations + O(survivors·log) sort —
NOT O(1) total, which would be physically impossible and is never claimed. The
ceiling routes through the shared :func:`tests.bench._o1_tiers.ratio_ceiling`
(5× on shared CI, the strict 1.1× under ``BRUTEX_M4PRO_STRICT=1``).
"""

from __future__ import annotations

import time

from brutex.ranking.overfit import OverfitGateConfig, deflated_sharpe_for

from tests.bench._o1_tiers import ratio_ceiling

_CFG = OverfitGateConfig()


def _deflate_once(n_trials: int) -> float | None:
    return deflated_sharpe_for(
        2.0, 250, 0.1, 1.0, n_trials=n_trials, trial_sharpe_variance=0.5, cfg=_CFG
    )


def test_deflation_small(benchmark: object) -> None:
    assert benchmark(lambda: _deflate_once(1_000)) is not None  # type: ignore[operator]


def test_deflation_large(benchmark: object) -> None:
    assert benchmark(lambda: _deflate_once(1_000_000)) is not None  # type: ignore[operator]


def test_deflation_ratio_is_o1() -> None:
    """The deflation cost at N=1M vs N=1K must stay O(1) (< ceiling)."""

    def _time(n_trials: int, *, iterations: int = 20_000) -> float:
        for _ in range(2_000):  # warm-up
            _deflate_once(n_trials)
        start = time.perf_counter_ns()
        for _ in range(iterations):
            _deflate_once(n_trials)
        return (time.perf_counter_ns() - start) / iterations

    t_small = _time(1_000)
    t_large = _time(1_000_000)
    ratio = t_large / max(t_small, 1.0)
    assert ratio < ratio_ceiling(5.0), (
        f"deflation ratio {ratio:.2f}× exceeds ceiling — cost must be N-independent "
        f"(t_1k={t_small:.0f}ns, t_1M={t_large:.0f}ns)"
    )
