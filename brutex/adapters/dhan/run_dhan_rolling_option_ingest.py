"""Dhan rolling-ATM option ingest — pull the ATM-relative series into ``feeds/dhan/``.

DEC-DHAN-ROLLING-OPTIONS-001 (ADR-0022). The lean rolling-ATM option spine: enumerate
the PURE cartesian of 1008 rolling keys (:func:`~brutex.adapters.dhan.rolling_option_\
plan.enumerate_rolling_option_keys`), tile each key's range into 30-day windows, fetch
each via the COMMON :meth:`~brutex.adapters.dhan.rolling_option_adapter.\
RollingOptionAdapter.fetch_rolling_option`, and merge into the honest rolling-ATM lake
(:class:`~brutex.adapters.dhan.rolling_option_writer.RollingOptionWriter`, keyed by
``timestamp`` keep-first). Each key IS the partition / dedup identity — NO metadata
phase (Dhan serves no chain), NO ``get_expiries`` / ``get_contracts``.

**Honest framing (mirrors the spot spine, DEC-DHAN-SPOT-PULL-001).** This is the LEAN,
single-threaded spine: the in-memory per-window :class:`RollingOptionWindowOutcome`\\ s
ARE the record — it uses **NO** :class:`~brutex.data.window_ledger.WindowLedger` and
pre-seeds NOTHING durable this pass. The per-key attribution namespace
``_MARKET="dhan-option"`` (:data:`~brutex.adapters.dhan.rolling_option_constants.\
DHAN_ROLLING_COVERAGE_MARKET`) is the prefix the feed binding already reserves — own
namespace so a rolling-option outcome never collides with a Groww / Dhan-spot /
Dhan-future window. The FULL Groww-parity spine (durable ledger, parallel fan-out,
``run_until_clean``) is deferred with the surface/dashboard work
(DEC-FEED-SPINE-GENERALIZE-001).

**Two honest empty states (§7 gate).** A key that returns zero bars is EMPTY-FROM-DHAN
— a clean SUCCESS with zero bars, EXPECTED for wing offsets / SENSEX-WEEK / too-old
spans (NOT flagged suspicious — unlike the spot all-empty scan, an empty rolling key is
the norm). A DH-905 whole-track servability refusal (UNVERIFIED) surfaces as a
per-window FAILED with the DH-905 error type + zero bars, re-pullable. Neither is ever
a fabricated bar.

**Terminal pull-stop carve-out (DEC-DHAN-ROLLING-TERMINAL-STOP-001).** A
:func:`~brutex.adapters.dhan.errors.is_terminal_pull_stop` failure — terminal auth,
a still-stale SSM token after the re-read, an inactive Data-API subscription — means
every later window of this pull would fail identically, so the FIRST occurrence
RE-RAISES past the per-window quarantine (the spot spine's exact behaviour,
``ingest.py``): the remaining windows are never burned against the 100k/day quota and
the job layer's ``is_pull_halt`` fires the CRITICAL Telegram pull-halt alert.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Final

import structlog

from brutex.adapters.dhan.errors import DhanError, is_retryable, is_terminal_pull_stop
from brutex.adapters.dhan.rolling_option_adapter import RollingOptionAdapter
from brutex.adapters.dhan.rolling_option_chunks import window_days_for_rolling_interval
from brutex.adapters.dhan.rolling_option_constants import (
    DHAN_ROLLING_COVERAGE_MARKET,
    DHAN_ROLLING_FETCH_INTERVAL,
)
from brutex.adapters.dhan.rolling_option_parser import RollingOptionBar
from brutex.adapters.dhan.rolling_option_plan import RollingOptionKey
from brutex.adapters.dhan.rolling_option_writer import RollingOptionWriter
from brutex.adapters.ingest_common import plan_windows
from brutex.data.option_parquet_writer import OptionSeriesCoordinate

_LOG = structlog.get_logger("brutex.adapters.dhan.run_dhan_rolling_option_ingest")

#: The per-key outcome / attribution namespace (``dhan-option`` — the binding's
#: reserved ``ledger_market_option`` prefix). NOT a durable ledger namespace this
#: pass (the lean spine uses in-memory outcomes — mirrors the spot ingest).
_MARKET: Final[str] = DHAN_ROLLING_COVERAGE_MARKET

#: Per-window outcome status vocabulary.
STATUS_SUCCESS: Final[str] = "success"
STATUS_FAILED: Final[str] = "failed"


def _noop_pace() -> None:
    """Default no-op pace hook (a real controller.pace is injected in production)."""


def _noop_backoff(_round_index: int) -> None:
    """Default no-op inter-round back-off (a test injects a recording one)."""


def _keep_all(bars: list[RollingOptionBar]) -> list[RollingOptionBar]:
    """Default session filter — keep every bar (identity).

    The vendor rolling series is already session-bounded, so the CORE does not
    re-filter (a calendar re-filter over :class:`RollingOptionBar` is a clean
    follow-up; the shared :func:`~brutex.data.session.filter_to_trading_session` is
    ``CandleBar``-typed, so it is not reused directly). Injectable so a production
    caller can supply a real filter.
    """
    return bars


def coordinate_for_key(key: RollingOptionKey) -> OptionSeriesCoordinate:
    """Build the REUSED :class:`OptionSeriesCoordinate` echo from a rolling key.

    The coordinate carries the seven series-addressing fields (``option_type`` +
    ``interval`` are the two path selectors passed alongside it to the writer). NOT
    a forked dataclass — the on-main coordinate already carries exactly these fields.
    """
    return OptionSeriesCoordinate(
        exch_id=key.exchange,
        exchange_segment=key.exchange_segment,
        security_id=key.security_id,
        underlying_symbol=key.underlying,
        expiry_flag=key.expiry_flag,
        expiry_code=key.expiry_code,
        strike_offset=key.strike_offset,
    )


@dataclass(frozen=True, slots=True)
class RollingOptionWorkItem:
    """One (key, window) unit of rolling-ATM option ingest work."""

    key: RollingOptionKey
    start: date
    end: date


@dataclass(frozen=True, slots=True)
class RollingOptionWindowOutcome:
    """The outcome of processing one :class:`RollingOptionWorkItem`."""

    key: RollingOptionKey
    start: date
    end: date
    status: str
    bars_appended: int
    retryable: bool = False
    error_type: str | None = None


@dataclass(frozen=True, slots=True)
class RollingOptionIngestReport:
    """The result of a Dhan rolling-ATM option ingest run.

    Attributes
    ----------
    outcomes
        One :class:`RollingOptionWindowOutcome` per processed window (latest per item).
    keys
        The rolling keys the run planned.
    market
        The per-key attribution namespace (``"dhan-option"``); NOT a durable ledger
        namespace (the lean spine uses no WindowLedger).
    """

    outcomes: tuple[RollingOptionWindowOutcome, ...]
    keys: tuple[RollingOptionKey, ...]
    market: str = _MARKET

    @property
    def total_bars(self) -> int:
        """Total NEW bars merged across all SUCCESS windows."""
        return sum(o.bars_appended for o in self.outcomes)

    @property
    def failed_windows(self) -> int:
        """Number of windows that ended FAILED (re-pullable)."""
        return sum(1 for o in self.outcomes if o.status == STATUS_FAILED)


def plan_rolling_option_work(
    *,
    keys: Sequence[RollingOptionKey],
    interval: str,
    start: date,
    end: date,
) -> list[RollingOptionWorkItem]:
    """Plan the ingest work: tile ``[start, end)`` into 30-day windows per key.

    ``[start, end)`` is half-open (the ``ingest_common`` tiling convention); an
    inclusive operator range passes ``end + 1 day`` at the surface. Every key gets
    the SAME window tiling (no per-key resolution — each key IS the identity).
    """
    windows = plan_windows(
        datetime.combine(start, time.min),
        datetime.combine(end, time.min),
        window_days=window_days_for_rolling_interval(interval),
    )
    work: list[RollingOptionWorkItem] = []
    for key in keys:
        for window in windows:
            work.append(
                RollingOptionWorkItem(
                    key=key,
                    # DateWindow bounds are ISO "YYYY-MM-DDTHH:MM:SS" strings.
                    start=date.fromisoformat(window.start[:10]),
                    end=date.fromisoformat(window.end[:10]),
                )
            )
    return work


def _process_item(
    item: RollingOptionWorkItem,
    *,
    adapter: RollingOptionAdapter,
    writer: RollingOptionWriter,
    interval: str,
    session_filter: Callable[[list[RollingOptionBar]], list[RollingOptionBar]],
    pace: Callable[[], None],
) -> RollingOptionWindowOutcome:
    """Fetch → session-filter → write one window; classify a Dhan failure honestly."""
    pace()
    try:
        raw_bars = adapter.fetch_rolling_option(key=item.key, start=item.start, end=item.end)
        kept = session_filter(raw_bars)
        added = writer.write(
            coordinate=coordinate_for_key(item.key),
            option_type=item.key.option_type,
            interval=interval,
            bars=kept,
        )
    except DhanError as exc:
        # The pull-wide global-stop carve-out (the SPOT spine's exact mirror —
        # ingest.py::_process_item, DEC-DHAN-ROLLING-TERMINAL-STOP-001): a terminal
        # auth / still-stale-token / subscription failure means EVERY later window of
        # this pull would fail identically, so the FIRST occurrence re-raises past the
        # per-window quarantine — the remaining ~1008-key window fan-out is never
        # burned against the 100k/day Dhan quota, and the job layer's is_pull_halt
        # fires the CRITICAL Telegram pull-halt alert (design §3,
        # DEC-DHAN-SPOT-FEED-001; the errors.py docstrings PROMISE this carve-out).
        if is_terminal_pull_stop(exc):
            raise
        retryable = is_retryable(exc)
        _LOG.warning(
            "dhan.rolling_option.window_failed",
            underlying=item.key.underlying,
            expiry_flag=item.key.expiry_flag,
            expiry_code=item.key.expiry_code,
            strike_offset=item.key.strike_offset,
            option_type=item.key.option_type,
            start=item.start.isoformat(),
            end=item.end.isoformat(),
            error_type=type(exc).__name__,
            retryable=retryable,
        )
        return RollingOptionWindowOutcome(
            key=item.key,
            start=item.start,
            end=item.end,
            status=STATUS_FAILED,
            bars_appended=0,
            retryable=retryable,
            error_type=type(exc).__name__,
        )
    return RollingOptionWindowOutcome(
        key=item.key,
        start=item.start,
        end=item.end,
        status=STATUS_SUCCESS,
        bars_appended=added,
    )


def run_dhan_rolling_option_ingest_with_retry(
    *,
    keys: Sequence[RollingOptionKey],
    adapter: RollingOptionAdapter,
    writer: RollingOptionWriter,
    start: date,
    end: date,
    interval: str = DHAN_ROLLING_FETCH_INTERVAL,
    session_filter: Callable[[list[RollingOptionBar]], list[RollingOptionBar]] = _keep_all,
    pace: Callable[[], None] = _noop_pace,
    max_rounds: int = 3,
    backoff: Callable[[int], None] = _noop_backoff,
) -> RollingOptionIngestReport:
    """Run the rolling-ATM option ingest with bounded in-run retry of retryable windows.

    Plans the work (:func:`plan_rolling_option_work`), then processes each window; a
    RETRYABLE failure (throttle / empty body / timeout) is re-processed in a later
    round up to *max_rounds*, and a PER-WINDOW terminal failure (a DH-905 servability
    refusal, vendor drift, unknown) is left FAILED, re-pullable by a future pull. A
    :func:`~brutex.adapters.dhan.errors.is_terminal_pull_stop` failure (terminal
    auth / still-stale SSM token / inactive Data-API subscription) PROPAGATES on its
    first occurrence — the whole pull stops instead of burning every remaining window
    (the job layer alerts it CRITICAL; the spot spine's exact behaviour,
    DEC-DHAN-ROLLING-TERMINAL-STOP-001). Returns the aggregated
    :class:`RollingOptionIngestReport`.

    Parameters
    ----------
    keys
        The rolling keys to pull (e.g.
        :func:`~brutex.adapters.dhan.rolling_option_plan.enumerate_rolling_option_keys`).
    adapter, writer
        The injected fetch adapter and rolling-ATM writer.
    start, end
        The half-open ``[start, end)`` operator range (``end`` exclusive).
    interval
        The fetch interval (``"1minute"`` — the only rolling interval).
    session_filter, pace, backoff
        Injected seams (default keep-all / no-op pace / no-op back-off); a production
        caller injects the controller's ``pace`` + a real back-off.
    max_rounds
        The bounded retry-round budget (``>= 1``).
    """
    work = plan_rolling_option_work(keys=keys, interval=interval, start=start, end=end)
    latest: dict[RollingOptionWorkItem, RollingOptionWindowOutcome] = {}
    pending = list(work)
    rounds = max(1, max_rounds)
    for round_index in range(rounds):
        if not pending:
            break
        next_pending: list[RollingOptionWorkItem] = []
        for item in pending:
            outcome = _process_item(
                item,
                adapter=adapter,
                writer=writer,
                interval=interval,
                session_filter=session_filter,
                pace=pace,
            )
            latest[item] = outcome
            if outcome.status == STATUS_FAILED and outcome.retryable:
                next_pending.append(item)
        pending = next_pending
        if pending and round_index < rounds - 1:
            backoff(round_index)
    report = RollingOptionIngestReport(
        outcomes=tuple(latest.values()),
        keys=tuple(keys),
    )
    _LOG.info(
        "dhan.rolling_option.summary",
        market=_MARKET,
        keys=len(report.keys),
        windows=len(report.outcomes),
        total_bars=report.total_bars,
        failed_windows=report.failed_windows,
    )
    return report


__all__ = [
    "STATUS_FAILED",
    "STATUS_SUCCESS",
    "RollingOptionIngestReport",
    "RollingOptionWindowOutcome",
    "RollingOptionWorkItem",
    "coordinate_for_key",
    "plan_rolling_option_work",
    "run_dhan_rolling_option_ingest_with_retry",
]
