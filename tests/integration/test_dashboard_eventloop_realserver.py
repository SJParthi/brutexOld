"""Real-uvicorn event-loop offload guard (DEC-DASHBOARD-EVENTLOOP-OFFLOAD-002).

Why a REAL server, not ``fastapi.testclient.TestClient``: the TestClient drives
each request through its own blocking portal, so it CANNOT reliably reproduce the
*cross-request* event-loop wedge — a stalled synchronous read in one ``async def``
route body freezing a concurrent request to a DIFFERENT route. This guard starts a
real :class:`uvicorn.Server` in a thread (so ``monkeypatch`` still reaches the
handler — shared process memory), stalls the underlying blocking read (the
``UniverseSnapshotCache`` DuckDB revision probe, and the ``lake_revision`` sweeps
digest behind ``_summary_cache.generation``) for ~5 s, and asserts a concurrent
poll to an already-offloaded route (``/api/v1/state``) still answers in well under
that stall. On the pre-offload code the read runs ON the uvicorn event loop and the
concurrent poll is frozen for the whole stall — this test then FAILS (verified by
reverting the route body); with the reads offloaded via ``run_in_threadpool`` the
loop stays free and the poll returns in single-digit ms.

Skipped cleanly without the ``[ui]`` extra (matches the sibling dashboard suites).
"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.request
from collections.abc import Generator, Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")
pytest.importorskip("uvicorn", reason="the real-server guard needs uvicorn")

import uvicorn
from brutex.dashboard.app import DashboardState, build_app
from brutex.data.instrument_store import InstrumentStore

#: Upper bound the stalled read blocks before it is released (released early on pass).
_STALL_S: float = 5.0
#: Budget for the concurrent poll while the read is stalled. A free loop answers
#: /api/v1/state in single-digit ms; a loop wedged inside the read waits the whole
#: stall (well over budget) — an unambiguous separation.
_POLL_BUDGET_S: float = 2.0


@pytest.fixture
def store(tmp_path: Path) -> Iterator[InstrumentStore]:
    lake_root = tmp_path / "lake"
    lake_root.mkdir()
    s = InstrumentStore.open(lake_root / "instruments.duckdb")
    yield s
    s.close()


def _free_port() -> int:
    """Grab an ephemeral localhost port (closed immediately; uvicorn re-binds it)."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _RealServer:
    """A real uvicorn server run in a daemon thread for cross-request wedge tests."""

    def __init__(self, app: Any, port: int) -> None:
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self.port = port

    def start(self) -> None:
        self._thread.start()
        deadline = time.time() + 10.0
        while not self._server.started and time.time() < deadline:
            time.sleep(0.02)
        if not self._server.started:  # pragma: no cover - CI startup failure
            raise RuntimeError("uvicorn server did not start within 10s")

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10.0)


@pytest.fixture
def server(store: InstrumentStore) -> Generator[_RealServer]:
    srv = _RealServer(build_app(DashboardState(), instrument_store=store), _free_port())
    srv.start()
    try:
        yield srv
    finally:
        srv.stop()


def _get(port: int, path: str, timeout: float) -> tuple[int, bytes]:
    """Blocking GET via stdlib urllib (called from worker threads)."""
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as resp:
        return resp.status, resp.read()


def _assert_loop_free_while_stalled(
    server: _RealServer,
    monkeypatch: pytest.MonkeyPatch,
    *,
    patch_target: str,
    stall_return: object,
    blocked_path: str,
) -> tuple[int, bytes]:
    """Stall *patch_target*, fire *blocked_path*, prove /api/v1/state stays free.

    Returns the eventually-completed blocked request's ``(status, body)``.
    """
    entered = threading.Event()
    release = threading.Event()

    def _stall(*_a: object, **_k: object) -> object:
        entered.set()
        release.wait(timeout=_STALL_S)
        return stall_return

    monkeypatch.setattr(patch_target, _stall)

    blocked: dict[str, Any] = {}

    def _fire_blocked() -> None:
        try:
            blocked["status"], blocked["body"] = _get(server.port, blocked_path, timeout=15.0)
        except Exception as exc:  # pragma: no cover - only on a hung/failed offload
            blocked["error"] = repr(exc)

    thread = threading.Thread(target=_fire_blocked, daemon=True)
    thread.start()
    try:
        assert entered.wait(timeout=_STALL_S + 2.0), "the stalled read was never entered"
        # The read now blocks (a worker thread if offloaded; the event loop if not).
        # A concurrent poll to an already-offloaded route must still be served fast.
        start = time.perf_counter()
        status, _ = _get(server.port, "/api/v1/state", timeout=_STALL_S + 2.0)
        elapsed = time.perf_counter() - start
        assert status == 200
        assert elapsed < _POLL_BUDGET_S, (
            f"/api/v1/state took {elapsed * 1000:.0f}ms while {blocked_path} stalled — "
            "the blocking read is running ON the uvicorn event loop (regression)."
        )
    finally:
        release.set()
        thread.join(timeout=10.0)
    assert not thread.is_alive(), f"the blocked {blocked_path} request never completed"
    assert "error" not in blocked, f"blocked request failed: {blocked.get('error')}"
    return int(blocked["status"]), bytes(blocked["body"])


def test_universe_summary_revision_probe_off_loop_real_server(
    server: _RealServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stalled ``master_revision`` DuckDB probe must not freeze a concurrent poll.

    ``/api/v1/universe/summary`` offloads ``UniverseSnapshotCache.summary`` (whose
    revision probe is a sync DuckDB ``COUNT`` + ``MAX``) via ``run_in_threadpool``.
    Stalling ``master_revision`` blocks a worker, not the loop, so ``/api/v1/state``
    stays fast. Pre-offload the probe ran on the loop and this FAILS.
    """
    status, _ = _assert_loop_free_while_stalled(
        server,
        monkeypatch,
        patch_target="brutex.dashboard.universe_cache.master_revision",
        stall_return=(0, ""),
        blocked_path="/api/v1/universe/summary",
    )
    assert status == 200


def test_generation_probe_off_loop_real_server(
    server: _RealServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stalled ``lake_revision`` sweeps digest must not freeze a concurrent poll.

    ``_summary_cache.generation`` stats the ledger + digests the ``<lake>/sweeps``
    tree (O(dirs)). The cascade/movers/preopen/db-datasets routes now read it INSIDE
    their offloaded closure, so stalling ``lake_revision`` blocks a worker, not the
    loop. Pre-offload ``gen = _summary_cache.generation`` ran on the loop and this
    FAILS.
    """
    status, body = _assert_loop_free_while_stalled(
        server,
        monkeypatch,
        patch_target="brutex.dashboard.app.lake_revision",
        stall_return="rev-stalled-token",
        blocked_path="/api/v1/db/fno/underlyings",
    )
    assert status == 200
    assert b"underlyings" in body


# ── DEC-LAKE-CENSUS-NONBLOCKING-001: the census must never starve the threadpool ──


class _HeldScan:
    """A ``scan_lake_invariants`` stand-in: ticks progress, then blocks until released.

    Mirrors the production callable's shape: it receives the holder's
    ``per_file_pause`` progress callback and fires it three times BEFORE
    blocking (so the route's honest ``scan_progress.files_scanned`` counter is
    observable mid-scan), records the executing thread's name (the dedicated
    daemon thread vs an AnyIO worker is load-bearing evidence), and returns a
    clean empty report once released so the REAL census fold runs after.
    """

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self.thread_names: list[str] = []
        self._lock = threading.Lock()

    def __call__(self, *args: object, **kwargs: object) -> object:
        with self._lock:
            self.calls += 1
            self.thread_names.append(threading.current_thread().name)
        pause = kwargs.get("per_file_pause")
        if callable(pause):
            for _ in range(3):
                pause()
        self.entered.set()
        self.release.wait(timeout=60.0)
        from brutex.data.lake_invariants import LakeScanReport

        return LakeScanReport(
            findings=(),
            files_scanned=3,
            files_skipped=0,
            series_scanned=0,
            option_files_scanned=0,
            option_files_skipped=0,
            duration_s=0.0,
        )


def _timed_get(port: int, path: str, timeout: float) -> tuple[float, int | None, bytes | None]:
    """GET with wall-clock timing; a timeout/failure returns (elapsed, None, None)."""
    t0 = time.perf_counter()
    try:
        status, body = _get(port, path, timeout=timeout)
    except Exception:  # pylint: disable=broad-except — a starved route times out; the elapsed IS the evidence
        return time.perf_counter() - t0, None, None
    return time.perf_counter() - t0, status, body


def test_lake_census_scan_never_starves_the_shared_threadpool(
    server: _RealServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DEC-LAKE-CENSUS-NONBLOCKING-001 — the exact frozen-probe repro, as a regression.

    PRE-FIX wedge mechanism (reproduced on a real uvicorn over the operator's
    2.14M-file lake): the census route offloaded the O(files)
    ``scan_lake_invariants`` walk to ``run_in_threadpool`` (the loop stayed
    free — #1316/#1302's offload was real), but ``SummaryCache``'s COLD
    single-flight parked every concurrent poller in an UNBOUNDED
    ``decision.wait()`` ON a threadpool worker, and ``universe.html`` polls the
    route every 15 s with no in-flight guard — so ~40 stacked pollers exhausted
    the shared default AnyIO limiter and EVERY offloaded route (the whole data
    surface incl. /db) froze until the 26-minute scan completed. Measured
    during the repro: /api/v1/state (pure async) = 5 ms while
    /api/v1/universe/summary and the /db datasets discovery were FROZEN >10 s.

    POST-FIX contract (all four measured here, asserted after collection so a
    pre-fix run reports every freeze):

    (i)   GET lake-uniqueness answers <100 ms with ``scan_in_progress=true``
          while the scan is held (O(1) snapshot — never a blocking wait);
    (ii)  after 45 additional stacked GETs, /api/v1/universe/summary AND
          /api/v1/db/candles/datasets still answer <100 ms (pre-fix: frozen —
          threadpool-token starvation);
    (iii) releasing the scan lands the verdict and exactly ONE scan ran;
    (iv)  the honest progress counter is monotone non-decreasing, and the scan
          ran on the DEDICATED census daemon thread, never an AnyIO worker.
    """
    scan = _HeldScan()
    monkeypatch.setattr("brutex.dashboard.app.scan_lake_invariants", scan)
    census = "/api/v1/universe/lake-uniqueness"

    # Warm the server (connection + route table) so timings measure the routes.
    _get(server.port, "/api/v1/state", timeout=10.0)

    # (i) the first poll arms the scan; it must return immediately, not wait.
    first: dict[str, Any] = {}

    def _first_poll() -> None:
        first["elapsed"], first["status"], first["body"] = _timed_get(server.port, census, 8.0)

    first_thread = threading.Thread(target=_first_poll, daemon=True)
    first_thread.start()
    assert scan.entered.wait(timeout=10.0), "the census scan was never entered"

    # A snapshot poll while the scan is held — honest warming state, O(1).
    snap_elapsed, snap_status, snap_body = _timed_get(server.port, census, 8.0)

    # (ii) 45 stacked polls — the pre-fix wedge parked each on a threadpool token.
    stacked: list[threading.Thread] = []
    for _ in range(45):
        t = threading.Thread(target=lambda: _timed_get(server.port, census, 25.0), daemon=True)
        t.start()
        stacked.append(t)
    time.sleep(1.0)  # let the stack land server-side

    summary_elapsed, summary_status, _ = _timed_get(server.port, "/api/v1/universe/summary", 6.0)
    datasets_elapsed, datasets_status, _ = _timed_get(
        server.port, "/api/v1/db/candles/datasets", 6.0
    )

    # (iv) two progress samples while the scan is still held.
    _, _, sample1 = _timed_get(server.port, census, 8.0)
    _, _, sample2 = _timed_get(server.port, census, 8.0)

    scan.release.set()
    first_thread.join(timeout=15.0)
    for t in stacked:
        t.join(timeout=30.0)

    # (iii) the verdict lands after release — poll briefly for the daemon store.
    verdict: dict[str, Any] | None = None
    deadline = time.time() + 10.0
    while time.time() < deadline:
        _, status, body = _timed_get(server.port, census, 8.0)
        if status == 200 and body is not None:
            candidate = json.loads(body)
            if "findings_total" in candidate:
                verdict = candidate
                break
        time.sleep(0.1)

    # ── assertions (collected first so a pre-fix run reports every freeze) ──
    assert summary_status == 200 and summary_elapsed < 0.1, (
        f"/api/v1/universe/summary took {summary_elapsed * 1000:.0f}ms "
        f"(status={summary_status}) while the census scan was held + 45 polls "
        "stacked — threadpool-token starvation (the pre-fix wedge)."
    )
    assert datasets_status == 200 and datasets_elapsed < 0.1, (
        f"/api/v1/db/candles/datasets took {datasets_elapsed * 1000:.0f}ms "
        f"(status={datasets_status}) while the census scan was held + 45 polls "
        "stacked — threadpool-token starvation (the pre-fix wedge)."
    )
    assert first.get("status") == 200 and first["elapsed"] < 0.1, (
        f"the scan-arming census poll took {first['elapsed'] * 1000:.0f}ms "
        f"(status={first.get('status')}) — it must snapshot, never wait."
    )
    assert snap_status == 200 and snap_elapsed < 0.1, (
        f"a census poll during the held scan took {snap_elapsed * 1000:.0f}ms "
        f"(status={snap_status}) — it must snapshot, never wait."
    )
    snap = json.loads(snap_body or b"{}")
    assert snap.get("scan_in_progress") is True
    assert "findings_total" not in snap, "no verdict may be fabricated mid-first-scan"
    p1 = json.loads(sample1 or b"{}").get("scan_progress") or {}
    p2 = json.loads(sample2 or b"{}").get("scan_progress") or {}
    assert p1.get("files_scanned") == 3  # the stub ticked the counter 3 times
    assert p2.get("files_scanned", 0) >= p1.get("files_scanned", 0)  # monotone
    assert verdict is not None, "the verdict never landed after the scan released"
    assert verdict["findings_total"] == 0
    assert scan.calls == 1, f"expected exactly ONE scan, saw {scan.calls}"
    assert scan.thread_names == ["brutex-lake-census"], (
        f"the scan must run on the dedicated census daemon thread, saw {scan.thread_names}"
    )
