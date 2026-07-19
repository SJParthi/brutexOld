"""P7/P8 route-level scope batteries (DEC-UNIVERSE-INDEX-ONLY-001, design §6 #3/#4).

Drives the REAL routes via ``build_app`` + ``TestClient`` over a STALE
stock-bearing store/registry — the exact window the P7/P8 screens exist for
(the boot auto-heal is best-effort, so a pre-cut ~750-row store can keep
serving): every single-symbol / selection arm that feeds a vendor pull must
refuse a stock selection with a 400 naming the DEC, the ``/pull-historical``
bulk arms must FILTER + count, and the explicit arms must refuse loudly.

**Fails-before law (design §6):** this module imports ONLY merge-base-available
names, so the whole file runs verbatim in a merge-base worktree — where the
``[FB]``-marked tests fail BEHAVIORALLY (the stock selection resolves and the
faked pull records it / the route answers 200) instead of by ImportError. The
``BRUTEX_FEED_SCOPE`` env name is therefore used as a LITERAL here (never the
``feed_scope`` import the house convention prefers — that module does not
exist at merge-base). Every pull spine is patched with an offline recording
fake, so a merge-base run issues ZERO network calls.
"""

from __future__ import annotations

import importlib
import time
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")
pytest.importorskip("duckdb", reason="the wired-store routes need duckdb")

from brutex.config.credentials import ENV_CREDENTIALS_FILE
from brutex.dashboard.app import DashboardState, build_app
from brutex.dashboard.composite_pull import CompositePullReport
from brutex.dashboard.historical_pull import HistoricalPullReport
from brutex.dashboard.metadata_pull import MetadataPullReport
from brutex.dashboard.pull_controller import (
    BulkSpotReport,
    PullCandlesReport,
    SymbolPullResult,
)
from brutex.data.instrument_store import InstrumentStore
from brutex.data.registry_store import ContractRow, RegistryStore
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

app_module = importlib.import_module("brutex.dashboard.app")

_DEC = "DEC-UNIVERSE-INDEX-ONLY-001"
#: The literal env name (NOT the feed_scope import — see the module docstring).
_SCOPE_ENV = "BRUTEX_FEED_SCOPE"
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


def _equity(symbol: str, security_id: int) -> Instrument:
    """A pre-cut F&O-eligible NSE cash equity (the stale-store stock class)."""
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=security_id,
        symbol_name=symbol,
        display_name=symbol,
        instrument=InstrumentType.EQ,
        segment=Segment.CASH,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=505,  # EQ requires a positive lot size
        tick_size=Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
    )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[InstrumentStore]:
    """A STALE wide store: the admitted NIFTY beside a pre-cut RELIANCE row."""
    instrument_store = InstrumentStore.open(tmp_path / "instruments.duckdb")
    instrument_store.upsert_many(
        [_index("NIFTY", 13), _equity("RELIANCE", 2885)],
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
    monkeypatch.setenv(ENV_CREDENTIALS_FILE, str(tmp_path / "credentials.json"))
    with TestClient(build_app(DashboardState(), instrument_store=store)) as c:
        yield c


def _pin_creds_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the credential gate OPEN (the sibling route suites' idiom)."""
    monkeypatch.setattr(app_module, "groww_credentials_error", lambda: None)


def _fake_metadata_report(**kwargs: Any) -> MetadataPullReport:
    return MetadataPullReport(
        dry_run=kwargs["dry_run"],
        years=tuple(kwargs["years"]),
        underlyings=tuple(inst.symbol_name for inst in kwargs["instruments"]),
        expiry_cells=0,
        cells_skipped=0,
        expiry_failures=0,
        expiries_persisted=0,
        contract_jobs=0,
        contract_failures=0,
        contracts_persisted=0,
        note="offline fake",
    )


def _wire_offline_pulls(monkeypatch: pytest.MonkeyPatch, pulled: list[str]) -> None:
    """Patch EVERY pull spine a started job could touch with a recording fake.

    *pulled* collects the symbol names each spine was actually asked to pull —
    the head batteries assert it stays EMPTY behind a refusal (zero vendor
    work), and the merge-base fails-before run proves the stock selection DID
    reach a spine.
    """

    def _fake_pull_candles(instrument: Instrument, **kwargs: Any) -> PullCandlesReport:
        pulled.append(instrument.symbol_name)
        return PullCandlesReport(
            symbol=instrument.symbol_name,
            year=0,
            dry_run=kwargs["dry_run"],
            start_date="2020-01-01",
            end_date="2026-07-01",
        )

    def _fake_pull_all_spot(instruments: list[Instrument], **kwargs: Any) -> BulkSpotReport:
        pulled.extend(inst.symbol_name for inst in instruments)
        return BulkSpotReport(
            dry_run=kwargs["dry_run"],
            start_date="2020-01-01",
            end_date="2026-07-01",
            symbols=[
                SymbolPullResult(symbol=inst.symbol_name, completed=1, failed=0, no_data=0)
                for inst in instruments
            ],
        )

    def _fake_metadata(instruments: list[Instrument], **kwargs: Any) -> MetadataPullReport:
        pulled.extend(inst.symbol_name for inst in instruments)
        return _fake_metadata_report(instruments=instruments, **kwargs)

    def _fake_composite(underlyings: list[str], **kwargs: Any) -> CompositePullReport:
        pulled.extend(underlyings)
        instruments: list[Instrument] = []
        inner = HistoricalPullReport(
            dry_run=kwargs["dry_run"],
            intervals=("1minute",),
            contracts_selected=0,
            contracts_completed=0,
            bars_appended=0,
            windows=0,
            failed_pairs=0,
            still_failed_windows=0,
            auth_aborted=False,
            note="offline fake",
            phase1_clean=True,
            phase1_complete=(),
            start_dates={},
            minute_requested=True,
            minute_skipped=False,
        )
        return CompositePullReport(
            dry_run=kwargs["dry_run"],
            requested=tuple(underlyings),
            tradable=tuple(underlyings),
            skipped_reference_only=(),
            metadata=_fake_metadata_report(
                instruments=instruments, dry_run=kwargs["dry_run"], years=kwargs.get("years", ())
            ),
            historical=inner,
            note="offline fake",
        )

    def _fake_historical_contracts(
        contracts: list[ContractRow], **kwargs: Any
    ) -> HistoricalPullReport:
        pulled.extend(row.contract_symbol for row in contracts)
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
            phase1_complete=tuple(row.contract_symbol for row in contracts),
            start_dates={},
            minute_requested="1minute" in kwargs["intervals"],
            minute_skipped=False,
        )

    monkeypatch.setattr(app_module, "pull_candles", _fake_pull_candles)
    monkeypatch.setattr(app_module, "pull_all_spot", _fake_pull_all_spot)
    monkeypatch.setattr(app_module, "pull_expiries_contracts", _fake_metadata)
    monkeypatch.setattr(app_module, "pull_historical_for_underlyings", _fake_composite)
    monkeypatch.setattr(app_module, "pull_contract_candles", _fake_historical_contracts)


def _wait_for_job(client: TestClient, *, timeout: float = 10.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = client.get("/api/v1/universe/pull-progress").json()
        assert isinstance(snap, dict)
        if snap["state"] in {"done", "error"}:
            return snap
        time.sleep(0.01)
    raise AssertionError("background job did not settle within the timeout")


def _drain_if_started(client: TestClient) -> None:
    """Settle any background job a merge-base (fails-before) run started.

    At head the refusal fires BEFORE any job start, so this is an instant
    idle read; at merge-base it keeps the faked job from outliving the test.
    """
    snap = client.get("/api/v1/universe/pull-progress").json()
    if snap.get("state") not in {"idle", "done", "error"}:
        _wait_for_job(client)


# ── P7 — the five single-symbol / selection route arms (design §6#3) ─────────


class TestP7SelectionRouteArms:
    """Each raw store-resolution arm feeding a vendor pull refuses a stock."""

    def _assert_refused(self, client: TestClient, resp: Any, pulled: list[str]) -> None:
        assert resp.status_code == 400, resp.text  # [FB] merge-base answers 200
        assert _DEC in resp.json()["detail"]
        assert "RELIANCE" in resp.json()["detail"]
        _drain_if_started(client)
        assert pulled == []  # zero vendor work behind the refusal

    def test_universe_pull_stock_symbol_refused_400(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """[FB] arm (iv) — POST /universe/pull?symbol=<stock> refuses."""
        _pin_creds_present(monkeypatch)
        pulled: list[str] = []
        _wire_offline_pulls(monkeypatch, pulled)
        resp = client.post(
            "/api/v1/universe/pull", params={"symbol": "RELIANCE", "dry_run": "true"}
        )
        self._assert_refused(client, resp, pulled)

    def test_pull_all_stock_symbol_refused_400(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """[FB] arm (v) — POST /pull-all?symbol=<stock> refuses."""
        _pin_creds_present(monkeypatch)
        pulled: list[str] = []
        _wire_offline_pulls(monkeypatch, pulled)
        resp = client.post(
            "/api/v1/universe/pull-all", params={"symbol": "RELIANCE", "dry_run": "true"}
        )
        self._assert_refused(client, resp, pulled)

    def test_pull_expiries_contracts_stock_symbols_refused_400(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """[FB] arm (i)/(iii) — the metadata route's ?symbols= selection refuses."""
        _pin_creds_present(monkeypatch)
        pulled: list[str] = []
        _wire_offline_pulls(monkeypatch, pulled)
        resp = client.post(
            "/api/v1/universe/pull-expiries-contracts",
            params={"symbols": "RELIANCE", "dry_run": "true"},
        )
        self._assert_refused(client, resp, pulled)

    def test_pull_pipeline_stock_symbols_refused_400(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """[FB] arm (ii) — the one-click pipeline's ?symbols= selection refuses."""
        _pin_creds_present(monkeypatch)
        pulled: list[str] = []
        _wire_offline_pulls(monkeypatch, pulled)
        resp = client.post(
            "/api/v1/universe/pull-pipeline",
            params={"symbols": "RELIANCE", "dry_run": "true"},
        )
        self._assert_refused(client, resp, pulled)

    def test_retry_failed_stock_symbols_refused_400(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """[FB] arm (ii) — /retry-failed's ?symbols= selection refuses."""
        _pin_creds_present(monkeypatch)
        pulled: list[str] = []
        _wire_offline_pulls(monkeypatch, pulled)
        resp = client.post(
            "/api/v1/universe/retry-failed",
            params={"symbols": "RELIANCE", "dry_run": "true"},
        )
        self._assert_refused(client, resp, pulled)

    def test_index_selection_still_pulls_on_the_inline_arms(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Steady-state parity: the admitted NIFTY still pulls on arms (iv)+(v)."""
        _pin_creds_present(monkeypatch)
        pulled: list[str] = []
        _wire_offline_pulls(monkeypatch, pulled)
        resp = client.post("/api/v1/universe/pull", params={"symbol": "NIFTY", "dry_run": "true"})
        assert resp.status_code == 200, resp.text
        resp = client.post(
            "/api/v1/universe/pull-all", params={"symbol": "NIFTY", "dry_run": "true"}
        )
        assert resp.status_code == 200, resp.text
        assert _wait_for_job(client)["state"] == "done"
        assert "NIFTY" in pulled

    def test_full_scope_widen_stock_selection_still_pulls(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The §6#3 widen pin: under ``full`` a stock selection still PULLS.

        Merge-base parity — kills the empty-sets legacy-EQ screen variant
        that would 400 every legitimate ``?symbols=<stock>`` under a wide
        scope (the bounded()-gate law).
        """
        _pin_creds_present(monkeypatch)
        monkeypatch.setenv(_SCOPE_ENV, "full")
        pulled: list[str] = []
        _wire_offline_pulls(monkeypatch, pulled)
        resp = client.post(
            "/api/v1/universe/pull-expiries-contracts",
            params={"symbols": "RELIANCE", "dry_run": "true"},
        )
        assert resp.status_code == 200, resp.text
        assert _wait_for_job(client)["state"] == "done"
        assert "RELIANCE" in pulled


# ── P8 — the /pull-historical direct registry grammar (design §6#4) ──────────


def _seed_registry(lake_root: Path, *, with_stock: bool) -> None:
    """Seed registry.duckdb with a NIFTY chain (+ a pre-cut RELIANCE chain)."""
    with RegistryStore.open(lake_root / "registry.duckdb") as registry:
        expiry = date(2024, 1, 25)
        legs: list[tuple[str, str, str, float | None]] = [
            ("NIFTY", "NSE-NIFTY-25Jan24-21000-CE", "CE", 21000.0),
            ("NIFTY", "NSE-NIFTY-25Jan24-21000-PE", "PE", 21000.0),
            ("NIFTY", "NSE-NIFTY-25Jan24-FUT", "FUT", None),
        ]
        if with_stock:
            legs += [
                ("RELIANCE", "NSE-RELIANCE-25Jan24-2500-CE", "CE", 2500.0),
                ("RELIANCE", "NSE-RELIANCE-25Jan24-2500-PE", "PE", 2500.0),
                ("RELIANCE", "NSE-RELIANCE-25Jan24-FUT", "FUT", None),
            ]
        for underlying, symbol, side, strike in legs:
            registry.upsert_contract(
                "NSE",
                underlying,
                expiry,
                symbol,
                side=side,
                strike=strike,
                run_id="r",
                fetched_at=_FETCHED,
            )


def _lake_root(store: InstrumentStore) -> Path:
    path = store.path
    assert isinstance(path, Path)
    return path.parent


class TestP8HistoricalRouteArms:
    """The direct registry grammar over a stale stock-bearing registry."""

    def test_all_true_filters_stock_contracts_and_counts(
        self, client: TestClient, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """[FB] ?all=true plans ZERO stock contracts + counts the exclusion."""
        _pin_creds_present(monkeypatch)
        pulled: list[str] = []
        _wire_offline_pulls(monkeypatch, pulled)
        _seed_registry(_lake_root(store), with_stock=True)
        with capture_logs() as logs:
            resp = client.post(
                "/api/v1/universe/pull-historical",
                params={"all": "true", "dry_run": "true"},
            )
            assert resp.status_code == 200, resp.text
            # [FB] merge-base selects all 6 cached contracts (3 of them stock).
            assert resp.json()["selected"] == 3
            assert _wait_for_job(client)["state"] == "done"
        # Only the NIFTY legs were planned/pulled — never a stock contract.
        assert pulled and all("NIFTY" in symbol for symbol in pulled)
        events = [e for e in logs if e["event"] == "universe.pull_historical.scope_excluded"]
        assert len(events) == 1
        assert events[0]["excluded"] == 3
        assert events[0]["dec"] == _DEC

    def test_underlying_stock_refused_400(
        self, client: TestClient, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """[FB] the explicit ?underlying=RELIANCE ask fails loud, never empty."""
        _pin_creds_present(monkeypatch)
        pulled: list[str] = []
        _wire_offline_pulls(monkeypatch, pulled)
        _seed_registry(_lake_root(store), with_stock=True)
        resp = client.post(
            "/api/v1/universe/pull-historical",
            params={"underlying": "RELIANCE", "dry_run": "true"},
        )
        assert resp.status_code == 400, resp.text
        assert _DEC in resp.json()["detail"]
        _drain_if_started(client)
        assert pulled == []

    def test_contract_stock_refused_400(
        self, client: TestClient, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """[FB] the explicit ?contract=<stock contract> ask fails loud."""
        _pin_creds_present(monkeypatch)
        pulled: list[str] = []
        _wire_offline_pulls(monkeypatch, pulled)
        _seed_registry(_lake_root(store), with_stock=True)
        resp = client.post(
            "/api/v1/universe/pull-historical",
            params={"contract": "NSE-RELIANCE-25Jan24-2500-CE", "dry_run": "true"},
        )
        assert resp.status_code == 400, resp.text
        assert _DEC in resp.json()["detail"]
        _drain_if_started(client)
        assert pulled == []

    def test_index_underlying_unchanged(
        self, client: TestClient, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The admitted NIFTY chain pulls unchanged beside the stock rows."""
        _pin_creds_present(monkeypatch)
        pulled: list[str] = []
        _wire_offline_pulls(monkeypatch, pulled)
        _seed_registry(_lake_root(store), with_stock=True)
        resp = client.post(
            "/api/v1/universe/pull-historical",
            params={"underlying": "NIFTY", "dry_run": "true"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["selected"] == 3
        assert _wait_for_job(client)["state"] == "done"
        assert sorted(pulled) == [
            "NSE-NIFTY-25Jan24-21000-CE",
            "NSE-NIFTY-25Jan24-21000-PE",
            "NSE-NIFTY-25Jan24-FUT",
        ]


# ── the greeks-backfill eager scope-env 400 (design §2.3) ────────────────────


class TestGreeksBackfillScopeEnv:
    def test_junk_scope_env_refused_400_at_post_time(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """[FB] junk BRUTEX_FEED_SCOPE refuses at POST time, before any scan."""
        monkeypatch.setenv(_SCOPE_ENV, "junk-token")
        resp = client.post("/api/v1/universe/greeks-backfill")
        assert resp.status_code == 400, resp.text  # [FB] merge-base starts the scan job
        assert _SCOPE_ENV in resp.json()["detail"]
        # Clear the junk BEFORE the drain probe — at head the progress route's
        # own scope-bound reads would otherwise trip on the poisoned env (the
        # drain only matters for the merge-base run, where the scan started).
        monkeypatch.delenv(_SCOPE_ENV)
        _drain_if_started(client)
