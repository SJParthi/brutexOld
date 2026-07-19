"""Risk-free rate source for the options-Greeks engine (Phase 1).

The ONE labelled Python source of the risk-free rate ``r`` consumed by the
gated Greeks phases (`docs/GREEKS_ENGINE_PLAN.md` §11 Phase 1;
`docs/adr/0015-shared-greeks-engine.md` §5 Phase 1) — the Phase-1 data
prerequisite of DEC-GREEKS-SHARED-CORE-001 (MIGRATION.md #1070), standalone
with NO Greeks dependency.

Two source shapes, both stamping the SAME provenance
(:class:`RiskFreeRate` — the rate + a ``source`` label + a ``version``, per
the plan's Phase-1 gate "the ``r``-source + version stamped"):

- :class:`FixedRateSource` — the plan §7 Proposed Decision #2 DEFAULT: a
  configurable fixed continuously-compounded annual rate, defaulting to
  :data:`DEFAULT_FIXED_RATE` (6.5%). The default is an ASSUMPTION recorded
  in ADR-0015 §3 (operator-overridable), NOT a market observation.
- :class:`CsvRateSource` (via :func:`load_risk_free_csv`) — the follow-up
  T-bill daily-CSV shape: a ``date,yield,tenor_days`` table of money-market
  simple yields, each converted to a continuously-compounded rate via
  :func:`money_market_to_continuous` (``r_cc = (365/n)·ln(1 + y·n/365)``,
  the exact conversion the plan §11 Phase 1 specifies) and served through a
  STEP-FUNCTION lookup — the latest row on/before the query date, with NO
  interpolation and NO backward extrapolation (a date before the first row
  is an honest ``None``, never a fabricated rate — Golden Rule #1).

Complexity envelope (honest): :meth:`FixedRateSource.rate_on` is O(1);
:meth:`CsvRateSource.rate_on` is O(log rows) per lookup (`bisect` over the
sorted date column — NOT a literal O(1)); :func:`load_risk_free_csv` is
O(rows·log rows) once at load.

Pure stdlib, no I/O beyond the explicit CSV load, no global mutable state.
"""

from __future__ import annotations

import csv
import math
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date
from itertools import pairwise
from pathlib import Path
from typing import Final, Protocol, runtime_checkable

__all__ = [
    "DAY_COUNT_BASE",
    "DEFAULT_FIXED_RATE",
    "RISK_FREE_SOURCE_VERSION",
    "CsvRateSource",
    "FixedRateSource",
    "RiskFreeError",
    "RiskFreeRate",
    "RiskFreeSource",
    "load_risk_free_csv",
    "money_market_to_continuous",
]

#: Version stamped on every emitted :class:`RiskFreeRate`. Bump when the
#: conversion formula or a source's semantics change — the Phase-2b
#: provenance stamp (`greeks_provenance_id`, plan §15.3) folds this in so
#: stored Greeks are staleness-detectable.
RISK_FREE_SOURCE_VERSION: Final[int] = 1

#: The plan §7 Proposed Decision #2 default fixed rate (~6.5%), recorded in
#: ADR-0015 §3 as an operator-overridable ASSUMPTION. Continuously
#: compounded, annualized, expressed as a decimal fraction.
DEFAULT_FIXED_RATE: Final[float] = 0.065

#: Day-count base of the money-market → continuous conversion (Act/365 per
#: the plan §11 Phase 1 formula ``r_cc = (365/n)·ln(1 + y·n/365)``).
DAY_COUNT_BASE: Final[int] = 365

_RATE_LOWER: Final[float] = 0.0
_RATE_UPPER: Final[float] = 1.0

_CSV_COLUMNS: Final[tuple[str, str, str]] = ("date", "yield", "tenor_days")


class RiskFreeError(ValueError):
    """A risk-free-rate input is invalid (bad rate, bad CSV, bad tenor).

    Raised eagerly at construction / load time so a mis-configured rate can
    never silently reach a Greeks solve. Subclasses :class:`ValueError`
    because every case is a caller-supplied-value defect.
    """


@dataclass(frozen=True, slots=True)
class RiskFreeRate:
    """One stamped risk-free-rate observation.

    Attributes
    ----------
    rate:
        Continuously-compounded annualized risk-free rate, decimal
        fraction (e.g. ``0.065`` = 6.5%). FULL precision — never rounded
        (DEC-MONEY-2DP-001 forbids rounding statistical values).
    source:
        Provenance label of the producing source (e.g. ``"fixed"`` or
        ``"csv:risk_free_daily.csv"``) — the Phase-1 gate's ``r``-source
        stamp.
    version:
        :data:`RISK_FREE_SOURCE_VERSION` at emission time — the staleness
        key the Phase-2b provenance stamp folds in.
    """

    rate: float
    source: str
    version: int


@runtime_checkable
class RiskFreeSource(Protocol):
    """Protocol every risk-free source satisfies (COMMON seam).

    The Greeks phases depend on THIS seam, never on a concrete class, so a
    T-bill curve can replace the fixed default without re-architecting
    (Golden Rule #11 DYNAMIC).
    """

    def rate_on(self, on_date: date) -> RiskFreeRate | None:
        """Return the stamped rate effective on ``on_date``, or ``None``.

        Parameters
        ----------
        on_date:
            The (IST trading) calendar date the rate applies to.

        Returns
        -------
        RiskFreeRate | None
            The stamped rate, or ``None`` when the source honestly has no
            rate on/before ``on_date`` (never a fabricated value).
        """
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed


def _validate_rate(rate: float, *, context: str) -> None:
    """Reject a non-finite or out-of-range annualized rate.

    Parameters
    ----------
    rate:
        Candidate continuously-compounded annual rate.
    context:
        Human-readable origin (``"fixed rate"`` / ``"csv row N"``) folded
        into the error message.

    Raises
    ------
    RiskFreeError
        If ``rate`` is NaN / infinite or outside ``[0.0, 1.0)`` — an INR
        sovereign short rate outside that band is a configuration defect,
        rejected eagerly rather than silently priced.
    """
    if not math.isfinite(rate):
        msg = f"{context}: rate must be finite, got {rate!r}"
        raise RiskFreeError(msg)
    if rate < _RATE_LOWER or rate >= _RATE_UPPER:
        msg = f"{context}: rate must lie in [0.0, 1.0), got {rate!r}"
        raise RiskFreeError(msg)


def money_market_to_continuous(simple_yield: float, tenor_days: int) -> float:
    """Convert a money-market simple yield to a continuously-compounded rate.

    Implements the plan §11 Phase 1 conversion exactly::

        r_cc = (365 / n) * ln(1 + y * n / 365)

    where ``y`` is the simple (money-market, Act/365) yield and ``n`` the
    instrument tenor in days (e.g. 91 for a 91-day T-bill).

    Parameters
    ----------
    simple_yield:
        Simple annualized yield as a decimal fraction (e.g. ``0.0655``).
    tenor_days:
        Instrument tenor in days; must be ``>= 1``.

    Returns
    -------
    float
        The equivalent continuously-compounded annualized rate. For
        ``simple_yield >= 0`` it satisfies ``0 <= r_cc <= simple_yield``
        (since ``ln(1 + x) <= x``) — property-tested.

    Raises
    ------
    RiskFreeError
        If ``tenor_days < 1``, ``simple_yield`` is non-finite, or the log
        argument ``1 + y·n/365`` is not strictly positive.
    """
    if tenor_days < 1:
        msg = f"tenor_days must be >= 1, got {tenor_days!r}"
        raise RiskFreeError(msg)
    if not math.isfinite(simple_yield):
        msg = f"simple_yield must be finite, got {simple_yield!r}"
        raise RiskFreeError(msg)
    growth = 1.0 + simple_yield * tenor_days / DAY_COUNT_BASE
    if growth <= 0.0:
        msg = (
            f"simple_yield {simple_yield!r} over tenor_days {tenor_days!r} "
            f"gives non-positive growth factor {growth!r}"
        )
        raise RiskFreeError(msg)
    return (DAY_COUNT_BASE / tenor_days) * math.log(growth)


@dataclass(frozen=True, slots=True)
class FixedRateSource:
    """The plan-default fixed-``r`` source (Proposed Decision #2).

    Returns the SAME stamped continuously-compounded rate for every date —
    reproducible and simple; matching curve accuracy is the recorded
    follow-up, not this class's claim (ADR-0015 §3, Golden Rule #10).

    Attributes
    ----------
    rate:
        The fixed continuously-compounded annual rate; defaults to
        :data:`DEFAULT_FIXED_RATE`. Validated eagerly at construction.
    """

    rate: float = DEFAULT_FIXED_RATE

    def __post_init__(self) -> None:
        """Validate the configured rate eagerly (fail-fast, never priced)."""
        _validate_rate(self.rate, context="fixed rate")

    def rate_on(self, on_date: date) -> RiskFreeRate:
        """Return the fixed stamped rate (date-independent by design).

        Parameters
        ----------
        on_date:
            Accepted for :class:`RiskFreeSource` conformance; a fixed
            source ignores it.

        Returns
        -------
        RiskFreeRate
            The configured rate stamped ``source="fixed"`` +
            :data:`RISK_FREE_SOURCE_VERSION`. Never ``None`` — a fixed
            source is total over dates.
        """
        del on_date
        return RiskFreeRate(rate=self.rate, source="fixed", version=RISK_FREE_SOURCE_VERSION)


@dataclass(frozen=True, slots=True)
class CsvRateSource:
    """Step-function daily risk-free curve loaded from a T-bill CSV.

    Built by :func:`load_risk_free_csv`; rows are pre-converted to
    continuously-compounded rates and sorted by date. Lookup is the plan's
    step function: the latest row on/before the query date, NO
    interpolation.

    Attributes
    ----------
    dates:
        Strictly-increasing effective dates (sorted at load).
    rates:
        The converted continuously-compounded rate per date (parallel to
        ``dates``).
    source:
        Provenance label, ``"csv:<file name>"``.
    """

    dates: tuple[date, ...]
    rates: tuple[float, ...]
    source: str

    def __post_init__(self) -> None:
        """Validate the curve eagerly — every construction path, not just the loader.

        DEC-GREEKS-CORRECTNESS-AUDIT-001 (RF-2): a hand-built instance used to
        bypass the ``[0, 1)`` band (only :func:`load_risk_free_csv` validated),
        so a rogue ``rate=-0.5`` could flow into a Greeks solve and be STORED.
        The invariants checked here — parallel ``dates``/``rates``, strictly
        ascending dates, every rate through :func:`_validate_rate` — are exactly
        what the blessed loader already produces, so the blessed path is
        byte-unchanged (fail-fast only for out-of-contract constructions).
        """
        if len(self.dates) != len(self.rates):
            msg = (
                f"csv source {self.source}: {len(self.dates)} dates but "
                f"{len(self.rates)} rates (parallel tuples required)"
            )
            raise RiskFreeError(msg)
        for earlier, later in pairwise(self.dates):
            if later <= earlier:
                msg = (
                    f"csv source {self.source}: dates must be strictly ascending, "
                    f"got {earlier} then {later}"
                )
                raise RiskFreeError(msg)
        for i, rate in enumerate(self.rates):
            _validate_rate(rate, context=f"csv source {self.source} rates[{i}]")

    def rate_on(self, on_date: date) -> RiskFreeRate | None:
        """Step-function lookup: latest row with ``row date <= on_date``.

        Parameters
        ----------
        on_date:
            The query date.

        Returns
        -------
        RiskFreeRate | None
            The stamped rate of the governing row, or ``None`` when
            ``on_date`` precedes the first row — an HONEST absence, never
            a backward extrapolation (Golden Rule #1). A date after the
            last row holds the last row's rate (the step extends forward
            until superseded).
        """
        idx = bisect_right(self.dates, on_date)
        if idx == 0:
            return None
        return RiskFreeRate(
            rate=self.rates[idx - 1],
            source=self.source,
            version=RISK_FREE_SOURCE_VERSION,
        )


def load_risk_free_csv(path: Path) -> CsvRateSource:
    """Load a ``date,yield,tenor_days`` T-bill CSV into a step-function source.

    Each row's money-market simple yield is converted via
    :func:`money_market_to_continuous`; rows may arrive unsorted (sorted at
    load) but duplicate dates are rejected — a duplicate is an ambiguous
    curve, not a tie to break silently.

    Parameters
    ----------
    path:
        CSV file with the exact header ``date,yield,tenor_days``:
        ISO ``YYYY-MM-DD`` date, decimal-fraction simple yield, integer
        tenor in days.

    Returns
    -------
    CsvRateSource
        The loaded step-function source, ``source="csv:<file name>"``.

    Raises
    ------
    RiskFreeError
        If the file is missing or not decodable as UTF-8 (e.g. a UTF-16 /
        binary Excel save), the header is wrong, any row is malformed
        (bad date / yield / tenor, out-of-range converted rate, duplicate
        date), or the file carries zero data rows (an empty curve is a
        configuration defect — fail fast, never a silent always-``None``).
    """
    from brutex.data.guarded_read import read_regular_text

    try:
        text = read_regular_text(path)
    except (OSError, UnicodeDecodeError) as exc:
        # UnicodeDecodeError (a UTF-16 / binary / Excel-saved file) maps into
        # the SAME loud RiskFreeError as an I/O failure, so the documented
        # "broken CSV → RiskFreeError" contract is total
        # (DEC-GREEKS-CORRECTNESS-AUDIT-001 RF-3).
        msg = f"cannot read risk-free CSV {path} (I/O or encoding failure): {exc}"
        raise RiskFreeError(msg) from exc

    reader = csv.reader(text.splitlines())
    header = next(reader, None)
    if header is None or tuple(cell.strip() for cell in header) != _CSV_COLUMNS:
        msg = (
            f"risk-free CSV {path} must start with header "
            f"{','.join(_CSV_COLUMNS)!r}, got {header!r}"
        )
        raise RiskFreeError(msg)

    by_date: dict[date, float] = {}
    for line_no, row in enumerate(reader, start=2):
        if not row:
            continue  # a trailing blank line is not a data defect
        if len(row) != len(_CSV_COLUMNS):
            msg = f"risk-free CSV {path} row {line_no}: expected 3 cells, got {row!r}"
            raise RiskFreeError(msg)
        raw_date, raw_yield, raw_tenor = (cell.strip() for cell in row)
        try:
            row_date = date.fromisoformat(raw_date)
        except ValueError as exc:
            msg = f"risk-free CSV {path} row {line_no}: bad date {raw_date!r}"
            raise RiskFreeError(msg) from exc
        try:
            simple_yield = float(raw_yield)
        except ValueError as exc:
            msg = f"risk-free CSV {path} row {line_no}: bad yield {raw_yield!r}"
            raise RiskFreeError(msg) from exc
        try:
            tenor_days = int(raw_tenor)
        except ValueError as exc:
            msg = f"risk-free CSV {path} row {line_no}: bad tenor_days {raw_tenor!r}"
            raise RiskFreeError(msg) from exc
        if row_date in by_date:
            msg = f"risk-free CSV {path} row {line_no}: duplicate date {raw_date}"
            raise RiskFreeError(msg)
        rate = money_market_to_continuous(simple_yield, tenor_days)
        _validate_rate(rate, context=f"risk-free CSV {path} row {line_no}")
        by_date[row_date] = rate

    if not by_date:
        msg = f"risk-free CSV {path} has no data rows"
        raise RiskFreeError(msg)

    ordered = sorted(by_date.items())
    return CsvRateSource(
        dates=tuple(d for d, _ in ordered),
        rates=tuple(r for _, r in ordered),
        source=f"csv:{path.name}",
    )
