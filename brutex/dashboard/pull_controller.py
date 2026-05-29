"""Click-to-pull controller — ingest ONE instrument's candle data.

Powers the F&O Universe Explorer's "⤓ Pull — all timeframes" button
(DEC-UNIVERSE-DASHBOARD-001). The controller is **instrument-agnostic**:
it pulls **spot / future / option** candle data for ANY Groww symbol —
a spot index/stock (``"NSE-NIFTY"``, ``"NSE-RELIANCE"``), a futures leg
(``"NSE-NIFTY-30Jan25-FUT"``), or an option contract's groww_symbol
(``"NSE-NIFTY-30Jan25-28500-CE"``). When the operator clicks a row, an
expiry's future, or an option-chain CE/PE cell and hits pull,
:func:`pull_candles` ingests that single contract's candle data across
every Groww native timeframe
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
DEC-FETCH-UNIVERSE-001 (#299): pulling candle data (spot / future /
option) for any index OR F&O-eligible stock — or any of its FUT / CE /
PE contracts — into the lake. The BRUTE-FORCE ENGINE is unchanged — it
still sweeps only the 3 indices. Pulling a stock's spot or any contract
here puts it in the lake (REFERENCE_ONLY), ready for a future,
separately-authorised engine expansion.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
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

#: Exchange prefixes a valid Groww symbol may start with. A spot index/
#: stock is ``NSE-…`` / ``BSE-…``; every FUT / CE / PE contract symbol is
#: likewise exchange-prefixed. Used by :func:`pull_candles` (L1 guard) so a
#: bare ``trading_symbol`` (e.g. ``"NIFTY"``) is rejected before it can
#: break :func:`brutex.adapters.groww.client.symbol_to_exchange_segment`.
_KNOWN_EXCHANGE_PREFIXES: tuple[str, ...] = ("NSE-", "BSE-")


def groww_creds_present(env: Mapping[str, str] | None = None) -> bool:
    """Return ``True`` iff Groww credentials resolve from env vars OR the saved file.

    Delegates to :func:`brutex.config.credentials.credentials_present`
    (DEC-CREDENTIALS-STORE-001), so a one-time ``brutex auth set`` (or the
    dashboard Credentials page) now satisfies the gate without re-exporting
    ``BRUTEX_GROWW_*`` every session. Mirrors the per-field resolution of
    :meth:`brutex.adapters.groww.auth.GrowwAuth.from_env`
    (``BRUTEX_GROWW_API_KEY`` AND ``BRUTEX_GROWW_TOTP_SECRET``). When
    neither source provides both fields a live pull is impossible, so the
    dashboard pull route forces ``dry_run`` and serves synthetic data
    offline.

    Parameters
    ----------
    env
        Environment mapping to inspect. Defaults to
        :data:`os.environ`. Injected by tests to exercise both branches
        without mutating the real process environment.

    Returns
    -------
    bool
        ``True`` only when both credentials resolve (from env OR the saved
        file) AND are non-empty.
    """
    from brutex.config.credentials import credentials_present

    return credentials_present(env)


#: A factory that builds the ``(fetcher, reauth)`` pair given the lake
#: root. The default builds the offline synthetic pair; the live path
#: injects :func:`_build_live_pull_fetcher`.
FetcherFactory = Callable[[Path], tuple[CandleFetcher, Callable[[], None] | None]]


@dataclass(frozen=True, slots=True)
class TimeframeResult:
    """Per-timeframe completed / failed / no-data / skipped counts for one pull."""

    interval: str
    completed: int
    failed: int
    no_data: int = 0
    skipped: int = 0


@dataclass(slots=True)
class PullCandlesReport:
    """Result of a single :func:`pull_candles` call.

    Attributes
    ----------
    symbol
        The Groww symbol that was pulled — a spot index/stock
        (e.g. ``"NSE-NIFTY"``), a future, or an option contract's
        groww_symbol (e.g. ``"NSE-NIFTY-30Jan25-28500-CE"``).
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

    @property
    def total_no_data(self) -> int:
        """Total chunks the vendor reported empty (NO_DATA) across all timeframes."""
        return sum(tf.no_data for tf in self.timeframes)

    @property
    def total_skipped(self) -> int:
        """Total chunks skipped as already-settled (resume) across all timeframes."""
        return sum(tf.skipped for tf in self.timeframes)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly dict for the ``/api/v1/universe/pull`` route."""
        return {
            "symbol": self.symbol,
            "year": self.year,
            "dry_run": self.dry_run,
            "total_completed": self.total_completed,
            "total_failed": self.total_failed,
            "total_no_data": self.total_no_data,
            "total_skipped": self.total_skipped,
            "timeframes": [
                {
                    "interval": tf.interval,
                    "completed": tf.completed,
                    "failed": tf.failed,
                    "no_data": tf.no_data,
                    "skipped": tf.skipped,
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


def pull_candles(
    symbol: str,
    *,
    lake_root: Path,
    year: int | None = None,
    dry_run: bool,
    fetcher_factory: FetcherFactory | None = None,
) -> PullCandlesReport:
    """Ingest *symbol*'s spot / future / option candles for *year*.

    Instrument-agnostic: *symbol* may be a spot index/stock, a future,
    or an option contract's groww_symbol — the chunk/fetch spine is the
    same for any Groww symbol. Plans + drives one
    :func:`~brutex.adapters.groww.ingest.run_ingest` pass over the single
    *symbol* and ALL
    :data:`~brutex.adapters.groww.chunks.ALL_GROWW_INTERVALS`, covering
    ``[Jan 1 year, Jan 1 year+1)`` in UTC. Idempotent + resumable via a
    fresh :class:`~brutex.data.watermark.FetchWatermark`.

    Parameters
    ----------
    symbol
        Groww symbol — a spot index/stock (e.g. ``"NSE-NIFTY"`` /
        ``"NSE-RELIANCE"``), a future, or an option contract's
        groww_symbol (e.g. ``"NSE-NIFTY-30Jan25-28500-CE"``).
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
    PullCandlesReport
        Per-timeframe completed / failed counts + the ``dry_run`` flag.

    Raises
    ------
    ValueError
        If *symbol* is empty / blank, or does not start with a known
        Groww exchange prefix (:data:`_KNOWN_EXCHANGE_PREFIXES` —
        ``NSE-`` / ``BSE-``). The latter guards against a bare
        ``trading_symbol`` (e.g. ``"NIFTY"``) reaching the ingest spine,
        which would mis-derive the exchange in
        :func:`brutex.adapters.groww.client.symbol_to_exchange_segment`.
    """
    if not symbol.strip():
        raise ValueError("symbol must be a non-empty Groww symbol")
    if not symbol.startswith(_KNOWN_EXCHANGE_PREFIXES):
        raise ValueError(
            "symbol must be an exchange-prefixed Groww symbol "
            f"(one of {_KNOWN_EXCHANGE_PREFIXES}), got {symbol!r}"
        )

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
            no_data=report.outcomes[symbol, interval].no_data,
            skipped=report.outcomes[symbol, interval].skipped,
        )
        for interval in ALL_GROWW_INTERVALS
    ]
    pull_report = PullCandlesReport(
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
    "PullCandlesReport",
    "TimeframeResult",
    "groww_creds_present",
    "pull_candles",
]
