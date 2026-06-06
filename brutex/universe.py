"""BruteX Track 1 universe — Indian spot indexes + India VIX.

**STATUS (2026-05-26):** universe definitions are vendor-agnostic. Post
DEC-VENDOR-PIVOT-001 the Dhan adapter consumes these definitions
(prior broker adapter wiped). The prior universe (8 F&O indices + dynamic
stock-derivative discovery) is out of scope for Track 1. India VIX
added after 4-agent verification on 2026-05-15 (late-day scope
expansion, see
``docs/TRACK1_DATA_INGEST_DESIGN.md`` §2 and ``docs/MIGRATION.md``
decisions #71-72).

## Track 1 universe

Four Indian equity-index instruments:

* **NIFTY 50** (NSE spot)
* **NIFTY BANK** (NSE spot) — symbol ``BANKNIFTY``
* **BSE SENSEX** (BSE spot) — symbol ``SENSEX``
* **India VIX** (NSE volatility index) — symbol ``INDIAVIX``

All four are computed index values, not tradable directly. Track 1
produces signal-only research output (``paper_pnl_paisa``); execution-
realistic PnL is deferred to the Options Track (post-Track-1).

India VIX is a volatility index calculated from NIFTY 50 option mid-
quotes. It updates every 15 seconds (NOT tick-by-tick) and shares the
NSE trading window (09:15-15:30 IST) with the spot indexes.

## Scope filters — two tiers (DEC-FETCH-ALL-001)

The project fetches a BROAD lake but brute-forces a NARROW engine:

* **FETCH tier** — ``is_in_fetch_scope(instrument, fno_eligible)``: every
  NSE index + every NSE F&O futures/option + every F&O-eligible NSE cash
  equity + BSE SENSEX. This is what gets downloaded + stored.
* **BRUTE-FORCE tier** — ``is_in_brute_force_scope(instrument)``: only the
  3 swept indexes (NIFTY 50, BANKNIFTY, SENSEX; India VIX excluded).

The legacy ``is_in_scope(instrument)`` is the original narrow index
filter (the 4 named indexes incl. India VIX) and is unchanged for
backwards compatibility — ``instrument_type == IDX`` AND
``(exchange, trading_symbol)`` in :data:`INDEX_UNDERLYINGS`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from brutex.models import Exchange, Instrument, InstrumentType

# ────────────────────────────────────────────────────────────────────────
# Spec dataclass
# ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class UnderlyingSpec:
    """Spec for one named index underlying in the Track 1 universe.

    Attributes
    ----------
    reference_only
        When True (India VIX), the instrument is fetched + stored but is
        NEVER part of brute-force pattern combinations or the 52-condition
        vocabulary. It is captured as ``vix_at_entry`` / ``vix_at_exit``
        snapshots on every trade for downstream filter analysis.
    """

    exchange: Exchange
    underlying_symbol: str
    display_name: str
    spot_live_since_year: int
    reference_only: bool = False

    def __str__(self) -> str:
        return f"{self.exchange}:{self.underlying_symbol}"


# ────────────────────────────────────────────────────────────────────────
# The 4 Track 1 named indexes (allow-list)
# ────────────────────────────────────────────────────────────────────────


#: The 4 named indexes. 3 brute-forced + 1 reference-only (India VIX).
V1_INDICES: Final[tuple[UnderlyingSpec, ...]] = (
    UnderlyingSpec(
        exchange=Exchange.NSE,
        underlying_symbol="NIFTY",
        display_name="NIFTY 50",
        spot_live_since_year=1996,
    ),
    UnderlyingSpec(
        exchange=Exchange.NSE,
        underlying_symbol="BANKNIFTY",
        display_name="NIFTY BANK",
        spot_live_since_year=2003,
    ),
    UnderlyingSpec(
        exchange=Exchange.BSE,
        underlying_symbol="SENSEX",
        display_name="BSE SENSEX",
        spot_live_since_year=1986,
    ),
    UnderlyingSpec(
        exchange=Exchange.NSE,
        underlying_symbol="INDIAVIX",
        display_name="India VIX",
        spot_live_since_year=2008,
        reference_only=True,
    ),
)


#: Symbols brute-forced (3 — excludes India VIX which is reference-only).
BRUTE_FORCE_INDICES: Final[tuple[UnderlyingSpec, ...]] = tuple(
    u for u in V1_INDICES if not u.reference_only
)


#: Symbols used only for reference / context (1 — India VIX).
REFERENCE_ONLY_INDICES: Final[tuple[UnderlyingSpec, ...]] = tuple(
    u for u in V1_INDICES if u.reference_only
)


#: Backwards-compatibility alias.
V1_UNDERLYINGS: Final[tuple[UnderlyingSpec, ...]] = V1_INDICES


# ────────────────────────────────────────────────────────────────────────
# Fast lookup sets — O(1) membership used by the scope filter
# ────────────────────────────────────────────────────────────────────────


def _build_index_symbols() -> frozenset[tuple[Exchange, str]]:
    """Return frozenset of (exchange, trading_symbol) tuples for the Track 1 universe."""
    return frozenset((u.exchange, u.underlying_symbol) for u in V1_INDICES)


#: ``(exchange, symbol)`` pairs for the 3 named spot indexes.
INDEX_UNDERLYINGS: Final[frozenset[tuple[Exchange, str]]] = _build_index_symbols()


#: Backwards-compat alias.
ALLOWED_UNDERLYINGS: Final[frozenset[tuple[Exchange, str]]] = INDEX_UNDERLYINGS


# ────────────────────────────────────────────────────────────────────────
# Scope classification
# ────────────────────────────────────────────────────────────────────────


def is_index_underlying(exchange: Exchange, underlying_symbol: str) -> bool:
    """Return True if ``(exchange, underlying_symbol)`` is one of the 3 named indexes."""
    return (exchange, underlying_symbol) in INDEX_UNDERLYINGS


def is_in_scope(instrument: Instrument) -> bool:
    """Return True if *instrument* belongs to the Track 1 spot-index universe.

    Accept rule (only one)
    ----------------------
    ``instrument_type == IDX`` AND ``(exchange, trading_symbol)`` is in
    :data:`INDEX_UNDERLYINGS`. Everything else (cash equity, derivatives,
    non-Track-1 indices, commodities) is rejected.
    """
    if instrument.instrument == InstrumentType.IDX:
        return (instrument.exch_id, instrument.symbol_name) in INDEX_UNDERLYINGS
    return False


def classify_underlying_kind(instrument: Instrument) -> str:
    """Return ``"index"`` for a Track 1 in-scope instrument.

    Track 1 contains only indexes — there are no stocks or derivatives.

    Raises
    ------
    ValueError
        If called on an instrument that is not an in-scope Track 1 index.
    """
    if not is_in_scope(instrument):
        raise ValueError(
            f"{instrument.symbol_name}: not in Track 1 scope — "
            f"only NIFTY, BANKNIFTY, SENSEX, INDIAVIX are accepted"
        )
    return "index"


# ────────────────────────────────────────────────────────────────────────
# Two-tier scope (DEC-FETCH-ALL-001) — FETCH tier vs BRUTE-FORCE tier
# ────────────────────────────────────────────────────────────────────────
#
# Per CLAUDE.md §3 + DEC-FETCH-ALL-001 the project fetches a BROAD lake
# (all NSE F&O spot + futures + options + all NSE indices + BSE SENSEX)
# while the brute-force ENGINE stays on the 3 indexes. These two filters
# encode that split:
#
#   * ``is_in_fetch_scope``       — the broad LAKE tier (what we download)
#   * ``is_in_brute_force_scope`` — the narrow ENGINE tier (what we sweep)
#
# ``is_in_scope`` (above) remains the legacy 4-index filter and is
# unchanged for backwards compatibility.


#: Derivative instrument types (futures + both option sides).
_DERIVATIVE_TYPES: Final[frozenset[InstrumentType]] = frozenset(
    {InstrumentType.FUT, InstrumentType.CE, InstrumentType.PE}
)

#: The only BSE instrument in the fetch universe is the SENSEX index.
_BSE_FETCH_INDEX_SYMBOL: Final[str] = "SENSEX"


#: ``(exchange, symbol)`` pairs for the 3 brute-forced indexes (excludes
#: India VIX, which is reference-only and never swept).
BRUTE_FORCE_INDEX_UNDERLYINGS: Final[frozenset[tuple[Exchange, str]]] = frozenset(
    (u.exchange, u.underlying_symbol) for u in BRUTE_FORCE_INDICES
)


def is_in_brute_force_scope(instrument: Instrument) -> bool:
    """Return True if *instrument* is one of the 3 brute-forced indexes.

    The brute-force ENGINE tier: NIFTY 50, BANKNIFTY (NSE) + SENSEX (BSE).
    India VIX is excluded (reference-only). Everything else — stocks,
    futures, options, other indices — is rejected from the engine even
    though it may live in the fetch lake.
    """
    return (
        instrument.instrument == InstrumentType.IDX
        and (instrument.exch_id, instrument.symbol_name) in BRUTE_FORCE_INDEX_UNDERLYINGS
    )


#: ``(exchange, symbol)`` pairs for the reference-only indexes (today: India
#: VIX). Fetched + stamped on trades, but NEVER brute-forced.
REFERENCE_ONLY_INDEX_UNDERLYINGS: Final[frozenset[tuple[Exchange, str]]] = frozenset(
    (u.exchange, u.underlying_symbol) for u in REFERENCE_ONLY_INDICES
)


def is_reference_only(instrument: Instrument) -> bool:
    """Return True if *instrument* is admitted to the lake as REFERENCE_ONLY.

    DEC-NIFTY-TOTAL-MARKET-REFTAG-001 (Stage 2b PR-2): the explicit
    REFERENCE_ONLY tier — fetched + stored for context, but NEVER part of the
    brute-force engine or the 52-condition vocabulary. Today this is exactly
    India VIX (the only ``UnderlyingSpec`` with ``reference_only=True``); the
    later Stage-2b sub-PRs widen it to the admitted non-F&O NIFTY-Total-Market
    constituents. This is the single, unified encoding of "reference-only" — it
    persists onto the ``instruments.reference_only`` column so VIX and the
    future constituent equities share ONE mechanism (the Stage-2b hostile
    review's P0 recommendation), instead of VIX's ``UnderlyingSpec`` flag
    living apart from a separate stock tag.

    Invariant: ``is_reference_only`` and :func:`is_in_brute_force_scope` are
    mutually exclusive — a reference-only row is never swept (locked by the
    engine-isolation property test, DEC-ENGINE-ISOLATION-INVARIANT-001).
    """
    return (
        instrument.instrument == InstrumentType.IDX
        and (instrument.exch_id, instrument.symbol_name) in REFERENCE_ONLY_INDEX_UNDERLYINGS
    )


def is_option_pullable(instrument: Instrument, *, reference_only_ids: frozenset[int]) -> bool:
    """Return True iff *instrument* has a pullable option chain (DEC-OPTIONS-PULL-GUARD-001).

    The operator rule: an option chain may be pulled ONLY for genuinely
    F&O-bearing underlyings. It is BLOCKED for a **REFERENCE_ONLY** instrument —
    NIFTY Total Market (a reference index with no options of its own), its non-F&O
    constituents, and India VIX — so that accidentally selecting NTM or one of its
    reference-only stocks can never start an options pull.

    This relies on the lake's ``reference_only`` tag being CORRECT: an F&O-eligible
    stock is NOT tagged reference_only even when it is also an NTM constituent
    (``instrument_refresh._is_reference`` excludes the F&O-eligible set,
    DEC-OPTIONS-PULL-GUARD-001), so such a stock keeps its options. Every persisted
    non-reference equity is therefore F&O-eligible (the lean store only admits EQ
    rows that are F&O-eligible OR reference), so ``not reference_only`` is exactly
    "has an option chain" for stocks; a non-reference index keeps its pre-existing
    pullable behaviour.

    Parameters
    ----------
    instrument
        The candidate underlying.
    reference_only_ids
        The ``SECURITY_ID`` set tagged ``reference_only=TRUE`` in the lake
        (``store.reference_only_security_ids``) — NTM + its non-F&O constituents
        + India VIX.
    """
    return instrument.security_id not in reference_only_ids


def build_fno_eligible_underlyings(
    instruments: Iterable[Instrument],
) -> frozenset[tuple[Exchange, str]]:
    """Return the set of ``(exchange, underlying_symbol)`` that have NSE F&O.

    A cash equity is "F&O-eligible" iff at least one NSE futures or option
    contract names it as the underlying. This set is derived by scanning
    the full instrument master once, then passed to
    :func:`is_in_fetch_scope` so equity rows can be admitted only when a
    derivative on the same underlying exists.
    """
    eligible: set[tuple[Exchange, str]] = set()
    for inst in instruments:
        underlying = inst.underlying_symbol
        if inst.is_derivative and inst.exch_id == Exchange.NSE and underlying is not None:
            eligible.add((inst.exch_id, underlying))
    return frozenset(eligible)


#: One underlying's contract economics derived from its derivative legs:
#: ``(lot_size, tick_size, freeze_quantity)``.
UnderlyingEconomics = tuple[int, Decimal, int | None]


def build_underlying_economics(
    instruments: Iterable[Instrument],
) -> dict[tuple[Exchange, str], UnderlyingEconomics]:
    """Map ``(exchange, underlying_symbol)`` to leg-derived contract economics.

    In the Dhan instrument master, IDX and EQ rows carry BLANK / placeholder
    economics — ``lot_size`` defaults to ``1`` (or ``0`` for indices),
    ``freeze_quantity`` is absent. The REAL ``lot_size`` / ``tick_size`` /
    ``freeze_quantity`` for an underlying live only on its derivative legs
    (e.g. ``RELIANCE`` options carry ``lot_size=500``; NIFTY options carry
    ``lot_size=75``). The lean ingest already parses every FUT / CE / PE leg
    in-memory to compute F&O-eligibility
    (:func:`build_fno_eligible_underlyings`) before discarding them; this
    helper rides that same single pass to also harvest the economics so each
    persisted IDX / EQ underlying can be back-filled with its real values
    (DEC-UNDERLYING-ECONOMICS-001).

    Rules
    -----
    * Only derivative legs (FUT / CE / PE) contribute — they are the rows that
      carry the authoritative economics. Non-derivative rows are ignored.
    * A leg with no ``underlying_symbol`` is skipped (derivatives always have
      one in a well-formed master, but the guard keeps this pure + total).
    * The FIRST leg seen for a given ``(exchange, underlying_symbol)`` wins, so
      the result is deterministic and order-stable for any fixed input ordering.

    Parameters
    ----------
    instruments
        The fully parsed instrument set (the same iterable fed to
        :func:`build_fno_eligible_underlyings`), including the derivative legs.

    Returns
    -------
    dict[tuple[Exchange, str], UnderlyingEconomics]
        ``{(exchange, underlying_symbol): (lot_size, tick_size,
        freeze_quantity)}`` for every underlying that has at least one
        derivative leg. Underlyings with no legs (e.g. a non-F&O index such
        as NIFTY100) are absent — callers keep the master row's own values.
    """
    economics: dict[tuple[Exchange, str], UnderlyingEconomics] = {}
    for inst in instruments:
        underlying = inst.underlying_symbol
        if not inst.is_derivative or underlying is None:
            continue
        key = (inst.exch_id, underlying)
        if key in economics:
            continue
        economics[key] = (inst.lot_size, inst.tick_size, inst.sm_freeze_qty)
    return economics


def derive_lake_underlyings(
    instruments: Iterable[Instrument],
) -> list[tuple[str, str]]:
    """Return ``(underlying_symbol, exchange)`` pairs to pull the lake for.

    The lake-pull scope fix (DEC-COVERAGE-LEDGER-001, Part A): the live
    universe ingest must fan expiries/contracts out over **every** lake
    underlying — all ~25 indices PLUS every F&O-eligible single stock (per
    DEC-P0-063) — not just the ~25 indices. The caller passes the IDX + EQ
    candidate rows (from
    :meth:`~brutex.data.instrument_store.InstrumentStore.list_index_equity_instruments`),
    and this projects each to the ``(symbol, exchange)`` pair the Dhan options
    rolling fan-out
    (:func:`~brutex.adapters.dhan.option_universe_ingest.run_dhan_options_ingest`)
    expects.

    The brute-force ENGINE is UNAFFECTED — it still sweeps only the 3
    indices; this only widens the LAKE pull.

    Rules
    -----
    * The symbol is ``underlying_symbol`` when present, else
      ``trading_symbol`` (IDX rows carry no ``underlying_symbol``; an EQ row
      may carry its own as ``underlying_symbol`` or only as
      ``trading_symbol``).
    * The exchange is the row's ``exchange.value`` token (e.g. ``"NSE"``).
    * Duplicates are collapsed and the result is sorted for determinism, so
      a resumable re-pull iterates a stable order.

    Parameters
    ----------
    instruments
        The IDX + EQ candidate rows (never the FUT/CE/PE legs).

    Returns
    -------
    list[tuple[str, str]]
        Deterministic, de-duplicated ``(symbol, exchange)`` pairs.
    """
    pairs: set[tuple[str, str]] = set()
    for inst in instruments:
        symbol = inst.underlying_symbol or inst.symbol_name
        pairs.add((symbol, inst.exch_id.value))
    return sorted(pairs)


def is_universe_underlying(
    instrument: Instrument,
    fno_eligible_underlyings: frozenset[tuple[Exchange, str]],
    reference_security_ids: frozenset[int] = frozenset(),
) -> bool:
    """Return True if *instrument* is a UNIVERSE underlying worth persisting.

    The lean-ingest predicate (DEC-INSTRUMENT-UNIVERSE-FAST-001): the
    ``instruments`` table stores ONLY the unique universe underlyings —
    every admitted index plus every F&O-eligible single-stock equity — and
    NEVER the ~tens-of-thousands of FUT / CE / PE option/future legs. The
    legs are still parsed in-memory (they are how
    :func:`build_fno_eligible_underlyings` discovers which equities are
    F&O-eligible) and then DISCARDED rather than upserted; their economics
    are recoverable on demand via ``get_contracts`` (CONTRACTS surface).

    The accept rule is the union of the two existing
    :func:`is_in_fetch_scope` clauses that admit a *non-derivative* row, and
    mirrors them exactly so the universe stays single-source (CLAUDE.md §11
    COMMON):

    * **IDX** — admitted by the same index rule as :func:`is_in_fetch_scope`:
      every NSE index; from BSE only SENSEX.
    * **EQ** — admitted iff it is an F&O-eligible NSE underlying
      (``(exchange, trading_symbol)`` in *fno_eligible_underlyings*) **OR** an
      admitted REFERENCE_ONLY constituent (``security_id`` in
      *reference_security_ids* — matched by id because a non-F&O equity's
      ``symbol_name`` is the long company name, DEC-NIFTY-TOTAL-MARKET-ADMIT-001).
      *reference_security_ids* defaults empty → identical to the prior
      F&O-only equity clause.

    Every derivative leg (FUT / CE / PE) returns ``False`` here — that is
    the whole point of the lean ingest — even though it is in the broad
    *fetch* scope.

    Parameters
    ----------
    instrument
        The candidate instrument-master row.
    fno_eligible_underlyings
        The set of NSE F&O-eligible ``(exchange, symbol)`` pairs, typically
        built once via :func:`build_fno_eligible_underlyings`.

    Returns
    -------
    bool
        ``True`` iff *instrument* is an admitted index or an F&O-eligible
        equity; ``False`` for every derivative leg or out-of-universe row.
    """
    instrument_type = instrument.instrument
    if instrument_type == InstrumentType.IDX:
        # Same IDX rule as is_in_fetch_scope: all NSE indices; BSE only SENSEX.
        return (
            instrument.exch_id == Exchange.NSE or instrument.symbol_name == _BSE_FETCH_INDEX_SYMBOL
        )
    if instrument_type == InstrumentType.EQ:
        # An NSE equity is a universe underlying if it is EITHER F&O-eligible
        # (by ticker) OR an admitted REFERENCE_ONLY constituent (by SECURITY_ID
        # — its SYMBOL_NAME is the long company name, not the ticker, so the
        # ticker key cannot match; DEC-NIFTY-TOTAL-MARKET-ADMIT-001). The
        # reference set defaults empty → byte-identical to the F&O-only rule.
        return instrument.exch_id == Exchange.NSE and (
            (instrument.exch_id, instrument.symbol_name) in fno_eligible_underlyings
            or instrument.security_id in reference_security_ids
        )
    # Every derivative leg (FUT / CE / PE) — parsed for eligibility, never stored.
    return False


def is_in_fetch_scope(
    instrument: Instrument,
    fno_eligible_underlyings: frozenset[tuple[Exchange, str]],
) -> bool:
    """Return True if *instrument* belongs to the broad fetch / lake tier.

    The fetch tier (DEC-FETCH-ALL-001) admits, by instrument type:

    * **IDX** — every NSE index; from BSE only SENSEX.
    * **FUT / CE / PE** — every NSE futures + option contract (no BSE
      derivatives — BSE contributes only the SENSEX index).
    * **EQ** — an NSE cash equity only if it is an F&O-eligible underlying
      (i.e. ``(NSE, trading_symbol)`` is in *fno_eligible_underlyings*,
      typically built via :func:`build_fno_eligible_underlyings`).

    Parameters
    ----------
    instrument
        The candidate instrument-master row.
    fno_eligible_underlyings
        The set of NSE F&O-eligible ``(exchange, symbol)`` pairs.
    """
    instrument_type = instrument.instrument
    exchange = instrument.exch_id
    if instrument_type == InstrumentType.IDX:
        # All NSE indices; from BSE only SENSEX.
        return exchange == Exchange.NSE or instrument.symbol_name == _BSE_FETCH_INDEX_SYMBOL
    if instrument_type in _DERIVATIVE_TYPES:
        # All NSE futures + options; no BSE derivatives.
        return exchange == Exchange.NSE
    # Remaining type is cash equity (EQ): in scope only if it is an
    # F&O-eligible NSE underlying.
    return (
        exchange == Exchange.NSE and (exchange, instrument.symbol_name) in fno_eligible_underlyings
    )


__all__ = [
    "ALLOWED_UNDERLYINGS",
    "BRUTE_FORCE_INDEX_UNDERLYINGS",
    "BRUTE_FORCE_INDICES",
    "INDEX_UNDERLYINGS",
    "REFERENCE_ONLY_INDICES",
    "V1_INDICES",
    "V1_UNDERLYINGS",
    "UnderlyingEconomics",
    "UnderlyingSpec",
    "build_fno_eligible_underlyings",
    "build_underlying_economics",
    "classify_underlying_kind",
    "derive_lake_underlyings",
    "is_in_brute_force_scope",
    "is_in_fetch_scope",
    "is_in_scope",
    "is_index_underlying",
    "is_option_pullable",
    "is_reference_only",
    "is_universe_underlying",
]
