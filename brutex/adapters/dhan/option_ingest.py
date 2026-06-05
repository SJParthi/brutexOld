"""Dhan rolling-option ingest — pull one expired-option series into the lake.

The thin orchestration the cli/dashboard call to fill the lake for ONE
expired-option series (one ``exchangeSegment`` / ``instrument`` /
``expiryFlag`` / ``expiryCode`` / ``strike`` (``ATM±N``) / ``drvOptionType``):
tile ``[start, end)`` into 30-day windows
(:func:`~brutex.adapters.dhan.chunks.plan_option_windows`), fetch each from
``/charts/rollingoption``, parse the per-side ``ce``/``pe`` columnar payload
(:func:`~brutex.adapters.dhan.option_parser.parse_rolling_option_sides`),
**session-filter** to ``[09:15, 15:30)`` IST
(:func:`~brutex.data.session.in_session` — the COMMON primitive, §11), and
append the :class:`~brutex.data.models.OptionBar` rows to an injected sink.

Track 1 pulls options at **1-minute only** (DEC-DHAN-1M-ONLY-001); higher
timeframes are resampled downstream, never fetched. Every documented field
is requested (``required_data`` defaults to the full set) so the lake loses
nothing (operator decree 2026-05-31 "capture EVERYTHING").

Both the JSON poster (the ``post_json`` surface of
:class:`~brutex.adapters.dhan.client.DhanClient`) and the sink are
**injected** :class:`~typing.Protocol` seams, so this is 100% unit-testable
without a network or a real lake. Pacing is injected too (``pace`` — the
orchestrator's token-bucket ``acquire`` hook), keeping the **5 req/s /
100k-day** limit honoured without coupling the limiter in here. A
**transient** (retryable) error on a window is counted and skipped (the
orchestrator re-queues it); a **terminal** error propagates.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Protocol

import structlog

from brutex.adapters.dhan.body import build_rolling_option_body
from brutex.adapters.dhan.chunks import plan_option_windows
from brutex.adapters.dhan.constants import ENDPOINT_ROLLING_OPTION, TRACK1_OPTIONS_INTERVAL
from brutex.adapters.dhan.errors import DhanError, is_rate_limit, is_retryable
from brutex.adapters.dhan.option_parser import parse_rolling_option_sides
from brutex.data.fetch_coverage import FetchCoverage, WindowKey
from brutex.data.models import OptionBar
from brutex.data.session import in_session
from brutex.observability.correlation import current_pull_id

#: Structured logger — lights the rolling-option ingest spine with per-window /
#: parse / lake-write / series events under the ``ingest.*`` namespace, each
#: carrying the pull's ``pull_id`` + the full series coordinate
#: (DEC-DHAN-INGEST-LOGGING-001). All DEBUG so a 16k-series fan-out never floods
#: INFO; the per-underlying rollup at INFO lives in ``option_universe_ingest``.
_LOG = structlog.get_logger("brutex.adapters.dhan.option_ingest")

#: Pacing / rate-feedback hook — a no-arg callback (e.g. token-bucket acquire, or
#: a rate-controller step). ``pace`` runs once before each window fetch; the
#: ``on_rate_limit`` / ``on_timeout`` / ``on_success`` feedback hooks mirror the
#: SPOT path's identical names + bound-method wiring (CLAUDE.md §11 COMMON,
#: ``ingest.ingest_spot``) so the ONE shared rate governor adapts the options
#: spine too (DEC-DHAN-TIMEOUT-BACKOFF-001): ``on_rate_limit`` on a swallowed 429,
#: ``on_timeout`` on a swallowed timeout / transport / 5xx (a slow, struggling
#: Dhan), ``on_success`` once per cleanly-fetched window. All default ``None`` so
#: the dry-run / synthetic path stays feedback-free.
Pacer = Callable[[], None]

#: Default ``requiredData`` — every documented field, so the lake keeps the
#: full option record (OHLC + iv + volume + oi + resolved strike + spot).
ALL_REQUIRED_DATA: tuple[str, ...] = (
    "open",
    "high",
    "low",
    "close",
    "volume",
    "oi",
    "strike",
    "spot",
    "iv",
)


class JsonPoster(Protocol):
    """The ``post_json`` slice of :class:`DhanClient` the ingest needs."""

    def post_json(self, endpoint: str, body: Mapping[str, object]) -> dict[str, object]:
        """POST *body* to *endpoint* and return the parsed JSON object."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed


class OptionBarSink(Protocol):
    """Write-side seam for parsed :class:`OptionBar` rows (one series)."""

    def append_option_bars(
        self,
        *,
        security_id: str,
        exch_id: str,
        underlying_symbol: str,
        exchange_segment: str,
        expiry_flag: str,
        expiry_code: int,
        strike: str,
        drv_option_type: str,
        bars: Sequence[OptionBar],
    ) -> int:
        """Persist *bars* for this series; return the count appended."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed


#: Namespace stamped on every option coverage key's ``market`` field.
_COVERAGE_MARKET: str = "dhan-option"


@dataclass(frozen=True, slots=True)
class OptionIngestReport:
    """Outcome of an :func:`ingest_rolling_option_series` run."""

    windows: int
    bars_appended: int
    transient_failures: int
    skipped_windows: int = 0


def _coverage_key(
    *,
    exchange_segment: str,
    security_id: str,
    expiry_flag: str,
    expiry_code: int,
    strike: str,
    drv_option_type: str,
    window_start: date,
    window_end: date,
) -> WindowKey:
    """Build the O(1) coverage key for one rolling-option fetch window.

    The full series coordinate (flag / code / strike / type) is folded into the
    ``interval`` field so two different series never collide and the reusable
    :class:`~brutex.data.fetch_coverage.WindowKey` stays the single COMMON key
    type (CLAUDE.md §11).
    """
    series = f"OPT|{expiry_flag}|{expiry_code}|{strike}|{drv_option_type}"
    return WindowKey(
        market=_COVERAGE_MARKET,
        security_id=security_id + "|" + exchange_segment,
        interval=series,
        start=window_start,
        end=window_end,
    )


def _window_dates(start: date, end: date) -> list[tuple[date, date]]:
    """Tile ``[start, end)`` into 30-day rolling-option fetch windows."""
    planned = plan_option_windows(datetime.combine(start, time()), datetime.combine(end, time()))
    return [(date.fromisoformat(w.start[:10]), date.fromisoformat(w.end[:10])) for w in planned]


def ingest_rolling_option_series(
    client: JsonPoster,
    sink: OptionBarSink,
    *,
    security_id: str,
    exch_id: str,
    underlying_symbol: str,
    exchange_segment: str,
    instrument: str,
    expiry_flag: str,
    expiry_code: int,
    strike: str,
    drv_option_type: str,
    start: date,
    end: date,
    required_data: Sequence[str] = ALL_REQUIRED_DATA,
    pace: Pacer | None = None,
    on_rate_limit: Pacer | None = None,
    on_timeout: Pacer | None = None,
    on_success: Pacer | None = None,
    coverage: FetchCoverage | None = None,
) -> OptionIngestReport:
    """Pull one expired-option series' 1-minute OHLC over ``[start, end)``.

    Parameters
    ----------
    client, sink
        The JSON poster (Dhan client) and the injected :class:`OptionBar`
        write sink.
    security_id
        The **underlying** Dhan ``SECURITY_ID`` (rolling option resolves
        strikes relative to this underlying's spot).
    exch_id, underlying_symbol
        The underlying's exchange id (``"NSE"`` / ``"BSE"``) and engine symbol
        (``"NIFTY"``) — promoted onto every persisted bar as real
        expired-options columns (DEC-EXPIRED-OPTIONS-TABLE-001).
    exchange_segment, instrument
        e.g. ``"NSE_FNO"`` and ``"OPTIDX"`` / ``"OPTSTK"``.
    expiry_flag, expiry_code
        ``"WEEK"`` / ``"MONTH"`` and the expiry selector.
    strike
        Relative strike (``"ATM"``, ``"ATM+1"`` … per the rolling-option doc).
    drv_option_type
        ``"CALL"`` or ``"PUT"``.
    start, end
        Half-open date range.
    required_data
        Fields to request — defaults to :data:`ALL_REQUIRED_DATA`.
    pace
        Optional per-window pacing hook (rate-limit acquire).

    Returns
    -------
    OptionIngestReport
        Window count, total bars appended (session-filtered), and
        transient-failure count.

    Raises
    ------
    DhanError
        On a terminal (non-retryable) fetch error.
    """
    bars_appended = 0
    transient = 0
    skipped = 0
    windows = _window_dates(start, end)
    # Read the pull's correlation id ONCE (the controller bound it via
    # ``pull_id_scope``); stamp it + the full series coordinate on every
    # ``ingest.*`` event so one pull greps end-to-end. Observability only — no
    # behaviour change (DEC-DHAN-INGEST-LOGGING-001).
    pull_id = current_pull_id()
    series_fields: dict[str, object] = {
        "security_id": security_id,
        "expiry_flag": expiry_flag,
        "expiry_code": expiry_code,
        "strike": strike,
        "drv_option_type": drv_option_type,
    }
    for window_start, window_end in windows:
        key = _coverage_key(
            exchange_segment=exchange_segment,
            security_id=security_id,
            expiry_flag=expiry_flag,
            expiry_code=expiry_code,
            strike=strike,
            drv_option_type=drv_option_type,
            window_start=window_start,
            window_end=window_end,
        )
        # Skip a window already pulled — no wasted Dhan hit (DEC-FETCH-COVERAGE-001).
        if coverage is not None and coverage.is_complete(key):
            skipped += 1
            _LOG.debug(
                "ingest.window",
                pull_id=pull_id,
                window_start=window_start.isoformat(),
                window_end=window_end.isoformat(),
                action="skipped",
                **series_fields,
            )
            continue
        if pace is not None:
            pace()
        body = build_rolling_option_body(
            security_id=security_id,
            exchange_segment=exchange_segment,
            instrument=instrument,
            expiry_flag=expiry_flag,
            expiry_code=expiry_code,
            strike=strike,
            drv_option_type=drv_option_type,
            required_data=required_data,
            from_date=datetime.combine(window_start, time()),
            to_date=datetime.combine(window_end, time()),
            interval=TRACK1_OPTIONS_INTERVAL,
        )
        try:
            payload = client.post_json(ENDPOINT_ROLLING_OPTION, body)
        except DhanError as exc:
            if is_retryable(exc):
                transient += 1
                # Feed the swallowed transient to the rate governor so it adapts
                # the options spine (mirrors the spot path's routing,
                # ``ingest.ingest_spot``): a 429 → the 429 ladder; a timeout /
                # transport / 5xx → the GENTLE timeout ladder, so a SUSTAINED
                # storm (the operator's long-pull `DhanTimeoutError` cascade)
                # eases the rate instead of grinding at 5 r/s into a struggling
                # Dhan (DEC-DHAN-TIMEOUT-BACKOFF-001). A one-off blip is ignored
                # by the controller's threshold → steady throughput unchanged.
                if is_rate_limit(exc):
                    if on_rate_limit is not None:
                        on_rate_limit()
                elif on_timeout is not None:
                    on_timeout()
                continue
            raise
        # A clean window is the recover-up signal for the rate governor.
        if on_success is not None:
            on_success()
        data = payload.get("data")
        source = data if isinstance(data, Mapping) else payload
        # Options are intraday 1m → drop pre-open / post-close before the lake.
        parsed = parse_rolling_option_sides(source)
        bars = [bar for bar in parsed if in_session(bar.timestamp)]
        # Parse nook: rows decoded from the per-side ce/pe payload vs how many
        # survived the [09:15,15:30) IST session gate.
        _LOG.debug(
            "ingest.parse",
            pull_id=pull_id,
            raw_bars=len(parsed),
            session_bars=len(bars),
            **series_fields,
        )
        appended = sink.append_option_bars(
            security_id=security_id,
            exch_id=exch_id,
            underlying_symbol=underlying_symbol,
            exchange_segment=exchange_segment,
            expiry_flag=expiry_flag,
            expiry_code=expiry_code,
            strike=strike,
            drv_option_type=drv_option_type,
            bars=bars,
        )
        bars_appended += appended
        _LOG.debug(
            "ingest.window",
            pull_id=pull_id,
            window_start=window_start.isoformat(),
            window_end=window_end.isoformat(),
            action="fetched",
            **series_fields,
        )
        # Lake-write nook: option bars actually persisted for this window.
        _LOG.debug("ingest.lake_write", pull_id=pull_id, bars_appended=appended, **series_fields)
        # Mark the window pulled only after a clean fetch + append.
        if coverage is not None:
            coverage.mark_complete(key)
    # Per-series summary (one fanned-out series) — DEBUG; the per-underlying
    # rollup at INFO is emitted by ``run_dhan_options_ingest`` one layer up.
    _LOG.debug(
        "ingest.series",
        pull_id=pull_id,
        windows=len(windows),
        bars_appended=bars_appended,
        transient_failures=transient,
        skipped_windows=skipped,
        **series_fields,
    )
    return OptionIngestReport(
        windows=len(windows),
        bars_appended=bars_appended,
        transient_failures=transient,
        skipped_windows=skipped,
    )


__all__ = [
    "ALL_REQUIRED_DATA",
    "JsonPoster",
    "OptionBarSink",
    "OptionIngestReport",
    "Pacer",
    "ingest_rolling_option_series",
]
