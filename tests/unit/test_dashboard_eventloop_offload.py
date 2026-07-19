"""Event-loop offload regressions (DEC-DASHBOARD-EVENTLOOP-OFFLOAD-001).

Forensically proven on the operator's 2026-07-04 completed 18.6 h run:
``GET /api/v1/reports/preopen/symbols`` took 361,596 ms and froze EVERY
dashboard request for 6 minutes, because the ``async def`` route ran
``resolve_report_datasets`` → ``list_candle_datasets`` (a full ``bars/`` tree
walk over ~66k FNO dirs) SYNCHRONOUSLY on the uvicorn event loop during the
prebuild write storm — 38 queued requests burst-flushed the second it
returned. The fix moves the blocking work onto the house
``run_in_threadpool`` pattern (DEC-DASHBOARD-FASTREAD-001), like every other
lake read in ``app.py``.

The oracle here is the DIRECT one: the walk is replaced by a stub that BLOCKS
on an event; while it is blocked, a concurrent ``/api/v1/state`` request must
still complete (the loop is free). On the pre-fix code the loop itself is
wedged inside the walk, so the concurrent request cannot even be dispatched —
the test fails (verified by reverting the route body).

Same-technique pin for the ``.xlsx`` render offload (the CPU-bound workbook
serialisation now also rides the threadpool).

DEC-LAKE-UNIQUENESS-OFFLOAD-001 (MIGRATION.md #1302): the same pin for the
``/api/v1/universe/lake-uniqueness`` census route. A round-5 forensic finding
(operator's 2026-07-05 run) measured that route at 30-46 s (max 46,615 ms) on a
~40k-contract lake and flagged it as the last route with >5 s latency. A
class-sweep of every ``async def`` route in ``app.py`` confirmed the route
already offloads its ``scan_lake_invariants`` whole-lake walk via
``run_in_threadpool`` (added at the route's birth in #1074, DEC-LAKE-HEAL-001) —
so its latency is the scan's OWN O(files) cost, NOT an event-loop block (it never
wedged the loop, matching the finding's own "did not cause a full blackout"
note). This test PINS that already-correct offload — the one offloaded lake route
that previously lacked a dedicated regression — so a future refactor cannot
silently inline the scan back onto the loop.

DEC-OPERATOR-PAGE-REAL-001 (hostile-round MINOR-2): the same pin for ALL SIX
``/api/v1/oneclick/*`` routes. Each route's blocking artifact work lives in a
``_resolve`` closure offloaded via ``run_in_threadpool``; every ``_resolve``
begins by resolving the fixture root, so blocking
``oneclick_artifacts.resolve_fixture_root`` blocks the whole read path of
each route on a WORKER — a future refactor that inlines ``_resolve`` back
onto the ``async def`` body wedges the loop and fails the pin.

Skipped cleanly without the ``[ui]`` extra (matches the sibling dashboard
suites).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Generator
from pathlib import Path
from types import SimpleNamespace
from typing import TypeVar

import pytest

pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")
pytest.importorskip("httpx", reason="TestClient needs httpx")

from brutex.config.feed_scope import FEED_SCOPE_ENV as _FEED_SCOPE_ENV_1934
from brutex.dashboard.app import DashboardState, build_app
from brutex.dashboard.lake_offload import LakeOffload
from brutex.data.instrument_store import InstrumentStore
from brutex.data.lake_invariants import LakeScanReport
from fastapi.testclient import TestClient

_T = TypeVar("_T")

#: How long the stubbed walk blocks (upper bound; released early on pass).
_WALK_HOLD_S: float = 5.0

#: Budget for the concurrent read while the walk is blocked. A free loop
#: answers /api/v1/state in single-digit ms; a loop wedged inside the walk
#: waits the full hold (10x over budget) — unambiguous separation.
_READ_BUDGET_S: float = 2.0


@pytest.fixture
def store(tmp_path: Path) -> Generator[InstrumentStore]:
    lake_root = tmp_path / "lake"
    lake_root.mkdir()
    s = InstrumentStore.open(lake_root / "instruments.duckdb")
    yield s
    s.close()


@pytest.fixture
def client(store: InstrumentStore) -> Generator[TestClient]:
    app = build_app(DashboardState(), instrument_store=store)
    with TestClient(app) as c:
        yield c


class _BlockingStub:
    """A stand-in for the blocking lake work: waits on an event, then returns.

    ``entered`` is set the moment a worker thread enters the stub (the barrier
    the test synchronises on); ``release`` unblocks it. If the stub ran ON the
    event loop (the regression), the loop is wedged for the whole hold and the
    concurrent request cannot complete within its budget.
    """

    def __init__(self, result: object) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self._result = result

    def __call__(self, *args: object, **kwargs: object) -> object:
        self.entered.set()
        self.release.wait(timeout=_WALK_HOLD_S)
        return self._result


def _assert_loop_free_while_blocked(
    client: TestClient, stub: _BlockingStub, url: str
) -> tuple[int, object]:
    """Drive *url* into the blocked stub; prove /api/v1/state still answers.

    Returns the eventually-completed blocked request's ``(status, json)``.
    """
    result: dict[str, object] = {}

    def _blocked_request() -> None:
        resp = client.get(url)
        result["status"] = resp.status_code
        result["json"] = resp.json()

    t = threading.Thread(target=_blocked_request, daemon=True)
    t.start()
    try:
        assert stub.entered.wait(timeout=5.0), "the stubbed blocking work was never entered"
        # The stub is now blocking a THREADPOOL worker. If it were blocking the
        # EVENT LOOP instead (the regression), this concurrent request could
        # not be served until the stub returns.
        t0 = time.perf_counter()
        state = client.get("/api/v1/state")
        elapsed = time.perf_counter() - t0
        assert state.status_code == 200
        assert elapsed < _READ_BUDGET_S, (
            f"/api/v1/state took {elapsed * 1000:.1f}ms while {url} blocked — "
            "the blocking work is running ON the event loop (regression)."
        )
    finally:
        stub.release.set()
        t.join(timeout=10.0)
    assert not t.is_alive(), f"the blocked {url} request never completed"
    return int(result["status"]), result["json"]  # type: ignore[arg-type]


def test_preopen_symbols_walk_runs_off_the_event_loop(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The proven 6-minute blackout: the symbols walk must not wedge the loop.

    Fails on the pre-fix route (the walk ran synchronously in the ``async
    def`` body — the loop was wedged and the concurrent /api/v1/state read
    waited out the whole hold).
    """
    stub = _BlockingStub(result=[("NSE", "INDEX", "NIFTY")])
    monkeypatch.setattr("brutex.dashboard.app.resolve_report_datasets", stub)

    status, payload = _assert_loop_free_while_blocked(
        client, stub, "/api/v1/reports/preopen/symbols"
    )
    # The offloaded request still returns the real payload once released.
    assert status == 200
    assert isinstance(payload, dict)
    assert payload["basis"] == "day"
    assert payload["symbols"] == [
        {"exchange": "NSE", "segment": "INDEX", "symbol": "NIFTY", "option_pullable": False}
    ]


def test_preopen_xlsx_render_runs_off_the_event_loop(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CPU-bound workbook serialisation also rides the threadpool."""
    stub = _BlockingStub(result=b"not-a-real-workbook")
    monkeypatch.setattr("brutex.dashboard.app.render_xlsx", stub)

    result: dict[str, object] = {}

    def _blocked_request() -> None:
        resp = client.get("/api/v1/reports/preopen.xlsx?from=2024-01-01&to=2024-01-02")
        result["status"] = resp.status_code
        result["body"] = resp.content

    t = threading.Thread(target=_blocked_request, daemon=True)
    t.start()
    try:
        assert stub.entered.wait(timeout=5.0), "render_xlsx stub was never entered"
        t0 = time.perf_counter()
        state = client.get("/api/v1/state")
        elapsed = time.perf_counter() - t0
        assert state.status_code == 200
        assert elapsed < _READ_BUDGET_S, (
            f"/api/v1/state took {elapsed * 1000:.1f}ms while the xlsx render "
            "blocked — the render is running ON the event loop (regression)."
        )
    finally:
        stub.release.set()
        t.join(timeout=10.0)
    assert not t.is_alive(), "the blocked .xlsx request never completed"
    assert result["status"] == 200
    assert result["body"] == b"not-a-real-workbook"


def test_preopen_symbols_payload_unchanged_through_offload(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Byte-shape sanity: the offloaded route returns the same payload contract.

    The stub mirrors the real ``resolve_report_datasets`` signature incl. the
    #1934 ``scope=`` kwarg (DEC-UNIVERSE-INDEX-ONLY-001 design §3 R3) the
    route now threads.
    """
    monkeypatch.setattr(
        "brutex.dashboard.app.resolve_report_datasets",
        lambda lake, wanted=None, basis="day", scope=None: [("NSE", "CASH", "COALINDIA")],
    )
    resp = client.get("/api/v1/reports/preopen/symbols?basis=minute")
    assert resp.status_code == 200
    body = resp.json()
    assert body["basis"] == "minute"
    assert body["symbols"] == [
        {"exchange": "NSE", "segment": "CASH", "symbol": "COALINDIA", "option_pullable": False}
    ]


def _empty_scan_report() -> LakeScanReport:
    """A clean, valid empty scan outcome the real census folders accept.

    The blocking stub returns this so the REAL ``build_lake_census`` +
    ``census_to_dict`` run on the worker (proving the whole census — not just the
    scan — rides the threadpool + returns the real payload contract), rather than
    a bare sentinel that would trip ``build_lake_census``'s attribute reads.
    """
    return LakeScanReport(
        findings=(),
        files_scanned=0,
        files_skipped=0,
        series_scanned=0,
        option_files_scanned=0,
        option_files_skipped=0,
        duration_s=0.0,
    )


def test_lake_uniqueness_scan_runs_on_the_dedicated_census_daemon_thread(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The census runs on the DEDICATED daemon thread — never the loop, never the pool.

    SUPERSESSION (DEC-LAKE-CENSUS-NONBLOCKING-001): this test previously pinned
    the DEC-LAKE-UNIQUENESS-OFFLOAD-001 ``run_in_threadpool`` offload of the
    census scan. That offload was REAL (#1302/#1316 — the loop never wedged),
    but it consumed shared AnyIO threadpool tokens, and ``SummaryCache``'s cold
    single-flight parked every stacked 15 s poller in an unbounded wait ON a
    worker — the reproduced 26-minute dashboard-wide freeze. The route now uses
    NO threadpool at all: it returns the ``LakeCensusHolder``'s O(1) snapshot
    immediately, and the scan runs on the named ``brutex-lake-census`` daemon
    thread (asserted below — an AnyIO worker carries a different name; the
    route completing while the scan is STILL HELD proves the request awaits
    neither the loop nor a worker running the scan).
    """
    thread_names: list[str] = []
    entered = threading.Event()
    release = threading.Event()

    def _held_scan(*args: object, **kwargs: object) -> object:
        thread_names.append(threading.current_thread().name)
        entered.set()
        release.wait(timeout=_WALK_HOLD_S)
        return _empty_scan_report()

    monkeypatch.setattr("brutex.dashboard.app.scan_lake_invariants", _held_scan)
    try:
        # The arming poll returns IMMEDIATELY (an O(1) snapshot) — the pre-fix
        # route blocked here for the whole scan on a threadpool worker.
        t0 = time.perf_counter()
        first = client.get("/api/v1/universe/lake-uniqueness")
        first_elapsed = time.perf_counter() - t0
        assert first.status_code == 200
        assert first_elapsed < _READ_BUDGET_S, (
            f"the arming census poll took {first_elapsed * 1000:.1f}ms — the route "
            "must snapshot, never wait for the scan."
        )
        assert entered.wait(timeout=5.0), "the census scan was never entered"

        # While the scan is held: the loop is free AND the snapshot is honest.
        t0 = time.perf_counter()
        state = client.get("/api/v1/state")
        elapsed = time.perf_counter() - t0
        assert state.status_code == 200
        assert elapsed < _READ_BUDGET_S, (
            f"/api/v1/state took {elapsed * 1000:.1f}ms while the census scan was "
            "held — the scan is running ON the event loop (regression)."
        )
        held = client.get("/api/v1/universe/lake-uniqueness").json()
        assert held["present"] is True
        assert held["scan_in_progress"] is True
        assert "findings_total" not in held  # no verdict fabricated mid-first-scan
    finally:
        release.set()

    # The scan ran on the DEDICATED census daemon thread — zero AnyIO tokens.
    assert thread_names == ["brutex-lake-census"]

    # Once the scan lands, the verdict serves (the REAL build_lake_census +
    # census_to_dict ran on the daemon thread over the stubbed-empty report).
    deadline = time.time() + 10.0
    payload: dict[str, object] = {}
    while time.time() < deadline:
        payload = client.get("/api/v1/universe/lake-uniqueness").json()
        if "findings_total" in payload:
            break
        time.sleep(0.05)
    assert payload["present"] is True
    assert payload["findings_total"] == 0
    assert payload["files_scanned"] == 0
    assert payload["scan_in_progress"] is False


# ── DEC-DASHBOARD-EVENTLOOP-OFFLOAD-002: the /universe backend + generation probe ──


def test_universe_summary_read_runs_off_the_event_loop(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``/universe/summary`` cache read must not wedge the loop.

    ``GET /api/v1/universe/summary`` now offloads ``UniverseSnapshotCache.summary``
    (its revision probe is a sync DuckDB ``COUNT`` + ``MAX``, plus the three base
    reads on a miss) via ``run_in_threadpool``. Blocking the cache's revision probe
    proves the read runs on a worker: a concurrent ``/api/v1/state`` still answers.
    On the pre-fix code the probe ran in the ``async def`` body and wedged the loop.
    """
    stub = _BlockingStub(result=(0, ""))
    monkeypatch.setattr("brutex.dashboard.universe_cache.master_revision", stub)

    status, payload = _assert_loop_free_while_blocked(client, stub, "/api/v1/universe/summary")
    assert status == 200
    assert payload == {
        "index_count": 0,
        "stock_count": 0,
        "total": 0,
        "reference_stock_count": 0,
        "stock_count_total": 0,
    }


def test_universe_index_constituents_reads_run_off_the_event_loop(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, store: InstrumentStore
) -> None:
    """The ``/universe/index-constituents`` store reads must not wedge the loop.

    The three master reads + the COMMON classifier now run in a ``_held_universe``
    threadpool closure. Blocking the first store read proves they run on a worker:
    a concurrent ``/api/v1/state`` still answers. The best-effort NSE resolve is
    stubbed so the test exercises the store-read offload, not the network path.

    EXPLICIT full arm (DEC-UNIVERSE-INDEX-ONLY-001 — the §6 migration Law 2):
    constituent picking is STOCK machinery, dormant under the shipped index4
    default (the A5b short-circuit answers BEFORE any store read — the
    dormancy guard arm lives in ``test_dashboard_app_backfill.py``), so the
    store-read offload under pin is reachable only under a stock-admitting
    scope.
    """
    monkeypatch.setenv(_FEED_SCOPE_ENV_1934, "full")
    monkeypatch.setattr(
        "brutex.dashboard.constituents_cache.resolve_constituents",
        lambda *_a, **_k: SimpleNamespace(rows=[], source="unavailable", as_of=None),
    )
    stub = _BlockingStub(result=frozenset())
    monkeypatch.setattr(store, "fno_eligible_underlyings", stub)

    status, payload = _assert_loop_free_while_blocked(
        client, stub, "/api/v1/universe/index-constituents?symbol=NIFTY"
    )
    assert status == 200
    assert isinstance(payload, dict)
    assert payload["source"] == "unavailable"
    assert payload["members"] == []


def test_fno_cascade_generation_probe_runs_off_the_event_loop(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The write-generation probe must not wedge the loop (the MAJOR-6 class).

    ``_summary_cache.generation`` stats the window ledger + digests the sweeps tree
    (an O(dirs) read via ``lake_revision``). The cascade/movers/preopen/db-datasets
    routes now read it INSIDE their offloaded closure, so blocking ``lake_revision``
    blocks a worker — a concurrent ``/api/v1/state`` still answers. On the pre-fix
    code ``gen = _summary_cache.generation`` ran on the event loop and wedged it.
    """
    stub = _BlockingStub(result="rev-blocking-token")
    monkeypatch.setattr("brutex.dashboard.app.lake_revision", stub)

    status, payload = _assert_loop_free_while_blocked(client, stub, "/api/v1/db/fno/underlyings")
    assert status == 200
    assert isinstance(payload, dict)
    assert payload["underlyings"] == []
    assert payload["unparsed"] == []


#: The six sweep-oneclick artifact routes (DEC-OPERATOR-PAGE-REAL-001) — the
#: threadpool-doctrine audit set (hostile-round MINOR-2).
_ONECLICK_OFFLOAD_URLS = (
    "/api/v1/oneclick/runs",
    "/api/v1/oneclick/manifest?run_id=r-1",
    "/api/v1/oneclick/top100?run_id=r-1",
    "/api/v1/oneclick/events?run_id=r-1",
    "/api/v1/oneclick/trades?run_id=r-1",
    "/api/v1/oneclick/verify?run_id=r-1",
)


@pytest.mark.parametrize("url", _ONECLICK_OFFLOAD_URLS)
def test_oneclick_routes_run_off_the_event_loop(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    """All six /api/v1/oneclick/* routes ride the threadpool (MINOR-2 pin).

    Every route's ``_resolve`` body starts by resolving the fixture root
    (``_oneclick_roots`` for the artifact routes; directly for trades), so a
    blocked ``resolve_fixture_root`` blocks the WHOLE artifact read path of
    each route. While it blocks a worker, a concurrent ``/api/v1/state`` must
    still answer within budget (a free loop). If a future refactor inlined
    ``_resolve`` onto the ``async def`` body, the loop would wedge for the
    whole hold — exactly the #1143 preopen regression class.
    """
    stub = _BlockingStub(result=None)  # None ⇒ the production (no-fixture) path
    monkeypatch.setattr("brutex.dashboard.oneclick_artifacts.resolve_fixture_root", stub)

    status, payload = _assert_loop_free_while_blocked(client, stub, url)
    # Once released the route completes with the honest production payload
    # (no fixture root, no artifacts on this empty lake) — never a 500.
    assert status == 200
    assert isinstance(payload, dict)
    assert payload["fixture_root_active"] is False


# ── DEC-DASHBOARD-LAKE-IO-LIMITER-002 — pool-choice dispatch pins (#1757) ──


class _CountingLakeOffload(LakeOffload):
    """A LakeOffload that records every dispatched callable's qualname."""

    def __init__(self) -> None:
        super().__init__()
        self.dispatched: list[str] = []

    async def __call__(  # signature mirrors the parent exactly (pyright on tests)
        self, func: Callable[..., _T], /, *args: object, **kwargs: object
    ) -> _T:
        self.dispatched.append(getattr(func, "__qualname__", repr(func)))
        return await super().__call__(func, *args, **kwargs)


class TestLakeLimiterRouteDispatch:
    """Pool-choice pins via the ``build_app(lake_offload=)`` seam (#1757).

    The wedged-lake realserver regression proves the LIMITER binds; these
    pins prove WHICH pool each route's dispatch rides — the class the equity
    route's bare ``run_in_threadpool`` regression (the #1665 merge-seam
    straggler) was invisible to.
    """

    @pytest.fixture
    def counting_client(
        self, store: InstrumentStore
    ) -> Generator[tuple[TestClient, _CountingLakeOffload]]:
        counting = _CountingLakeOffload()
        app = build_app(DashboardState(), instrument_store=store, lake_offload=counting)
        with TestClient(app) as c:
            yield c, counting

    def test_oneclick_equity_dispatches_through_the_lake_limiter(
        self, counting_client: tuple[TestClient, _CountingLakeOffload]
    ) -> None:
        """FAILS pre-fix: the equity dispatch was bare run_in_threadpool."""
        client, counting = counting_client
        resp = client.get("/api/v1/oneclick/equity?run_id=r-x&rank=1")
        assert resp.status_code == 200  # empty-lake awaiting — the route executed
        assert any("api_oneclick_equity.<locals>._resolve" in q for q in counting.dispatched), (
            f"equity _resolve not on the lake limiter; saw {counting.dispatched}"
        )

    def test_pull_rate_stays_on_the_default_pool(
        self, counting_client: tuple[TestClient, _CountingLakeOffload]
    ) -> None:
        """The deliberate-keep NEGATIVE twin (#1642 doctrine): the deadline-
        guarded liveness memo must NOT consume a lake token — a lake outage
        must never starve the route that reports on the lake outage."""
        client, counting = counting_client
        before = len(counting.dispatched)
        assert client.get("/api/v1/pull/rate").status_code == 200
        newly_dispatched = counting.dispatched[before:]
        assert not any("api_pull_rate" in q or "RateBridgeCache" in q for q in newly_dispatched), (
            f"/api/v1/pull/rate dispatched through the LAKE limiter "
            f"({newly_dispatched}) — the deadline-guarded liveness memo must "
            "stay on the default pool (DEC-DASHBOARD-LAKE-IO-LIMITER-001 "
            "deliberate-keep set)"
        )
