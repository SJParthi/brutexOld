"""Unit tests for ``brutex.data.fs_probe`` (DEC-LAKE-FS-PROBE-001).

The lake-root allocation-granularity probe: a 1-byte probe file's ``st_blocks``
measures the EFFECTIVE allocation unit; ≥ 64 KiB is the COARSE exFAT-class
verdict (one loud ``lake.fs_allocation_coarse`` warning + a one-line note for
the pipeline report); a fine unit is one ``lake.fs_allocation_ok`` INFO; any
I/O error degrades to a logged no-op — the probe NEVER raises and always
removes its probe file. Coarse results are faked via the injected ``stat_fn``
(no real exFAT mount in CI). 100% line+branch on the module.
"""

from __future__ import annotations

import os
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any

from brutex.data.fs_probe import (
    COARSE_ALLOCATION_BYTES,
    DEFAULT_PROJECTED_FILES,
    AllocationProbe,
    probe_allocation_granularity,
)
from structlog.testing import capture_logs


def _events(logs: list[MutableMapping[str, Any]]) -> list[str]:
    return [str(e["event"]) for e in logs if str(e["event"]).startswith("lake.fs_")]


class _FakeStat:
    """A stat_result double with controllable block accounting."""

    def __init__(self, *, st_blocks: int, st_blksize: int = 4096) -> None:
        self.st_blocks = st_blocks
        self.st_blksize = st_blksize


def test_fine_grained_filesystem_is_ok_with_no_note(tmp_path: Path) -> None:
    """A real tmp filesystem (4 KiB-class blocks) reads fine — INFO, note None."""
    with capture_logs() as logs:
        probe = probe_allocation_granularity(tmp_path / "lake")
    assert probe.coarse is False
    assert probe.note is None
    assert probe.allocation_unit_bytes is not None
    assert 0 < probe.allocation_unit_bytes < COARSE_ALLOCATION_BYTES
    assert _events(logs) == ["lake.fs_allocation_ok"]


def test_probe_file_is_always_cleaned_up(tmp_path: Path) -> None:
    """The 1-byte probe file never survives the call (finally cleanup)."""
    root = tmp_path / "lake"
    probe_allocation_granularity(root)
    assert list(root.iterdir()) == [], "no probe litter may remain in the lake"


def test_coarse_exfat_class_cluster_warns_with_projected_slack(tmp_path: Path) -> None:
    """A 128 KiB effective unit (exFAT-class) is the LOUD coarse verdict.

    ``st_blocks = 256`` × 512 B = 131072 B ≥ the 64 KiB threshold — the warning
    + note name the measured unit and the projected slack for the prebuild's
    ~1M tiny files (128 KiB × 1M ≈ 122 GiB).
    """
    fake = _FakeStat(st_blocks=256)
    with capture_logs() as logs:
        probe = probe_allocation_granularity(tmp_path / "lake", stat_fn=lambda _p: fake)  # type: ignore[arg-type,return-value]
    assert probe.coarse is True
    assert probe.allocation_unit_bytes == 131072
    assert probe.note is not None
    assert "128 KiB" in probe.note
    assert "122 GiB" in probe.note
    assert f"{DEFAULT_PROJECTED_FILES:,}" in probe.note
    (event,) = [e for e in logs if e["event"] == "lake.fs_allocation_coarse"]
    assert event["allocation_unit_bytes"] == 131072
    assert event["projected_slack_gib"] == 122.1


def test_coarse_threshold_boundary_is_inclusive(tmp_path: Path) -> None:
    """Exactly 64 KiB (128 × 512 B blocks) IS coarse (>= is the law)."""
    fake = _FakeStat(st_blocks=COARSE_ALLOCATION_BYTES // 512)
    probe = probe_allocation_granularity(tmp_path / "lake", stat_fn=lambda _p: fake)  # type: ignore[arg-type,return-value]
    assert probe.coarse is True
    assert probe.allocation_unit_bytes == COARSE_ALLOCATION_BYTES


def test_projected_files_scales_the_slack_projection(tmp_path: Path) -> None:
    """The slack projection is unit × projected_files (an upper bound, stated)."""
    fake = _FakeStat(st_blocks=256)  # 128 KiB
    probe = probe_allocation_granularity(
        tmp_path / "lake",
        projected_files=500_000,
        stat_fn=lambda _p: fake,  # type: ignore[arg-type,return-value]
    )
    assert probe.note is not None
    assert "61 GiB" in probe.note  # 131072 × 500k / 2**30 ≈ 61.0
    assert "500,000" in probe.note


def test_zero_blocks_falls_back_to_blksize(tmp_path: Path) -> None:
    """A filesystem reporting no block usage falls back to ``st_blksize``."""
    fake = _FakeStat(st_blocks=0, st_blksize=1024 * 1024)
    probe = probe_allocation_granularity(tmp_path / "lake", stat_fn=lambda _p: fake)  # type: ignore[arg-type,return-value]
    assert probe.allocation_unit_bytes == 1024 * 1024
    assert probe.coarse is True


def test_io_error_degrades_to_logged_noop_never_raises(tmp_path: Path) -> None:
    """An unprobeable root (a FILE where the dir should be) is the graceful path."""
    root = tmp_path / "lake"
    root.write_text("not a directory")
    with capture_logs() as logs:
        probe = probe_allocation_granularity(root)
    assert probe == AllocationProbe(allocation_unit_bytes=None, coarse=False, note=None)
    (event,) = [e for e in logs if e["event"] == "lake.fs_probe_unavailable"]
    assert "error_type" in event


def test_stat_error_still_cleans_up_and_degrades(tmp_path: Path) -> None:
    """A raising stat_fn takes the unavailable path AND removes the probe file."""

    def _boom(_p: Path) -> os.stat_result:
        raise OSError("stat failed")

    root = tmp_path / "lake"
    with capture_logs() as logs:
        probe = probe_allocation_granularity(root, stat_fn=_boom)
    assert probe.allocation_unit_bytes is None
    assert list(root.iterdir()) == []
    assert "lake.fs_probe_unavailable" in _events(logs)
