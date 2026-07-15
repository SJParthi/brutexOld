"""The COMMON bounded, regular-file-only sidecar loader (moved from pull_intent).

DEC-SETTLE-EVIDENCE-ADOPTION-001 (MIGRATION.md #1793): :class:`SidecarReadResult`
and :func:`read_sidecar_bytes` are MOVED **VERBATIM** here from
``brutex/dashboard/pull_intent.py`` (which keeps byte-compatible re-exports —
the #1715 ``sweep_identity.py`` leaf+shim precedent), so the calendars-layer
settle-evidence loader can ride the SAME hardened reader without importing the
dashboard package (a layering inversion) and without a second drift-prone copy
(CLAUDE.md §11 COMMON + the jscpd 1% gate both refuse one).

Lineage: the loader is the DEC-LAKE-STATS-MANIFEST-001 (#1614) round-6
regular-file-only template, re-derived for the pull sidecars by
DEC-PULL-SCOPED-ALL-ONECLICK-001 (#1779) refuter round 7 P-1/P-2 — the
``O_RDONLY | O_NONBLOCK`` open + ``S_ISREG`` on the descriptor + the size cap
checked on the ``fstat`` of the SAME open descriptor. Stdlib-only (``os``,
``stat``, ``dataclasses``, ``pathlib``) — ZERO brutex imports, so any layer
may consume it cycle-free.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from stat import S_ISREG, filemode

__all__ = [
    "SidecarReadResult",
    "read_sidecar_bytes",
]


@dataclass(frozen=True, slots=True)
class SidecarReadResult:
    """One bounded, regular-file-only sidecar read's outcome (round-7 P-1).

    ``shape`` is exactly one of ``"ok"`` (``raw`` carries the bytes) ·
    ``"absent"`` · ``"not_regular"`` · ``"oversized"`` · ``"read_error"``;
    ``raw`` is ``None`` on every non-``ok`` shape. ``detail`` is the
    fully-formed human clause for the caller's ONE WARN (names the inode
    shape / the size / the OS error); ``error_type`` carries the exception
    class name on ``read_error`` only.
    """

    shape: str
    raw: bytes | None = None
    detail: str = ""
    error_type: str | None = None


def read_sidecar_bytes(path: Path, *, max_bytes: int) -> SidecarReadResult:
    """Read a small JSON sidecar's bytes — regular-file-only, fd-capped, never raises.

    The DEC-LAKE-STATS-MANIFEST-001 round-6 loader template (refuter round 7
    P-1, DEC-PULL-SCOPED-ALL-ONECLICK-001): ``os.open(path, O_RDONLY |
    O_NONBLOCK)`` — a plain ``open``/``read_bytes`` on a FIFO blocks for a
    writer FOREVER and on a ``/dev/zero`` symlink reads infinite zeros, so a
    non-regular inode planted at the sidecar path hung the dashboard BOOT
    (the scheduler's ``start_if_armed`` state load) and the pull click's
    held-mutex intent snapshot with no exception for any belt to catch —
    then ``S_ISREG`` on ``os.fstat(fd)`` (a FIFO/device/socket/directory
    folds into ``not_regular``; ``O_NONBLOCK`` makes the FIFO open itself
    return immediately), the size cap checked on the SAME ``fstat`` (the
    round-6 stat→read TOCTOU window — refuter round 7 P-2 — is CLOSED: the
    capped size and the read come from ONE opened inode, so an out-of-band
    swap can never feed a bigger file past the cap), and a bounded
    ``read(max_bytes)`` from the descriptor. fd ownership mirrors the #1614
    loader exactly: ``owned`` is True while THIS frame must close it;
    ``os.fdopen`` transfers ownership to the reader (closed by the ``with``),
    so no path double-closes or leaks the descriptor.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except FileNotFoundError:
        # Absent — including a dangling symlink and a file racing away.
        return SidecarReadResult(shape="absent")
    except OSError as exc:
        return SidecarReadResult(
            shape="read_error",
            detail=f"unreadable ({type(exc).__name__}: {exc})",
            error_type=type(exc).__name__,
        )
    owned = True
    try:
        st = os.fstat(fd)
        if not S_ISREG(st.st_mode):
            return SidecarReadResult(
                shape="not_regular",
                detail=f"not a regular file ({filemode(st.st_mode)})",
            )
        if st.st_size > max_bytes:
            return SidecarReadResult(
                shape="oversized",
                detail=f"oversized ({st.st_size} bytes > the {max_bytes} B cap)",
            )
        with os.fdopen(fd, "rb") as handle:
            # The file object now owns (and closes) the descriptor.
            owned = False
            raw = handle.read(max_bytes)
    except OSError as exc:
        # A failure on the OPEN descriptor (fstat / fdopen / read) — the
        # FileNotFoundError→absent mapping applies only to ``os.open`` itself.
        return SidecarReadResult(
            shape="read_error",
            detail=f"unreadable ({type(exc).__name__}: {exc})",
            error_type=type(exc).__name__,
        )
    finally:
        if owned:
            os.close(fd)
    return SidecarReadResult(shape="ok", raw=raw)
