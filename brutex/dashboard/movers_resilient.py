"""Resilient per-instrument reads for the ``/movers`` screens (DEC-MOVERS-CORRUPT-SKIP-001).

A single corrupt / version-incompatible Parquet file in ONE symbol's partition must
NOT 500 the whole ``/movers`` screen — and because the route caches/recomputes per
call, an unguarded read error re-500s on EVERY open until the file is manually
quarantined. The spot / futures / options candidate loops guard each per-instrument
read: an unreadable file SKIPS that symbol (logged + counted in the screen's
``skipped`` tally), mirroring
:func:`brutex.dashboard.db_view._aggregate_parquet_resilient` — never a fabricated
price, never a crashed screen (Golden Rule #1 / #10).
"""

from __future__ import annotations

import structlog

_LOG = structlog.get_logger("brutex.dashboard.movers")


def movers_read_skip_errors() -> tuple[type[BaseException], ...]:
    """Exception types that mean a single instrument's Parquet is unreadable → skip it.

    ``polars`` raises a :class:`polars.exceptions.PolarsError` (e.g. ``ComputeError`` on
    a corrupt / version-incompatible file — confirmed against polars 1.41), ``duckdb``
    raises a :class:`duckdb.Error` (e.g. ``InvalidInputException`` — *"No magic bytes
    found at end of file"* — on the SAME corrupt file read via ``read_parquet``,
    confirmed against duckdb 1.5), and ``OSError`` covers file-level faults. The ONE
    source of truth for the unreadable-Parquet skip set (CLAUDE.md §11 COMMON): the
    polars + ``OSError`` half is DELEGATED to the data-layer
    :func:`brutex.data.errors.parquet_read_skip_errors` (shared with the lake writers'
    corrupt-partition quarantine), and this dashboard twin WIDENS it with
    :class:`duckdb.Error` because the ``/movers`` candidate loops read via ``polars``
    while the ``/db`` candle pager
    (:func:`brutex.dashboard.db_view._execute_parquet_resilient`) reads via ``duckdb`` —
    so the union covers BOTH engines from one place, no copy-pasted set. NARROW on
    purpose: a genuine code bug (``ValueError`` / ``KeyError`` / …) still propagates, so
    a logic error is never silently swallowed as a "skipped symbol" (Golden Rule #1).
    ``duckdb`` is imported inside the call (not at module load) to keep this tiny helper
    module importable without paying the import cost — both consumers import the engines
    anyway.
    """
    import duckdb

    from brutex.data.errors import parquet_read_skip_errors

    return (*parquet_read_skip_errors(), duckdb.Error)


def log_movers_skip(*, source: str, symbol: str, error: str) -> None:
    """Log one skipped (unreadable) instrument — the movers twin of ``coverage.parquet_skip``."""
    _LOG.warning("movers.parquet_skip", source=source, symbol=symbol, error=error)
