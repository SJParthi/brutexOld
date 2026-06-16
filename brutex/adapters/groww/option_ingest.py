"""Groww option ingest — fetch each resolved contract's candles into the lake.

DEC-GROWW-OPTION-INGEST-001 (K1). The third leg of the option path
(expiries → contracts → **candles**): given the
:class:`~brutex.adapters.protocol.DerivativeContract` rows
:mod:`~brutex.adapters.groww.contracts` resolved, fetch each contract's OHLCV+OI
window-by-window and append it to the **standard**
:class:`~brutex.data.sink.BarSink` under ``segment="FNO"``
(:data:`~brutex.adapters.groww.constants.OPTION_SEGMENT`) and the full contract
``groww_symbol`` as the partition symbol
(``bars/<exch>/FNO/NSE-NIFTY-02Jan25-28500-PE/<interval>/<yyyy>/<mm>.parquet`` —
``brutex/data/partition.py`` explicitly supports hyphenated contract symbols).

Groww options are ABSOLUTE-strike OHLCV+OI rows (all listed strikes,
03-backtesting.md:202-236), so they ride the SAME ``BarSink`` /
:class:`~brutex.data.models.CandleBar` path as spot — NOT a relative/rolling
option schema (Golden Rule #1 — no invented vendor shape).

**Index-free (the crux):** the ~89k FUT/CE/PE legs are DISCARDED from the lean
universe (DEC-INSTRUMENT-UNIVERSE-FAST-001), so a contract is never in the
:class:`~brutex.adapters.groww.instruments.InstrumentIndex` and the spot
``fetch_ohlcv`` (index-resolve-or-raise) cannot fetch it. This module fetches
each contract directly through the shared
:func:`~brutex.adapters.groww.candles.fetch_candle_windows`
(``segment="FNO"``, ``drop_volume=False`` — options trade, so volume + OI are
kept, 03-backtesting.md:141-143).

**Pacing (DEC-GROWW-RATE-PERMIN-001 / DEC-DHAN-RATE-SINGLE-BUCKET-001):** every
physical call goes through the injected ``pace`` hook — the SAME
:class:`~brutex.adapters.rate_common.RateController.pace` the spot pull uses
(ONE shared bucket + 500/min governor; the caller wires it). This module does
NOT reinvent pacing.

**Scope (CLAUDE.md §3):** option bars are **LAKE / REFERENCE_ONLY** (DEC-P0-063)
— never the brute-force engine or the 52-condition vocabulary.

**Honest envelope (Golden Rule #10):** the per-contract fetch tiles the contract's
range (network-bound, NOT O(1)-total); the multi-contract driver is a SEQUENTIAL,
paced outer loop (rate-safe — the bucket is the governor, not the worker count).
Parallel outer fan-out (the spot path's :class:`~brutex.orchestrator.parallel.PoolRunParallel`)
is a K2 wiring concern; the LAKE reference pull is not the brute-force hot path.
The LIVE pull needs real Groww credentials + network (operator-run); the code +
injected fakes reach 100% offline — full contract validation needs real expiries.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from time import sleep as _time_sleep

import structlog

from brutex.adapters.groww.candles import fetch_candle_windows
from brutex.adapters.groww.client import GrowwClient
from brutex.adapters.groww.constants import (
    GROWW_FUTURE_COVERAGE_MARKET,
    GROWW_OPTION_COVERAGE_MARKET,
    OPTION_SEGMENT,
    TRACK1_DAILY_INTERVAL,
)
from brutex.adapters.groww.contracts import (
    enumerate_contracts,
    future_contracts,
    option_contracts,
)
from brutex.adapters.groww.errors import GrowwError, is_auth_failure, is_rate_limit, is_retryable
from brutex.adapters.groww.ingest import IngestReport
from brutex.adapters.ingest_common import (
    clamp_window_end,
    summarize_conflicts,
)
from brutex.adapters.ingest_common import (
    now_ist as _now_ist,
)
from brutex.adapters.protocol import DerivativeContract
from brutex.data.fetch_coverage import WindowKey
from brutex.data.models import CandleBar
from brutex.data.session import filter_intraday_session
from brutex.data.sink import BarSink
from brutex.data.window_ledger import WindowLedger
from brutex.models import BruteXError
from brutex.observability.correlation import current_pull_id

#: Structured logger — per-contract DEBUG detail + the transient/quarantine
#: WARNING, every event carrying the pull's ``pull_id`` (the per-underlying INFO
#: rollup lives in the driver below).
_LOG = structlog.get_logger("brutex.adapters.groww.option_ingest")

#: Pacing hook fired once before every physical fetch (token-bucket acquire) —
#: the SAME ``Callable[[], None]`` contract the spot spine uses.
Pacer = Callable[[], None]

#: Optional per-contract minute-pull bounds, keyed by the contract ``groww_symbol``:
#: ``{symbol: (start, end)}`` half-open fetch windows derived from the 1-day lake
#: coverage (DEC-DERIV-MINUTE-BOUNDS-001 — see
#: :mod:`brutex.adapters.groww.contract_bounds`). A symbol ABSENT from the map (or a
#: ``None`` map) falls back to the spine's global ``(start, end)`` — backward-compatible
#: and never-skip. The values are the SAME half-open semantics as the spine's own
#: ``start``/``end``.
ContractWindows = Mapping[str, tuple[date, date]]

#: Canonical daily interval — the only interval not session-filtered.
_DAILY: str = "1day"

#: In-run transient-fetch retry budget for ONE contract window. ``0`` = a single
#: attempt (no retry). Small + finite (Golden Rule #10 — never an infinite loop).
_DEFAULT_MAX_ATTEMPTS: int = 2

#: Base backoff (seconds) before retry attempt *k* (0-based): ``base × 2**k``
#: clamped to :data:`_MAX_BACKOFF_S`.
_DEFAULT_BACKOFF_S: float = 1.0
_MAX_BACKOFF_S: float = 8.0


def _default_backoff(attempt_index: int) -> None:
    """Sleep the bounded exponential backoff before retry attempt *attempt_index* (0-based).

    Injected as the default ``backoff`` hook so production waits a real,
    network-recovery-scale gap between attempts while tests pass a no-op recorder.
    """
    _time_sleep(min(_DEFAULT_BACKOFF_S * (2.0**attempt_index), _MAX_BACKOFF_S))


def contract_exchange(contract: DerivativeContract) -> str:
    """Return the exchange token of a contract's ``groww_symbol`` (its first ``-`` field).

    A Groww contract symbol is ``EXCH-SYMBOL-…`` (03-backtesting.md:10-37), so the
    exchange is the leading token (``"NSE"`` / ``"BSE"``). Raises ``ValueError`` on
    a symbol with no ``-`` (fail-loud — a contract symbol always has at least the
    exchange + trading-symbol fields).
    """
    head, sep, _rest = contract.symbol.partition("-")
    if not sep:
        raise ValueError(f"contract symbol has no exchange token: {contract.symbol!r}")
    return head


def fetch_option_candles(
    client: GrowwClient,
    *,
    contract: DerivativeContract,
    interval: str,
    start: date,
    end: date,
) -> list[CandleBar]:
    """Fetch ONE contract's OHLCV+OI bars over ``[start, end)`` — index-free, FNO.

    The "feed each CE/PE ``groww_symbol`` to ``get_historical_candles``" step:
    delegates to the shared
    :func:`~brutex.adapters.groww.candles.fetch_candle_windows` with
    ``segment="FNO"`` (:data:`OPTION_SEGMENT`) and ``drop_volume=False`` (options
    trade — volume + OI are kept). The exchange comes from the contract symbol's
    leading token.

    Returns
    -------
    list[CandleBar]
        Parsed bars (with ``open_interest``) across the tiled windows.
    """
    return fetch_candle_windows(
        client,
        exchange=contract_exchange(contract),
        segment=OPTION_SEGMENT,
        groww_symbol=contract.symbol,
        interval=interval,
        start=start,
        end=end,
        drop_volume=False,
    )


def ingest_option_contract(
    client: GrowwClient,
    sink: BarSink,
    *,
    contract: DerivativeContract,
    interval: str,
    start: date,
    end: date,
    pace: Pacer | None = None,
    ledger: WindowLedger | None = None,
    on_rate_limit: Callable[[], None] | None = None,
    on_timeout: Callable[[], None] | None = None,
    on_success: Callable[[], None] | None = None,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    backoff: Callable[[int], None] | None = None,
    now_ist: Callable[[], datetime] | None = None,
    market: str = GROWW_OPTION_COVERAGE_MARKET,
) -> IngestReport:
    """Pull ONE contract's candles over ``[start, end)`` into *sink* (Groww, FNO).

    Tiles the contract range as ONE coverage window
    (``market="groww-option"``, ``security_id=<contract groww_symbol>``), fetches it
    via :func:`fetch_option_candles` (which tiles internally for the vendor's
    per-call cap), session-filters intraday bars, and appends to the standard
    :class:`~brutex.data.sink.BarSink` under ``segment="FNO"``. The unified
    :class:`~brutex.data.window_ledger.WindowLedger` is the SOLE skip + record
    authority: a settled (``SUCCESS`` / ``SKIPPED``) window is skipped before the
    vendor call; a transient error records ``FAILED`` and is retried up to
    *max_attempts* times, then left UNMARKED (deferred to the next pull — the
    no-skip guarantee). A clean fetch records ``SUCCESS`` / ``EMPTY``.

    Reuses :class:`~brutex.adapters.groww.ingest.IngestReport` (CLAUDE.md §11
    COMMON): ``windows`` is 1 when the range is non-empty (0 when ``start >= end``
    after the current-day clamp), ``skipped_windows`` 1 on a ledger skip.

    Raises
    ------
    GrowwError
        On a TERMINAL (non-retryable) fetch error — re-raised so the
        :func:`run_groww_option_ingest` quarantine sees it (auth included; Groww
        has no fail-fast latch — the same divergence as the spot spine).
    """
    exchange = contract_exchange(contract)
    clamped_end = clamp_window_end(interval, end, now_ist=(now_ist or _now_ist)())
    if start >= clamped_end:
        return IngestReport(windows=0, bars_appended=0, transient_failures=0)
    pull_id = current_pull_id()
    fields = {"symbol": contract.symbol, "interval": interval}
    key = WindowKey(
        market=market,
        security_id=contract.symbol,
        interval=interval,
        start=start,
        end=clamped_end,
    )
    if ledger is not None and ledger.is_skippable(key):
        _LOG.debug("option.ingest.window", pull_id=pull_id, action="skipped", **fields)
        return IngestReport(windows=1, bars_appended=0, transient_failures=0, skipped_windows=1)

    sleep = backoff if backoff is not None else _default_backoff
    attempt = 0
    while True:
        if pace is not None:
            pace()
        try:
            raw_bars = fetch_option_candles(
                client, contract=contract, interval=interval, start=start, end=clamped_end
            )
            break
        except GrowwError as exc:
            if not is_retryable(exc):
                raise  # terminal (auth included) → outer quarantine, never swallowed
            # Transient: feed the controller (a 429 steps the rate down, a timeout
            # drives the timeout ladder) and record FAILED so the operator SEES it.
            if is_rate_limit(exc):
                if on_rate_limit is not None:
                    on_rate_limit()
            elif on_timeout is not None:
                on_timeout()
            if ledger is not None:
                ledger.record(
                    key,
                    raw_bars=0,
                    error_type=type(exc).__name__,
                    bars_appended=0,
                    now=datetime.now(UTC),
                )
            _LOG.warning(
                "option.ingest.window.transient",
                pull_id=pull_id,
                error_type=type(exc).__name__,
                attempt=attempt,
                **fields,
            )
            if attempt >= max_attempts:
                # Budget spent — leave the window UNMARKED-as-success (deferred).
                return IngestReport(
                    windows=1,
                    bars_appended=0,
                    transient_failures=1,
                    retried_windows=1 if attempt > 0 else 0,
                    still_failed_windows=1,
                )
            attempt += 1
            sleep(attempt - 1)

    bars = raw_bars if interval == _DAILY else filter_intraday_session(raw_bars)
    result = sink.append(
        exchange=exchange,
        segment=OPTION_SEGMENT,
        symbol=contract.symbol,
        interval=interval,
        bars=bars,
    )
    conflicts = [c for wr in result.write_results for c in wr.conflicts]
    if conflicts:
        _LOG.warning(
            "option.ingest.conflict.summary",
            pull_id=pull_id,
            **summarize_conflicts(conflicts),
            **fields,
        )
    if ledger is not None:
        ledger.record(
            key,
            raw_bars=len(raw_bars),
            error_type=None,
            bars_appended=result.bars_appended,
            now=datetime.now(UTC),
        )
    if on_success is not None:
        on_success()
    return IngestReport(
        windows=1,
        bars_appended=result.bars_appended,
        transient_failures=0,
        conflicts=len(conflicts),
        retried_windows=1 if attempt > 0 else 0,
    )


@dataclass(frozen=True, slots=True)
class OptionPairFailure:
    """A ``(contract symbol, interval)`` pair quarantined on a TERMINAL Groww error."""

    symbol: str
    interval: str
    error: str
    auth_failure: bool = False
    """Whether the terminal error was a Groww auth failure (a dead/invalid token).

    Captured at quarantine time so an outer auto-retry wrapper can halt on a token
    death — retrying is futile until re-auth (mirrors the spot spine).
    """


@dataclass(frozen=True, slots=True)
class GrowwOptionIngestReport:
    """Aggregate outcome of a :func:`run_groww_option_ingest` pass.

    Mirrors :class:`~brutex.adapters.groww.run_ingest.GrowwIngestReport` (CLAUDE.md
    §11 COMMON — same derived-count surface), keyed by ``(contract symbol, interval)``.
    """

    pairs: dict[tuple[str, str], IngestReport]
    failures: tuple[OptionPairFailure, ...]

    @property
    def windows(self) -> int:
        """Total fetch windows across all successful pairs."""
        return sum(r.windows for r in self.pairs.values())

    @property
    def bars_appended(self) -> int:
        """Total bars appended across all successful pairs."""
        return sum(r.bars_appended for r in self.pairs.values())

    @property
    def transient_failures(self) -> int:
        """Total per-window transient skips across all successful pairs."""
        return sum(r.transient_failures for r in self.pairs.values())

    @property
    def skipped_windows(self) -> int:
        """Total windows skipped as already-complete across all pairs."""
        return sum(r.skipped_windows for r in self.pairs.values())

    @property
    def retried_windows(self) -> int:
        """Total windows re-attempted by the in-run retry across all pairs."""
        return sum(r.retried_windows for r in self.pairs.values())

    @property
    def still_failed_windows(self) -> int:
        """Total windows still failing after the retry budget (deferred to next pull)."""
        return sum(r.still_failed_windows for r in self.pairs.values())

    @property
    def failed_pairs(self) -> int:
        """Number of ``(contract, interval)`` pairs quarantined on a terminal error."""
        return len(self.failures)

    @property
    def auth_aborted(self) -> bool:
        """True iff any quarantined failure was a Groww auth failure (re-auth needed)."""
        return any(f.auth_failure for f in self.failures)


#: Live-progress hook fired once per ``(contract, interval)`` pair after it is
#: processed: ``(label, bars_appended, failed)``. ``bars_appended`` is 0 for a
#: quarantined pair.
ProgressHook = Callable[[str, int, bool], None]


def _resolve_window(
    contract_windows: ContractWindows | None,
    *,
    symbol: str,
    interval: str,
    start: date,
    end: date,
) -> tuple[date, date]:
    """Resolve one ``(contract, interval)`` pair's ``(start, end)`` — the smart-bounds hook.

    DEC-DERIV-MINUTE-BOUNDS-001. Returns the per-contract bounded window from
    *contract_windows* ONLY for sub-daily intervals and only when *symbol* is in the
    map; every other case falls back to the global ``(start, end)``:

    * ``contract_windows is None`` → ``(start, end)`` — byte-identical to the
      pre-feature behaviour (the param is opt-in).
    * ``interval == TRACK1_DAILY_INTERVAL`` → ``(start, end)`` — the DAILY interval is
      the coverage that PRODUCES the bounds, so it must NEVER be bounded by them, or a
      still-live contract's daily coverage would freeze at its first-observed extent
      and stop advancing (the blocker the design review caught).
    * *symbol* absent from the map → ``(start, end)`` — a contract with no derived
      window (no 1-day coverage yet) is pulled in FULL, never skipped.

    Pure + **O(1)** (one dict lookup).
    """
    if contract_windows is None or interval == TRACK1_DAILY_INTERVAL:
        return (start, end)
    return contract_windows.get(symbol, (start, end))


def run_groww_option_ingest(
    client: GrowwClient,
    sink: BarSink,
    *,
    contracts: Iterable[DerivativeContract],
    intervals: Sequence[str],
    start: date,
    end: date,
    pace: Pacer | None = None,
    ledger: WindowLedger | None = None,
    on_rate_limit: Callable[[], None] | None = None,
    on_timeout: Callable[[], None] | None = None,
    on_success: Callable[[], None] | None = None,
    on_progress: ProgressHook | None = None,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    backoff: Callable[[int], None] | None = None,
    now_ist: Callable[[], datetime] | None = None,
    market: str = GROWW_OPTION_COVERAGE_MARKET,
    contract_windows: ContractWindows | None = None,
) -> GrowwOptionIngestReport:
    """Ingest every ``(contract, interval)`` derivative pair over ``[start, end)``.

    Contract-type-agnostic: the SAME driver ingests OPTION (CE/PE) or FUTURE (FUT)
    contracts — only *market* differs (the lake-ledger namespace:
    ``"groww-option"`` vs ``"groww-future"``, DEC-GROWW-FUTURE-INGEST-001).

    A SEQUENTIAL, paced outer loop (the shared rate bucket the *pace* hook drives
    is the governor — not the worker count). **Never raises** on a Groww error: a
    TERMINAL failure on one pair is quarantined (recorded as an
    :class:`OptionPairFailure`) and the run continues to the next pair — one bad
    contract never kills the whole pull. A non-Groww exception (a genuine bug) is
    NOT swallowed.

    Each pair writes a DISTINCT lake partition (the contract symbol), so the
    appends are path-isolated; idempotent re-pulls are absorbed by the keep-first
    sink + the ledger skip.

    Parameters mirror :func:`ingest_option_contract`; *contracts* is the resolved
    (typically CE/PE — see :func:`~brutex.adapters.groww.contracts.option_contracts`)
    chain, *intervals* the canonical intervals to pull per contract.

    *contract_windows* (DEC-DERIV-MINUTE-BOUNDS-001) is an OPTIONAL per-contract
    ``{symbol: (start, end)}`` map (typically built by
    :func:`~brutex.adapters.groww.contract_bounds.derive_contract_minute_windows`
    from the 1-day lake coverage). When supplied, the heavy SUB-DAILY pull of a
    contract present in the map is bounded to its real listing→expiry window instead
    of the full *start*/*end* range — avoiding the first-pass requests over the dead
    periods a contract did not trade. ``None`` (the default) is byte-identical to the
    pre-feature behaviour; the DAILY interval is never bounded (see
    :func:`_resolve_window`); a contract absent from the map is pulled in full
    (never skipped). This is an OPTIMISATION, not a correctness change — the
    keep-first sink + WindowLedger make the resulting lake byte-identical to a
    full-range pull (Golden Rule #7).

    Returns
    -------
    GrowwOptionIngestReport
        Per-pair :class:`~brutex.adapters.groww.ingest.IngestReport` for successes +
        the quarantined failures.
    """
    pull_id = current_pull_id()
    pairs: dict[tuple[str, str], IngestReport] = {}
    failures: list[OptionPairFailure] = []
    for contract in contracts:
        for interval in intervals:
            label = f"{contract.symbol} {interval}"
            window_start, window_end = _resolve_window(
                contract_windows,
                symbol=contract.symbol,
                interval=interval,
                start=start,
                end=end,
            )
            try:
                report = ingest_option_contract(
                    client,
                    sink,
                    contract=contract,
                    interval=interval,
                    start=window_start,
                    end=window_end,
                    pace=pace,
                    ledger=ledger,
                    on_rate_limit=on_rate_limit,
                    on_timeout=on_timeout,
                    on_success=on_success,
                    max_attempts=max_attempts,
                    backoff=backoff,
                    now_ist=now_ist,
                    market=market,
                )
            except BruteXError as exc:
                # Quarantine ANY brutex DOMAIN error per contract (a terminal GrowwError,
                # a CandleParseError on un-storable option data, or an OutOfScopeError on an
                # unresolvable symbol) so one bad contract never crashes the whole bulk
                # option pull — the same per-symbol resilience the spot spine has
                # (DEC-GROWW-QUARANTINE-BRUTEX-001); a genuine code bug (a Python builtin)
                # still surfaces. Only a GrowwError can be an auth failure.
                auth_failed = isinstance(exc, GrowwError) and is_auth_failure(exc)
                failures.append(
                    OptionPairFailure(
                        symbol=contract.symbol,
                        interval=interval,
                        error=str(exc),
                        auth_failure=auth_failed,
                    )
                )
                _LOG.warning(
                    "option.ingest.quarantine",
                    pull_id=pull_id,
                    symbol=contract.symbol,
                    interval=interval,
                    error=str(exc),
                )
                if on_progress is not None:
                    on_progress(label, 0, True)
                continue
            pairs[contract.symbol, interval] = report
            if on_progress is not None:
                on_progress(label, report.bars_appended, False)
    return GrowwOptionIngestReport(pairs=pairs, failures=tuple(failures))


def ingest_underlying_options(
    client: GrowwClient,
    sink: BarSink,
    *,
    exchange: str,
    underlying_symbol: str,
    years: Iterable[int],
    intervals: Sequence[str],
    start: date,
    end: date,
    pace: Pacer | None = None,
    ledger: WindowLedger | None = None,
    on_rate_limit: Callable[[], None] | None = None,
    on_timeout: Callable[[], None] | None = None,
    on_success: Callable[[], None] | None = None,
    on_progress: ProgressHook | None = None,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    backoff: Callable[[int], None] | None = None,
    now_ist: Callable[[], datetime] | None = None,
    contract_windows: ContractWindows | None = None,
) -> GrowwOptionIngestReport:
    """Full path for ONE underlying: expiries → contracts → CE/PE candles → lake.

    Resolves the underlying's OPTION chain across *years*
    (:func:`~brutex.adapters.groww.contracts.enumerate_contracts` filtered by
    :func:`~brutex.adapters.groww.contracts.option_contracts` — CE/PE only), then
    ingests each via :func:`run_groww_option_ingest`. The resolution calls
    (``get_expiries`` / ``get_contracts``) ride the SAME injected ``client.pace``
    rate gate as the candle fetches.

    A TERMINAL Groww error during RESOLUTION (a dead token / bad request) is NOT
    swallowed here — it propagates so the caller (the K2 phased universe driver)
    can quarantine the whole underlying. Per-contract candle failures are
    quarantined inside :func:`run_groww_option_ingest`.

    *contract_windows* (DEC-DERIV-MINUTE-BOUNDS-001) is forwarded verbatim to
    :func:`run_groww_option_ingest` — an OPTIONAL ``{symbol: (start, end)}`` map
    bounding each contract's heavy sub-daily pull to its real listing→expiry window
    (``None`` = the full-range, pre-feature behaviour).

    **Scope (CLAUDE.md §3):** REFERENCE_ONLY lake — never the brute-force engine.
    """
    contracts = option_contracts(
        enumerate_contracts(
            client, exchange=exchange, underlying_symbol=underlying_symbol, years=years
        )
    )
    return run_groww_option_ingest(
        client,
        sink,
        contracts=contracts,
        intervals=intervals,
        start=start,
        end=end,
        pace=pace,
        ledger=ledger,
        on_rate_limit=on_rate_limit,
        on_timeout=on_timeout,
        on_success=on_success,
        on_progress=on_progress,
        max_attempts=max_attempts,
        backoff=backoff,
        now_ist=now_ist,
        contract_windows=contract_windows,
    )


def ingest_underlying_futures(
    client: GrowwClient,
    sink: BarSink,
    *,
    exchange: str,
    underlying_symbol: str,
    years: Iterable[int],
    intervals: Sequence[str],
    start: date,
    end: date,
    pace: Pacer | None = None,
    ledger: WindowLedger | None = None,
    on_rate_limit: Callable[[], None] | None = None,
    on_timeout: Callable[[], None] | None = None,
    on_success: Callable[[], None] | None = None,
    on_progress: ProgressHook | None = None,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    backoff: Callable[[int], None] | None = None,
    now_ist: Callable[[], datetime] | None = None,
    contract_windows: ContractWindows | None = None,
) -> GrowwOptionIngestReport:
    """Full path for ONE underlying: expiries → FUTURE contracts → candles → lake.

    The FUTURES mirror of :func:`ingest_underlying_options` (DEC-GROWW-FUTURE-INGEST-001):
    resolves the underlying's chain across *years*
    (:func:`~brutex.adapters.groww.contracts.enumerate_contracts` filtered by
    :func:`~brutex.adapters.groww.contracts.future_contracts` — ``FUT`` only, one
    expiry-dated future per expiry, no strike fan), then ingests each via the SHARED
    :func:`run_groww_option_ingest` under the DISTINCT
    :data:`~brutex.adapters.groww.constants.GROWW_FUTURE_COVERAGE_MARKET`
    (``"groww-future"``) ledger namespace, so future windows never collide with the
    option/spot windows. Pacing, ledger skip/record, per-pair quarantine, and the
    standard FNO ``BarSink`` are all reused (CLAUDE.md §11 COMMON). Like
    :func:`ingest_underlying_options`, a TERMINAL Groww error during RESOLUTION
    propagates (the caller quarantines the whole underlying); per-contract candle
    failures are quarantined inside :func:`run_groww_option_ingest`.

    *contract_windows* (DEC-DERIV-MINUTE-BOUNDS-001) is forwarded verbatim to
    :func:`run_groww_option_ingest` — the same OPTIONAL per-contract minute-bounds
    map :func:`ingest_underlying_options` accepts (``None`` = full-range behaviour).

    **Scope (CLAUDE.md §3):** REFERENCE_ONLY lake — never the brute-force engine.
    """
    contracts = future_contracts(
        enumerate_contracts(
            client, exchange=exchange, underlying_symbol=underlying_symbol, years=years
        )
    )
    return run_groww_option_ingest(
        client,
        sink,
        contracts=contracts,
        intervals=intervals,
        start=start,
        end=end,
        pace=pace,
        ledger=ledger,
        on_rate_limit=on_rate_limit,
        on_timeout=on_timeout,
        on_success=on_success,
        on_progress=on_progress,
        max_attempts=max_attempts,
        backoff=backoff,
        now_ist=now_ist,
        market=GROWW_FUTURE_COVERAGE_MARKET,
        contract_windows=contract_windows,
    )


__all__ = [
    "ContractWindows",
    "GrowwOptionIngestReport",
    "OptionPairFailure",
    "Pacer",
    "ProgressHook",
    "contract_exchange",
    "fetch_option_candles",
    "ingest_option_contract",
    "ingest_underlying_futures",
    "ingest_underlying_options",
    "run_groww_option_ingest",
]
