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
from decimal import Decimal
from pathlib import Path
from time import sleep as _time_sleep
from typing import TYPE_CHECKING, Final

import structlog

from brutex.adapters.empty_circuit_breaker import (
    EmptySettleBreaker,
    MassEmptySettleHalt,
    resolve_empty_breaker,
)
from brutex.adapters.error_storm_breaker import (
    ErrorStormBreaker,
    VendorDownError,
    observe_authorisation_failure,
    observe_window_failure,
    observe_window_success,
)
from brutex.adapters.groww.candles import RawPayloadHook, fetch_candle_windows
from brutex.adapters.groww.client import (
    GrowwClientProtocol,
    client_self_paces,
    client_self_reports_rate_limits,
    client_self_reports_successes,
)
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
from brutex.adapters.groww.errors import (
    GrowwAuthError,
    GrowwAuthorisationError,
    GrowwCredentialDeadError,
    GrowwError,
    is_auth_failure,
    is_rate_limit,
    is_retryable,
    is_terminal_auth_failure,
    steps_timeout_ladder,
)
from brutex.adapters.groww.fetch_pipeline import FetchPipeline, FetchTail, RawFetchResult
from brutex.adapters.groww.ingest import IngestReport
from brutex.adapters.groww.symbol_form_preference import SymbolFormPreference
from brutex.adapters.groww.write_deadline import append_with_deadline
from brutex.adapters.ingest_common import (
    CONFIRMED_EMPTY_MIN_FETCHES,
    DEFAULT_EMPTY_RETRY_ATTEMPTS,
    OLD_GAP_HORIZON_DAYS,
    clamp_window_end,
    drop_bars_after,
    maybe_audit,
    summarize_conflicts,
)
from brutex.adapters.ingest_common import (
    now_ist as _now_ist,
)
from brutex.adapters.protocol import DerivativeContract
from brutex.data.errors import (
    LakeWriteHalt,
    MarketHoursHalt,
    PartitionLockTimeout,
    WriteDeadlineExceeded,
)
from brutex.data.fetch_coverage import WindowKey
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import fold_max_close_delta
from brutex.data.partial_window_detect import (
    SUSPECTED_PARTIAL_WINDOW_ERROR_EVENT,
    SUSPECTED_PARTIAL_WINDOW_EVENT,
    observe_partial_window,
)
from brutex.data.raw_integrity_store import CaptureHookFactory
from brutex.data.session import ALL_FILTERED, filter_to_trading_session, observe_all_filtered
from brutex.data.session_density import observe_session_density
from brutex.data.sink import BarSink
from brutex.data.today_completeness import (
    TODAY_INCOMPLETE,
    InstrumentClass,
    observe_today_completeness,
)
from brutex.data.window_ledger import WindowLedger
from brutex.data.window_ledger_store import (
    LakeSpanIndex,
    SeedCandidate,
    index_lake_spans,
    seed_enclosed_windows,
)
from brutex.data.write_breaker import (
    WriteHealthBreaker,
    partition_health_key,
    resolve_write_breaker,
)
from brutex.data.write_consumer import WriteConsumer, WriteConsumerSink
from brutex.models import BruteXError
from brutex.observability.correlation import current_pull_id, pull_id_scope
from brutex.orchestrator.parallel import SerialRunParallel
from brutex.orchestrator.quota_tracker import DailyQuotaExceeded, QuotaInvariantError
from brutex.orchestrator.universe_phases import RunParallel

if TYPE_CHECKING:
    from brutex.orchestrator.audit import AuditWriter

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
#: fail identically; ``LakeWriteHalt`` (disk-full / OOM at the lake-write boundary,
#: DEC-LAKE-WRITE-HALT-001) joins it for the same reason — a full disk fails every
#: remaining contract identically and must halt, not quarantine per-contract. This
#: keeps the broad ``BruteXError`` per-contract quarantine
#: (DEC-GROWW-QUARANTINE-BRUTEX-001) from swallowing a pull-wide stop.
#: ``MassEmptySettleHalt`` joins it (DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001): a tripped
#: mass-empty circuit breaker means every remaining contract is fetching into the
#: same suspected soft-throttle / mis-addressing episode — halt, don't settle.
#: ``GrowwAuthError`` joins it (DEC-GROWW-AUTH-HALT-001): a TERMINAL auth failure —
#: a dead credential / an exhausted re-mint budget — fails every remaining contract
#: identically, so quarantining contract-by-contract would "complete" an all-failed
#: pull with NO critical alert. The drain check carves the two RETRYABLE
#: ``GrowwAuthError`` subclasses (``GrowwTokenIssuanceThrottled`` /
#: ``GrowwTokenStillStaleError``) BACK OUT via
#: :func:`~brutex.adapters.groww.errors.is_terminal_auth_failure` — a transient
#: auth-family shape stays a per-contract quarantine, and the mid-pull re-mint
#: self-heal (DEC-GROWW-TOKEN-REAUTH-001) never raises out of ``client._call`` at all.
#: ``VendorDownError`` + ``GrowwCredentialDeadError`` join it
#: (DEC-VENDOR-DOWN-BREAKER-001): a tripped vendor-down error-storm breaker means the
#: VENDOR (or the network path to it) is down — every remaining contract would burn
#: its whole retry ladder identically; a dead-credential escalation means every
#: remaining fetch is auth-stalled against the SAME dead token. Both halt
#: (``GrowwCredentialDeadError`` is a TERMINAL ``GrowwAuthError`` subclass, so it also
#: satisfies the ``is_terminal_auth_failure`` carve-out — listed explicitly as the
#: DEC-VENDOR-DOWN-BREAKER-001 escalation member).
_GLOBAL_STOP_ERRORS: tuple[type[BruteXError], ...] = (
    DailyQuotaExceeded,
    QuotaInvariantError,
    LakeWriteHalt,
    # DEC-GROWW-MARKET-HOURS-GUARD-001: the per-contract market-hours guard raised —
    # the pull crossed into the NSE trading window and must halt to leave the shared
    # Groww account free for TickVault's live feed. A PULL-WIDE stop: propagates past
    # the per-contract quarantine so the whole pull halts gracefully.
    MarketHoursHalt,
    MassEmptySettleHalt,
    GrowwAuthError,
    VendorDownError,
    GrowwCredentialDeadError,
)

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
    on_raw_payload: RawPayloadHook | None = None,
    form_preference: SymbolFormPreference | None = None,
) -> list[CandleBar]:
    """Fetch ONE contract's OHLCV+OI bars over ``[start, end)`` — index-free, FNO.

    The "feed each CE/PE ``groww_symbol`` to ``get_historical_candles``" step:
    delegates to the shared
    :func:`~brutex.adapters.groww.candles.fetch_candle_windows` with
    ``segment="FNO"`` (:data:`OPTION_SEGMENT`) and ``drop_volume=False`` (options
    trade — volume + OI are kept). The exchange comes from the contract symbol's
    leading token.

    *on_raw_payload* is the OPTIONAL raw-data integrity capture hook
    (DEC-RAW-DATA-INTEGRITY-001), threaded straight to
    :func:`fetch_candle_windows`; ``None`` (the default) keeps the fetch
    byte-identical to the pre-feature path.

    *form_preference* (DEC-SYMBOL-FORM-PREFERENCE-001) is threaded into
    :func:`fetch_candle_windows` KEYED BY THE CONTRACT'S UNDERLYING, so one
    ``&``-symbol form win transfers across the whole chain (every M&M contract
    issues the learned winner first). ``None`` keeps the fixed order.

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
        on_raw_payload=on_raw_payload,
        form_preference=form_preference,
        preference_key=contract.underlying_symbol,
    )


@dataclass(frozen=True, slots=True)
class _OptionRawWindow:
    """The SUCCESS-path payload handed from the option PRODUCER half to the CONSUMER tail.

    The F&O twin of :class:`~brutex.adapters.groww.ingest._RawWindow`: a clean fetch (bars
    may be empty) carries its RAW bars + the per-window metadata the tail needs to
    session-filter + ``sink.append`` + conflict-aggregate + ``ledger.record``. Producer-side
    resolutions (skip / transient / terminal) never build one. Immutable so it crosses the
    producer→consumer queue safely.
    """

    contract: DerivativeContract
    interval: str
    exchange: str
    key: WindowKey
    raw_bars: list[CandleBar]
    fetch_count: int
    settle_empty: bool
    #: The pure "vendor data can never appear" fact (``clamped_end`` strictly before the
    #: OLD_GAP_HORIZON_DAYS cutoff) — threaded to ``ledger.record(old_window=…)`` so the
    #: old-gap settle is BUDGET-AWARE (DEC-DEAD-STRIKE-LEDGER-SETTLE-001): a reduced
    #: empty-retry budget (1 fetch/pull) accumulates clean-EMPTY confirmations ACROSS
    #: pulls in the durable ledger, settling a confirmed-dead deep strike at pull 2
    #: instead of the across-pull streak promotion's pull 3 (an honest 3-pull tax, not
    #: literal immortality; the genuinely unbounded shapes — a breaker-suppressed empty
    #: or a live-contract key reset — deliberately stay unsettled, data-safety first).
    old_window: bool
    attempt: int
    #: The ``in_pull_fetches`` SPLIT (DEC-RETRY-COUNTER-SPLIT-001), threaded to the
    #: tail's ``ledger.record(empty_retries=…, transient_retries=…)``: clean 0-bar
    #: responses vs retryable transients the producer's fetch ladder observed for
    #: this window. Unlike the spot payload (whose transient half is structurally
    #: zero — its ladder short-circuits on the first transient) this spine RETRIES
    #: transients inline, so a clean tail can carry BOTH halves; by construction
    #: ``fetch_count == empty_fetches + transient_fetches + (1 if raw_bars else 0)``.
    #: Defaulted 0 (the structural floor of a first-try win) so the fields ride
    #: the frozen payload additively; the producer always passes the real counts.
    empty_fetches: int = 0
    transient_fetches: int = 0
    #: The IST date of the SAME clock instant ``_clamped_window_key`` resolved
    #: inside ``ingest_option_contract`` (DEC-PULL-TODAY-COMPLETENESS-GATE-001)
    #: — the tail's covers-today predicate (``key.end == clamp_today + 1 day``)
    #: is judged against the date that MINTED the clamped end, so clamp and gate
    #: stay coherent across a midnight-crossing async drain (the tail NEVER
    #: reads a clock for the gate). ``None`` (a legacy/un-threaded constructor)
    #: keeps the gate INERT for the window — fail-open to pre-gate behavior.
    #: Appended LAST + defaulted (the ``empty_fetches`` additive precedent).
    clamp_today: date | None = None


def _clamped_window_key(
    contract_symbol: str,
    *,
    interval: str,
    start: date,
    end: date,
    market: str,
    now_for_clamp: datetime,
) -> WindowKey | None:
    """The canonical fetch-window key for one RESOLVED ``(contract, interval)`` range — or ``None``.

    The SINGLE source of truth for a derivative ledger key (CLAUDE.md §11 COMMON): BOTH
    the fetch (:func:`ingest_option_contract`, which records the landed outcome) AND the
    pre-plan (:func:`plan_contract_windows`, which seeds ``PLANNED``) build the key HERE,
    so a planned key is BYTE-IDENTICAL to the fetched key — the "planned == fetched"
    invariant that keeps a ``PLANNED`` row from becoming a permanent ghost it can never
    settle (the F&O twin of the spot spine's guarantee in
    :func:`brutex.data.window_ledger_store.plan_windows`).

    *start*/*end* are ALREADY resolved (post-:func:`_resolve_window`). The ``end`` is clamped
    EXACTLY as the spine clamps it (:func:`~brutex.adapters.ingest_common.clamp_window_end` —
    daily → ``today``; intraday → ``today + 1`` only from the 15:30 IST close). Returns
    ``None`` for an empty clamped range (``start >= clamped_end``) — exactly the case the
    fetch returns ``windows=0`` for and never touches the ledger, so that pair is not planned
    either. Pure + clock-injected (every branch unit-testable offline).
    """
    clamped_end = clamp_window_end(interval, end, now_ist=now_for_clamp)
    if start >= clamped_end:
        return None
    return WindowKey(
        market=market,
        security_id=contract_symbol,
        interval=interval,
        start=start,
        end=clamped_end,
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
    on_raw_payload: RawPayloadHook | None = None,
    fetch_pipeline: FetchPipeline | None = None,
    observed_holidays: frozenset[date] = frozenset(),
    empty_breaker: EmptySettleBreaker | None = None,
    storm_breaker: ErrorStormBreaker | None = None,
    write_breaker: WriteHealthBreaker | None = None,
    form_preference: SymbolFormPreference | None = None,
    market_closed: Callable[[], bool] | None = None,
    today_completeness_gate: bool = False,
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

    Rate admission (DEC-RATE-SINGLE-ADMISSION-001): when *client* already
    self-paces (a live client whose ``pace`` hook is bound to the shared bucket —
    the production wiring), the window-level *pace* is IGNORED: the client's
    per-attempt acquire is the single admission per physical fetch (retry attempts
    included). *pace* applies only for an unpaced client, so a fetch is never
    admitted twice against the 500/min governor.

    Raises
    ------
    GrowwError
        On a TERMINAL (non-retryable) fetch error — re-raised so the
        :func:`run_groww_option_ingest` quarantine sees it (auth included; Groww
        has no fail-fast latch — the same divergence as the spot spine).
    MarketHoursHalt
        DEC-GROWW-MARKET-HOURS-GUARD-001 — consulted FIRST (before the clamp /
        ledger-skip probe / any vendor call): a pull that crossed into the NSE
        trading window ``[08:45, 15:35)`` IST halts on THIS contract. A
        ``_GLOBAL_STOP_ERRORS`` member, so it propagates past the per-contract
        quarantine and stops the whole pull gracefully.
    """
    if market_closed is not None and market_closed():
        raise MarketHoursHalt()
    exchange = contract_exchange(contract)
    now_for_clamp = (now_ist or _now_ist)()
    # The window key is derived in ONE shared place (CLAUDE.md §11 COMMON) so the key the
    # planner seeds PLANNED is byte-identical to the key this fetch records (the
    # "planned == fetched" invariant). ``None`` == the empty clamped range the old inline
    # ``start >= clamped_end`` guard returned ``windows=0`` for.
    key = _clamped_window_key(
        contract.symbol,
        interval=interval,
        start=start,
        end=end,
        market=market,
        now_for_clamp=now_for_clamp,
    )
    if key is None:
        return IngestReport(windows=0, bars_appended=0, transient_failures=0)
    clamped_end = key.end
    # OLD-gap fast-settle cutoff (DEC-OLD-GAP-FAST-SETTLE-001): an expired contract's window
    # whose ``end`` is strictly before this date can never gain vendor data, so an in-pull-
    # confirmed-empty old window settles SKIPPED in ONE pull (the spot-spine twin). Computed
    # off the SAME injected clock the current-day clamp uses, so an injected ``now_ist`` makes
    # the decision deterministic.
    old_cutoff = now_for_clamp.date() - timedelta(days=OLD_GAP_HORIZON_DAYS)
    pull_id = current_pull_id()
    fields = {"symbol": contract.symbol, "interval": interval}
    if ledger is not None and ledger.is_skippable(key):
        _LOG.debug("option.ingest.window", pull_id=pull_id, action="skipped", **fields)
        return IngestReport(windows=1, bars_appended=0, transient_failures=0, skipped_windows=1)

    sleep = backoff if backoff is not None else _default_backoff
    empty_backoff = empty_retry_backoff if empty_retry_backoff is not None else _noop_empty_backoff
    # SINGLE ADMISSION (DEC-RATE-SINGLE-ADMISSION-001): a live client already self-paces
    # every physical attempt (``build_live_client`` / ``build_live_pool`` bake the shared
    # bucket's ``pace`` into ``GrowwClient._call`` — retry attempts included), so pacing
    # again per window here would admit each fetch TWICE — halving the achieved rate and
    # double-charging the 500/min governor. The client is the canonical admission point;
    # the window-level pace stays ONLY for an unpaced client (fakes / a caller that paces
    # solely at the window level).
    if client_self_paces(client):
        pace = None
    # SINGLE FEEDBACK (DEC-SUSPICION-SWEEP-FIXES-001, D2): a live client self-hooks —
    # ``build_live_client`` / ``build_live_pool`` bake the shared controller's
    # ``on_rate_limit`` into ``GrowwClient._call``, which fires it on EVERY 429
    # occurrence INCLUDING the final one before the exhausted error propagates here.
    # The spine firing its own hook again on that propagated error double-counted the
    # final occurrence — 7 ladder hits per exhausted window instead of 6, two+
    # step-downs from one window's storm — so it stands down (the feedback twin of
    # the ``client_self_paces`` stand-down above). Same for the per-window
    # ``on_success`` recovery credit (the client already recovers per clean call).
    # ``on_timeout`` stays spine-owned — the client never fires the timeout ladder.
    if client_self_reports_rate_limits(client):
        on_rate_limit = None
    if client_self_reports_successes(client):
        on_success = None
    attempt = 0
    # LOGICAL vendor fetches THIS pull makes for this window (DEC-WINDOW-INPULL-FETCHES-001)
    # — counts BOTH the transient re-attempts and the empty re-fetches, so the ledger row's
    # ``in_pull_fetches`` exposes the in-pull retry the per-attempt DEBUG log hid. HONEST
    # accounting note: this is the LOGICAL count — for an ``&``-symbol whose plain form
    # returns clean-empty, ONE logical fetch here can issue up to 2× PHYSICAL SDK calls
    # (the `%26` fallback inside ``fetch_candle_windows`` —
    # DEC-GROWW-AMPERSAND-CANDLE-FALLBACK-001); each physical call still rides
    # ``client.pace`` + the call meter, so rate admission sees every real call.
    fetch_count = 0
    # SPLIT counters (DEC-RETRY-COUNTER-SPLIT-001): how ``fetch_count`` decomposes —
    # clean 0-bar responses (the outer EMPTY-retry's evidence) vs retryable
    # transients the inner ladder absorbed — persisted as the ledger's
    # ``empty_retries`` / ``transient_retries``, so an EMPTY row's depth and a
    # FAILED row's re-attempts are never conflated again (the #924/#1355 gap).
    empty_fetches = 0
    transient_fetches = 0
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
                    client,
                    contract=contract,
                    interval=interval,
                    start=start,
                    end=clamped_end,
                    on_raw_payload=on_raw_payload,
                    form_preference=form_preference,
                )
                break
            except GrowwError as exc:
                if not is_retryable(exc):
                    # AUTHORISATION-storm observation (DEC-GROWW-AUTHZ-QUARANTINE-001):
                    # feed the terminal authorisation rejection to the per-pull
                    # breaker's authorisation arm BEFORE the quarantine raise (the
                    # terminal path previously never observed — only the exhausted
                    # retryable branch below did). The distinct-symbol unit is the
                    # UNDERLYING symbol (one underlying's whole multi-contract chain
                    # reads as ONE symbol, mirroring observe_window_failure). Below
                    # the threshold: a no-op + the per-contract quarantine raise; at
                    # the threshold: RAISES GrowwAuthorisationStormError — the
                    # fleet-wide account-suspension global stop. None (dry-run /
                    # synthetic / tests) observes nothing.
                    if isinstance(exc, GrowwAuthorisationError):
                        observe_authorisation_failure(
                            storm_breaker,
                            symbol=contract.underlying_symbol,
                            window_key=key,
                            exc=exc,
                        )
                    raise  # terminal (auth included) → outer quarantine, never swallowed
                # Transient: feed the controller (a 429 steps the rate down, a NON-auth
                # transient drives the timeout ladder — `steps_timeout_ladder`,
                # DEC-AUTH-LADDER-EXCLUDE-001: a retryable auth-family failure
                # (token-stale / mint-throttle) feeds NEITHER hook, it is no evidence
                # about candle-endpoint health) and record FAILED so the operator SEES
                # it — the ledger record + breaker feed below stay byte-identical.
                if is_rate_limit(exc):
                    if on_rate_limit is not None:
                        on_rate_limit()
                elif on_timeout is not None and steps_timeout_ladder(exc):
                    on_timeout()
                # One more retryable transient absorbed by this window's ladder
                # (DEC-RETRY-COUNTER-SPLIT-001) — counted BEFORE the record below
                # so the FAILED row's split includes THIS occurrence.
                transient_fetches += 1
                if ledger is not None:
                    ledger.record(
                        key,
                        raw_bars=0,
                        error_type=type(exc).__name__,
                        bars_appended=0,
                        bars_net_new=0,  # the failed fetch stored nothing (a known 0)
                        now=datetime.now(UTC),
                        in_pull_fetches=fetch_count,
                        empty_retries=empty_fetches,
                        transient_retries=transient_fetches,
                    )
                _LOG.warning(
                    "option.ingest.window.transient",
                    pull_id=pull_id,
                    error_type=type(exc).__name__,
                    attempt=attempt,
                    **fields,
                )
                if attempt >= max_attempts:
                    # Vendor-down error-storm breaker + dead-credential StillStale
                    # escalation (DEC-VENDOR-DOWN-BREAKER-001): this window's bounded
                    # transient-retry ladder is EXHAUSTED — feed the per-pull breaker
                    # AFTER the ledger record above (the window stays re-pullable). The
                    # storm's distinct-symbol unit is the UNDERLYING symbol — one
                    # underlying's whole multi-contract chain failing alone can never
                    # satisfy the >=3-distinct-symbols floor (the constants.py
                    # guarantee) — while the window identity stays the CONTRACT-level
                    # ledger `key`, so each contract window counts once per pull. A
                    # sustained zero-success storm across distinct underlyings RAISES
                    # VendorDownError; K consecutive StillStale outcomes over T minutes
                    # RAISE GrowwCredentialDeadError — both _GLOBAL_STOP_ERRORS members,
                    # so the driver halts the pull instead of quarantining per contract.
                    # None (dry-run / synthetic / tests) observes nothing.
                    observe_window_failure(
                        storm_breaker,
                        symbol=contract.underlying_symbol,
                        window_key=key,
                        exc=exc,
                    )
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
        empty_fetches += 1  # one more clean 0-bar response (DEC-RETRY-COUNTER-SPLIT-001)
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

    # Mass-empty circuit breaker (DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001 — the spot-spine
    # twin): feed this contract window's final fetch shape to the per-pull breaker. A
    # final-empty window counts toward the trip rules; a trip RAISES MassEmptySettleHalt
    # on this producer thread, which propagates as a GLOBAL stop (the pull halts loudly;
    # the tripping window is never recorded, so it stays retryable). A window whose OWN
    # ledger row already says EMPTY (a prior pull saw it empty — the empty-dominated
    # RESUME shape) or is REOPENED-marked (a deliberate `brutex reopen-skipped` re-check)
    # re-confirming empty is EXPECTED, not throttle evidence — observed but never counted
    # (`is_expected_empty_recheck`). Observed BEFORE the fast-settle math so a poisoned
    # window can never settle on the tripping call.
    # Vendor-down error-storm breaker (DEC-VENDOR-DOWN-BREAKER-001): a clean fetch —
    # bars OR a vendor-confirmed empty 200 — is a transport SUCCESS (the vendor answered:
    # it is ALIVE), so it resets BOTH the vendor-down storm and the StillStale streak.
    # Observed before the mass-EMPTY breaker feed (a separate, content-level verdict).
    # The UNDERLYING symbol mirrors the failure seam (the storm's per-underlying unit).
    observe_window_success(storm_breaker, symbol=contract.underlying_symbol)
    if empty_breaker is not None:
        series = f"{contract.symbol}:{interval}"
        if raw_bars:
            empty_breaker.observe_data(series)
        else:
            empty_breaker.observe_empty(
                series,
                symbol=contract.symbol,
                window_end=clamped_end,
                expected=ledger is not None and ledger.is_expected_empty_recheck(key),
            )
    # OLD-gap fast-settle (DEC-OLD-GAP-FAST-SETTLE-001 — the spot-spine twin): an EMPTY
    # window confirmed empty ACROSS an in-pull retry (``fetch_count >= 2``) AND OLD enough
    # that vendor data can never appear (``clamped_end`` strictly before ``old_cutoff``) is a
    # PERMANENT gap (an expired contract's dead period) → settle SKIPPED in ONE pull. A RECENT
    # empty window (or one not re-hit) keeps the conservative across-pull EMPTY streak.
    # Computed on the PRODUCER from the pure fetch facts; the tail RE-CHECKS the breaker at
    # record time (``allow_settle``), so a settle computed here but recorded after a trip is
    # still suppressed (DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001).
    #
    # BUDGET-AWARE cumulative settle (DEC-DEAD-STRIKE-LEDGER-SETTLE-001): ``is_old`` is
    # ALSO threaded to the tail's ``ledger.record(old_window=…)`` — a confirmed-deep
    # strike's REDUCED empty-retry budget of 1 (DEC-FETCH-PER-WINDOW-GUARANTEE-001) makes
    # ``fetch_count >= 2`` structurally unreachable in one pull, so the ledger accumulates
    # its genuine clean-EMPTY confirmations ACROSS pulls (``prior.empty_streak`` +
    # ``in_pull_fetches``) and settles once the SAME threshold is crossed — pull 2 instead
    # of the across-pull streak promotion's pull 3 (an honest one-fetch-per-pull tax that
    # was BOUNDED at 3 pulls for an expired contract, not literally infinite; the
    # genuinely unbounded shapes — breaker-suppressed empties, live-contract key resets —
    # deliberately stay unsettled: data-safety first).
    confirmed_empty_after_retry = (not raw_bars) and fetch_count >= CONFIRMED_EMPTY_MIN_FETCHES
    is_old = clamped_end < old_cutoff
    settle_empty = confirmed_empty_after_retry and is_old
    raw = _OptionRawWindow(
        contract=contract,
        interval=interval,
        exchange=exchange,
        key=key,
        raw_bars=raw_bars,
        fetch_count=fetch_count,
        settle_empty=settle_empty,
        old_window=is_old,
        attempt=attempt,
        empty_fetches=empty_fetches,
        transient_fetches=transient_fetches,
        # The TODAY-gate clamp date (DEC-PULL-TODAY-COMPLETENESS-GATE-001): the
        # SAME instant the clamp above used, so clamp and gate stay coherent.
        clamp_today=now_for_clamp.date(),
    )
    if fetch_pipeline is None:
        # Synchronous, byte-identical path: run the tail inline on THIS thread.
        return _process_option_window_tail(
            sink,
            raw,
            ledger=ledger,
            on_success=on_success,
            pull_id=pull_id,
            fields=fields,
            observed_holidays=observed_holidays,
            empty_breaker=empty_breaker,
            write_breaker=write_breaker,
            today_completeness_gate=today_completeness_gate,
        )
    # Async separation: hand the tail off to a background consumer. The producer does NOT
    # wait on it — it returns a DEFERRED IngestReport immediately and re-paces + fetches the
    # next contract. The consumer runs the SAME ``_process_option_window_tail`` then folds the
    # window's bars/conflicts into the pipeline's pull-level totals. A tail failure (disk-full
    # / sink error) is captured by the pipeline + re-raised on a later submit / drain.
    pipeline = fetch_pipeline

    # The per-series reconcile key MUST equal the key the spine stores this contract's
    # ``IngestReport`` under (``run_groww_option_ingest`` keys pairs by ``(contract.symbol,
    # interval)``), so the controller's post-drain reconcile lines up the folded consumer totals
    # with the right deferred report (the completion-count fix).
    series_key = (contract.symbol, interval)

    def _run_tail(_tail: FetchTail) -> None:
        result_report = _process_option_window_tail(
            sink,
            raw,
            ledger=ledger,
            on_success=on_success,
            pull_id=pull_id,
            fields=fields,
            observed_holidays=observed_holidays,
            empty_breaker=empty_breaker,
            write_breaker=write_breaker,
            today_completeness_gate=today_completeness_gate,
        )
        if result_report.still_failed_windows:
            # A tail-side PartitionLockTimeout DEFERRED this contract window (a wedged peer held
            # the cross-process lock past the deadline). Surface it via the pipeline's
            # tail-transient tally so it is counted + reported at drain and the across-pull retry
            # round still fires — otherwise an async-mode tail-transient was silently dropped
            # (MAJOR-2 fix). The window's ledger row is already recorded FAILED → re-pull refetches.
            pipeline.fold_tail_transient(series_key)
            return
        pipeline.fold_totals(
            bars_appended=result_report.bars_appended,
            conflicts=result_report.conflicts,
            series_key=series_key,
            vendor_dup_rows_dropped=result_report.vendor_dup_rows_dropped,
            max_close_delta=result_report.max_close_delta,
        )

    def _rehydrate(bars: Sequence[CandleBar]) -> None:
        # The pipeline's spill hook (DEC-FETCH-NONBLOCKING-HANDOFF-001): swap the window's bars
        # in place in the closure's captured ``raw`` so ``_process_option_window_tail`` reads the
        # (dropped-on-spill / restored-on-reload) bars. ``raw`` is frozen but ``raw_bars`` is a
        # mutable list — assigning its slice mutates the SAME object the tail reads.
        raw.raw_bars[:] = bars

    tail = FetchTail(
        shard_key=_option_series_shard_key(exchange, contract.symbol, interval),
        process=_run_tail,
        raw_bars=raw.raw_bars,
        rehydrate=_rehydrate,
    )
    fetch_pipeline.submit(RawFetchResult(tail=tail))
    # DEFERRED: the consumer owns the bars/conflicts (pull-level totals); the producer
    # reports a clean window with zero bars here (it is neither skipped nor transient).
    return IngestReport(
        windows=1,
        bars_appended=0,
        transient_failures=0,
        retried_windows=1 if attempt > 0 else 0,
    )


def _option_series_shard_key(exchange: str, contract_symbol: str, interval: str) -> str:
    """The consumer routing key for an F&O contract series — its partition coordinates.

    Mirrors the spot :func:`~brutex.adapters.groww.ingest._series_shard_key` (and
    :meth:`~brutex.data.write_consumer.WriteRequest.shard_key`) so all windows of one
    contract series route to ONE consumer (the per-partition lock invariant). The segment is
    always ``OPTION_SEGMENT`` (``"FNO"``) for the F&O lake.
    """
    return f"{exchange}\x00{OPTION_SEGMENT}\x00{contract_symbol}\x00{interval}"


def _process_option_window_tail(
    sink: BarSink,
    raw: _OptionRawWindow,
    *,
    ledger: WindowLedger | None,
    on_success: Callable[[], None] | None,
    pull_id: str,
    fields: dict[str, str],
    observed_holidays: frozenset[date] = frozenset(),
    empty_breaker: EmptySettleBreaker | None = None,
    write_breaker: WriteHealthBreaker | None = None,
    today_completeness_gate: bool = False,
) -> IngestReport:
    """The CONSUMER half — session-filter + sink-append + conflict-aggregate + ledger.record.

    Runs on the consumer thread (async mode) or inline on the fetch thread (sync mode) — the
    SAME code either way (CLAUDE.md §11 COMMON). The durable write goes through
    :func:`~brutex.adapters.groww.write_deadline.append_with_deadline`, so a wedged
    fsync/replace that NEVER raises (a hung durable write) is bounded by
    :data:`~brutex.adapters.groww.constants.GROWW_WRITE_DEADLINE_S` and surfaces as a
    :class:`~brutex.data.errors.WriteDeadlineExceeded` instead of blocking the fetch lane
    forever (DEC-FETCH-HUNG-WRITE-DEADLINE-001). BOTH that and a
    :class:`PartitionLockTimeout` (the cross-process lock ACQUIRE bound) DEFER the window
    (record FAILED + return a transient :class:`IngestReport`) — OUR contention / pipeline
    stall, not a fatal halt, so the contract window re-pulls next round and the fetch lane is
    freed. Any other sink error (a :class:`~brutex.data.errors.LakeWriteHalt` disk-full)
    propagates — in async mode the pipeline captures it into its pool-wide stop, re-raised on
    the next submit / drain so the pull halts through ``_GLOBAL_STOP_ERRORS``.
    """
    contract = raw.contract
    interval = raw.interval
    exchange = raw.exchange
    key = raw.key
    raw_bars = raw.raw_bars
    fetch_count = raw.fetch_count
    # Breaker re-check at RECORD time (DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001): once the
    # pull's mass-empty breaker trips, NO further window may settle SKIPPED — including a
    # settle computed on the producer before the trip but recorded after it (an
    # async-pipeline queued tail / a sibling in-flight worker). The suppressed empty is
    # recorded UNTRUSTED (EMPTY, no streak bump — ``suppress_settle``): fully retryable,
    # no poisoned progress toward the across-pull promotion.
    suppress_settle = empty_breaker is not None and not empty_breaker.allow_settle()
    settle_empty = raw.settle_empty and not suppress_settle
    attempt = raw.attempt
    # CALENDAR-AWARE session filter (DEC-INGEST-BOUNDARY-INTEGRITY-001) — the F&O
    # twin of the spot boundary: bars outside their OWN IST date's real NSE
    # session (pre-open/post-close AND non-trading-date leaks) are dropped before
    # the lake write; evening-muhurat / special-weekend bars are KEPT. Daily bars
    # carry no intraday time — deliberately unfiltered.
    if interval == _DAILY:
        bars = raw_bars
    else:
        session_verdict = filter_to_trading_session(raw_bars)
        bars = session_verdict.kept
        if session_verdict.dropped:
            # EXACTLY ONE aggregated WARNING per window (never per bar), carrying
            # the window identity + per-cause counts.
            _LOG.warning(
                "option.ingest.session_filter.dropped",
                pull_id=pull_id,
                window_start=key.start.isoformat(),
                window_end=key.end.isoformat(),
                dropped=session_verdict.dropped,
                off_session=session_verdict.dropped_off_session,
                non_trading_date=session_verdict.dropped_non_trading_date,
                **fields,
            )
    # Drop any bar timestamped on a date STRICTLY AFTER the clamped window end
    # (DEC-CANDLE-DROP-FUTURE-BAR-001): a vendor bar PAST the clamped bound would write a
    # PHANTOM FUTURE partition. ``key.end`` is the clamped end (``_clamped_window_key``); a
    # bar ON the boundary is KEPT, so this only ever removes an out-of-window / clock-skew
    # phantom, never a legitimate in-range bar (existing behaviour byte-unchanged).
    bars = drop_bars_after(bars, key.end)
    # Pre-settle TODAY minute-completeness gate (DEC-PULL-TODAY-COMPLETENESS-GATE-001) —
    # the F&O twin of the spot tail's gate: a covers-today contract window
    # (``key.end == clamp_today + 1 day``) whose fetched TODAY bars carry the #837
    # still-settling shape (OI-null on FUT/CE/PE; all-zero-volume on FUT) must NOT store
    # today's bars (keep-first would freeze the forming rows FOREVER) nor settle SUCCESS.
    # The guarded wrapper returns ``None`` (⇒ append the ORIGINAL bars, never demote) for
    # every inert/abstain shape — a legacy payload, a non-1minute interval, a non-today
    # window, a raw-EMPTY fetch (the EMPTY machinery; the F&O dry-run client returns EMPTY
    # payloads, so the gate can never fire offline), zero today bars (a dead strike /
    # dormant far-month contract legitimately prints nothing), out-of-coverage /
    # non-trading dates, or an internal error (fail-open). On a FAIL only the bars
    # strictly BEFORE clamp_today are appended and the record below is demoted under the
    # streak-EXEMPT ``TODAY_INCOMPLETE`` reason. The FUT count-vs-expected check is
    # OBSERVE-ONLY (a far-serial-month future is legitimately sparse intraday).
    today_incomplete = False
    append_bars = bars
    if today_completeness_gate:
        gate_result = observe_today_completeness(
            _LOG,
            bars=bars,
            clamp_today=raw.clamp_today,
            window_end=key.end,
            instrument_class=(
                InstrumentClass.FUT if contract.contract_type == "FUT" else InstrumentClass.OPTION
            ),
            symbol=contract.symbol,
            interval=interval,
            pull_id=pull_id,
            raw_count=len(raw_bars),
            fields={
                "window_start": key.start.isoformat(),
                "window_end": key.end.isoformat(),
                **fields,
            },
        )
        if gate_result is not None and gate_result[1].incomplete:
            today_incomplete = True
            append_bars = gate_result[0].prior
    # The write-health GROUP key (DEC-WRITE-HEALTH-BREAKER-001) — grouped by the
    # UNDERLYING, not the contract: each F&O ``(contract, interval)`` pair has exactly
    # ONE clamped ledger window per pass (``_clamped_window_key``) and the tail has NO
    # in-pass retry (a write defer folds to ``tail_transients`` → the NEXT pull, which
    # builds a FRESH breaker), so a per-CONTRACT key could never reach the shipped
    # ``WRITE_BREAKER_CONSECUTIVE_LIMIT`` of 2 — the breaker was structurally INERT on
    # this spine (review MAJOR P3-1). Grouping by underlying lets a wedged volume's
    # defers on SIBLING contracts of one chain accumulate into a real streak, while a
    # durable write on ANY sibling still resets it. Built from what this tail already
    # has (no extra lookup); a blank underlying falls back to the contract symbol so
    # the key is never empty.
    health_key = partition_health_key(
        exchange=exchange,
        segment=OPTION_SEGMENT,
        symbol=contract.underlying_symbol or contract.symbol,
        interval=interval,
        month=key.start.strftime("%Y-%m"),
    )
    # ALL-FILTERED at the WHOLE boundary (DEC-DATA-HONESTY-ALLFILTERED-SEED-001,
    # widening DEC-INGEST-BOUNDARY-INTEGRITY-001's session-filter-only check): the
    # kept count is read AFTER the session filter AND the future-bar drop, so a
    # window emptied by EITHER stage is caught. The shared WARNING always fires
    # on the shape and carries the window bounds (spot-parity triage fields);
    # only the ledger demotion below is interval-gated.
    all_boundary_filtered = observe_all_filtered(
        _LOG,
        raw_count=len(raw_bars),
        kept_count=len(bars),
        pull_id=pull_id,
        fields={
            "window_start": key.start.isoformat(),
            "window_end": key.end.isoformat(),
            **fields,
        },
    )
    all_filtered = interval != _DAILY and all_boundary_filtered
    try:
        # Bound the durable write by a wall-clock deadline so a wedged fsync/replace (a hung
        # durable write that never raises) can NEVER block this consumer — and therefore the
        # back-pressured producer / fetch lane — forever (DEC-FETCH-HUNG-WRITE-DEADLINE-001).
        result = append_with_deadline(
            sink,
            exchange=exchange,
            segment=OPTION_SEGMENT,
            symbol=contract.symbol,
            interval=interval,
            # The TODAY-gate write guard: on a today-incomplete verdict only the
            # settled pre-today history is appended (``append_bars``); every
            # other path appends the ORIGINAL ``bars`` object — byte-identical.
            bars=append_bars,
        )
    except (PartitionLockTimeout, WriteDeadlineExceeded) as exc:
        # DEFER this contract window to the next pull, exactly like a 429 / timeout transient —
        # record FAILED + return the transient IngestReport (NOT a contract-quarantine, NOT a
        # rate-controller step: a lock-acquire timeout is OUR partition contention and a
        # write-deadline is OUR wedged-disk hang — both are our-pipeline stalls, not vendor
        # pressure). The window re-pulls next round; the worker / fetch lane is freed instead of
        # pinned until the 600 s watchdog kill. ``error_type`` (the class name) distinguishes
        # ``PartitionLockTimeout`` (lock acquire) from ``WriteDeadlineExceeded`` (hung write).
        defer_seconds = (
            exc.timeout_seconds if isinstance(exc, PartitionLockTimeout) else exc.deadline_seconds
        )
        _LOG.warning(
            "option.ingest.window.write_deferred",
            pull_id=pull_id,
            error_type=type(exc).__name__,
            defer_seconds=defer_seconds,
            **fields,
        )
        if ledger is not None:
            ledger.record(
                key,
                raw_bars=0,
                error_type=type(exc).__name__,
                bars_appended=0,
                bars_net_new=0,  # the deferred write stored nothing (a known 0)
                now=datetime.now(UTC),
                in_pull_fetches=fetch_count,
                # The SPLIT (DEC-RETRY-COUNTER-SPLIT-001): the fetch ladder's own
                # facts — a write defer is OUR stall, not a vendor transient, so
                # the counts pass through unbumped.
                empty_retries=raw.empty_fetches,
                transient_retries=raw.transient_fetches,
            )
        # Cross-window write-health accounting (DEC-WRITE-HEALTH-BREAKER-001): AFTER the
        # ledger row is durably FAILED (so the deferred window is never lost even if the
        # breaker trips here), observe the defer; a volume-wedged trip raises
        # LakeWriteHalt("volume_wedged") which propagates as a global stop.
        if write_breaker is not None:
            write_breaker.observe_defer(health_key)
        return IngestReport(
            windows=1,
            bars_appended=0,
            transient_failures=1,
            retried_windows=1 if attempt > 0 else 0,
            still_failed_windows=1,
        )
    if write_breaker is not None:
        # A durable write landed on this partition — reset its consecutive-defer streak.
        write_breaker.observe_success(health_key)
    conflicts = [c for wr in result.write_results for c in wr.conflicts]
    if conflicts:
        _LOG.warning(
            "option.ingest.conflict.summary",
            pull_id=pull_id,
            **summarize_conflicts(conflicts),
            **fields,
        )
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
            # Persist the sink's keep-first-DEDUPED net-new count alongside the
            # input count (DEC-INGEST-BOUNDARY-INTEGRITY-001).
            bars_net_new=result.bars_net_new,
            now=datetime.now(UTC),
            in_pull_fetches=fetch_count,
            # The SPLIT (DEC-RETRY-COUNTER-SPLIT-001): unlike the spot tail this
            # spine retries transients INLINE, so a clean tail can carry both
            # halves (e.g. one 429 then bars ⇒ transient_retries=1, empty
            # retries 0, in_pull_fetches 2).
            empty_retries=raw.empty_fetches,
            transient_retries=raw.transient_fetches,
            settle_empty=settle_empty,
            # ALL-FILTERED demotion (DEC-INGEST-BOUNDARY-INTEGRITY-001, boundary-
            # widened by DEC-DATA-HONESTY-ALLFILTERED-SEED-001): the vendor
            # returned bars but EVERY one was removed at the filter boundary
            # (session filter AND/OR future-bar drop) — a would-be SUCCESS with
            # ZERO stored bars the skip rule would never revisit. This is the
            # F&O spine's ONLY demotion: #1031 deliberately kept the F&O
            # interior-gap/partial-window detectors observe-only (option sparsity is
            # indistinguishable from truncation), and that stance is PRESERVED —
            # all-filtered is a narrower, INTRINSIC signal (vendor sent bars, the
            # boundary kept none — not a liquidity heuristic), so it is UNgated
            # here: the F&O dry-run client returns EMPTY candle payloads
            # (``raw_bars == 0`` → EMPTY, this flag can never fire offline).
            # Orthogonal to sink dedup — a full-dedup re-pull keeps ``bars``
            # non-empty and stays SUCCESS.
            # Demotion priority (DEC-PULL-TODAY-COMPLETENESS-GATE-001 Ruling
            # G-5): the TODAY gate's verdict WINS when it fired — its reason is
            # streak-EXEMPT, so the streak-BUMPING ALL_FILTERED must not own a
            # covers-today window (overlap is narrow anyway: an all-filtered
            # covers-today FNO window has zero today bars, which ABSTAINS the
            # gate, so ALL_FILTERED fires exactly as today).
            degraded=today_incomplete or all_filtered,
            degraded_reason=(
                TODAY_INCOMPLETE if today_incomplete else (ALL_FILTERED if all_filtered else None)
            ),
            old_window=raw.old_window,
            suppress_settle=suppress_settle,
        )
    # Observe-only (DEC-PARTIAL-WINDOW-DETECT-001): for a FUTURES window only (options are
    # excluded — sparsity near expiry is indistinguishable from truncation), WARN if its
    # SESSION-FILTERED bars stop materially before the expected end (a truncation the SUCCESS
    # classifier + skip rule would otherwise never revisit). Fully guarded inside — never
    # breaks the pull, changes no status/ledger/return.
    _observe_partial_window(raw, bars, pull_id=pull_id, fields=fields, observed=observed_holidays)
    # WARN-only per-session BAR-DENSITY census (DEC-SESSION-DENSITY-CENSUS-001):
    # the density twin of the truncation observe above, same FUT-only scope gate
    # (options structurally excluded — sparsity is indistinguishable from a hole,
    # partial_window_detect.py's BLOCKER-1 rationale). Observe-only, fully
    # guarded inside, zero ledger effect.
    _observe_session_density(raw, bars, pull_id=pull_id, fields=fields, observed=observed_holidays)
    if on_success is not None:
        on_success()
    return IngestReport(
        windows=1,
        bars_appended=result.bars_appended,
        transient_failures=0,
        conflicts=len(conflicts),
        retried_windows=1 if attempt > 0 else 0,
        vendor_dup_rows_dropped=result.vendor_dup_rows_dropped,
        max_close_delta=result.max_close_delta,
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
    def conflicts(self) -> int:
        """Total keep-first vendor re-statements across all successful pairs."""
        return sum(r.conflicts for r in self.pairs.values())

    @property
    def vendor_dup_rows_dropped(self) -> int:
        """Total within-batch duplicate vendor rows dropped across all pairs (sum-fold)."""
        return sum(r.vendor_dup_rows_dropped for r in self.pairs.values())

    @property
    def max_close_delta(self) -> Decimal | None:
        """MAX absolute keep-first close delta across all pairs (exact Decimal; ``None`` = none)."""
        delta: Decimal | None = None
        for r in self.pairs.values():
            delta = fold_max_close_delta(delta, r.max_close_delta)
        return delta

    @property
    def failed_pairs(self) -> int:
        """Number of ``(contract, interval)`` pairs quarantined on a terminal error."""
        return len(self.failures)

    @property
    def auth_aborted(self) -> bool:
        """True iff any quarantined failure was a Groww auth failure (re-auth needed)."""
        return any(f.auth_failure for f in self.failures)


# ──────────────────────────────────────────────────────────────────────────
# "Suspicious empty" diagnostic (DEC-SUSPICIOUS-EMPTY-FLAG-001) — OBSERVE-ONLY
#
# When a whole F&O underlying (e.g. ``BAJAJ-AUTO`` / ``M&M``) is pulled and EVERY
# one of its cleanly-fetched contracts comes back empty, the underlying currently
# vanishes silently — indistinguishable from a legit "no data" gap. This surfaces
# that case as ONE structured WARNING for human triage, mirroring the read-side
# ``completeness.symbol_reconcile_mismatch`` observe-only diagnostic (#907): it
# changes NO return value, NO ledger row, NO engine input — the brute-force ENGINE
# stays NIFTY/BANKNIFTY/SENSEX (CLAUDE.md §3). It is PULL-side (not read-side)
# because only the pull knows "tried-and-empty" vs "never-tried" — a lake scan sees
# only "0 partitions" and cannot tell a clean-but-empty vendor response from an
# underlying we never pulled.
# ──────────────────────────────────────────────────────────────────────────

#: The structlog event a whole-underlying 100%-empty F&O pull emits (observe-only;
#: the pull-side twin of ``completeness.RECONCILE_MISMATCH_EVENT``).
UNDERLYING_ALL_EMPTY_EVENT = "pull.underlying_all_empty"
#: Emitted if the observe-only scan itself raises — it NEVER breaks the pull.
UNDERLYING_ALL_EMPTY_ERROR_EVENT = "pull.underlying_all_empty_error"
#: Minimum CLEANLY-fetched contracts an underlying must have before a 100%-empty
#: result is "suspicious" UNCONDITIONALLY (Tier A — non-clean siblings ignored, as
#: shipped by DEC-SUSPICIOUS-EMPTY-FLAG-001). Below this, a 1-2-leg illiquid chain
#: is flagged ONLY by the STRICTER low-leg Tier B rule
#: (DEC-SILENT-VANISH-LOWLEG-001): every pair seen for the underlying was a CLEAN
#: empty AND no sibling pair was skipped / transient / empty-clamped AND no sibling
#: leg was terminally quarantined — so the false-positive shapes the #919 guards
#: exclude (a re-pull, a transient storm, a clamped no-op, a quarantined leg) can
#: never trip the low-leg flag. Tunable constant.
_ALL_EMPTY_MIN_CONTRACTS = 3
#: Cap on the number of flagged underlyings NAMED per pull, so a pathological pull
#: cannot flood the log (mirrors completeness.py's sample cap).
_ALL_EMPTY_SAMPLE_CAP = 20


@dataclass(frozen=True, slots=True)
class UnderlyingEmptyReport:
    """One underlying whose every cleanly-fetched F&O contract came back empty.

    Attributes
    ----------
    underlying
        The underlying symbol (``DerivativeContract.underlying_symbol``), e.g.
        ``"BAJAJ-AUTO"``.
    attempted_pairs
        Count of CLEANLY-fetched ``(contract, interval)`` pairs for this underlying
        — a real vendor fetch that could have stored bars (a ledger-skip, a
        transient residual, and an empty-clamp no-op are EXCLUDED; see
        :func:`_is_clean_fetch`).
    stored_bars
        Total bars stored across those attempted pairs — ``0`` for a flagged
        underlying (the whole chain produced nothing).
    low_leg
        ``True`` iff the flag fired via the LOW-LEG Tier B rule
        (DEC-SILENT-VANISH-LOWLEG-001 — ``attempted_pairs`` below
        :data:`_ALL_EMPTY_MIN_CONTRACTS`, every pair clean-empty, no non-clean or
        quarantined sibling). Surfaced in the WARNING payload so triage can weigh a
        1-leg flag lower than a whole-chain Tier A flag. HONEST caveat (Golden Rule
        #1/#10): a genuinely-illiquid 1-leg contract that truly never traded is
        INDISTINGUISHABLE at the pull's vantage point from a vendor silent-vanish —
        whether Groww returns a clean-200-empty for such legs is UNVERIFIED from an
        offline sandbox; Tier B trades that bounded warning noise for coverage and
        stays observe-only.
    never_planned_pairs
        Count of this underlying's contracts that were NEVER PLANNED this pull —
        far-future legs skipped at planning time (``fno.window_not_open``,
        DEC-FNO-FAR-EXPIRY-WINDOW-001: zero vendor calls, zero ledger rows, no
        ``IngestReport`` pair) — threaded in via
        :func:`scan_underlying_all_empty`'s *never_planned_symbols*
        (DEC-SILENT-VANISH-LOWLEG-ANNOTATE-001). ANNOTATION ONLY: the flag
        decision never reads it — a low-leg flag with
        ``never_planned_pairs >= 1`` still fires, it just SELF-DESCRIBES as a
        probable far-leg-not-open chain so triage can weigh it lower. Default
        ``0`` keeps every pre-existing construction byte-identical.
    """

    underlying: str
    attempted_pairs: int
    stored_bars: int
    low_leg: bool = False
    never_planned_pairs: int = 0


@dataclass(frozen=True, slots=True)
class AllEmptyScan:
    """The observe-only result of scanning one pull's pairs for whole-underlying empties."""

    suspicious: tuple[UnderlyingEmptyReport, ...]

    def has_suspicious(self) -> bool:
        """True iff at least one underlying tripped the flag (else the emit stays silent)."""
        return bool(self.suspicious)


def _is_clean_fetch(report: IngestReport) -> bool:
    """True iff *report* is a CLEAN vendor fetch (could have stored bars), not a skip.

    A ledger-skip (``skipped_windows``), a transient residual (``still_failed_windows``),
    and an empty-clamp no-op (``windows == 0``) are EXCLUDED — none is a clean
    tried-and-empty vendor response, so none counts toward "attempted". This is what
    keeps a re-pull (all already-settled) and a still-draining transient storm from
    tripping the flag (Golden Rule #1 — never-tried is not tried-and-empty).
    """
    return report.windows >= 1 and report.skipped_windows == 0 and report.still_failed_windows == 0


def scan_underlying_all_empty(
    pairs: Mapping[tuple[str, str], IngestReport],
    contract_list: Sequence[DerivativeContract],
    failed_symbols: Iterable[str] = (),
    never_planned_symbols: Iterable[str] = (),
) -> AllEmptyScan:
    """Scan one pull's ``pairs`` for underlyings whose every cleanly-fetched contract was empty.

    Groups the ``(contract symbol, interval)`` pairs by their contract's
    ``underlying_symbol`` (resolved via *contract_list*), counts the CLEANLY-fetched
    pairs (:func:`_is_clean_fetch` — the ONE COMMON per-pair guard predicate, untouched)
    per underlying, and flags an underlying whose clean pairs stored ZERO bars in total
    iff EITHER tier holds:

    * **Tier A** (DEC-SUSPICIOUS-EMPTY-FLAG-001, unchanged): at least
      :data:`_ALL_EMPTY_MIN_CONTRACTS` clean fetches — non-clean / quarantined
      siblings are ignored exactly as shipped (never stricter).
    * **Tier B — low-leg** (DEC-SILENT-VANISH-LOWLEG-001): 1-2 clean fetches AND
      ZERO non-clean sibling pairs (no ledger-skip / transient residual /
      empty-clamp no-op — the pairs previously ``continue``-dropped are now
      COUNTED per underlying so a 1-clean-empty + 2-skipped chain is
      distinguishable from a genuinely 1-leg all-clean-empty chain) AND ZERO
      terminally-quarantined sibling legs (*failed_symbols* — invisible to
      *pairs*, so it must be threaded in). This keeps every #919 false-positive
      shape excluded: a re-pull, a transient storm, a clamped range, and a
      quarantined leg each leave a non-clean/failed sibling that disqualifies the
      whole underlying at the low-leg tier.

    HONEST residual (now SELF-DESCRIBED, still never suppressed —
    DEC-SILENT-VANISH-LOWLEG-ANNOTATE-001): a NEVER-PLANNED sibling — a
    far-future contract skipped at planning time (``fno.window_not_open``,
    DEC-FNO-FAR-EXPIRY-WINDOW-001: zero vendor calls, zero ledger rows, no
    ``IngestReport`` pair, no failure) — stays structurally invisible to both the
    non-clean tally and *failed_symbols*, so a chain of one clean-empty near leg +
    one not-yet-open far leg STILL flags ``low_leg=True`` (annotate, NEVER
    suppress — the flag decision expression is untouched). What changed: the
    caller may thread *never_planned_symbols* (the planner's omitted set) and
    each flagged report then carries ``never_planned_pairs``, so the WARN reads
    e.g. ``low_leg=True never_planned_pairs=1`` — "probably a
    far-leg-not-open chain, weigh lower" — instead of leaving triage guessing.
    Consistent with the tried-vs-never-tried philosophy (Tier A ignores
    never-tried siblings identically); observe-only.

    Pure + O(pairs + failures); a pair (or failed symbol) not in *contract_list* is
    skipped (defensive — never raises).

    Parameters
    ----------
    pairs
        The ``(symbol, interval) -> IngestReport`` map a pull assembled.
    contract_list
        The contracts the pull attempted (carries each symbol's underlying).
    failed_symbols
        Contract symbols quarantined on a TERMINAL error this pull
        (:class:`OptionPairFailure` symbols / the post-drain phase's failure set) —
        they never enter *pairs*, so without them a 2-leg chain with one
        quarantined leg would be indistinguishable from a 1-leg clean-empty chain.
        Default empty (Tier A behaviour is failure-blind, unchanged).
    never_planned_symbols
        Contract symbols the PLANNER omitted entirely this pull (the not-yet-open
        far-future legs of DEC-FNO-FAR-EXPIRY-WINDOW-001 — zero vendor calls, zero
        ledger rows, so they can never appear in *pairs* or *failed_symbols*).
        Tallied per underlying onto each flagged report's ``never_planned_pairs``
        — ANNOTATION ONLY, the flag decision never reads it
        (DEC-SILENT-VANISH-LOWLEG-ANNOTATE-001). An unknown symbol is
        defensively skipped (same as *failed_symbols*). Default empty —
        byte-identical scan.

    Returns
    -------
    AllEmptyScan
        The flagged underlyings (sorted by symbol), empty when nothing is suspicious.
    """
    symbol_to_underlying = {c.symbol: c.underlying_symbol for c in contract_list}
    attempted: dict[str, int] = {}
    stored: dict[str, int] = {}
    nonclean: dict[str, int] = {}
    for (symbol, _interval), report in pairs.items():
        underlying = symbol_to_underlying.get(symbol)
        if underlying is None:
            continue
        if not _is_clean_fetch(report):
            nonclean[underlying] = nonclean.get(underlying, 0) + 1
            continue
        attempted[underlying] = attempted.get(underlying, 0) + 1
        stored[underlying] = stored.get(underlying, 0) + report.bars_appended
    failed_underlyings = {
        underlying
        for symbol in failed_symbols
        if (underlying := symbol_to_underlying.get(symbol)) is not None
    }
    never_planned: dict[str, int] = {}
    for symbol in never_planned_symbols:
        underlying = symbol_to_underlying.get(symbol)
        if underlying is None:  # defensive skip, same as failed_symbols
            continue
        never_planned[underlying] = never_planned.get(underlying, 0) + 1
    flagged = tuple(
        UnderlyingEmptyReport(
            underlying=underlying,
            attempted_pairs=attempted[underlying],
            stored_bars=stored[underlying],
            low_leg=attempted[underlying] < _ALL_EMPTY_MIN_CONTRACTS,
            never_planned_pairs=never_planned.get(underlying, 0),
        )
        for underlying in sorted(attempted)
        if stored[underlying] == 0
        and (
            attempted[underlying] >= _ALL_EMPTY_MIN_CONTRACTS  # Tier A (unchanged)
            or (  # Tier B low-leg: EVERY pair clean-empty, no failed/non-clean sibling
                nonclean.get(underlying, 0) == 0 and underlying not in failed_underlyings
            )
        )
    )
    return AllEmptyScan(suspicious=flagged)


def _log_underlying_all_empty(scan: AllEmptyScan, *, pull_id: str | None) -> None:
    """Emit ONE observe-only WARNING per flagged underlying (capped), else stay silent.

    Mirrors ``completeness._log_symbol_reconcile_mismatch`` (#907): early-returns in
    the healthy steady state (nothing suspicious), and otherwise names each underlying
    via ``repr()`` with its attempted/stored counts. Observe-only — it triggers no
    re-pull and no engine action.
    """
    if not scan.has_suspicious():
        return
    for rep in scan.suspicious[:_ALL_EMPTY_SAMPLE_CAP]:
        _LOG.warning(
            UNDERLYING_ALL_EMPTY_EVENT,
            pull_id=pull_id,
            underlying=repr(rep.underlying),
            attempted_pairs=rep.attempted_pairs,
            stored_bars=rep.stored_bars,
            low_leg=rep.low_leg,
            never_planned_pairs=rep.never_planned_pairs,
        )


def emit_underlying_all_empty(
    pairs: Mapping[tuple[str, str], IngestReport],
    contract_list: Sequence[DerivativeContract],
    *,
    failed_symbols: Iterable[str] = (),
    never_planned_symbols: Iterable[str] = (),
    pull_id: str | None = None,
) -> None:
    """Scan one pull's *pairs* for whole-underlying empties + WARN — fully guarded, observe-only.

    The ONE source of truth (CLAUDE.md §11 COMMON) for the
    DEC-SUSPICIOUS-EMPTY-FLAG-001 diagnostic's guarded scan→log: it runs
    :func:`scan_underlying_all_empty` over *pairs* / *contract_list* and hands the
    result to :func:`_log_underlying_all_empty`, the whole thing wrapped so the
    observe-only diagnostic can NEVER break the pull (a scan-internal error logs
    :data:`UNDERLYING_ALL_EMPTY_ERROR_EVENT` and returns). Reused by BOTH the
    synchronous in-driver call (:func:`run_groww_option_ingest`) AND the async
    POST-DRAIN production call site (DEC-EMPTY-DETECTOR-PRODUCTION-WIRING-001 — the
    F&O pull always wires a ``fetch_pipeline``, so the in-driver scan is SKIPPED and
    the real per-pair bar counts are only known after the pipeline drain + reconcile;
    the post-drain call over the RECONCILED pairs catches the exact BAJAJ-AUTO/M&M
    silent-vanish case the in-driver skip would otherwise leave uncatchable).

    Changes NO return value, NO ledger row, NO engine input — the brute-force ENGINE
    stays NIFTY/BANKNIFTY/SENSEX (CLAUDE.md §3). *pull_id* defaults to the current
    pull-scope id (:func:`~brutex.observability.correlation.current_pull_id`) so an
    out-of-scope caller need not thread it; pass it explicitly to override.
    *failed_symbols* (DEC-SILENT-VANISH-LOWLEG-001) is the pull's terminally
    quarantined contract symbols, threaded to
    :func:`scan_underlying_all_empty` so the LOW-LEG Tier B rule can disqualify an
    underlying with a quarantined sibling leg (quarantines never enter *pairs*);
    default empty keeps the Tier A behaviour byte-identical.
    *never_planned_symbols* (DEC-SILENT-VANISH-LOWLEG-ANNOTATE-001) is the
    planner's omitted not-yet-open contract set, threaded to
    :func:`scan_underlying_all_empty` so a Tier-B low-leg flag SELF-DESCRIBES via
    ``never_planned_pairs`` (annotation only — never a suppression); the in-driver
    synchronous call site leaves it default-empty because the DRIVER has no
    planning info (the planner's omitted set lives in
    ``dashboard.historical_pull``, the production post-drain caller).
    """
    resolved_pull_id = pull_id if pull_id is not None else current_pull_id()
    try:
        _log_underlying_all_empty(
            scan_underlying_all_empty(pairs, contract_list, failed_symbols, never_planned_symbols),
            pull_id=resolved_pull_id,
        )
    except Exception as exc:  # observe-only diagnostic must never break the pull
        _LOG.warning(UNDERLYING_ALL_EMPTY_ERROR_EVENT, pull_id=resolved_pull_id, error=str(exc))


#: F&O contract types the partial-window truncation detector WATCHES. Only FUTURES
#: (``FUT``) — futures trade densely every session, so a truncated futures window is a
#: genuine, reliably-detectable fetch gap. OPTIONS (``CE`` / ``PE``) are EXCLUDED by
#: design: an option routinely stops printing bars several sessions before expiry
#: (liquidity death in the final week) even though the contract is COMPLETE, so a
#: truncation there is indistinguishable from sparsity — watching the chain would flag
#: constantly (a false-positive storm, BLOCKER-1 of the hostile review).
_PARTIAL_WINDOW_WATCHED_CONTRACT_TYPES: frozenset[str] = frozenset({"FUT"})


def _observe_partial_window(
    raw: _OptionRawWindow,
    session_bars: list[CandleBar],
    *,
    pull_id: str | None,
    fields: dict[str, str],
    observed: frozenset[date] = frozenset(),
) -> None:
    """Observe-only: WARN if a FUTURES SUCCESS window's bars stop before its end.

    DEC-PARTIAL-WINDOW-DETECT-001. SCOPE GATE (BLOCKER-1): only FUTURES (``FUT``)
    contracts are watched — OPTIONS (``CE`` / ``PE``) are EXCLUDED because near expiry
    an option's bars die from illiquidity, indistinguishable from truncation (see
    :data:`_PARTIAL_WINDOW_WATCHED_CONTRACT_TYPES` + the module docstring). For a watched
    contract it delegates to the COMMON
    :func:`~brutex.data.partial_window_detect.observe_partial_window` over the
    SESSION-FILTERED bars (MINOR-1 — not the raw bars), which runs the pure classifier +
    emits ONE guarded ``window.suspected_partial`` WARNING, never breaking the pull.
    """
    if raw.contract.contract_type not in _PARTIAL_WINDOW_WATCHED_CONTRACT_TYPES:
        return
    last_bar_ts = session_bars[-1].timestamp if session_bars else None
    observe_partial_window(
        _LOG,
        last_bar_ts=last_bar_ts,
        window_end=raw.key.end,
        now=datetime.now(UTC),
        expiry=raw.contract.expiry,
        symbol=raw.contract.symbol,
        interval=fields.get("interval", raw.interval),
        pull_id=pull_id,
        observed=observed,
    )


#: The ONE vendor-fetched F&O interval (DEC-1MINUTE-ONLY-DERIVE-1DAY-001) —
#: the density census's interval belt (DEC-SESSION-DENSITY-CENSUS-001): the
#: per-minute-bucket expectation math only makes sense on the 1-minute base.
_DENSITY_INTERVAL: Final[str] = "1minute"


def _observe_session_density(
    raw: _OptionRawWindow,
    session_bars: list[CandleBar],
    *,
    pull_id: str | None,
    fields: dict[str, str],
    observed: frozenset[date] = frozenset(),
) -> None:
    """Observe-only: WARN if a FUTURES window's sessions are bar-DENSITY deficient.

    DEC-SESSION-DENSITY-CENSUS-001 — the density twin of
    :func:`_observe_partial_window`, sharing its SCOPE GATE verbatim: only
    FUTURES (``FUT``) contracts are watched
    (:data:`_PARTIAL_WINDOW_WATCHED_CONTRACT_TYPES` — an option's intraday
    sparsity is indistinguishable from a hole, the BLOCKER-1 rationale), and
    only the fetched ``1minute`` interval is judged
    (:data:`_DENSITY_INTERVAL`). Delegates to the COMMON guarded
    :func:`~brutex.data.session_density.observe_session_density` over the
    SESSION-FILTERED bars — capped WARNs + the known-irregular INFO arm,
    never breaking the pull, changing no status/ledger/return. The F&O
    dry-run client returns EMPTY candle payloads, so this is silent offline.
    """
    if raw.contract.contract_type not in _PARTIAL_WINDOW_WATCHED_CONTRACT_TYPES:
        return
    if fields.get("interval", raw.interval) != _DENSITY_INTERVAL:
        return
    observe_session_density(
        _LOG,
        bar_timestamps=[bar.timestamp for bar in session_bars],
        symbol=raw.contract.symbol,
        interval=fields.get("interval", raw.interval),
        pull_id=pull_id,
        observed=observed,
    )


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


def plan_contract_windows(
    ledger: WindowLedger,
    *,
    contracts: Sequence[DerivativeContract],
    intervals: Sequence[str],
    start: date,
    end: date,
    market: str,
    contract_windows: ContractWindows | None = None,
    now_ist: Callable[[], datetime] | None = None,
) -> int:
    """Upsert every ``(contract, interval)`` window as ``PLANNED`` BEFORE any fetch — returns the count.

    The F&O twin of the spot spine's
    :func:`brutex.data.window_ledger_store.plan_windows` (DEC-FNO-WINDOW-PLAN-001).
    For each resolved derivative ``(contract, interval)`` pair it derives the SAME ledger
    key the fetch records (:func:`_clamped_window_key`, over the
    :func:`_resolve_window`-resolved range) and :meth:`~brutex.data.window_ledger.WindowLedger.plan`-s
    it — idempotent, so a settled (``SUCCESS`` / ``SKIPPED``) row from a prior pull is NEVER
    reset (the no-re-fetch guarantee). This is what gives the ``/coverage/windows`` Options +
    Futures tabs a real ``PLANNED`` total + units BEFORE any contract lands: every contract this
    pull will attempt has a row up front, so an un-reached / quarantined contract shows
    ``PLANNED`` (genuinely-outstanding, never silently missing), exactly like spot.

    The key derivation is BYTE-IDENTICAL to :func:`ingest_option_contract` (both call
    :func:`_clamped_window_key` over the SAME :func:`_resolve_window` resolution), so the
    planned key always equals the fetched key — no ghost ``PLANNED`` row that can never
    settle. A pair whose clamped range is empty (``start >= clamped_end``) yields no key and
    is not planned (the fetch returns ``windows=0`` for it and never touches the ledger
    either). SUPERSEDED rows of the planned series — a key a previous pull's shorter / longer
    bound produced as the global ``end`` advances across pulls — are pruned afterwards
    (:meth:`~brutex.data.window_ledger.WindowLedger.prune_superseded`,
    DEC-WINDOW-TRAILING-GC-001), so the ledger tracks the live tiling instead of accumulating
    one orphan per series per day.

    Each derivative ``(contract, interval)`` maps to a SINGLE coverage window (the contract's
    whole range — the vendor per-call tiling happens INSIDE ``fetch_candle_windows``, not in
    the ledger), so each ``planned_tiles`` series carries exactly one ``{start: end}`` entry.

    *market* is the namespace the matching ingest records under
    (:data:`~brutex.adapters.groww.constants.GROWW_OPTION_COVERAGE_MARKET` for options,
    :data:`~brutex.adapters.groww.constants.GROWW_FUTURE_COVERAGE_MARKET` for futures), so the
    planned rows land in the SAME bucket ``/coverage/windows`` reads. *now_ist* injects the
    clamp clock (the production default is the live IST wall-clock); the 15:30 IST boundary
    race (the clamp moving between this plan and a fetch crossing the close) self-heals on the
    next pull's plan→prune cycle, exactly as the spot planner documents.

    Returns the number of windows planned. ``O(contracts × intervals)`` plan + one
    ``O(rows)`` prune scan — NOT an O(1)-total claim (the per-window plan IS O(1)).
    """
    stamp = datetime.now(UTC)
    now_for_clamp = (now_ist or _now_ist)()
    planned_tiles: dict[tuple[str, str, str], dict[str, str]] = {}
    planned = 0
    for contract in contracts:
        for interval in intervals:
            window_start, window_end = _resolve_window(
                contract_windows,
                symbol=contract.symbol,
                interval=interval,
                start=start,
                end=end,
            )
            key = _clamped_window_key(
                contract.symbol,
                interval=interval,
                start=window_start,
                end=window_end,
                market=market,
                now_for_clamp=now_for_clamp,
            )
            if key is None:
                continue  # empty clamped range — the fetch plans/records nothing here either
            ledger.plan(key, now=stamp)
            planned_tiles[market, contract.symbol, interval] = {
                key.start.isoformat(): key.end.isoformat()
            }
            planned += 1
    pruned = ledger.prune_superseded(planned_tiles)
    if pruned:
        _LOG.info(
            "option.ingest.pruned_superseded",
            pull_id=current_pull_id(),
            pruned=pruned,
            planned=planned,
            market=market,
        )
    return planned


def _fno_reconcile_candidates(
    *,
    contracts: Sequence[DerivativeContract],
    intervals: Sequence[str],
    start: date,
    end: date,
    market: str,
    contract_windows: ContractWindows | None,
    now_for_clamp: datetime,
) -> Iterable[SeedCandidate]:
    """Yield ``(window_key, span_key)`` cold-start seed candidates for the F&O chain.

    The F&O twin of the spot spine's candidate generator inside
    :func:`brutex.data.window_ledger_store.reconcile_ledger_windows`. For each
    ``(contract, interval)`` pair it derives the SAME ledger key the fetch + the
    pre-plan record (:func:`_clamped_window_key` over the :func:`_resolve_window`-resolved
    range), so a seeded ``SUCCESS`` key is BYTE-IDENTICAL to the ``PLANNED`` key
    :func:`plan_contract_windows` upserts (the "reconcile == plan == fetch" invariant that
    lets the cold-start flip a would-be-PLANNED window to skippable ``SUCCESS`` — the F&O
    equivalent of the spot rescue). The lake span lookup key is the CONTRACT's F&O
    partition ``(exchange, "FNO", groww_symbol, interval)`` — the exact directory
    :func:`ingest_option_contract` writes to via ``sink.append(segment="FNO", …)`` and
    :func:`~brutex.dashboard.db_view.summarize_spot_datasets` (which walks the whole
    ``bars/`` tree, spot AND FNO) reports the stored span for. A pair whose clamped range
    is empty (``key is None``) is skipped — the fetch/plan touch nothing there either.
    """
    for contract in contracts:
        exchange = contract_exchange(contract)
        for interval in intervals:
            window_start, window_end = _resolve_window(
                contract_windows,
                symbol=contract.symbol,
                interval=interval,
                start=start,
                end=end,
            )
            key = _clamped_window_key(
                contract.symbol,
                interval=interval,
                start=window_start,
                end=window_end,
                market=market,
                now_for_clamp=now_for_clamp,
            )
            if key is None:
                continue
            yield key, (exchange, OPTION_SEGMENT, contract.symbol, interval)


def reconcile_fno_ledger_windows(
    ledger: WindowLedger,
    spans: Sequence[dict[str, object]],
    *,
    contracts: Sequence[DerivativeContract],
    intervals: Sequence[str],
    start: date,
    end: date,
    market: str,
    contract_windows: ContractWindows | None = None,
    now: datetime | None = None,
    now_ist: Callable[[], datetime] | None = None,
    span_index: LakeSpanIndex | None = None,
) -> int:
    """Seed ``SUCCESS`` (only-if-absent) for every F&O window the lake already ENCLOSES — **pure** cold-start.

    The F&O twin of the spot spine's
    :func:`brutex.data.window_ledger_store.reconcile_ledger_windows`
    (DEC-FNO-LEDGER-LAKE-RECONCILE-001). On a fresh clone / lost / orphaned ledger the F&O
    candle lake (``bars/<exch>/FNO/<groww_symbol>/<interval>/…``) may already hold years of
    contract bars while the ledger is empty, so once the ledger drives the skip a re-pull
    would re-fetch the ENTIRE options chain (every strike × CE/PE × expiry). The SPOT phase
    has had this rescue (:func:`reconcile_ledger_with_lake`) since DEC-WINDOW-LEDGER-001; the
    F&O phase never did — the gap this closes.

    For each ``(contract, interval)`` whose stored F&O-partition span ENCLOSES the pair's
    clamped window (``first_date <= window_start AND window_end − 1 day <= last_date``,
    span with ≥ 1 bar — the fetch-EXCLUSIVE-end enclosure rule of
    DEC-WINDOW-LEDGER-RESCUE-001, shared with the spot reconcile via the COMMON
    :func:`~brutex.data.window_ledger_store.seed_enclosed_windows`; the prior
    ``window_end <= last_date`` form compared the exclusive ``expiry + 1 day`` end
    against the bar-inclusive ``last_date <= expiry``, so this rescue was DEAD CODE —
    deterministically 0 windows even over a maximally dense lake), seed that window
    ``SUCCESS`` — but ONLY if ABSENT
    (:meth:`~brutex.data.window_ledger.WindowLedger.seed_lake_success` NEVER overwrites a
    ``FAILED`` / ``PLANNED`` / settled row, so it can never mask a re-pull). A
    genuinely-missing window (no span, or the span does not fully enclose it — a
    PARTIALLY-covered window) is left with NO seeded row, so it stays re-pullable. Reuses
    the COMMON seed engine :func:`~brutex.data.window_ledger_store.seed_enclosed_windows`
    over :func:`~brutex.data.window_ledger_store.index_lake_spans` (CLAUDE.md §11 COMMON —
    the spot + F&O reconciles share ONE engine, parameterised only by the key convention).
    O(contracts × intervals) with O(1) span lookups. Returns the number of windows seeded.

    *spans* is the :func:`~brutex.dashboard.coverage_reconcile.load_lake_spans` shape (spot
    AND FNO partitions in one read). *now* stamps the seeded rows; *now_ist* injects the
    current-day clamp clock so the reconcile key equals the plan/fetch key deterministically.
    *span_index* (round-2 review polish, DEC-PULL-SIX-STEP-ORDER-001): an OPTIONAL
    pre-built :func:`~brutex.data.window_ledger_store.index_lake_spans` index over the
    SAME *spans*, so a caller reconciling several buckets of one phase (the step-major
    4-bucket loop in :mod:`brutex.dashboard.historical_pull`) builds the O(spans) index
    ONCE instead of once per bucket. ``None`` (the default — every other caller) builds
    it here, byte-identically.
    """
    stamp = now if now is not None else datetime.now(UTC)
    now_for_clamp = (now_ist or _now_ist)()
    if span_index is None:
        span_index = index_lake_spans(spans)
    candidates = _fno_reconcile_candidates(
        contracts=contracts,
        intervals=intervals,
        start=start,
        end=end,
        market=market,
        contract_windows=contract_windows,
        now_for_clamp=now_for_clamp,
    )
    # The live-edge guard's "today" is the SAME injected IST clamp clock the key
    # derivation uses, so reconcile == plan == fetch stays deterministic offline.
    return seed_enclosed_windows(
        ledger, span_index, candidates, now=stamp, today_ist=now_for_clamp.date()
    )


def reconcile_fno_ledger_with_lake(
    ledger: WindowLedger,
    lake_root: Path,
    *,
    contracts: Sequence[DerivativeContract],
    intervals: Sequence[str],
    start: date,
    end: date,
    market: str,
    contract_windows: ContractWindows | None = None,
    now: datetime | None = None,
    now_ist: Callable[[], datetime] | None = None,
) -> int:
    """Load the lake spans + seed the F&O ledger cold-start (the wired entry point).

    Thin composition of :func:`~brutex.dashboard.coverage_reconcile.load_lake_spans` (the
    DuckDB span read — ONE O(datasets) aggregate scan covering spot AND FNO partitions) +
    :func:`reconcile_fno_ledger_windows` (pure). Called once per F&O phase at pull start —
    right after ``begin_pull_ledger``, BEFORE ``plan_contract_windows`` seeds ``PLANNED`` —
    so a fresh-clone / orphaned ledger inherits the lake's already-stored F&O windows as
    ``SUCCESS`` and the pull SKIPS them instead of re-fetching the whole chain
    (DEC-FNO-LEDGER-LAKE-RECONCILE-001). O(datasets) aggregate reads, never per-window.
    """
    from brutex.dashboard.coverage_reconcile import load_lake_spans

    spans = load_lake_spans(lake_root)
    return reconcile_fno_ledger_windows(
        ledger,
        spans,
        contracts=contracts,
        intervals=intervals,
        start=start,
        end=end,
        market=market,
        contract_windows=contract_windows,
        now=now,
        now_ist=now_ist,
    )


def _deep_strike_expected_empty(
    empty_retry_by_symbol: Mapping[str, int] | None,
    full_budget: int,
) -> Callable[[str, date], bool] | None:
    """The breaker's expected-empty predicate from the confirmed-deep strike budget.

    DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001 (register rule 4 — "use existing knowledge
    if available"): a contract whose in-pull EMPTY-retry budget was REDUCED below
    the full *full_budget* is a confirmed-deep far-OTM strike
    (:func:`~brutex.adapters.groww.strike_chain_bound.build_empty_retry_budget`) —
    the pull already PROVED it sits far outside the chain's traded band, so its
    empty windows are expected and must not count toward a mass-empty trip.
    ``None`` when no budget map is wired (nothing is provable — count everything,
    the honest default). Pure + **O(1)** per probe (one dict get).
    """
    if empty_retry_by_symbol is None:
        return None
    budget = empty_retry_by_symbol

    def _expected(symbol: str, _window_end: date) -> bool:
        return budget.get(symbol, full_budget) < full_budget

    return _expected


#: The mass-empty breaker's vendor-health CANARY coordinates
#: (DEC-EMPTY-BREAKER-CANARY-001): the NIFTY spot index is the repo's most
#: liquid, always-listed series (CLAUDE.md §6 Track-1 universe) — its recent
#: sessions can only come back empty when the VENDOR is unhealthy (throttle /
#: soft-ban / mis-addressing episode), never from genuine no-data.
_CANARY_EXCHANGE: Final[str] = "NSE"
_CANARY_SEGMENT: Final[str] = "CASH"
_CANARY_SYMBOL: Final[str] = "NSE-NIFTY"
#: Calendar days the canary window spans — 7 always contains ≥ 3 NSE trading
#: sessions (a weekend + holiday cluster cannot blank a whole week), and at
#: 1-minute it tiles into ONE 30-day vendor chunk = exactly ONE vendor call.
_CANARY_LOOKBACK_DAYS: Final[int] = 7


def make_breaker_canary_probe(
    client: GrowwClientProtocol,
    *,
    pace: Pacer | None = None,
    now_utc: Callable[[], datetime] | None = None,
) -> Callable[[], bool]:
    """Build the breaker's ONE-call vendor-health canary over *client* — pure closure.

    DEC-EMPTY-BREAKER-CANARY-001. The probe fetches the last
    :data:`_CANARY_LOOKBACK_DAYS` calendar days of :data:`_CANARY_SYMBOL` spot
    1-minute candles through the SAME client (and the SAME *pace* rate gate —
    the canary is one governed vendor call, never a cap bypass, CLAUDE.md §2
    Rule 6): bars returned ⇒ ``True`` (the vendor serves data right now — the
    breaker's mass empties are genuine no-data); a clean empty OR any raised
    error ⇒ ``False`` (an unreachable/erroring vendor is exactly the
    unhealthy case — the breaker halts as today; the probe itself NEVER
    raises). One ``empty_breaker.canary_probe`` log per invocation names the
    verdict.
    """

    def _probe() -> bool:
        try:
            if pace is not None:
                pace()
            end = (now_utc() if now_utc is not None else datetime.now(UTC)).date()
            bars = fetch_candle_windows(
                client,
                exchange=_CANARY_EXCHANGE,
                segment=_CANARY_SEGMENT,
                groww_symbol=_CANARY_SYMBOL,
                interval="1minute",
                start=end - timedelta(days=_CANARY_LOOKBACK_DAYS),
                end=end,
                drop_volume=True,  # an INDEX carries no traded volume
            )
        except Exception as exc:  # pylint: disable=broad-except — an erroring canary is NOT health evidence
            _LOG.warning(
                "empty_breaker.canary_probe",
                symbol=_CANARY_SYMBOL,
                healthy=False,
                error_type=type(exc).__name__,
                error=str(exc),
                note="canary fetch raised — treated as vendor-unhealthy",
            )
            return False
        healthy = len(bars) > 0
        _LOG.info(
            "empty_breaker.canary_probe",
            symbol=_CANARY_SYMBOL,
            bars=len(bars),
            healthy=healthy,
        )
        return healthy

    return _probe


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
    empty_retry_attempts: int = DEFAULT_EMPTY_RETRY_ATTEMPTS,
    empty_retry_by_symbol: Mapping[str, int] | None = None,
    run_parallel: RunParallel | None = None,
    on_raw_payload: RawPayloadHook | None = None,
    capture_hook_factory: CaptureHookFactory | None = None,
    write_pool: WriteConsumer | None = None,
    fetch_pipeline: FetchPipeline | None = None,
    observed_holidays: frozenset[date] = frozenset(),
    empty_breaker: EmptySettleBreaker | None = None,
    breaker_health_probe: Callable[[], bool] | None = None,
    storm_breaker: ErrorStormBreaker | None = None,
    write_breaker: WriteHealthBreaker | None = None,
    form_preference: SymbolFormPreference | None = None,
    audit: AuditWriter | None = None,
    market_closed: Callable[[], bool] | None = None,
    today_completeness_gate: bool = False,
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

    Raw-data integrity capture (DEC-RAW-DATA-INTEGRITY-001) is wired via TWO
    optional seams (both ``None`` = byte-identical pre-feature behaviour): an
    explicit pre-bound *on_raw_payload* (single-contract callers / tests) wins;
    otherwise the production *capture_hook_factory* (the live build sites inject
    :func:`~brutex.data.raw_integrity_store.make_capture_hook_factory` bound to the
    lake root) builds a hook correctly attributed to EACH contract's
    ``(exchange, FNO, contract.symbol)`` — one run pulls many contracts, so the
    per-contract hook is built per pair. The hook is best-effort (it can never halt
    a pull), so wiring it never changes the pull's outcome.

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

    *write_pool* (DEC-FETCH-WRITE-OVERLAP-LEVER1-001) is the F&O twin of the spot
    spine's hand-off: when supplied, *sink* is wrapped in a
    :class:`~brutex.data.write_consumer.WriteConsumerSink` so each contract's
    O(month-depth) lake write runs on a background shard worker instead of serially on
    the fetch worker, freeing the connection to keep fetching the next contract. The
    per-contract settle-after-durable ledger ordering is preserved BYTE-FOR-BYTE (the
    wrapper blocks until the durable write returns). ``None`` (default) is the
    synchronous path. The caller owns the pool's lifetime and MUST :meth:`drain` it
    before saving the window ledger.

    *empty_retry_attempts* / *empty_retry_by_symbol* (DEC-FETCH-PER-WINDOW-GUARANTEE-001)
    bound the in-pull EMPTY-retry budget. *empty_retry_attempts* is the FULL budget every
    contract gets by default (:data:`~brutex.adapters.ingest_common.DEFAULT_EMPTY_RETRY_ATTEMPTS`
    = 3). *empty_retry_by_symbol* is an OPTIONAL ``{groww_symbol: attempts}`` map (built by
    :func:`~brutex.adapters.groww.strike_chain_bound.build_empty_retry_budget`) that assigns a
    REDUCED budget to confirmed-deep far-OTM strikes — a strike far outside the chain's traded
    band almost certainly never traded, so paying 3× to re-confirm its empty is waste; the
    reduced budget (default 1) confirms the dead strike in ONE fetch. A symbol ABSENT from the
    map (or a ``None`` map) keeps the full *empty_retry_attempts*, so this never starves an
    in-band strike and is byte-identical to the pre-feature path when unwired. NO data is lost:
    the first non-empty fetch always wins, so a deep strike that DOES return bars is stored
    exactly as before — only the empty re-confirmation count changes.

    *fetch_pipeline* (DEC-GROWW-FETCH-ASYNC-SEPARATION-001) is the F&O twin of the spot
    spine's full async separation: when supplied, each contract's SUCCESS-path tail
    (session-filter + ``sink.append`` + conflict-aggregate + ``ledger.record``) is handed
    OFF to a background consumer so the fetch lane does ONLY fetch + enqueue — internal
    processing can never block or slow the fetch. Forwarded verbatim into every
    :func:`ingest_option_contract` call. ``None`` (default) keeps the fully-synchronous,
    byte-identical path. The caller OWNS the pipeline's lifetime and MUST :meth:`drain` it
    BEFORE saving the window ledger.

    *empty_breaker* (DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001): ``None`` (the default —
    every production caller) resolves a FRESH env-configured
    :class:`~brutex.adapters.empty_circuit_breaker.EmptySettleBreaker` for THIS pass,
    shared by every contract's window fetches, so a mass clean-empty burst (a vendor
    soft-throttle [R2] / mis-addressed chain [R3]) RAISES
    :class:`~brutex.adapters.empty_circuit_breaker.MassEmptySettleHalt` — a
    :data:`_GLOBAL_STOP_ERRORS` member: the pull halts loudly instead of settling
    poisoned windows ``SKIPPED``. Per-pass construction means breaker state can never
    leak across pulls. Tests inject a configured instance. An expired-options chain
    legitimately contains many all-empty deep strikes, so the PROVABLE expected-empty
    knowledge the pull already computes — the confirmed-deep far-OTM set behind
    *empty_retry_by_symbol* — is threaded into the resolved breaker's
    ``expected_empty`` predicate (those windows are observed, never counted; the
    register's "use existing knowledge" rule). In-band strikes still count, so a
    whole-chain empty burst (the proven M&M-``&`` shape) still trips (Golden
    Rule #10: the residual false-positive budget on genuinely-illiquid in-band
    chains rides on the env-tunable thresholds, not a silent exclusion).
    *breaker_health_probe* (DEC-EMPTY-BREAKER-CANARY-001) threads the resolved
    breaker's vendor-health canary: consulted ONCE, single-flight, when a trip
    rule first fires — a HEALTHY canary (data returned) means the mass empties
    are genuine (e.g. a 2020-era dead chain at the bucket head), so the phase
    is NOT halted (the settle law governs the empties) and the trip evidence
    resets; an unhealthy/erroring canary halts exactly as today. Ignored when
    an explicit *empty_breaker* instance is injected (that instance carries its
    own probe, if any); ``None`` (the default) is byte-identical to the
    pre-canary behaviour.

    *storm_breaker* (DEC-VENDOR-DOWN-BREAKER-001): the vendor-down error-storm
    breaker + dead-credential StillStale escalator, threaded verbatim into every
    contract's window fetches. Its trip
    (:class:`~brutex.adapters.error_storm_breaker.VendorDownError` /
    :class:`~brutex.adapters.groww.errors.GrowwCredentialDeadError`) is a
    :data:`_GLOBAL_STOP_ERRORS` member — the pull halts loudly. ``None`` (the
    default — dry-run / synthetic / unit-test callers) never arms it; ONLY the
    real-vendor production sites construct one via
    :func:`~brutex.adapters.error_storm_breaker.resolve_error_storm_breaker`
    (the ``capture_hook_factory`` / ``detect_completeness_gaps`` gating pattern).

    *today_completeness_gate* (DEC-PULL-TODAY-COMPLETENESS-GATE-001): the
    pre-settle TODAY minute-completeness gate, threaded verbatim into every
    contract's window tail (:mod:`brutex.data.today_completeness`) — a
    covers-today ``1minute`` window whose fetched TODAY bars carry the #837
    still-settling shape (OI-null / all-zero-volume-FUT) has today's bars
    WITHHELD from the sink and is demoted to a re-pullable ``FAILED`` under the
    streak-EXEMPT ``TODAY_INCOMPLETE`` reason. Default ``False``
    (library-inert); the production site passes ``(not dry_run) and
    resolve_today_completeness_enabled()`` (belt — the F&O dry-run client
    returns EMPTY payloads, so the gate can never fire offline anyway).

    *market_closed* (DEC-GROWW-MARKET-HOURS-GUARD-001): a zero-arg ``() -> bool``
    predicate threaded verbatim into every contract's fetch — ``True`` means the
    pull crossed into the NSE market-hours window ``[08:45, 15:35)`` IST and the
    contract raises :class:`~brutex.data.errors.MarketHoursHalt`, a
    :data:`_GLOBAL_STOP_ERRORS` member that halts the pull gracefully. ``None`` (the
    default — dry-run / synthetic / unit-test callers) never gates; only the real
    production entrypoints bind the live guard.

    *audit* (DEC-INGEST-AUDIT-WIRING-001 — Golden Rule #8): the optional
    hash-chained :class:`~brutex.orchestrator.audit.AuditWriter` this driver
    appends its trail to, MIRRORING the spot spine's events —
    ``INGEST_RUN_START`` → one ``INGEST_PAIR_DONE`` / ``INGEST_PAIR_FAILED``
    per ``(contract, interval)`` pair → ``INGEST_RUN_COMPLETE`` — via the ONE
    COMMON :func:`~brutex.adapters.ingest_common.maybe_audit` guard. Every
    append runs on the single controller drain thread (never a fetch worker),
    O(1) per event, so wiring it can never block or slow the fetch lane.
    HONEST envelope (Golden Rule #10): in async-pipeline mode
    (``fetch_pipeline`` wired — the production pull) the per-pair
    ``bars_appended`` recorded on the trail is the PRODUCER-resolved count
    (``0``; the background consumer folds the real durable counts post-drain)
    — exactly the semantics of the spot chain's pair rows under the same
    pipeline, never a fabricated number. A pull-wide halt raised out of the
    drain leaves the chain WITHOUT its ``INGEST_RUN_COMPLETE`` (the honest
    marker of an aborted pass, mirroring the spot chain). ``None`` (default —
    dry-run / synthetic / unit-test callers) writes nothing.

    Returns
    -------
    GrowwOptionIngestReport
        Per-pair :class:`~brutex.adapters.groww.ingest.IngestReport` for successes +
        the quarantined failures.
    """
    pull_id = current_pull_id()
    # One breaker per PASS (DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001): resolved here — not
    # per contract — so ALL contracts of this pass share ONE counter and a fresh
    # pass/pull always starts clean. The confirmed-deep far-OTM strike set (the
    # reduced-budget keys of *empty_retry_by_symbol*) is PROVABLE expected-empty
    # knowledge, threaded as the breaker's exclusion predicate so a legitimately
    # dead deep chain never masquerades as a throttle.
    breaker = (
        empty_breaker
        if empty_breaker is not None
        else resolve_empty_breaker(
            expected_empty=_deep_strike_expected_empty(empty_retry_by_symbol, empty_retry_attempts),
            # Vendor-health canary (DEC-EMPTY-BREAKER-CANARY-001): consulted ONCE
            # before a trip latches — a healthy canary means the mass empties are
            # genuine (a 2020-era dead chain at the bucket head), not a throttle,
            # so the phase is NOT halted. None (every non-wired caller) keeps the
            # trip-halts-immediately behaviour byte-identical.
            health_probe=breaker_health_probe,
        )
    )
    # One WRITE-health breaker per PASS (DEC-WRITE-HEALTH-BREAKER-001), mirroring the
    # empty breaker above: all contracts share ONE per-partition defer accounting (a
    # wedged volume fails every partition) and a fresh pass/pull always starts clean.
    # BRUTEX_WRITE_BREAKER=off yields None (byte-identical per-window defer behaviour).
    wbreaker = write_breaker if write_breaker is not None else resolve_write_breaker()
    # ONE shared symbol-form preference per PASS (DEC-SYMBOL-FORM-PREFERENCE-001):
    # every contract of this run shares the learned `&`-form winners (keyed by
    # underlying inside the candle fetch), so ONE `%26` recovery reorders the whole
    # chain's subsequent fetches. Thread-safe (the pool fan-out records from worker
    # threads). The production spine threads ONE PersistentSymbolFormPreference over
    # the instrument master through `form_preference` (DEC-FORM-WINNER-PERSIST-001 —
    # durable across market buckets, until-clean rounds AND pulls); the fresh
    # process-local default below serves only the direct/unwired callers.
    preference = form_preference if form_preference is not None else SymbolFormPreference()
    # Lever 1 (DEC-FETCH-WRITE-OVERLAP-LEVER1-001): route every contract write through
    # the background pool when wired, freeing the fetch connection from the serial
    # O(month-depth) write. `write_pool=None` leaves `sink` untouched (byte-identical).
    if write_pool is not None:
        sink = WriteConsumerSink(consumer=write_pool)
    runner: RunParallel = run_parallel if run_parallel is not None else SerialRunParallel()
    # Deterministic worklist: one job per (contract, interval) pair, in the caller's
    # contract order × interval order — the OUTER fan-out unit (each a distinct lake
    # partition). ``contracts`` is materialised ONCE (it may be a one-shot iterator) so it
    # can feed BOTH the jobs list AND the pre-plan below.
    contract_list = list(contracts)
    jobs: list[OptionJob] = [
        (contract, interval) for contract in contract_list for interval in intervals
    ]
    # Pre-plan every window PLANNED before the drain (DEC-FNO-WINDOW-PLAN-001) so the
    # /coverage/windows Options + Futures tabs show a real planned total + units up front —
    # idempotent (a settled prior-pull row is untouched) and byte-key-identical to what each
    # fetch records, so a PLANNED row is never a ghost. Skipped for a dry-run (ledger=None).
    if ledger is not None:
        plan_contract_windows(
            ledger,
            contracts=contract_list,
            intervals=intervals,
            start=start,
            end=end,
            market=market,
            contract_windows=contract_windows,
            now_ist=now_ist,
        )

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
        # Raw-data integrity capture (DEC-RAW-DATA-INTEGRITY-001): an explicit
        # pre-bound ``on_raw_payload`` wins (single-contract callers + tests); else
        # the production ``capture_hook_factory`` builds a hook correctly attributed
        # to THIS contract's ``(exchange, FNO, contract.symbol)`` — one ingest run
        # pulls many contracts, so the per-contract hook must be built here.
        contract_hook = on_raw_payload
        if contract_hook is None and capture_hook_factory is not None:
            contract_hook = capture_hook_factory(
                contract_exchange(contract), OPTION_SEGMENT, contract.symbol
            )
        # Per-contract EMPTY-retry budget (DEC-FETCH-PER-WINDOW-GUARANTEE-001): a confirmed-deep
        # far-OTM strike gets a REDUCED budget (default 1) so a dead strike costs ONE fetch to
        # confirm empty instead of three — driving fetches-per-window toward ~1.0 WITHOUT losing
        # data (the first non-empty fetch still wins, so a deep strike that DOES return bars is
        # stored exactly as before). A symbol absent from the map keeps the full
        # ``empty_retry_attempts`` (every in-band option + every future), so a ``None`` map is
        # byte-identical to the pre-feature behaviour.
        contract_empty_retry = (
            empty_retry_attempts
            if empty_retry_by_symbol is None
            else empty_retry_by_symbol.get(contract.symbol, empty_retry_attempts)
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
                empty_retry_attempts=contract_empty_retry,
                now_ist=now_ist,
                market=market,
                on_raw_payload=contract_hook,
                fetch_pipeline=fetch_pipeline,
                observed_holidays=observed_holidays,
                empty_breaker=breaker,
                storm_breaker=storm_breaker,
                write_breaker=wbreaker,
                form_preference=preference,
                market_closed=market_closed,
                today_completeness_gate=today_completeness_gate,
            )

    pairs: dict[tuple[str, str], IngestReport] = {}
    failures: list[OptionPairFailure] = []
    # Golden Rule #8 (DEC-INGEST-AUDIT-WIRING-001): the F&O trail mirrors the spot
    # spine's — RUN_START before the drain, one PAIR row per outcome on the
    # controller thread, RUN_COMPLETE after the drain.
    maybe_audit(
        audit,
        "INGEST_RUN_START",
        f"pull:{pull_id}",
        {
            "contracts": len(contract_list),
            "intervals": list(intervals),
            "jobs": len(jobs),
            "market": market,
            "start": start.isoformat(),
            "end": end.isoformat(),
        },
    )
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
            maybe_audit(
                audit,
                "INGEST_PAIR_DONE",
                f"{contract.symbol}:{interval}",
                {
                    "bars_appended": report.bars_appended,
                    "windows": report.windows,
                    "still_failed_windows": report.still_failed_windows,
                },
            )
            if on_progress is not None:
                on_progress(label, report.bars_appended, False)
            continue
        # A failed ItemResult always carries the captured worker exception.
        exc = outcome.exc
        if exc is None:  # pragma: no cover — a failed outcome always carries exc
            continue
        if isinstance(exc, _GLOBAL_STOP_ERRORS) and (
            not isinstance(exc, GrowwAuthError) or is_terminal_auth_failure(exc)
        ):
            # A GLOBAL stop (quota exhausted / quota-ledger invariant / disk-full / a
            # TERMINAL dead-credential auth failure, DEC-GROWW-AUTH-HALT-001) is NOT one
            # contract's problem — re-raise so it propagates to the caller's halt
            # handling (DEC-SPOT-QUOTA-MIDROUND-001) instead of being quarantined as a
            # single pair (the carve-out from the broad BruteXError quarantine below).
            # The RETRYABLE ``GrowwAuthError`` subclasses (a throttled mint / a stale
            # read-only re-read) are carved BACK OUT here: transient by design, they
            # fall through to the ordinary per-contract quarantine, never a halt.
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
        maybe_audit(
            audit,
            "INGEST_PAIR_FAILED",
            f"{contract.symbol}:{interval}",
            {"error": str(exc), "auth_failure": auth_failed},
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
    # Observe-only (DEC-SUSPICIOUS-EMPTY-FLAG-001): surface a whole underlying whose
    # every cleanly-fetched contract came back empty (the BAJAJ-AUTO/M&M silent-vanish
    # case) — a WARNING for human triage, NEVER an engine input or auto-action. Wrapped
    # so the diagnostic can never break the pull (mirrors completeness.py #907).
    #
    # SKIP in async-pipeline mode (DEC-GROWW-FETCH-ASYNC-SEPARATION-001 MAJOR-3 fix): when a
    # ``fetch_pipeline`` is wired the per-pair ``IngestReport`` is producer-resolved with
    # ``bars_appended == 0`` (the real bar counts are folded onto the pipeline's pull-level
    # totals by the BACKGROUND consumer, NOT known synchronously here — the drain has not run
    # yet). Scanning now would see EVERY pair as 0-bars and flood a false
    # ``pull.underlying_all_empty`` for the whole universe. The honest "all empty" signal needs
    # the per-pair durable counts, which only exist after the drain — the PRODUCTION F&O pull
    # (always pipeline-wired) runs the scan POST-DRAIN over the reconciled pairs via the SAME
    # COMMON :func:`emit_underlying_all_empty` helper (DEC-EMPTY-DETECTOR-PRODUCTION-WIRING-001,
    # in ``dashboard/historical_pull.py``), so this skip no longer leaves the detector dead.
    if fetch_pipeline is None:
        emit_underlying_all_empty(
            pairs,
            contract_list,
            failed_symbols=[f.symbol for f in failures],
            pull_id=pull_id,
        )
    maybe_audit(
        audit,
        "INGEST_RUN_COMPLETE",
        f"pull:{pull_id}",
        {
            "pairs_ok": len(pairs),
            "pairs_failed": len(failures),
            "total_bars": sum(report.bars_appended for report in pairs.values()),
        },
    )
    run_report = GrowwOptionIngestReport(pairs=pairs, failures=tuple(failures))
    # VISIBLE settled-window skips (DEC-WINDOW-LEDGER-RESCUE-001): ONE INFO line per
    # driver pass with the AGGREGATE count — the per-window skip stays DEBUG (a
    # universe pull skips tens of thousands), but the operator could previously never
    # SEE the no-vendor-hit skip engaging at the default log level at all. Emitted
    # unconditionally (0 is an honest "nothing was skippable"), on the controller
    # thread, after the fan-out drain.
    _LOG.info(
        "option.ingest.windows_skipped_settled",
        pull_id=pull_id,
        market=market,
        windows_skipped_settled=run_report.skipped_windows,
        pairs=len(pairs),
    )
    return run_report


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
    (``None`` = the full-range, pre-feature behaviour). **WARNING
    (DEC-FNO-FAR-EXPIRY-WINDOW-001):** ``contract_windows=None`` BYPASSES the strict
    default-window law — any future caller MUST pass windows resolved via
    :func:`~brutex.adapters.groww.contract_bounds.default_fno_minute_window` (skipping
    a ``None``/not-yet-open contract) or it re-introduces the full-range over-pull the
    law forbids. *run_parallel*
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
    **WARNING (DEC-FNO-FAR-EXPIRY-WINDOW-001):** ``contract_windows=None`` BYPASSES
    the strict default-window law — any future caller MUST pass windows resolved via
    :func:`~brutex.adapters.groww.contract_bounds.default_fno_minute_window` (skipping
    a ``None``/not-yet-open contract) or it re-introduces the full-range over-pull the
    law forbids.
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
    "SUSPECTED_PARTIAL_WINDOW_ERROR_EVENT",
    "SUSPECTED_PARTIAL_WINDOW_EVENT",
    "UNDERLYING_ALL_EMPTY_EVENT",
    "AllEmptyScan",
    "ContractWindows",
    "GrowwOptionIngestReport",
    "OptionJob",
    "OptionPairFailure",
    "Pacer",
    "ProgressHook",
    "UnderlyingEmptyReport",
    "contract_exchange",
    "emit_underlying_all_empty",
    "fetch_option_candles",
    "ingest_option_contract",
    "ingest_underlying_futures",
    "ingest_underlying_options",
    "make_breaker_canary_probe",
    "plan_contract_windows",
    "reconcile_fno_ledger_windows",
    "reconcile_fno_ledger_with_lake",
    "run_groww_option_ingest",
    "scan_underlying_all_empty",
]
