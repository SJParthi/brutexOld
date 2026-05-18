"""Dhan worker — first production caller of orchestrator infrastructure.

Wires four orchestrator pieces together for the first time in production:

1. :class:`brutex.adapters.dhan.client.DhanClient` for HTTP fetches.
2. :class:`brutex.orchestrator.rate_limiter.AdaptiveTokenBucket` feedback
   loop (:meth:`record_success` / :meth:`record_rate_limit_hit`). Closes
   the §2 P-08 honest-envelope gap from DEC-P0-040.
3. :class:`brutex.orchestrator.audit.AuditWriter` chunk-level entries
   (``INGEST_CHUNK_DONE`` / ``INGEST_CHUNK_RETRY`` / ``QUARANTINE_NEW``).
   Closes the §2 P-07 gap (no Dhan-adapter audit caller existed).
4. :func:`brutex.orchestrator.retry.backoff_delay` with the canonical
   10-slot schedule and ±20% jitter.

The worker is a sync transducer over a sequence of fetch requests.
For each request it dispatches the returned :class:`Outcome` to one of
six actions and emits bars via a caller-supplied sink callback.

Design decisions (locked by 4-agent hostile pre-impl review on
2026-05-18, results synthesised in DEC-P0-045 / MIGRATION.md #182):

* **Rule #11 COMMON** — reuse :data:`BACKOFF_SCHEDULE_SECONDS` (10-slot
  ladder 2s → 900s) + :data:`MAX_SOFT_RETRIES` (= 10) directly from
  :mod:`brutex.orchestrator.retry` and :mod:`.state`. Do NOT introduce
  a worker-local ladder. The 10-slot schedule is sized against the
  60s downshift window so the first 3 retries land in ≤14s wall-time.
* **Sink callback, NOT generator** — generator-with-raise loses
  partial-progress on halt; sink callback + ``IngestStats`` return
  preserves accounting (matches :class:`PhaseRunner` pattern).
* **Audit-first ordering** — :meth:`AuditWriter.append` is called
  BEFORE emitting bars to the sink. If audit fails, the worker raises
  before any bars are visible to the caller.
* **``bucket.record_success()`` only on OK/NO_DATA outcomes** — calling
  it on ``BAD_REQUEST`` / ``NOT_FOUND`` would inappropriately advance
  the upshift recovery clock (those are failed exchanges, not healthy
  responses).
* **``bucket.record_rate_limit_hit()`` only on ``RATE_LIMIT``** — NOT
  on ``TIMEOUT`` or ``GENERIC_API_ERROR`` (those are server-side, not
  throttling-class).
* **``FATAL`` = quarantine + halt** — write ``QUARANTINE_NEW`` audit
  entry first, then raise :class:`DhanWorkerHaltError` carrying the
  partial :class:`IngestStats` (Hostile-agent §14). Matches FSM
  ``FATAL`` → ``QUARANTINED`` (reason ``FATAL_ERROR``) policy in
  :mod:`brutex.orchestrator.state`.
* **``DailyQuotaExceeded`` → halt** — caught from inside
  :meth:`DhanClient.fetch_intraday` (propagates from
  :meth:`AdaptiveTokenBucket.acquire`); no auto-resume per DEC-P0-029.
* **Caller-owned ``AuditWriter``** — the worker calls ``append`` but
  never closes it. :class:`AuditWriter` is documented not thread-safe
  so the caller serializes writers across worker invocations.
* **Single-thread, NOT re-entrant** — concurrent ``run()`` calls on
  the same instance would race on the shared accumulator. Concurrency
  comes from spawning multiple :class:`DhanWorker` instances with
  independent buckets.
* **Mode-agnostic** — Track 1 modes A/B/C apply at strategy-evaluation
  time, not at ingest. The worker fetches OHLC chunks; modes are
  irrelevant here.

References
----------
* docs/MIGRATION.md entry #182 (DEC-P0-045)
* docs/TRACK1_REQUIREMENTS_FINAL.md §2 P-07, P-08 (closes both gaps)
* docs/TRACK1_DATA_INGEST_DESIGN.md §10.1 (chunk-level worker)
* CLAUDE.md §2 rule 6 (Dhan rate limit) + rule 11 (COMMON)
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from structlog.stdlib import BoundLogger

from brutex.adapters.dhan.body import HistoricalDailyRequest, IntradayChunkRequest
from brutex.adapters.dhan.client import DhanClient
from brutex.adapters.dhan.parser import OHLCBar
from brutex.adapters.dhan.types import FetchResult
from brutex.models import BruteXError
from brutex.orchestrator.audit import AuditWriter
from brutex.orchestrator.quota_tracker import DailyQuotaExceeded
from brutex.orchestrator.rate_limiter import AdaptiveTokenBucket
from brutex.orchestrator.retry import backoff_delay
from brutex.orchestrator.state import MAX_SOFT_RETRIES, Outcome

# ────────────────────────────────────────────────────────────────────────
# Audit action constants — single source of truth for chunk-level entries
# ────────────────────────────────────────────────────────────────────────

#: Audit-log action string for a successfully-fetched chunk (OK_WITH_DATA,
#: OK_EMPTY, NO_DATA). One entry per fetch; payload carries outcome + bar
#: count + attempt number.
AUDIT_ACTION_CHUNK_DONE: Final[str] = "INGEST_CHUNK_DONE"

#: Audit-log action string for a transient-error retry attempt
#: (RATE_LIMIT, TIMEOUT, GENERIC_API_ERROR). One entry per attempt;
#: payload carries the outcome, attempt counter, max_retries cap, and
#: any Dhan error code.
AUDIT_ACTION_CHUNK_RETRY: Final[str] = "INGEST_CHUNK_RETRY"

#: Audit-log action string for a quarantined chunk (BAD_REQUEST,
#: NOT_FOUND, retry_exhausted, FATAL halt). Payload carries the
#: outcome / reason / attempt counter / Dhan error code.
AUDIT_ACTION_QUARANTINE_NEW: Final[str] = "QUARANTINE_NEW"

#: Halt-reason string for :class:`DhanWorkerHaltError` when the daily
#: Dhan API quota of 100K calls has been exhausted (per DEC-P0-029
#: ``DailyQuotaExceeded``). No auto-resume — operator action required.
HALT_REASON_DAILY_QUOTA: Final[str] = "daily_quota_exhausted"

#: Reason string written into the QUARANTINE_NEW payload when the worker
#: hits its retry cap with a transient outcome still pending.
QUARANTINE_REASON_RETRY_EXHAUSTED: Final[str] = "retry_exhausted"


# ────────────────────────────────────────────────────────────────────────
# Outcome routing — exhaustive over the 11 Outcome enum values
# ────────────────────────────────────────────────────────────────────────

#: Success-class outcomes — record_success() called, INGEST_CHUNK_DONE
#: audit entry written. Bars emitted to sink ONLY for OK_WITH_DATA.
_SUCCESS_OUTCOMES: Final[frozenset[Outcome]] = frozenset(
    {Outcome.OK_WITH_DATA, Outcome.OK_EMPTY, Outcome.NO_DATA}
)

#: Retry-class outcomes — sleep(backoff) + INGEST_CHUNK_RETRY audit
#: entry, then re-attempt. RATE_LIMIT additionally calls
#: record_rate_limit_hit() on the bucket.
_RETRY_OUTCOMES: Final[frozenset[Outcome]] = frozenset(
    {Outcome.RATE_LIMIT, Outcome.TIMEOUT, Outcome.GENERIC_API_ERROR}
)

#: Quarantine-class outcomes — QUARANTINE_NEW audit entry, move on to
#: the next request. No halt. record_success() NOT called.
_QUARANTINE_OUTCOMES: Final[frozenset[Outcome]] = frozenset(
    {Outcome.NOT_FOUND, Outcome.BAD_REQUEST}
)

#: Halt-class outcomes — QUARANTINE_NEW audit entry then raise
#: :class:`DhanWorkerHaltError`. Caller responsible for downstream
#: notification + cleanup.
_HALT_OUTCOMES: Final[frozenset[Outcome]] = frozenset(
    {Outcome.AUTH_FAILED, Outcome.AUTHZ_FAILED, Outcome.FATAL}
)


# ────────────────────────────────────────────────────────────────────────
# Result dataclasses
# ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class IngestStats:
    """Immutable per-run summary returned from :meth:`DhanWorker.run`.

    Attributes
    ----------
    ok_with_data
        Number of chunks that returned bars (OK_WITH_DATA).
    ok_empty
        Number of chunks that returned 200 + empty arrays (OK_EMPTY).
    no_data
        Number of chunks that returned DH-907 ("No data for query").
    quarantined
        Number of chunks quarantined (BAD_REQUEST + NOT_FOUND +
        retry_exhausted + halt-class FATAL/AUTH/AUTHZ).
    retries_total
        Sum of retry attempts across all chunks.
    bars_emitted
        Total :class:`OHLCBar` objects emitted to the sink.
    halt_reason
        ``None`` on a clean run; populated when the worker raised
        :class:`DhanWorkerHaltError`. Always carries the categorical
        outcome name (e.g. ``"AUTH_FAILED"``, ``"daily_quota_exhausted"``).
    """

    ok_with_data: int = 0
    ok_empty: int = 0
    no_data: int = 0
    quarantined: int = 0
    retries_total: int = 0
    bars_emitted: int = 0
    halt_reason: str | None = None

    def chunks_total(self) -> int:
        """Total chunks observed (success + quarantine)."""
        return self.ok_with_data + self.ok_empty + self.no_data + self.quarantined


@dataclass(slots=True)
class _Accumulator:
    """Internal mutable counter; frozen into :class:`IngestStats` on exit."""

    ok_with_data: int = 0
    ok_empty: int = 0
    no_data: int = 0
    quarantined: int = 0
    retries_total: int = 0
    bars_emitted: int = 0
    halt_reason: str | None = None

    def freeze(self) -> IngestStats:
        """Return an immutable :class:`IngestStats` snapshot."""
        return IngestStats(
            ok_with_data=self.ok_with_data,
            ok_empty=self.ok_empty,
            no_data=self.no_data,
            quarantined=self.quarantined,
            retries_total=self.retries_total,
            bars_emitted=self.bars_emitted,
            halt_reason=self.halt_reason,
        )


# ────────────────────────────────────────────────────────────────────────
# Halt exception — carries partial stats for caller-side audit emission
# ────────────────────────────────────────────────────────────────────────


class DhanWorkerHaltError(BruteXError):
    """Raised when the worker encounters an unrecoverable condition.

    Carries ``partial_stats`` so the caller can write a ``RUN_HALTED``
    audit entry with accurate counters before re-raising or aborting.
    The ``reason`` attribute is the categorical halt cause (e.g.
    ``"AUTH_FAILED"``, ``"daily_quota_exhausted"``) — safe to log.

    Notes
    -----
    Mirrors the partial-progress payload pattern from
    :class:`brutex.orchestrator.pipeline.PhaseHaltError` so the audit
    chain stays coherent across halt boundaries.
    """

    def __init__(self, *, reason: str, partial_stats: IngestStats) -> None:
        super().__init__(
            f"DhanWorker halted: {reason}",
            safe_message=f"DhanWorker halted ({reason})",
        )
        self.reason = reason
        self.partial_stats = partial_stats


#: Sink callback signature. Called once per ``OK_WITH_DATA`` chunk with
#: the parsed bars and the originating :class:`FetchResult` (so the
#: caller can reconstruct the request and audit context).
BarSink = Callable[[Sequence[OHLCBar], FetchResult], None]


# ────────────────────────────────────────────────────────────────────────
# Audit-subject helper
# ────────────────────────────────────────────────────────────────────────


def _subject_for(req: IntradayChunkRequest | HistoricalDailyRequest) -> str:
    """Canonical audit-log subject string for one fetch request.

    Format: ``chunk:<security_id>:<interval>:<from_date>:<to_date>``
    for intraday or ``chunk:<security_id>:daily:<from_date>:<to_date>``
    for historical-daily. Two runs with identical params produce
    identical subject strings (Golden Rule #7 replay).
    """
    sid = req.instrument.security_id
    if isinstance(req, IntradayChunkRequest):
        return f"chunk:{sid}:{req.interval}:{req.from_date.isoformat()}:{req.to_date.isoformat()}"
    return f"chunk:{sid}:daily:{req.from_date.isoformat()}:{req.to_date.isoformat()}"


# ────────────────────────────────────────────────────────────────────────
# DhanWorker
# ────────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class DhanWorker:
    """Sync per-chunk fetch worker. Single-thread, non-re-entrant.

    See module docstring for the full design rationale (DEC-P0-045 /
    4-agent hostile pre-impl review on 2026-05-18).

    Parameters
    ----------
    client
        Configured :class:`DhanClient`. The worker invokes
        :meth:`fetch_intraday` or :meth:`fetch_historical_daily` per
        request.
    bucket
        :class:`AdaptiveTokenBucket` — the SAME instance the client
        was constructed with. The worker calls :meth:`record_success`
        and :meth:`record_rate_limit_hit` based on outcome.
    audit
        Caller-owned :class:`AuditWriter`. The worker appends but does
        NOT close it. The writer is documented not thread-safe; the
        caller serialises writers across concurrent worker invocations.
    logger
        :class:`structlog.stdlib.BoundLogger`. The caller should have
        invoked :func:`brutex.observability.logging.configure_logging`
        before constructing the worker.
    sleep
        Sleep callable for retry backoffs. Defaults to :func:`time.sleep`.
        Inject (with a clock or counter) for tests.
    rng
        :class:`random.Random` for backoff jitter. ``None`` = production
        non-seeded (matches :func:`backoff_delay` semantics).
    max_retries
        Max retry attempts per chunk before quarantining with reason
        ``retry_exhausted``. Defaults to :data:`MAX_SOFT_RETRIES` (= 10)
        from :mod:`brutex.orchestrator.state`.
    """

    client: DhanClient
    bucket: AdaptiveTokenBucket
    audit: AuditWriter
    logger: BoundLogger
    sleep: Callable[[float], None] = field(default=time.sleep)
    rng: random.Random | None = None
    max_retries: int = MAX_SOFT_RETRIES

    def __repr__(self) -> str:
        # Redact: DhanClient already redacts its token in its own repr.
        # Bucket / audit / logger printed by type only — no token, no
        # path, no internal state.
        return f"DhanWorker(client={self.client!r}, max_retries={self.max_retries})"

    def run(
        self,
        requests: Sequence[IntradayChunkRequest | HistoricalDailyRequest],
        sink: BarSink,
    ) -> IngestStats:
        """Process every request once; emit bars via sink; return stats.

        Parameters
        ----------
        requests
            Sequence of fetch requests. The caller is responsible for
            chunking (Dhan's 90-day intraday window) and dedup (the
            :class:`brutex.orchestrator.store.WorkItemStore` content-hash
            cache). The worker re-fetches on every invocation.
        sink
            Bar-emit callback invoked once per ``OK_WITH_DATA`` chunk.
            See :data:`BarSink`.

        Returns
        -------
        IngestStats
            Per-outcome counters + bars_emitted total. ``halt_reason``
            is ``None`` on clean runs.

        Raises
        ------
        DhanWorkerHaltError
            On ``AUTH_FAILED`` / ``AUTHZ_FAILED`` / ``FATAL`` outcomes
            or :class:`DailyQuotaExceeded`. ``partial_stats`` carries the
            in-flight accounting up to the halt boundary.
        AuditWriteError
            Propagated if :meth:`AuditWriter.append` fails. The worker
            does NOT catch audit failures — a broken audit chain
            invalidates Golden Rule #8 and must halt the run.
        """
        acc = _Accumulator()
        try:
            for req in requests:
                self._process(req, sink, acc)
        finally:
            # Always log the completion summary — even on halt — so the
            # operator sees the final accounting in the structured log
            # stream regardless of whether the caller catches the halt.
            self.logger.info(
                "dhan_worker_complete",
                chunks_total=(acc.ok_with_data + acc.ok_empty + acc.no_data + acc.quarantined),
                ok_with_data=acc.ok_with_data,
                ok_empty=acc.ok_empty,
                no_data=acc.no_data,
                quarantined=acc.quarantined,
                retries_total=acc.retries_total,
                bars_emitted=acc.bars_emitted,
                halt_reason=acc.halt_reason,
            )
        return acc.freeze()

    # ── Single-request loop ─────────────────────────────────────────

    def _process(
        self,
        req: IntradayChunkRequest | HistoricalDailyRequest,
        sink: BarSink,
        acc: _Accumulator,
    ) -> None:
        """Run one request with up to ``max_retries`` attempts."""
        subject = _subject_for(req)
        last_result: FetchResult | None = None

        for attempt in range(1, self.max_retries + 1):
            # 1. Acquire token + fetch. DailyQuotaExceeded propagates
            #    from the bucket via _fetch_one (NOT caught by the
            #    client — see client.py:178-180 docstring).
            try:
                result = self._fetch(req)
            except DailyQuotaExceeded as exc:
                self._on_halt(HALT_REASON_DAILY_QUOTA, subject, acc, exc)
                return  # not reached; _on_halt raises

            last_result = result
            outcome = result.outcome

            # 2. Success-class — write audit, record_success, maybe emit bars.
            if outcome == Outcome.OK_WITH_DATA:
                self.bucket.record_success()
                self._on_ok_with_data(result, subject, attempt, sink, acc)
                return
            if outcome == Outcome.OK_EMPTY:
                self.bucket.record_success()
                self._on_ok_empty(subject, attempt, acc)
                return
            if outcome == Outcome.NO_DATA:
                self.bucket.record_success()
                self._on_no_data(result, subject, attempt, acc)
                return

            # 3. Quarantine-class — write audit, move on (no halt,
            #    no record_success).
            if outcome in _QUARANTINE_OUTCOMES:
                self._on_quarantine(result, subject, attempt, acc)
                return

            # 4. Halt-class — write QUARANTINE_NEW audit then raise.
            if outcome in _HALT_OUTCOMES:
                self._on_halt(outcome.value, subject, acc, result.error)
                return  # not reached; _on_halt raises

            # 5. Retry-class — fall through to backoff sleep.
            if outcome not in _RETRY_OUTCOMES:
                # Defensive: every Outcome value falls into exactly one
                # group above. If a new Outcome is added without updating
                # the routing tables, fail loudly rather than silently
                # treating it as a retry-class outcome.
                raise AssertionError(
                    f"DhanWorker: unmapped outcome {outcome!r} — "
                    f"update _SUCCESS / _RETRY / _QUARANTINE / _HALT "
                    f"frozensets in dhan_worker.py"
                )
            if outcome == Outcome.RATE_LIMIT:
                # Only 429-class outcomes feed the bucket's downshift
                # counter. TIMEOUT + GENERIC_API_ERROR are server-side
                # and do NOT count as throttling hits.
                self.bucket.record_rate_limit_hit()
            self._on_retry(result, subject, attempt, acc)

            # Skip backoff sleep on the final attempt — there's no
            # next iteration.
            if attempt < self.max_retries:
                delay = backoff_delay(attempt, rng=self.rng)
                self.sleep(delay)

        # All retries exhausted → quarantine retry_exhausted.
        # last_result is non-None because the loop ran at least once
        # (max_retries ≥ 1; constructor parameter validation handles
        # the edge case if a caller passes 0). Use explicit raise rather
        # than `assert` so the check survives `python -O`.
        if last_result is None:  # pragma: no cover — unreachable
            raise AssertionError("DhanWorker._process: retry loop exited with no result")
        self._on_retry_exhausted(last_result, subject, acc)

    def _fetch(
        self,
        req: IntradayChunkRequest | HistoricalDailyRequest,
    ) -> FetchResult:
        """Dispatch the request to the matching DhanClient method."""
        if isinstance(req, IntradayChunkRequest):
            return self.client.fetch_intraday(req)
        return self.client.fetch_historical_daily(req)

    # ── Outcome handlers (audit-first, then log, then sink, then acc) ──

    def _on_ok_with_data(
        self,
        result: FetchResult,
        subject: str,
        attempt: int,
        sink: BarSink,
        acc: _Accumulator,
    ) -> None:
        """OK_WITH_DATA: audit + log + sink + acc."""
        self.audit.append(
            action=AUDIT_ACTION_CHUNK_DONE,
            subject=subject,
            payload={
                "outcome": Outcome.OK_WITH_DATA.value,
                "bars": len(result.bars),
                "attempts": attempt,
            },
        )
        self.logger.info(
            "dhan_chunk_ok",
            subject=subject,
            bars=len(result.bars),
            attempts=attempt,
        )
        sink(result.bars, result)
        acc.ok_with_data += 1
        acc.bars_emitted += len(result.bars)

    def _on_ok_empty(
        self,
        subject: str,
        attempt: int,
        acc: _Accumulator,
    ) -> None:
        """OK_EMPTY: 200 + empty arrays (clean weekend/holiday)."""
        self.audit.append(
            action=AUDIT_ACTION_CHUNK_DONE,
            subject=subject,
            payload={
                "outcome": Outcome.OK_EMPTY.value,
                "bars": 0,
                "attempts": attempt,
            },
        )
        self.logger.info("dhan_chunk_empty", subject=subject, attempts=attempt)
        acc.ok_empty += 1

    def _on_no_data(
        self,
        result: FetchResult,
        subject: str,
        attempt: int,
        acc: _Accumulator,
    ) -> None:
        """NO_DATA: DH-907 ("No data for the query window") — distinct
        from OK_EMPTY (which is 200 + empty array). Both are non-error
        from the rate-limiter's perspective.
        """
        error_code = _extract_error_code(result.error)
        self.audit.append(
            action=AUDIT_ACTION_CHUNK_DONE,
            subject=subject,
            payload={
                "outcome": Outcome.NO_DATA.value,
                "bars": 0,
                "attempts": attempt,
                "error_code": error_code,
            },
        )
        self.logger.info(
            "dhan_chunk_no_data",
            subject=subject,
            attempts=attempt,
            error_code=error_code,
        )
        acc.no_data += 1

    def _on_retry(
        self,
        result: FetchResult,
        subject: str,
        attempt: int,
        acc: _Accumulator,
    ) -> None:
        """Transient outcome (RATE_LIMIT / TIMEOUT / GENERIC_API_ERROR):
        audit the retry attempt, log, increment retries_total.
        """
        error_code = _extract_error_code(result.error)
        self.audit.append(
            action=AUDIT_ACTION_CHUNK_RETRY,
            subject=subject,
            payload={
                "outcome": result.outcome.value,
                "attempt": attempt,
                "max_retries": self.max_retries,
                "error_code": error_code,
            },
        )
        self.logger.warning(
            "dhan_chunk_retry",
            subject=subject,
            outcome=result.outcome.value,
            attempt=attempt,
            error_code=error_code,
        )
        acc.retries_total += 1

    def _on_quarantine(
        self,
        result: FetchResult,
        subject: str,
        attempt: int,
        acc: _Accumulator,
    ) -> None:
        """BAD_REQUEST / NOT_FOUND: write QUARANTINE_NEW; move on."""
        error_code = _extract_error_code(result.error)
        reason = result.outcome.value.lower()
        self.audit.append(
            action=AUDIT_ACTION_QUARANTINE_NEW,
            subject=subject,
            payload={
                "outcome": result.outcome.value,
                "reason": reason,
                "attempts": attempt,
                "error_code": error_code,
            },
        )
        self.logger.warning(
            "dhan_chunk_quarantine",
            subject=subject,
            outcome=result.outcome.value,
            reason=reason,
            attempts=attempt,
            error_code=error_code,
        )
        acc.quarantined += 1

    def _on_retry_exhausted(
        self,
        last_result: FetchResult,
        subject: str,
        acc: _Accumulator,
    ) -> None:
        """All ``max_retries`` attempts exhausted on a transient outcome.

        Quarantine with reason ``retry_exhausted`` — caller's job to
        re-queue from the quarantine table once the upstream issue is
        resolved.
        """
        error_code = _extract_error_code(last_result.error)
        self.audit.append(
            action=AUDIT_ACTION_QUARANTINE_NEW,
            subject=subject,
            payload={
                "outcome": last_result.outcome.value,
                "reason": QUARANTINE_REASON_RETRY_EXHAUSTED,
                "attempts": self.max_retries,
                "error_code": error_code,
            },
        )
        self.logger.warning(
            "dhan_chunk_quarantine",
            subject=subject,
            outcome=last_result.outcome.value,
            reason=QUARANTINE_REASON_RETRY_EXHAUSTED,
            attempts=self.max_retries,
            error_code=error_code,
        )
        acc.quarantined += 1

    def _on_halt(
        self,
        reason: str,
        subject: str,
        acc: _Accumulator,
        cause: BaseException | None,
    ) -> None:
        """Halt-class outcomes: write QUARANTINE_NEW audit, raise halt.

        ``cause`` is the originating exception (``DailyQuotaExceeded``)
        or the typed ``DhanError`` from the ``FetchResult.error`` field;
        it's chained into the raised :class:`DhanWorkerHaltError` so the
        traceback preserves the root cause.
        """
        error_code = _extract_error_code(cause) if cause is not None else None
        self.audit.append(
            action=AUDIT_ACTION_QUARANTINE_NEW,
            subject=subject,
            payload={
                "outcome": reason,
                "reason": reason,
                "error_code": error_code,
            },
        )
        acc.quarantined += 1
        acc.halt_reason = reason
        self.logger.error(
            "dhan_worker_halt",
            subject=subject,
            reason=reason,
            error_code=error_code,
        )
        raise DhanWorkerHaltError(
            reason=reason,
            partial_stats=acc.freeze(),
        ) from cause


# ────────────────────────────────────────────────────────────────────────
# Internal helpers
# ────────────────────────────────────────────────────────────────────────


def _extract_error_code(err: BaseException | None) -> str | None:
    """Return ``err.dhan_error_code`` if it's a non-empty string; else None.

    Used to enrich audit + structlog payloads with the 22-code Dhan
    vocabulary identifier when one is available. Non-Dhan exceptions
    (e.g., :class:`DailyQuotaExceeded`) return ``None``.
    """
    if err is None:
        return None
    code: Any = getattr(err, "dhan_error_code", None)
    if isinstance(code, str) and code:
        return code
    return None


__all__ = [
    "AUDIT_ACTION_CHUNK_DONE",
    "AUDIT_ACTION_CHUNK_RETRY",
    "AUDIT_ACTION_QUARANTINE_NEW",
    "HALT_REASON_DAILY_QUOTA",
    "QUARANTINE_REASON_RETRY_EXHAUSTED",
    "BarSink",
    "DhanWorker",
    "DhanWorkerHaltError",
    "IngestStats",
]
