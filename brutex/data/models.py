"""Pure dataclasses for data-lake records.

No ``polars`` / ``duckdb`` / ``pyarrow`` imports live in this module —
the types are pure Python so tests and CLI code can construct and
inspect them without installing P2 data-lake dependencies.

## Types

* :class:`CandleBar` — one OHLCV bar, matches the Groww historical
  candle row layout defined in Groww historical data API docs.
* :class:`CatalogEntry` — one row in the DuckDB catalog, defined per
  ``FR-CAT-002``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Final

#: sha256 hex digest length (64 chars = 256 bits / 4 bits per nibble).
SHA256_HEX_LEN: Final[int] = 64

#: Number of months in a Gregorian calendar year. The trailing comma is
#: not a typo — the name exists so ``CatalogEntry`` validation reads as
#: a sentence (``1 <= month <= MONTHS_PER_YEAR``).
MONTHS_PER_YEAR: Final[int] = 12


@dataclass(frozen=True, slots=True)
class CandleBar:
    """One OHLCV bar row as written to Parquet.

    Matches the 7-tuple layout returned by Groww's
    ``get_historical_candles``:

    ``[timestamp, open, high, low, close, volume, open_interest_or_none]``

    We use ``Decimal`` for price fields to keep round-trip equality
    exact (float arithmetic introduces rounding drift which would
    fail the byte-for-byte reproducibility check in
    ``NFR-DET-001``). Volume and open interest are integers since
    Groww always delivers them as integer shares/contracts.

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
        if self.timestamp.tzinfo is None:
            raise ValueError(
                f"CandleBar.timestamp must be timezone-aware, got naive datetime {self.timestamp!r}"
            )
        if self.volume < 0:
            raise ValueError(f"CandleBar.volume must be non-negative, got {self.volume}")
        if self.open_interest is not None and self.open_interest < 0:
            raise ValueError(
                f"CandleBar.open_interest must be non-negative or None, got {self.open_interest}"
            )
        # Price ordering sanity — allow equal (limit-locked bars).
        if self.high < self.low:
            raise ValueError(f"CandleBar: high ({self.high}) must be >= low ({self.low})")
        if not (self.low <= self.open <= self.high):
            raise ValueError(
                f"CandleBar: open ({self.open}) must be in [low={self.low}, high={self.high}]"
            )
        if not (self.low <= self.close <= self.high):
            raise ValueError(
                f"CandleBar: close ({self.close}) must be in [low={self.low}, high={self.high}]"
            )


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
        Full Groww trading symbol, e.g. ``NSE-NIFTY-02Jan25-28500-PE``.
    interval
        the Groww interval name, e.g. ``1minute``, ``1day``.
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
