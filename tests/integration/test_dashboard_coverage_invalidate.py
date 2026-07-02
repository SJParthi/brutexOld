"""Regression: a single-symbol pull invalidates the coverage SummaryCache.

DEC-DASHBOARD-COVERAGE-INVALIDATE-001. PR-A (DEC-DASHBOARD-FASTREAD-001) added an
O(1) :class:`~brutex.dashboard.summary_cache.SummaryCache` for the ``/coverage``
spot+option polls, invalidated on every write tick by the BACKGROUND pull job
(``BackgroundPullJob._on_progress`` → ``mark_dirty``). But the SYNCHRONOUS
single-symbol pull route (``POST /api/v1/universe/pull`` → ``pull_candles``)
bypasses that job, so it never marked the cache dirty: after pulling one symbol
(e.g. just SENSEX) ``/coverage`` kept serving the stale cold-start snapshot
("0 series · 0 bars") even though the bars were in the lake (``/db`` could read
them — it bypasses the cache). The fix marks the cache dirty after the pull; this
oracle proves the next coverage poll reflects the just-written bars.

The cache is injected with ``ttl_floor_s=0.0`` so the post-pull re-read is
DETERMINISTIC — the production 2 s TTL floor would otherwise throttle the
immediate re-read (in real use the 4 s page poll clears it).
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("duckdb")
pytest.importorskip("fastapi")

from brutex.dashboard.app import DashboardState, build_app
from brutex.dashboard.summary_cache import SummaryCache
from brutex.data.instrument_store import InstrumentStore
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient


def _nifty() -> Instrument:
    """One admitted index instrument the pull route can resolve by symbol."""
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=13,
        symbol_name="NIFTY",
        display_name="NIFTY",
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


def _write_spot_bar(lake_root: Path) -> None:
    """Write one real 1-minute NIFTY spot bar straight into the lake."""
    ParquetWriter(lake_root=lake_root).write(
        bars=[
            CandleBar(
                timestamp=datetime(2024, 1, 2, 3, 45, tzinfo=UTC),
                open=Decimal("100.00"),
                high=Decimal("101.00"),
                low=Decimal("99.00"),
                close=Decimal("100.50"),
                volume=10,
                open_interest=0,
            )
        ],
        exchange="NSE",
        segment="CASH",
        symbol="NIFTY",
        interval="1minute",
    )


@pytest.fixture
def store(tmp_path: Path) -> Generator[InstrumentStore]:
    """An InstrumentStore on a tmp lake holding only the NIFTY underlying (no bars)."""
    lake_root = tmp_path / "lake"
    lake_root.mkdir()
    s = InstrumentStore.open(lake_root / "instruments.duckdb")
    s.upsert_many(
        [_nifty()],
        snapshot_date=date(2024, 1, 2),
        run_id="r1",
        csv_source_sha256="0" * 64,
    )
    yield s
    s.close()


def _spot_headline(client: TestClient) -> dict[str, int]:
    """GET /api/v1/coverage/spot and return its headline {datasets, total_bars}."""
    r = client.get("/api/v1/coverage/spot")
    assert r.status_code == 200
    return r.json()["headline"]


def test_single_symbol_pull_invalidates_coverage_cache(store: InstrumentStore) -> None:
    """A one-off /universe/pull must refresh /coverage/spot (the stale-empty bug)."""
    lake_root = Path(store.path).parent
    # ttl_floor_s=0.0 → no throttle, so the post-pull re-read is deterministic.
    cache = SummaryCache(ttl_floor_s=0.0)
    app = build_app(DashboardState(), instrument_store=store, summary_cache=cache)
    with TestClient(app) as client:
        # 1) Cold start: empty lake → coverage caches an EMPTY snapshot (generation 0).
        assert _spot_headline(client)["datasets"] == 0

        # 2) A pull writes spot bars into the lake (written directly here so the
        #    test needs no live Dhan; the bytes are what a real pull would land).
        _write_spot_bar(lake_root)

        # 3) The single-symbol pull route runs as an EXPLICIT dry-run
        #    (DEC-PULL-LOUD-CREDENTIAL-REFUSAL-001 — a creds-absent pull now
        #    REFUSES instead of silently degrading, so the offline test opts in;
        #    this must never touch AWS / the network). WITHOUT the fix it never
        #    marks the cache dirty, so step 4 would still read the stale empty
        #    snapshot (a clean cache hit at generation 0).
        resp = client.post("/api/v1/universe/pull", params={"symbol": "NIFTY", "dry_run": "true"})
        assert resp.status_code == 200

        # 4) The next coverage poll now REFLECTS the just-written dataset.
        headline = _spot_headline(client)
        assert headline["datasets"] == 1
        assert headline["total_bars"] == 1
