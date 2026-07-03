"""``GET /api/v1/universe/lake-uniqueness`` + the ``/universe`` chip (DEC-LAKE-HEAL-001).

The census route is memoised behind the COMPOSITE (write-generation,
master-revision) key — the mapping-integrity #870 cache pattern — so:

* a repeat 15 s chip poll within one token is an O(1) HIT (the O(files)
  scanner runs exactly once — spy-pinned),
* a generation bump (``mark_dirty`` — a pull, OR the L3 sweeper's
  ``on_healed``) MISSES → one fresh scan,
* an instrument-master change (revision token move, NO generation bump)
  MISSES → one fresh scan.

Plus the payload shape (clean + engine-poisoned), the honest
``{"present": false}`` no-lake-root gate, the 503 no-store contract, and the
``universe.html`` chip markers (textContent-only JS — the CodeQL XSS rule).

Lakes are REAL Parquet trees under ``tmp_path``; poison is planted with raw
pyarrow (the fixed writer refuses duplicates).
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

from collections.abc import Callable, Generator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("duckdb")
pytest.importorskip("polars")
pytest.importorskip("pyarrow")

import brutex.dashboard.app as app_module
from brutex.dashboard.summary_cache import SummaryCache
from brutex.data.instrument_store import InstrumentStore
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient


def _idx(symbol: str) -> Instrument:
    """One admitted index instrument row."""
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


def _write_engine_bar(lake_root: Path) -> None:
    """One clean 1-minute NIFTY INDEX bar through the production writer."""
    ParquetWriter(lake_root=lake_root).write(
        bars=[
            CandleBar(
                timestamp=datetime(2025, 9, 1, 4, 0, tzinfo=UTC),
                open=Decimal("100.00"),
                high=Decimal("101.00"),
                low=Decimal("99.00"),
                close=Decimal("100.50"),
                volume=10,
                open_interest=0,
            )
        ],
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY",
        interval="1minute",
    )


def _poison_engine_partition(lake_root: Path) -> None:
    """Plant a duplicate-timestamp NIFTY month file with raw pyarrow."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    dest = lake_root / "bars" / "NSE" / "INDEX" / "NIFTY" / "1minute" / "2025" / "10.parquet"
    dest.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime(2025, 10, 1, 4, 0, tzinfo=UTC)
    prices = [100.0, 100.0]
    table = pa.table(
        {
            "timestamp": pa.array([ts, ts], type=pa.timestamp("us", tz="UTC")),
            "open": pa.array(prices, type=pa.float64()),
            "high": pa.array(prices, type=pa.float64()),
            "low": pa.array(prices, type=pa.float64()),
            "close": pa.array(prices, type=pa.float64()),
            "volume": pa.array([10, 10], type=pa.int64()),
            "open_interest": pa.array([None, None], type=pa.int64()),
        }
    )
    pq.write_table(table, dest)


@pytest.fixture
def store(tmp_path: Path) -> Generator[InstrumentStore]:
    """An InstrumentStore on a tmp lake carrying one clean engine-index partition."""
    lake_root = tmp_path / "lake"
    lake_root.mkdir()
    s = InstrumentStore.open(lake_root / "instruments.duckdb")
    s.upsert_many(
        [_idx("NIFTY")],
        snapshot_date=date(2025, 9, 1),
        run_id="r1",
        csv_source_sha256="0" * 64,
    )
    _write_engine_bar(lake_root)
    yield s
    s.close()


def _bump_master(store: InstrumentStore) -> None:
    """Advance the master revision token WITHOUT bumping the write generation."""
    store.upsert_many(
        [_idx("NIFTY")],
        snapshot_date=date(2025, 9, 2),
        run_id="r2",
        csv_source_sha256="1" * 64,
    )


def _make_client(store: InstrumentStore | None, cache: SummaryCache) -> TestClient:
    return TestClient(
        app_module.build_app(
            app_module.DashboardState(), instrument_store=store, summary_cache=cache
        ),
        raise_server_exceptions=False,
    )


class TestLakeUniquenessRoute:
    """Payload shape + the honest gates."""

    def test_clean_lake_payload_shape(
        self, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("BRUTEX_LAKE_HEAL", raising=False)
        client = _make_client(store, SummaryCache())
        resp = client.get("/api/v1/universe/lake-uniqueness")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["present"] is True
        assert payload["mode"] == "auto"
        assert payload["engine_clean"] is True
        assert payload["findings_total"] == 0
        assert payload["files_scanned"] == 1
        assert "0 findings over 1 files" in payload["engine_detail"]
        segments = {row["segment"]: row for row in payload["segments"]}
        assert set(segments) == {"INDEX", "CASH", "FNO", "OPTIONS"}
        assert segments["INDEX"] == {
            "segment": "INDEX",
            "files": 1,
            "clean": 1,
            "dup_files": 0,
            "foreign_files": 0,
            "overlap_files": 0,
            "drift_dirs": 0,
        }
        assert payload["healed_total"] == 0
        assert payload["last_sweep_utc"] is None

    def test_engine_poisoned_verdict(self, store: InstrumentStore) -> None:
        """A poisoned NIFTY partition flips ``engine_clean`` — the chip's headline answer."""
        _poison_engine_partition(store.path.parent)
        client = _make_client(store, SummaryCache())
        payload = client.get("/api/v1/universe/lake-uniqueness").json()
        assert payload["present"] is True
        assert payload["engine_clean"] is False
        assert payload["findings_total"] == 1
        assert "dup bars/NSE/INDEX/NIFTY/1minute/2025/10.parquet" in payload["engine_detail"]

    def test_present_false_when_lake_root_unresolved(
        self, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A store without a resolvable lake path degrades honestly (the chip stays hidden)."""

        def _not_a_path(self: object) -> str:
            return "not-a-path"

        monkeypatch.setattr(InstrumentStore, "path", property(_not_a_path))
        client = _make_client(store, SummaryCache())
        assert client.get("/api/v1/universe/lake-uniqueness").json() == {"present": False}

    def test_503_when_store_absent(self) -> None:
        client = _make_client(None, SummaryCache())
        assert client.get("/api/v1/universe/lake-uniqueness").status_code == 503


class TestLakeUniquenessCache:
    """The generation-stamped O(1) repeat-poll contract (DEC-LAKE-HEAL-001)."""

    def test_repeat_poll_is_a_hit_then_generation_and_master_each_miss(
        self, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = {"n": 0}
        real = app_module.scan_lake_invariants

        def _counting(*args: Any, **kwargs: Any) -> Any:
            calls["n"] += 1
            return real(*args, **kwargs)

        monkeypatch.setattr(app_module, "scan_lake_invariants", _counting)

        cache = SummaryCache()
        client = _make_client(store, cache)

        first = client.get("/api/v1/universe/lake-uniqueness")
        assert first.status_code == 200
        second = client.get("/api/v1/universe/lake-uniqueness")
        assert second.status_code == 200
        # Repeat poll within one generation + master revision → ONE real scan.
        assert calls["n"] == 1
        # Payload is byte-identical to the uncached first build (only memoised).
        assert second.json() == first.json()

        # A pull OR a heal (`on_healed=mark_dirty`) bumps the generation → fresh scan.
        cache.mark_dirty()
        assert client.get("/api/v1/universe/lake-uniqueness").status_code == 200
        assert calls["n"] == 2

        # An instrument-master change (no generation bump) also MISSES → fresh scan.
        _bump_master(store)
        assert client.get("/api/v1/universe/lake-uniqueness").status_code == 200
        assert calls["n"] == 3


class TestStartupSweepChipFreshness:
    """The L3 sweeper → chip freshness contract (the load-bearing ``on_healed`` seam)."""

    def test_sweep_heals_and_bumps_the_route_generation(
        self, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A startup sweep over a poisoned lake heals it AND invalidates the cached census.

        Wired EXACTLY like ``cli._start_lake_heal_sweep`` wires it —
        ``on_healed=summary_cache.mark_dirty`` + ``catalog_path`` — but through
        the injectable ``spawn`` seam so the sweep body runs synchronously under
        coverage (no real thread). Proves the generation-bump seam end-to-end:
        the pre-sweep poisoned census is NOT served stale after the heal.
        """
        monkeypatch.delenv("BRUTEX_LAKE_HEAL", raising=False)
        from brutex.data.lake_heal import StartupSweeper

        lake_root = store.path.parent
        _poison_engine_partition(lake_root)
        cache = SummaryCache()
        client = _make_client(store, cache)

        before = client.get("/api/v1/universe/lake-uniqueness").json()
        assert before["engine_clean"] is False

        ran: list[bool] = []

        def _sync_spawn(body: Callable[[], None]) -> None:
            ran.append(True)
            body()

        gen_before = cache.generation
        StartupSweeper(
            lake_root,
            on_healed=cache.mark_dirty,
            catalog_path=lake_root / "catalog.duckdb",
            spawn=_sync_spawn,
        ).start()
        assert ran == [True]
        # The heal bumped the SAME generation every dashboard cache keys on…
        assert cache.generation > gen_before

        # …so the very next poll recomputes and serves the HEALED census.
        after = client.get("/api/v1/universe/lake-uniqueness").json()
        assert after["engine_clean"] is True
        assert after["findings_total"] == 0
        assert after["healed_total"] >= 1
        assert after["last_sweep_utc"] is not None


class TestUniverseChipMarkers:
    """The ``/universe`` page carries the chip + its poll wiring (marker-string pin)."""

    def test_chip_markers_present_and_js_is_textcontent_only(self, store: InstrumentStore) -> None:
        client = _make_client(store, SummaryCache())
        resp = client.get("/universe")
        assert resp.status_code == 200
        text = resp.text
        assert 'id="lakepanel"' in text
        assert 'id="lakeverdict"' in text
        assert 'id="lakedetail"' in text
        assert "/api/v1/universe/lake-uniqueness" in text
        assert "setInterval(loadLakeUniqueness, 15000)" in text
        # The new JS builds the chip via textContent ONLY (CodeQL XSS rule,
        # universe.html "never innerHTML" guard) — pinned on the function body.
        start = text.index("async function loadLakeUniqueness")
        end = text.index("function refreshCov", start)
        body = text[start:end]
        assert "textContent" in body
        assert "innerHTML" not in body
        assert "engine indexes POISONED" in body
