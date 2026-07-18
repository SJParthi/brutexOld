"""Unit tests for :mod:`brutex.engine.rust_rank_select` (LANE W1 seam).

DEC-RANK-PARETO-PERSIST-001 / DEC-RANK-QUOTA-PERSIST-1000-001: the
flag/burn-in/autorevert seam over the born-Rust selection kernels, incl. the
hypothesis differential (kernel vs oracle over hostile axis grids) against
the REAL compiled wheel. 100% line+branch standalone (whole-engine gate +
its own ci.yml stanza).
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import TYPE_CHECKING

import brutex.engine.rust_rank_select as seam
import pytest
import structlog
from brutex.engine.rust_rank_select import (
    RANK_RUST_SELECT_DEFAULT,
    RANK_RUST_SELECT_ENV,
    SELF_CHECK_SAMPLES,
    axis_quota_select,
    disable_kernel,
    frontier_select,
    kernel_disabled,
    rank_rust_select_enabled,
    reset_observability_memo_for_tests,
    reset_self_check_for_tests,
    union_selection,
    warn_fallback_once,
)
from brutex.ranking.multi_select import (
    SelectionInputs,
    axis_topm_indices,
    compute_union_selection,
    pareto_frontier_indices,
)
from hypothesis import given
from hypothesis import strategies as st

if TYPE_CHECKING:
    from collections.abc import Iterator

fno_math = pytest.importorskip("fno_math")


@pytest.fixture(autouse=True)
def _fresh_seam(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Fresh burn-in budget + memo + kernel-ON flag per test."""
    monkeypatch.setenv(RANK_RUST_SELECT_ENV, "1")
    reset_self_check_for_tests()
    reset_observability_memo_for_tests()
    yield
    reset_self_check_for_tests()
    reset_observability_memo_for_tests()


# Typed per-column fixture constants (pyright-exact: a mixed-value dict
# would widen every column to `list[int] | list[float]`).
_POOL_NET_PNL: list[int] = [1_000_000, 100, 90, 500_000]
_POOL_WIN_RATE: list[float] = [0.9, 0.1, 0.1, 0.5]
_POOL_DD: list[float] = [1.0, 30.0, 30.0, 15.0]
_POOL_LOSING: list[int] = [0, 50, 50, 10]
_QUOTA_COMPOSITE: list[float] = [1.0, 2.0, 3.0]
_QUOTA_MASKS: list[int] = [1, 2, (1 << 90) | 3]
_QUOTA_DD: list[float] = [10.0, 20.0, 5.0]
_QUOTA_LOSING: list[int] = [5, 3, 8]
_QUOTA_WIN_RATE: list[float] = [0.5, 0.6, 0.4]
_QUOTA_EXPECTANCY: list[int] = [100, 300, 200]


def _frontier() -> list[int]:
    return frontier_select(_POOL_NET_PNL, _POOL_WIN_RATE, _POOL_DD, _POOL_LOSING)


def _quota(m: int = 2) -> tuple[list[int], ...]:
    return axis_quota_select(
        m,
        _QUOTA_COMPOSITE,
        _QUOTA_MASKS,
        _QUOTA_DD,
        _QUOTA_LOSING,
        _QUOTA_WIN_RATE,
        _QUOTA_EXPECTANCY,
    )


class TestFlagResolution:
    def test_default_constant_documented_state(self) -> None:
        # ON since the LANE-W1 flip commit (the 2026-07-18 greenfield
        # ruling, coordinator-relayed) — the ONE-constant revert lever;
        # BRUTEX_RANK_RUST_SELECT=0 stays the per-process kill switch.
        assert RANK_RUST_SELECT_DEFAULT is True

    def test_truthy_forces_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(RANK_RUST_SELECT_ENV, "yes")
        assert rank_rust_select_enabled() is True

    def test_falsy_forces_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(RANK_RUST_SELECT_ENV, "off")
        assert rank_rust_select_enabled() is False

    def test_unset_falls_to_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(RANK_RUST_SELECT_ENV, raising=False)
        assert rank_rust_select_enabled() is RANK_RUST_SELECT_DEFAULT

    def test_whitespace_falls_to_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(RANK_RUST_SELECT_ENV, "   ")
        assert rank_rust_select_enabled() is RANK_RUST_SELECT_DEFAULT

    def test_unrecognised_fails_safe_off_with_warn_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(RANK_RUST_SELECT_ENV, "flase")
        with structlog.testing.capture_logs() as logs:
            assert rank_rust_select_enabled() is False
            assert rank_rust_select_enabled() is False
        warns = [log for log in logs if log["event"] == "rust_rank_select.fallback"]
        assert len(warns) == 1
        assert warns[0]["reason"] == "flag_unrecognised"


class TestLatchAndMemos:
    def test_disable_kernel_latches_once(self) -> None:
        assert not kernel_disabled()
        with structlog.testing.capture_logs() as logs:
            disable_kernel("test_reason", detail=1)
            disable_kernel("second_reason")
        assert kernel_disabled()
        errors = [log for log in logs if log["event"] == "rust_rank_select.kernel_autoreverted"]
        assert len(errors) == 1
        assert errors[0]["reason"] == "test_reason"

    def test_warn_fallback_once_memo(self) -> None:
        with structlog.testing.capture_logs() as logs:
            warn_fallback_once("some_reason")
            warn_fallback_once("some_reason")
        assert len([log for log in logs if log["event"] == "rust_rank_select.fallback"]) == 1

    def test_reset_seams(self) -> None:
        disable_kernel("x")
        reset_self_check_for_tests(remaining=0)
        assert not kernel_disabled()
        reset_self_check_for_tests()
        assert seam._SELF_CHECK_REMAINING == {
            "frontier": SELF_CHECK_SAMPLES,
            "quota": SELF_CHECK_SAMPLES,
        }


class TestValidIndexList:
    def test_valid_ascending(self) -> None:
        assert seam._valid_index_list([0, 2, 5], 6, ascending=True)

    def test_not_a_list(self) -> None:
        assert not seam._valid_index_list((0, 1), 5, ascending=True)

    def test_bool_refused(self) -> None:
        assert not seam._valid_index_list([True], 5, ascending=True)

    def test_non_int_refused(self) -> None:
        assert not seam._valid_index_list([0.5], 5, ascending=True)

    def test_out_of_range_refused(self) -> None:
        assert not seam._valid_index_list([5], 5, ascending=True)
        assert not seam._valid_index_list([-1], 5, ascending=True)

    def test_non_ascending_refused(self) -> None:
        assert not seam._valid_index_list([2, 1], 5, ascending=True)
        assert not seam._valid_index_list([1, 1], 5, ascending=True)

    def test_unordered_dup_refused(self) -> None:
        assert not seam._valid_index_list([2, 1, 2], 5, ascending=False)
        assert seam._valid_index_list([2, 1, 0], 5, ascending=False)


class TestFrontierSelectArms:
    def test_off_serves_oracle_and_never_imports_kernel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(RANK_RUST_SELECT_ENV, "0")

        def bomb(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("kernel must not be resolved on the OFF arm")

        monkeypatch.setattr(seam, "_resolve_kernel", bomb)
        assert _frontier() == pareto_frontier_indices(
            _POOL_NET_PNL, _POOL_WIN_RATE, _POOL_DD, _POOL_LOSING
        )

    def test_on_serves_kernel_result_and_engages(self) -> None:
        with structlog.testing.capture_logs() as logs:
            result = _frontier()
        assert result == pareto_frontier_indices(
            _POOL_NET_PNL, _POOL_WIN_RATE, _POOL_DD, _POOL_LOSING
        )
        assert any(log["event"] == "rust_rank_select.engaged" for log in logs)

    def test_latched_process_short_circuits(self) -> None:
        disable_kernel("prior_mismatch")
        with structlog.testing.capture_logs() as logs:
            result = _frontier()
        assert result == pareto_frontier_indices(
            _POOL_NET_PNL, _POOL_WIN_RATE, _POOL_DD, _POOL_LOSING
        )
        warns = [log for log in logs if log.get("reason") == "kernel_disabled"]
        assert len(warns) == 1

    def test_wheel_missing_degrades(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "fno_math", None)
        with structlog.testing.capture_logs() as logs:
            result = _frontier()
        assert result == pareto_frontier_indices(
            _POOL_NET_PNL, _POOL_WIN_RATE, _POOL_DD, _POOL_LOSING
        )
        assert any(log.get("reason") == "wheel_missing" for log in logs)

    def test_wheel_stale_degrades(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "fno_math", SimpleNamespace())
        with structlog.testing.capture_logs() as logs:
            result = _frontier()
        assert result == pareto_frontier_indices(
            _POOL_NET_PNL, _POOL_WIN_RATE, _POOL_DD, _POOL_LOSING
        )
        assert any(log.get("reason") == "wheel_stale" for log in logs)

    def test_kernel_error_degrades(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def raising(*_args: object) -> object:
            raise ValueError("boom")

        monkeypatch.setitem(sys.modules, "fno_math", SimpleNamespace(rank_pareto_frontier=raising))
        with structlog.testing.capture_logs() as logs:
            result = _frontier()
        assert result == pareto_frontier_indices(
            _POOL_NET_PNL, _POOL_WIN_RATE, _POOL_DD, _POOL_LOSING
        )
        assert any(log.get("reason") == "kernel_error" for log in logs)

    def test_memory_error_reraises_bare(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def oom(*_args: object) -> object:
            raise MemoryError

        monkeypatch.setitem(sys.modules, "fno_math", SimpleNamespace(rank_pareto_frontier=oom))
        with pytest.raises(MemoryError):
            _frontier()

    def test_poison_result_refused_serves_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def poison(*_args: object) -> list[int]:
            return [99, 98]

        monkeypatch.setitem(sys.modules, "fno_math", SimpleNamespace(rank_pareto_frontier=poison))
        with structlog.testing.capture_logs() as logs:
            result = _frontier()
            result_again = _frontier()
        assert (
            result
            == result_again
            == pareto_frontier_indices(_POOL_NET_PNL, _POOL_WIN_RATE, _POOL_DD, _POOL_LOSING)
        )
        refusals = [log for log in logs if log["event"] == "rust_rank_select.kernel_result_refused"]
        assert len(refusals) == 1  # warn-once per function

    def test_burn_in_mismatch_autoreverts_and_ships_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A domain-VALID but WRONG kernel result: index 1 is a real index but
        # not the oracle frontier — the burn-in must catch it.
        def wrong(*_args: object) -> list[int]:
            return [1]

        monkeypatch.setitem(sys.modules, "fno_math", SimpleNamespace(rank_pareto_frontier=wrong))
        expected = pareto_frontier_indices(_POOL_NET_PNL, _POOL_WIN_RATE, _POOL_DD, _POOL_LOSING)
        assert expected != [1]
        with structlog.testing.capture_logs() as logs:
            result = _frontier()
        assert result == expected  # the oracle value SHIPS
        assert kernel_disabled()
        assert any(log["event"] == "rust_rank_select.kernel_autoreverted" for log in logs)

    def test_post_burn_in_steady_state_skips_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reset_self_check_for_tests(remaining=0)
        calls: list[int] = []
        real_oracle = seam.pareto_frontier_indices

        def counting_oracle(*args: object, **kwargs: object) -> list[int]:
            calls.append(1)
            return real_oracle(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(seam, "pareto_frontier_indices", counting_oracle)
        result = _frontier()
        assert calls == []  # steady state: kernel only, no oracle double-run
        assert result == pareto_frontier_indices(
            _POOL_NET_PNL, _POOL_WIN_RATE, _POOL_DD, _POOL_LOSING
        )

    def test_burn_in_budget_decrements_per_passed_call(self) -> None:
        before = seam._SELF_CHECK_REMAINING["frontier"]
        _frontier()
        assert seam._SELF_CHECK_REMAINING["frontier"] == before - 1


class TestQuotaSelectArms:
    def test_on_matches_oracle(self) -> None:
        assert _quota() == axis_topm_indices(
            2,
            _QUOTA_COMPOSITE,
            _QUOTA_MASKS,
            _QUOTA_DD,
            _QUOTA_LOSING,
            _QUOTA_WIN_RATE,
            _QUOTA_EXPECTANCY,
        )

    def test_off_serves_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(RANK_RUST_SELECT_ENV, "false")
        assert _quota() == axis_topm_indices(
            2,
            _QUOTA_COMPOSITE,
            _QUOTA_MASKS,
            _QUOTA_DD,
            _QUOTA_LOSING,
            _QUOTA_WIN_RATE,
            _QUOTA_EXPECTANCY,
        )

    def test_latched_serves_oracle(self) -> None:
        disable_kernel("x")
        assert _quota() == axis_topm_indices(
            2,
            _QUOTA_COMPOSITE,
            _QUOTA_MASKS,
            _QUOTA_DD,
            _QUOTA_LOSING,
            _QUOTA_WIN_RATE,
            _QUOTA_EXPECTANCY,
        )

    def test_wheel_stale_degrades(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "fno_math", SimpleNamespace())
        assert _quota() == axis_topm_indices(
            2,
            _QUOTA_COMPOSITE,
            _QUOTA_MASKS,
            _QUOTA_DD,
            _QUOTA_LOSING,
            _QUOTA_WIN_RATE,
            _QUOTA_EXPECTANCY,
        )

    def test_kernel_error_degrades(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def raising(*_args: object) -> object:
            raise OverflowError("boom")

        monkeypatch.setitem(sys.modules, "fno_math", SimpleNamespace(rank_axis_topm=raising))
        assert _quota() == axis_topm_indices(
            2,
            _QUOTA_COMPOSITE,
            _QUOTA_MASKS,
            _QUOTA_DD,
            _QUOTA_LOSING,
            _QUOTA_WIN_RATE,
            _QUOTA_EXPECTANCY,
        )

    def test_memory_error_reraises_bare(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def oom(*_args: object) -> object:
            raise MemoryError

        monkeypatch.setitem(sys.modules, "fno_math", SimpleNamespace(rank_axis_topm=oom))
        with pytest.raises(MemoryError):
            _quota()

    @pytest.mark.parametrize(
        "poison",
        [
            [[], [], [], [], []],  # list, not tuple
            ([], [], [], []),  # wrong arity
            ([0, 0], [], [], [], []),  # dup within a list
            ([99], [], [], [], []),  # out of range
            ([0, 1, 2], [], [], [], []),  # longer than m
        ],
    )
    def test_poison_results_refused(self, monkeypatch: pytest.MonkeyPatch, poison: object) -> None:
        monkeypatch.setitem(
            sys.modules,
            "fno_math",
            SimpleNamespace(rank_axis_topm=lambda *_a: poison),
        )
        with structlog.testing.capture_logs() as logs:
            result = _quota()
        assert result == axis_topm_indices(
            2,
            _QUOTA_COMPOSITE,
            _QUOTA_MASKS,
            _QUOTA_DD,
            _QUOTA_LOSING,
            _QUOTA_WIN_RATE,
            _QUOTA_EXPECTANCY,
        )
        assert any(log["event"] == "rust_rank_select.kernel_result_refused" for log in logs)

    def test_burn_in_mismatch_autoreverts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def wrong(*_args: object) -> object:
            return ([0], [0], [0], [0], [0])  # valid shape, wrong content

        monkeypatch.setitem(sys.modules, "fno_math", SimpleNamespace(rank_axis_topm=wrong))
        expected = axis_topm_indices(
            2,
            _QUOTA_COMPOSITE,
            _QUOTA_MASKS,
            _QUOTA_DD,
            _QUOTA_LOSING,
            _QUOTA_WIN_RATE,
            _QUOTA_EXPECTANCY,
        )
        result = _quota()
        assert result == expected
        assert kernel_disabled()

    def test_quota_autorevert_names_the_first_differing_axis(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Review round 1 (NIT): the quota autorevert must carry the frontier
        # arm's diagnostic discipline — the FIRST differing axis by name +
        # both bounded index prefixes. The composite axis is fed the ORACLE
        # value so the diff sits on a LATER axis ("dd") — also covering the
        # equal-axis skip in the first-diff scan.
        expected = axis_topm_indices(
            2,
            _QUOTA_COMPOSITE,
            _QUOTA_MASKS,
            _QUOTA_DD,
            _QUOTA_LOSING,
            _QUOTA_WIN_RATE,
            _QUOTA_EXPECTANCY,
        )
        # m=2 over 3 rows ⇒ exactly one index is outside the oracle's dd axis.
        wrong_dd = [index for index in range(len(_QUOTA_DD)) if index not in expected[1]][:1]
        assert wrong_dd and wrong_dd != expected[1]

        def wrong(*_args: object) -> object:
            return (expected[0], wrong_dd, expected[2], expected[3], expected[4])

        monkeypatch.setitem(sys.modules, "fno_math", SimpleNamespace(rank_axis_topm=wrong))
        with structlog.testing.capture_logs() as logs:
            result = _quota()
        assert result == expected  # the oracle value SHIPS
        assert kernel_disabled()
        (autorevert,) = [
            log for log in logs if log["event"] == "rust_rank_select.kernel_autoreverted"
        ]
        assert autorevert["reason"] == "quota_mismatch"
        assert autorevert["axis"] == "dd"
        assert autorevert["kernel"] == wrong_dd[:8]
        assert autorevert["oracle"] == expected[1][:8]

    def test_burn_in_budget_decrements(self) -> None:
        before = seam._SELF_CHECK_REMAINING["quota"]
        _quota()
        assert seam._SELF_CHECK_REMAINING["quota"] == before - 1

    def test_poison_refusal_logs_once_per_process(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The quota twin of the frontier's warn-once refusal memo: a SECOND
        # refused kernel result skips the ERROR log (memo hit) yet still
        # serves the oracle.
        monkeypatch.setitem(
            sys.modules, "fno_math", SimpleNamespace(rank_axis_topm=lambda *_a: "junk")
        )
        with structlog.testing.capture_logs() as logs:
            first = _quota()
            second = _quota()
        expected = axis_topm_indices(
            2,
            _QUOTA_COMPOSITE,
            _QUOTA_MASKS,
            _QUOTA_DD,
            _QUOTA_LOSING,
            _QUOTA_WIN_RATE,
            _QUOTA_EXPECTANCY,
        )
        assert first == second == expected
        refusals = [log for log in logs if log["event"] == "rust_rank_select.kernel_result_refused"]
        assert len(refusals) == 1  # warn-once per function

    def test_post_burn_in_steady_state_skips_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The quota twin of the frontier steady-state pin: with the burn-in
        # budget exhausted the kernel serves WITHOUT the oracle double-run.
        reset_self_check_for_tests(remaining=0)
        calls: list[int] = []
        real_oracle = seam.axis_topm_indices

        def counting_oracle(*args: object, **kwargs: object) -> object:
            calls.append(1)
            return real_oracle(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(seam, "axis_topm_indices", counting_oracle)
        result = _quota()
        assert calls == []  # steady state: kernel only
        assert result == axis_topm_indices(
            2,
            _QUOTA_COMPOSITE,
            _QUOTA_MASKS,
            _QUOTA_DD,
            _QUOTA_LOSING,
            _QUOTA_WIN_RATE,
            _QUOTA_EXPECTANCY,
        )


class TestBurnInBookkeeping:
    def test_burn_in_passed_is_a_noop_at_zero_budget(self) -> None:
        # The `_burn_in_passed` floor guard: at an exhausted budget the
        # decrement is skipped (the budget can never go negative). Direct
        # call — production gates it behind `_consume_burn_in`, so this
        # branch is the defensive floor, pinned here.
        reset_self_check_for_tests(remaining=0)
        seam._burn_in_passed("frontier")
        assert seam._SELF_CHECK_REMAINING["frontier"] == 0


class TestUnionSelection:
    def test_union_selection_matches_pure_composer_with_oracles(self) -> None:
        inputs = SelectionInputs(
            masks=(1, 2, 3, 4),
            composites=(4.0, 3.0, 2.0, 1.0),
            net_pnl_paisa=(400, 300, 200, 100),
            win_rate=(0.8, 0.6, 0.4, 0.2),
            max_dd_pct=(4.0, 3.0, 2.0, 1.0),
            losing_trades=(1, 2, 3, 4),
            expectancy_paisa=(40, 30, 20, 10),
        )
        via_seam = union_selection(inputs, top_k=2)
        via_oracle = compute_union_selection(inputs, top_k=2)
        assert via_seam == via_oracle


class TestHypothesisDifferential:
    """The kernel-vs-oracle differential over hostile axis grids (real wheel)."""

    @given(
        st.lists(
            st.tuples(
                st.integers(min_value=-(10**12), max_value=10**12),
                st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
                st.floats(min_value=0.0, max_value=100.0, allow_nan=False),
                st.integers(min_value=0, max_value=10_000),
            ),
            min_size=0,
            max_size=40,
        )
    )
    def test_frontier_kernel_equals_oracle(
        self, points: list[tuple[int, float, float, int]]
    ) -> None:
        net = [p[0] for p in points]
        wr = [p[1] for p in points]
        dd = [p[2] for p in points]
        lt = [p[3] for p in points]
        from brutex.ranking.multi_select import (
            PARETO_EPS_LOSING_TRADES,
            PARETO_EPS_MAX_DD_PCT,
            PARETO_EPS_NET_PNL_PAISA,
            PARETO_EPS_WIN_RATE,
        )

        kernel = fno_math.rank_pareto_frontier(
            net,
            wr,
            dd,
            lt,
            PARETO_EPS_NET_PNL_PAISA,
            PARETO_EPS_WIN_RATE,
            PARETO_EPS_MAX_DD_PCT,
            PARETO_EPS_LOSING_TRADES,
        )
        assert kernel == pareto_frontier_indices(net, wr, dd, lt)

    @given(
        st.lists(
            st.tuples(
                st.floats(min_value=-10.0, max_value=10.0, allow_nan=False),
                st.integers(min_value=0, max_value=(1 << 128) - 1),
                st.floats(min_value=0.0, max_value=100.0, allow_nan=False),
                st.integers(min_value=0, max_value=10_000),
                st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
                st.integers(min_value=-(2**53), max_value=2**53),
            ),
            min_size=0,
            max_size=30,
            unique_by=lambda row: row[1],  # unique masks (the pool law)
        ),
        st.integers(min_value=0, max_value=35),
    )
    def test_quota_kernel_equals_oracle(
        self,
        rows: list[tuple[float, int, float, int, float, int]],
        m: int,
    ) -> None:
        composite = [r[0] for r in rows]
        masks = [r[1] for r in rows]
        dd = [r[2] for r in rows]
        losing = [r[3] for r in rows]
        wr = [r[4] for r in rows]
        exp = [r[5] for r in rows]
        kernel = fno_math.rank_axis_topm(
            m,
            composite,
            [mask & ((1 << 64) - 1) for mask in masks],
            [mask >> 64 for mask in masks],
            dd,
            losing,
            wr,
            exp,
        )
        oracle = axis_topm_indices(m, composite, masks, dd, losing, wr, exp)
        assert tuple(kernel) == tuple(oracle)

    @given(
        st.lists(
            st.floats(min_value=0.0, max_value=1000.0, allow_nan=False),
            min_size=0,
            max_size=50,
        )
    )
    def test_dd_band_kernel_equals_python_floor(self, dd: list[float]) -> None:
        import math

        from brutex.ranking.presets import DD_BAND_PCT

        kernel = fno_math.rank_dd_band_indices(dd, DD_BAND_PCT)
        assert kernel == [math.floor(value / DD_BAND_PCT) for value in dd]

    def test_dd_band_edge_vectors(self) -> None:
        import math

        from brutex.ranking.presets import DD_BAND_PCT

        edges = [0.0, 0.25, 0.49999999999, 0.5, 0.75, 0.99999999999999, 1.0, 4.99, 5.0, 5.01]
        kernel = fno_math.rank_dd_band_indices(edges, DD_BAND_PCT)
        assert kernel == [math.floor(value / DD_BAND_PCT) for value in edges]


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
            '"brutex/engine/rust_rank_select.py:tests/unit/test_engine_rust_rank_select.py:"'
            in text
        ), "rust_rank_select.py is not wired into mutation.yml's per-module mutmut loop"
