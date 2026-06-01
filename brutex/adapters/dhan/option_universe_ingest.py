"""Dhan options rolling-fan-out — pull the whole option universe (LAKE surface).

The orchestration the cli/dashboard call to fill the **options** lake
(DEC-DHAN-PULL-SCOPE-001, MIGRATION #348): for each underlying, fan out over
the ATM strike window × ``WEEK``/``MONTH`` expiry flags × the expiry codes ×
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

The strike window matches the documented Dhan rolling-option bounds
(``docs/dhan-ref/06``): **ATM±10** for the brute-force indices (near-expiry),
**ATM±3** for stocks. The options lake is **LAKE-only / REFERENCE_ONLY** — the
brute-force engine still sweeps only the 3 indices (CLAUDE.md §3; DEC-P0-063).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date

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

#: ATM strike-window half-widths (DEC-DHAN-PULL-SCOPE-001) — the documented
#: Dhan rolling-option bounds (``docs/dhan-ref/06``): indices ATM±10 (21
#: strikes), stocks ATM±3 (7 strikes).
INDEX_STRIKE_OFFSETS: int = 10
STOCK_STRIKE_OFFSETS: int = 3

#: The expiry depths the rolling-option endpoint serves — Near, Next, Far
#: (DEC-DHAN-EXPIRY-CODES-001). `/charts/rollingoption` documents
#: ``expiryCode`` **Allowed values: 1, 2, 3** (1-indexed for this endpoint;
#: the general Annexure indexes the same three depths 0/1/2). Verified from the
#: operator's 2026-06-01 Dhan docs + live 200-OK probes — this supersedes the
#: earlier unverified "4 expiryCodes" assumption (a Groww carryover). A code the
#: vendor rejects is still **safe**: the per-series quarantine records it and the
#: run continues (never fatal).
DEFAULT_EXPIRY_CODES: tuple[int, ...] = (1, 2, 3)

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
    """One underlying to fan out over (its id, segment, instrument, width)."""

    security_id: str
    exchange_segment: str
    instrument: str  # ``OPTIDX`` (index) / ``OPTSTK`` (stock)
    strike_offsets: int


@dataclass(frozen=True, slots=True)
class OptionSeriesFailure:
    """A single ``(underlying, flag, code, strike, type)`` series that failed terminally."""

    security_id: str
    expiry_flag: str
    expiry_code: int
    strike: str
    drv_option_type: str
    error: str


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
    expiry_flags: Sequence[str] = EXPIRY_FLAGS,
    expiry_codes: Sequence[int] = DEFAULT_EXPIRY_CODES,
    option_types: Sequence[str] = DRV_OPTION_TYPES,
    required_data: Sequence[str] = ALL_REQUIRED_DATA,
    pace: Pacer | None = None,
    on_progress: ProgressHook | None = None,
    coverage: FetchCoverage | None = None,
) -> OptionsUniverseReport:
    """Fan out the rolling-option pull over the whole options universe.

    For each *underlying* iterate ``strike_window(offsets) × expiry_flags ×
    expiry_codes × option_types`` and drive
    :func:`~brutex.adapters.dhan.option_ingest.ingest_rolling_option_series`
    per series (1-minute, session-filtered, spot-stamped). A **terminal** Dhan
    error on one series is quarantined into ``failures`` and the run continues.

    Parameters
    ----------
    client, sink
        The Dhan JSON poster and the injected :class:`OptionBar` write sink.
    underlyings
        The underlyings to fan out (each carries its own ``strike_offsets`` —
        ``INDEX_STRIKE_OFFSETS`` / ``STOCK_STRIKE_OFFSETS``).
    start, end
        Half-open date range, shared by every series.
    expiry_flags, expiry_codes, option_types
        The fan-out axes — default to ``WEEK``/``MONTH``, the 4 expiry codes
        (DEC-P0-063), and ``CALL``/``PUT``.
    required_data
        Fields to request (defaults to every documented field).
    pace
        Optional per-window pacing hook, threaded into each series.
    on_progress
        Optional live-progress hook fired once per **underlying** after its
        whole fan-out completes: ``(security_id, bars_appended, any_failed)``.
        Lets the dashboard stream real-time progress (DEC-PULL-PROGRESS-TRACKER-001).

    Returns
    -------
    OptionsUniverseReport
        Per-series :class:`OptionIngestReport` for successes + the quarantined
        terminal failures. **Never raises** on a Dhan error.
    """
    series: dict[SeriesKey, OptionIngestReport] = {}
    failures: list[OptionSeriesFailure] = []
    for underlying in underlyings:
        strikes = strike_window(underlying.strike_offsets)
        underlying_bars = 0
        underlying_failed = False
        for expiry_flag in expiry_flags:
            for expiry_code in expiry_codes:
                for strike in strikes:
                    for drv_option_type in option_types:
                        key: SeriesKey = (
                            underlying.security_id,
                            expiry_flag,
                            expiry_code,
                            strike,
                            drv_option_type,
                        )
                        try:
                            report = ingest_rolling_option_series(
                                client,
                                sink,
                                security_id=underlying.security_id,
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
                                coverage=coverage,
                            )
                        except DhanError as exc:
                            failures.append(
                                OptionSeriesFailure(
                                    security_id=underlying.security_id,
                                    expiry_flag=expiry_flag,
                                    expiry_code=expiry_code,
                                    strike=strike,
                                    drv_option_type=drv_option_type,
                                    error=str(exc),
                                )
                            )
                            underlying_failed = True
                            continue
                        series[key] = report
                        underlying_bars += report.bars_appended
        # Fire the live-progress hook once the WHOLE underlying fan-out is done.
        if on_progress is not None:
            on_progress(underlying.security_id, underlying_bars, underlying_failed)
    return OptionsUniverseReport(series=series, failures=tuple(failures))


__all__ = [
    "DEFAULT_EXPIRY_CODES",
    "INDEX_STRIKE_OFFSETS",
    "STOCK_STRIKE_OFFSETS",
    "OptionSeriesFailure",
    "OptionUnderlying",
    "OptionsUniverseReport",
    "ProgressHook",
    "SeriesKey",
    "run_dhan_options_ingest",
    "strike_window",
]
