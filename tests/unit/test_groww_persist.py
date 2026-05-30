"""Tests for `brutex.adapters.groww.persist` — the fetch → parse → lake link.

A fake inner fetcher + fake sink + the REAL parser prove `PersistingFetcher`
parses each chunk's rows, hands the bars to the sink with the right partition
coordinates, settles empty chunks without writing, and is row-resilient — a
single corrupt row (parse error / OHLC-sanity violation) is skipped + logged,
NOT fatal to the whole chunk; an all-corrupt chunk settles NO_DATA. An
unmappable interval / lake-write failure is still a terminal BAD_REQUEST. Kept
orchestrator-free so the module stays standalone-coverage-gateable.
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


class TestRowResilience:
    """A single corrupt row is skipped + logged, never fatal to the chunk
    (DEC-CANDLE-PARSE-RESILIENCE-001) — the prior behaviour quarantined every
    good bar in the 30-day window on the first bad row."""

    def test_corrupt_only_rows_settle_no_data_not_raise(self) -> None:
        from structlog.testing import capture_logs

        # TWO bad rows → the 2nd keeps the 1st's sample_error (covers the
        # "already have a sample" branch) + proves the count aggregates.
        bad1 = _good_row(ts="not-a-date")
        bad2 = _good_row(ts="also-bad")
        fetcher, sink = _fetcher(_FakeInner(rows=[bad1, bad2]))
        with capture_logs() as logs:
            out = fetcher.fetch_chunk(_key())
        assert out == []  # all rows corrupt → NO_DATA (settled), never raises
        assert sink.calls == []  # nothing written
        q = next(e for e in logs if e["event"] == "candle.rows.quarantined")
        assert q["quarantined"] == 2
        assert q["kept"] == 0
        assert "unparseable" in q["sample_error"]

    def test_ohlc_violation_row_is_quarantined_not_fatal(self) -> None:
        # high (98) < low (99) → CandleBar.__post_init__ ValueError → skipped.
        bad: list[object] = ["2021-01-04 09:15:00", 100.0, 98.0, 99.0, 100.5, 1000, None]
        fetcher, sink = _fetcher(_FakeInner(rows=[bad]))
        assert fetcher.fetch_chunk(_key()) == []
        assert sink.calls == []

    def test_one_bad_row_among_good_keeps_the_good(self) -> None:
        from structlog.testing import capture_logs

        rows = [_good_row(), _good_row(ts="not-a-date"), _good_row("2021-01-04 09:16:00")]
        fetcher, sink = _fetcher(_FakeInner(rows=rows))
        with capture_logs() as logs:
            out = fetcher.fetch_chunk(_key())
        assert out  # COMPLETED — the one bad row did NOT nuke the chunk
        assert len(sink.calls) == 1
        assert len(sink.calls[0]["bars"]) == 2  # both good rows persisted
        q = next(e for e in logs if e["event"] == "candle.rows.quarantined")
        assert q["quarantined"] == 1
        assert q["kept"] == 2

    def test_index_null_volume_persists_as_zero(self) -> None:
        # Spot INDEX: Groww returns volume=null → bar persists with volume 0,
        # never quarantined (the actual NSE-NIFTY pull bug).
        idx_row: list[object] = ["2021-01-04 09:15:00", 100.0, 101.0, 99.0, 100.5, None, None]
        fetcher, sink = _fetcher(_FakeInner(rows=[idx_row]))
        out = fetcher.fetch_chunk(_key("NSE-NIFTY", "1m"))
        assert out  # COMPLETED
        bars = sink.calls[0]["bars"]
        assert isinstance(bars, list)
        assert len(bars) == 1
        assert bars[0].volume == 0


_EMPTY_ROW: list[object] = ["2021-01-04 09:15:00", None, None, None, None, None, None]


class TestEmptyBarHandling:
    """No-data (no-trade / halted / unlisted) bars are skipped GRACEFULLY at
    info level — distinct from a malformed-row warning (DEC-CANDLE-EMPTY-BAR-001)."""

    def test_empty_only_rows_settle_no_data_with_info_not_warning(self) -> None:
        from structlog.testing import capture_logs

        fetcher, sink = _fetcher(_FakeInner(rows=[_EMPTY_ROW, _EMPTY_ROW]))
        with capture_logs() as logs:
            out = fetcher.fetch_chunk(_key())
        assert out == []  # NO_DATA — nothing to persist
        assert sink.calls == []
        info = next(e for e in logs if e["event"] == "candle.rows.empty_skipped")
        assert info["empty"] == 2
        assert info["kept"] == 0
        assert not [e for e in logs if e["event"] == "candle.rows.quarantined"]

    def test_good_plus_empty_keeps_good_logs_info(self) -> None:
        from structlog.testing import capture_logs

        fetcher, sink = _fetcher(_FakeInner(rows=[_good_row(), _EMPTY_ROW]))
        with capture_logs() as logs:
            out = fetcher.fetch_chunk(_key())
        assert out  # COMPLETED — the good bar survives
        assert len(sink.calls[0]["bars"]) == 1
        info = next(e for e in logs if e["event"] == "candle.rows.empty_skipped")
        assert info["empty"] == 1
        assert info["kept"] == 1

    def test_corrupt_and_empty_warns_with_both_counts(self) -> None:
        from structlog.testing import capture_logs

        rows = [_good_row(), _EMPTY_ROW, _good_row(ts="not-a-date")]
        fetcher, sink = _fetcher(_FakeInner(rows=rows))
        with capture_logs() as logs:
            out = fetcher.fetch_chunk(_key())
        assert out  # COMPLETED — the good bar survives
        assert len(sink.calls[0]["bars"]) == 1
        q = next(e for e in logs if e["event"] == "candle.rows.quarantined")
        assert q["quarantined"] == 1  # the malformed row
        assert q["empty"] == 1  # the no-data row
        assert q["kept"] == 1
        # When anything was malformed it's a single warning (no separate info).
        assert not [e for e in logs if e["event"] == "candle.rows.empty_skipped"]


class TestMismatchGuard:
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
