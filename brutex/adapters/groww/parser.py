"""Groww candle parser — row-of-arrays payload → :class:`CandleBar` rows.

Groww's ``get_historical_candles`` returns OHLCV as a **list of 7-element rows**
(``docs/groww/api-reference/03-backtesting.md``)::

    {"candles": [[timestamp, open, high, low, close, volume, open_interest], ...]}

The payload is row-oriented (not columnar), so this module iterates rows and
hands each to the shared
:func:`~brutex.data.candle_fields.assemble_candle` (CLAUDE.md §11 COMMON — the
field-level rules live once). The only Groww-specific glue is the **IST timestamp**
rule: Groww returns native-IST strings like ``"2025-09-24T10:30:00"`` (or epoch
seconds defensively), which are localized to IST (fixed +05:30, no DST) then
converted to UTC for storage.

No-trade bars (all OHLC absent/zero) are skipped gracefully, so the result may be
shorter than ``len(candles)`` (DEC-CANDLE-EMPTY-BAR-001).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta, timezone

from brutex.data.candle_fields import (
    CandleParseError,
    EmptyCandleRow,
    assemble_candle,
)
from brutex.data.models import CandleBar

#: India Standard Time — a FIXED +05:30 offset (no DST), so no tzdata needed.
_IST = timezone(timedelta(hours=5, minutes=30))
#: Field count of a FULL Groww candle row
#: ``[ts, open, high, low, close, volume, open_interest]`` (settled historical bars).
_CANDLE_ROW_LEN = 7
#: Field count of the SHORTER row Groww returns for the **current, not-yet-finalised
#: trading day's intraday bars**: ``[ts, open, high, low, close, volume]`` — the
#: trailing ``open_interest`` slot is DROPPED (and ``volume`` is typically ``None``)
#: until the vendor's end-of-day settlement fills them in. Index/spot rows carry null
#: volume/OI regardless, so accepting this shorter shape never loses data — it only
#: stops the still-settling live edge from quarantining the WHOLE symbol's series
#: (DEC-GROWW-FORMING-BAR-PARSE-001).
_CANDLE_ROW_LEN_NO_OI = 6
#: The row lengths the parser accepts: full settled (7) OR current-session-without-OI (6).
_ACCEPTED_ROW_LENS = frozenset({_CANDLE_ROW_LEN_NO_OI, _CANDLE_ROW_LEN})


def parse_timestamp_ist(raw: object) -> datetime:
    """Parse a Groww candle timestamp to a tz-aware UTC datetime.

    Accepts a native-IST string (``"yyyy-MM-ddTHH:mm:ss"`` or with a space
    separator) — localized to IST then converted to UTC — or epoch seconds
    (int/float, already UTC). Rejects bools, non-finite, and out-of-range values
    (raises :class:`CandleParseError`), applying the same timestamp-validation
    guards as the shared candle-field rules.
    """
    if isinstance(raw, bool):  # bool is an int subclass — reject explicitly
        raise CandleParseError(f"candle timestamp must not be bool: {raw!r}")
    if isinstance(raw, str):
        text = raw.strip().replace("T", " ")
        try:
            naive = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        except ValueError as exc:
            raise CandleParseError(f"unparseable Groww timestamp: {raw!r}") from exc
        return naive.replace(tzinfo=_IST).astimezone(UTC)
    if isinstance(raw, (int, float)):
        epoch = float(raw)
        if not math.isfinite(epoch):
            raise CandleParseError(f"non-finite candle timestamp: {raw!r}")
        try:
            return datetime.fromtimestamp(epoch, tz=UTC)
        except (OverflowError, OSError, ValueError) as exc:
            raise CandleParseError(f"out-of-range candle timestamp: {raw!r}") from exc
    raise CandleParseError(
        f"candle timestamp must be a string or epoch seconds, got {type(raw).__name__}: {raw!r}"
    )


def parse_candle_rows(
    payload: Mapping[str, object], *, drop_volume: bool = False
) -> list[CandleBar]:
    """Parse a Groww row-of-arrays candle payload into validated rows.

    Parameters
    ----------
    payload
        A mapping carrying a ``"candles"`` key whose value is a list of
        7-element ``[ts, open, high, low, close, volume, open_interest]`` rows
        (settled bars). The current, not-yet-finalised trading day's intraday rows
        may arrive as a 6-element ``[ts, open, high, low, close, volume]`` (no
        ``open_interest`` slot yet) — these are accepted with ``open_interest=None``
        (DEC-GROWW-FORMING-BAR-PARSE-001), never quarantined.
    drop_volume
        When ``True`` the per-row volume is ignored outright (every bar's volume
        → 0). An **index** (NIFTY / BANKNIFTY / SENSEX / INDIA VIX) carries no
        traded volume; the adapter passes ``drop_volume=instrument.is_index``
        (DEC-VENDOR-VOLUME-NORMALIZE-001).

    Returns
    -------
    list[CandleBar]
        One row per non-empty candle, in input order. No-trade bars are skipped.

    Raises
    ------
    CandleParseError
        If *payload* is not a mapping, ``"candles"`` is missing/non-array, a row is
        neither a 7-element nor a 6-element array, or any non-empty field is unparseable.
    """
    # DEFENSIVE-INSURANCE guard (DEC-CANDLE-PAYLOAD-MAPPING-GUARD-001): this is NOT a
    # current bug — the installed `growwapi` SDK's `get_historical_candles` returns a
    # `dict(...)` on success or RAISES, so it can never hand back a non-mapping (`None` /
    # a scalar / a top-level list). But the param is typed `Mapping[str, object]`, and a
    # `"candles" not in payload` on a non-mapping would raise a RAW `TypeError` (an
    # "escaped code bug" the spine crashes the whole pull on) rather than a clean
    # `CandleParseError` (→ per-symbol quarantine). This one-line isinstance check makes a
    # future SDK contract change degrade gracefully; the row-array branches below already
    # cover every malformed-but-dict body. The triage DISPROVED the alleged HIGH that this
    # path crashes today against the real SDK — this is insurance, not a fix for a live crash.
    if not isinstance(payload, Mapping):
        raise CandleParseError(
            f"Groww candle payload must be a mapping, got {type(payload).__name__}"
        )
    if "candles" not in payload:
        raise CandleParseError("missing required 'candles' key in Groww payload")
    candles = payload["candles"]
    if not isinstance(candles, (list, tuple)):
        raise CandleParseError(f"'candles' must be an array, got {type(candles).__name__}")

    bars: list[CandleBar] = []
    for index, row in enumerate(candles):
        if not isinstance(row, (list, tuple)) or len(row) not in _ACCEPTED_ROW_LENS:
            raise CandleParseError(
                f"candle row {index} must be a 7-element array "
                f"[ts, open, high, low, close, volume, open_interest] (settled bars) "
                f"or a 6-element array [ts, open, high, low, close, volume] (the current "
                f"not-yet-finalised session, no open_interest), got {row!r}"
            )
        # The current trading day's intraday bars come back WITHOUT the trailing
        # open_interest slot until the vendor's EOD settlement (DEC-GROWW-FORMING-BAR-
        # PARSE-001) — pad it to None so the live edge parses instead of quarantining
        # the whole series. OI is null for index/spot rows anyway, so no data is lost.
        if len(row) == _CANDLE_ROW_LEN_NO_OI:
            ts_raw, open_, high, low, close, volume = row
            open_interest = None
        else:
            ts_raw, open_, high, low, close, volume, open_interest = row
        try:
            bar = assemble_candle(
                ts_raw=ts_raw,
                parse_timestamp=parse_timestamp_ist,
                open_=open_,
                high=high,
                low=low,
                close=close,
                volume=None if drop_volume else volume,
                open_interest=open_interest,
            )
        except EmptyCandleRow:
            continue  # graceful skip — no-trade minute
        bars.append(bar)
    return bars


__all__ = [
    "CandleParseError",
    "EmptyCandleRow",
    "parse_candle_rows",
    "parse_timestamp_ist",
]
