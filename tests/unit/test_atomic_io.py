"""Unit tests for the crash-safe atomic lake writer + disk-full/OOM guard.

DEC-LAKE-WRITE-HALT-001 — :mod:`brutex.data.atomic_io` centralises the
tmp→fsync→``os.replace``→cleanup dance shared by the spot + option Parquet writers
and maps a full disk (``ENOSPC``) / out-of-memory (``MemoryError``) to a
:class:`~brutex.data.errors.LakeWriteHalt` pull-wide stop (a member of both ingest
spines' ``_GLOBAL_STOP_ERRORS``). Every OTHER error propagates unchanged.
"""

from __future__ import annotations

import errno
from pathlib import Path

import pytest
from brutex.data.atomic_io import atomic_write_via, lake_write_guard
from brutex.data.errors import DataLakeError, LakeWriteHalt
from brutex.models import BruteXError


# ── lake_write_guard ────────────────────────────────────────────────────────
def test_guard_passes_through_on_success() -> None:
    """A block that completes cleanly yields nothing and raises nothing."""
    ran = False
    with lake_write_guard():
        ran = True
    assert ran is True


def test_guard_maps_memory_error_to_out_of_memory() -> None:
    with pytest.raises(LakeWriteHalt) as exc_info, lake_write_guard():
        raise MemoryError("heap exhausted")
    assert exc_info.value.reason == "out_of_memory"
    # LakeWriteHalt is a pull-wide DataLakeError / BruteXError (global-stop member).
    assert isinstance(exc_info.value, DataLakeError)
    assert isinstance(exc_info.value, BruteXError)
    assert "out_of_memory" in str(exc_info.value)


def test_guard_maps_enospc_to_disk_full() -> None:
    with pytest.raises(LakeWriteHalt) as exc_info, lake_write_guard():
        raise OSError(errno.ENOSPC, "No space left on device")
    assert exc_info.value.reason == "disk_full"


def test_guard_reraises_other_oserror_unchanged() -> None:
    """A non-ENOSPC OSError (e.g. EACCES) is NOT escalated to a halt — propagates as-is."""
    with pytest.raises(OSError) as exc_info, lake_write_guard():
        raise OSError(errno.EACCES, "Permission denied")
    assert not isinstance(exc_info.value, LakeWriteHalt)
    assert exc_info.value.errno == errno.EACCES


# ── atomic_write_via ─────────────────────────────────────────────────────────
def test_atomic_write_via_writes_renames_and_leaves_no_tmp(tmp_path: Path) -> None:
    dest = tmp_path / "part.parquet"
    atomic_write_via(dest, lambda tmp: tmp.write_bytes(b"hello-lake"))
    assert dest.read_bytes() == b"hello-lake"
    # The temp file was consumed by os.replace — no .brutex-write-*.tmp left behind.
    assert list(tmp_path.glob(".brutex-write-*")) == []


def test_atomic_write_via_cleans_tmp_when_write_raises(tmp_path: Path) -> None:
    """A non-mapped error in *write_to* propagates AND the half-written tmp is removed."""
    dest = tmp_path / "part.parquet"

    def _boom(_tmp: Path) -> None:
        raise ValueError("render bug")

    with pytest.raises(ValueError, match="render bug"):
        atomic_write_via(dest, _boom)
    assert not dest.exists()
    assert list(tmp_path.glob(".brutex-write-*")) == []  # tmp cleaned on the error path


def test_atomic_write_via_maps_enospc_and_cleans_tmp(tmp_path: Path) -> None:
    dest = tmp_path / "part.parquet"

    def _full_disk(_tmp: Path) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    with pytest.raises(LakeWriteHalt) as exc_info:
        atomic_write_via(dest, _full_disk)
    assert exc_info.value.reason == "disk_full"
    assert not dest.exists()
    assert list(tmp_path.glob(".brutex-write-*")) == []


def test_atomic_write_via_maps_memory_error_and_cleans_tmp(tmp_path: Path) -> None:
    dest = tmp_path / "part.parquet"

    def _oom(_tmp: Path) -> None:
        raise MemoryError("render OOM")

    with pytest.raises(LakeWriteHalt) as exc_info:
        atomic_write_via(dest, _oom)
    assert exc_info.value.reason == "out_of_memory"
    assert not dest.exists()
    assert list(tmp_path.glob(".brutex-write-*")) == []
