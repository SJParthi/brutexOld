"""Tests for `brutex.adapters.groww.ingest` — end-to-end ingest orchestration.

Drives `run_ingest` with a fake/synthetic fetcher over the real `run_fetch`
loop + `FetchWatermark`: proves it plans chunks per (symbol, interval),
aggregates outcomes, resumes (skips settled), and that the `--dry-run`
`SyntheticCandleFetcher` emits rows the real parser + sink accept. Imports the
orchestrator (run_fetch) → runs in the global suite, not a standalone gate.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from unittest import mock

from brutex.adapters.groww.ingest import (
    BRUTE_FORCE_INDEX_SYMBOLS,
    SyntheticCandleFetcher,
    run_ingest,
)
from brutex.adapters.groww.parser import parse_candle_row
from brutex.adapters.groww.persist import PersistingFetcher
from brutex.data.models import CandleBar
from brutex.data.sink import SinkAppendResult
from brutex.data.watermark import ChunkKey, FetchWatermark


def _dt(month: int, day: int) -> datetime:
    return datetime(2021, month, day, 9, 15, tzinfo=UTC)


def _run(
    fetcher: object, symbols: list[str], intervals: list[str], wm: FetchWatermark, **kw: object
):
    return run_ingest(
        fetcher,  # type: ignore[arg-type]
        symbols,
        intervals,
        _dt(1, 1),
        _dt(1, 10),  # < 30 days → one 1m chunk per (symbol, interval)
        wm,
        sleep_fn=mock.Mock(),
        delay_fn=lambda _n: 0.0,
        **kw,  # type: ignore[arg-type]
    )


class _EmptyFetcher:
    def fetch_chunk(self, key: ChunkKey) -> Sequence[object]:
        return []


class TestRunIngest:
    def test_single_pair_completes(self) -> None:
        wm = FetchWatermark()
        report = _run(SyntheticCandleFetcher(), ["NSE-NIFTY"], ["1m"], wm)
        assert report.outcomes[("NSE-NIFTY", "1m")].completed == 1
        assert report.completed == 1
        assert (report.no_data, report.failed) == (0, 0)

    def test_all_three_indices_and_aggregate(self) -> None:
        wm = FetchWatermark()
        report = _run(SyntheticCandleFetcher(), list(BRUTE_FORCE_INDEX_SYMBOLS), ["1m"], wm)
        assert len(report.outcomes) == 3
        assert report.completed == 3  # one chunk each

    def test_empty_response_is_no_data(self) -> None:
        wm = FetchWatermark()
        report = _run(_EmptyFetcher(), ["NSE-NIFTY"], ["1m"], wm)
        assert report.no_data == 1
        assert report.completed == 0

    def test_resume_skips_settled(self) -> None:
        wm = FetchWatermark()
        first = _run(SyntheticCandleFetcher(), ["NSE-NIFTY"], ["1m"], wm)
        second = _run(SyntheticCandleFetcher(), ["NSE-NIFTY"], ["1m"], wm)
        assert first.completed == 1
        assert second.skipped == 1  # same watermark → re-run skips
        assert second.completed == 0


class TestSyntheticFetcher:
    def test_rows_parse_to_candlebars(self) -> None:
        key = ChunkKey("NSE-NIFTY", "1m", "2021-01-04T09:15:00+00:00", "2021-02-03T09:15:00+00:00")
        rows = SyntheticCandleFetcher(rows_per_chunk=4).fetch_chunk(key)
        assert len(rows) == 4
        bars = [parse_candle_row(r) for r in rows]  # type: ignore[arg-type]
        assert all(isinstance(b, CandleBar) for b in bars)
        # timestamps strictly ascending (1-minute spacing)
        stamps = [b.timestamp for b in bars]
        assert stamps == sorted(stamps)
        assert len(set(stamps)) == 4

    def test_feeds_persisting_fetcher_to_sink(self) -> None:
        key = ChunkKey("NSE-NIFTY", "1m", "2021-01-04T09:15:00+00:00", "2021-02-03T09:15:00+00:00")
        appended: list[CandleBar] = []

        class _Sink:
            def append(
                self,
                *,
                exchange: str,
                segment: str,
                symbol: str,
                interval: str,
                bars: Sequence[CandleBar],
            ) -> SinkAppendResult:
                appended.extend(bars)
                return SinkAppendResult(
                    bars_appended=len(bars), partitions_written=0, write_results=()
                )

        fetcher = PersistingFetcher(inner=SyntheticCandleFetcher(rows_per_chunk=3), sink=_Sink())
        out = fetcher.fetch_chunk(key)
        assert len(out) == 3
        assert len(appended) == 3
        assert all(isinstance(b, CandleBar) for b in appended)
