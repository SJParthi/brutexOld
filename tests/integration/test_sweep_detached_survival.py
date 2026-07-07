"""R7 detached survival — the child outlives its dead parent (WP-5 integration).

A REAL three-process proof: this test spawns a PARENT python process which
uses :func:`brutex.engine.sweep_detach.spawn_detached_sweep` to launch a CHILD
as its own session leader, then the parent EXITS immediately. The child keeps
running past the parent's death and durably writes its completion artifact —
exactly the dashboard-restart / terminal-death survival law
(DEC-SWEEP-UNKILLABLE-CORE-001 R7). Plus the CLI ``--detach`` early-exit seam
(``cmd_sweep_all`` re-execs and returns without touching the lake).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

#: The child body: waits until AFTER the parent has provably exited, then
#: writes its completion artifact. The wait is bounded and deterministic —
#: it polls for the parent-dead marker the TEST writes once the parent's
#: subprocess.run() returns, so no fragile sleep race.
_CHILD = """
import sys, time
from pathlib import Path
marker = Path(sys.argv[1])
done = Path(sys.argv[2])
deadline = time.monotonic() + 30.0
while time.monotonic() < deadline:
    if marker.exists():
        break
    time.sleep(0.05)
else:
    raise SystemExit("parent-dead marker never appeared")
done.write_text("survived", encoding="utf-8")
"""

_PARENT = """
import sys
from pathlib import Path
from brutex.engine.sweep_detach import spawn_detached_sweep

results_root = Path(sys.argv[1])
marker = sys.argv[2]
done = sys.argv[3]
child_body = sys.argv[4]
handle = spawn_detached_sweep(
    (marker, done),
    results_root=results_root,
    command=(sys.executable, "-c", child_body),
    run_id_hint="r7-survival",
)
print(handle.pid)
"""


def test_r7_detached_child_survives_parent_death(tmp_path: Path) -> None:
    results_root = tmp_path / "sweeps"
    results_root.mkdir()
    marker = tmp_path / "parent-dead.marker"
    done = tmp_path / "child-done.txt"

    parent = subprocess.run(
        [sys.executable, "-c", _PARENT, str(results_root), str(marker), str(done), _CHILD],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
        cwd=str(tmp_path),
    )
    child_pid = int(parent.stdout.strip().splitlines()[-1])
    # The parent process has EXITED (subprocess.run returned) — prove it, then
    # tell the still-running child so via the marker file.
    assert parent.returncode == 0
    marker.write_text("parent is dead", encoding="utf-8")

    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline and not done.exists():
        time.sleep(0.05)
    assert done.exists(), "the detached child died with its parent (R7 violated)"
    assert done.read_text(encoding="utf-8") == "survived"
    assert child_pid != os.getpid()

    # The pidfile survives for a restarted dashboard to re-discover the run.
    pidfile = results_root / "runs" / "detached-sweep.pid"
    doc = json.loads(pidfile.read_text(encoding="utf-8"))
    assert doc["pid"] == child_pid
    assert doc["run_id_hint"] == "r7-survival"

    # Once the child finished, the liveness probe reads honestly not-running.
    from brutex.engine.sweep_detach import DetachedSweepHandle, probe_handle
    from brutex.engine.sweep_liveness import RunVerdict

    handle = DetachedSweepHandle(
        pid=child_pid,
        run_id_hint="r7-survival",
        results_root=results_root,
        cmdline=("stub",),
    )
    verdict_deadline = time.monotonic() + 10.0
    while time.monotonic() < verdict_deadline:
        if probe_handle(handle) is not RunVerdict.RUNNING:
            break
        time.sleep(0.05)  # pragma: no cover — child pid still settling
    assert probe_handle(handle) is not RunVerdict.RUNNING


def test_r7_cli_detach_flag_reexecs_and_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from brutex.cli import EXIT_OK, cmd_sweep_all

    # The re-exec strips ONLY --detach and forwards the invocation verbatim;
    # `sweep-all --help` makes the spawned child print help and exit clean.
    monkeypatch.setattr(sys, "argv", ["brutex", "sweep-all", "--help", "--detach"])
    code = cmd_sweep_all(
        lake_root=tmp_path / "lake",
        catalog_path=None,
        results_root=tmp_path / "sweeps",
        exchange="NSE",
        segment="INDEX",
        symbols="NIFTY",
        intervals="1minute",
        start="2026-01-01",
        end="2026-01-02",
        directions="BUY",
        k="1",
        conditions=None,
        sl="80",
        tp="50",
        top=5,
        mode="A",
        max_tuples=None,
        max_combos=None,
        data_snapshot_hash=None,
        detach=True,
    )
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "sweep-all detached — pid=" in out
    # The early exit spawned the child WITHOUT touching the lake root.
    assert not (tmp_path / "lake").exists()
    pidfile = tmp_path / "sweeps" / "runs" / "detached-sweep.pid"
    assert pidfile.exists()
    doc = json.loads(pidfile.read_text(encoding="utf-8"))
    assert "--detach" not in doc["cmdline"]
    assert "--help" in doc["cmdline"]
