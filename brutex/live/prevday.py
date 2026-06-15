"""Prev-day OHLCV reference layer for the live dashboard (TickVault parity).

The operator asked for a prior-session **Open / High / Low / Close / Volume +
prev-close** reference per instrument — the same prev-day strip TickVault /
QuestDB surface beside the live tape. This module is the WRITE half: a pure
reader that pulls each instrument's prior **completed trading day** daily bar
out of the historical Parquet lake, plus a Parquet writer that persists the
result so the dashboard can later read it as plain Parquet.

DEC-LIVE-DECOUPLE-001 holds end-to-end: the dashboard imports NOTHING from
``brutex.live``; it reads the ``<lake_root>/live/prevday/dt=<as_of_date>.parquet``
artifact this module writes. Nothing here touches a vendor network — the read
path consumes the *existing* historical daily lake only.

## Where the daily bar lives

The historical lake stores the ``1day`` spot candle (the VERIFY-FIRST default
pull granularity per CLAUDE.md §6 / DEC-PULL-1DAY-FIRST-001) under the canonical
partition layout (``FR-CAT-001``)::

    <lake_root>/bars/<exchange>/<segment>/<symbol>/1day/<yyyy>/<mm>.parquet

with the 7-column candle schema ``timestamp (µs UTC) / open / high / low /
close (Float64 rupees) / volume (Int64) / open_interest (Int64|null)``
(``brutex/data/parquet_writer.py``; ``brutex/data/models.py`` ``CandleBar``).
Instrument identity is the partition path — there is NO ``instrument_key``
column — so an ``instrument_key`` here is the lake coordinate triple
``"<EXCH>:<SEGMENT>:<SYMBOL>"`` (e.g. ``"NSE:INDEX:NSE-NIFTY"``), the same
colon form ``brutex.live.lake.split_instrument_key`` parses, with the historical
``SEGMENT`` already in lake terms (``INDEX`` / ``CASH`` / ``FNO``).

## Money rule

Prices are snapped to the 2-decimal tick grid with ``round_money``
(``ROUND_HALF_UP``) at THIS write boundary, mirroring the spot lake
(DEC-SPOT-PRICE-2DP-001 / DEC-MONEY-2DP-001). Volume / open-interest are
integers and are NEVER rounded.

## Previous-completed-trading-day resolution

The "previous day" is the latest NSE/BSE trading day **strictly before**
``as_of_date`` (weekends + holidays skipped) via
:func:`brutex.calendars.nse.is_trading_day` — so a Monday resolves back to the
prior Friday and a post-holiday session resolves past the holiday.

## Honest degradation

A missing instrument or a missing prior-day bar is **omitted** from the result
dict (never raised); an empty daily lake yields ``{}`` and a schema-correct
empty Parquet file. The layer never invents data (Golden Rule #1 / #10).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import polars as pl
import structlog

from brutex.calendars.nse import IST, is_trading_day
from brutex.data.errors import PartitionError
from brutex.data.parquet_writer import ROW_GROUP_SIZE, ZSTD_LEVEL
from brutex.data.partition import build_partition_path
from brutex.live.lake import split_instrument_key
from brutex.money import round_money

_LOG = structlog.get_logger("brutex.live.prevday")

#: Lake interval label of the daily bar (long form; ``brutex.data.timeframes``
#: ``LAKE_INTERVAL_LABELS[375]``). The prev-day strip reads ONLY this partition.
_DAILY_INTERVAL = "1day"

#: Top-level lake subtree for the prev-day reference artifact (sibling of the
#: ``live/ticks`` + ``live/candles_1m`` tables in :mod:`brutex.live.lake`).
LIVE_DIR = "live"
_PREVDAY_DIR = "prevday"

#: Canonical Parquet schema for the prev-day artifact the dashboard reads.
#: ``prev_close`` is the prior session's daily CLOSE — identical to ``close``
#: here (the resolved day IS the prior session), surfaced as its own column so
#: the dashboard's "prev close" chip is a direct read, not a derivation.
PREVDAY_SCHEMA: dict[str, pl.DataType] = {
    "instrument_key": pl.String(),
    "session_date": pl.Date(),
    "open": pl.Float64(),
    "high": pl.Float64(),
    "low": pl.Float64(),
    "close": pl.Float64(),
    "volume": pl.Int64(),
    "open_interest": pl.Int64(),
    "prev_close": pl.Float64(),
}
_PREVDAY_PK = ("instrument_key",)


@dataclass(frozen=True, slots=True)
class PrevDayOHLCV:
    """One instrument's prior-session daily OHLCV reference row.

    Attributes
    ----------
    instrument_key
        Lake coordinate triple ``"<EXCH>:<SEGMENT>:<SYMBOL>"`` (e.g.
        ``"NSE:INDEX:NSE-NIFTY"``).
    session_date
        The resolved previous completed trading day (IST calendar date).
    open, high, low, close
        Prior-session daily prices, exact :class:`~decimal.Decimal` rupees
        snapped to two decimals (``round_money``, ``ROUND_HALF_UP``).
    volume
        Prior-session daily volume — an integer, NEVER rounded.
    open_interest
        Prior-session open interest (``None`` for non-FNO instruments).
    """

    instrument_key: str
    session_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    open_interest: int | None


def previous_trading_day(as_of_date: date) -> date:
    """Return the latest trading day strictly before *as_of_date*.

    Walks back one calendar day at a time, skipping weekends + NSE/BSE
    holidays via :func:`brutex.calendars.nse.is_trading_day`, so a Monday
    resolves to the prior Friday and a post-holiday session resolves past
    the holiday (and the weekend behind it).

    Parameters
    ----------
    as_of_date
        The reference (today's) IST trading date.

    Returns
    -------
    date
        The previous completed trading day.
    """
    candidate = as_of_date - timedelta(days=1)
    while not is_trading_day(candidate):
        candidate -= timedelta(days=1)
    return candidate


def _bar_ist_date(timestamp: datetime) -> date:
    """IST calendar (trading) date of a daily bar's UTC *timestamp*.

    Mirrors ``brutex.dashboard.db_view._bar_ist_date``: a 1-day bar may be
    stamped at IST midnight or at a session instant, so the trading day is the
    IST date — never a raw UTC ``CAST`` (DEC-DB-CANDLE-DERIVED-001).
    """
    return timestamp.astimezone(IST).date()


def _read_daily_bar(
    lake_root: Path, *, exchange: str, segment: str, symbol: str, session_date: date
) -> dict[str, object] | None:
    """Read the single ``1day`` bar for *session_date* from the historical lake.

    Computes the canonical partition file via
    :func:`brutex.data.partition.build_partition_path` (the COMMON helper — no
    path duplication), reads ONLY that one ``<yyyy>/<mm>.parquet`` month file,
    and filters to the bar whose IST trading date equals *session_date*. Returns
    the row as a dict, or ``None`` when the partition file or the day's bar is
    absent (graceful — the caller omits it).
    """
    partition = build_partition_path(
        lake_root=lake_root,
        exchange=exchange,
        segment=segment,
        symbol=symbol,
        interval=_DAILY_INTERVAL,
        year=session_date.year,
        month=session_date.month,
    )
    if not partition.exists():
        return None
    frame = pl.read_parquet(partition)
    for row in frame.iter_rows(named=True):
        if _bar_ist_date(row["timestamp"]) == session_date:
            return row
    return None


def _to_row(instrument_key: str, session_date: date, raw: dict[str, object]) -> PrevDayOHLCV:
    """Snap a raw lake bar dict into a :class:`PrevDayOHLCV` (2dp prices, raw volume)."""
    return PrevDayOHLCV(
        instrument_key=instrument_key,
        session_date=session_date,
        open=round_money(Decimal(str(raw["open"]))),
        high=round_money(Decimal(str(raw["high"]))),
        low=round_money(Decimal(str(raw["low"]))),
        close=round_money(Decimal(str(raw["close"]))),
        volume=int(raw["volume"]),  # type: ignore[call-overload]
        open_interest=None if raw["open_interest"] is None else int(raw["open_interest"]),  # type: ignore[call-overload]
    )


def load_prev_day(
    lake_root: str | Path,
    instrument_keys: Iterable[str],
    as_of_date: date,
    *,
    resolve_previous: bool = True,
) -> dict[str, PrevDayOHLCV]:
    """Load each instrument's prior-session daily OHLCV from the historical lake.

    Pure + read-only: resolves the previous completed trading day strictly
    before *as_of_date* (when *resolve_previous* — the default — else reads
    *as_of_date* itself), then reads that session's ``1day`` bar per instrument
    from ``<lake_root>/bars/...`` and returns an O(1) dict keyed by
    ``instrument_key``. No vendor network call. A missing instrument / missing
    day is OMITTED from the dict (never raised); a debug log records the miss.

    Parameters
    ----------
    lake_root
        Top-level lake directory.
    instrument_keys
        Lake coordinate triples ``"<EXCH>:<SEGMENT>:<SYMBOL>"``.
    as_of_date
        The reference (today's) IST trading date.
    resolve_previous
        When ``True`` (default) read the previous completed trading day; when
        ``False`` read *as_of_date* itself (the prev-day bar of a downstream
        caller that already resolved the date).

    Returns
    -------
    dict[str, PrevDayOHLCV]
        One entry per instrument that HAS a bar on the resolved day. Instruments
        whose key is malformed, whose partition is absent, or whose day's bar is
        missing are omitted.
    """
    root = Path(lake_root)
    session_date = previous_trading_day(as_of_date) if resolve_previous else as_of_date
    out: dict[str, PrevDayOHLCV] = {}
    for instrument_key in instrument_keys:
        exchange, segment, symbol = split_instrument_key(instrument_key)
        if not (exchange and segment and symbol):
            _LOG.debug("prevday.skip_malformed_key", instrument_key=instrument_key)
            continue
        try:
            raw = _read_daily_bar(
                root,
                exchange=exchange,
                segment=segment,
                symbol=symbol,
                session_date=session_date,
            )
        except (PartitionError, OSError) as exc:
            # Unknown exchange/segment/interval coordinate (PartitionError) or an
            # unreadable partition (OSError) → omit honestly, never crash the strip.
            _LOG.debug(
                "prevday.skip_unreadable",
                instrument_key=instrument_key,
                error=str(exc),
            )
            continue
        if raw is None:
            _LOG.debug(
                "prevday.miss",
                instrument_key=instrument_key,
                session_date=str(session_date),
            )
            continue
        out[instrument_key] = _to_row(instrument_key, session_date, raw)
    return out


def rows_to_frame(rows: Sequence[PrevDayOHLCV]) -> pl.DataFrame:
    """Render prev-day *rows* into the canonical :data:`PREVDAY_SCHEMA` frame.

    ``prev_close`` equals each row's ``close`` (the resolved day IS the prior
    session) — materialised as its own column so the dashboard's prev-close chip
    is a direct read. Prices are ``float`` of the already-2dp-snapped
    :class:`~decimal.Decimal`; volume / OI stay integers.
    """
    records: list[dict[str, object]] = [
        {
            "instrument_key": row.instrument_key,
            "session_date": row.session_date,
            "open": float(row.open),
            "high": float(row.high),
            "low": float(row.low),
            "close": float(row.close),
            "volume": row.volume,
            "open_interest": row.open_interest,
            "prev_close": float(row.close),
        }
        for row in rows
    ]
    return pl.DataFrame(records, schema=PREVDAY_SCHEMA, orient="row")


def _partition_path(lake_root: str | Path, as_of_date: date) -> Path:
    """``<lake_root>/live/prevday/dt=<as_of_date>.parquet``."""
    return Path(lake_root) / LIVE_DIR / _PREVDAY_DIR / f"dt={as_of_date}.parquet"


def write_prev_day(
    lake_root: str | Path,
    rows: Iterable[PrevDayOHLCV],
    as_of_date: date,
) -> Path:
    """Persist prev-day *rows* to ``<lake_root>/live/prevday/dt=<as_of_date>.parquet``.

    Idempotent overwrite-by-PK: the partition is REPLACED (never merged) on
    rerun, deduped on ``instrument_key`` (``keep="last"``) and sorted by key, so
    the same inputs yield a byte-identical file (Golden Rule #7). The Parquet is
    written with the SAME reproducible options as the bars-lake
    :class:`~brutex.data.parquet_writer.ParquetWriter` (``zstd`` /
    ``ZSTD_LEVEL`` / ``ROW_GROUP_SIZE`` / ``statistics=False`` — stats are
    non-deterministic), so byte-identity holds across toolchain versions. An
    empty *rows* writes a schema-correct empty frame — degrade honestly, never
    invent.

    Parameters
    ----------
    lake_root
        Top-level lake directory.
    rows
        The prev-day rows (e.g. ``load_prev_day(...).values()``).
    as_of_date
        The reference date — the partition key (``dt=<as_of_date>``).

    Returns
    -------
    pathlib.Path
        The written partition file.
    """
    frame = (
        rows_to_frame(list(rows))
        .unique(subset=list(_PREVDAY_PK), keep="last")
        .sort(list(_PREVDAY_PK))
    )
    dest = _partition_path(lake_root, as_of_date)
    dest.parent.mkdir(parents=True, exist_ok=True)
    frame.write_parquet(
        dest,
        compression="zstd",
        compression_level=ZSTD_LEVEL,
        row_group_size=ROW_GROUP_SIZE,
        use_pyarrow=False,
        statistics=False,  # stats are non-deterministic; skip for reproducibility
    )
    return dest


def read_prev_day(lake_root: str | Path, as_of_date: date) -> pl.DataFrame:
    """Read the prev-day partition for *as_of_date* (empty schema-frame if absent).

    The read half a downstream reader (or the dashboard, as plain Parquet) uses
    to surface the prev-day strip.
    """
    dest = _partition_path(lake_root, as_of_date)
    if not dest.exists():
        return pl.DataFrame(schema=dict(PREVDAY_SCHEMA))
    return pl.read_parquet(dest)


__all__ = [
    "LIVE_DIR",
    "PREVDAY_SCHEMA",
    "PrevDayOHLCV",
    "load_prev_day",
    "previous_trading_day",
    "read_prev_day",
    "rows_to_frame",
    "write_prev_day",
]
