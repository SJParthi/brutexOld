"""Service discovery file — ``~/.brutex/runtime.json``.

Implements ``FR-UI-007``: BruteX writes this file on startup so that a
separate terminal (or IntelliJ Services panel, or a browser) can find
the dashboard URL, metrics URL, PID, and current phase of a running
ingest without having to scroll through startup logs.

Contract
--------
* Written **on startup**, after all port binds have succeeded.
* Updated **periodically** (every phase transition at minimum; the
  dashboard's WebSocket event loop also updates ``phase`` in real time).
* Deleted **on clean exit** via SIGTERM handler in ``brutex.cli``.
* **Stale entries** (PID no longer alive) are auto-cleaned by
  ``brutex status`` the next time it runs. This handles crashes,
  SIGKILLs, and power losses.

Concurrency
-----------
The file is rewritten atomically via "write to temp file, then rename"
so a reader never observes a partial write. POSIX rename is atomic
on the same filesystem. There is no file locking — multiple BruteX
processes writing to the same file would race, but the design
explicitly supports only **one instance per user at a time** (multi-
instance use sets ``BRUTEX_RUNTIME_FILE`` per instance).

Security
--------
The file contains only non-sensitive data: PID, run ID, URLs, phase,
and a sanitised snapshot of ``BRUTEX_*`` env vars **excluding any
``*_TOKEN*`` / ``*_SECRET*`` / ``*_PASSWORD*`` / ``*_API_KEY*`` /
``*_AUTH*`` / ``*_CLIENT_ID*`` patterns**. Vendor API credentials, Telegram
bot tokens, and any similar credentials are redacted via
:func:`_redact_env_snapshot`, and the file is written owner-only (0600).
"""

from __future__ import annotations

import errno
import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from brutex.data.atomic_io import atomic_write_simple
from brutex.models import BruteXError

# ────────────────────────────────────────────────────────────────────────
# Paths and constants
# ────────────────────────────────────────────────────────────────────────

#: Default runtime file path. Lives under the user's home directory.
DEFAULT_RUNTIME_FILE: Final[Path] = Path.home() / ".brutex" / "runtime.json"

#: Env var to override the runtime file location (useful for tests and
#: multi-user / CI scenarios).
ENV_RUNTIME_FILE: Final[str] = "BRUTEX_RUNTIME_FILE"

#: Env var name pattern for secrets that must be redacted. CLIENT_ID is
#: included (R2-CS-1): vendor client identifiers are credential material and
#: must not land in a world-readable service-discovery file.
_SECRET_ENV_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(TOKEN|SECRET|PASSWORD|API_KEY|AUTH|CLIENT_ID)",
    re.IGNORECASE,
)


def runtime_file_path() -> Path:
    """Return the active runtime file path, honouring ``BRUTEX_RUNTIME_FILE``."""
    override = os.environ.get(ENV_RUNTIME_FILE)
    if override:
        return Path(override).expanduser()
    return DEFAULT_RUNTIME_FILE


# ────────────────────────────────────────────────────────────────────────
# Errors
# ────────────────────────────────────────────────────────────────────────


class RuntimeFileError(BruteXError):
    """Raised on any failure to read, parse, or write the runtime file."""


# ────────────────────────────────────────────────────────────────────────
# Info dataclass
# ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RuntimeInfo:
    """Snapshot of a running BruteX process, written to the runtime file.

    Attributes
    ----------
    pid
        OS process ID of the BruteX ingest process.
    run_id
        UUID of the current ingest run (matches the audit log).
    started_at
        ISO-8601 UTC timestamp of when the process started.
    host_bind
        The interface the dashboard is bound to (``0.0.0.0``, ``127.0.0.1``, etc.).
    dashboard_urls
        List of user-facing dashboard URLs. First is "primary".
    metrics_url
        Prometheus scrape URL.
    phase
        Current :class:`~brutex.orchestrator.state.IngestPhase` value,
        or ``"idle"`` / ``"starting"`` / ``"shutting_down"``.
    env_snapshot
        Sanitised copy of ``BRUTEX_*`` env vars (secrets redacted).
    environment
        Detected runtime environment (terminal/cowork/intellij/docker).
    """

    pid: int
    run_id: str
    started_at: str  # ISO-8601 UTC
    host_bind: str
    dashboard_urls: tuple[str, ...]
    metrics_url: str
    phase: str
    env_snapshot: tuple[tuple[str, str], ...]  # sorted tuple for determinism
    environment: str  # RuntimeEnvironment.value

    def to_json(self) -> str:
        """Serialise to a JSON string (pretty, stable key order)."""
        data = {
            "pid": self.pid,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "host_bind": self.host_bind,
            "dashboard_urls": list(self.dashboard_urls),
            "metrics_url": self.metrics_url,
            "phase": self.phase,
            "env_snapshot": dict(self.env_snapshot),
            "environment": self.environment,
        }
        return json.dumps(data, indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> RuntimeInfo:
        """Parse a RuntimeInfo from a JSON string."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeFileError(f"runtime.json is not valid JSON: {exc}") from exc

        required = {
            "pid",
            "run_id",
            "started_at",
            "host_bind",
            "dashboard_urls",
            "metrics_url",
            "phase",
            "env_snapshot",
            "environment",
        }
        missing = required - data.keys()
        if missing:
            raise RuntimeFileError(f"runtime.json missing required fields: {sorted(missing)}")

        env_snapshot = tuple(sorted(data["env_snapshot"].items()))
        dashboard_urls = tuple(data["dashboard_urls"])
        return cls(
            pid=int(data["pid"]),
            run_id=str(data["run_id"]),
            started_at=str(data["started_at"]),
            host_bind=str(data["host_bind"]),
            dashboard_urls=dashboard_urls,
            metrics_url=str(data["metrics_url"]),
            phase=str(data["phase"]),
            env_snapshot=env_snapshot,
            environment=str(data["environment"]),
        )


# ────────────────────────────────────────────────────────────────────────
# Env snapshot helpers
# ────────────────────────────────────────────────────────────────────────


def _redact_env_snapshot(environ: dict[str, str]) -> dict[str, str]:
    """Return a copy of *environ* filtered to ``BRUTEX_*`` with secrets redacted.

    * Only keys starting with ``BRUTEX_`` are kept.
    * Keys matching :data:`_SECRET_ENV_PATTERN` (TOKEN / SECRET /
      PASSWORD / API_KEY / AUTH / CLIENT_ID, case-insensitive) have their
      values replaced with ``"<redacted>"``.
    """
    out: dict[str, str] = {}
    for key, value in environ.items():
        if not key.startswith("BRUTEX_"):
            continue
        if _SECRET_ENV_PATTERN.search(key):
            out[key] = "<redacted>"
        else:
            out[key] = value
    return out


def build_runtime_info(
    *,
    pid: int,
    run_id: str,
    host_bind: str,
    dashboard_urls: list[str],
    metrics_url: str,
    phase: str,
    environment: str,
    started_at: datetime | None = None,
) -> RuntimeInfo:
    """Convenience factory that fills in ``env_snapshot`` and ``started_at``."""
    snapshot = _redact_env_snapshot(dict(os.environ))
    started = started_at if started_at is not None else datetime.now(UTC)
    return RuntimeInfo(
        pid=pid,
        run_id=run_id,
        started_at=started.isoformat().replace("+00:00", "Z"),
        host_bind=host_bind,
        dashboard_urls=tuple(dashboard_urls),
        metrics_url=metrics_url,
        phase=phase,
        env_snapshot=tuple(sorted(snapshot.items())),
        environment=environment,
    )


# ────────────────────────────────────────────────────────────────────────
# File I/O
# ────────────────────────────────────────────────────────────────────────


def write_runtime_file(info: RuntimeInfo, *, path: Path | None = None) -> Path:
    """Atomically write *info* to the runtime file.

    Creates the parent directory if needed. Uses the standard
    "write tempfile + rename" pattern so readers never observe a
    partial file.

    Parameters
    ----------
    info
        The :class:`RuntimeInfo` to serialise.
    path
        Override the target path. If ``None``, uses :func:`runtime_file_path`.

    Returns
    -------
    Path
        The path that was written.
    """
    target = path if path is not None else runtime_file_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    # The COMMON crash-safe atomic write (tmp→fsync→os.replace→cleanup), guard-free so
    # a runtime-file failure surfaces as RuntimeFileError — NOT the lake's LakeWriteHalt
    # (CLAUDE.md §11, DEC-ATOMIC-WRITE-CONSOLIDATE-001). Readers never observe a partial.
    # chmod=0o600 (R2-CS-1): the env snapshot is redacted but still names every
    # BRUTEX_* var — owner-only, matching the credential store's 0600, and set on
    # the temp file BEFORE the rename so it is never briefly world-readable.
    try:
        atomic_write_simple(
            target,
            lambda tmp: tmp.write_text(info.to_json() + "\n", encoding="utf-8"),
            chmod=0o600,
        )
    except OSError as exc:
        raise RuntimeFileError(f"Failed to write {target}: {exc}") from exc
    return target


def read_runtime_file(*, path: Path | None = None) -> RuntimeInfo | None:
    """Read and parse the runtime file, returning ``None`` if missing.

    Parameters
    ----------
    path
        Override the source path. If ``None``, uses :func:`runtime_file_path`.

    Returns
    -------
    RuntimeInfo | None
        ``None`` if the file does not exist; otherwise the parsed info.

    Raises
    ------
    RuntimeFileError
        If the file exists but cannot be read or parsed.
    """
    source = path if path is not None else runtime_file_path()
    if not source.exists():
        return None
    from brutex.data.guarded_read import (
        read_regular_text,
    )

    try:
        raw = read_regular_text(source)
    except OSError as exc:
        raise RuntimeFileError(f"Failed to read {source}: {exc}") from exc
    return RuntimeInfo.from_json(raw)


def delete_runtime_file(*, path: Path | None = None) -> bool:
    """Delete the runtime file if it exists.

    Returns
    -------
    bool
        ``True`` if a file was deleted, ``False`` if it did not exist.
    """
    target = path if path is not None else runtime_file_path()
    try:
        target.unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise RuntimeFileError(f"Failed to delete {target}: {exc}") from exc
    return True


# ────────────────────────────────────────────────────────────────────────
# Stale detection
# ────────────────────────────────────────────────────────────────────────


def is_pid_alive(pid: int) -> bool:
    """Return ``True`` if a process with *pid* is still running on this host.

    Uses ``os.kill(pid, 0)`` — signal 0 is "check for existence" and
    does not actually deliver a signal. Handles three cases:

    * Process exists and we have permission → ``True``
    * Process exists but we lack permission (different UID) → ``True``
      (we know it's alive, we just can't signal it)
    * Process does not exist (``ESRCH``) → ``False``
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except ProcessLookupError:
        return False
    except OSError as exc:
        # ESRCH == "no such process" → dead. Any other errno is
        # unexpected — treat as alive to avoid deleting a
        # possibly-valid runtime file on an ambiguous error.
        return exc.errno != errno.ESRCH
    else:
        return True


def clear_stale_runtime(*, path: Path | None = None) -> bool:
    """Delete the runtime file if its PID is no longer alive.

    Called by ``brutex status`` before reading the file, so the user
    gets an honest "no running instance" message after a crash rather
    than stale PID references.

    Returns
    -------
    bool
        ``True`` if a stale file was cleared, ``False`` if there was
        nothing to clear (file missing, or PID still alive).
    """
    info = read_runtime_file(path=path)
    if info is None:
        return False
    if is_pid_alive(info.pid):
        return False
    delete_runtime_file(path=path)
    return True


__all__ = [
    "DEFAULT_RUNTIME_FILE",
    "ENV_RUNTIME_FILE",
    "RuntimeFileError",
    "RuntimeInfo",
    "build_runtime_info",
    "clear_stale_runtime",
    "delete_runtime_file",
    "is_pid_alive",
    "read_runtime_file",
    "runtime_file_path",
    "write_runtime_file",
]
