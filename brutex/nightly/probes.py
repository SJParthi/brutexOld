"""Nightly pre-mutation probes: disk floors + sweep liveness (DEC-SWEEP-NIGHTLY-AUTOMATION-001).

Two refusal gates the chain runs BEFORE touching anything:

* **Disk floors** — fail-CLOSED free-space checks on the lake volume and the
  backup volume (inventory-SIZED with headroom). Contrast with
  :func:`brutex.data.lake_footprint.make_disk_guard`, which fails OPEN because
  it protects a RUNNING pipeline; refusing a nightly before it starts is cheap
  and the correct R42 "nothing touched" answer.
* **Sweep liveness** — HONESTLY A HEURISTIC (dual mtime signal): the progress
  snapshot's ``updated_at`` is ``time.monotonic()`` (process-local, meaningless
  cross-process — sweep_progress.py), so file mtime is the ONLY sound
  cross-process freshness signal until P2's results-root flock lands; the
  probe prefers that lock the moment its file exists.

Env parses are FAIL-LOUD (:class:`NightlyEnvError` → CLI ``EXIT_USER_ERROR``
pre-chain — the ``resolve_cli_greeks_enrich`` precedent,
DEC-CLI-GREEKS-ENRICH-001, MIGRATION.md #1536): a junk value must never be
silently coerced into a floor of 0.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import math
import os
import shutil
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from brutex.data.guarded_read import read_regular_text
from brutex.models import BruteXError

__all__ = [
    "NIGHTLY_BACKUP_HEADROOM",
    "NIGHTLY_MIN_FREE_BACKUP_ENV",
    "NIGHTLY_MIN_FREE_LAKE_ENV",
    "NIGHTLY_SWEEP_FRESH_ENV",
    "DiskVerdict",
    "NightlyEnvError",
    "SweepLiveness",
    "check_disk_floors",
    "probe_sweep_liveness",
    "resolve_free_floor",
    "resolve_sweep_fresh_seconds",
]

#: Free-space floor on the LAKE volume (bytes). Default 20 GiB — the pull +
#: prebuild write here; an overnight ENOSPC mid-chain is the exact failure the
#: floor pre-empts.
NIGHTLY_MIN_FREE_LAKE_ENV: Final[str] = "BRUTEX_NIGHTLY_MIN_FREE_LAKE_BYTES"
_DEFAULT_MIN_FREE_LAKE_BYTES: Final[int] = 20 * 1024**3

#: Free-space floor on the BACKUP volume (bytes). Default 10 GiB; the actual
#: requirement is inventory-sized (see :func:`check_disk_floors`).
NIGHTLY_MIN_FREE_BACKUP_ENV: Final[str] = "BRUTEX_NIGHTLY_MIN_FREE_BACKUP_BYTES"
_DEFAULT_MIN_FREE_BACKUP_BYTES: Final[int] = 10 * 1024**3

#: Headroom multiplier over the backup inventory size (copies + manifest +
#: filesystem slack) when sizing the backup-volume requirement.
NIGHTLY_BACKUP_HEADROOM: Final[float] = 1.2

#: Freshness window for the sweep-liveness mtime signals (seconds; default 900
#: = 15 min — a supervisor publishes progress/checkpoint far more often than
#: this on a healthy sweep).
NIGHTLY_SWEEP_FRESH_ENV: Final[str] = "BRUTEX_NIGHTLY_SWEEP_FRESH_SECONDS"
_DEFAULT_SWEEP_FRESH_SECONDS: Final[float] = 900.0

#: The P2 results-root sweep single-flight lock (documented integration seam:
#: sweep-parallel-integration-contracts). The probe PREFERS this mechanical
#: signal the moment the file exists; until P2 lands it never does.
_SWEEP_LOCK_REL: Final[str] = ".locks/sweep_singleflight.lock"

#: Relative paths of the two mtime signals.
_PROGRESS_REL: Final[str] = "progress/sweep-progress.json"
_CHECKPOINT_REL: Final[str] = "checkpoint/sweep-all.json"


class NightlyEnvError(BruteXError):
    """A nightly env knob failed its fail-loud parse (junk / negative value)."""


def resolve_free_floor(env: Mapping[str, str] | None, key: str, default: int) -> int:
    """Resolve one byte-floor env knob, fail-loud.

    Missing/blank ⇒ *default*; a non-integer or negative value raises
    :class:`NightlyEnvError` naming the variable AND the offending value —
    never a silent coercion (Golden Rule #1).
    """
    source = os.environ if env is None else env
    raw = source.get(key, "")
    if not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise NightlyEnvError(f"{key} must be an integer byte count, got {raw!r}") from exc
    if value < 0:
        raise NightlyEnvError(f"{key} must be >= 0, got {raw!r}")
    return value


def resolve_sweep_fresh_seconds(env: Mapping[str, str] | None = None) -> float:
    """Resolve the liveness freshness window (seconds), fail-loud.

    Missing/blank ⇒ 900.0; junk / non-positive ⇒ :class:`NightlyEnvError`
    (a zero or negative window would blind the probe entirely).
    """
    source = os.environ if env is None else env
    raw = source.get(NIGHTLY_SWEEP_FRESH_ENV, "")
    if not raw.strip():
        return _DEFAULT_SWEEP_FRESH_SECONDS
    try:
        value = float(raw)
    except ValueError as exc:
        raise NightlyEnvError(
            f"{NIGHTLY_SWEEP_FRESH_ENV} must be a number of seconds, got {raw!r}"
        ) from exc
    if value <= 0:
        raise NightlyEnvError(f"{NIGHTLY_SWEEP_FRESH_ENV} must be > 0, got {raw!r}")
    return value


@dataclass(frozen=True, slots=True)
class DiskVerdict:
    """The disk-floor verdict — carries the EXACT bytes for the refusal message."""

    ok: bool
    lake_free: int
    lake_floor: int
    backup_free: int
    backup_required: int
    detail: str


def _nearest_existing(path: Path) -> Path:
    """Return *path* or its nearest EXISTING ancestor (the volume to probe).

    The backup root may not exist yet on a first run; its filesystem does.
    """
    probe = path
    while not probe.exists():
        parent = probe.parent
        if parent == probe:  # filesystem root — always exists, but be total
            return probe
        probe = parent
    return probe


def check_disk_floors(
    *,
    lake_root: Path,
    backup_root: Path,
    inventory_bytes: int,
    env: Mapping[str, str] | None = None,
    disk_usage: Callable[[str], Any] = shutil.disk_usage,
) -> DiskVerdict:
    """The DISK link's fail-closed floor check (pure reads, nothing touched).

    ``backup_required = max(backup_floor, ceil(NIGHTLY_BACKUP_HEADROOM ×
    inventory_bytes))`` — inventory-SIZED, so a big sweeps tree raises the bar
    beyond the static floor. Boundary law: ``free >= required`` passes; ONE
    byte under refuses (strict ``<``, test-pinned). The refusal detail carries
    the exact free/required byte numbers — they land verbatim in the morning
    Telegram so the operator knows precisely how much to free.

    Raises
    ------
    NightlyEnvError
        On a junk floor env value (fail-loud, pre-chain).
    """
    lake_floor = resolve_free_floor(env, NIGHTLY_MIN_FREE_LAKE_ENV, _DEFAULT_MIN_FREE_LAKE_BYTES)
    backup_floor = resolve_free_floor(
        env, NIGHTLY_MIN_FREE_BACKUP_ENV, _DEFAULT_MIN_FREE_BACKUP_BYTES
    )
    backup_required = max(backup_floor, math.ceil(NIGHTLY_BACKUP_HEADROOM * inventory_bytes))

    lake_free = int(disk_usage(str(_nearest_existing(lake_root))).free)
    backup_free = int(disk_usage(str(_nearest_existing(backup_root))).free)

    problems: list[str] = []
    if lake_free < lake_floor:
        problems.append(
            f"lake volume {lake_root}: free {lake_free} bytes < required {lake_floor} bytes"
        )
    if backup_free < backup_required:
        problems.append(
            f"backup volume {backup_root}: free {backup_free} bytes "
            f"< required {backup_required} bytes "
            f"(floor {backup_floor}, inventory {inventory_bytes} × {NIGHTLY_BACKUP_HEADROOM})"
        )
    if problems:
        detail = "disk floor breached — " + "; ".join(problems)
        return DiskVerdict(
            ok=False,
            lake_free=lake_free,
            lake_floor=lake_floor,
            backup_free=backup_free,
            backup_required=backup_required,
            detail=detail,
        )
    return DiskVerdict(
        ok=True,
        lake_free=lake_free,
        lake_floor=lake_floor,
        backup_free=backup_free,
        backup_required=backup_required,
        detail=(
            f"disk ok — lake free {lake_free} >= {lake_floor}; "
            f"backup free {backup_free} >= {backup_required}"
        ),
    )


@dataclass(frozen=True, slots=True)
class SweepLiveness:
    """The sweep-liveness verdict. ``heuristic`` is ALWAYS ``True`` (honest)."""

    live: bool
    evidence: str
    heuristic: bool


def _default_pid_alive(pid: int) -> bool:
    """Best-effort liveness of *pid* (signal 0). Unknown/foreign ⇒ assume alive.

    ``EPERM`` means the pid EXISTS but belongs to another user — alive. Any
    other failure reads NOT alive.
    """
    try:
        os.kill(pid, 0)
    except OSError as exc:
        return exc.errno == errno.EPERM
    return True


def probe_sweep_liveness(
    results_root: Path,
    *,
    fresh_seconds: float,
    now: Callable[[], float] = time.time,
    pid_alive: Callable[[int], bool] | None = None,
) -> SweepLiveness:
    """Dual-signal manual-sweep liveness probe — honestly a HEURISTIC.

    Signals, in trust order:

    1. **P2 results-root flock** (``<results_root>/.locks/sweep_singleflight
       .lock``): preferred the moment the file exists — a held flock is
       mechanical proof a sweep process is alive; an acquirable one is strong
       evidence none is. (P2 has not landed; this arm is the documented
       contract seam, exercised by contract-fixture tests.)
    2. **Progress snapshot** (``progress/sweep-progress.json``): ``state ==
       "running"`` AND file-mtime age ≤ *fresh_seconds* reads live. The
       snapshot's ``updated_at`` is ``time.monotonic()`` — process-local and
       meaningless cross-process — so the FILE MTIME is the only sound
       freshness signal. A ``pid`` key (forward-compat only; the snapshot has
       none today) demotes a fresh "running" to not-live when the pid is dead.
    3. **Checkpoint mtime** (``checkpoint/sweep-all.json``): a supervisor
       mid-tuple updates the checkpoint between progress publishes, so a
       fresh checkpoint ALSO reads live.

    Honest envelope (Golden Rule #10): false-negative possible (a wedged
    sweep silent > threshold reads not-live — bounded to wasted compute,
    never corruption: checkpoint/sentinel/cache laws); false-positive
    possible (a fresh file from a just-killed sweep — refuse-on-doubt is the
    safe direction). ``heuristic`` is always ``True``.
    """
    checker = _default_pid_alive if pid_alive is None else pid_alive

    # Signal 0 — the P2 flock, preferred when present.
    lock_path = results_root / _SWEEP_LOCK_REL
    if lock_path.exists():
        try:
            fd = os.open(lock_path, os.O_RDWR)
        except OSError:
            # Unreadable lock file: fall through to the mtime heuristics.
            pass
        else:
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return SweepLiveness(
                        live=True,
                        evidence=f"sweep single-flight lock HELD at {lock_path}",
                        heuristic=True,
                    )
                # Acquired ⇒ no holder; release immediately (probe only).
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    # Signal 1 — the progress snapshot (stat + read under ONE suppress, so a
    # vanishing/garbled file degrades to the checkpoint signal, never a crash).
    progress_path = results_root / _PROGRESS_REL
    snapshot: dict[str, Any] | None = None
    progress_mtime = 0.0
    with contextlib.suppress(OSError, ValueError):
        stat_result = progress_path.stat()
        loaded = json.loads(read_regular_text(progress_path))
        if isinstance(loaded, dict):
            snapshot = loaded
            progress_mtime = stat_result.st_mtime
    if snapshot is not None and snapshot.get("state") == "running":
        age = now() - progress_mtime
        if age <= fresh_seconds:
            pid = snapshot.get("pid")
            if isinstance(pid, int) and not checker(pid):
                return SweepLiveness(
                    live=False,
                    evidence=f"progress reads running (age {age:.0f}s) but pid {pid} is dead",
                    heuristic=True,
                )
            return SweepLiveness(
                live=True,
                evidence=f"progress state=running, file age {age:.0f}s <= {fresh_seconds:.0f}s",
                heuristic=True,
            )

    # Signal 2 — the checkpoint mtime.
    checkpoint_path = results_root / _CHECKPOINT_REL
    with contextlib.suppress(OSError):
        age = now() - checkpoint_path.stat().st_mtime
        if age <= fresh_seconds:
            return SweepLiveness(
                live=True,
                evidence=f"checkpoint {checkpoint_path.name} age {age:.0f}s <= {fresh_seconds:.0f}s",
                heuristic=True,
            )

    return SweepLiveness(
        live=False,
        evidence=f"no fresh sweep signal under {results_root} (window {fresh_seconds:.0f}s)",
        heuristic=True,
    )
