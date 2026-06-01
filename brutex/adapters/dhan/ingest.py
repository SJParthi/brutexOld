"""Dhan spot ingest — pull a symbol's OHLCV window-by-window into the lake.

The thin orchestration the cli/dashboard call to fill the lake for one spot
instrument: plan the fetch windows (1-minute → 90-day tiles via
:func:`~brutex.adapters.dhan.chunks.plan_spot_intraday_windows`; 1-day → one
call), fetch each via :meth:`~brutex.adapters.protocol.MarketAdapter.fetch_ohlcv`,
and append the parsed bars to a :class:`~brutex.data.sink.BarSink` partitioned by
``(exchange, segment, security_id, interval)``.

Pacing is injected (``pace`` — the orchestrator's adaptive-token-bucket
``acquire`` hook) so the **5 req/s / 100k-day** Dhan limit is honoured
without coupling the limiter in here (CLAUDE.md §11 COMMON). A **transient**
(retryable) error on a window is counted and skipped (the orchestrator
re-queues it); a **terminal** error propagates. Track-1 = spot only
(indices + INDIA VIX + stock spot); options ingest is the separate
rollingoption path.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time

from brutex.adapters.dhan.chunks import plan_spot_intraday_windows
from brutex.adapters.dhan.errors import DhanError, is_retryable
from brutex.adapters.protocol import MarketAdapter
from brutex.data.session import filter_intraday_session
from brutex.data.sink import BarSink
from brutex.models import Instrument

#: Pacing hook invoked once before each window fetch (e.g. token-bucket acquire).
Pacer = Callable[[], None]

#: Canonical daily interval — the only interval not tiled/session-filtered.
_DAILY: str = "1day"


@dataclass(frozen=True, slots=True)
class IngestReport:
    """Outcome of an :func:`ingest_spot` run."""

    windows: int
    bars_appended: int
    transient_failures: int


def _windows(interval: str, start: date, end: date) -> list[tuple[date, date]]:
    """Tile ``[start, end)`` into fetch windows for *interval* (1m → 90d; 1d → one)."""
    if interval == _DAILY:
        return [(start, end)]
    planned = plan_spot_intraday_windows(
        datetime.combine(start, time()), datetime.combine(end, time())
    )
    return [(date.fromisoformat(w.start[:10]), date.fromisoformat(w.end[:10])) for w in planned]


def ingest_spot(
    adapter: MarketAdapter,
    sink: BarSink,
    *,
    instrument: Instrument,
    interval: str,
    start: date,
    end: date,
    pace: Pacer | None = None,
) -> IngestReport:
    """Pull *instrument*'s spot OHLCV over ``[start, end)`` into *sink*.

    Parameters
    ----------
    adapter, sink
        The Dhan adapter (data source) and the lake write sink.
    instrument
        The spot instrument; ``vendor_symbol`` is the Dhan ``SECURITY_ID``.
    interval
        Canonical interval — ``"1minute"`` (90-day tiles) or ``"1day"``.
    start, end
        Half-open date range.
    pace
        Optional per-window pacing hook (rate-limit acquire).

    Returns
    -------
    IngestReport
        Window count, total bars appended, and transient-failure count.

    Raises
    ------
    DhanError
        On a terminal (non-retryable) fetch error.
    """
    bars_appended = 0
    transient = 0
    windows = _windows(interval, start, end)
    for window_start, window_end in windows:
        if pace is not None:
            pace()
        try:
            bars = adapter.fetch_ohlcv(
                symbol=instrument.vendor_symbol,
                exchange=instrument.exchange,
                interval=interval,
                start=window_start,
                end=window_end,
            )
        except DhanError as exc:
            if is_retryable(exc):
                transient += 1
                continue
            raise
        # Drop pre-open / post-close bars before they enter the lake — intraday
        # only (a daily bar has no intraday time). DEC-SESSION-FILTER-001.
        if interval != _DAILY:
            bars = filter_intraday_session(bars)
        result = sink.append(
            exchange=instrument.exchange.value,
            segment=instrument.segment.value,
            symbol=instrument.vendor_symbol,
            interval=interval,
            bars=bars,
        )
        bars_appended += result.bars_appended
    return IngestReport(
        windows=len(windows), bars_appended=bars_appended, transient_failures=transient
    )


__all__ = [
    "IngestReport",
    "Pacer",
    "ingest_spot",
]
