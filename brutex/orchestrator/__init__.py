"""BruteX orchestrator — L6 in the layer stack.

This package owns the 4-phase sequential ingest pipeline, the work-item
state machine, the completeness verifier, the adaptive rate limiter,
and the Ray-backed workers.

Structure (being built incrementally, this module is NOT yet complete):

* :mod:`brutex.orchestrator.state` — work-item state machine (FR-ING-014)
* ``brutex.orchestrator.pipeline`` — phase pipeline + gating (FR-ING-013) — TBD
* ``brutex.orchestrator.retry`` — retry/quarantine classifier (FR-ING-008 refined) — TBD
* ``brutex.orchestrator.rate_limiter`` — adaptive token bucket (FR-ING-017) — TBD
* ``brutex.orchestrator.completeness`` — phase-complete verifier (§8.7) — TBD

See ``docs/REQUIREMENTS.md §8.5 / §8.6 / §8.7`` for the spec.
"""

from __future__ import annotations

from brutex.orchestrator.state import (
    IngestPhase,
    IncompletePhaseError,
    NonTerminalStateError,
    TransitionError,
    WorkItem,
    WorkItemState,
    TERMINAL_STATES,
    NON_TERMINAL_STATES,
    is_terminal,
    phase_complete,
    transition,
)

__all__ = [
    "IncompletePhaseError",
    "IngestPhase",
    "NON_TERMINAL_STATES",
    "NonTerminalStateError",
    "TERMINAL_STATES",
    "TransitionError",
    "WorkItem",
    "WorkItemState",
    "is_terminal",
    "phase_complete",
    "transition",
]
