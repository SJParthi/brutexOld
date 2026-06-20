"""Dashboard Groww-PRIMARY spot-pull gate (PR-J3, DEC-DHAN-GROWW-REMIGRATE-001).

PR-J3 of the Dhan → Groww cutover (ADR-0009) flipped the dashboard DEFAULT to
Groww: the live SPOT-pull readiness gate keys on
:func:`brutex.config.credentials.groww_credentials_present`. PR-J4c then darkened
the vestigial Dhan surface (the options pull, the Dhan credentials routes, and
the ``/credentials`` redirect were removed), so this file now covers only the
surviving Groww spot-pull gate.

These route tests prove the gate end-to-end against the REAL credential
resolution — credentials are saved to an isolated tmp file (never the function
monkeypatched), so the test exercises the production resolution path.

The injected ``pull_all_spot`` is an offline fake — NEVER a real network call —
so the live (``dry_run=False``) branch is exercised without touching Groww.
Skipped cleanly when the ``[ui]`` extra (FastAPI) or duckdb is absent.
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

# FAKE secrets ONLY — never a real credential.
_GROWW_API_KEY = "gw_primary_key_abcdef123456"
_GROWW_TOTP = "JBSWY3DPEHPK3PXP"


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
    with TestClient(build_app(DashboardState(), instrument_store=store)) as c:
        yield c


def _save_groww(client: TestClient) -> None:
    """Persist the fake Groww credentials to the isolated tmp file."""
    resp = client.post(
        "/api/v1/groww/credentials",
        json={"api_key": _GROWW_API_KEY, "totp_secret": _GROWW_TOTP},
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
# SPOT-pull gate keys on GROWW
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


def test_spot_interval_param_is_ignored_always_one_minute(client: TestClient) -> None:
    """The vestigial ``spot_interval`` param is IGNORED — always 1-minute only.

    DEC-1MINUTE-ONLY-DERIVE-1DAY-001: 1-minute is the ONLY granularity fetched (1-day is
    derived from it), so ``resolve_fetch_intervals`` always returns ``("1minute",)``
    regardless of the legacy ``spot_interval`` value. The route's reported ``total`` is
    symbols × intervals, so EVERY value (default / ``both`` / ``1minute``) gives the SAME
    one-interval-per-symbol total — the param no longer scopes the pull.
    """
    default = client.post("/api/v1/universe/pull-all", params={"kind": "index"})
    assert default.status_code == 200
    default_total = default.json()["total"]
    _drain_job(client)
    both = client.post(
        "/api/v1/universe/pull-all", params={"kind": "index", "spot_interval": "both"}
    )
    assert both.status_code == 200
    # ``both`` is IGNORED — still 1-minute only → the SAME total (not doubled).
    assert both.json()["total"] == default_total
    _drain_job(client)
    minute = client.post(
        "/api/v1/universe/pull-all", params={"kind": "index", "spot_interval": "1minute"}
    )
    assert minute.json()["total"] == default_total
    _drain_job(client)


# ────────────────────────────────────────────────────────────────────────
# Secret-leak guard — no credential value ever reaches a response body
# ────────────────────────────────────────────────────────────────────────


def test_no_secret_leaks_across_the_groww_spot_flow(client: TestClient) -> None:
    """Save the Groww creds + run a spot pull — no secret leaks into any response body."""
    secrets = (_GROWW_API_KEY, _GROWW_TOTP)
    bodies: list[str] = []
    bodies.append(
        client.post(
            "/api/v1/groww/credentials",
            json={"api_key": _GROWW_API_KEY, "totp_secret": _GROWW_TOTP},
        ).text
    )
    bodies.append(client.get("/api/v1/groww/credentials/status").text)

    spot = client.post("/api/v1/universe/pull-all", params={"kind": "index"})
    bodies.append(spot.text)
    _drain_job(client)

    for body in bodies:
        for secret in secrets:
            assert secret not in body, "a credential value leaked into a response body"
