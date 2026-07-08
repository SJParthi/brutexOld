"""Automated incremental off-box backup of the BruteX lake (DEC-BACKUP-OFFBOX-001).

restic → S3 Object-Lock / B2 immutable, with an O(1) state-file DEAD-MAN
freshness check (CRITICAL Telegram via the existing
:class:`~brutex.notifications.sweep_alerts.SweepAlerter`) and a byte-verified
RESTORE DRILL (1 MiB known-bytes canary + stratified per-category sample +
the audit-JSONL byte-PREFIX law). Ops tooling ONLY — the ENGINE
(NIFTY/BANKNIFTY/SENSEX) is untouched; nothing here writes the lake.

Lightweight re-exports only — the submodules import stdlib + structlog + the
narrow COMMON leaves (``brutex.config`` / ``brutex.data.atomic_io`` /
``brutex.notifications`` / the pull-singleflight lock-path helper), never
``dashboard/`` and never polars/duckdb.
"""

from __future__ import annotations

from brutex.backup.deadman import (
    ALARM_VERDICTS,
    DeadmanReport,
    DeadmanVerdict,
    evaluate_deadman,
    run_deadman_check,
)
from brutex.backup.errors import (
    EXIT_BUSY,
    EXIT_CONFIG,
    EXIT_CORRUPTION,
    EXIT_DEADMAN_BREACH,
    EXIT_MAINTENANCE,
    EXIT_OK,
    EXIT_PREFLIGHT,
    EXIT_RESTIC,
    BackupBusyError,
    BackupError,
    BackupStateError,
)
from brutex.backup.plan import BackupPlan, PreflightVerdict, build_backup_plan, preflight_lake
from brutex.backup.restic_runner import ResticInvocation, ResticRunner
from brutex.backup.restore_drill import run_restore_drill
from brutex.backup.run import run_backup
from brutex.backup.state import (
    BackupState,
    DeadmanState,
    deadman_path,
    load_deadman,
    load_state,
    save_deadman,
    save_state,
    state_path,
)
from brutex.config.backup_env import BackupEnvConfig, resolve_backup_env

__all__ = [
    "ALARM_VERDICTS",
    "EXIT_BUSY",
    "EXIT_CONFIG",
    "EXIT_CORRUPTION",
    "EXIT_DEADMAN_BREACH",
    "EXIT_MAINTENANCE",
    "EXIT_OK",
    "EXIT_PREFLIGHT",
    "EXIT_RESTIC",
    "BackupBusyError",
    "BackupEnvConfig",
    "BackupError",
    "BackupPlan",
    "BackupState",
    "BackupStateError",
    "DeadmanReport",
    "DeadmanState",
    "DeadmanVerdict",
    "PreflightVerdict",
    "ResticInvocation",
    "ResticRunner",
    "build_backup_plan",
    "deadman_path",
    "evaluate_deadman",
    "load_deadman",
    "load_state",
    "preflight_lake",
    "resolve_backup_env",
    "run_backup",
    "run_deadman_check",
    "run_restore_drill",
    "save_deadman",
    "save_state",
    "state_path",
]
