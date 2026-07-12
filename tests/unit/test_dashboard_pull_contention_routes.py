"""Route-level pins for DEC-DASHBOARD-PULL-CONTENTION-RESILIENCE-001 (G2 + G4).

G2 — the /db SPOT-cascade build prunes the F&O contract tree AT WALK DESCENT
(``exclude_segments={FNO}``): the walk-descent spy (the per-symbol-dir
liveness beat) proves the FNO contract dirs are never even iterated; the
served payload is BYTE-identical to the unpruned+filtered result; and the
manifest-rebuild division of duty holds (the FILTERED spot walk never rebuilds
the dataset manifest — the unfiltered datasets route keeps that duty).

G4 (widened) — the /universe pull-progress ``phases`` fold and the
/coverage/windows counts+page serve LAST-KNOWN instantly past the adaptive
floor mid-stamp-churn with a PARKED fold stub (the operator's live repro: the
windows page stuck at "loading…" + the /universe feed dropping ~154 s), on the
PRODUCTION cache shape (real daemon revalidator). FAILS-BEFORE: both routes
blocked for the whole fold.

Tables fold (coordinator-directed) — ``GET /api/v1/db/tables`` joins the
serve-last-known family: the previously-UNCACHED ``table_summary`` COUNT(*)
sweep is memoised behind a ``master_revision``-folded generation in an
SWR-flagged cache (the operator's live "request timed out after 12000ms" on
the tables list). Pins: instant last-known serve past-floor mid-pull with a
PARKED sweep stub; fresh-compute BYTE-identity vs the pre-cache route shape;
cache-key honesty (token move → recompute, no move → hit); the probe-fault
degrade to today's direct uncached sweep.

Skipped cleanly without the ``[ui]`` extra (the sibling dashboard suites).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Generator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")
pytest.importorskip("httpx", reason="TestClient needs httpx")

from brutex.dashboard import app as app_module
from brutex.dashboard import db_view
from brutex.dashboard.app import DashboardState, build_app
from brutex.dashboard.summary_cache import SummaryCache
from brutex.data import window_ledger_store as wls
from brutex.data.fetch_coverage import WindowKey
from brutex.data.instrument_store import InstrumentStore
from brutex.data.window_ledger import WindowLedger
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

_NOW = datetime(2026, 7, 11, 12, 0, tzinfo=UTC)

#: The parked-fold hold (upper bound; released early on pass).
_FOLD_HOLD_S: float = 6.0

#: Wall-clock budget for a serve-last-known response while the fold is parked.
#: A non-blocking serve answers in single-digit ms; a blocking one waits the
#: whole hold (4x over budget) — unambiguous separation.
_SERVE_BUDGET_S: float = 1.5

#: The #920 base floor (2 s) plus slack — real-time aging for the route-level
#: tests (the app-internal caches use the production monotonic clock).
_FLOOR_AGE_S: float = 2.2


def _touch_parquet(root: Path, *parts: str) -> None:
    """Create ``<root>/<parts...>/2024/01.parquet`` (the walk checks names only)."""
    d = root.joinpath(*parts, "2024")
    d.mkdir(parents=True, exist_ok=True)
    (d / "01.parquet").touch()


def _seed_lake(lake: Path, *, fno_contracts: int = 5) -> None:
    """Two spot symbol dirs + *fno_contracts* F&O contract dirs."""
    bars = lake / "bars"
    _touch_parquet(bars, "NSE", "INDEX", "NIFTY", "1minute")
    _touch_parquet(bars, "NSE", "CASH", "RELIANCE", "1minute")
    for i in range(fno_contracts):
        _touch_parquet(bars, "NSE", "FNO", f"NSE-NIFTY-30Jan25-2{i}000-CE", "1minute")


def _window_key(security_id: str) -> WindowKey:
    return WindowKey(
        market="groww",
        security_id=security_id,
        interval="1minute",
        start=date(2026, 1, 1),
        end=date(2026, 4, 1),
    )


def _seed_ledger(lake: Path, symbol: str) -> None:
    ledger = WindowLedger()
    ledger.record(_window_key(symbol), raw_bars=5, error_type=None, bars_appended=5, now=_NOW)
    wls.save_window_ledger(ledger, lake)


def _append_delta(lake: Path, symbol: str) -> None:
    live = wls.load_window_ledger(lake)
    live.record(_window_key(symbol), raw_bars=7, error_type=None, bars_appended=7, now=_NOW)
    assert wls.append_ledger_delta(live, lake) == 1


@pytest.fixture
def lake(tmp_path: Path) -> Path:
    root = tmp_path / "lake"
    root.mkdir()
    return root


@pytest.fixture
def store(lake: Path) -> Generator[InstrumentStore]:
    s = InstrumentStore.open(lake / "instruments.duckdb")
    yield s
    s.close()


@pytest.fixture
def client(store: InstrumentStore) -> Generator[TestClient]:
    app = build_app(DashboardState(), instrument_store=store)
    with TestClient(app) as c:
        yield c


class TestSpotCascadeFnoPrune:
    """G2 — the walk never descends the F&O contract tree (teeth + parity)."""

    def test_spot_cascade_walk_never_descends_fno(
        self, lake: Path, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """THE walk-descent spy: `_walk_candle_datasets` fires ONE liveness
        beat per walked SYMBOL dir, so the beat count IS the descent census.
        Pruned: 2 (the spot symbols). FAILS-BEFORE (unpruned): 7 — the walk
        iterated every F&O contract dir too (the 29.7-min class at the
        production 2.36M-file scale)."""
        _seed_lake(lake, fno_contracts=5)
        beats = {"n": 0}

        def _count_beat() -> None:
            beats["n"] += 1

        monkeypatch.setattr(db_view, "emit_progress_beat", _count_beat)
        resp = client.get("/api/v1/db/spot/symbols")
        assert resp.status_code == 200
        symbols = [s["symbol"] for s in resp.json()["symbols"]]
        assert symbols == ["NIFTY", "RELIANCE"]  # indices-first, then CASH alpha
        assert beats["n"] == 2, (
            f"the spot-cascade walk descended {beats['n']} symbol dirs — "
            "the F&O contract tree must be pruned at descent (exclude_segments)"
        )

    def test_pruned_walk_equals_unpruned_filtered(self, lake: Path) -> None:
        """Field-equality (dataclass `__eq__`): the pruned walk's spot rows ==
        the unpruned walk's rows through `filter_spot_datasets` (the belt) —
        walk path (no manifest exists on this fresh lake)."""
        from brutex.dashboard.discovery_warm import filter_spot_datasets
        from brutex.data.fno_candle_lake import FNO_CANDLE_LAKE_SEGMENT

        _seed_lake(lake)
        unpruned = filter_spot_datasets(db_view.list_candle_datasets(lake))
        pruned = filter_spot_datasets(
            db_view.list_candle_datasets(
                lake, exclude_segments=frozenset({FNO_CANDLE_LAKE_SEGMENT})
            )
        )
        assert pruned == unpruned
        assert pruned  # non-vacuous: real spot rows exist

    def test_pruned_read_equals_filtered_on_the_manifest_path(self, lake: Path) -> None:
        """The same equality on the MANIFEST-served path (an unfiltered walk
        seeds the manifest; both reads then serve from it)."""
        from brutex.dashboard.discovery_warm import filter_spot_datasets
        from brutex.data.fno_candle_lake import FNO_CANDLE_LAKE_SEGMENT

        _seed_lake(lake)
        _seed_ledger(lake, "NIFTY")  # the manifest needs a ledger token to stamp
        db_view.list_candle_datasets(lake)  # unfiltered walk → rebuilds the manifest
        unfiltered = filter_spot_datasets(db_view.list_candle_datasets(lake))
        pruned = filter_spot_datasets(
            db_view.list_candle_datasets(
                lake, exclude_segments=frozenset({FNO_CANDLE_LAKE_SEGMENT})
            )
        )
        assert pruned == unfiltered

    def test_spot_route_never_rebuilds_manifest_datasets_route_does(
        self, lake: Path, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The manifest division-of-duty spy, BOTH directions: the FILTERED
        spot-cascade walk never rebuilds (a filtered walk would persist a
        silently-partial row set — the db_view guard law), while the
        UNFILTERED datasets route keeps the rebuild duty."""
        _seed_lake(lake)
        _seed_ledger(lake, "NIFTY")
        calls: list[str] = []
        real_rebuild = db_view.rebuild_manifest

        def _spy(lake_root: Path, *args: Any, **kwargs: Any) -> Any:
            calls.append("rebuild")
            return real_rebuild(lake_root, *args, **kwargs)

        monkeypatch.setattr(db_view, "rebuild_manifest", _spy)
        assert client.get("/api/v1/db/spot/symbols").status_code == 200
        assert calls == []  # the filtered spot walk NEVER rebuilds
        assert client.get("/api/v1/db/candles/datasets").status_code == 200
        assert calls == ["rebuild"]  # the unfiltered datasets route still does


class _ParkedFold:
    """A parked `_window_counts_total` stand-in: first NEW call blocks on the
    release event (the slow O(rows + delta-footers) fold under churn); every
    call is counted. Pass-through to the real fold on release."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self._real = db_view._window_counts_total
        self._park = False

    def arm(self) -> None:
        self._park = True

    def __call__(self, conn: Any, source: Any) -> Any:
        self.calls += 1
        if self._park:
            self.entered.set()
            assert self.release.wait(timeout=_FOLD_HOLD_S)
        return self._real(conn, source)


class TestPullProgressServesLastKnown:
    """G4 — the pull-progress `phases` fold never blocks a poll past-floor."""

    def test_phases_serve_last_known_past_floor_with_parked_fold(
        self, lake: Path, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FAILS-BEFORE: a poll landing past the 120 s-capped floor after a
        stamp move recomputed the fold INLINE — with the fold parked, the
        route blocked for the whole hold (the operator's ~154 s feed drops).
        Now: the PRIOR stamp's counts serve within the budget and the fold
        runs in ONE background revalidator (the production daemon runner)."""
        _seed_ledger(lake, "NIFTY")
        first = client.get("/api/v1/universe/pull-progress")
        assert first.status_code == 200
        prior_phases = first.json()["phases"]
        assert any(p["done"] == 1 for p in prior_phases)  # the seeded row landed
        time.sleep(_FLOOR_AGE_S)  # age the entry past the 2 s base floor
        _append_delta(lake, "BANKNIFTY")  # the stamp moves (the mid-pull shape)
        fold = _ParkedFold()
        fold.arm()
        monkeypatch.setattr(db_view, "_window_counts_total", fold)
        try:
            started = time.monotonic()
            resp = client.get("/api/v1/universe/pull-progress")
            elapsed = time.monotonic() - started
            assert resp.status_code == 200
            assert elapsed < _SERVE_BUDGET_S, (
                f"pull-progress blocked {elapsed:.2f}s behind the parked fold — "
                "the counts cache must serve last-known past the floor"
            )
            assert resp.json()["phases"] == prior_phases  # the last-known counts
            assert fold.entered.wait(timeout=5.0)  # the revalidator IS folding
        finally:
            fold.release.set()
        # Bounded settle: the background revalidator lands the fresh counts.
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            phases = client.get("/api/v1/universe/pull-progress").json()["phases"]
            if any(p["done"] == 2 for p in phases):
                break
            time.sleep(0.05)
        else:  # pragma: no cover — a genuine hang fails loudly here
            pytest.fail("the background revalidate never landed the fresh counts")


class TestCoverageWindowsServesLastKnown:
    """G4 widened — /coverage/windows rows+counts never block past-floor."""

    def test_windows_page_serves_last_known_past_floor_with_parked_fold(
        self, lake: Path, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The operator's live repro (the windows page stuck at 'loading…'
        mid-pull): past the floor, with the counts fold PARKED, the route
        serves the PRIOR stamp's counts AND rows within the budget."""
        _seed_ledger(lake, "NIFTY")
        first = client.get("/api/v1/coverage/windows")
        assert first.status_code == 200
        prior = first.json()
        assert prior["count"] == 1
        time.sleep(_FLOOR_AGE_S)
        _append_delta(lake, "BANKNIFTY")
        fold = _ParkedFold()
        fold.arm()
        monkeypatch.setattr(db_view, "_window_counts_total", fold)
        try:
            started = time.monotonic()
            resp = client.get("/api/v1/coverage/windows")
            elapsed = time.monotonic() - started
            assert resp.status_code == 200
            assert elapsed < _SERVE_BUDGET_S, (
                f"/coverage/windows blocked {elapsed:.2f}s behind the parked fold"
            )
            body = resp.json()
            assert body["counts"] == prior["counts"]  # last-known counts
            assert [r["symbol"] for r in body["windows"]] == [
                r["symbol"] for r in prior["windows"]
            ]  # last-known rows — the page family serves too
        finally:
            fold.release.set()


def _instrument(symbol: str, security_id: int) -> Instrument:
    """A minimal IDX instrument for `upsert_many` (the nav-floor-rekey helper shape)."""
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=security_id,
        symbol_name=symbol,
        display_name=symbol,
        instrument=InstrumentType.IDX,
        segment=Segment.INDEX,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=0,
        tick_size=Decimal("0"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
    )


def _load_master(store: InstrumentStore, run_id: str) -> None:
    """One instrument load — moves `master_revision` WITHOUT a generation bump."""
    store.upsert_many(
        [_instrument("NIFTY", 13)],
        snapshot_date=date(2026, 1, 2),
        run_id=run_id,
        csv_source_sha256="0" * 64,
    )


class _ParkedTableSummary:
    """A parked `table_summary` stand-in (the tables twin of `_ParkedFold`):
    first armed call blocks on the release event (the COUNT(*) sweep convoyed
    behind the store read lock mid-pull); every call is counted. Pass-through
    to the real sweep on release."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self._real = app_module.table_summary
        self._park = False

    def arm(self) -> None:
        self._park = True

    def __call__(self, conn: Any, **kwargs: Any) -> Any:
        # Forwards the round-8 kw-only `raise_on_fault` flag (the route's
        # CACHED compute passes True; the degrade arm passes nothing).
        self.calls += 1
        if self._park:
            self.entered.set()
            assert self.release.wait(timeout=_FOLD_HOLD_S)
        return self._real(conn, **kwargs)


class TestDbTablesServesLastKnown:
    """The coordinator-directed tables-route fold — SWR + byte pin + key honesty."""

    def test_tables_serve_last_known_past_floor_with_parked_sweep(
        self, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FAILS-BEFORE: past-floor mid-pull the route ran the COUNT(*) sweep
        INLINE on the store read connection — with the sweep parked (the live
        ">12 s convoy" shape) the route blocked for the whole hold (the
        operator's "request timed out after 12000ms" tables list, twice).
        Now: the PRIOR token's counts serve within the budget and the sweep
        runs in ONE background revalidator (the production daemon runner)."""
        pull_on = {"v": False}
        app = build_app(
            DashboardState(), instrument_store=store, pull_active_probe=lambda: pull_on["v"]
        )
        with TestClient(app) as client:
            first = client.get("/api/v1/db/tables")
            assert first.status_code == 200
            prior = first.json()["tables"]
            assert [r["table"] for r in prior] == [
                "instruments",
                "instrument_runs",
                "schema_meta",
            ]
            time.sleep(_FLOOR_AGE_S)  # age the entry past the 2 s base floor
            pull_on["v"] = True
            _load_master(store, "r-mid-pull")  # the master token moves mid-pull
            parked = _ParkedTableSummary()
            parked.arm()
            monkeypatch.setattr(app_module, "table_summary", parked)
            try:
                started = time.monotonic()
                resp = client.get("/api/v1/db/tables")
                elapsed = time.monotonic() - started
                assert resp.status_code == 200
                assert elapsed < _SERVE_BUDGET_S, (
                    f"/api/v1/db/tables blocked {elapsed:.2f}s behind the parked "
                    "COUNT(*) sweep — the tables cache must serve last-known past "
                    "the floor"
                )
                assert resp.json()["tables"] == prior  # the last-known counts
                assert parked.entered.wait(timeout=5.0)  # the revalidator IS sweeping
            finally:
                parked.release.set()
            # Bounded settle: the background revalidator lands the fresh counts.
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                rows = client.get("/api/v1/db/tables").json()["tables"]
                counts = {r["table"]: r["row_count"] for r in rows}
                if counts["instruments"] == 1:
                    break
                time.sleep(0.05)
            else:  # pragma: no cover — a genuine hang fails loudly here
                pytest.fail("the background revalidate never landed the fresh counts")

    def test_tables_fresh_response_byte_identical_to_direct_summary(
        self, store: InstrumentStore
    ) -> None:
        """The byte-identity pin: a FRESH compute's wire bytes == the pre-cache
        route shape (`JSONResponse({"tables": table_summary(store.connection)})`),
        on a non-vacuous store (real instrument + run rows)."""
        _load_master(store, "r-byte-pin")
        app = build_app(DashboardState(), instrument_store=store)
        with TestClient(app) as client:
            resp = client.get("/api/v1/db/tables")
        assert resp.status_code == 200
        expected = JSONResponse({"tables": db_view.table_summary(store.connection)}).body
        assert resp.content == expected
        counts = {r["table"]: r["row_count"] for r in resp.json()["tables"]}
        assert counts["instruments"] == 1  # non-vacuous: real counts on the wire
        assert counts["schema_meta"] >= 1  # the store's own schema_version row

    def test_tables_cache_key_honesty(
        self, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Token move → recompute (BOTH components: the master content revision
        AND the write generation); no move → O(1) hit, zero sweeps."""
        summary_cache = SummaryCache()
        counted = {"n": 0}
        real = app_module.table_summary

        def _spy(conn: Any, **kwargs: Any) -> Any:
            counted["n"] += 1
            return real(conn, **kwargs)

        monkeypatch.setattr(app_module, "table_summary", _spy)
        app = build_app(DashboardState(), instrument_store=store, summary_cache=summary_cache)
        with TestClient(app) as client:
            assert client.get("/api/v1/db/tables").status_code == 200
            assert counted["n"] == 1
            assert client.get("/api/v1/db/tables").status_code == 200
            assert counted["n"] == 1  # no token move → hit
            _load_master(store, "r-key-honesty")  # master moves, NO generation bump
            rows = client.get("/api/v1/db/tables").json()["tables"]
            assert counted["n"] == 2  # the folded master token recomputed
            assert {r["table"]: r["row_count"] for r in rows}["instruments"] == 1
            summary_cache.mark_dirty()  # the write-generation component moves
            assert client.get("/api/v1/db/tables").status_code == 200
            assert counted["n"] == 3
            assert client.get("/api/v1/db/tables").status_code == 200
            assert counted["n"] == 3  # settled again → hit

    def test_tables_probe_fault_degrades_to_direct_uncached_sweep(
        self, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A freshness-probe fault takes today's exact pre-cache path: one
        direct UNCACHED sweep per open (never a 500, nothing cached)."""
        counted = {"n": 0}
        real = app_module.table_summary

        def _spy(conn: Any, **kwargs: Any) -> Any:
            counted["n"] += 1
            return real(conn, **kwargs)

        def _raise(_store: Any) -> Any:
            raise OSError("probe outage")

        monkeypatch.setattr(app_module, "table_summary", _spy)
        monkeypatch.setattr(app_module, "master_revision", _raise)
        app = build_app(DashboardState(), instrument_store=store)
        with TestClient(app) as client:
            r1 = client.get("/api/v1/db/tables")
            assert r1.status_code == 200
            assert counted["n"] == 1
            r2 = client.get("/api/v1/db/tables")
            assert r2.status_code == 200
            assert counted["n"] == 2  # uncached per open during the outage
            assert r2.content == r1.content  # the real sweep still serves

    def test_tables_wedged_store_degrades_to_honest_zero_counts(
        self, store: InstrumentStore
    ) -> None:
        """FAILS-BEFORE (round-7 review MAJOR): a WEDGED store
        (DEC-STORE-WRITE-DEADLINE-001 — an abandoned coverage write mid-pull,
        the EXACT contention window this DEC targets) made the
        ``master_revision`` freshness probe raise ``CatalogError`` — a
        ``DataLakeError``, NOT in ``STORE_FAULT_TYPES=(duckdb.Error, OSError)``
        — which escaped the probe-fault clause and ERRORED the route (500)
        where the pre-cache route answered the honest 0-counts 200. Now the
        wedge takes the SAME degrade: a 200 whose bytes equal today's exact
        pre-cache path on the wedged store, nothing cached — and once the
        wedge clears, the route recovers to REAL counts (proving the wedged
        response never poisoned the cache)."""
        _load_master(store, "r-wedge")
        app = build_app(DashboardState(), instrument_store=store)
        with TestClient(app) as client:
            # The documented wedge latch (StoreWriteDeadlineGuard): _active
            # non-None <=> wedged <=> _ensure_not_wedged raises CatalogError
            # on the read view's execute — a deterministic stand-in for the
            # abandoned-coverage-write breach, no zombie thread needed.
            guard = store._write_guard  # pyright: ignore[reportPrivateUsage]
            guard._active = object()  # pyright: ignore[reportPrivateUsage]
            try:
                # Today's exact pre-cache body on the SAME wedged store:
                # table_summary swallows the per-table CatalogError -> 0-counts.
                expected = JSONResponse({"tables": db_view.table_summary(store.connection)}).body
                resp = client.get("/api/v1/db/tables")
                assert resp.status_code == 200  # never a 500 on the wedge
                assert resp.content == expected  # byte-for-byte the degrade
                counts = {r["table"]: r["row_count"] for r in resp.json()["tables"]}
                assert counts == {
                    "instruments": 0,
                    "instrument_runs": 0,
                    "schema_meta": 0,
                }
            finally:
                guard._active = None  # pyright: ignore[reportPrivateUsage]
            # Nothing cached during the wedge: the healed store serves the
            # REAL counts on the very next open (no stale 0-count poison).
            recovered = client.get("/api/v1/db/tables")
            assert recovered.status_code == 200
            healed = {r["table"]: r["row_count"] for r in recovered.json()["tables"]}
            assert healed["instruments"] == 1
            assert healed["schema_meta"] >= 1

    def test_tables_wedge_after_probe_toctou_never_caches_zero_counts(
        self, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FAILS-BEFORE (round-8 review MINOR — the probe→compute TOCTOU):
        with the freshness probe HEALTHY, a store that wedged in the
        probe→compute window made ``table_summary`` swallow the wedge
        latch's per-table ``CatalogError`` into all-zero counts, and
        ``get_or_compute`` CACHED those zeros under the healthy pre-wedge
        token — served as stale zeros until an unrelated token move (a
        wedge self-heal bumps nothing). Now the CACHED compute is the
        raising ``raise_on_fault=True`` form: the wedged request answers
        the SAME direct uncached 0-count degrade body as the round-7 wedge
        pin, NOTHING lands in the tables cache (size 0 asserted), and the
        healed store computes the REAL counts on the very next open at the
        SAME token."""
        _load_master(store, "r-toctou")
        captured: list[Any] = []
        real_cls = app_module.MoversSnapshotCache

        def _capture(*args: Any, **kwargs: Any) -> Any:
            inst = real_cls(*args, **kwargs)
            # The tables cache is the ONE SWR-flagged capacity-8 instance.
            if kwargs.get("capacity") == 8 and kwargs.get("stale_while_revalidate"):
                captured.append(inst)
            return inst

        monkeypatch.setattr(app_module, "MoversSnapshotCache", _capture)
        app = build_app(DashboardState(), instrument_store=store)
        assert len(captured) == 1
        tables_cache = captured[0]
        guard = store._write_guard  # pyright: ignore[reportPrivateUsage]
        real_probe = app_module.master_revision
        arm = {"v": True}

        def _probe_then_wedge(s: Any) -> Any:
            token = real_probe(s)  # the HEALTHY probe succeeds first…
            if arm["v"]:
                arm["v"] = False  # one-shot: the wedge latches AFTER the probe
                guard._active = object()  # pyright: ignore[reportPrivateUsage]
            return token

        monkeypatch.setattr(app_module, "master_revision", _probe_then_wedge)
        with TestClient(app) as client:
            try:
                # Today's exact wedged direct-sweep body (the round-7 pin shape).
                guard._active = object()  # pyright: ignore[reportPrivateUsage]
                expected = JSONResponse({"tables": db_view.table_summary(store.connection)}).body
                guard._active = None  # pyright: ignore[reportPrivateUsage]
                resp = client.get("/api/v1/db/tables")
                assert resp.status_code == 200  # never a 500 in the TOCTOU window
                assert resp.content == expected  # the same uncached degrade body
                assert tables_cache.size() == 0, (
                    "a wedge-degraded 0-count summary was STORED under the "
                    "healthy pre-wedge token (the round-8 TOCTOU poison)"
                )
            finally:
                guard._active = None  # pyright: ignore[reportPrivateUsage]
            # Healed store, SAME token (no master move, no generation bump):
            # the next open must COMPUTE the real counts, never serve zeros.
            recovered = client.get("/api/v1/db/tables")
            assert recovered.status_code == 200
            healed = {r["table"]: r["row_count"] for r in recovered.json()["tables"]}
            assert healed["instruments"] == 1
            assert healed["schema_meta"] >= 1
            assert tables_cache.size() == 1  # the REAL counts landed post-heal

    def test_tables_fault_sets_member_equal(self) -> None:
        """The route's probe/degrade catch set and the strict sweep's raise
        set MUST stay member-equal — a strict-mode raise the route clause
        cannot catch would 500 where the law is the uncached degrade
        (the round-8 drift pin)."""
        assert set(
            app_module._TABLES_PROBE_FAULT_TYPES  # pyright: ignore[reportPrivateUsage]
        ) == set(db_view.TABLE_SUMMARY_FAULT_TYPES)
