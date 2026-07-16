# Typing surface for the brutex_lake PyO3 bridge (ADR-0020 PR-1).
# Mirrors rust/brutex-lake/src/lib.rs — keep in lockstep with the
# `#[pymodule]` registration block there.

__version__: str

def render_parquet_from_ipc(ipc_bytes: bytes) -> bytes:
    """Render uncompressed Arrow IPC bytes to parquet bytes.

    Byte-identical to the pinned Python wheel's
    ``DataFrame.write_parquet(use_pyarrow=False)`` under the BruteX
    bars-writer options (zstd level 3, row groups of 100 000, statistics
    off). Raises ``ValueError`` on an empty/undecodable payload or a
    polars render error; ``RuntimeError`` if the render panicked (caught
    at the FFI boundary — a panic never crosses raw).
    """

def pinned_polars_version() -> str:
    """The Python polars wheel version this crate byte-matches (e.g. ``"1.42.1"``)."""

def linked_zstd_version() -> str:
    """The libzstd version linked into this build (runtime-probed, e.g. ``"1.5.7"``)."""

def lock_try_exclusive(fd: int) -> bool:
    """One non-blocking exclusive ``flock`` attempt on a caller-owned raw fd.

    ADR-0020 PR-4 (DEC-LAKE-RUST-LOCKS-ATOMIC-001). ``True`` = acquired,
    ``False`` = a peer holds it (the ``BlockingIOError`` shape); any other
    failure raises a real ``OSError`` with the errno intact. The bounded
    retry loop / poll / progress-beat / ``PartitionLockTimeout`` law stay
    on the Python facade. ``RuntimeError`` if the attempt panicked (caught
    at the FFI boundary).
    """

def lock_release(fd: int) -> None:
    """Release the advisory lock on *fd* and close it (``LOCK_UN`` + ``close``).

    Ownership of the fd transfers to this call — on return (success or
    error) the fd must not be reused. A close failure raises a real
    ``OSError`` with the errno intact; ``RuntimeError`` on a panic.
    """

def atomic_commit_replace(tmp_path: str, dest: str, chmod: int | None = None) -> None:
    """The chmod→``fsync``→``rename`` commit of an already-written temp file.

    Order-exact twin of ``atomic_io._atomic_write_core``'s commit tail
    (``os.replace`` semantics — atomically replaces an existing *dest*).
    On ANY error the temp is left in place (the Python core's ``finally``
    owns cleanup) and a real ``OSError`` with the errno intact raises —
    ``ENOSPC``/``EDQUOT`` reach ``lake_write_guard`` unchanged.
    ``RuntimeError`` on a panic.
    """
