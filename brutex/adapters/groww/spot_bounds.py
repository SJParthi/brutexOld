"""Smart 1-minute fetch bounds derived from a SPOT symbol's 1-day lake coverage.

DEC-SPOT-1DAY-FIRST-001 (MIGRATION.md entry #836) — the **spot twin** of the
option/future :mod:`~brutex.adapters.groww.contract_bounds`
(DEC-DERIV-MINUTE-BOUNDS-001 / DEC-HISTORICAL-1DAY-FIRST-001). A spot index does
not begin trading on the same calendar day across vendors — Groww serves NIFTY
1-day from 2020, but SENSEX only from ~2022 and FINNIFTY only from ~2025 — so
pulling 1-minute candles blindly from the ``2020-01-01`` history floor (CLAUDE.md
§3) for every symbol wastes the bulk of the per-symbol API calls on dead periods
the symbol did not yet trade (and surfaces them as permanent empty windows the
operator sees as "stuck at partial"). After the **cheap 1-day pull** lands, each
symbol's REAL data window is visible in the lake; this module turns that 1-day
coverage into a per-symbol ``(start, end)`` that PINS where the **heavy** 1-minute
fill begins — never blindly from 2020.

Why the START moves but the END does NOT (the spot ≠ option divergence)
-----------------------------------------------------------------------
* **start** = the daily coverage's first observed trading day — the date the
  vendor's 1-day series actually begins for THIS symbol. Because the vendor
  aggregates the daily candle from the same trades that produce the intraday
  bars, every day with 1-minute data also has a 1-day candle (daily ⊇ minute
  trading days), so starting the minute pull at the first daily day can never
  miss earlier minute bars.
* **end** = the FULL requested ``full_end`` — UNLIKE an option/future, a spot
  INDEX never expires, so its 1-minute fill must run to the requested upper
  bound (typically the previous trading day), NOT be clamped to the last daily
  bar. (Options clamp the end to the contract ``expiry`` — its hard last
  tradable day — because the contract is dead afterwards; a spot index has no
  such ceiling. This is why this module does NOT reuse
  :func:`~brutex.adapters.groww.contract_bounds.coverage_to_minute_window`, whose
  end clamps to ``expiry``/``last_date`` + 1.)

The 3c rule — a symbol with NO 1-day data (operator decision 2026-06-19)
------------------------------------------------------------------------
A symbol the lake holds ZERO 1-day bars for has no start date to derive from, so
its 1-minute fill is **OMITTED** from the returned map (the caller EXCLUDES it
from the minute pull and reports "pull 1-day first for <symbol>"). This is the
honest verify-first default the operator chose: never pull 1-minute blindly when
the 1-day anchor is absent. (The option twin omits a no-coverage contract for the
same reason — its 1-minute would be empty too.) NEVER a fabricated start date
(Golden Rule #1).

Honest envelope (Golden Rule #10)
---------------------------------
* Reading the 1-day coverage is **O(partitions)** — it consults the lake
  :class:`~brutex.data.catalog.Catalog`'s partition metadata, preceded by a
  one-time **O(P) reconcile** scoped to the SPOT segments present (never the
  giant ``FNO`` contract tree). The per-symbol window LOOKUP from the returned
  map (the spine's hot use) is **O(1)** (one dict ``get``); the network pull it
  bounds is O(windows), I/O-bound, NOT O(1)-total. No O(1)-total claim is made.
* **This is a CORRECTNESS fix presented as a bound, not a silent narrowing.**
  The keep-first :class:`~brutex.data.sink.BarSink` + the unified
  :class:`~brutex.data.window_ledger.WindowLedger` already make the lake bytes
  identical whether a symbol is pulled over its bounded window or the full range
  (Golden Rule #7); bounding never *changes* the data — it avoids issuing the
  first-pass requests over the dead pre-listing periods AND stops the ledger from
  planning phantom never-fillable windows there.
* **Soundness precondition (stated, not hidden):** bounding is only sound when
  the 1-day pull is COMPLETE. The daily fetch is window-atomic — a failed daily
  window writes nothing — so an incomplete daily pull surfaces here as *absent*
  coverage → the symbol is omitted (skip-and-report), never a too-narrow window.

Scope (CLAUDE.md §3)
--------------------
This bounds the SPOT candle ingest for the brute-force indices + the lake's
reference-only spot universe. It lives in the adapter/data layer and NEVER
touches the brute-force engine or the 52-condition vocabulary.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from typing import TYPE_CHECKING

import structlog

from brutex.adapters.groww.constants import TRACK1_DAILY_INTERVAL
from brutex.adapters.groww.contract_bounds import ContractCoverage
from brutex.calendars.nse import IST
from brutex.data.errors import CatalogError
from brutex.models import Instrument

if TYPE_CHECKING:  # pragma: no cover — typing-only (duckdb stays lazy, mirrors contract_bounds)
    from pathlib import Path

    from brutex.data.catalog import Catalog

#: Structured logger — every degrade (catalog unavailable / a symbol skipped /
#: a symbol with no 1-day coverage) is logged (Golden Rule #10 observability), so
#: a wholesale degrade is LOUD, not silent, and the per-pull bounded/skipped split
#: is visible to the operator.
_LOG = structlog.get_logger("brutex.adapters.groww.spot_bounds")

#: Per-symbol ``{symbol_name: (start, end)}`` minute-window map — the spot analog
#: of :data:`~brutex.adapters.groww.option_ingest.ContractWindows`. A symbol ABSENT
#: from the map has no 1-day anchor → its 1-minute fill is skipped (the 3c rule).
SpotWindows = dict[str, tuple[date, date]]

__all__ = [
    "SpotWindows",
    "discover_spot_minute_windows",
    "spot_coverage_to_minute_window",
    "spot_daily_coverage",
]


def spot_daily_coverage(
    catalog: Catalog,
    *,
    instrument: Instrument,
    interval: str = TRACK1_DAILY_INTERVAL,
) -> ContractCoverage | None:
    """Return *instrument*'s inclusive ``[first_date, last_date]`` of lake 1-day data.

    A PURE catalog read (no reconcile, no network): it lists the symbol's partitions
    for ``(instrument.exch_id, instrument.segment, instrument.symbol_name, interval)``
    — the EXACT coordinates :func:`~brutex.adapters.groww.ingest.ingest_spot` writes
    spot bars under (``exch_id.value`` / ``segment.value`` / ``symbol_name``) — and
    folds them to the global ``min(first_ts)`` / ``max(last_ts)``, converting each to
    its IST trading day. The caller is responsible for having reconciled the catalog
    from the lake first (see :func:`discover_spot_minute_windows`, which reconciles
    ONCE per spot segment before reading per symbol — the blessed single-reconcile
    pattern; reconciling per symbol would be O(symbols × rows) of disk I/O + hashing).

    The ``min``/``max`` are taken over ALL partitions (never the first/last by the
    catalog's ``(year, month)`` sort order): under midnight-IST stamping a Jan-1-IST
    bar lives in the *December* UTC partition, so partition sort order is NOT IST-date
    order — only the global min/max is correct (mirrors
    :func:`~brutex.adapters.groww.contract_bounds.contract_daily_coverage`).

    Parameters
    ----------
    catalog
        An OPEN :class:`~brutex.data.catalog.Catalog`, already reconciled for the
        symbol's spot coordinates.
    instrument
        The spot instrument whose lake coverage to read.
    interval
        The interval whose coverage defines the window (default
        :data:`~brutex.adapters.groww.constants.TRACK1_DAILY_INTERVAL` — ``"1day"``).

    Returns
    -------
    ContractCoverage | None
        The inclusive IST-date coverage, or ``None`` when the lake holds no
        partitions for the symbol at *interval* (its 1-day pull has not landed yet —
        the caller OMITS it from the minute pull, never silently pulls 1-minute from
        the 2020 floor).
    """
    entries = catalog.list_partitions(
        exchange=instrument.exch_id.value,
        segment=instrument.segment.value,
        symbol=instrument.symbol_name,
        interval=interval,
    )
    if not entries:
        return None
    first_ts = min(entry.first_ts for entry in entries)
    last_ts = max(entry.last_ts for entry in entries)
    return ContractCoverage(
        first_date=first_ts.astimezone(IST).date(),
        last_date=last_ts.astimezone(IST).date(),
    )


def spot_coverage_to_minute_window(
    coverage: ContractCoverage | None,
    *,
    full_start: date,
    full_end: date,
) -> tuple[date, date]:
    """Map a spot symbol's 1-day *coverage* to the half-open ``[start, end)`` minute window.

    The smart conversion (see the module docstring for the START-moves-END-does-not
    rationale):

    * ``coverage is None`` → the full ``(full_start, full_end)`` range — defensive
      (the symbol is normally OMITTED upstream when it has no 1-day data; this keeps
      the pure mapper total + never-skip if a caller passes ``None``).
    * else ``start = max(full_start, coverage.first_date)`` (the listing day the
      1-day pull discovered) and ``end = full_end`` (a spot INDEX never expires, so
      the minute fill runs to the requested upper bound — NOT clamped to the last
      daily bar, the option-only behaviour).
    * a degenerate ``start >= end`` (coverage entirely at/after ``full_end`` — e.g. a
      symbol that began trading after the requested window) → the full range, the
      conservative never-skip choice.

    Pure + **O(1)** (a couple of ``max``/``min`` comparisons, no I/O).

    Parameters
    ----------
    coverage
        The symbol's inclusive 1-day coverage, or ``None``.
    full_start, full_end
        The half-open ``[full_start, full_end)`` range the unbounded pull would use
        (the same bounds the ingest spine is given).

    Returns
    -------
    tuple[date, date]
        The half-open ``(start, end)`` minute fetch window — always within
        ``[full_start, full_end)``, in the SAME semantics the ingest spine's
        ``start``/``end`` use.
    """
    if coverage is None:
        return (full_start, full_end)
    start = max(full_start, coverage.first_date)
    if start >= full_end:
        return (full_start, full_end)
    return (start, full_end)


def discover_spot_minute_windows(
    catalog: Catalog,
    *,
    lake_root: Path,
    instruments: Iterable[Instrument],
    full_start: date,
    full_end: date,
    interval: str = TRACK1_DAILY_INTERVAL,
    reconcile: bool = True,
) -> SpotWindows:
    """Build the per-symbol ``{symbol_name: (start, end)}`` minute-pull window map.

    The spot driver the pull controller consumes (the twin of
    :func:`~brutex.dashboard.historical_pull._discover_start_dates`): it reconciles
    the catalog ONCE per DISTINCT spot segment present in *instruments* (typically
    ``INDEX`` for the brute-force indices + ``CASH`` for stocks — never the giant
    ``FNO`` contract tree) from the on-disk Parquet, then reads each symbol's
    :func:`spot_daily_coverage` (a pure O(partitions) catalog lookup) and converts it
    to a half-open minute window via :func:`spot_coverage_to_minute_window`.

    The returned map contains ONLY symbols whose 1-day coverage the lake holds. A
    symbol with NO 1-day coverage is **OMITTED** (the caller EXCLUDES it from the
    minute pull + reports "pull 1-day first" — the 3c rule, operator decision
    2026-06-19), never pulled 1-minute from the blind 2020 floor and never a
    fabricated start (Golden Rule #1). A missing/locked catalog or a per-symbol
    catalog error degrades that symbol (or the whole pass) to ABSENT — every degrade
    is logged LOUDLY, the honest conservative choice (Golden Rule #10).

    Parameters
    ----------
    catalog
        An OPEN :class:`~brutex.data.catalog.Catalog` to reconcile + read.
    lake_root
        Top-level lake directory (the parent of ``bars/``), for the reconcile.
    instruments
        The resolved spot instruments to bound.
    full_start, full_end
        The half-open range the unbounded pull would use.
    interval
        The coverage interval (default ``"1day"``).
    reconcile
        When ``True`` (default), reconcile the catalog from the lake (once per
        distinct spot segment) before reading. Pass ``False`` when the caller has
        already reconciled to avoid repeating the O(P) pass.

    Returns
    -------
    SpotWindows
        ``{symbol_name: (start, end)}`` for every symbol with 1-day coverage. Empty
        when the catalog is unavailable (a degrade logged LOUDLY) or no symbol has
        coverage — both leave the caller to skip the minute pull entirely.
    """
    instrument_list = list(instruments)
    if reconcile:
        # Lazy import (mirrors contract_bounds): pulls polars/pyarrow only when run, so
        # importing this module never requires the P2 [data] extras.
        from brutex.data.catalog_reconcile import reconcile_catalog_from_lake

        # Reconcile ONCE per DISTINCT spot segment present — scoped so the O(P) scan
        # never walks the giant FNO contract tree (segment=None would). Deterministic
        # order so the reconcile sequence is reproducible (Rule #4).
        segments = sorted({inst.segment.value for inst in instrument_list})
        try:
            for segment in segments:
                reconcile_catalog_from_lake(
                    lake_root=lake_root, catalog=catalog, segment=segment, interval=interval
                )
        except CatalogError as exc:
            # The catalog file is busy / the data-lake deps are missing — degrade to
            # an EMPTY map (the caller skips the whole minute pull), loud not silent.
            _LOG.warning(
                "spot_bounds.reconcile_unavailable",
                error=str(exc),
                interval=interval,
                symbols=len(instrument_list),
            )
            return {}

    windows: SpotWindows = {}
    bounded = 0
    no_coverage = 0
    skipped = 0
    for instrument in instrument_list:
        symbol = instrument.symbol_name
        try:
            coverage = spot_daily_coverage(catalog, instrument=instrument, interval=interval)
        except (CatalogError, ValueError) as exc:
            # A mid-loop catalog error quarantines THIS symbol only (omitted → its
            # minute pull is skipped + reported) — mirrors the ingest spine's
            # per-symbol quarantine; one bad symbol never blanks the whole map.
            _LOG.warning("spot_bounds.symbol_skipped", symbol=symbol, error=str(exc))
            skipped += 1
            continue
        if coverage is None:
            # No 1-day data for this symbol — OMIT it (the 3c skip-and-report rule).
            no_coverage += 1
            continue
        windows[symbol] = spot_coverage_to_minute_window(
            coverage, full_start=full_start, full_end=full_end
        )
        bounded += 1

    _LOG.info(
        "spot_bounds.derived",
        interval=interval,
        symbols=len(instrument_list),
        bounded=bounded,
        no_coverage=no_coverage,
        skipped=skipped,
    )
    return windows
