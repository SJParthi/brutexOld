"""Integration tests for the 🎯 Options movers snapshot (DEC-MOVERS-OPTIONS-001).

Two layers, both against a REAL Parquet + DuckDB lake (mirrors
``test_dashboard_movers_futures.py``):

1. The lake-reading core (:func:`~brutex.dashboard.movers_options.run_options_snapshot`
   + ``_session_value_paisa`` + ``_discover_options``, reusing the futures
   ``_read_future_session`` / ``_prev_day_oi`` / ``_prev_day_close`` /
   ``_underlying_spot_ltp``) is exercised directly over a ``tmp_path`` lake
   seeded with :class:`~brutex.data.parquet_writer.ParquetWriter` — the OI / OI%
   / pct / value math, the spot pairing, the per-contract rows (NOT collapsed per
   underlying), the missing-prior-day null branch, the futures-only "no options"
   branch, and the daily-vs-intraday cutoff all reached deterministically.
2. The ``GET /api/v1/movers?asset=options`` route is proven end-to-end through
   FastAPI's :class:`TestClient` (the honest proof the production ``Catalog``
   lake feeds the options reader). Store at ``tmp_path/instruments.duckdb`` ⇒
   lake root ``tmp_path``.

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
from brutex.dashboard.movers_options import (
    _discover_options,
    _session_value_paisa,
    run_options_snapshot,
)
from brutex.data.instrument_store import InstrumentStore
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient

_EXCHANGE = "NSE"
_DAY = date(2024, 6, 13)
_CE = "NSE-NIFTY-27Jun24-24000-CE"
_PE = "NSE-NIFTY-27Jun24-24000-PE"
_FUT = "NSE-NIFTY-27Jun24-FUT"


# ── seeding helpers ───────────────────────────────────────────────────────


def _min_bar(
    *, hour: int, minute: int, close: str, volume: int, oi: int | None, day: date = _DAY
) -> CandleBar:
    """A flat 1-minute bar at ``HH:MM`` **IST** on *day* (IST = UTC + 05:30), with OI."""
    c = Decimal(close)
    total = hour * 60 + minute - (5 * 60 + 30)
    return CandleBar(
        timestamp=datetime(day.year, day.month, day.day, total // 60, total % 60, tzinfo=UTC),
        open=c,
        high=c,
        low=c,
        close=c,
        volume=volume,
        open_interest=oi,
    )


def _day_bar(*, day: date, close: str, oi: int | None) -> CandleBar:
    """A 1-day OHLCV bar labelled at 09:15 IST (03:45 UTC) on *day*, with OI."""
    c = Decimal(close)
    return CandleBar(
        timestamp=datetime(day.year, day.month, day.day, 3, 45, tzinfo=UTC),
        open=c,
        high=c,
        low=c,
        close=c,
        volume=1,
        open_interest=oi,
    )


def _write(
    tmp_path: Path, *, segment: str, symbol: str, interval: str, bars: list[CandleBar]
) -> None:
    ParquetWriter(lake_root=tmp_path).write(
        exchange=_EXCHANGE, segment=segment, symbol=symbol, interval=interval, bars=bars
    )


def _seed_options_lake(tmp_path: Path) -> None:
    """Two NIFTY option contracts (CE + PE) + the NIFTY INDEX spot.

    CE LTP 164 (+OI 1100) vs prior-day close 100 / prior-day OI 1000 → pct +64%,
    oi_chg +100, oi% +10%. PE LTP 80 (+OI 900) vs prior-day close 90 / prior-day
    OI 1200 → pct −11.11%, oi_chg −300, oi% −25%. Spot LTP 23622.90.
    """
    _write(
        tmp_path,
        segment="FNO",
        symbol=_CE,
        interval="1minute",
        bars=[
            _min_bar(hour=9, minute=15, close="150", volume=100, oi=1050),
            _min_bar(hour=9, minute=16, close="164", volume=400, oi=1100),
        ],
    )
    _write(
        tmp_path,
        segment="FNO",
        symbol=_CE,
        interval="1day",
        bars=[_day_bar(day=date(2024, 6, 12), close="100", oi=1000)],
    )
    _write(
        tmp_path,
        segment="FNO",
        symbol=_PE,
        interval="1minute",
        bars=[
            _min_bar(hour=9, minute=15, close="85", volume=50, oi=950),
            _min_bar(hour=9, minute=16, close="80", volume=150, oi=900),
        ],
    )
    _write(
        tmp_path,
        segment="FNO",
        symbol=_PE,
        interval="1day",
        bars=[_day_bar(day=date(2024, 6, 12), close="90", oi=1200)],
    )
    # Underlying NIFTY spot under INDEX (LTP 23622.90 at 09:16).
    _write(
        tmp_path,
        segment="INDEX",
        symbol="NIFTY",
        interval="1minute",
        bars=[
            _min_bar(hour=9, minute=15, close="23600.00", volume=50, oi=None),
            _min_bar(hour=9, minute=16, close="23622.90", volume=50, oi=None),
        ],
    )


# ── _discover_options (FNO 1minute, -CE/-PE only) ─────────────────────────


class TestDiscoverOptions:
    def test_discovers_both_contracts_not_the_future(self, tmp_path: Path) -> None:
        _seed_options_lake(tmp_path)
        # A future shares the FNO segment but ends -FUT → must be excluded.
        _write(
            tmp_path,
            segment="FNO",
            symbol=_FUT,
            interval="1minute",
            bars=[_min_bar(hour=9, minute=15, close="20100", volume=1, oi=5)],
        )
        found = {row[0] for row in _discover_options(tmp_path)}
        assert found == {_CE, _PE}

    def test_skips_unparseable_option_symbol(self, tmp_path: Path) -> None:
        # Ends -CE but the expiry token is malformed → parse None → dropped.
        _write(
            tmp_path,
            segment="FNO",
            symbol="NSE-NIFTY-30Xxx25-24000-CE",
            interval="1minute",
            bars=[_min_bar(hour=9, minute=15, close="10", volume=1, oi=5)],
        )
        assert _discover_options(tmp_path) == []


# ── _session_value_paisa (raw 1m cumulative Σ close×volume) ────────────────


class TestSessionValuePaisa:
    def test_cumulative_value(self, tmp_path: Path) -> None:
        _seed_options_lake(tmp_path)
        # CE: 150*100 + 164*400 = 15000 + 65600 = 80600 rupees → 8_060_000 paisa.
        got = _session_value_paisa(
            tmp_path, exchange=_EXCHANGE, symbol=_CE, day=_DAY, cutoff_min=None
        )
        assert got == 8_060_000

    def test_missing_dir_is_zero(self, tmp_path: Path) -> None:
        assert (
            _session_value_paisa(
                tmp_path,
                exchange=_EXCHANGE,
                symbol="NSE-GHOST-27Jun24-1-CE",
                day=_DAY,
                cutoff_min=None,
            )
            == 0
        )

    def test_cutoff_slices_value(self, tmp_path: Path) -> None:
        _seed_options_lake(tmp_path)
        # cutoff 09:15 (555) keeps only the first CE bar: 150*100 = 15000 → 1_500_000.
        got = _session_value_paisa(
            tmp_path, exchange=_EXCHANGE, symbol=_CE, day=_DAY, cutoff_min=555
        )
        assert got == 1_500_000

    def test_cutoff_before_session_is_zero(self, tmp_path: Path) -> None:
        _seed_options_lake(tmp_path)
        got = _session_value_paisa(tmp_path, exchange=_EXCHANGE, symbol=_CE, day=_DAY, cutoff_min=0)
        assert got == 0

    def test_wrong_day_is_zero(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            segment="FNO",
            symbol=_CE,
            interval="1minute",
            bars=[_min_bar(hour=9, minute=15, close="10", volume=1, oi=5, day=date(2024, 6, 10))],
        )
        assert (
            _session_value_paisa(
                tmp_path, exchange=_EXCHANGE, symbol=_CE, day=_DAY, cutoff_min=None
            )
            == 0
        )


# ── run_options_snapshot (direct, every branch) ───────────────────────────


class TestRunOptionsSnapshot:
    def test_empty_lake_says_pull_options_first(self, tmp_path: Path) -> None:
        payload = run_options_snapshot(
            lake_root=tmp_path, interval="1day", date="2024-06-13", time=None, top_n=10
        )
        assert payload["instrument_count"] == 0
        assert "pull the F&O options first" in payload["note"]

    def test_unknown_interval_degrades_honestly(self, tmp_path: Path) -> None:
        _seed_options_lake(tmp_path)
        payload = run_options_snapshot(
            lake_root=tmp_path, interval="bogus", date="2024-06-13", time=None, top_n=10
        )
        assert payload["instrument_count"] == 0
        assert "pull the F&O options first" in payload["note"]

    def test_futures_only_lake_has_no_options(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            segment="FNO",
            symbol=_FUT,
            interval="1minute",
            bars=[_min_bar(hour=9, minute=15, close="20100", volume=1, oi=5)],
        )
        payload = run_options_snapshot(
            lake_root=tmp_path, interval="1day", date="2024-06-13", time=None, top_n=10
        )
        assert payload["instrument_count"] == 0
        assert "pull the F&O options first" in payload["note"]

    def test_happy_daily_snapshot(self, tmp_path: Path) -> None:
        _seed_options_lake(tmp_path)
        payload = run_options_snapshot(
            lake_root=tmp_path,
            interval="1day",
            date="2024-06-13",
            time=None,
            top_n=10,
            names={"NIFTY": "Nifty 50"},
        )
        assert payload["instrument_count"] == 2  # CE + PE, one row each
        assert payload["skipped_count"] == 0

        # highest_oi: CE (1100) before PE (900).
        highest = payload["highest_oi"]
        assert [r["name"] for r in highest] == [
            "Nifty 50 27Jun24 24000 CE",
            "Nifty 50 27Jun24 24000 PE",
        ]
        ce_row = highest[0]
        assert ce_row["spot"] == "₹23,622.90"
        assert ce_row["ltp"] == "₹164.00"
        assert Decimal(ce_row["pct_change"]) == Decimal("64")  # 164 vs 100
        assert ce_row["oi"] == 1100
        assert ce_row["oi_chg"] == 100  # 1100 - 1000
        assert Decimal(ce_row["oi_chg_pct"]) == Decimal("10")  # 1100 vs 1000
        assert ce_row["volume"] == 500

        # CE oi% +10 → in oi_gainers; PE oi% -25 → in oi_losers.
        assert [r["name"] for r in payload["oi_gainers"]] == ["Nifty 50 27Jun24 24000 CE"]
        assert [r["name"] for r in payload["oi_losers"]] == ["Nifty 50 27Jun24 24000 PE"]

        # CE pct +64 → price_gainers; PE pct -11.11 → price_losers.
        assert [r["name"] for r in payload["price_gainers"]] == ["Nifty 50 27Jun24 24000 CE"]
        assert [r["name"] for r in payload["price_losers"]] == ["Nifty 50 27Jun24 24000 PE"]

        # value: CE 80600 > PE (85*50 + 80*150 = 4250 + 12000 = 16250).
        assert [r["name"] for r in payload["value"]] == [
            "Nifty 50 27Jun24 24000 CE",
            "Nifty 50 27Jun24 24000 PE",
        ]
        pe_signed = next(r for r in payload["oi_losers"])
        assert pe_signed["oi_chg"] == -300  # 900 - 1200

    def test_no_underlying_spot_nulls_spot(self, tmp_path: Path) -> None:
        # CE present but NO underlying spot → spot null, still ranked.
        _write(
            tmp_path,
            segment="FNO",
            symbol=_CE,
            interval="1minute",
            bars=[_min_bar(hour=9, minute=15, close="164", volume=100, oi=1100)],
        )
        _write(
            tmp_path,
            segment="FNO",
            symbol=_CE,
            interval="1day",
            bars=[_day_bar(day=date(2024, 6, 12), close="100", oi=1000)],
        )
        payload = run_options_snapshot(
            lake_root=tmp_path, interval="1day", date="2024-06-13", time=None, top_n=10
        )
        assert payload["instrument_count"] == 1
        assert payload["highest_oi"][0]["spot"] is None

    def test_no_prior_day_nulls_pct_and_oi_pct(self, tmp_path: Path) -> None:
        # CE has a session bar but NO 1day prior partition → null %/OI%.
        _write(
            tmp_path,
            segment="FNO",
            symbol=_CE,
            interval="1minute",
            bars=[_min_bar(hour=9, minute=15, close="164", volume=100, oi=1100)],
        )
        payload = run_options_snapshot(
            lake_root=tmp_path, interval="1day", date="2024-06-13", time=None, top_n=10
        )
        assert payload["instrument_count"] == 1
        row = payload["highest_oi"][0]
        assert row["pct_change"] is None
        assert row["oi_chg"] is None
        assert row["oi_chg_pct"] is None
        assert payload["price_gainers"] == []
        assert payload["oi_gainers"] == []

    def test_contract_with_no_session_bar_is_skipped(self, tmp_path: Path) -> None:
        # The contract exists but has NO bar on the snapshot day → skip.
        _write(
            tmp_path,
            segment="FNO",
            symbol=_CE,
            interval="1minute",
            bars=[_min_bar(hour=9, minute=15, close="10", volume=1, oi=5, day=date(2024, 6, 10))],
        )
        payload = run_options_snapshot(
            lake_root=tmp_path, interval="1day", date="2024-06-13", time=None, top_n=10
        )
        assert payload["instrument_count"] == 1
        assert payload["skipped_count"] == 1
        assert payload["highest_oi"] == []

    def test_intraday_cutoff_path(self, tmp_path: Path) -> None:
        _seed_options_lake(tmp_path)
        # 5-minute interval at 09:15 → only the 09:15 CE/PE bars (CE close 150).
        payload = run_options_snapshot(
            lake_root=tmp_path, interval="5minute", date="2024-06-13", time="09:15", top_n=10
        )
        assert payload["instrument_count"] == 2
        ce_row = payload["highest_oi"][0]
        assert ce_row["ltp"] == "₹150.00"
        assert ce_row["oi"] == 1050


# ── route wiring (FastAPI TestClient) ─────────────────────────────────────


def _idx(symbol: str) -> Instrument:
    """A minimal NSE index instrument (the store only needs a row to exist)."""
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
        [_idx("NIFTY")],
        snapshot_date=date(2024, 1, 2),
        run_id="r1",
        csv_source_sha256="0" * 64,
    )
    yield s
    s.close()


class TestOptionsApi:
    def test_ranks_a_real_options_lake(self, store: InstrumentStore, tmp_path: Path) -> None:
        # store.path is tmp_path/instruments.duckdb → lake root is tmp_path.
        _seed_options_lake(tmp_path)
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        body = client.get(
            "/api/v1/movers",
            params={"asset": "options", "interval": "1day", "date": "2024-06-13", "top_n": 5},
        ).json()
        assert body["instrument_count"] == 2
        assert body["skipped_count"] == 0
        ce_row = body["highest_oi"][0]
        assert ce_row["name"] == "NIFTY 27Jun24 24000 CE"
        assert ce_row["ltp"] == "₹164.00"
        assert ce_row["oi"] == 1100
        assert Decimal(ce_row["oi_chg_pct"]) == Decimal("10")
        assert ce_row["oi_chg"] == 100

    def test_expiry_tier_filters_near_vs_next(self, store: InstrumentStore, tmp_path: Path) -> None:
        # June (near) CE/PE + a July (next) CE for the SAME underlying.
        _seed_options_lake(tmp_path)
        jul_ce = "NSE-NIFTY-25Jul24-24000-CE"
        _write(
            tmp_path,
            segment="FNO",
            symbol=jul_ce,
            interval="1minute",
            bars=[_min_bar(hour=9, minute=16, close="200", volume=300, oi=2000)],
        )
        _write(
            tmp_path,
            segment="FNO",
            symbol=jul_ce,
            interval="1day",
            bars=[_day_bar(day=date(2024, 6, 12), close="180", oi=1800)],
        )
        client = TestClient(build_app(DashboardState(), instrument_store=store))

        # expiry=near (the front expiry as-of the date) → only the June 27 CE/PE.
        near = client.get(
            "/api/v1/movers",
            params={"asset": "options", "interval": "1day", "date": "2024-06-13", "expiry": "near"},
        ).json()
        assert near["instrument_count"] == 2
        assert "Near expiry" in near["note"]
        assert all("25Jul24" not in row["name"] for row in near["highest_oi"])

        # expiry=Next (mixed case → lower-cased) → only the July 25 CE.
        nxt = client.get(
            "/api/v1/movers",
            params={"asset": "options", "interval": "1day", "date": "2024-06-13", "expiry": "Next"},
        ).json()
        assert nxt["instrument_count"] == 1
        assert nxt["highest_oi"][0]["name"] == "NIFTY 25Jul24 24000 CE"
        assert "Next expiry" in nxt["note"]

    def test_empty_options_lake_degrades(self, store: InstrumentStore) -> None:
        # Store wired but no options parquet → honest empty payload (never 500).
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        body = client.get("/api/v1/movers", params={"asset": "options"}).json()
        assert body["instrument_count"] == 0
        assert "pull the F&O options first" in body["note"]

    def test_futures_asset_still_works(self, store: InstrumentStore, tmp_path: Path) -> None:
        # The route change must NOT break the futures dispatch (no contracts → empty).
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        body = client.get("/api/v1/movers", params={"asset": "futures"}).json()
        assert body["instrument_count"] == 0
        assert "pull the F&O futures first" in body["note"]

    def test_malformed_date_is_400(self, store: InstrumentStore) -> None:
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        resp = client.get("/api/v1/movers", params={"asset": "options", "date": "not-a-date"})
        assert resp.status_code == 400
        assert "malformed date" in resp.json()["detail"]

    def test_no_lake_root_options_empty(self) -> None:
        # A store whose `path` is not a filesystem Path → empty options payload.
        from types import SimpleNamespace

        fake = SimpleNamespace(path=None)
        client = TestClient(build_app(DashboardState(), instrument_store=fake))
        body = client.get("/api/v1/movers", params={"asset": "options"}).json()
        assert body["instrument_count"] == 0
        assert "pull the F&O options first" in body["note"]
