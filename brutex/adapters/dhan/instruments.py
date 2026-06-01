"""Dhan instrument-master parser — detailed scrip-master CSV → ``Instrument``.

Maps a row of Dhan's ``api-scrip-master-detailed.csv``
(``docs/dhan-ref/09``) into the vendor-neutral
:class:`brutex.models.Instrument` (CLAUDE.md §11 COMMON — same output model
the rest of BruteX consumes). The full verified column set is captured in
``docs/dhan-ref/09``; every field BruteX needs — identity, classification,
**lot size, tick size, freeze quantity**, expiry, strike, underlying
linkage — is mapped here. ``SECURITY_ID`` is the numeric key (never
hardcoded; resolved from the CSV).

The **brute-force universe** is selected by :func:`select_universe`:
- every **index** (NSE indices + BSE SENSEX + INDIA VIX), and
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
  reason the admitted equity's ``trading_symbol`` is taken from its
  ``UNDERLYING_SYMBOL`` ticker so a stock's identity matches how its F&O
  legs reference it (ground truth: real Dhan ``api-scrip-master-detailed``,
  219,082 rows, decoded 2026-06-01; 229 F&O underlyings).

Derivative legs (options/futures) are parsed for eligibility then dropped
from the lean universe (DEC-INSTRUMENT-UNIVERSE-FAST-001), recoverable on
demand. This module is pure (no I/O); the CSV download lives in the loader.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
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


class DhanInstrumentError(BruteXError):
    """A scrip-master row/header violates the documented shape."""


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
    options/futures carry expiry + strike. ``SECURITY_ID`` becomes both the
    numeric ``exchange_token`` and the string ``vendor_symbol`` (the Dhan
    API key).

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
    segment = (
        Segment.FNO
        if itype in {InstrumentType.FUT, InstrumentType.CE, InstrumentType.PE}
        else Segment.CASH
    )
    underlying = _opt_str(row.get("UNDERLYING_SYMBOL"))
    # An F&O-eligible equity carries the long company name in SYMBOL_NAME and
    # the ticker in UNDERLYING_SYMBOL; prefer the ticker so the spot's identity
    # matches how its derivative legs reference it (DEC-DHAN-FNO-JOIN-KEY-001).
    # Indices + non-F&O equities have no UNDERLYING_SYMBOL → keep SYMBOL_NAME;
    # derivative legs keep their own contract SYMBOL_NAME, not the underlying.
    symbol_name = row["SYMBOL_NAME"].strip()
    trading_symbol = (
        underlying if (itype is InstrumentType.EQ and underlying is not None) else symbol_name
    )
    return Instrument(
        exchange=Exchange(exch),
        exchange_token=_to_int(security_id, "SECURITY_ID"),
        trading_symbol=trading_symbol,
        vendor_symbol=security_id,
        name=_opt_str(row.get("DISPLAY_NAME")),
        instrument_type=itype,
        segment=segment,
        series=_opt_str(row.get("SERIES")),
        isin=_opt_str(row.get("ISIN")),
        underlying_symbol=underlying,
        underlying_exchange_token=_opt_int(row.get("UNDERLYING_SECURITY_ID")),
        lot_size=_to_int(row["LOT_SIZE"], "LOT_SIZE"),
        tick_size=_to_decimal(row["TICK_SIZE"], "TICK_SIZE"),
        freeze_quantity=_opt_int(row.get("SM_FREEZE_QTY")),
        expiry_date=_opt_expiry(row.get("SM_EXPIRY_DATE")),
        strike_price=_opt_decimal(row.get("STRIKE_PRICE")),
        is_reserved=bool(_opt_str(row.get("ASM_GSM_FLAG"))),
        buy_allowed=True,
        sell_allowed=True,
    )


def fno_underlying_symbols(rows: Iterable[Mapping[str, str]]) -> frozenset[str]:
    """Collect the set of symbols that have F&O derivatives (the eligible set).

    A symbol is F&O-eligible if it appears as ``UNDERLYING_SYMBOL`` on any
    futures/option row.
    """
    eligible: set[str] = set()
    for row in rows:
        itype = row.get("INSTRUMENT", "").strip().upper()
        if itype in _FUTURE_TYPES or itype in _OPTION_TYPES:
            underlying = _opt_str(row.get("UNDERLYING_SYMBOL"))
            if underlying is not None:
                eligible.add(underlying)
    return frozenset(eligible)


@dataclass(frozen=True, slots=True)
class UniverseReport:
    """The selected universe + parse accounting (for dashboard/cli summaries)."""

    universe: list[Instrument]
    parsed: int  # in-scope (NSE/BSE) rows seen
    persisted: int  # universe size = index + F&O-stock SPOT
    legs_dropped: int  # in-scope rows not in the universe (derivative legs + non-F&O equity)
    filtered: int  # out-of-scope rows skipped (MCX, etc.)


def select_universe_report(rows: Sequence[Mapping[str, str]]) -> UniverseReport:
    """Select the brute-force universe and report the parse accounting.

    Two-pass: collect the F&O-eligible underlying tickers, then keep (a) all
    NSE/BSE **index** rows and (b) all **equity SPOT** rows whose own
    ``UNDERLYING_SYMBOL`` ticker is F&O-eligible (NOT ``SYMBOL_NAME`` — that
    is the long company name; see DEC-DHAN-FNO-JOIN-KEY-001). Derivative legs
    + non-F&O equities are counted as ``legs_dropped`` (excluded from the lean
    universe, recoverable on demand); out-of-scope-exchange rows are counted
    as ``filtered``.
    """
    eligible = fno_underlying_symbols(rows)
    universe: list[Instrument] = []
    parsed = 0
    filtered = 0
    for row in rows:
        if row.get("EXCH_ID", "").strip().upper() not in (Exchange.NSE, Exchange.BSE):
            filtered += 1
            continue
        parsed += 1
        itype = row.get("INSTRUMENT", "").strip().upper()
        if itype in _INDEX_TYPES or (
            itype in _EQUITY_TYPES and _opt_str(row.get("UNDERLYING_SYMBOL")) in eligible
        ):
            universe.append(parse_instrument_row(row))
    persisted = len(universe)
    return UniverseReport(
        universe=universe,
        parsed=parsed,
        persisted=persisted,
        legs_dropped=parsed - persisted,
        filtered=filtered,
    )


def select_universe(rows: Sequence[Mapping[str, str]]) -> list[Instrument]:
    """Select the brute-force universe (index + F&O-stock SPOT); see :func:`select_universe_report`."""
    return select_universe_report(rows).universe


__all__ = [
    "DhanInstrumentError",
    "UniverseReport",
    "fno_underlying_symbols",
    "parse_instrument_row",
    "select_universe",
    "select_universe_report",
    "validate_header",
]
