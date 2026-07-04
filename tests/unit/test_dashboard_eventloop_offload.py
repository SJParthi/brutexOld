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

Skipped cleanly without the ``[ui]`` extra (matches the sibling dashboard
suites).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Generator
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")
pytest.importorskip("httpx", reason="TestClient needs httpx")

from brutex.dashboard.app import DashboardState, build_app
from brutex.data.instrument_store import InstrumentStore
from fastapi.testclient import TestClient

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
    """Byte-shape sanity: the offloaded route returns the same payload contract."""
    monkeypatch.setattr(
        "brutex.dashboard.app.resolve_report_datasets",
        lambda lake, wanted=None, basis="day": [("NSE", "CASH", "COALINDIA")],
    )
    resp = client.get("/api/v1/reports/preopen/symbols?basis=minute")
    assert resp.status_code == 200
    body = resp.json()
    assert body["basis"] == "minute"
    assert body["symbols"] == [
        {"exchange": "NSE", "segment": "CASH", "symbol": "COALINDIA", "option_pullable": False}
    ]
