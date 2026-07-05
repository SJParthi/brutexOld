# pyright: reportPrivateUsage=none
"""Tests for the ``brutex supervisor`` command wiring (DEC-SUPERVISOR-AUTORESTART-001).

Covers the CLI surface of the process supervisor: argument parsing, the
``main`` dispatch that threads every flag into :func:`brutex.cli.cmd_supervisor`,
and the two PURE helpers extracted for testability — :func:`_build_supervisor_backoff`
(flag → :class:`BackoffPolicy`, with module-default fallbacks) and
:func:`_supervisor_child_argv` (the forwarded ``sweep-daemon`` argv). The live
loop (``cmd_supervisor``) itself is ``# pragma: no cover`` (real subprocess +
signals + lake I/O); its pure supervise-loop core is 100%-tested in
``tests/unit/test_orchestrator_supervisor.py`` and end-to-end in
``tests/integration/test_supervisor_real_subprocess.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from brutex import cli
from brutex.orchestrator.supervisor import (
    DEFAULT_BACKOFF_BASE_SECONDS,
    DEFAULT_BACKOFF_CAP_SECONDS,
    DEFAULT_BACKOFF_FACTOR,
    DEFAULT_MAX_RESTARTS,
    DEFAULT_RESET_AFTER_UPTIME_SECONDS,
)

# ── Argument parsing ────────────────────────────────────────────────────


def test_parser_supervisor_defaults() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["supervisor"])
    assert args.command == "supervisor"
    assert args.child_args == []
    assert args.backoff_base_seconds is None
    assert args.backoff_factor is None
    assert args.backoff_cap_seconds is None
    assert args.reset_after_uptime_seconds is None
    assert args.supervisor_max_restarts is None
    assert args.shutdown_grace_seconds is None
    assert args.lake_root is None


def test_parser_supervisor_forwards_child_args_after_double_dash() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["supervisor", "--", "--schedule", "daily@02:30"])
    assert args.child_args == ["--", "--schedule", "daily@02:30"]


def test_parser_supervisor_flags() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "supervisor",
            "--backoff-base-seconds",
            "2.5",
            "--backoff-factor",
            "3.0",
            "--backoff-cap-seconds",
            "120",
            "--reset-after-uptime-seconds",
            "45",
            "--max-restarts",
            "7",
            "--shutdown-grace-seconds",
            "15",
        ]
    )
    assert args.backoff_base_seconds == 2.5
    assert args.backoff_factor == 3.0
    assert args.backoff_cap_seconds == 120.0
    assert args.reset_after_uptime_seconds == 45.0
    assert args.supervisor_max_restarts == 7
    assert args.shutdown_grace_seconds == 15.0


# ── main() dispatch ─────────────────────────────────────────────────────


def test_main_dispatch_threads_flags_into_cmd_supervisor() -> None:
    with mock.patch.object(cli, "cmd_supervisor", return_value=0) as supervise:
        rc = cli.main(
            [
                "supervisor",
                "--backoff-base-seconds",
                "2.0",
                "--max-restarts",
                "4",
                "--",
                "--once",
            ]
        )
    assert rc == 0
    _, kwargs = supervise.call_args
    assert kwargs["child_args"] == ["--", "--once"]
    assert kwargs["backoff_base_seconds"] == 2.0
    assert kwargs["max_restarts"] == 4
    assert kwargs["backoff_factor"] is None
    assert kwargs["lake_root"] is None


def test_main_dispatch_supervisor_defaults_all_none() -> None:
    with mock.patch.object(cli, "cmd_supervisor", return_value=0) as supervise:
        cli.main(["supervisor"])
    _, kwargs = supervise.call_args
    assert kwargs["child_args"] == []
    assert kwargs["backoff_base_seconds"] is None
    assert kwargs["backoff_factor"] is None
    assert kwargs["backoff_cap_seconds"] is None
    assert kwargs["reset_after_uptime_seconds"] is None
    assert kwargs["max_restarts"] is None
    assert kwargs["shutdown_grace_seconds"] is None
    assert kwargs["lake_root"] is None


# ── _build_supervisor_backoff (pure) ────────────────────────────────────


def test_build_backoff_all_none_uses_module_defaults() -> None:
    policy = cli._build_supervisor_backoff(
        backoff_base_seconds=None,
        backoff_factor=None,
        backoff_cap_seconds=None,
        reset_after_uptime_seconds=None,
        max_restarts=None,
    )
    assert policy.base_seconds == DEFAULT_BACKOFF_BASE_SECONDS
    assert policy.factor == DEFAULT_BACKOFF_FACTOR
    assert policy.cap_seconds == DEFAULT_BACKOFF_CAP_SECONDS
    assert policy.reset_after_uptime_seconds == DEFAULT_RESET_AFTER_UPTIME_SECONDS
    assert policy.max_restarts == DEFAULT_MAX_RESTARTS


def test_build_backoff_all_explicit_overrides_defaults() -> None:
    policy = cli._build_supervisor_backoff(
        backoff_base_seconds=2.0,
        backoff_factor=3.0,
        backoff_cap_seconds=99.0,
        reset_after_uptime_seconds=30.0,
        max_restarts=5,
    )
    assert policy.base_seconds == 2.0
    assert policy.factor == 3.0
    assert policy.cap_seconds == 99.0
    assert policy.reset_after_uptime_seconds == 30.0
    assert policy.max_restarts == 5


def test_build_backoff_propagates_validation_error() -> None:
    # cap < base is rejected by BackoffPolicy.__post_init__ — the CLI catches
    # this ValueError and prints a clean user error.
    with pytest.raises(ValueError, match="must be >= base_seconds"):
        cli._build_supervisor_backoff(
            backoff_base_seconds=10.0,
            backoff_factor=None,
            backoff_cap_seconds=5.0,
            reset_after_uptime_seconds=None,
            max_restarts=None,
        )


# ── _supervisor_child_argv (pure) ───────────────────────────────────────


def test_child_argv_strips_leading_double_dash() -> None:
    argv = cli._supervisor_child_argv(["--", "--schedule", "daily@02:30"])
    assert argv == [sys.executable, "-m", "brutex", "sweep-daemon", "--schedule", "daily@02:30"]


def test_child_argv_without_double_dash() -> None:
    argv = cli._supervisor_child_argv(["--once"])
    assert argv == [sys.executable, "-m", "brutex", "sweep-daemon", "--once"]


def test_child_argv_empty() -> None:
    argv = cli._supervisor_child_argv([])
    assert argv == [sys.executable, "-m", "brutex", "sweep-daemon"]


# ── cmd_supervisor production alerter binding (DEC-SUPERVISOR-CRASHLOOP-ALERT-001) ──


def test_cmd_supervisor_binds_sweep_alerter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The PRODUCTION binding pin (hostile-review MAJOR-2): ``cmd_supervisor``
    constructs :class:`ProcessSupervisor` with a REAL :class:`SweepAlerter` —
    deleting the ``alerter=`` argument would revert the whole crash-loop-alert
    feature to dormant with every other test still green (the exact
    dormant-wiring failure mode of MIGRATION.md #935 / #1014, whose fixes each
    added a dedicated wiring test like this one)."""
    from brutex.notifications.sweep_alerts import SweepAlerter
    from brutex.orchestrator import supervisor as sup_mod

    captured: dict[str, Any] = {}

    class _FakeSupervisor:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        def run(self) -> sup_mod.SupervisorResult:
            return sup_mod.SupervisorResult(sup_mod.SupervisorOutcome.CLEAN_EXIT, 0, 0)

    # cmd_supervisor imports these names locally from the supervisor module at
    # call time, so patching the SOURCE module intercepts the construction and
    # keeps the test free of real signal handlers.
    monkeypatch.setattr(sup_mod, "ProcessSupervisor", _FakeSupervisor)
    monkeypatch.setattr(sup_mod, "install_signal_stop", lambda: lambda: False)

    rc = cli.cmd_supervisor(
        child_args=[],
        backoff_base_seconds=None,
        backoff_factor=None,
        backoff_cap_seconds=None,
        reset_after_uptime_seconds=None,
        max_restarts=None,
        shutdown_grace_seconds=None,
        lake_root=tmp_path,
    )

    assert rc == cli.EXIT_OK
    # THE pin: the alerter is a real SweepAlerter (which also carries the
    # terminal supervisor_gave_up CRITICAL), not None / not forgotten.
    assert isinstance(captured["alerter"], SweepAlerter)
    # And the supervisor is otherwise assembled for production: an audit sink
    # plus the sweep-daemon subject the alerts name.
    assert captured["audit"] is not None
    assert captured["subject"] == "supervisor:sweep-daemon"


def test_cmd_supervisor_gave_up_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A GAVE_UP verdict surfaces as a loud non-zero exit (EXIT_INTERNAL_ERROR)."""
    from brutex.orchestrator import supervisor as sup_mod

    class _GaveUpSupervisor:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def run(self) -> sup_mod.SupervisorResult:
            return sup_mod.SupervisorResult(sup_mod.SupervisorOutcome.GAVE_UP, 3, 1)

    monkeypatch.setattr(sup_mod, "ProcessSupervisor", _GaveUpSupervisor)
    monkeypatch.setattr(sup_mod, "install_signal_stop", lambda: lambda: False)

    rc = cli.cmd_supervisor(
        child_args=[],
        backoff_base_seconds=None,
        backoff_factor=None,
        backoff_cap_seconds=None,
        reset_after_uptime_seconds=None,
        max_restarts=None,
        shutdown_grace_seconds=None,
        lake_root=tmp_path,
    )

    assert rc == cli.EXIT_INTERNAL_ERROR
