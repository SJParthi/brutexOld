"""Pure dataclasses for data-lake records.

No ``polars`` / ``duckdb`` / ``pyarrow`` imports live in this module —
the types are pure Python so tests and CLI code can construct and
inspect them without installing P2 data-lake dependencies.

## Types

* :class:`CandleBar` — one OHLCV bar (spot underlying), in the
  normalised lake layout produced by the vendor parser.
* :class:`OptionBar` — one expired-option OHLC bar (one ``ce`` / ``pe``
  side) from the vendor's rolling-option endpoint; carries the resolved
  ``strike`` / ``spot`` / ``iv`` the rolling endpoint stamps per bar.
* :class:`CatalogEntry` — one row in the DuckDB catalog, defined per
  ``FR-CAT-002``.
"""

from __future__ import annotations

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
        Naive timestamp, negative volume / open interest, a NEGATIVE OHLC price,
        or OHLC that is not ``low <= open, close <= high`` (``high >= low``).
    """
    if timestamp.tzinfo is None:
        raise ValueError(
            f"{owner}.timestamp must be timezone-aware, got naive datetime {timestamp!r}"
        )
    if volume < 0:
        raise ValueError(f"{owner}.volume must be non-negative, got {volume}")
    if open_interest is not None and open_interest < 0:
        raise ValueError(f"{owner}.open_interest must be non-negative or None, got {open_interest}")
    # NEGATIVE-price guard (DEC-CANDLE-NEGATIVE-PRICE-REJECT-001): a traded Indian
    # instrument's price is always positive (tick ≥ 0.05, rupees), so a ``< 0`` OHLC
    # value is impossible vendor data — never a valid bar. Checked on the LOW (the
    # bar's minimum) so a single comparison rejects any negative among the four
    # prices; the high<low + open/close-in-range checks below then pin the rest.
    # DEFENSIVE: the assembler path already loud-rejects a negative price upstream
    # (`candle_fields.to_price`); this is the COMMON `CandleBar`/`OptionBar` invariant
    # so a directly-constructed bar can never hold a fabricated negative price either.
    if low < 0:
        raise ValueError(f"{owner}: price must be non-negative, got low ({low})")
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
