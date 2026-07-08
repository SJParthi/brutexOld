"""Read-path wedge isolation (DEC-DASHBOARD-READPATH-WEDGE-001).

Forensically proven on the operator's 2026-07-08 live pull log: during the
write-heavy options phase the dashboard's page-serving path HARD-WEDGED at
05:02:52Z and stayed frozen ~37 minutes while the pull itself stayed healthy.
Root: the heavy full-lake read offloads (the completeness SPOT scan, the
``/coverage/windows`` ledger read, the mapping-integrity reconciliation) each
wedged for tens of seconds on the write-bound external SSD, and — because plain
``run_in_threadpool`` routes EVERY offload through ONE process-wide 40-token
AnyIO limiter — enough of them stacked to drain that shared pool, so every
threadpool-backed poll route then starved for a worker token.

The fix routes those heavy reads through a dedicated bounded
:class:`~brutex.dashboard.lake_offload.LakeOffloadPool` whose limiter is DISJOINT
from the default pool. The oracle below drives it DIRECTLY: inject a 1-token lake
pool AND shrink the default AnyIO limiter to 1, wedge a heavy read (it holds the
lake pool's only token AND a second heavy read confirms the lake pool is
saturated), then prove the three poll routes — ``/api/v1/state``,
``/api/v1/pull/rate``, ``/api/v1/universe/pull-progress`` — STILL answer within
budget. On the pre-fix code the wedged heavy read holds the SHARED (here: 1-token
default) pool's only token, so ``/api/v1/pull/rate`` (which offloads its bridge
read onto the default pool) can never get a worker and the poll times out — the
test fails (verified by reverting the route body onto ``run_in_threadpool``).

The steady-state pin asserts a healthy lake-pool serve returns the offloaded
payload byte-identically (a spy over the pool).

Skipped cleanly without the ``[ui]`` extra (matches the sibling dashboard suites).
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Generator
from pathlib import Path
from typing import Any

import anyio.to_thread
import pytest

pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")
pytest.importorskip("httpx", reason="async ASGI client needs httpx")

import httpx
from brutex.dashboard.app import DashboardState, build_app
from brutex.dashboard.lake_offload import LakeOffloadPool
from brutex.data.instrument_store import InstrumentStore
from httpx import ASGITransport

#: Upper bound the stubbed heavy read blocks (released early on pass).
_WEDGE_HOLD_S: float = 10.0

#: Budget for a poll route while a heavy read is wedged. A free pool answers in
#: single-digit ms; a starved pool waits out the whole hold (10x over budget).
_POLL_BUDGET_S: float = 3.0


@pytest.fixture
def store(tmp_path: Path) -> Generator[InstrumentStore]:
    lake_root = tmp_path / "lake"
    lake_root.mkdir()
    s = InstrumentStore.open(lake_root / "instruments.duckdb")
    yield s
    s.close()


def _asgi_client(app: Any) -> httpx.AsyncClient:
    """An in-process async client for *app* (no real socket)."""
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1")


def test_wedged_heavy_read_never_starves_the_poll_routes(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wedged heavy lake read leaves state / pull-rate / pull-progress live.

    Fails on the pre-fix route (the heavy read shared the default pool; wedging
    it drained the tokens the poll routes need).
    """
    entered = threading.Event()
    release = threading.Event()

    def _blocking_window_payload(*_args: object, **_kwargs: object) -> dict[str, object]:
        entered.set()
        release.wait(timeout=_WEDGE_HOLD_S)
        return {"source": "stub", "count": 0, "windows": []}

    # The heavy read the `/coverage/windows` route offloads through the lake pool.
    monkeypatch.setattr("brutex.dashboard.db_view.build_window_payload", _blocking_window_payload)

    # A 1-token lake pool: ONE wedged read saturates the whole heavy-read budget.
    app = build_app(DashboardState(), instrument_store=store, lake_offload=LakeOffloadPool(limit=1))

    async def scenario() -> None:
        # Shrink the DEFAULT AnyIO thread limiter to 1 so a pre-fix heavy read
        # (which would run on THIS pool) reproduces the shared-pool starvation
        # cheaply. The fix keeps heavy reads on the disjoint lake pool, so this
        # default budget stays free for the poll routes.
        anyio.to_thread.current_default_thread_limiter().total_tokens = 1

        async with _asgi_client(app) as client:
            # Wedge a heavy read: it acquires the lake pool's only token and blocks.
            wedged = asyncio.ensure_future(client.get("/api/v1/coverage/windows"))
            assert await asyncio.to_thread(entered.wait, 5.0), "heavy read never entered"

            # A SECOND heavy read confirms the lake pool is SATURATED (its handler
            # suspends awaiting the held token — it must NOT finish within budget).
            saturating = asyncio.ensure_future(client.get("/api/v1/coverage/windows"))
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(saturating), timeout=0.5)

            # The poll routes must answer within budget despite the saturated lake pool.
            for path in (
                "/api/v1/state",
                "/api/v1/pull/rate",
                "/api/v1/universe/pull-progress",
            ):
                resp = await asyncio.wait_for(client.get(path), timeout=_POLL_BUDGET_S)
                assert resp.status_code == 200, f"{path} did not answer while a heavy read wedged"

            # Release the wedge; both heavy reads drain and return the real payload.
            release.set()
            first = await asyncio.wait_for(wedged, timeout=_POLL_BUDGET_S)
            second = await asyncio.wait_for(saturating, timeout=_POLL_BUDGET_S)
            assert first.status_code == 200
            assert first.json()["source"] == "stub"
            assert second.status_code == 200

    asyncio.run(scenario())


def test_wedged_coverage_options_read_never_starves_the_poll_routes(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wedged ``/api/v1/coverage/options`` summarize leaves the poll routes live.

    The hostile-review MAJOR (asymmetric isolation): this route's
    ``summarize_option_datasets`` — the self-described "O(files) full-lake
    aggregate", polled every 4 s by coverage.html and the literal twin of the
    ALREADY-moved ``/coverage/spot`` scan — had been left on the shared default
    ``run_in_threadpool`` pool, so wedging it re-opened the exact original drain
    vector the fix closed for the moved routes. This oracle wedges THROUGH the
    route (a hanging summarize seam) and proves the three poll routes still
    answer — it fails with the route body reverted onto ``run_in_threadpool``
    (verified).
    """
    entered = threading.Event()
    release = threading.Event()

    def _blocking_summarize(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
        entered.set()
        release.wait(timeout=_WEDGE_HOLD_S)
        return [
            {
                "underlying": "STUB",
                "exchange": "NSE",
                "series": 1,
                "bars": 0,
                "first_date": None,
                "last_date": None,
            }
        ]

    # app.py imports the name directly (`from ...db_view import summarize_option_datasets`),
    # so patch the BOUND name in brutex.dashboard.app — the seam the route reads.
    monkeypatch.setattr("brutex.dashboard.app.summarize_option_datasets", _blocking_summarize)

    # A 1-token lake pool: the ONE wedged options summarize saturates the whole
    # heavy-read budget.
    app = build_app(DashboardState(), instrument_store=store, lake_offload=LakeOffloadPool(limit=1))

    async def scenario() -> None:
        # Shrink the DEFAULT AnyIO thread limiter to 1 so the pre-fix placement
        # (this route on the shared default pool) reproduces the shared-pool
        # starvation cheaply; the fixed route holds the LAKE pool's token instead.
        anyio.to_thread.current_default_thread_limiter().total_tokens = 1

        async with _asgi_client(app) as client:
            # Wedge the options coverage read: it acquires the lake pool's only
            # token and blocks inside the summarize recompute.
            wedged = asyncio.ensure_future(client.get("/api/v1/coverage/options"))
            assert await asyncio.to_thread(entered.wait, 5.0), "options summarize never entered"

            # A SECOND lake-pool route confirms the lake pool is SATURATED (its
            # offload suspends awaiting the held token — never finishes in budget).
            saturating = asyncio.ensure_future(client.get("/api/v1/coverage/options"))
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(saturating), timeout=0.5)

            # The poll routes must answer within budget despite the saturated lake pool.
            for path in (
                "/api/v1/state",
                "/api/v1/pull/rate",
                "/api/v1/universe/pull-progress",
            ):
                resp = await asyncio.wait_for(client.get(path), timeout=_POLL_BUDGET_S)
                assert resp.status_code == 200, f"{path} did not answer while a heavy read wedged"

            # Release the wedge; both requests drain with the stub-derived payload
            # (the second is a cache HIT of the entry the first stored).
            release.set()
            first = await asyncio.wait_for(wedged, timeout=_POLL_BUDGET_S)
            second = await asyncio.wait_for(saturating, timeout=_POLL_BUDGET_S)
            assert first.status_code == 200
            assert first.json()["rows"][0]["underlying"] == "STUB"
            assert second.status_code == 200
            assert second.json()["rows"][0]["underlying"] == "STUB"

    asyncio.run(scenario())


def test_healthy_lake_pool_serve_returns_the_payload_byte_identically(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Steady-state (spy-pinned): a healthy lake-pool serve passes the offload result verbatim.

    Proves the isolation is a transparent pass-through — the pool's ``run`` returns
    exactly what the offloaded closure produced, so no payload changes for the fix.
    """
    sentinel = {"source": "spy", "count": 3, "windows": [{"key": "K"}]}
    monkeypatch.setattr(
        "brutex.dashboard.db_view.build_window_payload",
        lambda *_a, **_k: sentinel,
    )

    calls: list[str] = []
    real_run = LakeOffloadPool.run

    async def _spy_run(
        self: LakeOffloadPool, func: object, *args: object, **kwargs: object
    ) -> object:
        calls.append(getattr(func, "__name__", type(func).__name__))
        return await real_run(self, func, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(LakeOffloadPool, "run", _spy_run)

    app = build_app(DashboardState(), instrument_store=store)

    async def scenario() -> None:
        async with _asgi_client(app) as client:
            resp = await client.get("/api/v1/coverage/windows")
            assert resp.status_code == 200
            assert resp.json() == sentinel

    asyncio.run(scenario())
    # The route DID go through the dedicated lake pool (not plain run_in_threadpool).
    assert calls, "the /coverage/windows route did not offload through the lake pool"
