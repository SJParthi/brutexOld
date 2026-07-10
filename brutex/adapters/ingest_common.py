"""Vendor-neutral ingest utilities shared by the Groww spot/option spines.

PR-J1 of the Groww cutover (CLAUDE.md §11 COMMON — one source of truth, no
copy-paste between modules). This module is the single home for the ingest
machinery that has zero vendor coupling, so the Groww package no longer imports
any of it from a vendor-specific package:

* **Window tiling** — :class:`DateWindow` + :func:`plan_windows` (the generic,
  symbol-free ``[start, end)`` tiler). The 90/30-day intraday/option caps pin it
  to those widths; the Groww ``chunks`` module pins it to the Groww tier table;
  both import the type + tiler from here.
* **Per-interval fetch-window builder + clamp** — :func:`tile_windows`,
  :func:`clamp_window_end`, :func:`now_ist`, and the IST/market-close constants.
  Both spines tile/clamp against the SAME NSE/BSE 15:30 IST reality; the only
  divergence (the per-interval window width) is INJECTED via *window_days_for*.
  The coverage-ledger market namespace is an explicit ARGUMENT, never a module
  constant, so the ledger key is unambiguous per vendor.
* **Conflict summary** — :func:`summarize_conflicts` (pure, takes a sequence of
  :class:`~brutex.data.parquet_writer.BarConflict`).
* **Post-run leftover-window auto-retry** — :func:`retry_failed_windows` +
  :class:`RetryOutcome` / :class:`RetryRound`. The known-gap classification (a
  no-data-window concept) is an INJECTED predicate (*is_known_gap*), so the
  engine itself stays vendor-neutral: a vendor with a no-data signal passes an
  ``is_no_data_message``-based predicate, Groww passes nothing (no known-gap
  concept → nothing is a known gap).

PR-J4a additionally relocated here (a behaviour-preserving MOVE out of the
original vendor package so a later slice can delete it — CLAUDE.md §11 COMMON):

* **Synthetic spot source** — :func:`synthetic_spot_bars` (pure offline bar
  generator) + :class:`SyntheticSpotAdapter` (a :class:`MarketAdapter` over it),
  the dry-run data source (no token / no network).
* **Pull verdict** — :class:`PullVerdict` / :data:`PullVerdictKind` /
  :func:`classify_pull` (the actionable, prioritised finished-pull verdict).
* **Re-pull-until-clean** — :func:`run_until_clean` + :class:`UntilCleanRound` /
  :class:`UntilCleanOutcome` / :func:`decide_until_clean` and the round budgets.
* **Coverage-key namespace + per-interval window width** — :data:`_MARKET` (the
  lake coverage-key namespace, value UNCHANGED from its original home) +
  :func:`window_days_for_interval` (the per-call tiling width policy), both
  consumed by :mod:`brutex.data.window_ledger_store`.

Everything here is **behaviour-preserving** — moved verbatim from the original
vendor modules, with the only change being the two injection seams
(*is_known_gap*, *window_days_for* / *market*) that let the one implementation
serve both vendors.
"""

from __future__ import annotations

import enum
import threading
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from time import sleep as _time_sleep
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol, TypeAlias
from zoneinfo import ZoneInfo

import structlog

from brutex.adapters.protocol import (
    DerivativeContract,
    InstrumentPage,
    MarketCapabilities,
    single_instrument_page,
)
from brutex.data.models import CandleBar
from brutex.data.window_ledger import CONFIRMED_EMPTY_MIN_FETCHES
from brutex.models import BruteXError, Exchange, Instrument

if TYPE_CHECKING:
    from brutex.data.parquet_writer import BarConflict
    from brutex.orchestrator.audit import AuditWriter

#: Structured logger — lights each retry round (``ingest.auto_retry.round``) and
#: the final residual (``ingest.auto_retry.done``) under the pull's correlation
#: scope, so an operator can grep the recovery end-to-end (DEC-INGEST-AUTO-RETRY-001).
_LOG = structlog.get_logger("brutex.adapters.ingest_common")


def maybe_audit(
    audit: AuditWriter | None,
    action: str,
    subject: str,
    payload: dict[str, object],
) -> None:
    """Append one hash-chained audit line when an *audit* writer is wired.

    The ONE vendor-neutral guard BOTH ingest spines share (CLAUDE.md §11
    COMMON — DEC-INGEST-AUDIT-WIRING-001; moved here from the spot spine's
    private ``run_ingest._maybe_audit`` so the F&O spine reuses it instead of
    copying). A no-op when ``audit is None`` (a dry-run / inspect / synthetic
    pull carries no writer). Called ONLY from the single controller thread
    (each spine's fan-out drain loop), so the not-thread-safe
    :class:`~brutex.orchestrator.audit.AuditWriter` is never touched
    concurrently and the O(1)-per-entry append never runs on — and can never
    block — a fetch worker (Golden Rule #8).
    """
    if audit is not None:
        audit.append(action=action, subject=subject, payload=payload)


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

#: Public alias of :data:`_DAILY` for cross-module consumers (the vendor-neutral
#: "this is the daily interval" constant). The 1-day-first window bounding
#: (DEC-SPOT-1DAY-FIRST-001) must NEVER bound the daily interval, so
#: :mod:`brutex.data.window_ledger_store` imports THIS public name rather than the
#: private ``_DAILY`` (one source of truth, no ``reportPrivateUsage``).
DAILY_INTERVAL: str = _DAILY

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

    At-close boundary (DEC-SESSION-CLOSE-BOUNDARY-PIN-001, pinned by
    ``tests/unit/test_session_close_boundary_pin.py``): exactly 15:30:00
    counts as CLOSED here (``>=``) — DELIBERATELY the OPPOSITE of the
    finalize-side :func:`brutex.calendars.nse.daily_derive_cutoff` (strict
    ``>``, at-close = still forming). Request-side vs finalize-side rationale:
    ALLOWING the ``today + 1`` end-exclusive bound at 15:30:00 is safe — no
    bar at/after 15:30 can exist under the half-open ``[open, close)`` ingest
    session filter, and the partial-window detector + keep-first sink guard
    downstream — while FINALIZING a daily bar at 15:30:00 could bake in a
    15:29 bar not yet pulled. The two verdicts are a pinned, cross-referenced
    pair (the #1267 drift-pin pattern), not hidden duplication. The date-blind
    hardcoded 15:30 here remains defended by the #1492 G2 Muhurat enumeration
    pin; calendar-awareness on this pull-planning side is a separate decision,
    explicitly NOT claimed.

    Pure + clock-injected — every branch unit-testable offline.
    """
    today = now_ist.date()
    if interval == _DAILY:
        return min(end, today)
    if now_ist.time() >= _MARKET_CLOSE_IST:
        return min(end, today + timedelta(days=1))
    return min(end, today)


def drop_bars_after(bars: Sequence[CandleBar], window_end: date) -> list[CandleBar]:
    """Drop every bar whose IST trading date is STRICTLY AFTER *window_end* — a FUTURE phantom.

    DEC-CANDLE-DROP-FUTURE-BAR-001. *window_end* is the CLAMPED window end (the
    spine never requests current-day / future data — :func:`clamp_window_end`). A
    vendor that returns a bar timestamped on a date STRICTLY AFTER *window_end* is
    handing back a bar PAST the requested window — which, if written, would create a
    PHANTOM FUTURE partition (e.g. a clock-skew bar, or a bar for a day past the clamp).
    This pure helper drops any such bar BEFORE the durable write so the lake can never
    hold a future-dated phantom.

    The bound is STRICTLY-AFTER (``> window_end``), NOT on-or-after: a bar landing
    exactly on *window_end* is KEPT, preserving the spine's existing boundary behaviour
    byte-for-byte (this is a DEFENSIVE future-phantom guard, not a re-interpretation of
    the half-open ``[start, end)`` tiling — only a bar genuinely PAST the clamp is
    dropped). The IST trading date (not the UTC date) is used because bars are stored
    UTC but trade on the IST session grid (CLAUDE.md §6) and *window_end* is an IST
    calendar date, so the comparison must be on the same grid. O(bars) over one window;
    pure (no I/O). DEFENSIVE: the real Groww SDK returns in-range bars and is not
    observed to return future-dated rows — this guards against unobserved vendor
    corruption / a clock-skew edge, never a current crash.
    """
    kept: list[CandleBar] = []
    for bar in bars:
        if bar.timestamp.astimezone(_IST_ZONE).date() <= window_end:
            kept.append(bar)
    return kept


#: Resolve a canonical interval to its per-call max-window width (days), or
#: ``None`` to emit ONE un-tiled window for the whole range (the daily case in
#: the ingest spine). The vendor injects this so the per-interval tier policy stays
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
    range" (the daily case, where the daily endpoint takes the full range in
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
# Coverage-key namespace + per-interval tiling width (relocated into this
# vendor-neutral home in PR-J4a so ``window_ledger_store`` no longer imports a
# vendor-specific package). Behaviour-preserving — the namespace VALUE and the
# width policy are byte-identical to their original home (changing _MARKET's value
# is a data-migration event — the lake coverage keys are stamped with it).
# --------------------------------------------------------------------------- #
#: The default spot data-source namespace stamped on a coverage :class:`WindowKey`
#: when no explicit ``market`` is supplied. The live Groww spot spine passes
#: :data:`GROWW_COVERAGE_MARKET` explicitly; this constant is the vendor-neutral
#: fallback for the generic ledger machinery. PUBLIC (no leading underscore) so
#: cross-module consumers (the ingest spine, the window ledger store) import it
#: WITHOUT a private-access warning.
COVERAGE_MARKET: str = "spot"

#: The Groww-spine coverage namespace stamped on every Groww :class:`WindowKey`
#: (``"groww"``), kept distinct from :data:`COVERAGE_MARKET` so the two namespaces
#: never collide on the shared lake ledger. PUBLIC so the Groww ingest
#: spine AND the ledger-seeding helpers (``plan_windows`` /
#: ``reconcile_ledger_with_lake``) share ONE literal (CLAUDE.md §11 COMMON) — the
#: dashboard + CLI Groww spot pulls pass it so the PLANNED rows they seed match
#: the SUCCESS rows the Groww spine records (PR-J4b).
GROWW_COVERAGE_MARKET: str = "groww"

#: Intraday per-call cap (days). The 90-day width policy is owned LOCALLY here so
#: it needs no vendor import; the value mirrors the verified 90-day intraday cap.
_INTRADAY_WINDOW_DAYS: int = 90


def window_days_for_interval(interval: str) -> int | None:
    """Resolve an interval to its tiling width — ``None`` (single window) for daily.

    The intraday per-call cap is a flat 90 days; the daily endpoint takes
    the whole range in ONE call (``None`` ⇒ :func:`tile_windows` emits a single
    un-tiled window). Injected into the shared tiler so the tiling MECHANISM is
    vendor-neutral while this width policy stays owned here (relocated verbatim
    from the original per-vendor window helper in PR-J4a).
    """
    return None if interval == _DAILY else _INTRADAY_WINDOW_DAYS


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

    Each vendor's ingest report (e.g.
    :class:`~brutex.adapters.groww.run_ingest.GrowwIngestReport`) satisfies it
    structurally (the engine is duck-typed over these three members).
    """

    @property
    def still_failed_windows(self) -> int:
        """Transient windows still failing after this pass — the retryable residual."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never called

    @property
    def auth_aborted(self) -> bool:
        """Whether the token died mid-pass (halts the loop)."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never called

    @property
    def failures(self) -> Sequence[object]:
        """The pass's quarantined per-pair failures (scanned by *is_known_gap*)."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never called


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
    #: Known-gap quarantines this round (e.g. a vendor no-data window) — NOT
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
    INJECTED via *is_known_gap*: a vendor with a no-data signal passes a predicate
    over its quarantined failures' serialised error (an ``is_no_data_message``-style
    check), while a vendor with NO known-gap concept (Groww) passes ``None`` ⇒
    nothing is a known gap.
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


# --------------------------------------------------------------------------- #
# Synthetic spot source (relocated from the original vendor ``synthetic`` +
# ``synthetic_adapter`` modules in PR-J4a — the dry-run, offline data source).
# --------------------------------------------------------------------------- #
#: India Standard Time — the synthetic bars use the NSE/BSE wall clock.
_IST: ZoneInfo = ZoneInfo("Asia/Kolkata")

#: Session open (09:15 IST) — the first synthetic bar's wall-clock time.
_SESSION_OPEN: time = time(9, 15)


def synthetic_spot_bars(
    day: date,
    *,
    count: int = 5,
    base_price: Decimal = Decimal("100"),
    interval_minutes: int = 1,
) -> list[CandleBar]:
    """Generate *count* valid in-session synthetic spot bars for *day*.

    Emits valid synthetic 1-minute :class:`~brutex.data.models.CandleBar` rows so
    the real parse → session-filter → sink pipeline runs **offline** (no token, no
    network) — the dry-run proof the pipe works before any credentials are set.
    Pure (no I/O); timestamps start at **09:15 IST** (session open) and step by one
    minute, so every bar survives the session filter (CLAUDE.md §7).

    Parameters
    ----------
    day
        The trading day the bars belong to.
    count
        Number of bars to emit (must be > 0).
    base_price
        The first bar's open; each subsequent bar steps +1 rupee.
    interval_minutes
        Spacing between bars in minutes (default 1).

    Returns
    -------
    list[CandleBar]
        *count* tz-aware (UTC) bars starting at 09:15 IST, each with a valid
        ``low <= open, close <= high`` OHLC and a small synthetic volume.

    Raises
    ------
    ValueError
        If *count* is not positive.
    """
    if count <= 0:
        raise ValueError(f"count must be positive, got {count}")
    start = datetime.combine(day, _SESSION_OPEN, tzinfo=_IST)
    bars: list[CandleBar] = []
    for i in range(count):
        open_px = base_price + i
        bars.append(
            CandleBar(
                timestamp=(start + timedelta(minutes=i * interval_minutes)).astimezone(UTC),
                open=open_px,
                high=open_px + 1,
                low=open_px - 1,
                close=open_px + Decimal("0.5"),
                volume=1000 + i,
                open_interest=None,
            )
        )
    return bars


#: Stable capabilities for the synthetic (dry-run) adapter — a faithful mirror of
#: the live adapter (spot only, 1m + 1d fetched, others resampled), so the
#: orchestrator plans the dry-run pull exactly as it would the live one.
_SYNTHETIC_CAPABILITIES: MarketCapabilities = MarketCapabilities(
    market_id="spot",
    display_name="Groww (synthetic dry-run)",
    exchanges=frozenset({Exchange.NSE, Exchange.BSE}),
    has_derivatives=False,
    has_expiries=False,
    has_option_chains=False,
    supports_intraday=True,
    earliest_history=date(2020, 1, 1),
    native_timezone="Asia/Kolkata",
    rate_limit_rps=5.0,
    supported_intervals=frozenset({"1minute", "1day"}),
)


@dataclass(frozen=True, slots=True)
class SyntheticSpotAdapter:
    """A :class:`~brutex.adapters.protocol.MarketAdapter` backed by synthetic bars.

    Drop-in for the live market adapter on the dry-run path: same protocol, but
    :meth:`fetch_ohlcv` returns :func:`synthetic_spot_bars` instead of hitting the
    network, so the whole parse → session-filter → sink pipeline runs **offline**.
    Pure (no I/O): every call is deterministic, so it is side-effect-free.

    Parameters
    ----------
    instruments
        The static universe surfaced by :meth:`list_instruments`. Empty by
        default — the pull spine fans out over an explicit instrument list, so
        the adapter need not carry one.
    bars_per_window
        Number of synthetic in-session bars emitted per fetched window
        (default 5).
    """

    instruments: tuple[Instrument, ...] = ()
    bars_per_window: int = 5

    @property
    def capabilities(self) -> MarketCapabilities:
        """Return the stable synthetic (dry-run) capabilities."""
        return _SYNTHETIC_CAPABILITIES

    def list_instruments(self, *, cursor: str | None = None) -> InstrumentPage:
        """Return the whole static universe in one page (cursors exhausted)."""
        return single_instrument_page(self.instruments, cursor)

    def list_derivatives(
        self, *, underlying_symbol: str, exchange: Exchange
    ) -> Iterator[DerivativeContract]:
        """Yield nothing — the dry-run adapter is spot (``has_derivatives=False``)."""
        del underlying_symbol, exchange
        return iter(())

    def fetch_ohlcv(
        self,
        *,
        symbol: str,
        exchange: Exchange,
        interval: str,
        start: date,
        end: date,
    ) -> list[CandleBar]:
        """Return *bars_per_window* synthetic in-session bars for *start*'s day.

        The arguments other than *start* are accepted to satisfy the
        :class:`~brutex.adapters.protocol.MarketAdapter` contract but do not
        affect the synthetic output — the bars are a deterministic offline
        stand-in, partitioned downstream by the caller's ``(symbol, interval)``.
        """
        del symbol, exchange, interval, end
        return synthetic_spot_bars(start, count=self.bars_per_window)


# --------------------------------------------------------------------------- #
# Pull verdict (relocated from the original vendor ``pull_verdict`` module in
# PR-J4a — the actionable, prioritised finished-pull verdict).
# --------------------------------------------------------------------------- #
#: The mutually-exclusive, priority-ordered outcomes of a finished pull — PLUS
#: ``credentials_missing``, the PRE-pull refusal verdict: a REAL pull that could
#: not even start because the Groww credentials did not resolve
#: (DEC-PULL-LOUD-CREDENTIAL-REFUSAL-001). It is produced ONLY by
#: :func:`credentials_refusal_verdict` / :func:`gate_pull_credentials`, never by
#: :func:`classify_pull` (which classifies pulls that RAN).
PullVerdictKind = Literal[
    "credentials_missing",
    "auth_expired",
    "mass_empty_halt",
    "quota_exhausted",
    "transient_residual",
    "quarantined_symbols",
    "known_gap_only",
    "clean",
]

#: The operator-facing headline + next action for each kind (DEC-PULL-VERDICT-001).
#: Kept as data (not f-strings) so the message is deterministic + test-pinnable;
#: dynamic counts are carried as structured fields on :class:`PullVerdict`.
_HEADLINE: Final[dict[str, str]] = {
    "auth_expired": "🔑 Groww auth failed mid-pull — the pull aborted.",
    "mass_empty_halt": (
        "🚨 Pull HALTED — mass empty-response circuit breaker tripped "
        "(suspected vendor soft-throttle or mis-addressing)."
    ),
    "quota_exhausted": "📊 Daily Groww quota reached — auto-resume paused for today.",
    "transient_residual": "⏳ Some windows are still pending after retries.",
    "quarantined_symbols": "✅ Pull complete — some symbols were quarantined (not on Groww).",
    "known_gap_only": "ℹ️ No new bars — these symbols have no intraday on Groww.",
    "clean": "✅ Pull complete.",
}
_ACTION: Final[dict[str, str]] = {
    "auth_expired": (
        "Re-check the Groww API key + TOTP secret, then re-pull. "
        "A retry can NEVER succeed on bad credentials — only valid credentials can."
    ),
    "mass_empty_halt": (
        "The vendor answered a mass of windows with clean-but-EMPTY responses — either a "
        "soft-throttle serving empty 200s [R2], mis-addressed requests [R3], or a genuine "
        "pre-listing gap; the code cannot tell which, so NOTHING more was settled as "
        "SKIPPED and every un-settled window stays retryable. Wait out a possible "
        "throttle, verify the symbol addressing, then re-pull; if windows were settled "
        "before the halt, `brutex reopen-skipped` re-opens them for re-verification "
        "(`--limit N` bounds the re-open to a sample). Re-opened (REOPENED) windows are "
        "deliberate re-checks the breaker never counts, so reopen + re-pull cannot "
        "re-trip endlessly; if a genuine no-data universe keeps tripping, the thresholds "
        "are env-tunable (BRUTEX_EMPTY_BREAKER_*) and BRUTEX_EMPTY_BREAKER=off is the "
        "explicit kill switch."
    ),
    "quota_exhausted": (
        "The 100,000/day Groww quota is spent — the auto-resume paused itself "
        "(it will NOT keep burning a dead quota). Re-pull after the IST-midnight "
        "reset; already-pulled windows are skipped, so only the gaps re-fetch."
    ),
    "transient_residual": (
        "Re-pull to finish — already-pulled windows are skipped and only the "
        "still-pending windows are re-fetched (auto-retry mops up transient 429s)."
    ),
    "quarantined_symbols": (
        "No re-pull needed — these symbols are not on Groww (e.g. a renamed/delisted "
        "ticker or a non-Groww constituent), so a retry can NEVER land a bar. Verify the "
        "symbol list; every resolvable symbol completed."
    ),
    "known_gap_only": (
        "No action needed — these are reference-only symbols Groww does not serve "
        "intraday (e.g. broad-market indices); they are not brute-force inputs."
    ),
    "clean": "No action needed.",
}

#: Settled-``SKIPPED`` count above which an otherwise-clean pull still flips
#: :attr:`PullVerdict.needs_operator` (DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001 minor
#: M-4). A pull that permanently wrote off MORE than this many windows in one go
#: deserves operator eyes even when nothing failed — a mass write-off is either a
#: legitimate first full backfill settling late-listing prefixes (confirm once,
#: then it never recurs — settles are transitions, not re-counts) or exactly the
#: throttle/mis-addressing shape the breaker exists for. The config knob is this
#: named constant (deliberately NOT env-read: the verdict layer stays pure +
#: deterministic — no I/O, no env, per :func:`classify_pull`'s contract).
SETTLED_SKIPPED_OPERATOR_THRESHOLD: Final[int] = 50


@dataclass(frozen=True, slots=True)
class PullVerdict:
    """A finished pull's single, prioritised, actionable verdict.

    Immutable + JSON-serialisable. ``kind`` is the worst (highest-priority)
    condition observed; ``pending_windows`` is the count of WINDOWS a re-pull will
    retry (the still-failed windows ONLY — NOT the quarantined pairs, which a
    re-pull can never recover; those are the separate ``failed_pairs`` field, per
    DEC-EMPTY-RESOLVED-REPORTING-001). ``auth_cause`` carries the originating
    error string ONLY for ``auth_expired`` (the vendor error) and
    ``credentials_missing`` (the relayed :class:`CredentialsError` fix text,
    DEC-PULL-LOUD-CREDENTIAL-REFUSAL-001); else ``None``.
    """

    kind: PullVerdictKind
    headline: str
    action: str
    pending_windows: int
    failed_pairs: int
    no_data: int
    completed: int
    auth_cause: str | None = None
    settled_skipped: int = 0
    """Windows THIS pull newly settled as terminal ``SKIPPED`` (DEC-EMPTY-WINDOW-
    CIRCUIT-BREAKER-001 honest-verdict rule). When > 0 the ``action`` text SAYS so
    (count + the ``brutex reopen-skipped`` re-verify hint) on EVERY kind — a pull
    that permanently settled windows may never read "No action needed"."""
    settled_exhausted: int = 0
    """The FETCH-EXHAUSTED subset of :attr:`settled_skipped` (review round 1,
    MAJOR 1.4 — DEC-AUTO-RESUME-ZERO-SURPRISES-001): windows settled because
    every fetch ERRORED :data:`~brutex.data.window_ledger.FAILED_TERMINAL_THRESHOLD`
    consecutive times (:attr:`~brutex.data.window_ledger.WindowLedger.newly_exhausted_failed`).
    The settle note names these SEPARATELY — calling them "vendor confirmed
    no-data" would be the exact lie the distinct ledger stamp exists to prevent
    (the vendor never confirmed anything). Always ``0 <= settled_exhausted <=
    settled_skipped`` from the ledger's tallies; a defensive clamp in the note
    builder keeps a violating caller from rendering a negative count."""

    @property
    def needs_operator(self) -> bool:
        """True iff a human must act before more data can land (the loud case).

        ``quota_exhausted`` counts: the day's quota is spent, so the operator must
        wait for the IST-midnight reset (or re-pull then) — the auto-resume cannot
        finish today on its own. ``credentials_missing`` counts for the same
        reason (DEC-PULL-LOUD-CREDENTIAL-REFUSAL-001): only the operator can fix
        the credentials — no retry can ever land a bar without them.
        ``mass_empty_halt`` counts (DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001): the pull
        halted on a suspected soft-throttle / mis-addressing episode — blindly
        re-pulling into the same episode is exactly what the breaker prevents.
        A ``settled_skipped`` count ABOVE
        :data:`SETTLED_SKIPPED_OPERATOR_THRESHOLD` also counts, on EVERY kind
        (minor M-4): a pull that permanently wrote off that many windows must be
        confirmed by a human once — the ``action`` text already names the count
        and the ``brutex reopen-skipped`` re-verify path.
        """
        if self.settled_skipped > SETTLED_SKIPPED_OPERATOR_THRESHOLD:
            return True
        return self.kind in (
            "credentials_missing",
            "auth_expired",
            "mass_empty_halt",
            "quota_exhausted",
            "transient_residual",
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly dict for the pull-all route / structured log."""
        return {
            "kind": self.kind,
            "headline": self.headline,
            "action": self.action,
            "needs_operator": self.needs_operator,
            "pending_windows": self.pending_windows,
            "failed_pairs": self.failed_pairs,
            "no_data": self.no_data,
            "completed": self.completed,
            "auth_cause": self.auth_cause,
            "settled_skipped": self.settled_skipped,
            "settled_exhausted": self.settled_exhausted,
        }


def classify_pull(
    *,
    auth_aborted: bool,
    still_failed_windows: int,
    failed_pairs: int,
    no_data: int,
    completed: int,
    auth_cause: str | None = None,
    quota_exhausted: bool = False,
    mass_empty_cause: str | None = None,
    settled_skipped: int = 0,
    settled_exhausted: int = 0,
) -> PullVerdict:
    """Classify a finished pull into ONE prioritised, actionable :class:`PullVerdict`.

    Priority (worst first), so the operator always sees the most urgent action:

    1. **auth_expired** — the token died mid-pull (``auth_aborted``). Most urgent:
       no retry can recover until the token is regenerated. ``pending_windows``
       counts the still-failed windows + quarantined pairs left behind.
    2. **mass_empty_halt** — the mass-empty circuit breaker tripped
       (``mass_empty_cause`` set, DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001): the vendor
       answered a mass of windows with clean-but-empty 200s — a suspected
       soft-throttle [R2] / mis-addressing [R3] episode. The pull HALTED before
       cementing more windows; re-pulling blindly into the same episode is what the
       breaker prevents, so the operator must triage first. ``mass_empty_cause``
       (the halt's own count-naming message) is PREFIXED to the action.
    3. **quota_exhausted** — the day's 100,000-call quota is spent
       (``quota_exhausted``) with windows still pending: the auto-resume paused
       itself rather than burn a dead quota; the operator re-pulls after the
       IST-midnight reset (DEC-AUTO-RESUME-QUOTA-001). Ranked just under a dead
       token because, like it, NO amount of immediate retrying can land a bar
       today — only the reset can.
    4. **transient_residual** — retryable WINDOWS are still failing
       (``still_failed_windows`` > 0) but the token is alive: a re-pull / the
       auto-retry layer will finish them. The loud "re-pull to finish" verdict.
    5. **quarantined_symbols** — NO retryable windows remain; the only "failures"
       are quarantined symbols (``failed_pairs`` > 0, e.g. a ticker not on Groww):
       a re-pull can NEVER recover them, so this is a COMPLETE pull with a note,
       NOT "re-pull to finish" (``needs_operator`` is False — grey, not amber).
    6. **known_gap_only** — nothing completed and nothing failed, but the vendor
       returned empty for some symbols (``no_data``): the vendor genuinely has no
       intraday for them (reference-only). Expected, not an error.
    7. **clean** — everything that could complete did.

    *settled_skipped* (DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001 honest-verdict rule) is
    the count of windows THIS pull newly settled as terminal ``SKIPPED``
    (:attr:`~brutex.data.window_ledger.WindowLedger.newly_settled_skipped`). When
    > 0 the verdict's ``action`` — on EVERY kind, including ``clean`` — names the
    count and the ``brutex reopen-skipped`` re-verify path: a pull that permanently
    settled windows may never read as a bare "No action needed".

    *settled_exhausted* (review round 1, MAJOR 1.4 —
    DEC-AUTO-RESUME-ZERO-SURPRISES-001) is the FETCH-EXHAUSTED subset of
    *settled_skipped*
    (:attr:`~brutex.data.window_ledger.WindowLedger.newly_exhausted_failed`):
    windows settled because every fetch ERRORED. The note names them
    SEPARATELY — never as "permanent no-data" (the vendor never confirmed
    anything for them). Both production verdict builders thread the pair from
    the ONE per-pull ledger instance.

    Pure + deterministic (no I/O / clock). ``auth_cause`` is preserved ONLY on
    the ``auth_expired`` verdict.
    """
    if auth_aborted:
        return _make_verdict(
            "auth_expired",
            still_failed_windows,
            failed_pairs,
            no_data,
            completed,
            auth_cause,
            settled_skipped=settled_skipped,
            settled_exhausted=settled_exhausted,
        )
    if mass_empty_cause is not None:
        # The breaker's own message carries the counts (empty/observed/consecutive) —
        # prefix it so the banner NAMES them (the register's "surface settle counts").
        verdict = _make_verdict(
            "mass_empty_halt",
            still_failed_windows,
            failed_pairs,
            no_data,
            completed,
            None,
            settled_skipped=settled_skipped,
            settled_exhausted=settled_exhausted,
        )
        return replace(verdict, action=f"{mass_empty_cause} {verdict.action}")
    if quota_exhausted:
        return _make_verdict(
            "quota_exhausted",
            still_failed_windows,
            failed_pairs,
            no_data,
            completed,
            None,
            settled_skipped=settled_skipped,
            settled_exhausted=settled_exhausted,
        )
    if still_failed_windows > 0:
        # Genuine retryable WINDOWS remain (transient 429/timeout): a re-pull / the
        # auto-retry layer will land them. Quarantined pairs (if any) ride along as a note.
        return _make_verdict(
            "transient_residual",
            still_failed_windows,
            failed_pairs,
            no_data,
            completed,
            None,
            settled_skipped=settled_skipped,
            settled_exhausted=settled_exhausted,
        )
    if failed_pairs > 0:
        # No retryable windows — the ONLY "failures" are quarantined symbols (not on Groww,
        # e.g. BAJAJ-AUTO / NAM-INDIA). A re-pull can NEVER recover them, so this is a
        # COMPLETE pull with a note, NOT "re-pull to finish" (DEC-EMPTY-RESOLVED-REPORTING-001).
        return _make_verdict(
            "quarantined_symbols",
            still_failed_windows,
            failed_pairs,
            no_data,
            completed,
            None,
            settled_skipped=settled_skipped,
            settled_exhausted=settled_exhausted,
        )
    if completed == 0 and no_data > 0:
        return _make_verdict(
            "known_gap_only",
            still_failed_windows,
            failed_pairs,
            no_data,
            completed,
            None,
            settled_skipped=settled_skipped,
            settled_exhausted=settled_exhausted,
        )
    return _make_verdict(
        "clean",
        still_failed_windows,
        failed_pairs,
        no_data,
        completed,
        None,
        settled_skipped=settled_skipped,
        settled_exhausted=settled_exhausted,
    )


def _settled_skipped_note(settled_skipped: int, settled_exhausted: int = 0) -> str:
    """The honest-verdict settle note appended to EVERY verdict that settled windows.

    DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001: names the count + the re-verify path, so
    a permanent settle is never silent (register R2 — "surface settle counts in the
    verdict; make SKIPPED reversible"). Review round 1, MAJOR 1.4
    (DEC-AUTO-RESUME-ZERO-SURPRISES-001): *settled_exhausted* — the
    FETCH-EXHAUSTED subset of *settled_skipped* — is named SEPARATELY: those
    windows settled because EVERY fetch ERRORED, so folding them into the
    "permanent no-data" wording was factually false (the vendor never answered
    — exactly the lie the ledger's distinct settle stamp exists to prevent);
    only the remainder reads as vendor-confirmed no-data. Pure + O(1); the
    subset is defensively clamped into ``[0, settled_skipped]`` so a violating
    caller can never render a negative count.
    """
    exhausted = min(max(settled_exhausted, 0), settled_skipped)
    no_data = settled_skipped - exhausted
    parts: list[str] = []
    if no_data > 0:
        parts.append(
            f"NOTE: {no_data} window(s) were settled as permanent no-data (SKIPPED) this pull."
        )
    if exhausted > 0:
        parts.append(
            f"NOTE: {exhausted} window(s) settled after repeated fetch failures "
            "(SKIPPED, exhausted — every fetch ERRORED; the vendor never confirmed "
            "no-data): inspect the failure cause (each row's stamp names its last "
            "error), then `brutex reopen-skipped`."
        )
    parts.append(
        "If that is unexpected (a vendor soft-throttle or a mis-addressed "
        "symbol can fake no-data), run `brutex reopen-skipped` to re-open them "
        "(`--symbol`/`--market` scope it, `--limit N` bounds a sample re-verify), then "
        "re-pull — re-opened (REOPENED) windows are breaker-exempt deliberate "
        "re-checks, so the re-verify pull cannot re-trip the mass-empty breaker on them."
    )
    return " ".join(parts)


def _make_verdict(
    kind: PullVerdictKind,
    still_failed_windows: int,
    failed_pairs: int,
    no_data: int,
    completed: int,
    auth_cause: str | None,
    *,
    settled_skipped: int = 0,
    settled_exhausted: int = 0,
) -> PullVerdict:
    """Build a :class:`PullVerdict` for *kind* with the canonical headline + action.

    When *settled_skipped* > 0 the settle note REPLACES a bare "No action needed."
    action (never both — a settling pull is not action-free) and is APPENDED to any
    other action text (the primary instruction stays first). *settled_exhausted*
    (the FETCH-EXHAUSTED subset — review round 1, MAJOR 1.4) makes the note name
    exhausted windows separately from vendor-confirmed no-data settles.
    """
    action = _ACTION[kind]
    if settled_skipped > 0:
        note = _settled_skipped_note(settled_skipped, settled_exhausted)
        action = note if action == _ACTION["clean"] else f"{action} {note}"
    return PullVerdict(
        kind=kind,
        headline=_HEADLINE[kind],
        action=action,
        pending_windows=still_failed_windows,  # WINDOWS a re-pull retries — NOT pairs
        failed_pairs=failed_pairs,
        no_data=no_data,
        completed=completed,
        auth_cause=auth_cause,
        settled_skipped=settled_skipped,
        settled_exhausted=settled_exhausted,
    )


# --------------------------------------------------------------------------- #
# Loud credential refusal + explicit synthetic labeling
# (DEC-PULL-LOUD-CREDENTIAL-REFUSAL-001). The ONE credential gate every REAL
# pull surface (dashboard routes + CLI fetch) runs before touching the vendor —
# CLAUDE.md §11 COMMON. Before this decision the dashboard downgraded ANY
# credential failure to a silent synthetic dry-run that reported GREEN with
# counts while ZERO real bars landed; the CLI refused with a HARDCODED guess at
# the cause. Now both surfaces refuse with the CredentialsError's OWN fix text
# (relayed verbatim — DYNAMIC, no per-field hardcoding), and synthetic data
# runs ONLY when explicitly requested, labeled unmistakably.
# --------------------------------------------------------------------------- #

#: The loud label every explicitly-requested synthetic surface carries — the
#: progress detail, the CLI stderr line, and (via :func:`mark_synthetic`) the
#: finished-pull verdict headline. One constant so no surface can drift into a
#: quieter wording (CLAUDE.md §11 COMMON).
SYNTHETIC_DRY_RUN_LABEL: Final[str] = "SYNTHETIC DRY-RUN — no real data"

#: The refusal headline for a REAL pull whose credentials did not resolve.
#: Static + test-pinnable; the dynamic fix text rides in ``action``.
CREDENTIALS_REFUSAL_HEADLINE: Final[str] = (
    "🔒 Pull REFUSED — Groww credentials unavailable (nothing was fetched, nothing was written)."
)


def label_synthetic(detail: str) -> str:
    """Prefix *detail* with the loud synthetic label (explicit dry-run surfaces).

    Used on the background-job ``detail`` string (the progress title) and the
    CLI stderr line so an explicitly-requested synthetic run can NEVER be read
    as a real pull. Pure + O(1).
    """
    return f"{SYNTHETIC_DRY_RUN_LABEL} — {detail}"


def mark_synthetic(verdict: PullVerdict) -> PullVerdict:
    """Return *verdict* re-labeled as the outcome of an EXPLICIT synthetic dry-run.

    DEC-PULL-LOUD-CREDENTIAL-REFUSAL-001: a dry-run's finished verdict must SAY
    synthetic loudly — a bare "✅ Pull complete." over discarded synthetic bars
    is exactly the fake-green this decision kills. The classification ``kind``
    is preserved (the classifier stays the one authority); only the
    operator-facing ``headline`` / ``action`` are prefixed/suffixed with the
    unmistakable label. Pure + O(1).
    """
    return replace(
        verdict,
        headline=f"🧪 {SYNTHETIC_DRY_RUN_LABEL} — {verdict.headline}",
        action=(
            f"{verdict.action} (Explicit synthetic dry-run: the bars were generated "
            "offline and DISCARDED — zero real bars landed in the lake. Run a real "
            "pull with valid credentials for real data.)"
        ),
    )


def credentials_refusal_verdict(reason: str) -> PullVerdict:
    """Build the ``credentials_missing`` refusal verdict for a REAL pull.

    *reason* is the :class:`~brutex.config.credentials_types.CredentialsError`'s
    own message, relayed VERBATIM as the verdict ``action`` (and ``auth_cause``)
    — whatever env var / SSM parameter / AWS failure it names, no per-field
    hardcoding (DYNAMIC). ``needs_operator`` is ``True`` by construction: no
    retry can land a bar until the operator fixes the credentials.

    Parameters
    ----------
    reason
        The relayed credential-failure fix text (secret-free by the loader's
        contract).
    """
    return PullVerdict(
        kind="credentials_missing",
        headline=CREDENTIALS_REFUSAL_HEADLINE,
        action=(
            f"{reason} (Synthetic data runs ONLY on an explicit dry-run request — "
            "a real pull NEVER silently degrades to synthetic.)"
        ),
        pending_windows=0,
        failed_pairs=0,
        no_data=0,
        completed=0,
        auth_cause=reason,
    )


def verdict_message(verdict: PullVerdict) -> str:
    """One-line operator message for surfaces without a banner (CLI stderr / HTTP detail).

    The single headline+action join both refusal surfaces print, so the CLI and
    the dashboard can never phrase the same verdict differently (CLAUDE.md §11
    COMMON). Pure + O(1).
    """
    return f"{verdict.headline} {verdict.action}"


def gate_pull_credentials(
    *,
    dry_run_requested: bool,
    credentials_error: Callable[[], str | None] | None = None,
) -> PullVerdict | None:
    """The ONE credential gate every REAL pull surface runs before touching the vendor.

    Returns ``None`` when the pull may proceed: either an EXPLICIT synthetic
    dry-run was requested (credentials are irrelevant — the resolver is never
    called), or the Groww credentials resolve. Otherwise returns the loud
    ``credentials_missing`` refusal verdict carrying the
    :class:`~brutex.config.credentials_types.CredentialsError`'s own fix text —
    the caller MUST refuse (HTTP error / non-zero exit), NEVER downgrade to
    synthetic data (DEC-PULL-LOUD-CREDENTIAL-REFUSAL-001).

    Parameters
    ----------
    dry_run_requested
        ``True`` iff the operator EXPLICITLY asked for the synthetic dry-run
        (the ``?dry_run=true`` route param / the CLI ``--dry-run`` flag).
    credentials_error
        Injectable resolver returning ``None`` when the credentials resolve,
        else the relayed fix text (default:
        :func:`brutex.config.credentials.groww_credentials_error`). Injected so
        the gate is 100% offline-testable and stays import-light.

    Returns
    -------
    PullVerdict | None
        ``None`` (proceed) or the ``credentials_missing`` refusal verdict.
    """
    if dry_run_requested:
        return None
    resolver: Callable[[], str | None]
    if credentials_error is None:
        # Function-local default so importing this vendor-neutral module never
        # drags in the AWS credential chain (mirrors credentials.py's own lazy
        # one-directional import of credentials_aws).
        from brutex.config.credentials import groww_credentials_error

        resolver = groww_credentials_error
    else:
        resolver = credentials_error
    reason = resolver()
    if reason is None:
        return None
    return credentials_refusal_verdict(reason)


# --------------------------------------------------------------------------- #
# Re-pull-until-clean (relocated from the original vendor ``until_clean`` module
# in PR-J4a — drive bounded pull rounds until the coverage ledger is CLEAN).
# --------------------------------------------------------------------------- #
#: The pure controller's verdicts: keep going, or one of five terminal stops.
LoopVerdict = Literal[
    "continue",
    "stop_clean",
    "stop_dead",
    "stop_quota",
    "stop_no_progress",
    "stop_budget",
]

#: Default bounded OUTER round budget. Small + finite (Golden Rule #10 — never an
#: infinite loop): a handful of full re-pull rounds drains a multi-minute 429 storm
#: once the burst is over, AND gives a recent maybe-late window (a daily/option bar
#: the vendor publishes a day later) a few more whole-pull chances to land before
#: the run gives up. REVERTED 10→5 under DEC-REALDATA-ONLY-NO-1M-TO-1D-001
#: (MIGRATION.md #852): the 5→10 raise of DEC-RETRY-PERSIST-10-001 only existed to
#: keep grinding recent-stock ``1day`` empties long enough for the (now-removed)
#: 1m→1d resample to fill them; with real-Groww-data-only those empties are honestly
#: terminal, so the round budget is restored to 5 (it stays ``>=`` the empty
#: terminal threshold of 3 so the until-clean loop can still reach it). Each round
#: itself runs the bounded inner :func:`retry_failed_windows` loop, and the four
#: ``decide_until_clean`` guards (clean / dead / quota / no-progress) terminate long
#: before this cap in every real run, so the worst case is strictly bounded.
DEFAULT_MAX_ROUNDS: int = 5

#: Auto-resume OUTER round budget (DEC-AUTO-RESUME-QUOTA-001): the "keep re-firing
#: batches until 100% clean, zero clicks" path. Larger than
#: :data:`DEFAULT_MAX_ROUNDS` but STILL finite (Golden Rule #10 — never infinite),
#: because four guards terminate the loop long before this cap in every real run:
#: ``stop_clean`` (done), ``stop_dead`` (token died), ``stop_quota`` (daily quota
#: spent), and ``stop_no_progress`` (a round that shrank the residual by zero — a
#: stuck window can never be drained by more rounds). The cap is the final
#: backstop against a pathological never-clean residual, NOT the normal exit.
AUTO_RESUME_MAX_ROUNDS: int = 50

#: Base inter-round backoff (seconds): round *k* (1-based, only rounds ≥ 2 wait)
#: sleeps ``base × 2**(k-2)`` clamped to :data:`_UC_MAX_ROUND_BACKOFF_S` — a
#: throttle-recovery-scale gap so a sustained throttle clears between full re-pull
#: rounds (mirrors :func:`retry_failed_windows`'s per-round backoff).
_UC_DEFAULT_ROUND_BACKOFF_S: float = 5.0

#: Hard ceiling (seconds) on a single inter-round backoff — defends an
#: operator-raised round budget from an unbounded sleep.
_UC_MAX_ROUND_BACKOFF_S: float = 60.0


def _until_clean_default_backoff(round_index: int) -> None:
    """Sleep the bounded exponential backoff BEFORE re-pull round *round_index* (1-based).

    Only rounds ``≥ 2`` ever call this (the first round is the initial pull — no
    pre-wait), so round 2 waits ``base × 2**0``, round 3 ``base × 2**1``, … clamped
    to :data:`_UC_MAX_ROUND_BACKOFF_S`. Injected as the default ``sleep`` hook so
    production waits a real, throttle-recovery-scale gap while tests pass a no-op
    recorder (no real sleep).
    """
    _time_sleep(
        min(_UC_DEFAULT_ROUND_BACKOFF_S * (2.0 ** (round_index - 2)), _UC_MAX_ROUND_BACKOFF_S)
    )


@dataclass(frozen=True, slots=True)
class UntilCleanRound:
    """One outer round's residual accounting — what :func:`run_until_clean`'s runner returns.

    A round is one full pull-pass-with-auto-retry; these counts come straight off
    its report (the still-failed transient windows + whether the token died + the
    first dead-token cause).
    """

    #: Transient windows still failing after this round (0 ⇒ this round was clean).
    still_failed_windows: int
    #: Whether the token died mid-round (a 401/403 or ``DH-906``) — halts the loop.
    auth_aborted: bool
    #: The dead-token cause string (only set when ``auth_aborted``), for the verdict.
    auth_cause: str | None = None
    #: Genuinely-outstanding windows after this round (DEC-UNTIL-CLEAN-ALL-OUTSTANDING-001)
    #: — ``total - settled`` from the unified ledger: PLANNED (never fetched) + FAILED
    #: (transient) + EMPTY (below the SKIPPED threshold). The "pull until successful"
    #: signal: a round is CLEAN only when this is 0 (every window SUCCESS / SKIPPED), NOT
    #: merely when the transient ``still_failed_windows`` reaches 0 (which ignored
    #: PLANNED + EMPTY). ``None`` (the default) keeps every legacy caller — the CLI, the
    #: options path, ``historical_pull`` — on the prior ``still_failed_windows``-only
    #: behaviour (byte-identical); only the ledger-backed ``pull_all_spot`` round
    #: populates it.
    outstanding_windows: int | None = None
    #: Settled windows (SUCCESS + SKIPPED) after this round — the MONOTONIC progress
    #: measure for the outstanding path (it only ever rises). ``None`` for legacy rounds.
    settled_windows: int | None = None
    #: Sum of ``empty_streak`` across the ledger after this round — the SECONDARY progress
    #: measure: an EMPTY window stepping toward its SKIPPED threshold advances this WITHOUT
    #: yet raising ``settled_windows``, so a round that only progressed empty streaks still
    #: counts as progress (else the loop would ``stop_no_progress`` before a recent EMPTY
    #: window settles in ``EMPTY_TERMINAL_THRESHOLD`` rounds). ``None`` for legacy rounds.
    streak_total: int | None = None


@dataclass(frozen=True, slots=True)
class UntilCleanOutcome:
    """Outcome of a :func:`run_until_clean` drive (MANUAL ``--until-clean`` or AUTOMATIC).

    The same value object the CLI ``brutex fetch --until-clean`` trigger and the
    dashboard / ``pull_all_spot`` automatic path receive, so the two report
    identically (CLAUDE.md §11 COMMON).
    """

    #: The terminal verdict the loop ended on (never ``"continue"``).
    verdict: LoopVerdict
    #: Number of OUTER rounds actually run (0 when ``max_rounds <= 0`` disabled it).
    rounds_run: int
    #: Per-round residual accounting, in run order.
    rounds: tuple[UntilCleanRound, ...]

    @property
    def clean(self) -> bool:
        """True iff the loop reached a CLEAN coverage ledger (``stop_clean``)."""
        return self.verdict == "stop_clean"

    @property
    def auth_aborted(self) -> bool:
        """True iff the loop halted on a dead token (``stop_dead``) — re-auth needed."""
        return self.verdict == "stop_dead"

    @property
    def stopped_on_budget(self) -> bool:
        """True iff the loop stopped with residual after spending the round budget."""
        return self.verdict == "stop_budget"

    @property
    def stopped_on_quota(self) -> bool:
        """True iff the auto-resume paused on the daily quota (``stop_quota``).

        DEC-AUTO-RESUME-QUOTA-001 — the day's 100,000-call quota is spent;
        re-pull after the IST-midnight reset (the auto-resume will not burn a dead
        quota).
        """
        return self.verdict == "stop_quota"

    @property
    def stopped_no_progress(self) -> bool:
        """True iff the loop halted because a round shrank the residual by zero.

        DEC-AUTO-RESUME-QUOTA-001 — a stuck window can never be drained by more
        identical rounds, so the loop self-terminates instead of churning to the
        budget cap (this is what makes the large auto-resume budget safe).
        """
        return self.verdict == "stop_no_progress"

    @property
    def final_still_failed(self) -> int:
        """Transient residual after the LAST round ran (0 when none ran / clean)."""
        return self.rounds[-1].still_failed_windows if self.rounds else 0

    @property
    def auth_cause(self) -> str | None:
        """The dead-token cause from the last round (``None`` unless ``stop_dead``)."""
        return self.rounds[-1].auth_cause if self.rounds else None

    def pull_verdict(self) -> PullVerdict:
        """Map the final state to the shared :class:`PullVerdict` (DEC-PULL-VERDICT-001).

        Reuses :func:`classify_pull` (#485) — the SAME verdict vocabulary a
        finished pull emits (CLAUDE.md §11 COMMON): ``auth_expired`` when the token
        died, ``quota_exhausted`` when the auto-resume paused on the daily quota
        (DEC-AUTO-RESUME-QUOTA-001), ``transient_residual`` when the budget /
        no-progress guard stopped with residual left, ``clean`` when the ledger is
        clean. A ``stop_no_progress`` finish carries residual but a live token +
        quota, so it maps to ``transient_residual`` (re-pull / the next round mops
        it up) — exactly like ``stop_budget``.
        """
        return classify_pull(
            auth_aborted=self.auth_aborted,
            still_failed_windows=self.final_still_failed,
            failed_pairs=0,
            no_data=0,
            completed=0,
            auth_cause=self.auth_cause,
            quota_exhausted=self.stopped_on_quota,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly dict for a route / structured log."""
        return {
            "verdict": self.verdict,
            "rounds_run": self.rounds_run,
            "clean": self.clean,
            "auth_aborted": self.auth_aborted,
            "stopped_on_budget": self.stopped_on_budget,
            "stopped_on_quota": self.stopped_on_quota,
            "stopped_no_progress": self.stopped_no_progress,
            "final_still_failed": self.final_still_failed,
        }


def decide_until_clean(
    *,
    still_failed_windows: int,
    auth_aborted: bool,
    rounds_used: int,
    max_rounds: int,
    quota_exhausted: bool = False,
    made_progress: bool = True,
    outstanding_windows: int | None = None,
) -> LoopVerdict:
    """Decide what the until-clean loop does after a round — the pure 6-way controller.

    Priority (matching :func:`retry_failed_windows` + :func:`classify_pull`, worst
    first):

    1. **stop_dead** — ``auth_aborted``: the token died; retrying is futile
       (``RenewToken`` cannot revive a dead token) — halt for re-auth.
    2. **stop_clean** — ``still_failed_windows == 0``: the coverage ledger is clean
       (success outranks every "can't continue" reason — if we're done, we're done).
    3. **stop_quota** — ``quota_exhausted``: the day's 100,000-call quota is
       spent (DEC-AUTO-RESUME-QUOTA-001). Like a dead token, NO amount of immediate
       retrying can land a bar today — the auto-resume pauses itself rather than
       burn a dead quota; the operator re-pulls after the IST-midnight reset. Ranked
       above ``no_progress`` because it is the SPECIFIC reason no progress is
       possible.
    4. **stop_no_progress** — ``not made_progress``: the last round shrank the
       residual by ZERO. A stuck window can never be drained by more identical
       rounds, so spinning is futile — halt instead of churning to the budget cap.
       This is what makes a LARGE ``max_rounds`` (the auto-resume budget) safe: the
       loop self-terminates the instant it stops making headway.
    5. **stop_budget** — ``rounds_used >= max_rounds``: residual remains, the loop is
       still making progress, but the bounded outer budget is spent (the final
       backstop).
    6. **continue** — residual remains, progress is being made, budget is left.

    Pure + O(1) (a constant number of comparisons). No I/O, no clock. The
    ``quota_exhausted`` + ``made_progress`` args default to the
    pre-DEC-AUTO-RESUME-QUOTA-001 behaviour; ``outstanding_windows`` defaults to
    ``None`` (the pre-DEC-UNTIL-CLEAN-ALL-OUTSTANDING-001 behaviour), so every
    existing caller is byte-unchanged.

    *outstanding_windows* (DEC-UNTIL-CLEAN-ALL-OUTSTANDING-001) is the ledger-derived
    ``total - settled`` count — PLANNED + FAILED + EMPTY-below-threshold. When
    provided (the ``pull_all_spot`` ledger path), the ``stop_clean`` test keys on it
    instead of ``still_failed_windows``, so the loop keeps re-pulling until EVERY
    window is SUCCESS / SKIPPED (the "pull until successful" guarantee), not merely
    until the transient failures clear (which left PLANNED + EMPTY windows behind).
    ``None`` falls back to the ``still_failed_windows == 0`` test (byte-identical).
    """
    if auth_aborted:
        return "stop_dead"
    clean = (
        outstanding_windows == 0 if outstanding_windows is not None else still_failed_windows == 0
    )
    if clean:
        return "stop_clean"
    if quota_exhausted:
        return "stop_quota"
    if not made_progress:
        return "stop_no_progress"
    if rounds_used >= max_rounds:
        return "stop_budget"
    return "continue"


def run_until_clean(
    round_runner: Callable[[], UntilCleanRound],
    *,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
    sleep: Callable[[int], None] | None = None,
    decide: Callable[..., LoopVerdict] = decide_until_clean,
    quota_exhausted: Callable[[], bool] | None = None,
    stop_when_stuck: bool = False,
) -> UntilCleanOutcome:
    """Run bounded re-pull rounds until CLEAN, the token dies, or the budget is spent.

    The single driver BOTH the MANUAL (``brutex fetch --until-clean``) and the
    AUTOMATIC (default-off ``pull_all_spot(until_clean=True)`` / dashboard) paths
    call (CLAUDE.md §11 COMMON). Each round invokes *round_runner* — ONE full
    pull-pass-with-auto-retry over the SHARED coverage ledger, which itself reuses
    :func:`retry_failed_windows` (this driver NEVER re-implements the per-window
    round loop). After each round :func:`decide_until_clean` says continue /
    stop_clean / stop_dead / stop_budget; the loop sleeps the injected *sleep*
    backoff BEFORE every round after the first (the first round is the initial pull
    — no pre-wait).

    **Honest envelope (Golden Rule #10):** O(failed_windows) per round, NOT
    O(1)-total — a round re-fetches its residual; the OUTER round COUNT is bounded
    by *max_rounds*, never infinite. Rate-safety is inherited from *round_runner*
    (the shared ``≤ 5 r/s`` bucket); *sleep* waits a throttle out.

    Parameters
    ----------
    round_runner
        Zero-arg callable that runs ONE full pull pass (with the inner auto-retry)
        over the leftover windows and returns its :class:`UntilCleanRound`. The
        caller binds the adapter / sink / instruments / SHARED coverage ledger +
        pace bucket into it (so each round is rate-safe + re-fetches only failed
        windows). Called once per round.
    max_rounds
        Bounded OUTER round budget (default :data:`DEFAULT_MAX_ROUNDS`). ``<= 0``
        runs the loop ZERO times (a disabled no-op) and returns ``stop_budget``
        with ``rounds_run=0`` — never infinite regardless of residual.
    sleep
        Injected ``(round_index) -> None`` backoff hook called BEFORE each round
        AFTER the first (``None`` → :func:`_until_clean_default_backoff`, a bounded
        exponential seconds-scale wait). Tests pass a no-op recorder so no real
        sleep occurs.
    decide
        The verdict function (default :func:`decide_until_clean`); injectable for
        tests.
    quota_exhausted
        Optional ``() -> bool`` hook checked AFTER each round (DEC-AUTO-RESUME-
        QUOTA-001): ``True`` means the day's 100,000-call quota is spent, so the
        loop stops with ``stop_quota`` rather than re-firing batches that would only
        raise :class:`~brutex.orchestrator.quota_tracker.DailyQuotaExceeded`. ``None``
        (the default) → quota is never the limit (the pre-feature behaviour, for the
        bounded ``DEFAULT_MAX_ROUNDS`` path + tests). The hook reads the shared
        persisted :class:`~brutex.orchestrator.quota_tracker.DailyQuotaTracker`, so
        it reflects the SAME counter the rate bucket increments per call.
    stop_when_stuck
        When ``True`` (the auto-resume path, DEC-AUTO-RESUME-QUOTA-001), a round
        that shrinks the residual by ZERO stops the loop with ``stop_no_progress``
        — this is what makes the large :data:`AUTO_RESUME_MAX_ROUNDS` budget safe (a
        stuck window self-terminates instead of churning to the cap). ``False`` (the
        default — the bounded ``DEFAULT_MAX_ROUNDS`` path + every existing caller)
        keeps ``made_progress=True`` always, so the loop runs to ``stop_clean`` /
        ``stop_dead`` / ``stop_budget`` exactly as before (byte-unchanged: a
        constant-residual run still ends on ``stop_budget``).

    Returns
    -------
    UntilCleanOutcome
        The terminal verdict + the rounds run + their per-round accounting.
    """
    wait = sleep if sleep is not None else _until_clean_default_backoff
    rounds: list[UntilCleanRound] = []
    if max_rounds <= 0:
        # Disabled — run nothing, report a spent budget (mirrors retry_failed_windows's
        # max_rounds=0 opt-out, never infinite). Still terminally OBSERVED
        # (DEC-PULL-TELEMETRY-TRIAD-001): the disabled loop says so in /logs instead of
        # ending silently; ``outstanding=None`` — no round ran, so no residual was measured.
        _LOG.info("until_clean.done", rounds=0, outstanding=None, verdict="stop_budget")
        return UntilCleanOutcome(verdict="stop_budget", rounds_run=0, rounds=())
    round_index = 0
    verdict: LoopVerdict = "continue"
    #: The previous round's residual, so a round that shrank it by ZERO trips the
    #: ``stop_no_progress`` guard (what makes the large auto-resume budget safe).
    #: ``None`` before round 1 ⇒ round 1 always counts as progress (no baseline).
    prev_failed: int | None = None
    #: The previous round's (settled, streak_total) — the MONOTONIC progress measure for
    #: the outstanding path (DEC-UNTIL-CLEAN-ALL-OUTSTANDING-001). ``settled`` only ever
    #: rises; a round that left it flat but advanced an EMPTY window's streak still made
    #: progress (the window is stepping toward SKIPPED). ``None`` before round 1.
    prev_settled: int | None = None
    prev_streak: int = 0
    while True:
        round_index += 1
        if round_index > 1:
            # The first round IS the initial pull (no pre-wait); subsequent rounds
            # wait a bounded backoff so a sustained throttle clears between pulls.
            wait(round_index)
        outcome = round_runner()
        rounds.append(outcome)
        # No-progress is only consulted when the caller opts in (auto-resume); the
        # default path keeps ``made_progress=True`` so its behaviour is byte-
        # unchanged (a constant-residual run still ends on ``stop_budget``).
        if outcome.outstanding_windows is not None:
            # DEC-UNTIL-CLEAN-ALL-OUTSTANDING-001 ledger path: progress = ``settled``
            # strictly rose, OR ``settled`` held flat but the empty-streak advanced (an
            # EMPTY window stepping toward its SKIPPED threshold) — so a round that only
            # progressed EMPTY streaks is NOT mistaken for a stall (which would stop the
            # loop before a recent EMPTY window settles in EMPTY_TERMINAL_THRESHOLD
            # rounds). ``settled`` is monotonic, so it never falsely flags progress.
            cur_settled = outcome.settled_windows or 0
            cur_streak = outcome.streak_total or 0
            made_progress = (not stop_when_stuck) or (
                prev_settled is None
                or cur_settled > prev_settled
                or (cur_settled == prev_settled and cur_streak > prev_streak)
            )
            prev_settled = cur_settled
            prev_streak = cur_streak
        else:
            made_progress = (not stop_when_stuck) or (
                prev_failed is None or outcome.still_failed_windows < prev_failed
            )
        verdict = decide(
            still_failed_windows=outcome.still_failed_windows,
            auth_aborted=outcome.auth_aborted,
            rounds_used=round_index,
            max_rounds=max_rounds,
            quota_exhausted=quota_exhausted() if quota_exhausted is not None else False,
            made_progress=made_progress,
            outstanding_windows=outcome.outstanding_windows,
        )
        prev_failed = outcome.still_failed_windows
        # Per-round observability (DEC-PULL-TELEMETRY-TRIAD-001): the loop that rescues
        # FAILED windows overnight previously ran SILENT — one INFO per round makes its
        # convergence (or stall) readable live in /logs. ``outstanding`` is the ledger's
        # genuinely-outstanding count when the round carries it
        # (DEC-UNTIL-CLEAN-ALL-OUTSTANDING-001), else the legacy still-failed count.
        # Logging ONLY — the round count, verdicts and returned outcome are byte-identical.
        outstanding = (
            outcome.outstanding_windows
            if outcome.outstanding_windows is not None
            else outcome.still_failed_windows
        )
        _LOG.info(
            "until_clean.round",
            round=round_index,
            outstanding=outstanding,
            made_progress=made_progress,
            verdict=verdict,
        )
        if verdict != "continue":
            break
    # Terminal observability (DEC-PULL-TELEMETRY-TRIAD-001): one INFO naming the final
    # verdict + the residual the loop ended on, so an overnight run's outcome is one grep.
    _LOG.info("until_clean.done", rounds=round_index, outstanding=outstanding, verdict=verdict)
    return UntilCleanOutcome(verdict=verdict, rounds_run=round_index, rounds=tuple(rounds))


#: Shared default "pull until a non-empty response" budget — the number of TOTAL attempts an
#: empty-prone Groww call (candles / expiries / contracts) is re-fetched before its empty is
#: trusted as genuine no-data (DEC-EMPTY-RETRY-EVERYWHERE-001 — generalises the spot candle
#: empty-retry of DEC-EMPTY-INPULL-RETRY-001). 3 mirrors the spot candle default.
DEFAULT_EMPTY_RETRY_ATTEMPTS: Final[int] = 3

# CONFIRMED_EMPTY_MIN_FETCHES (the old-gap fast-settle confirmation threshold,
# DEC-OLD-GAP-FAST-SETTLE-001) is RE-EXPORTED above from its canonical home in
# :mod:`brutex.data.window_ledger` (DEC-DEAD-STRIKE-LEDGER-SETTLE-001): the ledger's
# ``record`` now applies the BUDGET-AWARE cumulative cross-pull settle against it, and
# this module already imports from ``brutex.data`` (the reverse import would be a
# cycle). Both spines keep importing it from HERE — one source of truth, one surface
# (CLAUDE.md §11 COMMON).

#: Old-gap fast-settle horizon (days) — DEC-OLD-GAP-FAST-SETTLE-001. A fetch window whose
#: ``window_end`` is more than this many days in the PAST can never gain vendor data: Groww
#: serves a stable 2020+ history and any backfill lag is DAYS, not months (CLAUDE.md §6 —
#: historical depth "available from 2020"). So an in-pull-confirmed-empty OLD window is a
#: PERMANENT gap, not a transient one, and the spines settle it ``SKIPPED`` after ONE pull
#: (via ``WindowLedger.record(settle_empty=True)``) instead of needing the conservative
#: 3-pull EMPTY-streak. A RECENT window (within this horizon) keeps the 3-pull streak — a
#: just-missed-backfill or still-settling edge MUST be allowed to fill on a later pull. The
#: 60-day floor is comfortably wider than any observed Groww backfill lag, so it never
#: fast-settles a gap that could still legitimately fill. Idempotent + monotonic: a window
#: only ages INTO "old", never back, so re-runs are stable (Golden Rule #7).
OLD_GAP_HORIZON_DAYS: int = 60


def _noop_empty_backoff(_attempt: int) -> None:
    """Default empty-retry backoff — a no-op (each *fetch* already paces itself)."""


def fetch_until_nonempty[T](
    fetch: Callable[[], T],
    *,
    is_empty: Callable[[T], bool],
    attempts: int = DEFAULT_EMPTY_RETRY_ATTEMPTS,
    backoff: Callable[[int], None] = _noop_empty_backoff,
) -> T:
    """Call *fetch*; if the result is EMPTY, re-fetch up to *attempts* total — first non-empty wins.

    The COMMON "pull until a successful (non-empty) response" primitive
    (``DEC-EMPTY-RETRY-EVERYWHERE-001``) — generalises the spot candle empty-retry
    (``DEC-EMPTY-INPULL-RETRY-001``) to EVERY empty-prone Groww call: historical candles,
    ``get_expiries``, and ``get_contracts``. A clean-but-empty vendor reply is AMBIGUOUS on a
    single call — genuine no-data vs a transient glitch on data that DOES exist (the exact case
    that would make a re-pull silently MISS a newly-listed weekly/monthly expiry) — so re-fetch
    up to *attempts* times; the FIRST non-empty result wins (the glitch is recovered), and only
    an all-empty run returns the (now confidently) empty result. BOUNDED: a truly-empty call
    fires exactly *attempts* times, never loops forever. *backoff(attempt)* runs BEFORE each
    re-fetch (attempts 2..N); the default is a no-op because each *fetch* already paces itself
    through the shared rate bucket. O(1) state — a fixed attempt budget, no accumulation.

    Parameters
    ----------
    fetch
        The (self-pacing) call producing a result; invoked ``1..attempts`` times.
    is_empty
        Predicate marking a result as the ambiguous empty to retry (e.g. ``not result``).
    attempts
        Total call budget. ``<= 1`` disables the retry (a single call); the default is
        :data:`DEFAULT_EMPTY_RETRY_ATTEMPTS`.
    backoff
        Invoked with the upcoming attempt index before each re-fetch (default no-op).

    Returns
    -------
    T
        The first non-empty result, or the last (empty) result once the budget is spent.
    """
    result = fetch()
    for attempt in range(2, attempts + 1):
        if not is_empty(result):
            return result
        backoff(attempt)
        result = fetch()
    return result


# --------------------------------------------------------------------------- #
# The six-step pull-order vocabulary (DEC-PULL-SIX-STEP-ORDER-001).
#
# Operator hard law (2026-07-06): whenever a Groww fetch pull runs — any
# selection — it executes INDICES-SPOT → STOCKS-SPOT → INDICES-FUTURES →
# STOCKS-FUTURES → INDICES-OPTIONS → STOCKS-OPTIONS, skipping empty groups.
# This leaf owns the ONE COMMON vocabulary (CLAUDE.md §11 COMMON) consumed by
# BOTH execution chokepoints: the spot spine's group loop (steps 1-2, wired in
# ``pull_controller.pull_all_spot``) and the F&O bucket sort (steps 3-6, wired
# in ``historical_pull``). Everything here is pure / observe-only — ordering
# POLICY (the strict post-step gate) lives in the sequencer, not here.
# --------------------------------------------------------------------------- #


@enum.unique
class PullStep(enum.IntEnum):
    """The operator's strict pull order (DEC-PULL-SIX-STEP-ORDER-001). value IS the rank."""

    INDICES_SPOT = 1
    STOCKS_SPOT = 2
    INDICES_FUTURES = 3
    STOCKS_FUTURES = 4
    INDICES_OPTIONS = 5
    STOCKS_OPTIONS = 6

    @property
    def label(self) -> str:
        """The lowercase-hyphenated step name used in every ``pull.step.*`` event."""
        return _PULL_STEP_LABELS[self]


#: The ONE label table — event field values for ``pull.step.started`` /
#: ``pull.step.settled`` / ``pull.step.halted`` and the ordering diagnostics.
_PULL_STEP_LABELS: Final[dict[PullStep, str]] = {
    PullStep.INDICES_SPOT: "indices-spot",
    PullStep.STOCKS_SPOT: "stocks-spot",
    PullStep.INDICES_FUTURES: "indices-futures",
    PullStep.STOCKS_FUTURES: "stocks-futures",
    PullStep.INDICES_OPTIONS: "indices-options",
    PullStep.STOCKS_OPTIONS: "stocks-options",
}

#: One ordered spot step group — the step plus its instruments, input order kept.
#: Classic ``TypeAlias`` (not a PEP 695 ``type`` statement) because this alias is
#: exported via ``__all__`` and CodeQL's Python extractor does not recognize a
#: ``type`` statement as a definition (explicit-export alert on the export list).
SpotStepGroup: TypeAlias = tuple[PullStep, tuple[Instrument, ...]]  # noqa: UP040


def partition_spot_steps(instruments: Sequence[Instrument]) -> tuple[SpotStepGroup, ...]:
    """Ordered non-empty (INDICES_SPOT, STOCKS_SPOT) groups; empty groups OMITTED.

    Total classification via :attr:`~brutex.models.Instrument.is_index`
    (``IDX`` ⇒ indices; everything else ⇒ stocks). Stable (input order preserved
    within a group); every instrument lands in EXACTLY one group (the
    property-test invariant). Pure, O(n). India VIX (IDX, reference-only) rides
    INDICES_SPOT; an NTM+F&O dual-class EQ name rides STOCKS and STAYS tradable;
    reference-only filtering is untouched (it happens before this partition).
    """
    indices: list[Instrument] = []
    stocks: list[Instrument] = []
    for inst in instruments:
        if inst.is_index:
            indices.append(inst)
        else:
            stocks.append(inst)
    groups: list[SpotStepGroup] = []
    if indices:
        groups.append((PullStep.INDICES_SPOT, tuple(indices)))
    if stocks:
        groups.append((PullStep.STOCKS_SPOT, tuple(stocks)))
    return tuple(groups)


class StepOrderError(BruteXError):
    """A pull-step rank regression under ``StepOrderMonitor(raise_on_violation=True)``.

    Raised ONLY by the test oracle — the production monitor logs + counts and
    never raises (the #907/#919 detect-and-warn pattern).
    """


class StepOrderMonitor:
    """Observe-only ordering oracle for the six-step pull law.

    Thread-safe (one lock), O(1) per call. :meth:`begin_step` — a rank
    regression logs ERROR ``pull.step.order_violation`` (or raises
    :class:`StepOrderError` when ``raise_on_violation=True`` — the TEST
    oracle). :meth:`observe_write` — a write observed for a step BELOW the
    current one logs WARN ``pull.step.late_write`` + increments
    :attr:`late_writes` (production observe-only; never raises, never blocks —
    the #907/#919 detect-and-warn pattern).

    HONEST wiring envelope (Golden Rule #10): both production spines feed
    :meth:`observe_write` from PRODUCER-side hooks (the spot per-window drain
    callback; the F&O per-pair ``on_progress`` drain), so it observes the
    synchronous inline write path and any sequencer wiring regression (a pass
    still settling after a later ``begin_step``) — it CANNOT see an
    async-pipeline consumer tail draining a prior step's queued write after
    the boundary (those tails never touch the monitor; consumer-side
    attribution needs a submit-time step stamp through ``FetchPipeline`` — a
    named follow-up, NOT claimed). In the default async-pipeline mode
    :attr:`late_writes` therefore stays 0 on a healthy pull.
    """

    def __init__(self, *, raise_on_violation: bool = False) -> None:
        self._lock = threading.Lock()
        self._raise_on_violation = raise_on_violation
        self._current: PullStep | None = None
        self._late_writes = 0

    @property
    def current_step(self) -> PullStep | None:
        """The highest step begun so far (``None`` before the first step)."""
        with self._lock:
            return self._current

    @property
    def late_writes(self) -> int:
        """Count of writes observed for a step below the current one."""
        with self._lock:
            return self._late_writes

    def begin_step(self, step: PullStep) -> None:
        """Mark *step* as started; a rank regression is a loud ordering violation.

        Re-beginning the CURRENT step is a no-op (an until-clean retry round);
        the monitor's cursor only ever advances (a regression never rewinds it,
        so later writes for the lower step stay observable as late writes).
        """
        regressed_from: PullStep | None = None
        with self._lock:
            current = self._current
            if current is not None and step < current:
                regressed_from = current
            else:
                self._current = step
        if regressed_from is not None:
            if self._raise_on_violation:
                raise StepOrderError(
                    f"pull step order violation: began {step.label} (rank {int(step)}) "
                    f"after {regressed_from.label} (rank {int(regressed_from)})"
                )
            _LOG.error(
                "pull.step.order_violation",
                step=step.label,
                current_step=regressed_from.label,
            )

    def begin_phase(self) -> None:
        """Start a NEW ordered pass — rewind the step cursor to "no step yet".

        DEC-PULL-SIX-STEP-ORDER-001 dual-interval envelope: the retained
        defensive spot path that injects ``intervals=("1day", "1minute")``
        runs the ordered steps 1→2 ONCE PER INTERVAL PHASE, so the minute
        phase's ``begin_step(INDICES_SPOT)`` after the daily phase's
        STOCKS_SPOT is a LEGAL phase restart, never a rank regression (which
        previously logged a false ``pull.step.order_violation`` ERROR and
        raised under the test oracle). Production resolves ``("1minute",)``
        — one phase — so this is a no-op there (the cursor starts ``None``).

        HONEST (Golden Rule #10): the six-step law is enforced WITHIN each
        interval phase; the dual-interval maintenance path stays
        interval-major by construction (daily stock fetches precede minute
        index fetches) — a disclosed envelope, not silently blessed as
        step-major. Rewinding also relaxes the cross-phase cursor, so a
        caller sharing one monitor across surfaces keeps ordering guarantees
        only WITHIN each phase-delimited sequence. :attr:`late_writes` is
        NOT reset — the counter accumulates across phases.
        """
        with self._lock:
            self._current = None

    def observe_write(self, step: PullStep, *, symbol: str | None = None) -> None:
        """Observe one settle/write for *step*; a below-current write is a counted WARN.

        Never raises, never blocks — pure detect-and-warn, even under
        ``raise_on_violation=True`` (only :meth:`begin_step` is the hard oracle).
        Fed from PRODUCER-side hooks in both spines — see the class docstring's
        honest wiring envelope (an async consumer tail is NOT observable here).
        """
        late_behind: PullStep | None = None
        with self._lock:
            current = self._current
            if current is not None and step < current:
                late_behind = current
                self._late_writes += 1
        if late_behind is not None:
            _LOG.warning(
                "pull.step.late_write",
                step=step.label,
                current_step=late_behind.label,
                symbol=symbol,
            )


__all__ = [
    "AUTO_RESUME_MAX_ROUNDS",
    "CONFIRMED_EMPTY_MIN_FETCHES",
    "COVERAGE_MARKET",
    "CREDENTIALS_REFUSAL_HEADLINE",
    "DAILY_INTERVAL",
    "DEFAULT_EMPTY_RETRY_ATTEMPTS",
    "DEFAULT_MAX_ROUNDS",
    "GROWW_COVERAGE_MARKET",
    "OLD_GAP_HORIZON_DAYS",
    "SETTLED_SKIPPED_OPERATOR_THRESHOLD",
    "SYNTHETIC_DRY_RUN_LABEL",
    "DateWindow",
    "LoopVerdict",
    "PullStep",
    "PullVerdict",
    "PullVerdictKind",
    "RetryOutcome",
    "RetryRound",
    "SpotStepGroup",
    "StepOrderError",
    "StepOrderMonitor",
    "SyntheticSpotAdapter",
    "UntilCleanOutcome",
    "UntilCleanRound",
    "WindowDaysFor",
    "clamp_window_end",
    "classify_pull",
    "credentials_refusal_verdict",
    "decide_until_clean",
    "drop_bars_after",
    "fetch_until_nonempty",
    "gate_pull_credentials",
    "label_synthetic",
    "mark_synthetic",
    "maybe_audit",
    "now_ist",
    "partition_spot_steps",
    "plan_windows",
    "retry_failed_windows",
    "run_until_clean",
    "summarize_conflicts",
    "synthetic_spot_bars",
    "tile_windows",
    "verdict_message",
    "window_days_for_interval",
]
