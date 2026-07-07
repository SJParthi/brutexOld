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

from brutex.adapters.groww.symbol_form_preference import (
    PersistentSymbolFormPreference,
    form_winner_key,
)
from brutex.config.credentials import ENV_CREDENTIALS_FILE
from brutex.dashboard.app import DashboardState, build_app
from brutex.dashboard.greeks_health import DEFAULT_GREEKS_FAILURE_THRESHOLD
from brutex.dashboard.historical_pull import HistoricalPullReport
from brutex.data.instrument_store import InstrumentStore
from brutex.data.registry_store import ContractRow, RegistryStore
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

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


def _pin_creds_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the credential gate OPEN (DEC-PULL-LOUD-CREDENTIAL-REFUSAL-001).

    These tests exercise the historical route's selection/validation over a
    fully-faked candle pull, not the credential gate (which has its own
    dedicated suite); pinning ``groww_credentials_error`` to ``None`` keeps
    every request deterministic regardless of the ambient environment. The old
    ``_force_dry_run`` (creds-absent → silent synthetic downgrade) is gone:
    a creds-absent real pull now REFUSES with a 503.
    """
    monkeypatch.setattr(app_module, "groww_credentials_error", lambda: None)


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
        # DEC-PULL-SIX-STEP-ORDER-001 (review fix): the direct-selection route now
        # threads the index/stock classification map — captured for assertion.
        captured["underlying_kinds"] = kwargs["underlying_kinds"]
        # DEC-FORM-WINNER-PERSIST-001 (review round-2 fix): the direct-selection
        # route now threads the DURABLE form-winner map too — captured for
        # assertion (fail-loud key access: dropping the wiring fails every test).
        captured["form_preference"] = kwargs["form_preference"]
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
            phase1_clean=True,
            phase1_complete=tuple(c.contract_symbol for c in contracts),
            start_dates={},
            minute_requested="1minute" in kwargs["intervals"],
            minute_skipped=False,
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
    _pin_creds_present(monkeypatch)
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


def test_direct_selection_threads_six_step_underlying_kinds(
    client: TestClient, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The direct-selection grammars run the six-step order (review fix).

    DEC-PULL-SIX-STEP-ORDER-001 round 1 MAJOR: this route's
    ``?contract/?contracts/?underlying/?kind/?all`` paths passed NO
    ``underlying_kinds``, so a real vendor pull over a mixed index+stock
    selection ran the pre-change legacy 2-bucket FUT-then-OPT order —
    reproducing the motivating 2026-07-05 inversion. The route now builds the
    classification map over every admitted underlying (index + stock kinds via
    the SAME resolved-key builder the composite uses) and threads it into
    ``pull_contract_candles``. The seeded store admits ONE index (NIFTY), so
    the map classifies it index-side.
    """
    _pin_creds_present(monkeypatch)
    captured = _captured()
    _patch_historical(monkeypatch, captured)
    _seed_registry(_lake_root(store))
    resp = client.post("/api/v1/universe/pull-historical", params={"underlying": "NIFTY"})
    assert resp.status_code == 200
    assert _wait_for_job(client)["state"] == "done"
    assert captured["underlying_kinds"] == {"NIFTY": True}


def test_direct_selection_threads_durable_form_preference(
    client: TestClient, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The direct-selection grammars thread the DURABLE form-winner map (round-2 fix).

    DEC-FORM-WINNER-PERSIST-001 review round-2 MAJOR: round 1 wired the
    ``?symbols=`` composite branch + the metadata route but MISSED this
    ``?contract/?contracts/?underlying/?kind/?all`` call — the default ``None``
    let the spine build a fresh amnesiac ``SymbolFormPreference`` per pass, so
    on this real production surface a learned ``%26`` winner was never read
    back (an operator's ``?underlying=M&M`` re-paid the full wasted plain-form
    fetch), new wins were never persisted, and the within-one-pull amnesia
    across market buckets / until-clean rounds persisted here. The FUNCTIONAL
    pin: a winner recorded through the ROUTED preference lands in the wired
    ``InstrumentStore``'s ``schema_meta`` (never a fresh store-less map).
    """
    _pin_creds_present(monkeypatch)
    captured = _captured()
    _patch_historical(monkeypatch, captured)
    _seed_registry(_lake_root(store))
    resp = client.post("/api/v1/universe/pull-historical", params={"underlying": "NIFTY"})
    assert resp.status_code == 200
    assert _wait_for_job(client)["state"] == "done"
    pref = captured["form_preference"]
    assert isinstance(pref, PersistentSymbolFormPreference)
    forms = ("M&M", "M%26M")
    assert pref.record_winner("M&M", forms, "M%26M") is True
    assert store.get_meta(form_winner_key("M&M")) == "M%26M"  # the REAL wired store


def test_direct_selection_dry_run_form_preference_is_store_less(
    client: TestClient, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dry-run arm threads ``store=None`` — synthetic runs never persist a row."""
    _pin_creds_present(monkeypatch)
    captured = _captured()
    _patch_historical(monkeypatch, captured)
    _seed_registry(_lake_root(store))
    resp = client.post(
        "/api/v1/universe/pull-historical",
        params={"underlying": "NIFTY", "dry_run": "true"},
    )
    assert resp.status_code == 200
    assert _wait_for_job(client)["state"] == "done"
    pref = captured["form_preference"]
    assert isinstance(pref, PersistentSymbolFormPreference)
    forms = ("M&M", "M%26M")
    assert pref.record_winner("M&M", forms, "M%26M") is True  # in-memory learning only
    assert store.get_meta(form_winner_key("M&M")) is None  # store=None: no durable row


def test_pull_historical_work_feeds_greeks_health_counter(
    client: TestClient, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The /pull-historical ``_work`` closure REALLY feeds ``GreeksEnrichHealth``.

    Hostile-review MAJOR fold (the shipped-DORMANT failure mode of #935/#1014):
    deleting the ``_greeks_health.observe`` call in the /pull-historical
    ``_work`` body left every test green. This drives the REAL route +
    background job ``threshold`` times with a failing Greeks whole-pass
    verdict and asserts exactly ONE ``pull.greeks_enrich_failing`` WARNING
    fires through the production binding (the sibling pipeline-route test
    covers the /pull-pipeline binding + the reset/re-arm arc).
    """
    _pin_creds_present(monkeypatch)
    _seed_registry(_lake_root(store))

    def _failing_pull(contracts: Sequence[ContractRow], **kwargs: Any) -> HistoricalPullReport:
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
            phase1_clean=True,
            phase1_complete=tuple(c.contract_symbol for c in contracts),
            start_dates={},
            minute_requested="1minute" in kwargs["intervals"],
            minute_skipped=False,
            greeks_enrich_ok=False,
            greeks_enrich_error="RuntimeError: broken risk-free source",
        )

    monkeypatch.setattr(app_module, "pull_contract_candles", _failing_pull)
    with capture_logs() as logs:
        for _ in range(DEFAULT_GREEKS_FAILURE_THRESHOLD):
            resp = client.post("/api/v1/universe/pull-historical", params={"underlying": "NIFTY"})
            assert resp.status_code == 200
            assert _wait_for_job(client)["state"] == "done"
    breaches = [r for r in logs if r["event"] == "pull.greeks_enrich_failing"]
    assert len(breaches) == 1  # ONE warning at the OK→BREACH crossing, not per pull
    assert breaches[0]["consecutive"] == DEFAULT_GREEKS_FAILURE_THRESHOLD
    assert breaches[0]["last_error"] == "RuntimeError: broken risk-free source"


def test_expiry_narrows_to_chain(
    client: TestClient, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pin_creds_present(monkeypatch)
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
    _pin_creds_present(monkeypatch)
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
    _pin_creds_present(monkeypatch)
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
    _pin_creds_present(monkeypatch)
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
    _pin_creds_present(monkeypatch)
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
    _pin_creds_present(monkeypatch)
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
    _pin_creds_present(monkeypatch)
    resp = client.post("/api/v1/universe/pull-historical", params={"underlying": "NIFTY"})
    assert resp.status_code == 400
    assert "pull expiries + contracts first" in resp.json()["detail"]


def test_no_match_selection_is_honest_400(
    client: TestClient, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pin_creds_present(monkeypatch)
    _seed_registry(_lake_root(store))
    resp = client.post(
        "/api/v1/universe/pull-historical", params={"contract": "NSE-NOPE-01Jan20-100-CE"}
    )
    assert resp.status_code == 400
    assert "no cached contracts matched" in resp.json()["detail"]


def test_no_selection_is_honest_400(
    client: TestClient, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pin_creds_present(monkeypatch)
    _seed_registry(_lake_root(store))
    resp = client.post("/api/v1/universe/pull-historical")
    assert resp.status_code == 400
    assert "no cached contracts matched" in resp.json()["detail"]


# ── 400 arms (bad interval / expiry) ──────────────────────────────────────────


def test_bad_interval_400(
    client: TestClient, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pin_creds_present(monkeypatch)
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
    _pin_creds_present(monkeypatch)
    _seed_registry(_lake_root(store))
    resp = client.post(
        "/api/v1/universe/pull-historical",
        params={"underlying": "NIFTY", "expiry": "not-a-date"},
    )
    assert resp.status_code == 400
    assert "expiry must be an ISO date" in resp.json()["detail"]


# ── the per-stage button is consolidated into the ONE pipeline button ─────────


def test_universe_page_pull_historical_button_removed(client: TestClient) -> None:
    """The separate ⤓ "Pull historical" button is REMOVED — the historical candle
    fill is the pipeline's gated final fetch stage (DEC-UNIVERSE-PULL-CONSOLIDATE-001).

    The route ``/api/v1/universe/pull-historical`` itself still exists (covered by
    this file's route tests); it is no longer surfaced as its own button. The ONE
    ``pullPipelineBtn`` runs the gated chain that includes the historical pull.
    """
    resp = client.get("/universe")
    assert resp.status_code == 200
    # the redundant per-stage historical button is gone (no null .onclick at load)
    assert 'id="pullHistBtn"' not in resp.text
    assert 'getElementById("pullHistBtn")' not in resp.text
    # the ONE pull button drives the gated pipeline (which includes the historical stage)
    assert 'id="pullPipelineBtn"' in resp.text
    assert "/api/v1/universe/pull-pipeline" in resp.text


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


def test_resolve_historical_contracts_alias_key_symmetry(store: InstrumentStore) -> None:
    """Aliased/hyphenated spot names match the PRIMARY registry key the pull persisted.

    DEC-PULL-FORENSIC-WAVE2-001 regression: the metadata pull persists a chain under
    the PRIMARY F&O underlying form (``NIFTYJR`` → ``NIFTYNXT50``, ``BAJAJ-AUTO`` →
    ``BAJAJAUTO``); the ``underlying=`` and ``kind=`` selection arms previously
    filtered with the RAW spot name → zero rows → an honest-looking 400 despite a
    fully-cached chain. Both arms now resolve every form; a contract persisted under
    TWO key eras (legacy raw + primary) is returned ONCE (contract_symbol de-dup).
    """
    lake = _lake_root(store)
    expiry = date(2024, 1, 25)
    with RegistryStore.open(lake / "registry.duckdb") as registry:
        # The chain as the CURRENT metadata pull persists it — under the PRIMARY form.
        registry.upsert_contract(
            "NSE",
            "NIFTYNXT50",
            expiry,
            "NSE-NIFTYNXT50-25Jan24-60000-CE",
            side="CE",
            strike=60000.0,
            run_id="r",
            fetched_at=_FETCHED,
        )
        # The SAME contract also present under the LEGACY raw-key era → de-duped.
        registry.upsert_contract(
            "NSE",
            "NIFTYJR",
            expiry,
            "NSE-NIFTYNXT50-25Jan24-60000-CE",
            side="CE",
            strike=60000.0,
            run_id="r",
            fetched_at=_FETCHED,
        )
        # A hyphenated stock's chain under its normalized primary form.
        registry.upsert_contract(
            "NSE",
            "BAJAJAUTO",
            expiry,
            "NSE-BAJAJAUTO-25Jan24-FUT",
            side="FUT",
            strike=None,
            run_id="r",
            fetched_at=_FETCHED,
        )

        def _select_kind(kind: str) -> list[Instrument]:
            assert kind == "index"
            return [_index("NIFTYJR", 14)]

        # underlying= with the RAW spot spelling finds the primary-key chain, ONCE.
        by_alias = app_module._resolve_historical_contracts(
            registry,
            contract=None,
            contracts=None,
            underlying="NIFTYJR",
            expiry_date=None,
            kind=None,
            all_cached=False,
            select_kind=_select_kind,
        )
        assert [r.contract_symbol for r in by_alias] == ["NSE-NIFTYNXT50-25Jan24-60000-CE"]

        # underlying= with the HYPHENATED spot spelling finds the normalized key.
        by_hyphen = app_module._resolve_historical_contracts(
            registry,
            contract=None,
            contracts=None,
            underlying="BAJAJ-AUTO",
            expiry_date=None,
            kind=None,
            all_cached=False,
            select_kind=_select_kind,
        )
        assert [r.contract_symbol for r in by_hyphen] == ["NSE-BAJAJAUTO-25Jan24-FUT"]

        # kind= over an aliased spot instrument matches the primary-key rows (and the
        # legacy-key duplicate row — the registry holds two rows for two key eras).
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
        assert {r.contract_symbol for r in by_kind} == {"NSE-NIFTYNXT50-25Jan24-60000-CE"}


# ── one-click multi-select COMPOSITE (DEC-FNO-ONECLICK-HISTORICAL-001) ─────────


from brutex.dashboard.composite_pull import CompositePullReport  # noqa: E402
from brutex.dashboard.historical_pull import HistoricalPullReport as _HistReport  # noqa: E402
from brutex.dashboard.metadata_pull import MetadataPullReport as _MetaReport  # noqa: E402


@pytest.fixture
def ref_store(tmp_path: Path) -> Iterator[InstrumentStore]:
    """Store seeding a TRADABLE NIFTY + a REFERENCE_ONLY INDIAVIX (security_id 26000)."""
    instrument_store = InstrumentStore.open(tmp_path / "instruments.duckdb")
    instrument_store.upsert_many(
        [_index("NIFTY", 13), _index("INDIAVIX", 26000)],
        snapshot_date=date(2024, 1, 2),
        run_id="r1",
        csv_source_sha256="0" * 64,
        reference_only_of=lambda inst: inst.security_id == 26000,
    )
    yield instrument_store
    instrument_store.close()


@pytest.fixture
def ref_client(
    ref_store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[TestClient]:
    monkeypatch.setenv(ENV_CREDENTIALS_FILE, str(tmp_path / "credentials.json"))
    with TestClient(build_app(DashboardState(), instrument_store=ref_store)) as c:
        yield c


def _composite_report(
    *, names: list[str], skipped: list[str], dry_run: bool
) -> CompositePullReport:
    """An honest-shaped offline composite report echoing the selection (NO network)."""
    return CompositePullReport(
        dry_run=dry_run,
        requested=tuple(names + skipped),
        tradable=tuple(names),
        skipped_reference_only=tuple(skipped),
        metadata=_MetaReport(
            dry_run=dry_run,
            years=(2024,),
            underlyings=tuple(names),
            expiry_cells=0,
            cells_skipped=0,
            expiry_failures=0,
            expiries_persisted=0,
            contract_jobs=0,
            contract_failures=0,
            contracts_persisted=0,
            note="offline fake metadata",
        ),
        historical=_HistReport(
            dry_run=dry_run,
            intervals=("1day",),
            contracts_selected=len(names),
            contracts_completed=len(names),
            bars_appended=0,
            windows=0,
            failed_pairs=0,
            still_failed_windows=0,
            auth_aborted=False,
            note="offline fake candles",
            phase1_clean=True,
            phase1_complete=tuple(names),
            start_dates={},
            minute_requested=False,
            minute_skipped=False,
        ),
        note="offline fake composite",
    )


def _patch_composite(monkeypatch: pytest.MonkeyPatch, captured: dict[str, Any]) -> None:
    """Wire the offline composite fake that records its tradable + reference-only split."""

    def _fake(instruments: Sequence[Any], **kwargs: Any) -> CompositePullReport:
        ref_ids = kwargs["reference_only_ids"]
        names = [i.symbol_name for i in instruments if i.security_id not in ref_ids]
        skipped = [i.symbol_name for i in instruments if i.security_id in ref_ids]
        captured["names"] = names
        captured["skipped"] = skipped
        captured["intervals"] = kwargs["intervals"]
        captured["until_clean"] = kwargs["until_clean"]
        # DEC-FORM-WINNER-PERSIST-001 (round-5 pin): the composite route threads
        # the DURABLE form-winner map — captured for assertion (fail-loud key
        # access: dropping the wiring fails every composite-route test).
        captured["form_preference"] = kwargs["form_preference"]
        return _composite_report(names=names, skipped=skipped, dry_run=kwargs["dry_run"])

    monkeypatch.setattr(app_module, "pull_historical_for_underlyings", _fake)


def test_composite_symbols_starts_job(
    ref_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``?symbols=`` routes to the composite: tradable pulled, REF skipped + reported."""
    _pin_creds_present(monkeypatch)
    captured: dict[str, Any] = {}
    _patch_composite(monkeypatch, captured)
    resp = ref_client.post("/api/v1/universe/pull-historical", params={"symbols": "NIFTY,INDIAVIX"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["started"] is True
    assert body["enumerate"] is True
    assert body["symbols"] == "NIFTY,INDIAVIX"
    assert body["skipped_reference_only"] == ["INDIAVIX"]
    assert body["skipped_count"] == 1
    assert body["tradable"] == 1
    assert _wait_for_job(ref_client)["state"] == "done"
    # only NIFTY reached the composite; INDIAVIX was partitioned out.
    assert captured["names"] == ["NIFTY"]
    assert captured["skipped"] == ["INDIAVIX"]


def test_composite_threads_durable_form_preference(
    ref_client: TestClient, ref_store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``?symbols=`` composite branch wires the DURABLE form-winner map.

    The round-5 regression pin (the direct-selection route's round-2
    discipline, mirrored): this route's ``form_preference=...`` kwarg was
    asserted by ZERO tests, so deleting the wiring — silently degrading the
    composite back to the fresh-per-pass amnesiac default — passed every
    suite. The FUNCTIONAL pin: a winner recorded through the ROUTED
    preference lands in the wired ``InstrumentStore``'s ``schema_meta``
    (never a fresh store-less map) on a non-dry-run.
    """
    _pin_creds_present(monkeypatch)
    captured: dict[str, Any] = {}
    _patch_composite(monkeypatch, captured)
    resp = ref_client.post("/api/v1/universe/pull-historical", params={"symbols": "NIFTY"})
    assert resp.status_code == 200
    assert _wait_for_job(ref_client)["state"] == "done"
    pref = captured["form_preference"]
    assert isinstance(pref, PersistentSymbolFormPreference)
    forms = ("M&M", "M%26M")
    assert pref.record_winner("M&M", forms, "M%26M") is True
    assert ref_store.get_meta(form_winner_key("M&M")) == "M%26M"  # the REAL wired store


def test_composite_enumerate_flag_without_symbols_400(
    ref_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``enumerate=true`` with no ``?symbols=`` is an honest 400."""
    _pin_creds_present(monkeypatch)
    resp = ref_client.post("/api/v1/universe/pull-historical", params={"enumerate": "true"})
    assert resp.status_code == 400
    assert "enumerate=true requires ?symbols" in resp.json()["detail"]


def test_composite_unknown_symbol_400(
    ref_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unknown symbol in ``?symbols=`` is a 400 (never a fabricated underlying)."""
    _pin_creds_present(monkeypatch)
    resp = ref_client.post("/api/v1/universe/pull-historical", params={"symbols": "NIFTY,NOPE"})
    assert resp.status_code == 400
    assert "NOPE" in resp.json()["detail"]


def test_composite_all_reference_only_is_honest_no_op(
    ref_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ALL-REF ``?symbols=`` selection is an honest no-op (started: False, not a 400)."""
    _pin_creds_present(monkeypatch)
    captured: dict[str, Any] = {}
    _patch_composite(monkeypatch, captured)
    resp = ref_client.post("/api/v1/universe/pull-historical", params={"symbols": "INDIAVIX"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["started"] is False
    assert body["skipped_reference_only"] == ["INDIAVIX"]
    assert body["skipped_count"] == 1
    assert body["note"] == ("all 1 selected are reference-only (no F&O chain) — nothing to pull")
    # the composite fake was NEVER invoked (zero-call no-op before the job starts)
    assert captured == {}


def test_composite_bad_from_date_400(
    ref_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-ISO ``from_date`` with ``?symbols=`` is a 400 (the metadata-year parse)."""
    _pin_creds_present(monkeypatch)
    resp = ref_client.post(
        "/api/v1/universe/pull-historical",
        params={"symbols": "NIFTY", "from_date": "not-a-date"},
    )
    assert resp.status_code == 400
    assert "from_date must be an ISO date" in resp.json()["detail"]


def test_single_underlying_back_compat_preserved(
    client: TestClient, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ``?symbols=`` / ``enumerate`` → the ORIGINAL single-underlying path is unchanged."""
    _pin_creds_present(monkeypatch)
    captured = _captured()
    _patch_historical(monkeypatch, captured)
    _seed_registry(_lake_root(store))
    resp = client.post("/api/v1/universe/pull-historical", params={"underlying": "NIFTY"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["started"] is True
    assert "enumerate" not in body  # the single-underlying payload (not the composite shape)
    assert body["selected"] == 3
    assert _wait_for_job(client)["state"] == "done"


def test_universe_page_no_longer_sends_standalone_historical_query(client: TestClient) -> None:
    """The standalone ⤓ Pull historical multi-select query is GONE — the historical
    pull runs inside the ONE gated pipeline (DEC-UNIVERSE-PULL-CONSOLIDATE-001)."""
    resp = client.get("/universe")
    assert resp.status_code == 200
    # the removed button's one-click composite query is no longer emitted by the page
    assert "pull-historical?enumerate=true&symbols=" not in resp.text
    # the single pipeline button posts the consolidated route instead
    assert "/api/v1/universe/pull-pipeline" in resp.text
