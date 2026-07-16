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
import math
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from brutex.calendars.nse import IST
from brutex.data.bad_tick import (
    BAD_TICK_MAX_RATIO,
    BadTickScreen,
    bad_tick_screen_enabled,
    emit_bad_tick_warn,
)
from brutex.data.catalog import Catalog
from brutex.data.models import CandleBar
from brutex.feed.errors import FeedError, LookAheadError
from brutex.feed_rust import (
    ScanResult,
    feed_rust_enabled,
    resolve_feed_kernel,
    validate_scan,
    warn_degrade_once,
)

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

    Validation (DEC-STORED-DATA-VALIDATION-001 +
    DEC-LAKE-READ-NONFINITE-OHLC-GUARD-001): this is the ENGINE READ
    BOUNDARY (``ReplayFeed._load_bars`` → ``Catalog.get_bars`` → Stage-1A /
    run identity), so it — NOT the general-purpose :class:`CandleBar`
    constructor — enforces price FINITENESS (no ``inf``/``-inf``/``NaN`` in
    ANY OHLC field) and POSITIVITY (every OHLC price ``> 0``) on top of the
    constructor's ordering / ``volume >= 0`` invariants. A corrupt stored bar
    (e.g. a legacy pre-#912 partition holding an internally-ordered NEGATIVE
    bar the round-2 audit reproduced as a day_low of −1000, or a planted
    ``high = inf`` bar the assurance session executed-repro'd flowing SILENTLY
    into the feed as ``Decimal('Infinity')`` — the positivity + ordering
    checks pass VACUOUSLY on ``inf``, and a NaN price previously escaped as a
    raw uncaught ``decimal.InvalidOperation``) can never silently enter the
    engine NOR crash the whole read: a non-finite price, a non-positive price
    OR an ordering violation raises a LOUD :class:`ValueError` naming *source*
    (the partition coordinates, when the caller knows them) plus the offending
    bar's timestamp — the graceful per-symbol error surface the
    ingest/prebuild quarantines already handle. The construction wrapper
    additionally catches :class:`ArithmeticError` (which covers
    ``decimal.InvalidOperation``) and re-wraps it into the SAME corrupt-bar
    :class:`ValueError`, so any residual Decimal-conversion fault rides the
    existing handling instead of aborting ``Catalog.get_bars`` uncaught.
    No production writer emits a non-finite price (the vendor parse path
    rejects it — ``candle_fields.to_price``'s explicit ``is_finite`` guard),
    so this only ever fires on planted/out-of-band corrupt stored data.
    Positivity is checked HERE rather than in the constructor because the bar
    container is legitimately used to hold a non-positive price for downstream
    corrupt-input classification (the Greeks ``BAD_INPUT`` path, the executor
    ``require_tradeable_price`` guard, the Rust trade-sim out-of-envelope
    fallback) — see :func:`brutex.data.models._validate_price_bar`. Cost stays
    O(1)/bar: the finiteness probe + four fixed comparisons per row plus the
    constructor's own; the ``try``/``except`` wrapper is zero-cost on the
    non-raising path (CPython 3.11+ exception tables).

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

    screen_on = bad_tick_screen_enabled()

    # ── OPTIONAL Rust decision kernel (DEC-FEED-RUST-CORE-001) ──────────
    # Default-OFF (`BRUTEX_FEED_RUST`, Pattern A opt-in). When enabled AND
    # the kernel serves (canonical schema, zero nulls on ts/prices/volume,
    # timestamps inside the fixed-IST envelope, output passes the
    # refuse-poison gate), the DECISIONS come from Rust while EVERY
    # materialized object — Decimal/CandleBar/error/WARN — is still built
    # HERE by the same shared helpers over the same `to_pylist()` objects,
    # so run identity (`bxsnap2`) is byte-preserved by construction. ANY
    # degrade condition falls through to the retained oracle loop below.
    if feed_rust_enabled():
        result = _scan_via_kernel_or_none(table, screen_on, opens, highs, lows, closes)
        if result is not None:
            return _bars_from_scan(
                result, source, timestamps, opens, highs, lows, closes, volumes, open_interests
            )

    # ── The retained Python oracle (byte-identical legacy path) ─────────
    # Bounded bad-tick (spike / fat-finger / vendor-scaling) screen
    # (DEC-SWEEP-DATA-EDGE-GUARDS-001): a self-consistent SCALED bar — every
    # OHLC field ×~10 but internally ordered and positive — passes the
    # positivity + ordering checks above yet would silently flip every
    # price-relative bit and (via ``TodayOHLC.absorb``) poison the NEXT day's
    # PDH/PDL/pivots. The screen SKIPS-and-COUNTS such a bar (never value-edits,
    # never fabricates) comparing each close to the previous IN-SESSION kept
    # close; the first bar of each session is exempt (no reopen-gap false trip).
    # Default ON, ``BRUTEX_BAD_TICK_SCREEN`` kill switch. O(1)/bar.
    screen = BadTickScreen(source=source) if screen_on else None

    origin = f" in {source}" if source is not None else ""
    bars: list[CandleBar] = []
    for ts, o, h, lo, c, v, oi in zip(
        timestamps, opens, highs, lows, closes, volumes, open_interests, strict=True
    ):
        # Engine-boundary FINITENESS + POSITIVITY (DEC-STORED-DATA-VALIDATION-001
        # + DEC-LAKE-READ-NONFINITE-OHLC-GUARD-001): checked BEFORE construction
        # so an internally-ORDERED all-negative bar (which the ordering-only
        # constructor would accept), a ``high = inf`` bar (which the ordering +
        # positivity checks accept VACUOUSLY — inf compares fine) and a NaN bar
        # (whose Decimal comparison would raise a raw ``decimal.InvalidOperation``
        # past ``except ValueError``) are all refused with the real defect
        # named, never laundered into Stage-1A / run identity and never an
        # uncaught crash of the whole read. The Rust kernel arm never sees a
        # non-finite price (the eligibility gate degrades to THIS loop), so
        # the two arms cannot fork on this class.
        for price_name, price in (("open", o), ("high", h), ("low", lo), ("close", c)):
            if not math.isfinite(price):
                raise _nonfinite_error(origin, ts, price_name, price)
            if price <= 0:
                raise _positivity_error(origin, ts, price_name, price)
        # Bad-tick screen (post-positivity, pre-construction): a scaled spike is
        # DROPPED (not built, not appended) and counted; the last kept good close
        # carries forward as the comparison baseline. IST session date drives the
        # per-session grouping (``ts`` is timezone-aware — the constructor rejects
        # a naive datetime, so ``.astimezone`` is safe).
        if screen is not None and not screen.keep(ist_date=ts.astimezone(IST).date(), close=c):
            continue
        bars.append(_build_bar(origin, ts, o, h, lo, c, v, oi))
    if screen is not None:
        screen.finish()
    return bars


def _nonfinite_error(origin: str, ts: Any, price_name: str, price: float) -> ValueError:
    """Build the engine-boundary non-finite refusal (the ONE message source).

    DEC-LAKE-READ-NONFINITE-OHLC-GUARD-001: the finiteness twin of
    :func:`_positivity_error` — an ``inf``/``-inf``/``NaN`` price passed the
    positivity + ordering checks VACUOUSLY pre-fix (an inf-HIGH bar was
    served SILENTLY as ``Decimal('Infinity')``; a NaN escaped as a raw
    uncaught ``decimal.InvalidOperation``). No production writer emits a
    non-finite price (``candle_fields.to_price`` rejects it at the vendor
    parse), so this only ever fires on planted/out-of-band corrupt data.
    The Rust kernel arm never reaches this error: a non-finite price column
    fails the kernel's eligibility gate and degrades to the oracle loop.
    """
    return ValueError(
        f"corrupt stored bar{origin} at timestamp {ts!r}: "
        f"{price_name}={price} is a non-finite price (the vendor parse "
        f"rejects inf/NaN — a stored non-finite price is planted/corrupt "
        f"data) — refusing to serve corrupt data to the engine "
        f"(DEC-LAKE-READ-NONFINITE-OHLC-GUARD-001)"
    )


def _positivity_error(origin: str, ts: Any, price_name: str, price: float) -> ValueError:
    """Build the engine-boundary positivity refusal (the ONE message source).

    MOVED VERBATIM from the loop body (DEC-STORED-DATA-VALIDATION-001 wording
    byte-untouched) so the oracle loop and the Rust-kernel replay raise the
    IDENTICAL error from one f-string (CLAUDE.md §11 COMMON).
    """
    return ValueError(
        f"corrupt stored bar{origin} at timestamp {ts!r}: "
        f"{price_name}={price} is a non-positive price (a traded Indian "
        f"price is never <= 0) — refusing to serve corrupt data to the "
        f"engine (DEC-STORED-DATA-VALIDATION-001)"
    )


def _build_bar(
    origin: str,
    ts: Any,
    o: float,
    h: float,
    lo: float,
    c: float,
    v: int,
    oi: int | None,
) -> CandleBar:
    """Materialize ONE ``CandleBar`` + the corrupt-bar ``ValueError`` wrap.

    MOVED VERBATIM from the loop body: the ``Decimal(str(price))`` re-wrap
    and the wrapped constructor-refusal message are the run-identity-critical
    expressions (`bxsnap2` streams ``str(Decimal)``), kept in ONE place so
    the oracle loop and the Rust-kernel replay can never fork. Catches
    ``(ValueError, ArithmeticError)`` (DEC-LAKE-READ-NONFINITE-OHLC-GUARD-001
    — ``ArithmeticError`` covers ``decimal.InvalidOperation`` from a
    NaN/junk Decimal conversion or ordering compare, and ``OverflowError``
    from ``int(inf)`` on a float-typed count column; pre-fix those escaped
    ``except ValueError`` and aborted the WHOLE ``Catalog.get_bars`` read
    uncaught — now re-wrapped into the SAME corrupt-bar ValueError on BOTH
    arms). The oracle's ``TypeError`` escape on a null price/volume stays an
    escape (pre-existing, out of scope — the lake writers never render null
    prices; the frame gates' ``is_null`` arm owns that class).
    """
    try:
        return CandleBar(
            timestamp=ts,
            open=Decimal(str(o)),
            high=Decimal(str(h)),
            low=Decimal(str(lo)),
            close=Decimal(str(c)),
            volume=int(v),
            open_interest=int(oi) if oi is not None else None,
        )
    except (ValueError, ArithmeticError) as exc:
        raise ValueError(
            f"corrupt stored bar{origin} at timestamp {ts!r}: {exc} — "
            f"refusing to serve corrupt data to the engine "
            f"(DEC-STORED-DATA-VALIDATION-001)"
        ) from exc


def _scan_via_kernel_or_none(
    table: pa.Table,
    screen_on: bool,
    opens: list[float],
    highs: list[float],
    lows: list[float],
    closes: list[float],
) -> ScanResult | None:
    """Consult the Rust decision kernel; ``None`` = degrade to the oracle.

    The eligibility allow-list (DEC-FEED-RUST-CORE-001 §5): the wheel must
    resolve; the 7 canonical columns must carry EXACTLY the canonical types
    (``timestamp[us, tz=UTC]`` + four ``float64`` + two ``int64`` — extra
    columns are fine, ignored exactly as today; a MISSING column never
    reaches this gate — the shared ``to_pylist()`` extractions upstream
    raise pyarrow's own ``KeyError`` at the identical site in both arms);
    timestamp/price/volume columns must be null-free (``open_interest``
    nulls are legal and never cross the FFI). Each refusal emits one named
    warn-once and returns ``None`` — the caller falls through to the
    byte-identical oracle loop.
    """
    kernel = resolve_feed_kernel()
    if kernel is None:
        return None
    import pyarrow as pa  # local: module-scope pyarrow stays TYPE_CHECKING-only

    n_rows: int = table.num_rows
    schema = table.schema
    expected_types = (
        ("timestamp", pa.timestamp("us", tz="UTC")),
        ("open", pa.float64()),
        ("high", pa.float64()),
        ("low", pa.float64()),
        ("close", pa.float64()),
        ("volume", pa.int64()),
        ("open_interest", pa.int64()),
    )
    for name, expected in expected_types:
        if not schema.field(name).type.equals(expected):
            warn_degrade_once("schema_mismatch", n_rows=n_rows, column=name)
            return None
    for name in ("timestamp", "open", "high", "low", "close", "volume"):
        if table.column(name).null_count > 0:
            warn_degrade_once("null_column", n_rows=n_rows, column=name)
            return None
    # DEC-LAKE-READ-NONFINITE-OHLC-GUARD-001: the kernel's decision plane is
    # a bit-identity port of the PRE-guard checks and is BLIND to inf/NaN
    # (positivity holds vacuously; an inf close would fold into the bad-tick
    # ratio as a silent DROP instead of the oracle's typed refusal) — so a
    # non-finite price column DEGRADES to the oracle loop, whose finiteness
    # gate raises the loud corrupt-bar error. The probe runs over the SAME
    # already-materialized float lists the oracle checks (short-circuiting
    # ``all``; one O(rows) pass per price column, kernel-path only —
    # disclosed: the null gate above is metadata-only, this one genuinely
    # scans).
    for name, column in (("open", opens), ("high", highs), ("low", lows), ("close", closes)):
        if not all(map(math.isfinite, column)):
            warn_degrade_once("nonfinite_price_column", n_rows=n_rows, column=name)
            return None
    # Epoch-µs UTC ints for the kernel's IST session math — ONLY when the
    # screen is on (the oracle never touches timestamps screen-off; the
    # kernel contract demands [] then).
    ts_us: list[int] = table.column("timestamp").cast(pa.int64()).to_pylist() if screen_on else []
    try:
        result = kernel(ts_us, opens, highs, lows, closes, screen_on, BAD_TICK_MAX_RATIO)
    except Exception as exc:  # the defensive belt: ANY kernel raise degrades to the oracle
        warn_degrade_once("kernel_error", n_rows=n_rows, error=repr(exc))
        return None
    if result is None:
        # The kernel abstained: some timestamp lies outside the fixed-IST
        # envelope [epoch 0, 9999-12-31T00:00:00Z) where the day-ordinal law
        # is provably identical to `astimezone(IST).date()`.
        warn_degrade_once("ts_out_of_ist_fixed_range", n_rows=n_rows)
        return None
    if not validate_scan(result, n_rows):
        return None
    return result


def _emit_warn(
    event: tuple[int, int, int, int, int],
    source: str | None,
    timestamps: list[Any],
    closes: list[float],
) -> None:
    """Replay ONE kernel warn event through the shared bad-tick WARN seam.

    The kernel returns ROW COORDINATES only; the session ISO string comes
    from Python's OWN datetime object (the REAL ``ZoneInfo`` conversion) and
    the offender closes are the SAME float objects the oracle's screen would
    have captured — so the WARN record is byte-identical by construction.
    """
    _, session_first_row, skipped, first_prev_row, first_skip_row = event
    emit_bad_tick_warn(
        source=source,
        session_iso=timestamps[session_first_row].astimezone(IST).date().isoformat(),
        skipped=skipped,
        max_ratio=BAD_TICK_MAX_RATIO,
        first_prev_close=closes[first_prev_row],
        first_close=closes[first_skip_row],
    )


def _bars_from_scan(
    result: ScanResult,
    source: str | None,
    timestamps: list[Any],
    opens: list[float],
    highs: list[float],
    lows: list[float],
    closes: list[float],
    volumes: list[int],
    open_interests: list[int | None],
) -> list[CandleBar]:
    """Materialize kernel decisions with the ORACLE's exact side-effect order.

    The replay law (DEC-FEED-RUST-CORE-001 §6): warn anchors are always kept
    rows (a session's first row is always kept) or ``n_rows`` (the finish
    flush), so draining every event with ``emit_at_row <= i`` before
    building bar *i* emits each boundary WARN exactly where the oracle's
    ``keep()`` would — after row *i*'s positivity, before row *i*'s
    construction. A constructor raise at kept row *j* leaves later warns
    un-emitted (the exception propagates first — identical to the oracle's
    mid-loop raise); a positivity stop was truncated KERNEL-side (no event
    at/after the stop row, no finish event), so raising after the kept loop
    reproduces the oracle's log/raise ordering exactly.
    """
    kept, warns, stop = result
    origin = f" in {source}" if source is not None else ""
    bars: list[CandleBar] = []
    wi = 0
    for i in kept:
        while wi < len(warns) and warns[wi][0] <= i:
            _emit_warn(warns[wi], source, timestamps, closes)
            wi += 1
        bars.append(
            _build_bar(
                origin,
                timestamps[i],
                opens[i],
                highs[i],
                lows[i],
                closes[i],
                volumes[i],
                open_interests[i],
            )
        )
    if stop is not None:
        row, field = stop
        price_name = ("open", "high", "low", "close")[field]
        price = (opens, highs, lows, closes)[field][row]
        raise _positivity_error(origin, timestamps[row], price_name, price)
    while wi < len(warns):
        _emit_warn(warns[wi], source, timestamps, closes)
        wi += 1
    return bars


__all__ = [
    "FeedEvent",
    "Pacer",
    "ReplayFeed",
    "arrow_table_to_bars",
]
