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
100k-day** limit honoured without coupling the limiter in here.

## Resume-cursor / transactional commit (DEC-OPTION-429-RECOVERY-001, audit Gap #2)

The series' 30-day windows are the **cursors** of a multi-page option chain.
The earlier spine **deferred** a transient (429 / 5xx / timeout) cursor to the
next pull and kept fetching the rest — so a 429 mid-pagination left the
partition with a HOLE (a missing page). Keep-first dedup deduplicates a
duplicate timestamp on a re-pull, but it can NEVER fill a missing page, so the
lake could sit PARTIAL until the operator manually re-ran (audit Gap #2). This
spine closes that gap the way the spot path does (DEC-DHAN-429-RETRY-001 +
``until_clean``), with one stronger invariant for the option partition:

* **Resume the cursor** — a RETRYABLE error (429 / 5xx / timeout / transport)
  on a cursor is re-fetched IN-RUN, after a bounded seconds-scale backoff, up to
  ``max_retry_passes`` extra attempts. By then the opening 429 burst is over and
  the rate governor has stepped down, so the retry waits the limiter out rather
  than fighting it (mirrors the spot DEC-DHAN-429-RETRY-001 rationale).
* **Complete OR empty, never partial** — bars are STAGED per window and the sink
  is written + coverage marked only in a final commit pass AFTER every cursor has
  been fetched cleanly. If a cursor still fails after its retry budget (or a
  TERMINAL error raises mid-fetch), the whole series ABORTS having committed
  NOTHING — the partition stays EMPTY and the un-committed windows are deferred
  to the next pull (coverage untouched ⇒ a re-pull re-fetches them). So the
  partition the lake holds for a series is always whole or absent, never a
  page-holed partial.

**Honest envelope (Golden Rule #10):** the staging buffer is
O(session-bars in the series) SPACE, NOT O(1) — an all-or-nothing commit cannot
be O(1) space without a sink-side rollback (out of this path's scope). The O(1)
guarantees that DO hold are per-bar: the coverage probe, the dedup probe, and
the coverage key build are each O(1). The retry COUNT is bounded by
``max_retry_passes`` (never infinite) and every fetch (initial + retry) is paced
by the SAME ``≤ 5 r/s`` bucket (Rule #6); a token death / terminal error is
non-retryable and raises straight out.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from time import sleep as _time_sleep
from typing import Protocol

import structlog

from brutex.adapters.dhan.body import build_rolling_option_body
from brutex.adapters.dhan.chunks import plan_option_windows
from brutex.adapters.dhan.constants import ENDPOINT_ROLLING_OPTION, TRACK1_OPTIONS_INTERVAL
from brutex.adapters.dhan.errors import DhanError, is_no_data_error, is_rate_limit, is_retryable
from brutex.adapters.dhan.option_parser import parse_rolling_option_sides
from brutex.data.fetch_coverage import FetchCoverage, WindowKey
from brutex.data.models import OptionBar
from brutex.data.session import in_session
from brutex.data.window_ledger import WindowLedger
from brutex.observability.correlation import current_pull_id

#: Structured logger — lights the rolling-option ingest spine with per-window /
#: parse / lake-write / series events under the ``ingest.*`` namespace, each
#: carrying the pull's ``pull_id`` + the full series coordinate
#: (DEC-DHAN-INGEST-LOGGING-001). All DEBUG so a 16k-series fan-out never floods
#: INFO; the per-underlying rollup at INFO lives in ``option_universe_ingest``.
_LOG = structlog.get_logger("brutex.adapters.dhan.option_ingest")

#: Pacing / rate-feedback hook — a no-arg callback (e.g. token-bucket acquire, or
#: a rate-controller step). ``pace`` runs once before each window fetch (and once
#: before each in-run RETRY of that window, so a resume stays bucket-bounded); the
#: ``on_rate_limit`` / ``on_success`` feedback hooks REUSE the SPOT path's hook
#: names + bound-method wiring (CLAUDE.md §11 COMMON, ``ingest.ingest_spot``),
#: and ``on_timeout`` EXTENDS that pattern — the spot path has no timeout hook
#: (yet) — so the ONE shared rate governor adapts the options spine too
#: (DEC-DHAN-TIMEOUT-BACKOFF-001): ``on_rate_limit`` on a swallowed 429,
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

#: In-run transient-cursor auto-retry budget (DEC-OPTION-429-RECOVERY-001) — the
#: options analog of the spot path's in-run window retry (DEC-DHAN-WINDOW-RETRY-001
#: / DEC-DHAN-429-RETRY-001). A cursor (30-day window) that returns a RETRYABLE
#: error (429 / 5xx / timeout / transport) is re-fetched in-run up to this many
#: EXTRA attempts before the staged series is committed, so a 429 mid-pagination
#: is RESUMED to completion rather than left page-holed. ``0`` disables the retry
#: (single attempt then abort). Small + finite (Golden Rule #10 — never infinite).
_DEFAULT_MAX_RETRY_PASSES: int = 2

#: Base backoff (seconds) before an in-run cursor retry — SECONDS-scale (vs the
#: client's sub-second in-call hygiene) so a multi-second 429 storm / network
#: outage has time to clear between attempts. Retry *k* (1-based) waits
#: ``base × 2**(k-1)`` clamped to :data:`_MAX_RETRY_BACKOFF_S`.
_DEFAULT_RETRY_BACKOFF_S: float = 1.0

#: Hard ceiling (seconds) on a single retry backoff (defends an operator-set large
#: budget from driving the backoff toward an unbounded sleep).
_MAX_RETRY_BACKOFF_S: float = 8.0


def _default_retry_backoff(attempt_index: int) -> None:
    """Sleep the bounded exponential backoff before in-run retry ``attempt_index`` (0-based).

    Injected as the default ``retry_backoff`` hook so production waits a real,
    recovery-scale gap between attempts while tests pass a no-op recorder (no real
    sleep) — the same inject-the-sleep pattern as the spot path's
    :func:`~brutex.adapters.dhan.ingest._default_retry_backoff`.
    """
    _time_sleep(min(_DEFAULT_RETRY_BACKOFF_S * (2.0**attempt_index), _MAX_RETRY_BACKOFF_S))


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
    retried_windows: int = 0
    """Distinct cursors re-attempted by the in-run resume-cursor retry.

    DEC-OPTION-429-RECOVERY-001. A window that returned a retryable error on an
    attempt and was re-fetched is counted once here, whether or not the retry
    ultimately succeeded. Zero when no cursor failed transiently (or
    ``max_retry_passes=0``).
    """

    still_failed_windows: int = 0
    """Cursors NOT committed because the series aborted (a cursor outran its retry budget).

    On an aborted series the partition is committed all-or-nothing as EMPTY, so
    EVERY non-skipped window is deferred to the next pull (never marked complete —
    the no-skip guarantee); this equals that deferred count. Equals
    :attr:`transient_failures` (the same residual, under an explicit name mirroring
    the spot :class:`~brutex.adapters.dhan.ingest.IngestReport`). Zero on a fully
    committed (complete) series.
    """

    @property
    def complete(self) -> bool:
        """True iff the series committed (no residual) — the partition is WHOLE.

        The transactional invariant (DEC-OPTION-429-RECOVERY-001): a series either
        commits every window (``complete``) or commits nothing (``still_failed_windows
        > 0``). Never a page-holed partial.
        """
        return self.still_failed_windows == 0


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
    ledger: WindowLedger | None = None,
    max_retry_passes: int = _DEFAULT_MAX_RETRY_PASSES,
    retry_backoff: Callable[[int], None] | None = None,
) -> OptionIngestReport:
    """Pull one expired-option series' 1-minute OHLC over ``[start, end)`` — transactionally.

    The series' 30-day windows are the cursors of a multi-page option chain. They
    are fetched into a STAGING buffer with a bounded in-run resume-cursor retry on
    transient (429 / 5xx / timeout) errors, then written to *sink* + marked in
    *coverage* in a single COMMIT pass only after EVERY cursor has been fetched
    cleanly. If any cursor outruns its retry budget (or a terminal error raises),
    the whole series aborts having committed NOTHING — so the partition the lake
    holds is always complete or empty, never a page-holed partial
    (DEC-OPTION-429-RECOVERY-001, audit Gap #2).

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
        Optional per-window pacing hook (rate-limit acquire). Fired before the
        initial fetch AND before each in-run retry of a cursor, so a resume stays
        bucket-bounded (``≤ 5 r/s``, Rule #6).
    on_rate_limit, on_timeout, on_success
        Optional rate-governor feedback hooks (DEC-DHAN-TIMEOUT-BACKOFF-001).
        ``on_rate_limit`` fires on each swallowed 429, ``on_timeout`` on each
        swallowed timeout / transport / 5xx, ``on_success`` once per cleanly-fetched
        cursor (NOT on a skip or a still-failing attempt).
    coverage
        Optional :class:`~brutex.data.fetch_coverage.FetchCoverage` skip-ledger.
        A window already complete is skipped before the vendor call; a window is
        marked complete only in the COMMIT pass, so coverage advances all-or-nothing
        with the partition (DEC-FETCH-COVERAGE-001).
    max_retry_passes
        In-run resume-cursor retry budget (DEC-OPTION-429-RECOVERY-001). A cursor
        that returns a retryable error is re-fetched up to this many EXTRA attempts
        after a bounded backoff (default :data:`_DEFAULT_MAX_RETRY_PASSES`). ``0``
        disables the retry (single attempt then abort). A cursor still failing after
        the budget aborts the series (commit nothing — empty, never partial).
    retry_backoff
        Injected ``(attempt_index) -> None`` sleep hook called before each in-run
        retry (``None`` → :func:`_default_retry_backoff`, a bounded exponential
        seconds-scale wait). Tests pass a no-op recorder so no real sleep occurs.

    Returns
    -------
    OptionIngestReport
        Window count, total bars committed (session-filtered), the residual
        ``transient_failures`` / ``still_failed_windows`` (0 on a complete series;
        the deferred count on an aborted one), windows skipped as already-complete,
        and ``retried_windows`` (distinct cursors re-attempted). ``report.complete``
        is ``True`` iff the partition committed whole.

    Raises
    ------
    DhanError
        On a terminal (non-retryable) fetch error — propagated before any commit,
        so the partition stays empty and the series is quarantined upstream.
    """
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
    backoff = retry_backoff if retry_backoff is not None else _default_retry_backoff
    retried: set[tuple[date, date]] = set()

    def _fetch_cursor(window_start: date, window_end: date) -> list[OptionBar] | None:
        """Fetch ONE cursor with bounded in-run retry; return staged bars or ``None`` if exhausted.

        Resume-cursor (DEC-OPTION-429-RECOVERY-001): a RETRYABLE error
        (429 / 5xx / timeout / transport) is re-fetched in-run up to
        *max_retry_passes* EXTRA attempts after the injected backoff. Returns the
        parsed, session-filtered bars on a clean fetch (NOT yet written — the
        caller's COMMIT pass does that), or ``None`` when the retry budget is spent
        (the caller then ABORTS the whole series without committing). A TERMINAL
        (non-retryable) :class:`DhanError` propagates unchanged.
        """
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
        attempt = 0
        while True:
            if pace is not None:
                pace()
            try:
                payload = client.post_json(ENDPOINT_ROLLING_OPTION, body)
            except DhanError as exc:
                if not is_retryable(exc):
                    # TERMINAL — propagate before any commit (empty partition);
                    # quarantined upstream by ``run_dhan_options_ingest``.
                    raise
                # Feed the swallowed transient to the rate governor: a 429 drives
                # the 429 ladder, a timeout / transport / 5xx the GENTLE timeout
                # ladder (DEC-DHAN-TIMEOUT-BACKOFF-001) — so a sustained storm eases
                # the rate instead of grinding at 5 r/s into a struggling Dhan.
                if is_rate_limit(exc):
                    if on_rate_limit is not None:
                        on_rate_limit()
                elif on_timeout is not None:
                    on_timeout()
                if attempt >= max_retry_passes:
                    # Budget spent on this cursor — signal ABORT (commit nothing).
                    _LOG.warning(
                        "ingest.window.transient",
                        pull_id=pull_id,
                        window_start=window_start.isoformat(),
                        window_end=window_end.isoformat(),
                        error_type=type(exc).__name__,
                        in_run_retry=False,
                        attempts=attempt + 1,
                        **series_fields,
                    )
                    return None
                # Resume the cursor after a bounded backoff: by now the opening 429
                # burst is over + the governor has stepped down, so the retry waits
                # the limiter out rather than fighting it (DEC-OPTION-429-RECOVERY-001).
                retried.add((window_start, window_end))
                attempt += 1
                _LOG.warning(
                    "ingest.window.transient",
                    pull_id=pull_id,
                    window_start=window_start.isoformat(),
                    window_end=window_end.isoformat(),
                    error_type=type(exc).__name__,
                    in_run_retry=True,
                    retry_attempt=attempt,
                    **series_fields,
                )
                backoff(attempt - 1)
                continue
            # A clean cursor is the recover-up signal for the rate governor.
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
            _LOG.debug(
                "ingest.window",
                pull_id=pull_id,
                window_start=window_start.isoformat(),
                window_end=window_end.isoformat(),
                action="fetched",
                **series_fields,
            )
            return bars

    # ── Phase 1 — STAGE every cursor (no sink write, no coverage mark yet) ──
    # Fetch each window (with the in-run resume-cursor retry); buffer its bars.
    # Nothing is committed until the WHOLE series is fetched cleanly, so an abort
    # leaves the partition EMPTY rather than page-holed (audit Gap #2).
    staged: list[tuple[WindowKey, list[OptionBar]]] = []
    skipped = 0
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
        # Skip a window already settled — no wasted Dhan hit. The unified
        # WindowLedger is now the SOLE skip authority (DEC-WINDOW-LEDGER-001,
        # Stage c): a window is skipped ONLY when its ledger row is settled
        # (SUCCESS / SKIPPED), the precise per-window record — NOT the coarse
        # span-enclosure FetchCoverage set (#598). FetchCoverage is still marked
        # in the commit pass for one release (removed in the final stage).
        if ledger is not None and ledger.is_skippable(key):
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
        try:
            bars = _fetch_cursor(window_start, window_end)
        except DhanError as exc:
            # A Dhan DH-905 "no data present" for THIS option window is the vendor's
            # DEFINITIVE no-data answer (a genuine gap — e.g. an illiquid strike /
            # far expiry with no trades in the range). Settle it SKIPPED in the
            # unified ledger (DEC-WINDOW-LEDGER-001 Stage c2) so it stops ABORTING
            # the whole series forever: the next pull skips this known-gap window
            # and can commit the rest. It is STILL re-raised to the
            # run_dhan_options_ingest quarantine — behaviour unchanged; this only
            # enriches the ledger so the known gap is never re-fetched. A genuine
            # retryable error never reaches here (it returns None from _fetch_cursor
            # above); a non-DH-905 terminal error is re-raised WITHOUT a settle.
            if ledger is not None and is_no_data_error(exc):
                ledger.settle_known_gap(key, now=datetime.now(UTC), reason=type(exc).__name__)
            raise
        if bars is None:
            # A cursor outran its retry budget → ABORT the whole series, committing
            # NOTHING. Every non-skipped window is deferred to the next pull (the
            # no-skip guarantee — coverage untouched ⇒ a re-pull re-fetches them).
            still_failed = len(windows) - skipped
            _LOG.warning(
                "ingest.series.aborted",
                pull_id=pull_id,
                windows=len(windows),
                skipped_windows=skipped,
                still_failed_windows=still_failed,
                committed=False,
                note="cursor outran retry budget — partition NOT committed (empty, never partial)",
                **series_fields,
            )
            return OptionIngestReport(
                windows=len(windows),
                bars_appended=0,
                transient_failures=still_failed,
                skipped_windows=skipped,
                retried_windows=len(retried),
                still_failed_windows=still_failed,
            )
        staged.append((key, bars))

    # ── Phase 2 — COMMIT (every cursor fetched cleanly) ──
    # The only place the sink + coverage are mutated, so the partition is committed
    # all-or-nothing. Each window's append + coverage mark mirrors the prior
    # per-window write, just deferred until the series is proven whole.
    bars_appended = 0
    for key, bars in staged:
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
        # Lake-write nook: option bars actually persisted for this window.
        _LOG.debug("ingest.lake_write", pull_id=pull_id, bars_appended=appended, **series_fields)
        # Mark the window pulled only now — after the whole series committed.
        if coverage is not None:
            coverage.mark_complete(key)
        # Record the committed option window into the unified ledger DB
        # (DEC-WINDOW-LEDGER-001, PR-4b) so option chains are tracked in the SAME
        # browsable table as spot. A committed cursor returned bars → SUCCESS; an
        # aborted series commits nothing here, so its cursors are never recorded
        # (re-attempted next pull, mirroring the all-or-nothing coverage mark).
        if ledger is not None:
            ledger.record(
                key,
                raw_bars=len(bars),
                error_type=None,
                bars_appended=appended,
                now=datetime.now(UTC),
            )
    # Per-series summary (one fanned-out series) — DEBUG; the per-underlying
    # rollup at INFO is emitted by ``run_dhan_options_ingest`` one layer up.
    _LOG.debug(
        "ingest.series",
        pull_id=pull_id,
        windows=len(windows),
        bars_appended=bars_appended,
        transient_failures=0,
        skipped_windows=skipped,
        retried_windows=len(retried),
        still_failed_windows=0,
        **series_fields,
    )
    return OptionIngestReport(
        windows=len(windows),
        bars_appended=bars_appended,
        transient_failures=0,
        skipped_windows=skipped,
        retried_windows=len(retried),
        still_failed_windows=0,
    )


__all__ = [
    "ALL_REQUIRED_DATA",
    "JsonPoster",
    "OptionBarSink",
    "OptionIngestReport",
    "Pacer",
    "ingest_rolling_option_series",
]
