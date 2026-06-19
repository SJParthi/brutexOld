"""Data Completeness Scoreboard — per-symbol + aggregate, across all four dimensions.

``DEC-COMPLETENESS-SCOREBOARD-001``. One honest answer to "of all our symbols, how many
have the COMPLETE 6-year (2020→today) dataset?" — split **indices vs equities-cash,
SEPARATELY**, across four dimensions:

1. **1-day spot** — EXACT %: the window ledger knows every expected trading-day window, so
   completeness is ``resolved ÷ total`` (reuses the chip classification already proven by
   :func:`~brutex.dashboard.coverage_view.build_spot_coverage_payload`).
2. **1-minute spot** — EXACT %: same ledger authority.
3. **Option EXPIRIES** — there is **no knowable expected total** (Groww only returns the set
   that EXISTS when asked), so this is NOT a fake percentage; it is the pulled-count + a
   **stable / growing** signal: ``stable`` = every currently-listed expiry pulled AND the
   last re-pull found 0 new (the honest "complete" for options — made trustworthy by the
   ``get_expiries`` empty-retry of DEC-EMPTY-RETRY-EVERYWHERE-001).
4. **Option CONTRACTS** — same: pulled-count + status; expected total unknowable.

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

#: The two physically-fetched spot dimensions the scoreboard reports (the 19 intraday TFs are
#: RESAMPLED from 1-minute — derived, not separately pulled — so they are not scored).
SPOT_DIMENSIONS: Final[tuple[str, str]] = ("1day", "1minute")

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


def _overall_spot(one_day: str, one_minute: str) -> str:
    """A symbol's overall spot verdict: ``full`` iff BOTH 1-day and 1-minute are full."""
    if one_day == DIM_FULL and one_minute == DIM_FULL:
        return DIM_FULL
    if DIM_FAILED in (one_day, one_minute):
        return DIM_FAILED
    if DIM_PARTIAL in (one_day, one_minute) or DIM_FULL in (one_day, one_minute):
        return DIM_PARTIAL
    return DIM_NONE


def _per_symbol_spot(
    spot_rows: Iterable[Mapping[str, Any]], categories: Mapping[str, str]
) -> list[dict[str, Any]]:
    """Group annotated spot rows by symbol → one row per symbol with 1-day + 1-minute verdicts.

    *spot_rows* are the annotated rows from
    :func:`~brutex.dashboard.coverage_view.build_spot_coverage_payload` (each carries
    ``symbol`` · ``interval`` · ``completeness``). *categories* maps a symbol to its
    :data:`CATEGORIES` bucket; an unmapped symbol is excluded (it is not in the scored
    universe). Symbols are returned sorted (symbol, exchange) for a stable view.
    """
    by_symbol: dict[str, dict[str, Any]] = {}
    for row in spot_rows:
        symbol = str(row["symbol"])
        category = categories.get(symbol)
        if category is None:
            continue  # not a scored universe symbol (e.g. a stray lake partition)
        interval = str(row["interval"])
        if interval not in SPOT_DIMENSIONS:
            continue  # a derived intraday TF — not a separately-pulled dimension
        entry = by_symbol.setdefault(
            symbol,
            {
                "symbol": symbol,
                "exchange": str(row.get("exchange", "")),
                "category": category,
                "1day": DIM_NONE,
                "1minute": DIM_NONE,
            },
        )
        entry[interval] = _spot_dimension_verdict(row.get("completeness"))
    for entry in by_symbol.values():
        entry["overall"] = _overall_spot(entry["1day"], entry["1minute"])
    return sorted(by_symbol.values(), key=lambda e: (e["symbol"], e["exchange"]))


def _option_verdict(statuses: list[str]) -> str:
    """Fold one underlying's per-year coverage-cell statuses into the honest option verdict.

    Any ``failed`` year → ``failed`` (a re-pull should retry); else any ``pending`` year →
    ``growing`` (the chain is still filling — a new expiry could still arrive); else any
    ``expiries_only`` year → ``partial`` (expiries captured, contracts not yet); else (every
    year ``complete``) → ``stable`` — the only honest "complete" for options (all currently
    -listed expiries + contracts pulled, nothing new outstanding). No cells → ``none``.
    """
    if not statuses:
        return OPT_NONE
    if _CELL_FAILED in statuses:
        return OPT_FAILED
    if _CELL_PENDING in statuses:
        return OPT_GROWING
    if _CELL_EXPIRIES_ONLY in statuses:
        return OPT_PARTIAL
    return OPT_STABLE  # every attempted year is COMPLETE


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
                "_statuses": [],
            },
        )
        cell["expiries_pulled"] += int(rec.expiries_pulled)
        cell["contracts_pulled"] += int(rec.contracts_pulled)
        cell["years"].append(int(rec.year))
        cell["_statuses"].append(str(rec.status))
    out: list[dict[str, Any]] = []
    for cell in grouped.values():
        statuses = cell.pop("_statuses")
        cell["years"] = sorted(cell["years"])
        cell["status"] = _option_verdict(statuses)
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
        "spot": {"1day": dict(none_dim), "1minute": dict(none_dim), "overall": DIM_NONE},
        "options": {
            "status": OPT_NONE,
            "expiries_pulled": 0,
            "contracts_pulled": 0,
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
    symbol it carries, per spot dimension (``1day`` / ``1minute``), the verdict + the REAL
    earliest/latest bar date + the ``from_2020`` flag; and per option underlying the honest
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
        entry["spot"]["overall"] = _overall_spot(
            entry["spot"]["1day"]["status"], entry["spot"]["1minute"]["status"]
        )
    _fold_per_symbol_options(out, option_records, option_categories)
    return out


def _fold_per_symbol_options(
    out: dict[str, dict[str, Any]],
    option_records: Iterable[Any],
    option_categories: Mapping[str, str],
) -> None:
    """Fold option coverage records into the per-symbol map (status + counts + year span)."""
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
                "statuses": [],
            },
        )
        acc["expiries"] += int(rec.expiries_pulled)
        acc["contracts"] += int(rec.contracts_pulled)
        acc["years"].append(int(rec.year))
        acc["statuses"].append(str(rec.status))
    for underlying, acc in grouped.items():
        # ``years`` is always non-empty here: a grouped entry exists ONLY because a record was
        # folded into it, and each fold appends its year — so years[0]/years[-1] are safe.
        years = sorted(acc["years"])
        entry = out.setdefault(
            underlying, _new_per_symbol_entry(underlying, acc["exchange"], acc["category"])
        )
        entry["options"] = {
            "status": _option_verdict(acc["statuses"]),
            "expiries_pulled": acc["expiries"],
            "contracts_pulled": acc["contracts"],
            "years": years,
            "first_year": years[0],
            "last_year": years[-1],
        }


def _aggregate_spot(per_symbol: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Count, per category, how many symbols are FULL on each spot dimension + overall."""
    agg: dict[str, dict[str, int]] = {
        cat: {"total": 0, "full_1day": 0, "full_1minute": 0, "full_both": 0} for cat in CATEGORIES
    }
    for entry in per_symbol:
        bucket = agg[entry["category"]]
        bucket["total"] += 1
        if entry["1day"] == DIM_FULL:
            bucket["full_1day"] += 1
        if entry["1minute"] == DIM_FULL:
            bucket["full_1minute"] += 1
        if entry["overall"] == DIM_FULL:
            bucket["full_both"] += 1
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
    :class:`~brutex.models.Instrument`. A brute-force index → :data:`CATEGORY_INDEX`; a
    reference-only row (India VIX / NTM constituent / spot-only index) → :data:`CATEGORY_REFERENCE`;
    otherwise (an F&O-eligible or cash equity) → :data:`CATEGORY_EQUITY`. O(instruments).
    """
    out: dict[str, str] = {}
    for inst in instruments:
        if is_index(inst):
            out[inst.symbol_name] = CATEGORY_INDEX
        elif is_reference(inst):
            out[inst.symbol_name] = CATEGORY_REFERENCE
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


def scoreboard_to_csv(board: Mapping[str, Any]) -> str:
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

    1. ``SUMMARY — spot`` — per category: ``total``, and how many symbols are FULL on 1-day /
       1-minute / both (EXACT, window-ledger backed).
    2. ``SUMMARY — options`` — per category: ``total``, ``stable`` (the honest "complete"),
       ``growing_or_partial``, ``failed`` (NO fabricated %, per CLAUDE.md §6).
    3. ``DETAIL — spot`` — one row per symbol with its 1-day / 1-minute / overall verdict.
    4. ``DETAIL — options`` — one row per underlying with status + pulled counts + the years.

    Uses :mod:`csv` so any symbol with a comma / quote is escaped correctly (an extreme-input
    guard); ``\\n`` line terminator for deterministic, platform-stable output.
    """
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")

    writer.writerow(["BruteX — Data Completeness Scoreboard"])
    writer.writerow(
        [
            "spot completeness is EXACT (window-ledger); option status is "
            "stable/growing/partial/failed — no fabricated % (vendor exposes no expected total)"
        ]
    )

    spot_by_cat = board["spot"]["by_category"]
    writer.writerow([])
    writer.writerow(["SUMMARY — spot (count of symbols FULL per dimension — per category)"])
    writer.writerow(["category", "total", "full_1day", "full_1minute", "full_both"])
    for cat in CATEGORIES:
        bucket = spot_by_cat[cat]
        writer.writerow(
            [
                cat,
                bucket["total"],
                bucket["full_1day"],
                bucket["full_1minute"],
                bucket["full_both"],
            ]
        )

    opt_by_cat = board["options"]["by_category"]
    writer.writerow([])
    writer.writerow(["SUMMARY — options (count of underlyings per status — per category)"])
    writer.writerow(["category", "total", "stable", "growing_or_partial", "failed"])
    for cat in CATEGORIES:
        bucket = opt_by_cat[cat]
        writer.writerow(
            [cat, bucket["total"], bucket["stable"], bucket["growing_or_partial"], bucket["failed"]]
        )

    writer.writerow([])
    writer.writerow(["DETAIL — spot (per symbol)"])
    writer.writerow(["category", "symbol", "exchange", "1day", "1minute", "overall"])
    for row in board["spot"]["per_symbol"]:
        writer.writerow(
            [
                row["category"],
                row["symbol"],
                row["exchange"],
                row["1day"],
                row["1minute"],
                row["overall"],
            ]
        )

    writer.writerow([])
    writer.writerow(["DETAIL — options (per underlying)"])
    writer.writerow(
        [
            "category",
            "underlying",
            "exchange",
            "status",
            "expiries_pulled",
            "contracts_pulled",
            "years",
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
