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
    a corrupt / version-incompatible file — confirmed against polars 1.41) and
    ``OSError`` covers file-level faults. NARROW on purpose: a genuine code bug
    (``ValueError`` / ``KeyError`` / …) still propagates, so a logic error is never
    silently swallowed as a "skipped symbol" (Golden Rule #1). Imported lazily so
    importing this module never forces the optional ``polars`` dependency.
    """
    import polars as pl

    return (pl.exceptions.PolarsError, OSError)


def log_movers_skip(*, source: str, symbol: str, error: str) -> None:
    """Log one skipped (unreadable) instrument — the movers twin of ``coverage.parquet_skip``."""
    _LOG.warning("movers.parquet_skip", source=source, symbol=symbol, error=error)
