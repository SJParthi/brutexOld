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
from brutex.engine import trade_sim as ts_mod
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
# The 2021-02-24 NSE exchange-outage EXTENDED session (close 17:00 IST at
# NSE + BSE + MSEI per the SEBI press releases, verified 2026-07-11) →
# forced exit 16:50 (DEC-EXTENDED-SESSION-OUTAGE-001, the close−10min law).
_EXTENDED_OUTAGE = date(2021, 2, 24)


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

    def test_extended_outage_day_1525_bar_is_not_forced(self) -> None:
        # DEC-EXTENDED-SESSION-OUTAGE-001 (fails-before pin): on the 2021-02-24
        # extended day (close 17:00) a 15:25 bar is NOT a forced-exit bar — on
        # the pre-fix resolver every bar >= 15:20 forced out immediately. The
        # trade rides through the old 15:20 boundary and exits at 16:50.
        bars = [
            _bar(15, 18, "100", "100", "100", "100", day=_EXTENDED_OUTAGE),
            _bar(15, 25, "100", "100", "100", "100", day=_EXTENDED_OUTAGE),  # NOT forced
            _bar(16, 49, "100", "100", "100", "100", day=_EXTENDED_OUTAGE),  # NOT forced
            _bar(16, 50, "100.60", "101", "100", "100.80", day=_EXTENDED_OUTAGE),  # forced
        ]
        trades = _trade_with_signal_then_ride(bars)
        assert len(trades) == 1
        assert trades[0].exit_ts == _ts(16, 50, day=_EXTENDED_OUTAGE)  # NOT 15:20
        assert trades[0].pnl_paisa == 60  # forced at the 16:50 bar OPEN 100.60
        assert trades[0].holding_bars == 3  # 15:25, 16:49, 16:50

    def test_extended_outage_neighbor_day_still_exits_at_1520(self) -> None:
        # The delta is ONE date: the identical scenario on the EVE (2021-02-23,
        # a regular Tuesday) still force-exits at 15:20 — byte-identical law.
        eve = date(2021, 2, 23)
        bars = [
            _bar(15, 18, "100", "100", "100", "100", day=eve),
            _bar(15, 19, "100", "100", "100", "100", day=eve),
            _bar(15, 20, "100.30", "101", "100", "100.50", day=eve),
            _bar(15, 21, "100", "100", "100", "100", day=eve),
        ]
        trades = _trade_with_signal_then_ride(bars)
        assert len(trades) == 1
        assert trades[0].exit_ts == _ts(15, 20, day=eve)
        assert trades[0].pnl_paisa == 30

    def test_circuit_halt_day_keeps_regular_1520_exit(self) -> None:
        # DEC-HALT-DAY-TREATMENT-001: the annotated 2020-03-23 halt day stays a
        # full REGULAR session — the forced exit is the ordinary 15:20, and the
        # halt annotation changes NOTHING in the trade sim.
        halt_day = date(2020, 3, 23)
        bars = [
            _bar(15, 18, "100", "100", "100", "100", day=halt_day),
            _bar(15, 19, "100", "100", "100", "100", day=halt_day),
            _bar(15, 20, "100.20", "101", "100", "100.40", day=halt_day),
        ]
        trades = _trade_with_signal_then_ride(bars)
        assert len(trades) == 1
        assert trades[0].exit_ts == _ts(15, 20, day=halt_day)
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
        # ``get_session_checked`` seam so the real ``_forced_exit_seconds``
        # resolver runs.
        half_day = date(2024, 1, 2)
        session = TradingSession(
            date=half_day,
            session_type=SessionType.HALF_DAY,
            open_time_ist=time(9, 15),
            close_time_ist=time(12, 30),
        )
        monkeypatch.setattr(
            ts_mod,
            "get_session_checked",
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
        # A bar dated on an IN-COVERAGE date with NO trading session
        # (get_session_checked → None) must not crash; the resolver returns None
        # and is_forced_exit_bar falls back to the standard 15:20 boundary. A
        # contiguous real feed never hits this. (An OUT-of-coverage weekday now
        # raises instead — see TestOutOfCoverageDateRefusesLoudly.)
        monkeypatch.setattr(ts_mod, "get_session_checked", lambda d: None)
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
        # get_session_checked exactly once.
        calls: list[date] = []
        real_get_session = ts_mod.get_session_checked

        def _counting_get_session(d: date) -> TradingSession | None:
            calls.append(d)
            return real_get_session(d)

        monkeypatch.setattr(ts_mod, "get_session_checked", _counting_get_session)
        bars = [
            _bar(15, 17, "100", "100", "100", "100"),
            _bar(15, 18, "100", "100", "100", "100"),
            _bar(15, 20, "100.30", "101", "100", "100.50"),
        ]
        _trade_with_signal_then_ride(bars)
        # All three bars are the same IST date → one resolve (the rest hit cache).
        assert calls == [_DAY]

    def test_multi_day_feed_re_resolves_boundary_per_date(self) -> None:
        # A SINGLE simulate_trades call over a feed spanning TWO distinct IST
        # dates pins the 1-slot cache RE-RESOLVE-on-date-change branch: day-1 is
        # a regular day (forced 15:20), day-2 is the 2025 afternoon Muhurat
        # (forced 14:35). Two independent trades, each force-exiting at its OWN
        # session's boundary, prove the boundary is re-resolved on the date step.
        bars = [
            # Day-1 (regular): signal @15:17 → enter @15:18 → forced @15:20.
            _bar(15, 17, "100", "100", "100", "100"),
            _bar(15, 18, "100", "100", "100", "100"),
            _bar(15, 20, "100.30", "101", "100", "100.50"),
            # Day-2 (afternoon Muhurat): signal @14:33 → enter @14:34 → forced @14:35.
            _bar(14, 33, "100", "100", "100", "100", day=_AFTERNOON_MUHURAT),
            _bar(14, 34, "100", "100", "100", "100", day=_AFTERNOON_MUHURAT),
            _bar(14, 35, "100.10", "101", "100", "100.30", day=_AFTERNOON_MUHURAT),
        ]
        signals = [True, False, False, True, False, False]
        trades = simulate_trades(
            bars,
            signals,
            direction=Side.BUY,
            tf_minutes=1,
            stop_loss_paisa=100_000,
            take_profit_paisa=100_000,
        )
        assert len(trades) == 2
        assert trades[0].exit_ts == _ts(15, 20)  # day-1 regular boundary
        assert trades[1].exit_ts == _ts(14, 35, day=_AFTERNOON_MUHURAT)  # day-2 Muhurat
        assert trades[0].pnl_paisa == 30
        assert trades[1].pnl_paisa == 10


class TestOutOfCoverageDateRefusesLoudly:
    """Calendar-correctness pack: the OLD silent fabrication is GONE.

    A bar dated on a weekday OUTSIDE the published holiday span used to get a
    silently-FABRICATED regular 15:20 forced-exit boundary (``get_session``
    treats an unknown weekday as a trading day). The walker now resolves the
    session via the strict ``get_session_checked``, so the same feed raises
    ``HolidayDataUnavailableError`` — loud (quarantined per tuple by
    DEC-ENGINE-TUPLE-SKIP-RESILIENT-001), never a silent wrong boundary.
    """

    _OUT_DAY = date(2027, 1, 4)  # Monday, above the 2026 coverage ceiling

    def test_out_of_coverage_weekday_bar_raises_loud(self) -> None:
        from brutex.calendars.nse import HolidayDataUnavailableError

        bars = [
            _bar(9, 15, "100", "100", "100", "100", day=self._OUT_DAY),
            _bar(9, 16, "100", "100", "100", "100", day=self._OUT_DAY),
        ]
        # The forced-exit check (and thus the session resolve) runs the moment a
        # pending entry needs managing — the second push hits the resolver.
        with pytest.raises(HolidayDataUnavailableError):
            _trade_with_signal_then_ride(bars)

    def test_in_coverage_behaviour_unchanged(self) -> None:
        # Byte-identical regression on an in-coverage regular day.
        bars = [
            _bar(15, 18, "100", "100", "100", "100"),
            _bar(15, 19, "100", "100", "100", "100"),
            _bar(15, 20, "100.30", "101", "100", "100.50"),
        ]
        trades = _trade_with_signal_then_ride(bars)
        assert len(trades) == 1
        assert trades[0].exit_ts == _ts(15, 20)
        assert trades[0].pnl_paisa == 30


# ────────────────────────────────────────────────────────────────────────
# Cross-session pending-entry drop (R2-OC-2 — the overnight-entry leak)
# ────────────────────────────────────────────────────────────────────────

_NEXT_DAY = date(2024, 1, 3)  # the next NSE session (Wednesday)
_FRIDAY = date(2024, 1, 5)  # a regular NSE trading Friday
_MONDAY = date(2024, 1, 8)  # the Monday after the weekend gap


class TestCrossSessionPendingEntryDrop:
    """R2-OC-2: a pending entry never fills across a trading-day boundary.

    Pre-fix, a signal on a session's LAST stored bar (e.g. 15:29 IST) armed
    ``_pending_entry`` and the next pushed bar — the NEXT session's 09:15 open,
    ~18h later — opened the position, silently capturing the whole overnight
    gap at zero slippage. The fill gate now drops a pending entry whose fill
    bar falls on a different IST session date than its signal bar.
    """

    @pytest.mark.parametrize("direction", [Side.BUY, Side.SELL])
    def test_last_bar_signal_does_not_fill_next_session(self, direction: Side) -> None:
        # THE regression: signal on day-1's last stored bar (15:29 IST); the
        # feed then steps to day-2's 09:15 open with a 19-point overnight gap.
        # Pre-fix: one trade entering at 119.00 next-session open. Now: NO trade.
        bars = [
            _bar(15, 28, "100", "101", "99", "100"),
            _bar(15, 29, "100", "101", "99", "100"),  # signal on the session's last bar
            _bar(9, 15, "119", "120", "118", "119", day=_NEXT_DAY),  # +19 gap
            _bar(9, 16, "119.50", "120.50", "119", "119.50", day=_NEXT_DAY),
        ]
        signals = [False, True, False, False]
        trades = simulate_trades(bars, signals, direction=direction, tf_minutes=1)
        assert trades == []

    def test_1528_signal_still_dropped_by_forced_rule(self) -> None:
        # Regression 2 — the PRE-EXISTING forced-bar drop is untouched: a signal
        # at 15:28 arms, its fill bar 15:29 is >= 15:20 (a forced-exit bar) so
        # the entry drops SAME-session; nothing may fill on day-2 either.
        bars = [
            _bar(15, 28, "100", "101", "99", "100"),  # signal
            _bar(15, 29, "100", "101", "99", "100"),  # forced bar → drop (existing rule)
            _bar(9, 15, "119", "120", "118", "119", day=_NEXT_DAY),
            _bar(9, 16, "119.50", "120.50", "119", "119.50", day=_NEXT_DAY),
        ]
        signals = [True, False, False, False]
        trades = simulate_trades(bars, signals, direction=Side.BUY, tf_minutes=1)
        assert trades == []

    def test_mid_day_signal_unchanged_exact_trade(self) -> None:
        # Regression 3 — a mid-session signal is byte-for-byte UNCHANGED by the
        # fix (verified against the pre-fix walker): entry at the next bar's
        # open, forced exit at 15:20, exact ClosedTrade pinned field-by-field.
        bars = [
            _bar(11, 0, "100", "101", "99", "100"),  # signal
            _bar(11, 1, "100.50", "102", "100", "101"),  # entry @ open 100.50
            _bar(11, 2, "101", "103", "100.25", "102"),  # ride
            _bar(15, 20, "104", "105", "103", "104.50"),  # forced @ open 104.00
            _bar(9, 15, "90", "91", "89", "90", day=_NEXT_DAY),  # next session
        ]
        signals = [True, False, False, False, False]
        trades = simulate_trades(bars, signals, direction=Side.BUY, tf_minutes=1)
        assert trades == [
            ClosedTrade(
                pnl_paisa=350,  # forced fill 10400 − entry 10050
                mfe_paisa=350,  # (open, open) fold at the forced bar
                mae_paisa=50,  # bar 11:01 low 100.00 vs entry 100.50
                holding_bars=3,
                entry_ts=_ts(11, 1),
                exit_ts=_ts(15, 20),
                direction=Side.BUY,
            )
        ]

    def test_single_session_feed_unchanged_exact_trade(self) -> None:
        # A single-session feed is byte-identical to the pre-fix walker: the
        # same-session fill path is the one every prior test exercised.
        bars = [
            _bar(10, 0, "100", "101", "99", "100"),  # signal
            _bar(10, 1, "100.20", "101", "100", "100.80"),  # entry @ open 100.20
            _bar(10, 2, "100.80", "101.50", "100.50", "101"),  # end-of-data close
        ]
        signals = [True, False, False]
        trades = simulate_trades(bars, signals, direction=Side.BUY, tf_minutes=1)
        assert trades == [
            ClosedTrade(
                pnl_paisa=80,  # close 10100 − entry 10020
                mfe_paisa=130,  # high 101.50 vs entry 100.20
                mae_paisa=20,  # low 100.00 vs entry 100.20
                holding_bars=2,
                entry_ts=_ts(10, 1),
                exit_ts=_ts(10, 2),
                direction=Side.BUY,
            )
        ]

    def test_signal_on_final_feed_bar_never_opens(self) -> None:
        # A signal on the WHOLE feed's last bar has no next bar at all: the
        # pending entry evaporates at finish() (pre-existing path, kept pinned).
        bars = [
            _bar(10, 0, "100", "101", "99", "100"),
            _bar(10, 1, "100.20", "101", "100", "100.80"),  # signal, then feed ends
        ]
        signals = [False, True]
        trades = simulate_trades(bars, signals, direction=Side.BUY, tf_minutes=1)
        assert trades == []

    @pytest.mark.parametrize(
        ("day1", "day2"),
        [(_DAY, _NEXT_DAY), (_FRIDAY, _MONDAY)],
        ids=["one-calendar-day", "weekend-gap"],
    )
    def test_gap_width_is_irrelevant_both_drop(self, day1: date, day2: date) -> None:
        # The drop keys on SESSION-date inequality, not gap width: a Tue→Wed
        # step and a Fri→Mon weekend step both drop the pending entry.
        bars = [
            _bar(15, 29, "100", "101", "99", "100", day=day1),  # signal
            _bar(9, 15, "119", "120", "118", "119", day=day2),
            _bar(9, 16, "119.50", "120.50", "119", "119.50", day=day2),
        ]
        signals = [True, False, False]
        trades = simulate_trades(bars, signals, direction=Side.BUY, tf_minutes=1)
        assert trades == []

    def test_next_session_signal_still_fills_within_its_session(self) -> None:
        # The drop never eats a GENUINE next-session signal: day-1's last-bar
        # signal drops at day-2's 09:15 bar, but a signal ON that 09:15 bar
        # re-arms (on a bar that ran the forced check — the resolved-date reuse
        # path in (C)) and fills at 09:16 within its own session.
        bars = [
            _bar(15, 29, "100", "101", "99", "100"),  # day-1 signal → armed
            _bar(9, 15, "119", "120", "118", "119", day=_NEXT_DAY),  # drop; signal → re-arm
            _bar(9, 16, "119.50", "120.50", "119", "119.60", day=_NEXT_DAY),  # entry
            _bar(9, 17, "119.80", "120.80", "119.30", "120", day=_NEXT_DAY),
        ]
        signals = [True, True, False, False]
        trades = simulate_trades(bars, signals, direction=Side.BUY, tf_minutes=1)
        assert len(trades) == 1
        assert trades[0].entry_ts == _ts(9, 16, day=_NEXT_DAY)
        assert trades[0].pnl_paisa == 50  # close 120.00 − entry 119.50

    def test_streaming_push_drops_cross_session_like_run(self) -> None:
        # The streaming push/finish path shares the state machine: the drop
        # behaves identically bar-by-bar.
        walker = TradeSimWalker(direction=Side.BUY, tf_minutes=1)
        assert walker.push(_bar(15, 29, "100", "101", "99", "100"), entry_signal=True) is None
        assert (
            walker.push(_bar(9, 15, "119", "120", "118", "119", day=_NEXT_DAY), entry_signal=False)
            is None
        )
        assert (
            walker.push(
                _bar(9, 16, "119.50", "120.50", "119", "119.50", day=_NEXT_DAY),
                entry_signal=False,
            )
            is None
        )
        assert walker.finish() is None


# ────────────────────────────────────────────────────────────────────────
# Open-position session-boundary close (DEC-TRADESIM-OPEN-SESSION-GUARD-001)
#
# The OPEN-position twin of R2-OC-2: pre-fix, a position left open because its
# session's stored bars ended BEFORE any forced-exit-window bar (a session-tail
# data hole) silently survived the overnight gap and closed against the NEXT
# session's prices — a pending next-open exit filled at the next session's
# open, SL/TP triggered against the gap, or the next day's 15:20 bar forced it
# out — folding the whole overnight move into pnl AND the gap bar's range into
# MFE/MAE (executed repro: +30000/+50000 paisa fabricated per trade on a +₹500
# gap). Now the first bar of the new session closes the position retroactively
# at the PRIOR session's last stored bar (the finish() idiom) under the
# ``session_gap`` reason. Fixtures mirror the executed repro: day D =
# 2024-01-02 bars 11:10-11:14 (tail hole — nothing at/after 15:20), day D+1 =
# 2024-01-03 opening ₹600 (+₹500 overnight gap).
# ────────────────────────────────────────────────────────────────────────


def _gap_bar(hh: int, mm: int, o: str, h: str, lo: str, c: str) -> CandleBar:
    """A day-D+1 (2024-01-03) bar — the far side of the overnight gap."""
    return _bar(hh, mm, o, h, lo, c, day=_NEXT_DAY)


class TestOpenPositionSessionGapClose:
    """An open position never survives an IST session-date change."""

    def _day_d_hold(self) -> list[CandleBar]:
        """Signal @11:10 → entry @11:11 open ₹100; flat hold through 11:14."""
        return [
            _bar(11, 10, "100", "100", "100", "100"),  # signal
            _bar(11, 11, "100", "100", "100", "100"),  # entry @ open 100.00
            _bar(11, 12, "100", "100", "100", "100"),
            _bar(11, 13, "100", "100", "100", "100"),
            _bar(11, 14, "100", "100", "100", "100"),  # day-D's LAST stored bar
        ]

    def test_v1_gap_bar_tp_hit_becomes_session_gap_close(self) -> None:
        # V1 (executed repro): the gap bar's range "hits" the ₹300 TP level —
        # pre-fix a take_profit fill at 400.00 on day D+1 (+30000 paisa
        # fabricated). Now: session_gap close at day-D's 11:14 close, pnl 0,
        # and the gap bar's range NEVER enters MFE/MAE.
        bars = [*self._day_d_hold(), _gap_bar(9, 15, "600", "601", "599", "600")]
        signals = [True, False, False, False, False, False]
        sink: list[ts_mod.TradeDetail] = []
        trades = simulate_trades(
            bars,
            signals,
            direction=Side.BUY,
            tf_minutes=1,
            take_profit_paisa=30_000,
            detail_sink=sink,
        )
        assert trades == [
            ClosedTrade(
                pnl_paisa=0,
                mfe_paisa=0,
                mae_paisa=0,
                holding_bars=4,
                entry_ts=_ts(11, 11),
                exit_ts=_ts(11, 14),  # day-D's last stored bar — never day D+1
                direction=Side.BUY,
            )
        ]
        assert [d.exit_reason for d in sink] == ["session_gap"]
        assert sink[0].exit_fill_paisa == 10_000  # day-D 11:14 CLOSE

    def test_v1b_next_day_forced_bar_becomes_session_gap_close(self) -> None:
        # V1b: pre-fix the position rode all of day D+1 to its 15:20 forced
        # bar (+50000 paisa fabricated). The guard fires at day D+1's FIRST
        # bar — the forced flag never gets a say (never forced_1520 on D+1).
        bars = [
            *self._day_d_hold(),
            _gap_bar(9, 15, "600", "601", "599", "600"),
            _gap_bar(15, 20, "600", "601", "599", "600"),  # D+1's forced bar
        ]
        signals = [True] + [False] * 6
        sink: list[ts_mod.TradeDetail] = []
        trades = simulate_trades(bars, signals, direction=Side.BUY, tf_minutes=1, detail_sink=sink)
        assert len(trades) == 1
        assert trades[0].exit_ts == _ts(11, 14)
        assert trades[0].pnl_paisa == 0
        assert [d.exit_reason for d in sink] == ["session_gap"]

    def test_v1b_boundary_bar_itself_forced_still_session_gap(self) -> None:
        # Edge pin (§8.6): day D+1's FIRST stored bar is itself ≥ 15:20 — the
        # guard fires before the forced branch can, so the close is still the
        # session_gap close at day-D's last bar, never a forced fill at 600.
        bars = [*self._day_d_hold(), _gap_bar(15, 20, "600", "601", "599", "600")]
        signals = [True] + [False] * 5
        sink: list[ts_mod.TradeDetail] = []
        trades = simulate_trades(bars, signals, direction=Side.BUY, tf_minutes=1, detail_sink=sink)
        assert len(trades) == 1
        assert trades[0].exit_ts == _ts(11, 14)
        assert trades[0].pnl_paisa == 0
        assert [d.exit_reason for d in sink] == ["session_gap"]

    def test_v2_pending_next_open_exit_is_subsumed(self) -> None:
        # V2: a NEXT_OPEN stop scheduled on day-D's LAST stored bar pre-fix
        # filled at day D+1's 09:15 open (+50000 paisa fabricated). Now the
        # boundary close SUBSUMES the pending exit: fill = day-D's last CLOSE.
        # This pins the flat-walker TRADE SHAPE only (exactly one trade,
        # priced on day D) — with no day-D+1 entry there is no position a
        # stale pending could bite, so the CLEAR's killing teeth live in
        # test_cross_session_stale_pending_exit_never_reaches_a_new_position.
        bars = [
            _bar(11, 10, "100", "101", "99", "100"),  # signal
            _bar(11, 11, "104", "104", "103.50", "104"),  # entry @ open 104.00
            # low 100.00 hits the −400 stop (level 100.00) → NEXT_OPEN pending.
            _bar(11, 12, "101", "104.50", "100", "101"),  # day-D's LAST stored bar
            _gap_bar(9, 15, "600", "601", "599", "600"),
            _gap_bar(9, 16, "600", "601", "599", "600"),
        ]
        signals = [True] + [False] * 4
        sink: list[ts_mod.TradeDetail] = []
        trades = simulate_trades(
            bars,
            signals,
            direction=Side.BUY,
            tf_minutes=1,
            stop_loss_paisa=400,
            detail_sink=sink,
            policy=FillPolicy(stop_loss=LevelFill.NEXT_OPEN),
        )
        assert trades == [
            ClosedTrade(
                pnl_paisa=10_100 - 10_400,  # day-D 11:12 CLOSE 101.00 − entry 104.00
                mfe_paisa=50,  # high 104.50 on 11:12
                mae_paisa=400,  # low 100.00 on 11:12
                holding_bars=2,
                entry_ts=_ts(11, 11),
                exit_ts=_ts(11, 12),
                direction=Side.BUY,
            )
        ]
        assert [d.exit_reason for d in sink] == ["session_gap"]

    def test_v3_pending_signal_exit_is_subsumed(self) -> None:
        # V3: a Mode-B SIGNAL exit armed on day-D's last stored bar pre-fix
        # filled at day D+1's open. Same subsumption as V2.
        bars = [*self._day_d_hold(), _gap_bar(9, 15, "600", "601", "599", "600")]
        entry = [True] + [False] * 5
        exits = [False, False, False, False, True, False]  # signal on 11:14
        sink: list[ts_mod.TradeDetail] = []
        trades = simulate_trades(
            bars,
            entry,
            direction=Side.BUY,
            tf_minutes=1,
            exit_signals=exits,
            detail_sink=sink,
        )
        assert len(trades) == 1
        assert trades[0].exit_ts == _ts(11, 14)
        assert trades[0].pnl_paisa == 0
        assert [d.exit_reason for d in sink] == ["session_gap"]

    def test_cross_session_stale_pending_exit_never_reaches_a_new_position(self) -> None:
        """The (A2) pending-exit CLEAR has teeth (the Rust directed test's Python twin).

        After the boundary close, a NEW day-D+1 position must ride normally to
        end of data — under a mutant that drops the ``self._pending_exit =
        None`` clear in the (A2) guard, the stale day-D NEXT_OPEN stop pending
        would close the new position instantly at its first managed bar's open
        (its own ENTRY bar, managed same-push) with the stale ``stop_loss``
        reason. Day-D+1 lows stay ABOVE the second trade's own stop level
        (entry 605.00 − 4.00 = 601.00), so only a stale pending exit could
        close it before the end of data. ONE killer test covers BOTH pending
        kinds: the NEXT_OPEN SL/TP pending and the Mode-B SIGNAL pending share
        the ONE ``_pending_exit`` slot, so the single (A2) clear subsumes both
        (V2/V3 pin each shape's boundary-close fill; this test pins the CLEAR
        itself).
        """
        bars = [
            _bar(11, 10, "100", "101", "99", "100"),  # signal
            _bar(11, 11, "104", "104", "103.50", "104"),  # entry @ open 104.00
            # low 100.00 hits the −400 stop (level 100.00) → NEXT_OPEN pending
            # armed on day-D's LAST stored bar.
            _bar(11, 12, "101", "104.50", "100", "101"),
            _gap_bar(9, 15, "600", "601", "599", "600"),  # boundary → session_gap
            _gap_bar(9, 16, "600", "601", "599", "600"),  # signal → earliest re-arm
            _gap_bar(9, 17, "605", "606", "604", "605"),  # NEW entry @ open 605.00
            _gap_bar(9, 18, "607", "608", "606", "607.50"),  # rides → EOD close
        ]
        signals = [True, False, False, False, True, False, False]
        sink: list[ts_mod.TradeDetail] = []
        trades = simulate_trades(
            bars,
            signals,
            direction=Side.BUY,
            tf_minutes=1,
            stop_loss_paisa=400,
            detail_sink=sink,
            policy=FillPolicy(stop_loss=LevelFill.NEXT_OPEN),
        )
        assert len(trades) == 2
        assert trades[0].exit_ts == _ts(11, 12)  # the V2-shape session_gap close
        assert trades[0].pnl_paisa == 10_100 - 10_400
        # The NEW position rides to end of data — never an instant
        # stale-pending close at its first managed bar (which would read
        # exit_ts == entry_ts with the stale ``stop_loss`` reason).
        assert trades[1].entry_ts == _ts(9, 17, day=_NEXT_DAY)
        assert trades[1].exit_ts == _ts(9, 18, day=_NEXT_DAY)
        assert trades[1].pnl_paisa == 60_750 - 60_500  # EOD close 607.50 − entry 605.00
        assert trades[1].holding_bars == 2
        assert [d.exit_reason for d in sink] == ["session_gap", "end_of_data"]

    def test_sell_direction_symmetric_session_gap_close(self) -> None:
        # SELL variant of V1 (§8.9): a short pre-fix would have "profited" the
        # inverse gap; the boundary close is direction-aware through _close.
        bars = [*self._day_d_hold(), _gap_bar(9, 15, "600", "601", "599", "600")]
        signals = [True] + [False] * 5
        sink: list[ts_mod.TradeDetail] = []
        trades = simulate_trades(bars, signals, direction=Side.SELL, tf_minutes=1, detail_sink=sink)
        assert len(trades) == 1
        assert trades[0].direction is Side.SELL
        assert trades[0].pnl_paisa == 0  # entry 100.00 − day-D close 100.00
        assert trades[0].exit_ts == _ts(11, 14)
        assert [d.exit_reason for d in sink] == ["session_gap"]

    def test_boundary_bar_never_arms_earliest_rearm_next_bar(self) -> None:
        # §2.2 pinned law: the boundary bar EMITS the close, so step (C)'s
        # `closed is None` gate suppresses arming ON it. A signal on the
        # boundary bar produces nothing; the earliest re-arm is bar i+1 and
        # the earliest new fill is bar i+2 (same-session, unforced).
        bars = [
            *self._day_d_hold(),
            _gap_bar(9, 15, "600", "601", "599", "600"),  # boundary (i) — signal ignored
            _gap_bar(9, 16, "600", "601", "599", "600"),  # i+1 — earliest re-arm
            _gap_bar(9, 17, "605", "606", "604", "605"),  # i+2 — earliest new fill
            _gap_bar(9, 18, "607", "608", "606", "607.50"),
        ]
        signals = [True, False, False, False, False, True, True, False, False]
        trades = simulate_trades(bars, signals, direction=Side.BUY, tf_minutes=1)
        assert len(trades) == 2
        assert trades[0].exit_ts == _ts(11, 14)  # the session_gap close
        assert trades[1].entry_ts == _ts(9, 17, day=_NEXT_DAY)  # fill at i+2, never i/i+1
        assert trades[1].pnl_paisa == 60_750 - 60_500  # EOD close 607.50 − entry 605.00

    def test_multi_session_hole_fires_once_same_fill(self) -> None:
        # §8.1: the compare is date INEQUALITY, not adjacency — a k → k+2
        # step (day-D → Friday, skipping Wednesday+Thursday... here Tue → Fri)
        # fires the guard exactly once with the identical fill.
        bars = [
            _bar(11, 10, "100", "100", "100", "100", day=_DAY),
            _bar(11, 11, "100", "100", "100", "100", day=_DAY),
            _bar(11, 12, "100", "100.30", "99.90", "100.10", day=_DAY),
            _bar(9, 15, "600", "601", "599", "600", day=_FRIDAY),  # 3 calendar days later
        ]
        signals = [True, False, False, False]
        sink: list[ts_mod.TradeDetail] = []
        trades = simulate_trades(bars, signals, direction=Side.BUY, tf_minutes=1, detail_sink=sink)
        assert trades == [
            ClosedTrade(
                pnl_paisa=10,  # 11:12 close 100.10 − entry 100.00
                mfe_paisa=30,
                mae_paisa=10,
                holding_bars=2,
                entry_ts=_ts(11, 11),
                exit_ts=_ts(11, 12),
                direction=Side.BUY,
            )
        ]
        assert [d.exit_reason for d in sink] == ["session_gap"]

    def test_entry_on_sessions_last_stored_bar_degenerate_close(self) -> None:
        # §8.7: entry fills ON day-D's last stored bar → holding_bars == 1 and
        # pnl = that bar's close − its open (the degenerate one-bar close).
        bars = [
            _bar(11, 13, "100", "100", "100", "100"),  # signal
            _bar(11, 14, "100.20", "100.60", "100.10", "100.45"),  # entry AND last stored bar
            _gap_bar(9, 15, "600", "601", "599", "600"),
        ]
        signals = [True, False, False]
        sink: list[ts_mod.TradeDetail] = []
        trades = simulate_trades(bars, signals, direction=Side.BUY, tf_minutes=1, detail_sink=sink)
        assert trades == [
            ClosedTrade(
                pnl_paisa=25,  # close 100.45 − open 100.20
                mfe_paisa=40,  # high 100.60 folded by the same-push manage
                mae_paisa=10,  # low 100.10
                holding_bars=1,
                entry_ts=_ts(11, 14),
                exit_ts=_ts(11, 14),
                direction=Side.BUY,
            )
        ]
        assert [d.exit_reason for d in sink] == ["session_gap"]

    def test_streaming_push_emits_the_boundary_close_on_the_boundary_bar(self) -> None:
        # Streaming twin: push() returns the session_gap trade ON the boundary
        # bar (emitted there, priced at day-D's last stored bar); finish() then
        # has nothing left.
        walker = TradeSimWalker(direction=Side.BUY, tf_minutes=1)
        for bar in self._day_d_hold():
            assert walker.push(bar, entry_signal=bar.timestamp == _ts(11, 10)) is None
        closed = walker.push(_gap_bar(9, 15, "600", "601", "599", "600"), entry_signal=False)
        assert closed is not None
        assert closed.exit_ts == _ts(11, 14)
        assert closed.pnl_paisa == 0
        assert walker.finish() is None

    def test_entry_day_is_the_ist_session_date_not_the_utc_date(self) -> None:
        """The (A) fill stamps ``entry_day_ist`` from the IST session date, never
        the UTC calendar date (refuter round 4, directed mutant pin).

        The ``bar.timestamp.date()`` (UTC-date) mutant at the ``_open_position``
        call survived the suite because the mismatch is domain-bounded — no NSE
        session bar exists before 05:30 IST post-ingest-filter, so IST and UTC
        dates always agree on real lake bars. The guard's DOCUMENTED semantics
        are the IST session date, so this pins that law beyond the NSE trading
        window (defense-in-depth; the domain never produces such bars): an
        entry fill at IST 2024-01-02 01:00 is UTC 2024-01-01 19:30 — under the
        mutant ``entry_day_ist`` reads 2024-01-01 and the (A2) guard
        phantom-session_gap-closes the just-opened position on its OWN entry
        bar; the correct IST stamp HOLDS through the later same-IST-session bar
        (IST 05:00 = UTC 2024-01-01 23:30) to the honest end_of_data finish.
        """
        bars = [
            _flat(0, 55),  # signal — IST date 2024-01-02, UTC date 2024-01-01
            _flat(1, 0),  # entry fill @ open — UTC date still 2024-01-01
            _flat(5, 0),  # same IST session date — the position must HOLD
        ]
        signals = [True, False, False]
        sink: list[ts_mod.TradeDetail] = []
        trades = simulate_trades(bars, signals, direction=Side.BUY, tf_minutes=1, detail_sink=sink)
        assert trades == [
            ClosedTrade(
                pnl_paisa=0,
                mfe_paisa=0,
                mae_paisa=0,
                holding_bars=2,
                entry_ts=_ts(1, 0),
                exit_ts=_ts(5, 0),
                direction=Side.BUY,
            )
        ]
        assert [d.exit_reason for d in sink] == ["end_of_data"]

    def test_backward_session_date_change_still_closes_session_gap(self) -> None:
        """The (A2) compare is an EQUALITY law: ANY IST session-date CHANGE
        closes the open position — either direction, not only forward (the
        Python defense-in-depth twin of the Rust r8 M14 ``!=``→``>`` mutant
        killer ``cross_session_backward_session_ordinal_still_closes_boundary``).

        ``push()`` derives each bar's session date from that bar's OWN
        timestamp and enforces no monotonicity ("chronologically ordered" is a
        documented input contract, not a checked one — verified before adding
        this test), so a backward-dated bar IS API-reachable; production sweep
        feeds are chronological (Catalog/ReplayFeed sorted), hence
        LOW/defense-in-depth. Under a ``day_ist > pos.entry_day_ist`` mutant
        the earlier-dated bar folds into the open position and it rides to a
        wrong end_of_data finish at the backward bar's close instead of the
        session_gap boundary close.
        """
        bars = [
            _bar(11, 10, "100", "100", "100", "100", day=_NEXT_DAY),  # signal
            _bar(11, 11, "100", "102", "99", "101", day=_NEXT_DAY),  # entry @ open 100.00
            _bar(11, 12, "100", "101", "99", "100", day=_DAY),  # EARLIER IST date
        ]
        signals = [True, False, False]
        sink: list[ts_mod.TradeDetail] = []
        trades = simulate_trades(bars, signals, direction=Side.BUY, tf_minutes=1, detail_sink=sink)
        assert trades == [
            ClosedTrade(
                pnl_paisa=100,  # 11:11 close 101.00 − entry 100.00
                mfe_paisa=200,  # high 102.00 folded by the same-push manage
                mae_paisa=100,  # low 99.00
                holding_bars=1,
                entry_ts=_ts(11, 11, day=_NEXT_DAY),
                exit_ts=_ts(11, 11, day=_NEXT_DAY),  # never the backward bar
                direction=Side.BUY,
            )
        ]
        assert [d.exit_reason for d in sink] == ["session_gap"]
        assert sink[0].exit_fill_paisa == 10_100  # day-D+1 11:11 CLOSE


class TestSessionGapFinishEquivalence:
    """The finish()-equivalence pin (suffix independence by construction).

    The SAME day-D prefix closed by (a) the (A2) boundary guard when the feed
    continues into day D+1 vs (b) ``finish()`` when the feed truncates at
    day-D's last stored bar emits a bit-equal ``ClosedTrade`` AND exit index;
    ONLY ``TradeDetail.exit_reason`` differs (``session_gap`` vs
    ``end_of_data``) — DEC-TRADESIM-OPEN-SESSION-GUARD-001 §5.2. VIX stamps
    ride the same (entry_ts, exit_ts) pair in both worlds, so they are
    deterministic and identical too (§8.10).
    """

    def _prefix(self) -> list[CandleBar]:
        return [
            _bar(11, 10, "100", "101", "99", "100"),  # signal
            _bar(11, 11, "100.50", "102", "100", "101"),  # entry @ 100.50
            _bar(11, 12, "101", "103", "100.25", "102"),  # day-D's last stored bar
        ]

    def test_boundary_close_bit_equals_finish_close(self) -> None:
        from brutex.engine.reference_series import TradeReferenceStamper

        prefix = self._prefix()
        vix = [_vix_bar(11, 10, "15.10"), _vix_bar(11, 11, "15.20"), _vix_bar(11, 12, "15.30")]
        stamper = TradeReferenceStamper.from_vix_bars(vix)
        continued = [*prefix, _gap_bar(9, 15, "600", "601", "599", "600")]

        sink_full: list[ts_mod.TradeDetail] = []
        full_trades, full_idx = ts_mod.simulate_trades_with_exit_indices(
            continued,
            [True, False, False, False],
            direction=Side.BUY,
            tf_minutes=1,
            reference_stamper=stamper,
            detail_sink=sink_full,
        )
        sink_trunc: list[ts_mod.TradeDetail] = []
        trunc_trades, trunc_idx = ts_mod.simulate_trades_with_exit_indices(
            prefix,
            [True, False, False],
            direction=Side.BUY,
            tf_minutes=1,
            reference_stamper=stamper,
            detail_sink=sink_trunc,
        )
        assert full_trades == trunc_trades  # bit-equal ClosedTrade (VIX stamps included)
        assert full_idx == trunc_idx == [2]  # same TRUE exit index
        assert [d.exit_reason for d in sink_full] == ["session_gap"]
        assert [d.exit_reason for d in sink_trunc] == ["end_of_data"]
        assert sink_full[0].exit_fill_paisa == sink_trunc[0].exit_fill_paisa == 10_200
        assert full_trades[0].vix_at_exit == Decimal("15.30")


# ────────────────────────────────────────────────────────────────────────
# India VIX reference stamp (DEC-VIX-TRADE-STAMP-001)
# ────────────────────────────────────────────────────────────────────────


def _vix_bar(hh: int, mm: int, close: str, *, day: date = _DAY) -> CandleBar:
    """A flat VIX reference bar at IST ``hh:mm`` (aligned to the index feed)."""
    return _bar(hh, mm, close, close, close, close, day=day)


class TestVixStamp:
    """The reference-series stamper wires the India VIX level onto each trade.

    India VIX is REFERENCE ONLY (CLAUDE.md §1/§3): stamped for downstream
    filter analysis, never a signal input. ``None`` on a missing VIX bar is the
    no-hallucination contract (Golden Rule #1).
    """

    def _index_bars(self) -> list[CandleBar]:
        # signal @09:15 → enter @09:16 OPEN → end-of-data exit @09:17 CLOSE.
        return [
            _bar(9, 15, "100", "100", "100", "100"),
            _bar(9, 16, "101", "101", "101", "101"),
            _bar(9, 17, "102", "102", "102", "102"),
        ]

    def test_no_stamper_leaves_vix_none(self) -> None:
        """Without a stamper, the engine output is byte-identical (both VIX None)."""
        trades = simulate_trades(
            self._index_bars(), [True, False, False], direction=Side.BUY, tf_minutes=1
        )
        assert len(trades) == 1
        assert trades[0].vix_at_entry is None
        assert trades[0].vix_at_exit is None

    def test_stamp_present_at_entry_and_exit(self) -> None:
        """A VIX bar at both the entry (09:16) and exit (09:17) instant is stamped."""
        from brutex.engine.reference_series import TradeReferenceStamper

        vix = [_vix_bar(9, 15, "15.10"), _vix_bar(9, 16, "15.20"), _vix_bar(9, 17, "15.30")]
        trades = simulate_trades(
            self._index_bars(),
            [True, False, False],
            direction=Side.BUY,
            tf_minutes=1,
            reference_stamper=TradeReferenceStamper.from_vix_bars(vix),
        )
        assert len(trades) == 1
        # Entry fills at 09:16, exit at 09:17 — the stamp tracks those instants,
        # NOT the 09:15 signal bar.
        assert trades[0].vix_at_entry == Decimal("15.20")
        assert trades[0].vix_at_exit == Decimal("15.30")

    def test_stamp_missing_at_entry_is_none(self) -> None:
        """No VIX bar at the 09:16 entry instant → ``vix_at_entry`` is None (not faked)."""
        from brutex.engine.reference_series import TradeReferenceStamper

        vix = [_vix_bar(9, 15, "15.10"), _vix_bar(9, 17, "15.30")]  # no 09:16
        trades = simulate_trades(
            self._index_bars(),
            [True, False, False],
            direction=Side.BUY,
            tf_minutes=1,
            reference_stamper=TradeReferenceStamper.from_vix_bars(vix),
        )
        assert trades[0].vix_at_entry is None
        assert trades[0].vix_at_exit == Decimal("15.30")

    def test_stamp_missing_at_exit_is_none(self) -> None:
        """No VIX bar at the 09:17 exit instant → ``vix_at_exit`` is None."""
        from brutex.engine.reference_series import TradeReferenceStamper

        vix = [_vix_bar(9, 15, "15.10"), _vix_bar(9, 16, "15.20")]  # no 09:17
        trades = simulate_trades(
            self._index_bars(),
            [True, False, False],
            direction=Side.BUY,
            tf_minutes=1,
            reference_stamper=TradeReferenceStamper.from_vix_bars(vix),
        )
        assert trades[0].vix_at_entry == Decimal("15.20")
        assert trades[0].vix_at_exit is None

    def test_stamp_missing_at_both_is_none(self) -> None:
        """A timestamp-misaligned / empty VIX series → both fields None, trade still emitted."""
        from brutex.engine.reference_series import TradeReferenceStamper

        trades = simulate_trades(
            self._index_bars(),
            [True, False, False],
            direction=Side.BUY,
            tf_minutes=1,
            reference_stamper=TradeReferenceStamper.from_vix_bars([]),
        )
        assert len(trades) == 1
        assert trades[0].vix_at_entry is None
        assert trades[0].vix_at_exit is None

    def test_stamp_via_streaming_finish_path(self) -> None:
        """The end-of-data ``finish`` close path is also stamped (it routes via ``_close``)."""
        from brutex.engine.reference_series import TradeReferenceStamper

        vix = [_vix_bar(9, 15, "15.10"), _vix_bar(9, 16, "15.20"), _vix_bar(9, 17, "15.30")]
        walker = TradeSimWalker(
            direction=Side.BUY,
            tf_minutes=1,
            reference_stamper=TradeReferenceStamper.from_vix_bars(vix),
        )
        bars = self._index_bars()
        signals = [True, False, False]
        closed = [walker.push(b, entry_signal=s) for b, s in zip(bars, signals, strict=True)]
        assert all(c is None for c in closed)  # ends open → closes in finish()
        final = walker.finish()
        assert final is not None
        assert final.vix_at_entry == Decimal("15.20")
        assert final.vix_at_exit == Decimal("15.30")


# ────────────────────────────────────────────────────────────────────────
# DEC-SWEEP-EXIT-INDEX-FASTPATH-001 — exit-index-carrying walk
# ────────────────────────────────────────────────────────────────────────


class TestExitIndices:
    """run_with_exit_indices / simulate_trades_with_exit_indices."""

    def _bars(self) -> list[CandleBar]:
        """Two closed trades: a TP exit on bar 2 and an end-of-data close on bar 4."""
        return [
            _bar(9, 16, "100", "100", "100", "100"),  # signal arms here
            _bar(9, 17, "100", "100", "100", "100"),  # entry at open
            _bar(9, 18, "100", "101", "100", "101"),  # TP +50 hits (exit idx 2)
            _bar(9, 19, "100", "100", "100", "100"),  # signal arms again
            _bar(9, 20, "100", "100", "100", "100"),  # entry; end-of-data close (idx 4)
        ]

    def _signals(self) -> list[bool]:
        return [True, False, False, True, False]

    def test_indices_align_with_the_trades(self) -> None:
        trades, exit_indices = ts_mod.simulate_trades_with_exit_indices(
            self._bars(),
            self._signals(),
            direction=Side.BUY,
            tf_minutes=1,
            take_profit_paisa=50,
        )
        assert exit_indices is not None
        assert len(trades) == 2
        assert exit_indices == [2, 4]
        assert trades[0].exit_ts == self._bars()[2].timestamp
        assert trades[1].exit_ts == self._bars()[4].timestamp

    def test_trades_are_byte_identical_to_simulate_trades(self) -> None:
        plain = simulate_trades(
            self._bars(),
            self._signals(),
            direction=Side.BUY,
            tf_minutes=1,
            take_profit_paisa=50,
        )
        with_idx, _ = ts_mod.simulate_trades_with_exit_indices(
            self._bars(),
            self._signals(),
            direction=Side.BUY,
            tf_minutes=1,
            take_profit_paisa=50,
        )
        assert plain == with_idx

    def test_uncertified_window_returns_none_indices(self) -> None:
        """Duplicate timestamps fail the bars_strictly_increasing certificate:
        the trades still simulate, the indices are withheld."""
        bars = self._bars()
        bars.append(bars[-1])  # duplicate final timestamp
        signals = [*self._signals(), False]
        trades, exit_indices = ts_mod.simulate_trades_with_exit_indices(
            bars,
            signals,
            direction=Side.BUY,
            tf_minutes=1,
            take_profit_paisa=50,
        )
        assert exit_indices is None
        assert len(trades) == 2

    def test_no_trades_yields_empty_indices(self) -> None:
        trades, exit_indices = ts_mod.simulate_trades_with_exit_indices(
            self._bars(),
            [False] * 5,
            direction=Side.BUY,
            tf_minutes=1,
        )
        assert trades == []
        assert exit_indices == []

    def test_run_with_exit_indices_validates_signal_lengths(self) -> None:
        walker = TradeSimWalker(direction=Side.BUY, tf_minutes=1)
        with pytest.raises(ValueError, match="entry_signals length"):
            walker.run_with_exit_indices(self._bars(), [True])
        with pytest.raises(ValueError, match="exit_signals length"):
            walker.run_with_exit_indices(self._bars(), [False] * 5, [True])

    def test_run_delegates_to_the_index_walk(self) -> None:
        """run() is the thin delegate — same trades, indices dropped."""
        walker_a = TradeSimWalker(direction=Side.BUY, tf_minutes=1, take_profit_paisa=50)
        walker_b = TradeSimWalker(direction=Side.BUY, tf_minutes=1, take_profit_paisa=50)
        via_run = walker_a.run(self._bars(), self._signals())
        via_pair, _ = walker_b.run_with_exit_indices(self._bars(), self._signals())
        assert via_run == via_pair
