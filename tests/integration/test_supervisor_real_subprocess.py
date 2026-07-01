"""End-to-end supervisor test over a REAL subprocess (DEC-SUPERVISOR-AUTORESTART-001).

The pure supervise loop is 100%-line+branch tested with a FAKE child in
``tests/unit/test_orchestrator_supervisor.py``; this ONE integration test proves
the production wiring — :func:`build_subprocess_launcher` +
:class:`PopenChildProcess` driving a real :class:`subprocess.Popen` through the
real :class:`ProcessSupervisor.run` — actually spawns, monitors, and relaunches
a child.

Determinism: the supervised child is a tiny inline Python script that exits
NON-ZERO the first time (no marker file present) and exits ZERO on the relaunch
(marker present), so the supervisor observes exactly one crash → one relaunch →
one clean exit, then stops. No sleeping beyond a sub-second backoff; no flakiness.
"""

from __future__ import annotations

import sys
from pathlib import Path

from brutex.orchestrator.supervisor import (
    BackoffPolicy,
    ProcessSupervisor,
    SupervisorOutcome,
    build_subprocess_launcher,
)

# A trivial, deterministic child: crash once (create the marker + exit 3), then
# on the relaunch see the marker and exit 0. Written to a temp file so the real
# `sys.executable` runs it in a fresh process each launch.
_CHILD_SCRIPT = """
import sys
from pathlib import Path

marker = Path(sys.argv[1])
if marker.exists():
    sys.exit(0)          # relaunch: succeed cleanly
marker.write_text("crashed once")
sys.exit(3)              # first run: crash
"""


def test_real_subprocess_crash_then_relaunch_then_clean_exit(tmp_path: Path) -> None:
    script = tmp_path / "flaky_child.py"
    script.write_text(_CHILD_SCRIPT, encoding="utf-8")
    marker = tmp_path / "marker.txt"

    argv = [sys.executable, str(script), str(marker)]
    # Tiny backoff so the one real relaunch wait is sub-second; a low cap so a
    # regression (infinite loop) fails fast rather than hanging CI.
    policy = BackoffPolicy(
        base_seconds=0.05,
        factor=2.0,
        cap_seconds=0.2,
        reset_after_uptime_seconds=3600.0,
        max_restarts=3,
    )
    supervisor = ProcessSupervisor(
        launch=build_subprocess_launcher(argv),
        policy=policy,
        subject="supervisor:itest",
    )

    result = supervisor.run()

    assert result.outcome is SupervisorOutcome.CLEAN_EXIT
    assert result.restarts == 1  # crashed once, relaunched once
    assert result.last_exit_code == 0  # the relaunch succeeded
    assert marker.exists()  # the child ran + relaunched
