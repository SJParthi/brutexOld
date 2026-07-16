# pyright: reportPrivateUsage=none
"""``brutex.data.guarded_read`` — the COMMON two-defense gated file reader.

DEC-LAKE-READ-INODE-GUARD-001: defense (a) — the fd-level ``S_ISREG`` gate
(a planted FIFO / ``/dev/zero`` char-device symlink raises the typed
``NotRegularFileError`` instead of blocking forever / streaming infinite
zeros) — and defense (b) — the deadline-wrapped variants (a wedged REGULAR
file on a stalled volume raises ``ReadStallError`` in bounded time). Every
hang-class case runs under the house SIGALRM watchdog (the #1614 round-6
idiom), and regular-file behavior is pinned byte-identical to the plain
reads. 100% line+branch standalone (the ci.yml module gate).
"""

from __future__ import annotations

import os
import signal
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import polars as pl
import pytest
from brutex.config.deadline import DeadlineExceeded, DeadlineSpawnError
from brutex.data import guarded_read
from brutex.data.errors import parquet_read_skip_errors
from brutex.data.guarded_read import (
    NotRegularFileError,
    ReadStallError,
    open_regular_binary,
    read_parquet_regular,
    read_parquet_schema_regular,
    read_regular_bytes,
    read_regular_bytes_deadline,
    read_regular_text_deadline,
)


@contextmanager
def _hang_watchdog(seconds: int = 5) -> Generator[None]:
    """Turn an indefinite block inside the body into a LOUD failure.

    The fails-before shape of every defense-(a) case is a read that blocks
    FOREVER (a FIFO open waits for a writer), so a plain regression would
    HANG rather than fail; the ``SIGALRM`` handler interrupts the blocked
    syscall and fails the test instead (the #1614 round-6 watchdog idiom).
    """

    def _boom(_signum: int, _frame: object) -> None:
        raise AssertionError(f"guarded read blocked > {seconds}s — the inode-guard hang is back")

    previous = signal.signal(signal.SIGALRM, _boom)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def _write_parquet(path: Path) -> pl.DataFrame:
    frame = pl.DataFrame(
        {"timestamp": [1, 2, 3], "close": [10.5, 11.25, 9.75], "volume": [100, 0, 7]}
    )
    frame.write_parquet(path)
    return frame


class TestOpenRegularBinary:
    def test_regular_file_reads_byte_identical(self, tmp_path: Path) -> None:
        target = tmp_path / "payload.bin"
        target.write_bytes(b"\x00\x01payload\xff")
        with open_regular_binary(target) as handle:
            assert handle.read() == b"\x00\x01payload\xff"
        assert handle.closed  # the fd is owned by the handle and released.

    def test_fifo_raises_not_regular_without_blocking(self, tmp_path: Path) -> None:
        fifo = tmp_path / "fifo.parquet"
        os.mkfifo(fifo)
        with _hang_watchdog(), pytest.raises(NotRegularFileError, match="not a regular file"):
            open_regular_binary(fifo)

    def test_dev_zero_symlink_defeats_size_caps_but_not_the_gate(self, tmp_path: Path) -> None:
        """The size-cap-defeat pin: a ``/dev/zero`` symlink reports st_size 0
        (so ANY pre-read size cap passes) yet streams infinite zeros — the
        ``S_ISREG`` gate refuses it before a single byte is read."""
        if not Path("/dev/zero").exists():  # pragma: no cover — POSIX always has it
            pytest.skip("/dev/zero not available on this platform")
        link = tmp_path / "zero.json"
        link.symlink_to("/dev/zero")
        assert os.stat(link).st_size == 0  # a size cap is NO defence
        with _hang_watchdog(), pytest.raises(NotRegularFileError):
            open_regular_binary(link)

    def test_directory_raises_not_regular(self, tmp_path: Path) -> None:
        target = tmp_path / "dir.json"
        target.mkdir()
        with pytest.raises(NotRegularFileError):
            open_regular_binary(target)

    def test_dangling_symlink_raises_file_not_found(self, tmp_path: Path) -> None:
        link = tmp_path / "dangling.json"
        link.symlink_to(tmp_path / "gone.json")
        with pytest.raises(FileNotFoundError):
            open_regular_binary(link)

    def test_fstat_fault_propagates_and_closes_fd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An OSError on the OPEN descriptor propagates raw and the ``owned``
        finally arm still closes the fd (no descriptor leak)."""
        target = tmp_path / "payload.bin"
        target.write_bytes(b"x")
        closed: list[int] = []
        real_close = os.close

        def _boom_fstat(_fd: int) -> os.stat_result:
            raise OSError(5, "I/O error")

        def _spy_close(fd: int) -> None:
            closed.append(fd)
            real_close(fd)

        monkeypatch.setattr("brutex.data.sidecar_read.os.fstat", _boom_fstat)
        monkeypatch.setattr("brutex.data.sidecar_read.os.close", _spy_close)
        with pytest.raises(OSError, match="I/O error"):
            open_regular_binary(target)
        monkeypatch.undo()
        assert len(closed) == 1

    def test_not_regular_is_oserror_and_skip_set_member(self) -> None:
        """The fold law: the typed refusal is an ``OSError`` subclass, so
        every existing unreadable-file degrade arm (incl. the data layer's
        ``parquet_read_skip_errors()``) absorbs it with zero except churn."""
        assert issubclass(NotRegularFileError, OSError)
        assert any(issubclass(NotRegularFileError, member) for member in parquet_read_skip_errors())


class TestReadRegularBytes:
    def test_byte_identity(self, tmp_path: Path) -> None:
        target = tmp_path / "payload.json"
        target.write_bytes(b'{"a": 1}')
        assert read_regular_bytes(target) == target.read_bytes()

    def test_fifo_raises_bounded(self, tmp_path: Path) -> None:
        fifo = tmp_path / "fifo.json"
        os.mkfifo(fifo)
        with _hang_watchdog(), pytest.raises(NotRegularFileError):
            read_regular_bytes(fifo)


class TestReadParquetRegular:
    def test_regular_file_frame_identical(self, tmp_path: Path) -> None:
        target = tmp_path / "2024-01.parquet"
        frame = _write_parquet(target)
        via_guard = read_parquet_regular(target)
        via_path = pl.read_parquet(target, glob=False)
        assert via_guard.equals(via_path)
        assert via_guard.equals(frame)

    def test_columns_projection_identical(self, tmp_path: Path) -> None:
        target = tmp_path / "2024-01.parquet"
        _write_parquet(target)
        via_guard = read_parquet_regular(target, columns=("timestamp", "close"))
        via_path = pl.read_parquet(target, columns=["timestamp", "close"], glob=False)
        assert via_guard.equals(via_path)
        assert via_guard.columns == ["timestamp", "close"]

    def test_fifo_raises_not_regular_bounded(self, tmp_path: Path) -> None:
        fifo = tmp_path / "2024-01.parquet"
        os.mkfifo(fifo)
        with _hang_watchdog(), pytest.raises(NotRegularFileError):
            read_parquet_regular(fifo, columns=("timestamp",))

    def test_garbage_bytes_raise_polars_error(self, tmp_path: Path) -> None:
        """A corrupt REGULAR file keeps its ordinary PolarsError class — the
        guard narrows nothing about the existing corrupt-file degrades."""
        target = tmp_path / "junk.parquet"
        target.write_bytes(b"not parquet at all")
        with pytest.raises(pl.exceptions.PolarsError):
            read_parquet_regular(target)

    def test_schema_regular_identical(self, tmp_path: Path) -> None:
        target = tmp_path / "2024-01.parquet"
        _write_parquet(target)
        assert read_parquet_schema_regular(target) == dict(pl.read_parquet_schema(target))

    def test_schema_fifo_raises_bounded(self, tmp_path: Path) -> None:
        fifo = tmp_path / "2024-01.parquet"
        os.mkfifo(fifo)
        with _hang_watchdog(), pytest.raises(NotRegularFileError):
            read_parquet_schema_regular(fifo)


class TestReadRegularText:
    def test_byte_identity(self, tmp_path: Path) -> None:
        target = tmp_path / "payload.json"
        target.write_text('{"a": "₹", "b": 2}', encoding="utf-8")
        assert guarded_read.read_regular_text(target) == target.read_text(encoding="utf-8")

    def test_fifo_raises_not_regular_bounded(self, tmp_path: Path) -> None:
        fifo = tmp_path / "fifo.json"
        os.mkfifo(fifo)
        with _hang_watchdog(), pytest.raises(NotRegularFileError):
            guarded_read.read_regular_text(fifo)

    def test_decode_error_propagates(self, tmp_path: Path) -> None:
        target = tmp_path / "junk.json"
        target.write_bytes(b"\xff\xfe\xfa")
        with pytest.raises(UnicodeDecodeError):
            guarded_read.read_regular_text(target)


class TestReadParquetGlobRegular:
    def test_multi_file_frame_identical_to_plain_glob_read(self, tmp_path: Path) -> None:
        """Sorted-order parity: the fd-preopened multi-handle read yields the
        SAME frame as polars' own glob expansion over regular files."""
        part = tmp_path / "1minute" / "2024"
        part.mkdir(parents=True)
        _write_parquet(part / "01.parquet")
        frame_b = pl.DataFrame({"timestamp": [4, 5], "close": [12.0, 12.5], "volume": [3, 4]})
        frame_b.write_parquet(part / "02.parquet")
        pattern = str(tmp_path / "1minute" / "*" / "*.parquet")
        got = guarded_read.read_parquet_glob_regular(pattern)
        plain = pl.read_parquet(pattern)
        assert got.equals(plain)

    def test_columns_projection_identical(self, tmp_path: Path) -> None:
        _write_parquet(tmp_path / "2024-01.parquet")
        _write_parquet(tmp_path / "2024-02.parquet")
        pattern = str(tmp_path / "*.parquet")
        got = guarded_read.read_parquet_glob_regular(pattern, columns=("timestamp", "close"))
        plain = pl.read_parquet(pattern, columns=["timestamp", "close"])
        assert got.equals(plain)
        assert got.columns == ["timestamp", "close"]

    def test_fifo_among_partition_files_raises_bounded(self, tmp_path: Path) -> None:
        """THE planted-inode case this helper exists for: one FIFO hidden among
        real month partitions must raise typed — never hang the whole read."""
        _write_parquet(tmp_path / "2024-01.parquet")
        os.mkfifo(tmp_path / "2024-02.parquet")
        with _hang_watchdog(), pytest.raises(NotRegularFileError):
            guarded_read.read_parquet_glob_regular(str(tmp_path / "*.parquet"))

    def test_empty_glob_delegates_to_polars_for_the_genuine_error(self, tmp_path: Path) -> None:
        """Zero matches: delegate to ``pl.read_parquet(pattern)`` so callers see
        polars' own no-files error class — the degrade arms already fold it."""
        with pytest.raises((pl.exceptions.PolarsError, OSError)):
            guarded_read.read_parquet_glob_regular(str(tmp_path / "absent" / "*.parquet"))
        with pytest.raises((pl.exceptions.PolarsError, OSError)):
            guarded_read.read_parquet_glob_regular(
                str(tmp_path / "absent" / "*.parquet"), columns=("timestamp",)
            )

    def test_garbage_member_raises_polars_error(self, tmp_path: Path) -> None:
        _write_parquet(tmp_path / "2024-01.parquet")
        (tmp_path / "2024-02.parquet").write_bytes(b"not parquet at all")
        with pytest.raises(pl.exceptions.PolarsError):
            guarded_read.read_parquet_glob_regular(str(tmp_path / "*.parquet"))


class TestDeadlineVariants:
    def test_bytes_byte_identity(self, tmp_path: Path) -> None:
        target = tmp_path / "payload.json"
        target.write_bytes(b'{"a": 1}')
        assert read_regular_bytes_deadline(target) == target.read_bytes()

    def test_text_byte_identity(self, tmp_path: Path) -> None:
        target = tmp_path / "payload.json"
        target.write_text('{"a": "₹"}', encoding="utf-8")
        assert read_regular_text_deadline(target) == target.read_text(encoding="utf-8")

    def test_fifo_raises_not_regular_through_the_guard(self, tmp_path: Path) -> None:
        """Defense (a) fires INSIDE the deadline worker and re-raises the
        typed shape refusal unchanged on the caller thread."""
        fifo = tmp_path / "fifo.json"
        os.mkfifo(fifo)
        with _hang_watchdog(), pytest.raises(NotRegularFileError):
            read_regular_bytes_deadline(fifo)

    def test_stalled_read_raises_read_stall_error_in_bounded_time(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Defense (b): a wedged REGULAR-file read (simulated by a blocking
        read fn — the wedged-external-SSD shape, which ``S_ISREG`` cannot
        catch) releases the caller within the deadline as the typed
        ``ReadStallError`` (an OSError — the same degrade-arm fold)."""
        target = tmp_path / "payload.json"
        target.write_bytes(b"{}")
        release = {"done": False}

        def _wedged(_path: Path | str) -> bytes:
            while not release["done"]:  # a hung volume: blocks, never raises
                time.sleep(0.01)
            return b"{}"  # pragma: no cover — released only at teardown

        monkeypatch.setattr(guarded_read, "read_regular_bytes", _wedged)
        started = time.monotonic()
        try:
            with pytest.raises(ReadStallError, match="stalled past") as excinfo:
                read_regular_bytes_deadline(target, deadline_s=0.2, what="test read")
        finally:
            release["done"] = True
        elapsed = time.monotonic() - started
        assert elapsed < 4.0  # bounded — never the indefinite pre-fix hang
        assert isinstance(excinfo.value.__cause__, DeadlineExceeded)

    def test_spawn_failure_folds_into_read_stall_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The guard's OWN spawn failure (thread exhaustion) is the same
        bytes-never-arrived class — folded into ``ReadStallError`` too."""
        target = tmp_path / "payload.json"
        target.write_bytes(b"{}")

        def _no_spawn(*_args: object, **_kwargs: object) -> bytes:
            raise DeadlineSpawnError("guard thread spawn failed")

        monkeypatch.setattr(guarded_read, "run_with_deadline", _no_spawn)
        with pytest.raises(ReadStallError) as excinfo:
            read_regular_bytes_deadline(target)
        assert isinstance(excinfo.value.__cause__, DeadlineSpawnError)

    def test_text_decode_error_raises_on_caller_thread(self, tmp_path: Path) -> None:
        target = tmp_path / "junk.json"
        target.write_bytes(b"\xff\xfe\xfa")
        with pytest.raises(UnicodeDecodeError):
            read_regular_text_deadline(target)

    def test_read_stall_error_is_oserror(self) -> None:
        assert issubclass(ReadStallError, OSError)
        assert any(issubclass(ReadStallError, member) for member in parquet_read_skip_errors())


def test_module_import_is_wheel_free() -> None:
    """Importing the module must not import polars (the lazy-import law —
    light consumers like config/crossverify/nightly never pay the wheel,
    and the CLI ``version`` polars-free fast path stays intact)."""
    import subprocess
    import sys

    code = (
        "import sys\n"
        "assert 'polars' not in sys.modules\n"
        "import brutex.data.guarded_read\n"
        "assert 'polars' not in sys.modules, 'guarded_read imported polars at module import'\n"
        "print('wheel-free ok')\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "wheel-free ok" in proc.stdout
