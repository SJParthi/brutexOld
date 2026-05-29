"""Click-to-pull controller — ingest ONE instrument's spot OHLC.

Powers the F&O Universe Explorer's "⤓ Pull spot — all timeframes"
button (DEC-UNIVERSE-DASHBOARD-001). When the operator clicks a row and
hits pull, :func:`pull_spot` ingests that single instrument's SPOT OHLC
across every Groww native timeframe
(:data:`~brutex.adapters.groww.chunks.ALL_GROWW_INTERVALS`) for the
requested year, reusing the proven
:func:`~brutex.adapters.groww.ingest.run_ingest` orchestration core
(CLAUDE.md §11 COMMON — no copy-paste of the chunk/fetch spine).

## Offline-first

The default and the CI-tested path is **dry-run**: a
:class:`~brutex.adapters.groww.ingest.SyntheticCandleFetcher` emits valid
synthetic candle rows so the whole parse → guard → sink pipeline runs
with NO Groww credentials and NO network. The LIVE Groww wiring lives in
:func:`_build_live_pull_fetcher`, which is ``# pragma: no cover`` because
it needs real credentials + the ``growwapi`` SDK (mirrors
``brutex.cli._build_live_fetcher``). The network is blocked in CI, so the
live path is intentionally not unit-covered — its pure orchestration is
exercised end-to-end through the dry-run path.

## Scope (Golden Rule #3)

This is a LAKE-surface action authorised by DEC-P0-063 (#200) +
DEC-FETCH-UNIVERSE-001 (#299): pulling spot OHLC for any index OR
F&O-eligible stock into the lake. The BRUTE-FORCE ENGINE is unchanged —
it still sweeps only the 3 indices. Pulling a stock's spot here puts it
in the lake (REFERENCE_ONLY), ready for a future, separately-authorised
engine expansion.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from brutex.adapters.groww.chunks import ALL_GROWW_INTERVALS
from brutex.adapters.groww.fetch_loop import CandleFetcher
from brutex.adapters.groww.ingest import SyntheticCandleFetcher, run_ingest
from brutex.data.watermark import FetchWatermark

_LOG = structlog.get_logger(__name__)

#: A factory that builds the ``(fetcher, reauth)`` pair given the lake
#: root. The default builds the offline synthetic pair; the live path
#: injects :func:`_build_live_pull_fetcher`.
FetcherFactory = Callable[[Path], tuple[CandleFetcher, Callable[[], None] | None]]


@dataclass(frozen=True, slots=True)
class TimeframeResult:
    """Per-timeframe completed / failed counts for one pull."""

    interval: str
    completed: int
    failed: int


@dataclass(slots=True)
class PullSpotReport:
    """Result of a single :func:`pull_spot` call.

    Attributes
    ----------
    symbol
        The Groww spot symbol that was pulled (e.g. ``"NSE-NIFTY"``).
    year
        The calendar year the pull covered ``[Jan 1, Jan 1 next year)``.
    dry_run
        ``True`` when synthetic data was used (no creds / no network).
        The ``/universe`` page shows a "PREVIEW · synthetic" badge in
        this case.
    timeframes
        One :class:`TimeframeResult` per interval, in
        :data:`~brutex.adapters.groww.chunks.ALL_GROWW_INTERVALS` order.
    """

    symbol: str
    year: int
    dry_run: bool
    timeframes: list[TimeframeResult] = field(default_factory=list)

    @property
    def total_completed(self) -> int:
        """Total chunks fetched + persisted across all timeframes."""
        return sum(tf.completed for tf in self.timeframes)

    @property
    def total_failed(self) -> int:
        """Total chunks that failed terminally across all timeframes."""
        return sum(tf.failed for tf in self.timeframes)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly dict for the ``/api/v1/universe/pull`` route."""
        return {
            "symbol": self.symbol,
            "year": self.year,
            "dry_run": self.dry_run,
            "total_completed": self.total_completed,
            "total_failed": self.total_failed,
            "timeframes": [
                {
                    "interval": tf.interval,
                    "completed": tf.completed,
                    "failed": tf.failed,
                }
                for tf in self.timeframes
            ],
        }


def _build_dry_run_fetcher(
    _lake_root: Path,
) -> tuple[CandleFetcher, Callable[[], None] | None]:
    """Build the offline ``(SyntheticCandleFetcher, None)`` pair.

    The synthetic fetcher emits valid candle rows directly (no sink /
    Parquet) so the pull runs with zero dependencies — the dry-run proof
    that the spine works. ``reauth`` is ``None`` because synthetic data
    never expires a session.
    """
    return SyntheticCandleFetcher(), None


def _build_live_pull_fetcher(  # pragma: no cover — live Groww creds + SDK + network
    lake_root: Path,
) -> tuple[CandleFetcher, Callable[[], None] | None]:
    """Wire the real Groww auth + persisting candle fetcher.

    Mirrors :func:`brutex.cli._build_live_fetcher`: builds a
    ``ParquetBarSink`` over *lake_root*, authenticates via
    :class:`~brutex.adapters.groww.auth.GrowwAuth`, and returns the
    ``(PersistingFetcher, reauth)`` pair. ``# pragma: no cover`` — needs
    real credentials and network, both absent in CI.
    """
    from growwapi.groww.exceptions import BaseGrowwException

    from brutex.adapters.groww.auth import GrowwAuth
    from brutex.adapters.groww.client import GrowwCandleFetcher
    from brutex.adapters.groww.persist import PersistingFetcher
    from brutex.data.parquet_writer import ParquetWriter
    from brutex.data.sink import ParquetBarSink
    from brutex.orchestrator.rate_limiter import AdaptiveTokenBucket

    sink = ParquetBarSink(writer=ParquetWriter(lake_root=lake_root))
    auth = GrowwAuth.from_env()
    groww = GrowwCandleFetcher(
        sdk=auth.session(),
        bucket=AdaptiveTokenBucket(),
        base_exception=BaseGrowwException,
        reauth_fn=auth.refresh,
    )
    return PersistingFetcher(inner=groww, sink=sink), groww.reauth


def pull_spot(
    symbol: str,
    *,
    lake_root: Path,
    year: int | None = None,
    dry_run: bool,
    fetcher_factory: FetcherFactory | None = None,
) -> PullSpotReport:
    """Ingest *symbol*'s spot OHLC across every timeframe for *year*.

    Plans + drives one :func:`~brutex.adapters.groww.ingest.run_ingest`
    pass over the single *symbol* and ALL
    :data:`~brutex.adapters.groww.chunks.ALL_GROWW_INTERVALS`, covering
    ``[Jan 1 year, Jan 1 year+1)`` in UTC. Idempotent + resumable via a
    fresh :class:`~brutex.data.watermark.FetchWatermark`.

    Parameters
    ----------
    symbol
        Groww spot symbol, e.g. ``"NSE-NIFTY"`` or ``"NSE-RELIANCE"``.
    lake_root
        Lake directory the live sink writes Parquet into. Unused by the
        dry-run synthetic fetcher (which emits rows directly).
    year
        Calendar year to pull. ``None`` (default) uses the current UTC
        year.
    dry_run
        When ``True``, use the offline synthetic fetcher (no creds /
        network). When ``False``, build the live Groww fetcher via
        *fetcher_factory*.
    fetcher_factory
        Override the ``(fetcher, reauth)`` builder. Defaults to the
        offline synthetic builder when *dry_run* is ``True`` and to the
        live Groww builder otherwise. Tests inject a fake factory to
        exercise the non-dry-run branch without network.

    Returns
    -------
    PullSpotReport
        Per-timeframe completed / failed counts + the ``dry_run`` flag.

    Raises
    ------
    ValueError
        If *symbol* is empty / blank.
    """
    if not symbol.strip():
        raise ValueError("symbol must be a non-empty Groww spot symbol")

    resolved_year = year if year is not None else datetime.now(UTC).year
    start = datetime(resolved_year, 1, 1, tzinfo=UTC)
    end = datetime(resolved_year + 1, 1, 1, tzinfo=UTC)

    if fetcher_factory is None:
        fetcher_factory = _build_dry_run_fetcher if dry_run else _build_live_pull_fetcher
    fetcher, reauth = fetcher_factory(lake_root)

    _LOG.info(
        "universe.pull.start",
        symbol=symbol,
        year=resolved_year,
        dry_run=dry_run,
        intervals=len(ALL_GROWW_INTERVALS),
    )

    watermark = FetchWatermark()
    report = run_ingest(
        fetcher,
        [symbol],
        ALL_GROWW_INTERVALS,
        start,
        end,
        watermark,
        reauth=reauth,
    )

    timeframes = [
        TimeframeResult(
            interval=interval,
            completed=report.outcomes[symbol, interval].completed,
            failed=report.outcomes[symbol, interval].failed,
        )
        for interval in ALL_GROWW_INTERVALS
    ]
    pull_report = PullSpotReport(
        symbol=symbol,
        year=resolved_year,
        dry_run=dry_run,
        timeframes=timeframes,
    )
    _LOG.info(
        "universe.pull.done",
        symbol=symbol,
        year=resolved_year,
        dry_run=dry_run,
        completed=pull_report.total_completed,
        failed=pull_report.total_failed,
    )
    return pull_report


__all__ = [
    "FetcherFactory",
    "PullSpotReport",
    "TimeframeResult",
    "pull_spot",
]
