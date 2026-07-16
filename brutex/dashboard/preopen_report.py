"""Pre-open report — the NSE-style ``SYMBOL · PREV. CLOSE · IEP · CHNG · %CHNG`` export.

A **standalone, lake-derived** report (DEC-PREOPEN-REPORT-001, fully 1-day-sourced
per DEC-PREOPEN-REPORT-1DAY-001): for each requested spot symbol and each trading
day in a ``[from, to]`` range it reconstructs the NSE pre-open columns straight
from the **1-day** lake — and the numbers match NSE to the paisa (verified
against the operator's pre-open CSV: COALINDIA 2026-05-27 IEP 429.00, PREV. CLOSE
458.15, %CHNG −6.36):

* **PREV. CLOSE** — the prior trading day's **1-day official close** (= the NSE
  settlement / "previous close").
* **IEP** — the day's **1-day official open**. On NSE the pre-open auction's
  indicative equilibrium price *becomes* the day's official open, so the 1-day
  bar's ``open`` IS the IEP — on the SAME 1-day basis as PREV. CLOSE.
* **CHNG** = ``IEP − PREV. CLOSE``; **%CHNG** = ``CHNG / PREV. CLOSE × 100``.

Both columns come from the **1-day** partition ALONE — the report NEVER reads the
1-minute series (DEC-PREOPEN-REPORT-1DAY-001). Mixing a 1-minute open with a
1-day close is what made adjustment-mismatched symbols (e.g. TMPV — raw intraday
~186.6 vs demerger-adjusted daily ~112.9) report absurd gaps; staying on one
basis keeps every symbol precise and matches NSE, which itself compares against
1-day data. The brute-force engine is unaffected — it uses the prior day's last
1-minute close (DEC-SPOT-PREVCLOSE-1MIN-001); the report and engine are distinct.

The Excel is written by a tiny, dependency-free **stdlib OOXML writer**
(:func:`render_xlsx`) so there is no new third-party dependency to vet through the
licence/lockfile gates. The workbook **BODY** is byte-stable for the same data
(fixed internal zip metadata); the OUTER ``.zip`` member mtimes are the IST
**generation** time so a freshly-downloaded file reads "Date Modified = now"
(DEC-PREOPEN-XLSX-DATE-001, amended — the operator's "precise current IST
timestamp everywhere"). Per-row work is O(1); the per-symbol lake reads are one
columnar pass each (O(N) once), mirroring the ``/db`` viewer.
"""

# pyright: reportPrivateUsage=false
# ^ This module deliberately reuses db_view's shared lake helpers (_DAILY_INTERVAL,
#   _daily_close_by_date, _daily_open_by_date) — CLAUDE.md §11 COMMON (one source
#   of truth, no copy-paste).

from __future__ import annotations

import bisect
import io
import re
import zipfile
from collections.abc import Callable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from itertools import groupby
from pathlib import Path
from types import EllipsisType
from typing import Any, Final, Literal

import duckdb
import structlog

from brutex.calendars.nse import has_muhurat_session, previous_trading_session
from brutex.calendars.observed_sessions import load_observed_holidays
from brutex.dashboard.db_view import (
    _DAILY_INTERVAL,
    _INTRADAY_BASE_INTERVAL,
    _daily_close_by_date,
    _daily_open_by_date,
    _intraday_last_close_by_date,
    _intraday_open_by_date,
    list_candle_datasets,
)
from brutex.data.fno_candle_lake import FNO_CANDLE_LAKE_SEGMENT
from brutex.data.session import IST

#: Module logger — surfaces a per-symbol skip (DEC-PREOPEN-SPACE-SYMBOL-001) so a
#: degraded export is visible in ``/logs``, never silent.
_LOG = structlog.get_logger("brutex.dashboard.preopen_report")

#: The exact column order of the report (mirrors the NSE pre-open file + a DATE).
REPORT_COLUMNS: Final[tuple[str, ...]] = (
    "DATE",
    "SYMBOL",
    "PREV. CLOSE",
    "IEP",
    "CHNG",
    "%CHNG",
)

#: A fixed timestamp for the .xlsx INTERNAL OOXML parts (invisible to the operator)
#: so the same data always produces a byte-identical workbook BODY. The OUTER zip
#: member mtime is the IST generation clock (DEC-PREOPEN-XLSX-DATE-001, amended).
_ZIP_EPOCH: Final[tuple[int, int, int, int, int, int]] = (1980, 1, 1, 0, 0, 0)

#: Two-decimal display quantum (the report is a human-facing NSE-style sheet).
_2DP: Final[Decimal] = Decimal("0.01")

#: Cell-style indices into the ``xl/styles.xml`` ``<cellXfs>`` table
#: (see :data:`_STYLES`). 0 is the implicit default, so it is omitted from
#: the cell markup.
_STYLE_NORMAL: Final[int] = 0  # default font, General alignment
_STYLE_BOLD: Final[int] = 1  # bold font — the header row + the SYMBOL column
_STYLE_TITLE: Final[int] = 2  # bold + centred — the merged date-range title row
_STYLE_GREEN: Final[int] = 3  # solid green fill — a gainer (%CHNG > 0) plain cell
_STYLE_RED: Final[int] = 4  # solid red fill — a loser (%CHNG < 0) plain cell
_STYLE_GREEN_BOLD: Final[int] = 5  # green fill + bold — a gainer row's SYMBOL cell
_STYLE_RED_BOLD: Final[int] = 6  # red fill + bold — a loser row's SYMBOL cell

#: Per-column display widths (Excel "character" units), aligned 1:1 to
#: :data:`REPORT_COLUMNS`, so the sheet opens with precise, readable columns
#: instead of the cramped OOXML default width (operator: "make the spaces
#: precise"). DEC-PREOPEN-XLSX-STYLE-001.
_COL_WIDTHS: Final[tuple[float, ...]] = (12.0, 16.0, 13.0, 12.0, 11.0, 11.0)


@dataclass(frozen=True, slots=True)
class PreOpenRow:
    """One ``(symbol, trading-day)`` pre-open row — full precision until display."""

    day: date
    symbol: str
    prev_close: Decimal
    iep: Decimal
    chng: Decimal
    pct_chng: Decimal

    def display_cells(self) -> tuple[str, str, float, float, float, float]:
        """Return the row as display cells (money + %CHNG snapped to 2 decimals)."""
        return (
            self.day.isoformat(),
            self.symbol,
            float(_round2(self.prev_close)),
            float(_round2(self.iep)),
            float(_round2(self.chng)),
            float(_round2(self.pct_chng)),
        )


def _round2(value: Decimal) -> Decimal:
    """Snap *value* to 2 decimals, ROUND_HALF_UP (NSE-style display rounding)."""
    return value.quantize(_2DP, rounding=ROUND_HALF_UP)


#: The basis a pre-open report is computed on (DEC-PREOPEN-MINUTE-BASIS-001):
#: ``"day"`` (default — PREV. CLOSE + IEP from the **1-day** bar, the original
#: DEC-PREOPEN-REPORT-1DAY-001 behaviour) or ``"minute"`` (IEP = the **09:15:00
#: 1-minute** session-open OPEN; PREV. CLOSE = the **prior trading day's last
#: 1-minute close**, matching the engine's prev-close convention
#: DEC-SPOT-PREVCLOSE-1MIN-001). Each basis stays on ONE price scale (never mixed)
#: so an adjustment-mismatched symbol stays precise.
ReportBasis = Literal["day", "minute"]

#: The lake partition interval each basis reads from.
_BASIS_INTERVAL: Final[dict[str, str]] = {
    "day": _DAILY_INTERVAL,
    "minute": _INTRADAY_BASE_INTERVAL,
}

#: Every report basis, in picker order — the DYNAMIC iteration source for the
#: post-pull discovery warm (DEC-O1-CLAIMS-HONESTY-001): adding a basis to
#: :data:`_BASIS_INTERVAL` must extend this tuple too (test-pinned equal key
#: sets), so a new basis is auto-warmed, never silently cold.
REPORT_BASES: Final[tuple[ReportBasis, ...]] = ("day", "minute")

#: Segments EXCLUDED from report discovery (DEC-O1-CLAIMS-HONESTY-001): an F&O
#: contract partition is never a pre-open report symbol, so it is PRUNED at the
#: walk level (an excluded segment's tree is never descended —
#: DEC-COMPLETENESS-FNO-SKIP-001 machinery) instead of parse-then-discarded.
#: The ONE constant both the walk call and the pre-walked ``datasets`` screen
#: share (CLAUDE.md §11 COMMON).
REPORT_EXCLUDED_SEGMENTS: Final[frozenset[str]] = frozenset({FNO_CANDLE_LAKE_SEGMENT})

#: Cap on ``preopen.prev_close_noncontiguous`` WARNs emitted per report build
#: (DEC-PREOPEN-PREVDAY-COMMON-002) — deduped per ``(exchange, segment,
#: symbol)`` dataset triple (a bare-symbol key would silence a dual-listed
#: symbol's second exchange — the hostile-review fold); everything past the
#: cap rolls into ONE ``preopen.prev_close_noncontiguous_suppressed`` summary
#: (mirrors the /movers #1535 capped-warn precedent, state kept function-local).
_HOLE_WARN_CAP: Final[int] = 20

#: The reproduced shapes a VERIFIED prev-close hole takes — the machine
#: ``hole_shape`` field on the ``preopen.prev_close_noncontiguous`` WARN
#: (DEC-PREOPEN-PREVDAY-COMMON-002 hostile-review fold: the original one-line
#: detail unconditionally advised a re-pull, which is WRONG remediation for
#: most of the shapes; DEC-PREVSESSION-MUHURAT-COHERENCE-001 split the
#: calendar-confirmed Muhurat boundary — a HEALTHY lake — out of the
#: suspicious ``extra_nonsession_bar`` catch-all). See :func:`_hole_shape`.
_HoleShape = Literal[
    "muhurat_session_bar", "extra_nonsession_bar", "prebuild_lag", "missing_session"
]


def resolve_report_datasets(
    lake_root: Path,
    symbols: list[str] | None = None,
    *,
    basis: ReportBasis = "day",
    datasets: Sequence[Mapping[str, str]] | None = None,
) -> list[tuple[str, str, str]]:
    """Resolve the ``(exchange, segment, symbol)`` triples the report can build.

    A dataset qualifies when it has the partition the chosen *basis* reads
    (DEC-PREOPEN-MINUTE-BASIS-001): the ``1day`` partition for ``basis="day"``
    (the original DEC-PREOPEN-REPORT-1DAY-001 behaviour), else the ``1minute``
    partition for ``basis="minute"``. When *symbols* is given, the result is
    filtered to that set (case-sensitive on the lake symbol). Sorted by symbol
    for a stable picker / report order (ties broken by the deterministic
    4-key-sorted discovery order). An empty or missing lake yields ``[]``
    (the report degrades to a header-only sheet).

    DEC-O1-CLAIMS-HONESTY-001: discovery is FILTERED at the source —
    ``exclude_segments=REPORT_EXCLUDED_SEGMENTS`` prunes the F&O contract tree
    at the walk level (never descended; ~10⁵–10⁶ dirs on a materialised
    options lake) and ``intervals={required}`` bounds the listing to the one
    partition the basis reads. SANCTIONED behaviour change: an F&O contract
    that happens to hold the required interval is no longer offered as a
    report symbol (it never was one — a contract is not a spot underlying).
    NOTE: a filtered discovery deliberately never (re)builds the lake
    manifest (``db_view.list_candle_datasets`` unfiltered-only gate) — the
    unfiltered consumers (/db datasets, movers, the post-pull warm) remain
    the rebuilders.

    *datasets* is the pre-walked seam (the ``build_db_datasets_payload``
    pattern): the post-pull discovery warm passes its ONE shared unfiltered
    walk result so the preopen memos are seeded with zero extra walks; the
    in-loop screens below then produce a result byte-identical to the
    filtered-walk path (test-pinned).
    """
    required = _BASIS_INTERVAL[basis]
    wanted = set(symbols) if symbols else None
    source: Sequence[Mapping[str, str]] = (
        datasets
        if datasets is not None
        else list_candle_datasets(
            lake_root,
            exclude_segments=REPORT_EXCLUDED_SEGMENTS,
            intervals=frozenset({required}),
        )
    )
    resolved: dict[tuple[str, str, str], None] = {}
    for ds in source:
        if ds["segment"] in REPORT_EXCLUDED_SEGMENTS or ds["interval"] != required:
            continue
        if wanted is not None and ds["symbol"] not in wanted:
            continue
        resolved.setdefault((ds["exchange"], ds["segment"], ds["symbol"]), None)
    return sorted(resolved, key=lambda k: k[2])


def _previous_trading_day(day: date, *, observed: AbstractSet[date] = frozenset()) -> date | None:
    """The NSE trading session immediately before *day* (per the shared calendar).

    DEC-PREOPEN-PREVDAY-COMMON-002: a THIN delegator over the ONE COMMON
    :func:`brutex.calendars.nse.previous_trading_session` primitive (CLAUDE.md
    §11 COMMON — the #1535/#1539-named follow-up, now CLOSED; the private walk
    this module used to carry is DELETED). Name + signature are KEPT so every
    existing caller/test survives verbatim. *observed* is the REMOVE-ONLY
    observed-sessions overlay (DEC-CALENDAR-OBSERVED-SESSIONS-001): an
    observably-untraded day is stepped over exactly like a holiday; the default
    empty set is byte-identical back-compat (delegation-equivalence pinned
    against a verbatim copy of the old walk over the full covered span).

    ABSTAIN, never guess: when the back-walk crosses the published-holiday
    coverage floor the primitive emits ONE
    ``calendar.previous_session_coverage_exhausted`` warning (the COMMON event
    — the legacy ``calendar.coverage_exhausted`` twin this module emitted is
    RETIRED, a sanctioned observability rename) and returns ``None``; the
    callers treat ``None`` as prev-close-ABSENT so the row is honestly SKIPPED
    (the locked "correct-or-absent, never wrong" contract) — never a silent
    guess, never a crash/500.

    Honest limit (Golden Rule #10): a Diwali **Muhurat** date is classified as
    a holiday by the calendar, so it is a NON-session here. A symbol whose lake
    carries a Muhurat bar may therefore have the day-after-Diwali row
    conservatively SKIPPED by BOTH contiguity guards (the "correct-or-absent,
    never wrong" rule) — recorded in the decision ledger, not a silent wrong
    number.
    """
    return previous_trading_session(day, observed=observed)


def _daily_prev_close_contiguous(
    daily: dict[date, Decimal],
    daily_dates: list[date],
    day: date,
    *,
    observed: AbstractSet[date] = frozenset(),
    expected_session: date | None | EllipsisType = ...,
    on_hole: Callable[[date, date], None] | None = None,
) -> Decimal | None:
    """Daily-basis PREV. CLOSE that REFUSES to anchor across a MISSING daily bar.

    The NSE "previous close" for *day* is the **immediately-preceding trading
    session's** 1-day official close. For the daily basis the 1-day series IS the
    data, so it cannot be its own calendar (a missing bar would look like a
    holiday); instead the authoritative prior session is derived from the shared
    NSE trading calendar (:func:`_previous_trading_day`) and the anchor is valid
    ONLY when the lake actually holds that EXACT session's daily bar.

    This is the daily mirror of :func:`_minute_prev_close_contiguous`
    (DEC-PREOPEN-PREVCLOSE-CONTIGUITY-001, PR #484): the latent stale-jump bug in
    the old ``_prev_close_asof`` returned the most-recent AVAILABLE earlier close,
    which silently jumped across a MISSING daily bar onto a far-earlier session →
    a wrong %CHNG. Here:

    * ``idx == 0`` (no daily bar strictly before *day*) → ``None`` (first session).
    * The expected prior session (from the calendar) has its daily bar present in
      the lake — i.e. the latest available daily date strictly before *day* IS
      that exact session → return its close (``None`` if that close is zero, so a
      ``/0`` %CHNG can never form).
    * The expected prior session's daily bar is MISSING (the latest available
      earlier daily date is older than the calendar's prior session) → ``None`` so
      the caller SKIPS the row rather than anchoring on a stale far-earlier close
      (the operator's locked "correct-or-absent, never wrong" rule).

    A genuine weekend/holiday gap is NOT a hole: the calendar steps over it, so a
    contiguous series across a holiday still resolves to the real prior session.
    O(log N) per row via :func:`bisect.bisect_left` over *daily_dates* (the
    calendar walk is a bounded constant); a safety net, never a data fix — the
    1-day series is filled by re-pulling (DEC-DHAN-429-RETRY-001 #466) + the
    windows table (#465) + the auto-retry layer (DEC-INGEST-AUTO-RETRY-001).

    Seams (DEC-PREOPEN-PREVDAY-COMMON-002, shared with the minute mirror):
    *expected_session* is the Ellipsis-sentinel prior-session seam — the
    default ``...`` computes :func:`_previous_trading_day(day, observed=…)`
    internally (the historical behaviour), while a caller holding a memoised
    walk result passes it directly (``build_preopen_rows``'s per-report
    O(unique days) memo). *on_hole* is the observability seam: invoked as
    ``on_hole(expected, nearest_lake_session)`` EXACTLY when the anchor is
    refused because the nearest earlier daily bar is NOT the calendar's prior
    session (a lake hole) — never on the first-session, coverage-abstain, or
    zero-close refusals, and never when ``None``. The mismatch takes the three
    reproduced shapes :func:`_hole_shape` classifies for the WARN — an EXTRA
    calendar-non-session bar AFTER the prior session (``found > expected``, the
    Muhurat shape), a 1-day series LAGGING the minute series (prebuild lag), or
    a genuinely MISSING session — but on the daily basis the caller holds no
    minute evidence (this basis never reads the 1-minute series,
    DEC-PREOPEN-REPORT-1DAY-001), so its diagnostic is honestly TWO-WAY
    (extra-non-session-bar vs missing-session). The refusal VALUE contract
    is unchanged (payload byte-identical; its refusals already skip).
    """
    idx = bisect.bisect_left(daily_dates, day)
    if idx == 0:
        return None
    expected = (
        _previous_trading_day(day, observed=observed)
        if expected_session is ...
        else expected_session
    )
    # ``None`` = the calendar's coverage ran out while walking back (already
    # WARNed loudly by the COMMON primitive): the prior session cannot be
    # verified, so the anchor is honestly ABSENT and the row is skipped —
    # never a guess, and never an ``on_hole`` (there is no verified hole).
    if expected is None:
        return None
    if daily_dates[idx - 1] != expected:
        if on_hole is not None:
            on_hole(expected, daily_dates[idx - 1])
        return None
    close = daily[expected]
    if close == 0:
        return None
    return close


def _minute_prev_close_contiguous(
    minute_last: dict[date, Decimal],
    day: date,
    *,
    observed: AbstractSet[date] = frozenset(),
    expected_session: date | None | EllipsisType = ...,
) -> Decimal | None:
    """Minute-basis PREV. CLOSE anchored on the checked NSE calendar ALONE.

    The "previous close" for *day* is the **immediately preceding trading
    session's** last 1-minute close. The authoritative prior session is derived
    from the shared **NSE trading calendar** (:func:`_previous_trading_day`) —
    the SAME resolver the daily basis uses (:func:`_daily_prev_close_contiguous`)
    — and the anchor is served iff that EXACT session has 1-minute data in
    *minute_last*. The symbol's 1-day series is NOT consulted
    (DEC-STALENESS-CACHE-TRUTH-001, MIGRATION.md #1660 — supersedes-in-part the
    #1620/#1621 shape AND the minute-basis arm of the richer
    DEC-PREOPEN-PREVDAY-COMMON-002 #1649a confirmation guard folded at the
    2026-07-10 merge-reconcile, both of which required the possibly-stale 1-day
    series to CONFIRM the calendar's session): with the checked-calendar walk
    threaded, the 1-day intermediary could only ADD over-refusals (a 1-day-holed
    but 1-minute-complete lake skipped a row whose correct anchor existed in
    ``minute_last[expected]``) and could not catch anything the
    calendar + minute check misses. The previous-AVAILABLE fallback for a
    symbol with no 1-day partition is likewise REMOVED — that arm bridged a
    genuine 1-minute session hole onto a stale earlier close with NO calendar
    check (the captured 23.81%-where-truth-is-4.00% mis-anchor class), and a
    CLI-pulled / never-prebuilt lake put EVERY symbol on it. Because no 1-day
    comparison remains, the #1649a ``on_hole`` seam does not exist on THIS
    guard (the hole-WARN channel stays live on the daily mirror, which keeps
    its 1-day evidence); the #1649a *expected_session* memo seam IS kept —
    ``...`` computes the walk internally, a memo-holding caller passes it, so
    the calendar back-walk runs once per unique report day.

    Refusal ladder (each returns ``None`` so the caller SKIPS the row — the
    locked "correct-or-absent, never wrong" contract):

    * the calendar's prior session is unverifiable past the coverage floor
      (:func:`_previous_trading_day` LOUD-abstains → ``None``);
    * the prior session's 1-minute window is MISSING (``minute_last`` has no
      entry for it — the fake-mover bug where an un-fetched prior-day window
      produced a wildly wrong %CHNG, e.g. −7.88% on JUBLINGREA;
      DEC-PREOPEN-PREVCLOSE-CONTIGUITY-001), OR its close is zero.

    A genuine weekend/holiday gap is NOT a hole: the calendar steps over it, so
    a contiguous series across a holiday still resolves to the real prior
    session. The calendar walk is a bounded constant per row; the map lookup is
    O(1). Honest limit (Golden Rule #10): a PARTIAL prior-session 1-minute
    window (truncated intra-day) still serves its mid-session last close — real
    data at a real checked-calendar session, but not necessarily the 15:29
    close; intra-session completeness is not checked here (recorded in the
    ledger entry, note-only follow-up).
    """
    expected = (
        _previous_trading_day(day, observed=observed)
        if expected_session is ...
        else expected_session
    )
    # ``None`` = the calendar's coverage ran out while walking back (already
    # WARNed loudly by the helper): the prior session cannot be verified, so the
    # anchor is honestly ABSENT and the row is skipped — never a guess.
    if expected is None:
        return None
    close = minute_last.get(expected)
    if close is None or close == 0:
        return None
    return close


def _hole_shape(
    expected: date, found: date, minute_evidence: Mapping[date, Decimal] | None
) -> _HoleShape:
    """Classify a VERIFIED prev-close hole into its reproduced remediation shape.

    DIAGNOSTIC ONLY (the hostile-review fold on DEC-PREOPEN-PREVDAY-COMMON-002):
    the refusal itself stands in EVERY shape — the correct-or-absent contract is
    untouched, and in particular the guard is never re-anchored on
    ``minute_evidence[expected]``: 1-minute PRESENCE for the expected session
    does NOT prove session COMPLETENESS (a partially-pulled day's last minute
    close would be a fabricated PREV. CLOSE). The shape only aims the
    remediation text in the WARN's ``detail``:

    * ``found > expected`` AND :func:`brutex.calendars.nse.has_muhurat_session`
      confirms *found* → ``"muhurat_session_bar"`` — the annual day-after-
      Diwali boundary (DEC-PREVSESSION-MUHURAT-COHERENCE-001): the ingest
      legitimately keeps Muhurat bars (DEC-INGEST-BOUNDARY-INTEGRITY-001) and
      the prebuild derives their 1-day bar, so the lake is HEALTHY and no
      action is needed. The pre-open report is an ANCHOR-law surface, so the
      row still stays conservatively skipped — it mirrors the engine's
      ``stage1a.muhurat_prev_day_anchor_skip`` (#1616), while ``/movers``
      serves the Muhurat close under the DISPLAY baseline law (stamped
      ``prev_close_muhurat``) — the recorded two-law design.
    * ``found > expected`` on a NON-Muhurat date → ``"extra_nonsession_bar"``
      — an UNEXPECTED calendar-non-session 1-day bar sits AFTER the
      calendar's prior session: honestly SUSPICIOUS (a stray write, a corrupt
      partition, or a calendar gap) — inspect the unexpected bar; a re-pull
      alone cannot explain it away.
    * ``found < expected`` AND the expected session IS present in the symbol's
      minute series → ``"prebuild_lag"`` — the 1-day series LAGS the 1-minute
      series (e.g. a CLI-pulled lake awaiting the pipeline prebuild,
      DEC-PIPELINE-AUTOPREBUILD-001). Remediation: run the pipeline prebuild,
      not a re-pull.
    * ``found < expected`` AND the expected session is absent from the minute
      evidence too → ``"missing_session"`` — the genuine hole; remediation is
      re-pulling the missing session.

    *minute_evidence* is ``None`` on the DAILY basis (the day basis never reads
    the 1-minute series, DEC-PREOPEN-REPORT-1DAY-001), so the daily
    ``found < expected`` diagnostic is honestly reduced to
    ``missing_session`` — a daily-basis prebuild lag reads ``missing_session``
    (an accepted, documented limit; the minute basis, which holds the
    evidence, names it). The ``found > expected`` split needs no minute
    evidence, so both bases classify it identically.
    """
    if found > expected:
        if has_muhurat_session(found):
            return "muhurat_session_bar"
        return "extra_nonsession_bar"
    if minute_evidence is not None and expected in minute_evidence:
        return "prebuild_lag"
    return "missing_session"


def _hole_detail(shape: _HoleShape, expected: date, found: date) -> str:
    """The honest, shape-matched remediation text for the non-contiguity WARN.

    One sentence per :func:`_hole_shape` verdict — the ``missing_session`` text
    is the original (pre-fold) detail verbatim; the other shapes name the
    evidence and deliberately do NOT advise a re-pull (a re-pull cannot fix
    any of them). No served value changes in any shape — the row stays
    skipped. DEC-PREVSESSION-MUHURAT-COHERENCE-001 split the calendar-
    confirmed Muhurat boundary (healthy — "no action needed") out of
    ``extra_nonsession_bar``, whose text no longer GUESSES Muhurat and is now
    honestly suspicious ("inspect") instead of falsely reassuring.
    """
    if shape == "muhurat_session_bar":
        return (
            f"row conservatively skipped — the prior real session was the "
            f"{found.isoformat()} Diwali Muhurat session (calendar-confirmed); the "
            f"calendar's prior REGULAR session is {expected.isoformat()}; the lake is "
            "HEALTHY, no action needed — the pre-open anchor law refuses a Muhurat "
            "anchor (mirrors stage1a.muhurat_prev_day_anchor_skip; /movers serves the "
            "Muhurat close under the display law, stamped prev_close_muhurat)"
        )
    if shape == "extra_nonsession_bar":
        return (
            f"row conservatively skipped — an UNEXPECTED calendar-non-session 1-day bar "
            f"({found.isoformat()}, not a known Muhurat session) sits after the prior "
            f"NSE session {expected.isoformat()}; inspect the unexpected bar (a stray "
            "write / a corrupt partition / a calendar gap) — the day-after row stays "
            "absent by the correct-or-absent rule"
        )
    if shape == "prebuild_lag":
        return (
            f"row skipped — the 1-day series lags the minute series (the prior NSE session "
            f"{expected.isoformat()} has 1-minute bars but no 1-day bar); run the pipeline "
            "prebuild to derive the 1-day partition"
        )
    return "row skipped — prior NSE session missing from the lake; re-pull the missing session"


def build_preopen_rows(
    conn: Any,
    lake_root: Path,
    *,
    datasets: list[tuple[str, str, str]],
    start: date,
    end: date,
    basis: ReportBasis = "day",
) -> list[PreOpenRow]:
    """Build the pre-open rows for *datasets* over ``[start, end]`` (inclusive).

    Each dataset is an ``(exchange, segment, symbol)`` spot triple. The price
    *basis* (DEC-PREOPEN-MINUTE-BASIS-001) selects ONE consistent scale:

    * ``"day"`` (default, DEC-PREOPEN-REPORT-1DAY-001) — IEP = the day's **1-day
      open**; PREV. CLOSE = the prior **NSE-trading-session's** 1-day close
      (:func:`_daily_prev_close_contiguous`, which REFUSES to anchor across a
      MISSING daily bar — DEC-PREOPEN-PREVCLOSE-CONTIGUITY-DAILY-001 — and skips
      the row instead of jumping onto a stale far-earlier close).
    * ``"minute"`` (DEC-PREOPEN-MINUTE-BASIS-001) — IEP = the **09:15:00 1-minute
      session-open OPEN** (:func:`_intraday_open_by_date`); PREV. CLOSE = the
      **prior NSE-trading-session's last 1-minute close**
      (:func:`_minute_prev_close_contiguous` over
      :func:`_intraday_last_close_by_date` — the checked-calendar anchor,
      DEC-STALENESS-CACHE-TRUTH-001; a missing prior-session 1-minute window
      SKIPS the row, never a bridged stale close), matching the engine's own
      prev-close convention (DEC-SPOT-PREVCLOSE-1MIN-001).

    Both columns ALWAYS come from the same basis (never mixed), so an
    adjustment-mismatched symbol (e.g. TMPV) stays precise. A day is emitted only
    when its open exists, a non-zero prior close exists, and the day is in range.
    Per-symbol reads are wrapped so one un-queryable symbol can NEVER 500 the
    export (DEC-PREOPEN-SPACE-SYMBOL-001). Rows sorted by ``(day, symbol)``.

    DEC-PREOPEN-PREVDAY-COMMON-002: BOTH calendar-verified bases (the daily
    guard AND the minute guard — the minute basis was calendar-blind before)
    share ONE function-local prior-session memo, so the calendar back-walk runs
    O(unique report days) per build (never O(rows); no globals — NOT an
    O(1)-total claim). A verified hole refusal rides the existing skip
    ``continue`` — the row is honestly ABSENT, never a fabricated %CHNG, never
    a raise/500 — and, on the DAILY basis, is surfaced as ONE
    ``preopen.prev_close_noncontiguous`` WARN per ``(exchange, segment,
    symbol)`` dataset triple (capped at :data:`_HOLE_WARN_CAP` per build, with
    one ``preopen.prev_close_noncontiguous_suppressed`` summary past the cap).
    DEC-STALENESS-CACHE-TRUTH-001 merge-reconcile (2026-07-10): the MINUTE
    basis anchors on the checked calendar ALONE — it no longer reads the 1-day
    series at all, so there is no 1-day-vs-calendar comparison to classify and
    the hole-WARN channel fires on the daily basis only (a minute-basis
    missing prior-session window stays the byte-preserved silent skip).
    The WARN carries a machine ``hole_shape`` + a shape-matched ``detail``
    (:func:`_hole_shape` / :func:`_hole_detail` — the hostile-review fold +
    the DEC-PREVSESSION-MUHURAT-COHERENCE-001 split): ``muhurat_session_bar``
    (the calendar-confirmed day-after-Diwali boundary — a HEALTHY lake, no
    action needed; the anchor-law row still stays absent, mirroring the
    engine's #1616 Muhurat skip while /movers serves the close under the
    display law), ``extra_nonsession_bar`` (an UNEXPECTED non-Muhurat
    non-session 1-day bar after the prior session — honestly suspicious,
    inspect it), ``prebuild_lag`` (the 1-day series lags the minute series;
    remediation is the pipeline prebuild — the minute basis passes its
    ``minute_last`` as DIAGNOSTIC evidence only, the anchor is never taken
    from it — production-UNREACHABLE via this caller under the merged
    calendar-alone minute rule, kept for the pure classifier's totality and
    directly unit-tested), or ``missing_session`` (the genuine hole;
    remediation stays "re-pull the missing session"). The daily basis passes
    no minute evidence (it never reads the 1-minute series), so its
    ``found < expected`` diagnostic reads ``missing_session``.
    """
    rows: list[PreOpenRow] = []
    # Observed-sessions overlay (DEC-CALENDAR-OBSERVED-SESSIONS-001): loaded ONCE per
    # report and threaded into BOTH bases' prior-session walk, so an observably
    # untraded day (an overlay entry) is stepped over like a holiday instead of the
    # row being skipped for a "missing" bar. Remove-only; the loader never
    # raises (missing/corrupt sidecar -> empty overlay = the pre-change behaviour).
    observed = load_observed_holidays(lake_root)
    # Prior-session memo: ONE calendar back-walk per unique report day, shared
    # by BOTH bases across every symbol (function-local, never a global).
    expected_by_day: dict[date, date | None] = {}

    def _expected_session(day: date) -> date | None:
        if day not in expected_by_day:
            expected_by_day[day] = _previous_trading_day(day, observed=observed)
        return expected_by_day[day]

    # Non-contiguity WARN state: dedupe per (exchange, segment, symbol) dataset
    # triple (a bare-symbol key would silence a dual-listed symbol's second
    # exchange), cap the emitted warns per build, and roll everything past the
    # cap into ONE summary event.
    hole_keys: set[tuple[str, str, str]] = set()
    holes_suppressed = 0

    def _warn_hole(
        symbol: str,
        exchange: str,
        segment: str,
        day: date,
        expected: date,
        found: date,
        minute_evidence: Mapping[date, Decimal] | None,
    ) -> None:
        nonlocal holes_suppressed
        key = (exchange, segment, symbol)
        if key in hole_keys:
            return
        hole_keys.add(key)
        if len(hole_keys) > _HOLE_WARN_CAP:
            holes_suppressed += 1
            return
        shape = _hole_shape(expected, found, minute_evidence)
        _LOG.warning(
            "preopen.prev_close_noncontiguous",
            basis=basis,
            symbol=symbol,
            exchange=exchange,
            segment=segment,
            day=day.isoformat(),
            expected_session=expected.isoformat(),
            nearest_lake_session=found.isoformat(),
            hole_shape=shape,
            detail=_hole_detail(shape, expected, found),
        )

    for exchange, segment, symbol in datasets:
        # Resilience (DEC-PREOPEN-SPACE-SYMBOL-001): one un-queryable symbol must
        # NEVER 500 the whole multi-symbol export — a ``ValueError`` (the candle-
        # symbol validator) or a ``duckdb.Error`` (an unreadable / malformed
        # Parquet for one symbol) is logged + skipped; every other symbol exports.
        try:
            daily: dict[date, Decimal] = {}
            daily_dates: list[date] = []
            minute_last: dict[date, Decimal] = {}
            # The IEP (open) reads are SPAN-BOUNDED (DEC-DB-LATENCY-HARDENING-001):
            # only in-range days of `opens` are ever consumed (the loop below
            # `continue`s past out-of-range days), so binding only the report
            # span's month files is byte-identical — the profiling evidence
            # measured the unbounded minute basis binding EVERY 1-minute month
            # file of every symbol (616 files/click at 103 symbols). The CLOSE
            # maps deliberately stay whole-partition: the contiguity guards'
            # `found = nearest_lake_session` hole diagnostics + the calendar
            # back-walk depth need the unbounded view (a bounded close map could
            # silently change a WARN's evidence or an anchor refusal).
            if basis == "minute":
                opens = _intraday_open_by_date(
                    conn,
                    lake_root,
                    exchange=exchange,
                    segment=segment,
                    symbol=symbol,
                    span=(start, end),
                )
                minute_last = _intraday_last_close_by_date(
                    conn, lake_root, exchange=exchange, segment=segment, symbol=symbol
                )
                # The minute anchor is verified by the checked NSE calendar ALONE
                # (DEC-STALENESS-CACHE-TRUTH-001): the 1-day series is NOT read on
                # this basis any more — as a proxy calendar it only ever ADDED
                # over-refusals (a 1-day hole skipped a row whose correct anchor
                # existed in the 1-minute data), and its absence put every symbol
                # on the calendar-blind previous-AVAILABLE fallback (the bridged
                # stale-close mis-anchor class) — both closed by the one rule in
                # _minute_prev_close_contiguous.
            else:
                opens = _daily_open_by_date(
                    conn,
                    lake_root,
                    exchange=exchange,
                    segment=segment,
                    symbol=symbol,
                    span=(start, end),
                )
                daily = _daily_close_by_date(
                    conn, lake_root, exchange=exchange, segment=segment, symbol=symbol
                )
                daily_dates = sorted(daily)
        except (ValueError, duckdb.Error) as exc:
            _LOG.warning(
                "preopen.symbol.skipped",
                symbol=symbol,
                exchange=exchange,
                segment=segment,
                basis=basis,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            continue
        # DIAGNOSTIC evidence for the hole-shape classification (never the
        # anchor). Under the merged DEC-STALENESS-CACHE-TRUTH-001 rule only the
        # DAILY guard takes the on_hole seam (the minute guard has no 1-day
        # comparison), and the daily basis passes None (it never reads the
        # 1-minute series) — so the fired diagnostic is honestly two-way here
        # (see _hole_shape; the prebuild_lag arm is direct-unit-tested).
        minute_evidence: dict[date, Decimal] | None = minute_last if basis == "minute" else None
        for day in sorted(opens):
            if day < start or day > end:
                continue

            def _on_hole(
                expected: date,
                found: date,
                *,
                _symbol: str = symbol,
                _exchange: str = exchange,
                _segment: str = segment,
                _day: date = day,
                _minute: dict[date, Decimal] | None = minute_evidence,
            ) -> None:
                _warn_hole(_symbol, _exchange, _segment, _day, expected, found, _minute)

            if basis != "minute":
                anchor = _daily_prev_close_contiguous(
                    daily,
                    daily_dates,
                    day,
                    expected_session=_expected_session(day),
                    on_hole=_on_hole,
                )
            else:
                anchor = _minute_prev_close_contiguous(
                    minute_last, day, expected_session=_expected_session(day)
                )
            if anchor is None or anchor == 0:
                continue
            iep = opens[day]
            chng = iep - anchor
            rows.append(
                PreOpenRow(
                    day=day,
                    symbol=symbol,
                    prev_close=anchor,
                    iep=iep,
                    chng=chng,
                    pct_chng=chng / anchor * Decimal(100),
                )
            )
    if holes_suppressed:
        _LOG.warning(
            "preopen.prev_close_noncontiguous_suppressed",
            basis=basis,
            suppressed_symbols=holes_suppressed,
            cap=_HOLE_WARN_CAP,
        )
    rows.sort(key=lambda r: (r.day, r.symbol))
    return rows


#: The two report categories the operator toggles between on ``/reports``.
ReportMode = Literal["all", "topmover"]


def select_all_symbols(rows: list[PreOpenRow]) -> list[PreOpenRow]:
    """Select EVERY row, sorted by day then %CHNG **descending** within each day.

    The "All symbols" category (DEC-REPORTS-EXCEL-CATEGORIES-001): one row per
    ``(symbol, day)`` is kept verbatim — the minimum-%-change threshold is
    IGNORED in this mode. Within a trading day the rows run gainers → losers
    (``%CHNG`` high → low); ties break by SYMBOL alphabetical so the order is
    deterministic and reproducible.

    Parameters
    ----------
    rows
        The full set of :class:`PreOpenRow` rows (any order).

    Returns
    -------
    list[PreOpenRow]
        A new list sorted by ``(day, -pct_chng, symbol)``. The input is not
        mutated.
    """
    return sorted(rows, key=lambda r: (r.day, -r.pct_chng, r.symbol))


def select_top_movers(rows: list[PreOpenRow], *, min_pct: Decimal) -> list[PreOpenRow]:
    """Select the single biggest absolute mover per day (≥ *min_pct*), day-sorted.

    The "Top mover" category (DEC-REPORTS-EXCEL-CATEGORIES-001): for each trading
    day, among the rows whose ``abs(pct_chng) >= min_pct`` pick the ONE with the
    largest ``abs(pct_chng)`` — a gainer OR a loser (e.g. ``+5`` vs ``-6`` → the
    ``-6``). A day with no qualifying row is skipped entirely. Ties on
    ``abs(pct_chng)`` break by SYMBOL alphabetical so the choice is deterministic.
    The result is sorted by day.

    Parameters
    ----------
    rows
        The full set of :class:`PreOpenRow` rows (any order).
    min_pct
        The inclusive minimum absolute %CHNG a row must clear to qualify. ``0``
        (or any non-positive value) admits every row, so every day still yields
        its top mover.

    Returns
    -------
    list[PreOpenRow]
        One row per qualifying day, sorted by ``day``. The input is not mutated.
    """
    qualifiers = [r for r in rows if abs(r.pct_chng) >= min_pct]
    by_day = sorted(qualifiers, key=lambda r: r.day)
    picked: list[PreOpenRow] = []
    for _day, day_rows in groupby(by_day, key=lambda r: r.day):
        winner = min(day_rows, key=lambda r: (-abs(r.pct_chng), r.symbol))
        picked.append(winner)
    return picked


def select_rows(rows: list[PreOpenRow], *, mode: ReportMode, min_pct: Decimal) -> list[PreOpenRow]:
    """Dispatch to the row-selection function for *mode*.

    A thin façade over :func:`select_all_symbols` (``mode="all"``) and
    :func:`select_top_movers` (``mode="topmover"``) so callers pass a single
    *mode* string. *min_pct* is forwarded only in the top-mover branch (the
    "All symbols" category ignores the threshold by spec).

    Parameters
    ----------
    rows
        The full set of :class:`PreOpenRow` rows.
    mode
        ``"all"`` for every row, ``"topmover"`` for the per-day biggest mover.
    min_pct
        The inclusive minimum absolute %CHNG (top-mover mode only).

    Returns
    -------
    list[PreOpenRow]
        The selected, deterministically-ordered rows.
    """
    if mode == "all":
        return select_all_symbols(rows)
    return select_top_movers(rows, min_pct=min_pct)


def _cell_xml(col: int, row: int, value: str | float, style: int = _STYLE_NORMAL) -> str:
    """Render one ``<c>`` cell — numeric for int/float, inline-string otherwise.

    *style* is a 0-based index into the ``xl/styles.xml`` ``<cellXfs>`` table
    (:data:`_STYLE_NORMAL` / :data:`_STYLE_BOLD` / :data:`_STYLE_TITLE`). The
    default (0) is omitted from the markup since ``s="0"`` is the implicit
    per-cell style.
    """
    ref = f"{_col_letter(col)}{row}"
    s_attr = f' s="{style}"' if style else ""
    if isinstance(value, bool):  # pragma: no cover — defensive; never a bool cell
        value = str(value)
    if isinstance(value, (int, float)):
        return f'<c r="{ref}"{s_attr}><v>{value!r}</v></c>'
    return f'<c r="{ref}"{s_attr} t="inlineStr"><is><t>{_xml_escape(value)}</t></is></c>'


def _xml_escape(text: str) -> str:
    """Escape the 3 XML metacharacters for element text (``&`` first, then ``<``/``>``).

    A 3-replace stdlib escaper — deliberately NOT ``xml.sax.saxutils.escape`` (its
    ``xml.sax`` import is blacklisted by the security scanners even though it only
    escapes strings); this avoids the dependency + the false-positive finding.
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _col_letter(idx: int) -> str:
    """0-based column index → spreadsheet letter (0→A, 25→Z, 26→AA)."""
    letters = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _cols_xml(ncols: int) -> str:
    """Build the ``<cols>`` block giving each column a precise display width."""
    cols = "".join(
        f'<col min="{i + 1}" max="{i + 1}" width="{_COL_WIDTHS[i]:g}" customWidth="1"/>'
        for i in range(ncols)
    )
    return f"<cols>{cols}</cols>"


def _sheet_xml(
    header: tuple[str, ...], data: Sequence[tuple[str | float, ...]], *, title: str
) -> str:
    """Build ``sheet1.xml`` — the NSE-pre-open look, dependency-free.

    Row 1 is a merged, **bold, centred TITLE** carrying the report's date range
    (matching the NSE sheet's titled look — and the one place a *range* report
    states its dates beyond the per-row ``DATE`` column); row 2 is the **bold,
    frozen header**; data starts at row 3 with a **bold SYMBOL column**. Precise
    per-column widths + an auto-filter on the header + a frozen title/header keep
    the sheet readable the instant it opens. Element order follows the OOXML
    ``CT_Worksheet`` sequence (``sheetViews`` → ``cols`` → ``sheetData`` →
    ``autoFilter`` → ``mergeCells``) so Excel / Numbers open it without a repair
    prompt.
    """
    ncols = len(header)
    last_col = _col_letter(ncols - 1)
    last_row = len(data) + 2  # row 1 title + row 2 header + N data rows
    symbol_col = header.index("SYMBOL")
    out = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
        '<sheetViews><sheetView workbookViewId="0">',
        '<pane ySplit="2" topLeftCell="A3" activePane="bottomLeft" state="frozen"/>',
        "</sheetView></sheetViews>",
        _cols_xml(ncols),
        "<sheetData>",
        # Row 1 — the merged, bold, centred date-range title.
        f'<row r="1">{_cell_xml(0, 1, title, _STYLE_TITLE)}</row>',
        # Row 2 — the bold header.
        '<row r="2">'
        + "".join(_cell_xml(c, 2, h, _STYLE_BOLD) for c, h in enumerate(header))
        + "</row>",
    ]
    for r, record in enumerate(data, start=3):
        cells = "".join(
            _cell_xml(c, r, v, _STYLE_BOLD if c == symbol_col else _STYLE_NORMAL)
            for c, v in enumerate(record)
        )
        out.append(f'<row r="{r}">{cells}</row>')
    out.append("</sheetData>")
    out.append(f'<autoFilter ref="A2:{last_col}{last_row}"/>')
    out.append(f'<mergeCells count="1"><mergeCell ref="A1:{last_col}1"/></mergeCells>')
    out.append("</worksheet>")
    return "".join(out)


_CONTENT_TYPES: Final[str] = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
    '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
    "</Types>"
)
_ROOT_RELS: Final[str] = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
    "</Relationships>"
)
_WORKBOOK: Final[str] = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
    '<sheets><sheet name="Pre-Open" sheetId="1" r:id="rId1"/></sheets></workbook>'
)
_WORKBOOK_RELS: Final[str] = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
    '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
    "</Relationships>"
)

#: The workbook stylesheet: a default font (xf 0), a **bold** font (xf 1, the
#: header + SYMBOL column) and a **bold + horizontally-centred** font (xf 2, the
#: merged title row). The two mandatory default fills (``none`` + ``gray125``)
#: and the empty border keep strict OOXML consumers (Excel) happy.
#: DEC-PREOPEN-XLSX-STYLE-001.
_STYLES: Final[str] = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    '<fonts count="2">'
    '<font><sz val="11"/><name val="Calibri"/></font>'
    '<font><b/><sz val="11"/><name val="Calibri"/></font>'
    "</fonts>"
    '<fills count="4">'
    '<fill><patternFill patternType="none"/></fill>'
    '<fill><patternFill patternType="gray125"/></fill>'
    # fillId 2 — a light GREEN solid fill (gainer rows, %CHNG > 0).
    '<fill><patternFill patternType="solid"><fgColor rgb="FFC6EFCE"/>'
    '<bgColor indexed="64"/></patternFill></fill>'
    # fillId 3 — a light RED solid fill (loser rows, %CHNG < 0).
    '<fill><patternFill patternType="solid"><fgColor rgb="FFFFC7CE"/>'
    '<bgColor indexed="64"/></patternFill></fill>'
    "</fills>"
    '<borders count="1"><border/></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="7">'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
    '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1" applyAlignment="1">'
    '<alignment horizontal="center"/></xf>'
    # xf 3 — green fill, default font (a gainer's money/date cells).
    '<xf numFmtId="0" fontId="0" fillId="2" borderId="0" xfId="0" applyFill="1"/>'
    # xf 4 — red fill, default font (a loser's money/date cells).
    '<xf numFmtId="0" fontId="0" fillId="3" borderId="0" xfId="0" applyFill="1"/>'
    # xf 5 — green fill + bold (a gainer row's SYMBOL cell).
    '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>'
    # xf 6 — red fill + bold (a loser row's SYMBOL cell).
    '<xf numFmtId="0" fontId="1" fillId="3" borderId="0" xfId="0" applyFont="1" applyFill="1"/>'
    "</cellXfs>"
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
    "</styleSheet>"
)


def _row_fill_styles(pct_chng: Decimal, symbol_col: int, col: int) -> int:
    """Pick a cell style for column *col* of a row with the given ``%CHNG``.

    Maps the row's ``%CHNG`` sign to the green/red fill family and keeps the
    SYMBOL column bold (DEC-REPORTS-EXCEL-CATEGORIES-001 — every gainer row is
    green, every loser row red, an exact-zero row stays neutral):

    * ``pct_chng > 0`` → :data:`_STYLE_GREEN_BOLD` for the SYMBOL column,
      :data:`_STYLE_GREEN` for every other column;
    * ``pct_chng < 0`` → :data:`_STYLE_RED_BOLD` / :data:`_STYLE_RED`;
    * ``pct_chng == 0`` → :data:`_STYLE_BOLD` for SYMBOL, :data:`_STYLE_NORMAL`
      otherwise (the unfilled neutral look, identical to the single-sheet export).

    Parameters
    ----------
    pct_chng
        The row's full-precision ``%CHNG`` (its sign drives the fill).
    symbol_col
        The 0-based index of the bold SYMBOL column.
    col
        The 0-based index of the column being styled.

    Returns
    -------
    int
        The 0-based ``cellXfs`` index for the cell (see :data:`_STYLES`).
    """
    is_symbol = col == symbol_col
    if pct_chng > 0:
        return _STYLE_GREEN_BOLD if is_symbol else _STYLE_GREEN
    if pct_chng < 0:
        return _STYLE_RED_BOLD if is_symbol else _STYLE_RED
    return _STYLE_BOLD if is_symbol else _STYLE_NORMAL


def _filled_sheet_xml(rows: list[PreOpenRow], *, title: str) -> str:
    """Build one worksheet's XML with per-row green/red fills by ``%CHNG`` sign.

    The multi-sheet sibling of :func:`_sheet_xml` (DEC-REPORTS-EXCEL-CATEGORIES-001):
    same NSE look — a merged bold-centred TITLE (row 1), a bold frozen header
    (row 2), a bold SYMBOL column — but every data row is shaded by its
    ``%CHNG`` sign (green gainer / red loser / neutral zero) via
    :func:`_row_fill_styles`. Element order follows the OOXML ``CT_Worksheet``
    sequence so Excel / Numbers open it without a repair prompt.

    Parameters
    ----------
    rows
        The :class:`PreOpenRow` rows for this sheet, already in display order.
    title
        The row-1 banner text (e.g. the month label).

    Returns
    -------
    str
        The complete ``<worksheet>`` XML document.
    """
    header = REPORT_COLUMNS
    ncols = len(header)
    last_col = _col_letter(ncols - 1)
    last_row = len(rows) + 2  # row 1 title + row 2 header + N data rows
    symbol_col = header.index("SYMBOL")
    out = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
        '<sheetViews><sheetView workbookViewId="0">',
        '<pane ySplit="2" topLeftCell="A3" activePane="bottomLeft" state="frozen"/>',
        "</sheetView></sheetViews>",
        _cols_xml(ncols),
        "<sheetData>",
        f'<row r="1">{_cell_xml(0, 1, title, _STYLE_TITLE)}</row>',
        '<row r="2">'
        + "".join(_cell_xml(c, 2, h, _STYLE_BOLD) for c, h in enumerate(header))
        + "</row>",
    ]
    for r, record in enumerate(rows, start=3):
        # Widen the fixed heterogeneous display tuple to ``tuple[str | float, ...]``
        # so iterating it yields ``str | float`` (the type ``_cell_xml`` wants) —
        # iterating the FIXED tuple directly makes mypy join the element types to
        # ``object`` (matches how the single-sheet ``_sheet_xml`` types its data).
        row_cells: tuple[str | float, ...] = record.display_cells()
        cells = "".join(
            _cell_xml(c, r, v, _row_fill_styles(record.pct_chng, symbol_col, c))
            for c, v in enumerate(row_cells)
        )
        out.append(f'<row r="{r}">{cells}</row>')
    out.append("</sheetData>")
    out.append(f'<autoFilter ref="A2:{last_col}{last_row}"/>')
    out.append(f'<mergeCells count="1"><mergeCell ref="A1:{last_col}1"/></mergeCells>')
    out.append("</worksheet>")
    return "".join(out)


def _month_label(day: date) -> str:
    """The ``YYYY-MM`` sheet label for *day* (e.g. ``date(2024, 3, 4)`` → ``2024-03``)."""
    return f"{day.year:04d}-{day.month:02d}"


def _group_by_month(rows: list[PreOpenRow]) -> list[tuple[str, list[PreOpenRow]]]:
    """Group *already day-sorted* rows into ``(YYYY-MM, rows)`` pairs, month-ordered.

    Only months that actually have rows produce a pair, so empty months yield no
    sheet (DEC-REPORTS-EXCEL-CATEGORIES-001). The within-month order of *rows* is
    preserved verbatim (the caller's selection order — gainers→losers for "all",
    day-ascending for "top mover").

    Parameters
    ----------
    rows
        Rows pre-sorted so each month's rows are contiguous and ascending (the
        output of :func:`select_all_symbols` / :func:`select_top_movers`).

    Returns
    -------
    list[tuple[str, list[PreOpenRow]]]
        One ``(label, rows)`` pair per non-empty month, in month order.
    """
    return [
        (label, list(group)) for label, group in groupby(rows, key=lambda r: _month_label(r.day))
    ]


def _dynamic_content_types(nsheets: int) -> str:
    """Build ``[Content_Types].xml`` declaring *nsheets* worksheet overrides."""
    overrides = "".join(
        f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for i in range(1, nsheets + 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        f"{overrides}"
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        "</Types>"
    )


def _dynamic_workbook(sheet_names: list[str]) -> str:
    """Build ``xl/workbook.xml`` listing one ``<sheet>`` per label in *sheet_names*."""
    sheets = "".join(
        f'<sheet name="{_xml_escape(name)}" sheetId="{i}" r:id="rId{i}"/>'
        for i, name in enumerate(sheet_names, start=1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<sheets>{sheets}</sheets></workbook>"
    )


def _dynamic_workbook_rels(nsheets: int) -> str:
    """Build ``xl/_rels/workbook.xml.rels`` wiring *nsheets* sheets + the styles part."""
    sheet_rels = "".join(
        f'<Relationship Id="rId{i}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        f'Target="worksheets/sheet{i}.xml"/>'
        for i in range(1, nsheets + 1)
    )
    styles_id = nsheets + 1
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f"{sheet_rels}"
        f'<Relationship Id="rId{styles_id}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        'Target="styles.xml"/>'
        "</Relationships>"
    )


def render_month_workbook(months: list[tuple[str, list[PreOpenRow]]]) -> bytes:
    """Render one ``.xlsx`` with **one worksheet per month** in *months*.

    The per-year workbook of the two-category report
    (DEC-REPORTS-EXCEL-CATEGORIES-001): each ``(label, rows)`` pair becomes a
    worksheet named after *label* (e.g. ``2024-01``), carrying the
    ``DATE · SYMBOL · PREV. CLOSE · IEP · CHNG · %CHNG`` columns with per-row
    green/red fills by ``%CHNG`` sign (:func:`_filled_sheet_xml`). The bytes are
    deterministic (fixed :data:`_ZIP_EPOCH` member timestamps) so the same input
    always produces a byte-identical workbook (CLAUDE.md §2 Rule 7).

    An empty *months* yields a valid single header-only worksheet named
    ``Pre-Open`` (a workbook must have at least one sheet), so a year with no
    rows still opens cleanly rather than producing a corrupt file.

    Parameters
    ----------
    months
        ``(sheet_label, rows)`` pairs, one per non-empty month, in sheet order.

    Returns
    -------
    bytes
        The deterministic ``.xlsx`` byte string.
    """
    pairs = months or [("Pre-Open", [])]
    names = [label for label, _ in pairs]
    parts = {
        "[Content_Types].xml": _dynamic_content_types(len(pairs)),
        "_rels/.rels": _ROOT_RELS,
        "xl/workbook.xml": _dynamic_workbook(names),
        "xl/_rels/workbook.xml.rels": _dynamic_workbook_rels(len(pairs)),
        "xl/styles.xml": _STYLES,
    }
    for i, (label, rows) in enumerate(pairs, start=1):
        parts[f"xl/worksheets/sheet{i}.xml"] = _filled_sheet_xml(rows, title=label)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in parts.items():
            info = zipfile.ZipInfo(name, date_time=_ZIP_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, content)
    return buffer.getvalue()


def render_report_zip(
    rows: list[PreOpenRow],
    *,
    mode: ReportMode,
    min_pct: Decimal,
    now: datetime | None = None,
) -> bytes:
    """Render the two-category report to a ``.zip`` of one ``.xlsx`` per year.

    The top-level builder of DEC-REPORTS-EXCEL-CATEGORIES-001:

    1. select rows for *mode* (:func:`select_rows` — "all" keeps every row
       gainers→losers per day, "topmover" keeps the single biggest absolute
       mover per day clearing *min_pct*);
    2. group the selected rows by **year**;
    3. for each year build a workbook whose worksheets are that year's
       **months** (:func:`render_month_workbook`), with per-row green/red fills;
    4. write every yearly workbook into one ``.zip`` (stdlib :mod:`zipfile`),
       the member name carrying the mode + year (e.g. ``preopen_all_2024.xlsx``
       / ``preopen_topmover_2024.xlsx``) and each member stamped with the IST
       generation time so a fresh download reads "Date Modified = now".

    An empty selection yields a valid ``.zip`` holding a single header-only
    workbook (``preopen_<mode>_empty.xlsx``) so the download never fails.

    Parameters
    ----------
    rows
        The full set of :class:`PreOpenRow` rows (any order).
    mode
        ``"all"`` (every row) or ``"topmover"`` (per-day biggest mover).
    min_pct
        The inclusive minimum absolute ``%CHNG`` for the top-mover mode
        (ignored when *mode* is ``"all"``).
    now
        Injected generation timestamp for the member mtime (deterministic tests);
        defaults to :func:`datetime.now` in IST.

    Returns
    -------
    bytes
        The ``.zip`` byte string (member mtimes = the IST generation time;
        workbook BODIES are byte-stable for the same data).
    """
    selected = select_rows(rows, mode=mode, min_pct=min_pct)
    by_year: dict[int, list[PreOpenRow]] = {}
    for row in selected:
        by_year.setdefault(row.day.year, []).append(row)
    members: dict[str, bytes] = {}
    if not by_year:
        members[f"preopen_{mode}_empty.xlsx"] = render_month_workbook([])
    else:
        for year in sorted(by_year):
            members[f"preopen_{mode}_{year:04d}.xlsx"] = render_month_workbook(
                _group_by_month(by_year[year])
            )
    # Each ``.xlsx`` MEMBER is stamped with the GENERATION time in IST
    # (DEC-PREOPEN-XLSX-DATE-001, amended) — so a freshly-downloaded file shows
    # "Date Modified = now" in Finder, the natural expectation for a download
    # (the operator's recurring ask: "everywhere the precise current IST
    # timestamp"). ``now`` is injectable for deterministic tests; the route lets
    # it default to ``datetime.now(IST)``. The .xlsx INTERNAL OOXML parts keep
    # ``_ZIP_EPOCH`` (invisible to the operator) so the same data still produces
    # a byte-identical workbook BODY — only the outer member mtime is the clock.
    stamp = now if now is not None else datetime.now(IST)
    gen_date = (stamp.year, stamp.month, stamp.day, stamp.hour, stamp.minute, stamp.second)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in members.items():
            info = zipfile.ZipInfo(name, date_time=gen_date)
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, content)
    return buffer.getvalue()


def _default_title(rows: list[PreOpenRow]) -> str:
    """Derive the sheet title from *rows* when the caller passes none.

    Rows are sorted by ``(day, symbol)``, so the first/last row bound the date
    range. An empty report falls back to the bare ``"Pre-Open Market"`` label.
    """
    if not rows:
        return "Pre-Open Market"
    return f"Pre-Open Market: {rows[0].day.isoformat()} to {rows[-1].day.isoformat()}"


def render_xlsx(rows: list[PreOpenRow], *, title: str | None = None) -> bytes:
    """Render *rows* to a deterministic, dependency-free ``.xlsx`` byte string.

    A minimal valid OOXML workbook (one ``Pre-Open`` sheet: a bold, centred
    date-range title row, a bold + frozen header, a bold SYMBOL column and
    precise per-column widths) written with the stdlib :mod:`zipfile` + a FIXED
    member timestamp, so the same rows always produce byte-identical output
    (reproducible, CLAUDE.md §2 Rule 7). An empty *rows* yields a valid
    title + header-only workbook.

    *title* sets the row-1 banner (the report's date range — how a multi-day
    *range* report states its dates, complementing the per-row ``DATE``
    column); when omitted it is derived from the rows' date span via
    :func:`_default_title`.
    """
    data = [r.display_cells() for r in rows]
    sheet_title = title if title is not None else _default_title(rows)
    parts = {
        "[Content_Types].xml": _CONTENT_TYPES,
        "_rels/.rels": _ROOT_RELS,
        "xl/workbook.xml": _WORKBOOK,
        "xl/_rels/workbook.xml.rels": _WORKBOOK_RELS,
        "xl/styles.xml": _STYLES,
        "xl/worksheets/sheet1.xml": _sheet_xml(REPORT_COLUMNS, data, title=sheet_title),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in parts.items():
            info = zipfile.ZipInfo(name, date_time=_ZIP_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, content)
    return buffer.getvalue()


#: How many symbols a filename spells out before collapsing to ``+Nmore`` —
#: keeps a many-symbol selection's name readable and well under filesystem
#: name limits while staying a precise reference (DEC-REPORT-FILENAME-001).
_FILENAME_SYMBOL_CAP: Final[int] = 5


def _filename_token(value: str) -> str:
    """Sanitize one filename token to ``[A-Za-z0-9-]`` (``M&M`` → ``M-M``).

    Every non-safe run collapses to a single ``-`` and edge dashes are
    trimmed, so the token can never smuggle a path separator, a quote (the
    ``Content-Disposition`` header wraps the name in quotes), or whitespace
    into the download name. An all-unsafe input degrades to ``X`` rather
    than an empty token.
    """
    cleaned = re.sub(r"[^A-Za-z0-9-]+", "-", value).strip("-")
    return cleaned or "X"


def report_filename(
    *,
    extension: str,
    start: date,
    end: date,
    basis: str,
    symbols: Sequence[str] | None,
    mode: str | None = None,
    min_pct: Decimal | None = None,
) -> str:
    """Build the SELF-DESCRIBING download filename (DEC-REPORT-FILENAME-001).

    The operator's reference problem: a static ``preopen_report.zip`` says
    nothing about WHICH selection produced it. The name now encodes every
    selection knob, in order: report kind · mode (``all`` /
    ``topmover-minNpct`` — the threshold only where it applies) · price basis
    (``day``/``minute``) · the selected symbols (spelled out up to
    :data:`_FILENAME_SYMBOL_CAP`, then ``+Nmore``; ``all-symbols`` when no
    filter) · the inclusive date range. Examples::

        preopen_day_all-symbols_2026-06-01_2026-06-11.xlsx
        preopen_topmover-min2.5pct_minute_NIFTY-BANKNIFTY_2026-06-01_2026-06-11.zip

    Pure + deterministic (offline-testable; the routes stay thin wrappers).
    ``min_pct`` is rendered via ``Decimal.normalize`` so ``2.0`` reads
    ``2`` and ``2.5`` stays ``2.5`` — display formatting of a threshold,
    never money rounding (DEC-MONEY-2DP-001 is money-only).
    """
    parts = ["preopen"]
    if mode is not None:
        if mode == "topmover" and min_pct is not None:
            parts.append(f"topmover-min{format(min_pct.normalize(), 'f')}pct")
        else:
            parts.append(_filename_token(mode))
    parts.append(_filename_token(basis))
    if symbols:
        label = "-".join(_filename_token(s) for s in symbols[:_FILENAME_SYMBOL_CAP])
        if len(symbols) > _FILENAME_SYMBOL_CAP:
            label += f"+{len(symbols) - _FILENAME_SYMBOL_CAP}more"
        parts.append(label)
    else:
        parts.append("all-symbols")
    parts.append(start.isoformat())
    parts.append(end.isoformat())
    return "_".join(parts) + extension


__all__ = [
    "REPORT_BASES",
    "REPORT_COLUMNS",
    "REPORT_EXCLUDED_SEGMENTS",
    "PreOpenRow",
    "ReportMode",
    "build_preopen_rows",
    "render_month_workbook",
    "render_report_zip",
    "render_xlsx",
    "report_filename",
    "resolve_report_datasets",
    "select_all_symbols",
    "select_rows",
    "select_top_movers",
]
