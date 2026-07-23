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
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from brutex.calendars.nse import IST
from brutex.data.bad_tick import (
    BAD_TICK_MAX_RATIO,
    bad_tick_screen_enabled,
    emit_bad_tick_warn,
)
from brutex.data.catalog import Catalog
from brutex.data.models import CandleBar
from brutex.feed.errors import FeedError, LookAheadError
from brutex.feed_rust import (
    MATERIALIZE_DECISION,
    STOP_NONFINITE,
    StrictScanResult,
    require_feed_kernel,
    validate_strict_scan,
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
    Positivity is checked at this boundary rather than in the constructor
    because the bar container is legitimately used to hold a non-positive
    price for downstream corrupt-input classification (the Greeks
    ``BAD_INPUT`` path, the executor ``require_tradeable_price`` guard, the
    Rust trade-sim out-of-envelope fallback) — see
    :func:`brutex.data.models._validate_price_bar`. Cost stays O(1)/bar in
    the kernel scan; the ``try``/``except`` materialization wrapper is
    zero-cost on the non-raising path (CPython 3.11+ exception tables).

    DEC-FEED-RUST-MATERIALIZE-002 (the T17 zero-Python endgame): the
    DECISION plane — the finiteness/positivity refusal order AND the
    bad-tick screen — runs EXCLUSIVELY in the ``brutex_feed`` Rust wheel
    (``scan_bars_strict``); the retained Python loop was DELETED, so the
    wheel is REQUIRED (a missing wheel raises a typed
    :class:`~brutex.feed.errors.FeedError` naming the rebuild remediation
    — see :func:`~brutex.feed_rust.require_feed_kernel`). Python keeps
    ONLY materialization over the ``to_pylist()`` objects, so the served
    bytes (``bxsnap2`` run identity, error messages, WARN records) are the
    deleted oracle's bytes by construction — pinned by the frozen goldens
    + the retained-in-test frozen-oracle differential
    (``tests/property/test_feed_rust_parity.py``).

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

    # ── The wheel-REQUIRED strict path (DEC-FEED-RUST-MATERIALIZE-002) ──
    # Decisions ALWAYS come from the strict Rust scan (finiteness +
    # positivity + the bad-tick screen in ONE kernel pass); Python keeps
    # ONLY materialization (Decimal/CandleBar/error/WARN over the same
    # `to_pylist()` objects — run identity byte-preserved by
    # construction). No degrade exists: a missing wheel, a set-falsy/junk
    # retired flag, an out-of-envelope timestamp, a null consulted column
    # or a poisoned kernel result each raise LOUDLY (correct-or-absent,
    # never a silently wrong bar).
    return _bars_via_strict_kernel(
        table,
        source,
        screen_on,
        timestamps,
        opens,
        highs,
        lows,
        closes,
        volumes,
        open_interests,
    )


def _nonfinite_error(origin: str, ts: Any, price_name: str, price: float) -> ValueError:
    """Build the engine-boundary non-finite refusal (the ONE message source).

    DEC-LAKE-READ-NONFINITE-OHLC-GUARD-001: the finiteness twin of
    :func:`_positivity_error` — an ``inf``/``-inf``/``NaN`` price passed the
    positivity + ordering checks VACUOUSLY pre-fix (an inf-HIGH bar was
    served SILENTLY as ``Decimal('Infinity')``; a NaN escaped as a raw
    uncaught ``decimal.InvalidOperation``). No production writer emits a
    non-finite price (``candle_fields.to_price`` rejects it at the vendor
    parse), so this only ever fires on planted/out-of-band corrupt data.
    Raised from the strict kernel's ``STOP_NONFINITE`` coordinate
    (DEC-FEED-RUST-MATERIALIZE-002) over Python's OWN float object.
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

    The DEC-STORED-DATA-VALIDATION-001 wording, byte-untouched — raised
    from the strict kernel's ``STOP_NONPOSITIVE`` coordinate over Python's
    OWN float object (CLAUDE.md §11 COMMON: one f-string source).
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

    The ``Decimal(str(price))`` re-wrap and the wrapped constructor-refusal
    message are the run-identity-critical expressions (`bxsnap2` streams
    ``str(Decimal)``), kept in ONE place (CLAUDE.md §11 COMMON). Catches
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


# ── The wheel-REQUIRED strict path (DEC-FEED-RUST-MATERIALIZE-002) ──────


#: The columns whose values the read consumes as non-null (open_interest
#: nulls are legal — non-FNO bars carry them by contract).
_CONSULTED_COLUMNS: tuple[str, ...] = ("timestamp", "open", "high", "low", "close", "volume")

#: Exact epoch anchors for the microsecond conversion of NON-canonical
#: timestamp objects (integer arithmetic — no float ever enters).
_EPOCH_UTC = datetime(1970, 1, 1, tzinfo=UTC)
_EPOCH_NAIVE = datetime(1970, 1, 1)
_ONE_US = timedelta(microseconds=1)


def _corrupt_column_error(source: str | None, column: str, detail: str) -> ValueError:
    """The typed corrupt-COLUMN refusal of the wheel-required arm.

    DEC-FEED-RUST-MATERIALIZE-002: with the Python oracle loop deleted,
    a null / junk-typed consulted column becomes ONE typed corrupt-data
    ``ValueError`` naming the column + partition (the same graceful
    per-symbol error surface the ingest/prebuild quarantines already
    handle) — SUPERSEDING the deleted oracle's accidental raw
    ``TypeError``/``AttributeError`` escapes on such columns (documented
    pre-existing escapes, never a contract; the lake writers never render
    them — only planted/out-of-band data reaches this).
    """
    origin = f" in {source}" if source is not None else ""
    return ValueError(
        f"corrupt stored bar column{origin}: {column!r} {detail} — "
        f"refusing to serve corrupt data to the engine ({MATERIALIZE_DECISION})"
    )


def _decision_floats(source: str | None, column: str, values: list[Any]) -> list[float]:
    """f64 DECISION copies of a NON-canonical price column.

    Materialization always reads the ORIGINAL ``to_pylist()`` objects (so
    e.g. an int-typed price column still materializes ``Decimal(str(5))``
    — the deleted oracle's exact bytes); only the KERNEL's decision input
    is float-cast here. HONEST residue (ledgered): an integer/Decimal
    price beyond f64's exact range (>= 2**53) rounds in the DECISION
    plane where the oracle compared exactly — a hostile-input-only class
    (the lake writers render float64), disclosed, never silent for the
    served values themselves.
    """
    try:
        return [float(v) for v in values]
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise _corrupt_column_error(
            source, column, f"holds a non-numeric price value ({exc})"
        ) from exc


def _decision_ts_us(source: str | None, timestamps: list[Any]) -> list[int]:
    """Exact epoch-µs DECISION copies of a NON-canonical timestamp column.

    tz-aware objects convert exactly (integer arithmetic against the UTC
    epoch); a NAIVE timestamp is treated as-if-UTC for the DECISION plane
    only — observationally inert, because the first kept row's
    ``CandleBar`` constructor rejects the naive object with the same
    wrapped corrupt-bar ``ValueError`` before any decision output becomes
    visible (no warn can anchor before the first kept row).
    """
    out: list[int] = []
    for ts in timestamps:
        if not isinstance(ts, datetime):
            raise _corrupt_column_error(
                source, "timestamp", f"holds a non-timestamp value ({ts!r})"
            )
        epoch = _EPOCH_NAIVE if ts.tzinfo is None else _EPOCH_UTC
        out.append((ts - epoch) // _ONE_US)
    return out


def _bars_via_strict_kernel(
    table: pa.Table,
    source: str | None,
    screen_on: bool,
    timestamps: list[Any],
    opens: list[float],
    highs: list[float],
    lows: list[float],
    closes: list[float],
    volumes: list[int],
    open_interests: list[int | None],
) -> list[CandleBar]:
    """The unified wheel-REQUIRED read: strict Rust decisions, Python
    materialization (DEC-FEED-RUST-MATERIALIZE-002).

    Refusal lattice (each arm LOUD, never a degrade):

    * wheel missing/stale → :class:`FeedError` naming the rebuild remedy
      (:func:`~brutex.feed_rust.require_feed_kernel`);
    * a null consulted column → the typed corrupt-column ``ValueError``;
    * a junk-typed decision column → the typed corrupt-column
      ``ValueError``;
    * a consulted timestamp outside the fixed-IST envelope
      (``[epoch 0, 9999-12-31T00:00:00Z)`` — where the kernel's +05:30
      day-ordinal law provably equals ``astimezone(IST).date()``) →
      :class:`FeedError` naming the envelope (real lake bars are
      post-2020 by the ``GROWW_HISTORY_FLOOR`` law; only
      planted/out-of-band data can reach this);
    * a kernel raise → :class:`FeedError` (``MemoryError`` /
      ``KeyboardInterrupt`` / ``SystemExit`` re-raise RAW);
    * a structurally poisoned kernel result → :class:`FeedError`
      (a stale wheel can only refuse, never serve).

    Decisions on the CANONICAL 7-column schema consume the SAME float
    lists Python materializes from; a NON-canonical (hostile/legacy)
    table gets f64/µs decision COPIES while materialization keeps the
    original objects — the deleted oracle's output bytes by construction.
    """
    kernel = require_feed_kernel()
    n_rows: int = table.num_rows
    import pyarrow as pa  # local: module-scope pyarrow stays TYPE_CHECKING-only

    for name in _CONSULTED_COLUMNS:
        if table.column(name).null_count > 0:
            raise _corrupt_column_error(source, name, "holds null value(s)")

    expected_types = (
        ("timestamp", pa.timestamp("us", tz="UTC")),
        ("open", pa.float64()),
        ("high", pa.float64()),
        ("low", pa.float64()),
        ("close", pa.float64()),
    )
    canonical = all(
        table.schema.field(name).type.equals(expected) for name, expected in expected_types
    )
    if canonical:
        d_opens, d_highs, d_lows, d_closes = opens, highs, lows, closes
        ts_us: list[int] = (
            table.column("timestamp").cast(pa.int64()).to_pylist() if screen_on else []
        )
    else:
        d_opens = _decision_floats(source, "open", opens)
        d_highs = _decision_floats(source, "high", highs)
        d_lows = _decision_floats(source, "low", lows)
        d_closes = _decision_floats(source, "close", closes)
        ts_us = _decision_ts_us(source, timestamps) if screen_on else []

    try:
        result: StrictScanResult | None = kernel.scan_bars_strict(  # type: ignore[attr-defined]
            ts_us, d_opens, d_highs, d_lows, d_closes, screen_on, BAD_TICK_MAX_RATIO
        )
    except (MemoryError, KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        raise FeedError(
            f"the brutex_feed strict scan kernel raised {exc!r} over "
            f"{n_rows} row(s){' of ' + source if source is not None else ''} — "
            f"a kernel fault is a defect, never a silent degrade "
            f"({MATERIALIZE_DECISION})"
        ) from exc
    if result is None:
        raise FeedError(
            f"a stored timestamp{' in ' + source if source is not None else ''} "
            f"lies outside the fixed-IST envelope [epoch 0, 9999-12-31T00:00:00Z) "
            f"where the kernel's +05:30 civil-date law is provable — refusing "
            f"to serve a possibly-wrong session grouping (real lake bars are "
            f"post-2020; this is planted/out-of-band data — "
            f"{MATERIALIZE_DECISION})"
        )
    poison = validate_strict_scan(result, n_rows)
    if poison is not None:
        raise FeedError(
            f"the brutex_feed wheel returned a structurally invalid scan over "
            f"{n_rows} row(s) (violated law: {poison}) — a poisoned/stale "
            f"wheel can only refuse, never serve; rebuild it: run "
            f"`maturin develop --release` inside rust/brutex-feed "
            f"({MATERIALIZE_DECISION})"
        )
    return _bars_from_scan(
        result, source, timestamps, opens, highs, lows, closes, volumes, open_interests
    )


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
    result: StrictScanResult,
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
        # The strict stop is ``(row, field, kind)`` — a non-finite kind
        # raises the finiteness refusal, non-positive the positivity
        # refusal, both from Python's OWN float objects (the ONE message
        # source each; DEC-FEED-RUST-MATERIALIZE-002).
        row, field, kind = stop
        price_name = ("open", "high", "low", "close")[field]
        price = (opens, highs, lows, closes)[field][row]
        if kind == STOP_NONFINITE:
            raise _nonfinite_error(origin, timestamps[row], price_name, price)
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
