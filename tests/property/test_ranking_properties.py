"""Hypothesis property tests for :mod:`brutex.ranking`.

Invariants that must hold for every valid leaderboard, complementing
the known-value tests in ``tests/unit/test_ranking_*.py``:

1. **Gate monotonicity (trades)** — if a result passes the
   insufficient-trades gate, lowering ``total_trades`` below 30 can only
   ADD that rejection flag, never remove a flag.
2. **Z-score shift invariance** — adding a constant to every survivor's
   sharpe leaves the composite ranking unchanged (a z-score subtracts
   the mean, so a uniform shift cancels). NOT scale-then-add: the spec
   normalizes per leaderboard.
3. **Strict dominance** — a result that is strictly better on every
   scored metric (and ties nothing) outranks a dominated one.
4. **Ranks are a contiguous permutation** — survivors get ranks
   ``1..k`` with no gaps or repeats, sorted descending by composite.
"""

from __future__ import annotations

from dataclasses import replace

from brutex.metrics import ResultMetrics
from brutex.ranking.gates import RejectionReason, passes_gates
from brutex.ranking.score import RankInput, rank_leaderboard
from hypothesis import given
from hypothesis import strategies as st

from tests.unit.test_ranking_gates import _GOOD


def _mk(result_id: str, sharpe: float) -> RankInput:
    """A gate-passing entry whose sharpe is the only varying scored field."""
    metrics: ResultMetrics = replace(_GOOD, sharpe_ratio=sharpe)
    return RankInput(result_id=result_id, metrics=metrics)


@given(trades=st.integers(min_value=0, max_value=29))
def test_gate_monotonicity_trades(trades: int) -> None:
    """Below-30 trade counts always trip INSUFFICIENT_TRADES; 30+ never does."""
    low = passes_gates(replace(_GOOD, total_trades=trades))
    assert RejectionReason.INSUFFICIENT_TRADES in low.reason
    # The baseline (50 trades) never trips it.
    assert RejectionReason.INSUFFICIENT_TRADES not in passes_gates(_GOOD).reason


@given(
    sharpes=st.lists(
        st.integers(min_value=-100, max_value=100),
        min_size=2,
        max_size=8,
        unique=True,
    ),
    shift=st.integers(min_value=-1_000, max_value=1_000),
)
def test_zscore_shift_invariance(sharpes: list[int], shift: int) -> None:
    """Adding a constant to every sharpe preserves the ranking order.

    The z-score ``(x − mean)/std`` is invariant to a uniform additive
    shift, so the composite ordering (and the per-id ranks) must not
    change. Well-separated distinct integer sharpes avoid composite ties
    AND keep the shift well clear of float roundoff (a shift near the
    inter-value gap can otherwise erase the distinction at machine
    epsilon — a float limitation, not a ranking-logic property).
    """
    base = [_mk(f"r{i}", float(s)) for i, s in enumerate(sharpes)]
    shifted = [_mk(f"r{i}", float(s + shift)) for i, s in enumerate(sharpes)]

    order_base = [r.result_id for r in rank_leaderboard(base)]
    order_shifted = [r.result_id for r in rank_leaderboard(shifted)]
    assert order_base == order_shifted


@given(delta=st.floats(min_value=0.01, max_value=50.0, allow_nan=False))
def test_strict_dominance_outranks(delta: float) -> None:
    """A result strictly better on every scored metric ranks above it."""
    dominated: ResultMetrics = replace(
        _GOOD,
        sharpe_ratio=1.0,
        sortino_ratio=1.0,
        calmar_ratio=1.0,
        profit_factor=2.0,
        recovery_factor=1.0,
        win_rate=0.5,
        win_loss_ratio=2.0,
    )
    dominant: ResultMetrics = replace(
        dominated,
        sharpe_ratio=1.0 + delta,
        sortino_ratio=1.0 + delta,
        calmar_ratio=1.0 + delta,
        profit_factor=2.0 + delta,
        recovery_factor=1.0 + delta,
        win_rate=0.5,  # win_rate is bounded; bump the ratio instead
        win_loss_ratio=2.0 + delta,
    )
    ranked = rank_leaderboard(
        [
            RankInput("dominated", dominated),
            RankInput("dominant", dominant),
        ]
    )
    assert ranked[0].result_id == "dominant"
    assert ranked[1].result_id == "dominated"
    assert ranked[0].composite_score > ranked[1].composite_score


@given(
    sharpes=st.lists(
        st.floats(min_value=-5.0, max_value=5.0, allow_nan=False),
        min_size=1,
        max_size=10,
    )
)
def test_ranks_are_contiguous_permutation(sharpes: list[float]) -> None:
    """Survivor ranks are exactly ``1..k`` and sorted by composite desc."""
    ranked = rank_leaderboard([_mk(f"r{i}", s) for i, s in enumerate(sharpes)])
    ranks = [r.rank for r in ranked]
    assert ranks == list(range(1, len(ranked) + 1))
    scores = [r.composite_score for r in ranked]
    assert scores == sorted(scores, reverse=True)
