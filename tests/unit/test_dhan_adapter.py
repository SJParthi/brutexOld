"""Tests for :mod:`brutex.adapters.dhan.adapter` — the MarketAdapter impl."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime

import pytest
from brutex.adapters.dhan.adapter import DHAN_CAPABILITIES, DhanMarketAdapter
from brutex.adapters.protocol import MarketAdapter
from brutex.models import Exchange, OutOfScopeError

_EPOCH = int(datetime(2022, 6, 1, 3, 45, tzinfo=UTC).timestamp())


class _FakeClient:
    def __init__(self, payloads: list[Mapping[str, object]]) -> None:
        self._payloads = payloads
        self.calls: list[dict[str, object]] = []

    def intraday_candles(
        self,
        *,
        security_id: int,
        from_date: str,
        to_date: str,
        exchange_segment: str = "IDX_I",
        instrument: str = "INDEX",
        interval: int = 1,
        oi: bool = False,
    ) -> Mapping[str, object]:
        self.calls.append(
            {
                "security_id": security_id,
                "from_date": from_date,
                "to_date": to_date,
                "exchange_segment": exchange_segment,
                "instrument": instrument,
            }
        )
        return self._payloads.pop(0)


def _one_bar() -> Mapping[str, object]:
    return {
        "open": [100.0],
        "high": [101.0],
        "low": [99.0],
        "close": [100.5],
        "volume": [0],
        "timestamp": [_EPOCH],
    }


def test_adapter_satisfies_protocol() -> None:
    adapter = DhanMarketAdapter(client=_FakeClient([]), security_ids={"NIFTY": 13})
    assert isinstance(adapter, MarketAdapter)


def test_capabilities_property_returns_the_stable_descriptor() -> None:
    adapter = DhanMarketAdapter(client=_FakeClient([]), security_ids={})
    assert adapter.capabilities is DHAN_CAPABILITIES


def test_capabilities_are_spot_index_only() -> None:
    assert DHAN_CAPABILITIES.has_derivatives is False
    assert DHAN_CAPABILITIES.supports_intraday is True
    assert DHAN_CAPABILITIES.supported_intervals == frozenset({"1minute"})


def test_fetch_ohlcv_resolves_id_and_addresses_idx_i() -> None:
    client = _FakeClient([_one_bar()])
    adapter = DhanMarketAdapter(client=client, security_ids={"NIFTY": 13})
    bars = adapter.fetch_ohlcv(
        symbol="NIFTY",
        exchange=Exchange.NSE,
        interval="1minute",
        start=date(2022, 6, 1),
        end=date(2022, 6, 20),
    )
    assert len(bars) == 1
    assert bars[0].volume == 0  # index — dropped
    call = client.calls[0]
    assert call["security_id"] == 13
    assert call["exchange_segment"] == "IDX_I"
    assert call["instrument"] == "INDEX"
    assert call["from_date"] == "2022-06-01"  # ISO date part, not the full timestamp


def test_fetch_ohlcv_tiles_multiple_windows() -> None:
    client = _FakeClient([_one_bar(), _one_bar(), _one_bar()])
    adapter = DhanMarketAdapter(client=client, security_ids={"NIFTY": 13})
    bars = adapter.fetch_ohlcv(
        symbol="NIFTY",
        exchange=Exchange.NSE,
        interval="1minute",
        start=date(2022, 6, 1),
        end=date(2022, 12, 1),  # ~183 days → 3 windows
    )
    assert len(client.calls) == 3
    assert len(bars) == 3


def test_fetch_ohlcv_unresolved_symbol_raises_out_of_scope() -> None:
    adapter = DhanMarketAdapter(client=_FakeClient([]), security_ids={})
    with pytest.raises(OutOfScopeError):
        adapter.fetch_ohlcv(
            symbol="MYSTERY",
            exchange=Exchange.NSE,
            interval="1minute",
            start=date(2022, 6, 1),
            end=date(2022, 6, 20),
        )


def test_fetch_ohlcv_unsupported_interval_fails_loud() -> None:
    adapter = DhanMarketAdapter(client=_FakeClient([]), security_ids={"NIFTY": 13})
    with pytest.raises(ValueError, match="unsupported Dhan spot interval"):
        adapter.fetch_ohlcv(
            symbol="NIFTY",
            exchange=Exchange.NSE,
            interval="5minute",
            start=date(2022, 6, 1),
            end=date(2022, 6, 20),
        )


def test_list_instruments_is_empty_single_page() -> None:
    adapter = DhanMarketAdapter(client=_FakeClient([]), security_ids={})
    page = adapter.list_instruments()
    assert page.instruments == ()
    assert page.next_cursor is None


def test_list_derivatives_is_empty() -> None:
    adapter = DhanMarketAdapter(client=_FakeClient([]), security_ids={})
    assert list(adapter.list_derivatives(underlying_symbol="NIFTY", exchange=Exchange.NSE)) == []
