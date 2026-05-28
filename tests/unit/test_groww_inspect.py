"""Tests for `brutex.adapters.groww.inspect` — the candle-shape probe.

Builds bars at specific IST times-of-day and asserts the probe buckets them
pre / regular / post-market (NSE 09:15–15:30 IST) and flags off-grid
timestamps — the "does Groww give pre/post-market + precise timestamps?" answer.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from brutex.adapters.groww.inspect import (
    CandleInspection,
    CollectingBarSink,
    inspect_bars,
)
from brutex.data.models import CandleBar

_IST = ZoneInfo("Asia/Kolkata")


def _bar(hh: int, mm: int, *, day: int = 4) -> CandleBar:
    """A valid CandleBar whose timestamp is ``hh:mm`` IST on 2021-01-<day>."""
    ist_dt = datetime(2021, 1, day, hh, mm, tzinfo=_IST)
    return CandleBar(
        timestamp=ist_dt.astimezone(UTC),
        open=Decimal("100.0"),
        high=Decimal("101.0"),
        low=Decimal("99.0"),
        close=Decimal("100.0"),
        volume=10,
        open_interest=None,
    )


class TestClassification:
    def test_empty(self) -> None:
        insp = inspect_bars("NSE-NIFTY", "1minute", [])
        assert insp.bar_count == 0
        assert insp.aligned is True
        assert insp.first_tod_ist is None
        assert "NO DATA" in insp.summary()

    def test_pre_market_bar(self) -> None:
        insp = inspect_bars("NSE-NIFTY", "1minute", [_bar(9, 0)])  # 09:00 IST < 09:15
        assert (insp.pre_market, insp.regular, insp.post_market) == (1, 0, 0)

    def test_regular_bar_aligned(self) -> None:
        insp = inspect_bars("NSE-NIFTY", "1minute", [_bar(9, 15)])  # session open
        assert (insp.pre_market, insp.regular, insp.post_market) == (0, 1, 0)
        assert insp.aligned is True
        assert "aligned" in insp.summary()
        assert insp.first_tod_ist == "09:15:00"

    def test_post_market_bar(self) -> None:
        insp = inspect_bars("NSE-NIFTY", "1minute", [_bar(15, 45)])  # 15:45 IST > 15:30
        assert (insp.pre_market, insp.regular, insp.post_market) == (0, 0, 1)

    def test_mixed_session(self) -> None:
        bars = [_bar(9, 0), _bar(9, 15), _bar(12, 0), _bar(15, 45)]
        insp = inspect_bars("NSE-NIFTY", "5minute", bars)
        assert (insp.pre_market, insp.regular, insp.post_market) == (1, 2, 1)
        assert insp.first_tod_ist == "09:00:00"
        assert insp.last_tod_ist == "15:45:00"


class TestAlignment:
    def test_misaligned_intraday_flagged(self) -> None:
        # 09:17 IST is 2 min after open; not on the 5-minute grid.
        insp = inspect_bars("NSE-NIFTY", "5minute", [_bar(9, 17)])
        assert insp.aligned is False
        assert "MISALIGNED" in insp.summary()

    def test_aligned_5m_grid(self) -> None:
        insp = inspect_bars("NSE-NIFTY", "5minute", [_bar(9, 15), _bar(9, 20), _bar(9, 25)])
        assert insp.aligned is True

    def test_daily_interval_has_no_grid_check(self) -> None:
        # 1d has no intraday grid → an odd time-of-day never trips alignment.
        insp = inspect_bars("NSE-NIFTY", "1day", [_bar(9, 17)])
        assert insp.aligned is True


class TestCollectingSink:
    def test_accumulates_and_inspects_by_pair(self) -> None:
        sink = CollectingBarSink()
        sink.append(
            exchange="NSE",
            segment="CASH",
            symbol="NSE-NIFTY",
            interval="1minute",
            bars=[_bar(9, 15)],
        )
        sink.append(
            exchange="NSE",
            segment="CASH",
            symbol="NSE-NIFTY",
            interval="1minute",
            bars=[_bar(9, 16)],
        )
        sink.append(
            exchange="BSE",
            segment="CASH",
            symbol="BSE-SENSEX",
            interval="1day",
            bars=[_bar(9, 15)],
        )
        inspections = sink.inspections()
        assert len(inspections) == 2  # two distinct (symbol, interval) pairs
        by_symbol = {i.symbol: i for i in inspections}
        assert by_symbol["NSE-NIFTY"].bar_count == 2  # two appends accumulated
        assert by_symbol["BSE-SENSEX"].bar_count == 1
        assert all(isinstance(i, CandleInspection) for i in inspections)
