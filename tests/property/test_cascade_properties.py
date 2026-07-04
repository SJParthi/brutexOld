"""Hypothesis property tests for :mod:`brutex.metrics.cascade` (Stage-4 orchestrator).

Structural invariants that must hold for *every* candidate pool, plus
determinism (Golden Rule #4), complementing the per-branch unit tests and the
byte-exact golden fixture.
"""

from __future__ import annotations

import math

import numpy as np
import numpy.typing as npt
from brutex.metrics.cascade import (
    CandidateInput,
    CascadeConfig,
    DsrTrialStats,
    run_stage4_cascade,
)
from hypothesis import given, settings
from hypothesis import strategies as st

_RETURNS_LEN = 24  # multiple of the test pbo_n_splits=8; long enough for WFO


def _null_exit(positions: npt.NDArray[np.intp]) -> float:
    return 0.0


_CONFIG = CascadeConfig(
    seed=11,
    n_resamples=120,
    dsr_min=0.0,
    cpcv_min_pass_rate=0.0,
    cpcv_n_groups=4,
    cpcv_n_test_groups=2,
    pbo_max=1.0,
    pbo_n_splits=8,
    wfo_min_pass_rate=0.0,
    wfo_train_size=10,
    wfo_test_size=6,
    wfo_step=6,
    lockbox_min_trades=4,
    lockbox_default_window_days=5,
    mc_n_permutations=80,
)


@st.composite
def _pool(draw: st.DrawFn) -> list[CandidateInput]:
    """Draw a small pool of candidates sharing one returns length."""
    n = draw(st.integers(min_value=1, max_value=4))
    out: list[CandidateInput] = []
    for i in range(n):
        seed = draw(st.integers(min_value=0, max_value=2**31 - 1))
        edge = draw(st.floats(min_value=-1.0, max_value=2.0, allow_nan=False))
        rng = np.random.default_rng(seed)
        returns = tuple((rng.standard_normal(_RETURNS_LEN) + edge).tolist())
        lb = tuple((rng.standard_normal(20) * 0.05 + 0.1).tolist())
        out.append(
            CandidateInput(
                candidate_id=f"c{i}",
                sharpe_ratio=draw(st.floats(0.0, 3.0, allow_nan=False)),
                sortino_ratio=draw(st.floats(0.0, 3.0, allow_nan=False)),
                calmar_ratio=draw(st.floats(0.0, 3.0, allow_nan=False)),
                profit_factor=draw(st.floats(1.0, 3.0, allow_nan=False)),
                win_rate=draw(st.floats(0.2, 0.8, allow_nan=False)),
                win_loss_ratio=draw(st.floats(0.5, 4.0, allow_nan=False)),
                recovery_factor=draw(st.floats(0.0, 5.0, allow_nan=False)),
                max_drawdown_pct=draw(st.floats(0.0, 60.0, allow_nan=False)),
                max_consecutive_losses=draw(st.integers(0, 20)),
                total_trades=draw(st.integers(0, 1000)),
                training_sharpe=draw(st.floats(-1.0, 1.0, allow_nan=False)),
                training_max_drawdown=draw(st.floats(0.0, 10.0, allow_nan=False)),
                skew=0.0,
                kurtosis=3.0,
                returns=returns,
                lockbox_returns=lb,
                lockbox_days=tuple(range(20)),
                mc_n_bars=200,
                mc_n_entries=10,
                mc_evaluate=_null_exit,
            )
        )
    return out


@given(pool=_pool())
@settings(max_examples=25, deadline=None)
def test_cascade_structural_invariants(pool: list[CandidateInput]) -> None:
    rep = run_stage4_cascade(pool, _CONFIG)

    # One verdict per input candidate, ids preserved.
    assert len(rep.verdicts) == len(pool) == rep.n_input
    assert {v.candidate_id for v in rep.verdicts} == {c.candidate_id for c in pool}

    passed = [v for v in rep.verdicts if v.passed]
    rejected = [v for v in rep.verdicts if not v.passed]
    # passed XOR rejected_at; survivor bookkeeping consistent.
    for v in passed:
        assert v.rejected_at is None and v.composite_score is not None
    for v in rejected:
        assert v.rejected_at is not None and v.composite_score is None
    assert rep.n_survived == len(passed) == len(rep.survivor_ids)
    assert set(rep.survivor_ids) == {v.candidate_id for v in passed}
    assert set(rep.rejected_ids) == {v.candidate_id for v in rejected}

    # Survivors are ranked in the STRICT total order (DEC-STATS-GATE-
    # INTEGRITY-001, R2-SR-F6): score descending, candidate_id ascending on a
    # tie — for consecutive survivors, score[i] > score[i+1] OR (equal AND
    # id[i] < id[i+1]). Every composite is finite (the sort key's contract).
    scores = {v.candidate_id: v.composite_score for v in passed if v.composite_score is not None}
    assert all(math.isfinite(s) for s in scores.values())
    for earlier, later in zip(rep.survivor_ids, rep.survivor_ids[1:], strict=False):
        assert scores[earlier] > scores[later] or (
            scores[earlier] == scores[later] and earlier < later
        )

    # Pool-fallback provisional invariant (R2-SR-F4): with NO trial_stats the
    # DSR deflates against the pool ⇒ every verdict's provisional flag equals
    # its passed flag (pass-scoped, run-level source).
    assert rep.dsr_n_source == "pool"
    for v in rep.verdicts:
        assert v.provisional == v.passed
    assert rep.provisional_pass_ids == rep.survivor_ids

    # Stage-survivor counts are monotonically non-increasing down the pipeline.
    counts = list(rep.stage_survivors.values())
    assert all(counts[i] >= counts[i + 1] for i in range(len(counts) - 1))


@given(pool=_pool())
@settings(max_examples=15, deadline=None)
def test_cascade_trial_stats_passes_never_provisional(pool: list[CandidateInput]) -> None:
    # With frozen leaderboard trial stats supplied, NO verdict is provisional
    # (the leaderboard N is the honest deflation population).
    stats = DsrTrialStats(n_trials=50, sharpe_variance=0.25, n_finite=50)
    rep = run_stage4_cascade(pool, _CONFIG, trial_stats=stats)
    assert rep.dsr_n_source == "leaderboard"
    assert all(v.provisional is False for v in rep.verdicts)
    assert rep.provisional_pass_ids == ()


@given(pool=_pool())
@settings(max_examples=15, deadline=None)
def test_cascade_determinism(pool: list[CandidateInput]) -> None:
    assert run_stage4_cascade(pool, _CONFIG) == run_stage4_cascade(pool, _CONFIG)
