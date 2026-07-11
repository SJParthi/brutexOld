"""``brutex/dashboard/rust_front.py`` — the default-OFF Rust-front seam.

Covers the module 100% line+branch standalone (the self-contained gate the
PR ships) plus the thin ``cmd_dashboard`` wiring:

* flag law: absent/empty/off → OFF; ``on`` → ON; junk → loud refusal
  (``RustFrontError`` in the module, ``CliUserError`` exit-1 at the CLI);
* the FLAG-OFF IDENTITY PIN: with the flag absent, ``cmd_dashboard`` runs
  uvicorn on the operator host:port exactly as before (no brutexd spawn,
  no private port) — byte-zero behaviour change;
* binary lookup ladder (env override → repo target dirs → PATH → refusal);
* plan validation (loopback-only host law, static-dir presence);
* the supervised child lifecycle (terminate → wait → kill escalation;
  early-exit observation; teardown on a raising uvicorn).
"""

from __future__ import annotations

import importlib
import subprocess
from pathlib import Path
from typing import Any

import pytest
from brutex import cli
from brutex.dashboard import rust_front
from brutex.dashboard.rust_front import (
    ENV_BRUTEXD_BIN,
    ENV_RUST_FRONT,
    RustFrontError,
    RustFrontPlan,
    allocate_upstream_port,
    build_command,
    build_plan,
    default_static_dir,
    locate_brutexd,
    resolve_rust_front,
    run_with_rust_front,
)

# ── resolve_rust_front — the fail-loud flag law ──────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, False),
        ("", False),
        ("   ", False),
        ("off", False),
        ("OFF", False),
        (" Off ", False),
        ("on", True),
        ("ON", True),
        (" On ", True),
    ],
)
def test_resolve_flag_states(raw: str | None, expected: bool) -> None:
    """Absent/blank/off → OFF; on → ON (case/whitespace-insensitive)."""
    env: dict[str, str] = {} if raw is None else {ENV_RUST_FRONT: raw}
    assert resolve_rust_front(env) is expected


@pytest.mark.parametrize("junk", ["1", "0", "true", "yes", "enabled", "o n"])
def test_resolve_junk_refuses_loudly(junk: str) -> None:
    """Any value outside {'', on, off} refuses — never a silent fallback."""
    with pytest.raises(RustFrontError, match="must be 'on' or 'off'"):
        resolve_rust_front({ENV_RUST_FRONT: junk})


def test_resolve_reads_os_environ_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default env source is os.environ (the production path)."""
    monkeypatch.setenv(ENV_RUST_FRONT, "on")
    assert resolve_rust_front() is True
    monkeypatch.delenv(ENV_RUST_FRONT)
    assert resolve_rust_front() is False


# ── default_static_dir ───────────────────────────────────────────────────────


def test_default_static_dir_is_the_packaged_tree() -> None:
    """The packaged brutex/dashboard/static tree exists and holds the JS/CSS."""
    static = default_static_dir()
    assert static.is_dir()
    assert (static / "bx-format.js").is_file()


# ── locate_brutexd — lookup ladder ───────────────────────────────────────────


def test_locate_env_override_wins(tmp_path: Path) -> None:
    """A valid BRUTEX_BRUTEXD_BIN override is returned verbatim."""
    binary = tmp_path / "brutexd"
    binary.write_bytes(b"#!fake\n")
    found = locate_brutexd({ENV_BRUTEXD_BIN: str(binary)})
    assert found == binary


def test_locate_env_override_dangling_refuses(tmp_path: Path) -> None:
    """A dangling override REFUSES — never a silent fallthrough."""
    with pytest.raises(RustFrontError, match="does not point at a file"):
        locate_brutexd({ENV_BRUTEXD_BIN: str(tmp_path / "missing")})


def test_locate_blank_override_falls_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blank override is treated as unset (falls to the repo ladder)."""
    monkeypatch.setattr(rust_front.shutil, "which", lambda _name: None)
    with pytest.raises(RustFrontError, match="brutexd binary not found"):
        locate_brutexd({ENV_BRUTEXD_BIN: "   "}, repo_root=tmp_path)


@pytest.mark.parametrize("profile", ["release", "debug"])
def test_locate_repo_target_dirs(tmp_path: Path, profile: str) -> None:
    """The in-repo cargo target dirs are probed release-first."""
    target = tmp_path / "rust" / "brutexd" / "target" / profile
    target.mkdir(parents=True)
    (target / "brutexd").write_bytes(b"#!fake\n")
    found = locate_brutexd({}, repo_root=tmp_path)
    assert found == target / "brutexd"


def test_locate_release_beats_debug(tmp_path: Path) -> None:
    """When both profiles exist, release wins (the production build)."""
    for profile in ("release", "debug"):
        target = tmp_path / "rust" / "brutexd" / "target" / profile
        target.mkdir(parents=True)
        (target / "brutexd").write_bytes(b"#!fake\n")
    found = locate_brutexd({}, repo_root=tmp_path)
    assert "release" in str(found)


def test_locate_path_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With no override and no repo build, PATH lookup is the last rung."""
    on_path = tmp_path / "brutexd"
    on_path.write_bytes(b"#!fake\n")
    monkeypatch.setattr(rust_front.shutil, "which", lambda _name: str(on_path))
    assert locate_brutexd({}, repo_root=tmp_path / "empty") == on_path


def test_locate_nothing_found_names_remediation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The final refusal names the cargo build command + the override var."""
    monkeypatch.setattr(rust_front.shutil, "which", lambda _name: None)
    with pytest.raises(RustFrontError, match="cargo build --release"):
        locate_brutexd({}, repo_root=tmp_path)


def test_locate_default_repo_root_is_the_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default repo root resolves to the tree holding brutex/ + rust/."""
    root = rust_front._default_repo_root()  # pyright: ignore[reportPrivateUsage]
    assert (root / "brutex" / "dashboard").is_dir()
    # Drive the repo_root=None default path end-to-end (outcome depends on
    # whether a local cargo build exists — both arms are legitimate).
    monkeypatch.setattr(rust_front.shutil, "which", lambda _name: None)
    try:
        found = locate_brutexd({})
    except RustFrontError:
        pass  # no local build — the refusal rung
    else:
        assert found.name == "brutexd"


# ── allocate_upstream_port ───────────────────────────────────────────────────


def test_allocate_upstream_port_is_bindable_loopback() -> None:
    """The allocated port is a real, immediately re-bindable loopback port."""
    import socket

    port = allocate_upstream_port()
    assert 0 < port < 65536
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", port))  # free at allocation time


# ── build_plan / build_command ───────────────────────────────────────────────


def _fake_binary(tmp_path: Path) -> Path:
    binary = tmp_path / "brutexd"
    binary.write_bytes(b"#!fake\n")
    return binary


def test_build_plan_happy_path(tmp_path: Path) -> None:
    """A loopback host + present binary + packaged static dir plans cleanly."""
    binary = _fake_binary(tmp_path)
    plan = build_plan(
        host="127.0.0.1",
        port=9080,
        env={ENV_BRUTEXD_BIN: str(binary)},
        upstream_port=43210,
    )
    assert plan == RustFrontPlan(
        binary=binary,
        listen_host="127.0.0.1",
        listen_port=9080,
        upstream_port=43210,
        static_dir=default_static_dir(),
    )


def test_build_plan_allocates_a_port_by_default(tmp_path: Path) -> None:
    """upstream_port=None allocates a fresh private port."""
    binary = _fake_binary(tmp_path)
    plan = build_plan(host="localhost", port=9080, env={ENV_BRUTEXD_BIN: str(binary)})
    assert 0 < plan.upstream_port < 65536
    assert plan.upstream_port != 9080


def test_build_plan_refuses_non_loopback_host(tmp_path: Path) -> None:
    """The Wave-1 loopback-only law: 0.0.0.0/LAN hosts refuse under the flag."""
    binary = _fake_binary(tmp_path)
    for host in ("0.0.0.0", "192.168.1.50", "example.com"):
        with pytest.raises(RustFrontError, match="127.0.0.1-only"):
            build_plan(host=host, port=9080, env={ENV_BRUTEXD_BIN: str(binary)})


def test_build_plan_refuses_missing_static_dir(tmp_path: Path) -> None:
    """A missing static dir refuses loudly (never a 404-everything front)."""
    binary = _fake_binary(tmp_path)
    with pytest.raises(RustFrontError, match="not a directory"):
        build_plan(
            host="127.0.0.1",
            port=9080,
            env={ENV_BRUTEXD_BIN: str(binary)},
            static_dir=tmp_path / "no-such-dir",
        )


def test_build_command_shape(tmp_path: Path) -> None:
    """The argv is the fixed brutexd flag triple — no shell, no user input."""
    plan = RustFrontPlan(
        binary=tmp_path / "brutexd",
        listen_host="127.0.0.1",
        listen_port=9080,
        upstream_port=43210,
        static_dir=tmp_path / "static",
    )
    assert build_command(plan) == [
        str(tmp_path / "brutexd"),
        "--listen",
        "127.0.0.1:9080",
        "--upstream-port",
        "43210",
        "--static-dir",
        str(tmp_path / "static"),
    ]


# ── run_with_rust_front — supervised lifecycle ───────────────────────────────


class _FakeProc:
    """Recording ProcessHandle double."""

    def __init__(
        self,
        *,
        poll_result: int | None = None,
        wait_timeouts: int = 0,
    ) -> None:
        self.poll_result = poll_result
        self.wait_timeouts = wait_timeouts
        self.calls: list[str] = []

    def poll(self) -> int | None:
        self.calls.append("poll")
        return self.poll_result

    def terminate(self) -> None:
        self.calls.append("terminate")

    def kill(self) -> None:
        self.calls.append("kill")

    def wait(self, timeout: float | None = None) -> int:
        self.calls.append("wait")
        if self.wait_timeouts > 0:
            self.wait_timeouts -= 1
            raise subprocess.TimeoutExpired(cmd="brutexd", timeout=timeout or 0.0)
        return 0


def _run(
    tmp_path: Path,
    *,
    proc: _FakeProc,
    uvicorn_raises: bool = False,
) -> tuple[dict[str, Any], _FakeProc]:
    binary = _fake_binary(tmp_path)
    seen: dict[str, Any] = {}

    def fake_popen(command: list[str]) -> _FakeProc:
        seen["command"] = command
        return proc

    def fake_uvicorn(app: Any, *, host: str, port: int, log_config: Any) -> None:
        seen["uvicorn"] = {"app": app, "host": host, "port": port, "log_config": log_config}
        if uvicorn_raises:
            raise RuntimeError("uvicorn crashed")

    kwargs: dict[str, Any] = {
        "host": "127.0.0.1",
        "port": 9080,
        "uvicorn_run": fake_uvicorn,
        "env": {ENV_BRUTEXD_BIN: str(binary)},
        "popen": fake_popen,
    }
    if uvicorn_raises:
        with pytest.raises(RuntimeError, match="uvicorn crashed"):
            run_with_rust_front(object(), **kwargs)
    else:
        run_with_rust_front(object(), **kwargs)
    return seen, proc


def test_run_spawns_front_and_binds_uvicorn_private(tmp_path: Path) -> None:
    """brutexd gets the OPERATOR port; uvicorn binds the PRIVATE loopback port."""
    seen, proc = _run(tmp_path, proc=_FakeProc())
    command = seen["command"]
    assert command[1:3] == ["--listen", "127.0.0.1:9080"]
    upstream = int(command[4])
    assert seen["uvicorn"]["host"] == "127.0.0.1"
    assert seen["uvicorn"]["port"] == upstream
    assert upstream != 9080
    assert seen["uvicorn"]["log_config"] is None
    # Clean shutdown: terminate then ONE wait, no kill escalation.
    assert proc.calls == ["poll", "terminate", "wait"]


def test_run_terminates_child_even_when_uvicorn_raises(tmp_path: Path) -> None:
    """The finally supervises: a raising uvicorn still tears brutexd down."""
    _seen, proc = _run(tmp_path, proc=_FakeProc(), uvicorn_raises=True)
    assert "terminate" in proc.calls


def test_run_escalates_to_kill_on_wait_timeout(tmp_path: Path) -> None:
    """terminate → wait timeout → kill → wait (the bounded escalation)."""
    _seen, proc = _run(tmp_path, proc=_FakeProc(wait_timeouts=1))
    assert proc.calls == ["poll", "terminate", "wait", "kill", "wait"]


def test_run_observes_an_early_dead_child(tmp_path: Path) -> None:
    """A child that already exited is observed (logged), not re-signalled."""
    _seen, proc = _run(tmp_path, proc=_FakeProc(poll_result=2))
    assert "terminate" not in proc.calls
    assert "kill" not in proc.calls


# ── cmd_dashboard wiring — the thin CLI delta ────────────────────────────────


def _stub_dashboard_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, probes: dict[str, Any]
) -> None:
    """Stub logging + autoload + liveproof + auto-resume + uvicorn.run.

    Same harness shape as tests/unit/test_cli_dashboard_default_lake.py.
    """
    obs_logging = importlib.import_module("brutex.observability.logging")
    monkeypatch.setattr(obs_logging, "configure_logging", lambda **kwargs: tmp_path / "brutex.log")
    auto_pull = importlib.import_module("brutex.dashboard.auto_pull")
    monkeypatch.setattr(auto_pull, "should_reload_instrument_master", lambda **kwargs: False)
    uvicorn_mod = importlib.import_module("uvicorn")

    def fake_run(app: Any, *, host: str, port: int, log_config: Any) -> None:
        probes["host"] = host
        probes["port"] = port

    monkeypatch.setattr(uvicorn_mod, "run", fake_run)
    monkeypatch.setattr(cli, "_start_instrument_autoload", lambda store, log: None)
    monkeypatch.setattr(cli, "_start_auto_resume_on_boot", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "_build_liveproof_autorun", lambda _root: None)


def test_flag_off_identity_pin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """FLAG OFF (absent): uvicorn binds the operator host:port directly —
    byte-identical single-process behaviour, zero brutexd involvement."""
    pytest.importorskip("fastapi", reason="dashboard needs the [ui] extra")
    pytest.importorskip("uvicorn", reason="dashboard needs the [ui] extra")
    pytest.importorskip("duckdb", reason="the instrument store needs duckdb")
    probes: dict[str, Any] = {}
    _stub_dashboard_runtime(monkeypatch, tmp_path, probes)
    monkeypatch.delenv(ENV_RUST_FRONT, raising=False)

    spawned: list[Any] = []
    monkeypatch.setattr(rust_front, "run_with_rust_front", lambda *a, **k: spawned.append((a, k)))

    rc = cli.cmd_dashboard(tmp_path, "127.0.0.1", 9096)

    assert rc == cli.EXIT_OK
    assert probes == {"host": "127.0.0.1", "port": 9096}
    assert spawned == []  # no front process path touched


def test_flag_junk_is_a_loud_cli_user_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A junk flag value refuses the start BEFORE any heavy setup (exit 1)."""
    pytest.importorskip("fastapi", reason="dashboard needs the [ui] extra")
    pytest.importorskip("uvicorn", reason="dashboard needs the [ui] extra")
    monkeypatch.setenv(ENV_RUST_FRONT, "definitely-junk")

    with pytest.raises(cli.CliUserError, match="must be 'on' or 'off'"):
        cli.cmd_dashboard(tmp_path, "127.0.0.1", 9097)


def test_flag_on_routes_through_run_with_rust_front(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FLAG ON: cmd_dashboard delegates to run_with_rust_front (operator
    host:port threaded; the direct uvicorn.run tail is NOT taken)."""
    pytest.importorskip("fastapi", reason="dashboard needs the [ui] extra")
    pytest.importorskip("uvicorn", reason="dashboard needs the [ui] extra")
    pytest.importorskip("duckdb", reason="the instrument store needs duckdb")
    probes: dict[str, Any] = {}
    _stub_dashboard_runtime(monkeypatch, tmp_path, probes)
    monkeypatch.setenv(ENV_RUST_FRONT, "on")

    calls: list[dict[str, Any]] = []

    def fake_front(app: Any, *, host: str, port: int, uvicorn_run: Any) -> None:
        calls.append({"app": app, "host": host, "port": port, "uvicorn_run": uvicorn_run})

    monkeypatch.setattr(rust_front, "run_with_rust_front", fake_front)

    rc = cli.cmd_dashboard(tmp_path, "127.0.0.1", 9098)

    assert rc == cli.EXIT_OK
    assert len(calls) == 1
    assert calls[0]["host"] == "127.0.0.1"
    assert calls[0]["port"] == 9098
    assert probes == {}  # the direct uvicorn.run tail was not taken


def test_flag_on_launch_refusal_maps_to_cli_user_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A RustFrontError from the launcher (missing binary) → CliUserError."""
    pytest.importorskip("fastapi", reason="dashboard needs the [ui] extra")
    pytest.importorskip("uvicorn", reason="dashboard needs the [ui] extra")
    pytest.importorskip("duckdb", reason="the instrument store needs duckdb")
    probes: dict[str, Any] = {}
    _stub_dashboard_runtime(monkeypatch, tmp_path, probes)
    monkeypatch.setenv(ENV_RUST_FRONT, "on")

    def refusing_front(app: Any, *, host: str, port: int, uvicorn_run: Any) -> None:
        raise RustFrontError("brutexd binary not found — build it")

    monkeypatch.setattr(rust_front, "run_with_rust_front", refusing_front)

    with pytest.raises(cli.CliUserError, match="binary not found"):
        cli.cmd_dashboard(tmp_path, "127.0.0.1", 9099)
