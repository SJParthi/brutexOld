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

from collections.abc import Sequence
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

#: ATM strike-window half-widths (DEC-DHAN-PULL-SCOPE-001) — the documented
#: Dhan rolling-option bounds (``docs/dhan-ref/06``): indices ATM±10 (21
#: strikes), stocks ATM±3 (7 strikes).
INDEX_STRIKE_OFFSETS: int = 10
STOCK_STRIKE_OFFSETS: int = 3

#: Full expiry depth per flag — the locked DEC-P0-063 "all 4 expiryCodes"
#: (operator choice 2026-06-01). The exact valid Dhan ``expiryCode`` range is a
#: Phase-0b empirical question (``docs/dhan-ref/06`` documents it only as an
#: "expiry selector"); requesting an invalid code is **safe** because the
#: per-series quarantine records it and the run continues (never fatal).
DEFAULT_EXPIRY_CODES: tuple[int, ...] = (0, 1, 2, 3)

#: The report key for one fanned-out series.
SeriesKey = tuple[str, str, int, str, str]


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
                            series[key] = ingest_rolling_option_series(
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
    return OptionsUniverseReport(series=series, failures=tuple(failures))


__all__ = [
    "DEFAULT_EXPIRY_CODES",
    "INDEX_STRIKE_OFFSETS",
    "STOCK_STRIKE_OFFSETS",
    "OptionSeriesFailure",
    "OptionUnderlying",
    "OptionsUniverseReport",
    "SeriesKey",
    "run_dhan_options_ingest",
    "strike_window",
]
