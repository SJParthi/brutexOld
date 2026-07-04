"""``brutex dashboard`` default-lake resolution test (GROUP 4 · Task 6e, R2-OCLI-F6).

The asymmetry fixed: ``cmd_dashboard`` opened an InstrumentStore ONLY when an
explicit ``--lake-root`` was passed, while every sibling path in the same
function (sweep-progress, liveproof, auto-resume) and ``cmd_run`` resolve the
default lake (``--lake-root`` → ``$BRUTEX_LAKE_ROOT`` → ``~/.brutex/lake``).
Now the store resolution mirrors the siblings: a bare ``brutex dashboard``
opens the store at the resolved default lake.

Offline: uvicorn.run + the instrument auto-load + liveproof + auto-resume are
all faked (same harness shape as tests/unit/test_cli_run.py).
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest
from brutex import cli


def _stub_dashboard_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, probes: dict[str, Any]
) -> None:
    """Stub logging config + autoload + liveproof + auto-resume + uvicorn.run."""
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


def test_dashboard_without_lake_root_resolves_env_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``brutex dashboard`` with NO --lake-root opens the store at $BRUTEX_LAKE_ROOT."""
    pytest.importorskip("fastapi", reason="dashboard needs the [ui] extra")
    pytest.importorskip("uvicorn", reason="dashboard needs the [ui] extra")
    pytest.importorskip("duckdb", reason="the instrument store needs duckdb")
    probes: dict[str, Any] = {}
    _stub_dashboard_runtime(monkeypatch, tmp_path, probes)
    monkeypatch.setenv("BRUTEX_LAKE_ROOT", str(tmp_path))

    rc = cli.cmd_dashboard(None, "127.0.0.1", 9084)

    assert rc == cli.EXIT_OK
    assert probes["port"] == 9084
    # The default lake resolved and a REAL InstrumentStore was opened there —
    # symmetric with the explicit --lake-root path below.
    assert (tmp_path / "instruments.duckdb").exists()


def test_dashboard_with_explicit_lake_root_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit --lake-root keeps opening the store at exactly that path."""
    pytest.importorskip("fastapi", reason="dashboard needs the [ui] extra")
    pytest.importorskip("uvicorn", reason="dashboard needs the [ui] extra")
    pytest.importorskip("duckdb", reason="the instrument store needs duckdb")
    probes: dict[str, Any] = {}
    _stub_dashboard_runtime(monkeypatch, tmp_path, probes)
    explicit = tmp_path / "explicit-lake"
    explicit.mkdir()

    rc = cli.cmd_dashboard(explicit, "127.0.0.1", 9085)

    assert rc == cli.EXIT_OK
    assert probes["port"] == 9085
    assert (explicit / "instruments.duckdb").exists()
