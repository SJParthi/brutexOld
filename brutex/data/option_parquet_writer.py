"""Expired-option Parquet table writer — the concrete `OptionPartitionWriter`.

The physical lake layer the :class:`~brutex.data.option_sink.OptionBarSink`
delegates to: persists a deduped batch of
:class:`~brutex.data.models.OptionBar` rows to a Parquet partition under
``<lake_root>/options/<series>/<YYYY>-<MM>.parquet``. One series (one
``partition_key``) per call; bars spanning month boundaries are split into
their ``(year, month)`` partitions (:func:`~brutex.data.partition.partition_for_timestamp`).

**Idempotent merge-on-write (O(1) per bar amortised).** A 30-day option pull
straddles months and may be re-pulled, so each write **unions** the incoming
bars into the existing partition keyed by ``(side, timestamp)`` — a never-seen
key is added, an identical one is a silent no-op. The hash-keyed
``polars.unique`` makes the merge O(1) amortised per row (the upstream
:class:`~brutex.data.option_dedup.OptionDedupIndex` already guarantees O(1)
content dedup; this is the durable belt-and-braces across index resets).

``write`` returns the count of **newly-written** rows (what the sink reports
as "written"); a pure re-pull returns 0 and rewrites nothing.

Honest envelope (Golden Rule #10): per-bar work is O(1) amortised, but a flush
reads the whole month partition (O(month rows)) to union — inherent to a
month-partition lake (identical to the spot
:class:`~brutex.data.parquet_writer.ParquetWriter`). Vendor-drift detection
(same ``(side, ts)`` with different OHLCV) is deferred — existing rows win on
merge; the dedup index prevents in-run drift, cross-run drift is a tracked
follow-up.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from brutex.data.models import OptionBar
from brutex.data.partition import partition_for_timestamp
from brutex.models import BruteXError
from brutex.money import round_money

if TYPE_CHECKING:
    import polars as pl

#: Subdirectory under the lake root holding the expired-options table.
_OPTIONS_SUBDIR: str = "options"

#: Characters in a partition key replaced to make a filesystem-safe segment.
_UNSAFE: str = "|"
_SAFE: str = "__"


class OptionWriterError(BruteXError):
    """An expired-option Parquet write failed or got malformed input."""


def _safe_segment(partition_key: str) -> str:
    """Map a ``|``-joined series key to one filesystem-safe path segment.

    Rejects path-traversal / separator characters so a vendor-supplied series
    coordinate can never escape the options table directory.
    """
    if not partition_key:
        raise OptionWriterError("partition_key must be non-empty")
    if "/" in partition_key or "\\" in partition_key or ".." in partition_key:
        raise OptionWriterError(f"unsafe partition_key {partition_key!r}")
    return partition_key.replace(_UNSAFE, _SAFE)


class OptionParquetWriter:
    """Writes :class:`OptionBar` rows to the partitioned expired-options table.

    Parameters
    ----------
    lake_root
        Top-level data-lake directory; the options table lives under
        ``<lake_root>/options/``. Created on first write.
    """

    def __init__(self, *, lake_root: Path) -> None:
        self._lake_root = lake_root

    @property
    def lake_root(self) -> Path:
        """Return the configured top-level lake directory."""
        return self._lake_root

    def write(self, *, partition_key: str, bars: Sequence[OptionBar]) -> int:
        """Persist *bars* for one series; return the count of newly-written rows.

        Bars are split into ``(year, month)`` partitions and each group is
        merged into its partition file keyed by ``(side, timestamp)``. A pure
        re-pull (every bar already stored) writes nothing and returns 0.
        """
        if not bars:
            return 0
        segment = _safe_segment(partition_key)
        groups: dict[tuple[int, int], list[OptionBar]] = {}
        for bar in bars:
            groups.setdefault(partition_for_timestamp(bar.timestamp), []).append(bar)

        added_total = 0
        for (year, month), group in sorted(groups.items()):
            dest = self._lake_root / _OPTIONS_SUBDIR / segment / f"{year:04d}-{month:02d}.parquet"
            added_total += self._merge_partition(dest, group)
        return added_total

    def _merge_partition(self, dest: Path, new_bars: list[OptionBar]) -> int:
        """Union *new_bars* into *dest* keyed by ``(side, timestamp)``; return new rows."""
        new_df = _render_df(new_bars)  # validates the polars import
        import polars as pl

        if dest.exists():
            existing = pl.read_parquet(dest)
            # int() coercion keeps the return concretely int even in the CI
            # type-check env where polars is NOT installed (`[dev]`-only env, without the `[data]` extra)
            # — there `.height` is Any (ignore_missing_imports) and an un-coerced
            # return would trip mypy --strict's no-any-return.
            before = int(existing.height)
            combined = (
                pl.concat([existing, new_df])
                .unique(subset=["side", "timestamp"], keep="first", maintain_order=True)
                .sort(["side", "timestamp"])
            )
        else:
            before = 0
            combined = new_df.unique(
                subset=["side", "timestamp"], keep="first", maintain_order=True
            ).sort(["side", "timestamp"])
        added = int(combined.height) - before
        if added > 0:
            dest.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_parquet(dest, combined)
        return added


def _money(value: Decimal) -> float:
    """Round a money :class:`~decimal.Decimal` to 2dp (DEC-MONEY-2DP-001) → float."""
    return float(round_money(value))


def _opt_money(value: Decimal | None) -> float | None:
    """2dp-round an optional money field; ``None`` stays ``None``."""
    return None if value is None else _money(value)


def _render_df(bars: Sequence[OptionBar]) -> pl.DataFrame:
    """Build a typed polars DataFrame from *bars*.

    Money fields (``open``/``high``/``low``/``close``/``strike``/``spot``) are
    rounded to **2 decimals** (DEC-MONEY-2DP-001, CLAUDE.md §4). ``iv`` is a
    volatility metric, **NOT money** — it keeps full precision (rounding it
    would corrupt downstream analytics, per the same rule).
    """
    try:
        import polars as pl
    except ImportError as exc:  # pragma: no cover — exercised only without the data extra
        raise OptionWriterError(
            "OptionParquetWriter requires 'polars' — install '.[data]' for the lake"
        ) from exc

    rows = {
        "timestamp": [b.timestamp for b in bars],
        "open": [_money(b.open) for b in bars],
        "high": [_money(b.high) for b in bars],
        "low": [_money(b.low) for b in bars],
        "close": [_money(b.close) for b in bars],
        "volume": [b.volume for b in bars],
        "open_interest": [b.open_interest for b in bars],
        "side": [b.side for b in bars],
        "strike": [_opt_money(b.strike) for b in bars],
        "spot": [_opt_money(b.spot) for b in bars],
        "iv": [None if b.iv is None else float(b.iv) for b in bars],  # NOT money — full precision
    }
    # polars infers timestamp→Datetime(us, UTC), prices→Float64, side→Utf8.
    # An all-None batch infers a column as Null, so pin the nullable fields to
    # their stable lake dtypes (cast is a no-op when already typed).
    return pl.DataFrame(rows).with_columns(
        pl.col("open_interest").cast(pl.Int64),
        pl.col("strike").cast(pl.Float64),
        pl.col("spot").cast(pl.Float64),
        pl.col("iv").cast(pl.Float64),
    )


def _atomic_write_parquet(dest: Path, df: pl.DataFrame) -> None:
    """Write *df* to *dest* atomically (tmp file in the same dir + ``os.replace``)."""
    fd, tmp_name = tempfile.mkstemp(dir=dest.parent, suffix=".parquet.tmp")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        df.write_parquet(tmp)
        os.replace(tmp, dest)
    finally:
        if tmp.exists():
            tmp.unlink()


__all__ = [
    "OptionParquetWriter",
    "OptionWriterError",
]
