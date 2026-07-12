"""Supervisor run-core seam coverage (DEC-SWEEP-UNKILLABLE-CORE-001 WP-6).

Stem-matching supplement so the touched-module discovery gate proves
``brutex.engine.sweep_supervisor`` at 100% over its OWN ``test_engine_sweep_
supervisor*.py`` suite: the WP-5 additions — the G4 honesty keys, the run-core
``run_id``/``suspect``/``stage`` additive keys, the G11 warn-once fail-soft
write, and its ``reset_progress_write_warnings`` re-arm seam — were previously
exercised only by the run-core suites (a foreign stem the gate cannot map).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Final

from brutex.engine.sweep_progress import SweepProgress
from brutex.engine.sweep_supervisor import (
    publish_progress,
    reset_progress_write_warnings,
)
from structlog.testing import capture_logs

#: D-6 traceability — test name → requirement/gap tokens.
TRACEABILITY: Final[dict[str, tuple[str, ...]]] = {
    "test_g4_publish_progress_always_stamps_wall_clock_and_pid": ("G4",),
    "test_g4_run_core_keys_added_only_when_given": ("G4",),
    "test_g11_write_failure_warns_once_per_path_then_silent": ("G11",),
    "test_g11_reset_seam_rearms_the_warn_latch": ("G11",),
}


def _snapshot() -> object:
    progress = SweepProgress()
    progress.start(enumerated=4)
    progress.advance(current="t0", survived=True, skipped=False)
    return progress.snapshot()


def test_g4_publish_progress_always_stamps_wall_clock_and_pid(tmp_path: Path) -> None:
    path = tmp_path / "progress" / "sweep-progress.json"
    publish_progress(path, _snapshot())  # type: ignore[arg-type]
    doc = json.loads(path.read_text())
    assert isinstance(doc["wall_time_utc"], float)
    assert doc["pid"] == os.getpid()
    # No run-core kwargs given ⇒ the additive keys are honestly ABSENT.
    assert "run_id" not in doc
    assert "suspect" not in doc
    assert "stage" not in doc


def test_g4_run_core_keys_added_only_when_given(tmp_path: Path) -> None:
    path = tmp_path / "sweep-progress.json"
    publish_progress(
        path,
        _snapshot(),  # type: ignore[arg-type]
        run_id="cd" * 32,
        suspect=False,
        stage="sweep",
    )
    doc = json.loads(path.read_text())
    assert doc["run_id"] == "cd" * 32
    assert doc["suspect"] is False
    assert doc["stage"] == "sweep"
    # Pre-existing snapshot keys ride along byte-unchanged.
    assert doc["evaluated"] == 1


def test_g11_write_failure_warns_once_per_path_then_silent(tmp_path: Path) -> None:
    # A DIRECTORY at the progress path makes the atomic replace fail — the
    # G11 law: ONE WARN per path per process, run never killed.
    reset_progress_write_warnings()
    path = tmp_path / "progress-as-dir"
    path.mkdir()
    with capture_logs() as logs:
        publish_progress(path, _snapshot())  # type: ignore[arg-type]
        publish_progress(path, _snapshot())  # type: ignore[arg-type]
    warns = [e for e in logs if e["event"] == "sweep_supervisor.progress_write_failed"]
    assert len(warns) == 1
    assert warns[0]["path"] == str(path)
    reset_progress_write_warnings()


def test_g11_reset_seam_rearms_the_warn_latch(tmp_path: Path) -> None:
    reset_progress_write_warnings()
    path = tmp_path / "progress-as-dir"
    path.mkdir()
    with capture_logs() as logs:
        publish_progress(path, _snapshot())  # type: ignore[arg-type]
        reset_progress_write_warnings()  # the test seam re-arms the latch
        publish_progress(path, _snapshot())  # type: ignore[arg-type]
    warns = [e for e in logs if e["event"] == "sweep_supervisor.progress_write_failed"]
    assert len(warns) == 2
    reset_progress_write_warnings()
