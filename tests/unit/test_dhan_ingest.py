"""Tests for `brutex.adapters.dhan.ingest` — window-by-window spot ingest.

100% line+branch: daily single-window vs intraday multi-window tiling, the
pace hook, transient-error counting, terminal-error propagation, and the
bars-appended accounting. Driven by a real DhanMarketAdapter over a
controllable fake transport + a recording fake sink.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal

import pytest
from brutex.adapters.dhan.adapter import DhanMarketAdapter
from brutex.adapters.dhan.client import DhanClient
from brutex.adapters.dhan.errors import DhanBadRequestError
from brutex.adapters.dhan.ingest import IngestReport, ingest_spot
from brutex.data.models import CandleBar
from brutex.data.sink import SinkAppendResult
from brutex.models import Exchange, Instrument, InstrumentType, Segment

_CANDLES = {
    "open": [100.0],
    "high": [110.0],
    "low": [90.0],
    "close": [105.0],
    "timestamp": [1609733100],
}


class _Transport:
    def __init__(self, status: int = 200, payload: object = _CANDLES) -> None:
        self.status = status
        self.payload = payload

    def __call__(
        self, url: str, *, body: bytes, headers: Mapping[str, str], timeout: float
    ) -> tuple[int, bytes]:
        return self.status, json.dumps(self.payload).encode()


class _Sink:
    def __init__(self) -> None:
        self.appends: list[int] = []

    def append(
        self, *, exchange: str, segment: str, symbol: str, interval: str, bars: Sequence[CandleBar]
    ) -> SinkAppendResult:
        del exchange, segment, symbol, interval
        self.appends.append(len(bars))
        return SinkAppendResult(bars_appended=len(bars), partitions_written=1, write_results=())


_INST = Instrument(
    exchange=Exchange.NSE,
    exchange_token=13,
    trading_symbol="NIFTY",
    vendor_symbol="13",
    name="NIFTY",
    instrument_type=InstrumentType.IDX,
    segment=Segment.CASH,
    series=None,
    isin=None,
    underlying_symbol=None,
    underlying_exchange_token=None,
    lot_size=0,
    tick_size=Decimal("0"),
    freeze_quantity=None,
    expiry_date=None,
    strike_price=None,
    is_reserved=False,
    buy_allowed=True,
    sell_allowed=True,
)


def _adapter(status: int = 200, payload: object = _CANDLES) -> DhanMarketAdapter:
    client = DhanClient(
        access_token="t",
        client_id="c",
        transport=_Transport(status, payload),  # type: ignore[arg-type]
        base_url="https://api.dhan.co/v2/",
    )
    return DhanMarketAdapter(client=client, instruments=(_INST,))


class TestIngestSpot:
    def test_daily_single_window(self) -> None:
        sink = _Sink()
        report = ingest_spot(
            _adapter(),
            sink,
            instrument=_INST,
            interval="1day",
            start=date(2021, 1, 1),
            end=date(2021, 8, 1),
        )
        assert report == IngestReport(windows=1, bars_appended=1, transient_failures=0)
        assert sink.appends == [1]

    def test_intraday_tiles_into_90_day_windows(self) -> None:
        sink = _Sink()
        report = ingest_spot(
            _adapter(),
            sink,
            instrument=_INST,
            interval="1minute",
            start=date(2021, 1, 1),
            end=date(2021, 8, 1),  # ~212 days → 3 windows
        )
        assert report.windows == 3
        assert report.bars_appended == 3
        assert sink.appends == [1, 1, 1]

    def test_pace_called_once_per_window(self) -> None:
        calls = {"n": 0}

        def _pace() -> None:
            calls["n"] += 1

        ingest_spot(
            _adapter(),
            _Sink(),
            instrument=_INST,
            interval="1minute",
            start=date(2021, 1, 1),
            end=date(2021, 8, 1),
            pace=_pace,
        )
        assert calls["n"] == 3

    def test_transient_error_counted_and_skipped(self) -> None:
        sink = _Sink()
        report = ingest_spot(
            _adapter(status=429),
            sink,
            instrument=_INST,
            interval="1minute",
            start=date(2021, 1, 1),
            end=date(2021, 8, 1),
        )
        assert report.windows == 3
        assert report.bars_appended == 0
        assert report.transient_failures == 3
        assert sink.appends == []

    def test_intraday_drops_out_of_session_bars(self) -> None:
        # Two bars: 03:30 UTC = 09:00 IST (pre-open, dropped) + 04:05 UTC =
        # 09:35 IST (in-session, kept). Single 1-day-ish range → 1 window.
        payload = {
            "open": [100.0, 101.0],
            "high": [110.0, 111.0],
            "low": [90.0, 91.0],
            "close": [105.0, 106.0],
            "timestamp": [1609731000, 1609733100],  # 03:30 UTC, 04:05 UTC
        }
        sink = _Sink()
        report = ingest_spot(
            _adapter(payload=payload),
            sink,
            instrument=_INST,
            interval="1minute",
            start=date(2021, 1, 1),
            end=date(2021, 3, 1),  # 1 window
        )
        assert report.windows == 1
        assert report.bars_appended == 1  # pre-open bar filtered out
        assert sink.appends == [1]

    def test_terminal_error_propagates(self) -> None:
        with pytest.raises(DhanBadRequestError):
            ingest_spot(
                _adapter(status=400),
                _Sink(),
                instrument=_INST,
                interval="1day",
                start=date(2021, 1, 1),
                end=date(2021, 2, 1),
            )
