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

import json
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import structlog
from pydantic import BaseModel

from brutex.dashboard.coverage_view import (
    build_coverage_payload,
    build_option_coverage_payload,
    build_spot_coverage_payload,
)
from brutex.dashboard.db_view import (
    list_candle_datasets,
    list_columns,
    list_option_datasets,
    list_option_series,
    query_candles,
    query_option_candles,
    query_table,
    sample_rows,
    summarize_option_datasets,
    summarize_spot_datasets,
    table_summary,
)
from brutex.dashboard.option_pull_controller import pull_options
from brutex.dashboard.preopen_report import (
    ReportBasis,
    ReportMode,
    build_preopen_rows,
    render_report_zip,
    render_xlsx,
    resolve_report_datasets,
)
from brutex.dashboard.pull_controller import (
    SessionRateController,
    dhan_creds_present,
    pull_all_spot,
    pull_candles,
    resolve_pull_symbols,
)
from brutex.dashboard.pull_jobs import BackgroundPullJob, ProgressHook
from brutex.dashboard.request_logging import emit_request_log
from brutex.dashboard.summary_cache import OPTIONS, SPOT, SummaryCache
from brutex.dashboard.universe_pull import select_pull_instruments
from brutex.dashboard.universe_query import list_underlyings, universe_summary
from brutex.data.timeframe_materialize import (
    INTRADAY_TF_INTERVALS,
    MATERIALIZABLE_TFS,
    materialize_timeframes,
)
from brutex.models import Exchange, Instrument, InstrumentType
from brutex.observability.correlation import request_id_scope
from brutex.observability.throttle import make_throttled
from brutex.orchestrator.pipeline import (
    PipelineEvent,
    PipelineEventKind,
)
from brutex.orchestrator.state import IngestPhase
from brutex.universe import derive_lake_underlyings, is_option_pullable

#: Module logger — every material dashboard action (especially a pull that
#: fails) lands in the same ``.brutex-logs/brutex.log`` JSONL the operator
#: reads, so a 400 is never a silent dead-end (DEC-PULL-ERROR-VISIBILITY-001).
_LOG = structlog.get_logger("brutex.dashboard")

#: A structured-query filter is the 3-token form ``column:op:value`` (the
#: value may itself contain ``:``). Named to avoid a PLR2004 magic number.
_FILTER_TOKEN_COUNT = 3


# pydantic v2 BaseModel trips pyright strict `reportUntypedBaseClass` — a known
# false positive (the base IS typed); silence per-site so the gate stays honest
# everywhere else (DEC-PYRIGHT-UNMASK-001).
class CredentialsIn(BaseModel):  # pyright: ignore[reportUntypedBaseClass]
    """Request body for ``POST /api/v1/credentials``.

    Defined at **module scope** (not inside :func:`build_app`) so FastAPI
    correctly resolves it as a request-BODY model. A function-local Pydantic
    model is not recognised as a body by FastAPI's parameter analysis — it
    is mis-classified as a query parameter (``loc: ["query", "body"]``),
    which made every POST return 422 (DEC-CREDS-422-FIX-001). The fields are
    accepted in the BODY (never as query params, which leak into access
    logs) and are passed straight to
    :func:`brutex.config.credentials.save_dhan_credentials` — never logged.
    """

    access_token: str
    client_id: str


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
    """Rate-limiter snapshot (current + effective RPS, tokens, recent 429s, latency).

    ``current_rps`` is the controller's *target* pace (the adaptive [3..5] ladder);
    ``effective_rps`` is the *measured* inter-arrival rate from the transport meter
    (calls ÷ wall-span, never overstating) — together they answer the operator's
    "is 5 r/s actually working?" at a glance (DEC-DASHBOARD-RATE-PANEL-001).
    """

    current_rps: float = 0.0
    available_tokens: float = 0.0
    recent_429_count: int = 0
    effective_rps: float = 0.0
    latency_ms_p95: float = 0.0


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

    def update_throughput(
        self,
        *,
        effective_rps: float,
        latency_ms_p95: float,
    ) -> None:
        """Update the MEASURED throughput half of the rate panel from a ``CallMeter`` snapshot.

        ``effective_rps`` is the transport's inter-arrival rate (calls ÷ wall-span)
        and ``latency_ms_p95`` its 95th-percentile round-trip — both straight from
        :meth:`brutex.adapters.dhan.client.CallMeter.snapshot` (DEC-DASHBOARD-RATE-PANEL-001).
        """
        with self._lock:
            self.rate_limit.effective_rps = effective_rps
            self.rate_limit.latency_ms_p95 = latency_ms_p95

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


def make_rate_observer(
    state: DashboardState,
) -> Callable[[Mapping[str, Any], Mapping[str, Any]], None]:
    """Build a callback that pushes a controller + meter snapshot to *state*'s rate panel.

    Threaded (live-only) into the pull controllers' rate feedback so a live pull's
    adaptive rate (:meth:`~brutex.adapters.dhan.rate_control.DhanRateController.snapshot`
    → ``current_rps`` + ``recent_hits``) and measured throughput
    (:meth:`~brutex.adapters.dhan.client.CallMeter.snapshot` → ``effective_rps`` +
    ``latency_ms_p95``) flow to the dashboard rate panel (DEC-DASHBOARD-RATE-PANEL-001).
    Pure + testable: drive it with two snapshot mappings and assert the panel updated.
    """

    def _observe(rate: Mapping[str, Any], meter: Mapping[str, Any]) -> None:
        """Map one (rate-controller, call-meter) snapshot pair onto the rate panel."""
        state.update_rate_limit(
            current_rps=float(rate.get("current_rps", 0.0)),
            available_tokens=float(rate.get("available_tokens", 0.0)),
            recent_429_count=int(rate.get("recent_hits", 0)),
        )
        state.update_throughput(
            effective_rps=float(meter.get("effective_rps", 0.0)),
            latency_ms_p95=float(meter.get("latency_ms_p95", 0.0)),
        )

    return _observe


def build_app(
    state: DashboardState,
    *,
    instrument_store: Any = None,
    summary_cache: SummaryCache | None = None,
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
        section so a user can literally see what Dhan ingest
        pulled. When ``None`` (the default), those endpoints return
        503 and the HTML section stays hidden \u2014 the pipeline
        progress cards still work.
    summary_cache
        Optional :class:`~brutex.dashboard.summary_cache.SummaryCache`
        backing the O(1) coverage polls (DEC-DASHBOARD-FASTREAD-001).
        Injected so a test can disable the 2 s TTL floor
        (``SummaryCache(ttl_floor_s=0.0)``) and assert the write-path
        invalidation deterministically; ``None`` (the default) builds a
        fresh production cache.

    Returns
    -------
    fastapi.FastAPI
        Already-wired ASGI app. Caller mounts it on their uvicorn
        server of choice.
    """
    try:
        from fastapi import FastAPI, HTTPException, Query
        from fastapi.concurrency import run_in_threadpool
        from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
    except ImportError as exc:
        raise RuntimeError(
            "FastAPI is not installed. Install with `pip install "
            "fastapi uvicorn` to run the dashboard. Unit tests for the "
            "state model do not need FastAPI."
        ) from exc

    from brutex.observability.metrics import get_registry, render_prometheus

    app = FastAPI(title="BruteX Dashboard", version="0.1.0a0")

    @app.middleware("http")
    async def _log_request(request: Any, call_next: Any) -> Any:  # pragma: no cover — FastAPI glue
        """Per-request structured log + request-id correlation (DEC-DASHBOARD-REQUEST-LOGGING-001).

        Binds a ``request_id`` (an inbound ``X-Request-ID`` header, else a fresh
        one) for the request's whole context so any downstream event inherits it,
        times the handler, and emits ONE ``dashboard.request`` event via
        :func:`brutex.dashboard.request_logging.emit_request_log` (high-frequency
        polls at DEBUG, everything else at INFO — so the firehose stops drowning
        the business events). The resolved id is echoed in the ``X-Request-ID``
        response header. The emit runs in a ``finally`` so a handler that RAISES
        is still logged (status 500) before the error propagates; the header is
        set only when a response exists. The per-request pure logic + level policy
        live in the 100%-tested ``request_logging`` module — this is the thin glue.
        """
        inbound = request.headers.get("x-request-id")
        with request_id_scope(inbound) as rid:
            start = time.perf_counter()
            status_code = 500
            response = None
            try:
                response = await call_next(request)
                status_code = response.status_code
                return response
            finally:
                emit_request_log(
                    method=request.method,
                    path=request.url.path,
                    status_code=status_code,
                    latency_ms=(time.perf_counter() - start) * 1000.0,
                    request_id=rid,
                    client=request.client.host if request.client else None,
                )
                if response is not None:
                    response.headers["X-Request-ID"] = rid

    # ── Fast-read SPEED CORE (DEC-DASHBOARD-FASTREAD-001) ───────────────────
    # Every blocking lake/DuckDB read below is offloaded off the event loop via
    # `run_in_threadpool(fn, ...)` so a single full-lake scan (or a read queued
    # behind the active writer's store lock) can never freeze the whole async
    # server. CRITICAL: `store.connection` returns a fresh `_LockedReadConn`
    # whose RLock MUST be acquired+released on the SAME thread, so the offloaded
    # callable builds + uses `store.connection` INSIDE the worker thread (we pass
    # `store`, never a pre-built conn). The summary cache + incremental coverage
    # counters are owned by this app instance (no module-global mutable state —
    # CLAUDE.md §4) and injected into the routes / the background pull job below.
    _summary_cache = summary_cache if summary_cache is not None else SummaryCache()
    # ONE rate controller per dashboard session, shared across every pull
    # (DEC-DHAN-PERSIST-RATE-001) so the learned rate carries over instead of each
    # pull restarting at the 5 r/s ceiling and re-storming Dhan.
    _session_rate = SessionRateController()

    @app.get("/api/v1/state")
    async def api_state() -> JSONResponse:
        """GET /api/v1/state — JSON snapshot of the current dashboard state."""
        return JSONResponse(state.to_dict())

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        """GET /favicon.ico — 204 No Content (DEC-DASHBOARD-FAVICON-001).

        Browsers auto-request ``/favicon.ico`` on every page load; with no
        route each one logged a noisy ``404`` (visible in the operator's
        ``make run`` log). 204 is the canonical "no icon" answer — quieter
        than a 404 and it suppresses the browser's retry.
        """
        return Response(status_code=204)

    @app.get("/api/v1/rate")
    async def api_rate() -> JSONResponse:
        """GET /api/v1/rate — O(1) in-memory rate panel (target + effective r/s, 429s, latency).

        A dedicated, lock-light poll for the live rate panel: returns the
        :class:`RateLimitPanel` snapshot (``current_rps`` target vs ``effective_rps``
        measured, ``recent_429_count``, ``latency_ms_p95``), populated during a live
        pull by :func:`make_rate_observer` (DEC-DASHBOARD-RATE-PANEL-001). A pure
        in-memory dict copy — never touches the lake or the store lock.
        """
        return JSONResponse(asdict(state.rate_limit))

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
        stats = await run_in_threadpool(_collect_store_stats, store)
        return JSONResponse(stats)

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
        rows = await run_in_threadpool(
            lambda: _query_instruments(
                store,
                underlying=underlying,
                instrument_type=instrument_type,
                segment=segment,
                limit=limit,
                offset=offset,
            )
        )
        return JSONResponse({"instruments": rows, "limit": limit, "offset": offset})

    @app.get("/api/v1/expiries")
    async def api_expiries_list(
        underlying: str | None = Query(default=None),
    ) -> JSONResponse:
        """GET /api/v1/expiries - distinct expired-option (flag, code) series.

        Repointed (DEC-EXPIRED-OPTIONS-TABLE-001) from the dropped
        ``expiries`` DuckDB table to the expired-options Parquet lake
        (``<lake>/options/...``): returns the DISTINCT ``(expiry_flag,
        expiry_code)`` series present in the lake, filtered by ``underlying``
        when supplied. Returns ``[]`` when no lake root is wired or the lake
        is empty.
        """
        store = _require_store()
        lake_root = getattr(store, "path", None)
        if not isinstance(lake_root, Path):
            return JSONResponse({"expiries": []})
        lake = lake_root.parent
        series = await run_in_threadpool(
            lambda: list_option_series(store.connection, lake, underlying_symbol=underlying)
        )
        seen: set[tuple[str, str, int | None]] = set()
        expiries: list[dict[str, Any]] = []
        for s in series:
            key = (s["underlying_symbol"], s["expiry_flag"], s["expiry_code"])
            if key in seen:
                continue
            seen.add(key)
            expiries.append(
                {
                    "exchange": s["exchange"],
                    "underlying_symbol": s["underlying_symbol"],
                    "expiry_flag": s["expiry_flag"],
                    "expiry_code": s["expiry_code"],
                }
            )
        return JSONResponse({"expiries": expiries})

    @app.get("/api/v1/contracts")
    async def api_contracts_list(
        underlying: str | None = Query(default=None),
        expiry_code: int | None = Query(default=None),
        limit: int = Query(default=500, ge=1, le=5000),
    ) -> JSONResponse:
        """GET /api/v1/contracts - distinct expired-option series (the chain).

        Repointed (DEC-EXPIRED-OPTIONS-TABLE-001) from the dropped
        ``contracts`` DuckDB table to the expired-options Parquet lake: returns
        the DISTINCT ``(strike_offset, side, strike)`` series for the requested
        ``underlying`` (optionally narrowed to one ``expiry_code``), derived
        from the addressing columns now stamped on every bar. Returns ``[]``
        when no lake root is wired or the lake is empty.

        Query parameters
        ----------------
        underlying
            Filter by underlying symbol (e.g. ``NIFTY``).
        expiry_code
            Narrow to one expiry selector (Near/Next/Far).
        limit
            Server-side cap on returned series rows.
        """
        store = _require_store()
        lake_root = getattr(store, "path", None)
        if not isinstance(lake_root, Path):
            return JSONResponse({"contracts": [], "limit": limit})
        lake = lake_root.parent
        series = await run_in_threadpool(
            lambda: list_option_series(store.connection, lake, underlying_symbol=underlying)
        )
        rows = [
            {
                "exchange": s["exchange"],
                "underlying_symbol": s["underlying_symbol"],
                "expiry_flag": s["expiry_flag"],
                "expiry_code": s["expiry_code"],
                "strike_offset": s["strike_offset"],
                "side": s["side"],
                "strike": s["strike"],
            }
            for s in series
            if expiry_code is None or s["expiry_code"] == expiry_code
        ][:limit]
        return JSONResponse({"contracts": rows, "limit": limit})

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
        reference_only_ids = store.reference_only_security_ids()
        try:
            rows = list_underlyings(
                candidates,
                kind=kind,
                fno_eligible=eligible,
                reference_only_ids=reference_only_ids,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse({"instruments": rows, "kind": kind})

    @app.get("/api/v1/universe/index-constituents")
    async def api_universe_index_constituents(
        symbol: str = Query(...),
        refresh: bool = Query(False),
    ) -> JSONResponse:
        """GET /api/v1/universe/index-constituents?symbol=…&refresh=… — cache-backed.

        Resolve an INDEX (or broad-market basket — NIFTY 100/200/500)
        *symbol* to its NSE constituent stocks, intersect them with the
        F&O-eligible single stocks we actually hold, and return only the
        pullable members — so the ``/universe`` page can auto-tick them in
        the multi-select (DEC-CONSTITUENTS-CACHE-001, builds on
        DEC-UNIVERSE-NSE-CONSTITUENTS-001 / MIGRATION.md #445).

        Cache-backed (the "Auto-fetch + cache" the operator chose): the
        first successful NSE fetch is written to
        ``<lake>/index_constituents/<INDEX>.json`` and every later call —
        even when the live fetch is blocked / timed out — serves the cached
        list (:mod:`brutex.dashboard.constituents_cache`). ``refresh=true``
        forces a fresh fetch (use after a semi-annual rebalance), falling
        back to the stale cache if the refresh fails.

        Honesty (CLAUDE.md §1 / §10): this is **NOT Dhan data**. Index
        membership comes from NSE's per-index constituent CSVs. NSE
        bot-blocks those endpoints and this build environment
        network-blocks ``nseindia.com``, so the live fetch is UNVERIFIABLE
        in CI and runs only on the operator's machine. It degrades
        gracefully and **never 500s**. ``source`` is one of ``"nse"``
        (fresh + cached), ``"cache"`` (served from a prior fetch, with
        ``as_of``), ``"unknown-index"`` (no NSE mapping), or
        ``"unavailable"`` (live fetch failed AND no cache yet). The blocking
        resolve (disk cache + best-effort live fetch) is offloaded via
        ``run_in_threadpool`` so it never freezes the event loop
        (DEC-DASHBOARD-PULL-ASYNC-001).
        """
        from brutex.dashboard.constituents_cache import resolve_constituents
        from brutex.dashboard.nse_constituents import intersect_with_universe

        store = _require_store()
        eligible = store.fno_eligible_underlyings()
        candidates = store.list_index_equity_instruments()
        # The bare engine symbols of the F&O-eligible single stocks we hold
        # (the exact rows the multi-select renders) — reuse the COMMON
        # classifier (CLAUDE.md §11) rather than re-deriving the split.
        stock_rows = list_underlyings(candidates, kind="stock", fno_eligible=eligible)
        fno_symbols = [row["symbol"] for row in stock_rows]

        lake_root = getattr(store, "path", None)
        if not isinstance(lake_root, Path):
            # No lake wired → no cache root → degrade honestly (never 500).
            return JSONResponse(
                {
                    "members": [],
                    "source": "unavailable",
                    "as_of": None,
                    "note": (
                        "No lake wired — constituents cache unavailable; "
                        "use 'Pull ALL F&O stocks' (the superset)."
                    ),
                }
            )
        # The cache lives beside the lake at <lake>/index_constituents/.
        result = await run_in_threadpool(
            resolve_constituents,
            symbol,
            cache_root=lake_root.parent,
            force_refresh=refresh,
        )
        members = intersect_with_universe(result.symbols, fno_symbols)
        notes = {
            "nse": (
                f"{len(members)} F&O-eligible constituent(s) of {symbol} ticked — "
                "fresh from NSE + cached (verify; membership rebalances)."
            ),
            "cache": (
                f"{len(members)} F&O-eligible constituent(s) of {symbol} ticked — "
                f"from cache (fetched {result.as_of}; pass refresh=true after a rebalance)."
            ),
            "unknown-index": (
                f"No NSE constituent list known for {symbol!r} — "
                "use 'Pull ALL F&O stocks' (the superset)."
            ),
            "unavailable": (
                "NSE constituents unavailable + no cache yet — "
                "use 'Pull ALL F&O stocks' (the superset)."
            ),
        }
        return JSONResponse(
            {
                "members": members,
                "source": result.source,
                "as_of": result.as_of,
                "note": notes[result.source],
            }
        )

    @app.post("/api/v1/universe/load-instruments")
    async def api_universe_load_instruments() -> JSONResponse:
        """POST /api/v1/universe/load-instruments — keylessly populate the list.

        Thin wrapper over
        :func:`brutex.dashboard.instrument_loader.load_instrument_master`.
        The instrument master is a PUBLIC CSV needing NO Dhan token, so this
        endpoint can fill the empty ``/universe`` list without authentication
        (options / candles still need the token, separately). On any
        network / parse failure (the live fetch can't reach the Dhan asset
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
                detail=f"couldn't reach Dhan asset host / parse instrument master: {exc}",
            ) from exc
        return JSONResponse(counts)

    @app.post("/api/v1/universe/pull")
    async def api_universe_pull(
        symbol: str = Query(...),
        year: int | None = Query(default=None),
        from_date: str | None = Query(default=None),
        to_date: str | None = Query(default=None),
    ) -> JSONResponse:
        """POST /api/v1/universe/pull — ingest one instrument's candles.

        Thin wrapper over
        :func:`brutex.dashboard.pull_controller.pull_candles`. ``symbol`` is
        the engine ``symbol_name`` the universe grid sends (ADR-0003 dropped
        the old exchange-prefixed ``vendor_symbol``); it is resolved to an
        :class:`~brutex.models.Instrument` via the instrument store (the
        controller is instrument-agnostic). An unknown ``symbol`` surfaces as
        a 400 (DEC-PULL-ERROR-VISIBILITY-001).
        ``from_date`` / ``to_date`` are optional inclusive ISO
        ``YYYY-MM-DD`` bounds (the from/to candle picker); when either is
        given they select an explicit, clamped ``[2021-01-01, today]``
        range (a blank ``to_date`` defaults to the previous day — today is
        included only via an explicit ``to_date`` reaching it), otherwise
        the full calendar ``year``, else (no dates, no year) the full
        Track-1 history ``[2021-01-01, previous day]``. A malformed date or
        a ``from`` after ``to`` surfaces as a 400 with the real reason
        (DEC-PULL-ERROR-VISIBILITY-001).
        ``dry_run`` is forced ``True`` automatically when Dhan
        credentials are not present in the environment (via the testable
        :func:`brutex.dashboard.pull_controller.dhan_creds_present`
        gate, mirroring the CLI's offline default), so the page never
        attempts a live network call without creds.
        """
        dry_run = not dhan_creds_present()
        _LOG.info(
            "universe.pull.requested",
            symbol=symbol,
            year=year,
            from_date=from_date,
            to_date=to_date,
            dry_run=dry_run,
        )
        store = _require_store()
        instrument = store.get(symbol)
        if instrument is None:
            raise HTTPException(
                status_code=400,
                detail=f"unknown security_id {symbol!r} — not in the instrument store",
            )
        try:
            # Run the (blocking, often 20-30 s) single-symbol pull OFF the event
            # loop. This route is ``async`` but called the blocking ``pull_candles``
            # directly, so for the WHOLE pull every other request — /db, /coverage,
            # /summary — sat queued behind the frozen event loop (the logs showed
            # `GET /api/v1/db/tables latency_ms≈32796` during a 32 s NIFTY pull).
            # Offloading to the threadpool (mirroring every other route here) frees
            # the loop so the rest of the dashboard stays responsive DURING a pull
            # (DEC-DASHBOARD-PULL-ASYNC-001). The vendor-bound pull duration itself
            # is unchanged (Dhan rate-limit), but it no longer blocks the UI.
            report = await run_in_threadpool(
                pull_candles,
                instrument,
                lake_root=_resolve_pull_lake_root(instrument_store, dry_run=dry_run),
                year=year,
                from_date=from_date,
                to_date=to_date,
                dry_run=dry_run,
                rate_observer=make_rate_observer(state),
                session_rate=_session_rate,
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
        # Invalidate the coverage SummaryCache so the next /coverage poll reflects
        # the just-written bars. This single-symbol pull runs SYNCHRONOUSLY here
        # (not via the background _pull_job — the ONLY other writer that marks the
        # cache dirty), so without this a one-off pull (e.g. pulling just SENSEX)
        # leaves /coverage serving the stale cold-start snapshot: the "coverage
        # shows 0 series / 0 bars after I pulled" bug
        # (DEC-DASHBOARD-COVERAGE-INVALIDATE-001). mark_dirty is O(1); the cache's
        # 2 s TTL floor still bounds how often the next read actually rescans.
        _summary_cache.mark_dirty()
        return JSONResponse(report.to_dict())

    def _select_bulk_targets(kind: str) -> list[Instrument]:
        """Resolve every admitted spot underlying of *kind* (index / stock).

        Reuses the cheap candidates-only fast path + the single COMMON
        classifier (DEC-INSTRUMENT-UNIVERSE-FAST-001) so a bulk pull selects
        exactly the universe the grid shows. A bad *kind* surfaces as a 400.
        """
        store = _require_store()
        eligible = store.fno_eligible_underlyings()
        candidates = store.list_index_equity_instruments()
        try:
            return select_pull_instruments(candidates, kind=kind, fno_eligible=eligible)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    def _is_option_pullable(instrument: Instrument) -> bool:
        """Whether *instrument* has a pullable option chain (DEC-OPTIONS-PULL-GUARD-001).

        Reads the lake's F&O-eligible set + the ``reference_only`` id set and
        delegates to the single source of truth
        :func:`brutex.universe.is_option_pullable` — so NTM + its reference-only /
        non-F&O constituents + India VIX can never start an options pull, while
        F&O-eligible stocks (even NTM constituents) keep their options.
        """
        store = _require_store()
        return is_option_pullable(
            instrument, reference_only_ids=store.reference_only_security_ids()
        )

    def _reject_non_option_pullable(instruments: list[Instrument]) -> None:
        """Fail CLOSED (400) if any EXPLICITLY-requested underlying has no options.

        For ``?symbol=`` / ``?symbols=`` (a deliberate selection) a non-pullable
        target — NTM, a reference-only non-F&O constituent, or India VIX — is a
        user error, so we reject the whole request naming the offenders rather
        than silently pulling nothing for them (DEC-OPTIONS-PULL-GUARD-001).
        """
        store = _require_store()
        reference_only_ids = store.reference_only_security_ids()
        blocked = [
            inst.symbol_name
            for inst in instruments
            if not is_option_pullable(inst, reference_only_ids=reference_only_ids)
        ]
        if blocked:
            raise HTTPException(
                status_code=400,
                detail=(
                    "option chain is not available for reference-only / non-F&O "
                    f"underlyings: {', '.join(sorted(blocked))} — these (e.g. NIFTY "
                    "Total Market + its non-F&O constituents, India VIX) carry no "
                    "options; select an F&O-eligible underlying instead"
                ),
            )

    # The single-flight background pull job that powers the LIVE feed
    # (DEC-PULL-PROGRESS-LIVE-001): one universe pull at a time across the whole
    # server; the page polls /pull-progress for the per-unit tick. Owned by this
    # app instance (no module-global mutable state — CLAUDE.md §4). It is wired
    # with the app's SummaryCache so each window write invalidates the coverage
    # summary (the next eligible poll refreshes), and owns the incremental
    # CoverageCounters that back the O(1) coverage headline
    # (DEC-DASHBOARD-FASTREAD-001).
    _pull_job = BackgroundPullJob(summary_cache=_summary_cache)

    @app.post("/api/v1/universe/pull-all")
    async def api_universe_pull_all(
        kind: str | None = Query(default=None),
        symbol: str | None = Query(default=None),
        symbols: str | None = Query(default=None),
        year: int | None = Query(default=None),
        from_date: str | None = Query(default=None),
        to_date: str | None = Query(default=None),
    ) -> JSONResponse:
        """POST /api/v1/universe/pull-all — start a bulk / multi / per-symbol SPOT pull.

        Backs BOTH the one-click "⤓ Pull ALL indices / stocks spot" buttons
        (``?kind=index|stock``, full ``[2021-01-01, previous day]`` 1m + 1d) AND the
        unified multi-select "⤓ Pull spot" (``?symbols=a,b,c`` — the ticked
        SELECTION fanned in ONE saturating pass over the from/to picker range,
        DEC-DHAN-SPOT-MULTI-SATURATE-001). Precedence: ``symbols`` (ticked many) >
        ``symbol`` (one) > ``kind`` (every admitted underlying of index/stock).

        All forms route to :func:`brutex.dashboard.pull_controller.pull_all_spot`
        on the **background job** (returns at once; poll ``/pull-progress``): its
        cross-symbol ``(symbol, interval, window)`` fan-out keeps the ONE shared
        :class:`SessionRateController` SATURATED at the 5 r/s ceiling, replacing the
        old serial per-symbol ``/universe/pull`` loop that only kept ~3 r/s in
        flight. ``from_date`` / ``to_date`` / ``year`` carry the date picker; when
        ALL are blank the full Track-1 history applies (the Pull-ALL default, so
        the ``?kind=`` callers are byte-for-byte unchanged). Single-flight; an empty
        / unknown ``?symbols=`` id fails CLOSED (400, naming offenders — never a
        silent partial). ``dry_run`` is forced when Dhan creds are absent.
        """
        dry_run = not dhan_creds_present()
        if symbols is not None:
            try:
                instruments = resolve_pull_symbols(symbols, _require_store().get)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            detail = f"{len(instruments)} symbols spot"
        elif symbol is not None:
            store = _require_store()
            instrument = store.get(symbol)
            if instrument is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"unknown security_id {symbol!r} — not in the instrument store",
                )
            instruments = [instrument]
            detail = f"{symbol} spot"
        elif kind is not None:
            instruments = _select_bulk_targets(kind)
            detail = f"{kind} spot"
        else:
            raise HTTPException(
                status_code=400,
                detail="pull-all requires ?symbols=..., ?symbol=... or ?kind=...",
            )
        lake_root = _resolve_pull_lake_root(instrument_store, dry_run=dry_run)
        _LOG.info(
            "universe.pull_all.requested",
            kind=kind,
            symbol=symbol,
            symbols=symbols,
            count=len(instruments),
            from_date=from_date,
            to_date=to_date,
            year=year,
            dry_run=dry_run,
        )

        def _work(on_progress: ProgressHook) -> object:  # pragma: no cover — runs on the job thread
            """Background body: bulk-pull every selected spot symbol, streaming progress."""
            return pull_all_spot(
                instruments,
                lake_root=lake_root,
                dry_run=dry_run,
                from_date=from_date,
                to_date=to_date,
                year=year,
                on_progress=on_progress,
                rate_observer=make_rate_observer(state),
                session_rate=_session_rate,
            )

        # total = symbols × the 2 fetched intervals (1m + 1d) — the unit the hook fires on.
        started = _pull_job.start(_work, total=len(instruments) * 2, detail=detail)
        return JSONResponse(
            {
                "started": started,
                "kind": kind,
                "symbol": symbol,
                "symbols": symbols,
                "total": len(instruments) * 2,
                "dry_run": dry_run,
            }
        )

    @app.post("/api/v1/universe/pull-options")
    async def api_universe_pull_options(
        kind: str | None = Query(default=None),
        symbol: str | None = Query(default=None),
        symbols: str | None = Query(default=None),
    ) -> JSONResponse:
        """POST /api/v1/universe/pull-options — start a multi / per-symbol / bulk OPTIONS pull.

        Backs the unified "⛓ Pull options chain" button (single OR ticked many)
        + the bulk-by-kind buttons. Launches
        :func:`brutex.dashboard.option_pull_controller.pull_options` on the
        **background job** (returns at once; poll ``/pull-progress``). Precedence:
        ``symbols`` (the ticked SELECTION — a comma list, fans every one in ONE
        pass; DEC-UNIVERSE-OPTIONS-MULTIPULL-001) > ``symbol`` (one underlying) >
        ``kind`` (every admitted underlying of index/stock). Exactly one form is
        required (else 400); an empty / unknown ``symbols`` id fails CLOSED (400,
        naming the offenders — never a silent partial pull). Single-flight +
        forced ``dry_run`` when Dhan creds are absent, exactly like the bulk pull.
        """
        dry_run = not dhan_creds_present()
        if symbols is not None:
            try:
                instruments = resolve_pull_symbols(symbols, _require_store().get)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            _reject_non_option_pullable(instruments)
        elif symbol is not None:
            store = _require_store()
            instrument = store.get(symbol)
            if instrument is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"unknown security_id {symbol!r} — not in the instrument store",
                )
            instruments = [instrument]
            _reject_non_option_pullable(instruments)
        elif kind is not None:
            # Bulk-by-kind: silently SKIP non-option-pullable underlyings (NTM +
            # its reference-only / non-F&O constituents + VIX) rather than fail —
            # a bulk pull is a "pull everything that has options" action.
            instruments = [i for i in _select_bulk_targets(kind) if _is_option_pullable(i)]
        else:
            raise HTTPException(
                status_code=400,
                detail="pull-options requires ?symbols=..., ?symbol=... or ?kind=...",
            )
        lake_root = _resolve_pull_lake_root(instrument_store, dry_run=dry_run)
        # A human-friendly label for the live panel: the count for a multi-select,
        # else the single symbol / kind.
        label = f"{len(instruments)} underlyings" if symbols is not None else (kind or symbol)
        _LOG.info(
            "universe.pull_options.requested",
            kind=kind,
            symbol=symbol,
            symbols=symbols,
            count=len(instruments),
            dry_run=dry_run,
        )

        def _work(on_progress: ProgressHook) -> object:  # pragma: no cover — runs on the job thread
            """Background body: fan the options pull over the targets, streaming progress."""
            return pull_options(
                instruments,
                lake_root=lake_root,
                dry_run=dry_run,
                on_progress=on_progress,
                # Live status (DEC-OPTIONS-LIVE-STATUS-001): a THROTTLED per-series
                # summary-cache invalidation so /coverage/options ticks live during
                # a single-underlying pull (whose per-underlying on_progress fires
                # only at the very end). The 2 s throttle matches the cache TTL
                # floor; the per-underlying on_progress already marks dirty too.
                on_series_tick=make_throttled(_summary_cache.mark_dirty),
                session_rate=_session_rate,
            )

        # total = one progress tick per underlying (the options hook fires per underlying).
        started = _pull_job.start(_work, total=len(instruments), detail=f"{label} options")
        return JSONResponse(
            {
                "started": started,
                "kind": kind,
                "symbol": symbol,
                "symbols": symbols,
                "total": len(instruments),
                "dry_run": dry_run,
            }
        )

    @app.get("/api/v1/universe/pull-progress")
    async def api_universe_pull_progress() -> JSONResponse:
        """GET /api/v1/universe/pull-progress — the LIVE per-unit pull snapshot.

        Returns the background job's O(1)
        :meth:`brutex.dashboard.pull_jobs.BackgroundPullJob.snapshot` — ``state``
        (idle/running/done/error), ``total`` / ``completed`` / ``failed`` /
        ``bars`` / ``percent`` / ``current``. The page polls this while a pull
        runs to tick the counters window-by-window (DEC-PULL-PROGRESS-LIVE-001).
        """
        return JSONResponse(_pull_job.snapshot().to_dict())

    @app.post("/api/v1/universe/derive-timeframes")
    async def api_universe_derive_timeframes(
        symbol: str = Query(...),
        tfs: str | None = Query(default=None),
    ) -> JSONResponse:
        """POST /api/v1/universe/derive-timeframes — MANUAL derived-TF materialise.

        Resamples the symbol's STORED 1-minute bars to each selected derived
        intraday timeframe and writes each as its OWN stored interval partition,
        so the brute-force sweep then reads a 5m / 15m / 60m bar in O(1) (a
        direct Parquet read) instead of re-resampling 1m ~33 billion times
        (DEC-TF-MATERIALIZE-001). **Manual trigger only** — never automatic.

        Query parameters
        ----------------
        symbol
            The engine ``symbol_name`` / Dhan ``SECURITY_ID`` the universe grid
            sends; resolved to an instrument (and its lake exchange/segment/
            symbol partition coordinates) via the store. An unknown symbol is a
            400 (DEC-PULL-ERROR-VISIBILITY-001).
        tfs
            A comma-separated list of TF labels (e.g. ``5m,15m,60m``). Omitted ⇒
            every materialisable timeframe (:data:`MATERIALIZABLE_TFS`). A
            requested label outside the storable set is reported in ``skipped``,
            never written.

        Returns
        -------
        JSON ``{"written": {tf: bars}, "skipped": [...], "total_bars": <int>}``.
        """
        store = _require_store()
        instrument = store.get(symbol)
        if instrument is None:
            raise HTTPException(
                status_code=400,
                detail=f"unknown security_id {symbol!r} — not in the instrument store",
            )
        # Parse the comma list (blank entries dropped); omitted ⇒ all storable.
        requested: tuple[str, ...] = (
            tuple(t for t in (s.strip() for s in tfs.split(",")) if t)
            if tfs is not None
            else MATERIALIZABLE_TFS
        )
        lake_root = _resolve_pull_lake_root(instrument_store, dry_run=False)
        exchange = instrument.exch_id.value
        segment = instrument.segment.value
        sym = instrument.symbol_name
        _LOG.info(
            "universe.derive_timeframes.requested",
            symbol=sym,
            exchange=exchange,
            segment=segment,
            tfs=list(requested),
        )
        try:
            result = materialize_timeframes(
                tfs=requested,
                read_1m=_make_read_1m(
                    lake_root=lake_root, exchange=exchange, segment=segment, symbol=sym
                ),
                write_tf=_make_write_tf(
                    lake_root=lake_root, exchange=exchange, segment=segment, symbol=sym
                ),
            )
        except Exception as exc:  # surface the REAL reason, never swallow it
            _LOG.exception(
                "universe.derive_timeframes.failed",
                symbol=sym,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            raise HTTPException(status_code=400, detail=f"{type(exc).__name__}: {exc}") from exc
        _LOG.info(
            "universe.derive_timeframes.ok",
            symbol=sym,
            written=result.written,
            skipped=list(result.skipped),
            total_bars=result.total_bars,
        )
        # The just-materialised partitions are new datasets on disk; let the next
        # coverage poll rediscover them (mirrors the single-pull invalidation).
        _summary_cache.mark_dirty()
        return JSONResponse(
            {
                "written": result.written,
                "skipped": list(result.skipped),
                "total_bars": result.total_bars,
            }
        )

    @app.get("/api/v1/coverage/counters")
    async def api_coverage_counters() -> JSONResponse:
        """GET /api/v1/coverage/counters — the O(1) incremental coverage headline.

        Returns the background pull job's running, per-dataset-label bar tally
        (:meth:`brutex.dashboard.summary_cache.CoverageCounters.snapshot`) — a
        truly O(1) read (one dict copy, never a lake scan) fed by the SINGLE
        write-progress funnel (DEC-DASHBOARD-FASTREAD-001). ``bars`` is the
        POST-DEDUP newly-written count, so a duplicate re-pull leaves the
        headline unchanged (idempotent — CLAUDE.md §2 Rule 7). Not awaited on a
        threadpool: the snapshot is a pure in-memory O(1) copy, no blocking I/O.
        """
        return JSONResponse(_pull_job.counters.snapshot().to_dict())

    # ── Completeness ledger / Coverage (DEC-COVERAGE-LEDGER-001) ────

    @app.get("/api/v1/coverage")
    async def api_coverage() -> JSONResponse:
        """GET /api/v1/coverage — headline + per-cell coverage rows.

        Thin wrapper over
        :func:`brutex.dashboard.coverage_view.build_coverage_payload`. The
        expected universe×years grid is derived from the lake's IDX + EQ
        candidates (:func:`brutex.universe.derive_lake_underlyings`) × the
        Dhan history window (2021 → current year); the actual rows come
        from ``store.list_coverage()`` (indexed read). All projection logic
        lives in the (coverage-tested) ``coverage_view`` module.
        """
        store = _require_store()
        years = _coverage_years()

        def _build() -> dict[str, Any]:
            """Build the coverage payload off the event loop (threadpool worker)."""
            # Runs on a threadpool worker so the indexed coverage read + the
            # candidates scan never block the event loop, and the store lock the
            # synchronized methods take is acquired+released on this thread.
            # Scope guard (DEC-DHAN-COVERAGE-RESCOPE-001): keep every index (incl.
            # BSE SENSEX) but only NSE equities — BSE single-stock F&O is out of
            # scope (DEC-DHAN-UNIVERSE-TICKVAULT-PARITY-001). This drops stale
            # BSE-equity rows a pre-rescope lake may still carry so the coverage
            # grid matches the live universe even before a fresh re-load.
            candidates = [
                inst
                for inst in store.list_index_equity_instruments()
                if inst.instrument is InstrumentType.IDX or inst.exch_id is Exchange.NSE
            ]
            # derive_lake_underlyings yields (underlying, exchange) (the order the
            # universe ingest fan-out wants); the coverage reconciler / view want
            # (exchange, underlying) — swap so the expected grid keys line up with
            # the persisted ledger's (exchange, underlying_symbol, year) PK.
            expected = [
                (exchange, underlying)
                for underlying, exchange in derive_lake_underlyings(candidates)
            ]
            return build_coverage_payload(store.list_coverage(), expected, years)

        return JSONResponse(await run_in_threadpool(_build))

    @app.get("/api/v1/coverage/spot")
    async def api_coverage_spot() -> JSONResponse:
        """GET /api/v1/coverage/spot — per-spot-dataset bar counts + date span.

        Surfaces SPOT coverage alongside the options completeness ledger on the
        ``/coverage`` page (DEC-COVERAGE-SPOT-001). Discovers every spot candle
        partition set in the lake and reports its bar count + first/last date,
        delegating to :func:`brutex.dashboard.db_view.summarize_spot_datasets`
        (the lake read) + :func:`brutex.dashboard.coverage_view.build_spot_coverage_payload`
        (the pure projection). READ-ONLY — no pull logic.

        **Honest envelope (Golden Rule #10):** a dataset present in the lake
        counts as *pulled*; partial-date-range detection is an explicit
        follow-up, not claimed here. When no lake root is wired (a store opened
        without a filesystem path) the payload is an empty summary, never a 500.
        """
        store = _require_store()
        lake_root = getattr(store, "path", None)
        if not isinstance(lake_root, Path):
            return JSONResponse(build_spot_coverage_payload([]))
        lake = lake_root.parent

        def _recompute() -> list[dict[str, Any]]:
            """Recompute the spot-dataset summary (cache-miss / dirty path only)."""
            # The O(files) full-lake aggregate — runs ONLY on a cache miss / a
            # dirty read past the 2 s TTL floor, on a threadpool worker, and
            # builds + uses store.connection here so the store RLock stays on one
            # thread (DEC-DASHBOARD-FASTREAD-001).
            return summarize_spot_datasets(store.connection, lake)

        rows = await run_in_threadpool(_summary_cache.get_or_refresh, SPOT, recompute=_recompute)
        return JSONResponse(build_spot_coverage_payload(rows))

    @app.get("/api/v1/coverage/windows")
    async def api_coverage_windows() -> JSONResponse:
        """GET /api/v1/coverage/windows — per-window SUCCESS / EMPTY / FAILED status.

        The browsable per-window status record (DEC-WINDOW-STATUS-001): one row
        per fetch window with its status, attempt count, bars, and last error — so
        the operator SEES exactly which windows are stored (SUCCESS), genuinely
        empty (EMPTY), or failed and needing a re-pull (FAILED). READ-ONLY: loads
        the durable status sidecar (``<lake>/window_status.json``) the ingest
        writes; never runs a pull. Empty when no lake root is wired or no pull has
        recorded a window yet (never a 500).
        """
        from brutex.data.window_status_store import load_window_status

        store = _require_store()
        lake_root = getattr(store, "path", None)
        if not isinstance(lake_root, Path):
            return JSONResponse({"count": 0, "windows": []})
        lake = lake_root.parent

        def _load() -> dict[str, Any]:
            """Load the durable status sidecar + project it to the JSON payload (threadpool)."""
            log = load_window_status(lake)
            return {
                "count": log.count,
                "windows": [
                    {
                        "market": r.market,
                        "symbol": r.security_id,
                        "interval": r.interval,
                        "window_start": r.window_start,
                        "window_end": r.window_end,
                        "status": r.status,
                        "bars": r.bars,
                        "attempts": r.attempts,
                        "last_error": r.last_error,
                        "last_attempt_at": r.last_attempt_at,
                    }
                    for r in log.records()
                ],
            }

        return JSONResponse(await run_in_threadpool(_load))

    @app.get("/api/v1/coverage/options")
    async def api_coverage_options() -> JSONResponse:
        """GET /api/v1/coverage/options — per-underlying expired-options lake coverage.

        The options-side analog of ``/api/v1/coverage/spot`` (DEC-OPTIONS-COVERAGE-LAKE-001):
        replaces the dead legacy ``coverage_ledger`` (expiries/contracts) source
        — which the single expired-options pull never wrote — with the ACTUAL
        expired-options Parquet lake. Discovers every series per underlying and
        reports its series count + bar count + first/last date, delegating to
        :func:`brutex.dashboard.db_view.summarize_option_datasets` (the lake
        read) + :func:`brutex.dashboard.coverage_view.build_option_coverage_payload`
        (the pure projection). READ-ONLY — no pull logic.

        **Honest envelope (Golden Rule #10):** a series present in the lake
        counts as *pulled*; partial-date-range detection is an explicit
        follow-up, not claimed here. When no lake root is wired the payload is an
        empty summary, never a 500.
        """
        store = _require_store()
        lake_root = getattr(store, "path", None)
        if not isinstance(lake_root, Path):
            return JSONResponse(build_option_coverage_payload([]))
        lake = lake_root.parent

        def _recompute() -> list[dict[str, Any]]:
            """Recompute the option-dataset summary (cache-miss / dirty path only)."""
            # The O(files) full-lake aggregate — runs ONLY on a cache miss / a
            # dirty read past the 2 s TTL floor, on a threadpool worker, building
            # + using store.connection here so the store RLock stays on one
            # thread (DEC-DASHBOARD-FASTREAD-001).
            return summarize_option_datasets(store.connection, lake)

        rows = await run_in_threadpool(_summary_cache.get_or_refresh, OPTIONS, recompute=_recompute)
        return JSONResponse(build_option_coverage_payload(rows))

    @app.get("/coverage", response_class=HTMLResponse)
    async def coverage_page() -> str:
        """GET /coverage — the coverage hub (DEC-DASHBOARD-COVERAGE-SPLIT-001).

        The former combined three-section ledger was split into three focused
        pages; this hub links to each + shows the at-a-glance headline counts.
        """
        return _render_coverage_html()

    @app.get("/coverage/options", response_class=HTMLResponse)
    async def coverage_options_page() -> str:
        """GET /coverage/options — the focused expired-options coverage page
        (DEC-DASHBOARD-COVERAGE-SPLIT-001 / DEC-OPTIONS-COVERAGE-LAKE-001)."""
        return _render_expired_options_html()

    @app.get("/coverage/spot", response_class=HTMLResponse)
    async def coverage_spot_page() -> str:
        """GET /coverage/spot — the focused spot-coverage page
        (DEC-DASHBOARD-COVERAGE-SPLIT-001 / DEC-COVERAGE-SPOT-001)."""
        return _render_spot_coverage_html()

    @app.get("/coverage/windows", response_class=HTMLResponse)
    async def coverage_windows_page() -> str:
        """GET /coverage/windows — the focused fetch-windows page
        (DEC-DASHBOARD-COVERAGE-SPLIT-001 / DEC-WINDOW-STATUS-001)."""
        return _render_fetch_windows_html()

    # ── Pre-open report (DEC-PREOPEN-REPORT-001) ────────────────────────

    def _report_basis_literal(basis: str) -> ReportBasis:
        """Validate the ``?basis=`` query → the ``ReportBasis`` literal (400 on bad value).

        DEC-PREOPEN-MINUTE-BASIS-001: ``day`` (default — 1-day open + prior 1-day
        close) or ``minute`` (09:15 1-minute open + prior day's last 1-minute
        close). Mirrors the ``mode`` literal-narrowing pattern so mypy + pyright
        both narrow without a cast. ONE source of truth for the three endpoints.
        """
        if basis == "day":
            return "day"
        if basis == "minute":
            return "minute"
        raise HTTPException(status_code=400, detail="basis must be 'day' or 'minute'")

    @app.get("/api/v1/reports/preopen/symbols")
    async def api_preopen_symbols(basis: str = Query("day")) -> JSONResponse:
        """GET /api/v1/reports/preopen/symbols — spot symbols the report can cover.

        Returns every ``(exchange, segment, symbol)`` in the lake that has the
        partition the chosen ``basis`` reads (DEC-PREOPEN-MINUTE-BASIS-001): the
        ``1day`` partition for ``basis=day`` (default), else the ``1minute``
        partition for ``basis=minute``. The picker for the pre-open report panel.
        READ-ONLY; empty when no lake.
        """
        report_basis = _report_basis_literal(basis)
        store = _require_store()
        lake_root = getattr(store, "path", None)
        if not isinstance(lake_root, Path):
            return JSONResponse({"symbols": []})
        triples = resolve_report_datasets(lake_root.parent, basis=report_basis)
        return JSONResponse(
            {
                "symbols": [{"exchange": e, "segment": s, "symbol": y} for e, s, y in triples],
                "basis": report_basis,
            }
        )

    @app.get("/api/v1/reports/preopen.xlsx")
    async def api_preopen_xlsx(
        start: str = Query(..., alias="from"),
        end: str = Query(..., alias="to"),
        symbols: str | None = Query(None),
        basis: str = Query("day"),
    ) -> Response:
        """GET /api/v1/reports/preopen.xlsx — the NSE-style pre-open ``.xlsx`` download.

        Builds ``SYMBOL · PREV. CLOSE · IEP · CHNG · %CHNG`` for each requested
        spot symbol over the inclusive ``[from, to]`` date range, lake-derived
        (DEC-PREOPEN-REPORT-001) — the numbers match NSE to the paisa. *symbols*
        is an optional comma-separated filter; omitted ⇒ every report-eligible
        symbol in the lake. ``basis`` (DEC-PREOPEN-MINUTE-BASIS-001) selects the
        price scale: ``day`` (default — 1-day open + prior 1-day close) or
        ``minute`` (09:15 1-minute open + prior day's last 1-minute close).
        Returns a deterministic, dependency-free workbook as an attachment. A 400
        is raised for a malformed / inverted date range or an unknown ``basis``.
        """
        report_basis = _report_basis_literal(basis)
        store = _require_store()
        try:
            start_d = date.fromisoformat(start)
            end_d = date.fromisoformat(end)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"bad date: {exc}") from exc
        if start_d > end_d:
            raise HTTPException(status_code=400, detail="'from' must be on or before 'to'")
        wanted = [s for s in (symbols or "").split(",") if s.strip()] or None
        lake_root = getattr(store, "path", None)

        def _build_rows(lake: Path) -> list[Any]:
            """Build the pre-open report rows off the event loop (threadpool worker)."""
            # Offloaded: the dataset discovery (filesystem walk) + the per-symbol
            # DuckDB aggregate run on a threadpool worker, building store.connection
            # here so the store RLock stays on this thread (DEC-DASHBOARD-FASTREAD-001).
            return build_preopen_rows(
                store.connection,
                lake,
                datasets=resolve_report_datasets(lake, wanted, basis=report_basis),
                start=start_d,
                end=end_d,
                basis=report_basis,
            )

        rows = (
            await run_in_threadpool(_build_rows, lake_root.parent)
            if isinstance(lake_root, Path)
            else []
        )
        filename = f"preopen_{start_d.isoformat()}_{end_d.isoformat()}.xlsx"
        title = f"Pre-Open Market: {start_d.isoformat()} to {end_d.isoformat()}"
        return Response(
            content=render_xlsx(rows, title=title),
            media_type=("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/api/v1/reports/preopen.zip")
    async def api_preopen_zip(
        start: str = Query(..., alias="from"),
        end: str = Query(..., alias="to"),
        symbols: str | None = Query(None),
        mode: str = Query("all"),
        min_pct: float = Query(0.0),
        basis: str = Query("day"),
    ) -> Response:
        """GET /api/v1/reports/preopen.zip — the two-category pre-open report ``.zip``.

        The Part-B export (DEC-REPORTS-EXCEL-CATEGORIES-001): builds the
        ``DATE · SYMBOL · PREV. CLOSE · IEP · CHNG · %CHNG`` rows for the
        requested spot symbols over ``[from, to]`` (lake-derived), then packs a
        ``.zip`` holding **one ``.xlsx`` per year**, each with **one worksheet
        per month** and per-row green/red fills by ``%CHNG`` sign.

        * ``mode=all`` (default) — every selected symbol, every day, sorted
          gainers→losers within each day; *min_pct* is ignored.
        * ``mode=topmover`` — per day, the single biggest absolute mover whose
          ``|%CHNG| ≥ min_pct``; days with no qualifier are skipped.

        *symbols* is an optional comma-separated filter (omitted ⇒ every
        report-eligible symbol). A 400 is raised for a malformed / inverted date
        range, an unknown *mode*, or a negative *min_pct*.
        """
        store = _require_store()
        try:
            start_d = date.fromisoformat(start)
            end_d = date.fromisoformat(end)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"bad date: {exc}") from exc
        if start_d > end_d:
            raise HTTPException(status_code=400, detail="'from' must be on or before 'to'")
        # Assign the validated mode as a literal so BOTH mypy and pyright narrow
        # it to ReportMode without a cast (pyright's reportUnnecessaryCast errors
        # on a cast it has already narrowed; mypy needs the literal assignment).
        if mode == "all":
            report_mode: ReportMode = "all"
        elif mode == "topmover":
            report_mode = "topmover"
        else:
            raise HTTPException(status_code=400, detail="mode must be 'all' or 'topmover'")
        if min_pct < 0:
            raise HTTPException(status_code=400, detail="min_pct must be >= 0")
        report_basis = _report_basis_literal(basis)
        wanted = [s for s in (symbols or "").split(",") if s.strip()] or None
        lake_root = getattr(store, "path", None)

        def _build_rows(lake: Path) -> list[Any]:
            """Build the pre-open report rows off the event loop (threadpool worker)."""
            return build_preopen_rows(
                store.connection,
                lake,
                datasets=resolve_report_datasets(lake, wanted, basis=report_basis),
                start=start_d,
                end=end_d,
                basis=report_basis,
            )

        rows = (
            await run_in_threadpool(_build_rows, lake_root.parent)
            if isinstance(lake_root, Path)
            else []
        )
        blob = render_report_zip(rows, mode=report_mode, min_pct=Decimal(str(min_pct)))
        return Response(
            content=blob,
            media_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="preopen_report.zip"'},
        )

    @app.get("/reports", response_class=HTMLResponse)
    async def reports_page() -> str:
        """GET /reports — the pre-open report download page (DEC-PREOPEN-REPORT-001)."""
        return _render_reports_html()

    # ── Credentials store (DEC-CREDENTIALS-STORE-001) ───────────────
    # CredentialsIn is module-scope (see top of file) so FastAPI resolves
    # it as a request body, not a query param (DEC-CREDS-422-FIX-001).

    @app.post("/api/v1/credentials")
    async def api_credentials_set(body: CredentialsIn) -> JSONResponse:
        """POST /api/v1/credentials — save the Dhan credentials (mode 0600).

        Accepts the access token + client id in the request BODY (never query
        params, which leak into access logs). The body is NOT logged. On a
        validation error (blank field) the credentials store raises
        :class:`CredentialsError`, mapped here to HTTP 400 with a message
        that names the offending field but NEVER its value. Returns only
        the masked preview — never echoes a value back. Saving here ENABLES
        live pulls: the Dhan adapter resolver reads this file (env-first,
        file-fallback) so the spot/options buttons flip to live
        (DEC-DHAN-CREDS-WIRING-001).
        """
        from brutex.config.credentials import (
            CredentialsError,
            DhanFileCredentials,
            credentials_preview,
            save_dhan_credentials,
        )

        try:
            save_dhan_credentials(
                DhanFileCredentials(access_token=body.access_token, client_id=body.client_id)
            )
        except CredentialsError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
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
        tables = await run_in_threadpool(lambda: table_summary(store.connection))
        return JSONResponse({"tables": tables})

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
            payload = await run_in_threadpool(
                lambda: sample_rows(store.connection, name, limit=limit, offset=offset)
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(payload)

    # ── Structured query + candle viewer (DEC-DB-QUERY-001) ──────────
    # Thin wrappers over the 100%-tested db_view helpers. All identifier
    # validation (table/column/operator/coordinate allowlists) + value
    # parameter-binding live there; a bad request raises ValueError there,
    # mapped to HTTP 400 here. Read-only GETs (no CSRF surface).

    def _parse_filters(raw: list[str]) -> tuple[tuple[str, str, str], ...]:
        """Parse repeated ``f=col:op:value`` query params into filter triples.

        Splits on the FIRST two ``:`` so a value may itself contain ``:``
        (e.g. a timestamp). A token without two ``:`` is rejected 400.
        """
        out: list[tuple[str, str, str]] = []
        for item in raw:
            parts = item.split(":", 2)
            if len(parts) != _FILTER_TOKEN_COUNT:
                raise HTTPException(
                    status_code=400,
                    detail=f"filter must be 'column:op:value', got {item!r}",
                )
            out.append((parts[0], parts[1], parts[2]))
        return tuple(out)

    @app.get("/api/v1/db/columns/{name}")
    async def api_db_columns(name: str) -> JSONResponse:
        """GET /api/v1/db/columns/{name} — column names of an allowlisted table.

        Powers the structured-query column picker. Delegates to
        :func:`brutex.dashboard.db_view.list_columns`.
        """
        store = _require_store()
        try:
            columns = await run_in_threadpool(lambda: list_columns(store.connection, name))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse({"table": name, "columns": columns})

    @app.get("/api/v1/db/query/{name}")
    async def api_db_query(
        name: str,
        # FastAPI list query params require a `Query()` default; the `Annotated`
        # form is runtime-broken under `from __future__ import annotations`, and a
        # shared module-level singleton can't carry per-param aliases — so the
        # in-signature Query() is the only working spelling. Default is immutable
        # `None` (coalesced below); B008's mutable-typed-param heuristic is a
        # false positive here.
        column: list[str] | None = Query(default=None),  # noqa: B008
        f: list[str] | None = Query(default=None),  # noqa: B008
        order_by: str | None = Query(default=None),
        desc: bool = Query(default=False),
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> JSONResponse:
        """GET /api/v1/db/query/{name} — structured, filtered, column-picked page.

        Query params: repeatable ``column=`` (projection), repeatable
        ``f=col:op:value`` (WHERE, AND-combined; ``op`` is an
        :data:`~brutex.dashboard.db_view.ALLOWED_OPERATORS` token),
        ``order_by`` + ``desc``, and ``limit`` / ``offset``. Thin wrapper over
        :func:`brutex.dashboard.db_view.query_table` — every identifier is
        allowlist-validated + every value parameter-bound there.
        """
        store = _require_store()
        # _parse_filters can itself raise HTTPException(400) on a malformed
        # filter token — keep that on the event loop (no I/O) so only the
        # blocking query_table aggregate is offloaded.
        parsed_filters = _parse_filters(f or [])
        columns = tuple(column) if column else None
        try:
            payload = await run_in_threadpool(
                lambda: query_table(
                    store.connection,
                    name,
                    columns=columns,
                    filters=parsed_filters,
                    order_by=order_by,
                    descending=desc,
                    limit=limit,
                    offset=offset,
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(payload)

    @app.get("/api/v1/db/candles/datasets")
    async def api_db_candle_datasets() -> JSONResponse:
        """GET /api/v1/db/candles/datasets — discovered candle + option datasets.

        Lists every spot candle Parquet partition set (``<lake>/bars/…``, each
        stamped ``kind="spot"``) AND every expired-option series
        (``<lake>/options/…``, each ``kind="option"``) so the viewer can offer a
        single dataset picker that distinguishes the two. Delegates to
        :func:`brutex.dashboard.db_view.list_candle_datasets` +
        :func:`brutex.dashboard.db_view.list_option_datasets`.
        """
        store = _require_store()
        lake_root = getattr(store, "path", None)
        if not isinstance(lake_root, Path):
            return JSONResponse({"datasets": [], "options": []})
        lake = lake_root.parent
        return JSONResponse(
            {
                "datasets": list_candle_datasets(lake),
                "options": list_option_datasets(lake),
            }
        )

    @app.get("/api/v1/db/candles")
    async def api_db_candles(
        exchange: str = Query(...),
        segment: str = Query(...),
        symbol: str = Query(...),
        interval: str = Query(...),
        start: str | None = Query(default=None),
        end: str | None = Query(default=None),
        # See the api_db_query note — list query param needs an in-signature
        # Query() default; immutable None coalesced below (B008 false positive).
        column: list[str] | None = Query(default=None),  # noqa: B008
        desc: bool = Query(default=False),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> JSONResponse:
        """GET /api/v1/db/candles — one page of OHLCV candles from the Parquet lake.

        Reads ``<lake>/bars/<exch>/<seg>/<symbol>/<interval>/**/*.parquet`` via
        DuckDB ``read_parquet`` (partition-pruned + ``LIMIT``-bounded). ``start``
        / ``end`` are inclusive ISO ``YYYY-MM-DD`` bounds. Thin wrapper over
        :func:`brutex.dashboard.db_view.query_candles`; an invalid coordinate /
        column / date raises ``ValueError`` there → HTTP 400 here.
        """
        store = _require_store()
        lake_root = getattr(store, "path", None)
        if not isinstance(lake_root, Path):
            raise HTTPException(status_code=400, detail="no lake root wired for candle reads")
        lake = lake_root.parent
        columns = tuple(column) if column else None
        try:
            payload = await run_in_threadpool(
                lambda: query_candles(
                    store.connection,
                    lake,
                    exchange=exchange,
                    segment=segment,
                    symbol=symbol,
                    interval=interval,
                    start=start,
                    end=end,
                    columns=columns,
                    descending=desc,
                    limit=limit,
                    offset=offset,
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(payload)

    @app.get("/api/v1/db/options")
    async def api_db_options(
        series: str = Query(...),
        start: str | None = Query(default=None),
        end: str | None = Query(default=None),
        # See the api_db_query note — list query param needs an in-signature
        # Query() default; immutable None coalesced below (B008 false positive).
        column: list[str] | None = Query(default=None),  # noqa: B008
        desc: bool = Query(default=False),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> JSONResponse:
        """GET /api/v1/db/options — one page of expired-option bars from the lake.

        Reads ``<lake>/options/<series>/*.parquet`` via DuckDB ``read_parquet``
        (``LIMIT``-bounded). Projects OHLCV+OI PLUS the option stamps
        ``side`` / ``strike`` / ``spot`` / ``iv``. ``start`` / ``end`` are
        inclusive ISO ``YYYY-MM-DD`` bounds. Thin wrapper over
        :func:`brutex.dashboard.db_view.query_option_candles`; an invalid
        series / column / date raises ``ValueError`` there → HTTP 400 here.
        """
        store = _require_store()
        lake_root = getattr(store, "path", None)
        if not isinstance(lake_root, Path):
            raise HTTPException(status_code=400, detail="no lake root wired for option reads")
        lake = lake_root.parent
        columns = tuple(column) if column else None
        try:
            payload = await run_in_threadpool(
                lambda: query_option_candles(
                    store.connection,
                    lake,
                    series=series,
                    start=start,
                    end=end,
                    columns=columns,
                    descending=desc,
                    limit=limit,
                    offset=offset,
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(payload)

    @app.get("/db", response_class=HTMLResponse)
    async def db_page() -> HTMLResponse:
        """GET /db — read-only DuckDB browser page (DEC-DB-VIEWER-001).

        ``Cache-Control: no-store`` so a redeployed page is never shadowed by a
        browser-cached older ``db.html`` whose click handler no longer matches
        the API (DEC-DB-VIEWER-NOSTORE-001).
        """
        return HTMLResponse(_render_db_html(), headers={"Cache-Control": "no-store"})

    @app.get("/universe", response_class=HTMLResponse)
    async def universe_page() -> str:
        """GET /universe — the F&O Universe Explorer page (DEC-UNIVERSE-DASHBOARD-001)."""
        return _render_universe_html()

    @app.get("/welcome", response_class=HTMLResponse)
    async def welcome_page() -> str:
        """GET /welcome — the premium animated landing page (DEC-DASHBOARD-WELCOME-001)."""
        return _render_welcome_html()

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        """GET / — inline HTML dashboard with embedded JS WebSocket client."""
        return _render_index_html()

    # WebSocket handler — defined at module scope below to sidestep
    # the `from __future__ import annotations` closure-annotation
    # resolution problem. We register it dynamically.
    app.add_api_websocket_route("/ws", _make_ws_handler(state))

    return app


def _make_read_1m(  # pragma: no cover — raw lake read, integration-tested (DEC-TF-MATERIALIZE-001)
    *, lake_root: Path, exchange: str, segment: str, symbol: str
) -> Callable[[], Any]:
    """Build the injected 1-minute reader for :func:`materialize_timeframes`.

    Returns a zero-arg callable that reads the symbol's stored ``1minute``
    Parquet partitions into a sorted :class:`polars.DataFrame` with exactly the
    columns the resampler requires (``ts_utc, open, high, low, close,
    volume``). Raw lake I/O — ``# pragma: no cover``; exercised by
    ``tests/integration/test_timeframe_materialize_roundtrip.py``.
    """
    import polars as pl

    def _read() -> Any:
        base = lake_root / "bars" / exchange / segment / symbol / "1minute"
        if not (base.is_dir() and any(base.rglob("*.parquet"))):
            raise ValueError(
                f"no stored 1minute bars for {exchange}/{segment}/{symbol} — "
                "pull the 1-minute spot first, then derive timeframes"
            )
        df = pl.read_parquet(base.as_posix() + "/**/*.parquet")
        return (
            df.rename({"timestamp": "ts_utc"})
            .select("ts_utc", "open", "high", "low", "close", "volume")
            .sort("ts_utc")
        )

    return _read


def _make_write_tf(  # pragma: no cover — raw lake write, integration-tested (DEC-TF-MATERIALIZE-001)
    *, lake_root: Path, exchange: str, segment: str, symbol: str
) -> Callable[[str, Any], int]:
    """Build the injected per-timeframe writer for :func:`materialize_timeframes`.

    Returns a ``(tf_label, frame) -> bars_written`` callable that converts the
    resampled polars frame's rows to :class:`~brutex.data.models.CandleBar`
    objects (``open_interest=0`` for spot) and appends them to the lake via a
    :class:`~brutex.data.sink.ParquetBarSink` under the TF's own stored interval
    partition (:data:`INTRADAY_TF_INTERVALS`). The sink snaps prices to the
    2-decimal tick grid + merges idempotently by ``(year, month)`` (CLAUDE.md §7
    / DEC-SPOT-PRICE-2DP-001). Raw lake I/O — ``# pragma: no cover``; exercised
    by the round-trip integration test.
    """
    from decimal import Decimal

    from brutex.data.models import CandleBar
    from brutex.data.parquet_writer import ParquetWriter
    from brutex.data.sink import ParquetBarSink

    sink = ParquetBarSink(writer=ParquetWriter(lake_root=lake_root))

    def _write(tf_label: str, frame: Any) -> int:
        interval = INTRADAY_TF_INTERVALS[tf_label]
        bars = [
            CandleBar(
                timestamp=row["ts_utc"],
                open=Decimal(str(row["open"])),
                high=Decimal(str(row["high"])),
                low=Decimal(str(row["low"])),
                close=Decimal(str(row["close"])),
                volume=int(row["volume"]),
                open_interest=0,
            )
            for row in frame.iter_rows(named=True)
        ]
        result = sink.append(
            exchange=exchange,
            segment=segment,
            symbol=symbol,
            interval=interval,
            bars=bars,
        )
        return result.bars_appended

    return _write


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
        "Parquet sink, or run without Dhan creds for the offline "
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
    wants to see on landing: total instrument rows, the distinct
    expired-option ``(flag, code)`` series + total option series in the lake
    (repointed from the dropped ``expiries`` / ``contracts`` tables —
    DEC-EXPIRED-OPTIONS-TABLE-001), and the per-underlying instrument
    breakdown. Called by the ``/api/v1/instruments/stats`` endpoint.
    """
    # DEC-P0-017 (PR #39): use public InstrumentStore.connection property
    # instead of touching the private _conn attribute (pyright
    # reportPrivateUsage was project-wide silenced; narrowing to "warning"
    # would have flagged this; we used the property refactor route per
    # PR #39 pre-impl review recommendation).
    conn = store.connection
    row_instruments = conn.execute("SELECT COUNT(*) FROM instruments").fetchone()
    row_runs = conn.execute("SELECT COUNT(*) FROM instrument_runs").fetchone()
    by_underlying = conn.execute(
        "SELECT underlying_symbol, COUNT(*) FROM instruments "
        "WHERE underlying_symbol IS NOT NULL "
        "GROUP BY underlying_symbol "
        "ORDER BY COUNT(*) DESC "
        "LIMIT 20"
    ).fetchall()
    by_type = conn.execute(
        "SELECT instrument, COUNT(*) FROM instruments GROUP BY instrument"
    ).fetchall()
    # Expired-option series counts come from the Parquet lake, not a DuckDB
    # table (DEC-EXPIRED-OPTIONS-TABLE-001). "expiries" = distinct
    # (underlying, flag, code); "contracts" = distinct option series.
    expiries = 0
    contracts = 0
    lake_root = getattr(store, "path", None)
    if isinstance(lake_root, Path):
        series = list_option_series(conn, lake_root.parent)
        contracts = len(series)
        expiries = len(
            {(s["underlying_symbol"], s["expiry_flag"], s["expiry_code"]) for s in series}
        )
    return {
        "totals": {
            "instruments": int(row_instruments[0]) if row_instruments else 0,
            "expiries": expiries,
            "contracts": contracts,
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
        clauses.append("(underlying_symbol = ? OR symbol_name = ?)")
        params.append(underlying)
        params.append(underlying)
    if instrument_type is not None:
        clauses.append("instrument = ?")
        params.append(instrument_type)
    if segment is not None:
        clauses.append("segment = ?")
        params.append(segment)
    where_sql = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    # Clauses are fixed-literal WHERE fragments; every user value is
    # parameter-bound via DuckDB "?" placeholders. Not an injection vector.
    base_select = (
        "SELECT security_id, exch_id, symbol_name, instrument, "
        "segment, underlying_symbol, sm_expiry_date, strike_price, lot_size, "
        "sm_freeze_qty, tick_size, snapshot_date FROM instruments"
    )
    order_limit = " ORDER BY symbol_name LIMIT ? OFFSET ?"
    sql = base_select + where_sql + order_limit  # nosec B608
    params.append(limit)
    params.append(offset)
    rows = conn.execute(sql, params).fetchall()
    return [
        {
            "security_id": r[0],
            "exch_id": r[1],
            "symbol_name": r[2],
            "instrument": str(r[3]),
            "segment": str(r[4]),
            "underlying_symbol": r[5],
            "sm_expiry_date": r[6].isoformat() if r[6] is not None else None,
            "strike_price": str(r[7]) if r[7] is not None else None,
            "lot_size": r[8],
            "sm_freeze_qty": r[9],
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


_WELCOME_TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "welcome.html"
_WELCOME_TEMPLATE_CACHE: str | None = None


def _load_welcome_template() -> str:
    """Load + cache the premium animated landing page (DEC-DASHBOARD-WELCOME-001).

    Ships as a package-relative resource at
    ``brutex/dashboard/templates/welcome.html`` — a self-contained, dependency-free
    marketing landing (no external fonts/CDN, CSP + offline safe) built on the
    shared design tokens and fully accessible (keyboard ``:focus-visible`` rings +
    ``prefers-reduced-motion`` honouring). Additive: it never touches the locked
    v29 operator dashboard at ``/``. Loaded once, then served verbatim.
    """
    global _WELCOME_TEMPLATE_CACHE  # noqa: PLW0603 — single-load module cache
    if _WELCOME_TEMPLATE_CACHE is None:
        _WELCOME_TEMPLATE_CACHE = _WELCOME_TEMPLATE_PATH.read_text(encoding="utf-8")
    return _WELCOME_TEMPLATE_CACHE


def _render_welcome_html() -> str:
    """Return the premium animated landing page HTML (DEC-DASHBOARD-WELCOME-001)."""
    return _load_welcome_template()


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
    """Return the coverage hub HTML (DEC-DASHBOARD-COVERAGE-SPLIT-001)."""
    return _load_coverage_template()


_EXPIRED_OPTIONS_TEMPLATE_PATH = (
    Path(__file__).resolve().parent / "templates" / "expired_options.html"
)
_EXPIRED_OPTIONS_TEMPLATE_CACHE: str | None = None


def _load_expired_options_template() -> str:
    """Load + cache the focused expired-options coverage page template.

    Ships as ``brutex/dashboard/templates/expired_options.html``. The page polls
    ``/api/v1/coverage/options`` (DEC-DASHBOARD-COVERAGE-SPLIT-001). Loaded once,
    then served verbatim.
    """
    global _EXPIRED_OPTIONS_TEMPLATE_CACHE  # noqa: PLW0603 — single-load module cache
    if _EXPIRED_OPTIONS_TEMPLATE_CACHE is None:
        _EXPIRED_OPTIONS_TEMPLATE_CACHE = _EXPIRED_OPTIONS_TEMPLATE_PATH.read_text(encoding="utf-8")
    return _EXPIRED_OPTIONS_TEMPLATE_CACHE


def _render_expired_options_html() -> str:
    """Return the focused expired-options coverage page HTML
    (DEC-DASHBOARD-COVERAGE-SPLIT-001)."""
    return _load_expired_options_template()


_SPOT_COVERAGE_TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "spot_coverage.html"
_SPOT_COVERAGE_TEMPLATE_CACHE: str | None = None


def _load_spot_coverage_template() -> str:
    """Load + cache the focused spot-coverage page template.

    Ships as ``brutex/dashboard/templates/spot_coverage.html``. The page polls
    ``/api/v1/coverage/spot`` (DEC-DASHBOARD-COVERAGE-SPLIT-001). Loaded once,
    then served verbatim.
    """
    global _SPOT_COVERAGE_TEMPLATE_CACHE  # noqa: PLW0603 — single-load module cache
    if _SPOT_COVERAGE_TEMPLATE_CACHE is None:
        _SPOT_COVERAGE_TEMPLATE_CACHE = _SPOT_COVERAGE_TEMPLATE_PATH.read_text(encoding="utf-8")
    return _SPOT_COVERAGE_TEMPLATE_CACHE


def _render_spot_coverage_html() -> str:
    """Return the focused spot-coverage page HTML (DEC-DASHBOARD-COVERAGE-SPLIT-001)."""
    return _load_spot_coverage_template()


_FETCH_WINDOWS_TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "fetch_windows.html"
_FETCH_WINDOWS_TEMPLATE_CACHE: str | None = None


def _load_fetch_windows_template() -> str:
    """Load + cache the focused fetch-windows page template.

    Ships as ``brutex/dashboard/templates/fetch_windows.html``. The page polls
    ``/api/v1/coverage/windows`` (DEC-DASHBOARD-COVERAGE-SPLIT-001). Loaded once,
    then served verbatim.
    """
    global _FETCH_WINDOWS_TEMPLATE_CACHE  # noqa: PLW0603 — single-load module cache
    if _FETCH_WINDOWS_TEMPLATE_CACHE is None:
        _FETCH_WINDOWS_TEMPLATE_CACHE = _FETCH_WINDOWS_TEMPLATE_PATH.read_text(encoding="utf-8")
    return _FETCH_WINDOWS_TEMPLATE_CACHE


def _render_fetch_windows_html() -> str:
    """Return the focused fetch-windows page HTML (DEC-DASHBOARD-COVERAGE-SPLIT-001)."""
    return _load_fetch_windows_template()


_REPORTS_TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "reports.html"
_REPORTS_TEMPLATE_CACHE: str | None = None


def _load_reports_template() -> str:
    """Load + cache the pre-open report page template (DEC-PREOPEN-REPORT-001).

    Ships as ``brutex/dashboard/templates/reports.html``. The page lists the
    report-eligible symbols (``/api/v1/reports/preopen/symbols``) and links the
    From/To date range to the ``/api/v1/reports/preopen.xlsx`` download. Loaded
    once, then served verbatim.
    """
    global _REPORTS_TEMPLATE_CACHE  # noqa: PLW0603 — single-load module cache
    if _REPORTS_TEMPLATE_CACHE is None:
        _REPORTS_TEMPLATE_CACHE = _REPORTS_TEMPLATE_PATH.read_text(encoding="utf-8")
    return _REPORTS_TEMPLATE_CACHE


def _render_reports_html() -> str:
    """Return the pre-open report page HTML (DEC-PREOPEN-REPORT-001)."""
    return _load_reports_template()


def _coverage_years() -> list[int]:
    """Return the expected coverage years — Dhan Track-1 history floor 2021 → now.

    The operational floor is **2021-01-01** (DEC-DHAN-PULL-SCOPE-001); the
    2020 floor was a stale Groww-era carryover (DEC-DHAN-COVERAGE-RESCOPE-001).
    """
    return list(range(2021, datetime.now(UTC).year + 1))


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


_DB_TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "db.html"


def _load_db_template() -> str:
    """Read the read-only DuckDB browser page template **fresh** each call.

    Ships as a package-relative resource at
    ``brutex/dashboard/templates/db.html`` (DEC-DB-VIEWER-001). The
    template fetches ``/api/v1/db/tables`` for the table list + row counts,
    then ``/api/v1/db/columns/{name}`` + ``/api/v1/db/query/{name}`` when a
    table is clicked, escaping every column name + cell via an ``esc()``
    helper before it touches innerHTML (XSS — values originate from the
    instrument-master CSV). **Read fresh** (no module cache) so a redeployed
    template takes effect on the next request without a server restart — the
    load-once cache was why a clicked table showed nothing after a deploy
    (DEC-DB-VIEWER-NOSTORE-001).
    """
    return _DB_TEMPLATE_PATH.read_text(encoding="utf-8")


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
        <th>symbol_name</th>
        <th>instrument</th>
        <th>segment</th>
        <th>underlying</th>
        <th>sm_expiry_date</th>
        <th>strike</th>
        <th>lot</th>
        <th>sm_freeze_qty</th>
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
        <td>${i.symbol_name}</td>
        <td>${i.instrument}</td>
        <td>${i.segment}</td>
        <td>${i.underlying_symbol || '-'}</td>
        <td>${i.sm_expiry_date || '-'}</td>
        <td>${i.strike_price || '-'}</td>
        <td>${i.lot_size || '-'}</td>
        <td>${i.sm_freeze_qty || '-'}</td>
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
      target: <span id="rl-rps">-</span> rps ·
      effective: <span id="rl-eff">-</span> rps ·
      tokens: <span id="rl-tokens">-</span> ·
      429s (60s): <span id="rl-429">-</span> ·
      p95: <span id="rl-lat">-</span> ms
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
  document.getElementById('rl-eff').textContent = (rl.effective_rps || 0).toFixed(2);
  document.getElementById('rl-tokens').textContent = (rl.available_tokens || 0).toFixed(1);
  document.getElementById('rl-429').textContent = rl.recent_429_count || 0;
  document.getElementById('rl-lat').textContent = (rl.latency_ms_p95 || 0).toFixed(0);

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
    "pull_all_spot",
    "pull_candles",
    "pull_options",
    "select_pull_instruments",
    "snapshot_to_json",
    "universe_summary",
]
