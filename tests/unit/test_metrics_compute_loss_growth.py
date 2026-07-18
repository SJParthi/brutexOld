"""Unit + property tests for the loss/growth metrics on ``ResultMetrics``.

DEC-METRICS-LOSSRATE-FIRSTCLASS-001 (``loss_rate``) +
DEC-METRICS-KELLY-GROWTH-001 (``kelly_fraction`` /
``log_growth_at_half_kelly``). All three are COMBINE-side by the
fold/combine libm law (DEC-METRICS-RUST-COMPUTE-FOLD-001): pure functions of
EXISTING fold scalars, NO new fold state — pinned here by exercising the
public entries AND the public :func:`combine_metrics_from_folds` on
hand-built :class:`TradeFolds` (the only route to the defensive degenerate
arms real folds can never produce).
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

from brutex.metrics.compute import (
    Moments,
    TradeFolds,
    combine_metrics_from_folds,
    compute_metrics,
)
from brutex.metrics.types import ClosedTrade, EquityCurve
from brutex.strategies.types import Side
from hypothesis import given
from hypothesis import strategies as st

_T0 = datetime(2024, 1, 2, 4, 0, tzinfo=UTC)


def _trade(i: int, pnl: int) -> ClosedTrade:
    return ClosedTrade(
        pnl_paisa=pnl,
        mfe_paisa=max(pnl, 0),
        mae_paisa=max(-pnl, 0),
        holding_bars=1,
        entry_ts=_T0 + timedelta(minutes=2 * i),
        exit_ts=_T0 + timedelta(minutes=2 * i + 1),
        direction=Side.BUY,
    )


def _equity() -> EquityCurve:
    return EquityCurve(
        equity_paisa=(1_000_000, 1_000_100),
        start_equity_paisa=1_000_000,
        span_years=1.0,
    )


def _folds(
    *,
    n_trades: int = 4,
    winners: int = 2,
    losers: int = 1,
    gross_profit: int = 300,
    gross_loss_abs: int = 50,
) -> TradeFolds:
    """A hand-built fold set for the PUBLIC combine (arbitrary — the route to
    the defensive arms real ``_fold_trades`` output can never reach)."""
    return TradeFolds(
        n_trades=n_trades,
        net_pnl_paisa=gross_profit - gross_loss_abs,
        gross_profit_paisa=gross_profit,
        gross_loss_abs_paisa=gross_loss_abs,
        winners=winners,
        losers=losers,
        sum_mfe_paisa=0,
        sum_mae_paisa=0,
        sum_holding_bars=n_trades,
        max_holding_bars=1,
        largest_loss_paisa=-gross_loss_abs if losers else 0,
        max_consecutive_losses=min(losers, 1),
        max_consecutive_wins=min(winners, 1),
        moments=Moments(n=n_trades, mean=0.0, m2=0.0, m3=0.0, m4=0.0),
        downside_sum_sq=0.0,
        p95=0.0,
        p5=0.0,
    )


def _combine(folds: TradeFolds) -> tuple[float, float | None, float | None]:
    m = combine_metrics_from_folds(
        folds,
        total_bars=10,
        benchmark_return_pct=0.0,
        start_equity_paisa=1_000_000,
        span_years=1.0,
        drawdown=(0, 0.0, 0, 0.0),
    )
    return m.loss_rate, m.kelly_fraction, m.log_growth_at_half_kelly


class TestLossRate:
    def test_loss_rate_is_losers_over_all_trades(self) -> None:
        # Mixed win/loss/scratch: 2 winners, 1 loser, 1 scratch over 4 trades.
        trades = [_trade(0, 100), _trade(1, -50), _trade(2, 0), _trade(3, 200)]
        m = compute_metrics(trades, _equity(), total_bars=10)
        assert m.loss_rate == 1 / 4
        assert m.win_rate == 2 / 4

    def test_scratch_trades_make_loss_rate_not_one_minus_win_rate(self) -> None:
        # THE WEDGE that makes loss_rate non-redundant
        # (DEC-METRICS-LOSSRATE-FIRSTCLASS-001): scratch trades (pnl == 0)
        # count in the denominator only, so 1 − win_rate over-counts losses.
        trades = [_trade(0, 100), _trade(1, -50), _trade(2, 0), _trade(3, 200)]
        m = compute_metrics(trades, _equity(), total_bars=10)
        assert m.loss_rate != 1.0 - m.win_rate
        assert m.win_rate + m.loss_rate < 1.0

    def test_zero_trades_loss_rate_is_zero(self) -> None:
        # The _empty_metrics arm — the exact win_rate = 0.0 precedent.
        m = compute_metrics([], _equity(), total_bars=10)
        assert m.loss_rate == 0.0
        assert m.win_rate == 0.0

    @given(pnls=st.lists(st.integers(min_value=-10_000, max_value=10_000), min_size=1, max_size=40))
    def test_loss_rate_consistency_property(self, pnls: list[int]) -> None:
        trades = [_trade(i, pnl) for i, pnl in enumerate(pnls)]
        m = compute_metrics(trades, _equity(), total_bars=100)
        assert m.loss_rate == m.losing_trades / m.total_trades
        assert 0.0 <= m.loss_rate <= 1.0
        assert m.win_rate + m.loss_rate <= 1.0


class TestKellyGrowth:
    def test_hand_derived_kelly_and_half_kelly_growth(self) -> None:
        # 2 winners (100 + 200), 1 loser (−50), 1 scratch over 4 trades:
        # p = 2/4, q = 1/2, W = 300/2 = 150, L = 50/1 = 50, b = 3;
        # f* = 0.5 − 0.5/3 = 1/3; g(f*/2) = p·ln(1 + (1/6)·3) + q·ln(1 − 1/6).
        trades = [_trade(0, 100), _trade(1, -50), _trade(2, 0), _trade(3, 200)]
        m = compute_metrics(trades, _equity(), total_bars=10)
        p, q, b = 0.5, 0.5, 3.0
        f = p - q / b
        assert m.kelly_fraction == f
        assert m.log_growth_at_half_kelly == p * math.log(1.0 + (f / 2.0) * b) + q * math.log(
            1.0 - f / 2.0
        )

    def test_map_pinned_form_matches_win_rate_payoff_spelling(self) -> None:
        # The map's sizing-overlay row: f* = W − (1−W)/R with W = win_rate,
        # R = win_loss_ratio — algebraically identical to p − q/b. Note the
        # SPELLING check uses the full-precision fold ratios (the persisted
        # win_loss_ratio floor-divides averages first, so it is checked via
        # the same b the combine derives).
        trades = [_trade(0, 100), _trade(1, -50), _trade(2, 0), _trade(3, 200)]
        m = compute_metrics(trades, _equity(), total_bars=10)
        w = m.win_rate
        b = (m.gross_profit_paisa / m.winning_trades) / (m.gross_loss_paisa / m.losing_trades)
        assert m.kelly_fraction == w - (1.0 - w) / b

    def test_all_win_is_degenerate_none(self) -> None:
        m = compute_metrics([_trade(0, 100), _trade(1, 200)], _equity(), total_bars=10)
        assert m.kelly_fraction is None
        assert m.log_growth_at_half_kelly is None

    def test_all_loss_is_degenerate_none(self) -> None:
        m = compute_metrics([_trade(0, -100), _trade(1, -200)], _equity(), total_bars=10)
        assert m.kelly_fraction is None
        assert m.log_growth_at_half_kelly is None

    def test_zero_trades_is_degenerate_none(self) -> None:
        m = compute_metrics([], _equity(), total_bars=10)
        assert m.kelly_fraction is None
        assert m.log_growth_at_half_kelly is None

    def test_negative_kelly_still_yields_growth(self) -> None:
        # A losing edge: p = 1/2, b = 50/200 = 0.25 → f* = 0.5 − 2.0 = −1.5;
        # both half-Kelly log arguments stay positive (arg_win = (1+p+p·b)/2)
        # so the growth figure is real, never None. (A NEGATIVE f is a
        # short-the-bet stake in the closed form, so g(f*/2) is legitimately
        # positive here — the formula's answer, asserted exactly.)
        trades = [_trade(0, 50), _trade(1, -200)]
        m = compute_metrics(trades, _equity(), total_bars=10)
        f = 0.5 - 0.5 / 0.25
        assert m.kelly_fraction == f
        assert m.log_growth_at_half_kelly == 0.5 * math.log(1.0 + (f / 2.0) * 0.25) + 0.5 * (
            math.log(1.0 - f / 2.0)
        )

    # ── The defensive arms — reachable ONLY through the PUBLIC combine on
    # hand-built folds (real _fold_trades output cannot produce them). ──

    def test_hand_built_zero_gross_profit_with_winners_is_none(self) -> None:
        loss_rate, kelly, growth = _combine(
            _folds(winners=2, losers=1, gross_profit=0, gross_loss_abs=50)
        )
        assert loss_rate == 1 / 4
        assert kelly is None
        assert growth is None

    def test_hand_built_zero_gross_loss_with_losers_is_none(self) -> None:
        loss_rate, kelly, growth = _combine(
            _folds(winners=2, losers=1, gross_profit=300, gross_loss_abs=0)
        )
        assert loss_rate == 1 / 4
        assert kelly is None
        assert growth is None

    def test_hand_built_out_of_domain_log_argument_keeps_kelly_drops_growth(self) -> None:
        # winners > n_trades puts p > 1 (a hostile fold no real trade stream
        # produces): p = 5, q = −4, b = (300/5)/(50/1) = 1.2 →
        # f* = 5 + 4/1.2 ≈ 8.33 → arg_loss = 1 − f/2 < 0 → growth None,
        # kelly kept (the documented totality guard).
        loss_rate, kelly, growth = _combine(
            _folds(n_trades=1, winners=5, losers=1, gross_profit=300, gross_loss_abs=50)
        )
        assert loss_rate == 1.0
        assert kelly is not None
        assert kelly > 2.0
        assert growth is None

    @given(
        pnls=st.lists(
            st.integers(min_value=-10_000, max_value=10_000).filter(lambda v: v != 0),
            min_size=2,
            max_size=40,
        )
    )
    def test_kelly_defined_iff_both_sides_present_property(self, pnls: list[int]) -> None:
        trades = [_trade(i, pnl) for i, pnl in enumerate(pnls)]
        m = compute_metrics(trades, _equity(), total_bars=100)
        both_sides = m.winning_trades > 0 and m.losing_trades > 0
        assert (m.kelly_fraction is not None) == both_sides
        # For every REAL fold output the half-Kelly log domain is provably
        # open (arg_win = (1+p+p·b)/2 >= 1/2; arg_loss = 1 − f/2 >= 1/2), so
        # growth is defined exactly when kelly is.
        assert (m.log_growth_at_half_kelly is not None) == both_sides
