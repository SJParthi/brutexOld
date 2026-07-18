"""Dhan scrip-master parser + instrument index — pure keying/segment-mapping law.

DEC-DHAN-ADAPTER-001. Dhan publishes a compact ``api-scrip-master.csv`` and a
detailed ``api-scrip-master-detailed.csv`` (VERIFIED existence; the exact
``SEM_``-prefixed column names + order are **UNVERIFIED**). This module reads the
CSV **BY COLUMN NAME** (header-driven, order-tolerant) into lean
:class:`DhanScripRow` records and keys them for O(1) resolution
(``by_security_id`` / ``by_exchange_symbol``), deriving each row's
``exchangeSegment`` token from the ``(EXCH_ID, SEGMENT)`` pair.

The depended-on header names live in
:data:`~brutex.adapters.dhan.constants.DHAN_SCRIP_HEADERS_UNVERIFIED` (stamped
``_UNVERIFIED``); a rename in a real scrip-master fails LOUD
(:class:`~brutex.models.InstrumentFormatError`), never a silent mis-parse (Golden
Rule #1), and the drift-guard test pins the names.

A pure, I/O-free LEAF: it parses CSV **text** with the stdlib :mod:`csv` module (no
network download — the real downloader is a separate, out-of-scope future seam), so
a future ``dhan_instruments_rust.py`` facade could wrap the keying law 1:1
(DEC-DHAN-RUST-FACADE-001).
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from brutex.adapters.dhan.constants import (
    DHAN_ALLOWED_HOSTS,
    DHAN_EXCH_SEGMENT_MAP,
    DHAN_SCRIP_HEADERS_UNVERIFIED,
)
from brutex.models import InstrumentFormatError


@dataclass(frozen=True, slots=True)
class DhanScripRow:
    """One normalised Dhan scrip-master row (a lean vendor record).

    Deliberately NOT the engine :class:`~brutex.models.Instrument` (whose strict
    constructor rejects a lot-less derivative etc.) — the scrip master is a
    reference catalogue of every listed instrument, so this lean record preserves
    what Dhan lists without the engine's tradability invariants.

    Attributes
    ----------
    security_id
        The numeric ``SEM_SMST_SECURITY_ID`` — the addressing key every Dhan
        candle/chain request uses.
    trading_symbol
        The vendor trading symbol.
    exchange_segment
        The derived ``exchangeSegment`` token (e.g. ``NSE_EQ`` / ``IDX_I``).
    instrument_type
        The raw ``SEM_EXCH_INSTRUMENT_TYPE`` (e.g. ``EQUITY`` / ``INDEX`` / ``OPTIDX``).
    series, expiry_date, option_type
        Raw optional fields (``None`` when blank).
    strike_price, tick_size
        Exact Decimal rupees, or ``None`` when blank.
    lot_size
        Integer lot units, or ``None`` when blank.
    """

    security_id: int
    trading_symbol: str
    exchange_segment: str
    instrument_type: str
    series: str | None
    expiry_date: str | None
    option_type: str | None
    strike_price: Decimal | None
    lot_size: int | None
    tick_size: Decimal | None


def exchange_segment_for(exch_id: str, segment: str) -> str:
    """Map a ``(EXCH_ID, SEGMENT)`` scrip-master pair to a Dhan ``exchangeSegment`` token.

    The segment-token VALUES are VERIFIED — DhanHQ v2 docs (e.g. ``"NSE_EQ"``,
    ``"IDX_I"``); the single-letter ``(EXCH_ID, SEGMENT)`` value-code KEYS
    (``("NSE","E")``, ``("NSE","I")``, …) come from the **UNVERIFIED**
    scrip-master columns, so the code ENCODING is model-inferred — probe-gated
    under the scrip-master gate. Raises
    :class:`~brutex.models.InstrumentFormatError` for an unknown pair (fail-loud
    — never silently default a segment, so an unexpected code can never
    silently mis-parse).
    """
    key = (exch_id.strip().upper(), segment.strip().upper())
    token = DHAN_EXCH_SEGMENT_MAP.get(key)
    if token is None:
        raise InstrumentFormatError(f"unknown Dhan (exch_id, segment) pair: {key!r}")
    return token


def is_allowed_scrip_host(url: str) -> bool:
    """True iff *url* is ``https`` AND its host is on the Dhan SSRF allowlist.

    A future real downloader MUST call this before fetching (Golden Rule #1 — never
    follow an arbitrary host, never a plaintext/MITM-able scheme). Mirrors the Groww
    sibling ``instrument_loader._check_url``, which refuses BOTH a non-https scheme
    AND a non-allowlisted host. Pure string check; no network.
    """
    parsed = urlsplit(url)
    return parsed.scheme == "https" and parsed.hostname in DHAN_ALLOWED_HOSTS


def _optional_str(row: Mapping[str, str], header: str) -> str | None:
    """Return the trimmed cell for *header*, or ``None`` when the column is absent/blank."""
    raw = row.get(header)
    if raw is None:
        return None
    text = raw.strip()
    return text or None


def _optional_decimal(row: Mapping[str, str], header: str, *, field: str) -> Decimal | None:
    """Parse an optional Decimal cell for *header*, or ``None`` when absent/blank."""
    text = _optional_str(row, header)
    if text is None:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise InstrumentFormatError(f"unparseable {field}: {text!r}") from exc


def _optional_int(row: Mapping[str, str], header: str, *, field: str) -> int | None:
    """Parse an optional integer cell for *header*, or ``None`` when absent/blank.

    Tolerates a float-shaped lot value (e.g. ``"75.0"``) by rounding through Decimal.
    """
    text = _optional_str(row, header)
    if text is None:
        return None
    try:
        return int(Decimal(text))
    except (InvalidOperation, ValueError) as exc:
        raise InstrumentFormatError(f"unparseable {field}: {text!r}") from exc


def parse_scrip_master(csv_text: str) -> list[DhanScripRow]:
    """Parse Dhan scrip-master CSV **text** into :class:`DhanScripRow` records (header-driven).

    Reads columns BY NAME from :data:`~brutex.adapters.dhan.constants.\
    DHAN_SCRIP_HEADERS_UNVERIFIED`, tolerating column order AND whitespace-padded
    header names (headers are stripped for BOTH the presence check and the row
    reads — the header names are UNVERIFIED, so a padded real header must not
    silently drop data; Golden Rule #1). A missing REQUIRED header (security_id /
    trading_symbol / instrument_type / exch_id / segment) fails LOUD; the optional
    derivative/economics columns default to ``None`` when absent. An unparseable
    required ``security_id`` raises (a genuine shape problem).

    Raises
    ------
    InstrumentFormatError
        Empty CSV (no header), a missing required header, an unknown segment pair, or
        an unparseable required field.
    """
    reader = csv.DictReader(io.StringIO(csv_text))
    if reader.fieldnames is None:
        raise InstrumentFormatError("Dhan scrip-master CSV has no header row")
    header = {name.strip() for name in reader.fieldnames if name is not None}
    required = ("security_id", "trading_symbol", "instrument_type", "exch_id", "segment")
    for logical in required:
        column = DHAN_SCRIP_HEADERS_UNVERIFIED[logical]
        if column not in header:
            raise InstrumentFormatError(
                f"Dhan scrip-master missing required column {column!r} (logical {logical!r}); "
                f"headers are UNVERIFIED — see DHAN_SCRIP_HEADERS_UNVERIFIED"
            )

    cols = DHAN_SCRIP_HEADERS_UNVERIFIED
    rows: list[DhanScripRow] = []
    for raw_row in reader:
        # Strip the row KEYS exactly like the header presence check above — a
        # whitespace-padded header (e.g. " SEM_SMST_SECURITY_ID") passes the
        # required-header check on the stripped set, so the row read must resolve
        # the same canonical name (else EVERY row would silently skip on the raw
        # padded key — the round-1 silent-data-mismatch finding).
        row = {k.strip(): v for k, v in raw_row.items() if k is not None}
        sec_text = (row.get(cols["security_id"]) or "").strip()
        if not sec_text:
            continue  # skip a blank / continuation row (no addressing key)
        try:
            security_id = int(Decimal(sec_text))
        except (InvalidOperation, ValueError) as exc:
            raise InstrumentFormatError(f"unparseable security_id: {sec_text!r}") from exc
        exch_id = (row.get(cols["exch_id"]) or "").strip()
        segment = (row.get(cols["segment"]) or "").strip()
        rows.append(
            DhanScripRow(
                security_id=security_id,
                trading_symbol=(row.get(cols["trading_symbol"]) or "").strip(),
                exchange_segment=exchange_segment_for(exch_id, segment),
                instrument_type=(row.get(cols["instrument_type"]) or "").strip(),
                series=_optional_str(row, cols["series"]),
                expiry_date=_optional_str(row, cols["expiry_date"]),
                option_type=_optional_str(row, cols["option_type"]),
                strike_price=_optional_decimal(row, cols["strike_price"], field="strike_price"),
                lot_size=_optional_int(row, cols["lot_size"], field="lot_size"),
                tick_size=_optional_decimal(row, cols["tick_size"], field="tick_size"),
            )
        )
    return rows


@dataclass(frozen=True, slots=True)
class DhanInstrumentIndex:
    """O(1) lookup index over parsed :class:`DhanScripRow` records.

    Attributes
    ----------
    rows
        Every parsed scrip row, in input order.
    by_security_id
        ``security_id → DhanScripRow`` (last row wins on a duplicate id).
    by_exchange_symbol
        ``(exchange_segment, trading_symbol) → security_id`` for symbol→id resolution.
    """

    rows: tuple[DhanScripRow, ...]
    by_security_id: Mapping[int, DhanScripRow]
    by_exchange_symbol: Mapping[tuple[str, str], int]

    @classmethod
    def build(cls, rows: Iterable[DhanScripRow]) -> DhanInstrumentIndex:
        """Build the index from parsed rows — O(rows), deterministic."""
        materialised = tuple(rows)
        by_security_id: dict[int, DhanScripRow] = {}
        by_exchange_symbol: dict[tuple[str, str], int] = {}
        for row in materialised:
            by_security_id[row.security_id] = row
            if row.trading_symbol:
                by_exchange_symbol[(row.exchange_segment, row.trading_symbol)] = row.security_id
        return cls(
            rows=materialised,
            by_security_id=by_security_id,
            by_exchange_symbol=by_exchange_symbol,
        )

    def resolve_security_id(self, exchange_segment: str, trading_symbol: str) -> int | None:
        """Return the ``security_id`` for a ``(segment, trading_symbol)`` pair, or ``None``."""
        return self.by_exchange_symbol.get((exchange_segment, trading_symbol))


__all__ = [
    "DhanInstrumentIndex",
    "DhanScripRow",
    "exchange_segment_for",
    "is_allowed_scrip_host",
    "parse_scrip_master",
]
