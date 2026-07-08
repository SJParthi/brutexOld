"""Futures-partition preflight — the R4 extension for the futures leg (R14).

DEC-FUTURES-EXEC-TRANSLATION-001: before a run whose INDEX selection will be
futures-translated, verify that the futures contract partitions its trade
months need EXIST ON DISK — and refuse LOUDLY, naming the exact missing
months. NEVER auto-pull, NEVER silently sweep over a hole (R4: "DISK-verified
not just ledger"; refuse-whole, the O-a decision).

Structured as the Phase-1 preflight twins are
(:mod:`brutex.engine.sweep_preflight` — pure fold + thin probe), editing NO
Phase-1 file:

1. :func:`futures_partition_preflight` — the PURE fold over a
   ``{(underlying, year, month): present}`` map (100% line+branch by
   construction, no lake needed);
2. :func:`probe_futures_partitions` — the thin impure probe: resolves each
   trade month's front-month contract(s) through the injected resolver and
   ``Path.exists``-probes the REAL month file on disk (disk beats ledger);
3. :func:`preflight_futures_months` — probe then fold.

Phase-1's launch shim calls :func:`preflight_futures_months` (one additive
call); every branch lives here.

## Which contracts a month needs

Trades inside calendar month ``(y, m)`` resolve the front month as of their OWN
entry dates — which crosses the roll seam mid-month (post-expiry tail days
resolve M+1, R14). The probe therefore resolves the front month as of BOTH the
month's first and last day and requires EVERY distinct resolved contract's
``<yyyy>/<mm>.parquet`` month file on disk. A month with NO resolvable contract
is honestly ABSENT (refused — no calendar means no honest translation), never
skipped.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover — annotation-only imports
    from collections.abc import Iterable, Mapping
    from pathlib import Path

    from brutex.futures.executor import FrontMonthResolver

__all__ = [
    "FuturesPreflightReport",
    "FuturesTradeMonth",
    "futures_partition_preflight",
    "preflight_futures_months",
    "probe_futures_partitions",
]

#: One requested trade month: ``(underlying, year, month)``.
FuturesTradeMonth = tuple[str, int, int]

#: The F&O lake segment + the fetched interval (DEC-FNO-CANDLE-LAKE-001).
_FNO_SEGMENT = "FNO"
_INTERVAL = "1minute"


@dataclass(frozen=True, slots=True)
class FuturesPreflightReport:
    """The futures-partition preflight verdict (the R4/R14 refusal carrier).

    Attributes
    ----------
    missing_months
        Every requested ``(underlying, year, month)`` whose futures partition
        is NOT verified on disk — deterministic underlying-major then
        ``(year, month)`` order.
    requested
        Every month the selection needed (same deterministic order).
    """

    missing_months: tuple[FuturesTradeMonth, ...]
    requested: tuple[FuturesTradeMonth, ...]

    @property
    def ok(self) -> bool:
        """``True`` iff every requested month's futures partition is present."""
        return not self.missing_months

    def remediation(self) -> str:
        """The loud refusal line: names every missing month + the pull remedy.

        Never auto-pulls (R4) — the remedy is the operator's ``/universe``
        pull, stated explicitly.
        """
        named = ", ".join(f"{u} {y:04d}-{m:02d}" for u, y, m in self.missing_months)
        return (
            f"futures translation preflight REFUSED: no on-disk front-month futures "
            f"partition for {len(self.missing_months)} trade month(s): {named}. "
            f"Pull the missing F&O months on /universe first — the sweep never "
            f"auto-pulls and never silently skips a hole (R4/R14)."
        )


def _ordered(months: Iterable[FuturesTradeMonth]) -> tuple[FuturesTradeMonth, ...]:
    """Deterministic underlying-major then (year, month) order, deduplicated."""
    return tuple(sorted(set(months)))


def futures_partition_preflight(
    present: Mapping[FuturesTradeMonth, bool], *, requested: Iterable[FuturesTradeMonth]
) -> FuturesPreflightReport:
    """PURE fold: fail-closed presence verdict over the probed month map.

    A requested month is missing unless *present* maps it to ``True`` — a month
    ABSENT from the map counts missing (fail-closed: an unprobed month is never
    assumed present).

    Parameters
    ----------
    present
        ``{(underlying, year, month): disk-verified?}`` from
        :func:`probe_futures_partitions` (or any test-supplied map).
    requested
        Every month the selection needs.

    Returns
    -------
    FuturesPreflightReport
        The verdict; ``.ok`` / ``.remediation()`` drive the refusal.
    """
    ordered = _ordered(requested)
    missing = tuple(month for month in ordered if present.get(month) is not True)
    return FuturesPreflightReport(missing_months=missing, requested=ordered)


def probe_futures_partitions(
    lake_root: Path,
    *,
    resolver: FrontMonthResolver,
    trade_months: Iterable[FuturesTradeMonth],
) -> dict[FuturesTradeMonth, bool]:
    """Thin impure probe: disk-verify each trade month's futures partition(s).

    For each ``(underlying, year, month)``: resolve the front-month contract as
    of the month's FIRST and LAST day (both sides of the roll seam), then
    require EVERY distinct resolved contract's REAL month file
    (``bars/<exch>/FNO/<symbol>/1minute/<yyyy>/<mm>.parquet``) to exist ON DISK
    (``Path.exists`` — disk beats ledger, R4). No resolvable contract at all →
    ``False`` (an empty registry month is an honest refusal, never a skip).

    Parameters
    ----------
    lake_root
        The lake root (parent of ``bars/``).
    resolver
        The front-month resolver seam
        (:class:`~brutex.futures.contract_resolver.RegistryFrontMonthResolver`
        in production).
    trade_months
        The months to probe.

    Returns
    -------
    dict[FuturesTradeMonth, bool]
        Presence per requested month.
    """
    present: dict[FuturesTradeMonth, bool] = {}
    for underlying, year, month in _ordered(trade_months):
        first = date(year, month, 1)
        last = date(year, month, calendar.monthrange(year, month)[1])
        symbols = {
            symbol
            for probe_date in (first, last)
            if (symbol := resolver.resolve(instrument=underlying, on_date=probe_date)) is not None
        }
        if not symbols:
            present[underlying, year, month] = False
            continue
        present[underlying, year, month] = all(
            (
                lake_root
                / "bars"
                / symbol.split("-")[0]
                / _FNO_SEGMENT
                / symbol
                / _INTERVAL
                / f"{year:04d}"
                / f"{month:02d}.parquet"
            ).exists()
            for symbol in symbols
        )
    return present


def preflight_futures_months(
    lake_root: Path,
    *,
    resolver: FrontMonthResolver,
    trade_months: Iterable[FuturesTradeMonth],
) -> FuturesPreflightReport:
    """Probe then fold — the one call Phase-1's launch shim makes.

    Parameters
    ----------
    lake_root, resolver, trade_months
        See :func:`probe_futures_partitions`.

    Returns
    -------
    FuturesPreflightReport
        ``.ok`` gates the run; ``.remediation()`` is the loud refusal text
        naming the exact missing months (never auto-pulled).
    """
    months = _ordered(trade_months)
    present = probe_futures_partitions(lake_root, resolver=resolver, trade_months=months)
    return futures_partition_preflight(present, requested=months)
