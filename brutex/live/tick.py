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

import json
import math
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Final

import blake3
import structlog

from brutex.live.store import TickRecord

_LOG = structlog.get_logger("brutex.live.tick")

#: Lower plausibility bound for the vendor's ``tsInMillis`` epoch-millisecond
#: timestamp: 2015-01-01T00:00:00 UTC. Groww's own historical depth starts in
#: 2020 (CLAUDE.md §6 "available from 2020"); a value before 2015 is a vendor
#: glitch (a zero, a seconds-not-ms value, a sign-flip), never a real exchange
#: instant — its derived ``exchange_ts`` is dropped to ``None`` and the candle
#: path falls back to ``recv_ts`` (mirrors the :func:`_coerce_price` skip-not-raise
#: discipline; DEC-LIVE-EXCHANGE-TS-IST-001).
_MIN_PLAUSIBLE_VENDOR_TS_MS: Final[float] = datetime(2015, 1, 1, tzinfo=UTC).timestamp() * 1000.0

#: How far PAST "now" a vendor ``tsInMillis`` may be before it is rejected as a
#: far-future glitch: +1 day of slack (covers clock skew without admitting a
#: year-2099 garbage value). Evaluated against the wall clock at conversion time.
_FUTURE_VENDOR_TS_SLACK: Final[timedelta] = timedelta(days=1)

#: Leaf mapping keys (in priority order) that carry the numeric price/value of an
#: instrument inside a nested Groww feed snapshot. Tolerant by design — the exact
#: live leaf shape is a Phase-0 VERIFY-ON-RUN item; the raw snapshot is persisted
#: verbatim by :class:`~brutex.live.store.TickStore`, so a wrong guess here is
#: re-parseable offline from the same log (never a data-loss event).
#:
#: **LAST-TRADED-PRICE keys come FIRST, ``value`` later (DEC-LIVE-CANDLE-PRECISION-001).**
#: A Groww LTP-feed EQUITY/option/future leaf carries the real price in ``ltp`` while
#: its ``value`` field is ``0.0`` (that slot is the day's traded *value*, not a price);
#: an INDEX leaf instead carries the price in ``value`` and has NO ``ltp``. Listing
#: ``value`` first made :func:`_leaf_price` return ``0.0`` for every equity (the
#: confirmed "equity OHLC reads 0" bug). With the LTP keys first an equity picks its
#: true ``ltp`` and an index leaf (no ``ltp``) still falls through to ``value``
#: correctly. An un-traded equity whose ``ltp`` is a legitimate ``0.0`` (no trade yet)
#: still yields ``0`` — that is a real not-yet-traded price, NOT skipped.
PRICE_LEAF_KEYS: Final[tuple[str, ...]] = (
    "ltp",
    "last_price",
    "last_traded_price",
    "ltp_value",
    "tp",
    "value",
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
    exchange_ts
        The vendor's OWN exchange timestamp (tz-aware UTC, ms-precise), derived
        from ``tsInMillis`` via :func:`exchange_ts_from_vendor_ms` — ``None`` when
        the leaf omits ``tsInMillis`` or stamped an implausible one. The candle
        path buckets by this when present and falls back to ``recv_ts`` when
        ``None`` (see :func:`effective_clock`; DEC-LIVE-EXCHANGE-TS-IST-001).
    """

    recv_ts: datetime
    instrument_key: str
    value: Decimal
    exchange_ts: datetime | None = None

    def __post_init__(self) -> None:
        if self.recv_ts.tzinfo is None:
            raise ValueError(f"LiveTick.recv_ts must be timezone-aware, got naive {self.recv_ts!r}")
        if self.exchange_ts is not None and self.exchange_ts.tzinfo is None:
            raise ValueError(
                f"LiveTick.exchange_ts must be timezone-aware, got naive {self.exchange_ts!r}"
            )
        if not self.value.is_finite():
            raise ValueError(f"LiveTick.value must be finite, got {self.value!r}")


def effective_clock(tick: LiveTick) -> datetime:
    """The clock a candle buckets *tick* by: its exchange time, else OUR receive time.

    Returns ``tick.exchange_ts`` (the vendor's own exchange instant) when present and
    plausible, falling back to ``tick.recv_ts`` when the leaf carried no usable
    ``tsInMillis``. Used by EVERY bucketing site (batch fold, micro-batch fold, and
    the lake rebuild) so all three agree minute-for-minute and the rebuild stays
    byte-identical (DEC-LIVE-CANDLE-BUCKET-IST-001).

    **No-look-ahead clamp:** the result is never LATER than ``recv_ts`` — if a vendor
    ``exchange_ts`` is skewed into OUR future (a clock glitch the +1-day plausibility
    slack still admits), it is clamped to ``recv_ts`` so a tick can never be bucketed
    into a minute we had not yet received (CLAUDE.md §4 look-ahead invariant). The
    intended case — exchange time EARLIER than ``recv_ts`` (a 09:15:58-IST trade we
    received in the 09:16 wall-clock minute) — is preserved (``min`` keeps the earlier
    exchange instant), which is the whole point of exchange-minute bucketing.
    """
    if tick.exchange_ts is None:
        return tick.recv_ts
    return min(tick.exchange_ts, tick.recv_ts)


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

#: Leaf mapping keys (priority order) carrying an instrument's AVERAGE / VWAP
#: price — the TickVault-parity ``avg_price`` field (DEC-LIVE-TICK-FIELDS-PARITY-001).
#: Observed on a Groww LTP-feed EQUITY leaf as ``avgPrice`` (``tests/unit/test_live_core.py``
#: equity-leaf fixture). It is NOT in the documented ``get_ltp`` / ``get_index_value``
#: snapshot response tables (CLAUDE.md §6; ``docs/groww/api-reference/07-feed.md:105-110,180-185``
#: list ONLY ``tsInMillis`` + ``ltp`` / ``value``) — so an index leaf and a minimal
#: LTP leaf carry NO avg price and yield ``None`` (honest null, never fabricated). The
#: raw leaf is persisted verbatim (``payload_hash`` + ``raw_json``), so if the vendor adds
#: an avg field under a new name it is re-parseable offline by extending this tuple.
AVG_PRICE_LEAF_KEYS: Final[tuple[str, ...]] = (
    "avgPrice",
    "average_price",
    "avg_price",
    "vwap",
)


def _leaf_avg_price(mapping: Mapping[str, Any]) -> Decimal | None:
    """Return a leaf's average/VWAP price (non-negative Decimal), or ``None``.

    Mirrors :func:`_leaf_price`'s skip-not-raise discipline over
    :data:`AVG_PRICE_LEAF_KEYS`: the first present-and-parseable non-negative key
    wins; a leaf with no avg key (every index leaf and a minimal LTP leaf) yields
    ``None`` (honest — the field is genuinely absent, not zero).
    """
    for key in AVG_PRICE_LEAF_KEYS:
        if key in mapping:
            coerced = _coerce_price(mapping[key])
            if coerced is not None:
                return coerced
    return None


def payload_hash(raw_leaf: object) -> str:
    """Stable :mod:`blake3` hex digest of a raw feed *raw_leaf* (audit / dedup).

    Canonicalises *raw_leaf* with the SAME deterministic JSON encoding the lake
    uses for ``raw_json`` (:func:`brutex.live.lake.ticks_to_frame` — ``sort_keys`` +
    compact separators + ``default=str``), then hashes with :mod:`blake3` — the
    repo's canonical hash (Golden Rule #4). Hashing the IDENTICAL bytes the
    ``raw_json`` column stores makes the digest a faithful fingerprint OF THE
    PERSISTED RECORD: two byte-identical leaves hash equal (dedup) and any drift
    is detectable (audit). Purely DERIVED + ADDITIVE — the raw leaf is untouched.

    The Groww feed leaf is JSON-native (``int`` / ``float`` / ``str`` / nested
    ``dict``; ``docs/groww/api-reference/07-feed.md``), so ``json.dumps`` serialises
    it directly. ``default=str`` is a CRASH-GUARD only — if a non-JSON object (a
    :class:`~decimal.Decimal` / :class:`~datetime.datetime`) ever reaches here it is
    stringified rather than raising (one bad leaf never aborts a fold), at the cost
    of the digest then depending on that object's ``str`` form; it is NOT a
    feature, and matches ``raw_json`` byte-for-byte, so the hash and the stored raw
    stay consistent.

    Parameters
    ----------
    raw_leaf
        The raw mapping (or scalar) the vendor sent for one instrument.

    Returns
    -------
    str
        The lowercase hex blake3 digest of the canonical-JSON encoding.
    """
    canonical = json.dumps(raw_leaf, sort_keys=True, separators=(",", ":"), default=str)
    return blake3.blake3(canonical.encode("utf-8")).hexdigest()


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


def exchange_ts_from_vendor_ms(vendor_ts_ms: float | None) -> datetime | None:
    """Derive the vendor's OWN exchange timestamp (tz-aware UTC) from ``tsInMillis``.

    The vendor stamps each leaf with ``tsInMillis`` (epoch MILLISECONDS) — the
    exchange's own clock for that observation. We convert it to a millisecond-precise
    UTC :class:`~datetime.datetime` (the ``us`` lake column preserves the ``.mmm``),
    so the operator can read the exact IST ``hh:mm:ss.mmm`` by displaying it at the
    fixed +05:30 offset (CLAUDE.md §6 — IST is UTC+05:30, no DST). This is ADDITIVE
    and DERIVED — ``vendor_ts_ms`` itself is kept UNTAMPERED.

    Returns ``None`` (so the candle path falls back to OUR ``recv_ts`` for that tick)
    when *vendor_ts_ms* is ``None`` or IMPLAUSIBLE — non-finite (``nan``/``inf``, which
    the upstream ``_coerce_ms`` does NOT reject), negative, before 2015-01-01, or more
    than a day past "now". One bad tick is dropped, never raised (the
    :func:`_coerce_price` skip-not-raise discipline; DEC-LIVE-EXCHANGE-TS-IST-001).
    """
    if vendor_ts_ms is None or not math.isfinite(vendor_ts_ms):
        return None
    if vendor_ts_ms < _MIN_PLAUSIBLE_VENDOR_TS_MS:
        return None
    now_ms = (datetime.now(tz=UTC) + _FUTURE_VENDOR_TS_SLACK).timestamp() * 1000.0
    if vendor_ts_ms > now_ms:
        return None
    return datetime.fromtimestamp(vendor_ts_ms / 1000.0, tz=UTC)


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
    avg_price
        The leaf's AVERAGE / VWAP price (TickVault-parity), as exact
        :class:`~decimal.Decimal` — ``None`` when the leaf carries no
        :data:`AVG_PRICE_LEAF_KEYS` field (every index leaf + a minimal LTP leaf;
        DEC-LIVE-TICK-FIELDS-PARITY-001). Honest null, never fabricated.
    payload_hash
        Stable :mod:`blake3` hex fingerprint of the RAW leaf bytes (audit / dedup;
        :func:`payload_hash`). ``None`` ONLY for a flat numeric leaf built without
        a raw mapping to hash (the scalar value itself is the content).
    """

    value: Decimal
    vendor_ts_ms: float | None
    volume: int | None
    open_interest: int | None
    avg_price: Decimal | None = None
    payload_hash: str | None = None


def _leaf_quote(mapping: Mapping[str, Any]) -> Quote | None:
    """Build a :class:`Quote` from a price-leaf dict, or ``None`` if not one.

    Stamps the TickVault-parity ``avg_price`` (the leaf's avg/VWAP, ``None`` when
    absent) and ``payload_hash`` (the :mod:`blake3` fingerprint of the WHOLE raw
    leaf, for audit / dedup) — both purely DERIVED from the untouched leaf
    (DEC-LIVE-TICK-FIELDS-PARITY-001).
    """
    price = _leaf_price(mapping)
    if price is None:
        return None
    return Quote(
        value=price,
        vendor_ts_ms=_coerce_ms(mapping.get(VENDOR_TS_MS_KEY)),
        volume=_coerce_int(mapping.get("volume")),
        open_interest=_coerce_int(mapping.get("openInterest")),
        avg_price=_leaf_avg_price(mapping),
        payload_hash=payload_hash(mapping),
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
    snapshot: Mapping[str, Any], last_obs: dict[str, Quote]
) -> tuple[Mapping[str, Any], dict[str, Quote]]:
    """Keep ONLY the instruments whose FULL observation CHANGED since *last_obs*.

    Walks *snapshot* the SAME way :func:`extract_quotes` /
    :func:`flatten_index_snapshot` do — reusing :func:`_leaf_quote`, the
    ``"prefix:key"`` instrument-key rule, the drift guard, and ``_coerce_price``
    — and rebuilds the SAME nested shape containing only the leaves whose
    :class:`Quote` (price **and** the vendor ``tsInMillis`` **and** volume / open
    interest) differs from ``last_obs[instrument_key]`` (a never-seen instrument
    is ALWAYS kept). A kept leaf is copied **verbatim** (the full raw mapping), so
    ``tsInMillis`` / volume / open interest survive for every kept instrument.

    **Full-leaf, not price-only — "save ALL the ticks" (operator rule).** A
    liquid equity prints many trades at the *same price*, each carrying a fresh
    ``tsInMillis`` (and a rising cumulative volume). A price-only delta would drop
    every same-price reprint as "unchanged" and silently lose those ticks; keying
    on the whole :class:`Quote` keeps every DISTINCT vendor observation and drops
    ONLY a byte-for-byte re-read of the identical leaf (the ~140/sec redundancy
    the operator's 4.33 GB / 4-minute run was writing). Indices carry no volume
    and re-stamp ``tsInMillis`` on every print, so for the brute-force trio
    (NIFTY / BANKNIFTY / SENSEX) the full-leaf key is exact; the equity rows
    (REFERENCE_ONLY) are the ones a price-only key would have thinned.

    Keeping a *superset* of the price-only delta never disturbs the candle:
    the builder takes open = first value, close = last value, high = max, low =
    min, volume hard-0, and an extra same-price observation alters none of those.
    So the 1-minute candle rebuilt from the delta stream stays byte-identical to
    the one rebuilt from the full-snapshot stream (DEC-LIVE-DELTA-CAPTURE-001).

    **O(1) space — mutated IN PLACE, no per-callback copy.** *last_obs* is
    updated in place with every kept leaf's new :class:`Quote` and returned as the
    second tuple element, so a ~140/sec callback over ~779 instruments costs only
    O(number-of-kept-leaves) extra space, NOT an O(n) full-dict copy every
    callback (Golden Rule #5 O(1) discipline; the prior `dict(last_obs)` copy was
    pure overhead on the common idle path, where NOTHING is kept so NOTHING is
    mutated). The caller owns *last_obs* and passes a fresh ``{}`` whenever it
    wants a keyframe (the capture clears it at each minute boundary); on an
    idle callback no leaf is kept, so the baseline is left byte-for-byte untouched.

    Parameters
    ----------
    snapshot
        The raw (possibly nested) feed snapshot, exactly as the SDK returned it.
    last_obs
        ``{instrument_key: Quote}`` — the FULL observation last WRITTEN per
        instrument, **mutated in place** (the capture seeds ``{}`` for the first
        callback / each minute keyframe). The caller must pass a dict it owns.

    Returns
    -------
    tuple[Mapping[str, Any], dict[str, Quote]]
        ``(pruned_snapshot, last_obs)`` where *pruned_snapshot* is the same nested
        shape holding only changed instruments' full raw leaves (an EMPTY mapping
        when nothing changed), and the second element is the SAME *last_obs* dict,
        now carrying every kept instrument's new :class:`Quote` (the caller adopts
        it as the next-callback baseline). Unchanged instruments keep their prior
        Quote; instruments absent from *snapshot* are left untouched.
    """
    pruned = _prune_into(snapshot, last_obs, prefix="")
    return pruned, last_obs


def _prune_into(
    snapshot: Mapping[str, Any], updated: dict[str, Quote], *, prefix: str
) -> dict[str, Any]:
    """Recursive worker for :func:`prune_unchanged` — see its contract.

    Mutates *updated* in place with every kept leaf's new :class:`Quote` and
    returns the rebuilt nested mapping for this level (only branches that contain
    a changed leaf appear). The traversal mirrors :func:`extract_quotes` exactly:
    a price leaf-dict (matched by :func:`_leaf_quote`) is a leaf, a drifted leaf
    (:func:`_is_drifted_leaf`) is skipped, any other mapping recurses, and a flat
    numeric value is coerced via :func:`_coerce_price` into a bare-price Quote.
    """
    out: dict[str, Any] = {}
    for raw_key, raw_value in snapshot.items():
        key = f"{prefix}:{raw_key}" if prefix else str(raw_key)
        if isinstance(raw_value, Mapping):
            quote = _leaf_quote(raw_value)
            if quote is not None:
                if _changed(key, quote, updated):
                    out[raw_key] = raw_value
            elif _is_drifted_leaf(raw_value):
                _warn_schema_drift(key, raw_value)
            else:
                child = _prune_into(raw_value, updated, prefix=key)
                if child:
                    out[raw_key] = child
        else:
            coerced = _coerce_price(raw_value)
            if coerced is not None:
                quote = Quote(value=coerced, vendor_ts_ms=None, volume=None, open_interest=None)
                if _changed(key, quote, updated):
                    out[raw_key] = raw_value
    return out


def _changed(key: str, quote: Quote, updated: dict[str, Quote]) -> bool:
    """True if *quote* differs from the recorded observation for *key*.

    A never-seen *key* counts as changed (always kept). On a change the baseline
    is advanced in place to *quote* (so the IDENTICAL leaf next callback is
    dropped). Equality is the frozen :class:`Quote`'s value equality — price AND
    vendor ``tsInMillis`` AND volume / open interest — so a same-price reprint
    with a fresh timestamp is a genuine new tick and is KEPT.
    """
    previous = updated.get(key)
    if previous is not None and previous == quote:
        return False
    updated[key] = quote
    return True


def iter_ticks(records: Sequence[TickRecord]) -> Iterator[LiveTick]:
    """Expand captured :class:`TickRecord` snapshots into per-instrument ticks.

    One stored record (one feed callback) fans out into one :class:`LiveTick`
    per instrument present in its snapshot. Records whose snapshot flattens to
    nothing (e.g. an empty heartbeat) yield no ticks.

    Each tick carries the vendor's derived ``exchange_ts`` (from the leaf's
    ``tsInMillis`` via :func:`exchange_ts_from_vendor_ms`) — ``None`` for a leaf
    without a usable timestamp (e.g. a flat numeric snapshot or an index leaf with
    an implausible ms) so the candle path falls back to ``recv_ts`` for that tick.
    Built from :func:`extract_quotes` (NOT :func:`flatten_index_snapshot`) precisely
    so the vendor timestamp survives into the bucketing clock (DEC-LIVE-CANDLE-BUCKET-IST-001).

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
        for instrument_key, quote in extract_quotes(record.snapshot).items():
            yield LiveTick(
                recv_ts=record.recv_ts,
                instrument_key=instrument_key,
                value=quote.value,
                exchange_ts=exchange_ts_from_vendor_ms(quote.vendor_ts_ms),
            )


__all__ = [
    "AVG_PRICE_LEAF_KEYS",
    "PRICE_LEAF_KEYS",
    "VENDOR_TS_MS_KEY",
    "LiveTick",
    "Quote",
    "effective_clock",
    "exchange_ts_from_vendor_ms",
    "extract_quotes",
    "flatten_index_snapshot",
    "iter_ticks",
    "payload_hash",
    "prune_unchanged",
]
