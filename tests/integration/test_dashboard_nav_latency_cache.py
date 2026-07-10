"""Integration tests for the /coverage + /universe nav-latency caches.

DEC-DASHBOARD-NAV-LATENCY-001 — three previously-uncached pollers are now memoised
behind a COMPOSITE token = the dashboard write-generation (``_summary_cache.generation``,
bumped by every pull / materialize) AND the instrument-master content revision
(:func:`~brutex.dashboard.universe_cache.master_revision`, moved by a keyless
``load-instruments`` that does NOT bump the generation):

* ``GET /api/v1/coverage`` (the 4 s headline+grid poll),
* ``GET /api/v1/universe/completeness`` (the 15 s per-symbol map),
* ``GET /api/v1/universe/mapping-integrity`` (the 15 s spot↔options verdict; the CSV
  download shares the SAME cached report).

Each test SPIES on the route's real builder (``build_coverage_payload`` /
``per_symbol_completeness`` / ``reconcile_mapping_integrity``) and proves:

* a repeat poll within one generation + master revision is an O(1) HIT (the builder
  runs exactly once),
* a write-generation bump (``mark_dirty``, a pull) MISSES → exactly one fresh build,
* an instrument-master change (a fresh ``upsert_many`` advancing the revision token)
  MISSES → exactly one fresh build (the load-instruments freshness hole the composite
  key closes),
* the cached payload is BYTE-IDENTICAL to the uncached one (only memoised, never
  reshaped), and the JSON ↔ CSV mapping-integrity routes share one report.

Skipped when ``fastapi`` / ``duckdb`` / ``polars`` / ``pyarrow`` are absent.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("duckdb")
pytest.importorskip("polars")
pytest.importorskip("pyarrow")

import brutex.dashboard.app as app_module
from brutex.dashboard.summary_cache import SummaryCache
from brutex.data.coverage_ledger import CoverageRecord, classify_cell
from brutex.data.instrument_store import InstrumentStore
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient


def _idx(symbol: str) -> Instrument:
    """One admitted index instrument."""
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=13,
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


def _eq(symbol: str, *, security_id: int) -> Instrument:
    """An NSE cash-equity row — F&O-eligible by construction in the lean store."""
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=security_id,
        symbol_name=symbol,
        display_name=symbol,
        instrument=InstrumentType.EQ,
        segment=Segment.CASH,
        series="EQ",
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


def _cov(underlying: str, year: int, *, expiries: int = 2) -> CoverageRecord:
    return CoverageRecord(
        exch_id="NSE",
        underlying_symbol=underlying,
        year=year,
        expiries_pulled=expiries,
        contracts_pulled=expiries * 10,
        status=classify_cell(expiries, expiries * 10, None),
        last_run_id="run-1",
        last_fetched_at=datetime(2026, 5, 30, 9, 15, tzinfo=UTC),
    )


def _write_spot_bar(lake_root: Path, symbol: str) -> None:
    """Write one real 1-minute spot bar straight into the lake (so completeness scores it)."""
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
        symbol=symbol,
        interval="1minute",
    )


@pytest.fixture
def store(tmp_path: Path) -> Generator[InstrumentStore]:
    """An InstrumentStore on a tmp lake: one index + one F&O-eligible equity, both covered."""
    lake_root = tmp_path / "lake"
    lake_root.mkdir()
    s = InstrumentStore.open(lake_root / "instruments.duckdb")
    s.upsert_many(
        [_idx("NIFTY"), _eq("RELIANCE", security_id=2885)],
        snapshot_date=date(2024, 1, 2),
        run_id="r1",
        csv_source_sha256="0" * 64,
    )
    for year in app_module._coverage_years():
        s.upsert_coverage(_cov("RELIANCE", year))
    _write_spot_bar(lake_root, "NIFTY")
    yield s
    s.close()


def _bump_master(store: InstrumentStore) -> None:
    """Advance the instrument-master content revision WITHOUT bumping the generation.

    Mirrors a keyless ``load-instruments``: a fresh ``upsert_many`` stamps a new
    ``fetched_at``, so :func:`master_revision` moves while ``_summary_cache.generation``
    stays put.
    """
    store.upsert_many(
        [_idx("NIFTY"), _eq("RELIANCE", security_id=2885)],
        snapshot_date=date(2024, 1, 3),
        run_id="r2",
        csv_source_sha256="1" * 64,
    )


def _bump_coverage(store: InstrumentStore) -> None:
    """Write ONE coverage cell WITHOUT bumping the generation or the master.

    Mirrors a metadata (expiries/contracts) pull — in this process or ANOTHER one
    sharing the same ``instruments.duckdb``: ``upsert_coverage`` writes the
    ``coverage_ledger`` table only, so :func:`coverage_revision` moves while BOTH
    ``_summary_cache.generation`` AND :func:`master_revision` stay put
    (DEC-STALENESS-CACHE-TRUTH-001 — the freshness hole the 4th key component closes).
    """
    store.upsert_coverage(
        CoverageRecord(
            exch_id="NSE",
            underlying_symbol="RELIANCE",
            year=2019,  # a year OUTSIDE _coverage_years() — a brand-new cell
            expiries_pulled=1,
            contracts_pulled=5,
            status=classify_cell(1, 5, None),
            last_run_id="run-cov-bump",
            last_fetched_at=datetime(2026, 6, 1, 10, 0, tzinfo=UTC),
        )
    )


def _make_client(store: InstrumentStore, cache: SummaryCache) -> TestClient:
    return TestClient(
        app_module.build_app(
            app_module.DashboardState(), instrument_store=store, summary_cache=cache
        )
    )


# ── /api/v1/coverage ───────────────────────────────────────────────────────


class TestCoverageCache:
    def test_repeat_poll_is_a_hit_then_generation_and_master_each_miss(
        self, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = {"n": 0}
        real = app_module.build_coverage_payload

        def _counting(*args: object, **kwargs: object) -> object:
            calls["n"] += 1
            return real(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(app_module, "build_coverage_payload", _counting)

        cache = SummaryCache()
        client = _make_client(store, cache)

        first = client.get("/api/v1/coverage")
        assert first.status_code == 200
        second = client.get("/api/v1/coverage")
        assert second.status_code == 200
        # Repeat poll within one generation + master revision → ONE real build.
        assert calls["n"] == 1
        # Payload is byte-identical to the uncached first build (only memoised).
        assert second.json() == first.json()

        # A pull bumps the generation → the next poll MISSES → one fresh build.
        cache.mark_dirty()
        assert client.get("/api/v1/coverage").status_code == 200
        assert calls["n"] == 2

        # An instrument-master change (no generation bump) MISSES → one fresh build.
        _bump_master(store)
        assert client.get("/api/v1/coverage").status_code == 200
        assert calls["n"] == 3

        # A coverage-ledger write (no generation bump, master untouched — a
        # metadata pull, possibly from ANOTHER process) MISSES → one fresh build,
        # and the new cell is actually SERVED (DEC-STALENESS-CACHE-TRUTH-001;
        # fails before: the stale pre-write grid was re-served from cache).
        _bump_coverage(store)
        fresh = client.get("/api/v1/coverage")
        assert fresh.status_code == 200
        assert calls["n"] == 4
        # An unchanged store yields a byte-identical key → repeat poll is a HIT
        # (stable token — the 4th component never thrashes the cache).
        assert client.get("/api/v1/coverage").status_code == 200
        assert calls["n"] == 4


# ── /api/v1/universe/completeness ───────────────────────────────────────────


class TestUniverseCompletenessCache:
    def test_repeat_poll_is_a_hit_then_generation_and_master_each_miss(
        self, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = {"n": 0}
        real = app_module.per_symbol_completeness

        def _counting(*args: object, **kwargs: object) -> object:
            calls["n"] += 1
            return real(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(app_module, "per_symbol_completeness", _counting)

        # ttl_floor_s=0.0 so the inner SPOT SummaryCache never throttles the
        # cross-request re-read — the per-symbol cache decision is then the ONLY
        # gate under test (the production 2 s floor is cleared by the 15 s poll).
        cache = SummaryCache(ttl_floor_s=0.0)
        client = _make_client(store, cache)

        first = client.get("/api/v1/universe/completeness")
        assert first.status_code == 200
        second = client.get("/api/v1/universe/completeness")
        assert second.status_code == 200
        assert calls["n"] == 1
        assert second.json() == first.json()

        cache.mark_dirty()
        assert client.get("/api/v1/universe/completeness").status_code == 200
        assert calls["n"] == 2

        _bump_master(store)
        assert client.get("/api/v1/universe/completeness").status_code == 200
        assert calls["n"] == 3

        # A coverage-ledger write alone (no generation/master move) MISSES →
        # one fresh build (DEC-STALENESS-CACHE-TRUTH-001) — then a repeat poll
        # on the unchanged store is a HIT again (stable token, no thrash).
        _bump_coverage(store)
        assert client.get("/api/v1/universe/completeness").status_code == 200
        assert calls["n"] == 4
        assert client.get("/api/v1/universe/completeness").status_code == 200
        assert calls["n"] == 4


# ── /api/v1/universe/mapping-integrity (+ shared CSV) ───────────────────────


class TestMappingIntegrityCache:
    def test_repeat_poll_is_a_hit_then_generation_and_master_each_miss(
        self, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = {"n": 0}
        real = app_module.reconcile_mapping_integrity

        def _counting(*args: object, **kwargs: object) -> object:
            calls["n"] += 1
            return real(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(app_module, "reconcile_mapping_integrity", _counting)

        cache = SummaryCache()
        client = _make_client(store, cache)

        first = client.get("/api/v1/universe/mapping-integrity")
        assert first.status_code == 200
        second = client.get("/api/v1/universe/mapping-integrity")
        assert second.status_code == 200
        assert calls["n"] == 1
        assert second.json() == first.json()

        cache.mark_dirty()
        assert client.get("/api/v1/universe/mapping-integrity").status_code == 200
        assert calls["n"] == 2

        _bump_master(store)
        assert client.get("/api/v1/universe/mapping-integrity").status_code == 200
        assert calls["n"] == 3

        # A coverage-ledger write alone (the mapping-integrity report READS
        # store.list_coverage(), so a stale serve here was a WRONG verdict)
        # MISSES → one fresh reconcile (DEC-STALENESS-CACHE-TRUTH-001) — then a
        # repeat poll on the unchanged store is a HIT again (no thrash).
        _bump_coverage(store)
        assert client.get("/api/v1/universe/mapping-integrity").status_code == 200
        assert calls["n"] == 4
        assert client.get("/api/v1/universe/mapping-integrity").status_code == 200
        assert calls["n"] == 4

    def test_json_and_csv_share_one_cached_report(
        self, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The JSON poll warms the cache; the CSV download REUSES it (no second reconcile)."""
        calls = {"n": 0}
        real = app_module.reconcile_mapping_integrity

        def _counting(*args: object, **kwargs: object) -> object:
            calls["n"] += 1
            return real(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(app_module, "reconcile_mapping_integrity", _counting)

        client = _make_client(store, SummaryCache())
        assert client.get("/api/v1/universe/mapping-integrity").status_code == 200
        csv_resp = client.get("/api/v1/universe/mapping-integrity.csv")
        assert csv_resp.status_code == 200
        # The CSV reuses the JSON poll's cached report — only ONE reconciliation total.
        assert calls["n"] == 1
        assert csv_resp.headers["content-type"].startswith("text/csv")
