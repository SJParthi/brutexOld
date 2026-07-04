"""Groww instrument-master parsing + the O(1) lookup index.

DEC-GROWW-ADAPTER-LEAF-001 follow-up (PR-D, ADR-0009 §5). Parses the verified
21-column Groww instrument master (``docs/groww/probe/INSTRUMENT_MASTER_FINDINGS.md``)
into the vendor-neutral :class:`brutex.models.Instrument`, and builds the two
in-memory dicts that guarantee **O(1) instrument lookup + symbol mapping** (the
operator's hard requirement):

* ``by_groww_symbol``      — ``"NSE-NIFTY" -> Instrument``            (O(1))
* ``by_exchange_symbol``   — ``(Exchange.NSE, "NIFTY") -> Instrument`` (O(1))

The CSV is parsed once in a single O(n) pass (:func:`build_index`); every
downstream resolution is O(1) ``dict`` access — no list scans anywhere. Out-of-
scope rows (MCX / COMMODITY / unknown type) are filtered, never raised. The live
download (HTTP fetch behind the ``GROWW_CSV_ALLOWED_HOST`` SSRF allowlist) lands
in PR-E; this module takes already-fetched CSV text so it is fully testable with
golden fixtures — no live API.

The Groww → ``Instrument`` field mapping (operator-approved "reuse the model"):
``exch_id`` ← exchange · ``security_id`` ← exchange_token · ``symbol_name`` ←
trading_symbol · ``display_name`` ← name · ``instrument`` ← instrument_type
(already ``IDX/EQ/FUT/CE/PE``) · index rows → ``Segment.INDEX`` (else CASH/FNO).
"""

from __future__ import annotations

import csv
import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from io import StringIO

import structlog

from brutex.adapters.groww.constants import (
    EXCLUDED_SEGMENTS,
    GROWW_EXCHANGES,
    GROWW_INSTRUMENT_TYPES,
    INSTRUMENT_CSV_COLUMNS,
)
from brutex.models import (
    Exchange,
    Instrument,
    InstrumentFormatError,
    InstrumentType,
    Segment,
)

#: Parser logger — surfaces the count of in-scope rows Groww left WITHOUT a
#: groww_symbol (DEC-INSTRUMENT-SKIP-NO-GSYM-001), so a junk vendor row is visible
#: in /logs rather than silently dropped or (worse) aborting the whole master load.
_LOG = structlog.get_logger("brutex.adapters.groww.instruments")

_OPTION_TYPES = frozenset({InstrumentType.CE, InstrumentType.PE})

#: Row-level DATA failures we isolate-and-skip per row (DEC-INSTRUMENT-SKIP-NO-GSYM-001).
#: A single junk/stale vendor row must never abort the whole ~162k-row master, so
#: BOTH families a malformed/stale *cell* can raise are caught:
#:
#: * :class:`InstrumentFormatError` — our explicit field guards (blank groww_symbol,
#:   unparseable lot/tick/strike/date, unmappable segment);
#: * :class:`ValueError` — enum coercion of a drifted ``exchange``/``instrument_type``
#:   AND the :class:`~brutex.models.Instrument` ``__post_init__`` invariants (a FUT
#:   with no underlying, an option with strike ≤ 0, an ``EQ`` whose ``lot_size`` cell
#:   is blank → ``0`` → "must be positive"). ``ValueError`` is the documented stale-row
#:   surface, so catching it is what makes the load survive future vendor drift.
#:
#: This is deliberately NOT ``except Exception``: a structural fault still fail-louds
#: — the HEADER schema contract is checked once above the loop (Golden Rule #1), and a
#: genuine CODE bug (``AttributeError``/``TypeError``) surfaces loudly instead of being
#: silently swallowed row-by-row. We harden against bad DATA, never mask bad CODE.
_ROW_PARSE_ERRORS: tuple[type[Exception], ...] = (InstrumentFormatError, ValueError)


def _clean(value: str | None) -> str | None:
    """Return a stripped non-empty cell, or ``None`` for blank/absent."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _int_or_zero(value: str | None, *, field: str, gsym: str) -> int:
    """Parse an int cell; a blank maps to ``0`` (indices have no lot/freeze)."""
    text = _clean(value)
    if text is None:
        return 0
    try:
        return int(text)
    except ValueError as exc:
        raise InstrumentFormatError(f"{gsym}: unparseable {field}: {value!r}") from exc


def _security_id(token: str | None, *, gsym: str) -> int:
    """Resolve the model's int ``security_id`` from Groww's ``exchange_token``.

    Options/equities carry a numeric token (e.g. ``50467``) → used directly.
    Indices carry a STRING token (e.g. ``INDIAVIX`` / ``NIFTY``), but the model's
    ``security_id`` is ``int`` and the ``groww_symbol`` dict is the real identity
    key — so for a non-numeric token we derive a DETERMINISTIC surrogate from the
    ``groww_symbol`` (a stable blake2b-4byte int). This is an internal surrogate,
    explicitly NOT a vendor id (Golden Rule #1); index identity stays unique via
    the distinct ``groww_symbol`` + the ``Segment.INDEX`` PK partition.
    """
    text = _clean(token)
    if text is not None and text.isdigit():
        return int(text)
    return int.from_bytes(hashlib.blake2b(gsym.encode(), digest_size=4).digest(), "big")


def _opt_int(value: str | None, *, field: str, gsym: str) -> int | None:
    """Parse an optional int cell; blank → ``None``, malformed → raise."""
    text = _clean(value)
    if text is None:
        return None
    try:
        return int(text)
    except ValueError as exc:
        raise InstrumentFormatError(f"{gsym}: unparseable {field}: {value!r}") from exc


def _opt_decimal(value: str | None, *, field: str, gsym: str) -> Decimal | None:
    """Parse an optional Decimal cell (exact via str); blank → ``None``."""
    text = _clean(value)
    if text is None:
        return None
    try:
        parsed = Decimal(text)
    except InvalidOperation as exc:
        raise InstrumentFormatError(f"{gsym}: unparseable {field}: {value!r}") from exc
    if not parsed.is_finite():
        raise InstrumentFormatError(f"{gsym}: non-finite {field}: {value!r}")
    return parsed


def _opt_date(value: str | None, *, field: str, gsym: str) -> date | None:
    """Parse an optional ISO ``YYYY-MM-DD`` date cell; blank → ``None``."""
    text = _clean(value)
    if text is None:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise InstrumentFormatError(f"{gsym}: unparseable {field}: {value!r}") from exc


def _segment_for(itype: InstrumentType, groww_segment: str | None, *, gsym: str) -> Segment:
    """Map a Groww segment to the neutral :class:`Segment`.

    Indices take ``Segment.INDEX`` regardless of the Groww ``CASH`` label
    (DEC-INDEX-SEGMENT-PK-FIX-001 keeps indices off the cash PK).
    """
    if itype == InstrumentType.IDX:
        return Segment.INDEX
    if groww_segment == "CASH":
        return Segment.CASH
    if groww_segment == "FNO":
        return Segment.FNO
    raise InstrumentFormatError(f"{gsym}: unmappable segment {groww_segment!r}")


def is_in_scope_row(row: dict[str, str]) -> bool:
    """True if a CSV row is in Track-1 scope (NSE/BSE, not COMMODITY, known type).

    Filters MCX, the COMMODITY segment, and any unknown instrument type — those
    are skipped (never raised), so an out-of-scope row never reaches the model.
    """
    return (
        _clean(row.get("exchange")) in GROWW_EXCHANGES
        and _clean(row.get("segment")) not in EXCLUDED_SEGMENTS
        and _clean(row.get("instrument_type")) in GROWW_INSTRUMENT_TYPES
    )


def parse_instrument_row(row: dict[str, str]) -> tuple[str, Instrument]:
    """Map one in-scope 21-column Groww CSV row to ``(groww_symbol, Instrument)``.

    Raises :class:`InstrumentFormatError` on a malformed required field (the
    :class:`Instrument` constructor additionally enforces the derivative /
    option / lot-tick invariants).
    """
    gsym = _clean(row.get("groww_symbol"))
    if gsym is None:
        raise InstrumentFormatError(f"row missing groww_symbol: {row!r}")
    itype = InstrumentType(row["instrument_type"].strip())
    is_option = itype in _OPTION_TYPES

    instrument = Instrument(
        exch_id=Exchange(row["exchange"].strip()),
        security_id=_security_id(row.get("exchange_token"), gsym=gsym),
        symbol_name=row["trading_symbol"].strip(),
        display_name=_clean(row.get("name")),
        instrument=itype,
        segment=_segment_for(itype, _clean(row.get("segment")), gsym=gsym),
        series=_clean(row.get("series")),
        isin=_clean(row.get("isin")),
        underlying_symbol=_clean(row.get("underlying_symbol")),
        underlying_security_id=_opt_int(
            row.get("underlying_exchange_token"), field="underlying_exchange_token", gsym=gsym
        ),
        lot_size=_int_or_zero(row.get("lot_size"), field="lot_size", gsym=gsym),
        tick_size=_opt_decimal(row.get("tick_size"), field="tick_size", gsym=gsym) or Decimal(0),
        sm_freeze_qty=_opt_int(row.get("freeze_quantity"), field="freeze_quantity", gsym=gsym),
        sm_expiry_date=_opt_date(row.get("expiry_date"), field="expiry_date", gsym=gsym),
        strike_price=_opt_decimal(row.get("strike_price"), field="strike_price", gsym=gsym),
        option_type=itype.value if is_option else None,
        expiry_flag=None,
        asm_gsm_flag=_clean(row.get("is_reserved")) == "1",
        # Preserve the RAW CSV exchange_token (the live-feed token: "NIFTY"/"1"/
        # "2885") — security_id hashes non-numeric index tokens into a surrogate,
        # so the websocket feed can only be addressed from this literal token
        # (07-feed.md:54,132; DEC-LIVE-MANIFEST-FEED-FORMAT-001).
        feed_token=_clean(row.get("exchange_token")),
    )
    return gsym, instrument


@dataclass(frozen=True, slots=True)
class InstrumentIndex:
    """O(1) instrument lookup built in one pass over the master.

    ``by_groww_symbol`` keys on the Groww-native ``"EXCH-SYMBOL[-…]"`` string;
    ``by_exchange_symbol`` keys on ``(Exchange, trading_symbol)``. Both resolve
    in O(1); ``universe`` is the immutable in-scope page.

    ``groww_symbol_by_pair`` (DEC-GROWW-HYPHEN-SYMBOL-RESOLVE-001) is the master's
    REAL groww_symbol for each ``(Exchange, trading_symbol)`` pair — the authoritative
    string to hand the vendor when a BARE trading symbol is resolved, so the adapter
    never RECONSTRUCTS ``EXCH-symbol`` (which is wrong for a verbose-symbol index like
    ``MIDCPNIFTY`` whose ``trading_symbol`` ``"NIFTY MID SELECT"`` ≠ the ``MIDCPNIFTY``
    component of its ``NSE-MIDCPNIFTY`` groww_symbol). Same keys as
    ``by_exchange_symbol`` (built in the one pass); defaulted so a hand-built test
    index (empty pair map) stays valid.
    """

    by_groww_symbol: dict[str, Instrument]
    by_exchange_symbol: dict[tuple[Exchange, str], Instrument]
    universe: tuple[Instrument, ...]
    groww_symbol_by_pair: dict[tuple[Exchange, str], str] = field(default_factory=dict)


def build_index(pairs: Iterable[tuple[str, Instrument]]) -> InstrumentIndex:
    """Build the two O(1) dicts in a single O(n) pass (first-wins dedup).

    A duplicate ``groww_symbol`` keeps the first occurrence (the master
    occasionally repeats a key); the dedup is O(1) per row via ``dict`` membership
    — never a nested scan. The ``(exchange, trading_symbol)`` pair maps
    (``by_exchange_symbol`` + ``groww_symbol_by_pair``) are **also first-wins** and
    kept in lockstep (consistent with ``by_groww_symbol``): a second row colliding
    on the same pair is DROPPED — never silently last-wins — and the collision is
    logged at WARNING naming BOTH the kept and the dropped groww_symbol (Golden
    Rule #1). A row with a BLANK — or whitespace-only — ``trading_symbol`` cannot
    key a pair (blank rows would collapse onto ``(exchange, "")``, whitespace-only
    ones onto ``(exchange, " ")``): it stays pullable via
    ``by_groww_symbol`` but is excluded from the pair maps + counted, so the junk
    vendor row is visible in ``/logs`` rather than shadowing a real pair key.

    Index surrogate-collision detection (DEC-INDEX-SURROGATE-COLLISION-DETECT-001):
    a non-numeric index ``exchange_token`` becomes a deterministic 32-bit blake2b
    surrogate ``security_id`` derived from the ``groww_symbol``
    (:func:`_security_id`) — computed per row with no cross-row awareness. Two
    DISTINCT-gsym indices whose surrogates collide (~1e-7 birthday odds) would
    both enter this index, then one would be SILENTLY last-wins-overwritten
    downstream at the store's ``(security_id, exch_id, segment)`` identity dedup
    — order-dependently, with zero warning. This chokepoint now detects the
    collision with an O(1) reverse-map check keyed ``(exch_id, security_id)``
    (mirroring the store's ``Segment.INDEX`` PK partition), KEEPS the first index
    (deterministic — mirrors the gsym first-wins dedup above, and agrees with
    what the store retains), drops the second, and emits ONE loud
    ``instrument_master.index_security_id_collision`` WARNING naming BOTH
    colliding groww_symbols (``kept``/``dropped``) so nothing is silent
    (Golden Rule #1). WARN-not-raise: a raise would abort the whole ~750-
    underlying master load on a one-index blip (Recoverability loss).
    """
    by_groww: dict[str, Instrument] = {}
    by_pair: dict[tuple[Exchange, str], Instrument] = {}
    gsym_by_pair: dict[tuple[Exchange, str], str] = {}
    universe: list[Instrument] = []
    # Reverse map: which gsym FIRST claimed each INDEX (exchange, security_id)
    # identity — the surrogate-collision detector's O(1) membership probe.
    index_identity_owner: dict[tuple[Exchange, int], str] = {}
    blank_symbol_skipped = 0
    for gsym, instrument in pairs:
        if gsym in by_groww:
            continue  # first-wins dedup, O(1)
        if instrument.segment is Segment.INDEX:
            # Only index rows ever carry a hashed surrogate security_id
            # (numeric vendor tokens are used directly), so the detector is
            # scoped to Segment.INDEX — the store PK partition where a
            # collision would silently last-wins-overwrite one index.
            identity = (instrument.exch_id, instrument.security_id)
            owner = index_identity_owner.get(identity)
            if owner is not None:
                # A PRIOR index on this exchange already owns this 32-bit
                # surrogate under a DIFFERENT groww_symbol: keep-first, drop
                # this one, and say so LOUDLY naming both sides.
                _LOG.warning(
                    "instrument_master.index_security_id_collision",
                    security_id=instrument.security_id,
                    exchange=instrument.exch_id.value,
                    kept=owner,
                    dropped=gsym,
                )
                continue
            index_identity_owner[identity] = gsym
        by_groww[gsym] = instrument
        universe.append(instrument)
        symbol = instrument.symbol_name
        if not symbol.strip():
            # A blank trading_symbol cannot key the pair maps — every blank-symbol
            # row would collapse onto ``(exchange, "")`` and shadow the others. Keep
            # the row pullable by its groww_symbol (already in by_groww/universe) but
            # EXCLUDE it from the pair maps + COUNT it, so the junk vendor row is
            # visible in /logs instead of silently polluting a pair key.
            # Whitespace-ONLY counts as blank too (defense-in-depth: the CSV parser
            # strips, but a hand-built Instrument can carry ``" "`` and would
            # otherwise key ``(exchange, " ")``).
            blank_symbol_skipped += 1
            continue
        pair_key = (instrument.exch_id, symbol)
        existing_gsym = gsym_by_pair.get(pair_key)
        if existing_gsym is not None:
            # First-wins on the ``(exchange, trading_symbol)`` pair too — CONSISTENT
            # with the by_groww_symbol dedup above. The prior last-wins silently
            # dropped the FIRST row and could diverge the resolved instrument from
            # the sent groww_symbol. Keep the first; make the collision LOUD.
            _LOG.warning(
                "instrument_master.pair_key_collision",
                exchange=instrument.exch_id.value,
                trading_symbol=symbol,
                kept_groww_symbol=existing_gsym,
                dropped_groww_symbol=gsym,
            )
            continue
        by_pair[pair_key] = instrument
        # The master's REAL groww_symbol for this pair — used verbatim at fetch time
        # so a verbose-symbol index (NIFTY MID SELECT → NSE-MIDCPNIFTY) is never
        # mis-reconstructed (DEC-GROWW-HYPHEN-SYMBOL-RESOLVE-001). Kept in lockstep
        # with ``by_pair`` (same key, same first-wins assignment) so the resolved
        # instrument and the sent groww_symbol can never diverge.
        gsym_by_pair[pair_key] = gsym
    if blank_symbol_skipped:
        _LOG.warning(
            "instrument_master.blank_symbol_pair_skipped",
            count=blank_symbol_skipped,
        )
    return InstrumentIndex(
        by_groww_symbol=by_groww,
        by_exchange_symbol=by_pair,
        universe=tuple(universe),
        groww_symbol_by_pair=gsym_by_pair,
    )


def parse_instrument_csv(text: str) -> list[tuple[str, Instrument]]:
    """Parse Groww instrument-master CSV text into in-scope ``(gsym, Instrument)``.

    Validates the header against the verified 21-column schema (fail-loud on
    drift — Golden Rule #1), then parses each in-scope row. Out-of-scope rows
    (MCX / COMMODITY / unknown type) are skipped.

    Resilience (DEC-INSTRUMENT-SKIP-NO-GSYM-001): a single malformed or stale
    in-scope row (e.g. a blank ``groww_symbol`` or a blank ``lot_size``) is
    isolated — caught (see :data:`_ROW_PARSE_ERRORS`), counted, logged at
    WARNING, and skipped — so one junk vendor row can never abort the whole
    master and leave the dashboard with an empty universe (the HTTP 502 the
    operator hit). The HEADER schema contract above still fail-louds.
    """
    reader = csv.DictReader(StringIO(text))
    if tuple(reader.fieldnames or ()) != INSTRUMENT_CSV_COLUMNS:
        raise InstrumentFormatError(f"Groww instrument header drift: got {reader.fieldnames!r}")
    parsed: list[tuple[str, Instrument]] = []
    skipped: list[str] = []
    for row in reader:
        if not is_in_scope_row(row):
            continue
        # A SINGLE un-parseable in-scope row must NEVER abort the whole ~162k-row
        # master (DEC-INSTRUMENT-SKIP-NO-GSYM-001). The observed real-world case is
        # the BSE "BSE FOCUSED IT" IDX (exchange_token 93) with a BLANK groww_symbol
        # + blank trading_symbol — un-pullable (every Groww call keys on the
        # groww_symbol), so it cannot become a usable Instrument. We skip ANY row
        # whose required fields don't parse OR whose Instrument invariants fail
        # (blank groww_symbol, unparseable lot/tick/strike/date, unmappable segment,
        # a stale EQ with a blank lot_size, …) — see _ROW_PARSE_ERRORS for the full
        # caught surface — record it, and load every VALID row, so one junk/stale
        # vendor row can never leave the operator with an empty universe. This is NOT
        # silent data invention: the skip count + sample reasons are logged at
        # WARNING, and the schema CONTRACT (header drift, checked above) still
        # fail-louds (Golden Rule #1).
        try:
            parsed.append(parse_instrument_row(row))
        except _ROW_PARSE_ERRORS as exc:
            skipped.append(str(exc))
    if skipped:
        _LOG.warning(
            "instrument_master.skipped_unparseable_rows",
            count=len(skipped),
            samples=skipped[:3],
        )
    return parsed


def load_index_from_csv(text: str) -> InstrumentIndex:
    """Convenience: parse CSV text and build the O(1) index in one call."""
    return build_index(parse_instrument_csv(text))


def resolve(index: InstrumentIndex, groww_symbol: str) -> Instrument:
    """O(1) lookup by ``groww_symbol``; raises ``KeyError`` if absent."""
    return index.by_groww_symbol[groww_symbol]


def resolve_by_pair(index: InstrumentIndex, exchange: Exchange, trading_symbol: str) -> Instrument:
    """O(1) lookup by ``(exchange, trading_symbol)``; raises ``KeyError`` if absent."""
    return index.by_exchange_symbol[(exchange, trading_symbol)]


def index_symbol_of(groww_symbol: str) -> str:
    """Return the SYMBOL component of an index ``groww_symbol``.

    DEC-FNO-INDEX-GROWW-SYMBOL-MATCH-001. For an ``IDX`` row the
    ``groww_symbol`` is the two-part vendor key ``"EXCH-SYMBOL"`` (e.g.
    ``"NSE-NIFTY"`` → ``"NIFTY"``, ``"NSE-MIDCPNIFTY"`` → ``"MIDCPNIFTY"``,
    ``"BSE-SENSEX"`` → ``"SENSEX"``). The SYMBOL component is the vendor's
    F&O symbol for the index — the SAME string the vendor stamps as the
    ``underlying_symbol`` on that index's ``CE`` / ``PE`` option legs (the
    brute-force universe proves this convention: ``NSE-NIFTY`` ↔ option
    ``underlying_symbol`` ``"NIFTY"``). It is therefore the vendor-canonical
    bridge from a spot ``IDX`` row to its options when the row's verbose
    ``trading_symbol`` (``symbol_name``, e.g. ``"NIFTY MID SELECT"``) differs
    from that F&O symbol (DEC-NON-OPTION-INDEX-REFTAG-001 keyed the OPT badge
    on ``symbol_name`` alone, which silently dropped such indices to REF).

    This is a PURE string split on the FIRST ``-`` (an NSE index symbol never
    contains a hyphen, and an ``IDX`` ``groww_symbol`` has exactly one) — it
    NEVER encodes a per-index alias (Golden Rule #1); it only reads the
    vendor's own key. A ``groww_symbol`` with no ``-`` yields ``""`` (matches
    nothing — safe).

    Parameters
    ----------
    groww_symbol
        An ``IDX`` row's Groww-native ``"EXCH-SYMBOL"`` key.

    Returns
    -------
    str
        The SYMBOL component (everything after the first ``-``), or ``""``
        when *groww_symbol* carries no ``-``.
    """
    return groww_symbol.partition("-")[2]


__all__ = [
    "InstrumentIndex",
    "build_index",
    "index_symbol_of",
    "is_in_scope_row",
    "load_index_from_csv",
    "parse_instrument_csv",
    "parse_instrument_row",
    "resolve",
    "resolve_by_pair",
]
