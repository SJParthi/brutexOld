"""Unit coverage for the COMMON unreadable-Parquet skip set (corrupt-partition resilience).

The data-layer :func:`brutex.data.errors.parquet_read_skip_errors` is the ONE source of
truth for "this single Parquet file is corrupt / unreadable — skip / quarantine it,
don't crash the whole pass" (polars ``PolarsError`` + ``OSError``). The dashboard
``/movers`` + ``/db`` twin
:func:`brutex.dashboard.movers_resilient.movers_read_skip_errors` DELEGATES to it and
WIDENS it with ``duckdb.Error`` (CLAUDE.md §11 COMMON — no copy-pasted set).

The round-18 datetime-out-of-range set pins
(:func:`~brutex.data.errors.datetime_out_of_range_errors` /
:func:`~brutex.data.errors.datetime_out_of_range_escape_errors`) live in
``tests/unit/test_data_errors_pickle.py`` — the module's stem-matched
discovery-gate suite (ONE source of truth, moved there not copied).
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
