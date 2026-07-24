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
   :func:`_atomic_write_core` (the ONE tmp→``fsync``→``os.replace``→cleanup), exposed
   as two variants: :func:`atomic_write_via` (lake-safe — ENOSPC/OOM → ``LakeWriteHalt``)
   for the data-lake writers, and :func:`atomic_write_simple` (guard-free — every error
   propagates as-is) for non-lake callers (engine sidecars, config, cache) whose own
   domain errors must survive (DEC-ATOMIC-WRITE-CONSOLIDATE-001).

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
from brutex.data.lake_rust_locks import rust_atomic_commit

__all__ = ["atomic_write_simple", "atomic_write_via", "lake_write_guard"]

#: The errnos that mean "no room to write" and so must HALT the pull gracefully as a
#: ``LakeWriteHalt("disk_full")``, NOT crash raw. ``ENOSPC`` is a literally-full volume;
#: ``EDQUOT`` is a filesystem DISK-QUOTA exhaustion (the SAME operator condition — "out
#: of space" — on a quota'd mount) which previously re-raised raw and, on the write
#: path, crashed the whole pull mislabelled as an escaped code bug
#: (DEC-PULL-HARDENING-FOLLOWUP-001, MIGRATION.md #996). ``EDQUOT`` is absent on a few
#: exotic platforms, so it is admitted defensively via ``getattr`` (never an
#: ``AttributeError`` at import). ONE source of truth — both lake writers + the merge-read
#: path route through :func:`lake_write_guard`, so widening here covers every write site.
_DISK_FULL_ERRNOS: frozenset[int] = frozenset(
    code for code in (errno.ENOSPC, getattr(errno, "EDQUOT", None)) if code is not None
)


@contextmanager
def lake_write_guard() -> Generator[None, None, None]:
    """Map a disk-full / out-of-memory failure inside the block to a :class:`LakeWriteHalt`.

    ``OSError`` with errno in :data:`_DISK_FULL_ERRNOS` (``ENOSPC`` — a literally-full
    volume — OR ``EDQUOT`` — a filesystem disk-quota exhaustion, the SAME "out of space"
    operator condition on a quota'd mount) → ``LakeWriteHalt("disk_full")``;
    ``MemoryError`` → ``LakeWriteHalt("out_of_memory")``. EVERY OTHER error
    (a different ``OSError`` such as ``EACCES`` / ``EROFS``, a ``polars`` failure, a code
    bug) propagates UNCHANGED — only the unrecoverable host conditions are mapped, so an
    ordinary write error is never mis-escalated to a pull-wide halt (Golden Rule #1,
    fail-closed but never over-broad).
    """
    try:
        yield
    except MemoryError:
        # `from None`: the original MemoryError carries no actionable context and
        # re-chaining it can itself allocate; the reason token is the signal.
        raise LakeWriteHalt("out_of_memory") from None
    except OSError as exc:
        if exc.errno in _DISK_FULL_ERRNOS:
            raise LakeWriteHalt("disk_full") from exc
        raise


def _atomic_write_core(
    dest: Path, write_to: Callable[[Path], object], *, chmod: int | None = None
) -> None:
    """The shared tmp→write→``fsync``→``os.replace``→cleanup dance (NO error mapping).

    *write_to* receives the temp :class:`~pathlib.Path` and must write the final
    bytes to it (e.g. ``tmp.write_bytes(content)``, ``df.write_parquet(tmp)``, or
    ``tmp.write_text(...)``). The temp's data is ``fsync``-ed to stable storage BEFORE
    the atomic ``os.replace`` (``os.replace`` orders only the metadata, not the page
    cache), so a crash between the rename and the OS flush can never leave *dest*
    pointing at a truncated file. The temp is ALWAYS removed if the rename did not
    consume it (every error path); ``dest`` is untouched until the atomic ``os.replace``,
    so a half-written *dest* is impossible.

    When *chmod* is given, the temp's mode is set to it BEFORE the rename, so *dest*
    lands at that mode and is NEVER briefly world-readable (the ``mkstemp`` temp is
    already created 0600, so the secret never leaks even before the explicit
    ``chmod`` — used by the credential store's 0600 save).

    EVERY error propagates UNCHANGED — this core does NOT map disk-full/OOM.
    :func:`atomic_write_via` adds the lake ``LakeWriteHalt`` policy on top; the
    guard-free :func:`atomic_write_simple` is for non-lake callers whose own domain
    errors must survive. This is the ONE atomic-write implementation (CLAUDE.md §11);
    callers stop hand-rolling ``mkstemp``/``fsync``/``os.replace``/cleanup.
    """
    tmp: Path | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            dir=str(dest.parent), prefix=".brutex-write-", suffix=".parquet.tmp"
        )
        os.close(fd)
        tmp = Path(tmp_name)
        write_to(tmp)
        # The chmod→fsync→replace COMMIT — routed through the Rust twin when
        # ``BRUTEX_LAKE_RUST_LOCKS`` is ON (DEC-LAKE-RUST-LOCKS-ATOMIC-001);
        # OFF (the default) runs :func:`_python_commit_replace`, the verbatim
        # pre-PR-4 inline sequence. Temp creation (above) and cleanup (below)
        # stay Python on BOTH arms.
        rust_atomic_commit(tmp, dest, chmod, python_commit=_python_commit_replace)
    finally:
        # `tmp is None` → `mkstemp` itself failed (nothing to clean). Otherwise: on
        # success `os.replace` consumed tmp (no-op), on any error path remove the
        # half-written temp. Suppress a concurrent/own unlink race so cleanup never
        # masks the original error.
        if tmp is not None:
            with contextlib.suppress(FileNotFoundError):
                tmp.unlink()


def _python_commit_replace(tmp: Path, dest: Path, chmod: int | None) -> None:
    """The chmod→``fsync``→``os.replace`` commit tail — the Python oracle arm.

    The verbatim pre-PR-4 inline sequence, extracted so
    :func:`_atomic_write_core` can route the commit through the Rust twin
    (DEC-LAKE-RUST-LOCKS-ATOMIC-001) while THIS stays the oracle both the
    default-ON arm and every degrade path serve: optional ``chmod`` FIRST
    (dest lands at that mode, never briefly world-readable), the temp's data
    ``fsync``-ed to stable storage, then the atomic ``os.replace``. EVERY
    error propagates unchanged (the core's no-mapping law).
    """
    if chmod is not None:
        os.chmod(tmp, chmod)
    flush_fd = os.open(tmp, os.O_RDONLY)
    try:
        os.fsync(flush_fd)
    finally:
        os.close(flush_fd)
    os.replace(tmp, dest)


def atomic_write_via(dest: Path, write_to: Callable[[Path], object]) -> None:
    """Lake-safe atomic write: disk-full / OOM → :class:`LakeWriteHalt` (pull-wide stop).

    The COMMON crash-safe core (:func:`_atomic_write_core`) wrapped in
    :func:`lake_write_guard`, so a full disk / OOM at ANY step — including ``mkstemp``
    itself (on a full volume the inode/first-block allocation is frequently the FIRST
    call to fail with ``ENOSPC``) — becomes a :class:`LakeWriteHalt`, with any
    half-written temp already cleaned up by the core's ``finally`` BEFORE the halt
    propagates. Used by the data-lake writers (spot + option Parquet) whose ENOSPC
    SHOULD halt the pull (it is a member of both ingest spines' ``_GLOBAL_STOP_ERRORS``).
    """
    with lake_write_guard():
        _atomic_write_core(dest, write_to)


def atomic_write_simple(
    dest: Path, write_to: Callable[[Path], object], *, chmod: int | None = None
) -> None:
    """Guard-free atomic write: the same crash-safe core, but EVERY error propagates as-is.

    For non-data-lake callers — the engine results/checkpoint sidecars, ``config``
    runtime/credentials, the NTM cache — that wrap or suppress their
    OWN errors (``ResultsStoreError`` / ``SweepCheckpointError`` / ``RuntimeFileError`` /
    a best-effort ``False`` / a raw ``OSError``) and must NOT have a disk-full
    mis-escalated into the lake-specific :class:`LakeWriteHalt`. (The window ledger,
    once a caller of this variant, was promoted to :func:`atomic_write_via` — it lives
    under the lake root and a disk-full there SHOULD halt the pull, DEC-WINDOW-LEDGER-WRITE-HALT-001.) Shares the ONE
    tmp→``fsync``→``os.replace``→cleanup implementation (CLAUDE.md §11) so these sites
    stop hand-rolling it; a raw ``OSError`` (e.g. ``ENOSPC``) propagates unchanged for
    the caller to map. *chmod* (e.g. ``0o600`` for the credential store) sets the final
    file's mode without it ever being briefly world-readable.
    """
    _atomic_write_core(dest, write_to, chmod=chmod)
