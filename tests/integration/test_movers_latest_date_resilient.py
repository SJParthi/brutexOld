"""Tests for the resilient + memoised default-poll latest-movers-date (GAP #4).

The ``/api/v1/movers`` route with NO explicit date resolves the latest available spot
``1day`` via :func:`brutex.dashboard.movers_warm.latest_movers_date`. Two hardening
changes (corrupt-existing-partition read resilience batch):

* A corrupt / unreadable spot ``1day`` Parquet must NOT 500 the default poll — the read
  degrades to ``None`` (an honest empty/today payload) instead of crashing.
* The O(datasets) date resolution is MOVED off the event loop into the threadpool
  ``_resolve`` closure AND memoised behind the write generation, so a repeat default poll
  is an O(1) handoff and a pull invalidates it.

Skipped when ``fastapi`` / ``duckdb`` / ``polars`` / ``pyarrow`` are absent.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("duckdb")
pytest.importorskip("polars")
pytest.importorskip("pyarrow")

import brutex.dashboard.app as app_module
from brutex.dashboard.movers_cache import MoversSnapshotCache
from brutex.dashboard.summary_cache import SummaryCache
from brutex.data.instrument_store import InstrumentStore
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient

_DAY = date(2024, 6, 13)


def _day_bar(*, close: str) -> CandleBar:
    c = Decimal(close)
    return CandleBar(
        timestamp=datetime(_DAY.year, _DAY.month, _DAY.day, 3, 45, tzinfo=UTC),
        open=c,
        high=c,
        low=c,
        close=c,
        volume=10,
        open_interest=None,
    )


def _write_day(tmp_path: Path, *, symbol: str = "NIFTY") -> None:
    ParquetWriter(lake_root=tmp_path).write(
        exchange="NSE",
        segment="INDEX",
        symbol=symbol,
        interval="1day",
        bars=[_day_bar(close="100")],
    )


def _idx(symbol: str) -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=abs(hash(symbol)) % 100_000,
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
def store(tmp_path: Path) -> Iterator[InstrumentStore]:
    s = InstrumentStore.open(tmp_path / "instruments.duckdb")
    s.upsert_many(
        [_idx("NIFTY")], snapshot_date=date(2024, 1, 2), run_id="r1", csv_source_sha256="0" * 64
    )
    yield s
    s.close()


def _client(store: InstrumentStore, summary_cache: SummaryCache) -> TestClient:
    return TestClient(
        app_module.build_app(
            app_module.DashboardState(),
            instrument_store=store,
            summary_cache=summary_cache,
            movers_cache=MoversSnapshotCache(),
        )
    )


class TestCorruptDefaultPollDegrades:
    def test_corrupt_1day_default_poll_does_not_500(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        """A corrupt spot 1day under a DEFAULT (no-date) poll → 200 degrade, never a 500."""
        # A file at the spot 1day glob path that is GARBAGE bytes → DuckDB read raises.
        corrupt = tmp_path / "bars" / "NSE" / "INDEX" / "NIFTY" / "1day" / "2024" / "06.parquet"
        corrupt.parent.mkdir(parents=True)
        corrupt.write_bytes(b"this is not a parquet file at all")

        client = _client(store, SummaryCache(ttl_floor_s=0.0))
        # NO date param → the default-poll path resolves latest_movers_date over the lake.
        resp = client.get(
            "/api/v1/movers", params={"asset": "spot", "interval": "1day", "top_n": 5}
        )
        # Degrades to an honest empty/today payload instead of 500-ing the page.
        assert resp.status_code == 200


class TestLatestDateMemoised:
    def test_latest_date_resolved_once_per_generation_then_again_after_pull(
        self, store: InstrumentStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The O(datasets) latest-date scan runs ONCE across repeat default polls, again after a pull."""
        _write_day(tmp_path)

        calls = {"n": 0}
        real = app_module.latest_movers_date

        def _counting(lake_root: Path) -> str | None:
            calls["n"] += 1
            return real(lake_root)

        monkeypatch.setattr(app_module, "latest_movers_date", _counting)

        summary_cache = SummaryCache(ttl_floor_s=0.0)
        client = _client(store, summary_cache)
        params = {"asset": "spot", "interval": "1day", "top_n": 5}  # NO date → default poll
        for _ in range(3):
            assert client.get("/api/v1/movers", params=params).status_code == 200
        # The latest-date scan ran exactly ONCE for the three default polls (memoised).
        assert calls["n"] == 1

        # A write-generation bump (a pull) invalidates the memo → exactly one fresh scan.
        summary_cache.mark_dirty()
        assert client.get("/api/v1/movers", params=params).status_code == 200
        assert calls["n"] == 2

    def test_explicit_date_poll_never_resolves_latest_date(
        self, store: InstrumentStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An EXPLICIT date never pays the latest-date scan (only the default poll does)."""
        _write_day(tmp_path)
        calls = {"n": 0}
        real = app_module.latest_movers_date

        def _counting(lake_root: Path) -> str | None:
            calls["n"] += 1
            return real(lake_root)

        monkeypatch.setattr(app_module, "latest_movers_date", _counting)
        client = _client(store, SummaryCache(ttl_floor_s=0.0))
        resp = client.get(
            "/api/v1/movers",
            params={"asset": "spot", "interval": "1day", "date": "2024-06-13", "top_n": 5},
        )
        assert resp.status_code == 200
        assert calls["n"] == 0
