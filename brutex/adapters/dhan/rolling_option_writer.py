"""Rolling-ATM option Parquet writer — the honest rolling-ATM lake shape.

DEC-DHAN-ROLLING-OPTIONS-001 (ADR-0022). The physical lake layer for the Dhan
**rolling-ATM** option series. THE HONESTY LAW (binding): Dhan serves NO
per-expired-contract candles — only an ATM-relative ROLLING series — so the resolved
expiry DATE and the absolute STRIKE **change per bar-date** under a fixed selector.
They are therefore per-ROW columns (``resolved_expiry_date`` / ``resolved_strike``),
NEVER partition keys. Every partition segment is a REQUESTED selector::

    <lake_root>/rolling_options/{exchange}/{underlying}/{expiry_flag}/EC{expiry_code}/
        {strike_offset_safe}/{option_type}/{interval}/{yyyy}/{mm}.parquet

e.g. ``rolling_options/NSE/NIFTY/WEEK/EC0/ATMp1/CALL/1minute/2024/06.parquet``.

* Root named ``rolling_options`` (NEVER ``options`` / ``fno_candles`` / ``bars``) so
  ``parse_fno_symbol`` and every fixed-contract reader/heal path are STRUCTURALLY
  unable to touch it — a rolling label can never be mis-decoded as a real contract.
* ``strike_offset_safe`` maps the honest label to a path-hazard-safe segment
  (``ATM`` → ``ATM``, ``ATM+1`` → ``ATMp1``, ``ATM-1`` → ``ATMm1``); the honest
  ``strike_offset`` label is kept verbatim in a column.
* **Contrast with Groww:** Groww bakes a FIXED ``(expiry_date, strike)`` into each
  ``groww_symbol`` partition path (a genuine fixed-contract series). Baking either into
  the Dhan path would FABRICATE a series Dhan never served, scatter one rolling series
  across phantom dirs, and let ``parse_fno_symbol`` mis-decode a rolling label. Keeping
  them per-ROW is the honest encoding — one fixed ``expiry_code`` partition legitimately
  spans MANY resolved expiries across a roll (a structural pin proves it).

**O(1) dedup key = ``timestamp``** (the path fixes all six selectors → ordinality is
time-only): hash-keyed ``polars.unique(subset=["timestamp"], keep="first")``,
merge-on-write, per-``Path`` lock (the Groww ``(side, timestamp)`` idiom with every
selector absorbed into the path). Money fields are 2dp ``round_money`` HALF-UP snapped
at the write boundary; ``iv`` is a volatility metric (NOT money) kept full-precision.

The coordinate echo REUSES the on-main
:class:`~brutex.data.option_parquet_writer.OptionSeriesCoordinate`
(DEC-EXPIRED-OPTIONS-TABLE-001, which already carries exactly these rolling fields)
— NOT a forked dataclass. ``option_type`` + ``interval`` are the two additional path
selectors passed alongside it (the coordinate carries the other seven).

A NULLABLE **Greeks overlay** (``iv_used`` / ``delta`` / ``gamma`` / ``theta`` /
``vega`` / ``rho`` / ``spot_at_bar`` / ``t_years_used`` / ``rate_used`` /
``greeks_provenance_id`` / ``iv_source``) is RESERVED at ``schema_version=1`` — written
as null columns this pass; the Greeks pass (§3, deferred) fills them latest-wins.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from brutex.adapters.dhan.rolling_option_constants import (
    DHAN_ROLLING_SUBDIR,
    ROLLING_OPTIONS_SCHEMA_VERSION,
)
from brutex.adapters.dhan.rolling_option_parser import RollingOptionBar
from brutex.data.atomic_io import atomic_write_via, lake_write_guard
from brutex.data.errors import parquet_read_skip_errors
from brutex.data.option_parquet_writer import OptionSeriesCoordinate
from brutex.data.partition import partition_for_timestamp
from brutex.data.partition_lock import BoundedLockRegistry, partition_write_lock
from brutex.models import BruteXError
from brutex.money import round_money

if TYPE_CHECKING:
    from decimal import Decimal

    import polars as pl

#: Process-wide registry of per-partition write locks (the Groww / Dhan-spot
#: DEC-OPTION-PARTITION-LOCK-001 idiom). Keyed by the resolved destination ``Path``
#: so two workers writing the SAME rolling-ATM month partition serialise their
#: read-modify-write merge, while writers to DIFFERENT partitions never contend.
_PARTITION_LOCKS: BoundedLockRegistry = BoundedLockRegistry()

#: The Greeks-overlay columns reserved (all-null) at schema version 1 — filled by
#: the deferred Greeks pass (§3). ``iv_source`` is Utf8; the rest are Float64 save
#: ``greeks_provenance_id`` (Int64). Ordered for a stable render.
_GREEKS_OVERLAY_FLOAT_FIELDS: tuple[str, ...] = (
    "iv_used",
    "delta",
    "gamma",
    "theta",
    "vega",
    "rho",
    "spot_at_bar",
    "t_years_used",
    "rate_used",
)


class RollingOptionWriterError(BruteXError):
    """A rolling-ATM option Parquet write failed or got malformed input."""


def strike_offset_safe(label: str) -> str:
    """Map an honest ATM-relative label to a path-hazard-safe partition segment.

    ``ATM`` → ``ATM``, ``ATM+1`` → ``ATMp1``, ``ATM-10`` → ``ATMm10`` (``+`` → ``p``,
    ``-`` → ``m``). The honest label is preserved verbatim in the ``strike_offset``
    column; only the PATH segment is sanitised.

    Raises
    ------
    RollingOptionWriterError
        An empty label (fail-loud — never a blank path segment).
    """
    if not label:
        raise RollingOptionWriterError("strike_offset label must be non-empty")
    return label.replace("+", "p").replace("-", "m")


def _safe(segment: str, *, field: str) -> str:
    """Return *segment* unchanged, rejecting path-traversal / separator characters.

    The rolling selectors are enum-controlled, but a defensive guard keeps a
    mis-wired coordinate from ever escaping the ``rolling_options/`` subtree.
    """
    if not segment:
        raise RollingOptionWriterError(f"{field} must be non-empty")
    if "/" in segment or "\\" in segment or ".." in segment:
        raise RollingOptionWriterError(f"unsafe {field} segment {segment!r}")
    return segment


class RollingOptionWriter:
    """Writes :class:`RollingOptionBar` rows to the partitioned rolling-ATM lake.

    Parameters
    ----------
    lake_root
        The Dhan FEED root (``<top>/feeds/dhan``) — the rolling lake lives under
        ``<lake_root>/rolling_options/``. Created on first write. (The ingest binds
        this via :func:`~brutex.data.feed_root.feed_lake_root`, so Groww bytes are
        untouched — path-structural attribution.)
    """

    def __init__(self, *, lake_root: Path) -> None:
        self._lake_root = lake_root

    @property
    def lake_root(self) -> Path:
        """Return the configured Dhan feed lake root."""
        return self._lake_root

    def _partition_dir(
        self, *, coordinate: OptionSeriesCoordinate, option_type: str, interval: str
    ) -> Path:
        """Build the selector-fixed partition directory (six selectors + interval)."""
        return (
            self._lake_root
            / DHAN_ROLLING_SUBDIR
            / _safe(coordinate.exch_id, field="exchange")
            / _safe(coordinate.underlying_symbol, field="underlying")
            / _safe(coordinate.expiry_flag, field="expiry_flag")
            / f"EC{coordinate.expiry_code}"
            / _safe(strike_offset_safe(coordinate.strike_offset), field="strike_offset")
            / _safe(option_type, field="option_type")
            / _safe(interval, field="interval")
        )

    def write(
        self,
        *,
        coordinate: OptionSeriesCoordinate,
        option_type: str,
        interval: str,
        bars: Sequence[RollingOptionBar],
    ) -> int:
        """Persist *bars* for one rolling-ATM key; return the count of newly-written rows.

        Bars are split into ``(year, month)`` partitions and each group is merged
        into its partition file keyed by ``timestamp`` (keep-first) — a pure re-pull
        (every bar already stored) writes nothing and returns 0. *coordinate* carries
        the seven series-addressing fields; *option_type* + *interval* are the two
        additional path selectors. Both are echoed as real columns on every row.
        """
        if not bars:
            return 0
        partition_dir = self._partition_dir(
            coordinate=coordinate, option_type=option_type, interval=interval
        )
        groups: dict[tuple[int, int], list[RollingOptionBar]] = {}
        for bar in bars:
            groups.setdefault(partition_for_timestamp(bar.timestamp), []).append(bar)
        added_total = 0
        for (year, month), group in sorted(groups.items()):
            dest = partition_dir / f"{year:04d}" / f"{month:02d}.parquet"
            added_total += self._merge_partition(
                dest, coordinate=coordinate, option_type=option_type, interval=interval, bars=group
            )
        return added_total

    def _merge_partition(
        self,
        dest: Path,
        *,
        coordinate: OptionSeriesCoordinate,
        option_type: str,
        interval: str,
        bars: list[RollingOptionBar],
    ) -> int:
        """Union *bars* into *dest* keyed by ``timestamp``; return new DISTINCT rows.

        The read-modify-write runs under the per-path in-process lock + the
        cross-process ``partition_write_lock`` (a concurrent CLI + dashboard pull
        sharing the lake cannot lost-update) + ``lake_write_guard`` (ENOSPC / OOM →
        :class:`~brutex.data.errors.LakeWriteHalt`). The merge unions by ``timestamp``
        keep-first, so the stored bytes are order-independent (CLAUDE.md §2 Rule 7).
        """
        import polars as pl

        with (
            _PARTITION_LOCKS.acquire(dest),
            partition_write_lock(self._lake_root, dest),
            lake_write_guard(),
        ):
            new_df = _render_df(coordinate, option_type=option_type, interval=interval, bars=bars)
            if dest.exists():
                try:
                    existing = pl.read_parquet(dest, glob=False)
                except parquet_read_skip_errors() as exc:
                    raise RollingOptionWriterError(
                        f"corrupt existing rolling-option partition {dest}: {exc}"
                    ) from exc
                before = int(existing.n_unique(subset=["timestamp"]))
                combined = (
                    pl.concat([existing, new_df], how="vertical_relaxed")
                    .unique(subset=["timestamp"], keep="first", maintain_order=True)
                    .sort("timestamp")
                )
            else:
                before = 0
                combined = new_df.unique(
                    subset=["timestamp"], keep="first", maintain_order=True
                ).sort("timestamp")
            added = int(combined.height) - before
            if added > 0:
                dest.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_via(dest, combined.write_parquet)
            return added


def _money(value: Decimal) -> float:
    """Round a money :class:`~decimal.Decimal` to 2dp (DEC-MONEY-2DP-001, HALF-UP) → float."""
    return float(round_money(value))


def _opt_money(value: Decimal | None) -> float | None:
    """2dp-round an optional money field; ``None`` stays ``None``."""
    return None if value is None else _money(value)


def _render_df(
    coordinate: OptionSeriesCoordinate,
    *,
    option_type: str,
    interval: str,
    bars: Sequence[RollingOptionBar],
) -> pl.DataFrame:
    """Build a typed polars DataFrame at ``ROLLING_OPTIONS_SCHEMA_VERSION`` from *bars*.

    Coordinate-echo columns (constant within a partition) are promoted from the
    selectors; per-ROW columns carry the drifting resolved metadata; the nullable
    Greeks overlay is reserved (all-null). Money fields (``open`` / ``high`` /
    ``low`` / ``close`` / ``spot`` / ``resolved_strike``) are 2dp-snapped; ``iv``
    keeps full precision (a volatility metric, NOT money).
    """
    import polars as pl

    count = len(bars)
    rows: dict[str, list[object]] = {
        "schema_version": [ROLLING_OPTIONS_SCHEMA_VERSION] * count,
        # ── coordinate echo (promoted from the partition selectors) ──
        "exch_id": [coordinate.exch_id] * count,
        "exchange_segment": [coordinate.exchange_segment] * count,
        "security_id": [coordinate.security_id] * count,
        "underlying_symbol": [coordinate.underlying_symbol] * count,
        "expiry_flag": [coordinate.expiry_flag] * count,
        "expiry_code": [coordinate.expiry_code] * count,
        "strike_offset": [coordinate.strike_offset] * count,
        "option_type": [option_type] * count,
        "interval": [interval] * count,
        # ── per-bar value fields ──
        "timestamp": [b.timestamp for b in bars],
        "open": [_money(b.open) for b in bars],
        "high": [_money(b.high) for b in bars],
        "low": [_money(b.low) for b in bars],
        "close": [_money(b.close) for b in bars],
        "volume": [b.volume for b in bars],
        "open_interest": [b.open_interest for b in bars],
        "iv": [None if b.iv is None else float(b.iv) for b in bars],  # NOT money
        "spot": [_opt_money(b.spot) for b in bars],
        # ── per-ROW resolved metadata (the rolling-ATM crux — drifts per bar) ──
        "resolved_strike": [_opt_money(b.resolved_strike) for b in bars],
        "resolved_expiry_date": [b.resolved_expiry_date for b in bars],
        "resolved_expiry_source": [b.resolved_expiry_source for b in bars],
    }
    frame = pl.DataFrame(rows).with_columns(
        pl.col("schema_version").cast(pl.Int32),
        pl.col("security_id").cast(pl.Int64),
        pl.col("expiry_code").cast(pl.Int64),
        pl.col("volume").cast(pl.Int64),
        pl.col("open_interest").cast(pl.Int64),
        pl.col("iv").cast(pl.Float64),
        pl.col("spot").cast(pl.Float64),
        pl.col("resolved_strike").cast(pl.Float64),
        pl.col("resolved_expiry_date").cast(pl.Date),
    )
    # Reserved nullable Greeks overlay (schema_version=1): the Greeks pass fills
    # these latest-wins. Written as typed null columns so a read-back is
    # schema-tolerant.
    return frame.with_columns(
        *(pl.lit(None, dtype=pl.Float64).alias(name) for name in _GREEKS_OVERLAY_FLOAT_FIELDS),
        pl.lit(None, dtype=pl.Int64).alias("greeks_provenance_id"),
        pl.lit(None, dtype=pl.Utf8).alias("iv_source"),
    )


__all__ = [
    "RollingOptionWriter",
    "RollingOptionWriterError",
    "strike_offset_safe",
]
