"""Quarantine-not-abort mechanics for the sweep run core (G1/G12 — DEC-SWEEP-UNKILLABLE-CORE-001).

Two forensic quarantine paths, both MOVE-never-delete (evidence is kept for
the operator's post-mortem) and both under
``<results_root>/.quarantine/`` — a dot-prefixed namespace no existing results
reader/healer globs:

* **G1 — corrupt tuple-store trio** (:func:`quarantine_tuple_trio`). A
  garbage per-tuple Parquet previously ABORTED the whole grid. The
  orchestrator seam (run-core wiring) moves the trio — completion sentinel
  FIRST, then the trial-stats sidecar, then the store — to
  ``.quarantine/runtime/<prefix>-<n>/`` and redoes the tuple once. The
  sentinel moves FIRST so a crash mid-quarantine can never leave a
  sentinel-paired-but-quarantined store: without its sentinel the tuple is
  incomplete by the existing redo law, so recovery is deterministic.
* **G12 — corrupt checkpoint** (:func:`load_checkpoint_resilient`). A
  malformed checkpoint JSON crash-looped every relaunch. The resilient loader
  quarantines it to ``.quarantine/checkpoints/<n>.json`` (forensic copy
  kept), WARNs, appends a ``QUARANTINE`` flight record, and returns ``None``
  (fresh start — the per-tuple completion sentinels make the redo cheap and
  byte-identical). A checkpoint for a FOREIGN sweep id keeps the EXISTING law:
  ``None`` WITHOUT quarantine (it is another sweep's valid state, not
  corruption). A SECOND consecutive corruption in-process is the crash-loop
  edge: ONE ERROR log + the injected ``on_crash_loop`` callback (the run-core
  wiring binds it to the CRITICAL Telegram alert).

Directory ordinals are MONOTONIC integers (``<prefix>-1``, ``<prefix>-2`` …)
— NEVER a wall-clock dirname (I-2: wall time stays out of every on-disk
identity; the ordinal is derived from the existing sibling dirs, max + 1).

Honest envelope (Golden Rule #10): the ordinal scan is O(existing quarantine
entries) — quarantines are rare (each one is a WARN), so the dir stays tiny;
this is not claimed O(1). The trio move raises on an unexpected ``OSError``
(a dead disk mid-quarantine is a global-stop class); the checkpoint move is
fail-soft (returning ``None`` is safe even if the corrupt file remains — the
next launch re-quarantines and the tracker escalates).
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final

import structlog

from brutex.engine.sweep_checkpoint import (
    SweepCheckpoint,
    SweepCheckpointError,
    SweepCheckpointStore,
)
from brutex.engine.trial_stats import TRIAL_STATS_SUFFIX
from brutex.engine.tuple_complete import COMPLETION_SUFFIX

if TYPE_CHECKING:  # pragma: no cover — typing-only import (runtime is duck-typed)
    from brutex.observability.flight_recorder import FlightRecorder

__all__ = [
    "CHECKPOINT_QUARANTINE_SUBDIR",
    "RUNTIME_QUARANTINE_SUBDIR",
    "CheckpointCorruptionTracker",
    "QuarantineOutcome",
    "load_checkpoint_resilient",
    "quarantine_tuple_trio",
]

_LOG = structlog.get_logger("brutex.engine.sweep_quarantine")

#: Quarantine home for corrupt tuple-store trios (under the results root).
RUNTIME_QUARANTINE_SUBDIR: Final[str] = ".quarantine/runtime"

#: Quarantine home for corrupt checkpoint files (under the results root).
CHECKPOINT_QUARANTINE_SUBDIR: Final[str] = ".quarantine/checkpoints"

#: ``<prefix>-<n>`` dirname ordinal tail (ASCII digits only — a foreign-named
#: sibling never confuses the monotonic scan).
_ORDINAL_RE: Final[re.Pattern[str]] = re.compile(r"^(\d+)$")


@dataclass(frozen=True, slots=True)
class QuarantineOutcome:
    """One tuple-trio quarantine result (immutable).

    Attributes
    ----------
    moved
        Basenames of the members actually moved, in MOVE ORDER (sentinel →
        trial-stats → store); empty when none of the trio existed.
    dest
        The quarantine directory the members were moved into.
    reason
        The caller's quarantine reason (forensics + the flight record).
    """

    moved: tuple[str, ...]
    dest: Path
    reason: str


@dataclass(slots=True)
class CheckpointCorruptionTracker:
    """Consecutive checkpoint-corruption counter — the G12 crash-loop breaker edge.

    Mirrors the scheduler kernel's consecutive-failure shape (D-5):
    :meth:`record_corruption` returns ``True`` EXACTLY when the consecutive
    count reaches *threshold* (the edge — fires once, not on every further
    corruption); :meth:`record_clean` resets. The design's "2nd consecutive
    corruption in-process → CRITICAL" is ``threshold=2`` (the default).

    Attributes
    ----------
    threshold
        Consecutive corruptions at which the edge fires (``>= 1``).
    consecutive
        The current consecutive-corruption count (managed; starts 0).
    """

    threshold: int = 2
    consecutive: int = field(default=0)

    def __post_init__(self) -> None:
        if self.threshold < 1:
            raise ValueError(f"threshold must be >= 1, got {self.threshold}")

    def record_corruption(self) -> bool:
        """Count one corruption; return ``True`` iff this is the escalation edge."""
        self.consecutive += 1
        return self.consecutive == self.threshold

    def record_clean(self) -> None:
        """Reset the consecutive count (a clean load breaks the loop evidence)."""
        self.consecutive = 0


def _next_ordinal(parent: Path, *, prefix: str = "", suffix: str = "") -> int:
    """Return the next monotonic ordinal for ``<prefix><n><suffix>`` entries under *parent*.

    Scans the existing entries whose name is exactly ``<prefix><digits><suffix>``
    and returns ``max(n) + 1`` (``1`` when none). Foreign names — a different
    prefix/suffix, a non-digit core — are ignored, so an operator-dropped file
    can never break the scan. O(existing entries); never a wall-clock name (I-2).
    """
    highest = 0
    if parent.is_dir():
        for entry in parent.iterdir():
            name = entry.name
            if not (name.startswith(prefix) and name.endswith(suffix)):
                continue
            core = name[len(prefix) : len(name) - len(suffix)] if suffix else name[len(prefix) :]
            match = _ORDINAL_RE.match(core)
            if match is not None:
                highest = max(highest, int(match.group(1)))
    return highest + 1


def quarantine_tuple_trio(
    store_path: Path,
    *,
    results_root: Path,
    reason: str,
    run_id: str | None = None,
) -> QuarantineOutcome:
    """Move a corrupt tuple's trio into forensic quarantine (G1) — never delete.

    Moves ``<store>.complete.json`` (the completion sentinel, FIRST — see the
    module docstring's crash-ordering law), then ``<store>.trials.json``, then
    the store itself into
    ``<results_root>/.quarantine/runtime/<prefix>-<n>/`` where *prefix* is
    *run_id* (when given) else the store's stem, and *n* is the monotonic
    ordinal. Members that do not exist are skipped (a corrupt tuple may have
    torn before writing its sidecars); a member vanishing mid-move
    (``FileNotFoundError`` race) is skipped the same way.

    Parameters
    ----------
    store_path
        The tuple's store Parquet path.
    results_root
        The sweep results root (hosts ``.quarantine/``).
    reason
        Why the trio is being quarantined (forensics; carried on the outcome).
    run_id
        Optional run identity used as the quarantine dirname prefix (D-1);
        defaults to the store's stem.

    Returns
    -------
    QuarantineOutcome
        The moved basenames (in move order), the destination dir, the reason.

    Raises
    ------
    OSError
        On an unexpected move failure (a dead disk mid-quarantine is a
        global-stop class — deliberately not swallowed).
    """
    parent = results_root / RUNTIME_QUARANTINE_SUBDIR
    prefix = run_id if run_id is not None else store_path.stem
    dest = parent / f"{prefix}-{_next_ordinal(parent, prefix=f'{prefix}-')}"
    dest.mkdir(parents=True, exist_ok=True)
    members = (
        store_path.with_name(store_path.name + COMPLETION_SUFFIX),
        store_path.with_name(store_path.name + TRIAL_STATS_SUFFIX),
        store_path,
    )
    moved: list[str] = []
    for member in members:
        if not member.exists():
            continue
        try:
            os.replace(member, dest / member.name)
        except FileNotFoundError:
            continue  # vanished between the exists() probe and the move — skip
        moved.append(member.name)
    _LOG.warning(
        "sweep_quarantine.tuple_trio",
        store=str(store_path),
        dest=str(dest),
        moved=list(moved),
        reason=reason,
    )
    return QuarantineOutcome(moved=tuple(moved), dest=dest, reason=reason)


def load_checkpoint_resilient(
    store: SweepCheckpointStore,
    *,
    results_root: Path,
    recorder: FlightRecorder | None,
    tracker: CheckpointCorruptionTracker | None = None,
    on_crash_loop: Callable[[str], object] | None = None,
) -> SweepCheckpoint | None:
    """Load the sweep checkpoint, quarantining corruption instead of crash-looping (G12).

    A clean load passes through (and resets *tracker*): a valid checkpoint is
    returned; a missing file or a FOREIGN-sweep-id checkpoint returns ``None``
    WITHOUT quarantine (the store's existing law — another sweep's valid state
    is not corruption). A :class:`~brutex.engine.sweep_checkpoint.SweepCheckpointError`
    (garbage/truncated JSON, unknown schema) moves the file to
    ``.quarantine/checkpoints/<n>.json`` (forensic copy kept), WARNs, appends
    a ``QUARANTINE`` flight record, counts the corruption on *tracker*, and
    returns ``None`` — the run starts fresh and the per-tuple completion
    sentinels make the redo cheap and byte-identical.

    Crash-loop breaker: when *tracker* reports the consecutive-corruption edge
    (default: the 2nd in-process corruption), ONE
    ``sweep_quarantine.checkpoint_crash_loop`` ERROR is logged and
    *on_crash_loop* (when bound — the run core wires it to the CRITICAL
    Telegram alert) is invoked with the corruption detail.

    Parameters
    ----------
    store
        The checkpoint store to load through (its file is the quarantine
        subject on corruption).
    results_root
        The sweep results root (hosts ``.quarantine/``).
    recorder
        The run's flight recorder for the ``QUARANTINE`` record, or ``None``
        (record skipped — the WARN still fires).
    tracker
        The in-process consecutive-corruption tracker, or ``None`` (no
        escalation edge — each corruption still quarantines + WARNs).
    on_crash_loop
        Escalation callback invoked ONCE at the tracker edge with the
        corruption detail; ``None`` = log-only escalation.

    Returns
    -------
    SweepCheckpoint | None
        The loaded checkpoint, or ``None`` (fresh start).
    """
    try:
        checkpoint = store.load()
    except SweepCheckpointError as exc:
        detail = str(exc)
        dest = _quarantine_checkpoint(store.path, results_root=results_root)
        _LOG.warning(
            "sweep_quarantine.checkpoint",
            path=str(store.path),
            dest=str(dest) if dest is not None else None,
            error=detail,
        )
        if recorder is not None:
            recorder.record(
                "QUARANTINE",
                {
                    "what": "checkpoint",
                    "path_rel": os.path.relpath(store.path, results_root),
                    "reason": detail,
                },
            )
        if tracker is not None and tracker.record_corruption():
            _LOG.error(
                "sweep_quarantine.checkpoint_crash_loop",
                path=str(store.path),
                consecutive=tracker.consecutive,
                error=detail,
            )
            if on_crash_loop is not None:
                on_crash_loop(detail)
        return None
    if tracker is not None:
        tracker.record_clean()
    return checkpoint


def _quarantine_checkpoint(path: Path, *, results_root: Path) -> Path | None:
    """Move the corrupt checkpoint at *path* into quarantine — fail-soft.

    Returns the destination, or ``None`` when the move itself failed (WARNed;
    returning fresh-start ``None`` upstream is safe even with the corrupt file
    left in place — the next launch re-quarantines and the tracker escalates).
    """
    parent = results_root / CHECKPOINT_QUARANTINE_SUBDIR
    dest = parent / f"{_next_ordinal(parent, suffix='.json')}.json"
    try:
        parent.mkdir(parents=True, exist_ok=True)
        os.replace(path, dest)
    except OSError as exc:
        _LOG.warning(
            "sweep_quarantine.checkpoint_move_failed",
            path=str(path),
            dest=str(dest),
            error=str(exc),
        )
        return None
    return dest
