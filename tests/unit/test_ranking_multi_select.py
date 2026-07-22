"""Unit tests for :mod:`brutex.ranking.multi_select` (LANE W1 oracle).

DEC-RANK-PARETO-PERSIST-001 / DEC-RANK-QUOTA-PERSIST-1000-001: the ε-Pareto
frontier + per-axis quota Python oracles and the pure union/tag composer.
100% line+branch (the ranking package gate, ci.yml).
"""

from __future__ import annotations

import pytest
import structlog
from brutex.ranking.multi_select import (
    AXIS_QUOTA_M,
    BASIS_COMPOSITE,
    BASIS_DD,
    BASIS_EXPECTANCY,
    BASIS_FRONTIER,
    BASIS_LOSING_TRADES,
    BASIS_ORDER,
    BASIS_WIN_RATE,
    DEFAULT_PERSIST_TOP_K,
    PARETO_EPS_LOSING_TRADES,
    PARETO_EPS_MAX_DD_PCT,
    PARETO_EPS_NET_PNL_PAISA,
    PARETO_EPS_WIN_RATE,
    PARETO_FRONTIER_CAP,
    QUOTA_AXES,
    SelectionInputs,
    axis_topm_indices,
    compute_union_selection,
    join_basis_tokens,
    pareto_frontier_indices,
    union_persist_bound,
)
from hypothesis import given
from hypothesis import strategies as st


class TestConstants:
    def test_default_persist_top_k_is_the_spec_1000(self) -> None:
        assert DEFAULT_PERSIST_TOP_K == 1000

    def test_quota_m_and_frontier_cap_w1_defaults(self) -> None:
        assert AXIS_QUOTA_M == 200
        assert PARETO_FRONTIER_CAP == 500

    def test_epsilon_w1_defaults(self) -> None:
        assert PARETO_EPS_NET_PNL_PAISA == 1_000
        assert PARETO_EPS_WIN_RATE == 0.005
        assert PARETO_EPS_MAX_DD_PCT == 0.25
        assert PARETO_EPS_LOSING_TRADES == 0

    def test_union_persist_bound_law(self) -> None:
        # Review round 1 gate re-run: the ONE inflation-check bound —
        # top_k + QUOTA_AXES·quota_m + frontier_cap (loose, sound).
        assert QUOTA_AXES == 5
        assert union_persist_bound(1000) == 1000 + 5 * AXIS_QUOTA_M + PARETO_FRONTIER_CAP
        assert union_persist_bound(1) == 1 + 5 * AXIS_QUOTA_M + PARETO_FRONTIER_CAP
        assert union_persist_bound(10, quota_m=2, frontier_cap=3) == 10 + 5 * 2 + 3

    def test_basis_vocabulary_order(self) -> None:
        assert BASIS_ORDER == (
            BASIS_COMPOSITE,
            BASIS_DD,
            BASIS_LOSING_TRADES,
            BASIS_WIN_RATE,
            BASIS_EXPECTANCY,
            BASIS_FRONTIER,
        )


class TestJoinBasisTokens:
    def test_canonical_order_not_insertion_order(self) -> None:
        assert join_basis_tokens(("frontier", "composite", "dd")) == "composite,dd,frontier"

    def test_dedup(self) -> None:
        assert join_basis_tokens(("dd", "dd", "dd")) == "dd"

    def test_empty_is_empty_string(self) -> None:
        assert join_basis_tokens(()) == ""

    def test_unknown_token_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown selection_basis"):
            join_basis_tokens(("composite", "sharpe"))


class TestParetoFrontier:
    def test_empty(self) -> None:
        assert pareto_frontier_indices([], [], [], []) == []

    def test_single_point_kept(self) -> None:
        assert pareto_frontier_indices([100], [0.5], [5.0], [3]) == [0]

    def test_classical_dominance_at_eps_zero(self) -> None:
        kept = pareto_frontier_indices(
            [100, 200],
            [0.5, 0.6],
            [10.0, 5.0],
            [5, 2],
            eps_net_pnl_paisa=0,
            eps_win_rate=0.0,
            eps_max_dd_pct=0.0,
            eps_losing_trades=0,
        )
        assert kept == [1]

    def test_incomparable_points_both_kept(self) -> None:
        kept = pareto_frontier_indices(
            [200, 100],
            [0.5, 0.5],
            [10.0, 5.0],
            [5, 5],
            eps_net_pnl_paisa=0,
            eps_win_rate=0.0,
            eps_max_dd_pct=0.0,
            eps_losing_trades=0,
        )
        assert kept == [0, 1]

    def test_equal_points_all_kept(self) -> None:
        # Identical rows: the strict clause never fires — no domination.
        kept = pareto_frontier_indices(
            [100, 100],
            [0.5, 0.5],
            [5.0, 5.0],
            [3, 3],
            eps_net_pnl_paisa=0,
            eps_win_rate=0.0,
            eps_max_dd_pct=0.0,
            eps_losing_trades=0,
        )
        assert kept == [0, 1]

    def test_epsilon_absorbs_tiny_axis_win(self) -> None:
        # 0 beats 1 hugely on pnl; 1 beats 0 by 0.10 dd — inside ε_dd=0.25,
        # so 0 ε-dominates 1 at the W1 defaults but not at ε=0.
        pnl, wr, dd, lt = [1_000_000, 100], [0.5, 0.5], [5.10, 5.00], [3, 3]
        assert pareto_frontier_indices(
            pnl,
            wr,
            dd,
            lt,
            eps_net_pnl_paisa=0,
            eps_win_rate=0.0,
            eps_max_dd_pct=0.0,
            eps_losing_trades=0,
        ) == [0, 1]
        assert pareto_frontier_indices(pnl, wr, dd, lt) == [0]

    def test_epsilon_can_disarm_strict_clause(self) -> None:
        # The only differing axis differs by less than its ε — no
        # strictly-beyond-ε win exists, so nothing dominates.
        pnl, wr, dd, lt = [1_500, 1_000], [0.5, 0.5], [5.0, 5.0], [3, 3]
        assert pareto_frontier_indices(
            pnl,
            wr,
            dd,
            lt,
            eps_net_pnl_paisa=0,
            eps_win_rate=0.0,
            eps_max_dd_pct=0.0,
            eps_losing_trades=0,
        ) == [0]
        assert pareto_frontier_indices(pnl, wr, dd, lt) == [0, 1]

    def test_length_mismatch_raises(self) -> None:
        with pytest.raises(ValueError, match="length mismatch"):
            pareto_frontier_indices([1], [0.5, 0.6], [1.0], [1])

    def test_negative_epsilon_raises(self) -> None:
        for kwargs in (
            {"eps_net_pnl_paisa": -1},
            {"eps_win_rate": -0.1},
            {"eps_max_dd_pct": -0.1},
            {"eps_losing_trades": -1},
        ):
            with pytest.raises(ValueError, match="epsilon must be >= 0"):
                pareto_frontier_indices([1], [0.5], [1.0], [1], **kwargs)  # type: ignore[arg-type]

    @given(
        st.lists(
            st.tuples(
                st.integers(min_value=-10_000, max_value=10_000),
                st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
                st.floats(min_value=0.0, max_value=50.0, allow_nan=False),
                st.integers(min_value=0, max_value=100),
            ),
            min_size=1,
            max_size=25,
        )
    )
    def test_eps_zero_reduces_to_classical_pareto(
        self, points: list[tuple[int, float, float, int]]
    ) -> None:
        """At ε=0 the frontier equals the classical maximal set (independent oracle)."""

        def classical_dominates(x: int, y: int) -> bool:
            better_eq = (
                points[x][0] >= points[y][0]
                and points[x][1] >= points[y][1]
                and points[x][2] <= points[y][2]
                and points[x][3] <= points[y][3]
            )
            strictly = (
                points[x][0] > points[y][0]
                or points[x][1] > points[y][1]
                or points[x][2] < points[y][2]
                or points[x][3] < points[y][3]
            )
            return better_eq and strictly

        expected = [
            y
            for y in range(len(points))
            if not any(x != y and classical_dominates(x, y) for x in range(len(points)))
        ]
        actual = pareto_frontier_indices(
            [p[0] for p in points],
            [p[1] for p in points],
            [p[2] for p in points],
            [p[3] for p in points],
            eps_net_pnl_paisa=0,
            eps_win_rate=0.0,
            eps_max_dd_pct=0.0,
            eps_losing_trades=0,
        )
        assert actual == expected

    @given(
        st.lists(
            st.tuples(
                st.integers(min_value=-10_000, max_value=10_000),
                st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
                st.floats(min_value=0.0, max_value=50.0, allow_nan=False),
                st.integers(min_value=0, max_value=100),
            ),
            min_size=1,
            max_size=20,
        ),
        st.permutations(range(20)),
    )
    def test_permutation_invariance_of_membership(
        self, points: list[tuple[int, float, float, int]], perm: list[int]
    ) -> None:
        """Frontier MEMBERSHIP is input-order-free (indices remap under the permutation)."""
        order = [i for i in perm if i < len(points)]
        base = pareto_frontier_indices(
            [p[0] for p in points],
            [p[1] for p in points],
            [p[2] for p in points],
            [p[3] for p in points],
        )
        shuffled = [points[i] for i in order]
        permuted = pareto_frontier_indices(
            [p[0] for p in shuffled],
            [p[1] for p in shuffled],
            [p[2] for p in shuffled],
            [p[3] for p in shuffled],
        )
        assert sorted(order[j] for j in permuted) == base


# (composite, mask, dd, losing, win_rate, expectancy) parallel columns.
_TOPM_COLUMNS: tuple[list[float], list[int], list[float], list[int], list[float], list[int]] = (
    [1.0, 2.0, 3.0],
    [1, 2, 3],
    [10.0, 20.0, 5.0],
    [5, 3, 8],
    [0.50, 0.60, 0.40],
    [100, 300, 200],
)


class TestAxisTopM:
    def test_axis_order_laws(self) -> None:
        comp, mask, dd, losing, wr, exp = _TOPM_COLUMNS
        by_comp, by_dd, by_losing, by_wr, by_exp = axis_topm_indices(
            2, comp, mask, dd, losing, wr, exp
        )
        assert by_comp == [2, 1]  # composite DESC
        assert by_dd == [2, 0]  # dd ASC
        assert by_losing == [1, 0]  # losing ASC
        assert by_wr == [1, 0]  # win_rate DESC
        assert by_exp == [1, 2]  # expectancy DESC

    def test_tie_break_composite_then_mask(self) -> None:
        # Equal dd everywhere: higher composite first; equal composite too:
        # smaller mask — the (axis, -composite, mask) law.
        by_dd = axis_topm_indices(
            3,
            [1.0, 2.0, 2.0],
            [7, 9, 8],
            [5.0, 5.0, 5.0],
            [1, 1, 1],
            [0.5, 0.5, 0.5],
            [10, 10, 10],
        )[1]
        assert by_dd == [2, 1, 0]

    def test_m_zero_and_m_over_n(self) -> None:
        comp, mask, dd, losing, wr, exp = _TOPM_COLUMNS
        assert axis_topm_indices(0, comp, mask, dd, losing, wr, exp) == ([], [], [], [], [])
        assert axis_topm_indices(9, comp, mask, dd, losing, wr, exp)[0] == [2, 1, 0]

    def test_negative_m_raises(self) -> None:
        with pytest.raises(ValueError, match="m must be >= 0"):
            axis_topm_indices(-1, [1.0], [1], [5.0], [1], [0.5], [10])

    def test_length_mismatch_raises(self) -> None:
        with pytest.raises(ValueError, match="length mismatch"):
            axis_topm_indices(1, [1.0], [1, 2], [5.0], [1], [0.5], [10])


def _inputs(
    rows: list[tuple[int, float, int, float, float, int, int]],
) -> SelectionInputs:
    """Rows as (mask, composite, net_pnl, win_rate, dd, losing, expectancy)."""
    return SelectionInputs(
        masks=tuple(r[0] for r in rows),
        composites=tuple(r[1] for r in rows),
        net_pnl_paisa=tuple(r[2] for r in rows),
        win_rate=tuple(r[3] for r in rows),
        max_dd_pct=tuple(r[4] for r in rows),
        losing_trades=tuple(r[5] for r in rows),
        expectancy_paisa=tuple(r[6] for r in rows),
    )


class TestSelectionInputs:
    def test_parallel_length_validation(self) -> None:
        with pytest.raises(ValueError, match="parallel columns must match"):
            SelectionInputs(
                masks=(1, 2),
                composites=(1.0,),
                net_pnl_paisa=(1, 2),
                win_rate=(0.5, 0.5),
                max_dd_pct=(1.0, 1.0),
                losing_trades=(1, 1),
                expectancy_paisa=(1, 1),
            )


class TestComputeUnionSelection:
    def test_empty_pool(self) -> None:
        selection = compute_union_selection(_inputs([]), top_k=10)
        assert selection.order == ()
        assert selection.truncated == 0
        assert selection.frontier == frozenset()
        assert selection.frontier_dropped == 0

    def test_top_k_none_selects_whole_pool_as_composite(self) -> None:
        rows = [
            (1, 3.0, 300, 0.6, 5.0, 2, 30),
            (2, 2.0, 200, 0.5, 6.0, 3, 20),
            (3, 1.0, 100, 0.4, 7.0, 4, 10),
        ]
        selection = compute_union_selection(_inputs(rows), top_k=None)
        assert selection.order == (0, 1, 2)
        assert selection.truncated == 0
        for index in selection.order:
            assert BASIS_COMPOSITE in selection.basis_by_index[index]

    def test_truncation_counter_counts_unselected_gate_passers(self) -> None:
        # 5 rows, top_k=1, quota_m=0, and a frontier that keeps only row 0
        # (row 0 classically dominates everything at ε=0 defaults are wider —
        # construct rows where row 0 ε-dominates all others).
        rows = [
            (1, 5.0, 1_000_000, 0.9, 1.0, 0, 500),
            (2, 4.0, 100, 0.1, 30.0, 50, 5),
            (3, 3.0, 90, 0.1, 30.0, 50, 5),
            (4, 2.0, 80, 0.1, 30.0, 50, 5),
            (5, 1.0, 70, 0.1, 30.0, 50, 5),
        ]
        selection = compute_union_selection(_inputs(rows), top_k=1, quota_m=0)
        assert selection.order == (0,)
        assert selection.truncated == 4
        assert selection.frontier == frozenset({0})

    def test_union_rescues_dd_optimal_row_outside_composite_cut(self) -> None:
        """[FB anchor] a DD-optimal gate-passer outside composite top-K persists."""
        # Row 3 has the WORST composite but the BEST dd — under top_k=2 +
        # quota it must still be selected, tagged dd (+ frontier: it is
        # nondominated on the dd axis).
        rows = [
            (1, 9.0, 900, 0.80, 20.0, 10, 90),
            (2, 8.0, 800, 0.70, 21.0, 11, 80),
            (3, 7.0, 700, 0.60, 22.0, 12, 70),
            (4, 1.0, 100, 0.10, 0.5, 40, 10),
        ]
        selection = compute_union_selection(_inputs(rows), top_k=2, quota_m=1)
        assert 3 in selection.order
        assert BASIS_DD in selection.basis_by_index[3]
        assert 3 in selection.frontier
        # Persist order: composite prefix first, extras after, ascending.
        assert selection.order[0:2] == (0, 1)
        assert selection.order[-1] == 3 or 3 in selection.order[2:]

    def test_multi_basis_tags_join_canonically(self) -> None:
        rows = [(1, 5.0, 500, 0.9, 1.0, 0, 50)]
        selection = compute_union_selection(_inputs(rows), top_k=1)
        # The single row qualifies under every basis.
        assert selection.basis_by_index[0] == (
            "composite,dd,losing_trades,win_rate,expectancy,frontier"
        )

    def test_frontier_cap_drops_counted_and_warned(self) -> None:
        # 6 mutually-incomparable rows (ascending pnl, ascending dd) with a
        # cap of 4 → 2 dropped, kept = first 4 in composite order.
        rows = [(i, 10.0 - i, 100 * (i + 1), 0.5, float(i + 1) * 2.0, 5, 10) for i in range(6)]
        with structlog.testing.capture_logs() as logs:
            selection = compute_union_selection(
                _inputs(rows),
                top_k=None,
                quota_m=0,
                frontier_cap=4,
                frontier_fn=lambda *_cols: list(range(6)),
            )
        assert selection.frontier_dropped == 2
        assert selection.frontier == frozenset({0, 1, 2, 3})
        assert any(log["event"] == "multi_select.frontier_capped" for log in logs)

    def test_injected_fns_are_used(self) -> None:
        rows = [
            (1, 2.0, 200, 0.6, 5.0, 2, 20),
            (2, 1.0, 100, 0.5, 6.0, 3, 10),
        ]
        frontier_calls: list[int] = []
        quota_calls: list[int] = []

        def frontier_fn(*cols: object) -> list[int]:
            frontier_calls.append(1)
            return [1]

        def quota_fn(
            m: int, *cols: object
        ) -> tuple[list[int], list[int], list[int], list[int], list[int]]:
            quota_calls.append(m)
            return ([], [], [], [], [])

        selection = compute_union_selection(
            _inputs(rows), top_k=1, quota_m=7, frontier_fn=frontier_fn, quota_fn=quota_fn
        )
        assert frontier_calls == [1]
        assert quota_calls == [7]
        assert selection.frontier == frozenset({1})
        assert selection.basis_by_index[1] == BASIS_FRONTIER
        assert selection.order == (0, 1)

    def test_deterministic_tags_pure_function(self) -> None:
        rows = [
            (5, 2.0, 200, 0.6, 5.0, 2, 20),
            (9, 1.0, 100, 0.5, 6.0, 3, 10),
        ]
        first = compute_union_selection(_inputs(rows), top_k=1)
        second = compute_union_selection(_inputs(rows), top_k=1)
        assert first == second


class TestMutationWorkflowWiring:
    """Pin this module INTO ``mutation.yml``'s per-module zero-survivor loop.

    The DEC-LAKE-STATS-MANIFEST-001 round-8 lesson (an unwired mutation gate
    presented as deferred is a Golden-Rule-#10 defect). HONEST: pins the
    WIRING only — the weekly run executes on its own schedule.
    """

    def test_module_paired_with_this_suite_in_the_mutmut_loop(self) -> None:
        from pathlib import Path

        workflow = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "mutation.yml"
        text = workflow.read_text(encoding="utf-8")
        assert (
            '"brutex/ranking/multi_select.py:tests/unit/test_ranking_multi_select.py:"' in text
        ), "multi_select.py is not wired into mutation.yml's per-module mutmut loop"
