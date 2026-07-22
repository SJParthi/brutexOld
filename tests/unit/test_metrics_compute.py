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

import dataclasses
import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from brutex.metrics import (
    ClosedTrade,
    EquityCurve,
    Moments,
    ResultMetrics,
    compute_metrics,
)
from brutex.metrics.compute import (
    TradeFolds,
    _accumulate_moments,
    _consecutive_runs,
    _drawdown_stats,
    _excess_kurtosis,
    _fold_trades,
    _fold_wire,
    _folds_from_wire,
    _percentile,
    _resolve_folds,
    _skewness,
    combine_metrics_from_folds,
    compute_metrics_from_scalars,
    with_stage4_statistics,
)
from brutex.metrics.deflated_sharpe import GAUSSIAN_KURTOSIS, deflated_sharpe_ratio
from brutex.metrics.pbo import pbo_probability
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


# ── ClosedTrade VIX reference stamp (DEC-VIX-TRADE-STAMP-001) ────────


def test_closed_trade_vix_defaults_to_none() -> None:
    """A trade built without VIX carries ``None`` for both VIX fields."""
    trade = _trade(100)
    assert trade.vix_at_entry is None
    assert trade.vix_at_exit is None


def test_closed_trade_accepts_decimal_vix_levels() -> None:
    """A finite Decimal VIX level at entry + exit is preserved at full precision."""
    trade = ClosedTrade(
        pnl_paisa=1,
        mfe_paisa=0,
        mae_paisa=0,
        holding_bars=1,
        entry_ts=_T0,
        exit_ts=_T0 + timedelta(minutes=1),
        direction=Side.BUY,
        vix_at_entry=Decimal("14.123456789"),
        vix_at_exit=Decimal("15.987654321"),
    )
    # Full precision — VIX is a metric, never money-rounded (CLAUDE.md §4).
    assert trade.vix_at_entry == Decimal("14.123456789")
    assert trade.vix_at_exit == Decimal("15.987654321")


@pytest.mark.parametrize("bad", [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")])
def test_closed_trade_rejects_non_finite_vix_entry(bad: Decimal) -> None:
    """A NaN / inf ``vix_at_entry`` is rejected (a metric must stay finite)."""
    with pytest.raises(ValueError, match="vix_at_entry must be a finite Decimal"):
        ClosedTrade(
            pnl_paisa=1,
            mfe_paisa=0,
            mae_paisa=0,
            holding_bars=1,
            entry_ts=_T0,
            exit_ts=_T0 + timedelta(minutes=1),
            direction=Side.BUY,
            vix_at_entry=bad,
        )


@pytest.mark.parametrize("bad", [Decimal("NaN"), Decimal("Infinity")])
def test_closed_trade_rejects_non_finite_vix_exit(bad: Decimal) -> None:
    """A NaN / inf ``vix_at_exit`` is rejected (a metric must stay finite)."""
    with pytest.raises(ValueError, match="vix_at_exit must be a finite Decimal"):
        ClosedTrade(
            pnl_paisa=1,
            mfe_paisa=0,
            mae_paisa=0,
            holding_bars=1,
            entry_ts=_T0,
            exit_ts=_T0 + timedelta(minutes=1),
            direction=Side.BUY,
            vix_at_exit=bad,
        )


def test_closed_trade_vix_survives_dataclass_serialization_round_trip() -> None:
    """The VIX stamp round-trips through ``dataclasses.asdict`` / reconstruction.

    ``ClosedTrade`` is not persisted to Parquet directly (the per-combo
    ``ResultRow`` aggregate is), so its serialization contract is the frozen
    dataclass surface: a field-dict round-trip must preserve the VIX levels
    exactly (Decimal, full precision).
    """
    original = ClosedTrade(
        pnl_paisa=42,
        mfe_paisa=7,
        mae_paisa=3,
        holding_bars=4,
        entry_ts=_T0,
        exit_ts=_T0 + timedelta(minutes=4),
        direction=Side.SELL,
        vix_at_entry=Decimal("13.25"),
        vix_at_exit=Decimal("13.40"),
    )
    rebuilt = ClosedTrade(**dataclasses.asdict(original))
    assert rebuilt == original
    assert rebuilt.vix_at_entry == Decimal("13.25")
    assert rebuilt.vix_at_exit == Decimal("13.40")


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


@pytest.mark.parametrize("bad_span", [float("nan"), float("inf")])
def test_equity_curve_rejects_non_finite_span(bad_span: float) -> None:
    """A NaN/inf span is rejected (DEC-METRICS-NONFINITE-GUARD-001).

    Without the finiteness guard ``nan <= 0.0`` is ``False`` (so a NaN span
    would slip through and later annualise CAGR to NaN), and a ``+inf`` span
    would silently flatten CAGR to ~0 — both contradict the "no NaN/inf"
    contract. The integer paisa fields are not tested for NaN: they are typed
    ``int`` and cannot carry a non-finite value (mypy --strict enforces it).
    """
    with pytest.raises(ValueError, match="span_years must be > 0"):
        EquityCurve(equity_paisa=(1,), start_equity_paisa=1, span_years=bad_span)


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
    """Information ratio = (mean per-trade return − per-trade benchmark) / std.

    Per-trade horizon (DEC-METRICS-001): the buy-and-hold benchmark is shared
    out per trade so the ratio is comparable across results with different
    trade counts.
    """
    trades = [_trade(20_000), _trade(-10_000), _trade(30_000)]
    rets = [0.02, -0.01, 0.03]
    mean = sum(rets) / 3
    std = math.sqrt(sum((r - mean) ** 2 for r in rets) / 3)
    benchmark_per_trade = (1.0 / 100.0) / 3  # 1.0% buy-and-hold, shared per trade
    m = compute_metrics(trades, _curve([1_000_000]), total_bars=10, benchmark_return_pct=1.0)
    assert m.information_ratio == pytest.approx((mean - benchmark_per_trade) / std)


def test_information_ratio_zero_benchmark_equals_sharpe() -> None:
    """With the default zero benchmark, IR collapses to the Sharpe ratio."""
    trades = [_trade(20_000), _trade(-10_000), _trade(30_000)]
    m = compute_metrics(trades, _curve([1_000_000]), total_bars=10)
    assert m.information_ratio == pytest.approx(m.sharpe_ratio)


def test_cagr_tiny_span_stays_finite_and_capped() -> None:
    """A sub-day span annualises to a huge magnitude; it must NOT overflow or
    return inf — it is clamped to the finite CAGR cap (B1 regression,
    spec line 746 no-inf gate)."""
    trades = [_trade(50_000)]
    # A near-instant span makes growth ** (1/span) overflow float64; the guard
    # must catch the OverflowError and clamp instead of crashing.
    m = compute_metrics(trades, _curve([1_000_000], span=1.0e-6), total_bars=10)
    assert math.isfinite(m.cagr_pct)
    assert m.cagr_pct == pytest.approx(1.0e9)  # clamped to _CAGR_CAP_PCT
    # calmar (cagr / MDD%) must also stay finite.
    assert m.calmar_ratio is None or math.isfinite(m.calmar_ratio)


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


# ════════════════════════════════════════════════════════════════════
# Mutation-hardening (DEC-MUTATION-GATE-RANKING-001)
#
# These pin the EXACT arithmetic of the metrics that drive strategy
# selection so the weekly mutation gate (mutation.yml) finds zero
# surviving mutants. The pre-existing tests above assert structure and
# approximate values; the assertions below pin operator choices
# (``//`` vs ``/``, ``*`` vs ``/``, ``+`` vs ``-``), comparison
# boundaries (``<`` vs ``<=``), exact constants (powers, thresholds),
# the all-zero empty row, dataclass immutability/slots, and the exact
# error text — each of which a silent regression would otherwise slip
# past. A wrong Sharpe / skewness / drawdown / integer-division here
# corrupts the leaderboard ranking, so the gate is a correctness gate.
# ════════════════════════════════════════════════════════════════════


# ── _percentile: exact linear interpolation ─────────────────────────


def test_percentile_two_element_midpoint() -> None:
    """Two-element p50 interpolates to the midpoint (kills the ``len == 1``
    short-circuit mutant, which would return the first element instead)."""
    assert _percentile([2.0, 8.0], 50.0) == 5.0


def test_percentile_interpolates_between_neighbours() -> None:
    """A non-integer rank interpolates exactly between the bracketing values.

    rank = 0.5·3 = 1.5 → lower=1, upper=2, frac=0.5 →
    10·(1−0.5) + 20·0.5 = 15.0. Pins every operator in the interpolation
    expression and the ``lower == upper`` branch direction.
    """
    assert _percentile([0.0, 10.0, 20.0, 30.0], 50.0) == 15.0


def test_percentile_integer_rank_hits_exact_element() -> None:
    """An integer rank returns the exact element (the ``lower == upper`` path)."""
    assert _percentile([0.0, 10.0, 20.0, 30.0, 40.0], 50.0) == 20.0


def test_public_percentile_is_the_promoted_alias() -> None:
    """The public ``percentile`` (DEC-SOAK-REAL-TIER-001) is the same object as
    the back-compat ``_percentile`` alias — the ONE COMMON convention the soak
    drift monitor shares (Golden Rule #11)."""
    from brutex.metrics.compute import percentile

    assert percentile is _percentile
    assert percentile([2.0, 8.0], 50.0) == 5.0


# ── _drawdown_stats: comparison boundaries ──────────────────────────


def test_drawdown_duration_counts_drop_of_exactly_one() -> None:
    """A peak-to-value drop of exactly 1 paisa still counts as a drawdown bar.

    Kills the ``drop > 0`` → ``drop > 1`` mutant: at a 1-paisa drop the
    original increments the duration counter, the mutant resets it.
    """
    _dd_paisa, _dd_pct, dd_bars, _ulcer = _drawdown_stats(
        EquityCurve(equity_paisa=(1000, 1000, 999), start_equity_paisa=1000, span_years=1.0)
    )
    assert dd_bars == 1


def test_drawdown_pct_zero_peak_is_skipped_not_divided() -> None:
    """A non-positive running peak yields 0% drawdown, never a 0/0 division.

    Kills the ``peak > 0`` → ``peak >= 0`` mutant: at peak == 0 the original
    skips the percentage (cannot express as a fraction of zero); the mutant
    would evaluate ``drop / 0`` and raise.
    """
    _dd_paisa, dd_pct, _dd_bars, ulcer = _drawdown_stats(
        EquityCurve(equity_paisa=(0, -100), start_equity_paisa=1000, span_years=1.0)
    )
    assert dd_pct == 0.0
    assert ulcer == 0.0


def test_drawdown_pct_peak_of_one_is_included() -> None:
    """A running peak of exactly 1 IS a positive peak and yields a real %.

    Kills the ``peak > 0`` → ``peak > 1`` mutant: equity [1, 0] drops the full
    peak, so max_drawdown_pct must be 100%, not 0.
    """
    _dd_paisa, dd_pct, _dd_bars, _ulcer = _drawdown_stats(
        EquityCurve(equity_paisa=(1, 0), start_equity_paisa=1000, span_years=1.0)
    )
    assert dd_pct == 100.0


# ── _consecutive_runs: streak counters + resets ─────────────────────


def test_consecutive_runs_all_wins() -> None:
    """All wins ⇒ (0 losses, 3 wins). Pins the ``max_losses`` / ``cur_*``
    initialisers to 0 (a 1-init would report a phantom loss streak)."""
    assert _consecutive_runs([_trade(1), _trade(1), _trade(1)]) == (0, 3)


def test_consecutive_runs_all_losses() -> None:
    """All losses ⇒ (3 losses, 0 wins). Pins the ``max_wins`` initialiser."""
    assert _consecutive_runs([_trade(-1), _trade(-1), _trade(-1)]) == (3, 0)


def test_consecutive_runs_single_loss() -> None:
    """A single loss ⇒ (1, 0). Pins the ``cur_losses`` initialiser to 0
    (a 1-init would double-count the first loss to a streak of 2)."""
    assert _consecutive_runs([_trade(-1)]) == (1, 0)


def test_consecutive_runs_win_resets_loss_streak_and_vice_versa() -> None:
    """A win between losses resets the loss streak (and vice versa).

    [loss, loss, win, loss] ⇒ max 2 consecutive losses, 1 win. Kills the
    streak-reset mutants (``cur_wins = 0`` / ``cur_losses = 0`` inside the
    win/loss branches): without the reset the streak would not break.
    """
    assert _consecutive_runs([_trade(-1), _trade(-1), _trade(1), _trade(-1)]) == (2, 1)


def test_consecutive_runs_scratch_breaks_both_streaks() -> None:
    """A scratch (pnl == 0) trade resets BOTH streaks.

    [loss, scratch, loss] ⇒ max 1 consecutive loss (the scratch breaks the
    run). Kills the else-branch reset mutants (``cur_wins = 0`` /
    ``cur_losses = 0`` for the scratch case).
    """
    assert _consecutive_runs([_trade(-1), _trade(0), _trade(-1)]) == (1, 0)


# ── _skewness / _excess_kurtosis / Moments: exact moment math ───────

# Series [0, 0, 0, 4]: mean=1, m2=12, m3=24, m4=84 →
#   skewness        = (24/4) / (12/4)**1.5 = 6 / 3**1.5      = 1.1547005383792515
#   excess kurtosis = (84/4) / (12/4)**2 − 3 = 21/9 − 3      = -0.6666666666666667
_SKEW_SERIES = [0.0, 0.0, 0.0, 4.0]
_SKEW_GOLDEN = 6.0 / 3.0**1.5
_KURT_GOLDEN = 21.0 / 9.0 - 3.0


def test_skewness_exact_golden() -> None:
    """Population skewness pins ``m3/n / (m2/n)**1.5`` exactly (kills the
    ``/``→``*`` and ``1.5``→``2.5`` mutants in the moment arithmetic)."""
    skew = _skewness(_accumulate_moments(_SKEW_SERIES))
    assert skew == pytest.approx(_SKEW_GOLDEN, rel=1e-12)


def test_excess_kurtosis_exact_golden() -> None:
    """Population excess kurtosis pins ``m4/n / (m2/n)**2 − 3`` exactly."""
    kurt = _excess_kurtosis(_accumulate_moments(_SKEW_SERIES))
    assert kurt == pytest.approx(_KURT_GOLDEN, rel=1e-12)


def test_moments_variance_defined_at_n_equals_two() -> None:
    """``n == 2`` is the lower bound for a defined variance / skewness.

    Kills the ``_MIN_SAMPLES_VARIANCE`` (2→3) and the ``n < MIN`` → ``<=``
    boundary mutants in ``Moments.variance`` / ``_skewness`` / ``_excess_kurtosis``.
    """
    mom = _accumulate_moments([1.0, 3.0])  # n=2, mean=2, m2=2
    assert mom.n == 2
    assert mom.variance == 1.0
    assert mom.std_dev == 1.0
    # n=2 with non-zero variance ⇒ skewness / kurtosis are defined (not None).
    assert _skewness(mom) is not None
    assert _excess_kurtosis(mom) is not None


def test_moments_uses_slots() -> None:
    """``Moments`` is ``slots=True`` — kills the ``slots`` flip."""
    mom = Moments(n=1, mean=0.0, m2=0.0, m3=0.0, m4=0.0)
    assert not hasattr(mom, "__dict__")
    assert hasattr(Moments, "__slots__")


# ── compute_metrics: exact integer division + counts ────────────────


def _golden_trades() -> list[ClosedTrade]:
    """Two wins then two losses with deliberately non-divisible aggregates.

    Chosen so floor- vs true-division differ in the *value* (not just the
    type): gross_profit=401 / winners=2, gross_loss=299 / losers=2,
    net=102 / 4, sum_mfe=826 / 4, sum_mae=603 / 4 are all non-integer
    under ``/`` but exact under ``//``.
    """
    return [
        _trade(301, mfe=505, mae=101, holding=10, offset_min=0),
        _trade(100, mfe=200, mae=50, holding=4, offset_min=20),
        _trade(-200, mfe=80, mae=301, holding=6, offset_min=40),
        _trade(-99, mfe=41, mae=151, holding=2, offset_min=60),
    ]


def test_compute_metrics_integer_division_is_floor() -> None:
    """Per-trade money averages use floor division (int paisa), pinned by value.

    Kills the ``//`` → ``/`` mutants on avg_win, avg_loss, expectancy,
    avg_mfe and avg_mae: under true division these would be …5 / …75 floats,
    not the floored integers asserted here.
    """
    m = compute_metrics(
        _golden_trades(),
        _curve([1_000_000, 1_000_301, 1_000_401, 1_000_201, 1_000_102]),
        total_bars=200,
    )
    assert m.avg_winning_trade_paisa == 200  # 401 // 2
    assert m.avg_losing_trade_paisa == 149  # 299 // 2
    assert m.expectancy_paisa == 25  # 102 // 4
    assert m.avg_mfe_paisa == 206  # 826 // 4
    assert m.avg_mae_paisa == 150  # 603 // 4
    # Counts / money the same scenario pins (kills counting/sum mutants).
    assert m.net_pnl_paisa == 102
    assert m.gross_profit_paisa == 401
    assert m.gross_loss_paisa == 299
    assert m.winning_trades == 2
    assert m.losing_trades == 2
    assert m.total_trades == 4
    assert m.max_consecutive_wins == 2
    assert m.max_consecutive_losses == 2
    assert m.largest_losing_trade_paisa == -200
    assert m.max_holding_time_bars == 10
    assert m.win_rate == 0.5


def test_compute_metrics_total_bars_one_is_accepted() -> None:
    """``total_bars == 1`` is the valid lower bound and must NOT raise.

    Kills the ``total_bars < 1`` → ``<= 1`` / ``< 2`` mutants.
    """
    m = compute_metrics([_trade(10)], _curve([1_000_000, 1_000_010]), total_bars=1)
    assert m.time_in_market_pct == 100.0  # clamped


def test_compute_metrics_total_bars_zero_message_is_exact() -> None:
    """The ``total_bars`` guard message is pinned byte-exact."""
    with pytest.raises(ValueError) as exc:
        compute_metrics([_trade(10)], _curve([1_000_000]), total_bars=0)
    assert str(exc.value) == "total_bars must be >= 1, got 0"


def test_empty_metrics_row_is_all_zero() -> None:
    """A zero-trade result is exactly zero in every count/money/ratio-able
    field (kills the ``_empty_metrics`` ``0`` → ``1`` literal mutants)."""
    m = compute_metrics([], _curve([1_000_000, 1_000_000]), total_bars=10)
    assert m.total_return_pct == 0.0
    assert m.cagr_pct == 0.0
    assert m.gross_profit_paisa == 0
    assert m.gross_loss_paisa == 0
    assert m.largest_losing_trade_paisa == 0
    assert m.max_consecutive_losses == 0
    assert m.max_consecutive_wins == 0
    assert m.winning_trades == 0
    assert m.losing_trades == 0
    assert m.avg_winning_trade_paisa == 0
    assert m.avg_losing_trade_paisa == 0
    assert m.expectancy_paisa == 0
    assert m.avg_mfe_paisa == 0
    assert m.avg_mae_paisa == 0
    assert m.avg_holding_time_bars == 0.0
    assert m.max_holding_time_bars == 0
    assert m.net_pnl_paisa == 0
    assert m.total_trades == 0


# ── with_stage4_statistics: DSR + PBO gating and exact values ────────


def _stage4_metrics(**over: object) -> ResultMetrics:
    """A metrics row whose Stage-4 DSR inputs (Sharpe/skew/kurt/trades) are
    all defined, so :func:`with_stage4_statistics` computes a real DSR."""
    m = compute_metrics(_golden_trades(), _curve([1_000_000] * 5), total_bars=200)
    defaults: dict[str, object] = {
        "sharpe_ratio": 1.2,
        "skewness": -0.3,
        "kurtosis": 2.0,
        "total_trades": 120,
    }
    defaults.update(over)
    return dataclasses.replace(m, **defaults)


def test_stage4_dsr_exact_value_and_kurtosis_offset() -> None:
    """The filled DSR matches an independent ``deflated_sharpe_ratio`` call.

    Pins (a) that the gate computes the DSR at all, and (b) the
    ``kurtosis + GAUSSIAN_KURTOSIS`` excess→raw conversion (kills the
    ``+`` → ``-`` mutant and the ``dsr = None`` body-replacement mutant).
    """
    metrics = _stage4_metrics()
    result = with_stage4_statistics(metrics, n_trials=500, trial_sharpe_variance=0.4)
    expected = deflated_sharpe_ratio(
        1.2,
        n_trials=500,
        trial_sharpe_variance=0.4,
        sample_length=120,
        skew=-0.3,
        kurtosis=2.0 + GAUSSIAN_KURTOSIS,
    ).deflated_sharpe_ratio
    assert result.deflated_sharpe_ratio == expected
    assert result.deflated_sharpe_ratio is not None


def test_stage4_dsr_none_when_sharpe_missing() -> None:
    """DSR stays NULL (None, not ``""``) when the Sharpe is undefined.

    Kills the ``dsr: float | None = ""`` default-literal mutant and the
    ``sharpe_ratio is not None`` → ``is None`` gate-flip mutant.
    """
    metrics = _stage4_metrics(sharpe_ratio=None)
    result = with_stage4_statistics(metrics, n_trials=500, trial_sharpe_variance=0.4)
    assert result.deflated_sharpe_ratio is None


def test_stage4_dsr_none_when_skewness_missing() -> None:
    """DSR stays None when skewness is undefined (kills the skewness gate-flip
    AND the ``and`` → ``or`` mutant, which would attempt a DSR with skew=None)."""
    metrics = _stage4_metrics(skewness=None)
    result = with_stage4_statistics(metrics, n_trials=500, trial_sharpe_variance=0.4)
    assert result.deflated_sharpe_ratio is None


def test_stage4_dsr_none_when_kurtosis_missing() -> None:
    """DSR stays None when kurtosis is undefined (kills the kurtosis gate-flip)."""
    metrics = _stage4_metrics(kurtosis=None)
    result = with_stage4_statistics(metrics, n_trials=500, trial_sharpe_variance=0.4)
    assert result.deflated_sharpe_ratio is None


def test_stage4_dsr_computed_at_exactly_two_trades() -> None:
    """``total_trades == _MIN_SAMPLES_VARIANCE`` (2) is enough for the DSR.

    Kills the ``total_trades >= 2`` → ``> 2`` boundary mutant: at exactly two
    trades the DSR is still defined and must be filled.
    """
    metrics = _stage4_metrics(total_trades=2)
    result = with_stage4_statistics(metrics, n_trials=10, trial_sharpe_variance=0.2)
    assert result.deflated_sharpe_ratio is not None


def test_stage4_pbo_none_without_performance_matrix() -> None:
    """PBO stays None (not ``""``) when no CSCV matrix is supplied.

    Kills the ``pbo: float | None = ""`` default-literal mutant.
    """
    result = with_stage4_statistics(_stage4_metrics(), n_trials=50, trial_sharpe_variance=0.3)
    assert result.pbo_probability is None


def test_stage4_pbo_uses_default_sixteen_splits() -> None:
    """A supplied CSCV matrix fills PBO using the default S=16 splits.

    Kills the ``pbo_performance is not None`` → ``is None`` gate-flip, the
    ``pbo = None`` body-replacement, and the ``pbo_n_splits = 16`` → ``17``
    default mutant (the value must equal the explicit-16 reference).
    """
    # A 16×4 CSCV-style performance matrix (T rows × N strategies); the row
    # count must be a multiple of the default S=16 splits.
    base = [
        [0.5, 0.1, -0.2, 0.3],
        [0.4, 0.2, -0.1, 0.2],
        [0.6, 0.0, -0.3, 0.4],
        [0.3, 0.3, -0.2, 0.1],
        [0.5, 0.1, -0.1, 0.3],
        [0.4, 0.2, -0.2, 0.2],
        [0.6, 0.0, -0.3, 0.5],
        [0.2, 0.4, -0.1, 0.1],
    ]
    matrix = base + [[a + 0.01, b, c, d] for a, b, c, d in base]
    result = with_stage4_statistics(
        _stage4_metrics(),
        n_trials=50,
        trial_sharpe_variance=0.3,
        pbo_performance=matrix,
    )
    assert result.pbo_probability is not None
    assert result.pbo_probability == pbo_probability(matrix, n_splits=16).pbo


# ── DEC-MUTATION-GATE-COMPUTE-001: mutation-survivor kill pins ──────


def test_compute_rows_stage4_defaults_are_none() -> None:
    """Both Stage-4 fields default to exactly ``None`` on every compute row.

    Kills the dataclass default-literal mutants (``None`` → ``""``) on
    ``deflated_sharpe_ratio`` / ``pbo_probability`` — named as the pinned
    protection in the field-line pragma justification. Checks the
    zero-trade row AND a traded row (both construction paths omit the
    two Stage-4 kwargs).
    """
    empty = compute_metrics([], _curve([1_000_000]), total_bars=1)
    traded = compute_metrics([_trade(10)], _curve([1_000_000, 1_000_010]), total_bars=10)
    for row in (empty, traded):
        assert row.deflated_sharpe_ratio is None
        assert row.pbo_probability is None


def test_avg_mfe_mae_single_trade_exact() -> None:
    """With one trade the MFE/MAE averages equal the trade's values exactly.

    Kills the accumulator-initialiser mutants (``sum_mfe = 0`` → ``1``,
    ``sum_mae = 0`` → ``1``): at ``n == 1`` the floor division cannot
    absorb an off-by-one in the sum.
    """
    m = compute_metrics(
        [_trade(10, mfe=7, mae=3)],
        _curve([1_000_000, 1_000_010]),
        total_bars=10,
    )
    assert m.avg_mfe_paisa == 7
    assert m.avg_mae_paisa == 3


def test_largest_losing_trade_zero_when_no_losers() -> None:
    """An all-winner result reports ``largest_losing_trade_paisa == 0``.

    Kills the ``largest_loss = 0`` → ``1`` initialiser mutant: with no
    losers the initial value IS the reported value.
    """
    m = compute_metrics(
        [_trade(100), _trade(50)],
        _curve([1_000_000, 1_000_100, 1_000_150]),
        total_bars=10,
    )
    assert m.largest_losing_trade_paisa == 0


def test_cagr_terminal_equity_one_paisa_not_ruin() -> None:
    """``end_equity == 1`` (one paisa left) is NOT the ruin branch.

    Kills the ruin-boundary comparison mutants (``< 1`` → ``<= 1`` /
    ``< 2``): under either flip this row would clamp to exactly -100.0,
    but the real else-branch computes the annualised figure from
    ``growth == 1 / 1_000_000``.
    """
    m = compute_metrics(
        [_trade(-999_999)],
        _curve([1_000_000, 1]),
        total_bars=10,
    )
    growth = 1 / 1_000_000
    assert m.cagr_pct == 100.0 * (growth - 1.0)
    assert m.cagr_pct != -100.0


def test_cagr_terminal_equity_zero_is_ruin() -> None:
    """``end_equity == 0`` takes the ruin branch and clamps to exactly -100.0."""
    m = compute_metrics(
        [_trade(-1_000_000)],
        _curve([1_000_000, 0]),
        total_bars=10,
    )
    assert m.cagr_pct == -100.0


def test_sortino_exact_value_excludes_scratch_and_positive() -> None:
    """Sortino downside sum is byte-exact over ONLY the negative returns.

    Kills the downside-filter threshold mutants (``0.0`` → a positive
    threshold would admit the +0.02 / +0.03 returns; named as the pinned
    protection in the filter-line pragma justification) and pins that a
    scratch trade (pnl == 0) contributes nothing. The expectation
    replicates the implementation's expressions exactly (same Welford
    mean, same single-element RMS), so the assertion is ``==``, not
    ``approx``.
    """
    trades = [_trade(20_000), _trade(-10_000), _trade(0), _trade(30_000)]
    m = compute_metrics(trades, _curve([1_000_000]), total_bars=10)
    rets = [20_000 / 1_000_000, -10_000 / 1_000_000, 0 / 1_000_000, 30_000 / 1_000_000]
    mom = _accumulate_moments(rets)
    neg = -10_000 / 1_000_000
    downside_dev = math.sqrt((neg * neg) / 4)
    assert m.sortino_ratio == mom.mean / downside_dev


def test_result_metrics_uses_slots() -> None:
    """``ResultMetrics`` is ``slots=True`` — kills the ``slots`` flip."""
    m = compute_metrics([], _curve([1_000_000]), total_bars=1)
    assert not hasattr(m, "__dict__")
    assert hasattr(ResultMetrics, "__slots__")


# ── DEC-SWEEP-RUST-METRICS-001: drawdown injection + curve-less scalars ──


def test_compute_metrics_injected_drawdown_equals_default_traded() -> None:
    """``drawdown=_drawdown_stats(equity)`` is byte-identical to the default.

    The injection seam's contract: supplying the internally-computed tuple
    changes NOTHING (the flag-OFF sweep path is the ``None`` arm).
    """
    trades = [_trade(20_000), _trade(-10_000, mae=10_000)]
    curve = _curve([1_010_000, 1_010_000, 1_000_000, 1_010_000])
    default = compute_metrics(trades, curve, total_bars=4)
    injected = compute_metrics(trades, curve, total_bars=4, drawdown=_drawdown_stats(curve))
    assert default == injected


def test_compute_metrics_injected_drawdown_equals_default_zero_trade() -> None:
    """The zero-trade branch honours the injected tuple identically."""
    curve = _curve([1_000_000, 990_000, 1_000_000])
    default = compute_metrics([], curve, total_bars=3)
    injected = compute_metrics([], curve, total_bars=3, drawdown=_drawdown_stats(curve))
    assert default == injected


def test_compute_metrics_injected_drawdown_is_actually_used() -> None:
    """A sentinel tuple flows into the four drawdown fields verbatim.

    Kills the "silently recompute / ignore the parameter" mutant family:
    the sentinel values are distinct from anything the flat curve yields.
    """
    curve = _curve([1_000_000, 1_000_000])
    sentinel = (12_345, 6.25, 7, 3.5)
    m = compute_metrics([_trade(10_000)], curve, total_bars=2, drawdown=sentinel)
    assert m.max_drawdown_paisa == 12_345
    assert m.max_drawdown_pct == 6.25
    assert m.max_drawdown_duration_bars == 7
    assert m.ulcer_index == 3.5
    # recovery_factor divides by the INJECTED max_dd_paisa.
    assert m.recovery_factor == 10_000.0 / 12_345.0
    z = compute_metrics([], curve, total_bars=2, drawdown=sentinel)
    assert z.max_drawdown_paisa == 12_345
    assert z.max_drawdown_pct == 6.25
    assert z.max_drawdown_duration_bars == 7
    assert z.ulcer_index == 3.5


def test_compute_metrics_from_scalars_equals_curve_entry_traded() -> None:
    """The curve-less entry is byte-identical to ``compute_metrics`` on the
    same scalars — including a non-trivial benchmark baseline."""
    trades = [_trade(20_000), _trade(-10_000, mae=10_000), _trade(5_000)]
    curve = _curve([1_020_000, 1_010_000, 1_015_000], start=1_000_000, span=0.5)
    via_curve = compute_metrics(trades, curve, 3, 4.0)
    via_scalars = compute_metrics_from_scalars(
        trades,
        3,
        4.0,
        start_equity_paisa=1_000_000,
        span_years=0.5,
        drawdown=_drawdown_stats(curve),
    )
    assert via_curve == via_scalars


def test_compute_metrics_from_scalars_equals_curve_entry_zero_trade() -> None:
    """Zero-trade arm: curve-less entry == curve entry."""
    curve = _curve([1_000_000, 990_000], start=1_000_000, span=2.0)
    via_curve = compute_metrics([], curve, total_bars=2)
    via_scalars = compute_metrics_from_scalars(
        [],
        2,
        start_equity_paisa=1_000_000,
        span_years=2.0,
        drawdown=_drawdown_stats(curve),
    )
    assert via_curve == via_scalars


def test_compute_metrics_from_scalars_rejects_nonpositive_start() -> None:
    """The bypassed EquityCurve start invariant is re-asserted (belt)."""
    for start in (0, -1):
        with pytest.raises(ValueError, match="start_equity_paisa must be > 0"):
            compute_metrics_from_scalars(
                [],
                1,
                start_equity_paisa=start,
                span_years=1.0,
                drawdown=(0, 0.0, 0, 0.0),
            )


def test_compute_metrics_from_scalars_rejects_bad_span() -> None:
    """The bypassed EquityCurve span invariant is re-asserted (belt)."""
    for span in (0.0, -1.0, math.nan, math.inf):
        with pytest.raises(ValueError, match="span_years must be > 0"):
            compute_metrics_from_scalars(
                [],
                1,
                start_equity_paisa=1_000_000,
                span_years=span,
                drawdown=(0, 0.0, 0, 0.0),
            )


def test_compute_metrics_from_scalars_rejects_nonpositive_total_bars() -> None:
    """``total_bars < 1`` raises the same message as ``compute_metrics``."""
    with pytest.raises(ValueError, match="total_bars must be >= 1, got 0"):
        compute_metrics_from_scalars(
            [],
            0,
            start_equity_paisa=1_000_000,
            span_years=1.0,
            drawdown=(0, 0.0, 0, 0.0),
        )


# ── DEC-MUTATION-RUNTIME-003: scalars-entry mutation-survivor kills ──
# (MIGRATION.md #1597.) The 8 mutmut survivors 203, 205-207, 212-215 all
# live in compute_metrics_from_scalars (the #1186 scalars entry): its
# raise-tests used `pytest.raises(match=)` — which is re.search, so the
# XX-wrapped message mutants still matched — and no scalars call ever
# SUCCEEDED at the exact guard boundaries (start_equity_paisa=1,
# total_bars=1) or exercised the default benchmark_return_pct on a
# traded path. These pins use exact-message equality + succeeding
# boundary values + a curve-entry equality oracle (the same idiom that
# already kills the curve entry's equivalents at test:916/:925).


def test_compute_metrics_from_scalars_boundary_values_accepted() -> None:
    """start_equity_paisa == 1 and total_bars == 1 are the valid lower bounds.

    Kills mutants 205 (<= 0 -> <= 1), 213 (< 1 -> <= 1), 214 (< 1 -> < 2)
    (DEC-MUTATION-RUNTIME-003).
    """
    m = compute_metrics_from_scalars(
        [_trade(10)],
        1,
        start_equity_paisa=1,
        span_years=1.0,
        drawdown=(0, 0.0, 0, 0.0),
    )
    assert m.total_trades == 1
    assert m.time_in_market_pct == 100.0


def test_compute_metrics_from_scalars_start_equity_message_exact() -> None:
    """The start-equity guard message is pinned byte-exact (kills 206, 207)."""
    with pytest.raises(ValueError) as exc:
        compute_metrics_from_scalars(
            [],
            1,
            start_equity_paisa=0,
            span_years=1.0,
            drawdown=(0, 0.0, 0, 0.0),
        )
    assert str(exc.value) == ("compute_metrics_from_scalars: start_equity_paisa must be > 0, got 0")


def test_compute_metrics_from_scalars_span_message_exact() -> None:
    """The span guard message is pinned byte-exact (kills 212)."""
    with pytest.raises(ValueError) as exc:
        compute_metrics_from_scalars(
            [],
            1,
            start_equity_paisa=1_000_000,
            span_years=0.0,
            drawdown=(0, 0.0, 0, 0.0),
        )
    assert str(exc.value) == "compute_metrics_from_scalars: span_years must be > 0, got 0.0"


def test_compute_metrics_from_scalars_total_bars_message_exact() -> None:
    """The total_bars guard message is pinned byte-exact (kills 215)."""
    with pytest.raises(ValueError) as exc:
        compute_metrics_from_scalars(
            [],
            0,
            start_equity_paisa=1_000_000,
            span_years=1.0,
            drawdown=(0, 0.0, 0, 0.0),
        )
    assert str(exc.value) == "total_bars must be >= 1, got 0"


def test_compute_metrics_from_scalars_default_benchmark_is_zero() -> None:
    """The scalars entry's default benchmark is 0.0 — IR collapses to Sharpe.

    Kills mutant 203 (default 0.0 -> 1.0): with a nonzero default the
    information ratio would subtract a phantom per-trade benchmark.
    """
    trades = [_trade(20_000), _trade(-10_000), _trade(30_000)]
    curve = _curve([1_020_000, 1_010_000, 1_040_000])
    via_curve = compute_metrics(trades, curve, 10)
    via_scalars = compute_metrics_from_scalars(
        trades,
        10,
        start_equity_paisa=1_000_000,
        span_years=1.0,
        drawdown=_drawdown_stats(curve),
    )
    assert via_curve == via_scalars
    assert via_scalars.information_ratio == via_scalars.sharpe_ratio


# ── combine_metrics_from_folds n_trades guard (Slice-0 review fold) ──


def _zero_trade_folds(n_trades: int) -> TradeFolds:
    """Build a contract-violating :class:`TradeFolds` with the given count."""
    return TradeFolds(
        n_trades=n_trades,
        net_pnl_paisa=0,
        gross_profit_paisa=0,
        gross_loss_abs_paisa=0,
        winners=0,
        losers=0,
        sum_mfe_paisa=0,
        sum_mae_paisa=0,
        sum_holding_bars=0,
        max_holding_bars=0,
        largest_loss_paisa=0,
        max_consecutive_losses=0,
        max_consecutive_wins=0,
        moments=Moments(n=0, mean=0.0, m2=0.0, m3=0.0, m4=0.0),
        downside_sum_sq=0.0,
        p95=0.0,
        p5=0.0,
    )


class TestCombineFoldsGuard:
    """The public combine entry fails LOUD on contract misuse.

    DEC-SWEEP-COMPUTE-FOLD-DECOMP-001 round-1 review fold:
    ``combine_metrics_from_folds`` is public API (the Rust-core parity
    seam), so a zero-trade ``TradeFolds`` — a documented contract
    violation (the zero-trade row is ``_empty_metrics``, the caller's
    arm) — must raise a typed :class:`ValueError`, never an untyped
    ``ZeroDivisionError`` from deep inside the body.
    """

    def test_zero_trades_raises_typed_valueerror(self) -> None:
        with pytest.raises(ValueError) as exc:
            combine_metrics_from_folds(
                _zero_trade_folds(0),
                total_bars=10,
                benchmark_return_pct=0.0,
                start_equity_paisa=1_000_000,
                span_years=1.0,
                drawdown=(0, 0.0, 0, 0.0),
            )
        assert str(exc.value) == (
            "combine_metrics_from_folds: folds.n_trades must be >= 1, got 0 — "
            "the zero-trade row is _empty_metrics, the caller's arm"
        )

    def test_negative_trades_raises_typed_valueerror(self) -> None:
        with pytest.raises(ValueError, match="must be >= 1, got -3"):
            combine_metrics_from_folds(
                _zero_trade_folds(-3),
                total_bars=10,
                benchmark_return_pct=0.0,
                start_equity_paisa=1_000_000,
                span_years=1.0,
                drawdown=(0, 0.0, 0, 0.0),
            )

    def test_boundary_one_trade_passes_guard(self) -> None:
        """``n_trades == 1`` is VALID — kills a ``< 1`` → ``<= 1`` flip."""
        row = combine_metrics_from_folds(
            _fold_trades([_trade(500, mfe=600, mae=100, holding=3)], 1_000_000),
            total_bars=10,
            benchmark_return_pct=0.0,
            start_equity_paisa=1_000_000,
            span_years=1.0,
            drawdown=(100, 0.01, 2, 0.005),
        )
        assert row.total_trades == 1
        assert row.net_pnl_paisa == 500


class TestResolveFoldsDispatch:
    """The DEC-METRICS-RUST-COMPUTE-FOLD-001 dispatch chokepoint's mutant kills.

    The weekly mutation pair runs THIS file alone against ``compute.py``
    (mutation.yml — the #1685 recorded single-unit-file decision), so the
    ``_resolve_folds`` arms need their killing tests HERE; the fuller seam
    battery (flag matrix, degrade taxonomy, burn-in/autorevert) lives in
    ``tests/unit/test_metrics_compute_rust.py``.
    """

    @staticmethod
    def _dispatch_trades() -> list[ClosedTrade]:
        return [_trade(500, mfe=600, mae=100, holding=3), _trade(-300, mae=400, holding=2)]

    def test_flag_off_never_consults_the_kernel_seam(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Default path: the seam is provably untouched (never-routes spy)."""
        from brutex.metrics import compute_rust

        monkeypatch.delenv(compute_rust.METRICS_RUST_FOLD_ENV, raising=False)

        def _seam_bomb(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("the default path must never consult the kernel seam")

        monkeypatch.setattr(compute_rust, "fold_wire_via_kernel", _seam_bomb)
        trades = self._dispatch_trades()
        folds = _resolve_folds(trades, 1_000_000)
        assert folds == _fold_trades(trades, 1_000_000)

    def test_flag_on_kernel_wire_is_served(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Flag ON + a serving kernel: the served wire's rebuild ships."""
        from brutex.metrics import compute_rust

        monkeypatch.setenv(compute_rust.METRICS_RUST_FOLD_ENV, "1")
        sentinel_folds = _fold_trades([_trade(999, holding=1)], 500)
        sentinel_wire = _fold_wire(sentinel_folds)

        def _fake_seam(*_args: object, **_kwargs: object) -> object:
            return sentinel_wire

        monkeypatch.setattr(compute_rust, "fold_wire_via_kernel", _fake_seam)
        served = _resolve_folds(self._dispatch_trades(), 1_000_000)
        assert served == _folds_from_wire(sentinel_wire)
        assert served == sentinel_folds  # int-0 downside ≡ +0.0 (dataclass ==)

    def test_flag_on_dispatch_injects_the_matching_oracle_wire(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The injected ``oracle_wire`` computes the Python fold's wire for
        EXACTLY the dispatched arguments (the burn-in's correctness anchor —
        a wrong injection would autorevert a correct kernel)."""
        from brutex.metrics import compute_rust

        monkeypatch.setenv(compute_rust.METRICS_RUST_FOLD_ENV, "1")
        seen: dict[str, object] = {}

        def _capture_seam(trades: object, start: object, *, oracle_wire: object) -> None:
            seen["trades"] = trades
            seen["start"] = start
            seen["oracle_wire"] = oracle_wire

        monkeypatch.setattr(compute_rust, "fold_wire_via_kernel", _capture_seam)
        trades = self._dispatch_trades()
        _resolve_folds(trades, 1_000_000)
        assert seen["trades"] is trades
        assert seen["start"] == 1_000_000
        oracle_wire = seen["oracle_wire"]
        assert callable(oracle_wire)
        assert oracle_wire() == _fold_wire(_fold_trades(trades, 1_000_000))

    def test_flag_on_kernel_unavailable_falls_to_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Flag ON + a degraded kernel (``None``): the Python oracle serves."""
        from brutex.metrics import compute_rust

        monkeypatch.setenv(compute_rust.METRICS_RUST_FOLD_ENV, "1")
        monkeypatch.setattr(compute_rust, "fold_wire_via_kernel", lambda *_a, **_k: None)
        trades = self._dispatch_trades()
        folds = _resolve_folds(trades, 1_000_000)
        assert folds == _fold_trades(trades, 1_000_000)


class TestFoldWireRoundTrip:
    """`_fold_wire` ↔ `_folds_from_wire` — the plain-data boundary's pair laws.

    The ALL-DISTINCT fixture kills every wire-slot index-swap mutant: any
    two swapped slots change the rebuilt ``TradeFolds`` because no two
    field values collide anywhere in the fixture.
    """

    _DISTINCT = TradeFolds(
        n_trades=11,
        net_pnl_paisa=1201,
        gross_profit_paisa=2402,
        gross_loss_abs_paisa=3603,
        winners=4,
        losers=5,
        sum_mfe_paisa=4804,
        sum_mae_paisa=5905,
        sum_holding_bars=67,
        max_holding_bars=23,
        largest_loss_paisa=-7107,
        max_consecutive_losses=2,
        max_consecutive_wins=3,
        moments=Moments(n=11, mean=0.125, m2=0.25, m3=-0.375, m4=0.4375),
        downside_sum_sq=0.017,
        p95=0.921,
        p5=-0.843,
    )

    def test_round_trip_is_identity_on_all_distinct_fields(self) -> None:
        folds = self._DISTINCT
        # The fixture's mutant-killing precondition: no two int slots equal,
        # no two float slots equal (an index swap can never hide).
        counts, sums, moments_wire, floats = _fold_wire(folds)
        # moments.n == n_trades is a LAW of the wire (`_valid_fold_wire`
        # enforces it), so that one pair is deliberately equal — a swap
        # between those two slots is value-equivalent by construction.
        assert moments_wire[0] == counts[0]
        int_slots = [*counts, *sums]
        assert len(set(int_slots)) == len(int_slots)
        float_slots = [*moments_wire[1:], *floats]
        assert len(set(float_slots)) == len(float_slots)
        assert _folds_from_wire(_fold_wire(folds)) == folds

    def test_projection_float_normalises_int_zero_downside(self) -> None:
        """The fold's legitimate `sum([])` int-``0`` crosses as ``+0.0``."""
        folds = _fold_trades([_trade(7, holding=1), _trade(9, holding=2)], 100)
        assert isinstance(folds.downside_sum_sq, int)
        wire = _fold_wire(folds)
        assert isinstance(wire[3][0], float)
        assert wire[3][0] == 0.0


# ── sl_hit_rate at COMBINE (DEC-METRICS-LOSSRATE-FIRSTCLASS-001 Stage B) ─────


class TestSlHitRate:
    """The exit-quality COMBINE arithmetic + its honesty/refusal laws."""

    def test_counters_threaded_yield_concrete_rate(self) -> None:
        trades = [_trade(100), _trade(-50, offset_min=10), _trade(-30, offset_min=20)]
        curve = _curve([1_000_000, 1_000_100, 1_000_050, 1_000_020])
        m = compute_metrics(trades, curve, total_bars=4, sl_exit_count=2)
        assert m.sl_hit_rate == 2 / 3

    def test_absent_counters_yield_honest_none(self) -> None:
        """No counters ⇒ None on a non-empty trade set — never a fabricated 0.0."""
        trades = [_trade(100), _trade(-50, offset_min=10)]
        curve = _curve([1_000_000, 1_000_100, 1_000_050])
        m = compute_metrics(trades, curve, total_bars=3)
        assert m.sl_hit_rate is None

    def test_zero_trades_yield_vacuous_zero(self) -> None:
        """Zero trades ⇒ 0.0 (0 SL exits of 0 trades — the win_rate precedent)."""
        curve = _curve([1_000_000, 1_000_000])
        m = compute_metrics([], curve, total_bars=2)
        assert m.sl_hit_rate == 0.0

    def test_zero_sl_count_is_concrete_zero_not_none(self) -> None:
        """sl_exit_count=0 with counters PRESENT is a measured 0.0, not None."""
        trades = [_trade(100)]
        curve = _curve([1_000_000, 1_000_100])
        m = compute_metrics(trades, curve, total_bars=2, sl_exit_count=0)
        assert m.sl_hit_rate == 0.0

    def test_out_of_range_count_refused_at_combine(self) -> None:
        folds = _fold_trades([_trade(-50)], 1_000_000)
        with pytest.raises(ValueError, match="sl_exit_count=2 is outside"):
            combine_metrics_from_folds(
                folds,
                total_bars=2,
                benchmark_return_pct=0.0,
                start_equity_paisa=1_000_000,
                span_years=1.0,
                drawdown=(0, 0.0, 0, 0.0),
                sl_exit_count=2,
            )
        with pytest.raises(ValueError, match="sl_exit_count=-1 is outside"):
            combine_metrics_from_folds(
                folds,
                total_bars=2,
                benchmark_return_pct=0.0,
                start_equity_paisa=1_000_000,
                span_years=1.0,
                drawdown=(0, 0.0, 0, 0.0),
                sl_exit_count=-1,
            )

    def test_positive_count_over_zero_trades_refused(self) -> None:
        curve = _curve([1_000_000, 1_000_000])
        with pytest.raises(ValueError, match="zero-trade result"):
            compute_metrics([], curve, total_bars=2, sl_exit_count=1)

    def test_from_scalars_threads_the_count(self) -> None:
        """The curve-less entry threads sl_exit_count identically."""
        trades = [_trade(100), _trade(-50, offset_min=10)]
        curve = _curve([1_000_000, 1_000_100, 1_000_050])
        via_curve = compute_metrics(trades, curve, total_bars=3, sl_exit_count=1)
        via_scalars = compute_metrics_from_scalars(
            trades,
            3,
            start_equity_paisa=curve.start_equity_paisa,
            span_years=curve.span_years,
            drawdown=_drawdown_stats(curve),
            sl_exit_count=1,
        )
        assert via_scalars == via_curve
        assert via_scalars.sl_hit_rate == 0.5

    def test_full_rate_at_all_sl_exits(self) -> None:
        trades = [_trade(-50), _trade(-60, offset_min=10)]
        curve = _curve([1_000_000, 999_950, 999_890])
        m = compute_metrics(trades, curve, total_bars=3, sl_exit_count=2)
        assert m.sl_hit_rate == 1.0
