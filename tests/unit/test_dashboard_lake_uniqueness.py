"""``GET /api/v1/universe/lake-uniqueness`` + the ``/universe`` chip (DEC-LAKE-HEAL-001).

Since DEC-LAKE-CENSUS-NONBLOCKING-001 the census route rides the
``LakeCensusHolder`` (a dedicated daemon scan thread + an O(1) last-known
verdict snapshot — the superseded ``SummaryCache`` cold single-flight parked
every stacked 15 s poller in an unbounded wait ON a threadpool worker and
starved the shared AnyIO limiter for the whole production scan). The
FRESHNESS law is byte-compatible with the pre-fix wiring:

* a repeat 15 s chip poll is an O(1) snapshot (the O(files) scanner runs
  exactly once — spy-pinned),
* a write-tick STORM of ``mark_dirty`` bumps yields at most ONE scan per
  adaptive floor window — ``max(2 s, the scan's own duration)`` capped 120 s
  (the #920 law, preserved verbatim by the holder),
* a dirty holder past the floor rescans (a pull / the L3 sweeper's
  ``on_healed`` is served fresh within one bounded floor window — never
  indefinitely stale),
* an instrument-master change is NOT a lake-bytes signal → no rescan.

Plus the payload shape (clean + engine-poisoned — the pre-fix verdict keys
byte-compatible, the four additive scan-state keys stamped on top), the honest
``{"present": false}`` no-lake-root gate, the 503 no-store contract, and the
``universe.html`` chip markers (textContent-only JS — the CodeQL XSS rule)
including the new honest warming arm.

Lakes are REAL Parquet trees under ``tmp_path``; poison is planted with raw
pyarrow (the fixed writer refuses duplicates). Tests inject a SYNCHRONOUS
``census_spawn`` (build_app's holder test seam) so the daemon-thread scan runs
inline + deterministic under coverage.
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


def _sync_spawn(body: Callable[[], None]) -> None:
    """Run the census check/scan inline — the deterministic holder test seam."""
    body()


def _make_client(
    store: InstrumentStore | None,
    cache: SummaryCache,
    *,
    census_clock: Callable[[], float] | None = None,
) -> TestClient:
    return TestClient(
        app_module.build_app(
            app_module.DashboardState(),
            instrument_store=store,
            summary_cache=cache,
            census_spawn=_sync_spawn,
            census_clock=census_clock,
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
        }
        assert payload["healed_total"] == 0
        assert payload["last_sweep_utc"] is None
        # DEC-LAKE-CENSUS-NONBLOCKING-001: the ADDITIVE scan-state keys
        # (every verdict key above is byte-compatible with the pre-fix payload).
        assert payload["scan_in_progress"] is False
        assert payload["scan_stalled"] is False
        assert payload["refresh_probe_wedged"] is False
        assert payload["refresh_probe_wedged_s"] is None
        assert payload["scan_error"] is None
        assert isinstance(payload["as_of_utc"], str)
        progress = payload["scan_progress"]
        assert progress["files_scanned"] == 1  # one bar file → one progress tick
        assert progress["files_total_estimate"] is None  # first-ever — no L3 sentinel
        assert isinstance(progress["started_utc"], str)

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
    """The O(1) repeat-poll + adaptive-floor contract (DEC-LAKE-HEAL-001 law,
    served by the DEC-LAKE-CENSUS-NONBLOCKING-001 holder)."""

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
        client = _make_client(store, cache, census_clock=clock)

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
        per-pair write tick, so the PRE-#1074 generation-KEYED
        ``MoversSnapshotCache`` memo re-keyed on every bump and re-ran the
        whole-lake O(files) scan on every 15 s chip poll — on that wiring
        this test fails with ``calls == 5``. The ``LakeCensusHolder`` keeps
        the SummaryCache-era adaptive TTL floor verbatim
        (DEC-LAKE-CENSUS-NONBLOCKING-001): an expensive scan (30 s on the
        fake clock) sets its OWN refresh floor — five dirty polls inside that
        window pay ONE scan, and a poll past the window re-scans (a floor,
        never a freeze).
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
        client = _make_client(store, cache, census_clock=clock)

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
        client = _make_client(store, cache, census_clock=clock)

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
        # The L3 sweep sentinel now exists → the census progress denominator is
        # its honest ``files_scanned`` (DEC-LAKE-CENSUS-NONBLOCKING-001's
        # estimate seam; the pre-sweep polls read the first-ever ``None``).
        estimate = after["scan_progress"]["files_total_estimate"]
        assert isinstance(estimate, int) and estimate >= 1


class TestCensusEstimateSeam:
    """The L3-sentinel progress-denominator honesty arms (route estimate seam)."""

    @pytest.mark.parametrize("junk", [True, -5, "12"])
    def test_foreign_shaped_sentinel_reads_null_estimate_never_a_crash(
        self, store: InstrumentStore, junk: object
    ) -> None:
        """A junk ``files_scanned`` (bool / negative / non-int) degrades to null."""
        import json as _json

        lake_root = store.path.parent
        sentinel = lake_root / ".lake_heal" / "last_sweep.json"
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text(
            _json.dumps({"files_scanned": junk, "duration_ms": junk}), encoding="utf-8"
        )
        client = _make_client(store, SummaryCache())
        payload = client.get("/api/v1/universe/lake-uniqueness").json()
        assert payload["findings_total"] == 0  # the scan itself still landed
        assert payload["scan_progress"]["files_total_estimate"] is None

    @pytest.mark.parametrize("junk_ms", [float("inf"), float("-inf"), float("nan")])
    def test_non_finite_sentinel_duration_falls_to_the_files_arm(
        self, store: InstrumentStore, junk_ms: float
    ) -> None:
        """Round-1 refuter (route arm): ``Infinity`` IS valid input to
        Python's tolerant JSON decoder and passed the ``> 0`` gate — the
        hint would have seeded an INFINITE stall bound. The ``math.isfinite``
        guard rejects it and the hint falls to the ``files_scanned``
        derivation; the route serves the verdict + the honest estimate
        normally. (A FINITE huge duration is a DIFFERENT case — accepted
        here and clamped by the holder, round-3 law; see the sibling test.)"""
        import json as _json

        lake_root = store.path.parent
        sentinel = lake_root / ".lake_heal" / "last_sweep.json"
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text(
            _json.dumps({"files_scanned": 3, "duration_ms": junk_ms}), encoding="utf-8"
        )
        client = _make_client(store, SummaryCache())
        payload = client.get("/api/v1/universe/lake-uniqueness").json()
        assert payload["findings_total"] == 0
        assert payload["scan_progress"]["files_total_estimate"] == 3
        assert payload["scan_stalled"] is False
        assert payload["refresh_probe_wedged"] is False

    def test_huge_finite_sentinel_duration_is_accepted_and_holder_clamps(
        self, store: InstrumentStore
    ) -> None:
        """Round-3 refuter (route arm): a FINITE positive ``duration_ms`` of
        ANY magnitude (here the round-2 corrupt-sentinel repro ``1e308`` ms)
        is ACCEPTED by the hint helper — never rejected to the files arm /
        floor — and BOUNDED by the holder's single 24 h clamp site (the
        bound teeth live in the holder suite; behaviourally the route
        serves the verdict + estimate normally)."""
        import json as _json

        lake_root = store.path.parent
        sentinel = lake_root / ".lake_heal" / "last_sweep.json"
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text(
            _json.dumps({"files_scanned": 3, "duration_ms": 1e308}), encoding="utf-8"
        )
        client = _make_client(store, SummaryCache())
        payload = client.get("/api/v1/universe/lake-uniqueness").json()
        assert payload["findings_total"] == 0
        assert payload["scan_progress"]["files_total_estimate"] == 3
        assert payload["scan_stalled"] is False

    def test_absurd_magnitude_files_scanned_hint_degrades_never_a_crash(
        self, store: InstrumentStore
    ) -> None:
        """A ``files_scanned`` int too large for float conversion raises
        ``OverflowError`` in the per-file hint derivation — mapped to ``None``
        (bare-floor bound); the estimate itself (a plain int) still serves."""
        import json as _json

        lake_root = store.path.parent
        sentinel = lake_root / ".lake_heal" / "last_sweep.json"
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text(_json.dumps({"files_scanned": 10**400}), encoding="utf-8")
        client = _make_client(store, SummaryCache())
        payload = client.get("/api/v1/universe/lake-uniqueness").json()
        assert payload["findings_total"] == 0
        assert payload["scan_progress"]["files_total_estimate"] == 10**400
        assert payload["scan_stalled"] is False

    @pytest.mark.parametrize("huge_ms", [10**309, 10**400])
    def test_huge_int_sentinel_duration_saturates_never_fails_the_hint(
        self, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, huge_ms: int
    ) -> None:
        """Round-4 refuter: a JSON *int* ``duration_ms`` beyond float range
        made ``math.isfinite(duration_ms)`` raise ``OverflowError`` — the
        whole hint callable failed (a repeating
        ``lake_census.first_scan_bound_hint_failed`` WARN each check cycle)
        and the bound collapsed to the bare floor: the round-3
        never-a-floor-collapse doctrine violated through the INT shape. The
        local conversion guard now SATURATES a positive-huge int to a huge
        finite float, riding the same finite-positive → holder-clamp path as
        its 1e308 float twin (the clamp teeth live in the holder suite)."""
        import json as _json

        import brutex.dashboard.lake_census_holder as holder_module

        class _Rec:
            def __init__(self) -> None:
                self.events: list[str] = []

            def warning(self, event: str, **kw: object) -> None:
                self.events.append(event)

        rec = _Rec()
        monkeypatch.setattr(holder_module, "_LOG", rec)
        lake_root = store.path.parent
        sentinel = lake_root / ".lake_heal" / "last_sweep.json"
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text(
            _json.dumps({"files_scanned": 3, "duration_ms": huge_ms}), encoding="utf-8"
        )
        client = _make_client(store, SummaryCache())
        payload = client.get("/api/v1/universe/lake-uniqueness").json()
        assert payload["findings_total"] == 0
        assert payload["scan_progress"]["files_total_estimate"] == 3
        assert payload["scan_stalled"] is False
        # Pre-fix teeth: math.isfinite(10**309) RAISED — the hint callable
        # failed with exactly this WARN; the guarded conversion never does.
        assert "lake_census.first_scan_bound_hint_failed" not in rec.events

    def test_negative_huge_int_duration_falls_to_the_files_arm(
        self, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The negative-huge INT twin is unusable (like any non-positive
        value) — it falls through to the ``files_scanned`` arm without ever
        failing the hint callable."""
        import json as _json

        import brutex.dashboard.lake_census_holder as holder_module

        class _Rec:
            def __init__(self) -> None:
                self.events: list[str] = []

            def warning(self, event: str, **kw: object) -> None:
                self.events.append(event)

        rec = _Rec()
        monkeypatch.setattr(holder_module, "_LOG", rec)
        lake_root = store.path.parent
        sentinel = lake_root / ".lake_heal" / "last_sweep.json"
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text(
            _json.dumps({"files_scanned": 3, "duration_ms": -(10**400)}), encoding="utf-8"
        )
        client = _make_client(store, SummaryCache())
        payload = client.get("/api/v1/universe/lake-uniqueness").json()
        assert payload["findings_total"] == 0
        assert payload["scan_progress"]["files_total_estimate"] == 3
        assert "lake_census.first_scan_bound_hint_failed" not in rec.events

    def test_sentinel_duration_ms_arm_serves_normally(self, store: InstrumentStore) -> None:
        """A sentinel carrying a recorded duration exercises the duration-first
        bound-hint arm — the route serves the verdict identically."""
        import json as _json

        lake_root = store.path.parent
        sentinel = lake_root / ".lake_heal" / "last_sweep.json"
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text(
            _json.dumps({"files_scanned": 3, "duration_ms": 1560000}), encoding="utf-8"
        )
        client = _make_client(store, SummaryCache())
        payload = client.get("/api/v1/universe/lake-uniqueness").json()
        assert payload["findings_total"] == 0
        assert payload["scan_progress"]["files_total_estimate"] == 3
        assert payload["scan_stalled"] is False


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
        # DEC-LAKE-CENSUS-NONBLOCKING-001: the honest warming/rescan arms —
        # no-verdict-yet renders live progress instead of a fabricated verdict,
        # a rescan alongside a verdict is called out, a failed scan is visible,
        # and the 15 s cadence is unchanged (no unguarded faster poll — the
        # single setInterval pin above stays the ONLY census poll).
        assert "census scanning…" in body
        assert "(rescanning…" in body
        assert "census scan failed" in body
        assert "census scan stalled" in body  # the ADD-3 loud-staleness arm
        # The refresh-probe-wedge note (round-1 refuter FINDING 1) renders
        # beside the served verdict — distinct from a stalled scan.
        assert "refresh probe wedged" in body
        assert "refresh_probe_wedged" in body
        assert "refresh_probe_wedged_s" in body
        assert "scan_in_progress" in body
        assert "scan_stalled" in body
        assert text.count("setInterval(loadLakeUniqueness") == 1


class TestExtremePermutations:
    """ADD-4 permutation arms at the ROUTE level (holder arms live in the
    holder suite; single-flight/crash/estimate arms are pinned there)."""

    def test_empty_lake_zero_files_serves_a_zero_verdict(self, tmp_path: Path) -> None:
        """A 0-file lake lands an honest zero census — never a crash/blank."""
        lake_root = tmp_path / "lake"
        lake_root.mkdir()
        s = InstrumentStore.open(lake_root / "instruments.duckdb")
        try:
            client = _make_client(s, SummaryCache())
            payload = client.get("/api/v1/universe/lake-uniqueness").json()
            assert payload["present"] is True
            assert payload["findings_total"] == 0
            assert payload["files_scanned"] == 0
            assert payload["scan_progress"]["files_scanned"] == 0
        finally:
            s.close()

    def test_corrupt_file_mid_scan_rides_the_scanner_skip_semantics(
        self, store: InstrumentStore
    ) -> None:
        """A garbage Parquet is SKIPPED (the scanner's existing
        `parquet_read_skip_errors` discipline) — the census still lands."""
        lake_root = store.path.parent
        junk = lake_root / "bars" / "NSE" / "INDEX" / "NIFTY" / "1minute" / "2025" / "10.parquet"
        junk.parent.mkdir(parents=True, exist_ok=True)
        junk.write_bytes(b"this is not a parquet file")
        client = _make_client(store, SummaryCache())
        payload = client.get("/api/v1/universe/lake-uniqueness").json()
        assert payload["present"] is True
        assert payload["files_scanned"] == 1  # the clean bar file; junk skipped
        assert payload["findings_total"] == 0
        # Progress ticked for the skipped file too (per_file_pause fires on
        # skips — the scanner's documented seam contract).
        assert payload["scan_progress"]["files_scanned"] == 2

    def test_store_path_vanishing_mid_flight_hits_the_daemon_thread_belts(
        self, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The route gates ``path`` per REQUEST, but the estimate / bound-hint /
        scan-builder reads happen LATER on the daemon thread — a store whose
        path degrades between arming and the scan rides each helper's own
        not-a-Path belt (null estimate, bare-floor hint, the builder's honest
        ``{"present": false}``) — never a crash, never a fabricated verdict."""
        real_path = store.path
        flip = {"gone": False}
        monkeypatch.setattr(
            InstrumentStore,
            "path",
            property(lambda self: "gone-mid-flight" if flip["gone"] else real_path),
        )
        bodies: list[Callable[[], None]] = []
        client = TestClient(
            app_module.build_app(
                app_module.DashboardState(),
                instrument_store=store,
                summary_cache=SummaryCache(),
                census_spawn=bodies.append,
            ),
            raise_server_exceptions=False,
        )
        first = client.get("/api/v1/universe/lake-uniqueness").json()
        assert first["scan_in_progress"] is True  # armed; the body is queued
        assert "findings_total" not in first
        flip["gone"] = True
        bodies.pop()()  # the daemon body runs AFTER the path vanished
        flip["gone"] = False
        snap = client.get("/api/v1/universe/lake-uniqueness").json()
        assert snap["present"] is False  # the builder's own honest gate landed
        assert snap["scan_progress"]["files_total_estimate"] is None
        assert snap["scan_error"] is None
        assert snap["scan_stalled"] is False

    def test_scan_crash_route_keeps_serving_the_last_verdict(
        self, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A raising rescan (disk vanished) never blanks the route: the
        last-known verdict serves with `scan_error` stamped, 200 always."""
        clock = _FakeClock()
        cache = SummaryCache(clock=clock)
        client = _make_client(store, cache, census_clock=clock)
        first = client.get("/api/v1/universe/lake-uniqueness").json()
        assert first["findings_total"] == 0 and first["scan_error"] is None

        def _vanished(*args: Any, **kwargs: Any) -> Any:
            raise OSError("[Errno 5] Input/output error: lake volume vanished")

        monkeypatch.setattr(app_module, "scan_lake_invariants", _vanished)
        cache.mark_dirty()
        clock.advance(3.0)
        resp = client.get("/api/v1/universe/lake-uniqueness")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["findings_total"] == 0  # last-known verdict still serves
        # SECURITY (the store_error_payload stance): the served scan_error is
        # the STABLE sanitised token + class name — the raw OSError text (which
        # carried path-like detail) never reaches the route/DOM.
        assert payload["scan_error"] == (
            "census scan failed (OSError) — see server log (lake_census.scan_failed)"
        )
        assert "vanished" not in payload["scan_error"]
        assert payload["scan_stalled"] is False
        assert payload["refresh_probe_wedged"] is False
