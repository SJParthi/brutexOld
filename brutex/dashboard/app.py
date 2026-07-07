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
import math
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import structlog

from brutex.adapters.groww.expiry_scope import normalize_expiry_scope
from brutex.adapters.groww.instruments import PARSE_SKIPS_META_KEY
from brutex.adapters.ingest_common import (
    gate_pull_credentials,
    label_synthetic,
    verdict_message,
)
from brutex.calendars.nse import now_ist
from brutex.config.credentials import groww_credentials_error
from brutex.dashboard.completeness import (
    ReconcileLogGate,
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
    REGISTRY_CONTRACT_COLUMNS,
    REGISTRY_EXPIRY_COLUMNS,
    fno_candle_lake_schema,
    list_candle_datasets,
    list_columns,
    list_fno_candle_datasets,
    list_option_series,
    list_registry_contracts,
    list_registry_expiries,
    phase_progress_payload,
    query_candles,
    query_option_candles,
    query_table,
    registry_counts,
    registry_path_for,
    sample_rows,
    spot_window_rollup,
    summarize_option_datasets,
    summarize_spot_verdict_datasets,
    table_summary,
)
from brutex.dashboard.derivatives_pull import resolve_pull_intervals
from brutex.dashboard.discovery_warm import (
    DB_DATASETS_KEY,
    FNO_CASCADE_KEY,
    LATEST_MOVERS_DATE_KEY,
    MOVERS_DATASETS_KEY,
    SPOT_CASCADE_KEY,
    build_db_datasets_payload,
    filter_spot_datasets,
    warm_discovery_memos,
)
from brutex.dashboard.economics_integrity_csv import economics_integrity_to_csv
from brutex.dashboard.fno_cascade import (
    FnoCascadeIndex,
    build_expiry_counts,
    build_fno_cascade,
    contract_to_json,
)
from brutex.dashboard.fno_underlying import (
    fno_underlying_forms,
    resolve_fno_index_aliases,
)
from brutex.dashboard.greeks_health import GreeksEnrichHealth
from brutex.dashboard.historical_pull import pull_contract_candles
from brutex.dashboard.host_allowlist import DEFAULT_ALLOWED_HOSTS
from brutex.dashboard.lake_revision import lake_revision
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
    load_registry_expiries,
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
from brutex.dashboard.movers_warm import (
    MoversWarmResult,
    futures_warm_grid,
    latest_movers_date,
    options_warm_grid,
    spot_warm_grid,
    warm_coordinate_label,
    warm_movers_snapshots,
)
from brutex.dashboard.pipeline_prebuild import (
    PrebuildOutcome,
    PrebuildTarget,
    run_pipeline_prebuild_guarded,
)
from brutex.dashboard.pipeline_pull import run_full_pipeline
from brutex.dashboard.prebuild_gate import PrebuildGate, gated_movers_payload
from brutex.dashboard.prebuild_parallel import resolve_prebuild_workers
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
    GROWW_HISTORY_FLOOR,
    BulkSpotReport,
    SessionRateController,
    default_date_range,
    pull_all_spot,
    pull_candles,
    resolve_candle_range,
    resolve_fetch_intervals,
    resolve_pull_symbols,
    resolve_pull_targets,
)
from brutex.dashboard.pull_intent import (
    PullIntent,
    PullIntentStore,
    persist_intent_for_launch,
    run_auto_resume,
)
from brutex.dashboard.pull_jobs import BackgroundPullJob, ProgressHook
from brutex.dashboard.rate_bridge_cache import (
    BRIDGE_SOURCE_UNWIRED,
    BridgeReadResult,
    RateBridgeCache,
    stamp_bridge_freshness,
)
from brutex.dashboard.rate_sampler import RatePanelSampler
from brutex.dashboard.registry_strikes import make_registry_chain_loader
from brutex.dashboard.request_logging import emit_request_log
from brutex.dashboard.spot_cascade import (
    SpotCascadeIndex,
    build_spot_cascade,
    symbol_to_json,
)
from brutex.dashboard.summary_cache import LAKE_UNIQUENESS, OPTIONS, SPOT, SummaryCache
from brutex.dashboard.sweep_results_view import (
    DEFAULT_TOP_N as SWEEP_LEADERBOARD_DEFAULT_TOP_N,
)
from brutex.dashboard.sweep_results_view import (
    MAX_TOP_N as SWEEP_LEADERBOARD_MAX_TOP_N,
)
from brutex.dashboard.sweep_results_view import (
    build_sweep_leaderboard,
)
from brutex.dashboard.sweep_runner import (
    default_results_root,
    resolve_sweep_request,
    run_sweep_from_request,
    sweep_progress_file,
)
from brutex.dashboard.universe_cache import UniverseSnapshotCache, master_revision
from brutex.dashboard.universe_pull import select_pull_instruments
from brutex.dashboard.universe_query import list_underlyings, universe_summary
from brutex.dashboard.verdict_banner import banner_for
from brutex.data.derive_lock import derive_in_progress, derive_lock_held_by_this_process
from brutex.data.economics_integrity import META_KEY as ECONOMICS_INTEGRITY_META_KEY
from brutex.data.economics_integrity import report_from_dict as economics_report_from_dict
from brutex.data.lake_footprint import make_disk_guard, record_lake_footprint
from brutex.data.lake_invariants import (
    build_lake_census,
    census_to_dict,
    scan_lake_invariants,
)
from brutex.data.mapping_integrity import (
    EligibleSpot,
    MappingIntegrityReport,
    attempted_pairs_from_records,
    coverage_years_from_records,
    reconcile_mapping_integrity,
    report_to_dict,
)
from brutex.data.master_integrity import META_KEY as MASTER_INTEGRITY_META_KEY
from brutex.data.master_integrity import report_from_dict as master_report_from_dict
from brutex.data.registry_store import ContractRow, RegistryError, RegistryStore
from brutex.data.timeframe_materialize import MaterializeResult, materialize_lake_timeframes
from brutex.doctor_autorun import LiveProofAutorun
from brutex.models import Exchange, Instrument, InstrumentType
from brutex.observability.correlation import request_id_scope
from brutex.observability.power_keepawake import keep_awake
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
    #: The controller's MEASURED rolling-60s ISSUE count (``recent_minute``) and the HARD
    #: per-minute governor cap (``ceiling_per_min``) — straight from the controller
    #: snapshot (DEC-GROWW-RATE-PERMIN-001). Feed the live-pull-rate monitor's req/min
    #: gauge + its observed req/hour extrapolation (DEC-LIVE-RATE-MONITOR-001). ``0`` /
    #: ``-1`` when no per-minute governor is wired (the dry-run / no-live default).
    recent_minute: int = 0
    ceiling_per_min: int = -1
    #: The controller's OBSERVE-ONLY trailing-window REAL issue counts measured from actual
    #: issue timestamps (``recent_1s`` / ``recent_600s`` / ``recent_3600s``;
    #: DEC-RATE-MONITOR-EXPAND-001, MIGRATION.md #1004). These feed the live-rate monitor's
    #: req/sec, req/10min + MEASURED req/hour evidence so the operator can verify each rate is
    #: a genuine windowed count, not a per-second figure multiplied up. ``recent_60s`` reuses
    #: :attr:`recent_minute` (the same trailing-60s count, two independent controller sources).
    #: ``0`` when no live pull has ticked (the honest idle default).
    recent_1s: int = 0
    recent_600s: int = 0
    recent_3600s: int = 0
    #: The monotonic timestamp of the LAST live rate tick (a pull pushed a snapshot via
    #: :func:`make_rate_observer`). ``-inf`` (the default) means "never updated" — the
    #: live-pull-rate monitor reads this to tell an ACTIVE pull from an IDLE / ended one,
    #: so an ended pull never lingers as a fake "live" reading (DEC-LIVE-RATE-MONITOR-001,
    #: Golden Rule #1). Monotonic (not wall-clock) so it is immune to clock adjustments.
    last_update_monotonic: float = float("-inf")
    #: Whether the panel's :attr:`effective_rps` is a REAL measured completed-rate from the
    #: transport :class:`~brutex.adapters.call_meter.CallMeter` (DEC-RATE-WIDGET-FNO-HONEST-001).
    #: ``True`` for a spot-phase tick (the meter is wired); ``False`` for a metadata/F&O-phase
    #: tick from the admission-only :class:`~brutex.dashboard.rate_sampler.RatePanelSampler`,
    #: whose meter is unreachable — so the live-rate view keeps "achieved" an honest 0 that phase
    #: and bases the stall verdict on the controller's admission signal. Defaults ``True`` (the
    #: pre-decision meter-fed contract; an idle panel never reports a fabricated completed-rate).
    meter_present: bool = True
    #: The transport meter's MEASURED rolling failed-calls rate (the ``CallMeter`` snapshot's
    #: ``failed_rps`` — DEC-PULL-TELEMETRY-TRIAD-001): failed attempts per second over the
    #: meter's trailing window. Feeds the ``/api/v1/pull/rate`` view's distinct "failing"
    #: verdict for an issuing-but-all-failing pull (the #1022 flagged follow-up). ``0.0``
    #: when idle / no meter / no failures (the byte-identical default).
    failed_rps: float = 0.0

    def to_json_dict(self) -> dict[str, Any]:
        """Return a JSON-safe panel mapping, DROPPING the monotonic freshness baseline.

        ``last_update_monotonic`` is an internal bookkeeping value (a ``time.monotonic``
        reading, ``-inf`` until the first tick) that is NOT JSON-serialisable (``-inf``)
        and not wall-clock-meaningful to a client — the derived staleness is surfaced by
        the dedicated ``/api/v1/pull/rate`` poll instead. ONE source of truth for the
        serialised panel shape, shared by ``/api/v1/state`` (:meth:`to_dict`) and
        ``/api/v1/rate`` so the two payloads never drift (CLAUDE.md §11 COMMON;
        DEC-LIVE-RATE-MONITOR-001).
        """
        panel = asdict(self)
        panel.pop("last_update_monotonic", None)
        return panel


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
        recent_minute: int = 0,
        ceiling_per_min: int = -1,
        recent_1s: int = 0,
        recent_600s: int = 0,
        recent_3600s: int = 0,
    ) -> None:
        """Update the rate-limit panel from a fresh controller snapshot.

        The daily-quota gauge fields the panel once carried are GONE
        (DEC-QUOTA-VERDICT-MOOT-001): the live vendor (Groww) has no daily quota
        (``GROWW_DAILY_QUOTA_UNVERIFIED=None``, CLAUDE.md §2 Rule 6) and no
        production controller wires a ``DailyQuotaTracker``, so the snapshot never
        carried ``quota_*`` keys — the fields were a constant ``(0, 0, -1)``.

        ``recent_minute`` / ``ceiling_per_min`` are the controller's rolling-60s issue
        count + the HARD per-minute governor cap (DEC-GROWW-RATE-PERMIN-001), feeding the
        live-pull-rate monitor's req/min gauge (DEC-LIVE-RATE-MONITOR-001). A live tick
        ALSO stamps :attr:`RateLimitPanel.last_update_monotonic` (the staleness baseline
        the monitor reads to tell an active pull from an idle one) via
        :meth:`_stamp_rate_update`.

        ``recent_1s`` / ``recent_600s`` / ``recent_3600s`` are the controller's OBSERVE-ONLY
        trailing-window REAL issue counts (DEC-RATE-MONITOR-EXPAND-001) feeding the monitor's
        req/sec, req/10min + MEASURED req/hour evidence; they default to ``0`` so a snapshot
        from an older controller (or a non-live caller) leaves them honestly zeroed.
        """
        with self._lock:
            self.rate_limit.current_rps = current_rps
            self.rate_limit.available_tokens = available_tokens
            self.rate_limit.recent_429_count = recent_429_count
            self.rate_limit.recent_minute = recent_minute
            self.rate_limit.ceiling_per_min = ceiling_per_min
            self.rate_limit.recent_1s = recent_1s
            self.rate_limit.recent_600s = recent_600s
            self.rate_limit.recent_3600s = recent_3600s
            self._stamp_rate_update()

    def _stamp_rate_update(self) -> None:
        """Stamp the panel's live-tick monotonic timestamp (caller holds ``self._lock``).

        The freshness baseline the live-pull-rate monitor reads (DEC-LIVE-RATE-MONITOR-001):
        a pull pushing a snapshot moves it to ``monotonic()`` so a poll can compute the age
        and flip to idle once a pull ends. Wrapped in a method (rather than inlined at the
        two writer sites) so the SINGLE ``time.monotonic`` call site is patchable in tests.
        """
        self.rate_limit.last_update_monotonic = time.monotonic()

    def update_throughput(
        self,
        *,
        effective_rps: float,
        latency_ms_p95: float,
        rps_1s: int = 0,
        rps_1s_max: int = 0,
        meter_present: bool = True,
        failed_rps: float = 0.0,
    ) -> None:
        """Update the MEASURED throughput half of the rate panel from a ``CallMeter`` snapshot.

        ``effective_rps`` is the transport's inter-arrival rate (calls ÷ wall-span)
        and ``latency_ms_p95`` its 95th-percentile round-trip; ``rps_1s`` /
        ``rps_1s_max`` are the instantaneous trailing-1s issue gauge + its run peak —
        all straight from the vendor client's call-meter snapshot
        (DEC-DASHBOARD-RATE-PANEL-001 + DEC-DASHBOARD-RPS1S-PANEL-001). ``rps_1s_max``
        is the real-time proof the 5 r/s cap held (``<= 5`` ⇒ no breach, ever).

        ``meter_present`` (DEC-RATE-WIDGET-FNO-HONEST-001) records whether ``effective_rps``
        is a REAL measured completed-rate (``True`` — a spot-phase tick whose
        :class:`~brutex.adapters.call_meter.CallMeter` is wired) or honestly absent (``False``
        — a metadata/F&O-phase tick from the admission-only sampler, which pushes no measured
        completed-rate). The ``/api/v1/pull/rate`` view reads it to keep "achieved" an honest 0
        + use the controller's admission signal for the stall verdict during the meter-less
        phases, instead of fabricating a completed-rate.

        ``failed_rps`` (DEC-PULL-TELEMETRY-TRIAD-001) is the meter snapshot's MEASURED
        rolling failed-calls rate — the evidence the ``/api/v1/pull/rate`` view needs to
        read an issuing-but-all-failing pull as the distinct "failing" verdict (the #1022
        flagged follow-up). Default ``0.0`` (an older meter snapshot / the admission-only
        sampler) keeps the panel byte-identical.
        """
        with self._lock:
            self.rate_limit.effective_rps = effective_rps
            self.rate_limit.latency_ms_p95 = latency_ms_p95
            self.rate_limit.rps_1s = rps_1s
            self.rate_limit.rps_1s_max = rps_1s_max
            self.rate_limit.meter_present = meter_present
            self.rate_limit.failed_rps = failed_rps
            # The measured-throughput half of a live tick is just as much pull activity as
            # the controller half — stamp the freshness baseline here too so the monitor
            # reads "active" even on a tick where only the meter moved (DEC-LIVE-RATE-MONITOR-001).
            self._stamp_rate_update()

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
                "rate_limit": self.rate_limit.to_json_dict(),
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
        # The controller snapshot's conditional ``quota_*`` keys (a wired
        # DailyQuotaTracker only) are deliberately NOT mapped: no production
        # controller wires one (DEC-QUOTA-VERDICT-MOOT-001 — Groww has no daily
        # quota) and the panel's gauge fields are deleted with the /universe gauge.
        # ``ceiling_per_min`` is ``None`` in the snapshot when no governor is wired
        # (DEC-GROWW-RATE-PERMIN-001) — map that to the ``-1`` "no cap" panel sentinel so
        # the live-rate monitor shows no per-minute limit rather than a false 0.
        per_min_cap = rate.get("ceiling_per_min")
        state.update_rate_limit(
            current_rps=float(rate.get("current_rps", 0.0)),
            available_tokens=float(rate.get("available_tokens", 0.0)),
            recent_429_count=int(rate.get("recent_hits", 0)),
            recent_minute=int(rate.get("recent_minute", 0)),
            ceiling_per_min=int(per_min_cap) if per_min_cap is not None else -1,
            # OBSERVE-ONLY trailing-window real issue counts (DEC-RATE-MONITOR-EXPAND-001):
            # absent on an older controller snapshot → default 0 (honest idle), never a
            # fabricated number.
            recent_1s=int(rate.get("recent_1s", 0)),
            recent_600s=int(rate.get("recent_600s", 0)),
            recent_3600s=int(rate.get("recent_3600s", 0)),
        )
        state.update_throughput(
            effective_rps=float(meter.get("effective_rps", 0.0)),
            latency_ms_p95=float(meter.get("latency_ms_p95", 0.0)),
            rps_1s=int(meter.get("rps_1s", 0)),
            rps_1s_max=int(meter.get("rps_1s_max", 0)),
            # DEC-RATE-WIDGET-FNO-HONEST-001: the admission-only sampler (metadata + F&O phases)
            # pushes ``meter_present=False`` (no measured completed-rate this phase); a REAL spot
            # CallMeter snapshot carries no such key → defaults ``True`` (the meter-fed contract).
            meter_present=bool(meter.get("meter_present", True)),
            # The meter's MEASURED rolling failed-calls rate (DEC-PULL-TELEMETRY-TRIAD-001);
            # absent on an older snapshot / the admission-only sampler sentinel → an honest 0.0.
            failed_rps=float(meter.get("failed_rps", 0.0)),
        )

    return _observe


#: The honest note the ``/api/v1/db/registry/{expiries,contracts}`` routes return when
#: the ``registry.duckdb`` file EXISTS but cannot be opened (corrupt / truncated /
#: version-incompatible — ``RegistryStore.open`` raises ``RegistryError``, a
#: ``BruteXError`` NOT a ``ValueError``). Distinct from the db_view missing-file note so
#: the operator can tell "never pulled" apart from "pulled but the cache is damaged";
#: the cure is to re-pull, which rewrites the cache atomically. The corrupt-registry
#: guard lives HERE in the route handlers (not in ``db_view.py``) so ``db_view.py`` is
#: covered by its own curated multi-file gate while the new route branches register in
#: ``app.py``'s whole-package 100% coverage floor (DEC-DASHBOARD-EMPTY-STATE-001).
_REGISTRY_UNREADABLE_NOTE: Final = "registry unreadable — re-pull expiries + contracts"


def _registry_unreadable_payload(columns: tuple[str, ...]) -> dict[str, Any]:
    """Build the honest empty-grid payload for an unreadable ``registry.duckdb``.

    Mirrors the grid shape the db_view registry readers return for a MISSING file
    (``{columns, rows, total, limit, offset, note, counts}``) but with empty ``rows``,
    a zero ``total``, the DISTINCT :data:`_REGISTRY_UNREADABLE_NOTE`, and a ``counts``
    block that likewise degrades honestly (``exists=True`` — the file IS present, just
    damaged). Shared by both registry routes so the two corrupt responses stay
    byte-consistent (Golden Rule #11 COMMON).

    Parameters
    ----------
    columns
        The browsable column set for the grid — ``REGISTRY_EXPIRY_COLUMNS`` for the
        expiries route, ``REGISTRY_CONTRACT_COLUMNS`` for the contracts route.

    Returns
    -------
    dict[str, Any]
        The HTTP 200 empty-state payload.
    """
    return {
        "columns": list(columns),
        "rows": [],
        "total": 0,
        "note": _REGISTRY_UNREADABLE_NOTE,
        "counts": {
            "expiries": 0,
            "contracts": 0,
            "exists": True,
            "note": _REGISTRY_UNREADABLE_NOTE,
        },
    }


# ── Host / cross-origin hardening (R2-DS-F1) ────────────────────────────────
# The dashboard carries no auth/CSRF tokens, so a browser on the operator's
# machine can be induced to fire state-changing POSTs at this localhost
# service (CSRF fire-and-forget) or reach it under an attacker-controlled
# Host via DNS rebinding. Two thin guards close that: a Host-header allowlist
# (Starlette ``TrustedHostMiddleware``) + a same-origin check on
# state-changing methods. The pure decision logic lives in the module-level
# helpers below so it is directly unit-testable at 100% line+branch; the
# async middleware registered in :func:`build_app` is the thin glue.

# The loopback-only DEFAULT_ALLOWED_HOSTS constant lives in the LEAF module
# :mod:`brutex.dashboard.host_allowlist` (re-exported here via ``__all__``) so
# the CLI launcher can read it without importing this module — see that
# module's docstring for the cyclic-import rationale.

#: State-changing methods the cross-origin guard enforces on. GET / HEAD /
#: OPTIONS are never checked — they must stay side-effect-free reads.
_ORIGIN_GUARDED_METHODS: Final[frozenset[str]] = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: First 5xx status code — the intent-resume launch binding splits an
#: ``HTTPException`` on this floor: a 4xx is a DETERMINISTIC validation refusal
#: (→ ``ValueError`` → the resume core clears the intent), a 5xx is transient
#: (→ ``RuntimeError`` → the intent is kept and re-tried, cap-bounded) —
#: DEC-PULL-INTENT-RESUME-001.
_SERVER_ERROR_FLOOR: Final[int] = 500

#: 403 detail returned for a browser-initiated cross-site state change.
_CROSS_ORIGIN_DETAIL: Final = "cross-origin request blocked"


def _trusted_host_patterns(allowed_hosts: Sequence[str] | None) -> list[str]:
    """Normalize an allowed-host list into Starlette ``TrustedHostMiddleware`` patterns.

    Starlette compares the request's ``Host`` header with any ``:port`` suffix
    STRIPPED, and asserts that a pattern contains no ``*`` beyond a leading
    ``*.`` domain wildcard — so the operator-facing ``host:*`` (any-port)
    spelling is normalized to its bare host here, deduplicated
    order-preserving, and empty entries are dropped. The bracketed IPv6
    spelling rides the SAME rule (``"[::1]:*"`` → ``"[::1]"`` — the suffix
    strip is bracket-agnostic); the request-side bracket-aware port strip
    lives in :class:`brutex.dashboard.trusted_host.BracketAwareTrustedHostMiddleware`
    (DEC-ENGINE-MISC-PACK-002).

    Parameters
    ----------
    allowed_hosts
        Operator-supplied allowlist, or ``None`` for the loopback-only
        default (:data:`DEFAULT_ALLOWED_HOSTS`).

    Returns
    -------
    list[str]
        Deduplicated bare-host patterns safe to hand to Starlette.
    """
    entries = DEFAULT_ALLOWED_HOSTS if allowed_hosts is None else tuple(allowed_hosts)
    patterns: list[str] = []
    for entry in entries:
        host = entry[:-2] if entry.endswith(":*") else entry
        if host and host not in patterns:
            patterns.append(host)
    return patterns


def _origin_header_host(value: str) -> str | None:
    """Extract the lowercased ``host[:port]`` from an ``Origin``/``Referer`` value.

    Parameters
    ----------
    value
        The raw header value, e.g. ``"http://localhost:8000"`` or
        ``"http://localhost:8000/universe"``.

    Returns
    -------
    str | None
        The lowercased network location, or ``None`` when none can be parsed
        (the opaque ``Origin: null`` a sandboxed cross-site frame sends, a
        malformed URL raising ``ValueError``, or a bare path) — the caller
        treats an unparsable PRESENT header as cross-origin, never as a pass.
    """
    try:
        netloc = urlsplit(value).netloc
    except ValueError:
        return None
    return netloc.lower() or None


def _cross_origin_reason(
    method: str,
    request_host: str | None,
    origin: str | None,
    referer: str | None,
) -> str | None:
    """Decide whether a request is a browser cross-origin state change.

    Rules (R2-DS-F1): only methods in :data:`_ORIGIN_GUARDED_METHODS` are
    checked; a request with NEITHER ``Origin`` NOR ``Referer`` is always
    allowed (CLI / curl / non-browser clients send neither); when an
    ``Origin`` is present it is authoritative (``Referer`` is consulted only
    when ``Origin`` is absent); the header's ``host[:port]`` must equal the
    request's own ``Host`` header (case-insensitive) or the request is
    refused — an unparsable header or a missing ``Host`` refuses too (fail
    closed, never a pass on a malformed value).

    Parameters
    ----------
    method
        The HTTP request method.
    request_host
        The request's own ``Host`` header, or ``None`` when absent.
    origin
        The ``Origin`` header, or ``None`` when absent.
    referer
        The ``Referer`` header, or ``None`` when absent.

    Returns
    -------
    str | None
        ``None`` to allow the request, else the 403 detail string.
    """
    if method.upper() not in _ORIGIN_GUARDED_METHODS:
        return None
    source = origin if origin is not None else referer
    if source is None:
        return None
    source_host = _origin_header_host(source)
    own_host = (request_host or "").strip().lower()
    if source_host is not None and own_host and source_host == own_host:
        return None
    return _CROSS_ORIGIN_DETAIL


def build_app(
    state: DashboardState,
    *,
    instrument_store: Any = None,
    summary_cache: SummaryCache | None = None,
    movers_cache: MoversSnapshotCache | None = None,
    universe_cache: UniverseSnapshotCache | None = None,
    rate_bridge_cache: RateBridgeCache | None = None,
    prebuild_gate: PrebuildGate | None = None,
    pull_active_probe: Callable[[], bool | None] | None = None,
    pull_phase_probe: Callable[[], str | None] | None = None,
    liveproof: LiveProofAutorun | None = None,
    allowed_hosts: Sequence[str] | None = None,
    pull_intent_store: PullIntentStore | None = None,
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
    rate_bridge_cache
        Optional :class:`~brutex.dashboard.rate_bridge_cache.RateBridgeCache`
        backing the ``/api/v1/pull/rate`` CLI-bridge file read
        (DEC-PULL-RATE-BRIDGE-NONBLOCK-CACHE-001): a repeat poll within its
        TTL is a zero-syscall O(1) memo hit, a post-TTL refresh is
        deadline-guarded on a worker daemon, and a wedged lake volume serves
        the last-known snapshot honestly labelled ``unverified`` — the event
        loop never touches the lake volume on this route. Injected so tests
        drive the slow-read / cache-hit / provenance behaviour
        deterministically; ``None`` (the default) builds a fresh production
        cache.
    prebuild_gate
        Optional :class:`~brutex.dashboard.prebuild_gate.PrebuildGate` gating
        the ``/api/v1/movers`` screens while the one-click pull pipeline runs
        (DEC-PIPELINE-AUTOPREBUILD-001 chunk 2): the ``/pull-pipeline``
        background job ``begin()``s it at start and ``release(ok)``s it in a
        ``try/finally`` when the prebuild finishes or the pipeline stops early
        (a failed pull can never leave movers blocked). ONLY the full pipeline
        route gates; a ``dry_run`` pipeline does not. Injected so tests drive
        the gated/released transitions deterministically; ``None`` (the
        default) builds a fresh production gate.
    pull_active_probe
        Optional pull-run signal for the ``/api/v1/pull/rate`` stall gate
        (DEC-RATE-STALL-PULL-GATE-001). A zero-arg callable returning ``True``
        (a pull IS running), ``False`` (no pull running — a fresh-ticked panel
        then reads ``done``, never a red ``stalled`` right after a successful
        completion), or ``None`` (no signal — tick-freshness fallback). ``None``
        (the default) wires the production probe — this app's own
        ``BackgroundPullJob.running``. Injected so tests drive the mid-pull /
        just-finished transitions deterministically.
    pull_phase_probe
        Optional pipeline-phase signal (DEC-PIPELINE-PHASE-UI-HONESTY-001). A
        zero-arg callable returning the pipeline's CURRENT coarse ``set_phase``
        label (``spot`` / ``metadata`` / ``historical`` / ``movers`` /
        ``prebuild``; ``""`` = none in flight) — read by BOTH honest-progress
        surfaces: the gated ``/api/v1/movers`` payload names the phase in its
        note (never a blanket "prebuild in progress" during the fetch phases),
        and the ``/api/v1/pull/rate`` verdict reads the amber ``local`` state
        for the zero-vendor-call LOCAL phases (``prebuild``/``movers``) instead
        of a red "stalled". ``None`` (the default) wires the production probe —
        this app's own ``BackgroundPullJob.snapshot().phase``. Injected so
        tests drive the per-phase projections deterministically.
    liveproof
        Optional :class:`~brutex.doctor_autorun.LiveProofAutorun` — the
        zero-command auto-run of the Mac live-proof kit
        (DEC-DOCTOR-LIVE-AUTORUN-001). When supplied, ``GET
        /api/v1/liveproof`` serves its status (the persistent banner's data
        source), a REAL pull admitted by the loud-refusal gate triggers the
        one-time expensive probes (``on_real_pull``), and a credentials
        refusal mirrors its fix text onto the banner
        (``note_credentials_refusal`` — the banner and the 503 always
        agree). When ``None`` (the default — unit tests / bare apps) the
        route reports ``present: false`` and the pull gates behave exactly
        as before; ``build_app`` itself never STARTS a preflight (the
        launcher seam in ``cli.cmd_dashboard`` does), so constructing an app
        stays side-effect-free.
    allowed_hosts
        Optional ``Host``-header allowlist for the DNS-rebinding guard
        (R2-DS-F1). Entries may carry an any-port ``:*`` suffix (normalized
        via :func:`_trusted_host_patterns` — Starlette strips the port before
        matching). ``None`` (the default) allows ONLY the loopback names
        (``127.0.0.1`` / ``localhost``, any port) — ``testserver`` is NOT in
        the production default (tests re-point ``TestClient`` at
        ``http://localhost`` via the root conftest fixture, or inject this
        parameter). The CLI launcher passes the actual bound host when
        serving beyond loopback (``cli._dashboard_allowed_hosts``).
    pull_intent_store
        Optional :class:`~brutex.dashboard.pull_intent.PullIntentStore` — the
        durable ``.pull_intent.json`` crash-recovery sidecar
        (DEC-PULL-INTENT-RESUME-001). When supplied, a REAL ``/pull-pipeline``
        launch persists its re-fire intent (a ``dry_run`` never does), the
        background job clears it on worker exit (``on_finally`` — success AND
        failure, before the single-flight release), the app exposes
        ``app.state.brutex_resume_pipeline`` (the boot-time seam
        ``cli.cmd_dashboard`` wires into the extended auto-resume launcher),
        and ``GET /api/v1/liveproof`` carries the resume red-banner payload.
        ``None`` (the default — unit tests / bare apps) disables all of it;
        ``build_app`` itself never fires a resume (the launcher seam does), so
        constructing an app stays side-effect-free.

    Returns
    -------
    fastapi.FastAPI
        Already-wired ASGI app. Caller mounts it on their uvicorn
        server of choice.
    """
    from brutex.config.dotenv import bootstrap_env

    # GAP C6 — load a persisted ``.env`` ONCE at app construction (no-op when
    # absent; never overrides an already-set env var, secret values never
    # logged) so a dashboard-launched pull inherits the operator's config.
    bootstrap_env()

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

        from brutex.dashboard.trusted_host import BracketAwareTrustedHostMiddleware
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

    # ── Host / cross-origin hardening (R2-DS-F1) ─────────────────────────────
    # Registration ORDER matters: Starlette wraps middlewares so the LAST one
    # added is OUTERMOST. The origin guard is added FIRST (innermost of the
    # three), then TrustedHost, then the request logger below — so a request
    # flows logger → TrustedHost (400 on an unknown Host: DNS-rebinding guard)
    # → origin guard (403 on a browser cross-site state change: CSRF guard)
    # → routes, and every refusal is still request-logged. The guard's pure
    # decision logic lives in the module-level `_cross_origin_reason` (100%
    # line+branch unit-tested); this async wrapper is the thin glue.
    @app.middleware("http")
    async def _origin_guard(request: Any, call_next: Any) -> Any:
        """Refuse browser cross-origin state-changing requests with a 403.

        Enforced only for POST/PUT/PATCH/DELETE; a request with neither
        ``Origin`` nor ``Referer`` (CLI / curl) always passes. See
        :func:`_cross_origin_reason` for the full rule set.
        """
        reason = _cross_origin_reason(
            request.method,
            request.headers.get("host"),
            request.headers.get("origin"),
            request.headers.get("referer"),
        )
        if reason is not None:
            return JSONResponse({"detail": reason}, status_code=403)
        return await call_next(request)

    # Bracket-aware Host guard (DEC-ENGINE-MISC-PACK-002): a pure-ASGI SUBCLASS of
    # Starlette's TrustedHostMiddleware — NEVER an @app.middleware("http") shim,
    # which would drop Host-guard coverage on the /ws websocket route — whose only
    # delta is admitting an allowlisted BRACKETED IPv6 Host ([::1]) that the
    # parent's naive split(":")[0] port-strip would mangle to "[".
    app.add_middleware(
        BracketAwareTrustedHostMiddleware, allowed_hosts=_trusted_host_patterns(allowed_hosts)
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
    # DEC-HARDENING-LEFTOVERS-001: the production cache carries the cross-process
    # lake-revision probe (an O(1) stat of the window-ledger Parquet, which every
    # production pull — dashboard AND CLI — checkpoints), so a CLI `brutex fetch`
    # into the same lake bumps the generation on the next read instead of leaving
    # every generation-keyed cache stale until restart. An INJECTED cache (tests /
    # legacy callers) is used as-is — byte-identical to the pre-probe behaviour.
    _summary_cache = (
        summary_cache
        if summary_cache is not None
        else SummaryCache(revision_probe=_lake_revision_probe_or_none(instrument_store))
    )
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
    # The /api/v1/pull/rate CLI-bridge read memo (DEC-PULL-RATE-BRIDGE-NONBLOCK-CACHE-001):
    # the bridge file lives on the lake volume (an external SSD), so its per-poll
    # synchronous read used to run ON the event loop and a slow/stalled disk froze
    # the whole dashboard. The cache serves a repeat poll within its 2 s TTL as a
    # zero-syscall O(1) memo hit, bounds every post-TTL refresh (stat + read) under
    # the COMMON `run_with_deadline` freeze guard on a daemon, and never blocks a
    # follower while a refresh is in flight — the served provenance is stamped onto
    # the payload (`bridge_source` / `bridge_probe_age_s`), never a stale illusion.
    # Owned by this app instance (no module-global mutable state — CLAUDE.md §4).
    _rate_bridge_cache = rate_bridge_cache if rate_bridge_cache is not None else RateBridgeCache()

    # The movers PREBUILD GATE (DEC-PIPELINE-AUTOPREBUILD-001 chunk 2): while the
    # one-click /pull-pipeline runs, /api/v1/movers returns an honest `gated: true`
    # payload with ZERO lake reads (a half-filled lake / mid-overwrite derived
    # partitions must never be ranked); the pipeline job releases it in a
    # try/finally so a failed/stopped pull can never wedge the movers shut. Owned
    # by this app instance (no module-global mutable state — CLAUDE.md §4).
    def _cross_derive_probe() -> bool:
        """``True`` iff a derive holds this lake's cross-process derive lock — O(1).

        The gate's cross-process visibility probe
        (DEC-DERIVE-LOCK-VISIBILITY-001, closing the named PrebuildGate
        follow-up of DEC-ENGINE-MISC-PACK-001): resolves the lake root lazily
        from the injected instrument store (its DuckDB file lives at
        ``<lake_root>/instruments.duckdb`` — the same ``store.path.parent``
        the movers route reads from) and takes the NON-BLOCKING shared-flock
        probe of the fixed ``<lake_root>/.locks/derive.lock``
        (:func:`~brutex.data.derive_lock.derive_in_progress` — never waits,
        never writes). No store / no real ``Path`` → ``False`` (a bare app has
        nothing to probe). Lake-volume I/O: the gate only consults it off the
        event loop (the movers route snapshots via ``run_in_threadpool``), and
        a probe ``OSError`` fails OPEN inside the gate with one WARN per
        outage.
        """
        path = getattr(instrument_store, "path", None)
        if not isinstance(path, Path):
            return False
        return derive_in_progress(path.parent)

    def _local_derive_probe() -> bool:
        """``True`` iff THIS process holds the lake's derive lock — pure, zero I/O.

        The gate's same-process attribution probe (the round-1 review fold on
        DEC-DERIVE-LOCK-VISIBILITY-001): ``fcntl.flock`` contends across open
        file descriptions, so ``_cross_derive_probe`` alone reads ``True`` for
        this process's OWN background derive too (the lake-heal L2 post-pull /
        L3 startup recall, which derives while the gate is idle). The gate
        consults THIS registry read FIRST
        (:func:`~brutex.data.derive_lock.derive_lock_held_by_this_process` —
        O(1), no filesystem I/O) and labels such a holder
        ``source="local_derive"`` instead of fabricating an "another process"
        claim (Golden Rule #1). No store / no real ``Path`` → ``False``.
        """
        path = getattr(instrument_store, "path", None)
        if not isinstance(path, Path):
            return False
        return derive_lock_held_by_this_process(path.parent)

    _prebuild_gate = (
        prebuild_gate
        if prebuild_gate is not None
        else PrebuildGate(cross_probe=_cross_derive_probe, local_derive_probe=_local_derive_probe)
    )

    def _pull_live() -> bool:
        """``True`` iff a dashboard pull is running IN THIS PROCESS — O(1), never raises.

        The ONE pull-active signal every duty-degrade consumer shares
        (DEC-DB-DISCOVERY-ADAPTIVE-FLOOR-001 / DEC-COMPLETENESS-PULL-DUTY-DEGRADE-001
        / the movers ``prefer_resample`` switch of
        DEC-MOVERS-GATE-PREBUILD-SCOPE-001 — CLAUDE.md §11 COMMON). It reads
        ``_pull_active_probe`` LATE-BOUND: that name is assigned below, after the
        single-flight ``BackgroundPullJob`` exists (the circular construction
        order — the caches taking this probe are built first), and Python
        closures resolve names at CALL time, which is always a request-time
        moment long after ``build_app`` returned. A raising probe is swallowed
        to ``False`` — the probe must never break a read. A CLI pull in a
        SEPARATE process has no in-process job signal, so this reads ``False``
        there (the honest envelope every consumer states).
        """
        try:
            return _pull_active_probe() is True
        except Exception:  # pylint: disable=broad-except — probe must never break a read
            return False

    # LATE-BIND the pull probe onto the summary cache (the setter exists because
    # the cache may be INJECTED and is constructed before the pull job —
    # DEC-COMPLETENESS-PULL-DUTY-DEGRADE-001): while a pull runs, a dirty
    # detailed-summary read widens its floor to min(9 × the last scan's own
    # duration, 600 s) and the served staleness is stamped into the payloads
    # via `stamp_freshness` (duty_degraded / snapshot_age_s — visible, never
    # silent).
    _summary_cache.set_pull_active_probe(_pull_live)
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
    # The FOUR discovery-walk memos below additionally opt into the PULL-TIME
    # adaptive floor (DEC-DB-DISCOVERY-ADAPTIVE-FLOOR-001, the ported #920
    # pattern): during a pull the write generation bumps on EVERY window tick,
    # so each poll used to re-pay a full O(datasets) walk back-to-back — and
    # post-prebuild (dataset count × the 19 materialised TFs) that walk is
    # multi-minute. While `_pull_live()` reads True, a generation-mismatched
    # entry younger than min(max(2 s, its own walk duration), 600 s) is served
    # STALE (logged `movers_cache.stale_served`, never silent); the moment the
    # pull ends the probe reads False → fresh recompute, and the post-pipeline
    # `warm_discovery_memos` force-refreshes them all off ONE shared walk so
    # the stale-floor state can never outlive the pull.
    #
    # The /db F&O cascade cache (DEC-DB-FNO-CASCADE-001): the underlying→expiry→contract
    # index is an O(datasets) build over a lake dir-walk; memoised here behind the SAME
    # write-generation as the caches above so a pull rebuilds it but every cascade click
    # (underlyings / expiries / contracts) is an O(1) handoff — tiny lazy payloads, never
    # the flat thousand-row dropdown. Now the COMMON `MoversSnapshotCache` (keyed
    # FNO_CASCADE_KEY) so it shares the pull-time floor + the discovery warm; the
    # bespoke single-slot `FnoCascadeCache` class is superseded at this wiring site.
    _fno_cascade_cache = MoversSnapshotCache(pull_active_probe=_pull_live, adaptive_floor=True)
    # The /db candle-datasets discovery cache (DEC-DB-TAB-SWITCH-O1-001): the
    # `/api/v1/db/candles/datasets` endpoint walks the WHOLE `bars/` tree THREE times
    # (spot + F&O + options, each an O(files) `rglob("*.parquet")`) — and it fires on
    # EVERY `/db` candle tab switch (spot↔F&O↔option). The three lists are invariant
    # between lake writes, so they are memoised behind the SAME generation signal as the
    # caches above (a pull bumps it → rediscover; otherwise a tab switch is an O(1) handoff
    # of the cached lists). REUSES the COMMON `MoversSnapshotCache` (one dataset key).
    _db_datasets_cache = MoversSnapshotCache(pull_active_probe=_pull_live, adaptive_floor=True)
    # The /db SPOT cascade cache (DEC-DB-SPOT-CASCADE-001): the symbol→interval index is
    # an O(datasets) build over the non-F&O lake partitions; memoised behind the SAME
    # write-generation as the caches above (the spot twin of `_fno_cascade_cache`), so a
    # pull rebuilds it but every symbol-picker open is an O(1) handoff — the small
    # symbols-with-intervals payload, never the flat ~750-row spot dropdown.
    _spot_cascade_cache = MoversSnapshotCache(pull_active_probe=_pull_live, adaptive_floor=True)
    # The DEFAULT-poll latest-movers-date cache (GAP #4): the `/api/v1/movers` route with
    # NO explicit date resolves the latest available spot 1day via `latest_movers_date`,
    # an O(datasets) DuckDB MAX over the spot 1day globs — re-run on EVERY default poll.
    # It is invariant between lake writes, so it is memoised behind the SAME write
    # generation as the caches above (a pull bumps it → re-resolve; otherwise a default
    # poll is an O(1) handoff). REUSES the COMMON `MoversSnapshotCache` (one date key).
    # Pull-time staleness note: the derived 1day partitions are rewritten ONLY by the
    # pipeline's final prebuild, so the latest date genuinely cannot move mid-fetch.
    _latest_movers_date_cache = MoversSnapshotCache(
        pull_active_probe=_pull_live, adaptive_floor=True
    )
    # The Options-tab AUTHORITATIVE expiry-calendar cache (DEC-MOVERS-CALENDAR-TIERS-001):
    # the Near/Next/Far + Weekly/Monthly tier classification reads the FULL vendor expiry
    # calendar from `registry.duckdb` (`load_registry_expiries`), an O(expiries) DuckDB
    # read — re-run on EVERY options poll. The calendar is invariant between metadata
    # pulls, so it is memoised behind the SAME write generation as the caches above
    # (a metadata pull bumps it → re-read; otherwise an options poll is an O(1) handoff),
    # keyed per exchange. REUSES the COMMON `MoversSnapshotCache` (one calendar key).
    _options_calendar_cache = MoversSnapshotCache()
    # The Options-tab AUTHORITATIVE strike-chain cache (DEC-MONEYNESS-REGISTRY-STEP-001):
    # the ATM±N strike-grid step derives from the FULL vendor strike chain of each
    # tier-target (underlying, expiry) in `registry.duckdb` — otherwise the sparse
    # lake-observed ladder can OVERESTIMATE the step on a sparsely-pulled expiry. Read
    # LAZILY per key via `make_registry_chain_loader` (ONE narrowed DuckDB read per
    # (exchange, underlying, expiry) — never the whole monotonically-growing contracts
    # table; Golden Rule #5 bounded reads, DEC-FNO-HONESTY-PACK-001). Each chain is
    # invariant between metadata pulls, so it is memoised behind the SAME write
    # generation as the calendar cache above (a metadata pull bumps it → re-read;
    # otherwise an options poll is an O(1) handoff), keyed per (exchange, underlying,
    # expiry) — `registry_chain_key`. REUSES the COMMON `MoversSnapshotCache` (LRU: a
    # rarely-hit key evicts; a re-read is one bounded chain, never a whole-table scan).
    _options_strikes_cache = MoversSnapshotCache()
    # The /coverage + /universe nav-latency caches (DEC-DASHBOARD-NAV-LATENCY-001): three
    # uncached pollers — `/api/v1/coverage` (every 4 s), `/api/v1/universe/completeness`
    # and `/api/v1/universe/mapping-integrity` (every 15 s) — re-ran an O(datasets)+ledger
    # projection on EVERY poll. Each payload is a pure function of the instrument master +
    # the coverage ledger, so it is memoised behind a COMPOSITE token = the dashboard
    # write-generation (`_summary_cache.generation`, bumped by every pull / materialize)
    # AND the instrument-master content revision (`master_revision`, which a keyless
    # `load-instruments` moves WITHOUT bumping the generation). Folding BOTH into the cache
    # KEY (with a fixed generation=0) means a hit fires only when neither the ledger nor the
    # master has changed — a pull OR an instrument load → new key → recompute, never stale
    # (Golden Rule #1). REUSES the COMMON generation-stamped LRU `MoversSnapshotCache` (the
    # SAME generic memo `_db_datasets_cache` / `_candle_count_cache` use; no new class).
    _coverage_cache = MoversSnapshotCache()
    _universe_completeness_cache = MoversSnapshotCache()
    _mapping_integrity_cache = MoversSnapshotCache()
    # The /universe lake-uniqueness census (DEC-LAKE-HEAL-001) deliberately does
    # NOT get a `MoversSnapshotCache` here: it is a full bounded lake scan —
    # O(files), one key-column read per file — and `pull_jobs` bumps the write
    # generation on EVERY per-pair write tick, so a generation-keyed memo would
    # re-run the whole-lake scan on every 15 s chip poll during a pull (the #920
    # regression class). It rides `_summary_cache.get_or_refresh(LAKE_UNIQUENESS,
    # …)` instead — the COMMON adaptive-TTL-floor path /coverage/spot uses — so
    # an expensive scan self-throttles to max(2 s, its own duration) capped 120 s
    # while an idle lake stays snappy (see `_build_lake_census_payload_cached`).
    # The /results sweep-leaderboard cache (DEC-DASHBOARD-RESULTS-PAGE-001): the
    # `<lake>/sweeps` discovery + per-tuple Parquet read is O(stores × rows); the
    # ranked leaderboard for a given (instrument, tf, direction, rank_by, top_n) is a
    # pure function of the sweep tree, so it is memoised behind the SAME write
    # generation as the caches above (a sweep run / pull bumps `_summary_cache`'s
    # generation → re-read; otherwise a repeat poll or a filter the operator already
    # viewed is an O(1) handoff). REUSES the COMMON generation-stamped LRU
    # `MoversSnapshotCache` (no new class — CLAUDE.md §11 COMMON).
    _sweep_leaderboard_cache = MoversSnapshotCache()
    # ONE rate controller per dashboard session, shared across every pull
    # (DEC-DHAN-PERSIST-RATE-001) so the learned rate carries over instead of each
    # pull restarting at the 5 r/s ceiling and re-storming the vendor.
    _session_rate = SessionRateController()
    # The Greeks-enrichment consecutive-failure health counter
    # (DEC-GREEKS-ENRICH-OBSERVE-001): the fully-guarded post-pull Greeks pass can
    # NEVER fail a pull, so a persistently broken enrichment used to be one
    # swallowed log line per pull. Each pull job feeds its report's whole-pass
    # `greeks_enrich_ok` verdict here; at the threshold crossing ONE structlog
    # WARNING `pull.greeks_enrich_failing` fires (log-only — no Telegram push:
    # `pull_degraded`'s r/s units would be dishonest for a Greeks pass). Owned by
    # this app instance (no module-global mutable state — CLAUDE.md §4);
    # PROCESS-LOCAL — a restart resets the streak (honest envelope).
    _greeks_health = GreeksEnrichHealth()

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

        The internal ``last_update_monotonic`` freshness baseline (``-inf`` until a pull
        ticks — DEC-LIVE-RATE-MONITOR-001) is DROPPED from this payload (via the shared
        :meth:`RateLimitPanel.to_json_dict`): it is a monotonic bookkeeping value (not
        wall-clock-meaningful to a client) and ``-inf`` is not valid JSON. The dedicated
        ``/api/v1/pull/rate`` poll surfaces the derived staleness.
        """
        return JSONResponse(state.rate_limit.to_json_dict())

    @app.get("/api/v1/pull/rate")
    async def api_pull_rate() -> JSONResponse:
        """GET /api/v1/pull/rate — live pull req/sec·min·hour + a STALL verdict.

        DEC-LIVE-RATE-MONITOR-001. The dedicated, O(1) in-memory poll the live-rate
        monitor WIDGET reads (every ~2-4 s, site-wide). It projects the in-memory
        :class:`RateLimitPanel` — populated during a live Groww pull by
        :func:`make_rate_observer` from the REAL
        :meth:`~brutex.adapters.rate_common.RateController.snapshot` (``current_rps`` /
        ``recent_minute`` / ``recent_hits``) + the transport
        :meth:`~brutex.adapters.call_meter.CallMeter.snapshot` (``effective_rps``) — into
        a :class:`~brutex.dashboard.pull_rate_view.PullRateView`:

        * **req/sec** achieved (measured ``effective_rps``) vs allowed (``current_rps``);
        * **req/min** — the controller's measured rolling-60s issue count;
        * **req/hour** — an OBSERVED extrapolation (``req/min × 60``, so labelled);
        * a **stall signal** — ``seconds_since_update`` + the ``stalled`` / ``idle`` /
          ``flowing`` verdict — plus the configured ``ceiling_rps`` (8) /
          ``ceiling_per_min`` (500) LIMITS.

        When NO pull is running (no live tick within the staleness window) every live
        number is ZEROED and ``flow_state`` is ``idle`` — never a fabricated "live"
        figure (Golden Rule #1). The PANEL half is a pure in-memory read (never touches
        the lake / store lock, never blocks the pull); the BRIDGE half below is
        TTL+token-memoised, deadline-guarded and threadpool-offloaded, with its
        provenance disclosed via ``bridge_source`` — never an unbounded lake touch on
        the event loop (DEC-PULL-RATE-BRIDGE-NONBLOCK-CACHE-001).

        Cross-process bridge (DEC-CLI-RATE-BRIDGE-001): a CLI ``brutex fetch`` pull runs
        in a SEPARATE process and never touches this in-memory panel, so it ALSO publishes
        its rate to ``<lake_root>/.rate/pull_rate.json``. This route best-effort reads that
        bridge file — through the injected
        :class:`~brutex.dashboard.rate_bridge_cache.RateBridgeCache` on an AnyIO worker
        (a TTL hit is a zero-syscall O(1) memo; a post-TTL refresh is bounded by the
        freeze-guard deadline; a wedged lake volume serves the last-known snapshot
        labelled ``unverified``) — and surfaces whichever pull (dashboard OR CLI) ticked
        most recently, so the widget reflects the active CLI pull's real measured rate
        too. A missing / stale / unreadable bridge file is honestly ignored (the
        in-memory panel wins, or IDLE). Staleness stays honest under the memo: the
        verdict ages derive from the snapshot's OWN ``wall_time``, recomputed per
        request, so a cached serve can never fake freshness. This is cross-process
        VISIBILITY, NOT shared rate accounting — the per-process caps are unchanged.
        The payload carries two additive freshness keys — ``bridge_source`` ∈
        {``live``, ``cached``, ``unverified``, ``unwired``} + ``bridge_probe_age_s`` —
        stamped by the ONE :func:`~brutex.dashboard.rate_bridge_cache.stamp_bridge_freshness`.
        """
        from brutex.dashboard.pull_rate_view import build_pull_rate_view_merged

        now_monotonic = time.monotonic()
        with state._lock:  # pyright: ignore[reportPrivateUsage]
            panel = state.rate_limit
            panel_effective_rps = panel.effective_rps
            panel_current_rps = panel.current_rps
            panel_recent_minute = panel.recent_minute
            panel_recent_429 = panel.recent_429_count
            panel_recent_1s = panel.recent_1s
            panel_recent_600s = panel.recent_600s
            panel_recent_3600s = panel.recent_3600s
            panel_meter_present = panel.meter_present
            panel_failed_rps = panel.failed_rps
            panel_last_update = panel.last_update_monotonic
        if math.isinf(panel_last_update):
            panel_age = math.inf
        else:
            panel_age = max(0.0, now_monotonic - panel_last_update)

        # Best-effort cross-process bridge read (never raises, never blocks the pull): a
        # damaged / absent file degrades to None so the in-memory panel alone is projected
        # (byte-identical to the pre-bridge behaviour). The lake root is read off the wired
        # instrument store; without one (no resolvable lake) the bridge is simply skipped
        # (an honest `unwired` provenance, ZERO cache/threadpool work). The read itself
        # goes through the memoising, deadline-guarded RateBridgeCache on an AnyIO worker
        # (the app.py fast-read offload doctrine) so the event loop NEVER sees the lake
        # volume on any arm (DEC-PULL-RATE-BRIDGE-NONBLOCK-CACHE-001).
        bridge_age: float | None = None
        bridge_effective_rps: float | None = None
        bridge_current_rps: float | None = None
        bridge_recent_minute: int | None = None
        bridge_recent_429: int | None = None
        bridge_recent_1s: int | None = None
        bridge_recent_600s: int | None = None
        bridge_recent_3600s: int | None = None
        bridge_pull_active: bool | None = None
        bridge_lake_root = _rate_bridge_lake_root_or_none(instrument_store)
        if bridge_lake_root is None:
            bridge_result = BridgeReadResult(None, BRIDGE_SOURCE_UNWIRED, None)
        else:
            bridge_result = await run_in_threadpool(_rate_bridge_cache.get, bridge_lake_root)
        snapshot = bridge_result.snapshot
        if snapshot is not None:
            bridge_age = max(0.0, time.time() - snapshot.wall_time)
            bridge_effective_rps = snapshot.effective_rps
            bridge_current_rps = snapshot.current_rps
            bridge_recent_minute = snapshot.recent_minute
            bridge_recent_429 = snapshot.recent_429
            bridge_recent_1s = snapshot.recent_1s
            bridge_recent_600s = snapshot.recent_600s
            bridge_recent_3600s = snapshot.recent_3600s
            bridge_pull_active = snapshot.pull_active

        view = build_pull_rate_view_merged(
            panel_effective_rps=panel_effective_rps,
            panel_current_rps=panel_current_rps,
            panel_recent_minute=panel_recent_minute,
            panel_recent_429_count=panel_recent_429,
            panel_seconds_since_update=panel_age,
            panel_recent_1s=panel_recent_1s,
            panel_recent_600s=panel_recent_600s,
            panel_recent_3600s=panel_recent_3600s,
            panel_meter_present=panel_meter_present,
            # DEC-PULL-TELEMETRY-TRIAD-001: the meter's MEASURED failed-calls rate, so an
            # issuing-but-all-failing meter-fed pull reads the distinct "failing" verdict.
            panel_failed_rps=panel_failed_rps,
            # DEC-RATE-STALL-PULL-GATE-001: the AUTHORITATIVE pull-run signal — the
            # in-process panel is only ever fed by THIS app's pulls, so its stall
            # verdict is gated on the job actually running: a just-completed pull
            # reads "done"/"idle", never a red "stalled". The CLI bridge carries its
            # OWN flag since schema v3 (DEC-CLI-BRIDGE-PULL-ACTIVE-001) — a pre-v3 /
            # flagless file reads None (the honest tick-freshness fallback).
            panel_pull_active=_pull_active_probe(),
            # DEC-PIPELINE-PHASE-UI-HONESTY-001: the pipeline's current phase label —
            # the PANEL side only (the CLI bridge is a separate process with no
            # pipeline phase). During the zero-vendor-call LOCAL stages
            # (prebuild/movers) the verdict reads amber "local", never a red
            # "stalled" off the correctly-flatlined fetch rate.
            panel_phase=_pull_phase_probe(),
            bridge_effective_rps=bridge_effective_rps,
            bridge_current_rps=bridge_current_rps,
            bridge_recent_minute=bridge_recent_minute,
            bridge_recent_429_count=bridge_recent_429,
            bridge_seconds_since_update=bridge_age,
            bridge_recent_1s=bridge_recent_1s,
            bridge_recent_600s=bridge_recent_600s,
            bridge_recent_3600s=bridge_recent_3600s,
            bridge_pull_active=bridge_pull_active,
        )
        # Two ADDITIVE honest-freshness keys (bridge_source / bridge_probe_age_s),
        # written in exactly ONE place — every pre-existing key stays byte-identical.
        payload = view.to_dict()
        stamp_bridge_freshness(payload, bridge_result)
        return JSONResponse(payload)

    # The former ``GET /api/v1/quota`` route (DEC-DASHBOARD-QUOTA-PERSISTED-001)
    # is DELETED (DEC-QUOTA-VERDICT-MOOT-001): the live vendor (Groww) has no
    # daily quota (``GROWW_DAILY_QUOTA_UNVERIFIED=None``, CLAUDE.md §2 Rule 6),
    # no production caller ever injected a tracker (``brutex serve`` passes
    # none), so the route only ever returned the constant ``quota_limit=-1``
    # sentinel — a second, divergent quota surface with no consumer beyond the
    # /universe gauge deleted alongside it.

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
        dry_run: bool = Query(default=False),
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
        ``dry_run`` is EXPLICIT opt-in ONLY (DEC-PULL-LOUD-CREDENTIAL-REFUSAL-001):
        a real pull with missing/broken Groww credentials is REFUSED with a 503
        carrying the CredentialsError's own fix text — it is never silently
        downgraded to the synthetic dry-run (audit finding R1); pass
        ``?dry_run=true`` for the loudly-labeled synthetic run.
        """
        dry_run = _gate_pull_dry_run(dry_run, route="universe.pull")
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
                floor_store=instrument_store,
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

    # The /api/v1/pull/rate stall gate's pull-run signal (DEC-RATE-STALL-PULL-GATE-001):
    # the production default reads THIS app's own single-flight job (`running` is an O(1)
    # lock-guarded bool), so the widget can never read "stalled" after the pull finished;
    # tests inject a deterministic probe. Resolved at request time via the route closure.
    _pull_active_probe: Callable[[], bool | None] = (
        pull_active_probe if pull_active_probe is not None else (lambda: _pull_job.running)
    )

    # The pipeline-phase probe (DEC-PIPELINE-PHASE-UI-HONESTY-001): the production
    # default reads THIS app's own single-flight job's coarse phase label (an O(1)
    # lock-guarded PullProgress snapshot — "spot"/"metadata"/"historical"/"movers"/
    # "prebuild", "" when none is in flight). Read at request time by the gated
    # /api/v1/movers payload (phase-honest note) and the /api/v1/pull/rate verdict
    # (amber "local" during the zero-vendor-call prebuild/movers stages); tests
    # inject a deterministic probe.
    _pull_phase_probe: Callable[[], str | None] = (
        pull_phase_probe if pull_phase_probe is not None else (lambda: _pull_job.snapshot().phase)
    )

    # Last finished spot pull's verdict dict (DEC-VERDICT-BANNER-001). The
    # background _work closure stamps the BulkSpotReport's `.verdict.to_dict()`
    # here when a pull / retry completes; GET /pull-verdict maps it through the
    # pure banner_for() helper so /universe can SHOW the coloured banner. A
    # one-key holder (not a module-global — CLAUDE.md §4); the write is a single
    # atomic reference assignment on the job thread, the read is on the request
    # thread (CPython GIL → torn-state-free). `None` until the first pull lands.
    _last_pull_verdict: dict[str, dict[str, Any] | None] = {"verdict": None}

    def _gate_pull_dry_run(dry_run_requested: bool, *, route: str) -> bool:
        """Resolve a pull route's EFFECTIVE dry_run — explicit opt-in or LOUD refusal.

        DEC-PULL-LOUD-CREDENTIAL-REFUSAL-001 (killing audit finding R1): every
        pull route used to compute ``dry_run = not groww_credentials_present()``,
        so ANY Groww/AWS credential failure SILENTLY swapped the real pull for
        the synthetic ``(SyntheticSpotAdapter, NullBarSink)`` pair and reported
        the pull GREEN with counts while ZERO real bars landed — and the
        :class:`~brutex.config.credentials_types.CredentialsError`'s actionable
        fix text was discarded at the gate. Now the ONE shared gate
        (:func:`~brutex.adapters.ingest_common.gate_pull_credentials`, also run
        by the CLI — CLAUDE.md §11 COMMON) decides:

        * ``dry_run_requested=True`` (the explicit ``?dry_run=true`` param) →
          synthetic runs, loudly labeled — credentials irrelevant.
        * credentials resolve → the REAL pull proceeds (``False``).
        * otherwise → **HTTP 503** whose detail is the relayed CredentialsError
          fix text (whatever env var / SSM parameter it names — DYNAMIC, no
          hardcoding), the same text logged at ERROR and stamped into the
          ``/pull-verdict`` banner slot as the red ``credentials_missing``
          verdict (``needs_operator=True``) — never a silent downgrade.

        (Defined after ``_last_pull_verdict`` but referenced by the earlier
        ``/universe/pull`` route — closure names bind at request time.)
        """
        refusal = gate_pull_credentials(
            dry_run_requested=dry_run_requested,
            credentials_error=groww_credentials_error,
        )
        if refusal is not None:
            # Stamp the RED refusal verdict so the /universe banner shows the
            # fix text too — needs_operator semantics, not just a 503 body.
            _last_pull_verdict["verdict"] = refusal.to_dict()
            if liveproof is not None:
                # Mirror the SAME relayed fix text onto the persistent
                # live-proof banner (DEC-DOCTOR-LIVE-AUTORUN-001): the banner
                # and this 503 must agree even when credentials broke AFTER
                # the startup preflight ran. Belt (hostile-review MAJOR-1):
                # a holder bug must never mask the 503 with a 500 — the
                # refusal itself outranks its banner mirror.
                try:
                    liveproof.note_credentials_refusal(verdict_message(refusal))
                except Exception as exc:  # pylint: disable=broad-exception-caught  # belt
                    _LOG.warning("liveproof.refusal_mirror_failed", route=route, error=str(exc))
            _LOG.error(
                "universe.pull.refused_no_credentials",
                route=route,
                reason=refusal.auth_cause,
            )
            raise HTTPException(status_code=503, detail=verdict_message(refusal))
        if dry_run_requested:
            _LOG.info("universe.pull.synthetic_dry_run_requested", route=route)
            return True
        if liveproof is not None:
            # A REAL pull was admitted — the zero-command trigger for the
            # ONE-TIME expensive probes (VIX / 2020 depth / bhavcopy).
            # Single-flight + cached-verdict guarded inside; O(1) here, never
            # blocks or touches the pull job (DEC-DOCTOR-LIVE-AUTORUN-001).
            # Belt (hostile-review MAJOR-1): the trigger must be UNABLE to
            # raise into the admitted pull — the holder already converts a
            # spawn failure into an honest error verdict; this guard covers
            # any residual holder bug. The pull always proceeds.
            try:
                liveproof.on_real_pull()
            except Exception as exc:  # pylint: disable=broad-exception-caught  # belt
                _LOG.warning("liveproof.probe_trigger_failed", route=route, error=str(exc))
        return False

    @app.post("/api/v1/universe/pull-all")
    async def api_universe_pull_all(
        kind: str | None = Query(default=None),
        symbol: str | None = Query(default=None),
        symbols: str | None = Query(default=None),
        year: int | None = Query(default=None),
        from_date: str | None = Query(default=None),
        to_date: str | None = Query(default=None),
        spot_interval: str | None = Query(default=None),
        dry_run: bool = Query(default=False),
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
        silent partial). ``dry_run`` is EXPLICIT opt-in ONLY
        (DEC-PULL-LOUD-CREDENTIAL-REFUSAL-001): missing/broken Groww creds
        REFUSE the real pull with a 503 relaying the CredentialsError fix text
        (never a silent synthetic downgrade); ``?dry_run=true`` runs the
        loudly-labeled synthetic dry-run.
        """
        dry_run = _gate_pull_dry_run(dry_run, route="universe.pull_all")
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
        if dry_run:
            # Explicit synthetic run: the progress title must SAY so loudly
            # (DEC-PULL-LOUD-CREDENTIAL-REFUSAL-001 — never look like a real pull).
            detail = label_synthetic(detail)
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
            (every window fetched — the normal exit), ``stop_quota`` (dormant under
            Groww — no daily quota, no tracker wired; DEC-QUOTA-VERDICT-MOOT-001),
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
                floor_store=instrument_store,
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
        dry_run: bool = Query(default=False),
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
        400. ``dry_run`` is EXPLICIT opt-in ONLY
        (DEC-PULL-LOUD-CREDENTIAL-REFUSAL-001): missing/broken Groww creds
        REFUSE the real pull with a 503 relaying the CredentialsError fix text
        (never a silent synthetic downgrade); an explicit ``?dry_run=true``
        enumerates an honest EMPTY chain (strikes/expiries are real vendor data,
        never fabricated offline, Golden Rule #1), loudly labeled.
        """
        dry_run = _gate_pull_dry_run(dry_run, route="universe.pull_expiries_contracts")
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
        if dry_run:
            # Explicit synthetic run: the progress title must SAY so loudly
            # (DEC-PULL-LOUD-CREDENTIAL-REFUSAL-001 — never look like a real pull).
            detail = label_synthetic(detail)
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
            #
            # Keep the live PULL RATE panel fed through this STANDALONE metadata phase too
            # (DEC-PULL-RATE-WIDGET-VISIBILITY-001 MINOR-1): the metadata pull has no
            # per-window observe seam, so without the sampler the panel goes stale →
            # /api/v1/pull/rate reports `idle` for the whole chain enumeration even while
            # the SHARED controller is pacing real expiry/contract calls. The sampler ticks
            # the SAME controller through make_rate_observer (observe-only, best-effort,
            # never builds a controller), mirroring the /pull-pipeline wrap.
            with (
                RatePanelSampler(session_rate=_session_rate, observer=make_rate_observer(state)),
                RegistryStore.open(registry_path) as registry,
            ):
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

            # Keep the live PULL RATE panel fed through this STANDALONE composite
            # (metadata + long F&O/historical) phase too (DEC-PULL-RATE-WIDGET-VISIBILITY-001
            # MINOR-1): neither half has a per-window observe seam, so without the sampler
            # the panel goes stale → /api/v1/pull/rate reports `idle` for the whole
            # ~38-min F&O fill even while the SHARED controller paces real fetches. The
            # sampler ticks the SAME controller through make_rate_observer (observe-only,
            # best-effort, never builds a controller), mirroring the /pull-pipeline wrap.
            with (
                RatePanelSampler(session_rate=_session_rate, observer=make_rate_observer(state)),
                RegistryStore.open(registry_path) as registry,
            ):
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

        composite_detail = f"{tradable_count} composite"
        if dry_run:
            # Explicit synthetic run: the progress title must SAY so loudly
            # (DEC-PULL-LOUD-CREDENTIAL-REFUSAL-001 — never look like a real pull).
            composite_detail = label_synthetic(composite_detail)
        started = _pull_job.start(_work, total=tradable_count, detail=composite_detail)
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
        dry_run: bool = Query(default=False),
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
        EXPLICIT opt-in ONLY (DEC-PULL-LOUD-CREDENTIAL-REFUSAL-001): missing/broken
        Groww creds REFUSE the real pull with a 503 relaying the CredentialsError fix
        text (never a silent synthetic downgrade); an explicit ``?dry_run=true`` runs
        the loudly-labeled offline pass (the empty-candle client appends 0 bars — an
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
        dry_run = _gate_pull_dry_run(dry_run, route="universe.pull_historical")
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
        if dry_run:
            # Explicit synthetic run: the progress title must SAY so loudly
            # (DEC-PULL-LOUD-CREDENTIAL-REFUSAL-001 — never look like a real pull).
            detail = label_synthetic(detail)
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

            # Keep the live PULL RATE panel fed through this STANDALONE F&O/historical
            # phase too (DEC-PULL-RATE-WIDGET-VISIBILITY-001 MINOR-1): the contract-candle
            # pull has no per-window observe seam, so without the sampler the panel goes
            # stale → /api/v1/pull/rate reports `idle` for the whole candle fill even while
            # the SHARED controller is pacing real fetches. The sampler ticks the SAME
            # controller through make_rate_observer (observe-only, best-effort, never builds
            # a controller), mirroring the /pull-pipeline wrap.
            with (
                RatePanelSampler(session_rate=_session_rate, observer=make_rate_observer(state)),
                RegistryStore.open(registry_path) as registry,
            ):
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
            # Consecutive-failure health over the post-pull Greeks pass
            # (DEC-GREEKS-ENRICH-OBSERVE-001): a dry-run's `None` verdict is
            # ignored inside `observe`, so a synthetic pull never moves the streak.
            _greeks_health.observe(report.greeks_enrich_ok, error=report.greeks_enrich_error)
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

    def _launch_pipeline(
        *,
        symbols: str | None,
        symbol: str | None,
        from_date: str | None,
        to_date: str | None,
        dry_run: bool,
        intent_restarts: int = 0,
    ) -> dict[str, Any]:
        """The ONE-CLICK pull(+movers) pipeline launcher — the ``/pull-pipeline`` body.

        Extracted from the route (DEC-PULL-INTENT-RESUME-001) so the SAME
        validated launch path serves BOTH the operator's POST and the boot-time
        intent resume (``app.state.brutex_resume_pipeline``) — the persistable
        request surface is exactly this keyword quadruple plus ``dry_run``;
        everything else (targets, lake root, years, intervals) is re-derived
        here. *intent_restarts* threads the resume path's already-bumped
        restart counter into the re-persisted intent (a fresh operator click
        passes the default ``0``); a ``dry_run`` NEVER persists an intent and
        NEVER clears one (a crashed real pull's intent must survive a dry-run).
        Raises ``HTTPException`` exactly as the route did (400 validation /
        503 credential refusal); returns the route's JSON payload dict.

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
        4. **Pre-build timeframes (gated, the FINAL data stage —
           DEC-PIPELINE-AUTOPREBUILD-001)** — materialise ALL derived timeframes
           (19 intraday + the derived ``1day``) for SPOT + FUTURES + OPTIONS over
           the just-filled lake, ONLY after a fully-successful fetch. Derived data
           is written ONLY here — never mid-pull, and the old manual
           "Pre-build timeframes" button is removed.
        5. **Movers (gated, AFTER the prebuild)** — warm the default movers
           snapshots only once the prebuild has run (movers read the derived
           timeframes the prebuild just wrote).

        The GATE between phases is the pure
        :func:`~brutex.dashboard.pipeline_pull.run_full_pipeline`: a phase that ends
        with a blocking failure (dead token, daily quota, still-failing windows, or
        a raised chain enumeration) STOPS the chain — the later phases never make a
        doomed vendor call and are reported ``None`` (fail-CLOSED, never a false
        "complete"). Runs on the single-flight background job (returns at once; poll
        ``/pull-progress``). ``dry_run`` is EXPLICIT opt-in ONLY
        (DEC-PULL-LOUD-CREDENTIAL-REFUSAL-001): missing/broken Groww creds REFUSE
        the real pipeline with a 503 relaying the CredentialsError fix text (never
        a silent synthetic downgrade); an explicit ``?dry_run=true`` runs the
        loudly-labeled offline pass, which honestly pulls nothing real and stops
        at an empty chain (never fabricated data, Golden Rule #1).

        ``from_date`` / ``to_date`` (ISO ``YYYY-MM-DD``) re-add a date range to the
        SPOT history phase (DEC-UNIVERSE-PULL-DATERANGE-001, re-wiring the picker
        DEC-UNIVERSE-PULL-CONSOLIDATE-001 had removed as inert): they scope the
        SPOT pull window AND narrow the metadata-pull YEAR grid. **Blank ⇒ the
        canonical default** ``[2020-01-01, previous trading day]`` (byte-identical
        to the pre-picker behaviour). The F&O HISTORICAL phase receives the dates
        as an **EXPIRY scope** (DEC-FNO-EXPIRY-SCOPE-001, amended at the LIVE
        edge by DEC-EXPIRY-SCOPE-LIVE-EDGE-001): a DEFAULT-shaped bound is
        lifted to unbounded before threading (``to_date`` at/after the
        request-time previous day ⇒ ``scope_end=None`` so live/unexpired
        contracts stay in scope; ``from_date`` at/below the 2020 floor ⇒
        ``scope_start=None``) — only a GENUINELY narrowed range scopes: then
        only expiries within ``[from_date, to_date]`` (both inclusive) are candle-pulled — an
        out-of-scope expiry is fetched not at all (zero vendor calls, zero
        ledger rows, one ``fno.expiry_out_of_scope`` log per expiry; stored
        bars untouched) — while each IN-scope contract keeps its per-contract
        smart default WINDOW law (futures ``expiry − 60d`` / options
        ``expiry − 30d`` over the standard envelope), so a wide explicit range
        still can never over-pull a contract's window. The range is validated
        EAGERLY via :func:`~brutex.dashboard.pull_controller.resolve_candle_range`
        — a reversed / below-2020 / non-ISO range is a clean 400, never a silent
        empty pull.
        """
        dry_run = _gate_pull_dry_run(dry_run, route="universe.pull_pipeline")
        # Normalize the date params to ONE definition of "blank" up front: an
        # empty / whitespace-only value becomes ``None`` so every downstream
        # helper (``resolve_candle_range`` AND ``_resolve_metadata_years`` AND
        # ``pull_all_spot``) sees the SAME thing. Without this the two helpers
        # disagree — ``resolve_candle_range`` strips ``"  "`` to ``None`` (passes)
        # but ``_resolve_metadata_years`` does not, so ``date.fromisoformat("  ")``
        # would raise uncaught → a 500 on a whitespace param (unreachable from the
        # date-input UI, but ``/pull-pipeline`` is a public API) — DEC-UNIVERSE-
        # PULL-DATERANGE-001. Normalized blank ⇒ the canonical 2020→prev-day default.
        from_date = (from_date or "").strip() or None
        to_date = (to_date or "").strip() or None
        # Validate the spot date range EARLY (before the background job is queued)
        # so a reversed / below-2020 / non-ISO pick is a clean 400. The resolved
        # window is recomputed inside the spot phase from the SAME strings — this
        # call is the fail-fast guard.
        try:
            resolve_candle_range(
                from_date=from_date,
                to_date=to_date,
                year=None,
                now=datetime.now(UTC),
            )
            # The F&O EXPIRY scope (DEC-FNO-EXPIRY-SCOPE-001): the SAME operator
            # dates, parsed ONCE inside the same fail-fast guard (a bad date is a
            # clean 400, never a 500). Blank ⇒ None ⇒ unbounded ⇒ byte-identical
            # to the unscoped pull.
            fno_scope_start = _parse_iso_date_arg(from_date, "from_date") if from_date else None
            fno_scope_end = _parse_iso_date_arg(to_date, "to_date") if to_date else None
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # LIVE-EDGE scope normalization (DEC-EXPIRY-SCOPE-LIVE-EDGE-001, backend
        # AUTHORITATIVE): the pickers pre-fill the canonical default and the
        # frontend historically sent them whenever non-blank, so every default
        # one-click pull carried scope_end = previous day — scoping OUT all
        # live/unexpired contracts. A default-shaped bound (scope_end at/after
        # the request-time previous day per the SAME resolve_candle_range law
        # that pre-fills the picker; scope_start at/below GROWW_HISTORY_FLOOR)
        # is lifted to None here, whatever the frontend sent. A genuinely
        # narrowed range scopes exactly as before (DEC-FNO-EXPIRY-SCOPE-001
        # untouched below the live edge).
        fno_scope_start, fno_scope_end = normalize_expiry_scope(
            fno_scope_start,
            fno_scope_end,
            envelope_end=date.fromisoformat(default_date_range()[1]),
            floor=GROWW_HISTORY_FLOOR,
        )
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
        years = _resolve_metadata_years(from_date, to_date)
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
            # Movers PREBUILD GATE (DEC-PIPELINE-AUTOPREBUILD-001 chunk 2): flips
            # True inside `_prebuild_timeframes` when the prebuild outcome is ok;
            # the try/finally below releases the gate with it — including on a
            # spot/metadata early stop or a raised phase (ok stays False), so the
            # movers are NEVER left blocked by a failed pull.
            prebuild_ok = False

            def _phase_progress(label: str, bars: int, failed: bool) -> None:
                on_progress(label, bars, failed, False)

            def _ledger_sync(
                completed: int, total: int, bars: int, failed: int, current: str
            ) -> None:
                _pull_job.sync_from_ledger(
                    completed=completed, total=total, bars=bars, failed=failed, current=current
                )

            def _run_spot() -> BulkSpotReport:
                # DEC-UNIVERSE-PER-PHASE-PROGRESS-001: tag the coarse phase so the live
                # per-phase view highlights the running phase (label only — the window
                # tallies come from the ledger counts, never this string).
                _pull_job.set_phase("spot")
                # Honest gated-movers status (DEC-PREBUILD-GATE-HONEST-NOTE-001):
                # name the running stage on the gate too (no-op when idle/dry-run).
                _prebuild_gate.set_phase("spot")
                # The operator-picked spot range (DEC-UNIVERSE-PULL-DATERANGE-001);
                # blank ⇒ None ⇒ pull_all_spot's resolve_candle_range default
                # [2020-01-01, prev-day] (byte-identical to the pre-picker pull).
                return pull_all_spot(
                    targets,
                    lake_root=lake_root,
                    dry_run=dry_run,
                    from_date=from_date,
                    to_date=to_date,
                    year=None,
                    intervals=fetch_intervals,
                    on_progress=_phase_progress,
                    rate_observer=make_rate_observer(state),
                    session_rate=_session_rate,
                    auto_retry=True,
                    until_clean=True,
                    on_ledger_sync=_ledger_sync,
                    floor_store=instrument_store,
                )

            def _run_metadata() -> MetadataPullReport:
                # DEC-UNIVERSE-PER-PHASE-PROGRESS-001: the metadata phase writes NO
                # window-ledger rows, so the three window rows stay honestly "pending"
                # (build_phase_progress highlights no window row on the metadata label).
                _pull_job.set_phase("metadata")
                _prebuild_gate.set_phase("metadata")
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
                # DEC-UNIVERSE-PER-PHASE-PROGRESS-001: the historical F&O phase pulls
                # FUTURES before OPTIONS (DEC-FNO-PULL-ORDER-LOOKBACK-001); the coarse
                # "historical" label is REFINED to futures-vs-options by the live ledger
                # counts in build_phase_progress (fno_current_phase_from_counts), so no
                # segment tag is threaded through the F&O spine. The operator's from/to
                # rides in as the EXPIRY scope (DEC-FNO-EXPIRY-SCOPE-001): out-of-scope
                # expiries fetch nothing; in-scope windows keep the default 30d/60d law.
                # open_ended=True (DEC-PULL-PROGRESS-HONESTY-001 — supersedes the
                # pair_progress=True grow-total pin of DEC-PIPELINE-STALL-HEARTBEAT-001
                # at THIS site): the F&O phase now drives its units/total from the
                # THROTTLED market/interval-filtered ledger fold (on_ledger_sync below,
                # bars=None so the cross-run ledger bars never clobber the live tally),
                # its BARS from the fetch pipeline's per-window fold (on_bars — real
                # deltas; the async per-pair report honestly carries 0), and declares
                # the denominator NON-FINAL (contracts are planned mid-phase) so the
                # page shows "N units · total unknown" instead of a pinned 100%.
                _pull_job.set_phase("historical", open_ended=True)
                _prebuild_gate.set_phase("historical")

                def _hist_ledger_sync(
                    completed: int, total: int, bars: int, failed: int, current: str
                ) -> None:
                    del bars  # cross-run ledger bars ≠ this pull's live bars (on_bars owns them)
                    _pull_job.sync_from_ledger(
                        completed=completed, total=total, bars=None, failed=failed, current=current
                    )

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
                        scope_start=fno_scope_start,
                        scope_end=fno_scope_end,
                        on_bars=_pull_job.add_bars,
                        on_ledger_sync=_hist_ledger_sync,
                    )

            def _warm_for_date() -> tuple[str, int] | None:
                """Resolve the warm target ``(latest_movers_date, generation)``, or ``None``.

                Best-effort: the date resolution is wrapped so a transient lake-read race
                NEVER propagates to fail the already-successful segment (Correctness of the
                pull > the warm's latency benefit). Targets the LATEST available spot day
                (never 'today' — the pull fills only to the previous trading day, so a
                today-warm would cache empties); ``None`` when the lake has no spot day yet.
                """
                # Honest phase label (DEC-PIPELINE-STALL-HEARTBEAT-001): a stall
                # observed during the movers warm is named "movers". The gate
                # annotation lands on an ALREADY-RELEASED gate now — the prebuild
                # released it the moment its outcome was known
                # (DEC-MOVERS-WARM-UNBLOCK-001) — so it is invisible while idle
                # (a safe no-op annotation, kept for the finally-backstop path).
                _pull_job.set_phase("movers")
                _prebuild_gate.set_phase("movers")
                try:
                    warm_date = latest_movers_date(lake_root)
                except Exception as exc:  # pylint: disable=broad-except - best-effort warm
                    _LOG.warning("movers.warm.latest_date_failed", error=str(exc))
                    return None
                if warm_date is None:
                    return None
                # post-pull generation (the pull bumped it) — the SAME the /movers route reads.
                return warm_date, _summary_cache.generation

            def _warm_datasets(gen: int) -> list[dict[str, str]]:
                """The ONE lake-discovery walk per warm run (DEC-MOVERS-WARM-UNBLOCK-001).

                The 2026-07-04/05 incident root: each of the 39 warm jobs called
                its ``compute_*_candidates`` WITHOUT the ``datasets=`` memo param
                (DEC-MOVERS-DISCOVERY-MEMO-001's seam), so every job re-ran a
                fresh full-lake ``list_candle_datasets`` dir-walk — ~3 min each
                on the options-exploded post-prebuild tree (~10⁵-10⁶ partition
                dirs; forensics measured 170-256 s per key, one BSE key with
                ZERO candidates cost 169.4 s of pure walk). Reuse the EXACT
                generation-stamped memo the /movers route reads
                (``_db_datasets_cache`` key ``"movers_candle_datasets"``), so
                the whole warm run — AND the route afterwards — shares ONE walk
                per write generation (CLAUDE.md §11 COMMON; byte-identical
                candidates — same function, same inputs, pinned).
                """
                datasets: list[dict[str, str]] = _db_datasets_cache.get_or_compute(
                    "movers_candle_datasets",
                    generation=gen,
                    compute=lambda: list_candle_datasets(lake_root),
                )
                return datasets

            def _warm_spot_movers() -> MoversWarmResult:
                """Per-segment SPOT warm (DEC-PIPELINE-SEGMENT-MOVERS-001): fired when spot succeeds.

                Pre-warms the whole spot grid (every exchange scope — DEC-MOVERS-WARM-GRID-001)
                reusing the EXACT candidate_key + compute the /api/v1/movers route uses (no
                drift, CLAUDE.md §11 COMMON). Best-effort via ``warm_movers_snapshots`` — a
                warmer that raises is logged + counted failed, never failing the successful
                pull; per-coordinate progress logs + the gate's live i/N ride the SAME call
                (DEC-MOVERS-WARM-PROGRESS-001).
                """
                resolved = _warm_for_date()
                if resolved is None:
                    return MoversWarmResult(warmed=0, failed=0, total=0)
                warm_date, gen = resolved
                # ONE memoised discovery walk shared by every job in this warm
                # run + the /movers route (DEC-MOVERS-WARM-UNBLOCK-001 fix 1).
                warm_datasets = _warm_datasets(gen)

                def _make_spot_job(exch: str | None) -> Callable[[], int]:
                    def _spot_job() -> int:
                        cands = _movers_cache.get_or_compute(
                            candidate_key(
                                asset="spot",
                                exchange=exch,
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
                                exchange=exch,
                                datasets=warm_datasets,
                            ),
                        )
                        return len(cands.snapshots)

                    return _spot_job

                coords = spot_warm_grid()
                return warm_movers_snapshots(
                    [_make_spot_job(coord["exchange"]) for coord in coords],
                    segment="spot",
                    labels=[warm_coordinate_label(coord) for coord in coords],
                    # Segment-labelled i/N (DEC-MOVERS-WARM-UNBLOCK-001 fix 4):
                    # a bare "6/9" read as 7-of-9-total; label it "spot 6/9".
                    on_progress=lambda done, total: _prebuild_gate.set_progress(
                        done, total, segment="spot"
                    ),
                )

            def _warm_futures_movers() -> MoversWarmResult:
                """Per-segment FUTURES warm (DEC-PIPELINE-SEGMENT-MOVERS-001): fired when historical succeeds.

                Pre-warms the whole futures grid (exchange × Near/Next/Far —
                DEC-MOVERS-WARM-GRID-001) so any tier at any exchange is an O(1) cache hit.
                """
                resolved = _warm_for_date()
                if resolved is None:
                    return MoversWarmResult(warmed=0, failed=0, total=0)
                warm_date, gen = resolved
                # ONE memoised discovery walk shared by every job in this warm
                # run + the /movers route (DEC-MOVERS-WARM-UNBLOCK-001 fix 1).
                warm_datasets = _warm_datasets(gen)

                def _make_fut_job(exch: str | None, raw_tier: str | None) -> Callable[[], int]:
                    tier = normalize_futures_tier(raw_tier)

                    def _fut_job() -> int:
                        cands = _movers_cache.get_or_compute(
                            candidate_key(
                                asset="futures",
                                exchange=exch,
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
                                exchange=exch,
                                expiry_tier=tier,
                                datasets=warm_datasets,
                            ),
                        )
                        return len(cands.movers)

                    return _fut_job

                coords = futures_warm_grid()
                return warm_movers_snapshots(
                    [_make_fut_job(coord["exchange"], coord["expiry_tier"]) for coord in coords],
                    segment="futures",
                    labels=[warm_coordinate_label(coord) for coord in coords],
                    # Segment-labelled i/N (DEC-MOVERS-WARM-UNBLOCK-001 fix 4).
                    on_progress=lambda done, total: _prebuild_gate.set_progress(
                        done, total, segment="futures"
                    ),
                )

            def _warm_options_movers() -> MoversWarmResult:
                """Per-segment OPTIONS warm (DEC-PIPELINE-SEGMENT-MOVERS-001): fired when historical succeeds.

                Pre-warms the whole options grid (exchange × Near/Next/Far × All/Monthly/Weekly
                — DEC-MOVERS-WARM-GRID-001) so any of the operator's 27 default-day options
                coordinates is an O(1) cache hit (the "movers is slow" fix), reusing the
                AUTHORITATIVE vendor calendar (DEC-MOVERS-CALENDAR-TIERS-001) for the tiers.
                """
                resolved = _warm_for_date()
                if resolved is None:
                    return MoversWarmResult(warmed=0, failed=0, total=0)
                warm_date, gen = resolved
                # ONE memoised discovery walk shared by every job in this warm
                # run + the /movers route (DEC-MOVERS-WARM-UNBLOCK-001 fix 1).
                warm_datasets = _warm_datasets(gen)

                def _make_opt_job(
                    exch: str | None, raw_tier: str | None, raw_track: str | None
                ) -> Callable[[], int]:
                    tier, trk = normalize_options_selectors(raw_tier, raw_track)
                    calendar_expiries = _options_calendar_cache.get_or_compute(
                        f"options_calendar|{exch or ''}",
                        generation=gen,
                        compute=lambda: load_registry_expiries(registry_path, exchange=exch),
                    )
                    # The warm mirrors the live route's AUTHORITATIVE strike chains
                    # (DEC-MONEYNESS-REGISTRY-STEP-001) via the SAME lazy bounded
                    # per-(underlying, expiry) loader + cache (Golden Rule #5 /
                    # DEC-FNO-HONESTY-PACK-001) so the warmed candidate set is
                    # byte-identical to what the options poll would compute.
                    registry_chain_for = make_registry_chain_loader(
                        registry_path,
                        exchange=exch,
                        cache=_options_strikes_cache,
                        generation=gen,
                    )

                    def _opt_job() -> int:
                        cands = _movers_cache.get_or_compute(
                            candidate_key(
                                asset="options",
                                exchange=exch,
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
                                exchange=exch,
                                datasets=warm_datasets,
                                calendar_expiries=calendar_expiries,
                                registry_chain_for=registry_chain_for,
                            ),
                        )
                        return len(cands.movers)

                    return _opt_job

                coords = options_warm_grid()
                return warm_movers_snapshots(
                    [
                        _make_opt_job(
                            coord["exchange"], coord["expiry_tier"], coord["expiry_track"]
                        )
                        for coord in coords
                    ],
                    segment="options",
                    labels=[warm_coordinate_label(coord) for coord in coords],
                    # Segment-labelled i/N (DEC-MOVERS-WARM-UNBLOCK-001 fix 4).
                    on_progress=lambda done, total: _prebuild_gate.set_progress(
                        done, total, segment="options"
                    ),
                )

            def _prebuild_timeframes(exclude_symbols: frozenset[str]) -> PrebuildOutcome:
                """FINAL data stage (DEC-PIPELINE-AUTOPREBUILD-001): pre-build ALL derived TFs.

                Runs on EVERY completed-chain pull over the CLEAN symbol set —
                ``exclude_symbols`` is the UNCAPPED quarantined-name set
                ``run_full_pipeline`` derives, forwarded to the pure guarded
                runner so quarantined symbols are excluded + named while the
                other targets still get their derived timeframes
                (DEC-PREBUILD-CLEANSET-001, supersedes the #1145
                fully-clean-fetch gate).
                Covers the pulled SPOT underlyings PLUS their FUTURES + OPTIONS
                contracts discovered from the F&O candle lake cascade
                (``build_fno_cascade(list_fno_candle_datasets(lake_root))`` — the
                lean store holds only underlyings), each materialising the 19
                derived intraday timeframes AND the derived ``1day`` partition
                (``include_daily=True``) via the SHARED segment-aware
                ``materialize_lake_timeframes`` (CLAUDE.md §11 COMMON). The
                composition + quarantine control flow is the 100%-tested pure
                ``run_pipeline_prebuild_guarded`` — this binding is a thin seam.

                Crash-guarded INSIDE the pure helper: an unexpected NON-halt
                prebuild failure logs ``pipeline.prebuild.failed`` LOUDLY and
                returns an honest ``ok=False`` outcome carrying
                ``crashed=<ErrorType>`` — it can never kill the already-
                successful fetch job, and never a silent zero. A pull-wide
                GLOBAL STOP (disk-full/OOM ``LakeWriteHalt``, quota) RE-RAISES
                out of the guard (``pull_jobs.is_pull_halt`` — the ONE common
                predicate) and propagates to ``BackgroundPullJob._run``, which
                fires the CRITICAL Telegram pull-halt alert; the movers warm
                never runs after it (the raise precedes the warm stage inside
                ``run_full_pipeline``) and the ``finally`` below still releases
                the movers gate with ``prebuild_ok=False``.
                """
                # Movers gate DOWN for THE PREBUILD PHASE ONLY
                # (DEC-MOVERS-GATE-PREBUILD-SCOPE-001, narrowing the whole-
                # pipeline begin of DEC-PIPELINE-AUTOPREBUILD-001 chunk 2):
                # begin() as the FIRST statement of this closure, so the
                # multi-hour fetch phases never blank the movers — the gate's
                # honest meaning is "prebuild in progress". A dry_run pipeline
                # writes no real data and must NOT gate (mirrors the release
                # guard in the finally below).
                if not dry_run:
                    _prebuild_gate.begin()

                # Honest phase label (DEC-PIPELINE-STALL-HEARTBEAT-001): a stall
                # observed here is named "prebuild", never blamed on "historical".
                _pull_job.set_phase("prebuild")
                _prebuild_gate.set_phase("prebuild")

                # M1/m-4 (DEC-HARDWARE-ENVELOPE-001) × DEC-DAILY-CUTOFF-
                # GROUNDED-001: pin the forming-session cutoff's IST INSTANT
                # ONCE per prebuild run and thread it into BOTH paths (the
                # sequential closure below AND the threaded path via the
                # guard) — one instant, so a run straddling midnight IST (or
                # the session close) derives the same `1day` set regardless of
                # path or symbol order. Pinning the INSTANT (not a plain
                # today-IST DATE, the #1401 M1 regression this corrects) keeps
                # the session-close-aware daily_derive_cutoff default LIVE on
                # this production path — a POST-CLOSE pipeline run derives the
                # run day's own 1day bar (DEC-PREBUILD-SESSION-CLOSE-CUTOFF-001,
                # restored) — and keeps the dense symbols' data-grounded arm
                # reachable (an explicit daily_before date would be LAW and
                # suppress both).
                prebuild_cutoff_now = now_ist()

                def _materialize(target: PrebuildTarget) -> MaterializeResult:
                    return materialize_lake_timeframes(
                        lake_root=lake_root,
                        exchange=target.exchange,
                        segment=target.segment,
                        symbol=target.symbol,
                        kind=target.kind,
                        include_daily=True,
                        daily_cutoff_now=prebuild_cutoff_now,
                        # DEC-CALENDAR-EXPECTED-BARS-DAILY-OPEN-001: per-session
                        # substitution WARNs only for a SPOT INDEX target
                        # (PrebuildTarget.dense = Instrument.is_index).
                        dense_symbol=target.dense,
                    )

                nonlocal prebuild_ok
                # Parallelise the per-symbol derive across the host's CPU cores
                # (DEC-HARDWARE-ENVELOPE-001): auto-detect the worker count at
                # runtime (BRUTEX_PREBUILD_WORKERS=1 forces sequential). A
                # ``> 1`` count routes the guard to the THREAD-pool path
                # (byte-identical lake output); ``1`` keeps the sequential
                # injected-callable path. This sizes CPU COMPUTE only — never the
                # Groww fetch pool / rate ceiling (a real vendor limit).
                prebuild_workers = resolve_prebuild_workers(target_count=len(targets))
                try:
                    outcome = run_pipeline_prebuild_guarded(
                        instruments=targets,
                        cascade_or_error=lambda: build_fno_cascade(
                            list_fno_candle_datasets(lake_root)
                        ),
                        materialize=_materialize,
                        source="pipeline",
                        exclude_symbols=exclude_symbols,
                        # Allocation probe at the tiny-file writer's start
                        # (DEC-LAKE-FS-PROBE-001) — best-effort, never blocks.
                        lake_root=lake_root,
                        workers=prebuild_workers,
                        daily_cutoff_now=prebuild_cutoff_now,
                        # PRE-RUN disk guard (DEC-LAKE-FOOTPRINT-MEASURED-001):
                        # skip the whole prebuild gracefully when the lake
                        # volume can't hold the estimated derived writes
                        # (observed schema_meta multiplier, 6.5x fallback;
                        # BRUTEX_PREBUILD_DISK_GUARD=off to override). Fails
                        # OPEN on any measurement error. Applied on BOTH the
                        # sequential and the parallel (workers > 1) path.
                        disk_guard=make_disk_guard(
                            lake_root,
                            instrument_store.get_meta if instrument_store is not None else None,
                        ),
                    )
                    # Post-run observation (best-effort, never raises): persist
                    # the freshly-materialised lake's OBSERVED multiplier to
                    # schema_meta so the NEXT pull's guard uses reality instead
                    # of the fallback. Skipped when the guard blocked (nothing
                    # changed on disk) or without an instrument store.
                    if instrument_store is not None and not outcome.disk_guard_blocked:
                        record_lake_footprint(lake_root, instrument_store.set_meta)
                finally:
                    # New derived partitions may be on disk (even a partial run —
                    # or one the guard re-raised a global-stop out of — may have
                    # written some) — bump the write generation so the movers warm
                    # that FOLLOWS the prebuild (and the /db + coverage caches)
                    # reads fresh.
                    _summary_cache.mark_dirty()
                # The gate-release flag (chunk 2): the finally below releases the
                # movers gate with the prebuild's honest success verdict (a
                # re-raised global stop skips this line, leaving ok=False).
                prebuild_ok = outcome.ok
                # Release the movers gate the MOMENT the prebuild outcome is
                # known — ok OR failed (DEC-MOVERS-WARM-UNBLOCK-001 fix 3): the
                # half-filled-lake hazard the gate exists for ends at prebuild
                # completion, so the movers warm that FOLLOWS runs un-gated in
                # the background as a pure cache accelerator. The 2026-07-04/05
                # incident (pull_id 22fc7739f719): the warm's 39 full-lake-walk
                # keys (170-256 s each) held /movers hostage for hours behind
                # the finally-only release. The finally below STAYS as the
                # idempotent backstop for every path that never reaches this
                # line (early stop / raised phase / global stop / crash).
                if not dry_run:
                    _prebuild_gate.release(outcome.ok)
                return outcome

            # Keep the live PULL RATE panel fed through EVERY phase, not just spot
            # (DEC-PULL-RATE-WIDGET-VISIBILITY-001). The spot phase pushes the panel on its
            # per-window observe seam, but the metadata + F&O/historical phases have no such
            # seam, so without this the panel goes stale → /api/v1/pull/rate reports `idle`
            # for the entire ~38-min F&O phase even while the SHARED controller is pacing
            # real fetches. The sampler ticks the SAME controller through make_rate_observer
            # (observe-only, best-effort, never builds a controller) so the widget stays
            # live the whole pull.
            # Movers gate: begin() moved INSIDE `_prebuild_timeframes`
            # (DEC-MOVERS-GATE-PREBUILD-SCOPE-001) so the gate covers ONLY the
            # prebuild phase, never the multi-hour fetch — mid-fetch movers now
            # serve (with the spot stored-first read preferring the always-
            # fresh 1-minute resample while the pull runs).
            try:
                # keep_awake (DEC-PULL-KEEPAWAKE-001): hold the macOS host awake
                # for the WHOLE pipeline run (spot → metadata → historical →
                # prebuild → movers) — a sleeping Mac silently paused a real
                # 6.5 h pull mid-flight. Best-effort: non-darwin / missing
                # caffeinate is a logged no-op, never a raise.
                with (
                    keep_awake("brutex pull pipeline"),
                    RatePanelSampler(
                        session_rate=_session_rate, observer=make_rate_observer(state)
                    ),
                ):
                    report = run_full_pipeline(
                        run_spot=_run_spot,
                        run_metadata=_run_metadata,
                        run_historical=_run_historical,
                        warm_spot_movers=_warm_spot_movers,
                        warm_futures_movers=_warm_futures_movers,
                        warm_options_movers=_warm_options_movers,
                        run_prebuild=_prebuild_timeframes,
                    )
                # Discovery-memo warm (DEC-DB-DISCOVERY-ADAPTIVE-FLOOR-001 item
                # 1b): re-seed EVERY discovery memo off ONE shared walk at the
                # end-of-pull generation — runs on clean AND early-stopped
                # pipelines (any writes left the memos dirty), best-effort (a
                # warm failure never fails the pull), and force_fresh so the
                # still-`running` job's own pull-active floor cannot serve the
                # stale pre-pull entries back into the seed. A raised phase
                # (global stop) skips it — the next request then pays its own
                # walk with the pull probe already False.
                warm_discovery_memos(
                    lake_root=lake_root,
                    generation=_summary_cache.generation,
                    symbol_for_id_fn=lambda: {
                        str(i.security_id): i.symbol_name
                        for i in instrument_store.list_index_equity_instruments()
                    },
                    db_datasets_cache=_db_datasets_cache,
                    spot_cascade_cache=_spot_cascade_cache,
                    fno_cascade_cache=_fno_cascade_cache,
                    latest_date_cache=_latest_movers_date_cache,
                )
                if report.spot is not None:
                    _last_pull_verdict["verdict"] = report.spot.verdict.to_dict()
                # Consecutive-failure health over the post-pull Greeks pass
                # (DEC-GREEKS-ENRICH-OBSERVE-001), via the nested candle
                # sub-report. A stopped-early / no-candle pipeline (either
                # nesting level None) observes `None`, which `observe` ignores
                # — the streak only moves on a pull that actually ran the pass.
                composite = report.historical
                hist_report = composite.historical if composite is not None else None
                _greeks_health.observe(
                    None if hist_report is None else hist_report.greeks_enrich_ok,
                    error=None if hist_report is None else hist_report.greeks_enrich_error,
                )
                return report.to_dict()
            finally:
                # ALWAYS release — a spot/metadata stop, a raised phase, or a
                # clean run all un-gate the movers (never blocked forever);
                # `prebuild_ok` carries the prebuild's honest verdict (False when
                # the pipeline never reached it, in which case begin() never ran
                # either — release without begin is a pinned-safe no-op). On the
                # completed-prebuild path this is the idempotent BACKSTOP — the
                # primary release already fired inside `_prebuild_timeframes`
                # the moment the outcome was known (DEC-MOVERS-WARM-UNBLOCK-001),
                # so the movers warm never holds the gate. Logged by the gate
                # itself (`pipeline.prebuild.gate_released` {ok}).
                if not dry_run:
                    _prebuild_gate.release(prebuild_ok)

        pipeline_detail = f"pipeline {len(targets)} symbols"
        if dry_run:
            # Explicit synthetic run: the progress title must SAY so loudly
            # (DEC-PULL-LOUD-CREDENTIAL-REFUSAL-001 — never look like a real pull).
            pipeline_detail = label_synthetic(pipeline_detail)
        # Durable pull intent (DEC-PULL-INTENT-RESUME-001): persist the re-fire
        # payload AFTER validation and BEFORE the job starts, so a SIGKILL at
        # any later point leaves the crash-recovery record on disk. A dry_run
        # never persists (persist_intent_for_launch returns False without
        # writing) and never wires the clear hook — it must not erase a crashed
        # REAL pull's intent. The clear fires in the job's finally, on the
        # worker's success AND failure, BEFORE the single-flight release.
        on_finally: Callable[[], None] | None = None
        if pull_intent_store is not None and not dry_run:
            # Best-effort write (a failed write is logged, never blocks the
            # pull); the clear hook is wired for EVERY real pull regardless —
            # a completing real pull supersedes any lingering older intent.
            # This call ALSO auto-clears any stale resume red banner
            # (DEC-RESUME-BANNER-AUTOCLEAR-001): the banner told the operator
            # to re-launch manually, and this IS that launch.
            persist_intent_for_launch(
                pull_intent_store,
                dry_run=dry_run,
                symbols=symbols,
                symbol=symbol,
                from_date=from_date,
                to_date=to_date,
                restarts=intent_restarts,
            )
            on_finally = pull_intent_store.clear
        started = _pull_job.start(
            _work,
            total=len(targets),
            detail=pipeline_detail,
            ledger_synced=True,
            on_finally=on_finally,
        )
        return {
            "started": started,
            "targets": len(targets),
            "total": len(targets),  # the JS title reads body.total for the first render
            "tradable": len(tradable),
            "target_source": target_source,  # "selection" / "single" / "universe"
            "years": years,
            "dry_run": dry_run,
        }

    @app.post("/api/v1/universe/pull-pipeline")
    async def api_universe_pull_pipeline(
        symbols: str | None = Query(default=None),
        symbol: str | None = Query(default=None),
        from_date: str | None = Query(default=None),
        to_date: str | None = Query(default=None),
        dry_run: bool = Query(default=False),
    ) -> JSONResponse:
        """POST /api/v1/universe/pull-pipeline — the ONE-CLICK pull(+movers) pipeline.

        Thin delegator over :func:`_launch_pipeline` (which carries the full
        semantics docstring): the launch body is shared with the boot-time
        intent resume (DEC-PULL-INTENT-RESUME-001), so the operator's POST and
        a resumed pull can never drift onto two different launch paths.
        """
        return JSONResponse(
            _launch_pipeline(
                symbols=symbols,
                symbol=symbol,
                from_date=from_date,
                to_date=to_date,
                dry_run=dry_run,
            )
        )

    if pull_intent_store is not None:
        _intent_store: PullIntentStore = pull_intent_store

        def _resume_launch(intent: PullIntent) -> None:
            """Re-fire the persisted pipeline pull — the resume's launch binding.

            Always a REAL pull (``dry_run=False`` — a dry-run never persisted an
            intent) with the intent's already-bumped restart counter threaded
            through, so the re-persisted intent keeps the cap honest. A
            route-shaped validation refusal (``HTTPException`` — a stale
            selection that no longer resolves, a bad date, a credential 503) is
            normalised to ``ValueError``/``RuntimeError`` for the 100 %-tested
            resume core: a 4xx is DETERMINISTIC (the same intent can never
            launch → the core clears it), while a 5xx (credentials not yet
            fixed at boot) is transient (the core keeps the intent and re-tries
            next boot, bounded by the cap).
            """
            try:
                _launch_pipeline(
                    symbols=intent.symbols,
                    symbol=intent.symbol,
                    from_date=intent.from_date,
                    to_date=intent.to_date,
                    dry_run=False,
                    intent_restarts=intent.restarts,
                )
            except HTTPException as exc:
                if exc.status_code < _SERVER_ERROR_FLOOR:
                    raise ValueError(str(exc.detail)) from exc
                raise RuntimeError(str(exc.detail)) from exc

        def _resume_pipeline_from_intent() -> None:
            """Boot-time seam: run the intent auto-resume decision + dispatch.

            Exposed as ``app.state.brutex_resume_pipeline`` for the
            ``cli.cmd_dashboard`` startup launcher (the SAME daemon-thread seam
            family as the instrument autoload). Every load-bearing decision —
            single-flight probe, restart-cap refusal + CRITICAL alert + red
            banner, bump-before-launch, stale-intent clear — lives in the
            100 %-tested :func:`~brutex.dashboard.pull_intent.run_auto_resume`;
            this binding only supplies the real launcher. Never raises.
            """
            run_auto_resume(_intent_store, launch=_resume_launch)

        app.state.brutex_resume_pipeline = _resume_pipeline_from_intent

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
        dry_run: bool = Query(default=False),
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

        ``dry_run`` is EXPLICIT opt-in ONLY (DEC-PULL-LOUD-CREDENTIAL-REFUSAL-001):
        missing/broken Groww creds REFUSE the retry with a 503 relaying the
        CredentialsError fix text (never a silent synthetic downgrade);
        ``?dry_run=true`` runs the loudly-labeled synthetic pass.
        """
        dry_run = _gate_pull_dry_run(dry_run, route="universe.retry_failed")
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
        if dry_run:
            # Explicit synthetic run: the progress title must SAY so loudly
            # (DEC-PULL-LOUD-CREDENTIAL-REFUSAL-001 — never look like a real pull).
            detail = label_synthetic(detail)
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
                floor_store=instrument_store,
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
        """GET /api/v1/universe/pull-progress — the LIVE per-unit + per-phase pull snapshot.

        Returns the background job's O(1)
        :meth:`brutex.dashboard.pull_jobs.BackgroundPullJob.snapshot` — ``state``
        (idle/running/done/error), ``total`` / ``completed`` / ``failed`` /
        ``gaps`` / ``bars`` / ``percent`` / ``current`` / ``phase``. The page polls this
        while a pull runs to tick the counters window-by-window (DEC-PULL-PROGRESS-LIVE-001);
        the terminal-header builder (``terminalPullHeader``) reads ``gaps`` to
        distinguish a clean finish from a no-data-gap partial, so it is documented
        here as a first-class snapshot key (it is NOT dead code).

        DEC-UNIVERSE-PER-PHASE-PROGRESS-001: ADDITIONALLY carries a ``phases`` array —
        ONE row per window phase (spot / futures / options) with ``done`` / ``total`` /
        ``success`` / ``empty`` / ``failed`` / ``pending`` + a ``current`` flag — so the
        Universe pull section shows every phase's live windows count + progress
        CONSISTENTLY. The counts are read THROUGH the SAME generation-stamped
        ``_window_counts_cache`` the ``/coverage/windows`` page uses (NO new scan — a
        repeat poll within one ``(generation, ledger-mtime)`` is an O(1) hit), on the
        threadpool worker so the store's RLock stays on one thread
        (DEC-DASHBOARD-FASTREAD-001). None-safe: a dry-run / no store / no ledger yields
        ``phases: []`` (an honest empty list, never a fabricated window count). Every
        existing flat snapshot key is UNCHANGED.
        """
        from brutex.data.window_ledger_store import ledger_path

        snap = _pull_job.snapshot().to_dict()
        current_phase = str(snap.get("phase", ""))
        store: Any = instrument_store
        lake_root = getattr(store, "path", None)
        if store is None or not isinstance(lake_root, Path):
            snap["phases"] = []
            return JSONResponse(snap)
        # Resolve the ledger path HERE (not inside phase_progress_payload) so db_view
        # carries no `window_ledger_store` import edge — CodeQL py/cyclic-import.
        ledger_file = ledger_path(lake_root.parent)

        def _phases() -> list[dict[str, Any]]:
            """Read the cached per-segment window counts and project the phase rows."""
            return phase_progress_payload(
                store.connection,
                ledger_file,
                current_phase=current_phase,
                count_cache=_window_counts_cache,
                generation=_summary_cache.generation,
            )

        snap["phases"] = await run_in_threadpool(_phases)
        return JSONResponse(snap)

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

    @app.get("/api/v1/liveproof")
    async def api_liveproof() -> JSONResponse:
        """GET /api/v1/liveproof — the zero-command live-proof autorun status.

        DEC-DOCTOR-LIVE-AUTORUN-001: the data source for the persistent
        ``bx-liveproof.js`` status element on every dashboard page. Returns
        the injected :class:`~brutex.doctor_autorun.LiveProofAutorun`'s O(1)
        lock-guarded snapshot — ``enabled`` plus the ``preflight`` /
        ``probes`` blocks (``state`` + the last verdict with the kit's
        VERBATIM diagnostic lines), or ``{"present": false}`` when no autorun
        is wired (a bare/test app). Read-only: this route never starts a kit
        run (startup + the pull gate are the only triggers), so polling it
        costs nothing (Golden Rule #10 — display, not action).

        DEC-PULL-INTENT-RESUME-001: when a pull-intent store is wired AND its
        resume banner holds a payload (an over-cap refusal / failed resume
        launch), the payload rides along as a ``resume`` section —
        ``{"level": "critical", "message": …}`` — which the SAME site-wide
        ``bx-liveproof.js`` pill renders as the existing RED top banner (§11
        COMMON, no second banner mechanism). Absent banner ⇒ no ``resume`` key
        (the pre-existing payload stays byte-identical).
        """
        payload: dict[str, Any] = (
            {"present": False} if liveproof is None else {"present": True, **liveproof.status()}
        )
        if pull_intent_store is not None:
            resume = pull_intent_store.banner.snapshot()
            if resume is not None:
                payload["resume"] = resume
        return JSONResponse(payload)

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

    def _nav_latency_key(name: str, store: Any, *extra: object) -> str:
        """Build the COMPOSITE cache key for the nav-latency caches (DEC-DASHBOARD-NAV-LATENCY-001).

        Encodes the route name, the dashboard write-generation (``_summary_cache.generation``,
        bumped by every pull / materialize) AND the instrument-master content revision
        (:func:`master_revision`, moved by a keyless ``load-instruments`` that does NOT bump
        the generation) into ONE string key, plus any route-specific ``extra`` discriminants
        (e.g. the coverage ``years`` span). Folding BOTH freshness signals into the KEY — and
        passing ``generation=0`` to :meth:`MoversSnapshotCache.get_or_compute` — means a cache
        hit fires ONLY when neither the ledger NOR the master has changed since the entry was
        stored, so a pull or an instrument load always yields a fresh key (never a stale
        payload — Golden Rule #1). The token is cheap (a COUNT+MAX over the lean master table
        plus an O(1) generation read), strictly lighter than the projection it guards.
        """
        gen = _summary_cache.generation
        count, latest = master_revision(store)
        extra_repr = "|".join(repr(item) for item in extra)
        return f"{name}|gen={gen}|m={count}:{latest}|{extra_repr}"

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

        # Memoise behind the COMPOSITE (generation, master-revision) token
        # (DEC-DASHBOARD-NAV-LATENCY-001): a 4 s poll within one generation + master
        # revision is an O(1) handoff, while a pull (generation bump) OR an instrument
        # load (master-revision move) yields a new key → recompute, never stale. The
        # full O(datasets)+ledger build runs ONLY on a miss, on the threadpool worker.
        def _cached_build() -> dict[str, Any]:
            """Return the coverage payload, memoised behind the composite freshness key."""
            return _coverage_cache.get_or_compute(
                _nav_latency_key("coverage", store, years),
                generation=0,
                compute=_build,
            )

        return JSONResponse(await run_in_threadpool(_cached_build))

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
        follow-up). A session-density RANGE audit now feeds the
        ``/completeness`` amber ``settled_partial_range`` chip
        (DEC-SPOT-PARTIAL-RANGE-CHIP-001 — ``session_days`` rides this same
        SPOT scan); the PER-WINDOW trading-day gap audit and the options-side
        range audit remain follow-ups, NOT claimed. When no lake root
        is wired (a store opened without a filesystem path) the payload is an
        empty summary, never a 500.
        """
        store = _require_store()
        lake_root = getattr(store, "path", None)
        if not isinstance(lake_root, Path):
            # Same freshness-field shape as the real path (no cached entry ⇒
            # honest duty_degraded=False / snapshot_age_s=0.0).
            return JSONResponse(
                _summary_cache.stamp_freshness(SPOT, build_spot_coverage_payload([]))
            )
        lake = lake_root.parent

        def _recompute() -> list[dict[str, Any]]:
            """Recompute the spot-dataset summary (cache-miss / dirty path only)."""
            # The O(files) full-lake aggregate — runs ONLY on a cache miss / a
            # dirty read past the 2 s TTL floor, on a threadpool worker, and
            # builds + uses store.connection here so the store RLock stays on one
            # thread (DEC-DASHBOARD-FASTREAD-001). FNO-pruned + bounded to the
            # verdict intervals (DEC-COMPLETENESS-FNO-SKIP-001): a materialised
            # options lake holds ~10⁵-10⁶ contract datasets the spot verdicts
            # never read — the unfiltered scan blocked the first open for tens
            # of minutes.
            return summarize_spot_verdict_datasets(store.connection, lake)

        def _rows_and_rollup() -> dict[str, Any]:
            """Lake rows via SummaryCache + the ledger rollup PER REQUEST.

            The rollup deliberately lives OUTSIDE the SummaryCache (hostile C8):
            ledger flushes never bump that cache's generation, so fusing the
            join into ``_recompute`` would freeze the chips until the next LAKE
            write. ``spot_window_rollup`` carries its own mtime-keyed cache, so
            the per-request cost is O(datasets) between ledger flushes.
            """
            rows = _summary_cache.get_or_refresh(SPOT, recompute=_recompute)
            # Honest freshness stamp (DEC-COMPLETENESS-PULL-DUTY-DEGRADE-001):
            # duty_degraded + snapshot_age_s make a pull-time stale serve
            # VISIBLE to the operator — never silent (Golden Rule #1).
            return _summary_cache.stamp_freshness(
                SPOT, build_spot_coverage_payload(rows, spot_window_rollup(lake))
            )

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

    # Per-app fingerprint gate for the reconcile diagnostic (DEC-TELEMETRY-RECONCILE-
    # LEDGERSAVE-001): suppresses byte-identical repeats of the mismatch WARNING / the
    # counts-only not-yet-pulled INFO across scoreboard polls — a stable state logs once
    # per change, not once per build. Injected here (the production call site) so tests
    # control their own gate (or pass none = ungated).
    _reconcile_log_gate = ReconcileLogGate()

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
                SPOT, recompute=lambda: summarize_spot_verdict_datasets(store.connection, lake)
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
        # The per-app _reconcile_log_gate (DEC-TELEMETRY-RECONCILE-LEDGERSAVE-001) deduplicates
        # byte-identical repeats across polls, so a STABLE state logs once per change, not
        # once per scoreboard build (the 953x/night storm); the benign zero-stray partial-lake
        # shape is a counts-only INFO, never the WARNING.
        log_symbol_reconcile_from_board(spot_rows, categories, gate=_reconcile_log_gate)
        return spot_rows, categories, store.list_coverage()

    def _build_completeness_board(store: Any, lake: Path) -> dict[str, Any]:
        """Compose the aggregate + per-symbol scoreboard from the shared inputs.

        Stamped with the SPOT entry's freshness (duty_degraded / snapshot_age_s
        — DEC-COMPLETENESS-PULL-DUTY-DEGRADE-001): the scoreboard's spot rows
        ride the SAME cached SPOT scan, so a pull-time stale serve is visible
        here too, never silent.
        """
        spot_rows, categories, records = _completeness_inputs(store, lake)
        return _summary_cache.stamp_freshness(
            SPOT, build_completeness_scoreboard(spot_rows, categories, records, categories)
        )

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
            # Same freshness-field shape as the real path (an absent SPOT entry
            # honestly reads duty_degraded=False / snapshot_age_s=0.0).
            return _summary_cache.stamp_freshness(
                SPOT, build_completeness_scoreboard([], {}, [], {})
            )
        board: dict[str, Any] = await run_in_threadpool(
            _build_completeness_board, store, lake_root.parent
        )
        return board

    async def _completeness_csv() -> str:
        """Resolve the store + lake and build the downloadable CSV (empty + well-shaped if no lake)."""
        store = _require_store()
        lake_root = getattr(store, "path", None)
        if not isinstance(lake_root, Path):
            return scoreboard_to_csv(build_completeness_scoreboard([], {}, [], {}))
        csv_text: str = await run_in_threadpool(_build_completeness_csv, store, lake_root.parent)
        return csv_text

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
        lake = lake_root.parent

        # Memoise the per-symbol projection behind the COMPOSITE (generation,
        # master-revision) token (DEC-DASHBOARD-NAV-LATENCY-001). The inner SPOT lake
        # scan is ALREADY `_summary_cache`-cached (DEC-COMPLETENESS-O1-LATENCY-001), but
        # the residual `categorize_instruments` + `list_coverage` + `per_symbol_completeness`
        # projection re-ran on every 15 s poll; this caches the WHOLE map so a repeat poll
        # within one generation + master revision is an O(1) handoff. A pull (generation) or
        # an instrument load (master revision) yields a new key → recompute, never stale.
        def _cached_per_symbol() -> dict[str, dict[str, Any]]:
            """Return the per-symbol completeness map, memoised behind the composite freshness key."""
            return _universe_completeness_cache.get_or_compute(
                _nav_latency_key("universe-completeness", store),
                generation=0,
                compute=lambda: _build_per_symbol_completeness(store, lake),
            )

        per_symbol = await run_in_threadpool(_cached_per_symbol)
        return JSONResponse(per_symbol)

    def _build_mapping_integrity_report() -> MappingIntegrityReport:
        """Reconcile the live store's eligibility + coverage into one report (thin I/O glue).

        The SINGLE builder shared by the JSON route AND the CSV download (CLAUDE.md §11
        COMMON), so both surfaces serialise the identical verdict — never two drifting
        reconciliations. Gathers the two store reads (eligible underlyings + coverage ledger)
        off the event loop and delegates to the 100%-tested
        :func:`~brutex.data.mapping_integrity.reconcile_mapping_integrity`.

        The coverage ledger is read ONCE and projected two ways
        (DEC-MAPPING-INTEGRITY-ATTEMPTED-001): the covered-years map (cells with
        ``expiries_pulled > 0``) AND the attempted set (every underlying with ANY row — an
        expiry pull was actually run, even if it returned zero). Passing the attempted set
        lets the reconciler distinguish a real attempted-and-zero failure (M&M) from a benign
        never-pulled name (a selective pull), so the gate is no longer falsely RED. The
        eligible set is scoped to TRADABLE F&O underlyings (``tradable_only=True``), so the
        ~539 NIFTY-Total-Market REFERENCE_ONLY constituents (no option chain) never inflate
        the gate (DEC-MAPPING-INTEGRITY-ATTEMPTED-001).
        """
        store = _require_store()
        eligible = [
            EligibleSpot(exchange=exch.value, symbol=sym)
            for exch, sym in store.fno_eligible_underlyings(tradable_only=True)
        ]
        coverage = store.list_coverage()
        return reconcile_mapping_integrity(
            eligible_spots=eligible,
            covered_years=coverage_years_from_records(coverage),
            expected_years=_coverage_years(),
            attempted=attempted_pairs_from_records(coverage),
            whitelist=(),
            now=datetime.now(UTC),
        )

    def _build_mapping_integrity_report_cached() -> MappingIntegrityReport:
        """Cache the mapping-integrity report behind the COMPOSITE freshness token.

        DEC-DASHBOARD-NAV-LATENCY-001. The JSON route polls this every 15 s and the CSV
        download reuses the SAME builder; both run the two store reads
        (``fno_eligible_underlyings`` + ``list_coverage``) + the reconciliation on every
        call. The report is a pure function of the instrument master + the coverage ledger,
        so it is memoised behind the ``(generation, master-revision)`` key — a repeat poll
        within one generation + master revision is an O(1) handoff, while a pull OR an
        instrument load yields a new key → recompute (never stale — Golden Rule #1). The
        whitelist + ``now`` are constants here; ``now`` only stamps the report's clock, the
        verdict itself depends solely on the cached master + ledger, so a same-revision
        re-serve carrying the entry's ``now`` is byte-identical to recomputing (the entry is
        dropped the instant either signal moves).
        """
        return _mapping_integrity_cache.get_or_compute(
            _nav_latency_key("mapping-integrity", _require_store()),
            generation=0,
            compute=_build_mapping_integrity_report,
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
        report = await run_in_threadpool(_build_mapping_integrity_report_cached)
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
        report = await run_in_threadpool(_build_mapping_integrity_report_cached)
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

    def _read_parse_skips() -> dict[str, Any] | None:
        """Read the persisted parse-skip snapshot (``None`` if never loaded)."""
        raw = _require_store().get_meta(PARSE_SKIPS_META_KEY)
        if raw is None:
            return None
        parsed: dict[str, Any] = json.loads(raw)
        return parsed

    @app.get("/api/v1/universe/parse-skips")
    async def api_universe_parse_skips() -> JSONResponse:
        """GET /api/v1/universe/parse-skips — the green/red master parse-skip verdict.

        DEC-PARSE-SKIP-REPORT-001. Returns the bounded parse-skip snapshot the master
        load persisted to ``schema_meta``: the malformed in-scope rows the parser
        skipped-and-counted (DEC-INSTRUMENT-SKIP-NO-GSYM-001), the in-scope
        denominator, capped sample reasons, and the verdict — GREEN iff
        ``skipped_count <= max(10, ceil(0.1% of in-scope rows))``. READ-ONLY;
        ``{"present": false}`` until the master has been loaded once. No store → 503.
        """
        snapshot = await run_in_threadpool(_read_parse_skips)
        if snapshot is None:
            return JSONResponse({"present": False})
        return JSONResponse({"present": True, **snapshot})

    def _build_lake_census_payload() -> dict[str, Any]:
        """Scan the lake + fold the duplicate census JSON (DEC-LAKE-HEAL-001, thin I/O glue).

        The ONE builder behind the cached ``/api/v1/universe/lake-uniqueness`` route:
        runs the bounded invariant scanner (READ-ONLY / observe — the ROUTE never
        heals; healing belongs to the L2 pull seams + the L3 startup sweep), folds
        :func:`~brutex.data.lake_invariants.build_lake_census`, and stamps the heal
        mode + the ``.lake_heal`` stats/sentinel reads into the SAME dict shape
        ``brutex lake audit-unique --json`` prints. ``{"present": False}`` when no
        lake root can be resolved (mirrors the sibling panel routes' honest gate).
        """
        from brutex.data.lake_heal import (
            load_heal_stats,
            load_last_sweep,
            resolve_lake_heal_mode,
        )

        lake_db = getattr(_require_store(), "path", None)
        if not isinstance(lake_db, Path):
            return {"present": False}
        lake = lake_db.parent
        census = build_lake_census(scan_lake_invariants(lake))
        return census_to_dict(
            census,
            mode=resolve_lake_heal_mode(),
            stats=load_heal_stats(lake),
            last_sweep=load_last_sweep(lake),
        )

    def _build_lake_census_payload_cached() -> dict[str, Any]:
        """Memoise the lake census behind the ``SummaryCache`` adaptive TTL floor.

        DEC-LAKE-HEAL-001 (hostile-review MAJOR fold): the census rides the SAME
        ``_summary_cache.get_or_refresh`` path ``/coverage/spot`` uses — NOT a
        generation-KEYED memo. ``pull_jobs`` bumps the write generation on every
        per-pair write tick, so a generation-keyed cache re-ran this whole-lake
        O(files) scan on every 15 s chip poll during a pull (the #920 regression
        class); the ``SummaryCache`` instead serves the dirty entry until the
        ADAPTIVE floor — ``max(2 s, the last scan's own duration)`` capped 120 s
        (DEC-SUMMARY-CACHE-ADAPTIVE-FLOOR-001) — elapses, so an expensive scan
        self-throttles to a ≤50% duty cycle while an idle lake keeps the snappy
        2 s floor. Freshness: a pull, a materialize, OR a heal (the L3 sweeper's
        ``on_healed=summary_cache.mark_dirty``) dirties the entry → one fresh
        scan once past the floor; staleness is bounded by that floor, never
        indefinite (Golden Rule #1, stated honestly per Golden Rule #10). The
        payload dict is byte-identical to the uncached build — only the refresh
        SCHEDULING changed. (The single-payload dict rides the ``SummaryPayload``
        list shape as a one-element list.)
        """
        rows = _summary_cache.get_or_refresh(
            LAKE_UNIQUENESS, recompute=lambda: [_build_lake_census_payload()]
        )
        return rows[0]

    @app.get("/api/v1/universe/lake-uniqueness")
    async def api_universe_lake_uniqueness() -> JSONResponse:
        """GET /api/v1/universe/lake-uniqueness — the lake duplicate-census verdict.

        DEC-LAKE-HEAL-001: per-segment (INDEX/CASH/FNO/OPTIONS) clean vs
        dup/foreign/overlap/drift file counts + the ENGINE-index
        (NIFTY/BANKNIFTY/SENSEX) ``engine_clean`` verdict + the heal mode and
        ``.lake_heal`` stats/sentinel — the ``/universe`` chip's data source.
        READ-ONLY (never heals). Served through the ``SummaryCache`` adaptive
        TTL floor (the #920 machinery): a repeat poll is an O(1) handoff
        within the TTL window, and a scan's own cost sets its refresh floor
        (``max(2 s, scan duration)`` capped 120 s) — so a pull's write-tick
        generation storm can never amplify the 15 s poll into back-to-back
        whole-lake scans. HONEST: the refresh itself stays O(files); a dirty
        census may be served up to one floor window stale — bounded, never
        indefinite. No store wired → 503; no resolvable lake root →
        ``{"present": false}``.
        """
        _require_store()
        payload = await run_in_threadpool(_build_lake_census_payload_cached)
        return JSONResponse(payload)

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
        from brutex.dashboard.db_view import build_window_payload, last_pull_start_utc

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
                # Per-run honesty (DEC-TELEMETRY-SCOPE-BUDGET-RUNCOUNTS-001): the last
                # REAL pull's journal `started` instant — None (no journal / dry-run
                # only) OMITS the per-run figure entirely, never fabricates it.
                since=last_pull_start_utc(lake),
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
            # Same freshness-field shape as the real path (no cached entry ⇒
            # honest duty_degraded=False / snapshot_age_s=0.0).
            return JSONResponse(
                _summary_cache.stamp_freshness(OPTIONS, build_option_coverage_payload([]))
            )
        lake = lake_root.parent

        def _recompute() -> list[dict[str, Any]]:
            """Recompute the option-dataset summary (cache-miss / dirty path only)."""
            # The O(files) full-lake aggregate — runs ONLY on a cache miss / a
            # dirty read past the 2 s TTL floor, on a threadpool worker, building
            # + using store.connection here so the store RLock stays on one
            # thread (DEC-DASHBOARD-FASTREAD-001).
            return summarize_option_datasets(store.connection, lake)

        rows = await run_in_threadpool(_summary_cache.get_or_refresh, OPTIONS, recompute=_recompute)
        # Honest freshness stamp (DEC-COMPLETENESS-PULL-DUTY-DEGRADE-001) —
        # mirrors /coverage/spot: a pull-time stale serve is visible, never silent.
        return JSONResponse(
            _summary_cache.stamp_freshness(OPTIONS, build_option_coverage_payload(rows))
        )

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
        min_volume: int = Query(default=0, ge=0),
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
            # Movers PREBUILD GATE (DEC-PIPELINE-AUTOPREBUILD-001 chunk 2): while
            # the one-click pull pipeline runs, answer with the honest `gated: true`
            # payload and ZERO lake reads — a half-filled lake / mid-overwrite
            # derived partitions must never be ranked. The page keeps polling and
            # recovers by itself the moment the pipeline job releases the gate.
            # The snapshot ALSO consults the cross-process derive-lock probe
            # (DEC-DERIVE-LOCK-VISIBILITY-001 — a CLI-process derive gates too),
            # which is one flock syscall on the lake volume, so it runs on a
            # threadpool worker — never on the event loop (the app.py offload
            # doctrine, DEC-PULL-RATE-BRIDGE-NONBLOCK-CACHE-001).
            gate = await run_in_threadpool(_prebuild_gate.snapshot)
            if gate.blocking:
                return JSONResponse(
                    gated_movers_payload(
                        gate,
                        asset=asset,
                        interval=interval,
                        date=date,
                        time=time,
                        top_n=top_n,
                    )
                )
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
                            min_volume=min_volume,
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
                            min_volume=min_volume,
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
                        min_volume=min_volume,
                    )
                )
            lake_root = store_path.parent
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
                # Normalise the date so the candidate cache key is canonical. With NO
                # explicit date, default to the LATEST available spot day
                # (DEC-PULL-MOVERS-WARM-001), NOT today: a historical pull fills the lake
                # only to the previous trading day, so a today-default would show an EMPTY
                # snapshot on open (and miss the post-pull warm, which keys on that same
                # latest day). Falls back to today only when the lake has no spot data yet
                # (an honest empty payload). An EXPLICIT malformed date raises ValueError
                # here → caught by the outer `except ValueError` → 400. The latest-date
                # resolution is the O(datasets) `latest_movers_date` DuckDB MAX, so it runs
                # HERE on the threadpool worker (never the event loop) AND is memoised
                # behind the same write generation, so a repeat DEFAULT poll is an O(1)
                # handoff and a pull invalidates it (GAP #4).
                if date is None:
                    date_str = (
                        _latest_movers_date_cache.get_or_compute(
                            LATEST_MOVERS_DATE_KEY,
                            generation=generation,
                            compute=lambda: latest_movers_date(lake_root),
                        )
                        or parse_snapshot_date(None).isoformat()
                    )
                else:
                    date_str = parse_snapshot_date(date).isoformat()
                # Memoise the lake DISCOVERY once per write-generation (the proven
                # `/db` #896 pattern), shared across spot/futures/options, so a tab /
                # section / exchange / expiry switch within one generation REUSES a
                # single `bars/` walk instead of re-walking the whole lake inside each
                # compute on every cache miss (DEC-MOVERS-DISCOVERY-MEMO-001). Computed
                # on the worker thread; a pull bumps `generation` → one fresh walk.
                movers_datasets = _db_datasets_cache.get_or_compute(
                    MOVERS_DATASETS_KEY,
                    generation=generation,
                    compute=lambda: list_candle_datasets(lake_root),
                )
                if asset == "options":
                    # Options carries the extra Near/Next/Far expiry tier (blank ⇒
                    # near) + the All/Monthly/Weekly cadence track (blank ⇒ all),
                    # normalised so the key and the computed set agree.
                    tier, trk = normalize_options_selectors(expiry, track)
                    # The AUTHORITATIVE vendor expiry calendar (DEC-MOVERS-CALENDAR-TIERS-001):
                    # the Near/Next/Far + Weekly/Monthly tier classification reads the FULL
                    # calendar from registry.duckdb (not the sparse lake-partition set), so a
                    # sparsely-pulled lake no longer mislabels a tier. Memoised once per write
                    # generation (a metadata pull bumps it → re-read); `None` when the registry
                    # is absent/empty → the lake-partition fallback (a CLI-only lake stays correct).
                    calendar_expiries = _options_calendar_cache.get_or_compute(
                        f"options_calendar|{exch or ''}",
                        generation=generation,
                        compute=lambda: load_registry_expiries(
                            registry_path_for(store_path), exchange=exch
                        ),
                    )
                    # The AUTHORITATIVE strike chains (DEC-MONEYNESS-REGISTRY-STEP-001):
                    # the ATM±N grid step per (underlying, expiry) reads the FULL vendor
                    # chain from registry.duckdb (not the sparse lake ladder) via a LAZY
                    # per-(underlying, expiry) loader — one NARROWED DuckDB read per
                    # tier-target key, memoised per key + write generation, NEVER the
                    # whole monotonically-growing contracts table held resident
                    # (Golden Rule #5 bounded reads — DEC-FNO-HONESTY-PACK-001; the
                    # /db `_registry_strike_chain` shape). A per-key `None` (absent /
                    # empty registry) → the lake-ladder fallback with its honest
                    # low-confidence label.
                    registry_chain_for = make_registry_chain_loader(
                        registry_path_for(store_path),
                        exchange=exch,
                        cache=_options_strikes_cache,
                        generation=generation,
                    )
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
                            calendar_expiries=calendar_expiries,
                            registry_chain_for=registry_chain_for,
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
                        min_volume=min_volume,
                        calendar_only_expiry=opt_cands.calendar_only_expiry,
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
                        min_volume=min_volume,
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
                    # prefer_resample while a pull runs (DEC-MOVERS-GATE-PREBUILD-SCOPE-001):
                    # the gate now covers ONLY the prebuild phase, so a mid-FETCH movers
                    # read is served — but the stored TF partitions are rewritten only by
                    # the final prebuild, so mid-fetch they can predate the freshly-landing
                    # 1-minute bars. While `_pull_live()` reads True the spot intraday read
                    # takes the always-fresh 1-minute resample (byte-equal semantics per the
                    # pinned stored≡resample equality, just slower); no pull → stored-first,
                    # byte-identical. Evaluated inside the compute (scan-time truth).
                    compute=lambda: compute_spot_candidates(
                        lake_root=lake_root,
                        interval=interval,
                        date=date_str,
                        time=time,
                        exchange=exch,
                        datasets=movers_datasets,
                        prefer_resample=_pull_live(),
                    ),
                )
                return build_snapshot_payload(
                    spot_cands.snapshots,
                    interval=interval,
                    date=spot_cands.date,
                    time=time,
                    top_n=top_n,
                    skipped=spot_cands.skipped,
                    min_volume=min_volume,
                    stored_reads=spot_cands.stored_reads,
                    resampled_reads=spot_cands.resampled_reads,
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

        DEC-DASHBOARD-EVENTLOOP-OFFLOAD-001: the dataset discovery
        (``resolve_report_datasets`` → ``list_candle_datasets`` — a full
        ``bars/`` tree walk, ~66k dirs on a materialised options lake) + the
        store reads run on a threadpool worker, NEVER the event loop.
        Forensically proven (2026-07-04 run): this route ran the walk
        SYNCHRONOUSLY on the uvicorn loop during the prebuild write storm and
        froze EVERY dashboard request for 6 minutes (361,596 ms; 38 queued
        requests burst-flushed on release). The store connection is built +
        used inside the worker so its RLock stays on one thread
        (DEC-DASHBOARD-FASTREAD-001).
        """
        report_basis = _report_basis_literal(basis)
        store = _require_store()
        lake_root = getattr(store, "path", None)
        if not isinstance(lake_root, Path):
            return JSONResponse({"symbols": []})
        lake = lake_root.parent

        def _resolve() -> dict[str, Any]:
            """Walk the lake + tag pullability off the event loop (threadpool)."""
            triples = resolve_report_datasets(lake, basis=report_basis)
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
            return {
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

        return JSONResponse(await run_in_threadpool(_resolve))

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
        # The workbook serialisation is CPU-bound over every report row — off the
        # event loop like the row build (DEC-DASHBOARD-EVENTLOOP-OFFLOAD-001).
        return Response(
            content=await run_in_threadpool(render_xlsx, rows, title=title),
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
        # One xlsx per year + zip packing is CPU-bound over every report row —
        # off the event loop like the row build (DEC-DASHBOARD-EVENTLOOP-OFFLOAD-001).
        blob = await run_in_threadpool(
            render_report_zip, rows, mode=report_mode, min_pct=Decimal(str(min_pct))
        )
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

    @app.get("/results", response_class=HTMLResponse)
    async def results_page() -> str:
        """GET /results — the brute-force sweep leaderboard page (DEC-DASHBOARD-RESULTS-PAGE-001).

        A read-only view of the ranked sweep winners — the combos the brute-force
        engine found, decoded to their matched conditions, with net P&L (₹) and the
        full-precision Sharpe / profit-factor / win-rate. Mirrors the ``/universe``
        page style + nav; fetches ``/api/v1/sweep/leaderboard`` on open + on every
        filter change / manual Refresh, sorts any column client-side, and shows an
        honest empty-state when no sweep has been run yet.
        """
        return _render_results_html()

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
        the ``INDEX`` segment over ``2020-01-01 → today``. A second click while a
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

        # On a SUCCESSFUL sweep, bump the dashboard write-generation so the next
        # /results poll re-reads the new winners instead of serving the stale
        # leaderboard cache (the sweep _work never reports per-bar progress, so the
        # _on_progress mark_dirty funnel never fires for a sweep — MAJOR-1 fix,
        # DEC-DASHBOARD-RESULTS-PAGE-001). Fires only on success, never on a failed
        # sweep (see BackgroundPullJob._run).
        started = _pull_job.start(
            _work,
            total=1,
            detail=f"sweep {request.symbols}",
            on_success=_summary_cache.mark_dirty,
        )
        payload = request.to_dict()
        payload["started"] = started
        return JSONResponse(payload)

    @app.get("/api/v1/sweep/leaderboard")
    async def api_sweep_leaderboard(
        instrument: str | None = Query(default=None),
        tf: str | None = Query(default=None),
        direction: str | None = Query(default=None),
        rank_by: str | None = Query(default=None),
        top: int = Query(
            default=SWEEP_LEADERBOARD_DEFAULT_TOP_N, ge=1, le=SWEEP_LEADERBOARD_MAX_TOP_N
        ),
    ) -> JSONResponse:
        """GET /api/v1/sweep/leaderboard — the ranked brute-force sweep winners (DEC-DASHBOARD-RESULTS-PAGE-001).

        The data source for the ``/results`` page: discovers every per-tuple
        Parquet store under ``<lake>/sweeps``
        (:func:`brutex.dashboard.sweep_runner.default_results_root`), loads them via
        the SAME :class:`~brutex.engine.results_store.ResultsStore` the sweep wrote,
        applies the optional filters, ranks to the top-N, and projects each winning
        combo into a display row (rank, decoded matched conditions, net P&L in ``₹``,
        and the full-precision Sharpe / profit-factor / win-rate — NEVER rounded).

        READ-ONLY (CLAUDE.md §3): ranks results the operator already produced; it
        NEVER touches the brute-force engine or the 52-condition vocabulary (the
        ENGINE stays NIFTY/BANKNIFTY/SENSEX).

        Query parameters
        ----------------
        instrument, tf, direction
            Optional case-insensitive exact-match filters (e.g. ``NIFTY`` / ``5m`` /
            ``bull``); blank ⇒ no filter on that axis.
        rank_by
            The ranking column — one of
            :data:`brutex.dashboard.sweep_results_view.RANKABLE_COLUMNS`; an unknown
            value falls back to the default headline metric (``sharpe_ratio``),
            never a 500.
        top
            Leaderboard depth (1..500; default 50).

        **Latency (DEC-DASHBOARD-RESULTS-PAGE-001):** the ``<lake>/sweeps`` read is
        a pure function of the sweep tree, so the projected leaderboard is memoised
        behind the dashboard write-generation (``_summary_cache.generation``). It is
        bumped by a pull (the write funnel's ``mark_dirty``) AND by a
        SUCCESSFULLY-completed dashboard sweep (the ``on_success`` hook on
        ``/api/v1/sweep/run`` — a failed sweep never bumps it), so a sweep's winners
        appear on the next poll instead of behind a stale ``available: false``. A
        repeat poll, or a filter the operator already viewed, is an O(1) cache hit;
        the first read of a coordinate (or the first after a bump) still pays the
        real ``O(stores × rows)`` scan (honest envelope, Golden Rule #10).

        **Honest envelope (Golden Rule #10):** an empty / absent ``<lake>/sweeps``
        (no sweep run yet) returns ``available: false`` with zero rows — never a
        fabricated winner; a corrupt per-tuple Parquet is SKIPPED + counted, never a
        500. When no lake root is wired the payload is the same honest empty shape.
        """
        store = _require_store()
        store_path = getattr(store, "path", None)
        if not isinstance(store_path, Path):
            # No lake root wired → honest empty leaderboard (never a 500), mirroring
            # the /api/v1/movers no-lake-root degrade.
            empty = build_sweep_leaderboard(
                Path("/nonexistent-lake"),
                instrument=instrument,
                tf=tf,
                direction=direction,
                rank_by=rank_by,
                top_n=top,
            )
            return JSONResponse(empty.to_dict())
        lake_root = store_path.parent
        generation = _summary_cache.generation
        # Casefold the filter axes in the cache key: build_sweep_leaderboard's
        # _matches filters them case-insensitively, so ``NIFTY`` and ``nifty`` yield
        # the SAME leaderboard and must share ONE cache slot (rank_by stays raw — it
        # is matched exactly against RANKABLE_COLUMNS, not casefolded).
        key = (
            f"sweep_leaderboard|i={(instrument or '').casefold()}|tf={(tf or '').casefold()}"
            f"|d={(direction or '').casefold()}|r={rank_by or ''}|n={top}"
        )

        def _resolve() -> dict[str, Any]:
            """Cache-or-compute the projected leaderboard on a threadpool worker.

            The ``compute=`` closure performs the real ``<lake>/sweeps`` scan ONLY on
            a miss / post-write; a hit is an O(1) handoff (DEC-DASHBOARD-RESULTS-PAGE-001).
            """
            return _sweep_leaderboard_cache.get_or_compute(
                key,
                generation=generation,
                compute=lambda: build_sweep_leaderboard(
                    lake_root,
                    instrument=instrument,
                    tf=tf,
                    direction=direction,
                    rank_by=rank_by,
                    top_n=top,
                ).to_dict(),
            )

        return JSONResponse(await run_in_threadpool(_resolve))

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
        # A bounded read, but still DISK I/O — on a stalled/contended volume a
        # synchronous tail would wedge the event loop with every other request
        # behind it, so it rides the same threadpool offload as the lake reads
        # (DEC-DASHBOARD-EVENTLOOP-OFFLOAD-001).
        records = await run_in_threadpool(tail_records, path, max_lines=lines)
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
            The directory walks ride the same worker, off the event loop. The
            payload SHAPE is the ONE shared builder
            (:func:`~brutex.dashboard.discovery_warm.build_db_datasets_payload`
            — the post-pipeline discovery warm seeds this exact cache key with
            the byte-identical shape, DEC-DB-DISCOVERY-ADAPTIVE-FLOOR-001).
            """
            symbol_for_id = {
                str(i.security_id): i.symbol_name for i in store.list_index_equity_instruments()
            }
            return build_db_datasets_payload(lake, symbol_for_id=symbol_for_id)

        # DEC-DB-TAB-SWITCH-O1-001: the three O(files) dir-walks above are invariant
        # between lake writes, so memoise the discovered lists behind the shared write
        # generation — a tab switch within one generation is an O(1) cached handoff, a
        # pull bumps the generation → rediscover. The build still runs on the threadpool
        # worker (the store read + dir-walks stay off the event loop) on a cache miss.
        gen = _summary_cache.generation
        return JSONResponse(
            await run_in_threadpool(
                lambda: _db_datasets_cache.get_or_compute(
                    DB_DATASETS_KEY, generation=gen, compute=_datasets
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
        index: FnoCascadeIndex = await run_in_threadpool(
            lambda: _fno_cascade_cache.get_or_compute(
                FNO_CASCADE_KEY,
                generation=gen,
                compute=lambda: build_fno_cascade(list_fno_candle_datasets(lake)),
            )
        )
        return index

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

    def _fno_listed_by_expiry(underlying: str, expiries: list[str]) -> dict[str, int] | None:
        """Vendor-LISTED contract count per expiry for *underlying*, or ``None``.

        Feeds the ``/db`` F&O cascade "listed N with data M" chip
        (DEC-DB-UX-PACK-001 Feature 6): reads the authoritative ``registry.duckdb``
        calendar (the vendor ``get_contracts`` leg-set persisted by the metadata
        phase). Returns ``None`` — an honest "unknown, show with-data only" — when the
        registry file is absent, the registry does not know this underlying (a
        form/alias mismatch → never a misleading "listed 0"), or the registry is
        corrupt (``RegistryError``). Called ONLY with a non-``None`` cascade index in
        hand, so ``store.path`` is a real lake ``Path`` (verified by
        :func:`_fno_cascade_or_none`) and each *expiry* is ISO by construction
        (``FnoCascadeIndex.expiries``). Runs off the event loop.
        """
        registry_path = registry_path_for(_require_store().path)
        if not registry_path.exists():
            return None
        try:
            with RegistryStore.open(registry_path) as registry:
                # The registry must KNOW this underlying, else its per-expiry
                # count(*) is a misleading 0 (Golden Rule #1) — treat as unknown.
                if registry.count_expiries(underlying=underlying) == 0:
                    return None
                return {
                    expiry: registry.count_contracts(
                        underlying=underlying, expiry_date=date.fromisoformat(expiry)
                    )
                    for expiry in expiries
                }
        except RegistryError:
            return None

    @app.get("/api/v1/db/fno/expiries")
    async def api_db_fno_expiries(underlying: str = Query(...)) -> JSONResponse:
        """GET /api/v1/db/fno/expiries?underlying=NIFTY — level-2 picker, scoped to one underlying.

        The ISO ``YYYY-MM-DD`` expiries for *underlying* (ascending). Empty list for
        an unknown underlying (never a 500). O(1) cached read. ``expiry_counts`` pairs
        each expiry's vendor-LISTED count (``registry.duckdb``) with its WITH-DATA
        count (contracts that truly have lake candles), so the cascade renders a
        self-explanatory "listed N with data M" chip (DEC-DB-UX-PACK-001 Feature 6);
        ``listed`` is ``null`` when the registry is absent / does not know the
        underlying (honest "with data M" only).
        """
        idx = await _fno_cascade_or_none()
        if idx is None:
            return JSONResponse({"underlying": underlying, "expiries": [], "expiry_counts": []})
        expiries = list(idx.expiries(underlying))
        listed_by_expiry = await run_in_threadpool(
            lambda: _fno_listed_by_expiry(underlying, expiries)
        )
        return JSONResponse(
            {
                "underlying": underlying,
                "expiries": expiries,
                "expiry_counts": build_expiry_counts(idx, underlying, listed_by_expiry),
            }
        )

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
            """Build the spot symbol→interval cascade from the non-F&O lake datasets."""
            return build_spot_cascade(filter_spot_datasets(list_candle_datasets(lake)))

        index: SpotCascadeIndex = await run_in_threadpool(
            lambda: _spot_cascade_cache.get_or_compute(
                SPOT_CASCADE_KEY, generation=gen, compute=_build
            )
        )
        return index

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
        # NOTE: `store.path` is the instruments.duckdb FILE path (the lean master);
        # the lake root is its parent dir — named accordingly (readability, not a bug).
        store_path = getattr(store, "path", None)
        if not isinstance(store_path, Path):
            raise HTTPException(status_code=400, detail="no lake root wired for candle reads")
        lake = store_path.parent
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
                    # The AUTHORITATIVE registry strike chain for the F&O ATM±N grid
                    # (DEC-MONEYNESS-REGISTRY-STEP-001) — memoised behind the SAME
                    # count cache + generation; absent registry ⇒ the lake-observed
                    # step with its honest "observed" low-confidence label.
                    registry_path=registry_path_for(store_path),
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
        fetches / deduped net-new bars). READ-ONLY:
        loads ``<lake>/window_ledger/ledger.parquet``; never raises (empty
        ``{columns, rows, total}`` when no lake / no pull yet, never a 500). Shape
        matches the DuckDB-table grid so the /db viewer renders it identically.

        The response is now ONE ``LIMIT``/``OFFSET`` page with a real ``total``
        (DEC-DB-VIEWER-PAGINATE-ALL-001) so the /db viewer renders O(visible), not
        every window. Honest envelope (Golden Rule #10,
        DEC-SUSPICION-SWEEP-FIXES-001 D3): the ledger is NOT small — a pre-planned
        full-universe options pull holds ~841k rows (the measured shape behind
        ``db_view.WINDOW_ROWS_CAP``), and this route previously re-PARSED the whole
        Parquet (``load_window_ledger(...).to_rows()``) on EVERY paging click. The
        read now rides :func:`~brutex.dashboard.db_view.window_ledger_rows_cached`
        — the SAME mtime-keyed single parse the other ledger readers share — so a
        paging click within one ledger generation is an O(1) cache handoff + an
        O(page) slice/format; the FIRST read after a ledger flush still pays one
        real O(rows) parse (true O(page) needs a write-time manifest, NOT claimed).
        The page-slice preserves the canonical
        :meth:`~brutex.data.window_ledger.WindowLedger.to_rows` sort + derived
        columns byte-for-byte (ONE row-shape source:
        ``window_ledger.ledger_record_row``).
        """
        from brutex.dashboard.db_view import window_ledger_rows_cached

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
            "bars_net_new",
            "empty_retries",
            "transient_retries",
        ]
        store = _require_store()
        lake_root = getattr(store, "path", None)
        if not isinstance(lake_root, Path):
            return JSONResponse(
                {"columns": columns, "rows": [], "total": 0, "limit": limit, "offset": offset}
            )
        lake = lake_root.parent

        def _load() -> dict[str, Any]:
            """Read the cached ledger rows, then slice + IST-format ONLY the page."""
            from brutex.data.session import format_ist_clock

            all_rows = window_ledger_rows_cached(lake)
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
                    count_cache=_candle_count_cache,
                    generation=_summary_cache.generation,
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

        try:
            return JSONResponse(await run_in_threadpool(_load))
        except RegistryError:
            # A corrupt / truncated / version-incompatible registry.duckdb makes
            # RegistryStore.open raise RegistryError (a BruteXError, NOT a ValueError):
            # degrade to an honest 200 empty-state instead of a misleading HTTP 500
            # (DEC-DASHBOARD-EMPTY-STATE-001). Missing/empty/valid paths are unaffected
            # — only the EXISTING-but-unreadable file reaches here.
            return JSONResponse(_registry_unreadable_payload(REGISTRY_EXPIRY_COLUMNS))

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
        except RegistryError:
            # Corrupt / unreadable registry.duckdb → honest 200 empty-state, mirroring
            # the expiries route (DEC-DASHBOARD-EMPTY-STATE-001). A malformed expiry_date
            # is parsed BEFORE the registry is opened, so it still raises ValueError →
            # the 400 below; the two error contracts never collide.
            return JSONResponse(_registry_unreadable_payload(REGISTRY_CONTRACT_COLUMNS))
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


def _metadata_progress_sync(
    job: BackgroundPullJob, completed: int, total: int, current: str
) -> None:
    """Map the metadata pull's absolute ``(completed, total, current)`` progress onto the
    job's ``sync_from_ledger`` resync (DEC-METADATA-LIVE-PROGRESS-001).

    ``bars=None`` / ``failed=0``: the metadata pull persists only the chain SHAPE, never
    candles (DEC-INSTRUMENT-UNIVERSE-FAST-001) — there are no bars, so the sync PRESERVES
    the cumulative bar tally an earlier phase already streamed instead of wiping it to 0
    (DEC-PIPELINE-STALL-HEARTBEAT-001, the frozen-counter fix). Lifted OUT of the
    ``# pragma: no cover`` job-thread closure so the ``(completed, total, current)`` →
    ``sync_from_ledger`` mapping is unit-tested (a swapped arg / wrong ``bars`` would now
    fail a test, not pass CI green) instead of being pragma-hidden.
    """
    job.sync_from_ledger(completed=completed, total=total, bars=None, failed=0, current=current)


def _lake_revision_probe_or_none(instrument_store: Any) -> Callable[[], str | None] | None:
    """Build the cross-process lake-revision probe for the summary cache, or ``None``.

    DEC-HARDENING-LEFTOVERS-001. The lake root is derived the same way every
    other route does it — the wired :class:`~brutex.data.instrument_store.InstrumentStore`'s
    on-disk parent directory. When no store (or a path-less store) is wired
    there is no lake to watch, so ``None`` disables the probe and the
    :class:`~brutex.dashboard.summary_cache.SummaryCache` behaves byte-identically
    to the pre-probe cache (the tests/legacy contract).

    The returned probe is a zero-arg closure over
    :func:`brutex.dashboard.lake_revision.lake_revision` — one O(1) ``os.stat``
    of the window-ledger Parquet plus the bounded O(dirs) sweeps-tree digest
    (DEC-RESULTS-SURFACE-HONESTY-001; instruments × timeframes directory stats,
    never O(files)) per call, never raising (every ``OSError`` maps to ``None``
    = "no signal" inside ``lake_revision``).
    """
    store_path = getattr(instrument_store, "path", None)
    if not isinstance(store_path, Path):
        return None
    lake_root = store_path.parent

    def _probe() -> str | None:
        """The composite ledger-stat + sweeps-digest token for the wired lake root."""
        return lake_revision(lake_root)

    return _probe


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


def _rate_bridge_lake_root_or_none(instrument_store: Any) -> Path | None:
    """Resolve the lake root for the cross-process rate bridge, or ``None`` (never raises).

    DEC-CLI-RATE-BRIDGE-001. The ``/api/v1/pull/rate`` route reads the CLI bridge file at
    ``<lake_root>/.rate/pull_rate.json``. Unlike :func:`_resolve_pull_lake_root` (which
    RAISES on the live pull path when no store is wired — a pull MUST have a sink), this
    read-side resolver returns ``None`` when no lake root can be derived, so the rate poll
    simply falls back to the in-memory panel rather than 500-ing when the dashboard runs
    without a wired instrument store. O(1) — a single ``getattr``.

    Parameters
    ----------
    instrument_store
        The wired :class:`~brutex.data.instrument_store.InstrumentStore`, or ``None``.

    Returns
    -------
    Path | None
        The lake root (the store path's parent), or ``None`` when unresolvable.
    """
    store_path = getattr(instrument_store, "path", None)
    if isinstance(store_path, Path):
        return store_path.parent
    return None


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


_RESULTS_TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "results.html"
_RESULTS_TEMPLATE_CACHE: str | None = None


def _load_results_template() -> str:
    """Load + cache the sweep-leaderboard page template (DEC-DASHBOARD-RESULTS-PAGE-001).

    Ships as ``brutex/dashboard/templates/results.html``. The page fetches
    ``/api/v1/sweep/leaderboard`` on open + on every filter change / manual Refresh
    and renders the ranked sweep winners as a click-to-sort table with an honest
    empty-state. Loaded once, then served verbatim.
    """
    global _RESULTS_TEMPLATE_CACHE  # noqa: PLW0603 — single-load module cache
    if _RESULTS_TEMPLATE_CACHE is None:
        _RESULTS_TEMPLATE_CACHE = _RESULTS_TEMPLATE_PATH.read_text(encoding="utf-8")
    return _RESULTS_TEMPLATE_CACHE


def _render_results_html() -> str:
    """Return the brute-force sweep leaderboard page HTML (DEC-DASHBOARD-RESULTS-PAGE-001)."""
    return _load_results_template()


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
        # Query EVERY F&O underlying form (alias / normalized / raw spot spelling) so an
        # operator-typed spot name (NIFTYJR / BAJAJ-AUTO) matches the registry rows the
        # metadata pull persisted under the PRIMARY form (NIFTYNXT50 / BAJAJAUTO), while
        # a legacy raw-key row still matches too — DEC-PULL-FORENSIC-WAVE2-001. A
        # canonical symbol collapses to one form (byte-identical query). De-dup by
        # contract_symbol in case one contract was persisted under two key eras.
        rows: list[ContractRow] = []
        seen: set[str] = set()
        for form in fno_underlying_forms(underlying, aliases=resolve_fno_index_aliases()):
            for row in registry.list_contracts(underlying=form, expiry_date=expiry_date):
                if row.contract_symbol not in seen:
                    seen.add(row.contract_symbol)
                    rows.append(row)
        return rows
    if kind is not None:
        # Membership over EVERY form of each selected spot name (superset — the primary
        # write key, the normalized bridge AND the raw spelling), so aliased/hyphenated
        # underlyings match their persisted registry key — DEC-PULL-FORENSIC-WAVE2-001.
        aliases = resolve_fno_index_aliases()
        names = {
            form
            for inst in select_kind(kind)
            for form in fno_underlying_forms(inst.symbol_name, aliases=aliases)
        }
        return [row for row in registry.list_contracts() if row.underlying in names]
    if all_cached:
        return registry.list_contracts()
    return []


_DB_TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "db.html"


def _load_db_template() -> str:
    """Read the read-only DuckDB browser page template **fresh** each call.

    Ships as a package-relative resource at
    ``brutex/dashboard/templates/db.html`` (DEC-DB-VIEWER-001). The
    template fetches ``/api/v1/db/tables`` for the table list + row counts,
    then ``/api/v1/db/columns/{name}`` + ``/api/v1/db/query/{name}`` when a
    table is clicked, escaping every column name + cell via an ``esc()``
    helper before it is inserted into the DOM (XSS — values originate from the
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
    // Build rows via createElement + textContent (NO markup parsing) so server-derived
    // instrument values can never be interpreted as HTML — XSS-safe by construction.
    tb.replaceChildren();
    rows.forEach(i => {
      const tr = document.createElement('tr');
      [
        i.symbol_name, i.instrument, i.segment,
        i.underlying_symbol || '-', i.sm_expiry_date || '-',
        i.strike_price || '-', i.lot_size || '-', i.sm_freeze_qty || '-'
      ].forEach(val => {
        const td = document.createElement('td');
        td.textContent = (val == null ? '' : String(val));
        tr.appendChild(td);
      });
      tb.appendChild(tr);
    });
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

  phasesEl.replaceChildren();
  (s.phases || []).forEach(p => {
    // Build the phase card via createElement + textContent (NO markup parsing) so
    // server-derived phase/status/message text can never be interpreted as HTML (XSS-safe).
    const div = document.createElement('div');
    div.className = 'card phase-card';
    const h3 = document.createElement('h3');
    h3.textContent = (p.phase == null ? '' : String(p.phase)) + ' ';
    const badge = document.createElement('span');
    badge.className = 'badge ' + (p.status || '');
    badge.textContent = (p.status == null ? '' : String(p.status));
    h3.appendChild(badge);
    const bar = document.createElement('div');
    bar.className = 'bar';
    const fill = document.createElement('div');
    fill.style.width = (Number(p.percent) || 0) + '%';
    bar.appendChild(fill);
    const stats = document.createElement('div');
    stats.className = 'stats';
    let line = p.done + '/' + p.total + ' (' + p.percent + '%) · '
      + 'ok: ' + p.success + ' · no_data: ' + p.no_data
      + ' · quarantined: ' + p.quarantined;
    if (p.elapsed_seconds) { line += ' · ' + p.elapsed_seconds.toFixed(1) + 's'; }
    stats.textContent = line;
    if (p.message) {
      stats.appendChild(document.createElement('br'));
      const msg = document.createElement('span');
      msg.style.color = '#ef4444';
      msg.textContent = String(p.message);
      stats.appendChild(msg);
    }
    div.appendChild(h3);
    div.appendChild(bar);
    div.appendChild(stats);
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

  // Build the workers + events rows via createElement + textContent (NO markup parsing),
  // so server-derived worker/task/event/message text can never be interpreted as HTML.
  const mkRow = (cells, lastClass) => {
    const tr = document.createElement('tr');
    cells.forEach((val, idx) => {
      const td = document.createElement('td');
      if (lastClass && idx === cells.length - 1) { td.className = lastClass; }
      td.textContent = (val == null ? '' : String(val));
      tr.appendChild(td);
    });
    return tr;
  };

  const wtb = document.querySelector('#workers tbody');
  wtb.replaceChildren();
  (s.workers || []).forEach(w => {
    wtb.appendChild(mkRow([
      w.worker_id, w.status, w.current_task || '-',
      w.elapsed_seconds.toFixed(1) + 's'
    ]));
  });

  const etb = document.querySelector('#events tbody');
  etb.replaceChildren();
  (s.recent_events || []).slice(-20).reverse().forEach(e => {
    etb.appendChild(mkRow([
      e.at.split('T')[1].slice(0,8), e.phase || '-', e.kind,
      e.message || e.item_id || ''
    ], 'stats'));
  });
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
    "DEFAULT_ALLOWED_HOSTS",
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
