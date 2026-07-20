"""Read-path wedge isolation — route-level pins (DEC-DASHBOARD-READPATH-WEDGE-001).

Forensically proven on the operator's 2026-07-08 live pull log: during the
write-heavy options phase the dashboard's page-serving path HARD-WEDGED at
05:02:52Z and stayed frozen ~37 minutes while the pull itself stayed healthy.
Root: the heavy full-lake read offloads (the completeness SPOT scan, the
``/coverage/windows`` ledger read, the mapping-integrity reconciliation) each
wedged for tens of seconds on the write-bound external SSD, and — because plain
``run_in_threadpool`` routes EVERY offload through ONE process-wide 40-token
AnyIO limiter — enough of them stacked to drain that shared pool, so every
threadpool-backed poll route then starved for a worker token.

The SERVER fix is the LANDED DEC-DASHBOARD-LAKE-IO-LIMITER-001 (MIGRATION.md
#1642, PR #1362): every lake-touching offload rides the app's OWN bounded
:class:`~brutex.dashboard.lake_offload.LakeOffload` limiter, DISJOINT from the
default pool. (This branch's original ``LakeOffloadPool`` server half was
SUPERSEDED by #1642 and dropped — MIGRATION.md #1712.) The oracles below are
the retained ROUTE-LEVEL pins, retargeted at the landed limiter: inject a
1-token ``LakeOffload`` through the #1642 ``build_app(lake_offload=)`` seam AND
shrink the default AnyIO limiter to 1, wedge a heavy read (it holds the lake
limiter's only token AND a second heavy read confirms the limiter is
saturated), then prove the default-pool poll routes — ``/api/v1/state`` (pure
in-memory) and ``/api/v1/pull/rate`` (the #1522 deadline-guarded bridge memo on
the DEFAULT pool) — STILL answer within budget. With every site on the shared
(here: 1-token default) pool, the wedged heavy read holds the only token, so
``/api/v1/pull/rate`` can never get a worker and the poll times out.

Retargeting delta (honest, MIGRATION.md #1712): the original assertion that
``/api/v1/universe/pull-progress`` answers under a saturated lake limiter is
REMOVED — on #1642's landed design that route's ``_phases`` fold rides the
LAKE limiter BY DECISION (a lake-touching ledger fold), so pinning its
liveness there would pin the wrong contract. Distinct value vs #1642's own
suite (class units + the real-uvicorn 44-request ``/db/fno/underlyings``
drive): the ASGI-level per-route pins of the TWO coverage read seams, the
1-token injection-seam drive, and the limiter-saturation queue proof.

The steady-state pin asserts a healthy lake-limiter serve returns the
offloaded payload byte-identically (a spy subclass over the limiter).

Skipped cleanly without the ``[ui]`` extra (matches the sibling dashboard suites).
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Generator
from pathlib import Path
from typing import Any, TypeVar

import anyio.to_thread
import pytest

pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")
pytest.importorskip("httpx", reason="async ASGI client needs httpx")

import httpx
from brutex.dashboard.app import DashboardState, build_app
from brutex.dashboard.lake_offload import LakeOffload
from brutex.data.instrument_store import InstrumentStore
from httpx import ASGITransport

_T = TypeVar("_T")

#: Upper bound the stubbed heavy read blocks (released early on pass).
_WEDGE_HOLD_S: float = 10.0

#: Budget for a poll route while a heavy read is wedged. A free pool answers in
#: single-digit ms; a starved pool waits out the whole hold (10x over budget).
_POLL_BUDGET_S: float = 3.0

#: The default-pool poll routes that must stay live while the LAKE limiter is
#: saturated. ``/api/v1/universe/pull-progress`` is deliberately NOT here — on
#: the landed #1642 design its ``_phases`` fold rides the lake limiter.
_DEFAULT_POOL_POLL_ROUTES: tuple[str, ...] = ("/api/v1/state", "/api/v1/pull/rate")


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
    """A wedged heavy lake read leaves the default-pool poll routes live.

    Fails with the heavy read on the shared default pool (wedging it drains
    the tokens the poll routes need — the pre-#1642 placement).
    """
    entered = threading.Event()
    release = threading.Event()

    def _blocking_window_payload(*_args: object, **_kwargs: object) -> dict[str, object]:
        entered.set()
        release.wait(timeout=_WEDGE_HOLD_S)
        return {"source": "stub", "count": 0, "windows": []}

    # The heavy read the `/coverage/windows` route offloads through the lake limiter.
    monkeypatch.setattr("brutex.dashboard.db_view.build_window_payload", _blocking_window_payload)

    # A 1-token lake limiter (the #1642 injection seam): ONE wedged read
    # saturates the whole heavy-read budget.
    app = build_app(DashboardState(), instrument_store=store, lake_offload=LakeOffload(tokens=1))

    async def scenario() -> None:
        # Shrink the DEFAULT AnyIO thread limiter to 1 so the pre-#1642 heavy
        # read placement (on THIS pool) reproduces the shared-pool starvation
        # cheaply. The landed fix keeps heavy reads on the disjoint lake
        # limiter, so this default budget stays free for the poll routes.
        anyio.to_thread.current_default_thread_limiter().total_tokens = 1

        async with _asgi_client(app) as client:
            # Wedge a heavy read: it acquires the lake limiter's only token and blocks.
            wedged = asyncio.ensure_future(client.get("/api/v1/coverage/windows"))
            assert await asyncio.to_thread(entered.wait, 5.0), "heavy read never entered"

            # A SECOND heavy read confirms the lake limiter is SATURATED (its handler
            # suspends awaiting the held token — it must NOT finish within budget).
            saturating = asyncio.ensure_future(client.get("/api/v1/coverage/windows"))
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(saturating), timeout=0.5)

            # The poll routes must answer within budget despite the saturated limiter.
            for path in _DEFAULT_POOL_POLL_ROUTES:
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

    The per-route pin of the second coverage read seam: this route's
    ``summarize_option_datasets`` — the self-described "O(files) full-lake
    aggregate", polled every 4 s by coverage.html and the literal twin of the
    ``/coverage/spot`` scan — rides the LAKE limiter on the landed #1642
    design. This oracle wedges THROUGH the route (a hanging summarize seam)
    and proves the default-pool poll routes still answer.
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

    # A 1-token lake limiter: the ONE wedged options summarize saturates the
    # whole heavy-read budget.
    app = build_app(DashboardState(), instrument_store=store, lake_offload=LakeOffload(tokens=1))

    async def scenario() -> None:
        # Shrink the DEFAULT AnyIO thread limiter to 1 so the pre-#1642
        # placement (this route on the shared default pool) reproduces the
        # shared-pool starvation cheaply; the landed route holds the LAKE
        # limiter's token instead.
        anyio.to_thread.current_default_thread_limiter().total_tokens = 1

        async with _asgi_client(app) as client:
            # Wedge the options coverage read: it acquires the lake limiter's
            # only token and blocks inside the summarize recompute.
            wedged = asyncio.ensure_future(client.get("/api/v1/coverage/options"))
            assert await asyncio.to_thread(entered.wait, 5.0), "options summarize never entered"

            # A SECOND lake-limiter route confirms the limiter is SATURATED (its
            # offload suspends awaiting the held token — never finishes in budget).
            saturating = asyncio.ensure_future(client.get("/api/v1/coverage/options"))
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(saturating), timeout=0.5)

            # The poll routes must answer within budget despite the saturated limiter.
            for path in _DEFAULT_POOL_POLL_ROUTES:
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


class _SpyOffload(LakeOffload):
    """A :class:`LakeOffload` recording every offloaded callable's name."""

    def __init__(self) -> None:
        super().__init__(tokens=2)
        self.calls: list[str] = []

    async def __call__(self, func: Callable[..., _T], /, *args: object, **kwargs: object) -> _T:
        self.calls.append(getattr(func, "__name__", type(func).__name__))
        return await super().__call__(func, *args, **kwargs)


def test_healthy_lake_limiter_serve_returns_the_payload_byte_identically(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Steady-state (spy-pinned): a healthy lake-limiter serve passes the offload result verbatim.

    Proves the isolation is a transparent pass-through — the limiter's dispatch
    returns exactly what the offloaded closure produced (no payload change) —
    and that the ``/coverage/windows`` route DOES ride the injected lake
    limiter, not plain ``run_in_threadpool``.
    """
    sentinel = {"source": "spy", "count": 3, "windows": [{"key": "K"}]}
    monkeypatch.setattr(
        "brutex.dashboard.db_view.build_window_payload",
        lambda *_a, **_k: sentinel,
    )

    spy = _SpyOffload()
    app = build_app(DashboardState(), instrument_store=store, lake_offload=spy)

    async def scenario() -> None:
        async with _asgi_client(app) as client:
            resp = await client.get("/api/v1/coverage/windows")
            assert resp.status_code == 200
            assert resp.json() == sentinel

    asyncio.run(scenario())
    # The route DID go through the injected lake limiter (not plain run_in_threadpool).
    assert spy.calls, "the /coverage/windows route did not offload through the lake limiter"


def test_temporal_baseline_equity_routes_ride_the_lake_limiter(
    store: InstrumentStore,
) -> None:
    """The #1851 R3 adoption pin: the three S2-club lake-read dispatches —
    ``/api/v1/sweep/temporal`` (the per-period drill), ``/api/v1/oneclick/baseline``
    (the stored buy-&-hold series) and ``/api/v1/oneclick/equity`` (the
    club-devolved main-parity restore) — ride the injected lake limiter, not
    plain ``run_in_threadpool``. Artifact-free lake: each serves its honest
    absent/awaiting degrade at 200 THROUGH the offload (never a 500)."""
    spy = _SpyOffload()
    app = build_app(DashboardState(), instrument_store=store, lake_offload=spy)
    rid = "a" * 64

    async def scenario() -> None:
        async with _asgi_client(app) as client:
            for url, params in (
                ("/api/v1/sweep/temporal", {"run_id": rid, "horizon": "quarter"}),
                ("/api/v1/oneclick/baseline", {"run_id": rid}),
                ("/api/v1/oneclick/equity", {"run_id": rid}),
            ):
                resp = await client.get(url, params=params)
                assert resp.status_code == 200, (url, resp.status_code)

    asyncio.run(scenario())
    assert spy.calls.count("_resolve") == 3, spy.calls
