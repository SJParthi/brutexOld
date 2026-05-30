"""BruteX Track 1 universe — Indian spot indexes + India VIX.

**STATUS (2026-05-26):** universe definitions are vendor-agnostic. Post
DEC-VENDOR-PIVOT-001 the Groww adapter consumes these definitions
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
    if instrument.instrument_type == InstrumentType.IDX:
        return (instrument.exchange, instrument.trading_symbol) in INDEX_UNDERLYINGS
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
            f"{instrument.trading_symbol}: not in Track 1 scope — "
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
        instrument.instrument_type == InstrumentType.IDX
        and (instrument.exchange, instrument.trading_symbol) in BRUTE_FORCE_INDEX_UNDERLYINGS
    )


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
        if inst.is_derivative and inst.exchange == Exchange.NSE and underlying is not None:
            eligible.add((inst.exchange, underlying))
    return frozenset(eligible)


#: One underlying's contract economics derived from its derivative legs:
#: ``(lot_size, tick_size, freeze_quantity)``.
UnderlyingEconomics = tuple[int, Decimal, int | None]


def build_underlying_economics(
    instruments: Iterable[Instrument],
) -> dict[tuple[Exchange, str], UnderlyingEconomics]:
    """Map ``(exchange, underlying_symbol)`` to leg-derived contract economics.

    In the Groww instrument master, IDX and EQ rows carry BLANK / placeholder
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
        key = (inst.exchange, underlying)
        if key in economics:
            continue
        economics[key] = (inst.lot_size, inst.tick_size, inst.freeze_quantity)
    return economics


def is_universe_underlying(
    instrument: Instrument,
    fno_eligible_underlyings: frozenset[tuple[Exchange, str]],
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
    * **EQ** — admitted iff it is an F&O-eligible NSE underlying, i.e.
      ``(exchange, trading_symbol)`` is in *fno_eligible_underlyings*
      (identical to the equity clause of :func:`is_in_fetch_scope`).

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
    instrument_type = instrument.instrument_type
    if instrument_type == InstrumentType.IDX:
        # Same IDX rule as is_in_fetch_scope: all NSE indices; BSE only SENSEX.
        return (
            instrument.exchange == Exchange.NSE
            or instrument.trading_symbol == _BSE_FETCH_INDEX_SYMBOL
        )
    if instrument_type == InstrumentType.EQ:
        # Same EQ rule as is_in_fetch_scope: an F&O-eligible NSE underlying.
        return (
            instrument.exchange == Exchange.NSE
            and (instrument.exchange, instrument.trading_symbol) in fno_eligible_underlyings
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
    instrument_type = instrument.instrument_type
    exchange = instrument.exchange
    if instrument_type == InstrumentType.IDX:
        # All NSE indices; from BSE only SENSEX.
        return exchange == Exchange.NSE or instrument.trading_symbol == _BSE_FETCH_INDEX_SYMBOL
    if instrument_type in _DERIVATIVE_TYPES:
        # All NSE futures + options; no BSE derivatives.
        return exchange == Exchange.NSE
    # Remaining type is cash equity (EQ): in scope only if it is an
    # F&O-eligible NSE underlying.
    return (
        exchange == Exchange.NSE
        and (exchange, instrument.trading_symbol) in fno_eligible_underlyings
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
    "is_in_brute_force_scope",
    "is_in_fetch_scope",
    "is_in_scope",
    "is_index_underlying",
    "is_universe_underlying",
]
