# pyright: reportPrivateUsage=none
"""``brutex.data.sidecar_read`` — the COMMON regular-file-only sidecar loader.

DEC-SETTLE-EVIDENCE-ADOPTION-001 (MIGRATION.md #1793): the loader MOVED
VERBATIM out of ``brutex/dashboard/pull_intent.py`` (which re-exports it
byte-compatibly — the #1715 leaf+shim precedent). This is the DIRECT shape
matrix for the moved defs — ``test_pull_intent.py`` keeps its STORE-level
corruption matrix (it exercises pull_intent's own branches through the
re-export); only the one direct grow-after-fstat test moved here.
100% line+branch standalone (the ci.yml module gate).
"""

from __future__ import annotations

import os
import signal
import socket
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import pytest
from brutex.data.sidecar_read import SidecarReadResult, read_sidecar_bytes


@contextmanager
def _hang_watchdog(seconds: int = 5) -> Generator[None]:
    """Turn an indefinite block inside the body into a LOUD failure.

    The fails-before shape of the round-7 P-1 defect is a read that blocks
    FOREVER (a FIFO read waits for a writer; ``/dev/zero`` yields infinite
    zeros), so a plain regression would HANG rather than fail; the
    ``SIGALRM`` handler interrupts the blocked syscall and fails the test
    instead (the #1614 round-6 watchdog idiom).
    """

    def _boom(_signum: int, _frame: object) -> None:
        raise AssertionError(f"sidecar read blocked > {seconds}s — the P-1 hang is back")

    previous = signal.signal(signal.SIGALRM, _boom)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def test_ok_reads_bytes(tmp_path: Path) -> None:
    target = tmp_path / "sidecar.json"
    target.write_bytes(b'{"ok": true}')
    result = read_sidecar_bytes(target, max_bytes=1024)
    assert result == SidecarReadResult(shape="ok", raw=b'{"ok": true}')
    assert result.error_type is None


def test_absent(tmp_path: Path) -> None:
    result = read_sidecar_bytes(tmp_path / "nope.json", max_bytes=1024)
    assert result.shape == "absent"
    assert result.raw is None


def test_dangling_symlink_is_absent(tmp_path: Path) -> None:
    link = tmp_path / "dangling.json"
    link.symlink_to(tmp_path / "gone.json")
    assert read_sidecar_bytes(link, max_bytes=1024).shape == "absent"


def test_fifo_not_regular_no_hang(tmp_path: Path) -> None:
    """[round-7 P-1] a FIFO folds into ``not_regular`` WITHOUT blocking."""
    fifo = tmp_path / "fifo.json"
    os.mkfifo(fifo)
    with _hang_watchdog():
        result = read_sidecar_bytes(fifo, max_bytes=1024)
    assert result.shape == "not_regular"
    assert "not a regular file" in result.detail
    assert result.raw is None


def test_directory_not_regular(tmp_path: Path) -> None:
    target = tmp_path / "dir.json"
    target.mkdir()
    result = read_sidecar_bytes(target, max_bytes=1024)
    assert result.shape == "not_regular"


def test_socket_not_regular(tmp_path: Path) -> None:
    target = tmp_path / "sock.json"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(target))
        with _hang_watchdog():
            result = read_sidecar_bytes(target, max_bytes=1024)
    finally:
        server.close()
    # A bound-but-unconnected UNIX socket refuses open(2) with ENXIO on
    # Linux — either the not_regular or the read_error fold is the SAFE
    # answer (never a hang, never raw bytes).
    assert result.shape in {"not_regular", "read_error"}
    assert result.raw is None


def test_device_symlink_not_regular(tmp_path: Path) -> None:
    """[round-7 P-1] a ``/dev/zero`` symlink can never feed infinite zeros."""
    if not Path("/dev/zero").exists():  # pragma: no cover — POSIX always has it
        pytest.skip("/dev/zero not available on this platform")
    link = tmp_path / "zero.json"
    link.symlink_to("/dev/zero")
    with _hang_watchdog():
        result = read_sidecar_bytes(link, max_bytes=1024)
    assert result.shape == "not_regular"


def test_oversized_fstat_cap(tmp_path: Path) -> None:
    """[round-6 N-1 / round-7 P-2] the cap is judged on the OPEN fd's fstat."""
    target = tmp_path / "big.json"
    target.write_bytes(b"x" * 65)
    result = read_sidecar_bytes(target, max_bytes=64)
    assert result.shape == "oversized"
    assert "65 bytes > the 64 B cap" in result.detail


def test_open_oserror_is_read_error(tmp_path: Path) -> None:
    """A non-absent open(2) fault (ENOTDIR parent) folds into ``read_error``."""
    parent = tmp_path / "file.json"
    parent.write_bytes(b"{}")
    result = read_sidecar_bytes(parent / "nested.json", max_bytes=1024)
    assert result.shape == "read_error"
    assert result.error_type == "NotADirectoryError"


def test_read_error_shape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An OSError on the OPEN descriptor (fstat) folds into ``read_error``
    and the descriptor is still closed (the ``owned`` finally arm)."""
    target = tmp_path / "sidecar.json"
    target.write_bytes(b"{}")

    def _boom_fstat(_fd: int) -> os.stat_result:
        raise OSError(5, "I/O error")

    monkeypatch.setattr(os, "fstat", _boom_fstat)
    result = read_sidecar_bytes(target, max_bytes=1024)
    monkeypatch.undo()
    assert result.shape == "read_error"
    assert result.error_type == "OSError"
    assert "I/O error" in result.detail


def test_read_is_bounded_even_if_file_grows_after_fstat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[refuter round 8 S-2 — mutant L4 killer; MOVED from test_pull_intent.py]
    the loader's ``read(max_bytes)`` bound is load-bearing for the same-inode
    GROW-after-fstat vector (the one class the fstat-on-fd size cap cannot
    see — a swap cannot reach the open descriptor, but the SAME inode growing
    between the fstat and the read can): even when the fstat reports an
    under-cap size over a file whose on-disk bytes are 4x the cap, the served
    bytes are EXACTLY the cap — never the whole grown file. Deterministic via
    a monkeypatched ``os.fstat`` returning a small ``st_size`` (the grow
    simulated by writing the big file first — the loader sees a small stat,
    then reads from the big inode); under the unbounded-read mutant
    (``handle.read()``) all 4x-cap bytes come back and this pin fails."""
    cap = 64
    target = tmp_path / "grow.json"
    target.write_bytes(b"x" * (cap * 4))  # the inode already holds 4x the cap
    real_fstat = os.fstat

    def _small_fstat(fd: int) -> os.stat_result:
        st = real_fstat(fd)
        return os.stat_result(
            (
                st.st_mode,
                st.st_ino,
                st.st_dev,
                st.st_nlink,
                st.st_uid,
                st.st_gid,
                cap - 1,  # under-cap: the size gate passes, the read decides
                int(st.st_atime),
                int(st.st_mtime),
                int(st.st_ctime),
            )
        )

    monkeypatch.setattr(os, "fstat", _small_fstat)
    result = read_sidecar_bytes(target, max_bytes=cap)
    monkeypatch.undo()
    assert result.shape == "ok"
    assert result.raw is not None
    assert len(result.raw) == cap  # BOUNDED: never the 4x-cap on-disk bytes


def test_pull_intent_reexport_identity() -> None:
    """The shim pin: pull_intent re-exports THE SAME objects (never a copy)."""
    from brutex.dashboard import pull_intent

    assert pull_intent.read_sidecar_bytes is read_sidecar_bytes
    assert pull_intent.SidecarReadResult is SidecarReadResult
    assert "read_sidecar_bytes" in pull_intent.__all__
    assert "SidecarReadResult" in pull_intent.__all__


# --------------------------------------------------------------------------- #
# open_regular_binary / read_regular_bytes — the fd-validated raw primitives
# (DEC-LAKE-READ-INODE-GUARD-001: homed HERE, re-exported by
# brutex.data.guarded_read; this gate's standalone coverage of the new defs).
# --------------------------------------------------------------------------- #
def test_open_regular_binary_reads_regular_file(tmp_path: Path) -> None:
    """A regular file opens + reads byte-identically to a plain open."""
    from brutex.data.sidecar_read import open_regular_binary

    target = tmp_path / "plain.bin"
    target.write_bytes(b"\x00\x01payload")
    with open_regular_binary(target) as handle:
        assert handle.read() == b"\x00\x01payload"


def test_open_regular_binary_fifo_raises_not_regular_no_hang(tmp_path: Path) -> None:
    """A planted FIFO raises NotRegularFileError WITHOUT blocking (the (a) defense).

    Fails-before shape: a plain ``open(fifo, "rb")`` blocks for a writer
    FOREVER — the watchdog turns that hang into a loud failure.
    """
    from brutex.data.sidecar_read import NotRegularFileError, open_regular_binary

    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    with _hang_watchdog(), pytest.raises(NotRegularFileError):
        open_regular_binary(fifo)


def test_open_regular_binary_absent_raises_filenotfound(tmp_path: Path) -> None:
    """An absent path raises its ordinary FileNotFoundError (never wrapped)."""
    from brutex.data.sidecar_read import open_regular_binary

    with pytest.raises(FileNotFoundError):
        open_regular_binary(tmp_path / "ghost.bin")


def test_open_regular_binary_not_regular_closes_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refused fd is CLOSED on the not-regular arm (the owned-finally law)."""
    from brutex.data.sidecar_read import NotRegularFileError, open_regular_binary

    fifo = tmp_path / "pipe2"
    os.mkfifo(fifo)
    closed: list[int] = []
    real_close = os.close

    def _spy_close(fd: int) -> None:
        closed.append(fd)
        real_close(fd)

    # String-target monkeypatch (the #1810 CodeQL convention) — resolves the
    # SAME shared ``os`` module object sidecar_read binds at import time.
    monkeypatch.setattr("brutex.data.sidecar_read.os.close", _spy_close)
    with _hang_watchdog(), pytest.raises(NotRegularFileError):
        open_regular_binary(fifo)
    assert len(closed) == 1  # exactly one close — never leaked, never doubled


def test_read_regular_bytes_full_content(tmp_path: Path) -> None:
    """read_regular_bytes returns the COMPLETE bytes (deliberately uncapped)."""
    from brutex.data.sidecar_read import read_regular_bytes

    target = tmp_path / "big.bin"
    payload = b"z" * 70_000
    target.write_bytes(payload)
    assert read_regular_bytes(target) == payload


def test_read_regular_bytes_fifo_raises_not_regular(tmp_path: Path) -> None:
    """The whole-bytes convenience inherits the fd gate verbatim."""
    from brutex.data.sidecar_read import NotRegularFileError, read_regular_bytes

    fifo = tmp_path / "pipe3"
    os.mkfifo(fifo)
    with _hang_watchdog(), pytest.raises(NotRegularFileError):
        read_regular_bytes(fifo)


def test_guarded_read_reexport_identity() -> None:
    """guarded_read re-exports THE SAME primitive objects (never a copy)."""
    from brutex.data import guarded_read, sidecar_read

    assert guarded_read.open_regular_binary is sidecar_read.open_regular_binary
    assert guarded_read.read_regular_bytes is sidecar_read.read_regular_bytes
    assert guarded_read.NotRegularFileError is sidecar_read.NotRegularFileError
