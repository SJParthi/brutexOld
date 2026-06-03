"""Unit tests for the Stage-2 spot trade-simulation walker (`brutex.engine.trade_sim`).

100% line + branch coverage of the signal-only walker that turns a
``bool``-per-bar entry stream + a ``CandleBar`` feed into int64-paisa
:class:`~brutex.metrics.types.ClosedTrade` round-trips (DEC-TRADESIM-001). Covers:

* construction validation (HOLD, bad ``tf_minutes`` / SL / TP);
* entry @ next-bar OPEN; SL / TP fills at level; long + short P&L;
* same-bar SL+TP tie-break (pessimistic SL-first vs optimistic TP-first);
* the 15:20 IST forced exit (entry-skip near close + mid-trade override);
* Mode-B signal exit (next-bar OPEN fill);
* the ``LevelFill.NEXT_OPEN`` exit fill (+ its no-next-bar fallback);
* end-of-data close; MFE / MAE tracking; holding-bar counts;
* the single-position rule + the "ignore an entry signal on the exit bar";
* the streaming ``push`` / ``finish`` API and the ``run`` / ``simulate_trades``
  batch wrappers; the ``_levels`` SL/TP-optionality matrix.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from brutex.calendars.nse import IST
from brutex.data.models import CandleBar
from brutex.engine.trade_sim import (
    DEFAULT_FILL_POLICY,
    FillPolicy,
    LevelFill,
    SameBarTie,
    TradeSimWalker,
    simulate_trades,
)
from brutex.metrics.types import ClosedTrade
from brutex.strategies.types import Side

_DAY = date(2024, 1, 2)  # a regular NSE trading Tuesday


def _ts(hh: int, mm: int, *, day: date = _DAY) -> datetime:
    """UTC-aware timestamp for an IST wall-clock ``hh:mm`` (bars store UTC)."""
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=IST).astimezone(UTC)


def _bar(hh: int, mm: int, o: str, h: str, lo: str, c: str, *, day: date = _DAY) -> CandleBar:
    """Build a valid 1-minute :class:`CandleBar` at IST ``hh:mm`` from rupee strings."""
    return CandleBar(
        timestamp=_ts(hh, mm, day=day),
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(lo),
        close=Decimal(c),
        volume=0,
        open_interest=None,
    )


def _flat(hh: int, mm: int) -> CandleBar:
    """A flat doji bar (O=H=L=C=100.00 → 10000 paisa entry) at IST ``hh:mm``."""
    return _bar(hh, mm, "100", "100", "100", "100")


# ────────────────────────────────────────────────────────────────────────
# Construction validation
# ────────────────────────────────────────────────────────────────────────


class TestInit:
    def test_hold_direction_rejected(self) -> None:
        with pytest.raises(ValueError, match="BUY or SELL"):
            TradeSimWalker(direction=Side.HOLD, tf_minutes=1)

    def test_bad_tf_minutes_rejected(self) -> None:
        with pytest.raises(ValueError, match="tf_minutes must be >= 1"):
            TradeSimWalker(direction=Side.BUY, tf_minutes=0)

    def test_non_positive_stop_rejected(self) -> None:
        with pytest.raises(ValueError, match="stop_loss_paisa must be > 0"):
            TradeSimWalker(direction=Side.BUY, tf_minutes=1, stop_loss_paisa=0)

    def test_non_positive_target_rejected(self) -> None:
        with pytest.raises(ValueError, match="take_profit_paisa must be > 0"):
            TradeSimWalker(direction=Side.BUY, tf_minutes=1, take_profit_paisa=-5)

    def test_valid_construction_with_no_legs(self) -> None:
        walker = TradeSimWalker(direction=Side.SELL, tf_minutes=5)
        assert walker.run([], []) == []

    def test_default_policy_is_conservative(self) -> None:
        assert (
            FillPolicy(
                stop_loss=LevelFill.AT_LEVEL,
                take_profit=LevelFill.AT_LEVEL,
                same_bar=SameBarTie.SL_FIRST,
            )
            == DEFAULT_FILL_POLICY
        )


# ────────────────────────────────────────────────────────────────────────
# run() input validation
# ────────────────────────────────────────────────────────────────────────


class TestRunValidation:
    def test_entry_signal_length_mismatch(self) -> None:
        walker = TradeSimWalker(direction=Side.BUY, tf_minutes=1)
        with pytest.raises(ValueError, match="entry_signals length"):
            walker.run([_flat(9, 15)], [True, False])

    def test_exit_signal_length_mismatch(self) -> None:
        walker = TradeSimWalker(direction=Side.BUY, tf_minutes=1)
        with pytest.raises(ValueError, match="exit_signals length"):
            walker.run([_flat(9, 15)], [True], [True, False])

    def test_empty_feed_returns_no_trades(self) -> None:
        assert simulate_trades([], [], direction=Side.BUY, tf_minutes=1) == []

    def test_no_signal_never_opens(self) -> None:
        bars = [_flat(9, 15), _flat(9, 16), _flat(9, 17)]
        assert simulate_trades(bars, [False, False, False], direction=Side.BUY, tf_minutes=1) == []


# ────────────────────────────────────────────────────────────────────────
# Entry @ next-open + SL / TP at level + long / short P&L
# ────────────────────────────────────────────────────────────────────────


class TestEntryAndLevelExits:
    def test_long_take_profit_at_level(self) -> None:
        # signal@09:15 → enter @09:16 open=100.00; TP +50 paisa → level 100.50;
        # @09:17 high 101 touches it → fill at 100.50 → pnl +50, holding 2 bars.
        bars = [
            _flat(9, 15),
            _bar(9, 16, "100", "100", "100", "100"),
            _bar(9, 17, "100", "101", "99.50", "100.40"),
            _flat(9, 18),
        ]
        trades = simulate_trades(
            bars,
            [True, False, False, False],
            direction=Side.BUY,
            tf_minutes=1,
            take_profit_paisa=50,
        )
        assert trades == [
            ClosedTrade(
                pnl_paisa=50,
                mfe_paisa=100,  # high 101 → 100 paisa above entry
                mae_paisa=50,  # low 99.50 → 50 paisa below entry
                holding_bars=2,
                entry_ts=_ts(9, 16),
                exit_ts=_ts(9, 17),
                direction=Side.BUY,
            )
        ]

    def test_long_stop_loss_at_level(self) -> None:
        # SL -30 paisa → level 99.70; @09:17 low 99.50 breaches → fill 99.70 → -30.
        bars = [
            _flat(9, 15),
            _bar(9, 16, "100", "100", "100", "100"),
            _bar(9, 17, "100", "100.20", "99.50", "99.80"),
            _flat(9, 18),
        ]
        trades = simulate_trades(
            bars, [True, False, False, False], direction=Side.BUY, tf_minutes=1, stop_loss_paisa=30
        )
        assert len(trades) == 1
        assert trades[0].pnl_paisa == -30
        assert trades[0].mae_paisa == 50  # low 99.50
        assert trades[0].mfe_paisa == 20  # high 100.20

    def test_short_take_profit_at_level(self) -> None:
        # SELL: TP +40 paisa → level 99.60; @09:17 low 99.50 touches → +40.
        bars = [
            _flat(9, 15),
            _bar(9, 16, "100", "100", "100", "100"),
            _bar(9, 17, "100", "100.30", "99.50", "99.70"),
            _flat(9, 18),
        ]
        trades = simulate_trades(
            bars,
            [True, False, False, False],
            direction=Side.SELL,
            tf_minutes=1,
            take_profit_paisa=40,
        )
        assert trades[0].pnl_paisa == 40
        assert trades[0].mfe_paisa == 50  # short profits as price falls (low 99.50)
        assert trades[0].mae_paisa == 30  # high 100.30 is adverse for a short

    def test_short_stop_loss_at_level(self) -> None:
        # SELL: SL +25 paisa → level 100.25; @09:17 high 100.50 breaches → -25.
        bars = [
            _flat(9, 15),
            _bar(9, 16, "100", "100", "100", "100"),
            _bar(9, 17, "100", "100.50", "99.80", "100.20"),
            _flat(9, 18),
        ]
        trades = simulate_trades(
            bars, [True, False, False, False], direction=Side.SELL, tf_minutes=1, stop_loss_paisa=25
        )
        assert trades[0].pnl_paisa == -25

    def test_take_profit_on_entry_bar_holds_one(self) -> None:
        # Entry bar itself touches TP intrabar → holding 1.
        bars = [
            _flat(9, 15),
            _bar(9, 16, "100", "101", "100", "100.50"),
            _flat(9, 17),
        ]
        trades = simulate_trades(
            bars, [True, False, False], direction=Side.BUY, tf_minutes=1, take_profit_paisa=50
        )
        assert trades[0].holding_bars == 1
        assert trades[0].pnl_paisa == 50


# ────────────────────────────────────────────────────────────────────────
# Same-bar SL+TP tie-break
# ────────────────────────────────────────────────────────────────────────


class TestSameBarTieBreak:
    def _bars(self) -> list[CandleBar]:
        # @09:17 the range 98..102 spans both SL(99) and TP(101).
        return [
            _flat(9, 15),
            _bar(9, 16, "100", "100", "100", "100"),
            _bar(9, 17, "100", "102", "98", "100"),
            _flat(9, 18),
        ]

    def test_pessimistic_sl_first_default(self) -> None:
        trades = simulate_trades(
            self._bars(),
            [True, False, False, False],
            direction=Side.BUY,
            tf_minutes=1,
            stop_loss_paisa=100,
            take_profit_paisa=100,
        )
        assert trades[0].pnl_paisa == -100  # SL filled first (worst case)

    def test_optimistic_tp_first(self) -> None:
        trades = simulate_trades(
            self._bars(),
            [True, False, False, False],
            direction=Side.BUY,
            tf_minutes=1,
            stop_loss_paisa=100,
            take_profit_paisa=100,
            policy=FillPolicy(same_bar=SameBarTie.TP_FIRST),
        )
        assert trades[0].pnl_paisa == 100  # TP filled first

    def test_short_same_bar_sl_first(self) -> None:
        trades = simulate_trades(
            self._bars(),
            [True, False, False, False],
            direction=Side.SELL,
            tf_minutes=1,
            stop_loss_paisa=100,
            take_profit_paisa=100,
        )
        assert trades[0].pnl_paisa == -100


# ────────────────────────────────────────────────────────────────────────
# Forced 15:20 IST exit
# ────────────────────────────────────────────────────────────────────────


class TestForcedExit:
    def test_forced_exit_overrides_open_position(self) -> None:
        # Enter @15:18, ride to the 15:20 bar → forced exit at its OPEN (100.30).
        bars = [
            _bar(15, 17, "100", "100", "100", "100"),
            _bar(15, 18, "100", "100", "100", "100"),
            _bar(15, 19, "100", "100", "100", "100"),
            _bar(15, 20, "100.30", "101", "100", "100.50"),
        ]
        trades = simulate_trades(
            bars,
            [True, False, False, False],
            direction=Side.BUY,
            tf_minutes=1,
            stop_loss_paisa=1000,
            take_profit_paisa=1000,
        )
        assert trades[0].pnl_paisa == 30  # exit at the forced bar's OPEN 100.30
        assert trades[0].exit_ts == _ts(15, 20)
        assert trades[0].holding_bars == 3  # 15:18, 15:19, 15:20

    def test_entry_skipped_when_next_bar_is_forced(self) -> None:
        # signal @15:19 → entry would be @15:20 (forced) → no position opens.
        bars = [
            _bar(15, 19, "100", "100", "100", "100"),
            _bar(15, 20, "100", "105", "95", "100"),
            _bar(15, 21, "100", "105", "95", "100"),
        ]
        trades = simulate_trades(
            bars, [True, False, False], direction=Side.BUY, tf_minutes=1, take_profit_paisa=10
        )
        assert trades == []

    def test_forced_exit_is_tf_aware(self) -> None:
        # A 15-minute bar opening 15:15 IST spans to 15:30 → forced (tf-aware fix).
        bars = [
            _bar(15, 0, "100", "100", "100", "100"),
            _bar(15, 15, "100.40", "101", "99", "100.80"),
        ]
        trades = simulate_trades(
            bars, [True, False], direction=Side.BUY, tf_minutes=15, take_profit_paisa=10_000
        )
        # Enter @15:15 open? No — entry is next-open; here the signal is on the
        # 15:00 bar so entry is the 15:15 bar, which IS forced → entry skipped.
        assert trades == []

    def test_forced_exit_mid_trade_coarse_tf(self) -> None:
        # signal @14:30 (15m) → enter @14:45 bar; 15:15 bar straddles 15:20 → forced.
        bars = [
            _bar(14, 30, "100", "100", "100", "100"),
            _bar(14, 45, "100", "100", "100", "100"),
            _bar(15, 0, "100", "100", "100", "100"),
            _bar(15, 15, "100.20", "100.30", "99.90", "100.10"),
        ]
        trades = simulate_trades(
            bars,
            [True, False, False, False],
            direction=Side.BUY,
            tf_minutes=15,
            take_profit_paisa=9999,
        )
        assert trades[0].pnl_paisa == 20  # forced at 15:15 bar OPEN 100.20
        assert trades[0].holding_bars == 3


# ────────────────────────────────────────────────────────────────────────
# Mode-B signal exit + NEXT_OPEN level fill
# ────────────────────────────────────────────────────────────────────────


class TestSignalExitAndNextOpenFill:
    def test_signal_exit_fills_next_open(self) -> None:
        # Enter @09:16; exit signal @09:17 → fill at @09:18 OPEN 100.70 → +70.
        bars = [
            _flat(9, 15),
            _bar(9, 16, "100", "100", "100", "100"),
            _bar(9, 17, "100", "100.90", "99.90", "100.80"),
            _bar(9, 18, "100.70", "100.70", "100.70", "100.70"),
            _flat(9, 19),
        ]
        trades = simulate_trades(
            bars,
            [True, False, False, False, False],
            direction=Side.BUY,
            tf_minutes=1,
            exit_signals=[False, False, True, False, False],
        )
        assert trades[0].pnl_paisa == 70  # @09:18 open 100.70
        assert trades[0].exit_ts == _ts(9, 18)
        assert trades[0].holding_bars == 3

    def test_take_profit_next_open_fill(self) -> None:
        # TP touched @09:17 but the policy fills at @09:18 OPEN (100.20), not 100.50.
        bars = [
            _flat(9, 15),
            _bar(9, 16, "100", "100", "100", "100"),
            _bar(9, 17, "100", "101", "99.80", "100.60"),
            _bar(9, 18, "100.20", "100.20", "100.20", "100.20"),
            _flat(9, 19),
        ]
        trades = simulate_trades(
            bars,
            [True, False, False, False, False],
            direction=Side.BUY,
            tf_minutes=1,
            take_profit_paisa=50,
            policy=FillPolicy(take_profit=LevelFill.NEXT_OPEN),
        )
        assert trades[0].pnl_paisa == 20  # next-open fill, not the +50 level

    def test_next_open_fill_falls_back_to_close_at_end_of_data(self) -> None:
        # TP touched on the LAST bar under NEXT_OPEN → no next open → close at its CLOSE.
        bars = [
            _flat(9, 15),
            _bar(9, 16, "100", "100", "100", "100"),
            _bar(9, 17, "100", "101", "99.80", "100.40"),
        ]
        trades = simulate_trades(
            bars,
            [True, False, False],
            direction=Side.BUY,
            tf_minutes=1,
            take_profit_paisa=50,
            policy=FillPolicy(take_profit=LevelFill.NEXT_OPEN),
        )
        assert trades[0].pnl_paisa == 40  # @09:17 close 100.40 (fallback)
        assert trades[0].exit_ts == _ts(9, 17)


# ────────────────────────────────────────────────────────────────────────
# End-of-data close + the single-position / exit-bar rules
# ────────────────────────────────────────────────────────────────────────


class TestEndOfDataAndPositionRules:
    def test_open_position_closes_at_end_of_data(self) -> None:
        # No SL/TP/forced → rides to the end → closes at the last bar's CLOSE.
        bars = [
            _flat(9, 15),
            _bar(9, 16, "100", "100", "100", "100"),
            _bar(9, 17, "100", "100.50", "99.80", "100.30"),
        ]
        trades = simulate_trades(bars, [True, False, False], direction=Side.BUY, tf_minutes=1)
        assert trades[0].pnl_paisa == 30  # last close 100.30
        assert trades[0].holding_bars == 2
        assert trades[0].exit_ts == _ts(9, 17)

    def test_signals_ignored_while_in_position(self) -> None:
        # A signal every bar but only ONE position is open at a time.
        bars = [
            _flat(9, 15),
            _bar(9, 16, "100", "100", "100", "100"),
            _bar(9, 17, "100", "100", "100", "100"),
            _bar(9, 18, "100", "100", "100", "100"),
        ]
        trades = simulate_trades(bars, [True, True, True, True], direction=Side.BUY, tf_minutes=1)
        # One open across the whole window, closed at EOD.
        assert len(trades) == 1
        assert trades[0].holding_bars == 3  # 09:16, 09:17, 09:18

    def test_entry_signal_on_exit_bar_is_ignored(self) -> None:
        # TP closes @09:17 AND a signal fires @09:17 → no immediate re-entry; the
        # next signal (@09:18) re-enters @09:19.
        bars = [
            _flat(9, 15),
            _bar(9, 16, "100", "100", "100", "100"),
            _bar(9, 17, "100", "101", "100", "100.60"),  # TP +50 closes here
            _bar(9, 18, "100", "100", "100", "100"),
            _bar(9, 19, "100", "100", "100", "100"),
            _bar(9, 20, "100", "100", "100", "100"),
        ]
        trades = simulate_trades(
            bars,
            [True, False, True, True, False, False],
            direction=Side.BUY,
            tf_minutes=1,
            take_profit_paisa=50,
        )
        assert len(trades) == 2
        assert trades[0].exit_ts == _ts(9, 17)  # first: TP
        # second: signal@09:18 → enter@09:19 → EOD close@09:20.
        assert trades[1].entry_ts == _ts(9, 19)


# ────────────────────────────────────────────────────────────────────────
# _levels SL/TP-optionality matrix (both directions)
# ────────────────────────────────────────────────────────────────────────


class TestLevelsMatrix:
    @pytest.mark.parametrize("direction", [Side.BUY, Side.SELL])
    @pytest.mark.parametrize(
        ("sl", "tp"),
        [(30, 50), (30, None), (None, 50), (None, None)],
    )
    def test_levels_optionality(self, direction: Side, sl: int | None, tp: int | None) -> None:
        # A calm 4-bar window that never breaches a ±$1 band → every variant
        # rides to EOD (exercising every _levels branch without an early exit).
        bars = [
            _flat(9, 15),
            _bar(9, 16, "100", "100.10", "99.90", "100"),
            _bar(9, 17, "100", "100.10", "99.90", "100"),
        ]
        trades = simulate_trades(
            bars,
            [True, False, False],
            direction=direction,
            tf_minutes=1,
            stop_loss_paisa=sl,
            take_profit_paisa=tp,
        )
        assert len(trades) == 1
        assert trades[0].holding_bars == 2


# ────────────────────────────────────────────────────────────────────────
# Streaming push / finish API
# ────────────────────────────────────────────────────────────────────────


class TestStreamingAPI:
    def test_push_finish_matches_run(self) -> None:
        bars = [
            _flat(9, 15),
            _bar(9, 16, "100", "100", "100", "100"),
            _bar(9, 17, "100", "100.50", "99.80", "100.30"),
        ]
        signals = [True, False, False]

        batch = simulate_trades(bars, signals, direction=Side.BUY, tf_minutes=1)

        walker = TradeSimWalker(direction=Side.BUY, tf_minutes=1)
        streamed: list[ClosedTrade] = []
        for bar, sig in zip(bars, signals, strict=True):
            closed = walker.push(bar, entry_signal=sig)
            if closed is not None:
                streamed.append(closed)
        tail = walker.finish()
        if tail is not None:
            streamed.append(tail)
        assert streamed == batch

    def test_finish_on_flat_walker_is_none(self) -> None:
        walker = TradeSimWalker(direction=Side.BUY, tf_minutes=1)
        walker.push(_flat(9, 15), entry_signal=False)
        assert walker.finish() is None


# ────────────────────────────────────────────────────────────────────────
# MFE / MAE excursion tracking
# ────────────────────────────────────────────────────────────────────────


class TestExcursions:
    def test_mfe_mae_long(self) -> None:
        # Across the hold the price reaches +90 (high 100.90) and -40 (low 99.60).
        bars = [
            _flat(9, 15),
            _bar(9, 16, "100", "100.30", "99.80", "100"),
            _bar(9, 17, "100", "100.90", "99.60", "100.20"),
        ]
        trades = simulate_trades(bars, [True, False, False], direction=Side.BUY, tf_minutes=1)
        assert trades[0].mfe_paisa == 90
        assert trades[0].mae_paisa == 40

    def test_mfe_mae_short(self) -> None:
        # For a short, profit is price falling: best low 99.60 → MFE 40; worst
        # high 100.90 → MAE 90.
        bars = [
            _flat(9, 15),
            _bar(9, 16, "100", "100.30", "99.80", "100"),
            _bar(9, 17, "100", "100.90", "99.60", "100.20"),
        ]
        trades = simulate_trades(bars, [True, False, False], direction=Side.SELL, tf_minutes=1)
        assert trades[0].mfe_paisa == 40
        assert trades[0].mae_paisa == 90
