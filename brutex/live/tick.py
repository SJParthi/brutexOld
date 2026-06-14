"""Live tick records + Groww index-snapshot flattening.

The Groww feed callback (``on_data_received``) is **0-argument** — it only
signals "data arrived"; the caller then pulls the current snapshot via
``GrowwFeed.get_index_value()`` / ``get_ltp()`` (verified against ``growwapi``
v1.5.x ``feed.py``). That snapshot is a (possibly nested) mapping of instrument
→ value. :func:`flatten_index_snapshot` normalises whatever shape the SDK
returns into a flat ``{instrument_key: Decimal}`` so the candle builder never
has to know the vendor nesting.

Prices are kept as exact :class:`~decimal.Decimal` rupees (``str()`` parse, never
``float``) so the live candle is bit-comparable to the backtest candle
(``NFR-DET-001``).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from brutex.live.store import TickRecord

#: Leaf mapping keys (in priority order) that carry the numeric price/value of an
#: instrument inside a nested Groww feed snapshot. Tolerant by design — the exact
#: live leaf shape is a Phase-0 VERIFY-ON-RUN item; the raw snapshot is persisted
#: verbatim by :class:`~brutex.live.store.TickStore`, so a wrong guess here is
#: re-parseable offline from the same log (never a data-loss event).
PRICE_LEAF_KEYS: Final[tuple[str, ...]] = (
    "value",
    "ltp",
    "last_price",
    "last_traded_price",
    "tp",
    "ltp_value",
    "index_value",
    "price",
    "close",
)


@dataclass(frozen=True, slots=True)
class LiveTick:
    """One captured live observation for ONE instrument.

    Attributes
    ----------
    recv_ts
        UTC tz-aware wall-clock time the capture recorded this observation
        (OUR clock — the cadence oracle for "≈1/sec snapshot vs tick-by-tick").
    instrument_key
        Stable flattened key for the instrument (e.g. ``"NSE:IDX:NIFTY"`` or
        the dotted snapshot path the vendor returned).
    value
        The index value / LTP as exact :class:`~decimal.Decimal` rupees.
    """

    recv_ts: datetime
    instrument_key: str
    value: Decimal

    def __post_init__(self) -> None:
        if self.recv_ts.tzinfo is None:
            raise ValueError(f"LiveTick.recv_ts must be timezone-aware, got naive {self.recv_ts!r}")
        if not self.value.is_finite():
            raise ValueError(f"LiveTick.value must be finite, got {self.value!r}")


def _coerce_number(raw: object) -> Decimal | None:
    """Return *raw* as a finite :class:`~decimal.Decimal`, or ``None``.

    ``bool`` is rejected explicitly (``isinstance(True, int)`` is ``True`` in
    Python and ``True``/``False`` are never prices). Strings that parse as a
    number are accepted (the feed occasionally stringifies values).
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float, str)):
        try:
            value = Decimal(str(raw))
        except (InvalidOperation, ValueError):
            return None
        return value if value.is_finite() else None
    return None


def _leaf_price(mapping: Mapping[str, Any]) -> Decimal | None:
    """If *mapping* is a price leaf dict, return its value, else ``None``."""
    for key in PRICE_LEAF_KEYS:
        if key in mapping:
            coerced = _coerce_number(mapping[key])
            if coerced is not None:
                return coerced
    return None


def flatten_index_snapshot(snapshot: Mapping[str, Any], *, prefix: str = "") -> dict[str, Decimal]:
    """Flatten a (possibly nested) Groww feed snapshot to ``{key: Decimal}``.

    Handles all three shapes the SDK is documented/observed to emit:

    * **flat** ``{"NSE_NIFTY": 22500.5}`` → ``{"NSE_NIFTY": Decimal("22500.5")}``;
    * **nested** ``{exchange: {segment: {feed_key: value}}}`` → joined with ``":"``;
    * **nested leaf-dict** ``{..: {feed_key: {"value": 22500.5, ...}}}`` → the
      first matching :data:`PRICE_LEAF_KEYS` field is taken as the value.

    Non-numeric, non-mapping leaves (and unparseable ones) are skipped — the raw
    snapshot is preserved by the store, so dropping a junk leaf here is safe.

    Parameters
    ----------
    snapshot
        The mapping returned by ``GrowwFeed.get_index_value()`` / ``get_ltp()``.
    prefix
        Internal recursion accumulator (the dotted key path so far).

    Returns
    -------
    dict[str, Decimal]
        Flat instrument-key → value map (insertion order = traversal order).
    """
    out: dict[str, Decimal] = {}
    for raw_key, raw_value in snapshot.items():
        key = f"{prefix}:{raw_key}" if prefix else str(raw_key)
        if isinstance(raw_value, Mapping):
            leaf = _leaf_price(raw_value)
            if leaf is not None:
                out[key] = leaf
            else:
                out.update(flatten_index_snapshot(raw_value, prefix=key))
        else:
            coerced = _coerce_number(raw_value)
            if coerced is not None:
                out[key] = coerced
    return out


def iter_ticks(records: Sequence[TickRecord]) -> Iterator[LiveTick]:
    """Expand captured :class:`TickRecord` snapshots into per-instrument ticks.

    One stored record (one feed callback) fans out into one :class:`LiveTick`
    per instrument present in its flattened snapshot. Records whose snapshot
    flattens to nothing (e.g. an empty heartbeat) yield no ticks.

    Parameters
    ----------
    records
        The raw records read back from a :class:`~brutex.live.store.TickStore`.

    Yields
    ------
    LiveTick
        In capture order, then instrument order within each record.
    """
    for record in records:
        for instrument_key, value in flatten_index_snapshot(record.snapshot).items():
            yield LiveTick(
                recv_ts=record.recv_ts,
                instrument_key=instrument_key,
                value=value,
            )


__all__ = [
    "PRICE_LEAF_KEYS",
    "LiveTick",
    "flatten_index_snapshot",
    "iter_ticks",
]
