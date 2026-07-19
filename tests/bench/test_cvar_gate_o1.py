"""Bench pins for the CVaR tail gate (DEC-RANK-CVAR-TAIL-GATE-001).

The honest complexity claim: the gate costs **O(n_trades) per candidate**
(oracle: one sort; kernel: one quickselect) and is **flat in the CANDIDATE
count** — the per-candidate cost at 500 candidates equals the per-candidate
cost at 50 (the O(1)-in-candidate-count pin, routed through the house
two-tier :func:`tests.bench._o1_tiers.ratio_ceiling`). The O(n_trades)
per-candidate scaling is MEASURED + PRINTED, never asserted flat (Golden
Rule #10 — claiming per-candidate O(1) in trade count would be false).

Runs on the DEFAULT (Python-oracle) arm — the arm every production sweep
serves until the ``BRUTEX_RANK_CVAR_RUST`` opt-in; the kernel arm's parity
is the property suite's job, not a wall-clock claim.
"""

from __future__ import annotations

import random
import time
from typing import Final

from brutex.ranking.cvar_gate import CvarGateConfig, cvar_rejects

from tests.bench._o1_tiers import ratio_ceiling

_SMALL: Final[int] = 50
_LARGE: Final[int] = 500
_N_TRADES: Final[int] = 120
_BEST_OF: Final[int] = 5

_CFG: Final[CvarGateConfig] = CvarGateConfig(threshold_paisa=5_000, alpha_bps=500)


def _population(n_candidates: int, n_trades: int) -> list[list[int]]:
    """Deterministic seeded candidate P&L lists (int paisa)."""
    rng = random.Random(1957)
    return [[rng.randint(-20_000, 30_000) for _ in range(n_trades)] for _ in range(n_candidates)]


def _per_candidate_seconds(candidates: list[list[int]]) -> float:
    """Best-of-N mean per-candidate gate cost (the house best-of idiom)."""
    best = float("inf")
    for _ in range(_BEST_OF):
        start = time.perf_counter()
        for pnls in candidates:
            cvar_rejects(pnls, _CFG)
        best = min(best, (time.perf_counter() - start) / len(candidates))
    return best


class TestCvarGateFlatInCandidateCount:
    def test_per_candidate_cost_flat_at_10x_candidates(self) -> None:
        small = _per_candidate_seconds(_population(_SMALL, _N_TRADES))
        large = _per_candidate_seconds(_population(_LARGE, _N_TRADES))
        ratio = large / small
        assert ratio <= ratio_ceiling(3.0), (
            f"per-candidate CVaR gate cost grew {ratio:.2f}x from {_SMALL} to "
            f"{_LARGE} candidates — the gate must be flat in candidate count"
        )

    def test_per_candidate_cost_scales_in_trade_count_measured(self) -> None:
        # HONEST: per-candidate cost is O(n_trades) — measured + printed,
        # never asserted flat. The only assertion is the sane LINEAR bound:
        # 10x the trades must not cost more than ~100x (a quadratic
        # accident would blow far past this generous ceiling).
        t_small = _per_candidate_seconds(_population(_SMALL, 100))
        t_large = _per_candidate_seconds(_population(_SMALL, 1_000))
        ratio = t_large / t_small
        print(
            f"cvar gate per-candidate cost: {t_small * 1e6:.1f} us @100 trades, "
            f"{t_large * 1e6:.1f} us @1000 trades (ratio {ratio:.2f}x — O(n log n) oracle)"
        )
        assert ratio <= 100.0
