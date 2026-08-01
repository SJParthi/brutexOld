# pyright: reportPrivateUsage=none
"""Coverage backfill for the orphan ``brutex.cli`` commands (DEC-COV-CLI-HARNESS-001).

Closes the line + branch gaps on the CLI surfaces that have no dedicated
command-family test file — ``version`` / ``status`` / ``catalog reconcile`` /
``doctor`` / ``monitor``, the pure paisa-list parser, the ``main`` dispatcher's
threading for every remaining subcommand (mocked handlers — no network, no Dhan
credentials), and the defensive ``parser.error`` fallbacks that real argv can
never reach. Exit codes and operator-facing messages are byte-pinned where
stable (house mutation-gate discipline).
"""

from __future__ import annotations

import argparse
import importlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest
from brutex import __version__, cli

pytestmark = pytest.mark.usefixtures("_inert_sweep_logging_guard")  # see tests/conftest.py


# ── version ─────────────────────────────────────────────────────────────


def test_cmd_version_prints_pinned_version(capsys: pytest.CaptureFixture[str]) -> None:
    """``cmd_version`` prints exactly ``brutex <version>`` and exits 0."""
    rc = cli.cmd_version()
    assert rc == cli.EXIT_OK
    assert capsys.readouterr().out == f"brutex {__version__}\n"


def test_main_bare_invocation_prints_version(capsys: pytest.CaptureFixture[str]) -> None:
    """``brutex`` with no subcommand falls back to the version print."""
    rc = cli.main([])
    assert rc == cli.EXIT_OK
    assert capsys.readouterr().out == f"brutex {__version__}\n"


def test_main_version_subcommand_prints_version(capsys: pytest.CaptureFixture[str]) -> None:
    """``brutex version`` prints the same pinned line as the bare invocation."""
    rc = cli.main(["version"])
    assert rc == cli.EXIT_OK
    assert capsys.readouterr().out == f"brutex {__version__}\n"


# ── status ──────────────────────────────────────────────────────────────


def _runtime_stub() -> SimpleNamespace:
    """A minimal ``RuntimeInfo``-shaped object for ``cmd_status`` printing."""
    return SimpleNamespace(
        pid=4242,
        run_id="run-a",
        started_at="2026-06-12T00:00:00+00:00",
        phase="ingest",
    )


def test_cmd_status_without_runtime_file(capsys: pytest.CaptureFixture[str]) -> None:
    """No runtime file → the calm 'no pipeline running' line, exit 0."""
    with mock.patch.object(cli, "read_runtime_file", return_value=None):
        rc = cli.cmd_status(run_id=None)
    assert rc == cli.EXIT_OK
    assert capsys.readouterr().out == "[brutex] no runtime file (no pipeline running)\n"


def test_cmd_status_run_id_mismatch(capsys: pytest.CaptureFixture[str]) -> None:
    """A --run-id that differs from the live run prints the no-active-run line."""
    with mock.patch.object(cli, "read_runtime_file", return_value=_runtime_stub()):
        rc = cli.cmd_status(run_id="run-b")
    assert rc == cli.EXIT_OK
    assert capsys.readouterr().out == "[brutex] no active run for run_id=run-b\n"


def test_cmd_status_prints_full_snapshot(capsys: pytest.CaptureFixture[str]) -> None:
    """Without a --run-id filter the four runtime fields print byte-exactly."""
    with mock.patch.object(cli, "read_runtime_file", return_value=_runtime_stub()):
        rc = cli.cmd_status(run_id=None)
    assert rc == cli.EXIT_OK
    assert capsys.readouterr().out == (
        "pid       : 4242\n"
        "run_id    : run-a\n"
        "started   : 2026-06-12T00:00:00+00:00\n"
        "phase     : ingest\n"
    )


def test_cmd_status_matching_run_id_prints_snapshot(capsys: pytest.CaptureFixture[str]) -> None:
    """A --run-id equal to the live run's id is NOT filtered out."""
    with mock.patch.object(cli, "read_runtime_file", return_value=_runtime_stub()):
        rc = cli.cmd_status(run_id="run-a")
    assert rc == cli.EXIT_OK
    assert "phase     : ingest" in capsys.readouterr().out


def test_main_dispatch_status_threads_run_id() -> None:
    """``main`` forwards ``--run-id`` (and its None default) to ``cmd_status``."""
    with mock.patch.object(cli, "cmd_status", return_value=0) as status:
        assert cli.main(["status"]) == 0
        assert cli.main(["status", "--run-id", "r-1"]) == 0
    assert status.call_args_list == [
        mock.call(run_id=None),
        mock.call(run_id="r-1"),
    ]


# ── catalog reconcile ───────────────────────────────────────────────────


def test_cmd_catalog_reconcile_resolves_env_lake_root(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no --lake-root the env fallback is used; an empty lake reconciles to zeros."""
    pytest.importorskip("duckdb")
    monkeypatch.setenv("BRUTEX_LAKE_ROOT", str(tmp_path))
    rc = cli.cmd_catalog_reconcile(None)
    assert rc == cli.EXIT_OK
    assert capsys.readouterr().out == (
        f"[brutex] catalog reconcile — partitions=0 bars=0 skipped=0 (lake={tmp_path})\n"
    )
    assert (tmp_path / "catalog.duckdb").exists()


def test_main_dispatch_catalog_reconcile_threads_lake_root(tmp_path: Path) -> None:
    """``main`` routes ``catalog reconcile`` with the parsed --lake-root Path."""
    with mock.patch.object(cli, "cmd_catalog_reconcile", return_value=0) as reconcile:
        rc = cli.main(["catalog", "reconcile", "--lake-root", str(tmp_path)])
    assert rc == 0
    reconcile.assert_called_once_with(lake_root=tmp_path)


# ── doctor ──────────────────────────────────────────────────────────────


def test_cmd_doctor_defaults_audit_dir_and_runtime_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """None audit-dir/runtime-file fall back to the doctor module defaults."""
    doctor_mod = importlib.import_module("brutex.doctor")
    seen: dict[str, Any] = {}

    def fake_main(**kwargs: Any) -> int:
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(doctor_mod, "main", fake_main)
    rc = cli.cmd_doctor(None, None, None)
    assert rc == 0
    assert seen == {
        "audit_file": None,
        "audit_dir": doctor_mod.DEFAULT_AUDIT_DIR,
        "runtime_file": doctor_mod.DEFAULT_RUNTIME_FILE,
        "live_subjects_file": None,
        "replay_action_filter": "add",
    }


def test_cmd_doctor_threads_explicit_paths_and_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Explicit paths pass through unchanged and the doctor's exit code is returned."""
    doctor_mod = importlib.import_module("brutex.doctor")
    seen: dict[str, Any] = {}

    def fake_main(**kwargs: Any) -> int:
        seen.update(kwargs)
        return 5

    monkeypatch.setattr(doctor_mod, "main", fake_main)
    rc = cli.cmd_doctor(
        tmp_path / "a.jsonl",
        tmp_path / "audit",
        tmp_path / "runtime.json",
        live_subjects_file=tmp_path / "subjects.txt",
        replay_action_filter="del",
    )
    assert rc == 5
    assert seen == {
        "audit_file": tmp_path / "a.jsonl",
        "audit_dir": tmp_path / "audit",
        "runtime_file": tmp_path / "runtime.json",
        "live_subjects_file": tmp_path / "subjects.txt",
        "replay_action_filter": "del",
    }


def test_main_dispatch_doctor_threads_defaults() -> None:
    """``main`` forwards the doctor flags (all-default invocation)."""
    with mock.patch.object(cli, "cmd_doctor", return_value=0) as doctor:
        rc = cli.main(["doctor"])
    assert rc == 0
    doctor.assert_called_once_with(
        audit_file=None,
        audit_dir=None,
        runtime_file=None,
        live_subjects_file=None,
        replay_action_filter="add",
    )


# ── monitor ─────────────────────────────────────────────────────────────


class _BoundedLoop:
    """Fake monitor loop counting bounded ``tick_once`` calls."""

    def __init__(self) -> None:
        self.ticks = 0

    def tick_once(self) -> None:
        """Record one tick."""
        self.ticks += 1


class _FakeThread:
    """Fake daemon thread whose ``join`` optionally raises Ctrl-C."""

    def __init__(self, *, interrupt: bool) -> None:
        self._interrupt = interrupt
        self.joins = 0

    def join(self, timeout: float) -> None:
        """Record the join; raise ``KeyboardInterrupt`` when scripted to."""
        del timeout
        self.joins += 1
        if self._interrupt:
            raise KeyboardInterrupt


class _ForegroundLoop:
    """Fake monitor loop for the unbounded (ticks == 0) foreground path."""

    def __init__(self, running: list[bool], *, interrupt: bool) -> None:
        self._running = iter(running)
        self._thread = _FakeThread(interrupt=interrupt)
        self.started = False
        self.stopped = False

    def start(self) -> None:
        """Record the loop start."""
        self.started = True

    def stop(self) -> None:
        """Record the loop stop (the ``finally`` guarantee)."""
        self.stopped = True

    @property
    def is_running(self) -> bool:
        """Pop the next scripted liveness state (False once exhausted)."""
        return next(self._running, False)


def _patch_compose(monkeypatch: pytest.MonkeyPatch, loop: object) -> list[Any]:
    """Patch ``compose_standard_monitor`` to return *loop*; capture the config."""
    std = importlib.import_module("brutex.observability.standard_monitor")
    captured: list[Any] = []

    def fake_compose(cfg: Any) -> object:
        captured.append(cfg)
        return loop

    monkeypatch.setattr(std, "compose_standard_monitor", fake_compose)
    return captured


def test_cmd_monitor_bounded_ticks(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--ticks N`` runs exactly N ticks then exits 0 (no start/stop)."""
    loop = _BoundedLoop()
    captured = _patch_compose(monkeypatch, loop)
    rc = cli.cmd_monitor(poll_interval=0.25, ticks=3)
    assert rc == 0
    assert loop.ticks == 3
    assert captured[0].poll_interval_seconds == 0.25


def test_cmd_monitor_unbounded_stops_on_interrupt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ctrl-C during the foreground join breaks the wait and stops the loop."""
    loop = _ForegroundLoop([True], interrupt=True)
    _patch_compose(monkeypatch, loop)
    rc = cli.cmd_monitor(poll_interval=1.0, ticks=0)
    assert rc == 0
    assert loop.started
    assert loop.stopped
    assert loop._thread.joins == 1


def test_cmd_monitor_unbounded_exits_when_loop_dies(monkeypatch: pytest.MonkeyPatch) -> None:
    """A loop that reports not-running ends the wait normally; stop still runs."""
    loop = _ForegroundLoop([True, False], interrupt=False)
    _patch_compose(monkeypatch, loop)
    rc = cli.cmd_monitor(poll_interval=1.0, ticks=0)
    assert rc == 0
    assert loop.stopped
    assert loop._thread.joins == 1


def test_main_dispatch_monitor_threads_flags() -> None:
    """``main`` forwards --poll-interval/--ticks to ``cmd_monitor``."""
    with mock.patch.object(cli, "cmd_monitor", return_value=0) as monitor:
        rc = cli.main(["monitor", "--ticks", "4", "--poll-interval", "0.5"])
    assert rc == 0
    monitor.assert_called_once_with(poll_interval=0.5, ticks=4)


# ── paisa-list parser ───────────────────────────────────────────────────


def test_parse_paisa_list_mixed_none_and_values() -> None:
    """``none`` (any case) maps to None; integers parse; order is kept."""
    assert cli._parse_paisa_list("none,100,NONE,250") == [None, 100, None, 250]


def test_parse_paisa_list_skips_blank_tokens() -> None:
    """Blank/whitespace tokens are dropped, not parsed."""
    assert cli._parse_paisa_list(" , ,75,") == [75]


def test_parse_paisa_list_empty_string() -> None:
    """An empty string yields an empty list."""
    assert cli._parse_paisa_list("") == []


# ── main dispatch threading (mocked handlers) ───────────────────────────


def test_main_dispatch_wipe_default_and_flags() -> None:
    """``wipe`` threads --execute/--yes (store_true) into ``cmd_wipe``."""
    with mock.patch.object(cli, "cmd_wipe", return_value=0) as wipe:
        assert cli.main(["wipe"]) == 0
        assert cli.main(["wipe", "--execute", "--yes"]) == 0
    assert wipe.call_args_list == [
        mock.call(execute=False, assume_yes=False),
        mock.call(execute=True, assume_yes=True),
    ]


def test_main_dispatch_sweep_all_threads_grid_defaults() -> None:
    """``sweep-all`` threads the shared grid defaults into ``cmd_sweep_all``."""
    with mock.patch.object(cli, "cmd_sweep_all", return_value=7) as sweep_all:
        rc = cli.main(["sweep-all", "--start", "2024-01-01", "--end", "2024-01-02"])
    assert rc == 7
    _, kwargs = sweep_all.call_args
    assert kwargs["symbols"] == "NIFTY,BANKNIFTY"
    # DEC-SWEEP-TF-SCOPE-5-001: threading pin — the scope VALUE is pinned once, in
    # ``test_sweep_timeframe_scope.py``. Referencing the constant here keeps this
    # test about "the CLI threads its default through", never about the scope.
    from brutex.data.timeframes import DEFAULT_SWEEP_INTERVALS

    assert kwargs["intervals"] == DEFAULT_SWEEP_INTERVALS
    assert kwargs["start"] == "2024-01-01"
    assert kwargs["end"] == "2024-01-02"
    # LANE W1 (DEC-RANK-QUOTA-PERSIST-1000-001): the persist default is the
    # ONE minted constant — 1000, no longer the fast-operator 20.
    from brutex.ranking.multi_select import DEFAULT_PERSIST_TOP_K

    assert kwargs["top"] == DEFAULT_PERSIST_TOP_K == 1000
    assert kwargs["mode"] == "A"
    assert kwargs["options_sim"] is True
    assert kwargs["stage3_enable"] is False
    assert kwargs["allow_missing_slices"] is False


def test_main_dispatch_sweep_daemon_threads_schedule() -> None:
    """``sweep-daemon`` threads the cadence + --once into ``cmd_sweep_daemon``."""
    with mock.patch.object(cli, "cmd_sweep_daemon", return_value=0) as daemon:
        rc = cli.main(["sweep-daemon", "--once", "--start", "2024-01-01", "--end", "2024-01-02"])
    assert rc == 0
    _, kwargs = daemon.call_args
    assert kwargs["schedule"] == "daily@02:30"
    assert kwargs["once"] is True
    assert kwargs["max_cycles"] is None
    assert kwargs["tz_offset_minutes"] is None
    assert kwargs["symbols"] == "NIFTY,BANKNIFTY"


def test_main_dispatch_options_sim_threads_run_id(tmp_path: Path) -> None:
    """``options-sim`` threads the positional run-id + expiry/broker defaults."""
    with mock.patch.object(cli, "cmd_options_sim", return_value=0) as sim:
        rc = cli.main(
            [
                "options-sim",
                "deadbeef",
                "--results-root",
                str(tmp_path),
                "--start",
                "2024-01-01",
                "--end",
                "2024-01-02",
            ]
        )
    assert rc == 0
    _, kwargs = sim.call_args
    assert kwargs["run_id"] == "deadbeef"
    assert kwargs["results_root"] == tmp_path
    assert kwargs["expiry"] == "weekly"
    assert kwargs["broker"] == "groww"
    assert kwargs["segment"] == "IDX_I"


def test_main_dispatch_cascade_threads_optional_run_id(tmp_path: Path) -> None:
    """``cascade`` threads the optional run-id (None when omitted)."""
    with mock.patch.object(cli, "cmd_cascade", return_value=0) as cascade:
        assert cli.main(["cascade"]) == 0
        assert cli.main(["cascade", "RUN123", "--results-root", str(tmp_path)]) == 0
    first_kwargs = cascade.call_args_list[0].kwargs
    second_kwargs = cascade.call_args_list[1].kwargs
    assert first_kwargs["run_id"] is None
    assert first_kwargs["results_root"] is None
    assert second_kwargs["run_id"] == "RUN123"
    assert second_kwargs["results_root"] == tmp_path


def test_main_dispatch_verify_lookahead_threads_slice() -> None:
    """``verify-lookahead`` threads the slice coordinates + interval default."""
    with mock.patch.object(cli, "cmd_verify_lookahead", return_value=0) as verify:
        rc = cli.main(
            [
                "verify-lookahead",
                "--symbol",
                "NIFTY",
                "--start",
                "2024-01-01",
                "--end",
                "2024-01-02",
            ]
        )
    assert rc == 0
    _, kwargs = verify.call_args
    assert kwargs["symbol"] == "NIFTY"
    assert kwargs["interval"] == "1minute"
    assert kwargs["run_id"] is None
    assert kwargs["exchange"] == "NSE"
    assert kwargs["segment"] == "INDEX"


def test_main_dispatch_sweep_verify_threads_recount_flags() -> None:
    """``sweep-verify`` threads --recount/--emit-heal-plan into the doctor shim."""
    with mock.patch.object(cli, "cmd_sweep_verify", return_value=cli.EXIT_VERIFY_FINDINGS) as ver:
        rc = cli.main(
            [
                "sweep-verify",
                "--start",
                "2024-01-01",
                "--end",
                "2024-01-02",
                "--recount",
                "--emit-heal-plan",
            ]
        )
    assert rc == cli.EXIT_VERIFY_FINDINGS
    _, kwargs = ver.call_args
    assert kwargs["recount"] is True
    assert kwargs["emit_heal_plan"] is True
    assert kwargs["allow_context_mismatch"] is False
    assert kwargs["indicator_version"] is None
    assert kwargs["code_commit_sha"] is None


def test_main_dispatch_sweep_heal_threads_plan(tmp_path: Path) -> None:
    """``sweep-heal`` threads the plan path + quarantine flags."""
    plan = tmp_path / "heal-plan.json"
    with mock.patch.object(cli, "cmd_sweep_heal", return_value=cli.EXIT_HEAL_REFUSED) as heal:
        rc = cli.main(["sweep-heal", str(plan), "--execute", "--allow-mass-quarantine"])
    assert rc == cli.EXIT_HEAL_REFUSED
    heal.assert_called_once_with(
        plan=plan,
        lake_root=None,
        results_root=None,
        execute=True,
        allow_mass_quarantine=True,
    )


# ── defensive parser.error fallbacks (unreachable via real argv) ────────


class _ScriptedParser:
    """argparse stand-in steering ``main`` into its defensive error branches.

    Real argv can never produce an unknown sub/command (argparse rejects it at
    parse time), so these arms are reachable only by scripting ``parse_args``.
    """

    def __init__(self, namespace: argparse.Namespace, *, raise_on_error: bool) -> None:
        self._namespace = namespace
        self._raise_on_error = raise_on_error
        self.errors: list[str] = []

    def parse_args(self, argv: list[str] | None = None) -> argparse.Namespace:
        """Return the scripted namespace regardless of *argv*."""
        del argv
        return self._namespace

    def error(self, message: str) -> None:
        """Record *message*; optionally exit(2) exactly like real argparse."""
        self.errors.append(message)
        if self._raise_on_error:
            raise SystemExit(2)


def test_main_unknown_audit_subcommand_exits_via_parser_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An audit subcommand other than ``verify`` routes to ``parser.error``."""
    parser = _ScriptedParser(
        argparse.Namespace(command="audit", audit_command="nope"), raise_on_error=True
    )
    monkeypatch.setattr(cli, "build_parser", lambda: parser)
    with pytest.raises(SystemExit) as excinfo:
        cli.main([])
    assert excinfo.value.code == 2
    assert parser.errors == ["unknown audit subcommand: nope"]


def test_main_unknown_catalog_subcommand_exits_via_parser_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A catalog subcommand other than ``reconcile`` routes to ``parser.error``."""
    parser = _ScriptedParser(
        argparse.Namespace(command="catalog", catalog_command="nope"), raise_on_error=True
    )
    monkeypatch.setattr(cli, "build_parser", lambda: parser)
    with pytest.raises(SystemExit) as excinfo:
        cli.main([])
    assert excinfo.value.code == 2
    assert parser.errors == ["unknown catalog subcommand: nope"]


def test_main_unknown_command_falls_back_to_user_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The post-``parser.error`` fallback returns EXIT_USER_ERROR.

    ``argparse.ArgumentParser.error`` normally raises ``SystemExit``; the CLI
    keeps a defensive ``return EXIT_USER_ERROR`` after it for error
    implementations that do not exit. A ``results`` namespace whose
    sub-command is not ``show`` walks the WHOLE dispatch chain (covering every
    command's miss arm) before landing there.
    """
    parser = _ScriptedParser(
        argparse.Namespace(command="results", results_command="nope"), raise_on_error=False
    )
    monkeypatch.setattr(cli, "build_parser", lambda: parser)
    rc = cli.main([])
    assert rc == cli.EXIT_USER_ERROR
    assert parser.errors == ["unknown command: results"]


# ── sweep-all "Resilient: skip + continue" verdict ───────────────────────


def test_sweep_all_exit_code_clean_run_is_ok() -> None:
    """A fully clean grid (no failed tuples) exits EXIT_OK."""
    assert cli.sweep_all_exit_code(0) == cli.EXIT_OK


@pytest.mark.parametrize("failed", [1, 3, 42])
def test_sweep_all_exit_code_failures_are_non_zero(failed: int) -> None:
    """Any skipped tuple makes the verdict NON-ZERO (Golden Rule #10 — no silent skip)."""
    assert cli.sweep_all_exit_code(failed) == cli.EXIT_SWEEP_TUPLE_FAILED
    assert cli.EXIT_SWEEP_TUPLE_FAILED != cli.EXIT_OK


@pytest.mark.parametrize("total", [1, 4, 100])
def test_sweep_all_exit_code_all_tuples_failed_is_non_zero(total: int) -> None:
    """The all-fail grid (tuples_failed == total > 0) exits NON-ZERO, never EXIT_OK.

    The whole-grid failure is the harshest skip case; the verdict still surfaces
    it (no silent green illusion) — the CLI counterpart of the engine all-fail
    hardening tests.
    """
    assert total > 0
    assert cli.sweep_all_exit_code(total) == cli.EXIT_SWEEP_TUPLE_FAILED
    assert cli.sweep_all_exit_code(total) != cli.EXIT_OK


# ── sweep-verify verdict (partial shadow re-sweep → INCONCLUSIVE) ─────────


def test_sweep_verify_exit_code_findings_dominate() -> None:
    """Corruption findings dominate even when the run is also inconclusive."""
    assert cli.sweep_verify_exit_code(has_findings=True, inconclusive=False) == (
        cli.EXIT_VERIFY_FINDINGS
    )
    assert cli.sweep_verify_exit_code(has_findings=True, inconclusive=True) == (
        cli.EXIT_VERIFY_FINDINGS
    )


def test_sweep_verify_exit_code_inconclusive_when_no_findings() -> None:
    """No findings but inconclusive (e.g. a PARTIAL shadow re-sweep that skipped a tuple).

    DEC-ENGINE-TUPLE-SKIP-RESILIENT-001: ``run_sweep_grid`` now skips a raising
    tuple, so a recount with ``tuples_failed > 0`` is a PARTIAL re-sweep — the
    byte-diff cannot be trusted, so the verdict downgrades to INCONCLUSIVE rather
    than emitting a pass/fail over an incomplete sweep.
    """
    assert cli.sweep_verify_exit_code(has_findings=False, inconclusive=True) == (
        cli.EXIT_VERIFY_INCONCLUSIVE
    )
    assert cli.EXIT_VERIFY_INCONCLUSIVE != cli.EXIT_OK


def test_sweep_verify_exit_code_clean_run_is_ok() -> None:
    """No findings + conclusive → a clean OK verdict."""
    assert cli.sweep_verify_exit_code(has_findings=False, inconclusive=False) == cli.EXIT_OK
