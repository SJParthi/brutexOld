"""Core domain models — BruteX's broker-neutral internal representation.

Every broker adapter normalises its raw data into these models. Layers L2+
(data lake, feed, engine, etc.) see ONLY these types, never the raw SDK
dicts from ``growwapi`` or any other broker.

See ``docs/REQUIREMENTS.md §6`` for the full data-model specification and
``docs/groww-sdk/instruments.md §10`` for the Groww-specific mapping.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Final

# ────────────────────────────────────────────────────────────────────────
# Enums — frozen taxonomy, every layer imports from here
# ────────────────────────────────────────────────────────────────────────


class Exchange(StrEnum):
    """Exchange codes — matches Groww SDK EXCHANGE_* constants."""

    NSE = "NSE"
    BSE = "BSE"
    # MCX exists in Groww SDK but is out of scope for BruteX v1
    # (Groww backtesting does not support COMMODITY segment).


class Segment(StrEnum):
    """Trading segment — matches Groww SDK SEGMENT_* constants."""

    CASH = "CASH"
    FNO = "FNO"
    # COMMODITY exists in Groww SDK but is out of scope for BruteX v1.


class InstrumentType(StrEnum):
    """Instrument type — matches Groww SDK values verbatim.

    See ``docs/groww-sdk/annexures.md §5`` for the canonical list.
    """

    EQ = "EQ"  # Equity
    IDX = "IDX"  # Index (NIFTY, BANKNIFTY, SENSEX, …)
    FUT = "FUT"  # Futures
    CE = "CE"  # Call option
    PE = "PE"  # Put option


class OptionSide(StrEnum):
    """Call / put side for options, derivable from ``InstrumentType``."""

    CALL = "CE"
    PUT = "PE"


# ────────────────────────────────────────────────────────────────────────
# Instrument — the canonical per-contract metadata record
# ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Instrument:
    """Canonical representation of one tradable contract.

    Every Groww CSV row is normalised into one ``Instrument`` by
    ``brutex.adapters.groww.normaliser.normalise_instrument``. Downstream
    layers see only this class and never the raw ``growwapi`` dict.

    Field ordering matches ``docs/groww-sdk/instruments.md §4`` for
    side-by-side auditability.
    """

    # Identity
    exchange: Exchange
    exchange_token: int
    trading_symbol: str
    groww_symbol: str  # primary key — O(1) lookup
    name: str | None

    # Classification
    instrument_type: InstrumentType
    segment: Segment
    series: str | None  # EQ, A, B, … (NSE cash only)
    isin: str | None

    # Underlying linkage (derivatives only)
    underlying_symbol: str | None
    underlying_exchange_token: int | None

    # Contract economics
    lot_size: int
    tick_size: Decimal  # ALWAYS in rupees, normalised from paise if needed
    freeze_quantity: int | None

    # Derivatives-only fields
    expiry_date: date | None
    strike_price: Decimal | None

    # Permissions
    is_reserved: bool
    buy_allowed: bool
    sell_allowed: bool

    # Groww-specific undocumented field — see docs/groww-sdk/instruments.md §12.1
    internal_trading_symbol: str | None = None

    # ─────────────────────────────────────────────────────────────────
    # Derived properties
    # ─────────────────────────────────────────────────────────────────

    @property
    def is_derivative(self) -> bool:
        """True for FUT / CE / PE, False for EQ / IDX."""
        return self.instrument_type in {
            InstrumentType.FUT,
            InstrumentType.CE,
            InstrumentType.PE,
        }

    @property
    def is_option(self) -> bool:
        """True for CE / PE only."""
        return self.instrument_type in {InstrumentType.CE, InstrumentType.PE}

    @property
    def option_side(self) -> OptionSide | None:
        """CALL / PUT for options, None for non-options."""
        if self.instrument_type == InstrumentType.CE:
            return OptionSide.CALL
        if self.instrument_type == InstrumentType.PE:
            return OptionSide.PUT
        return None

    def __post_init__(self) -> None:  # pragma: no cover — dataclass init guard
        # Integrity invariants — catch adapter bugs at construction time
        if self.is_derivative and self.underlying_symbol is None:
            raise ValueError(f"Derivative {self.groww_symbol} must have underlying_symbol")
        if self.is_derivative and self.expiry_date is None:
            raise ValueError(f"Derivative {self.groww_symbol} must have expiry_date")
        if self.is_option and self.strike_price is None:
            raise ValueError(f"Option {self.groww_symbol} must have strike_price")
        if self.is_option and self.strike_price is not None and self.strike_price <= 0:
            raise ValueError(f"Option {self.groww_symbol} strike_price must be positive")
        if self.lot_size <= 0:
            raise ValueError(f"{self.groww_symbol} lot_size must be positive")
        if self.tick_size <= 0:
            raise ValueError(f"{self.groww_symbol} tick_size must be positive")


# ────────────────────────────────────────────────────────────────────────
# Errors raised by the normaliser / adapter
# ────────────────────────────────────────────────────────────────────────


class BruteXError(Exception):
    """Base class for all BruteX errors."""


class OutOfScopeError(BruteXError):
    """Row belongs to a scope that is explicitly excluded from v1.

    Examples: stock options, cash equity rows, commodity segment rows.
    The adapter raises this when a Groww row would be ingested that
    does not match the locked v1 universe (``FD-09a`` / ``FD-09b``).
    """


class InstrumentFormatError(BruteXError):
    """A Groww row violates the documented schema.

    Raised when required fields are missing, types are wrong, or the
    internal_trading_symbol / trading_symbol equality invariant fails.
    """


class TickSizeFormatError(InstrumentFormatError):
    """Raised when ``tick_size`` cannot be normalised to Decimal rupees.

    See ``docs/groww-sdk/instruments.md §12.2`` for the format drift
    (float rupees in CSV vs integer paise in dict output).
    """


# ────────────────────────────────────────────────────────────────────────
# Tick-size normalisation — handles the CSV(float rupees)/dict(int paise) drift
# ────────────────────────────────────────────────────────────────────────

VALID_TICK_SIZES_RUPEES: Final[frozenset[Decimal]] = frozenset(
    Decimal(s) for s in ("0.01", "0.05", "0.10", "0.25", "0.50", "1.00", "5.00")
)


def normalise_tick_size(raw: float | int | Decimal | str) -> Decimal:
    """Convert a Groww tick_size value to canonical Decimal rupees.

    Handles the format drift documented in
    ``docs/groww-sdk/instruments.md §12.2``:

    * CSV row: float rupees (``0.05``)
    * Dict output of ``get_instrument_by_groww_symbol``: integer paise (``5``)

    Rules
    -----
    * ``float`` or ``str`` with decimal point → interpret as rupees
    * ``int`` (or integer-valued float) in ``[1, 1000)`` → interpret as paise
    * After conversion, must be in :data:`VALID_TICK_SIZES_RUPEES`

    Raises
    ------
    TickSizeFormatError
        If the input cannot be interpreted or the normalised value is not
        in the set of known-valid NSE/BSE tick sizes.
    """
    if isinstance(raw, Decimal):
        candidate = raw
    elif isinstance(raw, str):
        try:
            candidate = Decimal(raw)
        except (ValueError, ArithmeticError) as e:
            raise TickSizeFormatError(f"Cannot parse tick_size {raw!r}") from e
    elif isinstance(raw, bool):
        # bool is a subclass of int — catch it explicitly
        raise TickSizeFormatError(f"tick_size cannot be bool: {raw!r}")
    elif isinstance(raw, int):
        if 1 <= raw < 1000:
            # integer paise convention (the dict-output form)
            candidate = Decimal(raw) / Decimal(100)
        else:
            raise TickSizeFormatError(
                f"tick_size integer out of expected paise range [1, 1000): {raw}"
            )
    elif isinstance(raw, float):
        # Convert via str to avoid binary-float precision loss.
        candidate = Decimal(str(raw))
        # Integer-valued floats > 1.0 (5.0, 10.0, 25.0, 50.0) are
        # treated as paise and converted to rupees. Values <= 1.0
        # (0.05, 0.10, 1.00) are kept as rupees. 1.00 is the unique
        # rupees/paise crossover value and we preserve it as rupees
        # because it's a common index tick size.
        if candidate > Decimal("1") and candidate == candidate.to_integral_value():
            candidate = candidate / Decimal(100)
    else:
        raise TickSizeFormatError(f"Unsupported tick_size type: {type(raw).__name__}")

    # Normalise to 2dp minimum so comparison against the valid set works
    candidate = candidate.quantize(Decimal("0.01"))
    if candidate not in VALID_TICK_SIZES_RUPEES:
        raise TickSizeFormatError(
            f"tick_size {candidate} is not a known valid NSE/BSE tick size. "
            f"Valid set: {sorted(VALID_TICK_SIZES_RUPEES)}"
        )
    return candidate


__all__ = [
    "VALID_TICK_SIZES_RUPEES",
    "BruteXError",
    "Exchange",
    "Instrument",
    "InstrumentFormatError",
    "InstrumentType",
    "OptionSide",
    "OutOfScopeError",
    "Segment",
    "TickSizeFormatError",
    "normalise_tick_size",
]
