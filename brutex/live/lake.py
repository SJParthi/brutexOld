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
from pathlib import Path

import polars as pl

from brutex.calendars.nse import IST
from brutex.data.models import CandleBar
from brutex.live.store import TickRecord
from brutex.live.tick import extract_quotes, flatten_index_snapshot
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
    """Union *frame* with any existing partition file, dedup on *pk*, write sorted."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        frame = pl.concat([pl.read_parquet(dest), frame], how="vertical_relaxed")
    frame = frame.unique(subset=list(pk), keep="first").sort(list(sort))
    frame.write_parquet(dest)


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
    """Persist 1-minute *candles* to ``live/candles_1m`` (one file per session date)."""
    frame = candles_to_frame(candles, built_run_id=built_run_id, tick_counts=tick_counts)
    written: list[Path] = []
    for session_date in frame["session_date"].unique().to_list():
        dest = _partition_path(lake_root, _CANDLES_DIR, session_date)
        _merge_write(
            frame.filter(pl.col("session_date") == session_date),
            dest,
            pk=_CANDLE_PK,
            sort=("instrument_key", "timestamp"),
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
