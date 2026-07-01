"""Tests for the one-click ``brutex run`` + ``brutex dashboard`` commands.

``cmd_run`` (DEC-ONECLICK-RUN-001) carries real logic — lake-root resolution,
the browser-open decision, and delegation to ``cmd_dashboard`` — and
``cmd_dashboard`` carries the [ui]-extra guard, the rotating-log stdout
mirror (DEC-RUN-LOG-CAPTURE-001), the instrument-store wiring, and the
uvicorn launch, so both are unit-tested here (offline: uvicorn.run and the
instrument-master auto-load are faked — no server bind, no network).
DEC-COV-CLI-HARNESS-001.
"""

from __future__ import annotations

import builtins
import importlib
import logging
import sys
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from brutex import cli


def test_run_resolves_explicit_lake_root_and_opens_browser() -> None:
    """--lake-root is honoured; browser opens to /universe; dashboard gets the lake."""
    with (
        mock.patch.object(cli, "cmd_dashboard", return_value=0) as dash,
        mock.patch.object(cli, "_open_browser") as opener,
    ):
        rc = cli.cmd_run(Path("/tmp/lake"), "127.0.0.1", 9080, open_browser=True)

    assert rc == 0
    # DEC-DASHBOARD-AUTOOPEN-001: cmd_run now opens the Universe Explorer
    # surface directly, not the bare dashboard root.
    opener.assert_called_once_with("http://127.0.0.1:9080/universe")
    dash.assert_called_once_with(lake_root=Path("/tmp/lake"), host="127.0.0.1", port=9080)


def test_run_no_browser_skips_opener() -> None:
    """--no-browser (open_browser=False) must not open a browser."""
    with (
        mock.patch.object(cli, "cmd_dashboard", return_value=0),
        mock.patch.object(cli, "_open_browser") as opener,
    ):
        cli.cmd_run(Path("/tmp/lake"), "0.0.0.0", 9090, open_browser=False)

    opener.assert_not_called()


def test_run_falls_back_to_env_lake_root() -> None:
    """When --lake-root is omitted, $BRUTEX_LAKE_ROOT is used."""
    with (
        mock.patch.object(cli, "cmd_dashboard", return_value=0) as dash,
        mock.patch.object(cli, "_open_browser"),
        mock.patch.dict("os.environ", {"BRUTEX_LAKE_ROOT": "/data/lake"}),
    ):
        cli.cmd_run(None, "127.0.0.1", 9080, open_browser=True)

    dash.assert_called_once_with(lake_root=Path("/data/lake"), host="127.0.0.1", port=9080)


def test_run_falls_back_to_home_default_when_no_env() -> None:
    """No --lake-root and no env → ~/.brutex/lake."""
    with (
        mock.patch.object(cli, "cmd_dashboard", return_value=0) as dash,
        mock.patch.object(cli, "_open_browser"),
        mock.patch.dict("os.environ", {}, clear=True),
        # cli.Path IS pathlib.Path (module re-use); patch the class directly so
        # the access stays explicit for mypy's no_implicit_reexport.
        mock.patch.object(Path, "home", return_value=Path("/home/op")),
    ):
        cli.cmd_run(None, "127.0.0.1", 9080, open_browser=False)

    dash.assert_called_once_with(
        lake_root=Path("/home/op/.brutex/lake"), host="127.0.0.1", port=9080
    )


def test_fetch_universe_flag_defaults_false() -> None:
    """``brutex fetch`` parses --universe (default False, store_true when present)."""
    parser = cli.build_parser()
    without = parser.parse_args(["fetch", "--dry-run"])
    with_flag = parser.parse_args(["fetch", "--dry-run", "--universe"])

    assert without.universe is False
    assert with_flag.universe is True


def test_main_dispatch_threads_universe_into_cmd_fetch() -> None:
    """``main`` forwards ``args.universe`` to ``cmd_fetch`` as the ``universe`` kwarg."""
    with mock.patch.object(cli, "cmd_fetch", return_value=0) as fetch:
        rc = cli.main(["fetch", "--dry-run", "--universe"])

    assert rc == 0
    _, kwargs = fetch.call_args
    assert kwargs["universe"] is True
    assert kwargs["dry_run"] is True


def test_main_dispatch_universe_defaults_false_without_flag() -> None:
    """Without --universe, ``main`` passes ``universe=False`` to ``cmd_fetch``."""
    with mock.patch.object(cli, "cmd_fetch", return_value=0) as fetch:
        cli.main(["fetch", "--dry-run"])

    _, kwargs = fetch.call_args
    assert kwargs["universe"] is False


def test_main_dispatch_run_threads_browser_flag() -> None:
    """``main`` maps --no-browser to ``open_browser=False`` on ``cmd_run``."""
    with mock.patch.object(cli, "cmd_run", return_value=0) as run:
        rc = cli.main(["run", "--no-browser", "--lake-root", "/tmp/lake"])
    assert rc == 0
    run.assert_called_once_with(
        lake_root=Path("/tmp/lake"), host="127.0.0.1", port=9080, open_browser=False
    )


def test_main_dispatch_dashboard_threads_defaults() -> None:
    """``main`` routes ``dashboard`` with the default host and parsed port."""
    with mock.patch.object(cli, "cmd_dashboard", return_value=0) as dash:
        rc = cli.main(["dashboard", "--port", "9191"])
    assert rc == 0
    dash.assert_called_once_with(lake_root=None, host="127.0.0.1", port=9191)


# ── cmd_dashboard (offline: uvicorn.run + auto-load faked) ──────────────


class _RecordingStream:
    """Stand-in for the real stdout the dashboard mirror wraps (TTY, fd 7)."""

    def __init__(self) -> None:
        self.chunks: list[str] = []
        self.flushes = 0

    def write(self, data: str) -> int:
        """Record the chunk and report its length like a real stream."""
        self.chunks.append(data)
        return len(data)

    def flush(self) -> None:
        """Count flushes so the mirror's flush proxy is observable."""
        self.flushes += 1

    def isatty(self) -> bool:
        """Pretend to be a TTY so the mirror's isatty proxy is observable."""
        return True

    def fileno(self) -> int:
        """Expose a fixed fd so the mirror's fileno proxy is observable."""
        return 7


def _stub_dashboard_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    probes: dict[str, Any],
    *,
    probe_stream: bool = True,
) -> None:
    """Stub logging config + instrument auto-load + uvicorn.run (offline).

    With ``probe_stream`` the fake server also exercises the stdout mirror's
    proxy methods (isatty/fileno/flush + a blank-line write) — only valid when
    the test installed its own stream, since pytest's capture has no fd.
    """
    obs_logging = importlib.import_module("brutex.observability.logging")
    monkeypatch.setattr(obs_logging, "configure_logging", lambda **kwargs: tmp_path / "brutex.log")
    auto_pull = importlib.import_module("brutex.dashboard.auto_pull")
    monkeypatch.setattr(auto_pull, "should_reload_instrument_master", lambda **kwargs: False)
    uvicorn_mod = importlib.import_module("uvicorn")

    def fake_run(app: Any, *, host: str, port: int, log_config: Any) -> None:
        """Record the bind and probe the stdout proxies in place of a server."""
        probes["host"] = host
        probes["port"] = port
        probes["log_config"] = log_config
        if probe_stream:
            probes["isatty"] = sys.stdout.isatty()
            probes["fileno"] = sys.stdout.fileno()
            sys.stdout.write("\n")  # blank line → the mirror's strip()-False arm
            sys.stdout.flush()

    monkeypatch.setattr(uvicorn_mod, "run", fake_run)
    # The instrument autoload now runs in a background daemon thread
    # (DEC-DASHBOARD-INSTANT-LAUNCH-001). For deterministic, OFFLINE unit tests,
    # run it SYNCHRONOUSLY in-process instead of spawning the thread — the
    # network-bound ``load_instrument_master`` is never reached here because
    # ``should_reload_instrument_master`` is stubbed above (False / raising).
    monkeypatch.setattr(cli, "_start_instrument_autoload", cli._autoload_instrument_master)
    # HERMETIC (DEC-AUTO-RESUME-ON-BOOT-001): stub the boot-time auto-resume launcher
    # to a no-op so the dashboard-boot tests never read the real ``BRUTEX_AUTO_RESUME_ON_BOOT``
    # env flag NOR spawn a daemon thread / subprocess — the launcher's own behaviour is
    # tested in isolation (test_cli_auto_resume + test_orchestrator_auto_resume). Mirrors the
    # ``_start_instrument_autoload`` stub above (both are live-side-effect boot seams).
    monkeypatch.setattr(cli, "_start_auto_resume_on_boot", lambda *args, **kwargs: None)


def test_dashboard_missing_uvicorn_is_user_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without the [ui] extra the dashboard prints the pip hint and exits 1."""
    real_import = builtins.__import__

    def deny_uvicorn(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "uvicorn":
            raise ImportError("No module named 'uvicorn'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", deny_uvicorn)
    rc = cli.cmd_dashboard(None, "127.0.0.1", 9080)
    assert rc == cli.EXIT_USER_ERROR
    err = capsys.readouterr().err
    assert '[brutex] dashboard requires fastapi+uvicorn — run: pip install -e ".[ui]"' in err


def test_dashboard_with_lake_mirrors_stdout_and_serves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file-handler session installs the stdout mirror and serves via uvicorn.

    Covers the DEC-RUN-LOG-CAPTURE-001 tee: the mirror proxies write/flush/
    isatty/fileno to the real stream, the lake branch opens a real (empty)
    InstrumentStore, and uvicorn.run receives log_config=None.
    """
    pytest.importorskip("fastapi", reason="dashboard needs the [ui] extra")
    pytest.importorskip("uvicorn", reason="dashboard needs the [ui] extra")
    pytest.importorskip("duckdb", reason="the instrument store needs duckdb")
    probes: dict[str, Any] = {}
    _stub_dashboard_runtime(monkeypatch, tmp_path, probes)

    marker = logging.NullHandler()
    setattr(marker, "_brutex_file_handler", True)  # noqa: B010 — runtime marker attr
    root = logging.getLogger()
    root.addHandler(marker)
    stream = _RecordingStream()
    monkeypatch.setattr(sys, "stdout", stream)
    try:
        rc = cli.cmd_dashboard(tmp_path, "127.0.0.1", 9080)
    finally:
        root.removeHandler(marker)

    assert rc == cli.EXIT_OK
    text = "".join(stream.chunks)
    assert "[brutex] dashboard → http://127.0.0.1:9080  (Ctrl-C to stop)" in text
    assert f"[brutex] logs → {tmp_path / 'brutex.log'}" in text
    assert probes["host"] == "127.0.0.1"
    assert probes["port"] == 9080
    assert probes["log_config"] is None
    assert probes["isatty"] is True
    assert probes["fileno"] == 7
    assert stream.flushes >= 1
    assert (tmp_path / "instruments.duckdb").exists()


def test_dashboard_autoload_failure_never_blocks_serving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failing instrument-master auto-load is swallowed; the dashboard serves.

    DEC-INDEX-PARSE-FIX-001: the startup auto-load is best-effort — a network
    failure must NOT stop the dashboard, so the broad except logs a warning
    and the launch continues to ``uvicorn.run``.
    """
    pytest.importorskip("fastapi", reason="dashboard needs the [ui] extra")
    pytest.importorskip("uvicorn", reason="dashboard needs the [ui] extra")
    pytest.importorskip("duckdb", reason="the instrument store needs duckdb")
    probes: dict[str, Any] = {}
    _stub_dashboard_runtime(monkeypatch, tmp_path, probes, probe_stream=False)

    def boom(**kwargs: Any) -> bool:
        raise RuntimeError("offline: no public CSV reachable")

    auto_pull = importlib.import_module("brutex.dashboard.auto_pull")
    monkeypatch.setattr(auto_pull, "should_reload_instrument_master", boom)

    rc = cli.cmd_dashboard(tmp_path, "127.0.0.1", 9082)
    assert rc == cli.EXIT_OK
    assert probes["port"] == 9082
    out = capsys.readouterr().out
    assert "[brutex] dashboard → http://127.0.0.1:9082  (Ctrl-C to stop)" in out


def test_dashboard_auto_resume_launch_failure_never_blocks_serving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failing boot-time auto-resume LAUNCH is swallowed; the dashboard still serves.

    DEC-AUTO-RESUME-ON-BOOT-001 (hostile-review NIT): the launch of the boot-time
    auto-resume is best-effort on the bind path — even a thread-spawn failure must
    NOT stop the dashboard, so the guard logs a warning and the launch continues to
    ``uvicorn.run`` (the "never crashes boot" contract).
    """
    pytest.importorskip("fastapi", reason="dashboard needs the [ui] extra")
    pytest.importorskip("uvicorn", reason="dashboard needs the [ui] extra")
    pytest.importorskip("duckdb", reason="the instrument store needs duckdb")
    probes: dict[str, Any] = {}
    _stub_dashboard_runtime(monkeypatch, tmp_path, probes, probe_stream=False)

    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("boot resume launch failed")

    # Override the harness no-op stub with a RAISING launcher to exercise the guard.
    monkeypatch.setattr(cli, "_start_auto_resume_on_boot", boom)

    rc = cli.cmd_dashboard(tmp_path, "127.0.0.1", 9083)
    assert rc == cli.EXIT_OK
    assert probes["port"] == 9083
    out = capsys.readouterr().out
    assert "[brutex] dashboard → http://127.0.0.1:9083  (Ctrl-C to stop)" in out


def test_dashboard_without_file_handler_keeps_plain_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No rotating-file handler → stdout is NOT wrapped; no-lake branch serves."""
    pytest.importorskip("fastapi", reason="dashboard needs the [ui] extra")
    pytest.importorskip("uvicorn", reason="dashboard needs the [ui] extra")
    probes: dict[str, Any] = {}
    _stub_dashboard_runtime(monkeypatch, tmp_path, probes, probe_stream=False)
    monkeypatch.setenv("BRUTEX_LAKE_ROOT", str(tmp_path))

    root = logging.getLogger()
    marked = [h for h in root.handlers if getattr(h, "_brutex_file_handler", False)]
    for handler in marked:
        root.removeHandler(handler)
    before = sys.stdout
    try:
        rc = cli.cmd_dashboard(None, "0.0.0.0", 9095)
        assert sys.stdout is before
    finally:
        for handler in marked:
            root.addHandler(handler)

    assert rc == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "[brutex] dashboard → http://0.0.0.0:9095  (Ctrl-C to stop)" in out
    assert probes["port"] == 9095
    assert not (tmp_path / "instruments.duckdb").exists()


def test_dashboard_autoload_runs_off_the_bind_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The instrument autoload must NOT block the uvicorn port bind.

    DEC-DASHBOARD-INSTANT-LAUNCH-001: a cold start can spend ~90 s in the
    niftyindices-blocked NTM constituent fetch. That work now runs in a
    background daemon thread, so ``uvicorn.run`` (the port bind) is reached
    BEFORE the autoload finishes — proving the launch is instant. Here the
    autoload is stubbed to BLOCK until the bind releases it; the real
    ``_start_instrument_autoload`` thread is exercised (not stubbed).
    """
    import threading

    pytest.importorskip("fastapi", reason="dashboard needs the [ui] extra")
    pytest.importorskip("uvicorn", reason="dashboard needs the [ui] extra")
    pytest.importorskip("duckdb", reason="the instrument store needs duckdb")

    obs_logging = importlib.import_module("brutex.observability.logging")
    monkeypatch.setattr(obs_logging, "configure_logging", lambda **kwargs: tmp_path / "brutex.log")

    autoload_started = threading.Event()
    release = threading.Event()
    probes: dict[str, Any] = {}

    def blocking_autoload(store: Any, log: Any) -> None:
        """Stand in for the slow niftyindices-bound autoload: block until released."""
        autoload_started.set()
        release.wait(timeout=5)

    # NOTE: _start_instrument_autoload is deliberately NOT stubbed — the real
    # daemon-thread starter runs, targeting this blocking stub.
    monkeypatch.setattr(cli, "_autoload_instrument_master", blocking_autoload)

    uvicorn_mod = importlib.import_module("uvicorn")

    def fake_run(app: Any, *, host: str, port: int, log_config: Any) -> None:
        """The port bind: record that the autoload was STILL blocked at this point."""
        probes["port"] = port
        probes["autoload_blocked_at_bind"] = not release.is_set()
        release.set()  # let the daemon thread unblock + finish

    monkeypatch.setattr(uvicorn_mod, "run", fake_run)

    rc = cli.cmd_dashboard(tmp_path, "127.0.0.1", 9083)

    assert rc == cli.EXIT_OK
    assert probes["port"] == 9083
    # The bind was reached while the (threaded) autoload had not yet completed —
    # i.e. the autoload did not block the port from binding.
    assert probes["autoload_blocked_at_bind"] is True
    # The daemon thread really did start (proving _start_instrument_autoload ran).
    assert autoload_started.wait(timeout=5)
