"""Unit tests for the O(page) candle-pager manifest resolver (DEC-DB-LAKE-MANIFEST-001).

Pure, deterministic, no I/O. The contract: ordering monthly partitions by
``(year, month)`` equals ordering their rows by timestamp (months don't time-overlap),
so the prefix-sum over ``row_count`` locates the exact file(s) a ``LIMIT/OFFSET`` page
covers — byte-identical to scanning the whole glob.
"""

from __future__ import annotations

from brutex.dashboard.candle_manifest import PartitionRow, resolve_page_files


def _rows(*counts: tuple[int, int, int]) -> list[PartitionRow]:
    return [PartitionRow(year=y, month=m, row_count=c) for (y, m, c) in counts]


def test_empty_partitions_is_empty_page() -> None:
    res = resolve_page_files([], descending=False, limit=50, offset=0)
    assert res.files == () and res.total == 0 and res.rebased_offset == 0


def test_single_file_full_page() -> None:
    res = resolve_page_files(_rows((2024, 1, 100)), descending=False, limit=50, offset=0)
    assert res.files == ((2024, 1),)
    assert res.rebased_offset == 0
    assert res.total == 100


def test_offset_inside_second_file_skips_first() -> None:
    # ASC: file1=[0,100) file2=[100,200) file3=[200,300); offset=150,limit=100
    # → covers file2 (the row-150 home) + file3 (page extends to row 249).
    res = resolve_page_files(
        _rows((2024, 1, 100), (2024, 2, 100), (2024, 3, 100)),
        descending=False,
        limit=100,
        offset=150,
    )
    assert res.files == ((2024, 2), (2024, 3))
    assert res.rebased_offset == 50  # 150 − 100 (file1 skipped)
    assert res.total == 300


def test_page_ends_mid_stream_breaks_early() -> None:
    # offset=0, limit=50 over two 100-row files → only file1; the break arm fires on file2.
    res = resolve_page_files(
        _rows((2024, 1, 100), (2024, 2, 100)), descending=False, limit=50, offset=0
    )
    assert res.files == ((2024, 1),)
    assert res.rebased_offset == 0


def test_descending_orders_files_newest_first() -> None:
    # DESC: file3 first. offset=150,limit=100 → covers file2 + file1 (in DESC order).
    res = resolve_page_files(
        _rows((2024, 1, 100), (2024, 2, 100), (2024, 3, 100)),
        descending=True,
        limit=100,
        offset=150,
    )
    assert res.files == ((2024, 2), (2024, 1))  # DESC walk: 3 skipped, then 2, then 1
    assert res.rebased_offset == 50
    assert res.total == 300


def test_offset_past_total_is_empty_page() -> None:
    res = resolve_page_files(
        _rows((2024, 1, 100), (2024, 2, 100)), descending=False, limit=50, offset=500
    )
    assert res.files == ()  # nothing covered — caller reads nothing
    assert res.total == 200


def test_limit_straddles_a_single_boundary() -> None:
    # offset=90, limit=20 spans file1's tail + file2's head → both files, rebased=90.
    res = resolve_page_files(
        _rows((2024, 1, 100), (2024, 2, 100)), descending=False, limit=20, offset=90
    )
    assert res.files == ((2024, 1), (2024, 2))
    assert res.rebased_offset == 90
