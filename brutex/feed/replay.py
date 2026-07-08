"""Chronological multi-symbol replay feed over the data lake.

Implements ``FR-FWD-001``: a :class:`ReplayFeed` streams bars from the
BruteX data lake in market order, interleaving multiple symbols so a
strategy sees events exactly as they happened.

## Contract

``ReplayFeed`` takes a :class:`~brutex.data.catalog.Catalog` plus the
usual partition coordinates and a requested universe (``symbols``).
Iterating yields :class:`FeedEvent` instances — each a ``(symbol,
bar)`` pair — in ascending ``bar.timestamp`` order, breaking ties
deterministically by the position of the symbol in the ``symbols``
argument (matches :func:`heapq.merge`'s stable-merge behavior).

### Look-ahead guard (``FR-INT-004``)

After each ``yield``, :attr:`ReplayFeed.current_ts` is updated to the
bar's timestamp. A strategy that wants to verify it has not peeked at
future data can call :meth:`ReplayFeed.ensure_no_lookahead(ts)` and
get a :class:`~brutex.feed.errors.LookAheadError` if ``ts`` is
strictly after ``current_ts``.

### Pacing

A :class:`ReplayFeed` yields bars instantly by default (accelerated
replay — the only mode backtests need). Callers that want wall-clock
pacing can pass a ``pacer`` callback; on each yielded bar after the
first, the pacer receives the :class:`datetime.timedelta` gap from the
previous bar and is expected to block for that duration. The default
``None`` bypasses this entirely — which is what every unit test uses.

## Scope — v1

* One interval per feed. Multi-interval feeds compose by running
  several ``ReplayFeed``s and merging their events externally, or by
  running one feed at the base interval and feeding it through
  :func:`~brutex.feed.resampler.resample_bars` before indicator
  consumption.
* Session-naive UTC bucketing. Session-aware replay lands with the
  trading-calendar module (P3, ADR-0014).
* The Arrow → :class:`CandleBar` conversion is done eagerly once per
  ``__iter__`` call. For very large windows the caller should
  materialise on demand by slicing the window into chunks.
"""

from __future__ import annotations

import heapq
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from brutex.calendars.nse import IST
from brutex.data.bad_tick import BadTickScreen, bad_tick_screen_enabled
from brutex.data.catalog import Catalog
from brutex.data.models import CandleBar
from brutex.feed.errors import FeedError, LookAheadError

if TYPE_CHECKING:  # pragma: no cover
    import pyarrow as pa


#: Type alias for the pacing callback. Accepts a
#: :class:`~datetime.timedelta` "gap since last bar" and is expected
#: to block for that duration (real-time pacing) or subsample it
#: (accelerated pacing). ``None`` bypasses pacing entirely.
Pacer = Callable[[timedelta], None]


# ────────────────────────────────────────────────────────────────────────
# FeedEvent
# ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class FeedEvent:
    """One bar yielded by a feed, paired with its symbol context.

    Attributes
    ----------
    symbol
        Canonical (vendor) symbol the bar belongs to, e.g.
        ``"NSE-NIFTY-02Jan25-28500-PE"``.
    bar
        The underlying :class:`~brutex.data.models.CandleBar`.
    """

    symbol: str
    bar: CandleBar

    @property
    def timestamp(self) -> datetime:
        """Bar's timezone-aware UTC timestamp. Shorthand for ``self.bar.timestamp``."""
        return self.bar.timestamp


# ────────────────────────────────────────────────────────────────────────
# ReplayFeed
# ────────────────────────────────────────────────────────────────────────


class ReplayFeed:
    """Chronological multi-symbol bar stream from the data lake.

    Parameters
    ----------
    catalog
        Open :class:`~brutex.data.catalog.Catalog` instance. The feed
        does NOT take ownership — it is the caller's job to close
        the catalog after iteration.
    lake_root
        Top-level lake directory. Passed through to
        :meth:`~brutex.data.catalog.Catalog.get_bars`.
    exchange, segment, interval
        Shared partition coordinates for every symbol in *symbols*.
    symbols
        Ordered sequence of canonical symbols to interleave. Order
        matters only for tie-breaking when two bars share a
        timestamp — :func:`heapq.merge` preserves the input order.
    start, end
        Inclusive replay window. Both must be timezone-aware.
    pacer
        Optional :data:`Pacer` callable invoked with
        ``bar.timestamp - previous_ts`` on every yielded bar after
        the first. ``None`` (the default) means instant replay.

    Raises
    ------
    FeedError
        If *symbols* is empty or if the Arrow → :class:`CandleBar`
        conversion encounters a non-UTC timestamp.
    """

    def __init__(
        self,
        *,
        catalog: Catalog,
        lake_root: Path,
        exchange: str,
        segment: str,
        symbols: Sequence[str],
        interval: str,
        start: datetime,
        end: datetime,
        pacer: Pacer | None = None,
    ) -> None:
        if not symbols:
            raise FeedError("ReplayFeed requires at least one symbol")
        if start.tzinfo is None or end.tzinfo is None:
            raise FeedError("ReplayFeed: start and end must be timezone-aware")
        if end < start:
            raise FeedError(f"ReplayFeed: end ({end}) must be >= start ({start})")

        self._catalog = catalog
        self._lake_root = lake_root
        self._exchange = exchange
        self._segment = segment
        self._symbols = list(symbols)
        self._interval = interval
        self._start = start
        self._end = end
        self._pacer = pacer
        self._current_ts: datetime | None = None

    # ── Public API ────────────────────────────────────────────────

    @property
    def current_ts(self) -> datetime | None:
        """Timestamp of the most-recently-yielded bar, or ``None`` before iteration."""
        return self._current_ts

    @property
    def symbols(self) -> tuple[str, ...]:
        """Return the ordered symbol tuple this feed was configured with."""
        return tuple(self._symbols)

    @property
    def interval(self) -> str:
        """Return the canonical interval name this feed streams bars at."""
        return self._interval

    def ensure_no_lookahead(self, ts: datetime) -> None:
        """Assert that *ts* is not strictly after the feed's current cursor.

        Implements the runtime half of ``FR-INT-004``. Callers should
        invoke this before any data read whose timestamp is derived
        from something other than :attr:`current_ts` itself — for
        example an indicator cache lookup.

        Raises
        ------
        LookAheadError
            If the feed has not yet yielded any bars, or if
            ``ts > current_ts``.
        """
        if ts.tzinfo is None:
            raise LookAheadError(f"ensure_no_lookahead requires tz-aware ts, got naive {ts!r}")
        if self._current_ts is None:
            raise LookAheadError(
                f"feed has not yielded any bars yet; any read at ts={ts!r} is a look-ahead"
            )
        if ts > self._current_ts:
            raise LookAheadError(
                f"ts {ts!r} > current_ts {self._current_ts!r}: "
                f"look-ahead read forbidden (FR-INT-004)"
            )

    def __iter__(self) -> Iterator[FeedEvent]:
        """Yield :class:`FeedEvent` values in ascending timestamp order.

        Per-symbol event lists are materialised once via
        :meth:`Catalog.get_bars` at the start of iteration, then
        merged with :func:`heapq.merge` so ties break deterministically
        in the order *symbols* were passed to ``__init__``.
        """
        per_symbol_events = [
            [FeedEvent(symbol=symbol, bar=bar) for bar in self._load_bars(symbol)]
            for symbol in self._symbols
        ]

        previous_ts: datetime | None = None
        for event in heapq.merge(*per_symbol_events, key=lambda e: e.bar.timestamp):
            if self._pacer is not None and previous_ts is not None:
                self._pacer(event.bar.timestamp - previous_ts)
            self._current_ts = event.bar.timestamp
            previous_ts = event.bar.timestamp
            yield event

    # ── Private ────────────────────────────────────────────────────

    def _load_bars(self, symbol: str) -> list[CandleBar]:
        """Fetch bars for *symbol* via :meth:`Catalog.get_bars` and
        convert the resulting Arrow table into a
        :class:`CandleBar` list.

        This is where the ``[data]`` optional dependency becomes
        load-bearing — :meth:`Catalog.get_bars` itself lazy-imports
        ``polars`` / ``pyarrow`` and raises
        :class:`~brutex.data.errors.CatalogError` if they are
        missing, so nothing extra is needed here.
        """
        table = self._catalog.get_bars(
            lake_root=self._lake_root,
            exchange=self._exchange,
            segment=self._segment,
            symbol=symbol,
            interval=self._interval,
            start=self._start,
            end=self._end,
        )
        return arrow_table_to_bars(
            table,
            source=f"{self._exchange}/{self._segment}/{symbol}/{self._interval}",
        )


# ────────────────────────────────────────────────────────────────────────
# Arrow → CandleBar
# ────────────────────────────────────────────────────────────────────────


def arrow_table_to_bars(table: pa.Table, *, source: str | None = None) -> list[CandleBar]:
    """Materialise a ``pyarrow.Table`` of canonical bars into ``list[CandleBar]``.

    The table must have the canonical 7-column schema emitted by
    :class:`~brutex.data.parquet_writer.ParquetWriter` /
    :meth:`~brutex.data.catalog.Catalog.get_bars`. Float prices are
    re-wrapped as :class:`~decimal.Decimal` via ``str()`` so the
    in-memory bars round-trip bit-for-bit against the Parquet source.

    Public (promoted from the former private ``_arrow_table_to_bars`` per
    CLAUDE.md §11 COMMON, ``DEC-MOVERS-DASHBOARD-PAGE-001``): this is the single
    source of truth for the Arrow → :class:`CandleBar` conversion, reused by the
    lake-reading screener paths — no copy-paste of the conversion.

    Validation (DEC-STORED-DATA-VALIDATION-001): this is the ENGINE READ
    BOUNDARY (``ReplayFeed._load_bars`` → ``Catalog.get_bars`` → Stage-1A /
    run identity), so it — NOT the general-purpose :class:`CandleBar`
    constructor — enforces price POSITIVITY (every OHLC price ``> 0``) on top
    of the constructor's ordering / ``volume >= 0`` invariants. A corrupt
    stored bar (e.g. a legacy pre-#912 partition holding an internally-ordered
    NEGATIVE bar the round-2 audit reproduced as a day_low of −1000) can never
    silently enter the engine: a non-positive price OR an ordering violation
    raises a LOUD :class:`ValueError` naming *source* (the partition
    coordinates, when the caller knows them) plus the offending bar's
    timestamp — the graceful per-symbol error surface the ingest/prebuild
    quarantines already handle. Positivity is checked HERE rather than in the
    constructor because the bar container is legitimately used to hold a
    non-positive price for downstream corrupt-input classification (the Greeks
    ``BAD_INPUT`` path, the executor ``require_tradeable_price`` guard, the
    Rust trade-sim out-of-envelope fallback) — see
    :func:`brutex.data.models._validate_price_bar`. Cost stays O(1)/bar: four
    fixed comparisons per row plus the constructor's own; the ``try``/``except``
    wrapper is zero-cost on the non-raising path (CPython 3.11+ exception tables).

    Parameters
    ----------
    table
        The canonical 7-column bar table.
    source
        Optional human-readable origin (e.g.
        ``"NSE/INDEX/NSE-NIFTY/1minute"``) woven into the corrupt-bar error
        so the operator can locate the poisoned partition. ``None`` keeps
        the bare per-bar message.
    """
    if table.num_rows == 0:
        return []

    timestamps: list[Any] = table.column("timestamp").to_pylist()
    opens: list[float] = table.column("open").to_pylist()
    highs: list[float] = table.column("high").to_pylist()
    lows: list[float] = table.column("low").to_pylist()
    closes: list[float] = table.column("close").to_pylist()
    volumes: list[int] = table.column("volume").to_pylist()
    open_interests: list[int | None] = table.column("open_interest").to_pylist()

    # Bounded bad-tick (spike / fat-finger / vendor-scaling) screen
    # (DEC-SWEEP-DATA-EDGE-GUARDS-001): a self-consistent SCALED bar — every
    # OHLC field ×~10 but internally ordered and positive — passes the
    # positivity + ordering checks above yet would silently flip every
    # price-relative bit and (via ``TodayOHLC.absorb``) poison the NEXT day's
    # PDH/PDL/pivots. The screen SKIPS-and-COUNTS such a bar (never value-edits,
    # never fabricates) comparing each close to the previous IN-SESSION kept
    # close; the first bar of each session is exempt (no reopen-gap false trip).
    # Default ON, ``BRUTEX_BAD_TICK_SCREEN`` kill switch. O(1)/bar.
    screen = BadTickScreen(source=source) if bad_tick_screen_enabled() else None

    bars: list[CandleBar] = []
    for ts, o, h, lo, c, v, oi in zip(
        timestamps, opens, highs, lows, closes, volumes, open_interests, strict=True
    ):
        origin = f" in {source}" if source is not None else ""
        # Engine-boundary POSITIVITY (DEC-STORED-DATA-VALIDATION-001): checked
        # BEFORE construction so an internally-ORDERED all-negative bar (which
        # the ordering-only constructor would accept) is refused with the real
        # defect named, never laundered into Stage-1A / run identity.
        for price_name, price in (("open", o), ("high", h), ("low", lo), ("close", c)):
            if price <= 0:
                raise ValueError(
                    f"corrupt stored bar{origin} at timestamp {ts!r}: "
                    f"{price_name}={price} is a non-positive price (a traded Indian "
                    f"price is never <= 0) — refusing to serve corrupt data to the "
                    f"engine (DEC-STORED-DATA-VALIDATION-001)"
                )
        # Bad-tick screen (post-positivity, pre-construction): a scaled spike is
        # DROPPED (not built, not appended) and counted; the last kept good close
        # carries forward as the comparison baseline. IST session date drives the
        # per-session grouping (``ts`` is timezone-aware — the constructor rejects
        # a naive datetime, so ``.astimezone`` is safe).
        if screen is not None and not screen.keep(ist_date=ts.astimezone(IST).date(), close=c):
            continue
        try:
            bars.append(
                CandleBar(
                    timestamp=ts,
                    open=Decimal(str(o)),
                    high=Decimal(str(h)),
                    low=Decimal(str(lo)),
                    close=Decimal(str(c)),
                    volume=int(v),
                    open_interest=int(oi) if oi is not None else None,
                )
            )
        except ValueError as exc:
            raise ValueError(
                f"corrupt stored bar{origin} at timestamp {ts!r}: {exc} — "
                f"refusing to serve corrupt data to the engine "
                f"(DEC-STORED-DATA-VALIDATION-001)"
            ) from exc
    if screen is not None:
        screen.finish()
    return bars


__all__ = [
    "FeedEvent",
    "Pacer",
    "ReplayFeed",
    "arrow_table_to_bars",
]
