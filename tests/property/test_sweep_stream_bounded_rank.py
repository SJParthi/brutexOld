"""Property centrepiece: bounded columnar rank ≡ batch rank, bit-for-bit.

DEC-SWEEP-STREAMING-RANK-001 hard invariant (a): for ANY candidate pool and ANY
``top_k``, ``rank_survivor_columns(SurvivorColumns(...))`` must equal
``rank_combos(pairs)[:top_k]`` EXACTLY — finalist masks in the same order,
composite scores equal under ``==`` (no tolerance: the two paths share the
scoring internals, they do not approximate each other), Deflated Sharpes equal,
and the pre-truncation leaderboard size (``n_ranked``) equal.

The hypothesis pools deliberately cover the adversarial shapes the spec names:
``None`` patterns on every optional field, NaN/±inf metric values (the
fail-closed gate arm), EXACT score ties (identical metrics under distinct
masks — the ``(-composite, mask)`` tie-break), zero-variance z columns, single
survivors, gate-rejecting candidates, and overfit ON / OFF / ABSTAIN
(undeflatable pool) arms.
"""

from __future__ import annotations

from typing import Any

from brutex.engine.sweep_stream import SurvivorColumns, rank_survivor_columns
from brutex.metrics.compute import ResultMetrics
from brutex.ranking import OverfitGateConfig, rank_combos
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.fakes.metrics_fixtures import make_result_metrics

#: Small value pools generate frequent EXACT collisions (ties + zero-variance
#: columns) while the floats/NaN/inf arms cover the numeric edges.
_METRIC_VALUES = st.one_of(
    st.none(),
    st.sampled_from([0.0, 1.0, -1.0, 2.5, float("nan"), float("inf"), float("-inf")]),
    st.floats(min_value=-50, max_value=50, allow_nan=False),
)


@st.composite
def _metrics(draw: st.DrawFn) -> ResultMetrics:
    overrides: dict[str, Any] = {
        "sharpe_ratio": draw(_METRIC_VALUES),
        "sortino_ratio": draw(_METRIC_VALUES),
        "calmar_ratio": draw(_METRIC_VALUES),
        "profit_factor": draw(st.one_of(st.none(), st.floats(0.0, 10.0))),
        "recovery_factor": draw(_METRIC_VALUES),
        "win_loss_ratio": draw(st.one_of(st.none(), st.floats(0.0, 10.0))),
        "win_rate": draw(st.floats(0.0, 1.0)),
        "max_drawdown_pct": draw(st.sampled_from([5.0, 29.9, 31.0, float("nan")])),
        "max_consecutive_losses": draw(st.integers(0, 12)),
        "total_trades": draw(st.integers(0, 80)),
        "avg_mfe_paisa": draw(st.integers(0, 500)),
        "avg_mae_paisa": draw(st.integers(0, 500)),
        "skewness": draw(_METRIC_VALUES),
        "kurtosis": draw(_METRIC_VALUES),
    }
    return make_result_metrics(**overrides)


@st.composite
def _candidate_pool(draw: st.DrawFn) -> list[tuple[int, ResultMetrics]]:
    n = draw(st.integers(0, 12))
    masks = draw(
        st.lists(
            st.integers(min_value=0, max_value=(1 << 128) - 1),
            min_size=n,
            max_size=n,
            unique=True,
        )
    )
    metrics_pool = draw(st.lists(_metrics(), min_size=max(1, n), max_size=max(1, n)))
    # Reuse drawn metrics across masks sometimes — IDENTICAL metrics under
    # distinct masks force exact composite ties (the tie-break teeth).
    picks = draw(st.lists(st.integers(0, len(metrics_pool) - 1), min_size=n, max_size=n))
    return [(mask, metrics_pool[pick]) for mask, pick in zip(masks, picks, strict=True)]


_OVERFIT = st.one_of(
    st.none(),
    st.builds(
        OverfitGateConfig,
        dsr_min=st.sampled_from([0.0, 0.3, 0.95]),
        weight=st.sampled_from([0.05, 0.2]),
    ),
)


@settings(max_examples=200, deadline=None)
@given(pool=_candidate_pool(), overfit=_OVERFIT, top_k=st.integers(1, 15))
def test_bounded_rank_is_bit_identical_to_batch_rank(
    pool: list[tuple[int, ResultMetrics]],
    overfit: OverfitGateConfig | None,
    top_k: int,
) -> None:
    ranked = rank_combos(pool, overfit=overfit)

    columns = SurvivorColumns(with_overfit=overfit is not None)
    for mask, metrics in pool:
        columns.append(mask, metrics)
    result = rank_survivor_columns(columns, overfit=overfit, top_k=top_k)

    expected = ranked[:top_k]
    assert result.n_ranked == len(ranked)
    assert [finalist.mask for finalist in result.finalists] == [rc.mask for rc in expected]
    for finalist, rc in zip(result.finalists, expected, strict=True):
        # EXACT equality — shared internals, never a tolerance (Golden Rule #4).
        assert finalist.composite_score == rc.composite_score
        assert finalist.deflated_sharpe == rc.deflated_sharpe


@settings(max_examples=60, deadline=None)
@given(pool=_candidate_pool())
def test_columns_roundtrip_preserves_every_ranking_input(
    pool: list[tuple[int, ResultMetrics]],
) -> None:
    """The columnar store is lossless for the fields ranking consumes."""
    from brutex.ranking import composite_term_inputs, passes_gates

    columns = SurvivorColumns(with_overfit=True)
    for mask, metrics in pool:
        columns.append(mask, metrics)
    assert columns.n_kept == len(pool)
    for index, (mask, metrics) in enumerate(pool):
        assert columns.mask_at(index) == mask
        assert columns.gate_passed(index) == passes_gates(metrics)
        stored_sharpe = columns.sharpe_at(index)
        if metrics.sharpe_ratio is None:
            assert stored_sharpe is None
        else:
            # NaN-safe exact bit comparison via repr (NaN != NaN under ==).
            assert repr(stored_sharpe) == repr(metrics.sharpe_ratio)
        expected_terms = composite_term_inputs(metrics)
        for term, expected in enumerate(expected_terms):
            (stored,) = columns.term_column(term, [index])
            assert repr(stored) == repr(expected)
        assert columns.trades_at(index) == metrics.total_trades
        for stored_opt, source in (
            (columns.skew_at(index), metrics.skewness),
            (columns.kurt_at(index), metrics.kurtosis),
        ):
            if source is None:
                assert stored_opt is None
            else:
                assert repr(stored_opt) == repr(source)
