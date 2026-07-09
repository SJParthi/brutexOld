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


# ── fold-in C (DEC-MOVERS-STORED-FRESHNESS-001): 1m-aware default date ─────


def _minute_bar(*, day: date, hh: int, mm: int, close: str, volume: int = 100) -> CandleBar:
    """A 1-minute bar stamped at *hh:mm* IST (stored UTC = IST − 5:30) on *day*."""
    c = Decimal(close)
    total = hh * 60 + mm - (5 * 60 + 30)
    return CandleBar(
        timestamp=datetime(day.year, day.month, day.day, total // 60, total % 60, tzinfo=UTC),
        open=c,
        high=c,
        low=c,
        close=c,
        volume=volume,
        open_interest=None,
    )


def _seed_mid_pull_lake(tmp_path: Path) -> None:
    """The r2j_seam2 lake shape: 1day through D−1, 1minute through D.

    Mid-fetch the day-D 1-minute bars have landed but the ``1day`` partition
    (rewritten only by the pull-END prebuild) still stops at D−1.
    """
    ParquetWriter(lake_root=tmp_path).write(
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY",
        interval="1day",
        bars=[_day_bar(close="100")],  # the D−1 (2024-06-13) stored daily bar
    )
    ParquetWriter(lake_root=tmp_path).write(
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY",
        interval="1minute",
        bars=[
            _minute_bar(day=date(2024, 6, 14), hh=9, mm=15, close="102"),
            _minute_bar(day=date(2024, 6, 14), hh=9, mm=16, close="105"),
        ],
    )


class TestDefaultDateIsOneMinuteAware:
    def test_default_poll_advances_to_the_fresh_1m_day_with_daily_rows(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        """The combined default-date repro (C6): with 1day through D−1 and 1m
        through D, the DEFAULT (no-date) poll resolves date == D (previously
        pinned to D−1 for hours mid-fetch) AND the 1day tab still shows the
        symbol — the missing-bar coherence arm derives the day-D daily bar
        from the real 1m instead of silently losing it."""
        _seed_mid_pull_lake(tmp_path)
        client = _client(store, SummaryCache(ttl_floor_s=0.0))
        resp = client.get(
            "/api/v1/movers", params={"asset": "spot", "interval": "1day", "top_n": 5}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["date"] == "2024-06-14"  # the fresh 1m day, not the 1day max
        (row,) = body["volume"]
        assert row["symbol"] == "NIFTY"
        assert row["ltp"] == "₹105.00"  # the derived day-D close off the real 1m
        assert body["resampled_reads"] == 1  # honest provenance: derived, not stored

    def test_warm_shaped_key_is_hit_by_the_default_poll(
        self, store: InstrumentStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The no-poison pin (C6): a `_warm_for_date`-shaped warm (the SAME pure
        `latest_movers_date` + `candidate_key` at the SAME generation the route
        reads) pre-computes the coordinate ONCE; the DEFAULT poll then hits the
        warmed key — compute-spy count stays 1, so warm key and route key
        cannot diverge within one generation even when the date resolves off
        the 1m arm."""
        from brutex.dashboard.movers_cache import candidate_key
        from brutex.dashboard.movers_snapshot import DEFAULT_INTERVAL
        from brutex.dashboard.movers_warm import latest_movers_date

        _seed_mid_pull_lake(tmp_path)
        calls = {"n": 0}
        real_compute = app_module.compute_spot_candidates

        def _counting(**kwargs: object) -> object:
            calls["n"] += 1
            return real_compute(**kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(app_module, "compute_spot_candidates", _counting)
        summary_cache = SummaryCache(ttl_floor_s=0.0)
        movers_cache = MoversSnapshotCache()
        client = TestClient(
            app_module.build_app(
                app_module.DashboardState(),
                instrument_store=store,
                summary_cache=summary_cache,
                movers_cache=movers_cache,
            )
        )
        # The _warm_for_date shape: the SAME pure resolver + key the warm uses.
        warm_date = latest_movers_date(tmp_path)
        assert warm_date == "2024-06-14"
        generation = summary_cache.generation
        movers_cache.get_or_compute(
            candidate_key(
                asset="spot",
                exchange=None,
                interval=DEFAULT_INTERVAL,
                date=warm_date,
                time=None,
            ),
            generation=generation,
            compute=lambda: app_module.compute_spot_candidates(
                lake_root=tmp_path,
                interval=DEFAULT_INTERVAL,
                date=warm_date,
                time=None,
                exchange=None,
            ),
        )
        assert calls["n"] == 1  # the warm paid the one compute
        resp = client.get(
            "/api/v1/movers", params={"asset": "spot", "interval": "1day", "top_n": 5}
        )
        assert resp.status_code == 200
        assert resp.json()["date"] == warm_date
        assert calls["n"] == 1  # the default poll HIT the warmed key — no recompute
