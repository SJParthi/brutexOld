"""Groww multi-instrument spot-ingest orchestrator.

The Groww vendor run-ingest layer (PR-H). Drives
:func:`~brutex.adapters.groww.ingest.ingest_spot`
across every ``(instrument, interval)`` pair resiliently: a **terminal** Groww
error on one pair is quarantined (recorded as failed) and the run continues to
the next pair — one bad symbol never kills the whole pull. Per-window transient
errors are already retried/skipped inside ``ingest_spot``; this layer adds the
multi-instrument fan-out + per-pair quarantine + an aggregate report.

The adapter, sink and pacing hook are all injected, so this
is 100% unit-testable without a network or a real lake.

Parallel fan-out: the OUTER loop — one job per
``(instrument, interval)`` pair — runs through an injected
:class:`~brutex.orchestrator.universe_phases.RunParallel` (production: a bounded
:class:`~brutex.orchestrator.parallel.PoolRunParallel`). Each pair writes a
DISTINCT lake partition, so the per-pair writes are path-isolated; the shared
rate bucket (the injected ``pace``) is the governor.

**Divergences from a latched, gap-aware spine (deliberate, Golden Rule #1):**

* **No auth fail-fast latch — but a TERMINAL auth failure HALTS (DEC-GROWW-AUTH-HALT-001).**
  Some vendors carry a shared ``AuthGuard`` + an ``auth_aborted`` / ``auth_cause``
  report field for a mid-pull token death. The Groww adapter provides no such
  latch; instead a TERMINAL ``GrowwAuthError`` (a dead credential / an exhausted
  re-mint budget — every remaining symbol would fail identically) is a
  :data:`_GLOBAL_STOP_ERRORS` member and PROPAGATES as a pull-wide halt (reaching
  the CRITICAL Telegram pull-halt alert), never a symbol-by-symbol quarantine
  that "completes" all-failed in silence. The RETRYABLE auth-family subclasses
  (a throttled mint / a stale read-only re-read) stay per-pair quarantinable,
  and the mid-pull re-mint self-heal (DEC-GROWW-TOKEN-REAUTH-001) is untouched.
* **No known-gap branch.** Some vendors special-case a "no data" 400 as a
  benign KNOWN_GAP. Groww has no such marker (Golden Rule #1 — no invented vendor
  code), so every terminal Groww error is a uniform quarantine.
* **No ``status_log``.** Dropped (the unified ledger is the sole record surface).

**Honest envelope (Golden Rule #10):** this targets UP TO the rate ceiling — it
cannot guarantee a constant rate (back-off step-downs + latency spikes lower the
achieved rate). Groww's backtesting r/s is UNVERIFIED, so no rate is hardcoded.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import date
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from brutex.adapters.groww.spot_bounds import SpotWindows
    from brutex.adapters.ingest_common import RetryOutcome
    from brutex.data.audit_capture import AuditCaptureSink
    from brutex.orchestrator.audit import AuditWriter, BufferedAuditRecord

from brutex.adapters.all_filtered_breaker import (
    AllFilteredBreaker,
    resolve_all_filtered_breaker,
)
from brutex.adapters.empty_circuit_breaker import (
    EmptySettleBreaker,
    MassEmptySettleHalt,
    resolve_empty_breaker,
)
from brutex.adapters.error_storm_breaker import ErrorStormBreaker, VendorDownError
from brutex.adapters.groww.constants import TRACK1_DAILY_INTERVAL
from brutex.adapters.groww.errors import (
    GrowwAuthError,
    GrowwCredentialDeadError,
    GrowwError,
    is_auth_failure,
    is_terminal_auth_failure,
)
from brutex.adapters.groww.fetch_pipeline import FetchPipeline
from brutex.adapters.groww.ingest import IngestReport, Pacer, WindowTick, ingest_spot
from brutex.adapters.groww.vendor_floor import VendorFloorLearner
from brutex.adapters.ingest_common import maybe_audit
from brutex.adapters.protocol import MarketAdapter
from brutex.data.errors import LakeWriteHalt, MarketHoursHalt
from brutex.data.sink import BarSink
from brutex.data.window_ledger import WindowLedger
from brutex.data.write_breaker import WriteHealthBreaker, resolve_write_breaker
from brutex.data.write_consumer import WriteConsumer, WriteConsumerSink
from brutex.models import BruteXError, Instrument
from brutex.observability.correlation import current_pull_id, pull_id_scope
from brutex.orchestrator.parallel import PoolRunParallel, SerialRunParallel
from brutex.orchestrator.quota_tracker import DailyQuotaExceeded, QuotaInvariantError
from brutex.orchestrator.universe_phases import RunParallel

#: Domain errors that are a GLOBAL stop, NOT one symbol's problem — they must
#: PROPAGATE past the per-symbol quarantine so the whole pull aborts, never be
#: isolated as a single pair's failure. A quota signal means the day's budget is
#: spent (``DailyQuotaExceeded``) or a quota-ledger invariant broke
#: (``QuotaInvariantError``), so every REMAINING symbol would fail identically;
#: ``DailyQuotaExceeded`` in particular must reach the controller's
#: ``except DailyQuotaExceeded`` to raise the loud ``quota_exhausted`` verdict +
#: resume after the IST-midnight reset (DEC-SPOT-QUOTA-MIDROUND-001). ``LakeWriteHalt``
#: (disk-full / OOM at the lake-write boundary, DEC-LAKE-WRITE-HALT-001) joins it: a
#: full disk fails every remaining window identically, so it must halt the pull
#: instead of being quarantined per-symbol (which would keep hammering a full disk).
#: This is the carve-out that keeps the broad ``BruteXError`` per-symbol quarantine
#: (DEC-GROWW-QUARANTINE-BRUTEX-001) from swallowing a pull-wide stop.
#: ``MassEmptySettleHalt`` joins it (DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001): a tripped
#: mass-empty circuit breaker means EVERY remaining window is fetching into the same
#: suspected soft-throttle / mis-addressing episode — settling on would poison the
#: ledger pull-wide, so the pull halts loudly instead of quarantining one symbol.
#: ``GrowwAuthError`` joins it (DEC-GROWW-AUTH-HALT-001): a TERMINAL auth failure — a
#: dead credential / an exhausted re-mint budget — fails every remaining symbol
#: identically, so quarantining symbol-by-symbol would "complete" an all-failed pull
#: with NO critical alert (the silent-for-days shape). NOTE the drain check carves the
#: two RETRYABLE ``GrowwAuthError`` subclasses (``GrowwTokenIssuanceThrottled`` /
#: ``GrowwTokenStillStaleError``) BACK OUT via
#: :func:`~brutex.adapters.groww.errors.is_terminal_auth_failure`, so a transient
#: auth-family shape stays a per-symbol quarantine and the mid-pull re-mint self-heal
#: (DEC-GROWW-TOKEN-REAUTH-001, which never raises out of ``client._call``) is untouched.
#: ``VendorDownError`` + ``GrowwCredentialDeadError`` join it
#: (DEC-VENDOR-DOWN-BREAKER-001): a tripped vendor-down error-storm breaker means the
#: VENDOR (or the network path to it) is down — every remaining window would burn its
#: whole retry ladder identically; a dead-credential escalation means every remaining
#: fetch is auth-stalled against the SAME dead token. Both halt, never quarantine
#: (``GrowwCredentialDeadError`` is a TERMINAL ``GrowwAuthError`` subclass, so it also
#: satisfies the ``is_terminal_auth_failure`` carve-out — listed explicitly as the
#: DEC-VENDOR-DOWN-BREAKER-001 escalation member).
_GLOBAL_STOP_ERRORS: tuple[type[BruteXError], ...] = (
    DailyQuotaExceeded,
    QuotaInvariantError,
    LakeWriteHalt,
    # DEC-GROWW-MARKET-HOURS-GUARD-001: the per-window market-hours guard raised —
    # the pull crossed into the NSE trading window and must halt to leave the shared
    # Groww account free for TickVault's live feed. A PULL-WIDE stop: propagates past
    # the per-symbol quarantine so the whole pull halts gracefully (the ledger's
    # PLANNED/FAILED windows stay resumable on the next out-of-hours run).
    MarketHoursHalt,
    MassEmptySettleHalt,
    GrowwAuthError,
    VendorDownError,
    GrowwCredentialDeadError,
)

#: Structured logger — per-underlying spot rollup at INFO (``ingest.underlying``)
#: + the terminal-failure cause at WARNING (``ingest.quarantine``), both carrying
#: the pull's ``pull_id``. The per-window / per-series DEBUG detail is one layer
#: down in ``ingest_spot``.
_LOG = structlog.get_logger("brutex.adapters.groww.run_ingest")

#: Live-progress hook fired once per ``(symbol, interval)`` pair after it is
#: processed: ``(label, bars_appended, failed)``. ``bars_appended`` is 0 for a
#: quarantined pair. (No Groww ``gap`` flag — there is no benign no-data boundary.)
ProgressHook = Callable[[str, int, bool], None]

#: One unit of OUTER-loop work for the parallel fan-out: a spot
#: ``(instrument, interval)`` pair. Each maps to a distinct lake partition.
SpotJob = tuple[Instrument, str]

#: Default bounded post-run auto-retry-ROUND budget — the number of EXTRA full
#: re-pulls of ONLY the leftover failed windows after the original pull, when it
#: left a transient residual. Small + finite (Golden Rule #10).
_DEFAULT_MAX_RETRY_ROUNDS: int = 3


def with_paced_client(
    adapter: MarketAdapter,
    pace: Pacer | None,
    on_rate_limit: Callable[[], None] | None = None,
    on_success: Callable[[], None] | None = None,
) -> MarketAdapter:
    """Return *adapter* with its Groww client's controller hooks wired to the shared bucket.

    The client's per-attempt ``pace()`` (inside ``GrowwClient._call``) is the ONE
    canonical admission point per physical fetch — first attempts AND the client's
    bounded fast-retry attempts alike (DEC-RATE-SINGLE-ADMISSION-001). Once bound
    here, the spine's per-window ``pace()`` is skipped (``ingest_spot`` detects the
    self-pacing client via
    :func:`~brutex.adapters.groww.client.client_self_paces`), so a fetch is never
    admitted twice against the 500/min governor + AIMD ladder.

    PUBLIC because it is the ONE binding seam for ANY out-of-spine consumer of the
    pull's adapter: the live pool factory hands every leaf ``_noop_pace``
    (``live.build_live_pool`` without a ``pace``), so an adapter captured BEFORE
    this binding fetches UNPACED — the vendor-floor depth probe's blocker fixed by
    constructing the learner over ``with_paced_client(adapter, pace)`` at the
    production build sites (DEC-VENDOR-FLOOR-LAZY-PROBE-001 review fold).

    DEC-SUSPICION-SWEEP-FIXES-001 (D1): this seam now ALSO binds the controller's
    ``on_rate_limit`` + ``on_success`` feedback hooks into the client — the spot
    twin of what the F&O build sites always did
    (``historical_pull._build_live_client`` bakes all three into
    :func:`~brutex.adapters.groww.live.build_live_client`). Before this, the seam
    rebound ONLY ``pace``: the client's bounded fast-retry absorbed 429s against a
    NO-OP ``on_rate_limit``, so the AIMD ladder saw a 429 only after the whole
    ``max_attempts`` budget was exhausted and the error propagated to the spine —
    ~3× ``max_attempts`` vendor rejections per designed step-down, and any
    sub-exhaustion 429 burst was INVISIBLE to the ladder. Both production spot
    build sites (``dashboard/pull_controller`` + ``cli``) feed every pull through
    :func:`run_groww_spot_ingest`, so this ONE COMMON seam (CLAUDE.md §11) covers
    them both; the spine's own per-window hooks then stand down
    (:func:`~brutex.adapters.groww.client.client_self_reports_rate_limits` /
    ``client_self_reports_successes`` in ``ingest_spot``) so the final propagated
    occurrence is never double-counted. Rate CAPS are untouched — this wires
    FEEDBACK, never the ceiling/governor values (CLAUDE.md §2 Rule 6).

    A no-op (returns *adapter* unchanged) when there is nothing to wire: an
    unthrottled run (*pace* is ``None`` — the dry-run / injected-factory quad is
    all-``None`` together), or any adapter that is not the live
    :class:`~brutex.adapters.groww.adapter.GrowwMarketAdapter`. The adapter +
    client are frozen dataclasses, so this rebuilds via :func:`replace` rather than
    mutating. The import is local to avoid an import cycle and is paid once per pull.
    """
    from brutex.adapters.groww.adapter import GrowwMarketAdapter

    if pace is None or not isinstance(adapter, GrowwMarketAdapter):
        return adapter
    # ``with_rate_hooks`` (the GrowwClientProtocol seam) re-binds the hook triple
    # immutably — for a single client it is a ``dataclasses.replace``; for a
    # multi-connection ClientPool it re-fans the hooks into EVERY leaf
    # (DEC-GROWW-CLIENT-POOL-001), so no connection bypasses the shared bucket or
    # under-reports its 429s. A ``None`` feedback hook keeps the client's current
    # binding (pace-only callers keep the pre-D1 behaviour byte-for-byte).
    return replace(
        adapter,
        client=adapter.client.with_rate_hooks(
            pace=pace, on_rate_limit=on_rate_limit, on_success=on_success
        ),
    )


def _resolve_spot_window(
    symbol_windows: SpotWindows | None,
    *,
    symbol: str,
    interval: str,
    start: date,
    end: date,
) -> tuple[date, date]:
    """Resolve one ``(instrument, interval)`` pair's ``(start, end)`` — the opt-in bound hook.

    The SPOT twin of the option/future
    :func:`~brutex.adapters.groww.option_ingest._resolve_window`. Returns the
    per-symbol window from *symbol_windows* ONLY for sub-daily intervals and only
    when *symbol* is in the map; every other case falls back to the global
    ``(start, end)``:

    * ``symbol_windows is None`` → ``(start, end)`` — the SPOT pull paths always pass
      ``None`` since DEC-SPOT-INTERVAL-DECOUPLE-001 (MIGRATION.md #851): the 1-minute
      pull is fully DECOUPLED from the 1-day coverage and runs over the full
      ``[start, end]`` range for every symbol. This branch is the live path.
    * ``interval == TRACK1_DAILY_INTERVAL`` → ``(start, end)`` — the DAILY interval is
      never bounded (a spot index's daily series must keep advancing to the requested
      end).
    * *symbol* absent from the map → ``(start, end)`` — the never-narrow fallback for
      any caller that does supply an opt-in *symbol_windows* map.

    Pure + **O(1)** (one dict lookup).
    """
    if symbol_windows is None or interval == TRACK1_DAILY_INTERVAL:
        return (start, end)
    return symbol_windows.get(symbol, (start, end))


def _append_capture_records(
    records: tuple[BufferedAuditRecord, ...],
    audit: AuditWriter,
) -> None:
    """Append drained capture records onto the hash-chained *audit* writer.

    The ONE append loop every capture drain seam shares (CLAUDE.md §11
    COMMON) — the in-run seams in :func:`run_groww_spot_ingest` and the pull
    owner's post-pipeline-drain :func:`flush_capture_to_audit`. Controller
    thread only (the ``AuditWriter`` is not thread-safe, Golden Rule #8).
    """
    for record in records:
        audit.append(action=record.action, subject=record.subject, payload=record.payload)


def flush_capture_to_audit(
    capture: AuditCaptureSink | None,
    audit: AuditWriter | None,
) -> int:
    """The FOURTH capture drain seam — flush records the async pipeline deferred.

    DEC-AUDIT-LAKE-REPLAY-ORACLE-001 round-1 review fix (BLOCKER): under the
    DEFAULT-ON async fetch pipeline (DEC-GROWW-FETCH-ASYNC-SEPARATION-001) a
    window's durable ``sink.append`` — the point the capture sink buffers its
    ``LAKE_APPEND_WRITTEN`` / ``add`` records — runs on a background pipeline
    consumer (or write-pool shard) thread, and the pipeline is only drained
    by the pull owner AFTER :func:`run_groww_spot_ingest_with_retry` returns.
    Records buffered after the run's own final ``drain_remaining()`` would
    therefore be STRANDED — silently discarded with the sink — making the
    capture log structurally incomplete (a false replay divergence + a
    permanent L3 ``extra_in_live``, since the settled windows are never
    re-pulled). The pull owner MUST call this on the controller thread AFTER
    draining the fetch pipeline + write consumer, while the ingest ``audit``
    writer is still open (``cli.py`` ``cmd_fetch``'s ``finally`` does).

    Replay is order-insensitive to the resulting post-``INGEST_RUN_COMPLETE``
    chain lines: events are applied in ``(capture_run, capture_seq)`` order,
    not chain-line order.

    Returns the number of records appended to the chain. ``capture is None``
    (the default OFF mode) and an empty buffer both return ``0``; a non-empty
    buffer with ``audit is None`` discards and logs the loud
    ``audit_capture.no_chain`` WARN (capture without a chain is useless,
    never silent).
    """
    if capture is None:
        return 0
    records = capture.drain_remaining()
    if not records:
        return 0
    pull_id = current_pull_id()
    if audit is None:
        _LOG.warning(
            "audit_capture.no_chain",
            pull_id=pull_id,
            discarded=len(records),
        )
        return 0
    _append_capture_records(records, audit)
    _LOG.info(
        "audit_capture.post_drain_flush",
        pull_id=pull_id,
        records=len(records),
    )
    return len(records)


@dataclass(frozen=True, slots=True)
class PairFailure:
    """A ``(symbol, interval)`` pair that failed terminally (quarantined)."""

    symbol: str
    interval: str
    error: str
    auth_failure: bool = False
    """Whether the terminal error was a Groww auth failure (a dead/invalid token).

    Captured from the live exception at quarantine time (Groww has no error-string
    no-data marker to re-derive it from), so the auto-retry wrapper
    can halt on a token death — retrying is futile until re-auth.
    """


@dataclass(frozen=True, slots=True)
class GrowwIngestReport:
    """Aggregate outcome of a :func:`run_groww_spot_ingest` pass."""

    pairs: dict[tuple[str, str], IngestReport]
    failures: tuple[PairFailure, ...]

    @property
    def windows(self) -> int:
        """Total fetch windows across all successful pairs."""
        return sum(r.windows for r in self.pairs.values())

    @property
    def bars_appended(self) -> int:
        """Total bars appended across all successful pairs."""
        return sum(r.bars_appended for r in self.pairs.values())

    @property
    def transient_failures(self) -> int:
        """Total per-window transient skips across all successful pairs."""
        return sum(r.transient_failures for r in self.pairs.values())

    @property
    def skipped_windows(self) -> int:
        """Total windows skipped as already-complete across all pairs."""
        return sum(r.skipped_windows for r in self.pairs.values())

    @property
    def retried_windows(self) -> int:
        """Total windows re-attempted by the in-run auto-retry."""
        return sum(r.retried_windows for r in self.pairs.values())

    @property
    def still_failed_windows(self) -> int:
        """Total windows still failing after the retry budget — deferred to the next pull."""
        return sum(r.still_failed_windows for r in self.pairs.values())

    @property
    def failed_pairs(self) -> int:
        """Number of ``(symbol, interval)`` pairs quarantined on a terminal error."""
        return len(self.failures)

    @property
    def auth_aborted(self) -> bool:
        """True iff any quarantined failure was a Groww auth failure.

        Groww has no fail-fast latch (the documented divergence), so this is a
        DERIVED read over the quarantined failures' captured ``auth_failure`` flag
        rather than a latch state. It lets the auto-retry wrapper halt the loop on
        a token death — retrying is futile until re-auth.
        """
        return any(f.auth_failure for f in self.failures)


def run_groww_spot_ingest(
    adapter: MarketAdapter,
    sink: BarSink,
    *,
    instruments: Iterable[Instrument],
    intervals: Sequence[str],
    start: date,
    end: date,
    pace: Pacer | None = None,
    ledger: WindowLedger | None = None,
    on_rate_limit: Callable[[], None] | None = None,
    on_timeout: Callable[[], None] | None = None,
    on_success: Callable[[], None] | None = None,
    on_progress: ProgressHook | None = None,
    run_parallel: RunParallel | None = None,
    audit: AuditWriter | None = None,
    on_window: WindowTick | None = None,
    symbol_windows: SpotWindows | None = None,
    write_pool: WriteConsumer | None = None,
    fetch_pipeline: FetchPipeline | None = None,
    detect_completeness_gaps: bool = False,
    observed_holidays: frozenset[date] = frozenset(),
    empty_breaker: EmptySettleBreaker | None = None,
    all_filtered_breaker: AllFilteredBreaker | None = None,
    storm_breaker: ErrorStormBreaker | None = None,
    write_breaker: WriteHealthBreaker | None = None,
    floor_learner: VendorFloorLearner | None = None,
    market_closed: Callable[[], bool] | None = None,
    capture: AuditCaptureSink | None = None,
) -> GrowwIngestReport:
    """Ingest every ``(instrument, interval)`` spot pair over ``[start, end)``.

    The OUTER loop is mapped through *run_parallel* so many windows are in flight
    at once and the pull targets UP TO the rate ceiling when Groww is healthy,
    governed by the shared rate bucket the *pace* hook drives. Each pair writes a
    DISTINCT lake partition. **Never raises** on a brutex DOMAIN error
    (:class:`~brutex.models.BruteXError` — incl. ``GrowwError`` / ``CandleParseError`` /
    ``OutOfScopeError``): a terminal failure on one pair is recorded and the run
    continues. A genuine CODE bug (a Python builtin like ``ValueError``) is NOT swallowed
    (DEC-GROWW-QUARANTINE-BRUTEX-001).

    Parameters
    ----------
    adapter, sink
        The Groww adapter (data source) and the lake write sink, shared across
        workers (the spot sink writes path-isolated partitions).
    instruments
        Spot instruments to pull.
    intervals
        Canonical intervals (``"1minute"`` / ``"1day"`` / tiered Groww intervals).
    start, end
        Half-open date range, shared by every pair.
    pace
        Optional pacing hook, wired into the adapter's Groww client as the ONE
        canonical per-fetch admission (retries included) AND threaded into each
        :func:`ingest_spot` call — where the per-window acquire stands down when
        the client self-paces, so each physical fetch is admitted exactly ONCE
        (DEC-RATE-SINGLE-ADMISSION-001). Groww's r/s is UNVERIFIED, so this
        injected bucket is the only rate governor.
    on_rate_limit, on_timeout, on_success
        Optional rate-controller feedback hooks threaded into every pair.
    on_progress
        Optional live-progress hook fired once per pair: ``(label, bars, failed)``.
        A quarantined pair fires with ``bars=0, failed=True``. Fired on the
        CONTROLLER thread (so it need not be thread-safe).
    run_parallel
        The concurrency primitive mapping the per-pair fetcher over the worklist.
        ``None`` builds a bounded
        :class:`~brutex.orchestrator.parallel.PoolRunParallel`. Tests inject a
        synchronous fake.
    on_window
        Optional :data:`~brutex.adapters.groww.ingest.WindowTick` threaded into every
        pair so the dashboard advances its bar + flushes /coverage per WINDOW
        (DEC-SPOT-WINDOW-PROGRESS-001), not just once per finished pair. Fired on the
        window worker threads; the caller owns throttle + thread-safety. ``None`` is a
        no-op (CLI / tests).
    symbol_windows
        Optional per-symbol ``{symbol_name: (start, end)}`` map (the spot twin of
        options' ``contract_windows``). When supplied, a SUB-DAILY pull of a symbol
        present in the map runs over its mapped ``(start, end)`` instead of the global
        range (see :func:`_resolve_spot_window`); the DAILY interval is never bounded
        and a symbol absent from the map is pulled over the full ``[start, end)``.
        ``None`` (the default — and what the SPOT pull paths always pass since
        DEC-SPOT-INTERVAL-DECOUPLE-001, MIGRATION.md #851) pulls the full
        ``[start, end)`` range for every symbol: the 1-minute fetch is fully decoupled
        from the 1-day coverage. The alias is retained for any caller that does supply
        an opt-in map (the never-narrow fallback).
    write_pool
        Optional :class:`~brutex.data.write_consumer.ShardedWriteConsumer`
        (DEC-FETCH-WRITE-OVERLAP-LEVER1-001). When supplied, *sink* is wrapped in a
        :class:`~brutex.data.write_consumer.WriteConsumerSink` so each window's lake
        write is handed OFF to a background shard worker instead of running serially on
        the fetch worker thread — the fetch lane stops idling on its own
        O(month-depth) write, and the ONLY limiter becomes Groww's rate. The spine's
        settle-after-durable ledger ordering is preserved BYTE-FOR-BYTE: the wrapper's
        ``append`` blocks until the durable write returns, so the existing
        ``ledger.record`` still runs only after the bytes are on disk. ``None`` (the
        default) reproduces TODAY's fully-synchronous path exactly — no thread, no queue.
        The caller OWNS the pool's lifetime and MUST :meth:`drain` it BEFORE saving the
        window ledger (so every settled window is durable).
    fetch_pipeline
        Optional :class:`~brutex.adapters.groww.fetch_pipeline.FetchPipeline`
        (DEC-GROWW-FETCH-ASYNC-SEPARATION-001). When supplied, each window's SUCCESS-path
        tail (session-filter + ``sink.append`` + conflict-aggregate + ``ledger.record``) is
        handed OFF to a background consumer so the fetch lane does ONLY fetch + enqueue —
        internal processing can never block or slow the fetch. Forwarded verbatim into every
        :func:`ingest_spot` call. ``None`` (the default) keeps the fully-synchronous,
        byte-identical path. Mutually independent of *write_pool* (a caller would wire ONE or
        the other; the pipeline subsumes the write-only decouple). The caller OWNS the
        pipeline's lifetime and MUST :meth:`drain` it BEFORE saving the window ledger.
    empty_breaker
        Optional :class:`~brutex.adapters.empty_circuit_breaker.EmptySettleBreaker`
        (DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001). ``None`` (the default — every
        production caller) constructs a FRESH env-configured breaker for THIS pass via
        :func:`~brutex.adapters.empty_circuit_breaker.resolve_empty_breaker`, so the
        mass-empty guard is ON by default and its state can never leak across
        pulls/passes. The ONE breaker is threaded into every pair's window workers
        (pull-wide counting — a soft-throttle episode spans symbols); a trip raises
        :class:`~brutex.adapters.empty_circuit_breaker.MassEmptySettleHalt`, a
        :data:`_GLOBAL_STOP_ERRORS` member — the whole pull halts loudly. Tests inject
        a configured instance.
    all_filtered_breaker
        Optional :class:`~brutex.adapters.all_filtered_breaker.AllFilteredBreaker`
        (DEC-ALLFILTERED-MASS-BREAKER-001). Every PRODUCTION caller injects the
        ONE pull-scoped instance resolved at its pull entrypoint (the dashboard
        bulk + single-symbol pulls and the CLI ``cmd_fetch`` — review round 4;
        :func:`run_groww_spot_ingest_with_retry` resolves one itself for a
        direct ``None`` caller), so the trip latches ACROSS retry passes and
        until-clean rounds, never just this invocation. ``None`` (direct
        single-pass callers / tests) constructs a FRESH env-configured breaker
        for THIS invocation via
        :func:`~brutex.adapters.all_filtered_breaker.resolve_all_filtered_breaker`,
        so the mass ALL-FILTERED guard is ON by default and its state can never
        leak across pulls. The ONE breaker is threaded into every pair's
        window tails (pull-wide counting — a vendor-day timestamp bug spans
        symbols); a trip SUPPRESSES further ``ALL_FILTERED`` SKIPPED-settles +
        auto-rescinds the same-pull pre-trip ones + emits ONE count-naming alarm
        — deliberately NOT a pull stop (fetching/storage stay unpoisoned). Tests
        inject a configured instance.
    storm_breaker
        Optional per-pull :class:`~brutex.adapters.error_storm_breaker.ErrorStormBreaker`
        (DEC-VENDOR-DOWN-BREAKER-001), threaded verbatim into every pair's window
        workers so a vendor outage (sustained zero-success transport failures across
        distinct symbols) or a dead credential (a consecutive StillStale streak over
        T minutes) raises its global stop out of this function. ``None`` (the
        default — dry-run / synthetic / unit-test callers) never arms it; ONLY the
        real-vendor production sites construct one
        (:func:`~brutex.adapters.error_storm_breaker.resolve_error_storm_breaker`),
        mirroring the ``detect_completeness_gaps`` gating.
    write_breaker
        Optional :class:`~brutex.data.write_breaker.WriteHealthBreaker`
        (DEC-WRITE-HEALTH-BREAKER-001). ``None`` (the default) resolves a FRESH
        env-configured breaker for THIS pass via
        :func:`~brutex.data.write_breaker.resolve_write_breaker` (armed by default;
        ``BRUTEX_WRITE_BREAKER=off`` disables). The ONE breaker is threaded into every
        pair's window tails: 2 consecutive write-defers open a partition, 3 DISTINCT
        open partitions raise ``LakeWriteHalt("volume_wedged")`` — a
        :data:`_GLOBAL_STOP_ERRORS` member, so a wedged VOLUME halts the pull
        gracefully (with the existing #888/#889 Telegram alert) instead of burning
        every remaining window on write-deadline defers. Tests inject a configured
        instance.
    floor_learner
        Optional per-pull :class:`~brutex.adapters.groww.vendor_floor.VendorFloorLearner`
        (DEC-VENDOR-FLOOR-LAZY-PROBE-001), threaded verbatim into every pair's
        window workers: a confirmed clean-empty window AT the configured history
        floor triggers ONE bounded, breaker-gated depth probe per symbol per pull
        (see :func:`~brutex.adapters.groww.ingest.ingest_spot`). ``None`` (the
        default — dry-run / synthetic / unit-test callers) never probes; ONLY the
        real-vendor production sites arm it
        (:func:`~brutex.adapters.groww.vendor_floor.make_vendor_floor_learner`).
    market_closed
        Optional zero-arg ``() -> bool`` predicate (DEC-GROWW-MARKET-HOURS-GUARD-001)
        consulted at the TOP of every window worker: ``True`` means the pull has
        crossed into the NSE market-hours window (``[08:45, 15:35)`` IST on a trading
        day) and must STOP to free the shared Groww account for TickVault's live feed,
        so the worker raises :class:`~brutex.data.errors.MarketHoursHalt` — a
        :data:`_GLOBAL_STOP_ERRORS` member that halts the pull gracefully (the
        ledger's PLANNED/FAILED windows stay resumable next out-of-hours run). ``None``
        (the default — dry-run / synthetic / unit-test callers) never gates; ONLY the
        real production entrypoints bind the live guard.
    capture
        Optional :class:`~brutex.data.audit_capture.AuditCaptureSink`
        (DEC-AUDIT-LAKE-REPLAY-ORACLE-001). When wired (the caller has ALREADY
        wrapped *sink* — or the write pool's base sink — in it via
        :func:`~brutex.data.audit_capture.resolve_audit_capture`), its buffered
        replay records are drained onto the SAME hash-chained *audit* writer on
        the CONTROLLER thread at three seams: after each pair's
        ``INGEST_PAIR_DONE``, after each ``INGEST_PAIR_FAILED`` (a quarantined
        pair's already-durable windows MUST still be audited or replay is
        structurally incomplete), and once more before ``INGEST_RUN_COMPLETE``.
        HONEST async-mode envelope (Golden Rule #10): those in-run seams flush
        everything ONLY on the synchronous path — when *fetch_pipeline* (the
        production DEFAULT) or *write_pool* is wired, a window's durable
        ``sink.append`` runs on a background consumer thread AFTER the
        producer's deferred outcome resolves, so a pair's records may not yet
        be buffered when its pair seam fires (the per-pair drain is then an
        empty no-op) and records may still arrive after the final in-run
        drain. The pull owner MUST therefore call
        :func:`flush_capture_to_audit` (the FOURTH seam) after draining the
        pipeline + write pool, while the chain is still open — ``cmd_fetch``'s
        ``finally`` does. The chain stays literally single-writer (Golden
        Rule #8) — no worker thread ever touches the
        :class:`~brutex.orchestrator.audit.AuditWriter`; replay applies events
        in ``(capture_run, capture_seq)`` order, so drain-seam chain-line
        placement is forensically irrelevant. ``capture`` wired with ``audit
        is None`` discards the records and logs ONE loud
        ``audit_capture.no_chain`` WARN per run (capture without a chain is
        useless, never silent). ``None`` (the default) is byte-identical to
        today.

    Returns
    -------
    GrowwIngestReport
        Per-pair :class:`IngestReport` for successes + the quarantined failures.
    """
    pull_id = current_pull_id()
    # One breaker per PASS (DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001): resolved here — not
    # inside ingest_spot — so ALL pairs of this pass share ONE counter (a vendor
    # soft-throttle hits every pair of the pass) and a fresh pass/pull always starts
    # clean. DISCLOSED envelope (DEC-PULL-SIX-STEP-ORDER-001 round-3 review, Golden
    # Rule #10): under the six-step spot order the dashboard pull invokes this spine
    # once per STEP GROUP (INDICES-SPOT, then STOCKS-SPOT) per until-clean round, so
    # "this pass" spans ONE group — no longer the whole ~750-underlying universe the
    # original rationale assumed. The ~4-series indices group (the ENGINE's
    # NIFTY/BANKNIFTY/SENSEX + VIX) runs FIRST and ALONE: on an incremental pull its
    # handful of outstanding windows sits below EVERY trip threshold
    # (`EMPTY_BREAKER_CONSECUTIVE_LIMIT` / `EMPTY_BREAKER_MIN_OBSERVATIONS` /
    # `EMPTY_BREAKER_FRACTION_MIN_SERIES`), so a vendor soft-throttle's clean
    # empties there cannot trip a halt and the index windows can settle SKIPPED
    # (`EMPTY_TERMINAL_THRESHOLD` after ~3 group-scoped rounds) before the
    # ~746-series stocks group's breaker trips — remediation `brutex
    # reopen-skipped`, the idempotent keep-first sink re-pulls them. Sharing one
    # breaker across a phase's groups is a possible future tightening, deliberately
    # NOT done here: an injected instance bypasses the per-pass resolve seam (the
    # documented `empty_breaker=` contract, mirroring the F&O twin's reasoning), and
    # the groups run SEQUENTIALLY to settlement, so cross-group evidence could never
    # protect the FIRST (indices) group anyway. Full disclosure: MIGRATION.md #1527.
    breaker = empty_breaker if empty_breaker is not None else resolve_empty_breaker()
    # One mass ALL-FILTERED breaker per PULL (DEC-ALLFILTERED-MASS-BREAKER-001):
    # every production caller INJECTS the pull-scoped instance resolved at its
    # entrypoint (review round 4 — the trip must latch across retry passes and
    # until-clean rounds, or a sub-floor retry pass settles windows the pass-1
    # alarm promised were halted), so all pairs — and all PASSES — share ONE
    # counter (a vendor-day timestamp bug empties every symbol's boundary at
    # once) and a fresh pull always starts clean (Golden Rule #7). The `None`
    # fallback resolve serves direct single-pass callers; the explicit param is
    # ALSO the test seam; BRUTEX_ALLFILTERED_BREAKER=off resolves a disabled
    # instance (observe no-ops — byte-identical settle behaviour).
    af_breaker = (
        all_filtered_breaker if all_filtered_breaker is not None else resolve_all_filtered_breaker()
    )
    # One WRITE-health breaker per PASS (DEC-WRITE-HEALTH-BREAKER-001), mirroring the
    # empty breaker above: all pairs share ONE per-partition defer accounting (a wedged
    # volume fails every partition) and a fresh pass/pull always starts clean. The
    # resolved default is armed; BRUTEX_WRITE_BREAKER=off yields None (byte-identical
    # per-window defer behaviour).
    wbreaker = write_breaker if write_breaker is not None else resolve_write_breaker()
    # Lever 1 (DEC-FETCH-WRITE-OVERLAP-LEVER1-001): when a write pool is wired, route
    # every sink.append through it so the O(month-depth) lake write runs on a background
    # shard worker, freeing the fetch connection to keep fetching. `write_pool=None`
    # leaves `sink` untouched → byte-identical synchronous path. The wrapper still blocks
    # the fetch worker until the durable write returns, so settle-after-durable holds.
    if write_pool is not None:
        sink = WriteConsumerSink(consumer=write_pool)
    # Wire the shared bucket's *pace* PLUS the controller's on_rate_limit/on_success
    # feedback into the adapter's Groww client — the ONE canonical admission +
    # feedback point per physical fetch, retries included; the spine's per-window
    # pace AND feedback hooks then stand down (DEC-RATE-SINGLE-ADMISSION-001 +
    # DEC-SUSPICION-SWEEP-FIXES-001 D1: without the feedback binding the client's
    # bounded fast-retry absorbed 429s invisibly to the AIMD ladder). No-op for
    # adapters without a paceable client (fakes) or when unthrottled — those keep
    # the per-window pace + hooks as the fallback.
    adapter = with_paced_client(adapter, pace, on_rate_limit, on_success)
    runner: RunParallel = run_parallel if run_parallel is not None else PoolRunParallel()

    # Deterministic worklist: one job per (instrument, interval) pair, in the
    # caller's instrument order × interval order. The OUTER fan-out unit.
    instruments = list(instruments)
    jobs: list[SpotJob] = [
        (instrument, interval) for instrument in instruments for interval in intervals
    ]
    # Concurrency model (DEC-SPOT-CROSS-SYMBOL-FANOUT-001). A MULTI-pair (bulk)
    # pull fans the ONE bounded outer pool ACROSS symbols and runs each symbol's
    # windows SERIALLY (``SerialRunParallel``) — one in-flight call per symbol,
    # ``len(jobs)`` symbols concurrent — so a single bounded pool saturates the
    # shared rate bucket. The previous design nested a second per-symbol *window*
    # pool inside every outer worker, multiplying into ``outer × inner`` threads
    # (a measured 24 × 30 ≈ up to 720) that only thrashed the GIL on the Parquet
    # write render without raising throughput. A SINGLE-pair pull (one symbol —
    # the /universe per-symbol button or the CLI) instead hands that one pair the
    # full outer ``runner`` for its WINDOWS (no symbol-level fan-out to spend it
    # on), preserving single-symbol window parallelism. The shared token bucket
    # stays the sole issue governor either way (Rule #6 — no ceiling is raised).
    inner_runner: RunParallel = runner if len(jobs) <= 1 else SerialRunParallel()

    def _run_pair(job: SpotJob) -> IngestReport:
        """Pull one ``(instrument, interval)`` pair — runs inside a worker thread.

        Re-binds *pull_id* into THIS thread's structlog context-vars. A raised
        :class:`GrowwError` is captured by *run_parallel* and quarantined below;
        any other exception is captured too and re-raised on the controller thread.
        Each pair's windows run through *inner_runner* (serial for a bulk pull;
        the full pool for a single-pair pull) — see the concurrency-model note above.
        """
        instrument, interval = job
        window_start, window_end = _resolve_spot_window(
            symbol_windows,
            symbol=instrument.symbol_name,
            interval=interval,
            start=start,
            end=end,
        )
        with pull_id_scope(pull_id):
            return ingest_spot(
                adapter,
                sink,
                instrument=instrument,
                interval=interval,
                start=window_start,
                end=window_end,
                pace=pace,
                ledger=ledger,
                on_rate_limit=on_rate_limit,
                on_timeout=on_timeout,
                on_success=on_success,
                run_parallel=inner_runner,
                on_window=on_window,
                fetch_pipeline=fetch_pipeline,
                detect_completeness_gaps=detect_completeness_gaps,
                observed_holidays=observed_holidays,
                empty_breaker=breaker,
                all_filtered_breaker=af_breaker,
                storm_breaker=storm_breaker,
                write_breaker=wbreaker,
                floor_learner=floor_learner,
                market_closed=market_closed,
            )

    # Replay-capture drain (DEC-AUDIT-LAKE-REPLAY-ORACLE-001): records buffered by
    # the capture sink on worker/shard threads are appended to the real chain ONLY
    # here on the controller thread — the AuditWriter stays literally single-writer
    # (Golden Rule #8; the sanctioned BufferedAuditRecord replay pattern).
    capture_no_chain_warned = False

    def _drain_capture(records: tuple[BufferedAuditRecord, ...]) -> None:
        """Append drained capture records to the chain (controller thread only)."""
        nonlocal capture_no_chain_warned
        if not records:
            return
        if audit is None:
            # Capture without a chain is useless — never silent (one WARN per run).
            if not capture_no_chain_warned:
                capture_no_chain_warned = True
                _LOG.warning(
                    "audit_capture.no_chain",
                    pull_id=pull_id,
                    discarded=len(records),
                )
            return
        _append_capture_records(records, audit)

    pairs: dict[tuple[str, str], IngestReport] = {}
    failures: list[PairFailure] = []
    maybe_audit(
        audit,
        "INGEST_RUN_START",
        f"pull:{pull_id}",
        {
            "instruments": len(instruments),
            "intervals": list(intervals),
            "jobs": len(jobs),
            "start": start.isoformat(),
            "end": end.isoformat(),
        },
    )
    # Drain the fan-out on the controller thread. on_progress + the quarantine
    # WARNING fire here (single-threaded). A captured non-BruteXError (a code bug) surfaces.
    for outcome in runner(_run_pair, jobs):
        instrument, interval = outcome.item
        label = f"{instrument.symbol_name} {interval}"
        if outcome.ok:
            report = outcome.result
            if report is None:  # pragma: no cover — ok=True always carries a result
                continue
            pairs[instrument.symbol_name, interval] = report
            maybe_audit(
                audit,
                "INGEST_PAIR_DONE",
                f"{instrument.symbol_name}:{interval}",
                {
                    "bars_appended": report.bars_appended,
                    "windows": report.windows,
                    "still_failed_windows": report.still_failed_windows,
                },
            )
            if capture is not None:
                _drain_capture(capture.drain_pair(instrument.symbol_name, interval))
            if on_progress is not None:
                on_progress(label, report.bars_appended, False)
            continue
        # A failed ItemResult always carries the captured worker exception.
        exc = outcome.exc
        if exc is None:  # pragma: no cover — a failed outcome always carries exc
            continue
        if isinstance(exc, _GLOBAL_STOP_ERRORS) and (
            not isinstance(exc, GrowwAuthError) or is_terminal_auth_failure(exc)
        ):
            # A GLOBAL stop (quota exhausted / quota-ledger invariant / disk-full /
            # a TERMINAL dead-credential auth failure, DEC-GROWW-AUTH-HALT-001) is NOT
            # a per-symbol failure — re-raise so it propagates to the controller's
            # halt handling (DEC-SPOT-QUOTA-MIDROUND-001) instead of being
            # quarantined as one pair (the carve-out from the broad BruteXError
            # quarantine — DEC-GROWW-QUARANTINE-BRUTEX-001). The RETRYABLE
            # ``GrowwAuthError`` subclasses (a throttled mint / a stale read-only
            # re-read) are carved BACK OUT here: transient by design, they fall
            # through to the ordinary per-symbol quarantine below, never a halt.
            raise exc
        if not isinstance(exc, BruteXError):
            # A genuine CODE bug (a Python builtin: ValueError / AttributeError /
            # KeyError / TypeError from a defect) escaped a worker — never swallow a real
            # error: re-raise so the orchestrator never hides a bug. ONLY a brutex DOMAIN
            # error (every explicit `raise` in the spine subclasses `BruteXError`) is
            # quarantined — the harden-against-bad-DATA-never-mask-bad-CODE line
            # (DEC-INSTRUMENT-SKIP-NO-GSYM-001's philosophy, DEC-GROWW-QUARANTINE-BRUTEX-001).
            raise exc
        # Terminal DOMAIN error → isolate this pair, the run continues. The base
        # `BruteXError` covers EVERY "this one symbol can't be pulled" cause that must
        # NEVER crash the whole 779-symbol bulk pull — the operator hit three of them in
        # turn: a `GrowwError` (vendor API), a `CandleParseError` (un-storable candle, e.g.
        # "unparseable open price: None"), and an `OutOfScopeError` ("symbol not in Groww
        # universe: …") for a constituent the niftyindices→Groww match produced that is
        # genuinely absent from the master (a HYPHENATED ticker like BAJAJ-AUTO now resolves
        # correctly, DEC-GROWW-HYPHEN-SYMBOL-RESOLVE-001 #845 — this guard remains the
        # backstop for a symbol in NO master spelling). Quarantine the ONE symbol — re-tried
        # next pull, never masked — and the pull finishes for the others. Only a `GrowwError`
        # can be an auth failure (a parse / scope error never is).
        auth_failed = isinstance(exc, GrowwError) and is_auth_failure(exc)
        failures.append(
            PairFailure(
                symbol=instrument.symbol_name,
                interval=interval,
                error=str(exc),
                auth_failure=auth_failed,
            )
        )
        maybe_audit(
            audit,
            "INGEST_PAIR_FAILED",
            f"{instrument.symbol_name}:{interval}",
            {"error": str(exc), "auth_failure": auth_failed},
        )
        # A quarantined pair's ALREADY-DURABLE windows must still be audited or
        # the replay is structurally incomplete (DEC-AUDIT-LAKE-REPLAY-ORACLE-001).
        if capture is not None:
            _drain_capture(capture.drain_pair(instrument.symbol_name, interval))
        # Quarantine-cause nook: WHY this pair was dropped (the bare failed=N count
        # otherwise hides the terminal Groww error).
        _LOG.warning(
            "ingest.quarantine",
            pull_id=pull_id,
            symbol=instrument.symbol_name,
            interval=interval,
            error=str(exc),
        )
        if on_progress is not None:
            on_progress(label, 0, True)

    # Per-underlying rollup — INFO, one line per instrument — in a DETERMINISTIC
    # post-pass grouped + sorted by symbol_name, so the log order is reproducible
    # regardless of worker completion order (Rule #4).
    failed_by_pair = {(f.symbol, f.interval) for f in failures}
    for instrument in sorted(instruments, key=lambda i: i.symbol_name):
        symbol = instrument.symbol_name
        completed = quarantined = inst_bars = inst_windows = 0
        inst_retried = inst_still_failed = 0
        for interval in intervals:
            if (symbol, interval) in failed_by_pair:
                quarantined += 1
                continue
            report = pairs[symbol, interval]
            completed += 1
            inst_bars += report.bars_appended
            inst_windows += report.windows
            inst_retried += report.retried_windows
            inst_still_failed += report.still_failed_windows
        _LOG.info(
            "ingest.underlying",
            pull_id=pull_id,
            symbol=symbol,
            series_completed=completed,
            series_failed=quarantined,
            bars_appended=inst_bars,
            windows=inst_windows,
            retried_windows=inst_retried,
            still_failed_windows=inst_still_failed,
        )
    # Final replay-capture drain: any records not claimed by a pair drain (e.g. a
    # pair whose fan-out outcome never arrived) land on the chain before the run
    # closes. HONEST envelope: this bounds capture memory to the run ONLY on the
    # synchronous path — under the default-ON async fetch pipeline / write pool a
    # window's sink.append (where records are buffered) runs on a background
    # consumer thread that may still be queued when this drain fires, so the pull
    # owner MUST flush once more via `flush_capture_to_audit` AFTER draining the
    # pipeline + write consumer (the fourth seam; cli.py cmd_fetch's finally) or
    # those records are stranded and the capture log is structurally incomplete.
    if capture is not None:
        _drain_capture(capture.drain_remaining())
    maybe_audit(
        audit,
        "INGEST_RUN_COMPLETE",
        f"pull:{pull_id}",
        {
            "pairs_ok": len(pairs),
            "pairs_failed": len(failures),
            "total_bars": sum(report.bars_appended for report in pairs.values()),
        },
    )
    run_report = GrowwIngestReport(pairs=pairs, failures=tuple(failures))
    # VISIBLE settled-window skips (DEC-WINDOW-LEDGER-RESCUE-001): ONE INFO line per
    # driver pass with the AGGREGATE count — the per-window skip stays DEBUG
    # (ingest.py logs one per window, far too chatty at INFO for a universe pull),
    # but the operator could previously never SEE the no-vendor-hit skip engaging at
    # the default log level. Emitted unconditionally (0 is an honest "nothing was
    # skippable"), on the controller thread, after the fan-out drain.
    _LOG.info(
        "ingest.windows_skipped_settled",
        pull_id=pull_id,
        windows_skipped_settled=run_report.skipped_windows,
        pairs=len(pairs),
    )
    return run_report


@dataclass(frozen=True, slots=True)
class SpotIngestWithRetry:
    """A pull's original report + its post-run auto-retry outcome.

    What :func:`run_groww_spot_ingest_with_retry` returns: the FIRST full-pull
    :class:`GrowwIngestReport` plus the
    :class:`~brutex.adapters.ingest_common.RetryOutcome` of the leftover-window
    recovery rounds (``None`` when no retry ran).
    """

    report: GrowwIngestReport
    retry: RetryOutcome | None

    @property
    def still_failed_windows(self) -> int:
        """Transient windows STILL failing after the auto-retry (or the original, if none ran).

        The retry's ``final_still_failed`` when a retry ran, else the original
        pull's ``still_failed_windows``. Zero ⇒ the lake is complete for this run.
        """
        if self.retry is not None:
            return self.retry.final_still_failed
        return self.report.still_failed_windows


def run_groww_spot_ingest_with_retry(
    adapter: MarketAdapter,
    sink: BarSink,
    *,
    instruments: Iterable[Instrument],
    intervals: Sequence[str],
    start: date,
    end: date,
    pace: Pacer | None = None,
    ledger: WindowLedger | None = None,
    on_rate_limit: Callable[[], None] | None = None,
    on_timeout: Callable[[], None] | None = None,
    on_success: Callable[[], None] | None = None,
    on_progress: ProgressHook | None = None,
    run_parallel: RunParallel | None = None,
    auto_retry: bool = True,
    max_retry_rounds: int = _DEFAULT_MAX_RETRY_ROUNDS,
    retry_backoff: Callable[[int], None] | None = None,
    audit: AuditWriter | None = None,
    on_window: WindowTick | None = None,
    symbol_windows: SpotWindows | None = None,
    write_pool: WriteConsumer | None = None,
    fetch_pipeline: FetchPipeline | None = None,
    detect_completeness_gaps: bool = False,
    observed_holidays: frozenset[date] = frozenset(),
    empty_breaker: EmptySettleBreaker | None = None,
    all_filtered_breaker: AllFilteredBreaker | None = None,
    storm_breaker: ErrorStormBreaker | None = None,
    write_breaker: WriteHealthBreaker | None = None,
    floor_learner: VendorFloorLearner | None = None,
    market_closed: Callable[[], bool] | None = None,
    capture: AuditCaptureSink | None = None,
) -> SpotIngestWithRetry:
    """Run the spot pull, then auto-retry any leftover failed windows.

    Run one full :func:`run_groww_spot_ingest` pass, and — when it leaves
    TRANSIENT residual windows (``still_failed_windows`` > 0) AND *auto_retry* is
    on — re-run additional bounded rounds via the SHARED
    :func:`~brutex.adapters.ingest_common.retry_failed_windows` engine (CLAUDE.md
    §11 COMMON — that round-loop is vendor-neutral; it reads only
    ``still_failed_windows`` / ``auth_aborted`` / ``failures`` off the report, all
    of which :class:`GrowwIngestReport` exposes). Each round re-fetches ONLY the
    still-failed windows through the SAME *ledger* skip authority (incremental +
    idempotent) and the SAME *pace* bucket (rate-safe).

    **Honest envelope (Golden Rule #10):** the retry is O(failed_windows), NOT
    O(1)-total; the round count is bounded by *max_retry_rounds* (never infinite);
    every round is rate-safe; and a Groww auth death halts the loop (re-auth needed).

    *symbol_windows* is forwarded verbatim to every :func:`run_groww_spot_ingest`
    pass (each retry round honours the SAME opt-in per-symbol bounds). The SPOT pull
    paths pass ``None`` since DEC-SPOT-INTERVAL-DECOUPLE-001 (MIGRATION.md #851), so
    each round re-pulls only the still-failed windows of the full ``[start, end)``
    range.

    *write_pool* (DEC-FETCH-WRITE-OVERLAP-LEVER1-001) is forwarded verbatim to every
    pass, so each retry round ALSO routes its writes through the same background
    consumer — preserving the settle-after-durable ledger ordering. ``None`` (default)
    keeps the synchronous write path.

    *fetch_pipeline* (DEC-GROWW-FETCH-ASYNC-SEPARATION-001) is likewise forwarded
    verbatim to every pass, so each retry round ALSO hands its tails off to the same
    background consumer pool. ``None`` (default) keeps the fully-synchronous path. The
    caller drains it ONCE at pull end (after the LAST pass), so every queued window is
    processed + ledger-recorded before the final ledger flush.

    *all_filtered_breaker* (DEC-ALLFILTERED-MASS-BREAKER-001): ONE instance spans
    the whole pull's retry rounds — a ``None`` resolves a single fresh
    env-configured breaker HERE (review round 4; the storm_breaker/floor_learner
    whole-pull precedent) and the SAME instance is threaded into every pass, so
    the trip LATCHES across retry rounds and the "settles halted for this pull"
    alarm promise holds through them. The superseded per-pass fresh resolve let a
    sub-floor retry pass (fewer re-attempted windows than the observation floor)
    bump a suppressed window's streak to threshold and settle it SKIPPED minutes
    AFTER the CRITICAL alarm — un-rescinded and un-alarmed. Production callers
    (the dashboard pull + the CLI) inject the pull-scoped instance they resolved,
    so the latch ALSO spans their outer until-clean rounds.
    *empty_breaker* (DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001) is forwarded verbatim to
    every pass. The production ``None`` makes EACH pass resolve its own fresh
    env-configured breaker (per-pass counting; no state leak across passes or pulls);
    a trip raises :class:`~brutex.adapters.empty_circuit_breaker.MassEmptySettleHalt`
    out of this function — the pull-wide halt, never a retryable residual.

    *storm_breaker* (DEC-VENDOR-DOWN-BREAKER-001) is forwarded verbatim to every pass:
    ONE instance spans the whole pull's retry rounds (a real vendor outage persists
    across rounds; any success resets it, so a recovering vendor never trips), and its
    trip (:class:`~brutex.adapters.error_storm_breaker.VendorDownError` /
    :class:`~brutex.adapters.groww.errors.GrowwCredentialDeadError`) raises out of
    this function as a pull-wide halt. The production ``None`` default keeps every
    dry-run / synthetic / test caller un-armed.

    *write_breaker* (DEC-WRITE-HEALTH-BREAKER-001) is forwarded verbatim to every
    pass. The production ``None`` makes EACH pass resolve its own fresh
    env-configured breaker (per-pass accounting; no wedge-history leak across passes);
    a volume-wedged trip raises ``LakeWriteHalt("volume_wedged")`` out of this
    function — the pull-wide graceful halt, never a retryable residual.
    *floor_learner* (DEC-VENDOR-FLOOR-LAZY-PROBE-001) is forwarded verbatim to every
    pass: ONE instance spans the whole pull's retry rounds, so its single-flight
    per-symbol probe budget holds across rounds (a retry round re-observing the same
    boundary-empty window never re-probes). ``None`` (the default) keeps every
    dry-run / synthetic / test caller un-armed.
    *market_closed* (DEC-GROWW-MARKET-HOURS-GUARD-001) is forwarded verbatim to every
    pass, so a retry round that begins after the market opens halts on the first
    window it tries (:class:`~brutex.data.errors.MarketHoursHalt`). ``None`` (the
    default) never gates.

    *capture* (DEC-AUDIT-LAKE-REPLAY-ORACLE-001) is forwarded verbatim to every
    pass: ONE capture sink (and ONE attached
    :class:`~brutex.data.audit_capture.DedupJournal`) spans all retry rounds, so a
    retry pass's idempotent re-appends admit zero new ``add`` rows and every
    pass's replay records drain onto the same chain at the controller seams.
    In async-pipeline mode the caller additionally owns the post-drain
    :func:`flush_capture_to_audit` seam (see :func:`run_groww_spot_ingest`'s
    *capture* envelope). ``None`` (the default) is byte-identical to today.

    Returns
    -------
    SpotIngestWithRetry
        The original :class:`GrowwIngestReport` plus the
        :class:`~brutex.adapters.ingest_common.RetryOutcome` (``None`` when no
        retry ran).
    """
    # Local import: the auto-retry engine is the shared, vendor-neutral round-loop
    # (CLAUDE.md §11 COMMON — one engine for both vendors); imported here, paid once
    # per pull, never on the hot path.
    from brutex.adapters.ingest_common import retry_failed_windows

    # ONE mass ALL-FILTERED breaker across EVERY pass of this call (review round 4,
    # DEC-ALLFILTERED-MASS-BREAKER-001): resolved HERE — not per pass — so the trip
    # LATCHES across the retry rounds. The per-pass fresh resolve broke the alarm's
    # pull-wide promise: a retry pass re-attempts exactly the FAILED windows a
    # tripped pass-1 breaker suppressed, and when that re-attempt set sits below
    # the observation/series floors its FRESH breaker never tripped — the window
    # bumped to threshold and settled SKIPPED post-alarm, un-rescinded. Fresh per
    # CALL still holds (Golden Rule #7: no state leak across pulls).
    af_breaker = (
        all_filtered_breaker if all_filtered_breaker is not None else resolve_all_filtered_breaker()
    )

    def _one_pass() -> GrowwIngestReport:
        """Run ONE full spot-ingest pass with the original call's bindings."""
        return run_groww_spot_ingest(
            adapter,
            sink,
            instruments=instruments,
            intervals=intervals,
            start=start,
            end=end,
            pace=pace,
            ledger=ledger,
            on_rate_limit=on_rate_limit,
            on_timeout=on_timeout,
            on_success=on_success,
            on_progress=on_progress,
            run_parallel=run_parallel,
            audit=audit,
            on_window=on_window,
            symbol_windows=symbol_windows,
            write_pool=write_pool,
            fetch_pipeline=fetch_pipeline,
            detect_completeness_gaps=detect_completeness_gaps,
            observed_holidays=observed_holidays,
            empty_breaker=empty_breaker,
            all_filtered_breaker=af_breaker,
            storm_breaker=storm_breaker,
            write_breaker=write_breaker,
            floor_learner=floor_learner,
            market_closed=market_closed,
            capture=capture,
        )

    report = _one_pass()
    # Only spend a retry round when auto-retry is ON and the original pull left a
    # TRANSIENT residual (a clean pull / one whose only failures were terminal
    # needs no re-pull — those are not retryable).
    if not auto_retry or report.still_failed_windows == 0:
        return SpotIngestWithRetry(report=report, retry=None)
    # No ``is_known_gap`` predicate: Groww has no no-data marker (Golden Rule #1 —
    # no invented vendor code), so the shared engine's default treats nothing as a
    # known gap (every Groww terminal failure is a uniform quarantine).
    retry = retry_failed_windows(
        _one_pass,
        max_rounds=max_retry_rounds,
        backoff=retry_backoff,
    )
    return SpotIngestWithRetry(report=report, retry=retry)


__all__ = [
    "GrowwIngestReport",
    "PairFailure",
    "ProgressHook",
    "SpotIngestWithRetry",
    "SpotJob",
    "flush_capture_to_audit",
    "run_groww_spot_ingest",
    "run_groww_spot_ingest_with_retry",
    "with_paced_client",
]
