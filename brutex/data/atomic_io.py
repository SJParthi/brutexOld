"""Crash-safe atomic lake writes + disk-full / OOM back-pressure (DEC-LAKE-WRITE-HALT-001).

The spot (:class:`~brutex.data.parquet_writer.ParquetWriter`) and option
(:mod:`brutex.data.option_parquet_writer`) lake writers each wrote their Parquet
partition through a near-identical "temp file in the same dir → ``fsync`` → atomic
``os.replace`` → clean up on error" dance. Two problems this module fixes, in ONE
source of truth (CLAUDE.md §11 COMMON):

1. **Disk-full / OOM was unhandled.** ``os.replace`` / ``write`` on a full volume
   raises ``OSError(ENOSPC)`` and a huge ``df.write_parquet`` can raise
   ``MemoryError`` — both propagated raw. A full disk is not one window's problem;
   it fails every remaining window, so it must HALT the pull gracefully, not be
   swallowed as a per-symbol quarantine (which would keep hammering a full disk).
2. **The atomic-write dance was duplicated.** Centralised here as
   :func:`atomic_write_via`.

:func:`lake_write_guard` is the single mapping primitive: any ``ENOSPC`` /
``MemoryError`` raised inside the block becomes a :class:`~brutex.data.errors.LakeWriteHalt`
(a member of both ingest spines' ``_GLOBAL_STOP_ERRORS`` → it propagates past the
per-symbol quarantine). It is used BOTH around the writers' render step (where a
``df.write_parquet`` may OOM) AND inside :func:`atomic_write_via` (where the temp
write / rename may hit ``ENOSPC``), so every lake-write failure site maps through
ONE place.

**Honest envelope (Golden Rule #10):** this is graceful DETECTION + halt, not a
memory CAP — a true RSS ceiling needs OS-level ``setrlimit`` / cgroups (explicitly
out of scope). Recovery is a clean re-pull (the durable window ledger + keep-first
idempotent sink), not an in-place resume.
"""

from __future__ import annotations

import contextlib
import errno
import os
import tempfile
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path

from brutex.data.errors import LakeWriteHalt

__all__ = ["atomic_write_via", "lake_write_guard"]


@contextmanager
def lake_write_guard() -> Generator[None, None, None]:
    """Map a disk-full / out-of-memory failure inside the block to a :class:`LakeWriteHalt`.

    ``OSError`` with errno ``ENOSPC`` → ``LakeWriteHalt("disk_full")``;
    ``MemoryError`` → ``LakeWriteHalt("out_of_memory")``. EVERY OTHER error
    (a different ``OSError`` such as ``EACCES``, a ``polars`` failure, a code bug)
    propagates UNCHANGED — only the two unrecoverable host conditions are mapped,
    so an ordinary write error is never mis-escalated to a pull-wide halt
    (Golden Rule #1, fail-closed but never over-broad).
    """
    try:
        yield
    except MemoryError:
        # `from None`: the original MemoryError carries no actionable context and
        # re-chaining it can itself allocate; the reason token is the signal.
        raise LakeWriteHalt("out_of_memory") from None
    except OSError as exc:
        if exc.errno == errno.ENOSPC:
            raise LakeWriteHalt("disk_full") from exc
        raise


def atomic_write_via(dest: Path, write_to: Callable[[Path], object]) -> None:
    """Atomically materialise *dest* by writing through a temp file in the same dir.

    *write_to* receives the temp :class:`~pathlib.Path` and must write the final
    bytes to it (e.g. ``tmp.write_bytes(content)`` for the spot writer, or
    ``df.write_parquet(tmp)`` for the option writer). The temp file's data is
    ``fsync``-ed to stable storage BEFORE the atomic ``os.replace`` (``os.replace``
    orders only the metadata, not the page cache), so a crash between the rename
    and the OS flush can never leave *dest* pointing at a truncated Parquet. The
    temp file is ALWAYS removed if the rename did not consume it (any error path).

    The write + rename run inside :func:`lake_write_guard`, so a full disk / OOM
    becomes a :class:`LakeWriteHalt` (pull-wide stop) with the half-written temp
    already cleaned up — never a half-written partition left on disk.
    """
    fd, tmp_name = tempfile.mkstemp(
        dir=str(dest.parent), prefix=".brutex-write-", suffix=".parquet.tmp"
    )
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        with lake_write_guard():
            write_to(tmp)
            flush_fd = os.open(tmp, os.O_RDONLY)
            try:
                os.fsync(flush_fd)
            finally:
                os.close(flush_fd)
            os.replace(tmp, dest)
    finally:
        # On success `os.replace` consumed tmp (it no longer exists → no-op); on any
        # error path remove the half-written temp. Suppress a concurrent/own unlink
        # race so cleanup never masks the original error.
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
