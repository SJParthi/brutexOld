"""Tests for `brutex.adapters.groww.persist` — the fetch → parse → lake link.

A fake inner fetcher + fake sink + the REAL parser prove `PersistingFetcher`
parses each chunk's rows, hands the bars to the sink with the right partition
coordinates, settles empty chunks without writing, and quarantines a corrupt
row (parse error / OHLC-sanity violation) as a terminal BAD_REQUEST rather than
dropping or writing it. Kept orchestrator-free so the module stays
standalone-coverage-gateable.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from brutex.adapters.groww.client import VendorSdkError
from brutex.adapters.groww.persist import PersistingFetcher
from brutex.data.errors import PartitionError
from brutex.data.models import CandleBar
from brutex.data.sink import SinkAppendResult
from brutex.data.watermark import ChunkKey


def _good_row(ts: str = "2021-01-04 09:15:00") -> list[object]:
    return [ts, 100.0, 101.0, 99.0, 100.5, 1000, None]


class _FakeInner:
    def __init__(self, rows: object = None, raises: BaseException | None = None) -> None:
        self._rows = rows
        self._raises = raises

    def fetch_chunk(self, key: ChunkKey) -> Sequence[object]:
        if self._raises is not None:
            raise self._raises
        return self._rows  # type: ignore[return-value]


class _FakeSink:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def append(
        self,
        *,
        exchange: str,
        segment: str,
        symbol: str,
        interval: str,
        bars: Sequence[CandleBar],
    ) -> SinkAppendResult:
        self.calls.append(
            {
                "exchange": exchange,
                "segment": segment,
                "symbol": symbol,
                "interval": interval,
                "bars": list(bars),
            }
        )
        return SinkAppendResult(bars_appended=len(bars), partitions_written=0, write_results=())


def _key(symbol: str = "NSE-NIFTY", interval: str = "1m") -> ChunkKey:
    return ChunkKey(symbol, interval, "2021-01-01T09:15:00+00:00", "2021-01-31T09:15:00+00:00")


def _fetcher(inner: _FakeInner) -> tuple[PersistingFetcher, _FakeSink]:
    sink = _FakeSink()
    return PersistingFetcher(inner=inner, sink=sink), sink


class TestPersist:
    def test_parses_and_appends(self) -> None:
        fetcher, sink = _fetcher(_FakeInner(rows=[_good_row(), _good_row("2021-01-04 09:16:00")]))
        out = fetcher.fetch_chunk(_key())
        assert len(out) == 2  # raw rows returned unchanged
        assert len(sink.calls) == 1
        call = sink.calls[0]
        assert call["exchange"] == "NSE"
        assert call["segment"] == "CASH"
        assert call["symbol"] == "NSE-NIFTY"
        assert call["interval"] == "1minute"  # translated short → lake-canonical
        bars = call["bars"]
        assert isinstance(bars, list)
        assert all(isinstance(b, CandleBar) for b in bars)
        assert len(bars) == 2

    def test_empty_chunk_settles_without_writing(self) -> None:
        fetcher, sink = _fetcher(_FakeInner(rows=[]))
        assert fetcher.fetch_chunk(_key()) == []
        assert sink.calls == []

    def test_derivative_symbol_maps_to_fno(self) -> None:
        fetcher, sink = _fetcher(_FakeInner(rows=[_good_row()]))
        fetcher.fetch_chunk(_key("NSE-NIFTY-30Sep25-24650-CE"))
        assert sink.calls[0]["segment"] == "FNO"

    def test_bse_symbol_maps_to_bse(self) -> None:
        fetcher, sink = _fetcher(_FakeInner(rows=[_good_row()]))
        fetcher.fetch_chunk(_key("BSE-SENSEX", "1d"))
        assert sink.calls[0]["exchange"] == "BSE"


class TestMismatchGuard:
    def test_unparseable_row_quarantines(self) -> None:
        bad = _good_row(ts="not-a-date")
        fetcher, sink = _fetcher(_FakeInner(rows=[bad]))
        with pytest.raises(VendorSdkError) as ei:
            fetcher.fetch_chunk(_key())
        assert type(ei.value).__name__ == "VendorAPIBadRequestException"
        assert sink.calls == []  # nothing written on a mismatch

    def test_ohlc_violation_quarantines(self) -> None:
        # high (98) < low (99) → CandleBar.__post_init__ ValueError.
        bad: list[object] = ["2021-01-04 09:15:00", 100.0, 98.0, 99.0, 100.5, 1000, None]
        fetcher, sink = _fetcher(_FakeInner(rows=[bad]))
        with pytest.raises(VendorSdkError) as ei:
            fetcher.fetch_chunk(_key())
        assert type(ei.value).__name__ == "VendorAPIBadRequestException"
        assert sink.calls == []

    def test_unmappable_interval_quarantines(self) -> None:
        fetcher, sink = _fetcher(_FakeInner(rows=[_good_row()]))
        with pytest.raises(VendorSdkError) as ei:
            fetcher.fetch_chunk(_key(interval="7m"))  # not a Groww native interval
        assert type(ei.value).__name__ == "VendorAPIBadRequestException"
        assert sink.calls == []

    def test_lake_write_failure_quarantines(self) -> None:
        class _BoomSink:
            def append(self, **_kw: object) -> SinkAppendResult:
                raise PartitionError("bad partition coords")

        fetcher = PersistingFetcher(inner=_FakeInner(rows=[_good_row()]), sink=_BoomSink())
        with pytest.raises(VendorSdkError) as ei:
            fetcher.fetch_chunk(_key())
        assert type(ei.value).__name__ == "VendorAPIBadRequestException"


class TestPassThrough:
    def test_inner_exception_propagates_unchanged(self) -> None:
        boom = VendorSdkError("inner blew up")
        fetcher, sink = _fetcher(_FakeInner(raises=boom))
        with pytest.raises(VendorSdkError) as ei:
            fetcher.fetch_chunk(_key())
        assert ei.value is boom  # not re-wrapped — only OUR parse errors are
        assert sink.calls == []

    def test_custom_parser_is_used(self) -> None:
        sentinel = CandleBar.__new__(CandleBar)  # bypass validation — identity check only
        fetcher, sink = _fetcher(_FakeInner(rows=[["anything"]]))
        fetcher.parse = lambda _row: sentinel
        fetcher.fetch_chunk(_key())
        assert sink.calls[0]["bars"] == [sentinel]
