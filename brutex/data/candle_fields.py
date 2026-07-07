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
    """A candle carries NO STORABLE OHLC bar — a graceful skip, never a crash.

    Two vendor cases land here, both legitimately UN-storable (there is no honest
    bar to write, and fabricating the missing price would violate Golden Rule #1):

    * **No-trade bar** — every OHLC price absent / empty / zero (a halted /
      not-yet-listed / illiquid period). The original DEC-CANDLE-EMPTY-BAR-001 case.
    * **Partially-absent bar** — at least one REQUIRED OHLC price is ABSENT
      (vendor ``null`` / empty) while others are present, e.g. Groww's observed
      ``open: None`` with a real high / low / close (DEC-CANDLE-PARTIAL-ABSENT-SKIP-001).
      Crashing an entire multi-symbol / multi-year pull on one such vendor quirk is the
      wrong trade — the SAME philosophy that normalizes a negative volume and clamps an
      under-reported high (see :func:`assemble_candle`).

    A price that is PRESENT but UNPARSEABLE / non-finite (e.g. ``"abc"`` / ``inf``)
    is NOT this — it stays a hard :class:`CandleParseError`, so a genuine shape /
    column-misalignment problem still surfaces loudly (and the ingest quarantines just
    that symbol rather than crashing the pull).
    """


def is_absent(value: object) -> bool:
    """True for a vendor "no value" cell — ``None`` or empty/whitespace string."""
    return value is None or (isinstance(value, str) and value.strip() == "")


def has_no_price(value: object) -> bool:
    """True when a price cell carries no tradeable information: absent, zero OR negative.

    A traded Indian instrument's price is never ``<= 0`` (tick ≥ 0.05, rupees), so
    an absent / empty / 0 / negative OHLC value means "no trade in this bar". A
    negative price is treated EXACTLY like zero — un-storable junk: storing it (and
    letting the ``high = max`` / ``low = min`` clamp hide it) would FABRICATE a
    negative price + range, the Golden-Rule-#1 corruption this guard exists to
    prevent (symmetric with the zero guard). An unparseable positive value returns
    ``False`` so the strict :func:`to_price` flags it (malformed, not empty).

    A NON-FINITE value (``inf`` / ``-inf`` / ``nan``) also returns ``False`` so it
    falls through to :func:`to_price`'s loud non-finite guard. Without this guard a
    present ``-inf`` round-trips through ``Decimal`` as ``-Infinity <= 0`` → ``True``
    and was SILENTLY swallowed as a "no-price" partial-bar skip, while ``+inf`` /
    ``nan`` correctly raised ``CandleParseError`` — an asymmetry that hid genuinely
    malformed ``-inf`` data instead of surfacing it (Golden Rule #1: a non-finite
    price is a data mismatch, never a no-trade bar).
    """
    if is_absent(value):
        return True
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return False
    if not decimal_value.is_finite():
        return False
    return decimal_value <= 0


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


#: Inclusive upper bound a count field (volume / open-interest) may carry: the
#: lake persists both as Parquet ``Int64`` (see ``parquet_writer``), so a value
#: above ``2**63 - 1`` cannot be stored — polars raises a raw ``TypeError``
#: ("found value of type Int128") while BUILDING the ``pl.Int64`` Series, which
#: would escape the per-symbol quarantine (``run_ingest`` re-raises any
#: non-``BruteXError``) and crash the whole multi-symbol pull.
_INT64_MAX: int = 2**63 - 1


def to_int(raw: object, field: str) -> int:
    """Parse an integer field (volume / OI); reject bools + non-numerics.

    Also rejects a value that overflows the lake's ``Int64`` storage
    (``> 2**63 - 1``): a vendor-glitch oversized volume / open-interest would
    otherwise pass parsing and crash the Parquet write with a raw ``TypeError``,
    escaping the per-symbol quarantine. Raising :class:`CandleParseError` (a
    :class:`~brutex.models.BruteXError`) here quarantines the ONE bad symbol
    instead — mirroring the negative-count clamp in :func:`assemble_candle`,
    which already keeps an impossible count from crashing a multi-year pull.
    """
    if isinstance(raw, bool):  # bool is an int subclass — reject explicitly
        raise CandleParseError(f"{field} must be an integer, got bool: {raw!r}")
    if not isinstance(raw, (int, float, str)):
        raise CandleParseError(f"unparseable {field}: {raw!r}")
    try:
        value = int(raw)
    except (ValueError, OverflowError) as exc:
        # OverflowError covers ``int(inf)``; ValueError covers ``int('xyz')``
        # / ``int(nan)``.
        raise CandleParseError(f"unparseable {field}: {raw!r}") from exc
    if value > _INT64_MAX:
        raise CandleParseError(
            f"{field} exceeds the lake's int64 storage ceiling ({_INT64_MAX}): {raw!r}"
        )
    return value


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
    on_clamp: Callable[[], None] | None = None,
) -> CandleBar:
    """Build a validated :class:`CandleBar` from raw field values.

    The **empty-bar check runs first** (before *parse_timestamp* is
    invoked), so a no-trade bar is recognised as :class:`EmptyCandleRow`
    independent of its timestamp. Spot **indices** carry no traded volume
    (vendor returns ``null``) — an absent volume maps to ``0`` rather than
    quarantining the row.

    Parameters
    ----------
    on_clamp
        OBSERVE-ONLY hook (DEC-STORED-DATA-VALIDATION-001): invoked exactly
        once per bar whose ``high``/``low`` the vendor-OHLC normalization
        below actually MOVED (``high != max(prices)`` or ``low !=
        min(prices)``), so a caller (the Groww parser) can count clamps per
        window and WARN instead of the clamp firing silently. ``None`` (the
        default — every other caller) is byte-identical to before; the
        returned bar is IDENTICAL either way (parse behavior unchanged).

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
        four prices (DEC-OHLC-VENDOR-NORMALIZE-001) — never raised (the
        clamp is COUNTED via *on_clamp*).
    """
    if all(has_no_price(v) for v in (open_, high, low, close)):
        raise EmptyCandleRow("candle has no OHLC data (no-trade / halted / unlisted bar)")
    # Partially-NO-PRICE bar (DEC-CANDLE-PARTIAL-ABSENT-SKIP-001 + DEC-CANDLE-PARTIAL-ZERO
    # -SKIP-001 + DEC-ADVERSARIAL-HARDENING-001): the row is NOT all-empty, but a REQUIRED
    # OHLC price carries no tradeable information — ABSENT (vendor null / empty), ZERO OR
    # NEGATIVE. The production crash was Groww returning ``open: None`` alongside a real high /
    # low / close, which `to_price(None)` raised as `unparseable open price: None`, crashing
    # the whole 779-symbol pull. The ZERO and NEGATIVE twins are the SAME un-storable junk: a
    # traded Indian price is never <= 0 (tick ≥ 0.05, see :func:`has_no_price`), so a lone
    # ``open: 0`` or ``open: -5`` among real prices is a vendor glitch — storing it (and
    # letting the ``low`` clamp below pull the bar to 0 / negative) would FABRICATE a junk
    # price + range, the exact Golden-Rule-#1 corruption the all-empty check already guards
    # against. We cannot fabricate the missing price, so SKIP the un-storable bar gracefully
    # (the parser's `except EmptyCandleRow: continue`), mirroring the negative-volume + OHLC
    # clamps below — never crash a pull, never store junk, on one vendor quirk. This uses the
    # SAME `has_no_price` predicate as the all-empty check above (absent OR zero OR negative),
    # so the two guards are symmetric. A price that is PRESENT but unparseable / non-finite
    # (e.g. ``"abc"`` — `has_no_price` is False) is left to the strict `to_price` calls below
    # (a hard error — a genuine malformation worth surfacing, never silently skipped).
    if any(has_no_price(v) for v in (open_, high, low, close)):
        raise EmptyCandleRow(
            "candle has an absent, zero or negative OHLC price (partial vendor bar — skipped)"
        )
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
    clamped_high = max(prices)
    clamped_low = min(prices)
    # Clamp observability (DEC-STORED-DATA-VALIDATION-001): the normalization
    # above used to fire SILENTLY with no counter — a vendor systematically
    # under-reporting highs would never surface. Observe-only: the bar built
    # below is byte-identical whether or not a hook is present.
    if on_clamp is not None and (clamped_high != high_price or clamped_low != low_price):
        on_clamp()
    return CandleBar(
        timestamp=parse_timestamp(ts_raw),
        open=open_price,
        high=clamped_high,
        low=clamped_low,
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
