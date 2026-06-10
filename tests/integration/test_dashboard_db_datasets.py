"""Integration tests for ``/api/v1/db/candles/datasets`` option-label decode.

DEC-DB-OPTION-DATASET-DECODE-001 (the #629 tracked follow-up): the /db
expired-options dataset names rendered the raw on-disk vendor segment
(``NSE_FNO__25__MONTH__1__ATM-10__CALL``) instead of the operator's
coordinates. The route now builds the id→symbol map from the wired
:class:`InstrumentStore` on the SAME threadpool worker
(DEC-DASHBOARD-FASTREAD-001) and each option entry carries a display-only
``label`` (``BANKNIFTY · MONTH#1 ATM-10 CALL · 1m``); the ``series`` KEY is
byte-unchanged so ``/api/v1/db/options?series=…`` keeps working — proven
end-to-end here with a real lake written by the production writer.

Skipped when ``fastapi`` / ``duckdb`` are absent — matches the existing
``test_dashboard_coverage.py`` convention.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("duckdb")

from brutex.dashboard.app import DashboardState, build_app
from brutex.data.instrument_store import InstrumentStore
from brutex.data.models import OptionBar
from brutex.data.option_parquet_writer import OptionParquetWriter, OptionSeriesCoordinate
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient


def _idx(symbol: str, security_id: int) -> Instrument:
    """A minimal NSE index row with an EXPLICIT security_id (the map key)."""
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=security_id,
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
    # security_id 25 is the map key the option series below addresses — the
    # live BANKNIFTY id from the operator's #629 screenshot.
    s = InstrumentStore.open(tmp_path / "instruments.duckdb")
    s.upsert_many(
        [_idx("BANKNIFTY", 25)],
        snapshot_date=date(2026, 6, 1),
        run_id="r1",
        csv_source_sha256="0" * 64,
    )
    yield s
    s.close()


def _obar(side: str) -> OptionBar:
    return OptionBar(
        timestamp=datetime(2024, 3, 1, 4, 0, tzinfo=UTC),
        open=Decimal("100.00"),
        high=Decimal("110.10"),
        low=Decimal("90.00"),
        close=Decimal("105.00"),
        volume=250,
        open_interest=1200,
        side=side,
        strike=Decimal("48000.00"),
        spot=Decimal("47980.55"),
        iv=Decimal("0.1523"),
    )


def _write_option_series(lake_root: Path, partition_key: str) -> None:
    """One CE+PE bar pair via the PRODUCTION writer (real on-disk layout)."""
    seg, sec, flag, code, offset, _typ = partition_key.split("|")
    OptionParquetWriter(lake_root=lake_root).write(
        partition_key=partition_key,
        coordinate=OptionSeriesCoordinate(
            exch_id="NSE",
            exchange_segment=seg,
            security_id=int(sec),
            underlying_symbol="BANKNIFTY",
            expiry_flag=flag,
            expiry_code=int(code),
            strike_offset=offset,
        ),
        bars=[_obar("CE"), _obar("PE")],
    )


class TestDbCandleDatasetsApi:
    """The dataset picker decodes option labels; the series KEY keeps querying."""

    def test_option_dataset_label_decoded_and_key_queryable(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        # store.path is tmp_path/instruments.duckdb → lake root is tmp_path.
        _write_option_series(tmp_path, "NSE_FNO|25|MONTH|1|ATM-10|CALL")
        client = TestClient(build_app(DashboardState(), instrument_store=store))

        body = client.get("/api/v1/db/candles/datasets").json()
        assert body["datasets"] == []  # no spot bars written
        (entry,) = body["options"]
        assert entry == {
            "kind": "option",
            "series": "NSE_FNO__25__MONTH__1__ATM-10__CALL",
            "interval": "1minute",
            "label": "BANKNIFTY · MONTH#1 ATM-10 CALL · 1m",
        }
        # The RAW series key still drives the option reader — the decode is
        # display-only, queries are byte-unchanged.
        page = client.get("/api/v1/db/options", params={"series": entry["series"]}).json()
        assert page["total"] == 2  # the CE + PE bars written above

    def test_plus_strike_series_listed_decoded_and_queryable(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        # Regression for the bundled allowlist fix: an ATM+N series (stored
        # VERBATIM with `+`) was skipped by the picker and refused by the
        # reader before DEC-DB-OPTION-DATASET-DECODE-001.
        _write_option_series(tmp_path, "NSE_FNO|25|WEEK|1|ATM+3|CALL")
        client = TestClient(build_app(DashboardState(), instrument_store=store))

        body = client.get("/api/v1/db/candles/datasets").json()
        (entry,) = body["options"]
        assert entry["series"] == "NSE_FNO__25__WEEK__1__ATM+3__CALL"
        assert entry["label"] == "BANKNIFTY · WEEK#1 ATM+3 CALL · 1m"
        page = client.get("/api/v1/db/options", params={"series": entry["series"]}).json()
        assert page["total"] == 2

    def test_no_lake_root_returns_empty_payload(self) -> None:
        from types import SimpleNamespace

        fake = SimpleNamespace(path=None)
        client = TestClient(build_app(DashboardState(), instrument_store=fake))
        assert client.get("/api/v1/db/candles/datasets").json() == {
            "datasets": [],
            "options": [],
        }
