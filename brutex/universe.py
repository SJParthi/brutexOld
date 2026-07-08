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

import re
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Literal

from brutex.data.fno_symbol import FUTURE_TYPE, parse_fno_symbol
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


def is_index_instrument(instrument: Instrument) -> bool:
    """Return True if *instrument* is a spot INDEX by TYPE (``instrument_type == IDX``).

    The asset-class / DISPLAY predicate (DEC-COMPLETENESS-INDEX-CATEGORY-001) — distinct
    from :func:`is_in_brute_force_scope`, which is the ENGINE-tier test (only the 3
    brute-forced indexes). For the coverage/completeness "indices vs equities" split EVERY
    index is an index, whether or not it is swept: NIFTY / BANKNIFTY / SENSEX (brute-forced)
    AND e.g. FINNIFTY / India VIX (NOT brute-forced) are all ``IDX``. The completeness route
    classifies reference-only first (:func:`is_reference_only` takes precedence), so India
    VIX still lands in REFERENCE — this predicate never mislabels it. O(1) — one enum compare.

    Why it exists: the prior /completeness mapping used :func:`is_in_brute_force_scope` as
    the index test, so a real index that is not brute-forced (FINNIFTY) was wrongly bucketed
    "equity (cash)" — inconsistent with the /universe page (which uses the true type). This
    is the single by-type index predicate both surfaces now share (CLAUDE.md §11 COMMON).
    """
    return instrument.instrument == InstrumentType.IDX


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


# ────────────────────────────────────────────────────────────────────────
# Engine sweep-entry scope law (DEC-ENGINE-SCOPE-GUARD-001)
# ────────────────────────────────────────────────────────────────────────
#
# The ONE canonical predicate every sweep-launch surface shares (CLAUDE.md
# §11 COMMON): the CLI (``cmd_sweep_all`` / ``cmd_sweep``), the dashboard
# sweep route, and the ``SweepGridConfig.__post_init__`` hard invariant all
# delegate HERE — never a re-typed copy. Static sets + a regex, ZERO I/O
# (deliberately NO ``InstrumentStore`` dependency, so there is no "master
# not loaded" failure mode). Deny-by-default: everything not explicitly
# allowed is refused.


#: The neutral lake SEGMENT partition label the 3 spot indexes live under
#: (``brutex.adapters.groww.instruments`` maps the vendor ``IDX``/``CASH``
#: row to this label; CLAUDE.md §6 "Backtesting segments").
_ENGINE_SWEEP_SPOT_SEGMENT: Final[str] = "INDEX"

#: The exact ``(exchange, segment, symbol)`` lake coordinates the brute-force
#: ENGINE may sweep as SPOT (CLAUDE.md §2 Rule 3) — DERIVED from
#: :data:`BRUTE_FORCE_INDEX_UNDERLYINGS` (never a hand-typed copy, so a
#: universe change propagates automatically).
ENGINE_SWEEP_SPOT_TRIPLES: Final[frozenset[tuple[str, str, str]]] = frozenset(
    (exchange.value, _ENGINE_SWEEP_SPOT_SEGMENT, symbol)
    for exchange, symbol in BRUTE_FORCE_INDEX_UNDERLYINGS
)

#: The bare spot symbols of the 3 swept indexes (the symbol-only tier of the
#: law — coordinate correctness is the caller-facing
#: :func:`classify_engine_sweep_target`'s job).
_ENGINE_SWEEP_SPOT_SYMBOLS: Final[frozenset[str]] = frozenset(
    symbol for _, symbol in BRUTE_FORCE_INDEX_UNDERLYINGS
)

#: The reference-only bare symbols (today: India VIX) — named in refusals so
#: the operator sees the SPECIFIC "REFERENCE_ONLY" law, not a generic no.
_REFERENCE_ONLY_SYMBOLS: Final[frozenset[str]] = frozenset(
    symbol for _, symbol in REFERENCE_ONLY_INDEX_UNDERLYINGS
)

#: The PINNED engine addressing label for the ADR-0012 NIFTY-futures track
#: (DEC-FUTURES-ADDRESSING-PIN-001, supersedes the #1178 dual-acceptance
#: envelope): ONE roll-aware series ALIAS — never a lake dir itself — resolved
#: at launch into per-contract ``bars/NSE/FNO/NSE-NIFTY-<DDMmmYY>-FUT``
#: partitions by :mod:`brutex.data.futures_series` (which imports THIS
#: constant; this module deliberately does NOT import that one — it lazily
#: pulls the trading_core wheel). ``parse_fno_symbol`` returns ``None`` for it
#: (3 hyphen tokens < the 4-token future minimum), so no vendor contract
#: symbol can ever collide with the alias (test-pinned).
NIFTY_FUTURES_SERIES_SYMBOL: Final[str] = "NSE-NIFTY-FUT"

#: The RETIRED ``FuturesContract.series_key`` shape (``NIFTY-FUT-YYYY-MM-DD``,
#: :meth:`brutex.data.futures_roll.FuturesContract.series_key`) — one of the
#: two pre-launch addressing envelopes DEC-FUTURES-ADDRESSING-PIN-001 retired
#: in favour of :data:`NIFTY_FUTURES_SERIES_SYMBOL`. Kept ONLY as the
#: did-you-mean refusal-hint matcher in
#: :func:`_looks_like_retired_futures_shape` (never an admit path). No
#: ``futures_roll`` import here (that module pulls the trading_core wheel); a
#: drift-pin test asserts this regex still fullmatches a real
#: ``FuturesContract(...).series_key`` so the hint can never rot apart from
#: the real retired shape.
_RETIRED_NIFTY_FUTURES_SERIES_RE: Final[re.Pattern[str]] = re.compile(
    r"^NIFTY-FUT-\d{4}-\d{2}-\d{2}$"
)

#: The NIFTY-futures underlying symbol (the ADR-0012 authorization is
#: exactly this one underlying).
_NIFTY_UNDERLYING: Final[str] = "NIFTY"

#: The lake exchange a NIFTY futures contract trades on (NSE — a foreign
#: exchange token embedded in a parsed contract symbol is refused).
_NIFTY_FUTURES_EXCHANGE: Final[str] = Exchange.NSE.value

#: The lake SEGMENT partition label F&O contract candles live under
#: (``bars/<exch>/FNO/…`` — CLAUDE.md §6 "F&O candle lake").
_ENGINE_SWEEP_FNO_SEGMENT: Final[str] = "FNO"


def is_engine_sweep_symbol(symbol: str) -> bool:
    """Return True iff *symbol* is an engine-sweepable instrument SYMBOL.

    The symbol-only tier of the DEC-ENGINE-SCOPE-GUARD-001 law (consumed by
    the ``SweepGridConfig.__post_init__`` hard invariant, which sees only
    bare symbols — coordinate correctness is
    :func:`classify_engine_sweep_target`'s job). Accepted, deny-by-default:

    * a bare spot symbol of the 3 brute-forced indexes (``NIFTY`` /
      ``BANKNIFTY`` / ``SENSEX`` — CLAUDE.md §2 Rule 3), OR
    * the PINNED NIFTY-futures series alias ``NSE-NIFTY-FUT`` (ADR-0012 /
      DEC-FUTURES-ADDRESSING-PIN-001 — the final addressing shape; the two
      pre-launch shapes ``NIFTY-FUT-YYYY-MM-DD`` and
      ``NSE-NIFTY-<DDMmmYY>-FUT`` are RETIRED —
      :func:`classify_engine_sweep_target` refuses them with a did-you-mean
      hint).

    Everything else — India VIX, the ~212 F&O stocks, NTM constituents,
    BANKNIFTY/SENSEX futures, every option, every per-contract futures
    symbol — is False. Exact-case (lake keys are case-exact). O(1): one
    frozenset probe + one string compare.
    """
    if symbol in _ENGINE_SWEEP_SPOT_SYMBOLS:
        return True
    return symbol == NIFTY_FUTURES_SERIES_SYMBOL


def _looks_like_retired_futures_shape(symbol: str) -> bool:
    """Hint matcher for the two RETIRED pre-launch NIFTY-futures shapes.

    DEC-FUTURES-ADDRESSING-PIN-001 refusal-hint ONLY (never an admit path):
    matches the retired ``FuturesContract.series_key`` form
    (``NIFTY-FUT-YYYY-MM-DD``) or a parseable per-contract NIFTY futures
    ``groww_symbol`` (``NSE-NIFTY-<DDMmmYY>-FUT``), so
    :func:`classify_engine_sweep_target` can point the operator at the ONE
    pinned series alias. Non-NIFTY futures (``NSE-BANKNIFTY-…-FUT``) do NOT
    match — they keep the generic out-of-scope refusal.
    """
    if _RETIRED_NIFTY_FUTURES_SERIES_RE.fullmatch(symbol) is not None:
        return True
    parts = parse_fno_symbol(symbol)
    return (
        parts is not None
        and parts.exchange == _NIFTY_FUTURES_EXCHANGE
        and parts.underlying == _NIFTY_UNDERLYING
        and parts.instrument_type == FUTURE_TYPE
    )


@dataclass(frozen=True, slots=True)
class EngineSweepVerdict:
    """One symbol's classification under the engine sweep-entry scope law.

    Attributes
    ----------
    allowed
        Whether the ``(exchange, segment, symbol)`` coordinate may launch.
    tier
        ``"spot_index"`` (one of the 3 §2 Rule 3 triples), ``"nifty_future"``
        (the ADR-0012 track), or ``"refused"``.
    reason
        The human-readable law citation (allowed) or refusal ground.
    """

    allowed: bool
    tier: Literal["spot_index", "nifty_future", "refused"]
    reason: str


def _expected_spot_coordinates(symbol: str) -> str:
    """Render the expected ``exchange/segment`` for an in-scope spot *symbol*."""
    coords = sorted(
        f"{exchange}/{segment}"
        for exchange, segment, spot_symbol in ENGINE_SWEEP_SPOT_TRIPLES
        if spot_symbol == symbol
    )
    return ", ".join(coords)


def classify_engine_sweep_target(*, exchange: str, segment: str, symbol: str) -> EngineSweepVerdict:
    """Classify one sweep-launch ``(exchange, segment, symbol)`` coordinate.

    The coordinate tier of the DEC-ENGINE-SCOPE-GUARD-001 law (Layer 2 —
    consumed by :func:`brutex.engine.sweep_scope.check_sweep_scope`). Pure +
    zero-I/O: static sets derived from :data:`BRUTE_FORCE_INDEX_UNDERLYINGS`,
    never an ``InstrumentStore`` read. Exact-case comparison (lake partition
    keys are case-exact); a lower-cased in-scope symbol gets a
    "did you mean" hint instead of a silent generic refusal.
    """
    if (exchange, segment, symbol) in ENGINE_SWEEP_SPOT_TRIPLES:
        return EngineSweepVerdict(
            allowed=True,
            tier="spot_index",
            reason=(
                f"{exchange}/{segment}/{symbol} is one of the 3 brute-force spot "
                "index triples (CLAUDE.md §2 Rule 3)"
            ),
        )
    if is_engine_sweep_symbol(symbol) and symbol not in _ENGINE_SWEEP_SPOT_SYMBOLS:
        # The NIFTY-futures series alias: allowed ONLY under the NSE/FNO lake
        # coordinates (DEC-FUTURES-ADDRESSING-PIN-001).
        if exchange == _NIFTY_FUTURES_EXCHANGE and segment == _ENGINE_SWEEP_FNO_SEGMENT:
            return EngineSweepVerdict(
                allowed=True,
                tier="nifty_future",
                reason=(
                    f"{symbol} is the pinned NIFTY-futures series alias — the "
                    "operator-authorized dynamic-strategy track (ADR-0012 / "
                    "DEC-FUTURES-ADDRESSING-PIN-001)"
                ),
            )
        return EngineSweepVerdict(
            allowed=False,
            tier="refused",
            reason=(
                # {symbol!r} + {exchange!r}/{segment!r} in every refusal
                # reason: all three are operator-typed (may embed \n/ESC) and
                # the reason is echoed to stderr + logs — never let them
                # forge lines (#907/#919 repr-naming convention; round-8
                # finding 2 extended the round-4 symbol fix to the pair).
                f"{symbol!r} is the NIFTY-futures series alias but "
                f"{exchange!r}/{segment!r} is not its lake coordinate — expected "
                f"{_NIFTY_FUTURES_EXCHANGE}/{_ENGINE_SWEEP_FNO_SEGMENT} (ADR-0012)"
            ),
        )
    if symbol in _ENGINE_SWEEP_SPOT_SYMBOLS:
        return EngineSweepVerdict(
            allowed=False,
            tier="refused",
            reason=(
                f"{symbol!r} is an engine index but {exchange!r}/{segment!r} is not "
                f"its lake coordinate — expected {_expected_spot_coordinates(symbol)} "
                "(CLAUDE.md §2 Rule 3)"
            ),
        )
    if symbol in _REFERENCE_ONLY_SYMBOLS:
        return EngineSweepVerdict(
            allowed=False,
            tier="refused",
            reason=(
                f"{symbol} is REFERENCE_ONLY (CLAUDE.md §2 Rule 3) — fetched + "
                "stamped on trades, NEVER brute-forced"
            ),
        )
    upper = symbol.upper()
    if upper != symbol and (
        upper in _ENGINE_SWEEP_SPOT_SYMBOLS
        or upper in _REFERENCE_ONLY_SYMBOLS
        # The futures series alias is in-scope too (review round-2 finding 3):
        # a lower-cased `nse-nifty-fut` gets the same hint-never-admit case
        # pointer instead of the generic out-of-scope refusal.
        or upper == NIFTY_FUTURES_SERIES_SYMBOL
    ):
        return EngineSweepVerdict(
            allowed=False,
            tier="refused",
            reason=(
                f"{symbol!r} is not an engine instrument — lake symbols are "
                f"case-exact; did you mean {upper!r}?"
            ),
        )
    # RETIRED-shape did-you-mean hint (DEC-FUTURES-ADDRESSING-PIN-001):
    # hint-never-admit, mirroring the case-hint branch above — a pre-launch
    # futures addressing form is refused pointing at the ONE pinned alias.
    if _looks_like_retired_futures_shape(symbol):
        return EngineSweepVerdict(
            allowed=False,
            tier="refused",
            reason=(
                f"{symbol!r} is a RETIRED per-contract NIFTY-futures addressing form — "
                "the ADR-0012 track is addressed by the ONE series alias "
                f"{NIFTY_FUTURES_SERIES_SYMBOL!r} (DEC-FUTURES-ADDRESSING-PIN-001), "
                "resolved roll-aware into per-contract lake partitions; "
                "did you mean that?"
            ),
        )
    return EngineSweepVerdict(
        allowed=False,
        tier="refused",
        reason=(
            # {symbol!r}: this generic branch is the one reached by an ARBITRARY
            # operator-typed name (\n/ESC included) — repr-quote it so the
            # echoed refusal can never forge stderr/log lines (round-4 finding 3).
            f"{symbol!r} is outside the brute-force engine scope — the engine sweeps "
            "only NIFTY/BANKNIFTY/SENSEX spot (CLAUDE.md §2 Rule 3) plus the "
            f"NIFTY-futures series alias {NIFTY_FUTURES_SERIES_SYMBOL!r} (ADR-0012); "
            "F&O stocks, NTM constituents and options are lake/REFERENCE_ONLY"
        ),
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
    rows that are F&O-eligible — by ticker OR by token-join,
    DEC-FNO-EQ-TOKEN-JOIN-PERSIST-001 — OR reference), so ``not reference_only`` is
    exactly "has an option chain" for stocks. For an INDEX the same holds because the load
    now also tags every **spot-only index** (one with no listed options of its own
    that is not a brute-force index — :func:`is_spot_only_index`) reference_only
    (DEC-NON-OPTION-INDEX-REFTAG-001), so a sectoral / broad-market index (NIFTY IT
    / 100 / TOTAL MARKET) + India VIX are correctly NOT option-pullable, while the
    options-bearing indices (NIFTY / BANKNIFTY / FINNIFTY …) keep their chain.

    Parameters
    ----------
    instrument
        The candidate underlying.
    reference_only_ids
        The ``SECURITY_ID`` set tagged ``reference_only=TRUE`` in the lake
        (``store.reference_only_security_ids``) — NTM + its non-F&O constituents,
        India VIX, and every spot-only (non-option, non-brute-force) index.
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


def build_options_eligible_underlyings(
    instruments: Iterable[Instrument],
) -> frozenset[tuple[Exchange, str]]:
    """Return the ``(exchange, underlying_symbol)`` set that has tradeable OPTIONS.

    DEC-OPTIONS-ELIGIBLE-GUARD-001 — **pull-scope clause SUPERSEDED by
    DEC-FUTURES-PULL-ENABLE-001 (#871).** The OPTIONS-ONLY sibling of
    :func:`build_fno_eligible_underlyings`: an underlying is options-eligible iff at least one
    vendor **option** leg (``CE`` / ``PE`` — i.e. an ``OPTIDX`` index option or an
    ``OPTSTK`` stock option) names it as the underlying; a ``FUT`` leg does NOT qualify it.

    **This set is the options-eligibility / reference-tag DETECTOR only — NOT a pull gate, and
    it does NOT mean "futures are never pulled".** FUTURES ARE pulled into the reference lake
    (DEC-GROWW-FUTURE-INGEST-001 #707, formalised by DEC-FUTURES-PULL-ENABLE-001 #871): the pull
    paths scope on the FUT-inclusive :func:`build_fno_eligible_underlyings` + the reference-only
    partition, never on this set. The original prior-vendor-era "we will NEVER pull futures — only
    options" decree (#602) was the prior vendor's options-CHAIN quota guard and is retired for
    pull scope. This is the AUTHORITATIVE, data-driven set — derived from the vendor's own
    instrument master in the SAME single pass the F&O-eligibility scan uses, so it auto-tracks
    NSE adding/removing an F&O name, never a hardcoded guess (Golden Rule #1). Its live consumer
    is the reference-only / OPT badge classification (`instrument_loader`), which keeps a
    mis-click from wasting quota on an options chain that does not exist (e.g. NIFTY 100 /
    INDIA VIX / a non-F&O equity).

    Cross-reference with the admitted ``INDEX`` vs ``EQ`` rows to split it into the
    **F&O indices** (the ~handful: NIFTY / BANKNIFTY / FINNIFTY / MIDCPNIFTY /
    NIFTY NEXT 50 …) and the **F&O stocks** (the NSE F&O equity list).
    """
    eligible: set[tuple[Exchange, str]] = set()
    for inst in instruments:
        underlying = inst.underlying_symbol
        if inst.is_option and inst.exch_id == Exchange.NSE and underlying is not None:
            eligible.add((inst.exch_id, underlying))
    return frozenset(eligible)


@dataclass(frozen=True, slots=True)
class OptionUnderlyingGap:
    """One NSE option ``underlying_symbol`` that has NO admitted spot-index row.

    DEC-FNO-INDEX-MATCH-DIAG-001 — the observe-only record of a *symbol
    mismatch*: an underlying that the vendor stamps on its ``CE`` / ``PE``
    legs but whose spelling does NOT exactly equal any admitted ``IDX``
    row's ``symbol_name``. Because :func:`is_spot_only_index` keys the
    OPT-vs-REF badge on that EXACT ``(exchange, symbol_name)`` match
    (DEC-NON-OPTION-INDEX-REFTAG-001), such an index is silently tagged
    REFERENCE_ONLY even though it genuinely has a listed option chain. This
    record SURFACES the real strings so the operator can see WHICH spelling
    differs — it never invents an alias (Golden Rule #1).

    Attributes
    ----------
    exchange
        The leg's exchange (always :attr:`Exchange.NSE` — the diagnostic
        only scans NSE option legs, mirroring
        :func:`build_options_eligible_underlyings`).
    underlying_symbol
        The exact ``underlying_symbol`` string the vendor stamped on the
        option leg (e.g. ``"NIFTY NEXT 50"``) — reported verbatim.
    matched_index_symbol_name
        ``None`` by construction: a gap is reported ONLY when no admitted
        ``IDX`` row's ``symbol_name`` equals *underlying_symbol* exactly.
        Kept as an explicit field (always ``None``) so the record's shape
        documents the contract — "this underlying matched no index spot row".
    nearest_index_symbol_name
        The admitted ``IDX`` ``symbol_name`` whose NORMALIZED form
        (:func:`normalize_underlying_symbol` — uppercase, spaces/``-``
        removed) equals *underlying_symbol*'s normalized form, else ``None``.
        A non-``None`` value means the mismatch is a pure CASE / SPACE
        difference (e.g. leg ``"NIFTY NEXT 50"`` vs IDX ``"NIFTYNEXT50"``);
        ``None`` means a genuinely different abbreviation (e.g. leg
        ``"MIDCPNIFTY"`` vs an IDX spelled ``"NIFTY MIDCAP SELECT"``), which
        normalization alone cannot bridge.
    """

    exchange: Exchange
    underlying_symbol: str
    matched_index_symbol_name: str | None
    nearest_index_symbol_name: str | None


def normalize_underlying_symbol(symbol: str) -> str:
    """Return *symbol* uppercased with spaces and ``-`` removed.

    A pure, total normalizer used ONLY to detect a CASE / SPACE mismatch
    between an option leg's ``underlying_symbol`` and an admitted index's
    ``symbol_name`` (DEC-FNO-INDEX-MATCH-DIAG-001). It is deliberately
    minimal — uppercase + strip the two separators the vendor varies
    (ASCII space and hyphen) — so it can flag ``"NIFTY NEXT 50"`` ≈
    ``"NIFTYNEXT50"`` WITHOUT ever bridging a genuine abbreviation
    (``"MIDCPNIFTY"`` ≠ ``"NIFTY MIDCAP SELECT"``). It NEVER encodes a
    per-index alias (Golden Rule #1); it only folds whitespace + case.

    Documented mirror (DEC-GREEKS-SPOTJOIN-STOCK-001):
    :func:`brutex.data.lake_keys.stock_drift_fold` EXACTLY mirrors this body —
    ``lake_keys`` is a pure stdlib leaf that cannot import this module, so the
    two sites carry this cross-reference and a mirror-pin unit test
    (``tests/unit/test_lake_keys.py::TestStockDriftFold``) enforces agreement.
    Change one, change both.

    Parameters
    ----------
    symbol
        The raw symbol string (an option ``underlying_symbol`` or an index
        ``symbol_name``).

    Returns
    -------
    str
        The uppercased symbol with every ASCII space and ``-`` removed.
    """
    return symbol.upper().replace(" ", "").replace("-", "")


def diagnose_unmatched_option_underlyings(
    instruments: Iterable[Instrument],
    index_alias_symbols: frozenset[tuple[Exchange, str]] | None = None,
) -> list[OptionUnderlyingGap]:
    """Report NSE option underlyings that look like an INDEX but match no IDX spot row.

    DEC-FNO-INDEX-MATCH-DIAG-001 — a pure, OBSERVE-ONLY diagnostic (it
    changes no classification). It surfaces the silent failure mode of
    :func:`is_spot_only_index`: a real F&O index is tagged REFERENCE_ONLY
    (loses its OPT badge) whenever its ``IDX`` row's ``symbol_name`` differs
    from the ``underlying_symbol`` the vendor stamps on its ``CE`` / ``PE``
    legs, because the OPT-vs-REF badge keys on an EXACT
    ``(exchange, symbol_name)`` match (DEC-NON-OPTION-INDEX-REFTAG-001).

    *index_alias_symbols* (DEC-FNO-INDEX-GROWW-SYMBOL-MATCH-001) is the set of
    ``(exchange, vendor_F&O_symbol)`` the loader harvests from each ``IDX``
    row's ``groww_symbol`` key (``"NSE-MIDCPNIFTY"`` → ``(NSE, "MIDCPNIFTY")``
    via :func:`~brutex.adapters.groww.instruments.index_symbol_of`). An
    underlying that the groww-symbol bridge ALREADY resolved to a spot row is
    NOT a real gap, so it is skipped here — the diagnostic stays HONEST and
    stops crying about indices the bridge fixed (it would otherwise log a
    permanent false ``count`` for MIDCPNIFTY / NIFTYNXT50 even after they were
    correctly badged OPT). ``None`` (the default) preserves the original
    symbol_name-only behaviour for any non-loader caller.

    For every distinct NSE option ``underlying_symbol`` in *instruments*:

    * If an admitted ``IDX`` row has ``symbol_name == underlying_symbol``
      (exact), the index is correctly matched → NOT a gap.
    * Else, if the underlying ALSO names an NSE ``EQ`` row's ``symbol_name``,
      it is a STOCK option (not an index) → NOT reported (the operator is
      hunting for *index* mismatches; a stock's own spot row is the EQ row).
    * Else it is an index-like underlying with NO exact spot row → reported
      as an :class:`OptionUnderlyingGap`. Its ``nearest_index_symbol_name``
      is the admitted ``IDX`` ``symbol_name`` whose
      :func:`normalize_underlying_symbol` form equals the underlying's
      normalized form (a pure CASE / SPACE mismatch), else ``None`` (a
      genuine abbreviation mismatch normalization cannot bridge).

    The function NEVER invents an alias (Golden Rule #1): it reports the real
    vendor strings + the normalization-only nearest match, and the caller
    fixes the data, not this diagnostic.

    Parameters
    ----------
    instruments
        The fully parsed instrument set (the same iterable fed to
        :func:`build_options_eligible_underlyings`), including the option
        legs and the IDX / EQ spot rows.

    Returns
    -------
    list[OptionUnderlyingGap]
        The index-like unmatched option underlyings, sorted deterministically
        by ``(exchange, underlying_symbol)``. Empty when every NSE option
        underlying either matches an IDX spot row exactly or is a known
        equity underlying.
    """
    index_symbol_names: set[str] = set()
    equity_symbol_names: set[str] = set()
    option_underlyings: set[str] = set()
    for inst in instruments:
        if inst.exch_id != Exchange.NSE:
            continue
        if inst.instrument == InstrumentType.IDX:
            index_symbol_names.add(inst.symbol_name)
        elif inst.instrument == InstrumentType.EQ:
            equity_symbol_names.add(inst.symbol_name)
        elif inst.is_option and inst.underlying_symbol is not None:
            option_underlyings.add(inst.underlying_symbol)

    # Pre-index the admitted IDX symbols by their normalized form so the
    # nearest-match lookup is O(1) per unmatched underlying (a normalized
    # form maps to ONE symbol_name; ties keep the first by sorted order so
    # the result is deterministic for any input ordering).
    normalized_index: dict[str, str] = {}
    for name in sorted(index_symbol_names):
        normalized_index.setdefault(normalize_underlying_symbol(name), name)

    aliases = index_alias_symbols if index_alias_symbols is not None else frozenset()
    gaps: list[OptionUnderlyingGap] = []
    for underlying in option_underlyings:
        if underlying in index_symbol_names or underlying in equity_symbol_names:
            continue
        # The groww-symbol bridge already resolved this underlying to a spot
        # IDX row (its verbose symbol_name differed, but its vendor F&O symbol
        # matched) → NOT a gap (DEC-FNO-INDEX-GROWW-SYMBOL-MATCH-001).
        if (Exchange.NSE, underlying) in aliases:
            continue
        nearest = normalized_index.get(normalize_underlying_symbol(underlying))
        gaps.append(
            OptionUnderlyingGap(
                exchange=Exchange.NSE,
                underlying_symbol=underlying,
                matched_index_symbol_name=None,
                nearest_index_symbol_name=nearest,
            )
        )
    return sorted(gaps, key=lambda gap: (gap.exchange.value, gap.underlying_symbol))


def is_spot_only_index(
    instrument: Instrument,
    options_eligible_underlyings: frozenset[tuple[Exchange, str]],
) -> bool:
    """Return True iff *instrument* is an index with NO tradable option chain.

    DEC-NON-OPTION-INDEX-REFTAG-001. An ``IDX`` row is **spot-only** — fetched +
    stored for context but with no listed options of its own — iff it is NEITHER

    * one of the 3 brute-force indices (NIFTY / BANKNIFTY / SENSEX — always
      individually tradable, never reference; :func:`is_in_brute_force_scope`), NOR
    * an options-eligible index (one named by at least one ``CE`` / ``PE`` leg in
      *options_eligible_underlyings* — e.g. FINNIFTY / MIDCPNIFTY / NIFTY NEXT 50).

    The broad-market (NIFTY 100 / 500 / TOTAL MARKET), sectoral (NIFTY IT / AUTO /
    PHARMA) indices and INDIA VIX are therefore spot-only. The keyless instrument
    load tags them ``reference_only`` so the ``/universe`` dashboard does NOT badge
    them option-pullable (the bug this closes: every non-F&O index wrongly showing
    "OPT" + passing the "F&O only" filter). A non-``IDX`` row (equity / derivative
    leg) returns ``False`` here — an equity's pullability is decided by
    F&O-eligibility, not this index rule.

    Parameters
    ----------
    instrument
        The candidate instrument-master row.
    options_eligible_underlyings
        The authoritative options-bearing ``(exchange, underlying_symbol)`` set
        from :func:`build_options_eligible_underlyings`, computed once at ingest
        from the FULL master (the lean store discards the option legs, so this is
        not recoverable at query time — Golden Rule #1, never a hardcoded guess).
    """
    if instrument.instrument != InstrumentType.IDX:
        return False
    if is_in_brute_force_scope(instrument):
        return False
    return (instrument.exch_id, instrument.symbol_name) not in options_eligible_underlyings


#: One underlying's contract economics derived from its derivative legs:
#: ``(lot_size, tick_size, freeze_quantity)``.
UnderlyingEconomics = tuple[int, Decimal, int | None]


def build_underlying_economics(
    instruments: Iterable[Instrument],
) -> dict[tuple[Exchange, str], UnderlyingEconomics]:
    """Map ``(exchange, underlying_symbol)`` to leg-derived contract economics.

    In the vendor instrument master, IDX and EQ rows carry BLANK / placeholder
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
    and this projects each to the ``(symbol, exchange)`` pair the options
    rolling fan-out expects.

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
    fno_eligible_security_ids: frozenset[int] = frozenset(),
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
      ``symbol_name`` is the long company name, DEC-NIFTY-TOTAL-MARKET-ADMIT-001)
      **OR** a TOKEN-JOIN F&O stock (``security_id`` in
      *fno_eligible_security_ids*) — a genuinely F&O-eligible stock whose SPOT
      ``symbol_name`` is spelled differently from its option leg's
      ``underlying_symbol`` (e.g. spot ``BAJAJ-AUTO`` vs leg ``BAJAJAUTO``), so
      the ticker key misses it, but whose GENUINE numeric vendor token matches an
      option leg's ``underlying_security_id`` (DEC-FNO-EQ-TOKEN-JOIN-PERSIST-001).
      Both id-keyed sets default empty → identical to the prior F&O-only clause.

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
    reference_security_ids
        ``SECURITY_ID``s of admitted REFERENCE_ONLY NTM constituents (defaults
        empty). Matched by id because a non-F&O equity's ``symbol_name`` is the
        long company name, not the ticker (DEC-NIFTY-TOTAL-MARKET-ADMIT-001).
    fno_eligible_security_ids
        ``SECURITY_ID``s of TOKEN-JOIN F&O stocks — genuinely F&O-eligible
        equities whose spot ``symbol_name`` ≠ their option leg's
        ``underlying_symbol`` so the ticker key misses them, but whose genuine
        numeric vendor token matches an option leg's ``underlying_security_id``
        (defaults empty; DEC-FNO-EQ-TOKEN-JOIN-PERSIST-001).

    Returns
    -------
    bool
        ``True`` iff *instrument* is an admitted index, an F&O-eligible equity
        (by ticker or token-join), or an admitted reference constituent;
        ``False`` for every derivative leg or out-of-universe row.
    """
    instrument_type = instrument.instrument
    if instrument_type == InstrumentType.IDX:
        # Same IDX rule as is_in_fetch_scope: all NSE indices; BSE only SENSEX.
        return (
            instrument.exch_id == Exchange.NSE or instrument.symbol_name == _BSE_FETCH_INDEX_SYMBOL
        )
    if instrument_type == InstrumentType.EQ:
        # An NSE equity is a universe underlying if it is F&O-eligible by TICKER,
        # OR an admitted REFERENCE_ONLY constituent (by SECURITY_ID — its
        # SYMBOL_NAME is the long company name, not the ticker, so the ticker key
        # cannot match; DEC-NIFTY-TOTAL-MARKET-ADMIT-001), OR a TOKEN-JOIN F&O
        # stock (by SECURITY_ID — its spot symbol is spelled differently from its
        # option leg's underlying_symbol so the ticker key misses it, but its
        # genuine numeric token matches a leg; DEC-FNO-EQ-TOKEN-JOIN-PERSIST-001).
        # Both id-keyed sets default empty → byte-identical to the F&O-only rule.
        return instrument.exch_id == Exchange.NSE and (
            (instrument.exch_id, instrument.symbol_name) in fno_eligible_underlyings
            or instrument.security_id in reference_security_ids
            or instrument.security_id in fno_eligible_security_ids
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
    "ENGINE_SWEEP_SPOT_TRIPLES",
    "INDEX_UNDERLYINGS",
    "NIFTY_FUTURES_SERIES_SYMBOL",
    "REFERENCE_ONLY_INDICES",
    "V1_INDICES",
    "V1_UNDERLYINGS",
    "EngineSweepVerdict",
    "OptionUnderlyingGap",
    "UnderlyingEconomics",
    "UnderlyingSpec",
    "build_fno_eligible_underlyings",
    "build_options_eligible_underlyings",
    "build_underlying_economics",
    "classify_engine_sweep_target",
    "classify_underlying_kind",
    "derive_lake_underlyings",
    "diagnose_unmatched_option_underlyings",
    "is_engine_sweep_symbol",
    "is_in_brute_force_scope",
    "is_in_fetch_scope",
    "is_in_scope",
    "is_index_instrument",
    "is_index_underlying",
    "is_option_pullable",
    "is_reference_only",
    "is_spot_only_index",
    "is_universe_underlying",
    "normalize_underlying_symbol",
]
