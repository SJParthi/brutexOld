"""``cmd_dashboard``'s zero-command live-proof wiring (DEC-DOCTOR-LIVE-AUTORUN-001).

The operator law: he starts BruteX as always (IDE ▶ / ``./up.sh`` /
``brutex dashboard`` — every surface funnels into ``cmd_dashboard``) and the
live-proof preflight runs ITSELF. These tests cover the two boot seams
(:func:`brutex.cli._build_liveproof_autorun` — offline construction — and
:func:`brutex.cli._start_liveproof_preflight` — the O(1) kick) plus the
``cmd_dashboard`` wiring: the holder is built for the resolved lake root,
handed to ``build_app``, and started; and EITHER seam failing only logs —
the dashboard always still binds (never crash boot, Golden Rule #10).
Offline throughout: uvicorn + the autoload/auto-resume seams stubbed via the
shared ``_stub_dashboard_runtime`` helper.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from brutex import cli
from brutex.doctor_autorun import LiveProofAutorun

from tests.unit.test_cli_run import _stub_dashboard_runtime


class _RecordingLiveProof:
    """A stand-in holder recording start_preflight kicks."""

    def __init__(self) -> None:
        self.kicks = 0

    def start_preflight(self) -> bool:
        """Record the kick and report 'started' like the real holder."""
        self.kicks += 1
        return True


class _RecordingLog:
    """A minimal structlog-shaped recorder for the seam's info line."""

    def __init__(self) -> None:
        self.infos: list[tuple[str, dict[str, Any]]] = []
        self.warnings: list[tuple[str, dict[str, Any]]] = []

    def info(self, event: str, **kwargs: Any) -> None:
        """Record an info event."""
        self.infos.append((event, kwargs))

    def warning(self, event: str, **kwargs: Any) -> None:
        """Record a warning event."""
        self.warnings.append((event, kwargs))


def test_build_liveproof_autorun_constructs_offline(tmp_path: Path) -> None:
    """Construction is cheap + offline: no thread, no network, caches loaded."""
    holder = cli._build_liveproof_autorun(tmp_path / "lake")
    assert isinstance(holder, LiveProofAutorun)
    status = holder.status()
    assert status["preflight"]["state"] == "idle"
    assert status["preflight"]["verdict"] is None
    assert status["probes"]["verdict"] is None


def test_start_liveproof_preflight_kicks_and_logs() -> None:
    """The boot seam kicks start_preflight once and logs the outcome."""
    holder = _RecordingLiveProof()
    log = _RecordingLog()
    cli._start_liveproof_preflight(holder, log)
    assert holder.kicks == 1
    assert log.infos == [("liveproof.autorun_preflight", {"started": True})]


def test_dashboard_boot_builds_and_starts_the_autorun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cmd_dashboard wires the holder into build_app AND kicks the preflight."""
    pytest.importorskip("fastapi", reason="dashboard needs the [ui] extra")
    pytest.importorskip("uvicorn", reason="dashboard needs the [ui] extra")
    pytest.importorskip("duckdb", reason="the instrument store needs duckdb")
    probes: dict[str, Any] = {}
    _stub_dashboard_runtime(monkeypatch, tmp_path, probes, probe_stream=False)
    holder = _RecordingLiveProof()
    built_for: list[Path] = []

    def _fake_builder(lake_root: Path) -> Any:
        built_for.append(lake_root)
        return holder

    monkeypatch.setattr(cli, "_build_liveproof_autorun", _fake_builder)
    # cmd_dashboard imports build_app lazily at call time, so patching the app
    # module's attribute intercepts the wiring.
    wired: dict[str, Any] = {}
    import brutex.dashboard.app as app_module

    original_build_app = app_module.build_app

    def _spy_build_app(*args: Any, **kwargs: Any) -> Any:
        wired["liveproof"] = kwargs.get("liveproof")
        return original_build_app(*args, **kwargs)

    monkeypatch.setattr(app_module, "build_app", _spy_build_app)
    rc = cli.cmd_dashboard(tmp_path, "127.0.0.1", 9080)
    assert rc == cli.EXIT_OK
    assert built_for == [tmp_path]  # the RESOLVED lake root (explicit --lake-root)
    assert wired["liveproof"] is holder
    assert holder.kicks == 1
    assert probes["port"] == 9080  # uvicorn still served


def test_dashboard_boot_survives_a_builder_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A holder-construction failure only logs — the dashboard still binds."""
    pytest.importorskip("fastapi", reason="dashboard needs the [ui] extra")
    pytest.importorskip("uvicorn", reason="dashboard needs the [ui] extra")
    pytest.importorskip("duckdb", reason="the instrument store needs duckdb")
    probes: dict[str, Any] = {}
    _stub_dashboard_runtime(monkeypatch, tmp_path, probes, probe_stream=False)

    def _boom(_lake_root: Path) -> Any:
        raise RuntimeError("cache volume exploded")

    monkeypatch.setattr(cli, "_build_liveproof_autorun", _boom)
    rc = cli.cmd_dashboard(tmp_path, "127.0.0.1", 9081)
    assert rc == cli.EXIT_OK
    assert probes["port"] == 9081


def test_dashboard_boot_survives_a_preflight_launch_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A preflight-kick failure only logs — the dashboard still binds."""
    pytest.importorskip("fastapi", reason="dashboard needs the [ui] extra")
    pytest.importorskip("uvicorn", reason="dashboard needs the [ui] extra")
    pytest.importorskip("duckdb", reason="the instrument store needs duckdb")
    probes: dict[str, Any] = {}
    _stub_dashboard_runtime(monkeypatch, tmp_path, probes, probe_stream=False)
    monkeypatch.setattr(cli, "_build_liveproof_autorun", lambda _root: _RecordingLiveProof())

    def _boom(_liveproof: Any, _log: Any) -> None:
        raise RuntimeError("thread spawn refused")

    monkeypatch.setattr(cli, "_start_liveproof_preflight", _boom)
    rc = cli.cmd_dashboard(tmp_path, "127.0.0.1", 9082)
    assert rc == cli.EXIT_OK
    assert probes["port"] == 9082
