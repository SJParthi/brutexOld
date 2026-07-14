"""Bar sink protocol + Parquet-backed implementation.

A :class:`BarSink` is the write-side interface between the Phase D
candle runner and the canonical data lake. The driver calls
:meth:`BarSink.append` with a symbol, interval, and a list of
normalised :class:`CandleBar` objects; the sink is responsible for
persisting them to the configured storage layer and updating the
catalog.

Two implementations ship in this module:

* :class:`NullBarSink` — discards input. Used as the default when
  no data lake is wired up (P1a tests, unit-test fixtures, dry-run
  CLI). Makes the ``bar_sink`` parameter in :class:`PhaseDDriver`
  a no-op-friendly injection point.

* :class:`ParquetBarSink` — the real sink. Groups incoming bars by
  ``(year, month)`` partition and delegates each group to
  :class:`ParquetWriter.write`. Upserts the resulting
  :class:`CatalogEntry` into the DuckDB catalog.

``polars`` / ``duckdb`` are only imported when
:class:`ParquetBarSink` actually writes — the module import itself
has no heavy deps.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from brutex.data.catalog import Catalog
from brutex.data.frame_write_gate import DegradeReason, FrameWriteStats, note_degrade
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import (
    ParquetWriter,
    WriteResult,
    fold_max_close_delta,
    max_conflict_close_delta,
)
from brutex.data.partition import partition_for_timestamp

if TYPE_CHECKING:  # pragma: no cover — typing-only polars import (lazy at runtime)
    import polars as pl


def vendor_surface_from_write_results(
    results: Sequence[WriteResult],
) -> tuple[int, Decimal | None]:
    """Fold per-partition write results into ``(dup_rows_dropped, max_close_delta)``.

    DEC-VENDOR-CONFLICT-SURFACE-001 — the ONE fold from a call's
    :class:`~brutex.data.parquet_writer.WriteResult` tuple to the two honest
    vendor-behaviour counters an append reports (CLAUDE.md §11 COMMON): the SUM
    of within-batch vendor duplicate rows dropped, and the MAX absolute
    keep-first close delta across every recorded conflict (exact Decimal —
    ``None`` when no conflict carried a close anywhere). Shared by
    :class:`ParquetBarSink` and the pooled
    :class:`~brutex.data.write_consumer.WriteConsumerSink` merge so the two
    production sinks can never drift.
    """
    dup_rows = sum(wr.vendor_dup_rows_dropped for wr in results)
    delta: Decimal | None = None
    for wr in results:
        delta = fold_max_close_delta(delta, max_conflict_close_delta(wr.conflicts))
    return dup_rows, delta


@dataclass(frozen=True, slots=True)
class SinkAppendResult:
    """Outcome of one :meth:`BarSink.append` call.

    Attributes
    ----------
    bars_appended
        Number of bars ACCEPTED by the sink — the INPUT batch size (``len(bars)``).
        This is the historical contract and is DELIBERATELY UNCHANGED: several
        downstream consumers read it as a ``> 0`` "did this call carry data?" gate
        (`spot_daily_resample` derived/no-minute split, `pull_controller`'s
        completed/no-data split), so it MUST stay the input count or a clean
        idempotent re-pull (every bar already stored) would mis-flip those gates to
        "no data". For the honest DEDUPED count of bars this call actually ADDED to
        the lake, read :attr:`bars_net_new` (DEC-SINK-DEDUP-COUNT-001).
    bars_net_new
        Number of bars actually ADDED to the lake by this call — the DEDUPED count
        (DEC-SINK-DEDUP-COUNT-001), summed from each partition's
        :attr:`~brutex.data.parquet_writer.WriteResult.bars_added`. A re-pull of a
        window whose bars are all already stored reports ``0`` (the idempotent
        keep-first merge added nothing); a keep-first conflict adds ``0`` (the stored
        bar wins). This is the HONEST "how many new bars did this write contribute"
        figure for a ledger / report column, never the inflated "re-counted what was
        already there". A :class:`NullBarSink` (no dedup state) reports the input
        count here too — there is nothing stored to dedupe against.
    partitions_written
        Number of distinct ``(year, month)`` partition files touched.
    write_results
        Per-partition :class:`WriteResult` tuples, in write order.
        Empty for sinks that don't actually write (e.g.
        :class:`NullBarSink`).
    vendor_dup_rows_dropped
        WITHIN-BATCH duplicate vendor rows the writer collapsed across this
        call's partitions (DEC-VENDOR-CONFLICT-SURFACE-001 — the sum of each
        :attr:`~brutex.data.parquet_writer.WriteResult.vendor_dup_rows_dropped`).
        ``0`` for a duplicate-free append and for non-writing sinks.
    max_close_delta
        The MAX absolute vendor-vs-stored close delta across every keep-first
        conflict this call recorded (exact Decimal rupees — prices are Decimal,
        never float, CLAUDE.md §6). ``None`` when the call recorded no conflict
        — honestly distinct from a real ``Decimal("0")`` delta.
    """

    bars_appended: int
    partitions_written: int
    write_results: tuple[WriteResult, ...]
    bars_net_new: int = 0
    vendor_dup_rows_dropped: int = 0
    max_close_delta: Decimal | None = None


@runtime_checkable
class BarSink(Protocol):
    """Write-side interface for canonical OHLCV bars."""

    def append(
        self,
        *,
        exchange: str,
        segment: str,
        symbol: str,
        interval: str,
        bars: Sequence[CandleBar],
        merge_mode: str = "keep-first",
    ) -> SinkAppendResult:
        """Persist *bars* for the given partition coordinates."""
        ...


# ────────────────────────────────────────────────────────────────────────
# NullBarSink — default no-op sink
# ────────────────────────────────────────────────────────────────────────


class NullBarSink:
    """A :class:`BarSink` that discards everything.

    Used as the default when the Phase D driver is constructed without
    an explicit sink. Keeps the driver interface uniform — every
    SUCCESS path calls ``sink.append(...)`` whether or not the caller
    cares about persistence.
    """

    def append(
        self,
        *,
        exchange: str,
        segment: str,
        symbol: str,
        interval: str,
        bars: Sequence[CandleBar],
        merge_mode: str = "keep-first",
    ) -> SinkAppendResult:
        """Discard *bars* and report the count — no persistence side effects."""
        del exchange, segment, symbol, interval, merge_mode
        return SinkAppendResult(
            bars_appended=len(bars),
            partitions_written=0,
            write_results=(),
            bars_net_new=len(bars),  # no dedup state — net-new == input
        )


# ────────────────────────────────────────────────────────────────────────
# ParquetBarSink — production sink
# ────────────────────────────────────────────────────────────────────────


class ParquetBarSink:
    """Writes bars to canonical Parquet + upserts into the DuckDB catalog.

    The sink groups each incoming ``append`` batch by
    ``(year, month)`` before delegating to
    :meth:`ParquetWriter.write`, so one call can safely contain bars
    spanning multiple months — e.g. a ``1minute`` chunk that straddles
    a month boundary.

    Parameters
    ----------
    writer
        Pre-built :class:`ParquetWriter`. Typically one per run.
    catalog
        Optional :class:`Catalog` reference. If supplied, every
        successful write produces a :class:`CatalogEntry` upsert.
        If ``None``, the sink writes Parquet files but does not
        maintain the DuckDB catalog — useful for tests or for users
        who only need raw files.
    """

    def __init__(self, *, writer: ParquetWriter, catalog: Catalog | None = None) -> None:
        self._writer = writer
        self._catalog = catalog

    @property
    def writer(self) -> ParquetWriter:
        """Return the underlying :class:`ParquetWriter`."""
        return self._writer

    @property
    def catalog(self) -> Catalog | None:
        """Return the configured :class:`Catalog`, or None if no catalog is wired."""
        return self._catalog

    def append(
        self,
        *,
        exchange: str,
        segment: str,
        symbol: str,
        interval: str,
        bars: Sequence[CandleBar],
        merge_mode: str = "keep-first",
    ) -> SinkAppendResult:
        """Group *bars* by partition and write each group.

        Bars spanning more than one ``(year, month)`` partition are
        handled transparently — each group becomes exactly one
        :meth:`ParquetWriter.write` call.

        *merge_mode* is forwarded verbatim to :meth:`ParquetWriter.write`:
        ``"keep-first"`` (default, the vendor-spine identity) or
        ``"latest-wins"`` (the derived ``1day`` partition's self-healing
        restate — DEC-1MINUTE-ONLY-DERIVE-1DAY-001 / FIX-2).
        """
        if not bars:
            return SinkAppendResult(
                bars_appended=0,
                partitions_written=0,
                write_results=(),
            )

        # Group by (year, month)
        groups: dict[tuple[int, int], list[CandleBar]] = defaultdict(list)
        for bar in bars:
            key = partition_for_timestamp(bar.timestamp)
            groups[key].append(bar)

        results: list[WriteResult] = []
        for (_year, _month), group in sorted(groups.items()):
            write_result = self._writer.write(
                exchange=exchange,
                segment=segment,
                symbol=symbol,
                interval=interval,
                bars=group,
                merge_mode=merge_mode,
            )
            results.append(write_result)

            if self._catalog is not None:
                entry = self._writer.make_catalog_entry(
                    exchange=exchange,
                    segment=segment,
                    symbol=symbol,
                    interval=interval,
                    result=write_result,
                )
                self._catalog.upsert(entry)

        # ``bars_appended`` stays the INPUT count (the historical contract that several
        # ``> 0`` "did this call carry data?" gates depend on — see SinkAppendResult docs).
        # ``bars_net_new`` (DEC-SINK-DEDUP-COUNT-001) is the HONEST DEDUPED count, summed
        # across the partitions this call touched: a re-pull whose bars are all already in
        # the lake merges to ``bars_added == 0`` per partition, so net-new is 0 there. The
        # on-disk merge already dedups idempotently — this only ADDS an honest count field,
        # changing NO existing control flow. The two vendor-behaviour counters
        # (DEC-VENDOR-CONFLICT-SURFACE-001) ride the COMMON fold so the pull can
        # surface them instead of the log swallowing them.
        dup_rows, max_delta = vendor_surface_from_write_results(results)
        return SinkAppendResult(
            bars_appended=len(bars),
            partitions_written=len(results),
            write_results=tuple(results),
            bars_net_new=sum(wr.bars_added for wr in results),
            vendor_dup_rows_dropped=dup_rows,
            max_close_delta=max_delta,
        )

    def append_frame(
        self,
        *,
        exchange: str,
        segment: str,
        symbol: str,
        interval: str,
        frame: pl.DataFrame,
        merge_mode: str = "latest-wins",
        absent_open_interest: int | None = 0,
        stats: FrameWriteStats | None = None,
    ) -> SinkAppendResult:
        """FRAME-DIRECT sibling of :meth:`append` (DEC-PREBUILD-FRAME-WRITE-001).

        Persists a resampled polars *frame* (columns ``ts_utc, open, high,
        low, close, volume[, open_interest]``) WITHOUT materialising per-bar
        :class:`~brutex.data.models.CandleBar` objects — the prebuild's
        measured GIL-held wall. The vendor :meth:`append` entry is
        byte/type-UNTOUCHED; the ONLY production caller of this sibling is
        the derived-TF writer built by
        :func:`~brutex.data.timeframe_materialize.make_lake_write_tf` when
        the ``BRUTEX_PREBUILD_FRAME_WRITE`` gate is engaged.

        The sink OWNS its derived-path contracts here (never bypassed):

        * an EMPTY frame early-returns the SAME empty
          :class:`SinkAppendResult` as ``append([])`` (the C13 law);
        * **`bars_appended` = `frame.height`** — the INPUT count, exactly
          the legacy ``len(bars)`` by 1:1 construction (the historical
          ``> 0`` "did this call carry data?" gate contract);
        * vectorized UTC-month grouping (the exact
          :func:`~brutex.data.partition.partition_for_timestamp` law on the
          tz-aware column), iterated in ASCENDING month order mirroring the
          legacy ``sorted(groups.items())``;
        * one :meth:`ParquetWriter.write_frame` per month sub-frame; the
          catalog arm reused verbatim (the prebuild sink wires
          ``catalog=None``, but parity is kept for any future caller);
        * ``bars_net_new`` = Σ ``WriteResult.bars_added`` as today.

        A frame whose ``ts_utc`` column cannot be trusted for UTC-month
        grouping (missing / wrong dtype / nulls) degrades the WHOLE call to
        the verbatim legacy ``append(bars=frame_to_candle_bars(frame), …)``
        — which reproduces today's grouping AND today's failure semantics
        exactly (counted ``schema_mismatch``; the writer-level G1 re-checks
        the full shape per month either way).
        """
        import polars as pl

        if frame.height == 0:
            # The C13 empty-input law — byte-identical to ``append([])``.
            return SinkAppendResult(
                bars_appended=0,
                partitions_written=0,
                write_results=(),
            )

        ts_dtype = frame.schema.get("ts_utc")
        if (
            ts_dtype != pl.Datetime(time_unit="us", time_zone="UTC")
            or frame.get_column("ts_utc").null_count() > 0
        ):
            # The month grouping below needs a trustworthy tz-aware UTC
            # column; anything else routes the WHOLE frame through the
            # verbatim legacy path (same grouping, same errors — e.g. a
            # null timestamp raises CandleBar's own loud error exactly as
            # the flag-OFF branch would). Observable, never silent.
            note_degrade(DegradeReason.SCHEMA_MISMATCH, stats, symbol=symbol, interval=interval)
            from brutex.data.spot_daily_resample import frame_to_candle_bars

            return self.append(
                exchange=exchange,
                segment=segment,
                symbol=symbol,
                interval=interval,
                bars=frame_to_candle_bars(frame, absent_open_interest=absent_open_interest),
                merge_mode=merge_mode,
            )

        # Vectorized UTC-month grouping — the exact `partition_for_timestamp`
        # law (year/month of the UTC instant; the column IS UTC by the dtype
        # check above). Helper columns are dropped before the writer sees the
        # sub-frame (G1 would reject them).
        keyed = frame.with_columns(
            pl.col("ts_utc").dt.year().alias("__fw_year"),
            pl.col("ts_utc").dt.month().alias("__fw_month"),
        )
        groups = keyed.partition_by(["__fw_year", "__fw_month"], as_dict=True)

        results: list[WriteResult] = []
        for _key, sub in sorted(groups.items()):  # ascending (year, month) — the sink law
            month_frame = sub.drop("__fw_year", "__fw_month")
            write_result = self._writer.write_frame(
                exchange=exchange,
                segment=segment,
                symbol=symbol,
                interval=interval,
                frame=month_frame,
                merge_mode=merge_mode,
                absent_open_interest=absent_open_interest,
                stats=stats,
            )
            results.append(write_result)

            if self._catalog is not None:
                entry = self._writer.make_catalog_entry(
                    exchange=exchange,
                    segment=segment,
                    symbol=symbol,
                    interval=interval,
                    result=write_result,
                )
                self._catalog.upsert(entry)

        dup_rows, max_delta = vendor_surface_from_write_results(results)
        return SinkAppendResult(
            bars_appended=frame.height,
            partitions_written=len(results),
            write_results=tuple(results),
            bars_net_new=sum(wr.bars_added for wr in results),
            vendor_dup_rows_dropped=dup_rows,
            max_close_delta=max_delta,
        )


__all__ = [
    "BarSink",
    "NullBarSink",
    "ParquetBarSink",
    "SinkAppendResult",
    "vendor_surface_from_write_results",
]
