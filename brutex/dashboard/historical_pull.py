"""Selection-based HISTORICAL pull — the separate "Pull historical" engine.

DEC-DERIV-PULL-HISTORICAL-001 (PR-5, the capstone of the metadata/historical split,
ADR-0010). The HEAVY half of the metadata/historical split: the metadata-only "Pull
expiries + contracts" button (PR-3, DEC-DERIV-METADATA-ROUTE-001) caches every expiry
DATE + contract SYMBOL into the separate ``registry.duckdb`` (PR-1, #732) WITHOUT a
single candle. This module is the twin button — download the OHLC candles for the
operator-SELECTED registry contracts (a single contract / one expiry's chain / a whole
underlying / all cached), now that the chain metadata is already cached.

## Why a selection-based pull (not a re-enumeration)

The retired fused bulk chain pull (decommissioned by
DEC-DERIV-DECOMMISSION-FUSED-001) RE-ENUMERATED the whole chain (``get_expiries`` ×
``get_contracts``) AND downloaded every contract's candles on every click — the
runaway "pull everything" shape that wasted the network when the operator wanted
ONLY a handful of already-cached contracts. This module takes a concrete
:class:`~brutex.data.registry_store.ContractRow` selection, rebuilds each into a
:class:`~brutex.adapters.protocol.DerivativeContract` via
:func:`~brutex.adapters.groww.contracts.parse_contract_symbol`, and feeds that RAW
contracts list straight to
:func:`~brutex.adapters.groww.option_ingest.run_groww_option_ingest` — so it pulls the
SPECIFIC contracts WITHOUT re-enumerating the chain (the whole point; the metadata is
already in the registry).

## Path A — the verified candle lake (NOT the OptionBar lake)

The candles land on **PATH A**: the standard verified candle lake
(``bars/<exch>/FNO/<groww_symbol>/<interval>/`` via
:class:`~brutex.data.sink.ParquetBarSink`), the SAME lake the bulk derivatives pull
writes — the spine
:func:`~brutex.adapters.groww.option_ingest.run_groww_option_ingest` already uses. It
is **NOT** the ``options/`` OptionBar lake (PATH B): an OptionBar needs ``iv`` / ``spot``
/ ``ATM±N`` enrichment that ``get_historical_candles`` does NOT return — fabricating
those would violate Golden Rule #1. Historical OHLCV+OI is exactly what the candle lake
stores, so Path A is the honest home.

## candle_status — the per-contract historical marker

AFTER a contract's candles land clean (every ``(contract, interval)`` pair succeeded —
no quarantine, no transient residual) the contract's registry
:meth:`~brutex.data.registry_store.RegistryStore.set_candle_status` is set to
``complete`` — the per-contract marker the ``/db`` chain view reads (the metadata pull
leaves it ``none``). A contract that quarantined or left a transient window is left
``none`` (honest — never marked complete on a partial pull).

## Honest envelope (Golden Rule #1 + #10)

* **Honest empty selection** — an EMPTY contract list pulls 0 bars + an honest note
  (never an invented contract). The route resolves the selection to registry rows; an
  empty registry / no-match is the route's honest 400 / "pull metadata first" payload.
* **Path A, not Path B** — candle-only lake; no fabricated ``iv``/``spot``/``ATM``.
* **until-clean** (DEC-GROWW-DERIV-UNTIL-CLEAN-001 #730) — when ``until_clean=True`` the
  pull runs BOUNDED re-pull rounds over the SHARED WindowLedger until the transient
  residual drains, the token dies, or the budget / a no-progress round halts it
  (:func:`~brutex.adapters.ingest_common.run_until_clean`, ``stop_when_stuck=True``).
  Each round re-ingests the SAME fixed contract list; the ledger SKIPS already-`SUCCESS`
  windows and RE-ATTEMPTS the prior round's `FAILED` (transient) ones.
* **Network-bound, NOT O(1)-total** — the per-contract fetch tiles each contract's
  range (the heavy minute fill for a full underlying's chain is genuinely weeks-scale).
  The WindowLedger makes every window rerun-safe (idempotent). Unlike the chain pull, a
  round here is ``O(failed windows)`` (NO re-enumeration — the contract list is fixed),
  the same shape as the spot round.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import structlog

from brutex.adapters.empty_circuit_breaker import MassEmptySettleHalt
from brutex.adapters.error_storm_breaker import resolve_error_storm_breaker
from brutex.adapters.groww.client import GrowwClient, GrowwClientProtocol
from brutex.adapters.groww.constants import (
    GROWW_FUTURE_COVERAGE_MARKET,
    GROWW_OPTION_COVERAGE_MARKET,
    OPTION_SEGMENT,
    TRACK1_SPOT_INTERVAL,
)
from brutex.adapters.groww.contract_bounds import default_fno_minute_window, fno_window_opens_on
from brutex.adapters.groww.contracts import parse_contract_symbol
from brutex.adapters.groww.errors import GrowwAuthError, is_terminal_auth_failure
from brutex.adapters.groww.expiry_scope import (
    log_expiry_scope_summary,
    log_out_of_scope_expiries,
    partition_contracts_by_scope,
)
from brutex.adapters.groww.fetch_pipeline import FetchPipeline
from brutex.adapters.groww.ingest import IngestReport
from brutex.adapters.groww.option_ingest import (
    ContractWindows,
    GrowwOptionIngestReport,
    ProgressHook,
    contract_exchange,
    emit_underlying_all_empty,
    make_breaker_canary_probe,
    reconcile_fno_ledger_windows,
    run_groww_option_ingest,
)
from brutex.adapters.groww.strike_chain_bound import build_empty_retry_budget
from brutex.adapters.ingest_common import (
    AUTO_RESUME_MAX_ROUNDS,
    DEFAULT_EMPTY_RETRY_ATTEMPTS,
    PullStep,
    PullVerdict,
    StepOrderMonitor,
    UntilCleanRound,
    classify_pull,
    mark_synthetic,
    run_until_clean,
)
from brutex.adapters.protocol import DerivativeContract
from brutex.calendars.observed_sessions import load_observed_holidays
from brutex.dashboard.coverage_reconcile import load_lake_spans
from brutex.dashboard.pull_census import emit_written_settled_census
from brutex.data.errors import StoreWriteDeadlineExceeded
from brutex.data.parquet_writer import fold_max_close_delta
from brutex.data.raw_integrity_store import make_capture_hook_factory
from brutex.data.registry_store import (
    CANDLE_STATUS_COMPLETE,
    ContractRow,
    RegistryStore,
)
from brutex.data.sink import BarSink, NullBarSink
from brutex.data.window_ledger import EMPTY, SKIPPED, WindowLedger
from brutex.data.window_ledger_store import (
    begin_pull_ledger,
    end_pull_ledger,
    index_lake_spans,
    make_live_ledger_flush,
)
from brutex.data.write_consumer import WriteConsumer
from brutex.notifications.sweep_alerts import emit_pull_halt_alert
from brutex.orchestrator.audit import open_ingest_audit
from brutex.orchestrator.parallel import PoolRunParallel
from brutex.orchestrator.universe_phases import RunParallel

if TYPE_CHECKING:
    # Type-only (DEC-PULL-SIX-STEP-ORDER-001): the seam only reads attributes
    # (annotations, never construction), and the dataclass lives in the LEAF
    # pipeline_verdict module — never import it from pipeline_pull here (that
    # re-export edge is a module-level import cycle: pipeline_pull imports
    # composite_pull → this module at runtime).
    from brutex.dashboard.pipeline_verdict import PriorStepsVerdict
    from brutex.dashboard.pull_controller import OnLedgerSync, SessionRateController
    from brutex.orchestrator.audit import AuditWriter

#: The ONLY granularity fetched from Groww for an F&O contract
#: (DEC-1MINUTE-ONLY-DERIVE-1DAY-001) — the heavy ``1minute`` fill. ``1day`` is DERIVED
#: from this (never vendor-fetched), so the prior two-phase "1-day-first to pin the
#: listing date" barrier (DEC-HISTORICAL-1DAY-FIRST-001) is RETIRED: there is one vendor
#: phase, bounded per contract to its ``expiry − 30 days → expiry`` default window.
_MINUTE_INTERVAL: Final[str] = TRACK1_SPOT_INTERVAL

#: Structured logger — the start-date discovery degrade paths (catalog unavailable /
#: per-contract skip) are logged LOUDLY (Golden Rule #10 observability), never silent.
_LOG = structlog.get_logger("brutex.dashboard.historical_pull")

#: FNO history floor — Groww FNO data is "available from 2020" (CLAUDE.md §6); the
#: historical pull mirrors the derivatives pull's floor so the candle range agrees.
GROWW_HISTORICAL_FLOOR: Final[date] = date(2020, 1, 1)

#: One calendar day — the inclusive→exclusive end shift + the previous-day default.
_ONE_DAY: Final[timedelta] = timedelta(days=1)

#: The ``FUT`` leg side → the futures ledger namespace; everything else (``CE`` / ``PE``)
#: is an option leg. Keeping option/future windows in DISTINCT ledger namespaces is the
#: SAME discipline the bulk pull uses (DEC-GROWW-FUTURE-INGEST-001) so the two never
#: collide on the same expiry.
_FUTURE_SIDE: Final[str] = "FUT"

#: WARN — the completion-stamp batch (``mark_candle_statuses``) blew the store-write
#: wall-clock deadline (DEC-STORE-WRITE-DEADLINE-001): the stamps are DEFERRED — the
#: unmarked contracts keep ``candle_status="none"`` and are re-marked idempotently by
#: the next pull (the window ledger, not this stamp, drives window re-pull — no candle
#: data is ever lost). Fields: ``contracts`` / ``error_type`` / ``defer_seconds``.
HISTORICAL_MARK_COMPLETE_DEFERRED_EVENT: Final[str] = "historical.mark_complete.write_deferred"

#: One F&O ordering bucket: ``(ledger-market, is_index)``. The MARKET half names the
#: ledger namespace (``_contract_market`` — KEPT verbatim, so every window row / skip
#: decision / ``/coverage/windows`` bucket is byte-identical); the BOOL half is the
#: ORDERING-only index/stock class (DEC-PULL-SIX-STEP-ORDER-001).
_BucketKey = tuple[str, bool]

#: The F&O steps 3-6 binding (DEC-PULL-SIX-STEP-ORDER-001 — REPLACES the 2-key
#: ``_MARKET_PULL_ORDER`` futures-first sort of DEC-FNO-LOOKBACK-SPLIT-001): binds the
#: ledger-market constants (owned here) to the COMMON :class:`PullStep` ranks (owned in
#: ``ingest_common``) — the law lives in ONE place, the binding at the ONE chokepoint.
#: INDICES-FUTURES → STOCKS-FUTURES → INDICES-OPTIONS → STOCKS-OPTIONS.
_STEP_FOR_BUCKET: Final[dict[_BucketKey, PullStep]] = {
    (GROWW_FUTURE_COVERAGE_MARKET, True): PullStep.INDICES_FUTURES,
    (GROWW_FUTURE_COVERAGE_MARKET, False): PullStep.STOCKS_FUTURES,
    (GROWW_OPTION_COVERAGE_MARKET, True): PullStep.INDICES_OPTIONS,
    (GROWW_OPTION_COVERAGE_MARKET, False): PullStep.STOCKS_OPTIONS,
}


def _bucket_sort_key(bucket: _BucketKey) -> tuple[int, str, int]:
    """Total, deterministic sort key over ordering buckets — the decreed 3→4→5→6.

    A known bucket sorts by its :class:`PullStep` rank; an UNKNOWN market sorts
    LAST (rank 99) with ``(market, index-first)`` string/flag tie-breaks so the
    order stays total + deterministic for ANY input (the property-test invariant).
    Pure, O(1).
    """
    market, is_index = bucket
    step = _STEP_FOR_BUCKET.get(bucket)
    return (int(step) if step is not None else 99, market, 0 if is_index else 1)


def _contract_bucket(
    row: ContractRow, underlying_kinds: Mapping[str, bool] | None
) -> tuple[_BucketKey, bool]:
    """Classify one contract row into its ordering bucket — pure, total, never raises.

    Returns ``(bucket_key, resolved)``. The market half is ALWAYS
    :func:`_contract_market` (the ledger namespace, byte-unchanged); the class
    half joins *underlying_kinds* — keyed by the SAME
    ``primary_fno_underlying``-resolved registry key the metadata phase wrote
    :class:`~brutex.data.registry_store.ContractRow.underlying` under, so
    NIFTYJR→NIFTYNXT50 / BAJAJ-AUTO join by construction. Fail-closed for ORDER,
    never for DATA: an underlying absent from the map classifies STOCK-side (the
    LATER bucket — an unknown can never jump ahead of a mandated index step) with
    ``resolved=False`` so the caller counts + WARNs it; the row is still pulled.
    ``underlying_kinds=None`` (a direct/legacy caller) classifies everything
    stock-side with ``resolved=True`` (no per-row noise) — the order degrades to
    exactly the legacy 2-bucket FUT-then-OPT law, byte-identical.
    """
    market = _contract_market(row)
    if underlying_kinds is None:
        return (market, False), True
    kind = underlying_kinds.get(row.underlying)
    if kind is None:
        return (market, False), False
    return (market, kind), True


#: Factory: ``(lake_root) -> (client, sink)`` — injected in tests; the default picks the
#: honest dry-run pair or the live ``(GrowwClient, ParquetBarSink)``. Re-uses the SAME
#: shape the bulk derivatives pull declares (CLAUDE.md §11 COMMON).
HistoricalClientFactory = Callable[[Path], tuple[GrowwClientProtocol, BarSink]]

#: A rebuilt selection entry: the authoritative registry row + its parsed contract.
_RebuiltContract = tuple[ContractRow, DerivativeContract]


@dataclass(frozen=True, slots=True)
class HistoricalPullReport:
    """The honest outcome of one selection-based historical candle pull (display + audit).

    Single-phase shape (DEC-1MINUTE-ONLY-DERIVE-1DAY-001): 1-minute is the ONLY
    granularity fetched from Groww; ``1day`` is DERIVED from it after the pull (never
    vendor-fetched), so the prior two-phase 1-day-first barrier is RETIRED. The
    (retained, repurposed) phase fields now describe the SINGLE minute phase:

    * :attr:`phase1_clean` — did the ENTIRE 1-minute phase settle clean (every selected
      contract, every window — the completion barrier)?
    * :attr:`phase1_complete` — the contract symbols whose 1-minute pull landed clean.
    * :attr:`start_dates` — each contract's RESOLVED 1-minute window start (the explicit
      operator start, or the per-contract ``expiry − lookback`` default, clamped into
      range). A NOT-YET-OPEN contract (window not reached — skipped, zero fetches) is
      absent here and counted in :attr:`contracts_not_yet_in_window`
      (DEC-FNO-FAR-EXPIRY-WINDOW-001).
    * :attr:`minute_requested` — always ``True`` (1-minute is the only granularity).
    * :attr:`minute_skipped` — always ``False`` (one phase; there is no barrier to skip).

    The aggregate :attr:`bars_appended` / :attr:`windows` / :attr:`failed_pairs` /
    :attr:`still_failed_windows` / :attr:`auth_aborted` describe the 1-minute phase (the
    top-line counts of the whole pull); :attr:`contracts_completed` is the count marked
    ``candle_status=complete`` (a contract is complete iff its 1-minute phase cleaned).
    The 1-day derivation runs AFTER, quarantined — it never affects these counts.
    """

    dry_run: bool
    intervals: tuple[str, ...]
    contracts_selected: int
    contracts_completed: int
    bars_appended: int
    windows: int
    failed_pairs: int
    still_failed_windows: int
    auth_aborted: bool
    note: str
    phase1_clean: bool
    phase1_complete: tuple[str, ...]
    start_dates: dict[str, str]
    minute_requested: bool
    minute_skipped: bool
    #: Contracts EXCLUDED from this pull because their strict default window has not
    #: OPENED yet (``expiry − lookback`` on/after the envelope end — zero vendor calls,
    #: zero ledger rows; DEC-FNO-FAR-EXPIRY-WINDOW-001). Always 0 under explicit dates.
    contracts_not_yet_in_window: int = 0
    #: Settled windows skipped WITHOUT a vendor hit (DEC-WINDOW-LEDGER-RESCUE-001) —
    #: the 1-minute phase's FIRST pass's ledger skips (later until-clean rounds
    #: re-skip the same settled windows by design). Rendered in the note as
    #: "skipped N already-pulled window(s) (no vendor hit)".
    windows_skipped: int = 0
    #: True when the persisted window ledger EXISTED but failed to reload at pull
    #: start (DEC-WINDOW-LEDGER-RESCUE-001) — the pull ran a full re-verify (no
    #: skip); the note announces it loudly, never silent.
    ledger_load_failed: bool = False
    mass_empty_cause: str | None = None
    """The mass-empty circuit-breaker halt message (DEC-EMPTY-WINDOW-CIRCUIT-
    BREAKER-001 minor M-5) when the F&O phase HALTED on a suspected soft-throttle
    [R2] / mis-addressing [R3] clean-empty burst; ``None`` when it never tripped."""
    settled_skipped: int = 0
    """Windows THIS pull newly settled as terminal ``SKIPPED`` (the per-pull ledger
    tally) — the honest-verdict count (never "no action" over a settling pull)."""
    expiries_out_of_scope: int = 0
    """Distinct ``(underlying, expiry)`` groups EXCLUDED by the operator's expiry
    scope (DEC-FNO-EXPIRY-SCOPE-001): an out-of-scope expiry is fetched not at
    all — zero vendor calls, zero ledger rows, one ``fno.expiry_out_of_scope``
    log per group; already-stored bars are never touched. Always 0 under the
    default unbounded scope."""
    contracts_out_of_scope: int = 0
    """Contract legs excluded by the expiry scope (the per-leg sum over
    :attr:`expiries_out_of_scope` groups). These never enter
    :attr:`contracts_selected` — the pull workload counts in-scope legs only."""
    conflicts: int = 0
    """Keep-first vendor re-statements recorded across the 1-minute phase
    (DEC-VENDOR-CONFLICT-SURFACE-001 — previously visible ONLY in the
    ``option.ingest.conflict.summary`` log lines). The stored bar always won."""
    vendor_dup_rows_dropped: int = 0
    """WITHIN-BATCH duplicate vendor rows the writer collapsed across the
    1-minute phase (DEC-VENDOR-CONFLICT-SURFACE-001 — the sum the
    ``lake.vendor_duplicate_rows`` WARNINGs previously swallowed)."""
    max_close_delta: Decimal | None = None
    """MAX absolute keep-first close delta across the phase's conflicts — exact
    Decimal rupees (never float, CLAUDE.md §6 price-unit row); ``None`` when no
    conflict was recorded."""
    greeks_enrich_ok: bool | None = None
    """Whole-pass verdict of the post-pull Greeks enrichment
    (DEC-GREEKS-ENRICH-OBSERVE-001): ``True`` = the pass COMPLETED (per-target
    quarantines ride :attr:`greeks_targets_errored`, they never flip this —
    one corrupt contract must not read as a whole-pass failure); ``False`` =
    the WHOLE pass failed (see :attr:`greeks_enrich_error`; the overlay is
    re-derivable — the next pull re-attempts it); ``None`` = the pass did not
    run (dry-run — nothing was written to enrich)."""
    greeks_enrich_error: str | None = None
    """``"ErrorType: message"`` when :attr:`greeks_enrich_ok` is ``False``
    (mirrors the ONE ``fno.greeks_enrich.error`` log line); ``None`` otherwise."""
    greeks_targets_errored: int = 0
    """The Greeks driver's per-target quarantine tally (a per-contract
    read/solve/write error is quarantined INSIDE the driver — surfaced here,
    never conflated with a whole-pass failure)."""
    class_unresolved: int = 0
    """Contract legs whose index/stock ORDERING class could not be resolved from
    the caller's ``underlying_kinds`` map (DEC-PULL-SIX-STEP-ORDER-001) — each
    classified STOCK-side (fail-closed for ORDER, never for DATA: the leg is
    still pulled, one ``fno.step_unclassified`` WARN per underlying) and named
    in the note. 0 for a legacy ``underlying_kinds=None`` caller (the 2-bucket
    order — nothing to resolve) and for a fully-resolved map."""

    @property
    def verdict(self) -> PullVerdict:
        """The polished, prioritised :class:`PullVerdict` for this F&O pull.

        DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001 minor M-5 — the F&O bulk route's twin
        of ``BulkSpotReport.verdict`` (CLAUDE.md §11 COMMON: the SAME
        :func:`~brutex.adapters.ingest_common.classify_pull` vocabulary, so a
        mass-empty halt reads identically loud on both routes). ``no_data`` is 0:
        the F&O report does not carry a per-symbol no-data rollup (an all-empty
        contract settles via the ledger and rides ``settled_skipped``). A dry-run
        verdict is marked SYNTHETIC like the spot route's.
        """
        verdict = classify_pull(
            auth_aborted=self.auth_aborted,
            still_failed_windows=self.still_failed_windows,
            failed_pairs=self.failed_pairs,
            no_data=0,
            completed=self.contracts_completed,
            mass_empty_cause=self.mass_empty_cause,
            settled_skipped=self.settled_skipped,
        )
        if self.dry_run:
            return mark_synthetic(verdict)
        return verdict

    def to_dict(self) -> dict[str, object]:
        """JSON-safe view for the ``pull-historical`` route + the live panel."""
        return {
            "dry_run": self.dry_run,
            "intervals": list(self.intervals),
            "contracts_selected": self.contracts_selected,
            "contracts_completed": self.contracts_completed,
            "contracts_not_yet_in_window": self.contracts_not_yet_in_window,
            "bars_appended": self.bars_appended,
            "windows": self.windows,
            "windows_skipped": self.windows_skipped,
            "failed_pairs": self.failed_pairs,
            "still_failed_windows": self.still_failed_windows,
            "auth_aborted": self.auth_aborted,
            "ledger_load_failed": self.ledger_load_failed,
            "phase1_clean": self.phase1_clean,
            "phase1_complete": list(self.phase1_complete),
            "start_dates": dict(self.start_dates),
            "minute_requested": self.minute_requested,
            "minute_skipped": self.minute_skipped,
            "note": self.note,
            "mass_empty_cause": self.mass_empty_cause,
            "settled_skipped": self.settled_skipped,
            "expiries_out_of_scope": self.expiries_out_of_scope,
            "contracts_out_of_scope": self.contracts_out_of_scope,
            "conflicts": self.conflicts,
            "vendor_dup_rows_dropped": self.vendor_dup_rows_dropped,
            # JSON-safe: the exact Decimal renders as its string (never a float
            # round-trip — CLAUDE.md §4 money rule); None stays null.
            "max_close_delta": None if self.max_close_delta is None else str(self.max_close_delta),
            "greeks_enrich_ok": self.greeks_enrich_ok,
            "greeks_enrich_error": self.greeks_enrich_error,
            "greeks_targets_errored": self.greeks_targets_errored,
            "class_unresolved": self.class_unresolved,
            "verdict": self.verdict.to_dict(),
        }


class _EmptyCandleSDK:
    """An honest dry-run SDK: no creds ⇒ no real candles, so the candle fetch is EMPTY.

    Refuses to fabricate OHLC for a REAL listed contract (Golden Rule #1 — unlike a
    synthetic spot preview, an expired option's candles are real vendor data). Carries
    the three methods the ingest spine may touch so the path runs offline; the contract
    list is the operator's real registry selection, so the dry-run still reports the
    honest selected count (it just appends 0 bars).
    """

    def get_expiries(self, **_kwargs: object) -> dict[str, object]:
        """No expiries without live creds (never reached — the contracts are pre-resolved)."""
        return {"expiries": []}

    def get_contracts(self, **_kwargs: object) -> dict[str, object]:
        """No contracts without live creds (never reached — the contracts are pre-resolved)."""
        return {"contracts": []}

    def get_historical_candles(self, **_kwargs: object) -> dict[str, object]:
        """No candles without live creds (the honest dry-run — 0 bars, never fabricated)."""
        return {"candles": []}


def _build_dry_run_client(_lake_root: Path) -> tuple[GrowwClientProtocol, BarSink]:
    """Honest dry-run pair: an empty-candle client + a :class:`NullBarSink`.

    A SINGLE client (not a pool): a dry-run does no network, so cross-contract
    concurrency buys nothing — and ``run_groww_option_ingest`` keeps its serial
    default for the dry-run (no ``run_parallel`` wired), so the path stays
    deterministic offline.
    """
    return GrowwClient(sdk=_EmptyCandleSDK()), NullBarSink()


def _build_live_client(  # pragma: no cover - live Groww TOTP token + SDK + network
    lake_root: Path,
    *,
    pace: Callable[[], None] | None = None,
    on_rate_limit: Callable[[], None] | None = None,
    on_success: Callable[[], None] | None = None,
) -> tuple[GrowwClientProtocol, BarSink]:
    """Build the LIVE ``(ClientPool, ParquetBarSink)`` from stored TOTP credentials.

    LIVE ONLY (network + TOTP); tests inject a fake ``client_factory``. Builds a
    multi-connection :class:`~brutex.adapters.groww.pool.ClientPool`
    (DEC-GROWW-CLIENT-POOL-001 / DEC-GROWW-OPTION-POOL-001) — K independent SDK
    connections so the cross-contract :class:`~brutex.orchestrator.parallel.PoolRunParallel`
    fan-out stops serialising on ONE socket (the measured ~0.49-in-flight starve), giving
    the option candle pull 8-RPS parity with the spot pull. The SHARED session
    RateController still caps the AGGREGATE rate (the rate hooks at the one choke point so
    EVERY candle call is paced + retried + decrement/recover, DEC-GROWW-RATELIMIT-RETRY-001);
    pool size is env-tunable (``BRUTEX_GROWW_POOL_SIZE``) with a safe default + hard clamp.
    """
    from brutex.adapters.groww.auth import GrowwCredentials
    from brutex.adapters.groww.constants import resolve_pool_size
    from brutex.adapters.groww.live import build_live_pool
    from brutex.config.credentials import resolve_groww_aws_credentials
    from brutex.data.parquet_writer import ParquetWriter
    from brutex.data.sink import ParquetBarSink

    aws_creds = resolve_groww_aws_credentials()
    creds = GrowwCredentials(api_key=aws_creds.api_key, totp_secret=aws_creds.totp_secret)
    pool_size = resolve_pool_size()
    client = build_live_pool(
        creds, size=pool_size, pace=pace, on_rate_limit=on_rate_limit, on_success=on_success
    )
    _LOG.info("historical.client_pool", connections=pool_size)
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=lake_root))
    return client, sink


def _resolve_client_factory(
    *,
    dry_run: bool,
    pace: Callable[[], None] | None,
    on_rate_limit: Callable[[], None] | None,
    on_success: Callable[[], None] | None,
) -> HistoricalClientFactory:
    """Pick the default ``(lake_root) -> (client, sink)`` factory, wiring the live limiter.

    Dry-run → the honest empty-candle pair. Live → a factory that injects the shared rate
    controller's pace / decrement-on-429 / recover-on-success hooks into the
    :class:`GrowwClient`, so the candle pull can NEVER bypass the limiter
    (DEC-GROWW-RATELIMIT-RETRY-001).
    """
    if dry_run:
        return _build_dry_run_client

    def _live(  # pragma: no cover - live network
        lake_root: Path,
    ) -> tuple[GrowwClientProtocol, BarSink]:
        return _build_live_client(
            lake_root, pace=pace, on_rate_limit=on_rate_limit, on_success=on_success
        )

    return _live


def _rate_hooks(
    session_rate: SessionRateController | None, *, dry_run: bool
) -> tuple[
    Callable[[], None] | None,
    Callable[[], None] | None,
    Callable[[], None] | None,
    Callable[[], None] | None,
]:
    """Resolve ``(pace, on_rate_limit, on_timeout, on_success)`` from the session rate.

    Dry-run or no session controller → all ``None`` (no pacing offline). Live with a
    :class:`~brutex.dashboard.pull_controller.SessionRateController` → its single
    persistent controller's hooks, so the 500/min governor spans the whole pull (the
    candle calls ride the SAME bucket as the spot/derivatives pulls).
    """
    if dry_run or session_rate is None:
        return None, None, None, None
    controller = session_rate.get()
    return (
        controller.pace,
        controller.on_rate_limit,
        controller.on_timeout,
        controller.on_success,
    )


def _option_run_parallel(
    *, dry_run: bool, session_rate: SessionRateController | None
) -> RunParallel | None:
    """Give the option candle pull its rate-sized cross-contract pool, or the serial default.

    DEC-GROWW-OPTION-POOL-001, the option twin of
    :func:`~brutex.dashboard.pull_controller._spot_run_parallel`: a LIVE pull (a session
    controller present, not a dry-run) fans contracts across a bounded
    :class:`~brutex.orchestrator.parallel.PoolRunParallel` over the multi-connection
    :class:`~brutex.adapters.groww.pool.ClientPool` the live factory builds, so the pull
    reaches UP TO the rate ceiling. A dry-run / no-controller pull returns ``None`` so
    :func:`~brutex.adapters.groww.option_ingest.run_groww_option_ingest` keeps its
    byte-identical serial default (deterministic offline). The SHARED token bucket is the
    sole issue governor either way (CLAUDE.md §2 Rule 6 — no ceiling is raised). Pure + O(1).
    """
    if dry_run or session_rate is None:
        return None
    return PoolRunParallel()


def _contract_market(row: ContractRow) -> str:
    """The ledger market namespace for a contract row's side (option vs future).

    A ``FUT`` leg rides :data:`GROWW_FUTURE_COVERAGE_MARKET`; a ``CE`` / ``PE`` leg rides
    :data:`GROWW_OPTION_COVERAGE_MARKET` — the SAME split the bulk pull uses so a
    contract's historical windows never collide across leg type (DEC-GROWW-FUTURE-INGEST-001).
    """
    return (
        GROWW_FUTURE_COVERAGE_MARKET if row.side == _FUTURE_SIDE else GROWW_OPTION_COVERAGE_MARKET
    )


def _resolve_range(
    *, from_date: date | None, to_date: date | None, now: datetime
) -> tuple[date, date]:
    """Resolve the half-open ``[start, end)`` candle window (clamped to ``[floor, today]``).

    ``from_date`` defaults to :data:`GROWW_HISTORICAL_FLOOR` (2020-01-01); ``to_date``
    defaults to the **previous day** (a routine pull never grabs the in-progress
    session). Both bounds are day-INCLUSIVE, so ``end`` is the midnight that STARTS the
    day after ``to_date`` (the ingest treats ``end`` as exclusive). An empty range raises
    ``ValueError``.
    """
    today = now.astimezone(UTC).date()
    floor = GROWW_HISTORICAL_FLOOR
    start = max(from_date or floor, floor)
    last = min(to_date or (today - _ONE_DAY), today)
    if last < start:
        raise ValueError(
            f"nothing to pull: resolved range [{start.isoformat()}, {last.isoformat()}] is empty"
        )
    return start, last + _ONE_DAY


def _rebuild_contracts(contracts: Sequence[ContractRow]) -> list[_RebuiltContract]:
    """Rebuild each :class:`ContractRow` into a :class:`DerivativeContract` (parse-or-raise).

    Uses :func:`~brutex.adapters.groww.contracts.parse_contract_symbol` with the row's
    AUTHORITATIVE ``underlying`` + ``expiry_date`` (never re-parsed from the lossy
    ``DDMmmYY`` token), so the rebuilt contract is bit-faithful to what the metadata pull
    persisted. A malformed registry symbol fails loud (Golden Rule #1 — never a silent
    skip). Returns ``(row, contract)`` pairs so the candle_status marker keys on the
    row's PK after its candles land.
    """
    return [
        (
            row,
            parse_contract_symbol(
                row.contract_symbol, underlying_symbol=row.underlying, expiry=row.expiry_date
            ),
        )
        for row in contracts
    ]


def _clean_symbols(report: GrowwOptionIngestReport) -> frozenset[str]:
    """The contract symbols this pass landed CLEAN (every pair SUCCESS, no still-failed).

    A symbol is clean iff it has at least one recorded pair AND no quarantined failure AND
    none of its pairs left a transient ``still_failed_windows`` residual — only then is its
    historical coverage truly complete and the ``candle_status=complete`` marker honest.
    """
    quarantined = {f.symbol for f in report.failures}
    still_failing = {
        symbol for (symbol, _interval), pair in report.pairs.items() if pair.still_failed_windows
    }
    landed = {symbol for (symbol, _interval) in report.pairs}
    return frozenset(landed - quarantined - still_failing)


@dataclass(frozen=True, slots=True)
class _PassResult:
    """One BUCKET ingest pass's aggregate counts + the contracts that landed CLEAN.

    Since DEC-PULL-SIX-STEP-ORDER-001 a pass covers ONE ``(ledger-market, is_index)``
    ordering bucket (one step's round); the phase fold sums buckets. ``bars`` /
    ``windows`` SUM across rounds (round N re-appends only newly-fetched
    windows — the keep-first sink + WindowLedger skip make double-counting impossible);
    ``failed_pairs`` / ``still_failed_windows`` / ``auth_aborted`` describe THIS pass's
    residual (the loop reports the LAST round's). ``clean_symbols`` is the set of contract
    symbols whose every ``(contract, interval)`` pair this pass succeeded with NO transient
    residual + NO quarantine — the contracts safe to mark ``complete``.
    """

    bars: int
    windows: int
    failed_pairs: int
    still_failed_windows: int
    auth_aborted: bool
    clean_symbols: frozenset[str]
    #: Settled windows this pass skipped WITHOUT a vendor hit (the WindowLedger skip
    #: authority engaging — DEC-WINDOW-LEDGER-RESCUE-001 visibility).
    skipped_windows: int = 0
    #: Keep-first vendor re-statements this pass recorded (sum across buckets —
    #: DEC-VENDOR-CONFLICT-SURFACE-001).
    conflicts: int = 0
    #: Within-batch duplicate vendor rows the writer collapsed this pass (sum).
    vendor_dup_rows_dropped: int = 0
    #: MAX absolute keep-first close delta this pass observed (exact Decimal;
    #: ``None`` = no conflict).
    max_close_delta: Decimal | None = None


@dataclass(frozen=True, slots=True)
class _PhaseResult:
    """One PHASE's folded outcome — its until-clean passes summed + the clean-landed set.

    A phase is ONE interval's full pull (Phase 1 = ``1day``, Phase 2 = ``1minute``) over
    the SAME fixed market buckets, run as the single-pass or until-clean loop. ``bars`` /
    ``windows`` SUM across the phase's rounds; ``failed_pairs`` / ``still_failed_windows`` /
    ``auth_aborted`` reflect the LAST round (the residual the operator must re-pull);
    ``complete`` is the union of every round's clean symbols (once-clean stays clean — the
    keep-first ledger). ``clean`` is the BARRIER flag: True iff EVERY selected contract
    landed clean this phase (no quarantine, no still-failing, no auth death).
    """

    bars: int
    windows: int
    failed_pairs: int
    still_failed_windows: int
    auth_aborted: bool
    complete: frozenset[str]
    clean: bool
    #: The union of every round's per-``(contract symbol, interval)`` :class:`IngestReport`
    #: (last-round-wins) + the set of quarantined contract symbols — kept ONLY so the async
    #: post-drain reconcile (DEC-GROWW-FETCH-ASYNC-SEPARATION-001) can fold the background
    #: consumer's real durable per-series counts back in (in async mode the producer returned
    #: ``bars_appended == 0``). Empty for the synchronous path / an empty selection.
    merged_pairs: dict[tuple[str, str], IngestReport]
    failure_symbols: frozenset[str]
    #: The FIRST pass's settled-window skips (DEC-WINDOW-LEDGER-RESCUE-001): each
    #: later until-clean round re-skips the same settled windows by design, so
    #: summing passes would double-count the operator-meaningful number ("how many
    #: already-pulled windows cost NO vendor hit this pull").
    skipped_windows: int = 0
    #: True when the persisted window ledger EXISTED but failed to reload at phase
    #: start (DEC-WINDOW-LEDGER-RESCUE-001) — this phase ran a full re-verify
    #: (no skip); folded into the report note, never silent.
    ledger_load_failed: bool = False
    #: The mass-empty circuit-breaker halt message (DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001
    #: minor M-5) when this phase HALTED mid-flight on a suspected soft-throttle [R2] /
    #: mis-addressing [R3] clean-empty burst; ``None`` when the breaker never tripped.
    mass_empty_cause: str | None = None
    #: Windows THIS phase newly settled as terminal ``SKIPPED`` — the per-pull ledger
    #: tally (:attr:`~brutex.data.window_ledger.WindowLedger.newly_settled_skipped`)
    #: the honest verdict names. 0 for a dry-run (no ledger — nothing settles).
    settled_skipped: int = 0
    #: Keep-first vendor re-statements across the phase's passes (sum —
    #: DEC-VENDOR-CONFLICT-SURFACE-001; the async reconcile re-derives it from
    #: the consumer's per-series totals).
    conflicts: int = 0
    #: Within-batch duplicate vendor rows dropped across the phase's passes (sum).
    vendor_dup_rows_dropped: int = 0
    #: MAX absolute keep-first close delta across the phase (exact Decimal;
    #: ``None`` = no conflict anywhere).
    max_close_delta: Decimal | None = None


def _drain_fetch_pipeline_in_phase(pipeline: FetchPipeline | None) -> None:
    """Drain the async fetch_pipeline from inside ``_run_phase`` BEFORE the ledger persists.

    DEC-FNO-ASYNC-PIPELINE-WIRE-001. Delegates to the COMMON
    :func:`~brutex.adapters.groww.pull_pipeline.drain_fetch_pipeline` (one drain implementation,
    lazy-imported to avoid a module-load cycle — the SAME pattern
    :func:`pull_contract_candles` uses for the build/drain helpers). A ``None`` pipeline (the
    synchronous / dry-run / kill-switch path) is a no-op. Idempotent — the caller's outer
    ``finally`` drain re-invokes it harmlessly. A captured ``LakeWriteHalt`` PROPAGATES (the
    graceful pull-halt path); any other drain error is logged + swallowed inside the helper so the
    ledger persist still runs.
    """
    from brutex.adapters.groww.pull_pipeline import drain_fetch_pipeline

    drain_fetch_pipeline(pipeline)


def _reconcile_phase_with_pipeline(
    phase: _PhaseResult,
    pipeline: FetchPipeline | None,
    *,
    selected_symbols: frozenset[str],
) -> _PhaseResult:
    """Fold the async pipeline's post-drain per-series totals back into a phase result — pure.

    DEC-FNO-ASYNC-PIPELINE-WIRE-001 completion-count fix — the F&O twin of the spot spine's
    :func:`~brutex.adapters.groww.pull_pipeline.reconcile_pairs_with_pipeline`. In async-pipeline
    mode (DEC-GROWW-FETCH-ASYNC-SEPARATION-001) every contract's SUCCESS-path tail (durable
    ``sink.append`` + ``ledger.record``) runs on a BACKGROUND consumer, so the producer returns a
    DEFERRED per-pair :class:`IngestReport` with ``bars_appended == 0`` and any tail-side
    transient (a ``PartitionLockTimeout``) is folded onto the pipeline (NOT the synchronous report).
    Without this reconcile :attr:`_PhaseResult.bars` would read 0 and a tail-transient-deferred
    contract would be falsely marked clean → ``candle_status=complete``.

    Called AFTER :meth:`~brutex.adapters.groww.fetch_pipeline.FetchPipeline.drain` (so the consumer
    totals are final), this OVERRIDES each deferred pair's ``bars_appended`` / ``conflicts`` /
    ``still_failed_windows`` (== the consumer's per-series ``transient``) with the real durable
    totals, then RE-DERIVES :attr:`bars` (the per-series bar sum), :attr:`still_failed_windows`
    (the tail-transient total), :attr:`complete` (the clean set excluding any contract with a
    quarantine OR a tail-transient residual), and the :attr:`clean` barrier — so the async-mode
    counts + verdict equal the synchronous path's. ``windows`` / ``failed_pairs`` / ``auth_aborted``
    are producer-resolved (correct as-is) and are left untouched. A ``None`` pipeline (the
    synchronous default) returns *phase* unchanged.
    """
    if pipeline is None:
        return phase
    per_series = pipeline.per_series_totals()
    if not per_series:
        return phase
    reconciled_pairs: dict[tuple[str, str], IngestReport] = {}
    for key, report in phase.merged_pairs.items():
        totals = per_series.get(key)
        if totals is None:
            reconciled_pairs[key] = report
            continue
        reconciled_pairs[key] = replace(
            report,
            bars_appended=totals.bars_appended,
            conflicts=totals.conflicts,
            transient_failures=totals.transient,
            still_failed_windows=totals.transient,
            # DEC-VENDOR-CONFLICT-SURFACE-001: the deferred producer report read
            # 0/None for the vendor-behaviour counters (the consumer did the
            # durable write) — fold the real per-series totals back in.
            vendor_dup_rows_dropped=totals.vendor_dup_rows_dropped,
            max_close_delta=totals.max_close_delta,
        )
    # Re-derive the clean set from the reconciled pairs: a contract is clean iff it landed at
    # least one pair, was not quarantined, and left NO transient residual (now visible post-drain).
    still_failing = {
        symbol
        for (symbol, _interval), pair in reconciled_pairs.items()
        if pair.still_failed_windows
    }
    landed = {symbol for (symbol, _interval) in reconciled_pairs}
    complete = frozenset(landed - phase.failure_symbols - still_failing)
    reconciled_max_delta: Decimal | None = None
    for pair in reconciled_pairs.values():
        reconciled_max_delta = fold_max_close_delta(reconciled_max_delta, pair.max_close_delta)
    return replace(
        phase,
        bars=sum(r.bars_appended for r in reconciled_pairs.values()),
        still_failed_windows=sum(r.still_failed_windows for r in reconciled_pairs.values()),
        complete=complete,
        clean=selected_symbols <= complete,
        merged_pairs=reconciled_pairs,
        conflicts=sum(r.conflicts for r in reconciled_pairs.values()),
        vendor_dup_rows_dropped=sum(r.vendor_dup_rows_dropped for r in reconciled_pairs.values()),
        max_close_delta=reconciled_max_delta,
    )


def _build_note(
    *,
    dry_run: bool,
    contracts_selected: int,
    contracts_completed: int,
    windows: int,
    failed_pairs: int,
    still_failed_windows: int,
    auth_aborted: bool,
    mass_empty_halted: bool = False,
    contracts_not_yet_in_window: int = 0,
    windows_skipped: int = 0,
    ledger_load_failed: bool = False,
    expiries_out_of_scope: int = 0,
    contracts_out_of_scope: int = 0,
    greeks_enrich_ok: bool | None = None,
    greeks_enrich_error: str | None = None,
    class_unresolved: int = 0,
    greeks_strict_withheld: bool = False,
) -> str:
    """Honest one-line outcome note for the report / live panel.

    Single-phase 1-minute pull (DEC-1MINUTE-ONLY-DERIVE-1DAY-001): the note describes
    the 1-minute phase's outcome — empty selection / dry-run / auth death / mass-empty
    halt / quarantine / transient residual / clean. The 1-day derivation runs after,
    quarantined, and never enters the note (its failure is logged, not surfaced as a
    pull outcome).

    *contracts_not_yet_in_window* (DEC-FNO-FAR-EXPIRY-WINDOW-001, display honesty):
    the clean line's ``completed/selected`` ratio counts the WHOLE selection, so a
    clean pull with strict-law skips would read like partial failure ("8/12"); when
    any contract was skipped as not-yet-open the ratio carries the honest breakdown
    ("8/12 contract(s) (4 not yet in window)") so a skip is never mistaken for a
    failure. Zero (the default) renders byte-identically to before.

    *windows_skipped* / *ledger_load_failed* (DEC-WINDOW-LEDGER-RESCUE-001, applied
    UNIFORMLY to whichever base note is built): a positive skip count appends
    "— skipped N already-pulled window(s) (no vendor hit)" so the operator SEES the
    settled-window skip engaging; a failed ledger reload prepends "window ledger
    reload FAILED — running full re-verify pull (no skip); " so a lost cross-run
    memory is announced, never silent. The defaults render byte-identically to before.

    *expiries_out_of_scope* / *contracts_out_of_scope* (DEC-FNO-EXPIRY-SCOPE-001):
    a scoped-out expiry appends ", N expiries out of scope skipped" to whichever
    base is built; an ALL-out-of-scope selection (nothing left to pull) gets its
    OWN honest base — never the misleading "no contracts selected — pull expiries
    + contracts first" (the registry IS populated; the operator's scope excluded
    everything). Stored bars are never touched — the note says so. The zero
    defaults render byte-identically to before.

    *greeks_enrich_ok* / *greeks_enrich_error* (DEC-GREEKS-ENRICH-OBSERVE-001):
    a WHOLE-pass Greeks-enrichment failure (``greeks_enrich_ok is False``)
    appends ", Greeks enrichment FAILED (<ErrorType>) — overlay re-attempted
    next pull" to whichever base is built, so the swallowed pass is surfaced as
    a pull outcome instead of one log line. ``True`` (pass completed —
    per-target quarantines never flip it) and the ``None`` default (pass did
    not run: dry-run) render byte-identically to before.

    *class_unresolved* (DEC-PULL-SIX-STEP-ORDER-001): a positive count appends
    ", N contract(s) with unresolved index/stock class (ordered as stocks)" —
    the honest fail-closed-for-ORDER clause (the legs were still pulled, just
    ordered stock-side). The zero default renders byte-identically to before.

    *greeks_strict_withheld* (DEC-PULL-SIX-STEP-ORDER-001): ``True`` appends
    ", Greeks enrichment withheld (strict gate) — re-attempted next pull" —
    the strict barrier withheld the pass on an unclean prior/F&O step. NEVER
    a FAILED verdict (nothing ran, nothing is lost — the overlay is
    re-derivable). The ``False`` default renders byte-identically to before.
    """
    prefix = (
        "window ledger reload FAILED — running full re-verify pull (no skip); "
        if ledger_load_failed
        else ""
    )
    class_suffix = (
        f", {class_unresolved} contract(s) with unresolved index/stock class (ordered as stocks)"
        if class_unresolved
        else ""
    )
    suffix = (
        f" — skipped {windows_skipped} already-pulled window(s) (no vendor hit)"
        if windows_skipped
        else ""
    )
    scope_suffix = (
        f", {expiries_out_of_scope} expiries out of scope skipped"
        if expiries_out_of_scope and contracts_selected
        else ""
    )
    greeks_suffix = (
        f", Greeks enrichment FAILED ({(greeks_enrich_error or 'error').split(':', 1)[0]}) "
        "— overlay re-attempted next pull"
        if greeks_enrich_ok is False
        else ""
    )
    if greeks_strict_withheld:
        # DEC-PULL-SIX-STEP-ORDER-001: the strict-withheld pass is NAMED (never
        # silence, never FAILED — the pass did not run; ok stays None).
        greeks_suffix = ", Greeks enrichment withheld (strict gate) — re-attempted next pull"
    if contracts_selected == 0 and contracts_out_of_scope:
        base = (
            f"all {contracts_out_of_scope} selected contract(s) across "
            f"{expiries_out_of_scope} expiry(ies) fall outside the requested expiry "
            "scope — nothing pulled (already-stored bars untouched)"
        )
    elif contracts_selected == 0:
        base = (
            "no contracts selected — pull expiries + contracts first (the registry is "
            "empty for this selection), then pick a contract / chain / underlying to pull"
        )
    elif dry_run:
        base = (
            "dry-run: set Groww credentials to download the selected contracts' OHLC "
            "(an expired contract's candles are real vendor data, never fabricated offline)"
        )
    elif auth_aborted:
        base = "auth token expired mid-pull — re-set Groww credentials and re-pull (idempotent)"
    elif mass_empty_halted:
        # DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001 minor M-5: the mass-empty breaker halted
        # the phase — the report's `verdict` carries the full count-naming message +
        # triage actions; this note is the one-line panel summary.
        base = (
            "mass-empty circuit breaker HALTED the pull (suspected vendor soft-throttle "
            "or mis-addressing) — see the verdict: triage, then re-pull; un-settled "
            "windows stay retryable and `brutex reopen-skipped` reverses earlier settles"
        )
    elif failed_pairs:
        base = (
            f"pulled {windows} windows; {failed_pairs} contract/interval pair(s) quarantined — "
            "re-pull to retry the deferred windows (WindowLedger skips the done ones)"
        )
    elif still_failed_windows:
        base = (
            f"pulled {windows} windows; {still_failed_windows} transient window(s) still "
            "failing after the re-pull rounds — re-pull to drain them (WindowLedger skips the done)"
        )
    else:
        skipped_suffix = (
            f" ({contracts_not_yet_in_window} not yet in window)"
            if contracts_not_yet_in_window
            else ""
        )
        base = (
            f"pulled {windows} windows across {contracts_completed}/{contracts_selected} "
            f"contract(s){skipped_suffix} into the candle lake (rerun-safe — WindowLedger dedups)"
        )
    return f"{prefix}{base}{suffix}{scope_suffix}{class_suffix}{greeks_suffix}"


def _mark_complete(
    registry: RegistryStore,
    rebuilt: Sequence[_RebuiltContract],
    completed_symbols: frozenset[str],
) -> int:
    """Mark each clean-landed contract ``candle_status=complete``; return the count updated.

    Keys the registry update on the row's authoritative ``(exch, underlying, contract_symbol)``
    PK (never a re-parse). A symbol absent from *completed_symbols* (it quarantined or left a
    transient residual) is LEFT ``none`` — honest, never marked complete on a partial pull.
    On a dry-run the empty-candle client appends 0 bars but the pair still records SUCCESS
    (an EMPTY window is a settled, clean window), so the selected contracts ARE marked
    complete — the registry status reflects coverage of the SELECTED range, which the
    dry-run honestly settled as empty.

    The whole stamp loop runs as ONE deadline-guarded registry write batch
    (:meth:`~brutex.data.registry_store.RegistryStore.mark_candle_statuses` — O(1)
    guard cost, DEC-STORE-WRITE-DEADLINE-001). A store-write-deadline breach DEFERS
    the stamps (one loud WARN, return ``0`` — the durably-marked count is honestly
    unknown): the unmarked contracts keep ``candle_status="none"`` and are re-marked
    idempotently by the next pull; the window LEDGER, not this stamp, drives window
    re-pull, so no candle data is ever lost. Never a raise past this seam — a wedged
    registry volume must not fail the pull whose candles already landed durably.
    """
    rows = [
        (row.exch, row.underlying, row.contract_symbol)
        for row, _contract in rebuilt
        if row.contract_symbol in completed_symbols
    ]
    if not rows:
        return 0
    try:
        registry.mark_candle_statuses(rows, CANDLE_STATUS_COMPLETE)
    except StoreWriteDeadlineExceeded as exc:
        _LOG.warning(
            HISTORICAL_MARK_COMPLETE_DEFERRED_EVENT,
            contracts=len(rows),
            error_type=type(exc).__name__,
            defer_seconds=exc.deadline_seconds,
        )
        return 0
    return len(rows)


#: The per-window ledger identity — ``(market, security_id, interval, window_start,
#: window_end)`` — used by the strike_chain.realized THIS-pull baseline below.
_WindowAttemptKey = tuple[str, str, str, str, str]


def _bounded_window_attempts(
    ledger: WindowLedger, empty_retry_by_symbol: Mapping[str, int]
) -> dict[_WindowAttemptKey, int]:
    """Snapshot the bounded symbols' per-window ``attempts`` — the pre-phase baseline.

    DEC-TELEMETRY-SCOPE-BUDGET-RUNCOUNTS-001 (item: deep-budget realized savings).
    The durable ledger loaded at phase start already carries PRIOR pulls' rows
    (each with the LAST pull's ``in_pull_fetches``), so the post-phase
    ``strike_chain.realized`` summary must restrict itself to windows attempted
    THIS pull. ``attempts`` bumps exactly once per pull that records a window,
    so "this pull" = ``attempts`` grew past this baseline (a never-seen window
    keys to the 0 default). Bounded symbols only (the sparse reduced-budget
    map), so the snapshot is O(bounded windows) not O(ledger). Read-only.
    """
    return {
        (
            record.market,
            record.security_id,
            record.interval,
            record.window_start,
            record.window_end,
        ): record.attempts
        for record in ledger.records()
        if record.security_id in empty_retry_by_symbol
    }


def _emit_strike_chain_realized(
    ledger: WindowLedger,
    *,
    interval: str,
    empty_retry_by_symbol: Mapping[str, int],
    pre_attempts: Mapping[_WindowAttemptKey, int],
) -> None:
    """Emit ONE ``strike_chain.realized`` INFO for the phase — observe-only, guarded.

    DEC-TELEMETRY-SCOPE-BUDGET-RUNCOUNTS-001, closing the #1026 observability gap:
    the deep-strike EMPTY-retry budget (DEC-FETCH-PER-WINDOW-GUARANTEE-001) shipped
    with NO per-pull realized signal, so the operator could not see whether the
    bounding actually engaged on a real pull. This scans the ledger's THIS-pull
    rows (``attempts`` grew past *pre_attempts* — see
    :func:`_bounded_window_attempts`) for the reduced-budget symbols at this
    phase's *interval* and reports:

    * ``strikes_bounded`` — contracts carrying a REDUCED budget this pull;
    * ``bounded_windows_attempted`` — their windows actually attempted this pull;
    * ``realized_confirms`` — of those, the clean-EMPTY confirms (status ``EMPTY``
      or settled ``SKIPPED``, zero bars) — the bounded fetches that would have
      re-fired the full retry loop without the budget;
    * ``fetches_saved_estimate`` — ``Σ max(full_budget − in_pull_fetches, 0)`` over
      the confirms. HONEST estimate (Golden Rule #10): the unbounded pull would
      have spent the FULL budget only on a strike that stayed empty every retry —
      which is exactly what a confirm observed — but the counterfactual is not
      re-run, so this is an estimate, never a measured A/B.

    Observe-only + fully guarded (the house `emit_underlying_all_empty` pattern):
    changes NO return value / ledger row / fetch behaviour; an internal error logs
    ``strike_chain.realized_error`` and NEVER breaks the pull. The caller emits
    only when a reduced-budget map exists AND a real ledger is open — a dry-run or
    an unbounded chain emits NOTHING (default-parity, zero new log calls).
    """
    try:
        bounded_windows_attempted = 0
        realized_confirms = 0
        fetches_saved_estimate = 0
        for record in ledger.records():
            if record.security_id not in empty_retry_by_symbol or record.interval != interval:
                continue
            key = (
                record.market,
                record.security_id,
                record.interval,
                record.window_start,
                record.window_end,
            )
            if record.attempts <= pre_attempts.get(key, 0):
                continue  # a prior pull's row — not attempted THIS pull
            bounded_windows_attempted += 1
            if record.status in (EMPTY, SKIPPED) and record.bars == 0:
                realized_confirms += 1
                fetches_saved_estimate += max(
                    DEFAULT_EMPTY_RETRY_ATTEMPTS - record.in_pull_fetches, 0
                )
        _LOG.info(
            "strike_chain.realized",
            interval=interval,
            strikes_bounded=len(empty_retry_by_symbol),
            bounded_windows_attempted=bounded_windows_attempted,
            realized_confirms=realized_confirms,
            fetches_saved_estimate=fetches_saved_estimate,
            full_budget=DEFAULT_EMPTY_RETRY_ATTEMPTS,
        )
    except Exception as exc:  # observe-only telemetry must never break the pull
        _LOG.warning("strike_chain.realized_error", interval=interval, error=str(exc))


def _run_phase(
    *,
    interval: str,
    by_bucket: dict[_BucketKey, list[_RebuiltContract]],
    selected_symbols: frozenset[str],
    step_monitor: StepOrderMonitor,
    client: GrowwClientProtocol,
    sink: BarSink,
    lake_root: Path,
    dry_run: bool,
    start: date,
    end: date,
    pace: Callable[[], None] | None,
    on_rate_limit: Callable[[], None] | None,
    on_timeout: Callable[[], None] | None,
    on_success: Callable[[], None] | None,
    on_progress: ProgressHook | None,
    session_rate: SessionRateController | None,
    until_clean: bool,
    until_clean_rounds: int,
    contract_windows: ContractWindows | None,
    empty_retry_by_symbol: dict[str, int] | None,
    run_parallel: RunParallel | None,
    write_pool: WriteConsumer | None,
    fetch_pipeline: FetchPipeline | None,
    audit: AuditWriter | None = None,
    on_bars: Callable[[int], None] | None = None,
    on_ledger_sync: OnLedgerSync | None = None,
) -> _PhaseResult:
    """Run ONE interval's full pull over *by_bucket* and fold its until-clean passes.

    The single ingest call site (CLAUDE.md §11 COMMON) — BOTH phases funnel through here,
    differing only by *interval* and the optional per-contract *contract_windows* bounds.
    Opens its OWN pull-ledger (each phase's windows are namespaced by interval already), runs
    the one-shot path OR the bounded until-clean loop, and returns the folded
    :class:`_PhaseResult`. The barrier flag :attr:`_PhaseResult.clean` is True iff EVERY
    selected contract landed clean with no quarantine / still-failing / auth death.

    **STEP-major (DEC-PULL-SIX-STEP-ORDER-001):** the ``(ledger-market, is_index)``
    buckets run in the decreed 3→4→5→6 order (:func:`_bucket_sort_key`) and — under
    ``until_clean=True`` — each bucket runs its OWN bounded until-clean loop, so each
    step SETTLES (clean or bounded-terminal) before the next step's first vendor call
    (retry rounds are scoped per step; a cross-step global retry pass no longer
    exists — a documented, law-sanctioned change). A dead token in step k halts steps
    k+1.. loudly (``pull.step.halted``); a tripped mass-empty breaker halts the WHOLE
    phase (the existing handler, committed work kept) with the remaining steps
    likewise announced. *step_monitor* attributes every per-pair settle to its step
    (late writes WARN + count) and is the tests' hard ordering oracle.
    """
    ledger: WindowLedger | None = begin_pull_ledger(lake_root, dry_run=dry_run)
    # LOUD reload failure (DEC-WINDOW-LEDGER-RESCUE-001): the persisted ledger existed
    # but could not be read — the store already WARNed (window_ledger.load_failed);
    # surface it on THIS phase's result too so the pull report/note announces
    # "window ledger reload FAILED — running full re-verify pull (no skip)".
    # Never crashes the pull; never silent.
    ledger_load_failed = ledger is not None and ledger.load_failed
    if ledger_load_failed:
        _LOG.warning(
            "historical.pull.ledger_reload_failed",
            interval=interval,
            note="window ledger reload FAILED — running full re-verify pull (no skip)",
        )
    # Cold-start seed (DEC-FNO-LEDGER-LAKE-RECONCILE-001): on a fresh clone / lost /
    # orphaned ledger the F&O candle lake may already hold this contract chain's bars
    # while the ledger is empty, so once the ledger drives the skip a re-pull would
    # re-fetch the ENTIRE options chain (every strike × CE/PE × expiry). Seed SUCCESS for
    # every (contract, interval) window the lake already ENCLOSES — only ABSENT rows, so a
    # FAILED / settled / genuinely-missing / partially-covered window is left re-pullable —
    # BEFORE ``plan_contract_windows`` seeds PLANNED, so the upcoming ``is_skippable`` flip
    # will NOT re-pull windows already durable in the lake (the F&O twin of the spot spine's
    # ``reconcile_ledger_with_lake``). The lake span scan is O(datasets) and runs ONCE per
    # phase (loaded here, reused across every market bucket); then a pure per-market seed lands
    # each contract's window in the SAME namespace the fetch records under (``groww-future`` /
    # ``groww-option``). A ``None`` ledger (dry-run) skips this entirely — the span read is
    # spent only on a real pull.
    census_spans: Sequence[dict[str, Any]] = []
    census_rows: list[tuple[Any, ...]] = []
    if ledger is not None:
        spans = load_lake_spans(lake_root)
        # Buckets REFINE (never cross) the market split, so the per-bucket
        # reconcile with ``market=key[0]`` is byte-equivalent to the legacy
        # per-market reconcile (DEC-PULL-SIX-STEP-ORDER-001). The span index
        # is built ONCE for the phase and shared across the 4 bucket
        # reconciles (round-2 review polish — the per-call ``index_lake_spans``
        # rebuild was a pure O(spans) dict build repeated per bucket).
        span_index = index_lake_spans(spans)
        for (reconcile_market, _is_index), reconcile_bucket in by_bucket.items():
            reconcile_fno_ledger_windows(
                ledger,
                spans,
                contracts=[contract for _row, contract in reconcile_bucket],
                intervals=(interval,),
                start=start,
                end=end,
                market=reconcile_market,
                contract_windows=contract_windows,
                span_index=span_index,
            )
        # Written-vs-settled census snapshot (DEC-WRITTEN-SETTLED-CENSUS-001):
        # capture BOTH sides pre-plan — the SAME already-loaded spans + the
        # just-reconciled ledger rows — so the post-drain emit below compares a
        # pre-pull-CONSISTENT pair (this pull's fresh writes/settles can never
        # false-flag each other against the pre-pull span read). Zero extra I/O.
        census_spans = spans
        census_rows = ledger.to_rows()
    # THIS-pull baseline for the observe-only ``strike_chain.realized`` summary
    # (DEC-TELEMETRY-SCOPE-BUDGET-RUNCOUNTS-001): snapshot the reduced-budget symbols'
    # per-window ``attempts`` BEFORE any pass runs, so the post-phase emit counts only
    # windows this pull actually attempted. ``None`` for a dry-run (no ledger) or an
    # unbounded chain (empty budget map) — then nothing is emitted (default parity).
    bounded_pre_attempts = (
        _bounded_window_attempts(ledger, empty_retry_by_symbol)
        if ledger is not None and empty_retry_by_symbol
        else None
    )
    flush = make_live_ledger_flush(ledger, lake_root) if ledger is not None else None

    # Honest live F&O progress (DEC-PULL-PROGRESS-HONESTY-001): a THROTTLED, market/
    # interval-FILTERED absolute fold of the pull's own ledger, pushed to the caller's
    # on_ledger_sync per per-pair tick (≤ once per LIVE_SYNC_INTERVAL_S). REUSES the
    # COMMON make_live_sync (the spot spine's proven throttle — CLAUDE.md §11 COMMON;
    # lazy import mirrors register_pull_meter's ordering-decoupled style). The flush
    # side is a no-op lambda — the per-pair wrapper below already runs the throttled
    # make_live_ledger_flush, and progress_counts reads the in-memory ledger directly.
    # A dry-run (no ledger) or an unwired caller keeps the byte-identical no-op.
    def _no_sync() -> None:
        """No-op live sync (dry-run / unwired caller) — byte-identical to before."""

    live_sync: Callable[[], None] = _no_sync
    if ledger is not None and on_ledger_sync is not None:
        from brutex.dashboard.pull_controller import make_live_sync

        live_sync = make_live_sync(
            ledger,
            lambda: None,
            on_ledger_sync,
            markets=frozenset(market for market, _is_index in by_bucket),
            intervals=(interval,),
        )
    # Raw-data integrity capture (DEC-RAW-DATA-INTEGRITY-001): bind the per-contract
    # capture-hook factory to the lake root on the LIVE path so every F&O window
    # records a hash of EXACTLY what Groww sent. Skipped for a dry-run (no real
    # vendor bytes to attest); best-effort inside the hook — it can never halt a pull.
    capture_hook_factory = None if dry_run else make_capture_hook_factory(lake_root)
    # Vendor-down error-storm breaker + dead-credential StillStale escalation
    # (DEC-VENDOR-DOWN-BREAKER-001): ONE per-phase breaker shared by every market
    # bucket / until-clean round (any success resets it, so a recovering vendor never
    # trips). Armed ONLY for a REAL live pull — a dry-run threads ``None`` (the same
    # gating as ``capture_hook_factory`` above), so a synthetic pull never observes.
    storm_breaker = None if dry_run else resolve_error_storm_breaker()
    # Observed-sessions overlay (DEC-CALENDAR-OBSERVED-SESSIONS-001): loaded ONCE per
    # phase and threaded into the FUTURES partial-window observe margin ONLY (an
    # observed holiday is not a data hole) — NEVER into window planning / the 30d/60d
    # window law. A dry-run threads the empty overlay (the ``capture_hook_factory``
    # gating pattern); the loader never raises (missing/corrupt -> empty).
    observed_holidays = frozenset() if dry_run else load_observed_holidays(lake_root)
    # Mass-empty breaker CANARY (DEC-EMPTY-BREAKER-CANARY-001): armed ONLY for a
    # REAL live pull (a dry-run threads None — the same gating as the storm
    # breaker above). Before the mid-run EmptySettleBreaker halts the whole F&O
    # phase it fetches ONE known-liquid NIFTY-spot canary window through THIS
    # phase's client (+ the shared pace gate — 1 governed vendor call): a
    # healthy canary means the mass empties are genuine (a 2020-era dead chain
    # at the bucket head), so the phase continues instead of false-halting.
    breaker_canary = None if dry_run else make_breaker_canary_probe(client, pace=pace)

    def _progress(label: str, bars: int, failed: bool) -> None:
        """Forward to the caller's hook + flush the ledger (either side optional).

        DEC-PULL-PROGRESS-HONESTY-001: also forwards the pair's report *bars* to
        *on_bars* — REAL in SYNC mode (no pipeline; the only bars source there) and
        an honest 0 in ASYNC mode (the pipeline consumer folds the real deltas via
        its own ``on_bars`` seam), so each bar is counted EXACTLY once in both
        modes — and fires the throttled, F&O-scope-filtered ledger sync so the
        units denominator tracks the pull's own windows instead of pinning at 100%.
        """
        if on_progress is not None:
            on_progress(label, bars, failed)
        if on_bars is not None:
            on_bars(bars)
        if flush is not None:
            flush()
        live_sync()

    passes: list[_PassResult] = []
    # Per-BUCKET pass lists (DEC-PULL-SIX-STEP-ORDER-001): each step's own rounds, so the
    # fold below can read a bucket's FIRST-round skips + LAST-round residual — and the
    # halt logs can report the halting BUCKET's own ``rounds_completed`` (review fix) —
    # while the flat ``passes`` list keeps the whole-phase sums.
    passes_by_bucket: dict[_BucketKey, list[_PassResult]] = {}
    # Merged per-pair reports + quarantined symbols across ALL rounds/buckets (last-round-wins),
    # kept ONLY for the async post-drain reconcile (DEC-FNO-ASYNC-PIPELINE-WIRE-001). The
    # synchronous path never reads them (the reconcile is a no-op when ``fetch_pipeline`` is None).
    merged_pairs: dict[tuple[str, str], IngestReport] = {}
    failure_symbols: set[str] = set()

    def _one_bucket_pass(bucket_key: _BucketKey, bucket: list[_RebuiltContract]) -> _PassResult:
        """Run ONE ingest pass over ONE ordering bucket at *interval*; collect clean.

        Re-attempts the SHARED ledger's prior-round FAILED windows (the ledger skips the
        done ones). The per-contract *contract_windows* bounds the heavy sub-daily fetch
        to each contract's discovered listing→expiry span. The bucket's per-pair progress
        is composed with the step monitor's ``observe_write`` (late writes WARN + count,
        never raise — DEC-PULL-SIX-STEP-ORDER-001).
        """
        market, _is_index = bucket_key
        step = _STEP_FOR_BUCKET[bucket_key]
        pass_bars = 0
        pass_windows = 0
        pass_failed_pairs = 0
        pass_still_failed = 0
        pass_skipped = 0
        pass_conflicts = 0
        pass_dup_rows = 0
        pass_max_delta: Decimal | None = None
        pass_auth = False
        clean: set[str] = set()

        def _step_progress(label: str, bars: int, failed: bool) -> None:
            """The per-pair progress wrapper, step-attributed for the ordering monitor."""
            _progress(label, bars, failed)
            step_monitor.observe_write(step, symbol=label)

        try:
            report: GrowwOptionIngestReport = run_groww_option_ingest(
                client,
                sink,
                contracts=[contract for _row, contract in bucket],
                intervals=(interval,),
                start=start,
                end=end,
                pace=pace,
                ledger=ledger,
                on_rate_limit=on_rate_limit,
                on_timeout=on_timeout,
                on_success=on_success,
                on_progress=_step_progress,
                market=market,
                contract_windows=contract_windows,
                empty_retry_by_symbol=empty_retry_by_symbol,
                run_parallel=run_parallel,
                capture_hook_factory=capture_hook_factory,
                write_pool=write_pool,
                fetch_pipeline=fetch_pipeline,
                observed_holidays=observed_holidays,
                breaker_health_probe=breaker_canary,
                storm_breaker=storm_breaker,
                audit=audit,
            )
        except GrowwAuthError as exc:
            # A TERMINAL dead-credential auth failure HALTED the F&O spine mid-
            # bucket (DEC-GROWW-AUTH-HALT-001 — the spine's ``_GLOBAL_STOP_ERRORS``
            # raise). Map it to the SAME graceful auth-abort the pre-halt quarantine
            # produced — the pass reports ``auth_aborted`` (the "auth token expired"
            # verdict/note), remaining steps are halted by the step loop (every one
            # would fail identically), committed work + the ledger survive via the
            # outer finally. Because the halt is CAUGHT here (it never escapes to the
            # job layer's ``_is_pull_halt`` path), the CRITICAL Telegram pull-halt
            # alert fires HERE (DEC-TELEGRAM-PULL-HALT-001 parity with the mass-empty
            # handler below; best-effort by construction). A RETRYABLE auth-family
            # shape (throttled mint / stale read-only re-read) re-raises unchanged.
            if not is_terminal_auth_failure(exc):
                raise
            emit_pull_halt_alert(exc)
            pass_auth = True
            # ``rounds_completed`` is the HALTING BUCKET's own completed
            # until-clean rounds (review fix): the flat ``passes`` list is now
            # phase-wide across ALL buckets under the step-major split, so
            # ``len(passes)`` was neither this bucket's nor the phase's round
            # count (an auth halt in bucket 3 after buckets 1+2 ran 2+1 rounds
            # read a meaningless 3). The current, aborted pass is not counted
            # (it never appended) — same convention as before the split.
            _LOG.warning(
                "historical.pull.auth_halt",
                market=market,
                error_type=type(exc).__name__,
                rounds_completed=len(passes_by_bucket.get(bucket_key, ())),
                note=(
                    "terminal Groww auth failure — dead credential / re-mint budget "
                    "exhausted; committed work kept; re-pull after re-auth"
                ),
            )
        else:
            pass_bars += report.bars_appended
            pass_windows += report.windows
            pass_failed_pairs += report.failed_pairs
            pass_still_failed += report.still_failed_windows
            pass_skipped += report.skipped_windows
            pass_conflicts += report.conflicts
            pass_dup_rows += report.vendor_dup_rows_dropped
            pass_max_delta = fold_max_close_delta(pass_max_delta, report.max_close_delta)
            pass_auth = pass_auth or report.auth_aborted
            clean |= _clean_symbols(report)
            # Fold this bucket's per-pair reports + quarantines into the phase-wide merge so
            # the async post-drain reconcile can override the deferred counts (no-op
            # synchronously).
            merged_pairs.update(report.pairs)
            failure_symbols.update(f.symbol for f in report.failures)
        result = _PassResult(
            bars=pass_bars,
            windows=pass_windows,
            failed_pairs=pass_failed_pairs,
            still_failed_windows=pass_still_failed,
            auth_aborted=pass_auth,
            clean_symbols=frozenset(clean),
            skipped_windows=pass_skipped,
            conflicts=pass_conflicts,
            vendor_dup_rows_dropped=pass_dup_rows,
            max_close_delta=pass_max_delta,
        )
        passes.append(result)
        passes_by_bucket.setdefault(bucket_key, []).append(result)
        return result

    def _bucket_round_runner(
        bucket_key: _BucketKey, bucket: list[_RebuiltContract]
    ) -> Callable[[], UntilCleanRound]:
        """Bind one bucket's until-clean round runner (no loop-variable capture)."""

        def _round() -> UntilCleanRound:
            res = _one_bucket_pass(bucket_key, bucket)
            # Groww derives auth_aborted from quarantined failures and carries no
            # auth_cause string (the documented divergence — mirrors the spot/chain
            # paths), so auth_cause stays at its None default.
            return UntilCleanRound(
                still_failed_windows=res.still_failed_windows,
                auth_aborted=res.auth_aborted,
            )

        return _round

    # Groww backtesting has NO daily quota (DEC-GROWW-NO-DAILY-QUOTA-001), so this
    # brake is dormant; wired for parity with the spot/chain paths. Offline /
    # dry-run pulls have no controller → no hook (never the limit).
    quota_hook = (
        session_rate.get().quota_exhausted if (session_rate is not None and not dry_run) else None
    )
    # STEP-major bucket order (DEC-PULL-SIX-STEP-ORDER-001): the decreed 3→4→5→6 via
    # ``_bucket_sort_key`` — a STABLE, deterministic, total order independent of dict
    # insertion order (supersedes the 2-key futures-first ``_MARKET_PULL_ORDER`` sort).
    ordered_buckets = sorted(by_bucket.items(), key=lambda kv: _bucket_sort_key(kv[0]))
    mass_empty_cause: str | None = None
    auth_latched = False
    #: Buckets ENTERED so far — on a mid-bucket MassEmptySettleHalt the not-yet-started
    #: remainder is exactly ``ordered_buckets[buckets_started:]`` (announced loudly).
    buckets_started = 0
    try:
        for bucket_key, bucket in ordered_buckets:
            buckets_started += 1
            step = _STEP_FOR_BUCKET[bucket_key]
            if auth_latched:
                # A dead token in an earlier step — every remaining step would fail
                # identically against it; halt LOUDLY, never silently (per step).
                _LOG.warning(
                    "pull.step.halted",
                    step=step.label,
                    ordinal=int(step),
                    reason="auth_expired",
                )
                continue
            step_monitor.begin_step(step)
            _LOG.info(
                "pull.step.started",
                step=step.label,
                ordinal=int(step),
                contracts=len(bucket),
            )
            if until_clean:
                # Each STEP settles — clean or bounded-terminal (clean/stuck/budget/
                # quota stops) — before the next step's first vendor call: the
                # until-clean rounds are scoped PER BUCKET (the round-major cross-step
                # retry pass no longer exists — a law-sanctioned change).
                # DISCLOSED envelope (round-2 review, Golden Rule #10): the mass-empty
                # ``EmptySettleBreaker`` is resolved fresh per ``run_groww_option_ingest``
                # invocation ("one breaker per PASS"), and passes are now per BUCKET —
                # so its trip-EVIDENCE population (consecutive clean-empties / the
                # fraction over min-observations) RESETS at the index/stock boundary
                # within each market, half the per-market pass population main
                # accumulated over. A mass-empty burst spanning that boundary trips
                # later (or, on a tiny bucket below the min-observations floor, not at
                # all — extra poisoned windows can settle SKIPPED before the halt);
                # the env-tunable thresholds, the vendor-health canary and the #919/
                # #1031 bounded-settle machinery are unchanged and still bound the
                # damage. Sharing one breaker per market across its two buckets is a
                # possible future tightening, NOT done here (an injected instance
                # bypasses the per-pass expected-empty predicate + canary wiring).
                run_until_clean(
                    _bucket_round_runner(bucket_key, bucket),
                    max_rounds=until_clean_rounds,
                    quota_exhausted=quota_hook,
                    stop_when_stuck=True,
                )
            else:
                _one_bucket_pass(bucket_key, bucket)
            bucket_passes = passes_by_bucket.get(bucket_key, ())
            last_pass = bucket_passes[-1] if bucket_passes else None
            step_failed = last_pass.still_failed_windows if last_pass is not None else 0
            step_auth = last_pass is not None and last_pass.auth_aborted
            # HONESTY (review fix, Golden Rule #1): the counts below are
            # PRODUCER-side. In the DEFAULT async-pipeline mode each pair's
            # durable write runs on the background consumer, so ``bars`` sums
            # deferred 0s until the post-drain ``_reconcile_phase_with_pipeline``
            # (which runs AFTER this step loop) restores the real counts — and
            # ``clean`` / ``still_failed`` cannot see tail transients still
            # queued on the pipeline. ``bars_deferred`` discloses that envelope
            # on the event itself; the SYNCHRONOUS path (pipeline off /
            # dry-run) logs real counts with ``bars_deferred=False``.
            _LOG.info(
                "pull.step.settled",
                step=step.label,
                ordinal=int(step),
                clean=(
                    last_pass is not None
                    and not step_auth
                    and last_pass.failed_pairs == 0
                    and step_failed == 0
                ),
                bars=sum(p.bars for p in bucket_passes),
                bars_deferred=fetch_pipeline is not None,
                windows=sum(p.windows for p in bucket_passes),
                still_failed=step_failed,
                skipped=bucket_passes[0].skipped_windows if bucket_passes else 0,
            )
            if step_auth:
                auth_latched = True
    except MassEmptySettleHalt as exc:
        # The mass-empty circuit breaker TRIPPED mid-phase (DEC-EMPTY-WINDOW-CIRCUIT-
        # BREAKER-001 minor M-5 — the F&O bulk route's polished halt, mirroring the
        # spot route's pull_controller handler): a suspected vendor soft-throttle [R2]
        # / mis-addressing [R3] episode was answering contract windows with clean-empty
        # 200s, so the spine HALTED before settling them SKIPPED. Everything already
        # committed survives (bars via the sink; window records via the finally's
        # drain + ledger persist below); every un-settled window stays retryable. The
        # cause is folded onto the phase result so the report's `verdict` reads the
        # loud, needs-operator ``mass_empty_halt`` — not a generic job error. Because
        # the halt is CAUGHT here (it never escapes to the job layer's
        # ``is_pull_halt`` path), the CRITICAL Telegram pull-halt alert fires HERE
        # (DEC-TELEGRAM-PULL-HALT-001 parity with the spot/CLI routes; best-effort by
        # construction).
        emit_pull_halt_alert(exc)
        mass_empty_cause = str(exc)
        # The trip fired inside the bucket ENTERED last (``buckets_started`` was
        # incremented at loop top, so index ``buckets_started - 1`` — the loop
        # body ran at least once for the exception to raise). ``rounds_completed``
        # is THAT bucket's own completed rounds (review fix — the flat ``passes``
        # list is phase-wide under the step-major split, so ``len(passes)`` was
        # neither any bucket's nor the phase's round count); ``rounds_by_step``
        # carries the honest per-step breakdown.
        halted_key = ordered_buckets[buckets_started - 1][0]
        _LOG.warning(
            "historical.pull.mass_empty_halt",
            error=str(exc),
            step=_STEP_FOR_BUCKET[halted_key].label,
            rounds_completed=len(passes_by_bucket.get(halted_key, ())),
            rounds_by_step={
                _STEP_FOR_BUCKET[key].label: len(passes_by_bucket.get(key, ()))
                for key, _bucket in ordered_buckets[:buckets_started]
            },
            note=(
                "mass clean-empty responses — F&O pull halted before settling; "
                "committed work kept; un-settled windows stay retryable"
            ),
        )
        # The steps the trip stopped from ever starting are announced LOUDLY, per
        # step (DEC-PULL-SIX-STEP-ORDER-001) — never a silent disappearance.
        for later_key, _later_bucket in ordered_buckets[buckets_started:]:
            later_step = _STEP_FOR_BUCKET[later_key]
            _LOG.warning(
                "pull.step.halted",
                step=later_step.label,
                ordinal=int(later_step),
                reason="mass_empty_halt",
            )
    finally:
        # CRITICAL ORDER (DEC-FNO-ASYNC-PIPELINE-WIRE-001): drain the async fetch_pipeline
        # BEFORE persisting the ledger. In async mode each contract's tail — incl. the durable
        # ``sink.append`` AND ``ledger.record`` — runs on a BACKGROUND consumer that the producer
        # does NOT wait on, so a still-queued tail's ``ledger.record`` would land on an
        # ALREADY-PERSISTED ledger and be LOST → that window re-fetches needlessly on the next
        # pull (the no-skip-of-settled invariant, Golden Rule #7). Draining here flushes+joins
        # every consumer so EVERY window is ledger-recorded before ``end_pull_ledger`` writes it
        # to disk (settle-after-durable at phase granularity, mirroring the spot spine's
        # drain-then-persist order in pull_controller). A ``None`` pipeline (synchronous /
        # dry-run / kill-switch) is a no-op; the caller's ``finally`` drain stays as an idempotent
        # safety net. The write_pool is NOT drained here — its ``WriteConsumerSink.append`` blocks
        # until durable, so its synchronous tail already records the ledger before this point.
        _drain_fetch_pipeline_in_phase(fetch_pipeline)
        end_pull_ledger(ledger, lake_root, dry_run=dry_run)

    # Observe-only realized-savings summary (DEC-TELEMETRY-SCOPE-BUDGET-RUNCOUNTS-001):
    # AFTER the drain + persist the ledger's in-memory rows are final (async tails
    # folded), so the THIS-pull realized reduced-budget confirms are countable. Fires
    # ONLY when a reduced-budget map exists and a real ledger is open; fully guarded —
    # the pull's control flow, counts and return value are byte-identical either way.
    if bounded_pre_attempts is not None and ledger is not None and empty_retry_by_symbol:
        _emit_strike_chain_realized(
            ledger,
            interval=interval,
            empty_retry_by_symbol=empty_retry_by_symbol,
            pre_attempts=bounded_pre_attempts,
        )

    # Observe-only written-vs-settled census (DEC-WRITTEN-SETTLED-CENSUS-001):
    # one guarded post-drain emit over the PRE-plan snapshot captured above —
    # DETECT+WARN only (the #1014 wiring pattern), can never break the pull.
    # A dry-run (no ledger) emits over empty inputs → a silent clean census.
    emit_written_settled_census(
        census_rows,
        census_spans,
        intervals=(interval,),
        symbols=selected_symbols,
        source="fno",
    )

    # FINAL absolute progress push (DEC-PULL-PROGRESS-HONESTY-001): the pipeline is
    # drained above, so every window is ledger-recorded — push the settled filtered
    # counts ONCE, unthrottled (the last throttled tick may predate the final windows;
    # the spot spine does the same final push in pull_all_spot). Idempotent absolute fold.
    if ledger is not None and on_ledger_sync is not None:
        final = ledger.progress_counts(
            markets=frozenset(market for market, _is_index in by_bucket), intervals=(interval,)
        )
        on_ledger_sync(final.resolved, final.total, final.bars, final.failed, final.current)

    # bars/windows SUM across all rounds of all buckets (round N re-appends only
    # newly-fetched windows — the keep-first sink + ledger skip make double-counting
    # impossible); the residual is each BUCKET's LAST-round residual, summed
    # (DEC-PULL-SIX-STEP-ORDER-001 — the until-clean rounds are step-scoped, so
    # "the last round" is a per-bucket notion now). ``until_clean_rounds <= 0``
    # runs zero rounds → empty passes → all-zero.
    complete: frozenset[str] = (
        frozenset().union(*(p.clean_symbols for p in passes)) if passes else frozenset()
    )
    # The GLOBAL barrier for this phase: EVERY selected contract landed clean (so the union
    # of clean symbols covers the whole selection) — an empty selection clears vacuously.
    barrier_clean = selected_symbols <= complete
    phase_max_delta: Decimal | None = None
    for p in passes:
        phase_max_delta = fold_max_close_delta(phase_max_delta, p.max_close_delta)
    bucket_last = [bucket_passes[-1] for bucket_passes in passes_by_bucket.values()]
    bucket_first = [bucket_passes[0] for bucket_passes in passes_by_bucket.values()]
    return _PhaseResult(
        bars=sum(p.bars for p in passes),
        windows=sum(p.windows for p in passes),
        failed_pairs=sum(p.failed_pairs for p in bucket_last),
        # HONEST UNDERCOUNT on a halt: ``passes`` holds only COMPLETED passes — the
        # pass a mid-flight MassEmptySettleHalt aborted never appended, so its
        # still-pending windows are NOT in this residual (the halt verdict already
        # demands triage + a re-pull; the ledger, not this count, is the retry
        # authority). Likewise a step halted by an earlier dead token contributes
        # nothing here (its windows were never attempted — the ledger re-pulls them).
        still_failed_windows=sum(p.still_failed_windows for p in bucket_last),
        auth_aborted=any(p.auth_aborted for p in bucket_last),
        complete=complete,
        # A halted phase is never the clean barrier, whatever landed before the trip.
        clean=barrier_clean and mass_empty_cause is None,
        merged_pairs=merged_pairs,
        failure_symbols=frozenset(failure_symbols),
        # Each bucket's FIRST-round skips, summed — the operator-meaningful "no
        # vendor hit" count (later until-clean rounds re-skip the same settled
        # windows by design; summing every round would double-count).
        skipped_windows=sum(p.skipped_windows for p in bucket_first),
        ledger_load_failed=ledger_load_failed,
        mass_empty_cause=mass_empty_cause,
        # Honest-verdict settle tally (DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001): every
        # window THIS phase newly settled SKIPPED (fast-settle + streak promotion),
        # counted by the per-pull ledger instance. 0 on a dry-run (no ledger).
        settled_skipped=ledger.newly_settled_skipped if ledger is not None else 0,
        # Vendor-behaviour honesty counters (DEC-VENDOR-CONFLICT-SURFACE-001):
        # conflicts/dup-drops SUM across passes (keep-first sink — a re-pulled
        # round re-records only what it re-touched); the delta MAX-folds.
        conflicts=sum(p.conflicts for p in passes),
        vendor_dup_rows_dropped=sum(p.vendor_dup_rows_dropped for p in passes),
        max_close_delta=phase_max_delta,
    )


def _resolve_minute_windows(
    *,
    rebuilt: Sequence[_RebuiltContract],
    explicit_dates: bool,
    start: date,
    end: date,
) -> dict[str, tuple[date, date]]:
    """Resolve each contract's half-open ``[start, end)`` 1-minute fetch window — pure.

    DEC-1MINUTE-ONLY-DERIVE-1DAY-001 / DEC-FUTURES-LOOKBACK-60D-001. When the operator
    passed explicit from/to dates (*explicit_dates* True) EVERY contract uses the
    resolved ``[start, end)`` envelope verbatim (the dates OVERRIDE — the operator can
    still pull ANY range they name). Otherwise each contract gets its OWN per-contract
    DEFAULT window — SPLIT by contract type: a FUTURE (``FUT``) defaults to
    ``expiry − 60 days → expiry+1day`` and an OPTION (``CE`` / ``PE``) to the tighter
    ``expiry − 30 days → expiry+1day`` — clamped into ``[start, end)``
    (:func:`~brutex.adapters.groww.contract_bounds.default_fno_minute_window`, passed
    ``is_option=contract.contract_type in {"CE", "PE"}``) — so the 1-minute fill
    targets the recent life of each contract, not blindly from 2020. A contract with no
    ``expiry`` falls back to the full range (the helper's never-skip choice).

    **NOT-YET-OPEN contracts are OMITTED (DEC-FNO-FAR-EXPIRY-WINDOW-001 — operator
    law):** a far-future contract whose strict ``[expiry − lookback, expiry]`` window
    has not intersected the envelope yet (the helper returns ``None``) gets NO map
    entry — the caller excludes it from the pull entirely (no plan row, no fetch,
    zero vendor calls) until the calendar reaches its ``opens_on`` date. Each skip is
    logged loudly (``fno.window_not_open`` with the contract, expiry and ``opens_on``)
    so the operator can SEE why the contract is absent. Pure + O(contracts); no lake
    I/O (the log is the only side effect).

    Returns
    -------
    dict[str, tuple[date, date]]
        ``{contract.symbol: (start, end)}`` for every contract whose window is OPEN
        (always every contract when *explicit_dates* is True); a not-yet-open
        contract is absent.
    """
    windows: dict[str, tuple[date, date]] = {}
    for _row, contract in rebuilt:
        if explicit_dates:
            windows[contract.symbol] = (start, end)
            continue
        is_option = contract.contract_type in {"CE", "PE"}
        window = default_fno_minute_window(
            contract.expiry,
            full_start=start,
            full_end=end,
            is_option=is_option,
        )
        if window is None:
            # The helper returns None ONLY for a real expiry (a None expiry maps to
            # the full range), so opens_on is always computable here; the ternaries
            # keep the type-checkers honest without an unreachable assert.
            _LOG.info(
                "fno.window_not_open",
                contract=contract.symbol,
                expiry=contract.expiry.isoformat() if contract.expiry is not None else None,
                opens_on=(
                    fno_window_opens_on(contract.expiry, is_option=is_option).isoformat()
                    if contract.expiry is not None
                    else None
                ),
            )
            continue
        windows[contract.symbol] = window
    return windows


@dataclass(frozen=True, slots=True)
class _GreeksEnrichOutcome:
    """The honest outcome of one post-pull Greeks-enrichment pass.

    DEC-GREEKS-ENRICH-OBSERVE-001 — the tiny value the fully-guarded
    :func:`_enrich_greeks_from_minute` wrapper returns so
    :func:`pull_contract_candles` can surface the pass on the report + note
    instead of one swallowed log line.

    Attributes
    ----------
    ok
        ``True`` = the pass COMPLETED (per-target quarantines ride
        :attr:`errored_targets`, they never flip ``ok`` — one corrupt contract
        must not read as a whole-pass failure); ``False`` = the WHOLE pass
        failed (see :attr:`error`); ``None`` = the pass did not run (dry-run).
    error
        ``f"{type(exc).__name__}: {exc}"`` on a whole-pass failure; ``None``
        otherwise.
    errored_targets
        The driver's per-target quarantine tally (the success branch's
        ``report.errored``); ``0`` on a whole-pass failure / not-run.
    """

    ok: bool | None
    error: str | None
    errored_targets: int


#: The "pass did not run" outcome (dry-run — nothing was written to enrich).
_GREEKS_NOT_RUN: Final[_GreeksEnrichOutcome] = _GreeksEnrichOutcome(
    ok=None, error=None, errored_targets=0
)

#: The strict-barrier WITHHELD outcome (DEC-PULL-SIX-STEP-ORDER-001): under
#: ``BRUTEX_PIPELINE_STRICT_GATE=1`` an unclean prior step (spot/metadata —
#: via the threaded ``PriorStepsVerdict``) or an unclean F&O phase withholds
#: the enrichment pass. ``ok=None`` — the pass did NOT run; never a FAILED
#: verdict (nothing is lost: the overlay is re-derivable and the next pull
#: re-attempts it — the #1188 value-check reuse gate recomputes correctly).
#: The note names it via the ``greeks_strict_withheld`` clause, never silence.
_GREEKS_STRICT_WITHHELD: Final[_GreeksEnrichOutcome] = _GreeksEnrichOutcome(
    ok=None, error=None, errored_targets=0
)


def _heal_lake_after_pull(
    *,
    rebuilt: Sequence[_RebuiltContract],
    windows: dict[str, tuple[date, date]],
    lake_root: Path,
) -> None:
    """L2 lake heal over the just-pulled F&O contracts (DEC-LAKE-HEAL-001).

    The F&O twin of the dashboard spot pull's ``_heal_after_pull`` glue: one
    COMMON :func:`~brutex.data.lake_heal.heal_after_pull` call over the
    rebuilt contracts' ``(exchange, "FNO", groww_symbol)`` coordinates,
    bounded to the UNION of their resolved minute-window spans (a months
    SUPERSET of every per-contract span — bounded, never the whole lake).
    Runs BEFORE :func:`_enrich_greeks_from_minute` (and before the pipeline's
    final prebuild phase — DEC-PIPELINE-AUTOPREBUILD-001 — which derives the
    intraday TFs + ``1day``) so every downstream derivation reads clean
    1-minute. A contract with no resolved window, or whose exchange is
    unresolvable, contributes no target; zero targets is a no-op.
    :class:`~brutex.data.errors.LakeWriteHalt` RE-RAISES (a disk-full heal
    halts the pull exactly like any lake write); every OTHER error is
    swallowed + logged ``lake_heal.post_pull_error`` (a heal bug never fails
    a successful pull — the ``_enrich_greeks_from_minute`` guard convention).
    The catalog at ``<lake>/catalog.duckdb`` is opened best-effort WHEN THE
    FILE EXISTS (the L3 sweeper's convention, via the COMMON
    :func:`~brutex.data.lake_heal.open_lake_catalog`) so a healed partition's
    rotated ``raw_hash`` reaches the catalog immediately — an open failure
    logs + proceeds with ``catalog=None``, never blocking the pull.
    """
    from brutex.data.errors import LakeWriteHalt
    from brutex.data.lake_heal import heal_after_pull, lake_heal_audit, open_lake_catalog
    from brutex.notifications.sweep_alerts import emit_lake_heal_alert
    from brutex.observability.correlation import current_pull_id

    targets: list[tuple[str, str, str]] = []
    spans: list[tuple[date, date]] = []
    for _row, contract in rebuilt:
        window = windows.get(contract.symbol)
        if window is None:
            continue
        try:
            exchange = contract_exchange(contract)
        except ValueError:
            # Also skipped (same guard) by ``_enrich_greeks_from_minute``.
            continue
        targets.append((exchange, OPTION_SEGMENT, contract.symbol))
        spans.append(window)
    if not targets:
        return
    scope = f"pull:{current_pull_id()}"
    try:
        with lake_heal_audit(lake_root) as audit:
            catalog = open_lake_catalog(lake_root)
            try:
                heal_after_pull(
                    lake_root=lake_root,
                    targets=targets,
                    start=min(win_start for win_start, _win_end in spans),
                    end=max(win_end for _win_start, win_end in spans),
                    scope=scope,
                    catalog=catalog,
                    audit=audit,
                    on_report=lambda report: emit_lake_heal_alert(report, scope=scope),
                )
            finally:
                if catalog is not None:
                    catalog.close()
    except LakeWriteHalt:
        raise
    except Exception as exc:  # pylint: disable=broad-exception-caught — never fail the pull
        _LOG.warning("lake_heal.post_pull_error", scope=scope, error=str(exc))


def _enrich_greeks_from_minute(
    *,
    rebuilt: Sequence[_RebuiltContract],
    windows: dict[str, tuple[date, date]],
    lake_root: Path,
) -> _GreeksEnrichOutcome:
    """Post-pull Greeks enrichment over the just-written option 1-minute — best-effort.

    ``greeks_enrich_after_pull`` (DEC-GREEKS-ENGINE-PR2B-001 /
    `DEC-GREEKS-GENERATION-TIME-001`, plan §15.4): the F&O Greeks pass at the
    post-drain seam (the fetch lane has fully drained — the pass can never
    block or slow a fetch). It once shared this seam with the (now removed)
    mid-pull ``_derive_daily_from_minute`` daily derive — the ``1day`` derive
    was relocated into the pipeline's automatic FINAL prebuild stage
    (DEC-PIPELINE-AUTOPREBUILD-001); the Greeks overlay stays HERE because it
    annotates the just-pulled 1-minute bars themselves.
    One :class:`~brutex.data.greeks_enrich.GreeksEnrichTarget` per resolved
    contract window; FUT legs are skipped INSIDE the driver (never a bogus
    Greek for a future); the risk-free source is the locked PR-1 seam
    (:func:`~brutex.data.greeks_enrich.resolve_risk_free_source` — fixed 6.5%
    default, ``BRUTEX_RISK_FREE_CSV`` override).

    The body is guarded (plan §15.4) with TWO deliberate exceptions
    (DEC-GREEKS-PARALLEL-001, the #1188 follow-up (a)): a
    :class:`~brutex.data.errors.LakeWriteHalt` (disk full / OOM at a lake
    write) and a :class:`~brutex.data.errors.GreeksEnrichPoolBroken` (a
    Greeks pool worker died abruptly — a host-resource condition) RE-RAISE so
    they reach :meth:`~brutex.dashboard.pull_jobs.BackgroundPullJob._run`'s
    halt handling and fire the CRITICAL Telegram pull-halt alert. EVERY other
    failure — a misconfigured rate CSV, a missing wheel, an unexpected driver
    error — logs ONE ``fno.greeks_enrich.error`` and returns; those can never
    fail the pull. The authoritative 1-minute candles are already durable in
    the lake; the Greeks overlay is a re-derivable convenience layer the NEXT
    pull re-attempts (stamp-driven, idempotent — Golden Rule #7/#10).
    Per-contract errors are additionally quarantined inside the driver, so
    one bad contract never sinks the pass.

    Returns
    -------
    _GreeksEnrichOutcome
        The honest pass verdict (DEC-GREEKS-ENRICH-OBSERVE-001): ``ok=True`` +
        the driver's per-target ``errored`` tally when the pass COMPLETED
        (per-target quarantines never flip ``ok``), or ``ok=False`` +
        ``"ErrorType: message"`` when the whole pass failed — surfaced on the
        report/note by the caller, never only a log line.
    """
    # Wheel-free at function top (brutex.data.errors imports only brutex.models);
    # the greeks_enrich import STAYS inside the try — a missing trading_core
    # wheel must keep hitting the ok=False fence, never crash the pull.
    from brutex.data.errors import GreeksEnrichPoolBroken, LakeWriteHalt

    try:
        from brutex.data.greeks_enrich import (
            GreeksEnrichTarget,
            enrich_greeks_for_targets,
            resolve_risk_free_source,
        )

        rate_source = resolve_risk_free_source()
        targets: list[GreeksEnrichTarget] = []
        for _row, contract in rebuilt:
            window = windows.get(contract.symbol)
            if window is None:
                continue
            try:
                exchange = contract_exchange(contract)
            except ValueError:
                # Already surfaced by the daily derive's `contract_skipped` log.
                continue
            targets.append(
                GreeksEnrichTarget(
                    exchange=exchange,
                    symbol=contract.symbol,
                    start=window[0],
                    end=window[1],
                )
            )
        report = enrich_greeks_for_targets(
            targets=targets, lake_root=lake_root, rate_source=rate_source
        )
        _LOG.info(
            "fno.greeks_enrich.done",
            enriched=report.enriched,
            already_current=report.already_current,
            futures_skipped=report.futures_skipped,
            undecodable=report.undecodable,
            no_bars=report.no_bars,
            errored=report.errored,
            bars_written=report.bars_written,
            bars_solved=report.bars_solved,
            bars_null=report.bars_null,
            bars_reused=report.bars_reused,
            spot_reads=report.spot_reads,
            spot_hits=report.spot_hits,
            bars_skipped=report.bars_skipped,
        )
        return _GreeksEnrichOutcome(ok=True, error=None, errored_targets=report.errored)
    except (LakeWriteHalt, GreeksEnrichPoolBroken):
        # A pull-wide HOST condition (disk full / OOM / a dead pool worker) —
        # re-raised past the fence so it reaches BackgroundPullJob._run's halt
        # handling + the CRITICAL Telegram alert (DEC-GREEKS-PARALLEL-001; the
        # _heal_lake_after_pull convention). Everything else stays fenced below.
        raise
    except Exception as exc:
        _LOG.error("fno.greeks_enrich.error", error=str(exc))
        return _GreeksEnrichOutcome(
            ok=False, error=f"{type(exc).__name__}: {exc}", errored_targets=0
        )


def pull_contract_candles(
    contracts: Sequence[ContractRow],
    *,
    lake_root: Path,
    dry_run: bool,
    intervals: Sequence[str],
    registry: RegistryStore,
    from_date: date | None = None,
    to_date: date | None = None,
    now: datetime | None = None,
    client_factory: HistoricalClientFactory | None = None,
    on_progress: ProgressHook | None = None,
    session_rate: SessionRateController | None = None,
    until_clean: bool = False,
    until_clean_rounds: int = AUTO_RESUME_MAX_ROUNDS,
    run_parallel: RunParallel | None = None,
    scope_start: date | None = None,
    scope_end: date | None = None,
    on_bars: Callable[[int], None] | None = None,
    on_ledger_sync: OnLedgerSync | None = None,
    underlying_kinds: Mapping[str, bool] | None = None,
    step_monitor: StepOrderMonitor | None = None,
    prior_steps: PriorStepsVerdict | None = None,
) -> HistoricalPullReport:
    """Download OHLC candles for the SELECTED registry *contracts* — Path A, no re-enumeration.

    Rebuilds each :class:`~brutex.data.registry_store.ContractRow` into a
    :class:`~brutex.adapters.protocol.DerivativeContract` (via
    :func:`~brutex.adapters.groww.contracts.parse_contract_symbol`) and feeds that RAW
    contracts list to :func:`~brutex.adapters.groww.option_ingest.run_groww_option_ingest`
    — so it pulls the SPECIFIC contracts WITHOUT re-enumerating the chain (the metadata is
    already cached in the registry). The candles land on the verified candle lake
    (``bars/<exch>/FNO/<symbol>/<interval>/`` via :class:`~brutex.data.sink.ParquetBarSink`
    — PATH A), NOT the ``options/`` OptionBar lake (PATH B needs ``iv``/``spot``/``ATM±N``
    that ``get_historical_candles`` does not return — fabricating them would violate Golden
    Rule #1). AFTER a contract's 1-minute phase lands CLEAN its registry
    :meth:`~brutex.data.registry_store.RegistryStore.set_candle_status` is set to
    ``complete``.

    **1-minute ONLY, 1-day DERIVED (DEC-1MINUTE-ONLY-DERIVE-1DAY-001).** For F&O the pull
    is a SINGLE vendor phase (the prior two-phase 1-day-first barrier is RETIRED):

    1. **Per-contract window.** When the operator passes NO explicit from/to dates, each
       contract's 1-minute window DEFAULTS to ``expiry − 30 days → expiry`` (its
       own expiry; ``expiry+1day`` end-exclusive so the expiry day's intraday bars land),
       clamped into the resolved envelope. Explicit operator dates OVERRIDE — every contract
       then uses the resolved ``[start, end)`` range verbatim.
    2. **One vendor phase — 1-minute ONLY.** Every selected contract's ``1minute`` is pulled
       over its window. ``1day`` is NEVER fetched from the vendor.
    3. **1-day DERIVED.** After the 1-minute lands, the 1-day partition is DERIVED by
       resampling each contract's just-written 1-minute to midnight-IST daily bars
       (:func:`~brutex.data.spot_daily_resample.fill_daily_from_minute`) and writing the
       ``FNO/<symbol>/1day`` partition. The derivation is QUARANTINED — it can NEVER fail the
       pull (the authoritative 1-minute candles are already in the lake).

    With ``until_clean=True`` (DEC-GROWW-DERIV-UNTIL-CLEAN-001) the single 1-minute phase runs
    BOUNDED re-pull rounds over its SHARED WindowLedger until the transient residual drains,
    the token dies, or the budget / a no-progress round stops it
    (:func:`~brutex.adapters.ingest_common.run_until_clean`, ``stop_when_stuck=True``).
    Each round re-ingests the SAME fixed contract list; the ledger SKIPS already-`SUCCESS`
    windows and RE-ATTEMPTS the prior round's `FAILED` (transient) ones. Default
    ``until_clean=False`` is one single pass.

    Honest envelope (Golden Rule #10): an EMPTY *contracts* selection appends 0 bars + an
    honest note (never an invented contract). The per-contract fetch tiles each range
    (network-bound, NOT O(1)-total) — the per-contract default window OPTIMISES the 1-minute
    set (the recent life of each contract), it does NOT make the pull O(1)-total. Unlike the
    chain pull, a round here is ``O(failed windows)`` — the contract list is fixed (no
    re-enumeration). Re-runs are rate-safe (the SAME 500/min bucket) + idempotent (the
    keep-first sink + WindowLedger).

    Parameters
    ----------
    contracts
        The selected registry rows whose candles to pull (single / chain / underlying /
        all); EMPTY ⇒ an honest 0-bar no-op.
    lake_root
        The lake root the Path-A candle sink writes under.
    dry_run
        When ``True``, the default factory uses the honest empty-candle client (0 bars,
        never a fabricated OHLC) + a :class:`NullBarSink`.
    intervals
        The operator's requested intervals — accepted + echoed in the report, but IGNORED
        for granularity: F&O always fetches ``1minute`` ONLY and DERIVES ``1day``
        (DEC-1MINUTE-ONLY-DERIVE-1DAY-001).
    registry
        The registry whose ``candle_status`` is marked ``complete`` per landed contract.
    from_date, to_date
        Optional inclusive date bounds; default ``[2020-01-01, previous day]``.
    now
        Injected clock (defaults to ``datetime.now(UTC)``).
    client_factory
        Injected ``(lake_root) -> (client, sink)`` (tests pass a fake); ``None`` selects
        the dry-run / live default.
    on_progress
        Per ``(contract, interval)`` progress hook ``(label, bars, failed)``.
    session_rate
        The dashboard's ``SessionRateController`` (live pacing); ``None`` offline.
    until_clean
        When ``True``, run bounded re-pull rounds until the transient residual drains
        (default ``False`` — one single pass).
    until_clean_rounds
        The OUTER round budget for ``until_clean`` (default
        :data:`~brutex.adapters.ingest_common.AUTO_RESUME_MAX_ROUNDS`); ``<= 0`` runs
        zero rounds (a safe no-op).
    run_parallel
        The cross-contract concurrency primitive forwarded to
        :func:`~brutex.adapters.groww.option_ingest.run_groww_option_ingest`
        (DEC-GROWW-OPTION-POOL-001). ``None`` (the default) derives the live
        :class:`~brutex.orchestrator.parallel.PoolRunParallel` from the session
        controller (:func:`_option_run_parallel`) — a dry-run / no-controller pull then
        keeps the byte-identical serial default; tests inject a synchronous fake.
    scope_start, scope_end
        OPTIONAL inclusive EXPIRY-scope bounds (DEC-FNO-EXPIRY-SCOPE-001): only
        contracts whose expiry lies within ``[scope_start, scope_end]`` are
        pulled; an out-of-scope expiry is skipped ENTIRELY — zero vendor calls,
        zero ledger rows (the partition runs strictly BEFORE any planning), one
        honest ``fno.expiry_out_of_scope`` log per ``(underlying, expiry)``
        group, stored bars untouched. The scope selects WHICH expiries — it
        NEVER reshapes windows (in-scope contracts keep the normal 30d/60d
        default window law over the standard envelope). ``None``/``None`` (the
        default) is byte-identical to the unscoped pull. Distinct from
        *from_date*/*to_date*, which reshape every contract's fetch WINDOW.
    on_bars
        Optional LIVE bars-delta hook ``(bars_appended_delta) -> None``
        (DEC-PULL-PROGRESS-HONESTY-001, the frozen-bars fix). Fired once per
        durable window from BOTH modes without double-counting: in ASYNC mode the
        fetch pipeline's background consumer invokes it per
        :meth:`~brutex.adapters.groww.fetch_pipeline.FetchPipeline.fold_totals`
        (the producer-side per-pair report carries 0 bars); in SYNC mode
        (kill-switch / no pipeline) the per-pair progress wrapper forwards the
        pair's REAL report bars (and in async mode that same forward adds the
        honest 0). Counts bars PROCESSED (sink-append input, the report's
        ``bars_appended`` semantics), not net-new lake rows. ``None`` (default)
        is byte-identical.
    on_ledger_sync
        Optional THROTTLED absolute-progress callback
        ``(resolved, total, bars, failed, current) -> None``
        (DEC-PULL-PROGRESS-HONESTY-001, the honest-denominator fix): fed from the
        pull's own WindowLedger via the COMMON
        :func:`~brutex.dashboard.pull_controller.make_live_sync`, FILTERED to this
        pull's F&O scope (``groww-option`` / ``groww-future`` × ``1minute``) so the
        cross-run, cross-market ledger's spot rows never pollute the denominator.
        Honest envelope: the F&O denominator still GROWS mid-phase (contracts are
        planned per market bucket), so the dashboard marks the phase *open_ended* —
        this feeds real moving counts, it does NOT claim a final total. ``None``
        (default — CLI / tests) is byte-identical.
    underlying_kinds
        Optional ``{registry-underlying-key: is_index}`` classification map
        (DEC-PULL-SIX-STEP-ORDER-001) — keyed by the SAME
        ``primary_fno_underlying``-resolved key the metadata phase persisted
        :class:`~brutex.data.registry_store.ContractRow.underlying` under (the
        composite builds it via ``_underlying_kind_map``). Drives the strict
        steps 3-6 ordering (INDICES-FUTURES → STOCKS-FUTURES → INDICES-OPTIONS
        → STOCKS-OPTIONS); an unresolved underlying classifies STOCK-side
        (fail-closed for ORDER, never for DATA — still pulled, WARNed +
        counted). ``None`` (a legacy caller — the CLI, tests) degrades
        byte-identically to the legacy 2-bucket FUT-then-OPT order; BOTH
        production dashboard routes now thread the map (the composite via
        ``_underlying_kind_map(tradable)``, the direct-selection
        ``/pull-historical`` grammars via the whole-universe map — the review
        fix closing the route gap that reproduced the 2026-07-05 inversion).
    step_monitor
        Optional :class:`~brutex.adapters.ingest_common.StepOrderMonitor`
        (DEC-PULL-SIX-STEP-ORDER-001). ``None`` (every production caller)
        builds a fresh observe-only monitor — ordering violations are logged +
        counted, never raised; tests inject ``raise_on_violation=True`` as the
        hard ordering oracle.
    prior_steps
        Optional :class:`~brutex.dashboard.pipeline_verdict.PriorStepsVerdict`
        (DEC-PULL-SIX-STEP-ORDER-001) — the sequencer's spot + metadata
        cleanliness + the resolved strict flag, threaded through the
        composite so the Greeks seam here can enforce the literal strict
        barrier: with ``prior_steps.strict`` and ANY uncleanliness (a prior
        step OR this F&O phase), the enrichment pass is WITHHELD
        (``pipeline.greeks.strict_skipped`` WARN; the note reads "withheld
        (strict gate) — re-attempted next pull", never FAILED — the overlay
        is re-derivable, the next pull re-attempts). ``None`` (every
        non-pipeline caller — the /db selection route, tests) keeps today's
        best-effort behavior byte-identical.
    """
    now = now or datetime.now(UTC)
    start, end = _resolve_range(from_date=from_date, to_date=to_date, now=now)
    pace, on_rate_limit, on_timeout, on_success = _rate_hooks(session_rate, dry_run=dry_run)
    if client_factory is None:
        client_factory = _resolve_client_factory(
            dry_run=dry_run, pace=pace, on_rate_limit=on_rate_limit, on_success=on_success
        )
    # The LIVE pull fans contracts across the rate-sized PoolRunParallel over the
    # ClientPool the live factory builds (8-RPS parity, DEC-GROWW-OPTION-POOL-001); a
    # dry-run / no-controller pull leaves it serial (deterministic). An explicit
    # *run_parallel* (a test fake) always wins.
    if run_parallel is None:
        run_parallel = _option_run_parallel(dry_run=dry_run, session_rate=session_rate)
    client, sink = client_factory(lake_root)
    # Register the F&O client pool's shared MEASURED-rate meter into the session controller
    # (DEC-RATE-WIDGET-FNO-MEASURED-001) so the observe-only RatePanelSampler — which holds
    # only the session controller, NOT this deep-in-the-spine pool — can read the transport
    # CallMeter's real ``effective_rps`` during the long F&O phase and push the TRUE completed
    # rate (instead of the admission average of DEC-RATE-WIDGET-FNO-HONEST-001). The
    # empty-candle dry-run GrowwClient DOES carry a real CallMeter (``client.py``
    # ``default_factory=CallMeter``), so honesty comes from the explicit ``dry_run=True``
    # gate, which DEREGISTERS (clears any stale prior-pull meter, never registers the
    # synthetic one) — never from a meterless client (the pre-#1534 "no ``.meter`` silent
    # no-op" claim here was factually FALSE; DEC-RATE-METER-LIFECYCLE-001). Best-effort +
    # observe-only. The lazy import avoids any module-load ordering coupling with
    # pull_controller.
    from brutex.dashboard.pull_controller import register_pull_meter

    register_pull_meter(session_rate, client, dry_run=dry_run)
    # FULL async fetch/process separation (DEC-GROWW-FETCH-ASYNC-SEPARATION-001) for the F&O
    # (futures + options) pull — the F&O TWIN of the spot spine's wiring
    # (DEC-FNO-ASYNC-PIPELINE-WIRE-001). When built (DEFAULT ON; ``None`` for a dry-run / the
    # ``BRUTEX_GROWW_FETCH_PIPELINE=off`` kill-switch / a construction failure), each contract's
    # SUCCESS-path tail (session-filter + ``sink.append`` + conflict-aggregate + ``ledger.record``)
    # is handed OFF to a background consumer so the fetch lane does ONLY fetch + enqueue —
    # internal processing can never block or slow the fetch. The RateController (per-second
    # ceiling + 500/min governor) stays entirely producer-side, UNTOUCHED (CLAUDE.md §2 Rule 6),
    # so it lets the connection pool USE the allowed headroom rather than idling ~75% on its own
    # serial write tail. Built + drained via the COMMON helpers in
    # brutex.adapters.groww.pull_pipeline (one wiring surface, CLAUDE.md §11 COMMON); the lazy
    # import avoids a module-load cycle.
    from brutex.adapters.groww.pull_pipeline import (
        build_fetch_pipeline as _build_fetch_pipeline,
    )
    from brutex.adapters.groww.pull_pipeline import (
        build_write_consumer as _build_write_consumer,
    )
    from brutex.adapters.groww.pull_pipeline import (
        drain_fetch_pipeline as _drain_fetch_pipeline,
    )
    from brutex.adapters.groww.pull_pipeline import (
        drain_write_consumer as _drain_write_consumer,
    )

    # The live-bars hook rides the pipeline's fold seam (DEC-PULL-PROGRESS-HONESTY-001):
    # the background consumer folds each window's REAL durable bar count, so invoking
    # on_bars there is the ONLY per-window truth in async mode (the producer-side
    # per-pair report honestly carries 0 bars).
    fetch_pipeline = _build_fetch_pipeline(dry_run=dry_run, lake_root=lake_root, on_bars=on_bars)
    # The fetch_pipeline SUBSUMES the Lever-1 write-only decouple (DEC-FETCH-WRITE-OVERLAP-LEVER1-001):
    # it owns the ENTIRE tail (incl. ``sink.append``), so the batched ``write_pool`` is built ONLY when
    # the pipeline is OFF (the kill-switch / dry-run path) — never BOTH. This makes the pipeline's
    # consumer the SINGLE writer, so there is no double-write / lost-write race and no redundant
    # double background hop even when ``BRUTEX_WRITE_BATCH`` is also set (the documented "ONE or the
    # other" contract, brutex/adapters/groww/run_ingest.py:347). With the pipeline ON the synchronous
    # ``write_pool`` is ``None`` so the per-contract write rides the pipeline consumer; with it OFF the
    # existing Lever-1 batched-write fallback is byte-identical.
    write_pool = (
        None if fetch_pipeline is not None else _build_write_consumer(sink, dry_run=dry_run)
    )
    # Expiry-level date scoping (DEC-FNO-EXPIRY-SCOPE-001): partition the selection
    # STRICTLY BEFORE the contract rebuild + window planning, so an out-of-scope
    # expiry never reaches plan_contract_windows (zero PLANNED ledger rows — a stray
    # row would make every later pull re-attempt it) and never costs a vendor call.
    # The default (None, None) scope keeps every contract — byte-identical to before.
    # Stored lake bars for a scoped-out expiry are NEVER touched.
    scoped = partition_contracts_by_scope(contracts, scope_start=scope_start, scope_end=scope_end)
    log_out_of_scope_expiries(scoped, scope_start=scope_start, scope_end=scope_end)
    # Observe-only in-scope summary (DEC-TELEMETRY-SCOPE-BUDGET-RUNCOUNTS-001): ONE
    # ``fno.expiry_scope_summary`` INFO naming BOTH sides of the partition + the bounds,
    # emitted ONLY when a from/to bound is actually set — the default (None, None) pull
    # emits nothing (the #1146 default-parity pin, logging included).
    log_expiry_scope_summary(scoped, scope_start=scope_start, scope_end=scope_end)
    rebuilt = _rebuild_contracts(scoped.in_scope)

    # Per-contract 1-minute window (DEC-1MINUTE-ONLY-DERIVE-1DAY-001): explicit operator
    # dates OVERRIDE (every contract uses the resolved range); else each contract's DEFAULT
    # ``expiry − lookback → expiry+1day`` clamped into range. ``intervals`` is accepted + echoed
    # but IGNORED for granularity — F&O always fetches 1-minute only. Resolved FIRST because
    # the strict window law (DEC-FNO-FAR-EXPIRY-WINDOW-001) EXCLUDES a not-yet-open contract
    # from the pull entirely: it must not reach the ingest at all (a map-absent symbol would
    # otherwise fall back to the FULL range inside ``_resolve_window`` — the exact full-history
    # over-pull the law forbids).
    explicit_dates = from_date is not None or to_date is not None
    minute_windows = _resolve_minute_windows(
        rebuilt=rebuilt, explicit_dates=explicit_dates, start=start, end=end
    )
    # The pull workload = ONLY the contracts whose window is OPEN (every contract under
    # explicit dates). A not-yet-open contract gets no plan row, no fetch, zero vendor
    # calls and zero ledger rows this pull — it joins automatically once the calendar
    # reaches its ``opens_on`` (each skip was logged by the resolver above).
    in_window = [(row, contract) for row, contract in rebuilt if contract.symbol in minute_windows]
    contracts_not_yet_in_window = len(rebuilt) - len(in_window)
    selected_symbols = frozenset(row.contract_symbol for row, _contract in in_window)
    # Option + future legs ride DISTINCT ledger namespaces (DEC-GROWW-FUTURE-INGEST-001 —
    # `_contract_market` untouched, every ledger row byte-identical); the ORDERING key
    # additionally splits each market by index/stock class (DEC-PULL-SIX-STEP-ORDER-001)
    # so the buckets run in the decreed steps 3→4→5→6.
    if underlying_kinds is None:
        # A direct/legacy caller passed no classification map: everything classifies
        # stock-side WITHOUT per-underlying noise — the order degrades to exactly the
        # legacy 2-bucket FUT-then-OPT law, byte-identical; announced once per pull.
        _LOG.info("fno.step_kinds_absent")
    by_bucket: dict[_BucketKey, list[_RebuiltContract]] = {}
    class_unresolved = 0
    unresolved_warned: set[str] = set()
    for row, contract in in_window:
        bucket_key, resolved = _contract_bucket(row, underlying_kinds)
        if not resolved:
            # Fail-closed for ORDER, never for DATA: the leg is STILL pulled, just
            # ordered stock-side (an unknown can never jump ahead of a mandated
            # index step); ONE WARN per underlying + an honest report count.
            class_unresolved += 1
            if row.underlying not in unresolved_warned:
                unresolved_warned.add(row.underlying)
                _LOG.warning("fno.step_unclassified", underlying=row.underlying)
        by_bucket.setdefault(bucket_key, []).append((row, contract))
    _step_monitor = step_monitor if step_monitor is not None else StepOrderMonitor()

    # Per-contract EMPTY-retry budget (DEC-FETCH-PER-WINDOW-GUARANTEE-001): a confirmed-deep
    # far-OTM strike (outside ITS (underlying, expiry) chain's median ± band — keyed PER
    # underlying so a mixed all-cached pull never pools two underlyings sharing one monthly
    # expiry into a bogus median) gets a REDUCED empty-retry budget, so a dead strike costs ONE
    # fetch to confirm empty instead of three — driving fetches-per-window toward ~1.0. NO data
    # is lost (the first non-empty fetch wins). The budget is sparse (only the deep strikes), so
    # an in-band option / every future keeps the full budget. Pure + O(chain), no network/lake I/O.
    empty_retry_budget = build_empty_retry_budget(contract for _row, contract in in_window)
    empty_retry_by_symbol = dict(empty_retry_budget.by_symbol) or None

    # ── The SINGLE vendor phase — 1-minute ONLY (1-day is derived, not fetched) ─────────
    # Golden Rule #8 (DEC-INGEST-AUDIT-WIRING-001): the F&O twin of the spot pull's
    # hash-chained ingest audit — the ONE COMMON factory opens
    # ``<lake>/audit/fno-ingest.jsonl`` (resume = continue the existing chain) for a
    # REAL pull and a null context for a dry-run (no real data → no audit), and the
    # spine appends RUN_START → per-pair → RUN_COMPLETE on its controller drain
    # thread. Held open across the whole phase (every until-clean round + step
    # bucket rides one chain), closed after the drains below.
    with open_ingest_audit(lake_root, name="fno-ingest", enabled=not dry_run) as ingest_audit:
        try:
            minute = _run_phase(
                interval=_MINUTE_INTERVAL,
                by_bucket=by_bucket,
                selected_symbols=selected_symbols,
                step_monitor=_step_monitor,
                client=client,
                sink=sink,
                lake_root=lake_root,
                dry_run=dry_run,
                start=start,
                end=end,
                pace=pace,
                on_rate_limit=on_rate_limit,
                on_timeout=on_timeout,
                on_success=on_success,
                on_progress=on_progress,
                session_rate=session_rate,
                until_clean=until_clean,
                until_clean_rounds=until_clean_rounds,
                contract_windows=minute_windows,
                empty_retry_by_symbol=empty_retry_by_symbol,
                run_parallel=run_parallel,
                write_pool=write_pool,
                fetch_pipeline=fetch_pipeline,
                audit=ingest_audit,
                on_bars=on_bars,
                on_ledger_sync=on_ledger_sync,
            )
        finally:
            # Drain the async work BEFORE the 1-day derivation reads the just-written 1-minute
            # lake (every settled window must be durable first) and BEFORE returning (so the
            # caller's ledger persist sees a fully-flushed lake). Drain the fetch_pipeline FIRST
            # (it owns the tail incl. ``sink.append`` → ``ledger.record``), then the write_pool —
            # mirroring the #951 MAJOR-4 drain-order fix (the producer must flush before the
            # writer). Only ONE is ever non-``None`` (they are mutually exclusive above), so this
            # is a single real drain; both helpers are best-effort + a no-op on ``None``.
            _drain_fetch_pipeline(fetch_pipeline)
            _drain_write_consumer(write_pool)

    # Fold the async pipeline's post-drain per-series totals back into the phase result
    # (DEC-FNO-ASYNC-PIPELINE-WIRE-001 completion-count fix): in async mode the producer returned
    # ``bars_appended == 0`` per pair (the consumer did the durable write on a background thread)
    # and a tail-side transient was folded onto the pipeline, so without this the report would
    # read 0 bars and a tail-transient-deferred contract would be falsely marked complete. The
    # pipeline is already DRAINED above, so its totals are final; a no-op (byte-identical) on the
    # synchronous path when ``fetch_pipeline`` is ``None``.
    minute = _reconcile_phase_with_pipeline(
        minute, fetch_pipeline, selected_symbols=selected_symbols
    )

    # Observe-only whole-underlying silent-empty detector (DEC-EMPTY-DETECTOR-PRODUCTION-WIRING-001
    # — completing DEC-SUSPICIOUS-EMPTY-FLAG-001 #919): surface a whole F&O underlying whose every
    # cleanly-fetched contract came back EMPTY (the BAJAJ-AUTO/M&M silent-vanish case) as ONE
    # ``pull.underlying_all_empty`` WARNING for human triage. The in-driver scan inside
    # ``run_groww_option_ingest`` SKIPS when a ``fetch_pipeline`` is wired (the producer reports 0
    # bars before the background consumer folds the real counts), and the PRODUCTION F&O pull ALWAYS
    # wires one — so the detector was DEAD in every real pull. Running it HERE, after the pipeline
    # drain + ``_reconcile_phase_with_pipeline`` has folded the REAL per-pair ``bars_appended`` back
    # into ``minute.merged_pairs``, is the only point those durable counts exist. Pipeline-only (the
    # ``fetch_pipeline is None`` synchronous path already emitted in-driver — a second emit here would
    # double-warn). Reuses the SAME COMMON ``emit_underlying_all_empty`` (fully guarded — can NEVER
    # break the pull). Observe-only: no return/ledger/engine change; ENGINE stays NIFTY/BANKNIFTY/SENSEX.
    if fetch_pipeline is not None:
        # ``failed_symbols`` (DEC-SILENT-VANISH-LOWLEG-001): the phase's terminally
        # quarantined contract symbols, so the LOW-LEG Tier B rule can disqualify a
        # 1-2-leg underlying with a quarantined sibling (quarantines never enter pairs).
        # ``never_planned_symbols`` (DEC-SILENT-VANISH-LOWLEG-ANNOTATE-001): exactly
        # the resolver's omitted not-yet-open set (``rebuilt − minute_windows``, the
        # ``fno.window_not_open`` skips), so a Tier-B low-leg WARN self-describes via
        # ``never_planned_pairs`` — annotation only, the flag still fires. Empty
        # under explicit dates (every contract gets a window) — WARN byte-identical.
        emit_underlying_all_empty(
            minute.merged_pairs,
            [contract for _row, contract in rebuilt],
            failed_symbols=minute.failure_symbols,
            never_planned_symbols=[
                contract.symbol
                for _row, contract in rebuilt
                if contract.symbol not in minute_windows
            ],
        )

    # NOTE (DEC-PIPELINE-AUTOPREBUILD-001): the mid-pull F&O 1-day derivation that ran
    # here (``_derive_daily_from_minute``) is REMOVED — derived data (intraday TFs +
    # the derived ``1day``) is written ONLY by the pipeline's final prebuild phase
    # (brutex.dashboard.pipeline_prebuild), strictly after a fully-successful fetch.
    # ``candle_status=complete`` keys on the 1-minute completion set (a contract is
    # "historically complete" once its 1-minute landed clean — the only vendor phase).
    # Stamped BEFORE the heal/enrich stage (DEC-GREEKS-PARALLEL-001): the fenced
    # wrapper below now RE-RAISES a pull-wide halt (LakeWriteHalt /
    # GreeksEnrichPoolBroken), and a cleanly-fetched contract must keep its
    # completion stamp even when the best-effort overlay stage aborts — otherwise
    # the next pull needlessly re-plans vendor-clean contracts. Order-safe:
    # ``_mark_complete`` depends only on (registry, rebuilt, minute.complete).
    contracts_completed = _mark_complete(registry, rebuilt, minute.complete)

    greeks_outcome = _GREEKS_NOT_RUN
    if not dry_run:
        # DEC-LAKE-HEAL-001 L2: heal the pulled contracts' slice FIRST so the
        # Greeks enrichment below (and the pipeline's later prebuild phase)
        # reads clean 1-minute (LakeWriteHalt re-raises). Integrity, NOT a
        # post-step — it always runs, strict gate or not.
        _heal_lake_after_pull(rebuilt=rebuilt, windows=minute_windows, lake_root=lake_root)
        # The strict Greeks arm (DEC-PULL-SIX-STEP-ORDER-001): the literal
        # all-or-nothing barrier needs BOTH the threaded prior-step verdict
        # (spot/metadata — invisible from this F&O-local seam otherwise) AND
        # this phase's own cleanliness. A withheld pass loses nothing — the
        # overlay is re-derivable; the next pull re-attempts it.
        fno_clean = (
            not minute.auth_aborted
            and minute.mass_empty_cause is None
            and minute.failed_pairs == 0
            and minute.still_failed_windows == 0
        )
        if (
            prior_steps is not None
            and prior_steps.strict
            and not (prior_steps.prior_clean and fno_clean)
        ):
            _LOG.warning(
                "pipeline.greeks.strict_skipped",
                prior_clean=prior_steps.prior_clean,
                fno_clean=fno_clean,
            )
            greeks_outcome = _GREEKS_STRICT_WITHHELD
        else:
            # ── Greeks enrichment over the just-written option 1-minute (quarantined) ───
            # greeks_enrich_after_pull (DEC-GREEKS-ENGINE-PR2B-001, plan §15.4): compute +
            # STORE per-bar IV/Greeks for every OPTION contract by joining the same-minute
            # underlying spot, at the post-drain seam. Best-effort with a carved-out
            # halt path: an ordinary enrich failure NEVER fails the pull (fenced
            # inside), while a pull-wide HOST halt (LakeWriteHalt /
            # GreeksEnrichPoolBroken) RE-RAISES to the job layer's CRITICAL alert
            # (DEC-GREEKS-PARALLEL-001). FUT legs never get Greeks; skipped on a
            # dry-run (nothing was written — the outcome honestly stays "did not
            # run"). The pass verdict rides the report + note
            # (DEC-GREEKS-ENRICH-OBSERVE-001).
            greeks_outcome = _enrich_greeks_from_minute(
                rebuilt=rebuilt, windows=minute_windows, lake_root=lake_root
            )

    note = _build_note(
        dry_run=dry_run,
        contracts_selected=len(rebuilt),
        contracts_completed=contracts_completed,
        windows=minute.windows,
        failed_pairs=minute.failed_pairs,
        still_failed_windows=minute.still_failed_windows,
        auth_aborted=minute.auth_aborted,
        mass_empty_halted=minute.mass_empty_cause is not None,
        contracts_not_yet_in_window=contracts_not_yet_in_window,
        windows_skipped=minute.skipped_windows,
        ledger_load_failed=minute.ledger_load_failed,
        expiries_out_of_scope=scoped.expiries_out_of_scope,
        contracts_out_of_scope=scoped.contracts_out_of_scope,
        greeks_enrich_ok=greeks_outcome.ok,
        greeks_enrich_error=greeks_outcome.error,
        class_unresolved=class_unresolved,
        greeks_strict_withheld=greeks_outcome is _GREEKS_STRICT_WITHHELD,
    )
    return HistoricalPullReport(
        dry_run=dry_run,
        intervals=tuple(intervals),
        contracts_selected=len(rebuilt),
        contracts_completed=contracts_completed,
        bars_appended=minute.bars,
        windows=minute.windows,
        failed_pairs=minute.failed_pairs,
        still_failed_windows=minute.still_failed_windows,
        auth_aborted=minute.auth_aborted,
        note=note,
        # The retained, repurposed phase fields describe the SINGLE 1-minute phase.
        phase1_clean=minute.clean,
        phase1_complete=tuple(sorted(minute.complete)),
        start_dates={sym: win[0].isoformat() for sym, win in sorted(minute_windows.items())},
        minute_requested=True,
        minute_skipped=False,
        contracts_not_yet_in_window=contracts_not_yet_in_window,
        windows_skipped=minute.skipped_windows,
        ledger_load_failed=minute.ledger_load_failed,
        # DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001 minor M-5: the polished halt verdict +
        # the honest settle tally ride the report (see `HistoricalPullReport.verdict`).
        mass_empty_cause=minute.mass_empty_cause,
        settled_skipped=minute.settled_skipped,
        # DEC-FNO-EXPIRY-SCOPE-001: the honest out-of-scope tallies (0/0 unscoped).
        expiries_out_of_scope=scoped.expiries_out_of_scope,
        contracts_out_of_scope=scoped.contracts_out_of_scope,
        # DEC-VENDOR-CONFLICT-SURFACE-001: the vendor-behaviour honesty counters
        # (post-reconcile, so async-mode counts equal the synchronous path's).
        conflicts=minute.conflicts,
        vendor_dup_rows_dropped=minute.vendor_dup_rows_dropped,
        max_close_delta=minute.max_close_delta,
        # DEC-GREEKS-ENRICH-OBSERVE-001: the post-pull Greeks pass verdict (None on
        # a dry-run — the pass never ran; per-target quarantines never flip ok).
        greeks_enrich_ok=greeks_outcome.ok,
        greeks_enrich_error=greeks_outcome.error,
        greeks_targets_errored=greeks_outcome.errored_targets,
        # DEC-PULL-SIX-STEP-ORDER-001: the fail-closed-for-ORDER classification tally.
        class_unresolved=class_unresolved,
    )


__all__ = [
    "GROWW_HISTORICAL_FLOOR",
    "HistoricalClientFactory",
    "HistoricalPullReport",
    "pull_contract_candles",
]
