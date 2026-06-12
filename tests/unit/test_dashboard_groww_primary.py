"""Dashboard Groww-PRIMARY routing + spot-pull gate (PR-J3, DEC-DHAN-GROWW-REMIGRATE-001).

PR-J3 of the Dhan → Groww cutover (ADR-0009) flips the dashboard DEFAULT to
Groww: ``GET /credentials`` 307-redirects to the Groww page, and the live
SPOT-pull readiness gate keys on
:func:`brutex.config.credentials.groww_credentials_present` instead of
``dhan_creds_present``. The OPTIONS pull stays on the Dhan gate until J4.

These route tests prove the swap end-to-end against the REAL gate — credentials
are saved to an isolated tmp file (never the function monkeypatched), so the
test exercises the production resolution path. The discriminating tests show
Dhan creds alone do NOT open the spot gate (it now keys on Groww) and Groww
creds alone do NOT open the options gate (it still keys on Dhan). A secret-leak
guard asserts no api key / token / secret value ever reaches a response body.

The injected ``pull_all_spot`` / ``pull_options`` are offline fakes — NEVER a
real network call — so the live (``dry_run=False``) branch is exercised without
touching Dhan / Groww. Skipped cleanly when the ``[ui]`` extra (FastAPI) or
duckdb is absent.
"""

from __future__ import annotations

import importlib
import time
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")
pytest.importorskip("duckdb", reason="the wired-store pull routes need duckdb")

from brutex.config.credentials import (
    ENV_ACCESS_TOKEN,
    ENV_CLIENT_ID,
    ENV_CREDENTIALS_FILE,
    ENV_GROWW_API_KEY,
    ENV_GROWW_TOTP_SECRET,
)
from brutex.dashboard.app import DashboardState, build_app
from brutex.data.instrument_store import InstrumentStore
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient

# The monkeypatch target for the route handlers' module-level pull collaborators.
app_module = importlib.import_module("brutex.dashboard.app")

# FAKE secrets ONLY — never a real credential. The Dhan token is a structurally
# valid (three base64url segments) but meaningless JWT so the save-time shape
# guard accepts it.
_GROWW_API_KEY = "gw_primary_key_abcdef123456"
_GROWW_TOTP = "JBSWY3DPEHPK3PXP"
_DHAN_JWT = "eyJhbGc.eyJzdWImC6.sIg-na_ture"
_DHAN_CLIENT_ID = "1100123456"
#: Every secret VALUE that must never appear verbatim in any response body.
_SECRETS = (_GROWW_API_KEY, _GROWW_TOTP, _DHAN_JWT)


# ────────────────────────────────────────────────────────────────────────
# Fixtures + offline fakes
# ────────────────────────────────────────────────────────────────────────


def _nifty() -> Instrument:
    """Build the admitted NIFTY index instrument fixture."""
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=13,
        symbol_name="NIFTY",
        display_name="NIFTY",
        instrument=InstrumentType.IDX,
        segment=Segment.CASH,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=1,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
    )


class _FakeVerdict:
    """Stub verdict carrying the single method the background ``_work`` reads."""

    def to_dict(self) -> dict[str, Any]:
        """Minimal verdict payload for the banner slot."""
        return {"status": "clean"}


class _FakeReport:
    """Stub pull report — only ``.verdict`` is read on the job thread."""

    @property
    def verdict(self) -> _FakeVerdict:
        """The stub verdict."""
        return _FakeVerdict()


def _fake_pull_all_spot(instruments: list[Instrument], **kwargs: Any) -> _FakeReport:
    """Offline stand-in for the bulk spot pull (no network, returns at once)."""
    return _FakeReport()


def _fake_pull_options(instruments: list[Instrument], **kwargs: Any) -> _FakeReport:
    """Offline stand-in for the options pull (no network, returns at once)."""
    return _FakeReport()


@pytest.fixture
def store(tmp_path: Path) -> Iterator[InstrumentStore]:
    """A real DuckDB-backed store with one admitted index (NIFTY)."""
    instrument_store = InstrumentStore.open(tmp_path / "instruments.duckdb")
    instrument_store.upsert_many(
        [_nifty()],
        snapshot_date=date(2024, 1, 2),
        run_id="r1",
        csv_source_sha256="0" * 64,
    )
    yield instrument_store
    instrument_store.close()


@pytest.fixture
def client(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[TestClient]:
    """A wired TestClient with an isolated, empty credentials file + offline pulls.

    Both vendors' env vars are cleared so the gate resolves ONLY from the
    isolated tmp file the routes write — making every assertion deterministic
    regardless of the ambient CI environment.
    """
    monkeypatch.setenv(ENV_CREDENTIALS_FILE, str(tmp_path / "credentials.json"))
    for var in (ENV_GROWW_API_KEY, ENV_GROWW_TOTP_SECRET, ENV_ACCESS_TOKEN, ENV_CLIENT_ID):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(app_module, "pull_all_spot", _fake_pull_all_spot)
    monkeypatch.setattr(app_module, "pull_options", _fake_pull_options)
    with TestClient(build_app(DashboardState(), instrument_store=store)) as c:
        yield c


def _save_groww(client: TestClient) -> None:
    """Persist the fake Groww credentials to the isolated tmp file."""
    resp = client.post(
        "/api/v1/groww/credentials",
        json={"api_key": _GROWW_API_KEY, "totp_secret": _GROWW_TOTP},
    )
    assert resp.status_code == 200


def _save_dhan(client: TestClient) -> None:
    """Persist the fake Dhan credentials to the isolated tmp file."""
    resp = client.post(
        "/api/v1/credentials",
        json={"access_token": _DHAN_JWT, "client_id": _DHAN_CLIENT_ID},
    )
    assert resp.status_code == 200


def _drain_job(client: TestClient, *, timeout: float = 10.0) -> None:
    """Poll /pull-progress until the single-flight background job settles."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = client.get("/api/v1/universe/pull-progress").json()
        assert isinstance(snap, dict)
        if snap["state"] in {"done", "error"}:
            return
        time.sleep(0.01)
    raise AssertionError("background pull job did not settle within the timeout")


# ────────────────────────────────────────────────────────────────────────
# /credentials redirect — Groww is now the primary surface
# ────────────────────────────────────────────────────────────────────────


def test_credentials_redirects_to_groww_and_legacy_dhan_api_survives(client: TestClient) -> None:
    """GET /credentials 307-redirects to Groww; the legacy Dhan API survives till J4."""
    redirect = client.get("/credentials", follow_redirects=False)
    assert redirect.status_code == 307
    assert redirect.headers["location"] == "/groww-credentials"
    # The Dhan credential status API is KEPT (J4 removes it) — proves J3 only
    # re-routed the page default, it did not delete the Dhan backend.
    status = client.get("/api/v1/credentials/status")
    assert status.status_code == 200
    assert status.json()["present"] is False


# ────────────────────────────────────────────────────────────────────────
# SPOT-pull gate now keys on GROWW
# ────────────────────────────────────────────────────────────────────────


def test_spot_pull_dry_run_without_groww_creds(client: TestClient) -> None:
    """No Groww creds → the spot pull is forced offline (dry_run=True)."""
    resp = client.post("/api/v1/universe/pull-all", params={"kind": "index"})
    assert resp.status_code == 200
    assert resp.json()["dry_run"] is True
    _drain_job(client)


def test_spot_pull_live_with_groww_creds(client: TestClient) -> None:
    """Groww creds present → the spot gate opens (dry_run=False)."""
    _save_groww(client)
    resp = client.post("/api/v1/universe/pull-all", params={"kind": "index"})
    assert resp.status_code == 200
    assert resp.json()["dry_run"] is False
    _drain_job(client)


def test_spot_pull_ignores_dhan_creds(client: TestClient) -> None:
    """Discriminator: Dhan creds ALONE do NOT open the spot gate (it keys on Groww)."""
    _save_dhan(client)
    resp = client.post("/api/v1/universe/pull-all", params={"kind": "index"})
    assert resp.status_code == 200
    assert resp.json()["dry_run"] is True
    _drain_job(client)


# ────────────────────────────────────────────────────────────────────────
# OPTIONS-pull gate is UNCHANGED — still keys on DHAN (J4 migrates it)
# ────────────────────────────────────────────────────────────────────────


def test_options_pull_dry_run_with_only_groww_creds(client: TestClient) -> None:
    """Groww creds alone do NOT open the options gate (it still keys on Dhan)."""
    _save_groww(client)
    resp = client.post("/api/v1/universe/pull-options", params={"kind": "index"})
    assert resp.status_code == 200
    assert resp.json()["dry_run"] is True
    _drain_job(client)


def test_options_pull_live_with_dhan_creds(client: TestClient) -> None:
    """Dhan creds present → the options gate opens (dry_run=False) — unchanged from pre-J3."""
    _save_dhan(client)
    resp = client.post("/api/v1/universe/pull-options", params={"kind": "index"})
    assert resp.status_code == 200
    assert resp.json()["dry_run"] is False
    _drain_job(client)


# ────────────────────────────────────────────────────────────────────────
# Secret-leak guard — no credential value ever reaches a response body
# ────────────────────────────────────────────────────────────────────────


def test_no_secret_leaks_across_the_full_groww_primary_flow(client: TestClient) -> None:
    """Save both vendors, run both pulls, follow the redirect — no secret leaks anywhere."""
    bodies: list[str] = []
    bodies.append(
        client.post(
            "/api/v1/groww/credentials",
            json={"api_key": _GROWW_API_KEY, "totp_secret": _GROWW_TOTP},
        ).text
    )
    bodies.append(
        client.post(
            "/api/v1/credentials",
            json={"access_token": _DHAN_JWT, "client_id": _DHAN_CLIENT_ID},
        ).text
    )
    bodies.append(client.get("/api/v1/groww/credentials/status").text)
    bodies.append(client.get("/api/v1/credentials/status").text)

    spot = client.post("/api/v1/universe/pull-all", params={"kind": "index"})
    bodies.append(spot.text)
    _drain_job(client)

    options = client.post("/api/v1/universe/pull-options", params={"kind": "index"})
    bodies.append(options.text)
    _drain_job(client)

    # Following the redirect renders the Groww page HTML — it must not echo a secret.
    bodies.append(client.get("/credentials").text)

    for body in bodies:
        for secret in _SECRETS:
            assert secret not in body, "a credential value leaked into a response body"
