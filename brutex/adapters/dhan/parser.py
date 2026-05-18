"""Dhan v2 historical-data response parser.

Pure functions that turn Dhan's 6-parallel-array JSON shape into a
deterministic ``tuple[OHLCBar, ...]``. Owns three correctness invariants:

1. **Length mismatch → DH-908.** If the 6 arrays disagree on length,
   we synthesize :class:`DhanErrorDH908` rather than silently truncate
   or raise ``KeyError``. This keeps the closed 22-code error vocabulary
   intact (Golden Rule #2 single source of truth).
2. **Decimal precision at the boundary.** Dhan publishes prices as JSON
   floats. ``Decimal(repr(x))`` round-trips losslessly (vs ``Decimal(x)``
   which inherits binary-float garbage). All prices are then quantised
   to 2 decimal places — sufficient for the 4 Track 1 indexes (NSE index
   publishes to 0.01 even though tick size on derivatives is 0.05).
3. **Sort + dedup.** Dhan has been observed to return duplicate boundary
   bars at chunk seams; the audit-replay oracle (Golden Rule #7) needs
   bit-identical output across re-calls, so the parser canonicalises by
   sorting on timestamp ascending and deduplicating by timestamp.

The parser does NOT validate that bars fall within the requested IST
window — that's the caller's job (out-of-window bars are a
data-integrity concern, not a parse concern).

References
----------
* CLAUDE.md §6: Dhan response format (verified facts table)
* docs/TRACK1_DATA_INGEST_DESIGN.md: assert_sorted_ascending + assert_no_duplicates
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any, Final

from brutex.adapters.dhan.timeconv import dhan_epoch_to_utc
from brutex.orchestrator.dhan_errors import DhanErrorDH908

#: Quantisation step for OHLC prices on Track 1 spot indexes. NSE index
#: publishes to 0.01 paise; this matches the closing-price decimals you
#: see on web.dhan.co charts. India VIX is also rounded to 2 dp by NSE.
_PRICE_QUANT: Final[Decimal] = Decimal("0.01")

#: The 6 arrays Dhan returns. Lengths must be equal — length divergence
#: is the canary the parser raises DH-908 on.
_REQUIRED_ARRAY_KEYS: Final[tuple[str, ...]] = (
    "open",
    "high",
    "low",
    "close",
    "volume",
    "timestamp",
)


@dataclass(frozen=True, slots=True)
class OHLCBar:
    """One parsed OHLC bar from a Dhan response.

    All prices are :class:`Decimal` quantised to 0.01 (paise). Volume is
    a non-negative int (spot indexes always 0 — see CLAUDE.md §7).
    Timestamp is tz-aware UTC; the IST anchor is preserved by
    :func:`brutex.adapters.dhan.timeconv.dhan_epoch_to_utc`.
    """

    timestamp_utc: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


def _to_decimal(value: float | int | str) -> Decimal:
    """Convert a Dhan-encoded numeric to :class:`Decimal` with 2-dp quant.

    Goes through ``repr()`` first because ``Decimal(float)`` would
    propagate the binary-float representation (e.g.,
    ``Decimal(24500.05) == Decimal('24500.04999999999927240423858165740966796875')``).
    """
    return Decimal(repr(value)).quantize(_PRICE_QUANT, rounding=ROUND_HALF_EVEN)


def parse_response(payload: dict[str, Any]) -> tuple[OHLCBar, ...]:
    """Parse a Dhan historical-data JSON payload into OHLC bars.

    Parameters
    ----------
    payload
        The decoded JSON dict returned by Dhan. Must contain the 6 keys
        in :data:`_REQUIRED_ARRAY_KEYS`, each mapped to a list of equal
        length. Empty lists are valid (interpretation = no-bars-in-window).

    Returns
    -------
    tuple[OHLCBar, ...]
        Bars sorted by timestamp ascending with duplicates removed by
        timestamp. Length 0 ⇔ empty input arrays.

    Raises
    ------
    DhanErrorDH908
        On missing key, non-list value, or length mismatch across the
        6 arrays. Synthesising DH-908 rather than a parser-specific
        error keeps the FSM dispatch uniform (per the 22-code whitelist
        in :mod:`brutex.orchestrator.dhan_errors`).
    """
    arrays: dict[str, list[Any]] = {}
    for key in _REQUIRED_ARRAY_KEYS:
        if key not in payload:
            raise DhanErrorDH908(detailed_message=f"parse_response: missing required key {key!r}")
        value = payload[key]
        if not isinstance(value, list):
            raise DhanErrorDH908(
                detailed_message=(
                    f"parse_response: key {key!r} is not a list (got {type(value).__name__})"
                )
            )
        arrays[key] = value

    lengths = {key: len(value) for key, value in arrays.items()}
    distinct = set(lengths.values())
    if len(distinct) > 1:
        raise DhanErrorDH908(
            detailed_message=(f"parse_response: parallel-array length mismatch — lengths={lengths}")
        )

    n = distinct.pop() if distinct else 0
    if n == 0:
        return ()

    bars: list[OHLCBar] = []
    for i in range(n):
        bars.append(
            OHLCBar(
                timestamp_utc=dhan_epoch_to_utc(int(arrays["timestamp"][i])),
                open=_to_decimal(arrays["open"][i]),
                high=_to_decimal(arrays["high"][i]),
                low=_to_decimal(arrays["low"][i]),
                close=_to_decimal(arrays["close"][i]),
                volume=int(arrays["volume"][i]),
            )
        )

    return _sort_and_dedup(bars)


def _sort_and_dedup(bars: list[OHLCBar]) -> tuple[OHLCBar, ...]:
    """Sort by timestamp ascending; keep first occurrence on duplicates.

    Dhan has been observed to return duplicate boundary bars at chunk
    seams. The audit-replay oracle (Golden Rule #7) demands bit-identical
    output, so canonicalisation lives at the parser boundary, not in
    downstream consumers.
    """
    bars.sort(key=lambda bar: bar.timestamp_utc)
    seen: set[datetime] = set()
    out: list[OHLCBar] = []
    for bar in bars:
        if bar.timestamp_utc in seen:
            continue
        seen.add(bar.timestamp_utc)
        out.append(bar)
    return tuple(out)
