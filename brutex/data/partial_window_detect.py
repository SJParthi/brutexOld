"""Observe-only diagnostic — flag a fetch window the pull marks SUCCESS but TRUNCATED.

DEC-PARTIAL-WINDOW-DETECT-001 (MIGRATION.md #1004). The window classifier
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

False-positive safety (the crux — read this before touching the threshold)
--------------------------------------------------------------------------
A naive "expected bar count vs actual bar count" check WOULD false-flag a
genuinely-sparse illiquid option (a far-OTM strike legitimately trades a handful
of bars over its whole life) and could imply a giant wasteful re-pull. We DO NOT
do that. The flag fires ONLY on a strong **TRUNCATION** signal, never on
sparsity:

* The signal is *temporal coverage*, not *bar density*. We compare the date of
  the LAST stored bar against where the last bar SHOULD be — the window's end
  boundary, clamped to the contract's expiry/delisting and to the last completed
  trading session. A contract whose bars run up to its expiry (or to the window
  end) is COMPLETE **even if it only has a few bars** (sparse-but-complete is NOT
  flagged). A window whose last bar stops materially earlier than that bound —
  with the contract still live well past it — is the truncation case.
* The truncation must exceed a conservative, env-tunable tolerance of *trading
  sessions* (:data:`PARTIAL_WINDOW_TOLERANCE_SESSIONS`, env
  ``BRUTEX_PARTIAL_WINDOW_TOLERANCE_SESSIONS``, default 5). One or two missing
  trailing sessions (a holiday cluster, a vendor publish-lag day) never trip it.
* Explicitly EXCLUDED (each returns :data:`NOT_TRUNCATED`):
  - an EMPTY window (no stored bars — already handled by the EMPTY status);
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
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from brutex.calendars.nse import IST, is_trading_day

__all__ = [
    "NOT_TRUNCATED",
    "PARTIAL_WINDOW_LIVE_EDGE_SESSIONS",
    "PARTIAL_WINDOW_TOLERANCE_SESSIONS",
    "SUSPECTED_PARTIAL",
    "PartialWindowVerdict",
    "classify_partial_window",
    "last_completed_session_on_or_before",
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


def last_completed_session_on_or_before(d: date) -> date:
    """Return the latest NSE trading day ``<= d`` — the last completed session by *d*.

    Walks backwards day-by-day from *d* until :func:`brutex.calendars.nse.is_trading_day`
    is True (a weekend / holiday rolls back to the prior session). Bounded: the NSE
    week always contains a trading day, so this returns within a handful of steps
    (O(1) amortised — never an unbounded loop). Used to map a calendar end boundary
    onto the trading-session grid the bars actually live on.
    """
    cursor = d
    while not is_trading_day(cursor):
        cursor -= timedelta(days=1)
    return cursor


def sessions_between(start: date, end: date) -> int:
    """Count NSE trading days in the half-open ``(start, end]`` — sessions AFTER *start*.

    Returns the number of trading sessions strictly after *start* up to and
    including *end* (``0`` when ``end <= start``). This is the "how many sessions did
    the last bar miss" measure: *start* is the last stored bar's session, *end* the
    expected-end session, so the result is the trailing gap in sessions (a holiday
    span between the two contributes nothing — only real sessions count). O(days in
    span); the spans here are bounded by a single contract window.
    """
    if end <= start:
        return 0
    count = 0
    cursor = start + timedelta(days=1)
    while cursor <= end:
        if is_trading_day(cursor):
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

    # The window's end boundary is END-EXCLUSIVE, so the last bar's session is the last
    # completed trading day STRICTLY BEFORE window_end. The expected end is that session,
    # additionally clamped to the contract's expiry (a dead contract has no bars past it)
    # and to today (the vendor cannot publish a future session). Whichever bound is
    # EARLIEST wins — the conservative never-flag-past-a-real-limit choice.
    end_exclusive = window_end - timedelta(days=1)
    if expiry is not None and expiry < end_exclusive:
        end_exclusive = expiry
    end_exclusive = min(end_exclusive, today)
    expected_end = last_completed_session_on_or_before(end_exclusive)

    # AT-EXPIRY exclusion: the window's expected end coincides with (or sits at/after) the
    # contract's expiry session. A bar cannot exist past a dead contract, so the last bar
    # stopping there is COMPLETE, never a truncation — even if it lands a session or two
    # short of the raw calendar end_exclusive.
    if expiry is not None:
        expiry_session = last_completed_session_on_or_before(expiry)
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
    today_session = last_completed_session_on_or_before(today)
    if sessions_between(expected_end, today_session) < live_edge:
        return PartialWindowVerdict(
            verdict=NOT_TRUNCATED,
            last_bar_date=last_bar_date,
            expected_end_date=expected_end,
            missing_sessions=0,
            reason="live_edge",
        )

    # COMPLETE-TO-BOUND: the last bar reaches the expected end (sparse-but-complete is
    # COMPLETE — bar DENSITY is never consulted, only temporal coverage).
    missing = sessions_between(last_bar_date, expected_end)
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
