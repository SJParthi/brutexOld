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
from brutex.universe import (
    build_fno_eligible_underlyings,
    is_option_pullable,
    is_universe_underlying,
)

#: Classification of an ``IDX`` instrument.
KIND_INDEX: Final[str] = "index"

#: Classification of an F&O-eligible single-stock (equity) underlying.
KIND_STOCK: Final[str] = "stock"

#: The two kinds the explorer admits, in display order (index first).
EXPLORER_KINDS: Final[tuple[str, ...]] = (KIND_INDEX, KIND_STOCK)


def classify_instrument(
    instrument: Instrument,
    fno_eligible_underlyings: frozenset[tuple[Exchange, str]],
    reference_security_ids: frozenset[int] = frozenset(),
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
    reference_security_ids
        Set of admitted REFERENCE_ONLY constituent ``security_id``\\s
        (DEC-NIFTY-TOTAL-MARKET-ADMIT-001 / DEC-INDEX-CONSTITUENTS-FULL-001).
        When supplied, a non-F&O NTM constituent EQ — whose ``symbol_name``
        is the long company name, so the F&O ticker key cannot match — is
        ALSO classified ``"stock"`` (so the explorer can show + tick the full
        ~750-stock universe, not just the ~212 F&O names). Defaults empty →
        byte-identical to the prior F&O-only classification (every existing
        caller that omits it, e.g. :func:`universe_summary`, is unchanged).

    Notes
    -----
    The admit/reject decision is delegated to
    :func:`brutex.universe.is_universe_underlying` so the index-vs-leg /
    F&O-eligibility rule lives in exactly ONE place (CLAUDE.md §11 COMMON,
    DEC-INSTRUMENT-UNIVERSE-FAST-001). This function only adds the
    KIND label (``"index"`` vs ``"stock"``) on top of that single rule:
    an admitted ``IDX`` → ``"index"``; an admitted ``EQ`` → ``"stock"``.
    """
    if not is_universe_underlying(instrument, fno_eligible_underlyings, reference_security_ids):
        return None
    return KIND_INDEX if instrument.instrument == InstrumentType.IDX else KIND_STOCK


def _iter_instruments(snapshot: Iterable[Instrument]) -> list[Instrument]:
    """Materialise *snapshot* into a list so it can be scanned twice."""
    return list(snapshot)


def universe_summary(
    snapshot: Iterable[Instrument],
    *,
    fno_eligible: frozenset[tuple[Exchange, str]] | None = None,
    reference_only_ids: frozenset[int] = frozenset(),
) -> dict[str, int]:
    """Return headline counts for the explorer's KPI band.

    Scans *snapshot* once to build the F&O-eligible set, then once more
    to tally how many instruments fall into each explorer kind.

    Returns
    -------
    dict[str, int]
        ``{"index_count": int, "stock_count": int, "total": int,
        "reference_stock_count": int, "stock_count_total": int}``
        where ``total == index_count + stock_count`` (unclassified
        rows — derivative legs, non-F&O equities — are excluded).

        The two ADDITIVE keys (DEC-LABEL-TRUTH-001) make the Stocks badge
        able to label the SAME population the explorer's Stocks tab lists
        (:func:`list_underlyings` classifies WITH *reference_only_ids*):

        * ``stock_count_total`` — every instrument classifying ``"stock"``
          under the SAME classification the tab list uses (the ticker arm
          plus the reference-id arm), i.e. the number of rows the Stocks
          tab actually shows.
        * ``reference_stock_count`` — the subset of those stocks whose
          ``security_id`` is in *reference_only_ids* (the admitted
          REFERENCE_ONLY NTM constituents), so the badge can honestly
          split "N stocks · M F&O" with ``M = stock_count_total −
          reference_stock_count`` — never a lost number.

        HONESTY NOTE (Golden Rule #10): the split is by REFERENCE-ID
        MEMBERSHIP, not by "classifies without the reference ids" — on the
        LEAN store the SQL-derived eligible set already contains every
        persisted NSE EQ ``symbol_name`` INCLUDING the reference rows
        (:meth:`~brutex.data.instrument_store.InstrumentStore.fno_eligible_underlyings`
        arm (b), default ``tradable_only=False``), so a reference row
        classifies ``"stock"`` by TICKER there and a classify-twice diff
        would read a dishonest 0. Membership in *reference_only_ids* is
        the store's own explicit REFERENCE_ONLY flag — the one honest
        discriminator on both store shapes.

        ``stock_count`` / ``total`` keep their EXACT legacy semantics
        (classification WITHOUT the reference ids) — byte-identical for
        every existing caller; the new keys read
        ``reference_stock_count == 0`` and ``stock_count_total ==
        stock_count`` when *reference_only_ids* is empty (stable key set).

    Parameters
    ----------
    snapshot
        Any iterable of :class:`~brutex.models.Instrument` — typically
        a :class:`~brutex.data.instrument_store.InstrumentSnapshot`, OR
        (fast path) just the IDX + EQ candidate rows from
        :meth:`~brutex.data.instrument_store.InstrumentStore.list_index_equity_instruments`.
    fno_eligible
        Pre-computed NSE F&O-eligible ``(exchange, symbol)`` set. When
        ``None`` (default) it is derived from *snapshot* itself (the
        snapshot must then carry the derivative legs). Supplying it lets
        callers pass a *candidates-only* snapshot (IDX + EQ) plus the
        cheaply-queried eligible set, avoiding materialising every leg
        (DEC-INSTRUMENT-UNIVERSE-FAST-001) — the classification rule is
        unchanged either way.
    reference_only_ids
        Set of admitted REFERENCE_ONLY constituent ``security_id``\\s (the
        same set :func:`list_underlyings` takes). Defaults empty —
        byte-identical legacy counts plus the two additive keys at their
        degenerate values. When supplied, ``stock_count_total`` counts the
        tab-list population and ``reference_stock_count`` its
        reference-only subset (see Returns).
    """
    instruments = _iter_instruments(snapshot)
    if fno_eligible is None:
        fno_eligible = build_fno_eligible_underlyings(instruments)
    index_count = 0
    stock_count = 0
    reference_stock_count = 0
    stock_count_total = 0
    for instrument in instruments:
        kind = classify_instrument(instrument, fno_eligible)
        if kind == KIND_INDEX:
            index_count += 1
        elif kind == KIND_STOCK:
            stock_count += 1
        # The FULL classification — the SAME rule list_underlyings applies, so
        # stock_count_total is exactly the Stocks-tab population (CLAUDE.md §11
        # COMMON: classify_instrument stays the ONE rule; this is a second CALL,
        # never a second rule). The reference split is by id membership — see
        # the Returns honesty note.
        kind_full = classify_instrument(instrument, fno_eligible, reference_only_ids)
        if kind_full == KIND_STOCK:
            stock_count_total += 1
            if instrument.security_id in reference_only_ids:
                reference_stock_count += 1
    return {
        "index_count": index_count,
        "stock_count": stock_count,
        "total": index_count + stock_count,
        "reference_stock_count": reference_stock_count,
        "stock_count_total": stock_count_total,
    }


def list_underlyings(
    snapshot: Iterable[Instrument],
    *,
    kind: str | None = None,
    fno_eligible: frozenset[tuple[Exchange, str]] | None = None,
    reference_only_ids: frozenset[int] = frozenset(),
) -> list[dict[str, Any]]:
    """Return the explorer's instrument rows, optionally filtered by *kind*.

    Each returned row is the JSON-friendly shape the ``/universe`` page's
    instruments column renders: ``symbol`` (the bare ``symbol_name`` for
    display), ``pull_symbol`` (also ``symbol_name`` — the key the spot pull
    button POSTs to resolve the instrument via the store; ADR-0003 dropped
    the old exchange-prefixed ``vendor_symbol``), display name, kind,
    exchange, the contract economics ``lot_size`` (int) / ``tick_size`` (str —
    a Decimal serialised exactly) / ``freeze_quantity`` (int | None) — the
    F&O lot + freeze folded from the legs at ingest (DEC-GROWW-LEG-ECONOMICS-001)
    + the spot tick, surfaced as columns per DEC-DASHBOARD-ECON-COLUMNS-001 —
    and ``option_pullable``. The real per-underlying contract count is fetched
    lazily via ``/api/v1/contracts`` when the row is clicked.

    Parameters
    ----------
    snapshot
        Iterable of :class:`~brutex.models.Instrument`.
    kind
        ``"index"`` / ``"stock"`` to filter, or ``None`` for both.
        Any other value raises :class:`ValueError`.
    fno_eligible
        Pre-computed NSE F&O-eligible ``(exchange, symbol)`` set. When
        ``None`` (default) it is derived from *snapshot* itself (the
        snapshot must then carry the derivative legs). Supplying it lets
        callers pass a *candidates-only* snapshot (IDX + EQ) plus the
        cheaply-queried eligible set, avoiding materialising every leg
        (DEC-INSTRUMENT-UNIVERSE-FAST-001) — the classification rule is
        unchanged either way.

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
    if fno_eligible is None:
        fno_eligible = build_fno_eligible_underlyings(instruments)
    rows: list[dict[str, Any]] = []
    for instrument in instruments:
        # Propagate reference_only_ids so admitted REFERENCE_ONLY NTM constituents
        # (non-F&O equities) classify as "stock" too — the explorer then shows + ticks
        # the FULL held universe, not just the F&O names (DEC-INDEX-CONSTITUENTS-FULL-001).
        # The same set already drives ``option_pullable`` below, so a reference row is a
        # "stock" with ``option_pullable=False`` (greys the options-pull button).
        classified = classify_instrument(instrument, fno_eligible, reference_only_ids)
        if classified is None:
            continue
        if kind is not None and classified != kind:
            continue
        rows.append(
            {
                "symbol": instrument.symbol_name,
                "pull_symbol": instrument.symbol_name,
                "name": instrument.display_name or instrument.symbol_name,
                "kind": classified,
                "exchange": str(instrument.exch_id),
                # Contract economics (DEC-DASHBOARD-ECON-COLUMNS-001): the tradable
                # F&O lot + freeze folded from the derivative legs at ingest
                # (DEC-GROWW-LEG-ECONOMICS-001) + the spot tick. ``tick_size`` is a
                # Decimal → serialised to ``str`` for JSON (exact, no float noise);
                # ``freeze_quantity`` is ``None`` for an index / non-F&O spot row.
                "lot_size": instrument.lot_size,
                "tick_size": str(instrument.tick_size),
                "freeze_quantity": instrument.sm_freeze_qty,
                # Whether the "⛓ Pull options chain" action applies to this row
                # (DEC-OPTIONS-PULL-GUARD-001) — False for NTM + its reference-only
                # non-F&O constituents + India VIX, so the UI greys the button.
                "option_pullable": is_option_pullable(
                    instrument, reference_only_ids=reference_only_ids
                ),
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
