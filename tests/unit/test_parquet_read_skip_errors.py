"""Unit coverage for the COMMON unreadable-Parquet skip set (corrupt-partition resilience).

The data-layer :func:`brutex.data.errors.parquet_read_skip_errors` is the ONE source of
truth for "this single Parquet file is corrupt / unreadable — skip / quarantine it,
don't crash the whole pass" (polars ``PolarsError`` + ``OSError``). The dashboard
``/movers`` + ``/db`` twin
:func:`brutex.dashboard.movers_resilient.movers_read_skip_errors` DELEGATES to it and
WIDENS it with ``duckdb.Error`` (CLAUDE.md §11 COMMON — no copy-pasted set).
"""

from __future__ import annotations

import pytest

pytest.importorskip("polars")
pytest.importorskip("duckdb")


def test_data_layer_set_is_polars_and_oserror() -> None:
    import polars as pl
    from brutex.data.errors import parquet_read_skip_errors

    skip = parquet_read_skip_errors()
    assert pl.exceptions.PolarsError in skip
    assert OSError in skip
    # NARROW: a generic code bug is NOT swallowed (Golden Rule #1).
    assert ValueError not in skip
    assert KeyError not in skip


def test_datetime_out_of_range_full_probe_set() -> None:
    """Round-18 (DEC-LAKE-HEAL-OPTIONS-FOREIGN-001): the FULL probe set covers
    every empirically-verified conversion-fault type — OverflowError
    (tz-aware positive overflow via min/max), ValueError (naive/negative
    overflow via min/max), and polars' PanicException (to_list — a
    BaseException that escapes ``except Exception``)."""
    import polars as pl
    from brutex.data.errors import datetime_out_of_range_errors, parquet_read_skip_errors

    probe = datetime_out_of_range_errors()
    assert OverflowError in probe
    assert ValueError in probe
    assert pl.exceptions.PanicException in probe
    # The panic rationale: a BaseException, NOT an Exception — the reason the
    # blanket guards must name it explicitly.
    assert not issubclass(pl.exceptions.PanicException, Exception)
    assert issubclass(pl.exceptions.PanicException, BaseException)
    # NOT folded into the file-unreadable set (different consumers/semantics).
    assert OverflowError not in parquet_read_skip_errors()


def test_datetime_out_of_range_escape_set_excludes_value_error() -> None:
    """The BROAD-guard subset (healer per-finding set + L3 boot blanket)
    deliberately EXCLUDES ValueError — around arbitrary heal/sweep code that
    is the genuine-code-bug class and must keep propagating (Golden Rule #1)."""
    import polars as pl
    from brutex.data.errors import (
        datetime_out_of_range_errors,
        datetime_out_of_range_escape_errors,
    )

    escape = datetime_out_of_range_escape_errors()
    assert OverflowError in escape
    assert pl.exceptions.PanicException in escape
    assert ValueError not in escape
    # A strict subset of the full probe set (no member invented twice).
    assert set(escape) < set(datetime_out_of_range_errors())


def test_movers_set_delegates_and_widens_with_duckdb() -> None:
    import duckdb
    from brutex.dashboard.movers_resilient import movers_read_skip_errors
    from brutex.data.errors import parquet_read_skip_errors

    movers = movers_read_skip_errors()
    # Every data-layer skip type is present (delegation, not a copy)…
    for exc_type in parquet_read_skip_errors():
        assert exc_type in movers
    # …plus the DuckDB engine's error (the /db + /movers duckdb read path).
    assert duckdb.Error in movers
