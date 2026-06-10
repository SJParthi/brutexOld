"""Dhan instrument-master parser — detailed scrip-master CSV → ``Instrument``.

Maps a row of Dhan's ``api-scrip-master-detailed.csv``
(``docs/dhan-ref/09``) into the vendor-neutral
:class:`brutex.models.Instrument` (CLAUDE.md §11 COMMON — same output model
the rest of BruteX consumes). The full verified column set is captured in
``docs/dhan-ref/09``; every field BruteX needs — identity, classification,
**lot size, tick size, freeze quantity**, expiry, strike, underlying
linkage — is mapped here. The raw Dhan ``SECURITY_ID`` (unique only within
an exchange-segment) is kept as the numeric ``security_id`` for the candle
API; identity is the ``(security_id, exch_id)`` pair (an NSE row and a BSE
row that share a numeric id are distinct rows — ADR-0003 dropped the old
exchange-prefixed ``vendor_symbol`` synthesis; ``symbol_name`` is the bare
engine symbol).

The **brute-force universe** is selected by :func:`select_universe`:
- the **curated headline indices** — the 31 NSE Index-tab symbols + BSE SENSEX
  (DEC-DHAN-INDEX-ALLOWLIST-001), NOT all ~194 ``INDEX`` rows, and
- every **F&O-eligible single-stock SPOT** equity — i.e. an ``EQUITY`` row
  whose **own ``UNDERLYING_SYMBOL`` ticker** also appears as a derivatives
  ``UNDERLYING_SYMBOL`` (a two-pass set membership, since F&O-eligibility
  can't be read off a lone spot row).

  **Join key (DEC-DHAN-FNO-JOIN-KEY-001):** the F&O-eligibility match is on
  the equity row's ``UNDERLYING_SYMBOL`` (the ticker, e.g. ``"RELIANCE"``),
  NOT its ``SYMBOL_NAME`` (the long company name, e.g.
  ``"RELIANCE INDUSTRIES LTD"``). In the real detailed CSV an equity carries
  the long name in ``SYMBOL_NAME`` and the ticker in ``UNDERLYING_SYMBOL`` /
  ``DISPLAY_NAME``, while a derivative leg carries the underlying ticker in
  ``UNDERLYING_SYMBOL`` — so keying on ``SYMBOL_NAME`` matched only ~19/229
  underlyings and the universe collapsed toward "1 stock". For the same
  reason the admitted equity's ``symbol_name`` is taken from its
  ``UNDERLYING_SYMBOL`` ticker so a stock's identity matches how its F&O
  legs reference it (ground truth: real Dhan ``api-scrip-master-detailed``,
  219,082 rows, decoded 2026-06-01; 229 F&O underlyings).

Derivative legs (options/futures) are parsed for eligibility then dropped
from the lean universe (DEC-INSTRUMENT-UNIVERSE-FAST-001), recoverable on
demand. This module is pure (no I/O); the CSV download lives in the loader.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Final

from brutex.models import BruteXError, Exchange, Instrument, InstrumentType, Segment

#: Detailed-CSV columns this parser requires (schema-drift guard).
#:
#: Classification reads the **``INSTRUMENT``** column (the long vocabulary —
#: ``INDEX`` / ``EQUITY`` / ``FUTSTK`` / ``OPTSTK`` / ``FUTIDX`` / ``OPTIDX`` …),
#: NOT ``INSTRUMENT_TYPE`` (the short codes ``INDEX`` / ``ES`` / ``OP`` / ``FUT``).
#: Reading ``INSTRUMENT_TYPE`` was the "120 indices / 1 stock" bug: option/future
#: legs carry ``INSTRUMENT_TYPE`` ``OP`` / ``FUT`` which match no derivative
#: vocabulary, so the F&O-eligible set came out empty and no stock was admitted
#: (DEC-DHAN-INSTRUMENT-CLASSIFY-001; mirrors the proven tickvault parser which
#: classifies off ``INSTRUMENT``).
_REQUIRED_COLUMNS: Final[frozenset[str]] = frozenset(
    {
        "EXCH_ID",
        "SECURITY_ID",
        "INSTRUMENT",
        "SYMBOL_NAME",
        "DISPLAY_NAME",
        "LOT_SIZE",
        "TICK_SIZE",
        "SM_FREEZE_QTY",
        "UNDERLYING_SYMBOL",
    }
)

#: Dhan ``INSTRUMENT`` → neutral :class:`InstrumentType`. Options resolve to
#: CE/PE via ``OPTION_TYPE``. These are the ``INSTRUMENT`` column's long codes.
_INDEX_TYPES: Final[frozenset[str]] = frozenset({"INDEX"})
_EQUITY_TYPES: Final[frozenset[str]] = frozenset({"EQUITY"})
_FUTURE_TYPES: Final[frozenset[str]] = frozenset({"FUTIDX", "FUTSTK", "FUTCOM", "FUTCUR"})
_OPTION_TYPES: Final[frozenset[str]] = frozenset({"OPTIDX", "OPTSTK", "OPTFUT", "OPTCUR"})

#: Single-stock derivative ``INSTRUMENT`` codes — the ONLY rows that make an
#: equity F&O-eligible. tickvault's ``STOCK_DERIVATIVE_PREFIXES`` (index/currency/
#: commodity derivatives reference index/curr/comm underlyings, never an equity
#: spot). F&O single stocks + their spots are **NSE** only — BSE single-stock F&O
#: is out of scope (only the BSE SENSEX *index* is admitted). DEC-DHAN-UNIVERSE-TICKVAULT-PARITY-001.
_STOCK_DERIVATIVE_TYPES: Final[frozenset[str]] = frozenset({"FUTSTK", "OPTSTK"})

#: Substring marking a Dhan placeholder TEST scrip (e.g. ``"11NSETEST"``). Such
#: rows are excluded from the universe so they never pollute it (tickvault's
#: ``CSV_TEST_SYMBOL_MARKER`` exclusion). DEC-DHAN-UNIVERSE-TICKVAULT-PARITY-001.
_TEST_SCRIP_MARKER: Final[str] = "NSETEST"


def _is_test_scrip(symbol_name: str | None, underlying_symbol: str | None) -> bool:
    """Return ``True`` if either symbol carries the Dhan TEST-scrip marker."""
    return (
        _TEST_SCRIP_MARKER in (symbol_name or "").upper()
        or _TEST_SCRIP_MARKER in (underlying_symbol or "").upper()
    )


#: The CURATED index allow-list (operator decree 2026-06-01, DEC-DHAN-INDEX-ALLOWLIST-001):
#: admit ONLY the headline indices Dhan shows in its **Index** tab — NOT all ~194
#: ``INDEX`` rows in the master (the "100+ indices" the operator rejected). The
#: 31 NSE tokens below are the EXACT ``SYMBOL_NAME`` values in the real Dhan
#: ``api-scrip-master-detailed`` (verified 2026-06-01 against the decoded master)
#: — abbreviated, not the web display names (``NIFTYIT`` not "Nifty IT"). Plus the
#: single BSE index **SENSEX**. Matching is exact on upper-cased ``SYMBOL_NAME``;
#: every other ``INDEX`` row is dropped. Edit this one set to add/remove an index.
_ADMITTED_NSE_INDEX_SYMBOLS: Final[frozenset[str]] = frozenset(
    {
        "NIFTY",  # Nifty 50
        "BANKNIFTY",  # Nifty Bank
        "FINNIFTY",  # Fin Nifty
        "MIDCPNIFTY",  # Nifty Midcap Select
        "NIFTY NEXT 50",
        "GIFTNIFTY",  # GIFT Nifty
        "INDIA VIX",
        "NIFTY 100",
        "NIFTY 200",
        "NIFTY 500",
        "NIFTYMCAP50",  # Nifty Mid Cap 50
        "NIFTY MID100 FREE",  # Nifty Midcap 100 (DEC-DHAN-INDEX-ALLOWLIST-002, tickvault parity)
        "NIFTY MIDCAP 150",
        "NIFTY SMALLCAP 50",
        "NIFTY SMALLCAP 100",
        "NIFTY SMALLCAP 250",
        "NIFTY MICROCAP250",
        "NIFTY AUTO",
        "NIFTYIT",  # Nifty IT
        "NIFTY MEDIA",
        "NIFTY METAL",
        "NIFTY PHARMA",
        "NIFTY PSU BANK",
        "NIFTY PVT BANK",  # Nifty Private Bank
        "NIFTY REALTY",
        "NIFTY FMCG",
        "NIFTY ENERGY",
        "NIFTYINFRA",  # Nifty Infra
        "NIFTY MNC",
        "NIFTY CONSUMPTION",
        "NIFTY SERV SECTOR",  # Nifty Services Sector
    }
)

#: The only BSE index admitted (operator: "one more index which is BSE SENSEX").
_ADMITTED_BSE_INDEX_SYMBOLS: Final[frozenset[str]] = frozenset({"SENSEX"})


def _is_admitted_index(
    exchange: str,
    symbol_name: str | None,
    extra_index_symbols: frozenset[str] = frozenset(),
) -> bool:
    """Return ``True`` if this ``INDEX`` row is on the curated allow-list.

    Indices are NOT exchange-gated like stocks — but only the headline set is
    kept: the 31 NSE Index-tab symbols + BSE SENSEX (DEC-DHAN-INDEX-ALLOWLIST-001/-002).

    ``extra_index_symbols`` (DEC-NIFTY-TOTAL-MARKET-DISCOVER-001): an OPTIONAL set
    of upper-cased ``SYMBOL_NAME`` tokens to additionally admit as indices —
    populated at RUNTIME with the exact symbol :func:`discover_index` found in
    the live master (e.g. NIFTY Total Market). Empty by default → byte-identical
    to the curated allow-list; never a hardcoded guess. Matched on ANY exchange
    (the discovered token is already exchange-specific + INDEX-gated upstream).
    """
    symbol = (symbol_name or "").strip().upper()
    if symbol in extra_index_symbols:
        return True
    if exchange == Exchange.NSE:
        return symbol in _ADMITTED_NSE_INDEX_SYMBOLS
    if exchange == Exchange.BSE:
        return symbol in _ADMITTED_BSE_INDEX_SYMBOLS
    return False


class DhanInstrumentError(BruteXError):
    """A scrip-master row/header violates the documented shape."""


class IndexDiscoveryError(DhanInstrumentError):
    """:func:`discover_index` could not resolve the live master to exactly one
    ``INDEX`` row (zero matches, an ambiguous multi-match, or a matched row that
    carries no ``SECURITY_ID``). Fail-loud by design — NEVER guess the id
    (DEC-NIFTY-TOTAL-MARKET-DISCOVER-001, Golden Rule #1 no-hallucination)."""


@dataclass(frozen=True, slots=True)
class DiscoveredIndex:
    """One ``INDEX`` row located in the LIVE master by pattern (no hardcoded guess).

    The whole point: the exact Dhan ``SYMBOL_NAME`` + numeric ``SECURITY_ID`` for
    an index BruteX does not pre-list (e.g. NIFTY Total Market) is read from the
    operator's real ``api-scrip-master-detailed.csv`` at runtime, not asserted by
    BruteX. ``symbol_name`` then feeds ``extra_index_symbols`` to admit the row;
    ``security_id`` is the candle-API key + the id to tag ``reference_only=TRUE``.
    """

    symbol_name: str
    security_id: int
    exchange: Exchange


def discover_index(
    rows: Iterable[Mapping[str, str]],
    *,
    pattern: str,
    exchange: Exchange = Exchange.NSE,
) -> DiscoveredIndex:
    """Find the SINGLE ``INDEX`` row whose ``SYMBOL_NAME`` matches *pattern*.

    Reads the LIVE detailed master (the same rows :func:`select_universe` parses)
    and returns the real ``(symbol_name, security_id)`` for the one matching index
    — so the operator never has to hand-transcribe the token and BruteX never
    hardcodes a guess (DEC-NIFTY-TOTAL-MARKET-DISCOVER-001).

    Parameters
    ----------
    rows
        Parsed detailed-CSV rows (mappings of column → cell).
    pattern
        A regular expression matched case-insensitively (``re.search``) against
        each ``INDEX`` row's ``SYMBOL_NAME`` (e.g. ``r"TOTAL\\s*MARKET"`` for NIFTY
        Total Market). The match is on the *vendor* symbol, so an abbreviated
        master name still resolves when the pattern is loose enough.
    exchange
        The exchange whose ``INDEX`` rows are scanned (default NSE).

    Returns
    -------
    DiscoveredIndex
        The single matching row.

    Raises
    ------
    IndexDiscoveryError
        If ZERO ``INDEX`` rows match (message names the scan size so the operator
        can loosen the pattern), if MORE THAN ONE matches (message lists every
        candidate ``symbol_name(id=...)`` so the operator can tighten it), or if
        the matched row has no ``SECURITY_ID``. Fail-loud — never a guessed id.
    """
    regex = re.compile(pattern, re.IGNORECASE)
    matches: list[DiscoveredIndex] = []
    index_rows_seen = 0
    for row in rows:
        if row.get("INSTRUMENT", "").strip().upper() not in _INDEX_TYPES:
            continue
        if row.get("EXCH_ID", "").strip().upper() != exchange:
            continue
        index_rows_seen += 1
        name = (row.get("SYMBOL_NAME") or "").strip()
        if not name or regex.search(name) is None:
            continue
        security_id = _opt_int(row.get("SECURITY_ID"))
        if security_id is None:
            raise IndexDiscoveryError(
                f"{exchange} INDEX row {name!r} matched /{pattern}/i but carries no "
                f"SECURITY_ID — the master row is unusable; inspect the CSV"
            )
        matches.append(
            DiscoveredIndex(symbol_name=name, security_id=security_id, exchange=Exchange(exchange))
        )
    if not matches:
        raise IndexDiscoveryError(
            f"no {exchange} INDEX row matched /{pattern}/i among {index_rows_seen} "
            f"{exchange} INDEX rows — loosen the pattern or confirm the index is "
            f"present in the master"
        )
    if len(matches) > 1:
        listing = ", ".join(
            f"{m.symbol_name!r}(id={m.security_id})"
            for m in sorted(matches, key=lambda m: m.symbol_name)
        )
        raise IndexDiscoveryError(
            f"/{pattern}/i is ambiguous — matched {len(matches)} {exchange} INDEX "
            f"rows: {listing}; tighten the pattern to select exactly one"
        )
    return matches[0]


def validate_header(fieldnames: Sequence[str] | None) -> None:
    """Raise :class:`DhanInstrumentError` if any required column is absent."""
    present = frozenset(fieldnames or ())
    missing = _REQUIRED_COLUMNS - present
    if missing:
        raise DhanInstrumentError(f"scrip-master CSV missing required columns: {sorted(missing)}")


def _opt_str(value: str | None) -> str | None:
    """Return a stripped non-empty string, else ``None``."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _to_int(value: str, field: str, *, default: int = 0) -> int:
    """Parse an integer cell; empty → *default*; junk → error."""
    text = value.strip()
    if not text:
        return default
    try:
        return int(float(text)) if "." in text else int(text)
    except ValueError as exc:
        raise DhanInstrumentError(f"unparseable {field}: {value!r}") from exc


def _opt_int(value: str | None) -> int | None:
    """Parse an optional integer cell; empty/None → ``None``."""
    if value is None or not value.strip():
        return None
    return _to_int(value, "int")


def _to_decimal(value: str, field: str) -> Decimal:
    """Parse a Decimal cell; empty → ``Decimal('0')``; junk → error."""
    text = value.strip()
    if not text:
        return Decimal("0")
    try:
        result = Decimal(text)
    except InvalidOperation as exc:
        raise DhanInstrumentError(f"unparseable {field}: {value!r}") from exc
    if not result.is_finite():
        raise DhanInstrumentError(f"non-finite {field}: {value!r}")
    return result


def _opt_decimal(value: str | None) -> Decimal | None:
    """Parse an optional Decimal cell; empty/None → ``None``."""
    if value is None or not value.strip():
        return None
    return _to_decimal(value, "decimal")


def _opt_expiry(value: str | None) -> date | None:
    """Parse ``SM_EXPIRY_DATE`` (``YYYY-MM-DD``, optional ``  HH:MM:SS`` tail).

    Empty/None → ``None``. Only the leading ``YYYY-MM-DD`` is significant, so
    both a bare date and a ``date time`` value parse via the 10-char prefix.
    """
    text = (value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError as exc:
        raise DhanInstrumentError(f"unparseable SM_EXPIRY_DATE: {value!r}") from exc


def _instrument_type(raw_type: str, option_type: str | None) -> InstrumentType:
    """Map Dhan ``INSTRUMENT`` (+ ``OPTION_TYPE``) → neutral type."""
    key = raw_type.strip().upper()
    if key in _INDEX_TYPES:
        return InstrumentType.IDX
    if key in _EQUITY_TYPES:
        return InstrumentType.EQ
    if key in _FUTURE_TYPES:
        return InstrumentType.FUT
    if key in _OPTION_TYPES:
        side = (option_type or "").strip().upper()
        if side == "CE":
            return InstrumentType.CE
        if side == "PE":
            return InstrumentType.PE
        raise DhanInstrumentError(
            f"option row {raw_type!r} has invalid OPTION_TYPE {option_type!r}"
        )
    raise DhanInstrumentError(f"unknown INSTRUMENT {raw_type!r}")


def parse_instrument_row(row: Mapping[str, str]) -> Instrument:
    """Convert one detailed-CSV row to a normalised :class:`Instrument`.

    Indices carry ``lot_size``/``tick_size`` of 0 (not directly tradable);
    options/futures carry expiry + strike. The raw Dhan ``SECURITY_ID``
    becomes the numeric ``security_id`` (the id the candle API needs);
    identity is the ``(security_id, exch_id)`` pair (``SECURITY_ID`` is
    unique only within an exchange-segment, so the exchange disambiguates a
    shared numeric id — ADR-0003 dropped the old ``vendor_symbol`` synthesis).
    ``symbol_name`` is the bare engine symbol; the raw ``OPTION_TYPE`` /
    ``EXPIRY_FLAG`` are now stored verbatim (``option_type`` / ``expiry_flag``).

    Raises
    ------
    DhanInstrumentError
        On an unknown exchange/instrument type or an unparseable numeric
        cell.
    """
    exch = row["EXCH_ID"].strip().upper()
    if exch not in (Exchange.NSE, Exchange.BSE):
        raise DhanInstrumentError(f"out-of-scope exchange {exch!r} (Track 1 = NSE/BSE)")
    itype = _instrument_type(row["INSTRUMENT"], row.get("OPTION_TYPE"))
    security_id = row["SECURITY_ID"].strip()
    if not security_id:
        raise DhanInstrumentError("row missing SECURITY_ID")
    # Segment mirrors Dhan's exchangeSegment family (DEC-DHAN-IDX-SEGMENT-001):
    # an INDEX lives in the cross-exchange IDX_I segment, NOT NSE_EQ/BSE_EQ.
    # Assigning indices a distinct ``INDEX`` segment (not ``CASH``) is what
    # keeps the (security_id, exch_id, segment, snapshot_date) PK unique when
    # an index and a cash equity share a numeric SECURITY_ID across Dhan's
    # distinct IDX_I vs NSE_EQ segments (e.g. id 13 = NIFTY in IDX_I AND an
    # NSE_EQ stock) — DEC-INDEX-SEGMENT-PK-FIX-001.
    if itype in {InstrumentType.FUT, InstrumentType.CE, InstrumentType.PE}:
        segment = Segment.FNO
    elif itype is InstrumentType.IDX:
        segment = Segment.INDEX
    else:
        segment = Segment.CASH
    underlying = _opt_str(row.get("UNDERLYING_SYMBOL"))
    # An F&O-eligible equity carries the long company name in SYMBOL_NAME and
    # the ticker in UNDERLYING_SYMBOL; prefer the ticker so the spot's identity
    # matches how its derivative legs reference it (DEC-DHAN-FNO-JOIN-KEY-001).
    # Indices + non-F&O equities have no UNDERLYING_SYMBOL → keep SYMBOL_NAME;
    # derivative legs keep their own contract SYMBOL_NAME, not the underlying.
    raw_symbol_name = row["SYMBOL_NAME"].strip()
    symbol_name = (
        underlying if (itype is InstrumentType.EQ and underlying is not None) else raw_symbol_name
    )
    # Identity is ``(security_id, exch_id)`` (ADR-0003). Dhan ``SECURITY_ID``
    # is unique only WITHIN an exchange-segment (an NSE row and a BSE row can
    # share the same numeric id — e.g. ``13`` on both), so the exchange is the
    # disambiguator. ``symbol_name`` is the bare engine symbol (NIFTY, RELIANCE).
    return Instrument(
        exch_id=Exchange(exch),
        security_id=_to_int(security_id, "SECURITY_ID"),
        symbol_name=symbol_name,
        display_name=_opt_str(row.get("DISPLAY_NAME")),
        instrument=itype,
        segment=segment,
        series=_opt_str(row.get("SERIES")),
        isin=_opt_str(row.get("ISIN")),
        underlying_symbol=underlying,
        underlying_security_id=_opt_int(row.get("UNDERLYING_SECURITY_ID")),
        lot_size=_to_int(row["LOT_SIZE"], "LOT_SIZE"),
        tick_size=_to_decimal(row["TICK_SIZE"], "TICK_SIZE"),
        sm_freeze_qty=_opt_int(row.get("SM_FREEZE_QTY")),
        sm_expiry_date=_opt_expiry(row.get("SM_EXPIRY_DATE")),
        strike_price=_opt_decimal(row.get("STRIKE_PRICE")),
        option_type=_opt_str(row.get("OPTION_TYPE")),
        expiry_flag=_opt_str(row.get("EXPIRY_FLAG")),
        asm_gsm_flag=bool(_opt_str(row.get("ASM_GSM_FLAG"))),
    )


def fno_underlying_symbols(rows: Iterable[Mapping[str, str]]) -> frozenset[str]:
    """Collect the set of symbols that have F&O derivatives (the eligible set).

    A symbol is F&O-eligible if it appears as ``UNDERLYING_SYMBOL`` on any
    futures/option row.
    """
    eligible: set[str] = set()
    for row in rows:
        if not _is_nse_stock_derivative(row):
            continue
        underlying = _opt_str(row.get("UNDERLYING_SYMBOL"))
        if underlying is not None and not _is_test_scrip(row.get("SYMBOL_NAME"), underlying):
            eligible.add(underlying)
    return frozenset(eligible)


#: OPTION leg ``INSTRUMENT`` codes — CE/PE only, single-stock (OPTSTK) + index
#: (OPTIDX). **Futures are DELIBERATELY excluded** (DEC-OPTIONS-ELIGIBLE-GUARD-001):
#: Track 1's option-chain pull NEVER touches futures, so a FUT-only underlying is
#: NOT options-eligible and must not be option-pullable.
_OPTION_LEG_TYPES: Final[frozenset[str]] = frozenset({"OPTSTK", "OPTIDX"})

#: Vendor name-skew aliases between an OPTIDX leg's ``UNDERLYING_SYMBOL`` and the
#: spot INDEX row's ``SYMBOL_NAME`` (DEC-OPTIONS-ELIGIBLE-PRECISE-001). Verified
#: against the operator-supplied detailed master (2026-06-10): the spot row is
#: ``SYMBOL_NAME='NIFTY NEXT 50'`` (NSE, id 38) while its option legs say
#: ``UNDERLYING_SYMBOL='NIFTYNXT50'`` (underlying id 26013) — no string transform
#: bridges NXT↔NEXT, so the alias is explicit and sourced. Every other admitted
#: option index matches by exact symbol (NIFTY/BANKNIFTY/FINNIFTY/MIDCPNIFTY/
#: SENSEX/BANKEX).
_OPTIDX_UNDERLYING_ALIASES: Final[dict[str, str]] = {"NIFTYNXT50": "NIFTY NEXT 50"}


def index_options_underlying_symbols(rows: Iterable[Mapping[str, str]]) -> frozenset[str]:
    """Collect the UPPER-CASED **index** tickers with listed OPTIDX legs.

    The PRECISE index arm of the option-pull guard
    (DEC-OPTIONS-ELIGIBLE-PRECISE-001): OPTIDX rows ONLY (an OPTSTK ticker can
    never make an index option-pullable), NSE + BSE (so SENSEX/BANKEX count),
    vendor name-skews bridged via :data:`_OPTIDX_UNDERLYING_ALIASES` (both the
    raw and the aliased spelling are returned, so either side of the skew
    matches). Futures and test scrips are excluded.

    The previous id arm (``UNDERLYING_SECURITY_ID`` membership) was VERIFIED
    WRONG against the real master (2026-06-10): option legs point at the
    derivatives id space (NIFTY → 26000), never the spot INDEX row's id
    (NIFTY → 13), and the pooled ids collide across namespaces — NSE equity ids
    (ABB=13, ADANIENT=25) and BSE underlying ids (SENSEX=1 ≙ NSE 'NIFTY MIDCAP
    150', BANKEX=12 ≙ NSE 'NIFTY ALPHA 50') — so id membership blessed and
    blocked the WRONG indices. Index eligibility is therefore symbol-only.
    """
    eligible: set[str] = set()
    for row in rows:
        if row.get("INSTRUMENT", "").strip().upper() != "OPTIDX":
            continue
        if row.get("EXCH_ID", "").strip().upper() not in (Exchange.NSE, Exchange.BSE):
            continue
        underlying = _opt_str(row.get("UNDERLYING_SYMBOL"))
        if underlying is None or _is_test_scrip(row.get("SYMBOL_NAME"), underlying):
            continue
        upper = underlying.upper()
        eligible.add(upper)
        alias = _OPTIDX_UNDERLYING_ALIASES.get(upper)
        if alias is not None:
            eligible.add(alias)
    return frozenset(eligible)


def options_underlying_symbols(rows: Iterable[Mapping[str, str]]) -> frozenset[str]:
    """Collect the UPPER-CASED tickers that have listed **OPTIONS** (OPTSTK/OPTIDX).

    OPTIONS-ONLY (FUTSTK/FUTIDX deliberately excluded — we never pull futures)
    per DEC-OPTIONS-ELIGIBLE-GUARD-001. A ticker is options-eligible if it
    appears as ``UNDERLYING_SYMBOL`` on any OPTSTK/OPTIDX row (NSE or BSE, so the
    BSE SENSEX/BANKEX options are caught too — unlike the NSE-only stock F&O set).
    Test scrips are excluded. The result is the symbol arm of the option-pull
    guard: an admitted index whose ``symbol_name`` is NOT in this set has no
    option chain and must be blocked from option-pull (quota safety).
    """
    eligible: set[str] = set()
    for row in rows:
        if row.get("INSTRUMENT", "").strip().upper() not in _OPTION_LEG_TYPES:
            continue
        if row.get("EXCH_ID", "").strip().upper() not in (Exchange.NSE, Exchange.BSE):
            continue
        underlying = _opt_str(row.get("UNDERLYING_SYMBOL"))
        if underlying is not None and not _is_test_scrip(row.get("SYMBOL_NAME"), underlying):
            eligible.add(underlying.upper())
    return frozenset(eligible)


def options_underlying_security_ids(rows: Iterable[Mapping[str, str]]) -> frozenset[int]:
    """Collect the spot ``SECURITY_ID``s that listed **OPTIONS** legs point to.

    The robust (string-free) arm of the option-pull guard: each OPTSTK/OPTIDX leg
    carries ``UNDERLYING_SECURITY_ID`` — the id of the ONE real spot row it derives
    from. Matching an admitted index by id avoids the ``"NIFTY"`` vs ``"Nifty 50"``
    symbol-case fragility entirely (DEC-OPTIONS-ELIGIBLE-GUARD-001). FUT legs are
    excluded (we never pull futures); test scrips are excluded.
    """
    ids: set[int] = set()
    for row in rows:
        if row.get("INSTRUMENT", "").strip().upper() not in _OPTION_LEG_TYPES:
            continue
        if row.get("EXCH_ID", "").strip().upper() not in (Exchange.NSE, Exchange.BSE):
            continue
        if _is_test_scrip(row.get("SYMBOL_NAME"), row.get("UNDERLYING_SYMBOL")):
            continue
        usid = _opt_int(row.get("UNDERLYING_SECURITY_ID"))
        if usid is not None:
            ids.add(usid)
    return frozenset(ids)


def _is_nse_stock_derivative(row: Mapping[str, str]) -> bool:
    """Return ``True`` for an NSE single-stock futures/option row (FUTSTK/OPTSTK).

    The F&O-eligible stock universe is built ONLY from these — tickvault's
    NSE-only ``STOCK_DERIVATIVE_PREFIXES`` gate (DEC-DHAN-UNIVERSE-TICKVAULT-PARITY-001).
    """
    return (
        row.get("INSTRUMENT", "").strip().upper() in _STOCK_DERIVATIVE_TYPES
        and row.get("EXCH_ID", "").strip().upper() == Exchange.NSE
    )


def fno_underlying_security_ids(rows: Iterable[Mapping[str, str]]) -> frozenset[int]:
    """Collect the canonical spot ``SECURITY_ID``s that F&O derivatives point to.

    Each futures/option leg carries ``UNDERLYING_SECURITY_ID`` — the id of the
    ONE real spot row it derives from (tickvault's underlying resolution). When
    the vendor lists a ticker under several equity rows (e.g. an old + a current
    listing of the same company, both with ``UNDERLYING_SYMBOL="MOTHERSON"`` but
    different ``SECURITY_ID``s), this set identifies the **canonical** spot so
    the deduper keeps the right one (DEC-DHAN-UNIVERSE-DEDUP-001).
    """
    ids: set[int] = set()
    for row in rows:
        if not _is_nse_stock_derivative(row):
            continue
        if _is_test_scrip(row.get("SYMBOL_NAME"), row.get("UNDERLYING_SYMBOL")):
            continue
        usid = _opt_int(row.get("UNDERLYING_SECURITY_ID"))
        if usid is not None:
            ids.add(usid)
    return frozenset(ids)


@dataclass(frozen=True, slots=True)
class UniverseReport:
    """The selected universe + parse accounting (for dashboard/cli summaries)."""

    universe: list[Instrument]
    parsed: int  # in-scope (NSE/BSE) rows seen
    persisted: int  # universe size = index + F&O-stock SPOT
    legs_dropped: int  # in-scope rows not in the universe (derivative legs + non-F&O equity)
    filtered: int  # out-of-scope rows skipped (MCX, etc.)
    #: Census of the parse, written to the ``instrument_runs`` audit row
    #: (DEC-INSTRUMENT-RUN-AUDIT-001). Counted over IN-SCOPE rows only, so
    #: ``nse_count + bse_count == in_scope_count`` and the type histogram
    #: reconciles against ``in_scope_count`` (the residual is in-scope EQUITY).
    total_raw_rows: int  # every CSV row seen (in-scope + out-of-scope)
    integrity_failures: int  # always 0 — the parser FAILS CLOSED (a bad row raises)
    in_scope_count: int  # NSE/BSE rows (== ``parsed``)
    out_of_scope_count: int  # non-NSE/BSE rows skipped (== ``filtered``)
    index_count: int  # in-scope rows of INDEX type (raw histogram, NOT the admitted ~32)
    futures_count: int  # in-scope rows of a FUT* type
    options_count: int  # in-scope rows of an OPT* type
    nse_count: int  # in-scope NSE rows
    bse_count: int  # in-scope BSE rows
    #: The NSE F&O-eligible underlying TICKERS (FUTSTK/OPTSTK underlyings). Lets a
    #: caller tell a genuinely-F&O equity from a reference-only constituent that
    #: merely shares the lake (DEC-OPTIONS-PULL-GUARD-001) — an admitted equity's
    #: ``symbol_name`` IS its ticker (DEC-DHAN-FNO-JOIN-KEY-001). Defaults empty.
    fno_eligible_symbols: frozenset[str] = frozenset()
    #: The UPPER-CASED tickers that have listed **OPTIONS** (OPTSTK/OPTIDX legs;
    #: futures excluded — we never pull futures). The symbol arm of the
    #: option-pull guard (DEC-OPTIONS-ELIGIBLE-GUARD-001): an admitted index whose
    #: ``symbol_name`` is absent here has no chain and is blocked from option-pull.
    options_eligible_symbols: frozenset[str] = frozenset()
    #: The ``UNDERLYING_SECURITY_ID``s that listed OPTIONS legs point to. HONEST
    #: LIMIT (DEC-OPTIONS-ELIGIBLE-PRECISE-001, measured 2026-06-10): these ids
    #: live in the DERIVATIVES id space and collide across exchanges/instrument
    #: families — they can NOT be matched against a spot INDEX row's id (NIFTY
    #: spot=13 vs its options' underlying=26000; BSE SENSEX legs carry id 1 which
    #: is NSE 'NIFTY MIDCAP 150'). Kept as an informational report surface only;
    #: NO eligibility decision may consume it.
    options_eligible_security_ids: frozenset[int] = frozenset()
    #: The UPPER-CASED **index** tickers with listed OPTIDX legs, alias-normalized
    #: (``NIFTYNXT50`` ↔ ``NIFTY NEXT 50``) — THE authoritative index arm of the
    #: option-pull guard (DEC-OPTIONS-ELIGIBLE-PRECISE-001).
    index_options_symbols: frozenset[str] = frozenset()


#: F&O leg ``INSTRUMENT`` codes whose lot/freeze define the TRADABLE contract
#: size of their underlying — single-stock (FUTSTK/OPTSTK) + index (FUTIDX/OPTIDX).
#: (Commodity/currency legs reference non-equity/non-index underlyings.)
_FNO_LEG_TYPES: Final[frozenset[str]] = frozenset({"FUTSTK", "OPTSTK", "FUTIDX", "OPTIDX"})


def build_leg_economics(
    rows: Iterable[Mapping[str, str]],
) -> dict[tuple[Exchange, str], tuple[int, int | None]]:
    """Map ``(exchange, underlying_symbol)`` → ``(lot_size, freeze_qty)`` from F&O legs.

    The Dhan master's EQUITY/INDEX rows carry the **cash** lot (1 / 0); the real
    **F&O lot** (RELIANCE 500, NIFTY 65, BANKNIFTY 30) lives on the derivative
    legs. This harvests that lot + freeze per underlying so the live universe
    underlying can be stamped with its tradable contract size
    (DEC-UNDERLYING-LEG-ECONOMICS-001) — the same detailed CSV is the COMMON
    single source (Golden Rule #2), validated row-for-row against the NSE
    ``fo_mktlots`` file.

    The **nearest** ``SM_EXPIRY_DATE`` leg wins (lots can differ across far
    months — e.g. TCS 175 near vs 225 far — so first-seen is non-deterministic
    and wrong; nearest-expiry is the live contract). A leg with no expiry sorts
    last. ``freeze`` ≤ 0 → ``None`` (a count's only valid floor; keeps
    ``InstrumentMeta`` happy). Keyed on the underlying TICKER (``UNDERLYING_SYMBOL``)
    — which equals the admitted equity's ``symbol_name`` (DEC-DHAN-FNO-JOIN-KEY-001)
    AND the admitted index's ``symbol_name`` (a FUTIDX's ``UNDERLYING_SYMBOL`` is
    the index symbol, e.g. ``"NIFTY"``).
    """
    best: dict[tuple[Exchange, str], tuple[date, int, int | None]] = {}
    for row in rows:
        if row.get("INSTRUMENT", "").strip().upper() not in _FNO_LEG_TYPES:
            continue
        exch = row.get("EXCH_ID", "").strip().upper()
        if exch not in (Exchange.NSE, Exchange.BSE):
            continue
        underlying = _opt_str(row.get("UNDERLYING_SYMBOL"))
        if underlying is None:
            continue
        lot = _to_int(row.get("LOT_SIZE", ""), "LOT_SIZE")
        if lot <= 0:
            continue
        freeze = _opt_int(row.get("SM_FREEZE_QTY"))
        if freeze is not None and freeze <= 0:
            freeze = None
        # No expiry → sort LAST (date.max) so any dated leg is preferred.
        expiry = _opt_expiry(row.get("SM_EXPIRY_DATE")) or date.max
        key = (Exchange(exch), underlying.upper())
        incumbent = best.get(key)
        if incumbent is None or expiry < incumbent[0]:
            best[key] = (expiry, lot, freeze)
    return {key: (lot, freeze) for key, (_expiry, lot, freeze) in best.items()}


def select_universe_report(
    rows: Sequence[Mapping[str, str]],
    *,
    reference_security_ids: frozenset[int] = frozenset(),
    extra_index_symbols: frozenset[str] = frozenset(),
) -> UniverseReport:
    """Select the brute-force universe and report the parse accounting.

    Two-pass: collect the F&O-eligible underlying tickers, then keep (a) the
    **curated headline indices** — the 31 NSE Index-tab symbols + BSE SENSEX
    (DEC-DHAN-INDEX-ALLOWLIST-001), every other ``INDEX`` row dropped — and
    (b) every **NSE equity SPOT** whose own
    ``UNDERLYING_SYMBOL`` ticker is F&O-eligible (NOT ``SYMBOL_NAME`` — that is
    the long company name; see DEC-DHAN-FNO-JOIN-KEY-001). Per tickvault parity
    (DEC-DHAN-UNIVERSE-TICKVAULT-PARITY-001) the F&O-stock universe is NSE-only
    (BSE single-stock F&O is out of scope) and Dhan TEST scrips are excluded.
    Derivative legs + non-F&O / out-of-scope equities are counted as
    ``legs_dropped`` (excluded from the lean universe, recoverable on demand);
    out-of-scope-exchange rows are counted as ``filtered``.
    The vendor lists some F&O tickers under MORE THAN ONE equity row (e.g. an
    old + a current listing of the same company — both ``MOTHERSON`` — with
    different ``SECURITY_ID``s). Both produce the same ``(exch_id, symbol_name)``
    pair (``"NSE"``, ``"MOTHERSON"``), which would collide. We therefore
    **deduplicate to ONE canonical spot per key** (O(1) via a dict), preferring
    — exactly like tickvault's underlying resolver — the row a derivative's
    ``UNDERLYING_SECURITY_ID`` points to; absent that link we keep the first
    seen. This guarantees a unique key with no count regression
    (DEC-DHAN-UNIVERSE-DEDUP-001).

    ``reference_security_ids`` (DEC-NIFTY-TOTAL-MARKET-ADMIT-001, Stage 2b
    PR-3): an OPTIONAL set of NSE-equity ``SECURITY_ID``s to ALSO admit as
    REFERENCE_ONLY spot — the non-F&O NIFTY-Total-Market constituents resolved
    by :func:`brutex.universe_constituents.resolve_constituents`. Matched by
    ``SECURITY_ID`` (NOT ticker — a non-F&O equity's ``SYMBOL_NAME`` is the long
    company name, DEC-DHAN-FNO-JOIN-KEY-001). Defaults to ``frozenset()`` → the
    selection is byte-identical to the F&O-only universe; the caller (Stage 2b
    PR-4 wiring) supplies the set AND a matching ``reference_only_of`` at upsert
    so the rows are tagged ``reference_only=TRUE``. The brute-force ENGINE is
    UNAFFECTED — these never become ``IDX`` and are blocked from the swept set
    by ``is_in_brute_force_scope`` (DEC-ENGINE-ISOLATION-INVARIANT-001).

    ``extra_index_symbols`` (DEC-NIFTY-TOTAL-MARKET-DISCOVER-001): an OPTIONAL set
    of ``SYMBOL_NAME`` tokens to admit as INDEX rows beyond the curated allow-list
    — populated at runtime with the symbol :func:`discover_index` found in the
    live master (e.g. NIFTY Total Market). Upper-cased here, then matched by
    :func:`_is_admitted_index`. Empty default → byte-identical. Such an index is
    REFERENCE_ONLY (tagged via ``reference_security_ids`` carrying its own id); the
    brute-force ENGINE is UNAFFECTED — it is an ``IDX`` outside the hardcoded
    3-index brute set, so ``is_in_brute_force_scope`` never sweeps it.
    """
    extra_idx = frozenset(sym.strip().upper() for sym in extra_index_symbols if sym.strip())
    eligible = fno_underlying_symbols(rows)
    canonical_ids = fno_underlying_security_ids(rows)
    options_symbols = options_underlying_symbols(rows)
    options_security_ids = options_underlying_security_ids(rows)
    index_options = index_options_underlying_symbols(rows)
    by_key: dict[tuple[Exchange, str], Instrument] = {}
    parsed = 0
    filtered = 0
    # Census counters for the ``instrument_runs`` audit row (counted IN-SCOPE only,
    # in the single existing pass — DEC-INSTRUMENT-RUN-AUDIT-001).
    index_count = 0
    futures_count = 0
    options_count = 0
    nse_count = 0
    bse_count = 0
    for row in rows:
        exch = row.get("EXCH_ID", "").strip().upper()
        if exch not in (Exchange.NSE, Exchange.BSE):
            filtered += 1
            continue
        parsed += 1
        itype = row.get("INSTRUMENT", "").strip().upper()
        # In-scope census: exchange split (NSE/BSE are the only in-scope exchanges,
        # so the else IS BSE) + instrument-type histogram. Pure counting; does not
        # change selection. EQUITY rows are the residual (not in any of the 3 sets).
        if exch == Exchange.NSE:
            nse_count += 1
        else:
            bse_count += 1
        if itype in _INDEX_TYPES:
            index_count += 1
        elif itype in _FUTURE_TYPES:
            futures_count += 1
        elif itype in _OPTION_TYPES:
            options_count += 1
        # Indices: ONLY the curated headline allow-list — the 31 NSE Index-tab
        # symbols + BSE SENSEX — NOT all ~194 INDEX rows (DEC-DHAN-INDEX-ALLOWLIST-001).
        # Stocks: an F&O-eligible **NSE** equity SPOT (BSE single stocks are out of
        # scope), excluding TEST scrips — tickvault's NSE_EQ-only stock universe
        # (DEC-DHAN-UNIVERSE-TICKVAULT-PARITY-001).
        is_index = itype in _INDEX_TYPES and _is_admitted_index(
            exch, row.get("SYMBOL_NAME"), extra_idx
        )
        is_fno_stock = (
            itype in _EQUITY_TYPES
            and exch == Exchange.NSE
            and _opt_str(row.get("UNDERLYING_SYMBOL")) in eligible
            and not _is_test_scrip(row.get("SYMBOL_NAME"), row.get("UNDERLYING_SYMBOL"))
        )
        # REFERENCE_ONLY constituent (DEC-NIFTY-TOTAL-MARKET-ADMIT-001): an NSE
        # equity admitted by its resolved SECURITY_ID (NOT ticker — its
        # SYMBOL_NAME is the long company name). Empty set → never matches, so
        # the default selection is byte-identical to the F&O-only universe.
        is_reference_stock = (
            itype in _EQUITY_TYPES
            and exch == Exchange.NSE
            and _opt_int(row.get("SECURITY_ID")) in reference_security_ids
            and not _is_test_scrip(row.get("SYMBOL_NAME"), row.get("UNDERLYING_SYMBOL"))
        )
        if is_index or is_fno_stock or is_reference_stock:
            inst = parse_instrument_row(row)
            key = (inst.exch_id, inst.symbol_name)
            existing = by_key.get(key)
            # First row for this key wins; a later row replaces it only when it
            # is the canonical spot (its SECURITY_ID is a derivative underlying)
            # and the incumbent is not — tickvault's UNDERLYING_SECURITY_ID rule.
            if existing is None or (
                inst.security_id in canonical_ids and existing.security_id not in canonical_ids
            ):
                by_key[key] = inst
    # Second collapse — by the table's IDENTITY ``(security_id, exch_id, segment)``,
    # the ``instruments`` PRIMARY KEY minus ``snapshot_date`` (DEC-INSTRUMENT-PK-SEGMENT-001).
    # The first dedup above keys on ``(exch_id, symbol_name)`` (the MOTHERSON case:
    # same name, different SECURITY_ID → keep canonical). But the vendor ALSO lists
    # one ``security_id`` under MORE THAN ONE ``SYMBOL_NAME`` (e.g. NIFTY id=13 as
    # both "Nifty 50" and "NIFTY") — different name-keys, so BOTH survive the first
    # pass yet share the same identity and would violate the PK on bulk INSERT
    # (the live `13, NSE, CASH` crash). A ``security_id`` is unique within an
    # exchange-segment, so collapsing here only merges genuine duplicates — never
    # two distinct instruments — with no count regression (DEC-DHAN-UNIVERSE-PK-DEDUP-001).
    # O(1) per row via a dict; first-seen wins (deterministic, follows CSV order).
    by_identity: dict[tuple[int, Exchange, Segment], Instrument] = {}
    for inst in by_key.values():
        identity = (inst.security_id, inst.exch_id, inst.segment)
        if identity not in by_identity:
            by_identity[identity] = inst
    # Enrich each admitted underlying with its TRADABLE F&O lot + freeze from the
    # nearest-expiry derivative leg (DEC-UNDERLYING-LEG-ECONOMICS-001). The EQUITY/
    # INDEX row carries only the cash lot (1 / 0); the real lot lives on the legs.
    # Keyed on ``symbol_name`` (== the underlying ticker for both admitted stocks
    # AND indices). ``tick_size`` is NOT overridden — an index spot's tick (0.05)
    # differs from its future's (10.0); only lot + freeze come from the leg. An
    # underlying with no leg (reference indices like INDIA VIX / NIFTY 100) keeps
    # its cash row value.
    leg_economics = build_leg_economics(rows)
    universe = [
        replace(inst, lot_size=econ[0], sm_freeze_qty=econ[1])
        if (econ := leg_economics.get((inst.exch_id, inst.symbol_name.upper()))) is not None
        else inst
        for inst in by_identity.values()
    ]
    persisted = len(universe)
    return UniverseReport(
        universe=universe,
        parsed=parsed,
        persisted=persisted,
        legs_dropped=parsed - persisted,
        filtered=filtered,
        total_raw_rows=len(rows),
        integrity_failures=0,
        in_scope_count=parsed,
        out_of_scope_count=filtered,
        index_count=index_count,
        futures_count=futures_count,
        options_count=options_count,
        nse_count=nse_count,
        bse_count=bse_count,
        fno_eligible_symbols=eligible,
        options_eligible_symbols=options_symbols,
        options_eligible_security_ids=options_security_ids,
        index_options_symbols=index_options,
    )


def select_universe(
    rows: Sequence[Mapping[str, str]],
    *,
    reference_security_ids: frozenset[int] = frozenset(),
    extra_index_symbols: frozenset[str] = frozenset(),
) -> list[Instrument]:
    """Select the universe (index + F&O-stock SPOT + optional REFERENCE_ONLY constituents).

    See :func:`select_universe_report`; ``reference_security_ids`` (default
    empty) admits extra NSE equities by ``SECURITY_ID`` as REFERENCE_ONLY, and
    ``extra_index_symbols`` (default empty) admits extra INDEX rows by
    ``SYMBOL_NAME`` (the runtime-discovered NIFTY Total Market token).
    """
    return select_universe_report(
        rows,
        reference_security_ids=reference_security_ids,
        extra_index_symbols=extra_index_symbols,
    ).universe


def parse_nse_equities(rows: Iterable[Mapping[str, str]]) -> list[Instrument]:
    """Parse ONLY the NSE ``EQUITY`` rows of the master into :class:`Instrument`s.

    The subset :func:`brutex.universe_constituents.resolve_constituents` needs to
    match an index's constituents — including the **non-F&O long-name equities**
    the lean brute-force universe drops (their ``SYMBOL_NAME`` is the company name,
    matched by ISIN; DEC-DHAN-FNO-JOIN-KEY-001). Restricting to NSE EQUITY rows
    avoids parsing the ~89k derivative legs and never trips
    :func:`_instrument_type` on a non-equity row. Pure; O(n) over the master
    (DEC-NIFTY-TOTAL-MARKET-DISCOVER-001).
    """
    out: list[Instrument] = []
    for row in rows:
        if (
            row.get("INSTRUMENT", "").strip().upper() in _EQUITY_TYPES
            and row.get("EXCH_ID", "").strip().upper() == Exchange.NSE
        ):
            out.append(parse_instrument_row(row))
    return out


__all__ = [
    "DhanInstrumentError",
    "DiscoveredIndex",
    "IndexDiscoveryError",
    "UniverseReport",
    "discover_index",
    "fno_underlying_security_ids",
    "fno_underlying_symbols",
    "options_underlying_security_ids",
    "options_underlying_symbols",
    "parse_instrument_row",
    "parse_nse_equities",
    "select_universe",
    "select_universe_report",
    "validate_header",
]
