"""Stress test for the Stage-3 overfit gate (DEC-RANKING-OVERFIT-001).

Ranks a LARGE leaderboard (tens of thousands of combos) through the opt-in overfit
gate to prove the gate stays correct + deterministic at scale: no crash on a wide
varied pool, the survivor set is a subset of the legacy gate-survivors, every
survivor carries a valid Deflated Sharpe that cleared the bar, and the leaderboard
is byte-stable across repeated runs (Golden Rule #4 / #7).
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from brutex.metrics.compute import ResultMetrics
from brutex.ranking import OverfitGateConfig, rank_combos

# Marker discipline (tests/stress/__init__.py): the nightly (`-m stress`) and
# self-hosted soak (`-m 'stress or soak'`) tiers select BY MARKER — an unmarked
# stress test silently drops out of both tiers (SOAK-R7-1).
pytestmark = pytest.mark.stress

_POOL_SIZE = 30_000


def _m(**overrides: Any) -> ResultMetrics:
    """A gate-passing ResultMetrics; override per scenario."""
    base: dict[str, Any] = {
        "net_pnl_paisa": 0,
        "total_return_pct": 0.0,
        "cagr_pct": 0.0,
        "gross_profit_paisa": 0,
        "gross_loss_paisa": 0,
        "sharpe_ratio": 1.0,
        "sortino_ratio": 1.0,
        "calmar_ratio": 1.0,
        "mar_ratio": 0.0,
        "information_ratio": 0.0,
        "omega_ratio": 0.0,
        "max_drawdown_pct": 5.0,
        "max_drawdown_paisa": 0,
        "max_drawdown_duration_bars": 0,
        "recovery_factor": 1.0,
        "ulcer_index": 0.0,
        "largest_losing_trade_paisa": 0,
        "max_consecutive_losses": 2,
        "max_consecutive_wins": 0,
        "total_trades": 50,
        "winning_trades": 30,
        "losing_trades": 20,
        "win_rate": 0.6,
        "profit_factor": 1.5,
        "avg_winning_trade_paisa": 0,
        "avg_losing_trade_paisa": 0,
        "win_loss_ratio": 1.5,
        "expectancy_paisa": 0,
        "avg_mfe_paisa": 0,
        "avg_mae_paisa": 0,
        "avg_holding_time_bars": 0.0,
        "max_holding_time_bars": 0,
        "time_in_market_pct": 0.0,
        "tail_ratio": 0.0,
        "skewness": 0.0,
        "kurtosis": 0.0,
        "returns_std_dev": 0.0,
        # The loss/growth trio (DEC-METRICS-LOSSRATE-FIRSTCLASS-001 /
        # DEC-METRICS-KELLY-GROWTH-001): loss_rate consistent with the
        # counts above (losing 20 / total 50); the Kelly pair defaults
        # None (undefined) — override per test where the value matters.
        "loss_rate": 0.4,
        "kelly_fraction": None,
        "log_growth_at_half_kelly": None,
    }
    base.update(overrides)
    return ResultMetrics(**base)


def _large_pool() -> list[tuple[int, ResultMetrics]]:
    # A deterministic, tightly-clustered Sharpe spread [1.0, 2.0) with long tracks
    # (500 trades) so the pool is firmly deflatable AND, against N=30,000 trials,
    # the multiple-testing bar splits it into a real partial survivor set: the
    # weak-Sharpe tail is deflated out, the strong cluster survives.
    return [
        (i, _m(sharpe_ratio=1.0 + (i % 100) / 100.0, total_trades=500)) for i in range(_POOL_SIZE)
    ]


def test_large_overfit_sweep_is_correct_and_deterministic() -> None:
    cand = _large_pool()
    cfg = OverfitGateConfig(dsr_min=0.95)

    legacy = {rc.mask for rc in rank_combos(cand)}
    ranked = rank_combos(cand, overfit=cfg)
    gated = {rc.mask for rc in ranked}

    # The gate only shrinks the leaderboard, and it DID reject some combos
    # (the low-Sharpe tail) — the multiple-testing correction is doing work.
    assert gated <= legacy
    assert 0 < len(gated) < len(legacy)

    # Every survivor carries a valid Deflated Sharpe that cleared the bar.
    for rc in ranked:
        assert rc.deflated_sharpe is not None
        assert 0.95 <= rc.deflated_sharpe <= 1.0

    # Sorted descending by composite, and byte-stable across a re-run.
    scores = [rc.composite_score for rc in ranked]
    assert scores == sorted(scores, reverse=True)
    rerun = rank_combos(cand, overfit=cfg)
    assert [rc.mask for rc in rerun] == [rc.mask for rc in ranked]
    assert [rc.composite_score for rc in rerun] == scores


def test_huge_all_tied_pool_is_permutation_stable_no_quadratic() -> None:
    """A 100k pool of byte-IDENTICAL combos ranks deterministically and fast.

    The tie-break stress case (DEC-RANKING-TIEBREAK-001): every combo shares the same
    metrics, so the composite scores collapse into a SINGLE tied value (the z-scores of
    an all-equal pool are all 0.0). The ranking is then decided ENTIRELY by the
    ascending-mask tie-break. Proves (a) the ranking is a total order at scale, (b) it
    is permutation-stable (a reversed enumeration yields the identical leaderboard),
    and (c) there is no accidental O(n²) blow-up — a single O(n log n) sort handles
    100k tied keys in well under a generous wall-clock ceiling.

    The metrics are chosen so every composite TERM input is an exactly-representable
    binary float (``win_rate * win_loss_ratio`` uses 0.5 × 2.0 = 1.0, NOT the default
    0.6 × 1.5 = 0.899…), so the population variance over 100k copies is EXACTLY 0.0 and
    the whole pool ties at composite 0.0 — making the all-tied regime unambiguous.
    """
    pool = [(i, _m(sharpe_ratio=1.0, win_rate=0.5, win_loss_ratio=2.0)) for i in range(100_000)]

    start = time.perf_counter()
    ranked = rank_combos(pool)
    elapsed = time.perf_counter() - start

    # All distinct masks survive; all tie at composite 0.0 (exact zero variance).
    assert len(ranked) == 100_000
    assert {rc.composite_score for rc in ranked} == {0.0}
    # Decided purely by the ascending-mask tie-break — a strict total order.
    assert [rc.mask for rc in ranked] == list(range(100_000))
    # Permutation stability: a reversed enumeration ranks byte-identically.
    reversed_ranked = rank_combos(list(reversed(pool)))
    assert [rc.mask for rc in reversed_ranked] == [rc.mask for rc in ranked]
    # No quadratic blow-up: 100k all-tied keys sort in << 5 s (O(n log n)). A
    # quadratic path would take minutes. Generous to stay green on shared CI.
    assert elapsed < 5.0


def test_huge_distinct_pool_is_permutation_stable() -> None:
    """A 100k pool of all-DISTINCT scores is permutation-stable at scale.

    Complements the all-tied case: here every combo has a unique Sharpe ⇒ unique
    composite, so the primary (score) key decides — the ranking must still be
    byte-identical across a reversed enumeration (Golden Rules #4/#7).
    """
    pool = [(i, _m(sharpe_ratio=float(i))) for i in range(100_000)]
    forward = [rc.mask for rc in rank_combos(pool)]
    reverse = [rc.mask for rc in rank_combos(list(reversed(pool)))]
    assert forward == reverse
    # Descending composite ⇒ descending Sharpe ⇒ descending mask here.
    assert forward[0] == 99_999
    assert forward[-1] == 0
