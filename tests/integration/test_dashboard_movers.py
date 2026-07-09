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
from typing import Any

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
    filter_to_ist_minute,
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
        # Unified raw-1m semantic (DEC-HARDENING-LEFTOVERS-001): cutoff 559 keeps
        # the RAW minutes 09:15 + 09:16 (minute-of-day ≤ 559) and EXCLUDES the raw
        # 09:20 bar BEFORE resampling, so only the 09:15 5-minute bucket forms
        # (close=105, volume=20) — the cut is on raw minutes, NOT on the resampled
        # bucket-open labels (that pre-#1069 mechanism gave the same outcome here
        # by coincidence).
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

    def test_as_of_cutoff_excludes_forming_bucket_minutes(self, tmp_path: Path) -> None:
        """DEC-HARDENING-LEFTOVERS-001 regression — the old spot forming-bucket leak.

        Before the fix the cutoff was applied AFTER resampling on the bucket-OPEN
        minute, so an as-of 09:18 snapshot on TF=5m kept the WHOLE 09:15 bucket —
        including the 09:19 bar PAST the as-of instant (LTP 400, volume 40). The
        unified raw-1m cut keeps only the elapsed minutes ≤ 09:18, so the last
        bucket honestly aggregates 3 bars (LTP 300, volume 30).
        """
        _write(
            tmp_path,
            "NIFTY",
            "1minute",
            [
                _min_bar(hour=9, minute=15, close="100", volume=10),
                _min_bar(hour=9, minute=16, close="200", volume=10),
                _min_bar(hour=9, minute=18, close="300", volume=10),
                _min_bar(hour=9, minute=19, close="400", volume=10),  # PAST the as-of
            ],
        )
        bars = _read_session_bars(
            tmp_path,
            exchange=_EXCHANGE,
            segment=_SEGMENT,
            symbol="NIFTY",
            day=_DAY,
            minutes=5,
            cutoff_min=9 * 60 + 18,  # as of 09:18 IST, mid-bucket
        )
        assert len(bars) == 1
        # Only the ELAPSED minutes: the 09:19 bar leaks under the OLD semantic
        # (close 400 / volume 40); the honest as-of cut excludes it.
        assert bars[0].close == Decimal("300")
        assert bars[0].volume == 30

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


# ── filter_to_ist_minute (the ONE COMMON as-of cut, DEC-HARDENING-LEFTOVERS-001) ──


class TestFilterToIstMinute:
    """The shared raw-1m IST minute-of-day cutoff (spot + futures + options)."""

    @staticmethod
    def _frame(ts_col: str, *hhmm: tuple[int, int]) -> Any:
        """A minimal 1-column UTC frame whose rows sit at the given IST HH:MM."""
        import polars as pl

        stamps = []
        for hour, minute in hhmm:
            total = hour * 60 + minute - (5 * 60 + 30)  # IST → UTC (fixed +05:30)
            stamps.append(
                datetime(_DAY.year, _DAY.month, _DAY.day, total // 60, total % 60, tzinfo=UTC)
            )
        return pl.DataFrame({ts_col: stamps}).with_columns(
            pl.col(ts_col).dt.convert_time_zone("UTC")
        )

    def test_boundary_minute_is_kept_inclusive(self) -> None:
        frame = self._frame("ts_utc", (9, 15), (9, 16), (9, 17))
        kept = filter_to_ist_minute(frame, 9 * 60 + 16, ts_col="ts_utc")
        assert kept.height == 2  # 09:15 and the ON-boundary 09:16; 09:17 dropped

    def test_afternoon_minute_survives_the_int32_cast(self) -> None:
        # 15:25 IST = minute-of-day 925 — an Int8 hour*60 would overflow (15*60
        # wraps); the helper's Int32 cast keeps the true minute-of-day.
        frame = self._frame("ts_utc", (15, 25), (15, 29))
        kept = filter_to_ist_minute(frame, 15 * 60 + 25, ts_col="ts_utc")
        assert kept.height == 1

    def test_futures_timestamp_column_spelling(self) -> None:
        # The futures/options reader carries `timestamp`, not `ts_utc` — the
        # ONE helper serves both spellings via ts_col (CLAUDE.md §11 COMMON).
        frame = self._frame("timestamp", (9, 15), (9, 20))
        kept = filter_to_ist_minute(frame, 9 * 60 + 15, ts_col="timestamp")
        assert kept.height == 1


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

    def test_reflected_asset_note_carries_no_unescaped_tag(self, store: InstrumentStore) -> None:
        # DEC-DASHBOARD-XSS-HARDENING-001 (GAP B, server defense-in-depth): the
        # unknown-asset note echoes the raw ?asset= query param. A reflected
        # `<img src=x onerror=alert(1)>` must come back HTML-escaped in the JSON
        # note (never a live tag) so any consumer of the payload is safe.
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        resp = client.get("/api/v1/movers", params={"asset": "<img src=x onerror=alert(1)>"})
        assert resp.status_code == 200
        note = resp.json()["note"]
        # NO literal angle bracket survives → the browser can never re-form a
        # live `<img …>` tag from the reflected note (the XSS sink is closed).
        assert "<" not in note
        assert ">" not in note
        assert "<img" not in note  # no live tag in the reflected note
        # The inert escaped form is present (title-cased to "&lt;Img …" by .title()).
        assert "&lt;img" in note.lower()

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

    def test_pull_active_probe_prefers_resample_over_stored(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        """The route passes ``prefer_resample=_pull_live()``
        (DEC-MOVERS-GATE-PREBUILD-SCOPE-001): pull running → the 1-minute
        resample serves WITHOUT paying the freshness compare (the zero-cost
        mid-pull bypass). CONSCIOUSLY FLIPPED idle arm
        (DEC-MOVERS-STORED-FRESHNESS-001): the idle read used to serve the
        POISONED stale stored close (``(1, 0)`` + ``₹99,999.00``) — the
        always-on freshness guard now catches the drift on the idle path too,
        so BOTH arms serve the fresh close; ``prefer_resample`` remains as
        defense-in-depth + the mid-pull compare-cost bypass, no longer the
        only net."""
        # Fresh 1-minute session 09:15–09:19 + a STALE stored 5minute partition
        # whose poisoned close the resample can never produce.
        writer = ParquetWriter(lake_root=tmp_path)
        writer.write(
            exchange=_EXCHANGE,
            segment=_SEGMENT,
            symbol="NIFTY",
            interval="1minute",
            bars=[
                _min_bar(hour=9, minute=15 + i, close=str(22000 + i * 10), volume=100 + i)
                for i in range(5)
            ],
        )
        writer.write(
            exchange=_EXCHANGE,
            segment=_SEGMENT,
            symbol="NIFTY",
            interval="5minute",
            bars=[_min_bar(hour=9, minute=15, close="99999", volume=1)],
        )
        params = {
            "asset": "spot",
            "interval": "5minute",
            "date": "2024-06-13",
            "time": "09:19",
        }
        # A pull IS running (injected probe) → the always-fresh resample serves.
        pulling = TestClient(
            build_app(DashboardState(), instrument_store=store, pull_active_probe=lambda: True)
        )
        body = pulling.get("/api/v1/movers", params=params).json()
        assert (body["stored_reads"], body["resampled_reads"]) == (0, 1)
        # No prior-day 1day baseline → the row ranks under volume (no % change).
        assert body["volume"][0]["ltp"] == "₹22,040.00"  # the FRESH close
        # No pull → the freshness guard ITSELF catches the stale stored
        # partition (DEC-MOVERS-STORED-FRESHNESS-001): the fallback serves the
        # SAME fresh close — the stale ₹99,999.00 illusion is gone on idle too.
        idle = TestClient(
            build_app(DashboardState(), instrument_store=store, pull_active_probe=lambda: False)
        )
        body2 = idle.get("/api/v1/movers", params=params).json()
        assert (body2["stored_reads"], body2["resampled_reads"]) == (0, 1)
        assert body2["volume"][0]["ltp"] == "₹22,040.00"  # the FRESH close, both arms

    def test_raising_pull_probe_is_swallowed_to_idle_read(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        """The ``_pull_live`` closure's never-break-a-read guarantee (app.py):
        an injected pull-active probe that RAISES is swallowed to ``False``
        (the documented "a raising probe is swallowed" except-arm), so the
        movers read lands on the IDLE path — a 200, never a 500. CONSCIOUSLY
        FLIPPED (DEC-MOVERS-STORED-FRESHNESS-001): on the idle path the
        freshness guard itself now catches the stale stored partition, so the
        swallowed probe serves the FRESH resample (``(0, 1)`` + ``₹22,040.00``)
        instead of the poisoned stored close — the never-500 pin is retained."""
        # Same lake shape as the pin above: fresh 1-minute session + a stale
        # aligned stored 5minute partition whose poisoned close identifies
        # which read path served the row.
        writer = ParquetWriter(lake_root=tmp_path)
        writer.write(
            exchange=_EXCHANGE,
            segment=_SEGMENT,
            symbol="NIFTY",
            interval="1minute",
            bars=[
                _min_bar(hour=9, minute=15 + i, close=str(22000 + i * 10), volume=100 + i)
                for i in range(5)
            ],
        )
        writer.write(
            exchange=_EXCHANGE,
            segment=_SEGMENT,
            symbol="NIFTY",
            interval="5minute",
            bars=[_min_bar(hour=9, minute=15, close="99999", volume=1)],
        )

        def _wedged_probe() -> bool:
            raise RuntimeError("pull-job probe wedged")

        client = TestClient(
            build_app(DashboardState(), instrument_store=store, pull_active_probe=_wedged_probe)
        )
        resp = client.get(
            "/api/v1/movers",
            params={
                "asset": "spot",
                "interval": "5minute",
                "date": "2024-06-13",
                "time": "09:19",
            },
        )
        assert resp.status_code == 200  # the probe error never breaks the read
        body = resp.json()
        # Swallowed to False ⇒ no pull signal ⇒ the IDLE path serves — where
        # the freshness guard catches the stale partition and the fallback
        # serves the fresh close (byte-identical to the idle arm above).
        assert (body["stored_reads"], body["resampled_reads"]) == (0, 1)
        assert body["volume"][0]["ltp"] == "₹22,040.00"  # the FRESH close

    def test_no_date_defaults_to_latest_available_day(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        # DEC-PULL-MOVERS-WARM-001: with NO ?date=, the route defaults to the LATEST
        # available spot day (2024-06-13 here), NOT today — so movers show DATA on open
        # right after a pull (the lake fills only to the prior day, so a today-default
        # would be empty), and the post-pull warm (which keys on that same latest day)
        # is a cache HIT, not a miss.
        _seed_spot_lake(tmp_path)
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        body = client.get("/api/v1/movers", params={"asset": "spot", "interval": "1day"}).json()
        assert body["date"] == "2024-06-13"  # the latest seeded day, not today
        assert body["instrument_count"] == 2  # NIFTY + BANKNIFTY, never an empty today
        assert [r["symbol"] for r in body["price_gainers"]] == ["NIFTY"]

    def test_topn_default_is_100_and_cap_is_500(self, store: InstrumentStore) -> None:
        # DEC-MOVERS-COMPLETENESS-001: the operator-directed deeper default + cap. The
        # cap boundary is enforced by the route (≤ 500 ok, > 500 → 422), never silently
        # clamped — so a deep markets screen is honest about its limit.
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        # No explicit top_n → the new 100 default is echoed back in the payload.
        assert client.get("/api/v1/movers").json()["top_n"] == 100
        assert client.get("/api/v1/movers", params={"top_n": 500}).status_code == 200
        assert client.get("/api/v1/movers", params={"top_n": 501}).status_code == 422

    def test_non_default_timeframe_resolves_not_422(self, store: InstrumentStore) -> None:
        # Every one of the 21 canonical TFs is a valid movers interval (derived from
        # 1-minute on-the-fly) — a 2hour/3hour request degrades to an honest empty
        # payload on an empty lake, never a 422/500 (DEC-MOVERS-COMPLETENESS-001).
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        for interval in ("2hour", "3hour", "7minute", "14minute"):
            resp = client.get("/api/v1/movers", params={"interval": interval, "time": "09:30"})
            assert resp.status_code == 200, f"{interval} must not 422"
            assert resp.json()["interval"] == interval

    def test_min_volume_floor_filters_spot_volume_ranking(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        # DEC-MOVERS-MIN-VOLUME-001: NIFTY 1day volume 700, BANKNIFTY 250 → a floor of
        # 400 keeps only NIFTY in the Top Volume ranking; price rankings are untouched.
        _seed_spot_lake(tmp_path)
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        body = client.get(
            "/api/v1/movers",
            params={
                "asset": "spot",
                "interval": "1day",
                "date": "2024-06-13",
                "min_volume": 400,
            },
        ).json()
        assert body["min_volume"] == 400
        assert [r["symbol"] for r in body["volume"]] == ["NIFTY"]
        assert "Min volume ≥ 400." in body["note"]
        # The price rankings still rank BOTH instruments.
        assert len(body["price_gainers"]) + len(body["price_losers"]) == 2

    def test_min_volume_default_zero_byte_identical_spot(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        _seed_spot_lake(tmp_path)
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        body = client.get(
            "/api/v1/movers",
            params={"asset": "spot", "interval": "1day", "date": "2024-06-13"},
        ).json()
        assert body["min_volume"] == 0
        assert {r["symbol"] for r in body["volume"]} == {"NIFTY", "BANKNIFTY"}
        assert "Min volume" not in body["note"]


def _seed_finalized_day_lake(tmp_path: Path) -> None:
    """A FINALIZED trading day whose last three 1-minute closes are DISTINCT.

    NIFTY 1-minute: 09:15 (20000) then the late trio 15:27 (21800) / 15:28
    (21900) / 15:29 (22000) — so an as-of pinned to a coarse TF's last grid slot
    (15:27) would freeze at 21800 while 1minute reaches 22000. The stored 1-day
    bar's close == the 15:29 close (22000), so the DAILY read agrees with the
    whole-day 1-minute last bar. Prior-day (06-12) 1-day close 20000 gives a
    clean +10% gainer. Used by DEC-MOVERS-ASOF-LATEST-DEFAULT-001's equality pin.

    The _DAY 1-day volume is 800 — the 1-minute session sum (100+100+100+500),
    RE-SEEDED derive-consistent for DEC-MOVERS-STORED-FRESHNESS-001 (the prior
    700 drifted from the session, which would reroute the daily arms to the
    stale fallback and stop this fixture pinning the STORED path).
    """
    _write(
        tmp_path,
        "NIFTY",
        "1minute",
        [
            _min_bar(hour=9, minute=15, close="20000", volume=100),
            _min_bar(hour=15, minute=27, close="21800", volume=100),
            _min_bar(hour=15, minute=28, close="21900", volume=100),
            _min_bar(hour=15, minute=29, close="22000", volume=500),
        ],
    )
    _write(
        tmp_path,
        "NIFTY",
        "1day",
        [
            _day_bar(day=date(2024, 6, 12), open_="19900", close="20000", volume=300),
            _day_bar(day=_DAY, open_="20000", close="22000", volume=800),
        ],
    )


class TestMoversAsofLatestDefault:
    """DEC-MOVERS-ASOF-LATEST-DEFAULT-001 — the default as-of shows the identical
    latest close across every timeframe (the Groww/Dhan-consistent behaviour)."""

    #: Every intraday TF (divisor + non-divisor) + the daily TF — the default
    #: poll must read the SAME latest close on all of them.
    _TFS = ("1minute", "2minute", "3minute", "5minute", "3hour", "1day")

    def test_default_asof_identical_latest_close_across_timeframes(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        # THE core pin (the executable form of "Dixon 1D/5m/3m/2m/1m all C=13237.00"):
        # with NO explicit time, EVERY timeframe shows the identical 15:29 latest close.
        _seed_finalized_day_lake(tmp_path)
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        ltps: set[str] = set()
        for tf in self._TFS:
            body = client.get("/api/v1/movers", params={"asset": "spot", "interval": tf}).json()
            assert body["date"] == "2024-06-13"  # defaulted to the latest available day
            assert body["price_gainers"][0]["symbol"] == "NIFTY"
            ltps.add(body["price_gainers"][0]["ltp"])
        assert ltps == {"₹22,000.00"}, ltps  # ONE value across all six timeframes

    def test_teeth_old_per_tf_default_slot_would_differ(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        # The bug reproduction: the OLD per-TF default pinned 3minute to its last
        # grid slot 15:27 → the 15:27 partial-bucket close 21800, DIFFERENT from
        # the new default's 15:29 latest close 22000. Explicit times still cut honestly.
        _seed_finalized_day_lake(tmp_path)
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        at_1527 = client.get(
            "/api/v1/movers",
            params={"asset": "spot", "interval": "3minute", "date": "2024-06-13", "time": "15:27"},
        ).json()
        at_0915 = client.get(
            "/api/v1/movers",
            params={"asset": "spot", "interval": "3minute", "date": "2024-06-13", "time": "09:15"},
        ).json()
        # Read the LTP from the volume category (always includes the row — at 09:15
        # the pct change is 0% so it is not a gainer/loser).
        assert at_1527["volume"][0]["ltp"] == "₹21,800.00"
        assert at_0915["volume"][0]["ltp"] == "₹20,000.00"
        # The per-TF slots DIFFER from each other AND from the new default's 22000 —
        # exactly the operator-reported divergence the default fix removes.
        assert at_1527["volume"][0]["ltp"] != at_0915["volume"][0]["ltp"]
        default = client.get(
            "/api/v1/movers", params={"asset": "spot", "interval": "3minute"}
        ).json()
        assert default["price_gainers"][0]["ltp"] == "₹22,000.00"

    def test_default_intraday_echoes_latest_time_and_null_time(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        # The additive display echo: default intraday poll → latest_time="15:29"
        # (the real latest minute), time echoes null (whole-day cut, stored-fast).
        _seed_finalized_day_lake(tmp_path)
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        body = client.get("/api/v1/movers", params={"asset": "spot", "interval": "3minute"}).json()
        assert body["latest_time"] == "15:29"
        assert body["time"] is None

    def test_daily_interval_latest_time_is_none(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        # A daily interval has no intraday time → latest_time is None (plain label).
        _seed_finalized_day_lake(tmp_path)
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        body = client.get("/api/v1/movers", params={"asset": "spot", "interval": "1day"}).json()
        assert body["latest_time"] is None

    def test_explicit_time_leaves_latest_time_none(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        # An EXPLICIT as-of is honestly partial — no latest_time echo (the picker
        # already shows the chosen minute); the honest cut semantic is unchanged.
        _seed_finalized_day_lake(tmp_path)
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        body = client.get(
            "/api/v1/movers",
            params={"asset": "spot", "interval": "3minute", "date": "2024-06-13", "time": "15:27"},
        ).json()
        assert body["latest_time"] is None

    def test_latest_time_none_when_no_intraday_bars(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        # Only a 1-day lake (no 1-minute) → the intraday default resolves latest_time
        # to None (empty 1m glob) and still cuts whole-day. Honest "latest" label.
        _write(
            tmp_path,
            "NIFTY",
            "1day",
            [
                _day_bar(day=date(2024, 6, 12), open_="19900", close="20000", volume=300),
                _day_bar(day=_DAY, open_="20000", close="22000", volume=700),
            ],
        )
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        body = client.get("/api/v1/movers", params={"asset": "spot", "interval": "3minute"}).json()
        assert body["latest_time"] is None


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

    def test_movers_page_lists_all_21_timeframes(self, store: InstrumentStore) -> None:
        # DEC-MOVERS-COMPLETENESS-001: the Timeframe dropdown lists EXACTLY the 21 canonical
        # lake interval labels — none missing AND no stray/dead extra option (an exact-set
        # check, so a dead `value=` can never sneak in). The labels mirror LAKE_INTERVAL_LABELS
        # (single source of truth). Top-N defaults to 100 and the input + the route cap allow
        # up to 500 — and the syncControls() handler must NOT clobber that back to 200 (the
        # hostile-review MAJOR: a 500 cap unreachable through the UI is exactly the illusion
        # the operator forbids).
        import re

        from brutex.data.timeframes import LAKE_INTERVAL_LABELS

        client = TestClient(build_app(DashboardState(), instrument_store=store))
        text = client.get("/movers").text
        select = re.search(r'<select id="interval">(.*?)</select>', text, re.DOTALL)
        assert select is not None, "interval <select> not found"
        dropdown_values = set(re.findall(r'<option value="([^"]+)"', select.group(1)))
        assert dropdown_values == set(LAKE_INTERVAL_LABELS.values())
        assert len(dropdown_values) == 21
        # Top-N default 100, reachable up to 500, and NEVER clobbered to 200 by syncControls()
        # (the hostile-review MAJOR — precisely target the JS `.max = "200"` assignment so an
        # unrelated "200" elsewhere can't false-pass/fail this guard).
        assert 'id="topn"' in text and 'value="100"' in text and 'max="500"' in text
        assert '.max = "200"' not in text, (
            "topn max must not be reset to 200 by syncControls (DEC-MOVERS-COMPLETENESS-001)"
        )
        assert '.max = "500"' in text, "syncControls must set the topn max to 500"

    def test_movers_link_in_every_pages_nav(self, store: InstrumentStore) -> None:
        # DEC-INDEX-CONSTITUENTS-COVERAGE-001: the /movers page must be reachable
        # from the top menu of EVERY page — not just by typing the URL. Each page's
        # nav now carries a "Movers" link (alongside Database · Coverage · Reports).
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        # The v29 main dashboard ("/") uses its own live-sweep layout (no standard
        # top nav); the standard-nav pages all carry the Movers link.
        # (The /groww-credentials page was removed — DEC-AWS-ONLY-CREDS-001 — credentials
        # now live ONLY in AWS Parameter Store, so it is no longer a served page.)
        for path in ("/welcome", "/coverage", "/reports", "/db", "/universe"):
            resp = client.get(path)
            assert resp.status_code == 200, path
            assert 'href="/movers"' in resp.text, f"{path} is missing the Movers nav link"


# ── prev-close calendar-contiguity goldens (DEC-MOVERS-PREVCLOSE-CONTIGUITY-001) ──
#
# Both arms of the EMIT-ON-FALSE byte-identity law + the ABSTAIN envelope:
#   * CONTIGUOUS (across a weekend AND across a listed holiday) → the full row
#     JSON is byte-identical to the pre-fix wire (NO additive key, no marker
#     gating flag, no WARN);
#   * NON-CONTIGUOUS (the confirmed-defect repro dates) → the served value is
#     UNCHANGED (still the nearest-prior close — the fix makes the number
#     HONEST, it does not change it) and the row gains exactly the two additive
#     keys + ONE capped WARN;
#   * ABSTAIN (pre-2020 snapshot) → value unchanged, keys omitted, no WARN.

from structlog.testing import capture_logs  # noqa: E402

#: The FROZEN additive-key vocabulary this decision may ever add to a movers
#: row — a new key needs a conscious edit here (the key-NAME set pin).
_CONTIGUITY_KEYS = frozenset(
    {
        "prev_close_session",
        "prev_close_contiguous",
        "oi_baseline_session",
        "oi_baseline_contiguous",
    }
)

#: The pre-fix spot row key set (the byte-identity vocabulary).
_SPOT_ROW_KEYS = frozenset(
    {
        "symbol",
        "name",
        "exchange",
        "segment",
        "ltp",
        "change",
        "pct_change",
        "value_cr",
        "volume",
        "bar_count",
    }
)


class TestPrevCloseContiguityGoldens:
    def test_contiguous_weekend_row_is_byte_identical_no_warn(self, tmp_path: Path) -> None:
        # Fri 2025-09-26 close 24600 → Mon 2025-09-29 close 24650 (the CORRECT
        # geometry of the repro: with Friday present the %change is ~0.2033%).
        _write(
            tmp_path,
            "NIFTY",
            "1day",
            [
                _day_bar(day=date(2025, 9, 26), open_="24600", close="24600", volume=90),
                _day_bar(day=date(2025, 9, 29), open_="24650", close="24650", volume=100),
            ],
        )
        with capture_logs() as logs:
            payload = run_movers_snapshot(
                lake_root=tmp_path, interval="1day", date="2025-09-29", time=None, top_n=5
            )
        (row,) = payload["price_gainers"]
        # FULL-row byte identity: the exact pre-fix wire, computed independently.
        assert row == {
            "symbol": "NIFTY",
            "name": "NIFTY",
            "exchange": "NSE",
            "segment": "INDEX",
            "ltp": "₹24,650.00",
            "change": "₹50.00",
            "pct_change": str(
                (Decimal("24650.00") - Decimal("24600.00")) / Decimal("24600.00") * 100
            ),
            "value_cr": "0.25",
            "volume": 100,
            "bar_count": 1,
        }
        assert set(row) == _SPOT_ROW_KEYS  # the frozen pre-fix key-name set
        assert set(row) & _CONTIGUITY_KEYS == set()  # EMIT-ON-FALSE: nothing added
        # The correct %change is ~0.2033% (the honest sibling of the 2.2822% repro).
        assert row["pct_change"].startswith("0.2032520325")
        assert [e for e in logs if e["event"].startswith("movers.prev_baseline")] == []

    def test_contiguous_across_a_listed_holiday_no_keys_no_warn(self, tmp_path: Path) -> None:
        # Fri 2024-03-22 → Tue 2024-03-26 spans the weekend AND Holi (Mon
        # 2024-03-25, HOLIDAYS_2024): still the immediately-preceding session.
        _write(
            tmp_path,
            "NIFTY",
            "1day",
            [
                _day_bar(day=date(2024, 3, 22), open_="22000", close="22000", volume=90),
                _day_bar(day=date(2024, 3, 26), open_="22100", close="22100", volume=50),
            ],
        )
        with capture_logs() as logs:
            payload = run_movers_snapshot(
                lake_root=tmp_path, interval="1day", date="2024-03-26", time=None, top_n=5
            )
        (row,) = payload["price_gainers"]
        assert row["change"] == "₹100.00"
        assert set(row) & _CONTIGUITY_KEYS == set()
        assert [e for e in logs if e["event"].startswith("movers.prev_baseline")] == []

    def test_noncontiguous_repro_serves_same_value_stamped_plus_one_warn(
        self, tmp_path: Path
    ) -> None:
        """The CONFIRMED-DEFECT repro (verified 2026-07-07 on the pre-fix code):
        snapshot Mon 2025-09-29 with Wed 09-24 (24000) + Thu 09-25 (24100) in
        the lake and the true prior session Fri 09-26 a HOLE — the baseline
        silently read 24100 from TWO sessions back and the %change printed
        2.2822% instead of the correct 0.2033%. The fix SERVES THE SAME VALUE
        (never fabricates a Friday close) but stamps the row + warns ONCE."""
        _write(
            tmp_path,
            "NIFTY",
            "1day",
            [
                _day_bar(day=date(2025, 9, 24), open_="24000", close="24000", volume=80),
                _day_bar(day=date(2025, 9, 25), open_="24100", close="24100", volume=90),
                # Fri 2025-09-26 is DELIBERATELY absent (the lake hole).
                _day_bar(day=date(2025, 9, 29), open_="24650", close="24650", volume=100),
            ],
        )
        with capture_logs() as logs:
            payload = run_movers_snapshot(
                lake_root=tmp_path, interval="1day", date="2025-09-29", time=None, top_n=5
            )
        (row,) = payload["price_gainers"]
        # VALUE-EQUALITY: the served baseline/change are UNCHANGED by the fix.
        assert row["ltp"] == "₹24,650.00"
        assert row["change"] == "₹550.00"  # vs Thu 24100 — the nearest-prior close
        assert row["pct_change"] == str(
            (Decimal("24650.00") - Decimal("24100.00")) / Decimal("24100.00") * 100
        )
        assert row["pct_change"].startswith("2.2821576763")  # ≈ 2.2822% (the repro)
        # The HONESTY stamp: exactly the two additive keys, strictly False.
        assert row["prev_close_session"] == "2025-09-25"
        assert row["prev_close_contiguous"] is False
        assert set(row) - _SPOT_ROW_KEYS == {"prev_close_session", "prev_close_contiguous"}
        # Exactly ONE capped WARN naming both sessions.
        warns = [e for e in logs if e["event"] == "movers.prev_baseline_noncontiguous"]
        assert len(warns) == 1
        assert warns[0]["kind"] == "prev_close"
        assert warns[0]["symbol"] == "NIFTY"
        assert warns[0]["found_session"] == "2025-09-25"
        assert warns[0]["expected_session"] == "2025-09-26"

    def test_abstain_pre2020_snapshot_serves_value_no_keys_no_warn(self, tmp_path: Path) -> None:
        # A pre-coverage snapshot day: the calendar ABSTAINS (contiguity
        # unknown) — the value is served, the additive keys are OMITTED
        # (EMIT-ON-FALSE), and no movers warning fires (one honest
        # calendar.previous_session_coverage_exhausted is allowed).
        # Two writes — the lake writer requires one (year, month) per call.
        _write(
            tmp_path,
            "NIFTY",
            "1day",
            [_day_bar(day=date(2019, 5, 31), open_="100", close="100", volume=10)],
        )
        _write(
            tmp_path,
            "NIFTY",
            "1day",
            [_day_bar(day=date(2019, 6, 3), open_="110", close="110", volume=10)],
        )
        with capture_logs() as logs:
            payload = run_movers_snapshot(
                lake_root=tmp_path, interval="1day", date="2019-06-03", time=None, top_n=5
            )
        (row,) = payload["price_gainers"]
        assert row["change"] == "₹10.00"
        assert row["pct_change"] == str(
            (Decimal("110.00") - Decimal("100.00")) / Decimal("100.00") * 100
        )
        assert set(row) & _CONTIGUITY_KEYS == set()
        assert [e for e in logs if e["event"].startswith("movers.prev_baseline")] == []
