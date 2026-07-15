"""Unit tests — :mod:`brutex.engine.sweep_diffcheck` (R45 differential spot-check).

Rule-by-rule hand fixtures for the naive reference simulator (entry /
same-session gate / session-gap boundary close / forced exit / SL-TP
touch + SL-first tie / AT_LEVEL fills / MFE-MAE / end-of-data), the
deterministic sampler pins, the tristate + fail-loud env resolution, the
AST import-ban pin, the
deliberately-BROKEN reference clone ⇒ SUSPECT teeth test, and the
never-adopt pin (design D-11 / I-4).
"""

from __future__ import annotations

import ast
import dataclasses
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
from brutex.calendars.nse import HolidayDataUnavailableError
from brutex.data.models import CandleBar
from brutex.engine import sweep_diffcheck
from brutex.engine.sweep_diffcheck import (
    CI_K_SAMPLES,
    COMPARED_TRADE_FIELDS,
    DIFFCHECK_DEFAULT_K_PER_RUN,
    DIFFCHECK_ENV,
    DIFFCHECK_K_ENV,
    MASKS_MISMATCHED_CAP,
    DiffCheckHook,
    DiffCheckMismatch,
    DiffCheckReport,
    ProductionSimulate,
    RefTrade,
    drawdown_bit_key,
    reference_drawdown,
    reference_simulate,
    resolve_diffcheck_k,
    sample_masks,
)
from brutex.execution.fill_mode import FillMode
from structlog.testing import capture_logs

#: D-6 traceability — test name → requirement/gap tokens.
TRACEABILITY: dict[str, tuple[str, ...]] = {
    "test_r45_entry_fills_next_bar_open": ("R45",),
    "test_r45_overnight_gap_entry_is_dropped": ("R45",),
    "test_r45_session_gap_closes_at_prior_session_last_bar": ("R45",),
    "test_r45_forced_exit_overrides_everything": ("R45",),
    "test_r45_sl_tp_touch_and_sl_first_tie": ("R45",),
    "test_r45_end_of_data_closes_at_last_close": ("R45",),
    "test_r45_sampler_same_identity_same_masks": ("R45",),
    "test_r45_sampler_different_snapshot_hash_different_masks": ("R45",),
    "test_r45_ast_import_ban": ("R45",),
    "test_r45_broken_reference_clone_marks_suspect": ("R45",),
    "test_r45_never_adopt_production_value_always_ships": ("R45",),
    "test_r45_resolve_diffcheck_k_tristate": ("R45",),
    "test_r45_drawdown_bit_key_parity_with_production_compare_key": ("R45",),
}

#: 09:15:00 IST session open on Tuesday 2024-01-02 (a regular NSE session).
_OPEN_UTC = datetime(2024, 1, 2, 3, 45, tzinfo=UTC)


def _rupees(paisa: int) -> Decimal:
    return Decimal(paisa) / Decimal(100)


def _bar(
    minute: int,
    open_p: int,
    high_p: int,
    low_p: int,
    close_p: int,
    *,
    anchor: datetime = _OPEN_UTC,
) -> CandleBar:
    return CandleBar(
        timestamp=anchor + timedelta(minutes=minute),
        open=_rupees(open_p),
        high=_rupees(high_p),
        low=_rupees(low_p),
        close=_rupees(close_p),
        volume=0,
        open_interest=None,
    )


def _flat_bars(n: int, price_p: int = 10_000, *, anchor: datetime = _OPEN_UTC) -> list[CandleBar]:
    return [_bar(m, price_p, price_p, price_p, price_p, anchor=anchor) for m in range(n)]


# ── Reference simulator: validation ────────────────────────────────────────


class TestReferenceSimulateValidation:
    def test_unknown_direction_rejected(self) -> None:
        with pytest.raises(ValueError, match="direction"):
            reference_simulate(
                _flat_bars(2),
                [True, False],
                direction="HOLD",
                tf_minutes=1,
                sl_paisa=None,
                tp_paisa=None,
            )

    def test_tf_minutes_below_one_rejected(self) -> None:
        with pytest.raises(ValueError, match="tf_minutes"):
            reference_simulate(
                _flat_bars(2),
                [True, False],
                direction="BUY",
                tf_minutes=0,
                sl_paisa=None,
                tp_paisa=None,
            )

    @pytest.mark.parametrize("bad", [0, -10])
    def test_non_positive_sl_rejected(self, bad: int) -> None:
        with pytest.raises(ValueError, match="sl_paisa"):
            reference_simulate(
                _flat_bars(2),
                [True, False],
                direction="BUY",
                tf_minutes=1,
                sl_paisa=bad,
                tp_paisa=None,
            )

    @pytest.mark.parametrize("bad", [0, -1])
    def test_non_positive_tp_rejected(self, bad: int) -> None:
        with pytest.raises(ValueError, match="tp_paisa"):
            reference_simulate(
                _flat_bars(2),
                [True, False],
                direction="SELL",
                tf_minutes=1,
                sl_paisa=None,
                tp_paisa=bad,
            )

    def test_length_mismatch_rejected(self) -> None:
        with pytest.raises(ValueError, match="entry_fires length 1 != bars length 2"):
            reference_simulate(
                _flat_bars(2),
                [True],
                direction="BUY",
                tf_minutes=1,
                sl_paisa=None,
                tp_paisa=None,
            )


# ── Reference simulator: rule fixtures ─────────────────────────────────────


class TestReferenceSimulateRules:
    def test_r45_entry_fills_next_bar_open(self) -> None:
        """Signal on bar 0 ⇒ enter at bar 1 OPEN; entry bar's range folds MFE/MAE."""
        bars = [
            _bar(0, 10_000, 10_005, 9_995, 10_000),
            _bar(1, 10_010, 10_030, 10_000, 10_020),
            _bar(2, 10_020, 10_050, 9_990, 10_040),
        ]
        trades = reference_simulate(
            bars,
            [True, False, False],
            direction="BUY",
            tf_minutes=1,
            sl_paisa=None,
            tp_paisa=None,
        )
        assert len(trades) == 1
        trade = trades[0]
        assert trade.entry_idx == 1
        assert trade.entry_paisa == 10_010
        assert trade.entry_ts == bars[1].timestamp
        assert trade.exit_idx == 2
        assert trade.exit_paisa == 10_040
        assert trade.exit_ts == bars[2].timestamp
        assert trade.pnl_paisa == 30
        assert trade.mfe_paisa == 40  # bar2 high 10_050 − entry 10_010
        assert trade.mae_paisa == 20  # bar2 low 9_990 vs entry 10_010
        assert trade.holding_bars == 2
        assert trade.exit_reason == "end_of_data"

    def test_r45_end_of_data_closes_at_last_close(self) -> None:
        bars = [
            _bar(0, 10_000, 10_000, 10_000, 10_000),
            _bar(1, 10_010, 10_012, 10_008, 10_011),
        ]
        trades = reference_simulate(
            bars, [True, False], direction="SELL", tf_minutes=1, sl_paisa=None, tp_paisa=None
        )
        assert len(trades) == 1
        trade = trades[0]
        assert trade.exit_reason == "end_of_data"
        assert trade.exit_paisa == 10_011
        assert trade.pnl_paisa == 10_010 - 10_011  # SELL: entry − exit == −1
        assert trade.holding_bars == 1
        assert trade.mfe_paisa == 2  # SELL fav = entry − low = 10_010 − 10_008
        assert trade.mae_paisa == 2  # SELL adv = high − entry = 10_012 − 10_010

    def test_no_signal_no_trades(self) -> None:
        trades = reference_simulate(
            _flat_bars(5),
            [False] * 5,
            direction="BUY",
            tf_minutes=1,
            sl_paisa=None,
            tp_paisa=None,
        )
        assert trades == []

    def test_signal_on_last_bar_never_fills(self) -> None:
        trades = reference_simulate(
            _flat_bars(3),
            [False, False, True],
            direction="BUY",
            tf_minutes=1,
            sl_paisa=None,
            tp_paisa=None,
        )
        assert trades == []

    def test_r45_sl_tp_touch_and_sl_first_tie(self) -> None:
        """SL/TP touch on [low, high]; a bar spanning BOTH fills the stop (pessimistic)."""
        bars = [
            _bar(0, 10_000, 10_000, 10_000, 10_000),
            _bar(1, 10_000, 10_060, 9_940, 10_000),  # entry bar spans BOTH levels
        ]
        trades = reference_simulate(
            bars, [True, False], direction="BUY", tf_minutes=1, sl_paisa=50, tp_paisa=50
        )
        assert len(trades) == 1
        trade = trades[0]
        assert trade.exit_reason == "stop_loss"  # SL_FIRST tie
        assert trade.exit_paisa == 9_950  # AT_LEVEL: exactly entry − 50
        assert trade.pnl_paisa == -50
        assert trade.exit_idx == 1  # the ENTRY bar itself is managed
        assert trade.holding_bars == 1

    def test_sl_touch_boundary_exact_level(self) -> None:
        """BUY stop fires iff low <= sl — a low exactly AT the level fills."""
        bars = [
            _bar(0, 10_000, 10_000, 10_000, 10_000),
            _bar(1, 10_000, 10_010, 9_950, 10_000),  # low == sl level exactly
        ]
        trades = reference_simulate(
            bars, [True, False], direction="BUY", tf_minutes=1, sl_paisa=50, tp_paisa=None
        )
        assert len(trades) == 1
        assert trades[0].exit_reason == "stop_loss"
        assert trades[0].pnl_paisa == -50

    def test_sl_one_paisa_above_level_does_not_fill(self) -> None:
        bars = [
            _bar(0, 10_000, 10_000, 10_000, 10_000),
            _bar(1, 10_000, 10_010, 9_951, 10_000),  # low = sl + 1: no touch
        ]
        trades = reference_simulate(
            bars, [True, False], direction="BUY", tf_minutes=1, sl_paisa=50, tp_paisa=None
        )
        assert len(trades) == 1
        assert trades[0].exit_reason == "end_of_data"

    def test_tp_touch_buy(self) -> None:
        bars = [
            _bar(0, 10_000, 10_000, 10_000, 10_000),
            _bar(1, 10_000, 10_030, 9_990, 10_010),
        ]
        trades = reference_simulate(
            bars, [True, False], direction="BUY", tf_minutes=1, sl_paisa=100, tp_paisa=30
        )
        assert len(trades) == 1
        trade = trades[0]
        assert trade.exit_reason == "take_profit"
        assert trade.exit_paisa == 10_030
        assert trade.pnl_paisa == 30

    def test_sell_mirror_levels(self) -> None:
        """SELL: stop above (high >= sl), target below (low <= tp)."""
        bars = [
            _bar(0, 10_000, 10_000, 10_000, 10_000),
            _bar(1, 10_000, 10_050, 9_995, 10_020),  # high == entry + 50 == sl level
        ]
        trades = reference_simulate(
            bars, [True, False], direction="SELL", tf_minutes=1, sl_paisa=50, tp_paisa=200
        )
        assert len(trades) == 1
        trade = trades[0]
        assert trade.exit_reason == "stop_loss"
        assert trade.exit_paisa == 10_050
        assert trade.pnl_paisa == -50  # SELL: entry − exit

    def test_sell_take_profit_below(self) -> None:
        bars = [
            _bar(0, 10_000, 10_000, 10_000, 10_000),
            _bar(1, 10_000, 10_005, 9_970, 9_980),  # low == entry − 30 == tp level
        ]
        trades = reference_simulate(
            bars, [True, False], direction="SELL", tf_minutes=1, sl_paisa=None, tp_paisa=30
        )
        assert len(trades) == 1
        assert trades[0].exit_reason == "take_profit"
        assert trades[0].pnl_paisa == 30

    def test_entry_ignored_while_open_and_on_exit_bar(self) -> None:
        """Single position: signals while open + on the exit bar are consumed."""
        bars = [
            _bar(0, 10_000, 10_000, 10_000, 10_000),  # arm
            _bar(1, 10_000, 10_010, 9_940, 10_000),  # entry + SL exit (same bar)
            _bar(2, 10_000, 10_000, 10_000, 10_000),  # exit-bar signal? no — bar1 was exit
            _bar(3, 10_000, 10_000, 10_000, 10_000),
        ]
        # Signals on EVERY bar: bar1's signal is ignored (a trade closed on it);
        # bar2 re-arms; bar3 fills; end-of-data closes at bar3.
        trades = reference_simulate(
            bars,
            [True, True, True, True],
            direction="BUY",
            tf_minutes=1,
            sl_paisa=50,
            tp_paisa=None,
        )
        assert [t.entry_idx for t in trades] == [1, 3]
        assert [t.exit_idx for t in trades] == [1, 3]
        assert trades[0].exit_reason == "stop_loss"
        assert trades[1].exit_reason == "end_of_data"

    def test_r45_forced_exit_overrides_everything(self) -> None:
        """The 15:20 IST bar force-closes at ITS OPEN, folding (open, open) only."""
        anchor = datetime(2024, 1, 2, 9, 47, tzinfo=UTC)  # 15:17 IST
        bars = [
            _bar(0, 10_000, 10_000, 10_000, 10_000, anchor=anchor),  # 15:17 arm
            _bar(1, 10_000, 10_010, 9_995, 10_005, anchor=anchor),  # 15:18 entry
            _bar(2, 10_005, 10_040, 9_980, 10_030, anchor=anchor),  # 15:19 range
            _bar(3, 10_035, 10_100, 9_900, 10_050, anchor=anchor),  # 15:20 FORCED
        ]
        trades = reference_simulate(
            bars,
            [True, False, False, False],
            direction="BUY",
            tf_minutes=1,
            sl_paisa=90,
            tp_paisa=90,  # bar3's wicks would touch both — forced wins
        )
        assert len(trades) == 1
        trade = trades[0]
        assert trade.exit_reason == "forced_1520"
        assert trade.exit_idx == 3
        assert trade.exit_paisa == 10_035  # the forced bar's OPEN
        assert trade.pnl_paisa == 35
        # Excursion: bar3 folds (open, open) ONLY — its 10_100/9_900 wicks never fold.
        assert trade.mfe_paisa == 40  # bar2 high 10_040 − entry 10_000
        assert trade.mae_paisa == 20  # bar2 low 9_980 vs entry 10_000

    def test_bar_opening_1519_is_not_forced_for_1m(self) -> None:
        """Strict boundary: a 1m bar closing exactly AT 15:20 is NOT forced."""
        anchor = datetime(2024, 1, 2, 9, 48, tzinfo=UTC)  # 15:18 IST
        bars = [
            _bar(0, 10_000, 10_000, 10_000, 10_000, anchor=anchor),  # 15:18 arm
            _bar(1, 10_010, 10_015, 10_005, 10_012, anchor=anchor),  # 15:19 entry
        ]
        trades = reference_simulate(
            bars, [True, False], direction="BUY", tf_minutes=1, sl_paisa=None, tp_paisa=None
        )
        assert len(trades) == 1
        assert trades[0].exit_reason == "end_of_data"  # filled + survived to EOD
        assert trades[0].entry_idx == 1

    def test_tf_straddle_forces_coarse_bar(self) -> None:
        """A 15m bar opening 15:15 IST runs to 15:30 — its window straddles 15:20."""
        anchor = datetime(2024, 1, 2, 9, 30, tzinfo=UTC)  # 15:00 IST
        bars = [
            _bar(0, 10_000, 10_000, 10_000, 10_000, anchor=anchor),  # 15:00 arm
            _bar(15, 10_005, 10_010, 10_000, 10_008, anchor=anchor),  # 15:15 entry?
        ]
        # The 15:15 fill bar IS a forced-exit bar for tf=15 → the entry is DROPPED.
        trades = reference_simulate(
            bars, [True, False], direction="BUY", tf_minutes=15, sl_paisa=None, tp_paisa=None
        )
        assert trades == []

    def test_entry_armed_on_last_usable_bar_is_dropped(self) -> None:
        anchor = datetime(2024, 1, 2, 9, 49, tzinfo=UTC)  # 15:19 IST
        bars = [
            _bar(0, 10_000, 10_000, 10_000, 10_000, anchor=anchor),  # 15:19 arm
            _bar(1, 10_010, 10_010, 10_010, 10_010, anchor=anchor),  # 15:20 forced
        ]
        trades = reference_simulate(
            bars, [True, False], direction="BUY", tf_minutes=1, sl_paisa=None, tp_paisa=None
        )
        assert trades == []

    def test_r45_overnight_gap_entry_is_dropped(self) -> None:
        """R2-OC-2: a fill bar on a LATER IST session date drops the entry."""
        day1 = datetime(2024, 1, 2, 9, 30, tzinfo=UTC)  # 15:00 IST Tue
        day2 = datetime(2024, 1, 3, 3, 45, tzinfo=UTC)  # 09:15 IST Wed
        bars = [
            _bar(0, 10_000, 10_000, 10_000, 10_000, anchor=day1),
            _bar(0, 10_100, 10_100, 10_100, 10_100, anchor=day2),
            _bar(1, 10_100, 10_100, 10_100, 10_100, anchor=day2),
        ]
        # Signal on day-1 bar: its fill bar is next day → DROPPED. The day-2
        # signal (bar index 1) fills on bar index 2 (same date).
        trades = reference_simulate(
            bars,
            [True, True, False],
            direction="BUY",
            tf_minutes=1,
            sl_paisa=None,
            tp_paisa=None,
        )
        assert len(trades) == 1
        assert trades[0].entry_idx == 2
        assert trades[0].entry_paisa == 10_100

    def test_r45_session_gap_closes_at_prior_session_last_bar(self) -> None:
        """(A2): an open position closes at the PRIOR session's last bar.

        The boundary bar is NEVER managed — its wild range would trip the
        SL (and fold huge excursions) if it were; the reference must close
        FIRST at the day-1 last bar's CLOSE/ts/idx under ``session_gap``
        (DEC-TRADESIM-OPEN-SESSION-GUARD-001, MIGRATION.md #1743 — the
        session-boundary law re-derived spec-side for the R45 reference).
        """
        day1 = datetime(2024, 1, 2, 9, 30, tzinfo=UTC)  # 15:00 IST Tue
        day2 = datetime(2024, 1, 3, 3, 45, tzinfo=UTC)  # 09:15 IST Wed
        bars = [
            _bar(0, 10_000, 10_000, 10_000, 10_000, anchor=day1),  # arm
            _bar(1, 10_010, 10_030, 10_005, 10_020, anchor=day1),  # entry + day-1 LAST
            _bar(0, 9_000, 12_000, 8_000, 9_500, anchor=day2),  # boundary (wild)
            _bar(1, 9_500, 9_500, 9_500, 9_500, anchor=day2),
        ]
        trades = reference_simulate(
            bars,
            [True, False, False, False],
            direction="BUY",
            tf_minutes=1,
            sl_paisa=50,  # would fill stop_loss at 9_960 IF the boundary bar were managed
            tp_paisa=None,
        )
        assert len(trades) == 1
        trade = trades[0]
        assert trade.exit_reason == "session_gap"
        assert trade.entry_idx == 1
        assert trade.exit_idx == 1  # the day-1 last stored bar, NOT the boundary bar
        assert trade.exit_ts == bars[1].timestamp
        assert trade.exit_paisa == 10_020  # day-1 last CLOSE — the finish() pricing
        assert trade.pnl_paisa == 10
        assert trade.holding_bars == 1
        # NO excursion fold from the boundary bar (day-1 evidence only).
        assert trade.mfe_paisa == 20  # bar1 high 10_030 − entry 10_010
        assert trade.mae_paisa == 5  # entry 10_010 − bar1 low 10_005

    def test_session_gap_boundary_bar_never_arms_next_bar_fills(self) -> None:
        """The boundary bar's own signal never arms; earliest re-arm i+1, fill i+2."""
        day1 = datetime(2024, 1, 2, 9, 30, tzinfo=UTC)  # 15:00 IST Tue
        day2 = datetime(2024, 1, 3, 3, 45, tzinfo=UTC)  # 09:15 IST Wed
        bars = [
            _bar(0, 10_000, 10_000, 10_000, 10_000, anchor=day1),  # arm
            _bar(1, 10_000, 10_000, 10_000, 10_000, anchor=day1),  # entry + day-1 LAST
            _bar(0, 10_100, 10_100, 10_100, 10_100, anchor=day2),  # boundary: fires, no arm
            _bar(1, 10_100, 10_100, 10_100, 10_100, anchor=day2),  # re-arm (i+1)
            _bar(2, 10_150, 10_150, 10_150, 10_150, anchor=day2),  # fill (i+2)
        ]
        trades = reference_simulate(
            bars,
            [True, False, True, True, False],
            direction="BUY",
            tf_minutes=1,
            sl_paisa=None,
            tp_paisa=None,
        )
        assert len(trades) == 2
        assert trades[0].exit_reason == "session_gap"
        assert trades[0].exit_idx == 1
        # The boundary bar (idx 2) armed nothing — the second trade fills at
        # idx 4 off the idx-3 arm, never at idx 3 off the boundary's signal.
        assert trades[1].entry_idx == 4
        assert trades[1].entry_paisa == 10_150
        assert trades[1].exit_reason == "end_of_data"

    def test_in_coverage_non_session_date_uses_default_boundary(self) -> None:
        """A Saturday (in-coverage, no session) falls back to the 15:20 default."""
        saturday = datetime(2024, 1, 6, 3, 45, tzinfo=UTC)  # 09:15 IST Sat
        bars = [
            _bar(0, 10_000, 10_000, 10_000, 10_000, anchor=saturday),
            _bar(1, 10_020, 10_025, 10_015, 10_022, anchor=saturday),
        ]
        trades = reference_simulate(
            bars, [True, False], direction="BUY", tf_minutes=1, sl_paisa=None, tp_paisa=None
        )
        assert len(trades) == 1
        assert trades[0].entry_idx == 1
        assert trades[0].exit_reason == "end_of_data"

    def test_out_of_coverage_weekday_raises_loud(self) -> None:
        far_future = datetime(2050, 1, 3, 3, 45, tzinfo=UTC)  # Monday 2050
        bars = [
            _bar(0, 10_000, 10_000, 10_000, 10_000, anchor=far_future),
            _bar(1, 10_000, 10_000, 10_000, 10_000, anchor=far_future),
        ]
        with pytest.raises(HolidayDataUnavailableError):
            reference_simulate(
                bars,
                [True, False],
                direction="BUY",
                tf_minutes=1,
                sl_paisa=None,
                tp_paisa=None,
            )

    def test_idle_feed_never_touches_the_calendar(self) -> None:
        """No signals ⇒ no session resolution ⇒ out-of-coverage dates are fine."""
        far_future = datetime(2050, 1, 3, 3, 45, tzinfo=UTC)
        bars = _flat_bars(3, anchor=far_future)
        assert (
            reference_simulate(
                bars, [False] * 3, direction="BUY", tf_minutes=1, sl_paisa=None, tp_paisa=None
            )
            == []
        )

    def test_paisa_snap_half_up(self) -> None:
        """The re-derived rupee→paisa snap rounds half AWAY from zero at 2dp."""
        bars = [
            _bar(0, 10_000, 10_000, 10_000, 10_000),
            CandleBar(
                timestamp=_OPEN_UTC + timedelta(minutes=1),
                open=Decimal("100.005"),  # → 100.01 → 10_001 paisa
                high=Decimal("100.10"),
                low=Decimal("99.90"),
                close=Decimal("100.00"),
                volume=0,
                open_interest=None,
            ),
        ]
        trades = reference_simulate(
            bars, [True, False], direction="BUY", tf_minutes=1, sl_paisa=None, tp_paisa=None
        )
        assert trades[0].entry_paisa == 10_001


# ── reference_drawdown ─────────────────────────────────────────────────────


def _ref_trade(exit_idx: int, pnl: int) -> RefTrade:
    ts = _OPEN_UTC + timedelta(minutes=exit_idx)
    return RefTrade(
        entry_idx=max(0, exit_idx - 1),
        exit_idx=exit_idx,
        entry_ts=_OPEN_UTC,
        exit_ts=ts,
        entry_paisa=10_000,
        exit_paisa=10_000 + pnl,
        pnl_paisa=pnl,
        mfe_paisa=max(pnl, 0),
        mae_paisa=max(-pnl, 0),
        holding_bars=2,
        exit_reason="end_of_data",
    )


class TestReferenceDrawdown:
    def test_hand_computed_exact(self) -> None:
        # start 1000; pnl +100 @1, −300 @3 ⇒ equity [1000,1100,1100,800,800]
        # peak 1100; max drop 300; pct 300/1100; below-peak run = bars 3..4.
        stats = reference_drawdown(
            [_ref_trade(1, 100), _ref_trade(3, -300)], n_bars=5, start_equity_paisa=1000
        )
        max_dd, max_pct, max_bars, ulcer = stats
        assert max_dd == 300
        assert max_pct == (300 / 1100) * 100.0
        assert max_bars == 2
        expected_ulcer = ((2 * (300 / 1100) ** 2) / 5) ** 0.5 * 100.0
        assert ulcer == pytest.approx(expected_ulcer, rel=1e-12)

    def test_flat_equity_is_all_zero(self) -> None:
        assert reference_drawdown([], n_bars=4, start_equity_paisa=1000) == (0, 0.0, 0, 0.0)

    def test_non_positive_peak_contributes_no_percentage(self) -> None:
        # start 0 → peak stays 0 for the whole scan (never > 0): the paisa
        # drop is REAL but no percentage/ulcer contribution is fabricated.
        stats = reference_drawdown([_ref_trade(1, -150)], n_bars=3, start_equity_paisa=0)
        assert stats == (150, 0.0, 2, 0.0)

    def test_percentage_unclamped_above_100(self) -> None:
        # start 100; −150 at bar1 ⇒ equity [100, −50]; drop 150 / peak 100.
        stats = reference_drawdown([_ref_trade(1, -150)], n_bars=2, start_equity_paisa=100)
        max_dd, max_pct, max_bars, _ulcer = stats
        assert max_dd == 150
        assert max_pct == 150.0  # unclamped > 100% — real, gate-relevant
        assert max_bars == 1

    def test_n_bars_zero_rejected(self) -> None:
        with pytest.raises(ValueError, match="n_bars"):
            reference_drawdown([], n_bars=0, start_equity_paisa=1000)

    def test_exit_idx_out_of_range_rejected(self) -> None:
        with pytest.raises(ValueError, match="exit_idx 5 out of range"):
            reference_drawdown([_ref_trade(5, 10)], n_bars=3, start_equity_paisa=1000)


class TestDrawdownBitKey:
    def test_r45_drawdown_bit_key_parity_with_production_compare_key(self) -> None:
        """The re-derived key equals the production burn-in key bit-for-bit."""
        from brutex.engine.rust_metrics import drawdown_compare_key

        samples = [
            (0, 0.0, 0, 0.0),
            (0, -0.0, 0, 0.0),  # signed zero must be distinguishable
            (300, (300 / 1100) * 100.0, 2, 1.23456789),
            (2**60, 99.999, 10**6, 0.1 + 0.2),
        ]
        for stats in samples:
            assert drawdown_bit_key(stats) == drawdown_compare_key(stats)

    def test_signed_zero_cannot_hide(self) -> None:
        assert drawdown_bit_key((0, 0.0, 0, 0.0)) != drawdown_bit_key((0, -0.0, 0, 0.0))
        assert (0, 0.0, 0, 0.0) == (0, -0.0, 0, 0.0)  # plain equality WOULD hide it

    def test_ints_compare_exactly_not_packed(self) -> None:
        huge = 2**200  # would overflow a struct pack — ints stay raw
        key = drawdown_bit_key((huge, 1.0, 3, 2.0))
        assert key[0] == huge
        assert key[2] == 3


# ── Sampler (D-11) ─────────────────────────────────────────────────────────


class TestSampleMasks:
    _MASKS = tuple(range(100, 120))

    def test_r45_sampler_same_identity_same_masks(self) -> None:
        kwargs = {
            "sweep_id": "sweep-A",
            "tuple_digest": "dig-1",
            "data_snapshot_hash": "snap-X",
        }
        first = sample_masks(masks=self._MASKS, k=5, **kwargs)
        second = sample_masks(masks=self._MASKS, k=5, **kwargs)
        # Frozen literal pin — mutation-hardened against any seed-material drift.
        assert first == (100, 101, 108, 112, 118)
        assert second == first

    def test_r45_sampler_different_snapshot_hash_different_masks(self) -> None:
        picked = sample_masks(
            sweep_id="sweep-A",
            tuple_digest="dig-1",
            data_snapshot_hash="snap-Y",
            masks=self._MASKS,
            k=5,
        )
        assert picked == (101, 103, 105, 113, 115)
        assert picked != (100, 101, 108, 112, 118)

    def test_output_preserves_canonical_order(self) -> None:
        picked = sample_masks(
            sweep_id="s", tuple_digest="d", data_snapshot_hash="h", masks=self._MASKS, k=7
        )
        assert list(picked) == sorted(picked)

    def test_k_larger_than_masks_returns_all(self) -> None:
        picked = sample_masks(
            sweep_id="s", tuple_digest="d", data_snapshot_hash="h", masks=(7, 8, 9), k=100
        )
        assert picked == (7, 8, 9)

    def test_k_zero_and_empty_masks(self) -> None:
        assert (
            sample_masks(sweep_id="s", tuple_digest="d", data_snapshot_hash="h", masks=(1,), k=0)
            == ()
        )
        assert (
            sample_masks(sweep_id="s", tuple_digest="d", data_snapshot_hash="h", masks=(), k=5)
            == ()
        )

    def test_negative_k_rejected(self) -> None:
        with pytest.raises(ValueError, match="k must be >= 0"):
            sample_masks(sweep_id="s", tuple_digest="d", data_snapshot_hash="h", masks=(1,), k=-1)


# ── Env resolution ─────────────────────────────────────────────────────────


class TestResolveDiffcheckK:
    def test_r45_resolve_diffcheck_k_tristate(self) -> None:
        assert resolve_diffcheck_k({}) == DIFFCHECK_DEFAULT_K_PER_RUN == 200
        for on in ("1", "true", "yes", "on", " ON "):
            assert resolve_diffcheck_k({DIFFCHECK_ENV: on}) == 200
        for off in ("0", "false", "no", "off", " OFF "):
            assert resolve_diffcheck_k({DIFFCHECK_ENV: off}) == 0

    def test_junk_flag_fails_loud(self) -> None:
        with pytest.raises(ValueError, match="BRUTEX_SWEEP_DIFFCHECK='flase'"):
            resolve_diffcheck_k({DIFFCHECK_ENV: "flase"})

    def test_whitespace_flag_is_unset(self) -> None:
        assert resolve_diffcheck_k({DIFFCHECK_ENV: "   "}) == 200

    def test_k_override_applies(self) -> None:
        assert resolve_diffcheck_k({DIFFCHECK_K_ENV: "17"}) == 17
        assert resolve_diffcheck_k({DIFFCHECK_K_ENV: " 8 "}) == CI_K_SAMPLES == 8
        assert resolve_diffcheck_k({DIFFCHECK_K_ENV: "0"}) == 0

    def test_k_junk_fails_loud(self) -> None:
        with pytest.raises(ValueError, match="BRUTEX_SWEEP_DIFFCHECK_K='many'"):
            resolve_diffcheck_k({DIFFCHECK_K_ENV: "many"})

    def test_k_negative_fails_loud(self) -> None:
        with pytest.raises(ValueError, match="BRUTEX_SWEEP_DIFFCHECK_K='-3'"):
            resolve_diffcheck_k({DIFFCHECK_K_ENV: "-3"})

    def test_off_flag_wins_over_k(self) -> None:
        assert resolve_diffcheck_k({DIFFCHECK_ENV: "0", DIFFCHECK_K_ENV: "50"}) == 0

    def test_k_whitespace_is_unset(self) -> None:
        assert resolve_diffcheck_k({DIFFCHECK_K_ENV: "  "}) == 200

    def test_env_none_reads_os_environ(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(DIFFCHECK_ENV, raising=False)
        monkeypatch.setenv(DIFFCHECK_K_ENV, "9")
        assert resolve_diffcheck_k() == 9


# ── AST import ban (D-11 independence teeth) ───────────────────────────────


class TestAstImportBan:
    def test_r45_ast_import_ban(self) -> None:
        """The reference module imports NONE of the banned production sim set."""
        banned = {
            "brutex.engine.trade_sim",
            "brutex.engine.rust_trade_sim",
            "brutex.engine.sweep_driver",
            "brutex.engine.rust_metrics",
            "brutex.metrics.compute",
            "brutex.engine.exit_index",
        }
        module_file = sweep_diffcheck.__file__
        assert module_file is not None
        tree = ast.parse(Path(module_file).read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported.add(node.module)
        offenders = {
            mod for mod in imported if any(mod == b or mod.startswith(b + ".") for b in banned)
        }
        assert offenders == set()
        # Positive control: the parse genuinely saw the module's real imports.
        assert "brutex.calendars.nse" in imported
        assert "blake3" in imported


# ── DiffCheckHook ──────────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class _TF:
    minutes: int = 1


@dataclasses.dataclass(frozen=True)
class _Dir:
    value: str = "BUY"


@dataclasses.dataclass(frozen=True)
class _Tuple:
    k: int = 2
    sl_paisa: int | None = None
    tp_paisa: int | None = None
    tf: _TF = dataclasses.field(default_factory=_TF)
    direction: _Dir = dataclasses.field(default_factory=_Dir)


@dataclasses.dataclass(frozen=True)
class _Grid:
    condition_bits: tuple[int, ...] = (0, 1, 2)
    data_snapshot_hash: str = "snap"
    start_equity_paisa: int = 10_000_000
    # DEC-FILL-WORST-CASE-001: the hook threads grid.fill_mode into the
    # reference (same-mode vs same-mode); PRECISE keeps the legacy fixtures'
    # hand-derived numbers byte-identical.
    fill_mode: FillMode = FillMode.PRECISE


class _SuspectStub:
    """Local SUSPECT-latch stand-in (no WP-1 import — design WP-2 note)."""

    def __init__(self) -> None:
        self.suspect = False
        self.mismatches: list[DiffCheckMismatch] = []

    def __call__(self, mismatch: DiffCheckMismatch) -> None:
        self.suspect = True
        self.mismatches.append(mismatch)


def _matching_production_simulate(
    bars: Sequence[CandleBar],
    entry_signals: Sequence[bool],
    *,
    direction: str,
    tf_minutes: int,
    stop_loss_paisa: int | None,
    take_profit_paisa: int | None,
) -> Sequence[RefTrade]:
    """A production stub that agrees with the reference by construction."""
    return reference_simulate(
        bars,
        entry_signals,
        direction=direction,
        tf_minutes=tf_minutes,
        sl_paisa=stop_loss_paisa,
        tp_paisa=take_profit_paisa,
    )


def _matching_production_drawdown(
    bars: Sequence[CandleBar],
    trades: Sequence[object],
    *,
    start_equity_paisa: int,
    tf_minutes: int,
) -> tuple[int, float, int, float]:
    return reference_drawdown(
        cast("Sequence[RefTrade]", trades),
        n_bars=len(bars),
        start_equity_paisa=start_equity_paisa,
    )


def _hook(sink: _SuspectStub, **overrides: object) -> DiffCheckHook:
    kwargs: dict[str, object] = {
        "sweep_id": "sweep-A",
        "tuple_digest_of": lambda tuple_, grid: "dig-1",
        "production_simulate": _matching_production_simulate,
        "production_drawdown": _matching_production_drawdown,
        "on_mismatch": sink,
    }
    kwargs.update(overrides)
    return DiffCheckHook(**kwargs)  # type: ignore[arg-type]


def _bars_and_bits(n: int = 6) -> tuple[list[CandleBar], list[int]]:
    prices = [10_000, 10_010, 10_020, 10_005, 10_015, 10_030]
    bars = [_bar(m, p, p + 5, p - 5, p) for m, p in enumerate(prices[:n])]
    bar_bits = [0b111 if m % 2 == 0 else 0b000 for m in range(n)]
    return bars, bar_bits


class TestDiffCheckHook:
    def test_empty_sweep_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="sweep_id"):
            _hook(_SuspectStub(), sweep_id="")

    def test_budget_zero_returns_empty_report(self) -> None:
        bars, bits = _bars_and_bits()
        report = _hook(_SuspectStub()).run_for_tuple(
            bars=bars, bar_bits=bits, tuple_=_Tuple(), grid=_Grid(), budget_left=0
        )
        assert report == DiffCheckReport(checked=0, mismatches=0, masks_mismatched=())

    def test_matching_engine_reports_clean(self) -> None:
        bars, bits = _bars_and_bits()
        sink = _SuspectStub()
        report = _hook(sink).run_for_tuple(
            bars=bars, bar_bits=bits, tuple_=_Tuple(), grid=_Grid(), budget_left=10
        )
        # bits (0,1,2) at k=2: {0,1} is a CONFLICT pair (pruned) → 2 masks left.
        assert report == DiffCheckReport(checked=2, mismatches=0, masks_mismatched=())
        assert sink.suspect is False

    def test_budget_bounds_checked_exactly(self) -> None:
        bars, bits = _bars_and_bits()
        report = _hook(_SuspectStub()).run_for_tuple(
            bars=bars, bar_bits=bits, tuple_=_Tuple(), grid=_Grid(), budget_left=1
        )
        assert report.checked == 1

    def test_r45_broken_reference_clone_marks_suspect(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Teeth: a deliberately-BROKEN reference clone ⇒ mismatch ⇒ SUSPECT."""
        real = reference_simulate

        def broken_clone(
            bars: Sequence[CandleBar],
            entry_fires: Sequence[bool],
            *,
            direction: str,
            tf_minutes: int,
            sl_paisa: int | None,
            tp_paisa: int | None,
            fill_mode: FillMode = FillMode.PRECISE,
        ) -> list[RefTrade]:
            trades = real(
                bars,
                entry_fires,
                direction=direction,
                tf_minutes=tf_minutes,
                sl_paisa=sl_paisa,
                tp_paisa=tp_paisa,
                fill_mode=fill_mode,
            )
            # The plant: one paisa of drift on the first trade.
            return [
                dataclasses.replace(t, pnl_paisa=t.pnl_paisa + 1) if j == 0 else t
                for j, t in enumerate(trades)
            ]

        monkeypatch.setattr(sweep_diffcheck, "reference_simulate", broken_clone)
        bars, bits = _bars_and_bits()
        sink = _SuspectStub()
        with capture_logs() as logs:
            report = _hook(sink).run_for_tuple(
                bars=bars, bar_bits=bits, tuple_=_Tuple(), grid=_Grid(), budget_left=10
            )
        assert report.checked == 2
        assert report.mismatches == 2
        assert len(report.masks_mismatched) == 2
        assert sink.suspect is True  # ⇒ SUSPECT, end-to-end through the injected latch
        first = sink.mismatches[0]
        assert first.field == "trade[0].pnl_paisa"
        assert first.tuple_digest == "dig-1"
        assert len(first.mask_hex) == 32
        errors = [e for e in logs if e["event"] == "sweep_diffcheck.mismatch"]
        assert len(errors) == 2
        assert errors[0]["log_level"] == "error"
        assert errors[0]["field"] == "trade[0].pnl_paisa"

    def test_r45_never_adopt_production_value_always_ships(self) -> None:
        """I-4: the hook only OBSERVES — production trades/inputs are untouched."""
        bars, bits = _bars_and_bits()
        produced: list[Sequence[RefTrade]] = []

        def counting_production(
            b: Sequence[CandleBar],
            s: Sequence[bool],
            *,
            direction: str,
            tf_minutes: int,
            stop_loss_paisa: int | None,
            take_profit_paisa: int | None,
        ) -> Sequence[RefTrade]:
            trades = _matching_production_simulate(
                b,
                s,
                direction=direction,
                tf_minutes=tf_minutes,
                stop_loss_paisa=stop_loss_paisa,
                take_profit_paisa=take_profit_paisa,
            )
            produced.append(trades)
            return trades

        bits_before = list(bits)
        sink = _SuspectStub()
        hook = _hook(sink, production_simulate=counting_production)
        report = hook.run_for_tuple(
            bars=bars, bar_bits=bits, tuple_=_Tuple(), grid=_Grid(), budget_left=10
        )
        assert report.mismatches == 0
        assert bits == bits_before  # inputs never mutated
        # The production trade objects are returned by the stub and never
        # replaced/edited: every recorded trade is still field-identical to a
        # fresh reference run (nothing was "adopted"/rewritten in place).
        fresh = _matching_production_simulate(
            bars,
            [(b & 0b101) == 0b101 for b in bits],
            direction="BUY",
            tf_minutes=1,
            stop_loss_paisa=None,
            take_profit_paisa=None,
        )
        assert produced[0] == fresh

    def test_n_trades_mismatch_field(self) -> None:
        bars, bits = _bars_and_bits()
        sink = _SuspectStub()
        hook = _hook(
            sink,
            production_simulate=lambda *a, **k: [],  # production "loses" every trade
        )
        report = hook.run_for_tuple(
            bars=bars, bar_bits=bits, tuple_=_Tuple(), grid=_Grid(), budget_left=10
        )
        assert report.mismatches == 2
        assert sink.mismatches[0].field == "n_trades"
        assert sink.mismatches[0].prod == "0"
        assert sink.mismatches[0].ref == "1"

    def test_drawdown_mismatch_field(self) -> None:
        bars, bits = _bars_and_bits()
        sink = _SuspectStub()
        hook = _hook(
            sink,
            production_drawdown=lambda b, t, *, start_equity_paisa, tf_minutes: (
                0,
                -0.0,  # signed zero — plain float equality would hide it
                0,
                0.0,
            ),
        )
        report = hook.run_for_tuple(
            bars=bars, bar_bits=bits, tuple_=_Tuple(), grid=_Grid(), budget_left=10
        )
        assert report.mismatches == 2
        assert sink.mismatches[0].field == "drawdown"

    def test_masks_mismatched_capped_at_8(self) -> None:
        bars = _flat_bars(4)
        bar_bits = [(1 << 60) - 1] * 4  # every mask fires on every bar
        sink = _SuspectStub()
        hook = _hook(sink, production_simulate=lambda *a, **k: [])
        grid = _Grid(condition_bits=(2, 3, 4, 5, 6, 7, 8))  # C(7,2)=21 masks
        report = hook.run_for_tuple(
            bars=bars, bar_bits=bar_bits, tuple_=_Tuple(), grid=grid, budget_left=12
        )
        assert report.checked == 12
        assert report.mismatches == 12
        assert len(report.masks_mismatched) == MASKS_MISMATCHED_CAP == 8

    def test_on_mismatch_sink_failure_is_swallowed(self) -> None:
        bars, bits = _bars_and_bits()

        def dead_sink(mismatch: DiffCheckMismatch) -> None:
            raise RuntimeError("sink down")

        hook = _hook(
            cast("_SuspectStub", dead_sink),
            production_simulate=lambda *a, **k: [],
        )
        with capture_logs() as logs:
            report = hook.run_for_tuple(
                bars=bars, bar_bits=bits, tuple_=_Tuple(), grid=_Grid(), budget_left=10
            )
        assert report.mismatches == 2  # counting survived the dead sink
        warns = [e for e in logs if e["event"] == "sweep_diffcheck.on_mismatch_failed"]
        assert len(warns) == 2
        assert warns[0]["error_type"] == "RuntimeError"

    def test_fail_soft_production_leg_error(self) -> None:
        """A raising production leg degrades to a WARN — never into the sweep."""
        bars, bits = _bars_and_bits()

        def exploding(*a: object, **k: object) -> Sequence[RefTrade]:
            raise RuntimeError("kernel down")

        sink = _SuspectStub()
        hook = _hook(sink, production_simulate=exploding)
        with capture_logs() as logs:
            report = hook.run_for_tuple(
                bars=bars, bar_bits=bits, tuple_=_Tuple(), grid=_Grid(), budget_left=10
            )
        assert report == DiffCheckReport(checked=0, mismatches=0, masks_mismatched=())
        warns = [e for e in logs if e["event"] == "sweep_diffcheck.error"]
        assert len(warns) == 1
        assert warns[0]["error_type"] == "RuntimeError"
        assert sink.suspect is False

    def test_conflicted_masks_are_pruned_from_the_sample_pool(self) -> None:
        """bits {0,1} conflict: at k=2 over (0,1,2) only {0,2} and {1,2} remain."""
        bars, bits = _bars_and_bits()
        seen: list[Sequence[bool]] = []

        def spying_production(
            b: Sequence[CandleBar], s: Sequence[bool], **kw: object
        ) -> Sequence[RefTrade]:
            seen.append(list(s))
            return _matching_production_simulate(
                b,
                s,
                direction="BUY",
                tf_minutes=1,
                stop_loss_paisa=None,
                take_profit_paisa=None,
            )

        hook = _hook(_SuspectStub(), production_simulate=spying_production)
        report = hook.run_for_tuple(
            bars=bars, bar_bits=bits, tuple_=_Tuple(), grid=_Grid(), budget_left=100
        )
        assert report.checked == 2  # C(3,2)=3 minus the (0,1) conflict
        # Projection re-derived inline: masks 0b101 and 0b110 over the bits.
        expected = [[(b & 0b101) == 0b101 for b in bits], [(b & 0b110) == 0b110 for b in bits]]
        assert seen == expected

    def test_mismatch_payload_shape(self) -> None:
        mismatch = DiffCheckMismatch(
            tuple_digest="d", mask_hex="0" * 32, field="drawdown", prod="p", ref="r"
        )
        assert mismatch.payload() == {
            "tuple_digest": "d",
            "mask_hex": "0" * 32,
            "field": "drawdown",
            "prod": "p",
            "ref": "r",
        }

    def test_compared_fields_are_int_datetime_only(self) -> None:
        assert COMPARED_TRADE_FIELDS == (
            "pnl_paisa",
            "mfe_paisa",
            "mae_paisa",
            "holding_bars",
            "entry_ts",
            "exit_ts",
        )
        sample = _ref_trade(1, 10)
        for field in COMPARED_TRADE_FIELDS:
            assert isinstance(getattr(sample, field), int | datetime)


# ── executed-population injection (DEC-SWEEP-APRIORI-DEFAULT-001, round 3) ──


class TestExecutedPopulation:
    """``mask_population=`` — the R45 hook samples the EXECUTED work.

    Round-3 BLOCKER (MIGRATION.md #1797): under the Apriori extinction
    default the raw C(n, k) walk is UNBOUNDED (the ``effective_max_combos``
    auto-raise), so the hook must sample the tuple's injected candidate
    list — never re-materialise the raw enumeration — and honestly SKIP an
    extinct tuple (zero executed masks) with one counted INFO.
    """

    @staticmethod
    def _spying_production(seen: list[list[bool]]) -> ProductionSimulate:
        def spy(b: Sequence[CandleBar], s: Sequence[bool], **kw: object) -> Sequence[RefTrade]:
            seen.append(list(s))
            return _matching_production_simulate(
                b,
                s,
                direction="BUY",
                tf_minutes=1,
                stop_loss_paisa=None,
                take_profit_paisa=None,
            )

        return cast("ProductionSimulate", spy)

    def test_r45_population_is_the_sample_pool_not_the_raw_walk(self) -> None:
        """An injected 1-mask population checks exactly that mask — the raw
        k=2 enumeration over bits (0, 1, 2) would offer 2 conflict-free."""
        bars, bits = _bars_and_bits()
        seen: list[list[bool]] = []
        hook = _hook(_SuspectStub(), production_simulate=self._spying_production(seen))
        report = hook.run_for_tuple(
            bars=bars,
            bar_bits=bits,
            tuple_=_Tuple(),
            grid=_Grid(),
            budget_left=100,
            mask_population=[0b101],
        )
        assert report == DiffCheckReport(checked=1, mismatches=0, masks_mismatched=())
        assert report.skipped_empty is False
        assert seen == [[(b & 0b101) == 0b101 for b in bits]]

    def test_population_conflicts_pruned_exactly_like_production(self) -> None:
        """The injected population rides the SAME ``has_conflict`` prune the
        production enumeration loop applies — {0,1} (0b011) never samples."""
        bars, bits = _bars_and_bits()
        seen: list[list[bool]] = []
        hook = _hook(_SuspectStub(), production_simulate=self._spying_production(seen))
        report = hook.run_for_tuple(
            bars=bars,
            bar_bits=bits,
            tuple_=_Tuple(),
            grid=_Grid(),
            budget_left=100,
            mask_population=[0b011, 0b110],
        )
        assert report.checked == 1
        assert seen == [[(b & 0b110) == 0b110 for b in bits]]

    def test_r45_extinct_population_is_a_counted_skip_never_silent(self) -> None:
        """Zero executed masks ⇒ ``skipped_empty`` + ONE INFO — no raw walk,
        no budget spent, no SUSPECT."""
        bars, bits = _bars_and_bits()
        sink = _SuspectStub()
        with capture_logs() as logs:
            report = _hook(sink).run_for_tuple(
                bars=bars,
                bar_bits=bits,
                tuple_=_Tuple(),
                grid=_Grid(),
                budget_left=100,
                mask_population=[],
            )
        assert report == DiffCheckReport(
            checked=0, mismatches=0, masks_mismatched=(), skipped_empty=True
        )
        assert sink.suspect is False
        skips = [e for e in logs if e["event"] == "sweep_diffcheck.apriori_empty_skip"]
        assert len(skips) == 1
        assert skips[0]["log_level"] == "info"
        assert skips[0]["tuple_digest"] == "dig-1"
        assert skips[0]["k"] == 2
        assert skips[0]["population"] == 0

    def test_all_conflicted_population_skips_with_the_raw_population_count(self) -> None:
        bars, bits = _bars_and_bits()
        with capture_logs() as logs:
            report = _hook(_SuspectStub()).run_for_tuple(
                bars=bars,
                bar_bits=bits,
                tuple_=_Tuple(),
                grid=_Grid(),
                budget_left=100,
                mask_population=[0b011],  # bits {0, 1} conflict — zero survive
            )
        assert report.skipped_empty is True
        assert report.checked == 0
        skips = [e for e in logs if e["event"] == "sweep_diffcheck.apriori_empty_skip"]
        assert len(skips) == 1
        assert skips[0]["population"] == 1  # the pre-prune injected count, honestly

    def test_r45_population_mismatch_still_marks_suspect(self) -> None:
        """The never-adopt law is population-source-independent: a diverging
        production leg over the INJECTED population latches SUSPECT."""
        bars, bits = _bars_and_bits()
        sink = _SuspectStub()
        hook = _hook(sink, production_simulate=lambda *a, **k: [])
        report = hook.run_for_tuple(
            bars=bars,
            bar_bits=bits,
            tuple_=_Tuple(),
            grid=_Grid(),
            budget_left=100,
            mask_population=[0b101],
        )
        assert report.checked == 1
        assert report.mismatches == 1
        assert sink.suspect is True
        assert sink.mismatches[0].mask_hex == f"{0b101:032x}"

    def test_population_sampling_is_deterministic_same_identity_same_picks(self) -> None:
        """D-11 over an injected population: two runs under the SAME identity
        sample the SAME masks (min(K, len(population)) picks) — and every
        pick comes from the population."""
        bars, bits = _bars_and_bits()
        population = [0b101, 0b110, 0b100, 0b010, 0b001]

        def _picks() -> list[str]:
            sink = _SuspectStub()
            hook = _hook(sink, production_simulate=lambda *a, **k: [])
            report = hook.run_for_tuple(
                bars=bars,
                bar_bits=bits,
                tuple_=_Tuple(),
                grid=_Grid(),
                budget_left=2,
                mask_population=list(population),
            )
            assert report.checked == 2
            return [m.mask_hex for m in sink.mismatches]

        first, second = _picks(), _picks()
        assert first == second
        assert set(first) <= {f"{m:032x}" for m in population}

    def test_none_population_is_the_legacy_raw_enumeration(self) -> None:
        """Explicit ``mask_population=None`` ≡ the pre-round-3 default call."""
        bars, bits = _bars_and_bits()
        report = _hook(_SuspectStub()).run_for_tuple(
            bars=bars,
            bar_bits=bits,
            tuple_=_Tuple(),
            grid=_Grid(),
            budget_left=10,
            mask_population=None,
        )
        assert report == DiffCheckReport(checked=2, mismatches=0, masks_mismatched=())
        assert report.skipped_empty is False


# ── DEC-FILL-WORST-CASE-001 — the independently re-stated worst-case arm ────


class TestReferenceSimulateWorstCase:
    """Rule-by-rule hand fixtures for the worst-case (adverse-max) fill arm."""

    def test_worst_case_entry_fills_bar_extreme(self) -> None:
        # BUY entry fills at the entry bar's HIGH (not its open); SELL at LOW.
        bars = [
            _bar(0, 10_000, 10_000, 10_000, 10_000),
            _bar(1, 10_010, 10_030, 10_000, 10_020),
            _bar(2, 10_020, 10_050, 9_990, 10_040),
        ]
        for direction, expected_entry in (("BUY", 10_030), ("SELL", 10_000)):
            trades = reference_simulate(
                bars,
                [True, False, False],
                direction=direction,
                tf_minutes=1,
                sl_paisa=None,
                tp_paisa=None,
                fill_mode=FillMode.WORST_CASE,
            )
            assert len(trades) == 1
            assert trades[0].entry_paisa == expected_entry, direction

    def test_worst_case_sl_gap_through_fills_at_extreme(self) -> None:
        # The bar gaps THROUGH the stop: the fill is min(level, low) — the
        # bar LOW, not the level fantasy (BUY arm).
        bars = [
            _bar(0, 10_000, 10_000, 10_000, 10_000),
            _bar(1, 10_000, 10_000, 10_000, 10_000),  # flat entry: worst == open
            _bar(2, 9_990, 9_990, 9_900, 9_950),  # gaps far through sl = 9_950
        ]
        trades = reference_simulate(
            bars,
            [True, False, False],
            direction="BUY",
            tf_minutes=1,
            sl_paisa=50,
            tp_paisa=None,
            fill_mode=FillMode.WORST_CASE,
        )
        assert len(trades) == 1
        assert trades[0].exit_reason == "stop_loss"
        assert trades[0].exit_paisa == 9_900  # min(9_950, 9_900) = the LOW
        assert trades[0].pnl_paisa == -100

    def test_worst_case_tp_gap_past_clamps_at_level(self) -> None:
        # THE OVERSTATEMENT COUNTEREXAMPLE: a long-TP bar entirely ABOVE the
        # target (low > tp) must fill AT the level — min(tp, low) == tp — a
        # naive sell-at-low would fill BETTER than today's at-level fill.
        bars = [
            _bar(0, 10_000, 10_000, 10_000, 10_000),
            _bar(1, 10_000, 10_000, 10_000, 10_000),  # flat entry
            _bar(2, 10_100, 10_120, 10_090, 10_110),  # entirely above tp = 10_050
        ]
        trades = reference_simulate(
            bars,
            [True, False, False],
            direction="BUY",
            tf_minutes=1,
            sl_paisa=None,
            tp_paisa=50,
            fill_mode=FillMode.WORST_CASE,
        )
        assert len(trades) == 1
        assert trades[0].exit_reason == "take_profit"
        assert trades[0].exit_paisa == 10_050  # clamped AT the level
        assert trades[0].pnl_paisa == 50

    def test_worst_case_sell_side_sl_fills_max_level_high(self) -> None:
        # SELL exit (cover) mirror: max(level, high) — a gap through the SELL
        # stop fills at the bar HIGH.
        bars = [
            _bar(0, 10_000, 10_000, 10_000, 10_000),
            _bar(1, 10_000, 10_000, 10_000, 10_000),  # flat entry (SELL at low=open)
            _bar(2, 10_060, 10_150, 10_060, 10_100),  # gaps through sl = 10_050
        ]
        trades = reference_simulate(
            bars,
            [True, False, False],
            direction="SELL",
            tf_minutes=1,
            sl_paisa=50,
            tp_paisa=None,
            fill_mode=FillMode.WORST_CASE,
        )
        assert len(trades) == 1
        assert trades[0].exit_reason == "stop_loss"
        assert trades[0].exit_paisa == 10_150  # max(10_050, high 10_150)
        assert trades[0].pnl_paisa == 10_000 - 10_150

    def test_worst_case_forced_exit_fills_adverse_extreme_fold_unchanged(self) -> None:
        # The forced bar fills at its adverse extreme (BUY sells the LOW) but
        # still folds (open, open) only — the excursion law is mode-invariant.
        anchor = datetime(2024, 1, 2, 9, 47, tzinfo=UTC)  # 15:17 IST
        bars = [
            _bar(0, 10_000, 10_000, 10_000, 10_000, anchor=anchor),  # arm
            _bar(1, 10_000, 10_000, 10_000, 10_000, anchor=anchor),  # entry (flat)
            _bar(2, 10_010, 10_040, 9_980, 10_020, anchor=anchor),  # in-position
            _bar(3, 10_030, 10_090, 9_900, 10_050, anchor=anchor),  # 15:20 forced
        ]
        trades = reference_simulate(
            bars,
            [True, False, False, False],
            direction="BUY",
            tf_minutes=1,
            sl_paisa=None,
            tp_paisa=None,
            fill_mode=FillMode.WORST_CASE,
        )
        assert len(trades) == 1
        trade = trades[0]
        assert trade.exit_reason == "forced_1520"
        assert trade.exit_paisa == 9_900  # the forced bar's LOW, not its open
        # Fold law unchanged: bar 2's full range + bar 3's (open, open) only.
        assert trade.mfe_paisa == 40  # bar2 high 10_040 − entry 10_000
        assert trade.mae_paisa == 20  # entry 10_000 − bar2 low 9_980 (NOT 9_900)

    def test_worst_case_end_of_data_closes_at_last_bar_extreme(self) -> None:
        bars = [
            _bar(0, 10_000, 10_000, 10_000, 10_000),
            _bar(1, 10_010, 10_040, 9_995, 10_020),
        ]
        trades = reference_simulate(
            bars,
            [True, False],
            direction="BUY",
            tf_minutes=1,
            sl_paisa=None,
            tp_paisa=None,
            fill_mode=FillMode.WORST_CASE,
        )
        assert len(trades) == 1
        assert trades[0].exit_reason == "end_of_data"
        assert trades[0].exit_paisa == 9_995  # last bar LOW (PRECISE: close 10_020)
        # Worst entry = bar 1 HIGH 10_040 ⇒ pnl = 9_995 − 10_040.
        assert trades[0].pnl_paisa == -45

    def test_worst_case_session_gap_closes_at_prior_last_bar_extreme(self) -> None:
        day1 = datetime(2024, 1, 2, 9, 30, tzinfo=UTC)  # 15:00 IST Tue
        day2 = datetime(2024, 1, 3, 3, 45, tzinfo=UTC)  # 09:15 IST Wed
        bars = [
            _bar(0, 10_000, 10_000, 10_000, 10_000, anchor=day1),  # arm
            _bar(1, 10_010, 10_030, 10_005, 10_020, anchor=day1),  # entry + day-1 LAST
            _bar(0, 9_000, 12_000, 8_000, 9_500, anchor=day2),  # boundary (wild)
        ]
        trades = reference_simulate(
            bars,
            [True, False, False],
            direction="BUY",
            tf_minutes=1,
            sl_paisa=None,
            tp_paisa=None,
            fill_mode=FillMode.WORST_CASE,
        )
        assert len(trades) == 1
        trade = trades[0]
        assert trade.exit_reason == "session_gap"
        assert trade.exit_idx == 1
        assert trade.exit_paisa == 10_005  # day-1 last bar LOW (PRECISE: 10_020)
        assert trade.entry_paisa == 10_030  # worst entry = day-1 bar HIGH

    def test_worst_case_levels_anchor_on_the_worst_entry_fill(self) -> None:
        # The documented honest interaction: SL/TP levels anchor on the
        # ACTUAL (adverse-extreme) entry fill, so trigger timing can differ
        # between modes — here the SAME bar trips the stop ONLY under
        # worst-case (level 10_030 − 50 = 9_980 vs precise 10_000 − 50 =
        # 9_950; bar 2's low 9_975 touches only the former).
        bars = [
            _bar(0, 10_000, 10_000, 10_000, 10_000),
            _bar(1, 10_000, 10_030, 10_000, 10_010),  # entry: worst 10_030
            _bar(2, 10_000, 10_010, 9_975, 10_000),
        ]
        worst = reference_simulate(
            bars,
            [True, False, False],
            direction="BUY",
            tf_minutes=1,
            sl_paisa=50,
            tp_paisa=None,
            fill_mode=FillMode.WORST_CASE,
        )
        precise = reference_simulate(
            bars,
            [True, False, False],
            direction="BUY",
            tf_minutes=1,
            sl_paisa=50,
            tp_paisa=None,
        )
        assert worst[0].exit_reason == "stop_loss"
        assert worst[0].exit_paisa == 9_975  # min(level 9_980, low 9_975)
        assert precise[0].exit_reason == "end_of_data"

    def test_worst_case_parity_with_production_walker(self) -> None:
        # TEST-SIDE oracle join (the module's own import ban binds the
        # PRODUCTION file, not this test): the naive worst-case arm agrees
        # bit-for-bit with the production walker under the SAME mode on a
        # mixed fixture (range bars, SL+TP, forced tail).
        from brutex.engine.trade_sim import FillPolicy, simulate_trades
        from brutex.strategies.types import Side

        anchor = datetime(2024, 1, 2, 9, 40, tzinfo=UTC)  # 15:10 IST
        bars = [
            _bar(0, 10_000, 10_020, 9_990, 10_010, anchor=anchor),
            _bar(1, 10_010, 10_045, 9_995, 10_030, anchor=anchor),
            _bar(2, 10_030, 10_070, 10_005, 10_050, anchor=anchor),
            _bar(3, 10_040, 10_080, 9_960, 10_000, anchor=anchor),
            _bar(4, 10_000, 10_030, 9_945, 9_990, anchor=anchor),
        ]
        fires = [True, False, False, True, False]
        for direction in (Side.BUY, Side.SELL):
            prod = simulate_trades(
                bars,
                fires,
                direction=direction,
                tf_minutes=1,
                stop_loss_paisa=60,
                take_profit_paisa=60,
                policy=FillPolicy(fill_mode=FillMode.WORST_CASE),
            )
            ref = reference_simulate(
                bars,
                fires,
                direction=direction.value,
                tf_minutes=1,
                sl_paisa=60,
                tp_paisa=60,
                fill_mode=FillMode.WORST_CASE,
            )
            assert len(prod) == len(ref), direction
            for p, r in zip(prod, ref, strict=True):
                for field in COMPARED_TRADE_FIELDS:
                    assert getattr(p, field) == getattr(r, field), (direction, field)
