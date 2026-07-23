"""``brutex/dashboard/rust_front.py`` — the default-AUTO-ON Rust-front seam.

Covers the module 100% line+branch standalone (the self-contained gate the
PR ships). The thin ``cmd_dashboard`` wiring tests (AUTO-default pins ·
junk-flag ``CliUserError`` · flag-on delegation · launcher-refusal mapping)
live in ``tests/unit/test_cli_dashboard_rust_front.py`` — the ``test_cli*``
stem the coverage-gate discovery maps to ``brutex/cli.py``:

* flag law (DEC-RUST-DEFAULT-ON-WAVE-001): absent → AUTO (probe-then-decide,
  never fatal); empty/``off`` → OFF; ``on`` → strict ON; junk → loud
  refusal (``RustFrontError``) — unchanged from the default-OFF era;
* the AUTO probe (``probe_auto_front``): preconditions hold → ``True``;
  any ``RustFrontError`` → ONE loud WARNING + ``False``, never a raise;
* binary lookup ladder (env override → repo target dirs → PATH → refusal);
* plan validation (loopback-only host law, static-dir presence);
* the supervised child lifecycle (terminate → wait → kill escalation;
  early-exit observation; teardown on a raising uvicorn).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
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
    probe_auto_front,
    resolve_rust_front,
    resolve_rust_front_mode,
    run_with_rust_front,
)
from structlog.testing import capture_logs

# ── resolve_rust_front_mode — the fail-loud tri-state flag law ───────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, "auto"),  # UNSET = AUTO (DEC-RUST-DEFAULT-ON-WAVE-001)
        ("", "off"),  # set-but-blank is an EXPLICIT off, never AUTO
        ("   ", "off"),
        ("off", "off"),
        ("OFF", "off"),
        (" Off ", "off"),
        ("on", "on"),
        ("ON", "on"),
        (" On ", "on"),
    ],
)
def test_resolve_mode_states(raw: str | None, expected: str) -> None:
    """Absent → AUTO; blank/off → OFF; on → ON (case/whitespace-insensitive)."""
    env: dict[str, str] = {} if raw is None else {ENV_RUST_FRONT: raw}
    assert resolve_rust_front_mode(env) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, False),  # unset = not EXPLICITLY on (AUTO lives in the mode fn)
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
    """The bool view reports EXPLICIT-on only (strict-arm compatibility)."""
    env: dict[str, str] = {} if raw is None else {ENV_RUST_FRONT: raw}
    assert resolve_rust_front(env) is expected


@pytest.mark.parametrize("junk", ["1", "0", "true", "yes", "enabled", "o n"])
def test_resolve_junk_refuses_loudly(junk: str) -> None:
    """Any value outside {'', on, off} refuses — never a silent fallback.

    Junk semantics are UNCHANGED by the AUTO default (both views refuse).
    """
    with pytest.raises(RustFrontError, match="must be 'on' or 'off'"):
        resolve_rust_front({ENV_RUST_FRONT: junk})
    with pytest.raises(RustFrontError, match="must be 'on' or 'off'"):
        resolve_rust_front_mode({ENV_RUST_FRONT: junk})


def test_resolve_reads_os_environ_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default env source is os.environ (the production path)."""
    monkeypatch.setenv(ENV_RUST_FRONT, "on")
    assert resolve_rust_front() is True
    assert resolve_rust_front_mode() == "on"
    monkeypatch.delenv(ENV_RUST_FRONT)
    assert resolve_rust_front() is False  # not EXPLICITLY on …
    assert resolve_rust_front_mode() == "auto"  # … but the default is AUTO


# ── probe_auto_front — the AUTO arm (DEC-RUST-DEFAULT-ON-WAVE-001) ───────────


def test_probe_auto_front_binary_present_is_on(tmp_path: Path) -> None:
    """Preconditions hold (binary found via the ladder) → True, zero warns."""
    binary = tmp_path / "brutexd"
    binary.write_bytes(b"#!fake\n")
    with capture_logs() as logs:
        assert (
            probe_auto_front(host="127.0.0.1", port=9080, env={ENV_BRUTEXD_BIN: str(binary)})
            is True
        )
    assert [e for e in logs if e["event"] == "rust_front.auto_fallback_python"] == []


def test_probe_auto_front_binary_absent_warns_and_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No brutexd anywhere → ONE loud WARNING naming the missing binary + the
    cargo remediation, and False (the Python-only fallback) — NEVER a raise."""

    def refusing_locate(env: Any = None, *, repo_root: Path | None = None) -> Path:
        raise RustFrontError(
            "brutexd binary not found — build it with "
            "`cargo build --release --manifest-path rust/brutexd/Cargo.toml`"
        )

    monkeypatch.setattr(rust_front, "locate_brutexd", refusing_locate)
    with capture_logs() as logs:
        assert probe_auto_front(host="127.0.0.1", port=9080) is False
    warns = [e for e in logs if e["event"] == "rust_front.auto_fallback_python"]
    assert len(warns) == 1  # exactly ONE loud warning
    assert warns[0]["log_level"] == "warning"
    assert "binary not found" in warns[0]["reason"]
    assert "cargo build --release" in warns[0]["remediation"]


def test_probe_auto_front_non_loopback_host_falls_back(tmp_path: Path) -> None:
    """A non-loopback --host under AUTO warns + falls back — never fatal
    (the explicit-on path keeps its strict refusal, pinned below)."""
    binary = tmp_path / "brutexd"
    binary.write_bytes(b"#!fake\n")
    with capture_logs() as logs:
        assert (
            probe_auto_front(host="0.0.0.0", port=9080, env={ENV_BRUTEXD_BIN: str(binary)}) is False
        )
    warns = [e for e in logs if e["event"] == "rust_front.auto_fallback_python"]
    assert len(warns) == 1
    assert "127.0.0.1-only" in warns[0]["reason"]


def test_probe_auto_front_missing_static_dir_falls_back(tmp_path: Path) -> None:
    """A missing static dir under AUTO warns + falls back — never fatal."""
    binary = tmp_path / "brutexd"
    binary.write_bytes(b"#!fake\n")
    with capture_logs() as logs:
        assert (
            probe_auto_front(
                host="127.0.0.1",
                port=9080,
                env={ENV_BRUTEXD_BIN: str(binary)},
                static_dir=tmp_path / "no-such-dir",
            )
            is False
        )
    warns = [e for e in logs if e["event"] == "rust_front.auto_fallback_python"]
    assert len(warns) == 1
    assert "not a directory" in warns[0]["reason"]


def test_probe_auto_front_never_allocates_a_real_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probe passes the upstream_port=0 sentinel — no throwaway socket."""
    binary = tmp_path / "brutexd"
    binary.write_bytes(b"#!fake\n")

    def bomb() -> int:  # pragma: no cover — must never run
        raise AssertionError("probe must not allocate a port")

    monkeypatch.setattr(rust_front, "allocate_upstream_port", bomb)
    assert probe_auto_front(host="127.0.0.1", port=9080, env={ENV_BRUTEXD_BIN: str(binary)}) is True


def test_probe_auto_front_propagates_a_genuine_code_bug(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only RustFrontError degrades — a non-domain bug still propagates
    (no blanket except; Golden Rule #1)."""

    def buggy_locate(env: Any = None, *, repo_root: Path | None = None) -> Path:
        raise ValueError("genuine code bug")

    monkeypatch.setattr(rust_front, "locate_brutexd", buggy_locate)
    with pytest.raises(ValueError, match="genuine code bug"):
        probe_auto_front(host="127.0.0.1", port=9080)


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


def _assert_parses_as_loopback_socketaddr(listen_value: str) -> None:
    """Mirror the Rust ``SocketAddr`` loopback check on a rendered --listen value.

    ``rust/brutexd/src/config.rs`` parses ``--listen`` as a numeric SocketAddr
    and refuses non-loopback IPs. A bracketed IPv6 authority (``[::1]:9080``) or
    a bare IPv4 (``127.0.0.1:9080``) must split into an IP that ``ipaddress``
    accepts AND reports loopback — the same law the Rust parser enforces.
    """
    import ipaddress

    host, _, port = listen_value.rpartition(":")
    assert port.isdigit() and 0 < int(port) < 65536, listen_value
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]  # RFC 3986 bracketed IPv6 literal
    assert ipaddress.ip_address(host).is_loopback, f"not loopback: {listen_value}"


def test_build_command_renders_parseable_listen_for_every_loopback_host(tmp_path: Path) -> None:
    """MED-1: every LOOPBACK_HOSTS spelling renders a --listen value the Rust
    SocketAddr parser accepts — ``localhost`` → 127.0.0.1, ``::1`` → ``[::1]``,
    an IP passes through. A bare ``localhost:9080``/``::1:9080`` would make
    brutexd exit 2 and leave the operator port dead."""
    binary = _fake_binary(tmp_path)
    expected = {
        "127.0.0.1": "127.0.0.1:9080",
        "localhost": "127.0.0.1:9080",
        "::1": "[::1]:9080",
    }
    # Coverage law: every accepted loopback spelling has a pinned rendering.
    assert set(expected) == set(rust_front.LOOPBACK_HOSTS)
    for host, want in expected.items():
        plan = build_plan(
            host=host, port=9080, env={ENV_BRUTEXD_BIN: str(binary)}, upstream_port=43210
        )
        assert plan.listen_host == host  # the operator spelling is PRESERVED on the plan
        command = build_command(plan)
        listen_value = command[command.index("--listen") + 1]
        assert listen_value == want
        _assert_parses_as_loopback_socketaddr(listen_value)


def test_render_listen_passes_through_a_bare_ip() -> None:
    """A non-mapped host (already an IP) renders verbatim."""
    assert rust_front.render_listen("127.0.0.1", 9080) == "127.0.0.1:9080"
    assert rust_front.render_listen("localhost", 1) == "127.0.0.1:1"
    assert rust_front.render_listen("::1", 65535) == "[::1]:65535"


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
        # Fast + deterministic liveness probe (no wall-clock wait): report the
        # child's poll() directly so the healthy path proceeds to uvicorn.
        "liveness_probe": lambda p: p.poll(),
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
    # liveness poll (alive) → uvicorn → shutdown poll → terminate → ONE wait.
    assert proc.calls == ["poll", "poll", "terminate", "wait"]


def test_run_terminates_child_even_when_uvicorn_raises(tmp_path: Path) -> None:
    """The finally supervises: a raising uvicorn still tears brutexd down."""
    _seen, proc = _run(tmp_path, proc=_FakeProc(), uvicorn_raises=True)
    assert "terminate" in proc.calls


def test_run_escalates_to_kill_on_wait_timeout(tmp_path: Path) -> None:
    """terminate → wait timeout → kill → wait (the bounded escalation)."""
    _seen, proc = _run(tmp_path, proc=_FakeProc(wait_timeouts=1))
    assert proc.calls == ["poll", "poll", "terminate", "wait", "kill", "wait"]


def test_run_refuses_when_brutexd_dies_before_uvicorn(tmp_path: Path) -> None:
    """MED-2: a brutexd that exits immediately (bad --listen, port busy, static
    dir gone) refuses LOUDLY and NEVER enters uvicorn — no half-up dashboard on
    a dead operator port."""
    binary = _fake_binary(tmp_path)
    proc = _FakeProc(poll_result=2)
    uvicorn_called: list[bool] = []

    def fake_uvicorn(app: Any, *, host: str, port: int, log_config: Any) -> None:
        uvicorn_called.append(True)  # pragma: no cover — must never run

    with pytest.raises(RustFrontError, match="exited immediately"):
        run_with_rust_front(
            object(),
            host="127.0.0.1",
            port=9080,
            uvicorn_run=fake_uvicorn,
            env={ENV_BRUTEXD_BIN: str(binary)},
            popen=lambda _command: proc,
            liveness_probe=lambda p: p.poll(),
        )
    assert uvicorn_called == []
    # The refusal names the operator port + the remediation runbook.
    with pytest.raises(RustFrontError, match="rust-front.md"):
        run_with_rust_front(
            object(),
            host="127.0.0.1",
            port=9080,
            uvicorn_run=fake_uvicorn,
            env={ENV_BRUTEXD_BIN: str(binary)},
            popen=lambda _command: proc,
            liveness_probe=lambda p: p.poll(),
        )
    # A dead-before-uvicorn child is NOT re-signalled (raise is before _shutdown).
    assert "terminate" not in proc.calls
    assert "kill" not in proc.calls


def test_run_tears_down_child_when_liveness_probe_raises(tmp_path: Path) -> None:
    """LOW-1: a liveness_probe that RAISES (e.g. KeyboardInterrupt during the
    startup-grace sleep) must not orphan the spawned brutexd — the child is
    terminated before the exception propagates ("never an orphan by design")."""
    binary = _fake_binary(tmp_path)
    proc = _FakeProc(poll_result=None)  # ALIVE — a leak would be a real orphan
    uvicorn_called: list[bool] = []

    def fake_uvicorn(app: Any, *, host: str, port: int, log_config: Any) -> None:
        uvicorn_called.append(True)  # pragma: no cover — must never run

    def raising_probe(_p: rust_front.ProcessHandle) -> int | None:
        raise KeyboardInterrupt("interrupted during startup grace")

    with pytest.raises(KeyboardInterrupt, match="startup grace"):
        run_with_rust_front(
            object(),
            host="127.0.0.1",
            port=9080,
            uvicorn_run=fake_uvicorn,
            env={ENV_BRUTEXD_BIN: str(binary)},
            popen=lambda _command: proc,
            liveness_probe=raising_probe,
        )
    assert uvicorn_called == []  # never sailed into uvicorn
    # The alive child WAS torn down (terminate → wait), not leaked.
    assert proc.calls == ["poll", "terminate", "wait"]


def test_default_liveness_probe_sleeps_then_polls() -> None:
    """The default probe waits ONE bounded grace then reports poll() — injectable
    sleep keeps it deterministic (no wall-clock wait in tests)."""
    proc = _FakeProc(poll_result=None)
    slept: list[float] = []
    rc = rust_front._default_liveness_probe(  # pyright: ignore[reportPrivateUsage]
        proc, sleep=slept.append
    )
    assert rc is None
    assert slept == [rust_front.STARTUP_GRACE_S]
    assert proc.calls == ["poll"]


def test_default_liveness_probe_reports_a_dead_child() -> None:
    """A child that already exited reports its code (drives the raise path)."""
    proc = _FakeProc(poll_result=7)
    rc = rust_front._default_liveness_probe(  # pyright: ignore[reportPrivateUsage]
        proc, sleep=lambda _s: None
    )
    assert rc == 7


def test_shutdown_observes_an_early_dead_child() -> None:
    """A child already dead at teardown is observed (logged), not re-signalled.

    ``_shutdown`` polls twice on this path (the guard + the logged returncode).
    """
    proc = _FakeProc(poll_result=2)
    rust_front._shutdown(proc)  # pyright: ignore[reportPrivateUsage]
    assert proc.calls == ["poll", "poll"]
    assert "terminate" not in proc.calls
    assert "kill" not in proc.calls


def test_shutdown_survives_an_unkillable_child() -> None:
    """LOW: a SIGKILLed child whose post-kill wait ALSO times out must not raise
    from _shutdown (which would mask uvicorn's own exception) — log + return."""
    proc = _FakeProc(wait_timeouts=2)
    rust_front._shutdown(proc)  # pyright: ignore[reportPrivateUsage] — must not raise
    assert proc.calls == ["poll", "terminate", "wait", "kill", "wait"]


def test_conftest_belt_pins_the_real_env_name() -> None:
    """DRIFT PIN: the tests/conftest.py hermeticity belt (`BRUTEX_RUST_FRONT`
    pinned "off" suite-wide under the AUTO default —
    DEC-RUST-DEFAULT-ON-WAVE-001) must name EXACTLY `ENV_RUST_FRONT`; a
    renamed flag without a belt update would silently re-expose every
    flag-unset `cmd_dashboard` test to the host's brutexd binary."""
    conftest = Path(__file__).resolve().parents[1] / "conftest.py"
    source = conftest.read_text(encoding="utf-8")
    assert f'os.environ.setdefault("{ENV_RUST_FRONT}", "off")' in source
