"""Missing-trading-day gap list — the per-date drill the coverage surfaces never had.

``DEC-COVERAGE-MISSING-SESSIONS-001`` (provisional). Every data-health surface today
disclaims a per-date gap audit — ``spot_coverage.html`` literally reads *"per-window gap
audit not claimed"* and :func:`brutex.dashboard.completeness._session_range_audit` returns
only ``(expected_sessions, ratio)``, never WHICH sessions are absent. So pre-sweep the
operator sees *that* holes exist but not *which* dates, and cannot re-pull exactly the
missing sessions before committing the 2-4 day sweep (CLAUDE.md §8).

This module is the honest fix, and it is **PURE**: it takes the DISTINCT stored IST bar
dates a caller has ALREADY read (the ``session_dates`` frozenset the ONE per-session CTE in
:func:`brutex.dashboard.db_view.summarize_spot_datasets` folds into its existing scan when
``session_date_segments`` is opted in — no second pass over the lake, CLAUDE.md §11 COMMON),
subtracts them from the expected NSE/BSE trading sessions
(:func:`brutex.calendars.trading_days_between` — respecting weekends, the exchange holiday
set, and special weekend sessions), and returns per (symbol, interval):

* ``present_count`` — expected sessions that ARE stored,
* ``expected_count`` — expected NSE/BSE trading sessions over the audited range,
* ``missing_dates`` — the sorted ISO ``YYYY-MM-DD`` list the operator re-pulls.

**Honest envelope (Golden Rule #1 / #10):** the ``session_dates`` the caller supplies come
from ONE ``COUNT(DISTINCT ist_date)``/``array_agg`` columnar aggregate — that read is
**O(bars)**: a SEPARATE session-dates-enriched execution of the same scan SHAPE
``/coverage`` runs (the ``array_agg`` rides one pass WITHIN the drill's own scan; the
drill's route memoises it per write-generation, so it is ONE ADDITIONAL O(bars) pass on
the first drill use per generation — deliberately NOT shared with the ``/coverage`` SPOT
cache entry, whose rows carry no session dates). This module's
own set-difference is **O(sessions)** — NOT O(bars), NOT O(1); the expected-session
calendar walk is memoisable per (start, end, exchange) via the *expected_cache* seam so
an unfiltered ~750-dataset payload pays the day-walk once per distinct range, not per
dataset. Muhurat evening sessions fall
on Diwali HOLIDAYS (``is_trading_day`` is False for them), so they are never *expected* and a
stored Muhurat bar is simply surplus (never flagged missing) — the honest handling of the §7
"muhurat" case. The audit ABSTAINS (never guesses — Golden Rule #1) when it has no honest
basis: no stored dates, an EXPLICIT range override that does not parse (per row,
``bad_range_override`` — an operator's typo'd bound never silently falls back to the
stored span and fakes an interior-only audit), either stored bound missing/unparseable
with no override, a reversed span, a degenerate
zero-session range, OR a bound outside the published holiday coverage
(:data:`~brutex.calendars.HOLIDAY_COVERAGE_FIRST_YEAR` /
:data:`~brutex.calendars.HOLIDAY_COVERAGE_LAST_YEAR` — an out-of-coverage weekday's
trading-day verdict would turn on a missing holiday list).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any, Final

from brutex.calendars import (
    HOLIDAY_COVERAGE_FIRST_YEAR,
    HOLIDAY_COVERAGE_LAST_YEAR,
    trading_days_between,
)

#: The exchanges :func:`~brutex.calendars.trading_days_between` accepts (its holiday-set
#: selector). Any other dataset ``exchange`` (there is none in the spot lake today) falls
#: back to the shared ``None`` holiday set rather than raise.
_CALENDAR_EXCHANGES: Final[frozenset[str]] = frozenset({"NSE", "BSE"})

#: The ONE fetched spot granularity a missing-session drill is honest for. 1-minute is the
#: SINGLE granularity fetched from Groww (DEC-1MINUTE-ONLY-DERIVE-1DAY-001); the derived
#: ``1day`` + intraday TFs are resampled from it and carry NO independent fetch history, so
#: a per-session gap on them would be an artefact of the derivation, not a vendor hole.
DRILL_INTERVAL: Final[str] = "1minute"

#: Per-entry cap on the returned ``missing_dates`` list. The full 2020→today span is
#: ~1,500 NSE sessions, so 2,000 covers a completely-empty dataset without truncating a
#: real one; ``missing_truncated`` + the true ``missing_count`` stay honest if ever exceeded
#: (a defensive bound so a corrupt/degenerate range can never emit an unbounded payload).
MAX_MISSING_DATES: Final[int] = 2000

#: Abstain reasons (Golden Rule #1 — abstain, never guess). Surfaced verbatim so the UI can
#: explain WHY a dataset carries no drill instead of implying "zero gaps".
ABSTAIN_NO_STORED_DATES: Final[str] = "no_stored_dates"
ABSTAIN_NO_RANGE: Final[str] = "no_covered_range"
ABSTAIN_OUT_OF_COVERAGE: Final[str] = "out_of_holiday_coverage"
ABSTAIN_NO_SESSIONS: Final[str] = "no_trading_sessions"
ABSTAIN_BAD_RANGE_OVERRIDE: Final[str] = "bad_range_override"


@dataclass(frozen=True, slots=True)
class MissingSessions:
    """The per-(symbol, interval) missing-trading-day verdict.

    ``abstained`` True ⇒ the count/date fields are all ``None``/empty and
    ``abstain_reason`` names the honest cause; the operator sees *"cannot audit"*,
    never a fabricated *"0 gaps"*.
    """

    exchange: str
    segment: str
    symbol: str
    interval: str
    range_start: str | None
    range_end: str | None
    expected_count: int | None
    present_count: int | None
    missing_count: int | None
    missing_dates: tuple[str, ...]
    missing_truncated: bool
    abstained: bool
    abstain_reason: str | None

    def to_dict(self) -> dict[str, Any]:
        """JSON projection — the exact wire shape the route serialises."""
        return {
            "exchange": self.exchange,
            "segment": self.segment,
            "symbol": self.symbol,
            "interval": self.interval,
            "range_start": self.range_start,
            "range_end": self.range_end,
            "expected_count": self.expected_count,
            "present_count": self.present_count,
            "missing_count": self.missing_count,
            "missing_dates": list(self.missing_dates),
            "missing_truncated": self.missing_truncated,
            "abstained": self.abstained,
            "abstain_reason": self.abstain_reason,
        }


def _parse_iso_date(value: str | None) -> date | None:
    """Parse an ISO ``YYYY-MM-DD`` string, or ``None`` for missing/unparseable input."""
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _calendar_exchange(exchange: str | None) -> str | None:
    """Map a dataset exchange to a :func:`trading_days_between` holiday-set selector.

    ``NSE`` / ``BSE`` pass through (SENSEX rides the BSE holiday list); anything else —
    there is no such spot dataset today — falls back to the shared ``None`` set rather
    than raise a ``ValueError`` deep inside the audit.
    """
    return exchange if exchange in _CALENDAR_EXCHANGES else None


def compute_missing_sessions(
    exchange: str,
    segment: str,
    symbol: str,
    interval: str,
    first_date: str | None,
    last_date: str | None,
    session_dates: Iterable[date] | None,
    *,
    range_start: str | None = None,
    range_end: str | None = None,
    expected_cache: dict[tuple[date, date, str | None], tuple[date, ...]] | None = None,
) -> MissingSessions:
    """Expected NSE/BSE sessions over the audited range MINUS the stored dates.

    Parameters
    ----------
    exchange, segment, symbol, interval
        The dataset coordinates (echoed back on the verdict).
    first_date, last_date
        The dataset's stored ISO ``YYYY-MM-DD`` span bounds (``MIN``/``MAX`` of the
        stored bar dates). The default audit range when no explicit override is given.
    session_dates
        The DISTINCT stored IST bar dates (the ``array_agg(DISTINCT ist_date)`` frozenset).
        ``None`` ⇒ the caller never opted the density enrichment in ⇒ abstain (never
        assume "all present").
    range_start, range_end
        Optional explicit ISO ``YYYY-MM-DD`` audit-range override — e.g. the operator
        checking the covered span against the 2020 pull floor rather than the interior of
        what is stored. When omitted (``None`` / blank) the range is
        ``[first_date, last_date]`` (interior gaps of what the lake HAS). A given
        parseable bound wins over the stored one; a given UNPARSEABLE bound ABSTAINS
        (:data:`ABSTAIN_BAD_RANGE_OVERRIDE`) — an operator's typo'd override never
        silently falls back to the stored span and fakes an interior-only audit
        (Golden Rule #1).
    expected_cache
        Optional mutable memo keyed ``(start, end, calendar_exchange)`` →
        the expected-session tuple. The payload builder passes ONE dict per payload
        so identical ranges (the common unfiltered ~750-dataset case) pay the
        O(span-days) ``is_trading_day`` walk once per distinct range, not per
        dataset. ``None`` (the default — every direct caller) recomputes.

    Returns
    -------
    MissingSessions
        Populated when the audit has an honest basis, or an abstaining verdict.
    """
    if session_dates is None:
        return _abstain(exchange, segment, symbol, interval, None, None, ABSTAIN_NO_STORED_DATES)

    start_override = _normalized_override(range_start)
    end_override = _normalized_override(range_end)
    # An EXPLICIT override that does not parse is a per-row abstain, never a silent
    # fall-back to the stored bound (the interior-only-audit illusion): the raw junk
    # is echoed back on the verdict so the operator sees exactly what was refused.
    if (start_override is not None and _parse_iso_date(start_override) is None) or (
        end_override is not None and _parse_iso_date(end_override) is None
    ):
        return _abstain(
            exchange,
            segment,
            symbol,
            interval,
            start_override,
            end_override,
            ABSTAIN_BAD_RANGE_OVERRIDE,
        )

    start = _parse_iso_date(start_override) or _parse_iso_date(first_date)
    end = _parse_iso_date(end_override) or _parse_iso_date(last_date)
    start_iso = start.isoformat() if start is not None else None
    end_iso = end.isoformat() if end is not None else None
    if start is None or end is None or end < start:
        return _abstain(exchange, segment, symbol, interval, start_iso, end_iso, ABSTAIN_NO_RANGE)
    if start.year < HOLIDAY_COVERAGE_FIRST_YEAR or end.year > HOLIDAY_COVERAGE_LAST_YEAR:
        return _abstain(
            exchange, segment, symbol, interval, start_iso, end_iso, ABSTAIN_OUT_OF_COVERAGE
        )

    expected = _expected_sessions(start, end, _calendar_exchange(exchange), expected_cache)
    if not expected:
        return _abstain(
            exchange, segment, symbol, interval, start_iso, end_iso, ABSTAIN_NO_SESSIONS
        )

    stored = frozenset(session_dates)
    # ``expected`` is already ascending (trading_days_between scans start→end), so the
    # missing list is sorted by construction — no second sort.
    missing = [d for d in expected if d not in stored]
    missing_count = len(missing)
    truncated = missing_count > MAX_MISSING_DATES
    shown = missing[:MAX_MISSING_DATES] if truncated else missing
    return MissingSessions(
        exchange=exchange,
        segment=segment,
        symbol=symbol,
        interval=interval,
        range_start=start_iso,
        range_end=end_iso,
        expected_count=len(expected),
        present_count=len(expected) - missing_count,
        missing_count=missing_count,
        missing_dates=tuple(d.isoformat() for d in shown),
        missing_truncated=truncated,
        abstained=False,
        abstain_reason=None,
    )


def _normalized_override(value: str | None) -> str | None:
    """Treat a blank/whitespace-only explicit range override as NOT PROVIDED.

    A UI form submitting ``?range_start=`` sends an empty string, which is
    "no override", not a typo'd one — only a NON-blank unparseable value trips
    the :data:`ABSTAIN_BAD_RANGE_OVERRIDE` refusal.
    """
    if value is None:
        return None
    stripped = value.strip()
    return stripped if stripped else None


def _expected_sessions(
    start: date,
    end: date,
    calendar_exchange: str | None,
    cache: dict[tuple[date, date, str | None], tuple[date, ...]] | None,
) -> tuple[date, ...]:
    """The expected trading sessions for ``[start, end]`` — memoised when *cache* is given.

    :func:`trading_days_between` is a day-by-day ``is_trading_day`` walk, so an
    unfiltered payload over ~750 datasets sharing (near-)identical ranges would pay
    ~O(datasets × span-days) without the memo. ONE dict per payload build (never a
    module global — no cross-request growth) collapses that to one walk per DISTINCT
    ``(start, end, calendar_exchange)`` key.
    """
    if cache is None:
        return tuple(trading_days_between(start, end, exchange=calendar_exchange))
    key = (start, end, calendar_exchange)
    hit = cache.get(key)
    if hit is None:
        hit = tuple(trading_days_between(start, end, exchange=calendar_exchange))
        cache[key] = hit
    return hit


def _abstain(
    exchange: str,
    segment: str,
    symbol: str,
    interval: str,
    range_start: str | None,
    range_end: str | None,
    reason: str,
) -> MissingSessions:
    """An abstaining verdict — every count ``None``, an honest reason, no fabricated zero."""
    return MissingSessions(
        exchange=exchange,
        segment=segment,
        symbol=symbol,
        interval=interval,
        range_start=range_start,
        range_end=range_end,
        expected_count=None,
        present_count=None,
        missing_count=None,
        missing_dates=(),
        missing_truncated=False,
        abstained=True,
        abstain_reason=reason,
    )


def _matches(value: str, wanted: str | None) -> bool:
    """Case-insensitive exact filter — a blank/``None`` filter matches everything."""
    if wanted is None:
        return True
    wanted = wanted.strip()
    if not wanted:
        return True
    return value.casefold() == wanted.casefold()


def build_missing_sessions_payload(
    rows: Iterable[Mapping[str, Any]],
    *,
    symbol: str | None = None,
    exchange: str | None = None,
    interval: str | None = None,
    range_start: str | None = None,
    range_end: str | None = None,
) -> dict[str, Any]:
    """Project ``summarize_spot_datasets`` rows into the missing-sessions drill payload.

    Consumes the ``session_dates``-enriched rows (each carrying ``exchange`` · ``segment`` ·
    ``symbol`` · ``interval`` · ``first_date`` · ``last_date`` · optionally
    ``session_dates``), keeps ONLY the fetched :data:`DRILL_INTERVAL` (or an explicit
    ``interval`` filter within it), applies the optional (case-insensitive) symbol/exchange
    filters, and computes one :class:`MissingSessions` per surviving dataset — sorted by
    (symbol, exchange, interval), the operator-scan order.

    The payload is well-shaped even for an empty ``rows`` (a lake with no spot datasets, or
    no filesystem-backed store) — the route NEVER 500s. ``datasets`` counts the audited
    datasets; ``total_missing`` sums the non-abstained missing counts (a quick "how many
    sessions to re-pull" headline). ONE per-payload ``expected_cache`` dict memoises the
    expected-session calendar walk per distinct ``(start, end, exchange)`` range, so the
    common unfiltered many-datasets-one-range case pays the O(span-days) walk once — not
    once per dataset.
    """
    expected_cache: dict[tuple[date, date, str | None], tuple[date, ...]] = {}
    entries: list[MissingSessions] = []
    for row in rows:
        row_interval = str(row.get("interval", ""))
        # Only the ONE fetched granularity is honestly auditable (derived TFs excluded),
        # and an explicit interval filter must still land inside it.
        if row_interval != DRILL_INTERVAL:
            continue
        if not _matches(row_interval, interval):
            continue
        row_symbol = str(row.get("symbol", ""))
        row_exchange = str(row.get("exchange", ""))
        if not _matches(row_symbol, symbol) or not _matches(row_exchange, exchange):
            continue
        raw_dates = row.get("session_dates")
        entries.append(
            compute_missing_sessions(
                row_exchange,
                str(row.get("segment", "")),
                row_symbol,
                row_interval,
                _iso_or_none(row.get("first_date")),
                _iso_or_none(row.get("last_date")),
                raw_dates if raw_dates is None else frozenset(raw_dates),
                range_start=range_start,
                range_end=range_end,
                expected_cache=expected_cache,
            )
        )
    entries.sort(key=lambda e: (e.symbol, e.exchange, e.interval))
    total_missing = sum(e.missing_count or 0 for e in entries)
    return {
        "datasets": len(entries),
        "total_missing": total_missing,
        "interval": DRILL_INTERVAL,
        "range_start": range_start,
        "range_end": range_end,
        "rows": [e.to_dict() for e in entries],
    }


def _iso_or_none(value: Any) -> str | None:
    """Coerce a row's date field to an ISO string or ``None`` (a real ``date`` or a str)."""
    if value is None:
        return None
    if isinstance(value, date):
        return value.isoformat()
    return str(value)
