"""Persist the :class:`~brutex.data.window_status.WindowStatusLog` beside the lake.

DEC-WINDOW-STATUS-001. The per-window status log is only useful ACROSS runs if it
SURVIVES one — so the browsable status table reflects the FULL history (a window
FAILED last pull, SUCCESS this pull). This is that durable backing: a
deterministic JSON sidecar at ``<lake_root>/window_status.json`` round-tripping
:meth:`WindowStatusLog.to_records` / :meth:`WindowStatusLog.from_records`.

Design (priority law — Correctness > Reliability > …):

* **Correctness.** The on-disk rows are :meth:`to_records` output (sorted,
  deterministic), so the file is byte-reproducible for the same log and a
  load→save→load is a fixed point (CLAUDE.md §2 Rule 7).
* **Reliability — never crash a pull.** :func:`load_window_status` degrades to an
  EMPTY log (a structured WARNING, never an exception) on a missing / unreadable
  / malformed file — a lost status file costs visibility, never data.
* **Reliability — crash-safe write.** :func:`save_window_status` writes a sibling
  ``*.tmp`` then :func:`os.replace` (atomic on POSIX within one filesystem).

The O(1) record / lookup stays in :class:`WindowStatusLog`; only the one-shot
load (run start) + save (run end) are O(N) over the recorded windows.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import structlog

from brutex.data.window_status import WindowStatusLog

#: Structured logger — surfaces a corrupt/absent sidecar into ``/logs``.
_LOG = structlog.get_logger("brutex.data.window_status_store")

#: The status sidecar filename, written directly under the lake root.
STATUS_FILENAME: str = "window_status.json"


def status_path(lake_root: Path) -> Path:
    """Return the status sidecar path for *lake_root* (``<lake_root>/window_status.json``)."""
    return lake_root / STATUS_FILENAME


def load_window_status(lake_root: Path) -> WindowStatusLog:
    """Load the persisted status log for *lake_root*, or an EMPTY one if unavailable.

    **Never raises** (reliability): a missing file is a clean empty log; a
    malformed / wrong-shaped / unreadable file logs a WARNING and also degrades
    to empty (a lost status file costs only visibility).
    """
    path = status_path(lake_root)
    if not path.is_file():
        return WindowStatusLog()
    try:
        raw = path.read_text(encoding="utf-8")
        records = json.loads(raw)
        coerced = [
            (
                str(market),
                str(security_id),
                str(interval),
                str(window_start),
                str(window_end),
                str(status),
                int(bars),
                int(attempts),
                (None if last_error is None else str(last_error)),
                str(last_attempt_at),
            )
            for (
                market,
                security_id,
                interval,
                window_start,
                window_end,
                status,
                bars,
                attempts,
                last_error,
                last_attempt_at,
            ) in records
        ]
        return WindowStatusLog.from_records(coerced)
    except (OSError, ValueError, TypeError) as exc:
        _LOG.warning("window_status.load_failed", path=str(path), error=str(exc))
        return WindowStatusLog()


def save_window_status(log: WindowStatusLog, lake_root: Path) -> None:
    """Persist *log* to *lake_root*'s sidecar atomically (crash-safe).

    Serialises :meth:`WindowStatusLog.to_records` (sorted, deterministic) to JSON,
    writes a sibling ``*.tmp`` then :func:`os.replace` onto the final path. The
    lake root is created if absent. O(N) over the recorded windows (once per run).
    """
    lake_root.mkdir(parents=True, exist_ok=True)
    path = status_path(lake_root)
    tmp = path.with_name(f"{STATUS_FILENAME}.tmp")
    tmp.write_text(json.dumps(log.to_records()), encoding="utf-8")
    os.replace(tmp, path)


def begin_pull_status(lake_root: Path, *, dry_run: bool) -> WindowStatusLog | None:
    """Resolve the status log to thread into a pull (load the persisted one for a live run).

    A **live** pull loads the persisted log so the status table reflects the full
    cross-run history; a **dry-run** returns ``None`` (no recording, no disk
    touch) — its synthetic fetch would otherwise pollute the real status table.
    """
    if dry_run:
        return None
    return load_window_status(lake_root)


def end_pull_status(log: WindowStatusLog | None, lake_root: Path, *, dry_run: bool) -> None:
    """Persist *log* after a live pull — a no-op for dry-run or an absent log."""
    if not dry_run and log is not None:
        save_window_status(log, lake_root)


__all__ = [
    "STATUS_FILENAME",
    "begin_pull_status",
    "end_pull_status",
    "load_window_status",
    "save_window_status",
    "status_path",
]
