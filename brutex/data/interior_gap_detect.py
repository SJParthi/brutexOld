"""Detect a SUCCESS window with a multi-session INTERIOR hole bookended by real bars.

DEC-INTERIOR-GAP-DETECT-001 (MIGRATION.md #1027). The window classifier
(:func:`brutex.data.window_status.classify_window_status`) returns ``SUCCESS`` the
moment the vendor returns ANY bar (``raw_bars > 0``) — with **no continuity
check** — and the unified ledger then treats ``SUCCESS`` as terminal/skippable
(:data:`brutex.data.window_ledger._SKIPPABLE`). So a window whose bars run from
the start boundary, then DROP OUT for several whole trading sessions in the
MIDDLE, then resume to the end boundary — a multi-session interior hole bookended
by real data — is stamped ``SUCCESS``, becomes skippable, and the interior hole is
**never re-pulled**. That is a real data-completeness blind spot, distinct from
the TRAILING truncation the partial-window detector
(:mod:`brutex.data.partial_window_detect`) watches.

This module is the pure, false-positive-safe CLASSIFIER for an interior gap. It is
the COMPLETENESS twin of :mod:`brutex.data.partial_window_detect`:

* the partial-window detector watches the window's **trailing** edge (does the last
  bar reach the expected end?);
* this detector watches the window's **interior** (is there a long contiguous run
  of trading sessions with NO bars, bracketed by sessions that DO have bars?).

False-positive safety (the crux — read this before touching the threshold)
--------------------------------------------------------------------------
A naive "expected sessions vs covered sessions" check WOULD false-flag a
legitimately low-liquidity window (an illiquid contract that simply does not print
every session) and could imply a giant wasteful re-pull. We DO NOT do that. The
flag fires ONLY on a strong **CONTIGUOUS INTERIOR HOLE** signal, never on
scattered sparsity:

* The signal is a single run of ``> threshold`` CONSECUTIVE trading sessions that
  carry ZERO bars, bracketed on BOTH sides by sessions that DO carry bars (so the
  hole is genuinely INTERIOR, not a leading/trailing edge). A window whose bars are
  merely thin but spread across the span (no long contiguous hole) is NOT flagged
  (sparse-but-continuous is healthy).
* The hole must exceed a conservative, env-tunable tolerance of *trading sessions*
  (:data:`INTERIOR_GAP_TOLERANCE_SESSIONS`, env
  ``BRUTEX_INTERIOR_GAP_TOLERANCE_SESSIONS``, default 5). A one- or two-session
  blip (a vendor publish-lag day, a single illiquid session) never trips it.
* Explicitly EXCLUDED (each returns :data:`NO_INTERIOR_GAP`):
  - a window with FEWER THAN TWO distinct bar-sessions (no interior to bracket);
  - an OUT-OF-CALENDAR-COVERAGE window — when ANY covered bar-session falls in a
    year OUTSIDE the NSE holiday list's covered span
    (:func:`brutex.calendars.nse.is_holiday_data_available`). Outside that span
    :func:`~brutex.calendars.nse.is_trading_day` would silently count a weekday
    holiday as a session, inflating the hole into a spurious flag — so we honestly
    abstain (Golden Rule #1, no guess).

Everything here is pure (no I/O, no network), O(sessions in the bracketed span) per
window, so it is unit-testable offline and safe to fire best-effort from the pull
spine. The trading-session arithmetic reuses the COMMON NSE calendar
(:func:`brutex.calendars.nse.is_trading_day`), never a re-implemented weekday rule
(CLAUDE.md §11 COMMON).
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from brutex.calendars.nse import IST, is_holiday_data_available, is_trading_day

__all__ = [
    "INTERIOR_GAP",
    "INTERIOR_GAP_TOLERANCE_SESSIONS",
    "INTERIOR_GAP_WINDOW_ERROR_EVENT",
    "INTERIOR_GAP_WINDOW_EVENT",
    "NO_INTERIOR_GAP",
    "InteriorGapVerdict",
    "classify_interior_gap",
    "classify_interior_gap_dates",
    "observe_interior_gap",
]

#: Verdict: a long contiguous run of trading sessions with NO bars sits in the
#: INTERIOR of the window, bracketed by sessions that DO have bars — a suspected
#: interior completeness hole worth a re-pull.
INTERIOR_GAP: str = "INTERIOR_GAP"
#: Verdict: the window is continuous (no long interior hole), too short to have an
#: interior, or out of calendar coverage — NOT an interior gap. The healthy state.
NO_INTERIOR_GAP: str = "NO_INTERIOR_GAP"

#: An interior hole needs a bar-session on BOTH sides to bracket it, so a window covering
#: fewer than this many distinct trading sessions has no interior to be "gappy" — it is
#: excluded as ``too_few_sessions`` before any hole arithmetic runs.
_MIN_BRACKETING_SESSIONS: int = 2

#: Default interior-hole tolerance, in TRADING SESSIONS. A contiguous run of
#: bar-less trading sessions STRICTLY MORE than this many — bracketed on both sides
#: by sessions that have bars — is flagged. Conservative on purpose: a one- or
#: two-session blip (a vendor publish-lag day, a single illiquid session) must never
#: trip the flag — only a material multi-session hole does. Env-tunable via
#: ``BRUTEX_INTERIOR_GAP_TOLERANCE_SESSIONS`` (a non-positive / malformed value falls
#: back to this default — the diagnostic never crashes a pull).
INTERIOR_GAP_TOLERANCE_SESSIONS: int = 5


@dataclass(frozen=True, slots=True)
class InteriorGapVerdict:
    """The immutable result of classifying one window for an interior multi-session hole.

    Attributes
    ----------
    verdict
        :data:`INTERIOR_GAP` or :data:`NO_INTERIOR_GAP`.
    first_bar_date
        The IST trading date of the window's FIRST stored bar, or ``None`` when the
        window has no (or too few) bar-sessions to bracket an interior.
    last_bar_date
        The IST trading date of the window's LAST stored bar, or ``None`` (see above).
    largest_gap_sessions
        The longest contiguous run of bar-less trading sessions found in the
        INTERIOR of ``[first_bar_date, last_bar_date]`` (``0`` when continuous or not
        applicable). Always ``>= 0``.
    reason
        A short, non-secret machine tag for WHY this verdict was reached
        (``"interior_gap"`` / ``"continuous"`` / ``"too_few_sessions"`` /
        ``"out_of_calendar_coverage"``), so the WARNING + tests can assert the path
        without parsing prose.
    """

    verdict: str
    first_bar_date: date | None
    last_bar_date: date | None
    largest_gap_sessions: int
    reason: str

    def is_interior_gap(self) -> bool:
        """True iff this window tripped the interior-hole flag (else the emit stays silent)."""
        return self.verdict == INTERIOR_GAP


def _tunable_sessions(env_var: str, default: int) -> int:
    """Read a positive-int trading-session tunable from *env_var*, else *default*.

    A missing, empty, non-integer, or non-positive value falls back to *default* —
    the diagnostic must NEVER crash a pull over a malformed env var (Golden Rule
    #10). Pure modulo the one ``os.environ`` read.
    """
    raw = os.environ.get(env_var)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _largest_interior_session_gap(covered: Sequence[date]) -> int:
    """Longest contiguous run of bar-less TRADING sessions strictly between *covered* dates.

    *covered* is the sorted, de-duplicated set of IST trading dates that carry bars.
    Walks the calendar from the FIRST covered date to the LAST and counts the longest
    maximal run of trading sessions that have NO bar — only sessions BETWEEN the first
    and last covered date are considered (the interior), so a leading/trailing edge can
    never contribute. O(trading days in the bracketed span); the span is bounded by one
    contract window. Returns ``0`` when fewer than two covered sessions exist.
    """
    if len(covered) < _MIN_BRACKETING_SESSIONS:
        return 0
    have = set(covered)
    largest = 0
    run = 0
    cursor = covered[0] + timedelta(days=1)
    last = covered[-1]
    while cursor < last:
        if is_trading_day(cursor):
            if cursor in have:
                run = 0
            else:
                run += 1
                largest = max(largest, run)
        cursor += timedelta(days=1)
    return largest


def classify_interior_gap_dates(
    *,
    covered_dates: Iterable[date],
    tolerance_sessions: int | None = None,
) -> InteriorGapVerdict:
    """Classify a set of covered IST dates as :data:`INTERIOR_GAP` or :data:`NO_INTERIOR_GAP`.

    The DATE-SET core of the pure, false-positive-safe interior-hole classifier
    (see the module docstring) — the ONE source of interior-hole truth shared by
    the fetch-time :func:`classify_interior_gap` / :func:`observe_interior_gap`
    wiring AND the cold-start ledger seed's density cross-check
    (:func:`~brutex.data.window_ledger_store.seed_enclosed_windows`,
    DEC-COLDSTART-SEED-INTERIOR-GAP-001), so the two can never drift (CLAUDE.md
    §11 COMMON). It NEVER changes any stored status — it only OBSERVES.

    Parameters
    ----------
    covered_dates
        The IST trading dates that carry bars (any order, duplicates fine —
        internally de-duplicated + sorted). Only the SET of distinct covered
        dates matters, so intraday density never affects the verdict.
    tolerance_sessions
        Override the interior-hole tolerance (trading sessions). ``None`` reads
        :data:`INTERIOR_GAP_TOLERANCE_SESSIONS` (env-tunable).

    Returns
    -------
    InteriorGapVerdict
        The immutable verdict + the facts that produced it.
    """
    tolerance = (
        tolerance_sessions
        if tolerance_sessions is not None
        else _tunable_sessions(
            "BRUTEX_INTERIOR_GAP_TOLERANCE_SESSIONS", INTERIOR_GAP_TOLERANCE_SESSIONS
        )
    )

    covered = sorted(set(covered_dates))

    # TOO-FEW-SESSIONS exclusion: an interior hole needs a bar-session on BOTH sides,
    # so a window covering fewer than two distinct sessions has no interior to bracket.
    if len(covered) < _MIN_BRACKETING_SESSIONS:
        return InteriorGapVerdict(
            verdict=NO_INTERIOR_GAP,
            first_bar_date=covered[0] if covered else None,
            last_bar_date=covered[-1] if covered else None,
            largest_gap_sessions=0,
            reason="too_few_sessions",
        )

    # OUT-OF-CALENDAR-COVERAGE exclusion: the NSE holiday list only covers a bounded
    # span; outside it ``is_trading_day`` would silently count a weekday holiday as a
    # session, inflating the hole into a SPURIOUS flag. When ANY covered session falls
    # in an uncovered year we honestly ABSTAIN (Golden Rule #1 — surface, never guess).
    if not (is_holiday_data_available(covered[0]) and is_holiday_data_available(covered[-1])):
        return InteriorGapVerdict(
            verdict=NO_INTERIOR_GAP,
            first_bar_date=covered[0],
            last_bar_date=covered[-1],
            largest_gap_sessions=0,
            reason="out_of_calendar_coverage",
        )

    largest = _largest_interior_session_gap(covered)
    if largest > tolerance:
        return InteriorGapVerdict(
            verdict=INTERIOR_GAP,
            first_bar_date=covered[0],
            last_bar_date=covered[-1],
            largest_gap_sessions=largest,
            reason="interior_gap",
        )
    return InteriorGapVerdict(
        verdict=NO_INTERIOR_GAP,
        first_bar_date=covered[0],
        last_bar_date=covered[-1],
        largest_gap_sessions=largest,
        reason="continuous",
    )


def classify_interior_gap(
    *,
    bar_timestamps: Sequence[datetime],
    tolerance_sessions: int | None = None,
) -> InteriorGapVerdict:
    """Classify one SUCCESS window as :data:`INTERIOR_GAP` or :data:`NO_INTERIOR_GAP`.

    The pure, false-positive-safe interior-hole classifier (see the module docstring).
    A thin timestamp→IST-date delegator over the COMMON date-set core
    (:func:`classify_interior_gap_dates` — extracted for the cold-start ledger
    seed, DEC-COLDSTART-SEED-INTERIOR-GAP-001; every existing caller is
    byte-identical). It NEVER changes the window's stored status — it only
    OBSERVES.

    Parameters
    ----------
    bar_timestamps
        UTC-aware timestamps of the window's stored bars (any order, any density).
        Each is mapped to its IST trading date internally (the bars are stored UTC but
        trade on the IST session grid, CLAUDE.md §6); only the SET of distinct covered
        dates matters, so intraday density never affects the verdict.
    tolerance_sessions
        Override the interior-hole tolerance (trading sessions). ``None`` reads
        :data:`INTERIOR_GAP_TOLERANCE_SESSIONS` (env-tunable).

    Returns
    -------
    InteriorGapVerdict
        The immutable verdict + the facts that produced it.
    """
    return classify_interior_gap_dates(
        covered_dates=(ts.astimezone(IST).date() for ts in bar_timestamps),
        tolerance_sessions=tolerance_sessions,
    )


#: The structlog event a window with a confirmed INTERIOR multi-session hole emits
#: (DEC-INTERIOR-GAP-DETECT-001). ONE WARNING for human triage; the SAME verdict also
#: DEMOTES the window to a re-pullable ledger status at the wiring (the demotion is the
#: caller's, not this helper's). Spot + futures only (CLAUDE.md §11 COMMON).
INTERIOR_GAP_WINDOW_EVENT = "window.interior_gap"
#: Emitted if the observe-only scan itself raises — it NEVER breaks the pull.
INTERIOR_GAP_WINDOW_ERROR_EVENT = "window.interior_gap_error"


def observe_interior_gap(
    log: object,
    *,
    bar_timestamps: Sequence[datetime],
    symbol: str,
    interval: str,
    pull_id: str | None,
) -> InteriorGapVerdict | None:
    """WARN if a SUCCESS window has a confirmed interior multi-session hole; return the verdict.

    The ONE shared wiring (CLAUDE.md §11 COMMON — no copy): runs
    :func:`classify_interior_gap` over the window's SESSION-FILTERED bar timestamps and
    emits ONE :data:`INTERIOR_GAP_WINDOW_EVENT` WARNING when (and only when) a confirmed
    interior hole is present. Fully wrapped: ANY failure of the diagnostic is swallowed +
    logged (:data:`INTERIOR_GAP_WINDOW_ERROR_EVENT`), so it can NEVER block / slow / crash
    the pull.

    Returns the computed :class:`InteriorGapVerdict` (so the caller may DEMOTE a gappy
    SUCCESS window to a re-pullable ledger status — DEC-INTERIOR-GAP-DETECT-001), or
    ``None`` when the guarded scan itself raised.

    *log* is the caller's structlog logger (duck-typed for ``.warning(event, **fields)``).
    The caller owns the SCOPE gate (spot + futures only — options are excluded at the
    wiring; see the module docstring).
    """
    try:
        verdict = classify_interior_gap(bar_timestamps=bar_timestamps)
        if verdict.is_interior_gap():
            log.warning(  # type: ignore[attr-defined]
                INTERIOR_GAP_WINDOW_EVENT,
                pull_id=pull_id,
                symbol=repr(symbol),
                interval=interval,
                first_bar_date=(
                    verdict.first_bar_date.isoformat() if verdict.first_bar_date else None
                ),
                last_bar_date=(
                    verdict.last_bar_date.isoformat() if verdict.last_bar_date else None
                ),
                largest_gap_sessions=verdict.largest_gap_sessions,
            )
        return verdict
    except Exception as exc:  # observe-only diagnostic must never break the pull
        log.warning(  # type: ignore[attr-defined]
            INTERIOR_GAP_WINDOW_ERROR_EVENT, pull_id=pull_id, error=str(exc)
        )
        return None
