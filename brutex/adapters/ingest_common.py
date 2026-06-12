"""Vendor-neutral ingest utilities shared by the Dhan + Groww spot/option spines.

PR-J1 of the Dhan→Groww cutover (CLAUDE.md §11 COMMON — one source of truth, no
copy-paste between modules). This module is the single home for the ingest
machinery that has zero vendor coupling, so the Groww package no longer imports
from the Dhan package (and vice-versa) for any of it:

* **Window tiling** — :class:`DateWindow` + :func:`plan_windows` (the generic,
  symbol-free ``[start, end)`` tiler). The Dhan ``chunks`` module pins it to the
  Dhan 90/30-day caps; the Groww ``chunks`` module pins it to the Groww tier
  table; both import the type + tiler from here.
* **Per-interval fetch-window builder + clamp** — :func:`tile_windows`,
  :func:`clamp_window_end`, :func:`now_ist`, and the IST/market-close constants.
  Both vendors tile/clamp against the SAME NSE/BSE 15:30 IST reality; the only
  divergence (the per-interval window width) is INJECTED via *window_days_for*.
  The coverage-ledger market namespace (``"dhan"`` / ``"groww"``) is an explicit
  ARGUMENT, never a module constant, so the ledger key is unambiguous per vendor.
* **Conflict summary** — :func:`summarize_conflicts` (pure, takes a sequence of
  :class:`~brutex.data.parquet_writer.BarConflict`).
* **Post-run leftover-window auto-retry** — :func:`retry_failed_windows` +
  :class:`RetryOutcome` / :class:`RetryRound`. The known-gap classification (a
  Dhan ``DH-905`` concept) is an INJECTED predicate (*is_known_gap*), so the
  engine itself stays vendor-neutral: Dhan passes its ``is_no_data_message``-based
  predicate, Groww passes nothing (no known-gap concept → nothing is a known gap).

Everything here is **behaviour-preserving** — moved verbatim from the Dhan
modules, with the only change being the two injection seams (*is_known_gap*,
*window_days_for* / *market*) that let the one implementation serve both vendors.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from time import sleep as _time_sleep
from typing import TYPE_CHECKING, Protocol
from zoneinfo import ZoneInfo

import structlog

if TYPE_CHECKING:
    from brutex.data.parquet_writer import BarConflict

#: Structured logger — lights each retry round (``ingest.auto_retry.round``) and
#: the final residual (``ingest.auto_retry.done``) under the pull's correlation
#: scope, so an operator can grep the recovery end-to-end (DEC-INGEST-AUTO-RETRY-001).
_LOG = structlog.get_logger("brutex.adapters.ingest_common")


# --------------------------------------------------------------------------- #
# Window tiling (vendor-neutral, symbol-free).
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class DateWindow:
    """A half-open ``[start, end)`` fetch window, ISO-8601 string bounds."""

    start: str
    end: str


def plan_windows(start: datetime, end: datetime, *, window_days: int) -> list[DateWindow]:
    """Tile ``[start, end)`` into windows no wider than *window_days*.

    Parameters
    ----------
    start, end
        Range to cover. If ``start >= end`` the result is empty.
    window_days
        Maximum width (days) of each emitted window. Must be positive.

    Returns
    -------
    list[DateWindow]
        Contiguous, non-overlapping windows (ISO-string bounds); the final
        window is clipped to *end*.

    Raises
    ------
    ValueError
        If *window_days* is not positive.
    """
    if window_days <= 0:
        raise ValueError(f"window_days must be positive, got {window_days}")
    windows: list[DateWindow] = []
    cursor = start
    span = timedelta(days=window_days)
    while cursor < end:
        window_end = min(cursor + span, end)
        windows.append(DateWindow(start=cursor.isoformat(), end=window_end.isoformat()))
        cursor = window_end
    return windows


# --------------------------------------------------------------------------- #
# Per-interval fetch-window builder + current-day clamp (vendor-neutral).
# --------------------------------------------------------------------------- #
#: Canonical daily interval — the only interval not tiled/session-filtered.
_DAILY: str = "1day"

#: IST clock pieces for the settled-data clamp (DEC-DAILY-CANDLE-CLAMP-001). The
#: NSE/BSE market is the same for every vendor, so the clamp reality is shared.
_IST_ZONE = ZoneInfo("Asia/Kolkata")
_MARKET_CLOSE_IST: time = time(15, 30)


def now_ist() -> datetime:
    """The current IST wall-clock (the clamp's production default)."""
    return datetime.now(tz=_IST_ZONE)


def clamp_window_end(interval: str, end: date, *, now_ist: datetime) -> date:
    """Clamp an end-EXCLUSIVE pull bound so unsettled CURRENT-DAY data is never requested.

    NSE/BSE reality (DEC-DAILY-CANDLE-CLAMP-001, operator-verified 2026-06-10 —
    the live window ledger showed a same-evening 1day request for "today"
    rejected with a vendor bad-request error):

    * the CURRENT day's **1-day** candle is published only the NEXT day —
      requesting it same-day burns a quota hit and 400s, so the daily bound
      is clamped to ``today`` (end-exclusive ⇒ the last requested candle is
      yesterday's);
    * the CURRENT day's **1-minute** bars are partial until the 15:30 IST
      close — pulling a partial day would settle the window SUCCESS in the
      ledger and the skip rule would never re-fetch the rest of the day, so
      before close the intraday bound is clamped to ``today`` too; from
      15:30 IST the full session exists and ``today + 1 day`` is allowed.

    Pure + clock-injected — every branch unit-testable offline.
    """
    today = now_ist.date()
    if interval == _DAILY:
        return min(end, today)
    if now_ist.time() >= _MARKET_CLOSE_IST:
        return min(end, today + timedelta(days=1))
    return min(end, today)


#: Resolve a canonical interval to its per-call max-window width (days), or
#: ``None`` to emit ONE un-tiled window for the whole range (the daily case in
#: the Dhan spine). The vendor injects this so the per-interval tier policy stays
#: vendor-owned while the tiling mechanism is shared.
WindowDaysFor = Callable[[str], "int | None"]


def tile_windows(
    interval: str, start: date, end: date, *, window_days_for: WindowDaysFor
) -> list[tuple[date, date]]:
    """Tile ``[start, end)`` into fetch windows for *interval*.

    The vendor-neutral twin of the per-spine ``_windows`` helpers: it resolves the
    interval's max-window width via *window_days_for*, then tiles via
    :func:`plan_windows`, decoding the ISO :class:`DateWindow` bounds back to
    :class:`datetime.date` (the meaningful granularity for the ledger). A
    *window_days_for* result of ``None`` means "one un-tiled window over the whole
    range" (the Dhan daily case, where the daily endpoint takes the full range in
    one call).

    Parameters
    ----------
    interval
        The canonical candle interval.
    start, end
        Half-open date range.
    window_days_for
        Vendor-supplied ``(interval) -> int | None`` resolver: a positive width
        tiles at that many days; ``None`` emits a single ``[start, end)`` window.
    """
    width = window_days_for(interval)
    if width is None:
        return [(start, end)]
    planned = plan_windows(
        datetime.combine(start, time()), datetime.combine(end, time()), window_days=width
    )
    return [(date.fromisoformat(w.start[:10]), date.fromisoformat(w.end[:10])) for w in planned]


# --------------------------------------------------------------------------- #
# Keep-first conflict summary (vendor-neutral, pure).
# --------------------------------------------------------------------------- #
#: OHLCV+OI fields compared bar-for-bar to detect a keep-first conflict — the
#: SAME tuple :meth:`ParquetWriter._bar_values_equal` compares, kept here so the
#: summary reports WHICH field a vendor re-statement touched (CLAUDE.md §11 COMMON).
_CONFLICT_FIELDS: tuple[str, ...] = ("open", "high", "low", "close", "volume", "open_interest")


def summarize_conflicts(conflicts: Sequence[BarConflict]) -> dict[str, object]:
    """Aggregate a window's keep-first conflicts into ONE log payload.

    DEC-LAKE-CONFLICT-LOG-AGG-001. The prior per-bar ``ingest.conflict`` WARNING
    flooded the log (hundreds of lines on a re-pull) AND printed only ``close`` —
    so a bar whose ``open``/``high``/``low`` was re-stated by the vendor looked
    like a *false* conflict (equal closes). This collapses a window's conflicts to
    one summary: the count, the sorted set of OHLCV+OI fields that ACTUALLY differ,
    the max absolute close delta, and the first/last conflicting timestamps.

    Parameters
    ----------
    conflicts
        The window's :class:`~brutex.data.parquet_writer.BarConflict` records
        (stored kept, vendor discarded — keep-first, Golden Rule #7).

    Returns
    -------
    dict
        ``conflicts`` (count), ``differing_fields`` (comma-joined, sorted),
        ``max_close_delta`` (str Decimal), ``first_ts`` / ``last_ts`` (ISO or None).
    """
    differing: set[str] = set()
    max_close_delta = Decimal(0)
    for c in conflicts:
        for field_name in _CONFLICT_FIELDS:
            if getattr(c.stored, field_name) != getattr(c.vendor, field_name):
                differing.add(field_name)
        max_close_delta = max(max_close_delta, abs(c.vendor.close - c.stored.close))
    timestamps = [c.timestamp for c in conflicts]
    return {
        "conflicts": len(conflicts),
        "differing_fields": ",".join(sorted(differing)),
        "max_close_delta": str(max_close_delta),
        "first_ts": min(timestamps).isoformat() if timestamps else None,
        "last_ts": max(timestamps).isoformat() if timestamps else None,
    }


# --------------------------------------------------------------------------- #
# Post-run leftover-window auto-retry (vendor-neutral round loop).
# --------------------------------------------------------------------------- #
class _RetryReport(Protocol):
    """The minimal read surface :func:`retry_failed_windows` needs off a pass report.

    Both :class:`~brutex.adapters.dhan.run_ingest.DhanIngestReport` and
    :class:`~brutex.adapters.groww.run_ingest.GrowwIngestReport` satisfy it
    structurally (the engine is duck-typed over these three members).
    """

    @property
    def still_failed_windows(self) -> int:
        """Transient windows still failing after this pass — the retryable residual."""
        ...

    @property
    def auth_aborted(self) -> bool:
        """Whether the token died mid-pass (halts the loop)."""
        ...

    @property
    def failures(self) -> Sequence[object]:
        """The pass's quarantined per-pair failures (scanned by *is_known_gap*)."""
        ...


#: Default bounded round budget. Small + finite (Golden Rule #10 — never an
#: infinite loop): after the in-run per-window passes already tried, a handful of
#: spaced rounds is enough to drain the residual of a multi-minute 429 storm once
#: the burst is over. The operator can raise it, but it is ALWAYS bounded.
_DEFAULT_MAX_ROUNDS: int = 3

#: Base inter-round backoff (seconds). Round *k* (1-based) waits ``base × 2**(k-1)``
#: clamped to :data:`_MAX_ROUND_BACKOFF_S` — SECONDS-to-minutes scale so a sustained
#: throttle has time to clear (the controller has STEPPED DOWN by now), vs the
#: in-run pass backoff's faster 1 s → 2 s cadence.
_DEFAULT_ROUND_BACKOFF_S: float = 5.0

#: Hard ceiling (seconds) on a single inter-round backoff — defends an
#: operator-raised round budget from an unbounded sleep.
_MAX_ROUND_BACKOFF_S: float = 60.0


def _default_round_backoff(round_index: int) -> None:
    """Sleep the bounded exponential backoff before retry round *round_index* (1-based).

    Injected as the default ``backoff`` hook so production waits a real,
    throttle-recovery-scale gap between rounds while tests pass a no-op recorder
    (no real sleep) — the same inject-the-sleep pattern as the in-run retry's
    per-window backoff.
    """
    _time_sleep(min(_DEFAULT_ROUND_BACKOFF_S * (2.0 ** (round_index - 1)), _MAX_ROUND_BACKOFF_S))


@dataclass(frozen=True, slots=True)
class RetryRound:
    """One auto-retry round's residual accounting (DEC-INGEST-AUTO-RETRY-001).

    A round is one full ingest pass over the leftover windows; these counts come
    straight off its vendor ingest report.
    """

    #: 1-based round ordinal (round 1 is the FIRST re-pull after the original pull).
    round_index: int
    #: Transient windows still failing after this round (the next round's worklist,
    #: 0 when clean) — the retryable residual that drives the loop.
    still_failed_windows: int
    #: Known-gap quarantines this round (e.g. a Dhan ``DH-905`` no-data) — NOT
    #: retried (left as-is). Always 0 when no *is_known_gap* predicate is supplied.
    known_gaps: int
    #: Whether the token died mid-round (an auth failure) — halts the loop.
    auth_aborted: bool


@dataclass(frozen=True, slots=True)
class RetryOutcome:
    """Outcome of a :func:`retry_failed_windows` run (MANUAL or AUTOMATIC).

    The same value object both the CLI ``--retry-failed`` trigger and the
    post-pull automatic path receive, so the two report identically (CLAUDE.md
    §11 COMMON).
    """

    #: Transient residual BEFORE any retry round (the original pull's leftover).
    initial_still_failed: int
    #: Transient residual AFTER the last round ran (0 ⇒ fully recovered).
    final_still_failed: int
    #: Number of retry rounds actually run (0 when there was nothing to retry).
    rounds_run: int
    #: Per-round residual accounting, in run order.
    rounds: tuple[RetryRound, ...]
    #: True iff the loop ended with ZERO transient residual (coverage clean).
    clean: bool
    #: True iff the loop halted because the token died mid-round (re-auth needed).
    auth_aborted: bool

    @property
    def recovered_windows(self) -> int:
        """Transient windows recovered across all rounds (initial − final, clamped ≥ 0).

        Clamped at 0 because a later round can, in principle, surface a NEW
        transient window the original pull had not yet reached (the residual is a
        live count, not a fixed worklist), so the difference is reported as
        "net recovered", never negative.
        """
        return max(self.initial_still_failed - self.final_still_failed, 0)


def _count_known_gaps(report: _RetryReport, is_known_gap: Callable[[object], bool] | None) -> int:
    """Count this pass's KNOWN-GAP quarantines — NOT retried.

    A known gap is an EXPECTED data boundary (the vendor genuinely has no data for
    the range), not a transient fault, so it must never drive another retry round
    (the honest-envelope rule, DEC-INGEST-AUTO-RETRY-001). The classification is
    INJECTED via *is_known_gap*: the Dhan caller passes a predicate over its
    quarantined failures' serialised error (delegating to
    :func:`~brutex.adapters.dhan.errors.is_no_data_message`), while a vendor with
    NO known-gap concept (Groww) passes ``None`` ⇒ nothing is a known gap.
    """
    if is_known_gap is None:
        return 0
    return sum(1 for failure in report.failures if is_known_gap(failure))


def retry_failed_windows(
    run_pass: Callable[[], _RetryReport],
    *,
    max_rounds: int = _DEFAULT_MAX_ROUNDS,
    backoff: Callable[[int], None] | None = None,
    is_known_gap: Callable[[object], bool] | None = None,
) -> RetryOutcome:
    """Re-pull ONLY the leftover failed windows in bounded rounds until clean (or budget out).

    The single underlying engine BOTH the automatic (post-pull, default-on) and
    the manual (``fetch --retry-failed`` / dashboard button) paths call (CLAUDE.md
    §11 COMMON), for BOTH vendors. Each round invokes *run_pass* — one full ingest
    pass wired to the SAME ``coverage`` ledger, so every already-completed window
    is skipped before the vendor call and only the still-failed windows are
    re-fetched (incremental + idempotent, DEC-FETCH-COVERAGE-001). The loop runs
    another round while a round leaves TRANSIENT residual windows
    (``still_failed_windows`` — 429 / timeout / 5xx) AND the budget is not spent
    AND the token has not died; it stops early on a clean round or an auth death.
    Known-gap quarantines (classified by *is_known_gap*) are counted but NEVER
    retried.

    **Honest envelope (Golden Rule #10):** O(failed_windows), NOT O(1)-total — a
    round re-fetches every residual window; the round COUNT is bounded by
    *max_rounds*, never infinite. Rate-safety is inherited from *run_pass* (the
    shared ``pace`` bucket keeps every round ``≤ 5 r/s``); the inter-round
    *backoff* waits a throttle out rather than fighting it.

    Parameters
    ----------
    run_pass
        A zero-arg callable that runs ONE ingest pass over the leftover windows
        and returns its vendor ingest report (duck-typed via :class:`_RetryReport`
        — reads ``still_failed_windows`` / ``auth_aborted`` / ``failures``). The
        caller binds the adapter / sink / instruments / intervals / the SHARED
        ``coverage`` ledger + ``pace`` bucket into it.
    max_rounds
        Bounded retry-round budget (default :data:`_DEFAULT_MAX_ROUNDS`). ``0``
        runs the loop zero times — used to DISABLE the automatic retry while
        keeping the same code path (``--no-auto-retry`` maps here). The loop runs
        at most this many rounds regardless of residual (never infinite).
    backoff
        Injected ``(round_index) -> None`` sleep hook called BEFORE each round
        (``None`` → :func:`_default_round_backoff`, a bounded exponential
        seconds-scale wait). Tests pass a no-op recorder so no real sleep occurs.
    is_known_gap
        Optional ``(failure) -> bool`` predicate classifying a quarantined failure
        as a benign KNOWN GAP (never a retryable residual). ``None`` (the default,
        used by vendors with no known-gap concept) treats nothing as a known gap.

    Returns
    -------
    RetryOutcome
        The initial + final transient residual, the rounds run + their per-round
        accounting, and whether the loop ended clean or on an auth death. When
        *max_rounds* is ``0`` the outcome carries ``rounds_run=0`` and
        ``initial_still_failed == final_still_failed == 0`` (nothing probed).
    """
    sleep = backoff if backoff is not None else _default_round_backoff
    rounds: list[RetryRound] = []
    initial_still_failed = 0
    final_still_failed = 0
    auth_aborted = False
    round_index = 0
    while round_index < max_rounds:
        round_index += 1
        sleep(round_index)
        report = run_pass()
        still_failed = report.still_failed_windows
        known_gaps = _count_known_gaps(report, is_known_gap)
        round_auth_aborted = report.auth_aborted
        if round_index == 1:
            initial_still_failed = still_failed
        final_still_failed = still_failed
        rounds.append(
            RetryRound(
                round_index=round_index,
                still_failed_windows=still_failed,
                known_gaps=known_gaps,
                auth_aborted=round_auth_aborted,
            )
        )
        _LOG.info(
            "ingest.auto_retry.round",
            retry_round=round_index,
            still_failed_windows=still_failed,
            known_gaps=known_gaps,
            auth_aborted=round_auth_aborted,
        )
        if round_auth_aborted:
            # The token died mid-round — retrying is futile (a token refresh cannot
            # revive a dead token), so halt and surface the residual for re-auth.
            auth_aborted = True
            break
        if still_failed == 0:
            # Coverage clean — every transient window recovered; stop early.
            break
    clean = final_still_failed == 0 and not auth_aborted and round_index > 0
    _LOG.info(
        "ingest.auto_retry.done",
        rounds_run=round_index,
        initial_still_failed=initial_still_failed,
        final_still_failed=final_still_failed,
        clean=clean,
        auth_aborted=auth_aborted,
    )
    return RetryOutcome(
        initial_still_failed=initial_still_failed,
        final_still_failed=final_still_failed,
        rounds_run=round_index,
        rounds=tuple(rounds),
        clean=clean,
        auth_aborted=auth_aborted,
    )


__all__ = [
    "DateWindow",
    "RetryOutcome",
    "RetryRound",
    "WindowDaysFor",
    "clamp_window_end",
    "now_ist",
    "plan_windows",
    "retry_failed_windows",
    "summarize_conflicts",
    "tile_windows",
]
