"""Durable pull journal — start/heartbeat/finish records under the lake root (G5/G6).

DEC-DOCTOR-LIVE-PROOF-KIT-001. Today a pull's verdict lives ONLY in process
memory (``PullProgress`` + the module-level ``_last_pull_verdict`` dict), so a
mid-pull process death — reboot, battery, uvicorn kill, logout SIGTERM, OOM —
leaves the morning dashboard simply IDLE: the operator cannot distinguish
finished / never-started / died (register row G5). And a MacBook that SLEEPS
mid-pull suspends the worker threads with zero trace (row G6).

This module is the durable counterpart: an append-only JSONL journal under
``<lake_root>/journal/pull_journal.jsonl``. Each record carries BOTH the wall
clock (UTC ISO) and the monotonic clock, so a host-sleep window is detectable
after the fact as ``wall_delta − monotonic_delta`` between consecutive records
(the monotonic clock pauses across sleep on macOS — ``mach_absolute_time``
semantics; treated as the honest best-effort signal, not a proof).

Write discipline: the record functions NEVER raise (an unwritable journal must
never break a pull) — an ``OSError`` degrades to a WARNING log. Read
discipline: the reader tolerates crash-truncated / garbled trailing lines
(skips them), because a power loss mid-append is exactly the scenario the
journal exists to survive.

Consumed by ``brutex doctor --live`` stage 12; written by the dashboard bulk
pull (:func:`brutex.dashboard.pull_controller.pull_all_spot`). CLI ``brutex
fetch`` wiring is a follow-up (noted in the kit PR).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import structlog

from brutex.data.guarded_read import read_regular_text

_LOG = structlog.get_logger("brutex.observability.pull_journal")

#: Journal directory + file name under the lake root.
JOURNAL_DIR_NAME: Final[str] = "journal"
JOURNAL_FILE_NAME: Final[str] = "pull_journal.jsonl"

#: Event kinds.
EVENT_STARTED: Final[str] = "started"
EVENT_HEARTBEAT: Final[str] = "heartbeat"
EVENT_FINISHED: Final[str] = "finished"
_KNOWN_EVENTS: Final[frozenset[str]] = frozenset({EVENT_STARTED, EVENT_HEARTBEAT, EVENT_FINISHED})

#: Minimum wall-vs-monotonic divergence (seconds) treated as a host-suspend gap.
#: Small drifts (NTP slew, scheduler jitter) stay far below this; a lid-close
#: sleep is minutes-to-hours above it.
SLEEP_GAP_THRESHOLD_S: Final[float] = 120.0


@dataclass(frozen=True, slots=True)
class JournalEvent:
    """One durable journal record.

    Attributes
    ----------
    event
        ``"started"`` / ``"heartbeat"`` / ``"finished"``.
    pull_id
        The pull's correlation id (the same ``pull_id`` structlog binds).
    wall_utc
        Wall-clock instant (UTC ISO-8601 string).
    monotonic_s
        Monotonic-clock reading (seconds) captured with the same record.
    dry_run
        The pull's dry-run flag (``started`` records; ``None`` elsewhere).
    verdict
        The pull's final verdict kind (``finished`` records; ``None`` elsewhere).
    """

    event: str
    pull_id: str
    wall_utc: str
    monotonic_s: float
    dry_run: bool | None = None
    verdict: str | None = None


@dataclass(frozen=True, slots=True)
class SleepGap:
    """A detected host-suspend window between two consecutive journal records."""

    after_wall_utc: str
    before_wall_utc: str
    gap_seconds: float


@dataclass(frozen=True, slots=True)
class JournalRun:
    """The records of ONE pull run (a ``started`` record + everything after it)."""

    started: JournalEvent
    heartbeats: tuple[JournalEvent, ...]
    finished: JournalEvent | None

    @property
    def interrupted(self) -> bool:
        """``True`` iff the run has a start but NO finish record (died mid-run)."""
        return self.finished is None

    @property
    def events(self) -> tuple[JournalEvent, ...]:
        """All of the run's records in order (started, heartbeats…, finished?)."""
        tail = (self.finished,) if self.finished is not None else ()
        return (self.started, *self.heartbeats, *tail)


def journal_path(lake_root: Path) -> Path:
    """The journal file path for *lake_root* (not created)."""
    return lake_root / JOURNAL_DIR_NAME / JOURNAL_FILE_NAME


def _append(lake_root: Path, record: dict[str, object]) -> None:
    """Append one JSON line, NEVER raising (an unwritable journal can't break a pull)."""
    path = journal_path(lake_root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError as exc:
        _LOG.warning("pull_journal.write_failed", error=str(exc), path=str(path))


def _stamp(now_utc: datetime | None, monotonic_s: float | None) -> tuple[str, float]:
    """Resolve the (wall, monotonic) pair, defaulting to the real clocks."""
    wall = (now_utc if now_utc is not None else datetime.now(UTC)).isoformat()
    mono = monotonic_s if monotonic_s is not None else time.monotonic()
    return wall, mono


def record_pull_start(
    lake_root: Path,
    *,
    pull_id: str,
    dry_run: bool,
    now_utc: datetime | None = None,
    monotonic_s: float | None = None,
) -> None:
    """Durably record that a pull STARTED (never raises)."""
    wall, mono = _stamp(now_utc, monotonic_s)
    _append(
        lake_root,
        {
            "event": EVENT_STARTED,
            "pull_id": pull_id,
            "wall_utc": wall,
            "monotonic_s": mono,
            "dry_run": dry_run,
        },
    )


def record_pull_heartbeat(
    lake_root: Path,
    *,
    pull_id: str,
    now_utc: datetime | None = None,
    monotonic_s: float | None = None,
) -> None:
    """Durably record a mid-pull HEARTBEAT (never raises)."""
    wall, mono = _stamp(now_utc, monotonic_s)
    _append(
        lake_root,
        {"event": EVENT_HEARTBEAT, "pull_id": pull_id, "wall_utc": wall, "monotonic_s": mono},
    )


def record_pull_finish(
    lake_root: Path,
    *,
    pull_id: str,
    verdict: str,
    now_utc: datetime | None = None,
    monotonic_s: float | None = None,
) -> None:
    """Durably record that a pull FINISHED with *verdict* (never raises)."""
    wall, mono = _stamp(now_utc, monotonic_s)
    _append(
        lake_root,
        {
            "event": EVENT_FINISHED,
            "pull_id": pull_id,
            "wall_utc": wall,
            "monotonic_s": mono,
            "verdict": verdict,
        },
    )


def _event_from_obj(obj: object) -> JournalEvent | None:
    """Parse one JSON object into a :class:`JournalEvent`, else ``None`` (tolerant)."""
    if not isinstance(obj, dict):
        return None
    event = obj.get("event")
    pull_id = obj.get("pull_id")
    wall = obj.get("wall_utc")
    mono = obj.get("monotonic_s")
    if (
        not isinstance(event, str)
        or event not in _KNOWN_EVENTS
        or not isinstance(pull_id, str)
        or not isinstance(wall, str)
        or not isinstance(mono, (int, float))
        or isinstance(mono, bool)
    ):
        return None
    dry_run = obj.get("dry_run")
    verdict = obj.get("verdict")
    return JournalEvent(
        event=event,
        pull_id=pull_id,
        wall_utc=wall,
        monotonic_s=float(mono),
        dry_run=dry_run if isinstance(dry_run, bool) else None,
        verdict=verdict if isinstance(verdict, str) else None,
    )


def read_journal(lake_root: Path) -> list[JournalEvent]:
    """Read every parseable journal record in file order (crash-truncation tolerant).

    A missing file is an empty journal. A garbled / truncated line (the
    power-loss-mid-append case) is SKIPPED, never fatal — the journal must be
    readable after exactly the crashes it exists to diagnose.
    """
    path = journal_path(lake_root)
    if not path.is_file():
        return []
    events: list[JournalEvent] = []
    for line in read_regular_text(path).splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            continue
        event = _event_from_obj(obj)
        if event is not None:
            events.append(event)
    return events


def last_run(events: list[JournalEvent]) -> JournalRun | None:
    """Group the LAST pull run from *events* (``None`` when no run ever started)."""
    start_index: int | None = None
    for index in range(len(events) - 1, -1, -1):
        if events[index].event == EVENT_STARTED:
            start_index = index
            break
    if start_index is None:
        return None
    started = events[start_index]
    heartbeats: list[JournalEvent] = []
    finished: JournalEvent | None = None
    for event in events[start_index + 1 :]:
        if event.pull_id != started.pull_id:
            continue
        if event.event == EVENT_HEARTBEAT:
            heartbeats.append(event)
        else:
            # Only heartbeat/finished can follow the anchoring LAST ``started``
            # (a later ``started`` would itself be the anchor) — so: finished.
            finished = event
    return JournalRun(started=started, heartbeats=tuple(heartbeats), finished=finished)


def sleep_gaps(run: JournalRun, *, threshold_s: float = SLEEP_GAP_THRESHOLD_S) -> list[SleepGap]:
    """Detect host-suspend windows inside *run* — wall advanced, monotonic did not (G6).

    For each consecutive record pair the suspend estimate is
    ``(wall_b − wall_a) − (mono_b − mono_a)``; gaps ≥ *threshold_s* are
    returned. With only a (start, finish) pair this yields the WHOLE-pull
    suspend total (coarse but honest); heartbeats narrow the window. An
    unparseable wall timestamp skips that pair (tolerant, never fatal).
    """
    from itertools import pairwise

    gaps: list[SleepGap] = []
    for previous, current in pairwise(run.events):
        try:
            wall_a = datetime.fromisoformat(previous.wall_utc)
            wall_b = datetime.fromisoformat(current.wall_utc)
        except ValueError:
            continue
        wall_delta = (wall_b - wall_a).total_seconds()
        mono_delta = current.monotonic_s - previous.monotonic_s
        gap = wall_delta - mono_delta
        if gap >= threshold_s:
            gaps.append(
                SleepGap(
                    after_wall_utc=previous.wall_utc,
                    before_wall_utc=current.wall_utc,
                    gap_seconds=gap,
                )
            )
    return gaps


__all__ = [
    "EVENT_FINISHED",
    "EVENT_HEARTBEAT",
    "EVENT_STARTED",
    "JOURNAL_DIR_NAME",
    "JOURNAL_FILE_NAME",
    "SLEEP_GAP_THRESHOLD_S",
    "JournalEvent",
    "JournalRun",
    "SleepGap",
    "journal_path",
    "last_run",
    "read_journal",
    "record_pull_finish",
    "record_pull_heartbeat",
    "record_pull_start",
    "sleep_gaps",
]
