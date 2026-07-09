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

import socket
import threading
import time
import urllib.error
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


# ── DEC-SWEEP-PROGRESS-NONBLOCK-CACHE-001 — the /api/v1/sweep-progress file read ──


@pytest.fixture
def sweep_progress_server(
    store: InstrumentStore, tmp_path: Path
) -> Generator[tuple[_RealServer, Path]]:
    """A real server whose state carries a REAL sweep-progress file bridge."""
    import json

    state = DashboardState()
    progress_file = tmp_path / "sweeps" / "progress" / "sweep-progress.json"
    progress_file.parent.mkdir(parents=True)
    progress_file.write_text(
        json.dumps({"state": "running", "running": True, "enumerated": 999}),
        encoding="utf-8",
    )
    state.sweep_progress_path = progress_file
    srv = _RealServer(build_app(state, instrument_store=store), _free_port())
    srv.start()
    try:
        yield srv, progress_file
    finally:
        srv.stop()


def test_sweep_progress_stalled_read_keeps_loop_free_and_is_bounded(
    sweep_progress_server: tuple[_RealServer, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stalled progress-file read must neither freeze the loop nor the route.

    The measured M1 repro: ``/api/v1/sweep-progress`` ran ``path.exists()`` +
    ``read_progress(path)`` ON the uvicorn event loop, so a 1.5 s stalled read
    moved a concurrent ``/api/v1/state`` poll 9.1 ms → 1305.4 ms. Post-fix the
    view runs on the threadpool AND the file arm rides the deadline-guarded
    ``SweepProgressCache`` (0.5 s refresh bound, non-blocking single-flight),
    so BOTH hold: (a) a concurrent ``/api/v1/state`` stays fast, and (b) the
    sweep-progress response ITSELF is bounded — the wedged read is abandoned
    to the guard's daemon and the route serves the honest fall-through (idle
    on a cold wedge; last-known labelled ``unverified`` once warm). Once the
    volume recovers, the late-returning daemon self-heals the memo and the
    next poll serves the real file payload, provenance-stamped.
    """
    server, _progress_file = sweep_progress_server
    entered = threading.Event()
    release = threading.Event()

    def _stalled_read(path: Path) -> dict[str, object]:
        entered.set()
        release.wait(timeout=_STALL_S)
        return {"state": "running", "running": True, "enumerated": 999}

    monkeypatch.setattr("brutex.engine.sweep_supervisor.read_progress", _stalled_read)

    # TRULY CONCURRENT pair (review fold): the sweep-progress request AND an
    # /api/v1/state poll are BOTH in flight during the first wedged OWNER
    # window. This is the discriminating shape — the 0.5 s deadline alone
    # would bound a SEQUENTIAL sweep-progress probe even if the view ran ON
    # the loop; only the run_in_threadpool offload keeps a concurrent request
    # fast while the owner is inside its deadline wait.
    timings: dict[str, Any] = {}

    def _timed_get(name: str, path: str) -> None:
        start = time.perf_counter()
        try:
            status, body = _get(server.port, path, timeout=_STALL_S + 2.0)
            timings[name] = (status, time.perf_counter() - start, body)
        except Exception as exc:  # pragma: no cover - only on a hung/failed offload
            timings[name] = ("error", repr(exc), b"")

    progress_thread = threading.Thread(
        target=_timed_get, args=("progress", "/api/v1/sweep-progress"), daemon=True
    )
    state_thread = threading.Thread(target=_timed_get, args=("state", "/api/v1/state"), daemon=True)
    try:
        progress_thread.start()
        assert entered.wait(timeout=_STALL_S + 2.0), "the stalled read was never entered"
        # The owner is now INSIDE its deadline window on the wedged read —
        # fire the concurrent poll while both are in flight.
        state_thread.start()
        progress_thread.join(timeout=_STALL_S + 4.0)
        state_thread.join(timeout=_STALL_S + 4.0)
        assert not progress_thread.is_alive() and not state_thread.is_alive()
        # (b) the route itself is BOUNDED while the read is wedged: the owner
        # waits at most the 0.5 s refresh deadline, then serves the honest
        # cold fall-through (idle — nothing memoised yet), never the stall.
        status, blocked_elapsed, body = timings["progress"]
        assert status == 200
        assert blocked_elapsed < _POLL_BUDGET_S, (
            f"/api/v1/sweep-progress took {blocked_elapsed * 1000:.0f}ms on a wedged "
            "read — the deadline-guarded cache bound is broken (regression)."
        )
        assert b'"idle"' in body  # honest cold-wedge fall-through, never fabricated
        # (a) the CONCURRENT poll stayed fast while the owner window was open.
        status, elapsed, _ = timings["state"]
        assert status == 200
        assert elapsed < _POLL_BUDGET_S, (
            f"/api/v1/state took {elapsed * 1000:.0f}ms while the sweep-progress read "
            "stalled — the blocking read is running ON the uvicorn event loop "
            "(regression)."
        )
    finally:
        release.set()
        progress_thread.join(timeout=10.0)
        state_thread.join(timeout=10.0)
    # Recovery: the daemon's late return self-heals the memo; the next polls
    # (past the 1 s TTL) serve the REAL file payload, provenance-stamped.
    deadline = time.time() + 10.0
    healed: dict[str, object] | None = None
    while time.time() < deadline:
        status, body = _get(server.port, "/api/v1/sweep-progress", timeout=5.0)
        assert status == 200
        parsed = __import__("json").loads(body)
        if parsed.get("enumerated") == 999:
            healed = parsed
            break
        time.sleep(0.2)
    assert healed is not None, "the memo never self-healed after the volume recovered"
    # "unverified" is admitted too (review fold): the healed payload can be
    # served from the memo in the microsecond window where the late daemon has
    # committed but a fresh owner refresh is still in flight — stamped
    # honestly, not a failure.
    assert healed["progress_source"] in ("live", "cached", "unverified")


# ── DEC-DASHBOARD-EVENTLOOP-OFFLOAD-003 — the pull-LAUNCH routes' resolver reads ──


def _post(port: int, path: str, timeout: float) -> tuple[int, bytes]:
    """Blocking POST via stdlib urllib (called from worker threads); 4xx returned, not raised."""
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=b"", method="POST")
    try:
        with urllib.request.urlopen(  # noqa: S310 — fixed local http://127.0.0.1 test URL
            request, timeout=timeout
        ) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:  # a 400/503 refusal IS the expected answer
        return exc.code, exc.read()


def _assert_launch_resolution_off_loop(
    server: _RealServer,
    monkeypatch: pytest.MonkeyPatch,
    *,
    patch_target: str,
    stall_return: object,
    launch_path: str,
    expect_status: int,
    expect_detail: bytes,
) -> None:
    """Stall a launch route's resolver read, prove /api/v1/state stays free.

    The M16 class: the POST pull-launch routes resolved their selection
    (RegistryStore.open / _resolve_historical_contracts / _select_bulk_targets /
    store.get / reference_only_security_ids / _underlying_kind_map) ON the
    uvicorn event loop — a slow/stalled lake volume froze every concurrent poll
    the moment the operator clicked a Pull button. Post-fix the resolution runs
    on a threadpool worker: the launch request itself still waits for its
    resolver (no deadline claimed — validation semantics preserved), but a
    concurrent ``/api/v1/state`` poll answers fast, and the eventually-released
    launch completes with its EXACT pre-offload validation answer.
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
            blocked["status"], blocked["body"] = _post(server.port, launch_path, timeout=15.0)
        except Exception as exc:  # pragma: no cover - only on a hung/failed offload
            blocked["error"] = repr(exc)

    thread = threading.Thread(target=_fire_blocked, daemon=True)
    thread.start()
    try:
        assert entered.wait(timeout=_STALL_S + 2.0), "the stalled resolver was never entered"
        start = time.perf_counter()
        status, _ = _get(server.port, "/api/v1/state", timeout=_STALL_S + 2.0)
        elapsed = time.perf_counter() - start
        assert status == 200
        assert elapsed < _POLL_BUDGET_S, (
            f"/api/v1/state took {elapsed * 1000:.0f}ms while {launch_path} resolved — "
            "the launch resolver is running ON the uvicorn event loop (regression)."
        )
    finally:
        release.set()
        thread.join(timeout=10.0)
    assert not thread.is_alive(), f"the blocked {launch_path} request never completed"
    assert "error" not in blocked, f"blocked request failed: {blocked.get('error')}"
    # Behaviour parity: the released launch answers EXACTLY as before the offload.
    assert blocked["status"] == expect_status, blocked
    assert expect_detail in bytes(blocked["body"]), blocked


def test_pull_single_symbol_lookup_off_loop_real_server(
    server: _RealServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /universe/pull: the store.get lookup must resolve on a worker."""
    _assert_launch_resolution_off_loop(
        server,
        monkeypatch,
        patch_target="brutex.data.instrument_store.InstrumentStore.get",
        stall_return=None,
        launch_path="/api/v1/universe/pull?symbol=GHOST&dry_run=true",
        expect_status=400,
        expect_detail=b"unknown security_id",
    )


def test_pull_all_selection_off_loop_real_server(
    server: _RealServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /universe/pull-all: the selection resolution must run on a worker."""
    _assert_launch_resolution_off_loop(
        server,
        monkeypatch,
        patch_target="brutex.data.instrument_store.InstrumentStore.get",
        stall_return=None,
        launch_path="/api/v1/universe/pull-all?symbol=GHOST&dry_run=true",
        expect_status=400,
        expect_detail=b"unknown security_id",
    )


def test_pull_expiries_contracts_selection_off_loop_real_server(
    server: _RealServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /universe/pull-expiries-contracts: selection + REF set on a worker."""
    _assert_launch_resolution_off_loop(
        server,
        monkeypatch,
        patch_target="brutex.data.instrument_store.InstrumentStore.get",
        stall_return=None,
        launch_path="/api/v1/universe/pull-expiries-contracts?symbol=GHOST&dry_run=true",
        expect_status=400,
        expect_detail=b"unknown security_id",
    )


def test_pull_historical_direct_selection_off_loop_real_server(
    server: _RealServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /universe/pull-historical: registry resolution must run on a worker.

    The WORST M16 offender: RegistryStore.open + _resolve_historical_contracts
    (a full list_contracts) + _underlying_kind_map all ran on the loop. The
    stub stalls the contract resolution and returns an empty match, so the
    released launch answers the EXACT pre-offload honest 400.
    """
    _assert_launch_resolution_off_loop(
        server,
        monkeypatch,
        patch_target="brutex.dashboard.app._resolve_historical_contracts",
        stall_return=[],
        launch_path="/api/v1/universe/pull-historical?contract=NSE-NIFTY-X&dry_run=true",
        expect_status=400,
        expect_detail=b"no cached contracts matched",
    )


def test_retry_failed_selection_off_loop_real_server(
    server: _RealServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /universe/retry-failed: resolve_pull_targets must run on a worker."""
    _assert_launch_resolution_off_loop(
        server,
        monkeypatch,
        patch_target="brutex.data.instrument_store.InstrumentStore.get",
        stall_return=None,
        launch_path="/api/v1/universe/retry-failed?symbols=GHOST&dry_run=true",
        expect_status=400,
        expect_detail=b"GHOST",
    )


# ── DEC-DASHBOARD-LAKE-IO-LIMITER-001 — a wedged lake must not starve the pool ──


def test_wedged_lake_offloads_do_not_starve_liveness_routes(
    server: _RealServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """44 wedged LAKE offloads must leave /api/v1/pull/rate + /api/v1/state fast.

    The measured M4 repro: all 59+ dashboard offload sites shared AnyIO's
    DEFAULT 40-token limiter and only the rate-bridge read was
    deadline-guarded, so 40 wedged lake reads pinned every token — the 41st
    offload queued 2701 ms and the deadline-guarded ``/api/v1/pull/rate``
    could not even START (its bound holds only GIVEN a free token — #1522's
    own stated envelope). Post-fix the lake reads ride the app's dedicated
    32-token ``LakeOffload`` limiter: 44 concurrent wedged lake requests pin
    at most 32 lake workers (the rest queue on the lake limiter — no thread
    pile-up), the DEFAULT pool stays free, and BOTH liveness probes answer
    fast. Pre-fix (every site on ``run_in_threadpool``) this test FAILS: the
    44 wedges exhaust the default pool and the /pull/rate probe waits out the
    whole stall.

    Budgets are generous for CI noise: the probes get 2.5 s against a 6 s
    stall — an unambiguous separation either way.
    """
    entered_lock = threading.Lock()
    entered_count = 0
    entered_reached = threading.Event()
    release = threading.Event()
    wedge_target = 32  # the lake limiter's full size — every token pinned

    def _stalled_revision(*_a: object, **_k: object) -> str:
        nonlocal entered_count
        with entered_lock:
            entered_count += 1
            if entered_count >= wedge_target:
                entered_reached.set()
        release.wait(timeout=6.0)
        return "rev-wedged-token"

    # The generation probe (`_summary_cache.generation` → lake_revision) runs
    # INSIDE the offloaded closure of /api/v1/db/fno/underlyings (#1622), and
    # OUTSIDE the cache lock (#1154) — so every concurrent request wedges its
    # OWN lake worker: the exact pool-exhaustion shape of the M4 repro.
    monkeypatch.setattr("brutex.dashboard.app.lake_revision", _stalled_revision)

    results: list[dict[str, Any]] = []
    threads: list[threading.Thread] = []

    def _fire(slot: dict[str, Any]) -> None:
        try:
            slot["status"], slot["body"] = _get(
                server.port, "/api/v1/db/fno/underlyings", timeout=30.0
            )
        except Exception as exc:  # pragma: no cover - only on a hung/failed offload
            slot["error"] = repr(exc)

    try:
        for _ in range(44):
            slot: dict[str, Any] = {}
            results.append(slot)
            thread = threading.Thread(target=_fire, args=(slot,), daemon=True)
            threads.append(thread)
            thread.start()
        assert entered_reached.wait(timeout=10.0), (
            f"only {entered_count} lake offloads entered the wedge — the lake "
            "limiter should admit exactly its token count"
        )
        # Every lake token is now pinned on a wedged read. The liveness routes
        # must still answer fast on the DEFAULT pool.
        for path in ("/api/v1/pull/rate", "/api/v1/state"):
            start = time.perf_counter()
            status, _ = _get(server.port, path, timeout=10.0)
            elapsed = time.perf_counter() - start
            assert status == 200
            assert elapsed < 2.5, (
                f"{path} took {elapsed * 1000:.0f}ms while 32 lake offloads were "
                "wedged — the lake reads are starving the default AnyIO pool "
                "(regression: the dedicated lake limiter is not binding)."
            )
        # The limiter BINDS: no more than its 32 tokens ever entered the wedge
        # while it was held (the remaining 12 queued on the lake limiter).
        with entered_lock:
            held_peak = entered_count
        assert held_peak <= wedge_target, (
            f"{held_peak} lake offloads ran concurrently — the 32-token lake "
            "limiter is not bounding the wedge"
        )
    finally:
        release.set()
        for thread in threads:
            thread.join(timeout=20.0)
    assert all(not t.is_alive() for t in threads), "wedged lake requests never completed"
    for slot in results:
        assert "error" not in slot, f"a wedged request failed: {slot.get('error')}"
        assert slot["status"] == 200
