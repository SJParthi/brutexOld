"""``GET /api/v1/universe/lake-uniqueness`` + the ``/universe`` chip (DEC-LAKE-HEAL-001).

The census route rides the ``SummaryCache`` ADAPTIVE-TTL-FLOOR path (the #920
machinery ``/coverage/spot`` uses — the hostile-review MAJOR fold; NOT a
generation-KEYED memo, which a pull's per-write-tick generation bumps would
re-key on every 15 s chip poll into back-to-back whole-lake scans), so:

* a repeat 15 s chip poll is an O(1) HIT (the O(files) scanner runs exactly
  once — spy-pinned),
* a write-tick STORM of ``mark_dirty`` bumps yields at most ONE scan per
  adaptive floor window — ``max(2 s, the scan's own duration)`` capped 120 s
  (the fails-before pin: the old generation-keyed memo re-scanned per poll),
* a dirty entry past the floor recomputes (a pull / the L3 sweeper's
  ``on_healed`` is served fresh within one bounded floor window — never
  indefinitely stale),
* an instrument-master change is NOT a lake-bytes signal → clean HIT.

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


class _FakeClock:
    """A manually-advanced monotonic clock driving the SummaryCache TTL floor."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


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
            "price_files": 0,
            "alien_files": 0,
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

    def test_options_foreign_overlap_real_numbers(self, store: InstrumentStore) -> None:
        """The chip payload carries REAL OPTIONS foreign/overlap counts
        (DEC-LAKE-HEAL-OPTIONS-FOREIGN-001 — no shape change, real values)."""
        import pyarrow as pa
        import pyarrow.parquet as pq

        series = store.path.parent / "options" / "NSE-NIFTY-2025-09"
        series.mkdir(parents=True, exist_ok=True)
        sep_ts = datetime(2025, 9, 1, 4, 0, tzinfo=UTC)
        oct_ts = datetime(2025, 10, 1, 4, 0, tzinfo=UTC)
        pq.write_table(
            pa.table(
                {
                    "side": pa.array(["CE"]),
                    "timestamp": pa.array([sep_ts], type=pa.timestamp("us", tz="UTC")),
                }
            ),
            series / "2025-09.parquet",
        )
        pq.write_table(
            pa.table(
                {
                    "side": pa.array(["CE", "CE"]),
                    "timestamp": pa.array([oct_ts, sep_ts], type=pa.timestamp("us", tz="UTC")),
                }
            ),
            series / "2025-10.parquet",
        )
        client = _make_client(store, SummaryCache())
        payload = client.get("/api/v1/universe/lake-uniqueness").json()
        assert payload["engine_clean"] is True  # options never touch the engine verdict
        assert payload["findings_total"] == 1
        options_row = next(row for row in payload["segments"] if row["segment"] == "OPTIONS")
        assert options_row == {
            "segment": "OPTIONS",
            "files": 2,
            "clean": 1,
            "dup_files": 0,
            "foreign_files": 1,
            "overlap_files": 1,
            "drift_dirs": 0,
            "price_files": 0,
            "alien_files": 0,
        }

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

    def test_repeat_poll_hits_then_dirty_past_floor_rescans_master_is_irrelevant(
        self, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = {"n": 0}
        real = app_module.scan_lake_invariants

        def _counting(*args: Any, **kwargs: Any) -> Any:
            calls["n"] += 1
            return real(*args, **kwargs)

        monkeypatch.setattr(app_module, "scan_lake_invariants", _counting)

        clock = _FakeClock()
        cache = SummaryCache(clock=clock)
        client = _make_client(store, cache)

        first = client.get("/api/v1/universe/lake-uniqueness")
        assert first.status_code == 200
        second = client.get("/api/v1/universe/lake-uniqueness")
        assert second.status_code == 200
        # Repeat poll within one generation → ONE real scan (clean HIT).
        assert calls["n"] == 1
        # Payload is byte-identical to the uncached first build (only memoised).
        assert second.json() == first.json()

        # A pull OR a heal (`on_healed=mark_dirty`) dirties the entry; once the
        # 2 s base floor elapses the next poll runs ONE fresh scan.
        cache.mark_dirty()
        clock.advance(3.0)
        assert client.get("/api/v1/universe/lake-uniqueness").status_code == 200
        assert calls["n"] == 2

        # An instrument-master change is NOT a lake-bytes signal — the census
        # scans lake files, never the instrument table — so it stays a clean
        # HIT (no rescan), even well past the floor.
        _bump_master(store)
        clock.advance(3.0)
        assert client.get("/api/v1/universe/lake-uniqueness").status_code == 200
        assert calls["n"] == 2

    def test_pull_write_storm_cannot_amplify_the_poll_into_scan_storms(
        self, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """THE hostile-review MAJOR pin (the #920 regression class) — fails before.

        During a pull, ``pull_jobs`` bumps the write generation on EVERY
        per-pair write tick, so the PRE-FIX generation-KEYED
        ``MoversSnapshotCache`` memo re-keyed on every bump and re-ran the
        whole-lake O(files) scan on every 15 s chip poll — on the OLD wiring
        this test fails with ``calls == 5``. Through the ``SummaryCache``
        adaptive TTL floor, an expensive scan (30 s on the fake clock) sets
        its OWN refresh floor: five dirty polls inside that window pay ONE
        scan, and a poll past the window re-scans (a floor, never a freeze).
        """
        calls = {"n": 0}
        clock = _FakeClock()
        real = app_module.scan_lake_invariants

        def _slow_scan(*args: Any, **kwargs: Any) -> Any:
            calls["n"] += 1
            clock.advance(30.0)  # the fake slow scanner: each scan "takes" 30 s
            return real(*args, **kwargs)

        monkeypatch.setattr(app_module, "scan_lake_invariants", _slow_scan)
        cache = SummaryCache(clock=clock)
        client = _make_client(store, cache)

        for _ in range(5):  # five chip polls, each preceded by a pull write tick
            cache.mark_dirty()  # pull_jobs' per-pair generation bump
            assert client.get("/api/v1/universe/lake-uniqueness").status_code == 200
            clock.advance(1.0)
        # ONE scan within the adaptive floor window — max(2 s, its own 30 s).
        assert calls["n"] == 1

        # Past the floor the dirty entry refreshes — bounded staleness, not a freeze.
        clock.advance(30.0)
        cache.mark_dirty()
        assert client.get("/api/v1/universe/lake-uniqueness").status_code == 200
        assert calls["n"] == 2


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
        the post-heal census is served within ONE bounded adaptive-floor
        window (``max(2 s, scan duration)`` — the honest freshness contract of
        the SummaryCache TTL-floor wiring; never indefinitely stale).
        """
        monkeypatch.delenv("BRUTEX_LAKE_HEAL", raising=False)
        from brutex.data.lake_heal import StartupSweeper

        lake_root = store.path.parent
        _poison_engine_partition(lake_root)
        clock = _FakeClock()
        cache = SummaryCache(clock=clock)
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

        # …so the first poll past the adaptive floor (2 s base here — the fake
        # clock never ticked during the scan) recomputes → the HEALED census.
        clock.advance(3.0)
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
