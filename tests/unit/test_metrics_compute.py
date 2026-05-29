"""Unit tests for :mod:`brutex.metrics` — per-result trading metrics.

Concrete known-value assertions for every metric in the Track 1
Result Row Schema (``docs/TRACK1_BACKTEST_SPEC.md`` lines 512-579), plus
explicit edge-case coverage (zero trades, no losers, no winners,
zero-variance returns, single trade, total ruin) so the module reaches
100% line + 100% branch coverage.

Per DEC-P0-017 (PR #39): this is a WHITEBOX test that probes the
private streaming-moment helpers (``_accumulate_moments``,
``_percentile``) directly to verify numerical stability independent of
the public surface. The file-level ``reportPrivateUsage=none`` directive
below is intentional.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest
from brutex.metrics import (
    ClosedTrade,
    EquityCurve,
    Moments,
    ResultMetrics,
    compute_metrics,
)
from brutex.metrics.compute import _accumulate_moments, _percentile
from brutex.strategies.types import Side

_T0 = datetime(2025, 1, 1, 9, 15, tzinfo=UTC)


def _trade(
    pnl: int,
    *,
    mfe: int = 0,
    mae: int = 0,
    holding: int = 5,
    direction: Side = Side.BUY,
    offset_min: int = 0,
) -> ClosedTrade:
    """Build a :class:`ClosedTrade` with sensible defaults for tests."""
    entry = _T0 + timedelta(minutes=offset_min)
    return ClosedTrade(
        pnl_paisa=pnl,
        mfe_paisa=mfe,
        mae_paisa=mae,
        holding_bars=holding,
        entry_ts=entry,
        exit_ts=entry + timedelta(minutes=holding),
        direction=direction,
    )


def _curve(values: list[int], *, start: int = 1_000_000, span: float = 1.0) -> EquityCurve:
    """Build an :class:`EquityCurve` from a list of paisa equity values."""
    return EquityCurve(equity_paisa=tuple(values), start_equity_paisa=start, span_years=span)


# ── ClosedTrade validation ──────────────────────────────────────────


def test_closed_trade_rejects_negative_mfe() -> None:
    """A negative MFE is rejected at construction."""
    with pytest.raises(ValueError, match="mfe_paisa must be >= 0"):
        _trade(100, mfe=-1)


def test_closed_trade_rejects_negative_mae() -> None:
    """A negative MAE is rejected at construction."""
    with pytest.raises(ValueError, match="mae_paisa must be >= 0"):
        _trade(100, mae=-1)


def test_closed_trade_rejects_zero_holding() -> None:
    """A non-positive holding span is rejected."""
    with pytest.raises(ValueError, match="holding_bars must be >= 1"):
        _trade(100, holding=0)


def test_closed_trade_rejects_naive_entry() -> None:
    """A naive entry timestamp is rejected."""
    with pytest.raises(ValueError, match="entry_ts must be tz-aware"):
        ClosedTrade(
            pnl_paisa=1,
            mfe_paisa=0,
            mae_paisa=0,
            holding_bars=1,
            entry_ts=datetime(2025, 1, 1, 9, 15),
            exit_ts=_T0,
            direction=Side.BUY,
        )


def test_closed_trade_rejects_naive_exit() -> None:
    """A naive exit timestamp is rejected."""
    with pytest.raises(ValueError, match="exit_ts must be tz-aware"):
        ClosedTrade(
            pnl_paisa=1,
            mfe_paisa=0,
            mae_paisa=0,
            holding_bars=1,
            entry_ts=_T0,
            exit_ts=datetime(2025, 1, 1, 9, 16),
            direction=Side.BUY,
        )


def test_closed_trade_rejects_exit_before_entry() -> None:
    """An exit timestamp before entry is rejected."""
    with pytest.raises(ValueError, match="before entry_ts"):
        ClosedTrade(
            pnl_paisa=1,
            mfe_paisa=0,
            mae_paisa=0,
            holding_bars=1,
            entry_ts=_T0,
            exit_ts=_T0 - timedelta(minutes=1),
            direction=Side.BUY,
        )


def test_closed_trade_rejects_hold_direction() -> None:
    """``HOLD`` is not a tradeable direction."""
    with pytest.raises(ValueError, match="must be BUY or SELL"):
        _trade(100, direction=Side.HOLD)


def test_closed_trade_win_loss_flags() -> None:
    """``is_win`` / ``is_loss`` follow the sign of the P&L (scratch = neither)."""
    assert _trade(100).is_win
    assert not _trade(100).is_loss
    assert _trade(-100).is_loss
    assert not _trade(-100).is_win
    assert not _trade(0).is_win
    assert not _trade(0).is_loss


# ── EquityCurve validation ──────────────────────────────────────────


def test_equity_curve_rejects_empty() -> None:
    """An empty equity curve is rejected."""
    with pytest.raises(ValueError, match="must be non-empty"):
        EquityCurve(equity_paisa=(), start_equity_paisa=1, span_years=1.0)


def test_equity_curve_rejects_nonpositive_start() -> None:
    """A non-positive starting capital is rejected."""
    with pytest.raises(ValueError, match="start_equity_paisa must be > 0"):
        EquityCurve(equity_paisa=(1,), start_equity_paisa=0, span_years=1.0)


def test_equity_curve_rejects_nonpositive_span() -> None:
    """A non-positive span is rejected."""
    with pytest.raises(ValueError, match="span_years must be > 0"):
        EquityCurve(equity_paisa=(1,), start_equity_paisa=1, span_years=0.0)


# ── compute_metrics: guard ──────────────────────────────────────────


def test_compute_rejects_zero_total_bars() -> None:
    """``total_bars`` must be at least 1."""
    with pytest.raises(ValueError, match="total_bars must be >= 1"):
        compute_metrics([], _curve([1_000_000]), total_bars=0)


# ── Zero-trade result ───────────────────────────────────────────────


def test_zero_trades_returns_empty_row() -> None:
    """A no-trade result yields zeros / ``None`` and a drawdown from the curve."""
    # Curve dips from 1_000_000 to 900_000 then recovers to 1_000_000.
    m = compute_metrics([], _curve([1_000_000, 900_000, 1_000_000]), total_bars=10)
    assert m.total_trades == 0
    assert m.net_pnl_paisa == 0
    assert m.sharpe_ratio is None
    assert m.profit_factor is None
    assert m.recovery_factor is None
    assert m.win_loss_ratio is None
    assert m.tail_ratio is None
    assert m.skewness is None
    assert m.kurtosis is None
    assert m.returns_std_dev is None
    assert m.win_rate == 0.0
    assert m.time_in_market_pct == 0.0
    # Drawdown still computed: 100_000 paisa = 10% of the 1_000_000 peak.
    assert m.max_drawdown_paisa == 100_000
    assert m.max_drawdown_pct == pytest.approx(10.0)
    assert m.max_drawdown_duration_bars == 1


# ── Return metrics ──────────────────────────────────────────────────


def test_return_metrics_known_values() -> None:
    """Net P&L, gross profit/loss, total return on a hand-checked set."""
    trades = [_trade(30_000), _trade(-10_000), _trade(20_000)]
    curve = _curve([1_000_000, 1_030_000, 1_020_000, 1_040_000], start=1_000_000)
    m = compute_metrics(trades, curve, total_bars=100)
    assert m.net_pnl_paisa == 40_000
    assert m.gross_profit_paisa == 50_000
    assert m.gross_loss_paisa == 10_000  # absolute
    assert m.total_return_pct == pytest.approx(4.0)  # 40_000 / 1_000_000


def test_cagr_one_year_span() -> None:
    """Over a 1-year span, CAGR equals the simple total return."""
    trades = [_trade(100_000)]  # +10% on 1_000_000
    curve = _curve([1_000_000, 1_100_000], start=1_000_000, span=1.0)
    m = compute_metrics(trades, curve, total_bars=10)
    assert m.cagr_pct == pytest.approx(10.0)


def test_cagr_two_year_span() -> None:
    """Over 2 years a +21% total return annualises to ~10% CAGR."""
    trades = [_trade(210_000)]  # +21% -> sqrt(1.21) = 1.10
    curve = _curve([1_000_000, 1_210_000], start=1_000_000, span=2.0)
    m = compute_metrics(trades, curve, total_bars=10)
    assert m.cagr_pct == pytest.approx(10.0)


def test_cagr_total_ruin_clamps_to_minus_100() -> None:
    """A wipe-out terminal equity clamps CAGR to -100%."""
    trades = [_trade(-1_000_000)]  # loses the entire base
    curve = _curve([1_000_000, 0], start=1_000_000, span=1.0)
    m = compute_metrics(trades, curve, total_bars=10)
    assert m.cagr_pct == -100.0


# ── Trade quality ───────────────────────────────────────────────────


def test_trade_quality_known_values() -> None:
    """Counts, win rate, profit factor, averages, expectancy on a fixed set."""
    trades = [_trade(40_000), _trade(20_000), _trade(-30_000), _trade(0)]
    m = compute_metrics(trades, _curve([1_000_000]), total_bars=100)
    assert m.total_trades == 4
    assert m.winning_trades == 2
    assert m.losing_trades == 1  # scratch (0) is neither
    assert m.win_rate == pytest.approx(2 / 4)
    assert m.gross_profit_paisa == 60_000
    assert m.gross_loss_paisa == 30_000
    assert m.profit_factor == pytest.approx(2.0)
    assert m.avg_winning_trade_paisa == 30_000  # 60_000 // 2
    assert m.avg_losing_trade_paisa == 30_000  # |−30_000| // 1
    assert m.win_loss_ratio == pytest.approx(1.0)
    assert m.expectancy_paisa == 30_000 // 4  # 30_000 net / 4 trades floored


def test_profit_factor_none_when_no_losers() -> None:
    """All-wins: profit factor / omega / win-loss-ratio are ``None``."""
    trades = [_trade(10_000), _trade(20_000)]
    m = compute_metrics(trades, _curve([1_000_000, 1_030_000]), total_bars=10)
    assert m.profit_factor is None
    assert m.omega_ratio is None
    assert m.win_loss_ratio is None
    assert m.losing_trades == 0
    assert m.avg_losing_trade_paisa == 0


def test_no_winners_zero_gross_profit() -> None:
    """All-losses: gross profit zero, profit factor zero (not ``None``)."""
    trades = [_trade(-10_000), _trade(-20_000)]
    m = compute_metrics(trades, _curve([1_000_000, 980_000, 970_000]), total_bars=10)
    assert m.winning_trades == 0
    assert m.avg_winning_trade_paisa == 0
    assert m.gross_profit_paisa == 0
    assert m.profit_factor == pytest.approx(0.0)
    assert m.win_loss_ratio == pytest.approx(0.0)


# ── MFE / MAE / timing ──────────────────────────────────────────────


def test_excursions_and_timing() -> None:
    """Average MFE/MAE and holding-time stats average correctly."""
    trades = [
        _trade(100, mfe=500, mae=200, holding=4),
        _trade(-50, mfe=100, mae=400, holding=8),
    ]
    m = compute_metrics(trades, _curve([1_000_000]), total_bars=24)
    assert m.avg_mfe_paisa == 300  # (500 + 100) // 2
    assert m.avg_mae_paisa == 300  # (200 + 400) // 2
    assert m.avg_holding_time_bars == pytest.approx(6.0)
    assert m.max_holding_time_bars == 8
    assert m.time_in_market_pct == pytest.approx(50.0)  # 12 / 24 bars


def test_time_in_market_clamped_to_100() -> None:
    """Overlapping holds cannot push time-in-market above 100%."""
    trades = [_trade(1, holding=20), _trade(1, holding=20)]
    m = compute_metrics(trades, _curve([1_000_000]), total_bars=10)
    assert m.time_in_market_pct == 100.0


# ── Drawdown family ─────────────────────────────────────────────────


def test_drawdown_and_recovery() -> None:
    """MDD paisa/pct, duration, recovery factor and largest loss."""
    # Peak 1_100_000, trough 880_000 -> drop 220_000 = 20% of peak.
    curve = _curve(
        [1_000_000, 1_100_000, 990_000, 880_000, 1_050_000],
        start=1_000_000,
    )
    trades = [_trade(60_000), _trade(-40_000), _trade(-90_000), _trade(120_000)]
    m = compute_metrics(trades, curve, total_bars=50)
    assert m.max_drawdown_paisa == 220_000
    assert m.max_drawdown_pct == pytest.approx(20.0)
    assert m.max_drawdown_duration_bars == 3  # bars 2,3,4 below the 1_100_000 peak
    assert m.largest_losing_trade_paisa == -90_000
    # recovery_factor = net_pnl / MDD = 50_000 / 220_000.
    assert m.recovery_factor == pytest.approx(50_000 / 220_000)


def test_recovery_factor_none_when_no_drawdown() -> None:
    """A monotonically rising curve has zero MDD -> recovery factor ``None``."""
    curve = _curve([1_000_000, 1_010_000, 1_020_000], start=1_000_000)
    m = compute_metrics([_trade(20_000)], curve, total_bars=10)
    assert m.max_drawdown_paisa == 0
    assert m.max_drawdown_pct == 0.0
    assert m.max_drawdown_duration_bars == 0
    assert m.recovery_factor is None


def test_calmar_none_when_no_drawdown() -> None:
    """Calmar / MAR are ``None`` when MaxDD% is zero."""
    curve = _curve([1_000_000, 1_010_000], start=1_000_000)
    m = compute_metrics([_trade(10_000)], curve, total_bars=10)
    assert m.calmar_ratio is None
    assert m.mar_ratio is None


def test_calmar_known_value() -> None:
    """Calmar = CAGR / MaxDD% on a hand-checked curve."""
    # +10% over 1 year -> CAGR 10%; peak 1_100_000 -> 1_045_000 = 5% MDD.
    curve = _curve([1_000_000, 1_100_000, 1_045_000, 1_100_000], start=1_000_000, span=1.0)
    m = compute_metrics([_trade(100_000)], curve, total_bars=10)
    assert m.max_drawdown_pct == pytest.approx(5.0)
    assert m.calmar_ratio == pytest.approx(10.0 / 5.0)
    assert m.mar_ratio == m.calmar_ratio


def test_ulcer_index_known_value() -> None:
    """Ulcer Index is the RMS of the percentage drawdown series."""
    # Curve: peak 100, then 90 (-10%), then 100 (0%). DD series (as %):
    # bar0 0, bar1 10, bar2 0 -> RMS = sqrt((0+100+0)/3) = 5.7735...
    curve = _curve([100, 90, 100], start=1_000_000)
    m = compute_metrics([_trade(1)], curve, total_bars=10)
    assert m.ulcer_index == pytest.approx(math.sqrt(100.0 / 3.0))


def test_drawdown_nonpositive_peak_skips_pct() -> None:
    """When the running peak is <= 0 the percentage drawdown is skipped.

    The curve values are independent of ``start_equity_paisa`` (the
    capital base). A curve whose running peak never rises above zero
    exercises the ``peak > 0`` guard's false branch: paisa drawdown is
    still tracked, but no fraction-of-peak contribution is added.
    """
    curve = _curve([-100, -300, -200], start=1_000_000)
    m = compute_metrics([_trade(1)], curve, total_bars=10)
    # Peak stays at -100; trough -300 -> 200 paisa drop tracked.
    assert m.max_drawdown_paisa == 200
    # No positive-peak bar -> percentage DD and ulcer stay zero.
    assert m.max_drawdown_pct == 0.0
    assert m.ulcer_index == 0.0


def test_consecutive_runs() -> None:
    """Max consecutive wins and losses; scratch breaks both streaks."""
    trades = [
        _trade(1),
        _trade(1),
        _trade(-1),
        _trade(-1),
        _trade(-1),
        _trade(0),  # scratch resets
        _trade(1),
    ]
    m = compute_metrics(trades, _curve([1_000_000]), total_bars=20)
    assert m.max_consecutive_wins == 2
    assert m.max_consecutive_losses == 3


# ── Risk-adjusted ───────────────────────────────────────────────────


def test_sharpe_known_value() -> None:
    """Sharpe = mean/std (population) of per-trade fractional returns."""
    # Returns as fraction of 1_000_000: +0.02, -0.01, +0.03.
    trades = [_trade(20_000), _trade(-10_000), _trade(30_000)]
    rets = [0.02, -0.01, 0.03]
    mean = sum(rets) / 3
    var = sum((r - mean) ** 2 for r in rets) / 3
    expected = mean / math.sqrt(var)
    m = compute_metrics(trades, _curve([1_000_000]), total_bars=10)
    assert m.sharpe_ratio == pytest.approx(expected)
    assert m.returns_std_dev == pytest.approx(math.sqrt(var))


def test_sharpe_none_zero_variance() -> None:
    """Identical returns -> zero variance -> Sharpe ``None``."""
    trades = [_trade(10_000), _trade(10_000), _trade(10_000)]
    m = compute_metrics(trades, _curve([1_000_000]), total_bars=10)
    assert m.returns_std_dev == pytest.approx(0.0)
    assert m.sharpe_ratio is None
    assert m.information_ratio is None
    assert m.skewness is None
    assert m.kurtosis is None


def test_sharpe_none_single_trade() -> None:
    """A single trade has undefined variance -> Sharpe / moments ``None``."""
    m = compute_metrics([_trade(10_000)], _curve([1_000_000]), total_bars=10)
    assert m.returns_std_dev is None
    assert m.sharpe_ratio is None
    assert m.skewness is None
    assert m.kurtosis is None


def test_sortino_known_value() -> None:
    """Sortino = mean / downside-deviation about a zero target."""
    trades = [_trade(20_000), _trade(-10_000), _trade(30_000)]
    rets = [0.02, -0.01, 0.03]
    mean = sum(rets) / 3
    downside = math.sqrt(sum(r * r for r in rets if r < 0) / 3)
    m = compute_metrics(trades, _curve([1_000_000]), total_bars=10)
    assert m.sortino_ratio == pytest.approx(mean / downside)


def test_sortino_none_when_no_downside() -> None:
    """No negative returns -> downside deviation zero -> Sortino ``None``."""
    trades = [_trade(10_000), _trade(20_000)]
    m = compute_metrics(trades, _curve([1_000_000]), total_bars=10)
    assert m.sortino_ratio is None


def test_omega_equals_profit_factor_at_zero_threshold() -> None:
    """Omega at a 0% threshold equals gross_profit / gross_loss."""
    trades = [_trade(30_000), _trade(-10_000), _trade(20_000)]
    m = compute_metrics(trades, _curve([1_000_000]), total_bars=10)
    assert m.omega_ratio == pytest.approx(5.0)  # 50_000 / 10_000
    assert m.omega_ratio == m.profit_factor


def test_information_ratio_uses_benchmark() -> None:
    """Information ratio subtracts the buy-and-hold benchmark return."""
    trades = [_trade(20_000), _trade(-10_000), _trade(30_000)]
    rets = [0.02, -0.01, 0.03]
    mean = sum(rets) / 3
    std = math.sqrt(sum((r - mean) ** 2 for r in rets) / 3)
    # total_return_pct = 4.0; benchmark 1.0 -> excess 3.0% = 0.03 fraction.
    m = compute_metrics(trades, _curve([1_000_000]), total_bars=10, benchmark_return_pct=1.0)
    assert m.information_ratio == pytest.approx(0.03 / std)


# ── Distribution ────────────────────────────────────────────────────


def test_skewness_symmetric_is_zero() -> None:
    """A symmetric return set has ~zero skewness."""
    trades = [_trade(-20_000), _trade(0), _trade(20_000)]
    m = compute_metrics(trades, _curve([1_000_000]), total_bars=10)
    assert m.skewness == pytest.approx(0.0, abs=1e-12)


def test_kurtosis_matches_manual_moment() -> None:
    """Excess kurtosis matches a direct fourth-moment computation."""
    pnls = [10_000, -20_000, 30_000, -5_000, 15_000]
    trades = [_trade(p) for p in pnls]
    rets = [p / 1_000_000 for p in pnls]
    n = len(rets)
    mean = sum(rets) / n
    m2 = sum((r - mean) ** 2 for r in rets) / n
    m4 = sum((r - mean) ** 4 for r in rets) / n
    expected = m4 / (m2 * m2) - 3.0
    m = compute_metrics(trades, _curve([1_000_000]), total_bars=10)
    assert m.kurtosis == pytest.approx(expected)


def test_tail_ratio_known_value() -> None:
    """Tail ratio = p95 / |p5| of the per-trade return series."""
    pnls = [-50_000, -20_000, 0, 20_000, 60_000]
    trades = [_trade(p) for p in pnls]
    rets = sorted(p / 1_000_000 for p in pnls)
    p95 = _percentile(rets, 95.0)
    p5 = _percentile(rets, 5.0)
    m = compute_metrics(trades, _curve([1_000_000]), total_bars=10)
    assert m.tail_ratio == pytest.approx(p95 / abs(p5))


def test_tail_ratio_none_when_p5_zero() -> None:
    """Tail ratio is ``None`` when |p5| is zero (degenerate left tail)."""
    # All identical positive returns -> p5 == p95 == that value (nonzero),
    # so build a case where p5 is exactly zero instead.
    trades = [_trade(0), _trade(0), _trade(100_000)]
    m = compute_metrics(trades, _curve([1_000_000]), total_bars=10)
    assert m.tail_ratio is None


# ── Moments / percentile helpers ────────────────────────────────────


def test_accumulate_moments_empty() -> None:
    """Empty input yields zero-count moments with no variance."""
    mom = _accumulate_moments([])
    assert mom.n == 0
    assert mom.variance is None
    assert mom.std_dev is None


def test_accumulate_moments_matches_numpy_style() -> None:
    """Streaming moments match a direct two-pass computation."""
    xs = [1.0, 2.0, 3.0, 4.0, 100.0]
    mom = _accumulate_moments(xs)
    n = len(xs)
    mean = sum(xs) / n
    m2 = sum((x - mean) ** 2 for x in xs)
    m3 = sum((x - mean) ** 3 for x in xs)
    m4 = sum((x - mean) ** 4 for x in xs)
    assert mom.mean == pytest.approx(mean)
    assert mom.m2 == pytest.approx(m2)
    assert mom.m3 == pytest.approx(m3)
    assert mom.m4 == pytest.approx(m4)
    assert mom.variance == pytest.approx(m2 / n)


def test_percentile_single_element() -> None:
    """A one-element series returns that element for any percentile."""
    assert _percentile([7.0], 95.0) == 7.0
    assert _percentile([7.0], 5.0) == 7.0


def test_percentile_exact_index() -> None:
    """A percentile landing on an exact index returns that element."""
    # 5 elements, p50 -> rank 2.0 -> exact index 2.
    assert _percentile([0.0, 1.0, 2.0, 3.0, 4.0], 50.0) == 2.0


def test_result_metrics_is_frozen() -> None:
    """ResultMetrics is an immutable, hashable value object."""
    m = compute_metrics([_trade(1)], _curve([1_000_000]), total_bars=10)
    assert isinstance(m, ResultMetrics)
    with pytest.raises((AttributeError, TypeError)):
        m.net_pnl_paisa = 5  # type: ignore[misc]


def test_moments_is_frozen_value() -> None:
    """Moments is a frozen dataclass."""
    mom = Moments(n=1, mean=1.0, m2=0.0, m3=0.0, m4=0.0)
    with pytest.raises((AttributeError, TypeError)):
        mom.n = 2  # type: ignore[misc]
