"""Core domain models — BruteX's broker-neutral internal representation.

Every broker adapter normalises its raw data into these models. Layers L2+
(data lake, feed, engine, etc.) see ONLY these types, never the raw vendor
payloads from the Groww Data API or any other broker.

See ``docs/TRACK1_BACKTEST_SPEC.md`` for the full data-model specification.
The canonical instrument fields map to the broker instrument-master schema
(``brutex/adapters/groww/instruments.py``; CLAUDE.md §6).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Final

from brutex.models_rust import models_rust_enabled, route_normalise_tick_size

# ────────────────────────────────────────────────────────────────────────
# Enums — frozen taxonomy, every layer imports from here
# ────────────────────────────────────────────────────────────────────────


class Exchange(StrEnum):
    """Exchange codes — matches broker SDK EXCHANGE_* constants."""

    NSE = "NSE"
    BSE = "BSE"
    # MCX exists in broker SDK but is out of scope for BruteX v1
    # (broker-backed backtesting does not support COMMODITY segment).


class Segment(StrEnum):
    """Trading segment — matches broker SDK SEGMENT_* constants.

    .. note::

        **Track 1 scope (CLAUDE.md §3):** the signal-only spot-index
        brute-force backtest uses ``INDEX`` (the 3 brute-force indices +
        India VIX live in the vendor's cross-exchange ``IDX_I`` segment, per
        DEC-DHAN-IDX-SEGMENT-001). ``CASH`` is the spot-equity segment
        (F&O-eligible single-stock SPOT rows in the lean lake). ``FNO`` is
        **Track 2 reference** — kept in the enum so Track 2 modules and
        forward-compat tests can construct it, but no Track 1 code path
        writes ``FNO``.

        ``INDEX`` was added by DEC-INDEX-SEGMENT-PK-FIX-001 (2026-06-02):
        before it, indices were mis-assigned ``CASH``, so an index and a
        cash equity that share a numeric ``SECURITY_ID`` across the vendor's
        distinct ``IDX_I`` vs ``NSE_EQ`` segments (e.g. ``13``) collided on
        the ``(security_id, exch_id, segment, snapshot_date)`` PK and
        crashed the instrument-master load.
    """

    INDEX = "INDEX"  # vendor IDX_I — Track 1 brute-force indices + India VIX
    CASH = "CASH"
    FNO = "FNO"  # Track 2 reference — out of scope for Track 1 signal-only mode
    # COMMODITY exists in broker SDK but is out of scope for BruteX v1.


class InstrumentType(StrEnum):
    """Instrument type — matches broker SDK values verbatim.

    See the vendor instrument-master schema (CLAUDE.md §6;
    ``brutex/adapters/groww/instruments.py``) for the canonical list.

    .. note::

        **Track 1 scope (CLAUDE.md §3):** only ``IDX`` is used (4 spot
        instruments: NIFTY / BANKNIFTY / SENSEX / INDIA VIX). The other
        4 values — ``EQ`` / ``FUT`` / ``CE`` / ``PE`` — are **Track 2
        reference / forward-compat**, kept in the enum so:

        1. Vendor instrument-master parity is preserved (broker-CSV roundtrip).
        2. Track 2 options modules + cost calculator can type-check.
        3. Existing test fixtures across 6+ files keep importing them.

        Per DEC-P0-040 Q-08, these values are annotated, not deleted.
    """

    EQ = "EQ"  # Equity — Track 2 / forward-compat (cost calculator EQ path retained)
    IDX = "IDX"  # Index (NIFTY, BANKNIFTY, SENSEX, India VIX) — Track 1 SCOPE
    FUT = "FUT"  # Futures — Track 2 reference
    CE = "CE"  # Call option — Track 2 reference
    PE = "PE"  # Put option — Track 2 reference


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

    Every vendor instrument-master CSV row is normalised into one
    ``Instrument`` by :mod:`brutex.adapters.groww.instruments`. Downstream
    layers see only this class and never the raw vendor dict.

    Field names + ordering mirror the vendor instrument-master schema
    (CLAUDE.md §6 / ADR-0003, the native instrument-master schema rename):
    the vendor's lower_snake column names, BruteX's engine-ready VALUES (e.g.
    ``instrument`` keeps ``IDX``/``EQ``/``FUT``/``CE``/``PE``; ``segment``
    keeps ``CASH``/``FNO``). Identity is ``(security_id, exch_id)`` — the
    exchange-prefixed ``vendor_symbol`` of the old schema was dropped
    (ADR-0003); ``symbol_name`` is the engine's primary symbol for
    in-memory lookup / lake partitioning.
    """

    # Identity
    exch_id: Exchange  # was exchange
    security_id: int  # was exchange_token (the numeric vendor SECURITY_ID)
    symbol_name: str  # was trading_symbol — the engine's primary symbol
    display_name: str | None  # was name (vendor DISPLAY_NAME)

    # Classification
    instrument: InstrumentType  # was instrument_type (values IDX/EQ/FUT/CE/PE)
    segment: Segment
    series: str | None  # EQ, A, B, … (NSE cash only)
    isin: str | None

    # Underlying linkage (derivatives only)
    underlying_symbol: str | None
    underlying_security_id: int | None  # was underlying_exchange_token

    # Contract economics
    lot_size: int  # 0 for indices (not directly tradable)
    tick_size: Decimal  # rupees, normalised; 0 for indices (not directly tradable)
    sm_freeze_qty: int | None  # was freeze_quantity

    # Derivatives-only fields
    sm_expiry_date: date | None  # was expiry_date
    strike_price: Decimal | None
    option_type: str | None = None  # NEW — the raw CE/PE vendor OPTION_TYPE
    expiry_flag: str | None = None  # NEW — the raw M/W vendor EXPIRY_FLAG

    # Reservation / surveillance flag (KEEP the working bool value; name is
    # the vendor-faithful change — was is_reserved).
    asm_gsm_flag: bool = False

    # RAW Groww feed token — the literal instrument-CSV ``exchange_token`` string
    # (e.g. "NIFTY" / "1" / "2885"), preserved because ``security_id`` hashes a
    # non-numeric index token into a surrogate. The live websocket feed
    # (subscribe_index_value / subscribe_ltp) addresses instruments by THIS token,
    # not security_id (docs/groww/api-reference/07-feed.md:54,132;
    # DEC-LIVE-MANIFEST-FEED-FORMAT-001). None for instruments built without it.
    feed_token: str | None = None

    # ─────────────────────────────────────────────────────────────────
    # Derived properties
    # ─────────────────────────────────────────────────────────────────

    @property
    def is_derivative(self) -> bool:
        """True for FUT / CE / PE, False for EQ / IDX."""
        return self.instrument in {
            InstrumentType.FUT,
            InstrumentType.CE,
            InstrumentType.PE,
        }

    @property
    def is_option(self) -> bool:
        """True for CE / PE only."""
        return self.instrument in {InstrumentType.CE, InstrumentType.PE}

    @property
    def is_index(self) -> bool:
        """True for IDX only (reference instruments — not directly tradable)."""
        return self.instrument == InstrumentType.IDX

    @property
    def option_side(self) -> OptionSide | None:
        """CALL / PUT for options, None for non-options."""
        if self.instrument == InstrumentType.CE:
            return OptionSide.CALL
        if self.instrument == InstrumentType.PE:
            return OptionSide.PUT
        return None

    def __post_init__(self) -> None:  # pragma: no cover — dataclass init guard
        # Integrity invariants — catch adapter bugs at construction time
        if self.is_derivative and self.underlying_symbol is None:
            raise ValueError(f"Derivative {self.symbol_name} must have underlying_symbol")
        if self.is_derivative and self.sm_expiry_date is None:
            raise ValueError(f"Derivative {self.symbol_name} must have sm_expiry_date")
        if self.is_option and self.strike_price is None:
            raise ValueError(f"Option {self.symbol_name} must have strike_price")
        if self.is_option and self.strike_price is not None and self.strike_price <= 0:
            raise ValueError(f"Option {self.symbol_name} strike_price must be positive")
        if self.is_index:
            if self.lot_size < 0:
                raise ValueError(f"{self.symbol_name} lot_size must not be negative")
            if self.tick_size < 0:
                raise ValueError(f"{self.symbol_name} tick_size must not be negative")
        else:
            if self.lot_size <= 0:
                raise ValueError(f"{self.symbol_name} lot_size must be positive")
            if self.tick_size <= 0:
                raise ValueError(f"{self.symbol_name} tick_size must be positive")


# ────────────────────────────────────────────────────────────────────────
# Errors raised by the normaliser / adapter
# ────────────────────────────────────────────────────────────────────────


class BruteXError(Exception):
    """Base class for all BruteX errors.

    Subclasses MAY pass an optional ``safe_message`` keyword in addition
    to the standard positional message. When set, ``str(exc)`` returns
    only the safe message — never the raw positional args, which may
    contain credentials, tokens, or HTTP response bodies. Callers that
    persist exception text to the audit log or surface it in the
    dashboard MUST use ``str(exc)`` so the redacted form is emitted.

    Subclasses that do NOT pass ``safe_message`` keep the standard
    Exception behaviour (``str(exc)`` returns the positional message).
    """

    def __init__(self, *args: object, safe_message: str | None = None) -> None:
        super().__init__(*args)
        self.safe_message: str | None = safe_message

    def __str__(self) -> str:
        if self.safe_message is not None:
            return self.safe_message
        return super().__str__()


class OutOfScopeError(BruteXError):
    """Row belongs to a scope that is explicitly excluded from v1.

    Examples: stock options, cash equity rows, commodity segment rows.
    The adapter raises this when a vendor row would be ingested that
    does not match the locked v1 universe (``FD-09a`` / ``FD-09b``).
    """


class InstrumentFormatError(BruteXError):
    """A scrip-master row violates the documented schema.

    Raised when required fields are missing, types are wrong, or the
    internal_trading_symbol / trading_symbol equality invariant fails.
    """


class TickSizeFormatError(InstrumentFormatError):
    """Raised when ``tick_size`` cannot be normalised to Decimal rupees.

    See the vendor instrument-master schema (CLAUDE.md §6) for the format
    drift (float rupees in CSV vs integer paise in dict output).
    """


# ────────────────────────────────────────────────────────────────────────
# Tick-size normalisation — handles the CSV(float rupees)/dict(int paise) drift
# ────────────────────────────────────────────────────────────────────────

#: Minimum value of a tick size when expressed as integer paise (the
#: dict-output form from the vendor). Below 1 is meaningless (paise < 1 =
#: fractional rupee, should use the float-rupees branch instead).
MIN_TICK_PAISE: Final[int] = 1

#: Upper bound on a tick size in paise. 1000 paise = Rs 10.00, which
#: is larger than any legitimate tick size BruteX has ever encountered.
MAX_TICK_PAISE: Final[int] = 1000


VALID_TICK_SIZES_RUPEES: Final[frozenset[Decimal]] = frozenset(
    Decimal(s) for s in ("0.01", "0.05", "0.10", "0.25", "0.50", "1.00", "5.00")
)


def normalise_tick_size(raw: float | int | Decimal | str) -> Decimal:
    """Convert a vendor tick_size value to canonical Decimal rupees.

    Handles the format drift documented in the vendor instrument-master
    schema (CLAUDE.md §6):

    * CSV row: float rupees (``0.05``)
    * Dict output of ``get_instrument_by_vendor_symbol``: integer paise (``5``)

    Rules
    -----
    * ``float`` or ``str`` with decimal point → interpret as rupees
    * ``int`` (or integer-valued float) in ``[1, 1000)`` → interpret as paise
    * After conversion, must be in :data:`VALID_TICK_SIZES_RUPEES`

    When ``BRUTEX_CORE_RUST_MODELS`` is ON (default ON — slice T35 PR-2)
    the decision law routes through the ``rust/brutex-core`` crate via
    :mod:`brutex.models_rust` (eligibility-gated, burn-in-checked,
    fail-closed to this oracle); the flag-OFF default is the verbatim
    Python law below, byte-identical and crate-untouched.

    Raises
    ------
    TickSizeFormatError
        If the input cannot be interpreted or the normalised value is not
        in the set of known-valid NSE/BSE tick sizes.
    """
    if not models_rust_enabled():
        return _normalise_tick_size_python(raw)
    return route_normalise_tick_size(raw, _normalise_tick_size_python, TickSizeFormatError)


def _normalise_tick_size_python(raw: float | int | Decimal | str) -> Decimal:
    """The verbatim CPython ``normalise_tick_size`` law — the seam's live
    parity oracle (the Slice-0 extraction idiom; body byte-identical to the
    pre-seam public function).
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
        if MIN_TICK_PAISE <= raw < MAX_TICK_PAISE:
            # integer paise convention (the dict-output form)
            candidate = Decimal(raw) / Decimal(100)
        else:
            raise TickSizeFormatError(
                f"tick_size integer out of expected paise range "
                f"[{MIN_TICK_PAISE}, {MAX_TICK_PAISE}): {raw}"
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
