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
