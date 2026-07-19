"""R2/R9/R10 route-level scope batteries (DEC-UNIVERSE-INDEX-ONLY-001, §6 #6/#7).

Plants STOCK partitions / store rows and drives the REAL routes —
``/api/v1/coverage/spot`` (R2, the shared verdict-scan layer),
``/api/v1/completeness/scoreboard`` (R2's second projection),
``/api/v1/coverage/options`` (R9, the post-aggregate row filter) and
``/api/v1/coverage`` (R10, the expected-grid filter) — via ``build_app`` +
``TestClient``: at head every stock row is excluded + counted
(emit-on-nonzero ``scope_excluded`` echo); an index-only lake keeps the
payload byte-identical (no echo key).

**Fails-before law (design §6):** this module imports ONLY merge-base-available
names, so the whole file runs verbatim in a merge-base worktree — where the
``[FB]``-marked tests fail BEHAVIORALLY (the stock rows SERVE and the echo key
is absent) instead of by ImportError.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")
pytest.importorskip("duckdb", reason="the wired-store routes need duckdb")

from brutex.dashboard.app import DashboardState, build_app
from brutex.data.instrument_store import InstrumentStore
from brutex.data.models import CandleBar, OptionBar
from brutex.data.option_parquet_writer import OptionParquetWriter, OptionSeriesCoordinate
from brutex.data.parquet_writer import ParquetWriter
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient

app_module = importlib.import_module("brutex.dashboard.app")

_DEC = "DEC-UNIVERSE-INDEX-ONLY-001"


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


def _bar(ts: datetime) -> CandleBar:
    return CandleBar(
        timestamp=ts,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100.5"),
        volume=10,
        open_interest=None,
    )


def _write_spot(lake: Path, symbol: str, interval: str) -> None:
    ParquetWriter(lake_root=lake).write(
        exchange="NSE",
        segment="CASH",
        symbol=symbol,
        interval=interval,
        bars=[
            _bar(datetime(2024, 3, 1, 9, 15, tzinfo=UTC)),
            _bar(datetime(2024, 3, 2, 9, 15, tzinfo=UTC)),
        ],
    )


def _obar(ts: datetime, side: str, close: str) -> OptionBar:
    return OptionBar(
        timestamp=ts,
        open=Decimal("100.005"),
        high=Decimal("110.10"),
        low=Decimal("90.00"),
        close=Decimal(close),
        volume=250,
        open_interest=1200,
        side=side,
        strike=Decimal("17500.005"),
        spot=Decimal("17480.567"),
        iv=Decimal("0.1523"),
    )


def _write_options(lake: Path, partition_key: str, underlying_symbol: str) -> None:
    """Write a CE+PE option pair via the real expired-options writer."""
    seg, sec, flag, code, offset, _typ = partition_key.split("|")
    OptionParquetWriter(lake_root=lake).write(
        partition_key=partition_key,
        coordinate=OptionSeriesCoordinate(
            exch_id="NSE",
            exchange_segment=seg,
            security_id=int(sec),
            underlying_symbol=underlying_symbol,
            expiry_flag=flag,
            expiry_code=int(code),
            strike_offset=offset,
        ),
        bars=[
            _obar(datetime(2024, 3, 1, 4, 0, tzinfo=UTC), "CE", "105.004"),
            _obar(datetime(2024, 3, 1, 4, 0, tzinfo=UTC), "PE", "95.5"),
        ],
    )


def _make_client(store: InstrumentStore) -> Iterator[TestClient]:
    with TestClient(build_app(DashboardState(), instrument_store=store)) as c:
        yield c


@pytest.fixture
def index_store(tmp_path: Path) -> Iterator[InstrumentStore]:
    """An index-only store (the lake carries the planted stock partitions)."""
    instrument_store = InstrumentStore.open(tmp_path / "instruments.duckdb")
    instrument_store.upsert_many(
        [_index("NIFTY", 13)],
        snapshot_date=date(2024, 1, 2),
        run_id="r1",
        csv_source_sha256="0" * 64,
    )
    yield instrument_store
    instrument_store.close()


@pytest.fixture
def index_client(index_store: InstrumentStore) -> Iterator[TestClient]:
    yield from _make_client(index_store)


@pytest.fixture
def wide_store(tmp_path: Path) -> Iterator[InstrumentStore]:
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
def wide_client(wide_store: InstrumentStore) -> Iterator[TestClient]:
    yield from _make_client(wide_store)


def _row_symbols(payload: dict[str, Any]) -> set[str]:
    return {str(row["symbol"]) for row in payload["rows"]}


# ── R2 — /coverage/spot + /completeness/scoreboard (the shared scan) ─────────


class TestCoverageSpotRouteScope:
    def test_stock_partitions_excluded_and_counted(
        self, index_client: TestClient, tmp_path: Path
    ) -> None:
        """[FB] planted stock spot partitions are excluded + counted (R2)."""
        _write_spot(tmp_path, "NSE-NIFTY", "1minute")
        _write_spot(tmp_path, "NSE-NIFTY", "1day")
        _write_spot(tmp_path, "RELIANCE", "1minute")
        _write_spot(tmp_path, "RELIANCE", "1day")
        resp = index_client.get("/api/v1/coverage/spot")
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        # [FB] merge-base serves the RELIANCE rows and carries NO echo key.
        assert _row_symbols(payload) == {"NSE-NIFTY"}
        assert payload["scope_excluded"] == 1  # DISTINCT symbols, not datasets

    def test_index_only_lake_payload_carries_no_echo(
        self, index_client: TestClient, tmp_path: Path
    ) -> None:
        """Emit-on-nonzero: an index-only lake stays byte-identical (no key)."""
        _write_spot(tmp_path, "NSE-NIFTY", "1minute")
        payload = index_client.get("/api/v1/coverage/spot").json()
        assert _row_symbols(payload) == {"NSE-NIFTY"}
        assert "scope_excluded" not in payload

    def test_completeness_scoreboard_stock_excluded_and_counted(
        self, index_client: TestClient, tmp_path: Path
    ) -> None:
        """[FB] the scoreboard (R2's second projection) excludes + counts."""
        _write_spot(tmp_path, "NSE-NIFTY", "1minute")
        _write_spot(tmp_path, "NSE-NIFTY", "1day")
        _write_spot(tmp_path, "RELIANCE", "1minute")
        _write_spot(tmp_path, "RELIANCE", "1day")
        resp = index_client.get("/api/v1/completeness/scoreboard")
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        # [FB] merge-base scores the RELIANCE rows into the board.
        assert "RELIANCE" not in resp.text
        assert payload["scope_excluded"] == 1


# ── R9 — /coverage/options (the post-aggregate row filter) ───────────────────


class TestCoverageOptionsRouteScope:
    def test_stock_underlying_excluded_and_counted(
        self, index_client: TestClient, tmp_path: Path
    ) -> None:
        """[FB] a stored stock option underlying is excluded + counted (R9)."""
        _write_options(tmp_path, "NSE_FNO|13|WEEK|0|ATM|CALL", "NIFTY")
        _write_options(tmp_path, "NSE_FNO|500325|MONTH|1|ATM|CALL", "RELIANCE")
        resp = index_client.get("/api/v1/coverage/options")
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        # [FB] merge-base serves BOTH underlyings and carries NO echo key.
        assert [row["underlying"] for row in payload["rows"]] == ["NIFTY"]
        assert payload["scope_excluded"] == 1

    def test_index_only_tree_payload_carries_no_echo(
        self, index_client: TestClient, tmp_path: Path
    ) -> None:
        _write_options(tmp_path, "NSE_FNO|13|WEEK|0|ATM|CALL", "NIFTY")
        payload = index_client.get("/api/v1/coverage/options").json()
        assert [row["underlying"] for row in payload["rows"]] == ["NIFTY"]
        assert "scope_excluded" not in payload


# ── R10 — /coverage (the expected universe×years grid) ───────────────────────


class TestCoverageGridRouteScope:
    def test_stock_grid_cells_excluded_and_counted(self, wide_client: TestClient) -> None:
        """[FB] a stale store's stock coverage cells are absent + counted (R10)."""
        resp = wide_client.get("/api/v1/coverage")
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        # [FB] merge-base derives RELIANCE expected cells from the stale store.
        assert "RELIANCE" not in resp.text
        assert payload["scope_excluded"] == 1
        # The admitted index keeps its expected cells.
        assert "NIFTY" in resp.text

    def test_index_only_store_payload_carries_no_echo(self, index_client: TestClient) -> None:
        payload = index_client.get("/api/v1/coverage").json()
        assert "scope_excluded" not in payload
        assert "NIFTY" in str(payload)
