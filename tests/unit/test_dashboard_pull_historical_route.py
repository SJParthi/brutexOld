"""The ``/api/v1/universe/pull-historical`` route (DEC-DERIV-PULL-HISTORICAL-001).

PR-5 of the metadata/historical split (the capstone): the "⤓ Pull historical" button
POSTs THIS route to download OHLC candles for the operator-SELECTED cached registry
contracts (single / multi / chain / underlying / all). This covers the route glue — the
selection grammar (+ its honest-empty 400), the interval / expiry resolution (+ their 400
arms), the ``until_clean`` default, and the single-flight background-job start. The
background ``_work`` body (which calls ``pull_contract_candles`` on the job thread) AND
the selection resolution are exercised through a REAL temp ``RegistryStore`` seeded with
contracts + an OFFLINE fake ``pull_contract_candles`` that asserts the resolved contracts
list is the registry selection (NEVER a re-enumeration / a real network call). Mirrors
``test_dashboard_pull_expiries_contracts_route.py``: a real DuckDB-backed store + a
``TestClient``, creds pinned dry-run. Skipped cleanly when the ``[ui]`` extra (FastAPI) or
duckdb is absent.
"""

from __future__ import annotations

import importlib
import time
from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")
pytest.importorskip("duckdb", reason="the wired-store routes need duckdb")

from brutex.config.credentials import ENV_CREDENTIALS_FILE
from brutex.dashboard.app import DashboardState, build_app
from brutex.dashboard.historical_pull import HistoricalPullReport
from brutex.data.instrument_store import InstrumentStore
from brutex.data.registry_store import ContractRow, RegistryStore
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient

app_module = importlib.import_module("brutex.dashboard.app")

_FETCHED = datetime(2026, 1, 1, tzinfo=UTC)


def _index(symbol: str, security_id: int) -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=security_id,
        symbol_name=symbol,
        display_name=symbol,
        instrument=InstrumentType.IDX,
        segment=Segment.INDEX,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=0,
        tick_size=Decimal("0"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
    )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[InstrumentStore]:
    instrument_store = InstrumentStore.open(tmp_path / "instruments.duckdb")
    instrument_store.upsert_many(
        [_index("NIFTY", 13)],
        snapshot_date=date(2024, 1, 2),
        run_id="r1",
        csv_source_sha256="0" * 64,
    )
    yield instrument_store
    instrument_store.close()


def _seed_registry(lake_root: Path) -> None:
    """Seed the sibling ``registry.duckdb`` with a NIFTY chain (so the route resolves)."""
    with RegistryStore.open(lake_root / "registry.duckdb") as registry:
        expiry = date(2024, 1, 25)
        for symbol, side, strike in (
            ("NSE-NIFTY-25Jan24-21000-CE", "CE", 21000.0),
            ("NSE-NIFTY-25Jan24-21000-PE", "PE", 21000.0),
            ("NSE-NIFTY-25Jan24-FUT", "FUT", None),
        ):
            registry.upsert_contract(
                "NSE",
                "NIFTY",
                expiry,
                symbol,
                side=side,
                strike=strike,
                run_id="r",
                fetched_at=_FETCHED,
            )


@pytest.fixture
def client(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[TestClient]:
    monkeypatch.setenv(ENV_CREDENTIALS_FILE, str(tmp_path / "credentials.json"))
    with TestClient(build_app(DashboardState(), instrument_store=store)) as c:
        yield c


def _force_dry_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_module, "groww_credentials_present", lambda: False)


def _captured() -> dict[str, Any]:
    return {}


def _patch_historical(monkeypatch: pytest.MonkeyPatch, captured: dict[str, Any]) -> None:
    """Wire the offline historical-pull fake that records the resolved contracts list."""

    def _fake_pull_contract_candles(
        contracts: Sequence[ContractRow], **kwargs: Any
    ) -> HistoricalPullReport:
        captured["contracts"] = list(contracts)
        captured["intervals"] = kwargs["intervals"]
        captured["until_clean"] = kwargs["until_clean"]
        captured["dry_run"] = kwargs["dry_run"]
        return HistoricalPullReport(
            dry_run=kwargs["dry_run"],
            intervals=tuple(kwargs["intervals"]),
            contracts_selected=len(contracts),
            contracts_completed=len(contracts),
            bars_appended=0,
            windows=len(contracts),
            failed_pairs=0,
            still_failed_windows=0,
            auth_aborted=False,
            note="offline fake",
        )

    monkeypatch.setattr(app_module, "pull_contract_candles", _fake_pull_contract_candles)


def _wait_for_job(client: TestClient, *, timeout: float = 10.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = client.get("/api/v1/universe/pull-progress").json()
        assert isinstance(snap, dict)
        if snap["state"] in {"done", "error"}:
            return snap
        time.sleep(0.01)
    raise AssertionError("background historical job did not settle within the timeout")


def _lake_root(store: InstrumentStore) -> Path:
    path = store.path
    assert isinstance(path, Path)
    return path.parent


# ── selection forms (happy paths) ─────────────────────────────────────────────


def test_underlying_form_resolves_whole_chain(
    client: TestClient, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force_dry_run(monkeypatch)
    captured = _captured()
    _patch_historical(monkeypatch, captured)
    _seed_registry(_lake_root(store))
    resp = client.post("/api/v1/universe/pull-historical", params={"underlying": "NIFTY"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["started"] is True
    assert body["selected"] == 3
    assert body["until_clean"] is True  # the DEFAULT
    assert _wait_for_job(client)["state"] == "done"
    # The resolved contracts list IS the registry selection (3 NIFTY legs), not a re-enum.
    assert {c.contract_symbol for c in captured["contracts"]} == {
        "NSE-NIFTY-25Jan24-21000-CE",
        "NSE-NIFTY-25Jan24-21000-PE",
        "NSE-NIFTY-25Jan24-FUT",
    }


def test_expiry_narrows_to_chain(
    client: TestClient, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force_dry_run(monkeypatch)
    captured = _captured()
    _patch_historical(monkeypatch, captured)
    _seed_registry(_lake_root(store))
    resp = client.post(
        "/api/v1/universe/pull-historical",
        params={"underlying": "NIFTY", "expiry": "2024-01-25"},
    )
    assert resp.status_code == 200
    assert _wait_for_job(client)["state"] == "done"
    assert len(captured["contracts"]) == 3


def test_single_contract_form(
    client: TestClient, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force_dry_run(monkeypatch)
    captured = _captured()
    _patch_historical(monkeypatch, captured)
    _seed_registry(_lake_root(store))
    resp = client.post(
        "/api/v1/universe/pull-historical",
        params={"contract": "NSE-NIFTY-25Jan24-FUT"},
    )
    assert resp.status_code == 200
    assert _wait_for_job(client)["state"] == "done"
    assert [c.contract_symbol for c in captured["contracts"]] == ["NSE-NIFTY-25Jan24-FUT"]


def test_multi_contracts_form(
    client: TestClient, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force_dry_run(monkeypatch)
    captured = _captured()
    _patch_historical(monkeypatch, captured)
    _seed_registry(_lake_root(store))
    resp = client.post(
        "/api/v1/universe/pull-historical",
        params={"contracts": "NSE-NIFTY-25Jan24-21000-CE,NSE-NIFTY-25Jan24-21000-PE"},
    )
    assert resp.status_code == 200
    assert _wait_for_job(client)["state"] == "done"
    assert {c.contract_symbol for c in captured["contracts"]} == {
        "NSE-NIFTY-25Jan24-21000-CE",
        "NSE-NIFTY-25Jan24-21000-PE",
    }


def test_kind_form_resolves_admitted_underlyings(
    client: TestClient, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force_dry_run(monkeypatch)
    captured = _captured()
    _patch_historical(monkeypatch, captured)
    _seed_registry(_lake_root(store))
    resp = client.post("/api/v1/universe/pull-historical", params={"kind": "index"})
    assert resp.status_code == 200
    assert _wait_for_job(client)["state"] == "done"
    # NIFTY is the only admitted index → its 3 cached legs resolve.
    assert len(captured["contracts"]) == 3


def test_all_form_resolves_every_cached_contract(
    client: TestClient, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force_dry_run(monkeypatch)
    captured = _captured()
    _patch_historical(monkeypatch, captured)
    _seed_registry(_lake_root(store))
    resp = client.post("/api/v1/universe/pull-historical", params={"all": "true"})
    assert resp.status_code == 200
    assert _wait_for_job(client)["state"] == "done"
    assert len(captured["contracts"]) == 3


def test_interval_and_until_clean_off_echoed(
    client: TestClient, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force_dry_run(monkeypatch)
    captured = _captured()
    _patch_historical(monkeypatch, captured)
    _seed_registry(_lake_root(store))
    resp = client.post(
        "/api/v1/universe/pull-historical",
        params={"underlying": "NIFTY", "interval": "both", "until_clean": "false"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["intervals"] == ["1day", "1minute"]
    assert body["until_clean"] is False
    assert _wait_for_job(client)["state"] == "done"
    assert captured["intervals"] == ("1day", "1minute")
    assert captured["until_clean"] is False


# ── honest empty / no-match (NEVER fabricate) ─────────────────────────────────


def test_empty_registry_is_honest_400(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """No cached contracts (registry empty) → an honest 400 pointing at the ⛓ button."""
    _force_dry_run(monkeypatch)
    resp = client.post("/api/v1/universe/pull-historical", params={"underlying": "NIFTY"})
    assert resp.status_code == 400
    assert "pull expiries + contracts first" in resp.json()["detail"]


def test_no_match_selection_is_honest_400(
    client: TestClient, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force_dry_run(monkeypatch)
    _seed_registry(_lake_root(store))
    resp = client.post(
        "/api/v1/universe/pull-historical", params={"contract": "NSE-NOPE-01Jan20-100-CE"}
    )
    assert resp.status_code == 400
    assert "no cached contracts matched" in resp.json()["detail"]


def test_no_selection_is_honest_400(
    client: TestClient, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force_dry_run(monkeypatch)
    _seed_registry(_lake_root(store))
    resp = client.post("/api/v1/universe/pull-historical")
    assert resp.status_code == 400
    assert "no cached contracts matched" in resp.json()["detail"]


# ── 400 arms (bad interval / expiry) ──────────────────────────────────────────


def test_bad_interval_400(
    client: TestClient, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force_dry_run(monkeypatch)
    _seed_registry(_lake_root(store))
    resp = client.post(
        "/api/v1/universe/pull-historical",
        params={"underlying": "NIFTY", "interval": "bogus"},
    )
    assert resp.status_code == 400
    assert "interval must be one of" in resp.json()["detail"]


def test_bad_expiry_400(
    client: TestClient, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force_dry_run(monkeypatch)
    _seed_registry(_lake_root(store))
    resp = client.post(
        "/api/v1/universe/pull-historical",
        params={"underlying": "NIFTY", "expiry": "not-a-date"},
    )
    assert resp.status_code == 400
    assert "expiry must be an ISO date" in resp.json()["detail"]


# ── the button is present + targets the route ─────────────────────────────────


def test_universe_page_has_pull_historical_button(client: TestClient) -> None:
    """The /universe page carries the ⤓ Pull historical button POSTing the route."""
    resp = client.get("/universe")
    assert resp.status_code == 200
    assert "/api/v1/universe/pull-historical" in resp.text
    assert 'id="pullHistBtn"' in resp.text
    # the tooltip wording the operator asked for
    assert "auto-retries until clean" in resp.text


# ── the selection-resolution helper (direct, all grammar arms) ────────────────


def test_resolve_historical_contracts_grammar(store: InstrumentStore) -> None:
    """``_resolve_historical_contracts`` covers every selection arm + the empty default."""
    _seed_registry(_lake_root(store))

    def _select_kind(kind: str) -> list[Instrument]:
        assert kind == "index"
        return [_index("NIFTY", 13)]

    with RegistryStore.open(_lake_root(store) / "registry.duckdb") as registry:
        single = app_module._resolve_historical_contracts(
            registry,
            contract="NSE-NIFTY-25Jan24-FUT",
            contracts=None,
            underlying=None,
            expiry_date=None,
            kind=None,
            all_cached=False,
            select_kind=_select_kind,
        )
        assert [r.contract_symbol for r in single] == ["NSE-NIFTY-25Jan24-FUT"]

        multi = app_module._resolve_historical_contracts(
            registry,
            contract=None,
            contracts="NSE-NIFTY-25Jan24-21000-CE, NSE-NIFTY-25Jan24-21000-PE ,",
            underlying=None,
            expiry_date=None,
            kind=None,
            all_cached=False,
            select_kind=_select_kind,
        )
        assert len(multi) == 2  # the trailing empty token is dropped

        chain = app_module._resolve_historical_contracts(
            registry,
            contract=None,
            contracts=None,
            underlying="NIFTY",
            expiry_date=date(2024, 1, 25),
            kind=None,
            all_cached=False,
            select_kind=_select_kind,
        )
        assert len(chain) == 3

        by_kind = app_module._resolve_historical_contracts(
            registry,
            contract=None,
            contracts=None,
            underlying=None,
            expiry_date=None,
            kind="index",
            all_cached=False,
            select_kind=_select_kind,
        )
        assert len(by_kind) == 3

        every = app_module._resolve_historical_contracts(
            registry,
            contract=None,
            contracts=None,
            underlying=None,
            expiry_date=None,
            kind=None,
            all_cached=True,
            select_kind=_select_kind,
        )
        assert len(every) == 3

        none = app_module._resolve_historical_contracts(
            registry,
            contract=None,
            contracts=None,
            underlying=None,
            expiry_date=None,
            kind=None,
            all_cached=False,
            select_kind=_select_kind,
        )
        assert none == []
