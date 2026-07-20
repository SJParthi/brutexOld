"""Unit tests for the columnar survivor store + bounded rank (DEC-SWEEP-STREAMING-RANK-001).

Covers :mod:`brutex.engine.sweep_stream` at 100% line+branch:

* ``SurvivorColumns`` round-trips — the u128 two-limb mask store (incl. a bit
  ≥ 64), the ``None``-bit encoding for sharpe/skew/kurt (``None`` restored
  exactly, NEVER NaN-encoded — a stored NaN stays NaN, distinct from ``None``),
  the gate bit vs :func:`brutex.ranking.passes_gates`, the 7 term columns vs
  :func:`brutex.ranking.composite_term_inputs`, and the EXACT
  ``owned_nbytes`` payload arithmetic.
* ``rank_survivor_columns`` — bit-identity against :func:`brutex.ranking.rank_combos`
  on hand-built pools (the hypothesis centrepiece lives in
  ``tests/property/test_sweep_stream_bounded_rank.py``), the overfit
  filter/abstain arms, the duplicate-mask guard (byte-identical message), the
  refusal guards, and the heap-vs-sort equivalence on score ties.
"""

from __future__ import annotations

import math
import struct
from functools import partial
from typing import Any

import pytest
from brutex.engine.sweep_stream import (
    BoundedFinalist,
    SurvivorColumns,
    rank_survivor_columns,
)
from brutex.metrics.compute import ResultMetrics
from brutex.ranking import (
    N_COMPOSITE_TERMS,
    OverfitGateConfig,
    composite_term_inputs,
    passes_gates,
    rank_combos,
)
from brutex.ranking.multi_select import (
    SelectionInputs,
    UnionSelection,
    compute_union_selection,
)

from tests.fakes.metrics_fixtures import make_result_metrics


def _columns(
    entries: list[tuple[int, ResultMetrics]],
    *,
    with_overfit: bool = False,
    max_sl_hit_rate: float | None = None,
) -> SurvivorColumns:
    columns = SurvivorColumns(with_overfit=with_overfit, max_sl_hit_rate=max_sl_hit_rate)
    for mask, metrics in entries:
        columns.append(mask, metrics)
    return columns


def _assert_bounded_equals_batch(
    entries: list[tuple[int, ResultMetrics]],
    *,
    overfit: OverfitGateConfig | None,
    top_k: int,
    max_sl_hit_rate: float | None = None,
) -> None:
    """The core oracle: bounded output == ``rank_combos(entries)[:top_k]`` exactly."""
    ranked = rank_combos(entries, overfit=overfit, max_sl_hit_rate=max_sl_hit_rate)
    result = rank_survivor_columns(
        _columns(entries, with_overfit=overfit is not None, max_sl_hit_rate=max_sl_hit_rate),
        overfit=overfit,
        top_k=top_k,
    )
    assert result.n_ranked == len(ranked)
    expected = ranked[:top_k]
    assert len(result.finalists) == len(expected)
    for bounded, batch in zip(result.finalists, expected, strict=True):
        assert bounded.mask == batch.mask
        # Exact float equality — shared internals, not a tolerance comparison.
        assert bounded.composite_score == batch.composite_score
        assert bounded.deflated_sharpe == batch.deflated_sharpe


# ────── SurvivorColumns round-trips ──────


class TestSurvivorColumnsRoundTrip:
    def test_mask_two_limb_roundtrip_incl_high_bits(self) -> None:
        masks = [0, 1, (1 << 64) - 1, 1 << 64, (1 << 100) | 5, (1 << 128) - 1]
        columns = _columns([(mask, make_result_metrics()) for mask in masks])
        assert [columns.mask_at(i) for i in range(len(masks))] == masks

    @pytest.mark.parametrize("mask", [-1, 1 << 128])
    def test_mask_out_of_range_rejected(self, mask: int) -> None:
        columns = SurvivorColumns(with_overfit=False)
        with pytest.raises(ValueError, match=r"outside \[0, 2\*\*128\)"):
            columns.append(mask, make_result_metrics())

    def test_sharpe_none_bit_roundtrip(self) -> None:
        entries = [
            (1, make_result_metrics(sharpe_ratio=None)),
            (2, make_result_metrics(sharpe_ratio=0.0)),
            (3, make_result_metrics(sharpe_ratio=-2.5)),
        ]
        columns = _columns(entries)
        assert columns.sharpe_at(0) is None
        assert columns.sharpe_at(1) == 0.0
        assert columns.sharpe_at(2) == -2.5
        assert columns.sharpe_values() == [None, 0.0, -2.5]

    def test_nan_sharpe_is_stored_as_nan_not_none(self) -> None:
        # NaN and None are semantically DIFFERENT downstream (pool dispersion
        # excludes both, but a NaN is a present value) — never conflated.
        columns = _columns([(1, make_result_metrics(sharpe_ratio=math.nan))])
        stored = columns.sharpe_at(0)
        assert stored is not None
        assert math.isnan(stored)

    def test_gate_bit_matches_passes_gates(self) -> None:
        passing = make_result_metrics()
        failing = make_result_metrics(total_trades=5)
        assert passes_gates(passing) and not passes_gates(failing)
        columns = _columns([(1, passing), (2, failing), (3, passing)])
        assert [columns.gate_passed(i) for i in range(3)] == [True, False, True]
        assert columns.gate_survivor_positions() == [0, 2]

    def test_gate_bit_honours_the_sl_hit_rate_ceiling(self) -> None:
        """Door 6 (DEC-METRICS-LOSSRATE-FIRSTCLASS-001 Stage B): the gate bit
        is recorded under the SAME ceiling the batch rank would use — an
        over-ceiling (or unmeasured ``None``) row records ``False``; the
        default knob leaves every bit byte-identical."""
        under = make_result_metrics(sl_hit_rate=0.2)
        over = make_result_metrics(sl_hit_rate=0.8)
        absent = make_result_metrics(sl_hit_rate=None)
        gated = _columns([(1, under), (2, over), (3, absent)], max_sl_hit_rate=0.5)
        assert [gated.gate_passed(i) for i in range(3)] == [True, False, False]
        assert gated.max_sl_hit_rate == 0.5
        ungated = _columns([(1, under), (2, over), (3, absent)])
        assert [ungated.gate_passed(i) for i in range(3)] == [True, True, True]
        assert ungated.max_sl_hit_rate is None

    def test_term_columns_match_composite_term_inputs(self) -> None:
        metrics = make_result_metrics(
            sharpe_ratio=1.25,
            sortino_ratio=None,
            calmar_ratio=0.5,
            profit_factor=2.0,
            recovery_factor=None,
            win_rate=0.4,
            win_loss_ratio=3.0,
            avg_mfe_paisa=300,
            avg_mae_paisa=100,
        )
        columns = _columns([(9, metrics)])
        expected = composite_term_inputs(metrics)
        stored = tuple(columns.term_column(term, [0])[0] for term in range(N_COMPOSITE_TERMS))
        assert stored == expected

    def test_overfit_columns_roundtrip(self) -> None:
        entries = [
            (1, make_result_metrics(total_trades=41, skewness=None, kurtosis=1.5)),
            (2, make_result_metrics(total_trades=52, skewness=-0.25, kurtosis=None)),
        ]
        columns = _columns(entries, with_overfit=True)
        assert columns.trades_at(0) == 41
        assert columns.skew_at(0) is None
        assert columns.kurt_at(0) == 1.5
        assert columns.trades_at(1) == 52
        assert columns.skew_at(1) == -0.25
        assert columns.kurt_at(1) is None

    def test_n_kept_and_with_overfit(self) -> None:
        columns = _columns([(1, make_result_metrics())] * 0, with_overfit=True)
        assert columns.n_kept == 0
        assert columns.with_overfit is True
        columns.append(7, make_result_metrics())
        assert columns.n_kept == 1


class TestOwnedNbytes:
    def test_exact_payload_arithmetic_without_overfit(self) -> None:
        n = 20
        columns = _columns([(i, make_result_metrics()) for i in range(n)])
        # mask limbs + sharpe + terms + the 5 LANE-W1 selection-axis columns
        # (net_pnl/win_rate/dd_pct/losing/expectancy —
        # DEC-RANK-QUOTA-PERSIST-1000-001, always retained).
        arrays = (2 + 1 + N_COMPOSITE_TERMS + 5) * 8 * n
        bit_sets = 2 * math.ceil(n / 8)  # gate + sharpe-None
        assert columns.owned_nbytes() == arrays + bit_sets

    def test_exact_payload_arithmetic_with_overfit(self) -> None:
        n = 9
        columns = _columns([(i, make_result_metrics()) for i in range(n)], with_overfit=True)
        # + trades/skew/kurt + the 5 LANE-W1 selection-axis columns.
        arrays = (2 + 1 + N_COMPOSITE_TERMS + 3 + 5) * 8 * n
        bit_sets = 4 * math.ceil(n / 8)  # + skew/kurt None bits
        assert columns.owned_nbytes() == arrays + bit_sets

    def test_monotone_in_appends(self) -> None:
        columns = SurvivorColumns(with_overfit=False)
        assert columns.owned_nbytes() == 0
        previous = 0
        for i in range(10):
            columns.append(i, make_result_metrics())
            current = columns.owned_nbytes()
            assert current > previous
            previous = current


# ────── rank_survivor_columns vs rank_combos (hand-built oracles) ──────


def _spread_metrics(seed: int, **overrides: Any) -> ResultMetrics:
    """Gate-passing metrics whose term values vary with *seed* (distinct scores)."""
    base: dict[str, Any] = {
        "sharpe_ratio": 0.5 + 0.35 * seed,
        "sortino_ratio": 1.0 + 0.1 * seed,
        "calmar_ratio": 0.2 * seed,
        "profit_factor": 1.3 + 0.05 * seed,
        "recovery_factor": 0.5 + 0.25 * seed,
        "win_rate": 0.3 + 0.04 * seed,
        "win_loss_ratio": 2.0 + 0.1 * seed,
        "avg_mfe_paisa": 100 + 40 * seed,
        "avg_mae_paisa": 80,
        "total_trades": 40 + seed,
        "skewness": 0.1 * seed,
        "kurtosis": 0.2 * seed,
    }
    base.update(overrides)
    return make_result_metrics(**base)


class TestRankSurvivorColumnsEqualsBatch:
    def test_plain_pool_every_top_k(self) -> None:
        entries = [(mask, _spread_metrics(mask)) for mask in range(1, 9)]
        for top_k in (1, 3, 8, 50):
            _assert_bounded_equals_batch(entries, overfit=None, top_k=top_k)

    def test_gate_failures_excluded_identically(self) -> None:
        entries = [
            (1, _spread_metrics(1)),
            (2, _spread_metrics(2, total_trades=3)),  # gate-fails
            (3, _spread_metrics(3, profit_factor=None)),  # gate-fails
            (4, _spread_metrics(4)),
        ]
        _assert_bounded_equals_batch(entries, overfit=None, top_k=10)

    def test_sl_hit_rate_ceiling_filters_identically(self) -> None:
        """Door 6 parity: bounded == batch under an active ceiling (over-limit
        + unmeasured rows excluded by BOTH paths, survivors identical)."""
        entries = [
            (1, _spread_metrics(1, sl_hit_rate=0.1)),
            (2, _spread_metrics(2, sl_hit_rate=0.9)),  # Door-6-fails
            (3, _spread_metrics(3, sl_hit_rate=None)),  # fail-closed
            (4, _spread_metrics(4, sl_hit_rate=0.5)),  # at-ceiling passes
        ]
        _assert_bounded_equals_batch(entries, overfit=None, top_k=10, max_sl_hit_rate=0.5)

    def test_all_gate_fail_yields_empty(self) -> None:
        entries = [(1, _spread_metrics(1, total_trades=2))]
        result = rank_survivor_columns(_columns(entries), overfit=None, top_k=5)
        assert result.finalists == ()
        assert result.n_ranked == 0

    def test_empty_columns_yield_empty(self) -> None:
        result = rank_survivor_columns(SurvivorColumns(with_overfit=False), overfit=None, top_k=5)
        assert result == type(result)(finalists=(), n_ranked=0)

    def test_zero_variance_single_survivor(self) -> None:
        _assert_bounded_equals_batch([(5, _spread_metrics(2))], overfit=None, top_k=1)

    def test_score_ties_break_by_mask_identically(self) -> None:
        # Identical metrics ⇒ identical (zero) z-scores ⇒ all-tied composites:
        # the heap and the sort must both fall back to mask ascending.
        metrics = _spread_metrics(3)
        entries = [(mask, metrics) for mask in (7, 3, 11, 5)]
        _assert_bounded_equals_batch(entries, overfit=None, top_k=2)

    def test_overfit_gate_filters_identically(self) -> None:
        overfit = OverfitGateConfig(dsr_min=0.5)
        entries = [(mask, _spread_metrics(mask)) for mask in range(1, 7)]
        _assert_bounded_equals_batch(entries, overfit=overfit, top_k=4)

    def test_overfit_abstain_pool_matches(self) -> None:
        # A single kept mask ⇒ < 2 finite Sharpes ⇒ the pool is not deflatable
        # and the gate ABSTAINS on both paths (no DSR term, no filtering).
        overfit = OverfitGateConfig(dsr_min=0.99)
        entries = [(3, _spread_metrics(4))]
        _assert_bounded_equals_batch(entries, overfit=overfit, top_k=2)
        result = rank_survivor_columns(
            _columns(entries, with_overfit=True), overfit=overfit, top_k=2
        )
        assert result.finalists[0].deflated_sharpe is None

    def test_overfit_none_sharpe_and_none_skew_kurt_match(self) -> None:
        overfit = OverfitGateConfig(dsr_min=0.0)
        entries = [
            (1, _spread_metrics(1, sharpe_ratio=None)),  # undeflatable survivor
            (2, _spread_metrics(2, skewness=None, kurtosis=None)),
            (3, _spread_metrics(5)),
            (4, _spread_metrics(3)),
        ]
        _assert_bounded_equals_batch(entries, overfit=overfit, top_k=4)


class TestRankSurvivorColumnsGuards:
    def test_top_k_below_one_rejected(self) -> None:
        with pytest.raises(ValueError, match="top_k must be >= 1"):
            rank_survivor_columns(SurvivorColumns(with_overfit=False), overfit=None, top_k=0)

    def test_overfit_without_columns_rejected(self) -> None:
        with pytest.raises(ValueError, match="with_overfit=False"):
            rank_survivor_columns(
                SurvivorColumns(with_overfit=False),
                overfit=OverfitGateConfig(),
                top_k=1,
            )

    def test_duplicate_mask_raises_the_shared_byte_identical_message(self) -> None:
        entries = [(5, _spread_metrics(1)), (5, _spread_metrics(2)), (7, _spread_metrics(3))]
        with pytest.raises(ValueError) as bounded_exc:
            rank_survivor_columns(_columns(entries), overfit=None, top_k=3)
        with pytest.raises(ValueError) as batch_exc:
            rank_combos(entries)
        assert str(bounded_exc.value) == str(batch_exc.value)
        assert "duplicate survivor mask(s): [5]" in str(bounded_exc.value)


class TestDeterministicUlpTripwire:
    """Deterministic bit-level drift tripwire (hostile-review teeth follow-up).

    The hypothesis centrepiece samples the input space, so a genuine ULP-level
    summation-reorder drift in the bounded arm can survive one bounded-example
    run by luck. This fixture is DENSE in exact score ties (large clusters of
    IDENTICAL metrics under distinct masks) and mixes magnitudes across ~14
    orders — the shapes where a reassociated float sum diverges first — and
    asserts the composite (and DSR) of EVERY finalist is BIT-equal between the
    arms via ``struct.pack('<d', …)``, on every CI run, no sampling.
    """

    @staticmethod
    def _double_bits(value: float) -> str:
        return struct.pack("<d", value).hex()

    def _dense_tie_pool(self) -> list[tuple[int, ResultMetrics]]:
        # Three tie CLUSTERS (shared metrics objects → exactly equal term
        # columns within a cluster) at wildly mixed magnitudes, plus unique
        # rows spanning tiny/huge values and a gate-failing row.
        tiny = make_result_metrics(
            sharpe_ratio=1e-12,
            sortino_ratio=2e-12,
            calmar_ratio=-3e-12,
            profit_factor=1.2000000000001,
            recovery_factor=5e-13,
            win_rate=0.26,
            win_loss_ratio=2.0000000000002,
            avg_mfe_paisa=1,
            avg_mae_paisa=10**9,
            total_trades=31,
            skewness=1e-9,
            kurtosis=-1e-9,
        )
        huge = make_result_metrics(
            sharpe_ratio=1.0e12,
            sortino_ratio=-2.0e12,
            calmar_ratio=3.0e11,
            profit_factor=9.5,
            recovery_factor=7.0e12,
            win_rate=0.99,
            win_loss_ratio=8.75,
            avg_mfe_paisa=10**12,
            avg_mae_paisa=1,
            total_trades=77,
            skewness=3.5,
            kurtosis=11.25,
        )
        mid = make_result_metrics(
            sharpe_ratio=0.1 + 0.2,  # a classic non-representable sum (0.30000000000000004)
            sortino_ratio=1.0 / 3.0,
            calmar_ratio=2.0 / 3.0,
            profit_factor=1.0 + 1.0 / 3.0,
            recovery_factor=1e-3,
            win_rate=0.6,
            win_loss_ratio=1.5,
            avg_mfe_paisa=333,
            avg_mae_paisa=97,
            total_trades=45,
            skewness=-0.75,
            kurtosis=0.5,
        )
        gate_fail = make_result_metrics(total_trades=3)
        pool: list[tuple[int, ResultMetrics]] = []
        mask = 1
        for cluster, size in ((tiny, 6), (huge, 5), (mid, 7)):
            for _ in range(size):
                pool.append((mask, cluster))
                mask += 3  # non-contiguous masks so ties break on value, not order
        pool.append((mask, gate_fail))
        return pool

    @pytest.mark.parametrize(
        "overfit", [None, OverfitGateConfig(dsr_min=0.0)], ids=["plain", "overfit"]
    )
    def test_dense_tie_mixed_magnitude_composites_are_bit_equal(
        self, overfit: OverfitGateConfig | None
    ) -> None:
        pool = self._dense_tie_pool()
        for top_k in (1, 4, len(pool)):
            ranked = rank_combos(pool, overfit=overfit)
            result = rank_survivor_columns(
                _columns(pool, with_overfit=overfit is not None), overfit=overfit, top_k=top_k
            )
            expected = ranked[:top_k]
            assert [f.mask for f in result.finalists] == [rc.mask for rc in expected]
            for bounded, batch in zip(result.finalists, expected, strict=True):
                # BIT equality — an ULP-level reassociation drift changes the
                # low mantissa bits and fails here deterministically.
                assert self._double_bits(bounded.composite_score) == self._double_bits(
                    batch.composite_score
                )
                if batch.deflated_sharpe is None:
                    assert bounded.deflated_sharpe is None
                else:
                    assert bounded.deflated_sharpe is not None
                    assert self._double_bits(bounded.deflated_sharpe) == self._double_bits(
                        batch.deflated_sharpe
                    )
            # The fixture must genuinely exercise dense ties: every cluster
            # ranks (only the gate_fail row drops), so n_ranked == 18.
            assert result.n_ranked == 18


class TestBoundedFinalistShape:
    def test_frozen_value_object(self) -> None:
        finalist = BoundedFinalist(mask=3, composite_score=0.5, deflated_sharpe=None)
        with pytest.raises(AttributeError):
            finalist.mask = 4  # type: ignore[misc]

    def test_selection_fields_default_none(self) -> None:
        # The LANE-W1 provenance fields are ADDITIVE + defaulted — a legacy
        # construction never stamps them (the back-compat law).
        finalist = BoundedFinalist(mask=3, composite_score=0.5, deflated_sharpe=None)
        assert finalist.selection_basis is None
        assert finalist.pareto_member is None


# ────── LANE-W1: selection-axis columns + the union arm ──────
# (DEC-RANK-PARETO-PERSIST-001 / DEC-RANK-QUOTA-PERSIST-1000-001)


class TestSelectionAxisColumns:
    def test_axis_accessors_roundtrip(self) -> None:
        entries = [
            (
                1,
                make_result_metrics(
                    net_pnl_paisa=123_456,
                    win_rate=0.25,
                    max_drawdown_pct=7.5,
                    losing_trades=4,
                    expectancy_paisa=-50,
                ),
            ),
            (
                2,
                make_result_metrics(
                    net_pnl_paisa=-9_900,
                    win_rate=0.75,
                    max_drawdown_pct=0.0,
                    losing_trades=0,
                    expectancy_paisa=12_345,
                ),
            ),
        ]
        columns = _columns(entries)
        assert [columns.net_pnl_at(i) for i in range(2)] == [123_456, -9_900]
        assert [columns.win_rate_at(i) for i in range(2)] == [0.25, 0.75]
        assert [columns.dd_pct_at(i) for i in range(2)] == [7.5, 0.0]
        assert [columns.losing_at(i) for i in range(2)] == [4, 0]
        assert [columns.expectancy_at(i) for i in range(2)] == [-50, 12_345]

    def test_axis_columns_retained_without_overfit_flag(self) -> None:
        # The axis columns are ALWAYS retained — independent of with_overfit.
        for with_overfit in (False, True):
            columns = _columns(
                [(1, make_result_metrics(net_pnl_paisa=777))], with_overfit=with_overfit
            )
            assert columns.net_pnl_at(0) == 777


def _axis_metrics(seed: int, **overrides: Any) -> ResultMetrics:
    """Gate-passing metrics with distinct composite AND selection-axis values."""
    axes: dict[str, Any] = {
        "net_pnl_paisa": 10_000 * seed - 25_000,
        "max_drawdown_pct": 2.0 + 1.5 * ((seed * 3) % 5),
        "losing_trades": 3 + (seed * 7) % 11,
        "expectancy_paisa": 500 * ((seed * 5) % 9) - 1_000,
    }
    axes.update(overrides)
    return _spread_metrics(seed, **axes)


def _batch_union(
    entries: list[tuple[int, ResultMetrics]],
    *,
    overfit: OverfitGateConfig | None,
    top_k: int,
    quota_m: int,
    frontier_cap: int,
) -> tuple[list[Any], UnionSelection]:
    """The BATCH arm's union: ``rank_combos`` → ``SelectionInputs`` → composer."""
    ranked = rank_combos(entries, overfit=overfit)
    inputs = SelectionInputs(
        masks=tuple(rc.mask for rc in ranked),
        composites=tuple(rc.composite_score for rc in ranked),
        net_pnl_paisa=tuple(rc.metrics.net_pnl_paisa for rc in ranked),
        win_rate=tuple(rc.metrics.win_rate for rc in ranked),
        max_dd_pct=tuple(rc.metrics.max_drawdown_pct for rc in ranked),
        losing_trades=tuple(rc.metrics.losing_trades for rc in ranked),
        expectancy_paisa=tuple(rc.metrics.expectancy_paisa for rc in ranked),
    )
    selection = compute_union_selection(
        inputs, top_k=top_k, quota_m=quota_m, frontier_cap=frontier_cap
    )
    return list(ranked), selection


class TestRankSurvivorColumnsUnion:
    def _bounded_union(
        self,
        entries: list[tuple[int, ResultMetrics]],
        *,
        overfit: OverfitGateConfig | None = None,
        top_k: int,
        quota_m: int,
        frontier_cap: int,
    ) -> Any:
        return rank_survivor_columns(
            _columns(entries, with_overfit=overfit is not None),
            overfit=overfit,
            top_k=top_k,
            union_selection_fn=partial(
                compute_union_selection, quota_m=quota_m, frontier_cap=frontier_cap
            ),
        )

    def test_union_matches_batch_composer_exactly(self) -> None:
        entries = [(mask, _axis_metrics(mask)) for mask in range(1, 11)]
        ranked, selection = _batch_union(entries, overfit=None, top_k=3, quota_m=2, frontier_cap=2)
        result = self._bounded_union(entries, top_k=3, quota_m=2, frontier_cap=2)
        assert [f.mask for f in result.finalists] == [
            ranked[index].mask for index in selection.order
        ]
        for finalist, index in zip(result.finalists, selection.order, strict=True):
            assert finalist.composite_score == ranked[index].composite_score
            assert finalist.selection_basis == selection.basis_by_index[index]
            assert finalist.pareto_member == (index in selection.frontier)
        assert result.truncated == selection.truncated
        assert result.frontier_dropped == selection.frontier_dropped
        assert result.n_ranked == len(ranked)

    def test_union_prefix_is_the_legacy_top_k(self) -> None:
        entries = [(mask, _axis_metrics(mask)) for mask in range(1, 9)]
        top_k = 3
        legacy = rank_survivor_columns(_columns(entries), overfit=None, top_k=top_k)
        union = self._bounded_union(entries, top_k=top_k, quota_m=2, frontier_cap=4)
        prefix = union.finalists[:top_k]
        assert [f.mask for f in prefix] == [f.mask for f in legacy.finalists]
        for union_finalist, legacy_finalist in zip(prefix, legacy.finalists, strict=True):
            assert union_finalist.composite_score == legacy_finalist.composite_score
            assert union_finalist.deflated_sharpe == legacy_finalist.deflated_sharpe
            assert union_finalist.selection_basis is not None
            assert "composite" in union_finalist.selection_basis.split(",")

    def test_union_extras_are_tagged_and_composite_ordered(self) -> None:
        entries = [(mask, _axis_metrics(mask)) for mask in range(1, 9)]
        union = self._bounded_union(entries, top_k=2, quota_m=2, frontier_cap=3)
        extras = union.finalists[2:]
        assert extras  # the tiny top_k forces union-only extras
        for finalist in extras:
            assert finalist.selection_basis is not None
            tokens = set(finalist.selection_basis.split(","))
            assert tokens & {
                "composite",
                "dd",
                "losing_trades",
                "win_rate",
                "expectancy",
                "frontier",
            }
        # Extras arrive in (-composite, mask) order — descending composite.
        composites = [f.composite_score for f in extras]
        assert composites == sorted(composites, reverse=True)

    def test_union_with_overfit_carries_dsr(self) -> None:
        overfit = OverfitGateConfig(dsr_min=0.0)
        entries = [(mask, _axis_metrics(mask)) for mask in range(1, 7)]
        ranked, selection = _batch_union(
            entries, overfit=overfit, top_k=2, quota_m=1, frontier_cap=2
        )
        result = self._bounded_union(entries, overfit=overfit, top_k=2, quota_m=1, frontier_cap=2)
        assert [f.mask for f in result.finalists] == [
            ranked[index].mask for index in selection.order
        ]
        for finalist, index in zip(result.finalists, selection.order, strict=True):
            assert finalist.deflated_sharpe == ranked[index].deflated_sharpe

    def test_union_truncated_and_frontier_dropped_propagate(self) -> None:
        entries = [(mask, _axis_metrics(mask)) for mask in range(1, 11)]
        result = self._bounded_union(entries, top_k=1, quota_m=1, frontier_cap=1)
        # top_k=1 + quota 1 + cap 1 cannot select all 10 survivors.
        assert result.truncated > 0
        assert result.truncated == result.n_ranked - len(result.finalists)
        # The frontier over these spread axes exceeds cap 1 ⇒ drops counted.
        assert result.frontier_dropped > 0
        assert sum(1 for f in result.finalists if f.pareto_member) <= 1

    def test_union_fn_not_called_when_no_survivors(self) -> None:
        def bomb(*_args: Any, **_kwargs: Any) -> UnionSelection:
            raise AssertionError("union_selection_fn must not run on an empty pool")

        entries = [(1, _spread_metrics(1, total_trades=2))]  # gate-fails
        result = rank_survivor_columns(
            _columns(entries), overfit=None, top_k=5, union_selection_fn=bomb
        )
        assert result.finalists == ()
        assert result.n_ranked == 0
        assert result.truncated == 0
        assert result.frontier_dropped == 0

    def test_legacy_path_never_stamps_selection_fields(self) -> None:
        entries = [(mask, _axis_metrics(mask)) for mask in range(1, 5)]
        legacy = rank_survivor_columns(_columns(entries), overfit=None, top_k=4)
        for finalist in legacy.finalists:
            assert finalist.selection_basis is None
            assert finalist.pareto_member is None
        assert legacy.truncated == 0
        assert legacy.frontier_dropped == 0
