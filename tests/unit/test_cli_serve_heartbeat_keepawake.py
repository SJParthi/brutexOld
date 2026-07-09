"""``brutex dashboard`` serve-observability wiring (DEC-AUDIT-ALERT-CLAMP-OBS-001).

Covers the cmd_dashboard branches added for the idle heartbeat + the opt-in
serve-lifetime keep-awake. The default (no-env) branches — heartbeat STARTED and
keep-awake OFF — are exercised by the existing cmd_dashboard suites
(test_cli_dashboard_allowed_hosts / test_cli_dashboard_default_lake); this file
covers the OTHER two branches: heartbeat DISABLED and keep-awake ON.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
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


def test_serve_heartbeat_disabled_and_keepawake_enabled_branches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("fastapi", reason="dashboard needs the [ui] extra")
    pytest.importorskip("uvicorn", reason="dashboard needs the [ui] extra")
    pytest.importorskip("duckdb", reason="the instrument store needs duckdb")
    probes: dict[str, Any] = {}
    _stub_dashboard_runtime(monkeypatch, tmp_path, probes)

    # Heartbeat OFF (interval 0) -> the hb-is-None branch; keep-awake ON.
    monkeypatch.setenv("BRUTEX_SERVER_HEARTBEAT", "0")
    monkeypatch.setenv("BRUTEX_SERVE_KEEP_AWAKE", "1")

    acquired: list[str] = []
    power = importlib.import_module("brutex.observability.power_keepawake")

    @contextmanager
    def fake_keep_awake(reason: str, **_kw: Any) -> Iterator[None]:
        acquired.append(reason)
        try:
            yield
        finally:
            acquired.append(f"released:{reason}")

    monkeypatch.setattr(power, "keep_awake", fake_keep_awake)

    with capture_logs() as logs:
        rc = cli.cmd_dashboard(tmp_path, "127.0.0.1", 9096)

    assert rc == cli.EXIT_OK
    assert probes["port"] == 9096
    events = [e["event"] for e in logs]
    assert "server.heartbeat.disabled" in events  # interval 0 -> no daemon
    assert "server.keepawake.serve_enabled" in events  # opt-in ON branch
    # The keep-awake assertion was acquired for the serve lifetime and released
    # when uvicorn.run returned (the ExitStack tore it down).
    assert acquired == ["brutex serve", "released:brutex serve"]


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
