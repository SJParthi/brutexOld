"""Smart 1-minute fetch bounds derived from a contract's 1-day lake coverage.

DEC-DERIV-MINUTE-BOUNDS-001 (MIGRATION.md entry #711). An F&O contract only
*lives* from its listing day to its expiry, so pulling 1-minute candles over the
full ``2020-01-01 → today`` range (CLAUDE.md §3) for every strike wastes the bulk
of the per-contract API calls on dead periods the contract did not yet / no longer
trades. After the **cheap 1-day pull** lands (one ``get_historical_candles`` window
per contract — DEC-GROWW-OPTION-INGEST-001, #702), each contract's REAL data window
is visible in the lake. This module turns that 1-day coverage into a per-contract
``(start, end)`` that bounds the **heavy** 1-minute pull.

**This is an OPTIMIZATION, not a correctness fix (Golden Rule #10).** The unified
:class:`~brutex.data.window_ledger.WindowLedger` already makes a re-pull cheap (a
settled contract-range window is skipped wholesale) and the keep-first
:class:`~brutex.data.sink.BarSink` already guarantees the lake bytes are identical
whether a contract is pulled over its bounded window or the full range (Golden
Rule #7 — idempotent at the data layer). Bounding therefore never *changes* the
data — it only avoids issuing the **first-pass** requests for the dead periods. If
a contract has no 1-day coverage yet, we fall back to the full range (NEVER skip
silently — a missing-coverage contract is pulled in full, not dropped).

Why the START uses the 1-day coverage but the END uses the contract's expiry
----------------------------------------------------------------------------
* **start** = the daily coverage's first observed trading day. The instrument
  master carries no reliable *listing* date, so the 1-day pull is the cheap way to
  discover when the contract began trading. Because the vendor aggregates the daily
  candle from the same trades that produce the intraday bars, every day with
  1-minute data also has a 1-day candle (daily ⊇ minute trading days), so starting
  the minute pull at the first daily day can never miss earlier minute bars.
* **end** = the contract's ``expiry`` + 1 day (end-EXCLUSIVE), clamped to the
  requested range. We deliberately use the contract metadata ``expiry`` — its hard,
  lag-free last tradable day — rather than the daily series' last observed day,
  because a still-LIVE contract's most recent session has 1-minute bars (after the
  15:30 IST close) before its 1-day candle is published the *next* day
  (DEC-DAILY-CANDLE-CLAMP-001 / ``ingest_common.clamp_window_end``). Bounding the
  end at the stale daily ``last_date`` would truncate that live edge; the expiry
  does not. For an EXPIRED contract the two coincide. When ``expiry`` is absent
  (defensive — a vendor-neutral perpetual has none) we fall back to the daily
  ``last_date`` + 1 day.

Honest envelope (Golden Rule #10)
---------------------------------
* Reading the 1-day coverage is **O(partitions)** — it consults the lake
  :class:`~brutex.data.catalog.Catalog`'s partition metadata (the same metadata
  ``get_bars`` lists before it reads rows), so a date *range* costs only the
  per-partition ``first_ts``/``last_ts`` rather than an O(rows) re-read. It is
  preceded by a one-time **O(P) reconcile** (each Parquet opened once to read its
  ``timestamp`` column + an ``sha256`` over the bytes — see
  :func:`~brutex.data.catalog_reconcile.reconcile_catalog_from_lake`); this mirrors
  the blessed :func:`brutex.engine.cascade_wire.load_bars_for_manifest` "reconcile
  once, then read many" pattern. NEITHER is O(1)-total and no
  such claim is made.
* The per-contract window LOOKUP from the returned map (the spine's hot use) is
  **O(1)**; the network pull it bounds is O(windows), I/O-bound, NOT O(1)-total.
* **Soundness precondition (stated, not hidden):** bounding is only sound when the
  1-day pull is COMPLETE. The single-call daily fetch is all-or-nothing — a failed
  daily pull writes NOTHING to the lake (the bars are accumulated then discarded on
  the raising window — ``candles.fetch_candle_windows`` / ``option_ingest`` append
  only after the full fetch returns), so an incomplete daily pull surfaces here as
  *absent* coverage → the full-range fallback, never as a too-narrow window.

Scope (CLAUDE.md §3)
--------------------
Option/future contract bars are **LAKE / REFERENCE_ONLY** (DEC-P0-063) — this
module bounds their reference-only ingest and NEVER touches the brute-force engine
or the shared condition vocabulary. It lives in the adapter/data layer; wiring the
derived windows into the production derivative-pull driver is owned by the in-flight
derivatives PR (this module is the adapter-layer building block it consumes).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING, Final

import structlog

from brutex.adapters.groww.constants import OPTION_SEGMENT, TRACK1_DAILY_INTERVAL
from brutex.adapters.groww.option_ingest import contract_exchange
from brutex.adapters.protocol import DerivativeContract
from brutex.calendars.nse import IST
from brutex.data.errors import CatalogError

if TYPE_CHECKING:  # pragma: no cover — typing-only (duckdb stays lazy, mirrors cascade_wire)
    from pathlib import Path

    from brutex.data.catalog import Catalog

#: Structured logger — every fall-back-to-full-range path is logged (Golden Rule
#: #10 observability), so a wholesale degrade (catalog unavailable) is LOUD, not
#: silent, and the per-pull bounded/unbounded split is visible to the operator.
_LOG = structlog.get_logger("brutex.adapters.groww.contract_bounds")

#: Default OPTION minute-window lookback — the per-OPTION (``CE`` / ``PE``) DEFAULT
#: span when the operator passes no explicit from/to dates
#: (DEC-1MINUTE-ONLY-DERIVE-1DAY-001). An option defaults to a fixed 30-day lookback
#: (``expiry − 30 days → expiry``): an option's liquid life is short and the chain is
#: huge, so a tight default keeps the pull bounded. The operator's explicit from/to
#: dates still override it. (Historically named ``DEFAULT_FNO_LOOKBACK_DAYS`` when
#: ALL F&O shared one 30-day default; DEC-FUTURES-LOOKBACK-60D-001 splits FUTURES out
#: to its own wider 60-day default below, so this constant is now OPTION-specific.)
DEFAULT_OPTION_LOOKBACK_DAYS: Final[int] = 30

#: Legacy alias — kept so existing importers of ``DEFAULT_FNO_LOOKBACK_DAYS`` (the
#: pre-split uniform constant) continue to resolve. It equals the OPTION lookback
#: (30); FUTURES now use the wider :data:`DEFAULT_FUTURES_LOOKBACK_DAYS` below.
DEFAULT_FNO_LOOKBACK_DAYS: Final[int] = DEFAULT_OPTION_LOOKBACK_DAYS

#: Default FUTURES minute-window lookback — the per-FUTURE (``FUT``) DEFAULT span
#: when the operator passes no explicit from/to dates. Operator-directed
#: (DEC-FUTURES-LOOKBACK-60D-001): a future defaults to a fixed 60-DAY lookback
#: (``expiry − 60 days → expiry``), wider than an option's 30-day default because a
#: future carries a continuous liquid life over its whole serial month and the
#: operator wants the fuller recent tail. A fixed DAY-count (NOT calendar months).
#: The operator's explicit from/to dates still override it.
DEFAULT_FUTURES_LOOKBACK_DAYS: Final[int] = 60

__all__ = [
    "DEFAULT_FNO_LOOKBACK_DAYS",
    "DEFAULT_FUTURES_LOOKBACK_DAYS",
    "DEFAULT_OPTION_LOOKBACK_DAYS",
    "ContractCoverage",
    "contract_daily_coverage",
    "coverage_to_minute_window",
    "default_fno_minute_window",
    "derive_contract_minute_windows",
    "fno_window_opens_on",
]


def fno_window_opens_on(expiry: date, *, is_option: bool) -> date:
    """The first envelope day a contract's DEFAULT window is OPEN — ``expiry − lookback``.

    DEC-FNO-FAR-EXPIRY-WINDOW-001 (operator law). The ONE place the per-type default
    lookback anchor is computed (CLAUDE.md §11 COMMON): an OPTION (``CE``/``PE``) opens
    :data:`DEFAULT_OPTION_LOOKBACK_DAYS` (30) days before its expiry, a FUTURE (``FUT``)
    :data:`DEFAULT_FUTURES_LOOKBACK_DAYS` (60) days before. Consumed by
    :func:`default_fno_minute_window` (the window START once open — fixed, it NEVER
    moves) and by the pull's not-yet-open skip log (the honest ``opens_on`` date the
    operator sees for a contract that is fetched not at all yet). Pure, O(1).

    Parameters
    ----------
    expiry
        The contract's expiry date (its hard last tradable day).
    is_option
        ``True`` for a ``CE`` / ``PE`` option (30-day lookback), ``False`` for a
        ``FUT`` future (60-day lookback).

    Returns
    -------
    date
        ``expiry − lookback`` — the fixed, never-moving default window start.
    """
    lookback_days = DEFAULT_OPTION_LOOKBACK_DAYS if is_option else DEFAULT_FUTURES_LOOKBACK_DAYS
    return expiry - timedelta(days=lookback_days)


@dataclass(frozen=True, slots=True)
class ContractCoverage:
    """The inclusive ``[first_date, last_date]`` of a contract's ACTUAL 1-day lake data.

    Both bounds are IST trading dates (the timestamps are stored UTC and converted
    via :data:`~brutex.calendars.nse.IST` so a midnight-IST daily bar — stored as
    the previous UTC day — still resolves to its IST trading day).

    Parameters
    ----------
    first_date, last_date
        The earliest and latest IST trading day the lake holds a 1-day candle for.

    Raises
    ------
    ValueError
        If ``last_date`` precedes ``first_date`` (the invariant is enforced at
        construction — the house frozen-and-validated style, mirroring
        :class:`~brutex.adapters.protocol.DerivativeContract`).
    """

    first_date: date
    last_date: date

    def __post_init__(self) -> None:
        if self.last_date < self.first_date:
            raise ValueError(
                f"ContractCoverage last_date {self.last_date} precedes first_date {self.first_date}"
            )


def contract_daily_coverage(
    catalog: Catalog,
    *,
    contract: DerivativeContract,
    interval: str = TRACK1_DAILY_INTERVAL,
) -> ContractCoverage | None:
    """Return *contract*'s inclusive ``[first_date, last_date]`` of lake 1-day data.

    A PURE catalog read (no reconcile, no network): it lists the contract's
    partitions for ``(exchange, segment="FNO", symbol=contract.symbol, interval)``
    and folds them to the global ``min(first_ts)`` / ``max(last_ts)``, converting
    each to its IST trading day. The caller is responsible for having reconciled the
    catalog from the lake first (see :func:`derive_contract_minute_windows`, which
    reconciles ONCE for the whole FNO/interval space before reading per contract —
    the blessed single-reconcile pattern; calling reconcile per contract would be
    O(contracts × rows) of disk I/O + hashing).

    The ``min``/``max`` are taken over ALL partitions (never the first/last by the
    catalog's ``(year, month)`` sort order): under midnight-IST stamping a Jan-1-IST
    bar lives in the *December* UTC partition, so partition sort order is NOT IST-date
    order — only the global min/max is correct.

    Parameters
    ----------
    catalog
        An OPEN :class:`~brutex.data.catalog.Catalog`, already reconciled for the
        contract's coordinates.
    contract
        The derivative contract whose lake coverage to read.
    interval
        The interval whose coverage defines the window (default
        :data:`~brutex.adapters.groww.constants.TRACK1_DAILY_INTERVAL` — ``"1day"``).

    Returns
    -------
    ContractCoverage | None
        The inclusive IST-date coverage, or ``None`` when the lake holds no
        partitions for the contract at *interval* (a contract whose 1-day pull has
        not landed yet — the caller falls back to the full range, never skips).
    """
    entries = catalog.list_partitions(
        exchange=contract_exchange(contract),
        segment=OPTION_SEGMENT,
        symbol=contract.symbol,
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


def coverage_to_minute_window(
    coverage: ContractCoverage | None,
    *,
    full_start: date,
    full_end: date,
    expiry: date | None = None,
) -> tuple[date, date]:
    """Map a contract's 1-day *coverage* to the half-open ``[start, end)`` minute window.

    The smart conversion (see the module docstring for the START-vs-END rationale):

    * ``coverage is None`` → the full ``(full_start, full_end)`` range — the contract
      has no 1-day data to bound against, so it is pulled IN FULL (never skipped).
    * else ``start = max(full_start, coverage.first_date)`` (the listing day the
      1-day pull discovered) and ``end = min(full_end, ceiling + 1 day)`` where
      ``ceiling`` is ``expiry`` (the lag-free trading-life bound) when supplied else
      ``coverage.last_date`` (the fallback when no expiry is known). The ``+ 1 day``
      makes the half-open ``end`` INCLUSIVE of the ceiling day's intraday bars.
    * a degenerate ``start >= end`` (e.g. coverage entirely outside the requested
      range, or an ``expiry`` before the listing day) → the full range, the
      conservative never-skip choice.

    Parameters
    ----------
    coverage
        The contract's inclusive 1-day coverage, or ``None``.
    full_start, full_end
        The half-open ``[full_start, full_end)`` range the unbounded pull would use
        (the same bounds the ingest spine is given).
    expiry
        The contract's expiry date (its hard last tradable day), or ``None`` to fall
        back to the daily ``last_date`` as the end ceiling.

    Returns
    -------
    tuple[date, date]
        The half-open ``(start, end)`` fetch window — always within
        ``[full_start, full_end)``, expressed in the SAME semantics the ingest spine's
        ``start``/``end`` use.
    """
    if coverage is None:
        return (full_start, full_end)
    ceiling = expiry if expiry is not None else coverage.last_date
    start = max(full_start, coverage.first_date)
    end = min(full_end, ceiling + timedelta(days=1))
    if start >= end:
        return (full_start, full_end)
    return (start, end)


def default_fno_minute_window(
    expiry: date | None,
    *,
    full_start: date,
    full_end: date,
    is_option: bool,
) -> tuple[date, date] | None:
    """The DEFAULT half-open ``[start, end)`` 1-minute window for an F&O contract.

    DEC-1MINUTE-ONLY-DERIVE-1DAY-001 / DEC-FUTURES-LOOKBACK-60D-001. When the operator
    passes NO explicit from/to dates, an F&O contract's 1-minute pull defaults to the
    recent tail of its trading life: ``expiry − <lookback>`` days → ``min(full_end,
    expiry + 1 day)`` (the ``+ 1 day`` end is end-EXCLUSIVE so the expiry day's own
    intraday bars are INCLUDED — the SAME ceiling convention
    :func:`coverage_to_minute_window` uses). The lookback is SPLIT by contract type
    (operator-directed): an OPTION (``CE`` / ``PE``) uses
    :data:`DEFAULT_OPTION_LOOKBACK_DAYS` (30) — its liquid life is short and the chain
    is huge — while a FUTURE (``FUT``) uses the wider
    :data:`DEFAULT_FUTURES_LOOKBACK_DAYS` (60), the recent two-month tail of its serial
    month. Both are fixed DAY-counts (NOT calendar months).

    Both bounds are clamped INTO the operator's resolved ``[full_start, full_end)``
    range (the historical floor → previous-day envelope), so the default never widens
    scope. A contract with NO ``expiry`` (defensive — a vendor-neutral perpetual has
    none) or whose window falls entirely BEFORE the envelope (an already-expired
    contract on a later sub-range) falls back to the FULL ``(full_start, full_end)``
    range — the conservative never-skip choice (Golden Rule #10).

    **NOT-YET-OPEN skip (DEC-FNO-FAR-EXPIRY-WINDOW-001 — operator law):** the default
    window is STRICTLY ``[expiry − lookback, expiry] ∩ [full_start, full_end)`` —
    never a substitute window. A far-future contract whose window has not OPENED yet
    (``expiry − lookback`` ON/AFTER ``full_end`` — e.g. a September monthly pulled in
    early July) returns ``None``: the caller skips it entirely — ZERO vendor calls,
    ZERO ledger rows — until the calendar reaches ``expiry − lookback``
    (:func:`fno_window_opens_on`), at which point the normal window with its FIXED
    start (``expiry − lookback``, never moving across daily pulls) kicks in. The
    pre-fix code instead degraded this case to the FULL 2020→today range (~80 ×
    30-day chunks of dead history per contract; a verified forensic audit of a real
    production log measured 4 such contracts × 80 chunks = 320 vendor calls = 22.5%
    of the entire ADANIENT pull wasted). A DEGENERATE envelope
    (``full_start >= full_end``) keeps the pre-existing full-range fallback (never
    ``None``), and explicit operator dates never reach this helper at all
    (``historical_pull._resolve_minute_windows`` short-circuits them verbatim).

    Parameters
    ----------
    expiry
        The contract's expiry date (its hard last tradable day), or ``None``.
    full_start, full_end
        The half-open ``[full_start, full_end)`` range the operator resolved (the
        explicit-dates-or-default envelope from ``_resolve_range``).
    is_option
        ``True`` for a ``CE`` / ``PE`` option (30-day lookback), ``False`` for a
        ``FUT`` future (60-day lookback). Required — the caller derives it from the
        contract's type (``contract_type in {"CE", "PE"}``), so the split is never
        guessed.

    Returns
    -------
    tuple[date, date] | None
        The half-open ``(start, end)`` 1-minute window, always within
        ``[full_start, full_end)`` — or ``None`` for a NOT-YET-OPEN contract
        (skip: fetch nothing this pull).
    """
    if expiry is None:
        return (full_start, full_end)
    lookback_start = fno_window_opens_on(expiry, is_option=is_option)
    if lookback_start >= full_end and full_start < full_end:
        # NOT-YET-OPEN (operator law, DEC-FNO-FAR-EXPIRY-WINDOW-001): the strict
        # window [expiry − lookback, expiry] has not intersected the envelope yet —
        # fetch NOTHING (the caller skips the contract: zero vendor calls, zero
        # ledger rows) until the calendar reaches expiry − lookback. Never a
        # substitute window; the pre-fix code degraded this to the FULL 2020→today
        # range. A degenerate envelope (full_start >= full_end) is NOT a skip — it
        # keeps the pre-existing conservative full-range fallback below.
        return None
    start = max(full_start, lookback_start)
    end = min(full_end, expiry + timedelta(days=1))
    if start >= end:
        return (full_start, full_end)
    return (start, end)


def derive_contract_minute_windows(
    catalog: Catalog,
    *,
    lake_root: Path,
    contracts: Iterable[DerivativeContract],
    full_start: date,
    full_end: date,
    interval: str = TRACK1_DAILY_INTERVAL,
    reconcile: bool = True,
) -> dict[str, tuple[date, date]]:
    """Build the per-contract ``{symbol: (start, end)}`` minute-pull window map.

    The convenience driver the derivative-pull layer consumes: it reconciles the
    catalog ONCE for the whole ``FNO``/*interval* space from the on-disk Parquet
    (the dashboard/derivative pull builds its sink with ``catalog=None``, so the
    ``partitions`` table is empty until reconciled — the same reason
    :func:`brutex.engine.cascade_wire.load_bars_for_manifest` reconciles), then reads
    each contract's coverage (a pure O(partitions) catalog lookup) and converts it to
    a half-open minute window via :func:`coverage_to_minute_window`.

    The returned map contains ONLY contracts with a usable bounded window. A contract
    with no 1-day coverage is OMITTED (the spine then uses its global ``start``/``end``
    for it — the never-skip fallback), and a contract whose coverage collapses to the
    full range (the disjoint edge) is logged but omitted likewise. The whole map is
    consumed by :func:`~brutex.adapters.groww.option_ingest.run_groww_option_ingest`
    via its ``contract_windows`` parameter; the spine applies a window ONLY to
    sub-daily intervals (never to the ``"1day"`` interval that produced the coverage,
    which must keep advancing — DEC-DERIV-MINUTE-BOUNDS-001).

    Parameters
    ----------
    catalog
        An OPEN :class:`~brutex.data.catalog.Catalog` to reconcile + read.
    lake_root
        Top-level lake directory (the parent of ``bars/``), for the reconcile.
    contracts
        The resolved derivative contracts to bound.
    full_start, full_end
        The half-open range the unbounded pull would use.
    interval
        The coverage interval (default ``"1day"``).
    reconcile
        When ``True`` (default), reconcile the catalog from the lake ONCE before
        reading. Pass ``False`` when the caller has already reconciled (e.g. it
        derives windows for several underlyings against one reconciled catalog) to
        avoid repeating the O(P) pass.

    Returns
    -------
    dict[str, tuple[date, date]]
        ``{contract.symbol: (start, end)}`` for every contract with a usable bounded
        window. Empty when the catalog is unavailable (a degrade logged LOUDLY) or no
        contract has coverage — both cases leave the spine on the full-range fallback.
    """
    contract_list = list(contracts)
    if reconcile:
        # Lazy import (mirrors cascade_wire): pulls polars/pyarrow only when run, so
        # importing this module never requires the P2 [data] extras.
        from brutex.data.catalog_reconcile import reconcile_catalog_from_lake

        try:
            # check_invariants=False: mid-pull HOT non-healing site — the L2 post-pull
            # heal owns this path's findings; skip the CPU-only per-file n_unique pass.
            reconcile_catalog_from_lake(
                lake_root=lake_root,
                catalog=catalog,
                segment=OPTION_SEGMENT,
                interval=interval,
                check_invariants=False,
            )
        except CatalogError as exc:
            # The catalog file is busy / the data-lake deps are missing — degrade to
            # the full range for EVERY contract (loud, never silent — Golden Rule #10).
            _LOG.warning(
                "contract_bounds.reconcile_unavailable",
                error=str(exc),
                interval=interval,
                contracts=len(contract_list),
            )
            return {}

    windows: dict[str, tuple[date, date]] = {}
    bounded = 0
    no_coverage = 0
    skipped = 0
    disjoint = 0
    for contract in contract_list:
        try:
            coverage = contract_daily_coverage(catalog, contract=contract, interval=interval)
        except (CatalogError, ValueError) as exc:
            # A malformed contract symbol (no exchange token) or a mid-loop catalog
            # error quarantines THIS contract only (full-range fallback) — mirrors the
            # ingest spine's per-contract quarantine; one bad contract never blanks the map.
            _LOG.warning(
                "contract_bounds.contract_skipped",
                symbol=contract.symbol,
                error=str(exc),
            )
            skipped += 1
            continue
        if coverage is None:
            no_coverage += 1
            continue
        window = coverage_to_minute_window(
            coverage, full_start=full_start, full_end=full_end, expiry=contract.expiry
        )
        if window == (full_start, full_end):
            # Coverage present but the window collapsed to the full range (the disjoint
            # edge) — surface it so a defeated optimization is visible, then omit.
            _LOG.warning(
                "contract_bounds.disjoint_fallback",
                symbol=contract.symbol,
                first_date=coverage.first_date.isoformat(),
                last_date=coverage.last_date.isoformat(),
            )
            disjoint += 1
            continue
        windows[contract.symbol] = window
        bounded += 1

    _LOG.info(
        "contract_bounds.derived",
        interval=interval,
        contracts=len(contract_list),
        bounded=bounded,
        no_coverage=no_coverage,
        disjoint=disjoint,
        skipped=skipped,
    )
    return windows
