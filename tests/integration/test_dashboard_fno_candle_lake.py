"""Integration: the F&O candle lake is first-class in ``/db`` (DEC-FNO-CANDLE-LAKE-001).

Proven end-to-end against a real lake written by the production
:class:`~brutex.data.sink.ParquetBarSink` + a real
:class:`~brutex.data.instrument_store.InstrumentStore`:

* ``GET /api/v1/db/candles/datasets`` returns the F&O sets under ``fno`` (decoded per
  contract) + the documented ``fno_schema``, and the spot ``datasets`` list EXCLUDES
  the F&O contracts (the honesty fix — an F&O bar is not spot).
* an F&O contract is queryable through the IDENTICAL ``segment="FNO"`` candle path, with
  ``open_interest`` populated (first-class for F&O).
* the F&O candle lake is registered in the master ``schema_meta`` at store-open and is
  browsable via the ``/db`` table reader.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("duckdb")
pytest.importorskip("polars")

from brutex.dashboard.app import DashboardState, build_app
from brutex.data.instrument_store import InstrumentStore
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient

_CE = "NSE-NIFTY-02Jan25-24000-CE"
_FUT = "NSE-NIFTY-30Jan25-FUT"


def _idx(symbol: str) -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=1,
        symbol_name=symbol,
        display_name=symbol,
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


@pytest.fixture
def store(tmp_path: Path):
    s = InstrumentStore.open(tmp_path / "instruments.duckdb")
    s.upsert_many(
        [_idx("NIFTY")], snapshot_date=date(2026, 6, 1), run_id="r1", csv_source_sha256="0" * 64
    )
    yield s
    s.close()


def _write(lake: Path, *, segment: str, symbol: str, oi: int | None) -> None:
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=lake))
    bar = CandleBar(
        timestamp=datetime(2025, 1, 2, 9, 15, tzinfo=UTC),
        open=Decimal("100.10"),
        high=Decimal("101.20"),
        low=Decimal("99.50"),
        close=Decimal("100.80"),
        volume=375,
        open_interest=oi,
    )
    sink.append(exchange="NSE", segment=segment, symbol=symbol, interval="1minute", bars=[bar])


class TestFnoCandleDatasetsEndpoint:
    def test_fno_listed_decoded_and_excluded_from_spot(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        _write(tmp_path, segment="FNO", symbol=_CE, oi=12000)
        _write(tmp_path, segment="FNO", symbol=_FUT, oi=9000)
        _write(tmp_path, segment="CASH", symbol="NSE-NIFTY", oi=None)
        client = TestClient(build_app(DashboardState(), instrument_store=store))

        body = client.get("/api/v1/db/candles/datasets").json()
        # spot list is honestly spot-only — the FNO contracts are NOT here
        spot_symbols = {d["symbol"] for d in body["datasets"]}
        assert spot_symbols == {"NSE-NIFTY"}
        # the F&O lake is its own first-class, decoded surface
        fno = {d["symbol"]: d for d in body["fno"]}
        assert set(fno) == {_CE, _FUT}
        assert fno[_CE]["underlying"] == "NIFTY"
        assert fno[_CE]["strike"] == "24000"
        assert fno[_CE]["option_type"] == "CE"
        assert fno[_FUT]["instrument_type"] == "FUT"
        assert fno[_FUT]["strike"] is None
        assert [c["name"] for c in body["fno_schema"]][:7] == [
            "timestamp",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "open_interest",
        ]

    def test_fno_contract_queryable_with_open_interest(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        _write(tmp_path, segment="FNO", symbol=_CE, oi=12345)
        client = TestClient(build_app(DashboardState(), instrument_store=store))

        page = client.get(
            "/api/v1/db/candles",
            params={"exchange": "NSE", "segment": "FNO", "symbol": _CE, "interval": "1minute"},
        ).json()
        assert page["total"] == 1
        assert "open_interest" in page["columns"]
        oi_idx = page["columns"].index("open_interest")
        assert page["rows"][0][oi_idx] == 12345


class TestSchemaMetaRegistration:
    def test_fno_candle_lake_registered_and_browsable(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        page = client.get("/api/v1/db/table/schema_meta", params={"limit": 500}).json()
        key_idx = page["columns"].index("key")
        val_idx = page["columns"].index("value")
        registered = {r[key_idx]: r[val_idx] for r in page["rows"]}
        assert registered["fno_candle_lake.table"] == "fno_candles"
        assert registered["fno_candle_lake.segment"] == "FNO"
        # v3: the stored nullable Greeks overlay landed (DEC-GREEKS-ENGINE-PR2B-001).
        assert registered["fno_candle_lake.schema_version"] == "3"


class TestFnoCandleRegistryStep:
    """DEC-MONEYNESS-REGISTRY-STEP-001 — the /db candle route threads the registry.

    The route passes ``registry_path_for(store.path)`` into ``query_candles`` so an
    F&O page's ATM±N strike-grid step prefers the AUTHORITATIVE vendor chain, and the
    payload carries the ``strike_step_confidence`` honesty label either way.
    """

    _CE_24100 = "NSE-NIFTY-02Jan25-24100-CE"

    def _seed_sparse(self, tmp_path: Path) -> None:
        # Only alternate strikes pulled (observed gap 100 — 2× the true 50 grid).
        _write(tmp_path, segment="FNO", symbol=_CE, oi=12000)
        _write(tmp_path, segment="FNO", symbol=self._CE_24100, oi=9000)

    def _seed_registry(self, tmp_path: Path) -> None:
        from brutex.data.registry_store import RegistryStore

        now = datetime(2025, 1, 1, tzinfo=UTC)
        with RegistryStore.open(tmp_path / "registry.duckdb") as reg:
            for strike in (24000.0, 24050.0, 24100.0):
                reg.upsert_contract(
                    "NSE",
                    "NIFTY",
                    date(2025, 1, 2),
                    f"NSE-NIFTY-02Jan25-{int(strike)}-CE",
                    side="CE",
                    strike=strike,
                    run_id="r1",
                    fetched_at=now,
                )

    def _page(self, store: InstrumentStore) -> dict[str, object]:
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        return client.get(
            "/api/v1/db/candles",
            params={"exchange": "NSE", "segment": "FNO", "symbol": _CE, "interval": "1minute"},
        ).json()

    def test_without_registry_the_page_is_honestly_low_confidence(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        self._seed_sparse(tmp_path)
        page = self._page(store)
        assert page["strike_step_confidence"] == "observed"

    def test_registry_chain_reaches_the_route(self, store: InstrumentStore, tmp_path: Path) -> None:
        # The registry sits beside the instrument store (registry_path_for), so the
        # route-threaded chain flips the page to high confidence end-to-end.
        self._seed_sparse(tmp_path)
        self._seed_registry(tmp_path)
        page = self._page(store)
        assert page["strike_step_confidence"] == "registry"

    def test_db_template_renders_the_low_confidence_marker(self) -> None:
        # The frontend honesty marker (a small muted note keyed off the payload
        # field) is present in the served template — display-only, string-pinned.
        import brutex.dashboard as dashboard_pkg

        template = (Path(dashboard_pkg.__file__).parent / "templates" / "db.html").read_text(
            encoding="utf-8"
        )
        assert 'strike_step_confidence === "observed"' in template
        assert "stepnote" in template
