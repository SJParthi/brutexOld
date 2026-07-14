"""``brutex dashboard`` loop-watchdog binding (DEC-DASHBOARD-PULL-CONTENTION-RESILIENCE-001, G5).

The production binding law: ``cmd_dashboard`` resolves the fail-loud
``BRUTEX_LOOP_WATCHDOG`` kill switch FIRST (junk aborts startup with a clean
one-liner — never a silent coercion), then best-effort-constructs a
:class:`~brutex.observability.loop_watchdog.LoopWatchdog` (a construction
fault logs and the dashboard still binds) and threads it into ``build_app``.

Offline: uvicorn.run + the instrument auto-load + liveproof + auto-resume are
all faked (the ``test_cli_dashboard_default_lake`` harness shape).
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest
from brutex import cli
from brutex.observability.loop_watchdog import LOOP_WATCHDOG_ENV, LoopWatchdog


def _stub_dashboard_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, probes: dict[str, Any]
) -> None:
    """Stub logging config + autoload + liveproof + auto-resume + uvicorn.run."""
    pytest.importorskip("fastapi", reason="dashboard needs the [ui] extra")
    pytest.importorskip("uvicorn", reason="dashboard needs the [ui] extra")
    pytest.importorskip("duckdb", reason="the instrument store needs duckdb")
    obs_logging = importlib.import_module("brutex.observability.logging")
    monkeypatch.setattr(obs_logging, "configure_logging", lambda **kwargs: tmp_path / "brutex.log")
    auto_pull = importlib.import_module("brutex.dashboard.auto_pull")
    monkeypatch.setattr(auto_pull, "should_reload_instrument_master", lambda **kwargs: False)
    uvicorn_mod = importlib.import_module("uvicorn")

    def fake_run(app: Any, *, host: str, port: int, log_config: Any) -> None:
        probes["ran"] = True

    monkeypatch.setattr(uvicorn_mod, "run", fake_run)
    monkeypatch.setattr(cli, "_start_instrument_autoload", lambda store, log: None)
    monkeypatch.setattr(cli, "_start_auto_resume_on_boot", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "_build_liveproof_autorun", lambda _root: None)
    monkeypatch.setattr(cli, "_start_lake_heal_sweep", lambda *args, **kwargs: None)
    # Capture the loop_watchdog build_app receives (the binding under test).
    app_mod = importlib.import_module("brutex.dashboard.app")
    real_build = app_mod.build_app

    def _spy_build(*args: Any, **kwargs: Any) -> Any:
        probes["loop_watchdog"] = kwargs.get("loop_watchdog")
        return real_build(*args, **kwargs)

    monkeypatch.setattr(app_mod, "build_app", _spy_build)


def test_default_env_binds_a_watchdog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset env (default ON): build_app receives a real LoopWatchdog wired
    with the env SweepAlerter + the global registry."""
    probes: dict[str, Any] = {}
    _stub_dashboard_runtime(monkeypatch, tmp_path, probes)
    monkeypatch.delenv(LOOP_WATCHDOG_ENV, raising=False)
    monkeypatch.setenv("BRUTEX_LAKE_ROOT", str(tmp_path))
    assert cli.cmd_dashboard(None, "127.0.0.1", 9085) == cli.EXIT_OK
    assert isinstance(probes["loop_watchdog"], LoopWatchdog)
    assert probes["ran"] is True


def test_kill_switch_off_binds_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``BRUTEX_LOOP_WATCHDOG=off``: build_app receives None (zero threads)."""
    probes: dict[str, Any] = {}
    _stub_dashboard_runtime(monkeypatch, tmp_path, probes)
    monkeypatch.setenv(LOOP_WATCHDOG_ENV, "off")
    monkeypatch.setenv("BRUTEX_LAKE_ROOT", str(tmp_path))
    assert cli.cmd_dashboard(None, "127.0.0.1", 9086) == cli.EXIT_OK
    assert probes["loop_watchdog"] is None
    assert probes["ran"] is True


def test_junk_env_fails_loud_before_bind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Junk env value: a clean EXIT_USER_ERROR one-liner BEFORE uvicorn ever
    runs — a typo can never silently disable the watchdog (the resolve_*
    fail-loud house pattern)."""
    probes: dict[str, Any] = {}
    _stub_dashboard_runtime(monkeypatch, tmp_path, probes)
    monkeypatch.setenv(LOOP_WATCHDOG_ENV, "maybe")
    monkeypatch.setenv("BRUTEX_LAKE_ROOT", str(tmp_path))
    assert cli.cmd_dashboard(None, "127.0.0.1", 9087) == cli.EXIT_USER_ERROR
    assert "ran" not in probes  # uvicorn.run never reached
    assert "BRUTEX_LOOP_WATCHDOG" in capsys.readouterr().err


def test_junk_revision_probe_ttl_env_fails_loud_before_bind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Junk ``BRUTEX_REVISION_PROBE_TTL_S``: the SAME clean EXIT_USER_ERROR
    one-liner as the loop-watchdog sibling above, BEFORE build_app/uvicorn —
    never a raw ValueError traceback (DEC-O1-GUARANTEE-HARDEN-001, review
    round 2; build_app keeps its OWN eager raise for non-CLI embedders,
    pinned in test_dashboard_revision_probe_memo.py)."""
    probes: dict[str, Any] = {}
    _stub_dashboard_runtime(monkeypatch, tmp_path, probes)
    monkeypatch.setenv("BRUTEX_REVISION_PROBE_TTL_S", "garbage")
    monkeypatch.setenv("BRUTEX_LAKE_ROOT", str(tmp_path))
    assert cli.cmd_dashboard(None, "127.0.0.1", 9089) == cli.EXIT_USER_ERROR
    assert "ran" not in probes  # uvicorn.run never reached
    assert "loop_watchdog" not in probes  # refused BEFORE build_app
    err = capsys.readouterr().err
    assert err.startswith("[brutex] ")  # the clean one-liner, not a traceback
    assert "BRUTEX_REVISION_PROBE_TTL_S" in err


def test_construction_fault_logs_and_still_binds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A LoopWatchdog construction fault is best-effort: the dashboard still
    binds with loop_watchdog=None (the liveproof never-crash-boot contract)."""
    probes: dict[str, Any] = {}
    _stub_dashboard_runtime(monkeypatch, tmp_path, probes)
    monkeypatch.delenv(LOOP_WATCHDOG_ENV, raising=False)
    monkeypatch.setenv("BRUTEX_LAKE_ROOT", str(tmp_path))
    lw_mod = importlib.import_module("brutex.observability.loop_watchdog")

    class _Boom:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("no metrics registry")

    monkeypatch.setattr(lw_mod, "LoopWatchdog", _Boom)
    assert cli.cmd_dashboard(None, "127.0.0.1", 9088) == cli.EXIT_OK
    assert probes["loop_watchdog"] is None
    assert probes["ran"] is True
