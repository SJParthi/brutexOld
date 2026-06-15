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

import structlog

from brutex.live.store import TickRecord

_LOG = structlog.get_logger("brutex.live.tick")

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


def _coerce_price(raw: object) -> Decimal | None:
    """Coerce a *price* leaf: a finite, **non-negative** Decimal, or ``None``.

    A negative index/spot/LTP value is never valid — a vendor sign-flip glitch
    is dropped (skipped, not raised, so one bad tick never aborts a build) and
    the raw snapshot still preserves it for offline audit.
    """
    coerced = _coerce_number(raw)
    return coerced if (coerced is not None and coerced >= 0) else None


def _leaf_price(mapping: Mapping[str, Any]) -> Decimal | None:
    """If *mapping* is a price leaf dict, return its value, else ``None``."""
    for key in PRICE_LEAF_KEYS:
        if key in mapping:
            coerced = _coerce_price(mapping[key])
            if coerced is not None:
                return coerced
    return None


#: The vendor's OWN timestamp field in a live-feed leaf — **MILLISECONDS**, a
#: double. VERIFIED from the ``growwapi`` proto (``stocksLiveIndices`` /
#: ``stockLivePrice`` both carry ``tsInMillis``). Stored UNTAMPERED.
VENDOR_TS_MS_KEY: Final[str] = "tsInMillis"


def _coerce_int(raw: object) -> int | None:
    """Return *raw* as an int (the feed sends volume / OI as doubles), or ``None``."""
    coerced = _coerce_number(raw)
    return None if coerced is None else int(coerced)


def _coerce_ms(raw: object) -> float | None:
    """Return the vendor millisecond timestamp UNTAMPERED as a float, or ``None``.

    No rounding, no unit conversion — the operator's rule is that the vendor's
    own timestamp is never edited (our nanosecond ``recv_ns`` is the separate,
    OURS-only clock).
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    return None


@dataclass(frozen=True, slots=True)
class Quote:
    """One instrument's FULL live observation from a feed leaf.

    Attributes
    ----------
    value
        Price (index ``value`` / stock ``ltp``) as exact :class:`~decimal.Decimal`.
    vendor_ts_ms
        The vendor's own ``tsInMillis`` (MILLISECONDS) — UNTAMPERED; ``None`` if
        the leaf omits it (e.g. a flat numeric snapshot).
    volume, open_interest
        Cumulative volume / open interest (LTP feed only; ``None`` for an index).
    """

    value: Decimal
    vendor_ts_ms: float | None
    volume: int | None
    open_interest: int | None


def _leaf_quote(mapping: Mapping[str, Any]) -> Quote | None:
    """Build a :class:`Quote` from a price-leaf dict, or ``None`` if not one."""
    price = _leaf_price(mapping)
    if price is None:
        return None
    return Quote(
        value=price,
        vendor_ts_ms=_coerce_ms(mapping.get(VENDOR_TS_MS_KEY)),
        volume=_coerce_int(mapping.get("volume")),
        open_interest=_coerce_int(mapping.get("openInterest")),
    )


def _is_drifted_leaf(mapping: Mapping[str, Any]) -> bool:
    """True if *mapping* is a feed LEAF (carries ``tsInMillis``) but no known price.

    Such a mapping is a vendor leaf whose price field was renamed/added (schema
    drift) — recursing INTO it would mint phantom instruments (e.g. a
    ``…:tsInMillis`` key whose "value" is the timestamp). We skip it instead and
    warn; the raw snapshot is persisted verbatim, so the real price is
    re-parseable offline once :data:`PRICE_LEAF_KEYS` is updated.
    """
    return VENDOR_TS_MS_KEY in mapping


def _warn_schema_drift(key: str, mapping: Mapping[str, Any]) -> None:
    """Emit a one-line schema-drift alarm for an unrecognized-price feed leaf."""
    _LOG.warning("live_tick_unknown_price_key", instrument_key=key, fields=sorted(mapping))


def extract_quotes(snapshot: Mapping[str, Any], *, prefix: str = "") -> dict[str, Quote]:
    """Flatten a snapshot to ``{instrument_key: Quote}`` — the FULL leaf.

    Like :func:`flatten_index_snapshot` but each value carries the price AND the
    vendor's untampered ``tsInMillis`` + volume/OI — the source for a
    ``live_ticks`` lake row. A flat numeric leaf yields a Quote with only
    ``value`` set; the three snapshot shapes are handled identically.
    """
    out: dict[str, Quote] = {}
    for raw_key, raw_value in snapshot.items():
        key = f"{prefix}:{raw_key}" if prefix else str(raw_key)
        if isinstance(raw_value, Mapping):
            quote = _leaf_quote(raw_value)
            if quote is not None:
                out[key] = quote
            elif _is_drifted_leaf(raw_value):
                _warn_schema_drift(key, raw_value)
            else:
                out.update(extract_quotes(raw_value, prefix=key))
        else:
            number = _coerce_price(raw_value)
            if number is not None:
                out[key] = Quote(value=number, vendor_ts_ms=None, volume=None, open_interest=None)
    return out


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
            elif _is_drifted_leaf(raw_value):
                _warn_schema_drift(key, raw_value)
            else:
                out.update(flatten_index_snapshot(raw_value, prefix=key))
        else:
            coerced = _coerce_price(raw_value)
            if coerced is not None:
                out[key] = coerced
    return out


def prune_unchanged(
    snapshot: Mapping[str, Any], last_prices: Mapping[str, Decimal]
) -> tuple[Mapping[str, Any], dict[str, Decimal]]:
    """Keep ONLY the instruments whose coerced price CHANGED since *last_prices*.

    Walks *snapshot* the SAME way :func:`extract_quotes` /
    :func:`flatten_index_snapshot` do — reusing :func:`_leaf_price`, the
    ``"prefix:key"`` instrument-key rule, the drift guard, and ``_coerce_price``
    — and rebuilds the SAME nested shape containing only the leaves whose price
    differs from ``last_prices[instrument_key]`` (a never-seen instrument is
    ALWAYS kept). A kept leaf is copied **verbatim** (the full raw mapping), so
    ``tsInMillis`` / volume / open interest survive for every kept instrument.

    This is the delta the capture persists instead of the full 779-instrument
    snapshot: the vast majority of leaves are unchanged callback-to-callback and
    are dropped (the redundancy the operator's 4.33 GB / 4-minute run was
    writing). Because the candle builder takes open = first value, close = last
    value, high = max, low = min, and an UNCHANGED leaf can never alter any of
    those four (the first occurrence of every DISTINCT value is always kept), the
    1-minute candle rebuilt from the delta stream is byte-identical to the one
    rebuilt from the full-snapshot stream (DEC-LIVE-DELTA-CAPTURE-001).

    Parameters
    ----------
    snapshot
        The raw (possibly nested) feed snapshot, exactly as the SDK returned it.
    last_prices
        ``{instrument_key: Decimal}`` — the coerced price last WRITTEN per
        instrument (the capture seeds ``{}`` for the first callback).

    Returns
    -------
    tuple[Mapping[str, Any], dict[str, Decimal]]
        ``(pruned_snapshot, updated_last_prices)`` where *pruned_snapshot* is the
        same nested shape holding only changed instruments' full raw leaves (an
        EMPTY mapping when nothing changed), and *updated_last_prices* is
        ``last_prices`` merged with every kept instrument's new price (a flat
        ``dict`` — the caller adopts it as the next-callback baseline). Unchanged
        instruments keep their prior price; instruments absent from *snapshot*
        are left untouched in the baseline.
    """
    updated = dict(last_prices)
    pruned = _prune_into(snapshot, updated, prefix="")
    return pruned, updated


def _prune_into(
    snapshot: Mapping[str, Any], updated: dict[str, Decimal], *, prefix: str
) -> dict[str, Any]:
    """Recursive worker for :func:`prune_unchanged` — see its contract.

    Mutates *updated* in place with every kept leaf's new price and returns the
    rebuilt nested mapping for this level (only branches that contain a changed
    leaf appear). The traversal mirrors :func:`extract_quotes` exactly: a price
    leaf-dict (matched by :func:`_leaf_price`) is a leaf, a drifted leaf
    (:func:`_is_drifted_leaf`) is skipped, any other mapping recurses, and a flat
    numeric value is coerced via :func:`_coerce_price`.
    """
    out: dict[str, Any] = {}
    for raw_key, raw_value in snapshot.items():
        key = f"{prefix}:{raw_key}" if prefix else str(raw_key)
        if isinstance(raw_value, Mapping):
            leaf = _leaf_price(raw_value)
            if leaf is not None:
                if _changed(key, leaf, updated):
                    out[raw_key] = raw_value
            elif _is_drifted_leaf(raw_value):
                _warn_schema_drift(key, raw_value)
            else:
                child = _prune_into(raw_value, updated, prefix=key)
                if child:
                    out[raw_key] = child
        else:
            coerced = _coerce_price(raw_value)
            if coerced is not None and _changed(key, coerced, updated):
                out[raw_key] = raw_value
    return out


def _changed(key: str, price: Decimal, updated: dict[str, Decimal]) -> bool:
    """True if *price* differs from the recorded baseline for *key*.

    A never-seen *key* counts as changed (always kept). On a change the baseline
    is advanced in place to *price* (so the SAME value next callback is dropped).
    """
    previous = updated.get(key)
    if previous is not None and previous == price:
        return False
    updated[key] = price
    return True


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
    "VENDOR_TS_MS_KEY",
    "LiveTick",
    "Quote",
    "extract_quotes",
    "flatten_index_snapshot",
    "iter_ticks",
    "prune_unchanged",
]
