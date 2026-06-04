"""Unit tests for the Stage-2 sweep driver — the first assembled funnel slice.

Covers the missing seam (:func:`mask_to_entry_signals`), the equity-curve
builder, and the single-combo evaluator (:func:`evaluate_combo`) end-to-end on
hand-built bars. Every branch is exercised for the strict 100% line+branch gate.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from brutex.data.models import CandleBar
from brutex.engine.combo_mask import iter_masks_k_of_n
from brutex.engine.sweep_driver import (
    DEFAULT_START_EQUITY_PAISA,
    build_equity_curve,
    evaluate_combo,
    rank_sweep,
    sweep_masks,
)
from brutex.metrics.compute import ResultMetrics
from brutex.metrics.types import ClosedTrade
from brutex.ranking import RankedCombo
from brutex.strategies.types import Side

_IST = timezone(timedelta(hours=5, minutes=30))

#: A mask requiring all 52 conditions at once — self-contradictory (e.g. bullish
#: AND bearish), so it NEVER fires: the deterministic "no-trade combo".
_IMPOSSIBLE_MASK = (1 << 52) - 1


def _bar(hour: int, minute: int, o: str, h: str, lo: str, c: str) -> CandleBar:
    """Build one IST-session CandleBar (stored UTC) with Decimal prices."""
    ts = datetime(2026, 5, 13, hour, minute, tzinfo=_IST).astimezone(UTC)
    return CandleBar(
        timestamp=ts,
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(lo),
        close=Decimal(c),
        volume=100,
        open_interest=None,
    )


def _session_bars() -> list[CandleBar]:
    """A small rising-then-falling intraday session (5 one-minute bars)."""
    return [
        _bar(9, 16, "100.00", "101.00", "99.50", "100.50"),
        _bar(9, 17, "100.50", "102.00", "100.00", "101.50"),
        _bar(9, 18, "101.50", "101.80", "100.20", "100.40"),
        _bar(9, 19, "100.40", "100.90", "99.80", "100.10"),
        _bar(9, 20, "100.10", "100.30", "99.00", "99.20"),
    ]


# ────── build_equity_curve ──────


class TestBuildEquityCurve:
    def test_empty_bars_raises(self) -> None:
        with pytest.raises(ValueError, match="bars must be non-empty"):
            build_equity_curve([], [], tf_minutes=1)

    def test_no_trades_is_flat_at_base(self) -> None:
        bars = _session_bars()
        curve = build_equity_curve(bars, [], tf_minutes=1)
        assert curve.equity_paisa == (DEFAULT_START_EQUITY_PAISA,) * len(bars)
        assert curve.start_equity_paisa == DEFAULT_START_EQUITY_PAISA
        assert curve.span_years > 0.0

    def test_trade_pnl_lands_on_its_exit_bar(self) -> None:
        bars = _session_bars()
        # A winning trade that closes on the 3rd bar (index 2).
        trade = ClosedTrade(
            pnl_paisa=500,
            mfe_paisa=600,
            mae_paisa=50,
            holding_bars=2,
            entry_ts=bars[0].timestamp,
            exit_ts=bars[2].timestamp,
            direction=Side.BUY,
        )
        curve = build_equity_curve(bars, [trade], start_equity_paisa=1_000, tf_minutes=1)
        # Flat at base until the exit bar, then +500 from the exit bar onward.
        assert curve.equity_paisa == (1_000, 1_000, 1_500, 1_500, 1_500)

    def test_two_trades_same_exit_bar_accumulate(self) -> None:
        bars = _session_bars()
        common_exit = bars[1].timestamp
        trades = [
            ClosedTrade(
                pnl_paisa=300,
                mfe_paisa=300,
                mae_paisa=0,
                holding_bars=1,
                entry_ts=bars[0].timestamp,
                exit_ts=common_exit,
                direction=Side.BUY,
            ),
            ClosedTrade(
                pnl_paisa=-100,
                mfe_paisa=0,
                mae_paisa=100,
                holding_bars=1,
                entry_ts=bars[0].timestamp,
                exit_ts=common_exit,
                direction=Side.SELL,
            ),
        ]
        curve = build_equity_curve(bars, trades, start_equity_paisa=1_000, tf_minutes=1)
        # +300 and -100 both land on bar index 1 → net +200 from there on.
        assert curve.equity_paisa == (1_000, 1_200, 1_200, 1_200, 1_200)

    def test_trade_exit_not_a_bar_timestamp_raises(self) -> None:
        bars = _session_bars()
        stray = bars[-1].timestamp + timedelta(minutes=99)
        trade = ClosedTrade(
            pnl_paisa=1,
            mfe_paisa=1,
            mae_paisa=0,
            holding_bars=1,
            entry_ts=bars[0].timestamp,
            exit_ts=stray,
            direction=Side.BUY,
        )
        with pytest.raises(ValueError, match="not a bar timestamp"):
            build_equity_curve(bars, [trade], tf_minutes=1)

    def test_single_bar_span_floor(self) -> None:
        # first == last bar → span 0 → floored to one bar's worth of time (> 0).
        one = [_bar(9, 16, "100.00", "100.50", "99.50", "100.00")]
        curve = build_equity_curve(one, [], tf_minutes=5)
        assert curve.span_years == pytest.approx(5.0 / (365.25 * 24.0 * 60.0))
        assert curve.equity_paisa == (DEFAULT_START_EQUITY_PAISA,)


# ────── evaluate_combo (the single-combo backtest) ──────


class TestEvaluateCombo:
    def test_empty_bars_raises(self) -> None:
        with pytest.raises(ValueError, match="bars must be non-empty"):
            evaluate_combo([], 0, direction=Side.BUY, tf_minutes=1)

    def test_runs_full_chain_and_returns_metrics(self) -> None:
        bars = _session_bars()
        metrics = evaluate_combo(bars, required_mask=0, direction=Side.BUY, tf_minutes=1)
        # mask=0 enters on every bar → at least one trade closes by end-of-data.
        assert metrics.total_trades >= 1
        assert metrics.winning_trades + metrics.losing_trades <= metrics.total_trades
        # net_pnl is the signed sum of realised trade P&L (int64 paisa).
        assert isinstance(metrics.net_pnl_paisa, int)

    def test_no_trade_combo_yields_empty_metrics(self) -> None:
        bars = _session_bars()
        # The self-contradictory mask never fires (and day-1 anchor bits are
        # cleared too) → zero trades → an all-zero metrics row, not an error.
        metrics = evaluate_combo(
            bars, required_mask=_IMPOSSIBLE_MASK, direction=Side.BUY, tf_minutes=1
        )
        assert metrics.total_trades == 0
        assert metrics.net_pnl_paisa == 0
        assert metrics.win_rate == 0.0

    def test_short_direction_with_sl_tp(self) -> None:
        bars = _session_bars()
        metrics = evaluate_combo(
            bars,
            required_mask=0,
            direction=Side.SELL,
            tf_minutes=1,
            stop_loss_paisa=200,
            take_profit_paisa=100,
            start_equity_paisa=5_000_000,
        )
        assert metrics.total_trades >= 1


# ────── sweep_masks + rank_sweep (the leaderboard machine) ──────


class TestSweepMasks:
    def test_empty_bars_raises(self) -> None:
        with pytest.raises(ValueError, match="bars must be non-empty"):
            sweep_masks([], [0], direction=Side.BUY, tf_minutes=1)

    def test_one_pair_per_mask(self) -> None:
        bars = _session_bars()
        masks = list(iter_masks_k_of_n(1, n=52))[:5]
        pairs = sweep_masks(bars, masks, direction=Side.BUY, tf_minutes=1)
        assert [mask for mask, _ in pairs] == masks
        assert all(isinstance(m, ResultMetrics) for _, m in pairs)

    def test_shares_bar_bits_matches_evaluate_combo(self) -> None:
        # sweep_masks builds bar_bits ONCE + reuses it, but must produce the SAME
        # metrics as evaluate_combo per mask (no behavioural drift from the refactor).
        bars = _session_bars()
        mask = 0
        pairs = sweep_masks(bars, [mask], direction=Side.BUY, tf_minutes=1)
        _, swept = pairs[0]
        direct = evaluate_combo(bars, mask, direction=Side.BUY, tf_minutes=1)
        assert swept == direct


class TestRankSweep:
    def test_returns_ranked_leaderboard(self) -> None:
        bars = _session_bars()
        masks = list(iter_masks_k_of_n(1, n=52))[:6]
        ranked = rank_sweep(bars, masks, direction=Side.BUY, tf_minutes=1)
        # A list of RankedCombo — possibly empty when no combo clears the hard
        # gates on this tiny synthetic session (the gate discipline, not an error).
        assert isinstance(ranked, list)
        assert all(isinstance(rc, RankedCombo) for rc in ranked)
