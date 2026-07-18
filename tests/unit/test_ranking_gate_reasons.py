"""Unit tests for :func:`brutex.ranking.gate_reasons` (DEC-SWEEP-RESULTS-DEPTH-001).

Gap-register ruling 12 / R28: the reason-returning twin of ``passes_gates`` —
every failed hard-gate door named from the STABLE :data:`GATE_REASON_ORDER`
vocabulary, empty tuple = pass. Every case here ALSO asserts the twin-parity
law ``bool(passes_gates(m)) == (gate_reasons(m) == ())`` so the two functions
can never drift door-by-door (the hypothesis-randomized parity property lives
in ``tests/property/test_ranking_gate_reasons_property.py``).
"""

from __future__ import annotations

import ast
import inspect
import math
from pathlib import Path

import pytest
from brutex.metrics.compute import ResultMetrics
from brutex.ranking import (
    GATE_CONSECUTIVE_LOSSES,
    GATE_MAX_DRAWDOWN,
    GATE_MIN_TRADES,
    GATE_NON_FINITE_INPUT,
    GATE_PROFIT_FACTOR,
    GATE_REASON_ORDER,
    GATE_SL_HIT_RATE,
    GATE_WIN_RATE_WLR,
    gate_reasons,
    passes_gates,
)

from tests.fakes.metrics_fixtures import make_result_metrics


def _assert_twin_parity(m: ResultMetrics) -> None:
    """The one law both functions must always agree on (ruling 12)."""
    assert passes_gates(m) == (gate_reasons(m) == ())


class TestVocabulary:
    """The wire vocabulary is stable, complete, ordered, and duplicate-free."""

    def test_reason_order_is_the_exact_wire_vocabulary(self) -> None:
        """P2's manifest counter fold keys on these EXACT strings — a rename is
        a breaking contract change, so the literals are pinned here."""
        assert GATE_REASON_ORDER == (
            "non_finite_input",
            "min_trades",
            "profit_factor",
            "max_drawdown",
            "win_rate_wlr",
            "consecutive_losses",
            "sl_hit_rate",
        )

    def test_tokens_match_their_constants(self) -> None:
        assert GATE_NON_FINITE_INPUT == "non_finite_input"
        assert GATE_MIN_TRADES == "min_trades"
        assert GATE_PROFIT_FACTOR == "profit_factor"
        assert GATE_MAX_DRAWDOWN == "max_drawdown"
        assert GATE_WIN_RATE_WLR == "win_rate_wlr"
        assert GATE_CONSECUTIVE_LOSSES == "consecutive_losses"
        assert GATE_SL_HIT_RATE == "sl_hit_rate"

    def test_sl_hit_rate_token_is_appended_last(self) -> None:
        """The wire vocabulary is append-only (Ruling 12): the Stage-B Door-6
        token sits strictly LAST so pre-extension consumers' indices hold."""
        assert GATE_REASON_ORDER[-1] == GATE_SL_HIT_RATE

    def test_no_duplicate_tokens(self) -> None:
        assert len(set(GATE_REASON_ORDER)) == len(GATE_REASON_ORDER)


class TestPassCase:
    def test_gate_passer_returns_empty_tuple(self) -> None:
        m = make_result_metrics()
        assert gate_reasons(m) == ()
        assert passes_gates(m) is True
        _assert_twin_parity(m)

    def test_result_is_a_tuple_not_a_list(self) -> None:
        """The wire shape is an immutable tuple (hashable, safe to share)."""
        assert isinstance(gate_reasons(make_result_metrics()), tuple)


class TestSingleDoors:
    """Each of the 5 hard doors fires alone, named, in twin parity."""

    def test_min_trades(self) -> None:
        m = make_result_metrics(total_trades=29, winning_trades=15, losing_trades=14)
        assert gate_reasons(m) == (GATE_MIN_TRADES,)
        _assert_twin_parity(m)

    def test_profit_factor_below_threshold(self) -> None:
        m = make_result_metrics(profit_factor=1.19)
        assert gate_reasons(m) == (GATE_PROFIT_FACTOR,)
        _assert_twin_parity(m)

    def test_profit_factor_none_fails_the_same_door(self) -> None:
        """A ``None`` metric (mathematically undefined) fails — the passes_gates law."""
        m = make_result_metrics(profit_factor=None)
        assert gate_reasons(m) == (GATE_PROFIT_FACTOR,)
        _assert_twin_parity(m)

    def test_max_drawdown(self) -> None:
        m = make_result_metrics(max_drawdown_pct=30.01)
        assert gate_reasons(m) == (GATE_MAX_DRAWDOWN,)
        _assert_twin_parity(m)

    def test_win_rate_with_low_wlr(self) -> None:
        m = make_result_metrics(win_rate=0.2, win_loss_ratio=1.9)
        assert gate_reasons(m) == (GATE_WIN_RATE_WLR,)
        _assert_twin_parity(m)

    def test_win_rate_with_none_wlr_counts_as_zero(self) -> None:
        """``None`` win/loss ratio projects to 0.0 — exactly like passes_gates."""
        m = make_result_metrics(win_rate=0.2, win_loss_ratio=None)
        assert gate_reasons(m) == (GATE_WIN_RATE_WLR,)
        _assert_twin_parity(m)

    def test_low_win_rate_with_big_wlr_passes_the_door(self) -> None:
        """A low hit-rate with winners ≥2× losers is acceptable (the AND law)."""
        m = make_result_metrics(win_rate=0.2, win_loss_ratio=2.0)
        assert gate_reasons(m) == ()
        _assert_twin_parity(m)

    def test_consecutive_losses(self) -> None:
        m = make_result_metrics(max_consecutive_losses=11)
        assert gate_reasons(m) == (GATE_CONSECUTIVE_LOSSES,)
        _assert_twin_parity(m)


class TestBoundaries:
    """Exact-threshold values PASS on both twins (the gates are strict </>)."""

    @pytest.mark.parametrize(
        "overrides",
        [
            {"total_trades": 30, "winning_trades": 18, "losing_trades": 12},
            {"profit_factor": 1.2},
            {"max_drawdown_pct": 30.0},
            {"win_rate": 0.25, "win_loss_ratio": 0.0},
            {"win_rate": 0.2, "win_loss_ratio": 2.0},
            {"max_consecutive_losses": 10},
        ],
    )
    def test_at_threshold_passes_both_twins(self, overrides: dict[str, object]) -> None:
        m = make_result_metrics(**overrides)
        assert gate_reasons(m) == ()
        assert passes_gates(m) is True


class TestMultipleDoors:
    """All failed doors are reported, in the documented evaluation order."""

    def test_every_door_fails_reports_all_five_in_order(self) -> None:
        m = make_result_metrics(
            total_trades=1,
            winning_trades=0,
            losing_trades=1,
            profit_factor=0.5,
            max_drawdown_pct=50.0,
            win_rate=0.1,
            win_loss_ratio=0.5,
            max_consecutive_losses=20,
        )
        assert gate_reasons(m) == (
            GATE_MIN_TRADES,
            GATE_PROFIT_FACTOR,
            GATE_MAX_DRAWDOWN,
            GATE_WIN_RATE_WLR,
            GATE_CONSECUTIVE_LOSSES,
        )
        _assert_twin_parity(m)

    def test_two_doors_keep_relative_order(self) -> None:
        m = make_result_metrics(max_drawdown_pct=99.0, max_consecutive_losses=15)
        assert gate_reasons(m) == (GATE_MAX_DRAWDOWN, GATE_CONSECUTIVE_LOSSES)
        _assert_twin_parity(m)

    def test_first_reason_is_the_passes_gates_short_circuit_door(self) -> None:
        """``gate_reasons(m)[0]`` names the door passes_gates trips first on."""
        m = make_result_metrics(profit_factor=0.1, max_consecutive_losses=99)
        assert gate_reasons(m)[0] == GATE_PROFIT_FACTOR
        _assert_twin_parity(m)


class TestNonFiniteFailClosed:
    """A present-but-non-finite gate input short-circuits to the ONE guard token."""

    @pytest.mark.parametrize(
        "overrides",
        [
            {"profit_factor": math.nan},
            {"profit_factor": math.inf},
            {"max_drawdown_pct": math.nan},
            {"max_drawdown_pct": -math.inf},
            {"win_rate": math.nan},
            {"win_loss_ratio": math.nan},
            {"win_loss_ratio": -math.inf},
        ],
    )
    def test_non_finite_input_is_the_only_reason(self, overrides: dict[str, object]) -> None:
        m = make_result_metrics(**overrides)
        assert gate_reasons(m) == (GATE_NON_FINITE_INPUT,)
        _assert_twin_parity(m)

    def test_non_finite_masks_other_door_verdicts(self) -> None:
        """Even with other doors provably failing, malformed metrics report ONLY
        the guard: judging the remaining doors on garbage would fabricate
        verdicts (NaN silently passes any comparison — Golden Rule #1)."""
        m = make_result_metrics(
            profit_factor=math.nan,
            total_trades=0,
            winning_trades=0,
            losing_trades=0,
            max_consecutive_losses=99,
        )
        assert gate_reasons(m) == (GATE_NON_FINITE_INPUT,)
        _assert_twin_parity(m)

    def test_none_is_not_non_finite(self) -> None:
        """``None`` keeps its per-door semantics — it never trips the guard."""
        m = make_result_metrics(profit_factor=None, win_loss_ratio=None)
        assert GATE_NON_FINITE_INPUT not in gate_reasons(m)
        _assert_twin_parity(m)


class TestTwinWlrProjectionStructuralParity:
    """Structural twin-parity pins for the door-4 ``None`` → 0.0 projection.

    Mutation-gate hardening (DEC-RANKING-MUTATION-ZERO-SURVIVOR-001,
    MIGRATION.md #1750): mutmut's ``else 0.0`` → ``else 1.0`` fallback mutant
    in ``gate_reasons`` is BEHAVIOURALLY unobservable at today's threshold —
    ``wlr`` feeds ONLY the ``wlr < _MIN_WIN_LOSS_RATIO`` (2.0) comparison, so
    every sub-2.0 fallback yields the identical door verdict (the SAME
    provable equivalence MIGRATION.md #615 documented for ``passes_gates``'
    twin line, which carries a ``# pragma: no mutate`` for exactly this
    reason). That hardening slice is TEST-ONLY (no source edit, so no new
    pragma), and the fallback value IS a documented wire contract — "``None``
    ratio counts as ``0.0``, exactly like :func:`passes_gates`" (the
    GATE_WIN_RATE_WLR docstring) — so the contract is pinned STRUCTURALLY
    instead: the twins' ``wlr`` projection expressions must be AST-identical
    and the shared fallback literal must be exactly ``0.0``. mutmut mutates
    the source file in place, so these pins re-read the live module source
    and FAIL under the fallback mutant (and under any future twin drift)
    without excluding a single mutant from the mutation surface.
    """

    @staticmethod
    def _wlr_projection(func_name: str) -> ast.expr:
        """The ``wlr = …`` projection expression inside ``func_name``'s body."""
        source = Path(inspect.getfile(gate_reasons)).read_text(encoding="utf-8")
        module = ast.parse(source)
        func = next(
            node
            for node in module.body
            if isinstance(node, ast.FunctionDef) and node.name == func_name
        )
        for node in ast.walk(func):
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "wlr"
            ):
                return node.value
        raise AssertionError(f"no `wlr = ...` assignment found in {func_name}")

    def test_wlr_projection_is_ast_identical_between_twins(self) -> None:
        """The twins share ONE projection expression (the §11 COMMON law)."""
        assert ast.dump(self._wlr_projection("gate_reasons")) == ast.dump(
            self._wlr_projection("passes_gates")
        )

    def test_wlr_projection_fallback_literal_is_exactly_zero(self) -> None:
        """``None`` projects to the documented ``0.0`` in BOTH twins."""
        for func_name in ("gate_reasons", "passes_gates"):
            projection = self._wlr_projection(func_name)
            assert isinstance(projection, ast.IfExp), func_name
            fallback = projection.orelse
            assert isinstance(fallback, ast.Constant), func_name
            assert isinstance(fallback.value, float), func_name
            assert fallback.value == 0.0, func_name


class TestDoor6SlHitRate:
    """The opt-in Door-6 SL-hit-rate ceiling (DEC-METRICS-LOSSRATE-FIRSTCLASS-001
    Stage B) — knob semantics + twin parity + fail-closed absence."""

    @staticmethod
    def _assert_twin_parity(m: ResultMetrics, ceiling: float | None) -> None:
        assert passes_gates(m, max_sl_hit_rate=ceiling) == (
            gate_reasons(m, max_sl_hit_rate=ceiling) == ()
        )

    def test_none_knob_never_emits_the_token(self) -> None:
        """The default knob is the pre-door behaviour — even a ceiling-busting
        rate emits nothing (the door does not exist)."""
        m = make_result_metrics(sl_hit_rate=0.99)
        assert gate_reasons(m) == ()
        assert passes_gates(m) is True

    def test_none_knob_ignores_a_non_finite_sl_hit_rate(self) -> None:
        """With the knob OFF, ``sl_hit_rate`` is not a gate input at all — a
        NaN in it cannot trip the fail-closed guard (byte-identical pre-door)."""
        m = make_result_metrics(sl_hit_rate=math.nan)
        assert gate_reasons(m) == ()
        assert passes_gates(m) is True

    def test_above_ceiling_fails_with_the_token(self) -> None:
        m = make_result_metrics(sl_hit_rate=0.51)
        assert gate_reasons(m, max_sl_hit_rate=0.5) == (GATE_SL_HIT_RATE,)
        self._assert_twin_parity(m, 0.5)

    def test_at_ceiling_passes(self) -> None:
        """The door is STRICTLY-above (``>``), so at-ceiling survives."""
        m = make_result_metrics(sl_hit_rate=0.5)
        assert gate_reasons(m, max_sl_hit_rate=0.5) == ()
        assert passes_gates(m, max_sl_hit_rate=0.5) is True

    def test_absent_rate_fails_closed_under_an_active_knob(self) -> None:
        """An active gate never passes a row it cannot judge: ``sl_hit_rate``
        ``None`` (a pre-extension row / counter-less caller) FAILS Door 6."""
        m = make_result_metrics(sl_hit_rate=None)
        assert gate_reasons(m, max_sl_hit_rate=0.5) == (GATE_SL_HIT_RATE,)
        self._assert_twin_parity(m, 0.5)

    def test_non_finite_rate_trips_the_guard_under_an_active_knob(self) -> None:
        """With the knob ON, ``sl_hit_rate`` joins the fail-closed scan — a
        NaN reports the guard token ALONE, never a fabricated door verdict."""
        m = make_result_metrics(sl_hit_rate=math.nan)
        assert gate_reasons(m, max_sl_hit_rate=0.5) == (GATE_NON_FINITE_INPUT,)
        self._assert_twin_parity(m, 0.5)

    def test_door_6_token_is_emitted_last_after_other_doors(self) -> None:
        """Evaluation order: Door 6 is judged LAST, so its token trails."""
        m = make_result_metrics(max_drawdown_pct=99.0, sl_hit_rate=0.9)
        assert gate_reasons(m, max_sl_hit_rate=0.5) == (GATE_MAX_DRAWDOWN, GATE_SL_HIT_RATE)
        self._assert_twin_parity(m, 0.5)


class TestDeterminism:
    def test_same_input_same_output(self) -> None:
        m = make_result_metrics(max_drawdown_pct=45.0, win_rate=0.1, win_loss_ratio=0.1)
        assert gate_reasons(m) == gate_reasons(m)

    def test_reasons_follow_gate_reason_order(self) -> None:
        """Emitted tokens appear in strictly increasing vocabulary order."""
        m = make_result_metrics(
            total_trades=5, winning_trades=2, losing_trades=3, max_consecutive_losses=50
        )
        reasons = gate_reasons(m)
        indices = [GATE_REASON_ORDER.index(token) for token in reasons]
        assert indices == sorted(indices)
        assert len(set(indices)) == len(indices)
