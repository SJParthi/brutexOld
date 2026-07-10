"""The dependency-free boot-phase marker (DEC-LAKE-HEAL-STARTUP-EPOCH-001 F2).

`brutex.cli._boot_mark` makes the pre-`configure_logging` dashboard-boot dead air
visible on stderr before structlog exists. Pins: the `[brutex boot +…s] <phase>`
shape, a monotonic origin captured once (deltas grow), STDERR (never stdout), and
best-effort (never raises).

Whitebox: drives the private `_boot_mark` + `_BOOT_T0` directly (the DEC-P0-017
convention), so the file silences ``reportPrivateUsage``.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import re

import pytest
from brutex import cli


@pytest.fixture(autouse=True)
def _reset_boot_origin() -> None:
    """Each test starts with a fresh boot origin (the module global is one-shot)."""
    cli._BOOT_T0 = None


class TestBootMark:
    def test_writes_the_phase_shape_to_stderr(self, capsys: pytest.CaptureFixture[str]) -> None:
        cli._boot_mark("hello phase")
        captured = capsys.readouterr()
        assert captured.out == ""  # NEVER stdout (version stdout contract untouched)
        assert re.fullmatch(r"\[brutex boot \+\s*0\.0s\] hello phase\n", captured.err)

    def test_first_call_sets_origin_and_later_deltas_grow(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ticks = iter([100.0, 103.5])  # one monotonic read per _boot_mark call

        def _fake_monotonic() -> float:
            return next(ticks)

        # `_boot_mark` imports `time` locally; patch the module's monotonic.
        import time as _time

        monkeypatch.setattr(_time, "monotonic", _fake_monotonic)
        cli._boot_mark("origin")  # captures origin=100.0, prints +0.0s
        cli._boot_mark("later")  # reads 103.5 → +3.5s
        err = capsys.readouterr().err.splitlines()
        assert err[0].endswith("+   0.0s] origin")
        assert err[1].endswith("+   3.5s] later")
        assert cli._BOOT_T0 == 100.0  # origin captured ONCE, not re-stamped

    def test_never_raises_when_stderr_is_broken(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import sys

        class _BrokenStderr:
            def write(self, _s: str) -> int:
                raise OSError("broken pipe")

            def flush(self) -> None:  # pragma: no cover — write raises first
                raise OSError("broken pipe")

        # cli does `import sys`, so `cli.sys is sys`; patch the shared module.
        monkeypatch.setattr(sys, "stderr", _BrokenStderr())
        cli._boot_mark("must not raise")  # the suppressed-exception path swallows it
