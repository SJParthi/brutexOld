"""Click-to-pull controller — ingest ONE instrument's spot candle data (Groww).

Powers the F&O Universe Explorer's "⤓ Pull" button
(DEC-UNIVERSE-DASHBOARD-001). The controller is **instrument-agnostic**: it
pulls spot OHLCV for any resolved :class:`~brutex.models.Instrument` — a spot
index/stock or (lake-side) a contract — keyed by ``symbol_name`` (the engine
symbol; ADR-0003). When the operator clicks a universe row and hits pull,
:func:`pull_candles` ingests that instrument's candles across the
**fetched** base timeframes
(:data:`GROWW_FETCH_INTERVALS` — **``1day`` only during the verify-first phase**,
DEC-PULL-1DAY-FIRST-001; ``1minute`` + the 19 intraday timeframes resampled from
it return once the OHLCV-vs-bhavcopy check passes), reusing the proven
:func:`~brutex.adapters.groww.run_ingest.run_groww_spot_ingest` orchestration
core (CLAUDE.md §11 COMMON — no copy-paste of the fan-out/quarantine spine).

PR-J4b vendor cutover (DEC-DHAN-GROWW-REMIGRATE-001, ADR-0009 §5): the SPOT
pull + instrument-master load now run on Groww — the J2-equivalent the
dashboard never got (J3 only swapped the credentials gate). PR-J4c then darkened
the vestigial previous-vendor TOKEN-LIFECYCLE seams (``start_token_keepalive`` /
``refresh_live_token`` / the holder + keepalive setters / the re-exported
Groww credentials gate): the dashboard authenticates via Groww TOTP, so those
previous-vendor-only helpers were removed here. The retired vendor's adapter
package has since been deleted; this module imports only Groww.

## Offline-first

The default and the CI-tested path is **dry-run**: a
``SyntheticSpotAdapter`` (the vendor-neutral
:class:`~brutex.adapters.ingest_common.SyntheticSpotAdapter`, aliased
``SyntheticSpotAdapter`` for back-compat) emits valid in-session candle rows
into a :class:`~brutex.data.sink.NullBarSink` so the whole fetch →
session-filter → sink pipeline runs with NO credentials and NO network. The
LIVE Groww wiring lives in :func:`_build_live_spot_adapter`, which is
``# pragma: no cover`` because it needs a real TOTP token + the Groww SDK. The
network is blocked in CI, so the live path is intentionally not unit-covered —
its pure orchestration is exercised end-to-end through the dry-run path
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

from brutex.adapters.groww.chunks import window_days_for_interval as groww_window_days_for
from brutex.adapters.groww.run_ingest import (
    GrowwIngestReport,
    ProgressHook,
    run_groww_spot_ingest,
    run_groww_spot_ingest_with_retry,
)
from brutex.adapters.ingest_common import (
    AUTO_RESUME_MAX_ROUNDS,
    GROWW_COVERAGE_MARKET,
    PullVerdict,
    SyntheticSpotAdapter,
    UntilCleanRound,
    classify_pull,
    run_until_clean,
)
from brutex.adapters.protocol import MarketAdapter
from brutex.data.sink import BarSink, NullBarSink
from brutex.data.tf_autoderive import autoderive_enabled
from brutex.data.window_ledger_store import (
    begin_pull_ledger,
    end_pull_ledger,
    make_live_ledger_flush,
    plan_windows,
    reconcile_ledger_with_lake,
)
from brutex.models import Instrument
from brutex.observability.correlation import pull_id_scope
from brutex.orchestrator.parallel import (
    DEFAULT_LATENCY_BUDGET_S,
    PoolRunParallel,
    workers_to_saturate,
)
from brutex.orchestrator.quota_tracker import DailyQuotaExceeded
from brutex.orchestrator.universe_phases import RunParallel

if TYPE_CHECKING:
    from brutex.adapters.groww.run_ingest import SpotIngestWithRetry
    from brutex.adapters.rate_common import RateController

_LOG = structlog.get_logger(__name__)

#: The Track-1 **operational** pull floor — the start of the rolling
#: "2020-01-01 → today" range. The vendor capability reaches back to 2020-01-01
#: (``GrowwMarketAdapter.earliest_history``; CLAUDE.md §6 "Historical depth — From
#: 2020"), and the operator-run Phase-0 granularity probe CONFIRMED 1-minute NIFTY
#: data exists from 2020-01-01T09:15:00 (8297 rows in 2020-Jan), so the floor is set
#: to the full vendor depth (DEC-PULL-FLOOR-2020-001, operator decision 2026-06-13 —
#: supersedes the prior 2021-01-01 conservative floor of DEC-DHAN-PULL-SCOPE-001 now
#: that the 2020 edge is probe-verified). Any ``from_date`` below this floor is
#: clamped up to it so a too-early pick can never waste quota.
GROWW_HISTORY_FLOOR: Final[date] = date(2020, 1, 1)

#: Spot-pull interval selection (DEC-PULL-1DAY-FIRST-001, operator decision
#: 2026-06-13). The operator picks the fetch granularity per pull via a
#: ``/universe`` dropdown — **1-day only** (the verify-first default), **1-minute
#: only**, or **both** — so they can pull 1-day first, cross-check it against the
#: official NSE/BSE bhavcopy (CLAUDE.md §6), THEN choose 1-minute / both, with no
#: code change. The three canonical choice tokens:
SPOT_INTERVAL_DAY: Final[str] = "1day"
SPOT_INTERVAL_MINUTE: Final[str] = "1minute"
SPOT_INTERVAL_BOTH: Final[str] = "both"

#: Dropdown choice → the fetched-interval tuple. ``both`` keeps the canonical
#: display order (1m before 1d; the 19 intraday TFs are resampled from the stored
#: 1-minute base, never fetched — DEC-DHAN-1M-ONLY-001).
SPOT_INTERVAL_CHOICES: Final[dict[str, tuple[str, ...]]] = {
    SPOT_INTERVAL_DAY: ("1day",),
    SPOT_INTERVAL_MINUTE: ("1minute",),
    SPOT_INTERVAL_BOTH: ("1minute", "1day"),
}

#: The verify-first DEFAULT the pull functions use when no explicit ``intervals``
#: is passed: **1-day only**. One source of truth shared by callers + tests.
GROWW_FETCH_INTERVALS: Final[tuple[str, ...]] = SPOT_INTERVAL_CHOICES[SPOT_INTERVAL_DAY]


def resolve_fetch_intervals(choice: str | None) -> tuple[str, ...]:
    """Map a spot-interval dropdown *choice* to the fetch-interval tuple.

    ``"1day"`` → ``("1day",)``, ``"1minute"`` → ``("1minute",)``, ``"both"`` →
    ``("1minute", "1day")``. A blank / ``None`` / unrecognised choice falls back to
    the verify-first **1-day only** default (:data:`GROWW_FETCH_INTERVALS`) — it
    NEVER silently widens to 1-minute (DEC-PULL-1DAY-FIRST-001 / Golden Rule #1).

    Parameters
    ----------
    choice
        The dropdown / query-param value, or ``None`` when unspecified.

    Returns
    -------
    tuple[str, ...]
        The intervals to fetch, in display order.
    """
    if choice is None:
        return GROWW_FETCH_INTERVALS
    return SPOT_INTERVAL_CHOICES.get(choice.strip(), GROWW_FETCH_INTERVALS)


#: Groww canonical interval → lake ``TF_LABELS`` display label, so the
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
       defaults to :data:`GROWW_HISTORY_FLOOR`; a missing *to_date*
       defaults to the **previous day** (``today - 1``), so a pull never
       grabs the still-forming current session by default. An EXPLICIT
       *to_date* IS honoured (clamped to ``today``), so the operator can
       deliberately include today by picking it. The pair is then clamped
       to ``[GROWW_HISTORY_FLOOR, today]`` so a too-early or future pick can
       never waste quota — and the future is never requested.
    2. **Year** — when neither date is given but *year* is: that full
       calendar year, **clamped to ``[GROWW_HISTORY_FLOOR, yesterday]``**.
       A past year resolves to the whole year; the current year stops at
       the previous day (never today / the future); a wholly-future or
       pre-2020 year raises "nothing to pull".
    3. **Default** — neither date nor year: the FULL Track-1 history,
       ``[GROWW_HISTORY_FLOOR (2020-01-01), previous day]``. (Previously
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
    # reach today (clamped below). yesterday < GROWW_HISTORY_FLOOR is impossible
    # in practice (today is well past 2020) but the start>end guard covers it.
    yesterday = today - timedelta(days=1)

    # An EXPLICIT to_date may reach today; every other mode (year / default /
    # missing to_date) stops at the previous day so a routine pull never grabs
    # the still-forming current session. ``ceiling`` is the upper clamp.
    explicit_to = to_date is not None
    ceiling = today if explicit_to else yesterday

    if from_date is None and to_date is None:
        # Mode 2/3 — an explicit *year* picks that full calendar year; absent a
        # year the default is the FULL Track-1 history (2020-01-01 → previous
        # day), NOT the current year. Both fall through the SAME clamp below so
        # the window never reaches today/the future and a wholly-future /
        # pre-2020 year raises "nothing to pull" (DEC-PULL-FUTURE-WINDOW-001).
        if year is not None:
            start_date = date(year, 1, 1)
            end_date = date(year, 12, 31)
        else:
            start_date = GROWW_HISTORY_FLOOR
            end_date = yesterday
    else:
        start_date = _parse_iso_date(from_date, "from_date") if from_date else GROWW_HISTORY_FLOOR
        # An explicit to_date is honoured (clamped to today via ``ceiling`` —
        # explicit today IS allowed); a missing to_date defaults to the
        # previous day.
        end_date = _parse_iso_date(to_date, "to_date") if to_date else yesterday

    # Clamp into the vendor-served window; never request the future. Applies to
    # EVERY mode (the year/default branch was previously the one unclamped path
    # — the live-crash root cause). Only an explicit to_date may reach today;
    # otherwise the ceiling is the previous day.
    start_date = max(start_date, GROWW_HISTORY_FLOOR)
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


def default_date_range(now: datetime | None = None) -> tuple[str, str]:
    """The canonical default ``(from_iso, to_iso)`` the date pickers pre-fill everywhere.

    DEC-DEFAULT-DATE-RANGE-001. Reuses :func:`resolve_candle_range` with ALL bounds
    blank, so the pre-filled UI dates are GUARANTEED byte-identical to what a blank
    spot / options pull (or report) actually fetches — ONE source of truth for "the
    default range is ``[2020-01-01, previous trading day]``" across the whole
    dashboard. ``to`` is the INCLUSIVE previous day (``resolve_candle_range``'s
    ``end`` is the exclusive half-open boundary, so subtract a day to display it).

    Parameters
    ----------
    now
        Injected "current" instant (defaults to :func:`datetime.now` UTC) so the
        "previous day" is deterministic in tests and stays current across midnight
        in production (the render path recomputes per request).

    Returns
    -------
    tuple[str, str]
        ``(from_iso, to_iso)`` ISO ``YYYY-MM-DD`` — e.g. ``("2020-01-01",
        "2026-06-07")`` when "today" is 2026-06-08.
    """
    start, end = resolve_candle_range(
        from_date=None,
        to_date=None,
        year=None,
        now=now if now is not None else datetime.now(UTC),
    )
    return start.date().isoformat(), (end - timedelta(days=1)).date().isoformat()


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

    The backend half of the unified multi-select SPOT pull
    (DEC-UNIVERSE-OPTIONS-MULTIPULL-001): the page sends the ticked underlyings
    as ``?symbols=a,b,c``; this resolves each to its
    :class:`~brutex.models.Instrument` so ONE
    :func:`~brutex.dashboard.pull_controller.pull_all_spot` pass fans them
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


#: A factory that builds the ``(adapter, sink)`` pair the Groww ingest spine
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
        :data:`GROWW_FETCH_INTERVALS` order (``1d`` only during the
        verify-first phase, DEC-PULL-1DAY-FIRST-001; ``1m`` then ``1d``
        once 1-minute is re-enabled). The 19 derived intraday timeframes
        are resampled from the 1-minute base downstream, not fetched here
        (DEC-DHAN-1M-ONLY-001).
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
    """Build the offline ``(SyntheticSpotAdapter, NullBarSink)`` pair.

    The synthetic adapter emits valid in-session bars and the null sink
    counts-but-discards them, so the pull runs with zero dependencies — the
    dry-run proof the spine works (DEC-VENDOR-PIVOT-001).
    """
    return SyntheticSpotAdapter(), NullBarSink()


def _build_live_spot_adapter(  # pragma: no cover — live Groww TOTP token + SDK + network
    lake_root: Path,
    instruments: Sequence[Instrument],
) -> tuple[MarketAdapter, BarSink]:
    """Wire the real Groww client + a :class:`GrowwMarketAdapter` for the spot pull.

    PR-J4b vendor cutover (DEC-DHAN-GROWW-REMIGRATE-001, ADR-0009 §5): the
    dashboard live spot pull now runs on Groww — the J2-equivalent the dashboard
    never got (J3 only swapped the credentials gate). Mirrors the CLI's
    :func:`~brutex.cli._build_live_groww_spot` exactly (CLAUDE.md §11 COMMON):
    resolve the Groww TOTP credentials (env-first, file-fallback) via
    :func:`~brutex.config.credentials.resolve_groww_file_credentials`, build a
    live SDK-backed client through the
    :func:`~brutex.adapters.groww.live.build_live_client` composition seam, load
    the Groww instrument master into the O(1)
    :class:`~brutex.adapters.groww.instruments.InstrumentIndex` (the adapter
    resolves every fetched symbol through it), and write through a
    :class:`~brutex.data.sink.ParquetBarSink` over *lake_root*.

    Unlike the retired builder there is NO ``transport`` to inject — Groww
    talks through its SDK client, not a urllib transport — and the *instruments*
    are NOT threaded into the adapter: it carries the whole master index and
    resolves each fetched symbol in O(1). *instruments* is kept in the signature
    so the single-instrument / bulk builder seams stay shape-identical
    (the fan-out worklist still drives WHICH symbols are pulled).
    ``# pragma: no cover`` — needs real credentials + network, absent in CI.
    """
    from brutex.adapters.groww.adapter import GrowwMarketAdapter
    from brutex.adapters.groww.auth import GrowwCredentials
    from brutex.adapters.groww.instrument_loader import load_index, urllib_http_get
    from brutex.adapters.groww.live import build_live_client
    from brutex.config.credentials import resolve_groww_file_credentials
    from brutex.data.parquet_writer import ParquetWriter
    from brutex.data.sink import ParquetBarSink

    del instruments  # the Groww adapter carries the whole master index (O(1) resolve)
    file_creds = resolve_groww_file_credentials()
    creds = GrowwCredentials(api_key=file_creds.api_key, totp_secret=file_creds.totp_secret)
    client = build_live_client(creds)
    index = load_index(urllib_http_get)
    adapter = GrowwMarketAdapter(client=client, index=index)
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
    (``1m`` + ``1d``) over the full ``[2020-01-01, previous day]`` range
    (DEC-PULL-FLOOR-2020-001) — the current session is excluded by default.
    """

    dry_run: bool
    start_date: str
    end_date: str
    symbols: list[SymbolPullResult] = field(default_factory=list)
    auth_aborted: bool = False
    """True if the Groww token died mid-pull (DEC-DHAN-AUTH-FAILFAST-RENEW-001) —
    drives the loud ``auth_expired`` verdict (DEC-PULL-VERDICT-001)."""
    auth_cause: str | None = None
    """The first dead-token cause string, carried onto the ``auth_expired`` verdict."""
    still_failed_windows: int = 0
    """Windows still failing after the in-run retry budget — deferred to the next pull."""
    quota_exhausted: bool = False
    """True if the pull paused on the daily Groww quota — the auto-resume's
    between-round brake (DEC-AUTO-RESUME-QUOTA-001) OR a mid-round
    ``DailyQuotaExceeded`` (DEC-SPOT-QUOTA-MIDROUND-001) — drives the loud
    ``quota_exhausted`` verdict (resume after the IST-midnight reset)."""

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

    @property
    def verdict(self) -> PullVerdict:
        """The prioritised, actionable verdict for this pull (DEC-PULL-VERDICT-001).

        Turns the raw tallies into one operator instruction so a token-stuck or
        retry-residual pull never sits silent — ``auth_expired`` (regenerate the
        token), ``transient_residual`` (re-pull / auto-retry finishes),
        ``known_gap_only`` (reference symbols with no intraday), or ``clean``.
        """
        return classify_pull(
            auth_aborted=self.auth_aborted,
            still_failed_windows=self.still_failed_windows,
            failed_pairs=self.total_failed,
            no_data=self.total_no_data,
            completed=self.total_completed,
            auth_cause=self.auth_cause,
            quota_exhausted=self.quota_exhausted,
        )

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
            "verdict": self.verdict.to_dict(),
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
    """Build the offline ``(SyntheticSpotAdapter, NullBarSink)`` pair for a bulk pull."""
    return SyntheticSpotAdapter(), NullBarSink()


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


def _spot_run_parallel(controller: RateController | None) -> RunParallel | None:
    """Size the spot fan-out pool the SAME way as every other Groww pull (uniform).

    DEC-DHAN-PARALLEL-INGEST-001 + DEC-SPOT-POOL-UNIFORM-001. The historical/spot
    pull uses the IDENTICAL Little's-law sizing as the expiry/contract pulls —
    ``workers_to_saturate(current_rps, DEFAULT_LATENCY_BUDGET_S)`` (bounded by the
    shared :data:`~brutex.orchestrator.parallel.MAX_FANOUT` = 32) — NO spot-special
    widening. The earlier spot-only 96-wide pool (DEC-SPOT-POOL-WIDTH-15S-001,
    reverted here) HUNG: the ONE shared ``GrowwAPI`` SDK that every pull funnels
    through does not tolerate ~96 simultaneous connections, and historical is the
    only pull that ever pushed that high. Expiries/contracts have always worked on
    this modest pool, so spot uses it too. ``None`` controller (dry-run /
    injected-factory pull) → ``None`` here, so ``run_groww_spot_ingest`` uses its
    default pool. The per-second issue rate is the shared token bucket's job (Rule
    #6), unchanged. This is the pure, covered sizing seam; the live controller
    construction is the ``# pragma: no cover`` part.
    """
    if controller is None:
        return None
    workers = workers_to_saturate(controller.current_rps or 8.0, DEFAULT_LATENCY_BUDGET_S)
    return PoolRunParallel(max_workers=workers)


def _build_live_controller() -> RateController:
    """Construct the shared adaptive [3..8] r/s controller (the session factory).

    The controller starts at the operator-DIRECTED 8 r/s ceiling
    (:data:`~brutex.adapters.groww.constants.GROWW_RATE_LIMIT_RPS_CEILING`,
    DEC-GROWW-RATE-CEILING-8-001) — the per-second backtesting cap stays officially
    UNVERIFIED; 8×60 = 480/min sits just under the 500/min governor so the
    per-second ceiling is the binding gate. The adaptive AIMD ladder is unchanged:
    a 429 burst still steps 8→…→3 (soft floor) and recovers back to 8.

    The controller is threaded into BOTH the per-window pacing hooks (``pace`` /
    ``on_rate_limit`` / ``on_success``) AND the pool sizing
    (:func:`_spot_run_parallel`), so the fan-out width tracks the controller's
    current rate (DEC-DHAN-ADAPTIVE-RPS-001 + DEC-DHAN-PARALLEL-INGEST-001). Built
    ONCE per dashboard session and reused across pulls via
    :class:`SessionRateController` (DEC-DHAN-PERSIST-RATE-001) so the learned rate
    carries over instead of every pull restarting at the 8 r/s ceiling.

    The controller is built with **NO** :class:`~brutex.orchestrator.quota_tracker.\
DailyQuotaTracker` (DEC-GROWW-NO-DAILY-QUOTA-001): Groww backtesting
    ``get_historical_candles`` has **no daily quota** (operator-confirmed 2026-06-13),
    so the dashboard pull no longer imposes the phantom 100,000/day cap that
    DEC-AUTO-RESUME-QUOTA-001 had wired — that cap would have wrongly HALTED a large
    multi-day backfill (e.g. the full option-chain pull, which exceeds 100k calls/day).
    This matches the already-merged CLI Groww spot path (``cli.py`` also passes
    ``quota_tracker=None``). The per-minute 500/min governor + the adaptive per-second
    bucket remain the live pacing gates. Consequence: the controller's
    :meth:`~brutex.adapters.rate_common.RateController.quota_exhausted` is always
    ``False`` (no tracker), so the auto-resume ``stop_quota`` branch is DORMANT for the
    dashboard pull (it never burns a non-existent quota); the separate read-only
    ``/api/v1/quota`` endpoint keeps its own informational tracker (display only, no
    enforcement) and is unaffected.
    """
    from brutex.adapters.groww.constants import (
        GROWW_RATE_LIMIT_PER_MIN,
        GROWW_RATE_LIMIT_RPS_CEILING,
    )
    from brutex.adapters.rate_common import RateController

    # Operator-confirmed Groww backtesting limits (DEC-GROWW-NO-DAILY-QUOTA-001):
    # 500 req/min (the HARD per-minute governor, DEC-GROWW-RATE-PERMIN-001) AND NO
    # daily quota — so NO DailyQuotaTracker is wired here, matching the CLI Groww
    # spot path (cli.py also builds the controller with quota_tracker=None). The
    # phantom 100,000/day cap would have wrongly HALTED a large multi-day backfill,
    # since Groww backtesting has no daily quota (operator-confirmed 2026-06-13).
    # The per-second start AND upshift cap is the operator-DIRECTED 8 r/s ceiling
    # (GROWW_RATE_LIMIT_RPS_CEILING, DEC-GROWW-RATE-CEILING-8-001) — 8×60 = 480/min
    # stays under the 500/min governor, so the per-second ceiling is the binding gate
    # without ever fighting the per-minute cap; the AIMD ladder still steps DOWN on
    # 429s. Supersedes the prior implicit 5 r/s ceiling (rate_common.CEILING_RPS).
    return RateController(
        ceiling_rps=GROWW_RATE_LIMIT_RPS_CEILING,
        ceiling_per_min=GROWW_RATE_LIMIT_PER_MIN,
    )


class SessionRateController:
    """Persist ONE :class:`RateController` across pulls in a dashboard session.

    DEC-DHAN-PERSIST-RATE-001. Each ``POST /universe/pull`` used to build a FRESH
    controller at the 8 r/s ceiling (:func:`_build_live_controller`, the
    operator-DIRECTED ceiling DEC-GROWW-RATE-CEILING-8-001), so every symbol-pull
    re-opened at 8 r/s and re-hit Groww's wall — the 429 storm the operator saw at
    the start of *every* pull, re-learnt per symbol. This holder builds the
    controller ONCE (lazily, thread-safe) and hands the SAME instance to every pull,
    so the rate the system LEARNED (e.g. stepped down to 3 r/s) CARRIES OVER: symbol
    2, 3, 4… start at the learned rate, not 8. The controller is itself thread-safe
    (DEC-DHAN-PARALLEL-INGEST-001), so sharing it across sequential AND concurrent
    pulls correctly keeps the AGGREGATE issue rate ≤ 8 r/s (Rule #6) — concurrent
    pulls now share one global budget instead of each running its own 8 r/s.
    """

    def __init__(self, factory: Callable[[], RateController] | None = None) -> None:
        """Build a holder. *factory* (a test seam) defaults to :func:`_build_live_controller`."""
        self._factory = factory if factory is not None else _build_live_controller
        self._controller: RateController | None = None
        self._lock = threading.Lock()

    def get(self) -> RateController:
        """Return the session's controller, building it ONCE on first use (thread-safe)."""
        with self._lock:
            if self._controller is None:
                self._controller = self._factory()
            return self._controller


#: The dashboard rate-panel observer (DEC-DASHBOARD-RATE-PANEL-001): given a
#: ``RateController.snapshot()`` (current_rps + recent_hits) and a
#: ``CallMeter.snapshot()`` (effective_rps + latency + the instantaneous
#: ``rps_1s`` / ``rps_1s_max`` gauge — DEC-DASHBOARD-RPS1S-PANEL-001), push both
#: to the panel.
RateObserver = Callable[[Mapping[str, Any], Mapping[str, Any]], None]


def _observe_rate(
    rate_observer: RateObserver | None,
    controller: RateController | None,
    adapter: MarketAdapter,
) -> None:
    """Push a live pull's adaptive rate + measured throughput to the dashboard panel.

    A no-op for a dry-run / injected-factory pull (no *controller*) or when no
    observer is wired (the default). On a live pull it sends the controller's
    ``snapshot()`` (current_rps + recent 429s) and the transport ``CallMeter``'s
    ``snapshot()`` (effective_rps + latency p95 + the instantaneous
    ``rps_1s`` / ``rps_1s_max`` gauge) to *rate_observer* (``make_rate_observer`` —
    DEC-DASHBOARD-RATE-PANEL-001 + DEC-DASHBOARD-RPS1S-PANEL-001), so the operator
    SEES the live per-second issue rate + its peak on the dashboard, not just in
    ``/logs``.
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
) -> tuple[_RateHooks, RunParallel | None, RateController | None]:
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
    from_date: str | None = None,
    to_date: str | None = None,
    year: int | None = None,
    intervals: tuple[str, ...] = GROWW_FETCH_INTERVALS,
    adapter_factory: BulkAdapterFactory | None = None,
    on_progress: ProgressHook | None = None,
    pull_id: str | None = None,
    rate_observer: RateObserver | None = None,
    session_rate: SessionRateController | None = None,
    auto_retry: bool = False,
    until_clean: bool = False,
    until_clean_rounds: int = AUTO_RESUME_MAX_ROUNDS,
) -> BulkSpotReport:
    """Bulk-pull EVERY instrument's spot over the full ``[2020-01-01, previous day]`` range.

    The one-click "⤓ Pull ALL indices / stocks spot" button core: one
    :func:`~brutex.adapters.groww.run_ingest.run_groww_spot_ingest` pass over
    the whole *instruments* list and the fetched base intervals
    (the injected *intervals*, defaulting to :data:`GROWW_FETCH_INTERVALS` —
    ``1d`` only during the verify-first phase, DEC-PULL-1DAY-FIRST-001), so the
    live adapter is built ONCE with the full universe (O(1) index) and the fan-out quarantines
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
        ``[2020-01-01, previous day]`` window; the current session is excluded.
    from_date, to_date, year
        Optional range selectors (DEC-DHAN-SPOT-MULTI-SATURATE-001) threaded to
        :func:`resolve_candle_range` so the multi-select "Pull spot" bulk route
        can pull the operator's date-picker window through this saturating
        cross-symbol flatten. When **all three are ``None``** (the one-click
        ``?kind=`` "Pull ALL" callers) the full Track-1 history
        ``[2020-01-01, previous day]`` applies — byte-for-byte the prior
        behaviour. An explicit ``from_date`` / ``to_date`` (inclusive ISO
        ``YYYY-MM-DD``, clamped to ``[2020-01-01, today]``) takes precedence over
        ``year`` (a full calendar year); a malformed date or ``from`` after
        ``to`` raises :class:`ValueError` (the route maps it to a 400).
    adapter_factory
        Override the bulk ``(adapter, sink)`` builder; defaults to the
        synthetic builder when *dry_run* else the live builder.
    auto_retry
        When ``True`` (the dashboard "Retry failed windows" button / the manual
        recovery path), leftover failed windows are auto-re-pulled in bounded
        rounds after the pull — only the still-failed windows, via the shared
        window ledger (rate-safe + idempotent; DEC-INGEST-AUTO-RETRY-001).
        ``False`` (default — the one-click Pull-ALL) runs a single pass, the prior
        behaviour. Either way the per-symbol rollup is computed from the ORIGINAL
        pass's report.
    until_clean
        When ``True`` (``brutex fetch --until-clean`` / the dashboard "until clean"
        option), keep running bounded auto-retry rounds — each one a full
        pull-pass-with-auto-retry over the SHARED window ledger, reusing
        :func:`~brutex.adapters.ingest_common.retry_failed_windows` (NO
        duplicated round loop) — until the ledger is CLEAN, OR the token dies (stop
        + ``auth_expired`` verdict), OR ``until_clean_rounds`` is spent
        (DEC-TOKEN-PREFLIGHT-001). ``False`` (default — the one-click Pull-ALL)
        runs the single ``auto_retry`` pass, the prior behaviour. The per-symbol
        rollup is still computed from the FIRST pass's report; the aggregate
        ``still_failed_windows`` / ``auth_aborted`` reflect the FINAL state.
    until_clean_rounds
        Bounded OUTER round budget for *until_clean* (default
        :data:`~brutex.adapters.ingest_common.AUTO_RESUME_MAX_ROUNDS` — the
        auto-resume budget, DEC-AUTO-RESUME-QUOTA-001). Never infinite (Golden
        Rule #10): the loop self-terminates on clean / token-dead / quota-spent /
        no-progress well before this cap, which is only the final backstop.

    Returns
    -------
    BulkSpotReport
        Per-symbol completed / failed / no-data counts + the aggregate totals.
    """
    instruments = list(instruments)
    # Range resolution (DEC-DHAN-SPOT-MULTI-SATURATE-001): when NO range is given
    # (the one-click "Pull ALL" caller) default to the full Track-1 history
    # ``[2020-01-01, previous day]`` — the prior behaviour, unchanged. When the
    # caller DOES pass an explicit ``from_date`` / ``to_date`` / ``year`` (the
    # multi-select "Pull spot" bulk route, which carries the operator's date
    # picker), honour it verbatim via :func:`resolve_candle_range` so routing the
    # multi-select through this saturating flatten never silently widens the range.
    if from_date is None and to_date is None and year is None:
        from_date = GROWW_HISTORY_FLOOR.isoformat()
    start, end = resolve_candle_range(
        from_date=from_date,
        to_date=to_date,
        year=year,
        now=now if now is not None else datetime.now(UTC),
    )
    use_live_rate = adapter_factory is None and not dry_run
    if adapter_factory is None:
        adapter_factory = _build_dry_run_bulk_adapter if dry_run else _build_live_bulk_adapter
    adapter, sink = adapter_factory(lake_root, instruments)
    # Open the pull's correlation scope: ``pid`` is bound into the structlog
    # contextvars for the whole pass, so every event — this start/done summary,
    # the ingest spine's ``ingest.*`` events, AND the already-lit transport
    # (``groww.call``) + rate (``rate.*``) events — shares one ``pull_id``
    # (DEC-DHAN-INGEST-LOGGING-001). Always unbinds on exit (no leak).
    with pull_id_scope(pull_id) as pid:
        # The coverage reconcile band-aids (lake-reconcile / EMPTY-terminal /
        # FAILED-repull) are RETIRED (DEC-WINDOW-LEDGER-001, Stage d): the unified
        # ledger is the SOLE skip authority, so its own per-window record + the
        # cold-start `reconcile_ledger_with_lake` below replace every coarse
        # coverage-set patch (the FetchCoverage set was deleted in Stage e).
        # The legacy window_status.json sidecar is WRITE-RETIRED
        # (DEC-STATUS-SIDECAR-WRITE-RETIRE-001): the ledger below is the page's
        # primary source; the sidecar stays on disk read-only (db_view fallback
        # + the one-shot ledger migration) until the Stage-6 deletion.
        # Unified per-window ledger (DEC-WINDOW-LEDGER-001): load (one-shot
        # migrate) the durable Parquet ledger + PLAN every window the pull will
        # touch as PLANNED, so EVERY spot window has a DB row BEFORE any fetch.
        # The ingest then records each outcome into it. Persisted to
        # <lake>/window_ledger/ — the single source /coverage/windows surfaces.
        ledger = begin_pull_ledger(lake_root, dry_run=dry_run)
        if ledger is not None:
            plan_windows(
                ledger,
                instruments=instruments,
                intervals=intervals,
                start=start.date(),
                end=end.date(),
                market=GROWW_COVERAGE_MARKET,
                window_days_for=groww_window_days_for,
            )
            # Cold-start seed (DEC-WINDOW-LEDGER-001, Stage b1): on a fresh clone
            # the ledger is empty but the lake may already hold years of bars.
            # Seed SUCCESS for every PLANNED window the lake already encloses (and
            # only ABSENT rows — never overwriting a FAILED/PLANNED row) so the
            # upcoming is_skippable flip will NOT re-pull the entire history. A
            # non-None ledger already implies a live (non-dry-run) pull —
            # ``begin_pull_ledger`` returns None for dry_run — so no extra guard.
            # ``market=GROWW_COVERAGE_MARKET`` so the seeded rows share the Groww
            # spine's namespace (PR-J4b).
            reconcile_ledger_with_lake(
                ledger,
                lake_root,
                instruments=instruments,
                intervals=intervals,
                start=start.date(),
                end=end.date(),
                market=GROWW_COVERAGE_MARKET,
                window_days_for=groww_window_days_for,
            )

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
        # NO per-pull RenewToken here (DEC-DHAN-RENEW-SINGLE-OWNER-001). RenewToken
        # ROTATES — it mints a new token and INVALIDATES the presented one
        # (DEC-DHAN-RENEWTOKEN-ROTATE-001). A renew fired at EVERY pull start (with
        # NO <12h gate) needlessly killed a healthy live token, and — because the
        # client reads the shared TokenHolder, not the rebuilt ``access_token``
        # field — left the pull running on the just-invalidated token → DH-906
        # ``auth_expired``. The background 12h TokenKeepalive is now the SINGLE
        # renewer: it renews ONLY when <12h remain (learning the expiry offline from
        # the token's JWT ``exp`` — no needless rotation) AND writes the fresh token
        # into the holder the client reads (DEC-DHAN-TOKEN-KEEPALIVE-001).
        # Live ledger flush (DEC-WINDOW-STATUS-LIVE-001 semantics on the LEDGER):
        # flush the unified ledger MID-pull (adaptive throttle) after each
        # completed pair, so /coverage/windows ticks up live instead of staying
        # 0 until pull end. The legacy status-sidecar flush was write-retired
        # (DEC-STATUS-SIDECAR-WRITE-RETIRE-001).
        ledger_flush = make_live_ledger_flush(ledger, lake_root)

        def _progress_then_flush(label: str, bars: int, failed: bool) -> None:
            if on_progress is not None:
                on_progress(label, bars, failed)
            ledger_flush()

        # The "Retry failed windows" button passes ``auto_retry=True`` so leftover
        # failed windows are auto-re-pulled in bounded rounds AFTER the pull (only
        # the still-failed windows, via the shared window ledger — rate-safe,
        # idempotent; DEC-INGEST-AUTO-RETRY-001). The default one-click Pull-ALL
        # keeps ``auto_retry=False`` (a single pass) — byte-for-byte the prior
        # behaviour. BOTH paths share the same ingest spine (CLAUDE.md §11 COMMON);
        # ``report`` is always the ORIGINAL pass's report for the per-symbol rollup.
        passes: list[SpotIngestWithRetry] = []

        def _one_pass(do_retry: bool) -> SpotIngestWithRetry:
            """Run ONE full pull-pass-with-auto-retry over the SHARED window ledger.

            The single ingest call site (CLAUDE.md §11 COMMON) — the one-shot path,
            every until-clean round, and the auto-retry inner loop all go through
            here, so the args can never drift between them. Every COMPLETED pass is
            recorded in ``passes`` so a mid-round quota death can still report the
            last finished round's state (DEC-SPOT-QUOTA-MIDROUND-001).
            """
            res = run_groww_spot_ingest_with_retry(
                adapter,
                sink,
                instruments=instruments,
                intervals=intervals,
                start=start.date(),
                end=end.date(),
                pace=pace,
                on_rate_limit=on_rate_limit,
                on_timeout=on_timeout,
                on_success=on_success,
                on_progress=_progress_then_flush,
                ledger=ledger,
                run_parallel=run_parallel,
                auto_retry=do_retry,
            )
            passes.append(res)
            return res

        final_still_failed = 0
        final_auth_aborted = False
        final_auth_cause: str | None = None
        final_quota_exhausted = False
        try:
            try:
                # Re-pull-until-clean (DEC-TOKEN-PREFLIGHT-001): keep running bounded
                # rounds — each a full pass over the SHARED window ledger (so it
                # re-fetches ONLY the still-failed windows; rate-safe + idempotent) —
                # until clean / token dead / outer budget spent. Reuses
                # ``run_until_clean`` over the SAME ``retry_failed_windows`` engine;
                # the round COUNT is bounded (never an O(1)-total claim). The
                # per-symbol rollup uses the FIRST pass's report; the aggregate
                # residual / auth state reflect the FINAL round.
                if until_clean:

                    def _round_runner() -> UntilCleanRound:
                        """One until-clean round = one full pull pass.

                        Groww's report has no ``auth_cause`` string (no fail-fast
                        latch — the documented divergence; it DERIVES ``auth_aborted``
                        from the quarantined failures), so the round leaves
                        ``auth_cause`` at its ``None`` default (mirrors the CLI).
                        """
                        res = _one_pass(True)
                        return UntilCleanRound(
                            still_failed_windows=res.report.still_failed_windows,
                            auth_aborted=res.report.auth_aborted,
                        )

                    # Auto-resume (DEC-AUTO-RESUME-QUOTA-001): keep re-firing batches
                    # with NO clicks until the ledger is 100% clean, stopping only on
                    # a real limit — token dead (existing), the daily quota spent
                    # (the mandatory brake; ``_rate_controller.quota_exhausted``
                    # reads the persisted tracker), or a no-progress round
                    # (``stop_when_stuck`` — what makes the large budget safe).
                    # Dry-run / injected-factory pulls have no controller → quota
                    # hook is ``None`` (never the limit), unchanged.
                    quota_hook = (
                        _rate_controller.quota_exhausted if _rate_controller is not None else None
                    )
                    until_outcome = run_until_clean(
                        _round_runner,
                        max_rounds=until_clean_rounds,
                        quota_exhausted=quota_hook,
                        stop_when_stuck=True,
                    )
                    final_still_failed = until_outcome.final_still_failed
                    final_auth_aborted = until_outcome.auth_aborted or (
                        bool(passes) and passes[0].report.auth_aborted
                    )
                    # Groww has no auth_cause string (the documented divergence) — the
                    # cause stays None; auth_aborted alone drives the loud verdict.
                    final_auth_cause = None
                    final_quota_exhausted = until_outcome.stopped_on_quota
                else:
                    res = _one_pass(auto_retry)
                    final_still_failed = res.report.still_failed_windows
                    final_auth_aborted = res.report.auth_aborted
                    final_auth_cause = None
            except DailyQuotaExceeded as exc:
                # The 100k daily quota died MID-round (DEC-SPOT-QUOTA-MIDROUND-001,
                # the spot arm of the options DEC-OPTIONS-UNTIL-CLEAN-001 mapping):
                # the until-clean loop's quota hook can only brake at round
                # boundaries, so a worker's pace bucket raises this mid-pass — and
                # it previously escaped as a generic job error BEFORE the ledger /
                # status persist. Everything already committed survives: bars per
                # pair via the sink, window records via the throttled flush + the
                # finally below. Map to the loud quota verdict instead of a generic
                # job error; the next pull (after IST midnight) resumes
                # incrementally over the SAME ledger.
                final_quota_exhausted = True
                final_still_failed = passes[-1].report.still_failed_windows if passes else 0
                final_auth_aborted = passes[-1].report.auth_aborted if passes else False
                # Groww has no auth_cause string (the documented divergence) — None.
                final_auth_cause = None
                _LOG.warning(
                    "universe.pull_all.quota_exhausted",
                    pull_id=pid,
                    error=str(exc),
                    rounds_completed=len(passes),
                    note=(
                        "daily quota spent mid-round — committed work kept; "
                        "resume after IST midnight"
                    ),
                )
        finally:
            # ALWAYS persist the ledger — a crash / abort / mid-round quota death
            # must never discard the settled-window history
            # (DEC-SPOT-QUOTA-MIDROUND-001; previously persisted only on a clean
            # return, shielded just by the throttled mid-pull flushes). Idempotent
            # with those flushes. The legacy status-sidecar persist that used to
            # nest here was write-retired (DEC-STATUS-SIDECAR-WRITE-RETIRE-001) —
            # the ledger is the skip authority (DEC-WINDOW-LEDGER-001).
            end_pull_ledger(ledger, lake_root, dry_run=dry_run)
        # Per-symbol rollup = the FIRST pass's report (the until-clean contract).
        # A quota death before ANY pass completed leaves no per-pair information —
        # the empty fallback rolls every symbol up as zeros (the quota verdict
        # carries the actionable state). Also fixes the latent ``passes[0]``
        # IndexError an ``until_clean_rounds=0`` no-op drive used to hit.
        report = passes[0].report if passes else GrowwIngestReport(pairs={}, failures=())
        _observe_rate(rate_observer, _rate_controller, adapter)
        # Groww has NO no-data marker — unlike the previous vendor's no-data 400
        # marker, which was special-cased as a benign KNOWN_GAP. Per Golden Rule #1 we
        # do not invent a Groww twin, so every terminal Groww error is a uniform terminal
        # quarantine → `failed` (`transient_residual`). A settled EMPTY window is
        # still surfaced as `no_data` below when a pair ran cleanly but returned zero
        # bars; there is no known-gap split (no_data from a terminal failure has no
        # Groww source).
        failure_keys = {(f.symbol, f.interval) for f in report.failures}
        results: list[SymbolPullResult] = []
        for instrument in instruments:
            symbol = instrument.symbol_name
            completed = failed = no_data = 0
            for fetch_interval in intervals:
                key = (symbol, fetch_interval)
                if key in failure_keys:
                    failed += 1
                else:
                    pair = report.pairs.get(key)
                    if pair is None:
                        # The quota died before this pair ran (the empty-report
                        # fallback above) — its outcome is UNKNOWN, so claim
                        # nothing (DEC-SPOT-QUOTA-MIDROUND-001).
                        continue
                    if pair.bars_appended > 0:
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
            auth_aborted=final_auth_aborted,
            auth_cause=final_auth_cause,
            still_failed_windows=final_still_failed,
            quota_exhausted=final_quota_exhausted,
        )
        verdict = bulk.verdict
        _LOG.info(
            "universe.pull_all.done",
            pull_id=pid,
            instruments=bulk.total_symbols,
            completed=bulk.total_completed,
            failed=bulk.total_failed,
            dry_run=dry_run,
            verdict=verdict.kind,
            pending_windows=verdict.pending_windows,
        )
        # Loud, actionable surface (DEC-PULL-VERDICT-001): when the operator must
        # act (a dead token, or windows still pending), emit ONE WARNING carrying
        # the headline + the exact next step + the pending count, so a stuck pull
        # is never silent in /logs (mirrors the loud ingest.auth_aborted alert).
        if verdict.needs_operator:
            _LOG.warning(
                "universe.pull_all.action_required",
                pull_id=pid,
                verdict=verdict.kind,
                headline=verdict.headline,
                action=verdict.action,
                pending_windows=verdict.pending_windows,
                auth_cause=verdict.auth_cause,
            )
        # AUTOMATIC derived-TF materialisation on settle (DEC-TF-MATERIALIZE-002):
        # re-derive the 19 intraday timeframes for every symbol that landed NEW
        # 1-minute bars — zero clicks, gated behind the BRUTEX_AUTO_DERIVE_TFS=1
        # opt-in (default OFF per the operator's 2026-06-11 storage decision).
        # Quarantined end-to-end: auto-derive can NEVER fail or slow a pull's
        # verdict (the pull already returned its bars + ledger state).
        if not dry_run and autoderive_enabled():  # pragma: no cover — raw lake I/O glue
            _autoderive_after_pull(
                instruments=instruments, bulk=bulk, lake_root=lake_root, pull_id=pid
            )
    return bulk


def _autoderive_after_pull(  # pragma: no cover — raw lake I/O glue; planner core is 100%-unit-tested
    *,
    instruments: Sequence[Instrument],
    bulk: BulkSpotReport,
    lake_root: Path,
    pull_id: str,
) -> None:
    """Re-materialise the 19 derived TFs for every symbol the pull refreshed.

    The pull-settle arm of DEC-TF-MATERIALIZE-002: binds the SHARED lake
    read/write builders (the same pair the manual derive route uses —
    CLAUDE.md §11 COMMON) per settled symbol and runs the quarantining
    planner. Wrapped so NO failure here can ever surface as a pull error —
    the pull's verdict is already final when this runs.
    """
    from brutex.data.tf_autoderive import plan_autoderive_symbols, run_autoderive
    from brutex.data.timeframe_materialize import (
        MATERIALIZABLE_TFS,
        MaterializeResult,
        make_lake_read_1m,
        make_lake_write_tf,
        materialize_timeframes,
    )

    try:
        by_symbol = {inst.symbol_name: inst for inst in instruments}
        planned = plan_autoderive_symbols(bulk.symbols)

        def _materialize_one(symbol: str) -> MaterializeResult:
            inst = by_symbol[symbol]
            exchange = inst.exch_id.value
            segment = inst.segment.value
            return materialize_timeframes(
                tfs=MATERIALIZABLE_TFS,
                read_1m=make_lake_read_1m(
                    lake_root=lake_root, exchange=exchange, segment=segment, symbol=symbol
                ),
                write_tf=make_lake_write_tf(
                    lake_root=lake_root, exchange=exchange, segment=segment, symbol=symbol
                ),
            )

        report = run_autoderive(symbols=planned, materialize_one=_materialize_one)
    except Exception as exc:
        _LOG.warning("universe.autoderive.error", pull_id=pull_id, error=str(exc))
        return
    _LOG.info(
        "universe.autoderive.done",
        pull_id=pull_id,
        symbols_planned=len(planned),
        symbols_ok=report.symbols_ok,
        symbols_failed=report.symbols_failed,
        derived_bars=report.total_bars,
    )
    if report.symbols_failed:
        _LOG.warning(
            "universe.autoderive.quarantined",
            pull_id=pull_id,
            symbols=[o.symbol for o in report.outcomes if not o.ok],
            note="auto-derive quarantined these symbols; the pull itself is unaffected",
        )


def pull_candles(
    instrument: Instrument,
    *,
    lake_root: Path,
    year: int | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    dry_run: bool,
    intervals: tuple[str, ...] = GROWW_FETCH_INTERVALS,
    adapter_factory: AdapterFactory | None = None,
    on_progress: ProgressHook | None = None,
    pull_id: str | None = None,
    rate_observer: RateObserver | None = None,
    session_rate: SessionRateController | None = None,
) -> PullCandlesReport:
    """Ingest *instrument*'s spot candles over a date range (Groww model).

    Plans + drives one
    :func:`~brutex.adapters.groww.run_ingest.run_groww_spot_ingest` pass over
    the single *instrument* and the Groww **fetched** base intervals
    (the injected *intervals*, defaulting to :data:`GROWW_FETCH_INTERVALS` —
    ``1day`` only during the verify-first phase, DEC-PULL-1DAY-FIRST-001;
    ``1minute`` + the 19 resampled intraday timeframes return once 1-minute is
    re-enabled, per DEC-DHAN-1M-ONLY-001). The window is resolved by
    :func:`resolve_candle_range`: an explicit *from_date* / *to_date* range
    (day-inclusive, clamped to ``[2020-01-01, today]``) when either is given,
    else the full calendar *year*, else (no dates, no year) the full Track-1
    history ``[2020-01-01, previous day]``. The default end is the previous
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
        (default) pulls the full Track-1 history (2020-01-01 → previous
        day), NOT just the current year. Ignored when *from_date* or
        *to_date* is supplied.
    from_date, to_date
        Inclusive ISO ``YYYY-MM-DD`` bounds for an explicit range. Either
        may be ``None`` (*from_date* defaults to the Groww history floor;
        *to_date* defaults to the previous day — an explicit *to_date* may
        reach today). When both are ``None`` the *year* / full-history mode
        applies.
    dry_run
        When ``True``, use the offline synthetic adapter (no creds /
        network). When ``False``, build the live Groww adapter via
        *adapter_factory*.
    adapter_factory
        Override the ``(adapter, sink)`` builder. Defaults to the offline
        synthetic builder when *dry_run* is ``True`` and to the live Groww
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
    # transport (``groww.call``) + rate (``rate.*``) events all share one
    # ``pull_id``. Always unbinds on exit (no leak).
    with pull_id_scope(pull_id) as pid:
        # The coverage reconcile band-aids are RETIRED (DEC-WINDOW-LEDGER-001,
        # Stage d) — the ledger's per-window record + the cold-start
        # `reconcile_ledger_with_lake` below are the sole skip source (the
        # FetchCoverage set was deleted in Stage e). The legacy window_status.json
        # sidecar is WRITE-RETIRED (DEC-STATUS-SIDECAR-WRITE-RETIRE-001) — kept on
        # disk read-only for the db_view fallback + one-shot ledger migration.
        # Unified per-window ledger (DEC-WINDOW-LEDGER-001): load/migrate + PLAN
        # this symbol's windows, then record each outcome into it.
        ledger = begin_pull_ledger(lake_root, dry_run=dry_run)
        if ledger is not None:
            plan_windows(
                ledger,
                instruments=[instrument],
                intervals=intervals,
                start=start.date(),
                end=end.date(),
                market=GROWW_COVERAGE_MARKET,
                window_days_for=groww_window_days_for,
            )
            # Cold-start seed (DEC-WINDOW-LEDGER-001, Stage b1): seed SUCCESS for
            # this symbol's lake-enclosed ABSENT windows so the upcoming
            # is_skippable flip won't re-pull bars the lake already holds. A
            # non-None ledger already implies a live pull (begin_pull_ledger
            # returns None for dry_run), so no extra dry-run guard is needed.
            # ``market=GROWW_COVERAGE_MARKET`` matches the Groww spine namespace.
            reconcile_ledger_with_lake(
                ledger,
                lake_root,
                instruments=[instrument],
                intervals=intervals,
                start=start.date(),
                end=end.date(),
                market=GROWW_COVERAGE_MARKET,
                window_days_for=groww_window_days_for,
            )

        _LOG.info(
            "universe.pull.start",
            pull_id=pid,
            symbol=symbol,
            year=resolved_year,
            start_date=start.date().isoformat(),
            end_date=end_label,
            dry_run=dry_run,
            intervals=len(intervals),
        )

        (pace, on_rate_limit, on_timeout, on_success), run_parallel, _rate_controller = (
            _live_rate_runtime(use_live_rate, session_rate)
        )
        # NO per-pull RenewToken here (DEC-DHAN-RENEW-SINGLE-OWNER-001) — RenewToken
        # ROTATES + invalidates the presented token, so an ungated per-pull renew
        # killed the live token and the pull ran on the corpse (DH-906). The 12h
        # TokenKeepalive is the SINGLE renewer (renews only <12h, into the holder
        # the client reads). See the bulk-pull path for the full rationale.
        # Live ledger flush (DEC-WINDOW-STATUS-LIVE-001 semantics on the LEDGER):
        # flush mid-pull (adaptive throttle) per completed interval so
        # /coverage/windows ticks up live, not only at end. The legacy
        # status-sidecar flush was write-retired
        # (DEC-STATUS-SIDECAR-WRITE-RETIRE-001).
        ledger_flush = make_live_ledger_flush(ledger, lake_root)

        def _progress_then_flush(label: str, bars: int, failed: bool) -> None:
            if on_progress is not None:
                on_progress(label, bars, failed)
            ledger_flush()

        report = run_groww_spot_ingest(
            adapter,
            sink,
            instruments=[instrument],
            intervals=intervals,
            start=start.date(),
            end=end.date(),
            pace=pace,
            on_rate_limit=on_rate_limit,
            on_timeout=on_timeout,
            on_success=on_success,
            on_progress=_progress_then_flush,
            ledger=ledger,
            run_parallel=run_parallel,
        )
        end_pull_ledger(ledger, lake_root, dry_run=dry_run)
        _observe_rate(rate_observer, _rate_controller, adapter)

        failure_keys = {(f.symbol, f.interval) for f in report.failures}
        # Build one result per ACTUALLY-fetched interval (the injected *intervals*,
        # 1day-only during the verify-first phase, DEC-PULL-1DAY-FIRST-001) — NOT a
        # hardcoded 1m+1d pair, which KeyError'd on report.pairs when 1-minute was
        # gated off. The label map gives the dashboard's ``1m``/``1d`` display label.
        # Explicit ``dict[str, str]`` so the keys are plain ``str`` (not the literal
        # union pyright would infer from the tuple), matching the ``str`` *intervals*.
        label_for: dict[str, str] = dict(_FETCH_INTERVAL_LABELS)
        timeframes: list[TimeframeResult] = []
        for fetch_interval in intervals:
            label = label_for.get(fetch_interval, fetch_interval)
            key = (symbol, fetch_interval)
            if key in failure_keys:
                # Terminal Groww error — quarantined, never in report.pairs.
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
    "GROWW_FETCH_INTERVALS",
    "GROWW_HISTORY_FLOOR",
    "SPOT_INTERVAL_BOTH",
    "SPOT_INTERVAL_CHOICES",
    "SPOT_INTERVAL_DAY",
    "SPOT_INTERVAL_MINUTE",
    "AdapterFactory",
    "BulkAdapterFactory",
    "BulkSpotReport",
    "PullCandlesReport",
    "SessionRateController",
    "SymbolPullResult",
    "TimeframeResult",
    "parse_symbol_list",
    "pull_all_spot",
    "pull_candles",
    "resolve_candle_range",
    "resolve_fetch_intervals",
    "resolve_pull_symbols",
]
