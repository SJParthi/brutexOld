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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from pydantic import BaseModel

from brutex.config.credentials import groww_credentials_present
from brutex.dashboard.completeness import (
    build_completeness_scoreboard,
    categorize_instruments,
    log_symbol_reconcile_from_board,
    per_symbol_completeness,
    scoreboard_to_csv,
)
from brutex.dashboard.composite_pull import (
    CompositePullReport,
    pull_historical_for_underlyings,
)
from brutex.dashboard.coverage_view import (
    build_coverage_payload,
    build_option_coverage_payload,
    build_spot_coverage_payload,
)
from brutex.dashboard.db_view import (
    fno_candle_lake_schema,
    list_candle_datasets,
    list_columns,
    list_fno_candle_datasets,
    list_option_datasets,
    list_option_series,
    list_registry_contracts,
    list_registry_expiries,
    query_candles,
    query_option_candles,
    query_table,
    registry_counts,
    registry_path_for,
    sample_rows,
    spot_window_rollup,
    summarize_option_datasets,
    summarize_spot_datasets,
    table_summary,
)
from brutex.dashboard.derivatives_pull import resolve_pull_intervals
from brutex.dashboard.economics_integrity_csv import economics_integrity_to_csv
from brutex.dashboard.fno_cascade import (
    FnoCascadeCache,
    FnoCascadeIndex,
    build_fno_cascade,
    contract_to_json,
)
from brutex.dashboard.historical_pull import pull_contract_candles
from brutex.dashboard.mapping_integrity_csv import mapping_integrity_to_csv
from brutex.dashboard.master_integrity_csv import master_integrity_to_csv
from brutex.dashboard.metadata_pull import MetadataPullReport, pull_expiries_contracts
from brutex.dashboard.movers_cache import MoversSnapshotCache, candidate_key
from brutex.dashboard.movers_futures import (
    build_futures_payload,
    compute_futures_candidates,
    normalize_futures_tier,
)
from brutex.dashboard.movers_options import (
    build_options_payload,
    compute_options_candidates,
    normalize_options_selectors,
)
from brutex.dashboard.movers_snapshot import (
    DEFAULT_INTERVAL,
    DEFAULT_TOP_N,
    build_snapshot_payload,
    compute_spot_candidates,
    parse_snapshot_date,
    pending_asset_payload,
)
from brutex.dashboard.movers_warm import latest_movers_date, warm_movers_snapshots
from brutex.dashboard.pipeline_pull import run_full_pipeline
from brutex.dashboard.preopen_report import (
    ReportBasis,
    ReportMode,
    build_preopen_rows,
    render_report_zip,
    render_xlsx,
    report_filename,
    resolve_report_datasets,
)
from brutex.dashboard.pull_controller import (
    BulkSpotReport,
    SessionRateController,
    pull_all_spot,
    pull_candles,
    resolve_fetch_intervals,
    resolve_pull_symbols,
    resolve_pull_targets,
)
from brutex.dashboard.pull_jobs import BackgroundPullJob, ProgressHook
from brutex.dashboard.request_logging import emit_request_log
from brutex.dashboard.spot_cascade import (
    SpotCascadeIndex,
    build_spot_cascade,
    symbol_to_json,
)
from brutex.dashboard.summary_cache import OPTIONS, SPOT, SummaryCache
from brutex.dashboard.sweep_runner import (
    default_results_root,
    resolve_sweep_request,
    run_sweep_from_request,
    sweep_progress_file,
)
from brutex.dashboard.universe_cache import UniverseSnapshotCache
from brutex.dashboard.universe_pull import select_pull_instruments
from brutex.dashboard.universe_query import list_underlyings, universe_summary
from brutex.dashboard.verdict_banner import banner_for
from brutex.data.economics_integrity import META_KEY as ECONOMICS_INTEGRITY_META_KEY
from brutex.data.economics_integrity import report_from_dict as economics_report_from_dict
from brutex.data.fno_candle_lake import FNO_CANDLE_LAKE_SEGMENT
from brutex.data.mapping_integrity import (
    EligibleSpot,
    MappingIntegrityReport,
    coverage_years_from_records,
    reconcile_mapping_integrity,
    report_to_dict,
)
from brutex.data.master_integrity import META_KEY as MASTER_INTEGRITY_META_KEY
from brutex.data.master_integrity import report_from_dict as master_report_from_dict
from brutex.data.registry_store import ContractRow, RegistryStore
from brutex.data.timeframe_materialize import (
    MATERIALIZABLE_TFS,
    make_lake_read_1m,
    make_lake_write_tf,
    materialize_timeframes,
)
from brutex.models import Exchange, Instrument, InstrumentType
from brutex.observability.correlation import request_id_scope
from brutex.orchestrator.pipeline import (
    PipelineEvent,
    PipelineEventKind,
)
from brutex.orchestrator.state import IngestPhase
from brutex.universe import (
    derive_lake_underlyings,
    is_index_instrument,
    is_reference_only,
)

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
class GrowwCredentialsIn(BaseModel):  # pyright: ignore[reportUntypedBaseClass]
    """Request body for ``POST /api/v1/groww/credentials``.

    Defined at **module scope** (not inside :func:`build_app`) so FastAPI
    correctly resolves it as a request-BODY model: a function-local Pydantic
    model is mis-classified by FastAPI as a query parameter (the 422 bug,
    DEC-CREDS-422-FIX-001). The api key + TOTP secret are accepted in the BODY
    (never query params, which leak into access logs) and passed straight to
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
    """Rate-limiter snapshot (current + effective RPS, tokens, recent 429s, latency).

    ``current_rps`` is the controller's *target* pace (the adaptive [3..5] ladder);
    ``effective_rps`` is the *measured* inter-arrival rate from the transport meter
    (calls ÷ wall-span, never overstating) — together they answer the operator's
    "is 5 r/s actually working?" at a glance (DEC-DASHBOARD-RATE-PANEL-001).

    ``rps_1s`` / ``rps_1s_max`` are the **instantaneous** per-second issue gauge
    straight from the vendor client's call-meter snapshot — the
    trailing-1s physical-POST count and its run peak. Unlike the cumulative
    ``effective_rps`` average (which can read high in the opening seconds without
    any true breach), ``rps_1s_max`` is the real-time PROOF the 5 r/s cap held:
    ``rps_1s_max <= 5`` ⇒ no per-second breach ever (DEC-DHAN-RATE-REALTIME-GUARANTEE-001).
    A value > 5 should be impossible (every physical POST is burst-1 bucket-gated);
    the panel flags it loudly if it ever happens (DEC-DASHBOARD-RPS1S-PANEL-001).
    """

    current_rps: float = 0.0
    available_tokens: float = 0.0
    recent_429_count: int = 0
    effective_rps: float = 0.0
    latency_ms_p95: float = 0.0
    rps_1s: int = 0
    rps_1s_max: int = 0
    #: The mandatory daily vendor quota gauge (DEC-AUTO-RESUME-QUOTA-001): calls spent
    #: today, calls remaining, and the 100,000/day ceiling — straight from the
    #: shared persisted ``DailyQuotaTracker`` via the controller snapshot. ``-1``
    #: ``quota_limit`` means "no tracker wired" (dry-run / no live pull) so the panel
    #: hides the gauge rather than showing a false 0.
    quota_used: int = 0
    quota_remaining: int = 0
    quota_limit: int = -1


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
    # Live brute-force SWEEP progress (DEC-SWEEP-WIRE-GA-001) — percent / ETA /
    # tuples-per-sec / survivors, served at ``/api/v1/sweep-progress``. Two
    # feeds: ``sweep_progress`` is the in-process push (a same-process sweep calls
    # :meth:`update_sweep_progress`), and ``sweep_progress_path`` is the
    # cross-process file bridge a CLI-launched ``brutex sweep-all`` publishes to
    # via :func:`brutex.engine.sweep_supervisor.publish_progress`. The route
    # prefers the file (most-recent for a separate sweep process), then the
    # in-process push, then an idle snapshot.
    sweep_progress: dict[str, Any] | None = None
    sweep_progress_path: Path | None = None
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
        quota_used: int = 0,
        quota_remaining: int = 0,
        quota_limit: int = -1,
    ) -> None:
        """Update the rate-limit panel from a fresh controller snapshot.

        ``quota_used`` / ``quota_remaining`` / ``quota_limit`` are the daily vendor
        quota gauge (DEC-AUTO-RESUME-QUOTA-001), defaulting to the "no tracker
        wired" sentinel (``quota_limit=-1``) so a caller that has no quota snapshot
        leaves the gauge hidden rather than showing a false 0.
        """
        with self._lock:
            self.rate_limit.current_rps = current_rps
            self.rate_limit.available_tokens = available_tokens
            self.rate_limit.recent_429_count = recent_429_count
            self.rate_limit.quota_used = quota_used
            self.rate_limit.quota_remaining = quota_remaining
            self.rate_limit.quota_limit = quota_limit

    def update_throughput(
        self,
        *,
        effective_rps: float,
        latency_ms_p95: float,
        rps_1s: int = 0,
        rps_1s_max: int = 0,
    ) -> None:
        """Update the MEASURED throughput half of the rate panel from a ``CallMeter`` snapshot.

        ``effective_rps`` is the transport's inter-arrival rate (calls ÷ wall-span)
        and ``latency_ms_p95`` its 95th-percentile round-trip; ``rps_1s`` /
        ``rps_1s_max`` are the instantaneous trailing-1s issue gauge + its run peak —
        all straight from the vendor client's call-meter snapshot
        (DEC-DASHBOARD-RATE-PANEL-001 + DEC-DASHBOARD-RPS1S-PANEL-001). ``rps_1s_max``
        is the real-time proof the 5 r/s cap held (``<= 5`` ⇒ no breach, ever).
        """
        with self._lock:
            self.rate_limit.effective_rps = effective_rps
            self.rate_limit.latency_ms_p95 = latency_ms_p95
            self.rate_limit.rps_1s = rps_1s
            self.rate_limit.rps_1s_max = rps_1s_max

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

    def update_sweep_progress(self, snapshot: Mapping[str, Any]) -> None:
        """Replace the in-process live sweep-progress snapshot (DEC-SWEEP-WIRE-GA-001).

        The in-process push seam: a sweep running in THIS process wires this as
        its supervisor ``on_progress`` hook (passing
        :meth:`SweepProgressSnapshot.to_dict
        <brutex.engine.sweep_progress.SweepProgressSnapshot.to_dict>`). Stores a
        shallow copy under the state lock. A separate sweep PROCESS instead
        publishes to :attr:`sweep_progress_path` (see :meth:`sweep_progress_view`).
        """
        with self._lock:
            self.sweep_progress = dict(snapshot)

    def sweep_progress_view(self) -> dict[str, Any]:
        """Return the current live sweep-progress snapshot for the dashboard route.

        Resolution order (crash-tolerant — a dashboard poll NEVER raises):

        1. :attr:`sweep_progress_path` if configured + readable — the
           cross-process file bridge a CLI ``brutex sweep-all`` publishes to (the
           freshest source for a separate sweep process);
        2. else the in-process :attr:`sweep_progress` push, if set;
        3. else an idle snapshot.

        A missing / malformed progress file falls through to (2)/(3) — never an
        error (delegated to :func:`brutex.engine.sweep_supervisor.read_progress`,
        which returns the idle view on any read/parse failure).
        """
        from brutex.engine.sweep_supervisor import idle_progress, read_progress

        with self._lock:
            path = self.sweep_progress_path
            in_process = self.sweep_progress
        if path is not None and path.exists():
            return read_progress(path)
        if in_process is not None:
            return dict(in_process)
        return idle_progress()

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
    adaptive rate (:meth:`~brutex.adapters.rate_common.RateController.snapshot`
    → ``current_rps`` + ``recent_hits``) and measured throughput
    (the vendor client's call-meter snapshot → ``effective_rps`` +
    ``latency_ms_p95`` + the instantaneous ``rps_1s`` / ``rps_1s_max`` gauge) flow to
    the dashboard rate panel (DEC-DASHBOARD-RATE-PANEL-001 +
    DEC-DASHBOARD-RPS1S-PANEL-001). Pure + testable: drive it with two snapshot
    mappings and assert the panel updated.
    """

    def _observe(rate: Mapping[str, Any], meter: Mapping[str, Any]) -> None:
        """Map one (rate-controller, call-meter) snapshot pair onto the rate panel."""
        # Quota gauge (DEC-AUTO-RESUME-QUOTA-001): the controller snapshot carries
        # ``quota_*`` only when a DailyQuotaTracker is wired (the live path); absent
        # → ``quota_limit=-1`` sentinel so the panel hides the gauge, not a false 0.
        state.update_rate_limit(
            current_rps=float(rate.get("current_rps", 0.0)),
            available_tokens=float(rate.get("available_tokens", 0.0)),
            recent_429_count=int(rate.get("recent_hits", 0)),
            quota_used=int(rate.get("quota_used", 0)),
            quota_remaining=int(rate.get("quota_remaining", 0)),
            quota_limit=int(rate.get("quota_limit", -1)),
        )
        state.update_throughput(
            effective_rps=float(meter.get("effective_rps", 0.0)),
            latency_ms_p95=float(meter.get("latency_ms_p95", 0.0)),
            rps_1s=int(meter.get("rps_1s", 0)),
            rps_1s_max=int(meter.get("rps_1s_max", 0)),
        )

    return _observe


def build_app(
    state: DashboardState,
    *,
    instrument_store: Any = None,
    summary_cache: SummaryCache | None = None,
    movers_cache: MoversSnapshotCache | None = None,
    universe_cache: UniverseSnapshotCache | None = None,
    quota_tracker: Any = None,
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
        section so a user can literally see what the vendor ingest
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
    movers_cache
        Optional :class:`~brutex.dashboard.movers_cache.MoversSnapshotCache`
        backing the O(1) ``/movers`` re-interactions (DEC-MOVERS-O1-LATENCY-001).
        Injected so a test can assert the cache hit/invalidation behaviour;
        ``None`` (the default) builds a fresh production cache. It is invalidated
        by the same write-generation as ``summary_cache`` (a pull/materialize
        bumps that generation; the movers cache stamps every entry with it).
    universe_cache
        Optional :class:`~brutex.dashboard.universe_cache.UniverseSnapshotCache`
        backing the O(1) ``/universe`` summary + instruments polls
        (DEC-UNIVERSE-BACKEND-CACHE-001). Caches the lean base reads + derived
        payloads keyed by the ``instruments``-table content-revision token, so a
        repeat poll between two instrument loads is a dict handoff, not a re-scan.
        Injected so a test can assert the hit/invalidation behaviour; ``None``
        (the default) builds a fresh production cache.
    quota_tracker
        Optional read-only daily-quota source for the ``/api/v1/quota``
        endpoint (DEC-DASHBOARD-QUOTA-PERSISTED-001). Any object exposing
        ``current_count() -> int``, ``remaining() -> int`` and a
        ``daily_limit`` int satisfies it (the live
        :class:`~brutex.orchestrator.quota_tracker.DailyQuotaTracker`). When
        ``None`` (the default) a real tracker is built lazily on first poll,
        reading the IST-midnight LMDB counter the pulls write — so the
        ``/universe`` gauge shows today's PERSISTED usage on page-load, before
        any pull in this session reports onto the live rate panel. Injected so
        tests drive a deterministic fake (no LMDB).

    Returns
    -------
    fastapi.FastAPI
        Already-wired ASGI app. Caller mounts it on their uvicorn
        server of choice.
    """
    try:
        from fastapi import FastAPI, HTTPException, Query
        from fastapi.concurrency import run_in_threadpool
        from fastapi.responses import (
            HTMLResponse,
            JSONResponse,
            PlainTextResponse,
            Response,
        )
        from fastapi.staticfiles import StaticFiles
    except ImportError as exc:
        raise RuntimeError(
            "FastAPI is not installed. Install with `pip install "
            "fastapi uvicorn` to run the dashboard. Unit tests for the "
            "state model do not need FastAPI."
        ) from exc

    from brutex.observability.metrics import get_registry, render_prometheus

    # The retired-vendor 24h-JWT token keepalive was retired here (PR-J4c): the
    # dashboard now authenticates via Groww TOTP, so the vestigial keepalive
    # lifespan wiring was removed. The retired-vendor adapter package has since been
    # deleted; the dashboard simply no longer drives any keepalive.
    app = FastAPI(title="BruteX Dashboard", version="0.1.0a0")

    # Serve the shared dashboard static assets (the date-picker component, etc.)
    # from ``/static`` — ONE source of truth loaded by every page via <link>/<script>
    # (no inline copy, so the duplication gate stays clean).
    app.mount(
        "/static",
        StaticFiles(directory=Path(__file__).resolve().parent / "static"),
        name="static",
    )

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
    # The /movers point-in-time snapshot cache (DEC-MOVERS-O1-LATENCY-001): caches
    # the expensive, top_n-independent candidate set per snapshot coordinate so a
    # sub-filter / top_n / re-selection is an O(1) hit + a cheap re-rank, never a
    # lake re-scan. It is invalidated for free by the SAME write-generation signal
    # as the coverage SummaryCache (a pull/materialize bumps `_summary_cache`'s
    # generation, which the movers cache stamps every entry with).
    _movers_cache = movers_cache if movers_cache is not None else MoversSnapshotCache()
    # The /universe backend snapshot cache (DEC-UNIVERSE-BACKEND-CACHE-001): caches
    # the lean base reads (eligible set + IDX/EQ candidates + reference-only ids) +
    # the derived summary / per-kind instrument rows, keyed by the master's own
    # `instruments`-table content-revision token (count(*), max(fetched_at)) — NOT
    # the `instrument_runs` audit sidecar, which can lag an upsert. A repeat
    # /universe poll between two loads is an O(1) dict handoff; a new instrument
    # load moves the token and rebuilds. Content-exact (never a stale illusion —
    # Golden Rule #1).
    _universe_cache = universe_cache if universe_cache is not None else UniverseSnapshotCache()
    # The /db candle-pager TOTAL cache (DEC-DB-CANDLE-COUNT-CACHE-001): the page
    # `COUNT(*)` over `read_parquet(glob)` is O(files) (a Parquet-footer read per
    # monthly file — ~8 ms at 6 years, growing as the lake deepens) and was
    # recomputed on EVERY paging click. The total is invariant across paging / sort
    # / column subsetting of one `(coords, start, end)`, so it is memoised here per
    # that key + stamped with `_summary_cache.generation` — a prev/next click is an
    # O(1) hit, and a pull/materialize bumps the generation → recompute (the SAME
    # write-invalidation the movers cache uses; never a stale total). We REUSE the
    # COMMON generation-stamped LRU `MoversSnapshotCache` (CLAUDE.md §11 — it is a
    # generic keyed cache; no movers coupling, just the shared implementation).
    _candle_count_cache = MoversSnapshotCache()
    # The /coverage/windows counts cache (DEC-COVERAGE-WINDOWS-O1-001): the
    # `GROUP BY market, status` bucketed counts over the window ledger are invariant
    # across which tab/filter is clicked, so they are memoised behind the SAME
    # generation signal — a repeat click within one generation is O(1), a pull bumps
    # the generation → recompute. Dedicated instance so the (counts, total) tuples
    # never share a key namespace with the candle int-totals above.
    _window_counts_cache = MoversSnapshotCache()
    # The /db F&O cascade cache (DEC-DB-FNO-CASCADE-001): the underlying→expiry→contract
    # index is an O(datasets) build over a lake dir-walk; memoised here behind the SAME
    # write-generation as the caches above so a pull rebuilds it but every cascade click
    # (underlyings / expiries / contracts) is an O(1) handoff — tiny lazy payloads, never
    # the flat thousand-row dropdown.
    _fno_cascade_cache = FnoCascadeCache()
    # The /db candle-datasets discovery cache (DEC-DB-TAB-SWITCH-O1-001): the
    # `/api/v1/db/candles/datasets` endpoint walks the WHOLE `bars/` tree THREE times
    # (spot + F&O + options, each an O(files) `rglob("*.parquet")`) — and it fires on
    # EVERY `/db` candle tab switch (spot↔F&O↔option). The three lists are invariant
    # between lake writes, so they are memoised behind the SAME generation signal as the
    # caches above (a pull bumps it → rediscover; otherwise a tab switch is an O(1) handoff
    # of the cached lists). REUSES the COMMON `MoversSnapshotCache` (one dataset key).
    _db_datasets_cache = MoversSnapshotCache()
    # The /db SPOT cascade cache (DEC-DB-SPOT-CASCADE-001): the symbol→interval index is
    # an O(datasets) build over the non-F&O lake partitions; memoised behind the SAME
    # write-generation as the caches above (the spot twin of `_fno_cascade_cache`), so a
    # pull rebuilds it but every symbol-picker open is an O(1) handoff — the small
    # symbols-with-intervals payload, never the flat ~750-row spot dropdown.
    _spot_cascade_cache = MoversSnapshotCache()
    # ONE rate controller per dashboard session, shared across every pull
    # (DEC-DHAN-PERSIST-RATE-001) so the learned rate carries over instead of each
    # pull restarting at the 5 r/s ceiling and re-storming the vendor.
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
        measured, the instantaneous ``rps_1s`` / ``rps_1s_max`` gauge,
        ``recent_429_count``, ``latency_ms_p95``), populated during a live pull by
        :func:`make_rate_observer` (DEC-DASHBOARD-RATE-PANEL-001 +
        DEC-DASHBOARD-RPS1S-PANEL-001). A pure in-memory dict copy — never touches
        the lake or the store lock.
        """
        return JSONResponse(asdict(state.rate_limit))

    @app.get("/api/v1/quota")
    async def api_quota() -> JSONResponse:
        """GET /api/v1/quota — today's PERSISTED vendor daily-quota usage (read-only).

        The live vendor (Groww) has **no daily quota** for backtesting
        (``GROWW_DAILY_QUOTA_UNVERIFIED=None``; the binding limit is the 500 req/min
        governor — CLAUDE.md §6 / DEC-GROWW-RATE-PERMIN-001). So when no daily-quota
        tracker is injected this returns the ``quota_limit=-1`` "no daily quota"
        sentinel and the ``/universe`` gauge stays hidden — never a fabricated
        ``X / 100,000`` (Golden Rule #1), and the route never constructs an LMDB
        tracker. When a tracker IS wired (a vendor that genuinely caps calls/day) it
        serves that tracker's IST-midnight ``used / limit / remaining`` (one O(1)
        LMDB read; DEC-DASHBOARD-QUOTA-PERSISTED-001). Never touches the lake/store.
        """
        if quota_tracker is None:
            return JSONResponse({"quota_used": 0, "quota_limit": -1, "quota_remaining": 0})
        return JSONResponse(
            {
                "quota_used": quota_tracker.current_count(),
                "quota_limit": quota_tracker.daily_limit,
                "quota_remaining": quota_tracker.remaining(),
            }
        )

    @app.get("/api/v1/sweep-progress")
    async def api_sweep_progress() -> JSONResponse:
        """GET /api/v1/sweep-progress — O(1) live brute-force sweep progress (DEC-SWEEP-WIRE-GA-001).

        The dashboard panel for the guarantee-&-assurance layer: percent / ETA /
        tuples-per-sec / survivors / skipped, populated by the supervisor's
        :class:`~brutex.engine.sweep_progress.SweepProgress` (wired into
        :func:`~brutex.engine.sweep_supervisor.run_supervised_grid`). Serves the
        freshest of the cross-process file bridge, the in-process push, or an idle
        snapshot — a pure O(1) read that never touches the lake or the store lock
        and never raises (see :meth:`DashboardState.sweep_progress_view`).
        """
        return JSONResponse(state.sweep_progress_view())

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

        O(1) repeat poll (DEC-UNIVERSE-BACKEND-CACHE-001): the base reads +
        derived counts are cached behind ``_universe_cache``, keyed by the
        master's ``instruments``-table content token — a re-poll between two loads
        is a dict handoff, a new load rebuilds.
        """
        store = _require_store()
        return JSONResponse(_universe_cache.summary(store))

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

        O(1) repeat poll (DEC-UNIVERSE-BACKEND-CACHE-001): the base reads + the
        per-*kind* classified rows are cached behind ``_universe_cache``, keyed by
        the master's ``instruments``-table content token. A bad *kind* still surfaces
        as a 400 (the classifier's ``ValueError`` is raised before any caching).
        """
        store = _require_store()
        try:
            rows = _universe_cache.instruments(store, kind=kind)
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

        Honesty (CLAUDE.md §1 / §10): this is **NOT vendor data**. Index
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
        from brutex.universe_constituents import held_members

        store = _require_store()
        eligible = store.fno_eligible_underlyings()
        candidates = store.list_index_equity_instruments()
        reference_only_ids = store.reference_only_security_ids()
        # The bare engine symbols of EVERY single stock we hold — F&O-eligible AND
        # admitted REFERENCE_ONLY NTM constituents (DEC-INDEX-CONSTITUENTS-FULL-001).
        # Passing reference_only_ids classifies the non-F&O constituents as "stock"
        # too, so the tick covers the index's FULL membership (not just the ~212 F&O
        # names); the frontend "F&O only" toggle then narrows to the option-pullable
        # rows. Reuse the COMMON classifier (CLAUDE.md §11) — no re-derived split.
        stock_rows = list_underlyings(
            candidates, kind="stock", fno_eligible=eligible, reference_only_ids=reference_only_ids
        )
        held_symbols = [row["symbol"] for row in stock_rows]

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
        # ISIN-primary, ticker-fallback match (DEC-INDEX-CONSTITUENTS-NSE-001):
        # a constituent whose NSE ticker was renamed still matches by its
        # globally-unique ISIN, so the tick is rename-proof. Reuses the COMMON
        # resolver (CLAUDE.md §11) over the held universe rows.
        members = held_members(result.rows, candidates, held_symbols)
        notes = {
            "nse": (
                f"{len(members)} constituent(s) of {symbol} ticked — "
                "fresh from NSE + cached, matched by ISIN→ticker (rename-proof; "
                "verify, membership rebalances). "
                "Toggle 'F&O only' to narrow to option-pullable members."
            ),
            "cache": (
                f"{len(members)} constituent(s) of {symbol} ticked — "
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
        The instrument master is a PUBLIC CSV needing NO vendor token, so this
        endpoint can fill the empty ``/universe`` list without authentication
        (options / candles still need the token, separately). On any
        network / parse failure (the live fetch can't reach the vendor asset
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
                detail=f"couldn't reach vendor asset host / parse instrument master: {exc}",
            ) from exc
        return JSONResponse(counts)

    @app.post("/api/v1/universe/pull")
    async def api_universe_pull(
        symbol: str = Query(...),
        year: int | None = Query(default=None),
        from_date: str | None = Query(default=None),
        to_date: str | None = Query(default=None),
        spot_interval: str | None = Query(default=None),
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
        given they select an explicit, clamped ``[2020-01-01, today]``
        range (a blank ``to_date`` defaults to the previous day — today is
        included only via an explicit ``to_date`` reaching it), otherwise
        the full calendar ``year``, else (no dates, no year) the full
        Track-1 history ``[2020-01-01, previous day]``. A malformed date or
        a ``from`` after ``to`` surfaces as a 400 with the real reason
        (DEC-PULL-ERROR-VISIBILITY-001).
        ``spot_interval`` is now a VESTIGIAL, ignored query param
        (DEC-1MINUTE-ONLY-DERIVE-1DAY-001): 1-minute is the ONLY granularity fetched
        from Groww and 1-day is DERIVED from it, so :func:`resolve_fetch_intervals`
        always resolves to ``("1minute",)`` regardless of this value.
        ``dry_run`` is forced ``True`` automatically when Groww
        credentials are not present (via the testable
        :func:`brutex.config.credentials.groww_credentials_present`
        gate — Groww is the PRIMARY spot surface post-cutover,
        DEC-DHAN-GROWW-REMIGRATE-001), so the page never attempts a live
        network call without creds.
        """
        dry_run = not groww_credentials_present()
        _LOG.info(
            "universe.pull.requested",
            symbol=symbol,
            year=year,
            from_date=from_date,
            to_date=to_date,
            spot_interval=spot_interval,
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
            # is unchanged (vendor rate-limit), but it no longer blocks the UI.
            report = await run_in_threadpool(
                pull_candles,
                instrument,
                lake_root=_resolve_pull_lake_root(instrument_store, dry_run=dry_run),
                year=year,
                from_date=from_date,
                to_date=to_date,
                intervals=resolve_fetch_intervals(spot_interval),
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

    # The single-flight background pull job that powers the LIVE feed
    # (DEC-PULL-PROGRESS-LIVE-001): one universe pull at a time across the whole
    # server; the page polls /pull-progress for the per-unit tick. Owned by this
    # app instance (no module-global mutable state — CLAUDE.md §4). It is wired
    # with the app's SummaryCache so each window write invalidates the coverage
    # summary (the next eligible poll refreshes), and owns the incremental
    # CoverageCounters that back the O(1) coverage headline
    # (DEC-DASHBOARD-FASTREAD-001).
    _pull_job = BackgroundPullJob(summary_cache=_summary_cache)

    # Last finished spot pull's verdict dict (DEC-VERDICT-BANNER-001). The
    # background _work closure stamps the BulkSpotReport's `.verdict.to_dict()`
    # here when a pull / retry completes; GET /pull-verdict maps it through the
    # pure banner_for() helper so /universe can SHOW the coloured banner. A
    # one-key holder (not a module-global — CLAUDE.md §4); the write is a single
    # atomic reference assignment on the job thread, the read is on the request
    # thread (CPython GIL → torn-state-free). `None` until the first pull lands.
    _last_pull_verdict: dict[str, dict[str, Any] | None] = {"verdict": None}

    @app.post("/api/v1/universe/pull-all")
    async def api_universe_pull_all(
        kind: str | None = Query(default=None),
        symbol: str | None = Query(default=None),
        symbols: str | None = Query(default=None),
        year: int | None = Query(default=None),
        from_date: str | None = Query(default=None),
        to_date: str | None = Query(default=None),
        spot_interval: str | None = Query(default=None),
    ) -> JSONResponse:
        """POST /api/v1/universe/pull-all — start a bulk / multi / per-symbol SPOT pull.

        Backs BOTH the one-click "⤓ Pull ALL indices / stocks spot" buttons
        (``?kind=index|stock``, full ``[2020-01-01, previous day]`` 1m + 1d) AND the
        unified multi-select "⤓ Pull spot" (``?symbols=a,b,c`` — the ticked
        SELECTION fanned in ONE saturating pass over the from/to picker range,
        DEC-DHAN-SPOT-MULTI-SATURATE-001). Precedence: ``symbols`` (ticked many) >
        ``symbol`` (one) > ``kind`` (every admitted underlying of index/stock).

        All forms route to :func:`brutex.dashboard.pull_controller.pull_all_spot`
        on the **background job** (returns at once; poll ``/pull-progress``): its
        cross-symbol ``(symbol, interval, window)`` fan-out keeps the ONE shared
        :class:`SessionRateController` SATURATED at the operator-DIRECTED 5 r/s
        ceiling (lowered 8 → 5 on 2026-06-22), replacing the old serial per-symbol
        ``/universe/pull`` loop that only kept ~3 r/s in flight. ``from_date`` / ``to_date`` / ``year`` carry the date picker; when
        ALL are blank the full Track-1 history applies (the Pull-ALL default, so
        the ``?kind=`` callers are byte-for-byte unchanged). Single-flight; an empty
        / unknown ``?symbols=`` id fails CLOSED (400, naming offenders — never a
        silent partial). ``dry_run`` is forced when Groww creds are absent
        (the PRIMARY spot gate post-cutover, DEC-DHAN-GROWW-REMIGRATE-001).
        """
        dry_run = not groww_credentials_present()
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
            spot_interval=spot_interval,
            dry_run=dry_run,
        )
        # 1-minute is the ONLY fetched granularity (DEC-1MINUTE-ONLY-DERIVE-1DAY-001):
        # ``resolve_fetch_intervals`` ignores the vestigial ``spot_interval`` and always
        # returns ``("1minute",)``; 1-day is DERIVED server-side after the pull. Drives
        # BOTH the fetch AND the progress total (symbols × intervals).
        fetch_intervals = resolve_fetch_intervals(spot_interval)

        def _work(on_progress: ProgressHook) -> object:  # pragma: no cover — runs on the job thread
            """Background body: bulk-pull every selected spot symbol, streaming progress.

            Runs the AUTO-RESUME-UNTIL-CLEAN loop (DEC-AUTO-RESUME-DEFAULT-001):
            ``auto_retry=True`` + ``until_clean=True`` so AFTER the saturating first
            pass the leftover failed windows are re-pulled in bounded rounds (over
            the SHARED coverage ledger — only the still-failed windows hit the vendor
            again, rate-safe) until ONE terminal state is reached: ``stop_clean``
            (every window fetched — the normal exit), ``stop_quota`` (the day's
            100,000-call vendor quota is spent — resume after the IST-midnight reset),
            ``stop_dead`` (the token died — re-auth), or ``stop_no_progress`` /
            budget backstop. So the one-click pull DRIVES ITSELF to a clean lake with
            zero manual "Re-pull to finish" clicks (CLAUDE.md §11 AUTOMATED), bounded
            + quota-safe (Golden Rule #10 — never an infinite or quota-burning loop).
            """

            # Adapt the job's 4-arg hook (label, bars, failed, gap) onto the
            # Groww spot ProgressHook (label, bars, failed) — Groww has no
            # known-gap concept, so ``gap`` is always ``False`` here (PR-J4b). In
            # ledger-synced mode this only refreshes the "current symbol" label —
            # the bar's counters come from _ledger_sync (DEC-SPOT-WINDOW-PROGRESS-001).
            def _spot_progress(label: str, bars: int, failed: bool) -> None:
                on_progress(label, bars, failed, False)

            # WINDOW-level live progress (DEC-SPOT-WINDOW-PROGRESS-001): the pull pushes
            # the live WindowLedger counts (settled / total / bars / failed + the
            # currently-fetching symbol) here on a ~1 s cadence, so the units bar +
            # /coverage advance per WINDOW instead of freezing for minutes between symbol
            # completions (the operator-observed "stuck at 24/779").
            def _ledger_sync(
                completed: int, total: int, bars: int, failed: int, current: str
            ) -> None:
                _pull_job.sync_from_ledger(
                    completed=completed,
                    total=total,
                    bars=bars,
                    failed=failed,
                    current=current,
                )

            report = pull_all_spot(
                instruments,
                lake_root=lake_root,
                dry_run=dry_run,
                from_date=from_date,
                to_date=to_date,
                year=year,
                intervals=fetch_intervals,
                on_progress=_spot_progress,
                rate_observer=make_rate_observer(state),
                session_rate=_session_rate,
                auto_retry=True,
                until_clean=True,
                on_ledger_sync=_ledger_sync,
            )
            _last_pull_verdict["verdict"] = report.verdict.to_dict()
            return report

        # Initial total = symbols × the chosen fetch intervals (1d / 1m / both) — a
        # placeholder the first per-WINDOW ledger sync re-scales to the planned window
        # count within ~1 s (DEC-SPOT-WINDOW-PROGRESS-001; the bar then tracks settled
        # WINDOWS, matching the /coverage "settled" headline).
        unit_total = len(instruments) * len(fetch_intervals)
        # ledger_synced=True: the per-pair hook only refreshes the "current" label; the
        # bar's completed / total / bars / failed are driven per-WINDOW from the live
        # WindowLedger via _ledger_sync, so it advances continuously instead of ticking
        # once per finished symbol (DEC-SPOT-WINDOW-PROGRESS-001).
        started = _pull_job.start(_work, total=unit_total, detail=detail, ledger_synced=True)
        return JSONResponse(
            {
                "started": started,
                "kind": kind,
                "symbol": symbol,
                "symbols": symbols,
                "total": unit_total,
                "dry_run": dry_run,
            }
        )

    @app.post("/api/v1/universe/pull-expiries-contracts")
    async def api_universe_pull_expiries_contracts(
        kind: str | None = Query(default=None),
        symbol: str | None = Query(default=None),
        symbols: str | None = Query(default=None),
        from_date: str | None = Query(default=None),
        to_date: str | None = Query(default=None),
    ) -> JSONResponse:
        """POST /api/v1/universe/pull-expiries-contracts — METADATA-only chain pull.

        Backs the "⛓ Pull expiries + contracts" button (DEC-DERIV-METADATA-ROUTE-001,
        PR-3 of the metadata/historical split). It enumerates + PERSISTS the F&O
        chain SHAPE — every expiry DATE + every listed contract SYMBOL for the
        selected underlyings — into the separate ``registry.duckdb`` cache via
        :func:`brutex.dashboard.metadata_pull.pull_expiries_contracts` on the
        single-flight background job (poll ``/pull-progress``), and pulls **ZERO
        historical candles** (the heavy minute fill stays behind its own separate
        "Pull historical" button — PR-5). The retired fused ``/pull-derivatives``
        route used to enumerate the chain AND download every contract's weeks-scale
        candles on one click; PR-6 (DEC-DERIV-DECOMMISSION-FUSED-001) decommissioned
        that candle path and repointed the bulk derivative buttons here, so every
        derivative button now lands only the cheap, NSE-verifiable chain shape in
        minutes (the candle pull is exclusively the separate "Pull historical"
        button). Selection precedence mirrors ``/pull-all``: ``symbols``
        (ticked many) > ``symbol`` (one) > ``kind`` (every admitted index/stock);
        a bad selection is a 400. ``from_date`` / ``to_date`` narrow the Phase-1
        YEAR grid (the metadata grid is year-granular) — absent ⇒ the full
        2020 → current-year vendor-history floor; a reversed or non-ISO range is a
        400. ``dry_run`` is forced when Groww creds are absent, and the dry-run
        enumerates an honest EMPTY chain (strikes/expiries are real vendor data,
        never fabricated offline, Golden Rule #1).
        """
        dry_run = not groww_credentials_present()
        try:
            years = _resolve_metadata_years(from_date, to_date)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if symbols is not None:
            try:
                instruments = resolve_pull_symbols(symbols, _require_store().get)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            detail = f"{len(instruments)} symbols metadata"
        elif symbol is not None:
            store = _require_store()
            instrument = store.get(symbol)
            if instrument is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"unknown security_id {symbol!r} — not in the instrument store",
                )
            instruments = [instrument]
            detail = f"{symbol} metadata"
        elif kind is not None:
            instruments = _select_bulk_targets(kind)
            detail = f"{kind} metadata"
        else:
            raise HTTPException(
                status_code=400,
                detail="pull-expiries-contracts requires ?symbols=..., ?symbol=... or ?kind=...",
            )
        # Skip reference-only underlyings BEFORE the chain pull's network calls
        # (DEC-SKIP-REF-METADATA-001). A REFERENCE_ONLY underlying — a spot-only
        # index (INDIAVIX / MIDCAP50 / NIFTY100, …) or a non-F&O NTM constituent
        # (DEC-NTM-GROWW-REPOINT-001 / DEC-NON-OPTION-INDEX-REFTAG-001) — has NO
        # F&O chain, so ``get_expiries`` would return empty and persist nothing
        # (already SAFE), but it still costs one wasted empty network call. The
        # store's ``reference_only_security_ids`` is the SAME set the /universe REF
        # badge uses; partition on it so a mistaken REF selection is a TRUE
        # zero-call no-op. The set is reported (never silently dropped); TRADABLE
        # behaviour is byte-unchanged (observability + an efficiency guard, NOT a
        # behaviour change for F&O underlyings).
        reference_only_ids = _require_store().reference_only_security_ids()
        tradable = [inst for inst in instruments if inst.security_id not in reference_only_ids]
        skipped_reference_only = [
            inst.symbol_name for inst in instruments if inst.security_id in reference_only_ids
        ]
        if not tradable:
            _LOG.info(
                "universe.pull_expiries_contracts.all_reference_only",
                kind=kind,
                symbol=symbol,
                symbols=symbols,
                skipped_reference_only=skipped_reference_only,
                skipped_count=len(skipped_reference_only),
            )
            note = (
                f"all {len(skipped_reference_only)} selected are reference-only "
                "(no F&O chain) — nothing to pull"
            )
            return JSONResponse(
                {
                    "started": False,
                    "kind": kind,
                    "symbol": symbol,
                    "symbols": symbols,
                    "years": years,
                    "dry_run": dry_run,
                    "skipped_reference_only": skipped_reference_only,
                    "skipped_count": len(skipped_reference_only),
                    "note": note,
                }
            )
        instruments = tradable
        # The registry cache is a sibling of the lean instruments.duckdb master
        # (DEC-DERIV-METADATA-HISTORICAL-SPLIT-001); the wired InstrumentStore IS
        # the coverage_store (its upsert_coverage / list_coverage back /coverage).
        lake_root = _resolve_pull_lake_root(instrument_store, dry_run=dry_run)
        registry_path = lake_root / "registry.duckdb"
        _LOG.info(
            "universe.pull_expiries_contracts.requested",
            kind=kind,
            symbol=symbol,
            symbols=symbols,
            count=len(instruments),
            skipped_reference_only=skipped_reference_only,
            skipped_count=len(skipped_reference_only),
            years=years,
            from_date=from_date,
            to_date=to_date,
            dry_run=dry_run,
        )

        def _work(on_progress: ProgressHook) -> object:  # pragma: no cover — runs on the job thread
            """Background body: enumerate + persist the chain METADATA (no candles).

            Wires LIVE incremental progress (DEC-METADATA-LIVE-PROGRESS-001): the
            metadata pull's ABSOLUTE ``(completed, total, current)`` hook is mapped onto
            the SAME ``sync_from_ledger`` resync the spot pull uses
            (DEC-SPOT-WINDOW-PROGRESS-001), so the ``/universe`` bar advances per
            (underlying, year) expiry cell (Phase 1) and per contract job (Phase 2)
            instead of freezing at ``0/N``. The job's per-pair 4-arg ``on_progress``
            is not the metadata pull's channel (it has no bars to stream), so it is
            unused here — the bar is driven entirely by the absolute resync below.
            """
            del on_progress  # metadata progress is the absolute resync hook, not the 4-arg one

            # The (completed, total, current) → sync_from_ledger mapping lives in the
            # module-level _metadata_progress_sync (unit-tested); partial binds this job.
            with RegistryStore.open(registry_path) as registry:
                report = pull_expiries_contracts(
                    instruments,
                    registry=registry,
                    coverage_store=instrument_store,
                    years=years,
                    dry_run=dry_run,
                    session_rate=_session_rate,
                    on_progress=partial(_metadata_progress_sync, _pull_job),
                )
            return report.to_dict()

        # ledger_synced=True mirrors the spot pull: the bar's completed/total are driven
        # by the absolute sync_from_ledger resync above (per cell + per contract job),
        # NOT by the per-pair 4-arg hook — so it fills 0→100% instead of freezing
        # (DEC-METADATA-LIVE-PROGRESS-001). The initial total is the instrument count, a
        # placeholder the first cell-settle resync re-scales to cells (then cells+jobs).
        started = _pull_job.start(_work, total=len(instruments), detail=detail, ledger_synced=True)
        return JSONResponse(
            {
                "started": started,
                "kind": kind,
                "symbol": symbol,
                "symbols": symbols,
                "years": years,
                "dry_run": dry_run,
                "skipped_reference_only": skipped_reference_only,
                "skipped_count": len(skipped_reference_only),
            }
        )

    def _start_composite_historical_pull(
        *,
        symbols: str | None,
        interval: str,
        intervals: Sequence[str],
        from_date: str | None,
        to_date: str | None,
        from_date_arg: date | None,
        to_date_arg: date | None,
        until_clean: bool,
        dry_run: bool,
        lake_root: Path,
    ) -> JSONResponse:
        """Start the one-click multi-select metadata→candle COMPOSITE on the pull job.

        DEC-FNO-ONECLICK-HISTORICAL-001: resolve ``?symbols=`` to instruments (an
        unknown / empty selection is an honest 400), partition + REPORT the
        reference-only skip set (the SAME guard the metadata route uses), then run
        :func:`brutex.dashboard.composite_pull.pull_historical_for_underlyings` on the
        single-flight background job (poll ``/pull-progress``). An ALL-reference-only
        selection is an honest no-op (``started: False``, never a 400).
        """
        if symbols is None:
            raise HTTPException(
                status_code=400,
                detail="enumerate=true requires ?symbols=a,b,c (the multi-select)",
            )
        try:
            instruments = resolve_pull_symbols(symbols, _require_store().get)
            years = _resolve_metadata_years(from_date, to_date)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        reference_only_ids = _require_store().reference_only_security_ids()
        skipped_reference_only = [
            inst.symbol_name for inst in instruments if inst.security_id in reference_only_ids
        ]
        tradable_count = len(instruments) - len(skipped_reference_only)
        registry_path = lake_root / "registry.duckdb"
        _LOG.info(
            "universe.pull_historical.composite_requested",
            symbols=symbols,
            count=len(instruments),
            tradable=tradable_count,
            skipped_reference_only=skipped_reference_only,
            skipped_count=len(skipped_reference_only),
            interval=interval,
            until_clean=until_clean,
            dry_run=dry_run,
        )
        if tradable_count == 0:
            note = (
                f"all {len(skipped_reference_only)} selected are reference-only "
                "(no F&O chain) — nothing to pull"
            )
            return JSONResponse(
                {
                    "started": False,
                    "enumerate": True,
                    "symbols": symbols,
                    "interval": interval,
                    "intervals": list(intervals),
                    "skipped_reference_only": skipped_reference_only,
                    "skipped_count": len(skipped_reference_only),
                    "dry_run": dry_run,
                    "note": note,
                }
            )

        def _work(on_progress: ProgressHook) -> object:  # pragma: no cover — runs on the job thread
            """Background body: metadata refresh THEN candle pull over the cached rows."""

            def _hist_progress(label: str, bars: int, failed: bool) -> None:
                on_progress(label, bars, failed, False)

            with RegistryStore.open(registry_path) as registry:
                report = pull_historical_for_underlyings(
                    instruments,
                    reference_only_ids=reference_only_ids,
                    registry=registry,
                    coverage_store=instrument_store,
                    lake_root=lake_root,
                    dry_run=dry_run,
                    intervals=intervals,
                    years=years,
                    from_date=from_date_arg,
                    to_date=to_date_arg,
                    on_progress=_hist_progress,
                    session_rate=_session_rate,
                    until_clean=until_clean,
                )
            return report.to_dict()

        started = _pull_job.start(_work, total=tradable_count, detail=f"{tradable_count} composite")
        return JSONResponse(
            {
                "started": started,
                "enumerate": True,
                "symbols": symbols,
                "interval": interval,
                "intervals": list(intervals),
                "skipped_reference_only": skipped_reference_only,
                "skipped_count": len(skipped_reference_only),
                "tradable": tradable_count,
                "until_clean": until_clean,
                "dry_run": dry_run,
            }
        )

    @app.post("/api/v1/universe/pull-historical")
    async def api_universe_pull_historical(
        contract: str | None = Query(default=None),
        contracts: str | None = Query(default=None),
        underlying: str | None = Query(default=None),
        expiry: str | None = Query(default=None),
        kind: str | None = Query(default=None),
        all: bool = Query(default=False),  # noqa: A002 — the public ?all=true query name
        symbols: str | None = Query(default=None),
        enumerate: bool = Query(default=False),  # noqa: A002 — the public ?enumerate flag name
        interval: str = Query(default="1minute"),
        from_date: str | None = Query(default=None),
        to_date: str | None = Query(default=None),
        until_clean: bool = Query(default=True),
    ) -> JSONResponse:
        """POST /api/v1/universe/pull-historical — download OHLC for the SELECTED contracts.

        Backs the "⤓ Pull historical" button (DEC-DERIV-PULL-HISTORICAL-001, PR-5 — the
        capstone of the metadata/historical split). The HEAVY half: the metadata-only
        "⛓ Pull expiries + contracts" button (PR-3) has already cached every expiry DATE +
        contract SYMBOL in the separate ``registry.duckdb``; this route resolves an
        operator SELECTION to those cached
        :class:`~brutex.data.registry_store.ContractRow` rows and pulls THEIR candles into
        the verified candle lake (``bars/<exch>/FNO/<symbol>/<interval>/`` — PATH A) via
        :func:`brutex.dashboard.historical_pull.pull_contract_candles` on the single-flight
        background job (poll ``/pull-progress``), WITHOUT re-enumerating the chain.

        Selection grammar (first non-empty wins): ``?symbols=a,b,c`` (the ONE-CLICK
        multi-select COMPOSITE — see below) · ``?contract=NSE-NIFTY-02Jan20-12100-CE``
        (single) · ``?contracts=a,b,c`` (multi) · ``?underlying=NIFTY[&expiry=2020-01-02]``
        (one expiry's chain or a whole underlying) · ``?kind=index|stock`` (every cached
        contract of that kind's admitted underlyings) · ``?all=true`` (every cached
        contract). ``interval`` is accepted (``1day`` / ``1minute`` / ``both``) but
        F&O ALWAYS fetches ``1minute`` ONLY and DERIVES ``1day``
        (DEC-1MINUTE-ONLY-DERIVE-1DAY-001); the param only affects the echoed report
        field. A bad ``interval`` or ``expiry`` is a 400. An EMPTY registry / no-match is an honest 400
        pointing at the metadata button (NEVER a fabricated contract — Golden Rule #1).
        ``until_clean`` DEFAULTS to ``true`` (DEC-GROWW-DERIV-UNTIL-CLEAN-001 #730 —
        auto-retry every failed window until the selection drains clean). ``dry_run`` is
        forced when Groww creds are absent (the empty-candle client appends 0 bars — an
        expired contract's OHLC is real vendor data, never fabricated offline).

        **One-click multi-select COMPOSITE (DEC-FNO-ONECLICK-HISTORICAL-001).** When
        ``?symbols=`` is supplied (the multi-select the spot / metadata buttons send),
        the route dispatches to
        :func:`brutex.dashboard.composite_pull.pull_historical_for_underlyings`, which
        for EACH F&O-eligible selected underlying runs (a) the GUARDED metadata refresh
        (the SAME reference-only partition the metadata route uses — index / India VIX /
        non-F&O NTM are SKIPPED, a true zero-call no-op, reported never dropped) THEN
        (b) the contract candle pull over the FRESHLY-cached registry rows (both FUT and
        CE/PE legs — superseding the stale never-futures decree #602 for the lake pull).
        The two existing spines are COMPOSED, not fused (the candle engine never
        re-enumerates). ``enumerate=true`` is the explicit composite flag (the route
        also infers the composite whenever ``?symbols=`` is present). An unknown symbol
        in ``?symbols=`` is a 400; an ALL-reference-only selection is an honest no-op.
        """
        dry_run = not groww_credentials_present()
        try:
            intervals = resolve_pull_intervals(interval)
            expiry_date = _parse_iso_date_arg(expiry, "expiry") if expiry is not None else None
            from_date_arg = _parse_iso_date_arg(from_date, "from_date") if from_date else None
            to_date_arg = _parse_iso_date_arg(to_date, "to_date") if to_date else None
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        lake_root = _resolve_pull_lake_root(instrument_store, dry_run=dry_run)
        # ── One-click multi-select COMPOSITE (DEC-FNO-ONECLICK-HISTORICAL-001) ──────
        # ``?symbols=`` (or an explicit ``enumerate=true``) routes to the metadata→candle
        # composite: refresh each F&O-eligible underlying's chain THEN pull its contracts'
        # candles, reusing BOTH existing spines + the reference-only guard. The single-
        # underlying / contract / kind / all grammar below is byte-unchanged.
        if symbols is not None or enumerate:
            return _start_composite_historical_pull(
                symbols=symbols,
                interval=interval,
                intervals=intervals,
                from_date=from_date,
                to_date=to_date,
                from_date_arg=from_date_arg,
                to_date_arg=to_date_arg,
                until_clean=until_clean,
                dry_run=dry_run,
                lake_root=lake_root,
            )
        registry_path = lake_root / "registry.duckdb"
        # Resolve the selection to concrete registry rows BEFORE the job starts, so an
        # empty/no-match selection is an immediate honest 400 (never a fabricated contract).
        with RegistryStore.open(registry_path) as registry:
            selected = _resolve_historical_contracts(
                registry,
                contract=contract,
                contracts=contracts,
                underlying=underlying,
                expiry_date=expiry_date,
                kind=kind,
                all_cached=all,
                select_kind=_select_bulk_targets,
            )
        if not selected:
            raise HTTPException(
                status_code=400,
                detail=(
                    "no cached contracts matched — pull expiries + contracts first "
                    "(the ⛓ button), then select a contract / chain / underlying / all"
                ),
            )
        detail = f"{len(selected)} contracts historical"
        _LOG.info(
            "universe.pull_historical.requested",
            contract=contract,
            contracts=contracts,
            underlying=underlying,
            expiry=expiry,
            kind=kind,
            all=all,
            count=len(selected),
            interval=interval,
            until_clean=until_clean,
            dry_run=dry_run,
        )

        def _work(on_progress: ProgressHook) -> object:  # pragma: no cover — runs on the job thread
            """Background body: pull the selected contracts' candles (Path A, until-clean)."""

            # The historical ingest fires a 3-arg Groww ProgressHook per (contract,
            # interval) pair; adapt it onto the job's 4-arg hook (no known-gap concept).
            def _hist_progress(label: str, bars: int, failed: bool) -> None:
                on_progress(label, bars, failed, False)

            with RegistryStore.open(registry_path) as registry:
                report = pull_contract_candles(
                    selected,
                    lake_root=lake_root,
                    dry_run=dry_run,
                    intervals=intervals,
                    registry=registry,
                    from_date=from_date_arg,
                    to_date=to_date_arg,
                    on_progress=_hist_progress,
                    session_rate=_session_rate,
                    until_clean=until_clean,
                )
            return report.to_dict()

        started = _pull_job.start(_work, total=len(selected), detail=detail)
        return JSONResponse(
            {
                "started": started,
                "contract": contract,
                "contracts": contracts,
                "underlying": underlying,
                "expiry": expiry,
                "kind": kind,
                "all": all,
                "interval": interval,
                "intervals": list(intervals),
                "selected": len(selected),
                "until_clean": until_clean,
                "dry_run": dry_run,
            }
        )

    @app.post("/api/v1/universe/pull-pipeline")
    async def api_universe_pull_pipeline(
        symbols: str | None = Query(default=None),
        symbol: str | None = Query(default=None),
    ) -> JSONResponse:
        """POST /api/v1/universe/pull-pipeline — the ONE-CLICK pull(+movers) pipeline.

        Backs BOTH the "▶ Pull EVERYTHING" hero button (no selection → the WHOLE
        admitted universe — DEC-ONE-CLICK-PIPELINE-001) AND the "▶ Pull selected · N
        — full pipeline" button (``?symbols=a,b,c`` → ONLY the ticked underlyings —
        DEC-PULL-SELECTED-PIPELINE-001). Precedence ``symbols`` > ``symbol`` >
        whole-universe is resolved by the SHARED
        :func:`~brutex.dashboard.pull_controller.resolve_pull_targets` (O(1) per id,
        fail-CLOSED on an empty/unknown selection → 400, so a typo never silently
        pulls a partial set). Chains the THREE existing, separately-tested pull
        spines in the exact order the operator asked for — "start from pull spot,
        once it is successfully completed then pull expiries and contracts, and when
        it is successfully completed then pull historical":

        1. **Spot** — :func:`~brutex.dashboard.pull_controller.pull_all_spot`.
        2. **Metadata** — :func:`~brutex.dashboard.metadata_pull.pull_expiries_contracts`.
        3. **Historical** — :func:`~brutex.dashboard.composite_pull.pull_historical_for_underlyings`.
        4. **Movers (gated)** — warm the default movers snapshots ONLY after a
           fully-successful fetch (DEC-PULL-SELECTED-PIPELINE-001 — "until/unless
           successful fetches happen, never run movers").

        The GATE between phases is the pure
        :func:`~brutex.dashboard.pipeline_pull.run_full_pipeline`: a phase that ends
        with a blocking failure (dead token, daily quota, still-failing windows, or
        a raised chain enumeration) STOPS the chain — the later phases never make a
        doomed vendor call and are reported ``None`` (fail-CLOSED, never a false
        "complete"). Runs on the single-flight background job (returns at once; poll
        ``/pull-progress``). ``dry_run`` is forced when Groww creds are absent (the
        PRIMARY gate post-cutover, DEC-DHAN-GROWW-REMIGRATE-001) — a no-creds run
        honestly pulls nothing and stops at an empty chain (never fabricated data,
        Golden Rule #1).
        """
        dry_run = not groww_credentials_present()
        try:
            targets, target_source = resolve_pull_targets(
                symbols=symbols,
                symbol=symbol,
                lookup=_require_store().get,
                whole_universe=lambda: (
                    _select_bulk_targets("index") + _select_bulk_targets("stock")
                ),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        years = _resolve_metadata_years(None, None)
        fetch_intervals = resolve_fetch_intervals(None)
        lake_root = _resolve_pull_lake_root(instrument_store, dry_run=dry_run)
        registry_path = lake_root / "registry.duckdb"
        reference_only_ids = _require_store().reference_only_security_ids()
        tradable = [inst for inst in targets if inst.security_id not in reference_only_ids]
        _LOG.info(
            "universe.pull_pipeline.requested",
            targets=len(targets),
            tradable=len(tradable),
            target_source=target_source,
            years=years,
            dry_run=dry_run,
        )

        def _work(on_progress: ProgressHook) -> object:  # pragma: no cover — runs on the job thread
            """Background body: run the gated spot→metadata→historical pipeline.

            Binds the three INJECTED callables of
            :func:`~brutex.dashboard.pipeline_pull.run_full_pipeline` to the real
            pull spines (with their store / registry / lake / rate dependencies),
            so the sequencer + gates stay 100%-testable offline while this thin
            wiring streams live progress through the shared single-flight job.
            """

            def _phase_progress(label: str, bars: int, failed: bool) -> None:
                on_progress(label, bars, failed, False)

            def _ledger_sync(
                completed: int, total: int, bars: int, failed: int, current: str
            ) -> None:
                _pull_job.sync_from_ledger(
                    completed=completed, total=total, bars=bars, failed=failed, current=current
                )

            def _run_spot() -> BulkSpotReport:
                return pull_all_spot(
                    targets,
                    lake_root=lake_root,
                    dry_run=dry_run,
                    from_date=None,
                    to_date=None,
                    year=None,
                    intervals=fetch_intervals,
                    on_progress=_phase_progress,
                    rate_observer=make_rate_observer(state),
                    session_rate=_session_rate,
                    auto_retry=True,
                    until_clean=True,
                    on_ledger_sync=_ledger_sync,
                )

            def _run_metadata() -> MetadataPullReport:
                with RegistryStore.open(registry_path) as registry:
                    return pull_expiries_contracts(
                        tradable,
                        registry=registry,
                        coverage_store=instrument_store,
                        years=years,
                        dry_run=dry_run,
                        session_rate=_session_rate,
                        on_progress=partial(_metadata_progress_sync, _pull_job),
                    )

            def _run_historical() -> CompositePullReport:
                with RegistryStore.open(registry_path) as registry:
                    return pull_historical_for_underlyings(
                        targets,
                        reference_only_ids=reference_only_ids,
                        registry=registry,
                        coverage_store=instrument_store,
                        lake_root=lake_root,
                        dry_run=dry_run,
                        intervals=("1minute",),
                        years=years,
                        on_progress=_phase_progress,
                        session_rate=_session_rate,
                        until_clean=True,
                    )

            def _warm_movers() -> int:
                """Gated phase 4 (DEC-PULL-MOVERS-WARM-001): warm the default movers snapshots.

                Runs ONLY after a fully-successful fetch (run_full_pipeline's gate). Targets the
                LATEST available spot day (never 'today' — the pull fills only to the previous
                trading day, so a today-warm would cache empties), at the current post-pull
                generation, so the operator's first /movers open is an O(1) cache hit. Reuses
                the EXACT candidate_key + compute the /api/v1/movers route uses (no drift,
                CLAUDE.md §11 COMMON). Returns how many of spot/futures/options warmed non-empty.

                Whole-body best-effort: the date resolution + every job is wrapped so a
                transient lake-read race NEVER propagates to fail the already-successful pull
                (Correctness of the pull > the warm's latency benefit, DEC-PULL-MOVERS-WARM-001).
                """
                try:
                    warm_date = latest_movers_date(lake_root)
                except Exception as exc:  # pylint: disable=broad-except - best-effort warm
                    _LOG.warning("movers.warm.latest_date_failed", error=str(exc))
                    return 0
                if warm_date is None:
                    return 0
                gen = _summary_cache.generation  # post-pull generation (the pull bumped it)
                tier, trk = normalize_options_selectors(None, None)  # near / all

                def _spot_job() -> int:
                    cands = _movers_cache.get_or_compute(
                        candidate_key(
                            asset="spot",
                            exchange=None,
                            interval=DEFAULT_INTERVAL,
                            date=warm_date,
                            time=None,
                        ),
                        generation=gen,
                        compute=lambda: compute_spot_candidates(
                            lake_root=lake_root,
                            interval=DEFAULT_INTERVAL,
                            date=warm_date,
                            time=None,
                            exchange=None,
                        ),
                    )
                    return len(cands.snapshots)

                def _fut_job() -> int:
                    cands = _movers_cache.get_or_compute(
                        candidate_key(
                            asset="futures",
                            exchange=None,
                            interval=DEFAULT_INTERVAL,
                            date=warm_date,
                            time=None,
                            expiry_tier=tier,
                        ),
                        generation=gen,
                        compute=lambda: compute_futures_candidates(
                            lake_root=lake_root,
                            interval=DEFAULT_INTERVAL,
                            date=warm_date,
                            time=None,
                            exchange=None,
                            expiry_tier=tier,
                        ),
                    )
                    return len(cands.movers)

                def _opt_job() -> int:
                    cands = _movers_cache.get_or_compute(
                        candidate_key(
                            asset="options",
                            exchange=None,
                            interval=DEFAULT_INTERVAL,
                            date=warm_date,
                            time=None,
                            expiry_tier=tier,
                            expiry_track=trk,
                        ),
                        generation=gen,
                        compute=lambda: compute_options_candidates(
                            lake_root=lake_root,
                            interval=DEFAULT_INTERVAL,
                            date=warm_date,
                            time=None,
                            expiry_tier=tier,
                            expiry_track=trk,
                            exchange=None,
                        ),
                    )
                    return len(cands.movers)

                return warm_movers_snapshots([_spot_job, _fut_job, _opt_job])

            report = run_full_pipeline(
                run_spot=_run_spot,
                run_metadata=_run_metadata,
                run_historical=_run_historical,
                run_movers=_warm_movers,
            )
            if report.spot is not None:
                _last_pull_verdict["verdict"] = report.spot.verdict.to_dict()
            return report.to_dict()

        started = _pull_job.start(
            _work, total=len(targets), detail=f"pipeline {len(targets)} symbols", ledger_synced=True
        )
        return JSONResponse(
            {
                "started": started,
                "targets": len(targets),
                "total": len(targets),  # the JS title reads body.total for the first render
                "tradable": len(tradable),
                "target_source": target_source,  # "selection" / "single" / "universe"
                "years": years,
                "dry_run": dry_run,
            }
        )

    @app.post("/api/v1/universe/retry-failed")
    async def api_universe_retry_failed(  # pragma: no cover — thin route over the 100%-tested core
        kind: str | None = Query(default=None),
        symbol: str | None = Query(default=None),
        symbols: str | None = Query(default=None),
        from_date: str | None = Query(default=None),
        to_date: str | None = Query(default=None),
        year: int | None = Query(default=None),
        spot_interval: str | None = Query(default=None),
        until_clean: bool = Query(default=False),
    ) -> JSONResponse:
        """POST /api/v1/universe/retry-failed — MANUAL re-pull of ONLY the leftover failed windows.

        Backs the "Retry failed windows" button (DEC-INGEST-AUTO-RETRY-001 — the
        MANUAL half of the manual⇄automatic retry duality). Routes to
        :func:`brutex.dashboard.pull_controller.pull_all_spot` with
        ``auto_retry=True`` so AFTER the (coverage-skipped) pass, bounded extra
        rounds re-fetch ONLY the still-failed windows — the SAME
        :func:`~brutex.adapters.ingest_common.retry_failed_windows` engine the
        CLI ``brutex fetch --retry-failed`` calls (CLAUDE.md §11 COMMON), rate-safe
        (``≤ 5 r/s``) + idempotent (a now-succeeded window is a no-op). With no
        ``?symbols=`` / ``?symbol=`` / ``?kind=`` the full admitted spot universe
        is targeted (index + stock). The persisted coverage ledger means windows
        already in the lake are skipped, so this re-pulls only the gaps.

        ``dry_run`` is forced when Groww creds are absent (the PRIMARY spot gate
        post-cutover, DEC-DHAN-GROWW-REMIGRATE-001).
        """
        dry_run = not groww_credentials_present()
        # SHARED selection resolver (CLAUDE.md §11 COMMON — the same precedence
        # symbols > symbol > whole-universe the pull-pipeline uses; fail-CLOSED on an
        # empty/unknown selection → 400, DEC-PULL-SELECTED-PIPELINE-001).
        try:
            instruments, target_source = resolve_pull_targets(
                symbols=symbols,
                symbol=symbol,
                lookup=_require_store().get,
                whole_universe=lambda: (
                    _select_bulk_targets("index") + _select_bulk_targets("stock")
                ),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        detail = f"retry {symbols or symbol or (kind or 'all')} spot ({target_source})"
        lake_root = _resolve_pull_lake_root(instrument_store, dry_run=dry_run)
        _LOG.info(
            "universe.retry_failed.requested",
            kind=kind,
            symbol=symbol,
            symbols=symbols,
            count=len(instruments),
            spot_interval=spot_interval,
            dry_run=dry_run,
        )
        # 1-minute is the ONLY fetched granularity (DEC-1MINUTE-ONLY-DERIVE-1DAY-001):
        # ``resolve_fetch_intervals`` ignores the vestigial ``spot_interval`` and always
        # returns ``("1minute",)``, so a retry re-plans the SAME 1-minute windows.
        fetch_intervals = resolve_fetch_intervals(spot_interval)

        def _work(on_progress: ProgressHook) -> object:  # pragma: no cover — runs on the job thread
            """Background body: re-pull every selected symbol with auto-retry on.

            ``?until_clean=true`` (DEC-TOKEN-PREFLIGHT-001) escalates to the bounded
            re-pull-until-clean loop (clean / token-dead / budget) — the dashboard
            twin of ``brutex fetch --until-clean``; default-off keeps the prior
            single auto-retry pass.
            """

            # Adapt the job's 4-arg hook (label, bars, failed, gap) onto the
            # Groww spot ProgressHook (label, bars, failed) — Groww has no
            # known-gap concept, so ``gap`` is always ``False`` here (PR-J4b).
            def _spot_progress(label: str, bars: int, failed: bool) -> None:
                on_progress(label, bars, failed, False)

            report = pull_all_spot(
                instruments,
                lake_root=lake_root,
                dry_run=dry_run,
                from_date=from_date,
                to_date=to_date,
                year=year,
                intervals=fetch_intervals,
                on_progress=_spot_progress,
                rate_observer=make_rate_observer(state),
                session_rate=_session_rate,
                auto_retry=True,
                until_clean=until_clean,
            )
            _last_pull_verdict["verdict"] = report.verdict.to_dict()
            return report

        unit_total = len(instruments) * len(fetch_intervals)
        started = _pull_job.start(_work, total=unit_total, detail=detail)
        return JSONResponse(
            {
                "started": started,
                "kind": kind,
                "symbol": symbol,
                "symbols": symbols,
                "total": unit_total,
                "dry_run": dry_run,
                "retry_failed": True,
                "until_clean": until_clean,
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

    @app.get("/api/v1/universe/pull-verdict")
    async def api_universe_pull_verdict() -> JSONResponse:  # pragma: no cover — thin route
        """GET /api/v1/universe/pull-verdict — the last finished pull's banner verdict.

        The MANUAL surface of DEC-VERDICT-BANNER-001: maps the most-recent
        completed pull / retry verdict (stamped by the background ``_work``) through
        the pure :func:`brutex.dashboard.verdict_banner.banner_for` helper into the
        display-ready banner (``kind`` / ``css_class`` / ``colour`` / ``headline`` /
        ``action`` / ``pending_windows`` / ``show_retry_button``). ``/universe``
        fetches this after a pull completes (AUTOMATIC) and on a manual refresh
        (MANUAL) to render / update the coloured banner. ``{"present": false}`` until
        the first pull lands. This route only DISPLAYS the already-computed verdict —
        it never fetches data or retries (Golden Rule #10); the "Retry failed
        windows" button targets the existing ``/retry-failed`` engine.
        """
        raw = _last_pull_verdict["verdict"]
        if raw is None:
            return JSONResponse({"present": False})
        return JSONResponse({"present": True, **banner_for(raw).to_dict()})

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
            The engine ``symbol_name`` / vendor ``SECURITY_ID`` the universe grid
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
        vendor history window (2020 → current year); the actual rows come
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
        counts as *pulled*; each row's ``completeness`` chip asserts ONLY
        ledger-settledness over planner-planned ranges
        (DEC-SPOT-COVERAGE-COMPLETENESS-001 — partially executes the #397
        follow-up; a trading-day gap audit is NOT claimed). When no lake root
        is wired (a store opened without a filesystem path) the payload is an
        empty summary, never a 500.
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

        def _rows_and_rollup() -> dict[str, Any]:
            """Lake rows via SummaryCache + the ledger rollup PER REQUEST.

            The rollup deliberately lives OUTSIDE the SummaryCache (hostile C8):
            ledger flushes never bump that cache's generation, so fusing the
            join into ``_recompute`` would freeze the chips until the next LAKE
            write. ``spot_window_rollup`` carries its own mtime-keyed cache, so
            the per-request cost is O(datasets) between ledger flushes.
            """
            rows = _summary_cache.get_or_refresh(SPOT, recompute=_recompute)
            return build_spot_coverage_payload(rows, spot_window_rollup(lake))

        return JSONResponse(await run_in_threadpool(_rows_and_rollup))

    @app.get("/api/v1/completeness/scoreboard")
    async def api_completeness_scoreboard() -> JSONResponse:
        """GET /api/v1/completeness/scoreboard — per-symbol + aggregate data completeness.

        The Data Completeness Scoreboard (DEC-COMPLETENESS-SCOREBOARD-001): for every spot
        symbol — indices vs equities-cash, **separately** — its 1-minute spot
        completeness (EXACT, ledger-backed; 1-day is DERIVED from 1-minute so it is not
        scored) and overall verdict, PLUS for every option
        underlying its expiries/contracts pulled + the honest ``stable``/``growing``/
        ``partial``/``failed`` verdict (NO fabricated % — the vendor exposes no expected
        total, so "stable" = all currently-listed expiries pulled + nothing new outstanding).
        Pure-engine delegation
        (:func:`brutex.dashboard.completeness.build_completeness_scoreboard` over the same
        ledger rollup the ``/coverage`` chips use); READ-ONLY, no pull logic.

        **Honest envelope (Golden Rule #10):** spot is exact (window-ledger); options report
        pulled-counts + stability, never an invented percentage. No lake root wired → an
        empty (well-shaped) scoreboard, never a 500.
        """
        return JSONResponse(await _completeness_board())

    def _completeness_inputs(
        store: Any, lake: Path
    ) -> tuple[list[dict[str, Any]], dict[str, str], list[Any]]:
        """Gather the (annotated spot rows, symbol→category map, option coverage records).

        The ONE lake+ledger read shared by the scoreboard, the CSV report, AND the per-symbol
        ``/universe`` map (CLAUDE.md §11 COMMON — one source, three projections). Runs on a
        threadpool worker, building ``store.connection`` here so the store RLock stays on one
        thread (DEC-DASHBOARD-FASTREAD-001).

        DEC-COMPLETENESS-O1-LATENCY-001: the O(datasets) full-lake spot scan
        (:func:`summarize_spot_datasets`, a per-dataset ``COUNT(*)``/``MIN``/``MAX`` Parquet
        footer read) is routed through the SHARED ``_summary_cache`` SPOT entry — the SAME
        cache ``/coverage/spot`` populates — instead of the prior UNCACHED direct call that
        re-ran the whole scan on every scoreboard / CSV / ``/universe`` completeness load
        (the operator's 7–9 s page). A repeat load is now O(1) on that scan; a pull/lake
        write marks the cache dirty so the next eligible read recomputes. The ledger rollup
        (:func:`spot_window_rollup`, its OWN mtime cache) stays PER-REQUEST and OUTSIDE the
        SummaryCache — exactly the hostile-C8 freshness contract ``/coverage/spot`` uses — so
        a ledger flush (no lake write) still refreshes the chips between scans.
        """
        categories = categorize_instruments(
            store.list_index_equity_instruments(),
            # By-TYPE index test (DEC-COMPLETENESS-INDEX-CATEGORY-001): EVERY non-brute-forced
            # index — FINNIFTY AND the spot-only / sectoral / broad-market indices (e.g.
            # NIFTY IT) — is still an INDEX for the asset-class split, NOT "equity (cash)".
            # Reference-only (India VIX) wins first inside categorize_instruments, so VIX
            # stays REFERENCE; the brute-force ENGINE tier (is_in_brute_force_scope) is
            # untouched — this is DISPLAY categorisation only.
            is_index=is_index_instrument,
            is_reference=is_reference_only,
        )
        spot_rows = build_spot_coverage_payload(
            _summary_cache.get_or_refresh(
                SPOT, recompute=lambda: summarize_spot_datasets(store.connection, lake)
            ),
            spot_window_rollup(lake),
        )["rows"]
        # OBSERVE-ONLY reconciliation diagnostic (DEC-COMPLETENESS-RECONCILE-DIAG-001): on a
        # lake-symbol-key ↔ scored-symbol-key divergence (the ``M&M``-empty symptom — a spot
        # underlying reads ``none`` while a near-identical lake key carries the data) this logs
        # ONE structured WARNING naming the offending keys via ``repr()``. It NEITHER alters the
        # returned inputs NOR the scoreboard payload (it only reads the SAME rows scored just
        # below), and is itself best-effort + internally guarded, so it adds no new failure
        # surface — a malformed row logs a warning and returns empty rather than break the page.
        log_symbol_reconcile_from_board(spot_rows, categories)
        return spot_rows, categories, store.list_coverage()

    def _build_completeness_board(store: Any, lake: Path) -> dict[str, Any]:
        """Compose the aggregate + per-symbol scoreboard from the shared inputs."""
        spot_rows, categories, records = _completeness_inputs(store, lake)
        return build_completeness_scoreboard(spot_rows, categories, records, categories)

    def _build_per_symbol_completeness(store: Any, lake: Path) -> dict[str, dict[str, Any]]:
        """Compose the per-symbol completeness map (with dates) from the shared inputs."""
        spot_rows, categories, records = _completeness_inputs(store, lake)
        return per_symbol_completeness(spot_rows, categories, records, categories)

    def _build_completeness_csv(store: Any, lake: Path) -> str:
        """Compose the downloadable CSV — board + per-symbol DATES — from ONE shared lake read.

        DEC-COMPLETENESS-CSV-READABLE-001: the CSV's spot detail is ENRICHED with the same
        explicit first→last bar dates + from-2020 flag the ``/universe`` strip shows, so the
        report is never less informative than the page. One ``_completeness_inputs`` read feeds
        BOTH projections (CLAUDE.md §11 COMMON — no second lake read).
        """
        spot_rows, categories, records = _completeness_inputs(store, lake)
        board = build_completeness_scoreboard(spot_rows, categories, records, categories)
        per_symbol = per_symbol_completeness(spot_rows, categories, records, categories)
        return scoreboard_to_csv(board, per_symbol=per_symbol)

    async def _completeness_board() -> dict[str, Any]:
        """Resolve the store + lake and build the scoreboard (empty + well-shaped if no lake)."""
        store = _require_store()
        lake_root = getattr(store, "path", None)
        if not isinstance(lake_root, Path):
            return build_completeness_scoreboard([], {}, [], {})
        return await run_in_threadpool(_build_completeness_board, store, lake_root.parent)

    async def _completeness_csv() -> str:
        """Resolve the store + lake and build the downloadable CSV (empty + well-shaped if no lake)."""
        store = _require_store()
        lake_root = getattr(store, "path", None)
        if not isinstance(lake_root, Path):
            return scoreboard_to_csv(build_completeness_scoreboard([], {}, [], {}))
        return await run_in_threadpool(_build_completeness_csv, store, lake_root.parent)

    @app.get("/api/v1/completeness/scoreboard.csv")
    async def api_completeness_scoreboard_csv() -> Response:
        """GET /api/v1/completeness/scoreboard.csv — the scoreboard as a downloadable report.

        DEC-COMPLETENESS-PAGE-001. The exact same data as ``/api/v1/completeness/scoreboard``,
        serialised by :func:`brutex.dashboard.completeness.scoreboard_to_csv` into a four-section
        human-readable CSV (spot + option SUMMARY per category, then the per-symbol + per
        -underlying DETAIL) so the operator can answer "how many indices vs equities have the
        full set?" in Excel. READ-ONLY; an empty (well-shaped) report when no lake is wired.
        """
        content = await _completeness_csv()
        return Response(
            content=content,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="completeness-scoreboard.csv"'},
        )

    @app.get("/api/v1/universe/completeness")
    async def api_universe_completeness() -> JSONResponse:
        """GET /api/v1/universe/completeness — per-symbol completeness map for the pull page.

        DEC-COMPLETENESS-UNIVERSE-001. Returns ``{symbol_name: {category, exchange, spot:{
        1minute,overall}, options:{...}}}`` so the ``/universe`` page overlays, on EACH symbol
        row, whether that symbol has the entire 2020→today set — the EXPLICIT earliest/latest
        bar date + a ``from_2020`` flag for the 1-minute spot dimension (1-day is DERIVED from
        1-minute so it is not scored) and the honest
        ``stable``/``growing``/``partial``/``failed`` option status + pulled counts + year span.
        Pure-engine delegation (:func:`brutex.dashboard.completeness.per_symbol_completeness`)
        over the SAME lake+ledger read the scoreboard uses; READ-ONLY, no pull logic. No lake
        root wired → an empty ``{}`` map (the page degrades to "not yet pulled"), never a 500.
        """
        store = _require_store()
        lake_root = getattr(store, "path", None)
        if not isinstance(lake_root, Path):
            return JSONResponse({})
        per_symbol = await run_in_threadpool(
            _build_per_symbol_completeness, store, lake_root.parent
        )
        return JSONResponse(per_symbol)

    def _build_mapping_integrity_report() -> MappingIntegrityReport:
        """Reconcile the live store's eligibility + coverage into one report (thin I/O glue).

        The SINGLE builder shared by the JSON route AND the CSV download (CLAUDE.md §11
        COMMON), so both surfaces serialise the identical verdict — never two drifting
        reconciliations. Gathers the two store reads (eligible underlyings + coverage ledger)
        off the event loop and delegates to the 100%-tested
        :func:`~brutex.data.mapping_integrity.reconcile_mapping_integrity`.
        """
        store = _require_store()
        eligible = [
            EligibleSpot(exchange=exch.value, symbol=sym)
            for exch, sym in store.fno_eligible_underlyings()
        ]
        return reconcile_mapping_integrity(
            eligible_spots=eligible,
            covered_years=coverage_years_from_records(store.list_coverage()),
            expected_years=_coverage_years(),
            whitelist=(),
            now=datetime.now(UTC),
        )

    @app.get("/api/v1/universe/mapping-integrity")
    async def api_universe_mapping_integrity() -> JSONResponse:
        """GET /api/v1/universe/mapping-integrity — the green/red spot↔options mapping verdict.

        DEC-MAPPING-INTEGRITY-RECONCILE-001. Reconciles every options-eligible spot underlying
        (``store.fno_eligible_underlyings``) against the coverage ledger
        (``store.list_coverage``) and returns the honest integrity verdict: each eligible spot
        that pulled ZERO expiries (gating RED — e.g. M&M, the special-character options-fetch
        bug F2 fixes) or only PART of the 2020→today span (amber — e.g. BAJAJ-AUTO's 2024-only).
        This is the real-time, self-checking guarantee that the core spot↔options mapping is
        precise, surfaced as ``is_green`` + a drill-down. Pure delegation to
        :mod:`brutex.data.mapping_integrity` (its grouping + reconciliation are at 100%
        line+branch); READ-ONLY, no pull. No store wired → 503.
        """
        _require_store()
        report = await run_in_threadpool(_build_mapping_integrity_report)
        return JSONResponse(report_to_dict(report))

    @app.get("/api/v1/universe/mapping-integrity.csv")
    async def api_universe_mapping_integrity_csv() -> Response:
        """GET /api/v1/universe/mapping-integrity.csv — the mapping verdict as a download.

        DEC-MAPPING-INTEGRITY-EXPORT-001 — the "open it in Excel and SEE the proof" file
        companion to the live panel + JSON route (#870). Reuses the SAME
        :func:`_build_mapping_integrity_report` builder (one reconciliation, never two that
        drift) and serialises it via the pure
        :func:`brutex.dashboard.mapping_integrity_csv.mapping_integrity_to_csv` (a three-section
        human report; every cell formula-injection-safe via the shared
        :class:`~brutex.dashboard._csv_utils.FormulaSafeWriter`). READ-ONLY, no pull; no store
        wired → 503.
        """
        _require_store()
        report = await run_in_threadpool(_build_mapping_integrity_report)
        return Response(
            content=mapping_integrity_to_csv(report),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="mapping_integrity.csv"',
            },
        )

    def _read_master_integrity() -> dict[str, Any] | None:
        """Read the persisted instrument-master integrity snapshot (``None`` if never loaded).

        The audit runs at ingest (the legs are discarded straight after), so its bounded JSON
        summary is persisted to ``schema_meta`` — this reads that snapshot back. One DB get.
        """
        raw = _require_store().get_meta(MASTER_INTEGRITY_META_KEY)
        if raw is None:
            return None
        parsed: dict[str, Any] = json.loads(raw)
        return parsed

    @app.get("/api/v1/universe/master-integrity")
    async def api_universe_master_integrity() -> JSONResponse:
        """GET /api/v1/universe/master-integrity — the green/red leg↔spot mapping verdict.

        DEC-INSTRUMENT-MASTER-INTEGRITY-001. Returns the structural-integrity snapshot the
        ingest hook persisted to ``schema_meta`` (every FUT/CE/PE leg maps to exactly one
        IDX/EQ spot by symbol AND token, else orphan/ambiguous/disagreement/empty). READ-ONLY;
        ``{"present": false}`` until the instrument master has been loaded once. No store → 503.
        """
        snapshot = await run_in_threadpool(_read_master_integrity)
        if snapshot is None:
            return JSONResponse({"present": False})
        return JSONResponse({"present": True, **snapshot})

    @app.get("/api/v1/universe/master-integrity.csv")
    async def api_universe_master_integrity_csv() -> Response:
        """GET /api/v1/universe/master-integrity.csv — the leg↔spot audit as a download.

        DEC-INSTRUMENT-MASTER-INTEGRITY-001 — the "open it in Excel and SEE the mapping proof"
        file companion. Re-renders the persisted snapshot via
        :func:`brutex.data.master_integrity.report_from_dict` →
        :func:`brutex.dashboard.master_integrity_csv.master_integrity_to_csv` (every cell
        formula-injection-safe). READ-ONLY; a one-line placeholder until the master is loaded.
        No store → 503.
        """
        snapshot = await run_in_threadpool(_read_master_integrity)
        if snapshot is None:
            content = (
                "BruteX — Instrument-Master Integrity Audit\n"
                "no audit recorded yet — load the instrument master first\n"
            )
        else:
            content = master_integrity_to_csv(master_report_from_dict(snapshot))
        return Response(
            content=content,
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="master_integrity.csv"',
            },
        )

    def _read_economics_integrity() -> dict[str, Any] | None:
        """Read the persisted economics-integrity snapshot (``None`` if never loaded)."""
        raw = _require_store().get_meta(ECONOMICS_INTEGRITY_META_KEY)
        if raw is None:
            return None
        parsed: dict[str, Any] = json.loads(raw)
        return parsed

    @app.get("/api/v1/universe/economics-integrity")
    async def api_universe_economics_integrity() -> JSONResponse:
        """GET /api/v1/universe/economics-integrity — the green/red lot-economics verdict.

        DEC-INSTRUMENT-ECONOMICS-INTEGRITY-001. Returns the economics snapshot the ingest hook
        persisted to ``schema_meta``: lot_size must be uniform per (underlying, expiry) — a
        within-expiry mismatch is RED; cross-expiry lot revisions are informational. READ-ONLY;
        ``{"present": false}`` until the master has been loaded once. No store → 503.
        """
        snapshot = await run_in_threadpool(_read_economics_integrity)
        if snapshot is None:
            return JSONResponse({"present": False})
        return JSONResponse({"present": True, **snapshot})

    @app.get("/api/v1/universe/economics-integrity.csv")
    async def api_universe_economics_integrity_csv() -> Response:
        """GET /api/v1/universe/economics-integrity.csv — the lot-economics audit as a download.

        DEC-INSTRUMENT-ECONOMICS-INTEGRITY-001 — re-renders the persisted snapshot via
        :func:`brutex.data.economics_integrity.report_from_dict` →
        :func:`brutex.dashboard.economics_integrity_csv.economics_integrity_to_csv` (every cell
        formula-injection-safe). READ-ONLY; a one-line placeholder until the master is loaded;
        no store → 503.
        """
        snapshot = await run_in_threadpool(_read_economics_integrity)
        if snapshot is None:
            content = (
                "BruteX — Instrument-Master Economics Integrity Audit\n"
                "no audit recorded yet — load the instrument master first\n"
            )
        else:
            content = economics_integrity_to_csv(economics_report_from_dict(snapshot))
        return Response(
            content=content,
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="economics_integrity.csv"',
            },
        )

    @app.get("/api/v1/coverage/windows")
    async def api_coverage_windows(
        market: str | None = Query(default=None),
        status: str | None = Query(default=None),
        limit: int = Query(default=20_000, ge=1),
        offset: int = Query(default=0, ge=0),
    ) -> JSONResponse:
        """GET /api/v1/coverage/windows — per-window PLANNED / SUCCESS / EMPTY / FAILED / SKIPPED.

        The browsable per-window record: one row per fetch window with its status,
        attempt count, bars, first-seen + last error — so the operator SEES exactly
        which windows are planned, stored, empty, failed (needing a re-pull), or
        settled. READ-ONLY (DEC-WINDOW-LEDGER-001, PR-3): reads the durable unified
        ledger Parquet (``<lake>/window_ledger/ledger.parquet``) when present, else
        falls back to the legacy ``window_status.json`` sidecar — never runs a
        pull. Empty when no lake root is wired or no window has been recorded yet
        (never a 500). ``source`` reports which backing answered.

        Scale guards (DEC-OPTION-WINDOW-PLAN-001, hostile C3): ``counts`` are
        always complete; the row list honours ``?market=spot|options`` +
        ``?status=...`` and is capped severity-first (FAILED/EMPTY can never be
        hidden); ``PLANNED`` rows ship only on explicit ``?status=PLANNED``. The
        ledger read is mtime-cached so the 4 s page poll stops re-paying the
        full Parquet load at pre-planned-universe scale (~841k rows measured).
        """
        from brutex.dashboard.db_view import build_window_payload

        store = _require_store()
        lake_root = getattr(store, "path", None)
        if not isinstance(lake_root, Path):
            return JSONResponse({"source": "none", "count": 0, "windows": []})
        lake = lake_root.parent

        def _payload() -> dict[str, Any]:
            """Build the payload with the id→symbol map for PRECISE option rows.

            DEC-WINDOW-OPTION-DECODE-001: option window keys carry the numeric
            vendor SECURITY_ID (``25|NSE_FNO``), so the map lets the projection
            display the operator's symbol (``BANKNIFTY``) instead. Built here on
            the SAME threadpool worker as the read so the store's RLock stays on
            one thread (DEC-DASHBOARD-FASTREAD-001); display-only.
            """
            symbol_for_id = {
                str(i.security_id): i.symbol_name for i in store.list_index_equity_instruments()
            }
            return build_window_payload(
                lake,
                symbol_for_id=symbol_for_id,
                market=market,
                status=status,
                limit=limit,
                offset=offset,
                # DEC-COVERAGE-WINDOWS-O1-001: inject the store's DuckDB connection +
                # the generation-stamped counts cache so the payload is O(page) (DuckDB
                # LIMIT/OFFSET + cached GROUP BY) instead of an O(N) Python scan per poll.
                conn=store.connection,
                count_cache=_window_counts_cache,
                generation=_summary_cache.generation,
            )

        return JSONResponse(await run_in_threadpool(_payload))

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

    # ── Movers (reference-only lake screens — DEC-MOVERS-DASHBOARD-PAGE-001) ──

    @app.get("/api/v1/movers")
    async def api_movers(
        asset: str = Query(default="spot"),
        interval: str = Query(default=DEFAULT_INTERVAL),
        date: str | None = Query(default=None),
        time: str | None = Query(default=None),
        top_n: int = Query(default=DEFAULT_TOP_N, ge=1, le=500),
        expiry: str | None = Query(default=None),
        track: str | None = Query(default=None),
        exchange: str | None = Query(default=None),
    ) -> JSONResponse:
        """GET /api/v1/movers — point-in-time markets-screen-style movers snapshot (DEC-MOVERS-SNAPSHOT-VIEW-001).

        Answers ONE question — *"who are the movers as of a single date, on one
        timeframe, at one time?"* — over the ingested lake. READ-ONLY
        (CLAUDE.md §3): ranks OHLCV the operator already pulled; NEVER touches the
        brute-force engine or the 52-vocab; NOT a live feed (Track-1 has none —
        CLAUDE.md §1). The page renders one ranked list per spot category
        (``price_gainers`` / ``price_losers`` / ``volume`` / ``value``) and sorts
        any column client-side.

        **Latency (DEC-MOVERS-O1-LATENCY-001):** the expensive lake scan is split
        from the cheap rank. The ``top_n``-independent *candidate* set is computed
        once per ``(asset, exchange, interval, date, time[, tier, track])``
        coordinate and cached behind ``_movers_cache`` (invalidated by the same
        write-generation as the coverage cache), so re-clicking a sub-filter,
        nudging ``top_n``, or re-selecting a coordinate is an O(1) hit + a cheap
        re-rank — never a re-scan. The first view of a coordinate still pays the
        real ``O(instruments × bars-read)`` scan (honest envelope, Golden Rule #10),
        bounded per symbol to the day's month Parquet for intraday timeframes.

        Query parameters
        ----------------
        asset
            ``spot`` / ``futures`` / ``options`` — all three wired to the lake.
            Any other (unknown) asset returns an honest "ships after Spot"
            pending payload.
        exchange
            Restrict the ranking to one exchange (``NSE`` / ``BSE``) for an exact
            per-exchange top-N; any other value (blank / unknown) ranks every
            exchange. Part of the cache key, so toggling it re-uses the cached
            scan when the coordinate was already viewed.
        expiry
            Futures + Options — the ``near`` / ``next`` / ``far`` serial-expiry
            tier (blank / unknown ⇒ ``near``); selects that serial contract
            as-of *date* (futures: one contract per underlying at the tier;
            options: ranks only that serial expiry). Part of the cache key, so
            Near / Next / Far cache + rank independently.
        track
            Options only — the ``all`` / ``monthly`` / ``weekly`` expiry cadence
            (blank / unknown ⇒ ``all``); the Near/Next/Far tier ranks within it.
            ``monthly`` = the last expiry per calendar month (structural, no NSE
            weekday rule — Golden Rule #1); ``weekly`` = the rest.
        interval
            The lake timeframe to snapshot (default ``1day``); the 21-timeframe
            vocab (1minute … 1day). An unknown label degrades to "pull spot first".
        date
            The single ISO ``YYYY-MM-DD`` snapshot day; blank ⇒ today (UTC). A
            malformed value is a 400.
        time
            The intraday ``HH:MM`` IST bar to snapshot as-of (blank / daily ⇒ the
            whole-day bar). A malformed value is a 400.
        top_n
            Ranking depth for each category (1..500; default 100 — DEC-MOVERS-COMPLETENESS-001).

        **Honest envelope (Golden Rule #10):** the snapshot is only as good as the
        ingested lake — an empty lake (or a store with no lake root) returns an
        honest ``instrument_count: 0`` payload with a "pull spot first" note,
        never a 500 and never a fabricated mover. % change is vs the PREVIOUS
        day's close; volume/value are the cumulative FRESH session from 09:15.
        """
        store = _require_store()
        try:
            if asset not in ("spot", "futures", "options"):
                # Milestone 1 wired Spot; Milestone 2 Futures; Milestone 3 Options.
                # Any other (unknown) asset still gets an honest pending payload.
                return JSONResponse(
                    pending_asset_payload(
                        asset,
                        interval=interval,
                        date=parse_snapshot_date(date).isoformat(),
                        time=time,
                        top_n=top_n,
                    )
                )
            # Normalise the exchange scope: NSE/BSE rank that exchange exactly;
            # anything else (blank / typo) is the all-exchange population (never an
            # empty-result surprise) — DEC-MOVERS-O1-LATENCY-001.
            exch = exchange if exchange in ("NSE", "BSE") else None
            store_path = getattr(store, "path", None)
            if not isinstance(store_path, Path):
                # No lake root wired → honest empty payload (never a 500), mirroring
                # the /api/v1/coverage/spot no-lake-root degrade. Futures / Options
                # degrade to their own "pull the F&O … first" empty payloads.
                empty_date = parse_snapshot_date(date).isoformat()
                if asset == "futures":
                    return JSONResponse(
                        build_futures_payload(
                            [],
                            interval=interval,
                            date=empty_date,
                            time=time,
                            top_n=top_n,
                            skipped=0,
                        )
                    )
                if asset == "options":
                    return JSONResponse(
                        build_options_payload(
                            [],
                            interval=interval,
                            date=empty_date,
                            time=time,
                            top_n=top_n,
                            skipped=0,
                        )
                    )
                return JSONResponse(
                    build_snapshot_payload(
                        [],
                        interval=interval,
                        date=empty_date,
                        time=time,
                        top_n=top_n,
                        skipped=0,
                    )
                )
            lake_root = store_path.parent
            # Normalise the date once so the cache key is canonical. With NO explicit
            # date, default to the LATEST available spot day (DEC-PULL-MOVERS-WARM-001),
            # NOT today: a historical pull fills the lake only to the previous trading
            # day, so a today-default would show an EMPTY snapshot on open (and miss the
            # post-pull warm, which keys on that same latest day). Falls back to today
            # only when the lake has no spot data yet (an honest empty payload). An
            # EXPLICIT malformed date still raises HERE (synchronously) → 400.
            if date is None:
                date_str = latest_movers_date(lake_root) or parse_snapshot_date(None).isoformat()
            else:
                date_str = parse_snapshot_date(date).isoformat()
            # The current write-generation — a pull/materialize bumps it, so every
            # cached candidate set computed at an older generation is dirty (the
            # page never serves pre-pull data). Reused from the coverage cache.
            generation = _summary_cache.generation

            def _resolve() -> dict[str, Any]:
                """Cache-or-compute the candidate set, then rank to top_n + exchange.

                Runs on a threadpool worker (blocking lake reads + the cache's
                threading.Lock). The ``compute=`` closure performs the real scan
                ONLY on a miss / post-pull; a hit is an O(1) handoff + a cheap
                in-memory rank (DEC-MOVERS-O1-LATENCY-001).
                """
                # Memoise the lake DISCOVERY once per write-generation (the proven
                # `/db` #896 pattern), shared across spot/futures/options, so a tab /
                # section / exchange / expiry switch within one generation REUSES a
                # single `bars/` walk instead of re-walking the whole lake inside each
                # compute on every cache miss (DEC-MOVERS-DISCOVERY-MEMO-001). Computed
                # on the worker thread; a pull bumps `generation` → one fresh walk.
                movers_datasets = _db_datasets_cache.get_or_compute(
                    "movers_candle_datasets",
                    generation=generation,
                    compute=lambda: list_candle_datasets(lake_root),
                )
                if asset == "options":
                    # Options carries the extra Near/Next/Far expiry tier (blank ⇒
                    # near) + the All/Monthly/Weekly cadence track (blank ⇒ all),
                    # normalised so the key and the computed set agree.
                    tier, trk = normalize_options_selectors(expiry, track)
                    key = candidate_key(
                        asset="options",
                        exchange=exch,
                        interval=interval,
                        date=date_str,
                        time=time,
                        expiry_tier=tier,
                        expiry_track=trk,
                    )
                    opt_cands = _movers_cache.get_or_compute(
                        key,
                        generation=generation,
                        compute=lambda: compute_options_candidates(
                            lake_root=lake_root,
                            interval=interval,
                            date=date_str,
                            time=time,
                            expiry_tier=tier,
                            expiry_track=trk,
                            exchange=exch,
                            datasets=movers_datasets,
                        ),
                    )
                    return build_options_payload(
                        opt_cands.movers,
                        interval=interval,
                        date=opt_cands.date,
                        time=time,
                        top_n=top_n,
                        skipped=opt_cands.skipped,
                        expiry_tier=opt_cands.expiry_tier,
                        expiry_track=opt_cands.expiry_track,
                    )
                if asset == "futures":
                    # Futures carries the Near/Next/Far serial-expiry tier (the
                    # ?expiry= selector; blank/unknown ⇒ near), normalised so the
                    # cache key and the computed set agree — so Near / Next / Far
                    # cache + rank independently (never a stale Near row served for
                    # a Far request) — DEC-MOVERS-FUTURES-TIER-001.
                    fut_tier = normalize_futures_tier(expiry)
                    key = candidate_key(
                        asset="futures",
                        exchange=exch,
                        interval=interval,
                        date=date_str,
                        time=time,
                        expiry_tier=fut_tier,
                    )
                    fut_cands = _movers_cache.get_or_compute(
                        key,
                        generation=generation,
                        compute=lambda: compute_futures_candidates(
                            lake_root=lake_root,
                            interval=interval,
                            date=date_str,
                            time=time,
                            exchange=exch,
                            datasets=movers_datasets,
                            expiry_tier=fut_tier,
                        ),
                    )
                    return build_futures_payload(
                        fut_cands.movers,
                        interval=interval,
                        date=fut_cands.date,
                        time=time,
                        top_n=top_n,
                        skipped=fut_cands.skipped,
                        expiry_tier=fut_cands.expiry_tier,
                    )
                key = candidate_key(
                    asset="spot",
                    exchange=exch,
                    interval=interval,
                    date=date_str,
                    time=time,
                )
                spot_cands = _movers_cache.get_or_compute(
                    key,
                    generation=generation,
                    compute=lambda: compute_spot_candidates(
                        lake_root=lake_root,
                        interval=interval,
                        date=date_str,
                        time=time,
                        exchange=exch,
                        datasets=movers_datasets,
                    ),
                )
                return build_snapshot_payload(
                    spot_cands.snapshots,
                    interval=interval,
                    date=spot_cands.date,
                    time=time,
                    top_n=top_n,
                    skipped=spot_cands.skipped,
                )

            payload = await run_in_threadpool(_resolve)
            return JSONResponse(payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

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

    @app.get("/completeness", response_class=HTMLResponse)
    async def completeness_page() -> str:
        """GET /completeness — the Data Completeness Scoreboard page.

        Renders the per-category summary (indices vs equities-cash, SEPARATELY) +
        the per-symbol spot and per-underlying option drill-downs from
        ``/api/v1/completeness/scoreboard``, with a CSV download
        (DEC-COMPLETENESS-SCOREBOARD-001 / DEC-COMPLETENESS-PAGE-001)."""
        return _render_completeness_html()

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
        # Tag each report symbol with option-pullability so /reports can badge
        # F&O vs spot-only at a glance (DEC-OPTIONS-ELIGIBLE-GUARD-001) — reusing
        # the SAME store calls + single-source predicate the /universe explorer
        # uses (no duplication). A symbol absent from the underlying universe
        # (a spot-only dataset) defaults to NOT pullable.
        pullable = {
            (row["exchange"], row["symbol"]): row["option_pullable"]
            for row in list_underlyings(
                store.list_index_equity_instruments(),
                fno_eligible=store.fno_eligible_underlyings(),
                reference_only_ids=store.reference_only_security_ids(),
            )
        }
        return JSONResponse(
            {
                "symbols": [
                    {
                        "exchange": e,
                        "segment": s,
                        "symbol": y,
                        "option_pullable": pullable.get((e, y), False),
                    }
                    for e, s, y in triples
                ],
                "basis": report_basis,
            }
        )

    @app.get("/api/v1/reports/preopen.xlsx")
    async def api_preopen_xlsx(
        start: str | None = Query(None, alias="from"),
        end: str | None = Query(None, alias="to"),
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
            start_d, end_d = _resolve_report_dates(start, end)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
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
        # Self-describing download name (DEC-REPORT-FILENAME-001): basis +
        # the selected symbols + the range — never a selection-blind static name.
        filename = report_filename(
            extension=".xlsx",
            start=start_d,
            end=end_d,
            basis=report_basis,
            symbols=wanted,
        )
        title = f"Pre-Open Market: {start_d.isoformat()} to {end_d.isoformat()}"
        return Response(
            content=render_xlsx(rows, title=title),
            media_type=("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/api/v1/reports/preopen.zip")
    async def api_preopen_zip(
        start: str | None = Query(None, alias="from"),
        end: str | None = Query(None, alias="to"),
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
            start_d, end_d = _resolve_report_dates(start, end)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
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
        # Self-describing download name (DEC-REPORT-FILENAME-001): mode (+ the
        # top-mover threshold where it applies) + basis + the selected symbols +
        # the range — replaces the selection-blind static "preopen_report.zip".
        zip_name = report_filename(
            extension=".zip",
            start=start_d,
            end=end_d,
            basis=report_basis,
            symbols=wanted,
            mode=report_mode,
            min_pct=Decimal(str(min_pct)),
        )
        return Response(
            content=blob,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{zip_name}"'},
        )

    @app.get("/reports", response_class=HTMLResponse)
    async def reports_page() -> str:
        """GET /reports — the pre-open report download page (DEC-PREOPEN-REPORT-001)."""
        return _render_reports_html()

    @app.get("/movers", response_class=HTMLResponse)
    async def movers_page() -> str:
        """GET /movers — the reference-only markets-screen-style movers page (DEC-MOVERS-SNAPSHOT-VIEW-001).

        A point-in-time snapshot view (Spot / Futures / Options tabs; single date
        + timeframe + auto-filled time; click-to-sort on every column) over the
        ingested lake. The page loads on open + on every control change / manual
        Refresh (no auto-poll timer); each load is an O(P) one-time lake pass.
        """
        return _render_movers_html()

    @app.post("/api/v1/cascade/run")
    async def api_cascade_run() -> JSONResponse:  # pragma: no cover — live lake + DuckDB replay
        """POST /api/v1/cascade/run — re-run the Stage-4 cascade on the latest sweep.

        The dashboard "Run cascade" button (DEC-CASCADE-WIRE-001): reloads the
        cascade manifest the most recent ``sweep-all`` wrote under
        ``<lake>/sweeps`` and replays it through the SAME pure core the sweep used,
        regenerating the byte-identical ``cascade-report.json``. Returns the pool /
        candidate / survivor tally as JSON (or a 400 with a clear message when no
        sweep has been cascaded yet). The cascade engine itself is 100%-tested in
        :mod:`brutex.engine.cascade_wire`; this is the thin live-lake trigger.
        """
        store = _require_store()
        lake_root = getattr(store, "path", None)
        if not isinstance(lake_root, Path):
            raise HTTPException(status_code=400, detail="no lake configured for cascade")
        lake = lake_root.parent
        root = lake / "sweeps"

        def _run() -> tuple[int, dict[str, object]]:
            """Replay the cascade off the event loop (threadpool worker)."""
            from brutex.engine.cascade_wire import (
                CascadeWireError,
                load_bars_for_manifest,
                load_manifest,
                run_and_write_from_manifest,
            )

            try:
                manifest = load_manifest(root)
            except CascadeWireError as exc:
                return 400, {"ok": False, "error": str(exc)}
            bars_for = load_bars_for_manifest(manifest, lake_root=lake)
            report = run_and_write_from_manifest(root, bars_for=bars_for)
            return 200, {
                "ok": True,
                "sweep_id": manifest.sweep_id,
                "pools": len(report.groups),
                "candidates": report.n_input,
                "survivors": report.n_survived,
            }

        status_code, payload = await run_in_threadpool(_run)
        return JSONResponse(payload, status_code=status_code)

    @app.post("/api/v1/sweep/run")
    async def api_sweep_run(
        symbols: str | None = Query(default=None),
        intervals: str | None = Query(default=None),
        directions: str | None = Query(default=None),
        k: str | None = Query(default=None),
        sl: str | None = Query(default=None),
        tp: str | None = Query(default=None),
        top: int | None = Query(default=None),
        mode: str | None = Query(default=None),
        exchange: str | None = Query(default=None),
        segment: str | None = Query(default=None),
        start: str | None = Query(default=None),
        end: str | None = Query(default=None),
    ) -> JSONResponse:
        """POST /api/v1/sweep/run — launch the brute-force grid sweep from the dashboard.

        The "🚀 Run Sweep" button (DEC-SWEEP-RUN-BUTTON-001): the ONE-CLICK web
        replacement for the ``brutex sweep-all …`` terminal command. Every form
        field is normalised + validated by the pure
        :func:`~brutex.dashboard.sweep_runner.resolve_sweep_request` (a bad field is
        a 400 with a clear message — never a deep background crash); the resolved
        request runs the SAME ``cmd_sweep_all`` engine the CLI uses on the
        **single-flight** background job (poll the live tick at
        ``/api/v1/sweep-progress``). Defaults mirror the CLI: NSE index symbols on
        the ``INDEX`` segment over ``2021-01-01 → today``. A second click while a
        pull OR sweep is already running is a no-op (``{"started": false}``) — one
        lake, one writer (CLAUDE.md §2 Rule 7 idempotence).

        The sweep reads the LOCAL lake (no vendor creds, no network); an empty /
        partly-pulled lake simply sweeps whatever slices exist and reports the
        honest tuple count in the progress feed (Golden Rule #10).
        """
        store = _require_store()
        lake_root = getattr(store, "path", None)
        if not isinstance(lake_root, Path):
            raise HTTPException(status_code=400, detail="no lake configured for sweep")
        lake = lake_root.parent
        try:
            request = resolve_sweep_request(
                symbols=symbols,
                intervals=intervals,
                directions=directions,
                k=k,
                sl=sl,
                tp=tp,
                top=top,
                mode=mode,
                exchange=exchange,
                segment=segment,
                start=start,
                end=end,
                today=date.today(),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        results_root = default_results_root(lake)
        # Point the live sweep-progress panel at the exact file cmd_sweep_all
        # publishes to, so GET /api/v1/sweep-progress serves THIS run with no new
        # plumbing (the cross-process file bridge — DEC-SWEEP-WIRE-GA-001).
        state.sweep_progress_path = sweep_progress_file(results_root)
        _LOG.info(
            "sweep.run.requested",
            symbols=request.symbols,
            intervals=request.intervals,
            directions=request.directions,
            k=request.k,
            exchange=request.exchange,
            segment=request.segment,
            start=request.start,
            end=request.end,
        )

        def _work(on_progress: ProgressHook) -> object:  # pragma: no cover — runs on the job thread
            """Background body: run the grid sweep; the live tick streams to the progress file."""
            return run_sweep_from_request(request, lake_root=lake, results_root=results_root)

        started = _pull_job.start(_work, total=1, detail=f"sweep {request.symbols}")
        payload = request.to_dict()
        payload["started"] = started
        return JSONResponse(payload)

    # ── Groww credentials store + live probe (DEC-DHAN-GROWW-REMIGRATE-001) ──
    # GrowwCredentialsIn is module-scope (see top of file) so FastAPI resolves
    # it as a request body, not a query param (DEC-CREDS-422-FIX-001).

    @app.post("/api/v1/groww/credentials")
    async def api_groww_credentials_set(body: GrowwCredentialsIn) -> JSONResponse:
        """POST /api/v1/groww/credentials — save the Groww TOTP credentials (mode 0600).

        Accepts the api key + TOTP secret in the request BODY (never query
        params, which leak into access logs). The body is NOT logged. The save is
        a read-merge-write (:func:`save_groww_credentials`) so a stored token-style
        section is preserved. A blank field raises :class:`CredentialsError`,
        mapped to HTTP 400 naming the field but NEVER its value. Returns only the
        masked preview — never echoes a value back.
        """
        from brutex.config.credentials import (
            CredentialsError,
            GrowwFileCredentials,
            groww_credentials_preview,
            save_groww_credentials,
        )

        try:
            save_groww_credentials(
                GrowwFileCredentials(api_key=body.api_key, totp_secret=body.totp_secret)
            )
        except CredentialsError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(groww_credentials_preview())

    @app.get("/api/v1/groww/credentials/status")
    async def api_groww_credentials_status() -> JSONResponse:
        """GET /api/v1/groww/credentials/status — presence + a masked, non-secret preview."""
        from brutex.config.credentials import groww_credentials_preview

        return JSONResponse(groww_credentials_preview())

    @app.post("/api/v1/groww/credentials/test")
    async def api_groww_credentials_test() -> JSONResponse:
        """POST /api/v1/groww/credentials/test — run the live Phase-0 Groww probe.

        Resolves the STORED Groww credentials (env-first, file-fallback), builds
        a live client via the lazily-imported (monkeypatchable)
        :func:`brutex.adapters.groww.live.build_live_client`, and runs
        :func:`brutex.adapters.groww.probe.run_probe`. Returns
        ``{"results": [...]}`` — each :class:`ProbeResult` as a dict. Missing
        credentials → 400; the optional ``.[groww]`` extra (growwapi + pyotp)
        not installed → 400 with an actionable install hint (``ImportError``,
        DEC-GROWW-CREDS-TEST-IMPORT-400-001); a :class:`GrowwAuthError` → 400 with
        the redacting ``safe_message``; any other :class:`GrowwError` → 502. No
        secret value is ever surfaced (every message goes through ``str(exc)``,
        which is the redacted form for the auth/groww errors).
        """
        from dataclasses import asdict

        from brutex.adapters.groww.auth import GrowwCredentials
        from brutex.adapters.groww.errors import GrowwAuthError, GrowwError
        from brutex.adapters.groww.live import build_live_client
        from brutex.adapters.groww.probe import run_probe
        from brutex.config.credentials import (
            CredentialsError,
            resolve_groww_file_credentials,
        )

        def _run() -> tuple[int, dict[str, object]]:
            """Resolve the stored Groww creds + run the live probe → ``(status, payload)``."""
            try:
                file_creds = resolve_groww_file_credentials()
            except CredentialsError as exc:
                return 400, {"detail": str(exc)}
            creds = GrowwCredentials(api_key=file_creds.api_key, totp_secret=file_creds.totp_secret)
            try:
                client = build_live_client(creds)
                results = run_probe(client)
            except ImportError as exc:
                # The live probe lazily imports the optional `.[groww]` extra
                # (growwapi + pyotp). When it is not installed, the raw
                # ModuleNotFoundError used to escape as a 500 the frontend
                # mis-rendered as "not valid JSON" — return a clean, actionable
                # 400 instead (DEC-GROWW-CREDS-TEST-IMPORT-400-001). `str(exc)` is
                # just "No module named '…'" — no secret value.
                return 400, {
                    "detail": (
                        "Groww live probe needs the optional dependencies — install "
                        "them with `pip install -e '.[groww]'` (growwapi + pyotp). "
                        f"({exc})"
                    )
                }
            except GrowwAuthError as exc:
                return 400, {"detail": str(exc)}
            except GrowwError as exc:
                return 502, {"detail": str(exc)}
            return 200, {"results": [asdict(r) for r in results]}

        status_code, payload = await run_in_threadpool(_run)
        return JSONResponse(payload, status_code=status_code)

    @app.get("/groww-credentials", response_class=HTMLResponse)
    async def groww_credentials_page() -> str:
        """GET /groww-credentials — the Groww Credentials page (DEC-DHAN-GROWW-REMIGRATE-001)."""
        return _render_groww_credentials_html()

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
        """GET /api/v1/db/candles/datasets — discovered candle + F&O + option datasets.

        Lists THREE first-class candle surfaces so the viewer can present each as its
        own browsable lake:

        * ``datasets`` — the SPOT candle partition sets (``<lake>/bars/`` with a
          non-``FNO`` segment: ``INDEX`` / ``CASH``). The F&O contracts are excluded
          here — they are NOT spot, the honesty fix of DEC-FNO-CANDLE-LAKE-001.
        * ``fno`` — the F&O (futures + options) candle partition sets
          (``<lake>/bars/<exch>/FNO/…``), each decoded to its contract identity
          (underlying / expiry / strike / instrument_type / option_type) via
          :func:`brutex.dashboard.db_view.list_fno_candle_datasets`. ``fno_schema``
          carries the documented F&O candle logical-table columns for API clients (the
          programmatic twin of the ``schema_meta`` registration; the operator browses the
          same schema via the ``schema_meta`` table row).
        * ``options`` — the expired-option series (``<lake>/options/…``), each with a
          display-only decoded ``label`` (DEC-DB-OPTION-DATASET-DECODE-001); the
          ``series`` KEY stays byte-unchanged so ``/api/v1/db/options?series=…`` keeps
          working.

        An F&O contract is queried through the IDENTICAL ``segment="FNO"`` candle path
        as spot (``/api/v1/db/candles``) — no new query surface.
        """
        store = _require_store()
        lake_root = getattr(store, "path", None)
        if not isinstance(lake_root, Path):
            return JSONResponse(
                {"datasets": [], "fno": [], "fno_schema": fno_candle_lake_schema(), "options": []}
            )
        lake = lake_root.parent

        def _datasets() -> dict[str, Any]:
            """Discover the datasets + decode the option labels (threadpool).

            DEC-DB-OPTION-DATASET-DECODE-001: the id→symbol map is built AND
            used here on the SAME threadpool worker as the store read so the
            store's RLock stays on one thread (DEC-DASHBOARD-FASTREAD-001, the
            DEC-WINDOW-OPTION-DECODE-001 windows-route pattern); display-only.
            The directory walks ride the same worker, off the event loop.
            """
            symbol_for_id = {
                str(i.security_id): i.symbol_name for i in store.list_index_equity_instruments()
            }
            # SPOT list is honestly spot-only: drop any FNO set (an F&O contract is not
            # spot) so the spot picker never conflates the two (DEC-FNO-CANDLE-LAKE-001);
            # the F&O sets get their own first-class `fno` surface below.
            spot = [
                d for d in list_candle_datasets(lake) if d["segment"] != FNO_CANDLE_LAKE_SEGMENT
            ]
            return {
                "datasets": spot,
                "fno": list_fno_candle_datasets(lake),
                "fno_schema": fno_candle_lake_schema(),
                "options": list_option_datasets(lake, symbol_for_id=symbol_for_id),
            }

        # DEC-DB-TAB-SWITCH-O1-001: the three O(files) dir-walks above are invariant
        # between lake writes, so memoise the discovered lists behind the shared write
        # generation — a tab switch within one generation is an O(1) cached handoff, a
        # pull bumps the generation → rediscover. The build still runs on the threadpool
        # worker (the store read + dir-walks stay off the event loop) on a cache miss.
        gen = _summary_cache.generation
        return JSONResponse(
            await run_in_threadpool(
                lambda: _db_datasets_cache.get_or_compute(
                    "db_candle_datasets", generation=gen, compute=_datasets
                )
            )
        )

    async def _fno_cascade_or_none() -> FnoCascadeIndex | None:
        """Resolve the cached F&O cascade index (DEC-DB-FNO-CASCADE-001), or ``None``.

        ONE source of truth for the three cascade routes (CLAUDE.md §11 COMMON):
        resolves the lake, then hands the ``O(datasets)`` dir-walk + grouping to a
        threadpool worker MEMOISED behind ``_summary_cache.generation`` — so a pull
        rebuilds the index but every cascade click is an O(1) cached handoff. Returns
        ``None`` when no lake path is configured (the route then ships an empty shape,
        never a 500 — mirrors the datasets endpoint).
        """
        store = _require_store()
        lake_root = getattr(store, "path", None)
        if not isinstance(lake_root, Path):
            return None
        lake = lake_root.parent
        gen = _summary_cache.generation
        return await run_in_threadpool(
            lambda: _fno_cascade_cache.get(
                generation=gen,
                build=lambda: build_fno_cascade(list_fno_candle_datasets(lake)),
            )
        )

    @app.get("/api/v1/db/fno/underlyings")
    async def api_db_fno_underlyings() -> JSONResponse:
        """GET /api/v1/db/fno/underlyings — the cascade's level-1 picker (DEC-DB-FNO-CASCADE-001).

        Every underlying that has F&O candle data (ascending) + the ``unparsed``
        partitions surfaced honestly. O(1) cached read; tiny payload (~hundreds of
        names) instead of the flat thousand-row contract dropdown.
        """
        idx = await _fno_cascade_or_none()
        if idx is None:
            return JSONResponse({"underlyings": [], "unparsed": []})
        return JSONResponse(
            {
                "underlyings": list(idx.underlyings()),
                "unparsed": [contract_to_json(c) for c in idx.unparsed],
            }
        )

    @app.get("/api/v1/db/fno/expiries")
    async def api_db_fno_expiries(underlying: str = Query(...)) -> JSONResponse:
        """GET /api/v1/db/fno/expiries?underlying=NIFTY — level-2 picker, scoped to one underlying.

        The ISO ``YYYY-MM-DD`` expiries for *underlying* (ascending). Empty list for
        an unknown underlying (never a 500). O(1) cached read.
        """
        idx = await _fno_cascade_or_none()
        if idx is None:
            return JSONResponse({"underlying": underlying, "expiries": []})
        return JSONResponse({"underlying": underlying, "expiries": list(idx.expiries(underlying))})

    @app.get("/api/v1/db/fno/contracts")
    async def api_db_fno_contracts(
        underlying: str = Query(...), expiry: str = Query(...)
    ) -> JSONResponse:
        """GET /api/v1/db/fno/contracts?underlying=NIFTY&expiry=2020-02-06 — level-3 picker.

        The contracts for one ``(underlying, expiry)`` — futures first, then ascending
        strike, CE before PE — each carrying its query ``symbol`` + display ``label`` +
        available ``intervals``. Empty list for an unknown cell (never a 500). O(1) cached.
        """
        idx = await _fno_cascade_or_none()
        if idx is None:
            return JSONResponse({"underlying": underlying, "expiry": expiry, "contracts": []})
        return JSONResponse(
            {
                "underlying": underlying,
                "expiry": expiry,
                "contracts": [contract_to_json(c) for c in idx.contracts(underlying, expiry)],
            }
        )

    async def _spot_cascade_or_none() -> SpotCascadeIndex | None:
        """Resolve the cached SPOT cascade index (DEC-DB-SPOT-CASCADE-001), or ``None``.

        The spot twin of :func:`_fno_cascade_or_none` (CLAUDE.md §11 COMMON): resolves the
        lake, then hands the ``O(datasets)`` dir-walk + grouping to a threadpool worker
        MEMOISED behind ``_summary_cache.generation`` via the shared ``MoversSnapshotCache``
        — so a pull rebuilds the index but every symbol-picker open is an O(1) cached
        handoff. The datasets are filtered to the non-F&O segments (``INDEX`` / ``CASH``),
        exactly the ``spot`` list the datasets endpoint builds (DEC-FNO-CANDLE-LAKE-001).
        Returns ``None`` when no lake path is configured (the route then ships an empty
        shape, never a 500 — mirrors the F&O cascade + datasets endpoints).
        """
        store = _require_store()
        lake_root = getattr(store, "path", None)
        if not isinstance(lake_root, Path):
            return None
        lake = lake_root.parent
        gen = _summary_cache.generation

        def _build() -> SpotCascadeIndex:
            spot = [
                d for d in list_candle_datasets(lake) if d["segment"] != FNO_CANDLE_LAKE_SEGMENT
            ]
            return build_spot_cascade(spot)

        return await run_in_threadpool(
            lambda: _spot_cascade_cache.get_or_compute(
                "db_spot_cascade", generation=gen, compute=_build
            )
        )

    @app.get("/api/v1/db/spot/symbols")
    async def api_db_spot_symbols() -> JSONResponse:
        """GET /api/v1/db/spot/symbols — the SPOT cascade picker (DEC-DB-SPOT-CASCADE-001).

        Every spot symbol that has candle data — the brute-force indices (``INDEX``
        segment) first, then the ``CASH`` reference equities alphabetically — each carrying
        its query coordinates (``exchange`` / ``segment`` / ``symbol``) + display ``label``
        + the STORED ``intervals`` (inline, so the picker needs no second fetch; only
        intervals with a real on-disk partition, never a phantom TF). O(1) cached read; a
        small symbols-with-intervals payload instead of the flat ~750-row dropdown. An
        empty list when no lake is wired (never a 500).
        """
        idx = await _spot_cascade_or_none()
        if idx is None:
            return JSONResponse({"symbols": []})
        return JSONResponse({"symbols": [symbol_to_json(s) for s in idx.symbols()]})

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
                    count_cache=_candle_count_cache,
                    generation=_summary_cache.generation,
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(payload)

    @app.get("/api/v1/db/window-ledger")
    async def api_db_window_ledger(
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> JSONResponse:
        """GET /api/v1/db/window-ledger — the unified per-window skip/retry ledger.

        DEC-WINDOW-LEDGER-001. EVERY spot + options fetch window of the live
        tiling (superseded trailing keys are pruned at plan time,
        DEC-WINDOW-TRAILING-GC-001), as a browsable table row (market /
        security_id / interval /
        window dates / status / bars / attempts / last_error / streak / in-pull
        fetches). READ-ONLY:
        loads ``<lake>/window_ledger/ledger.parquet``; never raises (empty
        ``{columns, rows, total}`` when no lake / no pull yet, never a 500). Shape
        matches the DuckDB-table grid so the /db viewer renders it identically.

        The response is now ONE ``LIMIT``/``OFFSET`` page with a real ``total``
        (DEC-DB-VIEWER-PAGINATE-ALL-001) so the /db viewer renders O(visible), not
        every window. Honest envelope (Golden Rule #10): the ledger is a SMALL
        bounded sidecar (one row per fetch window — thousands, not the millions a
        candle partition holds), so the read stays O(windows); only the page's rows
        are IST-formatted + serialized, and the page-slice preserves the canonical
        :meth:`~brutex.data.window_ledger_store.WindowLedger.to_rows` sort + derived
        columns byte-for-byte.
        """
        from brutex.data.window_ledger_store import load_window_ledger

        columns = [
            "market",
            "security_id",
            "interval",
            "window_start",
            "window_end",
            "status",
            "bars",
            "attempts",
            "last_error",
            "first_seen_at",
            "last_attempt_at",
            "empty_streak",
            "in_pull_fetches",
        ]
        store = _require_store()
        lake_root = getattr(store, "path", None)
        if not isinstance(lake_root, Path):
            return JSONResponse(
                {"columns": columns, "rows": [], "total": 0, "limit": limit, "offset": offset}
            )
        lake = lake_root.parent

        def _load() -> dict[str, Any]:
            """Load the ledger, then slice + IST-format ONLY the requested page."""
            from brutex.data.session import format_ist_clock

            all_rows = load_window_ledger(lake).to_rows()
            total = len(all_rows)
            # Slice BEFORE formatting → only the page's rows are IST-coerced (O(page)).
            # An offset past the end is a clean empty page (Python slice never raises).
            page = all_rows[offset : offset + limit]
            rows: list[list[Any]] = []
            for row in page:
                cells: list[Any] = list(row)
                # first_seen_at (idx 9) + last_attempt_at (idx 10) are stored UTC;
                # show clean IST (CLAUDE.md §6 / §11 COMMON). window_start/end stay
                # date-only (they are window boundaries, not clock times).
                cells[9] = format_ist_clock(cells[9])
                cells[10] = format_ist_clock(cells[10])
                rows.append(cells)
            return {
                "columns": columns,
                "rows": rows,
                "total": total,
                "limit": limit,
                "offset": offset,
            }

        return JSONResponse(await run_in_threadpool(_load))

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

    # ── F&O expiries+contracts REGISTRY browser (DEC-DERIV-REGISTRY-DB-VIEW-001) ─
    # The registry is a SEPARATE `registry.duckdb` cache (PR-1,
    # DEC-DERIV-METADATA-HISTORICAL-SPLIT-001) holding the actual expiry DATES +
    # contract SYMBOLS the lean master discards. These thin wrappers surface a
    # pulled chain on /db, structured by expiry / strike / CE-PE. The cache is
    # EMPTY (and the file absent) until a later PR populates it — the db_view
    # helpers degrade to an empty grid + an honest `note`, never a 500.

    @app.get("/api/v1/db/registry/expiries")
    async def api_db_registry_expiries(
        exch: str | None = Query(default=None),
        underlying: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> JSONResponse:
        """GET /api/v1/db/registry/expiries — the registry's persisted expiry dates.

        Reads the sibling ``registry.duckdb`` cache read-only. Returns the grid
        shape (``{columns, rows, total, limit, offset, counts, note}``) the /db page
        renders; the page is SQL-bounded (``LIMIT``/``OFFSET``) with a cheap FILTERED
        ``total`` (DEC-DB-VIEWER-PAGINATE-ALL-001), and ``counts`` carries the
        registry-wide expiry/contract totals for the nav badges. Thin wrapper over
        :func:`brutex.dashboard.db_view.list_registry_expiries`; never raises
        (an empty grid + ``note`` when no registry exists yet — Golden Rule #1).
        """
        store = _require_store()
        store_path = getattr(store, "path", None)
        if not isinstance(store_path, Path):
            return JSONResponse(
                {"columns": [], "rows": [], "total": 0, "counts": {}, "note": "no store wired"}
            )
        registry_path = registry_path_for(store_path)

        def _load() -> dict[str, Any]:
            """Read the expiry grid + registry counts off the event loop."""
            payload = list_registry_expiries(
                registry_path, exch=exch, underlying=underlying, limit=limit, offset=offset
            )
            payload["counts"] = registry_counts(registry_path)
            return payload

        return JSONResponse(await run_in_threadpool(_load))

    @app.get("/api/v1/db/registry/contracts")
    async def api_db_registry_contracts(
        exch: str | None = Query(default=None),
        underlying: str | None = Query(default=None),
        expiry_date: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> JSONResponse:
        """GET /api/v1/db/registry/contracts — the registry's persisted contract legs.

        The structured chain view: one row per contract (expiry / strike / CE-PE /
        per-contract historical ``candle_status``). Optional ``exch`` /
        ``underlying`` / ``expiry_date`` (ISO ``YYYY-MM-DD``) filters. The page is
        SQL-bounded (``LIMIT``/``OFFSET``) with a cheap FILTERED ``total`` so a chain
        with tens of thousands of legs loads O(page) (DEC-DB-VIEWER-PAGINATE-ALL-001).
        Reads the sibling ``registry.duckdb`` read-only; thin wrapper over
        :func:`brutex.dashboard.db_view.list_registry_contracts` — a malformed
        ``expiry_date`` raises ``ValueError`` there → HTTP 400 here; a missing
        registry degrades to an empty grid + ``note`` (never a 500).
        """
        store = _require_store()
        store_path = getattr(store, "path", None)
        if not isinstance(store_path, Path):
            return JSONResponse(
                {"columns": [], "rows": [], "total": 0, "counts": {}, "note": "no store wired"}
            )
        registry_path = registry_path_for(store_path)

        def _load() -> dict[str, Any]:
            """Read the contract grid + registry counts off the event loop."""
            payload = list_registry_contracts(
                registry_path,
                exch=exch,
                underlying=underlying,
                expiry_date=expiry_date,
                limit=limit,
                offset=offset,
            )
            payload["counts"] = registry_counts(registry_path)
            return payload

        try:
            return JSONResponse(await run_in_threadpool(_load))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

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


# The lake read/write builders moved to brutex.data.timeframe_materialize
# (DEC-TF-MATERIALIZE-002) so the manual derive route AND the pull-settle
# auto-derive share ONE pair of builders (CLAUDE.md §11 COMMON). Thin
# aliases keep this module's call sites unchanged.
_make_read_1m = make_lake_read_1m
_make_write_tf = make_lake_write_tf


def _metadata_progress_sync(
    job: BackgroundPullJob, completed: int, total: int, current: str
) -> None:
    """Map the metadata pull's absolute ``(completed, total, current)`` progress onto the
    job's ``sync_from_ledger`` resync (DEC-METADATA-LIVE-PROGRESS-001).

    ``bars=0`` / ``failed=0``: the metadata pull persists only the chain SHAPE, never
    candles (DEC-INSTRUMENT-UNIVERSE-FAST-001) — there are no bars. Lifted OUT of the
    ``# pragma: no cover`` job-thread closure so the ``(completed, total, current)`` →
    ``sync_from_ledger`` mapping is unit-tested (a swapped arg / wrong ``bars`` would now
    fail a test, not pass CI green) instead of being pragma-hidden.
    """
    job.sync_from_ledger(completed=completed, total=total, bars=0, failed=0, current=current)


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
        "Parquet sink, or run without vendor creds for the offline "
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


def _fill_default_dates(html: str) -> str:
    """Substitute the canonical ``[2020-01-01, previous day]`` into the picker tokens.

    DEC-DEFAULT-DATE-RANGE-001. The from/to date inputs carry ``value=``
    placeholder tokens (``__BRUTEX_DATE_FROM__`` / ``__BRUTEX_DATE_TO__``); this
    swaps them for the canonical default from
    :func:`~brutex.dashboard.pull_controller.default_date_range` (which itself
    reuses ``resolve_candle_range`` — one source of truth), so every page's pickers
    are PRE-FILLED with exactly what a blank pull/report fetches. Computed PER
    RENDER, so "previous day" stays current across midnight (the template cache
    holds the raw token, never a frozen date). A token-free template is returned
    unchanged.
    """
    from brutex.dashboard.pull_controller import default_date_range

    from_iso, to_iso = default_date_range()
    # The picker ceiling is TODAY (IST) — a future to-date was silently
    # clamped server-side, so the operator saw a date the pull never used
    # (DEC-UNIVERSE-SELECTION-PRECISE-001); the browser now refuses it. The
    # settled-data clamp (DEC-DAILY-CANDLE-CLAMP-001) still governs what a
    # today-inclusive range actually fetches.
    max_iso = datetime.now(tz=ZoneInfo("Asia/Kolkata")).date().isoformat()
    return (
        html.replace("__BRUTEX_DATE_FROM__", from_iso)
        .replace("__BRUTEX_DATE_TO__", to_iso)
        .replace("__BRUTEX_DATE_MAX__", max_iso)
    )


def _render_universe_html() -> str:
    """Return the F&O Universe Explorer page HTML (DEC-UNIVERSE-DASHBOARD-001).

    Pre-fills the spot/options from/to date pickers with the canonical
    ``[2020-01-01, previous day]`` default (DEC-DEFAULT-DATE-RANGE-001).
    """
    return _fill_default_dates(_load_universe_template())


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


_COMPLETENESS_TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "completeness.html"
_COMPLETENESS_TEMPLATE_CACHE: str | None = None


def _load_completeness_template() -> str:
    """Load + cache the Data Completeness Scoreboard page template.

    Ships as ``brutex/dashboard/templates/completeness.html``. The page polls
    ``/api/v1/completeness/scoreboard`` for the per-category summary + per-symbol /
    per-underlying drill-downs and links ``/api/v1/completeness/scoreboard.csv`` for
    the report download (DEC-COMPLETENESS-PAGE-001). Loaded once, then served verbatim.
    """
    global _COMPLETENESS_TEMPLATE_CACHE  # noqa: PLW0603 — single-load module cache
    if _COMPLETENESS_TEMPLATE_CACHE is None:
        _COMPLETENESS_TEMPLATE_CACHE = _COMPLETENESS_TEMPLATE_PATH.read_text(encoding="utf-8")
    return _COMPLETENESS_TEMPLATE_CACHE


def _render_completeness_html() -> str:
    """Return the Data Completeness Scoreboard page HTML (DEC-COMPLETENESS-PAGE-001)."""
    return _load_completeness_template()


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
    """Return the pre-open report page HTML (DEC-PREOPEN-REPORT-001).

    Pre-fills the From/To date pickers with the canonical ``[2020-01-01, previous
    day]`` default (DEC-DEFAULT-DATE-RANGE-001) — the SAME range the spot/options
    pickers and a blank report use.
    """
    return _fill_default_dates(_load_reports_template())


_MOVERS_TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "movers.html"
_MOVERS_TEMPLATE_CACHE: str | None = None


def _load_movers_template() -> str:
    """Load + cache the movers page template (DEC-MOVERS-SNAPSHOT-VIEW-001).

    Ships as ``brutex/dashboard/templates/movers.html``. The markets-screen-style page
    fetches ``/api/v1/movers`` on open + on every control change / manual Refresh
    (no auto-poll timer) and renders the active asset/category as a click-to-sort
    table. Loaded once, then served verbatim.
    """
    global _MOVERS_TEMPLATE_CACHE  # noqa: PLW0603 — single-load module cache
    if _MOVERS_TEMPLATE_CACHE is None:
        _MOVERS_TEMPLATE_CACHE = _MOVERS_TEMPLATE_PATH.read_text(encoding="utf-8")
    return _MOVERS_TEMPLATE_CACHE


def _render_movers_html() -> str:
    """Return the reference-only movers page HTML (DEC-MOVERS-SNAPSHOT-VIEW-001)."""
    return _load_movers_template()


def _resolve_report_dates(start: str | None, end: str | None) -> tuple[date, date]:
    """Resolve a pre-open report's inclusive ``[from, to]`` — blanks DEFAULT to the canon range.

    DEC-DEFAULT-DATE-RANGE-001. A blank ``from`` / ``to`` falls back to the SAME
    canonical ``[2020-01-01, previous day]`` window the spot/options pickers + a
    blank pull use (via :func:`~brutex.dashboard.pull_controller.default_date_range`),
    so a report is consistent with everything else and a cleared field never 400s.
    Pure + framework-free (raises :class:`ValueError`; the route maps it to a 400),
    so it is unit-tested without a TestClient.

    Raises
    ------
    ValueError
        On a malformed ISO date or an inverted (``from > to``) range.
    """
    from brutex.dashboard.pull_controller import default_date_range

    d_from, d_to = default_date_range()
    try:
        start_d = date.fromisoformat(start or d_from)
        end_d = date.fromisoformat(end or d_to)
    except ValueError as exc:
        raise ValueError(f"bad date: {exc}") from exc
    if start_d > end_d:
        raise ValueError("'from' must be on or before 'to'")
    return start_d, end_d


def _coverage_years() -> list[int]:
    """Return the expected coverage years — Track-1 vendor history floor 2020 → now.

    The operational floor is **2020-01-01** (DEC-PULL-FLOOR-2020-001, which
    supersedes the prior conservative 2021-01-01 floor of DEC-DHAN-PULL-SCOPE-001
    now that the Phase-0 granularity probe confirmed 1-minute data exists from
    2020-01-01T09:15:00). The coverage grid starts at the SAME floor the pull
    pickers default to, so a pulled 2020 cell is never invisible on /coverage.
    """
    return list(range(2020, datetime.now(UTC).year + 1))


def _metadata_year(value: str, *, field: str) -> int:
    """Parse the calendar YEAR out of one ISO date string (``YYYY-MM-DD``).

    The metadata pull's Phase-1 grid keys on YEARS (not days), so the
    ``from_date`` / ``to_date`` pickers collapse to their year here. A
    non-ISO value raises :class:`ValueError` (the route maps it to a 400),
    so a typo can never silently widen or skip a year (Golden Rule #1).
    """
    try:
        return date.fromisoformat(value).year
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO date (YYYY-MM-DD), got {value!r}") from exc


def _resolve_metadata_years(from_date: str | None, to_date: str | None) -> list[int]:
    """Resolve the inclusive Phase-1 year grid for the metadata pull.

    With neither bound supplied the grid is the full Track-1 vendor history
    floor → current year (the SAME 2020 floor :func:`_coverage_years` and the
    metadata pull's ``GROWW_METADATA_HISTORY_FLOOR`` use, so a metadata pull
    covers exactly the /coverage grid). A supplied ``from_date`` / ``to_date``
    narrows the floor / ceiling to that bound's YEAR (the metadata grid is
    year-granular). A reversed range (``from`` year after ``to`` year) raises
    :class:`ValueError` (→ 400) rather than silently yielding an empty pull.
    """
    floor = _metadata_year(from_date, field="from_date") if from_date else 2020
    ceiling = _metadata_year(to_date, field="to_date") if to_date else datetime.now(UTC).year
    if floor > ceiling:
        raise ValueError(
            f"from_date year {floor} is after to_date year {ceiling} — empty year range"
        )
    return list(range(floor, ceiling + 1))


def _parse_iso_date_arg(value: str, field: str) -> date:
    """Parse one ISO ``YYYY-MM-DD`` *value* → :class:`date` (``ValueError`` on junk).

    Shared by the historical-pull route's ``expiry`` / ``from_date`` / ``to_date``
    bounds; a non-ISO value raises so a typo is a clean 400, never a silent skip
    (Golden Rule #1).
    """
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO date (YYYY-MM-DD), got {value!r}") from exc


def _resolve_historical_contracts(
    registry: RegistryStore,
    *,
    contract: str | None,
    contracts: str | None,
    underlying: str | None,
    expiry_date: date | None,
    kind: str | None,
    all_cached: bool,
    select_kind: Callable[[str], list[Instrument]],
) -> list[ContractRow]:
    """Resolve the historical-pull SELECTION to concrete registry :class:`ContractRow` rows.

    The selection grammar, first non-empty wins (mirrors the metadata route's selection
    precedence but at CONTRACT granularity):

    * ``contract`` — one full ``contract_symbol`` (exact match against the registry).
    * ``contracts`` — a comma-separated list of full ``contract_symbol`` strings.
    * ``underlying`` (``+ expiry_date``) — one expiry's CHAIN, or (no expiry) the whole
      underlying's cached contracts, via the registry's static filters.
    * ``kind`` — every cached contract of the admitted index/stock underlyings of that
      *kind* (resolved via *select_kind*, the same classifier the bulk pull uses).
    * ``all_cached`` — every cached contract (no filter).

    Returns the matched rows (possibly EMPTY — the route turns an empty match into an
    honest 400 pointing at the metadata button; this never FABRICATES a contract, Golden
    Rule #1). A ``contract`` / ``contracts`` symbol absent from the registry simply does
    not appear in the result (an honest no-match, not an error here).
    """
    if contract is not None:
        wanted = {contract}
        return [row for row in registry.list_contracts() if row.contract_symbol in wanted]
    if contracts is not None:
        wanted = {token for token in (t.strip() for t in contracts.split(",")) if token}
        return [row for row in registry.list_contracts() if row.contract_symbol in wanted]
    if underlying is not None:
        return registry.list_contracts(underlying=underlying, expiry_date=expiry_date)
    if kind is not None:
        names = {inst.symbol_name for inst in select_kind(kind)}
        return [row for row in registry.list_contracts() if row.underlying in names]
    if all_cached:
        return registry.list_contracts()
    return []


_GROWW_CREDENTIALS_TEMPLATE_PATH = (
    Path(__file__).resolve().parent / "templates" / "groww_credentials.html"
)
_GROWW_CREDENTIALS_TEMPLATE_CACHE: str | None = None


def _load_groww_credentials_template() -> str:
    """Load + cache the Groww Credentials page template (DEC-DHAN-GROWW-REMIGRATE-001).

    Ships as a package-relative resource at
    ``brutex/dashboard/templates/groww_credentials.html``. The page POSTs the
    api key + TOTP secret to ``/api/v1/groww/credentials`` (body, never query),
    GETs ``/api/v1/groww/credentials/status`` for the masked status line, and
    POSTs ``/api/v1/groww/credentials/test`` to run the live probe — it never
    displays a secret value. Loaded once, then served verbatim.
    """
    global _GROWW_CREDENTIALS_TEMPLATE_CACHE  # noqa: PLW0603 — single-load module cache
    if _GROWW_CREDENTIALS_TEMPLATE_CACHE is None:
        _GROWW_CREDENTIALS_TEMPLATE_CACHE = _GROWW_CREDENTIALS_TEMPLATE_PATH.read_text(
            encoding="utf-8"
        )
    return _GROWW_CREDENTIALS_TEMPLATE_CACHE


def _render_groww_credentials_html() -> str:
    """Return the Groww Credentials page HTML (DEC-DHAN-GROWW-REMIGRATE-001)."""
    return _load_groww_credentials_template()


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
  /* rps_1s breach flag (DEC-DASHBOARD-RPS1S-PANEL-001): the trailing-1s issue
     gauge crossing the 5 r/s cap should be impossible — make it impossible to
     miss when it is not. */
  .rps-breach { color: #ef4444; font-weight: 700; }
  .rps-breach::after { content: " ⚠ >5 r/s BREACH"; }
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
      live: <span id="rl-rps1s">-</span> rps ·
      peak: <span id="rl-rps1s-max">-</span> rps ·
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
  // Instantaneous per-second issue gauge (DEC-DASHBOARD-RPS1S-PANEL-001): the
  // trailing-1s POST count + its run peak. rps_1s_max > 5 should be impossible
  // (every physical POST is burst-1 bucket-gated) — flag it loudly if it ever is.
  const RPS_CAP = 5;
  const rps1s = rl.rps_1s || 0;
  const rps1sMax = rl.rps_1s_max || 0;
  const rps1sEl = document.getElementById('rl-rps1s');
  const rps1sMaxEl = document.getElementById('rl-rps1s-max');
  rps1sEl.textContent = rps1s;
  rps1sMaxEl.textContent = rps1sMax;
  rps1sEl.classList.toggle('rps-breach', rps1s > RPS_CAP);
  rps1sMaxEl.classList.toggle('rps-breach', rps1sMax > RPS_CAP);

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
    "select_pull_instruments",
    "snapshot_to_json",
    "universe_summary",
]
