"""Dhan spot ingest — pull the index series' 1-minute OHLCV into the ``feeds/dhan/`` lake.

DEC-DHAN-SPOT-PULL-001 (ADR-0022). The Dhan spot spine: plan the fetch windows for
each index series, fetch each via the COMMON
:meth:`~brutex.adapters.protocol.MarketAdapter.fetch_ohlcv`, calendar-session-filter
the bars, and append to a :class:`~brutex.data.sink.BarSink`. Only **3 vendor
couplings** — :func:`~brutex.adapters.dhan.chunks.window_days_for_interval` (the flat
90-day tile), the :mod:`~brutex.adapters.dhan.errors` classifiers, and the
per-symbol outcome/attribution namespace ``_MARKET="dhan"`` (own namespace → no
Groww collision); everything else is vendor-neutral (the window planner, the
session filter, the sink).

**Honest degradation (Golden Rule #1, DEC-DHAN-HONEST-DEGRADE-001).** Two honest
states are structurally distinct:

* **empty-from-Dhan** — a holiday / no-session / too-old span returns empty bars: a
  recorded SUCCESS window with zero bars (clean-empty, never a failure).
* **UNVERIFIED-servability / unresolved securityId** — a symbol resolvable to NO
  securityId is loud-skipped BEFORE any fetch (``dhan.index_unverified`` WARN naming
  the index), its window left RE-PULLABLE (``skipped_unresolved``, zero bars), NEVER
  a fabricated bar and never a blind id.

**Honest envelope (Golden Rule #10).** This is the LEAN, single-threaded spot spine
(the "honest, simpler envelope" the Groww spine documents for its own auth latch): it
proves the Dhan feed end-to-end (resolve → plan → fetch → session-filter → sink →
bounded retry) with per-window outcomes. It uses **NO
:class:`~brutex.data.window_ledger.WindowLedger`** — the in-memory per-window
:class:`DhanWindowOutcome`\\ s ARE the record, and re-pull is a whole-range operator
re-run (bounded in-run retry re-processes a RETRYABLE-failed window only). The FULL
Groww-parity spine — a durable ``WindowLedger`` as the sole skip authority, the
parallel fan-out, and the ``run_until_clean`` integration — is the merge-integration
surface work (DEC-FEED-SPINE-GENERALIZE-001, option b: drive the binding fields from
this spot spine), deferred with the dashboard route / CLI ``--feed dhan``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Final, Protocol

import structlog

from brutex.adapters.dhan.chunks import window_days_for_interval
from brutex.adapters.dhan.constants import (
    DHAN_COVERAGE_MARKET,
    DHAN_FETCH_INTERVAL,
    DHAN_FLOOR_MARGIN_DAYS,
    SENSEX_SYMBOL,
    dhan_history_floor,
)
from brutex.adapters.dhan.errors import DhanError, is_retryable, is_terminal_pull_stop
from brutex.adapters.ingest_common import drop_bars_after, now_ist, plan_windows
from brutex.data.models import CandleBar
from brutex.data.session import filter_to_trading_session
from brutex.data.sink import BarSink
from brutex.models import Exchange

_LOG = structlog.get_logger("brutex.adapters.dhan.ingest")


class FetchOhlcv(Protocol):
    """The narrow fetch surface the spot spine needs (the COMMON ``fetch_ohlcv`` waist).

    The production :class:`~brutex.adapters.dhan.adapter.DhanMarketAdapter` satisfies
    it; typing the dependency at this narrow Protocol (not the concrete class) keeps
    the spine testable with a fetch-only fake (CLAUDE.md §11 COMMON).
    """

    def fetch_ohlcv(
        self,
        *,
        symbol: str,
        exchange: Exchange,
        interval: str,
        start: date,
        end: date,
    ) -> list[CandleBar]:
        """Fetch OHLCV bars for *symbol* over ``[start, end)`` at *interval*."""
        ...


#: The per-symbol outcome/attribution namespace for Dhan windows — used in the
#: structlog events and the all-empty scan, NOT a durable ledger namespace (this lean
#: spine uses no WindowLedger; the durable ledger is deferred to
#: DEC-FEED-SPINE-GENERALIZE-001). Own namespace so a Dhan outcome never collides with
#: a Groww window.
_MARKET: Final[str] = DHAN_COVERAGE_MARKET

#: The lake partition segment for a Dhan index series (the neutral INDEX segment; the
#: Dhan lake nests under ``feeds/dhan/`` so this convention is self-contained). Shared
#: by BOTH Dhan spot stages (this 1-minute spine + the native-daily
#: ``daily_ingest`` — CLAUDE.md §11 COMMON, never re-minted).
SPOT_SEGMENT: Final[str] = "INDEX"

#: Per-window outcome status vocabulary.
STATUS_SUCCESS: Final[str] = "success"
STATUS_FAILED: Final[str] = "failed"
STATUS_SKIPPED_UNRESOLVED: Final[str] = "skipped_unresolved"


def _noop_pace() -> None:
    """Default no-op pace hook (a real controller.pace is injected in production)."""


def _noop_backoff(_round_index: int) -> None:
    """Default no-op inter-round back-off (a test injects a recording one)."""


def dhan_index_exchange(symbol: str) -> Exchange:
    """Map a Dhan index symbol to its exchange (SENSEX → BSE, else NSE).

    Both ride the SHARED ``IDX_I`` segment on the wire, but the lake partition path
    keys on the exchange dir, so SENSEX (a BSE index) is attributed to
    :attr:`~brutex.models.Exchange.BSE` and the NSE indices to
    :attr:`~brutex.models.Exchange.NSE`.
    """
    return Exchange.BSE if symbol == SENSEX_SYMBOL else Exchange.NSE


@dataclass(frozen=True, slots=True)
class DhanWorkItem:
    """One (symbol, exchange, securityId, window) unit of Dhan spot ingest work."""

    symbol: str
    exchange: Exchange
    security_id: int
    start: date
    end: date


@dataclass(frozen=True, slots=True)
class DhanWindowOutcome:
    """The outcome of processing one :class:`DhanWorkItem` (or an unresolved symbol)."""

    symbol: str
    exchange: Exchange
    security_id: int | None
    start: date
    end: date
    status: str
    bars_appended: int
    retryable: bool = False
    error_type: str | None = None


@dataclass(frozen=True, slots=True)
class DhanIngestReport:
    """The result of a Dhan spot ingest run.

    Attributes
    ----------
    outcomes
        One :class:`DhanWindowOutcome` per processed window (latest per item) plus
        one ``skipped_unresolved`` outcome per unresolved symbol.
    resolved_symbols, unresolved_symbols
        The symbols that DID / did NOT resolve to a securityId.
    market
        The per-symbol outcome/attribution namespace (``"dhan"``); not a durable
        ledger namespace (this lean spine uses no WindowLedger).
    requested_start, effective_start, floor
        The rolling-floor stamps (DEC-DHAN-ROLLING-FLOOR-001): the operator's
        requested start, the CLAMPED effective start (``max(start, floor)``), and
        the floor itself — all computed from the SAME threaded ``today``.
        ``None`` on a report built without planning (a dry-run short-circuits
        BEFORE planning and echoes the REQUESTED range only).
    entirely_pre_floor
        ``True`` when the whole requested range ended before the floor — ZERO
        windows were planned (never doomed requests recorded as clean-empty
        SUCCESS).
    """

    outcomes: tuple[DhanWindowOutcome, ...]
    resolved_symbols: tuple[str, ...]
    unresolved_symbols: tuple[str, ...]
    market: str = _MARKET
    requested_start: date | None = None
    effective_start: date | None = None
    floor: date | None = None
    entirely_pre_floor: bool = False

    @property
    def total_bars(self) -> int:
        """Total bars appended across all SUCCESS windows."""
        return sum(o.bars_appended for o in self.outcomes)

    @property
    def failed_windows(self) -> int:
        """Number of windows that ended FAILED (re-pullable)."""
        return sum(1 for o in self.outcomes if o.status == STATUS_FAILED)


def clamp_dhan_window_end(end: date, *, today: date) -> date:
    """Clamp a HALF-OPEN window ``end`` so the last pulled day is the PREVIOUS day.

    DEC-DHAN-TODAY-CLAMP-001: BOTH Dhan window laws (the 1-minute
    :func:`plan_dhan_work` and the native-daily
    :func:`~brutex.adapters.dhan.daily_ingest.plan_dhan_daily_work`) consult this
    ONE helper. The half-open effective end is ``min(end, today)`` — i.e. the last
    INCLUSIVE day requested from the vendor is ``today − 1`` — the EXACT plain-
    calendar prev-day upper-bound law
    :func:`~brutex.dashboard.pull_controller.resolve_candle_range` already applies
    to a blank default (``today − timedelta(days=1)``; deliberately NOT a
    trading-day walk). A blank-default pull (whose surface already passes the
    half-open ``end == today``) is BYTE-IDENTICAL — no clamp, no note; an EXPLICIT
    ``to_date=today`` (half-open ``end == today + 1``) is CLAMPED (not refused)
    with ONE honest INFO note. *today* is the IST date resolved once per launch
    and threaded (never a mid-plan clock read).
    """
    if end <= today:
        return end
    _LOG.info(
        "dhan.window_end_clamped",
        requested_end=end.isoformat(),
        effective_end=today.isoformat(),
        detail=("clamped to previous day — today's bars settle after close; never wrong bytes"),
    )
    return today


def expand_dhan_symbol_windows(
    *,
    symbols: Sequence[str],
    security_ids: Mapping[str, int],
    windows: Sequence[object],
) -> tuple[list[DhanWorkItem], list[str]]:
    """Expand planned windows across symbols; split resolved work from unresolved symbols.

    The ONE symbol-expansion law both Dhan planners share (CLAUDE.md §11 COMMON —
    :func:`plan_dhan_work` and the native-daily
    :func:`~brutex.adapters.dhan.daily_ingest.plan_dhan_daily_work`). A symbol
    ABSENT from *security_ids* is unresolved (no work item planned — the caller
    loud-skips it). *windows* is the ``plan_windows`` output (``DateWindow``
    bounds are ISO ``YYYY-MM-DDTHH:MM:SS`` strings — the date part is taken).
    """
    work: list[DhanWorkItem] = []
    unresolved: list[str] = []
    for symbol in symbols:
        security_id = security_ids.get(symbol)
        if security_id is None:
            unresolved.append(symbol)
            continue
        exchange = dhan_index_exchange(symbol)
        for window in windows:
            work.append(
                DhanWorkItem(
                    symbol=symbol,
                    exchange=exchange,
                    security_id=security_id,
                    start=date.fromisoformat(window.start[:10]),  # type: ignore[attr-defined]
                    end=date.fromisoformat(window.end[:10]),  # type: ignore[attr-defined]
                )
            )
    return work, unresolved


def plan_dhan_work(
    *,
    symbols: Sequence[str],
    security_ids: Mapping[str, int],
    interval: str,
    start: date,
    end: date,
    today: date | None = None,
) -> tuple[list[DhanWorkItem], list[str]]:
    """Plan the ingest work: resolve each symbol + tile ``[start, end)`` into windows.

    Returns ``(work_items, unresolved_symbols)``. A symbol ABSENT from *security_ids*
    is unresolved (no work item planned — the caller loud-skips it). ``[start, end)``
    is half-open (the ``ingest_common`` tiling convention); an inclusive operator
    range passes ``end + 1 day`` at the surface.

    **The rolling-floor clamp (DEC-DHAN-ROLLING-FLOOR-001) lives HERE — the ONE
    plan chokepoint.** The effective start is ``max(start,
    dhan_history_floor(today))`` so no planned window ever starts (or straddles)
    before the floor — the first window STARTS at it; a range ending ON or before
    the floor plans ZERO windows with ONE ``dhan.pull_entirely_pre_floor`` WARNING
    (never doomed requests recorded as clean-empty SUCCESS). A genuine clamp emits
    ONE ``dhan.window_floor_clamped`` INFO (EMIT-ON-CLAMP — it fires on every
    blank-date pull by construction, since the default 2020-01-01 sits before the
    floor; that is honest, not noise). *today* is the IST date resolved ONCE per
    launch and threaded (``None`` resolves ``now_ist().date()`` here — a plan
    never re-reads the clock mid-run). The floor governs REQUESTS only: pre-floor
    bars already stored are never deleted (keep-first, append-only).

    **The prev-day END clamp (DEC-DHAN-TODAY-CLAMP-001) ALSO lives here:** the
    half-open end is clamped through :func:`clamp_dhan_window_end` (an explicit
    ``to_date=today`` pull ends at the previous day with ONE honest INFO note;
    the blank default is byte-identical).
    """
    today_resolved = today if today is not None else now_ist().date()
    floor = dhan_history_floor(today_resolved)
    effective_end = clamp_dhan_window_end(end, today=today_resolved)
    if effective_end <= floor:
        _LOG.warning(
            "dhan.pull_entirely_pre_floor",
            requested_start=start.isoformat(),
            requested_end=end.isoformat(),
            floor=floor.isoformat(),
            margin_days=DHAN_FLOOR_MARGIN_DAYS,
            detail="nothing to pull (requested range wholly before the Dhan rolling floor)",
        )
        windows: list[object] = []
    else:
        effective_start = max(start, floor)
        if effective_start > start:
            _LOG.info(
                "dhan.window_floor_clamped",
                requested_start=start.isoformat(),
                effective_start=effective_start.isoformat(),
                floor=floor.isoformat(),
                margin_days=DHAN_FLOOR_MARGIN_DAYS,
            )
        windows = list(
            plan_windows(
                datetime.combine(effective_start, time.min),
                datetime.combine(effective_end, time.min),
                window_days=window_days_for_interval(interval),
            )
        )
    return expand_dhan_symbol_windows(symbols=symbols, security_ids=security_ids, windows=windows)


def warn_unresolved(symbol: str) -> DhanWindowOutcome:
    """Loud-skip an unresolved symbol (zero bars, re-pullable) — never a fabricated bar.

    The ONE unresolved-symbol law BOTH Dhan spot stages share (CLAUDE.md §11
    COMMON — this 1-minute spine and the native-daily
    :func:`~brutex.adapters.dhan.daily_ingest.run_dhan_spot_daily`).
    """
    _LOG.warning(
        "dhan.index_unverified",
        symbol=symbol,
        reason="unresolved_security_id",
        remedy="resolve the scrip-master securityId (or seed the fallback map), then re-pull",
    )
    return DhanWindowOutcome(
        symbol=symbol,
        exchange=dhan_index_exchange(symbol),
        security_id=None,
        start=date.min,
        end=date.min,
        status=STATUS_SKIPPED_UNRESOLVED,
        bars_appended=0,
    )


def scan_resolved_all_empty(report: DhanIngestReport) -> tuple[str, ...]:
    """Return RESOLVED symbols whose windows all succeeded but summed ZERO bars.

    Mirrors Groww's ``scan_underlying_all_empty`` intent (DEC-DHAN-HONEST-DEGRADE-001):
    a resolved index that returned zero bars across the ENTIRE requested range —
    every window a clean SUCCESS, none FAILED — is suspicious (a wrong securityId or
    an UNVERIFIED servability), never a fabricated bar. Deliberately EXCLUDED: an
    unresolved symbol (already loud-skipped by :func:`_warn_unresolved`) and any
    symbol with a FAILED window (its per-window ``dhan.window_failed`` warnings are
    the story, not silent emptiness). A single clean-empty window is NOT flagged —
    only the whole-range zero.

    Parameters
    ----------
    report
        The :class:`DhanIngestReport` to scan.

    Returns
    -------
    tuple[str, ...]
        The resolved symbols that returned zero bars across every (SUCCESS) window.
    """
    by_symbol: dict[str, list[DhanWindowOutcome]] = {}
    for outcome in report.outcomes:
        if outcome.status == STATUS_SKIPPED_UNRESOLVED:
            continue
        by_symbol.setdefault(outcome.symbol, []).append(outcome)
    suspicious: list[str] = []
    for symbol in report.resolved_symbols:
        windows = by_symbol.get(symbol, [])
        if not windows:
            continue
        if any(o.status == STATUS_FAILED for o in windows):
            continue
        if sum(o.bars_appended for o in windows) == 0:
            suspicious.append(symbol)
    return tuple(suspicious)


def warn_resolved_all_empty(report: DhanIngestReport) -> None:
    """Emit exactly ONE WARN per RESOLVED symbol that returned zero bars all-range.

    Shared by BOTH Dhan spot stages (CLAUDE.md §11 COMMON — this 1-minute spine
    and the native-daily :func:`~brutex.adapters.dhan.daily_ingest.run_dhan_spot_daily`).
    """
    for symbol in scan_resolved_all_empty(report):
        _LOG.warning(
            "dhan.symbol_all_empty",
            symbol=symbol,
            reason="resolved_but_zero_bars_across_range",
            remedy=(
                "verify the securityId is correct + intraday servability is confirmed "
                "(run the read-only probe kit), then re-pull"
            ),
        )


def _process_item(
    item: DhanWorkItem,
    *,
    adapter: FetchOhlcv,
    sink: BarSink,
    interval: str,
    session_filter: Callable[..., object],
    pace: Callable[[], None],
) -> DhanWindowOutcome:
    """Fetch → session-filter → sink one window; classify a failure honestly."""
    pace()
    try:
        raw_bars = adapter.fetch_ohlcv(
            symbol=item.symbol,
            exchange=item.exchange,
            interval=interval,
            start=item.start,
            end=item.end,
        )
        kept = session_filter(raw_bars).kept  # type: ignore[attr-defined]
        # The toDate-inclusivity BELT (design §4, the COMMON drop_bars_after —
        # DEC-CANDLE-DROP-FUTURE-BAR-001): work items carry the HALF-OPEN ``end``,
        # so the last servable day is ``end − 1``. Dhan's documented daily
        # ``toDate`` is "non-inclusive" but the intraday boundary is UNVERIFIED
        # (probe gate #13) — under an INCLUSIVE vendor the FINAL window would
        # over-fetch one day PAST the operator range, and on a prev-day-capped
        # pull that day is TODAY'S forming session, which keep-first would freeze
        # (the #837/#1774 class). Dropped bars are counted + warned, never stored.
        bounded = drop_bars_after(kept, item.end - timedelta(days=1))
        dropped = len(kept) - len(bounded)
        if dropped > 0:
            _LOG.warning(
                "dhan.future_bar_dropped",
                symbol=item.symbol,
                window=f"{item.start.isoformat()}..{item.end.isoformat()}",
                dropped=dropped,
            )
        result = sink.append(
            exchange=item.exchange.value,
            segment=SPOT_SEGMENT,
            symbol=item.symbol,
            interval=interval,
            bars=bounded,
        )
    except DhanError as exc:
        # The pull-wide global-stop carve-out (the Groww spines' _GLOBAL_STOP_ERRORS
        # mirror): a terminal auth / still-stale-token / subscription failure means
        # EVERY later window of this pull would fail identically, so the FIRST
        # occurrence re-raises past the per-window quarantine — the remaining
        # windows are never burned, and the job layer's is_pull_halt fires the
        # CRITICAL Telegram pull-halt alert (design §3, DEC-DHAN-SPOT-FEED-001).
        if is_terminal_pull_stop(exc):
            raise
        retryable = is_retryable(exc)
        _LOG.warning(
            "dhan.window_failed",
            symbol=item.symbol,
            start=item.start.isoformat(),
            end=item.end.isoformat(),
            error_type=type(exc).__name__,
            retryable=retryable,
        )
        return DhanWindowOutcome(
            symbol=item.symbol,
            exchange=item.exchange,
            security_id=item.security_id,
            start=item.start,
            end=item.end,
            status=STATUS_FAILED,
            bars_appended=0,
            retryable=retryable,
            error_type=type(exc).__name__,
        )
    return DhanWindowOutcome(
        symbol=item.symbol,
        exchange=item.exchange,
        security_id=item.security_id,
        start=item.start,
        end=item.end,
        status=STATUS_SUCCESS,
        bars_appended=result.bars_net_new,
    )


def process_work_with_retry(
    work: Sequence[DhanWorkItem],
    *,
    process: Callable[[DhanWorkItem], DhanWindowOutcome],
    max_rounds: int = 3,
    backoff: Callable[[int], None] = _noop_backoff,
) -> list[DhanWindowOutcome]:
    """Process *work* items with bounded in-run retry of retryable-failed windows.

    The ONE bounded-retry round engine BOTH Dhan spot stages share (CLAUDE.md
    §11 COMMON — the 1-minute :func:`run_dhan_spot_ingest_with_retry` and the
    native-daily :func:`~brutex.adapters.dhan.daily_ingest.run_dhan_spot_daily`):
    each round applies *process* to every pending item, keeps the LATEST outcome
    per item, and re-queues only a RETRYABLE-failed item for a later round (up
    to ``max(1, max_rounds)`` rounds; *backoff* fires between rounds that still
    have pending items). Returns the latest per-item outcomes in first-seen
    order. A pull-wide stop raised by *process* (the
    :func:`~brutex.adapters.dhan.errors.is_terminal_pull_stop` carve-out lives
    INSIDE the per-item processors) propagates — the engine never swallows it.
    """
    latest: dict[DhanWorkItem, DhanWindowOutcome] = {}
    pending = list(work)
    rounds = max(1, max_rounds)
    for round_index in range(rounds):
        if not pending:
            break
        next_pending: list[DhanWorkItem] = []
        for item in pending:
            outcome = process(item)
            latest[item] = outcome
            if outcome.status == STATUS_FAILED and outcome.retryable:
                next_pending.append(item)
        pending = next_pending
        if pending and round_index < rounds - 1:
            backoff(round_index)
    return list(latest.values())


def run_dhan_spot_ingest_with_retry(
    *,
    symbols: Sequence[str],
    adapter: FetchOhlcv,
    sink: BarSink,
    security_ids: Mapping[str, int],
    start: date,
    end: date,
    interval: str = DHAN_FETCH_INTERVAL,
    session_filter: Callable[..., object] = filter_to_trading_session,
    pace: Callable[[], None] = _noop_pace,
    max_rounds: int = 3,
    backoff: Callable[[int], None] = _noop_backoff,
    today: date | None = None,
) -> DhanIngestReport:
    """Run the Dhan spot ingest with bounded in-run retry of retryable-failed windows.

    Resolves + plans the work (:func:`plan_dhan_work`), loud-skips unresolved symbols,
    then processes each window; a RETRYABLE failure (throttle / empty body / timeout /
    server blip) is re-processed in a later round up to *max_rounds* (a terminal
    failure is left FAILED — re-pullable by a future pull). A
    :func:`~brutex.adapters.dhan.errors.is_terminal_pull_stop` failure (terminal
    auth / still-stale token / inactive subscription) PROPAGATES on its first
    occurrence — the whole pull stops instead of burning every remaining window
    (the job layer alerts it CRITICAL). Returns the aggregated
    :class:`DhanIngestReport`.

    Parameters
    ----------
    symbols
        The index symbols to pull (e.g. the Dhan spot index set).
    adapter, sink, security_ids
        The injected fetch adapter, write sink, and resolved ``{symbol → securityId}``.
    start, end
        The half-open ``[start, end)`` operator range (``end`` exclusive).
    interval
        The fetch interval (``"1minute"`` — the only Dhan spot interval).
    session_filter, pace, backoff
        Injected seams (default the calendar-aware filter / no-op pace / no-op
        back-off); a production caller injects the controller's ``pace`` + a real
        back-off.
    max_rounds
        The bounded retry-round budget (``>= 1``).
    today
        The IST date the rolling floor keys on (DEC-DHAN-ROLLING-FLOOR-001) —
        resolved ONCE per launch at the surface and threaded; ``None`` resolves
        ``now_ist().date()`` HERE (once per run, never re-read mid-plan).
    """
    today_resolved = today if today is not None else now_ist().date()
    floor = dhan_history_floor(today_resolved)
    effective_start = max(start, floor) if end > floor else None
    work, unresolved = plan_dhan_work(
        symbols=symbols,
        security_ids=security_ids,
        interval=interval,
        start=start,
        end=end,
        today=today_resolved,
    )
    resolved = tuple(dict.fromkeys(item.symbol for item in work))
    outcomes: list[DhanWindowOutcome] = [warn_unresolved(symbol) for symbol in unresolved]

    def _process(item: DhanWorkItem) -> DhanWindowOutcome:
        return _process_item(
            item,
            adapter=adapter,
            sink=sink,
            interval=interval,
            session_filter=session_filter,
            pace=pace,
        )

    outcomes.extend(
        process_work_with_retry(work, process=_process, max_rounds=max_rounds, backoff=backoff)
    )
    report = DhanIngestReport(
        outcomes=tuple(outcomes),
        resolved_symbols=resolved,
        unresolved_symbols=tuple(unresolved),
        requested_start=start,
        effective_start=effective_start,
        floor=floor,
        entirely_pre_floor=end <= floor,
    )
    warn_resolved_all_empty(report)
    return report


__all__ = [
    "SPOT_SEGMENT",
    "STATUS_FAILED",
    "STATUS_SKIPPED_UNRESOLVED",
    "STATUS_SUCCESS",
    "DhanIngestReport",
    "DhanWindowOutcome",
    "DhanWorkItem",
    "clamp_dhan_window_end",
    "dhan_index_exchange",
    "expand_dhan_symbol_windows",
    "plan_dhan_work",
    "process_work_with_retry",
    "run_dhan_spot_ingest_with_retry",
    "scan_resolved_all_empty",
    "warn_resolved_all_empty",
    "warn_unresolved",
]
