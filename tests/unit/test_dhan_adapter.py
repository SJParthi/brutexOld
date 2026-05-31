"""Tests for `brutex.adapters.dhan.adapter` — the MarketAdapter implementation.

100% line+branch: capabilities, instrument paging, empty derivatives, and
fetch_ohlcv (intraday/daily endpoint + body, index/equity instrument map,
out-of-scope symbol/interval, and the optional ``data`` wrapper).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date
from decimal import Decimal

import pytest
from brutex.adapters.dhan.adapter import DhanMarketAdapter
from brutex.adapters.dhan.client import DhanClient
from brutex.adapters.protocol import MarketAdapter
from brutex.models import Exchange, Instrument, InstrumentType, OutOfScopeError, Segment

_CANDLES = {
    "open": [100.0],
    "high": [110.0],
    "low": [90.0],
    "close": [105.0],
    "timestamp": [1609733100],
}


class _Transport:
    """Records the last request and returns a canned JSON body."""

    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.calls: list[dict[str, object]] = []

    def __call__(
        self, url: str, *, body: bytes, headers: Mapping[str, str], timeout: float
    ) -> tuple[int, bytes]:
        self.calls.append({"url": url, "body": json.loads(body)})
        return 200, json.dumps(self.payload).encode()


def _inst(
    symbol: str, trading: str, itype: InstrumentType, exch: Exchange = Exchange.NSE
) -> Instrument:
    is_idx = itype is InstrumentType.IDX
    return Instrument(
        exchange=exch,
        exchange_token=int(symbol),
        trading_symbol=trading,
        vendor_symbol=symbol,
        name=trading,
        instrument_type=itype,
        segment=Segment.CASH,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_exchange_token=None,
        lot_size=0 if is_idx else 1,
        tick_size=Decimal("0") if is_idx else Decimal("0.05"),
        freeze_quantity=None,
        expiry_date=None,
        strike_price=None,
        is_reserved=False,
        buy_allowed=True,
        sell_allowed=True,
    )


_NIFTY = _inst("13", "NIFTY", InstrumentType.IDX)
_RELIANCE = _inst("2885", "RELIANCE", InstrumentType.EQ)
_SENSEX = _inst("51", "SENSEX", InstrumentType.IDX, exch=Exchange.BSE)


def _adapter(payload: object = _CANDLES) -> tuple[DhanMarketAdapter, _Transport]:
    transport = _Transport(payload)
    client = DhanClient(
        access_token="t",
        client_id="c",
        transport=transport,  # type: ignore[arg-type]
        base_url="https://api.dhan.co/v2/",
    )
    return DhanMarketAdapter(client=client, instruments=(_NIFTY, _RELIANCE, _SENSEX)), transport


class TestProtocolAndCapabilities:
    def test_satisfies_market_adapter_protocol(self) -> None:
        adapter, _ = _adapter()
        assert isinstance(adapter, MarketAdapter)

    def test_capabilities(self) -> None:
        caps = _adapter()[0].capabilities
        assert caps.market_id == "dhan"
        assert caps.exchanges == frozenset({Exchange.NSE, Exchange.BSE})
        assert caps.has_derivatives is False
        assert caps.rate_limit_rps == 5.0
        assert caps.supported_intervals == frozenset({"1minute", "1day"})


class TestListing:
    def test_list_instruments_full_page(self) -> None:
        page = _adapter()[0].list_instruments()
        assert page.instruments == (_NIFTY, _RELIANCE, _SENSEX)
        assert page.next_cursor is None

    def test_list_instruments_cursor_returns_empty(self) -> None:
        page = _adapter()[0].list_instruments(cursor="anything")
        assert page.instruments == ()
        assert page.next_cursor is None

    def test_list_derivatives_is_empty(self) -> None:
        contracts = list(
            _adapter()[0].list_derivatives(underlying_symbol="NIFTY", exchange=Exchange.NSE)
        )
        assert contracts == []


class TestFetchOhlcv:
    def test_intraday_index(self) -> None:
        adapter, t = _adapter()
        bars = adapter.fetch_ohlcv(
            symbol="13",
            exchange=Exchange.NSE,
            interval="1minute",
            start=date(2021, 1, 4),
            end=date(2021, 1, 5),
        )
        assert len(bars) == 1
        assert bars[0].open == Decimal("100.0")
        sent = t.calls[0]
        assert sent["url"].endswith("charts/intraday")  # type: ignore[union-attr]
        body = sent["body"]
        assert isinstance(body, dict)
        assert body["securityId"] == "13"
        assert body["exchangeSegment"] == "NSE_EQ"
        assert body["instrument"] == "INDEX"
        assert body["interval"] == "1"
        assert body["fromDate"] == "2021-01-04 00:00:00"

    def test_daily_equity(self) -> None:
        adapter, t = _adapter()
        bars = adapter.fetch_ohlcv(
            symbol="2885",
            exchange=Exchange.NSE,
            interval="1day",
            start=date(2021, 1, 4),
            end=date(2021, 1, 5),
        )
        assert len(bars) == 1
        sent = t.calls[0]
        assert sent["url"].endswith("charts/historical")  # type: ignore[union-attr]
        body = sent["body"]
        assert isinstance(body, dict)
        assert body["instrument"] == "EQUITY"
        assert body["exchangeSegment"] == "NSE_EQ"
        assert "interval" not in body
        assert body["fromDate"] == "2021-01-04"

    def test_bse_segment(self) -> None:
        adapter, t = _adapter()
        adapter.fetch_ohlcv(
            symbol="51",
            exchange=Exchange.BSE,
            interval="1day",
            start=date(2021, 1, 4),
            end=date(2021, 1, 5),
        )
        body = t.calls[0]["body"]
        assert isinstance(body, dict)
        assert body["exchangeSegment"] == "BSE_EQ"

    def test_data_wrapper_unwrapped(self) -> None:
        adapter, _ = _adapter(payload={"data": _CANDLES})
        bars = adapter.fetch_ohlcv(
            symbol="13",
            exchange=Exchange.NSE,
            interval="1minute",
            start=date(2021, 1, 4),
            end=date(2021, 1, 5),
        )
        assert len(bars) == 1

    def test_unknown_symbol_out_of_scope(self) -> None:
        adapter, _ = _adapter()
        with pytest.raises(OutOfScopeError, match="not in the Dhan universe"):
            adapter.fetch_ohlcv(
                symbol="99999",
                exchange=Exchange.NSE,
                interval="1minute",
                start=date(2021, 1, 4),
                end=date(2021, 1, 5),
            )

    def test_unfetched_interval_out_of_scope(self) -> None:
        adapter, _ = _adapter()
        with pytest.raises(OutOfScopeError, match="is not fetched"):
            adapter.fetch_ohlcv(
                symbol="13",
                exchange=Exchange.NSE,
                interval="5minute",
                start=date(2021, 1, 4),
                end=date(2021, 1, 5),
            )
