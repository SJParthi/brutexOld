"""Known-value unit tests for :mod:`brutex.ranking.gates`.

Each test pins a single hard rejection gate (spec lines 589-600) to a
hand-chosen :class:`~brutex.metrics.ResultMetrics` and asserts the exact
:class:`~brutex.ranking.gates.RejectionReason` flag(s) that should trip.
"""

from __future__ import annotations

from dataclasses import replace

from brutex.metrics import ResultMetrics
from brutex.ranking.gates import (
    RejectionReason,
    passes_gates,
)

# A baseline result that PASSES every gate. Each test mutates one field
# (via dataclasses.replace) to trip exactly one gate.
_GOOD = ResultMetrics(
    net_pnl_paisa=1_000_000,
    total_return_pct=10.0,
    cagr_pct=12.0,
    gross_profit_paisa=2_000_000,
    gross_loss_paisa=500_000,
    sharpe_ratio=1.5,
    sortino_ratio=2.0,
    calmar_ratio=1.0,
    mar_ratio=1.0,
    information_ratio=1.0,
    omega_ratio=2.0,
    max_drawdown_pct=12.0,
    max_drawdown_paisa=120_000,
    max_drawdown_duration_bars=10,
    recovery_factor=8.0,
    ulcer_index=3.0,
    largest_losing_trade_paisa=-50_000,
    max_consecutive_losses=4,
    max_consecutive_wins=6,
    total_trades=50,
    winning_trades=30,
    losing_trades=20,
    win_rate=0.60,
    profit_factor=4.0,
    avg_winning_trade_paisa=66_000,
    avg_losing_trade_paisa=25_000,
    win_loss_ratio=2.64,
    expectancy_paisa=20_000,
    avg_mfe_paisa=70_000,
    avg_mae_paisa=30_000,
    avg_holding_time_bars=5.0,
    max_holding_time_bars=12,
    time_in_market_pct=40.0,
    tail_ratio=1.3,
    skewness=0.1,
    kurtosis=0.5,
    returns_std_dev=0.02,
)


def test_baseline_passes_all_gates() -> None:
    """The baseline result trips no gate."""
    out = passes_gates(_GOOD)
    assert out.passed is True
    assert out.reason is RejectionReason.NONE
    assert out.overfit_gate_evaluated is False


def test_insufficient_trades_rejects() -> None:
    """``total_trades < 30`` trips INSUFFICIENT_TRADES (spec line 595)."""
    out = passes_gates(replace(_GOOD, total_trades=29))
    assert out.reason is RejectionReason.INSUFFICIENT_TRADES
    assert out.passed is False


def test_exactly_thirty_trades_passes() -> None:
    """``total_trades == 30`` is the boundary and PASSES (strict ``<``)."""
    assert passes_gates(replace(_GOOD, total_trades=30)).passed is True


def test_low_profit_factor_rejects() -> None:
    """``profit_factor < 1.2`` trips LOW_PROFIT_FACTOR (spec line 596)."""
    out = passes_gates(replace(_GOOD, profit_factor=1.19))
    assert out.reason is RejectionReason.LOW_PROFIT_FACTOR


def test_profit_factor_boundary_passes() -> None:
    """``profit_factor == 1.2`` is the boundary and PASSES (strict ``<``)."""
    assert passes_gates(replace(_GOOD, profit_factor=1.2)).passed is True


def test_none_profit_factor_passes() -> None:
    """A ``None`` profit_factor (no losers) PASSES the edge gate."""
    out = passes_gates(replace(_GOOD, profit_factor=None))
    assert RejectionReason.LOW_PROFIT_FACTOR not in out.reason
    assert out.passed is True


def test_excessive_drawdown_rejects() -> None:
    """``max_drawdown_pct > 30`` trips EXCESSIVE_DRAWDOWN (spec line 597)."""
    out = passes_gates(replace(_GOOD, max_drawdown_pct=30.01))
    assert out.reason is RejectionReason.EXCESSIVE_DRAWDOWN


def test_drawdown_boundary_passes() -> None:
    """``max_drawdown_pct == 30`` is the boundary and PASSES (strict ``>``)."""
    assert passes_gates(replace(_GOOD, max_drawdown_pct=30.0)).passed is True


def test_poor_win_profile_requires_both_clauses() -> None:
    """The win-profile gate needs BOTH low win_rate AND low win/loss (line 598)."""
    # Both low → rejects.
    both_low = replace(_GOOD, win_rate=0.20, win_loss_ratio=1.5)
    assert passes_gates(both_low).reason is RejectionReason.POOR_WIN_PROFILE
    # Only win_rate low (win/loss high) → passes.
    assert passes_gates(replace(_GOOD, win_rate=0.20, win_loss_ratio=3.0)).passed is True
    # Only win/loss low (win_rate high) → passes.
    assert passes_gates(replace(_GOOD, win_rate=0.60, win_loss_ratio=1.5)).passed is True


def test_poor_win_profile_none_win_loss_passes() -> None:
    """Low win_rate but ``None`` win/loss (no losers) does NOT trip the gate."""
    out = passes_gates(replace(_GOOD, win_rate=0.10, win_loss_ratio=None))
    assert RejectionReason.POOR_WIN_PROFILE not in out.reason
    assert out.passed is True


def test_win_profile_boundaries_pass() -> None:
    """At the exact floors (``0.25`` / ``2.0``) the strict ``<`` does not trip."""
    out = passes_gates(replace(_GOOD, win_rate=0.25, win_loss_ratio=2.0))
    assert out.passed is True


def test_loss_streak_rejects() -> None:
    """``max_consecutive_losses > 10`` trips LOSS_STREAK (spec line 599)."""
    out = passes_gates(replace(_GOOD, max_consecutive_losses=11))
    assert out.reason is RejectionReason.LOSS_STREAK


def test_loss_streak_boundary_passes() -> None:
    """``max_consecutive_losses == 10`` is the boundary and PASSES."""
    assert passes_gates(replace(_GOOD, max_consecutive_losses=10)).passed is True


def test_overfit_gate_skipped_without_lockbox() -> None:
    """With no lockbox_sharpe the overfit gate is SKIPPED, not failed."""
    out = passes_gates(_GOOD, lockbox_sharpe=None)
    assert out.overfit_gate_evaluated is False
    assert RejectionReason.OVERFIT not in out.reason


def test_overfit_gate_rejects_when_collapsed() -> None:
    """lockbox/sharpe < 0.5 trips OVERFIT when lockbox is supplied (line 600)."""
    # sharpe 1.5, lockbox 0.5 → ratio 0.333 < 0.5 → reject.
    out = passes_gates(_GOOD, lockbox_sharpe=0.5)
    assert out.overfit_gate_evaluated is True
    assert out.reason is RejectionReason.OVERFIT


def test_overfit_gate_passes_when_robust() -> None:
    """lockbox/sharpe >= 0.5 passes the overfit gate (evaluated, not tripped)."""
    # sharpe 1.5, lockbox 1.0 → ratio 0.667 >= 0.5 → pass.
    out = passes_gates(_GOOD, lockbox_sharpe=1.0)
    assert out.overfit_gate_evaluated is True
    assert out.passed is True


def test_overfit_boundary_passes() -> None:
    """ratio exactly 0.5 PASSES (strict ``<``)."""
    # sharpe 2.0, lockbox 1.0 → ratio 0.5, not < 0.5 → pass.
    out = passes_gates(replace(_GOOD, sharpe_ratio=2.0), lockbox_sharpe=1.0)
    assert out.passed is True


def test_overfit_skipped_when_training_sharpe_none() -> None:
    """A None training sharpe has no ratio → overfit gate skipped."""
    out = passes_gates(replace(_GOOD, sharpe_ratio=None), lockbox_sharpe=1.0)
    assert out.overfit_gate_evaluated is False
    assert RejectionReason.OVERFIT not in out.reason


def test_overfit_skipped_when_training_sharpe_zero() -> None:
    """A zero training sharpe has no finite ratio → overfit gate skipped."""
    out = passes_gates(replace(_GOOD, sharpe_ratio=0.0), lockbox_sharpe=1.0)
    assert out.overfit_gate_evaluated is False


def test_multiple_gates_combine() -> None:
    """Several simultaneous failures combine into one Flag value."""
    bad = replace(
        _GOOD,
        total_trades=10,
        profit_factor=1.0,
        max_drawdown_pct=40.0,
        max_consecutive_losses=20,
    )
    out = passes_gates(bad)
    assert RejectionReason.INSUFFICIENT_TRADES in out.reason
    assert RejectionReason.LOW_PROFIT_FACTOR in out.reason
    assert RejectionReason.EXCESSIVE_DRAWDOWN in out.reason
    assert RejectionReason.LOSS_STREAK in out.reason
    assert out.passed is False
