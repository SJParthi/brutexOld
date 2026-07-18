"""Unit tests for :mod:`brutex.adapters.dhan.adapter` — 100% line+branch."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from brutex.adapters.dhan.adapter import DHAN_CAPABILITIES, DhanMarketAdapter
from brutex.adapters.dhan.client import DhanClient
from brutex.adapters.dhan.instruments import DhanInstrumentIndex, DhanScripRow
from brutex.models import Exchange, Instrument, InstrumentType, OutOfScopeError, Segment

from tests.fakes.fake_dhan_transport import FakeDhanTransport, Response


def _row(sec: int, sym: str, seg: str, itype: str) -> DhanScripRow:
    return DhanScripRow(
        security_id=sec,
        trading_symbol=sym,
        exchange_segment=seg,
        instrument_type=itype,
        series=None,
        expiry_date=None,
        option_type=None,
        strike_price=None,
        lot_size=None,
        tick_size=None,
    )


def _index() -> DhanInstrumentIndex:
    return DhanInstrumentIndex.build(
        [
            _row(13, "NIFTY", "IDX_I", "INDEX"),
            _row(2885, "RELIANCE", "NSE_EQ", "EQUITY"),
        ]
    )


def _adapter(transport: FakeDhanTransport, **kwargs: object) -> DhanMarketAdapter:
    client = DhanClient(transport=transport, access_token="tok", client_id="1")
    return DhanMarketAdapter(client=client, index=_index(), **kwargs)  # type: ignore[arg-type]


def test_capabilities() -> None:
    adapter = _adapter(FakeDhanTransport())
    caps = adapter.capabilities
    assert caps is DHAN_CAPABILITIES
    assert caps.market_id == "dhan-nse-bse"
    assert caps.has_derivatives is False  # ENGINE scope, not the Dhan market
    assert caps.rate_limit_rps == 5.0
    assert "1day" in caps.supported_intervals


def test_list_instruments_empty_default_universe() -> None:
    adapter = _adapter(FakeDhanTransport())
    page = adapter.list_instruments()
    assert page.instruments == ()
    assert page.next_cursor is None


def test_list_instruments_injected_universe_and_cursor() -> None:
    inst = Instrument(
        exch_id=Exchange.NSE,
        security_id=13,
        symbol_name="NIFTY",
        display_name="NIFTY 50",
        instrument=InstrumentType.IDX,
        segment=Segment.INDEX,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=0,
        tick_size=Decimal("0"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
    )
    adapter = _adapter(FakeDhanTransport(), universe=(inst,))
    assert adapter.list_instruments().instruments == (inst,)
    # a non-None cursor is exhausted (single-page static universe)
    assert adapter.list_instruments(cursor="x").instruments == ()


def test_list_derivatives_empty_iterator() -> None:
    adapter = _adapter(FakeDhanTransport())
    got = list(adapter.list_derivatives(underlying_symbol="NIFTY", exchange=Exchange.NSE))
    assert got == []  # macro/ATM-relative addressing — no dated chain, honestly empty


def test_fetch_ohlcv_intraday_index_drops_volume() -> None:
    transport = FakeDhanTransport(default=Response(200, FakeDhanTransport.candle_payload(n=2)))
    adapter = _adapter(transport)
    bars = adapter.fetch_ohlcv(
        symbol="NIFTY",
        exchange=Exchange.NSE,
        interval="1minute",
        start=date(2024, 1, 1),
        end=date(2024, 1, 31),
    )
    assert len(bars) == 2
    assert all(bar.volume == 0 for bar in bars)  # INDEX → drop_volume
    body = transport.seen_bodies[0]
    assert body["securityId"] == "13"
    assert body["exchangeSegment"] == "IDX_I"
    assert body["interval"] == "1"
    assert body["fromDate"] == "2024-01-01 09:15:00"
    assert body["toDate"] == "2024-01-31 15:30:00"


def test_fetch_ohlcv_daily_equity_keeps_volume() -> None:
    transport = FakeDhanTransport(default=Response(200, FakeDhanTransport.candle_payload(n=1)))
    adapter = _adapter(transport)
    bars = adapter.fetch_ohlcv(
        symbol="RELIANCE",
        exchange=Exchange.NSE,
        interval="1day",
        start=date(2024, 1, 1),
        end=date(2024, 3, 1),
    )
    assert len(bars) == 1
    assert bars[0].volume == 1000  # EQUITY keeps volume
    body = transport.seen_bodies[0]
    assert body["exchangeSegment"] == "NSE_EQ"
    assert body["fromDate"] == "2024-01-01"  # DAILY bound shape
    assert "interval" not in body  # the daily endpoint has no interval field


def test_fetch_ohlcv_tiles_windows() -> None:
    transport = FakeDhanTransport(default=Response(200, FakeDhanTransport.candle_payload(n=1)))
    adapter = _adapter(transport)
    bars = adapter.fetch_ohlcv(
        symbol="NIFTY",
        exchange=Exchange.NSE,
        interval="1minute",
        start=date(2024, 1, 1),
        end=date(2024, 7, 1),
    )
    assert transport.calls == 3  # ~182 days at the 90-day intraday cap
    assert len(bars) == 3


def test_fetch_ohlcv_unknown_symbol_out_of_scope() -> None:
    adapter = _adapter(FakeDhanTransport())
    with pytest.raises(OutOfScopeError, match="not in Dhan scrip index"):
        adapter.fetch_ohlcv(
            symbol="GHOST",
            exchange=Exchange.NSE,
            interval="1minute",
            start=date(2024, 1, 1),
            end=date(2024, 1, 2),
        )


def test_fetch_ohlcv_unsupported_interval() -> None:
    adapter = _adapter(FakeDhanTransport())
    with pytest.raises(ValueError, match="unsupported Dhan interval"):
        adapter.fetch_ohlcv(
            symbol="NIFTY",
            exchange=Exchange.NSE,
            interval="2minute",
            start=date(2024, 1, 1),
            end=date(2024, 1, 2),
        )
