"""Integration tests for the dashboard instruments browser endpoints.

Uses FastAPI's :class:`TestClient` to hit every ``/api/v1/instruments*``,
``/api/v1/expiries``, ``/api/v1/contracts`` endpoint exactly as a
browser would. Verifies both the *enabled* path (a real
:class:`~brutex.data.instrument_store.InstrumentStore` wired in) and
the *disabled* path (returns 503 with a helpful error and the HTML
hides the Instruments Browser panel).

Skipped when ``fastapi`` / ``duckdb`` are absent \u2014 matches the
existing ``test_dashboard_e2e.py`` convention.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("duckdb")

from brutex.dashboard.app import DashboardState, build_app
from brutex.data.instrument_store import (
    InstrumentRun,
    InstrumentStore,
)
from brutex.data.models import OptionBar
from brutex.data.option_parquet_writer import OptionParquetWriter, OptionSeriesCoordinate
from brutex.models import (
    Exchange,
    Instrument,
    InstrumentType,
    Segment,
)
from fastapi.testclient import TestClient

# ────────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────────


def _make_instrument(
    symbol: str = "NSE-NIFTY",
    *,
    instrument_type: InstrumentType = InstrumentType.IDX,
    segment: Segment = Segment.CASH,
    underlying: str | None = "NIFTY",
    expiry: date | None = None,
    strike: Decimal | None = None,
) -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        # Derive a per-symbol id so the IDX / FUT / CE rows are distinct under
        # the (security_id, exch_id, segment, snapshot_date) PK
        # (DEC-INSTRUMENT-PK-SEGMENT-001 / ADR-0003) — a shared fixed id
        # collided the two FNO legs.
        security_id=26000 + (abs(hash(symbol)) % 100_000),
        symbol_name=symbol,
        display_name="Nifty 50",
        instrument=instrument_type,
        segment=segment,
        series=None,
        isin=None,
        underlying_symbol=underlying,
        underlying_security_id=None,
        lot_size=25,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=1800,
        sm_expiry_date=expiry,
        strike_price=strike,
        asm_gsm_flag=False,
    )


@pytest.fixture
def populated_store(tmp_path: Path):
    """Return an :class:`InstrumentStore` with one of each row type."""
    store = InstrumentStore.open(tmp_path / "instruments.duckdb")
    snap_date = date(2024, 1, 2)

    # 3 instruments: 1 index, 1 future, 1 option
    instruments = [
        _make_instrument("NSE-NIFTY"),
        _make_instrument(
            "NSE-NIFTY25JANFUT",
            instrument_type=InstrumentType.FUT,
            segment=Segment.FNO,
            expiry=date(2025, 1, 30),
        ),
        _make_instrument(
            "NSE-NIFTY25JAN24000CE",
            instrument_type=InstrumentType.CE,
            segment=Segment.FNO,
            expiry=date(2025, 1, 30),
            strike=Decimal("24000"),
        ),
    ]
    store.upsert_many(
        instruments,
        snapshot_date=snap_date,
        run_id="test-run",
        csv_source_sha256="a" * 64,
    )

    # 1 expired-option series (CE) in the lake — the dropped expiries/contracts
    # DuckDB tables were replaced by the expired-options Parquet lake
    # (DEC-EXPIRED-OPTIONS-TABLE-001). Lake root = the store's parent dir.
    OptionParquetWriter(lake_root=store.path.parent).write(
        partition_key="NSE_FNO|13|WEEK|0|ATM|CALL",
        coordinate=OptionSeriesCoordinate(
            exch_id="NSE",
            exchange_segment="NSE_FNO",
            security_id=13,
            underlying_symbol="NIFTY",
            expiry_flag="WEEK",
            expiry_code=0,
            strike_offset="ATM",
        ),
        bars=[
            OptionBar(
                timestamp=datetime(2024, 3, 1, 4, 0, tzinfo=UTC),
                open=Decimal("100"),
                high=Decimal("110"),
                low=Decimal("90"),
                close=Decimal("105"),
                volume=250,
                open_interest=1200,
                side="CE",
                strike=Decimal("24000"),
                spot=Decimal("17480.5"),
                iv=Decimal("0.15"),
            )
        ],
    )

    # 1 instrument_runs row
    store.record_run(
        InstrumentRun(
            run_id="test-run",
            snapshot_date=snap_date,
            fetched_at=datetime(2024, 1, 2, 12, tzinfo=UTC),
            csv_source_sha256="a" * 64,
            total_raw_rows=3,
            integrity_failures=0,
            in_scope_count=3,
            out_of_scope_count=0,
            index_count=1,
            futures_count=1,
            options_count=1,
            bse_count=0,
            nse_count=3,
            code_commit_sha="deadbeef",
            schema_version=1,
        )
    )

    yield store
    store.close()


@pytest.fixture
def client_with_store(populated_store: InstrumentStore) -> TestClient:
    """Return a ``TestClient`` bound to an app with the store wired."""
    state = DashboardState()
    app = build_app(state, instrument_store=populated_store)
    return TestClient(app)


@pytest.fixture
def client_without_store() -> TestClient:
    """Return a ``TestClient`` bound to an app with NO store."""
    state = DashboardState()
    app = build_app(state)
    return TestClient(app)


# ────────────────────────────────────────────────────────────────────────
# Enabled path — store wired
# ────────────────────────────────────────────────────────────────────────


class TestInstrumentsEnabled:
    def test_index_decouples_instruments_to_rest(self, client_with_store: TestClient) -> None:
        # Per locked design DEC-DASHBOARD-LIVE-FINALE (MIGRATION.md #234) the
        # Instruments Browser is no longer injected into the index page — it is
        # a separate concern served by the /api/v1/instruments REST endpoint.
        # The index therefore never embeds the card, even with a store wired.
        r = client_with_store.get("/")
        assert r.status_code == 200
        assert "instruments-card" not in r.text
        rest = client_with_store.get("/api/v1/instruments")
        assert rest.status_code == 200

    def test_stats_endpoint_returns_totals(self, client_with_store: TestClient) -> None:
        r = client_with_store.get("/api/v1/instruments/stats")
        assert r.status_code == 200
        body = r.json()
        assert body["totals"]["instruments"] == 3
        assert body["totals"]["expiries"] == 1
        assert body["totals"]["contracts"] == 1
        assert body["totals"]["runs"] == 1

    def test_stats_breaks_down_by_underlying(self, client_with_store: TestClient) -> None:
        r = client_with_store.get("/api/v1/instruments/stats")
        body = r.json()
        nifty_rows = [row for row in body["by_underlying"] if row["underlying"] == "NIFTY"]
        assert len(nifty_rows) == 1
        assert nifty_rows[0]["count"] == 3

    def test_instruments_list_returns_all_rows(self, client_with_store: TestClient) -> None:
        r = client_with_store.get("/api/v1/instruments")
        assert r.status_code == 200
        body = r.json()
        assert len(body["instruments"]) == 3

    def test_instruments_list_filter_by_type(self, client_with_store: TestClient) -> None:
        r = client_with_store.get("/api/v1/instruments?instrument_type=CE")
        assert r.status_code == 200
        body = r.json()
        assert len(body["instruments"]) == 1
        assert body["instruments"][0]["instrument"].endswith("CE")

    def test_instruments_list_filter_by_segment(self, client_with_store: TestClient) -> None:
        r = client_with_store.get("/api/v1/instruments?segment=FNO")
        body = r.json()
        assert len(body["instruments"]) == 2  # FUT + CE
        for row in body["instruments"]:
            assert row["segment"].endswith("FNO")

    def test_instruments_list_filter_by_underlying(self, client_with_store: TestClient) -> None:
        r = client_with_store.get("/api/v1/instruments?underlying=NIFTY")
        body = r.json()
        # Matches both underlying_symbol=NIFTY and trading_symbol=NIFTY
        # (the index row has trading_symbol=NIFTY but underlying=NIFTY too)
        assert len(body["instruments"]) == 3

    def test_instruments_list_pagination(self, client_with_store: TestClient) -> None:
        r = client_with_store.get("/api/v1/instruments?limit=1&offset=0")
        body = r.json()
        assert len(body["instruments"]) == 1
        assert body["limit"] == 1
        assert body["offset"] == 0

    def test_expiries_endpoint(self, client_with_store: TestClient) -> None:
        # Repointed to the expired-options lake: an "expiry" is now a distinct
        # (flag, code) series (DEC-EXPIRED-OPTIONS-TABLE-001).
        r = client_with_store.get("/api/v1/expiries")
        assert r.status_code == 200
        body = r.json()
        assert len(body["expiries"]) == 1
        assert body["expiries"][0]["underlying_symbol"] == "NIFTY"
        assert body["expiries"][0]["expiry_flag"] == "WEEK"
        assert body["expiries"][0]["expiry_code"] == 0

    def test_expiries_filter_by_underlying(self, client_with_store: TestClient) -> None:
        r = client_with_store.get("/api/v1/expiries?underlying=BANKNIFTY")
        body = r.json()
        assert len(body["expiries"]) == 0

    def test_contracts_endpoint(self, client_with_store: TestClient) -> None:
        r = client_with_store.get("/api/v1/contracts?underlying=NIFTY")
        assert r.status_code == 200
        body = r.json()
        assert len(body["contracts"]) == 1
        c = body["contracts"][0]
        assert c["underlying_symbol"] == "NIFTY"
        assert c["side"] == "CE"
        assert c["strike_offset"] == "ATM"
        assert Decimal(str(c["strike"])) == Decimal("24000")

    def test_contracts_filter_by_expiry_code(self, client_with_store: TestClient) -> None:
        r = client_with_store.get("/api/v1/contracts?underlying=NIFTY&expiry_code=0")
        body = r.json()
        assert len(body["contracts"]) == 1

    def test_contracts_filter_by_other_expiry_code_empty(
        self, client_with_store: TestClient
    ) -> None:
        r = client_with_store.get("/api/v1/contracts?underlying=NIFTY&expiry_code=2")
        body = r.json()
        assert len(body["contracts"]) == 0


# ────────────────────────────────────────────────────────────────────────
# Disabled path — no store wired
# ────────────────────────────────────────────────────────────────────────


class TestInstrumentsDisabled:
    def test_index_html_hides_instruments_card(self, client_without_store: TestClient) -> None:
        r = client_without_store.get("/")
        assert r.status_code == 200
        assert "instruments-card" not in r.text

    def test_stats_returns_503(self, client_without_store: TestClient) -> None:
        r = client_without_store.get("/api/v1/instruments/stats")
        assert r.status_code == 503
        assert "InstrumentStore not wired" in r.json()["detail"]

    def test_instruments_list_returns_503(self, client_without_store: TestClient) -> None:
        r = client_without_store.get("/api/v1/instruments")
        assert r.status_code == 503

    def test_expiries_returns_503(self, client_without_store: TestClient) -> None:
        r = client_without_store.get("/api/v1/expiries")
        assert r.status_code == 503

    def test_contracts_returns_503(self, client_without_store: TestClient) -> None:
        r = client_without_store.get("/api/v1/contracts")
        assert r.status_code == 503

    def test_state_endpoint_still_works(self, client_without_store: TestClient) -> None:
        """Core dashboard features must stay available even without store."""
        r = client_without_store.get("/api/v1/state")
        assert r.status_code == 200
        assert "phases" in r.json()
