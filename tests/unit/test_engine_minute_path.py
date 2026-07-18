"""Unit tests for :mod:`brutex.engine.minute_path` (DEC-SWEEP-RESULTS-DEPTH-001 WP1).

Every excursion / equity number below is hand-derived from the fixture bars
(the trade-sim golden discipline): prices are rupee strings, money asserted in
exact int64 paisa. Covers the honest-status matrix (OK / PARTIAL / NO_1M_DATA),
the gap-open depth requirement (R22), the exit-bar fold conventions, the
session equity walk (open-trade adverse marks, exit realization, partial
coverage flushes), and the minute-path max-drawdown fold.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from brutex.calendars.nse import IST
from brutex.data.models import CandleBar
from brutex.engine.minute_path import (
    MinuteIndex,
    MinutePathError,
    PathStatus,
    SessionEquityPoint,
    ist_session_dates,
    minute_path_max_drawdown,
    session_equity_series,
    trade_excursions_1m,
)
from brutex.money import rupees_to_paisa
from brutex.strategies.types import Side

_DAY = date(2024, 1, 2)  # a regular NSE Tuesday
_DAY2 = date(2024, 1, 3)
_DAY3 = date(2024, 1, 4)


def _ts(hh: int, mm: int, *, day: date = _DAY) -> datetime:
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=IST).astimezone(UTC)


def _bar(hh: int, mm: int, o: str, h: str, lo: str, c: str, *, day: date = _DAY) -> CandleBar:
    return CandleBar(
        timestamp=_ts(hh, mm, day=day),
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(lo),
        close=Decimal(c),
        volume=0,
        open_interest=None,
    )


def _flat_minutes(count: int, *, start_hh: int = 9, start_mm: int = 16) -> list[CandleBar]:
    """``count`` flat 100.00 minutes starting 09:16 IST on ``_DAY``."""
    base = _ts(start_hh, start_mm)
    bars = []
    for i in range(count):
        ts = base + timedelta(minutes=i)
        bars.append(
            CandleBar(
                timestamp=ts,
                open=Decimal("100"),
                high=Decimal("100"),
                low=Decimal("100"),
                close=Decimal("100"),
                volume=0,
                open_interest=None,
            )
        )
    return bars


@pytest.fixture
def trade_day_index() -> MinuteIndex:
    """A hand-built 6-minute day with a mid-trade gap-open plunge."""
    return MinuteIndex.from_bars(
        [
            _bar(9, 16, "100.00", "100.10", "99.95", "100.05"),
            _bar(9, 17, "100.05", "100.60", "99.90", "100.40"),
            # Gap-open plunge far beyond any stop: low 97.00.
            _bar(9, 18, "98.00", "98.50", "97.00", "98.20"),
            _bar(9, 19, "98.20", "99.00", "98.00", "98.90"),
            _bar(9, 20, "98.90", "99.50", "98.50", "99.40"),
            _bar(9, 21, "99.40", "100.20", "99.30", "100.00"),
        ]
    )


class TestMinuteIndex:
    def test_from_bars_builds_parallel_timestamps(self, trade_day_index: MinuteIndex) -> None:
        assert len(trade_day_index.bars) == 6
        assert trade_day_index.timestamps == tuple(b.timestamp for b in trade_day_index.bars)

    def test_from_bars_rejects_unsorted(self) -> None:
        bars = [_bar(9, 17, "100", "100", "100", "100"), _bar(9, 16, "100", "100", "100", "100")]
        with pytest.raises(MinutePathError, match="strictly increasing"):
            MinuteIndex.from_bars(bars)

    def test_from_bars_rejects_duplicate_timestamp(self) -> None:
        bar = _bar(9, 16, "100", "100", "100", "100")
        with pytest.raises(MinutePathError, match="strictly increasing"):
            MinuteIndex.from_bars([bar, bar])

    def test_span_slice_inclusive_both_ends(self, trade_day_index: MinuteIndex) -> None:
        lo, hi = trade_day_index.span_slice(_ts(9, 17), _ts(9, 19))
        assert (lo, hi) == (1, 4)

    def test_span_slice_empty_span(self, trade_day_index: MinuteIndex) -> None:
        lo, hi = trade_day_index.span_slice(_ts(10, 0), _ts(10, 5))
        assert lo == hi

    def test_span_slice_rejects_reversed(self, trade_day_index: MinuteIndex) -> None:
        with pytest.raises(MinutePathError, match="start .* after end"):
            trade_day_index.span_slice(_ts(9, 19), _ts(9, 17))


def test_ist_session_dates_sorted_unique() -> None:
    bars = [
        _bar(9, 16, "100", "100", "100", "100", day=_DAY2),
        _bar(9, 16, "100", "100", "100", "100"),
        _bar(9, 17, "100", "100", "100", "100"),
    ]
    assert ist_session_dates(bars) == (_DAY, _DAY2)


class TestTradeExcursions1m:
    def test_buy_excursions_hand_derived(self, trade_day_index: MinuteIndex) -> None:
        # Entry 100.00 @09:16, exit @09:21. Highs: 100.10/100.60/…/100.20 → MFE
        # = 100.60 − 100.00 = 60p. Lows: min 97.00 → MAE = 300p.
        path = trade_excursions_1m(
            trade_day_index,
            entry_ts=_ts(9, 16),
            exit_ts=_ts(9, 21),
            entry_fill_paisa=10_000,
            direction=Side.BUY,
        )
        assert path.status is PathStatus.OK
        assert path.mfe_paisa == 60
        assert path.mae_paisa == 300

    def test_sell_excursions_mirror(self, trade_day_index: MinuteIndex) -> None:
        # Short from 100.00: favourable = entry − min low = 300; adverse =
        # max high − entry = 60.
        path = trade_excursions_1m(
            trade_day_index,
            entry_ts=_ts(9, 16),
            exit_ts=_ts(9, 21),
            entry_fill_paisa=10_000,
            direction=Side.SELL,
        )
        assert path.status is PathStatus.OK
        assert path.mfe_paisa == 300
        assert path.mae_paisa == 60

    def test_gap_open_beyond_stop_shows_true_depth(self, trade_day_index: MinuteIndex) -> None:
        # A stop-loss exit AT the gap minute: even though the sim filled at the
        # −50p level (99.50), the REAL 1m low of the exit minute (97.00) is
        # folded (full-range exit-bar convention for stop_loss) → MAE 300p, the
        # R22 "true adverse depth" requirement.
        path = trade_excursions_1m(
            trade_day_index,
            entry_ts=_ts(9, 16),
            exit_ts=_ts(9, 18),
            entry_fill_paisa=10_000,
            direction=Side.BUY,
            exit_bar_full_range=True,
        )
        assert path.status is PathStatus.OK
        assert path.mae_paisa == 300

    def test_open_only_exit_bar_excludes_exit_range(self, trade_day_index: MinuteIndex) -> None:
        # A forced/next-open fill at 09:18 happens AT the open (98.00): the
        # exit minute's deeper low (97.00) is after the position closed and
        # must NOT be folded → MAE = entry − exit-open = 200p (the 09:16/09:17
        # lows are shallower: 99.95 / 99.90 → 5p / 10p).
        path = trade_excursions_1m(
            trade_day_index,
            entry_ts=_ts(9, 16),
            exit_ts=_ts(9, 18),
            entry_fill_paisa=10_000,
            direction=Side.BUY,
            exit_bar_full_range=False,
        )
        assert path.status is PathStatus.OK
        assert path.mae_paisa == 200
        assert path.mfe_paisa == 60  # 09:17 high folds in full (not the exit bar)

    def test_no_1m_data_when_span_empty(self, trade_day_index: MinuteIndex) -> None:
        path = trade_excursions_1m(
            trade_day_index,
            entry_ts=_ts(11, 0),
            exit_ts=_ts(11, 5),
            entry_fill_paisa=10_000,
            direction=Side.BUY,
        )
        assert path == (None, None, PathStatus.NO_1M_DATA) or (
            path.mae_paisa is None and path.mfe_paisa is None
        )
        assert path.status is PathStatus.NO_1M_DATA

    def test_partial_when_entry_minute_missing(self, trade_day_index: MinuteIndex) -> None:
        # Entry stamped 09:15 (no such minute bar) — the covered fold is an
        # honest lower bound flagged PARTIAL.
        path = trade_excursions_1m(
            trade_day_index,
            entry_ts=_ts(9, 15),
            exit_ts=_ts(9, 17),
            entry_fill_paisa=10_000,
            direction=Side.BUY,
        )
        assert path.status is PathStatus.PARTIAL
        assert path.mfe_paisa == 60

    def test_partial_when_exit_minute_missing(self, trade_day_index: MinuteIndex) -> None:
        path = trade_excursions_1m(
            trade_day_index,
            entry_ts=_ts(9, 16),
            exit_ts=_ts(9, 22),
            entry_fill_paisa=10_000,
            direction=Side.BUY,
        )
        assert path.status is PathStatus.PARTIAL

    def test_zero_range_bars_fold_cleanly(self) -> None:
        index = MinuteIndex.from_bars(_flat_minutes(3))
        path = trade_excursions_1m(
            index,
            entry_ts=_ts(9, 16),
            exit_ts=_ts(9, 18),
            entry_fill_paisa=10_000,
            direction=Side.BUY,
        )
        assert path == (0, 0, PathStatus.OK) or (path.mae_paisa == 0 and path.mfe_paisa == 0)
        assert path.status is PathStatus.OK

    def test_hold_rejected(self, trade_day_index: MinuteIndex) -> None:
        with pytest.raises(MinutePathError, match="BUY or SELL"):
            trade_excursions_1m(
                trade_day_index,
                entry_ts=_ts(9, 16),
                exit_ts=_ts(9, 17),
                entry_fill_paisa=10_000,
                direction=Side.HOLD,
            )


class _Trade:
    """A minimal :class:`EquityTradeLike` for direct series tests."""

    def __init__(
        self,
        entry: datetime,
        exit_: datetime,
        *,
        direction: Side = Side.BUY,
        pnl_paisa: int = 0,
        entry_px: str = "100.00",
    ) -> None:
        self.entry_ts = entry
        self.exit_ts = exit_
        self.direction = direction
        self.pnl_paisa = pnl_paisa
        self.entry_px = Decimal(entry_px)


class TestSessionEquitySeries:
    def test_rejects_non_positive_base(self) -> None:
        with pytest.raises(MinutePathError, match="start_equity_paisa"):
            session_equity_series(None, [], sessions=[_DAY], start_equity_paisa=0)

    def test_rejects_unsorted_sessions(self) -> None:
        with pytest.raises(MinutePathError, match="strictly increasing"):
            session_equity_series(None, [], sessions=[_DAY2, _DAY], start_equity_paisa=1)

    def test_rejects_hold_trade(self) -> None:
        trade = _Trade(_ts(9, 16), _ts(9, 17), direction=Side.HOLD)
        with pytest.raises(MinutePathError, match="BUY or SELL"):
            session_equity_series(None, [trade], sessions=[_DAY], start_equity_paisa=1)

    def test_rejects_overlapping_trades(self) -> None:
        trades = [
            _Trade(_ts(9, 16), _ts(9, 18)),
            _Trade(_ts(9, 18), _ts(9, 20)),  # entry not strictly after prior exit
        ]
        with pytest.raises(MinutePathError, match="non-overlapping"):
            session_equity_series(None, trades, sessions=[_DAY], start_equity_paisa=1)

    def test_rejects_exit_outside_sessions(self) -> None:
        trade = _Trade(_ts(9, 16, day=_DAY2), _ts(9, 17, day=_DAY2))
        with pytest.raises(MinutePathError, match="not one of the supplied sessions"):
            session_equity_series(None, [trade], sessions=[_DAY], start_equity_paisa=1)

    def test_no_index_gives_close_only_series(self) -> None:
        trades = [_Trade(_ts(9, 16), _ts(9, 17), pnl_paisa=50)]
        points, status = session_equity_series(
            None, trades, sessions=[_DAY, _DAY2], start_equity_paisa=1_000
        )
        assert status is PathStatus.NO_1M_DATA
        assert points == (
            SessionEquityPoint(_DAY, 1_050, None, 1),
            SessionEquityPoint(_DAY2, 1_050, None, 0),
        )

    def test_empty_index_is_no_1m_data(self) -> None:
        index = MinuteIndex.from_bars([])
        points, status = session_equity_series(index, [], sessions=[_DAY], start_equity_paisa=1_000)
        assert status is PathStatus.NO_1M_DATA
        assert points == (SessionEquityPoint(_DAY, 1_000, None, 0),)

    def test_empty_sessions_empty_series(self) -> None:
        points, status = session_equity_series(None, [], sessions=[], start_equity_paisa=1)
        assert points == ()
        assert status is PathStatus.NO_1M_DATA

    def test_intraday_low_marks_open_trade_buy(self, trade_day_index: MinuteIndex) -> None:
        # Long 100.00 @09:16 → exits @09:21 at +0. Worst minute low 97.00 →
        # open-trade mark −300p below the 100,000 base.
        trades = [_Trade(_ts(9, 16), _ts(9, 21), pnl_paisa=0)]
        points, status = session_equity_series(
            trade_day_index, trades, sessions=[_DAY], start_equity_paisa=100_000
        )
        assert status is PathStatus.OK
        assert points == (SessionEquityPoint(_DAY, 100_000, 99_700, 1),)

    def test_intraday_low_marks_open_trade_sell(self, trade_day_index: MinuteIndex) -> None:
        # Short 100.00: worst minute high 100.60 → mark −60p.
        trades = [_Trade(_ts(9, 16), _ts(9, 21), direction=Side.SELL, pnl_paisa=0)]
        points, _status = session_equity_series(
            trade_day_index, trades, sessions=[_DAY], start_equity_paisa=100_000
        )
        assert points[0].equity_intraday_low_paisa == 100_000 - 60

    def test_exit_realizes_on_exit_minute(self, trade_day_index: MinuteIndex) -> None:
        # A −100p loser exiting @09:18: post-exit minutes carry the realized
        # 99,900; the session low is the deeper open-trade mark (−300p @09:18).
        trades = [_Trade(_ts(9, 16), _ts(9, 18), pnl_paisa=-100)]
        points, _status = session_equity_series(
            trade_day_index, trades, sessions=[_DAY], start_equity_paisa=100_000
        )
        assert points == (SessionEquityPoint(_DAY, 99_900, 99_700, 1),)

    def test_two_trades_one_session(self, trade_day_index: MinuteIndex) -> None:
        trades = [
            _Trade(_ts(9, 16), _ts(9, 17), pnl_paisa=60),
            _Trade(_ts(9, 19), _ts(9, 20), pnl_paisa=-40, entry_px="98.20"),
        ]
        points, status = session_equity_series(
            trade_day_index, trades, sessions=[_DAY], start_equity_paisa=100_000
        )
        assert status is PathStatus.OK
        assert points[0].equity_close_paisa == 100_020
        assert points[0].trades_closed == 2

    def test_partial_session_without_bars(self, trade_day_index: MinuteIndex) -> None:
        # _DAY2 has no minute bars: its low is honestly None, status PARTIAL,
        # and a trade exiting there still realizes via the end-of-session flush.
        trades = [_Trade(_ts(9, 16, day=_DAY2), _ts(9, 17, day=_DAY2), pnl_paisa=75)]
        points, status = session_equity_series(
            trade_day_index, trades, sessions=[_DAY, _DAY2], start_equity_paisa=1_000
        )
        assert status is PathStatus.PARTIAL
        assert points == (
            SessionEquityPoint(_DAY, 1_000, 1_000, 0),
            SessionEquityPoint(_DAY2, 1_075, None, 1),
        )

    def test_missing_exit_minute_flushes_mid_walk(self) -> None:
        # The 09:18 exit minute bar is MISSING from the index but 09:19 exists:
        # the mid-walk flush realizes the pnl before marking 09:19.
        index = MinuteIndex.from_bars(
            [
                _bar(9, 16, "100.00", "100.10", "99.95", "100.05"),
                _bar(9, 17, "100.05", "100.20", "99.90", "100.10"),
                _bar(9, 19, "100.10", "100.15", "99.85", "100.00"),
            ]
        )
        trades = [_Trade(_ts(9, 16), _ts(9, 18), pnl_paisa=-100)]
        points, status = session_equity_series(
            index, trades, sessions=[_DAY], start_equity_paisa=100_000
        )
        assert status is PathStatus.OK  # every SESSION had bars (interior hole)
        # Post-flush realized 99,900 is the running level at 09:19 (flat).
        assert points == (SessionEquityPoint(_DAY, 99_900, 99_900, 1),)

    def test_trade_entering_later_in_session(self, trade_day_index: MinuteIndex) -> None:
        # Before entry the candidate equity is the flat base (no mark).
        trades = [_Trade(_ts(9, 19), _ts(9, 20), pnl_paisa=0, entry_px="98.20")]
        points, _status = session_equity_series(
            trade_day_index, trades, sessions=[_DAY], start_equity_paisa=100_000
        )
        # Open-trade worst mark: 09:19 low 98.00 → −20p; 09:20 low 98.50 above entry.
        assert points[0].equity_intraday_low_paisa == 100_000 - 20

    def test_zero_trades_flat_series(self, trade_day_index: MinuteIndex) -> None:
        points, status = session_equity_series(
            trade_day_index, [], sessions=[_DAY], start_equity_paisa=42_000
        )
        assert status is PathStatus.OK
        assert points == (SessionEquityPoint(_DAY, 42_000, 42_000, 0),)


class TestMinutePathMaxDrawdown:
    def test_rejects_non_positive_start(self) -> None:
        with pytest.raises(MinutePathError, match="start_equity_paisa"):
            minute_path_max_drawdown([], start_equity_paisa=0)

    def test_empty_series_no_drawdown(self) -> None:
        assert minute_path_max_drawdown([]) == (0, 0.0)

    def test_intraday_low_deepens_drawdown(self) -> None:
        points = [
            SessionEquityPoint(_DAY, 100_000, 99_000, 1),
            SessionEquityPoint(_DAY2, 101_000, 100_500, 1),
        ]
        dd, pct = minute_path_max_drawdown(points, start_equity_paisa=100_000)
        assert dd == 1_000  # 100,000 → intraday 99,000 (close-only would say 0)
        assert pct == 1_000 / 100_000

    def test_none_low_degrades_to_close(self) -> None:
        points = [
            SessionEquityPoint(_DAY, 100_000, None, 0),
            SessionEquityPoint(_DAY2, 99_500, None, 0),
        ]
        dd, pct = minute_path_max_drawdown(points, start_equity_paisa=100_000)
        assert dd == 500
        assert pct == 500 / 100_000

    def test_without_start_first_close_seeds_peak(self) -> None:
        points = [
            SessionEquityPoint(_DAY, 90_000, 80_000, 1),  # first low not measured
            SessionEquityPoint(_DAY2, 85_000, 84_000, 1),
        ]
        dd, pct = minute_path_max_drawdown(points)
        assert dd == 90_000 - 84_000
        assert pct == (90_000 - 84_000) / 90_000

    def test_negative_peak_reports_zero_pct(self) -> None:
        points = [
            SessionEquityPoint(_DAY, -100, None, 1),
            SessionEquityPoint(_DAY2, -500, None, 1),
        ]
        dd, pct = minute_path_max_drawdown(points)
        assert dd == 400
        assert pct == 0.0

    def test_max_pct_is_the_honest_max_not_the_pct_at_the_max_drop(self) -> None:
        """Review NIT (a) regression: the two maxima are INDEPENDENT.

        An early 20% dip from the 100k base (drop 20,000) is a LARGER share
        than the later 150,000 drop from the 1,000,000 peak (15%). The
        pre-fix code returned the pct AT the absolute-max point (0.15),
        understating the honest max-pct (0.20).
        """
        points = [
            SessionEquityPoint(_DAY, 100_000, 80_000, 1),  # 20% dip from the base
            SessionEquityPoint(_DAY2, 1_000_000, None, 1),  # new peak
            SessionEquityPoint(_DAY3, 900_000, 850_000, 1),  # 150k drop = 15%
        ]
        dd, pct = minute_path_max_drawdown(points, start_equity_paisa=100_000)
        assert dd == 150_000  # the absolute maximum
        assert pct == 20_000 / 100_000  # the honest MAX percentage — a different point

    def test_monotonic_rise_zero_drawdown(self) -> None:
        points = [
            SessionEquityPoint(_DAY, 100_100, 100_050, 1),
            SessionEquityPoint(_DAY2, 100_300, 100_150, 1),
        ]
        dd, pct = minute_path_max_drawdown(points, start_equity_paisa=100_000)
        assert (dd, pct) == (0, 0.0)


def test_paisa_conversion_matches_stage1a_seam() -> None:
    """The leaf's rupees→paisa law is bit-identical to the wheel-bound canonical seam."""
    from brutex.engine.stage1a import _price_to_paisa

    for raw in ("100.005", "99.994", "0.01", "14020.7999", "123456.78"):
        assert rupees_to_paisa(Decimal(raw)) == _price_to_paisa(Decimal(raw))


class TestDirectConstructionLazyIndex:
    """A directly-constructed MinuteIndex (bypassing from_bars) builds its
    crate-side index lazily on first use — and caches it
    (DEC-RUST-CASCADE-RESULTS-001; from_bars remains the documented builder).
    """

    def test_direct_construction_builds_and_caches_the_rs_index(self) -> None:
        bars = tuple(_bar(9, 16 + i, "100.00", "101.00", "99.00", "100.50") for i in range(3))
        index = MinuteIndex(bars=bars, timestamps=tuple(b.timestamp for b in bars))
        # First use builds the crate index lazily…
        assert index.span_slice(_ts(9, 16), _ts(9, 18)) == (0, 3)
        first = index._rs_index()
        # …and later uses reuse the SAME cached instance.
        assert index._rs_index() is first
        result = trade_excursions_1m(
            index,
            entry_ts=_ts(9, 16),
            exit_ts=_ts(9, 18),
            entry_fill_paisa=10_000,
            direction=Side.BUY,
        )
        assert result.status is PathStatus.OK
