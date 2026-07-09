"""``brutex dashboard`` serve-observability wiring (DEC-AUDIT-ALERT-CLAMP-OBS-001).

Covers the cmd_dashboard branches added for the idle heartbeat + the opt-in
serve-lifetime keep-awake. The default (no-env) branches — heartbeat STARTED and
keep-awake OFF — are exercised by the existing cmd_dashboard suites
(test_cli_dashboard_allowed_hosts / test_cli_dashboard_default_lake); this file
covers the OTHER two branches: heartbeat DISABLED and keep-awake ON.
"""

from __future__ import annotations

import importlib
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from brutex import cli
from structlog.testing import capture_logs


def _stub_dashboard_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, probes: dict[str, Any]
) -> None:
    obs_logging = importlib.import_module("brutex.observability.logging")
    monkeypatch.setattr(obs_logging, "configure_logging", lambda **kwargs: tmp_path / "brutex.log")
    uvicorn_mod = importlib.import_module("uvicorn")

    def fake_run(app: Any, *, host: str, port: int, log_config: Any) -> None:
        probes["port"] = port

    monkeypatch.setattr(uvicorn_mod, "run", fake_run)
    monkeypatch.setattr(cli, "_start_instrument_autoload", lambda store, log: None)
    monkeypatch.setattr(cli, "_start_auto_resume_on_boot", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "_build_liveproof_autorun", lambda _root: None)


def _fake_keep_awake_factory(acquired: list[str], *, engaged: bool) -> Any:
    """A keep_awake double yielding the injected engaged verdict (bool contract)."""

    @contextmanager
    def fake_keep_awake(reason: str, **_kw: Any) -> Generator[bool]:
        acquired.append(reason)
        try:
            yield engaged
        finally:
            acquired.append(f"released:{reason}")

    return fake_keep_awake


def test_serve_heartbeat_disabled_and_keepawake_enabled_branches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pytest.importorskip("fastapi", reason="dashboard needs the [ui] extra")
    pytest.importorskip("uvicorn", reason="dashboard needs the [ui] extra")
    pytest.importorskip("duckdb", reason="the instrument store needs duckdb")
    probes: dict[str, Any] = {}
    _stub_dashboard_runtime(monkeypatch, tmp_path, probes)

    # Heartbeat OFF (interval 0) -> the hb-is-None branch; keep-awake ON and
    # ENGAGED (the darwin+caffeinate-spawned arm — keep_awake yields True).
    monkeypatch.setenv("BRUTEX_SERVER_HEARTBEAT", "0")
    monkeypatch.setenv("BRUTEX_SERVE_KEEP_AWAKE", "1")

    acquired: list[str] = []
    power = importlib.import_module("brutex.observability.power_keepawake")
    monkeypatch.setattr(power, "keep_awake", _fake_keep_awake_factory(acquired, engaged=True))

    with capture_logs() as logs:
        rc = cli.cmd_dashboard(tmp_path, "127.0.0.1", 9096)

    assert rc == cli.EXIT_OK
    assert probes["port"] == 9096
    events = [e["event"] for e in logs]
    assert "server.heartbeat.disabled" in events  # interval 0 -> no daemon
    assert "server.keepawake.serve_enabled" in events  # opt-in ON + engaged branch
    # The keep-awake assertion was acquired for the serve lifetime and released
    # when uvicorn.run returned (the ExitStack tore it down).
    assert acquired == ["brutex serve", "released:brutex serve"]
    # ENGAGED arm banner: the "holds the host awake" claim is printed ONLY when
    # keep_awake reported the assertion is really held (Golden Rule #10).
    out = capsys.readouterr().out
    assert "[brutex] serve keep-awake ENABLED — this process holds the host awake." in out
    assert "does NOT hold the host awake" not in out


def test_serve_keepawake_requested_but_unavailable_prints_truthful_banner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """BRUTEX_SERVE_KEEP_AWAKE=1 on a host where keep_awake degrades to a no-op
    (non-darwin, or caffeinate failed) must NOT print "holds the host awake" —
    the banner states the process does NOT hold the host awake (hostile-review
    MINOR, Golden Rule #10)."""
    pytest.importorskip("fastapi", reason="dashboard needs the [ui] extra")
    pytest.importorskip("uvicorn", reason="dashboard needs the [ui] extra")
    pytest.importorskip("duckdb", reason="the instrument store needs duckdb")
    probes: dict[str, Any] = {}
    _stub_dashboard_runtime(monkeypatch, tmp_path, probes)

    monkeypatch.setenv("BRUTEX_SERVER_HEARTBEAT", "0")
    monkeypatch.setenv("BRUTEX_SERVE_KEEP_AWAKE", "1")

    acquired: list[str] = []
    power = importlib.import_module("brutex.observability.power_keepawake")
    monkeypatch.setattr(power, "keep_awake", _fake_keep_awake_factory(acquired, engaged=False))

    with capture_logs() as logs:
        rc = cli.cmd_dashboard(tmp_path, "127.0.0.1", 9098)

    assert rc == cli.EXIT_OK
    assert probes["port"] == 9098
    events = [e["event"] for e in logs]
    assert "server.keepawake.serve_unavailable" in events
    assert "server.keepawake.serve_enabled" not in events  # never a false ENABLED claim
    # The guard context is still entered/released (the no-op degrade lives
    # INSIDE keep_awake; the serve wiring holds it for the lifetime either way).
    assert acquired == ["brutex serve", "released:brutex serve"]
    # UNAVAILABLE arm banner: requested-but-not-engaged wording, no overstated claim.
    out = capsys.readouterr().out
    assert (
        "[brutex] serve keep-awake requested, but keep-awake is UNAVAILABLE on "
        "this host (non-macOS, or caffeinate failed) — the process does NOT "
        "hold the host awake." in out
    )
    assert "ENABLED — this process holds the host awake" not in out


def test_serve_keepawake_off_branch_logs_honest_banner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("fastapi", reason="dashboard needs the [ui] extra")
    pytest.importorskip("uvicorn", reason="dashboard needs the [ui] extra")
    pytest.importorskip("duckdb", reason="the instrument store needs duckdb")
    probes: dict[str, Any] = {}
    _stub_dashboard_runtime(monkeypatch, tmp_path, probes)
    # Heartbeat OFF keeps the test free of a real daemon thread; keep-awake unset
    # -> the honest "does NOT prevent sleep" banner branch.
    monkeypatch.setenv("BRUTEX_SERVER_HEARTBEAT", "0")
    monkeypatch.delenv("BRUTEX_SERVE_KEEP_AWAKE", raising=False)

    with capture_logs() as logs:
        rc = cli.cmd_dashboard(tmp_path, "127.0.0.1", 9097)

    assert rc == cli.EXIT_OK
    assert "server.keepawake.serve_disabled" in [e["event"] for e in logs]
