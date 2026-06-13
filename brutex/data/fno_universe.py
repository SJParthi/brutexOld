"""F&O underlying-universe reconciliation — pure transforms for the builder.

The universe-builder *script* (``scripts/build_fno_universe.py``) fetches three
live sources on the operator's machine (NSE underlying-information API, NSE
``fo_mktlots.csv``, and the vendor scrip master), then uses the deterministic,
fully-tested functions here to parse + cross-check + serialise the result
(DEC-FNO-UNIVERSE-BUILDER-001). Keeping the transforms in the ``brutex`` package
(not the script) makes them COMMON + 100%-covered; the network I/O stays in the
coverage-excluded script.

* :func:`parse_fo_mktlots` — the NSE lot-size CSV → near-month lot per F&O
  underlying, split into the 5 index underlyings vs the single-stock underlyings.
* :func:`cross_check` / :func:`common_symbols` — reconcile the three sources'
  symbol sets; surface every symbol present in one but missing from the others
  (the additions/exclusions to watch).
* :class:`FnoUnderlying` + :func:`to_csv` — the timestamped output rows.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final

#: The ``UNDERLYING`` cell that marks the index → single-stock boundary in
#: NSE's ``fo_mktlots.csv``.
_STOCK_SECTION_MARKER: Final[str] = "Derivatives on Individual Securities"

#: A real ``fo_mktlots`` data row has at least UNDERLYING, SYMBOL, and one lot cell.
_MIN_ROW_CELLS: Final[int] = 3

#: Output column order for ``data/fno_underlyings_<YYYYMMDD>.csv``.
OUTPUT_COLUMNS: Final[tuple[str, ...]] = (
    "symbol",
    "underlying_security_id",
    "instrument_type",
    "exchange",
    "lot_size",
    "source",
    "fetched_at_utc",
)


@dataclass(frozen=True, slots=True)
class FoMktLots:
    """Parsed NSE ``fo_mktlots.csv`` — near-month lot size per F&O underlying."""

    index_lots: dict[str, int]  # the 5 F&O index underlyings (NIFTY/BANKNIFTY/…)
    stock_lots: dict[str, int]  # the single-stock underlyings (~211)


@dataclass(frozen=True, slots=True)
class FnoUnderlying:
    """One resolved F&O underlying row for the output CSV."""

    symbol: str
    underlying_security_id: int
    instrument_type: str  # OPTSTK / OPTIDX / FUTSTK / FUTIDX
    exchange: str  # NSE / BSE
    lot_size: int


def _first_lot(cells: list[str]) -> int | None:
    """Return the first non-empty cell parsed as ``int``, else ``None``."""
    for cell in cells:
        text = cell.strip()
        if text:
            try:
                return int(text)
            except ValueError:
                return None
    return None


def parse_fo_mktlots(text: str) -> FoMktLots:
    """Parse the (space-padded) NSE ``fo_mktlots.csv`` into index + stock lot maps.

    Layout: an index block, a ``Derivatives on Individual Securities`` marker
    row, then the single-stock block. Each data row is
    ``UNDERLYING, SYMBOL, <near-month lot>, <next-month lot>, …`` with
    whitespace-padded cells; the near-month lot is the first non-empty lot cell.
    Header / blank / lot-less rows are skipped.
    """
    index_lots: dict[str, int] = {}
    stock_lots: dict[str, int] = {}
    in_stocks = False
    for row in csv.reader(io.StringIO(text)):
        if len(row) < _MIN_ROW_CELLS:
            continue
        underlying = row[0].strip()
        symbol = row[1].strip()
        if underlying == _STOCK_SECTION_MARKER:
            in_stocks = True
            continue
        if not underlying or not symbol or symbol.upper() == "SYMBOL":
            continue  # header / blank row
        lot = _first_lot(row[2:])
        if lot is None:
            continue  # no parseable lot → not a real underlying row
        (stock_lots if in_stocks else index_lots)[symbol] = lot
    return FoMktLots(index_lots=index_lots, stock_lots=stock_lots)


def cross_check(sources: Mapping[str, set[str]]) -> dict[str, set[str]]:
    """For each named symbol source, the symbols present ONLY there.

    A symbol unique to one source (missing from the union of all others) is an
    addition/exclusion to watch — the builder logs these so a divergence between
    the NSE API, ``fo_mktlots``, and the vendor master is never silent. O(total
    symbols) via set algebra.
    """
    result: dict[str, set[str]] = {}
    for name, symbols in sources.items():
        others: set[str] = set()
        for other_name, other in sources.items():
            if other_name != name:
                others |= other
        result[name] = symbols - others
    return result


def common_symbols(sources: Mapping[str, set[str]]) -> set[str]:
    """Return the symbols present in EVERY source (empty if no sources)."""
    sets = list(sources.values())
    if not sets:
        return set()
    common = set(sets[0])
    for other in sets[1:]:
        common &= other
    return common


def to_csv(underlyings: Iterable[FnoUnderlying], *, source: str, fetched_at_utc: str) -> str:
    """Serialise *underlyings* to the timestamped output CSV text (``OUTPUT_COLUMNS``)."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(OUTPUT_COLUMNS)
    for u in sorted(underlyings, key=lambda x: (x.exchange, x.symbol)):
        writer.writerow(
            [
                u.symbol,
                u.underlying_security_id,
                u.instrument_type,
                u.exchange,
                u.lot_size,
                source,
                fetched_at_utc,
            ]
        )
    return buf.getvalue()


__all__ = [
    "OUTPUT_COLUMNS",
    "FnoUnderlying",
    "FoMktLots",
    "common_symbols",
    "cross_check",
    "parse_fo_mktlots",
    "to_csv",
]
