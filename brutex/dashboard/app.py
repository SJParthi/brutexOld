"""Dashboard backend — state model + FastAPI app factory.

Implements ``FR-UI-006`` (real-time ingest dashboard) from
``REQUIREMENTS.md §4.11``.

## Design

The dashboard is **read-only** — it subscribes to the pipeline's
event stream and builds an in-memory :class:`DashboardState` snapshot.
Every HTTP / WebSocket response is derived from that snapshot.

FastAPI is imported lazily so importing this module in tests doesn't
require ``fastapi`` to be installed. The state model itself
(:class:`DashboardState`, :class:`PhaseSnapshot`) has zero external
dependencies and is fully unit-testable offline.

## Endpoints

* ``GET /``                 — HTML page (Jinja2 template)
* ``GET /api/v1/state``     — JSON snapshot of the dashboard state
* ``GET /api/v1/events``    — ring buffer of the last 500 events
* ``GET /metrics``          — Prometheus exporter passthrough
* ``WS /ws``                — 500ms WebSocket fan-out of state updates

## State lifecycle

The state is updated via pipeline event subscriptions —
:meth:`DashboardState.on_pipeline_event` is intended to be passed
to :class:`~brutex.orchestrator.pipeline.PhaseRunner` as a subscriber.
A second subscription point,
:meth:`DashboardState.on_telegram_stats`, is called periodically to
refresh the Telegram panel.
"""

from __future__ import annotations

import contextlib
import json
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import structlog
from pydantic import BaseModel

from brutex.dashboard.coverage_view import build_coverage_payload
from brutex.dashboard.db_view import sample_rows, table_summary
from brutex.dashboard.pull_controller import groww_creds_present, pull_candles
from brutex.dashboard.universe_query import list_underlyings, universe_summary
from brutex.orchestrator.pipeline import (
    PipelineEvent,
    PipelineEventKind,
)
from brutex.orchestrator.state import IngestPhase
from brutex.universe import derive_lake_underlyings

#: Module logger — every material dashboard action (especially a pull that
#: fails) lands in the same ``.brutex-logs/brutex.log`` JSONL the operator
#: reads, so a 400 is never a silent dead-end (DEC-PULL-ERROR-VISIBILITY-001).
_LOG = structlog.get_logger("brutex.dashboard")


class CredentialsIn(BaseModel):
    """Request body for ``POST /api/v1/credentials``.

    Defined at **module scope** (not inside :func:`build_app`) so FastAPI
    correctly resolves it as a request-BODY model. A function-local Pydantic
    model is not recognised as a body by FastAPI's parameter analysis — it
    is mis-classified as a query parameter (``loc: ["query", "body"]``),
    which made every POST return 422 (DEC-CREDS-422-FIX-001). The fields are
    accepted in the BODY (never as query params, which leak into access
    logs) and are passed straight to
    :func:`brutex.config.credentials.save_groww_credentials` — never logged.
    """

    api_key: str
    totp_secret: str


# ────────────────────────────────────────────────────────────────────────
# Snapshot dataclasses
# ────────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class PhaseSnapshot:
    """Per-phase snapshot shown on the dashboard."""

    phase: str  # IngestPhase.value
    status: str  # "pending" | "running" | "done" | "halted"
    total: int = 0
    done: int = 0
    success: int = 0
    no_data: int = 0
    quarantined: int = 0
    retried: int = 0
    started_at: str | None = None
    completed_at: str | None = None
    elapsed_seconds: float | None = None
    message: str | None = None  # populated on halt

    @property
    def percent(self) -> float:
        """Return progress percentage as float in [0.0, 100.0]."""
        if self.total <= 0:
            return 0.0
        return min(100.0, round(100.0 * self.done / self.total, 2))


@dataclass(slots=True)
class WorkerRow:
    """Per-worker activity row in the dashboard worker table."""

    worker_id: str
    status: str  # "idle" | "busy"
    current_task: str | None = None
    elapsed_seconds: float = 0.0
    last_heartbeat: str | None = None


@dataclass(slots=True)
class TelegramStatsPanel:
    """Telegram delivery counters shown in the dashboard sidebar."""

    enabled: bool = False
    sent: int = 0
    dropped_on_failure: int = 0
    dropped_on_dedupe: int = 0
    dropped_on_queue_full: int = 0
    dropped_on_disabled: int = 0


@dataclass(slots=True)
class RateLimitPanel:
    """Rate-limiter snapshot (current RPS, available tokens, recent 429s)."""

    current_rps: float = 0.0
    available_tokens: float = 0.0
    recent_429_count: int = 0


@dataclass(slots=True)
class SweepFunnelSnapshot:
    """One snapshot of the 9-filter survival pipeline.

    See ``docs/TRACK1_DASHBOARD_DESIGN.md §6.2`` for the schema contract
    and §6.4 for the incremental swap-out pattern that wires individual
    fields into the dashboard mockup one at a time.

    Every value is dynamic — the 33B / 12M / 1.2M / 84K / 2.4K / 410 /
    174 / 1 numbers in ``docs/dashboard/mockups/v29.html`` are PLACEHOLDER
    fallbacks until each field's ``data-bind`` attribute is wired (per
    spec §6.4 step 3) and the JS poller (per §6.4 step 4) replaces the
    inner text from a live ``SweepFunnelSnapshot``.

    Attributes
    ----------
    step_0_all_candidates
        Step 0 — total combos tested at the start of the funnel.
        At Day 1 this is ~12.6M; at Phase 10 it is the 730B target.
        The dashboard's 33B mockup value is the Phase-10 ceiling, not a
        hardcoded claim (see ``TRACK1_DASHBOARD_DESIGN.md §6.4 SCALABLE``).
    step_1_cheap_prefilter
        Step 1 — survivors of the cheap pre-filter (top-K composite).
    step_2_real_edge_test
        Step 2 — real-edge test (corrects for testing N billion ideas).
    step_3_unseen_data
        Step 3 — works on data it never saw (walk-forward splits).
    step_4_best_of_n
        Step 4 — best-of-N test.
    step_5_not_overfit
        Step 5 — not overfit (deflated Sharpe, PBO).
    step_6_multi_test
        Step 6 — multi-test correction (Bonferroni / White's reality check).
    step_7_three_windows
        Step 7 — 3 time-windows all profitable (rolling + anchored + expanding).
    step_8_sealed_lockbox
        Step 8 — 6-month sealed lockbox holdout.
    step_9_random_shuffle
        Step 9 — random shuffle test (label permutation).
    winners_top_n
        Final winners after all 9 filters (the chosen one(s) at the bottom).
    sweep_id
        Unique sweep identifier for audit trail.
    snapshot_at
        ISO-8601 timestamp the snapshot was taken (for staleness check
        in the JS poller — falls back to mockup value if older than 60s).
    """

    step_0_all_candidates: int = 0
    step_1_cheap_prefilter: int = 0
    step_2_real_edge_test: int = 0
    step_3_unseen_data: int = 0
    step_4_best_of_n: int = 0
    step_5_not_overfit: int = 0
    step_6_multi_test: int = 0
    step_7_three_windows: int = 0
    step_8_sealed_lockbox: int = 0
    step_9_random_shuffle: int = 0
    winners_top_n: int = 0
    sweep_id: str = ""
    snapshot_at: str = ""


# ────────────────────────────────────────────────────────────────────────
# DashboardState — the central in-memory model
# ────────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class DashboardState:
    """Thread-safe in-memory dashboard state.

    Listens to :class:`PipelineEvent` objects via
    :meth:`on_pipeline_event` and mutates its own fields under a
    lock. The FastAPI / WebSocket layer reads snapshots via
    :meth:`to_dict`.
    """

    run_id: str | None = None
    run_status: str = "idle"  # "idle" | "running" | "halted" | "complete"
    run_started_at: str | None = None
    run_completed_at: str | None = None
    run_elapsed_seconds: float | None = None
    phases: dict[str, PhaseSnapshot] = field(
        default_factory=lambda: {
            phase.value: PhaseSnapshot(phase=phase.value, status="pending") for phase in IngestPhase
        }
    )
    workers: dict[str, WorkerRow] = field(default_factory=dict)
    telegram: TelegramStatsPanel = field(default_factory=TelegramStatsPanel)
    rate_limit: RateLimitPanel = field(default_factory=RateLimitPanel)
    recent_events: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=500))
    # Sweep funnel snapshot — populated by the brute-force sweep
    # via :meth:`update_sweep_funnel`. ``None`` until first update;
    # the JS poller in v29.html falls back to placeholder mockup
    # values when this is None (PREVIEW DATA badge stays visible).
    # See ``TRACK1_DASHBOARD_DESIGN.md §6.4`` for the incremental
    # swap-out contract.
    sweep_funnel: SweepFunnelSnapshot | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # ── Event subscription entry point ──────────────────────────────

    def on_pipeline_event(self, event: PipelineEvent) -> None:
        """Apply a :class:`PipelineEvent` to the state.

        Intended to be passed as a subscriber callback to
        :class:`~brutex.orchestrator.pipeline.PhaseRunner`. This
        method is thread-safe — it acquires the state lock.
        """
        with self._lock:
            # Record the event in the ring buffer
            self.recent_events.append(
                {
                    "kind": event.kind,
                    "at": event.at.isoformat(),
                    "phase": event.phase.value if event.phase else None,
                    "item_id": event.item_id,
                    "item_state": (event.item_state.value if event.item_state else None),
                    "done": event.done,
                    "total": event.total,
                    "success": event.success,
                    "no_data": event.no_data,
                    "quarantined": event.quarantined,
                    "retried": event.retried,
                    "elapsed_seconds": event.elapsed_seconds,
                    "message": event.message,
                }
            )

            # Update run-level state
            if event.run_id is not None:
                self.run_id = event.run_id

            now_iso = event.at.isoformat()

            if event.kind == PipelineEventKind.RUN_START:
                self.run_status = "running"
                self.run_started_at = now_iso
            elif event.kind == PipelineEventKind.RUN_COMPLETE:
                self.run_status = "complete"
                self.run_completed_at = now_iso
                self.run_elapsed_seconds = event.elapsed_seconds

            # Update phase-level state
            if event.phase is not None:
                snapshot = self.phases.setdefault(
                    event.phase.value,
                    PhaseSnapshot(phase=event.phase.value, status="pending"),
                )

                if event.kind == PipelineEventKind.PHASE_START:
                    snapshot.status = "running"
                    snapshot.started_at = now_iso
                    if event.total is not None:
                        snapshot.total = event.total
                elif event.kind == PipelineEventKind.PHASE_PROGRESS:
                    if event.total is not None:
                        snapshot.total = event.total
                    if event.done is not None:
                        snapshot.done = event.done
                    if event.success is not None:
                        snapshot.success = event.success
                    if event.no_data is not None:
                        snapshot.no_data = event.no_data
                    if event.quarantined is not None:
                        snapshot.quarantined = event.quarantined
                elif event.kind == PipelineEventKind.PHASE_COMPLETE:
                    snapshot.status = "done"
                    snapshot.completed_at = now_iso
                    snapshot.elapsed_seconds = event.elapsed_seconds
                    if event.total is not None:
                        snapshot.total = event.total
                        snapshot.done = event.total
                    if event.success is not None:
                        snapshot.success = event.success
                    if event.no_data is not None:
                        snapshot.no_data = event.no_data
                    if event.quarantined is not None:
                        snapshot.quarantined = event.quarantined
                    if event.retried is not None:
                        snapshot.retried = event.retried
                elif event.kind == PipelineEventKind.PHASE_HALTED:
                    snapshot.status = "halted"
                    snapshot.completed_at = now_iso
                    snapshot.message = event.message
                    self.run_status = "halted"

    # ── Rate limiter stats passthrough ─────────────────────────────

    def update_rate_limit(
        self,
        *,
        current_rps: float,
        available_tokens: float,
        recent_429_count: int,
    ) -> None:
        """Update the rate-limit panel from a fresh :class:`AdaptiveTokenBucket` snapshot."""
        with self._lock:
            self.rate_limit.current_rps = current_rps
            self.rate_limit.available_tokens = available_tokens
            self.rate_limit.recent_429_count = recent_429_count

    # ── Sweep funnel passthrough (DEC-DASHBOARD-LIVE-1) ────────────

    def update_sweep_funnel(self, snapshot: SweepFunnelSnapshot) -> None:
        """Replace the sweep-funnel snapshot.

        Called by the brute-force sweep loop every N seconds. The
        JS poller in v29.html fetches ``/api/v1/state`` on the same
        cadence (per ``TRACK1_DASHBOARD_DESIGN.md §6.0``) and updates
        every ``[data-bind="<field>"]`` element with the new value.
        """
        with self._lock:
            self.sweep_funnel = snapshot

    def update_telegram(self, *, stats: dict[str, int | float]) -> None:
        """Update the Telegram panel from a fresh notifier counter snapshot."""
        with self._lock:
            self.telegram.sent = int(stats.get("sent", 0))
            self.telegram.dropped_on_failure = int(stats.get("dropped_on_failure", 0))
            self.telegram.dropped_on_dedupe = int(stats.get("dropped_on_dedupe", 0))
            self.telegram.dropped_on_queue_full = int(stats.get("dropped_on_queue_full", 0))
            self.telegram.dropped_on_disabled = int(stats.get("dropped_on_disabled", 0))

    def update_worker(
        self,
        *,
        worker_id: str,
        status: str,
        current_task: str | None = None,
        elapsed_seconds: float = 0.0,
    ) -> None:
        """Update one worker row in the workers table."""
        with self._lock:
            self.workers[worker_id] = WorkerRow(
                worker_id=worker_id,
                status=status,
                current_task=current_task,
                elapsed_seconds=elapsed_seconds,
                last_heartbeat=datetime.now(UTC).isoformat(),
            )

    # ── Snapshot (read path) ───────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        """Return a deep-enough snapshot for JSON serialisation.

        Thread-safe — called by the HTTP/WebSocket handlers.
        """
        with self._lock:
            return {
                "run_id": self.run_id,
                "run_status": self.run_status,
                "run_started_at": self.run_started_at,
                "run_completed_at": self.run_completed_at,
                "run_elapsed_seconds": self.run_elapsed_seconds,
                "phases": [
                    {
                        "phase": snap.phase,
                        "status": snap.status,
                        "total": snap.total,
                        "done": snap.done,
                        "success": snap.success,
                        "no_data": snap.no_data,
                        "quarantined": snap.quarantined,
                        "retried": snap.retried,
                        "percent": snap.percent,
                        "started_at": snap.started_at,
                        "completed_at": snap.completed_at,
                        "elapsed_seconds": snap.elapsed_seconds,
                        "message": snap.message,
                    }
                    for snap in self.phases.values()
                ],
                "workers": [asdict(w) for w in self.workers.values()],
                "telegram": asdict(self.telegram),
                "rate_limit": asdict(self.rate_limit),
                "recent_events": list(self.recent_events),
                # DEC-DASHBOARD-LIVE-1: sweep_funnel is None until the
                # brute-force loop calls update_sweep_funnel(). The
                # v29.html mockup falls back to its placeholder
                # numbers when sweep_funnel is null in the JSON.
                "sweep_funnel": (
                    asdict(self.sweep_funnel) if self.sweep_funnel is not None else None
                ),
            }


def snapshot_to_json(state: DashboardState) -> str:
    """Render a state snapshot as a JSON string."""
    return json.dumps(state.to_dict(), default=str)


# ────────────────────────────────────────────────────────────────────────
# FastAPI app factory (lazy import)
# ────────────────────────────────────────────────────────────────────────


def build_app(
    state: DashboardState,
    *,
    instrument_store: Any = None,
) -> Any:
    """Construct the FastAPI ASGI app bound to *state*.

    Lazy imports ``fastapi`` so unit tests can import this module
    without the ``fastapi`` dep installed. Raises a helpful error
    at construction time if FastAPI is missing.

    Parameters
    ----------
    state
        In-memory :class:`DashboardState` updated by pipeline events.
    instrument_store
        Optional :class:`~brutex.data.instrument_store.InstrumentStore`
        handle. When supplied, the app exposes read-only instruments /
        expiries / contracts / stats endpoints + an HTML browser
        section so a user can literally see what Groww ingest
        pulled. When ``None`` (the default), those endpoints return
        503 and the HTML section stays hidden \u2014 the pipeline
        progress cards still work.

    Returns
    -------
    fastapi.FastAPI
        Already-wired ASGI app. Caller mounts it on their uvicorn
        server of choice.
    """
    try:
        from fastapi import FastAPI, HTTPException, Query
        from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
    except ImportError as exc:
        raise RuntimeError(
            "FastAPI is not installed. Install with `pip install "
            "fastapi uvicorn` to run the dashboard. Unit tests for the "
            "state model do not need FastAPI."
        ) from exc

    from brutex.observability.metrics import get_registry, render_prometheus

    app = FastAPI(title="BruteX Dashboard", version="0.1.0a0")

    @app.get("/api/v1/state")
    async def api_state() -> JSONResponse:
        """GET /api/v1/state — JSON snapshot of the current dashboard state."""
        return JSONResponse(state.to_dict())

    @app.get("/api/v1/events")
    async def api_events() -> JSONResponse:
        """GET /api/v1/events — JSON dump of the recent-events ring buffer."""
        # DEC-P0-017 (PR #39): same-module friend access — DashboardState's
        # internal _lock guards the shared recent_events deque. The route
        # handler lives in the same module as DashboardState; using the
        # private lock directly is intentional friend-style access.
        with state._lock:  # pyright: ignore[reportPrivateUsage]
            events = list(state.recent_events)
        return JSONResponse({"events": events})

    @app.get("/metrics", response_class=PlainTextResponse)
    async def metrics() -> str:
        """GET /metrics — Prometheus text-format metrics for scraping."""
        return render_prometheus(get_registry())

    # ── Instruments browser endpoints (require InstrumentStore) ──────

    def _require_store() -> Any:
        """Return the configured store or raise 503 with a helpful message."""
        if instrument_store is None:
            raise HTTPException(
                status_code=503,
                detail=(
                    "InstrumentStore not wired. Start the dashboard with "
                    "`brutex dashboard --lake-root <path>` to enable the "
                    "instruments / expiries / contracts browser."
                ),
            )
        return instrument_store

    @app.get("/api/v1/instruments/stats")
    async def api_instruments_stats() -> JSONResponse:
        """GET /api/v1/instruments/stats — summary counts for the dashboard tiles.

        Returns the headline numbers:
          * total rows in the ``instruments`` table
          * total rows in the ``expiries`` table
          * total rows in the ``contracts`` table
          * per-underlying instrument count (for the chart strip)
        """
        store = _require_store()
        return JSONResponse(_collect_store_stats(store))

    @app.get("/api/v1/instruments")
    async def api_instruments_list(
        underlying: str | None = Query(default=None),
        instrument_type: str | None = Query(default=None),
        segment: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
    ) -> JSONResponse:
        """GET /api/v1/instruments — filterable + paginated instruments list.

        Query parameters
        ----------------
        underlying
            Filter by underlying symbol (e.g. ``NIFTY``). Matches
            both spot indices and their derivatives.
        instrument_type
            Filter by ``IDX`` / ``FUT`` / ``CE`` / ``PE``. (``EQ`` removed
            in DEC-P0-032 — Track 1 is index-only per CLAUDE.md §3.)
        segment
            Filter by ``CASH`` / ``FNO``.
        limit, offset
            Server-side pagination. ``limit`` is capped at 1000 to
            keep responses snappy even on large universes.
        """
        store = _require_store()
        rows = _query_instruments(
            store,
            underlying=underlying,
            instrument_type=instrument_type,
            segment=segment,
            limit=limit,
            offset=offset,
        )
        return JSONResponse({"instruments": rows, "limit": limit, "offset": offset})

    @app.get("/api/v1/expiries")
    async def api_expiries_list(
        underlying: str | None = Query(default=None),
    ) -> JSONResponse:
        """GET /api/v1/expiries — every expiry row for the latest snapshot.

        Filters by ``underlying`` if supplied. Returns the full list
        (no pagination) because one underlying rarely has > 100
        expiries worth of history.
        """
        store = _require_store()
        records = store.list_expiries(underlying_symbol=underlying)
        return JSONResponse(
            {
                "expiries": [
                    {
                        "exchange": r.exchange,
                        "underlying_symbol": r.underlying_symbol,
                        "expiry_date": r.expiry_date.isoformat(),
                        "snapshot_date": r.snapshot_date.isoformat(),
                        "pulled_year": r.pulled_year,
                    }
                    for r in records
                ]
            }
        )

    @app.get("/api/v1/contracts")
    async def api_contracts_list(
        underlying: str | None = Query(default=None),
        expiry: str | None = Query(default=None),
        limit: int = Query(default=500, ge=1, le=5000),
    ) -> JSONResponse:
        """GET /api/v1/contracts — filterable contracts list.

        Query parameters
        ----------------
        underlying
            Filter by underlying symbol (e.g. ``NIFTY``). Required
            for any interesting query \u2014 full universe is tens of
            thousands of rows.
        expiry
            ISO date string (``YYYY-MM-DD``). When supplied alongside
            ``underlying``, the query hits the composite index and
            returns in microseconds.
        limit
            Server-side cap. Defaults to 500 so a typical option
            chain (~150 strikes per expiry) fits comfortably.
        """
        store = _require_store()
        expiry_parsed: date | None = None
        if expiry is not None:
            try:
                expiry_parsed = date.fromisoformat(expiry)
            except ValueError as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"expiry must be YYYY-MM-DD, got {expiry!r}",
                ) from exc
        records = store.list_contracts(
            underlying_symbol=underlying,
            expiry_date=expiry_parsed,
        )[:limit]
        return JSONResponse(
            {
                "contracts": [
                    {
                        "exchange": r.exchange,
                        "underlying_symbol": r.underlying_symbol,
                        "expiry_date": r.expiry_date.isoformat(),
                        "contract_symbol": r.contract_symbol,
                        "instrument_type": str(r.instrument_type),
                        "strike_price": (
                            str(r.strike_price) if r.strike_price is not None else None
                        ),
                        "lot_size": r.lot_size,
                        "freeze_quantity": r.freeze_quantity,
                        "tick_size": str(r.tick_size),
                    }
                    for r in records
                ],
                "limit": limit,
            }
        )

    # ── F&O Universe Explorer (DEC-UNIVERSE-DASHBOARD-001) ───────────

    @app.get("/api/v1/universe/summary")
    async def api_universe_summary() -> JSONResponse:
        """GET /api/v1/universe/summary — index_count / stock_count / total.

        Thin wrapper over
        :func:`brutex.dashboard.universe_query.universe_summary`. All
        classification logic lives in that (coverage-tested) module.

        Fast path (DEC-INSTRUMENT-UNIVERSE-FAST-001): the eligible set is
        a SQL ``DISTINCT`` and the candidates are only the IDX + EQ rows, so
        neither this route nor the query helper ever materialises the
        ~tens-of-thousands of option/future legs.
        """
        store = _require_store()
        eligible = store.fno_eligible_underlyings()
        candidates = store.list_index_equity_instruments()
        return JSONResponse(universe_summary(candidates, fno_eligible=eligible))

    @app.get("/api/v1/universe/instruments")
    async def api_universe_instruments(
        kind: str | None = Query(default=None),
    ) -> JSONResponse:
        """GET /api/v1/universe/instruments?kind=index|stock — explorer rows.

        Thin wrapper over
        :func:`brutex.dashboard.universe_query.list_underlyings`. The
        drill into expiries / contracts reuses ``/api/v1/expiries`` +
        ``/api/v1/contracts`` (no duplication).

        Fast path (DEC-INSTRUMENT-UNIVERSE-FAST-001): the eligible set is
        a SQL ``DISTINCT`` and the candidates are only the IDX + EQ rows, so
        the option/future legs are never materialised here.
        """
        store = _require_store()
        eligible = store.fno_eligible_underlyings()
        candidates = store.list_index_equity_instruments()
        try:
            rows = list_underlyings(candidates, kind=kind, fno_eligible=eligible)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse({"instruments": rows, "kind": kind})

    @app.post("/api/v1/universe/load-instruments")
    async def api_universe_load_instruments() -> JSONResponse:
        """POST /api/v1/universe/load-instruments — keylessly populate the list.

        Thin wrapper over
        :func:`brutex.dashboard.instrument_loader.load_instrument_master`.
        The instrument master is a PUBLIC CSV needing NO Groww keys, so this
        endpoint can fill the empty ``/universe`` list without authentication
        (expiries / contracts / candles still need keys, separately). On any
        network / parse failure (the live fetch can't reach the Groww asset
        host, or the CSV header drifted) it maps to **HTTP 502** with a short
        message the UI surfaces.
        """
        from brutex.dashboard.instrument_loader import load_instrument_master
        from brutex.models import InstrumentFormatError

        store = _require_store()
        try:
            counts = load_instrument_master(store)
        except (InstrumentFormatError, OSError) as exc:
            raise HTTPException(
                status_code=502,
                detail=f"couldn't reach Groww asset host / parse instrument master: {exc}",
            ) from exc
        return JSONResponse(counts)

    @app.post("/api/v1/universe/pull")
    async def api_universe_pull(
        symbol: str = Query(...),
        year: int | None = Query(default=None),
    ) -> JSONResponse:
        """POST /api/v1/universe/pull — ingest one instrument's candles.

        Thin wrapper over
        :func:`brutex.dashboard.pull_controller.pull_candles`. ``symbol``
        may be a spot index/stock OR a contract's groww_symbol (a future
        or a CE/PE option) — the controller is instrument-agnostic.
        ``dry_run`` is forced ``True`` automatically when Groww
        credentials are not present in the environment (via the testable
        :func:`brutex.dashboard.pull_controller.groww_creds_present`
        gate, mirroring the CLI's offline default), so the page never
        attempts a live network call without creds.
        """
        dry_run = not groww_creds_present()
        _LOG.info("universe.pull.requested", symbol=symbol, year=year, dry_run=dry_run)
        try:
            report = pull_candles(
                symbol,
                lake_root=_resolve_pull_lake_root(instrument_store, dry_run=dry_run),
                year=year,
                dry_run=dry_run,
            )
        except Exception as exc:  # surface the REAL reason, never swallow it
            # A blank "no data returned" 400 was hiding the actual failure
            # (the route only caught ValueError, but the live path raises
            # VendorSdkError / RuntimeError too). Log the full type + message
            # to the operator's JSONL log so a 400 is never a dead-end, and
            # return the real reason in the response body
            # (DEC-PULL-ERROR-VISIBILITY-001).
            _LOG.exception(
                "universe.pull.failed",
                symbol=symbol,
                year=year,
                dry_run=dry_run,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            raise HTTPException(
                status_code=400,
                detail=f"{type(exc).__name__}: {exc}",
            ) from exc
        _LOG.info(
            "universe.pull.ok",
            symbol=symbol,
            year=year,
            dry_run=dry_run,
            completed=report.total_completed,
            failed=report.total_failed,
            no_data=report.total_no_data,
        )
        return JSONResponse(report.to_dict())

    # ── Two-phase manual pull (DEC-UNIVERSE-PULL-PHASED-001) ────────
    # The two /universe buttons. Each kicks its phase off in a background
    # daemon thread (single-flight guarded) and returns immediately —
    # progress is read from /coverage + /logs. Phase 2 ("contracts") reads
    # ONLY the expiries Phase 1 persisted, so contracts can never precede
    # expiries across clicks. Credentials are required (the live SDK), so a
    # keyless page returns 400 with an actionable message instead of a
    # silent no-op.

    def _phase_lake_years() -> list[int]:
        from datetime import UTC as _UTC
        from datetime import datetime as _dt

        return list(range(2020, _dt.now(_UTC).year + 1))

    @app.post("/api/v1/universe/fetch-expiries")
    async def api_fetch_expiries() -> JSONResponse:
        """POST — Phase 1: fetch ALL new expiries across the universe (2020→now).

        Launches :func:`brutex.cli._run_expiries_phase` in the background. The
        O(1) incremental skip means a re-click only pulls the current year +
        any previously-failed cell; settled past years are skipped before the
        network call. Returns ``{"phase": "expiries", "status": ...}`` where
        status is ``started`` / ``already_running`` / ``no_store``.
        """
        store = _require_store()
        if not groww_creds_present():
            raise HTTPException(
                status_code=400,
                detail="Groww credentials required — set them on the Credentials page, then retry.",
            )

        def _worker(lake_root: Path) -> object:  # pragma: no cover — live thread
            """Background body — run the Phase-1 expiries pull for ``lake_root``."""
            from brutex.cli import (
                _run_expiries_phase,  # pyright: ignore[reportPrivateUsage]
            )

            return _run_expiries_phase(lake_root, years=_phase_lake_years(), store=store)

        status = _start_phase("expiries", store, _worker)
        _LOG.info("universe.fetch_expiries.requested", status=status)
        return JSONResponse({"phase": "expiries", "status": status})

    @app.post("/api/v1/universe/fetch-contracts")
    async def api_fetch_contracts() -> JSONResponse:
        """POST — Phase 2: fetch ALL new contracts for every persisted expiry.

        Launches :func:`brutex.cli._run_contracts_phase` in the background. It
        reads ONLY the expiries Phase 1 persisted (so you must run "Fetch all
        expiries" first); the O(1) skip re-pulls only not-yet-expired expiries
        + any uncaptured expired one. Returns the same status shape as
        fetch-expiries.
        """
        store = _require_store()
        if not groww_creds_present():
            raise HTTPException(
                status_code=400,
                detail="Groww credentials required — set them on the Credentials page, then retry.",
            )

        def _worker(lake_root: Path) -> object:  # pragma: no cover — live thread
            """Background body — run the Phase-2 contracts pull for ``lake_root``."""
            from brutex.cli import (
                _run_contracts_phase,  # pyright: ignore[reportPrivateUsage]
            )

            return _run_contracts_phase(lake_root, store=store)

        status = _start_phase("contracts", store, _worker)
        _LOG.info("universe.fetch_contracts.requested", status=status)
        return JSONResponse({"phase": "contracts", "status": status})

    @app.get("/api/v1/universe/phase-status")
    async def api_phase_status() -> JSONResponse:
        """GET — whether each phase is currently running (for button enable/disable)."""
        return JSONResponse(
            {
                "expiries_running": _phase_busy("expiries"),
                "contracts_running": _phase_busy("contracts"),
            }
        )

    # ── Completeness ledger / Coverage (DEC-COVERAGE-LEDGER-001) ────

    @app.get("/api/v1/coverage")
    async def api_coverage() -> JSONResponse:
        """GET /api/v1/coverage — headline + per-cell coverage rows.

        Thin wrapper over
        :func:`brutex.dashboard.coverage_view.build_coverage_payload`. The
        expected universe×years grid is derived from the lake's IDX + EQ
        candidates (:func:`brutex.universe.derive_lake_underlyings`) × the
        Groww F&O history window (2020 → current year); the actual rows come
        from ``store.list_coverage()`` (indexed read). All projection logic
        lives in the (coverage-tested) ``coverage_view`` module.
        """
        store = _require_store()
        # derive_lake_underlyings yields (underlying, exchange) (the order the
        # universe ingest fan-out wants); the coverage reconciler / view want
        # (exchange, underlying) — swap so the expected grid keys line up with
        # the persisted ledger's (exchange, underlying_symbol, year) PK.
        expected = [
            (exchange, underlying)
            for underlying, exchange in derive_lake_underlyings(
                store.list_index_equity_instruments()
            )
        ]
        years = _coverage_years()
        return JSONResponse(build_coverage_payload(store.list_coverage(), expected, years))

    @app.post("/api/v1/coverage/repull")
    async def api_coverage_repull() -> JSONResponse:
        """POST /api/v1/coverage/repull — kick a resumable re-pull of the gaps.

        Launches the resumable universe re-pull (only the incomplete cells,
        per the persisted ledger) in a BACKGROUND daemon thread, mirroring the
        ``cmd_dashboard`` auto-pull pattern (DEC-AUTO-UNIVERSE-001) so a long
        live pull never blocks the request. Best-effort: when creds are
        absent or the live SDK can't be reached, the thread records the
        failure into the ledger and exits — the endpoint itself never crashes
        and always returns ``{"started": bool}``.
        """
        store = _require_store()
        started = _start_coverage_repull(store)
        return JSONResponse({"started": started})

    @app.get("/coverage", response_class=HTMLResponse)
    async def coverage_page() -> str:
        """GET /coverage — the completeness-ledger page (DEC-COVERAGE-LEDGER-001)."""
        return _render_coverage_html()

    # ── Credentials store (DEC-CREDENTIALS-STORE-001) ───────────────
    # CredentialsIn is module-scope (see top of file) so FastAPI resolves
    # it as a request body, not a query param (DEC-CREDS-422-FIX-001).

    @app.post("/api/v1/credentials")
    async def api_credentials_set(body: CredentialsIn) -> JSONResponse:
        """POST /api/v1/credentials — save the Groww credentials (mode 0600).

        Accepts the API key + TOTP secret in the request BODY (never query
        params, which leak into access logs). The body is NOT logged. On a
        validation error (blank field) the credentials store raises
        :class:`CredentialsError`, mapped here to HTTP 400 with a message
        that names the offending field but NEVER its value. Returns only
        ``{"present": true}`` — never echoes a value back.
        """
        from brutex.adapters.groww.auth import clear_cached_token
        from brutex.config.credentials import (
            CredentialsError,
            GrowwCredentials,
            credentials_preview,
            save_groww_credentials,
        )

        try:
            save_groww_credentials(
                GrowwCredentials(api_key=body.api_key, totp_secret=body.totp_secret)
            )
        except CredentialsError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # Invalidate any token minted from the OLD credentials so the corrected
        # paste takes effect on the next session (DEC-CREDS-NORMALISE-001) —
        # Groww resets API keys daily at 6 AM IST, so a stale cache would keep
        # failing. Best-effort: never block a successful save on cleanup.
        with contextlib.suppress(OSError):
            clear_cached_token()
        # Echo back the masked preview so the page can confirm the paste landed
        # (right value/length, not empty) — never a usable secret.
        return JSONResponse(credentials_preview())

    @app.get("/api/v1/credentials/status")
    async def api_credentials_status() -> JSONResponse:
        """GET /api/v1/credentials/status — presence + a masked, non-secret preview."""
        from brutex.config.credentials import credentials_preview

        return JSONResponse(credentials_preview())

    @app.get("/credentials", response_class=HTMLResponse)
    async def credentials_page() -> str:
        """GET /credentials — the Credentials page (DEC-CREDENTIALS-STORE-001)."""
        return _render_credentials_html()

    @app.get("/api/v1/logs")
    async def api_logs(
        lines: int = Query(default=200),
    ) -> JSONResponse:
        """GET /api/v1/logs?lines=N — tail of the rotating log file (DEC-LOGS-VIEWER-001).

        Thin wrapper over
        :func:`brutex.observability.log_tail.tail_records`. ``lines`` is
        clamped to ``[1, 2000]`` inside the (coverage-tested) reader, so a
        hostile / malformed query string can never trigger an unbounded
        read. Returns parsed JSON-log records plus the resolved file path.
        """
        from brutex.observability.log_tail import tail_records
        from brutex.observability.logging import resolve_log_file

        path = resolve_log_file()
        records = tail_records(path, max_lines=lines)
        return JSONResponse({"lines": records, "path": str(path)})

    # ── Database viewer (DEC-DB-VIEWER-001) ─────────────────────────

    @app.get("/api/v1/db/tables")
    async def api_db_tables() -> JSONResponse:
        """GET /api/v1/db/tables — row counts for every known DuckDB table.

        Thin wrapper over :func:`brutex.dashboard.db_view.table_summary`.
        Tolerates a missing table (count 0) so a fresh/empty store works.
        """
        store = _require_store()
        return JSONResponse({"tables": table_summary(store.connection)})

    @app.get("/api/v1/db/table/{name}")
    async def api_db_table(
        name: str,
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> JSONResponse:
        """GET /api/v1/db/table/{name}?limit=N&offset=M — one page of a table.

        Thin wrapper over :func:`brutex.dashboard.db_view.sample_rows`. Pages
        via ``LIMIT ? OFFSET ?`` over an indexed ``COUNT(*)`` so even the
        ~89k-row legs table (when present) never materialises in full
        (DEC-INSTRUMENT-UNIVERSE-FAST-001). The table-name allowlist + page
        clamps + read-only SELECT live in that (coverage-tested) module; an
        unknown / injection-y table name raises ``ValueError`` there, mapped
        to HTTP 400 here.
        """
        store = _require_store()
        try:
            payload = sample_rows(store.connection, name, limit=limit, offset=offset)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(payload)

    @app.get("/db", response_class=HTMLResponse)
    async def db_page() -> str:
        """GET /db — read-only DuckDB browser page (DEC-DB-VIEWER-001)."""
        return _render_db_html()

    @app.get("/logs", response_class=HTMLResponse)
    async def logs_page() -> str:
        """GET /logs — live log viewer page (DEC-LOGS-VIEWER-001)."""
        return _render_logs_html()

    @app.get("/universe", response_class=HTMLResponse)
    async def universe_page() -> str:
        """GET /universe — the F&O Universe Explorer page (DEC-UNIVERSE-DASHBOARD-001)."""
        return _render_universe_html()

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        """GET / — inline HTML dashboard with embedded JS WebSocket client."""
        return _render_index_html()

    # WebSocket handler — defined at module scope below to sidestep
    # the `from __future__ import annotations` closure-annotation
    # resolution problem. We register it dynamically.
    app.add_api_websocket_route("/ws", _make_ws_handler(state))

    return app


def _resolve_pull_lake_root(instrument_store: Any, *, dry_run: bool) -> Path:
    """Resolve the lake root for the pull sink.

    The live (``# pragma: no cover``) pull path writes Parquet into the
    store's on-disk parent directory; the dry-run synthetic fetcher
    ignores the path entirely. To avoid silently scattering a live pull
    into a guessed ``~/.brutex/lake`` (M1), the live path REQUIRES a
    resolvable store path and raises a clear error otherwise. The
    dry-run path tolerates a missing store (the synthetic fetcher never
    touches disk), returning a placeholder.

    Parameters
    ----------
    instrument_store
        The wired :class:`~brutex.data.instrument_store.InstrumentStore`,
        or ``None``.
    dry_run
        Whether the pull is offline/synthetic. When ``True`` the lake
        root is unused, so a missing store path is tolerated.

    Raises
    ------
    RuntimeError
        On the LIVE path when no lake root can be derived from the store.
    """
    store_path = getattr(instrument_store, "path", None)
    if isinstance(store_path, Path):
        return store_path.parent
    if dry_run:
        return Path.home() / ".brutex" / "lake"
    raise RuntimeError(  # pragma: no cover — live-only: needs creds + a wired store
        "Cannot resolve a lake root for a LIVE pull: no InstrumentStore "
        "path is wired. Start the dashboard with "
        "`brutex dashboard --lake-root <path>` so live pulls have a "
        "Parquet sink, or run without Groww creds for the offline "
        "synthetic preview."
    )


def _make_ws_handler(state: DashboardState) -> Any:
    """Build a WebSocket handler closure for :func:`build_app`.

    Defined at module scope so that when FastAPI inspects the
    handler's type annotations at registration time, the
    ``WebSocket`` name resolves against this module's globals
    rather than against a closure-local import that
    ``from __future__ import annotations`` has stringified.

    We import ``WebSocket`` / ``WebSocketDisconnect`` at call time
    to keep the ``fastapi`` dep lazy (unit tests that don't hit
    the dashboard still don't need FastAPI installed).
    """
    # Import inside the function body so the module-level doesn't
    # require fastapi. But hoist the types into globals so the
    # handler's annotation is resolvable.
    from fastapi import WebSocket, WebSocketDisconnect

    globals()["WebSocket"] = WebSocket
    globals()["WebSocketDisconnect"] = WebSocketDisconnect

    async def ws_handler(websocket: WebSocket) -> None:
        """WebSocket /ws — pushes a state snapshot every 500 ms."""
        await websocket.accept()
        try:
            while True:
                payload = state.to_dict()
                await websocket.send_json(payload)
                await _async_sleep(0.5)
        except WebSocketDisconnect:
            return

    return ws_handler


async def _async_sleep(seconds: float) -> None:
    """Async-friendly wrapper around ``asyncio.sleep`` used by the WS handler."""
    import asyncio

    await asyncio.sleep(seconds)


def _collect_store_stats(store: Any) -> dict[str, Any]:
    """Collect summary tile numbers from an :class:`InstrumentStore`.

    Returns a dict with the headline counts every dashboard user
    wants to see on landing: total rows in each of the three core
    tables plus the per-underlying instrument breakdown. Called by
    the ``/api/v1/instruments/stats`` endpoint.
    """
    # DEC-P0-017 (PR #39): use public InstrumentStore.connection property
    # instead of touching the private _conn attribute (pyright
    # reportPrivateUsage was project-wide silenced; narrowing to "warning"
    # would have flagged this; we used the property refactor route per
    # PR #39 pre-impl review recommendation).
    conn = store.connection
    row_instruments = conn.execute("SELECT COUNT(*) FROM instruments").fetchone()
    row_expiries = conn.execute("SELECT COUNT(*) FROM expiries").fetchone()
    row_contracts = conn.execute("SELECT COUNT(*) FROM contracts").fetchone()
    row_runs = conn.execute("SELECT COUNT(*) FROM instrument_runs").fetchone()
    by_underlying = conn.execute(
        "SELECT underlying_symbol, COUNT(*) FROM instruments "
        "WHERE underlying_symbol IS NOT NULL "
        "GROUP BY underlying_symbol "
        "ORDER BY COUNT(*) DESC "
        "LIMIT 20"
    ).fetchall()
    by_type = conn.execute(
        "SELECT instrument_type, COUNT(*) FROM instruments GROUP BY instrument_type"
    ).fetchall()
    return {
        "totals": {
            "instruments": int(row_instruments[0]) if row_instruments else 0,
            "expiries": int(row_expiries[0]) if row_expiries else 0,
            "contracts": int(row_contracts[0]) if row_contracts else 0,
            "runs": int(row_runs[0]) if row_runs else 0,
        },
        "by_underlying": [{"underlying": str(u), "count": int(c)} for u, c in by_underlying],
        "by_type": [{"instrument_type": str(t), "count": int(c)} for t, c in by_type],
    }


def _query_instruments(
    store: Any,
    *,
    underlying: str | None,
    instrument_type: str | None,
    segment: str | None,
    limit: int,
    offset: int,
) -> list[dict[str, Any]]:
    """Run a filtered instruments query and return JSON-friendly rows.

    Goes through the store's raw connection for speed (the dashboard
    is a read-only peer; all the tables are open at the same time).
    Every user-supplied filter value flows through parameter
    binding, never string concatenation.
    """
    # DEC-P0-017 (PR #39): use public InstrumentStore.connection property
    # instead of touching the private _conn attribute (pyright
    # reportPrivateUsage was project-wide silenced; narrowing to "warning"
    # would have flagged this; we used the property refactor route per
    # PR #39 pre-impl review recommendation).
    conn = store.connection
    clauses: list[str] = []
    params: list[Any] = []
    if underlying is not None:
        clauses.append("(underlying_symbol = ? OR trading_symbol = ?)")
        params.append(underlying)
        params.append(underlying)
    if instrument_type is not None:
        clauses.append("instrument_type = ?")
        params.append(instrument_type)
    if segment is not None:
        clauses.append("segment = ?")
        params.append(segment)
    where_sql = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    # Clauses are fixed-literal WHERE fragments; every user value is
    # parameter-bound via DuckDB "?" placeholders. Not an injection vector.
    base_select = (
        "SELECT vendor_symbol, exchange, trading_symbol, instrument_type, "
        "segment, underlying_symbol, expiry_date, strike_price, lot_size, "
        "freeze_quantity, tick_size, snapshot_date FROM instruments"
    )
    order_limit = " ORDER BY vendor_symbol LIMIT ? OFFSET ?"
    sql = base_select + where_sql + order_limit  # nosec B608
    params.append(limit)
    params.append(offset)
    rows = conn.execute(sql, params).fetchall()
    return [
        {
            "vendor_symbol": r[0],
            "exchange": r[1],
            "trading_symbol": r[2],
            "instrument_type": str(r[3]),
            "segment": str(r[4]),
            "underlying_symbol": r[5],
            "expiry_date": r[6].isoformat() if r[6] is not None else None,
            "strike_price": str(r[7]) if r[7] is not None else None,
            "lot_size": r[8],
            "freeze_quantity": r[9],
            "tick_size": str(r[10]) if r[10] is not None else None,
            "snapshot_date": r[11].isoformat() if r[11] is not None else None,
        }
        for r in rows
    ]


_V29_TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "v29.html"
_V29_TEMPLATE_CACHE: str | None = None


def _load_v29_template() -> str:
    """Load the v29 dashboard template once and cache.

    The polished v29 design (per DEC-DASHBOARD-LIVE-FINALE) ships as a
    package-relative resource at ``brutex/dashboard/templates/v29.html``.
    Loaded once at first access, then served verbatim by ``GET /``.

    The template embeds its own JS poller that fetches
    ``/api/v1/state`` every 2 seconds and updates every element with a
    ``data-bind`` attribute. When the producer fields are ``None``
    (engine not yet running), the poller leaves placeholder values
    intact — operator sees the polished design with mockup numbers
    until real data starts flowing.

    Returns:
        The full v29 HTML+CSS+JS as a single string.
    """
    global _V29_TEMPLATE_CACHE  # noqa: PLW0603 — single-load module-level cache
    if _V29_TEMPLATE_CACHE is None:
        _V29_TEMPLATE_CACHE = _V29_TEMPLATE_PATH.read_text(encoding="utf-8")
    return _V29_TEMPLATE_CACHE


def _render_index_html() -> str:
    """Return the polished v29 dashboard HTML (per DEC-DASHBOARD-LIVE-FINALE).

    The v29 mockup is the locked design spec from DEC-DASHBOARD-LIVE-FINALE
    (MIGRATION.md entry #234). It has all 13 ``SweepFunnelSnapshot`` fields
    wired via ``data-bind`` attributes, plus a built-in JS poller that
    fetches ``/api/v1/state`` every 2 seconds and updates every bound element.

    The Instruments Browser panel (formerly conditionally injected by an
    ``instruments_enabled`` switch) remains available as a separate concern
    via the ``/api/v1/instruments`` REST endpoint.

    Returns:
        The v29 HTML+CSS+JS template (~214 KB) ready for FastAPI
        ``HTMLResponse``. The template renders identically whether
        the data producer (Phase 3 engine) is online or not — placeholder
        mockup values stay visible until the poller receives real data.
    """
    return _load_v29_template()


_UNIVERSE_TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "universe.html"
_UNIVERSE_TEMPLATE_CACHE: str | None = None


def _load_universe_template() -> str:
    """Load + cache the F&O Universe Explorer page template.

    Ships as a package-relative resource at
    ``brutex/dashboard/templates/universe.html`` (DEC-UNIVERSE-DASHBOARD-001).
    Ported from ``docs/dashboard/mockups/universe-explorer-v1.html`` with
    the synthetic JS data swapped for ``fetch()`` calls to the real
    ``/api/v1/universe/*`` endpoints. Loaded once, then served verbatim.
    """
    global _UNIVERSE_TEMPLATE_CACHE  # noqa: PLW0603 — single-load module cache
    if _UNIVERSE_TEMPLATE_CACHE is None:
        _UNIVERSE_TEMPLATE_CACHE = _UNIVERSE_TEMPLATE_PATH.read_text(encoding="utf-8")
    return _UNIVERSE_TEMPLATE_CACHE


def _render_universe_html() -> str:
    """Return the F&O Universe Explorer page HTML (DEC-UNIVERSE-DASHBOARD-001)."""
    return _load_universe_template()


_COVERAGE_TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "coverage.html"
_COVERAGE_TEMPLATE_CACHE: str | None = None


def _load_coverage_template() -> str:
    """Load + cache the completeness-ledger page template (DEC-COVERAGE-LEDGER-001).

    Ships as a package-relative resource at
    ``brutex/dashboard/templates/coverage.html``. The page polls
    ``/api/v1/coverage`` for the headline + per-cell table and POSTs
    ``/api/v1/coverage/repull`` from the "Re-pull gaps" button. Loaded once,
    then served verbatim.
    """
    global _COVERAGE_TEMPLATE_CACHE  # noqa: PLW0603 — single-load module cache
    if _COVERAGE_TEMPLATE_CACHE is None:
        _COVERAGE_TEMPLATE_CACHE = _COVERAGE_TEMPLATE_PATH.read_text(encoding="utf-8")
    return _COVERAGE_TEMPLATE_CACHE


def _render_coverage_html() -> str:
    """Return the completeness-ledger page HTML (DEC-COVERAGE-LEDGER-001)."""
    return _load_coverage_template()


def _coverage_years() -> list[int]:
    """Return the expected coverage years — Groww serves F&O from 2020 → now."""
    return list(range(2020, datetime.now(UTC).year + 1))


def _start_coverage_repull(store: Any) -> bool:
    """Launch the resumable universe re-pull in a background daemon thread.

    Mirrors ``cmd_dashboard``'s auto-pull thread (DEC-AUTO-UNIVERSE-001): the
    live Groww SDK glue lives in :func:`brutex.cli._run_live_universe_ingest`
    (``# pragma: no cover``) which, with ``resume=True``, re-pulls ONLY the
    incomplete ledger cells and records each cell's outcome. Best-effort:
    any failure is swallowed (logged via the thread body's own print), so the
    request handler never crashes. Returns whether the thread was started.
    """
    store_path = getattr(store, "path", None)
    if not isinstance(store_path, Path):  # pragma: no cover — needs a wired store path
        return False

    def _repull() -> None:  # pragma: no cover — live SDK + network + thread
        from datetime import UTC as _UTC
        from datetime import datetime as _dt

        from brutex.cli import _run_live_universe_ingest

        try:
            # Re-pull the gaps across ALL years from Jan 2020 → now
            # (DEC-UNIVERSE-PULL-2020-001) — resume only touches the incomplete
            # cells, so this fills every missing expiry/contract from 2020.
            years = list(range(2020, _dt.now(_UTC).year + 1))
            _run_live_universe_ingest(store_path.parent, years=years, store=store, resume=True)
        except Exception:  # pylint: disable=broad-exception-caught
            # Best-effort background pull — never crash the server; the ledger
            # itself records per-cell failures inside the ingest.
            return

    threading.Thread(target=_repull, name="brutex-coverage-repull", daemon=True).start()
    return True


# ── Two-phase manual pull (DEC-UNIVERSE-PULL-PHASED-001) ────────────────────
# Single-flight state for the two /universe buttons: a phase already running
# must not be double-launched by an impatient second click. A plain dict +
# lock is sufficient (the dashboard is single-process); the flag is cleared in
# the worker's finally so a crashed phase can always be retried.
_PHASE_LOCK = threading.Lock()
_PHASE_RUNNING: dict[str, bool] = {"expiries": False, "contracts": False}


def _phase_busy(name: str) -> bool:
    """Return whether *name* phase ('expiries' | 'contracts') is currently running."""
    with _PHASE_LOCK:
        return _PHASE_RUNNING.get(name, False)


def _start_phase(name: str, store: Any, worker: Callable[[Path], object]) -> str:
    """Launch *worker* in a daemon thread under the single-flight guard.

    Returns one of: ``"started"`` (a fresh run kicked off), ``"already_running"``
    (a click while the same phase is in flight — ignored), or ``"no_store"`` (no
    wired lake path; the offline/dry page can't run a live phase). Best-effort:
    the worker body swallows its own exceptions (the ledger/logs record per-item
    failures) so the request handler never crashes.
    """
    store_path = getattr(store, "path", None)
    if not isinstance(store_path, Path):  # pragma: no cover — needs a wired store path
        return "no_store"
    with _PHASE_LOCK:
        if _PHASE_RUNNING.get(name, False):
            return "already_running"
        _PHASE_RUNNING[name] = True

    def _run() -> None:  # pragma: no cover — live SDK + network + thread
        try:
            worker(store_path.parent)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            # NEVER silent (DEC-PULL-ERROR-VISIBILITY-001): a setup/creds-runtime
            # failure BEFORE the per-item loop (bad token mint, master refresh)
            # would otherwise vanish — the operator clicks, the button re-enables,
            # and /logs shows nothing. Log the full reason to the JSONL file.
            _LOG.exception(
                "universe.phase.failed", phase=name, error_type=type(exc).__name__, error=str(exc)
            )
        finally:
            with _PHASE_LOCK:
                _PHASE_RUNNING[name] = False

    try:
        threading.Thread(target=_run, name=f"brutex-phase-{name}", daemon=True).start()
    except RuntimeError:  # pragma: no cover — OS thread exhaustion
        # If the thread can't even start, clear the flag we just set so the
        # button isn't latched disabled forever (the _run finally never fires).
        with _PHASE_LOCK:
            _PHASE_RUNNING[name] = False
        _LOG.exception("universe.phase.thread_start_failed", phase=name)
        return "no_store"
    return "started"


_CREDENTIALS_TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "credentials.html"
_CREDENTIALS_TEMPLATE_CACHE: str | None = None


def _load_credentials_template() -> str:
    """Load + cache the Credentials page template (DEC-CREDENTIALS-STORE-001).

    Ships as a package-relative resource at
    ``brutex/dashboard/templates/credentials.html``. The page POSTs the API
    key + TOTP secret to ``/api/v1/credentials`` (body, never query) and
    GETs ``/api/v1/credentials/status`` for a boolean status line — it
    never displays a secret value. Loaded once, then served verbatim.
    """
    global _CREDENTIALS_TEMPLATE_CACHE  # noqa: PLW0603 — single-load module cache
    if _CREDENTIALS_TEMPLATE_CACHE is None:
        _CREDENTIALS_TEMPLATE_CACHE = _CREDENTIALS_TEMPLATE_PATH.read_text(encoding="utf-8")
    return _CREDENTIALS_TEMPLATE_CACHE


def _render_credentials_html() -> str:
    """Return the Credentials page HTML (DEC-CREDENTIALS-STORE-001)."""
    return _load_credentials_template()


_LOGS_TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "logs.html"
_LOGS_TEMPLATE_CACHE: str | None = None


def _load_logs_template() -> str:
    """Load + cache the live log-viewer page template.

    Ships as a package-relative resource at
    ``brutex/dashboard/templates/logs.html`` (DEC-LOGS-VIEWER-001). The
    template polls ``/api/v1/logs`` every 2 seconds, escapes every
    interpolated log string before insertion (XSS), colour-codes the level,
    and auto-scrolls to the newest line. Loaded once, then served verbatim.
    """
    global _LOGS_TEMPLATE_CACHE  # noqa: PLW0603 — single-load module cache
    if _LOGS_TEMPLATE_CACHE is None:
        _LOGS_TEMPLATE_CACHE = _LOGS_TEMPLATE_PATH.read_text(encoding="utf-8")
    return _LOGS_TEMPLATE_CACHE


def _render_logs_html() -> str:
    """Return the live log-viewer page HTML (DEC-LOGS-VIEWER-001)."""
    return _load_logs_template()


_DB_TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "db.html"
_DB_TEMPLATE_CACHE: str | None = None


def _load_db_template() -> str:
    """Load + cache the read-only DuckDB browser page template.

    Ships as a package-relative resource at
    ``brutex/dashboard/templates/db.html`` (DEC-DB-VIEWER-001). The
    template fetches ``/api/v1/db/tables`` for the table list + row
    counts, then ``/api/v1/db/table/{name}`` when a table is clicked,
    escaping every column name + cell via an ``esc()`` helper before it
    touches innerHTML (XSS — values originate from the instrument-master
    CSV). Loaded once, then served verbatim.
    """
    global _DB_TEMPLATE_CACHE  # noqa: PLW0603 — single-load module cache
    if _DB_TEMPLATE_CACHE is None:
        _DB_TEMPLATE_CACHE = _DB_TEMPLATE_PATH.read_text(encoding="utf-8")
    return _DB_TEMPLATE_CACHE


def _render_db_html() -> str:
    """Return the read-only DuckDB browser page HTML (DEC-DB-VIEWER-001)."""
    return _load_db_template()


_INSTRUMENTS_HTML_BLOCK = """\
<div class="card" id="instruments-card">
  <h3>Instruments Browser
    <span class="stats" style="font-weight:normal;">
      (FR-ING-003 temporal master — read-only view of the pulled FNO universe)
    </span>
  </h3>
  <div class="grid" style="grid-template-columns: repeat(4, 1fr);">
    <div class="card">
      <h3>Total Instruments</h3>
      <div class="stats" style="font-size:22px;color:#e5e7eb;" id="ins-total">-</div>
    </div>
    <div class="card">
      <h3>Total Expiries</h3>
      <div class="stats" style="font-size:22px;color:#e5e7eb;" id="exp-total">-</div>
    </div>
    <div class="card">
      <h3>Total Contracts</h3>
      <div class="stats" style="font-size:22px;color:#e5e7eb;" id="con-total">-</div>
    </div>
    <div class="card">
      <h3>Ingest Runs</h3>
      <div class="stats" style="font-size:22px;color:#e5e7eb;" id="run-total">-</div>
    </div>
  </div>

  <div style="margin:12px 0;">
    <label class="stats">Underlying:
      <input id="ins-filter-underlying" type="text" placeholder="NIFTY"
             style="background:#1a1d24;color:#e5e7eb;border:1px solid #2a2e39;
                    padding:4px 8px;border-radius:4px;">
    </label>
    <label class="stats" style="margin-left:12px;">Type:
      <select id="ins-filter-type"
              style="background:#1a1d24;color:#e5e7eb;border:1px solid #2a2e39;
                     padding:4px 8px;border-radius:4px;">
        <option value="">(any)</option>
        <option value="IDX">IDX</option>
        <option value="FUT">FUT</option>
        <option value="CE">CE</option>
        <option value="PE">PE</option>
      </select>
    </label>
    <label class="stats" style="margin-left:12px;">Segment:
      <select id="ins-filter-segment"
              style="background:#1a1d24;color:#e5e7eb;border:1px solid #2a2e39;
                     padding:4px 8px;border-radius:4px;">
        <option value="">(any)</option>
        <option value="FNO">FNO</option>
        <option value="CASH">CASH</option>
      </select>
    </label>
    <button id="ins-refresh"
            style="margin-left:12px;background:#0b63b5;color:#fff;border:none;
                   padding:5px 12px;border-radius:4px;cursor:pointer;">
      Refresh
    </button>
  </div>

  <table id="ins-table">
    <thead>
      <tr>
        <th>vendor_symbol</th>
        <th>type</th>
        <th>segment</th>
        <th>underlying</th>
        <th>expiry</th>
        <th>strike</th>
        <th>lot</th>
        <th>freeze</th>
      </tr>
    </thead>
    <tbody></tbody>
  </table>
  <div class="stats" id="ins-status">no query yet</div>
</div>
"""

_INSTRUMENTS_JS_BLOCK = """\
async function loadInstrumentStats() {
  try {
    const r = await fetch('/api/v1/instruments/stats');
    if (!r.ok) return;
    const s = await r.json();
    document.getElementById('ins-total').textContent = s.totals.instruments.toLocaleString();
    document.getElementById('exp-total').textContent = s.totals.expiries.toLocaleString();
    document.getElementById('con-total').textContent = s.totals.contracts.toLocaleString();
    document.getElementById('run-total').textContent = s.totals.runs.toLocaleString();
  } catch (e) { /* dashboard continues without the tiles */ }
}

async function loadInstruments() {
  const u = document.getElementById('ins-filter-underlying').value.trim();
  const t = document.getElementById('ins-filter-type').value;
  const seg = document.getElementById('ins-filter-segment').value;
  const qs = new URLSearchParams();
  if (u) qs.set('underlying', u);
  if (t) qs.set('instrument_type', t);
  if (seg) qs.set('segment', seg);
  qs.set('limit', '200');
  const status = document.getElementById('ins-status');
  status.textContent = 'loading…';
  try {
    const r = await fetch('/api/v1/instruments?' + qs.toString());
    const body = await r.json();
    const rows = body.instruments || [];
    const tb = document.querySelector('#ins-table tbody');
    tb.innerHTML = rows.map(i =>
      `<tr>
        <td>${i.vendor_symbol}</td>
        <td>${i.instrument_type}</td>
        <td>${i.segment}</td>
        <td>${i.underlying_symbol || '-'}</td>
        <td>${i.expiry_date || '-'}</td>
        <td>${i.strike_price || '-'}</td>
        <td>${i.lot_size || '-'}</td>
        <td>${i.freeze_quantity || '-'}</td>
      </tr>`
    ).join('');
    status.textContent = rows.length + ' rows';
  } catch (e) { status.textContent = 'error: ' + e.message; }
}

document.getElementById('ins-refresh').addEventListener('click', loadInstruments);
loadInstrumentStats();
loadInstruments();
"""


_INDEX_HTML_TEMPLATE = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>BruteX Dashboard</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
         background: #0f1116; color: #e5e7eb; margin: 0; padding: 20px; }
  h1 { font-size: 20px; margin: 0 0 16px; }
  .card { background: #1a1d24; border: 1px solid #2a2e39;
          border-radius: 6px; padding: 14px; margin-bottom: 12px; }
  .phase-card .bar { background: #2a2e39; height: 10px; border-radius: 4px;
                     overflow: hidden; margin: 6px 0; }
  .phase-card .bar div { background: linear-gradient(90deg, #10b981, #3b82f6);
                         height: 100%; transition: width 300ms; }
  .phase-card h3 { margin: 0 0 4px; font-size: 14px; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 4px;
           font-size: 11px; font-weight: 600; margin-right: 6px; }
  .badge.running { background: #0b63b5; color: #fff; }
  .badge.done { background: #0b8a43; color: #fff; }
  .badge.halted { background: #c23b3b; color: #fff; }
  .badge.pending { background: #3a3f4a; color: #cbd5e1; }
  .stats { font-size: 12px; color: #94a3b8; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  table { width: 100%; border-collapse: collapse; font-size: 12px; }
  th, td { text-align: left; padding: 4px 8px;
           border-bottom: 1px solid #2a2e39; }
  th { color: #94a3b8; font-weight: 500; }
  .status-dot { display: inline-block; width: 8px; height: 8px;
                border-radius: 50%; margin-right: 6px; background: #64748b; }
  .status-dot.running { background: #3b82f6; animation: pulse 1.5s infinite; }
  .status-dot.halted { background: #ef4444; }
  .status-dot.complete { background: #10b981; }
  @keyframes pulse { 0% { opacity: 1; } 50% { opacity: 0.3; } 100% { opacity: 1; } }
  .muted { color: #64748b; }
</style>
</head>
<body>
<h1>
  <span id="run-dot" class="status-dot"></span>
  BruteX Dashboard — <span id="run-id" class="muted">(no run)</span>
</h1>

<div id="phases"></div>

<div class="grid">
  <div class="card">
    <h3>Rate Limit</h3>
    <div class="stats">
      current: <span id="rl-rps">-</span> rps ·
      tokens: <span id="rl-tokens">-</span> ·
      429s (60s): <span id="rl-429">-</span>
    </div>
  </div>
  <div class="card">
    <h3>Telegram</h3>
    <div class="stats">
      sent: <span id="tg-sent">-</span> ·
      dropped: fail <span id="tg-fail">-</span>,
      dedupe <span id="tg-dup">-</span>
    </div>
  </div>
</div>

<div class="card">
  <h3>Workers</h3>
  <table id="workers"><thead><tr>
    <th>id</th><th>status</th><th>task</th><th>elapsed</th>
  </tr></thead><tbody></tbody></table>
</div>

<div class="card">
  <h3>Recent Events</h3>
  <table id="events"><thead><tr>
    <th>time</th><th>phase</th><th>kind</th><th>detail</th>
  </tr></thead><tbody></tbody></table>
</div>

<!--INSTRUMENTS_BLOCK-->

<script>
const phasesEl = document.getElementById('phases');
const runDot = document.getElementById('run-dot');
const runId = document.getElementById('run-id');

function renderState(s) {
  runId.textContent = s.run_id || '(no run)';
  runDot.className = 'status-dot ' + (s.run_status || 'idle');

  phasesEl.innerHTML = '';
  (s.phases || []).forEach(p => {
    const div = document.createElement('div');
    div.className = 'card phase-card';
    div.innerHTML = `
      <h3>${p.phase} <span class="badge ${p.status}">${p.status}</span></h3>
      <div class="bar"><div style="width:${p.percent}%"></div></div>
      <div class="stats">
        ${p.done}/${p.total} (${p.percent}%) ·
        ok: ${p.success} · no_data: ${p.no_data} · quarantined: ${p.quarantined}
        ${p.elapsed_seconds ? '· ' + p.elapsed_seconds.toFixed(1) + 's' : ''}
        ${p.message ? '<br><span style="color:#ef4444">' + p.message + '</span>' : ''}
      </div>`;
    phasesEl.appendChild(div);
  });

  const rl = s.rate_limit || {};
  document.getElementById('rl-rps').textContent = (rl.current_rps || 0).toFixed(1);
  document.getElementById('rl-tokens').textContent = (rl.available_tokens || 0).toFixed(1);
  document.getElementById('rl-429').textContent = rl.recent_429_count || 0;

  const tg = s.telegram || {};
  document.getElementById('tg-sent').textContent = tg.sent || 0;
  document.getElementById('tg-fail').textContent = tg.dropped_on_failure || 0;
  document.getElementById('tg-dup').textContent = tg.dropped_on_dedupe || 0;

  const wtb = document.querySelector('#workers tbody');
  wtb.innerHTML = (s.workers || []).map(w =>
    `<tr><td>${w.worker_id}</td><td>${w.status}</td>
     <td>${w.current_task || '-'}</td><td>${w.elapsed_seconds.toFixed(1)}s</td></tr>`
  ).join('');

  const etb = document.querySelector('#events tbody');
  etb.innerHTML = (s.recent_events || []).slice(-20).reverse().map(e =>
    `<tr><td>${e.at.split('T')[1].slice(0,8)}</td>
     <td>${e.phase || '-'}</td><td>${e.kind}</td>
     <td class="stats">${e.message || e.item_id || ''}</td></tr>`
  ).join('');
}

function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onmessage = e => renderState(JSON.parse(e.data));
  ws.onclose = () => setTimeout(connect, 1500);
  ws.onerror = () => ws.close();
}

// Polling fallback for browsers where WebSocket fails
fetch('/api/v1/state').then(r => r.json()).then(renderState);
connect();

/*INSTRUMENTS_JS*/
</script>
</body>
</html>
"""


__all__ = [
    "DashboardState",
    "PhaseSnapshot",
    "RateLimitPanel",
    "SweepFunnelSnapshot",
    "TelegramStatsPanel",
    "WorkerRow",
    "build_app",
    "list_underlyings",
    "pull_candles",
    "snapshot_to_json",
    "universe_summary",
]
