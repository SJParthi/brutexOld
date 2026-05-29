"""Hypothesis property tests for :mod:`brutex.metrics`.

Proves invariants that must hold for *every* valid trade set / equity
curve, complementing the known-value unit tests in
``tests/unit/test_metrics_compute.py``:

1. **Profit-factor sign law** — ``profit_factor > 1`` iff the strategy
   is net positive on the realised legs (gross profit exceeds gross
   loss), and ``< 1`` iff net negative (spec line 562 + gate line 596).
2. **Drawdown sign** — ``max_drawdown_paisa >= 0`` and
   ``max_drawdown_pct >= 0``; the Ulcer Index is ``>= 0`` (spec lines
   548-552). The percentage is NOT clamped at 100%: when realised
   losses drive equity below zero (and below the prior peak by more
   than the peak), a peak-to-trough drop can legitimately exceed 100%
   of the peak. That is real, gate-relevant information (the
   ``max_drawdown_pct > 30%`` rejection gate, spec line 597), so it is
   reported as-is rather than truncated.
3. **Sharpe / mean-return sign agreement** — the per-trade-return
   Sharpe (when defined) shares the sign of the mean return.
4. **Scale invariance of Sharpe** — scaling every P&L by a positive
   constant leaves the Sharpe ratio unchanged (it is a pure ratio).
5. **Accounting identity** — ``net_pnl == gross_profit − gross_loss``
   and ``winning + losing + scratch == total``.
6. **No NaN / inf** — every populated float field is finite (spec
   line 746: "No NaN/inf in result rows").
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

from brutex.metrics import ClosedTrade, EquityCurve, compute_metrics
from brutex.strategies.types import Side
from hypothesis import given
from hypothesis import strategies as st

_T0 = datetime(2025, 1, 1, 9, 15, tzinfo=UTC)

# Per-trade P&L in paisa: wide range, includes zero (scratch).
_pnl = st.integers(min_value=-5_000_000, max_value=5_000_000)
_pnl_lists = st.lists(_pnl, min_size=1, max_size=40)


def _make_trades(pnls: list[int]) -> list[ClosedTrade]:
    """Build a chronological trade list from raw P&L values."""
    out: list[ClosedTrade] = []
    for i, pnl in enumerate(pnls):
        entry = _T0 + timedelta(minutes=10 * i)
        out.append(
            ClosedTrade(
                pnl_paisa=pnl,
                mfe_paisa=abs(pnl),
                mae_paisa=abs(pnl),
                holding_bars=3,
                entry_ts=entry,
                exit_ts=entry + timedelta(minutes=3),
                direction=Side.BUY if i % 2 == 0 else Side.SELL,
            )
        )
    return out


def _curve_from(pnls: list[int], start: int = 10_000_000) -> EquityCurve:
    """Build the equity curve implied by cumulating the P&L series."""
    equity = [start]
    running = start
    for pnl in pnls:
        running += pnl
        equity.append(running)
    return EquityCurve(equity_paisa=tuple(equity), start_equity_paisa=start, span_years=1.0)


@given(_pnl_lists)
def test_profit_factor_sign_law(pnls: list[int]) -> None:
    """``profit_factor`` crosses 1.0 exactly when gross profit crosses gross loss."""
    trades = _make_trades(pnls)
    m = compute_metrics(trades, _curve_from(pnls), total_bars=10_000)
    if m.profit_factor is None:
        # None only when there are no losing trades (gross_loss == 0).
        assert m.gross_loss_paisa == 0
        return
    if m.gross_profit_paisa > m.gross_loss_paisa:
        assert m.profit_factor > 1.0
    elif m.gross_profit_paisa < m.gross_loss_paisa:
        assert m.profit_factor < 1.0
    else:
        assert math.isclose(m.profit_factor, 1.0)


@given(_pnl_lists)
def test_drawdown_sign_and_range(pnls: list[int]) -> None:
    """Drawdown magnitudes are non-negative (the % may exceed 100 on ruin)."""
    trades = _make_trades(pnls)
    m = compute_metrics(trades, _curve_from(pnls), total_bars=10_000)
    assert m.max_drawdown_paisa >= 0
    assert m.max_drawdown_pct >= 0.0
    assert m.ulcer_index >= 0.0
    assert m.max_drawdown_duration_bars >= 0


@given(_pnl_lists)
def test_sharpe_sign_matches_mean(pnls: list[int]) -> None:
    """When defined, the Sharpe ratio shares the sign of the mean return."""
    trades = _make_trades(pnls)
    m = compute_metrics(trades, _curve_from(pnls), total_bars=10_000)
    if m.sharpe_ratio is None:
        return
    mean_pnl = sum(pnls) / len(pnls)
    if mean_pnl > 0:
        assert m.sharpe_ratio > 0
    elif mean_pnl < 0:
        assert m.sharpe_ratio < 0
    else:
        assert math.isclose(m.sharpe_ratio, 0.0, abs_tol=1e-9)


@given(_pnl_lists, st.integers(min_value=2, max_value=1000))
def test_sharpe_scale_invariant(pnls: list[int], scale: int) -> None:
    """Scaling every P&L by a positive constant leaves Sharpe unchanged."""
    base = compute_metrics(_make_trades(pnls), _curve_from(pnls), total_bars=10_000)
    scaled_pnls = [p * scale for p in pnls]
    scaled = compute_metrics(_make_trades(scaled_pnls), _curve_from(scaled_pnls), total_bars=10_000)
    if base.sharpe_ratio is None:
        assert scaled.sharpe_ratio is None
    else:
        assert scaled.sharpe_ratio is not None
        assert math.isclose(base.sharpe_ratio, scaled.sharpe_ratio, rel_tol=1e-9, abs_tol=1e-12)


@given(_pnl_lists)
def test_accounting_identities(pnls: list[int]) -> None:
    """Net P&L and trade-count partitions are internally consistent."""
    trades = _make_trades(pnls)
    m = compute_metrics(trades, _curve_from(pnls), total_bars=10_000)
    assert m.net_pnl_paisa == sum(pnls)
    assert m.net_pnl_paisa == m.gross_profit_paisa - m.gross_loss_paisa
    scratch = sum(1 for p in pnls if p == 0)
    assert m.winning_trades + m.losing_trades + scratch == m.total_trades
    assert m.total_trades == len(pnls)


@given(_pnl_lists)
def test_no_nan_or_inf(pnls: list[int]) -> None:
    """No populated float field is ever NaN or inf (spec line 746)."""
    trades = _make_trades(pnls)
    m = compute_metrics(trades, _curve_from(pnls), total_bars=10_000)
    floats = [
        m.total_return_pct,
        m.cagr_pct,
        m.sharpe_ratio,
        m.sortino_ratio,
        m.calmar_ratio,
        m.mar_ratio,
        m.information_ratio,
        m.omega_ratio,
        m.max_drawdown_pct,
        m.recovery_factor,
        m.ulcer_index,
        m.win_rate,
        m.profit_factor,
        m.win_loss_ratio,
        m.avg_holding_time_bars,
        m.time_in_market_pct,
        m.tail_ratio,
        m.skewness,
        m.kurtosis,
        m.returns_std_dev,
    ]
    for value in floats:
        if value is not None:
            assert math.isfinite(value)
