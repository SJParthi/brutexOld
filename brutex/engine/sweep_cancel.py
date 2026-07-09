"""Durable cooperative-cancel sidecar for the grid sweep (DEC-JOB-COOPERATIVE-CANCEL-001).

The ONE cancel-request mechanism every sweep surface shares: a small fsync'd JSON
sidecar ``<results_root>/.sweep_cancel.json`` (the sibling of the
DEC-SWEEP-STALL-OOM-GUARDS-001 quarantine sidecar ``.sweep_quarantine.json``).
A cancel REQUESTER (the dashboard ``POST /api/v1/sweep/cancel`` route, or an
operator ``touch``-equivalent) writes the sidecar; the RUNNING sweep probes for
it at the tuple boundary via :func:`make_cancel_probe` — ONE ``os.stat`` presence
probe per tuple, O(1) — and stops COOPERATIVELY after the in-flight tuple's
durable write chain (store flush → completion sentinel → cache record →
checkpoint record) has landed. Because the sidecar is a plain file, the SAME
mechanism covers the dashboard's in-process sweep, a CLI ``brutex sweep-all``,
AND the detached launch-page sweep — no per-process token is needed.

Semantics (the honest contract):

* **Presence IS the signal.** :func:`cancel_requested` never parses the file —
  a malformed / unreadable-body sidecar still cancels (fail-safe toward a
  GRACEFUL stop, which is always harmless: cancel ≡ a clean stop at the tuple
  boundary, never a corruption). The payload (:func:`request_cancel`) exists
  only for operator forensics (who asked, when).
* **Stale-file safety is LAUNCH-side.** ``cmd_sweep_all`` calls
  :func:`clear_cancel` before the grid starts, so a leftover sidecar from a
  prior cancelled run can never kill a fresh/resumed run. This module never
  auto-expires the file.
* **Cooperative, never instant** (Golden Rule #10): the probe is consulted at
  the TUPLE boundary only — cancel latency is the in-flight tuple's remaining
  runtime (a heavy k=5 tuple legitimately runs many minutes; the
  DEC-SWEEP-STALL-OOM-GUARDS-001 arithmetic), and a genuinely-wedged worker
  ignores it (the stall watchdog remains that class's honest terminal).

Stdlib + :mod:`brutex.data.atomic_io` only — no engine imports (a LEAF, so the
CLI, the engine drivers, and the dashboard route can all import it freely).
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import structlog

from brutex.data.atomic_io import atomic_write_simple

_LOG = structlog.get_logger("brutex.engine.sweep_cancel")

#: The durable cancel-request sidecar filename — a SIBLING of the
#: DEC-SWEEP-STALL-OOM-GUARDS-001 quarantine sidecar under the results root.
CANCEL_SIDECAR_NAME: Final[str] = ".sweep_cancel.json"

#: The sidecar payload schema version (forensics-only — never parsed on the
#: hot probe; presence is the signal).
CANCEL_SIDECAR_VERSION: Final[int] = 1


def cancel_sidecar_path(root: Path) -> Path:
    """The durable cancel-request sidecar path for the results *root*. O(1)."""
    return root / CANCEL_SIDECAR_NAME


def request_cancel(root: Path, *, source: str) -> Path:
    """DURABLY request a cooperative cancel of the sweep running under *root*.

    Writes the sidecar atomically (tmp → fsync → ``os.replace`` via the COMMON
    :func:`~brutex.data.atomic_io.atomic_write_simple`) so a crash can never
    leave a torn file, creating *root* if needed (a cancel may race the sweep's
    own directory creation). The payload records *source* + the request instant
    for operator forensics; the RUNNING sweep only ever probes PRESENCE.

    Parameters
    ----------
    root
        The sweep results root (the directory the running sweep probes).
    source
        A short human label of who asked (e.g. ``"route"`` / ``"cli"``).

    Returns
    -------
    Path
        The sidecar path that now exists.
    """
    path = cancel_sidecar_path(root)
    root.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "version": CANCEL_SIDECAR_VERSION,
        "requested_at_utc": datetime.now(UTC).isoformat(),
        "source": source,
    }

    def _write(tmp: Path) -> None:
        tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

    atomic_write_simple(path, _write)
    _LOG.warning("sweep_cancel.requested", root=str(root), source=source)
    return path


def cancel_requested(root: Path) -> bool:
    """``True`` iff a cancel-request sidecar is present under *root* — ONE ``os.stat``.

    The O(1)-per-tuple hot probe (bench-pinned): presence is the signal, the
    body is NEVER read here — a malformed sidecar still cancels (fail-safe
    toward a graceful stop; see the module docstring). Any probe ``OSError``
    other than absence (a permission defect, a hostile inode) reads ``True``
    with one WARNING — biasing toward the harmless graceful stop rather than
    silently ignoring an operator's cancel.
    """
    path = cancel_sidecar_path(root)
    try:
        os.stat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        _LOG.warning(
            "sweep_cancel.probe_error",
            path=str(path),
            error=f"{type(exc).__name__}: {exc}",
            note="probe failed non-ENOENT — treating as a cancel request (fail-safe)",
        )
        return True
    return True


def clear_cancel(root: Path) -> bool:
    """Remove the cancel-request sidecar; return whether one existed.

    Called at sweep LAUNCH (the stale-file guard — a leftover request must never
    kill a fresh/resumed run) and after a cancelled run consumed its request.
    A clear that fails on a non-ENOENT ``OSError`` is LOUD but non-fatal (the
    launch continues; the probe's fail-safe bias then cancels the run — an
    honest, visible degradation, never a silent one).
    """
    path = cancel_sidecar_path(root)
    try:
        os.unlink(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        _LOG.warning(
            "sweep_cancel.clear_failed",
            path=str(path),
            error=f"{type(exc).__name__}: {exc}",
        )
        return False
    return True


def make_cancel_probe(root: Path) -> Callable[[], bool]:
    """Return the zero-arg ``should_stop`` probe over *root* — the engine seam.

    The closure handed to
    :func:`~brutex.engine.sweep_supervisor.run_supervised_grid` /
    :func:`~brutex.engine.sweep_parallel.run_parallel_grid` as ``should_stop``;
    each call is ONE ``os.stat`` (:func:`cancel_requested`).
    """
    return lambda: cancel_requested(root)


__all__ = [
    "CANCEL_SIDECAR_NAME",
    "CANCEL_SIDECAR_VERSION",
    "cancel_requested",
    "cancel_sidecar_path",
    "clear_cancel",
    "make_cancel_probe",
    "request_cancel",
]
