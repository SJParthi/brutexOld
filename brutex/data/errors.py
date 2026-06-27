"""Typed exceptions for the BruteX data lake."""

from __future__ import annotations

from brutex.models import BruteXError


class DataLakeError(BruteXError):
    """Base class for every error raised by :mod:`brutex.data`."""


class PartitionError(DataLakeError):
    """Raised when a partition path cannot be computed or parsed.

    Examples:
    * An interval name not in the canonical intervals table
    * A symbol with an illegal character for the filesystem
    * A malformed partition path passed to :func:`parse_partition_path`
    """


class WriterError(DataLakeError):
    """Raised when a Parquet write fails or would violate idempotence.

    Examples:
    * Attempting to write an empty bar batch
    * Destination file exists with a different content hash
    * Underlying ``polars`` import fails at runtime
    """


class LakeWriteHalt(DataLakeError):
    """A lake write hit an UNRECOVERABLE host condition — disk full / out of memory.

    Unlike a per-symbol :class:`WriterError`, this is a PULL-WIDE stop: a full disk
    or an exhausted heap fails EVERY remaining window identically, so it must
    propagate past the per-symbol / per-contract quarantine (it is a member of both
    ingest spines' ``_GLOBAL_STOP_ERRORS``) and halt the pull GRACEFULLY — never a
    silent crash, never a half-written partition, never fabricated data (Golden Rule
    #1). The half-written temp file is always cleaned up before this is raised.

    ``reason`` is a non-secret machine token: ``"disk_full"`` (an ``OSError`` with
    errno ``ENOSPC``) or ``"out_of_memory"`` (a ``MemoryError``).
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"lake write halted: {reason}")
        self.reason = reason


class CatalogError(DataLakeError):
    """Raised when a DuckDB catalog operation fails or is malformed.

    Examples:
    * Duplicate ``(symbol, interval, yyyy, mm)`` row with divergent hash
    * ``duckdb`` import fails at runtime
    * Integrity check across catalog + filesystem reports a drift
    """


def parquet_read_skip_errors() -> tuple[type[BaseException], ...]:
    """Exception types that mean a single Parquet file is unreadable → skip / quarantine it.

    The ONE data-layer source of truth (CLAUDE.md §11 COMMON) for "this one Parquet
    file is corrupt / version-incompatible / fault — do not crash the whole pass over
    it". ``polars`` raises a :class:`polars.exceptions.PolarsError` (e.g. a
    ``ComputeError`` *"could not read parquet"* on a truncated / garbage file), and
    ``OSError`` covers file-level faults (a vanished / unreadable file). NARROW on
    purpose: a genuine code bug (``ValueError`` / ``KeyError`` / …) still propagates,
    so a logic error is never silently swallowed (Golden Rule #1).

    The dashboard ``/movers`` + ``/db`` read paths *widen* this set with
    :class:`duckdb.Error` (they read via DuckDB too) in
    :func:`brutex.dashboard.movers_resilient.movers_read_skip_errors`, which DELEGATES
    here — so the polars + OSError half lives in exactly one place. ``polars`` is
    imported inside the call (the data layer already lazy-imports it in the writers),
    so this stays importable without paying the polars import at module load.
    """
    import polars as pl

    return (pl.exceptions.PolarsError, OSError)
