"""BruteX orchestrator — L6 in the layer stack.

This package owns the hash-chained audit log, the adaptive rate limiter,
the Bloom dedup prefilter, the pull watchdog/supervisor/auto-resume
machinery, and the live pipeline event shapes the dashboard + Telegram
subscribe to.

Structure (the LIVE modules):

* :mod:`brutex.orchestrator.audit` — hash-chained JSONL audit log (Golden Rule #8)
* :mod:`brutex.orchestrator.rate_limiter` — adaptive token bucket
* :mod:`brutex.orchestrator.events` — live pipeline event shapes (dashboard + Telegram contract)
* :mod:`brutex.orchestrator.quota_tracker` — quota exception types + the ``QuotaGate`` seam
* :mod:`brutex.orchestrator.bloom` / ``parallel`` / ``universe_phases`` /
  ``watchdog`` / ``supervisor`` / ``auto_resume`` / ``replay`` /
  ``phase_throughput`` — imported directly by their consumers (not re-exported here)

DEC-ORCH-DEAD-SCAFFOLD-DELETE-001 (MIGRATION.md #1727 deletion record,
ADR-0019 phase 2a): the never-production-wired ingest FSM scaffolding —
``state.py`` (6-state work-item FSM), ``pipeline.py`` (PhaseRunner driver),
``retry.py`` (vendor-exception classifier + backoff schedule), ``store.py``
(LMDB WorkItemStore) and ``adaptive_rate_limiter.py`` (the superseded
env-var limiter) — was DELETED with its live shapes preserved in
:mod:`brutex.orchestrator.events` and the trimmed
:mod:`brutex.orchestrator.quota_tracker`. The live Groww retry ladders are
``adapters/groww/errors.is_retryable`` + the client ``max_attempts``; the
live rate control is ``adapters/rate_common.RateController`` over
:class:`~brutex.orchestrator.rate_limiter.AdaptiveTokenBucket`.
"""

from __future__ import annotations

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
from brutex.orchestrator.events import (
    EventSubscriber,
    IngestPhase,
    PipelineEvent,
    PipelineEventKind,
    WorkItemState,
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

__all__ = [
    "DEFAULT_BURST",
    "DEFAULT_DOWNSHIFT_RPS",
    "DEFAULT_DOWNSHIFT_TRIGGER",
    "DEFAULT_DOWNSHIFT_WINDOW",
    "DEFAULT_INITIAL_RPS",
    "DEFAULT_UPSHIFT_RECOVERY",
    "GENESIS_PREV_HASH",
    # rate_limiter.py
    "AdaptiveTokenBucket",
    # audit.py
    "AuditChainCorruption",
    "AuditEntry",
    "AuditError",
    "AuditWriteError",
    "AuditWriter",
    # events.py
    "EventSubscriber",
    "IngestPhase",
    "PipelineEvent",
    "PipelineEventKind",
    "RateLimiterError",
    "RateLimiterEvent",
    "WorkItemState",
    "read_entries",
    "verify_chain",
]
