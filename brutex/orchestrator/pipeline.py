"""Phase pipeline — the sequential A→B→C→D ingest driver.

Implements ``FR-ING-013`` (sequential phase gating) and ``FR-ING-014``
(work-item retry loop) from ``REQUIREMENTS.md §8.5``.

## Responsibilities

1. **Retry loop per item** — :func:`drive_item_to_terminal` calls the
   adapter in a loop, sleeps on RETRYING with exponential backoff,
   and returns when the item reaches a terminal state.
2. **Phase runner** — :class:`PhaseRunner` drives **many items** through
   the retry loop, aggregates stats, halts on unresolved quarantine,
   and verifies ``phase_complete`` at the end.
3. **Sequential gating** — :func:`run_sequential_pipeline` runs the
   four phases in order, blocking phase P+1 until ``phase_complete(P)``
   holds. Matches the spec diagram in §8.5.
4. **Event emission** — every phase start / progress / end / quarantine
   / halt is emitted as a :class:`PipelineEvent` so the Telegram
   notifier and the dashboard can subscribe without coupling to the
   pipeline internals.

## Design — pure composition, zero adapter lock-in

:class:`PhaseRunner` does NOT know about the GrowwAdapter directly.
It takes an ``invoke`` callable::

    invoke: Callable[[WorkItem], tuple[WorkItem, Any]]

For Phase A this is ``lambda wi: adapter.pull_all_instruments(work_item=wi)``.
For Phase B::

    lambda wi: adapter.pull_expiries(
        work_item=wi,
        exchange=wi.params["exchange"],
        underlying_symbol=wi.params["underlying"],
        year=wi.params["year"],
    )

This decouples the pipeline from the adapter's method signatures and
lets unit tests plug in a fake invoke function that returns pre-
canned results.

## Never-skip guarantee

Per ``FR-ING-015``:

* Every work item is driven until it reaches a terminal state OR
  ``max_iterations_per_item`` (default 50) is exceeded. The latter
  should never happen in practice (retry_count caps at 10 in
  :mod:`brutex.orchestrator.state`) — the extra budget is belt-and-
  braces.
* At phase end, :func:`phase_complete` is called and raises on any
  non-terminal or unresolved-quarantine item. If the raise happens,
  the pipeline halts and Phase P+1 never starts.
* Quarantine → phase halt. The next phase does NOT start until the
  operator resolves the quarantine manually (via CLI) or fixes the
  root cause and re-runs from scratch.
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, Protocol

from brutex.models import BruteXError
from brutex.orchestrator.audit import AuditWriter
from brutex.orchestrator.retry import backoff_delay
from brutex.orchestrator.state import (
    IncompletePhaseError,
    IngestPhase,
    NonTerminalStateError,
    WorkItem,
    WorkItemState,
    is_terminal,
    phase_complete,
)
from brutex.orchestrator.store import WorkItemStore

# ────────────────────────────────────────────────────────────────────────
# Constants & types
# ────────────────────────────────────────────────────────────────────────

#: Maximum times one work item may be (dispatched + transitioned)
#: before the pipeline gives up and escalates. Serves as a runaway
#: guard on top of the state-machine's retry_count cap (10).
DEFAULT_MAX_ITERATIONS_PER_ITEM: Final[int] = 50

#: Type alias for the adapter-invocation callable.
InvokeCallable = Callable[[WorkItem], tuple[WorkItem, Any]]

#: Type alias for a sleep function. Injectable for tests.
SleepCallable = Callable[[float], None]


# ────────────────────────────────────────────────────────────────────────
# Errors
# ────────────────────────────────────────────────────────────────────────


class PipelineError(BruteXError):
    """Base class for pipeline-orchestration errors."""


class PhaseHaltError(PipelineError):
    """Raised when a phase halts due to unresolved quarantines.

    The next phase cannot start. Operator must resolve the quarantined
    items (via ``brutex requeue`` or ``brutex mark-no-data``) and
    restart the pipeline from the halted phase.
    """

    def __init__(
        self,
        *,
        phase: IngestPhase,
        quarantined_count: int,
        message: str | None = None,
    ) -> None:
        self.phase = phase
        self.quarantined_count = quarantined_count
        msg = message or (
            f"Phase {phase.value} halted — {quarantined_count} item(s) "
            f"quarantined and not yet resolved. Phase {phase.value}+1 "
            f"cannot start. Run `brutex audit tail -f` to see the "
            f"quarantine reasons, then either re-queue or mark as "
            f"no-data via the CLI."
        )
        super().__init__(msg)


class IterationLimitError(PipelineError):
    """Raised when :func:`drive_item_to_terminal` exceeds its budget.

    Indicates a bug in the state machine or the adapter — an item
    should always reach terminal within a few iterations because
    retry_count caps at 10. The extra budget is 5x that.
    """


# ────────────────────────────────────────────────────────────────────────
# Pipeline events — pushed to subscribers
# ────────────────────────────────────────────────────────────────────────


class PipelineEventKind:  # noqa: D101 — enum-like namespace
    PHASE_START: Final[str] = "phase.start"
    PHASE_PROGRESS: Final[str] = "phase.progress"
    PHASE_COMPLETE: Final[str] = "phase.complete"
    PHASE_HALTED: Final[str] = "phase.halted"
    ITEM_COMPLETE: Final[str] = "item.complete"
    RUN_START: Final[str] = "run.start"
    RUN_COMPLETE: Final[str] = "run.complete"


@dataclass(frozen=True, slots=True)
class PipelineEvent:
    """One event from the pipeline driver.

    Subscribers (Telegram notifier, dashboard, audit logger) consume
    these to publish their own side-effects. The pipeline itself is
    side-effect-free except for the store / audit / adapter it owns.
    """

    kind: str
    at: datetime
    phase: IngestPhase | None = None
    run_id: str | None = None
    total: int | None = None
    done: int | None = None
    success: int | None = None
    no_data: int | None = None
    quarantined: int | None = None
    retried: int | None = None
    item_id: str | None = None
    item_state: WorkItemState | None = None
    elapsed_seconds: float | None = None
    message: str | None = None


#: Subscriber callback type — receives every event.
EventSubscriber = Callable[[PipelineEvent], None]


# ────────────────────────────────────────────────────────────────────────
# Phase result
# ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PhaseResult:
    """Summary of one phase's execution.

    Returned by :meth:`PhaseRunner.run`. The aggregator /
    ``brutex report`` command uses this to populate the completion
    screen.
    """

    phase: IngestPhase
    total: int
    success: int
    no_data: int
    quarantined: int
    retried: int  # total retry count across all items (not distinct items)
    elapsed_seconds: float

    @property
    def terminal(self) -> int:
        return self.success + self.no_data + self.quarantined

    @property
    def had_quarantines(self) -> bool:
        return self.quarantined > 0


# ────────────────────────────────────────────────────────────────────────
# Per-item retry loop
# ────────────────────────────────────────────────────────────────────────


def drive_item_to_terminal(
    *,
    work_item: WorkItem,
    invoke: InvokeCallable,
    sleep: SleepCallable = time.sleep,
    backoff: Callable[[int], float] = backoff_delay,
    max_iterations: int = DEFAULT_MAX_ITERATIONS_PER_ITEM,
) -> tuple[WorkItem, Any]:
    """Drive *work_item* until it reaches a terminal state.

    Calls ``invoke(item)`` in a loop. On each iteration:

    * If the returned item is terminal → return it plus the last
      response.
    * If it's :attr:`~brutex.orchestrator.state.WorkItemState.RETRYING`,
      sleep for ``backoff(item.retry_count)`` seconds then re-invoke.
    * If it's :attr:`~brutex.orchestrator.state.WorkItemState.PENDING`
      or :attr:`~brutex.orchestrator.state.WorkItemState.IN_FLIGHT`
      → bug in the adapter (should not return these). Raise
      :exc:`PipelineError`.

    Parameters
    ----------
    work_item
        The starting item (typically PENDING or a resumed RETRYING item).
    invoke
        Callable that takes a WorkItem and returns
        ``(updated WorkItem, raw response)``. Typically a lambda
        that calls ``adapter.pull_*``.
    sleep
        Callable to wait between retries. Defaults to :func:`time.sleep`.
        Injectable for tests.
    backoff
        Callable returning the wait time for a given retry_count.
        Defaults to :func:`brutex.orchestrator.retry.backoff_delay`.
    max_iterations
        Safety cap on iterations per item. The state machine already
        caps retries at 10, so a value of 50 gives 5× headroom.

    Returns
    -------
    tuple[WorkItem, Any]
        Final terminal WorkItem + the last response object (may be
        None if the last path was an error / retry).

    Raises
    ------
    IterationLimitError
        If *max_iterations* is exceeded without reaching terminal.
    PipelineError
        If the adapter returns an unexpected non-terminal, non-retrying
        state (PENDING or IN_FLIGHT).
    """
    item = work_item
    last_response: Any = None

    for _iteration in range(max_iterations):
        item, last_response = invoke(item)

        if is_terminal(item.state):
            return item, last_response

        if item.state == WorkItemState.RETRYING:
            delay = backoff(item.retry_count)
            sleep(delay)
            continue

        # PENDING or IN_FLIGHT after invoke is a contract violation.
        raise PipelineError(
            f"Adapter returned unexpected state {item.state.value} for "
            f"{item.item_id} — should be terminal or RETRYING. "
            f"This is a bug in the adapter's _execute_call pipeline."
        )

    raise IterationLimitError(
        f"Item {item.item_id} did not reach terminal state after "
        f"{max_iterations} iterations (final state: {item.state.value}, "
        f"retry_count: {item.retry_count}). State machine cap is 10 "
        f"retries — this indicates a deeper issue."
    )


# ────────────────────────────────────────────────────────────────────────
# PhaseRunner — drives many items
# ────────────────────────────────────────────────────────────────────────


class PhaseRunner:
    """Drives every work item through the retry loop for one phase.

    Aggregates stats, emits :class:`PipelineEvent` objects to all
    subscribers, verifies :func:`phase_complete` at the end, and
    raises :exc:`PhaseHaltError` on unresolved quarantines.

    Parameters
    ----------
    phase
        Which :class:`IngestPhase` this runner is for.
    store
        Shared :class:`~brutex.orchestrator.store.WorkItemStore`.
        The adapter writes items here via its own internal logic; the
        runner reads final states for the completeness check.
    audit
        Shared :class:`~brutex.orchestrator.audit.AuditWriter`. The
        runner writes phase-level events here via
        :meth:`_audit_phase_event`.
    invoke
        Adapter-invocation callable. See module docstring for the
        contract.
    subscribers
        Optional list of :class:`EventSubscriber` callbacks. Each
        receives every :class:`PipelineEvent`.
    sleep
        Injectable sleep callable. Defaults to :func:`time.sleep`.
    backoff
        Injectable backoff function. Defaults to
        :func:`brutex.orchestrator.retry.backoff_delay`.
    max_iterations_per_item
        Safety cap per item. Default 50.
    progress_every_n_items
        Emit a ``phase.progress`` event every N items. Default 100.
    """

    def __init__(
        self,
        *,
        phase: IngestPhase,
        store: WorkItemStore,
        audit: AuditWriter,
        invoke: InvokeCallable,
        subscribers: Sequence[EventSubscriber] = (),
        sleep: SleepCallable = time.sleep,
        backoff: Callable[[int], float] = backoff_delay,
        max_iterations_per_item: int = DEFAULT_MAX_ITERATIONS_PER_ITEM,
        progress_every_n_items: int = 100,
        run_id: str | None = None,
    ) -> None:
        self._phase = phase
        self._store = store
        self._audit = audit
        self._invoke = invoke
        self._subscribers = list(subscribers)
        self._sleep = sleep
        self._backoff = backoff
        self._max_iterations = max_iterations_per_item
        self._progress_every = max(1, progress_every_n_items)
        self._run_id = run_id

    # ── Event plumbing ─────────────────────────────────────────────

    def subscribe(self, subscriber: EventSubscriber) -> None:
        """Register an additional event subscriber."""
        self._subscribers.append(subscriber)

    def _publish(self, event: PipelineEvent) -> None:
        for sub in self._subscribers:
            try:
                sub(event)
            except Exception:  # noqa: BLE001
                # Subscriber errors must NEVER block the pipeline.
                # We eat them here but the audit log still records
                # the event itself via _audit_phase_event.
                pass

    def _audit_phase_event(
        self,
        *,
        action: str,
        payload: dict[str, Any],
    ) -> None:
        self._audit.append(
            action=action,
            subject=f"phase:{self._phase.value}",
            payload={"run_id": self._run_id, **payload},
        )

    # ── Main entry point ───────────────────────────────────────────

    def run(self, work_items: Iterable[WorkItem]) -> PhaseResult:
        """Drive every item in *work_items* to terminal.

        Returns a :class:`PhaseResult` summary. Raises
        :exc:`PhaseHaltError` if any item ends up quarantined AND the
        operator has not marked it resolved.

        Parameters
        ----------
        work_items
            Iterable of :class:`WorkItem`. Items may be freshly
            created (``PENDING``) or resumed from a store
            (``RETRYING`` / ``PENDING`` — any non-terminal state).
            Terminal items in the input are passed through
            untouched and counted in the stats.
        """
        items_list = list(work_items)
        total = len(items_list)
        start_ts = time.monotonic()

        self._emit_phase_start(total=total)

        counts: Counter[WorkItemState] = Counter()
        total_retries = 0
        final_items: list[WorkItem] = []

        for idx, item in enumerate(items_list, start=1):
            if is_terminal(item.state):
                counts[item.state] += 1
                final_items.append(item)
                continue

            final, _ = drive_item_to_terminal(
                work_item=item,
                invoke=self._invoke,
                sleep=self._sleep,
                backoff=self._backoff,
                max_iterations=self._max_iterations,
            )
            counts[final.state] += 1
            total_retries += final.retry_count
            final_items.append(final)

            self._emit_item_complete(final)

            if idx % self._progress_every == 0 or idx == total:
                self._emit_progress(done=idx, total=total, counts=counts)

        elapsed = time.monotonic() - start_ts

        result = PhaseResult(
            phase=self._phase,
            total=total,
            success=counts.get(WorkItemState.SUCCESS, 0),
            no_data=counts.get(WorkItemState.NO_DATA, 0),
            quarantined=counts.get(WorkItemState.QUARANTINED, 0),
            retried=total_retries,
            elapsed_seconds=elapsed,
        )

        # Verify phase_complete invariant (§8.7)
        try:
            phase_complete(final_items)
        except (NonTerminalStateError, IncompletePhaseError) as exc:
            # Emit halt event and raise.
            self._emit_phase_halted(result=result, reason=str(exc))
            raise PhaseHaltError(
                phase=self._phase,
                quarantined_count=result.quarantined,
                message=str(exc),
            ) from exc

        self._emit_phase_complete(result=result)
        return result

    # ── Event emitters ─────────────────────────────────────────────

    def _emit_phase_start(self, *, total: int) -> None:
        evt = PipelineEvent(
            kind=PipelineEventKind.PHASE_START,
            at=datetime.now(UTC),
            phase=self._phase,
            run_id=self._run_id,
            total=total,
        )
        self._publish(evt)
        self._audit_phase_event(
            action="PHASE_START",
            payload={"phase": self._phase.value, "total": total},
        )

    def _emit_progress(
        self, *, done: int, total: int, counts: Counter[WorkItemState]
    ) -> None:
        evt = PipelineEvent(
            kind=PipelineEventKind.PHASE_PROGRESS,
            at=datetime.now(UTC),
            phase=self._phase,
            run_id=self._run_id,
            total=total,
            done=done,
            success=counts.get(WorkItemState.SUCCESS, 0),
            no_data=counts.get(WorkItemState.NO_DATA, 0),
            quarantined=counts.get(WorkItemState.QUARANTINED, 0),
        )
        self._publish(evt)

    def _emit_item_complete(self, item: WorkItem) -> None:
        evt = PipelineEvent(
            kind=PipelineEventKind.ITEM_COMPLETE,
            at=datetime.now(UTC),
            phase=self._phase,
            run_id=self._run_id,
            item_id=item.item_id,
            item_state=item.state,
            retried=item.retry_count,
        )
        self._publish(evt)

    def _emit_phase_complete(self, *, result: PhaseResult) -> None:
        evt = PipelineEvent(
            kind=PipelineEventKind.PHASE_COMPLETE,
            at=datetime.now(UTC),
            phase=self._phase,
            run_id=self._run_id,
            total=result.total,
            done=result.total,
            success=result.success,
            no_data=result.no_data,
            quarantined=result.quarantined,
            retried=result.retried,
            elapsed_seconds=result.elapsed_seconds,
        )
        self._publish(evt)
        self._audit_phase_event(
            action="PHASE_COMPLETE",
            payload={
                "phase": self._phase.value,
                "total": result.total,
                "success": result.success,
                "no_data": result.no_data,
                "quarantined": result.quarantined,
                "retried": result.retried,
                "elapsed_seconds": result.elapsed_seconds,
            },
        )

    def _emit_phase_halted(self, *, result: PhaseResult, reason: str) -> None:
        evt = PipelineEvent(
            kind=PipelineEventKind.PHASE_HALTED,
            at=datetime.now(UTC),
            phase=self._phase,
            run_id=self._run_id,
            total=result.total,
            done=result.total,
            success=result.success,
            no_data=result.no_data,
            quarantined=result.quarantined,
            elapsed_seconds=result.elapsed_seconds,
            message=reason,
        )
        self._publish(evt)
        self._audit_phase_event(
            action="PHASE_HALTED",
            payload={
                "phase": self._phase.value,
                "quarantined": result.quarantined,
                "reason": reason,
            },
        )


# ────────────────────────────────────────────────────────────────────────
# Sequential pipeline — A → B → C → D with hard gates
# ────────────────────────────────────────────────────────────────────────


class PhaseDriverProtocol(Protocol):
    """Minimal protocol for a "phase driver" — one per phase.

    Each phase has its own driver because each uses different
    adapter methods and enumerates its work items from different
    sources (CSV for A, get_expiries for B, etc.).
    """

    phase: IngestPhase

    def enumerate_items(self) -> Iterable[WorkItem]:
        """Return the list of work items for this phase."""
        ...

    def run(self) -> PhaseResult:
        """Drive every item to terminal and return a summary.

        Should construct a :class:`PhaseRunner` internally and call
        its :meth:`~PhaseRunner.run` with the enumerated items.
        """
        ...


def run_sequential_pipeline(
    *,
    drivers: Sequence[PhaseDriverProtocol],
    audit: AuditWriter,
    subscribers: Sequence[EventSubscriber] = (),
    run_id: str,
) -> list[PhaseResult]:
    """Drive phases in strict sequence, halting on the first phase
    that does not reach a clean ``phase_complete``.

    Implements the §8.5 sequential gating rule: Phase P+1 does NOT
    start until ``phase_complete(P)`` holds.

    Parameters
    ----------
    drivers
        Sequence of :class:`PhaseDriverProtocol` implementations, in
        the order they should run. Typically:
        ``[PhaseADriver, PhaseBDriver, PhaseCDriver, PhaseDDriver]``.
    audit
        Shared audit writer used for run-level events (run.start /
        run.complete).
    subscribers
        Event subscribers for run-level events. Individual phases
        manage their own via the drivers.
    run_id
        Stable run identifier, stamped on every audit entry.

    Returns
    -------
    list[PhaseResult]
        One :class:`PhaseResult` per completed phase. Length may be
        less than ``len(drivers)`` if the pipeline halted early —
        the caller can inspect which phase blocked.

    Raises
    ------
    PhaseHaltError
        Re-raised from the phase that halted, after writing run-level
        halt metadata.
    """
    results: list[PhaseResult] = []
    start_ts = time.monotonic()

    _emit_run_event(
        audit=audit,
        subscribers=subscribers,
        kind=PipelineEventKind.RUN_START,
        run_id=run_id,
    )

    try:
        for driver in drivers:
            result = driver.run()
            results.append(result)
    except PhaseHaltError as exc:
        elapsed = time.monotonic() - start_ts
        _emit_run_event(
            audit=audit,
            subscribers=subscribers,
            kind=PipelineEventKind.PHASE_HALTED,
            run_id=run_id,
            phase=exc.phase,
            message=str(exc),
            elapsed_seconds=elapsed,
        )
        raise

    elapsed = time.monotonic() - start_ts
    _emit_run_event(
        audit=audit,
        subscribers=subscribers,
        kind=PipelineEventKind.RUN_COMPLETE,
        run_id=run_id,
        elapsed_seconds=elapsed,
    )
    return results


def _emit_run_event(
    *,
    audit: AuditWriter,
    subscribers: Sequence[EventSubscriber],
    kind: str,
    run_id: str,
    phase: IngestPhase | None = None,
    elapsed_seconds: float | None = None,
    message: str | None = None,
) -> None:
    """Helper for run-level event emission."""
    evt = PipelineEvent(
        kind=kind,
        at=datetime.now(UTC),
        run_id=run_id,
        phase=phase,
        elapsed_seconds=elapsed_seconds,
        message=message,
    )
    for sub in subscribers:
        try:
            sub(evt)
        except Exception:  # noqa: BLE001
            pass
    audit.append(
        action=kind.upper().replace(".", "_"),
        subject=f"run:{run_id}",
        payload={
            "phase": phase.value if phase else None,
            "elapsed_seconds": elapsed_seconds,
            "message": message,
        },
    )


__all__ = [
    "DEFAULT_MAX_ITERATIONS_PER_ITEM",
    "EventSubscriber",
    "InvokeCallable",
    "IterationLimitError",
    "PhaseDriverProtocol",
    "PhaseHaltError",
    "PhaseResult",
    "PhaseRunner",
    "PipelineError",
    "PipelineEvent",
    "PipelineEventKind",
    "SleepCallable",
    "drive_item_to_terminal",
    "run_sequential_pipeline",
]
