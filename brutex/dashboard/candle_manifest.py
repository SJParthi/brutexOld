"""O(page) candle-pager file resolution from the lake manifest (DEC-DB-LAKE-MANIFEST-001).

The `/db` candle pager pages a multi-year 1-minute Parquet partition with
``ORDER BY ts <dir> LIMIT k OFFSET m``. Today that runs over the WHOLE
``read_parquet(glob)`` — DuckDB opens EVERY monthly file's footer (O(files)) on
every paging click. This module turns that into O(page) for the dominant case (no
date filter) by reading the per-file ``row_count`` from the catalog manifest
(``brutex.data.catalog`` ``partitions`` table) and resolving WHICH monthly file(s)
the page's ``[offset, offset+limit)`` window lands in — so the page data query
scans only those 1-2 files, not all of them.

## The byte-identity invariant (why this is correct, not an illusion)

A monthly partition file ``<yyyy>/<mm>.parquet`` holds only that month's bars, and
months do not overlap in time. So ordering the partitions by ``(year, month)`` is
IDENTICAL to ordering their rows by timestamp. Walking the ``row_count`` prefix sum
in that order therefore locates the exact file(s) the page window covers, and
``read_parquet(<those files>) ORDER BY ts <dir> LIMIT k OFFSET <rebased>`` returns
the SAME rows, in the SAME order, as the full-glob query — provided the manifest's
file set matches the on-disk file set (the caller verifies this and falls back to
the exact full scan on any mismatch; see ``brutex.dashboard.db_view``).

## Scope (honest envelope)

This resolver handles the **no-date-filter** page (the pager's default — "1-50 /
N"): the per-file ``row_count`` is then the exact contribution of each file. When a
``start``/``end`` day filter is present it can CLIP a boundary month's contribution,
so ``row_count`` is no longer exact for that month — the caller does NOT use the
manifest path then (it falls back to the exact scan, byte-identical to today). A
future increment can add boundary-month exact counts; it is explicitly NOT claimed
here (Golden Rule #10).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

__all__ = ["PageResolution", "PartitionRow", "resolve_page_files"]


@dataclass(frozen=True, slots=True)
class PartitionRow:
    """One monthly partition file's manifest row — its coords + bar count.

    The ``(year, month)`` identify the file ``<yyyy>/<mm>.parquet``; ``row_count`` is
    the exact number of bars it holds (from the catalog). The caller builds these
    from ``Catalog.list_partitions`` for ONE ``(exchange, segment, symbol, interval)``.
    """

    year: int
    month: int
    row_count: int


@dataclass(frozen=True, slots=True)
class PageResolution:
    """The narrowed page plan: which files to read + the offset rebased into them.

    Attributes
    ----------
    files
        The ``(year, month)`` of every file the page window touches, in the SAME sort
        direction the page uses (so the caller globs exactly these). Empty when the
        page is entirely past the end of the data.
    rebased_offset
        The ``OFFSET`` to apply WITHIN the narrowed file set — the original offset
        minus the bars in the (skipped) files that sort before the first covered one.
    total
        The total bar count across all partitions (the page ``total``) — an exact
        ``SUM(row_count)`` with no footer scan.
    """

    files: tuple[tuple[int, int], ...]
    rebased_offset: int
    total: int


def resolve_page_files(
    partitions: Sequence[PartitionRow],
    *,
    descending: bool,
    limit: int,
    offset: int,
) -> PageResolution:
    """Resolve which monthly files a ``LIMIT/OFFSET`` page covers, via the manifest.

    Orders *partitions* by ``(year, month)`` in the page's sort direction (DESC when
    *descending*), walks the ``row_count`` prefix sum to find the contiguous run of
    files spanning ``[offset, offset+limit)``, and returns those files + the offset
    rebased into them + the exact total. Pure + deterministic (no I/O).

    Parameters
    ----------
    partitions
        The per-file manifest rows for ONE ``(coords)`` set (any order — sorted here).
    descending, limit, offset
        The page's sort direction + bounds (``limit``/``offset`` assumed already
        clamped non-negative by the caller).

    Returns
    -------
    PageResolution
        The narrowed file list, rebased offset, and exact total. ``files`` is empty
        when ``offset`` is at/after the total (an empty page) — the caller then reads
        nothing.
    """
    total = sum(p.row_count for p in partitions)
    ordered = sorted(partitions, key=lambda p: (p.year, p.month), reverse=descending)

    # Walk the prefix sum: find the first file whose rows reach past `offset` (the
    # page's first row) and keep files until cumulative covers `offset + limit`.
    page_end = offset + limit  # half-open upper bound of the requested window
    cumulative = 0
    skipped_before = 0  # rows in files entirely before the page's first row
    covered: list[tuple[int, int]] = []
    for p in ordered:
        file_start = cumulative  # rows before this file
        file_end = cumulative + p.row_count  # rows through this file
        cumulative = file_end
        # This file is entirely before the page window → skip (count its rows).
        if file_end <= offset:
            skipped_before = file_end
            continue
        # This file is entirely at/after the page window's end → stop.
        if file_start >= page_end:
            break
        covered.append((p.year, p.month))

    rebased_offset = offset - skipped_before
    return PageResolution(files=tuple(covered), rebased_offset=rebased_offset, total=total)
