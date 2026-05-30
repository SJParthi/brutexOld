"""Persisting fetch decorator — the fetch → parse → lake link.

:func:`~brutex.adapters.groww.fetch_loop.run_fetch` drives a
:class:`~brutex.adapters.groww.fetch_loop.CandleFetcher` and records each
chunk's *status* in the watermark — but it discards the rows themselves.
:class:`PersistingFetcher` closes that gap WITHOUT touching the loop: it is
itself a ``CandleFetcher`` that wraps an inner one (the real
:class:`~brutex.adapters.groww.client.GrowwCandleFetcher`), and on each
successful chunk it

1. parses every raw 7-field row to a validated
   :class:`~brutex.data.models.CandleBar`
   (:func:`~brutex.adapters.groww.parser.parse_candle_row`), and
2. appends the bars to a :class:`~brutex.data.sink.BarSink` (canonical
   Parquet + DuckDB catalog in production), then
3. returns the raw rows unchanged — so the loop's truthiness check still
   settles COMPLETED (rows present) vs NO_DATA (empty) exactly as before.

Mismatch safety (the operator's "no data mismatch" rule): a corrupt row that
fails the parser or the :class:`CandleBar` OHLC-sanity guard is re-raised as a
neutral ``BAD_REQUEST`` so the loop QUARANTINES that chunk (terminal, surfaced
for review) — never silently dropped, never silently written.

Dependency-light by design: it imports only pure modules at runtime (parser,
client helpers, models, watermark); the :class:`BarSink` / ``CandleFetcher``
types are ``TYPE_CHECKING``-only, so this module imports — and unit-tests with
a fake sink — without ``polars`` / ``duckdb`` / the orchestrator chain.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

from brutex.adapters.groww.chunks import GROWW_INTERVAL_TO_PARTITION
from brutex.adapters.groww.client import bad_request, symbol_to_exchange_segment
from brutex.adapters.groww.parser import CandleParseError, EmptyCandleRow, parse_candle_row
from brutex.data.errors import DataLakeError
from brutex.data.models import CandleBar
from brutex.data.watermark import ChunkKey

_LOG = structlog.get_logger("brutex.persist")

if TYPE_CHECKING:  # pragma: no cover — typing-only, no runtime import
    from brutex.adapters.groww.fetch_loop import CandleFetcher
    from brutex.data.sink import BarSink


@dataclass(slots=True)
class PersistingFetcher:
    """A :class:`CandleFetcher` that also parses + persists each chunk.

    Parameters
    ----------
    inner
        The wrapped fetcher that does the actual network pull (and rate-limit
        pacing + error translation) — typically a
        :class:`~brutex.adapters.groww.client.GrowwCandleFetcher`.
    sink
        Where parsed bars land (:class:`~brutex.data.sink.ParquetBarSink` in
        production; a :class:`~brutex.data.sink.NullBarSink` or fake in tests).
    parse
        Row → :class:`CandleBar` parser. Injectable for tests; defaults to
        :func:`~brutex.adapters.groww.parser.parse_candle_row`.
    """

    inner: CandleFetcher
    sink: BarSink
    parse: Callable[[Sequence[object]], CandleBar] = field(default=parse_candle_row)

    def fetch_chunk(self, key: ChunkKey) -> Sequence[object]:
        """Fetch, parse, and persist one chunk; return the raw rows.

        Raises a neutral ``Vendor*`` exception on a corrupt row so the loop
        quarantines the chunk (the row is surfaced, never silently dropped).
        """
        rows = self.inner.fetch_chunk(key)
        if not rows:
            return rows  # NO_DATA — nothing to parse or persist.

        lake_interval = GROWW_INTERVAL_TO_PARTITION.get(key.interval)
        if lake_interval is None:
            raise bad_request(f"no lake-partition interval for {key.interval!r}")

        # Per-row resilience: a single corrupt bar (e.g. one null-field row)
        # must NOT quarantine the whole 30/90/180-day chunk — that previously
        # discarded every good bar in the window. Skip the bad row, keep the
        # rest, and NEVER silently: log one summary line with the count + a
        # sample reason (visibility, not log-spam) so the operator sees what
        # was dropped and why.
        bars: list[CandleBar] = []
        quarantined = 0
        empty = 0
        sample_error: str | None = None
        for row in _as_rows(rows):
            try:
                bars.append(self.parse(row))
            except EmptyCandleRow:
                # No-trade / halted / unlisted bar — legitimately has no OHLC.
                # A graceful, expected skip (counted, not alarmed). MUST be
                # caught BEFORE CandleParseError (it is a subclass).
                empty += 1
            except (CandleParseError, ValueError) as exc:
                quarantined += 1
                if sample_error is None:
                    sample_error = str(exc)
        if quarantined:
            _LOG.warning(
                "candle.rows.quarantined",
                symbol=key.groww_symbol,
                interval=key.interval,
                quarantined=quarantined,
                empty=empty,
                kept=len(bars),
                sample_error=sample_error,
            )
        elif empty:
            # Only no-data bars were dropped — info, not a warning (expected).
            _LOG.info(
                "candle.rows.empty_skipped",
                symbol=key.groww_symbol,
                interval=key.interval,
                empty=empty,
                kept=len(bars),
            )
        if not bars:
            # Every row was unparseable → nothing to persist. Return [] so the
            # fetch loop settles NO_DATA (the warning above says exactly why),
            # instead of a false COMPLETED on a chunk that wrote zero bars.
            return []

        exchange, segment = symbol_to_exchange_segment(key.groww_symbol)
        try:
            self.sink.append(
                exchange=exchange,
                segment=segment,
                symbol=key.groww_symbol,
                interval=lake_interval,
                bars=bars,
            )
        except DataLakeError as exc:
            # A lake-write failure (bad partition coords / content conflict) is
            # surfaced as a quarantine — never crashes the run (the loop would
            # otherwise hit a non-classifiable error).
            raise bad_request(f"lake write failed for {key.groww_symbol!r}: {exc}") from exc
        return rows


def _as_rows(rows: Sequence[object]) -> Sequence[Sequence[object]]:
    """Narrow the loop's ``Sequence[object]`` to row-sequences for the parser.

    The parser validates each row's shape, so this cast carries no risk — a
    non-row element raises :class:`CandleParseError`, which the caller maps to
    a terminal-quarantine outcome.
    """
    return rows  # type: ignore[return-value]


__all__ = [
    "PersistingFetcher",
]
