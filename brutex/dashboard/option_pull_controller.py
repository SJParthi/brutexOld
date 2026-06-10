"""Click-to-pull controller — ingest the EXPIRED-OPTION universe (Dhan).

The options analog of :mod:`brutex.dashboard.pull_controller` — it powers the
dashboard's **options** pull buttons (② bulk indices-options, ④ bulk
stocks-options, ⑥ per-index options, ⑧ per-stock options;
DEC-UNIVERSE-DASHBOARD-001). A single :func:`pull_options` core serves all
four: a one-element *underlyings* list is the per-symbol case, the full
universe is the bulk case (CLAUDE.md §11 COMMON — one source, no copy-paste).

For each underlying it maps the resolved :class:`~brutex.models.Instrument` to
an :class:`~brutex.adapters.dhan.option_universe_ingest.OptionUnderlying`
(index → ``OPTIDX`` / ATM±10 / ``WEEK``+``MONTH``, stock → ``OPTSTK`` / ATM±3 /
``MONTH``-only — Indian single-stock options are monthly-only;
DEC-DHAN-PULL-SCOPE-001, DEC-DHAN-STOCK-MONTHLY-ONLY-001) and drives the proven
:func:`~brutex.adapters.dhan.option_universe_ingest.run_dhan_options_ingest`
fan-out (ATM window × the underlying's per-kind expiry flags × the per-flag
expiry-code depth — WEEK codes 1..8, MONTH 1..3,
DEC-DHAN-WEEKLY-EXPIRY-DEPTH-001 — ×
``CALL``/``PUT``). Every series is **1-minute only** (DEC-DHAN-1M-ONLY-001),
resolved relative to the underlying's spot, and each bar is stamped with its
resolved ``strike`` + ``spot`` — so every option bar is mapped to its
underlying (operator decree 2026-06-01). The full ``[2021-01-01, today]``
range is reused from :func:`~brutex.dashboard.pull_controller.resolve_candle_range`.

## Offline-first

The default and CI-tested path is **dry-run**: a
:class:`~brutex.adapters.dhan.option_synthetic.SyntheticOptionClient` feeds the
real rolling-option spine and a
:class:`~brutex.data.option_sink.NullOptionBarSink` counts-but-discards, so the
whole fan-out → parse → session-filter → sink pipeline runs with NO Dhan
credentials and NO network. The LIVE wiring lives in
:func:`_build_live_options`, which is ``# pragma: no cover`` because it needs a
real token + the ``urllib`` transport (the network is blocked in CI); its pure
orchestration is exercised end-to-end through the dry-run path
(DEC-VENDOR-PIVOT-001).

## Scope (Golden Rule §3)

The options lake is **LAKE-only / REFERENCE_ONLY** (DEC-P0-063 #200): pulling
option series fills the lake for future, separately-authorised expansion. The
BRUTE-FORCE ENGINE is unchanged — it still sweeps only the 3 indices.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

import structlog

from brutex.adapters.dhan.constants import DRV_OPTION_TYPES, EXPIRY_FLAGS
from brutex.adapters.dhan.option_ingest import (
    ALL_REQUIRED_DATA,
    JsonPoster,
    OptionBarSink,
    Pacer,
)
from brutex.adapters.dhan.option_synthetic import SyntheticOptionClient
from brutex.adapters.dhan.option_universe_ingest import (
    INDEX_STRIKE_OFFSETS,
    STOCK_EXPIRY_FLAGS,
    STOCK_STRIKE_OFFSETS,
    OptionsUniverseReport,
    OptionUnderlying,
    ProgressHook,
    run_dhan_options_ingest,
)
from brutex.adapters.dhan.pull_verdict import PullVerdict, classify_pull
from brutex.adapters.dhan.until_clean import (
    AUTO_RESUME_MAX_ROUNDS,
    UntilCleanRound,
    run_until_clean,
)
from brutex.dashboard.pull_controller import (
    DHAN_HISTORY_FLOOR,
    SessionRateController,
    resolve_candle_range,
)
from brutex.data.option_sink import NullOptionBarSink
from brutex.data.window_ledger_store import (
    begin_pull_ledger,
    end_pull_ledger,
    make_live_ledger_flush,
)
from brutex.models import Instrument, InstrumentType
from brutex.observability.correlation import pull_id_scope
from brutex.orchestrator.parallel import (
    DEFAULT_LATENCY_BUDGET_S,
    PoolRunParallel,
    workers_to_saturate,
)
from brutex.orchestrator.quota_tracker import DailyQuotaExceeded
from brutex.orchestrator.universe_phases import RunParallel

if TYPE_CHECKING:
    from brutex.adapters.dhan.rate_control import DhanRateController

_LOG = structlog.get_logger(__name__)


def instrument_to_option_underlying(instrument: Instrument) -> OptionUnderlying:
    """Map a spot :class:`Instrument` to its rolling-option fan-out underlying.

    O(1): pure field projection. An **index** (``IDX``) fans out as ``OPTIDX``
    with the wide ATM±10 window AND the full ``WEEK``+``MONTH`` expiry set; an
    **equity** (``EQ``) as ``OPTSTK`` with the ATM±3 window and the
    ``MONTH``-only expiry set — Indian single-stock options are monthly-only
    (NSE single stocks have no weekly series; only some indices carry weeklies),
    so fanning ``WEEK`` for a stock would only waste Dhan calls/quota
    (DEC-DHAN-PULL-SCOPE-001; DEC-DHAN-STOCK-MONTHLY-ONLY-001). The exchange-segment is the
    underlying's exchange in the F&O segment (``NSE_FNO`` / ``BSE_FNO``), and
    the underlying's NUMERIC ``security_id`` (the Dhan ``SECURITY_ID``; the
    renamed numeric id field, ADR-0003) is the rolling option's resolution
    anchor — NOT the engine ``symbol_name`` string.

    Parameters
    ----------
    instrument
        The resolved spot underlying — an index or an F&O-eligible stock.

    Returns
    -------
    OptionUnderlying
        The fan-out spec (security id, ``*_FNO`` segment, ``OPTIDX``/``OPTSTK``,
        strike-window half-width).

    Raises
    ------
    ValueError
        If *instrument*'s ``security_id`` is not a positive ``SECURITY_ID``,
        or its type is neither ``IDX`` nor ``EQ`` (a derivative leg is never a
        fan-out underlying).
    """
    # The rolling-option API addresses the underlying by its NUMERIC Dhan
    # SECURITY_ID (e.g. 13), held on the instrument as ``security_id`` (the
    # renamed numeric id field, ADR-0003) — NOT the engine ``symbol_name``
    # ("NIFTY"). Sending the symbol made Dhan reject EVERY series — the live
    # "252 failed / 0 bars" symptom (DEC-OPTION-SECURITY-ID-FIX-001). The spot
    # adapter already uses ``security_id``; the options path now matches.
    if instrument.security_id <= 0:
        raise ValueError(
            f"{instrument.symbol_name} has no valid numeric SECURITY_ID "
            f"(security_id={instrument.security_id})"
        )
    numeric_id = str(instrument.security_id)
    if instrument.instrument is InstrumentType.IDX:
        instrument_name, offsets, flags = "OPTIDX", INDEX_STRIKE_OFFSETS, EXPIRY_FLAGS
    elif instrument.instrument is InstrumentType.EQ:
        instrument_name, offsets, flags = "OPTSTK", STOCK_STRIKE_OFFSETS, STOCK_EXPIRY_FLAGS
    else:
        raise ValueError(
            f"{instrument.symbol_name} is a {instrument.instrument.value} — only "
            "IDX / EQ underlyings have an options fan-out"
        )
    return OptionUnderlying(
        security_id=numeric_id,
        exch_id=instrument.exch_id.value,
        underlying_symbol=instrument.symbol_name,
        exchange_segment=f"{instrument.exch_id.value}_FNO",
        instrument=instrument_name,
        strike_offsets=offsets,
        expiry_flags=flags,
    )


#: A factory that builds the ``(client, sink)`` pair the options fan-out needs,
#: given the lake root. The default builds the offline synthetic pair; the live
#: path injects :func:`_build_live_options`.
OptionClientFactory = Callable[[Path], tuple[JsonPoster, OptionBarSink]]


@dataclass(frozen=True, slots=True)
class OptionSymbolResult:
    """Per-underlying series / failure / bar counts for an options pull."""

    symbol: str
    instrument: str  # ``OPTIDX`` / ``OPTSTK``
    series_completed: int
    series_failed: int
    bars_appended: int
    windows: int


@dataclass(slots=True)
class OptionsPullReport:
    """Result of a :func:`pull_options` call (the options pull buttons' core).

    Aggregates the per-underlying outcome of one
    :func:`~brutex.adapters.dhan.option_universe_ingest.run_dhan_options_ingest`
    pass over the full ``[2021-01-01, today]`` range (DEC-DHAN-PULL-SCOPE-001).
    """

    dry_run: bool
    start_date: str
    end_date: str
    symbols: list[OptionSymbolResult] = field(default_factory=list)
    #: Transient windows still failing after the FINAL round (0 ⇒ ledger clean) —
    #: the aborted-series residual deferred to the next pull
    #: (DEC-OPTIONS-UNTIL-CLEAN-001).
    still_failed_windows: int = 0
    #: True iff the Dhan token died mid-pull (re-auth needed) + its first cause.
    auth_aborted: bool = False
    auth_cause: str | None = None
    #: True iff the pull paused on the spent 100,000-call daily quota
    #: (between-round brake OR a mid-round ``DailyQuotaExceeded``) — resume after
    #: the IST-midnight reset.
    quota_exhausted: bool = False
    #: First-pass series accounting (hostile finding 6): aborted all-or-nothing
    #: commits, latch-skipped series, benign DH-905 quarantines, genuine terminal
    #: quarantines.
    series_aborted: int = 0
    series_skipped_auth: int = 0
    no_data_series: int = 0
    real_failed_series: int = 0
    #: Until-clean rounds actually run (1 for a single-pass pull).
    rounds_run: int = 1

    @property
    def total_symbols(self) -> int:
        """Number of underlyings fanned out."""
        return len(self.symbols)

    @property
    def total_series_completed(self) -> int:
        """Total series that completed (non-quarantined) across all underlyings."""
        return sum(s.series_completed for s in self.symbols)

    @property
    def total_series_failed(self) -> int:
        """Total series quarantined on a terminal Dhan error."""
        return sum(s.series_failed for s in self.symbols)

    @property
    def total_bars(self) -> int:
        """Total option bars appended (session-filtered) across all underlyings."""
        return sum(s.bars_appended for s in self.symbols)

    @property
    def total_windows(self) -> int:
        """Total fetch windows across all successful series."""
        return sum(s.windows for s in self.symbols)

    @property
    def verdict(self) -> PullVerdict:
        """The pull's loud 4-way verdict (DEC-PULL-VERDICT-001, options arm).

        Reuses the SAME :func:`~brutex.adapters.dhan.pull_verdict.classify_pull`
        vocabulary the spot pull stamps (CLAUDE.md §11 COMMON): ``auth_expired``
        outranks everything, then ``quota_exhausted``, then ``transient_residual``
        (still-failing windows OR genuine terminal quarantines), then
        ``known_gap_only`` / ``clean``. Built from the REPORT (never the loop
        outcome, which zeroes the failure counts — hostile finding 5).
        """
        return classify_pull(
            auth_aborted=self.auth_aborted,
            still_failed_windows=self.still_failed_windows,
            failed_pairs=self.real_failed_series,
            no_data=self.no_data_series,
            completed=self.total_series_completed,
            auth_cause=self.auth_cause,
            quota_exhausted=self.quota_exhausted,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly dict for the ``/api/v1/universe/pull-options`` route."""
        return {
            "dry_run": self.dry_run,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "total_symbols": self.total_symbols,
            "total_series_completed": self.total_series_completed,
            "total_series_failed": self.total_series_failed,
            "total_bars": self.total_bars,
            "total_windows": self.total_windows,
            "still_failed_windows": self.still_failed_windows,
            "auth_aborted": self.auth_aborted,
            "auth_cause": self.auth_cause,
            "quota_exhausted": self.quota_exhausted,
            "series_aborted": self.series_aborted,
            "series_skipped_auth": self.series_skipped_auth,
            "no_data_series": self.no_data_series,
            "real_failed_series": self.real_failed_series,
            "rounds_run": self.rounds_run,
            "symbols": [
                {
                    "symbol": s.symbol,
                    "instrument": s.instrument,
                    "series_completed": s.series_completed,
                    "series_failed": s.series_failed,
                    "bars_appended": s.bars_appended,
                    "windows": s.windows,
                }
                for s in self.symbols
            ],
        }


def _build_dry_run_options(_lake_root: Path) -> tuple[JsonPoster, OptionBarSink]:
    """Build the offline ``(SyntheticOptionClient, NullOptionBarSink)`` pair.

    The synthetic client feeds the real rolling-option spine valid in-session
    bars and the null sink counts-but-discards them, so the options fan-out
    runs with zero dependencies — the dry-run proof the pipe works
    (DEC-VENDOR-PIVOT-001).
    """
    return SyntheticOptionClient(), NullOptionBarSink()


def _build_live_options(  # pragma: no cover — live Dhan token + transport + network
    lake_root: Path,
) -> tuple[JsonPoster, OptionBarSink]:
    """Wire the real Dhan client + the concrete dedup→write→audit option sink.

    Builds a token-authenticated
    :class:`~brutex.adapters.dhan.client.DhanClient` over the real ``urllib``
    transport and a :class:`~brutex.data.option_sink.OptionBarSink` backed by an
    O(1) :class:`~brutex.data.option_dedup.OptionDedupIndex` +
    :class:`~brutex.data.option_parquet_writer.OptionParquetWriter` over
    *lake_root*. ``# pragma: no cover`` — needs real credentials + network,
    absent in CI; its orchestration is exercised through the dry-run path.
    """
    from brutex.adapters.dhan.auth import resolve_dhan_credentials
    from brutex.adapters.dhan.client import DhanClient, urllib_transport
    from brutex.data.option_dedup import OptionDedupIndex
    from brutex.data.option_parquet_writer import OptionParquetWriter
    from brutex.data.option_sink import OptionBarSink as ConcreteOptionBarSink
    from brutex.orchestrator.audit import AuditWriter

    creds = resolve_dhan_credentials()
    client = DhanClient(
        access_token=creds.access_token,
        client_id=creds.client_id,
        transport=urllib_transport,
    )
    # Golden Rule #8 — wire the hash-chained option audit the sink already
    # supports (one OPTION_BARS_WRITTEN row per written series), for parity with
    # the spot pull's audit chain (DEC-INGEST-AUDIT-CHAIN-001). The AuditWriter
    # is flush-per-entry, so every row persists without an explicit close; the
    # sink's single write-lock keeps the chain single-writer (the append path is
    # 100%-tested in test_option_sink.py). DEC-OPTION-AUDIT-WIRE-001.
    audit_path = lake_root / "audit" / "option-ingest.jsonl"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    sink = ConcreteOptionBarSink(
        index=OptionDedupIndex(),
        writer=OptionParquetWriter(lake_root=lake_root),
        audit=AuditWriter.open(audit_path, resume=audit_path.exists()),
    )
    return client, sink


def _options_run_parallel(controller: DhanRateController | None) -> RunParallel | None:
    """Size the options fan-out pool to the controller's rate budget (Little's law).

    DEC-DHAN-PARALLEL-INGEST-001. A live pull threads its shared
    :class:`~brutex.adapters.dhan.rate_control.DhanRateController` here: the pool
    is widened to ``workers_to_saturate(controller.current_rps or 5.0,
    DEFAULT_LATENCY_BUDGET_S)`` so enough underlyings' windows are in flight to
    keep the rate BUCKET (not network latency) the bottleneck — targeting UP TO
    ~5 r/s (Rule #6 caps the aggregate; honest envelope: never a guaranteed
    constant 5 r/s). ``None`` controller (dry-run / injected factory) → ``None``,
    so ``run_dhan_options_ingest`` uses its default pool. Pure + covered; the
    live controller construction is the ``# pragma: no cover`` part.
    """
    if controller is None:
        return None
    workers = workers_to_saturate(controller.current_rps or 5.0, DEFAULT_LATENCY_BUDGET_S)
    return PoolRunParallel(max_workers=workers)


class _LiveOptionRuntime(NamedTuple):
    """The live-pull rate runtime: pacing + rate-feedback hooks + the sized pool.

    All five derive from ONE shared
    :class:`~brutex.adapters.dhan.rate_control.DhanRateController`. The
    ``on_rate_limit`` / ``on_success`` hooks reuse the spot path's bound-method
    wiring style (CLAUDE.md §11 COMMON); ``on_timeout`` extends it (the spot path
    has no timeout hook yet) so the governor adapts the options spine too
    (DEC-DHAN-TIMEOUT-BACKOFF-001). A dry-run / injected-factory pull uses the
    all-``None`` default → no throttle, default pool.
    """

    pace: Pacer | None = None
    on_rate_limit: Pacer | None = None
    on_timeout: Pacer | None = None
    on_success: Pacer | None = None
    run_parallel: RunParallel | None = None
    #: The shared controller's daily-quota probe (DEC-AUTO-RESUME-QUOTA-001) — the
    #: between-round brake the until-clean loop reads so it never re-fires a batch
    #: into a spent 100k-call day. ``None`` (dry-run / injected factory) ⇒ quota is
    #: never the limit.
    quota_exhausted: Callable[[], bool] | None = None


def _live_option_runtime(
    use_live_rate: bool, session_rate: SessionRateController | None = None
) -> _LiveOptionRuntime:
    """Build the :class:`_LiveOptionRuntime` for the options pull.

    A **default live** pull (no injected factory + not dry-run) resolves ONE shared
    controller and derives its ``pace`` hook, the three rate-feedback hooks (its
    own ``on_rate_limit`` / ``on_timeout`` / ``on_success`` bound methods), AND a
    rate-sized fan-out pool from it (DEC-DHAN-PARALLEL-INGEST-001 +
    DEC-DHAN-TIMEOUT-BACKOFF-001). When *session_rate* is given (the dashboard's
    persistent holder) the controller is the SAME one the spot pulls share — so
    the learned rate carries across spot AND options pulls and an options pull
    stops re-storming Dhan at the 5 r/s ceiling every time (DEC-DHAN-OPTION-PERSIST-RATE-001,
    extending the spot DEC-DHAN-PERSIST-RATE-001); the single-flight background
    job means spot + options never run concurrently, so one shared ≤ 5 r/s budget
    is correct (Rule #6). ``None`` (a CLI / injected-factory pull) builds a fresh
    controller, the prior behaviour. Dry-run / injected-factory pulls need no
    throttle → the all-``None`` default (the ingest spine uses its default pool).
    The pool sizing (:func:`_options_run_parallel`) is covered; only the live
    controller resolution is ``# pragma: no cover``.
    """
    if not use_live_rate:
        return _LiveOptionRuntime()
    controller = (  # pragma: no cover — live only
        session_rate.get() if session_rate is not None else _build_live_option_controller()
    )
    return _LiveOptionRuntime(  # pragma: no cover — live only
        pace=controller.pace,
        on_rate_limit=controller.on_rate_limit,
        on_timeout=controller.on_timeout,
        on_success=controller.on_success,
        run_parallel=_options_run_parallel(controller),
        quota_exhausted=controller.quota_exhausted,
    )


def _build_live_option_controller() -> DhanRateController:  # pragma: no cover — live controller
    """Construct the shared adaptive [3..5] r/s controller for a live options pull."""
    from brutex.adapters.dhan.rate_control import DhanRateController

    return DhanRateController()


def pull_options(
    instruments: Sequence[Instrument],
    *,
    lake_root: Path,
    dry_run: bool,
    now: datetime | None = None,
    expiry_flags: Sequence[str] | None = None,
    expiry_codes: Sequence[int] | None = None,
    option_types: Sequence[str] = DRV_OPTION_TYPES,
    client_factory: OptionClientFactory | None = None,
    on_progress: ProgressHook | None = None,
    on_series_tick: Callable[[], None] | None = None,
    pull_id: str | None = None,
    session_rate: SessionRateController | None = None,
    until_clean: bool = False,
    until_clean_rounds: int = AUTO_RESUME_MAX_ROUNDS,
    until_clean_sleep: Callable[[int], None] | None = None,
) -> OptionsPullReport:
    """Fan-out-pull the option universe for *instruments* over ``[2021-01-01, today]``.

    The core behind the options pull buttons: map each *instrument* to its
    :class:`OptionUnderlying` (index → ``OPTIDX``/ATM±10/``WEEK``+``MONTH``,
    stock → ``OPTSTK``/ATM±3/``MONTH``-only — single stocks are monthly-only,
    DEC-DHAN-STOCK-MONTHLY-ONLY-001), then drive ONE
    :func:`~brutex.adapters.dhan.option_universe_ingest.run_dhan_options_ingest`
    pass over the whole list so the live client is built ONCE and a bad series
    is quarantined without sinking the rest. A one-element list is the
    per-symbol button; the full universe is the bulk button. An empty list is a
    clean no-op.

    Parameters
    ----------
    instruments
        The index / stock spot underlyings to fan out into options.
    lake_root
        Lake directory the live sink writes Parquet into.
    dry_run
        When ``True`` use the offline synthetic client (no creds / network).
    now
        Injected "current" instant (defaults to :func:`datetime.now`) — its UTC
        date is the upper bound of the full ``[2021-01-01, today]`` window.
    expiry_flags
        Optional **uniform override** for the expiry-flag axis. ``None`` (the
        default) honours each underlying's own per-kind set — index
        ``WEEK``+``MONTH``, stock ``MONTH``-only (DEC-DHAN-STOCK-MONTHLY-ONLY-001);
        passing a sequence forces it on every underlying.
    expiry_codes, option_types
        The remaining fan-out axes. ``expiry_codes`` defaults to ``None`` →
        **per-flag** depth (WEEK codes 1..8, MONTH 1..3;
        DEC-DHAN-WEEKLY-EXPIRY-DEPTH-001); an explicit sequence forces that exact
        set on every flag. ``option_types`` defaults to ``CALL``/``PUT``.
    client_factory
        Override the ``(client, sink)`` builder; defaults to the synthetic
        builder when *dry_run* else the live builder. Tests inject a fake
        factory to exercise the non-dry-run branch without network.
    on_series_tick
        Optional **observability-only** zero-arg hook threaded to the ingest
        spine, fired once per SERIES as it completes (DEC-OPTIONS-LIVE-STATUS-001).
        The dashboard passes a THROTTLED summary-cache ``mark_dirty`` so a
        single-underlying options pull's lake-backed ``/coverage/options`` surface
        ticks live mid-fan-out, not only at the per-underlying ``on_progress`` at
        the very end. Never changes what is fetched/parsed/sunk or the rate.
    until_clean
        ``True`` (DEC-OPTIONS-UNTIL-CLEAN-001) escalates to the bounded
        re-pull-until-clean loop — the options arm of the spot auto-resume:
        bounded rounds over the SHARED window ledger (settled windows skip in
        O(1) — only the aborted-series residual hits Dhan again), reusing the
        SAME :func:`~brutex.adapters.dhan.until_clean.run_until_clean` driver
        (CLAUDE.md §11 COMMON; NO new loop logic), until ``stop_clean`` /
        ``stop_dead`` (token died) / ``stop_quota`` (daily quota spent) /
        ``stop_budget``. ``stop_when_stuck`` is OFF for options — an aborted
        all-or-nothing series settles NOTHING, so its residual is constant
        across failing rounds and the shrinkage signal would falsely stop the
        canonical 429-storm recovery after ~2 rounds (hostile finding 1); the
        quota brake + the bounded budget keep the loop safe instead. ``False``
        (the default) keeps the prior single pass.
    until_clean_rounds
        Bounded OUTER round budget for *until_clean* (default
        :data:`~brutex.adapters.dhan.until_clean.AUTO_RESUME_MAX_ROUNDS`).
        Never infinite (Golden Rule #10). Worst case (a never-recovering
        storm): ``rounds × residual × (1 + in-run retries)`` paced calls, every
        one ``≤ 5 r/s`` bucket-bounded and quota-braked.
    until_clean_sleep
        Injected ``(round_index) -> None`` inter-round backoff (``None`` → the
        driver's bounded exponential 5→60 s default). Tests pass a no-op so no
        real sleep occurs.

    Returns
    -------
    OptionsPullReport
        Per-underlying series-completed / series-failed / bars / windows counts
        + the aggregate totals, the FINAL-round residual/auth/quota state, and
        the loud :attr:`OptionsPullReport.verdict`. Per-symbol rollups come from
        the FIRST pass's report (mirroring the spot until-clean contract); the
        aggregate residual reflects the FINAL round.

    Raises
    ------
    ValueError
        Via :func:`instrument_to_option_underlying` for a blank
        ``symbol_name`` or a non-``IDX``/``EQ`` instrument.
    """
    instruments = list(instruments)
    underlyings = [instrument_to_option_underlying(i) for i in instruments]
    start, end = resolve_candle_range(
        from_date=DHAN_HISTORY_FLOOR.isoformat(),
        to_date=None,
        year=None,
        now=now if now is not None else datetime.now(UTC),
    )
    use_live_rate = client_factory is None and not dry_run
    if client_factory is None:
        client_factory = _build_dry_run_options if dry_run else _build_live_options
    client, sink = client_factory(lake_root)

    # Open the pull's correlation scope (DEC-DHAN-INGEST-LOGGING-001): ``pid`` is
    # bound into the structlog contextvars for the whole fan-out, so this
    # start/done/failures summary, the ingest spine's ``ingest.*`` events, and the
    # already-lit transport (``dhan.call``) + rate (``rate.*``) events all share
    # one ``pull_id``. Always unbinds on exit (no leak).
    with pull_id_scope(pull_id) as pid:
        # Unified per-window ledger (DEC-WINDOW-LEDGER-001, PR-4b): load/migrate the
        # durable ledger so each committed option cursor is recorded into the SAME
        # browsable DB table as spot. (Option windows are keyed per-series, so they
        # are recorded as they commit — not pre-PLANNED like spot.)
        ledger = begin_pull_ledger(lake_root, dry_run=dry_run)

        _LOG.info(
            "universe.pull_options.start",
            pull_id=pid,
            underlyings=len(underlyings),
            start_date=start.date().isoformat(),
            dry_run=dry_run,
            until_clean=until_clean,
        )
        runtime = _live_option_runtime(use_live_rate, session_rate)
        # Mid-pull durability (DEC-OPTIONS-UNTIL-CLEAN-001, hostile finding 2):
        # flush the in-memory window ledger to its Parquet THROTTLED on every
        # series tick, so a crash / kill / mid-round quota death loses ≤ the
        # throttle interval of window records — never hours of settled windows.
        # (Bars are already durable: the sink commits per series.) No-op for a
        # dry-run (``ledger`` is None).
        ledger_flush = make_live_ledger_flush(ledger, lake_root)

        def _tick_then_flush() -> None:
            if on_series_tick is not None:
                on_series_tick()
            ledger_flush()

        passes: list[OptionsUniverseReport] = []

        def _one_pass() -> OptionsUniverseReport:
            """Run ONE full fan-out pass — the single ingest call site (§11 COMMON).

            The one-shot path AND every until-clean round go through here, so the
            args can never drift between them; each pass shares the SAME ledger
            (settled windows skip in O(1) — only residual windows hit Dhan).
            Honest envelope (Golden Rule #10): a round still REBUILDS the full
            jobs list and probes every series' windows — O(series·windows) CPU
            per round with ZERO vendor calls for settled windows; never an
            O(residual) or O(1)-total claim.
            """
            rep = run_dhan_options_ingest(
                client,
                sink,
                underlyings=underlyings,
                start=start.date(),
                end=end.date(),
                expiry_flags=expiry_flags,
                expiry_codes=expiry_codes,
                option_types=option_types,
                required_data=ALL_REQUIRED_DATA,
                pace=runtime.pace,
                on_rate_limit=runtime.on_rate_limit,
                on_timeout=runtime.on_timeout,
                on_success=runtime.on_success,
                on_progress=on_progress,
                on_series_tick=_tick_then_flush,
                ledger=ledger,
                run_parallel=runtime.run_parallel,
            )
            passes.append(rep)
            return rep

        final_quota = False
        final_still_failed = 0
        final_auth_aborted = False
        final_auth_cause: str | None = None
        rounds_run = 1
        try:
            try:
                if until_clean:

                    def _round_runner() -> UntilCleanRound:
                        """One until-clean round = one full fan-out pass."""
                        rep = _one_pass()
                        return UntilCleanRound(
                            still_failed_windows=rep.still_failed_windows,
                            auth_aborted=rep.auth_aborted,
                            auth_cause=rep.auth_cause,
                        )

                    # The options auto-resume (DEC-OPTIONS-UNTIL-CLEAN-001): the SAME
                    # bounded driver + quota brake as spot. ``stop_when_stuck`` stays
                    # OFF — an aborted all-or-nothing series settles NOTHING, so its
                    # residual is CONSTANT across failing rounds and the shrinkage
                    # heuristic would falsely kill the canonical 429-storm recovery
                    # after ~2 rounds (hostile finding 1); the bounded budget + the
                    # quota hook are the safety here.
                    until_outcome = run_until_clean(
                        _round_runner,
                        max_rounds=until_clean_rounds,
                        sleep=until_clean_sleep,
                        quota_exhausted=runtime.quota_exhausted,
                        stop_when_stuck=False,
                    )
                    rounds_run = until_outcome.rounds_run
                    final_still_failed = until_outcome.final_still_failed
                    final_auth_aborted = until_outcome.auth_aborted or (
                        bool(passes) and passes[0].auth_aborted
                    )
                    final_auth_cause = until_outcome.auth_cause or (
                        passes[0].auth_cause if passes else None
                    )
                    final_quota = until_outcome.stopped_on_quota
                else:
                    rep = _one_pass()
                    final_still_failed = rep.still_failed_windows
                    final_auth_aborted = rep.auth_aborted
                    final_auth_cause = rep.auth_cause
            except DailyQuotaExceeded as exc:
                # The 100k daily quota died MID-round (the loop's between-round
                # hook can only brake at round boundaries — hostile finding 2c).
                # Everything already committed survives: bars per series via the
                # sink, window records via the throttled flush + the finally
                # below. Map to the loud quota verdict instead of a generic job
                # error; the next pull (after IST midnight) resumes incrementally.
                final_quota = True
                rounds_run = max(len(passes), 1)
                final_still_failed = passes[-1].still_failed_windows if passes else 0
                final_auth_aborted = passes[-1].auth_aborted if passes else False
                final_auth_cause = passes[-1].auth_cause if passes else None
                _LOG.warning(
                    "universe.pull_options.quota_exhausted",
                    pull_id=pid,
                    error=str(exc),
                    rounds_completed=len(passes),
                    note="daily Dhan quota spent mid-round — committed work kept; resume after IST midnight",
                )
        finally:
            # ALWAYS persist the window ledger — a crash/abort must never discard
            # the settled-window history (hostile finding 2a). Idempotent with the
            # throttled mid-pull flushes.
            end_pull_ledger(ledger, lake_root, dry_run=dry_run)
        report = passes[0] if passes else OptionsUniverseReport(series={}, failures=())

        # Aggregate the per-series reports + quarantined failures by underlying.
        failed_by_symbol: dict[str, int] = {}
        error_counts: dict[str, int] = {}
        for failure in report.failures:
            failed_by_symbol[failure.security_id] = failed_by_symbol.get(failure.security_id, 0) + 1
            error_counts[failure.error] = error_counts.get(failure.error, 0) + 1
        completed_by_symbol: dict[str, tuple[int, int, int]] = {}
        for (security_id, *_rest), series_report in report.series.items():
            prev_count, prev_bars, prev_windows = completed_by_symbol.get(security_id, (0, 0, 0))
            completed_by_symbol[security_id] = (
                # An ABORTED series committed nothing — never a completion
                # (hostile finding 6); its windows stay in the residual count.
                prev_count + (1 if series_report.complete else 0),
                prev_bars + series_report.bars_appended,
                prev_windows + series_report.windows,
            )

        results: list[OptionSymbolResult] = []
        for underlying in underlyings:
            sid = underlying.security_id
            completed, bars, windows = completed_by_symbol.get(sid, (0, 0, 0))
            results.append(
                OptionSymbolResult(
                    symbol=sid,
                    instrument=underlying.instrument,
                    series_completed=completed,
                    series_failed=failed_by_symbol.get(sid, 0),
                    bars_appended=bars,
                    windows=windows,
                )
            )

        pull_report = OptionsPullReport(
            dry_run=dry_run,
            start_date=start.date().isoformat(),
            end_date=(end - timedelta(days=1)).date().isoformat(),
            symbols=results,
            still_failed_windows=final_still_failed,
            auth_aborted=final_auth_aborted,
            auth_cause=final_auth_cause,
            quota_exhausted=final_quota,
            series_aborted=report.aborted_series,
            series_skipped_auth=report.series_skipped_auth,
            no_data_series=report.no_data_series,
            real_failed_series=report.real_failed_series,
            rounds_run=rounds_run,
        )
        # Surface WHY series failed (the silent-failure logging gap the operator
        # flagged): a bare ``series_failed=N`` count hides the cause. Log the
        # distinct terminal-error messages with counts so the failure is
        # diagnosable in one glance (DEC-OPTION-SECURITY-ID-FIX-001).
        if error_counts:
            top_errors = sorted(error_counts.items(), key=lambda kv: kv[1], reverse=True)[:5]
            _LOG.warning(
                "universe.pull_options.failures",
                pull_id=pid,
                total_failed=pull_report.total_series_failed,
                distinct_errors=len(error_counts),
                top_errors=[{"error": err[:300], "count": cnt} for err, cnt in top_errors],
            )
        _LOG.info(
            "universe.pull_options.done",
            pull_id=pid,
            underlyings=pull_report.total_symbols,
            series_completed=pull_report.total_series_completed,
            series_failed=pull_report.total_series_failed,
            series_aborted=pull_report.series_aborted,
            series_skipped_auth=pull_report.series_skipped_auth,
            still_failed_windows=pull_report.still_failed_windows,
            rounds_run=pull_report.rounds_run,
            verdict=pull_report.verdict.kind,
            bars=pull_report.total_bars,
            dry_run=dry_run,
        )
    return pull_report


__all__ = [
    "OptionClientFactory",
    "OptionSymbolResult",
    "OptionsPullReport",
    "instrument_to_option_underlying",
    "pull_options",
]
