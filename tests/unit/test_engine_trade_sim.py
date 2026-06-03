"""Unit tests for the Stage-2 Mode-A trade-sim walker (`brutex.engine.trade_sim`).

100% line + branch coverage of the signal-only SL/TP + forced-15:20 producer
(DEC-TRADE-SIM-MODEA-001). Covers every validation branch + the edge cases the
CLAUDE.md §9 hostile review surfaced:

* entry at N+1 OPEN, exit at M+1 OPEN, forced 15:20-OPEN exit (overrides);
* long + short P&L signs, SL-only / TP-only / both-in-one-bar (moot) breaches;
* entry bar self-trigger, signal-on-last-bar, in-position-ignored, re-entry,
  entry-past-15:20, drop-incomplete (no exit / breach-on-last-bar);
* MFE/MAE values; multi-trade; dedup; the input validators.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from brutex.data.models import CandleBar
from brutex.engine.trade_sim import TradeSimError, simulate_session_mode_a
from brutex.metrics.types import ClosedTrade
from brutex.strategies.types import Side

_IST = ZoneInfo("Asia/Kolkata")


def _bar(hour: int, minute: int, o: str, h: str, low: str, c: str, *, day: int = 2) -> CandleBar:
    """A valid single-session CandleBar at the given IST time (stored UTC)."""
    ts = datetime(2024, 1, day, hour, minute, tzinfo=_IST).astimezone(UTC)
    return CandleBar(
        timestamp=ts,
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(low),
        close=Decimal(c),
        volume=0,
        open_interest=None,
    )


def _flat(hour: int, minute: int, price: str, *, day: int = 2) -> CandleBar:
    """A zero-range bar (O=H=L=C) — handy as a signal / filler bar."""
    return _bar(hour, minute, price, price, price, price, day=day)


# ──────────────────────────────────────────────────────────────────────────
# Input validation
# ──────────────────────────────────────────────────────────────────────────


class TestValidation:
    def test_empty_bars_rejected(self) -> None:
        with pytest.raises(TradeSimError, match="non-empty"):
            simulate_session_mode_a([], signal_bars=[], direction=Side.BUY, sl_paisa=5, tp_paisa=10)

    def test_hold_direction_rejected(self) -> None:
        with pytest.raises(TradeSimError, match="BUY or SELL"):
            simulate_session_mode_a(
                [_flat(9, 15, "100")], signal_bars=[], direction=Side.HOLD, sl_paisa=5, tp_paisa=10
            )

    @pytest.mark.parametrize("sl", [0, -1])
    def test_non_positive_sl_rejected(self, sl: int) -> None:
        with pytest.raises(TradeSimError, match="sl_paisa must be > 0"):
            simulate_session_mode_a(
                [_flat(9, 15, "100")], signal_bars=[], direction=Side.BUY, sl_paisa=sl, tp_paisa=10
            )

    @pytest.mark.parametrize("tp", [0, -5])
    def test_non_positive_tp_rejected(self, tp: int) -> None:
        with pytest.raises(TradeSimError, match="tp_paisa must be > 0"):
            simulate_session_mode_a(
                [_flat(9, 15, "100")], signal_bars=[], direction=Side.BUY, sl_paisa=5, tp_paisa=tp
            )

    def test_unordered_timestamps_rejected(self) -> None:
        bars = [_flat(9, 16, "100"), _flat(9, 15, "100")]  # second is earlier
        with pytest.raises(TradeSimError, match="strictly time-ordered"):
            simulate_session_mode_a(
                bars, signal_bars=[], direction=Side.BUY, sl_paisa=5, tp_paisa=10
            )

    def test_duplicate_timestamp_rejected(self) -> None:
        bars = [_flat(9, 15, "100"), _flat(9, 15, "101")]  # equal ts
        with pytest.raises(TradeSimError, match="strictly time-ordered"):
            simulate_session_mode_a(
                bars, signal_bars=[], direction=Side.BUY, sl_paisa=5, tp_paisa=10
            )

    def test_multi_session_rejected(self) -> None:
        bars = [_flat(9, 15, "100", day=2), _flat(9, 16, "100", day=3)]  # two IST dates
        with pytest.raises(TradeSimError, match="ONE IST session"):
            simulate_session_mode_a(
                bars, signal_bars=[], direction=Side.BUY, sl_paisa=5, tp_paisa=10
            )

    @pytest.mark.parametrize("bad", [-1, 2])
    def test_signal_index_out_of_range_rejected(self, bad: int) -> None:
        bars = [_flat(9, 15, "100"), _flat(9, 16, "100")]
        with pytest.raises(TradeSimError, match="out of range"):
            simulate_session_mode_a(
                bars, signal_bars=[bad], direction=Side.BUY, sl_paisa=5, tp_paisa=10
            )


# ──────────────────────────────────────────────────────────────────────────
# Long (BUY) round-trips
# ──────────────────────────────────────────────────────────────────────────


class TestLong:
    def test_take_profit_exit_at_m_plus_1_open(self) -> None:
        bars = [
            _flat(9, 15, "100"),  # 0 signal
            _bar(9, 16, "100.00", "100.20", "99.90", "100.10"),  # 1 entry @100.00
            _bar(9, 17, "100.10", "100.60", "100.05", "100.55"),  # 2 high 100.60 >= TP 100.50
            _bar(9, 18, "100.50", "100.70", "100.40", "100.45"),  # 3 exit @ open 100.50
        ]
        (trade,) = simulate_session_mode_a(
            bars, signal_bars=[0], direction=Side.BUY, sl_paisa=30, tp_paisa=50
        )
        assert isinstance(trade, ClosedTrade)
        assert trade.pnl_paisa == 50  # 100.50 - 100.00
        assert trade.holding_bars == 2  # entry idx1 -> exit idx3
        assert trade.direction is Side.BUY
        assert trade.mfe_paisa == 60  # max high 100.60 - 100.00
        assert trade.mae_paisa == 10  # entry 100.00 - min low 99.90

    def test_stop_loss_exit_is_a_loss(self) -> None:
        bars = [
            _flat(9, 15, "100"),
            _bar(9, 16, "100.00", "100.05", "99.60", "99.70"),  # 1 entry; low 99.60 <= SL 99.70
            _bar(9, 17, "99.65", "99.80", "99.50", "99.60"),  # 2 exit @ open 99.65
        ]
        (trade,) = simulate_session_mode_a(
            bars, signal_bars=[0], direction=Side.BUY, sl_paisa=30, tp_paisa=200
        )
        assert trade.pnl_paisa == -35  # 99.65 - 100.00
        assert trade.holding_bars == 1

    def test_forced_15_20_exit_overrides(self) -> None:
        bars = [
            _flat(9, 15, "100"),
            _bar(9, 16, "100.00", "100.10", "99.95", "100.05"),  # 1 entry, no breach
            _bar(15, 20, "101.00", "101.50", "100.90", "101.20"),  # 2 forced exit @ open 101.00
        ]
        (trade,) = simulate_session_mode_a(
            bars, signal_bars=[0], direction=Side.BUY, sl_paisa=500, tp_paisa=900
        )
        assert trade.pnl_paisa == 100  # 101.00 - 100.00
        assert trade.holding_bars == 1
        # The forced bar's own range is NOT counted in MFE/MAE (exit at its open).
        assert trade.mfe_paisa == 10  # only bar 1's high 100.10
        assert trade.mae_paisa == 5  # only bar 1's low 99.95

    def test_entry_bar_can_self_trigger(self) -> None:
        bars = [
            _flat(9, 15, "100"),
            _bar(9, 16, "100.00", "100.60", "99.95", "100.40"),  # 1 entry; high 100.60 >= TP
            _bar(9, 17, "100.55", "100.60", "100.50", "100.55"),  # 2 exit @ open 100.55
        ]
        (trade,) = simulate_session_mode_a(
            bars, signal_bars=[0], direction=Side.BUY, sl_paisa=200, tp_paisa=50
        )
        assert trade.holding_bars == 1  # entry idx1 -> exit idx2
        assert trade.pnl_paisa == 55

    def test_same_bar_sl_and_tp_is_moot_exit_at_m_plus_1_open(self) -> None:
        # One bar breaches BOTH SL and TP; exit is M+1 OPEN regardless (no
        # precedence) — the proof the M+1-open rule dissolves the tie-break.
        bars = [
            _flat(9, 15, "100"),
            _bar(
                9, 16, "100.00", "100.60", "99.40", "100.10"
            ),  # 1 entry; hits TP 100.50 AND SL 99.50
            _bar(9, 17, "100.20", "100.30", "100.10", "100.25"),  # 2 exit @ open 100.20
        ]
        (trade,) = simulate_session_mode_a(
            bars, signal_bars=[0], direction=Side.BUY, sl_paisa=50, tp_paisa=50
        )
        assert trade.pnl_paisa == 20  # 100.20 - 100.00, regardless of SL-vs-TP


# ──────────────────────────────────────────────────────────────────────────
# Short (SELL) round-trips
# ──────────────────────────────────────────────────────────────────────────


class TestShort:
    def test_take_profit_when_price_falls(self) -> None:
        bars = [
            _flat(9, 15, "100"),
            _bar(9, 16, "100.00", "100.05", "99.90", "99.95"),  # 1 entry @100.00 short
            _bar(9, 17, "99.95", "100.00", "99.40", "99.45"),  # 2 low 99.40 <= TP 99.50
            _bar(9, 18, "99.50", "99.60", "99.40", "99.55"),  # 3 exit @ open 99.50
        ]
        (trade,) = simulate_session_mode_a(
            bars, signal_bars=[0], direction=Side.SELL, sl_paisa=80, tp_paisa=50
        )
        assert trade.pnl_paisa == 50  # short: entry 100.00 - exit 99.50
        assert trade.direction is Side.SELL
        assert trade.mfe_paisa == 60  # favourable short = entry - min low (100.00-99.40)
        assert trade.mae_paisa == 5  # adverse short = max high - entry (100.05-100.00)

    def test_stop_loss_when_price_rises(self) -> None:
        bars = [
            _flat(9, 15, "100"),
            _bar(9, 16, "100.00", "100.40", "99.95", "100.35"),  # 1 high 100.40 >= SL 100.30
            _bar(9, 17, "100.30", "100.50", "100.25", "100.45"),  # 2 exit @ open 100.30
        ]
        (trade,) = simulate_session_mode_a(
            bars, signal_bars=[0], direction=Side.SELL, sl_paisa=30, tp_paisa=200
        )
        assert trade.pnl_paisa == -30  # short loss: 100.00 - 100.30
        assert trade.holding_bars == 1


# ──────────────────────────────────────────────────────────────────────────
# Signal handling: drop / ignore / re-enter / forced-room
# ──────────────────────────────────────────────────────────────────────────


class TestSignalHandling:
    def test_signal_on_last_bar_no_trade(self) -> None:
        bars = [_flat(9, 15, "100"), _flat(9, 16, "100")]
        assert (
            simulate_session_mode_a(
                bars, signal_bars=[1], direction=Side.BUY, sl_paisa=5, tp_paisa=10
            )
            == []
        )

    def test_empty_signals_no_trades(self) -> None:
        bars = [
            _flat(9, 15, "100"),
            _bar(9, 16, "100", "100.1", "99.9", "100"),
            _flat(15, 20, "101"),
        ]
        assert (
            simulate_session_mode_a(
                bars, signal_bars=[], direction=Side.BUY, sl_paisa=5, tp_paisa=10
            )
            == []
        )

    def test_in_position_signal_ignored(self) -> None:
        # Two signals; the second fires while the first trade is open -> ignored.
        bars = [
            _flat(9, 15, "100"),  # 0 signal
            _bar(9, 16, "100.00", "100.10", "99.95", "100.05"),  # 1 entry, open until forced
            _flat(9, 17, "100"),  # 2 signal (ignored — in position)
            _flat(15, 20, "100"),  # 3 forced exit
        ]
        trades = simulate_session_mode_a(
            bars, signal_bars=[0, 2], direction=Side.BUY, sl_paisa=900, tp_paisa=900
        )
        assert len(trades) == 1

    def test_re_entry_after_exit(self) -> None:
        bars = [
            _flat(9, 15, "100"),  # 0 signal A
            _bar(9, 16, "100.00", "100.60", "99.95", "100.40"),  # 1 entry A; TP -> exit idx2
            _bar(9, 17, "100.50", "100.55", "100.45", "100.50"),  # 2 exit A @ open; signal B here
            _bar(9, 18, "100.50", "101.10", "100.45", "101.00"),  # 3 entry B; TP -> exit idx4
            _bar(9, 19, "101.00", "101.10", "100.95", "101.05"),  # 4 exit B @ open
        ]
        trades = simulate_session_mode_a(
            bars, signal_bars=[0, 2], direction=Side.BUY, sl_paisa=200, tp_paisa=50
        )
        assert len(trades) == 2

    def test_signal_with_entry_at_or_after_forced_bar_no_trade(self) -> None:
        # Signal at the 15:19 bar -> entry would be the 15:20 forced bar -> no room.
        bars = [
            _flat(9, 15, "100"),
            _flat(15, 19, "100"),  # 1 signal here -> entry idx2 == forced bar
            _flat(15, 20, "100"),  # 2 forced bar
        ]
        assert (
            simulate_session_mode_a(
                bars, signal_bars=[1], direction=Side.BUY, sl_paisa=5, tp_paisa=10
            )
            == []
        )

    def test_duplicate_signal_indices_deduped(self) -> None:
        bars = [
            _flat(9, 15, "100"),
            _bar(9, 16, "100.00", "100.10", "99.95", "100.05"),
            _flat(15, 20, "101"),
        ]
        trades = simulate_session_mode_a(
            bars, signal_bars=[0, 0, 0], direction=Side.BUY, sl_paisa=900, tp_paisa=900
        )
        assert len(trades) == 1


# ──────────────────────────────────────────────────────────────────────────
# Drop-incomplete (no fillable exit)
# ──────────────────────────────────────────────────────────────────────────


class TestDropIncomplete:
    def test_no_exit_no_forced_bar_dropped(self) -> None:
        # No 15:20 bar, no SL/TP breach -> position rides off the end -> dropped.
        bars = [
            _flat(9, 15, "100"),
            _bar(9, 16, "100.00", "100.10", "99.95", "100.05"),
            _bar(9, 17, "100.05", "100.12", "99.98", "100.08"),
        ]
        assert (
            simulate_session_mode_a(
                bars, signal_bars=[0], direction=Side.BUY, sl_paisa=900, tp_paisa=900
            )
            == []
        )

    def test_breach_on_last_bar_no_fill_dropped(self) -> None:
        # SL/TP breaches on the LAST bar (no M+1 to fill against) + no forced bar.
        bars = [
            _flat(9, 15, "100"),
            _bar(9, 16, "100.00", "100.10", "99.95", "100.05"),
            _bar(9, 17, "100.05", "100.70", "100.00", "100.60"),  # 2 (last) high 100.70 >= TP
        ]
        assert (
            simulate_session_mode_a(
                bars, signal_bars=[0], direction=Side.BUY, sl_paisa=900, tp_paisa=50
            )
            == []
        )


# ──────────────────────────────────────────────────────────────────────────
# Multi-trade ordering + determinism
# ──────────────────────────────────────────────────────────────────────────


def test_trades_in_entry_order() -> None:
    bars = [
        _flat(9, 15, "100"),  # 0 signal A
        _bar(9, 16, "100.00", "100.60", "99.95", "100.40"),  # 1 entry A; TP
        _bar(9, 17, "100.50", "100.55", "100.45", "100.50"),  # 2 exit A; signal B
        _bar(9, 18, "100.50", "100.55", "100.00", "100.05"),  # 3 entry B; SL
        _bar(9, 19, "100.05", "100.10", "99.95", "100.00"),  # 4 exit B
    ]
    trades = simulate_session_mode_a(
        bars, signal_bars=[2, 0], direction=Side.BUY, sl_paisa=40, tp_paisa=50
    )
    assert [t.entry_ts for t in trades] == sorted(t.entry_ts for t in trades)
    assert len(trades) == 2
