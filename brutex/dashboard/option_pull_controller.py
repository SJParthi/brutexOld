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
fan-out (ATM window × the underlying's per-kind expiry flags × the
:data:`~brutex.adapters.dhan.option_universe_ingest.DEFAULT_EXPIRY_CODES` ×
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
    DEFAULT_EXPIRY_CODES,
    INDEX_STRIKE_OFFSETS,
    STOCK_EXPIRY_FLAGS,
    STOCK_STRIKE_OFFSETS,
    OptionUnderlying,
    ProgressHook,
    run_dhan_options_ingest,
)
from brutex.dashboard.pull_controller import DHAN_HISTORY_FLOOR, resolve_candle_range
from brutex.data.fetch_coverage import FetchCoverage
from brutex.data.fetch_coverage_store import begin_pull_coverage, end_pull_coverage
from brutex.data.option_sink import NullOptionBarSink
from brutex.models import Instrument, InstrumentType
from brutex.observability.correlation import pull_id_scope
from brutex.orchestrator.parallel import (
    DEFAULT_LATENCY_BUDGET_S,
    PoolRunParallel,
    workers_to_saturate,
)
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

    creds = resolve_dhan_credentials()
    client = DhanClient(
        access_token=creds.access_token,
        client_id=creds.client_id,
        transport=urllib_transport,
    )
    sink = ConcreteOptionBarSink(
        index=OptionDedupIndex(),
        writer=OptionParquetWriter(lake_root=lake_root),
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


def _live_option_runtime(use_live_rate: bool) -> _LiveOptionRuntime:
    """Build the :class:`_LiveOptionRuntime` for the options pull.

    A **default live** pull (no injected factory + not dry-run) builds ONE shared
    controller and derives its ``pace`` hook, the three rate-feedback hooks (its
    own ``on_rate_limit`` / ``on_timeout`` / ``on_success`` bound methods), AND a
    rate-sized fan-out pool from it (DEC-DHAN-PARALLEL-INGEST-001 +
    DEC-DHAN-TIMEOUT-BACKOFF-001). Dry-run / injected-factory pulls need no
    throttle → the all-``None`` default (the ingest spine uses its default pool).
    The controller starts at the 5 r/s ceiling (DEC-DHAN-ADAPTIVE-RPS-001) and
    now ADAPTS the options spine: sustained 429s OR sustained timeouts step it
    5→4→3, recovering on a clean window. The pool sizing
    (:func:`_options_run_parallel`) is covered; only the live controller
    construction is ``# pragma: no cover``.
    """
    if not use_live_rate:
        return _LiveOptionRuntime()
    controller = _build_live_option_controller()  # pragma: no cover — live only
    return _LiveOptionRuntime(  # pragma: no cover — live only
        pace=controller.pace,
        on_rate_limit=controller.on_rate_limit,
        on_timeout=controller.on_timeout,
        on_success=controller.on_success,
        run_parallel=_options_run_parallel(controller),
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
    expiry_codes: Sequence[int] = DEFAULT_EXPIRY_CODES,
    option_types: Sequence[str] = DRV_OPTION_TYPES,
    client_factory: OptionClientFactory | None = None,
    on_progress: ProgressHook | None = None,
    coverage: FetchCoverage | None = None,
    pull_id: str | None = None,
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
        The remaining fan-out axes — default to the near/next/far expiry codes
        (DEC-DHAN-EXPIRY-CODES-001) and ``CALL``/``PUT``.
    client_factory
        Override the ``(client, sink)`` builder; defaults to the synthetic
        builder when *dry_run* else the live builder. Tests inject a fake
        factory to exercise the non-dry-run branch without network.
    coverage
        Optional pre-seeded skip-ledger. On a **live** pull (``dry_run`` False),
        ``None`` (the default) is replaced by the persisted ledger
        (``<lake_root>/fetch_coverage.json``) so already-pulled option windows
        are skipped and this run's completed windows are saved back — a re-pull
        never re-burns the Dhan quota on data already in the lake
        (DEC-FETCH-COVERAGE-WIRING-001). Dry-run ignores it (null sink).

    Returns
    -------
    OptionsPullReport
        Per-underlying series-completed / series-failed / bars / windows counts
        + the aggregate totals.

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
        # Load the persisted skip-ledger for a live pull so windows already in the
        # lake are not re-fetched (no wasted Dhan quota on a re-run);
        # DEC-FETCH-COVERAGE-WIRING-001.
        coverage = begin_pull_coverage(lake_root, dry_run=dry_run, coverage=coverage)

        _LOG.info(
            "universe.pull_options.start",
            pull_id=pid,
            underlyings=len(underlyings),
            start_date=start.date().isoformat(),
            dry_run=dry_run,
        )
        runtime = _live_option_runtime(use_live_rate)
        report = run_dhan_options_ingest(
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
            coverage=coverage,
            run_parallel=runtime.run_parallel,
        )
        # Persist the windows just completed so the next pull skips them.
        end_pull_coverage(coverage, lake_root, dry_run=dry_run)

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
                prev_count + 1,
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
