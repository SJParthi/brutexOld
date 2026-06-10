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
already retried/skipped inside ``ingest_rolling_option_series``, and a DH-905
"no data" window settles as a known gap WITHOUT quarantining the series
(DEC-OPTION-DH905-CONTINUE-001) — a fully-dead series completes GAP-ONLY in one
pass and is surfaced via ``gap_only_series`` + the ``ingest.known_gap`` INFO.

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

from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime

import structlog

from brutex.adapters.dhan.auth_guard import AuthGuard
from brutex.adapters.dhan.client import DhanClient
from brutex.adapters.dhan.constants import DRV_OPTION_TYPES, EXPIRY_FLAGS
from brutex.adapters.dhan.errors import DhanError, is_auth_failure, is_no_data_error
from brutex.adapters.dhan.option_ingest import (
    ALL_REQUIRED_DATA,
    JsonPoster,
    OptionBarSink,
    OptionIngestReport,
    Pacer,
    ingest_rolling_option_series,
)
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
#: done: ``(label, bars_appended, any_failed, gap)``. Lets a caller stream
#: real-time progress (e.g. into a ``PullProgress`` tracker) without coupling
#: this layer to the dashboard (DEC-PULL-PROGRESS-TRACKER-001). ``gap`` mirrors
#: the spot hook's benign-DH-905 flag (DEC-DHAN-NO-DATA-GAP-COUNT-001): an
#: underlying whose ONLY empty series were DH-905 no-data boundaries reads as a
#: no-data gap, not a failure (a real failure on any series outranks it).
ProgressHook = Callable[[str, int, bool, bool], None]


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
    #: True iff at least one series carried a benign Dhan ``DH-905`` no-data
    #: boundary (NOT a real failure — DEC-DHAN-NO-DATA-GAP-COUNT-001 options arm).
    #: Set when a report carries settled known-gap windows
    #: (DEC-OPTION-DH905-CONTINUE-001) or — defensively — on a drain-side DH-905
    #: quarantine.
    any_gap: bool = False
    #: Series that completed GAP-ONLY: committed whole with ZERO bars and ≥1
    #: settled DH-905 known-gap window (DEC-OPTION-DH905-CONTINUE-001) — Dhan
    #: genuinely has no data for them. Counted inside ``completed`` (the series
    #: IS settled); surfaced separately so the rollup + verdict stay honest.
    gap_only: int = 0
    #: Series whose transactional commit ABORTED (a cursor outran its in-run retry
    #: budget — ``report.complete`` is False; committed NOTHING, deferred to the
    #: next round/pull). Counted SEPARATELY from ``completed`` so the rollup never
    #: reads an uncommitted series as done (DEC-OPTIONS-UNTIL-CLEAN-001, hostile
    #: finding 6).
    aborted: int = 0
    #: Series short-circuited by the tripped dead-token latch (never dispatched a
    #: vendor call). NOT completed, NOT residual — a re-auth + re-pull re-fans them.
    skipped_auth: int = 0


@dataclass(frozen=True, slots=True)
class OptionsUniverseReport:
    """Aggregate outcome of a :func:`run_dhan_options_ingest` pass."""

    series: dict[SeriesKey, OptionIngestReport]
    failures: tuple[OptionSeriesFailure, ...]
    #: True iff the Dhan token died mid-pull (the shared :class:`AuthGuard`
    #: tripped) — re-auth needed; drives the loud ``auth_expired`` verdict
    #: (DEC-OPTIONS-UNTIL-CLEAN-001, the options arm of
    #: DEC-DHAN-AUTH-FAILFAST-RENEW-001).
    auth_aborted: bool = False
    #: The first dead-token cause string (``None`` unless :attr:`auth_aborted`).
    auth_cause: str | None = None
    #: Series short-circuited by the tripped latch (no vendor call fired).
    series_skipped_auth: int = 0

    @property
    def windows(self) -> int:
        """Total fetch windows across all non-quarantined series."""
        return sum(r.windows for r in self.series.values())

    @property
    def bars_appended(self) -> int:
        """Total option bars appended across all non-quarantined series."""
        return sum(r.bars_appended for r in self.series.values())

    @property
    def transient_failures(self) -> int:
        """Total per-window transient skips across all non-quarantined series."""
        return sum(r.transient_failures for r in self.series.values())

    @property
    def still_failed_windows(self) -> int:
        """Transient windows still failing after the in-run retry budget.

        The sum of every ABORTED series' deferred-window count — the retryable
        residual that drives the until-clean rounds (DEC-OPTIONS-UNTIL-CLEAN-001).
        ``0`` iff every dispatched series committed whole.
        """
        return sum(r.still_failed_windows for r in self.series.values())

    @property
    def skipped_windows(self) -> int:
        """Total windows skipped as already-pulled across all series (DEC-FETCH-COVERAGE-001)."""
        return sum(r.skipped_windows for r in self.series.values())

    @property
    def series_count(self) -> int:
        """Number of non-quarantined series (committed whole + aborted)."""
        return len(self.series)

    @property
    def aborted_series(self) -> int:
        """Series whose all-or-nothing commit ABORTED (committed nothing this pass).

        Disjoint from quarantines: an aborted series returned a report whose
        ``complete`` is False (its windows are the :attr:`still_failed_windows`
        residual). Never counted as completed (hostile finding 6).
        """
        return sum(1 for r in self.series.values() if not r.complete)

    @property
    def failed_series(self) -> int:
        """Number of series quarantined on a terminal error."""
        return len(self.failures)

    @property
    def no_data_series(self) -> int:
        """Quarantined series whose terminal error was a benign Dhan DH-905 no-data.

        Classified by the single source of truth
        :func:`~brutex.adapters.dhan.errors.is_no_data_message`
        (DEC-DHAN-NO-DATA-GAP-COUNT-001) — a data boundary, never a defect.
        DEFENSIVE-ONLY since DEC-OPTION-DH905-CONTINUE-001: the series spine
        settles a DH-905 cursor as a known gap and continues (never re-raises),
        so no production path quarantines a DH-905 series any more — this reads
        0 there and is kept only as the drain-side classifier for any other
        arrival path. AUTH OUTRANKS NO-DATA (hostile finding 1): a both-marker
        token death is a REAL failure here, never a benign gap.
        """
        from brutex.adapters.dhan.errors import is_auth_failure_message, is_no_data_message

        return sum(
            1
            for f in self.failures
            if is_no_data_message(f.error) and not is_auth_failure_message(f.error)
        )

    @property
    def real_failed_series(self) -> int:
        """Quarantined series with a GENUINE terminal error (non-DH-905, incl. auth)."""
        return self.failed_series - self.no_data_series

    @property
    def no_data_windows(self) -> int:
        """Total DH-905 known-gap windows settled this pass across all series.

        DEC-OPTION-DH905-CONTINUE-001: each is a 0-bar cursor the vendor
        definitively answered "no data" for — settled ``SKIPPED`` in the ledger,
        never a failure, never part of :attr:`still_failed_windows`.
        """
        return sum(r.no_data_windows for r in self.series.values())

    @property
    def gap_only_series(self) -> int:
        """Series that completed GAP-ONLY — whole, zero bars, ≥1 known-gap window.

        DEC-OPTION-DH905-CONTINUE-001: a fully-dead series (e.g. a far weekly
        expiry code that never listed) now settles in ONE pass and lands here —
        COMPLETE (its windows are settled vendor answers), but distinguished so
        the pull verdict can read ``known_gap_only`` instead of a lying ``clean``
        when a pull found NOTHING but gaps.
        """
        return sum(
            1
            for r in self.series.values()
            if r.complete and r.bars_appended == 0 and r.no_data_windows > 0
        )


def iter_series_jobs(
    underlyings: Sequence[OptionUnderlying],
    *,
    expiry_flags: Sequence[str] | None = None,
    expiry_codes: Sequence[int] | None = None,
    option_types: Sequence[str] = DRV_OPTION_TYPES,
) -> Iterator[_SeriesJob]:
    """Yield every series job of the fan-out — the ONE enumeration (CLAUDE.md §11).

    DEC-OPTION-WINDOW-PLAN-001 (hostile C4): the pre-pull window PLANNER and the
    fetch runner MUST consume the SAME series enumeration — including the two
    override resolutions (an explicit ``expiry_flags`` forces a uniform set vs
    each underlying's per-kind default, DEC-DHAN-STOCK-MONTHLY-ONLY-001; an
    explicit ``expiry_codes`` forces a uniform set vs the per-flag depth,
    DEC-DHAN-WEEKLY-EXPIRY-DEPTH-001) — so the PLANNED rows and the fetched
    windows can never drift. Extracted verbatim from the runner's jobs loop;
    ordering is deterministic (input order × flags × codes × strikes × types).
    """
    for idx, underlying in enumerate(underlyings):
        strikes = strike_window(underlying.strike_offsets)
        flags = underlying.expiry_flags if expiry_flags is None else expiry_flags
        for expiry_flag in flags:
            codes = expiry_codes_for_flag(expiry_flag) if expiry_codes is None else expiry_codes
            for expiry_code in codes:
                for strike in strikes:
                    for drv_option_type in option_types:
                        yield (idx, underlying, expiry_flag, expiry_code, strike, drv_option_type)


def plan_option_pull_windows(
    ledger: WindowLedger,
    underlyings: Sequence[OptionUnderlying],
    *,
    start: date,
    end: date,
    expiry_flags: Sequence[str] | None = None,
    expiry_codes: Sequence[int] | None = None,
    option_types: Sequence[str] = DRV_OPTION_TYPES,
    now: datetime | None = None,
) -> int:
    """PLAN every window of the pull's fan-out BEFORE any fetch — returns the count.

    The options arm of the spot ``plan_windows`` (DEC-OPTION-WINDOW-PLAN-001):
    every ``(series × 30-day window)`` the pull will touch gets a ``PLANNED``
    ledger row up-front, so /coverage/windows shows the full PLANNED → SUCCESS
    countdown and a never-reached window is visibly PLANNED instead of silently
    absent. Keys are built by the SAME exported
    :func:`~brutex.adapters.dhan.option_ingest.coverage_key` +
    :func:`~brutex.adapters.dhan.option_ingest.window_dates` the fetch spine
    probes, over the SAME :func:`iter_series_jobs` enumeration — planner and
    fetcher cannot drift (hostile C4; the planner-keys == fetch-keys golden test
    pins it). ``plan`` is idempotent and NEVER overwrites an existing row
    (FAILED stays FAILED, settled stays settled — window_ledger semantics,
    hostile finding 8). A never-reached window of the CURRENT tiling stays
    visibly ``PLANNED``; SUPERSEDED trailing rows of the planned series —
    keys a previous pull's shorter ``end`` produced, which this tiling can
    never emit again — are pruned afterwards
    (:meth:`~brutex.data.window_ledger.WindowLedger.prune_superseded`,
    DEC-WINDOW-TRAILING-GC-001), so the daily ``end`` advance stops minting
    one orphan row per series per day (~4.4M/yr at universe cadence).
    O(series × windows) plan + one O(rows) prune scan, once per pull, pure
    in-memory.

    Honest envelope (Golden Rule #10): a 3-index full-history pull plans ~93k
    rows (~50 MB ledger, ~3.7 s); a full-universe pull ~841k rows (~480 MB,
    ~7 s) — measured in the DEC-OPTION-WINDOW-PLAN-001 review. The prune scan
    measured 0.48 s over a 1.2M-row ledger with a ~1.4 MB series→tiles index
    (DEC-WINDOW-TRAILING-GC-001). NOT an O(1) claim; the per-window ``plan``
    probe is O(1).
    """
    from brutex.adapters.dhan.option_ingest import coverage_key, window_dates

    stamp = now if now is not None else datetime.now(UTC)
    tiles = window_dates(start, end)
    # The tiling's ISO bounds, materialised ONCE and shared by every series'
    # GC index entry (DEC-WINDOW-TRAILING-GC-001 hostile F3 — no per-window
    # re-serialisation).
    tiles_iso: dict[str, str] = {ws.isoformat(): we.isoformat() for ws, we in tiles}
    planned_tiles: dict[tuple[str, str, str], Mapping[str, str]] = {}
    planned = 0
    for job in iter_series_jobs(
        underlyings,
        expiry_flags=expiry_flags,
        expiry_codes=expiry_codes,
        option_types=option_types,
    ):
        _idx, underlying, expiry_flag, expiry_code, strike, drv_option_type = job
        for window_start, window_end in tiles:
            key = coverage_key(
                exchange_segment=underlying.exchange_segment,
                security_id=underlying.security_id,
                expiry_flag=expiry_flag,
                expiry_code=expiry_code,
                strike=strike,
                drv_option_type=drv_option_type,
                window_start=window_start,
                window_end=window_end,
            )
            # The series identity comes off the SAME key builder the fetch
            # spine probes (hostile C4 — no second folding to drift).
            planned_tiles[(key.market, key.security_id, key.interval)] = tiles_iso
            ledger.plan(key, now=stamp)
            planned += 1
    pruned = ledger.prune_superseded(planned_tiles)
    if pruned:
        _LOG.info("window_ledger.pruned_superseded", pruned=pruned, planned=planned)
    return planned


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
        whole fan-out completes: ``(security_id, bars_appended, any_failed, gap)``
        where ``gap`` flags a benign DH-905-only underlying (DEC-DHAN-NO-DATA-GAP-COUNT-001).
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
    # ONE shared "token is dead" latch per pass (DEC-OPTIONS-UNTIL-CLEAN-001 — the
    # options arm of DEC-DHAN-AUTH-FAILFAST-RENEW-001). The first series to hit a
    # dead-token error trips it AT THE SOURCE (inside ingest_rolling_option_series);
    # every series not yet dispatched then short-circuits to a no-call skip, so a
    # 16k-series fan-out stops flooding doomed paced calls within pool width of the
    # first failure. Honest envelope (Golden Rule #10): workers already mid-call
    # finish their current cursor — the latch stops NEW dispatches, it cannot
    # hard-cancel in-flight threads; their staged clean cursors are discarded by the
    # all-or-nothing abort and re-fetched after re-auth.
    auth_guard = AuthGuard()

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
    # The ONE enumeration (DEC-OPTION-WINDOW-PLAN-001 C4): the runner and the
    # pre-pull planner both consume iter_series_jobs, including its override
    # resolution — see its docstring; the inline loop moved there verbatim.
    jobs: list[_SeriesJob] = list(
        iter_series_jobs(
            underlyings,
            expiry_flags=expiry_flags,
            expiry_codes=expiry_codes,
            option_types=option_types,
        )
    )

    def _run_series(job: _SeriesJob) -> OptionIngestReport | None:
        """Pull ONE option series — runs in a worker thread under the fan-out.

        Re-binds *pull_id* into THIS thread's structlog context-vars (a worker does
        NOT inherit the controller's contextvar, so without this the ``ingest.*``
        events lose their ``pull_id`` — DEC-DHAN-INGEST-LOGGING-001; the per-series
        analog of the spot path's re-bind). A terminal :class:`DhanError`
        propagates and is captured by *run_parallel* into the outcome, then
        quarantined per series on the controller thread below; a non-Dhan exception
        (a genuine bug) is re-raised unchanged. Returns ``None`` — a no-call
        AUTH-SKIP, never folded into completed/aborted/residual — when the shared
        dead-token latch tripped before this series was dispatched
        (DEC-OPTIONS-UNTIL-CLEAN-001, hostile finding 6).
        """
        _idx, underlying, expiry_flag, expiry_code, strike, drv_option_type = job
        if auth_guard.tripped():
            return None
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
                ledger=ledger,
                auth_guard=auth_guard,
            )

    # Drain the SERIES fan-out, re-aggregating into a per-underlying bucket keyed
    # by INPUT INDEX (so two hand-built underlyings sharing a security_id each keep
    # their own rollup, exactly as the prior per-underlying list did). The merge +
    # the deterministic rollup/on_progress post-pass fire on THIS (controller)
    # thread, so neither needs to be thread-safe (Rule #4).
    buckets: list[_UnderlyingResult] = [_UnderlyingResult() for _ in underlyings]
    #: Whether the loud ``ingest.auth_aborted`` alert has fired — EXACTLY ONCE (the
    #: first dead-token quarantine); later auth failures are recorded silently to
    #: avoid the per-series WARNING flood (mirrors the spot drain,
    #: DEC-DHAN-AUTH-FAILFAST-RENEW-001).
    auth_alerted = False
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
            # The series is recorded (re-tried next pull, never masked) + counted in
            # the quarantined rollup EITHER way. AUTH OUTRANKS NO-DATA
            # (DEC-OPTION-DH905-CONTINUE-001 hostile finding 1): a token-death 400
            # can carry BOTH markers (DH-906 + DH-905), so the dead-token check
            # runs FIRST — it fires ONE loud ``ingest.auth_aborted`` alert instead
            # of a per-series WARNING flood (DEC-OPTIONS-UNTIL-CLEAN-001 / the
            # options arm of DEC-DHAN-AUTH-FAILFAST-RENEW-001). A pure ``DH-905``
            # "no data" is a benign GAP, NOT a failure
            # (DEC-DHAN-NO-DATA-GAP-COUNT-001, options arm): INFO
            # ``ingest.known_gap`` + ``any_gap``. Every other terminal error stays
            # a WARNING ``ingest.quarantine`` failure.
            result.quarantined += 1
            if is_auth_failure(exc):
                result.any_failed = True
                # The worker normally trips the latch at the source; trip here too in
                # case this exc reached the drain via another path (idempotent).
                auth_guard.trip(str(exc))
                if not auth_alerted:
                    auth_alerted = True
                    _LOG.warning(
                        "ingest.auth_aborted",
                        pull_id=pull_id,
                        security_id=underlying.security_id,
                        underlying_symbol=underlying.underlying_symbol,
                        cause=auth_guard.cause(),
                        note=(
                            "Dhan access token expired/invalid mid-pull — options pull "
                            "aborted; regenerate the 24h token at web.dhan.co (or it "
                            "will auto-renew while active next run)"
                        ),
                    )
            elif is_no_data_error(exc):
                # DEFENSIVE-ONLY since DEC-OPTION-DH905-CONTINUE-001: the series
                # spine settles a DH-905 cursor and continues (never re-raises), so
                # no production path reaches this branch — kept (and fake-runner
                # tested) so a DH-905 arriving via any OTHER path still classifies
                # as a benign gap, never a scary failure (hostile finding 8).
                result.any_gap = True
                _LOG.info(
                    "ingest.known_gap",
                    pull_id=pull_id,
                    security_id=underlying.security_id,
                    underlying_symbol=underlying.underlying_symbol,
                    expiry_flag=expiry_flag,
                    expiry_code=expiry_code,
                    strike=strike,
                    drv_option_type=drv_option_type,
                    reason=str(exc),
                    note="Dhan reports no data for this series (DH-905); re-tried next pull, not masked",
                )
            else:
                result.any_failed = True
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
        if report is None:
            # AUTH-SKIP: the latch tripped before this series was dispatched — no
            # vendor call fired. Distinct from completed/aborted/residual (hostile
            # finding 6): a re-auth + the next round/pull re-fans it.
            result.skipped_auth += 1
            continue
        result.series[key] = report
        result.bars += report.bars_appended
        result.windows += report.windows
        # Settled DH-905 known-gap windows read as a benign no-data gap on the
        # per-underlying progress hook, mirroring the old quarantine-path flag
        # (DEC-OPTION-DH905-CONTINUE-001 / DEC-DHAN-NO-DATA-GAP-COUNT-001).
        if report.no_data_windows > 0:
            result.any_gap = True
        # An ABORTED series (cursor outran its in-run retry budget) committed
        # NOTHING — it is residual work, never a completion (hostile finding 6).
        if report.complete:
            result.completed += 1
            if report.bars_appended == 0 and report.no_data_windows > 0:
                # GAP-ONLY: the whole series is settled vendor no-data — COMPLETE
                # (never re-fetched) but surfaced at INFO like the old per-series
                # quarantine was, so a dead strike/expiry stays one glance away
                # (DEC-OPTION-DH905-CONTINUE-001).
                result.gap_only += 1
                _LOG.info(
                    "ingest.known_gap",
                    pull_id=pull_id,
                    security_id=underlying.security_id,
                    underlying_symbol=underlying.underlying_symbol,
                    expiry_flag=expiry_flag,
                    expiry_code=expiry_code,
                    strike=strike,
                    drv_option_type=drv_option_type,
                    no_data_windows=report.no_data_windows,
                    note=(
                        "Dhan reports no data for this series (DH-905); all windows "
                        "settled SKIPPED — drained in one pass, never re-fetched"
                    ),
                )
        else:
            result.aborted += 1

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
            series_aborted=result.aborted,
            series_skipped_auth=result.skipped_auth,
            series_gap_only=result.gap_only,
            bars_appended=result.bars,
            windows=result.windows,
        )
        # Fire the live-progress hook once the WHOLE underlying fan-out is done.
        # ``failed`` and ``gap`` are mutually exclusive on this one unit: a real
        # failure outranks a benign DH-905 gap, so an underlying with ANY real
        # failure reads as failed; one whose only empty series were DH-905 reads
        # as a no-data gap (DEC-DHAN-NO-DATA-GAP-COUNT-001, options arm).
        if on_progress is not None:
            on_progress(
                underlying.security_id,
                result.bars,
                result.any_failed,
                result.any_gap and not result.any_failed,
            )
    return OptionsUniverseReport(
        series=series,
        failures=tuple(failures),
        auth_aborted=auth_guard.tripped(),
        auth_cause=auth_guard.cause(),
        series_skipped_auth=sum(result.skipped_auth for result in buckets),
    )


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
    "iter_series_jobs",
    "plan_option_pull_windows",
    "run_dhan_options_ingest",
    "strike_window",
]
