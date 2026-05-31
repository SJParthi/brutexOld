"""Resilient historical-candle fetch loop — the always-fetch, never-lose core.

Drives a thin :class:`CandleFetcher` (the real one wraps the growwapi SDK;
tests inject a fake) and turns "pull the data" into an unattended,
crash-safe, quota-safe loop:

* **Resume** — skips chunks the :class:`~brutex.data.watermark.FetchWatermark`
  already settled (COMPLETED / NO_DATA); re-fetches exactly the rest.
* **Transient errors** (rate-limit / timeout / generic) → backoff + retry,
  up to ``max_retries``, then quarantine (no infinite hammer).
* **Empty / not-found** → recorded ``NO_DATA`` and settled (the G-10
  anti-loop: never re-requested).
* **Auth expiry** (the daily/6 AM token rollover) → call ``reauth`` once,
  then retry — the self-healing token refresh; if ``reauth`` is absent or
  already used, quarantine.
* **Terminal errors** (bad-request / authz / fatal) → quarantine (FAILED)
  for operator attention.

Pure orchestration: no SDK import, no real sleep (both injected), so the
whole resilience behaviour is unit-testable with a fake fetcher that
simulates rate-limits, timeouts, empties, auth-rollover and crashes.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Final, Protocol

import structlog

from brutex.data.watermark import ChunkKey, ChunkStatus, FetchWatermark
from brutex.orchestrator.retry import (
    UnknownExceptionError,
    backoff_delay,
    classify_exception,
)
from brutex.orchestrator.state import Outcome

_LOG = structlog.get_logger("brutex.fetch")

#: Outcomes that are transient → backoff + retry.
_TRANSIENT: Final[frozenset[Outcome]] = frozenset(
    {Outcome.RATE_LIMIT, Outcome.TIMEOUT, Outcome.GENERIC_API_ERROR}
)
#: Outcomes meaning "legitimately empty" → record NO_DATA + settle (G-10).
_EMPTY: Final[frozenset[Outcome]] = frozenset({Outcome.NOT_FOUND, Outcome.NO_DATA})
#: Outcome meaning "token expired" → re-auth once, then retry.
_REAUTH: Final[Outcome] = Outcome.AUTH_FAILED


class CandleFetcher(Protocol):
    """Thin client the loop drives. The real impl wraps growwapi."""

    def fetch_chunk(self, key: ChunkKey) -> Sequence[object]:
        """Return bars for *key*'s window (possibly empty). Raise on error."""
        ...


@dataclass(frozen=True, slots=True)
class FetchOutcome:
    """Summary of one :func:`run_fetch` pass."""

    completed: int
    no_data: int
    failed: int
    skipped: int  # already-settled chunks skipped on resume


def _log_chunk_failure(
    key: ChunkKey,
    outcome: Outcome,
    exc: BaseException,
    *,
    retries_exhausted: int | None,
) -> None:
    """Emit one ``ingest.chunk.failed`` warning carrying the REAL vendor error.

    Before this, a quarantined chunk only bumped a counter — so a 100%-failed
    pull (e.g. a wrong segment for an index) looked like a silent "no data".
    Logging the ``outcome`` + exception type + message turns every failure
    into a diagnosable, real-time signal (Golden Rule #8 audit trail +
    observability) without changing the quarantine behaviour.
    """
    _LOG.warning(
        "ingest.chunk.failed",
        symbol=key.groww_symbol,
        interval=key.interval,
        start=key.start,
        end=key.end,
        outcome=outcome.name,
        error_type=type(exc).__name__,
        error=str(exc),
        retries_exhausted=retries_exhausted,
    )


def run_fetch(
    fetcher: CandleFetcher,
    keys: Iterable[ChunkKey],
    watermark: FetchWatermark,
    *,
    max_retries: int = 5,
    reauth: Callable[[], None] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    delay_fn: Callable[[int], float] = backoff_delay,
) -> FetchOutcome:
    """Fetch every chunk in *keys* resiliently, recording each in *watermark*.

    Idempotent + resumable: re-running with the same *watermark* skips
    already-settled chunks. Never raises on a known Groww error — it
    records the chunk's terminal status and moves on (a 100%-quarantined
    run still returns a summary rather than crashing).

    Parameters
    ----------
    fetcher
        The :class:`CandleFetcher` to pull each window.
    keys
        All chunk windows to fetch (e.g. from a chunk planner).
    watermark
        Mutated in place: each chunk's outcome is recorded.
    max_retries
        Max backoff-retries for a transient error before quarantine.
    reauth
        Optional callback invoked once on an auth-expiry, before retrying
        (the TOTP token-refresh hook). If ``None`` or already used, an
        auth error quarantines.
    sleep_fn, delay_fn
        Injected for tests (so retries don't actually sleep). ``delay_fn``
        maps a retry count to a backoff delay in seconds.
    """
    completed = 0
    no_data = 0
    failed = 0
    skipped = 0

    for key in keys:
        if watermark.is_settled(key):
            skipped += 1
            continue

        retry_count = 0
        reauth_used = False
        while True:
            try:
                bars = fetcher.fetch_chunk(key)
            except Exception as exc:
                try:
                    outcome = classify_exception(exc)
                except UnknownExceptionError:
                    # A non-vendor exception reached the loop — a programmer
                    # bug or an unexpected runtime error (corrupt watermark
                    # row, chunk-planning slip, …). NEVER let one chunk crash
                    # the whole pull: quarantine it with the REAL type +
                    # message logged at error level so it's diagnosable, then
                    # move on (the same blast-radius containment that turned
                    # the retry-count ValueError from a pull-killer into a
                    # logged failure). DEC-PULL-CHUNK-CONTAINMENT-001.
                    _LOG.error(
                        "ingest.chunk.unexpected_error",
                        symbol=key.groww_symbol,
                        interval=key.interval,
                        start=key.start,
                        end=key.end,
                        error_type=type(exc).__name__,
                        error=str(exc),
                    )
                    watermark.record(key, ChunkStatus.FAILED)
                    failed += 1
                    break
                if outcome in _TRANSIENT:
                    if retry_count < max_retries:
                        # Increment BEFORE computing the delay: ``delay_fn``
                        # (``backoff_delay``) is 1-based — it requires
                        # ``retry_count >= 1`` and raises ``ValueError`` on 0.
                        # Passing the pre-increment 0 on the first retry crashed
                        # the whole pull instead of backing off (regression
                        # guarded in tests/unit/test_groww_fetch_loop.py).
                        retry_count += 1
                        sleep_fn(delay_fn(retry_count))
                        continue
                    # Retries exhausted → quarantine. Surface the REAL vendor
                    # reason (never swallow it — DEC-PULL-ERROR-VISIBILITY-001)
                    # so the operator sees WHY a chunk failed, not just a count.
                    _log_chunk_failure(key, outcome, exc, retries_exhausted=max_retries)
                    watermark.record(key, ChunkStatus.FAILED)
                    failed += 1
                    break
                if outcome in _EMPTY:
                    watermark.record(key, ChunkStatus.NO_DATA)
                    no_data += 1
                    break
                if outcome == _REAUTH and reauth is not None and not reauth_used:
                    reauth()
                    reauth_used = True
                    continue
                # Terminal: bad-request / authz / fatal / un-refreshable auth.
                # Log the real reason before quarantining (visibility, as above).
                _log_chunk_failure(key, outcome, exc, retries_exhausted=None)
                watermark.record(key, ChunkStatus.FAILED)
                failed += 1
                break
            else:
                if bars:
                    watermark.record(key, ChunkStatus.COMPLETED)
                    completed += 1
                else:
                    watermark.record(key, ChunkStatus.NO_DATA)
                    no_data += 1
                break

    return FetchOutcome(completed=completed, no_data=no_data, failed=failed, skipped=skipped)


__all__ = [
    "CandleFetcher",
    "FetchOutcome",
    "run_fetch",
]
