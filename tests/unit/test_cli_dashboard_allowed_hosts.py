"""``brutex dashboard --host`` → ``build_app(allowed_hosts=…)`` wiring (R2-DS-F1).

The verify-stage MAJOR fixed here: ``cmd_dashboard`` called ``build_app``
WITHOUT ``allowed_hosts`` although ``--host`` is an advertised flag, so a
non-loopback bind (e.g. ``0.0.0.0`` / a LAN address) hard-400'd every remote
client at the TrustedHost DNS-rebinding guard. ``_dashboard_allowed_hosts``
now classifies the bound host (loopback → keep the strict default ·
concrete → extend with that host · wildcard → disable the Host guard,
loudly) and ``cmd_dashboard`` threads the result through ``build_app``'s
documented CLI seam.

Offline: uvicorn.run + the instrument auto-load + liveproof + auto-resume
are all faked (same harness shape as tests/unit/test_cli_dashboard_default_lake.py).
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest
from brutex import cli
from brutex.dashboard.app import DEFAULT_ALLOWED_HOSTS

# ── pure helper — every branch ───────────────────────────────────────────────


class TestDashboardAllowedHosts:
    """`_dashboard_allowed_hosts` classification of the bound host."""

    @pytest.mark.parametrize(
        "host",
        ["127.0.0.1", "localhost", "LOCALHOST", "::1", "[::1]", "  127.0.0.1  ", ""],
    )
    def test_loopback_binds_keep_default(self, host: str) -> None:
        """A loopback (or blank) bind keeps build_app's loopback-only default."""
        assert cli._dashboard_allowed_hosts(host) is None

    @pytest.mark.parametrize("host", ["0.0.0.0", "::", "[::]", "0.0.0.0 "])
    def test_wildcard_binds_disable_host_guard(self, host: str) -> None:
        """A wildcard bind cannot know the client-facing address → guard off."""
        assert cli._dashboard_allowed_hosts(host) == ["*"]

    def test_concrete_bind_extends_default(self) -> None:
        """A concrete LAN bind admits that host (any port) ON TOP of loopback."""
        resolved = cli._dashboard_allowed_hosts("192.168.1.50")
        assert resolved == [*DEFAULT_ALLOWED_HOSTS, "192.168.1.50:*"]

    def test_concrete_bind_normalized(self) -> None:
        """Case + whitespace + IPv6 brackets are normalized before matching."""
        assert cli._dashboard_allowed_hosts(" MyHost.LAN ") == [
            *DEFAULT_ALLOWED_HOSTS,
            "myhost.lan:*",
        ]


# ── cmd_dashboard wiring — the seam is actually passed ───────────────────────


def _stub_dashboard_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, probes: dict[str, Any]
) -> None:
    """Stub logging config + autoload + liveproof + auto-resume + uvicorn.run + build_app."""
    obs_logging = importlib.import_module("brutex.observability.logging")
    monkeypatch.setattr(obs_logging, "configure_logging", lambda **kwargs: tmp_path / "brutex.log")
    uvicorn_mod = importlib.import_module("uvicorn")

    def fake_run(app: Any, *, host: str, port: int, log_config: Any) -> None:
        probes["host"] = host
        probes["port"] = port

    monkeypatch.setattr(uvicorn_mod, "run", fake_run)
    monkeypatch.setattr(cli, "_start_instrument_autoload", lambda store, log: None)
    monkeypatch.setattr(cli, "_start_auto_resume_on_boot", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "_build_liveproof_autorun", lambda _root: None)

    app_mod = importlib.import_module("brutex.dashboard.app")
    real_build_app = app_mod.build_app

    def spy_build_app(*args: Any, **kwargs: Any) -> Any:
        probes["allowed_hosts"] = kwargs.get("allowed_hosts")
        return real_build_app(*args, **kwargs)

    monkeypatch.setattr(app_mod, "build_app", spy_build_app)


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", None),
        ("0.0.0.0", ["*"]),
        ("192.168.1.50", [*DEFAULT_ALLOWED_HOSTS, "192.168.1.50:*"]),
    ],
)
def test_cmd_dashboard_threads_allowed_hosts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host: str,
    expected: list[str] | None,
) -> None:
    """`cmd_dashboard` passes the resolved allowlist into build_app per bound host."""
    pytest.importorskip("fastapi", reason="dashboard needs the [ui] extra")
    pytest.importorskip("uvicorn", reason="dashboard needs the [ui] extra")
    pytest.importorskip("duckdb", reason="the instrument store needs duckdb")
    probes: dict[str, Any] = {}
    _stub_dashboard_runtime(monkeypatch, tmp_path, probes)

    rc = cli.cmd_dashboard(tmp_path, host, 9095)

    assert rc == cli.EXIT_OK
    assert probes["allowed_hosts"] == expected


def test_lan_client_host_header_admitted_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The empirical repro from the finding: Host `192.168.1.50:9095` gets 200, not 400."""
    fastapi_testclient = pytest.importorskip(
        "fastapi.testclient", reason="dashboard needs the [ui] extra"
    )
    from brutex.dashboard.app import DashboardState, build_app

    app = build_app(
        DashboardState(),
        instrument_store=None,
        allowed_hosts=cli._dashboard_allowed_hosts("192.168.1.50"),
    )
    with fastapi_testclient.TestClient(app) as client:
        ok = client.get("/api/v1/state", headers={"Host": "192.168.1.50:9095"})
        assert ok.status_code == 200
        # Foreign / rebound Hosts still refuse — the guard is extended, not off.
        refused = client.get("/api/v1/state", headers={"Host": "evil.example"})
        assert refused.status_code == 400
