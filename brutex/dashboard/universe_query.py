"""F&O Universe Explorer — classification + summary query helpers.

Pure, dependency-light functions powering the ``/universe`` dashboard
page (DEC-UNIVERSE-DASHBOARD-001). Per CLAUDE.md §11 (COMMON) the
classification rule lives in exactly ONE place — :mod:`brutex.universe`
— and this module *reuses* it rather than re-deriving the index-vs-stock
split. The dashboard FastAPI routes in :mod:`brutex.dashboard.app`
(coverage-excluded, FastAPI-dependent) stay thin wrappers that delegate
the real logic here so it is 100% unit-testable offline.

## The two filter kinds (locked design)

The F&O Universe Explorer (``docs/dashboard/mockups/universe-explorer-v1.html``)
shows exactly **two** instrument kinds:

* ``"index"``  — **ANY** ``IDX`` instrument, i.e. **every NSE index**,
  not only the 3 F&O / brute-force indices. This includes:

  - broad-market indices (e.g. NIFTY 50, NIFTY 100, NIFTY 500),
  - sectoral indices (e.g. NIFTY IT, NIFTY AUTO, NIFTY PHARMA),
  - the volatility index (INDIA VIX),
  - the brute-force universe (NIFTY / BANKNIFTY / SENSEX).

  The brute-force universe (+ India VIX reference-only) per CLAUDE.md §3
  is a *subset* of this kind — the explorer deliberately surfaces every
  index. Non-F&O indices are **spot-only**: they have no expiries /
  contracts, so the expiry / option-chain drill is simply empty for
  them (a sectoral index has no derivatives to list).
* ``"stock"``  — an **F&O-eligible single-stock underlying**. A
  "stock" here means an equity (``EQ``) whose ``(exchange, symbol)``
  appears as the underlying of at least one NSE futures / option in
  the same snapshot. There is NO non-F&O cash-equity bucket — a
  cash equity with no derivatives is simply not part of the explorer.

Anything else (a derivative leg itself — FUT / CE / PE, or a cash
equity that is not F&O-eligible) is *unclassified* and excluded from
the explorer's instrument list; the drill into expiries / contracts is
served by the existing ``/api/v1/expiries`` + ``/api/v1/contracts``
endpoints keyed by ``underlying``.

## Why a snapshot, not the store

The classifier needs the full set of derivatives to decide which
equities are F&O-eligible (:func:`brutex.universe.build_fno_eligible_underlyings`).
A :class:`~brutex.data.instrument_store.InstrumentSnapshot` is the
frozen in-memory view the dashboard already loads via
``store.load_snapshot(in_scope_only=False)``; these helpers iterate it
once and answer in O(1)/O(n-once) without further DuckDB round-trips.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Final

from brutex.models import Exchange, Instrument, InstrumentType
from brutex.universe import build_fno_eligible_underlyings

#: Classification of an ``IDX`` instrument.
KIND_INDEX: Final[str] = "index"

#: Classification of an F&O-eligible single-stock (equity) underlying.
KIND_STOCK: Final[str] = "stock"

#: The two kinds the explorer admits, in display order (index first).
EXPLORER_KINDS: Final[tuple[str, ...]] = (KIND_INDEX, KIND_STOCK)


def classify_instrument(
    instrument: Instrument,
    fno_eligible_underlyings: frozenset[tuple[Exchange, str]],
) -> str | None:
    """Classify *instrument* into an explorer kind, or ``None``.

    ``"index"`` is returned for **ANY** ``IDX`` instrument — every NSE
    index, not just the 3 F&O / brute-force indices. A sectoral index
    (e.g. NIFTY IT / AUTO / PHARMA), a broad-market index (e.g. NIFTY
    100 / 500) and INDIA VIX all classify as ``"index"`` exactly like
    NIFTY / BANKNIFTY / SENSEX. Non-F&O indices are spot-only (they have
    no expiries / contracts).

    Returns
    -------
    str | None
        ``"index"`` for any ``IDX`` instrument; ``"stock"`` for an
        ``EQ`` instrument whose ``(exchange, trading_symbol)`` is in
        *fno_eligible_underlyings*; ``None`` for everything else (a
        derivative leg, or a non-F&O-eligible equity).

    Parameters
    ----------
    instrument
        The candidate instrument-master row.
    fno_eligible_underlyings
        Set of NSE F&O-eligible ``(exchange, symbol)`` pairs, typically
        built once via
        :func:`brutex.universe.build_fno_eligible_underlyings`.
    """
    if instrument.instrument_type == InstrumentType.IDX:
        return KIND_INDEX
    if (
        instrument.instrument_type == InstrumentType.EQ
        and (
            instrument.exchange,
            instrument.trading_symbol,
        )
        in fno_eligible_underlyings
    ):
        return KIND_STOCK
    return None


def _iter_instruments(snapshot: Iterable[Instrument]) -> list[Instrument]:
    """Materialise *snapshot* into a list so it can be scanned twice."""
    return list(snapshot)


def universe_summary(snapshot: Iterable[Instrument]) -> dict[str, int]:
    """Return headline counts for the explorer's KPI band.

    Scans *snapshot* once to build the F&O-eligible set, then once more
    to tally how many instruments fall into each explorer kind.

    Returns
    -------
    dict[str, int]
        ``{"index_count": int, "stock_count": int, "total": int}``
        where ``total == index_count + stock_count`` (unclassified
        rows — derivative legs, non-F&O equities — are excluded).

    Parameters
    ----------
    snapshot
        Any iterable of :class:`~brutex.models.Instrument` — typically
        a :class:`~brutex.data.instrument_store.InstrumentSnapshot`.
    """
    instruments = _iter_instruments(snapshot)
    fno_eligible = build_fno_eligible_underlyings(instruments)
    index_count = 0
    stock_count = 0
    for instrument in instruments:
        kind = classify_instrument(instrument, fno_eligible)
        if kind == KIND_INDEX:
            index_count += 1
        elif kind == KIND_STOCK:
            stock_count += 1
    return {
        "index_count": index_count,
        "stock_count": stock_count,
        "total": index_count + stock_count,
    }


def list_underlyings(
    snapshot: Iterable[Instrument],
    *,
    kind: str | None = None,
) -> list[dict[str, Any]]:
    """Return the explorer's instrument rows, optionally filtered by *kind*.

    Each returned row is the JSON-friendly shape the ``/universe`` page's
    instruments column renders: symbol, display name, kind, exchange and
    a contract-count placeholder (``"spot"`` for an index that the
    explorer treats as spot-only; the real per-underlying contract count
    is fetched lazily via ``/api/v1/contracts`` when the row is clicked).

    Parameters
    ----------
    snapshot
        Iterable of :class:`~brutex.models.Instrument`.
    kind
        ``"index"`` / ``"stock"`` to filter, or ``None`` for both.
        Any other value raises :class:`ValueError`.

    Returns
    -------
    list[dict[str, Any]]
        Rows sorted by ``(kind, symbol)`` for a stable explorer order
        (indices first, then stocks, alphabetical within each).

    Raises
    ------
    ValueError
        If *kind* is not ``None``, ``"index"`` or ``"stock"``.
    """
    if kind is not None and kind not in EXPLORER_KINDS:
        raise ValueError(f"kind must be one of {EXPLORER_KINDS} or None, got {kind!r}")

    instruments = _iter_instruments(snapshot)
    fno_eligible = build_fno_eligible_underlyings(instruments)
    rows: list[dict[str, Any]] = []
    for instrument in instruments:
        classified = classify_instrument(instrument, fno_eligible)
        if classified is None:
            continue
        if kind is not None and classified != kind:
            continue
        rows.append(
            {
                "symbol": instrument.trading_symbol,
                "name": instrument.name or instrument.trading_symbol,
                "kind": classified,
                "exchange": str(instrument.exchange),
            }
        )
    rows.sort(key=lambda row: (EXPLORER_KINDS.index(row["kind"]), row["symbol"]))
    return rows


__all__ = [
    "EXPLORER_KINDS",
    "KIND_INDEX",
    "KIND_STOCK",
    "classify_instrument",
    "list_underlyings",
    "universe_summary",
]
