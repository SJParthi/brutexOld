"""O(1)-per-step symbol → expiry → contract cascade for the ``/db`` F&O browser.

DEC-DB-FNO-CASCADE-001. The ``/db`` F&O candle picker is otherwise ONE flat
dropdown of every ``[F&O] NIFTY · 2020-02-06 · 12250 PE · 1minute`` partition —
thousands of rows, impossible to navigate to a precise strike (the operator's
"it's really difficult"). This module pre-groups the decoded F&O datasets
(:func:`brutex.dashboard.db_view.list_fno_candle_datasets`, which decodes each
partition symbol via :func:`brutex.data.fno_symbol.parse_fno_symbol`) into a
THREE-LEVEL cascade — **underlying → expiry → contract** — so the operator picks
the underlying, then only its expiries, then only that expiry's strikes/CE-PE.

## Why the lake, not the registry (Golden Rule #1)

The grouping is derived from the F&O candle **lake partitions that truly have
data** — NOT the ``contracts`` registry table (which is frequently empty, as the
operator's screenshot showed: ``contracts: 0``). So the cascade only ever offers a
contract the viewer can actually open; it can never list a phantom strike.

## O(1) discipline (CLAUDE.md §2 Rule 5)

:func:`build_fno_cascade` groups in ONE ``O(datasets)`` pass at build time (sorts
are ``O(n log n)`` once); the per-interaction reads —
:meth:`FnoCascadeIndex.underlyings` / :meth:`~FnoCascadeIndex.expiries` /
:meth:`~FnoCascadeIndex.contracts` — are each an **O(1)** dict lookup returning a
pre-sorted tuple. The dashboard caches the index per dataset-list revision (the
same content-token pattern as :mod:`brutex.dashboard.universe_cache`), so a repeat
cascade click never re-scans the lake.

Pure + deterministic (no I/O). An undecodable partition is surfaced in
:attr:`FnoCascadeIndex.unparsed` rather than hidden — the lake is the single
source of truth and a present-but-unparseable partition is shown honestly, never
dropped (Golden Rule #1).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final

__all__ = [
    "FnoCascadeIndex",
    "FnoContract",
    "build_expiry_counts",
    "build_fno_cascade",
    "contract_to_json",
]

#: The sentinel sort key for a FUTURE (no strike) so it orders BEFORE any real
#: strike in a deterministic, type-stable way (a ``None`` strike cannot compare to
#: a Decimal-string). Futures group at the top of an expiry's contract list.
_NO_STRIKE_SORT: Final[tuple[int, float]] = (0, 0.0)


@dataclass(frozen=True, slots=True)
class FnoContract:
    """One tradable F&O contract under an ``(underlying, expiry)`` — the cascade leaf.

    Aggregates the (≥1) intervals the lake holds for ONE contract ``groww_symbol``,
    so a contract present at both ``1minute`` and a derived ``1day`` is ONE leaf with
    ``intervals=("1day", "1minute")`` rather than two flat rows.

    Attributes
    ----------
    symbol
        The contract ``groww_symbol`` (``NSE-NIFTY-06Feb20-12250-PE``) — the exact
        key :func:`brutex.dashboard.db_view.query_candles` takes.
    exchange
        The partition exchange (``NSE`` / ``BSE``).
    strike
        The option strike as a Decimal-string, or ``None`` for a future.
    option_type
        ``"CE"`` / ``"PE"``, or ``None`` for a future.
    instrument_type
        ``"CE"`` / ``"PE"`` / ``"FUT"`` (the decoded instrument type), or ``None``
        when the symbol did not decode.
    intervals
        The lake intervals available for this contract, ascending (e.g.
        ``("1day", "1minute")``).
    """

    symbol: str
    exchange: str
    strike: str | None
    option_type: str | None
    instrument_type: str | None
    intervals: tuple[str, ...]

    @property
    def label(self) -> str:
        """A compact human label: ``"12250 PE"`` for an option, ``"FUT"`` for a future."""
        if self.strike is not None and self.option_type is not None:
            return f"{self.strike} {self.option_type}"
        return self.instrument_type or self.symbol


@dataclass(frozen=True, slots=True)
class FnoCascadeIndex:
    """The pre-grouped underlying → expiry → contract index (O(1) per level).

    Built once by :func:`build_fno_cascade`; every accessor is an O(1) dict lookup
    into pre-sorted tuples. Immutable + hashable-free (the maps are plain dicts kept
    private), so the dashboard can cache one instance per dataset-list revision.
    """

    _underlyings: tuple[str, ...]
    _expiries: Mapping[str, tuple[str, ...]]
    _contracts: Mapping[tuple[str, str], tuple[FnoContract, ...]]
    #: Datasets whose ``groww_symbol`` did not decode to a contract (no underlying /
    #: expiry) — surfaced honestly, never hidden (Golden Rule #1).
    unparsed: tuple[FnoContract, ...]

    def underlyings(self) -> tuple[str, ...]:
        """Every underlying that has F&O candle data, ascending — O(1)."""
        return self._underlyings

    def expiries(self, underlying: str) -> tuple[str, ...]:
        """The ISO ``YYYY-MM-DD`` expiries for *underlying*, ascending — O(1).

        Empty tuple for an underlying with no F&O data (never raises).
        """
        return self._expiries.get(underlying, ())

    def contracts(self, underlying: str, expiry: str) -> tuple[FnoContract, ...]:
        """The contracts for one ``(underlying, expiry)``, sorted — O(1).

        Sorted futures-first then by ``(strike, option_type)`` so a CE/PE pair sits
        together at each strike. Empty tuple for an unknown cell (never raises).
        """
        return self._contracts.get((underlying, expiry), ())


def _contract_sort_key(contract: FnoContract) -> tuple[tuple[int, float], str]:
    """Deterministic order: futures (no strike) first, then ascending strike, then type.

    The strike is a Decimal-string; sorting it as a float keeps ``"9000"`` before
    ``"10000"`` (a lexical sort would put ``"10000"`` first). A non-numeric / absent
    strike (a future) uses :data:`_NO_STRIKE_SORT` so it never compares against a
    float. ``option_type`` breaks the CE/PE tie at one strike (CE before PE).
    """
    primary = _NO_STRIKE_SORT if contract.strike is None else (1, float(contract.strike))
    return (primary, contract.option_type or contract.instrument_type or "")


def build_fno_cascade(datasets: Iterable[Mapping[str, Any]]) -> FnoCascadeIndex:
    """Group decoded F&O datasets into the underlying → expiry → contract cascade.

    *datasets* are the rows from
    :func:`brutex.dashboard.db_view.list_fno_candle_datasets` (each carrying
    ``underlying`` / ``expiry`` / ``strike`` / ``instrument_type`` / ``option_type``
    / ``symbol`` / ``exchange`` / ``interval``). One ``O(datasets)`` pass buckets the
    parseable rows by ``(underlying, expiry)`` and aggregates a contract's intervals
    by its ``groww_symbol``; the per-level tuples are sorted ONCE here so every
    accessor is O(1). A row with no ``underlying`` or no ``expiry`` (an undecodable
    partition) goes to :attr:`FnoCascadeIndex.unparsed` — surfaced, never dropped.

    Returns an empty index (all tuples empty) for an empty / F&O-less lake — never
    raises (Golden Rule #1).
    """
    # symbol → (row fields + the set of intervals seen for that contract symbol).
    by_symbol: dict[str, dict[str, Any]] = {}
    unparsed_by_symbol: dict[str, dict[str, Any]] = {}
    for row in datasets:
        symbol = str(row["symbol"])
        underlying = row.get("underlying")
        expiry = row.get("expiry")
        interval = str(row["interval"])
        # An undecodable contract (no underlying OR no expiry) cannot be placed in the
        # cascade — collect it separately so it is visible, not hidden.
        target = (
            by_symbol if (underlying is not None and expiry is not None) else unparsed_by_symbol
        )
        entry = target.get(symbol)
        if entry is None:
            target[symbol] = {
                "symbol": symbol,
                "exchange": str(row["exchange"]),
                "underlying": underlying,
                "expiry": expiry,
                "strike": row.get("strike"),
                "option_type": row.get("option_type"),
                "instrument_type": row.get("instrument_type"),
                "intervals": {interval},
            }
        else:
            entry["intervals"].add(interval)

    def _to_contract(entry: Mapping[str, Any]) -> FnoContract:
        return FnoContract(
            symbol=str(entry["symbol"]),
            exchange=str(entry["exchange"]),
            strike=entry["strike"],
            option_type=entry["option_type"],
            instrument_type=entry["instrument_type"],
            intervals=tuple(sorted(entry["intervals"])),
        )

    contracts_by_cell: dict[tuple[str, str], list[FnoContract]] = {}
    expiries_by_underlying: dict[str, set[str]] = {}
    for entry in by_symbol.values():
        underlying = str(entry["underlying"])
        expiry = str(entry["expiry"])
        contracts_by_cell.setdefault((underlying, expiry), []).append(_to_contract(entry))
        expiries_by_underlying.setdefault(underlying, set()).add(expiry)

    contracts_sorted: dict[tuple[str, str], tuple[FnoContract, ...]] = {
        cell: tuple(sorted(contracts, key=_contract_sort_key))
        for cell, contracts in contracts_by_cell.items()
    }
    expiries_sorted: dict[str, tuple[str, ...]] = {
        underlying: tuple(sorted(expiries))  # ISO YYYY-MM-DD → lexical == chronological
        for underlying, expiries in expiries_by_underlying.items()
    }
    underlyings_sorted = tuple(sorted(expiries_by_underlying))
    unparsed = tuple(
        sorted(
            (_to_contract(entry) for entry in unparsed_by_symbol.values()),
            key=lambda c: c.symbol,
        )
    )
    return FnoCascadeIndex(
        _underlyings=underlyings_sorted,
        _expiries=expiries_sorted,
        _contracts=contracts_sorted,
        unparsed=unparsed,
    )


def build_expiry_counts(
    index: FnoCascadeIndex,
    underlying: str,
    listed_by_expiry: Mapping[str, int] | None,
) -> list[dict[str, Any]]:
    """Per-expiry "listed N with data M" rows for one underlying's cascade level.

    A young live option chain is otherwise silent about WHY an expiry shows only a
    few strikes: the vendor may list dozens but only a handful have been pulled yet.
    This surfaces both counts per expiry so the ``/db`` cascade can render a
    self-explanatory "listed N with data M" chip (DEC-DB-UX-PACK-001 Feature 6):

    * ``with_data`` (M) = the count of DISTINCT contracts that TRULY have lake candle
      data for that ``(underlying, expiry)`` — exactly what the cascade offers
      (:meth:`FnoCascadeIndex.contracts`), so it can never over-count a phantom strike.
    * ``listed`` (N) = the count of vendor-LISTED contracts for that expiry from the
      authoritative ``registry.duckdb`` calendar (passed in as *listed_by_expiry*), or
      ``None`` when the registry does not know this underlying (a CLI-only / absent /
      unreadable registry). ``None`` reads as an honest "with data M" chip — never a
      misleading "listed 0 with data M" (Golden Rule #1). ``listed`` MAY exceed
      ``with_data`` (the young-chain case) and MAY equal it (fully pulled).

    Rows are aligned with :meth:`FnoCascadeIndex.expiries` order (ascending ISO) and
    an unknown underlying yields ``[]``. Pure + deterministic (no I/O).
    """
    rows: list[dict[str, Any]] = []
    for expiry in index.expiries(underlying):
        with_data = len(index.contracts(underlying, expiry))
        listed = None if listed_by_expiry is None else listed_by_expiry.get(expiry)
        rows.append({"expiry": expiry, "listed": listed, "with_data": with_data})
    return rows


def contract_to_json(contract: FnoContract) -> dict[str, Any]:
    """Project one :class:`FnoContract` to a JSON-native cascade row (no Decimal/None traps).

    The exact shape the ``/db`` cascade frontend consumes: the query key (``symbol``)
    + the display ``label`` + the decoded identity + the available ``intervals``.
    Every value is a JSON primitive (strike stays a string, ``None`` is JSON ``null``),
    so Starlette's ``JSONResponse`` never chokes.
    """
    return {
        "symbol": contract.symbol,
        "label": contract.label,
        "exchange": contract.exchange,
        "strike": contract.strike,
        "option_type": contract.option_type,
        "instrument_type": contract.instrument_type,
        "intervals": list(contract.intervals),
    }
