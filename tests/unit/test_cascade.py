"""Unit tests for :mod:`brutex.metrics.cascade` — the Stage-4 orchestrator.

Targets 100% line + branch coverage: a full-pass survivor (exercises every
"keep" branch + composite ranking), a rejection at each of the 10 stages
(every hard gate + each of the 9 filters), the pool-level filter semantics
(SPA reject-all, PBO reject-all/skip-small/skip-short, RW keep-set), and the
edge guards (empty pool, duplicate id, unequal-length pool matrix).
"""

from __future__ import annotations

import math
import statistics

import numpy as np
import numpy.typing as npt
import pytest
from brutex.metrics.cascade import (
    CandidateInput,
    CascadeConfig,
    CascadeReport,
    DsrTrialStats,
    FilterStage,
    _zscore_clamped,
    run_stage4_cascade,
)
from brutex.metrics.pbo import pbo_probability

# pyright: reportPrivateUsage=none


def _null_exit(positions: npt.NDArray[np.intp]) -> float:
    """A null exit-logic: random entries never beat a positive observed Sharpe."""
    return 0.0


def _strong_returns(seed: int, n: int = 32, edge: float = 1.0) -> tuple[float, ...]:
    """A strongly-positive (real-edge) return series → passes SPA / RW."""
    return tuple((np.random.default_rng(seed).standard_normal(n) + edge).tolist())


def _good_lockbox(seed: int) -> tuple[tuple[float, ...], tuple[int, ...]]:
    """A lockbox trade series whose Sharpe clears the (low) training reference."""
    rng = np.random.default_rng(seed)
    returns = tuple((rng.standard_normal(40) * 0.05 + 0.2).tolist())  # high Sharpe
    days = tuple(range(40))
    return returns, days


def _candidate(cid: str, *, seed: int = 0, **overrides: object) -> CandidateInput:
    """A candidate that, with :func:`_pass_config`, survives every stage."""
    lb_returns, lb_days = _good_lockbox(seed + 100)
    base: dict[str, object] = {
        "candidate_id": cid,
        "sharpe_ratio": 2.0,
        "sortino_ratio": 2.4,
        "calmar_ratio": 1.6,
        "profit_factor": 1.8,
        "win_rate": 0.55,
        "win_loss_ratio": 2.5,
        "recovery_factor": 2.1,
        "max_drawdown_pct": 12.0,
        "max_consecutive_losses": 4,
        "total_trades": 600,
        "training_sharpe": 0.1,
        "training_max_drawdown": 5.0,
        "skew": 0.0,
        "kurtosis": 3.0,
        "returns": _strong_returns(seed),
        "lockbox_returns": lb_returns,
        "lockbox_days": lb_days,
        "mc_n_bars": 400,
        "mc_n_entries": 20,
        "mc_evaluate": _null_exit,
    }
    base.update(overrides)
    return CandidateInput(**base)  # type: ignore[arg-type]


def _pass_config(**overrides: object) -> CascadeConfig:
    """A config whose per-candidate thresholds are permissive enough that a
    strong-edge candidate survives every stage; tighten one to force a reject."""
    base: dict[str, object] = {
        "seed": 7,
        "n_resamples": 200,
        "dsr_min": 0.0,
        "cpcv_min_pass_rate": 0.0,
        "cpcv_n_groups": 4,
        "cpcv_n_test_groups": 2,
        "pbo_max": 1.0,
        "pbo_n_splits": 8,
        "wfo_min_pass_rate": 0.0,
        "wfo_train_size": 10,
        "wfo_test_size": 6,
        "wfo_step": 6,
        "lockbox_min_trades": 4,
        "lockbox_default_window_days": 5,
        "mc_n_permutations": 100,
    }
    base.update(overrides)
    return CascadeConfig(**base)  # type: ignore[arg-type]


def test_full_pass_two_survivors_ranked() -> None:
    cfg = _pass_config()
    cands = [_candidate("a", seed=1), _candidate("b", seed=2)]
    rep = run_stage4_cascade(cands, cfg)
    assert isinstance(rep, CascadeReport)
    assert rep.n_input == 2
    assert rep.n_survived == 2
    assert set(rep.survivor_ids) == {"a", "b"}
    for v in rep.verdicts:
        assert v.passed is True
        assert v.rejected_at is None
        assert v.composite_score is not None
    # Survivors are ranked best-first by composite score.
    scores = {v.candidate_id: v.composite_score for v in rep.verdicts}
    assert scores[rep.survivor_ids[0]] >= scores[rep.survivor_ids[1]]  # type: ignore[operator]
    assert rep.stage_survivors[FilterStage.MONTE_CARLO.value] == 2
    # DEC-PBO-SIDECAR-001: the PASS path records the measured pool PBO — pinned
    # against the pure-function oracle over the SAME matrix the filter builds
    # (both candidates survive to #5 in input order; 32 obs, usable = 32).
    expected = pbo_probability(
        [[float(c.returns[t]) for c in cands] for t in range(32)], n_splits=8
    ).pbo
    assert rep.pool_pbo == expected


def test_run_hard_gates_disabled() -> None:
    cfg = _pass_config(run_hard_gates=False)
    # A candidate with junk scalars survives the (disabled) gates and is judged
    # only by the statistical filters.
    cands = [_candidate("a", seed=1), _candidate("b", seed=2, profit_factor=1.0, total_trades=5)]
    rep = run_stage4_cascade(cands, cfg)
    assert rep.stage_survivors[FilterStage.HARD_GATES.value] == 2


@pytest.mark.parametrize(
    ("overrides", "needle"),
    [
        ({"total_trades": 10}, "total_trades"),
        ({"profit_factor": 1.0}, "profit_factor"),
        ({"max_drawdown_pct": 45.0}, "max_drawdown_pct"),
        ({"win_rate": 0.1, "win_loss_ratio": 1.0}, "win_rate"),
        ({"max_consecutive_losses": 20}, "max_consecutive_losses"),
    ],
)
def test_hard_gate_rejections(overrides: dict[str, object], needle: str) -> None:
    cfg = _pass_config()
    rep = run_stage4_cascade([_candidate("x", seed=1, **overrides)], cfg)
    (v,) = rep.verdicts
    assert v.passed is False
    assert v.rejected_at is FilterStage.HARD_GATES
    assert needle in v.detail


def test_dsr_rejection() -> None:
    # A near-zero Sharpe gives a low Deflated Sharpe (~0.5); dsr_min=0.95 rejects
    # (it still clears the hard gates, which don't look at the Sharpe).
    cfg = _pass_config(dsr_min=0.95)
    rep = run_stage4_cascade([_candidate("x", seed=1, sharpe_ratio=0.0)], cfg)
    (v,) = rep.verdicts
    assert v.rejected_at is FilterStage.DEFLATED_SHARPE


def test_cpcv_rejection() -> None:
    cfg = _pass_config(cpcv_min_pass_rate=1.01)  # impossible pass-rate → reject
    rep = run_stage4_cascade([_candidate("x", seed=1)], cfg)
    (v,) = rep.verdicts
    assert v.rejected_at is FilterStage.CPCV


def test_spa_rejects_noise_pool() -> None:
    # A pool of pure noise has no real best → Reality-Check p >= alpha → all out.
    noise = tuple(np.random.default_rng(5).standard_normal(32).tolist())
    cfg = _pass_config()
    rep = run_stage4_cascade([_candidate("x", seed=1, returns=noise)], cfg)
    (v,) = rep.verdicts
    assert v.rejected_at is FilterStage.REALITY_CHECK_SPA
    # DEC-PBO-SIDECAR-001 None-cause (a): the pool is already EMPTY when
    # filter #5 is reached, so no pool PBO was measured.
    assert rep.pool_pbo is None


def test_pbo_rejects_overfit_pool() -> None:
    # pbo_max = -1 makes any PBO probability exceed it → pool rejected at PBO.
    cfg = _pass_config(pbo_max=-1.0)
    cands = [_candidate("a", seed=1), _candidate("b", seed=2)]
    rep = run_stage4_cascade(cands, cfg)
    assert all(v.rejected_at is FilterStage.PBO for v in rep.verdicts)
    # DEC-PBO-SIDECAR-001: the REJECT path also records the measured value —
    # oracle-pinned, and it is the SAME number quoted in the reject detail.
    expected = pbo_probability(
        [[float(c.returns[t]) for c in cands] for t in range(32)], n_splits=8
    ).pbo
    assert rep.pool_pbo == expected
    assert all(f"{expected:.4f}" in v.detail for v in rep.verdicts)


def test_pbo_skipped_for_single_survivor() -> None:
    # One survivor → PBO is skipped (can't rank a pool of one); candidate
    # continues to later stages.
    cfg = _pass_config(pbo_max=-1.0)  # would reject if PBO ran
    rep = run_stage4_cascade([_candidate("solo", seed=1)], cfg)
    (v,) = rep.verdicts
    assert v.rejected_at is not FilterStage.PBO  # PBO was skipped
    assert rep.pool_pbo is None  # None-cause (b): lone survivor


def test_pbo_skipped_for_short_series() -> None:
    # n=16 series with n_splits=50 → usable = (16//50)*50 = 0 → PBO skipped,
    # yet the series is long enough for WFO/CPCV/SPA so the candidates reach
    # (and pass through) the skipped PBO stage rather than being rejected by it.
    cfg = _pass_config(pbo_max=-1.0, pbo_n_splits=50)
    cands = [
        _candidate("a", seed=1, returns=_strong_returns(1, n=16, edge=2.0)),
        _candidate("b", seed=2, returns=_strong_returns(2, n=16, edge=2.0)),
    ]
    rep = run_stage4_cascade(cands, cfg)
    assert all(v.rejected_at is not FilterStage.PBO for v in rep.verdicts)
    assert rep.pool_pbo is None  # None-cause (c): series shorter than one block


def test_romano_wolf_keeps_real_set() -> None:
    # A strong candidate + a near-noise one: RW keeps the real, drops the other.
    weak = tuple((np.random.default_rng(9).standard_normal(32) + 0.02).tolist())
    cfg = _pass_config()
    rep = run_stage4_cascade(
        [
            _candidate("strong", seed=1, returns=_strong_returns(1, edge=2.0)),
            _candidate("weak", seed=2, returns=weak),
        ],
        cfg,
    )
    by = {v.candidate_id: v for v in rep.verdicts}
    # The weak one is dropped no later than RW; the strong one passes RW.
    assert by["weak"].passed is False
    assert by["strong"].rejected_at is not FilterStage.ROMANO_WOLF


def test_wfo_rejection() -> None:
    cfg = _pass_config(wfo_min_pass_rate=1.01)  # impossible → WFO rejects
    rep = run_stage4_cascade([_candidate("x", seed=1)], cfg)
    (v,) = rep.verdicts
    assert v.rejected_at is FilterStage.WALK_FORWARD


def test_lockbox_rejection() -> None:
    # training_sharpe far above what the lockbox series can match → lockbox fails.
    cfg = _pass_config()
    rep = run_stage4_cascade([_candidate("x", seed=1, training_sharpe=99.0)], cfg)
    (v,) = rep.verdicts
    assert v.rejected_at is FilterStage.LOCKBOX


def test_monte_carlo_rejection() -> None:
    # An exit logic that always beats the observed Sharpe → MC p high → reject.
    def always_beats(positions: npt.NDArray[np.intp]) -> float:
        return 1.0e9

    cfg = _pass_config()
    rep = run_stage4_cascade([_candidate("x", seed=1, mc_evaluate=always_beats)], cfg)
    (v,) = rep.verdicts
    assert v.rejected_at is FilterStage.MONTE_CARLO


def test_determinism_same_input() -> None:
    cfg = _pass_config()
    cands = [_candidate("a", seed=1), _candidate("b", seed=2)]
    assert run_stage4_cascade(cands, cfg) == run_stage4_cascade(cands, cfg)


def test_empty_pool() -> None:
    rep = run_stage4_cascade([], _pass_config())
    assert rep.n_input == 0
    assert rep.n_survived == 0
    assert rep.survivor_ids == ()
    assert rep.rejected_ids == ()


def test_duplicate_id_rejected() -> None:
    with pytest.raises(ValueError, match="candidate_id values must be unique"):
        run_stage4_cascade([_candidate("dup", seed=1), _candidate("dup", seed=2)], _pass_config())


def test_unequal_pool_returns_length_raises() -> None:
    # Two survivors with different returns lengths → the pool matrix can't form.
    cfg = _pass_config()
    a = _candidate("a", seed=1, returns=_strong_returns(1, n=32))
    b = _candidate("b", seed=2, returns=_strong_returns(2, n=24))
    with pytest.raises(ValueError, match="share one returns length"):
        run_stage4_cascade([a, b], cfg)


def test_rejected_ids_property() -> None:
    cfg = _pass_config()
    rep = run_stage4_cascade([_candidate("x", seed=1, profit_factor=1.0)], cfg)
    assert rep.rejected_ids == ("x",)


def test_zscore_clamped_helper() -> None:
    assert _zscore_clamped([]) == []
    assert _zscore_clamped([1.0]) == [0.0]  # < 2 values
    assert _zscore_clamped([5.0, 5.0, 5.0]) == [0.0, 0.0, 0.0]  # zero variance
    # A lone outlier among many zeros clamps to +3.0 (z ~ 4.5 before clamping).
    z = _zscore_clamped([0.0] * 20 + [1.0])
    assert max(z) == 3.0


# ── DSR deflation source (ADR-0008 / DEC-TRIAL-STATS-CONSUME-001) ──


class TestDsrTrialStats:
    def test_valid_constructs(self) -> None:
        stats = DsrTrialStats(n_trials=10, sharpe_variance=0.25, n_finite=8)
        assert stats.n_trials == 10
        assert stats.sharpe_variance == 0.25
        assert stats.n_finite == 8

    def test_n_trials_below_one_raises(self) -> None:
        with pytest.raises(ValueError, match="n_trials must be >= 1"):
            DsrTrialStats(n_trials=0, sharpe_variance=0.25, n_finite=0)

    @pytest.mark.parametrize("n_finite", [-1, 11])
    def test_n_finite_out_of_range_raises(self, n_finite: int) -> None:
        with pytest.raises(ValueError, match=r"n_finite must be in \[0, n_trials\]"):
            DsrTrialStats(n_trials=10, sharpe_variance=0.25, n_finite=n_finite)

    @pytest.mark.parametrize("variance", [-0.1, math.nan, math.inf])
    def test_bad_variance_raises(self, variance: float) -> None:
        with pytest.raises(ValueError, match="sharpe_variance must be finite"):
            DsrTrialStats(n_trials=10, sharpe_variance=variance, n_finite=8)


def test_report_defaults_stamp_pool_source() -> None:
    # A report built without the new fields (the pre-ADR-0008 construction
    # shape) reads as the pool-source legacy state, never as "leaderboard".
    rep = CascadeReport(verdicts=(), survivor_ids=(), n_input=0, n_survived=0, stage_survivors={})
    assert rep.dsr_n_source == "pool"
    assert rep.dsr_n_trials is None
    assert rep.dsr_trial_variance is None


def test_pool_fallback_stamps_pool_and_echoes_n_v() -> None:
    # Without trial_stats the report stamps the legacy pool source and echoes
    # the exact N and V filter #2 deflated against (full precision).
    cands = [_candidate("a", seed=1), _candidate("b", seed=2, sharpe_ratio=1.0)]
    rep = run_stage4_cascade(cands, _pass_config())
    assert rep.dsr_n_source == "pool"
    assert rep.dsr_n_trials == 2
    assert rep.dsr_trial_variance == statistics.pvariance([2.0, 1.0])


def test_single_candidate_pool_dispersion_is_zero() -> None:
    # The < _MIN_FOR_DISPERSION arm: a pool of one has no dispersion estimate.
    rep = run_stage4_cascade([_candidate("a", seed=1)], _pass_config())
    assert rep.dsr_n_source == "pool"
    assert rep.dsr_n_trials == 1
    assert rep.dsr_trial_variance == 0.0


def test_trial_stats_stamp_leaderboard_and_echo() -> None:
    stats = DsrTrialStats(n_trials=500, sharpe_variance=0.25, n_finite=480)
    rep = run_stage4_cascade([_candidate("a", seed=1)], _pass_config(), trial_stats=stats)
    assert rep.dsr_n_source == "leaderboard"
    assert rep.dsr_n_trials == 500
    assert rep.dsr_trial_variance == 0.25


def test_leaderboard_n_supersedes_the_laxer_pool_n() -> None:
    # THE defect ADR-0008 fixed: a lone finalist deflated against pool-N=1
    # sails through filter #2; the SAME candidate deflated against the true
    # leaderboard population (large N, real V) is rejected. Condition 7: the
    # larger-N deflation is strictly stricter — no alpha is double-spent.
    cfg = _pass_config(dsr_min=0.95)
    cand = _candidate("x", seed=1)
    (pool_verdict,) = run_stage4_cascade([cand], cfg).verdicts
    assert pool_verdict.rejected_at is not FilterStage.DEFLATED_SHARPE
    big = DsrTrialStats(n_trials=1_000_000, sharpe_variance=1.0, n_finite=1_000_000)
    (lb_verdict,) = run_stage4_cascade([cand], cfg, trial_stats=big).verdicts
    assert lb_verdict.rejected_at is FilterStage.DEFLATED_SHARPE


def test_dsr_is_monotone_in_n_trials() -> None:
    # ADR-0008 condition 9: N↑ at fixed V>0 ⇒ DSR↓ ⇒ never more survivors.
    cfg = _pass_config(dsr_min=0.95)
    cand = _candidate("x", seed=1)
    small = DsrTrialStats(n_trials=2, sharpe_variance=1.0, n_finite=2)
    large = DsrTrialStats(n_trials=10_000_000, sharpe_variance=1.0, n_finite=10_000_000)
    rep_small = run_stage4_cascade([cand], cfg, trial_stats=small)
    rep_large = run_stage4_cascade([cand], cfg, trial_stats=large)
    assert (
        rep_large.stage_survivors[FilterStage.DEFLATED_SHARPE.value]
        <= rep_small.stage_survivors[FilterStage.DEFLATED_SHARPE.value]
    )


# ── CandidateInput construction-time validation (no NaN/inf, non-empty id) ──


def test_candidate_input_rejects_empty_id() -> None:
    with pytest.raises(ValueError, match="candidate_id must be a non-empty string"):
        _candidate("")


def test_candidate_input_rejects_non_finite_metric() -> None:
    with pytest.raises(ValueError, match="calmar_ratio must be finite"):
        _candidate("x", calmar_ratio=math.inf)


def test_candidate_input_rejects_non_finite_returns() -> None:
    with pytest.raises(ValueError, match="returns must be all-finite"):
        _candidate("x", returns=(1.0, math.nan, 2.0))


# ── Degenerate per-candidate inputs are isolated as rejections, never crashes ──


def test_spa_rejects_degenerate_constant_returns() -> None:
    # A constant excess-return series has zero variance: it passes CPCV (Sharpe
    # is undefined ⇒ pass-rate 0.0, not < 0.0) and reaches SPA, where it is
    # rejected as degenerate — rather than crashing Romano-Wolf downstream.
    cfg = _pass_config()
    rep = run_stage4_cascade([_candidate("flat", seed=1, returns=tuple([1.0] * 32))], cfg)
    (v,) = rep.verdicts
    assert v.rejected_at is FilterStage.REALITY_CHECK_SPA
    assert "degenerate" in v.detail


def test_dsr_input_error_rejects_candidate() -> None:
    # An out-of-domain (skew, kurtosis, Sharpe) makes the DSR variance radicand
    # negative ⇒ the engine raises; the cascade isolates it as a DSR rejection.
    cfg = _pass_config(dsr_min=0.0)
    rep = run_stage4_cascade([_candidate("x", seed=1, skew=5.0)], cfg)
    (v,) = rep.verdicts
    assert v.rejected_at is FilterStage.DEFLATED_SHARPE
    assert "could not be evaluated" in v.detail


def test_cpcv_input_error_rejects_candidate() -> None:
    # A returns series shorter than cpcv_n_groups makes CPCV raise ⇒ rejection.
    cfg = _pass_config()  # cpcv_n_groups=4
    rep = run_stage4_cascade([_candidate("x", seed=1, returns=_strong_returns(1, n=3))], cfg)
    (v,) = rep.verdicts
    assert v.rejected_at is FilterStage.CPCV
    assert "could not be evaluated" in v.detail


def test_wfo_input_error_rejects_candidate() -> None:
    # Returns long enough for CPCV/SPA/RW but too short for a large WFO window
    # ⇒ "no walks" ValueError ⇒ isolated as a Walk-Forward rejection.
    cfg = _pass_config(wfo_train_size=50, wfo_test_size=20, wfo_step=20)
    rep = run_stage4_cascade(
        [_candidate("x", seed=1, returns=_strong_returns(1, n=20, edge=2.0))], cfg
    )
    (v,) = rep.verdicts
    assert v.rejected_at is FilterStage.WALK_FORWARD
    assert "could not be evaluated" in v.detail


def test_lockbox_input_error_rejects_candidate() -> None:
    # Mismatched lockbox returns/days length makes the lockbox engine raise.
    cfg = _pass_config()
    rep = run_stage4_cascade(
        [_candidate("x", seed=1, lockbox_returns=(0.1, 0.2, 0.3), lockbox_days=(0, 1))], cfg
    )
    (v,) = rep.verdicts
    assert v.rejected_at is FilterStage.LOCKBOX
    assert "could not be evaluated" in v.detail


def test_monte_carlo_input_error_rejects_candidate() -> None:
    # mc_n_entries > mc_n_bars is out of the engine's domain ⇒ MC rejection.
    cfg = _pass_config()
    rep = run_stage4_cascade([_candidate("x", seed=1, mc_n_bars=10, mc_n_entries=100)], cfg)
    (v,) = rep.verdicts
    assert v.rejected_at is FilterStage.MONTE_CARLO
    assert "could not be evaluated" in v.detail
