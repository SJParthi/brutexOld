"""Property tests for the write-side uniqueness invariant (DEC-LAKE-WRITE-UNIQUE-001).

For ANY incoming batch with ARBITRARY duplicate patterns (identical dups,
differing dups, interleaved order, all-dup batches), the stored partition:

* holds UNIQUE timestamps, sorted ascending;
* keeps the FIRST occurrence's values per timestamp (keep-first);
* is byte-identical on an immediate re-run of the SAME batch (Rule #7).

Runs the REAL :class:`~brutex.data.parquet_writer.ParquetWriter` against a real
tmp lake (polars round-trip), so the invariant is proven at the physical file,
not on an in-memory model.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from hypothesis import given, settings
from hypothesis import strategies as st

pytest.importorskip("polars")
pytest.importorskip("pyarrow")

_COORDS = {
    "exchange": "NSE",
    "segment": "INDEX",
    "symbol": "NIFTY",
    "interval": "1minute",
}


def _bar(minute: int, price_paise: int) -> CandleBar:
    """A flat bar at minute-offset *minute* whose OHLC all equal ``price_paise/100``."""
    price = Decimal(price_paise) / Decimal(100)
    return CandleBar(
        timestamp=datetime(2024, 6, 3, 4, minute, tzinfo=UTC),
        open=price,
        high=price,
        low=price,
        close=price,
        volume=1,
        open_interest=None,
    )


#: A batch = a non-empty list of (minute, price_paise) pairs — minutes drawn from a
#: SMALL pool so duplicates are frequent; prices vary so differing-value duplicate
#: pairs (the conflict case) are generated alongside identical ones.
_batches = st.lists(
    st.tuples(st.integers(min_value=0, max_value=7), st.integers(min_value=1, max_value=5)),
    min_size=1,
    max_size=24,
)


@settings(max_examples=60, deadline=None)
@given(rows=_batches)
def test_any_dup_pattern_stores_unique_sorted_first_occurrence_wins(
    tmp_path_factory: pytest.TempPathFactory, rows: list[tuple[int, int]]
) -> None:
    """FAILS on unfixed main for any example with a duplicate minute: the fresh
    render stored the duplicate rows verbatim."""
    import polars as pl

    tmp_path = tmp_path_factory.mktemp("dedup-lake")
    writer = ParquetWriter(lake_root=Path(tmp_path))
    batch = [_bar(minute, price) for minute, price in rows]

    result = writer.write(bars=list(batch), **_COORDS)
    stored = pl.read_parquet(result.path).sort("timestamp")

    # Unique + sorted timestamps.
    timestamps = stored["timestamp"].to_list()
    assert timestamps == sorted(set(timestamps))
    assert len(timestamps) == len({minute for minute, _ in rows})

    # First-occurrence values win per timestamp (the batch order is the vendor
    # order; `sorted` is stable, so first-in-batch == first among equal keys).
    expected_close: dict[int, float] = {}
    for minute, price in rows:
        expected_close.setdefault(minute, price / 100)
    got = {ts.minute: close for ts, close in zip(timestamps, stored["close"].to_list())}
    assert got == expected_close

    # Conflicts recorded exactly for the differing-value duplicate pairs.
    differing = sum(
        1
        for i, (minute, price) in enumerate(rows)
        if any(m == minute for m, _ in rows[:i]) and price / 100 != expected_close[minute]
    )
    assert len(result.conflicts) == differing

    # Idempotent re-run: the same batch again is a byte-identical no-op (Rule #7).
    before = result.path.read_bytes()
    second = writer.write(bars=list(batch), **_COORDS)
    assert result.path.read_bytes() == before
    assert second.bars_added == 0
    assert second.raw_hash == result.raw_hash
