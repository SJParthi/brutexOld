"""Pure CSV byte renderer for the pinned TickVault cross-verify contract.

DEC-CROSSVERIFY-PRODUCER-001 §1.1/§1.2. Deterministic bytes: UTF-8, NO BOM,
byte-exact header, LF line terminator with a trailing newline on the LAST row,
``csv.QUOTE_MINIMAL`` with the no-quote-ever property (no legal cell can carry
``, " \\n \\r`` — any such cell REFUSES the file; a control character in any
cell likewise refuses — the canonical symbol-allowlist mirror). Prices render via the ONE
2dp law ``round_money(Decimal(str(stored)))`` (ROUND_HALF_UP — lossless for
post-snap stored values, canonical-2dp for legacy pre-snap noise floats;
NEVER ``f"{v:.2f}"`` on the raw float, the ``bxFixed2`` IEEE half-boundary
bug class). ``oi`` renders the EMPTY string for spot and the stored int for
futures (``0`` renders ``0``); a NULL-OI futures row REFUSES — a partial day
is a withheld file, never an empty cell in a published futures file.

The renderer writes through :class:`~brutex.dashboard._csv_utils.FormulaSafeWriter`
(the COMMON guard is structurally present) AND pre-validates every cell with
``csv_safe(cell) == cell`` — a would-be mutation raises
:class:`ExportContractError` (file withheld + counted; never a mutated byte).

O(rows) per file; zero I/O — bytes in, bytes out.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Final

from brutex.dashboard._csv_utils import FormulaSafeWriter, csv_safe
from brutex.data.session import IST
from brutex.models import BruteXError
from brutex.money import round_money

if TYPE_CHECKING:  # pragma: no cover — typing-only imports
    from collections.abc import Sequence

    from brutex.calendars.nse import TradingSession
    from brutex.crossverify.canonical import CanonicalIdentity

#: The byte-exact header line (without the terminator). Golden-pinned.
CSV_HEADER: Final[str] = "symbol,timestamp_ist,open,high,low,close,volume,oi"

#: Every row — including the last — ends with exactly one LF.
LINE_TERMINATOR: Final[str] = "\n"

#: Cell characters that would force CSV quoting — refused, never quoted.
_FORBIDDEN_CELL_CHARS: Final[frozenset[str]] = frozenset(',"\n\r')

#: Control characters (C0 block + DEL) — refused in EVERY cell, the render-
#: side mirror of the canonical symbol allowlist (F5, round-1 security review).
_CONTROL_CHAR_MAX: Final[int] = 0x1F
_DEL_CHAR: Final[int] = 0x7F

#: The spot instrument classes (``oi`` must be ``None`` → rendered ``""``).
_SPOT_CLASSES: Final[frozenset[str]] = frozenset({"index_spot", "cash_spot"})


class ExportContractError(BruteXError):
    """A row/cell violates the pinned byte contract — the file is REFUSED."""


@dataclass(frozen=True, slots=True)
class ExportRow:
    """One 1-minute bar staged for rendering (values as stored in the lake)."""

    ts_utc: datetime  # tz-aware UTC minute-open
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    oi: int | None  # ``None`` ⇔ spot


def render_price_cell(stored: float | Decimal) -> str:
    """Render one stored price to the pinned 2dp cell — the ONE price render law.

    ``f"{round_money(Decimal(str(stored))):.2f}"`` — for a post-snap stored
    Float64 this is lossless (the ``parquet_writer`` parity argument); for a
    legacy pre-snap noise float (``14020.7999…``) it reproduces the canonical
    2dp the writer itself would snap to (``14020.80``). O(1).
    """
    value = stored if isinstance(stored, Decimal) else Decimal(str(stored))
    return f"{round_money(value):.2f}"


def render_oi_cell(oi: int | None) -> str:
    """``""`` for ``None`` (spot — absent is never fabricated ``0``), else the int."""
    return "" if oi is None else str(oi)


def render_timestamp_ist(ts_utc: datetime) -> str:
    """Render a tz-aware UTC minute-open as ``YYYY-MM-DD HH:MM:SS`` IST."""
    return ts_utc.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S")


def _validate_prices(row: ExportRow) -> None:
    """Refuse a NaN / infinite / non-positive price."""
    for field_name in ("open", "high", "low", "close"):
        value: Decimal = getattr(row, field_name)
        if not value.is_finite() or value <= 0:
            raise ExportContractError(
                f"row {row.ts_utc.isoformat()}: {field_name}={value} is not a finite positive price"
            )


def _validate_row(row: ExportRow, *, identity: CanonicalIdentity, session: TradingSession) -> None:
    """Apply every per-row contract law (§1.1) — raise on the first violation."""
    if row.ts_utc.tzinfo is None:
        raise ExportContractError(f"row {row.ts_utc.isoformat()}: naive timestamp (must be UTC)")
    _validate_prices(row)
    if row.volume < 0:
        raise ExportContractError(f"row {row.ts_utc.isoformat()}: negative volume {row.volume}")
    if identity.instrument_class in _SPOT_CLASSES:
        if row.oi is not None:
            raise ExportContractError(
                f"row {row.ts_utc.isoformat()}: spot row carries oi={row.oi} "
                "(the lake stores None for spot)"
            )
    else:
        # A NULL-OI futures row must never reach a published futures file —
        # the FUT gate withholds on oi_null_settling FIRST; this refusal is
        # the kill-switched-gate tripwire (§1.1 oi law).
        if row.oi is None:
            raise ExportContractError(
                f"row {row.ts_utc.isoformat()}: futures row with NULL oi — "
                "a partial (settling) day, never an empty oi cell"
            )
        if row.oi < 0:
            raise ExportContractError(f"row {row.ts_utc.isoformat()}: negative oi {row.oi}")
    ist = row.ts_utc.astimezone(IST)
    in_session = (
        ist.date() == session.date
        and session.open_time_ist <= ist.time() < session.close_time_ist
        and ist.second == 0
        and ist.microsecond == 0
    )
    if not in_session:
        # Should not exist post-ingest-filter — refusal is the tripwire, not a drop.
        raise ExportContractError(
            f"row {ist.strftime('%Y-%m-%d %H:%M:%S')} IST is outside the session "
            f"window [{session.open_time_ist}, {session.close_time_ist}) of "
            f"{session.date.isoformat()} (or not a clean minute-open)"
        )


def _validate_cells(cells: Sequence[str], *, ts_label: str) -> None:
    """Refuse any cell ``csv_safe`` would mutate, that would force quoting,
    or that carries a control character (the mirror of the canonical symbol
    allowlist — a control char in a published byte is never legal)."""
    for cell in cells:
        if csv_safe(cell) != cell:
            raise ExportContractError(
                f"row {ts_label}: cell {cell!r} would be mutated by the "
                "formula-injection guard — refusing (never a mutated byte)"
            )
        if any(ch in _FORBIDDEN_CELL_CHARS for ch in cell):
            raise ExportContractError(
                f"row {ts_label}: cell {cell!r} contains a CSV metacharacter — "
                "refusing (the no-quote-ever property)"
            )
        if any(ord(ch) <= _CONTROL_CHAR_MAX or ord(ch) == _DEL_CHAR for ch in cell):
            raise ExportContractError(
                f"row {ts_label}: cell {cell!r} contains a control character — "
                "refusing (mirrors the canonical symbol allowlist)"
            )


def render_day_csv(
    *,
    identity: CanonicalIdentity,
    rows: Sequence[ExportRow],
    session: TradingSession,
) -> bytes:
    """Render one (symbol, day) file to the pinned contract bytes, or REFUSE.

    Refuses (:class:`ExportContractError`): empty *rows*; non-increasing /
    duplicate minutes; NaN / infinite / non-positive prices; negative
    volume/oi; ``oi=None`` on a futures identity / ``oi`` present on spot;
    any row outside the session's ``[open, close)`` IST window (or off the
    minute grid); any cell the formula guard would mutate or that carries a
    CSV metacharacter. Writes through :class:`FormulaSafeWriter` AFTER the
    validations. O(rows); deterministic bytes.
    """
    if not rows:
        raise ExportContractError(
            f"{identity.canonical_symbol}: empty row set — nothing to render "
            "(an absent day is a withheld file, never an empty one)"
        )
    buf = io.StringIO()
    writer = FormulaSafeWriter(csv.writer(buf, lineterminator=LINE_TERMINATOR))
    writer.writerow(CSV_HEADER.split(","))
    previous: datetime | None = None
    for row in rows:
        _validate_row(row, identity=identity, session=session)
        if previous is not None and row.ts_utc <= previous:
            raise ExportContractError(
                f"rows not strictly increasing at {row.ts_utc.isoformat()} "
                f"(previous {previous.isoformat()}) — duplicate/misordered minutes"
            )
        previous = row.ts_utc
        ts_cell = render_timestamp_ist(row.ts_utc)
        cells = [
            identity.canonical_symbol,
            ts_cell,
            render_price_cell(row.open),
            render_price_cell(row.high),
            render_price_cell(row.low),
            render_price_cell(row.close),
            str(row.volume),
            render_oi_cell(row.oi),
        ]
        _validate_cells(cells, ts_label=ts_cell)
        writer.writerow(cells)
    return buf.getvalue().encode("utf-8")


__all__ = [
    "CSV_HEADER",
    "LINE_TERMINATOR",
    "ExportContractError",
    "ExportRow",
    "render_day_csv",
    "render_oi_cell",
    "render_price_cell",
    "render_timestamp_ist",
]
