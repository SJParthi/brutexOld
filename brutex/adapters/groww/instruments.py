"""Groww instrument registry — CSV download, parsing, and domain mapping.

Downloads the Groww instrument master CSV, parses it into a registry of
:class:`GrowwInstrument` objects, and exposes O(1) lookups by
``symbol_name`` (the engine's primary key) and ``groww_symbol`` (the API key).

The CSV URL is ``docs/groww/api-reference/02-instruments.md``:
    ``https://growwapi-assets.groww.in/instruments/instrument.csv``

**No pandas.** Parsing uses the stdlib ``csv.DictReader`` so there is no
dependency on pandas (CLAUDE.md §4 "No pandas in hot paths"; the CSV load
is startup-only but we avoid the dep entirely to keep the dependency tree
clean for a library that requires no heavy data-science stack).

CSV columns:
    exchange, exchange_token, trading_symbol, groww_symbol, name,
    instrument_type, segment, series, isin, underlying_symbol,
    underlying_exchange_token, expiry_date, strike_price, lot_size,
    tick_size, freeze_quantity, is_reserved, buy_allowed, sell_allowed
"""

from __future__ import annotations

import csv
import io
import urllib.request
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

import structlog

from brutex.adapters.groww.constants import (
    BRUTE_FORCE_UNIVERSE,
    EXCHANGE_BSE,
    EXCHANGE_NSE,
    INSTRUMENT_CSV_URL,
    SEGMENT_CASH,
    SEGMENT_FNO,
)
from brutex.models import Exchange, Instrument, InstrumentType, Segment

_LOG = structlog.get_logger("brutex.adapters.groww.instruments")

#: Sentinel used in the CSV for missing values ("nan", "", "NaN").
_NULL_SENTINELS: frozenset[str] = frozenset({"nan", "NaN", "", "None", "none"})


@dataclass(frozen=True, slots=True)
class GrowwInstrument:
    """Groww-specific wrapping of a domain :class:`~brutex.models.Instrument`.

    Carries the API call metadata (groww_symbol, exchange string, segment
    string) alongside the broker-neutral domain model so the adapter can
    resolve ``symbol_name → (groww_symbol, exchange, segment)`` in O(1).

    Attributes
    ----------
    instrument
        The broker-neutral domain model (lake schema + engine key).
    groww_symbol
        Groww's unique instrument identifier (e.g. ``"NSE-NIFTY"``).
    api_exchange
        Exchange string for API calls (``"NSE"`` or ``"BSE"``).
    api_segment
        Segment string for API calls (``"CASH"`` or ``"FNO"``).
    """

    instrument: Instrument
    groww_symbol: str
    api_exchange: str
    api_segment: str


def _opt_str(value: str) -> str | None:
    """Return ``None`` if *value* is a null sentinel, else the stripped string."""
    stripped = value.strip()
    return None if stripped in _NULL_SENTINELS else stripped


def _opt_int(value: str) -> int | None:
    """Parse an optional integer CSV field."""
    stripped = value.strip()
    if stripped in _NULL_SENTINELS:
        return None
    try:
        return int(float(stripped))
    except ValueError:
        return None


def _opt_decimal(value: str) -> Decimal | None:
    """Parse an optional Decimal CSV field."""
    stripped = value.strip()
    if stripped in _NULL_SENTINELS:
        return None
    try:
        return Decimal(stripped)
    except InvalidOperation:
        return None


def _opt_date(value: str) -> date | None:
    """Parse an optional ISO date CSV field (``"YYYY-MM-DD"``)."""
    stripped = value.strip()
    if stripped in _NULL_SENTINELS:
        return None
    try:
        return date.fromisoformat(stripped)
    except ValueError:
        return None


def _parse_exchange(value: str) -> Exchange:
    """Map Groww CSV exchange string → :class:`~brutex.models.Exchange`."""
    upper = value.strip().upper()
    if upper == EXCHANGE_NSE:
        return Exchange.NSE
    if upper == EXCHANGE_BSE:
        return Exchange.BSE
    raise ValueError(f"Unsupported exchange {value!r}")


def _parse_instrument_type(value: str) -> InstrumentType:
    """Map Groww CSV ``instrument_type`` → :class:`~brutex.models.InstrumentType`."""
    upper = value.strip().upper()
    mapping: dict[str, InstrumentType] = {
        "EQ": InstrumentType.EQ,
        "IDX": InstrumentType.IDX,
        "FUT": InstrumentType.FUT,
        "CE": InstrumentType.CE,
        "PE": InstrumentType.PE,
    }
    if upper not in mapping:
        raise ValueError(f"Unknown instrument_type {value!r}")
    return mapping[upper]


def _parse_segment(csv_segment: str, itype: InstrumentType) -> Segment:
    """Map Groww CSV segment + instrument type → BruteX :class:`~brutex.models.Segment`.

    Groww uses ``CASH`` for both equities AND indices; BruteX distinguishes
    them via ``Segment.INDEX`` (same logic as DEC-INDEX-SEGMENT-PK-FIX-001).
    """
    if itype is InstrumentType.IDX:
        return Segment.INDEX
    upper = csv_segment.strip().upper()
    if upper == SEGMENT_CASH:
        return Segment.CASH
    if upper == SEGMENT_FNO:
        return Segment.FNO
    raise ValueError(f"Unknown segment {csv_segment!r}")


def _derive_symbol_name(groww_symbol: str, trading_symbol: str) -> str:
    """Derive the engine's bare ``symbol_name`` from the Groww symbol.

    For spot instruments: ``"NSE-NIFTY"`` → ``"NIFTY"``.
    For FNO contracts: use the trading_symbol directly.
    """
    parts = groww_symbol.split("-", 1)
    if len(parts) == 2 and parts[1]:
        return parts[1]
    return trading_symbol


def _row_to_groww_instrument(row: dict[str, str]) -> GrowwInstrument | None:
    """Parse one CSV row → :class:`GrowwInstrument`, or ``None`` to skip."""
    try:
        exchange = _parse_exchange(row["exchange"])
        itype = _parse_instrument_type(row["instrument_type"])
        segment = _parse_segment(row["segment"], itype)
        groww_symbol = row["groww_symbol"].strip()
        trading_symbol = row["trading_symbol"].strip()
        symbol_name = _derive_symbol_name(groww_symbol, trading_symbol)
        lot_size_raw = _opt_int(row["lot_size"]) or 0
        tick_size_raw = _opt_decimal(row["tick_size"]) or Decimal("0")
        instrument = Instrument(
            exch_id=exchange,
            security_id=int(float(row["exchange_token"].strip())),
            symbol_name=symbol_name,
            display_name=_opt_str(row.get("name", "")),
            instrument=itype,
            segment=segment,
            series=_opt_str(row.get("series", "")),
            isin=_opt_str(row.get("isin", "")),
            underlying_symbol=_opt_str(row.get("underlying_symbol", "")),
            underlying_security_id=_opt_int(row.get("underlying_exchange_token", "")),
            lot_size=lot_size_raw,
            tick_size=tick_size_raw,
            sm_freeze_qty=_opt_int(row.get("freeze_quantity", "")),
            sm_expiry_date=_opt_date(row.get("expiry_date", "")),
            strike_price=_opt_decimal(row.get("strike_price", "")),
            option_type=_opt_str(row.get("instrument_type", "")),
            asm_gsm_flag=bool(int(row.get("is_reserved", "0") or "0")),
        )
        api_segment = SEGMENT_FNO if segment is Segment.FNO else SEGMENT_CASH
        return GrowwInstrument(
            instrument=instrument,
            groww_symbol=groww_symbol,
            api_exchange=exchange.value,
            api_segment=api_segment,
        )
    except (KeyError, ValueError, ArithmeticError) as exc:
        _LOG.debug("groww.instruments.row_skip", row=row, reason=str(exc))
        return None


def _download_csv(url: str) -> str:  # pragma: no cover — network, smoke only
    """Download the instrument CSV and return as a raw string."""
    with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310
        return resp.read().decode("utf-8")


@dataclass(frozen=True, slots=True)
class InstrumentRegistry:
    """O(1) lookup registry for Groww instruments.

    Attributes
    ----------
    by_symbol_name
        Maps ``symbol_name`` (bare engine key) → :class:`GrowwInstrument`.
    by_groww_symbol
        Maps ``groww_symbol`` (API key) → :class:`GrowwInstrument`.
    all_instruments
        Tuple of all registered :class:`GrowwInstrument` objects.
    """

    by_symbol_name: dict[str, GrowwInstrument]
    by_groww_symbol: dict[str, GrowwInstrument]
    all_instruments: tuple[GrowwInstrument, ...]


def build_registry(
    csv_text: str,
    *,
    instrument_types: frozenset[str] | None = None,
    exchanges: frozenset[str] | None = None,
) -> InstrumentRegistry:
    """Parse *csv_text* and return an :class:`InstrumentRegistry`.

    Parameters
    ----------
    csv_text:
        Raw CSV content (UTF-8 string) from the Groww instrument master.
    instrument_types:
        If provided, only rows with matching ``instrument_type`` are included.
        Example: ``frozenset({"IDX"})`` for index-only.
    exchanges:
        If provided, only rows matching these exchange strings are included.

    Returns
    -------
    InstrumentRegistry
        O(1)-indexed registry of :class:`GrowwInstrument` objects.
    """
    reader = csv.DictReader(io.StringIO(csv_text))
    by_symbol: dict[str, GrowwInstrument] = {}
    by_groww: dict[str, GrowwInstrument] = {}
    items: list[GrowwInstrument] = []
    for row in reader:
        if instrument_types is not None:
            if row.get("instrument_type", "").strip().upper() not in instrument_types:
                continue
        if exchanges is not None:
            if row.get("exchange", "").strip().upper() not in exchanges:
                continue
        gi = _row_to_groww_instrument(row)
        if gi is None:
            continue
        by_symbol[gi.instrument.symbol_name] = gi
        by_groww[gi.groww_symbol] = gi
        items.append(gi)
    _LOG.info("groww.instruments.registry_built", count=len(items))
    return InstrumentRegistry(
        by_symbol_name=by_symbol,
        by_groww_symbol=by_groww,
        all_instruments=tuple(items),
    )


def build_brute_force_registry() -> InstrumentRegistry:
    """Build a hardcoded registry for the Track-1 brute-force universe.

    Returns a minimal :class:`InstrumentRegistry` from the constant
    ``BRUTE_FORCE_UNIVERSE`` table without a CSV download. Used as a
    fast-startup alternative when only the 4 brute-force instruments are needed.

    The ``security_id`` is set to ``0`` (placeholder) until the Phase 0 probe
    resolves real ``exchange_token`` values from the live CSV.
    """
    items: list[GrowwInstrument] = []
    by_symbol: dict[str, GrowwInstrument] = {}
    by_groww: dict[str, GrowwInstrument] = {}
    for sym, (groww_sym, exch_str, seg_str) in BRUTE_FORCE_UNIVERSE.items():
        exch = Exchange.NSE if exch_str == EXCHANGE_NSE else Exchange.BSE
        instrument = Instrument(
            exch_id=exch,
            security_id=0,  # resolved from CSV at Phase 0 probe
            symbol_name=sym,
            display_name=sym,
            instrument=InstrumentType.IDX,
            segment=Segment.INDEX,
            series=None,
            isin=None,
            underlying_symbol=None,
            underlying_security_id=None,
            lot_size=0,
            tick_size=Decimal("0.05"),
            sm_freeze_qty=None,
            sm_expiry_date=None,
            strike_price=None,
        )
        gi = GrowwInstrument(
            instrument=instrument,
            groww_symbol=groww_sym,
            api_exchange=exch_str,
            api_segment=seg_str,
        )
        items.append(gi)
        by_symbol[sym] = gi
        by_groww[groww_sym] = gi
    return InstrumentRegistry(
        by_symbol_name=by_symbol,
        by_groww_symbol=by_groww,
        all_instruments=tuple(items),
    )


def load_registry_from_csv(
    *,
    instrument_types: frozenset[str] | None = None,
    exchanges: frozenset[str] | None = None,
    csv_url: str = INSTRUMENT_CSV_URL,
    _csv_text: str | None = None,
) -> InstrumentRegistry:
    """Download the Groww instrument CSV and return an :class:`InstrumentRegistry`.

    Parameters
    ----------
    instrument_types, exchanges:
        Optional filters passed to :func:`build_registry`.
    csv_url:
        Override the default CSV URL (useful for integration tests).
    _csv_text:
        If provided, skip the download and use this text directly (for tests).

    Returns
    -------
    InstrumentRegistry
    """
    text = _csv_text if _csv_text is not None else _download_csv(csv_url)
    return build_registry(text, instrument_types=instrument_types, exchanges=exchanges)


__all__ = [
    "GrowwInstrument",
    "InstrumentRegistry",
    "build_brute_force_registry",
    "build_registry",
    "load_registry_from_csv",
]
