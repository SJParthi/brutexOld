"""``brutex dashboard`` auto-pull-scheduler binding (DEC-PULL-SCOPED-ALL-ONECLICK-001, round-5 fold).

The production binding law (the loop_watchdog pattern, mirrored):
``cmd_dashboard`` resolves the fail-loud ``BRUTEX_AUTO_PULL_TICK_S`` cadence
FIRST (junk aborts startup with a clean one-liner — a typo must never
silently change the cadence), then best-effort-constructs an
:class:`~brutex.dashboard.auto_pull_scheduler.AutoPullScheduler` over the
resolved lake root's :class:`AutoPullStateStore` (a construction fault logs
and the dashboard still binds — the button then runs without the auto-arm
surface) and threads it into ``build_app``.

Offline: uvicorn.run + the instrument auto-load + liveproof + auto-resume are
all faked (the ``test_cli_dashboard_loop_watchdog`` harness shape).
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest
from brutex import cli
from brutex.dashboard.auto_pull_scheduler import (
    AUTO_PULL_STATE_FILENAME,
    AUTO_PULL_TICK_ENV,
    AutoPullScheduler,
)


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
    # Capture the auto_pull_scheduler build_app receives (the binding under test).
    app_mod = importlib.import_module("brutex.dashboard.app")
    real_build = app_mod.build_app

    def _spy_build(*args: Any, **kwargs: Any) -> Any:
        probes["auto_pull_scheduler"] = kwargs.get("auto_pull_scheduler")
        return real_build(*args, **kwargs)

    monkeypatch.setattr(app_mod, "build_app", _spy_build)


def test_default_env_binds_a_scheduler_on_the_lake_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unset env: build_app receives a real AutoPullScheduler whose durable
    state sidecar lives under THIS lake root (the pull_intent_store twin)."""
    probes: dict[str, Any] = {}
    _stub_dashboard_runtime(monkeypatch, tmp_path, probes)
    monkeypatch.delenv(AUTO_PULL_TICK_ENV, raising=False)
    monkeypatch.setenv("BRUTEX_LAKE_ROOT", str(tmp_path))
    assert cli.cmd_dashboard(None, "127.0.0.1", 9185) == cli.EXIT_OK
    scheduler = probes["auto_pull_scheduler"]
    assert isinstance(scheduler, AutoPullScheduler)
    # The sidecar path is lake-root-keyed (arm() would persist right here).
    assert scheduler._store.path == tmp_path / AUTO_PULL_STATE_FILENAME  # pyright: ignore[reportPrivateUsage]
    assert probes["ran"] is True


def test_junk_tick_env_fails_loud_before_bind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Junk BRUTEX_AUTO_PULL_TICK_S: a clean EXIT_USER_ERROR one-liner BEFORE
    uvicorn ever runs (the resolve_* fail-loud house pattern)."""
    probes: dict[str, Any] = {}
    _stub_dashboard_runtime(monkeypatch, tmp_path, probes)
    monkeypatch.setenv(AUTO_PULL_TICK_ENV, "every-minute")
    monkeypatch.setenv("BRUTEX_LAKE_ROOT", str(tmp_path))
    assert cli.cmd_dashboard(None, "127.0.0.1", 9186) == cli.EXIT_USER_ERROR
    assert "ran" not in probes  # uvicorn.run never reached
    assert AUTO_PULL_TICK_ENV in capsys.readouterr().err


def test_junk_settle_env_fails_loud_before_bind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """[refuter round 6 M-2] junk BRUTEX_PULL_ALL_SETTLE_IST: the SAME clean
    EXIT_USER_ERROR one-liner as the tick env, BEFORE uvicorn ever runs.
    Pre-fix an ARMED dashboard booted fine and the junk surfaced only as
    per-tick gate errors (the scheduler never fired)."""
    from brutex.calendars.auto_pull_gate import PULL_ALL_SETTLE_ENV

    probes: dict[str, Any] = {}
    _stub_dashboard_runtime(monkeypatch, tmp_path, probes)
    monkeypatch.delenv(AUTO_PULL_TICK_ENV, raising=False)
    monkeypatch.setenv(PULL_ALL_SETTLE_ENV, "half past five")
    monkeypatch.setenv("BRUTEX_LAKE_ROOT", str(tmp_path))
    assert cli.cmd_dashboard(None, "127.0.0.1", 9189) == cli.EXIT_USER_ERROR
    assert "ran" not in probes  # uvicorn.run never reached
    assert PULL_ALL_SETTLE_ENV in capsys.readouterr().err


def test_construction_fault_logs_and_still_binds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scheduler construction fault is best-effort: the dashboard still
    binds with auto_pull_scheduler=None (the never-crash-boot contract; the
    button then runs without the auto-arm surface)."""
    probes: dict[str, Any] = {}
    _stub_dashboard_runtime(monkeypatch, tmp_path, probes)
    monkeypatch.delenv(AUTO_PULL_TICK_ENV, raising=False)
    monkeypatch.setenv("BRUTEX_LAKE_ROOT", str(tmp_path))
    aps_mod = importlib.import_module("brutex.dashboard.auto_pull_scheduler")

    class _Boom:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("state store exploded")

    monkeypatch.setattr(aps_mod, "AutoPullScheduler", _Boom)
    assert cli.cmd_dashboard(None, "127.0.0.1", 9187) == cli.EXIT_OK
    assert probes["auto_pull_scheduler"] is None
    assert probes["ran"] is True
