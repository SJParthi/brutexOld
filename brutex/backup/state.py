"""Durable backup state — TWO tiny JSON files, single-writer-per-file (DEC-BACKUP-OFFBOX-001).

The nightly runner OWNS ``<state_dir>/state.json`` (:class:`BackupState`); the
hourly dead-man checker OWNS ``<state_dir>/deadman.json``
(:class:`DeadmanState`) — no read-modify-write race between the two one-shot
processes. Both files follow the :mod:`brutex.config.runtime` JSON pattern
(``sort_keys`` stable serialisation, UTC ``…Z`` stamps) and are written
atomically owner-only via :func:`brutex.data.atomic_io.atomic_write_simple`
(``chmod=0o600``) — deliberately NOT ``atomic_write_via``, so a state-file
ENOSPC surfaces as a raw ``OSError`` for the caller to map (F10) and is never
mis-escalated to the lake's ``LakeWriteHalt``
(DEC-ATOMIC-WRITE-CONSOLIDATE-001 split).

Trust-root asymmetry (fail-toward-alarm):

* ``state.json`` is the dead-man's TRUST ROOT — any parse defect raises
  :class:`~brutex.backup.errors.BackupStateError` (F13; a hard alarm, and the
  corrupt file is NEVER overwritten by the checker — forensics preserved).
* ``deadman.json`` is only a re-alert THROTTLE — a missing/corrupt sidecar
  loads as EMPTY (+ ONE WARN): losing it can only cause an EXTRA alert, never
  a missed one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final

import structlog

from brutex.backup.errors import BackupStateError
from brutex.data.atomic_io import atomic_write_simple
from brutex.data.guarded_read import read_regular_text

if TYPE_CHECKING:
    from brutex.config.backup_env import BackupEnvConfig

_LOG = structlog.get_logger(__name__)

#: Schema version stamped into both state files; a mismatch is a parse defect.
STATE_SCHEMA_VERSION: Final[int] = 1

#: Owner-only mode for both state files (they are operational, not secret, but
#: the 0600 default matches every other ``~/.brutex`` sidecar).
_STATE_FILE_MODE: Final[int] = 0o600


def _stamp_to_json(value: datetime | None) -> str | None:
    """Serialise an aware UTC datetime to an ISO-8601 ``…Z`` string (or ``None``)."""
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _stamp_from_json(field: str, raw: object) -> datetime | None:
    """Parse an ISO-8601 ``…Z`` stamp; any defect → :class:`BackupStateError`."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise BackupStateError(f"state field {field!r} must be an ISO-8601 string, got {raw!r}")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BackupStateError(f"state field {field!r} has unparseable timestamp {raw!r}") from exc
    if parsed.tzinfo is None:
        raise BackupStateError(f"state field {field!r} timestamp {raw!r} is naive (no timezone)")
    return parsed.astimezone(UTC)


def _require(data: dict[str, object], field: str) -> object:
    """Return ``data[field]``; a missing field → :class:`BackupStateError`."""
    if field not in data:
        raise BackupStateError(f"state file missing required field {field!r}")
    return data[field]


def _require_int(data: dict[str, object], field: str) -> int:
    """Return ``data[field]`` as an int (bools rejected — JSON true/false ≠ counter)."""
    value = _require(data, field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise BackupStateError(f"state field {field!r} must be an integer, got {value!r}")
    return value


def _require_schema(data: dict[str, object]) -> int:
    """Validate the ``schema`` field equals :data:`STATE_SCHEMA_VERSION`."""
    schema = _require_int(data, "schema")
    if schema != STATE_SCHEMA_VERSION:
        raise BackupStateError(
            f"state schema {schema} != expected {STATE_SCHEMA_VERSION} (schema drift)"
        )
    return schema


def _optional_str(data: dict[str, object], field: str) -> str | None:
    """Return ``data[field]`` as ``str | None``; any other type is a defect."""
    value = _require(data, field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise BackupStateError(f"state field {field!r} must be a string or null, got {value!r}")
    return value


def _optional_int(data: dict[str, object], field: str) -> int | None:
    """Return ``data[field]`` as ``int | None``; any other type is a defect."""
    value = _require(data, field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise BackupStateError(f"state field {field!r} must be an integer or null, got {value!r}")
    return value


@dataclass(frozen=True, slots=True)
class BackupState:
    """Runner-OWNED durable state — ``<state_dir>/state.json``.

    Attributes
    ----------
    schema
        :data:`STATE_SCHEMA_VERSION` at write time.
    running_since
        Set at run start, cleared at EVERY exit (the F17b RUNNING/HUNG arm).
    last_attempt_at
        UTC stamp of the last run start (all datetimes below likewise
        tz-aware UTC).
    last_success_at
        UTC stamp of the last VERIFIED successful backup (the dead-man input).
    last_snapshot_id
        The snapshot id of the last verified success (the drill's source).
    last_failure_at
        UTC stamp of the last failed run.
    last_failure_reason
        Short reason token of the last failure (``"timeout"`` / …).
    consecutive_failures
        Failed runs since the last success.
    consecutive_partials
        Consecutive rc-3 partial runs (the F8 streak; success still stamped).
    maintenance_ok
        Outcome of the last ``forget``/prune (F23 — never retracts success).
    verify_subset_cursor
        Rotation cursor ``k`` for ``--read-data-subset=(cursor % 56)+1/56``;
        advances only after a SUCCESSFUL subset check.
    lake_device
        ``LakeRootIdentity.device`` at the last success (F1 drift WARN).
    """

    schema: int
    running_since: datetime | None
    last_attempt_at: datetime | None
    last_success_at: datetime | None
    last_snapshot_id: str | None
    last_failure_at: datetime | None
    last_failure_reason: str | None
    consecutive_failures: int
    consecutive_partials: int
    maintenance_ok: bool
    verify_subset_cursor: int
    lake_device: int | None

    def to_json(self) -> str:
        """Serialise to a byte-stable JSON string (``sort_keys``, ``…Z`` stamps)."""
        return json.dumps(
            {
                "schema": self.schema,
                "running_since": _stamp_to_json(self.running_since),
                "last_attempt_at": _stamp_to_json(self.last_attempt_at),
                "last_success_at": _stamp_to_json(self.last_success_at),
                "last_snapshot_id": self.last_snapshot_id,
                "last_failure_at": _stamp_to_json(self.last_failure_at),
                "last_failure_reason": self.last_failure_reason,
                "consecutive_failures": self.consecutive_failures,
                "consecutive_partials": self.consecutive_partials,
                "maintenance_ok": self.maintenance_ok,
                "verify_subset_cursor": self.verify_subset_cursor,
                "lake_device": self.lake_device,
            },
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, raw: str) -> BackupState:
        """Parse a :class:`BackupState`; ANY defect → :class:`BackupStateError`.

        Fail loud, never a crash-loop: bad JSON, a non-object payload, a wrong
        schema int, a missing field, an unparseable stamp, and a naive
        datetime all raise the SAME typed error the dead-man maps to a hard
        STATE_CORRUPT alarm (F13).
        """
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BackupStateError(f"state.json is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise BackupStateError(f"state.json must be a JSON object, got {type(data).__name__}")
        _require_schema(data)
        maintenance_ok = _require(data, "maintenance_ok")
        if not isinstance(maintenance_ok, bool):
            raise BackupStateError(
                f"state field 'maintenance_ok' must be a boolean, got {maintenance_ok!r}"
            )
        return cls(
            schema=STATE_SCHEMA_VERSION,
            running_since=_stamp_from_json("running_since", _require(data, "running_since")),
            last_attempt_at=_stamp_from_json("last_attempt_at", _require(data, "last_attempt_at")),
            last_success_at=_stamp_from_json("last_success_at", _require(data, "last_success_at")),
            last_snapshot_id=_optional_str(data, "last_snapshot_id"),
            last_failure_at=_stamp_from_json("last_failure_at", _require(data, "last_failure_at")),
            last_failure_reason=_optional_str(data, "last_failure_reason"),
            consecutive_failures=_require_int(data, "consecutive_failures"),
            consecutive_partials=_require_int(data, "consecutive_partials"),
            maintenance_ok=maintenance_ok,
            verify_subset_cursor=_require_int(data, "verify_subset_cursor"),
            lake_device=_optional_int(data, "lake_device"),
        )


def empty_backup_state() -> BackupState:
    """Return the all-empty baseline state a first-ever run starts from."""
    return BackupState(
        schema=STATE_SCHEMA_VERSION,
        running_since=None,
        last_attempt_at=None,
        last_success_at=None,
        last_snapshot_id=None,
        last_failure_at=None,
        last_failure_reason=None,
        consecutive_failures=0,
        consecutive_partials=0,
        maintenance_ok=True,
        verify_subset_cursor=0,
        lake_device=None,
    )


@dataclass(frozen=True, slots=True)
class DeadmanState:
    """Checker-OWNED sidecar — ``<state_dir>/deadman.json`` (a throttle, not a trust root).

    Attributes
    ----------
    schema
        :data:`STATE_SCHEMA_VERSION` at write time.
    last_overdue_alert_at
        Cross-run re-alert bound stamp (F12/F13/F14/F17b/F21) — written IFF a
        publish actually reached a live notifier (only-stamp-on-success).
    last_deep_check_at
        Durable gate for the daily auto deep cross-check (F21).
    """

    schema: int = STATE_SCHEMA_VERSION
    last_overdue_alert_at: datetime | None = None
    last_deep_check_at: datetime | None = None

    def to_json(self) -> str:
        """Serialise to a byte-stable JSON string (``sort_keys``, ``…Z`` stamps)."""
        return json.dumps(
            {
                "schema": self.schema,
                "last_overdue_alert_at": _stamp_to_json(self.last_overdue_alert_at),
                "last_deep_check_at": _stamp_to_json(self.last_deep_check_at),
            },
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, raw: str) -> DeadmanState:
        """Parse a :class:`DeadmanState`; ANY defect → :class:`BackupStateError`.

        The CALLER (:func:`load_deadman`) maps the error to an empty sidecar +
        ONE WARN — the sidecar is a throttle, so losing it fails toward an
        EXTRA alert, never a missed one.
        """
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BackupStateError(f"deadman.json is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise BackupStateError(f"deadman.json must be a JSON object, got {type(data).__name__}")
        _require_schema(data)
        return cls(
            schema=STATE_SCHEMA_VERSION,
            last_overdue_alert_at=_stamp_from_json(
                "last_overdue_alert_at", _require(data, "last_overdue_alert_at")
            ),
            last_deep_check_at=_stamp_from_json(
                "last_deep_check_at", _require(data, "last_deep_check_at")
            ),
        )


def state_path(cfg: BackupEnvConfig) -> Path:
    """Return the runner-owned state file path — ``<state_dir>/state.json``."""
    return cfg.state_dir / "state.json"


def deadman_path(cfg: BackupEnvConfig) -> Path:
    """Return the checker-owned sidecar path — ``<state_dir>/deadman.json``."""
    return cfg.state_dir / "deadman.json"


def lock_path(cfg: BackupEnvConfig) -> Path:
    """Return the single-flight lock file path — ``<state_dir>/backup.lock``."""
    return cfg.state_dir / "backup.lock"


def canary_dir(cfg: BackupEnvConfig) -> Path:
    """Return the restore-drill canary directory — ``<state_dir>/canary``."""
    return cfg.state_dir / "canary"


def drill_tmp_dir(cfg: BackupEnvConfig) -> Path:
    """Return the restore-drill scratch directory — ``<state_dir>/drill-tmp``."""
    return cfg.state_dir / "drill-tmp"


def load_state(path: Path) -> BackupState | None:
    """Load the runner-owned state file.

    Returns
    -------
    BackupState | None
        ``None`` when the file does not exist (a fresh install — the dead-man
        reads it as NEVER_SUCCEEDED).

    Raises
    ------
    BackupStateError
        On an unreadable file (``OSError``), a non-UTF-8 byte (bit-rot —
        ``UnicodeDecodeError``, a ``ValueError`` subclass the ``OSError`` arm
        alone would let escape raw; round-4 MAJOR, the RF-3/#1188 precedent),
        or ANY parse defect — the F13 hard alarm; the corrupt file is never
        overwritten by the checker.
    """
    try:
        raw = read_regular_text(path)
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise BackupStateError(f"failed to read {path}: {exc}") from exc
    return BackupState.from_json(raw)


def save_state(path: Path, state: BackupState) -> None:
    """Atomically save the runner-owned state file (0600).

    ``OSError`` (e.g. ENOSPC) propagates RAW for the caller to map (F10 —
    alarm-over-silence: an unstamped success can only cause a FALSE alarm,
    never silence). Parent directory is created if needed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_simple(
        path,
        lambda tmp: tmp.write_text(state.to_json() + "\n", encoding="utf-8"),
        chmod=_STATE_FILE_MODE,
    )


def load_deadman(path: Path) -> DeadmanState:
    """Load the checker-owned sidecar; missing/corrupt → EMPTY (+ ONE WARN on corrupt).

    The sidecar is a re-alert throttle, not a trust root — a lost/corrupt
    sidecar can only cause an EXTRA alert, never a missed one
    (fail-toward-alarm). The corrupt-file WARN is
    ``backup.deadman.sidecar_corrupt``.
    """
    try:
        raw = read_regular_text(path)
    except FileNotFoundError:
        return DeadmanState()
    except (OSError, UnicodeDecodeError) as exc:
        # UnicodeDecodeError (bit-rot — a ValueError, NOT an OSError) must
        # ride the same corrupt→EMPTY arm or it escapes the hourly checker
        # raw, violating this docstring's own contract (round-4 MAJOR).
        _LOG.warning(
            "backup.deadman.sidecar_corrupt", path=str(path), error_type=type(exc).__name__
        )
        return DeadmanState()
    try:
        return DeadmanState.from_json(raw)
    except BackupStateError as exc:
        _LOG.warning("backup.deadman.sidecar_corrupt", path=str(path), error=str(exc))
        return DeadmanState()


def save_deadman(path: Path, state: DeadmanState) -> None:
    """Atomically save the checker-owned sidecar (0600); ``OSError`` propagates raw."""
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_simple(
        path,
        lambda tmp: tmp.write_text(state.to_json() + "\n", encoding="utf-8"),
        chmod=_STATE_FILE_MODE,
    )


__all__ = [
    "STATE_SCHEMA_VERSION",
    "BackupState",
    "DeadmanState",
    "canary_dir",
    "deadman_path",
    "drill_tmp_dir",
    "empty_backup_state",
    "load_deadman",
    "load_state",
    "lock_path",
    "save_deadman",
    "save_state",
    "state_path",
]
