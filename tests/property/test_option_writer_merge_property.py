# pyright: reportPrivateUsage=none
"""Property tests for the OPTIONS ``(side, timestamp)`` merge law + escape injectivity.

The generative twin of :mod:`tests.property.test_parquet_writer_dedup_property`
(the bars-only ``(timestamp)`` property, DEC-LAKE-WRITE-UNIQUE-001 / MIGRATION.md
#1139). The options merge law (``_merge_frame_locked``, production-reachable via
the AUTO-ON lake healer) has ~60 hand-written unit pins but no HYPOTHESIS breadth
across dup / cross-write-overlap / split-order permutations — this closes that gap
(DEC-DEDUP-IDENTITY-EVIDENCE-PACK-001).

For ANY sequence of 1..3 sequential :meth:`OptionParquetWriter.write` calls over a
batch with arbitrary duplicate / CE-PE / split patterns, the stored partition:

* holds UNIQUE ``(side, timestamp)`` keys, sorted;
* keeps the FIRST occurrence's values per key across the WHOLE write sequence
  (keep-first — the intra-write dedup and the cross-write existing-partition merge
  share ONE law);
* has a CE and a PE sharing one minute BOTH survive (distinct keys);
* returns ``added`` counts whose SUM equals the final distinct-key count;
* is a zero-``added`` BYTE-IDENTICAL no-op on a full re-write of the concatenation
  (Rule #7).

Plus the escape injectivity property for :func:`_safe_segment`
(DEC-DEDUP-IDENTITY-EVIDENCE-PACK-001): any two keys the CURRENT guard ACCEPTS
never collide onto one segment.

SCOPING (honest): the batch is drawn within ONE ``(year, month)`` partition (all
bars land in ``2021-01``), so this exercises the single-partition merge law
generatively; the cross-MONTH split remains covered by the ~60 existing unit pins.
The writer merge is O(month-rows), so there is deliberately NO O(1) bench here
(that would violate Golden Rule #10).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Final

import pytest
from brutex.data.models import OptionBar
from brutex.data.option_parquet_writer import (
    OptionParquetWriter,
    OptionSeriesCoordinate,
    OptionWriterError,
    _safe_segment,
)
from hypothesis import assume, given, settings
from hypothesis import strategies as st

pytest.importorskip("polars")
pytest.importorskip("pyarrow")

#: A partition key whose escape (``|`` → ``__``) is unambiguous — carries none of
#: the refused ``{"__", "_|", "|_"}`` substrings, so ``_safe_segment`` accepts it.
_PARTITION_KEY: Final[str] = "EVID|MERGE"
#: The corresponding on-disk segment directory (``EVID|MERGE`` → ``EVID__MERGE``).
_SEGMENT_DIR: Final[str] = "EVID__MERGE"

_COORD: Final[OptionSeriesCoordinate] = OptionSeriesCoordinate(
    exch_id="NSE",
    exchange_segment="NSE_FNO",
    security_id=13,
    underlying_symbol="NIFTY",
    expiry_flag="WEEK",
    expiry_code=0,
    strike_offset="ATM",
)

#: One drawn row: (side ∈ {CE, PE}, minute 0..7 — a small pool so dups are frequent,
#: price 1..5 — integer rupees so differing-value dup pairs arise with no rounding).
_Row = tuple[str, int, int]


def _obar(row: _Row) -> OptionBar:
    """Build a flat :class:`OptionBar` from a drawn ``(side, minute, price)`` row."""
    side, minute, price = row
    value = Decimal(price)
    return OptionBar(
        timestamp=datetime(2021, 1, 4, 4, minute, tzinfo=UTC),
        open=value,
        high=value,
        low=value,
        close=value,
        volume=1,
        open_interest=1200,
        side=side,
        strike=Decimal("17500"),
        spot=Decimal("17480.57"),
        iv=Decimal("0.15"),
    )


@st.composite
def _write_sequences(draw: st.DrawFn) -> list[list[_Row]]:
    """Draw a batch of rows split into 1..3 sequential non-empty write chunks.

    The concatenation of the chunks (in order) is the vendor arrival order, so the
    first occurrence of a ``(side, minute)`` key across the flattened sequence is the
    keep-first winner.
    """
    rows: list[_Row] = draw(
        st.lists(
            st.tuples(
                st.sampled_from(["CE", "PE"]),
                st.integers(min_value=0, max_value=7),
                st.integers(min_value=1, max_value=5),
            ),
            min_size=1,
            max_size=24,
        )
    )
    n = len(rows)
    cuts: list[int] = (
        sorted(draw(st.sets(st.integers(min_value=1, max_value=n - 1), max_size=2)))
        if n > 1
        else []
    )
    bounds = [0, *cuts, n]
    return [rows[a:b] for a, b in pairwise(bounds) if b > a]


def _distinct_keys(chunks: list[list[_Row]]) -> set[tuple[str, int]]:
    """The set of distinct ``(side, minute)`` keys across all chunks."""
    return {(side, minute) for chunk in chunks for side, minute, _ in chunk}


def _first_close(chunks: list[list[_Row]]) -> dict[tuple[str, int], int]:
    """The keep-first price per ``(side, minute)`` key across the flattened sequence."""
    first: dict[tuple[str, int], int] = {}
    for chunk in chunks:
        for side, minute, price in chunk:
            first.setdefault((side, minute), price)
    return first


def _stored(tmp_path: Path) -> list[tuple[str, int, float]]:
    """Read the single stored partition as sorted ``(side, minute, close)`` rows."""
    import polars as pl

    (parquet,) = (tmp_path / "options" / _SEGMENT_DIR).glob("*.parquet")
    frame = pl.read_parquet(parquet).sort(["side", "timestamp"])
    return [
        (str(side), int(ts.minute), float(close))
        for side, ts, close in zip(
            frame["side"].to_list(),
            frame["timestamp"].to_list(),
            frame["close"].to_list(),
            strict=True,
        )
    ]


@settings(max_examples=60, deadline=None)
@given(chunks=_write_sequences())
def test_any_dup_and_split_pattern_stores_unique_side_timestamp_first_wins(
    tmp_path_factory: pytest.TempPathFactory, chunks: list[list[_Row]]
) -> None:
    tmp_path = tmp_path_factory.mktemp("opt-merge")
    writer = OptionParquetWriter(lake_root=tmp_path)
    for chunk in chunks:
        writer.write(
            partition_key=_PARTITION_KEY, coordinate=_COORD, bars=[_obar(r) for r in chunk]
        )

    stored = _stored(tmp_path)
    keys = [(side, minute) for side, minute, _ in stored]

    # UNIQUE + sorted (side, timestamp) keys.
    assert keys == sorted(set(keys))
    # CE + PE sharing a minute are DISTINCT keys — both survive, none collapse.
    assert set(keys) == _distinct_keys(chunks)

    # Keep-first: the stored close per key is the FIRST occurrence's price.
    first = _first_close(chunks)
    got = {(side, minute): close for side, minute, close in stored}
    assert got == {key: float(price) for key, price in first.items()}


@settings(max_examples=60, deadline=None)
@given(chunks=_write_sequences())
def test_added_counts_sum_to_distinct_keys(
    tmp_path_factory: pytest.TempPathFactory, chunks: list[list[_Row]]
) -> None:
    tmp_path = tmp_path_factory.mktemp("opt-merge-added")
    writer = OptionParquetWriter(lake_root=tmp_path)
    total_added = 0
    for chunk in chunks:
        total_added += writer.write(
            partition_key=_PARTITION_KEY, coordinate=_COORD, bars=[_obar(r) for r in chunk]
        )
    assert total_added == len(_distinct_keys(chunks))


@settings(max_examples=60, deadline=None)
@given(chunks=_write_sequences())
def test_full_replay_is_a_zero_added_byte_identical_noop(
    tmp_path_factory: pytest.TempPathFactory, chunks: list[list[_Row]]
) -> None:
    tmp_path = tmp_path_factory.mktemp("opt-merge-replay")
    writer = OptionParquetWriter(lake_root=tmp_path)
    for chunk in chunks:
        writer.write(
            partition_key=_PARTITION_KEY, coordinate=_COORD, bars=[_obar(r) for r in chunk]
        )
    (parquet,) = (tmp_path / "options" / _SEGMENT_DIR).glob("*.parquet")
    before = parquet.read_bytes()

    # A full re-write of the flattened concatenation is a zero-added no-op.
    replay = [_obar(r) for chunk in chunks for r in chunk]
    added = writer.write(partition_key=_PARTITION_KEY, coordinate=_COORD, bars=replay)
    assert added == 0
    assert parquet.read_bytes() == before


def _safe_or_none(key: str) -> str | None:
    """Return the escaped segment iff the CURRENT guard ACCEPTS *key*, else ``None``."""
    try:
        return _safe_segment(key)
    except OptionWriterError:
        return None


@settings(max_examples=200, deadline=None)
@given(
    k1=st.text(alphabet="A|_", min_size=1, max_size=8),
    k2=st.text(alphabet="A|_", min_size=1, max_size=8),
)
def test_safe_segment_is_injective_on_accepted_keys(k1: str, k2: str) -> None:
    """Any two DISTINCT keys the guard accepts map to DISTINCT segments.

    On the pre-fix guard this FAILS: ``"A|A"`` and ``"A__A"`` are both accepted
    (only ``/ \\ ..`` refused) yet both escape to ``"A__A"``. The ``{__, _|, |_}``
    refusal makes the escape injective on the accepted domain.
    """
    assume(k1 != k2)
    s1 = _safe_or_none(k1)
    s2 = _safe_or_none(k2)
    assume(s1 is not None and s2 is not None)
    assert s1 != s2
