"""Rolling-ATM option parser — parallel COLUMN arrays payload → rolling rows.

DEC-DHAN-ROLLING-OPTIONS-001 (ADR-0022). Dhan's rolling-option endpoint returns the
requested per-bar columns as parallel COLUMN arrays (like the spot ``/charts/intraday``
shape, plus the option extras ``oi`` / ``iv`` / ``strike`` / ``spot``)::

    {"timestamp": [...], "open": [...], "high": [...], "low": [...], "close": [...],
     "volume": [...], "oi": [...], "iv": [...], "strike": [...], "spot": [...]}

Each index-aligned row is turned into a :class:`RollingOptionBar` — the OHLCV core is
assembled by the shared :func:`~brutex.data.candle_fields.assemble_candle` (CLAUDE.md
§11 COMMON — snapping / OHLC-clamp / empty-skip identical to Groww + Dhan-spot), and
the rolling extras are attached:

* **``iv`` / ``strike`` (``resolved_strike``) / ``spot`` are PER-ROW, not partition
  keys** — the ATM-relative selector resolves to a DRIFTING absolute strike + spot
  per bar-date (the crux of the rolling-ATM honesty law; see
  :mod:`.rolling_option_writer`). An ABSENT optional column → a null field (§7 gate:
  "absent field → null column"), never a fabricated value.
* **``resolved_expiry_date`` / ``resolved_expiry_source``** — the resolved expiry
  drifts too, so it is per-ROW metadata: taken from a vendor ``expiry`` echo column
  when present (``source="vendor"``), else from an injected calendar deriver
  (``source="derived"``), else honestly ``unresolved`` (date ``None``) — the CORE
  pass ships WITHOUT a verified NSE-calendar deriver (a §7 OPEN item), so absent a
  vendor echo the source is ``unresolved`` until the follow-up wires the deriver.
  NEVER a fabricated expiry (Golden Rule #1).

* **Column arrays, not rows.** The REQUIRED columns (``timestamp`` + OHLC) must all be
  present and every PRESENT column's length must match; a missing required column or a
  length mismatch is a :class:`~brutex.adapters.dhan.errors.DhanVendorDriftError` that
  REJECTS the chunk (never silently mis-pairs a price with a wrong timestamp).
* **Unix-EPOCH-seconds UTC timestamps** — parsed by the shared
  :func:`~brutex.adapters.dhan.parser.parse_epoch_utc` (09:15 IST ⇔ 03:45 UTC, no DST).

An EMPTY payload (no columns, or all-empty arrays) is honest NO-DATA → ``[]``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Final

from brutex.adapters.dhan.errors import DhanVendorDriftError
from brutex.adapters.dhan.parser import parse_epoch_utc
from brutex.data.candle_fields import (
    CandleParseError,
    EmptyCandleRow,
    assemble_candle,
    has_no_price,
    is_absent,
)
from brutex.data.models import CandleBar

#: A calendar deriver seam: ``(bar_date_ist, expiry_flag, expiry_code) -> date | None``.
#: Injected (the CORE ships none — a §7 OPEN item); returning ``None`` is an honest
#: abstain (out-of-coverage), never a fabricated date.
DeriveExpiry = Callable[[date, str, int], date | None]

#: Resolved-expiry provenance labels. ``vendor`` = Dhan echoed the date; ``derived``
#: = an injected NSE-calendar deriver computed it; ``unresolved`` = neither (the
#: honest CORE state before the verified deriver lands — date ``None``, NEVER a
#: fabricated expiry).
RESOLVED_EXPIRY_SOURCE_VENDOR: Final[str] = "vendor"
RESOLVED_EXPIRY_SOURCE_DERIVED: Final[str] = "derived"
RESOLVED_EXPIRY_SOURCE_UNRESOLVED: Final[str] = "unresolved"

#: The valid ``resolved_expiry_source`` set (the model validates membership).
RESOLVED_EXPIRY_SOURCES: Final[frozenset[str]] = frozenset(
    {
        RESOLVED_EXPIRY_SOURCE_VENDOR,
        RESOLVED_EXPIRY_SOURCE_DERIVED,
        RESOLVED_EXPIRY_SOURCE_UNRESOLVED,
    }
)

#: The REQUIRED columns — a missing one is vendor drift (can't build honest bars).
_REQUIRED_COLUMNS: Final[tuple[str, ...]] = ("timestamp", "open", "high", "low", "close")

#: The OPTIONAL per-row columns — an absent one becomes a null field (§7 gate).
_OPTIONAL_COLUMNS: Final[tuple[str, ...]] = ("volume", "oi", "iv", "strike", "spot", "expiry")

#: Every column the parser may read.
_ALL_COLUMNS: Final[tuple[str, ...]] = (*_REQUIRED_COLUMNS, *_OPTIONAL_COLUMNS)

#: Fixed IST offset (Asia/Kolkata has no DST) — for the bar's IST date used by the
#: expiry deriver. Mirrors the SDK's fixed ``timezone(timedelta(hours=5, minutes=30))``.
_IST: Final[timezone] = timezone(timedelta(hours=5, minutes=30))


@dataclass(frozen=True, slots=True)
class RollingOptionBar:
    """One rolling-ATM option 1-minute bar (one requested selector, one minute).

    The OHLCV core mirrors :class:`~brutex.data.models.OptionBar`; the rolling
    extras (``iv`` / ``spot`` / ``resolved_strike`` / ``resolved_expiry_date`` /
    ``resolved_expiry_source``) are the DRIFTING per-row metadata the ATM-relative
    selector resolves to per bar-date — NEVER partition keys.

    Attributes
    ----------
    timestamp
        UTC timezone-aware datetime at the bar's open.
    open, high, low, close
        Decimal price fields (rupees), 2dp-snapped only at the lake-write boundary.
    volume
        Integer traded volume (options carry real volume).
    open_interest
        Integer open interest, or ``None`` when the ``oi`` column is absent.
    iv
        Implied volatility (full precision — NOT money), or ``None`` when absent /
        junk (never fabricated).
    spot
        Underlying spot at the bar (rupees), or ``None`` when absent.
    resolved_strike
        The resolved ABSOLUTE strike (rupees) the ATM-relative selector landed on
        for THIS bar, or ``None`` when absent.
    resolved_expiry_date
        The resolved expiry date for THIS bar, or ``None`` when unresolved.
    resolved_expiry_source
        Provenance of :attr:`resolved_expiry_date`
        (:data:`RESOLVED_EXPIRY_SOURCES`).
    """

    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    open_interest: int | None
    iv: Decimal | None
    spot: Decimal | None
    resolved_strike: Decimal | None
    resolved_expiry_date: date | None
    resolved_expiry_source: str

    def __post_init__(self) -> None:
        # Reuse the COMMON OHLCV validator through the PUBLIC CandleBar constructor
        # (Golden Rule #11 — no forked validation, no private import): naive
        # timestamp / negative volume-OI / inconsistent OHLC all raise here. The
        # throwaway bar is discarded (the parser has already validated the core via
        # assemble_candle; this defends a directly-constructed row too).
        CandleBar(
            timestamp=self.timestamp,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
            open_interest=self.open_interest,
        )
        if self.iv is not None and self.iv < 0:
            raise ValueError(f"RollingOptionBar.iv must be non-negative or None, got {self.iv}")
        if self.spot is not None and self.spot <= 0:
            raise ValueError(f"RollingOptionBar.spot must be positive or None, got {self.spot}")
        if self.resolved_strike is not None and self.resolved_strike <= 0:
            raise ValueError(
                f"RollingOptionBar.resolved_strike must be positive or None, "
                f"got {self.resolved_strike}"
            )
        if self.resolved_expiry_source not in RESOLVED_EXPIRY_SOURCES:
            raise ValueError(
                f"RollingOptionBar.resolved_expiry_source must be one of "
                f"{sorted(RESOLVED_EXPIRY_SOURCES)}, got {self.resolved_expiry_source!r}"
            )


def _optional_price(raw: object, field: str) -> Decimal | None:
    """Parse an optional PRICE cell (``strike`` / ``spot``) → positive Decimal or None.

    Absent / zero / negative → ``None`` (honest "not served", never fabricated);
    a present, finite, positive value → exact Decimal; a present but unparseable /
    non-finite value → :class:`CandleParseError` (a genuine malformation surfaces).
    """
    if has_no_price(raw):  # absent / empty / zero / negative
        return None
    return _to_price(raw, field)


def _optional_iv(raw: object) -> Decimal | None:
    """Parse an optional ``iv`` cell → non-negative Decimal or None.

    Absent → ``None``; a non-finite / negative value → ``None`` (vendor junk
    dropped, never fabricated); an unparseable value → :class:`CandleParseError`
    (a genuine malformation surfaces). ``iv`` is a volatility metric, NOT money —
    it keeps full precision.
    """
    if is_absent(raw):
        return None
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError) as exc:
        raise CandleParseError(f"unparseable iv: {raw!r}") from exc
    if not value.is_finite() or value < 0:
        return None
    return value


def _to_price(raw: object, field: str) -> Decimal:
    """Parse a present price cell to exact finite Decimal (reused shape of ``to_price``)."""
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError) as exc:
        raise CandleParseError(f"unparseable {field} price: {raw!r}") from exc
    if not value.is_finite():
        raise CandleParseError(f"non-finite {field} price: {raw!r}")
    return value


def _parse_vendor_expiry(raw: object) -> date:
    """Parse a vendor expiry-echo cell → a ``date``.

    Supports an ISO date/datetime string (``"2024-06-27"``) and Unix epoch seconds
    (UTC → IST date). An empty / unparseable cell is a :class:`CandleParseError`
    (a malformed echo surfaces — never a fabricated date).
    """
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            raise CandleParseError(f"empty vendor expiry echo: {raw!r}")
        try:
            return date.fromisoformat(text[:10])
        except ValueError as exc:
            raise CandleParseError(f"unparseable vendor expiry echo: {raw!r}") from exc
    if isinstance(raw, bool):  # bool is an int subclass — reject explicitly
        raise CandleParseError(f"vendor expiry echo must not be bool: {raw!r}")
    if isinstance(raw, (int, float)):
        try:
            return datetime.fromtimestamp(float(raw), tz=UTC).astimezone(_IST).date()
        except (OverflowError, OSError, ValueError) as exc:
            raise CandleParseError(f"out-of-range vendor expiry echo: {raw!r}") from exc
    raise CandleParseError(f"unparseable vendor expiry echo: {raw!r}")


def _resolve_expiry(
    *,
    timestamp: datetime,
    vendor_expiry_raw: object,
    has_vendor_column: bool,
    expiry_flag: str,
    expiry_code: int,
    derive_expiry: DeriveExpiry | None,
) -> tuple[date | None, str]:
    """Resolve one bar's ``(resolved_expiry_date, resolved_expiry_source)``.

    Vendor echo (when the ``expiry`` column is present) wins; else an injected
    deriver (whose ABSTAIN — a ``None`` return — also falls to ``unresolved``, so a
    ``derived`` label always carries a real date); else the honest ``unresolved``
    state (date ``None``).
    """
    if has_vendor_column:
        return _parse_vendor_expiry(vendor_expiry_raw), RESOLVED_EXPIRY_SOURCE_VENDOR
    if derive_expiry is not None:
        bar_date_ist = timestamp.astimezone(_IST).date()
        derived = derive_expiry(bar_date_ist, expiry_flag, expiry_code)
        if derived is not None:
            return derived, RESOLVED_EXPIRY_SOURCE_DERIVED
        # The deriver ABSTAINED (no NSE-calendar answer) — honest ``unresolved`` with a
        # ``None`` date, never a ``derived`` label without a real expiry.
        return None, RESOLVED_EXPIRY_SOURCE_UNRESOLVED
    return None, RESOLVED_EXPIRY_SOURCE_UNRESOLVED


def parse_rolling_option_columns(
    payload: Mapping[str, object],
    *,
    expiry_flag: str,
    expiry_code: int,
    derive_expiry: DeriveExpiry | None = None,
) -> list[RollingOptionBar]:
    """Parse a Dhan rolling-option parallel-column payload into validated rows.

    Parameters
    ----------
    payload
        The ``data`` sub-object of a rolling-option success response — a mapping of
        the requested column arrays.
    expiry_flag, expiry_code
        The requested selectors for THIS key — threaded to the expiry deriver
        (they are not per-bar values; they are partition-fixed).
    derive_expiry
        Optional calendar deriver (§7 OPEN — the CORE ships none). ``None`` → the
        honest ``unresolved`` source for every bar lacking a vendor echo.

    Returns
    -------
    list[RollingOptionBar]
        One row per non-empty bar, in input order. No-trade rows are skipped; an
        empty payload returns ``[]`` (honest no-data).

    Raises
    ------
    DhanVendorDriftError
        A required column (timestamp / OHLC) missing, or PRESENT column arrays of
        unequal length — the chunk is rejected + re-pulled, never mis-paired.
    CandleParseError
        A column is not an array, or a non-empty field is unparseable.
    """
    present = [name for name in _ALL_COLUMNS if name in payload]
    if not present:
        return []  # no columns — holiday / no-session / too-old span (honest no-data)
    missing_required = [name for name in _REQUIRED_COLUMNS if name not in payload]
    if missing_required:
        raise DhanVendorDriftError(
            f"Dhan rolling-option payload missing required column(s) {missing_required} "
            f"(partial vendor payload)"
        )
    for name in present:
        if not isinstance(payload[name], (list, tuple)):
            raise CandleParseError(
                f"Dhan rolling-option column {name!r} must be an array, "
                f"got {type(payload[name]).__name__}"
            )
    columns: dict[str, list[object] | tuple[object, ...]] = {
        name: payload[name]  # type: ignore[misc]  # narrowed to list/tuple above
        for name in present
    }
    lengths = {name: len(col) for name, col in columns.items()}
    if len(set(lengths.values())) > 1:
        raise DhanVendorDriftError(
            f"Dhan rolling-option column arrays have unequal lengths {lengths} — "
            f"vendor drift, chunk rejected"
        )
    count = next(iter(lengths.values()))
    if count == 0:
        return []  # all-empty arrays — honest no-data
    has_expiry = "expiry" in columns
    bars: list[RollingOptionBar] = []
    for index in range(count):
        try:
            core = assemble_candle(
                ts_raw=columns["timestamp"][index],
                parse_timestamp=parse_epoch_utc,
                open_=columns["open"][index],
                high=columns["high"][index],
                low=columns["low"][index],
                close=columns["close"][index],
                volume=columns["volume"][index] if "volume" in columns else None,
                open_interest=columns["oi"][index] if "oi" in columns else None,
            )
        except EmptyCandleRow:
            continue  # graceful skip — no-trade minute
        resolved_expiry_date, resolved_expiry_source = _resolve_expiry(
            timestamp=core.timestamp,
            vendor_expiry_raw=columns["expiry"][index] if has_expiry else None,
            has_vendor_column=has_expiry,
            expiry_flag=expiry_flag,
            expiry_code=expiry_code,
            derive_expiry=derive_expiry,
        )
        bars.append(
            RollingOptionBar(
                timestamp=core.timestamp,
                open=core.open,
                high=core.high,
                low=core.low,
                close=core.close,
                volume=core.volume,
                open_interest=core.open_interest,
                iv=_optional_iv(columns["iv"][index]) if "iv" in columns else None,
                spot=_optional_price(columns["spot"][index], "spot") if "spot" in columns else None,
                resolved_strike=(
                    _optional_price(columns["strike"][index], "strike")
                    if "strike" in columns
                    else None
                ),
                resolved_expiry_date=resolved_expiry_date,
                resolved_expiry_source=resolved_expiry_source,
            )
        )
    return bars


__all__ = [
    "RESOLVED_EXPIRY_SOURCES",
    "RESOLVED_EXPIRY_SOURCE_DERIVED",
    "RESOLVED_EXPIRY_SOURCE_UNRESOLVED",
    "RESOLVED_EXPIRY_SOURCE_VENDOR",
    "DeriveExpiry",
    "RollingOptionBar",
    "parse_rolling_option_columns",
]
