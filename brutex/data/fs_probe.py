"""Filesystem allocation-granularity probe for the lake root — best-effort, never raises.

DEC-LAKE-FS-PROBE-001 (operator-decreed P0, mechanization-sweep register #16).
The pipeline prebuild (DEC-PIPELINE-AUTOPREBUILD-001) writes on the order of
0.5-1M tiny Parquet files into the lake. On a COARSE-allocation filesystem —
exFAT's default cluster on a large external SSD is 128 KiB-1 MiB — every tiny
file still consumes one full allocation unit, so the cluster SLACK alone wastes
tens to hundreds of GiB (1M files × 128 KiB ≈ 122 GiB of pure slack). Nothing
told the operator; the disk just filled.

:func:`probe_allocation_granularity` MEASURES the effective allocation unit
empirically: write a 1-byte probe file under *root*, ``os.stat`` it, and read
the space actually charged (``st_blocks × 512``, falling back to
``st_blksize`` when the filesystem reports no block usage). A unit at/above
:data:`COARSE_ALLOCATION_BYTES` (64 KiB) logs ONE LOUD
``lake.fs_allocation_coarse`` WARNING naming the measured unit + the projected
slack for :data:`DEFAULT_PROJECTED_FILES` small files, and returns a one-line
note the pipeline folds into the prebuild start log +
``PipelinePullReport.fs_allocation_note``; a fine-grained unit logs one
``lake.fs_allocation_ok`` INFO. Any I/O error degrades to a logged
``lake.fs_probe_unavailable`` no-op — the probe can NEVER block or fail the
pull, and the probe file is removed in a ``finally``.

Honest envelope (Golden Rule #10): the projection is ``unit × N`` — an UPPER
bound on the pure per-file slack for N sub-unit files, not a filesystem-exact
accounting (metadata/directory overhead is extra and not claimed); the probe
measures the filesystem AT the lake root only (a symlinked subtree on another
volume is not probed).
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

import structlog

if TYPE_CHECKING:
    from collections.abc import Callable

_LOG = structlog.get_logger(__name__)

__all__ = [
    "COARSE_ALLOCATION_BYTES",
    "DEFAULT_PROJECTED_FILES",
    "AllocationProbe",
    "probe_allocation_granularity",
]

#: The threshold above which an allocation unit is COARSE for a tiny-file lake:
#: ext4/APFS/tmpfs charge 4 KiB-blocks; exFAT clusters on big volumes run
#: 64 KiB-1 MiB — 64 KiB is the smallest unit where a million tiny Parquet
#: files already waste ≥ ~60 GiB of pure slack.
COARSE_ALLOCATION_BYTES: Final[int] = 64 * 1024

#: The file-count the slack projection is quoted for — the prebuild's measured
#: order of magnitude (~0.5-1M tiny derived-TF Parquet files on a
#: fully-materialised options lake, DEC-PIPELINE-AUTOPREBUILD-001).
DEFAULT_PROJECTED_FILES: Final[int] = 1_000_000

#: 512-byte units per ``st_blocks`` (POSIX — the stat block size is fixed at
#: 512 regardless of the filesystem's own block/cluster size).
_STAT_BLOCK_BYTES: Final[int] = 512


@dataclass(frozen=True, slots=True)
class AllocationProbe:
    """The measured allocation verdict for one lake root.

    Attributes
    ----------
    allocation_unit_bytes
        The EFFECTIVE bytes one 1-byte file consumes (``st_blocks × 512``,
        ``st_blksize`` fallback), or ``None`` when the probe was unavailable
        (I/O error — logged, never raised).
    coarse
        ``True`` iff the unit is at/above :data:`COARSE_ALLOCATION_BYTES`.
    note
        The one-line operator note for a COARSE verdict (folded into the
        prebuild start log + ``PipelinePullReport``); ``None`` for a fine
        or unavailable probe.
    """

    allocation_unit_bytes: int | None
    coarse: bool
    note: str | None


def probe_allocation_granularity(
    root: Path,
    *,
    projected_files: int = DEFAULT_PROJECTED_FILES,
    stat_fn: Callable[[Path], os.stat_result] | None = None,
) -> AllocationProbe:
    """Measure *root*'s effective allocation unit with a 1-byte probe file — never raises.

    Parameters
    ----------
    root
        The directory to probe (the lake root; created if absent — the
        prebuild is about to write there anyway).
    projected_files
        The tiny-file count the coarse-verdict slack projection is quoted for.
    stat_fn
        Injectable stat (defaults to :func:`os.stat`) — tests fake a coarse
        exFAT-class result without a real exFAT mount in CI.
    """
    probe_path = root / f".brutex-fsprobe-{os.getpid()}"
    try:
        try:
            root.mkdir(parents=True, exist_ok=True)
            probe_path.write_bytes(b"\x00")
            st = (stat_fn if stat_fn is not None else os.stat)(probe_path)
        finally:
            # Best-effort cleanup on EVERY path (incl. a failed stat) — the
            # probe must never leave litter in the lake. A cleanup OSError is
            # suppressed (unlink after a successful write is near-infallible).
            with contextlib.suppress(OSError):
                probe_path.unlink(missing_ok=True)
    except OSError as exc:
        _LOG.warning(
            "lake.fs_probe_unavailable",
            root=str(root),
            error_type=type(exc).__name__,
            error=str(exc),
            note="allocation-granularity probe failed — no slack verdict (pull continues)",
        )
        return AllocationProbe(allocation_unit_bytes=None, coarse=False, note=None)
    allocated = int(getattr(st, "st_blocks", 0)) * _STAT_BLOCK_BYTES
    unit = allocated if allocated > 0 else int(getattr(st, "st_blksize", 0))
    if unit >= COARSE_ALLOCATION_BYTES:
        slack_gib = unit * projected_files / 2**30
        note = (
            f"COARSE filesystem allocation at {root}: one tiny file consumes "
            f"{unit // 1024} KiB (exFAT-class cluster) — ~{slack_gib:.0f} GiB of pure "
            f"slack projected for {projected_files:,} small Parquet files; consider an "
            f"APFS/ext4-class volume for the lake"
        )
        _LOG.warning(
            "lake.fs_allocation_coarse",
            root=str(root),
            allocation_unit_bytes=unit,
            projected_files=projected_files,
            projected_slack_gib=round(slack_gib, 1),
            note=note,
        )
        return AllocationProbe(allocation_unit_bytes=unit, coarse=True, note=note)
    _LOG.info(
        "lake.fs_allocation_ok",
        root=str(root),
        allocation_unit_bytes=unit,
        note="fine-grained allocation — tiny-file slack negligible",
    )
    return AllocationProbe(allocation_unit_bytes=unit, coarse=False, note=None)
