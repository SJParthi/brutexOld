"""NSE/BSE trading-session filter — keep only in-session intraday bars.

Track-1 rule (operator 2026-05-31; CLAUDE.md §7): the lake must hold **only**
real-session data — **never** pre-open (09:00–09:15) or post-close bars.
The regular session is **09:15:00 IST inclusive → 15:30:00 IST exclusive**.

Two filters live here:

* :func:`filter_intraday_session` — the LEGACY **date-blind** fixed-window
  filter (``[09:15, 15:30)`` IST regardless of date). Kept as the golden
  oracle for regular-day parity and for date-blind consumers (the doctor
  live-proof probe reads a known regular trading day).
* :func:`filter_to_trading_session` — the **calendar-aware** boundary filter
  (DEC-INGEST-BOUNDARY-INTEGRITY-001) BOTH ingest spines now use: each bar is
  judged against its OWN IST date's real NSE session
  (:func:`~brutex.calendars.nse.get_session` — muhurat / special-weekend /
  regular / no-session), so a bar on a non-trading date is dropped even when
  its wall clock looks in-session, and an evening muhurat bar is KEPT (the
  date-blind filter wrongly dropped it).

* **Intraday** bars (1m and anything derived from it) are filtered — a bar's
  UTC timestamp is converted to IST and kept iff its time-of-day is inside its
  date's session window ``[open, close)``. Every NSE session (regular,
  muhurat evening 18:15–19:15, special weekend) maps to the **same UTC
  calendar date** (IST = UTC+05:30, sessions end well before 18:30 UTC), so
  there is no day-boundary rollover to worry about.
* **Daily** (1-day) bars carry no meaningful intraday time, so they are
  **not** time-filtered (both spines gate on the daily interval — a
  deliberate, documented envelope).

Applied at the **pull/ingest boundary** (before the lake write) so bad
bars never enter the lake — and reused by every write path (CLAUDE.md §11
COMMON). The resamplers derive every timeframe from the stored (already
boundary-filtered) 1-minute, so this single choke point covers all 21 TFs.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Final
from zoneinfo import ZoneInfo

from brutex.calendars.nse import TradingSession, get_session, is_holiday_data_available
from brutex.data.models import CandleBar

#: India Standard Time (NSE/BSE wall clock).
IST: Final[ZoneInfo] = ZoneInfo("Asia/Kolkata")


def format_ist_clock(value: str | datetime | None) -> str | None:
    """Render a stored UTC timestamp as a clean IST wall-clock string — the COMMON display rule.

    The lake stores every timestamp in UTC (reproducibility, CLAUDE.md §6); this is
    the single display-boundary conversion to the Indian-market wall clock (§11
    COMMON), so the window-ledger / token-events / coverage surfaces all read like
    the candle viewer. A value like ``2026-06-09T08:35:53.763513+00:00`` (or the
    same as a :class:`~datetime.datetime`) renders ``2026-06-09 14:05:53`` (IST),
    dropping the ``+05:30`` offset. ``None`` passes through; an unparseable string
    is returned unchanged (a display helper must NEVER raise + break a page). A
    naive datetime / string (no offset) is assumed already-IST and just formatted.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return value  # not a timestamp (e.g. a free-text note) — leave it be
    ist = dt.astimezone(IST) if dt.tzinfo is not None else dt
    return ist.strftime("%Y-%m-%d %H:%M:%S")


#: Regular session open — 09:15:00 IST (inclusive).
SESSION_OPEN: Final[time] = time(9, 15)

#: Regular session close — 15:30:00 IST (exclusive).
SESSION_CLOSE: Final[time] = time(15, 30)

#: Trading symbols EXEMPT from the 09:15–15:30 IST session filter. **GIFT Nifty**
#: trades on NSE IX (GIFT City) for ~21 hours, far outside the NSE regular
#: session, so applying the NSE window would drop almost all of its bars. Its
#: full-day intraday series is kept verbatim (DEC-DHAN-GIFTNIFTY-SESSION-001).
SESSION_EXEMPT_SYMBOLS: Final[frozenset[str]] = frozenset({"GIFTNIFTY"})


def is_session_exempt(trading_symbol: str | None) -> bool:
    """True iff *trading_symbol* is exempt from the NSE session filter (GIFT Nifty)."""
    return (trading_symbol or "").strip().upper() in SESSION_EXEMPT_SYMBOLS


def in_session(timestamp: datetime) -> bool:
    """True iff *timestamp* (UTC, tz-aware) falls in ``[09:15, 15:30)`` IST."""
    ist_time = timestamp.astimezone(IST).time()
    return SESSION_OPEN <= ist_time < SESSION_CLOSE


def filter_intraday_session(bars: Sequence[CandleBar]) -> list[CandleBar]:
    """Return only the *bars* whose IST time-of-day is in ``[09:15, 15:30)``.

    For **intraday** series only — drops pre-open / post-close bars. Daily
    series must NOT be passed here (a daily bar has no intraday time).

    **DATE-BLIND** (legacy): judges only the wall-clock time, never the date's
    real session. The ingest boundary uses the calendar-aware
    :func:`filter_to_trading_session` instead (DEC-INGEST-BOUNDARY-INTEGRITY-001);
    this stays as the golden regular-day oracle + for date-blind consumers
    (the doctor live-proof probe reads a known regular trading day).
    """
    return [bar for bar in bars if in_session(bar.timestamp)]


#: The ``degraded_reason`` tag stamped on a window whose vendor bars were ALL
#: dropped by the calendar-aware session filter (raw bars > 0, surviving == 0)
#: — DEC-INGEST-BOUNDARY-INTEGRITY-001. Lives beside its detector (this module),
#: mirroring :data:`~brutex.data.interior_gap_detect.INTERIOR_GAP` /
#: :data:`~brutex.data.partial_window_detect.SUSPECTED_PARTIAL`.
ALL_FILTERED: Final[str] = "ALL_FILTERED"

#: The ONE structlog WARNING both ingest spines emit when a window's EVERY vendor
#: bar was removed at the ingest filter boundary — the calendar-aware session
#: filter (:func:`filter_to_trading_session`) **and/or** the future-bar drop
#: (:func:`~brutex.adapters.ingest_common.drop_bars_after`,
#: DEC-CANDLE-DROP-FUTURE-BAR-001). One shared event name so the operator greps
#: a single string across spot + F&O (DEC-DATA-HONESTY-ALLFILTERED-SEED-001;
#: CLAUDE.md §11 COMMON). ALWAYS emitted on the shape — visibility is never
#: gated; only the ledger demotion is (mirrors the #1031 observe-vs-act split).
ALL_FILTERED_WINDOW_EVENT: Final[str] = "ingest.window.all_bars_filtered"


def is_all_filtered(raw_count: int, kept_count: int) -> bool:
    """True iff the vendor returned bars but the filter boundary kept NONE.

    The WHOLE-boundary discriminator (DEC-DATA-HONESTY-ALLFILTERED-SEED-001,
    widening the session-filter-only :attr:`SessionFilterResult.all_filtered` of
    DEC-INGEST-BOUNDARY-INTEGRITY-001): *kept_count* is the bar count AFTER the
    session filter **and** the future-bar drop, so a window emptied by EITHER
    stage is caught. Pure arithmetic — ``raw_count == 0`` is the genuine-EMPTY
    channel (untouched), and any kept bar means the window has real storable
    data. Never keyed on the sink's dedup count: a full-dedup re-pull keeps its
    bars through the boundary and stays SUCCESS.
    """
    return raw_count > 0 and kept_count == 0


def observe_all_filtered(
    log: object,
    *,
    raw_count: int,
    kept_count: int,
    pull_id: str | None,
    fields: Mapping[str, object],
) -> bool:
    """WARN (ALWAYS — visibility never gated) iff ALL bars were boundary-filtered; return the verdict.

    The ONE shared wiring for :data:`ALL_FILTERED_WINDOW_EVENT` (CLAUDE.md §11
    COMMON — both ingest spines call this, no copy). *fields* is the caller's
    per-window structlog field dict (window bounds + series coordinates) so the
    event carries the same identity every other window event does. Pure
    arithmetic + one log call — nothing here can raise, so no guarded-error twin
    event is needed (contrast the calendar-walking detectors). *log* is the
    caller's structlog logger (duck-typed for ``.warning(event, **fields)``,
    mirroring :func:`~brutex.data.interior_gap_detect.observe_interior_gap`).
    """
    verdict = is_all_filtered(raw_count, kept_count)
    if verdict:
        log.warning(  # type: ignore[attr-defined]  # duck-typed structlog logger
            ALL_FILTERED_WINDOW_EVENT,
            pull_id=pull_id,
            raw_bars=raw_count,
            kept_bars=kept_count,
            **fields,
        )
    return verdict


@dataclass(frozen=True, slots=True)
class SessionFilterResult:
    """Outcome of one :func:`filter_to_trading_session` pass — kept bars + drop counts.

    Immutable. ``kept`` preserves input order; the two drop counters split the
    exclusions by cause so the ingest tail's one-per-window WARNING can say
    *why* bars were excluded (off-session wall clock vs a non-trading date).
    """

    kept: list[CandleBar]
    dropped_off_session: int = 0
    dropped_non_trading_date: int = 0

    @property
    def dropped(self) -> int:
        """Total bars excluded (off-session + non-trading-date) — O(1)."""
        return self.dropped_off_session + self.dropped_non_trading_date

    @property
    def all_filtered(self) -> bool:
        """True iff at least one bar came in and NONE survived — the GAP-(c) discriminator."""
        return not self.kept and self.dropped > 0


def filter_to_trading_session(bars: Sequence[CandleBar]) -> SessionFilterResult:
    """Filter *bars* against each bar's OWN IST date's real NSE trading session.

    The calendar-aware ingest-boundary filter (DEC-INGEST-BOUNDARY-INTEGRITY-001),
    superseding the date-blind :func:`filter_intraday_session` at BOTH ingest
    spines. Per bar: derive the IST wall clock (``timestamp.astimezone(IST)``),
    look up that IST date's session via :func:`~brutex.calendars.nse.get_session`
    (muhurat → special-weekend → regular weekday → ``None``), and keep the bar
    iff its time-of-day is inside ``[open, close)``. Consequences vs the legacy
    filter:

    * a bar on a **non-trading date** (weekend / holiday that leaked past the
      vendor) is DROPPED even when its wall clock looks in-session — counted in
      ``dropped_non_trading_date``;
    * an **evening muhurat** bar (e.g. 18:15–19:15 IST on Diwali) is now KEPT
      (the fixed 09:15–15:30 window wrongly dropped it — a sanctioned behavior
      change), while a regular-hours bar ON a muhurat date is dropped (the
      exchange is closed outside the muhurat hour);
    * a **special-weekend session** bar (Budget Saturday) is KEPT.

    **Out-of-coverage abstention (Golden Rule #1):** for a date outside the
    published holiday-list span (:func:`~brutex.calendars.nse.is_holiday_data_available`,
    currently 2020–2026) the calendar cannot distinguish a session from a
    holiday, so the filter ABSTAINS from the calendar and falls back to the
    legacy fixed ``[09:15, 15:30)`` window — byte-identical to the pre-change
    behaviour for those dates, never dropping MORE than the legacy filter did
    (an unknown future muhurat evening would still be dropped there — the
    honest limit until the calendar is extended).

    For **intraday** series only (daily bars carry no intraday time; both
    spines gate on the daily interval). Complexity: O(bars + unique_dates) —
    each unique IST date's session is resolved ONCE via a dict cache, so the
    per-bar cost is O(1) amortized (NOT an O(1)-total claim).

    Returns
    -------
    SessionFilterResult
        The kept bars (input order) plus the per-cause drop counts.
    """
    session_by_date: dict[date, TradingSession | None] = {}
    kept: list[CandleBar] = []
    off_session = 0
    non_trading = 0
    for bar in bars:
        ist = bar.timestamp.astimezone(IST)
        ist_date = ist.date()
        if not is_holiday_data_available(ist_date):
            # ABSTAIN out of holiday-list coverage: the calendar would silently
            # fabricate a regular session for an unknown weekday holiday, so fall
            # back to the legacy fixed window (byte-identical to the old filter).
            if SESSION_OPEN <= ist.time() < SESSION_CLOSE:
                kept.append(bar)
            else:
                off_session += 1
            continue
        if ist_date not in session_by_date:
            session_by_date[ist_date] = get_session(ist_date)
        session = session_by_date[ist_date]
        if session is None:
            non_trading += 1  # weekend / holiday — no session at all
        elif session.open_time_ist <= ist.time() < session.close_time_ist:
            kept.append(bar)
        else:
            off_session += 1
    return SessionFilterResult(
        kept=kept,
        dropped_off_session=off_session,
        dropped_non_trading_date=non_trading,
    )


__all__ = [
    "ALL_FILTERED",
    "ALL_FILTERED_WINDOW_EVENT",
    "IST",
    "SESSION_CLOSE",
    "SESSION_EXEMPT_SYMBOLS",
    "SESSION_OPEN",
    "SessionFilterResult",
    "filter_intraday_session",
    "filter_to_trading_session",
    "format_ist_clock",
    "in_session",
    "is_all_filtered",
    "is_session_exempt",
    "observe_all_filtered",
]
