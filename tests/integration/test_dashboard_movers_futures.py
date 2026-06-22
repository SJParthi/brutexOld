"""Integration tests for the 🔮 Futures movers snapshot (DEC-MOVERS-FUTURES-001).

Two layers, both against a REAL Parquet + DuckDB lake (mirrors
``test_dashboard_movers.py``):

1. The lake-reading core (:func:`~brutex.dashboard.movers_futures.run_futures_snapshot`
   + ``_read_future_session`` + ``_prev_day_oi`` + the underlying-spot resolver)
   is exercised directly over a ``tmp_path`` lake seeded with
   :class:`~brutex.data.parquet_writer.ParquetWriter` — the premium / pct /
   oi-% math, the near-month pick, the raw-1-minute OI read, the missing-dir /
   empty-day skip, the no-underlying-spot premium-null branch, the all-options
   "no futures" branch, and the daily-vs-intraday cutoff all reached
   deterministically.
2. The ``GET /api/v1/movers?asset=futures`` route is proven end-to-end through
   FastAPI's :class:`TestClient` (the honest proof the production ``Catalog``
   lake feeds the futures reader). Store at ``tmp_path/instruments.duckdb`` ⇒
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
from brutex.dashboard.movers_futures import (
    _prev_day_oi,
    _read_future_session,
    _underlying_spot_ltp,
    run_futures_snapshot,
)
from brutex.data.instrument_store import InstrumentStore
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient

_EXCHANGE = "NSE"
_DAY = date(2024, 6, 13)
#: A near-month future expiry strictly AFTER the snapshot day (front month).
_NEAR = "27Jun24"
_NEAR_FUT = f"NSE-NIFTY-{_NEAR}-FUT"
#: A later (next-month) future expiry — should NOT be picked over the near one.
_FAR_FUT = "NSE-NIFTY-25Jul24-FUT"


# ── seeding helpers ───────────────────────────────────────────────────────


def _min_bar(
    *,
    hour: int,
    minute: int,
    close: str,
    volume: int,
    oi: int | None,
    day: date = _DAY,
    high: str | None = None,
    low: str | None = None,
) -> CandleBar:
    """A 1-minute bar at ``HH:MM`` **IST** on *day* (IST = UTC + 05:30), with OI.

    *high*/*low* default to *close* (a flat zero-range bar) so existing callers are
    unchanged; pass them to exercise the HLC3 typical-price turnover.
    """
    c = Decimal(close)
    total = hour * 60 + minute - (5 * 60 + 30)
    return CandleBar(
        timestamp=datetime(day.year, day.month, day.day, total // 60, total % 60, tzinfo=UTC),
        open=c,
        high=Decimal(high) if high is not None else c,
        low=Decimal(low) if low is not None else c,
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


def _seed_futures_lake(tmp_path: Path) -> None:
    """Near-month NIFTY future (+ a later far month) + the NIFTY INDEX spot.

    Future LTP 20100 (+OI 1100) vs prior-day close 20000 / prior-day OI 1000;
    spot LTP 20040 → premium +60, pct +0.5%, oi% +10%.
    """
    _write(
        tmp_path,
        segment="FNO",
        symbol=_NEAR_FUT,
        interval="1minute",
        bars=[
            _min_bar(hour=9, minute=15, close="20050", volume=100, oi=1050),
            _min_bar(hour=9, minute=16, close="20100", volume=400, oi=1100),
        ],
    )
    _write(
        tmp_path,
        segment="FNO",
        symbol=_NEAR_FUT,
        interval="1day",
        bars=[_day_bar(day=date(2024, 6, 12), close="20000", oi=1000)],
    )
    # A later-expiry future exists too — the reader must pick the NEAR one.
    _write(
        tmp_path,
        segment="FNO",
        symbol=_FAR_FUT,
        interval="1minute",
        bars=[_min_bar(hour=9, minute=15, close="20500", volume=10, oi=200)],
    )
    # Underlying NIFTY spot under INDEX (LTP 20040 at 09:16).
    _write(
        tmp_path,
        segment="INDEX",
        symbol="NIFTY",
        interval="1minute",
        bars=[
            _min_bar(hour=9, minute=15, close="20020", volume=50, oi=None),
            _min_bar(hour=9, minute=16, close="20040", volume=50, oi=None),
        ],
    )


# ── _read_future_session (raw 1m, OI preserved; direct) ───────────────────


class TestReadFutureSession:
    def test_missing_dir_is_none(self, tmp_path: Path) -> None:
        assert (
            _read_future_session(
                tmp_path,
                exchange=_EXCHANGE,
                segment="FNO",
                symbol="GHOST",
                day=_DAY,
                cutoff_min=None,
            )
            is None
        )

    def test_reads_ltp_volume_and_oi(self, tmp_path: Path) -> None:
        _seed_futures_lake(tmp_path)
        read = _read_future_session(
            tmp_path, exchange=_EXCHANGE, segment="FNO", symbol=_NEAR_FUT, day=_DAY, cutoff_min=None
        )
        assert read is not None
        ltp, volume, value_paisa, oi = read
        assert ltp == Decimal("20100.00")
        assert volume == 500
        # Zero-range seed bars → HLC3 == close: 20050×100 + 20100×400 = ₹1,00,45,000
        # → 1_004_500_000 paisa (DEC-MOVERS-VWAP-VALUE-001).
        assert value_paisa == 1_004_500_000
        assert oi == 1100  # last bar's OI, preserved (make_lake_read_1m would drop it)

    def test_intraday_cutoff_slices(self, tmp_path: Path) -> None:
        _seed_futures_lake(tmp_path)
        # cutoff 09:15 (555) keeps only the first bar (close 20050, vol 100, oi 1050).
        read = _read_future_session(
            tmp_path, exchange=_EXCHANGE, segment="FNO", symbol=_NEAR_FUT, day=_DAY, cutoff_min=555
        )
        assert read is not None
        ltp, volume, value_paisa, oi = read
        assert ltp == Decimal("20050.00")
        assert volume == 100
        assert value_paisa == 200_500_000  # only bar1: 20050×100 = ₹20,05,000 → 200_500_000 paisa
        assert oi == 1050

    def test_cutoff_before_session_is_none(self, tmp_path: Path) -> None:
        _seed_futures_lake(tmp_path)
        assert (
            _read_future_session(
                tmp_path,
                exchange=_EXCHANGE,
                segment="FNO",
                symbol=_NEAR_FUT,
                day=_DAY,
                cutoff_min=0,
            )
            is None
        )

    def test_wrong_day_is_none(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            segment="FNO",
            symbol=_NEAR_FUT,
            interval="1minute",
            bars=[_min_bar(hour=9, minute=15, close="100", volume=1, oi=5, day=date(2024, 6, 10))],
        )
        assert (
            _read_future_session(
                tmp_path,
                exchange=_EXCHANGE,
                segment="FNO",
                symbol=_NEAR_FUT,
                day=_DAY,
                cutoff_min=None,
            )
            is None
        )

    def test_null_oi_column_yields_none_oi(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            segment="INDEX",
            symbol="NIFTY",
            interval="1minute",
            bars=[_min_bar(hour=9, minute=15, close="20020", volume=50, oi=None)],
        )
        read = _read_future_session(
            tmp_path, exchange=_EXCHANGE, segment="INDEX", symbol="NIFTY", day=_DAY, cutoff_min=None
        )
        assert read is not None
        assert read[3] is None  # oi is now the 4th element (ltp, volume, value, oi)

    def test_value_uses_hlc3_typical_price(self, tmp_path: Path) -> None:
        # DEC-MOVERS-VWAP-VALUE-001: a REAL intrabar range proves the turnover uses the
        # HLC3 typical price, NOT close × volume (which a zero-range seed can't distinguish).
        _write(
            tmp_path,
            segment="FNO",
            symbol=_NEAR_FUT,
            interval="1minute",
            bars=[_min_bar(hour=9, minute=15, high="112", low="106", close="110", volume=20, oi=5)],
        )
        read = _read_future_session(
            tmp_path, exchange=_EXCHANGE, segment="FNO", symbol=_NEAR_FUT, day=_DAY, cutoff_min=None
        )
        assert read is not None
        # typical=(112+106+110)/3=109.3333…→round_money 109.33 → 109.33×20=₹2186.60 → 218660 paisa
        # (the OLD close×volume would give 110×20=2200 → 220000 — genuinely distinct).
        assert read[2] == 218660


# ── _underlying_spot_ltp (INDEX then CASH) ────────────────────────────────


class TestUnderlyingSpotLtp:
    def test_index_segment_hit(self, tmp_path: Path) -> None:
        _seed_futures_lake(tmp_path)
        ltp = _underlying_spot_ltp(
            tmp_path, exchange=_EXCHANGE, underlying="NIFTY", day=_DAY, cutoff_min=None
        )
        assert ltp == Decimal("20040.00")

    def test_cash_segment_fallback(self, tmp_path: Path) -> None:
        # No INDEX series — a stock future's underlying lives under CASH.
        _write(
            tmp_path,
            segment="CASH",
            symbol="RELIANCE",
            interval="1minute",
            bars=[_min_bar(hour=9, minute=15, close="1293", volume=5, oi=None)],
        )
        ltp = _underlying_spot_ltp(
            tmp_path, exchange=_EXCHANGE, underlying="RELIANCE", day=_DAY, cutoff_min=None
        )
        assert ltp == Decimal("1293.00")

    def test_absent_underlying_is_none(self, tmp_path: Path) -> None:
        assert (
            _underlying_spot_ltp(
                tmp_path, exchange=_EXCHANGE, underlying="NIFTY", day=_DAY, cutoff_min=None
            )
            is None
        )


# ── run_futures_snapshot (direct, every branch) ───────────────────────────


class TestRunFuturesSnapshot:
    def test_empty_lake_says_pull_futures_first(self, tmp_path: Path) -> None:
        payload = run_futures_snapshot(
            lake_root=tmp_path, interval="1day", date="2024-06-13", time=None, top_n=10
        )
        assert payload["instrument_count"] == 0
        assert "pull the F&O futures first" in payload["note"]

    def test_unknown_interval_degrades_honestly(self, tmp_path: Path) -> None:
        _seed_futures_lake(tmp_path)
        payload = run_futures_snapshot(
            lake_root=tmp_path, interval="bogus", date="2024-06-13", time=None, top_n=10
        )
        assert payload["instrument_count"] == 0
        assert "pull the F&O futures first" in payload["note"]

    def test_all_options_lake_has_no_futures(self, tmp_path: Path) -> None:
        # An option series (FNO segment but -CE suffix) must NOT register as a future.
        _write(
            tmp_path,
            segment="FNO",
            symbol="NSE-NIFTY-27Jun24-24000-CE",
            interval="1minute",
            bars=[_min_bar(hour=9, minute=15, close="164", volume=1, oi=10)],
        )
        payload = run_futures_snapshot(
            lake_root=tmp_path, interval="1day", date="2024-06-13", time=None, top_n=10
        )
        assert payload["instrument_count"] == 0
        assert "pull the F&O futures first" in payload["note"]

    def test_fut_suffix_but_unparseable_is_dropped(self, tmp_path: Path) -> None:
        # Ends -FUT but the expiry token is malformed (bad month) → parse None,
        # so the dataset is dropped from the futures universe (no future found).
        _write(
            tmp_path,
            segment="FNO",
            symbol="NSE-NIFTY-30Xxx25-FUT",
            interval="1minute",
            bars=[_min_bar(hour=9, minute=15, close="100", volume=1, oi=5)],
        )
        payload = run_futures_snapshot(
            lake_root=tmp_path, interval="1day", date="2024-06-13", time=None, top_n=10
        )
        assert payload["instrument_count"] == 0
        assert "pull the F&O futures first" in payload["note"]

    def test_happy_daily_snapshot(self, tmp_path: Path) -> None:
        _seed_futures_lake(tmp_path)
        payload = run_futures_snapshot(
            lake_root=tmp_path,
            interval="1day",
            date="2024-06-13",
            time=None,
            top_n=10,
            names={"NIFTY": "Nifty 50"},
        )
        assert payload["instrument_count"] == 1
        assert payload["skipped_count"] == 0
        gain = payload["price_gainers"]
        assert len(gain) == 1
        row = gain[0]
        assert row["name"] == "Nifty 50"
        assert row["symbol"] == _NEAR_FUT  # near month, NOT the far one
        assert row["ltp"] == "₹20,100.00"
        assert row["spot"] == "₹20,040.00"
        assert row["premium"] == "₹60.00"  # 20100 − 20040
        assert Decimal(row["pct_change"]) == Decimal("0.5")  # 20100 vs 20000
        assert row["oi"] == 1100
        assert Decimal(row["oi_chg_pct"]) == Decimal("10")  # 1100 vs 1000
        assert row["volume"] == 500
        # Premium > 0 → in the premium bucket, not discount.
        assert [r["symbol"] for r in payload["premium"]] == [_NEAR_FUT]
        assert payload["discount"] == []
        assert [r["symbol"] for r in payload["oi_gainers"]] == [_NEAR_FUT]

    def test_no_underlying_spot_premium_null(self, tmp_path: Path) -> None:
        # Future present but NO underlying spot → premium / spot null, still ranked.
        _write(
            tmp_path,
            segment="FNO",
            symbol=_NEAR_FUT,
            interval="1minute",
            bars=[_min_bar(hour=9, minute=15, close="20100", volume=100, oi=1100)],
        )
        _write(
            tmp_path,
            segment="FNO",
            symbol=_NEAR_FUT,
            interval="1day",
            bars=[_day_bar(day=date(2024, 6, 12), close="20000", oi=1000)],
        )
        payload = run_futures_snapshot(
            lake_root=tmp_path, interval="1day", date="2024-06-13", time=None, top_n=10
        )
        assert payload["instrument_count"] == 1
        row = payload["price_gainers"][0]
        assert row["spot"] is None
        assert row["premium"] is None
        assert payload["premium"] == []
        assert payload["discount"] == []

    def test_all_expired_underlying_is_skipped(self, tmp_path: Path) -> None:
        # Only an already-expired future for NIFTY → no live contract → skipped.
        _write(
            tmp_path,
            segment="FNO",
            symbol="NSE-NIFTY-30Jan24-FUT",
            interval="1minute",
            bars=[_min_bar(hour=9, minute=15, close="100", volume=1, oi=5)],
        )
        payload = run_futures_snapshot(
            lake_root=tmp_path, interval="1day", date="2024-06-13", time=None, top_n=10
        )
        assert payload["instrument_count"] == 1
        assert payload["skipped_count"] == 1
        assert payload["price_gainers"] == []

    def test_near_month_with_no_session_bar_is_skipped(self, tmp_path: Path) -> None:
        # The near-month contract is live but has NO bar on the snapshot day → skip.
        _write(
            tmp_path,
            segment="FNO",
            symbol=_NEAR_FUT,
            interval="1minute",
            bars=[_min_bar(hour=9, minute=15, close="100", volume=1, oi=5, day=date(2024, 6, 10))],
        )
        payload = run_futures_snapshot(
            lake_root=tmp_path, interval="1day", date="2024-06-13", time=None, top_n=10
        )
        assert payload["instrument_count"] == 1
        assert payload["skipped_count"] == 1
        assert payload["price_gainers"] == []

    def test_intraday_cutoff_path(self, tmp_path: Path) -> None:
        _seed_futures_lake(tmp_path)
        # 5-minute interval at 09:15 → only the 09:15 future bar (close 20050).
        payload = run_futures_snapshot(
            lake_root=tmp_path, interval="5minute", date="2024-06-13", time="09:15", top_n=10
        )
        assert payload["instrument_count"] == 1
        row = payload["price_gainers"][0] if payload["price_gainers"] else payload["volume"][0]
        assert row["ltp"] == "₹20,050.00"


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


class TestFuturesApi:
    def test_ranks_a_real_futures_lake(self, store: InstrumentStore, tmp_path: Path) -> None:
        # store.path is tmp_path/instruments.duckdb → lake root is tmp_path.
        _seed_futures_lake(tmp_path)
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        body = client.get(
            "/api/v1/movers",
            params={"asset": "futures", "interval": "1day", "date": "2024-06-13", "top_n": 5},
        ).json()
        assert body["instrument_count"] == 1
        assert body["skipped_count"] == 0
        row = body["price_gainers"][0]
        assert row["symbol"] == _NEAR_FUT
        assert row["ltp"] == "₹20,100.00"
        assert row["premium"] == "₹60.00"
        assert Decimal(row["oi_chg_pct"]) == Decimal("10")

    def test_empty_futures_lake_degrades(self, store: InstrumentStore) -> None:
        # Store wired but no futures parquet → honest empty payload (never 500).
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        body = client.get("/api/v1/movers", params={"asset": "futures"}).json()
        assert body["instrument_count"] == 0
        assert "pull the F&O futures first" in body["note"]

    def test_options_asset_is_now_wired(self) -> None:
        # Milestone 3 wired Options — `asset=options` no longer returns the
        # "ships after Spot" pending note; with no lake it degrades to the
        # options reader's own "pull the F&O options first" empty payload.
        from types import SimpleNamespace

        fake = SimpleNamespace(path=None)
        client = TestClient(build_app(DashboardState(), instrument_store=fake))
        body = client.get("/api/v1/movers", params={"asset": "options"}).json()
        assert body["instrument_count"] == 0
        assert "pull the F&O options first" in body["note"]

    def test_unknown_asset_is_pending(self, store: InstrumentStore) -> None:
        # An unknown asset (not spot/futures/options) still gets the honest
        # "ships after Spot" pending payload.
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        body = client.get("/api/v1/movers", params={"asset": "commodity"}).json()
        assert body["instrument_count"] == 0
        assert "Commodity" in body["note"]
        assert "Spot" in body["note"]

    def test_malformed_date_is_400(self, store: InstrumentStore) -> None:
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        resp = client.get("/api/v1/movers", params={"asset": "futures", "date": "not-a-date"})
        assert resp.status_code == 400
        assert "malformed date" in resp.json()["detail"]

    def test_no_lake_root_futures_empty(self) -> None:
        # A store whose `path` is not a filesystem Path → empty futures payload.
        from types import SimpleNamespace

        fake = SimpleNamespace(path=None)
        client = TestClient(build_app(DashboardState(), instrument_store=fake))
        body = client.get("/api/v1/movers", params={"asset": "futures"}).json()
        assert body["instrument_count"] == 0
        assert "pull the F&O futures first" in body["note"]


# ── _prev_day_oi prior-day path (real lake) ───────────────────────────────


class TestPrevDayOiIntegration:
    def test_prior_day_oi_via_lake(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            segment="FNO",
            symbol=_NEAR_FUT,
            interval="1day",
            bars=[
                _day_bar(day=date(2024, 6, 11), close="19900", oi=800),
                _day_bar(day=date(2024, 6, 12), close="20000", oi=1000),
            ],
        )
        got = _prev_day_oi(tmp_path, exchange=_EXCHANGE, segment="FNO", symbol=_NEAR_FUT, day=_DAY)
        assert got == 1000
