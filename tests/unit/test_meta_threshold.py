"""Unit gate for :mod:`brutex.meta.threshold` (DEC-META-LABEL-ANNOTATE-001).

The fixed τ grid pin, the EXACT ``>=`` keep boundary, the never-fabricate
``None`` laws (precision at zero kept; kept_pct with zero scored), and the
monotone-shrink invariant.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from brutex.meta.threshold import N_THRESHOLDS_SWEPT, TAU_GRID, tau_curve


class TestGrid:
    def test_grid_is_the_pinned_ten_thresholds(self) -> None:
        assert TAU_GRID == (0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95)
        assert N_THRESHOLDS_SWEPT == 10

    def test_kept_pct_echo_taus_are_grid_members(self) -> None:
        # The annotate sidecar echoes kept_pct at 0.6/0.7/0.8 — all of which
        # must be EXACT grid members (float-equality lookups downstream).
        for tau in (0.6, 0.7, 0.8):
            assert tau in TAU_GRID


class TestCurve:
    def test_length_mismatch_refuses(self) -> None:
        with pytest.raises(ValueError, match="length"):
            tau_curve([0.5], [True, False])

    def test_empty_stream_reads_all_null(self) -> None:
        points = tau_curve([], [])
        assert len(points) == N_THRESHOLDS_SWEPT
        for point in points:
            assert point.n_kept == 0
            assert point.kept_pct is None
            assert point.precision is None

    def test_all_unscored_reads_all_null(self) -> None:
        points = tau_curve([None, None], [True, False])
        for point in points:
            assert point.n_kept == 0
            assert point.kept_pct is None  # n_scored == 0 — no denominator
            assert point.precision is None

    def test_keep_boundary_is_exact_gte(self) -> None:
        # p == τ KEEPS (the abstention gate's pinned >= law).
        points = tau_curve([0.7], [True])
        by_tau = {p.tau: p for p in points}
        assert by_tau[0.7].n_kept == 1
        assert by_tau[0.75].n_kept == 0

    def test_precision_and_kept_pct(self) -> None:
        p_win = [0.9, 0.9, 0.55, None]
        wins = [True, False, True, True]
        by_tau = {p.tau: p for p in tau_curve(p_win, wins)}
        # τ=0.5: all 3 scored kept; 2 wins → precision 2/3.
        assert by_tau[0.5].n_kept == 3
        assert by_tau[0.5].kept_pct == 1.0
        assert by_tau[0.5].precision == pytest.approx(2 / 3)
        # τ=0.6: the 0.55 drops; unscored NEVER in the denominator.
        assert by_tau[0.6].n_kept == 2
        assert by_tau[0.6].kept_pct == pytest.approx(2 / 3)
        assert by_tau[0.6].precision == pytest.approx(1 / 2)
        # τ=0.95: nothing kept — precision is None, NEVER a fabricated 0.
        assert by_tau[0.95].n_kept == 0
        assert by_tau[0.95].precision is None
        assert by_tau[0.95].kept_pct == 0.0

    def test_kept_is_monotone_non_increasing_in_tau(self) -> None:
        p_win = [i / 20 for i in range(20)]
        wins = [i % 2 == 0 for i in range(20)]
        points = tau_curve(p_win, wins)
        kept = [p.n_kept for p in points]
        assert kept == sorted(kept, reverse=True)


class TestMutationWorkflowWiring:
    """Pin this module INTO ``mutation.yml``'s per-module zero-survivor loop.

    The detectors_rust twin (#1797 pattern): the module:suite pair sits in
    the workflow's per-module ``mutmut`` loop with the EMPTY third
    (timeout) field — this deterministic pin fails if the pair is ever
    dropped. HONEST: it pins the WIRING only; the weekly run executes on
    its own schedule.
    """

    _WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "mutation.yml"

    def test_module_paired_with_this_suite_in_the_mutmut_loop(self) -> None:
        text = self._WORKFLOW.read_text(encoding="utf-8")
        assert '"brutex/meta/threshold.py:tests/unit/test_meta_threshold.py:"' in text, (
            "threshold.py is not wired into mutation.yml's per-module mutmut loop"
        )
