"""Post-run auto-retry of leftover failed ingest windows (DEC-INGEST-AUTO-RETRY-001).

The recovery layer that closes the **84-windows-still-failed** gap a live
783-symbol spot pull hit: a sustained Dhan 429 storm (~4 minutes, 100+
consecutive 429s) outlived the *in-run* per-window retry budget
(:data:`~brutex.adapters.dhan.ingest._DEFAULT_MAX_RETRY_PASSES`, only 2 extra
passes), so a pull finished with windows still failing — and the ONLY recovery
was the operator manually re-running the whole pull. This module re-runs ADDITIONAL
**rounds** of the SAME ingest, each re-fetching ONLY the leftover failed windows
(the coverage ledger skips every already-completed window — incremental +
idempotent, DEC-FETCH-COVERAGE-001), with exponential backoff between rounds,
until coverage is clean OR a bounded round budget is exhausted.

**One function, two callers (CLAUDE.md §11 COMMON).** :func:`retry_failed_windows`
is the single underlying engine; the AUTOMATIC path (called by default after a
pull, opt-out via ``--no-auto-retry`` / config) and the MANUAL path (``brutex
fetch --retry-failed`` + the dashboard "Retry failed windows" button) BOTH invoke
it — no duplicated round-loop logic.

**Honest envelope (Golden Rule #10):**

* The work is **O(failed_windows)**, NOT O(1)-total — each round re-fetches every
  still-failed window (the coverage skip-set makes a round skip the *completed*
  windows in O(1) per window, but the residual set itself is whatever the storm
  left). The round COUNT is bounded (``max_rounds``), never infinite.
* **Rate-safe** by construction: a round IS one
  :func:`~brutex.adapters.dhan.run_ingest.run_dhan_spot_ingest` pass, which is
  governed by the SAME shared adaptive token bucket (``pace``) — so every round
  stays ``≤ 5 r/s`` (CLAUDE.md §2 Rule 6) and the inter-round backoff waits a
  throttle out instead of fighting it.
* **Known-gaps are NOT retried forever.** A window that fails because Dhan
  genuinely has no data (a ``DH-905`` "no data present" / known-gap quarantine,
  classified by :func:`~brutex.adapters.dhan.errors.is_no_data_error`) is a data
  boundary, not a transient fault — it is counted as a *known gap*, never as a
  retryable residual, so it cannot drive another round. Only **transient**
  residual windows (``still_failed_windows`` — 429 / timeout / 5xx that outlived
  the in-run budget) keep the loop going.
* **Auth death stops the loop.** If a round reports the token died mid-pull
  (``auth_aborted`` — a 401/403 or ``DH-906``), retrying is futile (RenewToken
  cannot revive a dead token), so the loop halts immediately and surfaces the
  residual for the operator to re-auth.
* The backoff sleep is **injected** so tests drive it with a no-op recorder (no
  real sleeps), exactly like the in-run retry's
  :func:`~brutex.adapters.dhan.ingest._default_retry_backoff`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from time import sleep as _time_sleep
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from brutex.adapters.dhan.run_ingest import DhanIngestReport

#: Structured logger — lights each retry round (``ingest.auto_retry.round``) and
#: the final residual (``ingest.auto_retry.done``) under the pull's correlation
#: scope, so an operator can grep the recovery end-to-end (DEC-INGEST-AUTO-RETRY-001).
_LOG = structlog.get_logger("brutex.adapters.dhan.auto_retry")

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
    (no real sleep) — the same inject-the-sleep pattern as the in-run
    :func:`~brutex.adapters.dhan.ingest._default_retry_backoff`.
    """
    _time_sleep(min(_DEFAULT_ROUND_BACKOFF_S * (2.0 ** (round_index - 1)), _MAX_ROUND_BACKOFF_S))


@dataclass(frozen=True, slots=True)
class RetryRound:
    """One auto-retry round's residual accounting (DEC-INGEST-AUTO-RETRY-001).

    A round is one :func:`~brutex.adapters.dhan.run_ingest.run_dhan_spot_ingest`
    pass over the leftover windows; these counts come straight off its
    :class:`~brutex.adapters.dhan.run_ingest.DhanIngestReport`.
    """

    #: 1-based round ordinal (round 1 is the FIRST re-pull after the original pull).
    round_index: int
    #: Transient windows still failing after this round (the next round's worklist,
    #: 0 when clean) — the retryable residual that drives the loop.
    still_failed_windows: int
    #: Known-gap quarantines this round (``DH-905`` no-data) — NOT retried (left as-is).
    known_gaps: int
    #: Whether the token died mid-round (a 401/403 or ``DH-906``) — halts the loop.
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


def _count_known_gaps(report: DhanIngestReport) -> int:
    """Count this pass's KNOWN-GAP quarantines (``DH-905`` no-data) — NOT retried.

    A known gap is an EXPECTED data boundary (Dhan genuinely has no data for the
    range), not a transient fault, so it must never drive another retry round
    (the honest-envelope rule, DEC-INGEST-AUTO-RETRY-001). The classification
    delegates to the single source of truth
    :func:`~brutex.adapters.dhan.errors.is_no_data_message` (the string twin of
    ``is_no_data_error``) so "what counts as no-data" lives in ONE place
    (CLAUDE.md §11 COMMON). Imported locally to avoid an import cycle (``errors``
    is light, but the rule keeps this module's top imports type-only).
    """
    from brutex.adapters.dhan.errors import is_no_data_message

    return sum(1 for failure in report.failures if is_no_data_message(failure.error))


def retry_failed_windows(
    run_pass: Callable[[], DhanIngestReport],
    *,
    max_rounds: int = _DEFAULT_MAX_ROUNDS,
    backoff: Callable[[int], None] | None = None,
) -> RetryOutcome:
    """Re-pull ONLY the leftover failed windows in bounded rounds until clean (or budget out).

    The single underlying engine BOTH the automatic (post-pull, default-on) and
    the manual (``brutex fetch --retry-failed`` / dashboard button) paths call
    (CLAUDE.md §11 COMMON). Each round invokes *run_pass* — one full
    :func:`~brutex.adapters.dhan.run_ingest.run_dhan_spot_ingest` pass wired to
    the SAME ``coverage`` ledger, so every already-completed window is skipped
    before the vendor call and only the still-failed windows are re-fetched
    (incremental + idempotent, DEC-FETCH-COVERAGE-001). The loop runs another
    round while a round leaves TRANSIENT residual windows
    (``still_failed_windows`` — 429 / timeout / 5xx) AND the budget is not spent
    AND the token has not died; it stops early on a clean round or an auth death.
    Known-gap quarantines (``DH-905`` no-data) are counted but NEVER retried.

    **Honest envelope (Golden Rule #10):** O(failed_windows), NOT O(1)-total — a
    round re-fetches every residual window; the round COUNT is bounded by
    *max_rounds*, never infinite. Rate-safety is inherited from *run_pass* (the
    shared ``pace`` bucket keeps every round ``≤ 5 r/s``); the inter-round
    *backoff* waits a throttle out rather than fighting it.

    Parameters
    ----------
    run_pass
        A zero-arg callable that runs ONE ingest pass over the leftover windows
        and returns its :class:`~brutex.adapters.dhan.run_ingest.DhanIngestReport`.
        The caller binds the adapter / sink / instruments / intervals / the
        SHARED ``coverage`` ledger + ``pace`` bucket into it (so each round is
        rate-safe and re-fetches only failed windows). Called once per round
        (never if the original pull was already clean — see *run_pass* contract
        at the first call site).
    max_rounds
        Bounded retry-round budget (default :data:`_DEFAULT_MAX_ROUNDS`). ``0``
        runs the loop zero times — used to DISABLE the automatic retry while
        keeping the same code path (``--no-auto-retry`` maps here). The loop runs
        at most this many rounds regardless of residual (never infinite).
    backoff
        Injected ``(round_index) -> None`` sleep hook called BEFORE each round
        (``None`` → :func:`_default_round_backoff`, a bounded exponential
        seconds-scale wait so a sustained throttle clears between rounds). Tests
        pass a no-op recorder so no real sleep occurs.

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
        known_gaps = _count_known_gaps(report)
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
            # The token died mid-round — retrying is futile (RenewToken cannot
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
    "RetryOutcome",
    "RetryRound",
    "retry_failed_windows",
]
