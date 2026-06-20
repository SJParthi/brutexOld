"""Data Completeness Scoreboard — per-symbol + aggregate, across all three dimensions.

``DEC-COMPLETENESS-SCOREBOARD-001``. One honest answer to "of all our symbols, how many
have the COMPLETE 6-year (2020→today) dataset?" — split **indices vs equities-cash,
SEPARATELY**, across three dimensions:

1. **1-minute spot** — EXACT %: the window ledger knows every expected trading-day window, so
   completeness is ``resolved ÷ total`` (reuses the chip classification already proven by
   :func:`~brutex.dashboard.coverage_view.build_spot_coverage_payload`). 1-minute is the
   SINGLE granularity fetched from Groww (DEC-1MINUTE-ONLY-DERIVE-1DAY-001); 1-day + the 19
   intraday TFs are DERIVED from it (their fetch-ledger has no windows), so they are NOT
   scored — only the one fetched dimension is.
2. **Option EXPIRIES** — there is **no knowable expected total** (Groww only returns the set
   that EXISTS when asked), so this is NOT a fake percentage; it is the pulled-count + a
   **stable / growing** signal: ``stable`` = every currently-listed expiry pulled AND the
   last re-pull found 0 new (the honest "complete" for options — made trustworthy by the
   ``get_expiries`` empty-retry of DEC-EMPTY-RETRY-EVERYWHERE-001).
3. **Option CONTRACTS** — same: pulled-count + status; expected total unknowable.

This module is a **pure re-aggregation** over inputs the route already computes (the
annotated spot rows from :func:`build_spot_coverage_payload`, a ``symbol → category`` map,
and the option :class:`~brutex.data.coverage_ledger.CoverageRecord` rows), so the whole
surface is 100% unit-testable offline — no lake, no store, no network (CLAUDE.md §11 COMMON:
it REUSES the ledger classification rather than re-deriving it). O(rows): one pass to group
by symbol, one pass to fold options; O(1) per symbol lookup via dicts.

**Honest envelope (Golden Rule #1 / #10):** spot completeness is exact (ledger-backed);
option "stable" is the only honest "complete" given the vendor exposes no total — surfaced
as ``stable``/``growing``/``partial``/``failed``, never a fabricated %. A delisted / no-longer
-listed symbol is judged on the windows it HAS (the ledger never planned windows past its
real lifetime), so it reads complete-up-to-lifetime, not penalised.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Callable, Iterable, Mapping
from typing import Any, Final

from brutex.dashboard.coverage_view import (
    COMPLETENESS_FAILED,
    COMPLETENESS_PENDING,
    COMPLETENESS_SETTLED,
)

#: The ONE physically-fetched spot dimension the scoreboard reports. 1-minute is the SINGLE
#: granularity fetched from Groww (DEC-1MINUTE-ONLY-DERIVE-1DAY-001); ``1day`` (the PDH/PDL
#: anchor) AND the 19 intraday TFs are DERIVED from the stored 1-minute, never separately
#: pulled — so they are NOT scored. 1-day was dropped from the scoreboard view because its
#: fetch-ledger has no 1-day windows (it is derived), which made the column always read
#: "none" even though the derived 1-day exists in the lake.
SPOT_DIMENSIONS: Final[tuple[str]] = ("1minute",)

#: Instrument categories — counted SEPARATELY per the operator requirement.
CATEGORY_INDEX: Final[str] = "index"
CATEGORY_EQUITY: Final[str] = "equity"
CATEGORY_REFERENCE: Final[str] = "reference"
CATEGORIES: Final[tuple[str, str, str]] = (CATEGORY_INDEX, CATEGORY_EQUITY, CATEGORY_REFERENCE)

#: Per-dimension symbol verdicts (a symbol's standing on ONE dimension).
DIM_FULL: Final[str] = "full"  # ledger-settled: every expected window resolved (no pending/failed)
DIM_PARTIAL: Final[str] = "partial"  # has unfetched PLANNED windows still to pull
DIM_FAILED: Final[str] = "failed"  # has FAILED windows (transient — a re-pull should retry)
DIM_NONE: Final[str] = "none"  # no ledger record yet (never pulled)

#: Per-underlying OPTION verdicts (the honest "stable=complete", no fake %).
OPT_STABLE: Final[str] = "stable"  # all attempted years COMPLETE — the honest "complete"
OPT_PARTIAL: Final[str] = "partial"  # expiries captured, contracts not yet (expiries_only)
OPT_GROWING: Final[str] = "growing"  # a year still PENDING — chain still filling in
OPT_FAILED: Final[str] = "failed"  # a year FAILED — needs a re-pull
OPT_NONE: Final[str] = "none"  # no coverage record (no options / never attempted)

#: Honest annotation for the misleading ``contracts_pulled = 0`` on an
#: ``expiries_only`` underlying (DEC-METADATA-LIVE-PROGRESS-001). The lean universe
#: PARSES then DISCARDS the ~89k option legs (DEC-INSTRUMENT-UNIVERSE-FAST-001) — the
#: metadata pull persists only the chain SHAPE (expiry dates + contract symbols), so
#: ``0`` here is BY DESIGN, not a broken/failed pull. Surfaced as a row note so the
#: operator reads "metadata-only" instead of "0 = broken". Empty for every other case
#: (a row with persisted contracts, or a non-``expiries_only`` verdict) so no false
#: claim is ever attached.
OPT_CONTRACTS_BY_DESIGN_NOTE: Final[str] = (
    "metadata-only by design — option legs are discarded "
    "(DEC-INSTRUMENT-UNIVERSE-FAST-001), only the chain shape is kept"
)

#: The coverage-ledger cell statuses this module BRANCHES on (mirror of the
#: ``coverage_ledger.STATUS_*`` strings, duplicated here to keep this pure module
#: import-light — a drift test pins them equal). ``complete`` is intentionally NOT
#: duplicated: it is the fall-through of :func:`_option_verdict` (every year complete →
#: ``stable``), so naming a constant for it would be dead.
_CELL_EXPIRIES_ONLY: Final[str] = "expiries_only"
_CELL_PENDING: Final[str] = "pending"
_CELL_FAILED: Final[str] = "failed"

#: Exclusive upper bound for the "data begins in 2020" test (DEC-COMPLETENESS-UNIVERSE-001).
#: A dimension's earliest bar date ``< 2021-01-01`` ⇒ its first bar falls in calendar 2020
#: (the ``GROWW_HISTORY_FLOOR`` year per CLAUDE.md §3 / DEC-PULL-FLOOR-2020-001), so the symbol
#: HAS data starting 2020 on that dimension. Year-granular on purpose: robust to which exact
#: first-trading-day of Jan-2020 the vendor serves (holidays vary), never a magic threshold.
_FROM_2020_EXCLUSIVE_UPPER: Final[str] = "2021-01-01"


def _spot_dimension_verdict(completeness: str | None) -> str:
    """Map a spot row's ``completeness`` chip to the scoreboard's per-dimension verdict.

    ``settled`` (every window resolved) → ``full``; ``pending`` → ``partial``; ``failed`` →
    ``failed``; a missing / no-record / derived / ambiguous row → ``none`` (no positive claim).
    """
    if completeness == COMPLETENESS_SETTLED:
        return DIM_FULL
    if completeness == COMPLETENESS_PENDING:
        return DIM_PARTIAL
    if completeness == COMPLETENESS_FAILED:
        return DIM_FAILED
    # no_ledger_record / derived / ambiguous / absent → no positive completeness claim.
    return DIM_NONE


def _overall_spot(one_minute: str) -> str:
    """A symbol's overall spot verdict — the 1-MINUTE verdict alone.

    1-minute is the SINGLE granularity fetched (DEC-1MINUTE-ONLY-DERIVE-1DAY-001); 1-day +
    the intraday TFs are DERIVED from it, so the overall spot standing IS the 1-minute
    standing (``full`` / ``partial`` / ``failed`` / ``none``) — there is no second dimension
    to AND against.
    """
    return one_minute


def _per_symbol_spot(
    spot_rows: Iterable[Mapping[str, Any]], categories: Mapping[str, str]
) -> list[dict[str, Any]]:
    """Group annotated spot rows by symbol → one row per symbol with the 1-minute verdict.

    *spot_rows* are the annotated rows from
    :func:`~brutex.dashboard.coverage_view.build_spot_coverage_payload` (each carries
    ``symbol`` · ``interval`` · ``completeness``). *categories* maps a symbol to its
    :data:`CATEGORIES` bucket; an unmapped symbol is excluded (it is not in the scored
    universe). 1-day is DERIVED from 1-minute (DEC-1MINUTE-ONLY-DERIVE-1DAY-001) so it is
    not scored — only the single fetched 1-minute dimension is. Symbols are returned sorted
    (symbol, exchange) for a stable view.
    """
    by_symbol: dict[str, dict[str, Any]] = {}
    for row in spot_rows:
        symbol = str(row["symbol"])
        category = categories.get(symbol)
        if category is None:
            continue  # not a scored universe symbol (e.g. a stray lake partition)
        interval = str(row["interval"])
        if interval not in SPOT_DIMENSIONS:
            continue  # a derived TF (incl. 1-day) — not the separately-pulled dimension
        entry = by_symbol.setdefault(
            symbol,
            {
                "symbol": symbol,
                "exchange": str(row.get("exchange", "")),
                "category": category,
                "1minute": DIM_NONE,
                # DEC-COMPLETENESS-HONEST-EMPTY-001: the actually-covered date range for the
                # 1-minute dimension (the lake's earliest/latest bar) so a human sees in ONE
                # view what span it holds — None until a 1-minute row is seen.
                "1minute_start": None,
                "1minute_end": None,
            },
        )
        entry[interval] = _spot_dimension_verdict(row.get("completeness"))
        first = row.get("first_date")
        last = row.get("last_date")
        entry[f"{interval}_start"] = str(first) if first else None
        entry[f"{interval}_end"] = str(last) if last else None
    for entry in by_symbol.values():
        entry["overall"] = _overall_spot(entry["1minute"])
    return sorted(by_symbol.values(), key=lambda e: (e["symbol"], e["exchange"]))


def _option_verdict(cells: list[tuple[str, int]]) -> str:
    """Fold one underlying's per-year ``(status, expiries_pulled)`` cells into the honest verdict.

    DEC-OPTIONS-COMPLETENESS-REAL-YEARS-001 — a requested year that returned ZERO expiries (a
    pre-listing year of a late-listed underlying — e.g. JIOFIN before 2023, ETERNAL before 2024 —
    or a gap) is NOT a "still-filling chain": it must NOT force ``growing`` (the prior bug left
    such an underlying perpetually ``growing`` because its empty pre-listing years stayed
    ``pending``). The verdict, in order:

    * any ``failed`` year → ``failed`` (a re-pull should retry);
    * else NO year has ANY expiries → ``none`` (the underlying has no option history in the grid);
    * else any year that HAS expiries but is still ``pending`` → ``growing`` (DEFENSIVE /
      forward-compat — see the honesty note below);
    * else any requested year with ZERO expiries → ``partial`` (the option history does NOT span
      the full requested range — a late-listed start or a gap; HONEST, never a fabricated %);
    * else any ``expiries_only`` year → ``partial`` (expiries captured, contracts not yet);
    * else (every requested year ``complete`` AND non-empty) → ``stable`` — the honest "complete".

    Only years with ``expiries_pulled > 0`` count as real option-history; empty years are weighed
    here for the ``partial`` signal but are EXCLUDED from the displayed ``years`` span (the caller
    appends a year only when it has expiries), so a late-listed underlying shows only its real
    expiry years, never the full requested 2020→now range.

    Honesty note (Golden Rule #10, hostile-review #859): the ``growing`` arm is currently
    UNREACHABLE in production — the ledger's :func:`brutex.data.coverage_ledger.classify_cell`
    invariant is ``pending ⟺ expiries_pulled == 0`` (a year with discovered expiries is
    ``expiries_only`` or ``complete``, never ``pending``), so a ``(pending, n>0)`` cell cannot
    be emitted by the real pull. Under today's pipeline a still-being-filled current year reads
    ``expiries_only`` → ``partial`` until it completes; ``growing`` therefore never fires. The
    arm (and its synthetic ``(pending, n>0)`` unit fixtures) is retained as a DEFENSIVE /
    forward-compat fold in case the ledger ever gains a true "chain still filling" status —
    mirroring the existing defensive ``OPT_NONE`` aggregate case. It is NOT claimed as a live
    verdict. (Consequently the ``growing_or_partial`` aggregate bucket is, in real data,
    effectively partial-only.)
    """
    if not cells:
        return OPT_NONE
    with_expiries = [(status, n) for (status, n) in cells if n > 0]
    if any(status == _CELL_FAILED for status, _n in cells):
        return OPT_FAILED
    if not with_expiries:
        return OPT_NONE
    if any(status == _CELL_PENDING for status, _n in with_expiries):
        # DEFENSIVE / forward-compat (UNREACHABLE in production): the ledger invariant is
        # ``pending ⟺ expiries_pulled == 0`` (classify_cell), so no real cell is (pending, n>0).
        # Retained in case a true "chain still filling" status is ever added (see the docstring
        # honesty note + hostile-review #859); exercised only by synthetic unit fixtures.
        return OPT_GROWING
    if any(n == 0 for _status, n in cells):
        return OPT_PARTIAL
    if any(status == _CELL_EXPIRIES_ONLY for status, _n in with_expiries):
        return OPT_PARTIAL
    return OPT_STABLE  # every attempted year is COMPLETE


def _contracts_note(status: str, contracts_pulled: int) -> str:
    """Honest note for a ``contracts_pulled = 0`` row — empty unless it is by-design.

    Returns :data:`OPT_CONTRACTS_BY_DESIGN_NOTE` ONLY when the verdict is
    :data:`OPT_PARTIAL` (every captured year is ``expiries_only``) AND no contract was
    persisted — the lean-universe metadata-only state where ``0`` is intended, not
    broken (DEC-METADATA-LIVE-PROGRESS-001 / DEC-INSTRUMENT-UNIVERSE-FAST-001). Any
    other verdict, or any row that DID persist contracts, gets ``""`` so the page never
    attaches a false "by design" claim. O(1).
    """
    if status == OPT_PARTIAL and contracts_pulled == 0:
        return OPT_CONTRACTS_BY_DESIGN_NOTE
    return ""


def _per_underlying_options(
    records: Iterable[Any], categories: Mapping[str, str]
) -> list[dict[str, Any]]:
    """Roll option :class:`CoverageRecord` rows up per underlying → one row with the verdict.

    Sums ``expiries_pulled`` / ``contracts_pulled`` across all attempted years and folds the
    per-year statuses into one :func:`_option_verdict`. *categories* maps the underlying to
    its bucket (unmapped underlyings are skipped). Returned sorted by ``(underlying, exchange)``.
    """
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for rec in records:
        underlying = str(rec.underlying_symbol)
        category = categories.get(underlying)
        if category is None:
            continue
        exch = str(rec.exch_id)
        cell = grouped.setdefault(
            (underlying, exch),
            {
                "underlying": underlying,
                "exchange": exch,
                "category": category,
                "expiries_pulled": 0,
                "contracts_pulled": 0,
                "years": [],
                "_cells": [],
            },
        )
        expiries = int(rec.expiries_pulled)
        cell["expiries_pulled"] += expiries
        cell["contracts_pulled"] += int(rec.contracts_pulled)
        cell["_cells"].append((str(rec.status), expiries))
        # Only a year with REAL expiries enters the displayed span — a 0-expiry pre-listing
        # year (e.g. JIOFIN before 2023) is NEVER shown as covered (DEC-OPTIONS-COMPLETENESS
        # -REAL-YEARS-001); it is still weighed by _option_verdict for the partial signal.
        if expiries > 0:
            cell["years"].append(int(rec.year))
    out: list[dict[str, Any]] = []
    for cell in grouped.values():
        cells = cell.pop("_cells")
        cell["years"] = sorted(cell["years"])
        cell["status"] = _option_verdict(cells)
        # Honest annotation: a 0-contract expiries_only row is metadata-only BY DESIGN
        # (legs discarded), not broken (DEC-METADATA-LIVE-PROGRESS-001).
        cell["contracts_note"] = _contracts_note(cell["status"], cell["contracts_pulled"])
        out.append(cell)
    return sorted(out, key=lambda e: (e["underlying"], e["exchange"]))


def _from_2020(first_date: str | None) -> bool:
    """True iff *first_date* (an ISO ``YYYY-MM-DD`` or ``None``) falls in calendar 2020.

    The honest "has data starting 2020" flag for the per-symbol view — a symbol whose earliest
    bar is in 2020 has data from the GROWW_HISTORY_FLOOR year; one whose earliest bar is later
    (a late listing OR a gap) is flagged so the operator SEES it (the explicit dates disambiguate).
    """
    return first_date is not None and first_date < _FROM_2020_EXCLUSIVE_UPPER


def _spot_dimension_detail(row: Mapping[str, Any]) -> dict[str, Any]:
    """One spot dimension's verdict + EXPLICIT first/last bar dates + the ``from_2020`` flag."""
    first = row.get("first_date")
    last = row.get("last_date")
    first_s = str(first) if first else None
    return {
        "status": _spot_dimension_verdict(row.get("completeness")),
        "first_date": first_s,
        "last_date": str(last) if last else None,
        "from_2020": _from_2020(first_s),
    }


def _new_per_symbol_entry(symbol: str, exchange: str, category: str) -> dict[str, Any]:
    """A per-symbol completeness entry pre-seeded to the honest "no data" defaults."""
    none_dim: dict[str, Any] = {
        "status": DIM_NONE,
        "first_date": None,
        "last_date": None,
        "from_2020": False,
    }
    return {
        "symbol": symbol,
        "exchange": exchange,
        "category": category,
        "spot": {"1minute": dict(none_dim), "overall": DIM_NONE},
        "options": {
            "status": OPT_NONE,
            "expiries_pulled": 0,
            "contracts_pulled": 0,
            # OPT_NONE never carries the by-design note (no chain attempted) — empty.
            "contracts_note": "",
            "years": [],
            "first_year": None,
            "last_year": None,
        },
    }


def per_symbol_completeness(
    spot_rows: Iterable[Mapping[str, Any]],
    spot_categories: Mapping[str, str],
    option_records: Iterable[Any],
    option_categories: Mapping[str, str],
) -> dict[str, dict[str, Any]]:
    """Per-symbol completeness map (``symbol_name → detail``) WITH explicit first/last dates.

    DEC-COMPLETENESS-UNIVERSE-001 — the data behind the ``/universe`` per-row completeness strip
    that answers, at pull time, *does THIS symbol have the entire 2020→today set?* For each
    symbol it carries, for the single fetched spot dimension (``1minute``), the verdict + the
    REAL earliest/latest bar date + the ``from_2020`` flag; and per option underlying the honest
    ``stable``/``growing``/``partial``/``failed`` status + pulled counts + the year span. Keyed
    by ``symbol_name`` — the SAME key the ``/universe`` rows carry (``i.symbol``), the spot
    datasets carry (``symbol``), and the option records carry (``underlying_symbol``) — so the
    page joins it O(1) per row.

    Pure re-aggregation over the route-supplied inputs (no lake/store/network), so 100%
    unit-testable offline; reuses the shared verdict helpers (no re-derivation, CLAUDE.md §11
    COMMON). A symbol with only spot OR only option data gets the absent side's honest defaults;
    an unmapped symbol / a derived intraday TF is skipped. O(spot_rows + option_records).
    """
    out: dict[str, dict[str, Any]] = {}
    for row in spot_rows:
        symbol = str(row["symbol"])
        category = spot_categories.get(symbol)
        if category is None:
            continue  # not a scored universe symbol
        interval = str(row["interval"])
        if interval not in SPOT_DIMENSIONS:
            continue  # a derived intraday TF — not a separately-pulled dimension
        entry = out.setdefault(
            symbol, _new_per_symbol_entry(symbol, str(row.get("exchange", "")), category)
        )
        entry["spot"][interval] = _spot_dimension_detail(row)
    for entry in out.values():
        entry["spot"]["overall"] = _overall_spot(entry["spot"]["1minute"]["status"])
    _fold_per_symbol_options(out, option_records, option_categories)
    return out


def _fold_per_symbol_options(
    out: dict[str, dict[str, Any]],
    option_records: Iterable[Any],
    option_categories: Mapping[str, str],
) -> None:
    """Fold option coverage records into the per-symbol map (status + counts + year span)."""
    # Grouped by ``underlying`` ALONE (intentionally NOT ``(underlying, exch)`` like the
    # scoreboard's :func:`_per_underlying_options`): the ``/universe`` strip joins one row per
    # ``symbol_name`` (``COV[i.symbol]``), so a per-symbol rollup is the right granularity here.
    # If an underlying ever lists options on two exchanges, the counts sum across both and the
    # first record's ``exch_id`` is kept (exchange is display-only on the strip) — harmless.
    grouped: dict[str, dict[str, Any]] = {}
    for rec in option_records:
        underlying = str(rec.underlying_symbol)
        category = option_categories.get(underlying)
        if category is None:
            continue
        acc = grouped.setdefault(
            underlying,
            {
                "exchange": str(rec.exch_id),
                "category": category,
                "expiries": 0,
                "contracts": 0,
                "years": [],
                "cells": [],
            },
        )
        expiries = int(rec.expiries_pulled)
        acc["expiries"] += expiries
        acc["contracts"] += int(rec.contracts_pulled)
        acc["cells"].append((str(rec.status), expiries))
        # Only REAL-expiry years enter the displayed span (DEC-OPTIONS-COMPLETENESS-REAL-YEARS
        # -001) — a 0-expiry pre-listing year is excluded, so a late-listed underlying shows
        # only its real years, never the full requested range.
        if expiries > 0:
            acc["years"].append(int(rec.year))
    for underlying, acc in grouped.items():
        # ``years`` may be EMPTY when every requested year returned zero expiries (an underlying
        # with no option history in the grid) — first/last_year then stay None (the verdict is
        # OPT_NONE), mirroring the absent-options default.
        years = sorted(acc["years"])
        entry = out.setdefault(
            underlying, _new_per_symbol_entry(underlying, acc["exchange"], acc["category"])
        )
        status = _option_verdict(acc["cells"])
        entry["options"] = {
            "status": status,
            "expiries_pulled": acc["expiries"],
            "contracts_pulled": acc["contracts"],
            # Honest annotation: a 0-contract expiries_only underlying is metadata-only
            # BY DESIGN (legs discarded), not broken (DEC-METADATA-LIVE-PROGRESS-001).
            "contracts_note": _contracts_note(status, acc["contracts"]),
            "years": years,
            "first_year": years[0] if years else None,
            "last_year": years[-1] if years else None,
        }


def _aggregate_spot(per_symbol: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Count, per category, how many symbols are FULL on the 1-minute spot dimension.

    1-minute is the single fetched granularity (DEC-1MINUTE-ONLY-DERIVE-1DAY-001) so there is
    one ``full_1minute`` count per category; the prior ``full_1day`` / ``full_both`` buckets
    were dropped with the derived 1-day column.
    """
    agg: dict[str, dict[str, int]] = {cat: {"total": 0, "full_1minute": 0} for cat in CATEGORIES}
    for entry in per_symbol:
        bucket = agg[entry["category"]]
        bucket["total"] += 1
        if entry["1minute"] == DIM_FULL:
            bucket["full_1minute"] += 1
    return agg


def _aggregate_options(per_underlying: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Count, per category, how many underlyings have a STABLE (complete) option chain."""
    agg: dict[str, dict[str, int]] = {
        cat: {"total": 0, "stable": 0, "growing_or_partial": 0, "failed": 0} for cat in CATEGORIES
    }
    for entry in per_underlying:
        bucket = agg[entry["category"]]
        bucket["total"] += 1
        status = entry["status"]
        if status == OPT_STABLE:
            bucket["stable"] += 1
        elif status == OPT_FAILED:
            bucket["failed"] += 1
        elif status in (OPT_GROWING, OPT_PARTIAL):
            bucket["growing_or_partial"] += 1
    return agg


def categorize_instruments(
    instruments: Iterable[Any],
    *,
    is_index: Callable[[Any], bool],
    is_reference: Callable[[Any], bool],
) -> dict[str, str]:
    """Map each spot instrument's primary symbol → its scoreboard category.

    Keyed by ``symbol_name`` — the engine's primary symbol, which IS the spot window ledger's
    ``security_id`` (the key the annotated spot rows AND the option coverage records carry),
    so one map serves both. The classification predicates are INJECTED (the route passes
    :func:`~brutex.universe.is_in_brute_force_scope` + :func:`~brutex.universe.is_reference_only`)
    so this stays pure + 100% unit-testable without constructing a full
    :class:`~brutex.models.Instrument`. Classification is checked in PRECEDENCE order
    (DEC-COMPLETENESS-INDEX-CATEGORY-001): a **reference-only** row (India VIX / NTM
    constituent / spot-only index) → :data:`CATEGORY_REFERENCE` FIRST; else an **index**
    (the injected *is_index* — now the by-TYPE :func:`~brutex.universe.is_index_instrument`,
    so FINNIFTY and every non-brute-forced index is correctly an index, not a cash equity)
    → :data:`CATEGORY_INDEX`; otherwise (an F&O-eligible or cash equity) →
    :data:`CATEGORY_EQUITY`. O(instruments).

    Reference is checked BEFORE index because a reference-only index (India VIX) satisfies
    BOTH predicates by type — reference must win so VIX stays REFERENCE, never INDEX. For
    the legacy brute-force *is_index* the two sets are disjoint (the engine-isolation
    invariant), so this precedence is a behaviour-preserving no-op there.
    """
    out: dict[str, str] = {}
    for inst in instruments:
        if is_reference(inst):
            out[inst.symbol_name] = CATEGORY_REFERENCE
        elif is_index(inst):
            out[inst.symbol_name] = CATEGORY_INDEX
        else:
            out[inst.symbol_name] = CATEGORY_EQUITY
    return out


def build_completeness_scoreboard(
    spot_rows: Iterable[Mapping[str, Any]],
    spot_categories: Mapping[str, str],
    option_records: Iterable[Any],
    option_categories: Mapping[str, str],
) -> dict[str, Any]:
    """Compose the full Data Completeness Scoreboard payload (aggregate + per-symbol).

    DEC-COMPLETENESS-SCOREBOARD-001. Pure projection — every input is supplied by the route
    (annotated spot rows, the symbol→category maps, the option coverage records), so this is
    100% unit-testable offline.

    Parameters
    ----------
    spot_rows
        Annotated rows from
        :func:`~brutex.dashboard.coverage_view.build_spot_coverage_payload` (``symbol`` ·
        ``interval`` · ``completeness`` · ``exchange``).
    spot_categories
        ``symbol → {index|equity|reference}`` for every scored spot symbol.
    option_records
        :class:`~brutex.data.coverage_ledger.CoverageRecord` rows (per underlying + year).
    option_categories
        ``underlying → {index|equity|reference}`` for every scored option underlying.

    Returns
    -------
    dict[str, Any]
        ``{"spot": {"by_category", "per_symbol"}, "options": {"by_category", "per_underlying"}}``
        where ``by_category`` is the aggregate count table and the lists are the drill-down rows.
    """
    per_symbol = _per_symbol_spot(spot_rows, spot_categories)
    per_underlying = _per_underlying_options(option_records, option_categories)
    return {
        "spot": {"by_category": _aggregate_spot(per_symbol), "per_symbol": per_symbol},
        "options": {
            "by_category": _aggregate_options(per_underlying),
            "per_underlying": per_underlying,
        },
    }


def _csv_date(value: object) -> str:
    """An ISO date string, or ``''`` for a missing/None bar date (a clean empty Excel cell)."""
    return str(value) if value else ""


def _csv_yes_no(flag: object) -> str:
    """Human ``yes``/``no`` for a ``from_2020`` boolean (``no`` for a falsy/absent dimension)."""
    return "yes" if flag else "no"


#: Leading characters a spreadsheet app (Excel / Sheets / LibreOffice) treats as the start
#: of a FORMULA when it opens a CSV cell — the CSV/formula-injection vector (OWASP "CSV
#: injection"). ``= + - @`` plus a leading TAB / CR. A cell whose text starts with one of
#: these is prefixed with a single quote so the app renders it as literal text, never
#: evaluates it (DEC-CSV-FORMULA-INJECTION-001).
_CSV_INJECTION_LEADERS: Final[frozenset[str]] = frozenset("=+-@\t\r")
#: The formula lead-in characters that stay dangerous even behind LEADING WHITESPACE —
#: some spreadsheet apps strip a leading space then evaluate ``" =cmd"`` as a formula. So a
#: cell whose FIRST NON-WHITESPACE char is one of these is also neutralised (the
#: belt-and-suspenders "out-of-box" edge, even though every in-scope cell here is a digit /
#: status word / EXCH-prefixed symbol that never leads with whitespace+formula).
_CSV_FORMULA_FIRST_CHARS: Final[frozenset[str]] = frozenset("=+-@")


def _csv_safe(cell: object) -> object:
    """Neutralise CSV/formula injection on ONE cell.

    A STRING cell is prefixed with a single quote (``'``) — so a spreadsheet app shows the
    literal text instead of evaluating it as a formula — when EITHER its first character is
    a formula lead-in (``= + - @`` or a leading TAB / CR) OR its first NON-whitespace
    character is one of ``= + - @`` (the leading-whitespace bypass). Non-string cells (ints,
    ``None``) and empty / genuinely-plain strings pass through UNCHANGED, so numeric columns
    and ISO dates stay numeric/plain. O(1) — a first-char test plus one ``lstrip`` probe.
    """
    if (
        isinstance(cell, str)
        and cell
        and (cell[0] in _CSV_INJECTION_LEADERS or cell.lstrip()[:1] in _CSV_FORMULA_FIRST_CHARS)
    ):
        return "'" + cell
    return cell


class _FormulaSafeWriter:
    """A :func:`csv.writer` wrapper that runs every cell through :func:`_csv_safe`.

    Wrapping the writer ONCE neutralises formula-injection on EVERY ``writerow`` call
    (DEC-CSV-FORMULA-INJECTION-001) without sprinkling the guard across each of the
    ~15 call sites — one source of truth, so a future row can never bypass it
    (CLAUDE.md §11 COMMON). The CSV-quoting of commas/quotes is still the underlying
    :mod:`csv` writer's job; this only adds the formula-lead-in defusal on top.
    """

    __slots__ = ("_writer",)

    def __init__(self, writer: Any) -> None:
        self._writer = writer

    def writerow(self, row: Iterable[object]) -> None:
        """Write *row* with every cell formula-injection-sanitised via :func:`_csv_safe`."""
        self._writer.writerow([_csv_safe(cell) for cell in row])


def scoreboard_to_csv(
    board: Mapping[str, Any],
    per_symbol: Mapping[str, Mapping[str, Any]] | None = None,
) -> str:
    """Render a :func:`build_completeness_scoreboard` payload as a downloadable CSV report.

    DEC-COMPLETENESS-PAGE-001 — the "even a human can understand it in Excel" report
    behind the ``/api/v1/completeness/scoreboard.csv`` download. A single pure
    string-builder over the already-composed scoreboard (no lake / store / network), so it
    is 100% unit-testable offline and adds no new derivation (CLAUDE.md §11 COMMON — it only
    re-serialises the engine's own output).

    The file is a **four-section human report** (each section a self-describing title row +
    its own header, blank-line separated) that answers the operator's question directly —
    *how many symbols from indices vs equities-cash, SEPARATELY, have the full dataset* — and
    then the per-symbol / per-underlying drill-down:

    1. ``SUMMARY — spot`` — per category: ``total``, and how many symbols are FULL on 1-minute
       (EXACT, window-ledger backed; 1-day is DERIVED from 1-minute so it is not scored).
    2. ``SUMMARY — options`` — per category: ``total``, ``stable`` (the honest "complete"),
       ``growing_or_partial``, ``failed`` (NO fabricated %, per CLAUDE.md §6).
    3. ``DETAIL — spot`` — one row per symbol with its 1-minute / overall verdict;
       when *per_symbol* (a :func:`per_symbol_completeness` map) is supplied the row is
       ENRICHED with the explicit first→last bar date + ``from_2020`` flag for the 1-minute
       dimension (DEC-COMPLETENESS-CSV-READABLE-001 — the SAME rich info the ``/universe``
       strip shows, so the CSV is never less informative than the page); ``None`` keeps the
       compact verdict-only columns (backward-compatible).
    4. ``DETAIL — options`` — one row per underlying with status + pulled counts + the years.

    Uses :mod:`csv` so any symbol with a comma / quote is escaped correctly (an extreme-input
    guard); ``\\n`` line terminator for deterministic, platform-stable output. Every cell is
    additionally run through :class:`_FormulaSafeWriter` so a value starting with ``= + - @``
    (or a leading TAB / CR) can never execute as a spreadsheet formula when the report is
    opened in Excel / Sheets (DEC-CSV-FORMULA-INJECTION-001).
    """
    buf = io.StringIO()
    writer = _FormulaSafeWriter(csv.writer(buf, lineterminator="\n"))

    writer.writerow(["BruteX — Data Completeness Scoreboard"])
    writer.writerow(
        [
            "spot completeness is EXACT (window-ledger); option status is "
            "stable/growing/partial/failed — no fabricated % (vendor exposes no expected total)"
        ]
    )

    spot_by_cat = board["spot"]["by_category"]
    writer.writerow([])
    writer.writerow(["SUMMARY — spot: how many symbols have the FULL dataset per asset class"])
    writer.writerow(["Asset class", "Symbols", "Full 1-minute"])
    for cat in CATEGORIES:
        bucket = spot_by_cat[cat]
        writer.writerow([cat, bucket["total"], bucket["full_1minute"]])

    opt_by_cat = board["options"]["by_category"]
    writer.writerow([])
    writer.writerow(["SUMMARY — options: underlyings per chain status per asset class"])
    writer.writerow(
        ["Asset class", "Underlyings", "Stable (complete)", "Growing or partial", "Failed"]
    )
    for cat in CATEGORIES:
        bucket = opt_by_cat[cat]
        writer.writerow(
            [cat, bucket["total"], bucket["stable"], bucket["growing_or_partial"], bucket["failed"]]
        )

    writer.writerow([])
    if per_symbol is None:
        writer.writerow(["DETAIL — spot (per symbol)"])
        writer.writerow(
            [
                "Asset class",
                "Symbol",
                "Exchange",
                "1-minute complete?",
                "Overall",
            ]
        )
        for row in board["spot"]["per_symbol"]:
            writer.writerow(
                [
                    row["category"],
                    row["symbol"],
                    row["exchange"],
                    row["1minute"],
                    row["overall"],
                ]
            )
    else:
        writer.writerow(["DETAIL — spot (per symbol with first→last bar dates)"])
        writer.writerow(
            [
                "Asset class",
                "Symbol",
                "Exchange",
                "1-minute complete?",
                "1-minute first bar",
                "1-minute last bar",
                "1-minute from 2020?",
                "Overall",
            ]
        )
        for row in board["spot"]["per_symbol"]:
            spot = per_symbol.get(row["symbol"], {}).get("spot", {})
            one_min = spot.get("1minute", {})
            writer.writerow(
                [
                    row["category"],
                    row["symbol"],
                    row["exchange"],
                    row["1minute"],
                    _csv_date(one_min.get("first_date")),
                    _csv_date(one_min.get("last_date")),
                    _csv_yes_no(one_min.get("from_2020")),
                    row["overall"],
                ]
            )

    writer.writerow([])
    writer.writerow(["DETAIL — options (per underlying)"])
    writer.writerow(
        [
            "Asset class",
            "Underlying",
            "Exchange",
            "Chain status",
            "Expiries pulled",
            "Contracts pulled",
            "Years",
            "Notes",
        ]
    )
    for row in board["options"]["per_underlying"]:
        writer.writerow(
            [
                row["category"],
                row["underlying"],
                row["exchange"],
                row["status"],
                row["expiries_pulled"],
                row["contracts_pulled"],
                " ".join(str(year) for year in row["years"]),
                # Honest "0 contracts = metadata-only by design" note when applicable
                # (DEC-METADATA-LIVE-PROGRESS-001); empty otherwise.
                row.get("contracts_note", ""),
            ]
        )

    return buf.getvalue()


__all__ = [
    "CATEGORIES",
    "CATEGORY_EQUITY",
    "CATEGORY_INDEX",
    "CATEGORY_REFERENCE",
    "DIM_FAILED",
    "DIM_FULL",
    "DIM_NONE",
    "DIM_PARTIAL",
    "OPT_CONTRACTS_BY_DESIGN_NOTE",
    "OPT_FAILED",
    "OPT_GROWING",
    "OPT_NONE",
    "OPT_PARTIAL",
    "OPT_STABLE",
    "SPOT_DIMENSIONS",
    "build_completeness_scoreboard",
    "categorize_instruments",
    "per_symbol_completeness",
    "scoreboard_to_csv",
]
