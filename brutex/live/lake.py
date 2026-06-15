"""Persist captured live ticks + built 1-minute candles to the Parquet lake.

A SEPARATE ``live/`` tree (never the brute-force ``bars/`` lake) so the live
measurement surface can never contaminate the engine's historical candles
(Golden Rule #3 scope guard):

* ``<lake_root>/live/ticks/dt=<session_date>.parquet`` — one row per
  (feed-callback, instrument): the raw capture, queryable.
* ``<lake_root>/live/candles_1m/dt=<session_date>.parquet`` — the 1-minute
  candles, **schema-identical to the backtest bars** (timestamp µs UTC, OHLC
  Float64, volume/OI Int64) + provenance, so live-vs-backtest parity is a join.

Writes are **merge-on-write + idempotent**: re-persisting the same capture
unions on the primary key and keeps a stable, sorted file (Golden Rule #7).
Prices are snapped to the 2-decimal tick grid (``round_money``, ROUND_HALF_UP)
exactly like the spot lake (DEC-SPOT-PRICE-2DP-001).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import polars as pl

from brutex.calendars.nse import IST
from brutex.data.models import CandleBar
from brutex.live.candle_builder import build_minute_candles
from brutex.live.store import TickRecord
from brutex.live.tick import LiveTick, extract_quotes, flatten_index_snapshot
from brutex.money import round_money

#: Top-level lake subtree for the live measurement surface.
LIVE_DIR = "live"
_TICKS_DIR = "ticks"
_CANDLES_DIR = "candles_1m"
#: A fully-qualified flattened key is ``exchange:segment:symbol`` (3 parts).
_MIN_KEY_PARTS = 3

#: Polars schema for the ``live_ticks`` table (browsable in /db).
TICK_SCHEMA: dict[str, pl.DataType] = {
    "capture_run_id": pl.String(),
    "seq": pl.Int64(),
    "recv_ns": pl.Int64(),  # OURS — nanoseconds (time.time_ns); the cadence oracle
    "recv_ts": pl.Datetime("us", "UTC"),  # OURS — µs convenience mirror of recv_ns
    "vendor_ts_ms": pl.Float64(),  # THEIRS — tsInMillis (milliseconds), UNTAMPERED
    "session_date": pl.Date(),
    "exchange": pl.String(),
    "segment": pl.String(),
    "instrument_key": pl.String(),
    "groww_symbol": pl.String(),
    "value": pl.Float64(),  # raw observation — FULL precision (only candles snap to 2dp)
    "volume": pl.Int64(),
    "open_interest": pl.Int64(),
    "feed_kind": pl.String(),
    "raw_json": pl.String(),
}
_TICK_PK = ("capture_run_id", "seq", "instrument_key")

#: Polars schema for the ``live_candles_1m`` table — mirrors the backtest bar
#: schema (timestamp/open/high/low/close/volume/open_interest) + provenance.
CANDLE_SCHEMA: dict[str, pl.DataType] = {
    "instrument_key": pl.String(),
    "groww_symbol": pl.String(),
    "exchange": pl.String(),
    "segment": pl.String(),
    "timestamp": pl.Datetime("us", "UTC"),
    "session_date": pl.Date(),
    "open": pl.Float64(),
    "high": pl.Float64(),
    "low": pl.Float64(),
    "close": pl.Float64(),
    "volume": pl.Int64(),
    "open_interest": pl.Int64(),
    "tick_count": pl.Int64(),
    "source": pl.String(),
    "built_run_id": pl.String(),
}
_CANDLE_PK = ("instrument_key", "timestamp")


def split_instrument_key(instrument_key: str) -> tuple[str, str, str]:
    """Split a flattened key into ``(exchange, segment, symbol)``.

    ``"NSE:IDX:NIFTY"`` → ``("NSE", "IDX", "NIFTY")``. A key that does not carry
    the two leading segments degrades to ``("", "", <whole key>)`` rather than
    guessing — the raw key is always preserved in ``instrument_key``.
    """
    parts = instrument_key.split(":")
    if len(parts) >= _MIN_KEY_PARTS:
        return parts[0], parts[1], ":".join(parts[2:])
    return "", "", instrument_key


def _groww_symbol(exchange: str, symbol: str) -> str:
    """Best-effort ``EXCH-SYMBOL`` rendering (empty when the exchange is unknown)."""
    return f"{exchange}-{symbol}" if exchange else symbol


def _session_date(recv_ts: datetime) -> object:
    """IST trading date of a UTC instant (the partition key)."""
    return recv_ts.astimezone(IST).date()


def _minute_label(recv_ts: datetime) -> datetime:
    """UTC minute-start label for a tick (matches ``build_minute_candles``)."""
    return recv_ts.replace(second=0, microsecond=0)


def ticks_to_frame(
    records: Sequence[TickRecord], *, capture_run_id: str, feed_kind: str
) -> pl.DataFrame:
    """Fan captured snapshot *records* out into a ``live_ticks`` DataFrame.

    One row per (record, instrument) — the record's flattened snapshot. The raw
    snapshot is JSON-preserved per row for full-fidelity audit.
    """
    rows: list[dict[str, object]] = []
    for record in records:
        raw = json.dumps(dict(record.snapshot), sort_keys=True, separators=(",", ":"), default=str)
        for instrument_key, quote in extract_quotes(record.snapshot).items():
            exchange, segment, symbol = split_instrument_key(instrument_key)
            rows.append(
                {
                    "capture_run_id": capture_run_id,
                    "seq": record.seq,
                    "recv_ns": record.recv_ns,
                    "recv_ts": record.recv_ts,
                    "vendor_ts_ms": quote.vendor_ts_ms,
                    "session_date": _session_date(record.recv_ts),
                    "exchange": exchange,
                    "segment": segment,
                    "instrument_key": instrument_key,
                    "groww_symbol": _groww_symbol(exchange, symbol),
                    "value": float(quote.value),  # raw — NOT rounded (only candles snap)
                    "volume": quote.volume,
                    "open_interest": quote.open_interest,
                    "feed_kind": feed_kind,
                    "raw_json": raw,
                }
            )
    return pl.DataFrame(rows, schema=TICK_SCHEMA, orient="row")


def candles_to_frame(
    candles: Mapping[str, Sequence[CandleBar]],
    *,
    built_run_id: str,
    tick_counts: Mapping[tuple[str, datetime], int] | None = None,
) -> pl.DataFrame:
    """Build a ``live_candles_1m`` DataFrame from per-instrument candles.

    *tick_counts* (from :func:`minute_tick_counts`) stamps how many ticks formed
    each minute; absent counts default to ``0``.
    """
    counts = tick_counts or {}
    rows: list[dict[str, object]] = []
    for instrument_key, bars in candles.items():
        exchange, segment, symbol = split_instrument_key(instrument_key)
        for bar in bars:
            rows.append(
                {
                    "instrument_key": instrument_key,
                    "groww_symbol": _groww_symbol(exchange, symbol),
                    "exchange": exchange,
                    "segment": segment,
                    "timestamp": bar.timestamp,
                    "session_date": _session_date(bar.timestamp),
                    "open": float(round_money(bar.open)),
                    "high": float(round_money(bar.high)),
                    "low": float(round_money(bar.low)),
                    "close": float(round_money(bar.close)),
                    "volume": bar.volume,
                    "open_interest": bar.open_interest,
                    "tick_count": counts.get((instrument_key, bar.timestamp), 0),
                    "source": "live",
                    "built_run_id": built_run_id,
                }
            )
    return pl.DataFrame(rows, schema=CANDLE_SCHEMA, orient="row")


def minute_tick_counts(records: Sequence[TickRecord]) -> dict[tuple[str, datetime], int]:
    """Count ticks per ``(instrument_key, minute-start UTC)`` for the candle stamp."""
    counts: dict[tuple[str, datetime], int] = {}
    for record in records:
        minute = _minute_label(record.recv_ts)
        for instrument_key in flatten_index_snapshot(record.snapshot):
            key = (instrument_key, minute)
            counts[key] = counts.get(key, 0) + 1
    return counts


def _merge_write(
    frame: pl.DataFrame, dest: Path, *, pk: Sequence[str], sort: Sequence[str]
) -> None:
    """Union *frame* with any existing partition file, dedup on *pk*, write sorted.

    ``keep="first"`` is CORRECT for ``live_ticks`` (each ``(capture_run_id, seq,
    instrument_key)`` tick is a unique immutable observation — a re-fold of the same
    seq is a byte-identical duplicate, so keeping either copy is the same). It is
    WRONG for candles, which are RE-AGGREGATED across folds — those use
    :func:`_merge_candles` instead (DEC-LIVE-CANDLE-PRECISION-001).
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        frame = pl.concat([pl.read_parquet(dest), frame], how="vertical_relaxed")
    frame = frame.unique(subset=list(pk), keep="first").sort(list(sort))
    frame.write_parquet(dest)


def _live_ticks_for_session(lake_root: str | Path, session_date: object) -> pl.DataFrame:
    """Read the DEDUPED ``live_ticks`` partition for *session_date* (empty if none).

    This is the single deduped source of truth a candle is rebuilt from
    (DEC-LIVE-CANDLE-PRECISION-001): the ``live_ticks`` PK already collapsed every
    re-folded segment to one row per ``(capture_run_id, seq, instrument_key)``, so the
    minute's tick set read back here is identical no matter how many ~5s slices folded
    it — the property that makes the candle rebuild idempotent under a double-fold.
    """
    dest = _partition_path(lake_root, _TICKS_DIR, session_date)
    if not dest.exists():
        return pl.DataFrame(schema=dict(TICK_SCHEMA))
    return pl.read_parquet(dest)


def _ticks_from_lake_frame(frame: pl.DataFrame) -> list[LiveTick]:
    """Reconstruct :class:`LiveTick` rows from a deduped ``live_ticks`` lake frame.

    Only the three fields the candle fold needs are read back — ``recv_ts`` (the
    minute-bucketing / open-close ordering clock), ``instrument_key``, and the raw
    ``value`` (FULL precision; the 2dp snap happens later at the candle-write
    boundary). A ``null`` value (defensive — the lake column is nullable) is skipped.
    """
    ticks: list[LiveTick] = []
    for row in frame.select("recv_ts", "instrument_key", "value").iter_rows(named=True):
        value = row["value"]
        if value is None:  # pragma: no cover - defensive: writer never emits a null value
            continue
        ticks.append(
            LiveTick(
                recv_ts=row["recv_ts"],
                instrument_key=row["instrument_key"],
                value=Decimal(str(value)),
            )
        )
    return ticks


def _rebuild_candles_from_ticks(
    lake_root: str | Path,
    session_date: object,
    *,
    affected: set[tuple[str, datetime]],
    built_run_id: str,
) -> pl.DataFrame:
    """Rebuild the *affected* ``(instrument_key, minute)`` candles from the tick lake.

    Reads the deduped ``live_ticks`` partition for *session_date*, folds it through
    the SAME :func:`~brutex.live.candle_builder.build_minute_candles` the batch path
    uses, keeps only the *affected* minutes (the ones this fold touched), and stamps
    each with its TRUE full-minute ``tick_count`` (the deduped per-minute count). This
    is the authoritative + idempotent candle: a double-fold of the same segment reads
    back the SAME deduped tick set, so the rebuilt bar is byte-identical.
    """
    tick_frame = _live_ticks_for_session(lake_root, session_date)
    if tick_frame.is_empty():
        return pl.DataFrame(schema=dict(CANDLE_SCHEMA))
    ticks = _ticks_from_lake_frame(tick_frame)
    rebuilt = build_minute_candles(ticks)
    counts = _minute_counts_from_ticks(ticks)
    kept: dict[str, list[CandleBar]] = {}
    for instrument_key, bars in rebuilt.items():
        in_scope = [bar for bar in bars if (instrument_key, bar.timestamp) in affected]
        if in_scope:
            kept[instrument_key] = in_scope
    return candles_to_frame(kept, built_run_id=built_run_id, tick_counts=counts)


def _minute_counts_from_ticks(ticks: Sequence[LiveTick]) -> dict[tuple[str, datetime], int]:
    """Count deduped ticks per ``(instrument_key, minute-start UTC)`` for the rebuild.

    Mirrors :func:`minute_tick_counts` but over reconstructed :class:`LiveTick` rows
    (the deduped lake) rather than raw :class:`TickRecord` snapshots, so the stamped
    ``tick_count`` is the TRUE full-minute count — never one ~5s slice's partial count.
    """
    counts: dict[tuple[str, datetime], int] = {}
    for tick in ticks:
        key = (tick.instrument_key, _minute_label(tick.recv_ts))
        counts[key] = counts.get(key, 0) + 1
    return counts


def _merge_candles(frame: pl.DataFrame, dest: Path, *, lake_root: str | Path) -> None:
    """Re-aggregate candles on ``(instrument_key, timestamp)`` across folds, write sorted.

    The real-time fold (#750) writes a candle for EACH ~5s slice of a minute, then
    re-folds the same minute as more ticks arrive. The plain ``_merge_write`` with
    ``keep="first"`` DROPPED those later slices, leaving a truncated 1-minute bar
    (``tick_count=1``, partial OHLC) — the confirmed bug. Here each affected minute is
    instead REBUILT from the deduped ``live_ticks`` lake (the authoritative,
    fold-count-independent source), which is automatically correct AND idempotent: a
    double-fold of the same segment reads the SAME deduped ticks and rebuilds the SAME
    bar (no double tick_count). Minutes with NO ticks in the lake (e.g. the pure-candle
    batch tests that never wrote ticks) fall back to the candle as passed in *frame*.
    """
    session_date = frame["session_date"].to_list()[0]
    built_run_id = frame["built_run_id"].to_list()[-1]
    affected = {
        (row["instrument_key"], row["timestamp"])
        for row in frame.select("instrument_key", "timestamp").iter_rows(named=True)
    }
    rebuilt = _rebuild_candles_from_ticks(
        lake_root, session_date, affected=affected, built_run_id=built_run_id
    )
    # Minutes the tick lake could rebuild are authoritative; any affected minute it
    # could NOT (no ticks persisted) keeps the passed-in candle (batch / test path).
    rebuilt_keys = {
        (row["instrument_key"], row["timestamp"])
        for row in rebuilt.select("instrument_key", "timestamp").iter_rows(named=True)
    }
    fallback = frame.filter(
        ~pl.struct("instrument_key", "timestamp").is_in(
            [{"instrument_key": ik, "timestamp": ts} for ik, ts in rebuilt_keys]
        )
    )
    fresh = pl.concat([rebuilt, fallback], how="vertical_relaxed")

    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        existing = pl.read_parquet(dest)
        # Drop the existing rows for every affected minute — `fresh` supersedes them
        # (keep=last semantics, but recomputed not merged, so a re-fold is a no-op).
        existing = existing.filter(
            ~pl.struct("instrument_key", "timestamp").is_in(
                [{"instrument_key": ik, "timestamp": ts} for ik, ts in affected]
            )
        )
        fresh = pl.concat([existing, fresh], how="vertical_relaxed")
    # Every input frame (the tick rebuild, the passed-in fallback, the existing
    # partition) is already in CANDLE_SCHEMA dtypes, so `vertical_relaxed` never upcast
    # — `.select` only restores the canonical column order before the write.
    fresh = (
        fresh.unique(subset=list(_CANDLE_PK), keep="last")
        .sort(["instrument_key", "timestamp"])
        .select(list(CANDLE_SCHEMA))
    )
    fresh.write_parquet(dest)


def _partition_path(lake_root: str | Path, table_dir: str, session_date: object) -> Path:
    """``<lake_root>/live/<table>/dt=<session_date>.parquet``."""
    return Path(lake_root) / LIVE_DIR / table_dir / f"dt={session_date}.parquet"


def write_live_ticks(
    records: Sequence[TickRecord],
    *,
    lake_root: str | Path,
    capture_run_id: str,
    feed_kind: str = "index",
) -> list[Path]:
    """Persist captured tick *records* to ``live/ticks`` (one file per session date)."""
    frame = ticks_to_frame(records, capture_run_id=capture_run_id, feed_kind=feed_kind)
    written: list[Path] = []
    for session_date in frame["session_date"].unique().to_list():
        dest = _partition_path(lake_root, _TICKS_DIR, session_date)
        _merge_write(
            frame.filter(pl.col("session_date") == session_date),
            dest,
            pk=_TICK_PK,
            sort=("seq", "instrument_key"),
        )
        written.append(dest)
    return written


def write_live_candles(
    candles: Mapping[str, Sequence[CandleBar]],
    *,
    lake_root: str | Path,
    built_run_id: str,
    tick_counts: Mapping[tuple[str, datetime], int] | None = None,
) -> list[Path]:
    """Persist 1-minute *candles* to ``live/candles_1m`` (one file per session date).

    Candles are RE-AGGREGATED on ``(instrument_key, timestamp)`` across folds via
    :func:`_merge_candles` (NOT the tick path's ``keep="first"`` dedup): the real-time
    fold writes a candle per ~5s slice and re-folds the same minute as more ticks
    arrive, so each affected minute is rebuilt from the deduped ``live_ticks`` lake to
    the FULL minute — idempotent under a double-fold (DEC-LIVE-CANDLE-PRECISION-001).
    """
    frame = candles_to_frame(candles, built_run_id=built_run_id, tick_counts=tick_counts)
    written: list[Path] = []
    for session_date in frame["session_date"].unique().to_list():
        dest = _partition_path(lake_root, _CANDLES_DIR, session_date)
        _merge_candles(
            frame.filter(pl.col("session_date") == session_date),
            dest,
            lake_root=lake_root,
        )
        written.append(dest)
    return written


def read_live_ticks(lake_root: str | Path) -> pl.DataFrame:
    """Read every persisted ``live/ticks`` partition (empty frame if none)."""
    return _read_all(
        Path(lake_root) / LIVE_DIR / _TICKS_DIR, TICK_SCHEMA, ("seq", "instrument_key")
    )


def read_live_candles(lake_root: str | Path) -> pl.DataFrame:
    """Read every persisted ``live/candles_1m`` partition (empty frame if none)."""
    return _read_all(
        Path(lake_root) / LIVE_DIR / _CANDLES_DIR, CANDLE_SCHEMA, ("instrument_key", "timestamp")
    )


def _read_all(
    directory: Path, schema: Mapping[str, pl.DataType], sort: Iterable[str]
) -> pl.DataFrame:
    """Union every ``dt=*.parquet`` under *directory*, sorted (empty if none)."""
    files = sorted(directory.glob("dt=*.parquet")) if directory.exists() else []
    if not files:
        return pl.DataFrame(schema=dict(schema))
    return pl.concat([pl.read_parquet(f) for f in files], how="vertical_relaxed").sort(list(sort))


__all__ = [
    "CANDLE_SCHEMA",
    "LIVE_DIR",
    "TICK_SCHEMA",
    "candles_to_frame",
    "minute_tick_counts",
    "read_live_candles",
    "read_live_ticks",
    "split_instrument_key",
    "ticks_to_frame",
    "write_live_candles",
    "write_live_ticks",
]
