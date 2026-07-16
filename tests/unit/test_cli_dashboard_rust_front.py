"""``cmd_dashboard`` ↔ ``rust_front`` wiring — the thin CLI delta.

MOVED verbatim out of ``tests/unit/test_rust_front.py`` so the
coverage-gate discovery (``scripts/ci/coverage_gate_discovery.py``) maps
these tests to ``brutex/cli.py``: the discovery gate maps a module ONLY to
its stem-matching ``test_cli*`` files, so while these tests lived in the
``test_rust_front`` stem the cli.py scoped gate ran WITHOUT them and the
rust-front lines in ``cmd_dashboard`` read as uncovered (the CI 99.59%
failure this file fixes). The module-level ``rust_front`` behaviour tests
stay in ``test_rust_front.py`` (its own self-contained 100% gate).

Covers the ``cmd_dashboard`` rust-front seam:

* the FLAG-OFF IDENTITY PIN: with the flag absent, ``cmd_dashboard`` runs
  uvicorn on the operator host:port exactly as before (no brutexd spawn,
  no private port) — byte-zero behaviour change;
* junk ``BRUTEX_RUST_FRONT`` → loud ``CliUserError`` (exit 1) BEFORE any
  heavy setup;
* flag ON → delegation to ``run_with_rust_front`` (operator host:port
  threaded; the direct uvicorn.run tail NOT taken);
* a launcher ``RustFrontError`` (missing binary) → ``CliUserError``.

Offline: uvicorn.run + the instrument auto-load + liveproof + auto-resume
are all faked (same harness shape as tests/unit/test_cli_dashboard_default_lake.py).
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest
from brutex import cli
from brutex.dashboard import rust_front
from brutex.dashboard.rust_front import ENV_RUST_FRONT, RustFrontError

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
