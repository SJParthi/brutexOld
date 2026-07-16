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
import threading
import time
from collections.abc import Callable, Generator
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
    read_capped_bytes_deadline,
    read_parquet_regular,
    read_parquet_schema_regular,
    read_regular_bytes,
    read_regular_bytes_deadline,
    read_regular_text_deadline,
    scan_parquet_regular,
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


class TestScanParquetRegular:
    """The LAZY-scan twin (DEC-LAKE-READ-INODE-GUARD-002): the eager per-file
    fd probe at scan-BUILD time closes the collect-time FIFO hang the eager
    helpers cannot cover, while regular files collect byte-identical frames
    with lazy pushdown preserved."""

    def test_list_source_collect_identical_to_plain_scan(self, tmp_path: Path) -> None:
        """Explicit-list parity: the guarded lazy scan over a ``list[str]``
        (the ist_span_partition_files / month_partition_files site shape)
        collects the SAME frame as polars' own lazy scan."""
        _write_parquet(tmp_path / "2024-01.parquet")
        frame_b = pl.DataFrame({"timestamp": [4, 5], "close": [12.0, 12.5], "volume": [3, 4]})
        frame_b.write_parquet(tmp_path / "2024-02.parquet")
        files = [str(tmp_path / "2024-01.parquet"), str(tmp_path / "2024-02.parquet")]
        got = scan_parquet_regular(files).collect()
        plain = pl.scan_parquet(files).collect()
        assert got.equals(plain)

    def test_glob_source_collect_identical_to_plain_scan(self, tmp_path: Path) -> None:
        """Glob-string parity: the ORIGINAL pattern is handed to polars, so
        its own expansion/order laws apply — frames byte-identical (the
        timeframe_materialize whole-tree ``**/*.parquet`` site shape)."""
        part = tmp_path / "1minute" / "2024"
        part.mkdir(parents=True)
        _write_parquet(part / "01.parquet")
        frame_b = pl.DataFrame({"timestamp": [4, 5], "close": [12.0, 12.5], "volume": [3, 4]})
        frame_b.write_parquet(part / "02.parquet")
        pattern = str(tmp_path / "1minute") + "/**/*.parquet"
        got = scan_parquet_regular(pattern).collect()
        plain = pl.scan_parquet(pattern).collect()
        assert got.equals(plain)

    def test_schema_tolerant_kwargs_pass_through(self, tmp_path: Path) -> None:
        """The mixed 7-col/17-col tree shape (DEC-GREEKS-ENGINE-PR2B-001): the
        ``missing_columns='insert'`` / ``extra_columns='ignore'`` kwargs reach
        polars unchanged — parity with the plain schema-tolerant scan."""
        _write_parquet(tmp_path / "2024-01.parquet")
        enriched = pl.DataFrame(
            {
                "timestamp": [7],
                "close": [13.0],
                "volume": [9],
                "iv": [0.21],
            }
        )
        enriched.write_parquet(tmp_path / "2024-02.parquet")
        files = [str(tmp_path / "2024-01.parquet"), str(tmp_path / "2024-02.parquet")]
        got = scan_parquet_regular(
            files, missing_columns="insert", extra_columns="ignore"
        ).collect()
        plain = pl.scan_parquet(files, missing_columns="insert", extra_columns="ignore").collect()
        assert got.equals(plain)

    def test_projection_pushdown_survives_the_guard(self, tmp_path: Path) -> None:
        """Lazy semantics preserved: a ``.select(...)`` on the guarded scan
        projects identically to the plain scan (the spot_join site shape)."""
        _write_parquet(tmp_path / "2024-01.parquet")
        files = [str(tmp_path / "2024-01.parquet")]
        got = scan_parquet_regular(files).select("timestamp", "close").collect()
        plain = pl.scan_parquet(files).select("timestamp", "close").collect()
        assert got.equals(plain)
        assert got.columns == ["timestamp", "close"]

    def test_fifo_in_list_raises_eagerly_at_scan_build(self, tmp_path: Path) -> None:
        """THE residual this helper exists for: a planted FIFO among the month
        files raises typed at scan-BUILD (before any collect) — pre-fix the
        bare ``pl.scan_parquet(files).collect()`` hung in C-level fifo_open
        where no Python guard can fire."""
        _write_parquet(tmp_path / "2024-01.parquet")
        os.mkfifo(tmp_path / "2024-02.parquet")
        files = [str(tmp_path / "2024-01.parquet"), str(tmp_path / "2024-02.parquet")]
        with _hang_watchdog(), pytest.raises(NotRegularFileError):
            scan_parquet_regular(files)

    def test_fifo_under_glob_raises_eagerly_at_scan_build(self, tmp_path: Path) -> None:
        _write_parquet(tmp_path / "2024-01.parquet")
        os.mkfifo(tmp_path / "2024-02.parquet")
        with _hang_watchdog(), pytest.raises(NotRegularFileError):
            scan_parquet_regular(str(tmp_path / "*.parquet"))

    def test_vanished_list_entry_raises_oserror_eagerly(self, tmp_path: Path) -> None:
        """A list entry that no longer exists raises its ordinary ``OSError``
        at scan-build (matching the guarded eager readers' error timing)."""
        with pytest.raises(FileNotFoundError):
            scan_parquet_regular([str(tmp_path / "gone.parquet")])

    def test_zero_match_glob_probes_nothing_and_keeps_polars_error_contract(
        self, tmp_path: Path
    ) -> None:
        """A no-match pattern probes zero files; the error contract at collect
        is polars' own empty-expansion behavior, byte-identical to the plain
        scan (same exception type)."""
        pattern = str(tmp_path / "absent" / "*.parquet")
        # pytest.raises binds ``plain_type`` unconditionally (the plain scan
        # raising on empty expansion IS the premise — a non-raise fails here),
        # so the name can never be read unbound (CodeQL alert #1169 class).
        with pytest.raises((pl.exceptions.PolarsError, OSError)) as plain_exc:
            pl.scan_parquet(pattern).collect()
        plain_type = type(plain_exc.value)
        with pytest.raises(plain_type):
            scan_parquet_regular(pattern).collect()


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
        bytes-never-arrived class — folded into ``ReadStallError`` too, and
        NEVER registered in the stall gate (no worker was spawned, so there
        is nothing in flight to refuse against)."""
        target = tmp_path / "payload.json"
        target.write_bytes(b"{}")

        def _no_spawn(*_args: object, **_kwargs: object) -> bytes:
            raise DeadlineSpawnError("guard thread spawn failed")

        monkeypatch.setattr(guarded_read, "run_with_deadline", _no_spawn)
        with pytest.raises(ReadStallError) as excinfo:
            read_regular_bytes_deadline(target)
        assert isinstance(excinfo.value.__cause__, DeadlineSpawnError)
        assert str(target) not in guarded_read._STALLED_READS
        # The next read (guard restored) proceeds unhindered — never refused.
        monkeypatch.undo()
        assert read_regular_bytes_deadline(target) == b"{}"

    def test_text_decode_error_raises_on_caller_thread(self, tmp_path: Path) -> None:
        target = tmp_path / "junk.json"
        target.write_bytes(b"\xff\xfe\xfa")
        with pytest.raises(UnicodeDecodeError):
            read_regular_text_deadline(target)

    def test_read_stall_error_is_oserror(self) -> None:
        assert issubclass(ReadStallError, OSError)
        assert any(issubclass(ReadStallError, member) for member in parquet_read_skip_errors())


class TestReadCappedBytesDeadline:
    """The bounded-read twin (the #1614 cap + one-byte over-probe idiom)."""

    def test_under_cap_full_bytes_over_false(self, tmp_path: Path) -> None:
        target = tmp_path / "report.json"
        target.write_bytes(b'{"status": "ok"}')
        raw, over = read_capped_bytes_deadline(target, cap=1024)
        assert raw == target.read_bytes()
        assert over is False

    def test_exactly_at_cap_over_false(self, tmp_path: Path) -> None:
        """Boundary law: a file of EXACTLY cap bytes is NOT oversized —
        the one-byte over-probe returns empty."""
        target = tmp_path / "report.json"
        target.write_bytes(b"x" * 64)
        raw, over = read_capped_bytes_deadline(target, cap=64)
        assert raw == b"x" * 64
        assert over is False

    def test_over_cap_truncates_and_flags(self, tmp_path: Path) -> None:
        target = tmp_path / "report.json"
        target.write_bytes(b"y" * 65)
        raw, over = read_capped_bytes_deadline(target, cap=64)
        assert raw == b"y" * 64  # never reads the oversized body whole
        assert over is True

    def test_fifo_raises_not_regular_bounded(self, tmp_path: Path) -> None:
        """Defense (a) through the deadline worker — the typed shape refusal
        re-raises unchanged on the caller thread, never a hang."""
        fifo = tmp_path / "report.json"
        os.mkfifo(fifo)
        with _hang_watchdog(), pytest.raises(NotRegularFileError):
            read_capped_bytes_deadline(fifo, cap=64)

    def test_stalled_open_raises_read_stall_error_in_bounded_time(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Defense (b): a wedged open/read releases the caller within the
        deadline as ``ReadStallError`` (the same OSError degrade-arm fold)."""
        target = tmp_path / "report.json"
        target.write_bytes(b"{}")
        release = threading.Event()
        real_open = guarded_read.open_regular_binary

        def _wedged(path: Path | str) -> object:
            release.wait(timeout=30.0)  # a hung volume: blocks, never raises
            return real_open(path)  # pragma: no cover — released at teardown

        monkeypatch.setattr(guarded_read, "open_regular_binary", _wedged)
        started = time.monotonic()
        try:
            with pytest.raises(ReadStallError, match="stalled past") as excinfo:
                read_capped_bytes_deadline(target, cap=64, deadline_s=0.2, what="capped read")
        finally:
            release.set()
        assert time.monotonic() - started < 4.0  # bounded — never the pre-fix hang
        assert isinstance(excinfo.value.__cause__, DeadlineExceeded)


class TestStallSingleFlightGate:
    """The per-path stall gate (the hostile-review MINOR-2 fold): a polled
    site on a persistently wedged volume must never leak one abandoned
    daemon PER POLL — later reads of a still-stalled path refuse instantly."""

    def test_stalled_path_refuses_instantly_then_self_heals_on_late_return(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "payload.json"
        target.write_bytes(b'{"a": 1}')
        release = threading.Event()
        real_read = guarded_read.read_regular_bytes
        real_rwd = guarded_read.run_with_deadline
        spawned: list[str] = []

        def _wedged(path: Path | str) -> bytes:
            release.wait(timeout=30.0)  # a hung volume: blocks, never raises
            return real_read(path)

        def _counting_rwd(
            fn: Callable[[], bytes],
            seconds: float,
            *,
            what: str = "guarded file read",
            thread_name: str = "guarded-read",
        ) -> bytes:
            spawned.append(what)
            return real_rwd(fn, seconds, what=what, thread_name=thread_name)

        monkeypatch.setattr(guarded_read, "read_regular_bytes", _wedged)
        monkeypatch.setattr(guarded_read, "run_with_deadline", _counting_rwd)
        try:
            # (1) The first poll blows its deadline and REGISTERS the path.
            with pytest.raises(ReadStallError, match="stalled past"):
                read_regular_bytes_deadline(target, deadline_s=0.2, what="first poll")
            key = str(target)
            assert key in guarded_read._STALLED_READS
            # (2) Later polls of the SAME path refuse INSTANTLY — no new
            #     reader daemon is spawned (the single-flight law), and the
            #     refusal is the SAME OSError-subclass fold.
            started = time.monotonic()
            with pytest.raises(ReadStallError, match="refused"):
                read_regular_bytes_deadline(target, deadline_s=30.0, what="second poll")
            assert time.monotonic() - started < 1.0
            # (3) The capped twin shares the SAME gate (one law, all readers).
            with pytest.raises(ReadStallError, match="refused"):
                read_capped_bytes_deadline(target, cap=64, deadline_s=30.0, what="capped poll")
            assert spawned == ["first poll"]  # exactly ONE daemon ever spawned
        finally:
            release.set()
        # (4) The abandoned worker finally returns — its ``finally`` sets the
        #     done event; the gate SELF-HEALS lazily at the next consult.
        done = guarded_read._STALLED_READS[str(target)]
        assert done.wait(timeout=10.0), "the released worker never set its done event"
        monkeypatch.undo()  # restore the real read + guard
        assert read_regular_bytes_deadline(target) == b'{"a": 1}'
        assert str(target) not in guarded_read._STALLED_READS  # entry deleted

    def test_completed_just_after_expiry_never_registers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The completed-just-after-expiry race: the worker's ``finally`` set
        the done event BEFORE the caller processed the expiry — nothing is
        left in flight, so the path is NOT registered (the very next read
        proceeds normally instead of a spurious refusal)."""
        target = tmp_path / "payload.json"
        target.write_bytes(b'{"a": 1}')

        def _late_expiry(fn: Callable[[], bytes], *_args: object, **_kwargs: object) -> bytes:
            fn()  # the worker COMPLETED (done set) ...
            raise DeadlineExceeded("test read deadline exceeded (0.2s)")  # ... yet expired

        monkeypatch.setattr(guarded_read, "run_with_deadline", _late_expiry)
        with pytest.raises(ReadStallError, match="stalled past"):
            read_regular_bytes_deadline(target, deadline_s=0.2, what="racy read")
        assert str(target) not in guarded_read._STALLED_READS
        monkeypatch.undo()
        assert read_regular_bytes_deadline(target) == b'{"a": 1}'  # never refused


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
