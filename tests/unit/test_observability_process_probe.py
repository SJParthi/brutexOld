"""Unit tests for :mod:`brutex.observability.process_probe` (DEC-SOAK-REAL-TIER-001).

100% line + branch coverage of the cross-platform RSS + fd probe:

* :func:`read_rss_kib` — Linux ``/proc`` primary, ``ru_maxrss`` fallback (with
  the macOS bytes→KiB normalisation), ``None`` when both abstain.
* :func:`read_rss_kib_current` — the slope-safe reader: ``/proc`` current value
  ONLY, ``None`` off-``/proc`` (the ``ru_maxrss`` PEAK is never consulted, so a
  before/after-delta consumer can never gate on a peak).
* :func:`read_fd_count` — ``/proc/self/fd`` primary, ``/dev/fd`` fallback,
  ``None`` when neither lists.

Every branch is driven deterministically by monkeypatching the module's own
seams (``_read_proc_status`` / ``sys.platform`` / ``os.listdir``), so the tests
are platform-independent — they pass on the Linux CI box AND assert the macOS
fallback paths the real self-hosted soak tier depends on.
"""

from __future__ import annotations

import pytest
from brutex.observability import process_probe


class TestReadRssKibProc:
    def test_parses_vmrss_line(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            process_probe,
            "_read_proc_status",
            lambda: "Name:\tpython\nVmRSS:\t   12345 kB\nThreads:\t3\n",
        )
        assert process_probe._read_rss_kib_proc() == 12345

    def test_absent_vmrss_line_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            process_probe, "_read_proc_status", lambda: "Name:\tpython\nThreads:\t3\n"
        )
        assert process_probe._read_rss_kib_proc() is None

    def test_oserror_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom() -> str:
            raise OSError("no /proc here")

        monkeypatch.setattr(process_probe, "_read_proc_status", _boom)
        assert process_probe._read_rss_kib_proc() is None

    def test_real_proc_status_readable_on_linux(self) -> None:
        """The real ``open`` seam returns the live status text on a ``/proc`` box."""
        try:
            text = process_probe._read_proc_status()
        except OSError:
            pytest.skip("no /proc/self/status on this platform")
        else:
            assert "VmRSS:" in text


class TestReadRssKibRusage:
    def test_linux_returns_kib_verbatim(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(process_probe.sys, "platform", "linux")
        value = process_probe._read_rss_kib_rusage()
        assert value is not None
        assert value > 0

    def test_darwin_normalises_bytes_to_kib(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(process_probe.sys, "platform", "linux")
        kib_linux = process_probe._read_rss_kib_rusage()
        monkeypatch.setattr(process_probe.sys, "platform", "darwin")
        kib_darwin = process_probe._read_rss_kib_rusage()
        assert kib_linux is not None and kib_darwin is not None
        # macOS treats the SAME ru_maxrss integer as BYTES → //1024 the Linux read.
        assert kib_darwin == kib_linux // 1024


class TestReadRssKib:
    def test_prefers_proc_when_available(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(process_probe, "_read_rss_kib_proc", lambda: 4242)
        # rusage must NOT be consulted when /proc answered.
        monkeypatch.setattr(
            process_probe,
            "_read_rss_kib_rusage",
            lambda: pytest.fail("rusage fallback must not run when /proc answered"),
        )
        assert process_probe.read_rss_kib() == 4242

    def test_falls_back_to_rusage(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(process_probe, "_read_rss_kib_proc", lambda: None)
        monkeypatch.setattr(process_probe, "_read_rss_kib_rusage", lambda: 9001)
        assert process_probe.read_rss_kib() == 9001


class TestReadRssKibCurrent:
    """The slope-safe reader: /proc current-value ONLY, never the peak fallback."""

    def test_returns_proc_current_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(process_probe, "_read_rss_kib_proc", lambda: 1337)
        assert process_probe.read_rss_kib_current() == 1337

    def test_abstains_off_proc_never_falls_into_peak(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Off-/proc the reader returns None — the ru_maxrss PEAK is never consulted.

        A before/after-DELTA consumer (test_memory_growth's RSS backstop) rides
        this contract: a peak can never masquerade as a growth slope.
        """
        monkeypatch.setattr(process_probe, "_read_rss_kib_proc", lambda: None)
        monkeypatch.setattr(
            process_probe,
            "_read_rss_kib_rusage",
            lambda: pytest.fail("the PEAK fallback must never serve a slope consumer"),
        )
        assert process_probe.read_rss_kib_current() is None


class TestReadFdCount:
    def test_counts_proc_fd(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(process_probe.os, "listdir", lambda path: ["0", "1", "2", "7"])
        assert process_probe.read_fd_count() == 4

    def test_falls_back_to_dev_fd(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _listdir(path: str) -> list[str]:
            if path == "/proc/self/fd":
                raise OSError("no /proc/self/fd")
            assert path == "/dev/fd"
            return ["0", "1", "2"]

        monkeypatch.setattr(process_probe.os, "listdir", _listdir)
        assert process_probe.read_fd_count() == 3

    def test_returns_none_when_neither_lists(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(path: str) -> list[str]:
            raise OSError(f"cannot list {path}")

        monkeypatch.setattr(process_probe.os, "listdir", _boom)
        assert process_probe.read_fd_count() is None

    def test_real_fd_count_positive_on_linux(self) -> None:
        """The live probe returns a positive count on a ``/proc`` or ``/dev/fd`` box."""
        count = process_probe.read_fd_count()
        if count is None:
            pytest.skip("no /proc/self/fd or /dev/fd on this platform")
        else:
            assert count > 0
