"""Live pipeline event shapes — the SURVIVING orchestrator-scaffolding contract.

DEC-ORCH-DEAD-SCAFFOLD-DELETE-001 (MIGRATION.md #1727 deletion record,
ADR-0019 phase 2a): the never-production-wired ingest FSM scaffolding
(``state.py`` / ``pipeline.py`` / ``retry.py`` / ``store.py``) was DELETED —
LIVE shapes move here, dead machinery dies (the DEC-EXECUTION-TRIO-DELETE-001
#1550 precedent: keep the live containers, delete the drivers).

The shapes below are **verbatim moves**, kept because they feed REAL
production surfaces:

* :class:`IngestPhase` + :class:`PipelineEvent` + :class:`PipelineEventKind`
  drive the dashboard's ``DashboardState`` phase panel
  (``brutex/dashboard/app.py`` — constructed in production at the CLI
  ``serve`` entrypoint and passed to ``build_app``).
* :data:`EventSubscriber` + :class:`PipelineEvent` are the contract of the
  shipped Telegram subscriber factory
  (``brutex.notifications.wiring.make_pipeline_event_subscriber``).
* :class:`WorkItemState` rides along ONLY as the type of
  :attr:`PipelineEvent.item_state` (the dashboard reads
  ``event.item_state.value``); the FSM transition machinery around it is
  gone.

This module is a stdlib-only leaf — it imports NOTHING from the rest of the
orchestrator package, so every consumer (dashboard, notifications, CLI) can
import it without dragging in LMDB/bloom wheels.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final

# ────────────────────────────────────────────────────────────────────────
# Phases
# ────────────────────────────────────────────────────────────────────────


class IngestPhase(StrEnum):
    """The four sequential ingest phases (verbatim move from ``state.py``)."""

    A_INSTRUMENTS = "A_INSTRUMENTS"  # pragma: no mutate
    B_EXPIRIES = "B_EXPIRIES"  # pragma: no mutate
    C_CONTRACTS = "C_CONTRACTS"  # pragma: no mutate
    D_CANDLES = "D_CANDLES"  # pragma: no mutate


# ────────────────────────────────────────────────────────────────────────
# Work-item terminal-state labels
# ────────────────────────────────────────────────────────────────────────


class WorkItemState(StrEnum):
    """Work-item state labels (verbatim move from ``state.py``).

    Kept ONLY because :attr:`PipelineEvent.item_state` is typed with it and
    the dashboard's ``on_pipeline_event`` reads ``event.item_state.value``.
    The six-state FSM (``transition``/``dispatch``/``phase_complete``) that
    once moved items between these labels was deleted with ``state.py``.
    """

    # ── Non-terminal ──────────────────────────────────────────────
    PENDING = "PENDING"  # pragma: no mutate
    IN_FLIGHT = "IN_FLIGHT"  # pragma: no mutate
    RETRYING = "RETRYING"  # pragma: no mutate

    # ── Terminal ──────────────────────────────────────────────────
    SUCCESS = "SUCCESS"  # pragma: no mutate
    NO_DATA = "NO_DATA"  # pragma: no mutate
    QUARANTINED = "QUARANTINED"  # pragma: no mutate


# ────────────────────────────────────────────────────────────────────────
# Pipeline events — pushed to subscribers
# ────────────────────────────────────────────────────────────────────────


class PipelineEventKind:
    """String enum of pipeline event kind identifiers (RUN_START, PHASE_START, etc.)."""

    PHASE_START: Final[str] = "phase.start"
    PHASE_PROGRESS: Final[str] = "phase.progress"
    PHASE_COMPLETE: Final[str] = "phase.complete"
    PHASE_HALTED: Final[str] = "phase.halted"
    ITEM_COMPLETE: Final[str] = "item.complete"
    RUN_START: Final[str] = "run.start"
    RUN_COMPLETE: Final[str] = "run.complete"


@dataclass(frozen=True, slots=True)
class PipelineEvent:
    """One event on the pipeline event stream (verbatim move from ``pipeline.py``).

    Subscribers (Telegram notifier, dashboard) consume these to publish
    their own side-effects. Producers construct them; the shape is the
    COMMON contract between every producer and sink.
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


__all__ = [
    "EventSubscriber",
    "IngestPhase",
    "PipelineEvent",
    "PipelineEventKind",
    "WorkItemState",
]
