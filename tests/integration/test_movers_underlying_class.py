"""Integration tests for the ``/movers`` underlying-class filter (DEC-MOVERS-UNDERLYING-CLASS-001).

Drives the REAL ``/api/v1/movers`` route over a REAL Parquet + DuckDB lake to prove:

1. **Default byte-identity** — a request with NO ``?underlying_class=`` (and an
   explicit ``All``) carries NO ``underlying_class`` / ``excluded_unclassified``
   payload keys and no ``Class:`` note suffix, on all THREE tabs.
2. **Indices vs Stocks over the SAME coordinate return DIFFERENT row sets** — the
   class is a RANK-time filter over the ONE shared cached candidate set (the class
   is NOT in the candidate key), so ``movers_cache`` holds a single entry yet the two
   requests filter it to disjoint rows.
3. **Classification source** — SPOT from the lake segment; FUTURES/OPTIONS from the
   stored-master ``is_index`` kind map. An underlying absent from the master is
   ``unknown`` → included in ``All`` but excluded from both Indices AND Stocks, and
   the exclusion is COUNTED.

Skipped when ``fastapi`` / ``duckdb`` / ``polars`` / ``pyarrow`` are absent.
"""

# pyright: reportPrivateUsage=none

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

from brutex.dashboard.app import DashboardState, build_app
from brutex.dashboard.movers_cache import MoversSnapshotCache
from brutex.data.instrument_store import InstrumentStore
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient

_DAY = date(2024, 6, 13)


def _min_bar(*, close: str, volume: int, oi: int | None = None) -> CandleBar:
    """A flat 09:15 IST (03:45 UTC) 1-minute bar on ``_DAY``."""
    c = Decimal(close)
    return CandleBar(
        timestamp=datetime(_DAY.year, _DAY.month, _DAY.day, 3, 45, tzinfo=UTC),
        open=c,
        high=c,
        low=c,
        close=c,
        volume=volume,
        open_interest=oi,
    )


def _day_bar(*, close: str, volume: int, day: date = _DAY) -> CandleBar:
    """A 1-day OHLCV bar at 09:15 IST (03:45 UTC) on *day*."""
    c = Decimal(close)
    return CandleBar(
        timestamp=datetime(day.year, day.month, day.day, 3, 45, tzinfo=UTC),
        open=c,
        high=c,
        low=c,
        close=c,
        volume=volume,
        open_interest=None,
    )


def _write(
    tmp_path: Path, *, exchange: str, segment: str, symbol: str, interval: str, bars: list[CandleBar]
) -> None:
    ParquetWriter(lake_root=tmp_path).write(
        exchange=exchange, segment=segment, symbol=symbol, interval=interval, bars=bars
    )


def _inst(symbol: str, *, kind: InstrumentType) -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=abs(hash(symbol)) % 100_000,
        symbol_name=symbol,
        display_name=symbol,
        instrument=kind,
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
    """Master with NIFTY (index) + RELIANCE (stock); FINNIFTY deliberately ABSENT."""
    s = InstrumentStore.open(tmp_path / "instruments.duckdb")
    s.upsert_many(
        [
            _inst("NIFTY", kind=InstrumentType.IDX),
            _inst("RELIANCE", kind=InstrumentType.EQ),
        ],
        snapshot_date=date(2024, 1, 2),
        run_id="r1",
        csv_source_sha256="0" * 64,
    )
    yield s
    s.close()


def _client(store: InstrumentStore, cache: MoversSnapshotCache | None = None) -> TestClient:
    return build_app(
        DashboardState(), instrument_store=store, movers_cache=cache or MoversSnapshotCache()
    )  # type: ignore[return-value]


# ── spot ──────────────────────────────────────────────────────────────────


def _seed_spot(tmp_path: Path) -> None:
    _write(
        tmp_path,
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY",
        interval="1day",
        bars=[_day_bar(close="22000", volume=500)],
    )
    _write(
        tmp_path,
        exchange="NSE",
        segment="CASH",
        symbol="RELIANCE",
        interval="1day",
        bars=[_day_bar(close="2900", volume=800)],
    )


class TestSpotUnderlyingClass:
    def test_default_omits_class_keys(self, store: InstrumentStore, tmp_path: Path) -> None:
        _seed_spot(tmp_path)
        client = TestClient(_client(store))
        base = {"asset": "spot", "interval": "1day", "date": "2024-06-13", "top_n": 5}
        default = client.get("/api/v1/movers", params=base).json()
        explicit_all = client.get(
            "/api/v1/movers", params={**base, "underlying_class": "All"}
        ).json()
        for payload in (default, explicit_all):
            assert "underlying_class" not in payload
            assert "excluded_unclassified" not in payload
            assert "Class:" not in payload["note"]
            assert {r["symbol"] for r in payload["volume"]} == {"NIFTY", "RELIANCE"}

    def test_indices_vs_stocks_share_one_cached_set_but_differ(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        _seed_spot(tmp_path)
        cache = MoversSnapshotCache()
        client = TestClient(_client(store, cache))
        base = {"asset": "spot", "interval": "1day", "date": "2024-06-13", "top_n": 5}
        indices = client.get(
            "/api/v1/movers", params={**base, "underlying_class": "Indices"}
        ).json()
        stocks = client.get(
            "/api/v1/movers", params={**base, "underlying_class": "Stocks"}
        ).json()
        # DIFFERENT row sets (the cache-independence guarantee)…
        assert {r["symbol"] for r in indices["volume"]} == {"NIFTY"}
        assert {r["symbol"] for r in stocks["volume"]} == {"RELIANCE"}
        # …served from ONE shared candidate set (class is NOT in candidate_key).
        assert cache.size() == 1
        assert indices["underlying_class"] == "indices"
        assert stocks["underlying_class"] == "stocks"


# ── futures ───────────────────────────────────────────────────────────────


def _seed_futures(tmp_path: Path) -> None:
    for underlying, close, oi in (("NIFTY", "22010", 1000), ("RELIANCE", "2905", 700)):
        _write(
            tmp_path,
            exchange="NSE",
            segment="FNO",
            symbol=f"NSE-{underlying}-27Jun24-FUT",
            interval="1minute",
            bars=[_min_bar(close=close, volume=100, oi=oi)],
        )
    # FINNIFTY future — underlying ABSENT from the master → unknown class.
    _write(
        tmp_path,
        exchange="NSE",
        segment="FNO",
        symbol="NSE-FINNIFTY-27Jun24-FUT",
        interval="1minute",
        bars=[_min_bar(close="23000", volume=60, oi=500)],
    )


class TestFuturesUnderlyingClass:
    def test_default_ranks_all_three(self, store: InstrumentStore, tmp_path: Path) -> None:
        _seed_futures(tmp_path)
        client = TestClient(_client(store))
        base = {"asset": "futures", "interval": "1day", "date": "2024-06-13", "top_n": 10}
        default = client.get("/api/v1/movers", params=base).json()
        assert "underlying_class" not in default
        assert {r["symbol"] for r in default["volume"]} == {
            "NSE-NIFTY-27Jun24-FUT",
            "NSE-RELIANCE-27Jun24-FUT",
            "NSE-FINNIFTY-27Jun24-FUT",
        }

    def test_indices_excludes_unknown_and_counts_it(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        _seed_futures(tmp_path)
        client = TestClient(_client(store))
        base = {"asset": "futures", "interval": "1day", "date": "2024-06-13", "top_n": 10}
        indices = client.get(
            "/api/v1/movers", params={**base, "underlying_class": "Indices"}
        ).json()
        # NIFTY is an index; RELIANCE is a stock; FINNIFTY is unknown → only NIFTY.
        assert {r["symbol"] for r in indices["volume"]} == {"NSE-NIFTY-27Jun24-FUT"}
        # FINNIFTY (unknown) was excluded from Indices AND counted.
        assert indices["excluded_unclassified"] == 1
        assert "unclassified excluded" in indices["note"]

    def test_stocks_keeps_only_stock(self, store: InstrumentStore, tmp_path: Path) -> None:
        _seed_futures(tmp_path)
        client = TestClient(_client(store))
        base = {"asset": "futures", "interval": "1day", "date": "2024-06-13", "top_n": 10}
        stocks = client.get(
            "/api/v1/movers", params={**base, "underlying_class": "Stocks"}
        ).json()
        assert {r["symbol"] for r in stocks["volume"]} == {"NSE-RELIANCE-27Jun24-FUT"}


# ── options ───────────────────────────────────────────────────────────────


def _seed_options(tmp_path: Path) -> None:
    _write(
        tmp_path,
        exchange="NSE",
        segment="FNO",
        symbol="NSE-NIFTY-27Jun24-24000-CE",
        interval="1minute",
        bars=[_min_bar(close="120", volume=100, oi=1000)],
    )
    _write(
        tmp_path,
        exchange="NSE",
        segment="FNO",
        symbol="NSE-RELIANCE-27Jun24-2900-CE",
        interval="1minute",
        bars=[_min_bar(close="45", volume=80, oi=700)],
    )


class TestOptionsUnderlyingClass:
    def test_default_omits_class_keys(self, store: InstrumentStore, tmp_path: Path) -> None:
        _seed_options(tmp_path)
        client = TestClient(_client(store))
        base = {"asset": "options", "interval": "1day", "date": "2024-06-13", "top_n": 10}
        default = client.get("/api/v1/movers", params=base).json()
        assert "underlying_class" not in default
        assert "Class:" not in default["note"]
        assert {r["symbol"] for r in default["volume"]} == {
            "NSE-NIFTY-27Jun24-24000-CE",
            "NSE-RELIANCE-27Jun24-2900-CE",
        }

    def test_stocks_filter(self, store: InstrumentStore, tmp_path: Path) -> None:
        _seed_options(tmp_path)
        client = TestClient(_client(store))
        base = {"asset": "options", "interval": "1day", "date": "2024-06-13", "top_n": 10}
        stocks = client.get(
            "/api/v1/movers", params={**base, "underlying_class": "Stocks"}
        ).json()
        assert {r["symbol"] for r in stocks["volume"]} == {"NSE-RELIANCE-27Jun24-2900-CE"}
        assert stocks["underlying_class"] == "stocks"
