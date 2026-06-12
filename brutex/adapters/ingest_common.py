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

PR-J4a additionally relocated here (a behaviour-preserving MOVE out of the Dhan
package so a later slice can delete it — CLAUDE.md §11 COMMON):

* **Synthetic spot source** — :func:`synthetic_spot_bars` (pure offline bar
  generator) + :class:`SyntheticSpotAdapter` (a :class:`MarketAdapter` over it),
  the dry-run data source (no token / no network).
* **Pull verdict** — :class:`PullVerdict` / :data:`PullVerdictKind` /
  :func:`classify_pull` (the actionable, prioritised finished-pull verdict).
* **Re-pull-until-clean** — :func:`run_until_clean` + :class:`UntilCleanRound` /
  :class:`UntilCleanOutcome` / :func:`decide_until_clean` and the round budgets.
* **Coverage-key namespace + per-interval window width** — :data:`_MARKET` (the
  lake coverage-key namespace, value UNCHANGED from the Dhan home) +
  :func:`window_days_for_interval` (the Dhan per-call tiling width policy), both
  consumed by :mod:`brutex.data.window_ledger_store`.

Everything here is **behaviour-preserving** — moved verbatim from the Dhan
modules, with the only change being the two injection seams (*is_known_gap*,
*window_days_for* / *market*) that let the one implementation serve both vendors.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from time import sleep as _time_sleep
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol
from zoneinfo import ZoneInfo

import structlog

from brutex.adapters.protocol import (
    DerivativeContract,
    InstrumentPage,
    MarketCapabilities,
    single_instrument_page,
)
from brutex.data.models import CandleBar
from brutex.models import Exchange, Instrument

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
# Coverage-key namespace + per-interval tiling width (relocated from
# ``dhan.ingest`` in PR-J4a so ``window_ledger_store`` no longer imports the Dhan
# package). Behaviour-preserving — the namespace VALUE and the width policy are
# byte-identical to their Dhan home (changing _MARKET's value is a data-migration
# event — the lake coverage keys are stamped with it).
# --------------------------------------------------------------------------- #
#: The data-source namespace stamped on every coverage :class:`WindowKey`. Its
#: VALUE (``"dhan"``) is the lake coverage-key namespace and is INTENTIONALLY
#: UNCHANGED across the relocation — the persisted ledger keys carry it, so
#: changing it would orphan every existing coverage row (a data-migration event,
#: out of scope for this behaviour-preserving move). PUBLIC (no leading underscore)
#: so cross-module consumers (the Dhan spine, the window ledger store) import it
#: WITHOUT a private-access warning (the value was a private ``_MARKET`` constant in
#: its prior Dhan home).
COVERAGE_MARKET: str = "dhan"

#: Dhan intraday per-call cap (days). Relocated from
#: ``dhan.constants.INTRADAY_WINDOW_DAYS`` (same value 90) so the width policy has
#: NO dhan import here; ``dhan.constants`` remains the verified source of truth.
_INTRADAY_WINDOW_DAYS: int = 90


def window_days_for_interval(interval: str) -> int | None:
    """Resolve an interval to its tiling width — ``None`` (single window) for daily.

    The Dhan per-call cap is a flat 90 days for intraday; the daily endpoint takes
    the whole range in ONE call (``None`` ⇒ :func:`tile_windows` emits a single
    un-tiled window). Injected into the shared tiler so the tiling MECHANISM is
    vendor-neutral while this width policy stays owned here (relocated verbatim
    from ``dhan.ingest._dhan_window_days_for`` in PR-J4a).
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

    Both :class:`~brutex.adapters.dhan.run_ingest.DhanIngestReport` and
    :class:`~brutex.adapters.groww.run_ingest.GrowwIngestReport` satisfy it
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


# --------------------------------------------------------------------------- #
# Synthetic spot source (relocated from ``dhan.synthetic`` +
# ``dhan.synthetic_adapter`` in PR-J4a — the dry-run, offline data source).
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
    market_id="dhan",
    display_name="Dhan (synthetic dry-run)",
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
# Pull verdict (relocated from ``dhan.pull_verdict`` in PR-J4a — the actionable,
# prioritised finished-pull verdict).
# --------------------------------------------------------------------------- #
#: The mutually-exclusive, priority-ordered outcomes of a finished pull.
PullVerdictKind = Literal[
    "auth_expired",
    "quota_exhausted",
    "transient_residual",
    "known_gap_only",
    "clean",
]

#: The operator-facing headline + next action for each kind (DEC-PULL-VERDICT-001).
#: Kept as data (not f-strings) so the message is deterministic + test-pinnable;
#: dynamic counts are carried as structured fields on :class:`PullVerdict`.
_HEADLINE: Final[dict[str, str]] = {
    "auth_expired": "🔑 Dhan token expired mid-pull — the pull aborted.",
    "quota_exhausted": "📊 Daily Dhan quota reached — auto-resume paused for today.",
    "transient_residual": "⏳ Some windows are still pending after retries.",
    "known_gap_only": "ℹ️ No new bars — these symbols have no intraday on Dhan.",
    "clean": "✅ Pull complete.",
}
_ACTION: Final[dict[str, str]] = {
    "auth_expired": (
        "Regenerate the 24-hour token at web.dhan.co, then re-pull. "
        "A retry can NEVER succeed on a dead token — only a fresh token can."
    ),
    "quota_exhausted": (
        "The 100,000/day Dhan quota is spent — the auto-resume paused itself "
        "(it will NOT keep burning a dead quota). Re-pull after the IST-midnight "
        "reset; already-pulled windows are skipped, so only the gaps re-fetch."
    ),
    "transient_residual": (
        "Re-pull to finish — already-pulled windows are skipped and only the "
        "still-pending windows are re-fetched (auto-retry mops up transient 429s)."
    ),
    "known_gap_only": (
        "No action needed — these are reference-only symbols Dhan does not serve "
        "intraday (e.g. broad-market indices); they are not brute-force inputs."
    ),
    "clean": "No action needed.",
}


@dataclass(frozen=True, slots=True)
class PullVerdict:
    """A finished pull's single, prioritised, actionable verdict.

    Immutable + JSON-serialisable. ``kind`` is the worst (highest-priority)
    condition observed; ``pending_windows`` is the count of work that still has
    to succeed (the still-failed windows + the quarantined pairs) so the
    operator knows the pull is NOT done. ``auth_cause`` carries the originating
    vendor error string ONLY for ``auth_expired`` (else ``None``).
    """

    kind: PullVerdictKind
    headline: str
    action: str
    pending_windows: int
    failed_pairs: int
    no_data: int
    completed: int
    auth_cause: str | None = None

    @property
    def needs_operator(self) -> bool:
        """True iff a human must act before more data can land (the loud case).

        ``quota_exhausted`` counts: the day's quota is spent, so the operator must
        wait for the IST-midnight reset (or re-pull then) — the auto-resume cannot
        finish today on its own.
        """
        return self.kind in ("auth_expired", "quota_exhausted", "transient_residual")

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
) -> PullVerdict:
    """Classify a finished pull into ONE prioritised, actionable :class:`PullVerdict`.

    Priority (worst first), so the operator always sees the most urgent action:

    1. **auth_expired** — the token died mid-pull (``auth_aborted``). Most urgent:
       no retry can recover until the token is regenerated. ``pending_windows``
       counts the still-failed windows + quarantined pairs left behind.
    2. **quota_exhausted** — the day's 100,000-call Dhan quota is spent
       (``quota_exhausted``) with windows still pending: the auto-resume paused
       itself rather than burn a dead quota; the operator re-pulls after the
       IST-midnight reset (DEC-AUTO-RESUME-QUOTA-001). Ranked just under a dead
       token because, like it, NO amount of immediate retrying can land a bar
       today — only the reset can.
    3. **transient_residual** — windows are still failing (``still_failed_windows``)
       or pairs were quarantined (``failed_pairs``) but the token is alive: a
       re-pull / the auto-retry layer will finish them.
    4. **known_gap_only** — nothing completed and nothing failed, but the vendor
       returned empty for some symbols (``no_data``): the vendor genuinely has no
       intraday for them (reference-only). Expected, not an error.
    5. **clean** — everything that could complete did.

    Pure + deterministic (no I/O / clock). ``auth_cause`` is preserved ONLY on
    the ``auth_expired`` verdict.
    """
    if auth_aborted:
        return _make_verdict(
            "auth_expired", still_failed_windows, failed_pairs, no_data, completed, auth_cause
        )
    if quota_exhausted:
        return _make_verdict(
            "quota_exhausted", still_failed_windows, failed_pairs, no_data, completed, None
        )
    if still_failed_windows > 0 or failed_pairs > 0:
        return _make_verdict(
            "transient_residual", still_failed_windows, failed_pairs, no_data, completed, None
        )
    if completed == 0 and no_data > 0:
        return _make_verdict(
            "known_gap_only", still_failed_windows, failed_pairs, no_data, completed, None
        )
    return _make_verdict("clean", still_failed_windows, failed_pairs, no_data, completed, None)


def _make_verdict(
    kind: PullVerdictKind,
    still_failed_windows: int,
    failed_pairs: int,
    no_data: int,
    completed: int,
    auth_cause: str | None,
) -> PullVerdict:
    """Build a :class:`PullVerdict` for *kind* with the canonical headline + action."""
    return PullVerdict(
        kind=kind,
        headline=_HEADLINE[kind],
        action=_ACTION[kind],
        pending_windows=still_failed_windows + failed_pairs,
        failed_pairs=failed_pairs,
        no_data=no_data,
        completed=completed,
        auth_cause=auth_cause,
    )


# --------------------------------------------------------------------------- #
# Re-pull-until-clean (relocated from ``dhan.until_clean`` in PR-J4a — drive
# bounded pull rounds until the coverage ledger is CLEAN).
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
#: once the burst is over. Each round itself runs the bounded inner
#: :func:`retry_failed_windows` loop, so the worst case is strictly bounded.
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

        DEC-AUTO-RESUME-QUOTA-001 — the day's 100,000-call Dhan quota is spent;
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
) -> LoopVerdict:
    """Decide what the until-clean loop does after a round — the pure 6-way controller.

    Priority (matching :func:`retry_failed_windows` + :func:`classify_pull`, worst
    first):

    1. **stop_dead** — ``auth_aborted``: the token died; retrying is futile
       (``RenewToken`` cannot revive a dead token) — halt for re-auth.
    2. **stop_clean** — ``still_failed_windows == 0``: the coverage ledger is clean
       (success outranks every "can't continue" reason — if we're done, we're done).
    3. **stop_quota** — ``quota_exhausted``: the day's 100,000-call Dhan quota is
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

    Pure + O(1) (a constant number of comparisons). No I/O, no clock. The two new
    keyword args default to the pre-DEC-AUTO-RESUME-QUOTA-001 behaviour
    (``quota_exhausted=False`` + ``made_progress=True``), so every existing caller is
    byte-unchanged.
    """
    if auth_aborted:
        return "stop_dead"
    if still_failed_windows == 0:
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
        QUOTA-001): ``True`` means the day's 100,000-call Dhan quota is spent, so the
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
        # max_rounds=0 opt-out, never infinite).
        return UntilCleanOutcome(verdict="stop_budget", rounds_run=0, rounds=())
    round_index = 0
    verdict: LoopVerdict = "continue"
    #: The previous round's residual, so a round that shrank it by ZERO trips the
    #: ``stop_no_progress`` guard (what makes the large auto-resume budget safe).
    #: ``None`` before round 1 ⇒ round 1 always counts as progress (no baseline).
    prev_failed: int | None = None
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
        )
        prev_failed = outcome.still_failed_windows
        if verdict != "continue":
            break
    return UntilCleanOutcome(verdict=verdict, rounds_run=round_index, rounds=tuple(rounds))


__all__ = [
    "AUTO_RESUME_MAX_ROUNDS",
    "COVERAGE_MARKET",
    "DEFAULT_MAX_ROUNDS",
    "DateWindow",
    "LoopVerdict",
    "PullVerdict",
    "PullVerdictKind",
    "RetryOutcome",
    "RetryRound",
    "SyntheticSpotAdapter",
    "UntilCleanOutcome",
    "UntilCleanRound",
    "WindowDaysFor",
    "clamp_window_end",
    "classify_pull",
    "decide_until_clean",
    "now_ist",
    "plan_windows",
    "retry_failed_windows",
    "run_until_clean",
    "summarize_conflicts",
    "synthetic_spot_bars",
    "tile_windows",
    "window_days_for_interval",
]
