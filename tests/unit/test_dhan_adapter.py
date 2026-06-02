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
        exch_id=exch,
        security_id=int(symbol),
        symbol_name=trading,
        display_name=trading,
        instrument=itype,
        segment=Segment.CASH,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=0 if is_idx else 1,
        tick_size=Decimal("0") if is_idx else Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
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
            symbol="NIFTY",
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
        # An INDEX is addressed under the cross-exchange IDX_I segment, NOT
        # NSE_EQ (Dhan rejects / returns empty for an index under _EQ).
        assert body["exchangeSegment"] == "IDX_I"
        assert body["instrument"] == "INDEX"
        assert body["interval"] == "1"
        # OI is requested by default (operator wants Open Interest ON).
        assert body["oi"] is True
        assert body["fromDate"] == "2021-01-04 00:00:00"

    def test_intraday_index_todate_is_exclusive_end(self) -> None:
        # FIX 3 regression: Dhan's toDate is NON-INCLUSIVE, so the window's
        # exclusive end boundary (the midnight that STARTS the day after the
        # operator's inclusive end) is what's sent — fetching the whole
        # inclusive end day without dropping it. Here the half-open window is
        # [2021-01-04, 2021-01-05): toDate == the 05 midnight, so all of the
        # 04 session is returned and the 04 day is NOT lost.
        adapter, t = _adapter()
        adapter.fetch_ohlcv(
            symbol="NIFTY",
            exchange=Exchange.NSE,
            interval="1minute",
            start=date(2021, 1, 4),
            end=date(2021, 1, 5),
        )
        body = t.calls[0]["body"]
        assert isinstance(body, dict)
        assert body["toDate"] == "2021-01-05 00:00:00"

    def test_daily_equity(self) -> None:
        adapter, t = _adapter()
        bars = adapter.fetch_ohlcv(
            symbol="RELIANCE",
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
        # A real EQUITY keeps the <exchange>_EQ cash segment (only indices move
        # to IDX_I).
        assert body["exchangeSegment"] == "NSE_EQ"
        assert "interval" not in body
        assert body["oi"] is True
        assert body["fromDate"] == "2021-01-04"
        # FIX 3: daily toDate is the exclusive end day (non-inclusive Dhan), so
        # the inclusive end day 01-04 is fetched.
        assert body["toDate"] == "2021-01-05"

    def test_bse_index_segment_is_idx_i(self) -> None:
        # SENSEX is a BSE INDEX → IDX_I (the cross-exchange index segment),
        # NOT BSE_EQ.
        adapter, t = _adapter()
        adapter.fetch_ohlcv(
            symbol="SENSEX",
            exchange=Exchange.BSE,
            interval="1day",
            start=date(2021, 1, 4),
            end=date(2021, 1, 5),
        )
        body = t.calls[0]["body"]
        assert isinstance(body, dict)
        assert body["exchangeSegment"] == "IDX_I"

    def test_index_volume_is_ignored(self) -> None:
        """An INDEX has no traded volume — Dhan's junk ``volume`` array (here a
        stray ``-2``) is ignored outright, so the index bar's volume is 0
        (DEC-VENDOR-VOLUME-NORMALIZE-001)."""
        payload = {
            "open": [100.0],
            "high": [110.0],
            "low": [90.0],
            "close": [105.0],
            "timestamp": [1609733100],
            "volume": [-2],
        }
        adapter, _ = _adapter(payload=payload)
        bars = adapter.fetch_ohlcv(
            symbol="NIFTY",
            exchange=Exchange.NSE,
            interval="1minute",
            start=date(2021, 1, 4),
            end=date(2021, 1, 5),
        )
        assert bars[0].volume == 0

    def test_equity_volume_is_kept(self) -> None:
        """A real EQUITY keeps its traded volume (``drop_volume`` is False)."""
        payload = {
            "open": [100.0],
            "high": [110.0],
            "low": [90.0],
            "close": [105.0],
            "timestamp": [1609733100],
            "volume": [555],
        }
        adapter, _ = _adapter(payload=payload)
        bars = adapter.fetch_ohlcv(
            symbol="RELIANCE",
            exchange=Exchange.NSE,
            interval="1minute",
            start=date(2021, 1, 4),
            end=date(2021, 1, 5),
        )
        assert bars[0].volume == 555

    def test_data_wrapper_unwrapped(self) -> None:
        adapter, _ = _adapter(payload={"data": _CANDLES})
        bars = adapter.fetch_ohlcv(
            symbol="NIFTY",
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
                symbol="NIFTY",
                exchange=Exchange.NSE,
                interval="5minute",
                start=date(2021, 1, 4),
                end=date(2021, 1, 5),
            )
