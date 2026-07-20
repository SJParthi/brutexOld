"""Unit tests for :mod:`brutex.orchestrator.events` — the live pipeline shapes.

DEC-ORCH-DEAD-SCAFFOLD-DELETE-001 (MIGRATION.md #1727 deletion record):
``events.py`` holds the verbatim-moved LIVE shapes of the deleted
``state.py``/``pipeline.py`` scaffolding. The pins here are harvested from
the deleted suites' shape tests — they freeze the CONTRACT the dashboard
(``DashboardState.on_pipeline_event``) and the Telegram subscriber factory
(``make_pipeline_event_subscriber``) consume, so a silent value/field drift
fails loudly.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields
from datetime import UTC, datetime

import pytest
from brutex.orchestrator.events import (
    EventSubscriber,
    IngestPhase,
    PipelineEvent,
    PipelineEventKind,
    WorkItemState,
)


class TestIngestPhase:
    """The four sequential ingest phase labels (verbatim from state.py)."""

    def test_exact_member_values(self) -> None:
        assert [(p.name, p.value) for p in IngestPhase] == [
            ("A_INSTRUMENTS", "A_INSTRUMENTS"),
            ("B_EXPIRIES", "B_EXPIRIES"),
            ("C_CONTRACTS", "C_CONTRACTS"),
            ("D_CANDLES", "D_CANDLES"),
        ]

    def test_is_str_enum(self) -> None:
        # The dashboard keys its phase panel on ``phase.value`` strings and
        # uses members directly in f-strings — StrEnum semantics required.
        assert isinstance(IngestPhase.A_INSTRUMENTS, str)
        assert f"{IngestPhase.D_CANDLES}" == "D_CANDLES"


class TestWorkItemState:
    """The work-item state labels PipelineEvent.item_state is typed with."""

    def test_exact_member_values(self) -> None:
        assert [(s.name, s.value) for s in WorkItemState] == [
            ("PENDING", "PENDING"),
            ("IN_FLIGHT", "IN_FLIGHT"),
            ("RETRYING", "RETRYING"),
            ("SUCCESS", "SUCCESS"),
            ("NO_DATA", "NO_DATA"),
            ("QUARANTINED", "QUARANTINED"),
        ]

    def test_value_attribute_read(self) -> None:
        # app.py:on_pipeline_event serialises ``event.item_state.value``.
        assert WorkItemState.SUCCESS.value == "SUCCESS"
        assert isinstance(WorkItemState.QUARANTINED, str)


class TestPipelineEventKind:
    """The dotted event-kind identifier strings (verbatim from pipeline.py)."""

    def test_exact_kind_strings(self) -> None:
        assert PipelineEventKind.PHASE_START == "phase.start"
        assert PipelineEventKind.PHASE_PROGRESS == "phase.progress"
        assert PipelineEventKind.PHASE_COMPLETE == "phase.complete"
        assert PipelineEventKind.PHASE_HALTED == "phase.halted"
        assert PipelineEventKind.ITEM_COMPLETE == "item.complete"
        assert PipelineEventKind.RUN_START == "run.start"
        assert PipelineEventKind.RUN_COMPLETE == "run.complete"


class TestPipelineEvent:
    """The one event dataclass every producer and sink shares."""

    def test_minimal_construction_defaults(self) -> None:
        at = datetime(2026, 7, 11, 9, 15, tzinfo=UTC)
        event = PipelineEvent(kind=PipelineEventKind.RUN_START, at=at)
        assert event.kind == "run.start"
        assert event.at is at
        # Every optional field defaults to None — the sinks' emit-on-present law.
        for name in (
            "phase",
            "run_id",
            "total",
            "done",
            "success",
            "no_data",
            "quarantined",
            "retried",
            "item_id",
            "item_state",
            "elapsed_seconds",
            "message",
        ):
            assert getattr(event, name) is None, name

    def test_exact_field_order(self) -> None:
        # The frozen wire-shape pin: adding/removing/reordering a field is a
        # conscious contract change for BOTH sinks (dashboard + Telegram).
        assert [f.name for f in fields(PipelineEvent)] == [
            "kind",
            "at",
            "phase",
            "run_id",
            "total",
            "done",
            "success",
            "no_data",
            "quarantined",
            "retried",
            "item_id",
            "item_state",
            "elapsed_seconds",
            "message",
        ]

    def test_full_construction_carries_values(self) -> None:
        at = datetime(2026, 7, 11, 10, 0, tzinfo=UTC)
        event = PipelineEvent(
            kind=PipelineEventKind.ITEM_COMPLETE,
            at=at,
            phase=IngestPhase.D_CANDLES,
            run_id="run-1",
            total=10,
            done=3,
            success=2,
            no_data=1,
            quarantined=0,
            retried=4,
            item_id="item-7",
            item_state=WorkItemState.SUCCESS,
            elapsed_seconds=1.5,
            message="ok",
        )
        assert event.phase is IngestPhase.D_CANDLES
        assert event.item_state is WorkItemState.SUCCESS
        assert event.item_state.value == "SUCCESS"  # the app.py serialisation read
        assert (event.total, event.done, event.retried) == (10, 3, 4)

    def test_frozen(self) -> None:
        event = PipelineEvent(
            kind=PipelineEventKind.RUN_COMPLETE,
            at=datetime(2026, 7, 11, 15, 30, tzinfo=UTC),
        )
        with pytest.raises(FrozenInstanceError):
            event.kind = "phase.start"  # type: ignore[misc]

    def test_event_subscriber_alias_accepts_plain_callable(self) -> None:
        # EventSubscriber is Callable[[PipelineEvent], None] — the wiring
        # factory returns a closure and the dashboard passes a bound method.
        seen: list[PipelineEvent] = []
        subscriber: EventSubscriber = seen.append
        event = PipelineEvent(
            kind=PipelineEventKind.PHASE_PROGRESS,
            at=datetime(2026, 7, 11, 11, 0, tzinfo=UTC),
            phase=IngestPhase.B_EXPIRIES,
            done=1,
            total=2,
        )
        subscriber(event)
        assert seen == [event]
