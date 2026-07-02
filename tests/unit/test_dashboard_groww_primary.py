"""Dashboard Groww-PRIMARY spot-pull gate (DEC-AWS-ONLY-CREDS-001).

The live SPOT-pull gate keys on
:func:`brutex.config.credentials.groww_credentials_error`, which resolves the Groww
credentials from AWS Parameter Store — the SOLE source (the in-app credentials webpage +
the env-var / local-file paths were removed). These route tests prove the gate end-to-end
against the REAL ``groww_credentials_error`` → ``resolve_groww_aws_credentials`` path, with
the AWS reader (``load_groww_aws_credentials``) monkeypatched to an in-memory fake so the
test needs NO real AWS and NO network — present ⇒ live; absent ⇒ a LOUD 503 REFUSAL
relaying the CredentialsError's own fix text (DEC-PULL-LOUD-CREDENTIAL-REFUSAL-001 —
never the old silent synthetic downgrade); ``?dry_run=true`` ⇒ the explicit,
loudly-labeled synthetic run.

The injected ``pull_all_spot`` is an offline fake — NEVER a real network call —
so the live (``dry_run=False``) branch is exercised without touching Groww.
Skipped cleanly when the ``[ui]`` extra (FastAPI) or duckdb is absent.
"""

from __future__ import annotations

import importlib
import threading
import time
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")
pytest.importorskip("duckdb", reason="the wired-store pull routes need duckdb")

from brutex.config.credentials import CredentialsError, GrowwFileCredentials
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


#: Module-level toggle the fake AWS reader consults so a test can flip creds present/absent
#: WITHOUT rebuilding the app (the reader is monkeypatched once in the fixture).
_AWS_CREDS_PRESENT = {"on": False}


def _fake_aws_loader() -> GrowwFileCredentials:
    """Stand-in for ``load_groww_aws_credentials`` — present ⇒ creds, absent ⇒ fail-fast."""
    if not _AWS_CREDS_PRESENT["on"]:
        raise CredentialsError("no Groww credentials in AWS Parameter Store (test)")
    return GrowwFileCredentials(api_key=_GROWW_API_KEY, totp_secret=_GROWW_TOTP)


@pytest.fixture
def client(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[TestClient]:
    """A wired TestClient with the AWS creds reader faked (absent by default) + offline pulls.

    ``load_groww_aws_credentials`` is monkeypatched to :func:`_fake_aws_loader`, so the
    ``groww_credentials_present`` gate resolves ONLY from the in-memory toggle — making every
    assertion deterministic regardless of the ambient CI environment and needing NO real AWS.
    """
    _AWS_CREDS_PRESENT["on"] = False
    monkeypatch.setattr(
        "brutex.config.credentials_aws.load_groww_aws_credentials", _fake_aws_loader
    )
    monkeypatch.setattr(app_module, "pull_all_spot", _fake_pull_all_spot)
    with TestClient(build_app(DashboardState(), instrument_store=store)) as c:
        yield c


def _save_groww(client: TestClient) -> None:
    """Flip the fake AWS reader to report the Groww credentials PRESENT (Lambda-seeded)."""
    _AWS_CREDS_PRESENT["on"] = True


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


def test_spot_pull_refuses_loudly_without_groww_creds(client: TestClient) -> None:
    """No Groww creds → the REAL spot pull REFUSES with the relayed fix text.

    DEC-PULL-LOUD-CREDENTIAL-REFUSAL-001 (audit finding R1): the route used to
    silently downgrade to a synthetic dry-run and report the pull GREEN with
    counts while ZERO real bars landed. Now it must 503 with the
    CredentialsError's own message, and the /pull-verdict banner must carry the
    red ``credentials_missing`` needs-operator verdict.
    """
    resp = client.post("/api/v1/universe/pull-all", params={"kind": "index"})
    assert resp.status_code == 503
    detail = resp.json()["detail"]
    # The refusal headline + the CredentialsError's own fix text, relayed verbatim.
    assert "Pull REFUSED — Groww credentials unavailable" in detail
    assert "no Groww credentials in AWS Parameter Store (test)" in detail
    # The banner slot carries the RED needs-operator refusal verdict.
    verdict = client.get("/api/v1/universe/pull-verdict").json()
    assert verdict["present"] is True
    assert verdict["kind"] == "credentials_missing"
    assert verdict["colour"] == "red"
    assert verdict["show_retry_button"] is True  # needs_operator semantics
    assert "no Groww credentials in AWS Parameter Store (test)" in verdict["action"]
    # No background job was started — the refusal happens BEFORE any pull.
    snap = client.get("/api/v1/universe/pull-progress").json()
    assert snap["state"] == "idle"


def test_spot_pull_explicit_dry_run_without_creds_is_loudly_synthetic(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """?dry_run=true runs WITHOUT creds and is loudly labeled synthetic.

    The explicit opt-in keeps working (the offline proof-of-spine), but its
    progress title AND its finished verdict must SAY synthetic — never look
    like a real pull (DEC-PULL-LOUD-CREDENTIAL-REFUSAL-001). The fake pull is
    gated on an Event so the RUNNING progress snapshot (whose ``detail``
    carries the label until the job finishes) can be sampled deterministically.
    """
    from brutex.dashboard.pull_controller import BulkSpotReport, SymbolPullResult

    release = threading.Event()

    def _gated_pull_all_spot(instruments: list[Instrument], **kwargs: Any) -> BulkSpotReport:
        assert release.wait(timeout=10.0), "test never released the gated fake pull"
        return BulkSpotReport(
            dry_run=kwargs["dry_run"],
            start_date="2020-01-01",
            end_date="2026-07-01",
            symbols=[SymbolPullResult(symbol="NIFTY", completed=1, failed=0, no_data=0)],
        )

    monkeypatch.setattr(app_module, "pull_all_spot", _gated_pull_all_spot)
    resp = client.post("/api/v1/universe/pull-all", params={"kind": "index", "dry_run": "true"})
    assert resp.status_code == 200
    assert resp.json()["dry_run"] is True
    # While RUNNING the progress title carries the loud synthetic label.
    snap = client.get("/api/v1/universe/pull-progress").json()
    assert snap["state"] == "running"
    assert "SYNTHETIC DRY-RUN — no real data" in snap["detail"]
    release.set()
    _drain_job(client)
    # The finished verdict says synthetic loudly too (mark_synthetic).
    verdict = client.get("/api/v1/universe/pull-verdict").json()
    assert verdict["present"] is True
    assert "SYNTHETIC DRY-RUN — no real data" in verdict["headline"]


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
    default = client.post("/api/v1/universe/pull-all", params={"kind": "index", "dry_run": "true"})
    assert default.status_code == 200
    default_total = default.json()["total"]
    _drain_job(client)
    both = client.post(
        "/api/v1/universe/pull-all",
        params={"kind": "index", "spot_interval": "both", "dry_run": "true"},
    )
    assert both.status_code == 200
    # ``both`` is IGNORED — still 1-minute only → the SAME total (not doubled).
    assert both.json()["total"] == default_total
    _drain_job(client)
    minute = client.post(
        "/api/v1/universe/pull-all",
        params={"kind": "index", "spot_interval": "1minute", "dry_run": "true"},
    )
    assert minute.json()["total"] == default_total
    _drain_job(client)


# ────────────────────────────────────────────────────────────────────────
# Secret-leak guard — no credential value ever reaches a response body
# ────────────────────────────────────────────────────────────────────────


def test_no_secret_leaks_across_the_groww_spot_flow(client: TestClient) -> None:
    """AWS creds present + run a spot pull — no secret leaks into any response body."""
    secrets = (_GROWW_API_KEY, _GROWW_TOTP)
    _save_groww(client)  # AWS reports the creds present (no in-app credential surface exists)
    bodies: list[str] = []

    spot = client.post("/api/v1/universe/pull-all", params={"kind": "index"})
    assert spot.json()["dry_run"] is False
    bodies.append(spot.text)
    _drain_job(client)
    # The pull-progress snapshot must not echo a credential value either.
    bodies.append(client.get("/api/v1/universe/pull-progress").text)

    for body in bodies:
        for secret in secrets:
            assert secret not in body, "a credential value leaked into a response body"


def test_credentials_routes_are_gone(client: TestClient) -> None:
    """The in-app credentials webpage + its routes were removed (DEC-AWS-ONLY-CREDS-001)."""
    assert client.get("/groww-credentials").status_code == 404
    assert client.get("/api/v1/groww/credentials/status").status_code == 404
    assert (
        client.post(
            "/api/v1/groww/credentials",
            json={"api_key": _GROWW_API_KEY, "totp_secret": _GROWW_TOTP},
        ).status_code
        == 404
    )
