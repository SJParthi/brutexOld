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
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Final

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

#: Earliest calendar date Groww serves historical candles for equities,
#: indices and F&O (CLAUDE.md §6 "Historical depth — From 2020"). Any
#: ``from_date`` below this floor is clamped up to it so a too-early pick
#: can never waste quota on a window the vendor has no data for.
GROWW_HISTORY_FLOOR: Final[date] = date(2020, 1, 1)


def resolve_candle_range(
    *,
    from_date: str | None,
    to_date: str | None,
    year: int | None,
    now: datetime,
) -> tuple[datetime, datetime]:
    """Resolve the half-open ``[start, end)`` UTC window a pull must cover.

    O(1): pure date arithmetic, no I/O. Three modes, in priority order:

    1. **Explicit range** — when *from_date* and/or *to_date* is given.
       Each is an ISO ``YYYY-MM-DD`` calendar date; both ends are
       **day-inclusive** (``to_date``'s whole day is fetched, so *end* is
       the midnight that starts the next day). A missing *from_date*
       defaults to :data:`GROWW_HISTORY_FLOOR`; a missing *to_date*
       defaults to today (``now``'s UTC date). The pair is then clamped to
       ``[GROWW_HISTORY_FLOOR, today]`` so a too-early or future pick can
       never waste quota — and the future is never requested.
    2. **Year** — when neither date is given but *year* is: that full
       calendar year, **clamped to ``[GROWW_HISTORY_FLOOR, today]``** like
       mode 1. A past year resolves to the whole year; the current year
       stops at today (never the future); a wholly-future or pre-2020 year
       raises "nothing to pull".
    3. **Default** — neither date nor year: the current UTC calendar year,
       same clamp as mode 2 (so it runs ``[Jan 1, today]``, never ahead).

    Parameters
    ----------
    from_date, to_date
        Inclusive ISO ``YYYY-MM-DD`` bounds, or ``None``.
    year
        Calendar year, used only when both dates are ``None``.
    now
        The reference "current" instant (injected for testability). Its
        UTC date is the upper clamp and the default-year source.

    Returns
    -------
    tuple[datetime, datetime]
        ``(start, end)`` — timezone-aware UTC, half-open, ``start < end``.

    Raises
    ------
    ValueError
        If a date string is not ISO ``YYYY-MM-DD``, or if the resolved
        ``from`` is after the resolved ``to`` (e.g. a future *from_date*,
        or ``from_date > to_date``).
    """
    # Normalise empty / whitespace-only inputs to ``None`` so a bare
    # ``?from_date=`` query param (or a blank picker) behaves exactly like
    # an omitted bound, not as a malformed date — one obvious semantics for
    # "no bound given" across the UI and any direct API caller.
    from_date = (from_date or "").strip() or None
    to_date = (to_date or "").strip() or None

    today = now.astimezone(UTC).date()

    if from_date is None and to_date is None:
        # Mode 2/3 — an explicit *year* (or, absent that, the current UTC
        # year) → that full calendar year as a date pair, which then falls
        # through the SAME clamp as the explicit-range mode below. This
        # branch used to return ``[year-01-01, (year+1)-01-01)`` UNCLAMPED —
        # so the current or a future year produced a window whose tail ran
        # past today, and the vendor rejected the very first future chunk
        # with "Invalid start time. Start time cannot be in the future",
        # crashing the whole pull (DEC-PULL-FUTURE-WINDOW-001). Routing it
        # through the clamp makes the current year stop at today and a
        # wholly-future / pre-2020 year raise "nothing to pull" instead.
        resolved_year = year if year is not None else now.year
        start_date = date(resolved_year, 1, 1)
        end_date = date(resolved_year, 12, 31)
    else:
        start_date = _parse_iso_date(from_date, "from_date") if from_date else GROWW_HISTORY_FLOOR
        end_date = _parse_iso_date(to_date, "to_date") if to_date else today

    # Clamp into the vendor-served window; never request the future. Applies
    # to EVERY mode now (the year/default branch above was previously the one
    # unclamped path — the live-crash root cause).
    start_date = max(start_date, GROWW_HISTORY_FLOOR)
    end_date = min(end_date, today)
    if start_date > end_date:
        raise ValueError(
            f"from_date ({start_date.isoformat()}) is after to_date "
            f"({end_date.isoformat()}) — nothing to pull"
        )

    start = datetime(start_date.year, start_date.month, start_date.day, tzinfo=UTC)
    # End is exclusive; add a day so the whole of ``end_date`` is fetched.
    end = datetime(end_date.year, end_date.month, end_date.day, tzinfo=UTC) + timedelta(days=1)
    return start, end


def _parse_iso_date(value: str, field_name: str) -> date:
    """Parse an ISO ``YYYY-MM-DD`` string, re-raising with the field name."""
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be ISO YYYY-MM-DD, got {value!r}") from exc


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
        The calendar year the pull *started* in. For a full-year pull this
        is the year covered; for an explicit ``from``/``to`` range it is
        ``start_date``'s year (kept for backward compatibility — read
        ``start_date`` / ``end_date`` for the exact window).
    start_date, end_date
        The inclusive ISO ``YYYY-MM-DD`` bounds actually pulled, after the
        :func:`resolve_candle_range` clamp. The ``/universe`` page shows
        these so the operator sees the real window, not just the year.
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
    start_date: str = ""
    end_date: str = ""
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
            "start_date": self.start_date,
            "end_date": self.end_date,
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
    from_date: str | None = None,
    to_date: str | None = None,
    dry_run: bool,
    fetcher_factory: FetcherFactory | None = None,
) -> PullCandlesReport:
    """Ingest *symbol*'s spot / future / option candles over a date range.

    Instrument-agnostic: *symbol* may be a spot index/stock, a future,
    or an option contract's groww_symbol — the chunk/fetch spine is the
    same for any Groww symbol. Plans + drives one
    :func:`~brutex.adapters.groww.ingest.run_ingest` pass over the single
    *symbol* and ALL
    :data:`~brutex.adapters.groww.chunks.ALL_GROWW_INTERVALS`. The window
    is resolved by :func:`resolve_candle_range`: an explicit
    *from_date* / *to_date* range (day-inclusive, clamped to
    ``[2020-01-01, today]``) when either is given, else the full calendar
    *year*. Idempotent + resumable via a fresh
    :class:`~brutex.data.watermark.FetchWatermark`.

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
        Calendar year to pull when no explicit range is given. ``None``
        (default) uses the current UTC year. Ignored when *from_date* or
        *to_date* is supplied.
    from_date, to_date
        Inclusive ISO ``YYYY-MM-DD`` bounds for an explicit range. Either
        may be ``None`` (defaults to the Groww history floor / today
        respectively). When both are ``None`` the *year* mode applies.
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
        Also raised by :func:`resolve_candle_range` for a malformed date
        string or a ``from`` after ``to`` (incl. a future ``from_date``).
    """
    if not symbol.strip():
        raise ValueError("symbol must be a non-empty Groww symbol")
    if not symbol.startswith(_KNOWN_EXCHANGE_PREFIXES):
        raise ValueError(
            "symbol must be an exchange-prefixed Groww symbol "
            f"(one of {_KNOWN_EXCHANGE_PREFIXES}), got {symbol!r}"
        )

    start, end = resolve_candle_range(
        from_date=from_date,
        to_date=to_date,
        year=year,
        now=datetime.now(UTC),
    )
    resolved_year = start.year
    # Inclusive end label = the last day actually fetched (end is exclusive).
    end_label = (end - timedelta(days=1)).date().isoformat()

    if fetcher_factory is None:
        fetcher_factory = _build_dry_run_fetcher if dry_run else _build_live_pull_fetcher
    fetcher, reauth = fetcher_factory(lake_root)

    _LOG.info(
        "universe.pull.start",
        symbol=symbol,
        year=resolved_year,
        start_date=start.date().isoformat(),
        end_date=end_label,
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
        start_date=start.date().isoformat(),
        end_date=end_label,
        dry_run=dry_run,
        timeframes=timeframes,
    )
    _LOG.info(
        "universe.pull.done",
        symbol=symbol,
        year=resolved_year,
        start_date=start.date().isoformat(),
        end_date=end_label,
        dry_run=dry_run,
        completed=pull_report.total_completed,
        failed=pull_report.total_failed,
    )
    return pull_report


__all__ = [
    "GROWW_HISTORY_FLOOR",
    "FetcherFactory",
    "PullCandlesReport",
    "TimeframeResult",
    "groww_creds_present",
    "pull_candles",
    "resolve_candle_range",
]
