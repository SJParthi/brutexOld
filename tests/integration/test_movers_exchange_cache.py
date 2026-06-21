"""Integration tests for the ``/movers`` exchange scope + snapshot cache (DEC-MOVERS-O1-LATENCY-001).

Two new behaviours of the redesigned ``/movers`` page, both against a REAL
Parquet + DuckDB lake:

1. **Exchange scope** — ``?exchange=NSE|BSE`` ranks ONLY that exchange's
   instruments (an exact per-exchange top-N), wired through
   :func:`~brutex.dashboard.movers_snapshot.compute_spot_candidates` /
   ``compute_futures_candidates`` / ``compute_options_candidates`` discovery and
   the route's ``exch`` normalisation. Any other value ranks every exchange.
2. **Snapshot cache** — the route caches the ``top_n``-independent candidate set
   per coordinate behind the injected
   :class:`~brutex.dashboard.movers_cache.MoversSnapshotCache`, invalidated by the
   SAME write-generation as the coverage :class:`~brutex.dashboard.summary_cache.SummaryCache`:
   a fresh request for an already-viewed coordinate is served from cache (so newly
   written bars are NOT seen until a ``mark_dirty``), and a ``mark_dirty`` makes the
   next request recompute (so a pull is never served stale).

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
from brutex.dashboard.movers_futures import compute_futures_candidates
from brutex.dashboard.movers_options import compute_options_candidates
from brutex.dashboard.movers_snapshot import _read_session_bars, compute_spot_candidates
from brutex.dashboard.summary_cache import SummaryCache
from brutex.data.instrument_store import InstrumentStore
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient

_DAY = date(2024, 6, 13)


# ── seeding helpers (exchange is a parameter — multi-exchange lakes) ───────


def _min_bar(
    *, hour: int, minute: int, close: str, volume: int, oi: int | None = None
) -> CandleBar:
    """A flat 1-minute bar at ``HH:MM`` **IST** on ``_DAY`` (IST = UTC + 05:30)."""
    c = Decimal(close)
    total = hour * 60 + minute - (5 * 60 + 30)
    return CandleBar(
        timestamp=datetime(_DAY.year, _DAY.month, _DAY.day, total // 60, total % 60, tzinfo=UTC),
        open=c,
        high=c,
        low=c,
        close=c,
        volume=volume,
        open_interest=oi,
    )


def _day_bar(*, close: str, volume: int, day: date = _DAY) -> CandleBar:
    """A 1-day OHLCV bar labelled at 09:15 IST (03:45 UTC) on *day*."""
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
    tmp_path: Path,
    *,
    exchange: str,
    segment: str,
    symbol: str,
    interval: str,
    bars: list[CandleBar],
) -> None:
    ParquetWriter(lake_root=tmp_path).write(
        exchange=exchange, segment=segment, symbol=symbol, interval=interval, bars=bars
    )


def _idx(symbol: str) -> Instrument:
    """A minimal index instrument row (the store only needs to exist for the route)."""
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


def _seed_two_exchange_spot(tmp_path: Path) -> None:
    """NIFTY under NSE/INDEX + SENSEX under BSE/INDEX — one 1-day bar each on ``_DAY``."""
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
        exchange="BSE",
        segment="INDEX",
        symbol="SENSEX",
        interval="1day",
        bars=[_day_bar(close="77000", volume=300)],
    )


# ── spot exchange scope (compute core + route) ────────────────────────────


class TestSpotExchangeScope:
    def test_compute_filters_to_one_exchange(self, tmp_path: Path) -> None:
        _seed_two_exchange_spot(tmp_path)
        nse = compute_spot_candidates(
            lake_root=tmp_path, interval="1day", date="2024-06-13", time=None, exchange="NSE"
        )
        bse = compute_spot_candidates(
            lake_root=tmp_path, interval="1day", date="2024-06-13", time=None, exchange="BSE"
        )
        both = compute_spot_candidates(
            lake_root=tmp_path, interval="1day", date="2024-06-13", time=None, exchange=None
        )
        assert {s.instrument.symbol for s in nse.snapshots} == {"NIFTY"}
        assert {s.instrument.symbol for s in bse.snapshots} == {"SENSEX"}
        assert {s.instrument.symbol for s in both.snapshots} == {"NIFTY", "SENSEX"}

    def test_corrupt_partition_is_skipped_not_500(self, tmp_path: Path) -> None:
        """A single corrupt Parquet for ONE symbol is SKIPPED (logged + counted), never
        500-ing the whole spot screen (DEC-MOVERS-CORRUPT-SKIP-001).

        Seeds a good NIFTY + a SENSEX whose on-disk Parquet is then overwritten with
        garbage so the REAL ``pl.read_parquet`` raises a ``ComputeError``; the screen
        still ranks NIFTY, reports SENSEX skipped, and logs ``movers.parquet_skip``.
        """
        from structlog.testing import capture_logs

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
            exchange="BSE",
            segment="INDEX",
            symbol="SENSEX",
            interval="1day",
            bars=[_day_bar(close="77000", volume=300)],
        )
        corrupt = list((tmp_path / "bars" / "BSE" / "INDEX" / "SENSEX" / "1day").rglob("*.parquet"))
        assert corrupt, "expected the SENSEX 1day partition to exist"
        for path in corrupt:
            path.write_bytes(b"not a parquet file \r\n garbage")

        with capture_logs() as logs:
            result = compute_spot_candidates(
                lake_root=tmp_path, interval="1day", date="2024-06-13", time=None, exchange=None
            )
        # NIFTY still ranks; the unreadable SENSEX is skipped — the screen did NOT 500.
        assert {s.instrument.symbol for s in result.snapshots} == {"NIFTY"}
        assert result.skipped == 1
        skips = [e for e in logs if e["event"] == "movers.parquet_skip"]
        assert len(skips) == 1
        assert skips[0]["source"] == "spot" and skips[0]["symbol"] == "SENSEX"

    def test_route_exchange_param_filters(self, store: InstrumentStore, tmp_path: Path) -> None:
        _seed_two_exchange_spot(tmp_path)
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        params = {"asset": "spot", "interval": "1day", "date": "2024-06-13", "top_n": 5}
        nse = client.get("/api/v1/movers", params={**params, "exchange": "NSE"}).json()
        bse = client.get("/api/v1/movers", params={**params, "exchange": "BSE"}).json()
        allx = client.get("/api/v1/movers", params={**params, "exchange": "ALL"}).json()
        assert {r["symbol"] for r in nse["volume"]} == {"NIFTY"}
        assert {r["symbol"] for r in bse["volume"]} == {"SENSEX"}
        # An unknown exchange (``ALL``) ranks EVERY exchange (the all-population).
        assert {r["symbol"] for r in allx["volume"]} == {"NIFTY", "SENSEX"}


# ── snapshot cache: hit serves cached, mark_dirty invalidates ──────────────


class TestMoversRouteCache:
    def test_cache_hit_then_generation_invalidation(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        # Seed only NIFTY first.
        _write(
            tmp_path,
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            interval="1day",
            bars=[_day_bar(close="22000", volume=500)],
        )
        summary_cache = SummaryCache(ttl_floor_s=0.0)
        movers_cache = MoversSnapshotCache()
        client = TestClient(
            build_app(
                DashboardState(),
                instrument_store=store,
                summary_cache=summary_cache,
                movers_cache=movers_cache,
            )
        )
        params = {"asset": "spot", "interval": "1day", "date": "2024-06-13", "top_n": 5}

        first = client.get("/api/v1/movers", params=params).json()
        assert first["instrument_count"] == 1
        assert movers_cache.size() == 1  # candidate set cached for this coordinate.

        # Write a SECOND symbol but do NOT signal a write — the cache must still
        # serve the stale (1-instrument) candidate set (proves it is cached).
        _write(
            tmp_path,
            exchange="NSE",
            segment="INDEX",
            symbol="BANKNIFTY",
            interval="1day",
            bars=[_day_bar(close="48000", volume=900)],
        )
        cached = client.get("/api/v1/movers", params=params).json()
        assert cached["instrument_count"] == 1  # served from cache, BANKNIFTY unseen
        assert movers_cache.size() == 1

        # A write-generation bump (what a pull/materialize does) invalidates it →
        # the next request recomputes and now sees BANKNIFTY.
        summary_cache.mark_dirty()
        fresh = client.get("/api/v1/movers", params=params).json()
        assert fresh["instrument_count"] == 2
        assert {r["symbol"] for r in fresh["volume"]} == {"NIFTY", "BANKNIFTY"}


# ── futures / options exchange scope (discovery continue branch) ──────────


class TestDerivativeExchangeScope:
    def test_futures_compute_filters_exchange(self, tmp_path: Path) -> None:
        # An NSE near-month future (with a session bar) + a BSE future dataset.
        _write(
            tmp_path,
            exchange="NSE",
            segment="FNO",
            symbol="NSE-NIFTY-27Jun24-FUT",
            interval="1minute",
            bars=[_min_bar(hour=9, minute=15, close="22010", volume=100, oi=1000)],
        )
        _write(
            tmp_path,
            exchange="BSE",
            segment="FNO",
            symbol="BSE-SENSEX-27Jun24-FUT",
            interval="1minute",
            bars=[_min_bar(hour=9, minute=15, close="77010", volume=50, oi=500)],
        )
        nse = compute_futures_candidates(
            lake_root=tmp_path, interval="1day", date="2024-06-13", time=None, exchange="NSE"
        )
        assert {m.underlying for m in nse.movers} == {"NIFTY"}  # BSE future filtered out

    def test_options_compute_filters_exchange(self, tmp_path: Path) -> None:
        # An NSE near-expiry CE (with a session bar) + a BSE option dataset.
        _write(
            tmp_path,
            exchange="NSE",
            segment="FNO",
            symbol="NSE-NIFTY-27Jun24-24000-CE",
            interval="1minute",
            bars=[_min_bar(hour=9, minute=15, close="120", volume=100, oi=1000)],
        )
        _write(
            tmp_path,
            exchange="BSE",
            segment="FNO",
            symbol="BSE-SENSEX-27Jun24-80000-CE",
            interval="1minute",
            bars=[_min_bar(hour=9, minute=15, close="200", volume=50, oi=500)],
        )
        nse = compute_options_candidates(
            lake_root=tmp_path,
            interval="1day",
            date="2024-06-13",
            time=None,
            expiry_tier="near",
            expiry_track="all",
            exchange="NSE",
        )
        assert {m.underlying for m in nse.movers} == {"NIFTY"}  # BSE option filtered out

    def test_futures_corrupt_partition_is_skipped_not_500(self, tmp_path: Path) -> None:
        """A corrupt future Parquet is SKIPPED (logged + counted), never 500-ing the
        futures screen (DEC-MOVERS-CORRUPT-SKIP-001)."""
        from structlog.testing import capture_logs

        _write(
            tmp_path,
            exchange="NSE",
            segment="FNO",
            symbol="NSE-NIFTY-27Jun24-FUT",
            interval="1minute",
            bars=[_min_bar(hour=9, minute=15, close="22010", volume=100, oi=1000)],
        )
        _write(
            tmp_path,
            exchange="NSE",
            segment="FNO",
            symbol="NSE-BANKNIFTY-27Jun24-FUT",
            interval="1minute",
            bars=[_min_bar(hour=9, minute=15, close="48010", volume=80, oi=900)],
        )
        corrupt = list(
            (tmp_path / "bars" / "NSE" / "FNO" / "NSE-BANKNIFTY-27Jun24-FUT" / "1minute").rglob(
                "*.parquet"
            )
        )
        assert corrupt, "expected the BANKNIFTY future 1minute partition to exist"
        for path in corrupt:
            path.write_bytes(b"not a parquet file \r\n garbage")

        with capture_logs() as logs:
            result = compute_futures_candidates(
                lake_root=tmp_path, interval="1day", date="2024-06-13", time=None, exchange="NSE"
            )
        assert {m.underlying for m in result.movers} == {"NIFTY"}
        assert result.skipped == 1
        skips = [e for e in logs if e["event"] == "movers.parquet_skip"]
        assert len(skips) == 1
        assert skips[0]["source"] == "futures"
        assert skips[0]["symbol"] == "NSE-BANKNIFTY-27Jun24-FUT"

    def test_options_corrupt_partition_is_skipped_not_500(self, tmp_path: Path) -> None:
        """A corrupt option Parquet is SKIPPED (logged + counted), never 500-ing the
        options screen (DEC-MOVERS-CORRUPT-SKIP-001)."""
        from structlog.testing import capture_logs

        _write(
            tmp_path,
            exchange="NSE",
            segment="FNO",
            symbol="NSE-NIFTY-27Jun24-24000-CE",
            interval="1minute",
            bars=[_min_bar(hour=9, minute=15, close="120", volume=100, oi=1000)],
        )
        _write(
            tmp_path,
            exchange="NSE",
            segment="FNO",
            symbol="NSE-NIFTY-27Jun24-25000-CE",
            interval="1minute",
            bars=[_min_bar(hour=9, minute=15, close="60", volume=80, oi=700)],
        )
        corrupt = list(
            (tmp_path / "bars" / "NSE" / "FNO" / "NSE-NIFTY-27Jun24-25000-CE" / "1minute").rglob(
                "*.parquet"
            )
        )
        assert corrupt, "expected the 25000-CE 1minute partition to exist"
        for path in corrupt:
            path.write_bytes(b"not a parquet file \r\n garbage")

        with capture_logs() as logs:
            result = compute_options_candidates(
                lake_root=tmp_path,
                interval="1day",
                date="2024-06-13",
                time=None,
                expiry_tier="near",
                expiry_track="all",
                exchange="NSE",
            )
        # The good 24000-CE ranks; the corrupt 25000-CE is skipped — the screen did NOT 500.
        assert len(result.movers) == 1
        assert result.skipped == 1
        skips = [e for e in logs if e["event"] == "movers.parquet_skip"]
        assert len(skips) == 1
        assert skips[0]["source"] == "options"
        assert skips[0]["symbol"] == "NSE-NIFTY-27Jun24-25000-CE"


# ── session-exempt (GIFT Nifty) month-boundary regression ──────────────────


class TestSessionExemptMonthBoundary:
    """A session-exempt spot symbol's bars that cross a UTC month boundary are NOT dropped.

    GIFT Nifty (~21h) can hold an IST-``2026-07-01`` bar stored in the PRIOR UTC
    month's ``2026/06.parquet`` (an IST ``02:00`` bar is UTC ``2026-06-30 20:30``).
    The month-bounded read would drop it; the whole-partition fallback for
    session-exempt symbols keeps it — the regression the hostile-review found.
    """

    def test_intraday_read_keeps_cross_month_bars(self, tmp_path: Path) -> None:
        def _bar(ts: datetime, close: str, volume: int) -> CandleBar:
            c = Decimal(close)
            return CandleBar(
                timestamp=ts, open=c, high=c, low=c, close=c, volume=volume, open_interest=None
            )

        # IST 2026-07-01 02:00 → UTC 2026-06-30 20:30 → stored in 2026/06.parquet.
        # IST 2026-07-01 10:00 → UTC 2026-07-01 04:30 → stored in 2026/07.parquet.
        # The lake writer requires one (year, month) per call, so write each month
        # separately (exactly how the ingest lands cross-month days).
        for ts, close, vol in (
            (datetime(2026, 6, 30, 20, 30, tzinfo=UTC), "100", 10),
            (datetime(2026, 7, 1, 4, 30, tzinfo=UTC), "110", 20),
        ):
            _write(
                tmp_path,
                exchange="NSE",
                segment="INDEX",
                symbol="GIFTNIFTY",
                interval="1minute",
                bars=[_bar(ts, close, vol)],
            )
        bars = _read_session_bars(
            tmp_path,
            exchange="NSE",
            segment="INDEX",
            symbol="GIFTNIFTY",
            day=date(2026, 7, 1),
            minutes=1,
            cutoff_min=None,
        )
        # BOTH bars (the prior-UTC-month 02:00 and the same-month 10:00) are kept.
        assert len(bars) == 2
        assert sum(b.volume for b in bars) == 30
