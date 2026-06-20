"""Resample the missing **1-day** bars from stored **1-minute** (DEC-SPOT-1DAY-RESAMPLE-ON-EMPTY-001).

Why this module exists
----------------------
Groww's ``get_historical_candles(candle_interval="1day", segment="CASH")`` does
**not** serve the most-recent ~6 months of DAILY bars for cash equities (single
stocks), while the SAME range IS served at ``1minute`` granularity — and the
``IDX`` indices' ``1day`` is unaffected. A recent universe pull surfaced ~83
``1day`` windows EMPTY (zero ``1minute``), 48/50 stocks on the identical
``2025-11-30 → 2026-05-29`` window, with the 1-minute data present. So the daily
data EXISTS; it is derivable by resampling the 1-minute already in the lake. This
module derives those missing daily bars from 1-minute so a confirmed-empty daily
window is FILLED instead of being settled SKIPPED forever
(superseding DEC-OLD-GAP-FAST-SETTLE-001 for any window 1-minute can supply).

The midnight-IST stamping rule (the linchpin — DO NOT change without re-verifying)
----------------------------------------------------------------------------------
A vendor-fetched ``1day`` bar is stamped at **midnight IST** (``00:00`` IST =
``18:30`` UTC of the PREVIOUS calendar day), verified by
:func:`brutex.adapters.groww.contract_bounds.contract_daily_coverage` ("under
midnight-IST stamping a Jan-1-IST bar lives in the *December* UTC partition") and the
``test_daily_coverage_cross_utc_month_midnight_ist_golden`` /
``test_load_prev_day_ist_date_differs_from_utc_date`` golden tests. The shared
:func:`~brutex.data.tf_resampler.resample_1m_to_tf` labels its daily bucket at the
**09:15 IST** session open (``03:45`` UTC) instead — perfect for the brute-force
grid, WRONG for the lake's daily convention. So this module reuses that resampler
ONLY for the OHLCV aggregation (open=first / high=max / low=min / close=last /
volume=sum — CLAUDE.md §11 COMMON, never re-implemented) and then **re-stamps**
every derived bar to midnight IST using the SAME fixed +05:30 offset the Groww
parser uses (:data:`brutex.adapters.groww.parser`), so a resampled daily bar's UTC
instant is byte-identical IN STAMP/SCHEMA/OI to a vendor-parsed ``00:00:00 IST``
bar — the **timestamp** identity that makes the keep-first merge-on-write dedup a
later real vendor fetch (Golden Rule #7) and keeps the daily series uniform. The
OHLC **values** are a 1-minute aggregation (first-minute open / summed volume /
last-minute close), so they need NOT equal a vendor OFFICIAL daily (call-auction
open / block-deal volume / settlement close can differ); since the reconcile only
ever fills windows the vendor returned EMPTY there is no vendor bar to compete
with, and any future divergence surfaces as a logged keep-first ``BarConflict``
(visible, never a silent overwrite), NOT corruption.

Honest envelope (Golden Rule #1 / #10)
--------------------------------------
* **Never invents data.** When the lake holds NO 1-minute for the window, the fill
  is a no-op (``bars_written == 0``) — the daily window keeps whatever the ledger
  recorded (EMPTY → re-fetched next pull, or SKIPPED). The default verify-first
  ``1day``-only pull therefore self-heals nothing unless 1-minute was pulled too;
  that is surfaced LOUDLY by the caller, never papered over.
* **No partial/forming bar.** A session that is not yet complete (its IST date is
  on/after the ``before`` cutoff — today IST) is DROPPED, so today's still-forming
  session is never written as a final daily bar (no look-ahead).
* **Spot OI is null.** Resampled daily bars carry ``open_interest=None`` — exactly
  what the Groww parser stores for a cash-equity/index daily bar (OI is FNO-only).

Pure core, injected I/O (CLAUDE.md §4 / §11)
--------------------------------------------
:func:`resample_1m_to_daily` and :func:`fill_daily_from_minute` are pure; the lake
read + write are injected callables, so the whole control flow is 100%
unit-testable with fakes. :func:`reconcile_daily_from_minute` drives the per-window
fill over the unified :class:`~brutex.data.window_ledger.WindowLedger`, recording a
filled window ``SUCCESS`` with ``in_pull_fetches=0`` (the honest "no network fetch"
provenance — a resample made zero vendor calls). The raw lake builders
(:func:`make_lake_read_1m_window` / :func:`make_lake_write_1day`) are
``# pragma: no cover`` — integration-tested, not line-gated.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING

import structlog

from brutex.adapters.groww.constants import TRACK1_DAILY_INTERVAL
from brutex.data.fetch_coverage import WindowKey
from brutex.data.timeframes import DAILY_TF_MINUTES
from brutex.data.window_ledger import EMPTY, SKIPPED, WindowLedger, WindowLedgerRecord

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence
    from pathlib import Path

    import polars as pl

    from brutex.data.models import CandleBar

#: Fixed +05:30 IST — the SAME offset :mod:`brutex.adapters.groww.parser` uses to
#: localize a vendor ``00:00:00 IST`` daily timestamp, so the re-stamped UTC instant
#: of a resampled daily bar is byte-identical to a vendor-fetched one (the dedup
#: identity). India has no DST, so a fixed offset is exact for the 2020+ scope.
_IST = timezone(timedelta(hours=5, minutes=30))

#: Structured logger — every degrade (no 1-minute to fill from / a quarantined
#: window) is logged, so a resample-fill pass is visible in /logs (Golden Rule #10).
_LOG = structlog.get_logger("brutex.data.spot_daily_resample")

__all__ = [
    "DailyFillResult",
    "DailyReconcileReport",
    "DailyResampleWindow",
    "fill_daily_from_minute",
    "make_lake_read_1m_window",
    "make_lake_write_1day",
    "plan_daily_resample_windows",
    "reconcile_daily_from_minute",
    "resample_1m_to_daily",
]


@dataclass(frozen=True, slots=True)
class DailyFillResult:
    """Outcome of one :func:`fill_daily_from_minute` call.

    ``bars_written`` is whatever the injected ``write_1day`` returned (the sink's
    merge-on-write appended count — ``0`` when the 1-minute slice was empty OR every
    resampled bar already existed in the lake). ``first_ts`` / ``last_ts`` bound the
    bars that were resampled (``None`` when none), for the structured-log trail.
    """

    bars_written: int
    first_ts: datetime | None
    last_ts: datetime | None


@dataclass(frozen=True, slots=True)
class DailyResampleWindow:
    """One daily window the ledger marked empty that resample may be able to fill.

    Carries the exact :class:`~brutex.data.fetch_coverage.WindowKey` identity
    (``market`` / ``security_id`` / ``interval`` / ``[start, end)`` dates) so a
    successful fill re-records the SAME ledger row ``SUCCESS`` (never a duplicate).
    """

    market: str
    security_id: str
    interval: str
    start: date
    end: date


@dataclass(frozen=True, slots=True)
class DailyReconcileReport:
    """Aggregate outcome of a :func:`reconcile_daily_from_minute` pass.

    Attributes
    ----------
    filled
        Windows whose daily bars were derived from 1-minute and recorded ``SUCCESS``.
    no_minute
        Windows skipped because the lake held no 1-minute for them (honest no-op —
        the verify-first ``1day``-only default surfaces here when 1-minute was never
        pulled). The window keeps its prior EMPTY/SKIPPED status.
    errored
        Windows quarantined by a per-window error (a corrupt 1-minute slice / a write
        failure) — one bad window never blanks the pass (mirrors the ingest spine).
    bars
        Total daily bars written across every filled window.
    """

    filled: int
    no_minute: int
    errored: int
    bars: int

    @property
    def windows(self) -> int:
        """Total candidate windows considered (filled + no_minute + errored)."""
        return self.filled + self.no_minute + self.errored


def resample_1m_to_daily(df_1m: pl.DataFrame, *, before: date) -> list[CandleBar]:
    """Resample a 1-minute :class:`polars.DataFrame` to **midnight-IST** daily bars.

    Reuses :func:`~brutex.data.tf_resampler.resample_1m_to_tf` (TF
    :data:`~brutex.data.timeframes.DAILY_TF_MINUTES`) for the OHLCV aggregation —
    the single source of truth for the session bucketing — then re-stamps each
    derived bar from the resampler's 09:15-IST label to **midnight IST** (the vendor
    daily convention; see the module docstring) and converts the polars row to a
    :class:`~brutex.data.models.CandleBar` with ``open_interest=None`` (spot/index
    daily bars carry no OI).

    Parameters
    ----------
    df_1m
        The symbol's stored 1-minute bars as a polars frame with the resampler's
        required columns (``ts_utc, open, high, low, close, volume``), sorted
        ascending by ``ts_utc``. An empty frame yields an empty list.
    before
        The forming-session cutoff (today IST): any session whose IST date is
        ``>= before`` is DROPPED so a still-forming session is never emitted as a
        final daily bar (no look-ahead, no partial bar).

    Returns
    -------
    list[CandleBar]
        One midnight-IST-stamped daily bar per COMPLETE IST trading day present in
        ``df_1m``, ascending by timestamp. Prices stay full-precision ``Decimal``
        (the lake-write sink snaps to the 2-dp tick grid, DEC-SPOT-PRICE-2DP-001).
    """
    # Local import keeps polars out of this module's import graph for callers that
    # only need the dataclasses (CLAUDE.md §11 lazy dep, mirrors the materialiser).
    from brutex.data.models import CandleBar
    from brutex.data.tf_resampler import resample_1m_to_tf

    if df_1m.is_empty():
        return []

    daily = resample_1m_to_tf(df_1m, DAILY_TF_MINUTES)

    bars: list[CandleBar] = []
    for row in daily.iter_rows(named=True):
        ist_date = row["ts_utc"].astimezone(_IST).date()
        if ist_date >= before:
            # Still-forming / incomplete session — never written as a final bar.
            continue
        # Re-stamp to midnight IST (== 18:30 UTC of the prior calendar day) so the
        # UTC instant is byte-identical to a vendor-parsed "00:00:00 IST" daily bar.
        stamp_utc = datetime(ist_date.year, ist_date.month, ist_date.day, tzinfo=_IST).astimezone(
            UTC
        )
        bars.append(
            CandleBar(
                timestamp=stamp_utc,
                open=Decimal(str(row["open"])),
                high=Decimal(str(row["high"])),
                low=Decimal(str(row["low"])),
                close=Decimal(str(row["close"])),
                volume=int(row["volume"]),
                open_interest=None,
            )
        )
    return bars


def fill_daily_from_minute(
    *,
    read_1m: Callable[[], pl.DataFrame],
    write_1day: Callable[[Sequence[CandleBar]], int],
    before: date,
) -> DailyFillResult:
    """Read 1-minute, resample to midnight-IST daily, write — pure orchestration.

    Reads the window's 1-minute slice (via the injected *read_1m*), resamples it to
    daily (:func:`resample_1m_to_daily`), and — only when that yields at least one
    bar — writes the bars via the injected *write_1day*, returning the count. An
    empty 1-minute slice is an honest no-op (``bars_written == 0``, no write call),
    NEVER an invented bar. All lake/disk I/O is injected, so this is 100%
    unit-testable.

    Parameters
    ----------
    read_1m
        Zero-arg callable returning the window's stored 1-minute bars as a sorted
        polars frame (empty frame ⇒ no 1-minute in the lake ⇒ no-op).
    write_1day
        ``(bars) -> bars_written`` callable persisting the resampled daily bars to
        the ``1day`` partition (the sink snaps prices + merges idempotently).
    before
        The forming-session cutoff (today IST), forwarded to
        :func:`resample_1m_to_daily`.

    Returns
    -------
    DailyFillResult
        The written count + the resampled span (``first_ts`` / ``last_ts``).
    """
    bars = resample_1m_to_daily(read_1m(), before=before)
    if not bars:
        return DailyFillResult(bars_written=0, first_ts=None, last_ts=None)
    written = write_1day(bars)
    return DailyFillResult(
        bars_written=written, first_ts=bars[0].timestamp, last_ts=bars[-1].timestamp
    )


def plan_daily_resample_windows(
    records: Iterable[WindowLedgerRecord],
    *,
    daily_interval: str = TRACK1_DAILY_INTERVAL,
) -> list[DailyResampleWindow]:
    """Select the daily windows a resample MIGHT fill — pure, deterministic.

    A candidate is a ``1day`` window the ledger settled EMPTY or SKIPPED **via the
    empty path** (``empty_streak > 0``): a below-threshold EMPTY (re-fetched next
    pull anyway) or an EMPTY-terminal / OLD-gap fast-settle SKIPPED. A SUCCESS
    window (has data) is excluded, and a SKIPPED window with ``empty_streak == 0``
    (a definitive vendor known-gap — Groww has none, but defended for safety) is
    excluded too: resample only targets windows that came back genuinely empty.

    The window's ``[start, end)`` ISO dates are parsed back to :class:`datetime.date`
    (tolerant of a date-only ``YYYY-MM-DD`` or a full-ISO datetime string) so the
    rebuilt :class:`~brutex.data.fetch_coverage.WindowKey` round-trips to the exact
    ledger row identity.
    """
    out: list[DailyResampleWindow] = []
    for record in records:
        if record.interval != daily_interval:
            continue
        if record.status not in (EMPTY, SKIPPED):
            continue
        if record.empty_streak <= 0:
            continue
        out.append(
            DailyResampleWindow(
                market=record.market,
                security_id=record.security_id,
                interval=record.interval,
                start=_parse_window_date(record.window_start),
                end=_parse_window_date(record.window_end),
            )
        )
    return out


def reconcile_daily_from_minute(
    *,
    records: Iterable[WindowLedgerRecord],
    ledger: WindowLedger,
    build_read_1m: Callable[[DailyResampleWindow], Callable[[], pl.DataFrame]],
    build_write_1day: Callable[[DailyResampleWindow], Callable[[Sequence[CandleBar]], int]],
    before: date,
    now: datetime,
    daily_interval: str = TRACK1_DAILY_INTERVAL,
) -> DailyReconcileReport:
    """Fill every empty daily window the lake's 1-minute can supply — pure orchestration.

    For each candidate window (:func:`plan_daily_resample_windows`): bind its
    1-minute reader + 1-day writer (the injected builders), resample-and-write
    (:func:`fill_daily_from_minute`), and on a non-empty fill re-record the SAME
    ledger row ``SUCCESS`` with ``raw_bars = bars_written`` and ``in_pull_fetches=0``
    (a resample makes ZERO vendor calls — the honest provenance signal). The
    ``SUCCESS`` record resets ``empty_streak`` to 0, so the now-has-data window is
    never re-promoted to SKIPPED. Each window is quarantined: a per-window error
    (corrupt slice / write failure) increments ``errored`` and continues — one bad
    window never blanks the pass (mirrors the ingest spine's per-window quarantine).
    All lake I/O is injected, so the whole flow is 100% unit-testable.
    """
    filled = no_minute = errored = bars = 0
    for window in plan_daily_resample_windows(records, daily_interval=daily_interval):
        try:
            result = fill_daily_from_minute(
                read_1m=build_read_1m(window),
                write_1day=build_write_1day(window),
                before=before,
            )
        except Exception as exc:
            errored += 1
            _LOG.warning(
                "spot_daily_resample.window_errored",
                symbol=window.security_id,
                window_start=window.start.isoformat(),
                window_end=window.end.isoformat(),
                error=str(exc),
            )
            continue
        if result.bars_written > 0:
            ledger.record(
                WindowKey(
                    market=window.market,
                    security_id=window.security_id,
                    interval=window.interval,
                    start=window.start,
                    end=window.end,
                ),
                raw_bars=result.bars_written,
                error_type=None,
                bars_appended=result.bars_written,
                now=now,
                in_pull_fetches=0,  # a resample made ZERO vendor fetches (honest provenance)
            )
            filled += 1
            bars += result.bars_written
            _LOG.info(
                "spot_daily_resample.window_filled",
                symbol=window.security_id,
                window_start=window.start.isoformat(),
                window_end=window.end.isoformat(),
                bars=result.bars_written,
                first_ts=result.first_ts.isoformat() if result.first_ts is not None else None,
                last_ts=result.last_ts.isoformat() if result.last_ts is not None else None,
            )
        else:
            no_minute += 1
            _LOG.info(
                "spot_daily_resample.window_no_minute",
                symbol=window.security_id,
                window_start=window.start.isoformat(),
                window_end=window.end.isoformat(),
            )
    return DailyReconcileReport(filled=filled, no_minute=no_minute, errored=errored, bars=bars)


def _parse_window_date(value: str) -> date:
    """Parse a ledger window bound (``YYYY-MM-DD`` or full ISO) to a :class:`date`.

    The ledger stores daily window bounds as date-only ISO (``WindowKey.start`` is a
    :class:`date`), but parse the full-ISO form too so a future datetime-keyed
    window never trips the reconcile. ``datetime.fromisoformat`` accepts both on
    Python 3.12.
    """
    return datetime.fromisoformat(value).date()


def make_lake_read_1m_window(  # pragma: no cover — raw lake read, integration-tested
    *,
    lake_root: Path,
    exchange: str,
    segment: str,
    symbol: str,
    start: date,
    end: date,
) -> Callable[[], pl.DataFrame]:
    """Build the injected 1-minute reader for one ``[start, end)`` daily window.

    Returns a zero-arg callable reading the symbol's stored ``1minute`` Parquet,
    filtered to the bars whose IST trading date falls in the half-open window
    ``[start, end)``, into a sorted polars frame with exactly the resampler's columns
    (``ts_utc, open, high, low, close, volume``). UNLIKE
    :func:`~brutex.data.timeframe_materialize.make_lake_read_1m` (which RAISES when no
    1-minute exists), a missing partition tree returns an EMPTY frame so "no 1-minute
    to fill from" is a clean no-op, never an error (the verify-first default). Raw
    lake I/O — ``# pragma: no cover``; exercised by the round-trip integration test.
    """
    import polars as pl

    def _read() -> pl.DataFrame:
        base = lake_root / "bars" / exchange / segment / symbol / "1minute"
        if not (base.is_dir() and any(base.rglob("*.parquet"))):
            return pl.DataFrame(
                schema={
                    "ts_utc": pl.Datetime(time_unit="us", time_zone="UTC"),
                    "open": pl.Float64,
                    "high": pl.Float64,
                    "low": pl.Float64,
                    "close": pl.Float64,
                    "volume": pl.Int64,
                }
            )
        df = pl.read_parquet(base.as_posix() + "/**/*.parquet")
        # IST trading date of each 1-minute bar = (ts_utc + 05:30) date. Intraday
        # bars (09:15–15:30 IST) never cross the UTC midnight, so this is exact.
        ist_date = (pl.col("ts_utc") + pl.duration(hours=5, minutes=30)).dt.date()
        return (
            df.rename({"timestamp": "ts_utc"})
            .with_columns(ist_date.alias("_ist_date"))
            .filter((pl.col("_ist_date") >= start) & (pl.col("_ist_date") < end))
            .select("ts_utc", "open", "high", "low", "close", "volume")
            .sort("ts_utc")
        )

    return _read


def make_lake_write_1day(  # pragma: no cover — raw lake write, integration-tested
    *,
    lake_root: Path,
    exchange: str,
    segment: str,
    symbol: str,
) -> Callable[[Sequence[CandleBar]], int]:
    """Build the injected ``1day`` writer for :func:`fill_daily_from_minute`.

    Returns a ``(bars) -> bars_written`` callable appending the resampled daily bars
    to the lake's ``1day`` partition via a
    :class:`~brutex.data.sink.ParquetBarSink` (the SAME sink the vendor spot spine
    writes through — CLAUDE.md §11 COMMON), so a resampled daily bar shares the
    vendor stamp/schema/OI (see the module docstring on value-vs-stamp identity). The
    sink snaps prices to the 2-dp tick grid + merges keep-first by ``(year, month)``
    (DEC-SPOT-PRICE-2DP-001 / Golden Rule #7), so a re-run leaves the partition
    byte-identical (the keep-first merge adds zero NEW rows) and a later real vendor
    fetch dedups. The returned ``bars_appended`` is the INPUT bar count (the sink's
    contract), NOT the net-new merged count — functional idempotence rests on the
    SUCCESS status gate (a filled window is excluded from the next plan), not on this
    return value. Raw lake I/O — ``# pragma: no cover``; integration-tested.
    """
    from brutex.data.parquet_writer import ParquetWriter
    from brutex.data.sink import ParquetBarSink

    sink = ParquetBarSink(writer=ParquetWriter(lake_root=lake_root))

    def _write(bars: Sequence[CandleBar]) -> int:
        result = sink.append(
            exchange=exchange,
            segment=segment,
            symbol=symbol,
            interval=TRACK1_DAILY_INTERVAL,
            bars=list(bars),
        )
        return result.bars_appended

    return _write
