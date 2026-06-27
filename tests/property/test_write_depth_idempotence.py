"""Hypothesis property: the lake write path is idempotent across partition DEPTH.

DEC-ATSCALE-WRITEDEPTH-ORACLE-001 (ledger #939) — the reliability leg of the
Gate-2 at-scale write-depth oracle (CLAUDE.md Golden Rule #7: same inputs → same
outputs, bit-for-bit). The bench (``tests/bench/test_write_depth_oracle_bench.py``)
measures the merge-write *latency* across depth; these properties prove the
merge-write *bytes* are stable across depth, regardless of:

1. **Depth.** Seeding a month partition to any depth and re-merging the SAME bars
   yields a byte-identical file (``raw_hash`` unchanged, no new rows, no conflicts).
2. **Re-pull order.** Splitting the same bars into two arbitrary blocks and writing
   them in EITHER order converges to the SAME partition bytes — the merge unions by
   timestamp, so completion order never affects the stored data.

These use the same deterministic generator the bench uses
(:func:`brutex.data.write_depth_oracle.make_depth_bars`), so the property and the
bench exercise one source of truth (CLAUDE.md §11 COMMON).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

pytest.importorskip("polars")

from brutex.data.parquet_writer import ParquetWriter
from brutex.data.write_depth_oracle import make_depth_bars

_COORDS = {
    "exchange": "NSE",
    "segment": "INDEX",
    "symbol": "NIFTY",
    "interval": "1minute",
}


@settings(max_examples=20, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(depth=st.integers(min_value=1, max_value=600))
def test_remerge_is_byte_identical_across_depth(
    tmp_path_factory: pytest.TempPathFactory, depth: int
) -> None:
    """Re-merging the same bars into an N-deep partition is bit-for-bit stable."""
    lake = tmp_path_factory.mktemp("idem")
    writer = ParquetWriter(lake_root=lake)
    bars = make_depth_bars(depth)
    first = writer.write(bars=bars, **_COORDS)
    again = writer.write(bars=bars, **_COORDS)
    assert again.raw_hash == first.raw_hash
    assert again.row_count == depth
    assert again.conflicts == ()


@settings(max_examples=20, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(data=st.data())
def test_split_repull_order_independent(
    tmp_path_factory: pytest.TempPathFactory, data: st.DataObject
) -> None:
    """Two blocks of the same bars, written in either order → identical bytes."""
    depth = data.draw(st.integers(min_value=2, max_value=400))
    split = data.draw(st.integers(min_value=1, max_value=depth - 1))
    all_bars = make_depth_bars(depth)
    block_a = all_bars[:split]
    block_b = all_bars[split:]

    lake_ab = Path(tmp_path_factory.mktemp("ab"))
    w_ab = ParquetWriter(lake_root=lake_ab)
    w_ab.write(bars=block_a, **_COORDS)
    res_ab = w_ab.write(bars=block_b, **_COORDS)

    lake_ba = Path(tmp_path_factory.mktemp("ba"))
    w_ba = ParquetWriter(lake_root=lake_ba)
    w_ba.write(bars=block_b, **_COORDS)
    res_ba = w_ba.write(bars=block_a, **_COORDS)

    assert res_ab.raw_hash == res_ba.raw_hash
    assert res_ab.row_count == res_ba.row_count == depth
