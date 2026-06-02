"""Tests for `brutex.adapters.dhan.synthetic_adapter.SyntheticDhanAdapter`.

100% line+branch: the protocol surface (capabilities / list_instruments with
and without a cursor / empty list_derivatives / fetch_ohlcv), the default
``bars_per_window``, that it satisfies the runtime-checkable ``MarketAdapter``
protocol, and that it drives the real ingest spine offline end-to-end.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from brutex.adapters.dhan.run_ingest import run_dhan_spot_ingest
from brutex.adapters.dhan.synthetic_adapter import SyntheticDhanAdapter
from brutex.adapters.protocol import MarketAdapter
from brutex.data.session import in_session
from brutex.data.sink import NullBarSink
from brutex.models import Exchange, Instrument, InstrumentType, Segment


def _instrument(symbol: str = "13") -> Instrument:
    """Build a minimal spot index instrument keyed by *symbol* (``symbol_name``)."""
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=1,
        symbol_name=symbol,
        display_name=None,
        instrument=InstrumentType.IDX,
        segment=Segment.CASH,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=0,
        tick_size=Decimal("0"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
    )


def test_is_a_market_adapter() -> None:
    assert isinstance(SyntheticDhanAdapter(), MarketAdapter)


def test_capabilities() -> None:
    caps = SyntheticDhanAdapter().capabilities
    assert caps.market_id == "dhan"
    assert caps.has_derivatives is False
    assert caps.supported_intervals == frozenset({"1minute", "1day"})


def test_list_instruments_one_page_then_exhausted() -> None:
    inst = _instrument()
    adapter = SyntheticDhanAdapter(instruments=(inst,))
    page = adapter.list_instruments()
    assert page.instruments == (inst,)
    assert page.next_cursor is None
    # Any non-None cursor is exhausted (single static page).
    empty = adapter.list_instruments(cursor="x")
    assert empty.instruments == ()
    assert empty.next_cursor is None


def test_list_derivatives_is_empty() -> None:
    adapter = SyntheticDhanAdapter()
    assert list(adapter.list_derivatives(underlying_symbol="NIFTY", exchange=Exchange.NSE)) == []


def test_fetch_ohlcv_returns_in_session_bars() -> None:
    adapter = SyntheticDhanAdapter(bars_per_window=3)
    bars = adapter.fetch_ohlcv(
        symbol="13",
        exchange=Exchange.NSE,
        interval="1minute",
        start=date(2021, 1, 4),
        end=date(2021, 1, 5),
    )
    assert len(bars) == 3
    assert all(in_session(b.timestamp) for b in bars)


def test_default_bars_per_window() -> None:
    bars = SyntheticDhanAdapter().fetch_ohlcv(
        symbol="13",
        exchange=Exchange.NSE,
        interval="1minute",
        start=date(2021, 1, 4),
        end=date(2021, 1, 5),
    )
    assert len(bars) == 5  # default bars_per_window


def test_drives_the_ingest_spine_offline() -> None:
    """The synthetic adapter is a drop-in: it runs the real spine with no I/O."""
    adapter = SyntheticDhanAdapter(bars_per_window=4)
    report = run_dhan_spot_ingest(
        adapter,
        NullBarSink(),
        instruments=[_instrument("13")],
        intervals=["1minute", "1day"],
        start=date(2021, 1, 4),
        end=date(2021, 1, 5),
    )
    assert report.failed_pairs == 0
    # Both pairs ran; the daily pair is not session-filtered, the 1m pair is
    # (all synthetic bars are in-session, so none are dropped).
    assert set(report.pairs) == {("13", "1minute"), ("13", "1day")}
    assert report.bars_appended == 8  # 4 bars × 2 intervals
