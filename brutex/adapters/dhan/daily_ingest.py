"""Dhan NATIVE-daily spot ingest — pull the index series' 1-DAY bars from the daily endpoint.

DEC-DHAN-NATIVE-DAILY-001 (implementing `DEC-SCOPE-MINIMAL-CORE-001`). Dhan's 1-day
spot data is pulled NATIVELY from the vendor's daily endpoint (``POST
/v2/charts/historical`` via the SDK's ``historical_daily_data`` —
:meth:`~brutex.adapters.dhan.client.DhanClient.daily_candles`) as its OWN stage
that runs FIRST in the Dhan pull chain (the two-callable design:
:func:`run_dhan_spot_daily` → the 1-minute
:func:`~brutex.adapters.dhan.ingest.run_dhan_spot_ingest_with_retry`). The stored
``1day`` partition is therefore VENDOR truth, never derived from 1-minute and never
overwritten by the Dhan prebuild (which derives INTRADAY TFs only —
``include_daily=False`` structurally at the Dhan call site). The Groww law
`DEC-1MINUTE-ONLY-DERIVE-1DAY-001` stays Groww-only — this module is the Dhan
feed's deliberate, ledgered divergence from it.

**Two floors, two laws (never conflated):** the daily plan clamps to the FIXED
project floor :data:`~brutex.adapters.dhan.constants.DHAN_DAILY_HISTORY_FLOOR`
(2020-01-01 — the daily endpoint's doc-stated depth is "instrument inception", so
the floor is a PROJECT choice, CLAUDE.md §2 Rule 3's ``GROWW_HISTORY_FLOOR``
precedent), NOT the intraday ROLLING :func:`~brutex.adapters.dhan.constants.\
dhan_history_floor` (DEC-DHAN-ROLLING-FLOOR-001 — 1-minute only). Both plans share
the prev-day END clamp (`DEC-DHAN-TODAY-CLAMP-001` —
:func:`~brutex.adapters.dhan.ingest.clamp_dhan_window_end`).

**Session filtering is by TRADING DATE, not wall clock.** Native daily bars are
normalized to midnight-IST stamps (:func:`~brutex.adapters.dhan.parser.\
parse_daily_candle_columns` — the ``spot_daily_resample`` convention), which sit
OUTSIDE every intraday session window, so the intraday
:func:`~brutex.data.session.filter_to_trading_session` would drop them all. The
daily filter (:func:`filter_daily_to_trading_dates`) keeps a bar iff its IST DATE
is a real NSE trading date (weekend / holiday vendor leaks dropped + COUNTED);
out-of-coverage dates ABSTAIN and are KEPT (Golden Rule #1 — the calendar cannot
judge them, and dropping real vendor data on ignorance would fabricate a hole).

Everything else is the SAME lean spine machinery the 1-minute stage uses
(CLAUDE.md §11 COMMON — imported from :mod:`~brutex.adapters.dhan.ingest`, never
copied): the symbol×window expansion, the unresolved loud-skip, the bounded retry
round engine, the :func:`~brutex.adapters.dhan.errors.is_terminal_pull_stop`
pull-wide carve-out, the keep-first sink append, and the
:class:`~brutex.adapters.dhan.ingest.DhanIngestReport` shape.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, time, timedelta
from typing import Protocol

import structlog

from brutex.adapters.dhan.chunks import window_days_for_interval
from brutex.adapters.dhan.constants import (
    DHAN_DAILY_HISTORY_FLOOR,
    DHAN_DAILY_INTERVAL,
)
from brutex.adapters.dhan.errors import DhanError, is_retryable, is_terminal_pull_stop
from brutex.adapters.dhan.ingest import (
    SPOT_SEGMENT,
    STATUS_FAILED,
    STATUS_SUCCESS,
    DhanIngestReport,
    DhanWindowOutcome,
    DhanWorkItem,
    clamp_dhan_window_end,
    expand_dhan_symbol_windows,
    process_work_with_retry,
    warn_resolved_all_empty,
    warn_unresolved,
)
from brutex.adapters.dhan.parser import parse_daily_candle_columns
from brutex.adapters.ingest_common import drop_bars_after, now_ist, plan_windows
from brutex.calendars.nse import get_session, is_holiday_data_available
from brutex.data.models import CandleBar
from brutex.data.session import IST, SessionFilterResult
from brutex.data.sink import BarSink

_LOG = structlog.get_logger("brutex.adapters.dhan.daily_ingest")


def _noop_pace() -> None:
    """Default no-op pace hook (a real controller.pace is injected in production)."""


def _noop_backoff(_round_index: int) -> None:
    """Default no-op inter-round back-off (a test injects a recording one)."""


class DailyCandles(Protocol):
    """The narrow daily-fetch surface this stage needs (the client's daily method).

    :class:`~brutex.adapters.dhan.client.DhanClient` satisfies it structurally
    (its extra ``exchange_segment``/``instrument`` keyword parameters carry the
    index defaults); typing the dependency at this narrow Protocol keeps the
    stage testable with a fetch-only fake (CLAUDE.md §11 COMMON).
    """

    def daily_candles(
        self,
        *,
        security_id: int,
        from_date: str,
        to_date: str,
    ) -> Mapping[str, object]:
        """Fetch the raw daily candle payload for ``[from_date, to_date]``."""
        ...


def filter_daily_to_trading_dates(bars: Sequence[CandleBar]) -> SessionFilterResult:
    """Keep only the daily *bars* whose IST DATE is a real NSE trading date.

    The DAILY twin of the intraday calendar-aware session filter
    (:func:`~brutex.data.session.filter_to_trading_session`) — a midnight-IST
    daily bar carries no meaningful intraday time (it sits OUTSIDE every session
    window), so the judgment is DATE-level only: a bar on a weekend / holiday
    that leaked past the vendor is dropped + counted
    (``dropped_non_trading_date``); a bar on ANY trading date (regular, Budget
    special-weekend, Muhurat) is kept. **Out-of-coverage abstention (Golden
    Rule #1):** for a date outside the published holiday-list span the calendar
    cannot distinguish a session from a holiday, so the bar is KEPT (dropping
    real vendor data on ignorance would fabricate a lake hole — the honest
    limit until the calendar is extended). ``dropped_off_session`` is always 0
    on this path (no wall-clock judgment). O(bars + unique_dates) — each unique
    IST date's session resolves ONCE via a dict cache.
    """
    trading_by_date: dict[date, bool] = {}
    kept: list[CandleBar] = []
    non_trading = 0
    for bar in bars:
        ist_date = bar.timestamp.astimezone(IST).date()
        if not is_holiday_data_available(ist_date):
            kept.append(bar)  # ABSTAIN out of coverage — never drop on ignorance
            continue
        if ist_date not in trading_by_date:
            trading_by_date[ist_date] = get_session(ist_date) is not None
        if trading_by_date[ist_date]:
            kept.append(bar)
        else:
            non_trading += 1
    return SessionFilterResult(kept=kept, dropped_non_trading_date=non_trading)


def plan_dhan_daily_work(
    *,
    symbols: Sequence[str],
    security_ids: Mapping[str, int],
    start: date,
    end: date,
    today: date | None = None,
) -> tuple[list[DhanWorkItem], list[str]]:
    """Plan the NATIVE-daily work: resolve symbols + tile ``[start, end)`` into 365-day windows.

    Returns ``(work_items, unresolved_symbols)`` exactly like the 1-minute
    :func:`~brutex.adapters.dhan.ingest.plan_dhan_work` — the symbol×window
    expansion is the SAME shared law
    (:func:`~brutex.adapters.dhan.ingest.expand_dhan_symbol_windows`).

    **The FIXED daily floor lives HERE — the ONE daily plan chokepoint.** The
    effective start is ``max(start, DHAN_DAILY_HISTORY_FLOOR)`` (2020-01-01, a
    PROJECT floor — the vendor's daily depth is doc-stated "instrument
    inception", so no vendor cap forces this; the ROLLING intraday floor of
    DEC-DHAN-ROLLING-FLOOR-001 deliberately does NOT apply). A range ending ON
    or before the floor plans ZERO windows with ONE
    ``dhan.daily_pull_entirely_pre_floor`` WARNING; a genuine clamp emits ONE
    ``dhan.daily_window_floor_clamped`` INFO.

    **The prev-day END clamp (`DEC-DHAN-TODAY-CLAMP-001`) ALSO lives here** via
    the SAME shared :func:`~brutex.adapters.dhan.ingest.clamp_dhan_window_end`
    the 1-minute plan uses (an explicit ``to_date=today`` pull ends at the
    previous day with ONE honest INFO note; the blank default is
    byte-identical). *today* is the IST date resolved once per launch and
    threaded (``None`` resolves ``now_ist().date()`` here).

    Windows tile at the DEFENSIVE 365-day width
    (:func:`~brutex.adapters.dhan.chunks.window_days_for_interval` — the daily
    endpoint's per-request range cap is UNDOCUMENTED/UNVERIFIED; see the
    constant's docstring).
    """
    today_resolved = today if today is not None else now_ist().date()
    floor = DHAN_DAILY_HISTORY_FLOOR
    effective_end = clamp_dhan_window_end(end, today=today_resolved)
    if effective_end <= floor:
        _LOG.warning(
            "dhan.daily_pull_entirely_pre_floor",
            requested_start=start.isoformat(),
            requested_end=end.isoformat(),
            floor=floor.isoformat(),
            detail=("nothing to pull (requested range wholly before the Dhan daily PROJECT floor)"),
        )
        windows: list[object] = []
    else:
        effective_start = max(start, floor)
        if effective_start > start:
            _LOG.info(
                "dhan.daily_window_floor_clamped",
                requested_start=start.isoformat(),
                effective_start=effective_start.isoformat(),
                floor=floor.isoformat(),
            )
        windows = list(
            plan_windows(
                datetime.combine(effective_start, time.min),
                datetime.combine(effective_end, time.min),
                window_days=window_days_for_interval(DHAN_DAILY_INTERVAL),
            )
        )
    return expand_dhan_symbol_windows(symbols=symbols, security_ids=security_ids, windows=windows)


def _process_daily_item(
    item: DhanWorkItem,
    *,
    client: DailyCandles,
    sink: BarSink,
    pace: Callable[[], None],
) -> DhanWindowOutcome:
    """Fetch → parse → trading-date-filter → sink one daily window; classify failures honestly."""
    pace()
    try:
        payload = client.daily_candles(
            security_id=item.security_id,
            from_date=item.start.isoformat(),
            to_date=item.end.isoformat(),
        )
        parsed = parse_daily_candle_columns(payload)
        filtered = filter_daily_to_trading_dates(parsed)
        if filtered.dropped_non_trading_date > 0:
            _LOG.warning(
                "dhan.daily_non_trading_date_dropped",
                symbol=item.symbol,
                window=f"{item.start.isoformat()}..{item.end.isoformat()}",
                dropped=filtered.dropped_non_trading_date,
            )
        # The toDate-inclusivity BELT (the COMMON drop_bars_after —
        # DEC-CANDLE-DROP-FUTURE-BAR-001): work items carry the HALF-OPEN
        # ``end``, so the last servable day is ``end − 1``. The daily docs say
        # ``toDate`` is "non-inclusive" but the belt covers BOTH readings — on
        # a prev-day-capped pull an over-fetched day would be TODAY'S forming
        # daily bar, which keep-first would freeze (the #837/#1774 class).
        bounded = drop_bars_after(filtered.kept, item.end - timedelta(days=1))
        dropped = len(filtered.kept) - len(bounded)
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
            interval=DHAN_DAILY_INTERVAL,
            bars=bounded,
        )
    except DhanError as exc:
        # The pull-wide global-stop carve-out (the 1-minute spine's mirror): a
        # terminal auth / still-stale-token / subscription failure means EVERY
        # later window of this pull would fail identically, so the FIRST
        # occurrence re-raises past the per-window quarantine.
        if is_terminal_pull_stop(exc):
            raise
        retryable = is_retryable(exc)
        _LOG.warning(
            "dhan.daily_window_failed",
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


def run_dhan_spot_daily(
    *,
    symbols: Sequence[str],
    client: DailyCandles,
    sink: BarSink,
    security_ids: Mapping[str, int],
    start: date,
    end: date,
    pace: Callable[[], None] = _noop_pace,
    max_rounds: int = 3,
    backoff: Callable[[int], None] = _noop_backoff,
    today: date | None = None,
) -> DhanIngestReport:
    """Run the Dhan NATIVE-daily spot stage with bounded in-run retry.

    The FIRST stage of the Dhan pull chain (DEC-DHAN-NATIVE-DAILY-001 — the
    two-callable design: this daily stage, then the 1-minute
    :func:`~brutex.adapters.dhan.ingest.run_dhan_spot_ingest_with_retry`; the
    chain gate on a FAILED daily stage lives at the orchestration surfaces).
    Plans the work (:func:`plan_dhan_daily_work` — the FIXED
    ``DHAN_DAILY_HISTORY_FLOOR`` + the `DEC-DHAN-TODAY-CLAMP-001` prev-day end
    clamp), loud-skips unresolved symbols, then processes each 365-day window:
    fetch via the client's daily method → parse to midnight-IST-stamped bars
    (:func:`~brutex.adapters.dhan.parser.parse_daily_candle_columns` — the
    ``spot_daily_resample`` timestamp convention, byte-compatible with the
    derived-1day readers) → drop + count non-trading-date leaks
    (:func:`filter_daily_to_trading_dates`) → append ``interval="1day"``
    through the SAME keep-first feed sink the 1-minute stage writes. A
    RETRYABLE failure re-processes in a later round (up to *max_rounds*); a
    :func:`~brutex.adapters.dhan.errors.is_terminal_pull_stop` failure
    PROPAGATES on first occurrence (the pull-wide stop). Returns the SAME
    :class:`~brutex.adapters.dhan.ingest.DhanIngestReport` shape (its
    ``floor``/``effective_start`` stamps carry the FIXED daily floor —
    ``entirely_pre_floor`` reads True iff the CLAMPED end sits on/before it).

    Parameters mirror the 1-minute spine; *client* is the narrow
    :class:`DailyCandles` surface (the production
    :class:`~brutex.adapters.dhan.client.DhanClient` satisfies it — its
    ``_call`` funnel owns retry/reauth/classification below this layer, so a
    raised :class:`~brutex.adapters.dhan.errors.DhanError` here is already
    post-funnel). *today* is the IST date resolved once per launch and
    threaded; ``None`` resolves ``now_ist().date()`` HERE (once per run).
    """
    today_resolved = today if today is not None else now_ist().date()
    floor = DHAN_DAILY_HISTORY_FLOOR
    # The report's effective-end view of the clamp — the plan below re-derives it
    # through the ONE clamp helper (which owns the honest INFO note; this local
    # min() emits nothing, so the note still fires exactly once per plan).
    effective_end = min(end, today_resolved)
    work, unresolved = plan_dhan_daily_work(
        symbols=symbols,
        security_ids=security_ids,
        start=start,
        end=end,
        today=today_resolved,
    )
    resolved = tuple(dict.fromkeys(item.symbol for item in work))
    outcomes: list[DhanWindowOutcome] = [warn_unresolved(symbol) for symbol in unresolved]

    def _process(item: DhanWorkItem) -> DhanWindowOutcome:
        return _process_daily_item(item, client=client, sink=sink, pace=pace)

    outcomes.extend(
        process_work_with_retry(work, process=_process, max_rounds=max_rounds, backoff=backoff)
    )
    report = DhanIngestReport(
        outcomes=tuple(outcomes),
        resolved_symbols=resolved,
        unresolved_symbols=tuple(unresolved),
        requested_start=start,
        effective_start=max(start, floor) if effective_end > floor else None,
        floor=floor,
        entirely_pre_floor=effective_end <= floor,
    )
    warn_resolved_all_empty(report)
    return report


__all__ = [
    "DailyCandles",
    "filter_daily_to_trading_dates",
    "plan_dhan_daily_work",
    "run_dhan_spot_daily",
]
