"""Typed exceptions for the BruteX data lake."""

from __future__ import annotations

from brutex.models import BruteXError


class DataLakeError(BruteXError):
    """Base class for every error raised by :mod:`brutex.data`."""


class PartitionError(DataLakeError):
    """Raised when a partition path cannot be computed or parsed.

    Examples:
    * An interval name not in the the broker intervals table
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


class CatalogError(DataLakeError):
    """Raised when a DuckDB catalog operation fails or is malformed.

    Examples:
    * Duplicate ``(symbol, interval, yyyy, mm)`` row with divergent hash
    * ``duckdb`` import fails at runtime
    * Integrity check across catalog + filesystem reports a drift
    """
