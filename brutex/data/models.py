"""Pure dataclasses for data-lake records.

No ``polars`` / ``duckdb`` / ``pyarrow`` imports live in this module —
the types are pure Python so tests and CLI code can construct and
inspect them without installing P2 data-lake dependencies.

## Types

* :class:`CandleBar` — one OHLCV bar (spot underlying), in the
  normalised lake layout produced by the vendor parser.
* :class:`FnoGreeksBar` — one ENRICHED F&O option 1-minute bar: the 7 OHLCV
  fields plus the ten nullable Greeks-overlay fields the generation-time
  Greeks store appends (`DEC-GREEKS-GENERATION-TIME-001`, plan §15.3).
* :class:`OptionBar` — one expired-option OHLC bar (one ``ce`` / ``pe``
  side) from the vendor's rolling-option endpoint; carries the resolved
  ``strike`` / ``spot`` / ``iv`` the rolling endpoint stamps per bar.
* :class:`CatalogEntry` — one row in the DuckDB catalog, defined per
  ``FR-CAT-002``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Final


def _validate_price_bar(
    *,
    owner: str,
    timestamp: datetime,
    open_: Decimal,
    high: Decimal,
    low: Decimal,
    close: Decimal,
    volume: int,
    open_interest: int | None,
) -> None:
    """Validate the OHLCV invariants shared by every price bar (§11 COMMON).

    One source of truth for "is this a well-formed bar?" so
    :class:`CandleBar` and :class:`OptionBar` cannot drift. *owner* is the
    calling class name, woven into each message so errors read naturally.

    Raises
    ------
    ValueError
        Naive timestamp, negative volume / open interest, a non-positive
        price, or OHLC that is not ``low <= open, close <= high``
        (``high >= low``).
    """
    if timestamp.tzinfo is None:
        raise ValueError(
            f"{owner}.timestamp must be timezone-aware, got naive datetime {timestamp!r}"
        )
    if volume < 0:
        raise ValueError(f"{owner}.volume must be non-negative, got {volume}")
    if open_interest is not None and open_interest < 0:
        raise ValueError(f"{owner}.open_interest must be non-negative or None, got {open_interest}")
    # Price POSITIVITY (DEC-STORED-DATA-VALIDATION-001): a traded Indian price
    # is never <= 0 (tick >= 0.05 rupees — the same market fact behind
    # `candle_fields.has_no_price`). Checked BEFORE the ordering checks below,
    # because an internally-ORDERED all-negative bar (open=-5, high=-1,
    # low=-10, close=-2) satisfies every ordering invariant and previously
    # passed this validator SILENTLY — the round-2 audit reproduced a
    # day_low of -1000 flowing into Stage-1A + run identity from exactly such
    # a legacy stored bar. Parse-time already screens <= 0 (#912
    # `has_no_price`), so this closes the READ boundary for pre-#912 legacy
    # bars. O(1) per bar — four fixed comparisons, mirroring the per-bar
    # ordering mechanism below.
    for price_name, price in (("open", open_), ("high", high), ("low", low), ("close", close)):
        if price <= 0:
            raise ValueError(
                f"{owner}.{price_name} must be a positive price, got {price} "
                f"(bar timestamp {timestamp.isoformat()}) — a traded Indian price "
                f"is never <= 0; a stored non-positive price is corrupt data"
            )
    # Price ordering sanity — allow equal (limit-locked bars).
    if high < low:
        raise ValueError(f"{owner}: high ({high}) must be >= low ({low})")
    if not (low <= open_ <= high):
        raise ValueError(f"{owner}: open ({open_}) must be in [low={low}, high={high}]")
    if not (low <= close <= high):
        raise ValueError(f"{owner}: close ({close}) must be in [low={low}, high={high}]")


#: sha256 hex digest length (64 chars = 256 bits / 4 bits per nibble).
SHA256_HEX_LEN: Final[int] = 64

#: Number of months in a Gregorian calendar year. The trailing comma is
#: not a typo — the name exists so ``CatalogEntry`` validation reads as
#: a sentence (``1 <= month <= MONTHS_PER_YEAR``).
MONTHS_PER_YEAR: Final[int] = 12


@dataclass(frozen=True, slots=True)
class CandleBar:
    """One OHLCV bar row as written to Parquet.

    Matches the canonical 7-field candle tuple
    (open/high/low/close/volume/timestamp/open_interest):

    ``[timestamp, open, high, low, close, volume, open_interest_or_none]``

    We use ``Decimal`` for price fields to keep round-trip equality
    exact (float arithmetic introduces rounding drift which would
    fail the byte-for-byte reproducibility check in
    ``NFR-DET-001``). Volume and open-interest are integer
    shares/contracts.

    Attributes
    ----------
    timestamp
        UTC timezone-aware datetime at the bar's open.
    open, high, low, close
        Decimal price fields.
    volume
        Integer trade volume.
    open_interest
        Integer open interest. Only meaningful for F&O symbols;
        cash-equity bars carry ``None`` here.
    """

    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    open_interest: int | None

    def __post_init__(self) -> None:
        _validate_price_bar(
            owner="CandleBar",
            timestamp=self.timestamp,
            open_=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
            open_interest=self.open_interest,
        )


#: The ten nullable Greeks-overlay columns appended after ``open_interest`` in an
#: ENRICHED F&O 1-minute partition (`DEC-GREEKS-GENERATION-TIME-001`, plan §15.3 /
#: ADR-0015 D11) — the ONE canonical field/column ORDER shared by
#: :class:`FnoGreeksBar`, the :mod:`brutex.data.parquet_writer` render/read-back, and
#: :mod:`brutex.data.fno_candle_lake` (CLAUDE.md §11 COMMON; never re-declared).
#: ``iv``…``rho`` are FULL-precision floats (DEC-MONEY-2DP-001 forbids rounding
#: statistical values); ``spot_at_bar`` is the ONE 2dp-snapped price;
#: ``t_years_used`` / ``rate_used`` / ``greeks_provenance_id`` are the mandatory
#: model-input provenance stamp (§15.3 — a bare Greek with no stamp would re-create
#: the silent-staleness hazard §6 warns against).
GREEKS_OVERLAY_FIELDS: Final[tuple[str, ...]] = (
    "iv",
    "delta",
    "gamma",
    "theta",
    "vega",
    "rho",
    "spot_at_bar",
    "t_years_used",
    "rate_used",
    "greeks_provenance_id",
)

#: Exclusive upper bound of a stored ``greeks_provenance_id`` — the value is written
#: to a Parquet ``Int32`` column, so it must fit a signed 32-bit non-negative range.
GREEKS_PROVENANCE_ID_BOUND: Final[int] = 2**31


@dataclass(frozen=True, slots=True)
class FnoGreeksBar(CandleBar):
    """One ENRICHED F&O option 1-minute bar: OHLCV+OI plus the Greeks overlay.

    The dedicated bar type of the generation-time Greeks store
    (`DEC-GREEKS-GENERATION-TIME-001`, plan §15.3 "Bar-model note"): a frozen
    slotted subclass of :class:`CandleBar` carrying the ten nullable overlay
    fields (:data:`GREEKS_OVERLAY_FIELDS`), so a plain spot ``CandleBar`` never
    carries always-``None`` Greek fields and every existing sink/writer accepts
    it unchanged (``isinstance(bar, CandleBar)`` holds). The 7 OHLCV fields
    reuse :func:`_validate_price_bar`; the Greek fields validate their own
    envelope (finite-or-``None`` — a NaN / inf Greek is never stored, mirroring
    the kernel's never-NaN contract) and skip price validation.

    Attributes
    ----------
    iv, delta, gamma, theta, vega, rho
        The solved Greeks — full-precision floats, or ``None`` when honestly
        unsolvable (the stamped-NULL family of plan §15.8).
    spot_at_bar
        Underlying spot close at the SAME minute (₹ Decimal, 2dp-snapped at
        the write boundary), or ``None`` when no spot minute exists.
    t_years_used, rate_used
        The time-to-expiry / risk-free inputs frozen into THIS bar's Greeks
        (``None`` on a NULL-Greeks bar — no input was consumed).
    greeks_provenance_id
        The packed provenance stamp (:mod:`brutex.data.greeks_enrich`), the
        staleness-detection key. ``None`` only on a never-enriched bar.
    """

    iv: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    rho: float | None = None
    spot_at_bar: Decimal | None = None
    t_years_used: float | None = None
    rate_used: float | None = None
    greeks_provenance_id: int | None = None

    def __post_init__(self) -> None:
        _validate_price_bar(
            owner="FnoGreeksBar",
            timestamp=self.timestamp,
            open_=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
            open_interest=self.open_interest,
        )
        for name in ("iv", "delta", "gamma", "theta", "vega", "rho", "t_years_used", "rate_used"):
            value: float | None = getattr(self, name)
            if value is not None and not math.isfinite(value):
                raise ValueError(f"FnoGreeksBar.{name} must be finite or None, got {value!r}")
        if self.spot_at_bar is not None and self.spot_at_bar <= 0:
            raise ValueError(
                f"FnoGreeksBar.spot_at_bar must be positive or None, got {self.spot_at_bar}"
            )
        pid = self.greeks_provenance_id
        if pid is not None and not (0 <= pid < GREEKS_PROVENANCE_ID_BOUND):
            raise ValueError(
                f"FnoGreeksBar.greeks_provenance_id must be a non-negative Int32, got {pid}"
            )


#: The two option legs the rolling-option endpoint returns per call.
OPTION_SIDES: Final[frozenset[str]] = frozenset({"CE", "PE"})


@dataclass(frozen=True, slots=True)
class OptionBar:
    """One expired-option OHLC bar from ``/charts/rollingoption`` (one side).

    Track 1 / lake (DEC-P0-063) options live ONLY on the rolling endpoint
    (the vendor's expired-options data source) — every backtest contract
    is already expired. The endpoint returns per-side (``ce`` / ``pe``)
    columnar arrays and stamps, per bar, the **resolved** ``strike`` (the
    relative ``ATM±N`` selector resolved to an absolute strike), the
    underlying ``spot``, and the implied volatility ``iv``. We keep all three
    so the lake loses nothing (operator decree 2026-05-31 — "capture
    EVERYTHING").

    Prices (``open``/``high``/``low``/``close``/``strike``/``spot``) are exact
    :class:`~decimal.Decimal` rupees, mirroring :class:`CandleBar` (the
    paisa ``× 100`` happens at the lake-write boundary, never here).

    Attributes
    ----------
    timestamp
        UTC timezone-aware datetime at the bar's open.
    open, high, low, close
        Decimal price fields (rupees).
    volume
        Integer traded volume.
    open_interest
        Integer open interest, or ``None`` when the side omits it.
    side
        ``"CE"`` or ``"PE"`` — which leg this bar belongs to.
    strike
        Resolved absolute strike (rupees), or ``None`` if the side omits it.
    spot
        Underlying spot at the bar (rupees), or ``None`` if omitted.
    iv
        Implied volatility, or ``None`` if omitted.
    """

    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    open_interest: int | None
    side: str
    strike: Decimal | None
    spot: Decimal | None
    iv: Decimal | None

    def __post_init__(self) -> None:
        _validate_price_bar(
            owner="OptionBar",
            timestamp=self.timestamp,
            open_=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
            open_interest=self.open_interest,
        )
        if self.side not in OPTION_SIDES:
            raise ValueError(
                f"OptionBar.side must be one of {sorted(OPTION_SIDES)}, got {self.side!r}"
            )
        if self.strike is not None and self.strike <= 0:
            raise ValueError(f"OptionBar.strike must be positive or None, got {self.strike}")
        if self.spot is not None and self.spot <= 0:
            raise ValueError(f"OptionBar.spot must be positive or None, got {self.spot}")
        if self.iv is not None and self.iv < 0:
            raise ValueError(f"OptionBar.iv must be non-negative or None, got {self.iv}")


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    """One row in the DuckDB catalog (``FR-CAT-002``).

    Each row describes exactly one Parquet partition file written to
    the data lake. The ``(exchange, segment, symbol, interval, year,
    month)`` tuple is unique — the catalog enforces a primary key on
    it. ``raw_hash`` is the sha256 of the partition file's bytes,
    used for the reproducibility check and for the
    ``data_snapshot_hash`` rollup defined in ``FR-CAT-004``.

    Attributes
    ----------
    exchange
        NSE or BSE (ISO alpha string).
    segment
        CASH or FNO.
    symbol
        Full canonical contract symbol, e.g. ``NSE-NIFTY-02Jan25-28500-PE``.
    interval
        the canonical interval name, e.g. ``1minute``, ``1day``.
    year, month
        Partition key — calendar year/month of the bars in the file.
    raw_hash
        sha256 hex digest of the Parquet file bytes (64 chars).
    row_count
        Number of bars in the file.
    first_ts, last_ts
        Timestamp bounds of the bars in the file (UTC).
    fetched_at
        When BruteX wrote the file (UTC).
    """

    exchange: str
    segment: str
    symbol: str
    interval: str
    year: int
    month: int
    raw_hash: str
    row_count: int
    first_ts: datetime
    last_ts: datetime
    fetched_at: datetime

    def __post_init__(self) -> None:
        if len(self.raw_hash) != SHA256_HEX_LEN:
            raise ValueError(
                f"CatalogEntry.raw_hash must be {SHA256_HEX_LEN}-char sha256 hex, "
                f"got {len(self.raw_hash)} chars"
            )
        if not (1 <= self.month <= MONTHS_PER_YEAR):
            raise ValueError(
                f"CatalogEntry.month must be in [1, {MONTHS_PER_YEAR}], got {self.month}"
            )
        if self.row_count < 0:
            raise ValueError(f"CatalogEntry.row_count must be non-negative, got {self.row_count}")
        # Validate tz-awareness BEFORE any datetime comparison — comparing
        # an aware datetime against a naive one raises TypeError instead
        # of letting the guard below fire cleanly.
        for ts_name, ts in (
            ("first_ts", self.first_ts),
            ("last_ts", self.last_ts),
            ("fetched_at", self.fetched_at),
        ):
            if ts.tzinfo is None:
                raise ValueError(
                    f"CatalogEntry.{ts_name} must be timezone-aware, got naive datetime {ts!r}"
                )
        if self.first_ts > self.last_ts:
            raise ValueError(
                f"CatalogEntry: first_ts ({self.first_ts}) must be <= last_ts ({self.last_ts})"
            )
