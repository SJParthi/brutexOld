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
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from contextlib import nullcontext
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import structlog

from brutex.adapters.dhan.constants import (
    DHAN_FLOOR_MARGIN_DAYS,
    DHAN_SPOT_INDEX_SYMBOLS,
    dhan_history_floor,
)
from brutex.adapters.dhan.live_controller import build_dhan_live_controller
from brutex.adapters.dhan.rolling_option_plan import DHAN_ROLLING_PLAN_KEY_COUNT
from brutex.adapters.groww.expiry_scope import normalize_expiry_scope
from brutex.adapters.groww.instruments import PARSE_SKIPS_META_KEY
from brutex.adapters.groww.symbol_form_preference import PersistentSymbolFormPreference
from brutex.adapters.ingest_common import (
    gate_pull_credentials,
    label_synthetic,
    verdict_message,
)
from brutex.calendars.auto_pull_gate import compose_market_closed
from brutex.calendars.market_hours import resolve_market_closed
from brutex.calendars.nse import now_ist
from brutex.config.credentials import groww_credentials_error
from brutex.config.paths import resolve_lake_root
from brutex.dashboard import oneclick_artifacts, oneclick_view
from brutex.dashboard.auto_pull_scheduler import AutoLaunchResult, AutoPullScheduler
from brutex.dashboard.completeness import (
    ReconcileLogGate,
    build_completeness_scoreboard,
    categorize_instruments,
    log_symbol_reconcile_from_board,
    per_symbol_completeness,
    scoreboard_to_csv,
)
from brutex.dashboard.composite_join import (
    CompositeIndex,
    composite_index_revision,
    load_composite_index,
)
from brutex.dashboard.composite_pull import (
    CompositePullReport,
    # The ONE index/stock classification-map builder the six-step F&O order keys on
    # (DEC-PULL-SIX-STEP-ORDER-001; CLAUDE.md §11 COMMON — the friend import is the
    # established convention, pyproject reportPrivateUsage="warning" + per-site ignore
    # at deliberate cross-module reuse sites — DEC-P0-017).
    _underlying_kind_map,  # pyright: ignore[reportPrivateUsage]
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
    make_window_payload_caches,
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
from brutex.dashboard.dhan_pull import (
    DhanPullReport,
    DhanPullRequest,
    DhanSpotPullFn,
    default_dhan_spot_pull,
    dhan_credentials_error,
)
from brutex.dashboard.dhan_rolling_pull import (
    DhanRollingOptionPullFn,
    DhanRollingOptionPullReport,
    default_dhan_rolling_option_pull,
)
from brutex.dashboard.discovery_warm import (
    DB_DATASETS_KEY,
    FNO_CASCADE_KEY,
    LATEST_MOVERS_DATE_KEY,
    MOVERS_DATASETS_KEY,
    SPOT_CASCADE_KEY,
    build_db_datasets_payload,
    filter_spot_datasets,
    latest_movers_minute_key,
    preopen_datasets_key,
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
from brutex.dashboard.health_hub import HealthHub
from brutex.dashboard.historical_pull import pull_contract_candles
from brutex.dashboard.host_allowlist import DEFAULT_ALLOWED_HOSTS
from brutex.dashboard.lake_census_holder import (
    FIRST_SCAN_PER_FILE_BUDGET_S,
    LakeCensusHolder,
)
from brutex.dashboard.lake_revision import (
    RevisionProbeMemo,
    cascade_report_revision,
    lake_revision,
    registry_revision,
    resolve_revision_probe_ttl,
    sweep_results_revision,
)
from brutex.dashboard.launch_button import register_launch_routes, store_lake_root_resolver
from brutex.dashboard.mapping_integrity_csv import mapping_integrity_to_csv
from brutex.dashboard.master_integrity_csv import master_integrity_to_csv
from brutex.dashboard.metadata_pull import MetadataPullReport, pull_expiries_contracts
from brutex.dashboard.movers_cache import (
    MoversSnapshotCache,
    candidate_key,
    revision_generation,
)
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
    asof_through_label,
    build_snapshot_payload,
    compute_spot_candidates,
    is_intraday_interval,
    parse_snapshot_date,
    pending_asset_payload,
)
from brutex.dashboard.movers_underlying_class import build_underlying_kind_map
from brutex.dashboard.movers_warm import (
    MoversWarmResult,
    futures_warm_grid,
    latest_movers_date,
    latest_movers_minute,
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
from brutex.dashboard.pipeline_pull import (
    PriorStepsVerdict,
    run_full_pipeline,
    strict_gate_enabled,
)
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
    clear_banner_after_start,
    persist_intent_for_launch,
    probe_pull_singleflight,
    restore_intent_after_refused_start,
    run_auto_resume,
    run_legacy_resume,
)
from brutex.dashboard.pull_jobs import (
    JOB_KIND_PULL,
    JOB_KIND_SWEEP,
    BackgroundPullJob,
    ProgressHook,
)
from brutex.dashboard.pull_outcome import LastPullOutcome, classify_retained_pull_outcome
from brutex.dashboard.pull_scope import (
    SCOPE_FNO_ONLY,
    SCOPE_FULL,
    SCOPE_SPOT_ONLY,
    CrossverifyTail,
    CrossverifyTailOutcome,
    build_fno_only_note,
    build_spot_only_note,
    fno_only_spot_report,
    normalize_pull_scope,
    run_crossverify_tail,
    spot_only_historical_report,
    spot_only_metadata_report,
)
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
from brutex.dashboard.summary_cache import OPTIONS, SPOT, SummaryCache
from brutex.dashboard.sweep_progress_cache import SweepProgressCache

if TYPE_CHECKING:  # runtime import is LAZY inside build_app — anyio is [ui]-only
    from brutex.dashboard.lake_offload import LakeOffload
from brutex.dashboard.sweep_results_view import (
    DEFAULT_TOP_N as SWEEP_LEADERBOARD_DEFAULT_TOP_N,
)
from brutex.dashboard.sweep_results_view import (
    MAX_TOP_N as SWEEP_LEADERBOARD_MAX_TOP_N,
)
from brutex.dashboard.sweep_results_view import (
    SWEEPS_DIRNAME,
    SweepScan,
    build_sweep_leaderboard,
    load_cascade_join_file,
    load_sweep_scan,
    resolve_preset_param,
    store_error_payload,
)
from brutex.dashboard.sweep_runner import (
    default_results_root,
    resolve_sweep_request,
    run_sweep_from_request,
    sweep_progress_file,
)
from brutex.dashboard.universe_cache import (
    STORE_FAULT_TYPES,
    UniverseSnapshotCache,
    UniverseStoreOutageError,
    coverage_revision,
    master_revision,
)
from brutex.dashboard.universe_pull import select_pull_instruments
from brutex.dashboard.universe_query import list_underlyings, universe_summary
from brutex.dashboard.verdict_banner import banner_for
from brutex.data.derive_lock import derive_in_progress, derive_lock_held_by_this_process
from brutex.data.economics_integrity import META_KEY as ECONOMICS_INTEGRITY_META_KEY
from brutex.data.economics_integrity import report_from_dict as economics_report_from_dict
from brutex.data.errors import CatalogError
from brutex.data.feed_root import feed_lake_root
from brutex.data.fno_candle_lake import FNO_CANDLE_LAKE_SEGMENT
from brutex.data.greeks_backfill import (
    dry_run_backfill_report,
    plan_greeks_backfill,
    resolve_backfill_batch_size,
    resolve_backfill_pause_s,
    run_greeks_backfill,
    scan_stale_greeks_targets,
)
from brutex.data.greeks_enrich import resolve_risk_free_source
from brutex.data.lake_footprint import META_MEASURED_AT, make_disk_guard, record_lake_footprint
from brutex.data.lake_invariants import (
    build_lake_census,
    census_to_dict,
    scan_lake_invariants,
)
from brutex.data.lake_keys import StockDriftMap
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
from brutex.data.pcr import PcrCorruptionError
from brutex.data.registry_store import ContractRow, RegistryError, RegistryStore
from brutex.data.spot_drift import load_stock_drift_map
from brutex.data.timeframe_materialize import MaterializeResult, materialize_lake_timeframes
from brutex.doctor_autorun import LiveProofAutorun
from brutex.engine.results_store import ResultsStoreError
from brutex.models import Exchange, Instrument, InstrumentType
from brutex.notifications.sweep_alerts import emit_greeks_backfill_complete_alert
from brutex.observability.correlation import request_id_scope
from brutex.observability.loop_watchdog import LoopWatchdog
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

#: The ``/api/v1/db/tables`` freshness-probe fault set: the store fault types
#: (``duckdb.Error``/``OSError`` — a wedged volume / unreachable DuckDB) PLUS
#: ``CatalogError``, because the DEC-STORE-WRITE-DEADLINE-001 wedge latch
#: (``InstrumentStore._ensure_not_wedged``, threaded into the read view's
#: ``execute``) raises CatalogError — a ``DataLakeError``, NOT a
#: ``duckdb.Error`` — and the wedge fires in the EXACT mid-pull contention
#: window the tables-route fold exists for (round-7 review MAJOR: the wedge
#: previously escaped the ``STORE_FAULT_TYPES``-only clause and 500'd where
#: the pre-cache route answered honest 0-counts).
_TABLES_PROBE_FAULT_TYPES: Final[tuple[type[BaseException], ...]] = (
    *STORE_FAULT_TYPES,
    CatalogError,
)


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
    #: transport :class:`~brutex.adapters.call_meter.CallMeter` (DEC-RATE-WIDGET-FNO-HONEST-001,
    #: reachable across the metadata + F&O phases since DEC-RATE-WIDGET-FNO-MEASURED-001's
    #: registered-meter lifecycle). ``True`` for any tick fed by the REGISTERED pool meter's
    #: real snapshot — the spot per-window observe AND the sampler's metadata/F&O ticks once
    #: the build site registered the meter; ``False`` means exactly "no meter registered" —
    #: the pre-registration window at pull start, a dry-run (which explicitly DEREGISTERS,
    #: DEC-RATE-METER-LIFECYCLE-001), or after the job-finish clear — so the live-rate view
    #: keeps "achieved" an honest 0 and bases the stall verdict on the controller's admission
    #: signal. Defaults ``True`` (the meter-fed contract; an idle panel never reports a
    #: fabricated completed-rate).
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
        is a REAL measured completed-rate (``True`` — a tick fed by the REGISTERED pool
        meter's real :class:`~brutex.adapters.call_meter.CallMeter` snapshot: the spot
        per-window observe AND, since DEC-RATE-WIDGET-FNO-MEASURED-001, the sampler's
        metadata/F&O ticks) or honestly absent (``False`` — no meter registered: the
        pre-registration window at pull start, a dry-run — which explicitly DEREGISTERS,
        DEC-RATE-METER-LIFECYCLE-001 — or after the job-finish clear). The
        ``/api/v1/pull/rate`` view reads it to keep "achieved" an honest 0
        + use the controller's admission signal for the stall verdict during the meter-less
        window, instead of fabricating a completed-rate.

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

    def sweep_progress_view(
        self,
        *,
        file_read: Callable[[Path], dict[str, Any] | None] | None = None,
    ) -> dict[str, Any]:
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

        *file_read* is the DEC-SWEEP-PROGRESS-NONBLOCK-CACHE-001 seam: the
        route injects :meth:`SweepProgressCache.read
        <brutex.dashboard.sweep_progress_cache.SweepProgressCache.read>` so
        the file arm becomes a TTL-memoised, DEADLINE-BOUNDED read (a wedged
        lake volume serves the last-known payload labelled ``unverified``
        instead of blocking) — it returns the file payload to serve, or
        ``None`` to fall through to (2)/(3), exactly like the direct
        ``exists()`` check. The default ``None`` keeps the direct,
        byte-identical legacy read (the in-process test/consumer contract).
        """
        from brutex.engine.sweep_supervisor import idle_progress, read_progress

        with self._lock:
            path = self.sweep_progress_path
            in_process = self.sweep_progress
        if path is not None:
            if file_read is not None:
                payload = file_read(path)
                if payload is not None:
                    return payload
            elif path.exists():
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
            # DEC-RATE-WIDGET-FNO-HONEST-001 / DEC-RATE-METER-LIFECYCLE-001: the sampler
            # pushes ``meter_present=False`` ONLY when no meter is registered (the
            # pre-registration window, a dry-run — which deregisters — or after the
            # job-finish clear); a REAL CallMeter snapshot (the spot observe or the
            # sampler's registered-meter metadata/F&O ticks) carries no such key →
            # defaults ``True`` (the meter-fed contract).
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
    sweep_progress_cache: SweepProgressCache | None = None,
    lake_offload: LakeOffload | None = None,
    prebuild_gate: PrebuildGate | None = None,
    pull_active_probe: Callable[[], bool | None] | None = None,
    pull_phase_probe: Callable[[], str | None] | None = None,
    liveproof: LiveProofAutorun | None = None,
    allowed_hosts: Sequence[str] | None = None,
    pull_intent_store: PullIntentStore | None = None,
    oneclick_trades_reader: oneclick_artifacts.TradesReader | None = None,
    session_rate: SessionRateController | None = None,
    dhan_spot_pull: DhanSpotPullFn | None = None,
    dhan_rolling_option_pull: DhanRollingOptionPullFn | None = None,
    census_spawn: Callable[[Callable[[], None]], None] | None = None,
    census_clock: Callable[[], float] | None = None,
    pull_outcome_probe: Callable[[], LastPullOutcome] | None = None,
    health_hub: HealthHub | None = None,
    loop_watchdog: LoopWatchdog | None = None,
    crossverify_runner: Callable[[Any], Any] | None = None,
    crossverify_tail: CrossverifyTail | None = None,
    auto_pull_scheduler: AutoPullScheduler | None = None,
    revision_probe_memo: RevisionProbeMemo | None = None,
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
    sweep_progress_cache
        Optional :class:`~brutex.dashboard.sweep_progress_cache.SweepProgressCache`
        backing the ``/api/v1/sweep-progress`` file-bridge read
        (DEC-SWEEP-PROGRESS-NONBLOCK-CACHE-001) — the sweep-progress twin of
        *rate_bridge_cache*: a repeat poll within its 1 s TTL is a
        zero-syscall O(1) memo hit, a post-TTL refresh is deadline-guarded on
        a worker daemon, and a wedged lake volume serves the last-known
        payload honestly labelled ``unverified``. Injected so tests drive the
        slow-read / cache-hit / provenance behaviour deterministically;
        ``None`` (the default) builds a fresh production cache.
    lake_offload
        Optional :class:`~brutex.dashboard.lake_offload.LakeOffload` — the
        dedicated bounded CapacityLimiter every LAKE-touching offload site
        rides (DEC-DASHBOARD-LAKE-IO-LIMITER-001): a wedged lake volume pins
        at most its 32 lake tokens, leaving AnyIO's DEFAULT 40-token pool
        free for the liveness / in-memory / CPU offloads (``/pull/rate``,
        ``/sweep-progress``, the report renders, the log tail), which a lake
        outage therefore can no longer starve. Injected so tests size the
        limiter deterministically; ``None`` (the default) builds one at the
        env-resolved production size.
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
    oneclick_trades_reader
        Optional typed :class:`~brutex.dashboard.oneclick_artifacts.TradesReader`
        for the ``/api/v1/oneclick/trades`` route (DEC-OPERATOR-PAGE-REAL-001).
        ``None`` (the default) resolves per request
        (DEC-ONECLICK-SWEEP-RUNS-REBIND-001): the fail-loud
        ``BRUTEX_ONECLICK_FIXTURE_ROOT`` fixture tree activates a memoised
        :class:`~brutex.dashboard.oneclick_artifacts.FixtureTradesReader`;
        otherwise the PRODUCTION
        :class:`~brutex.dashboard.oneclick_artifacts.TradeStoreReader` pages
        the SHIPPED #1275 results-depth sidecars under the Ruling-4
        ``sweep_runs/`` sibling root (a run without those sidecars degrades
        to the honest ``status="awaiting"``), and the no-lake configuration
        keeps the honest
        :class:`~brutex.dashboard.oneclick_artifacts.AwaitingTradesReader`.
    session_rate
        Optional :class:`~brutex.dashboard.pull_controller.SessionRateController`
        — the ONE per-session rate holder every pull shares
        (DEC-DHAN-PERSIST-RATE-001) and the registered-CallMeter home of
        DEC-RATE-WIDGET-FNO-MEASURED-001 / DEC-RATE-METER-LIFECYCLE-001.
        Injected so tests drive the meter register/clear lifecycle
        deterministically (pre-warm a controller, pre-register a meter, then
        assert the dry-run / job-end eviction through the REAL routes); ``None``
        (the default) builds a fresh production holder — byte-identical to the
        pre-seam construction (mirrors the ``summary_cache`` injection pattern).
    pull_outcome_probe
        Optional zero-arg probe returning the app's live
        :class:`~brutex.dashboard.pull_outcome.LastPullOutcome`
        (DEC-OPERATOR-HEALTH-TRUTH-001).  The production default classifies
        THIS app's own retained last-pull state + id-correlated verdict holder
        through the ONE :func:`~brutex.dashboard.pull_outcome.\
classify_retained_pull_outcome`; tests inject a deterministic probe (or a
        probe over their OWN real :class:`BackgroundPullJob`) to drive every
        outcome state through the real routes (mirrors ``pull_active_probe``).
    health_hub
        Optional :class:`~brutex.dashboard.health_hub.HealthHub` backing the
        ``GET /api/v1/health/hub`` fold (DEC-OPERATOR-HEALTH-TRUTH-001).
        Injected so tests pin lane states with a deterministic clock/env;
        ``None`` (the default) builds the production hub over this app's lake
        root resolver + the resolved *pull_outcome_probe*.
    loop_watchdog
        Optional :class:`~brutex.observability.loop_watchdog.LoopWatchdog`
        (DEC-DASHBOARD-PULL-CONTENTION-RESILIENCE-001 G5): when given, the app
        rides a minimal LIFESPAN (``FastAPI(lifespan=...)`` — Starlette 1.x
        removed the ``add_event_handler``/``on_event`` seam, so the lifespan
        kwarg is the ONE lifecycle hook) that starts the 1 s event-loop
        heartbeat task + monitor daemon on serve and stops them cleanly on
        shutdown. ``None`` (the default) builds the app with NO lifespan —
        zero threads, tasks or lifecycle hooks (census-pinned), so every
        existing ``build_app()`` caller is byte-identical. Production binds
        one in ``cli.cmd_dashboard`` behind the fail-loud
        ``BRUTEX_LOOP_WATCHDOG`` kill switch (default ON).
    auto_pull_scheduler
        Optional :class:`~brutex.dashboard.auto_pull_scheduler.AutoPullScheduler`
        (DEC-PULL-SCOPED-ALL-ONECLICK-001 round-5 fold — the self-arming
        post-market daily auto-pull). When given, ``build_app`` binds the
        scheduler's fire seam to THIS app's own spot-only
        ``_launch_pipeline`` path (all launcher rails inherited), the
        ``/pull-pipeline`` route honors ``auto_arm=1`` (the button's
        first-click-arms-forever semantics), the arm/disarm toggle route +
        the additive ``auto_pull`` payload keys activate, and the lifespan
        resumes ticking at boot iff the durable state file says armed.
        ``None`` (the default) wires NOTHING — zero threads, no new payload
        keys, ``auto_arm`` refused 503 (the loop_watchdog zero-threads
        posture); production builds one in ``cli.cmd_dashboard`` for the
        resolved lake root.
    revision_probe_memo
        Optional :class:`~brutex.dashboard.lake_revision.RevisionProbeMemo`
        (DEC-O1-GUARANTEE-HARDEN-001 H2) bounding the two per-poll O(dirs)
        freshness probes — the oneclick runs-listing manifest-stat walk and
        the ``/results`` ``sweep_results_revision`` scandir digest — to one
        sweep per TTL window (default 1 s via
        ``BRUTEX_REVISION_PROBE_TTL_S``; ``0`` disables = byte-identical
        per-poll probes). ``None`` (the default) constructs a fresh memo with
        the env-resolved TTL; tests inject one with an explicit ``ttl_s`` +
        clock for deterministic pins.

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

        # Lazy like fastapi itself: lake_offload imports anyio, which ships
        # with the [ui] extra only (the app.py wheel-free-import doctrine).
        from brutex.dashboard.lake_offload import LakeOffload as _LakeOffload
        from brutex.dashboard.trusted_host import BracketAwareTrustedHostMiddleware
    except ImportError as exc:
        raise RuntimeError(
            "FastAPI is not installed. Install with `pip install "
            "fastapi uvicorn` to run the dashboard. Unit tests for the "
            "state model do not need FastAPI."
        ) from exc

    # Hoist the response classes used as ROUTE RETURN ANNOTATIONS into module
    # globals (DEC-OPENAPI-ANNOTATIONS-RESOLVE-001) — the SAME hoist
    # :func:`_make_ws_handler` applies to ``WebSocket``. Under
    # ``from __future__ import annotations`` every closure's ``-> JSONResponse``
    # (+ ``Response`` / ``HTMLResponse``) annotation is a STRING that
    # FastAPI/pydantic resolve against ``endpoint.__globals__`` — this module's
    # dict, where a function-local import is invisible. FastAPI's lenient
    # ``try_eval_type`` kept the unresolvable ForwardRefs at registration time,
    # so ``app.openapi()`` raised ``PydanticUserError`` → ``/openapi.json``
    # 500'd and ``/docs`` + ``/redoc`` were dead. The hoist runs strictly
    # BEFORE any route registration below; ``PlainTextResponse`` is used only
    # as a runtime value (never an annotation), so it is deliberately NOT
    # hoisted. Docs-surface only — zero route behavior change, and the
    # module still imports without fastapi (the guard above is untouched).
    globals()["HTMLResponse"] = HTMLResponse
    globals()["JSONResponse"] = JSONResponse
    globals()["Response"] = Response

    from brutex.observability.metrics import get_registry, render_prometheus

    # The retired-vendor 24h-JWT token keepalive was retired here (PR-J4c): the
    # dashboard now authenticates via Groww TOTP, so the vestigial keepalive
    # lifespan wiring was removed. The retired-vendor adapter package has since been
    # deleted; the dashboard simply no longer drives any keepalive.
    #
    # Event-loop liveness watchdog (DEC-DASHBOARD-PULL-CONTENTION-RESILIENCE-001
    # G5): OPT-IN — `None` (the default, every test construction) builds the app
    # with NO lifespan at all, spawning ZERO threads/tasks (byte-identical app;
    # census-pinned). A wired watchdog rides a minimal LIFESPAN (the modern
    # Starlette seam — `add_event_handler`/on_event were REMOVED in Starlette
    # 1.x, so the lifespan kwarg is the one lifecycle hook): startup starts the
    # 1 s heartbeat task on THIS app's running loop + the monitor daemon;
    # shutdown cancels the task + joins the monitor BOUNDED. Production binds
    # one in `cli.cmd_dashboard` behind the fail-loud BRUTEX_LOOP_WATCHDOG kill
    # switch (default ON).
    # The auto-pull scheduler rides the SAME lifespan seam (round-5 fold): its
    # startup hook resumes ticking iff the durable state file says armed
    # (a disarmed boot spawns ZERO threads and never auto-fires); shutdown
    # ends the ticker bounded. `None` for both (every test construction's
    # default) keeps the app lifespan-free — byte-identical, zero threads.
    if loop_watchdog is not None or auto_pull_scheduler is not None:
        from contextlib import asynccontextmanager

        watchdog = loop_watchdog  # narrow for the closure (mypy: possibly None)
        scheduler_boot = auto_pull_scheduler

        @asynccontextmanager
        async def _lifecycle_lifespan(_app: Any) -> AsyncGenerator[None]:
            """Start/stop the watchdog + auto-pull ticker around serving."""
            if watchdog is not None:
                await watchdog.start_from_running_loop()
            if scheduler_boot is not None:
                # One small state-file read at startup; armed ⇒ resume ticking
                # (the boot catch-up fires on an early TICK, never here).
                # Belt (refuter round 6 N-1, the run_auto_resume "never crash
                # boot" posture): the store's load() is itself never-raises,
                # but a convenience scheduler must not be ABLE to kill boot —
                # any unexpected raise logs and the dashboard serves.
                try:
                    scheduler_boot.start_if_armed()
                except Exception as boot_exc:  # pylint: disable=broad-exception-caught  # never crash boot
                    _LOG.warning(
                        "auto_pull.boot_start_failed",
                        error=str(boot_exc),
                        error_type=type(boot_exc).__name__,
                    )
            try:
                yield
            finally:
                if scheduler_boot is not None:
                    scheduler_boot.stop()
                if watchdog is not None:
                    watchdog.stop()

        app = FastAPI(title="BruteX Dashboard", version="0.1.0a0", lifespan=_lifecycle_lifespan)
    else:
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
    # The /movers point-in-time snapshot cache (DEC-MOVERS-O1-LATENCY-001) is
    # constructed BELOW — after `_pull_live` exists — so it can wire the SAME
    # pull-time adaptive floor its floored siblings carry
    # (DEC-LATENCY-HONESTY-BATCH-001; see the construction site's comment).
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
    # The /api/v1/sweep-progress file-bridge read memo
    # (DEC-SWEEP-PROGRESS-NONBLOCK-CACHE-001): the progress JSON lives under
    # <lake>/sweeps/progress/ on the SAME external volume, so its per-poll
    # synchronous exists()+read used to run ON the event loop — a measured
    # 1.5 s stalled read froze a concurrent /api/v1/state poll 9.1 ms →
    # 1305.4 ms. Same doctrine as the rate-bridge cache above: 1 s TTL memo,
    # deadline-bounded refresh (0.5 s) on a daemon, non-blocking single-flight,
    # honest additive `progress_source` / `progress_probe_age_s` provenance.
    # Owned by this app instance (no module-global mutable state — CLAUDE.md §4).
    _sweep_progress_cache = (
        sweep_progress_cache if sweep_progress_cache is not None else SweepProgressCache()
    )
    # The dedicated LAKE-I/O capacity limiter (DEC-DASHBOARD-LAKE-IO-LIMITER-001
    # — the #1522/#1622 named follow-up, audit M4): EVERY lake-touching offload
    # below rides `_run_lake` (a `run_in_threadpool` twin bound to this app's
    # OWN 32-token AnyIO CapacityLimiter) instead of the DEFAULT shared
    # 40-token pool, so a wedged lake volume pins at most the 32 lake tokens
    # and the liveness/in-memory/CPU offloads — the deadline-guarded
    # /pull/rate + /sweep-progress memos, the .xlsx/.zip renders, the local
    # log tail — keep the default pool to themselves and stay responsive
    # through a lake outage (the measured repro: the 41st offload queued
    # 2701 ms behind 40 wedged workers, starving even the bounded /pull/rate).
    # Owned by this app instance (no module-global mutable state — CLAUDE.md §4).
    _run_lake = lake_offload if lake_offload is not None else _LakeOffload()

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
    # The /movers point-in-time snapshot cache (DEC-MOVERS-O1-LATENCY-001): caches
    # the expensive, top_n-independent candidate set per snapshot coordinate so a
    # sub-filter / top_n / re-selection is an O(1) hit + a cheap re-rank, never a
    # lake re-scan. It is invalidated for free by the SAME write-generation signal
    # as the coverage SummaryCache (a pull/materialize bumps `_summary_cache`'s
    # generation, which the movers cache stamps every entry with).
    #
    # SANCTIONED design-pin change (DEC-LATENCY-HONESTY-BATCH-001, supersedes the
    # movers-rows carve-out recorded in `movers_cache.py`'s module docstring under
    # DEC-DB-DISCOVERY-ADAPTIVE-FLOOR-001): this cache now ALSO opts into the
    # pull-time adaptive floor its floored siblings below carry. It was the lone
    # bare instance, so during a pull every window-tick generation bump forced a
    # full O(instruments × bars-read) candidate re-scan per /movers poll —
    # back-to-back, the exact scan-storm class #920 fixed. The floor serves a
    # generation-MISMATCHED entry only while a pull is genuinely running in this
    # process AND the entry is younger than min(max(2 s, its own scan duration),
    # 600 s) — so a poll during a pull may serve a snapshot up to that
    # floor-window stale (each stale serve is LOGGED `movers_cache.stale_served`,
    # never silent), and never pre-pull data BEYOND the floor window; the moment
    # the pull ends the probe reads False → fresh recompute, and the post-pull
    # movers warm re-seeds at the end generation. An INJECTED cache (tests /
    # legacy callers) is used verbatim — byte-identical to before.
    _movers_cache = (
        movers_cache
        if movers_cache is not None
        else MoversSnapshotCache(pull_active_probe=_pull_live, adaptive_floor=True)
    )
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
    # The /db F&O REGISTRY count cache (DEC-O1-GUARANTEE-HARDEN-001 H1): the
    # registry pagers' FILTERED COUNT(*) totals + the nav-badge counts dict are
    # paging-invariant, so they are memoised per registry CONTENT — keyed by the
    # `registry_revision` stat token (registry.duckdb + its .wal sibling,
    # digested to the generation via `revision_generation`), the #867
    # `candle_count_key` idiom on a DEDICATED instance. A repeat/paging click
    # within one registry revision is an O(1) hit; a metadata pull's write
    # moves the file token → recount (never stale); a probe OUTAGE (token
    # None) computes fresh UNCACHED (the lake_revision-family posture). The
    # page DATA queries stay per-click LIMIT/OFFSET SELECTs — already O(page).
    _registry_count_cache = MoversSnapshotCache()
    # The revision-probe TTL memo (DEC-O1-GUARANTEE-HARDEN-001 H2): bounds the
    # two per-poll O(dirs) probe sweeps (oneclick runs listing + the /results
    # sweep_results_revision digest) to ONE sweep per TTL window (default 1 s,
    # env BRUTEX_REVISION_PROBE_TTL_S — junk fails loud HERE at build_app for
    # non-CLI embedders (review round 1 MINOR-1), and `cmd_dashboard` validates
    # the SAME env FIRST with the clean `[brutex]` one-liner + EXIT_USER_ERROR,
    # the loop-watchdog sibling idiom (review round 2); 0 disables).
    # Staleness ≤ TTL, far under the 120 s adaptive-floor norm; a post-TTL
    # poll always re-probes (no permanent-staleness state, pinned). The eager
    # resolve keeps the memo's own lazy first-use path intact (an injected
    # memo validates itself).
    _revision_probe_memo = (
        RevisionProbeMemo(ttl_s=resolve_revision_probe_ttl())
        if revision_probe_memo is None
        else revision_probe_memo
    )
    # The /coverage/windows counts + page-row caches (DEC-COVERAGE-WINDOWS-O1-001 +
    # DEC-WINDOWS-MIDPULL-FLOOR-001): the `GROUP BY market, status` bucketed counts
    # AND the projected row page over the window ledger are invariant within one
    # ledger stamp, so both families are memoised behind the composite-stamp-token
    # generation db_view derives (`revision_generation` — NOT `_summary_cache.
    # generation`, which mark_dirty bumps per window tick) — a repeat poll within
    # one stamp is O(1), any ledger flush moves the stamp → recompute. Mid-pull the
    # deltas accrue ~1 file/2 s and each fresh stamp's fold is O(total rows +
    # delta footers), so an unfloored cache recomputed counts+page back-to-back
    # on every 4 s poll as the delta count grew (the multi-hour-pull 4 s+ poll
    # class). Built by the ONE db_view factory (`make_window_payload_caches` —
    # parameters exist ONCE): both instances ride the `_LEDGER_READ_CACHE`
    # probe/floor shape (constant-True stamp-moved probe + the 120 s cap), so a
    # stamp-mismatched entry younger than min(max(2 s, its own compute
    # duration), 120 s) is served STALE (logged `movers_cache.stale_served`,
    # never silent) on ANY churn — an in-process pull's flushes AND a
    # cross-process CLI pull's — while steady state never reaches the floor
    # branch. The PAGE family gets its own SMALL instance (capacity 8; a page
    # entry holds up to min(limit, WINDOW_ROWS_CAP)=20k row-dicts, so it must
    # not share the 256-cap counts instance nor evict its tiny entries); both
    # stay outside the candle int-total cache's key namespace above.
    _window_counts_cache, _window_page_cache = make_window_payload_caches()
    # The pre-open EXPORT rows cache (DEC-DB-LATENCY-HARDENING-001, MIGRATION.md
    # #1719): a repeat `.xlsx`/`.zip` export click previously RE-RAN the entire
    # report build — measured 206 DuckDB per-symbol aggregates binding 616 files
    # + a 1,442-scandir dataset walk per click (326–574 ms at 103 symbols,
    # +22 MB RSS cold; identical statement counts on the REPEAT click — zero
    # caching). The built `PreOpenRow` list is invariant within one write
    # generation for one (basis, [from, to], symbols filter) coordinate, so it
    # is memoised here behind `_summary_cache.generation` (a pull — or a
    # cross-process CLI pull via the revision probe — bumps it → rebuild; the
    # SAME invalidation signal every discovery cache above rides). SMALL
    # dedicated instance (a row list holds symbols × days entries, so it must
    # not share — nor churn-evict — the 256-cap generic caches; the
    # WINDOW_PAGE_CACHE precedent). The workbook/zip SERIALISATION stays
    # per-click (CPU-bound over the cached rows, mode/min_pct-dependent).
    # Single-flight: two concurrent export clicks pay ONE build.
    _preopen_rows_cache = MoversSnapshotCache(capacity=8)
    # The discovery-walk memos below (the F&O/spot cascades, the datasets
    # triple-walk, the latest-date/minute resolvers) additionally opt into the
    # PULL-TIME adaptive floor (DEC-DB-DISCOVERY-ADAPTIVE-FLOOR-001, the
    # ported #920 pattern) — and since DEC-NAV-CACHES-FLOOR-REKEY-001 so do
    # the nav-latency trio (`_coverage_cache` / `_universe_completeness_cache`
    # / `_mapping_integrity_cache`) and the /results
    # `_sweep_leaderboard_cache`, whose freshness tokens moved OUT of the
    # cache KEY into the GENERATION (`revision_generation`) exactly so the
    # floor is REACHABLE (a token folded into the key at generation=0 minted
    # a COLD key per mid-pull poll — the floor's stale-serve branch was
    # structurally dead for them). Mechanics: during a pull the write
    # generation bumps on EVERY window tick, so each poll used to re-pay a
    # full O(datasets) walk back-to-back — and post-prebuild (dataset count ×
    # the 19 materialised TFs) that walk is multi-minute. While
    # `_pull_live()` reads True, a generation-mismatched entry younger than
    # min(max(2 s, its own walk duration), 600 s) is served STALE (logged
    # `movers_cache.stale_served`, never silent); the moment the pull ends
    # the probe reads False → fresh recompute, and the post-pipeline
    # `warm_discovery_memos` force-refreshes the discovery memos off ONE
    # shared walk so the stale-floor state can never outlive the pull.
    #
    # The /db F&O cascade cache (DEC-DB-FNO-CASCADE-001): the underlying→expiry→contract
    # index is an O(datasets) build over a lake dir-walk; memoised here behind the SAME
    # write-generation as the caches above so a pull rebuilds it but every cascade click
    # (underlyings / expiries / contracts) is an O(1) handoff — tiny lazy payloads, never
    # the flat thousand-row dropdown. Now the COMMON `MoversSnapshotCache` (keyed
    # FNO_CASCADE_KEY) so it shares the pull-time floor + the discovery warm; the
    # bespoke single-slot `FnoCascadeCache` class is superseded at this wiring site.
    # DEC-DASHBOARD-PULL-CONTENTION-RESILIENCE-001: the THREE discovery-walk memos
    # additionally opt into stale-while-revalidate — PAST the adaptive floor a
    # mid-pull poll serves the last-known index IMMEDIATELY and at most ONE
    # tokenless background daemon revalidates, instead of an inline O(datasets)
    # walk (or a blocking single-flight wait pinning a 32-token lake worker per
    # waiter — the 8-tab herd defect). Staleness stays bounded (≤ one walk/fold
    # behind live), log-labelled (`movers_cache.stale_served` + the
    # `stale_extended` escalation), and hard-converged by the pull-end
    # `warm_discovery_memos` force_fresh reseed (which bypasses floor AND SWR).
    _fno_cascade_cache = MoversSnapshotCache(
        pull_active_probe=_pull_live, adaptive_floor=True, stale_while_revalidate=True
    )
    # The /db candle-datasets discovery cache (DEC-DB-TAB-SWITCH-O1-001): the
    # `/api/v1/db/candles/datasets` endpoint walks the WHOLE `bars/` tree THREE times
    # (spot + F&O + options, each an O(files) `rglob("*.parquet")`) — and it fires on
    # EVERY `/db` candle tab switch (spot↔F&O↔option). The three lists are invariant
    # between lake writes, so they are memoised behind the SAME generation signal as the
    # caches above (a pull bumps it → rediscover; otherwise a tab switch is an O(1) handoff
    # of the cached lists). REUSES the COMMON `MoversSnapshotCache` (one dataset key).
    _db_datasets_cache = MoversSnapshotCache(
        pull_active_probe=_pull_live, adaptive_floor=True, stale_while_revalidate=True
    )
    # The /db SPOT cascade cache (DEC-DB-SPOT-CASCADE-001): the symbol→interval index is
    # an O(datasets) build over the non-F&O lake partitions; memoised behind the SAME
    # write-generation as the caches above (the spot twin of `_fno_cascade_cache`), so a
    # pull rebuilds it but every symbol-picker open is an O(1) handoff — the small
    # symbols-with-intervals payload, never the flat ~750-row spot dropdown.
    _spot_cascade_cache = MoversSnapshotCache(
        pull_active_probe=_pull_live, adaptive_floor=True, stale_while_revalidate=True
    )
    # The /db TABLES summary cache (DEC-DASHBOARD-PULL-CONTENTION-RESILIENCE-001,
    # the coordinator-directed tables-route fold): `GET /api/v1/db/tables` ran the
    # `table_summary` COUNT(*) sweep UNCACHED on the store's read connection on
    # EVERY open, so mid-pull it convoyed behind whatever multi-second read held
    # the store read lock (the operator's live "request timed out after 12000ms"
    # on the tables list, twice; the in-repo forensic precedent latency_ms≈32796).
    # The 3-row payload is a pure function of the store tables (instruments /
    # instrument_runs / schema_meta), so it is memoised on ONE STABLE key whose
    # GENERATION digests the write generation AND the instrument-master content
    # revision (`master_revision` — a keyless load-instruments moves the
    # instruments/instrument_runs counts WITHOUT bumping the generation; the
    # `_nav_latency_coords` digest-bridge form, so the floor + SWR arms stay
    # reachable on the one stable key), with the SAME serve-last-known posture
    # as the discovery memos above: past-floor mid-pull the last-known counts
    # serve instantly and AT MOST ONE tokenless background daemon revalidates.
    # SMALL dedicated instance (one live key — the WINDOW_PAGE_CACHE
    # small-instance precedent); the compute reads a FRESH `store.connection`
    # view per run, so the background revalidator never shares a
    # `_LockedReadConn` with a request thread.
    _db_tables_cache = MoversSnapshotCache(
        capacity=8,
        pull_active_probe=_pull_live,
        adaptive_floor=True,
        stale_while_revalidate=True,
    )
    # The tables cache's ONE stable key (the freshness token lives in the
    # GENERATION digest, never the key — the floor-rekey law).
    db_tables_key = "db-tables-summary"
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
    # The DEFAULT-poll latest-MINUTE display resolver cache (DEC-MOVERS-ASOF-LATEST-DEFAULT-001):
    # when no explicit `time` is sent AND the interval is intraday, the movers route resolves
    # the latest available spot 1-minute (`latest_movers_minute`) for the ADDITIVE `latest_time`
    # display echo — so the dropdown/label SHOW the real latest minute (e.g. `latest (15:29)`)
    # while the cut stays whole-day (`cutoff_min=None`, identical latest close across every TF).
    # An O(spot-1m-files) DuckDB MAX, so it is memoised per (write generation, latest date) —
    # a repeat DEFAULT intraday poll on the same day is an O(1) handoff; a pull re-resolves.
    _latest_movers_minute_cache = MoversSnapshotCache(
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
    # The Futures/Options underlying-CLASS kind map cache (DEC-MOVERS-UNDERLYING-CLASS-001):
    # the `{underlying → index|stock}` map built from the STORED instrument master
    # (`build_underlying_kind_map`, an O(underlyings) read) stamps each F&O candidate's
    # `underlying_kind` for the RANK-time `All | Indices | Stocks` filter. The map is a
    # pure function of the instrument MASTER, so it is keyed on the master CONTENT
    # revision (`master_revision`) at a FIXED generation=0. Review round 1: the original
    # generation-only key missed a keyless
    # `load-instruments`, which moves the master WITHOUT bumping the write generation
    # (the documented master_revision trap the sibling nav-latency caches close) — and
    # a write-generation key would ALSO rebuild pointlessly on every mid-pull write
    # tick (the #920 lesson), which the content token avoids. Built on the threadpool
    # worker (it reads `store.connection`). SPOT needs no map (its class comes from
    # the lake segment). REUSES the COMMON cache.
    _movers_kind_cache = MoversSnapshotCache()
    # The cache-key STEM for the underlying-kind map; the master content token is
    # folded in by `_underlying_kinds_cached` — the ONE key-law home (review round 1).
    movers_kind_key = "movers_underlying_kinds"

    def _underlying_kinds_cached(store: Any) -> tuple[dict[str, str], str]:
        """Return ``({F&O underlying → kind}, master_token)``, memoised on the master content token.

        The ONE home of the underlying-kind-map key law (review round 1 — previously
        two copy-pasted ``get_or_compute`` sites keyed on the write generation alone):
        the map is a pure function of the instrument MASTER, so the key folds
        :func:`master_revision` — the content token a keyless ``load-instruments``
        moves WITHOUT bumping the write generation — at a FIXED ``generation=0``
        (the ``_sweep_leaderboard_cache`` token-in-key precedent; the nav-latency
        trio has since moved to the ``_nav_latency_coords`` digest-bridge form —
        equivalent invalidation). The returned *master_token* MUST also be folded
        into the F&O
        candidate cache keys by every caller (the movers route AND the post-pull
        warmer), because the computed candidates carry the map's verdict STAMPED per
        row — a fresh map beside a stale stamped candidate set would still
        misclassify (the same staleness class, closed end-to-end).
        """
        count, latest = master_revision(store)
        master_token = f"{count}:{latest}"
        kinds: dict[str, str] = _movers_kind_cache.get_or_compute(
            f"{movers_kind_key}|m={master_token}",
            generation=0,
            compute=lambda: build_underlying_kind_map(store.list_index_equity_instruments()),
        )
        return kinds, master_token

    # The /db moneyness stock-drift map cache (DEC-DB-MONEYNESS-STOCK-EVIDENCE-001 —
    # the /db twin of the Greeks ladder's DEC-GREEKS-SPOTJOIN-STOCK-001 stock arm,
    # the exact memo law the #1482 named follow-up prescribed): the map is a pure
    # function of the on-disk spot dirs (moved by a pull → the write generation) AND
    # the instrument master (moved by a keyless `load-instruments` WITHOUT a
    # generation bump — the documented master_revision trap), so the memo keys on
    # the COMPOSITE token: `master_revision` folded into the KEY, the write
    # generation as the cache's native generation. REUSES the COMMON
    # `MoversSnapshotCache` (no new class).
    _stock_drift_cache = MoversSnapshotCache()

    def _stock_drift_map_cached(store: Any, lake: Path) -> StockDriftMap | None:
        """The moneyness spot-join's stock drift map, composite-token memoised.

        Thin memo over the COMMON fail-soft
        :func:`~brutex.data.spot_drift.load_stock_drift_map` — the EXACT loader +
        verification law the Greeks enrichment pass consumes (CLAUDE.md §11 COMMON,
        never a second fold), so a drifted stock lake reads the SAME recovered spot
        key on `/db` moneyness as the Greeks ladder stamps. Inherits the loader's
        contract verbatim: no master → ``None`` (the ladder stays byte-identical —
        the honest ``"?"``), a confirmed master-read error → ``None`` with the
        loader's own WARN, an empty master → ``{}`` with zero lake I/O. ``None`` is
        a cacheable value (the `_registry_strike_chain` precedent). Runs on the
        threadpool worker — the ONE consumer is the lazy `stock_drift_loader`
        closure inside the `/api/v1/db/candles` `query_candles` call, invoked only
        on the exhausted-primary recovery path (a healthy exact-key page never
        loads the map).
        """
        count, latest = master_revision(store)
        return _stock_drift_cache.get_or_compute(
            f"stockdrift|m={count}:{latest}",
            generation=_summary_cache.generation,
            compute=lambda: load_stock_drift_map(lake),
        )

    # The /coverage + /universe nav-latency caches (DEC-DASHBOARD-NAV-LATENCY-001,
    # re-keyed by DEC-NAV-CACHES-FLOOR-REKEY-001): three pollers —
    # `/api/v1/coverage` (every 4 s), `/api/v1/universe/completeness` and
    # `/api/v1/universe/mapping-integrity` (every 15 s) — each a pure function
    # of the instrument master + the coverage ledger, memoised behind the
    # COMPOSITE token = the dashboard write-generation (`_summary_cache.
    # generation`, bumped by every pull / materialize) AND the
    # instrument-master content revision (`master_revision`, which a keyless
    # `load-instruments` moves WITHOUT bumping the generation). The token now
    # drives the GENERATION via the `revision_generation` digest bridge over a
    # STABLE key (`_nav_latency_coords`) — invalidation is equivalent to the
    # superseded token-in-key-at-generation=0 fold (any token change moves the
    # generation → recompute, never stale — Golden Rule #1), but a mid-pull
    # mark_dirty storm now lands on the SAME key and can engage the pull-time
    # adaptive floor below instead of minting a cold key per poll (the #920
    # storm class this trio was previously blind to: every window tick forced
    # a full O(datasets)+ledger rebuild per poll). FLOOR CAVEAT (stated): a
    # floored serve is generation-blind within its window, so a poll landing
    # ≤ min(max(2 s, its own build duration), 600 s) after an instrument load
    # DURING a pull may serve the pre-load payload once — bounded, logged
    # `movers_cache.stale_served`, and closed the moment the pull ends (the
    # same envelope the discovery memos above accept). REUSES the COMMON
    # generation-stamped LRU `MoversSnapshotCache` (no new class).
    _coverage_cache = MoversSnapshotCache(pull_active_probe=_pull_live, adaptive_floor=True)
    _universe_completeness_cache = MoversSnapshotCache(
        pull_active_probe=_pull_live, adaptive_floor=True
    )
    _mapping_integrity_cache = MoversSnapshotCache(
        pull_active_probe=_pull_live, adaptive_floor=True
    )
    # The /universe lake-uniqueness census (DEC-LAKE-HEAL-001) deliberately does
    # NOT get a `MoversSnapshotCache` here: it is a full bounded lake scan —
    # O(files), one key-column read per file — and `pull_jobs` bumps the write
    # generation on EVERY per-pair write tick, so a generation-keyed memo would
    # re-run the whole-lake scan on every 15 s chip poll during a pull (the #920
    # regression class). Since DEC-LAKE-CENSUS-NONBLOCKING-001 it rides the
    # `LakeCensusHolder` (constructed beside the route, which needs the scan
    # closure defined below): the scan runs on a DEDICATED daemon thread (zero
    # AnyIO tokens), the route serves an O(1) last-known-verdict snapshot, and
    # the SAME adaptive-TTL-floor law throttles rescans — SUPERSEDING the prior
    # `_summary_cache.get_or_refresh(LAKE_UNIQUENESS, …)` wiring, whose COLD
    # single-flight parked every stacked 15 s poller in an unbounded wait ON a
    # threadpool worker and starved the shared AnyIO limiter for the whole
    # 26-minute production scan (the reproduced dashboard-wide freeze).
    # The /results sweep-leaderboard cache (DEC-DASHBOARD-RESULTS-PAGE-001,
    # re-keyed by DEC-LEDGER-DELTA-O-PAGE-001, then again by
    # DEC-NAV-CACHES-FLOOR-REKEY-001): the `<lake>/sweeps` discovery +
    # per-tuple Parquet read is O(stores × rows); the ranked leaderboard for a
    # given (instrument, tf, direction, rank_by, top_n) is a pure function of
    # the sweep tree, so it is memoised behind the `sweep_results_revision`
    # CONTENT token — NOT the `_summary_cache` write generation, which the
    # pull funnel's `mark_dirty` bumps per window tick and which a CLI
    # `brutex sweep-all` (a separate process) never bumps at all. The token
    # now drives the GENERATION (`revision_generation`) over STABLE keys —
    # invalidation equivalent to the superseded rev-in-key fold, but a RUNNING
    # dashboard sweep's per-flush revision moves can now engage the pull-time
    # adaptive floor (bounded stale serves, logged) instead of paying the full
    # scan per 4 s poll; a separate-process CLI sweep still recomputes per
    # flush (the probe reads False there — the floor's honest envelope).
    # Two-level entries share this ONE instance: the `sweep_scan` SweepScan +
    # a `sweep_leaderboard|…` ranked payload per filter key, so a filter/rank
    # click within one revision re-ranks the cached scan with zero store
    # loads; on a revision-probe OUTAGE the filter key doubles as the
    # serve-last-known prefix (DEC-CACHE-SERVE-LAST-KNOWN-001 — see
    # `api_sweep_leaderboard`). REUSES the COMMON LRU `MoversSnapshotCache`
    # (no new class — CLAUDE.md §11 COMMON); the LRU ages out old keys.
    _sweep_leaderboard_cache = MoversSnapshotCache(
        pull_active_probe=_pull_live, adaptive_floor=True
    )
    # The one-click artifact read memo (DEC-OPERATOR-PAGE-REAL-001): every
    # `/api/v1/oneclick/*` read (runs listing, manifest, top100, events tail
    # page, the LAST chain-verify verdict) is keyed on a per-artifact
    # `"mtime_ns:size"` stat token folded into the KEY at a FIXED generation=0
    # (the `_sweep_leaderboard_cache` `rev=` precedent) — a repeat poll of an
    # unchanged artifact is an O(1) hit, a token move re-parses, and an ABSENT
    # artifact computes fresh and caches NOTHING (never a stale serve, never a
    # poisoned key). A stat-probe OUTAGE (the artifact may still exist) serves
    # the LAST-KNOWN payload through `_oneclick_outages` with one loud WARN per
    # outage instead of a silent uncached O(artifact) re-parse per poll
    # (C-r2-5, DEC-ONECLICK-PERF-CLUB-001 — the serve-last-known doctrine; the
    # gate + chokepoint live in `oneclick_artifacts.serve_revision_cached`;
    # SIBLING stats folded into the verify/manifest keys ride the SAME gate
    # via `sibling_key_token` — r1 MINOR-1 fold; on the DEC-CACHE-SERVE-LAST-
    # KNOWN-001 reconcile this landed arm SUPERSEDED batch A's route-level
    # `serve_last_known` branches here — the labelled arm survives on the
    # non-oneclick keystone sites below).
    # HONEST envelope (Golden Rule #10): the FIRST read per revision pays the
    # O(artifact) parse; the cached hit is O(1) vs run count / artifact size.
    # REUSES the COMMON `MoversSnapshotCache`.
    _oneclick_cache = MoversSnapshotCache()
    _oneclick_outages = oneclick_artifacts.OutageGate()

    def _probe_outage_payload(
        payload: dict[str, Any], *, served_last_known: bool
    ) -> dict[str, Any]:
        """Shallow-copy *payload* + stamp the ADDITIVE `revision_probe` degrade marker.

        DEC-CACHE-SERVE-LAST-KNOWN-001 (the `store_error_payload` honest-degrade
        precedent): a payload served while the revision probe is DOWN must say
        so — either "serving last-known" (a previously-cached payload) or
        "computed fresh (seeded as last-known)" (the outage's first poll for
        this key family). The copy keeps the CACHED dict immutable — a marker
        must never leak into the entry another (post-recovery) poll serves.
        """
        out = dict(payload)
        out["revision_probe"] = _probe_outage_marker(served_last_known=served_last_known)
        return out

    def _probe_outage_marker(*, served_last_known: bool) -> str:
        """The ONE `revision_probe` degrade-marker string (JSON field + CSV row).

        Split out of :func:`_probe_outage_payload` (review fold) so the
        mapping-integrity CSV download can carry the SAME in-band staleness
        wording as the JSON routes — one marker vocabulary, never two that
        drift.
        """
        return (
            "outage — serving last-known"
            if served_last_known
            else "outage — computed fresh (seeded as last-known)"
        )

    def _store_outage_response(**extra: Any) -> JSONResponse:
        """The honest 503 degrade for a COLD store outage (nothing last-known).

        DEC-CACHE-SERVE-LAST-KNOWN-001, the `store_error_payload` precedent: when
        the instrument store is unreachable AND no last-known snapshot exists
        (:class:`UniverseStoreOutageError`), the route serves explicit error
        markers at 503 — never a raw 500 (the pre-fix per-poll traceback storm),
        never a fabricated empty universe (Golden Rule #1). SANITIZED: the wire
        carries only the stable generic markers — the full store fault detail is
        WARN-logged server-side by the cache's once-per-outage latch. ``extra``
        lets a route keep its envelope shape keys (e.g. ``kind``) beside the
        markers.
        """
        return JSONResponse(
            {
                "available": False,
                "error": "instrument store unavailable — retry shortly",
                "error_type": "store_outage",
                **extra,
            },
            status_code=503,
        )

    # ONE rate controller per dashboard session, shared across every pull
    # (DEC-DHAN-PERSIST-RATE-001) so the learned rate carries over instead of each
    # pull restarting at the 5 r/s ceiling and re-storming the vendor. Injectable
    # (DEC-RATE-METER-LIFECYCLE-001 test seam); ``None`` builds the production holder.
    _session_rate = session_rate if session_rate is not None else SessionRateController()
    # A SEPARATE rate controller for the standalone Dhan SPOT feed track
    # (DEC-DHAN-PULL-SURFACE-001): its own 5 r/s + 100k/day ceiling
    # (``build_dhan_live_controller``), so a Dhan pull's learned rate never mixes
    # with the Groww ``_session_rate`` and a Dhan+Groww pull can pace independently
    # (they write to DISJOINT lake trees — the top root vs ``feeds/dhan/``).
    _dhan_session_rate = SessionRateController(factory=build_dhan_live_controller)
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
            # KIND-AWARE since DEC-RATE-METER-LIFECYCLE-001: a running SWEEP on the
            # shared job reads False here (honest "idle" — zero vendor calls by
            # design), while _pull_live's duty-degrade probe stays sweep-inclusive.
            panel_pull_active=_rate_pull_active_probe(),
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
        # ONE more ADDITIVE key (DEC-OPERATOR-HEALTH-TRUTH-001): the honest
        # LAST-PULL OUTCOME block. ``flow_state == "done"`` is true-but-weak
        # evidence ("no pull running + a fresh tick") — the pre-fix operator
        # page rendered it as a green "completed cleanly", the SAME green a
        # disk-full / market-hours / quota HALT showed. The heartbeat now joins
        # this block: a halted pull renders its named degraded state, never the
        # clean-completion green. Every pre-existing key stays byte-identical.
        payload["last_pull"] = _pull_outcome_probe_resolved().to_dict()
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
        """GET /api/v1/sweep-progress — live brute-force sweep progress (DEC-SWEEP-WIRE-GA-001).

        The dashboard panel for the guarantee-&-assurance layer: percent / ETA /
        tuples-per-sec / survivors / skipped, populated by the supervisor's
        :class:`~brutex.engine.sweep_progress.SweepProgress` (wired into
        :func:`~brutex.engine.sweep_supervisor.run_supervised_grid`). Serves the
        freshest of the cross-process file bridge, the in-process push, or an idle
        snapshot, and never raises (see :meth:`DashboardState.sweep_progress_view`).

        HONEST envelope (DEC-SWEEP-PROGRESS-NONBLOCK-CACHE-001 — this route's
        prior "pure O(1) read that never touches the lake" claim was FALSE): the
        file-bridge arm READS ``<lake>/sweeps/progress/sweep-progress.json`` on
        the lake volume — a stalled 1.5 s read used to run ON the uvicorn event
        loop and moved a concurrent ``/api/v1/state`` poll 9.1 ms → 1305.4 ms.
        Now the whole view runs on the threadpool (never the loop) and the file
        arm rides the injected :class:`~brutex.dashboard.sweep_progress_cache.
        SweepProgressCache` — a 1 s-TTL memo whose refresh is deadline-bounded
        (0.5 s) with a non-blocking single-flight, so even the offloaded worker
        is bounded on a wedged volume (last-known payload served, labelled with
        the ADDITIVE ``progress_source`` / ``progress_probe_age_s`` keys — never
        a stale-as-fresh illusion). The in-process-push + idle arms stay pure
        in-memory, byte-identical, unstamped. Stays on the DEFAULT AnyIO pool
        (not the lake limiter): like ``/api/v1/pull/rate`` it is a
        deadline-guarded LIVENESS poll whose worker hold is capped per outage.
        """
        return JSONResponse(
            await run_in_threadpool(
                lambda: state.sweep_progress_view(file_read=_sweep_progress_cache.read)
            )
        )

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
        stats = await _run_lake(_collect_store_stats, store)
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
        rows = await _run_lake(
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
        series = await _run_lake(
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
        series = await _run_lake(
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

        DEC-DASHBOARD-EVENTLOOP-OFFLOAD-002: the cache's revision probe (a sync
        DuckDB ``COUNT`` + ``MAX``) — and the three base reads on a miss — run on a
        threadpool worker, NEVER the uvicorn event loop, so a stalled lake volume
        cannot freeze the whole dashboard.

        DEC-CACHE-SERVE-LAST-KNOWN-001 (the 8th keystone site): a store OUTAGE
        with a resident snapshot serves it labelled (the additive
        ``revision_probe`` marker on a COPY — the memoised dict stays clean);
        a COLD outage degrades to the honest 503 markers, never a raw 500.
        """
        store = _require_store()
        try:
            payload, served_last_known = await _run_lake(_universe_cache.summary_view, store)
        except UniverseStoreOutageError:
            return _store_outage_response()
        if served_last_known:
            return JSONResponse(_probe_outage_payload(payload, served_last_known=True))
        return JSONResponse(payload)

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
        as a 400 (the classifier's ``ValueError`` is raised before any caching;
        NO DuckDB exception subclasses ``ValueError``, so a store fault can never
        masquerade as a bad request).

        DEC-CACHE-SERVE-LAST-KNOWN-001 (the 8th keystone site): a store OUTAGE
        with a resident snapshot serves its rows labelled (the additive
        ``revision_probe`` marker on the route envelope); a COLD outage degrades
        to the honest 503 markers, never a raw 500 per poll (the pre-fix
        500-storm the scout reproduced).
        """
        store = _require_store()
        try:
            # DEC-DASHBOARD-EVENTLOOP-OFFLOAD-002: the cache's revision probe +
            # per-kind base reads run on a threadpool worker, never the event loop
            # (the classifier's ValueError for a bad kind still propagates out of
            # run_in_threadpool → 400, before any caching).
            rows, served_last_known = await _run_lake(
                lambda: _universe_cache.instruments_view(store, kind=kind)
            )
        except UniverseStoreOutageError:
            return _store_outage_response(instruments=[], kind=kind)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        payload: dict[str, Any] = {"instruments": rows, "kind": kind}
        if served_last_known:
            payload = _probe_outage_payload(payload, served_last_known=True)
        return JSONResponse(payload)

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

        def _held_universe() -> tuple[list[Instrument], list[str]]:
            """Read + classify the held single-stock universe off the event loop.

            DEC-DASHBOARD-EVENTLOOP-OFFLOAD-002: the three DuckDB store reads +
            the COMMON classifier run on a threadpool worker (built + used on the
            SAME thread so the store's RLock stays on one thread —
            DEC-DASHBOARD-FASTREAD-001), never the uvicorn event loop.
            """
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
                candidates,
                kind="stock",
                fno_eligible=eligible,
                reference_only_ids=reference_only_ids,
            )
            return candidates, [row["symbol"] for row in stock_rows]

        candidates, held_symbols = await _run_lake(_held_universe)

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
        result = await _run_lake(
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
        # DEC-DASHBOARD-EVENTLOOP-OFFLOAD-003: the symbol lookup is a synchronous
        # DuckDB read on the lake volume — run it on a worker (the #1622 doctrine)
        # so a slow/stalled volume can never freeze the loop at click time. The
        # unknown-symbol 400 stays OUT here (the api_universe_instruments idiom).
        instrument = await _run_lake(store.get, symbol)
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
            report = await _run_lake(
                pull_candles,
                instrument,
                lake_root=_resolve_pull_lake_root(instrument_store, dry_run=dry_run),
                year=year,
                from_date=from_date,
                to_date=to_date,
                intervals=resolve_fetch_intervals(spot_interval),
                dry_run=dry_run,
                rate_observer=make_rate_observer(state),
                # Meter lifecycle on this DIRECT (off-job) route — DEC-RATE-METER-
                # LIFECYCLE-001 round 2: a dry-run passes NO holder, so an operator's
                # ``?dry_run=true`` fired WHILE a live job pull runs (this route
                # bypasses the single-flight ``_pull_job``) can never evict that
                # pull's registered meter through ``register_pull_meter``'s dry-run
                # gate. The synthetic dry-run needs no session controller either
                # (``_live_rate_runtime`` ignores it when ``use_live_rate`` is
                # False), so the ``None`` arm is byte-identical apart from the
                # eviction it avoids. The REAL arm's register/clear pairing lives
                # INSIDE ``pull_candles`` (round 4 — ``_pull_meter_scope``): the
                # spine compare-and-clears the EXACT handle it registered, success
                # AND failure, so this route needs NO clear of its own. The prior
                # route-level unconditional ``finally: clear_meter()`` is REMOVED —
                # it evicted a concurrent JOB pull's LIVE meter whenever this route
                # 400'd BEFORE ``pull_candles`` ever registered (a malformed date),
                # leaving the job's F&O phase meterless for its remainder (the
                # round-4 MAJOR; its "a concurrent job pull re-registers at its own
                # build sites" claim was false — registration is once per phase
                # START, never periodic).
                session_rate=None if dry_run else _session_rate,
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
    # (DEC-DASHBOARD-FASTREAD-001). ``on_run_end`` clears the session's registered
    # CallMeter in the worker's identity-gated finally (DEC-RATE-METER-LIFECYCLE-001 —
    # the C0 cross-pull leak fix; the clear is UNCONDITIONAL, so a job finish CAN
    # evict a concurrent off-job direct pull's live meter — the widget degrades to
    # meter_present=False, never fabricates; blast radius disclosed at the
    # ``on_run_end`` param doc); together with ``pull_candles``' own paired
    # identity-gated clear (the direct ``/universe/pull`` route runs OFF-job — the
    # spine compare-and-clears the exact handle it registered, rounds 2/4),
    # "no in-process
    # pull running ⇒ no meter registered" is a process invariant, so a dead pull's
    # meter can never feed a later sampler tick.
    # Both jobs key their cross-process single-flight flock on the STORE-derived
    # WRITE root (`_job_lake_root_resolver` — the same root `_resolve_pull_lake_root`
    # gives their `_work` bodies), so a `brutex dashboard --lake-root X` launch with
    # a divergent/unset $BRUTEX_LAKE_ROOT still contends with a CLI pull/backfill
    # flocking X (DEC-GREEKS-BULK-REENRICH-001 fix round 2 — the lock root and the
    # write root can no longer diverge). ONE start-time exception on _pull_job:
    # the SWEEP route starts with ``cross_process_lock=False``
    # (DEC-SWEEP-LOCK-PULL-CONCURRENCY-001) because cmd_sweep_all owns its own
    # discipline on the SAME flock file — the G10 preload SH read-guard + the
    # G6 sweep single-flight; a job-level EX would self-conflict G10 (flock is
    # per open file description) and lock the daily pull/crossverify out for
    # the sweep's multi-day wall time.
    _pull_job = BackgroundPullJob(
        summary_cache=_summary_cache,
        on_run_end=_session_rate.clear_meter,
        lake_root_resolver=_job_lake_root_resolver(instrument_store),
    )

    # The standalone Dhan SPOT feed pull job (DEC-DHAN-PULL-SURFACE-001) — a
    # SEPARATE BackgroundPullJob so it never collides with Groww's ``_pull_job``.
    # BOTH cross-process guards key on DHAN identities
    # (DEC-DHAN-ACCOUNT-LOCK-ISOLATE-001): the lake flock on the DHAN WRITE root
    # (``feeds/dhan/`` — the ``_dhan_job_lake_root_resolver`` nesting, distinct
    # from Groww's top-root flock) AND the ACCOUNT flock on the Dhan CLIENT-ID
    # (``_dhan_account_key_resolver`` — pre-fix this defaulted to the GROWW
    # api-key resolver, so a wet Dhan pull took the Groww account lock and the
    # two vendors falsely serialised) — so a Dhan pull and a Groww pull genuinely
    # run concurrently (disjoint vendor accounts + disjoint lake trees) while two
    # Dhan pulls sharing the client-id serialise. Its meter clear rides the Dhan
    # session controller.
    _dhan_pull_job = BackgroundPullJob(
        summary_cache=_summary_cache,
        on_run_end=_dhan_session_rate.clear_meter,
        lake_root_resolver=_dhan_job_lake_root_resolver(instrument_store),
        account_key_resolver=_dhan_account_key_resolver,
    )

    # The standalone Dhan ROLLING-ATM option pull job (DEC-DHAN-ROLLING-OPTIONS-001) —
    # a SEPARATE BackgroundPullJob from the spot ``_dhan_pull_job`` so a rolling-ATM
    # option pull and a Dhan SPOT pull never collide in-process. Its cross-process
    # single-flight flock keys on the SAME DHAN WRITE root (``feeds/dhan/`` — the
    # ``_dhan_job_lake_root_resolver`` nesting), so it serialises with a concurrent
    # Dhan pull sharing the same account/lake (they write DISJOINT subtrees —
    # ``rolling_options/`` vs ``bars/`` — but share the one Dhan read-only account
    # rate budget). Its ACCOUNT flock likewise keys on the Dhan CLIENT-ID
    # (``_dhan_account_key_resolver``, DEC-DHAN-ACCOUNT-LOCK-ISOLATE-001 — never
    # the Groww api-key), so it contends with the sibling Dhan spot job (same
    # account budget) and never with a Groww pull. Its meter clear rides the
    # SHARED Dhan session controller.
    _dhan_option_pull_job = BackgroundPullJob(
        summary_cache=_summary_cache,
        on_run_end=_dhan_session_rate.clear_meter,
        lake_root_resolver=_dhan_job_lake_root_resolver(instrument_store),
        account_key_resolver=_dhan_account_key_resolver,
    )

    # The bulk Greeks v1→v2 backfill job (DEC-GREEKS-BULK-REENRICH-001) — a
    # SEPARATE BackgroundPullJob so its progress feed never collides with a
    # pull's. The ACCOUNT-keyed lock is skipped via the existing graceful-skip
    # path (`account_key_resolver` returning None — the backfill makes ZERO
    # vendor calls, so there is no account rate to protect); the WRITE-root
    # lake single-flight is KEPT for the WET run, so a backfill and a pull
    # against the SAME lake serialise (one lake-writer job at a time — the
    # honest posture; the per-partition flocks of DEC-LAKE-PARTITION-FLOCK-001
    # protect regardless). A DRY-RUN start skips the cross-process lock
    # entirely (`cross_process_lock=False` at the route — zero writes, the CLI
    # `--dry-run` lock-free posture). NO ``on_run_end`` meter clear here: the
    # backfill makes zero vendor calls and never registers a CallMeter, and the
    # clear is UNCONDITIONAL — wiring it would let a backfill finish evict a
    # concurrent pull's live meter (DEC-RATE-METER-LIFECYCLE-001 blast-radius
    # note). Owned by this app instance (no module global — CLAUDE.md §4).
    _greeks_backfill_job = BackgroundPullJob(
        summary_cache=_summary_cache,
        account_key_resolver=lambda: None,
        lake_root_resolver=_job_lake_root_resolver(instrument_store),
    )

    # The cross-verify producer job (DEC-CROSSVERIFY-PRODUCER-001, MIGRATION.md
    # #1776) — a THIRD BackgroundPullJob so its progress feed never collides
    # with a pull's or the backfill's. Its route ALWAYS starts it with
    # ``cross_process_lock=False``: the runner's own ``default_seams`` lock
    # seam takes the account→lake→lease trio itself (bounded wait-retry,
    # honest refusal), and a job-level second acquire of the SAME flock file
    # would self-conflict (flock is per open file description). The IN-process
    # single-flight (a second click) still applies. ``on_run_end`` clears the
    # shared session CallMeter exactly like ``_pull_job`` — the crossverify
    # pull leg is a REAL vendor pull through the SAME SessionRateController
    # (DEC-RATE-METER-LIFECYCLE-001 posture, same disclosed blast radius).
    _crossverify_job = BackgroundPullJob(
        summary_cache=_summary_cache,
        on_run_end=_session_rate.clear_meter,
        lake_root_resolver=_job_lake_root_resolver(instrument_store),
    )

    def _start_crossverify_producer(config: Any) -> bool:
        """Start the cross-verify producer job over *config* — the ONE start body.

        DEC-PULL-SCOPED-ALL-ONECLICK-001 (§11 COMMON): shared by the
        ``POST /api/v1/crossverify/run`` route AND the spot-only pipeline's
        crossverify TAIL, so the runner selection (the injected
        ``crossverify_runner`` test seam is observed by BOTH consumers), the
        job's ``_work`` verdict surfacing, and the lock/market-hours start
        kwargs can never drift onto two paths. Returns
        :meth:`BackgroundPullJob.start`'s verdict (``False`` = already
        running). Lock semantics (the phase-A handoff law): the job starts
        with ``cross_process_lock=False`` because the runner's own
        ``default_seams`` lock seam acquires the account→lake→lease trio
        itself with a bounded wait-retry (run.py step F) — a job-level second
        acquire would self-conflict; ``market_hours_exempt=True`` because the
        RUNNER owns the market-hours law at its step E.
        """

        def _run_producer(cfg: Any) -> Any:  # pragma: no cover — runs on the job thread
            """The production runner binding (tests inject crossverify_runner)."""
            from brutex.crossverify.run import default_seams, run_crossverify

            return run_crossverify(cfg, default_seams(cfg, session_rate=_session_rate))

        runner = crossverify_runner if crossverify_runner is not None else _run_producer

        def _work(on_progress: ProgressHook) -> object:
            """Background body: run the producer; surface the honest verdict.

            A non-zero exit verdict RAISES so the panel reads an honest ✕
            (the runner itself never raises for data/vendor/publish problems
            — it folds them into the verdict; the Telegram summary/refusal
            already fired inside the runner, so no double alert here).
            """
            report = runner(config)
            detail = f": {report.detail}" if report.detail else ""
            headline = f"crossverify {report.day.isoformat()} — {report.verdict}{detail}"
            if report.exit_code != 0:
                raise RuntimeError(headline)
            return {
                "note": (
                    f"{headline} · published {report.published} · "
                    f"already {report.skipped_already} · withheld {report.withheld} · "
                    f"nothing {report.nothing}"
                ),
                "verdict": report.verdict,
                "exit_code": report.exit_code,
            }

        return _crossverify_job.start(
            _work,
            total=0,
            detail=("cross-verify (DRY RUN)" if config.dry_run else "cross-verify → TickVault"),
            # The runner's default_seams lock seam owns the account→lake→lease
            # trio (bounded wait-retry) — a job-level acquire would self-conflict.
            cross_process_lock=False,
            # The runner owns the market-hours law at its step E (see docstring).
            market_hours_exempt=True,
        )

    def _default_crossverify_tail() -> CrossverifyTailOutcome:
        """The PRODUCTION crossverify tail (DEC-PULL-SCOPED-ALL-ONECLICK-001).

        Fired by the spot-only ``/pull-pipeline`` arm as the launcher's LAST
        step (report in hand, ledger durable). Resolves the SAME
        ``BRUTEX_CROSSVERIFY_*`` config surface the route resolves and starts
        the SAME ``_crossverify_job`` producer via
        :func:`_start_crossverify_producer` — never a nested inline run on the
        pipeline job thread (the runner's own bounded-wait lock trio absorbs
        the pull-flock handoff; ruling 8a). A CONFIG construction failure
        (junk ``BRUTEX_CROSSVERIFY_*`` env) is the ONLY production
        ``not_configured`` path on THIS binding — fail-SOFT, the spot pull is
        never blocked by a broken verify config (ruling 8). Seam construction
        (``default_seams``) happens later on the crossverify JOB thread, after
        this tail already returned ``started`` — a failure there surfaces as
        an honest job ✕, never ``not_configured`` (the CLI tail differs: its
        try-block wraps ``default_seams`` too — refuter round 4 H NIT-3).
        """
        from brutex.crossverify.config import resolve_crossverify_config

        lake_root = _resolve_pull_lake_root(instrument_store, dry_run=False)
        try:
            config = resolve_crossverify_config(lake_root=lake_root)
        except ValueError as exc:
            return CrossverifyTailOutcome(
                status="not_configured", human=f"not configured (ValueError: {exc})"
            )
        if not config.enabled:
            return CrossverifyTailOutcome(
                status="disabled", human="disabled (BRUTEX_CROSSVERIFY=off)"
            )
        if _start_crossverify_producer(config):
            return CrossverifyTailOutcome(
                status="started",
                human="producer started (poll /api/v1/crossverify/progress)",
            )
        return CrossverifyTailOutcome(status="already_running", human="already running")

    # The spot-only pipeline's tail binding: the injectable test seam wins,
    # PRODUCTION defaults to the REAL producer trigger above (never None —
    # "not configured" renders only on a genuine construction failure).
    _crossverify_tail: CrossverifyTail = (
        crossverify_tail if crossverify_tail is not None else _default_crossverify_tail
    )

    # The /api/v1/pull/rate stall gate's pull-run signal (DEC-RATE-STALL-PULL-GATE-001):
    # the production default reads THIS app's own single-flight job (`running` is an O(1)
    # lock-guarded bool), so the widget can never read "stalled" after the pull finished;
    # tests inject a deterministic probe. Resolved at request time via the route closure.
    _pull_active_probe: Callable[[], bool | None] = (
        pull_active_probe if pull_active_probe is not None else (lambda: _pull_job.running)
    )

    # RATE-ROUTE-ONLY pull signal (DEC-RATE-METER-LIFECYCLE-001): kind-aware so a
    # brute-force SWEEP on the shared single-flight job reads honest "idle" on the
    # PULL RATE widget instead of a red "stalled" for its 2-4-day run (audit C5).
    # _pull_active_probe / _pull_live DELIBERATELY stay sweep-INCLUSIVE — a sweep IS
    # a heavy in-process job and the cache duty-degrade floors (#920) must still
    # engage; "unifying" the two probes would resurrect either C5 or the #920
    # starvation. An injected pull_active_probe is used verbatim (bool | None
    # pass-through — the test seam bypasses the kind gate by design).
    _rate_pull_active_probe: Callable[[], bool | None] = (
        pull_active_probe
        if pull_active_probe is not None
        else (lambda: _pull_job.running_job_kind == JOB_KIND_PULL)
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
    # two-key holder (not a module-global — CLAUDE.md §4); each write assigns
    # BOTH keys on the job thread, the read is on the request thread (CPython
    # GIL → torn-state-free for the outcome probe's purposes: a mid-stamp read
    # at worst sees the OLD id with the NEW verdict or vice versa, and either
    # mismatch makes the probe honestly SKIP the verdict, never mis-colour).
    # `verdict` is None until the first pull lands; `pull_id` is the STAMPING
    # run's correlation id (DEC-OPERATOR-HEALTH-TRUTH-001, hostile-review M2:
    # the outcome probe consults the verdict ONLY when it matches the retained
    # snapshot's run — an un-timestamped stale verdict can never colour a
    # different run's outcome; "" = stamped outside any run, e.g. the pre-pull
    # credentials refusal, which correlates with the never-ran slot only).
    _last_pull_verdict: dict[str, Any] = {"verdict": None, "pull_id": ""}

    def _stamp_pull_verdict(verdict: dict[str, Any]) -> None:
        """Stamp *verdict* + the CURRENT run's correlation id into the holder.

        Called from the pull routes' _work closures, which run inside the
        job's ``pull_id_scope`` — so ``current_pull_id()`` is the run the
        verdict describes (``""`` at the pre-pull refusal gate, by design).
        ONE writer helper so no stamp site can forget the id half
        (DEC-OPERATOR-HEALTH-TRUTH-001, hostile-review M2).
        """
        from brutex.observability.correlation import current_pull_id

        _last_pull_verdict["verdict"] = verdict
        _last_pull_verdict["pull_id"] = current_pull_id()

    def _default_pull_outcome_probe() -> LastPullOutcome:
        """The honest LAST-PULL OUTCOME of THIS app's job slot (DEC-OPERATOR-HEALTH-TRUTH-001).

        Joins the two truths the app already records — the job's RETAINED last
        PULL-kind snapshot (``last_pull_state`` — live while a pull runs;
        sweep-proof between runs, so a halted pull's red outcome survives a
        later sweep — hostile-review M2/m5) and the last finished-pull verdict
        holder above, consulted ONLY when its stamped run id matches the
        retained snapshot's run — through the ONE pure classifier. O(1),
        in-memory, read-only.
        """
        return classify_retained_pull_outcome(
            _pull_job.last_pull_state(),
            verdict=_last_pull_verdict["verdict"],
            verdict_pull_id=_last_pull_verdict["pull_id"],
        )

    # Resolved probe (injection seam mirrors ``pull_active_probe``): the
    # ``/api/v1/pull/rate`` additive ``last_pull`` block AND the health hub's
    # pull lane both read THIS one callable — one truth source, two surfaces.
    _pull_outcome_probe_resolved: Callable[[], LastPullOutcome] = (
        pull_outcome_probe if pull_outcome_probe is not None else _default_pull_outcome_probe
    )

    # The unified health hub (DEC-OPERATOR-HEALTH-TRUTH-001): nightly chain +
    # backup dead-man from durable local state, the pull lane from the probe
    # above, doctor-daily + mutation honestly NOT WIRED (GitHub-Actions-only
    # gates with no local artifact — never a fabricated green). Injected for
    # deterministic tests; the production hub rides the same read-side lake
    # root resolver the rate bridge uses.
    _health_hub: HealthHub = (
        health_hub
        if health_hub is not None
        else HealthHub(
            lake_root_resolver=lambda: _rate_bridge_lake_root_or_none(instrument_store),
            pull_outcome_probe=_pull_outcome_probe_resolved,
        )
    )

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
            _stamp_pull_verdict(refusal.to_dict())
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

        def _resolve_selection() -> tuple[list[Instrument], str]:
            """Resolve the spot selection + its progress label — store reads only.

            DEC-DASHBOARD-EVENTLOOP-OFFLOAD-003 (the M16 launch-route class):
            ``resolve_pull_symbols`` runs one DuckDB ``store.get`` per token and
            ``_select_bulk_targets`` runs two whole-store reads — all of which
            sat ON the event loop, so a slow/stalled lake volume froze every
            concurrent poll the moment the operator clicked Pull. Runs on a
            threadpool worker; the ``HTTPException``s raised here propagate out
            of ``run_in_threadpool`` unchanged, and the ``ValueError`` → 400
            mapping stays OUTSIDE the offload (the #1622
            ``api_universe_instruments`` idiom).
            """
            if symbols is not None:
                selected = resolve_pull_symbols(symbols, _require_store().get)
                return selected, f"{len(selected)} symbols spot"
            if symbol is not None:
                instrument = _require_store().get(symbol)
                if instrument is None:
                    raise HTTPException(
                        status_code=400,
                        detail=f"unknown security_id {symbol!r} — not in the instrument store",
                    )
                return [instrument], f"{symbol} spot"
            if kind is not None:
                return _select_bulk_targets(kind), f"{kind} spot"
            raise HTTPException(
                status_code=400,
                detail="pull-all requires ?symbols=..., ?symbol=... or ?kind=...",
            )

        try:
            instruments, detail = await _run_lake(_resolve_selection)
        except ValueError as exc:
            # resolve_pull_symbols' fail-CLOSED empty/unknown-selection refusal
            # (byte-identical to the pre-offload per-arm catch — the other arms
            # raise HTTPException directly, which passes through untouched).
            raise HTTPException(status_code=400, detail=str(exc)) from exc
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
                # DEC-GROWW-MARKET-HOURS-GUARD-001: mid-run halt if the pull crosses
                # into the NSE market-hours window (the shared-account belt). The env
                # kill switch is parsed ONCE here (fail-loud); the bound predicate then
                # reads the live clock only (no per-window env re-parse → a junk env
                # set mid-pull can never crash a long pull).
                market_closed=resolve_market_closed(),
            )
            _stamp_pull_verdict(report.verdict.to_dict())
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
        started = _pull_job.start(
            _work,
            total=unit_total,
            detail=detail,
            ledger_synced=True,
            # DEC-GROWW-MARKET-HOURS-GUARD-001: a --dry-run pull uses a synthetic
            # adapter (no Groww API call), so it is EXEMPT from the market-hours gate.
            market_hours_exempt=dry_run,
        )
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

        def _resolve_selection() -> tuple[list[Instrument], str, frozenset[int]]:
            """Resolve the metadata selection + the reference-only skip set.

            DEC-DASHBOARD-EVENTLOOP-OFFLOAD-003 (the M16 launch-route class):
            the per-token ``store.get``s, ``_select_bulk_targets``' two
            whole-store reads AND the ``reference_only_security_ids`` scan are
            all synchronous DuckDB reads that sat ON the event loop. One
            threadpool closure resolves everything the launch needs;
            ``HTTPException``s propagate unchanged, the ``ValueError`` → 400
            mapping stays OUTSIDE (the #1622 ``api_universe_instruments``
            idiom).

            The reference-only set skips REF underlyings BEFORE the chain
            pull's network calls (DEC-SKIP-REF-METADATA-001): a REFERENCE_ONLY
            underlying — a spot-only index (INDIAVIX / MIDCAP50 / NIFTY100, …)
            or a non-F&O NTM constituent (DEC-NTM-GROWW-REPOINT-001 /
            DEC-NON-OPTION-INDEX-REFTAG-001) — has NO F&O chain, so
            ``get_expiries`` would return empty and persist nothing (already
            SAFE), but it still costs one wasted empty network call. The
            store's ``reference_only_security_ids`` is the SAME set the
            /universe REF badge uses; the caller partitions on it so a
            mistaken REF selection is a TRUE zero-call no-op — reported, never
            silently dropped; TRADABLE behaviour byte-unchanged.
            """
            if symbols is not None:
                selected = resolve_pull_symbols(symbols, _require_store().get)
                label = f"{len(selected)} symbols metadata"
            elif symbol is not None:
                instrument = _require_store().get(symbol)
                if instrument is None:
                    raise HTTPException(
                        status_code=400,
                        detail=f"unknown security_id {symbol!r} — not in the instrument store",
                    )
                selected = [instrument]
                label = f"{symbol} metadata"
            elif kind is not None:
                selected = _select_bulk_targets(kind)
                label = f"{kind} metadata"
            else:
                raise HTTPException(
                    status_code=400,
                    detail="pull-expiries-contracts requires ?symbols=..., ?symbol=... or ?kind=...",
                )
            return selected, label, _require_store().reference_only_security_ids()

        try:
            instruments, detail, reference_only_ids = await _run_lake(_resolve_selection)
        except ValueError as exc:
            # resolve_pull_symbols' fail-CLOSED refusal (byte-identical to the
            # pre-offload per-arm catch).
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if dry_run:
            # Explicit synthetic run: the progress title must SAY so loudly
            # (DEC-PULL-LOUD-CREDENTIAL-REFUSAL-001 — never look like a real pull).
            detail = label_synthetic(detail)
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
                    # Durable &-form winner map (DEC-FORM-WINNER-PERSIST-001):
                    # keyed by the PRIMARY underlying inside the pull; a dry-run
                    # threads store=None (base-identical, nothing persisted).
                    form_preference=PersistentSymbolFormPreference(
                        None if dry_run else instrument_store
                    ),
                )
            return report.to_dict()

        # ledger_synced=True mirrors the spot pull: the bar's completed/total are driven
        # by the absolute sync_from_ledger resync above (per cell + per contract job),
        # NOT by the per-pair 4-arg hook — so it fills 0→100% instead of freezing
        # (DEC-METADATA-LIVE-PROGRESS-001). The initial total is the instrument count, a
        # placeholder the first cell-settle resync re-scales to cells (then cells+jobs).
        started = _pull_job.start(
            _work,
            total=len(instruments),
            detail=detail,
            ledger_synced=True,
            # DEC-GROWW-MARKET-HOURS-GUARD-001: --dry-run is vendor-free → EXEMPT.
            market_hours_exempt=dry_run,
        )
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

    async def _start_composite_historical_pull(
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

        DEC-DASHBOARD-EVENTLOOP-OFFLOAD-003: the selection resolution (one DuckDB
        ``store.get`` per token + the ``reference_only_security_ids`` scan) runs on
        a threadpool worker — it sat ON the event loop, freezing every concurrent
        poll at click time on a slow/stalled lake volume. The ``ValueError`` → 400
        mapping stays OUTSIDE the offload (the #1622 idiom).
        """
        if symbols is None:
            raise HTTPException(
                status_code=400,
                detail="enumerate=true requires ?symbols=a,b,c (the multi-select)",
            )
        selected_symbols = symbols

        def _resolve_composite_selection() -> tuple[list[Instrument], list[int], frozenset[int]]:
            """Resolve selection + year grid + REF skip set — store reads only."""
            resolved = resolve_pull_symbols(selected_symbols, _require_store().get)
            year_grid = _resolve_metadata_years(from_date, to_date)
            return resolved, year_grid, _require_store().reference_only_security_ids()

        try:
            instruments, years, reference_only_ids = await _run_lake(_resolve_composite_selection)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
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
                    # ONE durable &-form winner map shared by BOTH composite phases
                    # (DEC-FORM-WINNER-PERSIST-001); dry-run threads store=None.
                    form_preference=PersistentSymbolFormPreference(
                        None if dry_run else instrument_store
                    ),
                )
            return report.to_dict()

        composite_detail = f"{tradable_count} composite"
        if dry_run:
            # Explicit synthetic run: the progress title must SAY so loudly
            # (DEC-PULL-LOUD-CREDENTIAL-REFUSAL-001 — never look like a real pull).
            composite_detail = label_synthetic(composite_detail)
        started = _pull_job.start(
            _work,
            total=tradable_count,
            detail=composite_detail,
            # DEC-GROWW-MARKET-HOURS-GUARD-001: --dry-run is vendor-free → EXEMPT.
            market_hours_exempt=dry_run,
        )
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
            return await _start_composite_historical_pull(
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

        def _resolve_direct_selection() -> tuple[list[ContractRow], dict[str, bool]]:
            """Resolve the direct grammar to registry rows + the six-step kind map.

            DEC-DASHBOARD-EVENTLOOP-OFFLOAD-003 (the M16 launch-route class — the
            WORST offender): ``RegistryStore.open`` + ``_resolve_historical_contracts``
            (a full ``list_contracts`` over the whole cached chain) +
            ``_underlying_kind_map`` over BOTH ``_select_bulk_targets`` reads all ran
            ON the event loop, so a click on "Pull historical" froze every concurrent
            dashboard request for the whole resolution on a slow/stalled volume. The
            selection is still resolved BEFORE the job starts, so an empty/no-match
            selection stays an immediate honest 400 (raised by the caller — never a
            fabricated contract).

            Six-step order on the DIRECT-selection grammars too
            (DEC-PULL-SIX-STEP-ORDER-001 review fix): this route's
            ?contract/?contracts/?underlying/?kind/?all paths ran the legacy 2-bucket
            FUT-then-OPT order because no ``underlying_kinds`` was threaded —
            reproducing the motivating 2026-07-05 inversion (stock options before
            index options) on a real vendor pull. Build the classification map over
            EVERY admitted underlying (both kinds) with the SAME resolved-key builder
            the composite uses; a selection whose underlying is somehow absent orders
            stock-side (WARNed + counted inside the spine, never dropped). Built at
            launch time on the worker, captured by the job closure below; skipped
            entirely for an empty selection (the pre-offload order — the map was
            never built when the 400 fired first).
            """
            with RegistryStore.open(registry_path) as registry:
                rows = _resolve_historical_contracts(
                    registry,
                    contract=contract,
                    contracts=contracts,
                    underlying=underlying,
                    expiry_date=expiry_date,
                    kind=kind,
                    all_cached=all,
                    select_kind=_select_bulk_targets,
                )
            if not rows:
                return [], {}
            return rows, _underlying_kind_map(
                _select_bulk_targets("index") + _select_bulk_targets("stock")
            )

        try:
            selected, underlying_kinds = await _run_lake(_resolve_direct_selection)
        except RegistryError as exc:
            # DEC-DASHBOARD-EVENTLOOP-OFFLOAD-003 review fold: offloading this
            # resolution WIDENED the concurrency window on ``RegistryStore.open``
            # — open is write-on-open (schema DDL + an ``INSERT OR REPLACE INTO
            # schema_meta`` stamp), so two simultaneous launch clicks that both
            # reach the open can now race a DuckDB Transaction/Constraint
            # exception → ``RegistryError`` (a BruteXError, NOT a ValueError),
            # which pre-offload the on-loop serialization made unreachable.
            # An honest 503 "busy — retry" instead of a misleading 500 (the
            # registry read routes' RegistryError-degrade precedent; the click
            # is retryable — nothing was launched, nothing was corrupted).
            raise HTTPException(
                status_code=503,
                detail=f"registry busy — retry (concurrent open on registry.duckdb): {exc}",
            ) from exc
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
                    # DEC-PULL-SIX-STEP-ORDER-001 (review fix): the direct-selection
                    # grammars run the SAME strict steps 3-6 order as the composite.
                    underlying_kinds=underlying_kinds,
                    # Durable &-form winner map on the direct-selection grammars too
                    # (DEC-FORM-WINNER-PERSIST-001 review round-2 fix: this seam was
                    # MISSED, so ?underlying=M&M re-paid the wasted plain-form fetch
                    # and never read/persisted a winner); dry-run threads store=None.
                    form_preference=PersistentSymbolFormPreference(
                        None if dry_run else instrument_store
                    ),
                )
            # Consecutive-failure health over the post-pull Greeks pass
            # (DEC-GREEKS-ENRICH-OBSERVE-001): a dry-run's `None` verdict is
            # ignored inside `observe`, so a synthetic pull never moves the streak.
            _greeks_health.observe(report.greeks_enrich_ok, error=report.greeks_enrich_error)
            return report.to_dict()

        started = _pull_job.start(
            _work,
            total=len(selected),
            detail=detail,
            # DEC-GROWW-MARKET-HOURS-GUARD-001: --dry-run is vendor-free → EXEMPT.
            market_hours_exempt=dry_run,
        )
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
        scope: str | None = None,
        auto_arm: bool = False,
    ) -> dict[str, Any]:
        """The ONE-CLICK pull(+movers) pipeline launcher — the ``/pull-pipeline`` body.

        *auto_arm* (DEC-PULL-SCOPED-ALL-ONECLICK-001 round-5 fold — the
        self-arming button semantics: first click = run now if allowed + arm
        forever) additionally ARMS the wired
        :class:`~brutex.dashboard.auto_pull_scheduler.AutoPullScheduler`
        BEFORE the launch attempt, so a market-hours/contention-REFUSED click
        still arms (the response then says both: ``started: false`` + the
        armed ``auto_pull`` block). Refused loudly when it cannot mean what
        it says: 400 on a non-spot-only scope or a ``dry_run`` (a dry-run
        must never arm the daily REAL pull), 503 when no scheduler is wired.
        The boot-time resume never passes it (an intent re-fire must not
        re-arm a deliberately-disarmed scheduler).

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

        Backs the TWO /universe buttons (DEC-PULL-SCOPED-ALL-ONECLICK-001
        round-6 two-button split): "▶ Pull Spot" (``scope=spot-only``,
        whole-universe, ``auto_arm=1``) and "▶ Pull F&O" (``scope=fno-only``,
        selection/date-aware, manual only) — while the bare ``scope=full``
        default stays the API-reachable full chain, byte-identical, for the
        nightly binding + old intents + external callers (the
        DEC-ONE-CLICK-PIPELINE-001 / DEC-PULL-SELECTED-PIPELINE-001 semantics
        live on at the ROUTE even though the UI no longer offers the combined
        button). Precedence ``symbols`` > ``symbol`` >
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
            # The strict post-step gate (DEC-PULL-SIX-STEP-ORDER-001): resolved
            # ONCE, EAGERLY, at launch — a garbage BRUTEX_PIPELINE_STRICT_GATE
            # value is a clean 400 here, never a mid-pull 500 on the job thread.
            strict_gate = strict_gate_enabled()
            # The pull SCOPE token (DEC-PULL-SCOPED-ALL-ONECLICK-001): absent /
            # blank ⇒ the byte-identical full chain; "spot-only" swaps the two
            # F&O phase closures for the no-op reports below; an unknown token
            # is a clean 400 (a typo must never widen or narrow a vendor pull).
            scope_token = normalize_pull_scope(scope)
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
        spot_only = scope_token == SCOPE_SPOT_ONLY
        # The round-6 two-button split (DEC-PULL-SCOPED-ALL-ONECLICK-001):
        # fno-only is the exact mirror — the SPOT phase becomes the honest
        # no-op report, metadata + F&O historical run for REAL, the prebuild
        # excludes the spot phase, only the futures/options movers warm, the
        # crossverify tail never fires (Spot-exclusive by ruling) and the
        # auto-pull scheduler never arms/fires it (auto_arm 400s below).
        fno_only = scope_token == SCOPE_FNO_ONLY
        # ── Self-arming daily auto-pull (round-5 fold) ────────────────────
        # Validate + ARM before the launch attempt: a refused start (market
        # hours / contention) must still leave the operator armed — the
        # response confirms both. `armed_scheduler` narrows the Optional once
        # (auto_arm=True with no scheduler already 503'd).
        armed_scheduler: AutoPullScheduler | None = None
        if auto_arm:
            if scope_token != SCOPE_SPOT_ONLY:
                raise HTTPException(
                    status_code=400,
                    detail="auto_arm requires scope=spot-only (the self-arming "
                    "Pull-EVERYTHING-spot-only button semantics)",
                )
            if dry_run:
                raise HTTPException(
                    status_code=400,
                    detail="auto_arm requires a real run — a dry-run must never arm "
                    "the daily auto-pull",
                )
            if auto_pull_scheduler is None:
                raise HTTPException(
                    status_code=503,
                    detail="auto-pull scheduler unavailable (not wired for this app) — "
                    "run the pull without auto_arm",
                )
            armed_scheduler = auto_pull_scheduler
        if armed_scheduler is not None:
            armed_scheduler.arm()
        # The run's LAUNCH day (IST) — a post-close run crossing midnight
        # marks THIS day complete, never the next (the scheduler's
        # once-per-day guard must not skip the following day's pull).
        launch_day = now_ist().date()
        _LOG.info(
            "universe.pull_pipeline.requested",
            targets=len(targets),
            tradable=len(tradable),
            target_source=target_source,
            years=years,
            dry_run=dry_run,
            strict_gate=strict_gate,
            scope=scope_token,
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

            # ONE durable &-form winner map for the WHOLE pipeline run
            # (DEC-FORM-WINNER-PERSIST-001): the metadata phase's %26 win reorders
            # the historical phase's very first fetch for that underlying (both key
            # by the PRIMARY underlying), and the schema_meta rows survive across
            # pulls/processes. A dry-run threads store=None (base-identical).
            form_preference = PersistentSymbolFormPreference(None if dry_run else instrument_store)

            # DEC-GROWW-MARKET-HOURS-GUARD-001: resolve the env kill switch ONCE, here
            # at pull start (fail-loud on junk), and reuse the SAME bound clock-only
            # predicate across every phase of this pipeline run — the spot spine's
            # per-window belt (_run_spot), the metadata phase's per-cell belt
            # (_run_metadata → pull_expiries_contracts), and the sequencer's
            # between-phase belt (run_full_pipeline). So a junk BRUTEX_MARKET_HOURS_GUARD
            # set MID-pull can never crash a long run with a per-call ValueError.
            # EVERY scoped arm (spot-only AND — round-6 — fno-only) additionally
            # composes the Muhurat session belt
            # (DEC-PULL-SCOPED-ALL-ONECLICK-001 — the trading-day-keyed guard is
            # structurally blind to a holiday-listed Muhurat session; composed,
            # the guard module untouched; the belt honors the SAME
            # BRUTEX_MARKET_HOURS_GUARD kill switch — guard off ⇒ belt off,
            # hostile-review r1 ruling; wiring frozen-clock-pinned by
            # test_scope_spot_only_market_closed_is_composed_muhurat_belt —
            # the R4 M6 mutant killer — plus its round-6 fno-only twin). The
            # full-scope arm keeps the bare resolve byte-identically (the
            # Muhurat blindness there is the named follow-up
            # DEC-PIPELINE-MUHURAT-BELT-001).
            _market_closed = (
                compose_market_closed(resolve_market_closed())
                if scope_token != SCOPE_FULL
                else resolve_market_closed()
            )

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
                    # DEC-GROWW-MARKET-HOURS-GUARD-001: mid-run halt if the pull
                    # crosses into the NSE market-hours window (shared-account belt).
                    market_closed=_market_closed,
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
                        form_preference=form_preference,
                        # DEC-GROWW-MARKET-HOURS-GUARD-001: a metadata phase CROSSING
                        # 08:45 halts mid-phase (per-cell belt), exactly like the
                        # spot/historical spines — dry-run threads None (never gated).
                        market_closed=None if dry_run else _market_closed,
                    )

            def _run_historical(prior_steps: PriorStepsVerdict) -> CompositePullReport:
                # DEC-PULL-SIX-STEP-ORDER-001: the sequencer's spot+metadata
                # cleanliness verdict rides into the composite → the Greeks seam,
                # so the strict barrier can withhold the enrichment there.
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
                        form_preference=form_preference,
                        prior_steps=prior_steps,
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
                            # force_fresh (DEC-LATENCY-HONESTY-BATCH-001 review
                            # fold): now that the production movers cache is
                            # FLOORED, the still-`running` job's own pull-active
                            # floor could serve a stale pre-pull entry back into
                            # this warm re-seed — the exact class the discovery
                            # warm already guards (warm_discovery_memos), so the
                            # movers warm mirrors its documented pattern.
                            force_fresh=True,
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
                # The stored-master kind map + its content token (review round 1):
                # thread the SAME kinds the route computes with AND fold the SAME
                # master token into the candidate key, so a warmed entry (a) carries
                # CORRECT `underlying_kind` stamps (previously the warm computed with
                # the default `underlying_kinds=None`, stamping everything unknown —
                # an Indices/Stocks click on a freshly-warmed coordinate returned
                # EMPTY rows) and (b) is actually HIT by the route's composite key
                # (key parity, CLAUDE.md §11 COMMON).
                underlying_kinds, kinds_token = _underlying_kinds_cached(_require_store())

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
                            )
                            + f"|m={kinds_token}",
                            generation=gen,
                            compute=lambda: compute_futures_candidates(
                                lake_root=lake_root,
                                interval=DEFAULT_INTERVAL,
                                date=warm_date,
                                time=None,
                                exchange=exch,
                                expiry_tier=tier,
                                datasets=warm_datasets,
                                underlying_kinds=underlying_kinds,
                            ),
                            # force_fresh — the warm re-seed must never be served
                            # its own pull-active floor's stale pre-pull entry
                            # (DEC-LATENCY-HONESTY-BATCH-001; see the spot job).
                            force_fresh=True,
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
                # The stored-master kind map + its content token (review round 1 —
                # see the futures warm above): correct stamps + route key parity.
                underlying_kinds, kinds_token = _underlying_kinds_cached(_require_store())

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
                            )
                            + f"|m={kinds_token}",
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
                                underlying_kinds=underlying_kinds,
                            ),
                            # force_fresh — the warm re-seed must never be served
                            # its own pull-active floor's stale pre-pull entry
                            # (DEC-LATENCY-HONESTY-BATCH-001; see the spot job).
                            force_fresh=True,
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
                        # SPOT-ONLY scope rides an EMPTY F&O cascade
                        # (DEC-PULL-SCOPED-ALL-ONECLICK-001): a lake already
                        # holding F&O candles is NOT re-derived by a spot-only
                        # run — the FUTURES/OPTIONS prebuild phases report the
                        # existing honest empty notes.
                        cascade_or_error=lambda: build_fno_cascade(
                            () if spot_only else list_fno_candle_datasets(lake_root)
                        ),
                        materialize=_materialize,
                        source="pipeline",
                        exclude_symbols=exclude_symbols,
                        # FNO-ONLY scope (round-6 two-button split): the SPOT
                        # prebuild phase is a REPORTED no-op — the exact
                        # spot-exclusion mirror of the empty-cascade arm above
                        # (a spot lake untouched by this pull is not re-derived;
                        # the F&O cascade phases run for real).
                        include_spot=not fno_only,
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
                    # SPOT-ONLY scope (DEC-PULL-SCOPED-ALL-ONECLICK-001): swap
                    # EXACTLY the two F&O phase closures for the no-op report
                    # builders (they pass the UNCHANGED sequencer gates
                    # vacuously) and honestly OMIT the futures/options movers
                    # warmers (an absent segment, never a fabricated 0).
                    # _run_spot is bound kwarg-for-kwarg IDENTICALLY on both
                    # arms; the sequencer itself is byte-untouched.
                    # The no-op builders echo the LAUNCH's dry_run flag
                    # (refuter round 1, r2 NIT-6 — a dry-run payload's nested
                    # sub-reports must never read ``dry_run: false``).
                    # FNO-ONLY scope (round-6 two-button split): the exact
                    # mirror — swap EXACTLY the spot phase closure for the
                    # no-op report builder (it passes the UNCHANGED
                    # spot_phase_ok gate vacuously) and honestly OMIT the
                    # spot movers warmer; metadata + historical are bound
                    # kwarg-for-kwarg IDENTICALLY to the full arm.
                    report = run_full_pipeline(
                        run_spot=(
                            (lambda: fno_only_spot_report(dry_run=dry_run))
                            if fno_only
                            else _run_spot
                        ),
                        run_metadata=(
                            (lambda: spot_only_metadata_report(dry_run=dry_run))
                            if spot_only
                            else _run_metadata
                        ),
                        run_historical=(
                            (lambda _prior: spot_only_historical_report(dry_run=dry_run))
                            if spot_only
                            else _run_historical
                        ),
                        warm_spot_movers=None if fno_only else _warm_spot_movers,
                        warm_futures_movers=None if spot_only else _warm_futures_movers,
                        warm_options_movers=None if spot_only else _warm_options_movers,
                        run_prebuild=_prebuild_timeframes,
                        strict_gate=strict_gate,
                        # DEC-GROWW-MARKET-HOURS-GUARD-001: if this pipeline pull
                        # reaches the sequencer inside the NSE market-hours window,
                        # skip the whole vendor chain (spot/metadata/historical), and
                        # halt between phases if it crosses in. ONE bound predicate,
                        # env parsed once at pull start (see _market_closed above).
                        market_closed=_market_closed,
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
                # The fno-only arm's spot report is a DESIGNED no-op — its
                # vacuous "clean" verdict must never clobber the last REAL
                # pull's verdict surface (the B H1 honesty law's spot axis).
                if report.spot is not None and not fno_only:
                    _stamp_pull_verdict(report.spot.verdict.to_dict())
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
                result = report.to_dict()
                if spot_only:
                    # The launcher's LAST step (DEC-PULL-SCOPED-ALL-ONECLICK-001
                    # ruling 8a): fire the crossverify TAIL — the production
                    # binding START-only triggers the landed _crossverify_job
                    # producer, whose runner acquires its OWN lock trio on ITS
                    # worker thread with the bounded wait-retry (never nested
                    # inside our still-held pull single-flight) — then rewrite
                    # the note through the ONE surgery law so the finish detail
                    # (rail 11, 500-char cap) carries the honest spot-only +
                    # Greeks-not-run + cross-verify text. Fail-soft by fence:
                    # a broken tail never fails the successful pull.
                    tail_outcome = run_crossverify_tail(
                        _crossverify_tail,
                        dry_run=dry_run,
                        stopped_after=report.stopped_after,
                    )
                    result["note"] = build_spot_only_note(report.note, tail_outcome)
                    if auto_pull_scheduler is not None and not dry_run:
                        # The scheduler's completion hook (round-5 fold): EVERY
                        # real spot-only run — manual click, boot resume, and
                        # scheduler-fired alike — reports its LAUNCH-day
                        # verdict, so a successful manual run counts as the
                        # day's auto pull (no redundant re-fire) while a
                        # failed one marks nothing. Guarded: the convenience
                        # scheduler must never fail a finished pull.
                        try:
                            auto_pull_scheduler.note_run_complete(
                                ok=bool(report.ok), run_date=launch_day, source="pipeline"
                            )
                        except Exception as note_exc:  # pylint: disable=broad-exception-caught  # never fail the pull
                            _LOG.warning(
                                "auto_pull.note_complete_failed",
                                error=str(note_exc),
                                error_type=type(note_exc).__name__,
                            )
                elif fno_only:
                    # Round-6 two-button split: the fno-only note surgery ONLY
                    # — deliberately NO crossverify tail (Spot-exclusive by
                    # ruling; the producer verifies spot+futures files a SPOT
                    # pull settles) and NO note_run_complete (an F&O pull must
                    # never count as the day's spot auto-pull).
                    result["note"] = build_fno_only_note(report.note)
                return result
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
        prior_intent: PullIntent | None = None
        # Intent-transaction mutex (refuter round 4 G-1 / R4-1,
        # DEC-PULL-SCOPED-ALL-ONECLICK-001): the snapshot → persist → start()
        # → restore span below is ONE transaction over the durable intent
        # file — two concurrent launcher threads (two tabs / button+curl / a
        # boot-resume racing a click; the route offloads via run_in_threadpool
        # so requests are genuinely concurrent) interleaving those steps could
        # defeat the round-3 restore (a stale prior=None snapshot
        # orphan-clearing the RUNNING winner's freshly-persisted intent; a
        # second refused click re-installing the first's clobber). The whole
        # span therefore serializes under the store's process-local
        # launch_mutex. Holding it across start() is SAFE: start() never
        # WAITS on a lock (market-hours gate / in-process flag / LOCK_NB
        # flock refusals all return False; on success the worker is spawned
        # and start() returns) and the worker thread never acquires this
        # mutex (its on_finally is the plain store.clear). HONEST hold
        # envelope (refuter round 5, J-NIT-1): "non-blocking" is about
        # lock-WAITS, not I/O latency — the held span synchronously does the
        # intent-file load/persist/restore + two lock-file open() syscalls
        # (not deadline-wrapped; unbounded on a wedged lake volume), the SSM
        # credential read (boto3 default timeouts — minutes on a blackholed
        # AWS route) and the lease enter/refresh (each run_with_deadline-
        # bounded ≈5 s, ≤~10 s total). So a queued sibling click waits that
        # span instead of the pre-mutex INSTANT in-process refusal — healthy
        # ≈ sub-second, latency-only, same fault domain as its own I/O; the
        # alternative (the G-1/R4-1 intent races) was strictly worse. A
        # dry_run enters the mutex too (it persists/restores nothing, but
        # its own start() still pays the same held-span I/O); store-less
        # apps skip it — there is no intent state to serialize.
        with pull_intent_store.launch_mutex if pull_intent_store is not None else nullcontext():
            if pull_intent_store is not None and not dry_run:
                # Best-effort write (a failed write is logged, never blocks the
                # pull); the clear hook is wired for EVERY real pull regardless —
                # a completing real pull supersedes any lingering older intent.
                # NOTE: the resume red-banner auto-clear does NOT live here — it
                # fires AFTER start() below, gated on `started`
                # (DEC-RESUME-BANNER-AUTOCLEAR-001, round-3 hostile-review fold:
                # a refused start launches nothing and must not drop the banner).
                # Snapshot the CURRENT on-disk intent FIRST so a REFUSED start can
                # put it back (refuter round 3 F-1, DEC-PULL-SCOPED-ALL-ONECLICK-001
                # — the pre-start persist otherwise CLOBBERS a RUNNING pull's
                # intent, and a crash in that window resumes the WRONG scope).
                prior_intent = pull_intent_store.load()
                persist_intent_for_launch(
                    pull_intent_store,
                    dry_run=dry_run,
                    symbols=symbols,
                    symbol=symbol,
                    from_date=from_date,
                    to_date=to_date,
                    restarts=intent_restarts,
                    # Non-default scope persists so a crash-resume re-fires the SAME
                    # scope — a spot-only pull must never resume as a full-scope F&O
                    # pull (DEC-PULL-SCOPED-ALL-ONECLICK-001; None = full, the
                    # optional-field back-compat template).
                    scope=scope_token if scope_token != SCOPE_FULL else None,
                )
                on_finally = pull_intent_store.clear
            started = _pull_job.start(
                _work,
                total=len(targets),
                detail=pipeline_detail,
                ledger_synced=True,
                on_finally=on_finally,
                # DEC-GROWW-MARKET-HOURS-GUARD-001: --dry-run is vendor-free → EXEMPT.
                market_hours_exempt=dry_run,
            )
            if pull_intent_store is not None:
                # Banner auto-clear (DEC-RESUME-BANNER-AUTOCLEAR-001): retire any
                # stale resume red banner ONLY when the real pull actually started
                # — start() acquired the in-process + cross-process guards and
                # spawned the worker. A refused start (contention / foreign-host
                # lease) keeps the CRITICAL crash-loop banner up; a dry_run never
                # touches it (both gates live inside the helper).
                clear_banner_after_start(pull_intent_store, dry_run=dry_run, started=started)
                # Refused-start intent RESTORE (refuter round 3 F-1): the persist
                # above ran BEFORE start() for pre-crash durability, so a REFUSED
                # start (a pull already RUNNING here, flock contention, or the
                # foreign-host lease) clobbered the running pull's on-disk intent
                # — put the prior snapshot back (prior=None ⇒ clear the orphan;
                # already-cleared-by-the-winner ⇒ leave absent). A started or
                # dry-run launch restores nothing (gates live in the helper).
                restore_intent_after_refused_start(
                    pull_intent_store, prior=prior_intent, dry_run=dry_run, started=started
                )
        payload: dict[str, Any] = {
            "started": started,
            "targets": len(targets),
            "total": len(targets),  # the JS title reads body.total for the first render
            "tradable": len(tradable),
            "target_source": target_source,  # "selection" / "single" / "universe"
            "years": years,
            "dry_run": dry_run,
            # ADDITIVE (DEC-PULL-SCOPED-ALL-ONECLICK-001): the normalized scope
            # token — every pre-existing key above is byte-identical (pinned).
            "scope": scope_token,
        }
        if spot_only and auto_pull_scheduler is not None:
            # ADDITIVE, spot-only arm ONLY (round-5 fold — the default/full
            # payload stays exactly the pinned shape above): the armed state +
            # last auto outcome + honest next-eligible description, so a
            # market-hours-refused arming click's response can say BOTH
            # "started: false" AND "armed — first auto run <when>".
            payload["auto_pull"] = auto_pull_scheduler.status()
        return payload

    @app.post("/api/v1/universe/pull-pipeline")
    async def api_universe_pull_pipeline(
        symbols: str | None = Query(default=None),
        symbol: str | None = Query(default=None),
        from_date: str | None = Query(default=None),
        to_date: str | None = Query(default=None),
        dry_run: bool = Query(default=False),
        scope: str | None = Query(default=None),
        auto_arm: bool = Query(default=False),
    ) -> JSONResponse:
        """POST /api/v1/universe/pull-pipeline — the ONE-CLICK pull(+movers) pipeline.

        ``auto_arm=1`` (spot-only scope only — the self-arming button sends
        it) additionally arms the daily post-market auto-pull scheduler
        BEFORE the launch attempt; see :func:`_launch_pipeline`.

        Thin delegator over :func:`_launch_pipeline` (which carries the full
        semantics docstring): the launch body is shared with the boot-time
        intent resume (DEC-PULL-INTENT-RESUME-001), so the operator's POST and
        a resumed pull can never drift onto two different launch paths.

        Offloaded via ``run_in_threadpool`` (the app.py offload doctrine — the
        round-1 review fold on DEC-ACCOUNT-LEASE-CROSSHOST-001): the launch
        acquires the cross-process pull guards synchronously
        (``BackgroundPullJob.start`` → ``_acquire_singleflight`` — two flock
        opens, the lease read and an fsync'd lease write, ALL on the lake
        volume), so running it on the event loop would freeze the WHOLE
        dashboard at launch time on a wedged/stalled external volume — the
        exact event-loop-freeze class DEC-PULL-RATE-BRIDGE-NONBLOCK-CACHE-001
        (#1522) removed for ``/pull/rate``. ``HTTPException``s raised inside
        the offloaded body propagate unchanged (the 400/503 contracts are
        byte-identical).
        """
        return JSONResponse(
            await _run_lake(
                lambda: _launch_pipeline(
                    symbols=symbols,
                    symbol=symbol,
                    from_date=from_date,
                    to_date=to_date,
                    dry_run=dry_run,
                    scope=scope,
                    auto_arm=auto_arm,
                )
            )
        )

    if auto_pull_scheduler is not None:
        _scheduler: AutoPullScheduler = auto_pull_scheduler

        def _auto_spot_launcher() -> AutoLaunchResult:
            """The scheduler's fire seam — the SAME spot-only launcher the button uses.

            Every rail is inherited by construction (`_launch_pipeline` IS
            the button's body): market-hours guard, single-flight + flock,
            durable intent + launch mutex, note surgery, crossverify tail.
            Never raises for a refusal — a route-shaped rejection
            (``HTTPException``: a credential 503, a validation 400) and a
            refused start both map to ``started=False`` with the honest
            reason, so the scheduler retries on later ticks (a healed
            credential outage / a finished sweep is picked up the same day).
            """
            try:
                payload = _launch_pipeline(
                    symbols=None,
                    symbol=None,
                    from_date=None,
                    to_date=None,
                    dry_run=False,
                    scope=SCOPE_SPOT_ONLY,
                )
            except HTTPException as exc:
                return AutoLaunchResult(
                    started=False,
                    detail=f"launch rejected ({exc.status_code}): {exc.detail}",
                )
            if payload.get("started"):
                return AutoLaunchResult(started=True, detail="spot-only pipeline job started")
            return AutoLaunchResult(
                started=False,
                detail=(
                    "launch refused — the pull single-flight/job slot is held "
                    "(a pull or a multi-day dashboard sweep is running)"
                ),
            )

        _scheduler.bind_launcher(_auto_spot_launcher)

        @app.post("/api/v1/universe/pull-all-spot-auto")
        async def api_universe_pull_all_spot_auto(armed: bool = Query()) -> JSONResponse:
            """POST /api/v1/universe/pull-all-spot-auto — arm/disarm the daily auto-pull.

            The "auto: ON — disarm" chip's toggle (DEC-PULL-SCOPED-ALL-
            ONECLICK-001 round-5 fold): ``armed=1`` arms (and ensures the
            ticker) WITHOUT launching anything — the ▶ button's ``auto_arm``
            is the launch-and-arm path; ``armed=0`` disarms (the durable
            state persists across restarts either way). Returns the fresh
            ``auto_pull`` status block. Offloaded (state-file write on the
            lake volume — the app.py offload doctrine). Registered only when
            a scheduler is wired; a scheduler-less app has no route (404 —
            the chip renders "unavailable" from the absent payload key).
            """

            def _toggle() -> dict[str, Any]:
                """Arm/disarm on the worker thread; return the fresh status block."""
                if armed:
                    _scheduler.arm()
                else:
                    _scheduler.disarm()
                return {"auto_pull": _scheduler.status()}

            return JSONResponse(await _run_lake(_toggle))

    # ─────────────────────────────────────────────────────────────────────────
    # DHAN SPOT feed pull (DEC-DHAN-PULL-SURFACE-001 / DEC-DHAN-SPOT-FEED-001).
    # The standalone read-only Dhan SPOT track: pulls NIFTY/BANKNIFTY/SENSEX
    # 1-minute (+ INDIA VIX reference-only) into its OWN ``feeds/dhan/`` lake,
    # via the lean Dhan spot spine + a Dhan-scoped prebuild (bound to dhan_root).
    # It reuses NOTHING of the Groww ``BulkSpotReport``-coupled ``run_full_pipeline``
    # (DEC-FEED-SPINE-GENERALIZE-001, option b — the deferred full-spine unification);
    # movers-warm is deferred with it (a feed-aware movers reader is the named
    # follow-up DEC-DHAN-MOVERS-FEED-AWARE-001).
    # ─────────────────────────────────────────────────────────────────────────
    def _default_dhan_spot_pull_production(
        request: DhanPullRequest,
    ) -> DhanPullReport:  # pragma: no cover — production wiring: real SSM + dhanhq + LMDB
        """The production Dhan pull seam: the real SSM/dhanhq pull paced by the Dhan controller."""
        return default_dhan_spot_pull(request, pace=_dhan_session_rate.get().pace)

    _dhan_spot_pull_fn: DhanSpotPullFn = (
        dhan_spot_pull if dhan_spot_pull is not None else _default_dhan_spot_pull_production
    )

    def _launch_dhan_pipeline(
        *, from_date: str | None, to_date: str | None, dry_run: bool
    ) -> dict[str, Any]:
        """The ``/pull-pipeline-dhan`` launch body: validate → resolve dhan_root → start.

        Credential gate FIRST (DEC-PULL-LOUD-CREDENTIAL-REFUSAL-001): a real Dhan pull
        with missing/unreachable read-only SSM credentials is a LOUD **HTTP 503**
        carrying the actionable fix text, never a silent synthetic downgrade; an
        explicit ``?dry_run=true`` is exempt (no creds resolved). The operator date
        range is validated EAGERLY via
        :func:`~brutex.dashboard.pull_controller.resolve_candle_range` (a reversed /
        non-ISO pick → clean 400); blank ⇒ the canonical ``[2020-01-01, prev day]``
        default, whose start the ROLLING FLOOR then CLAMPS
        (DEC-DHAN-ROLLING-FLOOR-001): ``today_ist`` is resolved ONCE here and
        threaded on the request; a range ending on/before
        ``dhan_history_floor(today_ist)`` is a LOUD **HTTP 400** naming the floor
        (Dhan cannot serve any of it — zero vendor calls, zero job start), and a
        partially-pre-floor start is clamped at the plan chokepoint
        (``max(start, floor)`` — one ``dhan.window_floor_clamped`` INFO + the
        clamp note on the report; NEVER doomed pre-floor requests recorded as
        clean-empty SUCCESS windows). The pull runs on the SEPARATE single-flight
        ``_dhan_pull_job`` (own flock
        on ``feeds/dhan/`` — no collision with Groww's ``_pull_job``), and is
        ``market_hours_exempt`` because Dhan uses its OWN account (the 08:45-15:35 NSE
        guard exists only to free the SHARED Groww account for TickVault → the Dhan
        pull passes ``market_closed=None``).

        A ``dry_run`` start is fully OFFLINE (Golden Rule #1): the production seam
        (:func:`~brutex.dashboard.dhan_pull.default_dhan_spot_pull`) short-circuits
        after resolving the securityId map — it builds NO adapter, loads NO AWS
        credentials, issues NO vendor call, and writes nothing — so the start skips the
        cross-process guards (``cross_process_lock=False``, the ``_greeks_backfill_job``
        dry-run precedent), meaning a dry-run never resolves the account-lock key from
        AWS. The WET run keeps the cross-process lock.
        """
        refusal = gate_pull_credentials(
            dry_run_requested=dry_run, credentials_error=dhan_credentials_error
        )
        if refusal is not None:
            _LOG.error(
                "universe.pull_pipeline_dhan.refused_no_credentials", reason=refusal.auth_cause
            )
            raise HTTPException(status_code=503, detail=verdict_message(refusal))
        from_date = (from_date or "").strip() or None
        to_date = (to_date or "").strip() or None
        try:
            start_dt, end_dt = resolve_candle_range(
                from_date=from_date, to_date=to_date, year=None, now=datetime.now(UTC)
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # DEC-DHAN-ROLLING-FLOOR-001: the IST "today" is resolved EXACTLY ONCE per
        # launch and threaded (the pull never re-reads the clock mid-plan). A range
        # ending on/before the floor is structurally unservable — refuse LOUDLY
        # (zero vendor calls, zero job start) instead of recording doomed
        # clean-empty windows; end_dt.date() is the EXCLUSIVE day.
        today_ist = now_ist().date()
        floor = dhan_history_floor(today_ist)
        if end_dt.date() <= floor:
            raise HTTPException(
                status_code=400,
                detail=(
                    "requested range ends before the Dhan rolling 5-year floor "
                    f"({floor.isoformat()} = today−5y+{DHAN_FLOOR_MARGIN_DAYS}d margin) — "
                    "Dhan cannot serve any of it; nothing was pulled"
                ),
            )
        lake_root = _resolve_pull_lake_root(instrument_store, dry_run=dry_run)
        dhan_root = feed_lake_root(lake_root, feed="dhan")
        request = DhanPullRequest(
            lake_root=lake_root,
            start=start_dt.date(),
            end=end_dt.date(),
            dry_run=dry_run,
            today=today_ist,
        )
        _LOG.info(
            "universe.pull_pipeline_dhan.requested",
            start=request.start.isoformat(),
            end=request.end.isoformat(),
            dry_run=dry_run,
            dhan_root=str(dhan_root),
        )

        def _work(on_progress: ProgressHook) -> object:  # pragma: no cover — job thread
            """Background body: run the Dhan spot pull (+ Dhan-scoped prebuild)."""
            _dhan_pull_job.set_phase("spot")
            report = _dhan_spot_pull_fn(request)
            return report.to_dict()

        detail = "dhan spot pipeline"
        if dry_run:
            detail = label_synthetic(detail)
        started = _dhan_pull_job.start(
            _work,
            total=len(DHAN_SPOT_INDEX_SYMBOLS),
            detail=detail,
            # A DRY-RUN is fully OFFLINE: the real seam short-circuits after resolving
            # the securityId map (no adapter build, no vendor call, no write), so it
            # needs NO cross-process guards — skipping them means the dry-run never
            # resolves the account-lock key from AWS (the `_greeks_backfill_job`
            # dry-run precedent). The WET run keeps the cross-process lock.
            cross_process_lock=not dry_run,
            market_hours_exempt=True,
        )
        payload: dict[str, Any] = {
            "started": started,
            "feed": "dhan",
            "total": len(DHAN_SPOT_INDEX_SYMBOLS),
            "dry_run": dry_run,
        }
        # EMIT-ON-CLAMP additive keys (DEC-DHAN-ROLLING-FLOOR-001): a start before
        # the floor names the clamp on the start payload too (the report carries
        # the same two keys computed from the SAME threaded today).
        if request.start < floor:
            payload["floor"] = floor.isoformat()
            payload["effective_start"] = max(request.start, floor).isoformat()
        return payload

    @app.post("/api/v1/universe/pull-pipeline-dhan")
    async def api_universe_pull_pipeline_dhan(
        from_date: str | None = Query(default=None),
        to_date: str | None = Query(default=None),
        dry_run: bool = Query(default=False),
    ) -> JSONResponse:
        """POST /api/v1/universe/pull-pipeline-dhan — the standalone Dhan SPOT pull.

        Thin delegator over :func:`_launch_dhan_pipeline`, offloaded via ``_run_lake``
        (the app.py offload doctrine — the launch acquires the cross-process
        single-flight guards synchronously). ``HTTPException``s (400 validation / 503
        credential refusal) propagate unchanged.
        """
        return JSONResponse(
            await _run_lake(
                lambda: _launch_dhan_pipeline(from_date=from_date, to_date=to_date, dry_run=dry_run)
            )
        )

    # ─────────────────────────────────────────────────────────────────────────
    # DHAN ROLLING-ATM OPTION feed pull (DEC-DHAN-ROLLING-OPTIONS-001).
    # The standalone read-only Dhan rolling-ATM option track (ATM±N per bar, NOT
    # per-contract — Dhan serves no option chain): pulls the PURE cartesian of 1008
    # rolling keys (3 indices × {WEEK,MONTH} × EC{0..3} × ATM±10 × {CALL,PUT}) at
    # 1-minute into ``feeds/dhan/rolling_options/``, on a SEPARATE single-flight
    # ``_dhan_option_pull_job`` (own flock on ``feeds/dhan/`` — no Groww collision).
    # ``market_closed=None`` (Dhan uses its OWN account). Servability is UNVERIFIED —
    # an unservable key degrades to an honest EMPTY / a DH-905 re-pullable FAILED
    # window; never a fabricated bar (§7 gate, Golden Rule #1).
    # ─────────────────────────────────────────────────────────────────────────
    def _default_dhan_rolling_option_pull_production(
        request: DhanPullRequest,
    ) -> DhanRollingOptionPullReport:  # pragma: no cover — production wiring: real SSM + dhanhq
        """The production Dhan rolling-ATM pull seam: the real SSM/dhanhq pull, Dhan-paced."""
        return default_dhan_rolling_option_pull(request, pace=_dhan_session_rate.get().pace)

    _dhan_rolling_option_pull_fn: DhanRollingOptionPullFn = (
        dhan_rolling_option_pull
        if dhan_rolling_option_pull is not None
        else _default_dhan_rolling_option_pull_production
    )

    def _launch_dhan_rolling_pipeline(
        *, from_date: str | None, to_date: str | None, dry_run: bool
    ) -> dict[str, Any]:
        """The ``/pull-rolling-options-dhan`` launch body: validate → resolve → start.

        The rolling-ATM options sibling of :func:`_launch_dhan_pipeline`. Credential
        gate FIRST (DEC-PULL-LOUD-CREDENTIAL-REFUSAL-001 — reuses the SAME read-only
        Dhan SSM gate as spot); the operator date range is validated EAGERLY (a
        reversed / non-ISO pick → clean 400); blank ⇒ the canonical default range (the
        rolling honest-degrade records clean-empty windows for a not-yet-servable span
        until ``DHAN_ROLLING_HISTORY_FLOOR`` is probe-verified, Golden Rule #1). The
        pull runs on the SEPARATE single-flight ``_dhan_option_pull_job`` (own flock on
        ``feeds/dhan/``), and is ``market_hours_exempt`` because Dhan uses its OWN
        account (the 08:45-15:35 NSE guard exists only to free the SHARED Groww account
        → the Dhan pull passes ``market_closed=None``).

        A ``dry_run`` start is fully OFFLINE (Golden Rule #1): the production seam
        (:func:`~brutex.dashboard.dhan_rolling_pull.default_dhan_rolling_option_pull`)
        short-circuits after planning the pure cartesian — it builds NO adapter, loads
        NO AWS credentials, issues NO vendor call, and writes nothing — so the start
        skips the cross-process guards (``cross_process_lock=False``, the
        ``_greeks_backfill_job`` dry-run precedent), meaning a dry-run never resolves
        the account-lock key from AWS. The WET run keeps the cross-process lock.
        """
        refusal = gate_pull_credentials(
            dry_run_requested=dry_run, credentials_error=dhan_credentials_error
        )
        if refusal is not None:
            _LOG.error(
                "universe.pull_rolling_options_dhan.refused_no_credentials",
                reason=refusal.auth_cause,
            )
            raise HTTPException(status_code=503, detail=verdict_message(refusal))
        from_date = (from_date or "").strip() or None
        to_date = (to_date or "").strip() or None
        try:
            start_dt, end_dt = resolve_candle_range(
                from_date=from_date, to_date=to_date, year=None, now=datetime.now(UTC)
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        lake_root = _resolve_pull_lake_root(instrument_store, dry_run=dry_run)
        dhan_root = feed_lake_root(lake_root, feed="dhan")
        request = DhanPullRequest(
            lake_root=lake_root, start=start_dt.date(), end=end_dt.date(), dry_run=dry_run
        )
        _LOG.info(
            "universe.pull_rolling_options_dhan.requested",
            start=request.start.isoformat(),
            end=request.end.isoformat(),
            dry_run=dry_run,
            dhan_root=str(dhan_root),
            keys=DHAN_ROLLING_PLAN_KEY_COUNT,
        )

        def _work(on_progress: ProgressHook) -> object:  # pragma: no cover — job thread
            """Background body: run the Dhan rolling-ATM option pull."""
            _dhan_option_pull_job.set_phase("rolling-options")
            report = _dhan_rolling_option_pull_fn(request)
            return report.to_dict()

        detail = "dhan rolling-ATM options pipeline"
        if dry_run:
            detail = label_synthetic(detail)
        started = _dhan_option_pull_job.start(
            _work,
            total=DHAN_ROLLING_PLAN_KEY_COUNT,
            detail=detail,
            # A DRY-RUN is fully OFFLINE: the real seam short-circuits after planning
            # the pure cartesian (no adapter build, no vendor call, no write), so it
            # needs NO cross-process guards — skipping them means the dry-run never
            # resolves the account-lock key from AWS (the `_greeks_backfill_job`
            # dry-run precedent). The WET run keeps the cross-process lock.
            cross_process_lock=not dry_run,
            market_hours_exempt=True,
        )
        return {
            "started": started,
            "feed": "dhan",
            "dataset": "rolling-ATM",
            "total": DHAN_ROLLING_PLAN_KEY_COUNT,
            "dry_run": dry_run,
        }

    @app.post("/api/v1/universe/pull-rolling-options-dhan")
    async def api_universe_pull_rolling_options_dhan(
        from_date: str | None = Query(default=None),
        to_date: str | None = Query(default=None),
        dry_run: bool = Query(default=False),
    ) -> JSONResponse:
        """POST /api/v1/universe/pull-rolling-options-dhan — the Dhan rolling-ATM pull.

        Thin delegator over :func:`_launch_dhan_rolling_pipeline`, offloaded via
        ``_run_lake`` (the launch acquires the cross-process single-flight guards
        synchronously). ``HTTPException``s (400 validation / 503 credential refusal)
        propagate unchanged. Rolling-ATM (ATM±N per bar) — NOT per-contract.
        """
        return JSONResponse(
            await _run_lake(
                lambda: _launch_dhan_rolling_pipeline(
                    from_date=from_date, to_date=to_date, dry_run=dry_run
                )
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
                    # A persisted spot-only intent resumes SPOT-ONLY — never a
                    # silently-widened full-scope F&O pull
                    # (DEC-PULL-SCOPED-ALL-ONECLICK-001; None = full).
                    scope=intent.scope,
                )
            except HTTPException as exc:
                if exc.status_code < _SERVER_ERROR_FLOOR:
                    raise ValueError(str(exc.detail)) from exc
                raise RuntimeError(str(exc.detail)) from exc

        _probe_job_singleflight = _resume_singleflight_probe(
            _job_lake_root_resolver(instrument_store)
        )

        def _resume_pipeline_from_intent() -> None:
            """Boot-time seam: run the intent auto-resume decision + dispatch.

            Exposed as ``app.state.brutex_resume_pipeline`` for the
            ``cli.cmd_dashboard`` startup launcher (the SAME daemon-thread seam
            family as the instrument autoload). Every load-bearing decision —
            single-flight probe, restart-cap refusal + CRITICAL alert + red
            banner, bump-before-launch, stale-intent clear — lives in the
            100 %-tested :func:`~brutex.dashboard.pull_intent.run_auto_resume`;
            this binding supplies the real launcher + the JOB-root-keyed
            single-flight probe (never the env default — the two must probe
            one lock). Never raises.
            """
            run_auto_resume(
                _intent_store,
                launch=_resume_launch,
                probe_singleflight=_probe_job_singleflight,
            )

        app.state.brutex_resume_pipeline = _resume_pipeline_from_intent

        def _resume_legacy_pull() -> None:
            """Boot-time seam: the LEGACY ledger-triggered resume, in-process.

            Exposed as ``app.state.brutex_resume_legacy`` for the
            ``cli.cmd_dashboard`` startup launcher (DEC-AUTO-RESUME-INPROCESS-001
            — replaces the deleted DOA ``brutex fetch --until-clean`` subprocess
            child, which died in ~0.3 s every opted-in boot on DuckDB's
            exclusive cross-process locks: ``catalog.duckdb`` first — held by
            the boot-window L3 lake-heal sweeper, the incident's first-raising
            site — and the dashboard's lifetime-held ``instruments.duckdb``
            one step later). Every load-bearing
            decision lives in the 100 %-tested
            :func:`~brutex.dashboard.pull_intent.run_legacy_resume` /
            :func:`~brutex.dashboard.pull_intent.run_auto_resume`; this binding
            supplies the SAME real launcher + JOB-root-keyed single-flight probe
            the intent seam above binds, so the two resumes contend on ONE lock.
            Raises only on the loud intent-write refusal (an unwritable lake) —
            the caller's ``maybe_resume_on_boot`` guard turns that into the
            honest ``error`` verdict.
            """
            run_legacy_resume(
                _intent_store,
                launch=_resume_launch,
                probe_singleflight=_probe_job_singleflight,
            )

        app.state.brutex_resume_legacy = _resume_legacy_pull

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
        # empty/unknown selection → 400, DEC-PULL-SELECTED-PIPELINE-001). Resolved on
        # a threadpool worker (DEC-DASHBOARD-EVENTLOOP-OFFLOAD-003 — the per-token
        # `store.get`s + the two whole-store `_select_bulk_targets` reads are
        # synchronous DuckDB reads that sat ON the event loop); the ValueError → 400
        # mapping stays OUTSIDE the offload (the #1622 api_universe_instruments idiom).
        def _resolve_targets() -> tuple[list[Instrument], str]:
            """Resolve the retry selection on the threadpool worker (DuckDB reads)."""
            return resolve_pull_targets(
                symbols=symbols,
                symbol=symbol,
                lookup=_require_store().get,
                whole_universe=lambda: (
                    _select_bulk_targets("index") + _select_bulk_targets("stock")
                ),
            )

        try:
            instruments, target_source = await _run_lake(_resolve_targets)
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
                # DEC-GROWW-MARKET-HOURS-GUARD-001: mid-run halt if the pull crosses
                # into the NSE market-hours window (the shared-account belt). The env
                # kill switch is parsed ONCE here (fail-loud); the bound predicate then
                # reads the live clock only (no per-window env re-parse → a junk env
                # set mid-pull can never crash a long pull).
                market_closed=resolve_market_closed(),
            )
            _stamp_pull_verdict(report.verdict.to_dict())
            return report

        unit_total = len(instruments) * len(fetch_intervals)
        started = _pull_job.start(
            _work,
            total=unit_total,
            detail=detail,
            # DEC-GROWW-MARKET-HOURS-GUARD-001: --dry-run is vendor-free → EXEMPT.
            market_hours_exempt=dry_run,
        )
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

    @app.post("/api/v1/universe/greeks-backfill")
    async def api_universe_greeks_backfill(
        dry_run: bool = Query(default=False),
        force: bool = Query(default=False),
        expired_only: bool = Query(default=False),
        batch: int | None = Query(default=None),
        limit: int | None = Query(default=None),
    ) -> JSONResponse:
        """POST /api/v1/universe/greeks-backfill — start the bulk Greeks v1→v2 re-enrich.

        DEC-GREEKS-BULK-REENRICH-001 (the MIGRATION.md #1188 named follow-up):
        scan the F&O options lake for contracts whose ``greeks_provenance_id``
        stamps are UNSTAMPED / v1 / inputs-drifted, then re-enrich them in
        bounded batches through the EXISTING
        :func:`~brutex.data.greeks_enrich.enrich_greeks_for_targets` driver on
        the background job (returns at once; poll ``/greeks-backfill-progress``).
        The scan runs ON the job thread — it is HONESTLY O(files), never O(1) —
        so the total is unknown at POST time; the first per-batch sync sets it.

        NO credential gate, deliberately (unlike the pull routes): the backfill
        makes ZERO vendor calls, and a credential-less CLI-lake box MUST be able
        to run it — that IS gap (b) of #1188. ``dry_run`` is an explicit query
        param (default false) and stops at the PLAN boundary: zero writes, zero
        checkpoint, zero Telegram — and it takes NO cross-process lake lock
        (``cross_process_lock=False``): a zero-write scan must never exclude a
        real pull for its whole O(files) duration (the CLI ``--dry-run``
        lock-free posture; DEC-GREEKS-BULK-REENRICH-001 fix round 2). WET
        single-flight: a second click — or a pull on the same lake — refuses
        with ``started: false`` (the flock is keyed on the store-derived WRITE
        root, so it contends with a CLI pull/backfill regardless of
        ``$BRUTEX_LAKE_ROOT``). A disk-full / broken pool halts through the
        job's existing ``is_pull_halt`` handling (one CRITICAL Telegram alert;
        the durable checkpoint survives for resume). ``force`` + ``limit``
        together is a 400: force-mode selection is stamp-independent, so a
        limited force run re-selects the SAME first N contracts every run —
        chipping works only in the default mode. The batch/pause ENV twins
        (``$BRUTEX_GREEKS_BACKFILL_BATCH`` / ``$BRUTEX_GREEKS_BACKFILL_PAUSE_S``)
        are resolved EAGERLY here (fix round 5): a misconfigured env is a 400
        at POST time, never a job-thread error AFTER the paid O(files) scan;
        the echoed ``batch`` is the resolved EFFECTIVE value.
        """
        if batch is not None and batch < 1:
            raise HTTPException(status_code=400, detail=f"batch must be >= 1, got {batch}")
        if limit is not None and limit < 1:
            raise HTTPException(status_code=400, detail=f"limit must be >= 1, got {limit}")
        if force and limit is not None:
            # force+limit never progresses: force-mode selection is stamp-
            # independent, so every run re-selects the SAME first N sorted
            # contracts — the incremental-chipping promise holds only for the
            # default stamp-driven mode (Golden Rule #1, no illusions).
            raise HTTPException(
                status_code=400,
                detail=(
                    "force with limit never progresses (force-mode selection is "
                    "stamp-independent, so every run re-selects the same first "
                    "N contracts) — drop force or drop limit"
                ),
            )
        if batch is None:
            # ENV twin resolved EAGERLY at POST time (fix round 5, refuter-
            # confirmed): a misconfigured $BRUTEX_GREEKS_BACKFILL_BATCH used to
            # detonate on the job thread only AFTER the whole O(files) scan —
            # a wasted multi-hour scan ending in an error state, the exact
            # lateness class the sibling `batch < 1` query check refuses
            # eagerly. 400 now, before the job even starts.
            try:
                batch = resolve_backfill_batch_size()
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            # The pause env twin ($BRUTEX_GREEKS_BACKFILL_PAUSE_S) — same eager
            # posture ("inf" would otherwise crash the wet job mid-batch).
            pause_s = resolve_backfill_pause_s()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        lake_root = _resolve_pull_lake_root(instrument_store, dry_run=dry_run)
        _LOG.info(
            "universe.greeks_backfill.requested",
            dry_run=dry_run,
            force=force,
            expired_only=expired_only,
            batch=batch,
            limit=limit,
            lake_root=str(lake_root),
        )

        def _work(on_progress: ProgressHook) -> object:  # pragma: no cover — runs on the job thread
            """Background body: scan → plan → (dry-run report | run + ONE Telegram summary)."""
            rate_source = resolve_risk_free_source()
            # Honest live label for the O(files) scan phase (the poller would
            # otherwise show a blank ""). Liveness itself is owned by the
            # scan's own per-contract/per-file emit_progress_beat seams
            # (DEC-PIPELINE-STALL-HEARTBEAT-001 — the 600 s watchdog stays fed).
            _greeks_backfill_job.sync_from_ledger(
                completed=0,
                total=0,
                bars=None,
                failed=0,
                current="scanning lake for stale Greeks stamps (O(files)) …",
            )
            scan = scan_stale_greeks_targets(
                lake_root,
                rate_source=rate_source,
                expired_only=expired_only,
                force=force,
                limit=limit,
            )
            plan = plan_greeks_backfill(
                scan, rate_source=rate_source, force=force, expired_only=expired_only
            )
            if dry_run:
                # INERT by construction: no runner, no checkpoint, no alert.
                return dry_run_backfill_report(plan).to_dict()

            def _sync(completed: int, total: int, bars: int, errored: int, label: str) -> None:
                # Absolute per-batch counts → the job's ledger-sync channel
                # (sets the total the POST could not know; marks the summary
                # cache dirty so overlay readers recompute — Golden Rule #1).
                _greeks_backfill_job.sync_from_ledger(
                    completed=completed, total=total, bars=bars, failed=errored, current=label
                )

            report = run_greeks_backfill(
                lake_root,
                plan=plan,
                rate_source=rate_source,
                batch_size=batch,
                pause_s=pause_s,
                on_progress=_sync,
            )
            # EXACTLY-ONCE completion summary — the one dashboard chokepoint
            # (the halt path is covered by the job's is_pull_halt handling).
            emit_greeks_backfill_complete_alert(report)
            return report.to_dict()

        # total=0 + ledger_synced=True: the scan (O(files), on the job thread)
        # discovers the real total; the first _sync sets it. The "note" of the
        # returned report dict becomes the completion headline (_finish_detail).
        # A DRY-RUN skips the cross-process lake lock (zero writes to protect —
        # a read-only scan must never refuse a real pull; the wet run keeps it).
        started = _greeks_backfill_job.start(
            _work,
            total=0,
            detail=("greeks backfill (DRY RUN)" if dry_run else "greeks backfill"),
            ledger_synced=True,
            cross_process_lock=not dry_run,
            # DEC-GROWW-MARKET-HOURS-GUARD-001: the Greeks backfill re-enriches
            # ALREADY-STORED bars and makes ZERO Groww API calls, so it is EXEMPT
            # from the market-hours start gate (safe to run in the trading window).
            market_hours_exempt=True,
        )
        return JSONResponse(
            {
                "started": started,
                "dry_run": dry_run,
                "force": force,
                "expired_only": expired_only,
                "batch": batch,
                "limit": limit,
                "note": (
                    "scan is O(files) — the total appears once the scan completes; "
                    + (
                        "dry-run takes no lake lock (zero writes — a concurrent "
                        "pull on this lake is unaffected)"
                        if dry_run
                        else "a pull on the same lake is refused while the backfill "
                        "runs (one lake-writer job at a time)"
                    )
                ),
            }
        )

    @app.get("/api/v1/universe/greeks-backfill-progress")
    async def api_universe_greeks_backfill_progress() -> JSONResponse:
        """GET /api/v1/universe/greeks-backfill-progress — the O(1) backfill snapshot.

        The backfill job's :meth:`~brutex.dashboard.pull_jobs.BackgroundPullJob.snapshot`
        (state / total / completed / failed / bars / current / detail / percent …)
        — O(1) per poll, ZERO lake reads (DEC-GREEKS-BULK-REENRICH-001).
        """
        return JSONResponse(_greeks_backfill_job.snapshot().to_dict())

    @app.post("/api/v1/crossverify/run")
    async def api_crossverify_run(
        dry_run: bool = Query(default=False),
        skip_pull: bool = Query(default=False),
        no_publish: bool = Query(default=False),
        day: str | None = Query(default=None),
    ) -> JSONResponse:
        """POST /api/v1/crossverify/run — start the daily TickVault cross-verify producer.

        DEC-CROSSVERIFY-PRODUCER-001 (MIGRATION.md #1776): the SAME
        :func:`brutex.crossverify.run.run_crossverify` runner the CLI
        ``brutex crossverify run`` dispatches to, sharing THIS app's
        ``SessionRateController`` (one account rate budget — §2 Rule 6),
        launched on a dedicated background job (returns at once; poll
        ``/api/v1/crossverify/progress``). Config junk
        (``BRUTEX_CROSSVERIFY_*`` AND — for vendor-calling runs — the
        market-hours guard env the runner's seams resolve) is validated
        EAGERLY → 400, never a job-thread error (the greeks-backfill
        posture; the CLI's eager ``default_seams`` validation, mirrored).

        Lock semantics (the phase-A handoff law): the job starts with
        ``cross_process_lock=False`` because the runner's own
        ``default_seams`` lock seam acquires the account→lake→lease trio
        itself with a bounded wait-retry + honest refusal — a job-level
        second acquire of the same flock would self-conflict. Market-hours
        semantics: ``market_hours_exempt=True`` at the JOB gate because the
        RUNNER owns the market-hours law at its step E (the SAME
        ``guard_active_default``; a vendor-calling in-window run returns the
        honest ``deferred_market_hours`` verdict, exit 0, while ``dry_run`` /
        ``skip_pull`` runs make no vendor calls and are legitimately allowed
        in-window — the job-level all-or-nothing gate cannot express that
        split). GREEKS N/A for this trigger: the Greeks stage is
        options-only, and this pull is SPOT-ONLY — all indices + all
        stocks; futures AND options excluded
        (DEC-CROSSVERIFY-SPOT-ONLY-001) — so there is nothing to enrich:
        pull → prebuild → movers-revision run with no Greeks stage.
        """
        parsed_day: date | None = None
        if day is not None:
            try:
                parsed_day = date.fromisoformat(day)
            except ValueError as exc:
                raise HTTPException(
                    status_code=400, detail=f"day must be YYYY-MM-DD, got {day!r}"
                ) from exc
        from brutex.crossverify.config import resolve_crossverify_config

        lake_root = _resolve_pull_lake_root(instrument_store, dry_run=dry_run)
        try:
            config = resolve_crossverify_config(
                lake_root=lake_root,
                day=parsed_day,
                dry_run=dry_run,
                skip_pull=skip_pull,
                no_publish=no_publish,
            )
            if not dry_run:
                # Eager fail-loud VALIDATION only (junk market-hours env →
                # 400 here, the CLI parity) — the runner's default_seams
                # re-resolves the predicate at bind; a dry-run threads None
                # (no vendor calls to gate), mirroring the seam's own gate.
                from brutex.calendars.market_hours import resolve_market_closed

                resolve_market_closed()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _LOG.info(
            "crossverify.requested",
            day=config.day.isoformat(),
            dry_run=dry_run,
            skip_pull=skip_pull,
            no_publish=no_publish,
            lake_root=str(lake_root),
        )

        # The ONE start body (DEC-PULL-SCOPED-ALL-ONECLICK-001 §11 COMMON):
        # runner selection + job _work + the lock/market-hours start kwargs
        # are shared with the spot-only pipeline's crossverify tail.
        started = _start_crossverify_producer(config)
        return JSONResponse(
            {
                "started": started,
                "dry_run": dry_run,
                "skip_pull": skip_pull,
                "no_publish": no_publish,
                "day": config.day.isoformat(),
                "note": (
                    "runs the same runner as `brutex crossverify run`; the run "
                    "takes the pull locks itself (bounded wait — a concurrent "
                    "pull defers it honestly); poll /api/v1/crossverify/progress"
                ),
            }
        )

    def _crossverify_last_run() -> dict[str, object] | None:
        """The durable last-run record — the reload-proof failure surface.

        DEC-CROSSVERIFY-WITHHELD-REGATE-001: reads the runner-written
        ``<lake_root>/.crossverify/last_run.json`` view sidecar (best-effort;
        absent / corrupt / no resolvable lake root → honest ``None`` — the
        panel simply shows nothing, never a 500). Runs on the threadpool via
        the route so a wedged volume can never block the event loop.
        """
        from brutex.crossverify.journal import load_last_run

        lake_root = _rate_bridge_lake_root_or_none(instrument_store)
        if lake_root is None:
            return None
        return load_last_run(lake_root)

    @app.get("/api/v1/crossverify/progress")
    async def api_crossverify_progress() -> JSONResponse:
        """GET /api/v1/crossverify/progress — the producer job snapshot.

        The dedicated job's :meth:`~brutex.dashboard.pull_jobs.BackgroundPullJob.snapshot`
        (state / current / detail / percent …) — O(1) per poll
        (DEC-CROSSVERIFY-PRODUCER-001) — plus the ADDITIVE ``last_run`` field:
        the durable end-of-run record the runner persists at
        ``<lake_root>/.crossverify/last_run.json`` (verdict + counts + capped
        failed keys + detail), so a finished failure survives a page reload
        AND a dashboard restart (DEC-CROSSVERIFY-WITHHELD-REGATE-001). One
        O(1) small-sidecar stat+read per poll, off the event loop; ``null``
        when absent/corrupt/unresolvable — never a fabricated record.
        """
        snapshot = _crossverify_job.snapshot().to_dict()
        snapshot["last_run"] = await run_in_threadpool(_crossverify_last_run)
        return JSONResponse(snapshot)

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
        if auto_pull_scheduler is not None:
            # ADDITIVE (round-5 fold): the daily auto-pull armed state + last
            # outcome + honest next-eligible text for the /universe chip. O(1)
            # memory after the scheduler's first lazy state load (single-writer
            # in-memory mirror) — offloaded anyway for that one first read.
            progress_scheduler = auto_pull_scheduler
            snap["auto_pull"] = await _run_lake(progress_scheduler.status)
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

        snap["phases"] = await _run_lake(_phases)
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

    @app.get("/api/v1/health/hub")
    async def api_health_hub() -> JSONResponse:
        """GET /api/v1/health/hub — the unified operator health fold.

        DEC-OPERATOR-HEALTH-TRUTH-001 (MIGRATION.md #1747). ONE green/red fold
        over the automation lanes with durable LOCAL state — pull outcome ·
        nightly chain (`<lake>/sweeps/nightly/<id>/report.json`) · backup
        dead-man (the PURE :func:`brutex.backup.deadman.evaluate_deadman`
        verdict, read-only) — plus the honestly NOT-WIRED doctor-daily +
        mutation lanes (GitHub-Actions-only gates, no local artifact; the
        honesty law: unknown/unwired reads unknown/not-wired, NEVER a
        fabricated green). Payload: ``{"fold": {verdict, headline},
        "lanes": [{lane, verdict, headline, detail, checked_at, age_s}, …],
        "generated_at", "ttl_s"}``.

        Honest O-envelope: a poll within the hub's TTL serves the memoised
        file lanes (O(1)); a refresh is one bounded ``scandir`` + three tiny
        JSON reads — never a lake scan. Runs on a threadpool worker (the
        app.py offload doctrine) so the event loop never touches the
        filesystem; a corrupt state file is a NAMED degraded lane, never a 500.
        """
        return JSONResponse(await run_in_threadpool(_health_hub.payload))

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

    def _nav_latency_coords(name: str, store: Any, *extra: object) -> tuple[str, int]:
        """Build the ``(stable key, token generation)`` pair for the nav-latency caches.

        DEC-DASHBOARD-NAV-LATENCY-001, re-keyed by DEC-NAV-CACHES-FLOOR-REKEY-001
        (supersedes ``_nav_latency_key``, which folded the freshness token into
        the KEY at ``generation=0``). The KEY is the STABLE route identity
        (name + route-specific ``extra`` discriminants, e.g. the coverage
        ``years`` span); the GENERATION digests the COMPOSITE freshness token —
        the dashboard write-generation (``_summary_cache.generation``, bumped
        by every pull / materialize) AND the instrument-master content revision
        (:func:`master_revision`, moved by a keyless ``load-instruments`` that
        does NOT bump the generation) AND the coverage-ledger content revision
        (:func:`coverage_revision`, moved by every ``upsert_coverage`` — a
        metadata pull, in THIS process or another one, writes coverage cells
        without moving either of the first two signals;
        DEC-STALENESS-CACHE-TRUTH-001, MIGRATION.md #1660) — through the ONE
        :func:`~brutex.dashboard.movers_cache.revision_generation` digest
        bridge (any token change moves the generation; the ~2^-64 collision
        envelope is documented on the bridge). Invalidation is equivalent to
        the superseded key fold — a pull OR an instrument load recomputes,
        never stale (Golden Rule #1) — but a mid-pull ``mark_dirty`` storm now
        lands on the SAME key, so the trio's pull-time adaptive floor is
        actually REACHABLE (the old cold-key-per-poll shape starved it). The
        token stays cheap (two COUNT+MAX aggregates over the lean master +
        the PK-indexed coverage ledger plus an O(1) generation read), strictly
        lighter than the projections it guards.
        """
        gen = _summary_cache.generation
        count, latest = master_revision(store)
        c_count, c_latest = coverage_revision(store)
        return _nav_latency_stable_key(name, *extra), revision_generation(
            f"gen={gen}|m={count}:{latest}|c={c_count}:{c_latest}"
        )

    def _nav_latency_stable_key(name: str, *extra: object) -> str:
        """The trio's STABLE cache key (route identity only — no freshness token).

        Split out of :func:`_nav_latency_coords` (DEC-CACHE-SERVE-LAST-KNOWN-001)
        so the store-outage arm can address the SAME key withOUT the
        ``master_revision`` probe that just failed — the key doubles as
        :meth:`~brutex.dashboard.movers_cache.MoversSnapshotCache.serve_last_known`'s
        prefix (entries are stored at exactly this key, so the direct
        ``key == prefix`` fallback resolves the last-known entry).
        """
        extra_repr = "|".join(repr(item) for item in extra)
        return f"{name}|{extra_repr}"

    def _nav_cached_or_last_known(
        cache: MoversSnapshotCache,
        name: str,
        store: Any,
        *extra: object,
        compute: Callable[[], Any],
    ) -> tuple[Any, bool, bool]:
        """Resolve a nav-latency cached payload; a store-probe OUTAGE serves last-known.

        DEC-CACHE-SERVE-LAST-KNOWN-001 (the 8th keystone site's nav-trio arm —
        the scout-confirmed ``_nav_latency_coords`` → ``master_revision`` unguarded
        probe 500'd `/api/v1/coverage` + `/api/v1/universe/completeness` +
        `/api/v1/universe/mapping-integrity` under the SAME store outage): the
        composite freshness probe is guarded by :data:`STORE_FAULT_TYPES` — on a
        probe fault the route's STABLE key resolves the last-known entry via
        :meth:`~brutex.dashboard.movers_cache.MoversSnapshotCache.serve_last_known`
        (once-per-outage WARN latch + recovery INFO live there; a cold outage
        seeds ONE fresh compute at ``OUTAGE_GENERATION``). A compute that ITSELF
        hits a store fault during the cold-outage seed raises the typed
        :class:`UniverseStoreOutageError` the routes map to the honest 503
        degrade — never a raw 500.

        Returns ``(value, probe_outage, served_last_known)`` — the caller stamps
        the additive ``revision_probe`` marker when ``probe_outage`` is ``True``
        (a stale serve is never silent, Golden Rule #1).
        """
        try:
            key, token_gen = _nav_latency_coords(name, store, *extra)
        except STORE_FAULT_TYPES:
            key = _nav_latency_stable_key(name, *extra)
            try:
                value, served_last_known = cache.serve_last_known(key, compute=compute)
            except STORE_FAULT_TYPES as inner:
                raise UniverseStoreOutageError(
                    f"instrument store unreachable at the {name} probe with no last-known payload"
                ) from inner
            return value, True, served_last_known
        return cache.get_or_compute(key, generation=token_gen, compute=compute), False, False

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
        # (DEC-DASHBOARD-NAV-LATENCY-001, re-keyed by
        # DEC-NAV-CACHES-FLOOR-REKEY-001 — the token drives the GENERATION over
        # a stable key): a 4 s poll within one token is an O(1) handoff; a pull
        # (generation bump) OR an instrument load (master-revision move) moves
        # the token → recompute — UNLESS a pull is actively running, where the
        # per-window-tick bumps engage the bounded, logged adaptive floor
        # instead of a full O(datasets)+ledger rebuild per poll (the #920
        # storm class). The build runs ONLY on a miss, on the threadpool worker.
        def _cached_build() -> dict[str, Any]:
            """Return the coverage payload, memoised behind the composite freshness token.

            DEC-CACHE-SERVE-LAST-KNOWN-001: a store fault from the composite
            probe serves the last-known payload LABELLED (the additive
            ``revision_probe`` marker on a copy) instead of the pre-fix raw 500.
            """
            value, probe_outage, served = _nav_cached_or_last_known(
                _coverage_cache, "coverage", store, years, compute=_build
            )
            if probe_outage:
                payload: dict[str, Any] = _probe_outage_payload(value, served_last_known=served)
                return payload
            result: dict[str, Any] = value
            return result

        try:
            return JSONResponse(await _run_lake(_cached_build))
        except UniverseStoreOutageError:
            # COLD outage (nothing last-known, the seed compute hit the same
            # fault): the honest 503 markers, never a raw 500.
            return _store_outage_response()

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

        return JSONResponse(await _run_lake(_rows_and_rollup))

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
        board: dict[str, Any] = await _run_lake(_build_completeness_board, store, lake_root.parent)
        return board

    async def _completeness_csv() -> str:
        """Resolve the store + lake and build the downloadable CSV (empty + well-shaped if no lake)."""
        store = _require_store()
        lake_root = getattr(store, "path", None)
        if not isinstance(lake_root, Path):
            return scoreboard_to_csv(build_completeness_scoreboard([], {}, [], {}))
        csv_text: str = await _run_lake(_build_completeness_csv, store, lake_root.parent)
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
        # master-revision) token (DEC-DASHBOARD-NAV-LATENCY-001, re-keyed by
        # DEC-NAV-CACHES-FLOOR-REKEY-001 — the token drives the GENERATION over
        # a stable key). The inner SPOT lake
        # scan is ALREADY `_summary_cache`-cached (DEC-COMPLETENESS-O1-LATENCY-001), but
        # the residual `categorize_instruments` + `list_coverage` + `per_symbol_completeness`
        # projection re-ran on every 15 s poll; this caches the WHOLE map so a repeat poll
        # within one token is an O(1) handoff. A pull (generation) or an instrument load
        # (master revision) moves the token → recompute, never stale — with mid-pull
        # window-tick bumps riding the bounded, logged adaptive floor (#920).
        def _cached_per_symbol() -> dict[str, Any]:
            """Return the per-symbol completeness map, memoised behind the composite token.

            DEC-CACHE-SERVE-LAST-KNOWN-001: a store fault from the composite
            probe serves the last-known map LABELLED (the additive
            ``revision_probe`` marker on a copy — symbols are instrument names,
            so the marker key can never collide with a row) instead of the
            pre-fix raw 500.
            """
            value, probe_outage, served = _nav_cached_or_last_known(
                _universe_completeness_cache,
                "universe-completeness",
                store,
                compute=lambda: _build_per_symbol_completeness(store, lake),
            )
            if probe_outage:
                payload: dict[str, Any] = _probe_outage_payload(value, served_last_known=served)
                return payload
            result: dict[str, Any] = value
            return result

        try:
            per_symbol = await _run_lake(_cached_per_symbol)
        except UniverseStoreOutageError:
            return _store_outage_response()
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

    def _build_mapping_integrity_report_cached() -> tuple[MappingIntegrityReport, bool, bool]:
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
        dropped the instant either signal moves — with mid-pull window-tick bumps riding
        the bounded, logged adaptive floor per DEC-NAV-CACHES-FLOOR-REKEY-001, which also
        moved the token from the key into the generation via ``_nav_latency_coords``).

        DEC-CACHE-SERVE-LAST-KNOWN-001: returns ``(report, probe_outage,
        served_last_known)`` — a store fault from the composite probe serves the
        last-known report (the JSON route stamps the ``revision_probe`` marker on
        the serialized dict; the CSV download prepends the in-band
        ``# revision_probe: …`` marker row — the review fold — and the
        server-side WARN latch still fires); a COLD outage raises the typed
        :class:`UniverseStoreOutageError` both routes map to the honest 503.
        """
        report, probe_outage, served = _nav_cached_or_last_known(
            _mapping_integrity_cache,
            "mapping-integrity",
            _require_store(),
            compute=_build_mapping_integrity_report,
        )
        result: MappingIntegrityReport = report
        return result, probe_outage, served

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
        line+branch); READ-ONLY, no pull. No store wired → 503. A store OUTAGE
        (DEC-CACHE-SERVE-LAST-KNOWN-001) serves the last-known verdict labelled
        with the additive ``revision_probe`` marker; a COLD outage degrades to
        the honest 503 markers, never a raw 500.
        """
        _require_store()
        try:
            report, probe_outage, served = await _run_lake(_build_mapping_integrity_report_cached)
        except UniverseStoreOutageError:
            return _store_outage_response()
        payload = report_to_dict(report)
        if probe_outage:
            payload = _probe_outage_payload(payload, served_last_known=served)
        return JSONResponse(payload)

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
        wired → 503. A store OUTAGE (DEC-CACHE-SERVE-LAST-KNOWN-001 + the review
        fold) serves the last-known report LABELLED IN-BAND: one informational
        ``# revision_probe: …`` row is prepended (the CSV twin of the JSON
        routes' additive marker — same :func:`_probe_outage_marker` wording,
        rendered through the same ``FormulaSafeWriter``), so an Excel reader
        sees the staleness signal, not just the server-side WARN latch; a
        healthy serve is byte-identical (no marker row — regression-pinned).
        A COLD outage returns a plain-text 503, never a raw 500.
        """
        _require_store()
        try:
            report, probe_outage, served = await _run_lake(_build_mapping_integrity_report_cached)
        except UniverseStoreOutageError:
            return Response(
                content="instrument store unavailable — retry shortly",
                media_type="text/plain; charset=utf-8",
                status_code=503,
            )
        marker: str | None = None
        if probe_outage:
            marker = _probe_outage_marker(served_last_known=served) + " (store unreachable)"
        return Response(
            content=mapping_integrity_to_csv(report, revision_probe=marker),
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
        snapshot = await _run_lake(_read_master_integrity)
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
        snapshot = await _run_lake(_read_master_integrity)
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
        snapshot = await _run_lake(_read_economics_integrity)
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
        snapshot = await _run_lake(_read_economics_integrity)
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
        snapshot = await _run_lake(_read_parse_skips)
        if snapshot is None:
            return JSONResponse({"present": False})
        return JSONResponse({"present": True, **snapshot})

    def _build_lake_census_payload(
        per_file_pause: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """Scan the lake + fold the duplicate census JSON (DEC-LAKE-HEAL-001, thin I/O glue).

        The ONE builder behind the ``/api/v1/universe/lake-uniqueness`` route —
        since DEC-LAKE-CENSUS-NONBLOCKING-001 invoked ONLY by the
        ``LakeCensusHolder``'s dedicated daemon scan thread, never a request:
        runs the bounded invariant scanner (READ-ONLY / observe — the ROUTE never
        heals; healing belongs to the L2 pull seams + the L3 startup sweep), folds
        :func:`~brutex.data.lake_invariants.build_lake_census`, and stamps the heal
        mode + the ``.lake_heal`` stats/sentinel reads into the SAME dict shape
        ``brutex lake audit-unique --json`` prints. ``{"present": False}`` when no
        lake root can be resolved (mirrors the sibling panel routes' honest gate).
        *per_file_pause* is the holder's live progress tick, threaded through the
        scanner's EXISTING L3 throttle seam (fires per file incl. skips).
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
        census = build_lake_census(scan_lake_invariants(lake, per_file_pause=per_file_pause))
        return census_to_dict(
            census,
            mode=resolve_lake_heal_mode(),
            stats=load_heal_stats(lake),
            last_sweep=load_last_sweep(lake),
        )

    def _census_files_estimate() -> int | None:
        """The honest census progress denominator — the L3 sweep sentinel's count.

        Reads ``.lake_heal/last_sweep.json``'s ``files_scanned`` (the last L3
        startup sweep's own full-lake tally — the best available prior for how
        many files THIS scan will visit). ``None`` on a first-ever lake with no
        sentinel / a foreign-shaped sentinel — the UI then reads ``~?`` instead
        of a fabricated total (Golden Rule #1). Runs ON the census daemon
        thread (it is lake I/O), guarded by the holder's estimate fence.
        """
        from brutex.data.lake_heal import load_last_sweep

        lake_db = getattr(instrument_store, "path", None)
        if not isinstance(lake_db, Path):
            return None
        sweep = load_last_sweep(lake_db.parent)
        if sweep is None:
            return None
        files = sweep.get("files_scanned")
        if isinstance(files, bool) or not isinstance(files, int) or files < 0:
            return None
        return files

    def _census_first_scan_bound_hint() -> float | None:
        """Seed the FIRST-scan stall bound from the L3 sweep sentinel.

        Prefers the sentinel's own recorded duration (``duration_ms`` /
        ``duration_s`` when a future sweep stores one — none does today),
        else derives ``files_scanned × FIRST_SCAN_PER_FILE_BUDGET_S`` (the
        conservative 2 ms/file budget). ``None`` on a first-ever lake / a
        foreign-shaped sentinel — the holder then uses the bare 30-min
        floor. NON-FINITE sentinel values are junk, never a hint (round-1
        refuter: ``Infinity`` is valid JSON to Python's tolerant decoder,
        passes ``> 0``, and would have yielded an INFINITE stall bound —
        staleness detection silently disabled); a FINITE positive hint of
        ANY magnitude is ACCEPTED here and CLAMPED by the holder's
        ``_stale_bound_locked`` at ``FIRST_SCAN_BOUND_CEILING_S`` (24 h) —
        the ONE clamp site (rounds 2+3 refuters: the round-2 upper REJECT
        here dropped a legitimate >24 h hint — the conservative 2 ms/file
        budget crosses 24 h at ~43.2M files — to the 30-min floor,
        re-opening the large-lake first-scan false-stall the hint exists
        to prevent; the most-authoritative signal is never discarded, only
        bounded). Round-4 refuter: the ``duration_ms`` arm converts under
        a local ``OverflowError`` guard — a JSON int beyond float range
        (``10**309``) made ``math.isfinite`` RAISE, failing the whole hint
        callable (repeating WARN each check cycle) and collapsing the
        bound to the floor; a positive-huge int now SATURATES to a huge
        finite float and rides the same holder-clamp path as its ``1e308``
        float twin. The ``files_scanned`` arm is a non-negative int
        (structurally finite) whose product with the per-file budget is
        guarded against an absurd-magnitude ``OverflowError`` → ``None``;
        its result rides the same holder clamp. Runs ON the census daemon
        thread (lake I/O), guarded by the holder's hint fence.
        """
        from brutex.data.lake_heal import load_last_sweep

        lake_db = getattr(instrument_store, "path", None)
        if not isinstance(lake_db, Path):
            return None
        sweep = load_last_sweep(lake_db.parent)
        if sweep is None:
            return None
        duration_ms = sweep.get("duration_ms")
        if isinstance(duration_ms, (int, float)) and not isinstance(duration_ms, bool):
            d: float | None
            try:
                d = float(duration_ms)
            except OverflowError:
                # Round-4 refuter: a JSON *int* beyond float range (10**309)
                # made `math.isfinite(duration_ms)` raise pre-fix — the whole
                # hint callable failed (a repeating holder
                # `first_scan_bound_hint_failed` WARN each check cycle) and
                # the bound collapsed to the bare floor: the round-3
                # never-a-floor-collapse doctrine violated again, through the
                # INT shape. A positive-huge int is mathematically FINITE —
                # SATURATE it to a huge finite float so it rides the SAME
                # finite-positive → holder-clamp path as its 1e308 float twin
                # (→ the 24 h ceiling, never the floor); a negative-huge int
                # is unusable like any non-positive value (falls through to
                # the files arm).
                d = 1e308 if duration_ms > 0 else None
            if d is not None and math.isfinite(d) and d > 0:
                return d / 1000.0
        files = sweep.get("files_scanned")
        if isinstance(files, bool) or not isinstance(files, int) or files < 0:
            return None
        try:
            return files * FIRST_SCAN_PER_FILE_BUDGET_S
        except OverflowError:
            # An int too large for float conversion — junk sentinel.
            return None

    # DEC-LAKE-CENSUS-NONBLOCKING-001: the census holder — the scan runs on a
    # DEDICATED named daemon thread (zero AnyIO threadpool tokens), single-flight
    # (a concurrent poll neither spawns a second scan nor waits), and the route
    # below serves an O(1) snapshot. The generation probe (`_summary_cache
    # .generation` — one ledger stat + the O(dirs) sweeps digest, lake I/O per
    # DEC-DASHBOARD-EVENTLOOP-OFFLOAD-002 #1622) is read ON the daemon thread,
    # never the event loop. `census_spawn`/`census_clock` are the build_app test
    # seams (a synchronous spawn + fake clock make the holder deterministic).
    _lake_census_holder = LakeCensusHolder(
        scan=_build_lake_census_payload,
        generation=lambda: _summary_cache.generation,
        files_total_estimate=_census_files_estimate,
        spawn=census_spawn,
        clock=census_clock,
        first_scan_bound_hint=_census_first_scan_bound_hint,
    )

    @app.get("/api/v1/universe/lake-uniqueness")
    async def api_universe_lake_uniqueness() -> JSONResponse:
        """GET /api/v1/universe/lake-uniqueness — the lake duplicate-census verdict.

        DEC-LAKE-HEAL-001 payload; DEC-LAKE-CENSUS-NONBLOCKING-001 serving law:
        per-segment (INDEX/CASH/FNO/OPTIONS) clean vs dup/foreign/overlap/drift
        file counts + the ENGINE-index (NIFTY/BANKNIFTY/SENSEX) ``engine_clean``
        verdict + the heal mode and ``.lake_heal`` stats/sentinel — the
        ``/universe`` chip's data source. READ-ONLY (never heals). The route is
        **O(1) and never blocks**: it returns the holder's LAST-KNOWN verdict
        merged with the additive ``scan_in_progress`` / ``scan_progress`` /
        ``as_of_utc`` / ``scan_error`` / ``scan_stalled`` keys (the last =
        the LOUD staleness verdict — an in-flight scan past
        ``max(2 x last duration, 30 min)`` is flagged + WARNed once per
        episode; detection, never termination), after arming (at most) ONE
        single-flight background freshness check on the DEDICATED census daemon
        thread — no ``run_in_threadpool`` (a 26-minute production scan + the
        stacked 15 s pollers previously starved the shared AnyIO limiter and
        froze the whole data surface), no on-loop lake I/O (the generation
        probe runs on the daemon thread, the #1622 offload law). Rescans keep
        the pre-fix freshness law: generation moved AND past the adaptive
        floor (``max(2 s, scan duration)`` capped 120 s). HONEST: the scan
        itself stays O(files); the first-ever boot serves ``scan_in_progress``
        with live progress until one scan lands — never a fabricated verdict.
        No store wired → 503; no resolvable lake root → ``{"present": false}``.
        """
        store = _require_store()
        lake_db = getattr(store, "path", None)
        if not isinstance(lake_db, Path):
            return JSONResponse({"present": False})
        # DEC-LAKE-CENSUS-NONBLOCKING-001: the census deliberately does NOT ride
        # `_run_lake` (DEC-DASHBOARD-LAKE-IO-LIMITER-001) — its ~26-min scan
        # would pin a limiter token for its whole runtime; the holder keeps the
        # census OFF every request path (zero AnyIO/limiter tokens), the
        # ledgered L4 doctrine exception.
        _lake_census_holder.maybe_refresh()
        return JSONResponse(_lake_census_holder.snapshot())

    @app.get("/api/v1/coverage/windows")
    async def api_coverage_windows(
        market: str | None = Query(default=None),
        status: str | None = Query(default=None),
        # ge=0 (DEC-WINDOWS-COUNTS-ONLY-001): `?limit=0` = counts-only — counts +
        # meta with rows=[] (the cheap mode for the counts-only consumers). The
        # SERVER default stays 20_000 (fetch_windows.html genuinely renders rows).
        limit: int = Query(default=20_000, ge=0),
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

        ``?limit=0`` (DEC-WINDOWS-COUNTS-ONLY-001) = COUNTS-ONLY: the identical
        ``counts``/``count``/``matched`` meta with ``windows=[]`` — the row-page
        query is skipped server-side. The consumers that render only counts
        (coverage.html's windows KPI, universe.html's all-segments line) use it
        so their poll stops downloading the up-to-20k-row default payload
        (~294 B/row ⇒ ~5.9 MB per poll mid-pull) they never read.
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
                # The DEDICATED small page-row instance (DEC-WINDOWS-MIDPULL-FLOOR-001
                # round-2 MEDIUM fold — see the construction comment above).
                page_cache=_window_page_cache,
                generation=_summary_cache.generation,
                # Per-run honesty (DEC-TELEMETRY-SCOPE-BUDGET-RUNCOUNTS-001): the last
                # REAL pull's journal `started` instant — None (no journal / dry-run
                # only) OMITS the per-run figure entirely, never fabricates it.
                since=last_pull_start_utc(lake),
            )

        return JSONResponse(await _run_lake(_payload))

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

        rows = await _run_lake(_summary_cache.get_or_refresh, OPTIONS, recompute=_recompute)
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
        underlying_class: str | None = Query(default=None),
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
        underlying_class
            ``All`` / ``Indices`` / ``Stocks`` underlying-class filter
            (DEC-MOVERS-UNDERLYING-CLASS-001) applied at RANK time on all three
            tabs; blank / unknown ⇒ ``all`` (byte-identical default — no filtering).
            SPOT class comes from the lake segment; FUTURES/OPTIONS from the stored
            instrument-master ``is_index`` kind map. An unclassifiable underlying is
            in ``all`` but excluded from both ``Indices`` and ``Stocks`` (counted).

        **Honest envelope (Golden Rule #10):** the snapshot is only as good as the
        ingested lake — an empty lake (or a store with no lake root) returns an
        honest ``instrument_count: 0`` payload with a "pull spot first" note,
        never a 500 and never a fabricated mover. % change is vs the PREVIOUS
        day's close; volume/value are the cumulative FRESH session from 09:15.
        """
        store = _require_store()
        # ECHO HONESTY (DEC-MOVERS-DAILY-ASOF-ECHO-HONEST-001): a non-intraday
        # interval (``1day`` / any unknown label) has NO intraday as-of — the bar
        # IS the day, so the VALUE already ignores ``time``
        # (``compute_spot_candidates`` cuts whole-day via ``cutoff_min=None``,
        # DEC-MOVERS-ASOF-LATEST-DEFAULT-001). Null the echoed ``time`` here — the
        # ONE COMMON ``is_intraday_interval`` predicate (mirroring the ``latest_time``
        # gate below) — so every downstream echo surface (the "As-of …" note +
        # ``payload["time"]`` across spot/futures/options/empty/pending/gated) is
        # truthful instead of claiming an as-of the served whole-day close never
        # honoured. Byte-identical for value (daily already ignores it) and for a
        # genuine intraday interval (the predicate is True → ``time`` untouched).
        if not is_intraday_interval(interval):
            time = None
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
            gate = await _run_lake(_prebuild_gate.snapshot)
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
                            underlying_class=underlying_class,
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
                            underlying_class=underlying_class,
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
                        underlying_class=underlying_class,
                    )
                )
            lake_root = store_path.parent

            def _resolve() -> dict[str, Any]:
                """Cache-or-compute the candidate set, then rank to top_n + exchange.

                Runs on a threadpool worker (blocking lake reads + the cache's
                threading.Lock). The ``compute=`` closure performs the real scan
                ONLY on a miss / post-pull; a hit is an O(1) handoff + a cheap
                in-memory rank (DEC-MOVERS-O1-LATENCY-001).
                """
                # The current write-generation — a pull/materialize bumps it, so every
                # cached candidate set computed at an older generation is dirty (the
                # page never serves pre-pull data). Reused from the coverage cache.
                # DEC-DASHBOARD-EVENTLOOP-OFFLOAD-002: read HERE on the threadpool
                # worker — the probe stats the ledger + digests the sweeps tree (an
                # O(dirs) read), which must never run on the uvicorn event loop.
                generation = _summary_cache.generation
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
                # DEFAULT-poll latest-MINUTE display echo (DEC-MOVERS-ASOF-LATEST-DEFAULT-001):
                # ONLY when no explicit `time` was sent AND the interval is intraday, resolve the
                # latest available spot 1-minute of `date_str` for the ADDITIVE `latest_time`
                # display field — so the dropdown/label SHOW the real latest minute while the CUT
                # stays whole-day (`time` still threads `None` → identical latest close across
                # every TF). Memoised per (generation, date); `None` (empty/unreadable lake) → the
                # frontend shows a plain "latest" label and still cuts whole-day. Display-only —
                # the ranked numbers are byte-unchanged (the cut is not touched).
                latest_time: str | None = None
                if time is None and is_intraday_interval(interval):
                    latest_time = _latest_movers_minute_cache.get_or_compute(
                        latest_movers_minute_key(date_str),
                        generation=generation,
                        compute=lambda: latest_movers_minute(
                            lake_root, parse_snapshot_date(date_str)
                        ),
                    )
                # DEC-MOVERS-EXPLICIT-BUCKET-ASOF-001: display-only provenance — the
                # completed-bucket final "HH:MM" an explicit bucket-anchor `time` was
                # resolved forward to (`None` for daily / latest / TF-1minute / non-anchor,
                # keeping the payload byte-identical). O(1) pure; shared by all three tabs.
                resolved_through = asof_through_label(interval, time)
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
                    # The stored-master index-vs-stock kind map for the RANK-time
                    # `All | Indices | Stocks` filter (DEC-MOVERS-UNDERLYING-CLASS-001) —
                    # memoised on the master CONTENT token via the ONE
                    # `_underlying_kinds_cached` helper (review round 1: a keyless
                    # load-instruments moves the master with NO generation bump). The
                    # class itself is NOT a candidate_key coordinate (rank-time filter
                    # over the shared set), but the MASTER token IS folded into the key
                    # below — the candidates carry the map's verdict STAMPED per row,
                    # so stale stamps would defeat a fresh map.
                    underlying_kinds, kinds_token = _underlying_kinds_cached(store)
                    key = (
                        candidate_key(
                            asset="options",
                            exchange=exch,
                            interval=interval,
                            date=date_str,
                            time=time,
                            expiry_tier=tier,
                            expiry_track=trk,
                        )
                        + f"|m={kinds_token}"
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
                            underlying_kinds=underlying_kinds,
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
                        pcr=opt_cands.pcr,
                        latest_time=latest_time,
                        underlying_class=underlying_class,
                        resolved_through=resolved_through,
                    )
                if asset == "futures":
                    # Futures carries the Near/Next/Far serial-expiry tier (the
                    # ?expiry= selector; blank/unknown ⇒ near), normalised so the
                    # cache key and the computed set agree — so Near / Next / Far
                    # cache + rank independently (never a stale Near row served for
                    # a Far request) — DEC-MOVERS-FUTURES-TIER-001.
                    fut_tier = normalize_futures_tier(expiry)
                    # The stored-master kind map for the RANK-time class filter
                    # (DEC-MOVERS-UNDERLYING-CLASS-001) — memoised on the master
                    # CONTENT token via the ONE `_underlying_kinds_cached` helper
                    # (review round 1); the master token is ALSO folded into the
                    # candidate key below (the stamped-verdict staleness class —
                    # see the options arm above).
                    underlying_kinds, kinds_token = _underlying_kinds_cached(store)
                    key = (
                        candidate_key(
                            asset="futures",
                            exchange=exch,
                            interval=interval,
                            date=date_str,
                            time=time,
                            expiry_tier=fut_tier,
                        )
                        + f"|m={kinds_token}"
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
                            underlying_kinds=underlying_kinds,
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
                        latest_time=latest_time,
                        underlying_class=underlying_class,
                        resolved_through=resolved_through,
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
                    latest_time=latest_time,
                    underlying_class=underlying_class,
                    resolved_through=resolved_through,
                )

            payload = await _run_lake(_resolve)
            return JSONResponse(payload)
        except PcrCorruptionError as exc:
            # Lake VALUE corruption (an out-of-band negative OI/volume cell —
            # the ingest boundary clamps negatives and CandleBar rejects them,
            # so only a tampered/bit-rotted Parquet reaches here) is a
            # SERVER-side fault, never a client error: the PCR fold stays
            # fail-loud per the DEC-MOVERS-OPTIONS-PCR-001 design (edge 5), but
            # it must surface as an honest 500 with a corruption-specific
            # detail — NOT ride the malformed-date/time 400 mapping below
            # (round-1 hostile-review fold). Checked BEFORE ValueError (it is a
            # ValueError subclass, so ordering is load-bearing). ONE structlog
            # WARN so the corruption (named by the adapter-enriched
            # underlying/expiry group key, round-7 fold) reaches the log even
            # when nobody is watching the wire.
            _LOG.warning("movers.pcr_corruption", error=str(exc))
            raise HTTPException(status_code=500, detail=f"lake corruption: {exc}") from exc
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
        (``resolve_report_datasets`` → ``list_candle_datasets``) + the
        store reads run on a threadpool worker, NEVER the event loop.
        Forensically proven (2026-07-04 run — a PRE-DEC-O1-CLAIMS-HONESTY-001
        measurement of the then-UNFILTERED full ``bars/`` tree walk, ~66k
        dirs on a materialised options lake): this route ran that walk
        SYNCHRONOUSLY on the uvicorn loop during the prebuild write storm and
        froze EVERY dashboard request for 6 minutes (361,596 ms; 38 queued
        requests burst-flushed on release). The store connection is built +
        used inside the worker so its RLock stays on one thread
        (DEC-DASHBOARD-FASTREAD-001).

        DEC-O1-CLAIMS-HONESTY-001: the cold-miss discovery is now STRICTLY
        CHEAPER than that measured unfiltered walk — FNO-pruned at walk
        DESCENT (``exclude_segments={FNO}``: the F&O contract tree, the bulk
        of the ~66k dirs above, is never entered) + interval-bounded
        (``intervals`` = the one partition the basis reads), and a
        manifest-served lake pays an O(manifest-rows) read, no walk at all.
        The discovery result (the triples ONLY) is additionally memoised in
        the COMMON generation-keyed ``_db_datasets_cache`` (the #896 pattern,
        key ``preopen_datasets_key(basis)`` — one memo per basis, seeded by
        the post-pull ``warm_discovery_memos``), so a repeat open within one
        write generation is an O(1) handoff and the pruned cold-miss walk is
        paid at most once per generation per basis. The pullability tagging below
        is DELIBERATELY NOT cached: it reads the instrument master, which
        moves on a keyless ``load-instruments`` WITHOUT a generation bump
        (the ``master_revision`` trap) — the three store SELECTs are bounded
        + cheap and stay live. Mid-pull the adaptive floor may serve a
        bounded-stale triple list (logged ``movers_cache.stale_served``).
        """
        report_basis = _report_basis_literal(basis)
        store = _require_store()
        lake_root = getattr(store, "path", None)
        if not isinstance(lake_root, Path):
            # Same additive freshness-key shape as the real path (stable key set;
            # DEC-STALENESS-CACHE-TRUTH-001) — a store without a filesystem lake
            # has trivially never observed a prebuild.
            return JSONResponse(
                {
                    "symbols": [],
                    "basis": report_basis,
                    "basis_derived": report_basis == "day",
                    "derived_last_built": None,
                }
            )
        lake = lake_root.parent

        def _resolve() -> dict[str, Any]:
            """Tag pullability off the event loop; discovery is generation-memoised."""
            # DEC-DASHBOARD-EVENTLOOP-OFFLOAD-002: the generation probe (ledger stat +
            # sweeps-tree digest) runs HERE on the threadpool worker, never the loop.
            gen = _summary_cache.generation
            triples = _db_datasets_cache.get_or_compute(
                preopen_datasets_key(report_basis),
                generation=gen,
                compute=lambda: resolve_report_datasets(lake, basis=report_basis),
            )
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
                # 1day-basis freshness (DEC-STALENESS-CACHE-TRUTH-001): the 1day
                # partitions the "day" basis reads are written ONLY by the
                # pipeline prebuild (DEC-PIPELINE-AUTOPREBUILD-001), so a
                # CLI-pulled / mid-pull lake serves a 1day basis that lags the
                # 1-minute truth. `derived_last_built` = the last SUCCESSFUL
                # post-prebuild footprint OBSERVATION (lake_footprint's
                # META_MEASURED_AT stamp, one O(1) get_meta — lake-wide, not
                # per-symbol; best-effort writer, so None = honestly never
                # observed, e.g. a CLI-only lake — never a fabricated time).
                "basis_derived": report_basis == "day",
                "derived_last_built": store.get_meta(META_MEASURED_AT),
            }

        return JSONResponse(await _run_lake(_resolve))

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
            """Serve the memoised report rows off the event loop (threadpool worker)."""
            # DEC-DB-LATENCY-HARDENING-001: rows memoised per (basis, range,
            # symbols filter) at the write generation — a repeat click within one
            # generation is an O(1) handoff (ZERO dataset walks, ZERO DuckDB
            # statements, spy-pinned); the cold build (discovery walk + per-symbol
            # aggregates) runs on this threadpool worker, building store.connection
            # here so the store RLock stays on this thread (DEC-DASHBOARD-FASTREAD-001).
            return _preopen_rows_cache.get_or_compute(
                _preopen_rows_key(report_basis, start_d, end_d, wanted),
                generation=_summary_cache.generation,
                compute=lambda: build_preopen_rows(
                    store.connection,
                    lake,
                    datasets=resolve_report_datasets(lake, wanted, basis=report_basis),
                    start=start_d,
                    end=end_d,
                    basis=report_basis,
                ),
            )

        rows = await _run_lake(_build_rows, lake_root.parent) if isinstance(lake_root, Path) else []
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
            """Serve the memoised report rows off the event loop (threadpool worker).

            DEC-DB-LATENCY-HARDENING-001: the SAME memo coordinate as the
            ``.xlsx`` route — mode/min_pct shape only the RENDER below, so both
            exports share ONE cached row build per (basis, range, symbols).
            """
            return _preopen_rows_cache.get_or_compute(
                _preopen_rows_key(report_basis, start_d, end_d, wanted),
                generation=_summary_cache.generation,
                compute=lambda: build_preopen_rows(
                    store.connection,
                    lake,
                    datasets=resolve_report_datasets(lake, wanted, basis=report_basis),
                    start=start_d,
                    end=end_d,
                    basis=report_basis,
                ),
            )

        rows = await _run_lake(_build_rows, lake_root.parent) if isinstance(lake_root, Path) else []
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
    async def api_cascade_run() -> JSONResponse:
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
            from brutex.data.futures_series import FuturesSeriesError
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
            # The dashboard twin of cmd_cascade's user-error net (review
            # round-2 findings 10/12): the replay's frozen-vs-re-derived
            # roll-schedule cross-check refusal + a futures-series resolution
            # refusal (interior hole appeared since launch) are operator-
            # remediable lake states — a clear 400, never a 500 traceback.
            # NO VIX stamper build here (DEC-VIX-STAMP-WIRE-001 round-6): the
            # replayed candidates' trades are DISCARDED streams (report bytes
            # identical either way), so the old per-click
            # vix_stamper_for_manifest build paid a full window-bounded VIX
            # lake read for zero observable output.
            try:
                bars_for = load_bars_for_manifest(manifest, lake_root=lake)
                report = run_and_write_from_manifest(root, bars_for=bars_for)
            except (CascadeWireError, FuturesSeriesError) as exc:
                return 400, {"ok": False, "error": str(exc)}
            return 200, {
                "ok": True,
                "sweep_id": manifest.sweep_id,
                "pools": len(report.groups),
                "candidates": report.n_input,
                "survivors": report.n_survived,
            }

        status_code, payload = await _run_lake(_run)
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
        pull OR sweep is already running IN THIS PROCESS is a no-op
        (``{"started": false}``) — one lake, one writer (CLAUDE.md §2 Rule 7
        idempotence). Cross-process exclusion is owned by ``cmd_sweep_all``
        itself (G10 preload read-guard + G6 sweep single-flight,
        DEC-SWEEP-LOCK-PULL-CONCURRENCY-001): a sweep started while another
        process's pull holds the lake flock fails loudly at preload
        (``LakeBusyError`` in the progress feed), and a pull/crossverify can run
        CONCURRENTLY with the sweep's compute phase.

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

        # On a SUCCESSFUL sweep, bump the dashboard write-generation (the sweep
        # _work never reports per-bar progress, so the _on_progress mark_dirty
        # funnel never fires for a sweep — MAJOR-1, DEC-DASHBOARD-RESULTS-PAGE-001).
        # Since DEC-LEDGER-DELTA-O-PAGE-001 this hook is REDUNDANT for /results
        # itself (the leaderboard keys on the `sweep_results_revision` content
        # token, which the store flush moves) — it is KEPT because every OTHER
        # generation-keyed cache still rides `_summary_cache.generation` (zero
        # drift). Fires only on success, never on a failed sweep (see
        # BackgroundPullJob._run).
        # job_kind=SWEEP (DEC-RATE-METER-LIFECYCLE-001): the kind-aware rate probe
        # reads a running sweep as "no PULL active" so the site-wide PULL RATE widget
        # stays honest dark "idle" for the sweep's 2-4-day wall time — never a red
        # "stalled" off the (correctly) zero vendor rate. Single-flight, progress and
        # the duty-degrade probes are unchanged (they stay sweep-inclusive).
        # cross_process_lock=False (DEC-SWEEP-LOCK-PULL-CONCURRENCY-001): the
        # sweep engine OWNS its own cross-process exclusion — cmd_sweep_all
        # takes the G10 lake read-guard (LOCK_SH on the SAME pull single-flight
        # file, held across the bar PRELOAD ONLY) + the G6 sweep single-flight
        # (DEC-SWEEP-UNKILLABLE-CORE-001). A job-level whole-run EX here BOTH
        # self-conflicted with G10 (flock is per open file description — the
        # dashboard sweep died at preload with LakeBusyError since G10 landed,
        # commit 3f32b3e) AND would lock the daily pull/crossverify out for the
        # sweep's multi-day wall time (the crossverify precedent — the callee
        # owns its locks). A sweep started while a CROSS-process pull holds the
        # lake flock now fails LOUDLY at preload (LakeBusyError in the progress
        # feed, bounded to the preload) instead of a start()-refusal; the
        # IN-process second click stays the started=false no-op (layer 1).
        started = _pull_job.start(
            _work,
            total=1,
            detail=f"sweep {request.symbols}",
            on_success=_summary_cache.mark_dirty,
            job_kind=JOB_KIND_SWEEP,
            cross_process_lock=False,
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
        preset: str | None = Query(default=None),
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
        NEVER touches the brute-force engine or the 60-condition vocabulary (the
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
            never a 500. IGNORED for ordering when ``preset`` is given.
        preset
            OPTIONAL R30 ranking-preset token (DEC-SWEEP-RESULTS-DEPTH-001,
            gap-register ruling 10) — one of
            :data:`brutex.ranking.presets.PRESET_NAMES`. Absent/blank keeps the
            legacy ``rank_by`` ordering BYTE-IDENTICAL (payload shape included);
            a known token orders via the ONE preset law in
            :mod:`brutex.ranking.presets` (full precision, ``run_id``-ASC
            deterministic ties — the store's total-order idiom) and the payload
            gains the additive ``preset`` + ``preset_inert_axes`` keys; an
            UNKNOWN token is a clean 400 naming the valid values (an explicit
            operator choice must refuse loudly, never silently rank by
            something else). THIN adapter — the ranking logic lives entirely in
            ``brutex.ranking`` (this route only validates + threads the token).
        top
            Leaderboard depth (1..500; default 50).

        **Store-fault degrade (DEC-SWEEP-RESULTS-DEPTH-001, C3):** a typed
        :class:`~brutex.engine.results_store.ResultsStoreError` escaping the
        per-store skip net returns the honest
        :func:`~brutex.dashboard.sweep_results_view.store_error_payload` (200
        with explicit ``error`` / ``error_type`` markers + the full empty
        leaderboard shape) — never a 500, and never the bare ``available:
        false`` blank that would masquerade as "no sweep run yet".

        **Latency (DEC-DASHBOARD-RESULTS-PAGE-001, re-keyed by
        DEC-LEDGER-DELTA-O-PAGE-001):** the ``<lake>/sweeps`` read is a pure
        function of the sweep tree, so it is memoised behind the
        ``sweep_results_revision`` CONTENT token (the sweeps directory-mtime
        digest + the audit chain's file stat), folded into the cache KEY at a
        fixed generation. A mid-pull window-ledger checkpoint (which bumps the
        write generation via the ``pull_jobs`` ``mark_dirty`` funnel, per window
        tick) no longer invalidates the leaderboard; a results-store flush from
        the dashboard sweep OR a CLI ``brutex sweep-all`` (which never calls
        ``mark_dirty`` — the case the old generation keying NEVER covered), a
        store delete, and a ``SWEEP_START`` audit append ALL move the token.
        Two-level caching: the rank-independent :class:`SweepScan` is cached
        ONCE per revision, so a filter/rank/top_n click within one revision
        re-pays only the rank + projection — zero store loads, zero audit
        reads; a repeat poll of a viewed coordinate is an O(1) hit. Honest
        envelope (Golden Rule #10): every poll pays one O(dirs)+O(1-stat)
        revision probe; the first read per revision pays the real
        ``O(stores × rows)`` scan; a probe outage (token ``None``) computes
        fresh and caches NOTHING — never a stale serve, never a poisoned key.

        **Honest envelope (Golden Rule #10):** an empty / absent ``<lake>/sweeps``
        (no sweep run yet) returns ``available: false`` with zero rows — never a
        fabricated winner; a corrupt per-tuple Parquet is SKIPPED + counted, never a
        500. When no lake root is wired the payload is the same honest empty shape.
        """
        # ── P3 INTEGRATION ADAPTER (DEC-SWEEP-RESULTS-DEPTH-001, ruling 10) ──
        # Validate the preset token EAGERLY (before any threadpool hop / cache
        # key build): an unknown token is a deterministic 400 naming the valid
        # values; None/blank resolves to None = the byte-identical legacy path.
        try:
            resolved_preset = resolve_preset_param(preset)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

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
                preset=resolved_preset,
            )
            return JSONResponse(empty.to_dict())
        lake_root = store_path.parent

        def _resolve() -> dict[str, Any]:
            """Cache-or-compute the projected leaderboard on a threadpool worker.

            Runs OFF the event loop: the ``sweep_results_revision`` probe is
            O(dirs)+O(1-stat) per poll, the ``composite_index_revision``
            probe adds O(run dirs + results-depth dirs) ``scandir``/``stat``
            per poll — NAME-only, zero manifest reads/parses (disclosed —
            fix round 1; the per-poll manifest.json read+parse the
            ``discover_runs`` hop hid was removed in fix round 2,
            bench-pinned) — and the ``compute=`` closures perform the real
            ``<lake>/sweeps`` scan / rank ONLY on a miss
            (DEC-LEDGER-DELTA-O-PAGE-001).
            """
            # DEC-O1-GUARANTEE-HARDEN-001 H2: the sweeps digest is an O(sweep
            # dirs) scandir sweep per poll — memoise the token for the short
            # TTL (one sweep per window; an outage `None` is never memoised,
            # so the outage arm below re-derives per poll exactly as before).
            rev = _revision_probe_memo.get(
                f"sweep_results|{lake_root}",
                lambda: sweep_results_revision(lake_root),
            )
            # DEC-RESULTS-CASCADE-VERDICT-JOIN-001: the cascade report is
            # atomically OVERWRITTEN in place per sweep — the rewrite moves NO
            # directory mtime the sweeps digest sees, so its DEDICATED file
            # stat token folds into the CACHE KEY (a rewrite mints a new key →
            # recompute; the sweep_scan itself stays generation-keyed). A
            # probe OUTAGE (None) folds the literal "outage" segment: the
            # cached decorated payload keeps serving for the outage's duration
            # (bounded staleness, WARNed inside the probe; recovery on the
            # next successful stat — the r1 MINOR-1 doctrine).
            casc_rev = cascade_report_revision(lake_root)
            sweeps_root = lake_root / SWEEPS_DIRNAME
            # Fix round 1 (refuter-confirmed MAJOR): the aggs sidecars feeding
            # the composite index live under the sweep_runs/ SIBLING tree,
            # which sweep_results_revision NEVER stats — and cmd_sweep_all's
            # depth pass lands them AFTER the sweep's last store flush / audit
            # append, so keying the index on the sweeps generation alone
            # provably froze an EMPTY/PARTIAL index (a poll landing
            # mid-depth-pass) until the NEXT sweep. The DEDICATED probe folds
            # into the payload cache key AND drives the composite-index slot's
            # generation below; a probe OUTAGE (None) folds the literal
            # "outage" segment — the cached payload holds (the casc_rev
            # doctrine above).
            comp_rev = composite_index_revision(sweeps_root)
            # The STABLE (revision-free) filter key. Casefold the filter
            # axes: build_sweep_leaderboard's _matches filters them
            # case-insensitively, so ``NIFTY`` and ``nifty`` yield the SAME
            # leaderboard and must share ONE cache slot (rank_by stays raw — it
            # is matched exactly against RANKABLE_COLUMNS, not casefolded; the
            # RESOLVED preset token rides too — a preset-ranked payload must
            # never share a slot with the default ordering, and blank/absent
            # share ONE slot because both resolve to None).
            lb_key = (
                f"sweep_leaderboard|i={(instrument or '').casefold()}"
                f"|tf={(tf or '').casefold()}|d={(direction or '').casefold()}"
                f"|r={rank_by or ''}|p={resolved_preset or ''}|n={top}"
                f"|casc={casc_rev or 'outage'}|comp={comp_rev or 'outage'}"
            )

            def _fresh() -> dict[str, Any]:
                return build_sweep_leaderboard(
                    lake_root,
                    instrument=instrument,
                    tf=tf,
                    direction=direction,
                    rank_by=rank_by,
                    top_n=top,
                    preset=resolved_preset,
                    cascade=load_cascade_join_file(sweeps_root, token=casc_rev),
                    composite=load_composite_index(sweeps_root),
                ).to_dict()

            if rev is None:
                # Probe OUTAGE (`sweep_results_revision` returns None ONLY on a
                # non-missing-file OSError — an absent sweeps tree is the stable
                # ABSENT_TOKEN composite): serve the LAST-KNOWN payload for this
                # filter key, labelled + WARN-latched (DEC-CACHE-SERVE-LAST-
                # KNOWN-001, C-r2-3 — the pre-change "compute fresh, cache
                # NOTHING" doctrine re-paid the full O(stores × rows) scan on
                # EVERY poll for the outage's duration).
                payload, served = _sweep_leaderboard_cache.serve_last_known(lb_key, compute=_fresh)
                return _probe_outage_payload(payload, served_last_known=served)
            # Level 1: ONE rank-independent SweepScan per revision — the
            # O(stores × rows) load + the audit era pass run once, then every
            # filter/rank/top_n click within this revision re-ranks it. The
            # revision drives the GENERATION via the ONE `revision_generation`
            # digest bridge over STABLE keys (DEC-NAV-CACHES-FLOOR-REKEY-001):
            # invalidation is equivalent to the superseded rev-in-key fold
            # (any token change moves the generation), while a RUNNING
            # dashboard sweep's per-flush revision moves can now engage the
            # instance's pull-time adaptive floor (bounded, logged stale
            # serves) instead of paying the full scan per poll.
            gen = revision_generation(rev)
            scan: SweepScan = _sweep_leaderboard_cache.get_or_compute(
                "sweep_scan",
                generation=gen,
                compute=lambda: load_sweep_scan(lake_root),
            )
            # Level 1b (DEC-RESULTS-CASCADE-VERDICT-JOIN-001, fix round 1):
            # ONE composite-index read per `composite_index_revision` token —
            # the slot's generation rides the ONE `revision_generation` digest
            # bridge over the DEDICATED probe, NOT the sweeps generation. The
            # pre-fix comment's sufficiency claim ("aggs writes move
            # sweep_results_revision") was FALSE: the depth pass writes the
            # aggs AFTER the sweep's last store flush / audit append into the
            # sweep_runs/ sibling tree the sweeps token never stats, so `gen`
            # alone froze an empty/partial index until the NEXT sweep
            # (refuter-reproduced; now killed by the route regression). Any
            # aggs write moves a results-depth dir mtime → new token →
            # recompute; a sweeps-only move (store flush) leaves the cached
            # index untouched.
            composite_index: CompositeIndex = _sweep_leaderboard_cache.get_or_compute(
                "composite_index",
                generation=revision_generation(comp_rev or "outage"),
                compute=lambda: load_composite_index(sweeps_root),
            )
            # Level 2: the ranked payload per stable filter key (same token
            # generation; `mru_prefix` records the last-known pointer the
            # probe-outage arm above serves; the cascade join parses ONCE per
            # report token via the loader's own single-entry memo).
            return _sweep_leaderboard_cache.get_or_compute(
                lb_key,
                generation=gen,
                compute=lambda: build_sweep_leaderboard(
                    lake_root,
                    instrument=instrument,
                    tf=tf,
                    direction=direction,
                    rank_by=rank_by,
                    top_n=top,
                    scan=scan,
                    preset=resolved_preset,
                    cascade=load_cascade_join_file(sweeps_root, token=casc_rev),
                    composite=composite_index,
                ).to_dict(),
                mru_prefix=lb_key,
            )

        try:
            return JSONResponse(await _run_lake(_resolve))
        except ResultsStoreError as exc:
            # C3 (DEC-SWEEP-RESULTS-DEPTH-001): a typed store fault escaping the
            # per-store skip net degrades to the honest error payload — 200 with
            # explicit error markers, never a 500, never a blank illusion. The
            # payload is SANITIZED (CodeQL py/stack-trace-exposure): str(exc)
            # never reaches the wire — store_error_payload logs the full detail
            # server-side and serves only the stable generic markers.
            return JSONResponse(store_error_payload(exc))

    # ── One-click artifact routes (DEC-OPERATOR-PAGE-REAL-001) ──────────
    # The ONE integration-adapter surface over the sweep-oneclick artifact
    # CONTRACTS (manifest.json / top100.json / events.jsonl / trades pages) —
    # thin closures over the 100%-unit-tested `oneclick_artifacts` +
    # `oneclick_view` modules, offloaded via `run_in_threadpool` (the app.py
    # doctrine). Unknown run dirs are HONEST 200 `status:"awaiting"` payloads
    # (polls must never error-loop); ONLY a genuinely bad request — missing/
    # unsafe run_id, a junk BRUTEX_ONECLICK_FIXTURE_ROOT — is a 400.

    def _oneclick_require_run_id(run_id: str | None) -> str:
        """400 unless *run_id* is present and ONE safe path segment (no traversal)."""
        if run_id is None or not run_id.strip():
            raise HTTPException(status_code=400, detail="run_id query parameter is required")
        rid = run_id.strip()
        if not oneclick_artifacts.is_safe_run_id(rid):
            raise HTTPException(
                status_code=400,
                detail="run_id must be one safe path segment ([A-Za-z0-9._-])",
            )
        return rid

    def _oneclick_roots() -> tuple[Path | None, Path | None]:
        """Resolve ``(results_root, fixture_root)`` — the fixture override wins.

        Returns ``(None, None)`` when no lake root is wired — no instrument
        store at all (mirrors ``_require_store``'s condition WITHOUT raising
        its 503) or a store lacking a real ``path`` — so the routes serve the
        honest ``status:"awaiting"`` payload, never a 500/503. A junk fixture
        env raises the 400.
        """
        try:
            fixture_root = oneclick_artifacts.resolve_fixture_root()
        except oneclick_artifacts.OneclickConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if fixture_root is not None:
            # The pseudo sweeps root INSIDE the fixture tree — the Ruling-4
            # parent-sibling derivation then lands runs at
            # <fixture_root>/sweep_runs/ (self-contained, never escapes).
            return oneclick_artifacts.fixture_results_root(fixture_root), fixture_root
        if instrument_store is None:
            return None, None
        store_path = getattr(instrument_store, "path", None)
        if not isinstance(store_path, Path):
            return None, None
        return default_results_root(store_path.parent), None

    # Fixture/production reader instances memoised per root so their
    # parse-once-per-revision memos survive across requests (owned by this app
    # instance — no module-global mutable state, CLAUDE.md §4).
    _oneclick_fixture_readers: dict[str, oneclick_artifacts.FixtureTradesReader] = {}
    _oneclick_store_readers: dict[str, oneclick_artifacts.TradeStoreReader] = {}

    def _oneclick_reader(
        results_root: Path | None, fixture_root: Path | None
    ) -> oneclick_artifacts.TradesReader:
        """The typed trades seam (DEC-ONECLICK-SWEEP-RUNS-REBIND-001 order).

        injected reader > fixture reader (env override) > the PRODUCTION
        ``TradeStoreReader`` over the #1275 results-depth sidecars (per-run
        honest awaiting when absent) > ``AwaitingTradesReader`` when no lake
        root is wired at all.
        """
        if oneclick_trades_reader is not None:
            return oneclick_trades_reader
        if fixture_root is not None:
            fkey = str(fixture_root)
            freader = _oneclick_fixture_readers.get(fkey)
            if freader is None:
                freader = oneclick_artifacts.FixtureTradesReader(fixture_root)
                _oneclick_fixture_readers[fkey] = freader
            return freader
        if results_root is None:
            return oneclick_artifacts.AwaitingTradesReader()
        skey = str(results_root)
        sreader = _oneclick_store_readers.get(skey)
        if sreader is None:
            sreader = oneclick_artifacts.TradeStoreReader(results_root)
            _oneclick_store_readers[skey] = sreader
        return sreader

    @app.get("/api/v1/oneclick/runs")
    async def api_oneclick_runs() -> JSONResponse:
        """GET /api/v1/oneclick/runs — discovered sweep runs, newest first.

        Lists the Ruling-4 ``sweep_runs/<run_id>/`` directories with per-artifact
        presence flags + the manifest's ``suspect`` verdict (``null`` until
        readable — never fabricated). Memoised per ``runs/`` dir stat token;
        an empty/absent tree is the loud ``status:"awaiting"`` payload; a
        stat OUTAGE serves the last-known listing with one loud WARN per
        outage (C-r2-5 — never a silent per-poll re-walk).
        """

        def _resolve() -> dict[str, Any]:
            """Build the oneclick runs payload off the event loop (revision-cached)."""
            root, fixture_root = _oneclick_roots()
            fixture_active = fixture_root is not None
            if root is None:
                return oneclick_view.build_runs_payload((), fixture_root_active=fixture_active)
            results_root: Path = root
            # DEC-O1-GUARANTEE-HARDEN-001 H2: the probe is an UNCAPPED O(run
            # dirs) manifest-stat walk per poll (the F5 ruling) — memoise the
            # RESULT for the short TTL so N polls within one window pay ONE
            # walk (staleness ≤ TTL; absence/outage verdicts never memoised).
            probe = _revision_probe_memo.get(
                f"oc_runs|{results_root}",
                lambda: oneclick_artifacts.runs_revision_probe(results_root),
                cacheable=lambda p: p.token is not None,
            )

            def _fresh() -> dict[str, Any]:
                return oneclick_view.build_runs_payload(
                    oneclick_artifacts.discover_runs(results_root),
                    fixture_root_active=fixture_active,
                )

            key = None if probe.token is None else f"oc_runs|rev={probe.token}|fx={fixture_active}"
            return oneclick_artifacts.serve_revision_cached(
                _oneclick_cache,
                _oneclick_outages,
                coordinate=f"oc_runs|fx={fixture_active}",
                probe=probe,
                key=key,
                compute=_fresh,
            )

        return JSONResponse(await _run_lake(_resolve))

    @app.get("/api/v1/oneclick/manifest")
    async def api_oneclick_manifest(
        run_id: str | None = Query(default=None),
    ) -> JSONResponse:
        """GET /api/v1/oneclick/manifest?run_id= — one run's manifest + pill state.

        The P2 contract keys pass through VERBATIM; the pill "state" word is
        derived mechanically and the payload names the derivation
        (``pill.derived_from`` — C-MINOR-2). Missing → awaiting; bad JSON →
        corrupt; missing contract keys → partial naming them. NEVER invents
        counts.
        """
        rid = _oneclick_require_run_id(run_id)

        def _resolve() -> dict[str, Any]:
            """Build the manifest payload off the event loop (stat-token cached)."""
            root, fixture_root = _oneclick_roots()
            fixture_active = fixture_root is not None
            if root is None:
                return oneclick_view.build_manifest_payload(
                    rid,
                    oneclick_artifacts.ManifestResult(
                        "awaiting", None, "no lake configured — awaiting real run artifact", None
                    ),
                    fixture_root_active=fixture_active,
                )
            run_dir = oneclick_artifacts.runs_root(root) / rid
            probe = oneclick_artifacts.artifact_probe(
                run_dir / oneclick_artifacts.MANIFEST_FILENAME
            )
            # Sibling-artifact PRESENCE rides the cache KEY as two cheap stat
            # probes (never a parse) so the pill's missing-file naming
            # refreshes when top100.json / events.jsonl land without a
            # manifest rewrite; the manifest PARSE (incl. the pill probe)
            # stays inside the cached compute — parse once per revision.
            # r1 MINOR-1: the sibling stats ride the SAME outage doctrine via
            # sibling_key_token — a transient sibling stat flap holds the
            # last-known token (one WARN) instead of flapping the key.
            top100_tok = oneclick_artifacts.sibling_key_token(
                _oneclick_outages,
                coordinate=f"oc_manifest|{rid}|sib=top100|fx={fixture_active}",
                probe=oneclick_artifacts.artifact_probe(
                    run_dir / oneclick_artifacts.TOP100_FILENAME
                ),
            )
            has_top100 = top100_tok is not None
            events_rev = oneclick_artifacts.sibling_key_token(
                _oneclick_outages,
                coordinate=f"oc_manifest|{rid}|sib=events|fx={fixture_active}",
                probe=oneclick_artifacts.artifact_probe(
                    run_dir / oneclick_artifacts.EVENTS_FILENAME
                ),
            )
            has_events = events_rev is not None

            def _fresh() -> dict[str, Any]:
                manifest_result = oneclick_artifacts.load_manifest(run_dir)
                payload = oneclick_view.build_manifest_payload(
                    rid,
                    manifest_result,
                    fixture_root_active=fixture_active,
                    info=oneclick_artifacts.probe_run_dir(run_dir),
                )
                # R41 chain map: link states derive from per_stage_timings +
                # tripwires + the RECENT event kinds (one O(page) tail read —
                # never a whole-file scan); an unknown stage reads "pending",
                # never green (DEC-OPERATOR-PAGE-REAL-001).
                recent = oneclick_artifacts.tail_events(run_dir, offset=-1, limit=200)
                kinds = [str(rec.get("kind", "")) for rec in recent.records]
                payload["chain_links"] = oneclick_view.chain_links(manifest_result, kinds)
                return payload

            # The key folds the manifest revision AND the events revision (the
            # chain-map input) — a growing events.jsonl refreshes the links.
            key = None
            if probe.token is not None:
                key = (
                    f"oc_manifest|{rid}|rev={probe.token}"
                    f"|files={int(has_top100)}{int(has_events)}"
                    f"|ev={events_rev}|fx={fixture_active}"
                )
            return oneclick_artifacts.serve_revision_cached(
                _oneclick_cache,
                _oneclick_outages,
                coordinate=f"oc_manifest|{rid}|fx={fixture_active}",
                probe=probe,
                key=key,
                compute=_fresh,
            )

        return JSONResponse(await _run_lake(_resolve))

    @app.get("/api/v1/oneclick/top100")
    async def api_oneclick_top100(
        run_id: str | None = Query(default=None),
    ) -> JSONResponse:
        """GET /api/v1/oneclick/top100?run_id= — the P3 leaderboard rows, verbatim.

        Rows pass through UNTOUCHED (metrics keep full precision — money 2dp
        is client-side via ``bxFixed2`` ONLY); ``row_count`` is always
        ``len(rows)`` (a valid EMPTY artifact is ``ok`` with the honest
        ``empty_note`` — T-M2). A bad row degrades to ``partial`` with the
        row index named, rows kept up to it — never a 500.
        """
        rid = _oneclick_require_run_id(run_id)

        def _resolve() -> dict[str, Any]:
            """Build the top100 payload off the event loop (stat-token cached)."""
            root, fixture_root = _oneclick_roots()
            fixture_active = fixture_root is not None
            if root is None:
                return oneclick_view.build_top100_payload(
                    rid,
                    oneclick_artifacts.Top100Result(
                        "awaiting", (), "no lake configured — awaiting real run artifact", None
                    ),
                    fixture_root_active=fixture_active,
                )
            run_dir = oneclick_artifacts.runs_root(root) / rid
            probe = oneclick_artifacts.artifact_probe(run_dir / oneclick_artifacts.TOP100_FILENAME)

            def _fresh() -> dict[str, Any]:
                return oneclick_view.build_top100_payload(
                    rid,
                    oneclick_artifacts.load_top100(run_dir),
                    fixture_root_active=fixture_active,
                )

            key = None
            if probe.token is not None:
                key = f"oc_top100|{rid}|rev={probe.token}|fx={fixture_active}"
            return oneclick_artifacts.serve_revision_cached(
                _oneclick_cache,
                _oneclick_outages,
                coordinate=f"oc_top100|{rid}|fx={fixture_active}",
                probe=probe,
                key=key,
                compute=_fresh,
            )

        return JSONResponse(await _run_lake(_resolve))

    @app.get("/api/v1/oneclick/events")
    async def api_oneclick_events(
        run_id: str | None = Query(default=None),
        offset: int = Query(default=0, ge=-1),
        limit: int = Query(default=200, ge=1, le=1000),
        kind: str | None = Query(default=None),
        q: str | None = Query(default=None),
    ) -> JSONResponse:
        """GET /api/v1/oneclick/events — O(page) byte-offset flight-recorder tail.

        ``seek(offset)`` + read forward ≤ ``limit`` lines bounded by a 1 MiB
        window — server work is bounded by the READ WINDOW, never the log
        size; ``kind``/``q`` filter that window only (``window_filtered`` in
        the payload states it). ``offset=-1`` anchors at the tail; a repeat
        poll at ``offset == total_bytes`` with an unchanged stat token
        short-circuits via the cache. A half-written last line is the honest
        ``truncated_tail`` — re-read once complete, never an error.
        """
        rid = _oneclick_require_run_id(run_id)

        def _resolve() -> dict[str, Any]:
            """Tail the events page off the event loop (byte-offset seek, O(page))."""
            root, fixture_root = _oneclick_roots()
            fixture_active = fixture_root is not None
            window_filtered = bool(kind or q)
            if root is None:
                return oneclick_view.build_events_payload(
                    rid,
                    oneclick_artifacts.EventsPage(
                        "awaiting",
                        (),
                        0,
                        0,
                        False,
                        "no lake configured — awaiting real run artifact",
                    ),
                    fixture_root_active=fixture_active,
                    window_filtered=window_filtered,
                )
            run_dir = oneclick_artifacts.runs_root(root) / rid
            probe = oneclick_artifacts.artifact_probe(run_dir / oneclick_artifacts.EVENTS_FILENAME)

            def _fresh() -> dict[str, Any]:
                page = oneclick_artifacts.tail_events(
                    run_dir, offset=offset, limit=limit, kind=kind, q=q
                )
                return oneclick_view.build_events_payload(
                    rid,
                    page,
                    fixture_root_active=fixture_active,
                    window_filtered=window_filtered,
                )

            # json-encoded key parts: kind/q are operator-typed strings that
            # may contain the `|` separator — encoding keeps keys collision-free.
            coordinate = "oc_events|" + json.dumps(
                [rid, offset, limit, kind, q, fixture_active], ensure_ascii=False
            )
            key = None
            if probe.token is not None:
                key = "oc_events|" + json.dumps(
                    [rid, offset, limit, kind, q, probe.token, fixture_active],
                    ensure_ascii=False,
                )
            return oneclick_artifacts.serve_revision_cached(
                _oneclick_cache,
                _oneclick_outages,
                coordinate=coordinate,
                probe=probe,
                key=key,
                compute=_fresh,
            )

        return JSONResponse(await _run_lake(_resolve))

    @app.get("/api/v1/oneclick/trades")
    async def api_oneclick_trades(
        run_id: str | None = Query(default=None),
        rank: int = Query(default=1, ge=1),
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=1000),
    ) -> JSONResponse:
        """GET /api/v1/oneclick/trades — one paged trade list via the typed seam.

        PRODUCTION DEFAULT is the ``TradeStoreReader`` over the SHIPPED #1275
        results-depth sidecars (DEC-ONECLICK-SWEEP-RUNS-REBIND-001) — a run
        without those sidecars degrades to the honest awaiting page (the
        ``AwaitingTradesReader`` semantics, preserved); the file-reading
        ``FixtureTradesReader`` activates ONLY under the fail-loud
        ``BRUTEX_ONECLICK_FIXTURE_ROOT``. ``source`` is a VISIBLE badge
        client-side (C-M5); ``total`` stays ``null`` when unknown — never a
        fabricated 0; VIX + lots/costs nulls pass through (R31 honest NULL /
        Ruling 18 NULL-by-design).
        """
        rid = _oneclick_require_run_id(run_id)

        def _resolve() -> dict[str, Any]:
            """Build the trades payload off the event loop via the TradesReader seam."""
            root, fixture_root = _oneclick_roots()
            reader = _oneclick_reader(root, fixture_root)
            page = reader.page(rid, rank, offset=offset, limit=limit)
            return oneclick_view.build_trades_payload(
                rid,
                rank,
                page,
                offset=offset,
                limit=limit,
                fixture_root_active=fixture_root is not None,
            )

        return JSONResponse(await _run_lake(_resolve))

    @app.get("/api/v1/oneclick/equity")
    async def api_oneclick_equity(
        run_id: str | None = Query(default=None),
        rank: int = Query(default=1, ge=1),
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=5000, ge=1, le=5000),
    ) -> JSONResponse:
        """GET /api/v1/oneclick/equity — the stored minute-path equity SERIES page.

        DEC-ONECLICK-WINNER-DETAIL-TRUTH-001 (audit item 4C — the #48
        overlap): serves the entry's ``equity_ref`` sidecar points
        (``{session_date, equity_close_paisa, equity_intraday_low_paisa,
        trades_closed}``) through the SAME typed reader seam + honesty
        degrades as the trades route (pre-#1275 artifact / absent sidecars /
        fixture / no-lake → honest awaiting; hostile tuple ref / unreadable
        sidecar → NAMED corrupt; never a 500, never a fabricated curve).
        HONEST O-envelope: the FIRST read per (sidecar revision, combo) is an
        O(equity-file) run_id filter (the stamp indexes no equity spans);
        repeat pages slice the reader's memo — O(page). One row per session,
        so the default limit 5000 charts ~20 years in one fetch.
        """
        rid = _oneclick_require_run_id(run_id)

        def _resolve() -> dict[str, Any]:
            """Build the equity payload off the event loop via the TradesReader seam."""
            root, fixture_root = _oneclick_roots()
            reader = _oneclick_reader(root, fixture_root)
            page = reader.equity_page(rid, rank, offset=offset, limit=limit)
            return oneclick_view.build_equity_payload(
                rid,
                rank,
                page,
                offset=offset,
                limit=limit,
                fixture_root_active=fixture_root is not None,
            )

        return JSONResponse(await run_in_threadpool(_resolve))

    @app.get("/api/v1/oneclick/verify")
    async def api_oneclick_verify(
        run_id: str | None = Query(default=None),
        check: str | None = Query(default=None),
    ) -> JSONResponse:
        """GET /api/v1/oneclick/verify?run_id=[&check=chain] — the R44 panel rows.

        Seven checks under the 5-state truth table (pass/fail/ready/awaiting/
        not_wired): operator-judged checks cap at ``ready`` server-side;
        un-wired capabilities read ``not_wired``, never a fake green.
        ``check=chain`` triggers the O(file) hash-chain verification
        (button-click ONLY — its verdict is cached per events revision);
        without it the chain row serves the LAST cached verdict, or the
        honest ``awaiting`` when this revision was never verified (the
        raising-compute peek stores nothing — T-M4 cache-keyed resolution).

        F1 (DEC-ONECLICK-PERF-CLUB-001): the WHOLE payload is revision-cached
        like the sibling routes — the key folds the manifest stat token
        (primary probe) + the events + top100 tokens + the chain
        verified-state, so a primed poll is three ``os.stat`` probes + the
        raising chain cache-peek + one hit (r2 sweep M1 — the chain state is
        a cache-peek, never a stat; the B-7 bench measures exactly this
        shape); the FIRST poll per changed revision pays the
        O(manifest) parse + funnel reconcile + the one-row trades probe.
        HONEST bound (Golden Rule #10): a results-depth sidecar rewrite that
        moves NONE of the three artifact tokens does not move the key — in
        the shipped #1617 pass the sidecars land BEFORE top100.json, so the
        trades-wired flag refreshes with the top100 token; a manifest-less
        run computes fresh per poll (cheap: no manifest to parse).
        """
        rid = _oneclick_require_run_id(run_id)

        def _resolve() -> dict[str, Any]:
            """Run the verify checks off the event loop (chain check on click only)."""
            root, fixture_root = _oneclick_roots()
            fixture_active = fixture_root is not None
            if root is None:
                return oneclick_view.build_verify_payload(
                    rid,
                    oneclick_artifacts.ManifestResult(
                        "awaiting", None, "no lake configured — awaiting real run artifact", None
                    ),
                    None,
                    False,
                    fixture_root_active=fixture_active,
                )
            run_dir = oneclick_artifacts.runs_root(root) / rid
            probe = oneclick_artifacts.artifact_probe(
                run_dir / oneclick_artifacts.MANIFEST_FILENAME
            )
            # r1 MINOR-1: sibling stats ride the outage doctrine too — a
            # transient events/top100 stat flap during a manifest-healthy
            # poll keeps the last-known sibling token in the key (one WARN),
            # so the chain row never momentarily degrades to "awaiting".
            events_rev = oneclick_artifacts.sibling_key_token(
                _oneclick_outages,
                coordinate=f"oc_verify|{rid}|sib=events|fx={fixture_active}",
                probe=oneclick_artifacts.artifact_probe(
                    run_dir / oneclick_artifacts.EVENTS_FILENAME
                ),
            )
            top100_rev = oneclick_artifacts.sibling_key_token(
                _oneclick_outages,
                coordinate=f"oc_verify|{rid}|sib=top100|fx={fixture_active}",
                probe=oneclick_artifacts.artifact_probe(
                    run_dir / oneclick_artifacts.TOP100_FILENAME
                ),
            )
            chain: tuple[bool, int, int | None, str] | None = None
            if events_rev is not None:
                chain_key = f"oc_chain|{rid}|rev={events_rev}"
                if check == "chain":
                    chain = _oneclick_cache.get_or_compute(
                        chain_key,
                        generation=0,
                        compute=lambda: oneclick_artifacts.verify_events_chain(run_dir),
                    )
                else:
                    try:
                        chain = _oneclick_cache.get_or_compute(
                            chain_key,
                            generation=0,
                            compute=oneclick_artifacts.raise_chain_not_verified,
                        )
                    except oneclick_artifacts.ChainNotVerifiedError:
                        chain = None

            def _fresh() -> dict[str, Any]:
                manifest = oneclick_artifacts.load_manifest(run_dir)
                trades_probe = _oneclick_reader(root, fixture_root).page(rid, 1, offset=0, limit=1)
                return oneclick_view.build_verify_payload(
                    rid,
                    manifest,
                    chain,
                    trades_probe.status in {"ok", "partial"},
                    fixture_root_active=fixture_active,
                )

            key = None
            if probe.token is not None:
                key = (
                    f"oc_verify|{rid}|rev={probe.token}|ev={events_rev}"
                    f"|t100={top100_rev}|chain={int(chain is not None)}"
                    f"|fx={fixture_active}"
                )
            return oneclick_artifacts.serve_revision_cached(
                _oneclick_cache,
                _oneclick_outages,
                coordinate=f"oc_verify|{rid}|fx={fixture_active}",
                probe=probe,
                key=key,
                compute=_fresh,
            )

        return JSONResponse(await _run_lake(_resolve))

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
        Memoised in the SWR-flagged ``_db_tables_cache``
        (DEC-DASHBOARD-PULL-CONTENTION-RESILIENCE-001, the tables-route fold):
        the COUNT(*) sweep previously ran UNCACHED on the store read connection
        per open and convoyed >12 s behind mid-pull reads holding the store
        read lock (the operator's live tables-list timeout). Now a repeat open
        within one freshness token is an O(1) hit, a token move (a pull
        generation bump OR a keyless ``load-instruments`` master move)
        recomputes, and a past-floor mid-pull open serves the LAST-KNOWN
        counts instantly (logged ``movers_cache.stale_served``) while at most
        ONE tokenless background daemon revalidates — the fresh-compute
        payload is byte-identical to the pre-cache route (pinned). The
        CACHED compute runs ``table_summary(..., raise_on_fault=True)``, so
        a fault is NEVER stored: a probe fault (``_TABLES_PROBE_FAULT_TYPES``
        — the store fault types + the DEC-STORE-WRITE-DEADLINE-001 wedge
        latch's ``CatalogError``) OR the SAME fault striking AFTER a healthy
        probe (the round-8 probe→compute TOCTOU — the strict compute raises
        out of ``get_or_compute``, which caches nothing on a raise) degrades
        to today's exact pre-cache path: one direct UNCACHED default-form
        sweep (honest 0-counts on a faulted/wedged connection — never a 500,
        nothing cached on EITHER degrade arm).
        """
        store = _require_store()

        def _tables() -> list[dict[str, Any]]:
            """Resolve the cached table summary off the event loop (worker thread).

            The freshness probe (one O(1) generation read + the cheap
            ``master_revision`` COUNT+MAX) runs INSIDE the offloaded callable
            — never on the event loop (DEC-DASHBOARD-EVENTLOOP-OFFLOAD-002).
            The compute reads a FRESH ``store.connection`` view per run, so
            the background SWR revalidator never shares a ``_LockedReadConn``
            with a request thread (each view serialises its own
            ``execute → fetch`` under the store read lock).
            """

            def _compute() -> list[dict[str, Any]]:
                # STRICT sweep (round-8 review MINOR — the probe→compute
                # TOCTOU): a store fault / the wedge latch's CatalogError
                # striking AFTER the healthy probe RAISES here instead of
                # folding to all-zero counts, so `get_or_compute` can never
                # store a wedge-degraded summary under the healthy pre-wedge
                # token (a raising compute caches NOTHING and releases the
                # single-flight — the pinned movers_cache law,
                # test_movers_cache.py::TestComputeRaises
                # ::test_exception_propagates_and_nothing_cached).
                return table_summary(store.connection, raise_on_fault=True)

            try:
                gen = _summary_cache.generation
                count, latest = master_revision(store)
                return _db_tables_cache.get_or_compute(
                    db_tables_key,
                    generation=revision_generation(f"gen={gen}|m={count}:{latest}"),
                    compute=_compute,
                )
            except _TABLES_PROBE_FAULT_TYPES:
                # Probe outage OR a WEDGED/faulted store — surfacing at the
                # probe OR mid-compute (the round-8 TOCTOU window; the strict
                # compute's raise set TABLE_SUMMARY_FAULT_TYPES is
                # member-equal to this clause, drift-pinned) → the exact
                # pre-cache posture, byte-for-byte: one direct uncached
                # DEFAULT-form sweep (honest 0-counts on a faulted/wedged
                # conn — the fold-to-zero arm never raises there), nothing
                # cached.
                return table_summary(store.connection)

        tables = await _run_lake(_tables)
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
            payload = await _run_lake(
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
            columns = await _run_lake(lambda: list_columns(store.connection, name))
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
            payload = await _run_lake(
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
        # DEC-DASHBOARD-EVENTLOOP-OFFLOAD-002: the generation probe (ledger stat +
        # sweeps-tree digest) is read INSIDE the offloaded lambda, never the event loop.
        return JSONResponse(
            await _run_lake(
                lambda: _db_datasets_cache.get_or_compute(
                    DB_DATASETS_KEY, generation=_summary_cache.generation, compute=_datasets
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
        # DEC-DASHBOARD-EVENTLOOP-OFFLOAD-002: the generation probe (ledger stat +
        # sweeps-tree digest) is read INSIDE the offloaded lambda, never the event loop.
        index: FnoCascadeIndex = await _run_lake(
            lambda: _fno_cascade_cache.get_or_compute(
                FNO_CASCADE_KEY,
                generation=_summary_cache.generation,
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
        listed_by_expiry = await _run_lake(lambda: _fno_listed_by_expiry(underlying, expiries))
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

        def _build() -> SpotCascadeIndex:
            """Build the spot symbol→interval cascade from the non-F&O lake datasets.

            DEC-DASHBOARD-PULL-CONTENTION-RESILIENCE-001 (G2): the walk PRUNES the
            F&O contract tree AT DESCENT (``exclude_segments`` — the
            DEC-COMPLETENESS-FNO-SKIP-001 mechanism db_view already implements at
            the segment-dir level), so a cold spot-cascade build never pays the
            ~10⁵-10⁶ per-contract partition dirs of a materialised options lake
            (measured 42× on the synthetic repro's WALK, 420.0 ms → 10.0 ms —
            the full unfiltered ``_build`` measured 490.9 ms; the production
            unfiltered walk's own docstring figure is 29.7 min at 2.36M
            files — OUT-OF-CI measurements; the structural walk-flatness the
            figures follow from is pinned deterministically by the committed
            op-count bench ``tests/bench/test_db_spot_cascade_fno_flat_o1.py``
            — round-5 review). ``filter_spot_datasets``
            stays as the provable-no-op belt (the ONE spot-vs-F&O split, §11
            COMMON). A FILTERED walk deliberately NEVER rebuilds the dataset
            manifest (db_view's :func:`list_candle_datasets` guard) — rebuild duty
            stays with the unfiltered datasets route + ``warm_discovery_memos`` +
            the write-side ``record_dataset`` convergence (spy-pinned).
            """
            return build_spot_cascade(
                filter_spot_datasets(
                    list_candle_datasets(
                        lake, exclude_segments=frozenset({FNO_CANDLE_LAKE_SEGMENT})
                    )
                )
            )

        # DEC-DASHBOARD-EVENTLOOP-OFFLOAD-002: the generation probe (ledger stat +
        # sweeps-tree digest) is read INSIDE the offloaded lambda, never the event loop.
        index: SpotCascadeIndex = await _run_lake(
            lambda: _spot_cascade_cache.get_or_compute(
                SPOT_CASCADE_KEY, generation=_summary_cache.generation, compute=_build
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
            payload = await _run_lake(
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
                    # The evidence-grounded stock drift rung for the moneyness
                    # spot-join (DEC-DB-MONEYNESS-STOCK-EVIDENCE-001): lazy — the
                    # composite-token memo runs ONLY when the exact-key spot probe
                    # came back empty, so a drifted stock lake classifies moneyness
                    # exactly as the Greeks ladder recovers its spot join while a
                    # healthy page pays zero extra I/O.
                    stock_drift_loader=lambda: _stock_drift_map_cached(store, lake),
                    # Manifest-first paging (DEC-LAKE-STATS-MANIFEST-001): the
                    # production route opts into the per-file stats manifest —
                    # served only after full per-request revalidation, ANY
                    # failure ⇒ the byte-identical legacy scan; kill switch
                    # BRUTEX_LAKE_STATS_MANIFEST=off forces legacy.
                    use_stats_manifest=True,
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
        DEC-DB-LATENCY-HARDENING-001 — supersedes the D3 cached-Python-parse
        cost model): the ledger is NOT small — a pre-planned full-universe
        options pull holds ~841k rows (the measured shape behind
        ``db_view.WINDOW_ROWS_CAP``), and the D3-era read still paid ONE full
        ``load_window_ledger`` Python parse per ledger STAMP move (measured
        7,177.5 ms of a 7,628 ms request at 400k rows — recurring every
        mid-pull flush). The read now rides
        :func:`~brutex.dashboard.db_view.query_window_ledger_page` — a DuckDB
        ``LIMIT``/``OFFSET`` projection over the SAME delta-folded ledger scan
        source the ``/coverage/windows`` fast paths use, with the total from
        ONE stamp-memoised ``COUNT(*)`` — so a fresh-stamp click is one
        columnar scan (O(total rows + delta footers), NOT the O(rows) Python
        record fold) and a repeat click within one stamp is an O(1) cache
        handoff + an O(page) format. A legacy-shape ledger / a persistently
        malformed delta / no DuckDB conn degrades to the byte-identical cached
        Python parse (``window_ledger_rows_cached``). The page preserves the
        canonical :meth:`~brutex.data.window_ledger.WindowLedger.to_rows` sort
        + derived columns byte-for-byte (fast-equals-legacy pinned; ONE
        row-shape source: ``window_ledger.ledger_record_row``).
        """
        from brutex.dashboard.db_view import query_window_ledger_page

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
            """Read ONE DuckDB page (legacy-parse fallback), then IST-format it."""
            from brutex.data.session import format_ist_clock

            # DEC-DB-LATENCY-HARDENING-001: DuckDB LIMIT/OFFSET page + memoised
            # COUNT over the delta-folded scan source (the /coverage/windows
            # caches — distinct `wledger-*` key families, stamp-token keyed);
            # the store connection is built + used on this threadpool worker so
            # its RLock stays on one thread (DEC-DASHBOARD-FASTREAD-001).
            page, total = query_window_ledger_page(
                store.connection,
                lake,
                limit=limit,
                offset=offset,
                count_cache=_window_counts_cache,
                page_cache=_window_page_cache,
            )
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

        return JSONResponse(await _run_lake(_load))

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
            payload = await _run_lake(
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
                    # Manifest-first paging (DEC-LAKE-STATS-MANIFEST-001) — the
                    # expired-options twin of the /db/candles opt-in above.
                    use_stats_manifest=True,
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
            """Read the expiry grid + registry counts off the event loop.

            DEC-O1-GUARANTEE-HARDEN-001 H1: the paging-invariant COUNTs ride
            the dedicated registry count cache keyed by the registry-content
            stat token; a probe outage (token ``None``) computes fresh
            UNCACHED — never a stale total, never a fabricated one.
            """
            token = registry_revision(registry_path)
            cache = None if token is None else _registry_count_cache
            gen = 0 if token is None else revision_generation(token)
            payload = list_registry_expiries(
                registry_path,
                exch=exch,
                underlying=underlying,
                limit=limit,
                offset=offset,
                count_cache=cache,
                generation=gen,
            )
            payload["counts"] = registry_counts(registry_path, count_cache=cache, generation=gen)
            return payload

        try:
            return JSONResponse(await _run_lake(_load))
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
            """Read the contract grid + registry counts off the event loop.

            DEC-O1-GUARANTEE-HARDEN-001 H1 — the expiries route's cache law,
            applied to the contracts pager's FILTERED leg count.
            """
            token = registry_revision(registry_path)
            cache = None if token is None else _registry_count_cache
            gen = 0 if token is None else revision_generation(token)
            payload = list_registry_contracts(
                registry_path,
                exch=exch,
                underlying=underlying,
                expiry_date=expiry_date,
                limit=limit,
                offset=offset,
                count_cache=cache,
                generation=gen,
            )
            payload["counts"] = registry_counts(registry_path, count_cache=cache, generation=gen)
            return payload

        try:
            return JSONResponse(await _run_lake(_load))
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

    @app.get("/screener", response_class=HTMLResponse)
    async def screener_page() -> str:
        """GET /screener — the strategy screener over the top-100 artifact.

        DEC-OPERATOR-PAGE-REAL-001 / R36: client-side filter/sort (win% · DD ·
        PF · VIX band · TF · direction) over ONE ``/api/v1/oneclick/top100``
        fetch; rows are createElement/textContent-built (never markup
        interpolation); a row click opens ``/strategy?run_id=&rank=`` — the
        ≤2-clicks law (Overview → Winners → strategy). Serves the static
        template verbatim; every number arrives client-side from the artifact
        contracts (loud "awaiting real run artifact" until a run exists).
        """
        return _render_screener_html()

    @app.get("/strategy", response_class=HTMLResponse)
    async def strategy_page() -> str:
        """GET /strategy — the chart-first single-strategy page.

        DEC-OPERATOR-PAGE-REAL-001 / R36: candle chart with entry/exit arrows
        drawn ON it (candles from the EXISTING ``/api/v1/db/candles``; trades
        from ``/api/v1/oneclick/trades`` — inline at load, never tab-gated),
        equity curve below (client integer-paisa cumsum, labeled "per-trade
        close equity — minute-path equity lands with P3"), hover-any-number
        plain-English lines, Beginner/Pro toggle, ``vix_band`` on the story
        card. ``run_id``/``rank``/``trade`` arrive as query params read
        client-side; the template is served verbatim.
        """
        return _render_strategy_html()

    # WebSocket handler — defined at module scope below to sidestep
    # the `from __future__ import annotations` closure-annotation
    # resolution problem. We register it dynamically.
    app.add_api_websocket_route("/ws", _make_ws_handler(state))

    # INTERIM Run-Sweep launch page (DEC-SWEEP-LAUNCH-BUTTON-INTERIM-001):
    # strictly additive registration — /launch + /api/v1/launch/* live in
    # brutex/dashboard/launch_button.py (single-flight detached CLI launch).
    register_launch_routes(app, lake_root_resolver=store_lake_root_resolver(instrument_store))

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
    never O(files)) plus one O(1) ``os.stat`` of the Greeks-backfill freshness
    marker (DEC-GREEKS-BULK-REENRICH-001 fix round 2 — a CLI backfill's
    overlay writes reach the caches without a restart) per call, never raising
    (every ``OSError`` maps to ``None`` = "no signal" inside ``lake_revision``).
    """
    store_path = getattr(instrument_store, "path", None)
    if not isinstance(store_path, Path):
        return None
    lake_root = store_path.parent

    def _probe() -> str | None:
        """The composite ledger-stat + sweeps-digest token for the wired lake root."""
        return lake_revision(lake_root)

    return _probe


def _job_lake_root_resolver(instrument_store: Any) -> Callable[[], Path | None]:
    """Build the background jobs' single-flight lake-root resolver — the WRITE root.

    DEC-GREEKS-BULK-REENRICH-001 fix round 2 (refuter-confirmed MAJOR): the
    jobs' cross-process ``flock`` used to key off the DEFAULT
    :func:`brutex.dashboard.pull_jobs._default_lake_root_resolver`
    (``$BRUTEX_LAKE_ROOT`` → ``~/.brutex/lake``), while their ``_work`` bodies
    scan and WRITE :func:`_resolve_pull_lake_root` — the wired
    :class:`~brutex.data.instrument_store.InstrumentStore`'s on-disk parent.
    ``brutex dashboard --lake-root /data/lake`` with ``$BRUTEX_LAKE_ROOT``
    unset (the exact launch mode :func:`_resolve_pull_lake_root`'s own error
    message prescribes) therefore flocked ``~/.brutex/lake`` while writing
    ``/data/lake`` — a CLI ``brutex greeks-backfill --lake-root /data/lake``
    (which flocks the REAL root) never contended, silently re-opening the
    stale-read → latest-wins NO_SPOT-NULL clobber the lock exists to prevent
    (the backfill deliberately has NO account-lock second belt). The lock root
    and the write root are now derived from the SAME source: the store's
    parent when a path-carrying store is wired (mirrors
    :func:`_lake_revision_probe_or_none`), else the COMMON
    :func:`~brutex.config.paths.resolve_lake_root` env default — byte-identical
    to the prior behaviour for a store-less app (where every live pull is
    refused by :func:`_resolve_pull_lake_root` anyway). O(1), pure path
    computation per call.
    """

    def _resolve() -> Path | None:
        store_path = getattr(instrument_store, "path", None)
        if isinstance(store_path, Path):
            return store_path.parent
        return resolve_lake_root()

    return _resolve


def _dhan_feed_root_resolver(
    base: Callable[[], Path | None],
) -> Callable[[], Path | None]:
    """Nest a top-root resolver *base* onto the Dhan feed subtree ``<top>/feeds/dhan/``.

    Pure wrapper (O(1), zero I/O): the value of *base* — the top write root — is
    resolved to ``feed_lake_root(<top>, feed="dhan")``; a ``None`` top root (no
    resolvable root) propagates ``None`` so the job takes no lock (mirroring
    :meth:`~brutex.dashboard.pull_jobs.BackgroundPullJob._acquire_singleflight`).
    Split from :func:`_dhan_job_lake_root_resolver` so both arms are directly tested.
    """

    def _resolve() -> Path | None:
        root = base()
        return None if root is None else feed_lake_root(root, feed="dhan")

    return _resolve


def _dhan_job_lake_root_resolver(instrument_store: Any) -> Callable[[], Path | None]:
    """Build the DHAN pull job's single-flight lake-root resolver — the ``feeds/dhan/`` root.

    DEC-DHAN-PULL-SURFACE-001. The Dhan job flocks its OWN write tree
    (``feed_lake_root(<top>, feed="dhan")`` == ``<top>/feeds/dhan/``), distinct from
    Groww's top-root flock (:func:`_job_lake_root_resolver`), so a Dhan pull and a
    Groww pull never contend on one lock (they write DISJOINT trees) while two Dhan
    pulls serialise. Thin composition of :func:`_dhan_feed_root_resolver` over the
    Groww job's own top-root resolver.
    """
    return _dhan_feed_root_resolver(_job_lake_root_resolver(instrument_store))


def _dhan_account_key_resolver() -> str | None:
    """Resolve the DHAN account-lock key — the Dhan client-id, never the Groww api-key.

    DEC-DHAN-ACCOUNT-LOCK-ISOLATE-001 (found by the ADR-0021 §5/§12 review
    2026-07-15): the two Dhan jobs' ACCOUNT-keyed single-flight must key on the
    DHAN credential, so a wet Dhan pull contends with other Dhan pulls sharing the
    one read-only Dhan account (the 5 r/s + 100,000 req/day budget, CLAUDE.md §6b)
    and NEVER takes — or is refused by — the GROWW account flock. Pre-fix both
    Dhan jobs inherited :func:`~brutex.dashboard.pull_jobs._default_account_key_resolver`
    (the Groww api-key), so a wet Dhan pull and a Groww pull falsely serialised on
    ONE account lock despite disjoint vendor accounts and disjoint lake trees.

    Failure posture mirrors the Groww default EXACTLY: a
    :class:`~brutex.config.credentials_types.CredentialsError` (AWS unreachable /
    parameters not seeded) maps to ``None`` — the account lock is then SKIPPED
    with one loud ``pull_job.account_lock_skipped`` line (``feed="dhan"``) and the
    Dhan feed-root lake flock still guards same-lake peers. Distinct credential
    STRINGS yield distinct lock files by construction (the account lock
    fingerprints the string it is given); the Dhan client-id and the Groww
    api-key are separate credentials read from separate SSM parameters. Lazy
    imports keep app import light (the #889 pattern the Groww default uses).
    """
    from brutex.config.credentials_dhan_aws import load_dhan_aws_credentials
    from brutex.config.credentials_types import CredentialsError

    try:
        return load_dhan_aws_credentials().client_id
    except CredentialsError as exc:
        _LOG.warning(
            "pull_job.account_lock_skipped",
            feed="dhan",
            reason="Dhan credentials unavailable — account-keyed single-flight not taken",
            error=str(exc),
        )
        return None


def _resume_singleflight_probe(
    lake_root_resolver: Callable[[], Path | None],
) -> Callable[[], bool]:
    """Build the intent-resume peer probe keyed on the JOBS' single-flight root.

    DEC-GREEKS-BULK-REENRICH-001 fix round 2 re-keyed the background jobs'
    cross-process ``flock`` onto the STORE-derived WRITE root
    (:func:`_job_lake_root_resolver`); the boot-time intent resume's peer
    probe must probe the IDENTICAL lock file, or a live peer holding the job
    lock at the store parent reads "acquirable" on the intent store's
    env-derived lake root — burning a restart token (and re-persisting the
    intent with ``restarts+1``) on a HEALTHY concurrent system, the exact
    false-positive :attr:`~brutex.dashboard.pull_intent.ResumeAction.PEER_ACTIVE`
    exists to prevent. A ``None`` root mirrors
    :meth:`~brutex.dashboard.pull_jobs.BackgroundPullJob._acquire_singleflight`
    exactly: with no root the jobs take NO lock, so there is nothing to
    probe and the resume may fire (``True``).
    """

    def _probe() -> bool:
        root = lake_root_resolver()
        if root is None:
            return True
        return probe_pull_singleflight(root)

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


_SCREENER_TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "screener.html"
_SCREENER_TEMPLATE_CACHE: str | None = None


def _load_screener_template() -> str:
    """Read ``templates/screener.html`` once and serve the cached text verbatim.

    Same read-once module-cache pattern as every other page template
    (DEC-OPERATOR-PAGE-REAL-001); the page is fully static — all data arrives
    client-side from the ``/api/v1/oneclick/*`` artifact contracts.
    """
    global _SCREENER_TEMPLATE_CACHE  # noqa: PLW0603 — single-load module cache
    if _SCREENER_TEMPLATE_CACHE is None:
        _SCREENER_TEMPLATE_CACHE = _SCREENER_TEMPLATE_PATH.read_text(encoding="utf-8")
    return _SCREENER_TEMPLATE_CACHE


def _render_screener_html() -> str:
    """Render the ``/screener`` strategy-screener page (verbatim template)."""
    return _load_screener_template()


_STRATEGY_TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "strategy.html"
_STRATEGY_TEMPLATE_CACHE: str | None = None


def _load_strategy_template() -> str:
    """Read ``templates/strategy.html`` once and serve the cached text verbatim.

    Same read-once module-cache pattern as every other page template
    (DEC-OPERATOR-PAGE-REAL-001); query params (``run_id``/``rank``/``trade``)
    are read client-side — no server-side substitution.
    """
    global _STRATEGY_TEMPLATE_CACHE  # noqa: PLW0603 — single-load module cache
    if _STRATEGY_TEMPLATE_CACHE is None:
        _STRATEGY_TEMPLATE_CACHE = _STRATEGY_TEMPLATE_PATH.read_text(encoding="utf-8")
    return _STRATEGY_TEMPLATE_CACHE


def _render_strategy_html() -> str:
    """Render the ``/strategy`` chart-first strategy page (verbatim template)."""
    return _load_strategy_template()


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


def _preopen_rows_key(basis: str, start: date, end: date, wanted: Sequence[str] | None) -> str:
    """The pre-open export rows-memo key — pure, deterministic, collision-safe.

    DEC-DB-LATENCY-HARDENING-001 (MIGRATION.md #1719): one memo coordinate per
    ``(basis, inclusive [from, to] range, symbols filter)`` — the exact inputs
    that shape the built ``PreOpenRow`` list (the ``.zip`` route's mode/min_pct
    shape only the RENDER, so both export routes share one coordinate). The
    filter segment rides one tuple ``repr`` (the window-page precedent —
    unambiguous, a client-supplied comma-joined symbols string can never forge
    a key collision through a bare separator); ``None`` (no filter) keys as
    ``None``, distinct from any explicit tuple.
    """
    filter_token = None if wanted is None else tuple(wanted)
    return f"preopen-rows|{basis}|{start.isoformat()}|{end.isoformat()}|{filter_token!r}"


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
