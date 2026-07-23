"""Unit tests for the crash-safe atomic lake writer + disk-full/OOM guard.

DEC-LAKE-WRITE-HALT-001 — :mod:`brutex.data.atomic_io` centralises the
tmp→fsync→``os.replace``→cleanup dance shared by the spot + option Parquet writers
and maps a full disk (``ENOSPC``) / out-of-memory (``MemoryError``) to a
:class:`~brutex.data.errors.LakeWriteHalt` pull-wide stop (a member of both ingest
spines' ``_GLOBAL_STOP_ERRORS``). Every OTHER error propagates unchanged.
"""

from __future__ import annotations

import errno
import tempfile
from pathlib import Path

import pytest
from brutex.data.atomic_io import atomic_write_simple, atomic_write_via, lake_write_guard
from brutex.data.errors import DataLakeError, LakeWriteHalt
from brutex.models import BruteXError


def _raise(exc: BaseException) -> None:
    """Raise *exc* via a CALL, not a literal ``raise`` statement.

    Keeps the assertions AFTER a ``with pytest.raises(...), lake_write_guard():``
    block reachable to the static analyzer: a literal ``raise`` as the last
    with-body statement makes CodeQL mark the following asserts "unreachable" (a
    false positive — ``pytest.raises`` suppresses the exception). A function call
    is not treated as unconditionally terminating, so the asserts stay reachable.
    """
    raise exc


# ── lake_write_guard ────────────────────────────────────────────────────────
def test_guard_passes_through_on_success() -> None:
    """A block that completes cleanly raises nothing."""
    ran = False
    with lake_write_guard():
        ran = True
    assert ran is True


def test_guard_maps_memory_error_to_out_of_memory() -> None:
    with pytest.raises(LakeWriteHalt) as exc_info, lake_write_guard():
        _raise(MemoryError("heap exhausted"))
    assert exc_info.value.reason == "out_of_memory"
    # LakeWriteHalt is a pull-wide DataLakeError / BruteXError (global-stop member).
    assert isinstance(exc_info.value, DataLakeError)
    assert isinstance(exc_info.value, BruteXError)
    assert "out_of_memory" in str(exc_info.value)


def test_guard_maps_enospc_to_disk_full() -> None:
    with pytest.raises(LakeWriteHalt) as exc_info, lake_write_guard():
        _raise(OSError(errno.ENOSPC, "No space left on device"))
    assert exc_info.value.reason == "disk_full"


def test_guard_maps_edquot_to_disk_full() -> None:
    """EDQUOT (filesystem disk-quota exhausted) is the SAME 'out of space' condition as
    ENOSPC, so it maps to ``LakeWriteHalt('disk_full')`` too — never a raw OSError that
    crashes the pull (DEC-PULL-HARDENING-FOLLOWUP-001, MIGRATION.md #996)."""
    edquot = getattr(errno, "EDQUOT", None)
    if edquot is None:  # pragma: no cover — EDQUOT exists on the Track-1 POSIX envelope
        pytest.skip("errno.EDQUOT not present on this platform")
    with pytest.raises(LakeWriteHalt) as exc_info, lake_write_guard():
        _raise(OSError(edquot, "Disk quota exceeded"))
    assert exc_info.value.reason == "disk_full"


def test_guard_reraises_other_oserror_unchanged() -> None:
    """A non-ENOSPC/EDQUOT OSError (e.g. EACCES / EROFS) is NOT escalated to a halt —
    it propagates as-is so an ordinary write error is never mis-escalated."""
    with pytest.raises(OSError) as exc_info, lake_write_guard():
        _raise(OSError(errno.EACCES, "Permission denied"))
    assert not isinstance(exc_info.value, LakeWriteHalt)
    assert exc_info.value.errno == errno.EACCES


def test_guard_reraises_erofs_unchanged() -> None:
    """EROFS (read-only filesystem) is a DIFFERENT class of fault than out-of-space and
    is deliberately NOT mapped — it propagates raw (Golden Rule #1, never over-broad)."""
    with pytest.raises(OSError) as exc_info, lake_write_guard():
        _raise(OSError(errno.EROFS, "Read-only file system"))
    assert not isinstance(exc_info.value, LakeWriteHalt)
    assert exc_info.value.errno == errno.EROFS


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


def test_atomic_write_via_maps_mkstemp_enospc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ENOSPC during ``mkstemp`` ITSELF maps to LakeWriteHalt too.

    On a full volume the inode / first-block allocation is frequently the FIRST
    call to fail, BEFORE any temp exists — so `mkstemp` runs inside the guard and
    this exercises the ``tmp is None`` cleanup branch (nothing to unlink) and proves
    no ``dest`` is created.
    """
    dest = tmp_path / "part.parquet"

    def _full_disk_mkstemp(*_a: object, **_k: object) -> tuple[int, str]:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(tempfile, "mkstemp", _full_disk_mkstemp)
    with pytest.raises(LakeWriteHalt) as exc_info:
        atomic_write_via(dest, lambda tmp: tmp.write_bytes(b"never reached"))
    assert exc_info.value.reason == "disk_full"
    assert not dest.exists()
    assert list(tmp_path.glob(".brutex-write-*")) == []


# ── atomic_write_simple (guard-free variant) ─────────────────────────────────
def test_atomic_write_simple_writes_renames_and_leaves_no_tmp(tmp_path: Path) -> None:
    dest = tmp_path / "out.json"
    atomic_write_simple(dest, lambda tmp: tmp.write_bytes(b"engine-bytes"))
    assert dest.read_bytes() == b"engine-bytes"
    assert list(tmp_path.glob(".brutex-write-*")) == []  # temp consumed by os.replace


def test_atomic_write_simple_propagates_raw_enospc_not_halt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guard-FREE: an ENOSPC propagates as a RAW OSError, NOT a LakeWriteHalt.

    This is the whole point of the simple variant — non-lake callers (engine
    sidecars / config / cache) keep their own domain-error contract and must NOT
    have a disk-full mis-escalated into the lake-specific pull-wide halt.
    """

    def _enospc(_src: str, _dst: str) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    # This is the PYTHON-ARM fault injection (the #1912-disclosed
    # os.replace-monkeypatch seam) — pin the arm via the explicit "0" kill
    # switch now that BRUTEX_LAKE_RUST_LOCKS defaults ON
    # (DEC-RUST-DEFAULT-ON-WAVE-001); the ON-arm twin lives in
    # tests/unit/test_lake_rust_locks.py.
    monkeypatch.setenv("BRUTEX_LAKE_RUST_LOCKS", "0")
    monkeypatch.setattr("brutex.data.atomic_io.os.replace", _enospc)
    with pytest.raises(OSError) as exc_info:
        atomic_write_simple(dest=tmp_path / "x", write_to=lambda tmp: tmp.write_bytes(b"d"))
    assert not isinstance(exc_info.value, LakeWriteHalt)  # NOT escalated
    assert exc_info.value.errno == errno.ENOSPC
    assert list(tmp_path.glob(".brutex-write-*")) == []  # temp still cleaned on the error path


def test_atomic_write_simple_cleans_tmp_and_propagates_generic_error(tmp_path: Path) -> None:
    """A non-OSError in *write_to* propagates as-is AND the half-written temp is removed."""
    dest = tmp_path / "x"

    with pytest.raises(ValueError, match="render bug"):
        atomic_write_simple(dest, lambda _tmp: _raise(ValueError("render bug")))
    assert not dest.exists()
    assert list(tmp_path.glob(".brutex-write-*")) == []


def test_atomic_write_simple_applies_chmod(tmp_path: Path) -> None:
    """`chmod` pins the final file's mode (the credential store's 0600 save path)."""
    import stat

    dest = tmp_path / "secret.json"
    atomic_write_simple(dest, lambda tmp: tmp.write_text("token"), chmod=0o600)
    assert dest.read_text() == "token"
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600  # owner-only, never world-readable
