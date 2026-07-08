"""Read REAL futures bars from the F&O candle lake for the executor (R14/R15).

DEC-FUTURES-EXEC-TRANSLATION-001: the production
:class:`~brutex.futures.executor.FuturesBarSource` — the futures twin of
:class:`brutex.options.lake_source.LakeOptionBarSource`. Given a resolved FUT
``contract_symbol`` and a trade's entry/exit timestamps it reads the contract's
1-minute partition (``bars/<exch>/FNO/<contract_symbol>/1minute/<yyyy>/<mm>.parquet``
— the layout the Groww pull writes, DEC-FNO-CANDLE-LAKE-001) and returns the
EXACT-minute entry + exit bars plus the printed minute PATH between them
(:class:`~brutex.futures.executor.FuturesBarWindow` — the R22 MAE/MFE input), or
``None`` when either endpoint bar is absent — **never a fabricated price and
never a nearest-bar substitute** (R15 / Golden Rule #1).

## Robustness (universal-standards error hardening)

* **Duplicates:** the frame is collapsed keep-FIRST on ``timestamp`` via the
  COMMON :func:`~brutex.data.lake_read.dedup_bars_frame` BEFORE the
  ``{instant → bar}`` dict build, so a poisoned duplicate can never LAST-wins
  overwrite the real bar (DEC-LAKE-READ-UNIQUE-001).
* **Corrupt partition:** the fast whole-tree scan falls back to per-file reads
  on a read failure; each unreadable file is SKIPPED + counted + WARNed
  (``futures_lake.parquet_skip``) — the readable months still serve
  (quarantine-not-abort, the DEC-MOVERS-CORRUPT-SKIP-001 pattern; the error set
  is the COMMON :func:`~brutex.dashboard.movers_resilient.movers_read_skip_errors`).
* **Schema drift:** the scan is schema-tolerant (``missing_columns="insert"``,
  ``extra_columns="ignore"``) so a 7-column FUT tree beside a 17-column
  Greeks-enriched shape (or an older schema) reads without a crash.

## O(1) story (Golden Rule #10)

Each contract's ``{utc_instant → CandleBar}`` map builds ONCE on first touch
(O(bars-in-contract) — a FUT life is ``expiry−60d → expiry`` ≈ ≤ 3 month files)
and is cached; each subsequent lookup is an O(1) dict probe. The PATH build
walks the trade's own minutes (O(bars-in-trade), ≤ ~375 intraday) — inherent
R22 work, stated, never an O(1)-total claim. The path walk reads only instants
``<= exit_ts`` — suffix-independent by construction (no look-ahead, R18).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Final

import structlog

if TYPE_CHECKING:  # pragma: no cover — annotation-only import
    import polars as pl

from brutex.data.models import CandleBar
from brutex.futures.executor import FuturesBarWindow

# The tiny Parquet-cell coercion helpers are IMPORTED from the options lake
# source (CLAUDE.md §11 COMMON — one source of truth, never a copy); R12 is
# untouched (importing is not modifying the options path).
from brutex.options.lake_source import _as_datetime, _as_decimal, _utc_instant

__all__ = ["PARQUET_SKIP_EVENT", "LakeFuturesBarSource"]

_LOG = structlog.get_logger(__name__)

#: Structlog WARNING event emitted once per unreadable Parquet file skipped by
#: the corrupt-partition fallback (quarantine-not-abort; counted on the source).
PARQUET_SKIP_EVENT: Final[str] = "futures_lake.parquet_skip"

#: The F&O lake segment + the only fetched interval (DEC-1MINUTE-ONLY-DERIVE-1DAY-001).
_FNO_SEGMENT: Final[str] = "FNO"
_INTERVAL: Final[str] = "1minute"

#: One lake minute.
_ONE_MINUTE: Final[timedelta] = timedelta(minutes=1)


class LakeFuturesBarSource:
    """Production :class:`~brutex.futures.executor.FuturesBarSource` over the F&O lake.

    Construct with the lake root (the parent of ``bars/``). Stateless per call
    except the per-contract read-through bar cache, so it is safe to reuse
    across a whole run. ``corrupt_skipped`` counts unreadable Parquet files
    skipped by the fallback reader (observable, R28).
    """

    def __init__(self, lake_root: Path) -> None:
        self._lake_root = lake_root
        # contract_symbol → {utc_instant: CandleBar}, built once per contract.
        self._bar_cache: dict[str, dict[datetime, CandleBar]] = {}
        #: Unreadable Parquet files skipped so far (counted, never silent).
        self.corrupt_skipped = 0

    def load_window(
        self, *, contract_symbol: str, entry_ts: datetime, exit_ts: datetime
    ) -> FuturesBarWindow | None:
        """Return the trade's :class:`FuturesBarWindow`, or ``None`` if absent.

        The entry and exit bars are matched on their EXACT UTC minute instants;
        a missing endpoint is an honest ``None`` (→ ``NO_DATA``), never the
        nearest-prior bar (R15). The path collects every printed bar between
        them inclusive, in ascending order.

        Parameters
        ----------
        contract_symbol
            The resolved FUT ``groww_symbol`` (also the partition directory
            name); its leading token is the exchange.
        entry_ts, exit_ts
            The trade's entry/exit timestamps (any zone; localised to UTC).
        """
        bars = self._contract_bars(contract_symbol)
        entry_instant = _utc_instant(entry_ts)
        exit_instant = _utc_instant(exit_ts)
        entry_bar = bars.get(entry_instant)
        exit_bar = bars.get(exit_instant)
        if entry_bar is None or exit_bar is None:
            return None
        path: list[CandleBar] = []
        instant = entry_instant
        while instant <= exit_instant:
            bar = bars.get(instant)
            if bar is not None:
                path.append(bar)
            instant += _ONE_MINUTE
        return FuturesBarWindow(entry_bar=entry_bar, exit_bar=exit_bar, path=tuple(path))

    def _contract_bars(self, contract_symbol: str) -> dict[datetime, CandleBar]:
        """Read the contract's 1-minute bars into ``{utc_instant: CandleBar}`` — cached.

        A contract with no Parquet yet yields an empty map (every lookup misses
        → honest ``None``). Dedup runs keep-FIRST BEFORE the dict build.
        """
        cached = self._bar_cache.get(contract_symbol)
        if cached is not None:
            return cached
        from brutex.data.lake_read import dedup_bars_frame

        exchange = contract_symbol.split("-", maxsplit=1)[0]
        base = self._lake_root / "bars" / exchange / _FNO_SEGMENT / contract_symbol / _INTERVAL
        bars: dict[datetime, CandleBar] = {}
        files = sorted(base.rglob("*.parquet")) if base.is_dir() else []
        if files:
            frame = self._read_frame(base, files)
            frame, _dropped = dedup_bars_frame(frame, source=f"futures_lake:{contract_symbol}")
            for row in frame.iter_rows(named=True):
                bar = CandleBar(
                    timestamp=_as_datetime(row["timestamp"]),
                    open=_as_decimal(row["open"]),
                    high=_as_decimal(row["high"]),
                    low=_as_decimal(row["low"]),
                    close=_as_decimal(row["close"]),
                    volume=int(row["volume"]),
                    open_interest=(
                        None if row["open_interest"] is None else int(row["open_interest"])
                    ),
                )
                bars[_utc_instant(bar.timestamp)] = bar
        self._bar_cache[contract_symbol] = bars
        return bars

    def _read_frame(self, base: Path, files: list[Path]) -> pl.DataFrame:
        """Read the contract tree schema-tolerantly; corrupt files skip + count.

        Fast path: ONE whole-tree ``scan_parquet`` (the options reader's shape).
        On a read failure the fallback re-reads per file, skipping each
        unreadable one with a :data:`PARQUET_SKIP_EVENT` WARN — narrow catch
        (only the COMMON unreadable-parquet error set); a genuine code bug
        still propagates.
        """
        import polars

        from brutex.dashboard.movers_resilient import movers_read_skip_errors

        try:
            return polars.scan_parquet(
                base.as_posix() + "/**/*.parquet",
                missing_columns="insert",
                extra_columns="ignore",
            ).collect()
        except movers_read_skip_errors():
            frames: list[polars.DataFrame] = []
            for file in files:
                try:
                    frames.append(
                        polars.scan_parquet(
                            file.as_posix(), missing_columns="insert", extra_columns="ignore"
                        ).collect()
                    )
                except movers_read_skip_errors() as exc:
                    self.corrupt_skipped += 1
                    _LOG.warning(
                        PARQUET_SKIP_EVENT,
                        file=str(file),
                        reason=f"{type(exc).__name__}: {exc}",
                    )
            if not frames:
                return polars.DataFrame(
                    schema={
                        "timestamp": polars.Datetime(time_zone="UTC"),
                        "open": polars.Float64,
                        "high": polars.Float64,
                        "low": polars.Float64,
                        "close": polars.Float64,
                        "volume": polars.Int64,
                        "open_interest": polars.Int64,
                    }
                )
            return polars.concat(frames, how="diagonal_relaxed")
