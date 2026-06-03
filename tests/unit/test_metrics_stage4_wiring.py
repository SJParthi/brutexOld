"""Unit tests for :func:`brutex.metrics.compute.with_stage4_statistics`.

Verifies the Stage-4 wiring (spec lines 582-583): ``compute_metrics`` leaves
``deflated_sharpe_ratio`` / ``pbo_probability`` NULL, and
``with_stage4_statistics`` fills them from the pure DSR / PBO functions —
including every NULL-guard branch and the excess→non-excess kurtosis
conversion (DEC-STAT-DSR-PBO-001).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from brutex.metrics import (
    ClosedTrade,
    EquityCurve,
    ResultMetrics,
    compute_metrics,
    deflated_sharpe_ratio,
    with_stage4_statistics,
)
from brutex.metrics.deflated_sharpe import GAUSSIAN_KURTOSIS
from brutex.strategies.types import Side

_T0 = datetime(2025, 1, 1, 9, 15, tzinfo=UTC)
_PNLS = [500, -200, 300, -100, 400, -50, 250, -150, 600, -300]
#: A CSCV (T, N) matrix for the PBO leg: two anti-correlated strategies.
_PBO_MATRIX = [[float(p), float(-p)] for p in _PNLS]


def _base_metrics() -> ResultMetrics:
    """A 10-trade result whose Sharpe / skew / kurtosis are all defined."""
    trades = [
        ClosedTrade(
            pnl_paisa=pnl,
            mfe_paisa=abs(pnl),
            mae_paisa=abs(pnl),
            holding_bars=3,
            entry_ts=_T0 + timedelta(minutes=10 * i),
            exit_ts=_T0 + timedelta(minutes=10 * i + 3),
            direction=Side.BUY,
        )
        for i, pnl in enumerate(_PNLS)
    ]
    equity = [10_000_000]
    running = 10_000_000
    for pnl in _PNLS:
        running += pnl
        equity.append(running)
    curve = EquityCurve(equity_paisa=tuple(equity), start_equity_paisa=10_000_000, span_years=1.0)
    return compute_metrics(trades, curve, total_bars=10_000)


def test_compute_metrics_leaves_stage4_null() -> None:
    """The per-result pass cannot know the cross-trial inputs — both stay NULL."""
    base = _base_metrics()
    assert base.deflated_sharpe_ratio is None
    assert base.pbo_probability is None


def test_fills_both_dsr_and_pbo() -> None:
    """With all inputs supplied, both Stage-4 fields are populated probabilities."""
    result = with_stage4_statistics(
        _base_metrics(),
        n_trials=5000,
        trial_sharpe_variance=0.4,
        pbo_performance=_PBO_MATRIX,
        pbo_n_splits=2,
    )
    assert result.deflated_sharpe_ratio is not None
    assert 0.0 <= result.deflated_sharpe_ratio <= 1.0
    assert result.pbo_probability is not None
    assert 0.0 <= result.pbo_probability <= 1.0


def test_kurtosis_excess_to_nonexcess_conversion() -> None:
    """The wiring adds back ``GAUSSIAN_KURTOSIS`` (excess → raw) before the DSR."""
    base = _base_metrics()
    assert base.sharpe_ratio is not None
    assert base.skewness is not None
    assert base.kurtosis is not None
    expected = deflated_sharpe_ratio(
        base.sharpe_ratio,
        n_trials=5000,
        trial_sharpe_variance=0.4,
        sample_length=base.total_trades,
        skew=base.skewness,
        kurtosis=base.kurtosis + GAUSSIAN_KURTOSIS,
    ).deflated_sharpe_ratio
    result = with_stage4_statistics(base, n_trials=5000, trial_sharpe_variance=0.4)
    assert result.deflated_sharpe_ratio == expected


def test_pbo_null_when_no_matrix_supplied() -> None:
    """Without a CSCV matrix the PBO stays NULL while the DSR still fills."""
    result = with_stage4_statistics(_base_metrics(), n_trials=100, trial_sharpe_variance=0.2)
    assert result.deflated_sharpe_ratio is not None
    assert result.pbo_probability is None


def test_dsr_null_when_sharpe_none() -> None:
    """A row with no Sharpe (NULL) leaves the DSR NULL (first guard operand)."""
    base = replace(_base_metrics(), sharpe_ratio=None)
    result = with_stage4_statistics(base, n_trials=100, trial_sharpe_variance=0.2)
    assert result.deflated_sharpe_ratio is None


def test_dsr_null_when_skewness_none() -> None:
    """A row with no skewness leaves the DSR NULL (second guard operand)."""
    base = replace(_base_metrics(), skewness=None)
    result = with_stage4_statistics(base, n_trials=100, trial_sharpe_variance=0.2)
    assert result.deflated_sharpe_ratio is None


def test_dsr_null_when_kurtosis_none() -> None:
    """A row with no kurtosis leaves the DSR NULL (third guard operand)."""
    base = replace(_base_metrics(), kurtosis=None)
    result = with_stage4_statistics(base, n_trials=100, trial_sharpe_variance=0.2)
    assert result.deflated_sharpe_ratio is None


def test_dsr_null_when_too_few_trades() -> None:
    """Defined moments but < 2 trades leaves the DSR NULL (fourth guard operand)."""
    base = replace(_base_metrics(), total_trades=1)
    result = with_stage4_statistics(base, n_trials=100, trial_sharpe_variance=0.2)
    assert result.deflated_sharpe_ratio is None


def test_other_fields_are_preserved() -> None:
    """The Stage-4 pass copies every other metric unchanged (frozen replace)."""
    base = _base_metrics()
    result = with_stage4_statistics(
        base, n_trials=100, trial_sharpe_variance=0.2, pbo_performance=_PBO_MATRIX, pbo_n_splits=2
    )
    assert result.net_pnl_paisa == base.net_pnl_paisa
    assert result.sharpe_ratio == base.sharpe_ratio
    assert result.total_trades == base.total_trades
    assert result.max_drawdown_paisa == base.max_drawdown_paisa
