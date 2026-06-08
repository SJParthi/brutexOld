"""Property invariants for the Stage-2.5 SPRT early-stop core (DEC-SPRT-EARLY-STOP-CORE-001).

Hypothesis properties over the pure, inert ``brutex.engine.early_stop`` decision:
determinism (Golden Rule #4), the ``min_trades`` floor (a KILL can never fire
below it), and monotone moment-folding. These back the ADR-0006 Layer-A test plan.
"""

from __future__ import annotations

from brutex.engine.early_stop import Decision, RunningSharpe, SprtConfig, sprt_decision
from hypothesis import given, settings
from hypothesis import strategies as st

_returns = st.lists(
    st.floats(min_value=-0.5, max_value=0.5, allow_nan=False, allow_infinity=False),
    min_size=1,
    max_size=120,
)
_threshold = st.floats(min_value=-2.0, max_value=2.0, allow_nan=False, allow_infinity=False)


def _fold(returns: list[float]) -> RunningSharpe:
    stat = RunningSharpe()
    for r in returns:
        stat.update(r)
    return stat


@given(returns=_returns, p95=_threshold)
@settings(max_examples=400)
def test_decision_is_deterministic(returns: list[float], p95: float) -> None:
    """Same trade stream + same threshold → identical decision, every time."""
    cfg = SprtConfig()
    first = sprt_decision(_fold(returns), p95_threshold=p95, cfg=cfg)
    second = sprt_decision(_fold(returns), p95_threshold=p95, cfg=cfg)
    assert first is second
    assert first in (Decision.CONTINUE, Decision.KILL)


@given(returns=_returns, p95=_threshold)
@settings(max_examples=400)
def test_kill_never_fires_below_the_min_trades_floor(returns: list[float], p95: float) -> None:
    """A KILL is impossible while ``n < min_trades`` — the spec floor holds for
    any stream (so a combo is never killed on an under-sampled track)."""
    cfg = SprtConfig()
    stat = _fold(returns)
    if stat.n < cfg.min_trades:
        assert sprt_decision(stat, p95_threshold=p95, cfg=cfg) is Decision.CONTINUE


@given(returns=_returns)
@settings(max_examples=300)
def test_running_moments_are_consistent(returns: list[float]) -> None:
    """``n`` counts every trade and the running mean equals the offline mean."""
    stat = _fold(returns)
    assert stat.n == len(returns)
    assert (
        stat.mean == sum(returns) / len(returns)
        or abs(stat.mean - sum(returns) / len(returns)) < 1e-9
    )
