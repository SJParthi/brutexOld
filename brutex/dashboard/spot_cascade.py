"""O(1)-per-step symbol → interval cascade for the ``/db`` SPOT candle browser.

DEC-DB-SPOT-CASCADE-001. The ``/db`` SPOT candle picker is otherwise ONE flat
dropdown of every ``[spot] NSE · CASH · RELIANCE · 1minute`` partition — with the
~750-name reference universe × the stored intervals that is a long flat list the
operator can't easily navigate ("i can't select the underlying symbol or
timeframe"). This module pre-groups the discovered SPOT datasets
(:func:`brutex.dashboard.db_view.list_candle_datasets`, the non-F&O segments) into
a TWO-LEVEL cascade — **symbol → interval** — the spot twin of the F&O
underlying → expiry → contract cascade (:mod:`brutex.dashboard.fno_cascade`,
DEC-DB-FNO-CASCADE-001).

## Stored-only intervals (Golden Rule #1 — no phantom timeframes)

The interval level offers ONLY the intervals that PHYSICALLY exist for the symbol
in the lake — in practice ``1minute`` plus the derived ``1day`` partition
(DEC-1MINUTE-ONLY-DERIVE-1DAY-001). :func:`brutex.dashboard.db_view.query_candles`
reads a real on-disk partition and returns an EMPTY page for an absent one (it does
NOT resample on-the-fly), so offering all 21 timeframes here would let the operator
pick a TF that renders an empty grid — a phantom. The cascade therefore mirrors F&O
exactly: each leaf advertises the set of intervals actually on disk.

## O(1) discipline (CLAUDE.md §2 Rule 5)

:func:`build_spot_cascade` groups in ONE ``O(datasets)`` pass at build time (the sort
is ``O(n log n)`` once); the per-interaction read :meth:`SpotCascadeIndex.symbols` is
an O(1) tuple handoff of the pre-sorted leaves (each carrying its own intervals, so
the frontend needs no second round-trip). The dashboard caches the index per
dataset-list revision behind the SAME generation signal the F&O cascade and the
candle-count caches use (the COMMON :class:`~brutex.dashboard.movers_cache.MoversSnapshotCache`),
so a repeat cascade click never re-scans the lake.

Pure + deterministic (no I/O). Imports nothing from ``dashboard.*`` / ``data.*`` —
the route does the lake read + wiring — so there is no import cycle (the CodeQL
``py/cyclic-import`` invariant, mirroring :mod:`brutex.dashboard.fno_cascade`).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final

__all__ = [
    "SpotCascadeIndex",
    "SpotSymbol",
    "build_spot_cascade",
    "symbol_to_json",
]

#: Segment sort rank: an INDEX (the brute-force indices) floats ABOVE the CASH
#: reference equities, then symbol-alphabetical within each rank. Data-driven (no
#: hardcoded symbol list — DYNAMIC, CLAUDE.md §2 Rule 11): the ``INDEX`` segment
#: naturally pins NIFTY/BANKNIFTY/SENSEX to the top of the picker.
_INDEX_SEGMENT: Final[str] = "INDEX"


@dataclass(frozen=True, slots=True)
class SpotSymbol:
    """One spot symbol under an ``(exchange, segment)`` — the cascade leaf.

    Aggregates the (≥1) intervals the lake holds for ONE spot ``groww_symbol``, so a
    symbol present at both ``1minute`` and the derived ``1day`` is ONE leaf with
    ``intervals=("1day", "1minute")`` rather than two flat rows.

    Attributes
    ----------
    exchange
        The partition exchange (``NSE`` / ``BSE``).
    segment
        The partition segment (``INDEX`` for the indices, ``CASH`` for equities) —
        the exact coordinate :func:`brutex.dashboard.db_view.query_candles` takes.
    symbol
        The spot ``groww_symbol`` (``NSE-NIFTY`` / ``NSE-RELIANCE``) — the query key.
    intervals
        The lake intervals available for this symbol, ascending (e.g.
        ``("1day", "1minute")``). ONLY intervals with a real on-disk partition.
    """

    exchange: str
    segment: str
    symbol: str
    intervals: tuple[str, ...]

    @property
    def label(self) -> str:
        """A compact human label — the bare ``symbol`` (already exchange-prefixed)."""
        return self.symbol


@dataclass(frozen=True, slots=True)
class SpotCascadeIndex:
    """The pre-grouped symbol → interval index (O(1) read).

    Built once by :func:`build_spot_cascade`; :meth:`symbols` is an O(1) handoff of the
    pre-sorted leaves (each carrying its own intervals). Immutable, so the dashboard can
    cache one instance per dataset-list revision.
    """

    _symbols: tuple[SpotSymbol, ...]

    def symbols(self) -> tuple[SpotSymbol, ...]:
        """Every spot symbol that has candle data — indices first, then alpha — O(1)."""
        return self._symbols


def _symbol_sort_key(sym: SpotSymbol) -> tuple[int, str, str]:
    """Deterministic order: INDEX segment first, then by symbol, then exchange.

    ``segment_rank`` is 0 for the ``INDEX`` segment (the brute-force indices float to the
    top) and 1 otherwise (the CASH reference equities), so the picker leads with the
    three indices the operator most often opens, then the alphabetical equity tail.
    """
    segment_rank = 0 if sym.segment == _INDEX_SEGMENT else 1
    return (segment_rank, sym.symbol, sym.exchange)


def build_spot_cascade(datasets: Iterable[Mapping[str, Any]]) -> SpotCascadeIndex:
    """Group discovered SPOT datasets into the symbol → interval cascade.

    *datasets* are the non-F&O rows from
    :func:`brutex.dashboard.db_view.list_candle_datasets` (each carrying ``exchange`` /
    ``segment`` / ``symbol`` / ``interval``). One ``O(datasets)`` pass aggregates a
    symbol's intervals by its ``(exchange, segment, symbol)`` triple; the leaves are
    sorted ONCE here (INDEX-first, then symbol) so :meth:`SpotCascadeIndex.symbols` is
    O(1). Returns an empty index for an empty / spot-less lake — never raises (Golden
    Rule #1).
    """
    # (exchange, segment, symbol) → the set of intervals seen for that spot symbol.
    by_key: dict[tuple[str, str, str], set[str]] = {}
    for row in datasets:
        key = (str(row["exchange"]), str(row["segment"]), str(row["symbol"]))
        by_key.setdefault(key, set()).add(str(row["interval"]))

    leaves = tuple(
        sorted(
            (
                SpotSymbol(
                    exchange=exchange,
                    segment=segment,
                    symbol=symbol,
                    intervals=tuple(sorted(intervals)),
                )
                for (exchange, segment, symbol), intervals in by_key.items()
            ),
            key=_symbol_sort_key,
        )
    )
    return SpotCascadeIndex(_symbols=leaves)


def symbol_to_json(sym: SpotSymbol) -> dict[str, Any]:
    """Project one :class:`SpotSymbol` to a JSON-native cascade row.

    The exact shape the ``/db`` spot cascade frontend consumes: the query coordinates
    (``exchange`` / ``segment`` / ``symbol``) + the display ``label`` + the available
    ``intervals`` (inline, so the picker needs no second fetch). Every value is a JSON
    primitive, so Starlette's ``JSONResponse`` never chokes.
    """
    return {
        "exchange": sym.exchange,
        "segment": sym.segment,
        "symbol": sym.symbol,
        "label": sym.label,
        "intervals": list(sym.intervals),
    }
