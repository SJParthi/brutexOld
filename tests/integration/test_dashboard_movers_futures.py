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
from typing import Any

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
    compute_futures_candidates,
    run_futures_snapshot,
)
from brutex.data.instrument_store import InstrumentStore
from brutex.data.models import CandleBar, FnoGreeksBar
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
        ltp, volume, value_paisa, oi, iv, delta = read
        assert ltp == Decimal("20100.00")
        assert volume == 500
        # A plain (never-enriched) frame carries no Greeks overlay → honest None
        # (DEC-GREEKS-ENGINE-PR3B-001), never a fabricated value.
        assert iv is None
        assert delta is None
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
        ltp, volume, value_paisa, oi, iv, delta = read
        assert iv is None and delta is None  # plain frame → no overlay
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


def _greeks_min_bar(
    *,
    hour: int,
    minute: int,
    close: str,
    volume: int,
    oi: int | None,
    iv: float | None,
    delta: float | None,
    day: date = _DAY,
) -> FnoGreeksBar:
    """An ENRICHED option 1-minute bar (stored Greeks overlay) at ``HH:MM`` IST."""
    c = Decimal(close)
    total = hour * 60 + minute - (5 * 60 + 30)
    return FnoGreeksBar(
        timestamp=datetime(day.year, day.month, day.day, total // 60, total % 60, tzinfo=UTC),
        open=c,
        high=c,
        low=c,
        close=c,
        volume=volume,
        open_interest=oi,
        iv=iv,
        delta=delta,
    )


class TestSessionGreeksOverlay:
    """The session reader's stored-Greeks extraction (DEC-GREEKS-ENGINE-PR3B-001):
    the LAST kept bar's iv/delta off the SAME frame — zero extra I/O, schema-tolerant,
    honest None for a legacy frame / a stamped-NULL bar, never backfilled."""

    _OPT = "NSE-NIFTY-27Jun24-24000-CE"

    def _seed_enriched(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            segment="FNO",
            symbol=self._OPT,
            interval="1minute",
            bars=[
                _greeks_min_bar(
                    hour=9, minute=15, close="150", volume=100, oi=1050, iv=0.17, delta=0.58
                ),
                _greeks_min_bar(
                    hour=9, minute=16, close="164", volume=400, oi=1100, iv=0.1812, delta=0.62
                ),
            ],
        )

    def test_enriched_frame_returns_last_bar_greeks(self, tmp_path: Path) -> None:
        self._seed_enriched(tmp_path)
        read = _read_future_session(
            tmp_path, exchange=_EXCHANGE, segment="FNO", symbol=self._OPT, day=_DAY, cutoff_min=None
        )
        assert read is not None
        assert read[4] == 0.1812  # the LAST bar's stored iv — not the first's
        assert read[5] == 0.62

    def test_cutoff_selects_that_bars_greeks(self, tmp_path: Path) -> None:
        """An intraday cutoff snapshots the AS-OF bar's Greeks — the same last-bar
        rule LTP/OI use, so iv/delta always describe the shown snapshot instant."""
        self._seed_enriched(tmp_path)
        read = _read_future_session(
            tmp_path, exchange=_EXCHANGE, segment="FNO", symbol=self._OPT, day=_DAY, cutoff_min=555
        )
        assert read is not None
        assert read[0] == Decimal("150.00")
        assert read[4] == 0.17
        assert read[5] == 0.58

    def test_stamped_null_last_bar_reads_none_never_backfilled(self, tmp_path: Path) -> None:
        """A §15.8 stamped-NULL LAST bar yields None even though an EARLIER bar has
        solved Greeks — honest NA, never backfilled (Golden Rule #1)."""
        _write(
            tmp_path,
            segment="FNO",
            symbol=self._OPT,
            interval="1minute",
            bars=[
                _greeks_min_bar(
                    hour=9, minute=15, close="150", volume=100, oi=1050, iv=0.17, delta=0.58
                ),
                _greeks_min_bar(
                    hour=9, minute=16, close="164", volume=400, oi=1100, iv=None, delta=None
                ),
            ],
        )
        read = _read_future_session(
            tmp_path, exchange=_EXCHANGE, segment="FNO", symbol=self._OPT, day=_DAY, cutoff_min=None
        )
        assert read is not None
        assert read[4] is None
        assert read[5] is None


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
        # The NAME is the FULL dated contract label using the renamed underlying
        # (DEC-MOVERS-FULL-CONTRACT-NAME-001); the raw symbol is still carried.
        assert row["name"] == "Nifty 50 27-Jun-2024 FUT"
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

    def test_min_volume_floor_filters_futures_volume_ranking(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        # DEC-MOVERS-MIN-VOLUME-001: the single near future has cumulative session volume
        # 500; a floor of 1000 drops it from the Top Volume ranking but the price ranking
        # (its only mover) is unaffected — the floor applies to the volume category only.
        _seed_futures_lake(tmp_path)
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        body = client.get(
            "/api/v1/movers",
            params={
                "asset": "futures",
                "interval": "1day",
                "date": "2024-06-13",
                "min_volume": 1000,
            },
        ).json()
        assert body["min_volume"] == 1000
        assert body["volume"] == []  # the 500-volume future is below the floor
        assert "Min volume ≥ 1,000." in body["note"]
        # The future still appears in the price ranking (the floor is volume-only).
        assert len(body["price_gainers"]) == 1

    def test_min_volume_default_zero_byte_identical_futures(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        _seed_futures_lake(tmp_path)
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        body = client.get(
            "/api/v1/movers",
            params={"asset": "futures", "interval": "1day", "date": "2024-06-13"},
        ).json()
        assert body["min_volume"] == 0
        assert len(body["volume"]) == 1
        assert "Min volume" not in body["note"]

    def test_default_intraday_echoes_latest_time_from_spot(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        # DEC-MOVERS-ASOF-LATEST-DEFAULT-001: the additive `latest_time` echo rides the
        # Futures payload too, resolved from the SPOT 1-minute (INDEX NIFTY → 09:16 here);
        # `time` stays null (whole-day cut, identical latest close across every TF).
        _seed_futures_lake(tmp_path)
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        body = client.get(
            "/api/v1/movers",
            params={"asset": "futures", "interval": "3minute", "date": "2024-06-13"},
        ).json()
        assert body["latest_time"] == "09:16"
        assert body["time"] is None

    def test_daily_futures_latest_time_is_none(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        _seed_futures_lake(tmp_path)
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        body = client.get(
            "/api/v1/movers",
            params={"asset": "futures", "interval": "1day", "date": "2024-06-13"},
        ).json()
        assert body["latest_time"] is None


class TestFuturesExpiryTier:
    """The Near / Next / Far serial-expiry selector (DEC-MOVERS-FUTURES-TIER-001).

    The lake seeds ONE underlying (NIFTY) with two live serial futures:
    ``_NEAR_FUT`` (27Jun24) and ``_FAR_FUT`` (25Jul24). Near must pick the front
    month, Next the second serial, Far (no third contract) must honestly skip —
    and the three must cache independently (no stale Near row served for Next).
    """

    def _client(self, store: InstrumentStore, tmp_path: Path) -> TestClient:
        _seed_futures_lake(tmp_path)
        return TestClient(build_app(DashboardState(), instrument_store=store))

    def _get(self, client: TestClient, tier: str | None) -> dict[str, Any]:
        params: dict[str, str] = {"asset": "futures", "interval": "1day", "date": "2024-06-13"}
        if tier is not None:
            params["expiry"] = tier
        result: dict[str, Any] = client.get("/api/v1/movers", params=params).json()
        return result

    def test_near_picks_front_month(self, store: InstrumentStore, tmp_path: Path) -> None:
        body = self._get(self._client(store, tmp_path), "Near")
        assert body["expiry_tier"] == "near"
        assert body["price_gainers"][0]["symbol"] == _NEAR_FUT

    def test_blank_expiry_defaults_to_near(self, store: InstrumentStore, tmp_path: Path) -> None:
        body = self._get(self._client(store, tmp_path), None)
        assert body["expiry_tier"] == "near"
        assert body["price_gainers"][0]["symbol"] == _NEAR_FUT

    def test_next_picks_second_serial(self, store: InstrumentStore, tmp_path: Path) -> None:
        body = self._get(self._client(store, tmp_path), "Next")
        assert body["expiry_tier"] == "next"
        assert body["instrument_count"] == 1
        # The second serial (25Jul24) is selected — present in the volume ranking
        # (it has a fresh-session 1-minute bar). This is a DIFFERENT contract than
        # Near, proving the tier actually changes the pick.
        assert body["volume"][0]["symbol"] == _FAR_FUT

    def test_far_beyond_available_is_skipped(self, store: InstrumentStore, tmp_path: Path) -> None:
        body = self._get(self._client(store, tmp_path), "Far")
        assert body["expiry_tier"] == "far"
        # No third serial contract exists → the underlying is honestly skipped,
        # never a fabricated or wrong-tier row (Golden Rule #1).
        assert body["instrument_count"] == 1
        assert body["skipped_count"] == 1
        assert body["price_gainers"] == []
        assert body["volume"] == []

    def test_tiers_cache_independently(self, store: InstrumentStore, tmp_path: Path) -> None:
        # The stale-cache regression: Near then Next on the SAME client must NOT
        # serve the cached Near rows for Next (the tier is part of the cache key).
        client = self._client(store, tmp_path)
        near = self._get(client, "Near")
        nxt = self._get(client, "Next")
        assert near["price_gainers"][0]["symbol"] == _NEAR_FUT
        assert nxt["volume"][0]["symbol"] == _FAR_FUT
        assert near["volume"][0]["symbol"] != nxt["volume"][0]["symbol"]

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


# ── compute_futures_candidates discovery + resilience branches ────────────
# These exercise the per-file gate branches the broad dashboard suite covered
# but the narrowly-mapped tests did not: the _discover_futures segment/interval
# skip and the compute_futures_candidates corrupt-Parquet skip
# (DEC-MOVERS-CORRUPT-SKIP-001).


class TestComputeFuturesCandidatesBranches:
    def test_non_one_minute_fno_dataset_is_skipped_in_discovery(self, tmp_path: Path) -> None:
        # A real FUT future is discovered; a sibling FNO dataset at a NON-1minute
        # interval (here 1day) is skipped at the segment/interval guard in
        # _discover_futures, so only the 1minute future registers.
        _write(
            tmp_path,
            segment="FNO",
            symbol=_NEAR_FUT,
            interval="1minute",
            bars=[_min_bar(hour=9, minute=15, close="20010", volume=100, oi=1000)],
        )
        _write(
            tmp_path,
            segment="FNO",
            symbol=_FAR_FUT,
            interval="1day",
            bars=[_day_bar(day=_DAY, close="20100", oi=900)],
        )
        result = compute_futures_candidates(
            lake_root=tmp_path, interval="1day", date="2024-06-13", time=None
        )
        assert {m.underlying for m in result.movers} == {"NIFTY"}
        assert result.skipped == 0

    def test_corrupt_future_partition_is_skipped_not_raised(self, tmp_path: Path) -> None:
        # A single corrupt future Parquet is SKIPPED (logged + counted), never
        # 500-ing the futures screen (DEC-MOVERS-CORRUPT-SKIP-001).
        from structlog.testing import capture_logs

        good = "NSE-NIFTY-27Jun24-FUT"
        bad = "NSE-BANKNIFTY-27Jun24-FUT"
        _write(
            tmp_path,
            segment="FNO",
            symbol=good,
            interval="1minute",
            bars=[_min_bar(hour=9, minute=15, close="20010", volume=100, oi=1000)],
        )
        _write(
            tmp_path,
            segment="FNO",
            symbol=bad,
            interval="1minute",
            bars=[_min_bar(hour=9, minute=15, close="48010", volume=80, oi=900)],
        )
        corrupt = list((tmp_path / "bars" / _EXCHANGE / "FNO" / bad / "1minute").rglob("*.parquet"))
        assert corrupt, "expected the BANKNIFTY future 1minute partition to exist"
        for path in corrupt:
            path.write_bytes(b"not a parquet file \r\n garbage")

        with capture_logs() as logs:
            result = compute_futures_candidates(
                lake_root=tmp_path, interval="1day", date="2024-06-13", time=None
            )
        assert {m.underlying for m in result.movers} == {"NIFTY"}
        assert result.skipped == 1
        skips = [e for e in logs if e["event"] == "movers.parquet_skip"]
        assert len(skips) == 1
        assert skips[0]["source"] == "futures"
        assert skips[0]["symbol"] == bad


# ── prev-close/OI calendar-contiguity goldens (DEC-MOVERS-PREVCLOSE-CONTIGUITY-001) ──

from structlog.testing import capture_logs  # noqa: E402

#: The FROZEN additive-key vocabulary this decision may ever add to a row.
_CONTIGUITY_KEYS = frozenset(
    {
        "prev_close_session",
        "prev_close_contiguous",
        "oi_baseline_session",
        "oi_baseline_contiguous",
    }
)

#: The pre-fix futures row key set (the byte-identity vocabulary).
_FUTURES_ROW_KEYS = frozenset(
    {
        "name",
        "symbol",
        "exchange",
        "segment",
        "ltp",
        "spot",
        "premium",
        "pct_change",
        "oi",
        "oi_chg_pct",
        "volume",
        "value_cr",
    }
)

_G_DAY = date(2025, 9, 29)  # Mon — the repro snapshot day
_G_FUT = "NSE-NIFTY-30Sep25-FUT"  # expiry 2025-09-30 ≥ snapshot → near tier


def _seed_contiguity_future(tmp_path: Path, *, prior_day: date) -> None:
    """One near FUT: session bars on Mon 2025-09-29 + ONE prior 1-day bar on
    *prior_day* (close 20000, OI 1000) — Fri 2025-09-26 contiguous when passed,
    Thu 2025-09-25 the lake-hole arm."""
    _write(
        tmp_path,
        segment="FNO",
        symbol=_G_FUT,
        interval="1minute",
        bars=[
            _min_bar(hour=9, minute=15, close="20050", volume=100, oi=1050, day=_G_DAY),
            _min_bar(hour=9, minute=16, close="20100", volume=400, oi=1100, day=_G_DAY),
        ],
    )
    _write(
        tmp_path,
        segment="FNO",
        symbol=_G_FUT,
        interval="1day",
        bars=[_day_bar(day=prior_day, close="20000", oi=1000)],
    )
    # Underlying NIFTY spot under INDEX (LTP 20040) for the premium leg.
    _write(
        tmp_path,
        segment="INDEX",
        symbol="NIFTY",
        interval="1minute",
        bars=[_min_bar(hour=9, minute=16, close="20040", volume=50, oi=None, day=_G_DAY)],
    )


class TestFuturesContiguityGoldens:
    def test_contiguous_row_is_byte_identical_no_warn(self, tmp_path: Path) -> None:
        # Prior 1-day on Fri 2025-09-26 = the immediately-preceding session of
        # Mon 2025-09-29 (weekend spanned) → NO additive key, no WARN.
        _seed_contiguity_future(tmp_path, prior_day=date(2025, 9, 26))
        with capture_logs() as logs:
            payload = run_futures_snapshot(
                lake_root=tmp_path, interval="1day", date=_G_DAY.isoformat(), time=None, top_n=5
            )
        (row,) = payload["price_gainers"]
        # FULL-row byte identity: the exact pre-fix wire, computed independently.
        assert row == {
            "name": "NIFTY 30-Sep-2025 FUT",
            "symbol": _G_FUT,
            "exchange": "NSE",
            "segment": "FNO",
            "ltp": "₹20,100.00",
            "spot": "₹20,040.00",
            "premium": "₹60.00",
            "pct_change": str(
                (Decimal("20100.00") - Decimal("20000.00")) / Decimal("20000.00") * 100
            ),
            "oi": 1100,
            "oi_chg_pct": str((Decimal(1100) - Decimal(1000)) / Decimal(1000) * 100),
            "volume": 500,
            # HLC3 turnover: 20050x100 + 20100x400 = 1.0045 Cr -> 2dp half-up.
            "value_cr": "1.00",
        }
        assert set(row) == _FUTURES_ROW_KEYS
        assert set(row) & _CONTIGUITY_KEYS == set()
        assert [e for e in logs if e["event"].startswith("movers.prev_baseline")] == []

    def test_noncontiguous_close_and_oi_both_stamped_two_warns(self, tmp_path: Path) -> None:
        # The lake-hole arm: the ONLY prior 1-day bar sits on Thu 2025-09-25
        # while Fri 2025-09-26 (the true prior session) is a hole — BOTH the
        # close and the OI baselines ride that bar → both flagged, one WARN
        # per kind, served values UNCHANGED.
        _seed_contiguity_future(tmp_path, prior_day=date(2025, 9, 25))
        with capture_logs() as logs:
            payload = run_futures_snapshot(
                lake_root=tmp_path, interval="1day", date=_G_DAY.isoformat(), time=None, top_n=5
            )
        (row,) = payload["price_gainers"]
        # VALUE-EQUALITY: identical served numbers to the contiguous golden —
        # the fix stamps, it never changes a value.
        assert row["ltp"] == "₹20,100.00"
        assert row["pct_change"] == str(
            (Decimal("20100.00") - Decimal("20000.00")) / Decimal("20000.00") * 100
        )
        assert row["oi_chg_pct"] == str((Decimal(1100) - Decimal(1000)) / Decimal(1000) * 100)
        # The four additive keys, both baselines independently False.
        assert row["prev_close_session"] == "2025-09-25"
        assert row["prev_close_contiguous"] is False
        assert row["oi_baseline_session"] == "2025-09-25"
        assert row["oi_baseline_contiguous"] is False
        assert set(row) - _FUTURES_ROW_KEYS == _CONTIGUITY_KEYS
        # Exactly TWO warns — one per kind, independently.
        warns = [e for e in logs if e["event"] == "movers.prev_baseline_noncontiguous"]
        assert sorted((e["kind"], e["symbol"]) for e in warns) == [
            ("oi", _G_FUT),
            ("prev_close", _G_FUT),
        ]
        for e in warns:
            assert e["found_session"] == "2025-09-25"
            assert e["expected_session"] == "2025-09-26"
