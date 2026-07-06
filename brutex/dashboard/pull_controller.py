"""Click-to-pull controller — ingest ONE instrument's spot candle data (Groww).

Powers the F&O Universe Explorer's "⤓ Pull" button
(DEC-UNIVERSE-DASHBOARD-001). The controller is **instrument-agnostic**: it
pulls spot OHLCV for any resolved :class:`~brutex.models.Instrument` — a spot
index/stock or (lake-side) a contract — keyed by ``symbol_name`` (the engine
symbol; ADR-0003). When the operator clicks a universe row and hits pull,
:func:`pull_candles` ingests that instrument's candles across the
**fetched** base timeframes
(:data:`GROWW_FETCH_INTERVALS` — **``1minute`` ONLY**,
DEC-1MINUTE-ONLY-DERIVE-1DAY-001; 1-minute is the single granularity fetched and
``1day`` + the 19 intraday timeframes are DERIVED from it, never vendor-fetched),
reusing the proven
:func:`~brutex.adapters.groww.run_ingest.run_groww_spot_ingest` orchestration
core (CLAUDE.md §11 COMMON — no copy-paste of the fan-out/quarantine spine). The
``1day`` partition is filled by resampling the just-pulled 1-minute to
midnight-IST daily bars (:mod:`brutex.data.spot_daily_resample`), so the engine's
daily anchor is an openly-derived bar, not a separate vendor pull.

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
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import structlog

from brutex.adapters.empty_circuit_breaker import MassEmptySettleHalt
from brutex.adapters.error_storm_breaker import resolve_error_storm_breaker
from brutex.adapters.groww.chunks import window_days_for_interval as groww_window_days_for
from brutex.adapters.groww.constants import TRACK1_DAILY_INTERVAL
from brutex.adapters.groww.errors import GrowwAuthError, is_terminal_auth_failure
from brutex.adapters.groww.ingest import IngestReport
from brutex.adapters.groww.pull_pipeline import (
    build_fetch_pipeline as _build_fetch_pipeline,
)
from brutex.adapters.groww.pull_pipeline import (
    build_write_consumer as _build_write_consumer,
)
from brutex.adapters.groww.pull_pipeline import (
    drain_fetch_pipeline as _drain_fetch_pipeline,
)
from brutex.adapters.groww.pull_pipeline import (
    drain_write_consumer as _drain_write_consumer,
)
from brutex.adapters.groww.pull_pipeline import (
    reconcile_pairs_with_pipeline as _reconcile_pairs_with_pipeline,
)
from brutex.adapters.groww.run_ingest import (
    GrowwIngestReport,
    PairFailure,
    ProgressHook,
    run_groww_spot_ingest,
    run_groww_spot_ingest_with_retry,
    with_paced_client,
)
from brutex.adapters.groww.spot_bounds import SpotWindows
from brutex.adapters.groww.vendor_floor import (
    VendorFloorLearner,
    VendorFloorMetaStore,
    floor_symbol_windows,
    load_vendor_floors,
    make_vendor_floor_learner,
)
from brutex.adapters.ingest_common import (
    AUTO_RESUME_MAX_ROUNDS,
    GROWW_COVERAGE_MARKET,
    PullVerdict,
    SyntheticSpotAdapter,
    UntilCleanRound,
    classify_pull,
    mark_synthetic,
    run_until_clean,
)
from brutex.adapters.protocol import MarketAdapter
from brutex.calendars.observed_sessions import load_observed_holidays
from brutex.dashboard.coverage_reconcile import load_lake_spans
from brutex.dashboard.pull_census import emit_written_settled_census
from brutex.data.parquet_writer import fold_max_close_delta
from brutex.data.sink import BarSink, NullBarSink
from brutex.data.window_ledger import WindowLedger
from brutex.data.window_ledger_store import (
    begin_pull_ledger,
    end_pull_ledger,
    make_live_ledger_flush,
    plan_windows,
    reconcile_ledger_windows,
    reconcile_ledger_with_lake,
    run_skipped_audit_guarded,
)
from brutex.models import Instrument
from brutex.notifications.sweep_alerts import emit_pull_halt_alert
from brutex.observability.correlation import pull_id_scope
from brutex.observability.pull_journal import record_pull_finish, record_pull_start
from brutex.orchestrator.audit import open_ingest_audit
from brutex.orchestrator.parallel import PoolRunParallel
from brutex.orchestrator.quota_tracker import DailyQuotaExceeded
from brutex.orchestrator.universe_phases import RunParallel

if TYPE_CHECKING:
    from brutex.adapters.call_meter import CallMeter
    from brutex.adapters.groww.run_ingest import SpotIngestWithRetry
    from brutex.adapters.rate_common import RateController
    from brutex.observability.pull_health import PullHealthMonitor

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

#: Legacy spot-interval choice tokens (DEC-1MINUTE-ONLY-DERIVE-1DAY-001 retired the
#: ``/universe`` granularity dropdown). 1-minute is now the SOLE fetched granularity
#: for spot — ``1day`` + the 19 intraday TFs are DERIVED from it, never vendor-fetched
#: (the spot twin of DEC-DHAN-1M-ONLY-001). These tokens remain importable for the
#: vestigial CLI ``--interval`` alias map (:func:`brutex.cli._resolve_fetch_intervals`),
#: but the dashboard pull always fetches 1-minute only — :func:`resolve_fetch_intervals`
#: ignores any choice and returns ``("1minute",)``.
SPOT_INTERVAL_DAY: Final[str] = "1day"
SPOT_INTERVAL_MINUTE: Final[str] = "1minute"
SPOT_INTERVAL_BOTH: Final[str] = "both"

#: Legacy dropdown choice → fetched-interval tuple. RETAINED only for the CLI alias
#: map; the dashboard no longer reads it (:func:`resolve_fetch_intervals` is now a
#: constant ``("1minute",)``). ``both`` keeps the canonical display order.
SPOT_INTERVAL_CHOICES: Final[dict[str, tuple[str, ...]]] = {
    SPOT_INTERVAL_DAY: ("1day",),
    SPOT_INTERVAL_MINUTE: ("1minute",),
    SPOT_INTERVAL_BOTH: ("1minute", "1day"),
}

#: The ONLY fetched spot granularity (DEC-1MINUTE-ONLY-DERIVE-1DAY-001): **1-minute**.
#: ``1day`` is resampled from it after the pull (:mod:`brutex.data.spot_daily_resample`),
#: never fetched. One source of truth shared by callers + tests.
GROWW_FETCH_INTERVALS: Final[tuple[str, ...]] = (SPOT_INTERVAL_MINUTE,)


def resolve_fetch_intervals(choice: str | None) -> tuple[str, ...]:
    """Resolve the spot fetch-interval tuple — ALWAYS ``("1minute",)``.

    DEC-1MINUTE-ONLY-DERIVE-1DAY-001 makes **1-minute the single granularity fetched
    from Groww for spot**; ``1day`` and the 19 intraday timeframes are DERIVED from
    the stored 1-minute, never vendor-fetched. The legacy ``/universe`` granularity
    dropdown is retired, so this function ignores *choice* entirely and always returns
    :data:`GROWW_FETCH_INTERVALS` (``("1minute",)``). The *choice* parameter is kept so
    the route signatures + the harmless ``spot_interval`` query param still type-check.

    Parameters
    ----------
    choice
        The (now-ignored) legacy dropdown / query-param value, or ``None``.

    Returns
    -------
    tuple[str, ...]
        Always ``("1minute",)``.
    """
    return GROWW_FETCH_INTERVALS


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


def resolve_pull_targets(
    *,
    symbols: str | None,
    symbol: str | None,
    lookup: Callable[[str], Instrument | None],
    whole_universe: Callable[[], list[Instrument]],
) -> tuple[list[Instrument], str]:
    """Resolve a pull's target instruments from an OPTIONAL selection (DEC-PULL-SELECTED-PIPELINE-001).

    The single COMMON resolver behind every "selection OR whole-universe" pull
    surface — the one-click pipeline (``/pull-pipeline``) and the failed-window
    retry (``/retry-failed``) — replacing the byte-identical ``if symbols … elif
    symbol … else`` block both used to inline (CLAUDE.md §11 COMMON, no copy-paste).

    Precedence (most-specific wins): ``symbols`` (the comma list of ticked ids) >
    ``symbol`` (one highlighted id) > ``whole_universe()`` (NO selection → the full
    admitted universe — the byte-for-byte prior default, so an un-parametrised call
    is unchanged). Resolution is O(selection) with O(1) ``lookup`` per id (the store
    ``get`` is a dict hit); the ``symbols`` branch de-duplicates + preserves input
    order via :func:`resolve_pull_symbols`.

    Fails CLOSED (Golden Rule #1 — no silent scope drift): an EMPTY or ANY-unknown
    ``symbols`` selection, or an unknown single ``symbol``, raises
    :class:`ValueError` naming the offender(s); the route maps it to a 400 so a typo
    never silently pulls a partial set. The ``whole_universe`` fallback is only
    reached when BOTH ``symbols`` and ``symbol`` are ``None``.

    Parameters
    ----------
    symbols
        The raw comma-joined ``?symbols=`` value (the ticked many), or ``None``.
    symbol
        A single ``?symbol=`` security id (one highlight), or ``None``.
    lookup
        ``store.get``-style resolver: an id → :class:`Instrument` or ``None``.
    whole_universe
        Zero-arg factory for the full admitted universe (every index + stock
        underlying), called only when no selection is given.

    Returns
    -------
    tuple[list[Instrument], str]
        ``(instruments, source)`` where *source* is ``"selection"`` (``symbols``),
        ``"single"`` (``symbol``), or ``"universe"`` (the fallback) — for the
        route's log + progress detail.

    Raises
    ------
    ValueError
        On an empty/unknown ``symbols`` list, or an unknown single ``symbol``.
    """
    if symbols is not None:
        return resolve_pull_symbols(symbols, lookup), "selection"
    if symbol is not None:
        instrument = lookup(symbol)
        if instrument is None:
            raise ValueError(f"unknown security_id {symbol!r} — not in the instrument store")
        return [instrument], "single"
    return whole_universe(), "universe"


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
        :data:`GROWW_FETCH_INTERVALS` order (``1m`` only —
        DEC-1MINUTE-ONLY-DERIVE-1DAY-001). The 19 derived intraday timeframes
        AND ``1d`` are derived from the 1-minute base downstream, not fetched
        here (DEC-DHAN-1M-ONLY-001).
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
    resolve the Groww TOTP credentials from AWS Parameter Store (the SOLE source,
    DEC-AWS-ONLY-CREDS-001) via
    :func:`~brutex.config.credentials.resolve_groww_aws_credentials`, build a
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

    Rate-controller hooks (DEC-SUSPICION-SWEEP-FIXES-001 D1): this builder wires
    NO ``pace`` / ``on_rate_limit`` / ``on_success`` — the session controller does
    not exist here yet. The FULL hook triple is bound downstream at the ONE COMMON
    spine seam (``run_ingest.with_paced_client``, called by
    ``run_groww_spot_ingest`` with the ``_live_rate_runtime`` quad), so every 429
    the client's bounded fast-retry absorbs still decrements the shared AIMD
    ladder — pinned by the production-wiring test in
    ``tests/unit/test_groww_spot_rate_feedback.py`` (which monkeypatches NO
    builders).
    """
    from brutex.adapters.groww.adapter import GrowwMarketAdapter
    from brutex.adapters.groww.auth import GrowwCredentials
    from brutex.adapters.groww.constants import resolve_pool_size
    from brutex.adapters.groww.instrument_loader import load_index, urllib_http_get
    from brutex.adapters.groww.live import build_live_pool
    from brutex.config.credentials import resolve_groww_aws_credentials
    from brutex.data.parquet_writer import ParquetWriter
    from brutex.data.raw_integrity_store import make_capture_hook_factory
    from brutex.data.sink import ParquetBarSink

    del instruments  # the Groww adapter carries the whole master index (O(1) resolve)
    aws_creds = resolve_groww_aws_credentials()
    creds = GrowwCredentials(api_key=aws_creds.api_key, totp_secret=aws_creds.totp_secret)
    # Multi-connection pool (DEC-GROWW-CLIENT-POOL-001): K independent SDK connections so the
    # fan-out worker threads stop serialising on ONE socket (the measured avg-0.49-in-flight
    # → ~3.4 r/s starve). The SHARED session RateController still caps the AGGREGATE rate; the
    # shared CallMeter proves the real achieved number on the dashboard rate panel. Pool size
    # is env-tunable (BRUTEX_GROWW_POOL_SIZE) with a safe default + hard clamp.
    pool_size = resolve_pool_size()
    client = build_live_pool(creds, size=pool_size)
    _LOG.info("groww.spot.client_pool", connections=pool_size)
    index = load_index(urllib_http_get)
    # Raw-data integrity capture (DEC-RAW-DATA-INTEGRITY-001): bind the per-symbol
    # capture-hook factory to the lake root so EVERY live spot window records a hash
    # of EXACTLY what Groww sent. Best-effort inside the hook — it can never halt a
    # pull (a malformed payload / sidecar write failure is logged + swallowed).
    adapter = GrowwMarketAdapter(
        client=client, index=index, capture_hook_factory=make_capture_hook_factory(lake_root)
    )
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
    windows_skipped: int = 0
    """Settled windows skipped WITHOUT a vendor hit (DEC-WINDOW-LEDGER-RESCUE-001) —
    each phase's FIRST pass's ledger skips summed (later until-clean rounds re-skip
    the same settled windows by design, so summing passes would double-count). The
    /universe surface renders it as "skipped N already-pulled windows (no vendor
    hit)" so the operator SEES the skip engaging."""
    ledger_load_failed: bool = False
    """True when the persisted window ledger EXISTED but failed to reload at pull
    start (DEC-WINDOW-LEDGER-RESCUE-001) — the pull ran a full re-verify (no skip),
    announced loudly in the log + the pipeline note, never silent."""
    mass_empty_cause: str | None = None
    """The mass-empty circuit-breaker halt message (DEC-EMPTY-WINDOW-CIRCUIT-
    BREAKER-001) when the pull HALTED on a suspected soft-throttle [R2] /
    mis-addressing [R3] clean-empty burst — drives the loud, needs-operator
    ``mass_empty_halt`` verdict naming the counts + candidate causes. ``None``
    when the breaker never tripped."""
    settled_skipped: int = 0
    """Windows THIS pull newly settled as terminal ``SKIPPED`` (the per-pull
    ledger tally) — the honest-verdict count every verdict kind must surface
    (DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001; never "No action needed" over a
    settling pull)."""
    conflicts: int = 0
    """Keep-first vendor re-statements recorded across the whole spot pull
    (DEC-VENDOR-CONFLICT-SURFACE-001 — previously visible ONLY in the
    ``ingest.conflict.summary`` log lines). The stored bar always won."""
    vendor_dup_rows_dropped: int = 0
    """WITHIN-BATCH duplicate vendor rows the writer collapsed across the pull
    (DEC-VENDOR-CONFLICT-SURFACE-001 — the sum the ``lake.vendor_duplicate_rows``
    WARNINGs previously swallowed)."""
    max_close_delta: Decimal | None = None
    """MAX absolute keep-first close delta across the pull's conflicts — exact
    Decimal rupees (never float, CLAUDE.md §6 price-unit row); ``None`` when no
    conflict was recorded."""

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

        An EXPLICIT synthetic dry-run's verdict is re-labeled through
        :func:`~brutex.adapters.ingest_common.mark_synthetic`
        (DEC-PULL-LOUD-CREDENTIAL-REFUSAL-001): the headline/action SAY
        "SYNTHETIC DRY-RUN — no real data" loudly, so a dry-run can never read
        as a green real pull (the classification ``kind`` itself is preserved).
        """
        verdict = classify_pull(
            auth_aborted=self.auth_aborted,
            still_failed_windows=self.still_failed_windows,
            failed_pairs=self.total_failed,
            no_data=self.total_no_data,
            completed=self.total_completed,
            auth_cause=self.auth_cause,
            quota_exhausted=self.quota_exhausted,
            mass_empty_cause=self.mass_empty_cause,
            settled_skipped=self.settled_skipped,
        )
        if self.dry_run:
            return mark_synthetic(verdict)
        return verdict

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
            "windows_skipped": self.windows_skipped,
            "ledger_load_failed": self.ledger_load_failed,
            "conflicts": self.conflicts,
            "vendor_dup_rows_dropped": self.vendor_dup_rows_dropped,
            # JSON-safe: the exact Decimal renders as its string (never a float
            # round-trip — CLAUDE.md §4 money rule); None stays null.
            "max_close_delta": None if self.max_close_delta is None else str(self.max_close_delta),
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
    """Give the spot pull the IDENTICAL default pool the expiry/contract pulls use.

    DEC-DHAN-PARALLEL-INGEST-001 + DEC-SPOT-POOL-UNIFORM-001. There is NO spot-special
    sizing: a live spot pull gets the same bounded
    :class:`~brutex.orchestrator.parallel.PoolRunParallel` (default
    :data:`~brutex.orchestrator.parallel.DEFAULT_MAX_WORKERS` = 8) that
    ``get_expiries`` / ``get_contracts`` have always used against the SAME shared
    ``GrowwAPI`` SDK — the modest concurrency that SDK reliably tolerates. The
    spot-only 96-wide pool (DEC-SPOT-POOL-WIDTH-15S-001, reverted) HUNG it; the
    operator's cross-check is decisive — expiries/contracts run fine on 8 workers
    through that same SDK, so spot uses 8 too (not a special number). ``None``
    controller (dry-run / injected-factory pull) → ``None`` here, so
    ``run_groww_spot_ingest`` uses its own default pool. The per-second issue rate is
    the shared token bucket's job (Rule #6); the REAL per-call latency is now logged
    (``groww.call``, DEC-GROWW-CALL-LATENCY-LOG-001) so throughput is measured, not
    assumed. This is the pure, covered sizing seam; the live controller construction
    is the ``# pragma: no cover`` part.
    """
    if controller is None:
        return None
    return PoolRunParallel()


def _build_live_controller() -> RateController:
    """Construct the shared adaptive [3..8] r/s controller (the session factory).

    The controller starts at the operator-DIRECTED 8 r/s ceiling
    (:data:`~brutex.adapters.groww.constants.GROWW_RATE_LIMIT_RPS_CEILING`,
    operator-approved 2026-06-24, RAISED 5 → 8, DEC-GROWW-RATE-CEILING-8-002 — see
    CLAUDE.md §6) — the per-second backtesting cap stays officially UNVERIFIED; 8×60 =
    480/min stays under the 500/min governor so the HARD per-minute cap can never be
    overshot, and the ceiling was raised back to 8 once DEC-FETCH-WRITE-OVERLAP-001
    Lever 2 (pool 2→4) gave the pull enough connections to GENERATE demand toward it. A
    CEILING raise only ALLOWS a higher rate — the adaptive AIMD ladder still steps DOWN on
    a 429 burst (soft floor 3) and recovers UP only as far as the vendor tolerates, so the
    worst case at 8 is identical to 5; SUSTAINED 8 is UNVERIFIED (the log shows the vendor
    429ing at 5).

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
        resolve_smooth_pacing_mode,
    )
    from brutex.adapters.rate_common import RateController

    # Operator-confirmed Groww backtesting limits (DEC-GROWW-NO-DAILY-QUOTA-001):
    # 500 req/min (the HARD per-minute governor, DEC-GROWW-RATE-PERMIN-001) AND NO
    # daily quota — so NO DailyQuotaTracker is wired here, matching the CLI Groww
    # spot path (cli.py also builds the controller with quota_tracker=None). The
    # phantom 100,000/day cap would have wrongly HALTED a large multi-day backfill,
    # since Groww backtesting has no daily quota (operator-confirmed 2026-06-13).
    # The per-second start AND upshift cap is the operator-DIRECTED 8 r/s ceiling
    # (GROWW_RATE_LIMIT_RPS_CEILING, RAISED 5 → 8 on 2026-06-24, DEC-GROWW-RATE-CEILING-8-002,
    # CLAUDE.md §6) — 8×60 = 480/min stays under the 500/min governor, so the HARD per-minute
    # cap can never be overshot; the AIMD ladder still steps DOWN on 429s (soft floor 3) and
    # recovers UP only as far as the vendor tolerates. Raised back to 8 once Lever 2 (pool
    # 2→4, DEC-FETCH-WRITE-OVERLAP-001) gave the pull enough connections to generate demand
    # toward the ceiling; SUSTAINED 8 is UNVERIFIED (the vendor 429s as low as 3–5 r/s).
    # Anti-burst smooth pacing (DEC-GROWW-SMOOTH-PACING-001 +
    # DEC-SMOOTH-PACING-AUTOLATCH-001): the tri-state mode is threaded — a truthy
    # BRUTEX_GROWW_SMOOTH_PACING forces the gate ON from construction, an explicit
    # falsy value disables gate AND latch, and the unset DEFAULT is ADAPTIVE:
    # pacing starts byte-identical and the controller one-way latches smoothing ON
    # on the first 429 that carries measured per-1s burst evidence above the
    # then-current allowed rate. Whatever the mode, smoothing only ADDS spacing —
    # the 8 r/s ceiling, the 500/min governor, and the AIMD ladder are UNCHANGED.
    return RateController(
        ceiling_rps=GROWW_RATE_LIMIT_RPS_CEILING,
        ceiling_per_min=GROWW_RATE_LIMIT_PER_MIN,
        smooth_pacing_mode=resolve_smooth_pacing_mode(),
    )


class SessionRateController:
    """Persist ONE :class:`RateController` across pulls in a dashboard session.

    DEC-DHAN-PERSIST-RATE-001. Each ``POST /universe/pull`` used to build a FRESH
    controller at the 8 r/s ceiling (:func:`_build_live_controller`, the
    operator-DIRECTED ceiling, RAISED 5 → 8 on 2026-06-24, DEC-GROWW-RATE-CEILING-8-002),
    so every symbol-pull re-opened at 8 r/s and re-hit Groww's wall — the 429 storm the
    operator saw at the start of *every* pull, re-learnt per symbol. This holder builds the
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
        #: The live pull's shared transport :class:`~brutex.adapters.call_meter.CallMeter`
        #: — the MEASURED completed-rate source (``effective_rps``) — REGISTERED by the pull
        #: build sites once they build the client pool (DEC-RATE-WIDGET-FNO-MEASURED-001).
        #: ``None`` until a real live pull registers one; a dry-run / metadata-before-client
        #: tick reads ``None`` and the sampler falls back to its honest ``meter_present=False``.
        self._meter: CallMeter | None = None
        self._lock = threading.Lock()

    def get(self) -> RateController:
        """Return the session's controller, building it ONCE on first use (thread-safe)."""
        with self._lock:
            if self._controller is None:
                self._controller = self._factory()
            return self._controller

    def peek(self) -> RateController | None:
        """Return the session's controller IFF already built, else ``None`` — NEVER builds.

        The non-building twin of :meth:`get`, for OBSERVE-ONLY readers (the live-rate
        panel sampler, :class:`~brutex.dashboard.rate_sampler.RatePanelSampler`) that
        must never FORCE a live controller into existence. :meth:`get` lazily builds a
        LIVE :class:`RateController` (which on the production factory needs creds) on
        first call; a sampler that called it would conjure a live controller during a
        dry-run / a phase that has not yet paced anything. :meth:`peek` only ever
        observes a controller a REAL pull already built, so an idle / dry-run sampler
        reads ``None`` and pushes nothing (the panel stays honestly idle). Thread-safe +
        O(1) (one lock, one read).
        """
        with self._lock:
            return self._controller

    def register_meter(self, meter: CallMeter | None) -> None:
        """Register the live pull's shared transport :class:`CallMeter` (thread-safe, O(1)).

        DEC-RATE-WIDGET-FNO-MEASURED-001. The MEASURED completed-rate (``effective_rps``)
        lives in the transport :class:`~brutex.adapters.call_meter.CallMeter` built fresh
        inside the F&O / spot client pool (:func:`~brutex.adapters.groww.live.build_live_pool`),
        deep in the fetch spine — unreachable from the observe-only
        :class:`~brutex.dashboard.rate_sampler.RatePanelSampler` seam, which holds only THIS
        controller. The pull build sites (which build the pool AND receive this holder)
        REGISTER the pool's shared meter here so the sampler can :meth:`peek_meter` it and
        push the REAL measured rate during the metadata + F&O phases (not the admission
        average). A ``None`` *meter* (a client with no meter attribute — a defensive
        best-effort guard) is silently ignored, leaving any prior registration intact so a
        transient factory oddity never blanks a live meter. Idempotent — re-registering the
        SAME session's pool meter is a harmless overwrite (the pool holds ONE aggregate
        meter for the whole pull). Observe-only: this stores a READ handle; it changes NO
        pacing decision.
        """
        if meter is None:
            return
        with self._lock:
            self._meter = meter

    def peek_meter(self) -> CallMeter | None:
        """Return the registered live :class:`CallMeter`, else ``None`` — NEVER builds one.

        The meter twin of :meth:`peek`, for the observe-only
        :class:`~brutex.dashboard.rate_sampler.RatePanelSampler`: when a real live pull has
        :meth:`register_meter`\\ ed the pool's shared meter, the sampler reads its
        :meth:`~brutex.adapters.call_meter.CallMeter.snapshot` MEASURED ``effective_rps`` and
        pushes ``meter_present=True`` with the true completed-rate; when no meter has been
        registered (a dry-run / a phase before the client pool is built), it reads ``None``
        and the sampler falls back to the honest ``meter_present=False`` sentinel (no
        fabricated rate — DEC-RATE-WIDGET-FNO-HONEST-001). Thread-safe + O(1) (one lock,
        one read).
        """
        with self._lock:
            return self._meter


def register_pull_meter(session_rate: SessionRateController | None, source: object) -> None:
    """Register *source*'s transport :class:`CallMeter` into *session_rate* (best-effort).

    DEC-RATE-WIDGET-FNO-MEASURED-001. The ONE COMMON seam the live pull build sites call
    right after building their client pool (CLAUDE.md §11 COMMON — no copy of the
    meter-extraction logic). *source* is either a live
    :class:`~brutex.adapters.groww.pool.ClientPool` / :class:`~brutex.adapters.groww.client.\
GrowwClient` (its own ``.meter``) OR a :class:`~brutex.adapters.protocol.MarketAdapter`
    whose ``.client`` carries the ``.meter`` — so the shared aggregate meter reaches the
    observe-only :class:`~brutex.dashboard.rate_sampler.RatePanelSampler` via
    :meth:`SessionRateController.peek_meter` and the metadata + F&O phases surface the REAL
    measured completed-rate instead of the admission average
    (DEC-RATE-WIDGET-FNO-HONEST-001 follow-up).

    Mirrors :func:`_observe_rate`'s ``getattr`` probe: it looks for a ``meter`` directly on
    *source* (a client/pool) and, failing that, on ``source.client`` (an adapter). A
    ``None`` *session_rate* (a dry-run / an injected-factory pull with no live holder) OR a
    *source* carrying no meter is silently a NO-OP — the sampler then keeps its honest
    ``meter_present=False`` fallback (never a fabricated rate). Observe-only + O(1): it reads
    a handle and stores it; it changes NO pacing decision and can never block/crash the pull.
    """
    if session_rate is None:
        return
    meter = getattr(source, "meter", None)
    if meter is None:
        meter = getattr(getattr(source, "client", None), "meter", None)
    session_rate.register_meter(meter)


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
    monitor: PullHealthMonitor | None = None,
) -> None:
    """Push a live pull's adaptive rate + measured throughput to the dashboard panel.

    A no-op for a dry-run / injected-factory pull (no *controller*) or when neither an
    observer nor a *monitor* is wired (the default). On a live pull it builds the
    controller's ``snapshot()`` (current_rps + recent 429s) and the transport
    ``CallMeter``'s ``snapshot()`` (effective_rps + latency p95 + the instantaneous
    ``rps_1s`` / ``rps_1s_max`` gauge) and:

    * sends them to *rate_observer* (``make_rate_observer`` — DEC-DASHBOARD-RATE-PANEL-001
      + DEC-DASHBOARD-RPS1S-PANEL-001), so the operator SEES the live per-second issue
      rate + its peak on the dashboard, not just in ``/logs``;
    * feeds them to *monitor* (:class:`~brutex.observability.pull_health.PullHealthMonitor`
      — DEC-PULL-HEALTH-MONITOR-001), which WATCHES the same snapshots for a degraded /
      stalled pull and fires a best-effort Telegram alert on a breach edge.

    The monitor's :meth:`~brutex.observability.pull_health.PullHealthMonitor.observe` is
    itself best-effort (never raises), so this seam can never block or crash the pull.
    """
    if controller is None or (rate_observer is None and monitor is None):
        return
    meter = getattr(getattr(adapter, "client", None), "meter", None)
    controller_snap = controller.snapshot()
    meter_snap = meter.snapshot() if meter is not None else {}
    if rate_observer is not None:
        rate_observer(controller_snap, meter_snap)
    if monitor is not None:
        monitor.observe(controller_snap, meter_snap)


def make_window_observer(
    inner: Callable[[], None],
    rate_observer: RateObserver | None,
    controller: RateController | None,
    adapter: MarketAdapter,
    monitor: PullHealthMonitor | None,
) -> Callable[[], None]:
    """Wrap a per-window ``on_window`` callback to ALSO observe the live pull each window.

    DEC-PULL-STALL-DETECTION-001 (GAP 2). Before this, :class:`~brutex.observability.\
pull_health.PullHealthMonitor.observe` was fed ONLY at pull / instrument BOUNDARIES (the
    post-loop :func:`_observe_rate` call), so the dwell-gated ``pull.stalled`` rule — which
    needs several CONSECUTIVE observe samples to fire — could never accumulate, and a
    MID-pull stall was undetectable. The ingest spine already fires an ``on_window``
    :data:`~brutex.adapters.groww.ingest.WindowTick` once per PROCESSED window (~1 s cadence
    under load); this wraps it so each window ALSO pushes the live rate/throughput snapshot
    to the panel + the monitor via :func:`_observe_rate`. The dwell now accumulates on the
    pull's own activity cadence, so a sustained near-zero-throughput stall trips the
    CRITICAL ``pull.stalled`` alert.

    The wrapped *inner* (the dashboard live-sync, or a no-op for the CLI / dry-run) runs
    FIRST so the existing per-window progress + ledger flush is byte-unchanged; the observe
    follows. :func:`_observe_rate` is itself a no-op when neither a *rate_observer* nor a
    *monitor* is wired and is best-effort (the monitor never raises), so wrapping per window
    can never block or crash the pull. When there is nothing extra to observe (no observer
    AND no monitor), *inner* is returned unwrapped (zero overhead).
    """
    if rate_observer is None and monitor is None:
        return inner

    def _on_window() -> None:
        inner()
        _observe_rate(rate_observer, controller, adapter, monitor)

    return _on_window


def _build_pull_health_monitor(
    controller: RateController | None,
) -> PullHealthMonitor | None:
    """Build the live pull's :class:`PullHealthMonitor`, or ``None`` (best-effort).

    BINDS the dormant monitor at the dashboard live-pull build site
    (DEC-PULL-HEALTH-WIRING-001). PR #893 (DEC-PULL-HEALTH-MONITOR-001) shipped the
    monitor + the :func:`_observe_rate` ``monitor`` seam, but NO production build site
    constructed one — so on a real pull it watched nothing. This wires it to the live
    rails: the process-global :class:`~brutex.observability.metrics.MetricRegistry`
    (so ``/metrics`` carries the live pull-health gauges), the
    :meth:`~brutex.notifications.sweep_alerts.SweepAlerter.pull_degraded` Telegram
    alert callback (so a breach edge reaches the operator's phone — a silent no-op
    when Telegram is disabled, the default), and the resolved connection-pool width
    (:func:`~brutex.adapters.groww.constants.resolve_pool_size`) used by the
    ``pull.pool_idle_high`` rule.

    Returns ``None`` — disabling the monitor without affecting the pull — when there is
    no live *controller* (a dry-run / injected-factory pull never observes a rate), so
    :func:`_observe_rate`'s ``monitor is None`` arm is a no-op. The whole body is
    GUARDED: an import / construction error is swallowed + logged and yields ``None``,
    so binding the monitor can NEVER block, slow, or crash the pull (Golden Rule #10).

    Honest envelope: the monitor's thresholds are operator-tunable heuristics, NOT
    vendor-verified limits, and a breach is an OBSERVATION + alert, never an
    auto-remediation — the pull continues unchanged whether or not the monitor fires.
    """
    if controller is None:
        return None
    try:
        from brutex.adapters.groww.constants import resolve_pool_size
        from brutex.notifications.sweep_alerts import SweepAlerter
        from brutex.observability.metrics import get_registry
        from brutex.observability.pull_health import PullHealthMonitor

        alerter = SweepAlerter()

        def _on_degraded(rule: str, achieved: float, allowed: float, detail: str) -> None:
            """Route a monitor breach edge to the best-effort ``pull_degraded`` alert."""
            alerter.pull_degraded(rule=rule, achieved=achieved, allowed=allowed, detail=detail)

        return PullHealthMonitor(
            registry=get_registry(),
            on_degraded=_on_degraded,
            pool_size=resolve_pool_size(),
        )
    except Exception:  # pylint: disable=broad-exception-caught — best-effort, never crash the pull
        _LOG.exception("pull_health.monitor_build_failed")
        return None


# The async fetch/write-pipeline lifecycle helpers used below
# (``_build_fetch_pipeline`` / ``_drain_fetch_pipeline`` / ``_build_write_consumer`` /
# ``_drain_write_consumer`` / ``_reconcile_pairs_with_pipeline``) are the COMMON
# :mod:`brutex.adapters.groww.pull_pipeline` source of truth shared by BOTH this dashboard
# build site AND the CLI ``brutex fetch`` build site (DEC-CLI-FETCH-ISOLATION-001 — no
# copy-paste, CLAUDE.md §11 COMMON). They were originally defined HERE; the bodies moved to
# that module verbatim and are re-imported at the TOP of this module under the SAME private
# names so every internal call site is byte-unchanged.
def _emit_pull_pool_size(pull_id: str) -> None:
    """Emit the resolved client connection-pool width once at pull start.

    Logs one ``pull.pool_size`` structlog event carrying the resolved
    :func:`~brutex.adapters.groww.constants.resolve_pool_size` (DEC-LAKE-WRITE-INSTRUMENT-001).
    The 28-hour pull-log forensic could NOT see the pool width — it inferred it
    from ``in_flight`` peaks (which actually reached 7, refuting the prior
    "pool=2" assumption) because no ``pool_size`` was logged. Pairing the
    resolved pool width with the per-partition ``lake.partition.write`` latency
    lets the write/lake bottleneck be sized against the real connection budget.

    Best-effort: ``resolve_pool_size`` is a pure env-read + clamp (no network), but
    the whole call is GUARDED so even an unexpected error can never block, slow, or
    crash the pull — the emission is purely observational (Golden Rule #10).
    """
    try:
        from brutex.adapters.groww.constants import resolve_pool_size

        _LOG.info("pull.pool_size", pull_id=pull_id, pool_size=resolve_pool_size())
    except Exception:  # pylint: disable=broad-exception-caught — best-effort, never crash the pull
        _LOG.exception("pull.pool_size_emit_failed", pull_id=pull_id)


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


#: Push the bulk spot pull's live WINDOW-level progress to the dashboard bar:
#: ``(completed, total, bars, failed, current)`` — the settled / total / bars / failed
#: window counts + the most-recently-attempted ``"<symbol> <interval>"`` label
#: (DEC-SPOT-WINDOW-PROGRESS-001).
OnLedgerSync = Callable[[int, int, int, int, str], None]

#: Minimum seconds between WINDOW-level live-sync pushes (DEC-SPOT-WINDOW-PROGRESS-001).
#: ~1 s feels live to the operator yet is far coarser than the per-window fetch, so the
#: O(N) ledger fold + the (separately throttled) Parquet flush never run per window and
#: never dominate wall time. Mirrors the live-ledger-flush cadence floor.
LIVE_SYNC_INTERVAL_S: Final[float] = 1.0


def make_live_sync(
    ledger: WindowLedger | None,
    ledger_flush: Callable[[], None],
    on_ledger_sync: OnLedgerSync | None,
    *,
    min_interval_s: float = LIVE_SYNC_INTERVAL_S,
    clock: Callable[[], float] | None = None,
    markets: Collection[str] | None = None,
    intervals: Collection[str] | None = None,
) -> Callable[[], None]:
    """Build a THROTTLED, thread-safe per-window live-sync callback (DEC-SPOT-WINDOW-PROGRESS-001).

    The bulk spot pull advanced its dashboard bar + flushed /coverage ONLY when a whole
    ``(symbol, interval)`` pair finished its multi-year backfill (``on_progress`` fires once
    per pair). With a modest pool sharing one adaptively-stepped-down rate bucket, a wave of
    symbols takes minutes — so BOTH surfaces sat frozen for minutes mid-pull (the
    operator-observed "stuck at 24/779") even though windows kept landing. This returns the
    callback the ingest fires once per WINDOW (:data:`~brutex.adapters.groww.ingest.WindowTick`):
    at most once per *min_interval_s* it flushes the ledger (so /coverage/windows ticks live)
    and folds the ledger's O(N) :class:`~brutex.data.window_ledger.LedgerProgress`, pushing
    it to *on_ledger_sync* (so the units bar advances per settled WINDOW — the spot twin of
    the metadata pull's per-call tick).

    Driven by REAL fetch activity (not a timer), the bar moves whenever windows land and
    stops moving exactly when the pull stalls — an honest live signal. The fold is absolute,
    so a repeated fire never double-counts a window the retry rounds re-touch.

    Thread-safe (``on_window`` fires from the ingest worker threads): one lock guards the
    throttle claim + the (non-thread-safe) ``ledger_flush`` + the O(N) fold, so the flush is
    single-flight and the ledger Parquet is never written by two threads at once; the O(1)
    *on_ledger_sync* push runs lock-free. A ``None`` *ledger* / *on_ledger_sync* (dry-run /
    non-dashboard) yields a no-op (the CLI / synthetic paths pay nothing). *clock* /
    *min_interval_s* are injectable for deterministic tests.

    *markets* / *intervals* (DEC-PULL-PROGRESS-HONESTY-001): optional SCOPE filters
    forwarded verbatim to :meth:`~brutex.data.window_ledger.WindowLedger.progress_counts`
    — the F&O historical phase folds ONLY its own ``groww-option`` / ``groww-future`` ×
    ``1minute`` windows so the cross-run, cross-market ledger's spot rows never pollute
    its denominator. ``None`` (the default — the spot pull) is byte-identical to the
    unfiltered fold.
    """
    if ledger is None or on_ledger_sync is None:
        return lambda: None
    from time import monotonic

    tick = clock if clock is not None else monotonic
    lock = threading.Lock()
    last_sync: list[float | None] = [None]

    def _sync() -> None:
        now = tick()
        with lock:
            if last_sync[0] is not None and now - last_sync[0] < min_interval_s:
                return
            last_sync[0] = now
            ledger_flush()
            progress = ledger.progress_counts(markets=markets, intervals=intervals)
        # The bar numerator is RESOLVED (SUCCESS+SKIPPED+EMPTY), not just settled — a
        # genuine no-data window is done, not "pending" (DEC-EMPTY-RESOLVED-REPORTING-001),
        # so a complete historical pull reads ~100% instead of "stuck / re-pull".
        on_ledger_sync(
            progress.resolved, progress.total, progress.bars, progress.failed, progress.current
        )

    return _sync


#: The Phase-1 daily interval (``"1day"``). The SPOT pull runs the cheap ``1day``
#: phase and the heavy ``1minute`` phase over INDEPENDENT full ``[start, end]``
#: ranges (DEC-SPOT-INTERVAL-DECOUPLE-001, MIGRATION.md #851 — the minute phase no
#: longer reads 1-day coverage to bound itself). This constant just names the
#: interval that splits the two phases; it is one source of truth shared with
#: :func:`~brutex.adapters.groww.run_ingest._resolve_spot_window`.
_PHASE1_DAILY: Final[str] = TRACK1_DAILY_INTERVAL


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
    on_ledger_sync: OnLedgerSync | None = None,
    floor_store: VendorFloorMetaStore | None = None,
) -> BulkSpotReport:
    """Bulk-pull EVERY instrument's spot over the full ``[2020-01-01, previous day]`` range.

    The one-click "⤓ Pull ALL indices / stocks spot" button core: one
    :func:`~brutex.adapters.groww.run_ingest.run_groww_spot_ingest` pass over
    the whole *instruments* list and the fetched base intervals
    (the injected *intervals*, defaulting to :data:`GROWW_FETCH_INTERVALS` —
    ``1m`` only, DEC-1MINUTE-ONLY-DERIVE-1DAY-001; ``1d`` is DERIVED after the pull),
    so the live adapter is built ONCE with the full universe (O(1) index) and the
    fan-out quarantines a bad symbol without sinking the rest. An empty list is a
    clean no-op.

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
    on_ledger_sync
        Optional :data:`OnLedgerSync` (the dashboard wires it; CLI / tests pass ``None``).
        When set, the pull advances the dashboard bar + flushes /coverage per WINDOW via a
        throttled, thread-safe :func:`make_live_sync` callback fired once per fetched window
        (DEC-SPOT-WINDOW-PROGRESS-001), instead of only when a whole symbol's backfill
        finishes — so a long backfill never LOOKS frozen. When set, the live-sync owns the
        ledger flush (the per-pair flush is suppressed so there is one writer); a final
        unthrottled push lands the exact end state. ``None`` keeps the prior per-pair flush
        + no window sync (byte-unchanged for every non-dashboard caller).
    floor_store
        Optional :class:`~brutex.adapters.groww.vendor_floor.VendorFloorMetaStore`
        (DEC-VENDOR-FLOOR-LAZY-PROBE-001 — the dashboard routes pass their open
        :class:`~brutex.data.instrument_store.InstrumentStore`, whose generic
        ``schema_meta`` sidecar satisfies the Protocol structurally). Read at pull
        start for the per-symbol learned-floor PLANNING clamp
        (``max(GROWW_HISTORY_FLOOR, learned floor)`` via the existing
        ``symbol_windows`` seam) and written by the in-pull lazy depth learner on
        a clean boundary-empty probe. ``None`` (CLI / tests / dry-run) keeps the
        full-range planning byte-identical and learns un-persisted (this pull's
        mass-settle still applies; the CLI ``brutex fetch`` path stays unwired —
        an explicit follow-up, mirroring DEC-PULL-HEALTH-WIRING-001's envelope).

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
    # Register the pool's shared MEASURED-rate meter for the observe-only sampler
    # (DEC-RATE-WIDGET-FNO-MEASURED-001): the FULL-pipeline run wraps this spot phase in a
    # RatePanelSampler (which holds only the session controller); registering the meter lets
    # that sampler read the REAL completed-rate too. The spot per-window observe (which reads
    # ``adapter.client.meter`` directly) is UNCHANGED — this only widens the sampler's reach.
    # A dry-run / injected-factory pull (no live ``session_rate``) is a silent no-op.
    register_pull_meter(session_rate, adapter)
    # Open the pull's correlation scope: ``pid`` is bound into the structlog
    # contextvars for the whole pass, so every event — this start/done summary,
    # the ingest spine's ``ingest.*`` events, AND the already-lit transport
    # (``groww.call``) + rate (``rate.*``) events — shares one ``pull_id``
    # (DEC-DHAN-INGEST-LOGGING-001). Always unbinds on exit (no leak).
    # Golden Rule #8 hash-chained ingest audit (DEC-INGEST-AUDIT-WIRING-001): bind
    # the SAME COMMON trail the CLI ``brutex fetch`` pull writes —
    # ``<lake>/audit/spot-ingest.jsonl`` via the ONE ``open_ingest_audit`` factory
    # (resume = continue the existing chain; a dry-run gets the null context — no
    # real data, no audit). The spine appends RUN_START → per-pair → RUN_COMPLETE
    # on its controller drain thread, O(1) per event, never on a fetch worker.
    with (
        pull_id_scope(pull_id) as pid,
        open_ingest_audit(lake_root, enabled=not dry_run) as ingest_audit,
    ):
        # DURABLE pull journal (G5, DEC-DOCTOR-LIVE-PROOF-KIT-001): a start record
        # that survives a mid-pull process death, paired with the finish record at
        # the bottom — ``brutex doctor --live`` stage 12 reads a start-without-finish
        # as "the overnight pull died mid-run". Never raises (an unwritable journal
        # must never break a pull — the swallow lives inside the journal module).
        record_pull_start(lake_root, pull_id=pid, dry_run=dry_run)
        # The coverage reconcile band-aids (lake-reconcile / EMPTY-terminal /
        # FAILED-repull) are RETIRED (DEC-WINDOW-LEDGER-001, Stage d): the unified
        # ledger is the SOLE skip authority, so its own per-window record + the
        # cold-start `reconcile_ledger_with_lake` below replace every coarse
        # coverage-set patch (the FetchCoverage set was deleted in Stage e).
        # The legacy window_status.json sidecar is WRITE-RETIRED
        # (DEC-STATUS-SIDECAR-WRITE-RETIRE-001): the ledger below is the page's
        # primary source; the sidecar stays on disk read-only (db_view fallback
        # + the one-shot ledger migration) until the Stage-6 deletion.
        # 1-MINUTE-ONLY fetch (DEC-1MINUTE-ONLY-DERIVE-1DAY-001): the SPOT pull fetches
        # ONLY ``1minute`` from Groww — :func:`resolve_fetch_intervals` always resolves
        # to ``("1minute",)`` so ``daily_requested`` is False and only the 1-minute phase
        # runs. ``1day`` is DERIVED from the stored 1-minute by the pipeline's final
        # prebuild phase (DEC-PIPELINE-AUTOPREBUILD-001 — never mid-pull, never
        # vendor-fetched). The legacy ``1day``
        # phase block below is RETAINED but dormant for any defensive caller that still
        # injects ``intervals=("1day",)`` directly. The decoupled-phase plumbing
        # (DEC-SPOT-INTERVAL-DECOUPLE-001, MIGRATION.md #851) is unchanged; both phases
        # pull their OWN full ``[start, end]`` range over EVERY requested symbol with
        # ``symbol_windows=None``.
        daily_requested = _PHASE1_DAILY in intervals
        minute_intervals: tuple[str, ...] = tuple(i for i in intervals if i != _PHASE1_DAILY)
        minute_requested = bool(minute_intervals)
        # Unified per-window ledger (DEC-WINDOW-LEDGER-001): load (one-shot
        # migrate) the durable Parquet ledger ONCE for the whole pull, shared by
        # BOTH phases so the live progress (on_ledger_sync) spans them. Each phase
        # plans its full ``[start, end]`` tiling per symbol upfront (1-day and
        # 1-minute alike — DEC-SPOT-INTERVAL-DECOUPLE-001); the planned keys equal
        # the fetched keys (no bounding, no phantom rows). Persisted to
        # <lake>/window_ledger/ — the single source /coverage/windows surfaces.
        ledger = begin_pull_ledger(lake_root, dry_run=dry_run)
        # LOUD reload failure (DEC-WINDOW-LEDGER-RESCUE-001): the persisted ledger
        # existed but could not be read — the store already WARNed
        # (window_ledger.load_failed); surface it on the PULL report too so the
        # /universe response says "window ledger reload FAILED — running full
        # re-verify pull (no skip)". Never crashes the pull; never silent.
        ledger_reload_failed = ledger is not None and ledger.load_failed
        if ledger_reload_failed:
            _LOG.warning(
                "universe.pull_all.ledger_reload_failed",
                pull_id=pid,
                note="window ledger reload FAILED — running full re-verify pull (no skip)",
            )
        if ledger is not None and daily_requested:
            # Cold-start seed FIRST (DEC-WINDOW-LEDGER-001, Stage b1): on a fresh clone
            # the ledger is empty but the lake may already hold years of bars. Seed
            # SUCCESS for every window of this pull's tiling the lake already
            # encloses, so the is_skippable flip will NOT re-pull the entire history.
            # The seed is ABSENT-ONLY (``WindowLedger.seed_lake_success`` never
            # overwrites ANY existing row — a PLANNED one included), so it MUST run
            # BEFORE ``plan_windows`` mints the PLANNED rows: seeding after the plan
            # was a permanent no-op and a cold start (lost/corrupt ledger over a
            # fully-populated lake) re-fetched every window (forensic-audit #7). A
            # non-None ledger already implies a live (non-dry-run) pull —
            # ``begin_pull_ledger`` returns None for dry_run — so no extra guard.
            # ``market=GROWW_COVERAGE_MARKET`` so the seeded rows share the Groww
            # spine's namespace (PR-J4b).
            reconcile_ledger_with_lake(
                ledger,
                lake_root,
                instruments=instruments,
                intervals=(_PHASE1_DAILY,),
                start=start.date(),
                end=end.date(),
                market=GROWW_COVERAGE_MARKET,
                window_days_for=groww_window_days_for,
            )
            # PLAN the remaining (genuinely-absent) windows — ``WindowLedger.plan``
            # is itself absent-only, so a just-seeded SUCCESS row stays settled.
            plan_windows(
                ledger,
                instruments=instruments,
                intervals=(_PHASE1_DAILY,),
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
        # BIND the live pull-health monitor to the resolved controller
        # (DEC-PULL-HEALTH-WIRING-001) — its observe() is fed BOTH per-window (the
        # window_observer below — DEC-PULL-STALL-DETECTION-001 GAP 2, so the dwell-gated
        # pull.stalled rule actually accumulates) AND at the post-loop boundary; best-effort
        # (None for a dry-run, never crashes the pull).
        _pull_health_monitor = _build_pull_health_monitor(_rate_controller)
        # Coalescing batched write consumer (DEC-WRITE-BATCH-COALESCE-001): when
        # ``BRUTEX_WRITE_BATCH`` is opted in (DEFAULT OFF), route every sink.append through
        # a background writer that COALESCES pending same-series windows into ONE merge per
        # drain cycle, so the fetch lane stops idling on its own O(month-depth) write. It
        # changes ONLY how fetched data is WRITTEN — the RateController (per-second ceiling +
        # 500/min governor) is UNTOUCHED, so the issue rate still binds at the controller's
        # allowed cap (Golden Rule #6). ``None`` (the default / a dry-run) keeps the
        # synchronous write path byte-identical; drained in the ``finally`` before the
        # ledger is saved (so every settled window is durable).
        _write_consumer = _build_write_consumer(sink, dry_run=dry_run)
        # FULL async fetch/process separation (DEC-GROWW-FETCH-ASYNC-SEPARATION-001): ON BY
        # DEFAULT (operator-authorized 2026-06-28) — the fetch lane does ONLY fetch + enqueue
        # and a SEPARATE consumer pool does ALL internal work (filter / write / ledger.record),
        # so internal processing can never block or slow the fetch. The RateController
        # (8/500 + AIMD) stays entirely producer-side, UNTOUCHED (Golden Rule #6). ``None``
        # (the ``BRUTEX_GROWW_FETCH_PIPELINE=off`` kill-switch / a dry-run) keeps the
        # synchronous path byte-identical; drained in the ``finally`` before the ledger is
        # saved (so every queued window is processed + ledger-recorded first).
        _fetch_pipeline = _build_fetch_pipeline(dry_run=dry_run, lake_root=lake_root)
        # Vendor-down error-storm breaker + dead-credential StillStale escalation
        # (DEC-VENDOR-DOWN-BREAKER-001): ONE per-pull breaker shared by every phase /
        # pass / until-clean round (any success resets it, so a recovering vendor never
        # trips). Armed ONLY for a REAL live pull — a dry-run (synthetic adapter)
        # threads ``None`` so it can never observe synthetic outcomes (the same gating
        # as ``detect_completeness_gaps``).
        _storm_breaker = None if dry_run else resolve_error_storm_breaker()
        # Lazy vendor-depth-floor learner (DEC-VENDOR-FLOOR-LAZY-PROBE-001): ONE
        # per-pull instance over the PACED replica of THIS pull's adapter. The live
        # pool factory hands every leaf a no-op pace (`build_live_pool` without a
        # `pace`), and the session controller is bound only inside the spine
        # (`run_ingest.with_paced_client`) — so the learner MUST bind the SAME
        # `pace` here or its probe fetches would bypass the 8 r/s ceiling + the
        # 500/min governor (the review BLOCKER this closes; every probe fetch now
        # admits through `GrowwClient._call`'s per-attempt pace —
        # DEC-RATE-SINGLE-ADMISSION-001). Armed ONLY for a real pull (the
        # _storm_breaker gating pattern); ``today`` = the resolved pull end (the
        # bisect's upper bound never reaches past the envelope).
        _floor_learner: VendorFloorLearner | None = (
            None
            if dry_run
            else make_vendor_floor_learner(
                with_paced_client(adapter, pace),
                history_floor=GROWW_HISTORY_FLOOR,
                today=end.date(),
                store=floor_store,
            )
        )
        # Learned-floor PLANNING clamp (DEC-VENDOR-FLOOR-LAZY-PROBE-001): a symbol
        # with a persisted learned floor plans its 1-minute windows from
        # ``max(GROWW_HISTORY_FLOOR, learned floor)`` via the EXISTING per-symbol
        # ``symbol_windows`` seam (plan_windows / reconcile / _resolve_spot_window
        # — planned keys stay == fetched keys). A ``None`` floor_store / no learned
        # floors resolves to ``None`` — full-range planning, byte-identical.
        _floor_windows = floor_symbol_windows(
            (i.symbol_name for i in instruments),
            start=start.date(),
            end=end.date(),
            floors=load_vendor_floors(floor_store, [i.symbol_name for i in instruments]),
        )
        # Observed-sessions overlay (DEC-CALENDAR-OBSERVED-SESSIONS-001): loaded ONCE
        # per pull and threaded into the completeness detectors' margin arithmetic
        # ONLY (an observed holiday is not a data hole) — NEVER into window planning.
        # A dry-run threads the empty overlay (the ``detect_completeness_gaps`` gating
        # pattern); ``load_observed_holidays`` never raises (missing/corrupt -> empty).
        _observed_holidays = frozenset() if dry_run else load_observed_holidays(lake_root)
        # Emit the resolved client connection-pool width once (DEC-LAKE-WRITE-INSTRUMENT-001)
        # so the write/lake latency telemetry can be sized against the real budget.
        _emit_pull_pool_size(pid)
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
        # WINDOW-level live sync (DEC-SPOT-WINDOW-PROGRESS-001): when the dashboard wires
        # ``on_ledger_sync`` the pull advances the bar + flushes /coverage per fetched
        # WINDOW (so a long backfill never LOOKS frozen between minutes-apart symbol
        # completions). The throttled, thread-safe callback OWNS the ledger flush in that
        # mode (single writer, no race), so the per-pair flush below is suppressed;
        # otherwise the per-pair flush is byte-unchanged.
        live_sync = make_live_sync(ledger, ledger_flush, on_ledger_sync)
        live_sync_active = on_ledger_sync is not None and ledger is not None
        # PER-WINDOW pull-health observe (DEC-PULL-STALL-DETECTION-001 GAP 2): wrap the
        # window live-sync so each PROCESSED window also pushes the live rate/throughput
        # snapshot to the panel + the monitor, so the dwell-gated pull.stalled rule
        # accumulates on the pull's own activity cadence (it would never accumulate from
        # the post-loop boundary call alone). A no-op extra when neither an observer nor a
        # monitor is wired (the wrapper returns live_sync unchanged).
        window_observer = make_window_observer(
            live_sync, rate_observer, _rate_controller, adapter, _pull_health_monitor
        )

        def _progress_then_flush(label: str, bars: int, failed: bool) -> None:
            if on_progress is not None:
                on_progress(label, bars, failed)
            if not live_sync_active:
                ledger_flush()

        # The "Retry failed windows" button passes ``auto_retry=True`` so leftover
        # failed windows are auto-re-pulled in bounded rounds AFTER the pull (only
        # the still-failed windows, via the shared window ledger — rate-safe,
        # idempotent; DEC-INGEST-AUTO-RETRY-001). The default one-click Pull-ALL
        # keeps ``auto_retry=False`` (a single pass) — byte-for-byte the prior
        # behaviour. BOTH paths share the same ingest spine (CLAUDE.md §11 COMMON);
        # the per-symbol rollup is the FIRST pass's report of EACH phase.
        final_still_failed = 0
        final_auth_aborted = False
        final_auth_cause: str | None = None
        final_quota_exhausted = False
        # The mass-empty circuit-breaker halt cause (DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001):
        # set when a phase raises MassEmptySettleHalt — drives the loud, needs-operator
        # ``mass_empty_halt`` verdict naming the counts + the likely causes.
        final_mass_empty_cause: str | None = None
        # Per-phase FIRST-pass reports keyed by interval — the per-symbol rollup reads
        # each interval's 1-day/1-minute outcome from its own phase's report. Both
        # phases run over EVERY requested instrument (DEC-SPOT-INTERVAL-DECOUPLE-001 —
        # the minute phase is no longer filtered by 1-day coverage).
        phase_reports: list[GrowwIngestReport] = []

        def _run_phase(
            phase_intervals: tuple[str, ...],
            phase_instruments: Sequence[Instrument],
            phase_windows: SpotWindows | None,
        ) -> None:
            """Run ONE phase (1-day or 1-minute) over the SHARED ledger; fold its outcome.

            The single ingest call site (CLAUDE.md §11 COMMON) — both phases funnel
            through here, differing only by *phase_intervals* and the
            *phase_instruments*. Both phases run over the SAME full instrument list
            with *phase_windows* = ``None`` (DEC-SPOT-INTERVAL-DECOUPLE-001 — the
            1-minute fetch is no longer bounded by the 1-day coverage; it pulls the
            full ``[start, end]`` range for every requested symbol). Preserves EXACTLY
            the existing until-clean / auto-retry / mid-round ``DailyQuotaExceeded`` /
            verdict behaviour — it just runs once per phase over the shared ledger.
            Aggregates into the ``final_*`` outer state: ``still_failed`` SUMS across
            phases; ``auth_aborted`` / ``quota_exhausted`` are ANY phase's; the FIRST
            pass of each phase is appended to ``phase_reports`` for the rollup.
            """
            nonlocal final_still_failed, final_auth_aborted, final_quota_exhausted
            nonlocal final_mass_empty_cause
            passes: list[SpotIngestWithRetry] = []
            # Synthesized quarantine rows for a TERMINAL mid-pass auth halt
            # (DEC-GROWW-AUTH-HALT-001): the spine now RAISES a dead-credential
            # auth error before returning its report, so the aborted pass never
            # lands in ``passes`` — these stand in for the per-pair FAILED rows the
            # pre-halt quarantine produced (empty on every non-auth path).
            auth_failures: tuple[PairFailure, ...] = ()

            def _one_pass(do_retry: bool) -> SpotIngestWithRetry:
                """Run ONE full pull-pass-with-auto-retry over the SHARED window ledger.

                The one-shot path, every until-clean round, and the auto-retry inner
                loop all go through here, so the args can never drift between them.
                Every COMPLETED pass is recorded in ``passes`` so a mid-round quota
                death can still report the last finished round's state.
                """
                res = run_groww_spot_ingest_with_retry(
                    adapter,
                    sink,
                    instruments=phase_instruments,
                    intervals=phase_intervals,
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
                    audit=ingest_audit,
                    on_window=window_observer,
                    symbol_windows=phase_windows,
                    write_pool=_write_consumer,
                    fetch_pipeline=_fetch_pipeline,
                    observed_holidays=_observed_holidays,
                    storm_breaker=_storm_breaker,
                    floor_learner=_floor_learner,
                )
                passes.append(res)
                return res

            try:
                # Re-pull-until-clean (DEC-TOKEN-PREFLIGHT-001): keep running bounded
                # rounds — each a full pass over the SHARED window ledger (so it
                # re-fetches ONLY the still-failed windows; rate-safe + idempotent) —
                # until clean / token dead / outer budget spent. Reuses
                # ``run_until_clean`` over the SAME ``retry_failed_windows`` engine;
                # the round COUNT is bounded (never an O(1)-total claim).
                if until_clean:

                    def _round_runner() -> UntilCleanRound:
                        """One until-clean round = one full pull pass.

                        Groww's report has no ``auth_cause`` string (no fail-fast
                        latch — the documented divergence; it DERIVES ``auth_aborted``
                        from the quarantined failures), so the round leaves
                        ``auth_cause`` at its ``None`` default (mirrors the CLI).

                        DEC-UNTIL-CLEAN-ALL-OUTSTANDING-001 — the round also reports the
                        ledger-derived ``outstanding`` (``total - settled``: PLANNED +
                        FAILED + EMPTY-below-threshold) + the progress measures
                        (``settled`` and the summed ``empty_streak``), so the loop
                        re-pulls until EVERY window is SUCCESS / SKIPPED ("pull until
                        successful"), not merely until the transient failures clear. A
                        dry-run (``ledger is None``) leaves them ``None`` → the legacy
                        ``still_failed_windows`` behaviour (byte-identical).
                        """
                        res = _one_pass(True)
                        outstanding = settled = streak = None
                        if ledger is not None:
                            prog = ledger.progress_counts()
                            outstanding = prog.total - prog.settled
                            settled = prog.settled
                            # Summed empty_streak — the secondary progress signal (an
                            # EMPTY window stepping toward SKIPPED advances this before
                            # ``settled`` rises). A dedicated single-lock O(N) fold (no
                            # sort), once per OUTER round, NOT the per-bar hot path.
                            streak = ledger.streak_total()
                        return UntilCleanRound(
                            still_failed_windows=res.report.still_failed_windows,
                            auth_aborted=res.report.auth_aborted,
                            outstanding_windows=outstanding,
                            settled_windows=settled,
                            streak_total=streak,
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
                    final_still_failed += until_outcome.final_still_failed
                    final_auth_aborted = (
                        final_auth_aborted
                        or until_outcome.auth_aborted
                        or (bool(passes) and passes[0].report.auth_aborted)
                    )
                    final_quota_exhausted = final_quota_exhausted or until_outcome.stopped_on_quota
                else:
                    res = _one_pass(auto_retry)
                    final_still_failed += res.report.still_failed_windows
                    final_auth_aborted = final_auth_aborted or res.report.auth_aborted
            except DailyQuotaExceeded as exc:
                # The daily quota died MID-round (DEC-SPOT-QUOTA-MIDROUND-001, the
                # spot arm of the options DEC-OPTIONS-UNTIL-CLEAN-001 mapping): the
                # until-clean loop's quota hook can only brake at round boundaries, so
                # a worker's pace bucket raises this mid-pass — and it previously
                # escaped as a generic job error BEFORE the ledger / status persist.
                # Everything already committed survives: bars per pair via the sink,
                # window records via the throttled flush + the finally below. Map to
                # the loud quota verdict instead of a generic job error; the next pull
                # (after IST midnight) resumes incrementally over the SAME ledger.
                final_quota_exhausted = True
                final_still_failed += passes[-1].report.still_failed_windows if passes else 0
                final_auth_aborted = final_auth_aborted or (
                    bool(passes) and passes[-1].report.auth_aborted
                )
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
            except MassEmptySettleHalt as exc:
                # The mass-empty circuit breaker TRIPPED mid-phase
                # (DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001): a suspected vendor soft-throttle
                # [R2] / mis-addressing [R3] episode was answering windows with clean-empty
                # 200s, so the spine HALTED before settling them SKIPPED. Everything already
                # committed survives (bars via the sink, window records via the throttled
                # flush + the outer finally); every un-settled window stays retryable. Map to
                # the loud needs-operator ``mass_empty_halt`` verdict (naming the counts +
                # candidate causes) instead of a generic job error. Because the halt is
                # CAUGHT here (it never escapes to the job layer's ``is_pull_halt``
                # escaping-exception path), the CRITICAL Telegram pull-halt alert is fired
                # HERE — parity with the F&O route and the CLI ``_pull_halt_errors`` set
                # (DEC-TELEGRAM-PULL-HALT-001; minor M-3). Best-effort by construction —
                # the helper can never mask the verdict flow.
                emit_pull_halt_alert(exc)
                final_mass_empty_cause = str(exc)
                # HONEST UNDERCOUNT: ``passes`` holds only COMPLETED passes — the pass the
                # halt aborted never appended, so its still-pending windows are NOT in this
                # tally (the verdict's ``pending_windows`` reads low after a mid-pass halt).
                # Harmless for control flow — the halt verdict already demands operator
                # triage + a re-pull, and the ledger (not this count) is the retry
                # authority — but do not present this number as the full residual.
                final_still_failed += passes[-1].report.still_failed_windows if passes else 0
                final_auth_aborted = final_auth_aborted or (
                    bool(passes) and passes[-1].report.auth_aborted
                )
                _LOG.warning(
                    "universe.pull_all.mass_empty_halt",
                    pull_id=pid,
                    error=str(exc),
                    rounds_completed=len(passes),
                    note=(
                        "mass clean-empty responses — pull halted before settling; "
                        "committed work kept; un-settled windows stay retryable"
                    ),
                )
            except GrowwAuthError as exc:
                # A TERMINAL dead-credential auth failure HALTED the spine mid-phase
                # (DEC-GROWW-AUTH-HALT-001 — the spine's ``_GLOBAL_STOP_ERRORS`` raise).
                # Like the quota / mass-empty stops above, it is CAUGHT here and mapped
                # to the existing loud ``auth_expired`` verdict (the graceful abort the
                # pre-halt quarantine produced) instead of escaping as a generic job
                # error; the RETRYABLE auth-family subclasses (a throttled mint / a
                # stale read-only re-read) never reach here from the spine and re-raise
                # unchanged if they somehow do. Because the halt is CAUGHT (it never
                # escapes to the job layer's ``_is_pull_halt`` path), the CRITICAL
                # Telegram pull-halt alert fires HERE — parity with the mass-empty
                # handler + the CLI ``_pull_halt_errors`` set
                # (DEC-TELEGRAM-PULL-HALT-001; best-effort by construction).
                if not is_terminal_auth_failure(exc):
                    raise
                emit_pull_halt_alert(exc)
                final_auth_aborted = True
                final_still_failed += passes[-1].report.still_failed_windows if passes else 0
                # Synthesize the per-pair quarantine rows the pre-halt spine reported
                # (every phase pair FAILED against the dead credential — the halt
                # rationale). HONEST envelope: a symbol that completed BEFORE the
                # mid-pass death is conservatively reported failed here (its outcome
                # is unknowable once the pass aborted); the window LEDGER — not this
                # summary — stays the retry authority, and its settled windows are
                # skipped by the next pull (Golden Rule #7 idempotence).
                auth_failures = tuple(
                    PairFailure(
                        symbol=inst.symbol_name,
                        interval=phase_interval,
                        error=str(exc),
                        auth_failure=True,
                    )
                    for inst in phase_instruments
                    for phase_interval in phase_intervals
                )
                _LOG.warning(
                    "universe.pull_all.auth_halt",
                    pull_id=pid,
                    error_type=type(exc).__name__,
                    rounds_completed=len(passes),
                    note=(
                        "terminal Groww auth failure — dead credential / re-mint budget "
                        "exhausted; committed work kept; re-pull after re-auth"
                    ),
                )
            # The per-symbol rollup reads each phase's FIRST pass; an absent (no pass
            # completed) phase contributes an empty report — except a terminal auth
            # halt, whose synthesized quarantine rows preserve the pre-halt shape.
            phase_reports.append(
                passes[0].report if passes else GrowwIngestReport(pairs={}, failures=auth_failures)
            )

        # Written-vs-settled census snapshot (DEC-WRITTEN-SETTLED-CENSUS-001):
        # captured at the minute phase's reconcile below — the SAME already-loaded
        # spans + the just-reconciled ledger rows, a pre-pull-CONSISTENT pair —
        # and emitted ONCE post-drain. Empty when that phase never runs.
        spot_census_spans: list[dict[str, Any]] = []
        spot_census_rows: list[tuple[Any, ...]] = []
        # Success-path latch for the post-drain L2 lake heal below (DEC-LAKE-HEAL-001):
        # set as the try body's LAST statement, so a phase error leaves it False and
        # the ``finally`` skips the heal (the drains + ledger persist still run).
        fetch_phases_ok = False
        try:
            # ── Phase 1 — 1-day ONLY first (over EVERY symbol; full range) ──
            if daily_requested:
                _run_phase((_PHASE1_DAILY,), instruments, None)
            # ── Phase 2 — 1-minute over EVERY symbol, full range (DECOUPLED) ──
            # DEC-SPOT-INTERVAL-DECOUPLE-001 (MIGRATION.md #851): the 1-minute pull is
            # FULLY DECOUPLED from the 1-day coverage — it pulls its full ``[start,
            # end]`` range for EVERY requested symbol (no coverage discovery, no
            # per-symbol filtering, no minute-start bounding). ``symbol_windows=None``
            # flows to plan_windows / reconcile_ledger_with_lake / _run_phase so the
            # full range is used. SUPERSEDES the 1-minute-bounded-to-1-day-coverage
            # coupling of DEC-SPOT-1DAY-FIRST-001. Per DEC-1MINUTE-ONLY-DERIVE-1DAY-001
            # this 1-minute phase is the ONLY vendor phase (1-day is derived after).
            if minute_requested:
                # GLOBAL-STOP GUARD: if Phase 1 ended with a dead token, a spent quota,
                # or a tripped mass-empty breaker, the minute pull would fail (or poison)
                # identically — SKIP it honestly (the verdict already carries the
                # actionable state).
                if (
                    final_auth_aborted
                    or final_quota_exhausted
                    or final_mass_empty_cause is not None
                ):
                    _LOG.warning(
                        "universe.pull_all.minute_skipped_global_stop",
                        pull_id=pid,
                        auth_aborted=final_auth_aborted,
                        quota_exhausted=final_quota_exhausted,
                        mass_empty_halt=final_mass_empty_cause is not None,
                    )
                else:
                    if ledger is not None:
                        # Cold-start seed FIRST, then PLAN Phase 2's full-range minute
                        # windows (``symbol_windows=None`` → the global ``[start, end]``
                        # per symbol). The seed is ABSENT-ONLY
                        # (``WindowLedger.seed_lake_success`` never overwrites ANY
                        # existing row — a PLANNED one included), so it MUST precede
                        # ``plan_windows``: seeding after the plan was a permanent no-op
                        # and a cold start re-fetched every lake-held window
                        # (forensic-audit #7). ``plan`` is itself absent-only, so a
                        # just-seeded SUCCESS row stays settled. The DAILY interval is
                        # never in this phase. Guarded on a non-None ledger (the live
                        # pull; ``begin_pull_ledger`` returns None for a dry-run, which
                        # simply skips the ledger reconcile + plan, mirroring Phase 1).
                        # The span read is hoisted out of ``reconcile_ledger_with_lake``
                        # so the SAME loaded spans also feed the written-vs-settled
                        # census snapshot below (DEC-WRITTEN-SETTLED-CENSUS-001 —
                        # "reuse the already-loaded spans", zero extra I/O); the
                        # reconcile itself is byte-identical (`reconcile_ledger_windows`
                        # is the pure core that wrapper composed).
                        spot_census_spans = load_lake_spans(lake_root)
                        reconcile_ledger_windows(
                            ledger,
                            spot_census_spans,
                            instruments=instruments,
                            intervals=minute_intervals,
                            start=start.date(),
                            end=end.date(),
                            market=GROWW_COVERAGE_MARKET,
                            window_days_for=groww_window_days_for,
                            symbol_windows=_floor_windows,
                        )
                        # Pre-plan ledger snapshot — pairs with the pre-pull spans
                        # above so this pull's own settles can never false-flag.
                        spot_census_rows = ledger.to_rows()
                        # Mechanized round-robin skipped-window audit
                        # (DEC-SKIPPED-AUDIT-ROUNDROBIN-001): re-open ≤ 25
                        # terminally-SKIPPED windows per pull (cursor-rotated,
                        # once per pull_id, ≈0.03% of the worst-case 8h @ 3 r/s
                        # call budget) on the pull's OWN ledger — BEFORE plan,
                        # so the reopened FAILED rows are simply re-fetched by
                        # this pull's normal spine (breaker-exempt via the
                        # REOPENED marker; DATA-SAFE by construction). Runs
                        # AFTER the census snapshot above so the audit's own
                        # demotions never enter the pre-pull census pair.
                        # Best-effort: any error is a 0 no-op, never a block.
                        run_skipped_audit_guarded(ledger, lake_root, pull_id=pid)
                        plan_windows(
                            ledger,
                            instruments=instruments,
                            intervals=minute_intervals,
                            start=start.date(),
                            end=end.date(),
                            market=GROWW_COVERAGE_MARKET,
                            window_days_for=groww_window_days_for,
                            symbol_windows=_floor_windows,
                        )
                    # Learned-floor clamp (DEC-VENDOR-FLOOR-LAZY-PROBE-001): a symbol
                    # with a persisted learned floor pulls its 1-minute range from
                    # ``max(GROWW_HISTORY_FLOOR, learned floor)`` — the SAME
                    # ``_floor_windows`` the plan above tiled, so planned keys ==
                    # fetched keys; ``None`` (no learned floors) is byte-identical.
                    _run_phase(minute_intervals, instruments, _floor_windows)
            # Fetch phases done — arm the post-drain L2 lake heal in the finally.
            fetch_phases_ok = True
        finally:
            # Drain the FULL async fetch/process pipeline FIRST (DEC-GROWW-FETCH-ASYNC-SEPARATION-001
            # MAJOR-4 fix): a pipeline consumer's tail calls ``sink.append`` which may FEED the
            # batched write consumer, so the pipeline MUST be drained (every queued tail run)
            # BEFORE the write consumer is stopped — draining the write consumer first could lose
            # a late write a still-running pipeline consumer emits. Process every still-queued
            # window's tail + record its ledger row BEFORE the ledger persist, so
            # settle-after-durable holds at pull granularity. A no-op when the pipeline is OFF
            # (the synchronous default); a captured disk-full halt PROPAGATES (the graceful
            # pull-halt path), any other drain error is swallowed.
            _drain_fetch_pipeline(_fetch_pipeline)
            # Drain the coalescing batched write consumer AFTER the pipeline (DEC-WRITE-BATCH-
            # COALESCE-001): flush every still-queued window durably + join the writer BEFORE
            # saving the ledger, so every settled window is on disk (settle-after-durable). A
            # no-op when the batched path is OFF (the synchronous default). Best-effort: a drain
            # error is logged, never re-raised, so the ledger persist below always runs.
            _drain_write_consumer(_write_consumer)
            # NOTE (DEC-PIPELINE-AUTOPREBUILD-001): the always-on mid-pull 1-day
            # derivation that ran here (DEC-1MINUTE-ONLY-DERIVE-1DAY-001's
            # ``_resample_daily_after_pull``) is REMOVED — derived data (the 19
            # intraday TFs + the derived ``1day``) is written ONLY by the pipeline's
            # final prebuild phase (brutex.dashboard.pipeline_prebuild), which runs
            # strictly after a fully-successful fetch. Never mid-pull.
            # DEC-LAKE-HEAL-001 L2: heal the just-pulled slice at the SAME
            # post-drain seam (the fetched 1-minute is durably in the lake only
            # after both drains above), so the pipeline's later prebuild phase
            # reads clean 1-minute. SUCCESS-path only (``fetch_phases_ok``);
            # LakeWriteHalt re-raises inside (graceful pull-halt), any other
            # heal error is swallowed — it can never fail the pull.
            if fetch_phases_ok and not dry_run:  # pragma: no cover — raw lake I/O glue
                _heal_after_pull(
                    instruments=instruments,
                    lake_root=lake_root,
                    start=start.date(),
                    end=end.date(),
                    pull_id=pid,
                )
            # ALWAYS persist the ledger — a crash / abort / mid-round quota death
            # must never discard the settled-window history
            # (DEC-SPOT-QUOTA-MIDROUND-001; previously persisted only on a clean
            # return, shielded just by the throttled mid-pull flushes). Idempotent
            # with those flushes. The legacy status-sidecar persist that used to
            # nest here was write-retired (DEC-STATUS-SIDECAR-WRITE-RETIRE-001) —
            # the ledger is the skip authority (DEC-WINDOW-LEDGER-001).
            end_pull_ledger(ledger, lake_root, dry_run=dry_run)
            # Final authoritative window-progress push (DEC-SPOT-WINDOW-PROGRESS-001): the
            # throttled live-sync may have suppressed the last tick, so land the EXACT end
            # state on the dashboard bar. Runs single-threaded here (the pull is done — no
            # worker can race it), in the finally so even a crash/abort lands the partial
            # progress (the operator sees how far the pull got, e.g. "5,819/10,906").
            if on_ledger_sync is not None and ledger is not None:
                final = ledger.progress_counts()
                on_ledger_sync(final.resolved, final.total, final.bars, final.failed, final.current)
        # Per-symbol rollup combines BOTH phases (DEC-SPOT-INTERVAL-DECOUPLE-001): a
        # symbol's 1-day result comes from the 1-day phase's report + its 1-minute
        # result from the 1-minute phase's — both phases run over EVERY symbol now (no
        # 1-day-coverage skip), so a symbol missing one interval only means that
        # interval was not requested. Each phase's report is the FIRST
        # pass (the until-clean contract); a quota death before ANY pass completed leaves
        # an empty report → that interval's outcome is UNKNOWN (claim nothing — the quota
        # verdict carries the actionable state). The merged pair/failure maps fold across
        # phases so the lookup below is one O(1) dict get per (symbol, interval).
        merged_pairs: dict[tuple[str, str], IngestReport] = {}
        failure_keys: set[tuple[str, str]] = set()
        for phase_report in phase_reports:
            merged_pairs.update(phase_report.pairs)
            failure_keys.update((f.symbol, f.interval) for f in phase_report.failures)
        # Fold the async pipeline's post-drain per-series totals back into the deferred per-pair
        # reports (DEC-GROWW-FETCH-ASYNC-SEPARATION-001 completion-count fix): in async mode the
        # producer returned ``bars_appended == 0`` per series (the consumer did the durable write
        # on a background thread), so the rollup below would falsely read 0 bars → ``completed ==
        # 0``. The pipeline is already DRAINED in the finally above, so its totals are final. A
        # no-op (synchronous path) when ``_fetch_pipeline`` is ``None``.
        merged_pairs = _reconcile_pairs_with_pipeline(merged_pairs, _fetch_pipeline)
        # Observe-only written-vs-settled census (DEC-WRITTEN-SETTLED-CENSUS-001):
        # one guarded post-drain emit over the pre-plan snapshot captured at the
        # minute phase's reconcile — DETECT+WARN only (the #1014 wiring pattern),
        # can never break the pull. A dry-run / skipped minute phase emits over
        # empty inputs → a silent clean census.
        emit_written_settled_census(
            spot_census_rows,
            spot_census_spans,
            intervals=minute_intervals,
            symbols=frozenset(inst.symbol_name for inst in instruments),
            source="spot",
        )
        _observe_rate(rate_observer, _rate_controller, adapter, _pull_health_monitor)
        # Groww has NO no-data marker — unlike the previous vendor's no-data 400
        # marker, which was special-cased as a benign KNOWN_GAP. Per Golden Rule #1 we
        # do not invent a Groww twin, so every terminal Groww error is a uniform terminal
        # quarantine → `failed` (`transient_residual`). A settled EMPTY window is
        # still surfaced as `no_data` below when a pair ran cleanly but returned zero
        # bars; there is no known-gap split (no_data from a terminal failure has no
        # Groww source).
        results: list[SymbolPullResult] = []
        for instrument in instruments:
            symbol = instrument.symbol_name
            completed = failed = no_data = 0
            for fetch_interval in intervals:
                key = (symbol, fetch_interval)
                if key in failure_keys:
                    failed += 1
                else:
                    pair = merged_pairs.get(key)
                    if pair is None:
                        # The interval was never run for this symbol (a 3c-skipped
                        # minute, or a quota death before the pair ran) — its outcome
                        # is UNKNOWN, so claim nothing (DEC-SPOT-1DAY-FIRST-001 /
                        # DEC-SPOT-QUOTA-MIDROUND-001).
                        continue
                    if pair.bars_appended > 0:
                        completed += 1
                    else:
                        no_data += 1
            results.append(
                SymbolPullResult(symbol=symbol, completed=completed, failed=failed, no_data=no_data)
            )
        # Vendor-behaviour honesty counters (DEC-VENDOR-CONFLICT-SURFACE-001): the
        # post-reconcile merged pairs carry the real per-series counts on BOTH the
        # sync and async paths, so the report-level sums/max-fold are path-invariant.
        bulk_conflicts = sum(pair.conflicts for pair in merged_pairs.values())
        bulk_dup_rows = sum(pair.vendor_dup_rows_dropped for pair in merged_pairs.values())
        bulk_max_delta: Decimal | None = None
        for pair in merged_pairs.values():
            bulk_max_delta = fold_max_close_delta(bulk_max_delta, pair.max_close_delta)
        bulk = BulkSpotReport(
            dry_run=dry_run,
            start_date=start.date().isoformat(),
            end_date=(end - timedelta(days=1)).date().isoformat(),
            symbols=results,
            auth_aborted=final_auth_aborted,
            auth_cause=final_auth_cause,
            still_failed_windows=final_still_failed,
            quota_exhausted=final_quota_exhausted,
            conflicts=bulk_conflicts,
            vendor_dup_rows_dropped=bulk_dup_rows,
            max_close_delta=bulk_max_delta,
            # Each phase's FIRST pass's settled-window skips (until-clean rounds
            # re-skip the same windows by design — summing passes would double-count).
            windows_skipped=sum(pr.skipped_windows for pr in phase_reports),
            ledger_load_failed=ledger_reload_failed,
            mass_empty_cause=final_mass_empty_cause,
            # Honest-verdict settle tally (DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001): the
            # per-pull ledger instance counted every window THIS pull newly settled
            # SKIPPED (fast-settle + streak promotion + degraded accept-gap), so the
            # verdict can NAME the count + the reopen-skipped re-verify path. 0 for a
            # dry-run (no ledger — nothing settles).
            settled_skipped=ledger.newly_settled_skipped if ledger is not None else 0,
        )
        verdict = bulk.verdict
        _LOG.info(
            "universe.pull_all.done",
            pull_id=pid,
            instruments=bulk.total_symbols,
            completed=bulk.total_completed,
            failed=bulk.total_failed,
            windows_skipped=bulk.windows_skipped,
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
        # NOTE (DEC-PIPELINE-AUTOPREBUILD-001): the BRUTEX_AUTO_DERIVE_TFS pull-settle
        # auto-derive (DEC-TF-MATERIALIZE-002's ``_autoderive_after_pull``) is REMOVED
        # — derived timeframes are written ONLY by the pipeline's final prebuild phase.
        # The G5 finish record (pairs with the start above): a crash anywhere in
        # between leaves start-without-finish — exactly the interrupted-pull
        # signature stage 12 diagnoses. Never raises.
        record_pull_finish(lake_root, pull_id=pid, verdict=verdict.kind)
    return bulk


def _heal_after_pull(  # pragma: no cover — raw lake I/O glue; heal core is 100%-unit-tested
    *,
    instruments: Sequence[Instrument],
    lake_root: Path,
    start: date,
    end: date,
    pull_id: str,
) -> None:
    """L2 lake heal over the just-pulled coordinates (DEC-LAKE-HEAL-001).

    Runs the COMMON :func:`~brutex.data.lake_heal.heal_after_pull` core —
    a scan BOUNDED to the pulled ``(exchange, segment, symbol)`` targets ×
    the pulled span's UTC months, then the automatic keep-FIRST heal
    (``BRUTEX_LAKE_HEAL=observe`` logs only) — at the post-drain seam, so
    the pipeline's final prebuild phase (DEC-PIPELINE-AUTOPREBUILD-001,
    which now owns ALL derived-TF/1-day writes) reads clean 1-minute.
    :class:`~brutex.data.errors.LakeWriteHalt` RE-RAISES (a disk-full heal
    must halt the pull exactly like any lake write — it reaches the existing
    ``_is_pull_halt`` / ``emit_pull_halt_alert`` handling); every OTHER error
    is swallowed + logged ``lake_heal.post_pull_error`` (a heal bug never
    fails a successful pull). The catalog at ``<lake>/catalog.duckdb`` is
    opened best-effort WHEN THE FILE EXISTS (the L3 sweeper's convention, via
    the COMMON :func:`~brutex.data.lake_heal.open_lake_catalog`) so a healed
    partition's rotated ``raw_hash`` reaches the catalog immediately — an
    open failure logs + proceeds with ``catalog=None``, never blocking the
    pull. Marked ``no cover`` per the raw-lake-I/O-glue convention; the core
    is 100%-unit-tested and the glue contract is pinned by
    ``tests/integration/test_lake_heal_pull_seam.py``.
    """
    from brutex.data.errors import LakeWriteHalt
    from brutex.data.lake_heal import heal_after_pull, lake_heal_audit, open_lake_catalog
    from brutex.notifications.sweep_alerts import emit_lake_heal_alert

    scope = f"pull:{pull_id}"
    try:
        with lake_heal_audit(lake_root) as audit:
            catalog = open_lake_catalog(lake_root)
            try:
                heal_after_pull(
                    lake_root=lake_root,
                    targets=[
                        (inst.exch_id.value, inst.segment.value, inst.symbol_name)
                        for inst in instruments
                    ],
                    start=start,
                    end=end,
                    scope=scope,
                    catalog=catalog,
                    audit=audit,
                    on_report=lambda report: emit_lake_heal_alert(report, scope=scope),
                )
            finally:
                if catalog is not None:
                    catalog.close()
    except LakeWriteHalt:
        raise
    except Exception as exc:  # pylint: disable=broad-exception-caught — never fail the pull
        _LOG.warning("lake_heal.post_pull_error", pull_id=pull_id, error=str(exc))


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
    floor_store: VendorFloorMetaStore | None = None,
) -> PullCandlesReport:
    """Ingest *instrument*'s spot candles over a date range (Groww model).

    Plans + drives one
    :func:`~brutex.adapters.groww.run_ingest.run_groww_spot_ingest` pass over
    the single *instrument* and the Groww **fetched** base intervals
    (the injected *intervals*, defaulting to :data:`GROWW_FETCH_INTERVALS` —
    ``1minute`` ONLY, DEC-1MINUTE-ONLY-DERIVE-1DAY-001; ``1day`` is DERIVED from
    the just-pulled 1-minute after the pull, and the 19 intraday timeframes are
    resampled from the same 1-minute base, per DEC-DHAN-1M-ONLY-001). The window is resolved by
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
    floor_store
        Optional :class:`~brutex.adapters.groww.vendor_floor.VendorFloorMetaStore`
        (DEC-VENDOR-FLOOR-LAZY-PROBE-001) — the single-symbol twin of the
        :func:`pull_all_spot` wiring: read for the learned-floor planning clamp,
        written by the in-pull lazy depth learner. ``None`` (tests / dry-run) is
        byte-identical full-range planning.

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
    # Register the pool's shared MEASURED-rate meter for the observe-only sampler
    # (DEC-RATE-WIDGET-FNO-MEASURED-001) — see :func:`pull_all_spot`. No-op offline /
    # when no live ``session_rate`` is wired; the spot per-window observe is unchanged.
    register_pull_meter(session_rate, adapter)

    # Open the pull's correlation scope (DEC-DHAN-INGEST-LOGGING-001): ``pid`` is
    # bound into the structlog contextvars for the whole pass, so this start/done
    # summary, the ingest spine's ``ingest.*`` events, and the already-lit
    # transport (``groww.call``) + rate (``rate.*``) events all share one
    # ``pull_id``. Always unbinds on exit (no leak).
    # Golden Rule #8 hash-chained ingest audit (DEC-INGEST-AUDIT-WIRING-001): bind
    # the SAME COMMON trail the CLI ``brutex fetch`` pull writes —
    # ``<lake>/audit/spot-ingest.jsonl`` via the ONE ``open_ingest_audit`` factory
    # (resume = continue the existing chain; a dry-run gets the null context — no
    # real data, no audit). The spine appends RUN_START → per-pair → RUN_COMPLETE
    # on its controller drain thread, O(1) per event, never on a fetch worker.
    with (
        pull_id_scope(pull_id) as pid,
        open_ingest_audit(lake_root, enabled=not dry_run) as ingest_audit,
    ):
        # The coverage reconcile band-aids are RETIRED (DEC-WINDOW-LEDGER-001,
        # Stage d) — the ledger's per-window record + the cold-start
        # `reconcile_ledger_with_lake` below are the sole skip source (the
        # FetchCoverage set was deleted in Stage e). The legacy window_status.json
        # sidecar is WRITE-RETIRED (DEC-STATUS-SIDECAR-WRITE-RETIRE-001) — kept on
        # disk read-only for the db_view fallback + one-shot ledger migration.
        # 1-MINUTE-ONLY fetch (DEC-1MINUTE-ONLY-DERIVE-1DAY-001): the SPOT pull fetches
        # ONLY ``1minute`` from Groww (``resolve_fetch_intervals`` → ``("1minute",)``),
        # so only the 1-minute phase runs; ``1day`` is DERIVED from the stored 1-minute
        # by the pipeline's final prebuild phase (DEC-PIPELINE-AUTOPREBUILD-001 — never
        # mid-pull, never vendor-fetched). The dormant ``1day``
        # phase block is RETAINED for any defensive caller that injects
        # ``intervals=("1day",)`` directly. The decoupled-phase plumbing
        # (DEC-SPOT-INTERVAL-DECOUPLE-001, MIGRATION.md #851) is unchanged; both phases
        # pass ``symbol_windows=None``.
        daily_requested = _PHASE1_DAILY in intervals
        minute_intervals: tuple[str, ...] = tuple(i for i in intervals if i != _PHASE1_DAILY)
        minute_requested = bool(minute_intervals)
        # Unified per-window ledger (DEC-WINDOW-LEDGER-001): load/migrate ONCE for the
        # whole pull. Only the 1-minute phase runs; ``1day`` is derived after the pull.
        ledger = begin_pull_ledger(lake_root, dry_run=dry_run)
        if ledger is not None and daily_requested:
            # Cold-start seed FIRST (DEC-WINDOW-LEDGER-001, Stage b1): seed SUCCESS
            # for this symbol's lake-enclosed windows so the upcoming is_skippable
            # flip won't re-pull bars the lake already holds. The seed is
            # ABSENT-ONLY (``seed_lake_success`` never overwrites ANY existing row
            # — a PLANNED one included), so it MUST run BEFORE ``plan_windows``
            # mints the PLANNED rows (seeding after the plan was a permanent no-op
            # — forensic-audit #7); ``plan`` is itself absent-only, so a seeded
            # SUCCESS row stays settled. A non-None ledger already implies a live
            # pull (begin_pull_ledger returns None for dry_run), so no extra
            # dry-run guard is needed. ``market=GROWW_COVERAGE_MARKET`` matches the
            # Groww spine namespace.
            reconcile_ledger_with_lake(
                ledger,
                lake_root,
                instruments=[instrument],
                intervals=(_PHASE1_DAILY,),
                start=start.date(),
                end=end.date(),
                market=GROWW_COVERAGE_MARKET,
                window_days_for=groww_window_days_for,
            )
            plan_windows(
                ledger,
                instruments=[instrument],
                intervals=(_PHASE1_DAILY,),
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
        # BIND the live pull-health monitor to the resolved controller
        # (DEC-PULL-HEALTH-WIRING-001) — its observe() is fed BOTH per-window (the
        # window_observer below — DEC-PULL-STALL-DETECTION-001 GAP 2, so the dwell-gated
        # pull.stalled rule actually accumulates) AND at the post-loop boundary; best-effort
        # (None for a dry-run, never crashes the pull).
        _pull_health_monitor = _build_pull_health_monitor(_rate_controller)
        # Coalescing batched write consumer (DEC-WRITE-BATCH-COALESCE-001): when
        # ``BRUTEX_WRITE_BATCH`` is opted in (DEFAULT OFF), route every sink.append through
        # a background writer that COALESCES pending same-series windows into ONE merge per
        # drain cycle, so the fetch lane stops idling on its own O(month-depth) write. It
        # changes ONLY how fetched data is WRITTEN — the RateController (per-second ceiling +
        # 500/min governor) is UNTOUCHED, so the issue rate still binds at the controller's
        # allowed cap (Golden Rule #6). ``None`` (the default / a dry-run) keeps the
        # synchronous write path byte-identical; drained in the ``finally`` before the
        # ledger is saved (so every settled window is durable).
        _write_consumer = _build_write_consumer(sink, dry_run=dry_run)
        # FULL async fetch/process separation (DEC-GROWW-FETCH-ASYNC-SEPARATION-001): when
        # ``BRUTEX_GROWW_FETCH_PIPELINE`` is opted in (DEFAULT OFF), the fetch lane does ONLY
        # fetch + enqueue and a SEPARATE consumer pool does ALL internal work, so internal
        # processing can never block or slow the fetch. The RateController (8/500 + AIMD)
        # stays entirely producer-side, UNTOUCHED. ``None`` (default / dry-run) keeps the
        # synchronous path byte-identical; drained in the ``finally`` before the ledger save.
        _fetch_pipeline = _build_fetch_pipeline(dry_run=dry_run, lake_root=lake_root)
        # Vendor-down error-storm breaker + dead-credential StillStale escalation
        # (DEC-VENDOR-DOWN-BREAKER-001): one per-pull breaker, armed ONLY for a REAL
        # live pull (a dry-run threads ``None`` — the ``detect_completeness_gaps``
        # gating below).
        _storm_breaker = None if dry_run else resolve_error_storm_breaker()
        # Lazy vendor-depth-floor learner + learned-floor planning clamp
        # (DEC-VENDOR-FLOOR-LAZY-PROBE-001) — the single-symbol twin of the
        # pull_all_spot wiring: armed only for a real pull, probing through the
        # PACED replica of THIS pull's adapter (`with_paced_client(adapter, pace)`
        # — the live pool leaves carry a no-op pace, so binding the session `pace`
        # here is what keeps the probe inside the 8 r/s ceiling + 500/min
        # governor, the review BLOCKER; DEC-RATE-SINGLE-ADMISSION-001); a ``None``
        # floor_store / no learned floor resolves ``_floor_windows`` to ``None``
        # — byte-identical planning.
        _floor_learner: VendorFloorLearner | None = (
            None
            if dry_run
            else make_vendor_floor_learner(
                with_paced_client(adapter, pace),
                history_floor=GROWW_HISTORY_FLOOR,
                today=end.date(),
                store=floor_store,
            )
        )
        _floor_windows = floor_symbol_windows(
            (symbol,),
            start=start.date(),
            end=end.date(),
            floors=load_vendor_floors(floor_store, (symbol,)),
        )
        # Observed-sessions overlay (DEC-CALENDAR-OBSERVED-SESSIONS-001): loaded ONCE
        # per pull and threaded into the completeness detectors' margin arithmetic
        # ONLY (an observed holiday is not a data hole) — NEVER into window planning.
        # A dry-run threads the empty overlay (the ``detect_completeness_gaps`` gating
        # pattern); ``load_observed_holidays`` never raises (missing/corrupt -> empty).
        _observed_holidays = frozenset() if dry_run else load_observed_holidays(lake_root)
        # Emit the resolved client connection-pool width once (DEC-LAKE-WRITE-INSTRUMENT-001)
        # so the write/lake latency telemetry can be sized against the real budget.
        _emit_pull_pool_size(pid)
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
        # PER-WINDOW pull-health observe (DEC-PULL-STALL-DETECTION-001 GAP 2): this
        # single-symbol path has no window-level ledger sync, so the inner per-window
        # callback is a no-op — but the monitor's dwell-gated pull.stalled rule still needs
        # a per-window observe to accumulate, so wrap a no-op inner. A no-op extra when
        # neither an observer nor a monitor is wired (the wrapper returns the no-op).
        window_observer = make_window_observer(
            lambda: None, rate_observer, _rate_controller, adapter, _pull_health_monitor
        )

        def _progress_then_flush(label: str, bars: int, failed: bool) -> None:
            if on_progress is not None:
                on_progress(label, bars, failed)
            ledger_flush()

        # Per-phase reports merged for the timeframe rollup below (a symbol skipped
        # from Phase 2 simply has no 1-minute pair → that interval is absent).
        merged_pairs: dict[tuple[str, str], IngestReport] = {}
        failure_keys: set[tuple[str, str]] = set()
        phase_auth_aborted = False

        def _run_phase(
            phase_intervals: tuple[str, ...], phase_windows: SpotWindows | None
        ) -> GrowwIngestReport:
            """Run ONE phase's single ingest pass over the shared ledger; fold its report."""
            nonlocal phase_auth_aborted
            try:
                phase_report = run_groww_spot_ingest(
                    adapter,
                    sink,
                    instruments=[instrument],
                    intervals=phase_intervals,
                    start=start.date(),
                    end=end.date(),
                    pace=pace,
                    on_rate_limit=on_rate_limit,
                    on_timeout=on_timeout,
                    on_success=on_success,
                    on_progress=_progress_then_flush,
                    ledger=ledger,
                    run_parallel=run_parallel,
                    audit=ingest_audit,
                    on_window=window_observer,
                    symbol_windows=phase_windows,
                    write_pool=_write_consumer,
                    fetch_pipeline=_fetch_pipeline,
                    # Real production Groww pull → OPT IN to the data-completeness re-pull demotion
                    # (DEC-INTERIOR-GAP-DETECT-001 / DEC-PARTIAL-WINDOW-REPULL-001). A dense INDEX
                    # 1-minute window with a confirmed interior hole / trailing truncation is
                    # re-pulled; dry-run / synthetic pulls leave this OFF (the default) so they never
                    # false-red on the synthetic adapter's first-day-only bars.
                    detect_completeness_gaps=True,
                    observed_holidays=_observed_holidays,
                    storm_breaker=_storm_breaker,
                    floor_learner=_floor_learner,
                )
            except GrowwAuthError as exc:
                # A TERMINAL dead-credential auth failure HALTED the spine mid-phase
                # (DEC-GROWW-AUTH-HALT-001): map it to the SAME graceful auth-abort the
                # pre-halt quarantine produced — the phase's pairs read FAILED, the
                # global-stop guard below skips the remaining phase(s), and because the
                # halt is CAUGHT here (it never escapes to the job layer's
                # ``_is_pull_halt`` path) the CRITICAL Telegram pull-halt alert fires
                # HERE (DEC-TELEGRAM-PULL-HALT-001 parity). A RETRYABLE auth-family
                # shape (throttled mint / stale read-only re-read) re-raises unchanged.
                if not is_terminal_auth_failure(exc):
                    raise
                emit_pull_halt_alert(exc)
                phase_auth_aborted = True
                failure_keys.update((symbol, phase_interval) for phase_interval in phase_intervals)
                _LOG.warning(
                    "universe.pull.phase_auth_halt",
                    pull_id=pid,
                    symbol=symbol,
                    intervals=list(phase_intervals),
                    error_type=type(exc).__name__,
                )
                return GrowwIngestReport(pairs={}, failures=())
            merged_pairs.update(phase_report.pairs)
            failure_keys.update((f.symbol, f.interval) for f in phase_report.failures)
            phase_auth_aborted = phase_auth_aborted or phase_report.auth_aborted
            return phase_report

        # Success-path latch for the post-drain L2 lake heal below (DEC-LAKE-HEAL-001):
        # set as the try body's LAST statement, so a phase error leaves it False and
        # the ``finally`` skips the heal (the drains + ledger persist still run).
        fetch_phases_ok = False
        try:
            # ── Phase 1 — 1-day ONLY first (full range) ──
            if daily_requested:
                _run_phase((_PHASE1_DAILY,), None)
            # ── Phase 2 — 1-minute over the full range (DECOUPLED) ──
            # DEC-SPOT-INTERVAL-DECOUPLE-001 (MIGRATION.md #851): the 1-minute pull is
            # FULLY DECOUPLED from the 1-day coverage — it pulls its full ``[start,
            # end]`` range for the requested symbol (no coverage discovery, no skip
            # when 1-day is absent). ``symbol_windows=None`` flows to plan_windows /
            # reconcile_ledger_with_lake / _run_phase so the full range is used.
            if minute_requested:
                # GLOBAL-STOP GUARD: a dead token in Phase 1 means the minute pull
                # would fail identically — skip it honestly (the timeframes already
                # carry Phase 1's quarantine).
                if phase_auth_aborted:
                    _LOG.warning("universe.pull.minute_skipped_global_stop", pull_id=pid)
                else:
                    if ledger is not None:
                        # Cold-start seed FIRST, then PLAN the full-range minute
                        # windows (``symbol_windows=None`` → the global ``[start,
                        # end]``). The seed is ABSENT-ONLY (``seed_lake_success``
                        # never overwrites ANY existing row — a PLANNED one
                        # included), so it MUST precede ``plan_windows`` (seeding
                        # after the plan was a permanent no-op — forensic-audit #7);
                        # ``plan`` is itself absent-only, so a seeded SUCCESS row
                        # stays settled. Guarded on a non-None ledger (the live pull;
                        # ``begin_pull_ledger`` returns None for a dry-run, which
                        # simply skips the ledger reconcile + plan, mirroring
                        # Phase 1).
                        reconcile_ledger_with_lake(
                            ledger,
                            lake_root,
                            instruments=[instrument],
                            intervals=minute_intervals,
                            start=start.date(),
                            end=end.date(),
                            market=GROWW_COVERAGE_MARKET,
                            window_days_for=groww_window_days_for,
                            symbol_windows=_floor_windows,
                        )
                        plan_windows(
                            ledger,
                            instruments=[instrument],
                            intervals=minute_intervals,
                            start=start.date(),
                            end=end.date(),
                            market=GROWW_COVERAGE_MARKET,
                            window_days_for=groww_window_days_for,
                            symbol_windows=_floor_windows,
                        )
                    # Learned-floor clamp (DEC-VENDOR-FLOOR-LAZY-PROBE-001): the SAME
                    # ``_floor_windows`` the plan above tiled (planned keys ==
                    # fetched keys); ``None`` (no learned floor) is byte-identical.
                    _run_phase(minute_intervals, _floor_windows)
            # Fetch phases done — arm the post-drain L2 lake heal in the finally.
            fetch_phases_ok = True
        finally:
            # Drain the FULL async fetch/process pipeline FIRST (DEC-GROWW-FETCH-ASYNC-SEPARATION-001
            # MAJOR-4 fix): a pipeline consumer's tail calls ``sink.append`` which may FEED the
            # batched write consumer, so the pipeline MUST be drained (every queued tail run)
            # BEFORE the write consumer is stopped — else a late write a still-running pipeline
            # consumer emits could be lost. Process every still-queued window's tail + record its
            # ledger row BEFORE the ledger persist (settle-after-durable at pull granularity). A
            # no-op when OFF; a captured disk-full halt PROPAGATES (graceful pull-halt), else
            # swallowed.
            _drain_fetch_pipeline(_fetch_pipeline)
            # Drain the coalescing batched write consumer AFTER the pipeline (DEC-WRITE-BATCH-
            # COALESCE-001): flush every still-queued window durably + join the writer BEFORE
            # saving the ledger, so every settled window is on disk (settle-after-durable). A
            # no-op when the batched path is OFF (the synchronous default). Best-effort.
            _drain_write_consumer(_write_consumer)
            # NOTE (DEC-PIPELINE-AUTOPREBUILD-001): the mid-pull 1-day derivation
            # that ran here is REMOVED — derived data is written ONLY by the
            # pipeline's final prebuild phase (brutex.dashboard.pipeline_prebuild).
            # DEC-LAKE-HEAL-001 L2: heal the just-pulled slice at the SAME
            # post-drain seam (both drains above landed the 1-minute durably),
            # so the pipeline's later prebuild phase reads clean 1-minute.
            # SUCCESS-path only (``fetch_phases_ok``); LakeWriteHalt re-raises
            # inside (graceful pull-halt), any other heal error is swallowed.
            if fetch_phases_ok and not dry_run:  # pragma: no cover — raw lake I/O glue
                _heal_after_pull(
                    instruments=[instrument],
                    lake_root=lake_root,
                    start=start.date(),
                    end=end.date(),
                    pull_id=pid,
                )
            end_pull_ledger(ledger, lake_root, dry_run=dry_run)
        _observe_rate(rate_observer, _rate_controller, adapter, _pull_health_monitor)
        # Fold the async pipeline's post-drain per-series totals back into the deferred per-pair
        # reports (DEC-GROWW-FETCH-ASYNC-SEPARATION-001 completion-count fix): in async mode the
        # producer returned ``bars_appended == 0`` per series (the consumer did the durable write
        # on a background thread), so the rollup below would falsely read 0 bars → ``completed ==
        # 0``. The pipeline is already DRAINED in the finally above. A no-op (synchronous path)
        # when ``_fetch_pipeline`` is ``None``.
        merged_pairs = _reconcile_pairs_with_pipeline(merged_pairs, _fetch_pipeline)

        # Build one result per REQUESTED interval (the injected *intervals*, ``1m`` only
        # — DEC-1MINUTE-ONLY-DERIVE-1DAY-001). The label map gives
        # the dashboard's ``1m``/``1d`` display label. Explicit ``dict[str, str]`` so
        # the keys are plain ``str`` (matching the ``str`` *intervals*).
        label_for: dict[str, str] = dict(_FETCH_INTERVAL_LABELS)
        timeframes: list[TimeframeResult] = []
        for fetch_interval in intervals:
            label = label_for.get(fetch_interval, fetch_interval)
            key = (symbol, fetch_interval)
            pair = merged_pairs.get(key)
            if key in failure_keys:
                # Terminal Groww error — quarantined, never in merged_pairs.
                timeframes.append(TimeframeResult(interval=label, completed=0, failed=1))
            elif pair is None:
                # The interval was never RUN for this symbol (a 1-minute phase skipped
                # by the global-stop guard — a dead Phase-1 token) — report it as
                # SKIPPED, NOT no_data: we deliberately refused to query the vendor,
                # which is honestly distinct from "vendor queried, returned empty" (the
                # no_data cell below). Mirrors pull_all_spot, which likewise never
                # labels a never-queried interval vendor-empty (Golden Rule #1 — no
                # illusion).
                timeframes.append(TimeframeResult(interval=label, completed=0, failed=0, skipped=1))
            elif pair.bars_appended > 0:
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
    "make_window_observer",
    "parse_symbol_list",
    "pull_all_spot",
    "pull_candles",
    "register_pull_meter",
    "resolve_candle_range",
    "resolve_fetch_intervals",
    "resolve_pull_symbols",
    "resolve_pull_targets",
]
