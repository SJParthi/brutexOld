"""Unit tests for the pure Stage-2.5 SPRT early-stop core (DEC-SPRT-EARLY-STOP-CORE-001).

Drives ``brutex/engine/early_stop.py`` to 100% line+branch (under the
``--cov=brutex.engine`` aggregate gate). The module is inert — these tests are
the only callers; they prove the decision is deterministic and exercises every
branch (floor, undefined-Sharpe, KILL, CONTINUE, config validation).
"""

from __future__ import annotations

import math

import pytest
from brutex.engine.early_stop import (
    Decision,
    EarlyStopConfigError,
    RunningSharpe,
    SprtConfig,
    sprt_decision,
    wald_bounds,
)


# ── SprtConfig ───────────────────────────────────────────────────────
def test_config_defaults_match_spec() -> None:
    cfg = SprtConfig()
    assert cfg.alpha == 0.05
    assert cfg.beta == 0.10
    assert cfg.min_trades == 50
    assert cfg.kill_posterior == 0.10


@pytest.mark.parametrize(
    "kwargs",
    [
        {"alpha": 0.0},
        {"alpha": 1.0},
        {"beta": 0.0},
        {"beta": 1.0},
        {"min_trades": 0},
        {"kill_posterior": 0.0},
        {"kill_posterior": 1.0},
    ],
)
def test_config_rejects_out_of_bounds(kwargs: dict[str, float]) -> None:
    with pytest.raises(EarlyStopConfigError):
        SprtConfig(**kwargs)  # type: ignore[arg-type]


# ── wald_bounds ──────────────────────────────────────────────────────
def test_wald_bounds_match_alpha_beta_formula() -> None:
    cfg = SprtConfig()
    lower, upper = wald_bounds(cfg)
    assert lower == math.log(0.10 / 0.95)
    assert upper == math.log(0.90 / 0.05)
    assert lower < 0.0 < upper


# ── RunningSharpe ────────────────────────────────────────────────────
def test_running_sharpe_undefined_below_two_trades() -> None:
    stat = RunningSharpe()
    assert stat.sharpe is None
    stat.update(0.01)
    assert stat.n == 1
    assert stat.sharpe is None  # n < 2


def test_running_sharpe_zero_variance_is_none() -> None:
    stat = RunningSharpe()
    for _ in range(10):
        stat.update(0.02)  # identical returns → variance 0
    assert stat.n == 10
    assert stat.sharpe is None


def test_running_sharpe_matches_offline_mean_over_std() -> None:
    returns = [0.01, -0.02, 0.03, 0.00, 0.015]
    stat = RunningSharpe()
    for r in returns:
        stat.update(r)
    n = len(returns)
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / n
    assert stat.sharpe == pytest.approx(mean / math.sqrt(var))


# ── sprt_decision ────────────────────────────────────────────────────
def _stream(stat: RunningSharpe, value: float, *, sigma: float, count: int, seed: int) -> None:
    import random

    rng = random.Random(seed)
    for _ in range(count):
        stat.update(rng.gauss(value, sigma))


def test_decision_continues_below_min_trades_floor() -> None:
    stat = RunningSharpe()
    _stream(stat, -0.05, sigma=0.01, count=49, seed=1)  # clearly losing but n < 50
    assert sprt_decision(stat, p95_threshold=0.5, cfg=SprtConfig()) is Decision.CONTINUE


def test_decision_continues_when_sharpe_undefined_at_floor() -> None:
    stat = RunningSharpe()
    for _ in range(50):
        stat.update(-0.02)  # 50 identical → variance 0 → sharpe None
    assert stat.n == 50
    assert sprt_decision(stat, p95_threshold=0.1, cfg=SprtConfig()) is Decision.CONTINUE


def test_decision_kills_a_hopeless_combo() -> None:
    stat = RunningSharpe()
    _stream(stat, -0.01, sigma=0.02, count=60, seed=2)  # losing
    # Posterior P(true Sharpe > a high 0.3 benchmark) is tiny → KILL.
    assert sprt_decision(stat, p95_threshold=0.3, cfg=SprtConfig()) is Decision.KILL


def test_decision_keeps_a_strong_combo() -> None:
    stat = RunningSharpe()
    _stream(stat, 0.05, sigma=0.02, count=60, seed=3)  # strongly winning
    # Posterior P(true Sharpe > a low 0.1 benchmark) is high → CONTINUE.
    assert sprt_decision(stat, p95_threshold=0.1, cfg=SprtConfig()) is Decision.CONTINUE


def test_decision_is_deterministic() -> None:
    """Same stream + same threshold → same decision (Golden Rule #4)."""
    returns = [0.01, -0.03, 0.02, -0.04, 0.00] * 12  # 60 trades
    a, b = RunningSharpe(), RunningSharpe()
    for r in returns:
        a.update(r)
        b.update(r)
    cfg = SprtConfig()
    assert sprt_decision(a, p95_threshold=0.2, cfg=cfg) is sprt_decision(
        b, p95_threshold=0.2, cfg=cfg
    )


def test_decision_enum_values() -> None:
    assert Decision.CONTINUE.value == "continue"
    assert Decision.KILL.value == "kill"
