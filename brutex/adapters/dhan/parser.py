"""Dhan candle/rolling-option/option-chain/expiry parsers — pure column-array law.

DEC-DHAN-ADAPTER-001. The DhanHQ v2 chart endpoints return **COLUMN arrays** (not
the row-of-arrays Groww shape): a candle payload carries parallel ``open`` /
``high`` / ``low`` / ``close`` / ``volume`` / ``timestamp`` arrays (+
``open_interest`` when ``oi=true``), and the rolling-option endpoint returns
separate ``ce`` / ``pe`` objects each carrying ``iv`` / ``oi`` / ``strike`` /
``spot`` / OHLCV / ``timestamp`` arrays (VERIFIED — DhanHQ v2 docs).

Correctness rules (Golden Rule #1 — no data mismatch):

* **Timestamps** are **epoch/UNIX seconds** under key ``timestamp`` (VERIFIED) —
  parsed to tz-aware UTC.
* **Prices** parse to exact :class:`~decimal.Decimal` via the shared
  :mod:`brutex.data.candle_fields` rules — the parser does **NOT** snap to the 2dp
  tick grid (the shared lake writer snaps). The rupees-vs-paisa price unit is
  **UNVERIFIED** — assumed rupees, stamped in the ADR probe register; parsing to
  exact Decimal is unit-agnostic, so a later paisa finding only changes the write
  boundary, never these bytes.

A pure, I/O-free LEAF (imports only stdlib + the shared candle-field + model
types) so a future ``dhan_parser_rust.py`` facade could wrap the column-array law
1:1 (DEC-DHAN-RUST-FACADE-001).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from brutex.data.candle_fields import (
    CandleParseError,
    EmptyCandleRow,
    assemble_candle,
    has_no_price,
    is_absent,
    to_price,
)
from brutex.data.models import CandleBar, OptionBar

#: Column keys of a Dhan candle payload (VERIFIED). ``open_interest`` is present
#: only when the request set ``oi=true``.
_OHLCV_KEYS: tuple[str, ...] = ("open", "high", "low", "close", "volume", "timestamp")

#: Option sides the rolling-option endpoint returns (VERIFIED — separate ``ce`` /
#: ``pe`` objects). Mapped to the :class:`~brutex.data.models.OptionBar` side token.
_ROLLING_SIDE_KEYS: Mapping[str, str] = {"ce": "CE", "pe": "PE"}

#: Per-strike option-chain field keys the parser can name defensively. The exact
#: Greek field NAMES are **UNVERIFIED** — the parser surfaces the RAW per-strike
#: mapping and extracts only these keys, stamping the assumed names. A live probe
#: closes the gap.
_OPTION_CHAIN_FIELDS_UNVERIFIED: tuple[str, ...] = (
    "last_price",
    "oi",
    "previous_oi",
    "volume",
    "implied_volatility",
    "top_bid_price",
    "top_ask_price",
)


def parse_timestamp_epoch(raw: object) -> datetime:
    """Parse a Dhan candle timestamp (epoch/UNIX seconds) to a tz-aware UTC datetime.

    VERIFIED — Dhan candle timestamps are epoch seconds under the ``timestamp`` key.
    Rejects bools, non-numerics, non-finite, and out-of-range values (raises
    :class:`CandleParseError`, a quarantinable :class:`~brutex.models.BruteXError`, so
    ONE bad row isolates its symbol instead of crashing a multi-symbol pull). Unlike
    Groww's native-IST STRING form, Dhan documents epoch seconds directly, so there is
    no IST-shift ambiguity (Golden Rule #1 — this is VERIFIED, not a defensive branch).
    """
    if isinstance(raw, bool):  # bool is an int subclass — reject explicitly
        raise CandleParseError(f"candle timestamp must not be bool: {raw!r}")
    if not isinstance(raw, (int, float)):
        raise CandleParseError(
            f"Dhan candle timestamp must be epoch seconds, got {type(raw).__name__}: {raw!r}"
        )
    epoch = float(raw)
    if not math.isfinite(epoch):
        raise CandleParseError(f"non-finite candle timestamp: {raw!r}")
    try:
        return datetime.fromtimestamp(epoch, tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise CandleParseError(f"out-of-range candle timestamp: {raw!r}") from exc


def _require_column(payload: Mapping[str, object], key: str) -> Sequence[object]:
    """Return *key*'s array from a column payload, raising :class:`CandleParseError` otherwise."""
    if key not in payload:
        raise CandleParseError(f"missing required column {key!r} in Dhan payload")
    value = payload[key]
    if not isinstance(value, (list, tuple)):
        raise CandleParseError(f"column {key!r} must be an array, got {type(value).__name__}")
    return value


def parse_candle_columns(
    payload: Mapping[str, object], *, drop_volume: bool = False
) -> list[CandleBar]:
    """Parse a Dhan COLUMN-array candle payload into validated rows.

    Parameters
    ----------
    payload
        A mapping carrying parallel ``open`` / ``high`` / ``low`` / ``close`` /
        ``volume`` / ``timestamp`` arrays (plus ``open_interest`` when the request
        set ``oi=true``). All present columns must be the SAME length.
    drop_volume
        When ``True`` per-row volume is ignored (every bar's volume → 0). An index
        carries no traded volume, so the adapter passes ``drop_volume=instrument.is_index``.

    Returns
    -------
    list[CandleBar]
        One row per non-empty candle, in input order. No-trade bars are skipped.

    Raises
    ------
    CandleParseError
        If a required column is missing / non-array, the columns are ragged (unequal
        length), or any non-empty field is unparseable.
    """
    columns = {key: _require_column(payload, key) for key in _OHLCV_KEYS}
    n = len(columns["timestamp"])
    for key, values in columns.items():
        if len(values) != n:
            raise CandleParseError(
                f"ragged Dhan columns: {key!r} has {len(values)} rows but 'timestamp' has {n}"
            )
    oi_col: Sequence[object] | None = None
    if "open_interest" in payload:
        oi_col = _require_column(payload, "open_interest")
        if len(oi_col) != n:
            raise CandleParseError(
                f"ragged Dhan columns: 'open_interest' has {len(oi_col)} rows but 'timestamp' has {n}"
            )

    bars: list[CandleBar] = []
    for index in range(n):
        open_interest = oi_col[index] if oi_col is not None else None
        try:
            bar = assemble_candle(
                ts_raw=columns["timestamp"][index],
                parse_timestamp=parse_timestamp_epoch,
                open_=columns["open"][index],
                high=columns["high"][index],
                low=columns["low"][index],
                close=columns["close"][index],
                volume=None if drop_volume else columns["volume"][index],
                open_interest=open_interest,
            )
        except EmptyCandleRow:
            continue  # graceful skip — no-trade minute
        bars.append(bar)
    return bars


def _price_or_none(raw: object, field: str) -> Decimal | None:
    """Parse an optional positive price cell (``strike`` / ``spot``) to Decimal, or ``None``."""
    if is_absent(raw) or has_no_price(raw):
        return None
    return to_price(raw, field)


def _iv_or_none(raw: object) -> Decimal | None:
    """Parse an optional non-negative implied-volatility cell to Decimal, or ``None``.

    IV is not a price (0 is a legitimate value), so :func:`has_no_price` is NOT used;
    an absent cell → ``None``, a present cell → exact Decimal (rejects non-finite via
    :func:`~brutex.data.candle_fields.to_price`, which the OptionBar constructor's
    ``iv >= 0`` guard then bounds).
    """
    if is_absent(raw):
        return None
    return to_price(raw, "iv")


def parse_rolling_option(payload: Mapping[str, object]) -> list[OptionBar]:
    """Parse a Dhan rolling-option (expired) payload into per-side :class:`OptionBar` rows.

    VERIFIED — the endpoint returns separate ``ce`` / ``pe`` objects, each carrying
    parallel ``iv`` / ``oi`` / ``strike`` / ``spot`` / OHLCV / ``timestamp`` arrays
    (ATM-relative macro addressing — the request's ``ATM±N`` selector is resolved to
    the absolute ``strike`` stamped per bar). At least one of ``ce`` / ``pe`` must be
    present; a side is skipped when absent. Returns CE rows then PE rows, each in
    input order; no-trade bars (all-empty OHLC) are skipped.

    Raises
    ------
    CandleParseError
        If neither ``ce`` nor ``pe`` is present, a side is not a mapping, a side's
        columns are ragged, or a field is unparseable.
    """
    if not any(side in payload for side in _ROLLING_SIDE_KEYS):
        raise CandleParseError(
            f"Dhan rolling-option payload has neither 'ce' nor 'pe' (keys: {sorted(map(str, payload))})"
        )
    bars: list[OptionBar] = []
    for side_key, side_token in _ROLLING_SIDE_KEYS.items():
        side_payload = payload.get(side_key)
        if side_payload is None:
            continue
        if not isinstance(side_payload, Mapping):
            raise CandleParseError(
                f"Dhan rolling-option side {side_key!r} must be a mapping, "
                f"got {type(side_payload).__name__}"
            )
        bars.extend(_parse_rolling_side(side_payload, side_token))
    return bars


def _parse_rolling_side(side_payload: Mapping[str, object], side_token: str) -> list[OptionBar]:
    """Parse ONE rolling-option side's column arrays into :class:`OptionBar` rows."""
    columns = {key: _require_column(side_payload, key) for key in _OHLCV_KEYS}
    n = len(columns["timestamp"])
    extra_keys = ("oi", "strike", "spot", "iv")
    optionals: dict[str, Sequence[object] | None] = {}
    for key in extra_keys:
        optionals[key] = _require_column(side_payload, key) if key in side_payload else None
    for key, values in {**columns, **{k: v for k, v in optionals.items() if v is not None}}.items():
        if len(values) != n:
            raise CandleParseError(
                f"ragged Dhan rolling-option {side_token} columns: {key!r} has "
                f"{len(values)} rows but 'timestamp' has {n}"
            )

    bars: list[OptionBar] = []
    for index in range(n):
        try:
            base = assemble_candle(
                ts_raw=columns["timestamp"][index],
                parse_timestamp=parse_timestamp_epoch,
                open_=columns["open"][index],
                high=columns["high"][index],
                low=columns["low"][index],
                close=columns["close"][index],
                volume=columns["volume"][index],
                open_interest=_cell(optionals["oi"], index),
            )
        except EmptyCandleRow:
            continue  # graceful skip — no-trade minute
        bars.append(
            OptionBar(
                timestamp=base.timestamp,
                open=base.open,
                high=base.high,
                low=base.low,
                close=base.close,
                volume=base.volume,
                open_interest=base.open_interest,
                side=side_token,
                strike=_price_or_none(_cell(optionals["strike"], index), "strike"),
                spot=_price_or_none(_cell(optionals["spot"], index), "spot"),
                iv=_iv_or_none(_cell(optionals["iv"], index)),
            )
        )
    return bars


def _cell(column: Sequence[object] | None, index: int) -> object:
    """Return ``column[index]`` when the optional column is present, else ``None``."""
    return None if column is None else column[index]


@dataclass(frozen=True, slots=True)
class OptionChainStrike:
    """One strike row of a Dhan option chain — RAW per-side mappings surfaced verbatim.

    Attributes
    ----------
    strike
        The resolved absolute strike (Decimal rupees).
    ce, pe
        The RAW per-side mappings exactly as Dhan returned them (OI / Greeks / volume /
        top bid-ask / price). Greek field NAMES are **UNVERIFIED** — a consumer reads
        only the fields it can name (see :func:`nameable_option_chain_fields`).
    """

    strike: Decimal
    ce: Mapping[str, object]
    pe: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class OptionChainSnapshot:
    """A parsed Dhan option-chain snapshot — the underlying last price + per-strike rows."""

    last_price: Decimal | None
    strikes: tuple[OptionChainStrike, ...]


def parse_option_chain(payload: Mapping[str, object]) -> OptionChainSnapshot:
    """Parse a Dhan option-chain payload defensively into an :class:`OptionChainSnapshot`.

    VERIFIED shape: ``{data: {last_price, oc: {"<strike>": {"ce": {...}, "pe": {...}}}}}``
    (the ``data`` envelope may be absent — the map may sit at the top level). Extracts
    the underlying ``last_price`` and every ``(strike, ce, pe)`` row, surfacing the RAW
    per-side mappings verbatim (Greek NAMES UNVERIFIED). Strikes are returned sorted
    ascending. Rows whose strike key is unparseable / a non-mapping value are skipped
    (never crash on one vendor quirk); an empty chain is a valid empty snapshot.

    Raises
    ------
    CandleParseError
        If the option-chain map (``oc``) is present but not a mapping.
    """
    data = payload.get("data")
    envelope = data if isinstance(data, Mapping) else payload
    last_price = _price_or_none(envelope.get("last_price"), "last_price")
    oc = envelope.get("oc")
    if oc is None:
        return OptionChainSnapshot(last_price=last_price, strikes=())
    if not isinstance(oc, Mapping):
        raise CandleParseError(f"Dhan option-chain 'oc' must be a mapping, got {type(oc).__name__}")

    rows: list[OptionChainStrike] = []
    for strike_key, sides in oc.items():
        if not isinstance(sides, Mapping):
            continue  # skip a malformed strike entry (never crash the whole chain)
        strike = _strike_key_to_decimal(strike_key)
        if strike is None:
            continue  # unparseable / non-positive strike key — skip
        ce = sides.get("ce")
        pe = sides.get("pe")
        rows.append(
            OptionChainStrike(
                strike=strike,
                ce=ce if isinstance(ce, Mapping) else {},
                pe=pe if isinstance(pe, Mapping) else {},
            )
        )
    rows.sort(key=lambda row: row.strike)
    return OptionChainSnapshot(last_price=last_price, strikes=tuple(rows))


def _strike_key_to_decimal(raw: object) -> Decimal | None:
    """Parse an option-chain STRIKE KEY to a positive Decimal, or ``None`` to skip.

    Unlike :func:`_price_or_none` (which raises on a non-blank unparseable value), a
    malformed / non-positive strike KEY is SKIPPED (never crash the whole chain on one
    vendor quirk), so both an unparseable key and a zero/negative key return ``None``.
    """
    if is_absent(raw) or has_no_price(raw):
        return None
    try:
        return to_price(raw, "strike")
    except CandleParseError:
        return None


def nameable_option_chain_fields(side: Mapping[str, object]) -> dict[str, object]:
    """Extract ONLY the option-chain fields we can name (UNVERIFIED assumed keys).

    The exact per-strike Greek NAMES are UNVERIFIED, so this returns exactly the keys
    in :data:`_OPTION_CHAIN_FIELDS_UNVERIFIED` that are PRESENT on *side* — surfacing
    what we can name without fabricating a field. A key absent from *side* is omitted
    (never a fabricated ``None``). O(fields).
    """
    return {key: side[key] for key in _OPTION_CHAIN_FIELDS_UNVERIFIED if key in side}


def parse_expiry_list(payload: Mapping[str, object]) -> list[date]:
    """Parse a Dhan expiry-list payload into ``date`` objects.

    VERIFIED shape: ``{data: ["YYYY-MM-DD", ...], status: "success"}``. Returns the
    dates in ascending order (deduplicated). A blank / non-string entry is skipped
    gracefully; an unparseable date string raises (a genuine shape problem).

    Raises
    ------
    CandleParseError
        If ``data`` is missing / non-array, or an entry is a non-blank unparseable
        date string.
    """
    if "data" not in payload:
        raise CandleParseError("missing required 'data' key in Dhan expiry-list payload")
    data = payload["data"]
    if not isinstance(data, (list, tuple)):
        raise CandleParseError(f"'data' must be an array, got {type(data).__name__}")
    seen: set[date] = set()
    for entry in data:
        if is_absent(entry):
            continue  # skip a blank / null expiry entry gracefully
        if not isinstance(entry, str):
            raise CandleParseError(f"expiry entry must be a date string, got {entry!r}")
        try:
            seen.add(date.fromisoformat(entry.strip()))
        except ValueError as exc:
            raise CandleParseError(f"unparseable Dhan expiry date: {entry!r}") from exc
    return sorted(seen)


__all__ = [
    "CandleParseError",
    "EmptyCandleRow",
    "OptionChainSnapshot",
    "OptionChainStrike",
    "nameable_option_chain_fields",
    "parse_candle_columns",
    "parse_expiry_list",
    "parse_option_chain",
    "parse_rolling_option",
    "parse_timestamp_epoch",
]
