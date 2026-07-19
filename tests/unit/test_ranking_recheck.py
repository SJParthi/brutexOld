"""Unit tests for :func:`brutex.ranking.persisted_hard_gates_recheck` (DEC-RESULTS-TRUST-HONESTY-001).

The /results serve-time belt (PR-B commit B2a): re-judges the hard doors
reconstructible from a stored ``ResultRow``'s persisted scalars — the
fail-closed non-finite guard + doors 1-3 — reading the SAME threshold
constants as :func:`brutex.ranking.passes_gates` (one home, no drift). The
verdict vocabulary is the two-state MARK-not-drop wire pair ``"ok"`` /
``"marked"``; the one-way law (``"marked"`` ⇒ the full ``passes_gates``
rejects; ``"ok"`` is necessary-not-sufficient) is pinned here explicitly.
"""

from __future__ import annotations

import math

import pytest
from brutex.metrics.compute import ResultMetrics
from brutex.ranking import (
    HARD_GATES_RECHECK_MARKED,
    HARD_GATES_RECHECK_OK,
    MIN_TRADES,
    passes_gates,
    persisted_hard_gates_recheck,
)

from tests.fakes.metrics_fixtures import make_result_metrics


def _recheck_of(m: ResultMetrics) -> str:
    """Project the persisted-field view of *m* into the recheck (the view's call shape)."""
    return persisted_hard_gates_recheck(
        total_trades=m.total_trades,
        profit_factor=m.profit_factor,
        max_drawdown_pct=m.max_drawdown_pct,
        win_rate=m.win_rate,
    )


class TestVocabulary:
    def test_wire_tokens_pinned(self) -> None:
        """The /results ``hard_gates_recheck`` key serves these EXACT strings."""
        assert HARD_GATES_RECHECK_OK == "ok"
        assert HARD_GATES_RECHECK_MARKED == "marked"


class TestVerdicts:
    def test_gate_passer_reads_ok(self) -> None:
        assert _recheck_of(make_result_metrics()) == HARD_GATES_RECHECK_OK

    @pytest.mark.parametrize(
        "overrides",
        [
            {"total_trades": 29, "winning_trades": 15, "losing_trades": 14},  # door 1
            {"profit_factor": None},  # door 2 (undefined = fail, the passes_gates law)
            {"profit_factor": 1.19},  # door 2
            {"max_drawdown_pct": 30.01},  # door 3
            {"profit_factor": math.nan},  # fail-closed guard
            {"max_drawdown_pct": math.inf},  # fail-closed guard
            {"win_rate": math.nan},  # fail-closed guard (win_rate is guard-only)
        ],
    )
    def test_persisted_door_failures_read_marked(self, overrides: dict[str, object]) -> None:
        m = make_result_metrics(**overrides)
        assert _recheck_of(m) == HARD_GATES_RECHECK_MARKED
        # The one-way law: a MARKED row would also fail the full rank-time gate.
        assert passes_gates(m) is False

    @pytest.mark.parametrize(
        "overrides",
        [
            {"total_trades": MIN_TRADES, "winning_trades": 20, "losing_trades": 10},
            {"profit_factor": 1.2},  # exactly at the floor passes (>= law)
            {"max_drawdown_pct": 30.0},  # exactly at the cap passes (strict > law)
        ],
    )
    def test_exact_boundaries_read_ok(self, overrides: dict[str, object]) -> None:
        """Boundary-exact values pass — the mutant-killing twins of passes_gates'
        own boundary pins (>=/</> flips fail these)."""
        assert _recheck_of(make_result_metrics(**overrides)) == HARD_GATES_RECHECK_OK


class TestHonestOneWayEnvelope:
    def test_ok_is_necessary_not_sufficient(self) -> None:
        """A combo failing ONLY the unpersisted doors (win-rate × wlr here) reads
        "ok" on the recheck while the full gate rejects — the documented
        one-way envelope (doors 4-5 need fields a ResultRow does not persist;
        judging them would fabricate verdicts)."""
        m = make_result_metrics(win_rate=0.10, win_loss_ratio=0.5)
        assert _recheck_of(m) == HARD_GATES_RECHECK_OK
        assert passes_gates(m) is False

    def test_nan_wlr_is_invisible_to_the_recheck(self) -> None:
        """A non-finite UNPERSISTED input trips the full gate's guard but is
        structurally invisible here — "ok" stays honest (necessary-only)."""
        m = make_result_metrics(win_loss_ratio=math.nan)
        assert _recheck_of(m) == HARD_GATES_RECHECK_OK
        assert passes_gates(m) is False
