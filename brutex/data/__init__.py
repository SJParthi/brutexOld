"""BruteX data lake — canonical Parquet storage + DuckDB catalog.

Implements ``FR-CAT-001`` through ``FR-CAT-004`` from
``docs/REQUIREMENTS.md §4.2``.

## Layout

On-disk layout is **content-addressed and partition-hierarchical**:

::

    <lake_root>/
    └── bars/
        └── <exchange>/<segment>/<symbol>/<interval>/<yyyy>/<mm>.parquet

Example: ``lake/bars/NSE/FNO/NSE-NIFTY-02Jan25-28500-PE/1minute/2025/01.parquet``

Every Parquet file is **immutable** once written. Re-ingesting the same
``(symbol, interval, yyyy-mm)`` tuple with identical content produces a
byte-for-byte identical file — which is how we enforce
``NFR-DET-001`` reproducibility.

## Package layout

* :mod:`brutex.data.errors` — typed exceptions
* :mod:`brutex.data.models` — pure dataclasses for bars + catalog
  rows (no heavy deps)
* :mod:`brutex.data.partition` — pure path computation
  (no ``polars`` / ``duckdb`` / ``pyarrow`` imports)
* :mod:`brutex.data.parquet_writer` — lazy-imports ``polars`` when
  actually writing a file
* :mod:`brutex.data.catalog` — lazy-imports ``duckdb`` when opening
  the catalog

Pure modules stay test-friendly on any system; heavy modules are
optional-import so unit tests and the CLI work without the P2 deps
installed (the deps land in ``pyproject.toml`` under
``[project.optional-dependencies].data``).
"""

from __future__ import annotations

from brutex.data.errors import (
    CatalogError,
    DataLakeError,
    PartitionError,
    WriterError,
)
from brutex.data.models import CandleBar, CatalogEntry
from brutex.data.partition import (
    BAR_DIR_NAME,
    build_partition_path,
    parse_partition_path,
    partition_for_timestamp,
)
from brutex.data.sink import (
    BarSink,
    NullBarSink,
    ParquetBarSink,
    SinkAppendResult,
)

__all__ = [
    "BAR_DIR_NAME",
    "BarSink",
    "CandleBar",
    "CatalogEntry",
    "CatalogError",
    "DataLakeError",
    "NullBarSink",
    "ParquetBarSink",
    "PartitionError",
    "SinkAppendResult",
    "WriterError",
    "build_partition_path",
    "parse_partition_path",
    "partition_for_timestamp",
]
