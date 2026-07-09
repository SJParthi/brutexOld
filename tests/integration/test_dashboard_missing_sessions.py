"""Integration tests for GET /api/v1/coverage/missing-sessions (DEC-COVERAGE-MISSING-SESSIONS-001).

Drives the REAL route through :class:`TestClient` over a real ``ParquetWriter`` lake, proving
the end-to-end missing-date drill, the never-500 contract on absent/corrupt data, and the
template presence of the drill panel.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("duckdb")

from brutex.dashboard.app import DashboardState, build_app
from brutex.data.instrument_store import InstrumentStore
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient

_IST_OFFSET = timedelta(hours=5, minutes=30)


def _idx(symbol: str, *, exchange: Exchange = Exchange.NSE) -> Instrument:
    return Instrument(
        exch_id=exchange,
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


def _bars(session_date: date, *, n: int = 3) -> list[CandleBar]:
    """*n* 1-minute IST-session bars (09:16 IST start → single UTC month)."""
    open_ist = datetime.combine(session_date, datetime.min.time()).replace(hour=9, minute=16)
    base_utc = (open_ist - _IST_OFFSET).replace(tzinfo=UTC)
    return [
        CandleBar(
            timestamp=base_utc + timedelta(minutes=i),
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100.5"),
            volume=10,
            open_interest=None,
        )
        for i in range(n)
    ]


def _write_nifty(lake_root: Path, dates: list[date]) -> None:
    writer = ParquetWriter(lake_root=lake_root)
    bars: list[CandleBar] = [b for d in dates for b in _bars(d)]
    by_month: dict[tuple[int, int], list[CandleBar]] = {}
    for bar in bars:
        by_month.setdefault((bar.timestamp.year, bar.timestamp.month), []).append(bar)
    for batch in by_month.values():
        writer.write(
            exchange="NSE", segment="INDEX", symbol="NIFTY", interval="1minute", bars=batch
        )


@pytest.fixture
def store(tmp_path: Path):
    s = InstrumentStore.open(tmp_path / "instruments.duckdb")
    s.upsert_many(
        [_idx("NIFTY")], snapshot_date=date(2024, 1, 2), run_id="r1", csv_source_sha256="0" * 64
    )
    yield s
    s.close()


class TestMissingSessionsRoute:
    def test_reports_exact_missing_interior_date(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        # 2024-01-02..05 are all trading days; store 02, 04, 05 → 03-Jan missing.
        _write_nifty(tmp_path, [date(2024, 1, 2), date(2024, 1, 4), date(2024, 1, 5)])
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        resp = client.get("/api/v1/coverage/missing-sessions")
        assert resp.status_code == 200
        body = resp.json()
        assert body["interval"] == "1minute"
        nifty = [r for r in body["rows"] if r["symbol"] == "NIFTY"]
        assert len(nifty) == 1
        row = nifty[0]
        assert row["abstained"] is False
        assert row["missing_dates"] == ["2024-01-03"]
        assert row["missing_count"] == 1
        assert row["present_count"] == 3
        assert body["total_missing"] >= 1

    def test_complete_span_has_no_gaps(self, store: InstrumentStore, tmp_path: Path) -> None:
        _write_nifty(
            tmp_path,
            [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4), date(2024, 1, 5)],
        )
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        row = client.get("/api/v1/coverage/missing-sessions").json()["rows"][0]
        assert row["missing_count"] == 0
        assert row["missing_dates"] == []

    def test_symbol_filter_narrows(self, store: InstrumentStore, tmp_path: Path) -> None:
        _write_nifty(tmp_path, [date(2024, 1, 2), date(2024, 1, 4)])
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        hit = client.get("/api/v1/coverage/missing-sessions?symbol=NIFTY").json()
        assert hit["datasets"] == 1
        miss = client.get("/api/v1/coverage/missing-sessions?symbol=NOPE").json()
        assert miss["datasets"] == 0

    def test_range_override(self, store: InstrumentStore, tmp_path: Path) -> None:
        _write_nifty(tmp_path, [date(2024, 1, 22), date(2024, 1, 23)])
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        body = client.get(
            "/api/v1/coverage/missing-sessions?range_start=2024-01-01&range_end=2024-01-31"
        ).json()
        assert body["range_start"] == "2024-01-01"
        row = body["rows"][0]
        assert row["missing_count"] > 0
        assert "2024-01-22" not in row["missing_dates"]

    def test_empty_lake_never_500(self, store: InstrumentStore) -> None:
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        resp = client.get("/api/v1/coverage/missing-sessions")
        assert resp.status_code == 200
        assert resp.json()["rows"] == []

    def test_no_lake_root_empty_payload(self) -> None:
        class _Fake:
            path = None

            def list_index_equity_instruments(self):  # pragma: no cover - unused by route
                return []

        client = TestClient(build_app(DashboardState(), instrument_store=_Fake()))
        resp = client.get("/api/v1/coverage/missing-sessions")
        assert resp.status_code == 200
        assert resp.json() == {
            "datasets": 0,
            "total_missing": 0,
            "total_missing_before_first_stored": 0,
            "interval": "1minute",
            "range_start": None,
            "range_end": None,
            "rows": [],
        }

    def test_no_store_503(self) -> None:
        client = TestClient(build_app(DashboardState(), instrument_store=None))
        resp = client.get("/api/v1/coverage/missing-sessions")
        assert resp.status_code == 503

    def test_corrupt_parquet_does_not_500(self, store: InstrumentStore, tmp_path: Path) -> None:
        # A single garbage Parquet degrades to its readable subset, never a 500
        # (the resilient aggregate path). Write a good month, then corrupt a file.
        _write_nifty(tmp_path, [date(2024, 1, 2), date(2024, 1, 4)])
        part = tmp_path / "bars" / "NSE" / "INDEX" / "NIFTY" / "1minute"
        for pq in part.rglob("*.parquet"):
            pq.write_bytes(b"not a parquet file")
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        resp = client.get("/api/v1/coverage/missing-sessions")
        assert resp.status_code == 200  # degrades, never 500


class TestMissingSessionsTemplate:
    def test_drill_panel_present(self, store: InstrumentStore) -> None:
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        html = client.get("/coverage/spot").text
        assert 'id="drillbtn"' in html
        assert "Missing-session gap list" in html
        assert "/api/v1/coverage/missing-sessions" in html
        # textContent-only rendering — the drill JS must not use innerHTML.
        drill_js = html.split("Missing-session gap drill")[-1]
        assert "innerHTML" not in drill_js.split("</script>")[0]
