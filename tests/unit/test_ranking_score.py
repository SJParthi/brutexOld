"""Known-value unit tests for :mod:`brutex.ranking.score`.

A small leaderboard with hand-computed z-scores and composite values
pins :func:`~brutex.ranking.score.rank_leaderboard` (spec lines
602-618), including ties, the ``std == 0`` rule, the lockbox
present-vs-absent conventions, and the ``None``-metric convention.
"""

from __future__ import annotations

import math
from dataclasses import replace

from brutex.metrics import ResultMetrics
from brutex.ranking.gates import RejectionReason
from brutex.ranking.score import (
    CLAMP_HI,
    CLAMP_LO,
    RankInput,
    rank_leaderboard,
)

from tests.unit.test_ranking_gates import _GOOD


def _mk(
    result_id: str,
    *,
    sharpe: float | None = 1.5,
    sortino: float | None = 2.0,
    calmar: float | None = 1.0,
    profit_factor: float | None = 4.0,
    recovery_factor: float | None = 8.0,
    win_rate: float = 0.60,
    win_loss_ratio: float | None = 2.64,
    lockbox: float | None = None,
) -> RankInput:
    """Build a gate-passing :class:`RankInput` with chosen scored fields."""
    metrics: ResultMetrics = replace(
        _GOOD,
        sharpe_ratio=sharpe,
        sortino_ratio=sortino,
        calmar_ratio=calmar,
        profit_factor=profit_factor,
        recovery_factor=recovery_factor,
        win_rate=win_rate,
        win_loss_ratio=win_loss_ratio,
    )
    return RankInput(result_id=result_id, metrics=metrics, lockbox_sharpe=lockbox)


def test_empty_leaderboard_returns_empty() -> None:
    """No entries → no ranked results."""
    assert rank_leaderboard([]) == []


def test_all_rejected_returns_empty() -> None:
    """When every entry fails a gate, nothing survives."""
    losers = [_mk("a", profit_factor=1.0), _mk("b", profit_factor=1.0)]
    assert rank_leaderboard(losers) == []


def test_single_survivor_all_zscores_zero() -> None:
    """A lone survivor: every column has std 0 → every term z-score 0.

    Composite therefore 0.0, rank 1 (spec: std==0 → 0 for all).
    """
    ranked = rank_leaderboard([_mk("solo")])
    assert len(ranked) == 1
    assert ranked[0].rank == 1
    assert ranked[0].composite_score == 0.0
    assert all(v == 0.0 for v in ranked[0].normalized_terms.values())
    assert ranked[0].rejection.passed is True


def test_two_results_only_sharpe_differs_hand_computed() -> None:
    """Two survivors differing ONLY in sharpe; verify exact composite.

    Both share every other scored column (std 0 → those terms = 0). The
    sharpe column is ``[2.0, 1.0]``: mean 1.5, population std 0.5, so
    z-scores are ``+1.0`` and ``-1.0`` (within the [-3,+3] clamp). With
    NO lockbox the six base weights renormalize by ``/0.95``; only the
    sharpe term is non-zero, so composite = ``(0.25/0.95) × (±1.0)``.
    """
    a = _mk("a", sharpe=2.0)
    b = _mk("b", sharpe=1.0)
    ranked = rank_leaderboard([b, a])  # deliberately unsorted input

    expected_top = (0.25 / 0.95) * 1.0
    expected_bottom = (0.25 / 0.95) * -1.0

    assert ranked[0].result_id == "a"
    assert ranked[0].rank == 1
    assert math.isclose(ranked[0].composite_score, expected_top)
    assert math.isclose(ranked[0].normalized_terms["sharpe"], 1.0)

    assert ranked[1].result_id == "b"
    assert ranked[1].rank == 2
    assert math.isclose(ranked[1].composite_score, expected_bottom)
    assert math.isclose(ranked[1].normalized_terms["sharpe"], -1.0)


def test_no_lockbox_drops_term_and_renormalizes() -> None:
    """Pre-Stage-4: the lockbox term is absent and weights sum to 1.0."""
    ranked = rank_leaderboard([_mk("a", sharpe=2.0), _mk("b", sharpe=1.0)])
    # No "lockbox" key in the normalized-terms map.
    assert "lockbox" not in ranked[0].normalized_terms
    # The six base term keys are present.
    assert set(ranked[0].normalized_terms) == {
        "sharpe",
        "sortino",
        "calmar",
        "profit_factor",
        "recovery_factor",
        "win_quality",
    }


def test_lockbox_present_uses_seven_terms() -> None:
    """When a survivor carries lockbox, the 7-term formula is used.

    Two survivors share every base column (all z 0) and differ ONLY in
    lockbox_sharpe ``[1.2, 1.0]``: but lockbox also feeds the overfit
    gate. With sharpe 1.5, lockbox 1.0 → ratio 0.667 ≥ 0.5 (passes);
    1.2 → 0.8 (passes). Lockbox column ``[1.2, 1.0]`` → mean 1.1, std
    0.1 → z ``+1.0`` / ``-1.0``; composite = ``0.05 × (±1.0)``.
    """
    a = _mk("a", lockbox=1.2)
    b = _mk("b", lockbox=1.0)
    ranked = rank_leaderboard([a, b])

    assert "lockbox" in ranked[0].normalized_terms
    assert ranked[0].result_id == "a"
    assert math.isclose(ranked[0].normalized_terms["lockbox"], 1.0)
    assert math.isclose(ranked[0].composite_score, 0.05 * 1.0)
    assert math.isclose(ranked[1].composite_score, 0.05 * -1.0)


def test_lockbox_partial_missing_normalizes_present_only() -> None:
    """When only SOME survivors have lockbox, missing ones get worst clamp.

    Lockbox active (one survivor has it). Survivor "a" has lockbox 1.0;
    "b" has None. The lockbox column is ``[1.0, None]`` → only "a"
    defines it (std 0 over one value) → "a" normalizes to 0.0, "b" gets
    the worst clamp ``-3.0`` on the lockbox term.
    """
    a = _mk("a", lockbox=1.0)
    b = _mk("b", lockbox=None)
    ranked = rank_leaderboard([a, b])
    terms = {r.result_id: r.normalized_terms for r in ranked}
    assert terms["a"]["lockbox"] == 0.0
    assert terms["b"]["lockbox"] == CLAMP_LO


def test_none_scored_metric_gets_worst_clamp() -> None:
    """A None in a scored metric → that term z-score is the worst clamp.

    Two survivors: "a" has a defined recovery_factor (8.0); "b" has
    None. The recovery column ``[8.0, None]`` → "a" defines it (std 0
    over one) → 0.0; "b" → ``-3.0``. So "a" outranks "b".
    """
    a = _mk("a", recovery_factor=8.0)
    b = _mk("b", recovery_factor=None)
    ranked = rank_leaderboard([a, b])
    terms = {r.result_id: r.normalized_terms for r in ranked}
    assert terms["a"]["recovery_factor"] == 0.0
    assert terms["b"]["recovery_factor"] == CLAMP_LO
    assert ranked[0].result_id == "a"


def test_clamp_bounds_applied() -> None:
    """An extreme outlier z-score is clamped to ``[-3, +3]`` (spec line 618).

    One huge sharpe among twelve identical low ones: the outlier's raw
    z-score (``sqrt(n-1) = sqrt(11) ≈ 3.32``) exceeds +3 and is clamped;
    each low value sits below 0 but above -3.
    """
    entries = [_mk("hi", sharpe=1_000.0)]
    entries += [_mk(f"lo{i}", sharpe=1.0) for i in range(11)]
    ranked = rank_leaderboard(entries)
    terms = {r.result_id: r.normalized_terms for r in ranked}
    assert terms["hi"]["sharpe"] == CLAMP_HI
    assert all(CLAMP_LO <= terms[f"lo{i}"]["sharpe"] <= 0.0 for i in range(11))
    assert terms["lo0"]["sharpe"] == terms["lo1"]["sharpe"]


def test_ties_broken_by_result_id() -> None:
    """Identical composites tie-break deterministically by result_id asc."""
    # Two identical survivors (every column std 0 → composite 0.0 each).
    ranked = rank_leaderboard([_mk("zeta"), _mk("alpha")])
    assert ranked[0].composite_score == ranked[1].composite_score
    assert ranked[0].result_id == "alpha"
    assert ranked[1].result_id == "zeta"
    assert [r.rank for r in ranked] == [1, 2]


def test_rejected_entries_excluded_survivors_ranked() -> None:
    """Gate failures are dropped; only survivors are scored and ranked."""
    good_a = _mk("a", sharpe=2.0)
    good_b = _mk("b", sharpe=1.0)
    bad = RankInput(
        result_id="bad",
        metrics=replace(_GOOD, total_trades=5),
        lockbox_sharpe=None,
    )
    ranked = rank_leaderboard([good_a, good_b, bad])
    ids = [r.result_id for r in ranked]
    assert "bad" not in ids
    assert ids == ["a", "b"]


def test_column_all_none_contributes_zero() -> None:
    """A metric undefined for EVERY survivor contributes 0 to all of them.

    Both survivors have ``win_loss_ratio=None`` (no losers) and
    ``profit_factor=None``, so the win_quality AND profit_factor columns
    are entirely ``None``: no information, every entry's term is 0.0 (the
    ``not defined`` branch). The two are then ranked solely on the
    differing sharpe.
    """
    a = _mk("a", sharpe=2.0, profit_factor=None, win_loss_ratio=None)
    b = _mk("b", sharpe=1.0, profit_factor=None, win_loss_ratio=None)
    ranked = rank_leaderboard([a, b])
    terms = {r.result_id: r.normalized_terms for r in ranked}
    # Both win_quality terms are 0.0 (column all-None → no info).
    assert terms["a"]["win_quality"] == 0.0
    assert terms["b"]["win_quality"] == 0.0
    assert terms["a"]["profit_factor"] == 0.0
    assert terms["b"]["profit_factor"] == 0.0
    # Sharpe still separates them.
    assert ranked[0].result_id == "a"


def test_none_with_nonzero_variance_gets_worst_clamp() -> None:
    """A None entry in a column with spread among defined values → -3.

    Three survivors' sharpe column is ``[2.0, 1.0, None]``: the two
    defined values give a non-zero variance (so the general branch runs),
    and the None entry takes the worst clamp ``-3.0`` while the defined
    ones get real z-scores.
    """
    a = _mk("a", sharpe=2.0)
    b = _mk("b", sharpe=1.0)
    c = _mk("c", sharpe=None)
    ranked = rank_leaderboard([a, b, c])
    terms = {r.result_id: r.normalized_terms for r in ranked}
    assert terms["c"]["sharpe"] == CLAMP_LO
    # a and b have the +1/-1 z-scores of a two-point spread.
    assert math.isclose(terms["a"]["sharpe"], 1.0)
    assert math.isclose(terms["b"]["sharpe"], -1.0)
    # c (worst on sharpe, the heaviest weight) ranks last.
    assert ranked[-1].result_id == "c"


def test_survivor_carries_passing_rejection() -> None:
    """Each survivor's rejection result is the passing one (NONE)."""
    ranked = rank_leaderboard([_mk("a")])
    assert ranked[0].rejection.reason is RejectionReason.NONE
    assert ranked[0].rejection.passed is True
