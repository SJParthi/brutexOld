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

from datetime import UTC, date, datetime, time
from decimal import Decimal

import pytest
from brutex.calendars.nse import IST, SessionType, TradingSession
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


# ────────────────────────────────────────────────────────────────────────
# Session-aware forced exit (DEC-SESSION-FORCED-EXIT-001)
#
# The forced-exit boundary is the bar SESSION's own close − 10min: 15:20 IST
# on a regular day (unchanged regression), the early close − 10min on a
# half-day / Muhurat session. Each case drives the REAL ``TradeSimWalker.push``
# production path and asserts the resolved exit timestamp.
# ────────────────────────────────────────────────────────────────────────

# Real in-scope short sessions (verified from ``MUHURAT_SESSIONS``):
_EVENING_MUHURAT = date(2021, 11, 4)  # 18:15-19:15 IST → forced 19:05
_AFTERNOON_MUHURAT = date(2025, 10, 21)  # 13:45-14:45 IST → forced 14:35


def _trade_with_signal_then_ride(
    bars: list[CandleBar], *, tf_minutes: int = 1
) -> list[ClosedTrade]:
    """Signal on bar[0], then ride flat: a position opens at bar[1]'s open and is
    only ever closed by the session forced exit (huge SL/TP disable the legs)."""
    signals = [i == 0 for i in range(len(bars))]
    return simulate_trades(
        bars,
        signals,
        direction=Side.BUY,
        tf_minutes=tf_minutes,
        stop_loss_paisa=100_000,
        take_profit_paisa=100_000,
    )


class TestSessionAwareForcedExit:
    def test_regular_day_still_exits_at_1520_unchanged(self) -> None:
        # Regression: a regular trading day forces out at 15:20 IST exactly.
        bars = [
            _bar(15, 18, "100", "100", "100", "100"),
            _bar(15, 19, "100", "100", "100", "100"),
            _bar(15, 20, "100.30", "101", "100", "100.50"),
            _bar(15, 21, "100", "100", "100", "100"),
        ]
        trades = _trade_with_signal_then_ride(bars)
        assert len(trades) == 1
        assert trades[0].exit_ts == _ts(15, 20)  # forced @ the 15:20 bar OPEN
        assert trades[0].pnl_paisa == 30  # filled at OPEN 100.30

    def test_regular_day_no_premature_exit_before_1520(self) -> None:
        # A bar at 15:19 (one minute BEFORE the 15:20 boundary) does NOT force out;
        # the 15:20 bar does.
        bars = [
            _bar(15, 17, "100", "100", "100", "100"),
            _bar(15, 18, "100", "100", "100", "100"),
            _bar(15, 19, "100", "100", "100", "100"),  # NOT forced (close 15:20 ≯ 15:20)
            _bar(15, 20, "100.70", "101", "100", "100.90"),  # forced here
        ]
        trades = _trade_with_signal_then_ride(bars)
        assert len(trades) == 1
        assert trades[0].exit_ts == _ts(15, 20)
        assert trades[0].holding_bars == 3  # 15:18, 15:19, 15:20

    def test_evening_muhurat_exits_at_close_minus_10_not_1520(self) -> None:
        # The 2021 evening Diwali Muhurat runs 18:15-19:15 IST → forced exit 19:05.
        # On origin/main this trade would force out IMMEDIATELY on the first
        # managed bar (every evening bar is "≥ 15:20"); the fix exits at 19:05.
        bars = [
            _bar(18, 30, "100", "100", "100", "100", day=_EVENING_MUHURAT),
            _bar(18, 31, "100", "100", "100", "100", day=_EVENING_MUHURAT),
            _bar(19, 4, "100", "100", "100", "100", day=_EVENING_MUHURAT),  # NOT forced
            _bar(19, 5, "100.40", "101", "100", "100.60", day=_EVENING_MUHURAT),  # forced
        ]
        trades = _trade_with_signal_then_ride(bars)
        assert len(trades) == 1
        assert trades[0].exit_ts == _ts(19, 5, day=_EVENING_MUHURAT)  # NOT 15:20
        assert trades[0].pnl_paisa == 40  # forced at 19:05 bar OPEN 100.40

    def test_afternoon_muhurat_2025_exits_at_close_minus_10(self) -> None:
        # The 2025 afternoon Muhurat runs 13:45-14:45 IST → forced exit 14:35.
        bars = [
            _bar(14, 0, "100", "100", "100", "100", day=_AFTERNOON_MUHURAT),
            _bar(14, 34, "100", "100", "100", "100", day=_AFTERNOON_MUHURAT),  # NOT forced
            _bar(14, 35, "100.20", "101", "100", "100.30", day=_AFTERNOON_MUHURAT),  # forced
        ]
        trades = _trade_with_signal_then_ride(bars)
        assert len(trades) == 1
        assert trades[0].exit_ts == _ts(14, 35, day=_AFTERNOON_MUHURAT)
        assert trades[0].pnl_paisa == 20

    def test_position_open_exactly_at_forced_minute_exits_that_bar(self) -> None:
        # A position still open exactly AT the forced-exit boundary bar exits on
        # THAT bar (at its open), not the next.
        bars = [
            _bar(14, 33, "100", "100", "100", "100", day=_AFTERNOON_MUHURAT),
            _bar(14, 34, "100", "100", "100", "100", day=_AFTERNOON_MUHURAT),
            _bar(14, 35, "100.50", "101", "100", "100.90", day=_AFTERNOON_MUHURAT),  # boundary
            _bar(14, 36, "200", "200", "200", "200", day=_AFTERNOON_MUHURAT),  # never reached
        ]
        trades = _trade_with_signal_then_ride(bars)
        assert len(trades) == 1
        assert trades[0].exit_ts == _ts(14, 35, day=_AFTERNOON_MUHURAT)  # THIS bar
        assert trades[0].pnl_paisa == 50  # OPEN 100.50, not the next bar's 200

    def test_half_day_session_exits_at_early_close_minus_10(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No real HALF_DAY date exists in 2021-2026 (NSE retired pre-festival
        # half-days; the enum is forward-compat). Drive a genuine HALF_DAY
        # session (12:30 early close → forced 12:20) through the production
        # ``get_session`` seam so the real ``_forced_exit_seconds`` resolver runs.
        half_day = date(2024, 1, 2)
        session = TradingSession(
            date=half_day,
            session_type=SessionType.HALF_DAY,
            open_time_ist=time(9, 15),
            close_time_ist=time(12, 30),
        )
        monkeypatch.setattr(
            "brutex.engine.trade_sim.get_session",
            lambda d: session if d == half_day else None,
        )
        bars = [
            _bar(12, 0, "100", "100", "100", "100", day=half_day),
            _bar(12, 19, "100", "100", "100", "100", day=half_day),  # NOT forced
            _bar(12, 20, "100.10", "101", "100", "100.30", day=half_day),  # forced @ 12:20
        ]
        trades = _trade_with_signal_then_ride(bars)
        assert len(trades) == 1
        assert trades[0].exit_ts == _ts(12, 20, day=half_day)
        assert trades[0].pnl_paisa == 10

    def test_session_with_no_reachable_forced_exit_closes_at_end_of_data(self) -> None:
        # A short Muhurat feed that ENDS before the 19:05 boundary never force-
        # exits — it closes deterministically at the last bar's CLOSE (no crash,
        # the standard end-of-data path).
        bars = [
            _bar(18, 30, "100", "100", "100", "100", day=_EVENING_MUHURAT),
            _bar(18, 31, "100", "100", "100", "100", day=_EVENING_MUHURAT),
            _bar(18, 45, "100", "100.50", "99.80", "100.40", day=_EVENING_MUHURAT),
        ]
        trades = _trade_with_signal_then_ride(bars)
        assert len(trades) == 1
        assert trades[0].exit_ts == _ts(18, 45, day=_EVENING_MUHURAT)  # last close
        assert trades[0].pnl_paisa == 40  # last CLOSE 100.40, not a forced fill

    def test_non_session_date_falls_back_to_1520_no_crash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A bar dated on a date with NO trading session (get_session → None) must
        # not crash; the resolver returns None and is_forced_exit_bar falls back
        # to the standard 15:20 boundary. A contiguous real feed never hits this.
        monkeypatch.setattr("brutex.engine.trade_sim.get_session", lambda d: None)
        bars = [
            _bar(15, 18, "100", "100", "100", "100"),
            _bar(15, 19, "100", "100", "100", "100"),
            _bar(15, 20, "100.30", "101", "100", "100.50"),
        ]
        trades = _trade_with_signal_then_ride(bars)
        assert len(trades) == 1
        assert trades[0].exit_ts == _ts(15, 20)  # default 15:20 boundary
        assert trades[0].pnl_paisa == 30

    def test_boundary_cache_resolves_once_per_date(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # O(1)-per-bar / O(1)-space: the boundary is resolved ONCE per IST date
        # and memoised (1-slot, last-date), so a 3-bar single-day feed calls
        # get_session exactly once.
        calls: list[date] = []
        import brutex.engine.trade_sim as ts_mod

        real_get_session = ts_mod.get_session

        def _counting_get_session(d: date) -> TradingSession | None:
            calls.append(d)
            return real_get_session(d)

        monkeypatch.setattr(ts_mod, "get_session", _counting_get_session)
        bars = [
            _bar(15, 17, "100", "100", "100", "100"),
            _bar(15, 18, "100", "100", "100", "100"),
            _bar(15, 20, "100.30", "101", "100", "100.50"),
        ]
        _trade_with_signal_then_ride(bars)
        # All three bars are the same IST date → one resolve (the rest hit cache).
        assert calls == [_DAY]
