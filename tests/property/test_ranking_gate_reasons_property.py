"""Property pins for :func:`brutex.ranking.gate_reasons` (DEC-SWEEP-RESULTS-DEPTH-001).

Gap-register ruling 12: the reason-returning twin may NEVER disagree with
``passes_gates`` — ``bool(passes_gates(m)) == (gate_reasons(m) == ())`` across
randomized metrics, including ``None`` and non-finite (``NaN``/±``inf``) gate
inputs. Plus the structural laws: tokens come from the stable vocabulary in
strictly increasing :data:`GATE_REASON_ORDER` order with no duplicates, the
non-finite guard is exclusive, and the function is deterministic.
"""

# pyright: reportCallIssue=false
# Rationale (the DEC-PYRIGHT-UNMASK-001 precedent, test_combo_hash_uniqueness):
# the Hypothesis `@st.composite` strategy `_metrics` is called WITHOUT the
# `draw` argument — Hypothesis injects it at runtime; pyright does not model
# the `@composite` signature transform and reports a false "Argument missing
# for parameter draw" at every `@given(m=_metrics())` site. Scoped to this
# file only; `reportCallIssue` stays a hard error everywhere else.
from __future__ import annotations

import math

from brutex.metrics.compute import ResultMetrics
from brutex.ranking import GATE_NON_FINITE_INPUT, GATE_REASON_ORDER, gate_reasons, passes_gates
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.fakes.metrics_fixtures import make_result_metrics

# Gate-relevant inputs, deliberately spanning the WHOLE hostile envelope the
# fail-closed guard exists for: None, NaN, ±inf, negatives, and values on both
# sides of every threshold.
_maybe_ratio = st.one_of(
    st.none(),
    st.floats(min_value=-10.0, max_value=10.0, allow_nan=False, allow_infinity=False),
    st.sampled_from([math.nan, math.inf, -math.inf]),
)
_pct = st.one_of(
    st.floats(min_value=-100.0, max_value=100.0, allow_nan=False, allow_infinity=False),
    st.sampled_from([math.nan, math.inf, -math.inf]),
)
_rate = st.one_of(
    st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
    st.sampled_from([math.nan, math.inf, -math.inf]),
)


@st.composite
def _metrics(draw: st.DrawFn) -> ResultMetrics:
    total = draw(st.integers(min_value=0, max_value=100))
    winning = draw(st.integers(min_value=0, max_value=total))
    return make_result_metrics(
        total_trades=total,
        winning_trades=winning,
        losing_trades=total - winning,
        profit_factor=draw(_maybe_ratio),
        max_drawdown_pct=draw(_pct),
        win_rate=draw(_rate),
        win_loss_ratio=draw(_maybe_ratio),
        max_consecutive_losses=draw(st.integers(min_value=0, max_value=50)),
    )


@given(m=_metrics())
@settings(max_examples=300, deadline=None)
def test_twin_parity_bool_equals_empty_reasons(m: ResultMetrics) -> None:
    """The ruling-12 law: pass ⇔ zero reasons, for EVERY input shape."""
    assert bool(passes_gates(m)) == (gate_reasons(m) == ())


@given(m=_metrics())
@settings(max_examples=300, deadline=None)
def test_reasons_are_ordered_unique_vocabulary_tokens(m: ResultMetrics) -> None:
    """Tokens ⊆ the stable vocabulary, in strictly increasing order, no dupes."""
    reasons = gate_reasons(m)
    assert set(reasons) <= set(GATE_REASON_ORDER)
    indices = [GATE_REASON_ORDER.index(token) for token in reasons]
    assert indices == sorted(set(indices))


@given(m=_metrics())
@settings(max_examples=300, deadline=None)
def test_non_finite_guard_is_exclusive(m: ResultMetrics) -> None:
    """When the fail-closed guard trips it is the ONLY reason (no fabricated
    door verdicts over malformed inputs — Golden Rule #1)."""
    reasons = gate_reasons(m)
    if GATE_NON_FINITE_INPUT in reasons:
        assert reasons == (GATE_NON_FINITE_INPUT,)


@given(m=_metrics())
@settings(max_examples=100, deadline=None)
def test_deterministic(m: ResultMetrics) -> None:
    assert gate_reasons(m) == gate_reasons(m)
