"""Groww credentials + probe routes (DEC-DHAN-GROWW-REMIGRATE-001).

Mirrors ``tests/unit/test_dashboard_credentials_route.py``: an env-redirected
real store + a ``TestClient``. Covers all four Groww routes including every
error branch, and the Test/verify route with ``build_live_client`` / ``run_probe``
monkeypatched to fakes (success + auth-fail + other-error + missing-creds). A
secret-leak guard asserts the api key / secret never appear in any response body.
Skipped cleanly when the ``[ui]`` extra (FastAPI) is absent.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")

from brutex.config.credentials import ENV_CREDENTIALS_FILE
from brutex.dashboard.app import DashboardState, build_app
from fastapi.testclient import TestClient

# FAKE values only — never a real secret.
_API_KEY = "gw_api_key_abcdef123456"
_TOTP_SECRET = "JBSWY3DPEHPK3PXP"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[TestClient]:
    """A TestClient whose credential saves land in an isolated tmp file."""
    monkeypatch.setenv(ENV_CREDENTIALS_FILE, str(tmp_path / "credentials.json"))
    # Make sure no real env credentials leak into the resolve-from-stored path.
    monkeypatch.delenv("BRUTEX_GROWW_API_KEY", raising=False)
    monkeypatch.delenv("BRUTEX_GROWW_TOTP_SECRET", raising=False)
    with TestClient(build_app(DashboardState(), instrument_store=None)) as c:
        yield c


def _save(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/groww/credentials",
        json={"api_key": _API_KEY, "totp_secret": _TOTP_SECRET},
    )
    assert resp.status_code == 200


# ── save + status ───────────────────────────────────────────────────────────


def test_save_returns_masked_preview(client: TestClient, tmp_path: Path) -> None:
    resp = client.post(
        "/api/v1/groww/credentials",
        json={"api_key": _API_KEY, "totp_secret": _TOTP_SECRET},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["present"] is True
    assert body["api_key_len"] == len(_API_KEY)
    # No usable secret ever echoed back.
    assert _API_KEY not in str(body)
    assert _TOTP_SECRET not in str(body)
    assert (tmp_path / "credentials.json").exists()


def test_blank_api_key_maps_to_400(client: TestClient, tmp_path: Path) -> None:
    resp = client.post(
        "/api/v1/groww/credentials",
        json={"api_key": "", "totp_secret": _TOTP_SECRET},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "api_key must not be blank"
    assert not (tmp_path / "credentials.json").exists()


def test_status_flips_absent_to_present(client: TestClient) -> None:
    before = client.get("/api/v1/groww/credentials/status")
    assert before.status_code == 200
    assert before.json()["present"] is False
    _save(client)
    after = client.get("/api/v1/groww/credentials/status")
    assert after.status_code == 200
    body = after.json()
    assert body["present"] is True
    assert _API_KEY not in str(body)
    assert _TOTP_SECRET not in str(body)


# ── HTML page ───────────────────────────────────────────────────────────────


def test_groww_credentials_page_serves_html(client: TestClient) -> None:
    resp = client.get("/groww-credentials")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "/api/v1/groww/credentials" in resp.text
    assert "/api/v1/groww/credentials/test" in resp.text
    assert "Test / verify" in resp.text


# ── Test / verify route ─────────────────────────────────────────────────────


def _patch_probe(
    monkeypatch: pytest.MonkeyPatch,
    *,
    build: object = None,
    run: object = None,
) -> None:
    """Monkeypatch the lazily-imported probe seam on its source modules."""
    live_mod = importlib.import_module("brutex.adapters.groww.live")
    probe_mod = importlib.import_module("brutex.adapters.groww.probe")

    if build is not None:
        monkeypatch.setattr(live_mod, "build_live_client", build)
    if run is not None:
        monkeypatch.setattr(probe_mod, "run_probe", run)


def test_test_route_success(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from brutex.adapters.groww.probe import ProbeResult

    _save(client)

    sentinel_client = object()
    _patch_probe(
        monkeypatch,
        build=lambda creds: sentinel_client,
        run=lambda c: [
            ProbeResult(name="vix", verdict="OK", detail="present", symbol="NSE-INDIAVIX", rows=10),
            ProbeResult(name="granularity", verdict="ABSENT", detail="empty", rows=0),
        ],
    )
    resp = client.post("/api/v1/groww/credentials/test")
    assert resp.status_code == 200
    body = resp.json()
    assert [r["name"] for r in body["results"]] == ["vix", "granularity"]
    assert body["results"][0]["verdict"] == "OK"
    assert body["results"][0]["symbol"] == "NSE-INDIAVIX"


def test_test_route_missing_creds_400(client: TestClient) -> None:
    # No credentials saved → resolve raises CredentialsError → 400.
    resp = client.post("/api/v1/groww/credentials/test")
    assert resp.status_code == 400
    assert "missing Groww credentials" in resp.json()["detail"]


def test_test_route_auth_failure_400_redacted(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from brutex.adapters.groww.errors import GrowwAuthError

    _save(client)

    def boom(creds: object) -> object:
        raise GrowwAuthError(
            f"failed with secret {_TOTP_SECRET}",
            safe_message="Groww access-token issuance failed",
        )

    _patch_probe(monkeypatch, build=boom)
    resp = client.post("/api/v1/groww/credentials/test")
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail == "Groww access-token issuance failed"
    # The raw secret must never reach the response.
    assert _TOTP_SECRET not in resp.text
    assert _API_KEY not in resp.text


def test_test_route_other_groww_error_502(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from brutex.adapters.groww.errors import GrowwError, GrowwRateLimitError

    _save(client)

    def build_ok(creds: object) -> object:
        return object()

    def run_boom(c: object) -> object:
        raise GrowwRateLimitError("rate limited")

    _patch_probe(monkeypatch, build=build_ok, run=run_boom)
    resp = client.post("/api/v1/groww/credentials/test")
    assert resp.status_code == 502
    assert "rate limited" in resp.json()["detail"]
    # GrowwRateLimitError is a GrowwError (sanity for the except chain).
    assert issubclass(GrowwRateLimitError, GrowwError)


# ── template loader (whitebox: both cache branches + key UX strings) ─────────


def test_groww_template_loader_caches_both_branches() -> None:
    """The cached template loader covers the load branch AND the cached branch."""
    app_mod = importlib.import_module("brutex.dashboard.app")

    # Force the cold (cache-is-None) branch, then the warm (cached) branch.
    app_mod._GROWW_CREDENTIALS_TEMPLATE_CACHE = None
    cold = app_mod._render_groww_credentials_html()
    warm = app_mod._render_groww_credentials_html()
    assert cold == warm
    # Key UX strings the page must carry.
    assert "Test / verify" in cold
    assert "/api/v1/groww/credentials/test" in cold
    assert cold.count('class="eye"') == 2
    assert 'data-target="api_key"' in cold
    assert 'data-target="totp_secret"' in cold
