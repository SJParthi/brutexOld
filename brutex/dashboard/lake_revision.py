"""COMMON cheap lake content-/directory-revision tokens (DEC-CROSS-PROCESS-CACHE-FRESH-001).

The dashboard's read caches (the SPOT/OPTIONS coverage aggregate, the ``/db``
candle-pager total, the three ``/db`` discovery caches) are invalidated by an
**in-process** write generation: a dashboard pull bumps
:meth:`brutex.dashboard.summary_cache.SummaryCache.mark_dirty`, so the next eligible
read recomputes. That signal is BLIND to a **cross-process** writer — a CLI
``brutex fetch`` running alongside the dashboard, sharing ``BRUTEX_LAKE_ROOT``,
writes Parquet partitions WITHOUT touching the dashboard's own generation counter, so
a generation-only cache would serve those stale counts forever (the
:mod:`brutex.data.partition_lock` cross-process write race's read-side twin).

This module is the ONE source of truth (CLAUDE.md §11 COMMON) for the cheap
filesystem tokens that close that gap by folding a content fingerprint into each
cache's key / hit predicate — mirroring the ``window-counts|…|{mtime_ns}`` shape the
``/coverage/windows`` counts cache already uses for the ledger Parquet
(:mod:`brutex.dashboard.db_view`):

* :func:`lake_revision_token` — ``(count of directories under the roots, max
  directory ``st_mtime_ns``)``. A new partition leaf directory moves the COUNT; an
  atomic append into an existing partition (``os.replace`` bumps the containing
  directory's mtime) moves the MAX. O(dirs) — one ``stat`` per directory, NOT per
  Parquet file. Used to invalidate the lake-wide discovery caches (the candle
  datasets list + the spot/F&O cascade indices) and the per-segment coverage
  aggregate on a cross-process write.
* :func:`partition_parquet_mtime_token` — ``max(st_mtime_ns)`` over the
  ``*.parquet`` files of ONE partition base. O(files-in-partition) for a single
  ``(symbol, interval)`` partition (a handful of monthly files), used to invalidate
  the candle-pager TOTAL when a cross-process append rewrites a month file in place
  (the file twin of the directory token).

Honest envelope (CLAUDE.md §2 Rule 10)
--------------------------------------
* The tokens are a CHEAP fingerprint, NOT a content hash: two different lake states
  that happen to share the same ``(dir-count, max-mtime)`` would collide. In
  practice a write ALWAYS moves one or the other (proven by the regression tests), so
  this is a soundness-preserving accelerator, not a cryptographic guarantee.
* A token computation is O(dirs) (or O(files-in-one-partition)), paid once per
  refresh-eligibility check — NOT per cached read of an unchanged lake (the cache
  HIT still returns in O(1) once the token matches; the token compute is the cheap
  guard, not the slow scan it protects).
* mtime granularity is the filesystem's; on a filesystem with coarse mtime two
  writes within the same tick into the SAME directory could share an mtime — but the
  directory COUNT still moves for a new partition, and the in-process generation
  still moves for a dashboard pull, so the cross-process miss this closes is the
  append-into-existing-partition case the file/dir mtime catches.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

__all__ = ["lake_revision_token", "partition_parquet_mtime_token"]


def lake_revision_token(roots: Iterable[Path]) -> str:
    """Return a cheap ``(dir-count, max-dir-mtime)`` revision token over *roots*.

    Walks every directory under each existing root in *roots* and returns a stable
    string token ``"<dir-count>:<max-st_mtime_ns>"``. A cross-process write moves the
    token: a NEW partition leaf directory increments the count, and an atomic append
    into an EXISTING partition (``os.replace`` bumps the containing directory's mtime)
    raises the max. So folding this into a cache key forces a rediscover/recompute
    when — and only when — the lake's directory shape or recency changed, even if the
    writer was a separate process that never bumped the in-process generation.

    Parameters
    ----------
    roots
        The lake partition roots to fingerprint (e.g. ``<lake>/bars`` and/or
        ``<lake>/options``). A non-existent root contributes nothing (an empty lake
        keys at the stable ``"0:0"``), so the token is well-defined before the first
        pull.

    Returns
    -------
    str
        ``"<dir-count>:<max-st_mtime_ns>"`` — O(dirs) to compute, stable across
        repeat calls on an unchanged lake.

    Notes
    -----
    O(dirs): one ``stat`` per directory (NOT per Parquet file). A directory whose
    ``stat`` raises mid-walk (a concurrent ``os.replace`` swapping it) is skipped —
    the next eligible read recomputes, so a transient race never crashes the page.
    """
    mtimes: list[int] = []
    for root in roots:
        if not root.is_dir():
            continue
        for path in (root, *root.rglob("*")):
            if not path.is_dir():
                continue
            try:
                mtimes.append(path.stat().st_mtime_ns)
            except OSError:  # pragma: no cover — a dir vanishing mid-walk is a rare race
                continue
    return f"{len(mtimes)}:{max(mtimes, default=0)}"


def partition_parquet_mtime_token(base: Path) -> int:
    """Return ``max(st_mtime_ns)`` over the ``*.parquet`` files under one partition *base*.

    The file twin of :func:`lake_revision_token`, scoped to ONE
    ``<lake>/bars/<exch>/<seg>/<symbol>/<interval>`` partition base: a cross-process
    append rewrites a month file in place (``os.replace`` bumps THAT file's mtime), so
    folding this token into the candle-pager total cache key forces a recount when the
    partition's bars changed — even when the writer never bumped the dashboard's
    in-process generation.

    Parameters
    ----------
    base
        The partition base directory
        (``<lake>/bars/<exch>/<seg>/<symbol>/<interval>``). A non-existent / empty
        base yields ``0`` (so an empty partition keys stably; the candle reader
        already short-circuits an empty partition to a 0-total page).

    Returns
    -------
    int
        The newest ``st_mtime_ns`` among the partition's Parquet files, or ``0`` when
        the partition has none. O(files-in-this-partition) — a handful of monthly
        files for one ``(symbol, interval)``.

    Notes
    -----
    A file whose ``stat`` raises mid-walk (a concurrent ``os.replace``) is skipped;
    the next eligible read recomputes, so a transient race never crashes the page.
    """
    if not base.is_dir():
        return 0
    mtimes: list[int] = []
    for path in base.rglob("*.parquet"):
        try:
            mtimes.append(path.stat().st_mtime_ns)
        except OSError:  # pragma: no cover — a file vanishing mid-walk is a rare race
            continue
    return max(mtimes, default=0)
