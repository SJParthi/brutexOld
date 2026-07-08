"""Backup error hierarchy + the deterministic exit-code vocabulary (DEC-BACKUP-OFFBOX-001).

Every failure path in :mod:`brutex.backup` maps to a deterministic triple —
exit code + ``backup.*`` structlog event + alert (or an explicit no-alert
rationale) — per the design's failure-mode matrix. The exit codes live HERE
(one source of truth) so the CLI, launchd logs, and tests all read the same
vocabulary.
"""

from __future__ import annotations

from typing import Final

#: Success — the run/check/drill completed with nothing to alarm about.
EXIT_OK: Final[int] = 0

#: Dead-man breach — any alarm verdict (HUNG / OVERDUE / NEVER_SUCCEEDED /
#: STATE_CORRUPT / CLOCK_SKEW / remote divergence).
EXIT_DEADMAN_BREACH: Final[int] = 1

#: Unusable configuration (missing repo, password sources ≠ exactly one, no
#: recorded snapshot for a drill) — refused BEFORE touching restic (F6/F19).
EXIT_CONFIG: Final[int] = 2

#: Preflight refusal — unmounted /Volumes lake, missing lake root, an empty
#: backup set (F1/F2), or a preflight/plan-build I/O failure
#: (``preflight_io``). Never snapshots, never stamps success.
EXIT_PREFLIGHT: Final[int] = 3

#: restic invocation failure — missing binary, remote unreachable, timeout,
#: init/auth failure, or a state-write failure after the run started (F3/F4/
#: F5/F7/F9/F10/F17a).
EXIT_RESTIC: Final[int] = 4

#: Another backup already holds the single-flight lock — benign overlap, no
#: alert (F15; the running backup is making progress).
EXIT_BUSY: Final[int] = 5

#: Verified corruption — ``restic check`` failure or a restore-drill byte
#: mismatch (F11/F20). Nightly-verify arm: success is NOT stamped, so the
#: dead-man escalates too. Drill arm: nightly successes keep stamping — the
#: dead-man stays QUIET; the CRITICAL page is the ONLY automated signal
#: (the per-arm honesty of the round-5 alert-body split).
EXIT_CORRUPTION: Final[int] = 6

#: Backup + verify succeeded but maintenance (``forget``/``--prune``) failed
#: (F23). The data-success stamp STANDS — the data IS off-box.
EXIT_MAINTENANCE: Final[int] = 7


class BackupError(Exception):
    """Base error for the off-box backup system (:mod:`brutex.backup`)."""


class BackupStateError(BackupError):
    """The runner-owned ``state.json`` is unreadable / corrupt / schema-drifted.

    Raised by :func:`brutex.backup.state.load_state` on ANY defect (bad JSON,
    wrong schema int, missing field, unparseable or naive timestamp) — the
    dead-man checker treats it as a hard STATE_CORRUPT alarm (F13), never an
    assume-fine.
    """


class BackupBusyError(BackupError):
    """Another backup run already holds the single-flight lock (F15 — benign)."""


__all__ = [
    "EXIT_BUSY",
    "EXIT_CONFIG",
    "EXIT_CORRUPTION",
    "EXIT_DEADMAN_BREACH",
    "EXIT_MAINTENANCE",
    "EXIT_OK",
    "EXIT_PREFLIGHT",
    "EXIT_RESTIC",
    "BackupBusyError",
    "BackupError",
    "BackupStateError",
]
