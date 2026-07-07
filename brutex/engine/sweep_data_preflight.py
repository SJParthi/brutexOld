"""Pure sweep data-preflight cores — clamps + coverage/quarantine/exclusion folds.

DEC-SWEEP-PREFLIGHT-ESTIMATOR-001 (Phase 1, WP2). The hard, refuse-whole sweep
data preflight (``brutex.engine.sweep_gate``) is composed from PURE cores that
take already-scanned inputs and return frozen verdict reports — this module is
those cores:

* :class:`RefusalCode` — the machine-stable refusal vocabulary (the P2-P6
  payload contract; values are never renamed/renumbered).
* :func:`clamp_dates` → :class:`DateClampReport` — the deterministic date
  clamp + expected-session enumeration (floor clamp with a visible note; the
  PULL-DELIVERABLE end bound; reversed / zero-session / calendar-horizon
  gates — the coverage-horizon gate runs BEFORE the enumerator so the
  unguarded calendar is never consulted on an uncovered span: no fabricated
  sessions).
* :func:`preflight_disk_coverage` → :class:`CoverageReport` — the
  refuse-whole, gather-all fold over the READ-ONLY disk scans
  (:class:`~brutex.data.session_coverage.PairScan`): every requested
  ``(symbol, TF)`` pair is walked (never fail-fast-first), exact missing
  spans are contiguous runs in the EXPECTED-session sequence (a holiday
  between two missing sessions does not split a span), an ABSENT scan key IS
  a not-ready verdict (fail-closed), and a corrupt file is a NAMED
  ``CORRUPT_PARTITION`` refusal. DISK is the presence authority
  (DISK-verified, never ledger-only).
* :func:`preflight_quarantine` → :class:`QuarantineReport` — the ledger
  quarantine fold (bimodal ``SKIPPED`` split: an accept-gap settle over
  KNOWN-incomplete bars refuses, an expired-window empty settle never does;
  an unreadable ledger is a fail-CLOSED ``LEDGER_UNREADABLE`` refusal — the
  only reading under which "no quarantined partitions" is verifiable). The
  ledger can only ADD refusals, never approve.
* :class:`ExclusionReport` — the sweep-vs-pull exclusion probe verdict
  ("lock wins").

**Purity law:** zero I/O — every input is injected (scans, ledger flags,
lock probe booleans, the ``today`` clock). Cost is O(expected sessions ×
pairs) pure folding. Calendar math reuses the COMMON primitives
(:func:`brutex.calendars.trading_days_between`,
:func:`brutex.data.partial_window_detect.last_completed_session_on_or_before`)
— never a copy (Golden Rule #11 COMMON).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from brutex.calendars import (
    HOLIDAY_COVERAGE_FIRST_YEAR,
    HOLIDAY_COVERAGE_LAST_YEAR,
    trading_days_between,
)
from brutex.data.partial_window_detect import last_completed_session_on_or_before
from brutex.data.window_ledger import DEMOTED_SETTLE_REASONS, FAILED, SKIPPED

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Mapping, Sequence
    from collections.abc import Set as AbstractSet

    from brutex.data.session_coverage import PairScan, QuarantineScan

__all__ = [
    "MAX_SPANS_NAMED",
    "SWEEP_HISTORY_FLOOR",
    "CoverageReport",
    "DateClampReport",
    "ExclusionReport",
    "LedgerFlag",
    "MissingSpan",
    "PairVerdict",
    "QuarantineReport",
    "RefusalCode",
    "clamp_dates",
    "preflight_disk_coverage",
    "preflight_quarantine",
]


class RefusalCode(StrEnum):
    """Machine-stable refusal vocabulary — the P2-P6 contract. Never renumber/rename."""

    ZERO_SESSIONS = "zero_sessions"
    REVERSED_RANGE = "reversed_range"
    CALENDAR_COVERAGE_EXHAUSTED = "calendar_coverage_exhausted"
    COVERAGE_MISSING = "coverage_missing"  # 1m/1day sessions absent on disk
    DERIVED_TF_MISSING = "derived_tf_missing"  # requested derived TF never materialized in span
    CORRUPT_PARTITION = "corrupt_partition"  # unreadable parquet in the probed range
    QUARANTINED_RANGE = "quarantined_range"  # ledger demotion tags overlap the range
    LEDGER_UNREADABLE = "ledger_unreadable"  # load_failed=True — fail-CLOSED
    PULL_IN_FLIGHT = "pull_in_flight"
    DERIVE_IN_FLIGHT = "derive_in_flight"
    DISK_SPACE = "disk_space"
    SLA_OVERRUN = "sla_overrun"  # the relocated check_sweep_sla REFUSED verdict


#: Track-1 data floor, restated per the derive_lock.py:86-88 convention (engine must not
#: import brutex.dashboard.pull_controller); equality drift-pinned by test.
SWEEP_HISTORY_FLOOR: Final[date] = date(2020, 1, 1)

#: Span-list display cap per pair — "(+K more spans)" with the TRUE total (house cap style).
MAX_SPANS_NAMED: Final[int] = 12

#: The two session-DATE-verifiable intervals (fetched ``1minute`` + the derived-but-
#: date-checkable ``1day``); every other requested TF is a derived intraday timeframe
#: verified by span-bounded PRESENCE. Restated from
#: :data:`brutex.data.session_coverage.DATE_CHECKED_INTERVALS` so this module stays
#: import-light (the scanner drags polars); byte-equality drift-pinned by test.
_DATE_CHECKED_INTERVALS: Final[frozenset[str]] = frozenset({"1minute", "1day"})

#: The ONE data-readiness remedy line (golden-pinned downstream): the sweep NEVER
#: pulls (R3) — the operator pulls on /universe, then re-runs.
_PULL_REMEDY: Final[str] = (
    "pull on /universe first (▶ Pull · full pipeline); the sweep NEVER pulls (R3)"
)


@dataclass(frozen=True, slots=True)
class DateClampReport:
    """Outcome of :func:`clamp_dates` — the deterministic date clamp + enumeration.

    Attributes
    ----------
    requested_start, requested_end
        The dates exactly as requested (pre-clamp).
    start
        Post-clamp effective start (``max(requested_start, SWEEP_HISTORY_FLOOR)``).
    end
        Post-clamp effective end — never past the last PULL-DELIVERABLE session
        (:func:`~brutex.data.partial_window_detect.last_completed_session_on_or_before`
        of ``today − 1``).
    notes
        Human-visible clamp notes, one per moved endpoint (R4 "visible note";
        empty when nothing moved).
    reversed_range
        ``start > end`` after clamping — a genuinely reversed request OR a
        fully-future range that clamped past its start.
    zero_sessions
        The clamped range contains ZERO expected sessions (weekends / holidays
        / observed-overlay days only). Only computed when the range is neither
        reversed nor out of calendar — each flag names its true cause.
    out_of_calendar
        The clamped range exits the verified NSE holiday coverage horizon
        (``HOLIDAY_COVERAGE_FIRST_YEAR..HOLIDAY_COVERAGE_LAST_YEAR``) — the
        unguarded enumerator was NOT consulted (no fabricated sessions).
    expected_sessions
        The canonical expected-session list (observed-overlay subtracted;
        Muhurat dates excluded — their stored bars read as never-missing
        extras; special-weekend Budget sessions included). Empty on any
        hard refusal.
    """

    requested_start: date
    requested_end: date
    start: date
    end: date
    notes: tuple[str, ...]
    reversed_range: bool
    zero_sessions: bool
    out_of_calendar: bool
    expected_sessions: tuple[date, ...]

    @property
    def ok(self) -> bool:
        """``True`` iff no clamp-level refusal flag is set."""
        return not (self.reversed_range or self.zero_sessions or self.out_of_calendar)

    @property
    def refusal_codes(self) -> tuple[RefusalCode, ...]:
        """Machine codes for the set flags, in clamp-algorithm order."""
        codes: list[RefusalCode] = []
        if self.reversed_range:
            codes.append(RefusalCode.REVERSED_RANGE)
        if self.out_of_calendar:
            codes.append(RefusalCode.CALENDAR_COVERAGE_EXHAUSTED)
        if self.zero_sessions:
            codes.append(RefusalCode.ZERO_SESSIONS)
        return tuple(codes)

    def refusal(self) -> str:
        """The complete clamp refusal text — exact cause(s) + remedy; ``""`` when ok."""
        causes: list[str] = []
        if self.reversed_range:
            causes.append(
                f"reversed range after clamping: start {self.start.isoformat()} > end "
                f"{self.end.isoformat()} — fix the requested dates (requested "
                f"{self.requested_start.isoformat()} → {self.requested_end.isoformat()}; "
                "a fully-future range clamps past its start)"
            )
        if self.out_of_calendar:
            next_year = HOLIDAY_COVERAGE_LAST_YEAR + 1
            causes.append(
                f"clamped range {self.start.isoformat()} → {self.end.isoformat()} exits the "
                f"verified NSE holiday coverage horizon {HOLIDAY_COVERAGE_FIRST_YEAR}.."
                f"{HOLIDAY_COVERAGE_LAST_YEAR} — land HOLIDAYS_{next_year} in "
                f"brutex/calendars/nse.py before sweeping {next_year} dates"
            )
        if self.zero_sessions:
            causes.append(
                f"0 expected sessions in the clamped range {self.start.isoformat()} → "
                f"{self.end.isoformat()} (weekends / holidays / observed-overlay days only) — "
                "widen the range to include at least one NSE trading session"
            )
        return "; ".join(causes)


def clamp_dates(
    requested_start: date,
    requested_end: date,
    *,
    today: date,
    observed: AbstractSet[date] = frozenset(),
) -> DateClampReport:
    """Clamp the requested sweep dates and enumerate the expected sessions — pure.

    The deterministic clamp algorithm (BUILD-SPEC §2.1/§4, in order):

    1. ``start = max(requested_start, SWEEP_HISTORY_FLOOR)`` — note appended
       iff moved (a pre-2020 request precedes the Track-1 data pull floor).
    2. **Pull-deliverable end bound** — ``end_bound =
       last_completed_session_on_or_before(today − 1, observed=observed)``.
       Rationale: the DEFAULT pull's upper bound is the PREVIOUS day
       (``resolve_candle_range``), so any clamp admitting *today* (all day,
       or post-close via ``daily_derive_cutoff``) would make the default
       2020→today sweep chronically refuse on a hole the default remediation
       ("pull on /universe first") cannot fill. The bound is time-of-day
       independent (injectable ``today: date``) and structurally immune to
       ``daily_derive_cutoff``'s EXCLUSIVE-semantics off-by-one. Honest
       trade-off (named): an explicitly-pulled post-close *today* is excluded
       — one-session conservatism. ``end = min(requested_end, end_bound)``;
       note appended iff moved. A past ``requested_end`` on a non-session
       date is NOT moved (expected-session enumeration handles it).
    3. ``reversed_range = start > end`` — covers a genuinely reversed request
       AND a fully-future range that clamped past the start.
    4. Coverage-horizon gate BEFORE enumeration: a clamped range exiting
       ``HOLIDAY_COVERAGE_FIRST_YEAR..HOLIDAY_COVERAGE_LAST_YEAR`` sets
       ``out_of_calendar`` — the unguarded enumerator is never called on an
       uncovered span (no fabricated sessions).
    5. ``expected = trading_days_between(start, end)`` minus the *observed*
       overlay — includes special-weekend Budget sessions, excludes weekends
       / holidays AND Muhurat dates; ``zero_sessions = not expected``.
       Enumeration is skipped on a reversed / out-of-calendar range (each
       flag names its true cause; a reversed range has no session list).

    Parameters
    ----------
    requested_start, requested_end
        The operator-requested inclusive date range.
    today
        The injectable clock (the ``resolve_sweep_request(today=…)``
        precedent — no ``now_ist`` needed).
    observed
        The observed-holidays overlay
        (:func:`~brutex.calendars.observed_sessions.load_observed_holidays`
        output) — subtracted from expected, and the end bound rolls back
        past an observed day.

    Returns
    -------
    DateClampReport
        The frozen clamp verdict + the canonical expected-session list.
    """
    notes: list[str] = []
    start = requested_start
    if start < SWEEP_HISTORY_FLOOR:
        start = SWEEP_HISTORY_FLOOR
        notes.append(
            f"start clamped {requested_start.isoformat()} → {start.isoformat()} "
            f"(requested start precedes SWEEP_HISTORY_FLOOR {SWEEP_HISTORY_FLOOR.isoformat()}, "
            "the Track-1 data pull floor)"
        )
    end_bound = last_completed_session_on_or_before(today - timedelta(days=1), observed=observed)
    end = requested_end
    if end > end_bound:
        end = end_bound
        notes.append(
            f"end clamped {requested_end.isoformat()} → {end.isoformat()} "
            "(last pull-deliverable session on/before today − 1 — the default /universe "
            "pull's upper bound is the previous day)"
        )
    reversed_range = start > end
    out_of_calendar = (
        start.year < HOLIDAY_COVERAGE_FIRST_YEAR or end.year > HOLIDAY_COVERAGE_LAST_YEAR
    )
    if reversed_range or out_of_calendar:
        expected: tuple[date, ...] = ()
        zero_sessions = False
    else:
        expected = tuple(d for d in trading_days_between(start, end) if d not in observed)
        zero_sessions = not expected
    return DateClampReport(
        requested_start=requested_start,
        requested_end=requested_end,
        start=start,
        end=end,
        notes=tuple(notes),
        reversed_range=reversed_range,
        zero_sessions=zero_sessions,
        out_of_calendar=out_of_calendar,
        expected_sessions=expected,
    )


@dataclass(frozen=True, slots=True)
class MissingSpan:
    """One contiguous run of missing sessions in the EXPECTED sequence.

    Contiguity is in the expected-session SEQUENCE — a holiday (or weekend /
    Muhurat date) between two missing sessions does not split a span.

    Attributes
    ----------
    start
        First missing expected session of the run (inclusive).
    end
        Last missing expected session of the run (inclusive; ``== start``
        for a one-day span).
    sessions
        Expected sessions inside the span.
    """

    start: date
    end: date
    sessions: int

    def describe(self) -> str:
        """Render the span as ``start→end (N sessions)`` (single-day: one date)."""
        if self.start == self.end:
            return f"{self.start.isoformat()} (1 session)"
        return f"{self.start.isoformat()}→{self.end.isoformat()} ({self.sessions} sessions)"


@dataclass(frozen=True, slots=True)
class PairVerdict:
    """One not-ready ``(symbol, interval)`` pair verdict from the coverage fold.

    Attributes
    ----------
    symbol, interval
        The requested pair.
    reason
        ``COVERAGE_MISSING`` | ``DERIVED_TF_MISSING`` | ``CORRUPT_PARTITION``.
    spans
        Exact missing spans (empty for presence/corrupt verdicts).
    detail
        One honest clause (file path(s) for corrupt; "never materialized" for
        a derived TF; the missing-session tally for coverage).
    """

    symbol: str
    interval: str
    reason: RefusalCode
    spans: tuple[MissingSpan, ...]
    detail: str

    def describe(self) -> str:
        """Render the verdict line, capping spans at :data:`MAX_SPANS_NAMED`.

        The cap keeps the refusal readable on a pathological lake while
        staying honest: the tail carries the TRUE totals —
        ``(+K more spans; N spans / M missing sessions in total)``.
        """
        head = f"{self.symbol} × {self.interval}: {self.detail}"
        if not self.spans:
            return head
        named = ", ".join(span.describe() for span in self.spans[:MAX_SPANS_NAMED])
        extra = len(self.spans) - MAX_SPANS_NAMED
        if extra > 0:
            total_sessions = sum(span.sessions for span in self.spans)
            named += (
                f" (+{extra} more spans; {len(self.spans)} spans / "
                f"{total_sessions} missing sessions in total)"
            )
        return f"{head} — {named}"


@dataclass(frozen=True, slots=True)
class CoverageReport:
    """The refuse-whole disk-coverage verdict over every requested pair.

    Attributes
    ----------
    not_ready
        Every not-ready pair verdict, deterministic: request order,
        symbols-major (a pair can carry TWO verdicts — its kind verdict AND a
        ``CORRUPT_PARTITION`` — both are gathered, never masked).
    checked_pairs
        Total requested pairs (symbols × intervals).
    expected_sessions
        Size of the canonical expected-session list the fold verified against.
    covered_sessions
        Per DATES pair, the count of expected sessions found on disk —
        observability only, never a verdict axis.
    """

    not_ready: tuple[PairVerdict, ...]
    checked_pairs: int
    expected_sessions: int
    covered_sessions: Mapping[tuple[str, str], int]

    @property
    def ok(self) -> bool:
        """``True`` iff every requested pair is DISK-ready."""
        return not self.not_ready

    def remediation(self) -> str:
        """The ONE data-readiness remedy — the sweep NEVER pulls (R3)."""
        return _PULL_REMEDY

    def refusal(self) -> str:
        """The complete coverage refusal — EVERY pair + exact spans + remedy; ``""`` when ok.

        Refuse-whole aggregation law: one text names every failing pair and
        every span (capped per pair with the TRUE totals) — never
        fail-fast-first-symbol, never a run-ready subset.
        """
        if self.ok:
            return ""
        lines = "; ".join(verdict.describe() for verdict in self.not_ready)
        return (
            f"data not ready on disk ({len(self.not_ready)} not-ready verdict(s) over "
            f"{self.checked_pairs} requested pair(s)): {lines} — {self.remediation()}"
        )


def _missing_spans(
    expected_sessions: Sequence[date], covered: AbstractSet[date]
) -> tuple[MissingSpan, ...]:
    """Group the missing expected sessions into contiguous EXPECTED-sequence runs.

    Two missing sessions are one span iff they are CONSECUTIVE entries of the
    expected sequence — a holiday/weekend between them never splits the run.
    Pure, O(expected).
    """
    spans: list[MissingSpan] = []
    run_start: date | None = None
    run_end: date = date.min
    run_sessions = 0
    for session in expected_sessions:
        if session not in covered:
            if run_start is None:
                run_start = session
                run_sessions = 0
            run_end = session
            run_sessions += 1
        elif run_start is not None:
            spans.append(MissingSpan(start=run_start, end=run_end, sessions=run_sessions))
            run_start = None
    if run_start is not None:
        spans.append(MissingSpan(start=run_start, end=run_end, sessions=run_sessions))
    return tuple(spans)


def preflight_disk_coverage(
    expected_sessions: Sequence[date],
    scans: Mapping[tuple[str, str], PairScan],
    *,
    symbols: Sequence[str],
    intervals: Sequence[str],
) -> CoverageReport:
    """Fold the read-only disk scans into the refuse-whole coverage verdict — pure.

    Fold rules (gather-all — the fold walks EVERY pair, never
    fail-fast-first):

    * every requested pair MUST have a scan entry — an ABSENT key IS a
      not-ready verdict (fail-closed, mirroring the shipped
      ``preflight_slices`` absent-counts-zero law): a DATES pair reads as
      zero coverage, a PRESENCE pair as not present;
    * DATES pairs (``1minute`` / ``1day``): the expected sessions absent from
      ``scan.covered`` are grouped into contiguous EXPECTED-sequence runs
      (:class:`MissingSpan`) ⇒ ``COVERAGE_MISSING``;
    * PRESENCE pairs (the 19 derived intraday TFs): ``present=False`` ⇒
      ``DERIVED_TF_MISSING`` (a present-but-STALE derived TF passes here and
      is refused post-load by the shipped #1492 equality gate — the honest
      split);
    * any ``scan.corrupt_files`` ⇒ an ADDITIONAL ``CORRUPT_PARTITION``
      verdict naming the paths — the pair is not-ready even if its dates are
      covered.

    Parameters
    ----------
    expected_sessions
        The canonical expected list (:attr:`DateClampReport.expected_sessions`
        of an ok clamp — the clamp gate refuses an empty list one gate
        earlier).
    scans
        :func:`~brutex.data.session_coverage.scan_selection_coverage` output.
    symbols, intervals
        The requested selection axes (request order — the verdict order).

    Returns
    -------
    CoverageReport
        The gathered verdict; ``ok`` iff every pair is DISK-ready.
    """
    expected = tuple(expected_sessions)
    not_ready: list[PairVerdict] = []
    covered_counts: dict[tuple[str, str], int] = {}
    for symbol in symbols:
        for interval in intervals:
            scan = scans.get((symbol, interval))
            corrupt = scan.corrupt_files if scan is not None else ()
            if interval in _DATE_CHECKED_INTERVALS:
                covered = scan.covered if scan is not None else frozenset()
                covered_counts[(symbol, interval)] = sum(1 for d in expected if d in covered)
                spans = _missing_spans(expected, covered)
                if spans:
                    missing_total = sum(span.sessions for span in spans)
                    not_ready.append(
                        PairVerdict(
                            symbol=symbol,
                            interval=interval,
                            reason=RefusalCode.COVERAGE_MISSING,
                            spans=spans,
                            detail=(
                                f"{missing_total} of {len(expected)} expected sessions "
                                "absent on disk"
                            ),
                        )
                    )
            elif scan is None or not scan.present:
                not_ready.append(
                    PairVerdict(
                        symbol=symbol,
                        interval=interval,
                        reason=RefusalCode.DERIVED_TF_MISSING,
                        spans=(),
                        detail="derived TF never materialized in the requested span",
                    )
                )
            if corrupt:
                not_ready.append(
                    PairVerdict(
                        symbol=symbol,
                        interval=interval,
                        reason=RefusalCode.CORRUPT_PARTITION,
                        spans=(),
                        detail=f"unreadable parquet file(s): {', '.join(corrupt)}",
                    )
                )
    return CoverageReport(
        not_ready=tuple(not_ready),
        checked_pairs=len(symbols) * len(intervals),
        expected_sessions=len(expected),
        covered_sessions=covered_counts,
    )


@dataclass(frozen=True, slots=True)
class LedgerFlag:
    """One demoted / accepted-gap ledger window rendered by the quarantine fold.

    The engine-side projection of
    :class:`~brutex.data.session_coverage.LedgerRowFlag` (converted by
    :func:`preflight_quarantine`).

    Attributes
    ----------
    security_id, interval
        The flagged window's coordinates (ledger ``symbol_name`` form).
    window_start, window_end
        ISO date strings, as stored (``window_end`` is fetch-exclusive).
    status
        ``FAILED`` (open demotion) | ``SKIPPED`` (accepted gap).
    last_error
        The demotion tag (a ``DEMOTED_SETTLE_REASONS`` member).
    """

    security_id: str
    interval: str
    window_start: str
    window_end: str
    status: str
    last_error: str


@dataclass(frozen=True, slots=True)
class QuarantineReport:
    """The ledger-quarantine fold verdict — the ledger only ADDS refusals.

    Attributes
    ----------
    demoted_open
        ``FAILED`` windows carrying a ``DEMOTED_SETTLE_REASONS`` tag — an
        OPEN demotion (will re-pull).
    accepted_gaps
        ``SKIPPED`` windows carrying a ``DEMOTED_SETTLE_REASONS`` tag — an
        ACCEPT-GAP settle: the stored bars are KNOWN-incomplete for that
        window (bimodal-``SKIPPED`` honesty: an expired-window
        "vendor confirmed no trades" settle never lands here).
    ledger_load_failed
        The persisted ledger existed but could not be read ⇒ the fail-CLOSED
        ``LEDGER_UNREADABLE`` refusal ("no quarantined partitions" is
        unverifiable).
    """

    demoted_open: tuple[LedgerFlag, ...]
    accepted_gaps: tuple[LedgerFlag, ...]
    ledger_load_failed: bool

    @property
    def ok(self) -> bool:
        """``True`` iff no quarantine annotation overlaps AND the ledger was readable."""
        return not (self.demoted_open or self.accepted_gaps or self.ledger_load_failed)

    def remediation(self) -> str:
        """Per-class operator remedies (bimodal-``SKIPPED`` honesty); ``""`` when ok."""
        parts: list[str] = []
        if self.ledger_load_failed:
            parts.append(
                "the persisted window ledger is UNREADABLE (fail-CLOSED — 'no quarantined "
                "partitions' is unverifiable): restore <lake>/window_ledger/ledger.parquet "
                "or re-pull on /universe to rebuild it"
            )
        if self.demoted_open:
            parts.append(
                "open demotions (FAILED windows flagged by the completeness detectors — "
                f"will re-pull): {_PULL_REMEDY}"
            )
        if self.accepted_gaps:
            parts.append(
                "accepted gaps (SKIPPED windows settled over KNOWN-incomplete stored bars — "
                "an accepted gap, not a vendor no-trades answer): run `brutex reopen-skipped` "
                "to re-open them, then pull on /universe first (▶ Pull · full pipeline)"
            )
        return "; ".join(parts)


def preflight_quarantine(scan: QuarantineScan) -> QuarantineReport:
    """Fold the ledger-quarantine scan into the pure gate report.

    Splits the scanned flags bimodally: ``FAILED`` + demotion tag ⇒
    ``demoted_open`` (refuses); ``SKIPPED`` + demotion tag ⇒ ``accepted_gaps``
    (refuses — the bars on disk are KNOWN-incomplete); a ``SKIPPED`` row whose
    ``last_error`` is NOT a :data:`~brutex.data.window_ledger.
    DEMOTED_SETTLE_REASONS` member (e.g. an expired-window
    "vendor confirmed no trades" stamp) NEVER refuses — disk stays the
    presence authority. ``load_failed`` is surfaced verbatim ⇒ the fail-CLOSED
    ``LEDGER_UNREADABLE`` refusal. Pure, O(flags).

    Parameters
    ----------
    scan
        :func:`~brutex.data.session_coverage.scan_ledger_quarantine` output.

    Returns
    -------
    QuarantineReport
        The bimodal split + the fail-closed ledger verdict.
    """
    demoted: list[LedgerFlag] = []
    accepted: list[LedgerFlag] = []
    for flag in scan.flags:
        if flag.last_error not in DEMOTED_SETTLE_REASONS:
            continue  # empty-settle / expired-window stamps never refuse
        converted = LedgerFlag(
            security_id=flag.security_id,
            interval=flag.interval,
            window_start=flag.window_start,
            window_end=flag.window_end,
            status=flag.status,
            last_error=flag.last_error,
        )
        if flag.status == FAILED:
            demoted.append(converted)
        elif flag.status == SKIPPED:
            accepted.append(converted)
        # Any other status is defensive dead weight: the scanner filters to
        # {FAILED, SKIPPED}; an unexpected status can never silently refuse.
    return QuarantineReport(
        demoted_open=tuple(demoted),
        accepted_gaps=tuple(accepted),
        ledger_load_failed=scan.load_failed,
    )


@dataclass(frozen=True, slots=True)
class ExclusionReport:
    """The sweep-vs-pull exclusion probe verdict — lock wins.

    Attributes
    ----------
    pull_active
        A real pull holds the lake's pull single-flight lock
        (``pull_in_progress`` — write-free probe).
    derive_active
        A prebuild/derive holds the lake's derive lock
        (``derive_in_progress`` — write-free probe).
    """

    pull_active: bool
    derive_active: bool

    @property
    def ok(self) -> bool:
        """``True`` iff neither a pull nor a prebuild holds the lake."""
        return not (self.pull_active or self.derive_active)

    def refusal(self) -> str:
        """The exclusion refusal — names the holder(s) + the wait remedy; ``""`` when ok."""
        if self.ok:
            return ""
        if self.pull_active and self.derive_active:
            head = "a pull and a prebuild are running"
        elif self.pull_active:
            head = "a pull is running"
        else:
            head = "a prebuild is running"
        return (
            f"{head} for this lake — lock wins; wait for it to finish "
            "(watch /universe), then re-run the sweep"
        )
