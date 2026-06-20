"""End-to-end offline ingest via the synthetic adapter (DEC-SYNTHETIC-ADAPTER-001).

Proves the synthetic adapter is a true drop-in: it drives the REAL vendor-neutral
ingest spine (``run_groww_spot_ingest``) with NO creds and NO network, through
the real session filter and a real :class:`BarSink`. This is the "engine + ingest
run end-to-end fully offline" guarantee the task asks for.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date

from brutex.adapters.groww.run_ingest import run_groww_spot_ingest
from brutex.adapters.synthetic import SyntheticMarketAdapter, build_synthetic_index
from brutex.data.models import CandleBar
from brutex.data.sink import NullBarSink, SinkAppendResult
from brutex.models import Instrument

#: One regular trading day → 375 in-session 1-minute bars, 1 daily bar.
_START = date(2021, 1, 4)
_END = date(2021, 1, 5)
_BARS_PER_DAY_1M = 375


def _index_instrument(symbol: str) -> Instrument:
    return next(i for i in build_synthetic_index().universe if i.symbol_name == symbol)


@dataclass
class _CountingSink:
    """A real :class:`~brutex.data.sink.BarSink` that records what it receives."""

    received: list[tuple[str, str, int]] = field(default_factory=list)

    def append(
        self,
        *,
        exchange: str,
        segment: str,
        symbol: str,
        interval: str,
        bars: Sequence[CandleBar],
        merge_mode: str = "keep-first",
    ) -> SinkAppendResult:
        """Record ``(symbol, interval, count)`` and report the count back."""
        del exchange, segment
        self.received.append((symbol, interval, len(bars)))
        return SinkAppendResult(bars_appended=len(bars), partitions_written=0, write_results=())


def test_drives_the_real_ingest_spine_offline() -> None:
    adapter = SyntheticMarketAdapter()
    report = run_groww_spot_ingest(
        adapter,
        NullBarSink(),
        instruments=[_index_instrument("NIFTY")],
        intervals=["1minute", "1day"],
        start=_START,
        end=_END,
    )
    assert report.failed_pairs == 0
    assert set(report.pairs) == {("NIFTY", "1minute"), ("NIFTY", "1day")}
    # 375 in-session 1m bars (none dropped by the session filter) + 1 daily bar.
    assert report.bars_appended == _BARS_PER_DAY_1M + 1


def test_session_filter_keeps_every_synthetic_intraday_bar() -> None:
    sink = _CountingSink()
    report = run_groww_spot_ingest(
        SyntheticMarketAdapter(),
        sink,
        instruments=[_index_instrument("SENSEX")],
        intervals=["1minute"],
        start=_START,
        end=_END,
    )
    assert report.failed_pairs == 0
    assert sink.received == [("SENSEX", "1minute", _BARS_PER_DAY_1M)]


def test_spine_runs_for_the_full_synthetic_index_universe() -> None:
    adapter = SyntheticMarketAdapter()
    indices = [i for i in adapter.list_instruments().instruments if i.is_index]
    report = run_groww_spot_ingest(
        adapter,
        NullBarSink(),
        instruments=indices,
        intervals=["1day"],
        start=_START,
        end=_END,
    )
    assert report.failed_pairs == 0
    assert {sym for sym, _ in report.pairs} == {"NIFTY", "BANKNIFTY", "SENSEX"}
    assert report.bars_appended == len(indices)  # one daily bar each
