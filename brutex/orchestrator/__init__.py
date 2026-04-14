"""BruteX orchestrator — L6 in the layer stack.

This package owns the 4-phase sequential ingest pipeline, the work-item
state machine, the completeness verifier, the adaptive rate limiter,
and the Ray-backed workers.

Structure (being built incrementally):

* :mod:`brutex.orchestrator.state` — work-item state machine (FR-ING-014)
* :mod:`brutex.orchestrator.retry` — Groww exception → Outcome classifier + backoff schedule (FR-ING-008 refined)
* :mod:`brutex.orchestrator.audit` — hash-chained JSONL audit log (NFR-OBS-003)
* ``brutex.orchestrator.pipeline`` — phase pipeline + gating (FR-ING-013) — TBD
* ``brutex.orchestrator.rate_limiter`` — adaptive token bucket (FR-ING-017) — TBD
* ``brutex.orchestrator.store`` — LMDB-backed WorkItem persistence — TBD

See ``docs/REQUIREMENTS.md §8.5 / §8.6 / §8.7 / §15.5`` for the spec.
"""

from __future__ import annotations

from brutex.orchestrator.pipeline import (
    DEFAULT_MAX_ITERATIONS_PER_ITEM,
    EventSubscriber,
    InvokeCallable,
    IterationLimitError,
    PhaseDriverProtocol,
    PhaseHaltError,
    PhaseResult,
    PhaseRunner,
    PipelineError,
    PipelineEvent,
    PipelineEventKind,
    SleepCallable,
    drive_item_to_terminal,
    run_sequential_pipeline,
)
from brutex.orchestrator.audit import (
    GENESIS_PREV_HASH,
    AuditChainCorruption,
    AuditEntry,
    AuditError,
    AuditWriteError,
    AuditWriter,
    read_entries,
    verify_chain,
)
from brutex.orchestrator.rate_limiter import (
    DEFAULT_BURST,
    DEFAULT_DOWNSHIFT_RPS,
    DEFAULT_DOWNSHIFT_TRIGGER,
    DEFAULT_DOWNSHIFT_WINDOW,
    DEFAULT_INITIAL_RPS,
    DEFAULT_UPSHIFT_RECOVERY,
    AdaptiveTokenBucket,
    RateLimiterError,
    RateLimiterEvent,
)
from brutex.orchestrator.retry import (
    BACKOFF_SCHEDULE_SECONDS,
    GROWW_EXCEPTION_CLASSIFICATION,
    JITTER_FRACTION,
    REQUIRED_EXCEPTION_NAMES,
    UnknownExceptionError,
    backoff_delay,
    classify_exception,
    classify_exception_name,
    outcome_severity,
)
from brutex.orchestrator.store import (
    CONTENT_HASH_BYTES,
    DEFAULT_MAP_SIZE_MB,
    MAX_DBS,
    StoreCorruptionError,
    StoreError,
    StoreKeyError,
    WorkItemStore,
)
from brutex.orchestrator.state import (
    NON_TERMINAL_STATES,
    TERMINAL_STATES,
    IncompletePhaseError,
    IngestPhase,
    NonTerminalStateError,
    Outcome,
    QuarantineReason,
    TransitionError,
    WorkItem,
    WorkItemState,
    dispatch,
    is_terminal,
    phase_complete,
    resolve_quarantine,
    transition,
)

__all__ = [
    # state.py
    "IncompletePhaseError",
    "IngestPhase",
    "NON_TERMINAL_STATES",
    "NonTerminalStateError",
    "Outcome",
    "QuarantineReason",
    "TERMINAL_STATES",
    "TransitionError",
    "WorkItem",
    "WorkItemState",
    "dispatch",
    "is_terminal",
    "phase_complete",
    "resolve_quarantine",
    "transition",
    # retry.py
    "BACKOFF_SCHEDULE_SECONDS",
    "GROWW_EXCEPTION_CLASSIFICATION",
    "JITTER_FRACTION",
    "REQUIRED_EXCEPTION_NAMES",
    "UnknownExceptionError",
    "backoff_delay",
    "classify_exception",
    "classify_exception_name",
    "outcome_severity",
    # audit.py
    "AuditChainCorruption",
    "AuditEntry",
    "AuditError",
    "AuditWriteError",
    "AuditWriter",
    "GENESIS_PREV_HASH",
    "read_entries",
    "verify_chain",
    # rate_limiter.py
    "AdaptiveTokenBucket",
    "DEFAULT_BURST",
    "DEFAULT_DOWNSHIFT_RPS",
    "DEFAULT_DOWNSHIFT_TRIGGER",
    "DEFAULT_DOWNSHIFT_WINDOW",
    "DEFAULT_INITIAL_RPS",
    "DEFAULT_UPSHIFT_RECOVERY",
    "RateLimiterError",
    "RateLimiterEvent",
    # store.py
    "CONTENT_HASH_BYTES",
    "DEFAULT_MAP_SIZE_MB",
    "MAX_DBS",
    "StoreCorruptionError",
    "StoreError",
    "StoreKeyError",
    "WorkItemStore",
    # pipeline.py
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
