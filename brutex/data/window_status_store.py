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
from collections.abc import Callable
from pathlib import Path
from time import monotonic

import structlog

from brutex.data.guarded_read import read_regular_text
from brutex.data.window_status import WindowStatusLog

#: Structured logger — surfaces a corrupt/absent sidecar into ``/logs``.
_LOG = structlog.get_logger("brutex.data.window_status_store")

#: The status sidecar filename, written directly under the lake root.
STATUS_FILENAME: str = "window_status.json"

#: Minimum seconds between live mid-pull status flushes (DEC-WINDOW-STATUS-LIVE-001).
#: Small enough that /coverage/windows feels live; large enough that a multi-
#: thousand-window JSON is not rewritten on every completed pair.
LIVE_FLUSH_MIN_INTERVAL_S: float = 2.0


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
        raw = read_regular_text(path)
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


def make_live_status_flush(
    log: WindowStatusLog | None,
    lake_root: Path,
    *,
    min_interval_s: float = LIVE_FLUSH_MIN_INTERVAL_S,
    clock: Callable[[], float] = monotonic,
) -> Callable[[], None]:
    """Return a THROTTLED mid-pull status-flush callback (DEC-WINDOW-STATUS-LIVE-001).

    **WRITE-RETIRED** (DEC-STATUS-SIDECAR-WRITE-RETIRE-001): no production pull
    drives this any more — the unified ledger's adaptive
    :func:`~brutex.data.window_ledger_store.make_live_ledger_flush` is the live
    mid-pull flush. Kept (with its tests) only until the Stage-6 deletion.

    During a long pull every window's SUCCESS / EMPTY / FAILED record accumulates
    in *log* in memory, but the sidecar is only written by :func:`end_pull_status`
    at the very END — so ``/coverage/windows`` shows ``0 windows`` for the whole
    run and only "jumps" to the final table once the pull finishes. This returns a
    callable fired after each completed pair to persist *log* MID-pull, so the
    page ticks up live.

    Throttled to at most once per *min_interval_s* (default
    :data:`LIVE_FLUSH_MIN_INTERVAL_S`): a several-thousand-window JSON must not be
    rewritten on every one of ~1,500 pairs. A ``None`` *log* (a dry-run, which
    records nothing) yields a no-op callable. *clock* / *min_interval_s* are
    injectable for deterministic tests. O(N) only on the calls that actually write.
    """
    last_write: list[float | None] = [None]

    def _flush() -> None:
        if log is None:
            return
        now = clock()
        if last_write[0] is not None and now - last_write[0] < min_interval_s:
            return
        last_write[0] = now
        save_window_status(log, lake_root)

    return _flush


def begin_pull_status(lake_root: Path, *, dry_run: bool) -> WindowStatusLog | None:
    """Resolve the status log to thread into a pull (load the persisted one for a live run).

    **WRITE-RETIRED** (DEC-STATUS-SIDECAR-WRITE-RETIRE-001): no production pull
    calls this any more — the unified ledger lifecycle
    (:func:`~brutex.data.window_ledger_store.begin_pull_ledger`) replaced it.
    Kept until the Stage-6 deletion. A **live** pull loads the persisted log so
    the status table reflects the full cross-run history; a **dry-run** returns
    ``None`` (no recording, no disk touch).
    """
    if dry_run:
        return None
    return load_window_status(lake_root)


def end_pull_status(log: WindowStatusLog | None, lake_root: Path, *, dry_run: bool) -> None:
    """Persist *log* after a live pull — a no-op for dry-run or an absent log.

    **WRITE-RETIRED** (DEC-STATUS-SIDECAR-WRITE-RETIRE-001): no production pull
    calls this any more; kept until the Stage-6 deletion.
    """
    if not dry_run and log is not None:
        save_window_status(log, lake_root)


__all__ = [
    "LIVE_FLUSH_MIN_INTERVAL_S",
    "STATUS_FILENAME",
    "begin_pull_status",
    "end_pull_status",
    "load_window_status",
    "make_live_status_flush",
    "save_window_status",
    "status_path",
]
