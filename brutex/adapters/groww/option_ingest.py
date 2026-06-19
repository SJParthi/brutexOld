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

**Parallel outer fan-out (DEC-GROWW-OPTION-POOL-001):** the multi-contract driver
maps one job per ``(contract, interval)`` pair through an injected
:class:`~brutex.orchestrator.universe_phases.RunParallel` — the SAME concurrency seam
the spot spine uses. Production injects a
:class:`~brutex.orchestrator.parallel.PoolRunParallel` over a multi-connection
:class:`~brutex.adapters.groww.pool.ClientPool` so the per-contract fetches fan across
K SDK connections (8-RPS parity with spot) instead of serialising on one socket; the
DEFAULT is a LAZY :class:`~brutex.orchestrator.parallel.SerialRunParallel` that is
byte-identical to the prior sequential loop. This was the deferred "K2 wiring concern"
the spot path resolved first; it is now wired here too (the LAKE reference pull is
still not the brute-force hot path).

**Honest envelope (Golden Rule #10):** the per-contract fetch tiles the contract's
range (network-bound, NOT O(1)-total); concurrency is purely CROSS-contract (one
in-flight call per contract — no nested window pool), and the SHARED rate bucket the
``pace`` hook drives is still the sole issue governor (CLAUDE.md §2 Rule 6 — the pool
adds concurrency, NEVER extra rate). The LIVE pull needs real Groww credentials +
network (operator-run); the code + injected fakes reach 100% offline — full contract
validation needs real expiries.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from time import sleep as _time_sleep

import structlog

from brutex.adapters.groww.candles import fetch_candle_windows
from brutex.adapters.groww.client import GrowwClientProtocol
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
    CONFIRMED_EMPTY_MIN_FETCHES,
    DEFAULT_EMPTY_RETRY_ATTEMPTS,
    OLD_GAP_HORIZON_DAYS,
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
from brutex.observability.correlation import current_pull_id, pull_id_scope
from brutex.orchestrator.parallel import SerialRunParallel
from brutex.orchestrator.quota_tracker import DailyQuotaExceeded, QuotaInvariantError
from brutex.orchestrator.universe_phases import RunParallel

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

#: Domain errors that are a GLOBAL stop, NOT one contract's problem — they must
#: PROPAGATE past the per-contract quarantine so the whole pull aborts, never be
#: isolated as a single pair's failure. Mirrors the spot spine's carve-out
#: (:data:`~brutex.adapters.groww.run_ingest._GLOBAL_STOP_ERRORS`): a quota signal
#: means the day's budget is spent (``DailyQuotaExceeded``) or a quota-ledger
#: invariant broke (``QuotaInvariantError``), so every REMAINING contract would
#: fail identically. This keeps the broad ``BruteXError`` per-contract quarantine
#: (DEC-GROWW-QUARANTINE-BRUTEX-001) from swallowing a pull-wide stop.
_GLOBAL_STOP_ERRORS: tuple[type[BruteXError], ...] = (DailyQuotaExceeded, QuotaInvariantError)

#: One unit of OUTER-loop work for the parallel fan-out: a derivative
#: ``(contract, interval)`` pair. Each maps to a DISTINCT lake partition (the
#: contract ``groww_symbol``), so the per-pair writes are path-isolated and a
#: worker pool can fan across them safely (DEC-GROWW-OPTION-POOL-001).
OptionJob = tuple[DerivativeContract, str]


def _default_backoff(attempt_index: int) -> None:
    """Sleep the bounded exponential backoff before retry attempt *attempt_index* (0-based).

    Injected as the default ``backoff`` hook so production waits a real,
    network-recovery-scale gap between attempts while tests pass a no-op recorder.
    """
    _time_sleep(min(_DEFAULT_BACKOFF_S * (2.0**attempt_index), _MAX_BACKOFF_S))


def _noop_empty_backoff(_attempt: int) -> None:
    """Default no-op backoff between EMPTY re-fetches (the token bucket already paces).

    Mirrors the spot spine's empty-retry default (``ingest._noop_empty_backoff``): a clean
    0-bar response is re-fetched immediately — the per-call ``pace()`` token-bucket acquire
    already spaces the calls, so no extra sleep is needed by default. Tests inject a recorder.
    """


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
    client: GrowwClientProtocol,
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
    client: GrowwClientProtocol,
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
    empty_retry_attempts: int = DEFAULT_EMPTY_RETRY_ATTEMPTS,
    empty_retry_backoff: Callable[[int], None] | None = None,
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

    In-pull EMPTY retry (DEC-OPTION-EMPTY-RETRY-001 — the option-candle twin of the spot
    spine's DEC-EMPTY-INPULL-RETRY-001 / DEC-EMPTY-RETRY-EVERYWHERE-001): a clean **0-bar
    response is re-fetched** up to *empty_retry_attempts* times — a listed strike chain can
    return an empty ``200`` EITHER because the window is genuinely empty OR because of a
    transient vendor glitch on a window that DOES have data; the first attempt that returns
    bars WINS, and only an all-empty run records genuine no-data WITH confidence. Bounded, so
    a truly-empty window can never loop forever. This closes the last "pull until a successful
    response" gap (spot candles + ``get_expiries`` + ``get_contracts`` already had it).

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
    now_for_clamp = (now_ist or _now_ist)()
    clamped_end = clamp_window_end(interval, end, now_ist=now_for_clamp)
    if start >= clamped_end:
        return IngestReport(windows=0, bars_appended=0, transient_failures=0)
    # OLD-gap fast-settle cutoff (DEC-OLD-GAP-FAST-SETTLE-001): an expired contract's window
    # whose ``end`` is strictly before this date can never gain vendor data, so an in-pull-
    # confirmed-empty old window settles SKIPPED in ONE pull (the spot-spine twin). Computed
    # off the SAME injected clock the current-day clamp uses, so an injected ``now_ist`` makes
    # the decision deterministic.
    old_cutoff = now_for_clamp.date() - timedelta(days=OLD_GAP_HORIZON_DAYS)
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
    empty_backoff = empty_retry_backoff if empty_retry_backoff is not None else _noop_empty_backoff
    attempt = 0
    # Vendor fetches THIS pull makes for this window (DEC-WINDOW-INPULL-FETCHES-001) —
    # counts BOTH the transient re-attempts and the empty re-fetches, so the ledger row's
    # ``in_pull_fetches`` exposes the in-pull retry the per-attempt DEBUG log hid.
    fetch_count = 0
    raw_bars: list[CandleBar] = []
    # Outer EMPTY-retry loop (DEC-OPTION-EMPTY-RETRY-001): each pass runs the inner
    # transient-retry fetch; a 0-bar SUCCESS is re-fetched up to empty_retry_attempts times.
    # The first attempt with bars WINS (a glitched empty is recovered); only an all-empty run
    # falls through to record genuine no-data. Bounded — a truly-empty window never loops.
    # ``max(1, …)`` HARDENS over the spot mirror's bare ``range(N)``: a degenerate
    # ``empty_retry_attempts<1`` still fetches exactly once, never silently records an empty
    # window WITHOUT calling the vendor (the out-of-box guard; default is 3, never <1).
    for empty_attempt in range(max(1, empty_retry_attempts)):
        while True:
            if pace is not None:
                pace()
            fetch_count += 1
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
                        in_pull_fetches=fetch_count,
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
        if raw_bars:
            break  # bars on this attempt — the (possibly glitched) empty is recovered
        if empty_attempt + 1 < empty_retry_attempts:
            # Still empty + a retry remains: log + (default no-op) backoff, then re-fetch.
            # The backoff arg is 0-based (``empty_attempt``) to TRULY mirror the spot spine
            # (ingest._ingest_one_window: ``empty_retry_backoff(empty_attempt)``); the log
            # shows the 1-based round + total, same keys as spot, for readability.
            _LOG.debug(
                "option.ingest.window.empty_retry",
                pull_id=pull_id,
                attempt=empty_attempt + 1,
                of=empty_retry_attempts,
                **fields,
            )
            empty_backoff(empty_attempt)

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
    # OLD-gap fast-settle (DEC-OLD-GAP-FAST-SETTLE-001 — the spot-spine twin): an EMPTY
    # window confirmed empty ACROSS an in-pull retry (``fetch_count >= 2``) AND OLD enough
    # that vendor data can never appear (``clamped_end`` strictly before ``old_cutoff``) is a
    # PERMANENT gap (an expired contract's dead period) → settle SKIPPED in ONE pull. A RECENT
    # empty window (or one not re-hit) keeps the conservative across-pull EMPTY streak.
    confirmed_empty_after_retry = (not raw_bars) and fetch_count >= CONFIRMED_EMPTY_MIN_FETCHES
    is_old = clamped_end < old_cutoff
    settle_empty = confirmed_empty_after_retry and is_old
    # Mirror the spot spine's INFO surface (DEC-WINDOW-INPULL-FETCHES-001): when the
    # in-pull retry fired AND the window stayed empty, one INFO line makes the retry
    # visible in the log (the per-attempt event above is DEBUG). ``settled`` flags an
    # old-gap fast-settle so the log is honest about whether it became terminal SKIPPED.
    if not raw_bars and fetch_count > 1:
        _LOG.info(
            "option.ingest.window.empty_after_retries",
            pull_id=pull_id,
            fetches=fetch_count,
            settled=settle_empty,
            **fields,
        )
    if ledger is not None:
        ledger.record(
            key,
            raw_bars=len(raw_bars),
            error_type=None,
            bars_appended=result.bars_appended,
            now=datetime.now(UTC),
            in_pull_fetches=fetch_count,
            settle_empty=settle_empty,
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
    client: GrowwClientProtocol,
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
    run_parallel: RunParallel | None = None,
) -> GrowwOptionIngestReport:
    """Ingest every ``(contract, interval)`` derivative pair over ``[start, end)``.

    Contract-type-agnostic: the SAME driver ingests OPTION (CE/PE) or FUTURE (FUT)
    contracts — only *market* differs (the lake-ledger namespace:
    ``"groww-option"`` vs ``"groww-future"``, DEC-GROWW-FUTURE-INGEST-001).

    The OUTER loop — one job per ``(contract, interval)`` pair — is mapped through
    *run_parallel* (DEC-GROWW-OPTION-POOL-001), the SAME concurrency seam the spot
    spine uses (:func:`~brutex.adapters.groww.run_ingest.run_groww_spot_ingest`):
    pass a :class:`~brutex.orchestrator.parallel.PoolRunParallel` (over a
    multi-connection :class:`~brutex.adapters.groww.pool.ClientPool`) and the
    per-contract fetches fan across K SDK connections so the pull reaches UP TO the
    rate ceiling instead of serialising on one socket. The DEFAULT
    (``run_parallel=None``) is a :class:`~brutex.orchestrator.parallel.SerialRunParallel`
    — a LAZY in-thread map that is **byte-identical** to the prior sequential loop
    (same contract-major order, same early-stop-on-quota), so every non-wired caller
    + test keeps the exact prior behaviour. (Under the live ``PoolRunParallel`` the
    early-stop is best-effort: a GLOBAL stop re-raised mid-drain aborts the pull, but
    already-in-flight sibling contracts finish first — the pool submits eagerly and
    its teardown waits on them; harmless since writes are idempotent + each call is
    deadline-bounded, the established spot-pool semantics.) Each contract fetches its own windows
    SERIALLY (``fetch_candle_windows`` tiles internally), so concurrency is purely
    CROSS-contract — one in-flight call per contract, ``len(jobs)`` contracts at once
    (no nested window pool, unlike spot). The SHARED rate bucket the *pace* hook
    drives is still the sole issue governor (CLAUDE.md §2 Rule 6 — the pool adds
    concurrency, NEVER extra rate).

    **Never raises** on a brutex DOMAIN error (:class:`~brutex.models.BruteXError` —
    incl. ``GrowwError`` / ``CandleParseError`` / ``OutOfScopeError``): a TERMINAL
    failure on one pair is quarantined (recorded as an :class:`OptionPairFailure`)
    and the run continues — one bad contract never kills the whole pull. A GLOBAL
    stop (:data:`_GLOBAL_STOP_ERRORS` — quota) PROPAGATES (it is every contract's
    problem), and a genuine CODE bug (a Python builtin) is NOT swallowed
    (DEC-GROWW-QUARANTINE-BRUTEX-001). The quarantine WARNING + *on_progress* fire on
    the CONTROLLER thread (the single-threaded drain), so the hook need not be
    thread-safe.

    Each pair writes a DISTINCT lake partition (the contract symbol), so the
    appends are path-isolated; idempotent re-pulls are absorbed by the keep-first
    sink + the ledger skip.

    Parameters mirror :func:`ingest_option_contract`; *contracts* is the resolved
    (typically CE/PE — see :func:`~brutex.adapters.groww.contracts.option_contracts`)
    chain, *intervals* the canonical intervals to pull per contract. *run_parallel*
    is the outer concurrency primitive (``None`` → the byte-identical serial default;
    production injects a rate-sized ``PoolRunParallel``; tests inject a synchronous
    fake).

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
    runner: RunParallel = run_parallel if run_parallel is not None else SerialRunParallel()
    # Deterministic worklist: one job per (contract, interval) pair, in the caller's
    # contract order × interval order — the OUTER fan-out unit (each a distinct lake
    # partition). ``contracts`` is materialised ONCE (it may be a one-shot iterator).
    jobs: list[OptionJob] = [
        (contract, interval) for contract in contracts for interval in intervals
    ]

    def _run_pair(job: OptionJob) -> IngestReport:
        """Pull one ``(contract, interval)`` pair — runs inside a worker thread.

        Re-binds *pull_id* into THIS thread's structlog context-vars (they do not cross
        the pool boundary). A raised :class:`~brutex.models.BruteXError` is captured by
        *run_parallel* and quarantined on the controller thread below; any other
        exception is captured too and re-raised there.
        """
        contract, interval = job
        window_start, window_end = _resolve_window(
            contract_windows,
            symbol=contract.symbol,
            interval=interval,
            start=start,
            end=end,
        )
        with pull_id_scope(pull_id):
            return ingest_option_contract(
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

    pairs: dict[tuple[str, str], IngestReport] = {}
    failures: list[OptionPairFailure] = []
    # Drain the fan-out on the controller thread: on_progress + the quarantine WARNING
    # fire here (single-threaded), so the hook need not be thread-safe. A global-stop or
    # a captured code bug surfaces; a terminal DOMAIN error is quarantined.
    for outcome in runner(_run_pair, jobs):
        contract, interval = outcome.item
        label = f"{contract.symbol} {interval}"
        if outcome.ok:
            report = outcome.result
            if report is None:  # pragma: no cover — ok=True always carries a result
                continue
            pairs[contract.symbol, interval] = report
            if on_progress is not None:
                on_progress(label, report.bars_appended, False)
            continue
        # A failed ItemResult always carries the captured worker exception.
        exc = outcome.exc
        if exc is None:  # pragma: no cover — a failed outcome always carries exc
            continue
        if isinstance(exc, _GLOBAL_STOP_ERRORS):
            # A GLOBAL stop (quota exhausted / quota-ledger invariant) is NOT one
            # contract's problem — re-raise so it propagates to the caller's quota
            # handling (DEC-SPOT-QUOTA-MIDROUND-001) instead of being quarantined as a
            # single pair (the carve-out from the broad BruteXError quarantine below).
            raise exc
        if not isinstance(exc, BruteXError):
            # A genuine CODE bug (a Python builtin: ValueError / KeyError / TypeError
            # from a defect) escaped a worker — never swallow a real error: re-raise so
            # the orchestrator never hides a bug. ONLY a brutex DOMAIN error is
            # quarantined (DEC-GROWW-QUARANTINE-BRUTEX-001).
            raise exc
        # Terminal DOMAIN error → quarantine THIS pair, the run continues. The base
        # BruteXError covers every "this one contract can't be pulled" cause (a terminal
        # GrowwError, a CandleParseError on un-storable option data, an OutOfScopeError);
        # only a GrowwError can be an auth failure (a parse / scope error never is).
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
    return GrowwOptionIngestReport(pairs=pairs, failures=tuple(failures))


def ingest_underlying_options(
    client: GrowwClientProtocol,
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
    run_parallel: RunParallel | None = None,
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
    (``None`` = the full-range, pre-feature behaviour). *run_parallel*
    (DEC-GROWW-OPTION-POOL-001) is forwarded verbatim too — the outer cross-contract
    concurrency primitive (``None`` = the byte-identical serial default).

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
        run_parallel=run_parallel,
    )


def ingest_underlying_futures(
    client: GrowwClientProtocol,
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
    run_parallel: RunParallel | None = None,
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
    *run_parallel* (DEC-GROWW-OPTION-POOL-001) is forwarded verbatim too — the outer
    cross-contract concurrency primitive (``None`` = the byte-identical serial default).

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
        run_parallel=run_parallel,
    )


__all__ = [
    "ContractWindows",
    "GrowwOptionIngestReport",
    "OptionJob",
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
