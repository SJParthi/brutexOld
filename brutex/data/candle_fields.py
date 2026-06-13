"""Vendor-neutral candle-field parsing — shared by every adapter parser.

Extracted from the Groww parser (ADR-0002) so every adapter
parser shares ONE implementation of the field-level rules (CLAUDE.md §11
COMMON — no copy-paste between modules; the jscpd gate enforces this). Each
adapter keeps only its **vendor-specific** glue:

* Groww — 7-element row shape + IST-string timestamps.
* Any future vendor — its own candle response shape + timestamp encoding.

The two correctness rules the operator cares most about ("no data
mismatch") live here, once:

* **Prices** parse to exact :class:`~decimal.Decimal` via ``str(...)`` —
  no binary-float drift (``NFR-DET-001`` bit-exactness).
* **OHLC sanity** is validated by the :class:`CandleBar` constructor
  (``high >= low``, open/close within ``[low, high]``, non-negative
  volume/OI), so a corrupt row raises rather than entering the lake.

Timestamp parsing stays vendor-specific and is injected as a callable so
the **empty-bar check runs before** timestamp parsing — preserving the
exact order a no-trade bar is recognised regardless of its timestamp.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal, InvalidOperation

from brutex.data.models import CandleBar
from brutex.models import BruteXError


class CandleParseError(BruteXError):
    """A candle field violates the documented shape or value rules."""


class EmptyCandleRow(CandleParseError):
    """A candle carries NO OHLC data (every price absent / empty / zero).

    A no-trade / halted / not-yet-listed / illiquid period legitimately
    returns a bar with null/empty/zero OHLC — there is simply no price to
    store. This is a GRACEFUL skip, explicitly distinct from a
    partially-malformed bar (some prices present, some broken) which stays
    a plain :class:`CandleParseError` (DEC-CANDLE-EMPTY-BAR-001).
    """


def is_absent(value: object) -> bool:
    """True for a vendor "no value" cell — ``None`` or empty/whitespace string."""
    return value is None or (isinstance(value, str) and value.strip() == "")


def has_no_price(value: object) -> bool:
    """True when a price cell carries no tradeable information: absent OR zero.

    A traded Indian instrument's price is never 0 (tick ≥ 0.05, rupees), so
    an absent / empty / 0 OHLC value means "no trade in this bar". An
    unparseable non-zero value returns ``False`` so the strict
    :func:`to_price` flags it (malformed, not empty).
    """
    if is_absent(value):
        return True
    try:
        return Decimal(str(value)) == 0
    except (InvalidOperation, ValueError):
        return False


def to_price(raw: object, field: str) -> Decimal:
    """Parse a price field to exact Decimal rupees (via str, no float drift).

    Rejects non-finite values: ``inf`` / ``nan`` round-trip through
    ``Decimal`` as ``Infinity`` / ``NaN`` (no parse error), so we guard
    explicitly — a non-finite price is a data mismatch, never a valid bar.
    """
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError) as exc:
        raise CandleParseError(f"unparseable {field} price: {raw!r}") from exc
    if not value.is_finite():
        raise CandleParseError(f"non-finite {field} price: {raw!r}")
    return value


def to_int(raw: object, field: str) -> int:
    """Parse an integer field (volume / OI); reject bools + non-numerics."""
    if isinstance(raw, bool):  # bool is an int subclass — reject explicitly
        raise CandleParseError(f"{field} must be an integer, got bool: {raw!r}")
    if not isinstance(raw, (int, float, str)):
        raise CandleParseError(f"unparseable {field}: {raw!r}")
    try:
        return int(raw)
    except (ValueError, OverflowError) as exc:
        # OverflowError covers ``int(inf)``; ValueError covers ``int('xyz')``
        # / ``int(nan)``.
        raise CandleParseError(f"unparseable {field}: {raw!r}") from exc


def assemble_candle(
    *,
    ts_raw: object,
    parse_timestamp: Callable[[object], datetime],
    open_: object,
    high: object,
    low: object,
    close: object,
    volume: object,
    open_interest: object,
) -> CandleBar:
    """Build a validated :class:`CandleBar` from raw field values.

    The **empty-bar check runs first** (before *parse_timestamp* is
    invoked), so a no-trade bar is recognised as :class:`EmptyCandleRow`
    independent of its timestamp. Spot **indices** carry no traded volume
    (vendor returns ``null``) — an absent volume maps to ``0`` rather than
    quarantining the row.

    Raises
    ------
    EmptyCandleRow
        Every OHLC price absent / empty / zero (graceful skip).
    CandleParseError
        Any field unparseable, or the timestamp invalid.
    ValueError
        Propagated from :class:`CandleBar` on a naive timestamp or a
        negative volume / open-interest. Inconsistent OHLC (a vendor
        ``open`` / ``close`` outside the reported ``[low, high]``) is
        **normalized** here — ``high`` / ``low`` are clamped to bound all
        four prices (DEC-OHLC-VENDOR-NORMALIZE-001) — never raised.
    """
    if all(has_no_price(v) for v in (open_, high, low, close)):
        raise EmptyCandleRow("candle has no OHLC data (no-trade / halted / unlisted bar)")
    # Vendor count-field normalization (DEC-VENDOR-VOLUME-NORMALIZE-001, sibling
    # of the OHLC clamp below): an IMPOSSIBLE negative volume / open-interest
    # must NEVER crash a multi-year pull on the strict ``CandleBar`` invariant.
    # A vendor can emit a stray negative (e.g. ``-2``) on the ``volume`` field of
    # an INDEX (indices have no traded volume at all — the value is junk), and the
    # observed live failure was exactly ``volume must be non-negative, got -2``.
    # A count's only valid floor is 0, so clamp to 0 — never raise, never lose
    # the bar's authoritative OHLC price. (Indices additionally have ``volume``
    # IGNORED outright upstream — the spot parser's ``drop_volume`` — so an index
    # bar is always 0 regardless of the junk volume the vendor sends.)
    oi = None if is_absent(open_interest) else max(0, to_int(open_interest, "open_interest"))
    open_price = to_price(open_, "open")
    high_price = to_price(high, "high")
    low_price = to_price(low, "low")
    close_price = to_price(close, "close")
    # Vendor OHLC normalization (DEC-OHLC-VENDOR-NORMALIZE-001): ``open`` and
    # ``close`` are authoritative traded prices, so the bar's true high/low
    # MUST bound them. A vendor occasionally under-reports ``high`` (or over-
    # reports ``low``) by a tick — a single such quirk bar would otherwise
    # crash an entire multi-year pull on the strict ``CandleBar`` invariant
    # (``low <= open, close <= high``). Clamp to enclose all four prices
    # (``high = max``, ``low = min``) — the mathematically-correct bound (a
    # traded open of 17289 implies the true high >= 17289), never data loss.
    # The ``CandleBar`` invariant stays strict for any non-assembler path.
    prices = (open_price, high_price, low_price, close_price)
    return CandleBar(
        timestamp=parse_timestamp(ts_raw),
        open=open_price,
        high=max(prices),
        low=min(prices),
        close=close_price,
        volume=0 if is_absent(volume) else max(0, to_int(volume, "volume")),
        open_interest=oi,
    )


__all__ = [
    "CandleParseError",
    "EmptyCandleRow",
    "assemble_candle",
    "has_no_price",
    "is_absent",
    "to_int",
    "to_price",
]
