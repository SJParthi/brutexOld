# pyright: reportPrivateUsage=none
"""``cmd_dashboard``'s pull-intent wiring (DEC-PULL-INTENT-RESUME-001).

Drives the REAL :func:`brutex.cli.cmd_dashboard` with the established stubbed
runtime (logging / autoload / liveproof / uvicorn.run replaced — the same
pattern as ``test_cli_dashboard_allowed_hosts``) and pins the three new wires:

* a :class:`~brutex.dashboard.pull_intent.PullIntentStore` for the RESOLVED
  lake root is built and threaded into ``build_app`` (so the intent sidecar
  and the pull's account single-flight guard the SAME lake);
* the extended auto-resume launcher receives BOTH the store and the app's
  ``app.state.brutex_resume_pipeline`` seam (the ONE decision point);
* a store construction failure is best-effort: logged, ``build_app`` gets
  ``None``, the resume seam stays unwired, and the boot still binds.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest
from brutex import cli

pytest.importorskip("fastapi", reason="dashboard needs the [ui] extra")
pytest.importorskip("uvicorn", reason="dashboard needs the [ui] extra")
pytest.importorskip("duckdb", reason="the instrument store needs duckdb")

from brutex.dashboard.pull_intent import PullIntentStore


def _stub_dashboard_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, probes: dict[str, Any]
) -> None:
    """Stub logging + autoload + liveproof + lake-heal + uvicorn.run; spy the seams."""
    obs_logging = importlib.import_module("brutex.observability.logging")
    monkeypatch.setattr(obs_logging, "configure_logging", lambda **kwargs: tmp_path / "brutex.log")
    uvicorn_mod = importlib.import_module("uvicorn")
    monkeypatch.setattr(uvicorn_mod, "run", lambda app, *, host, port, log_config: None)
    monkeypatch.setattr(cli, "_start_instrument_autoload", lambda store, log: None)
    monkeypatch.setattr(cli, "_build_liveproof_autorun", lambda _root: None)

    def _spy_resume_launcher(lake_root: Path, log: Any, **kwargs: Any) -> None:
        probes["resume_kwargs"] = dict(kwargs)
        probes["resume_lake_root"] = lake_root

    monkeypatch.setattr(cli, "_start_auto_resume_on_boot", _spy_resume_launcher)

    app_mod = importlib.import_module("brutex.dashboard.app")
    real_build_app = app_mod.build_app

    def spy_build_app(*args: Any, **kwargs: Any) -> Any:
        probes["pull_intent_store"] = kwargs.get("pull_intent_store")
        app = real_build_app(*args, **kwargs)
        probes["app"] = app
        return app

    monkeypatch.setattr(app_mod, "build_app", spy_build_app)


def test_cmd_dashboard_builds_and_threads_the_intent_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probes: dict[str, Any] = {}
    _stub_dashboard_runtime(monkeypatch, tmp_path, probes)

    rc = cli.cmd_dashboard(tmp_path, "127.0.0.1", 9095)

    assert rc == cli.EXIT_OK
    intent_store = probes["pull_intent_store"]
    assert isinstance(intent_store, PullIntentStore)
    assert intent_store.lake_root == tmp_path, "the intent guards the RESOLVED lake root"
    # The ONE decision point is wired: the launcher got the store AND the seam.
    assert probes["resume_kwargs"]["intent_store"] is intent_store
    seam = probes["resume_kwargs"]["resume_pipeline"]
    assert seam is probes["app"].state.brutex_resume_pipeline
    # DEC-AUTO-RESUME-INPROCESS-001: the legacy arm's IN-PROCESS seam is wired
    # beside it — the resume runs through the app's own launcher, never a child.
    legacy_seam = probes["resume_kwargs"]["resume_legacy"]
    assert legacy_seam is probes["app"].state.brutex_resume_legacy
    assert probes["resume_lake_root"] == tmp_path


def test_cmd_dashboard_store_build_failure_degrades_and_still_binds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probes: dict[str, Any] = {}
    _stub_dashboard_runtime(monkeypatch, tmp_path, probes)
    pull_intent_mod = importlib.import_module("brutex.dashboard.pull_intent")

    class _BoomStore:
        def __init__(self, _lake_root: Path) -> None:
            raise RuntimeError("store construction exploded")

    monkeypatch.setattr(pull_intent_mod, "PullIntentStore", _BoomStore)

    rc = cli.cmd_dashboard(tmp_path, "127.0.0.1", 9095)

    assert rc == cli.EXIT_OK, "a failed intent-store build must never block the bind"
    assert probes["pull_intent_store"] is None
    # No store ⇒ the app never exposes the seam ⇒ the launcher gets None (legacy).
    assert probes["resume_kwargs"]["intent_store"] is None
    assert probes["resume_kwargs"]["resume_pipeline"] is None
    # No store ⇒ no in-process legacy seam either: the launcher's legacy arm
    # then reports the honest `error` verdict — never a DOA subprocess child
    # (DEC-AUTO-RESUME-INPROCESS-001).
    assert probes["resume_kwargs"]["resume_legacy"] is None
    assert not hasattr(probes["app"].state, "brutex_resume_pipeline")
    assert not hasattr(probes["app"].state, "brutex_resume_legacy")
