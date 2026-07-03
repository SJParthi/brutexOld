"""Golden byte-equality oracle for :mod:`brutex.metrics.cascade`.

A hand-fixed 3-candidate pool (two strong real-edge winners + one that fails a
hard rejection gate) under a pinned seed. Locks the full cascade outcome
bit-for-bit (Golden Rule #4): the ranked survivor set, the per-stage survivor
counts, and the per-candidate verdicts.
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt
from brutex.metrics.cascade import (
    CandidateInput,
    CascadeConfig,
    FilterStage,
    run_stage4_cascade,
)


def _null_exit(positions: npt.NDArray[np.intp]) -> float:
    """Null exit logic: random entries never beat a positive observed Sharpe."""
    return 0.0


def _candidate(cid: str, seed: int, edge: float, **overrides: object) -> CandidateInput:
    rng = np.random.default_rng(seed)
    returns = tuple((rng.standard_normal(32) + edge).tolist())
    lockbox = tuple((rng.standard_normal(40) * 0.05 + 0.2).tolist())
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
        "returns": returns,
        "lockbox_returns": lockbox,
        "lockbox_days": tuple(range(40)),
        "mc_n_bars": 400,
        "mc_n_entries": 20,
        "mc_evaluate": _null_exit,
    }
    base.update(overrides)
    return CandidateInput(**base)  # type: ignore[arg-type]


_CONFIG = CascadeConfig(
    seed=20240603,
    n_resamples=300,
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
    mc_n_permutations=200,
)


def _pool() -> list[CandidateInput]:
    return [
        _candidate("alpha", 1, 1.0),
        _candidate("beta", 2, 0.9),
        _candidate("junk", 3, 0.0, profit_factor=1.0),  # fails the profit-factor gate
    ]


def test_golden_cascade() -> None:
    rep = run_stage4_cascade(_pool(), _CONFIG)
    assert rep.n_input == 3
    assert rep.n_survived == 2
    assert rep.survivor_ids == ("alpha", "beta")
    assert rep.rejected_ids == ("junk",)
    assert rep.stage_survivors == {
        "hard_gates": 2,
        "deflated_sharpe": 2,
        "cpcv": 2,
        "reality_check_spa": 2,
        "pbo": 2,
        "romano_wolf": 2,
        "walk_forward": 2,
        "lockbox": 2,
        "monte_carlo": 2,
    }
    by = {v.candidate_id: v for v in rep.verdicts}
    # DEC-STATS-GATE-INTEGRITY-001 (R2-SR-F5): the composite's lockbox term now
    # reads each survivor's MEASURED filter-#8 lockbox Sharpe (alpha
    # 4.086626678022204 vs beta 2.932208319049763 — recomputed independently
    # in-repo via lockbox_evaluation + fmean/pstdev z-scores), so the two
    # scores split ±(0.05 · z) instead of the pre-fix 0.0/0.0 (the old code fed
    # the IDENTICAL training_sharpe=0.1 to both → a zero-variance z column).
    # Every other raw column is identical across the pair, so the composite is
    # exactly the weighted lockbox z-term: 0.05 × (±1 up to 1 ulp).
    assert by["alpha"].passed and by["alpha"].composite_score == 0.050000000000000024
    assert by["beta"].passed and by["beta"].composite_score == -0.04999999999999999
    # R2-SR-F4: a pool-N-deflated pass is stamped PROVISIONAL (dsr_n_source
    # is "pool" below); the rejected verdict stays False (pass-scoped flag).
    assert by["alpha"].provisional is True
    assert by["beta"].provisional is True
    assert by["junk"].provisional is False
    assert rep.provisional_pass_ids == rep.survivor_ids
    assert by["junk"].rejected_at is FilterStage.HARD_GATES
    assert by["junk"].composite_score is None
    # DEC-PBO-SIDECAR-001: the measured pool PBO is now SURFACED on the report
    # (previously computed then dropped on the pass path). Exact literal —
    # CSCV is exhaustive (no RNG): 46 overfit logits of C(8,4)=70 splits.
    assert rep.pool_pbo == 0.6571428571428571
    # ADR-0008 / DEC-TRIAL-STATS-CONSUME-001: with no trial_stats supplied the
    # legacy pool source is STAMPED, with the exact N and V echoed — all three
    # candidates share sharpe_ratio=2.0, so the pool dispersion is exactly 0.
    assert rep.dsr_n_source == "pool"
    assert rep.dsr_n_trials == 3
    assert rep.dsr_trial_variance == 0.0


def test_golden_cascade_determinism() -> None:
    assert run_stage4_cascade(_pool(), _CONFIG) == run_stage4_cascade(_pool(), _CONFIG)
