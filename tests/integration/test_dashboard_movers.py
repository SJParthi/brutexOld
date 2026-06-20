"""Integration tests for the markets-screen-style ``/movers`` snapshot (DEC-MOVERS-SNAPSHOT-VIEW-001).

Two layers, both against a REAL Parquet + DuckDB lake:

1. The lake-reading core (:func:`~brutex.dashboard.movers_snapshot.run_movers_snapshot`
   + ``_read_session_bars`` + ``_read_daily_bars`` + ``_prev_day_close``) is exercised
   directly over a ``tmp_path`` lake seeded with
   :class:`~brutex.data.parquet_writer.ParquetWriter` — every branch (intraday
   resample + cutoff slice, the daily-DIRECT 1-day read for the 1-day-only pull,
   missing day, missing 1-minute dir, missing / prior-less 1-day baseline) reached
   deterministically.
2. The ``GET /api/v1/movers`` + ``GET /movers`` route wiring is proven end-to-end
   through FastAPI's :class:`TestClient` (the honest proof the production
   ``Catalog`` lake feeds the snapshot reader). Mirrors the
   ``test_dashboard_coverage.py`` convention (store at ``tmp_path/instruments.duckdb``
   ⇒ lake root ``tmp_path``).

Skipped when ``fastapi`` / ``duckdb`` / ``polars`` / ``pyarrow`` are absent.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("duckdb")
pytest.importorskip("polars")
pytest.importorskip("pyarrow")

from brutex.dashboard.app import DashboardState, build_app
from brutex.dashboard.movers_snapshot import (
    _prev_day_close,
    _read_daily_bars,
    _read_session_bars,
    run_movers_snapshot,
)
from brutex.data.instrument_store import InstrumentStore
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient

_EXCHANGE = "NSE"
_SEGMENT = "INDEX"
_DAY = date(2024, 6, 13)


# ── seeding helpers ───────────────────────────────────────────────────────


def _min_bar(*, hour: int, minute: int, close: str, volume: int, day: date = _DAY) -> CandleBar:
    """A flat 1-minute bar at ``HH:MM`` **IST** on *day* (IST = UTC + 05:30)."""
    c = Decimal(close)
    # IST minute-of-day → UTC by subtracting 5h30m (no DST).
    total = hour * 60 + minute - (5 * 60 + 30)
    return CandleBar(
        timestamp=datetime(day.year, day.month, day.day, total // 60, total % 60, tzinfo=UTC),
        open=c,
        high=c,
        low=c,
        close=c,
        volume=volume,
        open_interest=None,
    )


def _day_bar(*, day: date, open_: str, close: str, volume: int) -> CandleBar:
    """A daily OHLCV bar labelled at 09:15 IST (03:45 UTC) on *day*."""
    o = Decimal(open_)
    c = Decimal(close)
    return CandleBar(
        timestamp=datetime(day.year, day.month, day.day, 3, 45, tzinfo=UTC),
        open=o,
        high=max(o, c),
        low=min(o, c),
        close=c,
        volume=volume,
        open_interest=None,
    )


def _write(tmp_path: Path, symbol: str, interval: str, bars: list[CandleBar]) -> None:
    ParquetWriter(lake_root=tmp_path).write(
        exchange=_EXCHANGE, segment=_SEGMENT, symbol=symbol, interval=interval, bars=bars
    )


def _seed_spot_lake(tmp_path: Path) -> None:
    """NIFTY (+10% gainer) + BANKNIFTY (-10% loser).

    Both the **1-minute** session (for the intraday-timeframe path) AND the stored
    **1-day** bars for the prior close (06-12) AND the snapshot day (06-13) — so the
    DAILY timeframe reads the stored 1-day bar DIRECTLY (the derived-1-day partition,
    DEC-1MINUTE-ONLY-DERIVE-1DAY-001), no 1-minute resample required. The 06-13 1-day close
    equals the 1-minute session close, so both paths agree (NIFTY 22000 = +10% vs
    20000; BANKNIFTY 40500 = -10% vs 45000).
    """
    _write(
        tmp_path,
        "NIFTY",
        "1minute",
        [
            _min_bar(hour=9, minute=15, close="20000", volume=100),
            _min_bar(hour=9, minute=16, close="20100", volume=100),
            _min_bar(hour=9, minute=17, close="22000", volume=500),
        ],
    )
    _write(
        tmp_path,
        "NIFTY",
        "1day",
        [
            _day_bar(day=date(2024, 6, 12), open_="19900", close="20000", volume=300),
            _day_bar(day=_DAY, open_="20000", close="22000", volume=700),
        ],
    )
    _write(
        tmp_path,
        "BANKNIFTY",
        "1minute",
        [
            _min_bar(hour=9, minute=15, close="45000", volume=80),
            _min_bar(hour=9, minute=16, close="44000", volume=80),
            _min_bar(hour=9, minute=17, close="40500", volume=90),
        ],
    )
    _write(
        tmp_path,
        "BANKNIFTY",
        "1day",
        [
            _day_bar(day=date(2024, 6, 12), open_="45000", close="45000", volume=200),
            _day_bar(day=_DAY, open_="45000", close="40500", volume=250),
        ],
    )


# ── _read_session_bars (direct, every branch) ─────────────────────────────


class TestReadSessionBars:
    def test_missing_1minute_dir_returns_empty(self, tmp_path: Path) -> None:
        # No bars written at all → make_lake_read_1m raises ValueError → [].
        assert (
            _read_session_bars(
                tmp_path,
                exchange=_EXCHANGE,
                segment=_SEGMENT,
                symbol="GHOST",
                day=_DAY,
                minutes=5,
                cutoff_min=600,
            )
            == []
        )

    def test_data_on_a_different_day_returns_empty(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "NIFTY",
            "1minute",
            [_min_bar(hour=9, minute=15, close="100", volume=1, day=date(2024, 6, 10))],
        )
        assert (
            _read_session_bars(
                tmp_path,
                exchange=_EXCHANGE,
                segment=_SEGMENT,
                symbol="NIFTY",
                day=_DAY,
                minutes=5,
                cutoff_min=600,
            )
            == []
        )

    def test_intraday_resample_and_cutoff_slice(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "NIFTY",
            "1minute",
            [
                _min_bar(hour=9, minute=15, close="100", volume=10),
                _min_bar(hour=9, minute=16, close="105", volume=10),
                _min_bar(hour=9, minute=20, close="200", volume=10),  # next 5m bucket
            ],
        )
        # 5-minute buckets: 09:15 (label 555) and 09:20 (label 560). cutoff 559
        # keeps ONLY the 09:15 bucket (close=105, volume=20).
        bars = _read_session_bars(
            tmp_path,
            exchange=_EXCHANGE,
            segment=_SEGMENT,
            symbol="NIFTY",
            day=_DAY,
            minutes=5,
            cutoff_min=559,
        )
        assert len(bars) == 1
        assert bars[0].close == Decimal("105")
        assert bars[0].volume == 20

    def test_cutoff_before_session_returns_empty(self, tmp_path: Path) -> None:
        _write(tmp_path, "NIFTY", "1minute", [_min_bar(hour=9, minute=15, close="100", volume=10)])
        # cutoff 0 is before 09:15 (555) → the resampled frame is filtered empty.
        assert (
            _read_session_bars(
                tmp_path,
                exchange=_EXCHANGE,
                segment=_SEGMENT,
                symbol="NIFTY",
                day=_DAY,
                minutes=5,
                cutoff_min=0,
            )
            == []
        )

    def test_daily_no_cutoff_one_session_bar(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "NIFTY",
            "1minute",
            [
                _min_bar(hour=9, minute=15, close="100", volume=10),
                _min_bar(hour=15, minute=25, close="130", volume=40),
            ],
        )
        # Daily (375) bucket, cutoff_min=None → one bar spanning the whole session.
        bars = _read_session_bars(
            tmp_path,
            exchange=_EXCHANGE,
            segment=_SEGMENT,
            symbol="NIFTY",
            day=_DAY,
            minutes=375,
            cutoff_min=None,
        )
        assert len(bars) == 1
        assert bars[0].close == Decimal("130")
        assert bars[0].volume == 50


# ── _read_daily_bars (direct, every branch) ───────────────────────────────


class TestReadDailyBars:
    """The derived-1-day DIRECT reader (DEC-1MINUTE-ONLY-DERIVE-1DAY-001) — no 1-minute resample."""

    def test_missing_1day_dir_returns_empty(self, tmp_path: Path) -> None:
        # No 1-day partition at all → _read_daily_partition is None → [].
        assert (
            _read_daily_bars(
                tmp_path, exchange=_EXCHANGE, segment=_SEGMENT, symbol="GHOST", day=_DAY
            )
            == []
        )

    def test_selected_day_bar_read_directly(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "NIFTY",
            "1day",
            [
                _day_bar(day=date(2024, 6, 12), open_="100", close="111", volume=3),
                _day_bar(day=_DAY, open_="111", close="222", volume=7),
            ],
        )
        bars = _read_daily_bars(
            tmp_path, exchange=_EXCHANGE, segment=_SEGMENT, symbol="NIFTY", day=_DAY
        )
        assert len(bars) == 1
        assert bars[0].close == Decimal("222")
        assert bars[0].volume == 7
        assert bars[0].timestamp.tzinfo is not None  # stored UTC instant preserved

    def test_no_bar_on_selected_day_returns_empty(self, tmp_path: Path) -> None:
        # 1-day data exists, but only for a DIFFERENT day → sel.is_empty → [].
        _write(
            tmp_path,
            "NIFTY",
            "1day",
            [_day_bar(day=date(2024, 6, 12), open_="1", close="2", volume=1)],
        )
        assert (
            _read_daily_bars(
                tmp_path, exchange=_EXCHANGE, segment=_SEGMENT, symbol="NIFTY", day=_DAY
            )
            == []
        )


# ── _prev_day_close (direct, every branch) ────────────────────────────────


class TestPrevDayClose:
    def test_no_1day_dir_is_none(self, tmp_path: Path) -> None:
        assert (
            _prev_day_close(
                tmp_path, exchange=_EXCHANGE, segment=_SEGMENT, symbol="NIFTY", day=_DAY
            )
            is None
        )

    def test_prior_day_close_returned(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "NIFTY",
            "1day",
            [
                _day_bar(day=date(2024, 6, 11), open_="100", close="111", volume=1),
                _day_bar(day=date(2024, 6, 12), open_="111", close="222", volume=1),
            ],
        )
        # The last close strictly BEFORE 2024-06-13 is 2024-06-12's 222.
        got = _prev_day_close(
            tmp_path, exchange=_EXCHANGE, segment=_SEGMENT, symbol="NIFTY", day=_DAY
        )
        assert got == Decimal("222")

    def test_only_same_day_1day_has_no_prior(self, tmp_path: Path) -> None:
        _write(tmp_path, "NIFTY", "1day", [_day_bar(day=_DAY, open_="100", close="105", volume=1)])
        assert (
            _prev_day_close(
                tmp_path, exchange=_EXCHANGE, segment=_SEGMENT, symbol="NIFTY", day=_DAY
            )
            is None
        )


# ── run_movers_snapshot (direct, every branch) ────────────────────────────


class TestRunMoversSnapshot:
    def test_empty_lake_says_pull_spot_first(self, tmp_path: Path) -> None:
        payload = run_movers_snapshot(
            lake_root=tmp_path, interval="1day", date="2024-06-13", time=None, top_n=10
        )
        assert payload["instrument_count"] == 0
        assert "pull spot first" in payload["note"]

    def test_unknown_interval_degrades_honestly(self, tmp_path: Path) -> None:
        _seed_spot_lake(tmp_path)
        payload = run_movers_snapshot(
            lake_root=tmp_path, interval="bogus", date="2024-06-13", time=None, top_n=10
        )
        assert payload["instrument_count"] == 0
        assert "pull spot first" in payload["note"]

    def test_happy_daily_snapshot_ranks_and_names(self, tmp_path: Path) -> None:
        _seed_spot_lake(tmp_path)
        payload = run_movers_snapshot(
            lake_root=tmp_path,
            interval="1day",
            date="2024-06-13",
            time=None,
            top_n=5,
            names={"NIFTY": "Nifty 50", "BANKNIFTY": "Nifty Bank"},
        )
        assert payload["instrument_count"] == 2
        assert payload["skipped_count"] == 0
        # NIFTY 20000→22000 (+10%); BANKNIFTY 45000→40500 (-10%).
        assert [r["symbol"] for r in payload["price_gainers"]] == ["NIFTY"]
        assert [r["symbol"] for r in payload["price_losers"]] == ["BANKNIFTY"]
        assert payload["price_gainers"][0]["name"] == "Nifty 50"
        assert payload["price_gainers"][0]["ltp"] == "₹22,000.00"
        assert Decimal(payload["price_gainers"][0]["pct_change"]) == Decimal("10")
        # Both have fresh-session volume + value.
        assert {r["symbol"] for r in payload["volume"]} == {"NIFTY", "BANKNIFTY"}
        assert {r["symbol"] for r in payload["value"]} == {"NIFTY", "BANKNIFTY"}

    def test_skipped_symbol_and_missing_prev_close(self, tmp_path: Path) -> None:
        # NIFTY: bars on the day + a prior 1-day close (full snapshot).
        _write(
            tmp_path,
            "NIFTY",
            "1minute",
            [_min_bar(hour=9, minute=15, close="100", volume=10)],
        )
        _write(
            tmp_path,
            "NIFTY",
            "1day",
            [_day_bar(day=date(2024, 6, 12), open_="90", close="90", volume=1)],
        )
        # NOPREV: bars on the day but NO 1-day data → prev_close None (pct None).
        _write(
            tmp_path,
            "NOPREV",
            "1minute",
            [_min_bar(hour=9, minute=15, close="50", volume=5)],
        )
        # OLDSYM: 1-minute data only on a DIFFERENT day → skipped (no bars on 06-13).
        _write(
            tmp_path,
            "OLDSYM",
            "1minute",
            [_min_bar(hour=9, minute=15, close="10", volume=1, day=date(2024, 6, 10))],
        )
        payload = run_movers_snapshot(
            lake_root=tmp_path, interval="1minute", date="2024-06-13", time=None, top_n=10
        )
        assert payload["instrument_count"] == 3  # NIFTY + NOPREV counted, OLDSYM skipped
        assert payload["skipped_count"] == 1
        # NIFTY 90→100 = +11.11% (gainer); NOPREV has no prior day → pct None.
        assert [r["symbol"] for r in payload["price_gainers"]] == ["NIFTY"]
        symbols_with_pct = {r["symbol"] for r in payload["volume"] if r["pct_change"] is not None}
        assert symbols_with_pct == {"NIFTY"}
        nopr = next(r for r in payload["volume"] if r["symbol"] == "NOPREV")
        assert nopr["pct_change"] is None
        assert nopr["change"] is None

    def test_daily_discovers_1day_and_skips_symbol_absent_on_the_day(self, tmp_path: Path) -> None:
        # The DAILY timeframe discovers + reads the stored 1-day partitions DIRECTLY
        # (the derived-1-day partition — DEC-1MINUTE-ONLY-DERIVE-1DAY-001), NOT the 1-minute series.
        # HASDAY has a 1-day bar on the snapshot day (ranked); GAPDAY has 1-day data only
        # on a different day → no bar on 06-13 → skipped (never fabricated).
        _write(
            tmp_path,
            "HASDAY",
            "1day",
            [
                _day_bar(day=date(2024, 6, 12), open_="100", close="100", volume=10),
                _day_bar(day=_DAY, open_="100", close="110", volume=20),
            ],
        )
        _write(
            tmp_path,
            "GAPDAY",
            "1day",
            [_day_bar(day=date(2024, 6, 10), open_="50", close="55", volume=5)],
        )
        payload = run_movers_snapshot(
            lake_root=tmp_path, interval="1day", date="2024-06-13", time=None, top_n=10
        )
        assert payload["instrument_count"] == 2  # HASDAY ranked + GAPDAY skipped
        assert payload["skipped_count"] == 1
        assert [r["symbol"] for r in payload["price_gainers"]] == ["HASDAY"]
        assert Decimal(payload["price_gainers"][0]["pct_change"]) == Decimal("10")


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
        [_idx("NIFTY"), _idx("BANKNIFTY")],
        snapshot_date=date(2024, 1, 2),
        run_id="r1",
        csv_source_sha256="0" * 64,
    )
    yield s
    s.close()


class TestMoversApi:
    def test_503_when_store_unwired(self) -> None:
        client = TestClient(build_app(DashboardState(), instrument_store=None))
        assert client.get("/api/v1/movers").status_code == 503

    def test_empty_payload_when_no_lake_root(self) -> None:
        # A store whose `path` is not a filesystem Path → empty payload, never 500.
        fake = SimpleNamespace(path=None)
        client = TestClient(build_app(DashboardState(), instrument_store=fake))
        body = client.get("/api/v1/movers").json()
        assert body["instrument_count"] == 0
        assert body["price_gainers"] == []
        assert "pull spot first" in body["note"]

    def test_400_on_malformed_date(self, store: InstrumentStore) -> None:
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        resp = client.get("/api/v1/movers", params={"date": "not-a-date"})
        assert resp.status_code == 400
        assert "malformed date" in resp.json()["detail"]

    def test_400_on_malformed_time(self, store: InstrumentStore) -> None:
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        resp = client.get("/api/v1/movers", params={"interval": "5minute", "time": "99x99"})
        assert resp.status_code == 400
        assert "malformed time" in resp.json()["detail"]

    def test_unknown_asset_is_pending_not_500(self, store: InstrumentStore) -> None:
        # Spot / Futures / Options are all wired now; an UNKNOWN asset still gets
        # an honest "ships after Spot" pending payload (never a 500). The wired
        # Options tab is covered by test_dashboard_movers_options.py.
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        body = client.get("/api/v1/movers", params={"asset": "commodities"}).json()
        assert body["instrument_count"] == 0
        assert "Spot" in body["note"]

    def test_empty_lake_degrades_honestly(self, store: InstrumentStore) -> None:
        # Store wired but no parquet written → honest empty payload (never 500).
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        body = client.get("/api/v1/movers").json()
        assert body["instrument_count"] == 0
        assert "pull spot first" in body["note"]

    def test_ranks_a_real_parquet_lake(self, store: InstrumentStore, tmp_path: Path) -> None:
        # store.path is tmp_path/instruments.duckdb → lake root is tmp_path.
        _seed_spot_lake(tmp_path)
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        body = client.get(
            "/api/v1/movers",
            params={"asset": "spot", "interval": "1day", "date": "2024-06-13", "top_n": 5},
        ).json()
        assert body["instrument_count"] == 2
        assert body["skipped_count"] == 0
        assert [r["symbol"] for r in body["price_gainers"]] == ["NIFTY"]
        assert [r["symbol"] for r in body["price_losers"]] == ["BANKNIFTY"]
        assert body["price_gainers"][0]["ltp"] == "₹22,000.00"
        assert Decimal(body["price_gainers"][0]["pct_change"]) == Decimal("10")
        assert {r["symbol"] for r in body["volume"]} == {"NIFTY", "BANKNIFTY"}


class TestMoversPage:
    def test_movers_page_renders(self, store: InstrumentStore) -> None:
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        resp = client.get("/movers")
        assert resp.status_code == 200
        assert "Movers" in resp.text
        assert "/api/v1/movers" in resp.text
        # Markets-screen-style tabs + the cross-page nav + the click-to-sort head wiring.
        assert 'data-asset="spot"' in resp.text
        assert 'data-asset="futures"' in resp.text
        assert 'data-asset="options"' in resp.text
        assert 'href="/coverage"' in resp.text
        assert 'href="/reports"' in resp.text
        assert 'id="head"' in resp.text
        # Options cadence toggle (All/Monthly/Weekly) wiring (DEC-MOVERS-OPTIONS-TRACK-001).
        assert 'id="track"' in resp.text
        # A second render is served verbatim from the module template cache
        # (exercises the cached branch of `_load_movers_template`).
        again = client.get("/movers")
        assert again.status_code == 200
        assert again.text == resp.text

    def test_movers_link_in_every_pages_nav(self, store: InstrumentStore) -> None:
        # DEC-INDEX-CONSTITUENTS-COVERAGE-001: the /movers page must be reachable
        # from the top menu of EVERY page — not just by typing the URL. Each page's
        # nav now carries a "Movers" link (alongside Database · Coverage · Reports).
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        # The v29 main dashboard ("/") uses its own live-sweep layout (no standard
        # top nav); the standard-nav pages all carry the Movers link.
        for path in ("/welcome", "/coverage", "/reports", "/db", "/universe", "/groww-credentials"):
            resp = client.get(path)
            assert resp.status_code == 200, path
            assert 'href="/movers"' in resp.text, f"{path} is missing the Movers nav link"
