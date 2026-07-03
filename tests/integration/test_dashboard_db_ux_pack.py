"""Integration tests for the ``/db`` F&O cascade "listed N with data M" chip counts.

DEC-DB-UX-PACK-001 Feature 6 (BACKEND). The ``/api/v1/db/fno/expiries`` route pairs
each expiry's vendor-LISTED count (the authoritative ``registry.duckdb`` calendar) with
its WITH-DATA count (contracts that truly have lake candles), so a young live chain is
self-explanatory. Exercised end-to-end against a real lake written by the production
:class:`~brutex.data.parquet_writer.ParquetWriter` + a real sibling
:class:`~brutex.data.registry_store.RegistryStore`, covering every graceful-degrade
path (no registry, registry that does not know the underlying, corrupt registry).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("duckdb")
pytest.importorskip("polars")

from brutex.dashboard.app import DashboardState, build_app
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.registry_store import RegistryStore
from fastapi.testclient import TestClient

_NOW = datetime(2025, 1, 2, 9, 15, tzinfo=UTC)


def _write_fno(lake: Path, symbol: str) -> None:
    """Write one F&O 1-minute bar to ``bars/NSE/FNO/<symbol>/1minute/…``."""
    ParquetWriter(lake_root=lake).write(
        exchange="NSE",
        segment="FNO",
        symbol=symbol,
        interval="1minute",
        bars=[
            CandleBar(
                timestamp=_NOW,
                open=Decimal("100.10"),
                high=Decimal("101.20"),
                low=Decimal("99.50"),
                close=Decimal("100.80"),
                volume=375,
                open_interest=1000,
            )
        ],
    )


def _seed_registry(lake: Path, *, listed: int, underlying: str = "NIFTY") -> None:
    """Seed ``registry.duckdb`` with one 2025-01-02 expiry + *listed* NIFTY legs."""
    reg = RegistryStore.open(lake / "registry.duckdb")
    try:
        exp = date(2025, 1, 2)
        reg.upsert_expiry("NSE", underlying, exp, run_id="r1", fetched_at=_NOW)
        for i in range(listed):
            reg.upsert_contract(
                "NSE",
                underlying,
                exp,
                f"NSE-{underlying}-02Jan25-{24000 + i * 100}-CE",
                side="CE",
                strike=float(24000 + i * 100),
                run_id="r1",
                fetched_at=_NOW,
            )
    finally:
        reg.close()


def _client(lake: Path) -> TestClient:
    """A TestClient whose store path makes ``registry.duckdb`` a sibling of the lake."""
    fake_store = SimpleNamespace(path=lake / "instruments.duckdb")
    return TestClient(build_app(DashboardState(), instrument_store=fake_store))


def test_expiry_counts_registry_present_young_chain(tmp_path: Path) -> None:
    # The vendor listed THREE strikes but only ONE has candle data yet →
    # "listed 3 with data 1" (the self-explanatory young-chain case).
    lake = tmp_path / "lake"
    _write_fno(lake, "NSE-NIFTY-02Jan25-24000-CE")
    _seed_registry(lake, listed=3)
    body = _client(lake).get("/api/v1/db/fno/expiries", params={"underlying": "NIFTY"}).json()
    assert body["expiries"] == ["2025-01-02"]
    assert body["expiry_counts"] == [{"expiry": "2025-01-02", "listed": 3, "with_data": 1}]


def test_expiry_counts_registry_absent_listed_is_null(tmp_path: Path) -> None:
    # No sibling registry.duckdb → an honest "with data M", listed is null.
    lake = tmp_path / "lake"
    _write_fno(lake, "NSE-NIFTY-02Jan25-24000-CE")
    _write_fno(lake, "NSE-NIFTY-02Jan25-24000-PE")
    body = _client(lake).get("/api/v1/db/fno/expiries", params={"underlying": "NIFTY"}).json()
    assert body["expiry_counts"] == [{"expiry": "2025-01-02", "listed": None, "with_data": 2}]


def test_expiry_counts_registry_does_not_know_underlying_listed_is_null(tmp_path: Path) -> None:
    # A registry that knows BANKNIFTY but NOT the queried NIFTY → listed null (never a
    # misleading "listed 0", Golden Rule #1) even though the registry file exists.
    lake = tmp_path / "lake"
    _write_fno(lake, "NSE-NIFTY-02Jan25-24000-CE")
    _seed_registry(lake, listed=2, underlying="BANKNIFTY")
    body = _client(lake).get("/api/v1/db/fno/expiries", params={"underlying": "NIFTY"}).json()
    assert body["expiry_counts"] == [{"expiry": "2025-01-02", "listed": None, "with_data": 1}]


def test_expiry_counts_corrupt_registry_degrades_to_null(tmp_path: Path) -> None:
    # A corrupt registry.duckdb (RegistryError on open) → listed null, never a 500.
    lake = tmp_path / "lake"
    _write_fno(lake, "NSE-NIFTY-02Jan25-24000-CE")
    (lake).mkdir(parents=True, exist_ok=True)
    (lake / "registry.duckdb").write_bytes(b"not-a-duckdb-file \x00\x01 corrupt")
    resp = _client(lake).get("/api/v1/db/fno/expiries", params={"underlying": "NIFTY"})
    assert resp.status_code == 200
    assert resp.json()["expiry_counts"] == [
        {"expiry": "2025-01-02", "listed": None, "with_data": 1}
    ]
