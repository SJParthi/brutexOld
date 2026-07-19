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
* ``missing_dates`` — the sorted ISO ``YYYY-MM-DD`` list the operator re-pulls,
* ``missing_before_first_stored`` — how many of ``missing_count`` fall strictly BEFORE
  the earliest stored session (combined-PR fix round 2): under an explicit range
  override reaching before the stored span (the 2020-floor audit), a LATE-LISTED
  symbol's un-pullable pre-listing dates would otherwise be conflated with genuine
  interior vendor holes in the "sessions to re-pull" headline — the split keeps both
  honest without clamping away a genuinely-missing early history.

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
trading-day verdict would turn on a missing holiday list). An audit end AT OR AFTER the
caller-injected IST *today* (continuation-v2 fix + the continuation-v3 residue fix) is
CLAMPED to ``today − 1 day`` with the clamp NAMED on the verdict
(``range_end_clamped_to_today``) — a not-yet-occurred trading session is never a
re-pullable vendor hole, and TODAY's own session is never auditable either (pre-close it
has not occurred; post-close ``resolve_candle_range`` still caps every pull at the
previous day), so neither may count into ``missing_count``; the module stays PURE
(*today* is injected — the route binds ``now_ist().date()``; ``today=None`` applies no
clamp).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Final

from brutex.calendars import (
    HOLIDAY_COVERAGE_FIRST_YEAR,
    HOLIDAY_COVERAGE_LAST_YEAR,
    trading_days_between,
)
from brutex.data.scope_filter import scope_excluded_echo

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
    missing_before_first_stored: int | None
    missing_dates: tuple[str, ...]
    missing_truncated: bool
    abstained: bool
    abstain_reason: str | None
    #: Continuation-v2 fix (+ the continuation-v3 residue fix) — True iff an
    #: audit end AT OR AFTER the injected *today* (an explicit ``range_end``
    #: of today or later, or a corrupt future stored bound) was clamped to
    #: ``today − 1 day``, the last auditable session date: a not-yet-occurred
    #: trading session is never a re-pullable vendor hole, and TODAY's own
    #: session is never auditable either (pre-close it has not occurred;
    #: post-close ``resolve_candle_range`` still caps every pull at the
    #: previous day). The clamp is NAMED here (the PAGE_LIMIT_MAX naming
    #: precedent) instead of silently applied.
    range_end_clamped_to_today: bool = False

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
            "missing_before_first_stored": self.missing_before_first_stored,
            "missing_dates": list(self.missing_dates),
            "missing_truncated": self.missing_truncated,
            "abstained": self.abstained,
            "abstain_reason": self.abstain_reason,
            "range_end_clamped_to_today": self.range_end_clamped_to_today,
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
    today: date | None = None,
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
        (Golden Rule #1). CAVEAT (combined-PR fix round 2): a ``range_start``
        earlier than the first stored session counts dates the lake never held —
        for a late-listed symbol those are un-pullable pre-listing dates, not
        vendor holes; the verdict's ``missing_before_first_stored`` splits them
        out of ``missing_count`` so the two classes are never conflated.
    today
        The audit clock (continuation-v2 fix + the continuation-v3 residue
        fix): the caller-injected IST *today* (the module stays PURE — the
        route binds ``now_ist().date()``). A resolved audit end AT OR AFTER
        *today* (an explicit ``range_end`` of today or later, or a corrupt
        future stored bound) is CLAMPED to ``today − 1 day`` with the clamp
        NAMED on the verdict (``range_end_clamped_to_today`` + the clamped
        ``range_end`` echo) — a not-yet-occurred trading session must never
        count into ``missing_count``/``total_missing`` (which every consumer
        presents as re-pullable vendor holes), and TODAY's own session is
        equally un-auditable: pre-close it has not occurred, post-close it is
        still un-pullable (``resolve_candle_range`` caps every pull at the
        previous day). ``None`` (the default — pure/offline callers) applies
        NO clamp, byte-identical to the prior behaviour.
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
    # Future/today-end clamp (continuation-v2 fix + the continuation-v3 residue
    # fix): an audit end past the injected *today* would count every
    # not-yet-occurred trading session into ``missing_count`` — fabricated
    # "sessions to re-pull" — and an end AT *today* counted TODAY's own session
    # the same way (pre-close it has not occurred; post-close it is still
    # un-pullable — ``resolve_candle_range`` caps every pull at the previous
    # day), so the clamp is ``end >= today`` → ``today − 1 day`` (the last
    # auditable, re-pullable session date). Clamp BEFORE the range/coverage
    # checks (so a far-future ``range_end`` audits honestly up to yesterday
    # instead of abstaining out-of-coverage) and NAME the clamp on the verdict;
    # ``today is None`` (pure callers) applies no clamp.
    end_clamped = False
    if today is not None and end is not None and end >= today:
        end = today - timedelta(days=1)
        end_clamped = True
    start_iso = start.isoformat() if start is not None else None
    end_iso = end.isoformat() if end is not None else None
    if start is None or end is None or end < start:
        return _abstain(
            exchange,
            segment,
            symbol,
            interval,
            start_iso,
            end_iso,
            ABSTAIN_NO_RANGE,
            range_end_clamped=end_clamped,
        )
    if start.year < HOLIDAY_COVERAGE_FIRST_YEAR or end.year > HOLIDAY_COVERAGE_LAST_YEAR:
        return _abstain(
            exchange,
            segment,
            symbol,
            interval,
            start_iso,
            end_iso,
            ABSTAIN_OUT_OF_COVERAGE,
            range_end_clamped=end_clamped,
        )

    expected = _expected_sessions(start, end, _calendar_exchange(exchange), expected_cache)
    if not expected:
        return _abstain(
            exchange,
            segment,
            symbol,
            interval,
            start_iso,
            end_iso,
            ABSTAIN_NO_SESSIONS,
            range_end_clamped=end_clamped,
        )

    stored = frozenset(session_dates)
    # ``expected`` is already ascending (trading_days_between scans start→end), so the
    # missing list is sorted by construction — no second sort.
    missing = [d for d in expected if d not in stored]
    missing_count = len(missing)
    truncated = missing_count > MAX_MISSING_DATES
    shown = missing[:MAX_MISSING_DATES] if truncated else missing
    # Pre-stored-span split (combined-PR fix round 2 — Golden Rule #10): an explicit
    # ``range_start`` earlier than the FIRST stored session counts dates the lake has
    # never held — for a LATE-LISTED symbol those are un-pullable pre-listing dates, NOT
    # vendor holes, yet they inflate ``missing_count`` under the route's own advertised
    # 2020-floor audit. The split names how many of ``missing_count`` fall strictly
    # BEFORE the earliest stored session, so the operator can tell "never listed /
    # never pulled" head gaps from interior re-pullable holes. Deliberately NOT a
    # clamp — an index genuinely missing its early history must stay auditable. With
    # ZERO stored sessions there is no first-stored anchor, so the split is an honest
    # 0 (every gap counts as ordinary missing — conservative, disclosed).
    first_stored = min(stored) if stored else None
    missing_before = 0 if first_stored is None else sum(1 for d in missing if d < first_stored)
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
        missing_before_first_stored=missing_before,
        missing_dates=tuple(d.isoformat() for d in shown),
        missing_truncated=truncated,
        abstained=False,
        abstain_reason=None,
        range_end_clamped_to_today=end_clamped,
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
    *,
    range_end_clamped: bool = False,
) -> MissingSessions:
    """An abstaining verdict — every count ``None``, an honest reason, no fabricated zero.

    *range_end_clamped* carries the future-end clamp flag through the POST-clamp
    abstains (e.g. a fully-future range collapsing to ``end < start``), so the
    clamp stays NAMED even when the audit then abstains; pre-clamp abstains keep
    the ``False`` default.
    """
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
        missing_before_first_stored=None,
        missing_dates=(),
        missing_truncated=False,
        abstained=True,
        abstain_reason=reason,
        range_end_clamped_to_today=range_end_clamped,
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
    today: date | None = None,
    scope_excluded: int = 0,
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
    sessions to re-pull" headline) and ``total_missing_before_first_stored`` sums the
    pre-stored-span split (combined-PR fix round 2 — under an explicit range override a
    late-listed symbol's un-pullable pre-listing dates land in the split, not silently
    in the re-pull headline's interior-hole reading). *today* (continuation-v2 fix + the
    continuation-v3 residue fix) is the caller-injected IST audit clock threaded to every
    per-dataset audit — an at/after-*today* audit end is clamped to ``today − 1 day`` with
    the clamp NAMED per row (``range_end_clamped_to_today``), so neither a
    not-yet-occurred session nor today's still-un-pullable one ever inflates
    ``total_missing``; ``None`` (pure/offline callers) applies no clamp. ONE per-payload ``expected_cache`` dict memoises the
    expected-session calendar walk per distinct ``(start, end, exchange)`` range, so the
    common unfiltered many-datasets-one-range case pays the O(span-days) walk once — not
    once per dataset.

    *scope_excluded* (DEC-UNIVERSE-INDEX-ONLY-001, the post-merge hostile-review
    r4 BLOCKER fold): the census scan's read-scope exclusion count
    (:func:`~brutex.dashboard.db_view.summary_rows_scope_excluded` over the
    route's R2-wrapper rows) — EMIT-ON-NONZERO via
    :func:`~brutex.data.scope_filter.scope_excluded_echo`, so the default ``0``
    (an unfiltered caller / an index-only lake) keeps the payload
    byte-identical; a nonzero count names how many stored-only symbols the
    bounded scope excluded from the drill (their re-pull guidance would point
    at symbols the P7 pull gates refuse).
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
                today=today,
                expected_cache=expected_cache,
            )
        )
    entries.sort(key=lambda e: (e.symbol, e.exchange, e.interval))
    total_missing = sum(e.missing_count or 0 for e in entries)
    total_missing_before = sum(e.missing_before_first_stored or 0 for e in entries)
    payload: dict[str, Any] = {
        "datasets": len(entries),
        "total_missing": total_missing,
        "total_missing_before_first_stored": total_missing_before,
        "interval": DRILL_INTERVAL,
        "range_start": range_start,
        "range_end": range_end,
        "rows": [e.to_dict() for e in entries],
    }
    payload.update(scope_excluded_echo(scope_excluded))
    return payload


def _iso_or_none(value: Any) -> str | None:
    """Coerce a row's date field to an ISO string or ``None`` (a real ``date`` or a str)."""
    if value is None:
        return None
    if isinstance(value, date):
        return value.isoformat()
    return str(value)
