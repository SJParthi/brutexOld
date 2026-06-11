"""Manual derived-timeframe **materialiser** — the testable core (DEC-TF-MATERIALIZE-001).

The brute-force sweep evaluates strategies at all 21 timeframes (CLAUDE.md §1).
Only the **1-minute** base is fetched from Dhan; the 19 derived intraday
timeframes are *resampled* from it (DEC-DHAN-1M-ONLY-001) by
:func:`~brutex.data.spot_resample.derive_intraday_tfs`. Re-resampling 1m on
every read would re-pay that cost ~33 billion times across a production sweep.
This module **materialises** a selected derived timeframe **once** — writing it
as its own stored interval partition — so the sweep then reads a 5m / 15m / 60m
bar in **O(1)** (a direct Parquet read), CLAUDE.md §5.

## Manual route + automatic pull-settle trigger

The original surface is the manual ``POST /api/v1/universe/derive-timeframes``
route (DEC-TF-MATERIALIZE-001). DEC-TF-MATERIALIZE-002 adds an AUTOMATIC
trigger: when a spot pull settles with new 1-minute bars, the pull
controller re-materialises the settled symbols through this same core —
gated behind the ``BRUTEX_AUTO_DERIVE_TFS=1`` opt-in (default OFF; storage
stays operator-approved, external-drive-backed via ``$BRUTEX_LAKE_ROOT``).
See :mod:`brutex.data.tf_autoderive` for the planner.

## Pure core, injected I/O (CLAUDE.md §11 COMMON / §4 testability)

:func:`materialize_timeframes` is pure orchestration: the 1-minute **read** and
the per-timeframe **write** are *injected* callables, so the whole control flow
(de-dup, derive, route-each-frame, skip-unknown) is 100% unit-testable with
fakes — no lake, no DuckDB, no FastAPI. The raw lake I/O lives in the route
glue (``# pragma: no cover`` — integration-tested, not line-gated). The actual
resampling is **reused** wholesale from
:func:`~brutex.data.spot_resample.derive_intraday_tfs` (the single source of
truth for the session/DST/no-look-ahead bucketing) — this module never
re-implements it.

## Storable timeframes

The resampler emits all **19** derived intraday timeframes, and since
DEC-TF-VOCAB-21-001 the lake partition layer
(``brutex.data.partition.VALID_INTERVALS``) addresses every one of them.
:data:`INTRADAY_TF_INTERVALS` is the canonical TF-label → partition interval
map (all 19, derived from ``brutex.data.timeframes``); a requested label
absent from a derivation — or absent from the derived dict — is reported as
``skipped``, never written.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from brutex.data.timeframes import (
    ALL_TF_MINUTES,
    DAILY_TF_MINUTES,
    LAKE_INTERVAL_LABELS,
    TF_LABELS,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from pathlib import Path

    import polars as pl

#: Canonical TF-label → lake partition interval map for ALL 19 derived
#: intraday timeframes (DEC-TF-VOCAB-21-001 — derived from the canonical
#: ``brutex.data.timeframes`` maps, CLAUDE.md §11 COMMON). Keys are the
#: :data:`~brutex.data.timeframes.TF_LABELS` the resampler emits; values are
#: the ``brutex.data.partition.VALID_INTERVALS`` member each materialises
#: into. Pre-DEC-TF-VOCAB-21-001 only 8 of the 19 were storable (the lake
#: lacked labels for ``4m, 6m … 14m, 120m, 180m``); the full ladder is now
#: addressable, so every derived TF is storable. An unknown / non-derived
#: requested label is still reported as ``skipped``, never written.
INTRADAY_TF_INTERVALS: Final[dict[str, str]] = {
    TF_LABELS[minutes]: LAKE_INTERVAL_LABELS[minutes]
    for minutes in ALL_TF_MINUTES
    if minutes not in (1, DAILY_TF_MINUTES)  # 1m is the fetched base; 1d is fetched natively
}

#: The materialisable timeframe labels, in canonical (ascending-minutes) order —
#: the route's default selection when the caller omits ``tfs``.
MATERIALIZABLE_TFS: Final[tuple[str, ...]] = tuple(INTRADAY_TF_INTERVALS)


@dataclass(frozen=True, slots=True)
class MaterializeResult:
    """Outcome of one :func:`materialize_timeframes` call.

    Attributes
    ----------
    written
        ``{tf_label: bar_count}`` for every requested timeframe that was
        present in the derivation and written, in request (de-duplicated)
        order. ``bar_count`` is whatever the injected ``write_tf`` returned.
    skipped
        The requested timeframe labels that were NOT written because the
        derivation produced no frame for them (an unknown / non-derived /
        non-storable label), in request (de-duplicated) order.
    """

    written: dict[str, int]
    skipped: tuple[str, ...]

    @property
    def total_bars(self) -> int:
        """Total bars written across every materialised timeframe."""
        return sum(self.written.values())


def materialize_timeframes(
    *,
    tfs: Iterable[str],
    read_1m: Callable[[], pl.DataFrame],
    write_tf: Callable[[str, pl.DataFrame], int],
) -> MaterializeResult:
    """Resample stored 1-minute bars → write each selected derived timeframe.

    Reads the symbol's 1-minute bars **once** (via *read_1m*), resamples them
    to the 19 derived intraday timeframes via the REUSED
    :func:`~brutex.data.spot_resample.derive_intraday_tfs`, then writes each
    **requested** timeframe present in that derivation via *write_tf*. A
    requested label with no derived frame (unknown / non-storable) is recorded
    in :attr:`MaterializeResult.skipped`, never written. Pure orchestration —
    all lake / disk I/O is injected, so this is 100% unit-testable.

    Parameters
    ----------
    tfs
        The requested timeframe labels (e.g. ``("5m", "15m", "60m")``).
        De-duplicated preserving first-seen order via ``dict.fromkeys``
        (O(1)-per-item uniqueness) so a label is never derived or written
        twice — re-listing ``5m`` twice writes it once.
    read_1m
        Zero-arg callable returning the symbol's stored 1-minute bars as a
        sorted :class:`polars.DataFrame` (columns ``ts_utc, open, high, low,
        close, volume``). Injected so the core never touches the lake.
    write_tf
        ``(tf_label, frame) -> bar_count`` callable that persists one
        resampled frame to its own stored interval partition and returns the
        number of bars written. Injected so the core never touches the sink.

    Returns
    -------
    MaterializeResult
        The per-timeframe written bar counts + the skipped labels.
    """
    # Local import keeps the module's import graph free of polars for callers
    # that only need the dataclass / the interval map (CLAUDE.md §11 lazy dep,
    # mirroring spot_resample's own polars-only-when-used discipline).
    from brutex.data.spot_resample import derive_intraday_tfs

    # De-dup preserving first-seen order — O(1) uniqueness, deterministic order
    # (CLAUDE.md §5 / §7). A duplicate label derives + writes exactly once.
    requested = tuple(dict.fromkeys(tfs))

    derived = derive_intraday_tfs(read_1m())

    written: dict[str, int] = {}
    skipped: list[str] = []
    for tf in requested:
        frame = derived.get(tf)
        if frame is None:
            skipped.append(tf)
            continue
        written[tf] = write_tf(tf, frame)

    return MaterializeResult(written=written, skipped=tuple(skipped))


__all__ = [
    "INTRADAY_TF_INTERVALS",
    "MATERIALIZABLE_TFS",
    "MaterializeResult",
    "make_lake_read_1m",
    "make_lake_write_tf",
    "materialize_timeframes",
]


def make_lake_read_1m(  # pragma: no cover — raw lake read, integration-tested (DEC-TF-MATERIALIZE-001)
    *,
    lake_root: Path,
    exchange: str,
    segment: str,
    symbol: str,
) -> Callable[[], pl.DataFrame]:
    """Build the injected 1-minute reader for :func:`materialize_timeframes`.

    Returns a zero-arg callable that reads the symbol's stored ``1minute``
    Parquet partitions into a sorted :class:`polars.DataFrame` with exactly
    the columns the resampler requires (``ts_utc, open, high, low, close,
    volume``). Raw lake I/O — ``# pragma: no cover``; exercised by
    ``tests/integration/test_timeframe_materialize_roundtrip.py``. Moved
    here from the dashboard route glue (DEC-TF-MATERIALIZE-002) so the
    manual route AND the pull-settle auto-derive share ONE builder
    (CLAUDE.md §11 COMMON).
    """
    import polars as pl

    def _read() -> pl.DataFrame:
        base = lake_root / "bars" / exchange / segment / symbol / "1minute"
        if not (base.is_dir() and any(base.rglob("*.parquet"))):
            raise ValueError(
                f"no stored 1minute bars for {exchange}/{segment}/{symbol} — "
                "pull the 1-minute spot first, then derive timeframes"
            )
        df = pl.read_parquet(base.as_posix() + "/**/*.parquet")
        return (
            df.rename({"timestamp": "ts_utc"})
            .select("ts_utc", "open", "high", "low", "close", "volume")
            .sort("ts_utc")
        )

    return _read


def make_lake_write_tf(  # pragma: no cover — raw lake write, integration-tested (DEC-TF-MATERIALIZE-001)
    *,
    lake_root: Path,
    exchange: str,
    segment: str,
    symbol: str,
) -> Callable[[str, pl.DataFrame], int]:
    """Build the injected per-timeframe writer for :func:`materialize_timeframes`.

    Returns a ``(tf_label, frame) -> bars_written`` callable that converts the
    resampled polars frame's rows to :class:`~brutex.data.models.CandleBar`
    objects (``open_interest=0`` for spot) and appends them to the lake via a
    :class:`~brutex.data.sink.ParquetBarSink` under the TF's own stored interval
    partition (:data:`INTRADAY_TF_INTERVALS`). The sink snaps prices to the
    2-decimal tick grid + merges idempotently by ``(year, month)`` (CLAUDE.md §7
    / DEC-SPOT-PRICE-2DP-001). Raw lake I/O — ``# pragma: no cover``; exercised
    by the round-trip integration test. Moved here from the dashboard route
    glue (DEC-TF-MATERIALIZE-002) — one builder for every caller.
    """
    from decimal import Decimal

    from brutex.data.models import CandleBar
    from brutex.data.parquet_writer import ParquetWriter
    from brutex.data.sink import ParquetBarSink

    sink = ParquetBarSink(writer=ParquetWriter(lake_root=lake_root))

    def _write(tf_label: str, frame: pl.DataFrame) -> int:
        interval = INTRADAY_TF_INTERVALS[tf_label]
        bars = [
            CandleBar(
                timestamp=row["ts_utc"],
                open=Decimal(str(row["open"])),
                high=Decimal(str(row["high"])),
                low=Decimal(str(row["low"])),
                close=Decimal(str(row["close"])),
                volume=int(row["volume"]),
                open_interest=0,
            )
            for row in frame.iter_rows(named=True)
        ]
        result = sink.append(
            exchange=exchange,
            segment=segment,
            symbol=symbol,
            interval=interval,
            bars=bars,
        )
        return result.bars_appended

    return _write
