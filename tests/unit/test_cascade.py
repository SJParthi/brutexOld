"""Unit tests for :mod:`brutex.metrics.cascade` — the Stage-4 orchestrator.

Targets 100% line + branch coverage: a full-pass survivor (exercises every
"keep" branch + composite ranking), a rejection at each of the 10 stages
(every hard gate + each of the 9 filters), the pool-level filter semantics
(SPA reject-all, PBO reject-all/skip-small/skip-short, RW keep-set), and the
edge guards (empty pool, duplicate id, unequal-length pool matrix).
"""

from __future__ import annotations

import math
import random
import statistics

import numpy as np
import numpy.typing as npt
import pytest
from brutex.metrics.cascade import (
    _COMPOSITE_WEIGHTS,
    CandidateInput,
    CascadeConfig,
    CascadeReport,
    DsrTrialStats,
    FilterStage,
    _measured_lockbox_sharpe,
    _zscore_clamped,
    run_stage4_cascade,
)
from brutex.metrics.lockbox import (
    LOCKBOX_DEFAULT_MIN_TRADES,
    LOCKBOX_DEFAULT_WINDOW_DAYS,
    LockboxResult,
    LockboxStatus,
    lockbox_evaluation,
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


# ── Trade-evidence lockbox count gate (DEC-STATS-GATE-INTEGRITY-001, R2-SR-F2) ──


def test_lockbox_evidence_makes_insufficient_n_reachable_on_bar_series() -> None:
    # THE F2 regression (fails before the change): the candidate's 40-element
    # per-bar lockbox series clears min_trades=4 by BAR count, so the legacy
    # count gate could never fire; with only 2 TRUE trades of evidence the
    # candidate is now honestly rejected INSUFFICIENT_N at the lockbox stage.
    cfg = _pass_config()
    without_evidence = run_stage4_cascade([_candidate("x", seed=1)], cfg)
    (v0,) = without_evidence.verdicts
    assert v0.rejected_at is not FilterStage.LOCKBOX  # bars counted — unreachable before
    with_evidence = run_stage4_cascade([_candidate("x", seed=1, lockbox_trade_days=(36, 38))], cfg)
    (v1,) = with_evidence.verdicts
    assert v1.rejected_at is FilterStage.LOCKBOX
    assert LockboxStatus.INSUFFICIENT_N.value in v1.detail


def test_lockbox_evidence_window_extension_driven_by_trade_count() -> None:
    # 4 trades, only 2 in the 5-day default window ⇒ the window extends back
    # to the 4th-most-recent TRADE's day and the candidate still passes.
    cfg = _pass_config()
    rep = run_stage4_cascade([_candidate("x", seed=1, lockbox_trade_days=(10, 20, 36, 38))], cfg)
    (v,) = rep.verdicts
    assert v.passed is True


def test_lockbox_empty_trade_evidence_rejects_insufficient_n() -> None:
    # A zero-trade candidate (evidence ()) reaches filter #8 legally and is
    # rejected INSUFFICIENT_N — never a crash (review A2).
    cfg = _pass_config(run_hard_gates=False, dsr_min=-1.0)
    rep = run_stage4_cascade([_candidate("x", seed=1, total_trades=0, lockbox_trade_days=())], cfg)
    (v,) = rep.verdicts
    assert v.rejected_at is FilterStage.LOCKBOX
    assert LockboxStatus.INSUFFICIENT_N.value in v.detail


def test_lockbox_trade_days_equal_to_bar_days_matches_legacy() -> None:
    # Evidence == the lockbox day axis itself ⇒ the count source is the same
    # array the legacy mode counted ⇒ identical verdicts (the None-path pin).
    cfg = _pass_config()
    legacy = run_stage4_cascade([_candidate("x", seed=1)], cfg)
    explicit = run_stage4_cascade(
        [_candidate("x", seed=1, lockbox_trade_days=tuple(range(40)))], cfg
    )
    assert legacy.verdicts == explicit.verdicts
    assert legacy.survivor_ids == explicit.survivor_ids


def test_lockbox_evidence_outside_days_span_is_isolated_rejection() -> None:
    # Misaligned evidence (outside the lockbox_days span) is a per-candidate
    # LOCKBOX rejection, never a sweep crash (review A4 / edge 11).
    cfg = _pass_config()
    rep = run_stage4_cascade([_candidate("x", seed=1, lockbox_trade_days=(36, 999))], cfg)
    (v,) = rep.verdicts
    assert v.rejected_at is FilterStage.LOCKBOX
    assert "could not be evaluated" in v.detail


def test_candidate_input_rejects_unsorted_trade_days() -> None:
    with pytest.raises(ValueError, match="lockbox_trade_days must be non-decreasing"):
        _candidate("x", lockbox_trade_days=(5, 1))


def test_cascade_config_lockbox_defaults_are_the_lockbox_constants() -> None:
    # CLAUDE.md §11 COMMON: the config's lockbox defaults REFERENCE the lockbox
    # module's public constants (kills a literal-drift mutation) and the values
    # are byte-identical to the pre-repoint literals (manifest bytes unchanged).
    cfg = CascadeConfig(seed=1)
    assert cfg.lockbox_min_trades == LOCKBOX_DEFAULT_MIN_TRADES == 500
    assert cfg.lockbox_default_window_days == LOCKBOX_DEFAULT_WINDOW_DAYS == 182


# ── Composite lockbox term reads the MEASURED lockbox Sharpe (R2-SR-F5) ──


def test_composite_weights_pinned() -> None:
    # The missing direct pin (mirrors TestCompositeWeightPins, which pins only
    # the RANKING weights): exact keys, exact values, summing to 1.0.
    assert _COMPOSITE_WEIGHTS == {
        "sharpe_ratio": 0.25,
        "sortino_ratio": 0.20,
        "calmar_ratio": 0.15,
        "profit_factor_excess": 0.15,
        "recovery_factor": 0.10,
        "win_quality": 0.10,
        "lockbox_sharpe": 0.05,
    }
    assert math.fsum(_COMPOSITE_WEIGHTS.values()) == 1.0


def test_composite_uses_measured_lockbox_sharpe_not_training() -> None:
    # THE F5 regression (kills a revert of the raw-source swap): training
    # Sharpe ordering is the REVERSE of the measured lockbox-window Sharpe
    # ordering; every other raw column is identical across the pair, so the
    # ranking is decided by the lockbox term alone. The OLD code (feeding
    # c.training_sharpe) ranked "lowmeasured" first; the fix ranks by the
    # MEASURED filter-#8 value.
    cfg = _pass_config()
    lb_hi, days = _good_lockbox(101)
    # A uniform -0.1 shift lowers the mean while keeping the dispersion
    # identical ⇒ a strictly lower Sharpe series.
    lb_lo = tuple(r - 0.1 for r in lb_hi)
    measured_hi = lockbox_evaluation(
        lb_hi,
        days,
        training_sharpe=0.0,
        training_max_drawdown=5.0,
        min_trades=4,
        default_window_days=5,
    ).lockbox_sharpe
    measured_lo = lockbox_evaluation(
        lb_lo,
        days,
        training_sharpe=0.0,
        training_max_drawdown=5.0,
        min_trades=4,
        default_window_days=5,
    ).lockbox_sharpe
    assert measured_hi is not None and measured_lo is not None
    assert measured_hi > measured_lo  # fixture sanity
    shared = _strong_returns(7)
    cands = [
        # HIGHER measured lockbox Sharpe, LOWER training Sharpe...
        _candidate(
            "himeasured",
            seed=1,
            returns=shared,
            lockbox_returns=lb_hi,
            lockbox_days=days,
            training_sharpe=0.1,
        ),
        # ...vs LOWER measured lockbox Sharpe, HIGHER training Sharpe.
        _candidate(
            "lowmeasured",
            seed=2,
            returns=shared,
            lockbox_returns=lb_lo,
            lockbox_days=days,
            training_sharpe=0.2,
        ),
    ]
    rep = run_stage4_cascade(cands, cfg)
    assert rep.n_survived == 2
    assert rep.survivor_ids == ("himeasured", "lowmeasured")
    scores = {v.candidate_id: v.composite_score for v in rep.verdicts}
    assert scores["himeasured"] is not None and scores["lowmeasured"] is not None
    assert scores["himeasured"] > scores["lowmeasured"]
    assert all(math.isfinite(s) for s in scores.values() if s is not None)


def test_measured_lockbox_sharpe_helper() -> None:
    # Float passthrough + LOUD ValueError on a hand-built inconsistent result
    # (passed=True with lockbox_sharpe=None is unreachable via
    # lockbox_evaluation — LockboxResult has no cross-field validation, so the
    # helper is the only guard; the ONLY route to the defensive branch).
    ok = LockboxResult(
        status=LockboxStatus.PASS,
        passed=True,
        n_trades=10,
        lockbox_sharpe=1.25,
        lockbox_max_drawdown=0.1,
        training_sharpe=1.0,
        training_max_drawdown=0.2,
        window_start_day=0,
        extended=False,
        sharpe_gate_passed=True,
        mdd_gate_passed=True,
        count_gate_passed=True,
        min_trades=4,
    )
    assert _measured_lockbox_sharpe(ok) == 1.25
    bad = LockboxResult(
        status=LockboxStatus.PASS,
        passed=True,
        n_trades=10,
        lockbox_sharpe=None,
        lockbox_max_drawdown=0.1,
        training_sharpe=1.0,
        training_max_drawdown=0.2,
        window_start_day=0,
        extended=False,
        sharpe_gate_passed=True,
        mdd_gate_passed=True,
        count_gate_passed=True,
        min_trades=4,
    )
    with pytest.raises(ValueError, match="must carry a measured lockbox_sharpe"):
        _measured_lockbox_sharpe(bad)


def test_lone_survivor_composite_is_exactly_zero() -> None:
    # The single-survivor invariance envelope (review A6): with one survivor
    # every z column is [0.0], so the composite stays EXACTLY 0.0 pre- and
    # post-F5 — protects every 0/1-survivor wire pin from silent drift.
    rep = run_stage4_cascade([_candidate("solo", seed=1)], _pass_config())
    (v,) = rep.verdicts
    assert v.passed is True
    assert v.composite_score == 0.0


def test_lockbox_passer_rejected_at_mc_does_not_pollute_z_pool() -> None:
    # A #8-passer later rejected at #9 leaves an unused map entry (the map is
    # a SUPERSET of the survivors): no KeyError, and the lone remaining
    # survivor's composite is exactly 0.0 (a leaked second entry in the z pool
    # would produce ±0.05-scale nonzero scores).
    def always_beats(positions: npt.NDArray[np.intp]) -> float:
        return 1.0e9

    rep = run_stage4_cascade(
        [_candidate("keep", seed=1), _candidate("mcdead", seed=2, mc_evaluate=always_beats)],
        _pass_config(),
    )
    by = {v.candidate_id: v for v in rep.verdicts}
    assert by["mcdead"].rejected_at is FilterStage.MONTE_CARLO
    assert by["keep"].passed is True
    assert by["keep"].composite_score == 0.0


# ── Deterministic survivor tie-break (R2-SR-F6, mirrors DEC-RANKING-TIEBREAK-001) ──


def test_tied_survivors_order_by_candidate_id_permutation_invariant() -> None:
    # FULL-tie fixture: three candidates identical in every scalar AND every
    # series (distinct ids only) ⇒ every raw z column is zero-variance ⇒ all
    # composites exactly 0.0 ⇒ the id tie-break alone orders them. Shuffled
    # input yields the IDENTICAL ascending-id survivor tuple (fails before F6:
    # sorted() is stable, so tied scores followed input order).
    cfg = _pass_config()
    shared_returns = _strong_returns(11)
    lb, days = _good_lockbox(12)

    def clone(cid: str) -> CandidateInput:
        return _candidate(
            cid, seed=99, returns=shared_returns, lockbox_returns=lb, lockbox_days=days
        )

    ids = ["alpha", "beta", "gamma"]
    orderings: set[tuple[str, ...]] = set()
    for shuffle_seed in (0, 1, 2):
        shuffled = [clone(cid) for cid in ids]
        random.Random(shuffle_seed).shuffle(shuffled)
        rep = run_stage4_cascade(shuffled, cfg)
        assert rep.n_survived == 3
        scores = [v.composite_score for v in rep.verdicts]
        # Genuine full tie: every survivor scores IDENTICALLY. (The exact
        # value need not be 0.0 — statistics.pstdev can report a ~1e-16
        # pseudo-dispersion on an all-equal float column, mapping the whole
        # column to a uniform ±1 z; identical across candidates either way.)
        assert len(set(scores)) == 1
        orderings.add(rep.survivor_ids)
    assert orderings == {("alpha", "beta", "gamma")}


def test_distinct_scores_ignore_the_id_tie_break() -> None:
    # Non-tie regression: with distinct composites the order is purely
    # score-descending — the id key is inert ("zeta" outranks "alpha").
    cfg = _pass_config()
    lb_hi, days = _good_lockbox(101)
    lb_lo = tuple(r - 0.1 for r in lb_hi)  # strictly lower Sharpe series
    shared = _strong_returns(7)
    rep = run_stage4_cascade(
        [
            _candidate("alpha", seed=1, returns=shared, lockbox_returns=lb_lo, lockbox_days=days),
            _candidate("zeta", seed=2, returns=shared, lockbox_returns=lb_hi, lockbox_days=days),
        ],
        cfg,
    )
    assert rep.survivor_ids == ("zeta", "alpha")


# ── Provisional pool-N passes (R2-SR-F4) ──


def test_pool_fallback_survivors_are_provisional() -> None:
    # No trial_stats ⇒ dsr_n_source == "pool" ⇒ every PASS is provisional and
    # provisional_pass_ids is the survivor tuple itself (run-level stamp).
    rep = run_stage4_cascade([_candidate("a", seed=1), _candidate("b", seed=2)], _pass_config())
    assert rep.dsr_n_source == "pool"
    for v in rep.verdicts:
        assert v.passed is True
        assert v.provisional is True
    assert rep.provisional_pass_ids == rep.survivor_ids


def test_leaderboard_sourced_survivors_are_not_provisional() -> None:
    stats = DsrTrialStats(n_trials=5, sharpe_variance=0.01, n_finite=5)
    rep = run_stage4_cascade([_candidate("a", seed=1)], _pass_config(), trial_stats=stats)
    (v,) = rep.verdicts
    assert v.passed is True
    assert rep.dsr_n_source == "leaderboard"
    assert v.provisional is False
    assert rep.provisional_pass_ids == ()


def test_rejected_verdict_provisional_stays_false_under_pool_fallback() -> None:
    # Pass-scoped flag (review A6/F4-A6): a pool-N REJECTION is a-fortiori
    # safe under the true larger N, so it is never flagged. Also kills the
    # `provisional: bool = False` default mutant.
    rep = run_stage4_cascade([_candidate("x", seed=1, profit_factor=1.0)], _pass_config())
    (v,) = rep.verdicts
    assert v.passed is False
    assert v.provisional is False
    assert rep.provisional_pass_ids == ()


def test_single_candidate_pool_fallback_pass_is_provisional() -> None:
    # The most important case (review edge 3): a lone-candidate pool has
    # n_trials=1 / V=0 — an essentially undeflated PSR — yet can pass; the
    # pass MUST read provisional.
    rep = run_stage4_cascade([_candidate("solo", seed=1)], _pass_config())
    (v,) = rep.verdicts
    assert v.passed is True
    assert rep.dsr_n_trials == 1
    assert v.provisional is True
    assert rep.provisional_pass_ids == ("solo",)


def test_empty_report_provisional_pass_ids_empty() -> None:
    rep = CascadeReport(verdicts=(), survivor_ids=(), n_input=0, n_survived=0, stage_survivors={})
    assert rep.provisional_pass_ids == ()


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


# ── DEC-RESULTS-CASCADE-VERDICT-JOIN-001: the CPCV pass-rate capture matrix ──
# The recording law: the stage-3 rate is captured the moment cpcv_pass_rate()
# returns and BEFORE the threshold compare, then stamped onto EVERY verdict by
# ONE final dataclasses.replace pass — provably recording-only (no ordering /
# RNG / threshold / statistic change; the determinism + field-preservation
# pins below are the teeth).


def _oracle_rate(candidate: CandidateInput, cfg: CascadeConfig) -> float:
    """The independent stage-3 oracle — the SAME pure engine the cascade calls."""
    from brutex.metrics.cpcv import cpcv_pass_rate

    return cpcv_pass_rate(
        candidate.returns,
        n_groups=cfg.cpcv_n_groups,
        n_test_groups=cfg.cpcv_n_test_groups,
    )


class TestCpcvRateCapture:
    """Semantics table: None before-#3/ValueError; the measured rate after."""

    def test_pass_carries_the_measured_rate(self) -> None:
        cfg = _pass_config()
        cand = _candidate("a", seed=1)
        rep = run_stage4_cascade([cand], cfg)
        (v,) = rep.verdicts
        assert v.passed is True
        assert v.cpcv_pass_rate == _oracle_rate(cand, cfg)

    def test_cpcv_threshold_reject_carries_the_failing_rate(self) -> None:
        cfg = _pass_config(cpcv_min_pass_rate=1.01)
        cand = _candidate("x", seed=1)
        rep = run_stage4_cascade([cand], cfg)
        (v,) = rep.verdicts
        assert v.rejected_at is FilterStage.CPCV
        rate = _oracle_rate(cand, cfg)
        # The FAILING rate is recorded (captured BEFORE the compare — the
        # record-only-on-pass mutant dies here) and matches the detail prose.
        assert v.cpcv_pass_rate == rate
        assert f"{rate:.4f}" in v.detail

    def test_later_stage_reject_carries_its_stage3_rate(self) -> None:
        # Rejected at WALK-FORWARD (#7) — the stage-3 rate still rides.
        cfg = _pass_config(wfo_min_pass_rate=1.01)
        cand = _candidate("x", seed=1)
        rep = run_stage4_cascade([cand], cfg)
        (v,) = rep.verdicts
        assert v.rejected_at is FilterStage.WALK_FORWARD
        assert v.cpcv_pass_rate == _oracle_rate(cand, cfg)

    def test_hard_gate_reject_never_reached_stage3(self) -> None:
        cfg = _pass_config()
        rep = run_stage4_cascade([_candidate("x", seed=1, total_trades=10)], cfg)
        (v,) = rep.verdicts
        assert v.rejected_at is FilterStage.HARD_GATES
        assert v.cpcv_pass_rate is None

    def test_dsr_reject_never_reached_stage3(self) -> None:
        cfg = _pass_config(dsr_min=0.95)
        rep = run_stage4_cascade([_candidate("x", seed=1, sharpe_ratio=0.0)], cfg)
        (v,) = rep.verdicts
        assert v.rejected_at is FilterStage.DEFLATED_SHARPE
        assert v.cpcv_pass_rate is None

    def test_cpcv_valueerror_arm_records_no_rate(self) -> None:
        # A 2-obs series cannot form 4 CPCV groups → the engine raises → the
        # candidate rejects at CPCV with NO rate (never a fabricated number).
        cfg = _pass_config(run_hard_gates=False)
        rep = run_stage4_cascade(
            [_candidate("x", seed=1, returns=(0.1, 0.2), total_trades=600)], cfg
        )
        (v,) = rep.verdicts
        assert v.rejected_at is FilterStage.CPCV
        assert "could not be evaluated" in v.detail
        assert v.cpcv_pass_rate is None

    def test_determinism_two_runs_identical_verdicts(self) -> None:
        cfg = _pass_config(cpcv_min_pass_rate=1.01)
        cands = [_candidate("a", seed=1), _candidate("b", seed=2, total_trades=10)]
        first = run_stage4_cascade(cands, cfg)
        second = run_stage4_cascade(cands, cfg)
        assert first.verdicts == second.verdicts
        assert first.survivor_ids == second.survivor_ids

    def test_replace_stamp_preserves_every_other_field(self) -> None:
        import dataclasses

        cfg = _pass_config()
        mixed = [
            _candidate("a", seed=1),
            _candidate("b", seed=2, total_trades=10),
        ]
        rep = run_stage4_cascade(mixed, cfg)
        by_id = {v.candidate_id: v for v in rep.verdicts}
        # Nulling ONLY the new field reproduces the pre-change verdict shape
        # exactly — passed/rejected_at/detail/composite/provisional intact.
        a = dataclasses.replace(by_id["a"], cpcv_pass_rate=None)
        assert a.passed is True and a.rejected_at is None
        assert a.detail == "survived all Stage-4 filters"
        assert a.composite_score == by_id["a"].composite_score
        # No trial_stats threaded here → the pool-N fallback → provisional
        # (R2-SR-F4) — preserved verbatim through the stamp.
        assert a.provisional is True
        b = dataclasses.replace(by_id["b"], cpcv_pass_rate=None)
        assert b.passed is False and b.rejected_at is FilterStage.HARD_GATES
        # The verdict ORDER stays the input id order (the pre-change law).
        assert [v.candidate_id for v in rep.verdicts] == ["a", "b"]

    def test_default_construction_none(self) -> None:
        from brutex.metrics.cascade import CandidateVerdict

        v = CandidateVerdict(candidate_id="x", passed=False, rejected_at=None, detail="d")
        assert v.cpcv_pass_rate is None


# ── DEC-CASCADE-STAT-TIER-WIRE-001: the SPA join / embargo threading / the ──
# per-candidate PBO. The join law: filter #4 is RC-first (byte-identical
# historical detail) with Hansen's SPA-consistent as the primary test once the
# RC floor passes — monotone-tightening, never a loosening.


def _rc_spa_split_pool() -> list[CandidateInput]:
    """A REAL pool whose RC p-value (0.37) < Hansen-SPA consistent (0.595).

    Found by numeric probe under ``_pass_config()``'s BootstrapConfig
    (seed=7, n_resamples=200): one modest edge + two poor high-variance
    alternatives — exactly the pool shape Hansen (2005) shows White's RC is
    distorted by. Any ``spa_alpha`` strictly between the two p-values makes
    the RC floor PASS while the SPA primary REJECTS.
    """
    rng = np.random.default_rng(0)
    a = tuple((rng.standard_normal(32) * 1.0 + 0.25).tolist())
    b = tuple((rng.standard_normal(32) * 3.0 - 0.5).tolist())
    c = tuple((rng.standard_normal(32) * 3.0 - 0.5).tolist())
    return [
        _candidate("a", seed=1, returns=a),
        _candidate("b", seed=2, returns=b),
        _candidate("c", seed=3, returns=c),
    ]


def _pool_matrix_of(cands: list[CandidateInput]) -> npt.NDArray[np.float64]:
    return np.column_stack([np.asarray(c.returns, dtype=np.float64) for c in cands])


class TestSpaJoin:
    """Filter #4 = the RC-floor + Hansen-SPA-primary monotone-tightening join."""

    def test_rc_reject_keeps_the_byte_identical_detail_and_skips_spa(self) -> None:
        from brutex.metrics.spa import reality_check

        cfg = _pass_config()
        noise = [
            _candidate("a", seed=1, returns=_strong_returns(1, edge=0.0)),
            _candidate("b", seed=2, returns=_strong_returns(2, edge=0.0)),
        ]
        rep = run_stage4_cascade(noise, cfg)
        rc = reality_check(_pool_matrix_of(noise), cfg.bootstrap())
        assert rc.pvalue >= cfg.spa_alpha  # the premise: the RC floor fails
        by = {v.candidate_id: v for v in rep.verdicts}
        for cid in ("a", "b"):
            assert by[cid].rejected_at is FilterStage.REALITY_CHECK_SPA
            # The EXACT pre-join detail string — the historical floor's
            # rejection is byte-identical to the shipped cascade.
            assert by[cid].detail == (f"pool Reality-Check p {rc.pvalue:.4f} >= {cfg.spa_alpha}")
        assert rep.pool_rc_pvalue == rc.pvalue
        # The SPA arm never ran — None is DISTINGUISHABLE from a measured p.
        assert rep.pool_spa_pvalue is None

    def test_rc_pass_spa_fail_rejects_with_the_hansen_detail(self) -> None:
        from brutex.metrics.spa import hansen_spa, reality_check

        pool = _rc_spa_split_pool()
        cfg = _pass_config(spa_alpha=0.5)
        boot = cfg.bootstrap()
        rc = reality_check(_pool_matrix_of(pool), boot)
        spa = hansen_spa(_pool_matrix_of(pool), boot)
        assert rc.pvalue < cfg.spa_alpha <= spa.pvalue_consistent  # the premise
        rep = run_stage4_cascade(pool, cfg)
        assert rep.n_survived == 0
        assert rep.stage_survivors["reality_check_spa"] == 0
        for v in rep.verdicts:
            assert v.rejected_at is FilterStage.REALITY_CHECK_SPA
            assert v.detail == (
                f"pool Hansen-SPA consistent p {spa.pvalue_consistent:.4f} >= {cfg.spa_alpha}"
            )
        # BOTH measured p-values are recorded — the RC passed, the SPA killed.
        assert rep.pool_rc_pvalue == rc.pvalue
        assert rep.pool_spa_pvalue == spa.pvalue_consistent

    def test_both_pass_records_both_pvalues_and_survives(self) -> None:
        cfg = _pass_config()
        rep = run_stage4_cascade([_candidate("a", seed=1), _candidate("b", seed=2)], cfg)
        assert rep.n_survived == 2
        assert rep.pool_rc_pvalue is not None and rep.pool_rc_pvalue < cfg.spa_alpha
        assert rep.pool_spa_pvalue is not None and rep.pool_spa_pvalue < cfg.spa_alpha

    def test_pool_never_reaching_filter_4_records_neither(self) -> None:
        cfg = _pass_config()
        rep = run_stage4_cascade([_candidate("x", seed=1, total_trades=1)], cfg)
        assert rep.pool_rc_pvalue is None
        assert rep.pool_spa_pvalue is None

    def test_all_degenerate_pool_records_neither(self) -> None:
        # Constant returns are degenerate-rejected BEFORE the pool matrix is
        # built — the RC never runs, both p-values honestly None.
        cfg = _pass_config()
        rep = run_stage4_cascade([_candidate("x", seed=1, returns=(0.1,) * 32)], cfg)
        (v,) = rep.verdicts
        assert v.rejected_at is FilterStage.REALITY_CHECK_SPA
        assert "degenerate returns" in v.detail
        assert rep.pool_rc_pvalue is None
        assert rep.pool_spa_pvalue is None

    def test_report_defaults_are_none(self) -> None:
        rep = run_stage4_cascade([], _pass_config())
        assert rep.pool_rc_pvalue is None
        assert rep.pool_spa_pvalue is None


class TestEmbargoThreading:
    """``embargo_bars`` reaches BOTH engines; ``None`` is byte-identical to 0."""

    def test_embargo_reaches_cpcv_and_wfo(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from brutex.metrics import cascade as cascade_module

        seen: dict[str, list[int]] = {"cpcv": [], "wfo": []}
        real_cpcv = cascade_module.cpcv_pass_rate
        real_wfo = cascade_module.wfo_pass_rate

        def cpcv_spy(returns: object, **kwargs: object) -> float:
            seen["cpcv"].append(int(kwargs["embargo"]))  # type: ignore[call-overload]
            assert kwargs["label_span"] == 0  # per-bar labels: 1-bar horizon
            return real_cpcv(returns, **kwargs)  # type: ignore[arg-type]

        def wfo_spy(returns: object, scheme: object, **kwargs: object) -> float:
            seen["wfo"].append(int(kwargs["embargo"]))  # type: ignore[call-overload]
            return real_wfo(returns, scheme, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(cascade_module, "cpcv_pass_rate", cpcv_spy)
        monkeypatch.setattr(cascade_module, "wfo_pass_rate", wfo_spy)
        rep = cascade_module.run_stage4_cascade(
            [_candidate("a", seed=1)], _pass_config(), embargo_bars=3
        )
        assert rep.n_survived == 1
        assert seen["cpcv"] == [3]
        assert seen["wfo"] == [3, 3, 3]  # all three WFO schemes

    def test_none_is_byte_identical_to_zero(self) -> None:
        cfg = _pass_config()
        cands = [_candidate("a", seed=1), _candidate("b", seed=2)]
        assert run_stage4_cascade(cands, cfg) == run_stage4_cascade(cands, cfg, embargo_bars=0)

    def test_negative_embargo_raises(self) -> None:
        with pytest.raises(ValueError, match="embargo_bars must be >= 0"):
            run_stage4_cascade([], _pass_config(), embargo_bars=-1)

    def test_embargo_that_leaves_no_walks_rejects_at_wfo(self) -> None:
        # n=32, train_size=10: embargo 32 puts the first test start at 42 > 32
        # → the WFO engine's "yields no walks" ValueError → the EXISTING
        # short-series degrade arm rejects at the walk-forward stage (never a
        # raise out of the cascade).
        cfg = _pass_config()
        rep = run_stage4_cascade([_candidate("a", seed=1)], cfg, embargo_bars=32)
        (v,) = rep.verdicts
        assert v.rejected_at is FilterStage.WALK_FORWARD
        assert "WFO could not be evaluated" in v.detail
        assert "yields no walks" in v.detail

    def test_embargo_shifts_the_wfo_walks_for_real(self) -> None:
        # The WFO threading is a GENUINE change (unlike CPCV): the same
        # candidate passes with embargo=0 yet loses walks under embargo=8 —
        # proven by the engine's walk count over the same windows.
        from brutex.metrics.wfo import WFOScheme, walk_forward_splits

        n = 32
        cfg = _pass_config()
        walks_0 = walk_forward_splits(
            n,
            WFOScheme.ROLLING,
            train_size=cfg.wfo_train_size,
            test_size=cfg.wfo_test_size,
            step=cfg.wfo_step,
        )
        walks_8 = walk_forward_splits(
            n,
            WFOScheme.ROLLING,
            train_size=cfg.wfo_train_size,
            test_size=cfg.wfo_test_size,
            step=cfg.wfo_step,
            embargo=8,
        )
        assert len(walks_8) < len(walks_0)


class TestPerCandidatePbo:
    """Filter #5's per-candidate CSCV over the candidate's OWN search family."""

    @staticmethod
    def _family_for(cand: CandidateInput, seed: int, k: int) -> tuple[tuple[float, ...], ...]:
        """A family: the candidate's own series + ``k - 1`` sibling columns."""
        rng = np.random.default_rng(seed)
        siblings = [
            tuple((rng.standard_normal(len(cand.returns)) * 0.5).tolist()) for _ in range(k - 1)
        ]
        return (cand.returns, *siblings)

    @staticmethod
    def _oracle_pbo(family: tuple[tuple[float, ...], ...], n_splits: int) -> float:
        n_obs = len(family[0])
        usable = (n_obs // n_splits) * n_splits
        return pbo_probability(
            [[float(column[t]) for column in family] for t in range(n_obs - usable, n_obs)],
            n_splits=n_splits,
        ).pbo

    @staticmethod
    def _lucky_family(cand: CandidateInput) -> tuple[tuple[float, ...], ...]:
        """An overfit-shaped family: five HIGH-variance lucky siblings whose
        IS-best regularly flops OOS (probe-verified CSCV PBO ≈ 0.157 > 0)."""
        rng = np.random.default_rng(49)
        siblings = [
            tuple((rng.standard_normal(len(cand.returns)) * 8.0 + 1.0).tolist()) for _ in range(5)
        ]
        return (cand.returns, *siblings)

    def test_reject_carries_the_measured_value_and_detail(self) -> None:
        cfg = _pass_config()
        cand = _candidate("a", seed=1)
        family = self._lucky_family(cand)
        oracle = self._oracle_pbo(family, cfg.pbo_n_splits)
        assert oracle > 0.1  # anti-vacuous: the reject threshold below bites
        tight = _pass_config(pbo_max=0.1)
        rep = run_stage4_cascade([_candidate("a", seed=1, family_returns=family)], tight)
        (v,) = rep.verdicts
        assert v.rejected_at is FilterStage.PBO
        assert v.detail == f"candidate PBO {oracle:.4f} > {tight.pbo_max}"
        assert v.candidate_pbo == oracle
        assert rep.stage_survivors["pbo"] == 0

    def test_pass_carries_the_measured_value(self) -> None:
        cfg = _pass_config()  # pbo_max=1.0 — nothing rejects
        cand = _candidate("a", seed=1)
        family = self._family_for(cand, seed=99, k=4)
        rep = run_stage4_cascade([_candidate("a", seed=1, family_returns=family)], cfg)
        (v,) = rep.verdicts
        assert v.passed is True
        assert v.candidate_pbo == self._oracle_pbo(family, cfg.pbo_n_splits)

    def test_two_column_family_is_the_minimum_that_proceeds(self) -> None:
        # Boundary law (hostile-review round 1, MINOR-2): _MIN_POOL_FOR_PBO
        # == 2 is an INCLUSIVE floor — a K=2 family runs the per-candidate
        # CSCV and records the measured value (kills the `<` -> `<=` skip
        # mutant, under which a 2-column family would abstain with None; the
        # K=1 skip side is pinned by test_single_column_family_skips below).
        cfg = _pass_config()
        cand = _candidate("a", seed=1)
        family = self._family_for(cand, seed=99, k=2)
        assert len(family) == 2
        rep = run_stage4_cascade([_candidate("a", seed=1, family_returns=family)], cfg)
        (v,) = rep.verdicts
        assert v.passed is True
        assert v.candidate_pbo is not None
        assert v.candidate_pbo == self._oracle_pbo(family, cfg.pbo_n_splits)

    def test_no_family_skips_with_none(self) -> None:
        rep = run_stage4_cascade([_candidate("a", seed=1)], _pass_config())
        (v,) = rep.verdicts
        assert v.passed is True
        assert v.candidate_pbo is None

    def test_single_column_family_skips(self) -> None:
        cand = _candidate("a", seed=1)
        rep = run_stage4_cascade(
            [_candidate("a", seed=1, family_returns=(cand.returns,))], _pass_config()
        )
        (v,) = rep.verdicts
        assert v.passed is True
        assert v.candidate_pbo is None

    def test_short_series_family_skips(self) -> None:
        # pbo_n_splits=64 > n=32 → usable=0 → the per-candidate check (and
        # the pool half) honestly abstain.
        cfg = _pass_config(pbo_n_splits=64)
        cand = _candidate("a", seed=1)
        family = self._family_for(cand, seed=99, k=3)
        rep = run_stage4_cascade([_candidate("a", seed=1, family_returns=family)], cfg)
        (v,) = rep.verdicts
        assert v.passed is True
        assert v.candidate_pbo is None
        assert rep.pool_pbo is None

    def test_pool_pbo_runs_first_and_a_pool_kill_skips_the_per_candidate_check(
        self,
    ) -> None:
        # pbo_max=-1.0: the pool PBO (>= 0 always) rejects EVERYTHING at the
        # pool half, so the per-candidate half never runs — candidate_pbo
        # stays None even though a family is present (ordering law).
        cfg = _pass_config(pbo_max=-1.0)
        a = _candidate("a", seed=1)
        fam_a = self._family_for(a, seed=99, k=3)
        rep = run_stage4_cascade(
            [
                _candidate("a", seed=1, family_returns=fam_a),
                _candidate("b", seed=2),
            ],
            cfg,
        )
        by = {v.candidate_id: v for v in rep.verdicts}
        assert by["a"].rejected_at is FilterStage.PBO
        assert by["a"].detail.startswith("pool PBO ")
        assert by["a"].candidate_pbo is None
        assert rep.pool_pbo is not None

    def test_family_validation_rejects_mismatched_length(self) -> None:
        with pytest.raises(ValueError, match="must share len"):
            _candidate("a", seed=1, family_returns=((0.1, 0.2),))

    def test_family_validation_rejects_non_finite(self) -> None:
        bad = tuple([float("nan")] + [0.1] * 31)
        cand = _candidate("a", seed=1)
        with pytest.raises(ValueError, match="all-finite"):
            _candidate("a", seed=1, family_returns=(cand.returns, bad))

    def test_verdict_default_candidate_pbo_none(self) -> None:
        from brutex.metrics.cascade import CandidateVerdict

        v = CandidateVerdict(candidate_id="x", passed=True, rejected_at=None, detail="d")
        assert v.candidate_pbo is None
