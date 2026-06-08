"""Dhan options rolling-fan-out — pull the whole option universe (LAKE surface).

The orchestration the cli/dashboard call to fill the **options** lake
(DEC-DHAN-PULL-SCOPE-001, MIGRATION #348): for each underlying, fan out over
the ATM strike window × its **per-kind** expiry flags × the expiry codes ×
``CALL``/``PUT``, driving the proven
:func:`~brutex.adapters.dhan.option_ingest.ingest_rolling_option_series`
per series (CLAUDE.md §11 COMMON — no copy-paste of the window/parse/sink
spine). Each rolling-option series is 1-minute only (DEC-DHAN-1M-ONLY-001),
resolved **relative to the underlying's spot**, and the parser stamps the
resolved ``strike`` + ``spot`` on every bar — so every option bar is mapped
to its underlying (operator decree 2026-06-01).

Resilient like the spot fan-out: a **terminal** Dhan error on one series is
quarantined (recorded as a failure) and the run continues — one bad strike /
expiry-code never kills the universe pull. Per-window transient errors are
already retried/skipped inside ``ingest_rolling_option_series``.

Parallel fan-out (DEC-DHAN-PARALLEL-INGEST-001). The OUTER loop — one job per
**underlying** — runs through an injected
:class:`~brutex.orchestrator.universe_phases.RunParallel` (production: a bounded
:class:`~brutex.orchestrator.parallel.PoolRunParallel`) so many underlyings'
windows are in flight at once and the pull **targets/saturates UP TO ~5 r/s**
when Dhan is healthy (vs the old ~1 r/s sequential stream). Each worker runs its
underlying's existing 4-deep series fan (``flags × codes × strikes × types``)
**sequentially inside**, so the per-series spine is UNCHANGED; the shared rate
bucket (the injected ``pace``) is the governor that keeps the aggregate
``≤ 5 r/s`` (Rule #6). The shared option **sink** serialises its
dedup→write→audit tail under a single lock so the hash-chained ``AuditWriter``
advances as one writer (Rule #8; the per-series network fetch runs OUTSIDE that
lock, so parallelism is preserved). **Honest envelope (Golden Rule #10):** this
targets up to ~5 r/s — it CANNOT guarantee a constant 5 r/s (429 step-down +
latency spikes). ``run_parallel`` is injectable so tests drive a synchronous
fake. The per-underlying ``ingest.underlying`` rollup + ``on_progress`` fire on
the CONTROLLER thread in a DETERMINISTIC post-pass (sorted by ``security_id``)
so the log + progress order is reproducible (Rule #4).

The strike window matches the documented Dhan rolling-option bounds
(``docs/dhan-ref/06``): **ATM±10** for the brute-force indices (near-expiry),
**ATM±3** for stocks. The **expiry set is per-kind too**: indices fan
``WEEK``+``MONTH`` but Indian single-stock options are **monthly-only** (NSE
single stocks carry no weekly series — only some indices carry weeklies), so a
stock fans ``MONTH`` ONLY. Fanning ``WEEK`` for a stock can only ever return
empty + waste Dhan calls/quota (CLAUDE.md §2 Rule 6;
DEC-DHAN-STOCK-MONTHLY-ONLY-001). The options lake is
**LAKE-only / REFERENCE_ONLY** — the brute-force engine still sweeps only the
3 indices (CLAUDE.md §3; DEC-P0-063).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date

import structlog

from brutex.adapters.dhan.client import DhanClient
from brutex.adapters.dhan.constants import DRV_OPTION_TYPES, EXPIRY_FLAGS
from brutex.adapters.dhan.errors import DhanError
from brutex.adapters.dhan.option_ingest import (
    ALL_REQUIRED_DATA,
    JsonPoster,
    OptionBarSink,
    OptionIngestReport,
    Pacer,
    ingest_rolling_option_series,
)
from brutex.data.fetch_coverage import FetchCoverage
from brutex.data.window_ledger import WindowLedger
from brutex.observability.correlation import current_pull_id, pull_id_scope
from brutex.orchestrator.parallel import PoolRunParallel
from brutex.orchestrator.universe_phases import RunParallel

#: Structured logger — emits the per-underlying options rollup at INFO
#: (``ingest.underlying``) + the per-series terminal-failure cause at WARNING
#: (``ingest.quarantine``), both carrying the pull's ``pull_id``
#: (DEC-DHAN-INGEST-LOGGING-001). The per-window / per-series DEBUG detail is one
#: layer down in ``ingest_rolling_option_series``.
_LOG = structlog.get_logger("brutex.adapters.dhan.option_universe_ingest")

#: ATM strike-window half-widths (DEC-DHAN-PULL-SCOPE-001) — the documented
#: Dhan rolling-option bounds (``docs/dhan-ref/06``): indices ATM±10 (21
#: strikes), stocks ATM±3 (7 strikes).
INDEX_STRIKE_OFFSETS: int = 10
STOCK_STRIKE_OFFSETS: int = 3

#: Per-kind ``expiryFlag`` fan-out set for a SINGLE STOCK
#: (DEC-DHAN-STOCK-MONTHLY-ONLY-001). Indian single-stock options are
#: **monthly-only** — NSE single stocks have no weekly option series; only some
#: INDICES carry weeklies. So a stock fans ``MONTH`` ONLY, while an index keeps
#: the full :data:`~brutex.adapters.dhan.constants.EXPIRY_FLAGS`
#: (``WEEK``+``MONTH``). Fanning ``WEEK`` for a stock can only ever return empty
#: → wasted Dhan calls + quota (CLAUDE.md §2 Rule 6); this set elides them — it
#: only ever REDUCES the fan-out, never adds. (Microstructure fact paired with
#: the decision ID per Golden Rule #1 — no hallucination.)
STOCK_EXPIRY_FLAGS: tuple[str, ...] = ("MONTH",)

#: The fallback / **MONTH** expiry depths — Near, Next, Far
#: (DEC-DHAN-EXPIRY-CODES-001). `/charts/rollingoption` documents
#: ``expiryCode`` **Allowed values: 1, 2, 3** (1-indexed for this endpoint;
#: the general Annexure indexes the same three depths 0/1/2). Verified from the
#: operator's 2026-06-01 Dhan docs + live 200-OK probes — this supersedes the
#: earlier unverified "4 expiryCodes" assumption (a Groww carryover). A code the
#: vendor rejects is still **safe**: the per-series quarantine records it and the
#: run continues (never fatal). MONTH stays at 1/2/3 — the operator's live probe
#: showed MONTH ``expiryCode 4`` returns ``400 DH-905`` (no 4th monthly depth).
DEFAULT_EXPIRY_CODES: tuple[int, ...] = (1, 2, 3)

#: The **WEEK** expiry depths — codes 1..8 (DEC-DHAN-WEEKLY-EXPIRY-DEPTH-001,
#: operator decision 2026-06-05). Indian index weeklies historically listed many
#: simultaneous weekly expiries (the operator's live probe pulled real
#: ``expiryCode 8`` data for 2021-06; recent windows return empty past the few
#: live weeks). So a 2021→ pull fans codes 1..8 to capture the full historical
#: weekly ladder; any code with no contract for a window simply returns an empty
#: response → 0 bars written, no audit row (the sink's existing no-op), so the
#: extra far codes never store junk — they only ever ADD real history where it
#: exists (Golden Rule #1). The cap is 8 — the operator-observed historical max.
WEEKLY_EXPIRY_CODES: tuple[int, ...] = (1, 2, 3, 4, 5, 6, 7, 8)


def expiry_codes_for_flag(expiry_flag: str) -> tuple[int, ...]:
    """Return the expiry-code fan-out for *expiry_flag* (per-flag, never mixed).

    ``WEEK`` → :data:`WEEKLY_EXPIRY_CODES` (1..8, the deep historical weekly
    ladder); every other flag (``MONTH``) → :data:`DEFAULT_EXPIRY_CODES` (1..3,
    Near/Next/Far — MONTH has no 4th depth). DEC-DHAN-WEEKLY-EXPIRY-DEPTH-001.
    """
    return WEEKLY_EXPIRY_CODES if expiry_flag == "WEEK" else DEFAULT_EXPIRY_CODES


#: The report key for one fanned-out series.
SeriesKey = tuple[str, str, int, str, str]

#: Live-progress hook fired once per **underlying** after its whole fan-out is
#: done: ``(label, bars_appended, any_failed)``. Lets a caller stream real-time
#: progress (e.g. into a ``PullProgress`` tracker) without coupling this layer
#: to the dashboard (DEC-PULL-PROGRESS-TRACKER-001).
ProgressHook = Callable[[str, int, bool], None]


def strike_window(offsets: int) -> list[str]:
    """Return the ordered ATM strike window ``ATM, ATM±1 … ATM±offsets``.

    Parameters
    ----------
    offsets
        Half-width of the window (``10`` for indices, ``3`` for stocks). ``0``
        yields just ``["ATM"]``.

    Returns
    -------
    list[str]
        ``2 * offsets + 1`` relative-strike strings (``"ATM"``, ``"ATM+1"``,
        ``"ATM-1"`` …), in the rolling-option ``strike`` format.

    Raises
    ------
    ValueError
        If *offsets* is negative.
    """
    if offsets < 0:
        raise ValueError(f"offsets must be >= 0, got {offsets}")
    strikes = ["ATM"]
    for i in range(1, offsets + 1):
        strikes.append(f"ATM+{i}")
        strikes.append(f"ATM-{i}")
    return strikes


@dataclass(frozen=True, slots=True)
class OptionUnderlying:
    """One underlying to fan out over (its id, segment, instrument, width, expiry set)."""

    security_id: str
    exch_id: str  # ``NSE`` / ``BSE`` — promoted to the expired-options table
    underlying_symbol: str  # engine symbol (e.g. ``NIFTY``) — table column
    exchange_segment: str
    instrument: str  # ``OPTIDX`` (index) / ``OPTSTK`` (stock)
    strike_offsets: int
    #: Per-kind expiry-flag fan-out set (DEC-DHAN-STOCK-MONTHLY-ONLY-001):
    #: ``EXPIRY_FLAGS`` (``WEEK``+``MONTH``) for an index, ``STOCK_EXPIRY_FLAGS``
    #: (``MONTH`` only) for a single stock. Defaults to the index set so a
    #: hand-built underlying never silently drops a flag.
    expiry_flags: tuple[str, ...] = EXPIRY_FLAGS


#: One flattened series job: the input-underlying index (so per-underlying rollups
#: re-group deterministically) + the underlying + its 4 fan-out coordinates
#: (DEC-DHAN-OPTION-SERIES-FANOUT-001).
_SeriesJob = tuple[int, OptionUnderlying, str, int, str, str]


def _with_paced_client(client: JsonPoster, pace: Pacer | None) -> JsonPoster:
    """Return *client* with its ``pace`` wired to the shared bucket (DEC-DHAN-RATE-REALTIME-GUARANTEE-001).

    The options analog of the spot ``run_ingest._with_paced_client``: the option
    ``post_json`` fast-retry (a `rollingoption` 5xx/timeout) must re-acquire a
    bucket token instead of bypassing the limiter, so EVERY physical option POST
    (initial via the per-window pace + each retry via this hook) is bucket-bounded
    ⇒ ≤ ``current_rps`` ≤ 5 (CLAUDE.md §2 Rule 6). A no-op when unthrottled
    (*pace* ``None``) or for a non-:class:`DhanClient` poster (test fakes); the
    frozen dataclass is rebuilt via :func:`replace`, never mutated.
    """
    if pace is None or not isinstance(client, DhanClient):
        return client
    return replace(client, pace=pace)


@dataclass(frozen=True, slots=True)
class OptionSeriesFailure:
    """A single ``(underlying, flag, code, strike, type)`` series that failed terminally."""

    security_id: str
    expiry_flag: str
    expiry_code: int
    strike: str
    drv_option_type: str
    error: str


@dataclass(slots=True)
class _UnderlyingResult:
    """One parallel worker's outcome for a single underlying's whole fan-out.

    Carries the per-series reports + quarantined failures it produced PLUS the
    pre-aggregated rollup counts, so the controller thread can merge into the
    universe report, emit the deterministic ``ingest.underlying`` rollup, and
    fire ``on_progress`` without re-deriving anything (DEC-DHAN-PARALLEL-INGEST-001).
    """

    series: dict[SeriesKey, OptionIngestReport] = field(default_factory=dict)
    failures: list[OptionSeriesFailure] = field(default_factory=list)
    bars: int = 0
    windows: int = 0
    completed: int = 0
    quarantined: int = 0
    any_failed: bool = False


@dataclass(frozen=True, slots=True)
class OptionsUniverseReport:
    """Aggregate outcome of a :func:`run_dhan_options_ingest` pass."""

    series: dict[SeriesKey, OptionIngestReport]
    failures: tuple[OptionSeriesFailure, ...]

    @property
    def windows(self) -> int:
        """Total fetch windows across all successful series."""
        return sum(r.windows for r in self.series.values())

    @property
    def bars_appended(self) -> int:
        """Total option bars appended across all successful series."""
        return sum(r.bars_appended for r in self.series.values())

    @property
    def transient_failures(self) -> int:
        """Total per-window transient skips across all successful series."""
        return sum(r.transient_failures for r in self.series.values())

    @property
    def skipped_windows(self) -> int:
        """Total windows skipped as already-pulled across all series (DEC-FETCH-COVERAGE-001)."""
        return sum(r.skipped_windows for r in self.series.values())

    @property
    def series_count(self) -> int:
        """Number of series that completed (non-quarantined)."""
        return len(self.series)

    @property
    def failed_series(self) -> int:
        """Number of series quarantined on a terminal error."""
        return len(self.failures)


def run_dhan_options_ingest(
    client: JsonPoster,
    sink: OptionBarSink,
    *,
    underlyings: Sequence[OptionUnderlying],
    start: date,
    end: date,
    expiry_flags: Sequence[str] | None = None,
    expiry_codes: Sequence[int] | None = None,
    option_types: Sequence[str] = DRV_OPTION_TYPES,
    required_data: Sequence[str] = ALL_REQUIRED_DATA,
    pace: Pacer | None = None,
    on_rate_limit: Pacer | None = None,
    on_timeout: Pacer | None = None,
    on_success: Pacer | None = None,
    on_progress: ProgressHook | None = None,
    on_series_tick: Callable[[], None] | None = None,
    coverage: FetchCoverage | None = None,
    ledger: WindowLedger | None = None,
    run_parallel: RunParallel | None = None,
) -> OptionsUniverseReport:
    """Fan out the rolling-option pull over the whole options universe.

    The runner is mapped over individual SERIES — one job per
    ``(underlying, expiry_flag, expiry_code, strike, drv_option_type)`` across the
    flattened ``strike_window(offsets) × (its per-kind expiry flags) × expiry_codes ×
    option_types`` worklist — so EVERY series is in flight under the pool, not just
    one per underlying (targets/saturates UP TO ~5 r/s when Dhan is healthy, even
    for a single-symbol pull). Each job drives
    :func:`~brutex.adapters.dhan.option_ingest.ingest_rolling_option_series`
    per series (1-minute, session-filtered, spot-stamped). A **terminal** Dhan
    error on one series is quarantined into ``failures`` and the run continues;
    the per-underlying rollup + ``on_progress`` still fire ONCE per underlying in a
    deterministic ``security_id``-sorted post-pass (DEC-DHAN-OPTION-SERIES-FANOUT-001).
    The shared rate bucket (the *pace* hook) bounds the aggregate to ``≤ 5 r/s``
    (Rule #6); the shared sink serialises its dedup→write→audit tail so the
    hash-chained audit advances as one writer (Rule #8). **Honest envelope
    (Golden Rule #10):** this targets ~5 r/s — it cannot guarantee a constant
    5 r/s (429 step-down + latency spikes).

    Parameters
    ----------
    client, sink
        The Dhan JSON poster and the injected :class:`OptionBar` write sink.
        Both are shared across workers; the concrete sink's dedup→write→audit is
        now single-writer-locked (DEC-DHAN-PARALLEL-INGEST-001).
    underlyings
        The underlyings to fan out (each carries its own ``strike_offsets`` —
        ``INDEX_STRIKE_OFFSETS`` / ``STOCK_STRIKE_OFFSETS``).
    start, end
        Half-open date range, shared by every series.
    expiry_flags
        Optional **uniform override** for the expiry-flag axis. ``None`` (the
        default) honours each underlying's own per-kind
        :attr:`OptionUnderlying.expiry_flags` (index ``WEEK``+``MONTH``, stock
        ``MONTH``-only; DEC-DHAN-STOCK-MONTHLY-ONLY-001). Passing a sequence
        forces that exact set on **every** underlying (custom / test pulls).
    expiry_codes, option_types
        The remaining fan-out axes. ``expiry_codes`` defaults to ``None`` →
        **per-flag** depth (WEEK fans codes 1..8, MONTH 1..3;
        DEC-DHAN-WEEKLY-EXPIRY-DEPTH-001) so a 2021→ pull captures the deep
        historical weekly ladder without sending MONTH a rejected 4th code.
        Passing an explicit sequence forces that exact set on EVERY flag (a
        uniform override for custom / test pulls). ``option_types`` defaults to
        ``CALL``/``PUT``.
    required_data
        Fields to request (defaults to every documented field).
    pace
        Optional per-window pacing hook, threaded into each series. Under the
        fan-out this is the SHARED rate bucket every worker acquires from — the
        single governor that bounds the aggregate to ``≤ 5 r/s`` (Rule #6).
    on_progress
        Optional live-progress hook fired once per **underlying** after its
        whole fan-out completes: ``(security_id, bars_appended, any_failed)``.
        Fired on the CONTROLLER thread in a DETERMINISTIC post-pass (sorted by
        ``security_id``) so the hook need not be thread-safe and the order is
        reproducible (DEC-PULL-PROGRESS-TRACKER-001, Rule #4).
    on_series_tick
        Optional **observability-only** zero-arg hook fired once per SERIES as
        each outcome is drained on the controller thread — far finer than the
        per-underlying ``on_progress`` (DEC-OPTIONS-LIVE-STATUS-001). A
        single-underlying pull fires ``on_progress`` only at the very end, so its
        lake-backed status surface would otherwise stay frozen for the whole
        fan-out; this lets the caller refresh that surface mid-pull (e.g. a
        THROTTLED summary-cache ``mark_dirty``, see
        :func:`brutex.observability.throttle.make_throttled`). It NEVER changes
        what is fetched, parsed, sunk, or the rate — it only signals "a series
        finished". ``None`` (the default) is a no-op.
    run_parallel
        The concurrency primitive mapping the per-underlying fetcher over the
        worklist (a :class:`~brutex.orchestrator.universe_phases.RunParallel`).
        ``None`` (the default) builds a bounded
        :class:`~brutex.orchestrator.parallel.PoolRunParallel`; callers size the
        pool via :func:`~brutex.orchestrator.parallel.workers_to_saturate` and
        inject it. Tests inject a synchronous fake (mirrors
        :func:`brutex.orchestrator.universe_phases.run_two_phase`).

    Returns
    -------
    OptionsUniverseReport
        Per-series :class:`OptionIngestReport` for successes + the quarantined
        terminal failures. **Never raises** on a Dhan error. A non-Dhan
        exception (a genuine bug) is NOT swallowed — it surfaces unchanged.
    """
    pull_id = current_pull_id()
    # Rate ≤5 GUARANTEE (DEC-DHAN-RATE-REALTIME-GUARANTEE-001): wire the shared
    # bucket's *pace* into the options client so a `rollingoption` fast-retry
    # (5xx/timeout) re-acquires a token instead of bypassing the limiter — the
    # options analog of the spot fix, closing the last un-gated physical-POST path.
    client = _with_paced_client(client, pace)
    runner: RunParallel = run_parallel if run_parallel is not None else PoolRunParallel()
    underlyings = list(underlyings)

    # DEC-DHAN-OPTION-SERIES-FANOUT-001: map the parallel runner over individual
    # SERIES, not whole underlyings. Before, one job == one underlying and its
    # 4-deep series fan ran SEQUENTIALLY inside the worker — so a single-underlying
    # option pull used exactly ONE worker and crawled (~2 r/s), leaving the rest of
    # the (already 5-r/s-sized) pool idle. One job per series lets a single
    # underlying's hundreds of series saturate the pool, and multi-select /
    # whole-universe pulls fan out end to end — all still bounded to ≤ 5 r/s by the
    # ONE shared rate bucket (the *pace* hook, Rule #6) and serialised at the
    # dedup→write→audit tail by the single-writer sink (DEC-DHAN-PARALLEL-INGEST-001,
    # Rule #8). The 4-agent hostile review (MIGRATION.md DEC-DHAN-OPTION-SERIES-
    # FANOUT-001) verified the invariants: distinct series → distinct partition
    # files; strikes resolve relative-to-spot server-side (no shared client anchor);
    # the audit chain stays verifiable (arrival-order rows the oracle tolerates).
    # Honest envelope (Rule #10): the audit-log ROW ORDER is arrival-order
    # (non-deterministic); only the lake bytes + chain integrity are reproducible —
    # NOT a bit-identical audit log.
    jobs: list[_SeriesJob] = []
    for idx, underlying in enumerate(underlyings):
        strikes = strike_window(underlying.strike_offsets)
        # Per-kind expiry set: each underlying carries its own (index WEEK+MONTH,
        # stock MONTH-only; DEC-DHAN-STOCK-MONTHLY-ONLY-001). An explicit
        # ``expiry_flags`` argument is a uniform override; ``None`` (the default)
        # honours each underlying's own set.
        flags = underlying.expiry_flags if expiry_flags is None else expiry_flags
        for expiry_flag in flags:
            # Per-flag expiry-code depth (DEC-DHAN-WEEKLY-EXPIRY-DEPTH-001): WEEK
            # fans 1..8 (the deep historical weekly ladder), MONTH 1..3. An
            # explicit ``expiry_codes`` argument forces that exact set on EVERY
            # flag (uniform override for custom / test pulls); ``None`` (the
            # default) honours the per-flag depth and never mixes the two.
            codes = expiry_codes_for_flag(expiry_flag) if expiry_codes is None else expiry_codes
            for expiry_code in codes:
                for strike in strikes:
                    for drv_option_type in option_types:
                        jobs.append(
                            (idx, underlying, expiry_flag, expiry_code, strike, drv_option_type)
                        )

    def _run_series(job: _SeriesJob) -> OptionIngestReport:
        """Pull ONE option series — runs in a worker thread under the fan-out.

        Re-binds *pull_id* into THIS thread's structlog context-vars (a worker does
        NOT inherit the controller's contextvar, so without this the ``ingest.*``
        events lose their ``pull_id`` — DEC-DHAN-INGEST-LOGGING-001; the per-series
        analog of the spot path's re-bind). A terminal :class:`DhanError`
        propagates and is captured by *run_parallel* into the outcome, then
        quarantined per series on the controller thread below; a non-Dhan exception
        (a genuine bug) is re-raised unchanged.
        """
        _idx, underlying, expiry_flag, expiry_code, strike, drv_option_type = job
        with pull_id_scope(pull_id):
            return ingest_rolling_option_series(
                client,
                sink,
                security_id=underlying.security_id,
                exch_id=underlying.exch_id,
                underlying_symbol=underlying.underlying_symbol,
                exchange_segment=underlying.exchange_segment,
                instrument=underlying.instrument,
                expiry_flag=expiry_flag,
                expiry_code=expiry_code,
                strike=strike,
                drv_option_type=drv_option_type,
                start=start,
                end=end,
                required_data=required_data,
                pace=pace,
                on_rate_limit=on_rate_limit,
                on_timeout=on_timeout,
                on_success=on_success,
                coverage=coverage,
                ledger=ledger,
            )

    # Drain the SERIES fan-out, re-aggregating into a per-underlying bucket keyed
    # by INPUT INDEX (so two hand-built underlyings sharing a security_id each keep
    # their own rollup, exactly as the prior per-underlying list did). The merge +
    # the deterministic rollup/on_progress post-pass fire on THIS (controller)
    # thread, so neither needs to be thread-safe (Rule #4).
    buckets: list[_UnderlyingResult] = [_UnderlyingResult() for _ in underlyings]
    for outcome in runner(_run_series, jobs):
        # Live status tick (DEC-OPTIONS-LIVE-STATUS-001): fire the throttled
        # observability hook as each series outcome is drained on THIS controller
        # thread — the series' lake write already happened in the worker before
        # the outcome was yielded, so the lake reflects it. This is what makes a
        # single-underlying pull's lake-backed coverage surface tick live
        # mid-fan-out instead of only at the per-underlying ``on_progress`` at the
        # very end. Pure observability — it does not touch fetch/parse/sink/rate.
        if on_series_tick is not None:
            on_series_tick()
        idx, underlying, expiry_flag, expiry_code, strike, drv_option_type = outcome.item
        result = buckets[idx]
        key: SeriesKey = (
            underlying.security_id,
            expiry_flag,
            expiry_code,
            strike,
            drv_option_type,
        )
        if not outcome.ok:
            exc = outcome.exc
            if exc is None:  # pragma: no cover — a failed outcome always carries exc
                continue
            # Only a terminal DhanError is quarantined per series; a non-Dhan
            # exception escaping the worker is a genuine bug → surface it.
            if not isinstance(exc, DhanError):
                raise exc
            result.failures.append(
                OptionSeriesFailure(
                    security_id=underlying.security_id,
                    expiry_flag=expiry_flag,
                    expiry_code=expiry_code,
                    strike=strike,
                    drv_option_type=drv_option_type,
                    error=str(exc),
                )
            )
            result.any_failed = True
            result.quarantined += 1
            # Quarantine-cause nook: WHY this series was dropped, full coordinate.
            _LOG.warning(
                "ingest.quarantine",
                pull_id=pull_id,
                security_id=underlying.security_id,
                underlying_symbol=underlying.underlying_symbol,
                expiry_flag=expiry_flag,
                expiry_code=expiry_code,
                strike=strike,
                drv_option_type=drv_option_type,
                error=str(exc),
            )
            continue
        report = outcome.result
        if report is None:  # pragma: no cover — ok=True always carries a result
            continue
        result.series[key] = report
        result.bars += report.bars_appended
        result.windows += report.windows
        result.completed += 1

    series: dict[SeriesKey, OptionIngestReport] = {}
    failures: list[OptionSeriesFailure] = []
    # Deterministic post-pass: emit the per-underlying rollup + fire on_progress in
    # (security_id, input-index) order, so the log + progress stream is reproducible
    # regardless of which series finished first (Rule #4). Each underlying's series
    # are merged in canonical coordinate order so the returned ``series`` dict
    # ordering is reproducible too (workers insert in completion order otherwise).
    for idx in sorted(range(len(underlyings)), key=lambda i: (underlyings[i].security_id, i)):
        underlying = underlyings[idx]
        result = buckets[idx]
        for skey in sorted(result.series, key=lambda k: tuple(str(part) for part in k)):
            series[skey] = result.series[skey]
        failures.extend(result.failures)
        # Per-underlying rollup — INFO, one line per underlying (not per series),
        # so a 16k-series fan-out's progress is visible without the DEBUG firehose.
        _LOG.info(
            "ingest.underlying",
            pull_id=pull_id,
            security_id=underlying.security_id,
            underlying_symbol=underlying.underlying_symbol,
            instrument=underlying.instrument,
            series_completed=result.completed,
            series_failed=result.quarantined,
            bars_appended=result.bars,
            windows=result.windows,
        )
        # Fire the live-progress hook once the WHOLE underlying fan-out is done.
        if on_progress is not None:
            on_progress(underlying.security_id, result.bars, result.any_failed)
    return OptionsUniverseReport(series=series, failures=tuple(failures))


__all__ = [
    "DEFAULT_EXPIRY_CODES",
    "INDEX_STRIKE_OFFSETS",
    "STOCK_EXPIRY_FLAGS",
    "STOCK_STRIKE_OFFSETS",
    "WEEKLY_EXPIRY_CODES",
    "OptionSeriesFailure",
    "OptionUnderlying",
    "OptionsUniverseReport",
    "ProgressHook",
    "SeriesKey",
    "expiry_codes_for_flag",
    "run_dhan_options_ingest",
    "strike_window",
]
