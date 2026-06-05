"""Click-to-pull controller — ingest ONE instrument's candle data (Dhan).

Powers the F&O Universe Explorer's "⤓ Pull" button
(DEC-UNIVERSE-DASHBOARD-001). The controller is **instrument-agnostic**: it
pulls spot OHLCV for any resolved :class:`~brutex.models.Instrument` — a spot
index/stock or (lake-side) a contract — keyed by ``symbol_name`` (the engine
symbol; ADR-0003). When the operator clicks a universe row and hits pull,
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

import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import structlog

from brutex.adapters.dhan.auth import dhan_creds_present
from brutex.adapters.dhan.run_ingest import ProgressHook, run_dhan_spot_ingest
from brutex.adapters.dhan.synthetic_adapter import SyntheticDhanAdapter
from brutex.adapters.protocol import MarketAdapter
from brutex.dashboard.coverage_reconcile import (
    reconcile_coverage_with_lake,
    reconcile_empty_terminal,
)
from brutex.data.fetch_coverage import FetchCoverage
from brutex.data.fetch_coverage_store import begin_pull_coverage, end_pull_coverage
from brutex.data.sink import BarSink, NullBarSink
from brutex.data.window_status_store import begin_pull_status, end_pull_status
from brutex.models import Instrument
from brutex.observability.correlation import pull_id_scope
from brutex.orchestrator.parallel import (
    DEFAULT_LATENCY_BUDGET_S,
    PoolRunParallel,
    workers_to_saturate,
)
from brutex.orchestrator.universe_phases import RunParallel

if TYPE_CHECKING:
    from brutex.adapters.dhan.rate_control import DhanRateController

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
       defaults to the **previous day** (``today - 1``), so a pull never
       grabs the still-forming current session by default. An EXPLICIT
       *to_date* IS honoured (clamped to ``today``), so the operator can
       deliberately include today by picking it. The pair is then clamped
       to ``[DHAN_HISTORY_FLOOR, today]`` so a too-early or future pick can
       never waste quota — and the future is never requested.
    2. **Year** — when neither date is given but *year* is: that full
       calendar year, **clamped to ``[DHAN_HISTORY_FLOOR, yesterday]``**.
       A past year resolves to the whole year; the current year stops at
       the previous day (never today / the future); a wholly-future or
       pre-2021 year raises "nothing to pull".
    3. **Default** — neither date nor year: the FULL Track-1 history,
       ``[DHAN_HISTORY_FLOOR (2021-01-01), previous day]``. (Previously
       this defaulted to the current calendar year — corrected so a blank
       picker pulls the whole locked range, not just this year.)

    The default upper bound is the **previous day** (``today - 1``) so a
    routine pull never includes the in-progress current session; today is
    included ONLY when an explicit *to_date* reaching today is selected.

    Parameters
    ----------
    from_date, to_date
        Inclusive ISO ``YYYY-MM-DD`` bounds, or ``None``.
    year
        Calendar year, used only when both dates are ``None``.
    now
        The reference "current" instant (injected for testability). Its
        UTC date drives the upper clamp (``today``) and the default end
        (``yesterday``).

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
    # Default upper bound = the PREVIOUS day, so a routine pull never grabs the
    # still-forming current session. An EXPLICIT to_date overrides this and may
    # reach today (clamped below). yesterday < DHAN_HISTORY_FLOOR is impossible
    # in practice (today is well past 2021) but the start>end guard covers it.
    yesterday = today - timedelta(days=1)

    # An EXPLICIT to_date may reach today; every other mode (year / default /
    # missing to_date) stops at the previous day so a routine pull never grabs
    # the still-forming current session. ``ceiling`` is the upper clamp.
    explicit_to = to_date is not None
    ceiling = today if explicit_to else yesterday

    if from_date is None and to_date is None:
        # Mode 2/3 — an explicit *year* picks that full calendar year; absent a
        # year the default is the FULL Track-1 history (2021-01-01 → previous
        # day), NOT the current year. Both fall through the SAME clamp below so
        # the window never reaches today/the future and a wholly-future /
        # pre-2021 year raises "nothing to pull" (DEC-PULL-FUTURE-WINDOW-001).
        if year is not None:
            start_date = date(year, 1, 1)
            end_date = date(year, 12, 31)
        else:
            start_date = DHAN_HISTORY_FLOOR
            end_date = yesterday
    else:
        start_date = _parse_iso_date(from_date, "from_date") if from_date else DHAN_HISTORY_FLOOR
        # An explicit to_date is honoured (clamped to today via ``ceiling`` —
        # explicit today IS allowed); a missing to_date defaults to the
        # previous day.
        end_date = _parse_iso_date(to_date, "to_date") if to_date else yesterday

    # Clamp into the vendor-served window; never request the future. Applies to
    # EVERY mode (the year/default branch was previously the one unclamped path
    # — the live-crash root cause). Only an explicit to_date may reach today;
    # otherwise the ceiling is the previous day.
    start_date = max(start_date, DHAN_HISTORY_FLOOR)
    end_date = min(end_date, ceiling)
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


def parse_symbol_list(symbols: str) -> list[str]:
    """Split a comma-separated ``?symbols=`` value into a clean, ordered id list.

    Blank entries are dropped and duplicates are removed (first occurrence wins),
    while INPUT ORDER is preserved — so the multi-select pull fans the ticked
    underlyings in the exact order the operator sees them, deterministically
    (DEC-UNIVERSE-OPTIONS-MULTIPULL-001). O(n) over the (tiny) selection.

    Parameters
    ----------
    symbols
        The raw comma-joined query value (e.g. ``"NIFTY, BANKNIFTY ,NIFTY"``).

    Returns
    -------
    list[str]
        De-duplicated, order-preserving, whitespace-trimmed ids
        (``["NIFTY", "BANKNIFTY"]`` for the example).
    """
    seen: set[str] = set()
    out: list[str] = []
    for raw in symbols.split(","):
        sid = raw.strip()
        if sid and sid not in seen:
            seen.add(sid)
            out.append(sid)
    return out


def resolve_pull_symbols(
    symbols: str, lookup: Callable[[str], Instrument | None]
) -> list[Instrument]:
    """Resolve a comma ``?symbols=`` list to instruments via *lookup* (a store ``get``).

    The backend half of the unified multi-select OPTIONS pull
    (DEC-UNIVERSE-OPTIONS-MULTIPULL-001): the page sends the ticked underlyings
    as ``?symbols=a,b,c``; this resolves each to its
    :class:`~brutex.models.Instrument` so ONE
    :func:`~brutex.dashboard.option_pull_controller.pull_options` pass fans them
    all (the core already accepts a list). Blanks are dropped + duplicates
    removed (:func:`parse_symbol_list`), order preserved.

    Fails CLOSED (Golden Rule #1, no silent scope drift): an EMPTY selection or
    ANY unknown id raises :class:`ValueError` naming every offender, which the
    route maps to a 400 — so a typo never silently pulls a partial set.

    Parameters
    ----------
    symbols
        The raw comma-joined ``?symbols=`` value.
    lookup
        ``store.get``-style resolver: an id → :class:`Instrument` or ``None``.

    Returns
    -------
    list[Instrument]
        One resolved instrument per (de-duplicated) id, in input order.

    Raises
    ------
    ValueError
        If the list is empty after cleaning, or any id does not resolve.
    """
    ids = parse_symbol_list(symbols)
    if not ids:
        raise ValueError("symbols= was empty — tick at least one instrument")
    resolved: list[Instrument] = []
    unknown: list[str] = []
    for sid in ids:
        instrument = lookup(sid)
        if instrument is None:
            unknown.append(sid)
        else:
            resolved.append(instrument)
    if unknown:
        raise ValueError(
            f"unknown security_id(s) not in the instrument store: {', '.join(unknown)}"
        )
    return resolved


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
        The engine ``symbol_name`` that was pulled (ADR-0003).
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
    (``1m`` + ``1d``) over the full ``[2021-01-01, previous day]`` range
    (DEC-DHAN-PULL-SCOPE-001) — the current session is excluded by default.
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


#: The "no rate controller" feedback triple (dry-run / injected-factory pulls).
_NO_RATE_HOOKS: tuple[None, None, None, None] = (None, None, None, None)

#: ``(pace, on_rate_limit, on_timeout, on_success)`` — the controller hooks the
#: spot ingest spine threads (on_timeout added per DEC-DHAN-SPOT-TIMEOUT-BACKOFF-001).
_RateHooks = tuple[
    Callable[[], None] | None,
    Callable[[], None] | None,
    Callable[[], None] | None,
    Callable[[], None] | None,
]


def _spot_run_parallel(controller: DhanRateController | None) -> RunParallel | None:
    """Size the spot fan-out pool to the controller's rate budget (Little's law).

    DEC-DHAN-PARALLEL-INGEST-001. A live pull threads its shared
    :class:`~brutex.adapters.dhan.rate_control.DhanRateController` through here:
    the pool is widened to ``workers_to_saturate(controller.current_rps or 5.0,
    DEFAULT_LATENCY_BUDGET_S)`` so enough windows are in flight to keep the rate
    BUCKET (not network latency) the bottleneck — targeting/saturating UP TO
    ~5 r/s (Rule #6 still caps the aggregate; honest envelope: never a guaranteed
    constant 5 r/s). ``None`` controller (dry-run / injected-factory pull) →
    ``None`` here, so ``run_dhan_spot_ingest`` uses its default pool. This is the
    pure, covered sizing seam; the live controller construction is the
    ``# pragma: no cover`` part.
    """
    if controller is None:
        return None
    workers = workers_to_saturate(controller.current_rps or 5.0, DEFAULT_LATENCY_BUDGET_S)
    return PoolRunParallel(max_workers=workers)


def _build_live_controller() -> DhanRateController:  # pragma: no cover — live Dhan controller
    """Construct the shared adaptive [3..5] r/s controller (the session factory).

    The controller is threaded into BOTH the per-window pacing hooks (``pace`` /
    ``on_rate_limit`` / ``on_success``) AND the pool sizing
    (:func:`_spot_run_parallel`), so the fan-out width tracks the controller's
    current rate (DEC-DHAN-ADAPTIVE-RPS-001 + DEC-DHAN-PARALLEL-INGEST-001). Built
    ONCE per dashboard session and reused across pulls via
    :class:`SessionRateController` (DEC-DHAN-PERSIST-RATE-001) so the learned rate
    carries over instead of every pull restarting at the 5 r/s ceiling.
    """
    from brutex.adapters.dhan.rate_control import DhanRateController

    return DhanRateController()


class SessionRateController:
    """Persist ONE :class:`DhanRateController` across pulls in a dashboard session.

    DEC-DHAN-PERSIST-RATE-001. Each ``POST /universe/pull`` used to build a FRESH
    controller at the 5 r/s ceiling (:func:`_build_live_controller`), so every
    symbol-pull re-opened at 5 r/s and re-hit Dhan's wall — the 429 storm the
    operator saw at the start of *every* pull, re-learnt per symbol. This holder
    builds the controller ONCE (lazily, thread-safe) and hands the SAME instance
    to every pull, so the rate the system LEARNED (e.g. stepped down to 3 r/s)
    CARRIES OVER: symbol 2, 3, 4… start at the learned rate, not 5. The controller
    is itself thread-safe (DEC-DHAN-PARALLEL-INGEST-001), so sharing it across
    sequential AND concurrent pulls correctly keeps the AGGREGATE issue rate
    ≤ 5 r/s (Rule #6) — concurrent pulls now share one global budget instead of
    each running its own 5 r/s.
    """

    def __init__(self, factory: Callable[[], DhanRateController] | None = None) -> None:
        """Build a holder. *factory* (a test seam) defaults to :func:`_build_live_controller`."""
        self._factory = factory if factory is not None else _build_live_controller
        self._controller: DhanRateController | None = None
        self._lock = threading.Lock()

    def get(self) -> DhanRateController:
        """Return the session's controller, building it ONCE on first use (thread-safe)."""
        with self._lock:
            if self._controller is None:
                self._controller = self._factory()
            return self._controller


#: The dashboard rate-panel observer (DEC-DASHBOARD-RATE-PANEL-001): given a
#: ``DhanRateController.snapshot()`` (current_rps + recent_hits) and a
#: ``CallMeter.snapshot()`` (effective_rps + latency), push both to the panel.
RateObserver = Callable[[Mapping[str, Any], Mapping[str, Any]], None]


def _observe_rate(
    rate_observer: RateObserver | None,
    controller: DhanRateController | None,
    adapter: MarketAdapter,
) -> None:
    """Push a live pull's adaptive rate + measured throughput to the dashboard panel.

    A no-op for a dry-run / injected-factory pull (no *controller*) or when no
    observer is wired (the default). On a live pull it sends the controller's
    ``snapshot()`` (current_rps + recent 429s) and the transport ``CallMeter``'s
    ``snapshot()`` (effective_rps + latency p95) to *rate_observer*
    (``make_rate_observer`` — DEC-DASHBOARD-RATE-PANEL-001), so the operator SEES
    the live rate on the dashboard, not just in ``/logs``.
    """
    if rate_observer is None or controller is None:
        return
    meter = getattr(getattr(adapter, "client", None), "meter", None)  # pragma: no cover — live
    rate_observer(  # pragma: no cover — live
        controller.snapshot(),
        meter.snapshot() if meter is not None else {},
    )


def _live_rate_runtime(
    use_live_rate: bool,
    session_rate: SessionRateController | None = None,
) -> tuple[_RateHooks, RunParallel | None, DhanRateController | None]:
    """Return ``((pace, on_rate_limit, on_timeout, on_success), run_parallel, controller)``.

    A **default live** pull (no injected factory + not dry-run) resolves ONE shared
    controller and derives both the feedback hooks AND a rate-sized fan-out pool
    from it; the controller is also returned so the caller can push its live
    ``snapshot()`` to the dashboard rate panel (:func:`_observe_rate`). When a
    *session_rate* holder is wired the controller is the SESSION's persisted one
    (:class:`SessionRateController` — DEC-DHAN-PERSIST-RATE-001) so its learned
    rate carries across pulls; otherwise a fresh controller is built. Dry-run /
    injected-factory pulls need no throttle → the None-quad + ``None`` pool +
    ``None`` controller (the ingest spine then uses its default pool). The sizing
    itself (:func:`_spot_run_parallel`) is covered; only the live controller
    resolution is ``# pragma: no cover``.
    """
    if not use_live_rate:
        return _NO_RATE_HOOKS, None, None
    controller = (  # pragma: no cover — live only
        session_rate.get() if session_rate is not None else _build_live_controller()
    )
    hooks = (  # pragma: no cover — live only
        controller.pace,
        controller.on_rate_limit,
        controller.on_timeout,
        controller.on_success,
    )
    return hooks, _spot_run_parallel(controller), controller  # pragma: no cover — live only


def pull_all_spot(
    instruments: Sequence[Instrument],
    *,
    lake_root: Path,
    dry_run: bool,
    now: datetime | None = None,
    adapter_factory: BulkAdapterFactory | None = None,
    on_progress: ProgressHook | None = None,
    coverage: FetchCoverage | None = None,
    pull_id: str | None = None,
    rate_observer: RateObserver | None = None,
    session_rate: SessionRateController | None = None,
) -> BulkSpotReport:
    """Bulk-pull EVERY instrument's spot over the full ``[2021-01-01, previous day]`` range.

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
        UTC date drives the upper bound (the previous day) of the full
        ``[2021-01-01, previous day]`` window; the current session is excluded.
    adapter_factory
        Override the bulk ``(adapter, sink)`` builder; defaults to the
        synthetic builder when *dry_run* else the live builder.
    coverage
        Optional pre-seeded skip-ledger. On a **live** pull (``dry_run`` False),
        ``None`` (the default) is replaced by the persisted ledger
        (``<lake_root>/fetch_coverage.json``) so already-pulled windows are
        skipped and the windows completed by this run are saved back — a re-pull
        never re-burns the Dhan quota on data already in the lake
        (DEC-FETCH-COVERAGE-WIRING-001). Dry-run ignores it (null sink).

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
    use_live_rate = adapter_factory is None and not dry_run
    if adapter_factory is None:
        adapter_factory = _build_dry_run_bulk_adapter if dry_run else _build_live_bulk_adapter
    adapter, sink = adapter_factory(lake_root, instruments)

    # Open the pull's correlation scope: ``pid`` is bound into the structlog
    # contextvars for the whole pass, so every event — this start/done summary,
    # the ingest spine's ``ingest.*`` events, AND the already-lit transport
    # (``dhan.call``) + rate (``rate.*``) events — shares one ``pull_id``
    # (DEC-DHAN-INGEST-LOGGING-001). Always unbinds on exit (no leak).
    with pull_id_scope(pull_id) as pid:
        # Skip windows already in the lake on a live re-pull (no wasted Dhan quota);
        # DEC-FETCH-COVERAGE-WIRING-001.
        coverage = begin_pull_coverage(lake_root, dry_run=dry_run, coverage=coverage)
        # Reconcile the ledger against the lake (the source of truth): windows
        # whose bars are already stored are marked complete → skipped (no re-fetch
        # / conflict flood), while windows the lake does not cover stay un-marked →
        # re-pulled (DEC-LAKE-RECONCILE-001). One-time O(datasets) at pull start.
        if not dry_run and coverage is not None:
            reconcile_coverage_with_lake(
                coverage,
                lake_root,
                instruments=instruments,
                intervals=DHAN_FETCH_INTERVALS,
                start=start.date(),
                end=end.date(),
            )
        # Per-window status (DEC-WINDOW-STATUS-001): a browsable SUCCESS/EMPTY/
        # FAILED record, loaded for the run + persisted at pull end.
        status_log = begin_pull_status(lake_root, dry_run=dry_run)
        # EMPTY-terminal (DEC-EMPTY-TERMINAL-001): a window seen genuinely EMPTY on
        # `threshold` separate pulls (e.g. SENSEX 1-minute before 2021-08-04) is
        # marked complete → skipped, so it stops re-fetching every run. Pure
        # (coverage + the cross-run status log), O(recorded windows) at pull start.
        if not dry_run and coverage is not None and status_log is not None:
            reconcile_empty_terminal(coverage, status_log)

        _LOG.info(
            "universe.pull_all.start",
            pull_id=pid,
            instruments=len(instruments),
            start_date=start.date().isoformat(),
            dry_run=dry_run,
        )
        (pace, on_rate_limit, on_timeout, on_success), run_parallel, _rate_controller = (
            _live_rate_runtime(use_live_rate, session_rate)
        )
        report = run_dhan_spot_ingest(
            adapter,
            sink,
            instruments=instruments,
            intervals=DHAN_FETCH_INTERVALS,
            start=start.date(),
            end=end.date(),
            pace=pace,
            on_rate_limit=on_rate_limit,
            on_timeout=on_timeout,
            on_success=on_success,
            on_progress=on_progress,
            coverage=coverage,
            status_log=status_log,
            run_parallel=run_parallel,
        )
        end_pull_coverage(coverage, lake_root, dry_run=dry_run)
        end_pull_status(status_log, lake_root, dry_run=dry_run)
        _observe_rate(rate_observer, _rate_controller, adapter)
        failure_keys = {(f.symbol, f.interval) for f in report.failures}
        results: list[SymbolPullResult] = []
        for instrument in instruments:
            symbol = instrument.symbol_name
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
            pull_id=pid,
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
    on_progress: ProgressHook | None = None,
    coverage: FetchCoverage | None = None,
    pull_id: str | None = None,
    rate_observer: RateObserver | None = None,
    session_rate: SessionRateController | None = None,
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
    else the full calendar *year*, else (no dates, no year) the full Track-1
    history ``[2021-01-01, previous day]``. The default end is the previous
    day — today is pulled only via an explicit *to_date* reaching it.
    Idempotent + rerun-safe — the sink append is a no-op on already-stored
    bars (CLAUDE.md §2 Rule 7).

    Parameters
    ----------
    instrument
        The resolved spot instrument to pull; ``symbol_name`` is the engine
        symbol (the partition key) and ``exch_id`` / ``segment`` drive the
        sink partition. Resolved by the route from the instrument store.
    lake_root
        Lake directory the live sink writes Parquet into. Unused by the
        dry-run null sink.
    year
        Calendar year to pull when no explicit range is given. ``None``
        (default) pulls the full Track-1 history (2021-01-01 → previous
        day), NOT just the current year. Ignored when *from_date* or
        *to_date* is supplied.
    from_date, to_date
        Inclusive ISO ``YYYY-MM-DD`` bounds for an explicit range. Either
        may be ``None`` (*from_date* defaults to the Dhan history floor;
        *to_date* defaults to the previous day — an explicit *to_date* may
        reach today). When both are ``None`` the *year* / full-history mode
        applies.
    dry_run
        When ``True``, use the offline synthetic adapter (no creds /
        network). When ``False``, build the live Dhan adapter via
        *adapter_factory*.
    adapter_factory
        Override the ``(adapter, sink)`` builder. Defaults to the offline
        synthetic builder when *dry_run* is ``True`` and to the live Dhan
        builder otherwise. Tests inject a fake factory to exercise the
        non-dry-run branch and the per-interval outcomes without network.
    coverage
        Optional pre-seeded skip-ledger. On a **live** pull (``dry_run`` False),
        ``None`` (the default) is replaced by the persisted ledger
        (``<lake_root>/fetch_coverage.json``) so already-pulled windows are
        skipped and this run's completed windows are saved back — a re-pull never
        re-burns the Dhan quota on data already in the lake
        (DEC-FETCH-COVERAGE-WIRING-001). Dry-run ignores it (null sink).

    Returns
    -------
    PullCandlesReport
        Per-fetched-interval completed / failed / no-data counts + the
        ``dry_run`` flag.

    Raises
    ------
    ValueError
        If *instrument*'s ``symbol_name`` is empty / blank, or by
        :func:`resolve_candle_range` for a malformed date string or a
        ``from`` after ``to`` (incl. a future ``from_date``).
    """
    symbol = instrument.symbol_name
    if not symbol.strip():
        raise ValueError("instrument.symbol_name must be a non-empty engine symbol")

    start, end = resolve_candle_range(
        from_date=from_date,
        to_date=to_date,
        year=year,
        now=datetime.now(UTC),
    )
    resolved_year = start.year
    # Inclusive end label = the last day actually fetched (end is exclusive).
    end_label = (end - timedelta(days=1)).date().isoformat()

    use_live_rate = adapter_factory is None and not dry_run
    if adapter_factory is None:
        adapter_factory = _build_dry_run_adapter if dry_run else _build_live_pull_adapter
    adapter, sink = adapter_factory(lake_root, instrument)

    # Open the pull's correlation scope (DEC-DHAN-INGEST-LOGGING-001): ``pid`` is
    # bound into the structlog contextvars for the whole pass, so this start/done
    # summary, the ingest spine's ``ingest.*`` events, and the already-lit
    # transport (``dhan.call``) + rate (``rate.*``) events all share one
    # ``pull_id``. Always unbinds on exit (no leak).
    with pull_id_scope(pull_id) as pid:
        # Skip windows already in the lake on a live re-pull (no wasted Dhan quota);
        # DEC-FETCH-COVERAGE-WIRING-001.
        coverage = begin_pull_coverage(lake_root, dry_run=dry_run, coverage=coverage)
        # Reconcile the ledger against the lake (the source of truth): already-stored
        # windows are marked complete → skipped (no re-fetch / conflict flood);
        # windows the lake does not cover re-pull (DEC-LAKE-RECONCILE-001).
        if not dry_run and coverage is not None:
            reconcile_coverage_with_lake(
                coverage,
                lake_root,
                instruments=[instrument],
                intervals=DHAN_FETCH_INTERVALS,
                start=start.date(),
                end=end.date(),
            )
        # Per-window status (DEC-WINDOW-STATUS-001): browsable SUCCESS/EMPTY/FAILED.
        status_log = begin_pull_status(lake_root, dry_run=dry_run)
        # EMPTY-terminal (DEC-EMPTY-TERMINAL-001): a window seen genuinely EMPTY on
        # `threshold` separate pulls (e.g. SENSEX 1-minute before 2021-08-04) is
        # marked complete → skipped, so it stops re-fetching every run. Pure
        # (coverage + the cross-run status log), O(recorded windows) at pull start.
        if not dry_run and coverage is not None and status_log is not None:
            reconcile_empty_terminal(coverage, status_log)

        _LOG.info(
            "universe.pull.start",
            pull_id=pid,
            symbol=symbol,
            year=resolved_year,
            start_date=start.date().isoformat(),
            end_date=end_label,
            dry_run=dry_run,
            intervals=len(DHAN_FETCH_INTERVALS),
        )

        (pace, on_rate_limit, on_timeout, on_success), run_parallel, _rate_controller = (
            _live_rate_runtime(use_live_rate, session_rate)
        )
        report = run_dhan_spot_ingest(
            adapter,
            sink,
            instruments=[instrument],
            intervals=DHAN_FETCH_INTERVALS,
            start=start.date(),
            end=end.date(),
            pace=pace,
            on_rate_limit=on_rate_limit,
            on_timeout=on_timeout,
            on_success=on_success,
            on_progress=on_progress,
            coverage=coverage,
            status_log=status_log,
            run_parallel=run_parallel,
        )
        end_pull_coverage(coverage, lake_root, dry_run=dry_run)
        end_pull_status(status_log, lake_root, dry_run=dry_run)
        _observe_rate(rate_observer, _rate_controller, adapter)

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
            pull_id=pid,
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
    "SessionRateController",
    "SymbolPullResult",
    "TimeframeResult",
    "dhan_creds_present",
    "parse_symbol_list",
    "pull_all_spot",
    "pull_candles",
    "resolve_candle_range",
    "resolve_pull_symbols",
]
