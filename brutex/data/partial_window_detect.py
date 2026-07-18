"""Observe-only diagnostic — flag a fetch window the pull marks SUCCESS but TRUNCATED.

DEC-PARTIAL-WINDOW-DETECT-001 (MIGRATION.md #1008). The window classifier
(:func:`brutex.data.window_status.classify_window_status`) returns ``SUCCESS`` the
moment the vendor returns ANY bar (``raw_bars > 0``) — with **no completeness
check** — and the unified ledger then treats ``SUCCESS`` as terminal/skippable
(:data:`brutex.data.window_ledger._SKIPPABLE`,
:meth:`brutex.data.window_ledger.WindowLedger.is_skippable`). So a window that
SHOULD hold ~30 days but returned only the first 2 days is stamped ``SUCCESS``,
becomes skippable, and is **never re-pulled to complete**. That is a real
data-completeness blind spot.

This module is the OBSERVE-ONLY diagnostic for it, mirroring the existing
pull-side suspicious-empty flag
(:func:`brutex.adapters.groww.option_ingest.scan_underlying_all_empty`,
DEC-SUSPICIOUS-EMPTY-FLAG-001) and the read-side reconcile diagnostic
(``completeness.symbol_reconcile_mismatch``, #907). It changes **NO** return
value, **NO** ``SUCCESS`` classification, **NO** skip rule, **NO** ledger row,
**NO** engine input — the brute-force ENGINE stays NIFTY/BANKNIFTY/SENSEX
(CLAUDE.md §3). It DETECTS and WARNs; it does not auto-remediate (Golden Rule
#10).

Scope: SPOT + FUTURES windows only (OPTIONS EXCLUDED by design)
--------------------------------------------------------------
The detector is wired into the SPOT pull tail (`brutex/adapters/groww/ingest.py`)
and the FUTURES contract path (`brutex/adapters/groww/option_ingest.py`, gated to
``contract_type == "FUT"``) — both trade DENSELY every session, so a truncated
window there is a genuine, reliably-detectable fetch gap. **OPTIONS (CE/PE) are
deliberately NOT watched**: an option routinely stops printing bars several
sessions before its expiry (liquidity death in the final week) even though the
contract is COMPLETE, so truncation is indistinguishable from sparsity for an
option — watching the chain would flag constantly (a false-positive storm, "worse
than no detector"). That exclusion is enforced at the wiring (the spine decides
which windows to observe); the classifier below is contract-agnostic and pure.

False-positive safety (the crux — read this before touching the threshold)
--------------------------------------------------------------------------
A naive "expected bar count vs actual bar count" check WOULD false-flag a
genuinely-sparse window and could imply a giant wasteful re-pull. We DO NOT do
that. The flag fires ONLY on a strong **TRUNCATION** signal, never on sparsity:

* The signal is *temporal coverage*, not *bar density*. We compare the date of
  the LAST stored bar against where the last bar SHOULD be — the window's end
  boundary, clamped to the contract's expiry/delisting and to the last completed
  trading session. A window whose bars run up to its expiry (or to the window
  end) is COMPLETE **even if it only has a few bars** (sparse-but-complete is NOT
  flagged). A window whose last bar stops materially earlier than that bound —
  with the instrument still live well past it — is the truncation case.
* The truncation must exceed a conservative, env-tunable tolerance of *trading
  sessions* (:data:`PARTIAL_WINDOW_TOLERANCE_SESSIONS`, env
  ``BRUTEX_PARTIAL_WINDOW_TOLERANCE_SESSIONS``, default 5). One or two missing
  trailing sessions (a holiday cluster, a vendor publish-lag day) never trip it.
* Explicitly EXCLUDED (each returns :data:`NOT_TRUNCATED`):
  - an EMPTY window (no stored bars — already handled by the EMPTY status);
  - an OUT-OF-CALENDAR-COVERAGE window — when ``window_end``, the last bar, OR
    *today* (the live-edge anchor also walks the calendar) falls in a year
    OUTSIDE the NSE holiday list's covered span
    (:func:`brutex.calendars.nse.is_holiday_data_available`,
    ``[HOLIDAY_COVERAGE_FIRST_YEAR, HOLIDAY_COVERAGE_LAST_YEAR]``). Outside that
    span :func:`~brutex.calendars.nse.is_trading_day` would silently count a
    weekday holiday (Holi / Good Friday) as a session, inflating the gap and
    producing a spurious flag — so we honestly abstain (Golden Rule #1, no guess);
  - a window at the LIVE EDGE (its end boundary is within the live-edge horizon
    :data:`PARTIAL_WINDOW_LIVE_EDGE_SESSIONS` of *today* — the vendor may simply
    not have published the most recent sessions yet);
  - a window whose end boundary IS the contract's expiry/delisting bound (a bar
    cannot exist past a dead contract, so stopping there is COMPLETE).

Everything here is pure + clock-injected (no I/O, no network), O(1) per window,
so it is unit-testable offline and safe to fire best-effort from the pull spine.
The trading-session arithmetic reuses the COMMON NSE calendar
(:func:`brutex.calendars.nse.is_trading_day`), never a re-implemented weekday
rule (CLAUDE.md §11 COMMON).
"""

from __future__ import annotations

import os
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from brutex.calendars.nse import IST, is_holiday_data_available
from brutex.calendars.observed_sessions import is_trading_day_observed
from brutex.data.detectors_rust import (
    PartialWindowPayload,
    partial_window_rust_enabled,
    routed_classify_partial_window,
)

__all__ = [
    "NOT_TRUNCATED",
    "PARTIAL_WINDOW_LIVE_EDGE_SESSIONS",
    "PARTIAL_WINDOW_TOLERANCE_SESSIONS",
    "SUSPECTED_PARTIAL",
    "SUSPECTED_PARTIAL_WINDOW_ERROR_EVENT",
    "SUSPECTED_PARTIAL_WINDOW_EVENT",
    "PartialWindowVerdict",
    "classify_partial_window",
    "last_completed_session_on_or_before",
    "observe_partial_window",
    "sessions_between",
]

#: Verdict: the window's last stored bar stops materially before its expected end
#: — a SUSPECTED truncation worth a human look (observe-only; never an auto-action).
SUSPECTED_PARTIAL: str = "SUSPECTED_PARTIAL"
#: Verdict: the window is complete-to-bound, sparse-but-complete, empty, at the
#: live edge, or at expiry — NOT a truncation. The healthy steady state.
NOT_TRUNCATED: str = "NOT_TRUNCATED"

#: Default truncation tolerance, in TRADING SESSIONS. The last stored bar must lag
#: the expected end bound by STRICTLY MORE than this many trading sessions before a
#: window is flagged. Conservative on purpose: a holiday cluster or a one-day vendor
#: publish-lag (a few missing trailing sessions) must never trip the flag — only a
#: materially-truncated window (e.g. last bar on day 2 of a 30-day window) does.
#: Env-tunable via ``BRUTEX_PARTIAL_WINDOW_TOLERANCE_SESSIONS`` (a non-positive /
#: malformed value falls back to this default — the diagnostic never crashes a pull).
PARTIAL_WINDOW_TOLERANCE_SESSIONS: int = 5

#: Default live-edge horizon, in TRADING SESSIONS. When the window's expected end
#: bound is within this many trading sessions of *today*, the window is at the LIVE
#: EDGE and is NEVER flagged — the vendor may not have published the most recent
#: sessions yet, so a "missing tail" there is expected, not a truncation. Env-tunable
#: via ``BRUTEX_PARTIAL_WINDOW_LIVE_EDGE_SESSIONS`` (same non-positive/malformed
#: fallback as the tolerance).
PARTIAL_WINDOW_LIVE_EDGE_SESSIONS: int = 3


@dataclass(frozen=True, slots=True)
class PartialWindowVerdict:
    """The immutable result of classifying one window for SUSPECTED truncation.

    Attributes
    ----------
    verdict
        :data:`SUSPECTED_PARTIAL` or :data:`NOT_TRUNCATED`.
    last_bar_date
        The IST trading date of the window's last stored bar, or ``None`` for an
        EMPTY window (no bars stored).
    expected_end_date
        The IST trading date the last bar SHOULD reach — the window's end boundary
        clamped to the expiry/delisting bound and to the last completed trading
        session before *now*. ``None`` only for an EMPTY window (excluded before it
        is computed).
    missing_sessions
        How many trading sessions the last bar lags behind *expected_end_date*
        (``0`` when complete-to-bound or not flagged). Always ``>= 0``.
    reason
        A short, non-secret machine tag for WHY this verdict was reached
        (``"truncated"`` / ``"complete"`` / ``"empty"`` / ``"live_edge"`` /
        ``"at_expiry"`` / ``"sparse_complete"``), so the WARNING + tests can assert
        the path without parsing prose.
    """

    verdict: str
    last_bar_date: date | None
    expected_end_date: date | None
    missing_sessions: int
    reason: str

    def is_suspected_partial(self) -> bool:
        """True iff this window tripped the truncation flag (else the emit stays silent)."""
        return self.verdict == SUSPECTED_PARTIAL


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


def last_completed_session_on_or_before(
    d: date, *, observed: AbstractSet[date] = frozenset()
) -> date:
    """Return the latest NSE trading day ``<= d`` — the last completed session by *d*.

    Walks backwards day-by-day from *d* until :func:`brutex.calendars.nse.is_trading_day`
    is True AND the date is not an *observed* overlay day
    (DEC-CALENDAR-OBSERVED-SESSIONS-001 — an observably-untraded day is not a
    session, so the expected end rolls back past it). A weekend / holiday rolls
    back to the prior session. Bounded: the NSE week always contains a trading
    day, the DETECTOR-written overlay is capped at 8 days/year (write-time —
    ``classify_month_drift``; a hand-edited sidecar still loads a FINITE set),
    so this returns within a handful of steps (O(1) amortised — never an
    unbounded loop). Used to map a
    calendar end boundary onto the trading-session grid the bars actually live on.
    """
    cursor = d
    while not is_trading_day_observed(cursor, observed=observed):
        cursor -= timedelta(days=1)
    return cursor


def sessions_between(start: date, end: date, *, observed: AbstractSet[date] = frozenset()) -> int:
    """Count NSE trading days in the half-open ``(start, end]`` — sessions AFTER *start*.

    Returns the number of trading sessions strictly after *start* up to and
    including *end* (``0`` when ``end <= start``). This is the "how many sessions did
    the last bar miss" measure: *start* is the last stored bar's session, *end* the
    expected-end session, so the result is the trailing gap in sessions (a holiday
    span between the two contributes nothing — only real sessions count; an
    *observed* overlay day likewise contributes nothing —
    DEC-CALENDAR-OBSERVED-SESSIONS-001, remove-only: subtracting sessions can only
    SHRINK the measured lag). O(days in span); the spans here are bounded by a
    single contract window.
    """
    if end <= start:
        return 0
    count = 0
    cursor = start + timedelta(days=1)
    while cursor <= end:
        if is_trading_day_observed(cursor, observed=observed):
            count += 1
        cursor += timedelta(days=1)
    return count


def classify_partial_window(
    *,
    last_bar_ts: datetime | None,
    window_end: date,
    now: datetime,
    expiry: date | None = None,
    tolerance_sessions: int | None = None,
    live_edge_sessions: int | None = None,
    observed: AbstractSet[date] = frozenset(),
) -> PartialWindowVerdict:
    """Classify one SUCCESS window as :data:`SUSPECTED_PARTIAL` or :data:`NOT_TRUNCATED`.

    The pure, false-positive-safe truncation classifier (see the module docstring).
    It NEVER changes the window's stored status — it only OBSERVES.

    Parameters
    ----------
    last_bar_ts
        UTC-aware timestamp of the window's LAST stored bar, or ``None`` for an
        EMPTY window (no bars). Converted to its IST trading date internally (the
        bars are stored UTC but trade on the IST session grid, CLAUDE.md §6).
    window_end
        The window's end boundary (the END-EXCLUSIVE fetch bound, e.g. the ledger
        row's ``window_end``). The last bar would land on the last trading session
        STRICTLY BEFORE this date.
    now
        The current wall-clock (any tz-aware datetime); its IST date anchors the
        live-edge horizon. Injected so the verdict is deterministic offline.
    expiry
        The contract's expiry / delisting date (its hard last tradable day), or
        ``None`` for a spot series / perpetual with no expiry. When set, the
        expected end can never reach past it — a window ending at expiry is COMPLETE.
    tolerance_sessions
        Override the truncation tolerance (trading sessions). ``None`` reads
        :data:`PARTIAL_WINDOW_TOLERANCE_SESSIONS` (env-tunable).
    live_edge_sessions
        Override the live-edge horizon (trading sessions). ``None`` reads
        :data:`PARTIAL_WINDOW_LIVE_EDGE_SESSIONS` (env-tunable).
    observed
        Observed-holiday overlay days (DEC-CALENDAR-OBSERVED-SESSIONS-001,
        remove-only; default empty = byte-identical back-compat). An overlaid
        date is not a session in the margin arithmetic — an observed holiday is
        not a data hole, so the expected end + missing-session count step over it.

    Returns
    -------
    PartialWindowVerdict
        The immutable verdict + the facts that produced it.
    """
    tolerance = (
        tolerance_sessions
        if tolerance_sessions is not None
        else _tunable_sessions(
            "BRUTEX_PARTIAL_WINDOW_TOLERANCE_SESSIONS", PARTIAL_WINDOW_TOLERANCE_SESSIONS
        )
    )
    live_edge = (
        live_edge_sessions
        if live_edge_sessions is not None
        else _tunable_sessions(
            "BRUTEX_PARTIAL_WINDOW_LIVE_EDGE_SESSIONS", PARTIAL_WINDOW_LIVE_EDGE_SESSIONS
        )
    )

    def _oracle() -> PartialWindowVerdict:
        return _classify_partial_window_python(
            last_bar_ts=last_bar_ts,
            window_end=window_end,
            now=now,
            expiry=expiry,
            tolerance=tolerance,
            live_edge=live_edge,
            observed=observed,
        )

    if not partial_window_rust_enabled():
        return _oracle()

    def _payload() -> PartialWindowPayload:
        return _partial_window_payload(
            last_bar_ts=last_bar_ts,
            window_end=window_end,
            now=now,
            expiry=expiry,
            tolerance=tolerance,
            live_edge=live_edge,
            observed=observed,
        )

    return routed_classify_partial_window(
        payload_factory=_payload, python_law=_oracle, materialize=_verdict_from_fields
    )


#: The session-grid back-walk margin, in DAYS, below the earliest walked
#: anchor (SLICE T24 PR-1). 45 days dwarfs any real NSE non-session run
#: (the longest festival+weekend cluster is ~5 days; the DETECTOR-written
#: observed overlay is capped at 8 days/year) — and correctness NEVER
#: depends on it: a walk stepping below the grid ABSTAINS to the oracle.
_SPAN_BACKWALK_MARGIN_DAYS = 45

#: The session-grid size cap, in DAYS. A span this wide (a far-future /
#: far-past expiry, a degenerate hypothesis input) is never materialised —
#: the payload carries an EMPTY grid, so the crate's first walk abstains
#: and the oracle serves (whose per-walk cost is tiny on those shapes,
#: unlike a multi-million-flag grid).
_SPAN_CAP_DAYS = 9_000


def _partial_window_payload(
    *,
    last_bar_ts: datetime | None,
    window_end: date,
    now: datetime,
    expiry: date | None,
    tolerance: int,
    live_edge: int,
    observed: AbstractSet[date],
) -> PartialWindowPayload:
    """Build the ints-only FFI payload for the routed classifier (flag-ON only).

    Python computes every CALENDAR fact — the IST dates, the three-way
    ``is_holiday_data_available`` conjunction (folded to ``coverage_ok``;
    computed only when a last bar exists, mirroring the oracle's EMPTY-first
    arm order) and the per-day ``is_trading_day_observed`` SESSION GRID —
    and Rust decides the verdict from plain data (the honest split: the NSE
    calendar is never re-implemented in Rust). The grid spans every session
    walk the law can make, floored :data:`_SPAN_BACKWALK_MARGIN_DAYS` below
    the earliest anchor; a span past :data:`_SPAN_CAP_DAYS` ships an EMPTY
    grid (→ the crate abstains, the oracle serves).
    """
    if last_bar_ts is None:
        return PartialWindowPayload(
            last_bar_ordinal=None,
            window_end_ordinal=window_end.toordinal(),
            today_ordinal=now.astimezone(IST).date().toordinal(),
            expiry_ordinal=expiry.toordinal() if expiry is not None else None,
            coverage_ok=False,  # unused — the crate's EMPTY arm returns first
            tolerance=tolerance,
            live_edge=live_edge,
            span_start_ordinal=0,
            trading_flags=(),
        )
    last_bar_date = last_bar_ts.astimezone(IST).date()
    today = now.astimezone(IST).date()
    coverage_ok = (
        is_holiday_data_available(window_end)
        and is_holiday_data_available(last_bar_date)
        and is_holiday_data_available(today)
    )
    low_anchors = [last_bar_date, window_end - timedelta(days=1), today]
    if expiry is not None:
        low_anchors.append(expiry)
    span_low = min(low_anchors) - timedelta(days=_SPAN_BACKWALK_MARGIN_DAYS)
    span_high = max(today, expiry) if expiry is not None else today
    span_days = (span_high - span_low).days + 1
    if coverage_ok and span_days <= _SPAN_CAP_DAYS:
        flags = tuple(
            is_trading_day_observed(span_low + timedelta(days=i), observed=observed)
            for i in range(span_days)
        )
    else:
        # Out-of-coverage (the crate returns before any walk) or an
        # over-cap span (the crate's first walk abstains → oracle) — never
        # pay a giant grid.
        flags = ()
    return PartialWindowPayload(
        last_bar_ordinal=last_bar_date.toordinal(),
        window_end_ordinal=window_end.toordinal(),
        today_ordinal=today.toordinal(),
        expiry_ordinal=expiry.toordinal() if expiry is not None else None,
        coverage_ok=coverage_ok,
        tolerance=tolerance,
        live_edge=live_edge,
        span_start_ordinal=span_low.toordinal(),
        trading_flags=flags,
    )


def _verdict_from_fields(
    reason: str,
    last_bar_ordinal: int | None,
    expected_end_ordinal: int | None,
    missing_sessions: int,
) -> PartialWindowVerdict:
    """Materialize the crate's verdict fields into a :class:`PartialWindowVerdict`.

    The verdict string derives from the reason token exactly as the oracle
    assigns it — ``"truncated"`` is the ONE :data:`SUSPECTED_PARTIAL`
    reason, every other arm is :data:`NOT_TRUNCATED`. Defined target-side
    so the facade never imports this module's dataclass (no import cycle).
    """
    return PartialWindowVerdict(
        verdict=SUSPECTED_PARTIAL if reason == "truncated" else NOT_TRUNCATED,
        last_bar_date=(
            date.fromordinal(last_bar_ordinal) if last_bar_ordinal is not None else None
        ),
        expected_end_date=(
            date.fromordinal(expected_end_ordinal) if expected_end_ordinal is not None else None
        ),
        missing_sessions=missing_sessions,
        reason=reason,
    )


def _classify_partial_window_python(
    *,
    last_bar_ts: datetime | None,
    window_end: date,
    now: datetime,
    expiry: date | None,
    tolerance: int,
    live_edge: int,
    observed: AbstractSet[date],
) -> PartialWindowVerdict:
    """The verbatim classifier body — the LIVE parity oracle (SLICE T24 PR-1).

    Extracted token-for-token from :func:`classify_partial_window` for the
    DEC-DATA-RUST-INTEGRITY-DETECTORS-001 routing, MINUS the two
    env-resolution blocks (which stay at the public composition root — the
    Slice-0 precedent — so both arms consume ONE resolved
    *tolerance*/*live_edge*). Flag OFF (the default) runs exactly this
    body, byte-identical to the pre-facade law.
    """
    # EMPTY window — no stored bars to be "truncated"; the EMPTY status already owns it.
    if last_bar_ts is None:
        return PartialWindowVerdict(
            verdict=NOT_TRUNCATED,
            last_bar_date=None,
            expected_end_date=None,
            missing_sessions=0,
            reason="empty",
        )

    last_bar_date = last_bar_ts.astimezone(IST).date()
    today = now.astimezone(IST).date()

    # OUT-OF-CALENDAR-COVERAGE exclusion (MAJOR-1): the NSE holiday list only covers
    # ``[HOLIDAY_COVERAGE_FIRST_YEAR, HOLIDAY_COVERAGE_LAST_YEAR]`` (currently 2020-2026).
    # OUTSIDE that span ``is_trading_day`` would silently count a weekday holiday (Holi /
    # Good Friday) as a session, inflating the missing-session count into a SPURIOUS flag.
    # When either the window end OR the last bar — or TODAY itself (the live-edge anchor:
    # ``last_completed_session_on_or_before(today)`` + the ``sessions_between`` horizon
    # count both walk the calendar up to *now*, so an uncovered current year would
    # inflate the horizon and shrink the live-edge exclusion; the today-anchor gap the
    # calendar-correctness audit found) — falls in an uncovered year we honestly ABSTAIN
    # (Golden Rule #1 — surface the gap, never guess), reusing the COMMON
    # ``is_holiday_data_available`` rather than a hardcoded year range.
    if not (
        is_holiday_data_available(window_end)
        and is_holiday_data_available(last_bar_date)
        and is_holiday_data_available(today)
    ):
        return PartialWindowVerdict(
            verdict=NOT_TRUNCATED,
            last_bar_date=last_bar_date,
            expected_end_date=None,
            missing_sessions=0,
            reason="out_of_calendar_coverage",
        )

    # The window's end boundary is END-EXCLUSIVE, so the last bar's session is the last
    # completed trading day STRICTLY BEFORE window_end. The expected end is that session,
    # additionally clamped to the contract's expiry (a dead contract has no bars past it)
    # and to today (the vendor cannot publish a future session). Whichever bound is
    # EARLIEST wins — the conservative never-flag-past-a-real-limit choice.
    end_exclusive = window_end - timedelta(days=1)
    if expiry is not None and expiry < end_exclusive:
        end_exclusive = expiry
    end_exclusive = min(end_exclusive, today)
    expected_end = last_completed_session_on_or_before(end_exclusive, observed=observed)

    # AT-EXPIRY exclusion: the window's expected end coincides with (or sits at/after) the
    # contract's expiry session. A bar cannot exist past a dead contract, so the last bar
    # stopping there is COMPLETE, never a truncation — even if it lands a session or two
    # short of the raw calendar end_exclusive.
    if expiry is not None:
        expiry_session = last_completed_session_on_or_before(expiry, observed=observed)
        if expected_end >= expiry_session:
            expected_end = expiry_session
            if last_bar_date >= expiry_session:
                return PartialWindowVerdict(
                    verdict=NOT_TRUNCATED,
                    last_bar_date=last_bar_date,
                    expected_end_date=expected_end,
                    missing_sessions=0,
                    reason="at_expiry",
                )

    # LIVE-EDGE exclusion: the expected end is within the live-edge horizon of today, so
    # the vendor may simply not have published the most recent sessions yet — a missing
    # tail here is expected, never a truncation.
    today_session = last_completed_session_on_or_before(today, observed=observed)
    if sessions_between(expected_end, today_session, observed=observed) < live_edge:
        return PartialWindowVerdict(
            verdict=NOT_TRUNCATED,
            last_bar_date=last_bar_date,
            expected_end_date=expected_end,
            missing_sessions=0,
            reason="live_edge",
        )

    # COMPLETE-TO-BOUND: the last bar reaches the expected end (sparse-but-complete is
    # COMPLETE — bar DENSITY is never consulted, only temporal coverage).
    missing = sessions_between(last_bar_date, expected_end, observed=observed)
    if missing == 0:
        return PartialWindowVerdict(
            verdict=NOT_TRUNCATED,
            last_bar_date=last_bar_date,
            expected_end_date=expected_end,
            missing_sessions=0,
            reason="complete",
        )

    # TRUNCATION: the last bar lags the expected end. Flag ONLY when the gap exceeds the
    # conservative tolerance — a small trailing gap (a holiday cluster, a publish-lag day)
    # stays NOT_TRUNCATED (sparse_complete), only a material truncation is SUSPECTED.
    if missing > tolerance:
        return PartialWindowVerdict(
            verdict=SUSPECTED_PARTIAL,
            last_bar_date=last_bar_date,
            expected_end_date=expected_end,
            missing_sessions=missing,
            reason="truncated",
        )
    return PartialWindowVerdict(
        verdict=NOT_TRUNCATED,
        last_bar_date=last_bar_date,
        expected_end_date=expected_end,
        missing_sessions=missing,
        reason="sparse_complete",
    )


#: The structlog event a SUSPECTED-PARTIAL (truncated-but-marked-SUCCESS) window emits
#: (observe-only; DEC-PARTIAL-WINDOW-DETECT-001). ONE WARNING for human triage — it
#: changes NO status / ledger / skip / engine input. Shared by BOTH the spot
#: (`ingest.py`) and the futures (`option_ingest.py`) wiring (CLAUDE.md §11 COMMON).
SUSPECTED_PARTIAL_WINDOW_EVENT = "window.suspected_partial"
#: Emitted if the observe-only scan itself raises — it NEVER breaks the pull.
SUSPECTED_PARTIAL_WINDOW_ERROR_EVENT = "window.suspected_partial_error"


def observe_partial_window(
    log: object,
    *,
    last_bar_ts: datetime | None,
    window_end: date,
    now: datetime,
    expiry: date | None,
    symbol: str,
    interval: str,
    pull_id: str | None,
    observed: AbstractSet[date] = frozenset(),
) -> PartialWindowVerdict | None:
    """WARN if a SUCCESS window's bars stop materially before its end; return the verdict.

    The ONE shared wiring both pull spines call (CLAUDE.md §11 COMMON — no copy):
    runs :func:`classify_partial_window` over the window's last-stored-bar timestamp
    vs its end bound + the instrument's expiry, and emits ONE
    :data:`SUSPECTED_PARTIAL_WINDOW_EVENT` WARNING when (and only when) the window is
    a SUSPECTED truncation. Fully wrapped: ANY failure of the diagnostic is swallowed
    + logged (:data:`SUSPECTED_PARTIAL_WINDOW_ERROR_EVENT`), so it can NEVER block /
    slow / crash the pull.

    Returns the computed :class:`PartialWindowVerdict` (so a caller may, beyond the
    WARNING, DEMOTE a truncated SUCCESS window to a re-pullable ledger status —
    DEC-PARTIAL-WINDOW-REPULL-001), or ``None`` when the guarded scan itself raised.
    Emitting the WARNING is unconditional of what the caller does with the verdict —
    a caller that ignores the return value gets the byte-identical observe-only
    behaviour of DEC-PARTIAL-WINDOW-DETECT-001.

    *log* is the caller's structlog logger (typed ``object`` so this module need not
    import structlog — it is duck-typed for ``.warning(event, **fields)``). *last_bar_ts*
    is the SESSION-FILTERED last bar's UTC timestamp (or ``None`` for an empty window).
    *expiry* is the contract expiry (``None`` for a spot series). The caller is
    responsible for the SCOPE gate (spot + futures only — options are excluded at the
    wiring; see the module docstring).
    """
    try:
        verdict = classify_partial_window(
            last_bar_ts=last_bar_ts,
            window_end=window_end,
            now=now,
            expiry=expiry,
            observed=observed,
        )
        if verdict.is_suspected_partial():
            log.warning(  # type: ignore[attr-defined]
                SUSPECTED_PARTIAL_WINDOW_EVENT,
                pull_id=pull_id,
                symbol=repr(symbol),
                interval=interval,
                window_end=window_end.isoformat(),
                last_bar_date=(
                    verdict.last_bar_date.isoformat() if verdict.last_bar_date else None
                ),
                expected_end_date=(
                    verdict.expected_end_date.isoformat() if verdict.expected_end_date else None
                ),
                missing_sessions=verdict.missing_sessions,
                expiry=expiry.isoformat() if expiry else None,
            )
        return verdict
    except Exception as exc:  # observe-only diagnostic must never break the pull
        log.warning(  # type: ignore[attr-defined]
            SUSPECTED_PARTIAL_WINDOW_ERROR_EVENT, pull_id=pull_id, error=str(exc)
        )
        return None
