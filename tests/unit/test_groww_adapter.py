"""Unit tests for ``brutex.adapters.groww.adapter`` (DEC-DHAN-GROWW-REMIGRATE-001).

Composes a fake SDK + a small real instrument index — no live API.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date

import pytest
from brutex.adapters.groww.adapter import GROWW_CAPABILITIES, GrowwMarketAdapter
from brutex.adapters.groww.client import GrowwClient
from brutex.adapters.groww.constants import INSTRUMENT_CSV_COLUMNS
from brutex.adapters.groww.instruments import load_index_from_csv
from brutex.adapters.protocol import MarketAdapter
from brutex.models import Exchange, OutOfScopeError

_HEADER = ",".join(INSTRUMENT_CSV_COLUMNS)


def _row(**over: str) -> str:
    cells = dict.fromkeys(INSTRUMENT_CSV_COLUMNS, "")
    cells.update(over)
    return ",".join(cells[c] for c in INSTRUMENT_CSV_COLUMNS)


_NIFTY = _row(
    exchange="NSE",
    exchange_token="NIFTY",
    trading_symbol="NIFTY",
    groww_symbol="NSE-NIFTY",
    name="NIFTY 50",
    instrument_type="IDX",
    segment="CASH",
)
_FUT = _row(
    exchange="NSE",
    exchange_token="53001",
    trading_symbol="NIFTY25Aug26FUT",
    groww_symbol="NSE-NIFTY-25Aug26-FUT",
    instrument_type="FUT",
    segment="FNO",
    underlying_symbol="NIFTY",
    underlying_exchange_token="26000",
    expiry_date="2026-08-25",
    lot_size="65",
    tick_size="0.05",
)
_INDEX = load_index_from_csv("\n".join([_HEADER, _NIFTY, _FUT]) + "\n")


class _FakeSDK:
    def __init__(self) -> None:
        self.calls = 0
        self.last_kwargs: dict[str, object] = {}

    def get_historical_candles(self, **kwargs: object) -> Mapping[str, object]:
        self.calls += 1
        self.last_kwargs = kwargs
        return {"candles": [["2024-01-01T09:15:00", 100.0, 110.0, 90.0, 105.0, 999, None]]}

    def get_expiries(self, **_kw: object) -> Mapping[str, object]:  # pragma: no cover
        return {}

    def get_contracts(self, **_kw: object) -> Mapping[str, object]:  # pragma: no cover
        return {}


def _adapter() -> tuple[GrowwMarketAdapter, _FakeSDK]:
    sdk = _FakeSDK()
    return GrowwMarketAdapter(client=GrowwClient(sdk=sdk, sleep=lambda _s: None), index=_INDEX), sdk


def test_is_a_market_adapter() -> None:
    adapter, _ = _adapter()
    assert isinstance(adapter, MarketAdapter)


def test_capabilities_spot_scope() -> None:
    caps = _adapter()[0].capabilities
    assert caps is GROWW_CAPABILITIES
    assert caps.has_derivatives is False
    assert caps.supports_intraday is True
    assert caps.exchanges == frozenset({Exchange.NSE, Exchange.BSE})
    assert "/" not in caps.market_id
    assert caps.rate_limit_rps > 0


def test_list_instruments_one_page_then_exhausted() -> None:
    adapter, _ = _adapter()
    page = adapter.list_instruments()
    assert len(page.instruments) == 2
    assert page.next_cursor is None
    # any non-None cursor is past the single page
    assert adapter.list_instruments(cursor="x").instruments == ()


def test_list_derivatives_empty() -> None:
    adapter, _ = _adapter()
    assert list(adapter.list_derivatives(underlying_symbol="NIFTY", exchange=Exchange.NSE)) == []


def test_fetch_ohlcv_index_reconstructs_symbol_and_drops_volume() -> None:
    adapter, sdk = _adapter()
    bars = adapter.fetch_ohlcv(
        symbol="NIFTY",
        exchange=Exchange.NSE,
        interval="1minute",
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
    )
    assert len(bars) == 1
    assert bars[0].volume == 0  # index → volume dropped
    assert sdk.last_kwargs["groww_symbol"] == "NSE-NIFTY"  # reconstructed from bare symbol
    assert sdk.last_kwargs["segment"] == "CASH"
    assert sdk.last_kwargs["candle_interval"] == "1minute"


def test_fetch_ohlcv_future_full_symbol_fno_segment_keeps_volume() -> None:
    adapter, sdk = _adapter()
    bars = adapter.fetch_ohlcv(
        symbol="NSE-NIFTY-25Aug26-FUT",
        exchange=Exchange.NSE,
        interval="1minute",
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
    )
    assert bars[0].volume == 999  # non-index → volume kept
    assert sdk.last_kwargs["segment"] == "FNO"
    assert sdk.last_kwargs["groww_symbol"] == "NSE-NIFTY-25Aug26-FUT"  # used as-is


def test_fetch_ohlcv_tiles_multiple_windows() -> None:
    adapter, sdk = _adapter()
    bars = adapter.fetch_ohlcv(
        symbol="NIFTY",
        exchange=Exchange.NSE,
        interval="1minute",
        start=date(2024, 1, 1),
        end=date(2024, 3, 16),  # 75 days → 3 × 30-day windows
    )
    assert sdk.calls == 3
    assert len(bars) == 3  # one bar per window payload


def test_fetch_ohlcv_unknown_symbol_raises() -> None:
    adapter, _ = _adapter()
    with pytest.raises(OutOfScopeError, match="not in Groww universe"):
        adapter.fetch_ohlcv(
            symbol="UNKNOWN",
            exchange=Exchange.NSE,
            interval="1minute",
            start=date(2024, 1, 1),
            end=date(2024, 1, 2),
        )


def test_fetch_ohlcv_bad_interval_raises() -> None:
    adapter, _ = _adapter()
    with pytest.raises(ValueError, match="unsupported Groww interval"):
        adapter.fetch_ohlcv(
            symbol="NIFTY",
            exchange=Exchange.NSE,
            interval="7minute",
            start=date(2024, 1, 1),
            end=date(2024, 1, 2),
        )
