"""Unit tests for :mod:`brutex.engine.trade_capture` (DEC-SWEEP-RESULTS-DEPTH-001 WP1).

Covers: the parity-tooth pass/fail branches (mismatch ⇒ loud WARN + empty
capture, never a wrong trade list), VIX stamped / missing / no-stamper honesty
counting, zero-trade combos as first-class, the exit-fill belt + alignment
belt, the 1m-path attachment (incl. the tf==1m walker-equality law), the
NULL-BY-DESIGN ``lots`` / ``costs_paisa`` (honest ``None`` until P5's futures
translation — gap-register Ruling 18, never a fabricated 1/0), and the O-e
baked decision — the walker's AS-FOUND re-entry semantics pinned by test (next
fresh signal after an exit re-enters, no cooldown, until the 15:20 wall).
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
import structlog.testing
from brutex.calendars.nse import IST
from brutex.data.models import CandleBar
from brutex.engine.minute_path import MinuteIndex, PathStatus
from brutex.engine.reference_series import TradeReferenceStamper
from brutex.engine.reference_wire import aggregate_trade_vix
from brutex.engine.results_store import ResultRow
from brutex.engine.trade_capture import (
    _FULL_RANGE_EXIT_REASONS,
    CapturedTrade,
    TradeCaptureError,
    _check_detail_alignment,
    _check_exit_fill_belt,
    capture_combo,
    derived_exit_fill,
)
from brutex.engine.trade_sim import TradeDetail, simulate_trades
from brutex.metrics.types import ClosedTrade
from brutex.strategies.types import Side

_DAY = date(2024, 1, 2)


def _ts(hh: int, mm: int) -> datetime:
    return datetime(_DAY.year, _DAY.month, _DAY.day, hh, mm, tzinfo=IST).astimezone(UTC)


def _bar(hh: int, mm: int, o: str, h: str, lo: str, c: str) -> CandleBar:
    return CandleBar(
        timestamp=_ts(hh, mm),
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(lo),
        close=Decimal(c),
        volume=0,
        open_interest=None,
    )


# TP → SL → forced-15:20 under the ONE worst-case law
# (DEC-FILL-WORST-CASE-ONLY-001). Derived from the trade-sim golden Fixture A
# with TWO low edits (15:18 low 99.80 → 99.95, 15:19 low 99.90 → 99.95): the
# worst-case entry fills at the 15:18 bar HIGH (100.20), so its SL floor sits
# at 99.90 — the ORIGINAL lows touched it on the entry bar itself; the raised
# lows keep the third trade riding to the forced 15:20 wall (the fixture's
# point: TP, then SL, then forced_1520).
_BARS = [
    _bar(9, 15, "100", "100", "100", "100"),
    _bar(9, 16, "100.00", "100.10", "99.95", "100.05"),
    _bar(9, 17, "100.00", "100.60", "99.90", "100.40"),
    _bar(9, 18, "100", "100", "100", "100"),
    _bar(9, 19, "100.00", "100.05", "99.90", "99.95"),
    _bar(9, 20, "100.00", "100.10", "99.60", "99.70"),
    _bar(15, 17, "100", "100", "100", "100"),
    _bar(15, 18, "100.00", "100.20", "99.95", "100.10"),
    _bar(15, 19, "100.00", "100.30", "99.95", "100.20"),
    _bar(15, 20, "100.15", "101.00", "100.00", "100.50"),
]
#: bar_bits with bit 0 set exactly where Fixture A's signals fired.
_SIGNAL_IDX = (0, 3, 6)
_BAR_BITS = [1 if i in _SIGNAL_IDX else 0 for i in range(len(_BARS))]
_MASK = 0b1

_RUN_ID = "ab" * 32
_SL = 30
_TP = 50


def _oracle_trades(direction: Side = Side.BUY) -> list[ClosedTrade]:
    signals = [bool(b & _MASK == _MASK) for b in _BAR_BITS]
    return simulate_trades(
        _BARS,
        signals,
        direction=direction,
        tf_minutes=1,
        stop_loss_paisa=_SL,
        take_profit_paisa=_TP,
    )


def _row(
    *,
    run_id: str = _RUN_ID,
    direction: str = "bull",
    total: int | None = None,
    winning: int | None = None,
    losing: int | None = None,
    net: int | None = None,
) -> ResultRow:
    trades = _oracle_trades(Side.BUY if direction == "bull" else Side.SELL)
    return ResultRow(
        run_id=run_id,
        instrument="NIFTY",
        tf="1minute",
        direction=direction,
        mode="A",
        mask_bits=_MASK,
        net_pnl_paisa=net if net is not None else sum(t.pnl_paisa for t in trades),
        gross_profit_paisa=sum(t.pnl_paisa for t in trades if t.pnl_paisa > 0),
        gross_loss_paisa=-sum(t.pnl_paisa for t in trades if t.pnl_paisa < 0),
        max_drawdown_paisa=0,
        expectancy_paisa=0,
        total_trades=total if total is not None else len(trades),
        winning_trades=(
            winning if winning is not None else sum(1 for t in trades if t.pnl_paisa > 0)
        ),
        losing_trades=losing if losing is not None else sum(1 for t in trades if t.pnl_paisa < 0),
        sharpe_ratio=0.0,
        sortino_ratio=0.0,
        calmar_ratio=0.0,
        profit_factor=0.0,
        win_rate=0.0,
        max_drawdown_pct=0.0,
        avg_entry_price=Decimal("100.00"),
        avg_exit_price=Decimal("100.00"),
    )


class TestCaptureParityPass:
    def test_capture_reproduces_the_stored_row(self) -> None:
        capture = capture_combo(
            _row(),
            bars=_BARS,
            bar_bits=_BAR_BITS,
            tf_minutes=1,
            sl_paisa=_SL,
            tp_paisa=_TP,
            minute_index=None,
            stamper=None,
        )
        assert capture.parity_ok is True
        assert capture.parity_detail == ""
        assert capture.run_id == _RUN_ID
        assert [t.exit_reason for t in capture.trades] == [
            "take_profit",
            "stop_loss",
            "forced_1520",
        ]
        # Contract fields: real fill prices as 2dp Decimals; lots/costs are
        # honest None — NULL-by-design until P5 (Ruling 18), never a
        # fabricated 1/0.
        # Hand-derived under the one worst-case law: entries fill at the
        # NEXT bar's HIGH (100.10 / 100.05 / 100.20); the TP fill is
        # min(level, low) = min(100.60, 99.90) = 99.90 (−20); the SL fill is
        # min(99.75, 99.60) = 99.60 (−45); the forced 15:20 exit fills at the
        # 15:20 bar's adverse LOW = 100.00 (−20).
        assert [t.entry_px for t in capture.trades] == [
            Decimal("100.10"),
            Decimal("100.05"),
            Decimal("100.20"),
        ]
        assert [t.exit_px for t in capture.trades] == [
            Decimal("99.90"),
            Decimal("99.60"),
            Decimal("100.00"),
        ]
        assert all(t.lots is None for t in capture.trades)
        assert all(t.costs_paisa is None for t in capture.trades)
        assert [t.pnl_paisa for t in capture.trades] == [-20, -45, -20]
        # No minute index → honest NO_1M_DATA, None excursions.
        assert all(t.path_1m_status is PathStatus.NO_1M_DATA for t in capture.trades)
        assert all(t.mae_1m_paisa is None and t.mfe_1m_paisa is None for t in capture.trades)
        # No stamper → every trade counts as VIX-missing (honest NULL).
        assert capture.vix_missing_trades == 3
        assert all(t.vix_entry is None and t.vix_exit is None for t in capture.trades)
        # Equity: one session, close carries the −85 worst-case net.
        assert capture.equity_status is PathStatus.NO_1M_DATA
        assert len(capture.equity) == 1
        assert capture.equity[0].equity_close_paisa == 10_000_000 - 85
        assert capture.equity[0].trades_closed == 3

    def test_sell_direction_maps_bear(self) -> None:
        capture = capture_combo(
            _row(direction="bear"),
            bars=_BARS,
            bar_bits=_BAR_BITS,
            tf_minutes=1,
            sl_paisa=_SL,
            tp_paisa=_TP,
            minute_index=None,
            stamper=None,
        )
        assert capture.parity_ok is True
        assert all(t.direction is Side.SELL for t in capture.trades)

    def test_minute_index_at_tf1m_matches_walker_excursions(self) -> None:
        """At tf==1m the 1m fold equals the walker's own MFE/MAE bit-for-bit."""
        capture = capture_combo(
            _row(),
            bars=_BARS,
            bar_bits=_BAR_BITS,
            tf_minutes=1,
            sl_paisa=_SL,
            tp_paisa=_TP,
            minute_index=MinuteIndex.from_bars(_BARS),
            stamper=None,
        )
        assert capture.parity_ok is True
        for trade in capture.trades:
            assert trade.path_1m_status is PathStatus.OK
            assert trade.mae_1m_paisa == trade.mae_paisa
            assert trade.mfe_1m_paisa == trade.mfe_paisa
        assert capture.equity_status is PathStatus.OK
        assert capture.equity[0].equity_intraday_low_paisa is not None

    def test_vix_stamped_and_partial_miss_counted(self) -> None:
        # VIX bars exist at ALL trade instants except the 15:20 forced exit.
        vix_bars = [
            _bar(9, 16, "15.25", "15.25", "15.25", "15.25"),
            _bar(9, 17, "15.30", "15.30", "15.30", "15.30"),
            _bar(9, 19, "15.40", "15.40", "15.40", "15.40"),
            _bar(9, 20, "15.45", "15.45", "15.45", "15.45"),
            _bar(15, 18, "15.50", "15.50", "15.50", "15.50"),
        ]
        capture = capture_combo(
            _row(),
            bars=_BARS,
            bar_bits=_BAR_BITS,
            tf_minutes=1,
            sl_paisa=_SL,
            tp_paisa=_TP,
            minute_index=None,
            stamper=TradeReferenceStamper.from_vix_bars(vix_bars),
        )
        assert capture.parity_ok is True
        first, second, third = capture.trades
        assert (first.vix_entry, first.vix_exit) == (Decimal("15.25"), Decimal("15.30"))
        assert (second.vix_entry, second.vix_exit) == (Decimal("15.40"), Decimal("15.45"))
        assert third.vix_entry == Decimal("15.50")
        assert third.vix_exit is None  # no VIX bar at 15:20 — honest NULL
        assert capture.vix_missing_trades == 1

    def test_result_row_trio_cross_checks_depth_capture_stamps(self) -> None:
        """The merge-train binding cross-check (PR #1274 coordinator note 3).

        DEC-VIX-STAMP-WIRE-001 ∩ DEC-SWEEP-RESULTS-DEPTH-001: for the SAME
        combo over ONE India-VIX series, the launch side's persisted
        ``ResultRow`` trio (``aggregate_trade_vix`` over the stamped oracle
        trades) must agree with the depth pass's captured per-trade stamps —
        both surfaces consume the ONE :class:`TradeReferenceStamper`, and this
        pin makes a derivation drift between them impossible to land silently.
        Uses the partial-miss VIX series so the ``None``-skip law is
        cross-checked too.
        """
        vix_bars = [
            _bar(9, 16, "15.25", "15.25", "15.25", "15.25"),
            _bar(9, 17, "15.30", "15.30", "15.30", "15.30"),
            _bar(9, 19, "15.40", "15.40", "15.40", "15.40"),
            _bar(9, 20, "15.45", "15.45", "15.45", "15.45"),
            _bar(15, 18, "15.50", "15.50", "15.50", "15.50"),
        ]
        stamper = TradeReferenceStamper.from_vix_bars(vix_bars)
        # Launch side: the stamped oracle trades → the ResultRow trio fold.
        signals = [bool(b & _MASK == _MASK) for b in _BAR_BITS]
        trades = simulate_trades(
            _BARS,
            signals,
            direction=Side.BUY,
            tf_minutes=1,
            stop_loss_paisa=_SL,
            take_profit_paisa=_TP,
            reference_stamper=stamper,
        )
        trio = aggregate_trade_vix(trades)
        # Depth side: the SAME combo captured with the SAME stamper.
        capture = capture_combo(
            _row(),
            bars=_BARS,
            bar_bits=_BAR_BITS,
            tf_minutes=1,
            sl_paisa=_SL,
            tp_paisa=_TP,
            minute_index=None,
            stamper=stamper,
        )
        assert capture.parity_ok is True
        # Per-trade stamps agree pairwise (entry AND exit, None included).
        assert [(t.vix_at_entry, t.vix_at_exit) for t in trades] == [
            (c.vix_entry, c.vix_exit) for c in capture.trades
        ]
        # The persisted trio equals an INDEPENDENT fold over the capture's
        # stamps (None-skipped, mean clamped into [min, max] like the ONE law).
        levels = [
            float(v) for c in capture.trades for v in (c.vix_entry, c.vix_exit) if v is not None
        ]
        assert levels, "teeth: the fixture must stamp at least one level"
        low, high = min(levels), max(levels)
        mean = min(max(math.fsum(levels) / len(levels), low), high)
        assert trio == (mean, low, high)

    def test_zero_trade_combo_first_class(self) -> None:
        # A mask bit that never fires → zero trades everywhere; parity holds.
        row = _row(total=0, winning=0, losing=0, net=0)
        object.__setattr__(row, "mask_bits", 0b10)
        capture = capture_combo(
            row,
            bars=_BARS,
            bar_bits=_BAR_BITS,
            tf_minutes=1,
            sl_paisa=_SL,
            tp_paisa=_TP,
            minute_index=MinuteIndex.from_bars(_BARS),
            stamper=None,
        )
        assert capture.parity_ok is True
        assert capture.trades == ()
        assert capture.vix_missing_trades == 0
        assert len(capture.equity) == 1
        assert capture.equity[0].equity_close_paisa == 10_000_000  # flat base


class TestCaptureParityFail:
    def test_mismatch_returns_loud_empty_capture(self) -> None:
        row = _row(total=99, winning=9)  # deliberately wrong stored counts
        with structlog.testing.capture_logs() as logs:
            capture = capture_combo(
                row,
                bars=_BARS,
                bar_bits=_BAR_BITS,
                tf_minutes=1,
                sl_paisa=_SL,
                tp_paisa=_TP,
                minute_index=MinuteIndex.from_bars(_BARS),
                stamper=None,
            )
        assert capture.parity_ok is False
        assert capture.trades == ()
        assert capture.equity == ()
        assert "total_trades=3!=99" in capture.parity_detail
        assert "winning_trades=0!=9" in capture.parity_detail
        events = [log for log in logs if log["event"] == "results_depth.capture_mismatch"]
        assert len(events) == 1
        assert events[0]["run_id"] == _RUN_ID

    def test_net_pnl_mismatch_alone_fails(self) -> None:
        capture = capture_combo(
            _row(net=999_999),
            bars=_BARS,
            bar_bits=_BAR_BITS,
            tf_minutes=1,
            sl_paisa=_SL,
            tp_paisa=_TP,
            minute_index=None,
            stamper=None,
        )
        assert capture.parity_ok is False
        assert capture.parity_detail == "net_pnl_paisa=-85!=999999"


class TestReentryPinAsFound:
    """O-e baked decision: pin the walker's AS-FOUND intraday re-entry semantics."""

    def test_multi_entry_day_reenters_on_next_fresh_signal(self) -> None:
        capture = capture_combo(
            _row(),
            bars=_BARS,
            bar_bits=_BAR_BITS,
            tf_minutes=1,
            sl_paisa=_SL,
            tp_paisa=_TP,
            minute_index=None,
            stamper=None,
        )
        # Three entries the SAME session — after each exit the next fresh
        # signal re-enters at the following bar's open with NO cooldown and
        # NO same-bar re-entry (each entry is a later bar than the prior exit),
        # up to the 15:20 forced wall.
        assert len(capture.trades) == 3
        for earlier, later in zip(capture.trades, capture.trades[1:], strict=False):
            assert later.entry_ts > earlier.exit_ts
        assert capture.trades[-1].exit_reason == "forced_1520"
        assert all(
            t.entry_ts.astimezone(IST).date() == t.exit_ts.astimezone(IST).date()
            for t in capture.trades
        )


# ── session_gap exit-bar full-range fold (refuter round 3 CM4) ───────────────

_GAP_DAY2 = date(2024, 1, 3)


def _gap_ts(hh: int, mm: int, *, day: date = _DAY) -> datetime:
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=IST).astimezone(UTC)


def _gap_bar(hh: int, mm: int, o: str, h: str, lo: str, c: str, *, day: date = _DAY) -> CandleBar:
    return CandleBar(
        timestamp=_gap_ts(hh, mm, day=day),
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(lo),
        close=Decimal(c),
        volume=0,
        open_interest=None,
    )


#: Session-gap fixture: day-1 (2024-01-02) is tail-holed at 09:17 — its stored
#: bars never reach a forced-exit bar — so day-2's first bar trips the (A2)
#: open-position session guard and the trade closes RETROACTIVELY at day-1's
#: last stored bar (09:17) CLOSE under the ``session_gap`` reason
#: (DEC-TRADESIM-OPEN-SESSION-GUARD-001). The 09:17 EXIT bar carries a WIDE
#: range (high 101.00 / low 99.00 around the 100.00 entry fill), so the fold
#: law has teeth: the walker manages 09:17 as an ORDINARY in-position bar —
#: folding its FULL high/low into MFE/MAE — before the retroactive close
#: prices at its close, so the 1m mirror must fold the exit bar IN FULL too
#: (a ``session_gap`` fill is the CLOSE of a fully-folded bar, mechanically
#: identical to ``end_of_data`` — the ``_FULL_RANGE_EXIT_REASONS`` law).
_GAP_BARS = [
    _gap_bar(9, 15, "100", "100", "100", "100"),  # signal arms
    _gap_bar(9, 16, "100.00", "100.10", "99.95", "100.05"),  # entry @ open 100.00
    _gap_bar(9, 17, "100.00", "101.00", "99.00", "100.20"),  # exit bar — WIDE range
    _gap_bar(9, 15, "100", "100", "100", "100", day=_GAP_DAY2),  # boundary bar
]
_GAP_BAR_BITS = [1, 0, 0, 0]


def _gap_row() -> ResultRow:
    # ONE BUY trade under the one worst-case law: entry fill 10_010 paisa
    # (09:16 HIGH); session_gap close prices day-1 09:17's adverse LOW
    # 9_900 paisa → pnl −110; 0 winners, 1 loser.
    return ResultRow(
        run_id=_RUN_ID,
        instrument="NIFTY",
        tf="1minute",
        direction="bull",
        mode="B",
        mask_bits=_MASK,
        net_pnl_paisa=-110,
        gross_profit_paisa=0,
        gross_loss_paisa=110,
        max_drawdown_paisa=0,
        expectancy_paisa=0,
        total_trades=1,
        winning_trades=0,
        losing_trades=1,
        sharpe_ratio=0.0,
        sortino_ratio=0.0,
        calmar_ratio=0.0,
        profit_factor=0.0,
        win_rate=0.0,
        max_drawdown_pct=0.0,
        avg_entry_price=Decimal("100.00"),
        avg_exit_price=Decimal("99.00"),
    )


class TestSessionGapExitBarFullRangeFold:
    """The ``session_gap`` member of ``_FULL_RANGE_EXIT_REASONS`` has behavioral teeth.

    Refuter round 3 CM4: dropping ``"session_gap"`` from the frozenset
    (``trade_capture.py:98``) survived the whole suite — no capture-level test
    drove a ``session_gap`` trade WITH a minute index whose exit bar's range
    extends beyond its open. Under that mutant ``trade_excursions_1m`` folds
    the exit minute OPEN-ONLY, silently UNDERSTATING ``mae_1m``/``mfe_1m``
    (15/0 here instead of the true 110/90 — worst-case entry fill 10_010).
    """

    def test_session_gap_exit_bar_folds_full_1m_range(self) -> None:
        capture = capture_combo(
            _gap_row(),
            bars=_GAP_BARS,
            bar_bits=_GAP_BAR_BITS,
            tf_minutes=1,
            sl_paisa=None,  # Mode B — no SL/TP leg fires on the wide exit bar
            tp_paisa=None,
            minute_index=MinuteIndex.from_bars(_GAP_BARS),
            stamper=None,
        )
        assert capture.parity_ok is True
        (trade,) = capture.trades
        assert trade.exit_reason == "session_gap"
        assert trade.exit_ts == _gap_ts(9, 17)  # day-1's LAST stored bar
        assert trade.pnl_paisa == -110  # session_gap LOW 9_900 − entry 10_010
        # Hand-derived walker (TF-path) excursions from the worst-case entry
        # fill 10_010 (the 09:16 bar HIGH — DEC-FILL-WORST-CASE-ONLY-001):
        #   09:16 (entry bar, full range): fav = 10_010 − 10_010 = 0;
        #                                  adv = 10_010 − 9_995 = 15.
        #   09:17 (exit bar, managed as an ordinary bar BEFORE the retroactive
        #          close): fav = 10_100 − 10_010 = 90;
        #                  adv = 10_010 − 9_900 = 110.
        # → walker mae = max(15, 110) = 110, mfe = max(0, 90) = 90.
        assert trade.mae_paisa == 110
        assert trade.mfe_paisa == 90
        # THE MUTANT KILLER: the 1m fold must mirror the walker's fold law —
        # the session_gap EXIT bar contributes its FULL 1m range, so at tf==1m
        # the 1m pair equals the walker pair bit-for-bit. Under the CM4 mutant
        # (session_gap dropped from _FULL_RANGE_EXIT_REASONS) the exit minute
        # contributes only its OPEN (10_000) and the pair understates to
        # mae_1m=15 / mfe_1m=0.
        assert trade.path_1m_status is PathStatus.OK
        assert trade.mae_1m_paisa == 110 == trade.mae_paisa
        assert trade.mfe_1m_paisa == 90 == trade.mfe_paisa

    def test_session_gap_is_a_full_range_exit_reason(self) -> None:
        # Frozenset contents pin: the walker itself folds the session_gap exit
        # bar in full (it is managed as an ordinary bar before the retroactive
        # close prices at its CLOSE — the finish()/end_of_data pricing class),
        # so the 1m mirror must list it beside end_of_data
        # (DEC-TRADESIM-OPEN-SESSION-GUARD-001).
        assert "session_gap" in _FULL_RANGE_EXIT_REASONS
        assert "end_of_data" in _FULL_RANGE_EXIT_REASONS


class TestBelts:
    def test_derived_exit_fill_buy_sell(self) -> None:
        assert derived_exit_fill(Side.BUY, 10_000, 50) == 10_050
        assert derived_exit_fill(Side.SELL, 10_000, 50) == 9_950

    def test_derived_exit_fill_rejects_hold(self) -> None:
        with pytest.raises(TradeCaptureError, match="BUY or SELL"):
            derived_exit_fill(Side.HOLD, 10_000, 50)

    def test_alignment_belt(self) -> None:
        _check_detail_alignment(2, 2)  # no raise
        with pytest.raises(TradeCaptureError, match="per-close append invariant"):
            _check_detail_alignment(2, 1)

    def test_exit_fill_belt_pass_and_fail(self) -> None:
        trade = ClosedTrade(
            pnl_paisa=50,
            mfe_paisa=60,
            mae_paisa=10,
            holding_bars=2,
            entry_ts=_ts(9, 16),
            exit_ts=_ts(9, 17),
            direction=Side.BUY,
        )
        good = TradeDetail(
            trade=trade, exit_reason="take_profit", entry_fill_paisa=10_000, exit_fill_paisa=10_050
        )
        _check_exit_fill_belt(good, Side.BUY)  # no raise
        bad = TradeDetail(
            trade=trade, exit_reason="take_profit", entry_fill_paisa=10_000, exit_fill_paisa=10_049
        )
        with pytest.raises(TradeCaptureError, match="exit-fill belt failed"):
            _check_exit_fill_belt(bad, Side.BUY)


class TestCapturedTradeValidation:
    @staticmethod
    def _kwargs() -> dict[str, object]:
        return {
            "entry_ts": _ts(9, 16),
            "entry_px": Decimal("100.00"),
            "exit_ts": _ts(9, 17),
            "exit_px": Decimal("100.50"),
            "direction": Side.BUY,
            "lots": None,
            "exit_reason": "take_profit",
            "pnl_paisa": 50,
            "costs_paisa": None,
            "mae_paisa": 10,
            "mfe_paisa": 60,
            "mae_1m_paisa": 10,
            "mfe_1m_paisa": 60,
            "path_1m_status": PathStatus.OK,
            "vix_entry": None,
            "vix_exit": None,
        }

    def test_valid_construction(self) -> None:
        trade = CapturedTrade(**self._kwargs())  # type: ignore[arg-type]
        assert trade.exit_reason == "take_profit"
        assert trade.lots is None  # NULL-by-design until P5 (Ruling 18)
        assert trade.costs_paisa is None

    def test_future_real_execution_leg_values_stay_constructible(self) -> None:
        # P5's translation sidecar will carry REAL lots/costs — the container
        # accepts genuine values while refusing fabricated sub-1 lots.
        kwargs = self._kwargs()
        kwargs["lots"] = 2
        kwargs["costs_paisa"] = 0
        trade = CapturedTrade(**kwargs)  # type: ignore[arg-type]
        assert (trade.lots, trade.costs_paisa) == (2, 0)

    @pytest.mark.parametrize(
        ("field", "value", "match"),
        [
            ("direction", Side.HOLD, "BUY or SELL"),
            ("exit_reason", "vibes", "is not one of"),
            ("lots", 0, "lots must be None or >= 1"),
            ("costs_paisa", -1, "costs_paisa must be None or >= 0"),
            ("mae_paisa", -1, "must be >= 0"),
            ("mfe_paisa", -5, "must be >= 0"),
            ("mae_1m_paisa", None, "None exactly when"),
            ("mfe_1m_paisa", None, "None exactly when"),
            ("mae_1m_paisa", -1, "1m excursions are magnitudes"),
            ("mfe_1m_paisa", -1, "1m excursions are magnitudes"),
        ],
    )
    def test_invalid_field_rejected(self, field: str, value: object, match: str) -> None:
        kwargs = self._kwargs()
        kwargs[field] = value
        with pytest.raises(TradeCaptureError, match=match):
            CapturedTrade(**kwargs)  # type: ignore[arg-type]

    def test_no_1m_data_requires_none_pair(self) -> None:
        kwargs = self._kwargs()
        kwargs["path_1m_status"] = PathStatus.NO_1M_DATA  # values still set → reject
        with pytest.raises(TradeCaptureError, match="None exactly when"):
            CapturedTrade(**kwargs)  # type: ignore[arg-type]

    def test_no_1m_data_with_none_pair_ok(self) -> None:
        kwargs = self._kwargs()
        kwargs.update(
            {"path_1m_status": PathStatus.NO_1M_DATA, "mae_1m_paisa": None, "mfe_1m_paisa": None}
        )
        trade = CapturedTrade(**kwargs)  # type: ignore[arg-type]
        assert trade.path_1m_status is PathStatus.NO_1M_DATA


class TestCaptureOverRealSweepStore:
    """Integration tooth: capture EVERY finalist of a real ``run_persisted_sweep``.

    The stored rows come off the production sweep spine (the Rust kernel arm
    where wheels are installed); the captures come off the pure-Python oracle
    — so a green parity tooth here IS the kernel⇄oracle correctness bridge the
    design leans on (F.3), exercised end-to-end against a real store.
    """

    def test_every_persisted_finalist_captures_with_parity(self, tmp_path: Path) -> None:
        from brutex.data.option_dedup import OptionDedupIndex
        from brutex.engine.results_store import ResultsStore
        from brutex.engine.stage1a import build_bar_bits
        from brutex.engine.sweep_run import ComboDedup, SweepRunConfig, run_persisted_sweep
        from brutex.orchestrator.audit import AuditWriter

        bars = _drift_bars()
        cfg = SweepRunConfig(
            instrument="NIFTY",
            tf="1m",
            direction=Side.BUY,
            mode="A",
            condition_bits=[0, 1, 2, 3, 4],
            k=1,
            data_snapshot_hash="snap0001",
            indicator_version="ind-v1",
            code_commit_sha="deadbeef",
            tf_minutes=1,
            # EOD-only exits (DEC-FILL-WORST-CASE-ONLY-001): a tight SL/TP
            # over these wide-range synthetic bars always fills at the
            # adverse extreme and gate-rejects every combo — the sweep_run
            # suite's _trending_bars recipe, mirrored here.
            sl_paisa=None,
            tp_paisa=None,
        )
        store = ResultsStore(path=tmp_path / "store.parquet")
        dedup = ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6))
        with AuditWriter(path=tmp_path / "audit.jsonl") as audit:
            summary = run_persisted_sweep(bars, cfg, store=store, dedup=dedup, audit=audit)
        assert summary.persisted > 0
        rows = ResultsStore.load(tmp_path / "store.parquet").rows()
        assert len(rows) == summary.persisted
        bar_bits = build_bar_bits(
            bars, cfg.proximity, prior_n=cfg.prior_n, fib_anchor=cfg.fib_anchor
        )
        index = MinuteIndex.from_bars(bars)
        for row in rows:
            capture = capture_combo(
                row,
                bars=bars,
                bar_bits=bar_bits,
                tf_minutes=cfg.tf_minutes,
                sl_paisa=cfg.sl_paisa,
                tp_paisa=cfg.tp_paisa,
                minute_index=index,
                stamper=None,
            )
            assert capture.parity_ok is True, capture.parity_detail
            assert len(capture.trades) == row.total_trades
            assert sum(t.pnl_paisa for t in capture.trades) == row.net_pnl_paisa
            assert all(t.path_1m_status is PathStatus.OK for t in capture.trades)


def _drift_bars() -> list[CandleBar]:
    """The sweep suite's 45-day mostly-up drift with every-5th-day DOWN days.

    The worst-case twin of the old 12-day up-drift fixture: up days win via
    the #1743 ``session_gap`` close despite the entry-at-HIGH haircut, the
    DOWN days keep ``profit_factor`` finite so the ranking gates can pass,
    and 45 days clears MIN_TRADES=30 (deterministic — no RNG).
    """
    from datetime import timedelta

    out: list[CandleBar] = []
    for d in range(45):
        price = 100.0
        down_day = d % 5 == 4
        for m in range(120):
            if down_day:
                close = price - 0.6 if m % 3 else price + 0.3
            else:
                close = price + 0.6 if m % 3 else price - 0.3
            ts = (
                datetime(2026, 1, 5, 9, 16, tzinfo=IST) + timedelta(days=d, minutes=m)
            ).astimezone(UTC)
            hi = max(price, close) + 0.5
            lo = min(price, close) - 0.5
            out.append(
                CandleBar(
                    timestamp=ts,
                    open=Decimal(str(round(price, 2))),
                    high=Decimal(str(round(hi, 2))),
                    low=Decimal(str(round(lo, 2))),
                    close=Decimal(str(round(close, 2))),
                    volume=100,
                    open_interest=None,
                )
            )
            price = close
    return out
