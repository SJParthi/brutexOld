"""Click-to-pull controller — ingest ONE instrument's candle data (Dhan).

Powers the F&O Universe Explorer's "⤓ Pull" button
(DEC-UNIVERSE-DASHBOARD-001). The controller is **instrument-agnostic**: it
pulls spot OHLCV for any resolved :class:`~brutex.models.Instrument` — a spot
index/stock or (lake-side) a contract — keyed by ``vendor_symbol`` (the Dhan
``SECURITY_ID``). When the operator clicks a universe row and hits pull,
:func:`pull_candles` ingests that instrument's candles across the Dhan
**fetched** base timeframes
(:data:`DHAN_FETCH_INTERVALS` — ``1minute`` + ``1day``; the 19 intraday
timeframes are resampled from the 1-minute base, never fetched, per
DEC-DHAN-1M-ONLY-001), reusing the proven
:func:`~brutex.adapters.dhan.run_ingest.run_dhan_spot_ingest` orchestration
core (CLAUDE.md §11 COMMON — no copy-paste of the fan-out/quarantine spine).

## Offline-first

The default and the CI-tested path is **dry-run**: a
:class:`~brutex.adapters.dhan.synthetic_adapter.SyntheticDhanAdapter` emits
valid in-session candle rows into a
:class:`~brutex.data.sink.NullBarSink` so the whole fetch → session-filter →
sink pipeline runs with NO Dhan credentials and NO network. The LIVE Dhan
wiring lives in :func:`_build_live_pull_adapter`, which is ``# pragma: no
cover`` because it needs a real token + the ``urllib`` transport. The network
is blocked in CI, so the live path is intentionally not unit-covered — its
pure orchestration is exercised end-to-end through the dry-run path
(DEC-VENDOR-PIVOT-001).

## Scope (Golden Rule §3)

This is a LAKE-surface action authorised by DEC-P0-063 (#200) +
DEC-FETCH-UNIVERSE-001 (#299): pulling spot candle data for any index OR
F&O-eligible stock into the lake. The BRUTE-FORCE ENGINE is unchanged — it
still sweeps only the 3 indices. Pulling a stock's spot here puts it in the
lake (REFERENCE_ONLY), ready for a future, separately-authorised engine
expansion.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import structlog

from brutex.adapters.dhan.auth import dhan_creds_present
from brutex.adapters.dhan.run_ingest import run_dhan_spot_ingest
from brutex.adapters.dhan.synthetic_adapter import SyntheticDhanAdapter
from brutex.adapters.protocol import MarketAdapter
from brutex.data.sink import BarSink, NullBarSink
from brutex.models import Instrument

_LOG = structlog.get_logger(__name__)

#: The Track-1 **operational** pull floor — the start of the locked
#: "Rolling 2021-01-01 → today" range (CLAUDE.md §3). Dhan's vendor capability
#: reaches back to 2020-01-01 (``DhanMarketAdapter.earliest_history``; CLAUDE.md
#: §6 "Historical depth — From 2020"), but the operator deliberately starts the
#: pull at 2021-01-01 for the safer case — the 2020 edge's data quality is
#: unverified (DEC-DHAN-PULL-SCOPE-001). Any ``from_date`` below this floor is
#: clamped up to it so a too-early pick can never waste quota.
DHAN_HISTORY_FLOOR: Final[date] = date(2021, 1, 1)

#: The base timeframes Track 1 actually FETCHES from Dhan: 1-minute (every
#: intraday timeframe is resampled from it) + 1-day (PDH/PDL anchors). The 19
#: derived intraday timeframes are produced from the stored 1-minute base by
#: the resampler, never fetched (DEC-DHAN-1M-ONLY-001). Order = display order.
DHAN_FETCH_INTERVALS: Final[tuple[str, ...]] = ("1minute", "1day")

#: Dhan canonical interval → lake ``TF_LABELS`` display label, so the
#: ``/universe`` grid shows ``1m`` / ``1d`` (consistent with the rest of the
#: dashboard) rather than the vendor's ``1minute`` / ``1day``.
_FETCH_INTERVAL_LABELS: Final[tuple[tuple[str, str], ...]] = (
    ("1minute", "1m"),
    ("1day", "1d"),
)


def resolve_candle_range(
    *,
    from_date: str | None,
    to_date: str | None,
    year: int | None,
    now: datetime,
) -> tuple[datetime, datetime]:
    """Resolve the half-open ``[start, end)`` UTC window a pull must cover.

    O(1): pure date arithmetic, no I/O. Three modes, in priority order:

    1. **Explicit range** — when *from_date* and/or *to_date* is given.
       Each is an ISO ``YYYY-MM-DD`` calendar date; both ends are
       **day-inclusive** (``to_date``'s whole day is fetched, so *end* is
       the midnight that starts the next day). A missing *from_date*
       defaults to :data:`DHAN_HISTORY_FLOOR`; a missing *to_date*
       defaults to today (``now``'s UTC date). The pair is then clamped to
       ``[DHAN_HISTORY_FLOOR, today]`` so a too-early or future pick can
       never waste quota — and the future is never requested.
    2. **Year** — when neither date is given but *year* is: that full
       calendar year, **clamped to ``[DHAN_HISTORY_FLOOR, today]``** like
       mode 1. A past year resolves to the whole year; the current year
       stops at today (never the future); a wholly-future or pre-2021 year
       raises "nothing to pull".
    3. **Default** — neither date nor year: the current UTC calendar year,
       same clamp as mode 2 (so it runs ``[Jan 1, today]``, never ahead).

    Parameters
    ----------
    from_date, to_date
        Inclusive ISO ``YYYY-MM-DD`` bounds, or ``None``.
    year
        Calendar year, used only when both dates are ``None``.
    now
        The reference "current" instant (injected for testability). Its
        UTC date is the upper clamp and the default-year source.

    Returns
    -------
    tuple[datetime, datetime]
        ``(start, end)`` — timezone-aware UTC, half-open, ``start < end``.

    Raises
    ------
    ValueError
        If a date string is not ISO ``YYYY-MM-DD``, or if the resolved
        ``from`` is after the resolved ``to`` (e.g. a future *from_date*,
        or ``from_date > to_date``).
    """
    # Normalise empty / whitespace-only inputs to ``None`` so a bare
    # ``?from_date=`` query param (or a blank picker) behaves exactly like
    # an omitted bound, not as a malformed date — one obvious semantics for
    # "no bound given" across the UI and any direct API caller.
    from_date = (from_date or "").strip() or None
    to_date = (to_date or "").strip() or None

    today = now.astimezone(UTC).date()

    if from_date is None and to_date is None:
        # Mode 2/3 — an explicit *year* (or, absent that, the current UTC
        # year) → that full calendar year as a date pair, which then falls
        # through the SAME clamp as the explicit-range mode below, so the
        # current year stops at today and a wholly-future / pre-2021 year
        # raises "nothing to pull" instead of requesting a window the vendor
        # rejects (DEC-PULL-FUTURE-WINDOW-001).
        resolved_year = year if year is not None else now.year
        start_date = date(resolved_year, 1, 1)
        end_date = date(resolved_year, 12, 31)
    else:
        start_date = _parse_iso_date(from_date, "from_date") if from_date else DHAN_HISTORY_FLOOR
        end_date = _parse_iso_date(to_date, "to_date") if to_date else today

    # Clamp into the vendor-served window; never request the future. Applies
    # to EVERY mode (the year/default branch was previously the one unclamped
    # path — the live-crash root cause).
    start_date = max(start_date, DHAN_HISTORY_FLOOR)
    end_date = min(end_date, today)
    if start_date > end_date:
        raise ValueError(
            f"from_date ({start_date.isoformat()}) is after to_date "
            f"({end_date.isoformat()}) — nothing to pull"
        )

    start = datetime(start_date.year, start_date.month, start_date.day, tzinfo=UTC)
    # End is exclusive; add a day so the whole of ``end_date`` is fetched.
    end = datetime(end_date.year, end_date.month, end_date.day, tzinfo=UTC) + timedelta(days=1)
    return start, end


def _parse_iso_date(value: str, field_name: str) -> date:
    """Parse an ISO ``YYYY-MM-DD`` string, re-raising with the field name."""
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be ISO YYYY-MM-DD, got {value!r}") from exc


#: A factory that builds the ``(adapter, sink)`` pair the Dhan ingest spine
#: needs, given the lake root + the instrument being pulled. The default
#: builds the offline synthetic pair; the live path injects
#: :func:`_build_live_pull_adapter`.
AdapterFactory = Callable[[Path, Instrument], tuple[MarketAdapter, BarSink]]


@dataclass(frozen=True, slots=True)
class TimeframeResult:
    """Per-timeframe completed / failed / no-data / skipped counts for one pull."""

    interval: str
    completed: int
    failed: int
    no_data: int = 0
    skipped: int = 0


@dataclass(slots=True)
class PullCandlesReport:
    """Result of a single :func:`pull_candles` call.

    Attributes
    ----------
    symbol
        The Dhan ``vendor_symbol`` (``SECURITY_ID``) that was pulled.
    year
        The calendar year the pull *started* in. For a full-year pull this
        is the year covered; for an explicit ``from``/``to`` range it is
        ``start_date``'s year (kept for backward compatibility — read
        ``start_date`` / ``end_date`` for the exact window).
    start_date, end_date
        The inclusive ISO ``YYYY-MM-DD`` bounds actually pulled, after the
        :func:`resolve_candle_range` clamp. The ``/universe`` page shows
        these so the operator sees the real window, not just the year.
    dry_run
        ``True`` when synthetic data was used (no creds / no network).
        The ``/universe`` page shows a "PREVIEW · synthetic" badge in
        this case.
    timeframes
        One :class:`TimeframeResult` per **fetched** interval, in
        :data:`DHAN_FETCH_INTERVALS` order (``1m`` then ``1d``). The 19
        derived intraday timeframes are resampled from the 1-minute base
        downstream, not fetched here (DEC-DHAN-1M-ONLY-001).
    """

    symbol: str
    year: int
    dry_run: bool
    start_date: str = ""
    end_date: str = ""
    timeframes: list[TimeframeResult] = field(default_factory=list)

    @property
    def total_completed(self) -> int:
        """Total intervals that fetched + persisted at least one bar."""
        return sum(tf.completed for tf in self.timeframes)

    @property
    def total_failed(self) -> int:
        """Total intervals that failed terminally (quarantined)."""
        return sum(tf.failed for tf in self.timeframes)

    @property
    def total_no_data(self) -> int:
        """Total intervals the vendor returned empty (NO_DATA)."""
        return sum(tf.no_data for tf in self.timeframes)

    @property
    def total_skipped(self) -> int:
        """Total intervals skipped as already-settled (always 0 — see field doc)."""
        return sum(tf.skipped for tf in self.timeframes)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly dict for the ``/api/v1/universe/pull`` route."""
        return {
            "symbol": self.symbol,
            "year": self.year,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "dry_run": self.dry_run,
            "total_completed": self.total_completed,
            "total_failed": self.total_failed,
            "total_no_data": self.total_no_data,
            "total_skipped": self.total_skipped,
            "timeframes": [
                {
                    "interval": tf.interval,
                    "completed": tf.completed,
                    "failed": tf.failed,
                    "no_data": tf.no_data,
                    "skipped": tf.skipped,
                }
                for tf in self.timeframes
            ],
        }


def _build_dry_run_adapter(
    _lake_root: Path,
    _instrument: Instrument,
) -> tuple[MarketAdapter, BarSink]:
    """Build the offline ``(SyntheticDhanAdapter, NullBarSink)`` pair.

    The synthetic adapter emits valid in-session bars and the null sink
    counts-but-discards them, so the pull runs with zero dependencies — the
    dry-run proof the spine works (DEC-VENDOR-PIVOT-001).
    """
    return SyntheticDhanAdapter(), NullBarSink()


def _build_live_spot_adapter(  # pragma: no cover — live Dhan token + transport + network
    lake_root: Path,
    instruments: Sequence[Instrument],
) -> tuple[MarketAdapter, BarSink]:
    """Wire the real Dhan client + a :class:`DhanMarketAdapter` over *instruments*.

    The single source of the live spot wiring (CLAUDE.md §11 COMMON) — shared
    by the single-instrument and bulk builders. Builds a token-authenticated
    :class:`~brutex.adapters.dhan.client.DhanClient` over the real ``urllib``
    transport, a :class:`~brutex.adapters.dhan.adapter.DhanMarketAdapter`
    carrying every *instrument* (its O(1) index must contain each symbol it
    fetches), and a :class:`~brutex.data.sink.ParquetBarSink` over *lake_root*.
    ``# pragma: no cover`` — needs real credentials + network, absent in CI.
    """
    from brutex.adapters.dhan.adapter import DhanMarketAdapter
    from brutex.adapters.dhan.auth import resolve_dhan_credentials
    from brutex.adapters.dhan.client import DhanClient, urllib_transport
    from brutex.data.parquet_writer import ParquetWriter
    from brutex.data.sink import ParquetBarSink

    creds = resolve_dhan_credentials()
    client = DhanClient(
        access_token=creds.access_token,
        client_id=creds.client_id,
        transport=urllib_transport,
    )
    adapter = DhanMarketAdapter(client=client, instruments=tuple(instruments))
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=lake_root))
    return adapter, sink


def _build_live_pull_adapter(  # pragma: no cover — thin single-instrument wrapper
    lake_root: Path,
    instrument: Instrument,
) -> tuple[MarketAdapter, BarSink]:
    """Live ``(adapter, sink)`` for a single-instrument pull (wraps the shared builder)."""
    return _build_live_spot_adapter(lake_root, (instrument,))


#: A factory that builds the ``(adapter, sink)`` pair for a BULK pull, given
#: the lake root + ALL the instruments being pulled (the live adapter's O(1)
#: index must contain every symbol it fetches).
BulkAdapterFactory = Callable[[Path, Sequence[Instrument]], tuple[MarketAdapter, BarSink]]


@dataclass(frozen=True, slots=True)
class SymbolPullResult:
    """Per-symbol completed / failed / no-data interval counts for a bulk pull."""

    symbol: str
    completed: int
    failed: int
    no_data: int


@dataclass(slots=True)
class BulkSpotReport:
    """Result of a :func:`pull_all_spot` call (the one-click bulk-spot button).

    Aggregates the per-symbol outcome across the two fetched intervals
    (``1m`` + ``1d``) over the full ``[2021-01-01, today]`` range
    (DEC-DHAN-PULL-SCOPE-001).
    """

    dry_run: bool
    start_date: str
    end_date: str
    symbols: list[SymbolPullResult] = field(default_factory=list)

    @property
    def total_symbols(self) -> int:
        """Number of instruments pulled."""
        return len(self.symbols)

    @property
    def total_completed(self) -> int:
        """Total ``(symbol, interval)`` pairs that fetched at least one bar."""
        return sum(s.completed for s in self.symbols)

    @property
    def total_failed(self) -> int:
        """Total pairs quarantined on a terminal error."""
        return sum(s.failed for s in self.symbols)

    @property
    def total_no_data(self) -> int:
        """Total pairs the vendor returned empty."""
        return sum(s.no_data for s in self.symbols)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly dict for the ``/api/v1/universe/pull-all`` route."""
        return {
            "dry_run": self.dry_run,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "total_symbols": self.total_symbols,
            "total_completed": self.total_completed,
            "total_failed": self.total_failed,
            "total_no_data": self.total_no_data,
            "symbols": [
                {
                    "symbol": s.symbol,
                    "completed": s.completed,
                    "failed": s.failed,
                    "no_data": s.no_data,
                }
                for s in self.symbols
            ],
        }


def _build_dry_run_bulk_adapter(
    _lake_root: Path,
    _instruments: Sequence[Instrument],
) -> tuple[MarketAdapter, BarSink]:
    """Build the offline ``(SyntheticDhanAdapter, NullBarSink)`` pair for a bulk pull."""
    return SyntheticDhanAdapter(), NullBarSink()


def _build_live_bulk_adapter(  # pragma: no cover — thin bulk wrapper
    lake_root: Path,
    instruments: Sequence[Instrument],
) -> tuple[MarketAdapter, BarSink]:
    """Live ``(adapter, sink)`` for a bulk pull (wraps the shared builder over all instruments)."""
    return _build_live_spot_adapter(lake_root, instruments)


def pull_all_spot(
    instruments: Sequence[Instrument],
    *,
    lake_root: Path,
    dry_run: bool,
    now: datetime | None = None,
    adapter_factory: BulkAdapterFactory | None = None,
) -> BulkSpotReport:
    """Bulk-pull EVERY instrument's spot over the full ``[2021-01-01, today]`` range.

    The one-click "⤓ Pull ALL indices / stocks spot" button core: one
    :func:`~brutex.adapters.dhan.run_ingest.run_dhan_spot_ingest` pass over
    the whole *instruments* list and the fetched base intervals
    (:data:`DHAN_FETCH_INTERVALS` — ``1m`` + ``1d``), so the live adapter is
    built ONCE with the full universe (O(1) index) and the fan-out quarantines
    a bad symbol without sinking the rest. An empty list is a clean no-op.

    Parameters
    ----------
    instruments
        Every index (or stock) spot :class:`Instrument` to pull.
    lake_root
        Lake directory the live sink writes Parquet into.
    dry_run
        When ``True`` use the offline synthetic adapter (no creds / network).
    now
        Injected "current" instant (defaults to :func:`datetime.now`) — its
        UTC date is the upper bound of the full ``[2021-01-01, today]`` window.
    adapter_factory
        Override the bulk ``(adapter, sink)`` builder; defaults to the
        synthetic builder when *dry_run* else the live builder.

    Returns
    -------
    BulkSpotReport
        Per-symbol completed / failed / no-data counts + the aggregate totals.
    """
    instruments = list(instruments)
    start, end = resolve_candle_range(
        from_date=DHAN_HISTORY_FLOOR.isoformat(),
        to_date=None,
        year=None,
        now=now if now is not None else datetime.now(UTC),
    )
    if adapter_factory is None:
        adapter_factory = _build_dry_run_bulk_adapter if dry_run else _build_live_bulk_adapter
    adapter, sink = adapter_factory(lake_root, instruments)

    _LOG.info(
        "universe.pull_all.start",
        instruments=len(instruments),
        start_date=start.date().isoformat(),
        dry_run=dry_run,
    )
    report = run_dhan_spot_ingest(
        adapter,
        sink,
        instruments=instruments,
        intervals=DHAN_FETCH_INTERVALS,
        start=start.date(),
        end=end.date(),
    )
    failure_keys = {(f.symbol, f.interval) for f in report.failures}
    results: list[SymbolPullResult] = []
    for instrument in instruments:
        symbol = instrument.vendor_symbol
        completed = failed = no_data = 0
        for fetch_interval, _label in _FETCH_INTERVAL_LABELS:
            key = (symbol, fetch_interval)
            if key in failure_keys:
                failed += 1
            elif report.pairs[key].bars_appended > 0:
                completed += 1
            else:
                no_data += 1
        results.append(
            SymbolPullResult(symbol=symbol, completed=completed, failed=failed, no_data=no_data)
        )
    bulk = BulkSpotReport(
        dry_run=dry_run,
        start_date=start.date().isoformat(),
        end_date=(end - timedelta(days=1)).date().isoformat(),
        symbols=results,
    )
    _LOG.info(
        "universe.pull_all.done",
        instruments=bulk.total_symbols,
        completed=bulk.total_completed,
        failed=bulk.total_failed,
        dry_run=dry_run,
    )
    return bulk


def pull_candles(
    instrument: Instrument,
    *,
    lake_root: Path,
    year: int | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    dry_run: bool,
    adapter_factory: AdapterFactory | None = None,
) -> PullCandlesReport:
    """Ingest *instrument*'s spot candles over a date range (Dhan model).

    Plans + drives one
    :func:`~brutex.adapters.dhan.run_ingest.run_dhan_spot_ingest` pass over
    the single *instrument* and the Dhan **fetched** base intervals
    (:data:`DHAN_FETCH_INTERVALS` — ``1minute`` + ``1day``; the 19 derived
    intraday timeframes are resampled from the 1-minute base downstream,
    never fetched, per DEC-DHAN-1M-ONLY-001). The window is resolved by
    :func:`resolve_candle_range`: an explicit *from_date* / *to_date* range
    (day-inclusive, clamped to ``[2021-01-01, today]``) when either is given,
    else the full calendar *year*. Idempotent + rerun-safe — the sink append
    is a no-op on already-stored bars (CLAUDE.md §2 Rule 7).

    Parameters
    ----------
    instrument
        The resolved spot instrument to pull; ``vendor_symbol`` is the Dhan
        ``SECURITY_ID`` and ``exchange`` / ``segment`` drive the sink
        partition. Resolved by the route from the instrument store.
    lake_root
        Lake directory the live sink writes Parquet into. Unused by the
        dry-run null sink.
    year
        Calendar year to pull when no explicit range is given. ``None``
        (default) uses the current UTC year. Ignored when *from_date* or
        *to_date* is supplied.
    from_date, to_date
        Inclusive ISO ``YYYY-MM-DD`` bounds for an explicit range. Either
        may be ``None`` (defaults to the Dhan history floor / today
        respectively). When both are ``None`` the *year* mode applies.
    dry_run
        When ``True``, use the offline synthetic adapter (no creds /
        network). When ``False``, build the live Dhan adapter via
        *adapter_factory*.
    adapter_factory
        Override the ``(adapter, sink)`` builder. Defaults to the offline
        synthetic builder when *dry_run* is ``True`` and to the live Dhan
        builder otherwise. Tests inject a fake factory to exercise the
        non-dry-run branch and the per-interval outcomes without network.

    Returns
    -------
    PullCandlesReport
        Per-fetched-interval completed / failed / no-data counts + the
        ``dry_run`` flag.

    Raises
    ------
    ValueError
        If *instrument*'s ``vendor_symbol`` is empty / blank, or by
        :func:`resolve_candle_range` for a malformed date string or a
        ``from`` after ``to`` (incl. a future ``from_date``).
    """
    symbol = instrument.vendor_symbol
    if not symbol.strip():
        raise ValueError("instrument.vendor_symbol must be a non-empty Dhan SECURITY_ID")

    start, end = resolve_candle_range(
        from_date=from_date,
        to_date=to_date,
        year=year,
        now=datetime.now(UTC),
    )
    resolved_year = start.year
    # Inclusive end label = the last day actually fetched (end is exclusive).
    end_label = (end - timedelta(days=1)).date().isoformat()

    if adapter_factory is None:
        adapter_factory = _build_dry_run_adapter if dry_run else _build_live_pull_adapter
    adapter, sink = adapter_factory(lake_root, instrument)

    _LOG.info(
        "universe.pull.start",
        symbol=symbol,
        year=resolved_year,
        start_date=start.date().isoformat(),
        end_date=end_label,
        dry_run=dry_run,
        intervals=len(DHAN_FETCH_INTERVALS),
    )

    report = run_dhan_spot_ingest(
        adapter,
        sink,
        instruments=[instrument],
        intervals=DHAN_FETCH_INTERVALS,
        start=start.date(),
        end=end.date(),
    )

    failure_keys = {(f.symbol, f.interval) for f in report.failures}
    timeframes: list[TimeframeResult] = []
    for fetch_interval, label in _FETCH_INTERVAL_LABELS:
        key = (symbol, fetch_interval)
        if key in failure_keys:
            # Terminal Dhan error — quarantined, never in report.pairs.
            timeframes.append(TimeframeResult(interval=label, completed=0, failed=1))
        elif report.pairs[key].bars_appended > 0:
            timeframes.append(TimeframeResult(interval=label, completed=1, failed=0))
        else:
            # Ran cleanly but the vendor returned no bars for the window.
            timeframes.append(TimeframeResult(interval=label, completed=0, failed=0, no_data=1))

    pull_report = PullCandlesReport(
        symbol=symbol,
        year=resolved_year,
        start_date=start.date().isoformat(),
        end_date=end_label,
        dry_run=dry_run,
        timeframes=timeframes,
    )
    _LOG.info(
        "universe.pull.done",
        symbol=symbol,
        year=resolved_year,
        start_date=start.date().isoformat(),
        end_date=end_label,
        dry_run=dry_run,
        completed=pull_report.total_completed,
        failed=pull_report.total_failed,
    )
    return pull_report


__all__ = [
    "DHAN_FETCH_INTERVALS",
    "DHAN_HISTORY_FLOOR",
    "AdapterFactory",
    "BulkAdapterFactory",
    "BulkSpotReport",
    "PullCandlesReport",
    "SymbolPullResult",
    "TimeframeResult",
    "dhan_creds_present",
    "pull_all_spot",
    "pull_candles",
    "resolve_candle_range",
]
